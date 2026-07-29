from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import BedrockError, JSONWriteError
from .paths import ensure_private_directory

PRIVATE_JSON_LIMIT = 1024 * 1024
_DEFAULT_MISSING = object()


@contextmanager
def locked(path: Path) -> Iterator[None]:
    try:
        ensure_private_directory(path.parent)
    except OSError as error:
        raise BedrockError(
            f"cannot prepare private lock directory {path.parent}: {error}"
        ) from error
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise BedrockError(f"cannot open private lock {path}: {error}") from error
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BedrockError(f"lock path is not a regular file: {path}")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        acquired = True
    except OSError as error:
        os.close(descriptor)
        raise BedrockError(f"cannot use private lock {path}: {error}") from error
    except BaseException:
        os.close(descriptor)
        raise
    try:
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        except OSError as error:
            raise BedrockError(f"cannot close private lock {path}: {error}") from error


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def read_private_json_object(
    path: Path,
    *,
    label: str = "private JSON state",
    limit: int = PRIVATE_JSON_LIMIT,
    missing: Any = _DEFAULT_MISSING,
) -> dict[str, Any] | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {} if missing is _DEFAULT_MISSING else missing
    except OSError as error:
        raise BedrockError(f"cannot open {label}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise BedrockError(f"{label} must be a mode-0600 regular file")
        if metadata.st_size > limit:
            raise BedrockError(f"{label} exceeds the {limit}-byte limit")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(limit + 1)
        if len(payload) > limit:
            raise BedrockError(f"{label} exceeds the {limit}-byte limit")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise BedrockError(f"cannot read {label}: {error}") from error
        if not isinstance(value, dict):
            raise BedrockError(f"{label} root must be a JSON object")
        return value
    except OSError as error:
        raise BedrockError(f"cannot read {label}: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_json(path: Path, value: Any) -> None:
    temporary: str | None = None
    committed = False
    try:
        ensure_private_directory(path.parent)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
    except OSError as error:
        raise JSONWriteError(path, error, committed=False) from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        committed = True
        _fsync_directory(path.parent)
    except (OSError, TypeError, ValueError, RecursionError) as error:
        try:
            if not committed and temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
        raise JSONWriteError(path, error, committed=committed) from error


def unlink_durable(path: Path, *, label: str = "private state") -> None:
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except FileNotFoundError:
        return
    except OSError as error:
        raise BedrockError(f"cannot remove {label} {path}: {error}") from error


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
