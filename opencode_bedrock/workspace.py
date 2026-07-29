from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from .errors import BedrockError

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise BedrockError("project names must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return name


def canonical_workspace(value: str, allow_non_git: bool = False) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise BedrockError(f"workspace must be an absolute path: {value}")
    try:
        path = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise BedrockError(f"workspace does not exist: {candidate}") from error
    if not path.is_dir():
        raise BedrockError(f"workspace is not a directory: {path}")
    if not allow_non_git and not is_git_repository(path):
        raise BedrockError(
            f"workspace is not a Git repository: {path}; pass --allow-non-git to opt in"
        )
    return path


def is_git_repository(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def service_key(name: str | None, workspace: Path) -> str:
    prefix = validate_name(name) if name else re.sub(r"[^A-Za-z0-9._-]", "-", workspace.name)
    digest = hashlib.sha256(str(workspace).encode()).hexdigest()[:12]
    return f"{prefix}-{digest}"


def contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_mount_separation(workspace: Path, service_dir: Path) -> None:
    resolved_service = service_dir.resolve()
    if contains(workspace, resolved_service) or contains(resolved_service, workspace):
        raise BedrockError("workspace and service state directories must not overlap")
    protected = [
        Path(item).resolve()
        for item in [
            "/boot",
            "/dev",
            "/etc",
            "/lib",
            "/lib64",
            "/proc",
            "/run",
            "/sbin",
            "/sys",
            "/usr",
            "/var",
        ]
    ]
    protected.extend(
        [
            (Path.home() / item).resolve()
            for item in [".aws", ".config", ".docker", ".gnupg", ".kube", ".ssh"]
        ]
    )
    if next(
        (path for path in protected if contains(workspace, path) or contains(path, workspace)),
        None,
    ):
        raise BedrockError(f"workspace overlaps a protected system or credential path: {workspace}")
