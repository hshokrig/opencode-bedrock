#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
version="$(python3 -c 'from opencode_bedrock import __version__; print(__version__)')"
upstream="$(tr -d '[:space:]' < "$repo/UPSTREAM_REVISION")"
source_revision="$(git -C "$repo" rev-parse HEAD)"
source_dirty=false
if [[ -n "$(git -C "$repo" status --porcelain --untracked-files=normal)" ]]; then
  if [[ "${ALLOW_DIRTY_BUILD:-0}" != "1" ]]; then
    echo "refusing to build from a dirty source tree; commit the release or set ALLOW_DIRTY_BUILD=1 for local testing" >&2
    exit 1
  fi
  source_dirty=true
fi
release="$version+${source_revision:0:12}"
machine="$(uname -m)"
case "$machine" in
  x86_64) arch="x64" ;;
  aarch64|arm64) arch="arm64" ;;
  *) echo "unsupported architecture: $machine" >&2; exit 1 ;;
esac

output="${1:-$repo/artifacts}"
mkdir -p "$output"

if [[ -n "${OPENCODE_BIN:-}" ]]; then
  if [[ "${ALLOW_UNVERIFIED_OPENCODE_BIN:-0}" != "1" ]]; then
    echo "OPENCODE_BIN bypasses the source build; set ALLOW_UNVERIFIED_OPENCODE_BIN=1 only for packaging tests or a separately attested binary" >&2
    exit 1
  fi
  binary="$(realpath "$OPENCODE_BIN")"
else
  command -v bun >/dev/null 2>&1 || {
    echo "bun is required; run scripts/bootstrap.sh first" >&2
    exit 1
  }
  (
    cd "$repo"
    bun install --frozen-lockfile
    MODELS_DEV_API_JSON="$repo/opencode_bedrock/models-dev-api.json" \
      OPENCODE_VERSION="0.0.0-bedrock.${upstream:0:12}" \
      bun run --cwd packages/opencode build --single --skip-install --skip-embed-web-ui
  )
  binary="$repo/packages/opencode/dist/opencode-linux-${arch}/bin/opencode"
fi

[[ -x "$binary" ]] || {
  echo "OpenCode binary was not produced: $binary" >&2
  exit 1
}

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
name="opencode-bedrock-${release}-linux-${arch}"
root="$stage/$name"
mkdir -p "$root/bin/aws" "$root/share/docs" "$root/share/licenses" "$root/share/policies"
install -m 0755 "$binary" "$root/bin/opencode"
install -m 0755 "$repo/bin/opencode-bedrock" "$root/bin/opencode-bedrock"
install -m 0755 "$repo/scripts/verify-bedrock.sh" "$root/bin/opencode-bedrock-verify-aws"
install -m 0644 "$repo/scripts/aws/bedrock_smoke.py" "$root/bin/aws/bedrock_smoke.py"
cp -R "$repo/opencode_bedrock" "$root/bin/opencode_bedrock"
find "$root/bin/opencode_bedrock" -type d -name __pycache__ -prune -exec rm -rf {} +
cp -R "$repo/docs/." "$root/share/docs/"
install -m 0644 "$repo/policies/sagemaker-bedrock-iam.json" "$root/share/policies/"
install -m 0644 "$repo/LICENSE" "$root/share/licenses/OpenCode-LICENSE"
install -m 0644 "$repo/NOTICE" "$root/share/licenses/NOTICE"
install -m 0755 "$repo/scripts/install-offline.sh" "$root/install.sh"

opencode_version="$("$binary" --version | head -n 1)"
python3 - "$root/manifest.json" "$version" "$release" "$arch" "$upstream" "$source_revision" "$source_dirty" "$opencode_version" <<'PY'
import json
import sys

path, version, release, arch, upstream, source_revision, source_dirty, opencode = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "artifact_version": release,
            "package_version": version,
            "platform": "linux",
            "architecture": arch,
            "upstream_commit": upstream,
            "source_revision": source_revision,
            "source_dirty": source_dirty == "true",
            "opencode_version": opencode,
            "python_minimum": "3.10",
        },
        handle,
        indent=2,
        sort_keys=True,
    )
    handle.write("\n")
PY

(
  cd "$root"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
)

archive="$output/$name.tar.gz"
epoch="$(git -C "$repo" show -s --format=%ct "$upstream")"
tar \
  --sort=name \
  --mtime="@$epoch" \
  --owner=0 \
  --group=0 \
  --numeric-owner \
  -C "$stage" \
  -cf - \
  "$name" |
  gzip -n > "$archive"
installer="$output/install-opencode-bedrock.sh"
install -m 0755 "$repo/scripts/install-offline.sh" "$installer"
(
  cd "$output"
  sha256sum "$(basename "$archive")" > "$(basename "$archive").sha256"
  sha256sum "$(basename "$installer")" > "$(basename "$installer").sha256"
)
echo "$archive"
