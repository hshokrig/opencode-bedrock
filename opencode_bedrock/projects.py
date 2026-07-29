from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import BedrockError
from .io import locked, read_json, write_json
from .paths import projects_file
from .workspace import canonical_workspace, is_git_repository, validate_name


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    git: bool


def _load() -> dict:
    data = read_json(projects_file(), {"version": 1, "projects": {}})
    if data.get("version") != 1 or not isinstance(data.get("projects"), dict):
        raise BedrockError(f"unsupported project registry format: {projects_file()}")
    return data


def add(name: str, value: str, allow_non_git: bool = False) -> Project:
    validate_name(name)
    path = canonical_workspace(value, allow_non_git)
    lock = projects_file().with_suffix(".lock")
    with locked(lock):
        data = _load()
        duplicate = next(
            (
                alias
                for alias, item in data["projects"].items()
                if Path(item["path"]).resolve() == path and alias != name
            ),
            None,
        )
        if duplicate:
            raise BedrockError(f"workspace is already registered as {duplicate}")
        existing = data["projects"].get(name)
        if existing and Path(existing["path"]).resolve() != path:
            raise BedrockError(f"project name is already registered: {name}")
        data["projects"][name] = {"path": str(path), "git": is_git_repository(path)}
        write_json(projects_file(), data)
    return Project(name, path, bool(data["projects"][name]["git"]))


def list_projects() -> list[Project]:
    return [
        Project(name, Path(item["path"]), bool(item.get("git")))
        for name, item in sorted(_load()["projects"].items())
    ]


def get(name: str) -> Project:
    item = _load()["projects"].get(name)
    if not item:
        raise BedrockError(f"project is not registered: {name}")
    path = canonical_workspace(item["path"], allow_non_git=True)
    return Project(name, path, bool(item.get("git")))
