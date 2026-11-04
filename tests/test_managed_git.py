"""M2: optional managed Git leaves reviewed editing and recovery intact."""
import asyncio

import pytest

from skua.agent import run_turn
from skua.client import Reply, ToolCall
from skua.config import ConfigError, build_config
from skua.tools import Runtime
from skua import patches
from test_patches import config, init, git, state, edit, turn, yes


@pytest.mark.parametrize('repository', [False, True])
def test_unmanaged_edit_test_fix_preserves_index_without_author(workspace, monkeypatch, repository):
    repo = init(workspace) if repository else None
    if repo:
        (workspace / 'a.txt').write_text('staged target\n')
        (workspace / 'b.txt').write_text('staged unrelated\n')
        git(workspace, 'add', 'a.txt', 'b.txt')
        (workspace / 'b.txt').write_text('unstaged unrelated\n')
        git(workspace, 'config', '--unset', 'user.name')
        git(workspace, 'config', '--unset', 'user.email')
        git(workspace, 'config', 'user.useConfigOnly', 'true')
        before = repo.identity()
    else:
        (workspace / 'a.txt').write_text('old\n')
        (workspace / 'b.txt').write_text('unstaged unrelated\n')
    reviews = []
    async def approve(review):
        reviews.append(review)
        assert review.action in {'patch', 'command'}
        return 'approved'
    runtime = Runtime(config(workspace, git={'managed': False},
                             lint={'enabled': True, 'command': 'grep fixed a.txt'}), approve=approve)
    replies = iter([
        Reply('', (ToolCall('apply_patch', {'message': 'edit', 'changes': [edit(text='bad\n')]}),)),
        Reply('', (ToolCall('apply_patch', {'message': 'fix', 'changes': [edit(text='fixed\n')]}),)),
        Reply('done'),
    ])
    class Client:
        async def complete(self, messages, **kwargs):
            assert 'Managed Git: disabled' in messages[0]['content']
            assert 'tools' in kwargs, 'unexpected commit-message request'
            return next(replies)
    runtime.client = Client()
    session = state(workspace)
    asyncio.run(run_turn(session, runtime.client, 'edit/test/fix', runtime=runtime))
    assert [e['status'] for e in session.events if e['type'] == 'tool_result'] == ['error', 'ok']
    assert [r.action for r in reviews] == ['patch', 'command', 'patch', 'command']
    assert runtime.workflows.pending is None
    assert (workspace / 'a.txt').read_text() == 'fixed\n'
    assert (workspace / 'b.txt').read_text() == 'unstaged unrelated\n'
    if repo:
        assert repo.identity() == before
    else:
        assert not (workspace / '.git').exists()
    assert not list((workspace / '.skua').glob('sessions/*'))


def test_unmanaged_command_never_offers_commit(workspace):
    repo = init(workspace)
    before = repo.identity()
    reviews = []
    async def approve(review):
        reviews.append(review.action)
        return 'approved'
    runtime = Runtime(config(workspace, git={'managed': False}), approve=approve)
    result = asyncio.run(runtime.execute(state(workspace), 3, 'run_command', {'command': 'printf changed > a.txt'}))
    assert result.status == 'ok' and result.data['commit'] is None
    assert reviews == ['command']
    assert repo.identity() == before
    assert (workspace / 'a.txt').read_text() == 'changed'


@pytest.mark.parametrize('change', ['file', 'index', 'head'])
def test_unmanaged_patch_rejects_changed_identity(workspace, change):
    repo = init(workspace)
    async def approve(review):
        if review.action == 'patch':
            if change == 'file':
                (workspace / 'a.txt').write_text('newer user work')
            elif change == 'index':
                (workspace / 'b.txt').write_text('staged later')
                git(workspace, 'add', 'b.txt')
            else:
                git(workspace, 'commit', '--allow-empty', '-m', 'concurrent user commit')
        return 'approved'
    result, _ = turn(workspace, Runtime(config(workspace, git={'managed': False}), approve=approve), edit())
    assert result['status'] == 'error'
    assert (workspace / 'a.txt').read_text() == ('newer user work' if change == 'file' else 'original\n')


@pytest.mark.parametrize('managed', [False, True])
def test_recovery_approval_and_conditional_restore_in_both_modes(workspace, managed):
    init(workspace)
    patch = patches.prepare(workspace, [edit()])
    snapshot = patches.save_snapshot(workspace, patch)
    (workspace / 'a.txt').write_text('changed\n')
    runtime = Runtime(config(workspace, git={'managed': managed}))
    asyncio.run(runtime.workflows.recovery(state(workspace)))
    assert snapshot.exists()
    runtime.approve = yes
    (workspace / 'a.txt').write_text('newer work')
    asyncio.run(runtime.workflows.recovery(state(workspace)))
    assert snapshot.exists() and (workspace / 'a.txt').read_text() == 'newer work'
    (workspace / 'a.txt').write_text('changed\n')
    asyncio.run(runtime.workflows.recovery(state(workspace)))
    assert not snapshot.exists() and (workspace / 'a.txt').read_text() == 'original\n'


def test_unmanaged_partial_patch_restores_originals(workspace, monkeypatch):
    repo = init(workspace)
    before = repo.identity()
    write = patches.write_file
    def fail(path, *args, **kwargs):
        if path.name == 'last':
            raise OSError('injected failure')
        return write(path, *args, **kwargs)
    monkeypatch.setattr(patches, 'write_file', fail)
    result, _ = turn(workspace, Runtime(config(workspace, git={'managed': False}), approve=yes),
                     edit(), edit('last', 'new'))
    assert result['status'] == 'error'
    assert (workspace / 'a.txt').read_text() == 'original\n'
    assert not (workspace / 'last').exists() and repo.identity() == before


def test_unmanaged_can_edit_ignored_files_and_undo_is_unavailable(workspace):
    repo = init(workspace)
    before = repo.identity()
    runtime = Runtime(config(workspace, git={'managed': False}), approve=yes)
    result, _ = turn(workspace, runtime, edit('ignored/new'))
    assert result['status'] == 'ok'
    assert repo.identity() == before
    result = asyncio.run(runtime.workflows.undo(state(workspace)))
    assert result.status == 'error' and 'Managed Git is disabled' in result.text


def test_managed_git_config_default_override_and_validation(workspace):
    assert config(workspace).managed_git is True
    cfg = build_config(workspace, [('global', {'schema_version': 1, 'git': {'managed': True}}),
                                   ('project', {'schema_version': 1, 'git': {'managed': False}})])
    assert cfg.managed_git is False and cfg.sources['git.managed'] == 'project'
    for value in ['false', 0, None]:
        with pytest.raises(ConfigError, match='git.managed'):
            config(workspace, git={'managed': value})


@pytest.mark.parametrize('managed', [False, True])
@pytest.mark.parametrize('location', ['nested', 'external'])
def test_repository_boundary_is_only_a_managed_patch_restriction(workspace, managed, location):
    root_repo = init(workspace)
    other = (workspace if location == 'nested' else workspace.parent) / 'other-repo'
    other.mkdir()
    other_repo = init(other)
    for root in [workspace, other]:
        (root / 'a.txt').write_text('staged target\n')
        (root / 'b.txt').write_text('staged unrelated\n')
        git(root, 'add', 'a.txt', 'b.txt')
        (root / 'b.txt').write_text('unstaged unrelated\n')
    before = [repo.identity() for repo in [root_repo, other_repo]]
    reviews = []
    async def approve(review):
        reviews.append(review)
        return 'approved'
    result, _ = turn(workspace, Runtime(config(workspace, git={'managed': managed}), approve=approve),
                     edit(), edit(str(other / 'a.txt')))
    assert result['status'] == ('error' if managed else 'ok')
    if managed:
        assert 'Split patches by repository' in result['model_text']
    else:
        assert any(r.action == 'patch' for r in reviews)
        if location == 'external':
            assert any('outside_root' in r.reasons for r in reviews)
    for root in [workspace, other]:
        assert (root / 'a.txt').read_text() == ('staged target\n' if managed else 'changed\n')
        assert (root / 'b.txt').read_text() == 'unstaged unrelated\n'
    assert [repo.identity() for repo in [root_repo, other_repo]] == before


@pytest.mark.parametrize('replacement', ['file', 'symlink'])
def test_unmanaged_nested_target_replacement_is_rejected(workspace, replacement):
    init(workspace)
    nested = workspace / 'nested'
    nested.mkdir()
    init(nested)
    target = nested / 'a.txt'
    async def approve(review):
        if review.action == 'patch':
            target.unlink()
            if replacement == 'file':
                target.write_text('concurrent work')
            else:
                target.symlink_to(workspace / 'a.txt')
        return 'approved'
    result, _ = turn(workspace, Runtime(config(workspace, git={'managed': False}), approve=approve),
                     edit(str(target)))
    assert result['status'] == 'error'
    assert (workspace / 'a.txt').read_text() == 'original\n'
    assert target.read_text() == ('concurrent work' if replacement == 'file' else 'original\n')


def test_unmanaged_cross_repository_failure_restores_targets(workspace, monkeypatch):
    init(workspace)
    nested = workspace / 'nested'
    nested.mkdir()
    init(nested)
    write = patches.write_file
    def fail(path, *args, **kwargs):
        if path == nested / 'a.txt' and args[0] == b'changed\n':
            raise OSError('injected failure')
        return write(path, *args, **kwargs)
    monkeypatch.setattr(patches, 'write_file', fail)
    result, _ = turn(workspace, Runtime(config(workspace, git={'managed': False}), approve=yes),
                     edit(), edit(str(nested / 'a.txt')))
    assert result['status'] == 'error'
    assert (workspace / 'a.txt').read_text() == 'original\n'
    assert (nested / 'a.txt').read_text() == 'original\n'


def test_unmanaged_external_patch_still_requires_authorization(workspace):
    init(workspace)
    other = workspace.parent / 'external'
    other.mkdir()
    init(other)
    result, _ = turn(workspace, Runtime(config(workspace, git={'managed': False})),
                     edit(str(other / 'a.txt')))
    assert result['status'] == 'denied'
    assert (other / 'a.txt').read_text() == 'original\n'


def test_unmanaged_lint_correction_can_cross_repository_boundaries(workspace):
    init(workspace)
    nested = workspace / 'nested'
    nested.mkdir()
    init(nested)
    runtime = Runtime(config(workspace, git={'managed': False},
                             lint={'enabled': True, 'command': 'grep fixed nested/a.txt'}), approve=yes)
    result, _ = turn(workspace, runtime, edit())
    assert result['status'] == 'error' and runtime.workflows.pending is not None
    result, _ = turn(workspace, runtime, edit('nested/a.txt', 'fixed\n'))
    assert result['status'] == 'ok' and runtime.workflows.pending is None
    assert (workspace / 'a.txt').read_text() == 'changed\n'
    assert (nested / 'a.txt').read_text() == 'fixed\n'
