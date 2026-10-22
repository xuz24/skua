import asyncio
import os
from pathlib import Path
import subprocess

import pytest

from skua.agent import run_turn
from skua.client import Reply, ToolCall
from skua.config import build_config
from skua import patches
from skua.git import Repo
from skua.permissions import AccessError
from skua.session import ModelIdentity, Session
from skua.storage import encoded, decode
from skua.tools import Runtime


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True).stdout.decode().strip()


def init(root):
    git(root, 'init')
    git(root, 'config', 'user.name', 'Test User')
    git(root, 'config', 'user.email', 'test@example.invalid')
    (root / '.gitignore').write_text('.skua/\nignored/\n')
    (root / 'a.txt').write_text('original\n')
    (root / 'b.txt').write_text('other\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'initial user commit')
    return Repo.find(root)


def config(root, **tables):
    return build_config(root, [('test', {'schema_version': 1, 'shell': {'executable': '/bin/bash'}, **tables})])


def state(root):
    return Session(root, ModelIdentity('default', 'https://example/v1', 'm'))


def edit(path='a.txt', text='changed\n'):
    return {'op': 'write', 'path': path, 'content': text}


async def yes(review):
    return 'approved'


def turn(root, runtime, *changes):
    session = state(root)
    replies = iter([Reply('', (ToolCall('apply_patch', {'message': 'Reviewed change', 'changes': list(changes)}),)), Reply('done')])
    class Client:
        async def complete(self, *args, **kwargs):
            return next(replies)
    asyncio.run(run_turn(session, Client(), 'change files', runtime=runtime))
    decode(encoded(session, None), root)
    result = next(e for e in session.events if e['type'] == 'tool_result')
    return result, session


def test_patch_commit_preserves_unrelated_staged_and_unstaged(workspace):
    repo = init(workspace)
    (workspace / 'b.txt').write_text('staged\n')
    git(workspace, 'add', 'b.txt')
    (workspace / 'b.txt').write_text('unstaged on staged\n')
    before_index = git(workspace, 'show', ':b.txt')
    result, _ = turn(workspace, Runtime(config(workspace), approve=yes), edit())
    assert result['status'] == 'ok', result
    assert (workspace / 'a.txt').read_text() == 'changed\n'
    assert git(workspace, 'show', 'HEAD:a.txt') == 'changed'
    assert git(workspace, 'show', 'HEAD:b.txt') == 'other'
    assert git(workspace, 'show', ':b.txt') == before_index
    assert (workspace / 'b.txt').read_text() == 'unstaged on staged\n'
    assert repo.metadata(repo.head())['type'] == 'change'


def test_checkpoint_then_patch_separate_commits(workspace):
    repo = init(workspace)
    (workspace / 'a.txt').write_text('user edit\n')
    reviews = []
    async def approve(review):
        reviews.append(review)
        return 'approved'
    result, _ = turn(workspace, Runtime(config(workspace), approve=approve), edit())
    assert result['status'] == 'ok', result
    parent = git(workspace, 'rev-parse', 'HEAD^')
    assert repo.metadata(parent)['type'] == 'checkpoint'
    assert git(workspace, 'show', 'HEAD^:a.txt') == 'user edit'
    assert [r.action for r in reviews] == ['checkpoint', 'patch', 'commit']
    assert 'Exact commit message:' in reviews[-1].summary
    assert 'user edit' in reviews[0].summary
    assert git(workspace, 'show', '-s', '--format=%B', 'HEAD^') in reviews[0].summary
    assert git(workspace, 'show', '-s', '--format=%B', 'HEAD') in reviews[-1].summary


def test_checkpoint_decline_stops_entire_patch(workspace):
    init(workspace)
    (workspace / 'a.txt').write_text('user edit\n')
    before_index = (workspace / '.git/index').read_bytes()
    result, _ = turn(workspace, Runtime(config(workspace)), edit(), edit('new.txt', 'new'))
    assert result['status'] == 'denied'
    assert (workspace / 'a.txt').read_text() == 'user edit\n'
    assert not (workspace / 'new.txt').exists()
    assert (workspace / '.git/index').read_bytes() == before_index


def test_non_git_init_decline_still_allows_patch_once(workspace):
    reviews = []
    async def approve(review):
        reviews.append(review)
        return 'denied' if review.action == 'checkpoint' else 'approved'
    runtime = Runtime(config(workspace), approve=approve)
    result, _ = turn(workspace, runtime, edit('nested/new.txt', 'created'))
    assert result['status'] == 'ok', result
    result, _ = turn(workspace, runtime, edit('nested/new.txt', 'updated'))
    assert result['status'] == 'ok'
    assert not (workspace / '.git').exists()
    assert len([r for r in reviews if r.action == 'checkpoint']) == 1
    assert (workspace / 'nested/new.txt').read_text() == 'updated'


def test_patch_denial_no_target_changes(workspace):
    repo = init(workspace)
    before = repo.identity()
    result, _ = turn(workspace, Runtime(config(workspace)), edit())
    assert result['status'] == 'denied'
    assert repo.identity() == before
    assert (workspace / 'a.txt').read_text() == 'original\n'


def test_stale_patch_rejected(workspace):
    init(workspace)
    async def changed(review):
        if review.action == 'patch':
            (workspace / 'a.txt').write_text('concurrent\n')
        return 'approved'
    result, _ = turn(workspace, Runtime(config(workspace), approve=changed), edit())
    assert result['status'] == 'error'
    assert (workspace / 'a.txt').read_text() == 'concurrent\n'


def test_create_delete_rename_and_replace_destination(workspace):
    init(workspace)
    result, _ = turn(workspace, Runtime(config(workspace), approve=yes),
                     {'op': 'rename', 'path': 'a.txt', 'destination': 'b.txt', 'content': 'renamed\n'},
                     edit('new.txt', 'new\n'))
    assert result['status'] == 'ok', result
    assert not (workspace / 'a.txt').exists()
    assert (workspace / 'b.txt').read_text() == 'renamed\n'
    result, _ = turn(workspace, Runtime(config(workspace), approve=yes), {'op': 'delete', 'path': 'new.txt'})
    assert result['status'] == 'ok', result
    assert not (workspace / 'new.txt').exists()


def test_snapshot_failure_prevents_mutation(workspace, monkeypatch):
    (workspace / 'a.txt').write_text('old')
    patch = patches.prepare(workspace, [edit()])
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(patches, 'save_snapshot', fail)
    with pytest.raises(OSError):
        patches.apply(workspace, patch)
    assert (workspace / 'a.txt').read_text() == 'old'


def test_partial_write_restores_all_operations(workspace, monkeypatch):
    (workspace / 'a.txt').write_text('old')
    (workspace / 'b.txt').write_text('other')
    (workspace / 'a.txt').chmod(0o755)
    patch = patches.prepare(workspace, [{'op': 'rename', 'path': 'a.txt', 'destination': 'new/n.txt'},
                                        {'op': 'delete', 'path': 'b.txt'}, edit('last', 'last')])
    write = patches.write_file
    def fail_once(path, *args, **kwargs):
        if path.name == 'last':
            raise OSError('injected')
        return write(path, *args, **kwargs)
    monkeypatch.setattr(patches, 'write_file', fail_once)
    ok, unresolved, snapshot = patches.apply(workspace, patch)
    assert not ok and not unresolved and snapshot is None
    assert (workspace / 'a.txt').read_text() == 'old'
    assert (workspace / 'a.txt').stat().st_mode & 0o777 == 0o755
    assert (workspace / 'b.txt').read_text() == 'other'
    assert not (workspace / 'new').exists()


def test_crash_recovery_preserves_newer_work_and_retains_snapshot(workspace):
    (workspace / 'a.txt').write_text('old')
    patch = patches.prepare(workspace, [edit()])
    path = patches.save_snapshot(workspace, patch)
    (workspace / 'a.txt').write_text('newer user content')
    assert patches.recover(workspace, path) == [str(workspace / 'a.txt')]
    assert path.exists()
    (workspace / 'a.txt').write_text('changed\n')
    assert patches.recover(workspace, path) == []
    assert not path.exists()
    assert (workspace / 'a.txt').read_text() == 'old'


def test_launch_recovery_requires_approval(workspace):
    (workspace / 'a.txt').write_text('old')
    patch = patches.prepare(workspace, [edit()])
    path = patches.save_snapshot(workspace, patch)
    (workspace / 'a.txt').write_text('changed\n')
    session = state(workspace)
    asyncio.run(Runtime(config(workspace)).workflows.recovery(session))
    assert path.exists() and (workspace / 'a.txt').read_text() == 'changed\n'
    asyncio.run(Runtime(config(workspace), approve=yes).workflows.recovery(session))
    assert not path.exists() and (workspace / 'a.txt').read_text() == 'old'
    decode(encoded(session, None), workspace)


def hook(root, text):
    path = root / '.git/hooks/pre-commit'
    path.write_text('#!/bin/sh\n' + text)
    path.chmod(0o755)


def test_failed_patch_hook_keeps_edits_and_does_not_retry(workspace):
    repo = init(workspace)
    before = repo.head()
    hook(workspace, 'printf hook-change > a.txt\nexit 1\n')
    result, _ = turn(workspace, Runtime(config(workspace), approve=yes), edit())
    assert result['status'] == 'error', result
    assert repo.head() == before
    assert (workspace / 'a.txt').read_text() == 'hook-change'
    assert 'Commit failed' in result['model_text']


def test_failed_checkpoint_hook_stops_patch(workspace):
    repo = init(workspace)
    before = repo.head()
    (workspace / 'a.txt').write_text('user work')
    hook(workspace, 'exit 1\n')
    result, _ = turn(workspace, Runtime(config(workspace), approve=yes), edit())
    assert result['status'] == 'error'
    assert repo.head() == before
    assert (workspace / 'a.txt').read_text() == 'user work'


def test_successful_hook_staging_is_included_in_final_commit_review(workspace):
    init(workspace)
    hook(workspace, 'printf hook-change > a.txt\ngit add a.txt\n')
    reviews = []
    async def approve(review):
        reviews.append(review)
        return 'approved'
    result, _ = turn(workspace, Runtime(config(workspace), approve=approve), edit())
    assert result['status'] == 'ok', result
    assert git(workspace, 'show', 'HEAD:a.txt') == 'hook-change'
    assert [r.action for r in reviews] == ['patch', 'commit']
    assert 'Exact commit message:' in reviews[-1].summary
    assert result['data']['hooks'] == ['pre-commit']


def test_lint_corrections_form_one_commit(workspace):
    repo = init(workspace)
    before = repo.head()
    cfg = config(workspace, lint={'enabled': True, 'command': "test \"$(cat a.txt)\" = fixed"})
    runtime = Runtime(cfg, approve=yes)
    result, _ = turn(workspace, runtime, edit())
    assert result['error']['code'] == 'lint_failed', result
    assert repo.head() == before
    result, _ = turn(workspace, runtime, edit(text='fixed\n'))
    assert result['status'] == 'ok', result
    assert git(workspace, 'rev-parse', 'HEAD^') == before
    assert runtime.workflows.pending is None


def test_lint_exhaustion_keeps_edits_uncommitted(workspace):
    repo = init(workspace)
    before = repo.head()
    runtime = Runtime(config(workspace, lint={'enabled': True, 'command': 'false'}), approve=yes)
    for i in range(4):
        result, _ = turn(workspace, runtime, edit(text=f'correction {i}'))
        assert result['error']['code'] == ('lint_exhausted' if i == 3 else 'lint_failed')
    assert runtime.workflows.pending is None
    assert repo.head() == before
    assert (workspace / 'a.txt').read_text() == 'correction 3'
    assert 'exhausted' in result['model_text']


def test_external_sensitive_patch_denied_before_read(workspace, monkeypatch):
    outside = workspace.parent / '.env'
    outside.write_text('SECRET')
    original = patches.Target.read
    def guarded(self, *args):
        assert self.path != outside
        return original(self, *args)
    monkeypatch.setattr(patches.Target, 'read', guarded)
    result, session = turn(workspace, Runtime(config(workspace)), edit(str(outside), 'new'))
    assert result['status'] == 'denied'
    assert 'SECRET' not in str(session.events)


def test_binary_and_ignored_targets_rejected(workspace):
    init(workspace)
    (workspace / 'binary').write_bytes(b'a\0b')
    result, _ = turn(workspace, Runtime(config(workspace), approve=yes), edit('binary'))
    assert result['status'] == 'error'
    result, _ = turn(workspace, Runtime(config(workspace), approve=yes), edit('ignored/new'))
    assert result['status'] == 'error'
    assert not (workspace / 'ignored/new').exists()


def test_local_undo_and_checkpoint_boundary(workspace):
    repo = init(workspace)
    original = repo.head()
    runtime = Runtime(config(workspace), approve=yes)
    result, _ = turn(workspace, runtime, edit())
    assert result['status'] == 'ok'
    result = asyncio.run(runtime.workflows.undo(state(workspace), 'local'))
    assert result.status == 'ok', result
    assert repo.head() == original
    assert (workspace / 'a.txt').read_text() == 'original\n'
    assert asyncio.run(runtime.workflows.undo(state(workspace), 'local')).status == 'error'


def test_unknown_publication_no_mutations(workspace):
    repo = init(workspace)
    runtime = Runtime(config(workspace), approve=yes)
    turn(workspace, runtime, edit())
    before = repo.identity()
    result = asyncio.run(runtime.workflows.undo(state(workspace)))
    assert result.code == 'publication_unknown'
    assert repo.identity() == before
    assert (workspace / 'a.txt').read_text() == 'changed\n'


def test_repeated_reversals_do_not_redo(workspace):
    repo = init(workspace)
    runtime = Runtime(config(workspace), approve=yes)
    turn(workspace, runtime, edit(text='one\n'))
    first = repo.head()
    turn(workspace, runtime, edit(text='two\n'))
    second = repo.head()
    result = asyncio.run(runtime.workflows.undo(state(workspace), 'revert'))
    assert result.status == 'ok', result
    assert repo.metadata(repo.head())['original'] == second
    assert (workspace / 'a.txt').read_text() == 'one\n'
    result = asyncio.run(runtime.workflows.undo(state(workspace), 'revert'))
    assert result.status == 'ok', result
    assert repo.metadata(repo.head())['original'] == first
    assert (workspace / 'a.txt').read_text() == 'original\n'
    assert asyncio.run(runtime.workflows.undo(state(workspace), 'revert')).status == 'error'


def test_published_change_always_reverses(workspace):
    repo = init(workspace)
    remote = workspace.parent / 'remote.git'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    git(workspace, 'remote', 'add', 'origin', str(remote))
    runtime = Runtime(config(workspace), approve=yes)
    turn(workspace, runtime, edit())
    original = repo.head()
    git(workspace, 'push', '-u', 'origin', 'HEAD')
    result = asyncio.run(runtime.workflows.undo(state(workspace), 'local'))
    assert result.status == 'ok', result
    assert repo.metadata(repo.head())['type'] == 'undo'
    assert git(workspace, 'rev-parse', 'HEAD^') == original


def test_command_commit_requires_separate_review(workspace):
    repo = init(workspace)
    reviews = []
    async def approve(review):
        reviews.append(review)
        return 'denied' if review.action == 'commit' else 'approved'
    runtime = Runtime(config(workspace), approve=approve)
    session = state(workspace)
    session.begin('command')
    session._append('message', role='assistant', text='', completion='complete', turn=1, call_seqs=[3])
    seq = session._append('tool_call', turn=1, assistant_seq=2, name='run_command', arguments={'command': 'printf command > a.txt'})
    before = repo.head()
    result = asyncio.run(runtime.execute(session, seq, 'run_command', {'command': 'printf command > a.txt'}))
    result.record(session, seq)
    session.finish('done')
    assert result.status == 'ok'
    assert [r.action for r in reviews] == ['command', 'commit']
    assert repo.head() == before
    decode(encoded(session, None), workspace)


def test_lint_formatter_failure_can_be_corrected(workspace):
    repo = init(workspace)
    before = repo.head()
    cfg = config(workspace, lint={'enabled': True, 'command': "if test \"$(cat a.txt)\" = fixed; then exit 0; fi; printf formatted > a.txt; exit 1"})
    runtime = Runtime(cfg, approve=yes)
    result, _ = turn(workspace, runtime, edit())
    assert result['error']['code'] == 'lint_failed'
    assert (workspace / 'a.txt').read_text() == 'formatted'
    result, _ = turn(workspace, runtime, edit(text='fixed'))
    assert result['status'] == 'ok', result
    assert git(workspace, 'rev-parse', 'HEAD^') == before


def test_pending_concurrent_changes_not_checkpointed_as_own(workspace):
    repo = init(workspace)
    before = repo.head()
    runtime = Runtime(config(workspace, lint={'enabled': True, 'command': 'false'}), approve=yes)
    turn(workspace, runtime, edit())
    (workspace / 'a.txt').write_text('user concurrent edit')
    result, _ = turn(workspace, runtime, edit(text='correction'))
    assert result['status'] == 'error'
    assert (workspace / 'a.txt').read_text() == 'user concurrent edit'
    assert repo.head() == before


def test_nested_repo_patch_rejected_without_changes(workspace):
    init(workspace)
    nested = workspace / 'nested'
    nested.mkdir()
    init(nested)
    result, _ = turn(workspace, Runtime(config(workspace), approve=yes), edit(), edit('nested/a.txt'))
    assert result['status'] == 'error'
    assert (workspace / 'a.txt').read_text() == (nested / 'a.txt').read_text() == 'original\n'


def test_local_undo_preserves_unrelated_index(workspace):
    repo = init(workspace)
    runtime = Runtime(config(workspace), approve=yes)
    turn(workspace, runtime, edit())
    (workspace / 'b.txt').write_text('staged')
    git(workspace, 'add', 'b.txt')
    (workspace / 'b.txt').write_text('unstaged')
    result = asyncio.run(runtime.workflows.undo(state(workspace), 'local'))
    assert result.status == 'ok', result
    assert git(workspace, 'show', ':b.txt') == 'staged'
    assert (workspace / 'b.txt').read_text() == 'unstaged'
    assert repo.undo_target() is None


def test_undo_declined_fetch_requires_explicit_choice(workspace):
    repo = init(workspace)
    runtime = Runtime(config(workspace), approve=yes)
    turn(workspace, runtime, edit())
    head = repo.head()
    async def no_fetch(review):
        return 'denied' if review.action == 'fetch' else 'approved'
    runtime.approve = no_fetch
    result = asyncio.run(runtime.workflows.undo(state(workspace)))
    assert result.code == 'publication_unknown' and repo.head() == head
    result = asyncio.run(runtime.workflows.undo(state(workspace), 'revert'))
    assert result.status == 'ok'
    assert git(workspace, 'rev-parse', 'HEAD^') == head


def test_undo_stops_on_newer_user_commit_and_dirty_target(workspace):
    repo = init(workspace)
    runtime = Runtime(config(workspace), approve=yes)
    turn(workspace, runtime, edit())
    (workspace / 'a.txt').write_text('dirty')
    assert asyncio.run(runtime.workflows.undo(state(workspace), 'local')).status == 'error'
    git(workspace, 'add', 'a.txt')
    git(workspace, 'commit', '-m', 'user change')
    assert repo.undo_target() is None
    assert asyncio.run(runtime.workflows.undo(state(workspace), 'revert')).status == 'error'


def test_undo_deleted_executable_restores_mode(workspace):
    init(workspace)
    (workspace / 'a.txt').chmod(0o755)
    git(workspace, 'add', 'a.txt')
    git(workspace, 'commit', '-m', 'executable')
    runtime = Runtime(config(workspace), approve=yes)
    result, _ = turn(workspace, runtime, {'op': 'delete', 'path': 'a.txt'})
    assert result['status'] == 'ok'
    result = asyncio.run(runtime.workflows.undo(state(workspace), 'local'))
    assert result.status == 'ok', result
    assert (workspace / 'a.txt').stat().st_mode & 0o777 == 0o755


def test_undo_deleted_sensitive_file_requires_read_approval(workspace):
    init(workspace)
    (workspace / '.env.example').write_text('SECRET')
    git(workspace, 'add', '.env.example')
    git(workspace, 'commit', '-m', 'sensitive fixture')
    runtime = Runtime(config(workspace), approve=yes)
    result, _ = turn(workspace, runtime, {'op': 'delete', 'path': '.env.example'})
    assert result['status'] == 'ok'
    reviews = []
    async def no_read(review):
        reviews.append(review)
        return 'denied' if review.action == 'read' else 'approved'
    runtime.approve = no_read
    result = asyncio.run(runtime.workflows.undo(state(workspace), 'local'))
    assert result.status == 'denied'
    assert 'SECRET' not in str(reviews)
    assert not (workspace / '.env.example').exists()


def test_command_changes_commit_only_reviewed_nonignored_paths(workspace):
    repo = init(workspace)
    reviews = []
    async def approve(review):
        reviews.append(review)
        return 'approved'
    class Client:
        async def complete(self, messages):
            return Reply('Record command output')
    runtime = Runtime(config(workspace), approve=approve)
    runtime.client = Client()
    session = state(workspace)
    session.begin('command')
    session._append('message', role='assistant', text='', completion='complete', turn=1, call_seqs=[3])
    command = 'printf command > a.txt; mkdir ignored; printf build > ignored/output'
    session._append('tool_call', turn=1, assistant_seq=2, name='run_command', arguments={'command': command})
    result = asyncio.run(runtime.execute(session, 3, 'run_command', {'command': command}))
    result.record(session, 3)
    session.finish('done')
    assert result.status == 'ok'
    assert result.data['commit']['commit'] == repo.head()
    assert git(workspace, 'show', 'HEAD:a.txt') == 'command'
    assert not git(workspace, 'ls-files', 'ignored')
    assert [review.action for review in reviews] == ['command', 'commit']
    assert git(workspace, 'show', '-s', '--format=%s', 'HEAD') == 'Record command output'
    assert git(workspace, 'show', '-s', '--format=%B', 'HEAD') in reviews[-1].summary
    decode(encoded(session, None), workspace)


def test_cancel_during_patch_mutation_waits_then_leaves_uncommitted(workspace, monkeypatch):
    import threading
    repo = init(workspace)
    head = repo.head()
    started, release = threading.Event(), threading.Event()
    apply = patches.apply
    def paused(*args):
        started.set()
        assert release.wait(5)
        return apply(*args)
    monkeypatch.setattr(patches, 'apply', paused)
    async def scenario():
        runtime = Runtime(config(workspace), approve=yes)
        session = state(workspace)
        session.begin('patch')
        task = asyncio.create_task(runtime.workflows.patch(session, None, {'message': 'change', 'changes': [edit()]}))
        for _ in range(300):
            if started.is_set():
                break
            await asyncio.sleep(.01)
        assert started.is_set()
        task.cancel()
        release.set()
        result = await task
        assert result.status == 'cancelled'
    asyncio.run(scenario())
    assert repo.head() == head
    assert (workspace / 'a.txt').read_text() == 'changed\n'
    assert not list((workspace / '.skua/.tmp').glob('patch-*.json'))


def test_initial_managed_commit_can_be_undone(workspace):
    git(workspace, 'init')
    git(workspace, 'config', 'user.name', 'Test User')
    git(workspace, 'config', 'user.email', 'test@example.invalid')
    runtime = Runtime(config(workspace), approve=yes)
    result, _ = turn(workspace, runtime, edit('first.txt', 'first'))
    assert result['status'] == 'ok', result
    repo = Repo.find(workspace)
    assert repo.head() is not None
    result = asyncio.run(runtime.workflows.undo(state(workspace), 'local'))
    assert result.status == 'ok', result
    assert repo.head() is None
    assert not (workspace / 'first.txt').exists()


def test_init_decline_is_remembered_from_session_events(workspace):
    session = state(workspace)
    session._append('approval', turn=None, call_seq=None, action='checkpoint', decision='denied',
                    reasons=['git_change'], targets=[str(workspace)], summary=f'Initialize Git at {workspace}?',
                    review_sha256='0' * 64)
    calls = []
    async def approve(review):
        calls.append(review.action)
        return 'approved'
    runtime = Runtime(config(workspace), approve=approve)
    assert asyncio.run(runtime.workflows.repo_for(session, None, [workspace / 'new'])) is None
    assert calls == []


def test_recovery_offered_before_credentials_or_endpoint(workspace):
    from skua.terminal import Terminal, start
    from skua.config import ConfigError
    (workspace / 'a.txt').write_text('old')
    path = patches.save_snapshot(workspace, patches.prepare(workspace, [edit()]))
    (workspace / 'a.txt').write_text('changed\n')
    cfg = config(workspace, profiles={'default': {'base_url': 'https://example/v1', 'model': 'm',
                 'auth': {'enabled': True, 'env_var': 'SKUA_MISSING_TEST_CREDENTIAL'}}})
    class UI(Terminal):
        def __init__(self):
            pass
        def write(self, *args, **kwargs):
            pass
        async def read(self, *args):
            return 'yes'
    with pytest.raises(ConfigError):
        asyncio.run(start(cfg, terminal=UI(), client_factory=lambda *args: pytest.fail('endpoint must not be opened')))
    assert not path.exists()
    assert (workspace / 'a.txt').read_text() == 'old'


def test_oversized_review_rejected_before_approval(workspace):
    from skua.permissions import Review
    async def unexpected(review):
        pytest.fail('oversized review must not be approved')
    runtime = Runtime(config(workspace), approve=unexpected)
    session = state(workspace)
    review = Review.create('patch', ['write'], [], '"' * 7_000_000, [])
    with pytest.raises(AccessError, match='record budget'):
        asyncio.run(runtime.review(session, None, review))
    assert not session.events
