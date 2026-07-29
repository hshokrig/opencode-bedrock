from __future__ import annotations

import io
import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from opencode_bedrock.chat import (
    MESSAGE_BATCH_BYTES,
    PROMPT_BYTES,
    Chat,
    SessionLock,
    read_state,
    sanitize,
)
from opencode_bedrock.errors import (
    BedrockError,
    HTTPResponseError,
    JSONWriteError,
    NotFoundError,
    TransportError,
)
from opencode_bedrock.io import write_json
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
        self.history_calls = 0
        self.projected_messages: list[dict] | None = None
        self.prompt_calls = 0
        self.fail_prompt_once = False
        self.prompt_failures: list[BaseException] = []
        self.prompt_requests: list[tuple[str, str, str]] = []
        self.prompt_resumes: list[bool] = []
        self.create_failures: list[BaseException] = []
        self.create_ids: list[str] = []
        self.event_calls = 0
        self.wait_calls = 0
        self.wait_failures: list[BaseException] = []
        self.active: set[str] = set()
        self.recovery_calls: list[tuple[str, str | None]] = []
        self.requested_statuses: dict[str, str] = {}
        self.recovery = {
            "unfinishedProviderAttempt": False,
            "unfinishedCompaction": False,
            "unresolvedInput": False,
            "attemptedUnsettledInput": False,
            "requestedInputStatus": "not-requested",
            "otherUnresolvedInput": False,
        }

    def create_chat_session(self, session_id: str) -> dict:
        self.create_ids.append(session_id)
        if self.create_failures:
            raise self.create_failures.pop(0)
        self.session = {**self.session, "id": session_id}
        return self.session

    def get_chat_session(self, session_id: str) -> dict:
        if session_id != self.session["id"]:
            raise NotFoundError("not found")
        return self.session

    def active_chat_sessions(self) -> set[str]:
        return self.active

    def chat_history(
        self, session_id: str, after: int | None = None, limit: int = 100
    ) -> dict:
        self.history_calls += 1
        return {"data": self.history, "hasMore": False}

    def chat_recovery(
        self, session_id: str, message_id: str | None = None
    ) -> dict[str, bool | str]:
        self.recovery_calls.append((session_id, message_id))
        active: set[str] = set()
        for event in self.history:
            attempt_id = event.get("data", {}).get("attemptID")
            if event.get("type") == "session.next.provider-attempt.started":
                active.add(str(attempt_id))
            if event.get("type") == "session.next.provider-attempt.ended":
                active.discard(str(attempt_id))
        requested = "not-requested"
        if message_id is not None:
            configured = self.requested_statuses.get(
                message_id, str(self.recovery["requestedInputStatus"])
            )
            requested = str(configured) if configured != "not-requested" else "absent"
            if configured == "not-requested" and message_id == self.prompt_id:
                requested = (
                    "unattempted"
                    if self.recovery["unresolvedInput"]
                    else "settled"
                )
        return {
            **self.recovery,
            "unfinishedProviderAttempt": bool(active)
            or self.recovery["unfinishedProviderAttempt"],
            "requestedInputStatus": requested,
        }

    def list_chat_sessions(self) -> dict:
        return {"data": [self.session], "cursor": {}}

    def prompt_chat(
        self,
        session_id: str,
        message_id: str,
        prompt: str,
        resume: bool = True,
    ) -> dict:
        self.prompt_id = message_id
        self.prompt_calls += 1
        self.prompt_requests.append((session_id, message_id, prompt))
        self.prompt_resumes.append(resume)
        if self.prompt_failures:
            raise self.prompt_failures.pop(0)
        if self.fail_prompt_once and self.prompt_calls == 1:
            raise TransportError("response lost")
        return {"id": message_id}

    def events(self) -> "FakeEventStream":
        self.event_calls += 1
        return FakeEventStream(self, emit=self.event_calls == 1)

    def chat_message(self, session_id: str, message_id: str) -> dict:
        if message_id != self.prompt_id:
            raise NotFoundError("not found")
        return {"id": message_id, "type": "user", "text": "Hello"}

    def wait_chat(self, session_id: str) -> None:
        self.wait_calls += 1
        if self.wait_failures:
            raise self.wait_failures.pop(0)
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
        if self.projected_messages is not None:
            return {"data": self.projected_messages, "cursor": {}}
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

    def test_private_state_rejects_unknown_keys_and_unsafe_identifiers(self) -> None:
        with isolated_environment() as root:
            path = root / "chat.json"
            for value in (
                {"last_session": "ses_ok", "extra": "unsafe"},
                {"last_session": ""},
                {"pending_creation": "ses_\nunsafe"},
            ):
                with self.subTest(value=value):
                    write_json(path, value)
                    with self.assertRaisesRegex(BedrockError, "state is invalid"):
                        read_state(path)

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
                with self.assertRaisesRegex(BedrockError, "eligible"):
                    chat.validate(
                        {
                            **fake.session,
                            "location": {
                                "directory": str(root),
                                "workspaceID": None,
                            },
                        }
                    )
            finally:
                chat.close()

    def test_lost_creation_response_reuses_persisted_id_after_restart(self) -> None:
        with isolated_environment() as root:
            first = Chat(self.record(root), no_stream=True)
            failed = FakeClient(root)
            failed.create_failures = [
                TransportError("lost one"),
                TransportError("lost two"),
            ]
            first.api = failed  # type: ignore[assignment]
            with self.assertRaisesRegex(TransportError, "outcome is unknown"):
                first.create()
            pending = read_state(first.state_path)["pending_creation"]
            self.assertEqual(failed.create_ids, [pending, pending])

            resumed = Chat(self.record(root), no_stream=True)
            confirmed = FakeClient(root)
            resumed.api = confirmed  # type: ignore[assignment]
            try:
                resumed.open()
                self.assertEqual(resumed.session["id"], pending)
                self.assertEqual(confirmed.create_ids, [pending])
                self.assertNotIn("pending_creation", read_state(resumed.state_path))
            finally:
                resumed.close()

    def test_definite_creation_error_is_not_retried(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            fake.create_failures = [HTTPResponseError("rejected", 400)]
            chat.api = fake  # type: ignore[assignment]

            with self.assertRaises(HTTPResponseError):
                chat.create()

            self.assertEqual(len(fake.create_ids), 1)
            self.assertNotIn("pending_creation", read_state(chat.state_path))

    def test_default_open_prioritizes_pending_creation_over_last_session(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            chat._write_state(
                last_session="ses_previous",
                pending_creation="ses_pending",
            )
            fake = FakeClient(root)
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open()
                self.assertEqual(fake.create_ids, ["ses_pending"])
                self.assertEqual(chat.session["id"], "ses_pending")
            finally:
                chat.close()

    def test_creation_error_after_transport_keeps_pending_id(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            fake.create_failures = [
                TransportError("response lost"),
                HTTPResponseError("rejected", 409),
            ]
            chat.api = fake  # type: ignore[assignment]

            with self.assertRaises(HTTPResponseError):
                chat.create()

            self.assertEqual(len(fake.create_ids), 2)
            self.assertIn("pending_creation", read_state(chat.state_path))

    def test_creation_5xx_keeps_pending_id_even_if_server_committed(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)

            def committed_then_failed(session_id: str) -> dict:
                fake.create_ids.append(session_id)
                fake.session = {**fake.session, "id": session_id}
                raise HTTPResponseError("failed after commit", 500)

            fake.create_chat_session = committed_then_failed  # type: ignore[method-assign]
            chat.api = fake  # type: ignore[assignment]

            with self.assertRaises(HTTPResponseError):
                chat.create()

            self.assertEqual(
                read_state(chat.state_path)["pending_creation"],
                fake.session["id"],
            )

    def test_concurrent_creation_sequences_do_not_share_identity(self) -> None:
        with isolated_environment() as root:
            first = Chat(self.record(root), no_stream=True)
            second = Chat(self.record(root), no_stream=True)
            entered = threading.Event()
            release = threading.Event()
            identities: list[str] = []
            guard = threading.Lock()

            class ConcurrentClient(FakeClient):
                def create_chat_session(self, session_id: str) -> dict:
                    with guard:
                        identities.append(session_id)
                        position = len(identities)
                    if position == 1:
                        entered.set()
                        release.wait(timeout=2)
                    return {**self.session, "id": session_id}

            first.api = ConcurrentClient(root)  # type: ignore[assignment]
            second.api = ConcurrentClient(root)  # type: ignore[assignment]
            results: list[dict] = []
            failures: list[BaseException] = []

            def create(chat: Chat) -> None:
                try:
                    chat.open(new=True)
                    assert chat.session is not None
                    results.append(chat.session)
                except BaseException as error:
                    failures.append(error)

            one = threading.Thread(target=create, args=(first,))
            two = threading.Thread(target=create, args=(second,))
            one.start()
            self.assertTrue(entered.wait(timeout=2))
            two.start()
            time.sleep(0.05)
            self.assertEqual(len(identities), 1)
            release.set()
            one.join(timeout=2)
            two.join(timeout=2)

            self.assertFalse(failures)
            self.assertEqual(len(results), 2)
            self.assertEqual(len(set(identities)), 2)
            self.assertNotIn("pending_creation", read_state(first.state_path))
            first.close()
            second.close()

    def test_failed_creation_switch_preserves_previous_state_and_pending_id(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            chat._write_state(last_session="ses_previous")

            class ActiveCreatedClient(FakeClient):
                def active_chat_sessions(self) -> set[str]:
                    if not self.create_ids:
                        return set()
                    return {str(self.session["id"])}

            fake = ActiveCreatedClient(root)
            chat.api = fake  # type: ignore[assignment]

            with self.assertRaisesRegex(BedrockError, "currently generating"):
                chat.open(new=True)

            state = read_state(chat.state_path)
            self.assertEqual(state["last_session"], "ses_previous")
            self.assertEqual(state["pending_creation"], fake.create_ids[0])
            self.assertIsNone(chat.session)

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
                chat.open(session_id="ses_chat")
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

    def test_uncertain_admission_is_reconciled_after_restart_with_exact_body(self) -> None:
        with isolated_environment() as root:
            first = Chat(self.record(root), no_stream=True)
            failed = FakeClient(root)
            first.api = failed  # type: ignore[assignment]
            try:
                first.open(new=True)
                failed.prompt_failures = [
                    TransportError("lost one"),
                    TransportError("lost two"),
                ]
                with self.assertRaisesRegex(TransportError, "outcome is unknown"):
                    first.submit("Recover exactly")
                journal = first._pending_prompt(str(failed.session["id"]))
                self.assertIsNotNone(journal)
                assert journal is not None
            finally:
                first.close()

            resumed = Chat(self.record(root), no_stream=True)
            confirmed = FakeClient(root)
            confirmed.session = failed.session
            resumed.api = confirmed  # type: ignore[assignment]
            try:
                resumed.open()
                self.assertEqual(
                    confirmed.prompt_requests,
                    [
                        (
                            journal["session_id"],
                            journal["message_id"],
                            journal["prompt"],
                        )
                    ],
                )
                self.assertIsNone(resumed._pending_prompt(str(failed.session["id"])))
            finally:
                resumed.close()

    def test_keyboard_interrupt_during_admission_keeps_recovery_journal(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                fake.prompt_failures = [KeyboardInterrupt()]
                with patch("sys.stdout", new=io.StringIO()):
                    chat.submit("Do not lose me")
                journal = chat._pending_prompt(str(fake.session["id"]))
                self.assertIsNotNone(journal)
                assert journal is not None
                self.assertEqual(journal["prompt"], "Do not lose me")
                self.assertEqual(fake.prompt_calls, 1)
            finally:
                chat.close()

    def test_definite_admission_error_is_not_retried_and_clears_journal(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                fake.prompt_failures = [HTTPResponseError("rejected", 400)]
                with self.assertRaises(HTTPResponseError):
                    chat.submit("Rejected")
                self.assertEqual(fake.prompt_calls, 1)
                self.assertIsNone(chat._pending_prompt(str(fake.session["id"])))
            finally:
                chat.close()

    def test_admission_error_after_transport_keeps_journal(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                fake.prompt_failures = [
                    TransportError("response lost"),
                    HTTPResponseError("rejected", 409),
                ]
                with self.assertRaises(HTTPResponseError):
                    chat.submit("Rejected exactly")
                self.assertEqual(fake.prompt_calls, 2)
                self.assertIsNotNone(chat._pending_prompt(str(fake.session["id"])))
            finally:
                chat.close()

    def test_admission_5xx_keeps_journal(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                fake.prompt_failures = [HTTPResponseError("failed after commit", 500)]
                with self.assertRaises(HTTPResponseError):
                    chat.submit("Possibly committed")
                self.assertEqual(fake.prompt_calls, 1)
                self.assertIsNotNone(chat._pending_prompt(str(fake.session["id"])))
            finally:
                chat.close()

    def test_attachment_recovers_journaled_never_attempted_input_before_new_input(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            entry = {
                "session_id": "ses_chat",
                "message_id": "msg_durable",
                "prompt": "Durable prompt",
                "delivery": "queue",
            }
            chat._save_pending_prompt(entry)
            fake.recovery["unresolvedInput"] = True

            def settle(session_id: str) -> None:
                fake.recovery["unresolvedInput"] = False

            fake.wait_chat = settle  # type: ignore[method-assign]
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(session_id="ses_chat")
                self.assertEqual(
                    fake.prompt_requests,
                    [(fake.session["id"], "msg_durable", "Durable prompt")],
                )
                self.assertEqual(fake.prompt_calls, 1)
                self.assertIsNone(chat._pending_prompt(str(fake.session["id"])))
            finally:
                chat.close()

    def test_attachment_reconciles_settled_journal_without_waking(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            entry = {
                "session_id": "ses_chat",
                "message_id": "msg_settled",
                "prompt": "Already settled",
                "delivery": "queue",
            }
            chat._save_pending_prompt(entry)
            fake.requested_statuses["msg_settled"] = "settled"
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(session_id="ses_chat")
                self.assertEqual(fake.prompt_resumes, [False])
                self.assertEqual(fake.wait_calls, 0)
                self.assertIsNone(chat._pending_prompt("ses_chat"))
            finally:
                chat.close()

    def test_attachment_never_wakes_attempted_or_competing_unresolved_input(self) -> None:
        with isolated_environment() as root:
            for status, other in [("attempted", False), ("absent", True)]:
                chat = Chat(self.record(root), no_stream=True)
                fake = FakeClient(root)
                entry = {
                    "session_id": "ses_chat",
                    "message_id": f"msg_{status}_{other}",
                    "prompt": "Keep blocked",
                    "delivery": "queue",
                }
                chat._save_pending_prompt(entry)
                fake.requested_statuses[entry["message_id"]] = status
                fake.recovery["unresolvedInput"] = True
                fake.recovery["otherUnresolvedInput"] = other
                chat.api = fake  # type: ignore[assignment]
                try:
                    chat.open(session_id="ses_chat")
                    self.assertEqual(fake.prompt_calls, 0)
                    self.assertEqual(fake.wait_calls, 0)
                    self.assertEqual(chat._pending_prompt("ses_chat"), entry)
                finally:
                    chat.close()

    def test_attachment_only_clears_journal_after_target_settles(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            entry = {
                "session_id": "ses_chat",
                "message_id": "msg_stays_unattempted",
                "prompt": "Do not forget",
                "delivery": "queue",
            }
            chat._save_pending_prompt(entry)
            fake.recovery["unresolvedInput"] = True
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(session_id="ses_chat")
                self.assertEqual(fake.prompt_resumes, [True])
                self.assertEqual(fake.wait_calls, 1)
                self.assertEqual(chat._pending_prompt("ses_chat"), entry)
            finally:
                chat.close()

    def test_attachment_blocks_unresolved_input_without_recovery_journal(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            fake.recovery["unresolvedInput"] = True
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(session_id="ses_chat")
                self.assertEqual(fake.prompt_requests, [])
                with self.assertRaisesRegex(BedrockError, "unresolved input"):
                    chat.submit("Must not merge")
                self.assertEqual(fake.prompt_requests, [])
            finally:
                chat.close()

    def test_attachment_blocks_unfinished_compaction_and_attempted_unsettled_input(
        self,
    ) -> None:
        with isolated_environment() as root:
            for field, message in [
                ("unfinishedCompaction", "compaction call"),
                ("attemptedUnsettledInput", "without terminal assistant"),
            ]:
                chat = Chat(self.record(root), no_stream=True)
                fake = FakeClient(root)
                fake.recovery[field] = True
                fake.recovery["unresolvedInput"] = True
                chat.api = fake  # type: ignore[assignment]
                try:
                    chat.open(session_id="ses_chat")
                    with self.assertRaisesRegex(BedrockError, message):
                        chat.submit("Must remain blocked")
                    self.assertEqual(fake.prompt_requests, [])
                finally:
                    chat.close()

    def test_switch_preserves_previous_session_and_lock_when_state_write_fails(self) -> None:
        with isolated_environment() as root:
            record = self.record(root)
            chat = Chat(record, no_stream=True)
            fake = FakeClient(root)
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                previous_session = chat.session
                previous_lock = chat.lock
                replacement = session(root, "ses_replacement")
                with patch(
                    "opencode_bedrock.chat.write_json",
                    side_effect=BedrockError("disk full"),
                ):
                    with self.assertRaisesRegex(BedrockError, "disk full"):
                        chat.switch(replacement)
                self.assertIs(chat.session, previous_session)
                self.assertIs(chat.lock, previous_lock)
                released = SessionLock(record, "ses_replacement")
                released.close()
            finally:
                chat.close()

    def test_switch_adopts_target_when_replace_commits_but_directory_fsync_fails(
        self,
    ) -> None:
        with isolated_environment() as root:
            record = self.record(root)
            chat = Chat(record, no_stream=True)
            fake = FakeClient(root)
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                previous_id = str(chat.session["id"])
                replacement = session(root, "ses_replacement")
                with patch(
                    "opencode_bedrock.io._fsync_directory",
                    side_effect=OSError("fsync failed"),
                ):
                    with self.assertRaisesRegex(
                        JSONWriteError,
                        "durability is uncertain",
                    ) as caught:
                        chat.switch(replacement)
                self.assertTrue(caught.exception.committed)
                self.assertEqual(chat.session, replacement)
                self.assertEqual(
                    read_state(chat.state_path)["last_session"],
                    "ses_replacement",
                )
                released = SessionLock(record, previous_id)
                released.close()
                with self.assertRaisesRegex(BedrockError, "already open"):
                    SessionLock(record, "ses_replacement")
            finally:
                chat.close()

    def test_post_replace_fsync_failures_leave_recoverable_journals(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            entry = {
                "session_id": "ses_chat",
                "message_id": "msg_pending",
                "prompt": "recover me",
                "delivery": "queue",
            }
            with patch(
                "opencode_bedrock.io._fsync_directory",
                side_effect=OSError("fsync failed"),
            ):
                with self.assertRaises(JSONWriteError) as prompt_error:
                    chat._save_pending_prompt(entry)
            self.assertTrue(prompt_error.exception.committed)
            self.assertEqual(chat._pending_prompt("ses_chat"), entry)

            with patch(
                "opencode_bedrock.io._fsync_directory",
                side_effect=OSError("fsync failed"),
            ):
                with self.assertRaises(JSONWriteError) as creation_error:
                    chat._creation_id()
            self.assertTrue(creation_error.exception.committed)
            self.assertTrue(read_state(chat.state_path)["pending_creation"].startswith("ses_"))

    def test_failed_target_recovery_preserves_previous_attachment(self) -> None:
        with isolated_environment() as root:
            record = self.record(root)
            chat = Chat(record, no_stream=True)
            fake = FakeClient(root)
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                previous_session = chat.session
                previous_lock = chat.lock
                previous_state = read_state(chat.state_path)
                entry = {
                    "session_id": "ses_replacement",
                    "message_id": "msg_replacement",
                    "prompt": "Recover target",
                    "delivery": "queue",
                }
                chat._save_pending_prompt(entry)
                fake.wait_failures = [TransportError("wait failed")]

                with self.assertRaisesRegex(TransportError, "wait failed"):
                    chat.switch(session(root, "ses_replacement"))

                self.assertIs(chat.session, previous_session)
                self.assertIs(chat.lock, previous_lock)
                self.assertEqual(read_state(chat.state_path), previous_state)
                released = SessionLock(record, "ses_replacement")
                released.close()
            finally:
                chat.close()

    def test_new_checks_current_activity_before_creating_session(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                created = len(fake.create_ids)
                fake.active = {str(chat.session["id"])}
                with self.assertRaisesRegex(BedrockError, "currently generating"):
                    chat.command("/new")
                self.assertEqual(len(fake.create_ids), created)
            finally:
                chat.close()

    def test_submit_revalidates_session_before_recovery_or_admission(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                recovery_calls = len(fake.recovery_calls)
                fake.session = {**fake.session, "agent": "build"}
                with self.assertRaisesRegex(BedrockError, "eligible"):
                    chat.submit("Do not admit")
                self.assertEqual(fake.prompt_calls, 0)
                self.assertEqual(len(fake.recovery_calls), recovery_calls)
            finally:
                chat.close()

    def test_prompt_journal_rejects_empty_unknown_control_and_oversized_values(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            path = chat._prompt_path("ses_chat")
            invalid = [
                {},
                {
                    "session_id": "ses_chat",
                    "message_id": "msg_ok",
                    "prompt": "hello",
                    "delivery": "queue",
                    "extra": True,
                },
                {
                    "session_id": "ses_chat",
                    "message_id": "msg_\nunsafe",
                    "prompt": "hello",
                    "delivery": "queue",
                },
                {
                    "session_id": "ses_chat",
                    "message_id": "msg_large",
                    "prompt": "x" * (1024 * 1024 + 1),
                    "delivery": "queue",
                },
            ]
            for value in invalid:
                with self.subTest(keys=list(value)):
                    write_json(path, value)
                    with self.assertRaisesRegex(BedrockError, "journal is invalid"):
                        chat._pending_prompt("ses_chat")

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
                self.assertEqual(fake.event_calls, 1)
            finally:
                chat.close()

    def test_stream_end_never_constructs_an_uncancellable_replacement(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root))
            fake = FakeClient(root)

            class EndedStream:
                def __iter__(self):
                    return iter(())

                def close(self) -> None:
                    return

            def events() -> EndedStream:
                fake.event_calls += 1
                if fake.event_calls > 1:
                    raise AssertionError("replacement stream must not be constructed")
                return EndedStream()

            fake.events = events  # type: ignore[method-assign]
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                with patch("sys.stdout", new=TTYStringIO()):
                    chat.submit("Hello")
                self.assertEqual(fake.event_calls, 1)
            finally:
                chat.close()

    def test_message_batch_has_an_aggregate_decoded_content_budget(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root), no_stream=True)
            fake = FakeClient(root)
            large = "x" * PROMPT_BYTES
            pages = [
                {"id": "msg_legal", "type": "user", "text": large},
                *[
                    {
                        "id": f"msg_{index}",
                        "type": "user",
                        "text": "y" * (512 * 1024),
                    }
                    for index in range(49)
                ],
            ]
            requested_limits: list[int] = []

            def chat_messages(
                session_id: str,
                limit: int = 50,
                cursor: str | None = None,
                order: str = "desc",
            ) -> dict:
                requested_limits.append(limit)
                index = int(cursor[1:]) if cursor else 0
                next_cursor = f"c{index + 1}" if index + 1 < len(pages) else None
                return {
                    "data": [pages[index]],
                    "cursor": {"next": next_cursor} if next_cursor else {},
                }

            fake.chat_messages = chat_messages  # type: ignore[method-assign]
            chat.api = fake  # type: ignore[assignment]
            chat.session = fake.session
            messages, cursor = chat._message_batch()
            encoded = sum(
                len(json.dumps(message, separators=(",", ":")).encode())
                for message in messages
            )
            self.assertEqual(messages[0]["text"], large)
            self.assertLessEqual(encoded, MESSAGE_BATCH_BYTES)
            self.assertLess(len(messages), len(pages))
            self.assertIsNotNone(cursor)
            self.assertTrue(all(limit == 1 for limit in requested_limits))

    def test_malformed_stream_event_falls_back_without_thread_failure(self) -> None:
        with isolated_environment() as root:
            chat = Chat(self.record(root))
            fake = FakeClient(root)

            class MalformedStream:
                def __iter__(self):
                    yield []

                def close(self) -> None:
                    return

            def events() -> MalformedStream:
                fake.event_calls += 1
                return MalformedStream()

            fake.events = events  # type: ignore[method-assign]
            chat.api = fake  # type: ignore[assignment]
            try:
                chat.open(new=True)
                with (
                    patch("sys.stdout", new=TTYStringIO()) as output,
                    patch("threading.excepthook") as thread_failure,
                ):
                    chat.submit("Hello")
                self.assertIn("Hello safely", output.getvalue())
                thread_failure.assert_not_called()
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
