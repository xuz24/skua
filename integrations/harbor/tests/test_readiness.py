"""Final run gates, cancellation, and cleanup ownership; no paid endpoints."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading

import pytest

from test_reporting import load_runner, fixture_tasks


def test_full_gate_requires_verified_success_and_exact_evaluated_build():
    runner = load_runner()
    _, full = runner.select_tasks(fixture_tasks(runner), 'text-only', 'full')
    _, pilot = runner.select_tasks(fixture_tasks(runner), 'text-only', 'pilot-runtime')
    expected = {'bundle': {'sha': 'current'}, 'max_seconds': 300, 'model': 'test',
                'adapter_files_sha256': {'runner': 'current'}}
    pilot.update(expected, stopped_reason='completed')
    rows = [{'task': n, 'category': 'passed', 'rewards': {'reward': 1}} for n in runner.PILOT_TASKS]
    runner.check_full_gate({'native_tool_call': True}, rows, pilot, full, expected)
    for bad in ([{**r, 'category': 'task', 'rewards': {'reward': 0}} for r in rows],
                rows[:-1], [rows[0]] * 3,
                [{**rows[0], 'category': 'infrastructure'}, *rows[1:]]):
        with pytest.raises(SystemExit, match='Pilot gate failed'):
            runner.check_full_gate({'native_tool_call': True}, bad, pilot, full, expected)
    for key in expected:
        with pytest.raises(SystemExit, match='configuration differs'):
            runner.check_full_gate({'native_tool_call': True}, rows, {**pilot, key: 'stale'}, full, expected)
    with pytest.raises(SystemExit, match='Pilot gate failed'):
        runner.check_full_gate({'native_tool_call': True}, rows, {**pilot, 'stopped_reason': 'signal'}, full, expected)


@pytest.mark.parametrize('ignore_term', [False, True])
def test_stop_controller_signals_active_child_and_never_launches_next(tmp_path, ignore_term):
    runner = load_runner()
    controller = runner.StopController(grace=.1)
    ready = tmp_path / 'ready'
    # The monitor requests cancellation only after the child installed its handler.
    code = ('import signal, time; from pathlib import Path; '
            f'signal.signal(signal.SIGTERM, signal.SIG_IGN if {ignore_term} else lambda *_: exit(0)); '
            f'Path({str(ready)!r}).touch(); time.sleep(20)')
    def stop_when_ready():
        import time
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        controller.request(signal.SIGTERM)
    thread = threading.Thread(target=stop_when_ready)
    thread.start()
    try:
        result = controller.run([sys.executable, '-c', code])
    finally:
        thread.join(timeout=6)
    assert result == (-signal.SIGKILL if ignore_term else 0)
    assert controller.forced is ignore_term
    assert controller.run(['must-not-launch']) is None


def test_signal_handlers_are_restored():
    runner = load_runner()
    original = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    controller = runner.StopController()
    controller.install()
    try:
        signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGINT)
        assert controller.signum == signal.SIGTERM
    finally:
        controller.restore()
    assert {s: signal.getsignal(s) for s in original} == original


def test_interrupted_cleanup_only_removes_exact_trial_resources(tmp_path, monkeypatch):
    runner = load_runner()
    job = tmp_path / 'jobs' / 'fixture'
    trial = job / 'fixture__ABC'
    trial.mkdir(parents=True)
    task = tmp_path / 'tasks' / 'fixture'
    config = {'trial_name': trial.name, 'trials_dir': str(job), 'task': {'path': str(task)}}
    (trial / 'config.json').write_text(json.dumps(config))
    listed, removed = [], []
    def listing(command, **kwargs):
        listed.append(command)
        return 'owned-id\n'
    def removal(command, **kwargs):
        removed.append(command)
    monkeypatch.setattr(runner.subprocess, 'check_output', listing)
    monkeypatch.setattr(runner.subprocess, 'run', removal)
    assert runner.cleanup_interrupted_trial(job, task) == []
    assert len(listed) == len(removed) == 3
    assert all(c[-1] == 'label=com.docker.compose.project=fixture__abc__env' for c in listed)
    assert all(c[-1] == 'owned-id' for c in removed)
    (trial / 'config.json').write_text(json.dumps({**config, 'trials_dir': '/unrelated'}))
    assert runner.cleanup_interrupted_trial(job, task) == ['trial_identity_mismatch']
    assert len(removed) == 3


def test_cleanup_failure_is_visible(tmp_path, monkeypatch):
    runner = load_runner()
    job = tmp_path / 'jobs'
    trial = job / 'fixture'
    trial.mkdir(parents=True)
    (trial / 'config.json').write_text(json.dumps({'trial_name': 'fixture', 'trials_dir': str(job),
                                                 'task': {'path': str(tmp_path)}}))
    def failed(*args, **kwargs):
        raise subprocess.TimeoutExpired('docker', 30)
    monkeypatch.setattr(runner.subprocess, 'check_output', failed)
    assert runner.cleanup_interrupted_trial(job, tmp_path) == ['containers', 'networks', 'volumes']


def test_secondary_flags_keep_multiple_observed_signals_without_private_text():
    runner = load_runner()
    events = [{'type': 'tool_result', 'status': 'error', 'error': {'code': 'invalid_arguments'}},
              {'type': 'tool_result', 'status': 'error', 'model_text': 'ModuleNotFoundError private-package'},
              {'type': 'tool_result', 'status': 'ok', 'model_text': 'command not found is documentation'},
              {'type': 'notice', 'code': 'model_refusal'}]
    flags = runner.secondary_flags(events, [{'finish_reason': 'length'}, {'finish_reason': 'content_filter'}])
    assert flags == ['content_filtered', 'invalid_arguments', 'missing_dependency_observed',
                     'model_refusal', 'response_truncated']


def test_interrupted_scheduler_retains_active_row_and_stops_next_task(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace
    runner = load_runner()
    archive = b'fixture'
    (tmp_path / 'dataset.tar.gz').write_bytes(archive)
    monkeypatch.setattr(runner, 'ARCHIVE_SHA', hashlib.sha256(archive).hexdigest())
    tasks = [{**t, 'path': t['name']} for t in fixture_tasks(runner)]
    (tmp_path / 'dataset.json').write_text(json.dumps({'tasks': tasks}))
    monkeypatch.setattr('sys.argv', ['run.py', '--phase', 'pilot-runtime', '--data', str(tmp_path), '--bundle', str(tmp_path)])
    monkeypatch.setattr(runner, 'checked_bundle', lambda _: (tmp_path, {'files': {}}))
    profile = SimpleNamespace(model='gpt-5.6-luna', base_url='https://unused.invalid', auth=SimpleNamespace(env_var='SKUA_API_KEY'))
    monkeypatch.setattr(runner, 'load_config', lambda _: SimpleNamespace(profile=lambda: profile))
    monkeypatch.setattr(runner, 'resolve_root', lambda _: tmp_path)
    monkeypatch.setattr(runner, 'resolve_credential', lambda _: 'unused-secret')
    monkeypatch.setattr(runner.shutil, 'disk_usage', lambda _: SimpleNamespace(free=10 * 1024**3))
    def copy(source, target, **kwargs):
        target.mkdir(parents=True)
        (target / 'task.toml').write_text('[environment]\ndocker_image="fixture:latest"\n')
    monkeypatch.setattr(runner.shutil, 'copytree', copy)
    monkeypatch.setattr(runner, 'capture', lambda command, **kwargs: json.dumps({'Descriptor': {
        'digest': 'sha256:fixture', 'platform': {'architecture': 'amd64'}}}) if command[0] == 'docker' else 'fixture')
    monkeypatch.setattr(runner.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0))
    gateway = SimpleNamespace(token='mock-token', server_address=('localhost', 1), completion_limit=8192,
                              serve_forever=lambda: None, shutdown=lambda: None, server_close=lambda: None)
    monkeypatch.setattr(runner, 'Gateway', lambda *args, **kwargs: gateway)
    controller = runner.StopController()
    calls = []
    def interrupted(command, **kwargs):
        calls.append(command)
        controller.request(signal.SIGTERM)
        return -signal.SIGTERM
    monkeypatch.setattr(controller, 'run', interrupted)
    monkeypatch.setattr(runner, 'StopController', lambda: controller)
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 143 and len(calls) == 1
    output = tmp_path / 'pilot-runtime'
    rows = json.loads((output / 'results.json').read_text())
    assert len(rows) == 1 and rows[0]['category'] == 'interrupted'
    assert rows[0]['signal'] == 'SIGTERM' and rows[0]['cleanup_failures'] == []
    assert json.loads((output / 'manifest.json').read_text())['stopped_reason'] == 'signal'
    assert json.loads((output / 'summary.json').read_text())['unrecorded_tasks'] == 2
