"""Bounded, symlink-safe discovery using Git's ignore matcher, also outside repos."""
from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import subprocess
import tempfile

from .permissions import AccessError, Target, open_resolved

MAX_ENTRIES = 20_000


@contextmanager
def ignore_matcher(root):
    # Isolated metadata prevents project/global Git config from affecting discovery.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    try:
        with tempfile.TemporaryDirectory(prefix='skua-ignore-') as directory:
            subprocess.run(['git', 'init', '--bare', '--template=', directory], env=env,
                           capture_output=True, check=True, timeout=10)
            def ignored(paths):
                if not paths:
                    return set()
                relative = [p.relative_to(root).as_posix() for p in paths]
                result = subprocess.run(['git', '--git-dir=' + directory, '--work-tree=' + str(root),
                                         '-c', 'core.excludesFile=' + os.devnull,
                                         'check-ignore', '--no-index', '-z', '--stdin'],
                                        input=('\0'.join(relative) + '\0').encode(),
                                        capture_output=True, env=env, timeout=10)
                if result.returncode not in {0, 1}:
                    raise AccessError('Could not evaluate ignore rules.')
                return {root / os.fsdecode(p) for p in result.stdout.split(b'\0') if p}
            yield ignored
    except (OSError, subprocess.SubprocessError):
        raise AccessError('File discovery requires Git and readable ignore rules.') from None


def discover(root: Path, *, include_ignored=False, start=None):
    """Never follow directory symlinks. File symlinks retain their lexical path."""
    result = []
    pending = [start or root]
    seen = 0
    matcher = nullcontext(lambda paths: set()) if include_ignored else ignore_matcher(root)
    with matcher as ignored:
        while pending:
            directory = pending.pop()
            try:
                target = Target.inspect(directory)
                if target.resolved != directory or not directory.is_relative_to(root):
                    raise AccessError('Discovery directory changed or escaped its root.')
                fd = open_resolved(directory, directory=True)
                try:
                    children = sorted(directory / name for name in os.listdir(fd))
                finally:
                    os.close(fd)
                target.check()
                seen += len(children)
                if seen > MAX_ENTRIES:
                    raise AccessError(f'Discovery exceeds {MAX_ENTRIES} entries; select a smaller directory.')
                excluded = set() if include_ignored else ignored(children)
                for path in children:
                    if path.name == '.git' or path in excluded:
                        continue
                    if path.is_symlink():
                        result.append(path)
                    elif path.is_dir():
                        pending.append(path)
                    elif path.is_file():
                        result.append(path)
            except OSError:
                raise AccessError('Directory could not be listed.') from None
    return sorted(result)
