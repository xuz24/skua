"""Explicit Git bootstrap profiles for pinned benchmark environments."""
from typing import Literal

GitBootstrap = Literal['default', 'bullseye-20251020']


def git_bootstrap_command(profile: GitBootstrap = 'default'):
    if profile not in ('default', 'bullseye-20251020'):
        raise ValueError('Unknown Git bootstrap profile')
    apt = 'apt-get update\nDEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git ca-certificates'
    if profile == 'bullseye-20251020':
        apt = r'''(
set -eu
. /etc/os-release
test "$ID" = debian && test "$VERSION_ID" = 11
skua_apt_dir=$(mktemp -d /tmp/skua-git-apt.XXXXXX)
trap 'rm -rf "$skua_apt_dir"' EXIT
chmod 755 "$skua_apt_dir"
mkdir -p "$skua_apt_dir/lists/partial"
printf '%s\n' \
  'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20251020T000000Z bullseye main' \
  'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20251020T000000Z bullseye-security main' \
  > "$skua_apt_dir/sources.list"
skua_apt() {
  apt-get -o Dir::Etc::sourcelist="$skua_apt_dir/sources.list" \
    -o Dir::Etc::sourceparts=- -o Dir::State::lists="$skua_apt_dir/lists" \
    -o APT::Update::Error-Mode=any -o Acquire::Retries=2 \
    -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 "$@"
}
skua_apt update
DEBIAN_FRONTEND=noninteractive skua_apt install -y --no-install-recommends git ca-certificates
)'''
    return ('set -eu\nif ! command -v git >/dev/null 2>&1; then\n'
            'if command -v apt-get >/dev/null 2>&1; then\n' + apt + '\n'
            'elif command -v apk >/dev/null 2>&1; then apk add --no-cache git ca-certificates;\n'
            'else echo "Skua requires Git; unsupported package manager" >&2; exit 1; fi; fi\n'
            'git --version && command -v bash && command -v sha256sum')


QEMU_RUNTIME_CHECK = r'''set -eu
command -v qemu-system-x86_64 >/dev/null
command -v timeout >/dev/null
skua_qemu_log=$(mktemp /tmp/skua-qemu-check.XXXXXX)
trap 'rm -f "$skua_qemu_log"' EXIT
skua_qemu_code=0
timeout --signal=TERM --kill-after=2 2 qemu-system-x86_64 \
  -M none -display none -serial null -monitor null >"$skua_qemu_log" 2>&1 || skua_qemu_code=$?
if [ "$skua_qemu_code" != 124 ]; then
  cat "$skua_qemu_log" >&2
  echo "QEMU runtime check failed before model execution (exit $skua_qemu_code). Check host amd64 emulation; Rosetta syscall failures require a compatible backend." >&2
  exit 1
fi
echo 'QEMU runtime check passed'
'''
