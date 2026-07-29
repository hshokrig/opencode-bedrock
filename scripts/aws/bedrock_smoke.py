#!/usr/bin/env python3
from __future__ import annotations

import os
import sys


def main() -> int:
    if os.environ.get("RUN_AWS_SMOKE") != "1":
        print("RUN_AWS_SMOKE=1 is required", file=sys.stderr)
        return 2
    try:
        import boto3  # pyright: ignore[reportMissingImports]
    except ImportError:
        print("boto3 is required for this opt-in AWS smoke test", file=sys.stderr)
        return 2

    region = required("AWS_REGION")
    profile = required("BEDROCK_INFERENCE_PROFILE")
    print("caller identity:")
    print(boto3.client("sts", region_name=region).get_caller_identity())

    print("inference profile:")
    control = boto3.client(
        "bedrock",
        region_name=region,
        endpoint_url=os.environ.get("BEDROCK_CONTROL_ENDPOINT"),
    )
    print(control.get_inference_profile(inferenceProfileIdentifier=profile))

    runtime = boto3.client(
        "bedrock-runtime",
        region_name=region,
        endpoint_url=os.environ.get("BEDROCK_RUNTIME_ENDPOINT"),
    )
    messages = [{"role": "user", "content": [{"text": "Reply with exactly: bedrock-ok"}]}]
    print("converse:")
    response = runtime.converse(
        modelId=profile,
        messages=messages,
        inferenceConfig={"maxTokens": 32, "temperature": 0},
    )
    print(response["output"]["message"]["content"])

    print("converse_stream:")
    stream = runtime.converse_stream(
        modelId=profile,
        messages=messages,
        inferenceConfig={"maxTokens": 32, "temperature": 0},
    )
    for event in stream["stream"]:
        delta = event.get("contentBlockDelta", {}).get("delta", {}).get("text")
        if delta:
            print(delta, end="", flush=True)
    print()
    return 0


def required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"set {name}")
    return value


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
