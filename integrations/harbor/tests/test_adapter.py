"""Use real Harbor 0.25.0 models/base class; mock only the container boundary."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'

import pytest
from harbor.agents.installed.base import BaseInstalledAgent
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories import Trajectory

from skua_harbor.agent import SkuaAgent, checked_bundle
from skua_harbor.atif import export_trajectory


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / 'bundle'
    directory.mkdir()
    files = {}
    for kind, name in [('wheel', 'skua-1.0.0-py3-none-any.whl'), ('requirements', 'requirements.txt')]:
        path = directory / name
        path.write_bytes(b'fixture bytes')
        files[kind] = {'name': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    (directory / 'manifest.json').write_text(json.dumps({'version': 1, 'files': files}))
    return directory


def agent(tmp_path, bundle, **kwargs):
    return SkuaAgent(tmp_path / 'logs', bundle_path=str(bundle), model_name='openai/mock',
                     base_url='https://mock.invalid/v1', **kwargs)


class Environment:
    default_user = None
    def __init__(self, *, code=0, interrupted=False, reason=None):
        self.commands = []
        self.uploads = {}
        self.code = code
        self.interrupted = interrupted
        self.reason = reason

    async def exec(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if 'pwd -P' in command:
            return SimpleNamespace(return_code=0, stdout='/workspace\n', stderr='')
        if '/runner.py --settings' in command:
            if self.interrupted:
                raise TimeoutError('outer timeout')
            return SimpleNamespace(return_code=self.code, stdout='untrusted-output', stderr='untrusted-error')
        return SimpleNamespace(return_code=0, stdout='', stderr='')

    async def upload_file(self, source, target):
        self.uploads[target] = Path(source).read_bytes()

    async def download_file(self, source, target):
        if source.endswith('result.json') and not self.interrupted:
            Path(target).write_text(json.dumps({'status': 'completed' if self.code == 0 else 'failed',
                'reason': self.reason or ('turn_completed' if self.code == 0 else 'turn_error')}))
        else:
            raise FileNotFoundError(source)


def test_real_base_class_and_options_preflight(tmp_path, bundle):
    instance = agent(tmp_path, bundle, auth_enabled=False)
    assert isinstance(instance, BaseInstalledAgent)
    assert not instance.__abstractmethods__
    SkuaAgent.preflight({'bundle_path': str(bundle), 'max_seconds': 3, 'max_tool_calls': 2,
                        'auth_enabled': False, 'base_url': 'http://localhost/v1'})
    assert instance.model_connection.model_id == 'mock'
    assert instance.model_connection.model_api_format == 'openai_chat_completions'


@pytest.mark.parametrize('kwargs', [{'max_seconds': float('inf')}, {'max_tool_calls': 0},
                                   {'max_tool_calls': 1.5}, {'auth_enabled': 'anything'}])
def test_reject_invalid_options(tmp_path, bundle, kwargs):
    with pytest.raises(ValueError):
        agent(tmp_path, bundle, **kwargs)


def test_bundle_tampering_rejected_before_install(tmp_path, bundle):
    instance = agent(tmp_path, bundle, auth_enabled=False)
    (bundle / 'requirements.txt').write_text('tampered')
    environment = Environment()
    with pytest.raises(ValueError, match='hash mismatch'):
        asyncio.run(instance.install(environment))
    assert environment.commands == []


def test_manifest_cannot_escape_bundle(bundle):
    path = bundle / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['files']['wheel']['name'] = '../escape.whl'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='filename'):
        checked_bundle(bundle)


def test_installer_uses_hashes_and_isolated_python(tmp_path, bundle):
    instance = agent(tmp_path, bundle, auth_enabled=False)
    environment = Environment()
    asyncio.run(instance.setup(environment))
    commands = '\n'.join(c for c, _ in environment.commands)
    assert 'sha256sum -c' in commands and 'python3 -m venv venv' in commands
    assert 'pip install --no-deps skua-1.0.0-py3-none-any.whl' in commands
    assert '>= (3, 12)' in commands and 'pip check' in commands
    assert all('user' not in kw or kw['user'] == 'root' for _, kw in environment.commands)
    assert json.loads((instance.logs_dir / 'artifact.json').read_text())['harbor_version'] == '0.25.0'


@pytest.mark.parametrize('code', [0, 1, 124])
def test_safe_task_transport_env_credentials_and_failure_retention(tmp_path, bundle, code):
    instance = agent(tmp_path, bundle, extra_env={'SKUA_API_KEY': 'private-key'})
    environment = Environment(code=code)
    context = AgentContext()
    instruction = 'Quotes " and $(touch /escape);\nUnicode 🐦'
    if code:
        with pytest.raises(RuntimeError, match='inspect result.json'):
            asyncio.run(instance.run(instruction, environment, context))
    else:
        asyncio.run(instance.run(instruction, environment, context))
    commands = '\n'.join(c for c, _ in environment.commands)
    assert instruction not in commands and 'private-key' not in commands
    assert any(value == instruction.encode() for value in environment.uploads.values())
    run = next(kw for c, kw in environment.commands if '/runner.py --settings' in c)
    assert run['env'] == {'SKUA_API_KEY': 'private-key'} and run['cwd'] == '/workspace'
    settings = json.loads(next(v for p, v in environment.uploads.items() if p.endswith('settings.json')))
    assert settings['model'] == 'mock' and settings['base_url'] == 'https://mock.invalid/v1'
    assert 'private-key' not in json.dumps(settings)
    assert context.n_input_tokens is None and context.cost_usd is None
    assert (instance.logs_dir / 'result.json').exists()


def test_outer_timeout_retains_actionable_metadata(tmp_path, bundle):
    instance = agent(tmp_path, bundle, auth_enabled=False)
    context = AgentContext()
    with pytest.raises(TimeoutError):
        asyncio.run(instance.run('task', Environment(interrupted=True), context))
    assert context.metadata['skua_reason'] == 'outer_interruption_or_missing_artifacts'


def test_rejects_unsupported_transport(tmp_path, bundle):
    with pytest.raises(ValueError):
        instance = agent(tmp_path, bundle, auth_enabled=False, model_api_format='openai_responses')
        asyncio.run(instance.run('task', Environment(), AgentContext()))


BOOTSTRAP = r'''
import json, os, runpy, sys
from unittest.mock import patch
import httpx
mode = os.environ['SKUA_FIXTURE_MODE']
count = 0
def handler(request):
    global count
    count += 1
    calls = []
    if count == 1:
        if mode == 'runtime-python':
            name, args = 'run_command', {'command': str(__import__('pathlib').Path(os.environ['SKUA_FIXTURE_INTERPRETER'])) + ' -c \"print(42)\"'}
        elif mode == 'protected-settings':
            name, args = 'run_command', {'command': 'cat settings.json'}
        elif mode == 'success':
            name, args = 'apply_patch', {'message': 'fixture', 'changes': [{'op': 'write', 'path': 'answer.txt', 'content': 'harbor-ready\n'}]}
        else:
            name, args = 'run_command', {'command': 'printf started; sleep 30' if mode == 'timeout' else 'exit 7'}
        calls = [{'id': 'call', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]
    delta = {'role': 'assistant', 'content': 'Fixture response', 'tool_calls': [dict(c, index=i) for i, c in enumerate(calls)]}
    frames = [{'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
              {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if calls else 'stop'}]}]
    body = ''.join('data: ' + json.dumps(f) + '\n\n' for f in frames) + 'data: [DONE]\n\n'
    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body.encode())
runner = sys.argv.pop(1)
with patch.object(httpx.AsyncClient, '_transport_for_url', lambda self, url: httpx.MockTransport(handler)):
    runpy.run_path(runner, run_name='__main__')
'''


@pytest.mark.parametrize('mode,code,status', [('success', 0, 'completed'), ('failure', 1, 'failed'), ('timeout', 124, 'budget_exhausted'),
    ('runtime-python', 0, 'completed'), ('protected-settings', 3, 'approval_required')])
def test_native_runner_and_pinned_atif_schema(tmp_path, bundle, mode, code, status):
    repo = Path(__file__).resolve().parents[3]
    python = Path(os.environ.get('SKUA_RUNTIME_PYTHON', str(repo / '.venv/bin/python'))).absolute()
    root = tmp_path / 'project'
    root.mkdir()
    logs = tmp_path / 'native'
    logs.mkdir()
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'root': str(root), 'base_url': 'http://mock.invalid/v1', 'model': 'mock',
        'auth_enabled': False, 'max_seconds': .5 if mode == 'timeout' else 10, 'max_tool_calls': 3}))
    task = tmp_path / 'task.txt'
    task.write_text('Fixture " $(touch escaped)\n')
    runner = Path(__file__).parents[1] / 'skua_harbor/runner.py'
    interpreter = root / 'installed-agent' / 'python'
    interpreter.parent.mkdir()
    interpreter.symlink_to(python)
    (root / 'settings.json').write_text('protected fixture')
    env = dict(os.environ, SKUA_FIXTURE_MODE=mode, SKUA_FIXTURE_INTERPRETER=str(interpreter),
               PYTHONPATH='' if 'SKUA_RUNTIME_PYTHON' in os.environ else str(repo), HOME=str(tmp_path))
    result = subprocess.run([str(python), '-c', BOOTSTRAP, str(runner), '--settings', str(settings),
        '--task', str(task), '--logs', str(logs)], cwd=root, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=15)
    assert result.returncode == code, result.stderr
    assert json.loads((logs / 'result.json').read_text())['status'] == status
    trajectory = export_trajectory(logs / 'session.jsonl', wheel_sha256='a' * 64)
    parsed = Trajectory.model_validate_json(trajectory.model_dump_json(exclude_none=True))
    assert parsed.schema_version == 'ATIF-v1.8' and parsed.final_metrics is None
    assert parsed.steps[1].tool_calls[0].tool_call_id == parsed.steps[1].observation.results[0].source_call_id
    if mode == 'success':
        assert (root / 'answer.txt').read_text() == 'harbor-ready\n'
    assert not (root / 'escaped').exists()
    instance = agent(tmp_path, bundle, auth_enabled=False)
    for name in ('session.jsonl', 'result.json'):
        (instance.logs_dir / name).write_bytes((logs / name).read_bytes())
    context = AgentContext()
    instance.populate_context_post_run(context)
    assert context.metadata['skua_status'] == status
    assert (instance.logs_dir / 'trajectory.json').exists()


def test_export_rejects_incomplete_history(tmp_path):
    path = tmp_path / 'incomplete.jsonl'
    path.write_text(json.dumps({'type': 'session', 'schema_version': 1, 'event_count': 2}) + '\n')
    with pytest.raises(ValueError, match='Incomplete'):
        export_trajectory(path, wheel_sha256='a' * 64)


def test_pinned_job_and_task_models_accept_smoke_configuration(tmp_path, bundle):
    import importlib.util
    import tomllib
    from harbor.models.job.config import JobConfig
    from harbor.models.task.config import TaskConfig
    smoke = Path(__file__).parents[1] / 'smoke'
    spec = importlib.util.spec_from_file_location('smoke_run', smoke / 'run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = JobConfig.model_validate(module.job_config(tmp_path, bundle, 'success'))
    assert config.agents[0].import_path == 'skua_harbor.agent:SkuaAgent'
    SkuaAgent.preflight(config.agents[0].kwargs)
    task = TaskConfig.model_validate(tomllib.loads((smoke / 'task/task.toml').read_text()))
    assert task.agent.timeout_sec > config.agents[0].kwargs['max_seconds'] + 90


def test_real_harbor_factory_preserves_literal_model_and_named_secret(tmp_path, bundle, monkeypatch):
    from harbor.agents.factory import AgentFactory
    from harbor.models.trial.config import AgentConfig
    monkeypatch.setenv('CUSTOM_MODEL_TOKEN', 'named-private-key')
    config = AgentConfig(import_path='skua_harbor.agent:SkuaAgent', model_name='vendor/literal-model',
        skip_model_parsing=True, base_url='https://mock.invalid/v1',
        model_api_key_name='CUSTOM_MODEL_TOKEN', model_api_format='openai_chat_completions',
        kwargs={'bundle_path': str(bundle), 'max_tool_calls': 7})
    instance = AgentFactory.create_agent_from_config(config, logs_dir=tmp_path / 'logs')
    assert instance.model_connection.model_id == 'vendor/literal-model'
    assert instance.model_connection.api_key == 'named-private-key'
    assert instance.options.max_tool_calls == 7


def test_invalid_native_artifact_is_retained_without_leaking_error(tmp_path, bundle):
    instance = agent(tmp_path, bundle, auth_enabled=False)
    (instance.logs_dir / 'session.jsonl').write_text('private-corrupt-data')
    (instance.logs_dir / 'result.json').write_text(json.dumps({'status': 'completed', 'reason': 'private-secret'}))
    context = AgentContext()
    instance.populate_context_post_run(context)
    assert context.metadata['trajectory_export'] == 'failed'
    assert context.metadata['skua_reason'] == 'invalid_outcome'
    assert 'private' not in (instance.logs_dir / 'trajectory-error.json').read_text()
    assert (instance.logs_dir / 'session.jsonl').read_text() == 'private-corrupt-data'


def test_optional_standalone_python_is_hashed_and_private(tmp_path, bundle):
    archive = bundle / 'cpython-3.12.12+20251014-x86_64-unknown-linux-gnu-install_only.tar.gz'
    archive.write_bytes(b'archive fixture')
    path = bundle / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['files']['python'] = {'name': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    path.write_text(json.dumps(manifest))
    instance = agent(tmp_path, bundle, auth_enabled=False)
    environment = Environment()
    asyncio.run(instance.setup(environment))
    commands = '\n'.join(c for c, _ in environment.commands)
    assert 'tar -xzf ' + archive.name in commands
    assert './python/bin/python3 -m venv venv' in commands
    assert 'test "$(uname -m)" = x86_64' in commands
    assert manifest['files']['python']['sha256'] in commands
    archive.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='hash mismatch'):
        checked_bundle(bundle)


def test_completed_turn_with_tool_failure_allows_external_verification(tmp_path, bundle):
    instance = agent(tmp_path, bundle, auth_enabled=False)
    context = AgentContext()
    environment = Environment(code=1, reason='unresolved_tool_error')
    asyncio.run(instance.run('task', environment, context))
    assert context.metadata['skua_status'] == 'failed'
    assert context.metadata['skua_reason'] == 'unresolved_tool_error'
    assert context.metadata['skua_exit_code'] == 1
    assert context.metadata['task_success_verified'] is False
    assert json.loads((instance.logs_dir / 'result.json').read_text())['status'] == 'failed'


def test_invalid_history_still_blocks_verification_after_tool_failure(tmp_path, bundle):
    instance = agent(tmp_path, bundle, auth_enabled=False)
    (instance.logs_dir / 'session.jsonl').write_text('invalid history')
    context = AgentContext()
    with pytest.raises(RuntimeError, match='trajectory export: failed'):
        asyncio.run(instance.run('task', Environment(code=1, reason='unresolved_tool_error'), context))


def test_snapshot_git_bootstrap_is_explicit_and_validated(tmp_path, bundle):
    instance = agent(tmp_path, bundle, auth_enabled=False, git_bootstrap='bullseye-20251020')
    environment = Environment()
    asyncio.run(instance.install(environment))
    command = environment.commands[0][0]
    assert 'snapshot.debian.org/archive/debian/20251020T000000Z' in command
    assert 'Dir::Etc::sourceparts=-' in command
    assert 'Dir::State::lists=' in command
    assert 'trusted=yes' not in command and 'allow-unauthenticated' not in command
    assert instance.options.git_bootstrap == 'bullseye-20251020'
    with pytest.raises(ValueError):
        agent(tmp_path, bundle, git_bootstrap='arbitrary-source')


def test_git_bootstrap_stops_on_package_install_failure():
    from skua_harbor.bootstrap import git_bootstrap_command
    # Mock only package lookup/install; run the actual shell control flow.
    prelude = '''
command() {
  if [ "$1" = -v ] && [ "$2" = git ]; then return 1; fi
  if [ "$1" = -v ] && [ "$2" = apt-get ]; then return 0; fi
  builtin command "$@"
}
apt-get() { if [ "$1" = update ]; then return 0; fi; return 42; }
git() { echo MUST_NOT_RUN; }
'''
    result = subprocess.run(['/bin/bash', '-c', prelude + git_bootstrap_command()],
                            capture_output=True, text=True)
    assert result.returncode == 42
    assert 'MUST_NOT_RUN' not in result.stdout


def test_qemu_runtime_check_runs_before_agent_upload_or_model(tmp_path, bundle):
    instance = agent(tmp_path, bundle, auth_enabled=False, qemu_runtime_check=True)
    class UnsupportedQemu(Environment):
        async def exec(self, command, **kwargs):
            if 'skua_qemu_code' in command:
                self.commands.append((command, kwargs))
                return SimpleNamespace(return_code=1, stdout='', stderr='Unsupported host emulation')
            return await super().exec(command, **kwargs)
    environment = UnsupportedQemu()
    with pytest.raises(Exception, match='Unsupported host emulation'):
        asyncio.run(instance.install(environment))
    assert not environment.uploads
    assert not any('/runner.py --settings' in command for command, _ in environment.commands)


def test_qemu_runtime_check_is_opt_in(tmp_path, bundle):
    environment = Environment()
    asyncio.run(agent(tmp_path, bundle, auth_enabled=False).install(environment))
    assert not any('skua_qemu_code' in command for command, _ in environment.commands)
