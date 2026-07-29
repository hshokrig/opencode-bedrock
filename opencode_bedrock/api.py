from __future__ import annotations

import base64
import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BedrockError, NotFoundError

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SSE_LIMIT = 1024 * 1024


class EventStream:
    def __init__(self, port: int, password: str):
        self.connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        try:
            self.connection.request(
                "GET",
                "/api/event",
                headers={
                    "Authorization": f"Basic {token}",
                    "Accept": "text/event-stream",
                },
            )
            self.response = self.connection.getresponse()
        except OSError as error:
            self.connection.close()
            raise BedrockError(f"cannot open OpenCode event stream: {error}") from error
        if self.response.status != 200:
            self.response.read(8193)
            status = self.response.status
            self.close()
            raise BedrockError(f"OpenCode event stream failed ({status})")
        if self.connection.sock:
            self.connection.sock.settimeout(120)

    def __iter__(self):
        data: list[bytes] = []
        size = 0
        while True:
            try:
                line = self.response.readline(SSE_LIMIT + 1)
            except (OSError, socket.timeout) as error:
                raise BedrockError(f"OpenCode event stream stalled: {error}") from error
            if not line:
                if data:
                    raise BedrockError("OpenCode event stream ended inside an event")
                return
            size += len(line)
            if size > SSE_LIMIT:
                raise BedrockError("OpenCode event exceeds the 1 MiB limit")
            line = line.rstrip(b"\r\n")
            if not line:
                if data:
                    try:
                        yield json.loads(b"\n".join(data).decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise BedrockError("OpenCode event stream returned invalid JSON") from error
                data = []
                size = 0
                continue
            if line.startswith(b":"):
                continue
            if line.startswith(b"data:"):
                data.append(line[5:].lstrip(b" "))

    def close(self) -> None:
        try:
            self.response.close()
        finally:
            self.connection.close()


@dataclass(frozen=True)
class Client:
    port: int
    password: str
    workspace: Path

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/global/health", timeout=1)

    def events(self) -> EventStream:
        return EventStream(self.port, self.password)

    def create_session(self, title: str) -> dict[str, Any]:
        return self._request("POST", "/session", {"title": title}, workspace=True)

    def prompt_async(self, session_id: str, prompt: str, agent: str) -> None:
        self._request(
            "POST",
            f"/session/{urllib.parse.quote(session_id)}/prompt_async",
            {
                "agent": agent,
                "parts": [{"type": "text", "text": prompt}],
            },
            workspace=True,
            no_content=True,
        )

    def statuses(self) -> dict[str, Any]:
        return self._request("GET", "/session/status", workspace=True)

    def list_chat_sessions(
        self, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        query: dict[str, str | int] = {
            "directory": str(self.workspace),
            "purpose": "terminal-chat",
            "limit": limit,
            "order": "desc",
        }
        if cursor:
            query = {"cursor": cursor, "limit": limit}
        return self._request("GET", "/api/session", query=query)

    def create_chat_session(self, session_id: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/session",
            {
                "id": session_id,
                "purpose": "terminal-chat",
                "agent": "chat",
                "model": {"providerID": "amazon-bedrock", "id": "opus"},
                "location": {"directory": str(self.workspace)},
            },
        )
        return response["data"]

    def get_chat_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/session/{self._segment(session_id)}")["data"]

    def active_chat_sessions(self) -> set[str]:
        return set(self._request("GET", "/api/session/active")["data"])

    def prompt_chat(self, session_id: str, message_id: str, prompt: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/session/{self._segment(session_id)}/prompt",
            {
                "id": message_id,
                "prompt": {"text": prompt},
                "delivery": "queue",
            },
        )["data"]

    def wait_chat(self, session_id: str, timeout: float = 600) -> None:
        self._request(
            "POST",
            f"/api/session/{self._segment(session_id)}/wait",
            no_content=True,
            timeout=timeout,
        )

    def interrupt_chat(self, session_id: str) -> None:
        self._request(
            "POST",
            f"/api/session/{self._segment(session_id)}/interrupt",
            no_content=True,
        )

    def chat_messages(
        self,
        session_id: str,
        limit: int = 50,
        cursor: str | None = None,
        order: str = "desc",
    ) -> dict[str, Any]:
        query: dict[str, str | int] = {"limit": limit, "order": order}
        if cursor:
            query = {"cursor": cursor, "limit": limit}
        return self._request(
            "GET",
            f"/api/session/{self._segment(session_id)}/message",
            query=query,
        )

    def chat_message(self, session_id: str, message_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/session/{self._segment(session_id)}/message/{self._segment(message_id)}",
        )["data"]

    def chat_history(
        self,
        session_id: str,
        after: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        query: dict[str, str | int] = {"limit": limit}
        if after is not None:
            query["after"] = after
        return self._request(
            "GET",
            f"/api/session/{self._segment(session_id)}/history",
            query=query,
        )

    def compare_and_set_title(
        self, session_id: str, expected: str, title: str
    ) -> bool:
        response = self._request(
            "POST",
            f"/api/session/{self._segment(session_id)}/title",
            {"expected": expected, "title": title},
        )
        return bool(response["data"]["updated"])

    def ensure_chat_title(self, session_id: str, first_message_id: str) -> str:
        response = self._request(
            "POST",
            f"/api/session/{self._segment(session_id)}/title/ensure",
            {"firstMessageID": first_message_id},
            timeout=120,
        )
        return str(response["data"]["title"])

    def permissions(self) -> list[dict[str, Any]]:
        return self._request("GET", "/permission", workspace=True)

    def reply(self, request_id: str, reply: str, message: str | None = None) -> bool:
        body: dict[str, str] = {"reply": reply}
        if message:
            body["message"] = message
        return self._request(
            "POST",
            f"/permission/{urllib.parse.quote(request_id)}/reply",
            body,
            workspace=True,
        )

    def _request(
        self,
        method: str,
        route: str,
        body: dict[str, Any] | None = None,
        workspace: bool = False,
        no_content: bool = False,
        timeout: float = 10,
        query: dict[str, str | int] | None = None,
    ) -> Any:
        parameters = {"directory": str(self.workspace)} if workspace else {}
        parameters.update(query or {})
        encoded = urllib.parse.urlencode(parameters)
        url = f"http://127.0.0.1:{self.port}{route}" + (f"?{encoded}" if encoded else "")
        data = json.dumps(body).encode() if body is not None else None
        token = base64.b64encode(f"opencode:{self.password}".encode()).decode()
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Basic {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with OPENER.open(request, timeout=timeout) as response:
                payload = response.read()
                if no_content or not payload:
                    return None
                return json.loads(payload)
        except urllib.error.HTTPError as error:
            error.read(8193)
            failure = NotFoundError if error.code == 404 else BedrockError
            raise failure(f"OpenCode API {method} {route} failed ({error.code})") from error
        except (TimeoutError, urllib.error.URLError) as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            raise BedrockError(
                f"cannot reach OpenCode service on 127.0.0.1:{self.port}: {reason}"
            ) from error

    @staticmethod
    def _segment(value: str) -> str:
        if not value or any(ord(character) < 32 for character in value):
            raise BedrockError("invalid empty or control-bearing identifier")
        return urllib.parse.quote(value, safe="")
