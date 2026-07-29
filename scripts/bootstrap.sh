#!/usr/bin/env bash
set -euo pipefail

required_bun="1.3.14"

if command -v bun >/dev/null 2>&1; then
  current="$(bun --version)"
  if [[ "$current" == "$required_bun" ]]; then
    echo "bun $current is ready"
    exit 0
  fi
  echo "bun $current is installed; this repository pins $required_bun" >&2
  exit 1
fi

if [[ "${ALLOW_NETWORK_BOOTSTRAP:-0}" != "1" ]]; then
  echo "bun $required_bun is required." >&2
  echo "Set ALLOW_NETWORK_BOOTSTRAP=1 to install it with the official Bun installer." >&2
  exit 1
fi

curl --fail --location --proto '=https' --tlsv1.2 https://bun.sh/install |
  bash -s -- "bun-v${required_bun}"

echo "Restart the shell or add \$HOME/.bun/bin to PATH."
