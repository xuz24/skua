"""Unattended environment hints never run discovery commands or reset budgets."""
import json
import os
import shlex
import sys

from skua.run_context import RunContext, environment_snapshot
from test_headless import config, response, run


def unpack(request):
    messages = [m['content'] for m in request['messages'] if m['role'] == 'system'
                and m['content'].startswith('Unattended runtime context')]
    assert len(messages) == 1
    return json.loads(messages[0].splitlines()[1])


def test_environment_discovery_only_inspects_path_and_marks_uncertainty(workspace, monkeypatch):
    binary = workspace / 'bin'
    binary.mkdir()
    for name in ['python3', 'apt-get', 'gcc']:
        path = binary / name
        path.write_text('#!/bin/sh\ntouch must-not-run\n')
        path.chmod(0o755)
    monkeypatch.setenv('PATH', 'bin')
    monkeypatch.setenv('SKUA_API_KEY', 'private-credential')
    monkeypatch.setenv('UNRELATED_SECRET', 'other-secret')
    cfg = config(workspace, shell={'executable': '/bin/bash', 'startup': {'enabled': True}},
                 environment={'kind': 'venv', 'path': str(workspace / '.venv')})
    snapshot = environment_snapshot(cfg)
    assert snapshot['interpreters_on_path']['python3'] == str(binary / 'python3')
    assert snapshot['interpreters_on_path']['python'] is None
    assert snapshot['package_managers_on_path']['apt-get'] == str(binary / 'apt-get')
    assert snapshot['build_tools_on_path']['make'] is None
    assert snapshot['agent_python'] == sys.executable
    assert snapshot['configured_environment']['kind'] == 'venv'
    assert snapshot['shell_startup_enabled'] is True
    message = RunContext(cfg, 100, 100, 4).message(0)['content']
    assert 'before shell startup or activation' in message
    assert 'No versions, modules, or permissions have been tested' in message
    assert 'private harness runtime' in message
    assert 'private-credential' not in message and 'other-secret' not in message
    assert not (workspace / 'must-not-run').exists()


def test_budget_reports_effective_ceiling_and_never_extends_deadline(workspace, monkeypatch):
    now = [10.0]
    monkeypatch.setattr('skua.run_context.time.monotonic', lambda: now[0])
    context = RunContext(config(workspace, commands={'timeout_seconds': 1}), 12, 2, 3)
    assert context.budget(1)['effective_command_ceiling_seconds'] == 1
    assert context.budget(1)['remaining_tool_calls'] == 2
    now[0] = 11.75
    assert context.budget(2)['remaining_seconds'] == .25
    assert context.budget(2)['effective_command_ceiling_seconds'] == .25
    now[0] = 13
    assert context.budget(4)['remaining_seconds'] == 0
    assert context.budget(4)['remaining_tool_calls'] == 0
    assert context.deadline == 12


def test_missing_prerequisite_setup_and_verification_share_live_budget(workspace, monkeypatch):
    binary = workspace / 'task-bin'
    binary.mkdir()
    python = binary / 'python3'
    python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
    python.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary) + os.pathsep + '/usr/bin:/bin')
    # A local module is the offline stand-in for installing a task prerequisite.
    check = "python3 -c 'import skua_fixture_prerequisite; print(skua_fixture_prerequisite.answer)'"
    install = "printf 'answer = 42\\n' > skua_fixture_prerequisite.py"
    result, session, requests = run(workspace,
        response(calls=[('run_command', {'command': check})]),
        response(calls=[('run_command', {'command': install})]),
        response(calls=[('run_command', {'command': check})]), response('Verified 42'),
        max_seconds=10, max_tool_calls=3)
    assert result['status'] == 'completed' and result['task_success_verified'] is False
    snapshots = [unpack(request) for request in requests]
    assert all(s['environment']['interpreters_on_path']['python3'] == str(python) for s in snapshots)
    assert snapshots[0]['environment']['agent_python'] != str(python)
    assert [s['budget']['remaining_tool_calls'] for s in snapshots] == [3, 2, 1, 0]
    times = [s['budget']['remaining_seconds'] for s in snapshots]
    assert 10 >= times[0] > times[-1] > 0
    assert times == sorted(times, reverse=True)
    results = [e for e in session.events if e['type'] == 'tool_result']
    assert [r['status'] for r in results] == ['error', 'ok', 'ok']
    assert 'ModuleNotFoundError' in results[0]['model_text']
    assert '42' in results[-1]['model_text']
    for entry in results:
        dispatch = entry['data']['task_budget_at_dispatch']
        assert dispatch['effective_command_ceiling_seconds'] <= dispatch['remaining_seconds'] <= 10
        assert entry['data']['task_budget']['remaining_seconds'] <= dispatch['remaining_seconds']
    assert len([e for e in session.events if e['type'] == 'notice' and e['code'] == 'runtime_context']) == 4
    assert len([e for e in session.events if e['type'] == 'notice' and e['code'] == 'command_budget']) == 3


def test_path_discovery_refreshes_after_task_environment_changes(workspace, monkeypatch):
    binary = workspace / 'bin'
    binary.mkdir()
    monkeypatch.setenv('PATH', str(binary) + ':/usr/bin:/bin')
    cfg = config(workspace)
    # Put a newly available interpreter first on PATH; discovery must refresh.
    command = "printf '#!/bin/sh\\nexit 0\\n' > bin/R; chmod +x bin/R"
    _, _, requests = run(workspace, response(calls=[('run_command', {'command': command})]), response('done'), cfg=cfg)
    assert unpack(requests[-1])['environment']['interpreters_on_path']['R'] == str(binary / 'R')
    assert unpack(requests[0])['environment']['interpreters_on_path']['R'] != str(binary / 'R')
