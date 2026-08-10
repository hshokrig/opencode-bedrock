#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 ARTIFACT.tar.gz [PREFIX]" >&2
  exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
archive="$(realpath "$1")"
prefix="${2:-$HOME/.local/opencode-bedrock}"
[[ -f "$archive" ]] || {
  echo "artifact not found: $archive" >&2
  exit 1
}

checksum="$archive.sha256"
[[ -f "$checksum" ]] || {
  echo "checksum file not found: $checksum" >&2
  exit 1
}
expected="$(awk 'NR == 1 { print $1 }' "$checksum")"
[[ "$expected" =~ ^[0-9a-fA-F]{64}$ ]] || {
  echo "invalid checksum file: $checksum" >&2
  exit 1
}
actual="$(sha256sum "$archive")"
actual="${actual%% *}"
[[ "$actual" == "$expected" ]] || {
  echo "archive checksum mismatch: $archive" >&2
  exit 1
}
echo "$archive: OK"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
python3 - "$archive" "$stage" <<'PY'
import pathlib
import sys
import tarfile

archive, destination = sys.argv[1:]
with tarfile.open(archive, "r:gz") as bundle:
    members = bundle.getmembers()
    roots = set()
    names = set()
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if (
            not path.parts
            or path.is_absolute()
            or ".." in path.parts
            or "\n" in member.name
            or member.name in names
            or not (member.isfile() or member.isdir())
        ):
            raise SystemExit(f"artifact contains an unsafe member: {member.name!r}")
        names.add(member.name)
        roots.add(path.parts[0])
    if len(roots) != 1:
        raise SystemExit("artifact must contain exactly one root directory")
    bundle.extractall(destination)
PY
root="$(find "$stage" -mindepth 1 -maxdepth 1 -type d -name 'opencode-bedrock-*' -print -quit)"
[[ -n "$root" ]] || {
  echo "artifact root not found" >&2
  exit 1
}
python3 - "$root" <<'PY'
import hashlib
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1]).resolve()
checksum_file = root / "SHA256SUMS"
entries = {}
for line in checksum_file.read_text(encoding="utf-8").splitlines():
    match = re.fullmatch(r"([0-9a-f]{64})  (\./.+)", line)
    if not match:
        raise SystemExit(f"invalid inner checksum line: {line!r}")
    path = (root / match.group(2)).resolve()
    if root not in path.parents or path in entries:
        raise SystemExit(f"unsafe inner checksum path: {match.group(2)!r}")
    entries[path] = match.group(1)

files = {path.resolve() for path in root.rglob("*") if path.is_file() and path != checksum_file}
if files != set(entries):
    raise SystemExit("inner checksum file list does not match artifact contents")
for path, expected in entries.items():
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit(f"inner checksum mismatch: {path.relative_to(root)}")
print("inner checksums: OK")
PY

read -r version platform architecture python_minimum source_dirty < <(
  python3 -c 'import json,sys; data=json.load(open(sys.argv[1])); print(data["artifact_version"], data["platform"], data["architecture"], data["python_minimum"], str(data["source_dirty"]).lower())' "$root/manifest.json"
)
[[ "$version" =~ ^[0-9A-Za-z][0-9A-Za-z.+-]{0,95}$ ]] || {
  echo "invalid artifact version: $version" >&2
  exit 1
}
[[ "$source_dirty" == "false" || "${ALLOW_DIRTY_INSTALL:-0}" == "1" ]] || {
  echo "refusing to install an artifact built from a dirty source tree" >&2
  exit 1
}
python3 - "$python_minimum" <<'PY'
import sys

minimum = tuple(int(part) for part in sys.argv[1].split("."))
if sys.version_info[: len(minimum)] < minimum:
    raise SystemExit(f"Python {sys.argv[1]} or newer is required")
PY
machine="$(uname -m)"
case "$machine" in
  x86_64) target_architecture="x64" ;;
  aarch64|arm64) target_architecture="arm64" ;;
  *) echo "unsupported target architecture: $machine" >&2; exit 1 ;;
esac
[[ "$platform" == "linux" && "$architecture" == "$target_architecture" ]] || {
  echo "artifact target mismatch: artifact=$platform/$architecture target=linux/$target_architecture" >&2
  exit 1
}
mkdir -p "$prefix" "$HOME/.local/bin"
prefix="$(realpath "$prefix")"
destination="$prefix/$version"
[[ ! -e "$destination" && ! -L "$destination" ]] || {
  echo "version is already installed: $destination" >&2
  exit 1
}
[[ ! -e "$prefix/current" || -L "$prefix/current" ]] || {
  echo "refusing to replace non-symlink: $prefix/current" >&2
  exit 1
}

for name in opencode opencode-bedrock opencode-bedrock-verify-aws; do
  link="$HOME/.local/bin/$name"
  if [[ -e "$link" && ! -L "$link" ]]; then
    echo "refusing to replace non-symlink: $link" >&2
    exit 1
  fi
done

mv "$root" "$destination"
current="$prefix/current"
temporary_current="$prefix/.current.$$"
ln -s "$destination" "$temporary_current"
mv -T "$temporary_current" "$current"
for name in opencode opencode-bedrock opencode-bedrock-verify-aws; do
  link="$HOME/.local/bin/$name"
  temporary="$link.opencode-bedrock.$$"
  ln -s "$current/bin/$name" "$temporary"
  mv -T "$temporary" "$link"
done

echo "installed $destination"
echo "add $HOME/.local/bin to PATH if it is not already present"
