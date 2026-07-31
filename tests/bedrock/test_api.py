from __future__ import annotations

import http.client
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from opencode_bedrock.api import JSON_RESPONSE_LIMIT, Client, EventStream
from opencode_bedrock.errors import (
    BedrockError,
    HTTPResponseError,
    NotFoundError,
    TransportError,
)


def protocol_tokens() -> dict:
    return {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache": {"read": 0, "write": 0},
    }


def protocol_session(**updates: object) -> dict:
    return {
        "id": "ses_chat",
        "projectID": "project",
        "title": "Chat",
        "cost": 0,
        "tokens": protocol_tokens(),
        "location": {"directory": "/tmp/a workspace"},
        "time": {"created": 1785283200000, "updated": 1785283200000},
        **updates,
    }


def protocol_user(message_id: str = "msg_chat", text: str = "hello") -> dict:
    return {
        "id": message_id,
        "type": "user",
        "text": text,
        "time": {"created": 1785283200000},
    }


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
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header(
                "location",
                f"http://127.0.0.1:{self.server.server_port}/redirect-target",
            )
            self.end_headers()
            return
        if self.path == "/redirect-target":
            return self._reply(200, {"authorization": self.headers.get("authorization")})
        if self.path == "/not-found":
            return self._reply(404, {"error": "missing"})
        if self.path == "/failure":
            return self._reply(500, {"error": "failed"})
        if self.path == "/oversized":
            body = b" " * (JSON_RESPONSE_LIMIT + 1)
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/malformed-utf8":
            body = b'{"value":"\xff"}'
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/malformed-json":
            body = b'{"value":'
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
                return self._reply(
                    200,
                    {"data": protocol_user()},
                )
            return self._reply(200, {"data": [], "cursor": {}})
        if self.path.startswith("/api/session/ses_chat/history"):
            return self._reply(200, {"data": [], "hasMore": False})
        if self.path.startswith("/api/session/ses_chat/recovery"):
            requested = "absent" if "messageID=" in self.path else "not-requested"
            return self._reply(
                200,
                {
                    "data": {
                        "unfinishedProviderAttempt": False,
                        "unfinishedCompaction": False,
                        "unresolvedInput": False,
                        "attemptedUnsettledInput": False,
                        "requestedInputStatus": requested,
                        "otherUnresolvedInput": False,
                    }
                },
            )
        if self.path == "/api/session/active":
            return self._reply(200, {"data": {"ses_active": {"type": "running"}}})
        if self.path.startswith("/api/session/ses_chat"):
            return self._reply(
                200,
                {"data": protocol_session()},
            )
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
            return self._reply(
                200,
                {
                    "data": {
                        **protocol_session(
                            agent=body["agent"],
                            model=body["model"],
                            location=body["location"],
                            **({"purpose": body["purpose"]} if "purpose" in body else {}),
                        ),
                        "id": body["id"],
                    }
                },
            )
        if self.path.endswith("/prompt"):
            if body["id"] == "msg_invalid":
                return self._reply(200, {"data": []})
            return self._reply(
                200,
                {
                    "data": {
                        "admittedSeq": 1,
                        "id": body["id"],
                        "sessionID": "ses_chat",
                        "prompt": body["prompt"],
                        "delivery": body["delivery"],
                        "timeCreated": 1785283200000,
                    }
                },
            )
        if self.path.endswith("/title/ensure"):
            return self._reply(200, {"data": {"title": "Generated title"}})
        if self.path.endswith("/wait") or self.path.endswith("/interrupt"):
            return self._reply(204)
        if "prompt_async" in self.path:
            return self._reply(204)
        return self._reply(200, True)


class ApiResponseValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Client(12345, "secret", Path("/tmp/a workspace"))

    def test_unreadable_malformed_and_oversized_successes_are_unconfirmed(self) -> None:
        class Response:
            def __init__(self, payload: bytes | BaseException):
                self.payload = payload

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, limit: int) -> bytes:
                if isinstance(self.payload, BaseException):
                    raise self.payload
                return self.payload

        for payload in (
            OSError("connection reset after success status"),
            b'{"data":',
            b" " * (JSON_RESPONSE_LIMIT + 1),
        ):
            with self.subTest(payload=type(payload).__name__):
                with patch(
                    "opencode_bedrock.api.OPENER.open",
                    return_value=Response(payload),
                ):
                    with self.assertRaises(TransportError):
                        self.client.prompt_chat("ses_chat", "msg_chat", "hello")

        with patch(
            "opencode_bedrock.api.OPENER.open",
            return_value=Response(b"{}"),
        ):
            with self.assertRaisesRegex(TransportError, "no-content response"):
                self.client.interrupt_chat("ses_chat")

    def test_truncated_and_structurally_invalid_successes_are_unconfirmed(self) -> None:
        class TruncatedResponse:
            def __enter__(self) -> "TruncatedResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, limit: int) -> bytes:
                raise http.client.IncompleteRead(b'{"data":', 20)

        with patch(
            "opencode_bedrock.api.OPENER.open",
            return_value=TruncatedResponse(),
        ):
            with self.assertRaises(TransportError):
                self.client.prompt_chat("ses_chat", "msg_chat", "hello")

        with patch.object(
            Client,
            "_request",
            return_value={"data": []},
        ):
            with self.assertRaisesRegex(
                TransportError,
                "invalid successful prompt admission",
            ):
                self.client.prompt_chat("ses_chat", "msg_invalid", "hello")

        with patch.object(
            Client,
            "_request",
            return_value={"data": {"id": "msg_other"}},
        ):
            with self.assertRaisesRegex(TransportError, "did not confirm"):
                self.client.prompt_chat("ses_chat", "msg_invalid", "hello")

    def test_definite_http_status_survives_an_unreadable_error_body(self) -> None:
        class UnreadableBody:
            def read(self, limit: int) -> bytes:
                raise http.client.IncompleteRead(b"{", 20)

            def close(self) -> None:
                return None

        error = urllib.error.HTTPError(
            "http://127.0.0.1/prompt",
            409,
            "Conflict",
            {},
            UnreadableBody(),
        )
        with patch("opencode_bedrock.api.OPENER.open", side_effect=error):
            with self.assertRaises(HTTPResponseError) as caught:
                self.client.prompt_chat("ses_chat", "msg_chat", "hello")

        self.assertEqual(caught.exception.status, 409)

    def test_port_is_validated_before_any_url_is_constructed(self) -> None:
        for port in (True, 0, 65536, "1234"):
            with self.subTest(port=port):
                with self.assertRaisesRegex(BedrockError, "port"):
                    Client(port, "secret", Path("/tmp"))  # type: ignore[arg-type]

    def test_pages_validate_message_types_and_both_cursor_directions(self) -> None:
        response = {
            "data": [
                {
                    "id": "msg_system",
                    "type": "system",
                    "text": "system",
                    "time": {"created": 1785283200000},
                },
                {
                    "id": "msg_compaction",
                    "type": "compaction",
                    "reason": "auto",
                    "summary": "summary",
                    "recent": "recent",
                    "time": {"created": 1785283200000},
                },
            ],
            "cursor": {"previous": "before_1", "next": "after_1"},
        }
        with patch.object(Client, "_request", return_value=response):
            self.assertEqual(
                self.client.chat_messages("ses_chat")["cursor"],
                response["cursor"],
            )
        for malformed in (
            {"data": [], "cursor": {"previous": "\n"}},
            {
                "data": [
                    {
                        "id": "msg",
                        "type": "unknown",
                        "time": {"created": 1},
                    }
                ],
                "cursor": {},
            },
            {"data": [], "cursor": {}, "extra": True},
        ):
            with self.subTest(malformed=malformed):
                with patch.object(Client, "_request", return_value=malformed):
                    with self.assertRaises(TransportError):
                        self.client.chat_messages("ses_chat")

    def test_endpoint_specific_success_shapes_are_not_trusted(self) -> None:
        cases = [
            (
                lambda: self.client.list_chat_sessions(),
                {"data": {"id": "ses"}, "cursor": {}},
            ),
            (
                lambda: self.client.active_chat_sessions(),
                {"data": {"ses_active": "running"}},
            ),
            (
                lambda: self.client.chat_message("ses_chat", "msg_chat"),
                {"data": {"id": "msg_other", "type": "user"}},
            ),
            (
                lambda: self.client.chat_history("ses_chat"),
                {"data": [], "hasMore": "false"},
            ),
        ]
        for operation, response in cases:
            with self.subTest(response=response):
                with patch.object(Client, "_request", return_value=response):
                    with self.assertRaises(TransportError):
                        operation()

    def test_nested_session_and_message_shapes_are_validated(self) -> None:
        valid_session = {
            "data": protocol_session()
        }
        for missing in (
            "id",
            "projectID",
            "title",
            "cost",
            "tokens",
            "location",
            "time",
        ):
            data = {**valid_session["data"]}
            data.pop(missing)
            with self.subTest(missing_session_field=missing):
                with patch.object(Client, "_request", return_value={"data": data}):
                    with self.assertRaises(TransportError):
                        self.client.get_chat_session("ses_chat")
        for mutation in (
            {"title": None},
            {"time": []},
            {"time": {"created": float("nan")}},
            {"model": {"providerID": "amazon-bedrock"}},
            {"location": {"directory": 1}},
        ):
            with self.subTest(session=mutation):
                with patch.object(
                    Client,
                    "_request",
                    return_value={"data": {**valid_session["data"], **mutation}},
                ):
                    with self.assertRaises(TransportError):
                        self.client.get_chat_session("ses_chat")

        valid_assistant = {
            "data": {
                "id": "msg_chat",
                "type": "assistant",
                "agent": "chat",
                "model": {"providerID": "amazon-bedrock", "id": "opus"},
                "content": [{"type": "text", "id": "text_1", "text": "answer"}],
                "time": {"created": 1785283200000},
            }
        }
        for mutation in (
            {"content": {}},
            {"content": [None]},
            {"content": [{"type": "text", "text": None}]},
            {"error": "failed"},
            {"error": {"message": None}},
            {"time": None},
        ):
            with self.subTest(message=mutation):
                with patch.object(
                    Client,
                    "_request",
                    return_value={"data": {**valid_assistant["data"], **mutation}},
                ):
                    with self.assertRaises(TransportError):
                        self.client.chat_message("ses_chat", "msg_chat")

    def test_all_protocol_message_variants_and_tool_states_are_validated(self) -> None:
        created = {"created": 1785283200000}
        tool_time = {"created": 1785283200000}
        tool_content = [{"type": "text", "text": "result"}]
        variants = [
            {
                "id": "msg_agent",
                "type": "agent-switched",
                "agent": "chat",
                "time": created,
            },
            {
                "id": "msg_model",
                "type": "model-switched",
                "model": {"providerID": "amazon-bedrock", "id": "opus"},
                "time": created,
            },
            {
                **protocol_user("msg_user"),
                "files": [
                    {
                        "uri": "file:///tmp/a",
                        "mime": "text/plain",
                        "source": {"start": 0, "end": 1, "text": "a"},
                    }
                ],
                "agents": [{"name": "chat"}],
            },
            {
                "id": "msg_synthetic",
                "type": "synthetic",
                "sessionID": "ses_chat",
                "text": "synthetic",
                "time": created,
            },
            {
                "id": "msg_system",
                "type": "system",
                "text": "system",
                "time": created,
            },
            {
                "id": "msg_shell",
                "type": "shell",
                "callID": "call_1",
                "command": "true",
                "output": "",
                "time": {**created, "completed": 1785283200001},
            },
            {
                "id": "msg_assistant",
                "type": "assistant",
                "agent": "chat",
                "model": {"providerID": "amazon-bedrock", "id": "opus"},
                "content": [
                    {"type": "text", "id": "text_1", "text": "answer"},
                    {
                        "type": "reasoning",
                        "id": "reasoning_1",
                        "text": "thought",
                        "time": created,
                    },
                    {
                        "type": "tool",
                        "id": "tool_1",
                        "name": "read",
                        "time": tool_time,
                        "state": {"status": "pending", "input": "{}"},
                    },
                    {
                        "type": "tool",
                        "id": "tool_2",
                        "name": "read",
                        "time": tool_time,
                        "state": {
                            "status": "running",
                            "input": {},
                            "structured": {},
                            "content": tool_content,
                        },
                    },
                    {
                        "type": "tool",
                        "id": "tool_3",
                        "name": "read",
                        "time": tool_time,
                        "state": {
                            "status": "completed",
                            "input": {},
                            "structured": {},
                            "content": tool_content,
                            "attachments": [
                                {"uri": "file:///tmp/a", "mime": "text/plain"}
                            ],
                            "outputPaths": ["result.txt"],
                        },
                    },
                    {
                        "type": "tool",
                        "id": "tool_4",
                        "name": "read",
                        "time": tool_time,
                        "state": {
                            "status": "error",
                            "input": {},
                            "structured": {},
                            "content": tool_content,
                            "error": {"type": "unknown", "message": "failed"},
                        },
                    },
                ],
                "tokens": protocol_tokens(),
                "cost": 0,
                "time": created,
            },
            {
                "id": "msg_compaction",
                "type": "compaction",
                "reason": "manual",
                "summary": "summary",
                "recent": "recent",
                "retainedMessageIDs": ["msg_user"],
                "time": created,
            },
        ]
        with patch.object(
            Client,
            "_request",
            return_value={"data": variants, "cursor": {}},
        ):
            self.assertEqual(
                len(self.client.chat_messages("ses_chat")["data"]),
                len(variants),
            )

        malformed = [
            {**variants[2], "files": [{"uri": "file:///tmp/a"}]},
            {**variants[3], "sessionID": "wrong"},
            {**variants[5], "callID": None},
            {**variants[6], "agent": None},
            {
                **variants[6],
                "content": [
                    {
                        "type": "tool",
                        "id": "tool_bad",
                        "name": "read",
                        "time": tool_time,
                        "state": {
                            "status": "completed",
                            "input": {},
                            "content": tool_content,
                        },
                    }
                ],
            },
            {**variants[7], "reason": "unknown"},
        ]
        for message in malformed:
            with self.subTest(message=message["type"]):
                with patch.object(
                    Client,
                    "_request",
                    return_value={"data": [message], "cursor": {}},
                ):
                    with self.assertRaises(TransportError):
                        self.client.chat_messages("ses_chat")

    def test_sse_setup_does_not_read_optional_failure_body(self) -> None:
        class Response:
            status = 500

            def read(self, limit: int) -> bytes:
                raise AssertionError("optional failure body must not be read")

            def close(self) -> None:
                return

        class Connection:
            sock = None

            def request(self, *args: object, **kwargs: object) -> None:
                return

            def getresponse(self) -> Response:
                return Response()

            def close(self) -> None:
                return

        with patch("opencode_bedrock.api.http.client.HTTPConnection", return_value=Connection()):
            with self.assertRaises(HTTPResponseError) as caught:
                EventStream(12345, "secret")
        self.assertEqual(caught.exception.status, 500)

    def test_sse_decoded_events_must_be_objects_with_object_data(self) -> None:
        class Response:
            status = 200

            def __init__(self, payload: bytes):
                self.lines = iter((payload, b"\n", b""))

            def readline(self, limit: int) -> bytes:
                return next(self.lines)

            def close(self) -> None:
                return

        for payload in (
            b"data: []\n",
            b"data: 1\n",
            b'data: {"type":"test","data":[]}\n',
        ):
            with self.subTest(payload=payload):
                stream = EventStream.__new__(EventStream)
                stream.response = Response(payload)
                with self.assertRaisesRegex(TransportError, "invalid event"):
                    list(stream)

    def test_recovery_contract_is_exact_and_correlates_requested_message(self) -> None:
        valid = {
            "data": {
                "unfinishedProviderAttempt": False,
                "unfinishedCompaction": False,
                "unresolvedInput": True,
                "attemptedUnsettledInput": False,
                "requestedInputStatus": "unattempted",
                "otherUnresolvedInput": False,
            }
        }
        with patch.object(Client, "_request", return_value=valid) as request:
            recovery = self.client.chat_recovery("ses_chat", "msg_chat")
        self.assertEqual(recovery["requestedInputStatus"], "unattempted")
        self.assertEqual(request.call_args.kwargs["query"], {"messageID": "msg_chat"})

        for mutation in (
            {"requestedInputStatus": "not-requested"},
            {"requestedInputStatus": "unknown"},
            {"otherUnresolvedInput": 0},
            {"extra": False},
        ):
            data = {**valid["data"], **mutation}
            with self.subTest(mutation=mutation):
                with patch.object(Client, "_request", return_value={"data": data}):
                    with self.assertRaises(TransportError):
                        self.client.chat_recovery("ses_chat", "msg_chat")

    def test_prompt_reconciliation_can_admit_without_waking(self) -> None:
        admitted = {
            "data": {
                "admittedSeq": 4,
                "id": "msg_chat",
                "sessionID": "ses_chat",
                "prompt": {"text": "hello"},
                "delivery": "queue",
                "timeCreated": 1785283200000,
            }
        }
        with patch.object(Client, "_request", return_value=admitted) as request:
            self.client.prompt_chat(
                "ses_chat",
                "msg_chat",
                "hello",
                resume=False,
            )
        self.assertIs(request.call_args.args[2]["resume"], False)


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
        recovery = self.client.chat_recovery("ses_chat")
        message = self.client.chat_message("ses_chat", "msg_chat")
        title = self.client.ensure_chat_title("ses_chat", "msg_chat")
        active = self.client.active_chat_sessions()

        self.assertEqual(session["id"], "ses_chat")
        self.assertEqual(admitted["id"], "msg_chat")
        self.assertEqual(title, "Generated title")
        self.assertEqual(message["id"], "msg_chat")
        self.assertEqual(active, {"ses_active"})
        self.assertEqual(recovery["requestedInputStatus"], "not-requested")
        self.assertFalse(recovery["unresolvedInput"])
        created = Handler.requests[0][2]
        prompted = Handler.requests[1][2]
        assert created is not None
        assert prompted is not None
        self.assertEqual(created["purpose"], "terminal-chat")
        self.assertEqual(created["model"], {"providerID": "amazon-bedrock", "id": "opus"})
        self.assertEqual(prompted["delivery"], "queue")
        self.assertIs(prompted["resume"], True)
        self.assertEqual(prompted["prompt"], {"text": "hello"})

    def test_v2_task_session_uses_exact_identity_and_agent(self) -> None:
        session = self.client.create_task_session("ses_task", "build")
        request = Handler.requests[-1][2]
        self.assertEqual(session["id"], "ses_task")
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request["id"], "ses_task")
        self.assertEqual(request["agent"], "build")
        self.assertNotIn("purpose", request)

    def test_event_stream_parses_crlf_comments_and_multiline_data(self) -> None:
        stream = self.client.events()
        try:
            self.assertEqual(list(stream), [{"type": "test", "data": {}}])
        finally:
            stream.close()

    def test_redirect_is_rejected_without_following_or_forwarding_credentials(self) -> None:
        with self.assertRaises(HTTPResponseError) as caught:
            self.client._request("GET", "/redirect")

        self.assertEqual(caught.exception.status, 302)
        self.assertEqual([item[1] for item in Handler.requests], ["/redirect"])

    def test_http_status_errors_are_definite_and_typed(self) -> None:
        with self.assertRaises(NotFoundError) as missing:
            self.client._request("GET", "/not-found")
        with self.assertRaises(HTTPResponseError) as failed:
            self.client._request("GET", "/failure")

        self.assertEqual(missing.exception.status, 404)
        self.assertEqual(failed.exception.status, 500)
        self.assertNotIsInstance(failed.exception, TransportError)

    def test_json_responses_are_bounded_and_validated(self) -> None:
        with self.assertRaisesRegex(TransportError, "8 MiB"):
            self.client._request("GET", "/oversized")
        with self.assertRaisesRegex(TransportError, "invalid JSON"):
            self.client._request("GET", "/malformed-utf8")
        with self.assertRaisesRegex(TransportError, "invalid JSON"):
            self.client._request("GET", "/malformed-json")

    def test_requests_ignore_environment_proxy_configuration(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "HTTP_PROXY": "http://127.0.0.1:1",
                "http_proxy": "http://127.0.0.1:1",
                "NO_PROXY": "",
                "no_proxy": "",
            },
            clear=False,
        ):
            self.assertEqual(self.client.health(), {"healthy": True})

    def test_transport_failure_is_typed_as_uncertain(self) -> None:
        with patch(
            "opencode_bedrock.api.OPENER.open",
            side_effect=urllib.error.URLError("connection lost"),
        ):
            with self.assertRaises(TransportError):
                self.client.prompt_chat("ses_chat", "msg_chat", "hello")


if __name__ == "__main__":
    unittest.main()
