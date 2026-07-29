from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from opencode_bedrock.api import Client


class Handler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, dict | None]] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def _reply(self, status: int, value: object | None = None) -> None:
        body = b"" if value is None else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict | None:
        length = int(self.headers.get("content-length", "0"))
        return json.loads(self.rfile.read(length)) if length else None

    def do_GET(self) -> None:
        self.requests.append(("GET", self.path, None))
        if self.path.startswith("/permission"):
            return self._reply(200, [{"id": "perm", "sessionID": "ses", "permission": "edit"}])
        return self._reply(200, {"healthy": True})

    def do_POST(self) -> None:
        body = self._body()
        self.requests.append(("POST", self.path, body))
        if self.path.startswith("/session?"):
            return self._reply(200, {"id": "ses"})
        if "prompt_async" in self.path:
            return self._reply(204)
        return self._reply(200, True)


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        Handler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(self.server.server_port, "secret", Path("/tmp/a workspace"))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def test_async_task_and_approval_requests_are_workspace_scoped(self) -> None:
        session = self.client.create_session("title")
        self.client.prompt_async(session["id"], "prompt", "build")
        pending = self.client.permissions()
        self.client.reply(pending[0]["id"], "reject", "change it")
        paths = [item[1] for item in Handler.requests]
        self.assertTrue(all("directory=%2Ftmp%2Fa+workspace" in path for path in paths))
        prompt = Handler.requests[1][2]
        rejection = Handler.requests[-1][2]
        self.assertIsNotNone(prompt)
        self.assertIsNotNone(rejection)
        assert prompt is not None
        assert rejection is not None
        self.assertEqual(prompt["parts"][0]["text"], "prompt")
        self.assertEqual(rejection["message"], "change it")


if __name__ == "__main__":
    unittest.main()
