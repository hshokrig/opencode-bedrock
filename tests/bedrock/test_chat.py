from __future__ import annotations

import io
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from opencode_bedrock.chat import Chat, SessionLock, read_state, sanitize
from opencode_bedrock.errors import BedrockError, NotFoundError
from opencode_bedrock.service import Record
from tests.bedrock.support import isolated_environment


def session(workspace: Path, session_id: str = "ses_chat") -> dict:
    return {
        "id": session_id,
        "purpose": "terminal-chat",
        "agent": "chat",
        "title": "New session - now",
        "model": {"providerID": "amazon-bedrock", "id": "opus"},
        "location": {"directory": str(workspace)},
        "time": {"created": "2026-07-29T00:00:00Z"},
    }


class FakeClient:
    def __init__(self, workspace: Path):
        self.session = session(workspace)
        self.prompt_id = ""
        self.title_calls = 0
        self.history: list[dict] = []
        self.prompt_calls = 0
        self.fail_prompt_once = False
        self.event_calls = 0

    def create_chat_session(self, session_id: str) -> dict:
        self.session = {**self.session, "id": session_id}
        return self.session

    def get_chat_session(self, session_id: str) -> dict:
        if session_id != self.session["id"]:
            raise NotFoundError("not found")
        return self.session

    def active_chat_sessions(self) -> set[str]:
        return set()

    def chat_history(
        self, session_id: str, after: int | None = None, limit: int = 100
    ) -> dict:
        return {"data": self.history, "hasMore": False}

    def list_chat_sessions(self) -> dict:
        return {"data": [self.session], "cursor": {}}

    def prompt_chat(self, session_id: str, message_id: str, prompt: str) -> dict:
        self.prompt_id = message_id
        self.prompt_calls += 1
        if self.fail_prompt_once and self.prompt_calls == 1:
            raise BedrockError("response lost")
        return {"id": message_id}

    def events(self) -> "FakeEventStream":
        self.event_calls += 1
        return FakeEventStream(self, emit=self.event_calls == 1)

    def chat_message(self, session_id: str, message_id: str) -> dict:
        if message_id != self.prompt_id:
            raise NotFoundError("not found")
        return {"id": message_id, "type": "user", "text": "Hello"}

    def wait_chat(self, session_id: str) -> None:
        if self.event_calls:
            time.sleep(0.05)
        return

    def chat_messages(
        self,
        session_id: str,
        limit: int = 50,
        cursor: str | None = None,
        order: str = "desc",
    ) -> dict:
        if not self.prompt_id:
            return {"data": [], "cursor": {}}
        data = [
            {"id": self.prompt_id, "type": "user", "text": "Hello"},
            {
                "id": "msg_assistant",
                "type": "assistant",
                "finish": "stop",
                "content": [{"type": "text", "text": "Hello safely"}],
            },
        ]
        return {"data": data if order == "asc" else list(reversed(data)), "cursor": {}}

    def ensure_chat_title(self, session_id: str, first_message_id: str) -> str:
        self.title_calls += 1
        self.session = {**self.session, "title": "Friendly greeting"}
        return self.session["title"]

    def interrupt_chat(self, session_id: str) -> None:
        return


class FakeEventStream:
    def __init__(self, client: FakeClient, emit: bool):
        self.client = client
        self.emit = emit

    def __iter__(self):
        if not self.emit:
            return
        for _ in range(100):
            if self.client.prompt_id:
                break
            time.sleep(0.001)
        yield {
            "type": "session.next.provider-attempt.started",
            "data": {
                "sessionID": self.client.session["id"],
                "attemptID": "attempt_target",
                "inputMessageIDs": [self.client.prompt_id],
            },
        }
        yield {
            "type": "session.next.step.started",
            "data": {
                "sessionID": self.client.session["id"],
                "assistantMessageID": "msg_assistant",
            },
        }
        yield {
            "type": "session.next.text.started",
            "data": {
                "sessionID": self.client.session["id"],
                "assistantMessageID": "msg_assistant",
                "textID": "text_target",
            },
        }
        yield {
            "type": "session.next.text.delta",
            "data": {
                "sessionID": self.client.session["id"],
                "assistantMessageID": "msg_unrelated",
                "textID": "text_other",
                "delta": "UNRELATED",
            },
        }
        for delta in ["Hello ", "safely"]:
            yield {
                "type": "session.next.text.delta",
                "data": {
                    "sessionID": self.client.session["id"],
                    "assistantMessageID": "msg_assistant",
                    "textID": "text_target",
                    "delta": delta,
                },
            }

    def close(self) -> None:
        return


class TTYStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class ChatTests(unittest.TestCase):
    def test_terminal_sanitizer_removes_escape_controls_and_bidi_overrides(self) -> None:
        self.assertEqual(sanitize("safe\x1b]52;secret\x07\rX\u202e"), "safe]52;secretX")
        self.assertEqual(sanitize("one\n\ttwo"), "one\n\ttwo")

    def test_session_lock_is_nonblocking_and_released_on_close(self) -> None:
        with isolated_environment() as root:
            record = self.record(root)
            first = SessionLock(record, "ses_chat")
            try:
                with self.assertRaisesRegex(BedrockError, "already open"):
                    SessionLock(record, "ses_chat")
            finally:
                first.close()
            second = SessionLock(record, "ses_chat")
            second.close()

    def test_private_state_rejects_symlinks_and_group_readable_files(self) -> None:
        with isolated_environment() as root:
            target = root / "target.json"
            target.write_text('{"last_session": "ses_secret"}', encoding="utf-8")
            target.chmod(0o600)
            link = root / "chat.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(BedrockError, "private chat state"):
                read_state(link)
            link.unlink()
            target.chmod(0o640)
            with self.assertRaisesRegex(BedrockError, "mode-0600"):
                read_state(target)

    def test_chat_validates_identity_persists_selection_and_generates_title(self) -> None:
        with isolated_environment() as root:
            record = self.record(root)
            chat = Chat(record, no_stream=True)
            fake = FakeClient(root)
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                self.assertEqual(chat.session["purpose"], "terminal-chat")
                with patch("sys.stdout", new=io.StringIO()) as output:
                    chat.submit("Hello")
                self.assertIn("Hello safely", output.getvalue())
                self.assertIn("Friendly greeting", output.getvalue())
                self.assertEqual(fake.title_calls, 1)
                with self.assertRaisesRegex(BedrockError, "eligible"):
                    chat.validate({**fake.session, "agent": "build"})
            finally:
                chat.close()

    def test_chat_classifies_unfinished_provider_attempt_without_replaying_it(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            fake.history = [
                {
                    "type": "session.next.provider-attempt.started",
                    "data": {"attemptID": "attempt_one"},
                    "durable": {"seq": 1},
                }
            ]
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                self.assertTrue(chat.has_unfinished_provider_attempt())
                fake.history.append(
                    {
                        "type": "session.next.provider-attempt.ended",
                        "data": {"attemptID": "attempt_one", "outcome": "interrupted"},
                        "durable": {"seq": 2},
                    }
                )
                self.assertFalse(chat.has_unfinished_provider_attempt())
            finally:
                chat.close()

    def test_lost_admission_response_uses_one_exact_retry_with_the_same_id(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            fake.fail_prompt_once = True
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                with patch("sys.stdout", new=io.StringIO()) as output:
                    chat.submit("Hello")
                self.assertEqual(fake.prompt_calls, 2)
                self.assertIn("exact durable retry", output.getvalue())
                self.assertIn("Hello safely", output.getvalue())
            finally:
                chat.close()

    def test_streaming_correlates_and_buffers_only_target_text_deltas(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root))
            fake = FakeClient(root)
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                with patch("sys.stdout", new=TTYStringIO()) as output:
                    chat.submit("Hello")
                self.assertIn("claude> Hello safely", output.getvalue())
                self.assertNotIn("UNRELATED", output.getvalue())
            finally:
                chat.close()

    def test_recent_history_keeps_ten_complete_turns_and_ignores_pending(self) -> None:
        messages: list[dict] = []
        for index in range(11):
            messages.extend(
                [
                    {"id": f"user_{index}", "type": "user", "text": f"question {index}"},
                    {
                        "id": f"assistant_{index}",
                        "type": "assistant",
                        "finish": "stop",
                        "content": [{"type": "text", "text": f"answer {index}"}],
                    },
                ]
            )
        messages.append({"id": "pending", "type": "user", "text": "not settled"})

        recent = Chat._recent_complete(list(reversed(messages)))

        self.assertEqual(len(recent), 20)
        self.assertNotIn(("you", "question 0"), recent)
        self.assertIn(("you", "question 10"), recent)
        self.assertNotIn(("you", "not settled"), recent)

    def test_render_never_exceeds_two_hundred_lines(self) -> None:
        with patch("sys.stdout", new=io.StringIO()) as output:
            Chat._render([("claude", "\n".join(str(index) for index in range(250)))])
        self.assertEqual(len(output.getvalue().splitlines()), 200)

    @staticmethod
    def record(workspace: Path) -> Record:
        return Record(
            key="chat-test",
            project=None,
            workspace=str(workspace),
            pid=os.getpid(),
            process_start=0,
            port=1,
            password="test",
            started_at=0,
            headless_policy="approval",
            region="eu-north-1",
            inference_profile="profile",
            agent_models={},
            endpoint=None,
            opencode="/bin/true",
            log=str(workspace / "service.log"),
        )


if __name__ == "__main__":
    unittest.main()
