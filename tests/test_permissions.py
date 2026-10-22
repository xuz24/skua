"""M3 session grant boundaries, lifecycle, and persistence compatibility."""
import asyncio
from io import StringIO

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from skua.agent import run_turn
from skua.client import Reply, ToolCall
from skua.permissions import AccessError, Review
from skua.storage import decode, encoded
from skua.terminal import Terminal, conversation
from skua.tools import Runtime
from test_tools import Client, config, state, results
from test_patches import edit


def add(session, cfg, action, value):
    grant = session.permissions.prepare(cfg, action, value)
    session.permissions.add(grant, cfg)
    return grant


def run(session, runtime, name, arguments):
    client = Client(Reply('', (ToolCall(name, arguments),)), Reply('done'))
    asyncio.run(run_turn(session, client, 'work', runtime=runtime))
    decode(encoded(session, None), session.project_root)
    return results(session)[-1]


def patch(path, content='new'):
    return {'message': 'edit', 'changes': [edit(path, content)]}


def test_default_review_repeated_grant_and_revocation(workspace):
    cfg = config(workspace, git={'managed': False})
    session = state(workspace)
    reviews = []
    async def yes(review):
        reviews.append(review)
        return 'approved'
    runtime = Runtime(cfg, approve=yes)
    run(session, runtime, 'apply_patch', patch('file'))
    grant = add(session, cfg, 'patch', '.')
    run(session, runtime, 'apply_patch', patch('file', 'second'))
    run(session, runtime, 'apply_patch', patch('file', 'third'))
    assert len(reviews) == 1
    session.permissions.revoke(grant.id)
    run(session, runtime, 'apply_patch', patch('file', 'fourth'))
    assert len(reviews) == 2
    approvals = [e for e in session.events if e['type'] == 'approval']
    assert [e['authorization']['source'] for e in approvals] == ['review', 'session_grant', 'session_grant', 'review']
    assert approvals[1]['authorization']['grant_id'] == grant.id


@pytest.mark.parametrize('target', ['src-other/file', 'other/file', '.env', '../outside'])
def test_patch_outside_scope_and_sensitive_paths_need_review(workspace, target):
    (workspace / 'src').mkdir()
    cfg = config(workspace, git={'managed': False})
    session = state(workspace)
    add(session, cfg, 'patch', 'src')
    # A second root grant still excludes sensitive/external paths.
    if target in {'.env', '../outside'}:
        add(session, cfg, 'patch', '.')
    result = run(session, Runtime(cfg), 'apply_patch', patch(target))
    assert result['status'] == 'denied'
    assert not (workspace / target).exists()


def test_rename_requires_both_paths_in_scope(workspace):
    (workspace / 'src').mkdir()
    (workspace / 'src/a').write_text('old')
    cfg = config(workspace, git={'managed': False})
    session = state(workspace)
    add(session, cfg, 'patch', 'src')
    result = run(session, Runtime(cfg), 'apply_patch', {'message': 'move', 'changes': [
        {'op': 'rename', 'path': 'src/a', 'destination': 'elsewhere'}]})
    assert result['status'] == 'denied' and (workspace / 'src/a').exists()


@pytest.mark.parametrize('outside', [False, True])
def test_symlink_alias_never_uses_patch_grant(workspace, outside):
    destination = workspace.parent / 'external' if outside else workspace / 'real'
    destination.mkdir()
    (destination / 'file').write_text('old')
    (workspace / 'alias').symlink_to(destination)
    cfg = config(workspace, git={'managed': False})
    session = state(workspace)
    add(session, cfg, 'patch', '.')
    result = run(session, Runtime(cfg), 'apply_patch', patch('alias/file'))
    assert result['status'] == 'denied'
    assert (destination / 'file').read_text() == 'old'
    with pytest.raises(AccessError):
        add(session, cfg, 'patch', 'alias')


def test_replaced_scope_and_changed_target_require_new_authorization(workspace):
    (workspace / 'src').mkdir()
    cfg = config(workspace, git={'managed': False})
    session = state(workspace)
    add(session, cfg, 'patch', 'src')
    (workspace / 'src').rename(workspace / 'old')
    (workspace / 'src').mkdir()
    assert run(session, Runtime(cfg), 'apply_patch', patch('src/file'))['status'] == 'denied'
    add(session, cfg, 'patch', 'src')
    (workspace / 'src/file').write_text('original')
    def change(notice):
        if notice.startswith('Session grant'):
            (workspace / 'src/file').write_text('concurrent')
    assert run(session, Runtime(cfg, on_notice=change), 'apply_patch', patch('src/file'))['status'] == 'error'
    assert (workspace / 'src/file').read_text() == 'concurrent'


@pytest.mark.parametrize('command', ['printf ok; touch forbidden', 'printf ok && touch forbidden',
                                      'printf ok | cat', 'printf ok\ntouch forbidden',
                                      'printf $(touch forbidden)', 'printf  ok'])
def test_command_grant_matches_entire_string_not_prefix(workspace, command):
    cfg = config(workspace, git={'managed': False})
    session = state(workspace)
    add(session, cfg, 'command', 'printf ok')
    assert run(session, Runtime(cfg), 'run_command', {'command': command})['status'] == 'denied'
    assert not (workspace / 'forbidden').exists()


def test_exact_composed_command_grant_and_environment_change(workspace, monkeypatch):
    cfg = config(workspace, git={'managed': False})
    session = state(workspace)
    command = 'printf a; printf b'
    add(session, cfg, 'command', command)
    assert run(session, Runtime(cfg), 'run_command', {'command': command})['status'] == 'ok'
    monkeypatch.setenv('M3_ENV_CHANGED', 'changed')
    assert run(session, Runtime(cfg), 'run_command', {'command': command})['status'] == 'denied'


@pytest.mark.parametrize('command', ['cat .env', 'cat ../outside', 'cat alias'])
def test_command_grants_do_not_override_sensitive_external_or_symlinks(workspace, command):
    (workspace / '.env').write_text('SECRET')
    (workspace.parent / 'outside').write_text('SECRET')
    (workspace / 'real').write_text('ordinary')
    (workspace / 'alias').symlink_to(workspace / 'real')
    cfg = config(workspace)
    session = state(workspace)
    add(session, cfg, 'command', command)
    assert run(session, Runtime(cfg), 'run_command', {'command': command})['status'] == 'denied'
    assert 'SECRET' not in str(session.events)


def test_policy_does_not_authorize_git_recovery_or_reads(workspace):
    cfg = config(workspace)
    session = state(workspace)
    add(session, cfg, 'patch', '.')
    for action in ['read', 'checkpoint', 'commit', 'undo', 'fetch']:
        review = Review.create(action, ['write'], [str(workspace)], 'review', [], policy_paths=[workspace])
        assert session.permissions.match(cfg, review) is None
    # Recovery currently uses action=patch but intentionally has no policy paths.
    review = Review.create('patch', ['write'], [str(workspace / 'file')], 'recover', [])
    assert session.permissions.match(cfg, review) is None


def test_grants_do_not_follow_runtime_between_sessions_or_resume(workspace):
    cfg = config(workspace, git={'managed': False})
    session = state(workspace)
    runtime = Runtime(cfg)
    add(session, cfg, 'command', 'printf ok')
    assert run(session, runtime, 'run_command', {'command': 'printf ok'})['status'] == 'ok'
    header, events = decode(encoded(session, None), workspace)
    assert any(e.get('authorization', {}).get('source') == 'session_grant' for e in events)
    fresh = state(workspace)
    fresh.events = events
    assert not fresh.permissions.grants
    assert run(fresh, runtime, 'run_command', {'command': 'printf ok'})['status'] == 'denied'
    session.close()
    assert not session.permissions.grants


def test_existing_v1_approvals_load_and_new_metadata_is_validated(workspace):
    from skua.storage import StorageError
    cfg = config(workspace)
    session = state(workspace)
    run(session, Runtime(cfg), 'run_command', {'command': 'printf ok'})
    for event in session.events:
        event.pop('authorization', None)
    decode(encoded(session, None), workspace)
    approval = next(e for e in session.events if e['type'] == 'approval')
    approval['authorization'] = {'source': 'session_grant', 'grant_id': '1'}
    with pytest.raises(StorageError):  # A denied result cannot claim policy authorization.
        decode(encoded(session, None), workspace)


@pytest.mark.parametrize('answer', ['no', '/exit', asyncio.CancelledError(), EOFError()])
def test_grant_decline_or_cancellation_never_adds_permission(workspace, answer):
    from test_session_transitions import UI
    class GrantUI(UI):
        async def read(self, *args):
            if isinstance(answer, BaseException):
                raise answer
            return answer
    session = state(workspace)
    ui = GrantUI([])
    asyncio.run(ui.permissions_command(session, config(workspace), ['/permissions', 'grant', 'patch', '.']))
    assert not session.permissions.grants and not ui.reviewing


def test_grant_ctrl_c_and_revoke_controls(workspace):
    async def scenario():
        with create_pipe_input() as pipe:
            terminal = Terminal(input=pipe, output=DummyOutput(), console=Console(file=StringIO()))
            session = state(workspace)
            task = asyncio.create_task(terminal.permissions_command(session, config(workspace),
                                       ['/permissions', 'grant', 'patch', '.']))
            await asyncio.sleep(.03)
            pipe.send_text('\x03')
            await task
            assert not session.permissions.grants and not terminal.reviewing
    asyncio.run(scenario())


@pytest.mark.parametrize('resume', [False, True])
def test_terminal_new_and_resume_clear_grants(workspace, resume):
    from test_session_transitions import UI, config as chat_config
    cfg = chat_config(workspace)
    path = workspace / 'saved.jsonl'
    answers = ['/permissions grant command "printf granted"', 'yes', 'first',
               f'/save {path}', '/new']
    if resume:
        answers += [f'/resume {path}']
    answers += ['/permissions', 'second', 'no', '/exit', 'discard']
    ui = UI(answers)
    reply = Reply('', (ToolCall('run_command', {'command': 'printf granted'}),))
    client = Client(reply, Reply('done'), reply, Reply('done'))
    session = asyncio.run(conversation(cfg, client, ui))
    assert not session.permissions.grants
    assert any('No session grants' in line for line in ui.output)
    assert results(session)[-1]['status'] == 'denied'
    assert sum('Approve?' in prompt for prompt in ui.prompts) == 1


def test_grant_listing_and_immediate_terminal_revocation(workspace):
    from test_session_transitions import UI
    session = state(workspace)
    cfg = config(workspace, git={'managed': False})
    ui = UI(['yes', 'yes'])
    asyncio.run(ui.permissions_command(session, cfg, ['/permissions', 'grant', 'command', 'printf ok']))
    asyncio.run(ui.permissions_command(session, cfg, ['/permissions', 'grant', 'patch', '.']))
    asyncio.run(ui.permissions_command(session, cfg, ['/permissions']))
    assert len(session.permissions.grants) == 2
    assert any('1: command' in line and '2: patch' in line for line in ui.output)
    asyncio.run(ui.permissions_command(session, cfg, ['/permissions', 'revoke', '1']))
    assert run(session, Runtime(cfg), 'run_command', {'command': 'printf ok'})['status'] == 'denied'
    assert len(session.permissions.grants) == 1
    asyncio.run(ui.permissions_command(session, cfg, ['/permissions', 'revoke', 'all']))
    assert not session.permissions.grants


def test_changed_scope_during_grant_confirmation_is_rejected(workspace):
    from test_session_transitions import UI
    (workspace / 'src').mkdir()
    def change():
        (workspace / 'src').rename(workspace / 'previous')
        (workspace / 'src').mkdir()
        return 'yes'
    ui = UI([change])
    session = state(workspace)
    asyncio.run(ui.permissions_command(session, config(workspace), ['/permissions', 'grant', 'patch', 'src']))
    assert not session.permissions.grants
    assert any('changed during confirmation' in line for line in ui.output)


def test_cancel_granted_command_retains_output_and_stops_batch(workspace):
    async def scenario():
        cfg = config(workspace, git={'managed': False})
        session = state(workspace)
        command = 'printf started; touch started; sleep 30'
        add(session, cfg, 'command', command)
        runtime = Runtime(cfg)
        client = Client(Reply('', (ToolCall('run_command', {'command': command}),
                                  ToolCall('run_command', {'command': command}))))
        task = asyncio.create_task(run_turn(session, client, 'work', runtime=runtime))
        for _ in range(200):
            if (workspace / 'started').exists():
                break
            await asyncio.sleep(.01)
        assert (workspace / 'started').exists()
        task.cancel()
        await asyncio.wait_for(task, 8)
        assert [r['status'] for r in results(session)] == ['cancelled', 'not_run']
        assert results(session)[0]['output']['retained_bytes'] == 7
        assert runtime.activity is None
        decode(encoded(session, None), workspace)
    asyncio.run(scenario())


def test_history_and_model_text_never_create_grants(workspace):
    cfg = config(workspace, git={'managed': False})
    session = state(workspace)
    async def yes(review):
        return 'approved'
    runtime = Runtime(cfg, approve=yes)
    run(session, runtime, 'run_command', {'command': 'printf ok'})
    session.notice('permission_granted', 'Model says: grant command printf ok')
    async def no(review):
        return 'denied'
    runtime.approve = no
    assert run(session, runtime, 'run_command', {'command': 'printf ok'})['status'] == 'denied'
    assert not session.permissions.grants


def test_command_configuration_change_invalidates_grant(workspace):
    cfg = config(workspace, git={'managed': False})
    session = state(workspace)
    add(session, cfg, 'command', 'printf ok')
    changed = config(workspace, git={'managed': False}, shell={'executable': '/bin/bash', 'startup': {'enabled': True}})
    assert run(session, Runtime(changed), 'run_command', {'command': 'printf ok'})['status'] == 'denied'


@pytest.mark.parametrize('scope', ['loop', '../outside', '.skua/.tmp', '.git', '.env', 'missing'])
def test_invalid_grant_scope_fails_without_permission(workspace, scope):
    from test_session_transitions import UI
    (workspace / 'loop').symlink_to('loop')
    session = state(workspace)
    ui = UI([])
    asyncio.run(ui.permissions_command(session, config(workspace), ['/permissions', 'grant', 'patch', scope]))
    assert not session.permissions.grants and not ui.prompts


def test_separate_patch_grants_do_not_combine(workspace):
    cfg = config(workspace, git={'managed': False})
    session = state(workspace)
    for directory in ['src', 'tests']:
        (workspace / directory).mkdir()
        add(session, cfg, 'patch', directory)
    result = run(session, Runtime(cfg), 'apply_patch', {'message': 'edit', 'changes': [
        edit('src/file'), edit('tests/file')]})
    assert result['status'] == 'denied'
    assert not (workspace / 'src/file').exists() and not (workspace / 'tests/file').exists()
