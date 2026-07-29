from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .errors import BedrockError
from .paths import ensure_private_directory
from .workspace import contains

AWS_ENV = {
    "AWS_ACCESS_KEY_ID",
    "AWS_CA_BUNDLE",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_DEFAULT_REGION",
    "AWS_EC2_METADATA_DISABLED",
    "AWS_EC2_METADATA_SERVICE_ENDPOINT",
    "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE",
    "AWS_REGION",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_STS_REGIONAL_ENDPOINTS",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
}


def find_opencode(explicit: str | None = None) -> Path:
    candidates = [
        explicit,
        os.environ.get("OPENCODE_BIN"),
        str(Path(sys.argv[0]).resolve().parent / "opencode"),
        shutil.which("opencode"),
    ]
    for item in candidates:
        if not item:
            continue
        path = Path(item).expanduser().resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise BedrockError(
        "opencode binary not found; set OPENCODE_BIN or install the offline artifact"
    )


def find_bwrap() -> Path:
    value = shutil.which("bwrap")
    if not value:
        raise BedrockError(
            "bubblewrap is required for isolation; install bwrap before starting a service"
        )
    return Path(value).resolve()


def environment(
    service_dir: Path,
    config: dict[str, Any],
    password: str,
) -> dict[str, str]:
    xdg = service_dir / "xdg"
    home = service_dir / "home"
    for directory in [
        xdg / "config",
        xdg / "data",
        xdg / "cache",
        xdg / "state",
        home,
    ]:
        ensure_private_directory(directory)

    result = {
        key: value
        for key, value in os.environ.items()
        if key in AWS_ENV
        or key in {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    result.update(
        {
            "HOME": str(home),
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "XDG_CONFIG_HOME": str(xdg / "config"),
            "XDG_DATA_HOME": str(xdg / "data"),
            "XDG_CACHE_HOME": str(xdg / "cache"),
            "XDG_STATE_HOME": str(xdg / "state"),
            "OPENCODE_CONFIG_CONTENT": json.dumps(config, separators=(",", ":")),
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENCODE_SERVER_USERNAME": "opencode",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
            "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_SHARE": "1",
            "OPENCODE_PURE": "1",
        }
    )
    return result


def command(
    workspace: Path,
    service_dir: Path,
    opencode: Path,
    port: int,
) -> list[str]:
    bwrap = find_bwrap()
    args = [
        str(bwrap),
        "--new-session",
        "--unshare-pid",
        "--tmpfs",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]

    for source in ["/usr", "/usr/local", "/bin", "/sbin", "/lib", "/lib64"]:
        path = Path(source)
        if path.exists():
            args.extend(_parents(path))
            args.extend(["--ro-bind", source, source])

    args.extend(["--dir", "/etc"])
    for source in [
        "/etc/alternatives",
        "/etc/ca-certificates",
        "/etc/ssl",
        "/etc/gitconfig",
        "/etc/group",
        "/etc/hosts",
        "/etc/ld.so.cache",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/resolv.conf",
    ]:
        path = Path(source)
        if not path.exists():
            continue
        args.extend(_parents(path))
        args.extend(["--ro-bind", source, source])

    args.extend(_parents(service_dir))
    args.extend(["--bind", str(service_dir), str(service_dir)])

    args.extend(_parents(workspace))
    args.extend(["--bind", str(workspace), str(workspace)])

    if not contains(Path("/usr"), opencode):
        args.extend(_parents(opencode))
        args.extend(["--ro-bind", str(opencode), str(opencode)])

    for name in [
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "AWS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ]:
        value = os.environ.get(name)
        if not value:
            continue
        path = Path(value).expanduser().resolve()
        if not path.exists():
            continue
        args.extend(_parents(path))
        args.extend(["--ro-bind", str(path), str(path)])

    args.extend(
        [
            "--chdir",
            str(workspace),
            "--",
            str(opencode),
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
        ]
    )
    return args


def _parents(path: Path) -> list[str]:
    current = path if path.is_dir() else path.parent
    parents = list(current.parents)
    result: list[str] = []
    for parent in reversed(parents[:-1]):
        result.extend(["--dir", str(parent)])
    result.extend(["--dir", str(current)])
    return result
