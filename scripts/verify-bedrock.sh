#!/usr/bin/env bash
set -euo pipefail

if [[ "${RUN_AWS_SMOKE:-0}" != "1" ]]; then
  echo "This script makes paid Amazon Bedrock calls." >&2
  echo "Set RUN_AWS_SMOKE=1 after setting AWS_REGION and BEDROCK_INFERENCE_PROFILE." >&2
  exit 2
fi
: "${AWS_REGION:?set AWS_REGION}"
: "${BEDROCK_INFERENCE_PROFILE:?set BEDROCK_INFERENCE_PROFILE}"

python3 "$(dirname "$0")/aws/bedrock_smoke.py"
