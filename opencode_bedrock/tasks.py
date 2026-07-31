from __future__ import annotations

import time
import uuid
from typing import Any

from .io import locked, read_json, write_json
from .errors import BedrockError
from .paths import services_root
from .service import Record, client


def add(
    record: Record,
    session_id: str,
    prompt: str,
    agent: str,
    message_id: str | None = None,
    status: str = "submitted",
) -> dict[str, Any]:
    directory = services_root() / record.key
    path = directory / "tasks.json"
    task = {
        "id": f"task-{uuid.uuid4().hex[:12]}",
        "session_id": session_id,
        "message_id": message_id or f"msg_{uuid.uuid4().hex}",
        "workspace": record.workspace,
        "project": record.project,
        "agent": agent,
        "prompt": prompt,
        "submitted_at": time.time(),
        "status": status,
    }
    with locked(directory / "tasks.lock"):
        data = read_json(path, {"version": 1, "tasks": []})
        data["tasks"].append(task)
        write_json(path, data)
    return task


def update(record: Record, task_id: str, status: str) -> None:
    directory = services_root() / record.key
    path = directory / "tasks.json"
    with locked(directory / "tasks.lock"):
        data = read_json(path, {"version": 1, "tasks": []})
        task = next((item for item in data["tasks"] if item.get("id") == task_id), None)
        if task is None:
            raise BedrockError(f"task is not recorded: {task_id}")
        task["status"] = status
        write_json(path, data)


def list_tasks(record: Record, refresh: bool = True) -> list[dict[str, Any]]:
    directory = services_root() / record.key
    path = directory / "tasks.json"
    if not refresh:
        return read_json(path, {"version": 1, "tasks": []})["tasks"]
    api = client(record)
    statuses = api.statuses()
    active = api.active_chat_sessions()
    pending = api.permissions()
    waiting = {item["sessionID"] for item in pending}
    with locked(directory / "tasks.lock"):
        data = read_json(path, {"version": 1, "tasks": []})
        for task in data["tasks"]:
            if task.get("status") == "failed":
                continue
            if task.get("status") == "delivery unknown":
                try:
                    recovery = api.chat_recovery(task["session_id"], task.get("message_id"))
                except BedrockError:
                    continue
                if recovery["requestedInputStatus"] == "absent":
                    continue
            session_id = task["session_id"]
            if session_id in waiting:
                task["status"] = "awaiting approval"
                continue
            if session_id in active:
                task["status"] = "busy"
                continue
            status = statuses.get(session_id)
            task["status"] = status.get("type", "idle") if isinstance(status, dict) else "idle"
        write_json(path, data)
    return data["tasks"]
