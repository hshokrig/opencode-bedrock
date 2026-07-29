from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BedrockError

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class Client:
    port: int
    password: str
    workspace: Path

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/global/health", timeout=1)

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
    ) -> Any:
        query = urllib.parse.urlencode({"directory": str(self.workspace)}) if workspace else ""
        url = f"http://127.0.0.1:{self.port}{route}" + (f"?{query}" if query else "")
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
            detail = error.read().decode("utf-8", errors="replace")
            raise BedrockError(
                f"OpenCode API {method} {route} failed ({error.code}): {detail}"
            ) from error
        except (TimeoutError, urllib.error.URLError) as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            raise BedrockError(
                f"cannot reach OpenCode service on 127.0.0.1:{self.port}: {reason}"
            ) from error
