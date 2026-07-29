from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import BedrockError

SAFE_BASH = [
    "pwd",
    "git status",
    "git status --short",
    "git status --porcelain",
    "git diff --no-ext-diff --no-textconv",
    "git diff --no-ext-diff --no-textconv --stat",
    "git diff --cached --no-ext-diff --no-textconv",
    "git diff --cached --no-ext-diff --no-textconv --stat",
    "git branch --show-current",
]
AGENTS = {"plan", "build", "explore", "implement", "review", "test"}


def opencode_config(
    region: str,
    inference_profile: str,
    endpoint: str | None,
    headless_policy: str,
    workspace: Path | None = None,
    agent_models: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not region.strip():
        raise BedrockError("AWS region is required")
    if not inference_profile.strip():
        raise BedrockError("Bedrock inference-profile ID or ARN is required")
    if headless_policy not in {"approval", "workspace-write"}:
        raise BedrockError(f"unsupported headless policy: {headless_policy}")

    edit = "ask" if headless_policy == "approval" else "allow"
    bash = {"*": "ask", **{pattern: "allow" for pattern in SAFE_BASH}}
    options: dict[str, Any] = {"region": region}
    if endpoint:
        options["endpoint"] = endpoint

    models = {
        "opus": {
            "id": inference_profile,
            "name": "Claude Opus through Amazon Bedrock",
        }
    }
    agents: dict[str, dict[str, Any]] = {
        "implement": {
            "mode": "subagent",
            "description": "Make one focused, authorized change inside the active workspace.",
            "prompt": (
                "Work only in the active workspace. Implement the assigned change with "
                "the smallest maintainable diff. Respect permission prompts, report "
                "files changed, and do not claim tests you did not run."
            ),
        },
        "review": {
            "mode": "subagent",
            "description": (
                "Review workspace changes for correctness, security, and maintainability."
            ),
            "prompt": (
                "Review the active workspace diff. Do not edit files. Prioritize concrete "
                "correctness, security, isolation, and maintainability findings, with "
                "file and line evidence. Use git diff --no-ext-diff --no-textconv when "
                "you need an unattended diff."
            ),
            "permission": {
                "edit": "deny",
                "bash": {
                    "*": "ask",
                    "git diff --no-ext-diff --no-textconv": "allow",
                    "git diff --no-ext-diff --no-textconv --stat": "allow",
                    "git status": "allow",
                    "git status --short": "allow",
                },
            },
        },
        "test": {
            "mode": "subagent",
            "description": "Select and run bounded tests, then interpret failures.",
            "prompt": (
                "Work only in the active workspace. Choose the smallest relevant test "
                "set, state the command and working directory, keep execution bounded, "
                "and distinguish product failures from environment failures."
            ),
        },
    }
    for name, profile in (agent_models or {}).items():
        if name not in AGENTS:
            raise BedrockError(f"unsupported agent model override: {name}")
        if not profile.strip():
            raise BedrockError(f"agent model profile must not be empty: {name}")
        alias = f"agent-{name}"
        models[alias] = {
            "id": profile,
            "name": f"{name} agent through Amazon Bedrock",
        }
        agents.setdefault(name, {})["model"] = f"amazon-bedrock/{alias}"

    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "model": "amazon-bedrock/opus",
        "default_agent": "build",
        "provider": {
            "amazon-bedrock": {
                "options": options,
                "models": models,
            }
        },
        "permission": {
            "*": "ask",
            "read": {
                "*": "allow",
                "*.env": "deny",
                "*.env.*": "deny",
                "**/.env": "deny",
                "**/.env.*": "deny",
                "**/*credential*": "deny",
                "**/*secret*": "deny",
                "**/*.pem": "deny",
                "**/*.key": "deny",
            },
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "lsp": "allow",
            "todowrite": "allow",
            "question": "allow",
            "task": "allow",
            "external_directory": "deny",
            "edit": edit,
            "bash": bash,
            "webfetch": "deny",
            "websearch": "deny",
        },
        "agent": agents,
    }
    if workspace and (workspace / "AGENTS.md").is_file():
        config["instructions"] = [str(workspace / "AGENTS.md")]
    return config
