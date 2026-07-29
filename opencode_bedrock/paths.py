from __future__ import annotations

import os
from pathlib import Path

APP = "opencode-bedrock"


def _xdg(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else fallback


def config_root() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / APP


def state_root() -> Path:
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP


def data_root() -> Path:
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP


def projects_file() -> Path:
    return config_root() / "projects.json"


def services_root() -> Path:
    return state_root() / "services"


def ensure_private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path
