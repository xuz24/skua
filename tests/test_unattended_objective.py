"""Scripted model fixtures exercise real tools; they do not measure model compliance."""
import json
import os
from pathlib import Path
import sys

import httpx
import pytest

from skua.run_context import UNATTENDED_OBJECTIVE
from test_headless import response, run


def command(text):
    return response(calls=[('run_command', {'command': text})])


@pytest.mark.parametrize('execute_setup', [False, True])
def test_setup_deliverable_is_distinct_from_configured_behavior(workspace, execute_setup):
    # Offline service stand-in: its response depends on installed configuration.
    (workspace / 'service.sh').write_text('cat service.conf\n')
    patch = response(calls=[('apply_patch', {'message': 'Prepare setup', 'changes': [
        {'op': 'write', 'path': 'setup.sh', 'content': "printf 'ready\\n' > service.conf\n"}]})])
    replies = [patch]
    if execute_setup:
        replies += [command('bash setup.sh'), command('test "$(bash service.sh)" = ready')]
    replies += [response('Configured and checked.' if execute_setup else 'Run bash setup.sh.')]
    result, session, requests = run(workspace, *replies, max_tool_calls=3,
        prompt='Configure the fixture service to respond ready and check its behavior.')
    assert (workspace / 'setup.sh').exists()
    assert (workspace / 'service.conf').exists() is execute_setup
    if execute_setup:
        assert (workspace / 'service.conf').read_text() == 'ready\n'
        assert [e['status'] for e in session.events if e['type'] == 'tool_result'] == ['ok'] * 3
    # Neither successful tool execution nor confident prose proves external success.
    assert result['task_success_verified'] is False
    for request in requests:
        assert any(UNATTENDED_OBJECTIVE in m['content'] for m in request['messages'] if m['role'] == 'system')


@pytest.mark.parametrize('repair', [False, True])
def test_answer_write_does_not_validate_failed_calculation(workspace, repair, monkeypatch):
    monkeypatch.setenv('PATH', str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', ''))
    python = 'python3'
    replies = [command(f'{python} -c "print(6 *)"'),
               command("printf '41\\n' > answer.txt")]
    if repair:
        replies += [command(f'{python} -c "from pathlib import Path; assert int(Path(\'answer.txt\').read_text()) == 6 * 7"'),
                    command(f'{python} -c "from pathlib import Path; value = 6 * 7; print(value); Path(\'answer.txt\').write_text(str(value))"'),
                    command(f'{python} -c "from pathlib import Path; assert int(Path(\'answer.txt\').read_text()) == sum([6] * 7); print(\'verified 42\')"')]
    replies += [response('Answer saved.')]
    result, session, _ = run(workspace, *replies, max_tool_calls=5,
        prompt='Compute six times seven, save answer.txt, and validate the saved result.')
    results = [e for e in session.events if e['type'] == 'tool_result']
    assert 'SyntaxError' in results[0]['model_text']
    assert results[1]['status'] == 'ok'
    assert int((workspace / 'answer.txt').read_text()) == (42 if repair else 41)
    if repair:
        assert 'AssertionError' in results[2]['model_text']
        assert 'verified 42' in results[-1]['model_text']
    assert result['task_success_verified'] is False


@pytest.mark.parametrize('explicit', [False, True])
def test_refusal_diagnostic_uses_provider_evidence_without_retry(workspace, explicit):
    body = {'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
        'role': 'assistant', 'content': None if explicit else 'I cannot help with that.',
        'refusal': 'I cannot help with that.' if explicit else None}}]}
    result, session, requests = run(workspace, httpx.Response(200, json=body))
    assert len(requests) == 1 and result['tool_calls'] == 0
    assert result['status'] == 'completed' and result['reason'] == 'turn_completed'
    assert result['diagnostics'] == (['model_refusal'] if explicit else [])
    assert result['task_success_verified'] is False
    assert not any(e['type'] == 'notice' and e['code'] == 'tool_correction' for e in session.events)
    assert 'I cannot help' not in json.dumps(result)
