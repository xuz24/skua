"""Git CLI integration: isolated commit indexes and conservative undo planning."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
from uuid import uuid4

from .permissions import AccessError


class GitError(AccessError):
    pass


def environment(index=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_EDITOR='true', GIT_LITERAL_PATHSPECS='1')
    if index:
        env['GIT_INDEX_FILE'] = str(index)
    return env


def invoke(root, *args, index=None, input=None, check=True, timeout=60):
    try:
        env = environment(index)
        if args and args[0] == 'check-ignore':
            env.pop('GIT_LITERAL_PATHSPECS', None)  # check-ignore accepts literal names, not pathspec magic.
        process = subprocess.Popen(['git', '-C', str(root), *args], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                   start_new_session=True)
        try:
            out, err = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            raise GitError('Git timed out; its process group was stopped. Inspect repository state before retrying.') from None
        result = subprocess.CompletedProcess(process.args, process.returncode, out, err)
    except (OSError, subprocess.SubprocessError):
        raise GitError('Git could not complete; inspect repository state before retrying.') from None
    if check and result.returncode:
        raise GitError('Git operation failed. Changes are retained; check identity, hooks, conflicts, and repository permissions.')
    return result


@dataclass(frozen=True)
class Repo:
    root: Path
    index: Path

    @classmethod
    def find(cls, path):
        candidate = path if path.is_dir() else path.parent
        while not candidate.exists():
            candidate = candidate.parent
        result = invoke(candidate, 'rev-parse', '--show-toplevel', check=False)
        if result.returncode:
            return None
        root = Path(os.fsdecode(result.stdout).strip()).resolve()
        index = invoke(root, 'rev-parse', '--path-format=absolute', '--git-path', 'index').stdout
        return cls(root, Path(os.fsdecode(index).strip()))

    def run(self, *args, **kwargs):
        return invoke(self.root, *args, **kwargs)

    def head(self):
        result = self.run('rev-parse', '--verify', 'HEAD', check=False)
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def relative(self, paths):
        return [str(p.relative_to(self.root)) for p in paths]

    def index_bytes(self):
        return self.index.read_bytes() if self.index.exists() else None

    def identity(self):
        return self.head(), self.index_bytes()

    def assert_identity(self, expected):
        if self.identity() != expected:
            raise GitError('Git HEAD or index changed since review; request a new review.')

    def dirty(self, paths):
        result = self.run('status', '--porcelain=v1', '-z', '--untracked-files=all', '--', *self.relative(paths))
        return bool(result.stdout)

    def tracked(self, path):
        return bool(self.run('ls-files', '-z', '--', *self.relative([path])).stdout)

    def ignored(self, path):
        result = self.run('check-ignore', '-q', '--', *self.relative([path]), check=False)
        if result.returncode not in {0, 1}:
            raise GitError('Could not determine ignore status; refusing automatic staging.')
        return result.returncode == 0

    def head_content(self, path):
        head = self.head()
        if head is None:
            return None
        name = path.relative_to(self.root).as_posix()
        result = self.run('show', head + ':' + name, check=False)
        return result.stdout if result.returncode == 0 else None

    def diff(self, paths):
        # Caller has already approved sensitive/external content reads.
        return self.run('diff', '--no-ext-diff', '--no-textconv', 'HEAD', '--', *self.relative(paths)).stdout.decode('utf-8', 'replace') if self.head() else ''

    def validate_targets(self, paths):
        for path in paths:
            if not path.is_relative_to(self.root) or Repo.find(path) != self:
                raise GitError('Split patches by repository; nested/external repositories cannot share a managed commit.')
            if self.ignored(path) and not self.tracked(path):
                raise GitError('Ignored untracked patch targets cannot be automatically committed; change ignore rules or use an approved command.')
            # Symlink and submodule index entries cannot be managed as ordinary text.
            entries = self.run('ls-files', '--stage', '-z', '--', *self.relative([path])).stdout
            if entries and not entries.startswith((b'100644 ', b'100755 ')):
                raise GitError('Symlink, submodule, or conflicted index targets are unsupported for managed patches.')
        if self.run('ls-files', '-u').stdout:
            raise GitError('Resolve index conflicts before managed Git changes.')
        for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
            location = self.run('rev-parse', '--git-path', name).stdout.decode().strip()
            if (self.root / location).exists():
                raise GitError('Finish the active Git operation before managed changes.')

    def commit(self, paths, title, kind, change_id, expected, *, original=None):
        """Commit only targets, then update just those paths in the user's original index.

        Holding index.lock prevents cooperating Git writers. Hooks inherit the isolated
        index, run normally, and can intentionally alter the resulting commit.
        """
        self.validate_targets(paths)
        self.assert_identity(expected)
        lock = self.index.with_name(self.index.name + '.lock')
        try:
            lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            raise GitError('Git index is locked; nothing committed.') from None
        try:
            self.assert_identity(expected)
            with tempfile.TemporaryDirectory(prefix='skua-index-', dir=self.index.parent) as directory:
                isolated = Path(directory) / 'commit-index'
                if expected[0]:
                    self.run('read-tree', expected[0], index=isolated)
                else:
                    self.run('read-tree', '--empty', index=isolated)
                self.run('add', '-A', '--', *self.relative(paths), index=isolated)
                # An empty patch must not create an artificial undo boundary.
                if expected[0] and not self.run('diff', '--cached', '--name-only', expected[0], index=isolated).stdout:
                    return {'commit': None, 'hooks': [], 'message': 'No content change to commit.'}
                metadata = {'version': 1, 'type': kind, 'id': change_id}
                if original:
                    metadata['original'] = original
                message = title.splitlines()[0][:200] + '\n\nSkua-Metadata: ' + json.dumps(metadata, sort_keys=True) + '\n'
                hooks_path = self.run('rev-parse', '--git-path', 'hooks').stdout.decode().strip()
                hooks = [name for name in ('pre-commit', 'prepare-commit-msg', 'commit-msg', 'post-commit')
                         if os.access(self.root / hooks_path / name, os.X_OK)]
                try:
                    result = self.run('commit', '--file=-', index=isolated, input=message.encode(), check=False, timeout=300)
                    reported_success = result.returncode == 0
                except GitError:
                    reported_success = False
                actual = self.head()
                if actual == expected[0]:
                    raise GitError('Commit failed; no new commit detected. Applied edits and hook changes remain. '
                                   'A retry requires fresh diff review; hooks will not be bypassed.')
                # Confirm a normal single-parent continuation; never reset unexpected history.
                parents = self.run('rev-list', '--parents', '-n', '1', actual).stdout.decode().split()[1:]
                if parents != ([expected[0]] if expected[0] else []):
                    raise GitError('HEAD changed unexpectedly during commit; inspect history and index before continuing.')
                original_index = Path(directory) / 'original-index'
                if expected[1] is not None:
                    original_index.write_bytes(expected[1])
                else:
                    self.run('read-tree', '--empty', index=original_index)
                self.run('reset', '-q', actual, '--', *self.relative(paths), index=original_index)
                with os.fdopen(os.dup(lock_fd), 'wb') as stream:
                    stream.write(original_index.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(lock, self.index)
                return {'commit': actual, 'hooks': hooks, 'message':
                        'Commit created.' if reported_success else 'Commit exists despite Git reporting a failure; inspect hooks and repository state locally.'}
        finally:
            os.close(lock_fd)
            if lock.exists():
                lock.unlink()

    def metadata(self, commit):
        message = self.run('show', '-s', '--format=%B', commit).stdout.decode('utf-8', 'replace')
        lines = [line[len('Skua-Metadata: '):] for line in message.splitlines() if line.startswith('Skua-Metadata: ')]
        if len(lines) != 1:
            return None
        try:
            value = json.loads(lines[0])
            expected = {'version', 'type', 'id'} | ({'original'} if value.get('type') == 'undo' else set())
            if set(value) != expected or value['version'] != 1 or value['type'] not in {'change', 'checkpoint', 'undo'}:
                return None
            if not re.fullmatch('[0-9a-f]{32}', value['id']):
                return None
            if 'original' in value and not re.fullmatch('[0-9a-f]{40,64}', value['original']):
                return None
            return value
        except (ValueError, TypeError, KeyError):
            return None

    def undo_target(self):
        commits = self.run('rev-list', '--first-parent', 'HEAD', check=False).stdout.decode().splitlines()
        undone = set()
        for commit in commits:
            meta = self.metadata(commit)
            if not meta or meta['type'] == 'checkpoint':
                return None
            if meta['type'] == 'undo':
                undone.add(meta['original'])
            elif commit not in undone:
                return commit
        return None

    def undo_files(self, commit):
        parents = self.run('rev-list', '--parents', '-n', '1', commit).stdout.decode().split()[1:]
        if len(parents) > 1:
            raise GitError('Undo of merge changes is unsupported.')
        parent = parents[0] if parents else None
        paths = self.run('diff-tree', '--root', '--no-commit-id', '--name-only', '-r', '-z', commit).stdout.split(b'\0')
        result = []
        for raw in paths:
            if not raw:
                continue
            path = self.root / os.fsdecode(raw)
            old = self.run('show', parent + ':' + os.fsdecode(raw), check=False) if parent else None
            new = self.run('show', commit + ':' + os.fsdecode(raw), check=False)
            entry = self.run('ls-tree', parent, '--', os.fsdecode(raw)).stdout if parent else b''
            mode = 0o755 if entry.startswith(b'100755 ') else 0o644
            if entry and not entry.startswith((b'100644 ', b'100755 ')):
                raise GitError('Undo of symlink/submodule entries is unsupported.')
            result.append((path, old.stdout if old and old.returncode == 0 else None,
                           new.stdout if new.returncode == 0 else None, mode))
        return parent, result

    def rewind(self, expected, parent, paths):
        """Move tip after reviewed inverse patch, preserving unrelated index entries."""
        self.assert_identity(expected)
        lock = self.index.with_name(self.index.name + '.lock')
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            raise GitError('Git index is locked; history not rewritten.') from None
        try:
            self.assert_identity(expected)
            with tempfile.TemporaryDirectory(prefix='skua-undo-', dir=self.index.parent) as directory:
                index = Path(directory) / 'index'
                if expected[1] is not None:
                    index.write_bytes(expected[1])
                if parent:
                    self.run('reset', '-q', parent, '--', *self.relative(paths), index=index)
                else:
                    self.run('update-index', '--force-remove', '--', *self.relative(paths), index=index)
                with os.fdopen(os.dup(fd), 'wb') as stream:
                    stream.write(index.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                if parent:
                    self.run('update-ref', '-m', 'skua local undo', 'HEAD', parent, expected[0])
                else:
                    self.run('update-ref', '-d', 'HEAD', expected[0])
                os.replace(lock, self.index)
        finally:
            os.close(fd)
            if lock.exists():
                lock.unlink()
