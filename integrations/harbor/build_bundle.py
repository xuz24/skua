"""Build an immutable-by-hash Skua wheel bundle from this checkout (no clone)."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--python-archive', type=Path, help='Optional trusted standalone Linux amd64 Python 3.12 install_only archive')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                    '--wheel-dir', str(output), str(repo)], check=True)
    wheel, = output.glob('skua-*.whl')
    requirements = output / 'requirements.txt'
    shutil.copyfile(repo / 'requirements.txt', requirements)
    manifest = {'version': 1, 'files': {key: {'name': path.name,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                for key, path in [('wheel', wheel), ('requirements', requirements)]}}
    if args.python_archive:
        archive = output / args.python_archive.name
        shutil.copyfile(args.python_archive, archive)
        manifest['files']['python'] = {'name': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Bundle pinned by SHA-256:', output)


if __name__ == '__main__':
    main()
