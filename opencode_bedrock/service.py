from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .api import Client
from .errors import BedrockError
from .io import locked, read_json, write_json
from .paths import ensure_private_directory, services_root
from .policy import opencode_config
from .sandbox import command, environment, find_opencode
from .workspace import service_key, validate_mount_separation

_children: dict[int, subprocess.Popen[str]] = {}


@dataclass
class Record:
    key: str
    project: str | None
    workspace: str
    pid: int
    process_start: int
    port: int
    password: str
    started_at: float
    headless_policy: str
    region: str
    inference_profile: str
    agent_models: dict[str, str]
    endpoint: str | None
    opencode: str
    log: str


def start(
    workspace: Path,
    project: str | None,
    region: str,
    inference_profile: str,
    endpoint: str | None,
    headless_policy: str,
    foreground: bool,
    port: int | None,
    opencode_value: str | None,
    agent_models: dict[str, str] | None = None,
) -> Record:
    key = service_key(project, workspace)
    directory = ensure_private_directory(services_root() / key)
    validate_mount_separation(workspace, directory)
    lock_path = services_root() / ".start.lock"
    with locked(lock_path):
        _reject_duplicate(workspace)
        existing = load_record(directory)
        if existing and alive(existing):
            raise BedrockError(f"service is already running for {workspace} (pid {existing.pid})")
        if existing:
            (directory / "service.json").unlink(missing_ok=True)

        selected_port = port if port is not None else _free_port()
        if selected_port < 1 or selected_port > 65535:
            raise BedrockError("port must be between 1 and 65535")
        password = secrets.token_urlsafe(32)
        opencode = find_opencode(opencode_value)
        config = opencode_config(
            region,
            inference_profile,
            endpoint,
            headless_policy,
            workspace,
            agent_models,
        )
        process_command = command(workspace, directory, opencode, selected_port)
        process_environment = environment(directory, config, password)
        log_path = directory / "service.log"
        rotate_logs(log_path)
        log_handle = log_path.open("a", encoding="utf-8", buffering=1)
        os.chmod(log_path, 0o600)
        log_handle.write(f"[opencode-bedrock] workspace={workspace} project={project or '-'}\n")

        try:
            process = subprocess.Popen(
                process_command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=workspace,
                env=process_environment,
                start_new_session=True,
                text=True,
            )
        except OSError as error:
            log_handle.close()
            raise BedrockError(f"could not start OpenCode: {error}") from error
        record = Record(
            key=key,
            project=project,
            workspace=str(workspace),
            pid=process.pid,
            process_start=_process_start(process.pid),
            port=selected_port,
            password=password,
            started_at=time.time(),
            headless_policy=headless_policy,
            region=region,
            inference_profile=inference_profile,
            agent_models=agent_models or {},
            endpoint=endpoint,
            opencode=str(opencode),
            log=str(log_path),
        )
        write_json(directory / "service.json", asdict(record))
        _children[record.pid] = process

    if foreground:
        print(f"workspace: {workspace}")
        print(f"service:   http://127.0.0.1:{selected_port}")
        try:
            _wait_ready(record, process)
            try:
                returncode = process.wait()
            except KeyboardInterrupt:
                os.killpg(process.pid, signal.SIGTERM)
                returncode = process.wait(timeout=10)
        finally:
            if alive(record):
                _terminate(record, timeout=5)
            _reap(record.pid)
            (directory / "service.json").unlink(missing_ok=True)
            log_handle.close()
        if returncode:
            raise BedrockError(f"OpenCode exited with status {returncode}")
        return record

    log_handle.close()
    try:
        _wait_ready(record, process)
    except Exception:
        _terminate(record, timeout=5)
        (directory / "service.json").unlink(missing_ok=True)
        raise
    return record


def stop(record: Record, timeout: float = 15) -> None:
    directory = services_root() / record.key
    with locked(directory / "service.lock"):
        current = load_record(directory)
        if not current:
            return
        if alive(current):
            _terminate(current, timeout)
        (directory / "service.json").unlink(missing_ok=True)


def load_record(directory: Path) -> Record | None:
    path = directory / "service.json"
    if not path.exists():
        return None
    try:
        return Record(**read_json(path, {}))
    except (TypeError, json.JSONDecodeError) as error:
        raise BedrockError(f"invalid service state: {path}") from error


def list_records(include_stale: bool = True) -> list[Record]:
    root = ensure_private_directory(services_root())
    records = [
        record
        for directory in root.iterdir()
        if directory.is_dir()
        for record in [load_record(directory)]
        if record is not None
    ]
    return records if include_stale else [record for record in records if alive(record)]


def find_record(
    project: str | None, workspace: Path | None, require_running: bool = True
) -> Record:
    matches = [
        record
        for record in list_records()
        if (project is not None and record.project == project)
        or (workspace is not None and Path(record.workspace) == workspace)
    ]
    if not matches:
        target = project or str(workspace)
        raise BedrockError(f"no service state found for {target}")
    record = matches[0]
    if require_running and not alive(record):
        raise BedrockError(
            f"service is not running for {record.workspace}; stale PID state detected"
        )
    return record


def alive(record: Record) -> bool:
    try:
        os.kill(record.pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        if Path(f"/proc/{record.pid}/stat").read_text(encoding="utf-8").split()[2] == "Z":
            return False
        return _process_start(record.pid) == record.process_start
    except (FileNotFoundError, ValueError):
        return False


def client(record: Record) -> Client:
    return Client(record.port, record.password, Path(record.workspace))


def rotate_logs(path: Path, generations: int = 3, max_bytes: int = 5 * 1024 * 1024) -> None:
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    oldest = path.with_name(f"{path.name}.{generations}")
    oldest.unlink(missing_ok=True)
    for number in range(generations - 1, 0, -1):
        source = path.with_name(f"{path.name}.{number}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{number + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


def scrub(record: Record) -> dict[str, Any]:
    value = asdict(record)
    value.pop("password", None)
    value["inference_profile"] = "<configured>"
    value["agent_models"] = {name: "<configured>" for name in record.agent_models}
    value["running"] = alive(record)
    value["workspace"] = str(Path(record.workspace))
    return value


def _reject_duplicate(workspace: Path) -> None:
    duplicate = next(
        (
            record
            for record in list_records()
            if Path(record.workspace) == workspace and alive(record)
        ),
        None,
    )
    if duplicate:
        label = duplicate.project or duplicate.key
        raise BedrockError(
            f"workspace already has a running service ({label}, pid {duplicate.pid})"
        )


def _wait_ready(record: Record, process: subprocess.Popen[str], timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = _tail(Path(record.log))
            raise BedrockError(f"OpenCode exited during startup\n{tail}")
        try:
            if client(record).health().get("healthy") is True:
                return
        except BedrockError as error:
            last_error = error
        time.sleep(0.1)
    raise BedrockError(f"OpenCode did not become ready: {last_error}\n{_tail(Path(record.log))}")


def _terminate(record: Record, timeout: float) -> None:
    if not alive(record):
        _reap(record.pid)
        return
    try:
        os.killpg(record.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _group_alive(record.pid):
            _reap(record.pid)
            return
        time.sleep(0.1)
    if _group_alive(record.pid):
        os.killpg(record.pid, signal.SIGKILL)
    _reap(record.pid)


def _reap(pid: int) -> None:
    process = _children.pop(pid, None)
    if process:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return


def _group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _process_start(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    return int(fields[21])


def _tail(path: Path, lines: int = 30) -> str:
    if not path.exists():
        return "(no service log)"
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
