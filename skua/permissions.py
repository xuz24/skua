"""Explicit reviews and opt-in, in-memory session grants."""
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import hashlib
import json
import os
from pathlib import Path
import stat


class AccessError(Exception):
    pass


def glob_match(path: str, pattern: str) -> bool:
    parts, rules = path.split('/'), pattern.split('/')
    def match(i, j):
        if j == len(rules):
            return i == len(parts)
        if rules[j] == '**':
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatchcase(parts[i], rules[j]) and match(i + 1, j + 1)
    return match(0, 0)


def lexical(root, value):
    return Path(os.path.abspath(root / Path(value).expanduser()))


def sensitive(config, path):
    builtins = ('.env', '.env.*', '.netrc', '.npmrc', '.pypirc', '.git-credentials',
                'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', '*.key', '*.p12', '*.pfx')
    for candidate in (path, path.resolve()):
        if any(fnmatchcase(part, rule) for part in candidate.parts for rule in builtins):
            return True
        if any(part in config.sensitive.filenames for part in candidate.parts):
            return True
        if any('/'.join(candidate.parts[i:i+2]) in {'.aws/credentials', '.kube/config'}
               for i in range(len(candidate.parts) - 1)):
            return True
        if candidate.is_relative_to(config.root):
            if any(glob_match(candidate.relative_to(config.root).as_posix(), p)
                   for p in config.sensitive.patterns):
                return True
    return False


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def open_resolved(path, *, directory=False):
    """Open an already-resolved absolute path without following replacement symlinks."""
    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if directory:
            flags |= os.O_DIRECTORY
        return os.open(path.name or '.', flags, dir_fd=parent)
    finally:
        os.close(parent)


@dataclass(frozen=True)
class Target:
    path: Path
    resolved: Path
    identity: tuple

    @classmethod
    def inspect(cls, path):
        try:
            resolved = path.resolve(strict=True)
            info = resolved.stat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise AccessError('Only regular files and directories are supported.')
            return cls(path, resolved, stamp(info))
        except (OSError, RuntimeError):
            raise AccessError('Path is unavailable or cannot be resolved.') from None

    def check(self):
        if Target.inspect(self.path) != self:
            raise AccessError('Target changed since review; request a new operation.')

    def read(self, limit=1_000_000):
        self.check()
        if not stat.S_ISREG(self.identity[2]):
            raise AccessError('Expected a regular text file.')
        if self.identity[3] > limit:
            raise AccessError(f'File exceeds the {limit}-byte text read limit.')
        try:
            fd = open_resolved(self.resolved)
            with os.fdopen(fd, 'rb') as handle:
                if stamp(os.fstat(handle.fileno())) != self.identity:
                    raise AccessError('Target changed since review.')
                raw = handle.read(limit + 1)
                if stamp(os.fstat(handle.fileno())) != self.identity:
                    raise AccessError('File changed during read.')
            self.check()
            if b'\0' in raw or len(raw) > limit:
                raise AccessError('Only bounded UTF-8 text files are supported.')
            return raw.decode('utf-8')
        except (OSError, UnicodeError):
            raise AccessError('File is unavailable or is not UTF-8 text.') from None


@dataclass(frozen=True)
class Review:
    action: str
    reasons: tuple[str, ...]
    targets: tuple[str, ...]
    summary: str
    review_sha256: str
    policy_paths: tuple[str, ...] = ()
    command: str | None = None
    invocation: object = field(default=None, repr=False)

    @classmethod
    def create(cls, action, reasons, targets, summary, evidence, *, policy_paths=(), command=None, invocation=None):
        digest = hashlib.sha256(json.dumps([action, reasons, targets, summary, evidence],
                                          sort_keys=True, default=str).encode()).hexdigest()
        return cls(action, tuple(sorted(set(reasons))), tuple(targets), summary, digest,
                   tuple(str(p) for p in policy_paths), command, invocation)


async def deny_review(review):
    return 'denied'


@dataclass(frozen=True)
class Grant:
    id: str
    action: str
    root: Path
    scope: Path
    anchors: tuple
    command: str | None = None
    invocation: object = field(default=None, repr=False)

    @property
    def description(self):
        if self.action == 'patch':
            return f'{self.id}: patch {self.scope} (recursive create/replace/delete/rename)'
        return f'{self.id}: command in {self.root}: {self.command!r} (exact string and execution environment)'


def directory_identity(path):
    # Contents may evolve under a grant; replacing its directory invalidates it.
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or path.resolve() != path:
        raise AccessError('Grant directory must be an existing directory without symlink aliases.')
    return info.st_dev, info.st_ino, info.st_mode


class SessionPolicy:
    """No history/config restoration. Only the terminal's explicit grant control adds grants."""
    def __init__(self):
        self.grants = {}
        self._next = 1

    def prepare(self, config, action, value):
        from .environment import prepare_command
        root = config.root
        if action == 'patch':
            try:
                scope = lexical(root, value)
                if (not scope.is_relative_to(root) or sensitive(config, scope) or '.git' in scope.parts
                        or scope.is_relative_to(root / '.skua/.tmp')):
                    raise AccessError('Patch grants require a nonsensitive project directory outside Git/recovery storage.')
            except (OSError, RuntimeError, ValueError):
                raise AccessError('Grant directory is unavailable.') from None
            command = invocation = None
        elif action == 'command':
            scope = root
            if not value.strip() or len(value) > 100_000 or '\0' in value:
                raise AccessError('Expected a nonempty command of at most 100000 characters without NUL.')
            command = value
            invocation = prepare_command(config, command)
        else:
            raise AccessError('Grant actions are patch or command.')
        try:
            anchors = (directory_identity(root), directory_identity(scope))
        except (OSError, RuntimeError):
            raise AccessError('Grant directory is unavailable.') from None
        return Grant(str(self._next), action, root, scope, anchors, command, invocation)

    def current(self, grant, config):
        try:
            return (grant.root == config.root and
                    grant.anchors == (directory_identity(grant.root), directory_identity(grant.scope)))
        except (AccessError, OSError, RuntimeError):
            return False

    def add(self, grant, config):
        from .environment import prepare_command
        if not self.current(grant, config) or (grant.command is not None and
                prepare_command(config, grant.command) != grant.invocation):
            raise AccessError('Grant scope or environment changed during confirmation; request it again.')
        if grant.id != str(self._next):
            raise AccessError('Grant changed during confirmation; request it again.')
        self.grants[grant.id] = grant
        self._next += 1

    def revoke(self, identifier):
        if identifier == 'all':
            self.grants.clear()
            return True
        return self.grants.pop(identifier, None) is not None

    def match(self, config, review):
        if review.action not in {'patch', 'command'} or set(review.reasons) & {'sensitive', 'outside_root', 'network'}:
            return None
        if review.action == 'patch' and not review.policy_paths:
            return None
        paths = [Path(p) for p in review.policy_paths]
        # Never use a resolved-only review to silently authorize a symlink alias.
        try:
            if any(not p.is_absolute() or not p.is_relative_to(config.root) or p.resolve() != p
                   or sensitive(config, p) or '.git' in p.parts
                   or p.is_relative_to(config.root / '.skua/.tmp') for p in paths):
                return None
            for grant in self.grants.values():
                if grant.action != review.action or not self.current(grant, config):
                    continue
                if grant.action == 'patch' and all(p.is_relative_to(grant.scope) for p in paths):
                    return grant
                if (grant.action == 'command' and review.command == grant.command
                        and review.invocation == grant.invocation):
                    return grant
        except (OSError, RuntimeError):
            return None
        return None
