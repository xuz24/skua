"""Reviewed text patches with durable, conditional filesystem recovery."""
from dataclasses import dataclass, replace
import base64
import difflib
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from .permissions import AccessError, Target, lexical, open_resolved

MAX_PATCH = 5_000_000


def text_bytes(value):
    if not isinstance(value, str) or '\0' in value:
        raise AccessError('Patch contents must be UTF-8 text without NUL.')
    try:
        raw = value.encode('utf-8')
    except UnicodeError:
        raise AccessError('Patch contents must be UTF-8 text.') from None
    if len(raw) > 1_000_000:
        raise AccessError('Each patch file is limited to 1000000 bytes.')
    return raw


def validate_changes(changes):
    if not isinstance(changes, list) or not 1 <= len(changes) <= 100:
        return 'changes must contain 1 to 100 file operations.'
    total = 0
    for item in changes:
        if not isinstance(item, dict) or item.get('op') not in {'write', 'delete', 'rename'}:
            return 'Each operation must be write, delete, or rename.'
        required = {'op', 'path'} | ({'content'} if item['op'] == 'write' else {'destination'} if item['op'] == 'rename' else set())
        allowed = required | ({'content'} if item['op'] == 'rename' else set())
        if not required <= item.keys() or item.keys() - allowed:
            return 'Missing or unknown patch operation fields.'
        for key in ('path', 'destination'):
            if key in item and (not isinstance(item[key], str) or not item[key] or '\0' in item[key] or len(item[key]) > 4096):
                return 'Patch paths must be nonempty strings, at most 4096 characters.'
        if 'content' in item:
            try:
                total += len(text_bytes(item['content']))
            except AccessError as exc:
                return str(exc)
    if total > MAX_PATCH:
        return 'Patch content exceeds 5000000 bytes.'
    return None


@dataclass(frozen=True)
class FileState:
    requested: Path
    path: Path
    target: Target | None
    content: bytes | None
    mode: int
    mtime_ns: int | None

    @classmethod
    def inspect(cls, requested, *, read=False):
        resolved = requested.resolve()
        if '.git' in requested.parts or '.git' in resolved.parts:
            raise AccessError('Git metadata must not be changed through patch tools.')
        if requested.is_symlink() and not requested.exists():
            raise AccessError('Dangling symlink targets are unsupported.')
        if requested.exists():
            target = Target.inspect(requested)
            if not stat.S_ISREG(target.identity[2]):
                raise AccessError('Patch targets must be regular text files.')
            content = target.read().encode() if read else None
            return cls(requested, resolved, target, content, stat.S_IMODE(target.identity[2]), target.identity[4])
        return cls(requested, resolved, None, None, 0o644, None)

    def check(self):
        if self.requested.resolve() != self.path:
            raise AccessError('Patch path changed since review.')
        if self.target:
            self.target.check()
        elif self.path.exists() or self.path.is_symlink():
            raise AccessError('Patch destination appeared since review.')


@dataclass
class Patch:
    before: dict[Path, FileState]
    after: dict[Path, bytes | None]
    modes: dict[Path, int]
    renames: list[tuple[Path, Path]]

    def check(self):
        for item in self.before.values():
            item.check()

    def diff(self):
        result = [f'Rename: {a} -> {b}\n' for a, b in self.renames]
        for path, before in self.before.items():
            old = (before.content or b'').decode()
            new = (self.after[path] or b'').decode()
            result.append(f'Path: {path}\n')
            lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=str(path) if before.content is not None else '/dev/null',
                                        tofile=str(path) if self.after[path] is not None else '/dev/null')
            for line in lines:
                result.append(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
            if old == new and (before.content is None) != (self.after[path] is None):
                result.append('[Empty file created/deleted]\n')
        return ''.join(result)


def requested_paths(root, changes):
    return [lexical(root, item[key]) for item in changes for key in ('path', 'destination') if key in item]


def prepare(root, changes, reviewed=None):
    before = {}
    states = reviewed or [FileState.inspect(p) for p in requested_paths(root, changes)]
    for item in states:
        item.check()
        if item.target:
            item = replace(item, content=item.target.read().encode())
        if item.path in before:
            raise AccessError('Each resolved patch path may appear only once; split overlapping operations.')
        before[item.path] = item
    after = {}
    modes = {}
    renames = []
    for item in changes:
        path = lexical(root, item['path']).resolve()
        old = before[path]
        if item['op'] in {'delete', 'rename'} and old.target is None:
            raise AccessError('Delete/rename source does not exist.')
        modes[path] = old.mode
        if item['op'] == 'write':
            after[path] = text_bytes(item['content'])
        else:
            after[path] = None
        if item['op'] == 'rename':
            destination = lexical(root, item['destination']).resolve()
            after[destination] = text_bytes(item['content']) if 'content' in item else old.content
            modes[destination] = old.mode
            renames.append((path, destination))
    patch = Patch(before, after, modes, renames)
    if (sum(len(f.content or b'') for f in before.values()) > MAX_PATCH or
            sum(len(value or b'') for value in after.values()) > MAX_PATCH):
        raise AccessError('Original or resulting patch contents exceed 5000000 bytes.')
    patch.check()
    return patch


def ensure_parent(path):
    """Create missing parents, rejecting symlinks in every resolved component."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_file(path, content, mode=0o644, *, mtime_ns=None):
    fd = ensure_parent(path)
    name = '.skua-write-' + uuid4().hex
    created = False
    try:
        if content is None:
            os.unlink(path.name, dir_fd=fd)
        else:
            out = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode, dir_fd=fd)
            created = True
            with os.fdopen(out, 'wb') as stream:
                stream.write(content)
                os.fchmod(stream.fileno(), mode)
                if mtime_ns is not None:
                    os.utime(stream.fileno(), ns=(mtime_ns, mtime_ns))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path.name, src_dir_fd=fd, dst_dir_fd=fd)
            created = False
        os.fsync(fd)
    finally:
        if created:
            os.unlink(name, dir_fd=fd)
        os.close(fd)


def pack(data):
    return None if data is None else base64.b64encode(data).decode('ascii')


def unpack(data):
    if data is None:
        return None
    raw = base64.b64decode(data, validate=True)
    text_bytes(raw.decode('utf-8'))
    return raw


def recovery_dir(root):
    path = root / '.skua/.tmp'
    if path.resolve() != path:
        raise AccessError('Recovery directory must not traverse symlinks.')
    return path


def save_snapshot(root, patch):
    path = recovery_dir(root) / ('patch-' + uuid4().hex + '.json')
    directories = set()
    for target in patch.before:
        parent = target.parent
        while not parent.exists():
            directories.add(str(parent))
            parent = parent.parent
    document = {'version': 1, 'root': str(root), 'directories': sorted(directories), 'files': [
        {'path': str(p), 'before': pack(f.content), 'after': pack(patch.after[p]),
         'mode': f.mode, 'after_mode': patch.modes[p], 'mtime_ns': f.mtime_ns}
        for p, f in patch.before.items()]}
    write_file(path, json.dumps(document).encode(), 0o600)
    return path


def load_snapshot(root, path):
    if path.parent != recovery_dir(root) or path.is_symlink():
        raise AccessError('Invalid recovery snapshot location.')
    raw = Target.inspect(path).read(15_000_000)
    try:
        document = json.loads(raw)
        if set(document) != {'version', 'root', 'directories', 'files'} or document['version'] != 1 or document['root'] != str(root):
            raise ValueError
        if not isinstance(document['files'], list) or not 1 <= len(document['files']) <= 200:
            raise ValueError
        seen = set()
        for item in document['files']:
            if set(item) != {'path', 'before', 'after', 'mode', 'after_mode', 'mtime_ns'}:
                raise ValueError
            p = Path(item['path'])
            if not p.is_absolute() or '..' in p.parts or '.git' in p.parts or p in seen:
                raise ValueError
            seen.add(p)
            for key in ('before', 'after'):
                unpack(item[key])
            if any(type(item[k]) is not int or not 0 <= item[k] <= 0o7777 for k in ('mode', 'after_mode')):
                raise ValueError
            if item['mtime_ns'] is not None and type(item['mtime_ns']) is not int:
                raise ValueError
        if not isinstance(document['directories'], list):
            raise ValueError
        for directory in document['directories']:
            d = Path(directory)
            if not d.is_absolute() or '..' in d.parts or not any(d in p.parents for p in seen):
                raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise AccessError('Recovery snapshot is invalid; left untouched.') from None
    return document


def recover(root, path):
    document = load_snapshot(root, path)
    unresolved = []
    for item in reversed(document['files']):
        p = Path(item['path'])
        try:
            current = FileState.inspect(p, read=True)
            if current.path != p:
                raise AccessError('Recovery target changed.')
            old, new = unpack(item['before']), unpack(item['after'])
            if current.content == old and (old is None or current.mode == item['mode']):
                continue
            if current.content != new or (new is not None and current.mode != item['after_mode']):
                raise AccessError('Newer edits prevent restoration.')
            current.check()
            write_file(p, old, item['mode'], mtime_ns=item['mtime_ns'])
        except (AccessError, OSError):
            unresolved.append(str(p))
    for directory in sorted(document['directories'], key=len, reverse=True):
        d = Path(directory)
        try:
            if d.resolve() == d:
                d.rmdir()  # Only empty directories; never remove contents.
        except OSError:
            pass
    if not unresolved:
        path.unlink()
    return unresolved


def apply(root, patch):
    patch.check()
    snapshot = save_snapshot(root, patch)  # Durable before the first target mutation.
    try:
        patch.check()
        for path, before in patch.before.items():
            before.check()
            if before.content != patch.after[path] or (patch.after[path] is not None and before.mode != patch.modes[path]):
                write_file(path, patch.after[path], patch.modes[path])
    except (OSError, AccessError):
        unresolved = recover(root, snapshot)
        return False, unresolved, snapshot if unresolved else None
    snapshot.unlink()
    return True, [], None
