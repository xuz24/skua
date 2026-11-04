"""M6: bounded shared turns plus real closed-stdin CLI subprocesses."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx
import pytest

from skua.client import ChatClient
from skua.config import build_config
from skua.headless import execute
from skua.storage import decode, encoded
from test_patches import init, git


def config(root, **tables):
    return build_config(root, [('test', {'schema_version': 1,
        'profiles': {'default': {'base_url': 'https://mock.invalid/v1', 'model': 'mock',
                                'auth': {'enabled': False}, 'stream': False}},
        'shell': {'executable': '/bin/bash'}, **tables})])


def response(text='', calls=()):
    return httpx.Response(200, json={'choices': [{'index': 0, 'finish_reason': 'tool_calls' if calls else 'stop',
        'message': {'role': 'assistant', 'content': text, 'tool_calls': [
            {'id': f'call{i}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
            for i, (name, args) in enumerate(calls)]}}]})


def run(root, *replies, cfg=None, **options):
    requests = []
    answers = iter(replies)
    def handler(request):
        requests.append(json.loads(request.content))
        return next(answers)
    factory = lambda p, k: ChatClient(p, k, transport=httpx.MockTransport(handler))
    result, session, _ = asyncio.run(execute(cfg or config(root), options.pop('prompt', 'task'), policy=options.pop('policy', 'disposable'),
                                  client_factory=factory, **options))
    if session:
        decode(encoded(session, None), root)
    return result, session, requests


def patch(content='fixed\n'):
    return ('apply_patch', {'message': 'Fix fixture', 'changes': [{'op': 'write', 'path': 'a.txt', 'content': content}]})


@pytest.mark.parametrize('repository', [False, True])
def test_unmanaged_patch_and_command_no_prompts_or_saves(workspace, repository):
    if repository:
        init(workspace)
        head = git(workspace, 'rev-parse', 'HEAD')
    result, session, requests = run(workspace, response(calls=[patch(), ('run_command', {'command': 'cat a.txt'})]), response('Done'))
    assert result['status'] == 'completed' and result['tool_calls'] == 2
    assert result['task_success_verified'] is False and result['provider_usage'] is None
    assert 'not been independently verified' in result['message']
    assert 'external verifier' in result['next_step']
    assert (workspace / 'a.txt').read_text() == 'fixed\n'
    if repository:
        assert git(workspace, 'rev-parse', 'HEAD') == head
    else:
        assert not (workspace / '.git').exists()
    assert not list((workspace / '.skua').glob('sessions/*'))
    approvals = [e for e in session.events if e['type'] == 'approval']
    assert [e['action'] for e in approvals] == ['patch', 'command']
    assert all(e['authorization']['source'] == 'unattended_policy' for e in approvals)


def test_explicit_managed_git_commits_only_with_disposable_policy(workspace):
    init(workspace)
    head = git(workspace, 'rev-parse', 'HEAD')
    result, session, _ = run(workspace, response(calls=[patch()]), response('Fix fixture'), response('Done'), managed_git=True)
    assert result['status'] == 'completed'
    assert git(workspace, 'rev-list', '--count', head + '..HEAD') == '1'
    assert any(e['type'] == 'approval' and e['action'] == 'commit' for e in session.events)


@pytest.mark.parametrize('policy,call', [('review', patch()), ('disposable', ('read_file', {'path': '.env'})),
                                       ('disposable', ('run_command', {'command': 'cat .env'}))])
def test_policy_stops_batch_without_final_model_request(workspace, policy, call):
    (workspace / '.env').write_text('SECRET=private')
    result, session, requests = run(workspace, response(calls=[call, patch()]), policy=policy)
    assert result['status'] == 'approval_required' and result['exit_code'] == 3
    assert len(requests) == 1
    assert [e['status'] for e in session.events if e['type'] == 'tool_result'] == ['denied', 'not_run']
    assert not (workspace / 'a.txt').exists()


def test_total_budget_counts_invalid_calls_and_stops_pending_batch(workspace):
    result, session, _ = run(workspace, response(calls=[('run_command', {'command': 123}), patch()]), max_tool_calls=1)
    assert result['status'] == 'budget_exhausted' and result['reason'] == 'tool_limit'
    assert result['tool_calls'] == 1 and not (workspace / 'a.txt').exists()
    assert [e['status'] for e in session.events if e['type'] == 'tool_result'] == ['error', 'not_run']


def test_argument_correction_preserves_total_call_budget(workspace):
    command = {'command': 'touch must-not-run'}
    result, session, _ = run(workspace,
        response(calls=[('run_command', {**command, 'unsupported_timeout': 1000})]),
        response(calls=[('run_command', {**command, 'max_output_chars': 999999})]),
        response(calls=[('run_command', command)]), max_tool_calls=2)
    assert result['status'] == 'budget_exhausted' and result['tool_calls'] == 2
    assert not (workspace / 'must-not-run').exists()


def test_argument_correction_cannot_extend_total_deadline(workspace):
    command = {'command': 'printf started; sleep 30'}
    result, session, _ = run(workspace,
        response(calls=[('run_command', {**command, 'unsupported_timeout': 30000})]),
        response(calls=[('run_command', command)]), max_seconds=.5)
    assert result['status'] == 'budget_exhausted' and result['reason'] == 'time_limit'
    assert result['message'] == 'Task time limit reached.'
    assert 'partial work' in result['next_step']
    assert [e['status'] for e in session.events if e['type'] == 'tool_result'] == ['error', 'cancelled']


def test_total_budget_spans_responses_and_replaces_interactive_block_limit(workspace):
    cfg = config(workspace, agent={'tool_calls_per_block': 1})
    result, session, requests = run(workspace, response(calls=[('list_files', {'path': '.'})]),
        response(calls=[('list_files', {'path': '.'}), patch()]), cfg=cfg, max_tool_calls=2)
    assert result['status'] == 'budget_exhausted' and result['tool_calls'] == 2
    assert not any(e['type'] == 'notice' and e['code'] == 'tool_block' for e in session.events)
    assert len(requests) == 2


def test_exact_budget_can_finish_without_another_tool(workspace):
    result, _, _ = run(workspace, response(calls=[('list_files', {'path': '.'})]), response('Done'), max_tool_calls=1)
    assert result['status'] == 'completed'


def test_failed_command_is_failed_not_verified_success(workspace):
    result, _, _ = run(workspace, response(calls=[('run_command', {'command': 'exit 7'})]), response('Done'))
    assert result['status'] == 'failed' and result['reason'] == 'unresolved_tool_error'


def test_failed_lint_can_be_corrected(workspace):
    cfg = config(workspace, lint={'enabled': True, 'command': 'grep fixed a.txt'})
    result, _, _ = run(workspace, response(calls=[patch('bad\n')]), response(calls=[patch()]), response('Done'), cfg=cfg)
    assert result['status'] == 'completed'


def test_recovery_blocks_before_credentials_or_endpoint(workspace, monkeypatch):
    recovery = workspace / '.skua/.tmp/patch-unresolved.json'
    recovery.parent.mkdir(parents=True)
    recovery.write_text('even an invalid snapshot must block')
    monkeypatch.setattr('skua.headless.resolve_credential', lambda _: pytest.fail('credentials should not be resolved'))
    result, session, requests = run(workspace)
    assert result['status'] == 'approval_required' and result['reason'] == 'recovery_pending'
    assert session is None and requests == [] and recovery.exists()


def test_time_budget_cleans_command_and_retains_output(workspace):
    result, session, _ = run(workspace, response(calls=[('run_command', {'command': 'printf started; sleep 30'})]), max_seconds=.5)
    assert result['status'] == 'budget_exhausted' and result['reason'] == 'time_limit'
    output = next(e for e in session.events if e['type'] == 'tool_result')['output']
    assert output['retained_bytes'] == 7


def test_time_budget_stops_model_wait(workspace):
    async def handler(request):
        await asyncio.Event().wait()
    factory = lambda p, k: ChatClient(p, k, transport=httpx.MockTransport(handler))
    result, session, _ = asyncio.run(execute(config(workspace), 'task', policy='review',
                                            max_seconds=.05, client_factory=factory))
    assert result['reason'] == 'time_limit' and result['status'] == 'budget_exhausted'
    assert session.events[-1]['status'] == 'cancelled'


# A real CLI process with closed stdin, actual HTTPX/ChatClient parsing, and an
# in-process mocked transport. No test-only transport switch enters production.
CHILD = r'''
import json, os, socket
from unittest.mock import patch
import httpx
from skua.cli import main

def blocked(*args, **kwargs):
    raise AssertionError('Unexpected real network connection')
socket.socket.connect = blocked
count = 0
def handler(request):
    global count
    count += 1
    mode = os.environ.get('SKUA_TEST_SCENARIO', 'done')
    if mode == 'error':
        return httpx.Response(401, json={'error': {'message': 'fixture-secret'}})
    text = 'fixture-secret' if mode == 'secret' else 'Done'
    calls = []
    if count == 1 and mode in {'command', 'cancel', 'budget', 'denied'}:
        command = 'printf started; touch started; sleep 30' if mode in {'cancel', 'budget'} else 'printf ok'
        calls = [{'id': 'remote', 'type': 'function', 'function': {'name': 'run_command',
                  'arguments': json.dumps({'command': command})}}]
    return httpx.Response(200, json={'choices': [{'index': 0, 'finish_reason': 'tool_calls' if calls else 'stop',
        'message': {'role': 'assistant', 'content': text, 'tool_calls': calls}}]})
with patch.object(httpx.AsyncClient, '_transport_for_url', lambda self, url: httpx.MockTransport(handler)):
    raise SystemExit(main())
'''


def child_args(workspace, monkeypatch, scenario='done', extra=(), policy='disposable'):
    project = workspace / '.skua'
    project.mkdir(exist_ok=True)
    (project / 'config.toml').write_text('''schema_version = 1
[profiles.default]
base_url = "https://mock.invalid/v1"
model = "mock"
stream = false
[profiles.default.auth]
enabled = false
[shell]
executable = "/bin/bash"
''')
    (workspace / 'task.txt').write_text('Perform the fixture task')
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).parents[1]))
    monkeypatch.setenv('SKUA_TEST_SCENARIO', scenario)
    return [sys.executable, '-c', CHILD, 'run', '--root', str(workspace),
            '--prompt-file', str(workspace / 'task.txt'), '--policy', policy, *extra]


@pytest.mark.parametrize('scenario,policy,status,code', [('done', 'review', 'completed', 0),
    ('command', 'disposable', 'completed', 0), ('denied', 'review', 'approval_required', 3),
    ('error', 'disposable', 'failed', 1), ('budget', 'disposable', 'budget_exhausted', 124)])
def test_closed_stdin_cli_outcomes(workspace, monkeypatch, scenario, policy, status, code):
    args = child_args(workspace, monkeypatch, scenario, policy=policy, extra=['--max-seconds', '0.5'])
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert result.returncode == code, result.stderr
    assert json.loads(result.stdout)['status'] == status
    assert 'fixture-secret' not in result.stdout + result.stderr


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM])
def test_cli_signal_cancels_command_and_writes_artifacts(workspace, monkeypatch, sig):
    args = child_args(workspace, monkeypatch, 'cancel', extra=['--result-file', 'result.json', '--transcript-file', 'history.jsonl'])
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(400):
            if (workspace / 'started').exists() or process.poll() is not None:
                break
            time.sleep(.01)
        assert (workspace / 'started').exists()
        process.send_signal(sig)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 130, stderr
        report = json.loads(stdout)
        assert report['status'] == 'cancelled'
        assert json.loads((workspace / 'result.json').read_text()) == report
        data = (workspace / 'history.jsonl').read_bytes()
        decode(data, workspace)
        assert b'started' in data
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_stdin_and_artifacts_redact_resolved_credential(workspace, monkeypatch):
    args = child_args(workspace, monkeypatch, 'secret', extra=['--prompt-file', '-', '--result-file', 'result.json', '--transcript-file', 'history.jsonl'])
    path = workspace / '.skua/config.toml'
    path.write_text(path.read_text().replace('enabled = false', 'enabled = true'))
    monkeypatch.setenv('SKUA_API_KEY', 'fixture-secret')
    result = subprocess.run(args, input='task with fixture-secret', capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    for value in [result.stdout, result.stderr, (workspace / 'result.json').read_text(), (workspace / 'history.jsonl').read_text()]:
        assert 'fixture-secret' not in value
    assert '[REDACTED]' in (workspace / 'history.jsonl').read_text()
    assert (workspace / 'history.jsonl').stat().st_mode & 0o777 == 0o600


def test_existing_artifact_never_overwritten(workspace, monkeypatch):
    (workspace / 'result.json').write_text('preserve')
    args = child_args(workspace, monkeypatch, extra=['--result-file', 'result.json'])
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and json.loads(result.stdout)['reason'] == 'input_or_artifact_error'
    assert (workspace / 'result.json').read_text() == 'preserve'


@pytest.mark.parametrize('flag,value', [('--max-seconds', 'nan'), ('--max-seconds', 'inf'),
                                      ('--max-seconds', '0'), ('--max-tool-calls', '0')])
def test_cli_rejects_unbounded_limits(workspace, monkeypatch, flag, value):
    args = child_args(workspace, monkeypatch, extra=[flag, value])
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2


def test_empty_closed_stdin_is_actionable_failure(workspace, monkeypatch):
    args = child_args(workspace, monkeypatch, extra=['--prompt-file', '-'])
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and json.loads(result.stdout)['reason'] == 'input_or_artifact_error'


def test_unexpected_provider_exception_closes_serializable_turn(workspace):
    def handler(request):
        raise RuntimeError('private exception detail')
    factory = lambda p, k: ChatClient(p, k, transport=httpx.MockTransport(handler))
    result, session, _ = asyncio.run(execute(config(workspace), 'task', policy='review', client_factory=factory))
    assert result['status'] == 'failed'
    assert session.active_turn is None
    assert b'private exception detail' not in encoded(session, None)
    decode(encoded(session, None), workspace)


def test_cli_recovery_blocks_and_emits_result_artifact(workspace, monkeypatch):
    args = child_args(workspace, monkeypatch, extra=['--result-file', 'result.json'])
    path = workspace / '.skua/.tmp/patch-broken.json'
    path.parent.mkdir()
    path.write_text('unfinished')
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert result.returncode == 3
    outcome = json.loads(result.stdout)
    assert outcome['reason'] == 'recovery_pending'
    assert outcome == json.loads((workspace / 'result.json').read_text())
    assert path.read_text() == 'unfinished'


@pytest.mark.parametrize('credential', ['1', '"\nsecreté'])
def test_transcript_redaction_preserves_json_syntax(workspace, credential):
    from skua.headless import transcript_bytes
    from skua.session import Session, ModelIdentity
    session = Session(workspace, ModelIdentity('default', 'https://mock.invalid/v1', 'mock'))
    session.begin('Task ' + credential)
    session.finish('Answer ' + credential)
    records = [json.loads(line) for line in transcript_bytes(session, credential).splitlines()]
    assert records[0]['schema_version'] == 1
    assert '[REDACTED]' in records[1]['text']


def test_container_policy_allows_external_task_files_but_disposable_does_not(workspace):
    outside = workspace.parent / 'external-task.txt'
    outside.write_text('fixture')
    call = ('read_file', {'path': str(outside)})
    result, _, _ = run(workspace, response(calls=[call]), policy='disposable')
    assert result['status'] == 'approval_required'
    result, session, _ = run(workspace, response(calls=[call,
        ('run_command', {'command': 'printf ok >/dev/null'}),
        ('apply_patch', {'message': 'Update external task file', 'changes': [
            {'op': 'write', 'path': str(outside), 'content': 'updated'}]})]), response('Done'), policy='container')
    assert result['status'] == 'completed'
    assert outside.read_text() == 'updated'
    assert any(e['type'] == 'approval' and 'outside_root' in e['reasons'] and e['decision'] == 'approved'
               for e in session.events)


def test_container_policy_keeps_sensitive_reviews_closed(workspace):
    (workspace / '.env').write_text('private')
    result, _, _ = run(workspace, response(calls=[('read_file', {'path': '.env'})]), policy='container')
    assert result['status'] == 'approval_required'


def test_python_heredoc_division_is_not_an_external_path(workspace):
    import sys
    command = f"{sys.executable} - <<'PY'\nvals = [3, 6]\nprint(sum(vals)/3)\nPY"
    result, session, _ = run(workspace, response(calls=[('run_command', {'command': command})]), response('Done'), policy='container')
    assert result['status'] == 'completed'
    review = next(e for e in session.events if e['type'] == 'approval')
    assert '/3' not in review['targets']


def test_heredoc_path_candidates_preserve_real_paths():
    from skua.tools import command_tokens
    assert '/tmp/task.txt' in command_tokens("python3 - <<'PY'\nprint(open('/tmp/task.txt').read())\nPY")
    assert '.env' in command_tokens("python3 - <<'PY'\nprint(open('.env').read())\nPY")
    assert '.env' in command_tokens('bash <<EOF\ncat .env\nEOF')
    assert '/3' not in command_tokens("python3 - <<'PY'\nprint(sum(vals)/3)\nPY")
    assert '/3' in command_tokens('cat /3')


def test_task_keys_require_explicit_container_opt_in_and_support_followup(workspace):
    root = workspace.resolve()
    keys = root / 'ssl'
    key = keys / 'server.key'
    command = ('run_command', {'command': f"mkdir -p {keys}; printf fixture > {key}"})
    result, _, _ = run(root, response(calls=[command]), policy='container')
    assert result['status'] == 'approval_required'
    assert not key.exists()
    result, session, _ = run(root, response(calls=[command]),
        response(calls=[('read_file', {'path': str(key)})]), response('Done'),
        policy='container', task_key_directories=(keys,))
    assert result['status'] == 'completed' and key.read_text() == 'fixture'
    assert any(e.get('code') == 'task_key_permissions' for e in session.events)


@pytest.mark.parametrize('case', ['existing', 'outside', 'env', 'mixed', 'symlink', 'configured', 'agent'])
def test_task_key_exception_does_not_allow_other_sensitive_access(workspace, case):
    root = workspace.resolve()
    keys = root / 'ssl'
    keys.mkdir()
    key = keys / 'server.key'
    cfg = config(root)
    target = key
    if case == 'existing':
        key.write_text('existing secret')
    elif case == 'outside':
        target = root / 'outside.key'
    elif case == 'env':
        target = keys / '.env'
    elif case == 'symlink':
        real = root / 'real.key'
        real.write_text('existing secret')
        key.symlink_to(real)
    elif case == 'configured':
        cfg = config(root, sensitive={'patterns': ['ssl/*.key']})
    elif case == 'agent':
        target = root / 'installed-agent' / 'server.key'
        cfg = config(root, sensitive={'filenames': ['installed-agent']})
    command = f'printf changed > {target}'
    if case == 'mixed':
        command += f'; cat {root / ".env"}'
    result, _, _ = run(root, response(calls=[('run_command', {'command': command})]),
        cfg=cfg, policy='container', task_key_directories=(keys,))
    assert result['status'] == 'approval_required'
    if case == 'existing':
        assert key.read_text() == 'existing secret'
    elif case == 'symlink':
        assert real.read_text() == 'existing secret'
    else:
        assert not target.exists()


def test_task_keys_can_use_explicit_external_directory(workspace):
    root = workspace.resolve()
    keys = root.parent / 'external-ssl'
    key = keys / 'localhost.key'
    command = ('run_command', {'command': f'mkdir -p {keys}; printf fixture > {key}'})
    result, _, _ = run(root, response(calls=[command]), response('Done'),
        policy='container', task_key_directories=(keys,))
    assert result['status'] == 'completed' and key.read_text() == 'fixture'
    with pytest.raises(ValueError, match='container policy'):
        run(root, response('Done'), policy='disposable', task_key_directories=(keys,))


@pytest.mark.parametrize('field', ['timeout', 'timeout_seconds'])
def test_requested_command_timeout_does_not_extend_task_deadline(workspace, field):
    result, session, _ = run(workspace,
        response(calls=[('run_command', {'command': 'printf started; sleep 10', field: 1000})]),
        max_seconds=.3)
    assert result['status'] == 'budget_exhausted'
    assert result['reason'] == 'time_limit'
    assert result['tool_calls'] == 1
