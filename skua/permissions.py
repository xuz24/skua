"""Per-operation reviews. No approval is reusable or restored from history."""
from dataclasses import dataclass
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

    @classmethod
    def create(cls, action, reasons, targets, summary, evidence):
        digest = hashlib.sha256(json.dumps([action, reasons, targets, summary, evidence],
                                          sort_keys=True, default=str).encode()).hexdigest()
        return cls(action, tuple(sorted(set(reasons))), tuple(targets), summary, digest)


async def deny_review(review):
    return 'denied'
