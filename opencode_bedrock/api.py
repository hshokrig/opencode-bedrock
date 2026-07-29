from __future__ import annotations

import base64
import http.client
import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BedrockError, HTTPResponseError, NotFoundError, TransportError


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    NoRedirectHandler(),
)
SSE_LIMIT = 1024 * 1024
# Ordinary API responses may include bounded chat history, but must not grow without limit.
JSON_RESPONSE_LIMIT = 8 * 1024 * 1024


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
        except (OSError, http.client.HTTPException) as error:
            self.connection.close()
            raise TransportError(f"cannot open OpenCode event stream: {error}") from error
        if self.response.status != 200:
            status = self.response.status
            self.close()
            message = f"OpenCode event stream failed ({status})"
            if status == 404:
                raise NotFoundError(message, status)
            raise HTTPResponseError(message, status)
        if self.connection.sock:
            self.connection.sock.settimeout(120)

    def __iter__(self):
        data: list[bytes] = []
        size = 0
        while True:
            try:
                line = self.response.readline(SSE_LIMIT + 1)
            except (OSError, socket.timeout, http.client.HTTPException) as error:
                raise TransportError(f"OpenCode event stream stalled: {error}") from error
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
                        event = json.loads(b"\n".join(data).decode("utf-8"))
                    except (
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                        RecursionError,
                    ) as error:
                        raise TransportError(
                            "OpenCode event stream returned invalid JSON"
                        ) from error
                    if (
                        not isinstance(event, dict)
                        or not isinstance(event.get("type"), str)
                        or not event["type"]
                        or not isinstance(event.get("data"), dict)
                    ):
                        raise TransportError(
                            "OpenCode event stream returned an invalid event"
                        )
                    yield event
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

    def __post_init__(self) -> None:
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise BedrockError("OpenCode service port must be an integer from 1 to 65535")

    def health(self) -> dict[str, Any]:
        return self._expect_object(
            self._request("GET", "/global/health", timeout=1),
            "health",
        )

    def events(self) -> EventStream:
        return EventStream(self.port, self.password)

    def create_session(self, title: str) -> dict[str, Any]:
        return self._expect_object(
            self._request("POST", "/session", {"title": title}, workspace=True),
            "session creation",
        )

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
        return self._expect_object(
            self._request("GET", "/session/status", workspace=True),
            "session status",
        )

    def list_chat_sessions(
        self, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        self._limit(limit)
        if cursor is not None:
            self._segment(cursor)
        query: dict[str, str | int] = {
            "directory": str(self.workspace),
            "purpose": "terminal-chat",
            "limit": limit,
            "order": "desc",
        }
        if cursor:
            query = {"cursor": cursor, "limit": limit}
        return self._expect_page(
            self._request("GET", "/api/session", query=query),
            "session listing",
            item=self._expect_session,
        )

    def create_chat_session(self, session_id: str) -> dict[str, Any]:
        self._segment(session_id)
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
        session = self._expect_data_object(response, "chat session creation")
        if session.get("id") != session_id:
            raise TransportError(
                "OpenCode API did not confirm the requested chat session ID"
            )
        return self._expect_session(session, "chat session creation")

    def get_chat_session(self, session_id: str) -> dict[str, Any]:
        session = self._expect_data_object(
            self._request("GET", f"/api/session/{self._segment(session_id)}"),
            "chat session lookup",
        )
        if session.get("id") != session_id:
            raise TransportError(
                "OpenCode API did not confirm the requested chat session ID"
            )
        return self._expect_session(session, "chat session lookup")

    def active_chat_sessions(self) -> set[str]:
        data = self._expect_data_object(
            self._request("GET", "/api/session/active"),
            "active chat session listing",
        )
        if not all(
            self._is_identifier(session_id)
            and isinstance(status, dict)
            and status == {"type": "running"}
            for session_id, status in data.items()
        ):
            raise TransportError(
                "OpenCode API returned an invalid successful active chat session listing response"
            )
        return set(data)

    def prompt_chat(
        self,
        session_id: str,
        message_id: str,
        prompt: str,
        resume: bool = True,
    ) -> dict[str, Any]:
        self._segment(message_id)
        if type(resume) is not bool:
            raise BedrockError("prompt resume mode must be a boolean")
        message = self._expect_data_object(
            self._request(
                "POST",
                f"/api/session/{self._segment(session_id)}/prompt",
                {
                    "id": message_id,
                    "prompt": {"text": prompt},
                    "delivery": "queue",
                    "resume": resume,
                },
            ),
            "prompt admission",
        )
        if message.get("id") != message_id:
            raise TransportError(
                "OpenCode API did not confirm the requested durable prompt message ID"
            )
        required = {
            "admittedSeq",
            "id",
            "sessionID",
            "prompt",
            "delivery",
            "timeCreated",
        }
        if (
            not required <= set(message)
            or set(message) - required - {"promotedSeq"}
            or message.get("sessionID") != session_id
            or message.get("prompt") != {"text": prompt}
            or message.get("delivery") != "queue"
            or type(message.get("admittedSeq")) is not int
            or message["admittedSeq"] < 0
            or type(message.get("timeCreated")) not in {int, float}
            or not math.isfinite(message["timeCreated"])
            or (
                "promotedSeq" in message
                and (
                    type(message["promotedSeq"]) is not int
                    or message["promotedSeq"] < 0
                )
            )
        ):
            raise TransportError(
                "OpenCode API returned an invalid successful prompt admission response"
            )
        return message

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
        self._limit(limit, 200)
        if cursor is not None:
            self._segment(cursor)
        if order not in {"asc", "desc"}:
            raise BedrockError("chat message order must be asc or desc")
        query: dict[str, str | int] = {"limit": limit, "order": order}
        if cursor:
            query = {"cursor": cursor, "limit": limit}
        return self._expect_page(
            self._request(
                "GET",
                f"/api/session/{self._segment(session_id)}/message",
                query=query,
            ),
            "chat message listing",
            item=self._expect_message,
        )

    def chat_recovery(
        self,
        session_id: str,
        message_id: str | None = None,
    ) -> dict[str, bool | str]:
        query = None
        if message_id is not None:
            self._segment(message_id)
            query = {"messageID": message_id}
        data = self._expect_data_object(
            self._request(
                "GET",
                f"/api/session/{self._segment(session_id)}/recovery",
                query=query,
            ),
            "chat recovery state",
        )
        keys = {
            "unfinishedProviderAttempt",
            "unfinishedCompaction",
            "unresolvedInput",
            "attemptedUnsettledInput",
            "requestedInputStatus",
            "otherUnresolvedInput",
        }
        boolean_keys = keys - {"requestedInputStatus"}
        statuses = {
            "not-requested",
            "absent",
            "unattempted",
            "attempted",
            "settled",
        }
        if (
            set(data) != keys
            or not all(isinstance(data[key], bool) for key in boolean_keys)
            or data["requestedInputStatus"] not in statuses
            or (message_id is None and data["requestedInputStatus"] != "not-requested")
            or (message_id is not None and data["requestedInputStatus"] == "not-requested")
        ):
            raise TransportError(
                "OpenCode API returned an invalid successful chat recovery response"
            )
        return data

    def chat_message(self, session_id: str, message_id: str) -> dict[str, Any]:
        message = self._expect_data_object(
            self._request(
                "GET",
                f"/api/session/{self._segment(session_id)}/message/{self._segment(message_id)}",
            ),
            "chat message lookup",
        )
        if message.get("id") != message_id:
            raise TransportError(
                "OpenCode API did not confirm the requested chat message ID"
            )
        return self._expect_message(message, "chat message lookup")

    def chat_history(
        self,
        session_id: str,
        after: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._limit(limit, 100)
        if after is not None and (type(after) is not int or after < 0):
            raise BedrockError("chat history offset must be a non-negative integer")
        query: dict[str, str | int] = {"limit": limit}
        if after is not None:
            query["after"] = after
        response = self._expect_object(
            self._request(
                "GET",
                f"/api/session/{self._segment(session_id)}/history",
                query=query,
            ),
            "chat history",
        )
        if (
            set(response) != {"data", "hasMore"}
            or not isinstance(response["data"], list)
            or not all(isinstance(event, dict) for event in response["data"])
            or not isinstance(response["hasMore"], bool)
        ):
            raise TransportError(
                "OpenCode API returned an invalid successful chat history response"
            )
        return response

    def compare_and_set_title(
        self, session_id: str, expected: str, title: str
    ) -> bool:
        response = self._request(
            "POST",
            f"/api/session/{self._segment(session_id)}/title",
            {"expected": expected, "title": title},
        )
        data = self._expect_data_object(response, "title update")
        if not isinstance(data.get("updated"), bool):
            raise TransportError(
                "OpenCode API returned an invalid successful title update response"
            )
        return data["updated"]

    def ensure_chat_title(self, session_id: str, first_message_id: str) -> str:
        self._segment(first_message_id)
        response = self._request(
            "POST",
            f"/api/session/{self._segment(session_id)}/title/ensure",
            {"firstMessageID": first_message_id},
            timeout=120,
        )
        data = self._expect_data_object(response, "title generation")
        if not isinstance(data.get("title"), str):
            raise TransportError(
                "OpenCode API returned an invalid successful title generation response"
            )
        return data["title"]

    def permissions(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/permission", workspace=True)
        if not isinstance(response, list) or not all(
            isinstance(item, dict) for item in response
        ):
            raise TransportError(
                "OpenCode API returned an invalid successful permission listing response"
            )
        return response

    def reply(self, request_id: str, reply: str, message: str | None = None) -> bool:
        body: dict[str, str] = {"reply": reply}
        if message:
            body["message"] = message
        response = self._request(
            "POST",
            f"/permission/{urllib.parse.quote(request_id)}/reply",
            body,
            workspace=True,
        )
        if not isinstance(response, bool):
            raise TransportError(
                "OpenCode API returned an invalid successful permission reply response"
            )
        return response

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
                payload = response.read(JSON_RESPONSE_LIMIT + 1)
                if len(payload) > JSON_RESPONSE_LIMIT:
                    raise TransportError(
                        "OpenCode API returned an unconfirmed response exceeding "
                        "the 8 MiB limit"
                    )
                if no_content:
                    if payload:
                        raise TransportError(
                            "OpenCode API returned an invalid non-empty successful "
                            "no-content response"
                        )
                    return None
                if not payload:
                    return None
                try:
                    return json.loads(payload)
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    RecursionError,
                ) as error:
                    raise TransportError(
                        "OpenCode API returned an unconfirmed invalid JSON response"
                    ) from error
        except urllib.error.HTTPError as error:
            try:
                error.read(8193)
            except Exception:
                # The status is authoritative even when its optional error body is not.
                pass
            message = f"OpenCode API {method} {route} failed ({error.code})"
            if error.code == 404:
                raise NotFoundError(message, error.code) from error
            raise HTTPResponseError(message, error.code) from error
        except (
            TimeoutError,
            http.client.HTTPException,
            urllib.error.URLError,
            OSError,
        ) as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            raise TransportError(
                f"cannot reach OpenCode service on 127.0.0.1:{self.port}: {reason}"
            ) from error

    @staticmethod
    def _expect_object(value: Any, operation: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TransportError(
                f"OpenCode API returned an invalid successful {operation} response"
            )
        return value

    @classmethod
    def _expect_data_object(cls, value: Any, operation: str) -> dict[str, Any]:
        response = cls._expect_object(value, operation)
        if "data" not in response:
            raise TransportError(
                f"OpenCode API returned an invalid successful {operation} response"
            )
        return cls._expect_object(response["data"], operation)

    @classmethod
    def _expect_page(
        cls,
        value: Any,
        operation: str,
        *,
        item: Any,
    ) -> dict[str, Any]:
        response = cls._expect_object(value, operation)
        data = response.get("data")
        cursor = response.get("cursor", {})
        if (
            set(response) != {"data", "cursor"}
            or not isinstance(data, list)
            or not all(isinstance(entry, dict) for entry in data)
            or not isinstance(cursor, dict)
            or set(cursor) - {"previous", "next"}
            or (
                "next" in cursor
                and not cls._is_identifier(cursor["next"])
            )
            or (
                "previous" in cursor
                and not cls._is_identifier(cursor["previous"])
            )
        ):
            raise TransportError(
                f"OpenCode API returned an invalid successful {operation} response"
            )
        checked = [item(entry, operation) for entry in data]
        return {"data": checked, "cursor": cursor}

    @classmethod
    def _expect_session(cls, value: Any, operation: str) -> dict[str, Any]:
        session = cls._expect_object(value, operation)
        model = session.get("model")
        location = session.get("location")
        time = session.get("time")
        if (
            not cls._is_session_id(session.get("id"))
            or not isinstance(session.get("projectID"), str)
            or not isinstance(session.get("title"), str)
            or not cls._is_finite(session.get("cost"))
            or not cls._is_tokens(session.get("tokens"))
            or not isinstance(time, dict)
            or not cls._is_time_value(time.get("created"))
            or not cls._is_time_value(time.get("updated"))
            or (
                "updated" in time
                and not cls._is_time_value(time.get("updated"))
            )
            or (
                "archived" in time
                and not cls._is_time_value(time.get("archived"))
            )
            or (
                "purpose" in session
                and not isinstance(session.get("purpose"), str)
            )
            or (
                "parentID" in session
                and not cls._is_session_id(session.get("parentID"))
            )
            or (
                "agent" in session
                and not cls._is_identifier(session.get("agent"))
            )
            or ("model" in session and not cls._is_model(model))
            or (
                not isinstance(location, dict)
                or not isinstance(location.get("directory"), str)
                or (
                    "workspaceID" in location
                    and not cls._is_identifier(location.get("workspaceID"))
                )
            )
        ):
            raise TransportError(
                f"OpenCode API returned an invalid successful {operation} response"
            )
        return session

    @classmethod
    def _expect_message(cls, value: Any, operation: str) -> dict[str, Any]:
        message = cls._expect_object(value, operation)
        message_type = message.get("type")
        time = message.get("time")
        if (
            not cls._is_message_id(message.get("id"))
            or message_type
            not in {
                "agent-switched",
                "model-switched",
                "user",
                "synthetic",
                "system",
                "shell",
                "assistant",
                "compaction",
            }
            or not isinstance(time, dict)
            or not cls._is_time_value(time.get("created"))
            or (
                "metadata" in message
                and not isinstance(message.get("metadata"), dict)
            )
            or (
                "completed" in time
                and not cls._is_time_value(time.get("completed"))
            )
            or (
                message_type == "agent-switched"
                and not cls._is_identifier(message.get("agent"))
            )
            or (
                message_type == "model-switched"
                and not cls._is_model(message.get("model"))
            )
            or (
                message_type in {"user", "synthetic", "system"}
                and not isinstance(message.get("text"), str)
            )
            or (
                message_type == "user"
                and (
                    not cls._is_optional_list(
                        message,
                        "files",
                        cls._is_file_attachment,
                    )
                    or not cls._is_optional_list(
                        message,
                        "agents",
                        cls._is_agent_attachment,
                    )
                )
            )
            or (
                message_type == "synthetic"
                and not cls._is_session_id(message.get("sessionID"))
            )
            or (
                message_type == "shell"
                and (
                    not isinstance(message.get("callID"), str)
                    or not isinstance(message.get("command"), str)
                    or not isinstance(message.get("output"), str)
                )
            )
            or (
                message_type == "assistant"
                and not cls._is_assistant_message(message)
            )
            or (
                message_type == "compaction"
                and (
                    message.get("reason") not in {"auto", "manual"}
                    or not isinstance(message.get("summary"), str)
                    or not isinstance(message.get("recent"), str)
                    or (
                        "retainedMessageIDs" in message
                        and (
                            not isinstance(message.get("retainedMessageIDs"), list)
                            or not all(
                                cls._is_message_id(item)
                                for item in message["retainedMessageIDs"]
                            )
                        )
                    )
                )
            )
        ):
            raise TransportError(
                f"OpenCode API returned an invalid successful {operation} response"
            )
        return message

    @classmethod
    def _is_assistant_message(cls, message: dict[str, Any]) -> bool:
        content = message.get("content")
        if (
            not cls._is_identifier(message.get("agent"))
            or not cls._is_model(message.get("model"))
            or not isinstance(content, list)
        ):
            return False
        if "error" in message and not cls._is_unknown_error(message.get("error")):
            return False
        if (
            "finish" in message
            and not isinstance(message.get("finish"), str)
            or "cost" in message
            and not cls._is_finite(message.get("cost"))
            or "tokens" in message
            and not cls._is_tokens(message.get("tokens"))
            or "snapshot" in message
            and not cls._is_snapshot(message.get("snapshot"))
        ):
            return False
        return all(cls._is_assistant_content(part) for part in content)

    @classmethod
    def _is_assistant_content(cls, value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        if value.get("type") == "text":
            return isinstance(value.get("id"), str) and isinstance(
                value.get("text"), str
            )
        if value.get("type") == "reasoning":
            return (
                isinstance(value.get("id"), str)
                and isinstance(value.get("text"), str)
                and (
                    "providerMetadata" not in value
                    or cls._is_provider_metadata(value.get("providerMetadata"))
                )
                and (
                    "time" not in value
                    or cls._is_created_time(value.get("time"))
                )
            )
        if value.get("type") != "tool":
            return False
        time = value.get("time")
        return (
            isinstance(value.get("id"), str)
            and isinstance(value.get("name"), str)
            and cls._is_created_time(time, optional=("ran", "completed", "pruned"))
            and cls._is_tool_state(value.get("state"))
            and (
                "provider" not in value
                or cls._is_tool_provider(value.get("provider"))
            )
        )

    @classmethod
    def _is_tool_state(cls, value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        status = value.get("status")
        if status == "pending":
            return isinstance(value.get("input"), str)
        if status not in {"running", "completed", "error"}:
            return False
        if (
            not isinstance(value.get("input"), dict)
            or not isinstance(value.get("structured"), dict)
            or not isinstance(value.get("content"), list)
            or not all(cls._is_tool_content(item) for item in value["content"])
        ):
            return False
        if status == "completed":
            return (
                cls._is_optional_list(
                    value,
                    "attachments",
                    cls._is_file_attachment,
                )
                and cls._is_optional_list(
                    value,
                    "outputPaths",
                    lambda item: isinstance(item, str),
                )
            )
        if status == "error":
            return cls._is_unknown_error(value.get("error"))
        return True

    @classmethod
    def _is_tool_provider(cls, value: Any) -> bool:
        return (
            isinstance(value, dict)
            and isinstance(value.get("executed"), bool)
            and (
                "metadata" not in value
                or cls._is_provider_metadata(value.get("metadata"))
            )
            and (
                "resultMetadata" not in value
                or cls._is_provider_metadata(value.get("resultMetadata"))
            )
        )

    @staticmethod
    def _is_provider_metadata(value: Any) -> bool:
        return isinstance(value, dict) and all(
            isinstance(item, dict) for item in value.values()
        )

    @staticmethod
    def _is_tool_content(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        if value.get("type") == "text":
            return isinstance(value.get("text"), str)
        return (
            value.get("type") == "file"
            and isinstance(value.get("uri"), str)
            and isinstance(value.get("mime"), str)
            and (
                "name" not in value
                or isinstance(value.get("name"), str)
            )
        )

    @classmethod
    def _is_file_attachment(cls, value: Any) -> bool:
        return (
            isinstance(value, dict)
            and isinstance(value.get("uri"), str)
            and isinstance(value.get("mime"), str)
            and (
                "name" not in value
                or isinstance(value.get("name"), str)
            )
            and (
                "description" not in value
                or isinstance(value.get("description"), str)
            )
            and (
                "source" not in value
                or cls._is_source(value.get("source"))
            )
        )

    @classmethod
    def _is_agent_attachment(cls, value: Any) -> bool:
        return (
            isinstance(value, dict)
            and isinstance(value.get("name"), str)
            and (
                "source" not in value
                or cls._is_source(value.get("source"))
            )
        )

    @classmethod
    def _is_source(cls, value: Any) -> bool:
        return (
            isinstance(value, dict)
            and cls._is_finite(value.get("start"))
            and cls._is_finite(value.get("end"))
            and isinstance(value.get("text"), str)
        )

    @classmethod
    def _is_snapshot(cls, value: Any) -> bool:
        return (
            isinstance(value, dict)
            and (
                "start" not in value
                or isinstance(value.get("start"), str)
            )
            and (
                "end" not in value
                or isinstance(value.get("end"), str)
            )
            and cls._is_optional_list(
                value,
                "files",
                lambda item: isinstance(item, str),
            )
        )

    @classmethod
    def _is_tokens(cls, value: Any) -> bool:
        return (
            isinstance(value, dict)
            and all(
                cls._is_finite(value.get(key))
                for key in ("input", "output", "reasoning")
            )
            and isinstance(value.get("cache"), dict)
            and cls._is_finite(value["cache"].get("read"))
            and cls._is_finite(value["cache"].get("write"))
        )

    @staticmethod
    def _is_unknown_error(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and value.get("type") == "unknown"
            and isinstance(value.get("message"), str)
        )

    @classmethod
    def _is_created_time(
        cls,
        value: Any,
        optional: tuple[str, ...] = ("completed",),
    ) -> bool:
        return (
            isinstance(value, dict)
            and cls._is_time_value(value.get("created"))
            and all(
                key not in value or cls._is_time_value(value.get(key))
                for key in optional
            )
        )

    @staticmethod
    def _is_optional_list(
        value: dict[str, Any],
        key: str,
        item: Any,
    ) -> bool:
        return (
            key not in value
            or isinstance(value.get(key), list)
            and all(item(entry) for entry in value[key])
        )

    @classmethod
    def _is_model(cls, value: Any) -> bool:
        return (
            isinstance(value, dict)
            and cls._is_identifier(value.get("providerID"))
            and cls._is_identifier(value.get("id"))
            and (
                "variant" not in value
                or isinstance(value.get("variant"), str)
            )
        )

    @staticmethod
    def _is_time_value(value: Any) -> bool:
        return Client._is_finite(value)

    @staticmethod
    def _is_finite(value: Any) -> bool:
        return type(value) in {int, float} and math.isfinite(value)

    @staticmethod
    def _is_message_id(value: Any) -> bool:
        return isinstance(value, str) and value.startswith("msg_")

    @staticmethod
    def _is_session_id(value: Any) -> bool:
        return isinstance(value, str) and value.startswith("ses")

    @staticmethod
    def _is_identifier(value: Any) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and all(character.isprintable() for character in value)
        )

    @classmethod
    def _segment(cls, value: str) -> str:
        if not cls._is_identifier(value):
            raise BedrockError("invalid empty or control-bearing identifier")
        return urllib.parse.quote(value, safe="")

    @staticmethod
    def _limit(value: int, maximum: int | None = None) -> None:
        if type(value) is not int or value < 1 or (
            maximum is not None and value > maximum
        ):
            suffix = f" from 1 to {maximum}" if maximum is not None else " above zero"
            raise BedrockError(f"API page limit must be an integer{suffix}")
