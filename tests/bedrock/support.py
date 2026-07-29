from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

FAKE_OPENCODE = r"""#!/usr/bin/env python3
import json
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if "--version" in sys.argv:
    print("opencode-fake 1.0")
    raise SystemExit(0)

if "attach" in sys.argv:
    raise SystemExit(0)

port = int(sys.argv[sys.argv.index("--port") + 1])

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def send(self, status, value=None):
        body = b"" if value is None else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/global/health"):
            return self.send(200, {"healthy": True, "version": "fake"})
        if self.path.startswith("/session/status"):
            return self.send(200, {})
        if self.path.startswith("/permission"):
            return self.send(200, [])
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        if length:
            self.rfile.read(length)
        if self.path.startswith("/session?"):
            return self.send(200, {"id": "ses_fake"})
        if "/prompt_async" in self.path:
            return self.send(204)
        if self.path.startswith("/permission/"):
            return self.send(200, True)
        return self.send(404, {"error": "not found"})

server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
server.serve_forever()
"""


@contextmanager
def isolated_environment() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as value:
        root = Path(value)
        env = {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
        }
        with patch.dict(os.environ, env, clear=False):
            yield root


def git_repository(root: Path, name: str = "repo") -> Path:
    path = root / name
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "test"], check=True)
    return path.resolve()


def fake_opencode(root: Path) -> Path:
    path = root / "fake-opencode"
    path.write_text(FAKE_OPENCODE, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path.resolve()
