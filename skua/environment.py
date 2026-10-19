"""Resolve local shells and build fresh, non-login command invocations."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
from typing import Mapping

from .config import Config, ConfigError, command_environment


def detect_shell(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    candidates = [env.get('SHELL'), shutil.which('zsh', path=env.get('PATH', os.defpath)),
                  shutil.which('bash', path=env.get('PATH', os.defpath))]
    for candidate in candidates:
        if candidate and Path(candidate).name in {'bash', 'zsh'} and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).absolute())
    return None


@dataclass(frozen=True)
class Candidate:
    kind: str
    path: Path


def discover_environments(root: Path, environ: Mapping[str, str] | None = None) -> list[Candidate]:
    """Read local candidates only; never invoke activation or create environments."""
    env = os.environ if environ is None else environ
    paths = [('venv', root / '.venv'), ('venv', root / 'venv')]
    for key, kind in [('VIRTUAL_ENV', 'venv'), ('CONDA_PREFIX', 'conda')]:
        if env.get(key):
            paths.append((kind, Path(env[key])))
    try:
        paths.extend(('conda', Path(line.strip())) for line in
                     (Path.home() / '.conda/environments.txt').read_text().splitlines() if line.strip())
    except (OSError, UnicodeError):
        pass
    result = []
    seen = set()
    for kind, path in paths:
        try:
            path = path.expanduser().resolve()
            marker = path / ('pyvenv.cfg' if kind == 'venv' else 'conda-meta')
            if marker.exists() and path not in seen:
                result.append(Candidate(kind, path))
                seen.add(path)
        except (OSError, RuntimeError):
            continue
    return result


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    env: dict[str, str]
    root: Path
    shell: Path


def prepare_command(config: Config, command: str, environ: Mapping[str, str] | None = None) -> Invocation:
    if not isinstance(command, str) or '\x00' in command:
        raise ConfigError('command: expected text without NUL')
    env = command_environment(config, environ)
    shell = config.shell.executable
    if shell is None:
        detected = detect_shell(env)
        if detected is None:
            raise ConfigError('shell: no executable Bash or Zsh found')
        shell = Path(detected)
    if shell.name not in {'bash', 'zsh'} or not shell.is_file() or not os.access(shell, os.X_OK):
        raise ConfigError('shell.executable: selected Bash/Zsh executable is unavailable')
    if config.shell.startup_enabled:
        flags = ['-i', '-c']
    elif shell.name == 'bash':
        flags = ['--noprofile', '--norc', '-c']
        # Bash reads this even when noninteractive and --norc was supplied.
        env.pop('BASH_ENV', None)
        env.pop('ENV', None)
    else:
        flags = ['-f', '-c']  # /etc/zshenv is still unavoidable.
    quote = shlex.quote
    setup = []
    selected = config.environment
    if selected.kind == 'venv':
        path = selected.path
        if (path is None or not (path / 'pyvenv.cfg').is_file()
                or not (path / 'bin/activate').is_file()
                or not os.access(path / 'bin/python', os.X_OK)):
            raise ConfigError('environment.path: selected virtualenv is unavailable')
        setup.append(f'. {quote(str(path / "bin/activate"))} || exit 125')
    elif selected.kind == 'conda':
        conda = env.get('CONDA_EXE') or shutil.which('conda', path=env.get('PATH', os.defpath))
        if not conda or not Path(conda).is_file() or not os.access(conda, os.X_OK):
            raise ConfigError('environment: Conda executable is unavailable (CONDA_EXE or PATH)')
        selector = str(selected.path) if selected.path else selected.name
        if not selector or (selected.path is not None and not (selected.path / 'conda-meta').is_dir()):
            raise ConfigError('environment: selected Conda environment is unavailable')
        if selector.startswith('-'):
            raise ConfigError('environment.name: Conda names cannot begin with a hyphen')
        # Capture only the hook; hook/activation failures must not run the command.
        setup.extend([
            f'_skua_conda_hook=$({quote(str(Path(conda).absolute()))} shell.{shell.name} hook) || exit 125',
            'eval "$_skua_conda_hook" || exit 125',
            'unset _skua_conda_hook',
            f'conda activate {quote(selector)} || exit 125',
        ])
    # Startup/activation may cd. Every requested command still starts at the root.
    setup.append(f'cd -- {quote(str(config.root))} || exit 125')
    # eval keeps the command separate from activation syntax, including comments.
    setup.append(f'eval {quote(command)}')
    return Invocation(tuple([str(shell), *flags, '\n'.join(setup)]), env, config.root, shell)
