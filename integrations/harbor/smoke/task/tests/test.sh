#!/bin/bash
set -eu
mkdir -p /logs/verifier
python3 - <<'PY'
from pathlib import Path
p = Path('/workspace/answer.txt')
passed = p.is_file() and p.read_text() == 'harbor-ready\n'
external = Path('/tmp/skua-fixture.txt')
passed = passed and external.is_file() and external.read_text() == 'external-ready\n'
Path('/logs/verifier/reward.txt').write_text('1' if passed else '0')
PY
