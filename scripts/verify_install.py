"""Build a wheel, install pinned runtime dependencies in a fresh venv, then validate it.

Run with the development interpreter (setuptools installed). --python selects the
fresh environment's interpreter. --wheelhouse makes installation strictly offline.
All build/install artifacts are scoped to a disposable temporary directory.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def run(args, *, cwd):
    subprocess.run([str(arg) for arg in args], cwd=cwd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--wheelhouse', type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    wheelhouse = args.wheelhouse.resolve() if args.wheelhouse else None
    with tempfile.TemporaryDirectory(prefix='skua-install-') as directory:
        base = Path(directory)
        wheels = base / 'wheels'
        wheels.mkdir()
        run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
             '--wheel-dir', wheels, repo], cwd=base)
        wheel, = wheels.glob('skua-*.whl')
        run([args.python, '-m', 'venv', base / 'venv'], cwd=base)
        python = base / 'venv/bin/python'
        sources = ['--no-index', '--find-links', str(wheelhouse)] if wheelhouse else []
        run([python, '-m', 'pip', 'install', *sources, '-r', repo / 'requirements.txt'], cwd=base)
        run([python, '-m', 'pip', 'install', '--no-index', '--no-deps', wheel], cwd=base)
        run([python, '-m', 'pip', 'check'], cwd=base)
        run([python, '-m', 'skua', '--help'], cwd=base)
        run([base / 'venv/bin/skua', '--version'], cwd=base)
        # Copy the smoke script so neither its import path nor cwd is the checkout.
        smoke = base / 'installed_smoke.py'
        shutil.copyfile(repo / 'scripts/installed_smoke.py', smoke)
        for shell in ('/bin/bash', '/bin/zsh'):
            if not Path(shell).exists():
                continue
            for mode in ([], ['--nonstreaming']):
                run([python, smoke, '--shell', shell, *mode], cwd=base)
        print('Fresh pinned installation and all available shell/streaming combinations passed.')


if __name__ == '__main__':
    main()
