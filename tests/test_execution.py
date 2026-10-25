import asyncio
from dataclasses import replace
import os
import signal
from pathlib import Path
import shlex
import sys

import pytest

from skua.config import ConfigError, build_config
from skua.environment import discover_environments, prepare_command
from skua.execution import Capture, run_command

SHELLS = [p for p in ('/bin/bash', '/bin/zsh') if Path(p).exists()]


def config(root, shell='/bin/bash', **tables):
    return build_config(root, [('test', {'schema_version': 1, 'shell': {'executable': shell}, **tables})])


def run(cfg, command, **kwargs):
    return asyncio.run(run_command(cfg, command, **kwargs))


@pytest.mark.parametrize('shell', SHELLS)
def test_fresh_root_environment_and_streams(workspace, shell):
    cfg = config(workspace, shell)
    first = run(cfg, 'cd /; export SKUA_TEST_VAR=changed; printf first; printf error >&2')
    assert first.returncode == 0
    assert first.stdout.text() == 'first'
    assert first.stderr.text() == 'error'
    result = run(cfg, 'pwd; printf "%s" "${SKUA_TEST_VAR-unset}"')
    assert result.stdout.text() == f'{workspace}\nunset'


@pytest.mark.parametrize('shell', SHELLS)
def test_startup_and_activation_order(workspace, shell):
    # Zsh permits an isolated startup fixture through ZDOTDIR. Bash uses the
    # existing test fixture home, set before the subprocess is started.
    startup = Path.home() / ('.bashrc' if shell.endswith('bash') else '.zshrc')
    startup.write_text('printf startup; export SKUA_ORDER=startup; cd /\n')
    venv = workspace / 'env with spaces'
    (venv / 'bin').mkdir(parents=True)
    (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')
    (venv / 'bin/python').symlink_to(sys.executable)
    (venv / 'bin/activate').write_text('export SKUA_ORDER="${SKUA_ORDER-unset}-activated"\n')
    cfg = config(workspace, shell, environment={'kind': 'venv', 'path': str(venv)})
    env = dict(os.environ, ZDOTDIR=str(Path.home()))
    off = run(cfg, 'printf "%s" "$SKUA_ORDER"', environ=env)
    assert off.stdout.text() == 'unset-activated'
    cfg = replace(cfg, shell=replace(cfg.shell, startup_enabled=True))
    on = run(cfg, 'printf "%s:%s" "$SKUA_ORDER" "$PWD"', environ=env)
    assert on.stdout.text() == f'startupstartup-activated:{workspace}'


def test_bash_env_is_disabled(workspace):
    startup = workspace / 'startup'
    startup.write_text('printf unexpected')
    result = run(config(workspace), 'printf expected', environ=dict(os.environ, BASH_ENV=str(startup)))
    assert result.stdout.text() == 'expected'


def test_allowlist_and_missing_environment(workspace):
    (workspace / '.env').write_text('APP_MODE=added\nSKUA_API_KEY=secret\n')
    cfg = config(workspace, commands={'dotenv_allowlist': ['APP_MODE']})
    invocation = prepare_command(cfg, ':', {'PATH': os.defpath})
    assert invocation.env['APP_MODE'] == 'added'
    assert 'SKUA_API_KEY' not in invocation.env
    assert prepare_command(cfg, ':', {'APP_MODE': 'inherited'}).env['APP_MODE'] == 'inherited'
    cfg = config(workspace, environment={'kind': 'venv', 'path': 'missing'})
    with pytest.raises(ConfigError, match='unavailable'):
        run(cfg, 'touch should-not-exist')
    assert not (workspace / 'should-not-exist').exists()


def test_missing_shell_no_fallback(workspace):
    with pytest.raises(ConfigError, match='unavailable'):
        prepare_command(config(workspace, str(workspace / 'bash')), ':')


def test_discovery(workspace):
    venv = workspace / '.venv'
    venv.mkdir()
    (venv / 'pyvenv.cfg').touch()
    conda = workspace / 'conda'
    (conda / 'conda-meta').mkdir(parents=True)
    registry = Path.home() / '.conda/environments.txt'
    registry.parent.mkdir()
    registry.write_text(f'{conda}\n/missing\n')
    found = discover_environments(workspace, {'VIRTUAL_ENV': str(venv)})
    assert [(c.kind, c.path) for c in found] == [('venv', venv), ('conda', conda)]


@pytest.mark.parametrize('shell', SHELLS)
def test_conda_hook_and_failure(workspace, shell):
    conda = workspace / 'fake conda'
    conda.write_text("#!/bin/sh\ncat <<'HOOK'\nconda() { [ \"$1\" = activate ] && [ \"$2\" = good ] || return 1; export SKUA_CONDA_ACTIVE=yes; }\nHOOK\n")
    conda.chmod(0o755)
    env = dict(os.environ, CONDA_EXE=str(conda))
    cfg = config(workspace, shell, environment={'kind': 'conda', 'name': 'good'})
    assert run(cfg, 'printf "%s" "$SKUA_CONDA_ACTIVE"', environ=env).stdout.text() == 'yes'
    cfg = config(workspace, shell, environment={'kind': 'conda', 'name': 'missing'})
    result = run(cfg, 'touch forbidden', environ=env)
    assert result.returncode == 125
    assert not (workspace / 'forbidden').exists()


def test_bounded_noisy_streams(workspace):
    cfg = config(workspace, output={'retain_max_bytes': 1000, 'model_max_bytes': 100})
    script = "import os; os.write(1,b'HEAD'+b'x'*200000+b'TAIL'); os.write(2,b'ERR'+b'y'*200000+b'END')"
    result = run(cfg, f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}')
    assert result.returncode == 0
    assert result.stdout.head.startswith(b'HEAD') and result.stdout.tail.endswith(b'TAIL')
    assert result.stderr.head.startswith(b'ERR') and result.stderr.tail.endswith(b'END')
    assert result.stdout.omitted_bytes == 200008 - 500
    assert result.stderr.omitted_bytes == 200006 - 500
    assert 'HEAD' in result.preview() and 'END' in result.preview()
    assert 'discarded beyond retention' in result.preview()


@pytest.mark.parametrize('limit', [0, 1, 2, 7, 100])
def test_capture_chunk_boundaries(limit):
    capture = Capture(limit)
    for chunk in (b'abc', b'd', b'efghijkl'):
        capture.feed(chunk)
    result = capture.snapshot()
    assert len(result.head) + len(result.tail) <= limit
    assert result.total_bytes == 12
    assert result.omitted_bytes == max(0, 12-limit)


@pytest.mark.parametrize('cancel', [False, True])
def test_timeout_cancel_retains_output_and_changes(workspace, cancel):
    cfg = config(workspace, commands={'timeout_seconds': 1})
    # Parent shell and child both handle SIGTERM; command effects remain.
    command = "trap 'printf stopped; exit 0' TERM; touch changed; printf ready; while :; do sleep 0.1; done"
    async def scenario():
        task = asyncio.create_task(run_command(cfg, command, _grace_seconds=0.2))
        if cancel:
            while not (workspace / 'changed').exists():
                await asyncio.sleep(0.01)
            task.cancel()
        return await asyncio.wait_for(task, 3)
    result = asyncio.run(scenario())
    assert result.status == ('cancelled' if cancel else 'timeout')
    assert 'ready' in result.stdout.text() and 'stopped' in result.stdout.text()
    assert (workspace / 'changed').exists()


def test_force_stop_remaining_group(workspace):
    cfg = config(workspace, commands={'timeout_seconds': 1})
    # Shell exits on TERM but its child ignores TERM and retains the output pipe.
    script = "import signal,time,os; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('child-ready',flush=True); time.sleep(60)"
    command = f"trap 'exit 0' TERM; {shlex.quote(sys.executable)} -c {shlex.quote(script)} & wait"
    result = asyncio.run(asyncio.wait_for(run_command(cfg, command, _grace_seconds=0.2), 4))
    assert result.status == 'timeout'
    assert 'child-ready' in result.stdout.text()
    assert result.duration_seconds < 3


@pytest.mark.parametrize('shell', SHELLS)
def test_real_virtualenv_does_not_change_harness(workspace, shell):
    import json
    import venv
    target = workspace / 'project env'
    venv.EnvBuilder(with_pip=False).create(target)
    before = (sys.executable, sys.prefix, dict(os.environ))
    cfg = config(workspace, shell, environment={'kind': 'venv', 'path': 'project env'})
    script = 'import json,sys; print(json.dumps([sys.prefix,sys.executable]))'
    result = run(cfg, f'python -c {shlex.quote(script)}')
    assert result.returncode == 0
    prefix, executable = json.loads(result.stdout.text())
    assert Path(prefix) == target
    assert Path(executable) == target / 'bin/python'
    assert (sys.executable, sys.prefix, dict(os.environ)) == before


def test_repeated_cancellation_finishes_cleanup(workspace):
    cfg = config(workspace)
    async def scenario():
        command = "trap '' TERM; touch ready; printf output; while :; do sleep 1; done"
        task = asyncio.create_task(run_command(cfg, command, _grace_seconds=0.2))
        while not (workspace / 'ready').exists():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        return await task
    result = asyncio.run(scenario())
    assert result.status == 'cancelled'
    assert result.stdout.text() == 'output'
    assert result.returncode < 0


def test_conda_missing_executable_and_option_selector(workspace):
    cfg = config(workspace, environment={'kind': 'conda', 'name': 'missing'})
    with pytest.raises(ConfigError, match='executable is unavailable'):
        prepare_command(cfg, ':', {'PATH': '/nonexistent'})
    cfg = config(workspace, environment={'kind': 'conda', 'name': '--help'})
    with pytest.raises(ConfigError, match='hyphen'):
        prepare_command(cfg, ':', {'CONDA_EXE': '/bin/echo'})


def test_conda_path_is_quoted_and_hook_failure_stops_command(workspace):
    target = workspace / "conda env 'quoted'"
    (target / 'conda-meta').mkdir(parents=True)
    conda = workspace / 'conda'
    conda.write_text("#!/bin/sh\ncat <<'HOOK'\nconda() { export CONDA_PREFIX=\"$2\"; }\nHOOK\n")
    conda.chmod(0o755)
    cfg = config(workspace, environment={'kind': 'conda', 'path': str(target)})
    env = dict(os.environ, CONDA_EXE=str(conda))
    result = run(cfg, 'printf "%s" "$CONDA_PREFIX"', environ=env)
    assert result.stdout.text() == str(target)
    conda.write_text('#!/bin/sh\nexit 1\n')
    result = run(cfg, 'touch forbidden', environ=env)
    assert result.returncode == 125
    assert not (workspace / 'forbidden').exists()


def test_cancellation_during_spawn_cleans_process(workspace, monkeypatch):
    original = asyncio.create_subprocess_exec
    async def delayed_spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        await asyncio.sleep(0.1)
        return proc
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_spawn)
    async def scenario():
        task = asyncio.create_task(run_command(config(workspace), 'printf started; sleep 60', _grace_seconds=0.1))
        await asyncio.sleep(0.03)
        task.cancel()
        return await task
    result = asyncio.run(scenario())
    assert result.status == 'cancelled'
    assert result.stdout.text() == 'started'
    assert result.duration_seconds < 2


def test_preview_counts_retained_and_discarded_bytes():
    capture = Capture(10)
    capture.feed(b'abcdefghijklmnopqrst')
    retained = capture.snapshot()
    assert retained.head + retained.tail == b'abcdepqrst'
    preview = retained.preview(4)
    assert preview.head == b'ab' and preview.tail == b'st'
    assert retained.omitted_bytes == 10
    assert preview.omitted_bytes == 16


@pytest.mark.parametrize('sig', [signal.SIGTERM, 0, signal.SIGKILL])
def test_darwin_group_signal_retries_until_reaped(monkeypatch, sig):
    import errno
    from skua import execution
    monkeypatch.setattr(execution.sys, 'platform', 'darwin')
    attempts = []
    async def scenario():
        reaped = False
        def signal_group(pid, requested):
            attempts.append((pid, requested))
            if not reaped:
                raise PermissionError(errno.EPERM, 'unreaped group')
            return False
        async def reap():
            nonlocal reaped
            await asyncio.sleep(.005)
            reaped = True
        monkeypatch.setattr(execution, '_signal_group', signal_group)
        reaper = asyncio.create_task(reap())
        assert not await execution._signal_group_after_reaping(123, sig)
        await reaper
    asyncio.run(scenario())
    assert len(attempts) >= 2 and set(attempts) == {(123, sig)}


@pytest.mark.parametrize('platform,error', [('darwin', 1), ('linux', 1), ('darwin', 13)])
def test_group_signal_preserves_real_permission_errors(monkeypatch, platform, error):
    from skua import execution
    monkeypatch.setattr(execution.sys, 'platform', platform)
    attempts = []
    def denied(pid, sig):
        attempts.append(sig)
        raise PermissionError(error, 'denied')
    monkeypatch.setattr(execution, '_signal_group', denied)
    async def scenario():
        with pytest.raises(PermissionError) as caught:
            await asyncio.wait_for(execution._signal_group_after_reaping(123, 0), 1)
        assert caught.value.errno == error
    asyncio.run(scenario())
    if platform == 'darwin' and error == 1:
        assert len(attempts) > 1
    else:
        assert len(attempts) == 1


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin unreaped process-group regression')
def test_real_darwin_unreaped_group_cleanup():
    import subprocess
    import time
    from skua.execution import _signal_group_after_reaping
    # Unlike asyncio's subprocess watcher, Popen leaves reaping under test control.
    proc = subprocess.Popen(['/bin/sleep', '30'], start_new_session=True)
    async def scenario():
        os.killpg(proc.pid, signal.SIGTERM)
        # Wait until the kernel exposes the zombie state, without calling poll/wait.
        deadline = time.monotonic() + 2
        while True:
            try:
                os.killpg(proc.pid, 0)
            except PermissionError:
                break
            assert time.monotonic() < deadline
            await asyncio.sleep(.01)
        async def reap():
            await asyncio.sleep(.02)
            await asyncio.to_thread(proc.wait)
        reaper = asyncio.create_task(reap())
        assert not await _signal_group_after_reaping(proc.pid, 0)
        await reaper
    try:
        asyncio.run(scenario())
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
