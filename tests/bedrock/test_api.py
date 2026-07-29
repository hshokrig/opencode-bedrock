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
        if self.path == "/api/event":
            body = b': heartbeat\r\ndata: {"type":\r\ndata: "test", "data": {}}\r\n\r\n'
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/session/ses_chat/message"):
            if self.path.startswith("/api/session/ses_chat/message/msg_chat"):
                return self._reply(200, {"data": {"id": "msg_chat", "type": "user"}})
            return self._reply(200, {"data": [], "cursor": {}})
        if self.path.startswith("/api/session/ses_chat/history"):
            return self._reply(200, {"data": [], "hasMore": False})
        if self.path == "/api/session/active":
            return self._reply(200, {"data": {"ses_active": {"type": "running"}}})
        if self.path.startswith("/api/session/ses_chat"):
            return self._reply(200, {"data": {"id": "ses_chat"}})
        if self.path.startswith("/api/session?"):
            return self._reply(200, {"data": [], "cursor": {}})
        if self.path.startswith("/permission"):
            return self._reply(200, [{"id": "perm", "sessionID": "ses", "permission": "edit"}])
        return self._reply(200, {"healthy": True})

    def do_POST(self) -> None:
        body = self._body()
        self.requests.append(("POST", self.path, body))
        if self.path.startswith("/session?"):
            return self._reply(200, {"id": "ses"})
        if self.path == "/api/session":
            return self._reply(200, {"data": body})
        if self.path.endswith("/prompt"):
            return self._reply(200, {"data": {"id": body["id"]}})
        if self.path.endswith("/title/ensure"):
            return self._reply(200, {"data": {"title": "Generated title"}})
        if self.path.endswith("/wait") or self.path.endswith("/interrupt"):
            return self._reply(204)
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

    def test_v2_chat_requests_use_exact_identity_and_queue_delivery(self) -> None:
        session = self.client.create_chat_session("ses_chat")
        admitted = self.client.prompt_chat("ses_chat", "msg_chat", "hello")
        self.client.wait_chat("ses_chat")
        self.client.chat_messages("ses_chat")
        message = self.client.chat_message("ses_chat", "msg_chat")
        title = self.client.ensure_chat_title("ses_chat", "msg_chat")
        active = self.client.active_chat_sessions()

        self.assertEqual(session["id"], "ses_chat")
        self.assertEqual(admitted["id"], "msg_chat")
        self.assertEqual(title, "Generated title")
        self.assertEqual(message["id"], "msg_chat")
        self.assertEqual(active, {"ses_active"})
        created = Handler.requests[0][2]
        prompted = Handler.requests[1][2]
        assert created is not None
        assert prompted is not None
        self.assertEqual(created["purpose"], "terminal-chat")
        self.assertEqual(created["model"], {"providerID": "amazon-bedrock", "id": "opus"})
        self.assertEqual(prompted["delivery"], "queue")
        self.assertEqual(prompted["prompt"], {"text": "hello"})

    def test_event_stream_parses_crlf_comments_and_multiline_data(self) -> None:
        stream = self.client.events()
        try:
            self.assertEqual(list(stream), [{"type": "test", "data": {}}])
        finally:
            stream.close()


if __name__ == "__main__":
    unittest.main()
