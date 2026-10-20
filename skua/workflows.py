"""Patch, lint, checkpoint, command-commit, recovery and undo orchestration."""
import asyncio
from dataclasses import dataclass
import difflib
from pathlib import Path
from uuid import uuid4

from . import patches
from .execution import _finish_despite_cancellation
from .git import GitError, Repo, invoke
from .permissions import AccessError, Review, sensitive


async def mutation(function, *args, **kwargs):
    """A cancelled await never leaves a filesystem/Git mutation running unattended."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        result = await _finish_despite_cancellation(task)
        # Report completed mutations before returning control; do not infer rollback.
        return result


def content_diff(path, old, new):
    old_text = '' if old is None else old.decode('utf-8')
    new_text = '' if new is None else new.decode('utf-8')
    lines = difflib.unified_diff(old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                                fromfile=str(path) if old is not None else '/dev/null',
                                tofile=str(path) if new is not None else '/dev/null')
    return ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n'
                   for line in lines) + '\n'


def current_state(path):
    current = patches.FileState.inspect(path)
    if current.path != path:
        raise AccessError('Target was replaced by a symlink; obtain a new review.')
    if current.target:
        from dataclasses import replace
        current = replace(current, content=current.target.read().encode())
    return current


@dataclass
class Pending:
    repo: Repo | None
    identity: tuple | None
    change_id: str
    title: str
    expected: dict
    corrections: int = 0


class Workflows:
    def __init__(self, runtime):
        self.runtime = runtime
        self.pending = None
        self.init_offered = False
        self.in_lint = False

    @property
    def root(self):
        return self.runtime.config.root

    async def review(self, session, call_seq, action, paths, summary, evidence, reasons=None):
        review = Review.create(action, reasons or ['write'], [str(p) for p in paths], summary, evidence)
        return await self.runtime.review(session, call_seq, review)

    async def history_access(self, session, call_seq, paths):
        """Deleted sensitive files still require permission before reading Git contents."""
        from .tools import Result
        missing = [p for p in paths if not p.exists() and sensitive(self.runtime.config, p)]
        if missing:
            decision = await self.review(session, call_seq, 'read', missing,
                'Read historical sensitive contents from Git for diff review; the reviewed diff may be sent to the model.',
                [str(p) for p in missing], ['sensitive'])
            if decision != 'approved':
                return Result(decision, 'Historical sensitive read not approved; operation stopped.')
        return None

    async def repo_for(self, session, call_seq, paths):
        repo = await asyncio.to_thread(Repo.find, self.root)
        if any(event['type'] == 'approval' and event['action'] == 'checkpoint'
               and event['summary'].startswith('Initialize Git at ') for event in session.events):
            self.init_offered = True
        if repo is None and not self.init_offered:
            self.init_offered = True
            decision = await self.review(session, call_seq, 'checkpoint', [self.root],
                                         f'Initialize Git at {self.root}? Declining permits approved edits without commit-based undo.',
                                         [], ['git_change'])
            if decision == 'cancelled':
                raise asyncio.CancelledError
            if decision == 'approved':
                await mutation(invoke, self.root, 'init')
                repo = await asyncio.to_thread(Repo.find, self.root)
        target_repos = await asyncio.to_thread(lambda: [Repo.find(p) for p in paths])
        if any(r != repo for r in target_repos):
            raise GitError('Split patches by repository. External or nested-repository targets need their own workflow.')
        if repo:
            await asyncio.to_thread(repo.validate_targets, paths)
        return repo

    async def checkpoint(self, session, call_seq, repo, patch, paths):
        from .tools import Result
        if not paths or not await asyncio.to_thread(repo.dirty, paths):
            return None
        expected = await asyncio.to_thread(repo.identity)
        diff = ''
        for path in paths:
            old = await asyncio.to_thread(repo.head_content, path)
            if old is not None:
                patches.text_bytes(old.decode('utf-8'))
            diff += content_diff(path, old, patch.before[path].content)
        staged = await asyncio.to_thread(repo.run, 'diff', '--cached', '--no-ext-diff', '--no-textconv', '--', *repo.relative(paths))
        decision = await self.review(session, call_seq, 'checkpoint', paths,
            'Checkpoint existing work in these targets before applying the patch.\n'
            'This commits the reviewed working contents and clears staging for these targets only.\n' + diff +
            '\nExisting staged diff in these targets:\n' + staged.stdout.decode('utf-8', 'replace'),
            [expected[0], diff, staged.stdout.hex()], ['git_change'])
        if decision != 'approved':
            return Result(decision, 'Checkpoint declined/cancelled; entire patch remains unapplied.')
        patch.check()
        result = await mutation(repo.commit, paths, 'Checkpoint existing work', 'checkpoint', uuid4().hex, expected)
        self.report_commit(result)
        patch.check()  # Hook edits invalidate the pending patch review.
        return None

    def report_commit(self, result):
        text = result['message']
        if result['commit']:
            text += ' ' + result['commit']
        if result['hooks']:
            text += '\nGit used normal hook processing; executable hooks configured: ' + ', '.join(result['hooks'])
        self.runtime.on_notice(text)

    async def patch(self, session, call_seq, args):
        from .tools import Result
        try:
            paths = patches.requested_paths(self.root, args['changes'])
            protected = self.root / '.skua/.tmp'
            if any(p.resolve().is_relative_to(protected) for p in paths):
                raise AccessError('Patch tools cannot edit their recovery storage.')
            inspected = await asyncio.to_thread(lambda: [patches.FileState.inspect(p) for p in paths])
            existing = [item.target for item in inspected if item.target]
            denied = await self.runtime.authorize(session, call_seq, existing, contents=True)
            if denied:
                self.pending = None
                return denied
            patch = await asyncio.to_thread(patches.prepare, self.root, args['changes'], inspected)
            repo = await self.repo_for(session, call_seq, list(patch.before))
            pending = self.pending
            if pending:
                if repo != pending.repo:
                    raise GitError('Finish the pending lint correction in the same repository.')
                await asyncio.to_thread(self.check_pending)
                if pending.corrections >= 3:
                    self.pending = None
                    return Result('error', 'Lint correction limit exhausted; changes remain uncommitted.', code='lint_exhausted')
            new_paths = [p for p in patch.before if not pending or p not in pending.expected]
            if repo:
                refused = await self.checkpoint(session, call_seq, repo, patch, new_paths)
                if refused:
                    self.pending = None
                    return refused
            identity = await asyncio.to_thread(repo.identity) if repo else None
            diff = patch.diff()
            reasons = ['write'] + (['git_change'] if repo else [])
            if any(not p.is_relative_to(self.root) for p in patch.before):
                reasons.append('outside_root')
            summary = ('Apply and commit this patch' if repo else 'Apply this patch without Git commit-based undo')
            if pending:
                summary += f' as correction {pending.corrections + 1}/3 of the pending change'
            summary += '.\n' + diff
            if self.runtime.config.lint.enabled:
                summary += '\nConfigured lint requires separate command approval before commit.'
            decision = await self.review(session, call_seq, 'patch', list(patch.before), summary,
                                         [identity[0] if identity else None, diff], reasons)
            if decision != 'approved':
                self.pending = None
                return Result(decision, 'Patch not applied; any earlier pending edits remain uncommitted.')
            patch.check()
            if repo:
                repo.assert_identity(identity)
            applied, unresolved, snapshot = await mutation(patches.apply, self.root, patch)
            if not applied:
                self.pending = None
                return Result('error', 'Patch application failed; restoration attempted. ' +
                              ('Unresolved paths: ' + ', '.join(unresolved) + f'; recovery: {snapshot}' if unresolved else 'Original state restored.'),
                              code='patch_failed')
            if pending is None:
                pending = Pending(repo, identity, uuid4().hex, args['message'], {})
            else:
                pending.corrections += 1
                pending.identity = identity
            pending.expected.update({p: (patch.after[p], patch.modes[p]) for p in patch.before})
            self.pending = pending
            # Cancellation after completed mutation must not start lint or a commit.
            if asyncio.current_task().cancelling():
                self.pending = None
                return Result('cancelled', 'Patch applied; cancellation left edits uncommitted.')
            return await self.finish(session, call_seq)
        except (AccessError, OSError, UnicodeError) as exc:
            self.pending = None
            return Result('error', str(exc) if isinstance(exc, AccessError) else 'Patch could not complete; inspect current files and recovery records.',
                          code='patch_error')

    def check_pending(self):
        if self.pending is None:
            return
        if self.pending.repo:
            self.pending.repo.assert_identity(self.pending.identity)
        for path, (contents, mode) in self.pending.expected.items():
            current = current_state(path)
            if current.path != path or current.content != contents or (contents is not None and current.mode != mode):
                raise AccessError('Pending files changed outside the reviewed correction sequence; edits kept uncommitted.')

    async def finish(self, session, call_seq):
        from .tools import Result
        pending = self.pending
        config = self.runtime.config
        if config.lint.enabled:
            self.in_lint = True
            try:
                lint = await self.runtime.command(session, call_seq, config.lint.command)
            finally:
                self.in_lint = False
            if lint.status != 'denied':
                for path in pending.expected:
                    current = await asyncio.to_thread(current_state, path)
                    pending.expected[path] = (current.content, current.mode)
            # Retained lint output belongs to the patch result (no duplicate call result).
            if lint.status != 'ok':
                if lint.status in {'cancelled', 'denied'} or pending.corrections >= 3:
                    self.pending = None
                return Result(lint.status if lint.status in {'cancelled', 'denied'} else 'error',
                              'Patch applied; lint did not pass. Changes remain uncommitted.\n' + lint.text +
                              ('\nCorrection budget exhausted.' if pending.corrections >= 3 else '\nUse a reviewed corrective patch; at most three corrections.'),
                              lint.data, lint.output, 'lint_exhausted' if pending.corrections >= 3 else 'lint_failed')
            # Formatting may have changed a target. It was authorized by lint approval.
            # Never silently include new paths or concurrent edits outside pending targets.
            for path in pending.expected:
                current = await asyncio.to_thread(current_state, path)
                pending.expected[path] = (current.content, current.mode)
        await asyncio.to_thread(self.check_pending)
        self.pending = None
        if not pending.repo:
            return Result('ok', 'Patch applied without Git; commit-based undo is unavailable.')
        result = await mutation(pending.repo.commit, list(pending.expected), pending.title, 'change',
                                pending.change_id, pending.identity)
        self.report_commit(result)
        return Result('ok', 'Patch applied. ' + result['message'] + (f" Commit: {result['commit']}" if result['commit'] else ''), result)

    async def recovery(self, session):
        directory = patches.recovery_dir(self.root)
        if not directory.exists():
            return
        for path in sorted(directory.glob('patch-*.json')):
            try:
                document = await asyncio.to_thread(patches.load_snapshot, self.root, path)
                targets = [Path(item['path']) for item in document['files']]
                decision = await self.review(session, None, 'patch', targets,
                    f'Unfinished patch recovery: {path}\nRestore pre-patch contents only where current contents still match the snapshot? '
                    'Newer edits will be preserved. Recovery does not alter Git history.',
                    document, ['write'] + (['outside_root'] if any(not p.is_relative_to(self.root) for p in targets) else []))
                if decision != 'approved':
                    self.runtime.on_notice('Recovery left pending: ' + str(path))
                    continue
                unresolved = await mutation(patches.recover, self.root, path)
                message = 'Recovery complete.' if not unresolved else 'Recovery incomplete; newer/unavailable paths preserved: ' + ', '.join(unresolved)
                session.notice('patch_recovery', message)
                self.runtime.on_notice(message)
                if asyncio.current_task().cancelling():
                    return
            except (AccessError, OSError) as exc:
                self.runtime.on_notice(str(exc))

    async def undo(self, session, strategy=None):
        from .tools import Result
        try:
            if self.pending:
                raise GitError('Finish or abandon the pending lint change before undo.')
            repo = await asyncio.to_thread(Repo.find, self.root)
            if not repo:
                raise GitError('No Git repository; commit-based undo is unavailable.')
            target = await asyncio.to_thread(repo.undo_target)
            if not target:
                raise GitError('No consecutive Skua change at the tip; checkpoints and other commits are protected.')
            decision = await self.review(session, None, 'fetch', [repo.root],
                'Fetch all configured remotes before checking publication? This accesses the network. '
                'Declining leaves publication status unknown.', [target], ['network', 'git_change'])
            fetched = decision == 'approved' and await mutation(repo.fetch)
            if asyncio.current_task().cancelling():
                return Result('cancelled', 'Undo cancelled after fetch; files and history unchanged.')
            published = fetched and await asyncio.to_thread(repo.published, target)
            if decision == 'cancelled':
                return Result('cancelled', 'Undo cancelled.')
            if published:
                strategy = 'revert'
            elif strategy not in {'local', 'revert'}:
                return Result('error', 'Publication is unknown; no files/history changed. Choose /undo local only if you confirm '
                              'the change was never shared, or /undo revert to preserve history.', code='publication_unknown')
            expected = await asyncio.to_thread(repo.identity)
            if strategy == 'local' and target != expected[0]:
                raise GitError('Local undo requires the target at the current tip; use /undo revert for a reversal chain.')
            names = await asyncio.to_thread(repo.run, 'diff-tree', '--root', '--no-commit-id', '--name-only', '-r', '-z', target)
            import os
            paths = [repo.root / os.fsdecode(p) for p in names.stdout.split(b'\0') if p]
            if any(not p.is_relative_to(self.root) for p in paths):
                raise GitError('Undo spans outside the selected project root; launch at the repository root to review it.')
            await asyncio.to_thread(repo.validate_targets, paths)
            if await asyncio.to_thread(repo.dirty, paths):
                raise GitError('Undo targets have staged/unstaged work; preserve or commit it before undo.')
            inspected = await asyncio.to_thread(lambda: [patches.FileState.inspect(p) for p in paths])
            denied = await self.runtime.authorize(session, None, [f.target for f in inspected if f.target], contents=True)
            if denied:
                return denied
            denied = await self.history_access(session, None, paths)
            if denied:
                return denied
            parent, files = await asyncio.to_thread(repo.undo_files, target)
            changes = []
            for path, old, new, mode in files:
                current = await asyncio.to_thread(current_state, path)
                if current.content != new:
                    raise GitError('Undo target differs from the original change; cannot preserve newer work safely.')
                if old is None:
                    changes.append({'op': 'delete', 'path': str(path)})
                else:
                    changes.append({'op': 'write', 'path': str(path), 'content': old.decode('utf-8')})
            patch = await asyncio.to_thread(patches.prepare, self.root, changes, inspected)
            for path, old, new, mode in files:
                patch.modes[path] = mode
            decision = await self.review(session, None, 'undo', paths,
                f'Undo {target} using {"local tip rewrite (you confirm never shared)" if strategy == "local" else "a new reversing commit"}.\n' + patch.diff(),
                [expected[0], target, strategy, patch.diff()], ['git_change', 'write'])
            if decision != 'approved':
                return Result(decision, 'Undo not applied.')
            repo.assert_identity(expected)
            patch.check()
            applied, unresolved, snapshot = await mutation(patches.apply, self.root, patch)
            if not applied:
                return Result('error', f'Undo file restoration failed; unresolved={unresolved}; recovery={snapshot}', code='undo_failed')
            if asyncio.current_task().cancelling():
                return Result('cancelled', 'Undo file changes applied; cancellation left them uncommitted. Git history unchanged.')
            if strategy == 'local':
                await mutation(repo.rewind, expected, parent, paths)
                return Result('ok', 'Local change removed; unrelated work preserved. HEAD: ' + (parent or 'unborn branch'))
            result = await mutation(repo.commit, paths, 'Undo Skua change ' + target[:12], 'undo', uuid4().hex, expected, original=target)
            self.report_commit(result)
            return Result('ok', 'Reversing commit created: ' + str(result['commit']), result)
        except (AccessError, OSError, UnicodeError) as exc:
            return Result('error', str(exc) if isinstance(exc, AccessError) else 'Undo failed; inspect files and Git state. No automatic retry.', code='undo_error')

    async def command_before(self):
        """Capture only clean, discoverable text paths; ambiguous overlaps are excluded."""
        if self.in_lint or self.pending:
            return None
        repo = await asyncio.to_thread(Repo.find, self.root)
        if not repo:
            return None
        return repo, await asyncio.to_thread(repo.identity), await asyncio.to_thread(
            repo.run, 'status', '--porcelain=v1', '-z', '--untracked-files=all')

    async def command_commit(self, session, call_seq, baseline, changes):
        if baseline is None or self.in_lint or self.pending:
            return None
        from .tools import Result
        repo, identity, status = baseline
        # Any prior dirtiness makes attribution uncertain. Do not guess which hunks
        # were caused by a command; leave them for an explicitly reviewed patch.
        if status.stdout:
            self.runtime.on_notice('Command commit not offered: preexisting repository changes make attribution ambiguous. Edits remain uncommitted.')
            return None
        repo.assert_identity(identity)
        paths = []
        for entry in changes:
            path = self.root / entry['path']
            if path.is_dir() or path.is_symlink() or not path.is_relative_to(repo.root):
                continue
            if repo.ignored(path) or Repo.find(path) != repo:
                continue
            paths.append(path)
        if not paths:
            return None
        inspected = [patches.FileState.inspect(p) for p in paths]
        denied = await self.runtime.authorize(session, call_seq, [f.target for f in inspected if f.target], contents=True)
        if denied:
            return None
        denied = await self.history_access(session, call_seq, paths)
        if denied:
            return None
        diff = ''
        for item in inspected:
            item.check()
            current = current_state(item.path)
            old = repo.head_content(item.path)
            diff += content_diff(item.path, old, current.content)
        decision = await self.review(session, call_seq, 'commit', paths,
            'Commit these observed command changes? This is separate from command execution approval. '
            'Review for concurrent edits; only these paths will be staged.\n' + diff, [identity[0], diff], ['git_change'])
        if decision != 'approved':
            self.runtime.on_notice('Command changes left uncommitted.')
            return None
        for item in inspected:
            item.check()
        result = await mutation(repo.commit, paths, 'Record approved command changes', 'change', uuid4().hex, identity)
        self.report_commit(result)
        return result
