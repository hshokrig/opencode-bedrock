from __future__ import annotations

import fcntl
import hashlib
import json
import os
import queue
import stat
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from .api import Client
from .errors import (
    BedrockError,
    HTTPResponseError,
    JSONWriteError,
    NotFoundError,
    TransportError,
)
from .io import locked, read_private_json_object, unlink_durable, write_json
from .paths import ensure_private_directory, services_root
from .service import Record

PURPOSE = "terminal-chat"
AGENT = "chat"
PROVIDER = "amazon-bedrock"
MODEL = "opus"
HISTORY_MESSAGES = 50
API_MESSAGE_PAGE = 1
MESSAGE_BATCH_BYTES = 8 * 1024 * 1024
HISTORY_LINES = 200
HISTORY_BYTES = 64 * 1024
PROMPT_BYTES = 1024 * 1024
PROMPT_JOURNAL_BYTES = 8 * PROMPT_BYTES
STREAM_BUFFER_BYTES = 1024 * 1024
BIDI_CONTROLS = {
    "\u061c",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
}


class IneligibleChatError(BedrockError):
    pass


def read_state(path: Path) -> dict[str, Any]:
    value = read_private_json_object(path, label="private chat state")
    assert value is not None
    if set(value) - {"last_session", "pending_creation"} or not all(
        _is_identifier(item) for item in value.values()
    ):
        raise BedrockError("private chat state is invalid")
    return value


def _is_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and all(character.isprintable() for character in value)
    )


def sanitize(value: object) -> str:
    text = str(value)
    return "".join(
        character
        for character in text
        if character in {"\n", "\t"}
        or (
            character not in BIDI_CONTROLS
            and character not in {"\r", "\b", "\x1b"}
            and not (ord(character) < 32 or 127 <= ord(character) <= 159)
        )
    )


class SessionLock:
    def __init__(self, record: Record, session_id: str):
        digest = hashlib.sha256(session_id.encode()).hexdigest()
        try:
            directory = ensure_private_directory(
                services_root() / record.key / "chat-locks"
            )
        except OSError as error:
            raise BedrockError(f"cannot prepare chat session lock: {error}") from error
        self.path = directory / f"{digest}.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self.descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise BedrockError(f"cannot open chat session lock: {error}") from error
        try:
            if not stat.S_ISREG(os.fstat(self.descriptor).st_mode):
                raise BedrockError("chat session lock is not a regular file")
            os.fchmod(self.descriptor, 0o600)
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(self.descriptor)
            raise BedrockError(
                f"chat session is already open locally: {session_id}; close it or use /new"
            ) from error
        except OSError as error:
            os.close(self.descriptor)
            raise BedrockError(f"cannot use chat session lock: {error}") from error
        except Exception:
            os.close(self.descriptor)
            raise

    def close(self) -> None:
        if self.descriptor < 0:
            return
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = -1
        except OSError as error:
            raise BedrockError(f"cannot close chat session lock: {error}") from error


class Chat:
    def __init__(self, record: Record, no_stream: bool = False):
        self.record = record
        self.api = Client(record.port, record.password, Path(record.workspace))
        self.no_stream = no_stream
        self.session: dict[str, Any] | None = None
        self.lock: SessionLock | None = None
        self.history_cursor: str | None = None
        self.state_path = services_root() / record.key / "chat.json"
        self.state_lock_path = services_root() / record.key / "chat-state.lock"
        self.creation_lock_path = services_root() / record.key / "chat-creation.lock"

    def open(self, new: bool = False, session_id: str | None = None) -> None:
        if new:
            self._create_and_switch()
            self.ensure_existing_title()
            return
        if session_id:
            self.switch(self.api.get_chat_session(session_id))
            self.ensure_existing_title()
            return
        state = read_state(self.state_path)
        if state.get("pending_creation") is not None:
            self._create_and_switch()
            self.ensure_existing_title()
            return
        selected = state.get("last_session")
        if isinstance(selected, str):
            try:
                self.switch(self.api.get_chat_session(selected))
                self.ensure_existing_title()
                return
            except (IneligibleChatError, NotFoundError):
                pass
        self._create_and_switch()
        self.ensure_existing_title()

    def create(self) -> dict[str, Any]:
        with locked(self.creation_lock_path):
            return self._create_locked()

    def _create_and_switch(self) -> None:
        with locked(self.creation_lock_path):
            self.switch(self._create_locked())

    def _create_locked(self) -> dict[str, Any]:
        session_id = self._creation_id()
        try:
            session = self.api.create_chat_session(session_id)
        except TransportError as first:
            try:
                session = self.api.create_chat_session(session_id)
            except TransportError as second:
                raise TransportError(
                    f"chat creation outcome is unknown for session {session_id}: {second}"
                ) from first
            except BedrockError:
                raise
        except HTTPResponseError as error:
            if self._definite_precommit(error):
                self._clear_creation_id(session_id)
            raise
        except BedrockError:
            self._clear_creation_id(session_id)
            raise
        if session.get("id") != session_id:
            raise BedrockError("server did not confirm the requested chat session ID")
        try:
            self.validate(session)
        except BedrockError:
            self._clear_creation_id(session_id)
            raise
        return session

    def switch(self, session: dict[str, Any]) -> None:
        self.validate(session)
        target_id = str(session["id"])
        self._ensure_current_inactive()
        if target_id in self.api.active_chat_sessions():
            raise BedrockError(
                f"chat session is currently generating: {session['id']}; "
                "wait or interrupt it before switching"
            )
        if self.session and str(self.session["id"]) == target_id:
            self._recover_session(target_id)
            self.session = session
            self.history_cursor = None
            return
        next_lock = SessionLock(self.record, target_id)
        previous = self.lock
        try:
            self._recover_session(target_id)
            if target_id in self.api.active_chat_sessions():
                raise BedrockError(
                    f"chat session is currently generating: {target_id}; "
                    "wait or interrupt it before switching"
                )
            committed_error: JSONWriteError | None = None
            try:
                with locked(self.state_lock_path):
                    state = read_state(self.state_path)
                    state["last_session"] = session["id"]
                    if state.get("pending_creation") == session["id"]:
                        state.pop("pending_creation")
                    write_json(self.state_path, state)
            except JSONWriteError as error:
                if not error.committed:
                    raise
                committed_error = error
        except BaseException:
            next_lock.close()
            raise
        self.lock = next_lock
        self.session = session
        self.history_cursor = None
        if previous:
            previous.close()
        if committed_error:
            raise committed_error

    def _ensure_current_inactive(self) -> None:
        if not self.session:
            return
        session_id = str(self.session["id"])
        if session_id in self.api.active_chat_sessions():
            raise BedrockError(
                f"chat session is currently generating: {session_id}; "
                "wait or interrupt it before switching"
            )

    def validate(self, session: dict[str, Any]) -> None:
        model = session.get("model")
        location = session.get("location")
        if (
            session.get("purpose") != PURPOSE
            or session.get("parentID") is not None
            or session.get("agent") != AGENT
            or not isinstance(model, dict)
            or model.get("providerID") != PROVIDER
            or model.get("id") != MODEL
            or not isinstance(location, dict)
            or "workspaceID" in location
            or not isinstance(location.get("directory"), str)
            or Path(str(location.get("directory"))).resolve()
            != Path(self.record.workspace).resolve()
        ):
            raise IneligibleChatError("session is not an eligible chat for this workspace")

    def close(self) -> None:
        if self.lock:
            self.lock.close()
            self.lock = None

    def _write_state(self, **updates: object) -> None:
        with locked(self.state_lock_path):
            state = read_state(self.state_path)
            for key, value in updates.items():
                if value is None:
                    state.pop(key, None)
                    continue
                state[key] = value
            write_json(self.state_path, state)

    def _creation_id(self) -> str:
        with locked(self.state_lock_path):
            state = read_state(self.state_path)
            pending = state.get("pending_creation")
            if pending is not None and (not isinstance(pending, str) or not pending):
                raise BedrockError("private chat creation journal is invalid")
            if isinstance(pending, str):
                return pending
            session_id = f"ses_{uuid.uuid4().hex}"
            state["pending_creation"] = session_id
            write_json(self.state_path, state)
            return session_id

    def _clear_creation_id(self, session_id: str) -> None:
        with locked(self.state_lock_path):
            state = read_state(self.state_path)
            if state.get("pending_creation") != session_id:
                return
            state.pop("pending_creation")
            write_json(self.state_path, state)

    def _pending_prompt(self, session_id: str) -> dict[str, str] | None:
        value = read_private_json_object(
            self._prompt_path(session_id),
            label="private prompt recovery journal",
            limit=PROMPT_JOURNAL_BYTES,
            missing=None,
        )
        if value is None:
            return None
        if (
            set(value) != {"session_id", "message_id", "prompt", "delivery"}
            or value.get("session_id") != session_id
            or not _is_identifier(value.get("session_id"))
            or not _is_identifier(value.get("message_id"))
            or not isinstance(value.get("prompt"), str)
            or len(value["prompt"].encode("utf-8")) > PROMPT_BYTES
            or value.get("delivery") != "queue"
        ):
            raise BedrockError(
                f"private prompt recovery journal is invalid for session {session_id}"
            )
        return {
            "session_id": session_id,
            "message_id": value["message_id"],
            "prompt": value["prompt"],
            "delivery": "queue",
        }

    def _save_pending_prompt(self, entry: dict[str, str]) -> None:
        self._validate_prompt_entry(entry)
        path = self._prompt_path(entry["session_id"])
        with locked(path.with_suffix(".lock")):
            write_json(path, entry)

    def _clear_pending_prompt(self, entry: dict[str, str]) -> None:
        path = self._prompt_path(entry["session_id"])
        with locked(path.with_suffix(".lock")):
            value = read_private_json_object(
                path,
                label="private prompt recovery journal",
                limit=PROMPT_JOURNAL_BYTES,
                missing=None,
            )
            if value is None:
                return
            self._validate_prompt_entry(value)
            if value != entry:
                return
            unlink_durable(path, label="private prompt recovery journal")

    def _prompt_path(self, session_id: str) -> Path:
        if not _is_identifier(session_id):
            raise BedrockError("invalid chat session identifier")
        digest = hashlib.sha256(session_id.encode()).hexdigest()
        try:
            directory = ensure_private_directory(
                services_root() / self.record.key / "chat-pending"
            )
        except OSError as error:
            raise BedrockError(
                f"cannot prepare prompt recovery directory: {error}"
            ) from error
        return directory / f"{digest}.json"

    @staticmethod
    def _validate_prompt_entry(entry: dict[str, str]) -> None:
        if (
            set(entry) != {"session_id", "message_id", "prompt", "delivery"}
            or not _is_identifier(entry.get("session_id"))
            or not _is_identifier(entry.get("message_id"))
            or not isinstance(entry.get("prompt"), str)
            or len(entry["prompt"].encode("utf-8")) > PROMPT_BYTES
            or entry.get("delivery") != "queue"
        ):
            raise BedrockError("private prompt recovery journal entry is invalid")

    def _admit_exact(
        self,
        entry: dict[str, str],
        definite_is_final: bool,
        *,
        resume: bool,
    ) -> bool:
        retried = False
        try:
            response = self.api.prompt_chat(
                entry["session_id"],
                entry["message_id"],
                entry["prompt"],
                resume=resume,
            )
        except TransportError as first:
            retried = True
            try:
                response = self.api.prompt_chat(
                    entry["session_id"],
                    entry["message_id"],
                    entry["prompt"],
                    resume=resume,
                )
            except TransportError as second:
                raise TransportError(
                    "prompt admission outcome is unknown for message "
                    f"{entry['message_id']}: {second}"
                ) from first
            except BedrockError:
                raise
        except HTTPResponseError as error:
            if definite_is_final and self._definite_precommit(error):
                self._clear_pending_prompt(entry)
            raise
        except BedrockError:
            if definite_is_final:
                self._clear_pending_prompt(entry)
            raise
        if response.get("id") != entry["message_id"]:
            raise BedrockError(
                "server did not confirm the requested durable prompt message ID; "
                f"recovery remains pending for {entry['message_id']}"
            )
        return retried

    @staticmethod
    def _definite_precommit(error: HTTPResponseError) -> bool:
        return 400 <= error.status < 500

    def recover_attachment(self) -> None:
        assert self.session is not None
        self._recover_session(str(self.session["id"]))

    def _recover_session(self, session_id: str) -> None:
        pending = self._pending_prompt(session_id)
        recovery = self.api.chat_recovery(
            session_id,
            pending["message_id"] if pending else None,
        )
        if not pending:
            return
        status = recovery["requestedInputStatus"]
        if status == "settled":
            self._admit_exact(
                pending,
                definite_is_final=False,
                resume=False,
            )
            self._clear_pending_prompt(pending)
            return
        if status == "attempted" or self._recovery_block(recovery):
            return
        if status not in {"absent", "unattempted"}:
            raise TransportError("OpenCode API returned an invalid prompt recovery status")
        resume = not recovery["otherUnresolvedInput"]
        self._admit_exact(
            pending,
            definite_is_final=False,
            resume=resume,
        )
        if not resume:
            return
        self.api.wait_chat(session_id)
        settled = self.api.chat_recovery(
            session_id,
            pending["message_id"],
        )["requestedInputStatus"]
        if settled == "settled":
            self._clear_pending_prompt(pending)

    @staticmethod
    def _recovery_block(
        recovery: dict[str, Any],
    ) -> str | None:
        if recovery["unfinishedProviderAttempt"]:
            return "a provider attempt has no durable outcome"
        if recovery["unfinishedCompaction"]:
            return "a compaction call has no durable outcome"
        if (
            recovery["attemptedUnsettledInput"]
            or recovery["requestedInputStatus"] == "attempted"
        ):
            return "a provider attempt ended without terminal assistant settlement"
        if recovery["otherUnresolvedInput"]:
            return "another unresolved input has no safe automatic recovery path"
        if (
            recovery["unresolvedInput"]
            and recovery["requestedInputStatus"] == "not-requested"
        ):
            return "an unresolved input has no local exact-retry journal"
        return None

    def ensure_existing_title(self) -> None:
        assert self.session is not None
        if not str(self.session.get("title", "")).startswith("New session - "):
            return
        messages, _ = self._message_batch(order="asc")
        first_user = next((message for message in messages if message.get("type") == "user"), None)
        if not first_user:
            return
        user_index = messages.index(first_user)
        following = messages[user_index + 1 :]
        next_user = next(
            (
                index
                for index, message in enumerate(following)
                if message.get("type") == "user"
            ),
            len(following),
        )
        settled = next(
            (
                message
                for message in following[:next_user]
                if message.get("type") == "assistant"
                and message.get("error") is None
                and message.get("finish") != "error"
                and (
                    message.get("time", {}).get("completed") is not None
                    or message.get("finish") is not None
                )
            ),
            None,
        )
        if not settled:
            return
        try:
            self.api.ensure_chat_title(str(self.session["id"]), str(first_user["id"]))
            self.session = self.api.get_chat_session(str(self.session["id"]))
        except BedrockError:
            return

    def run(self) -> int:
        self.header()
        self.show_recent()
        while True:
            try:
                value = input("you> ")
            except EOFError:
                return 0
            if not value:
                continue
            try:
                if value.startswith("//"):
                    self.submit(value[1:])
                    continue
                if value.startswith("/"):
                    if self.command(value):
                        return 0
                    continue
                self.submit(value)
            except BedrockError as error:
                print(f"error: {sanitize(error)}")

    def header(self) -> None:
        assert self.session is not None
        print(f"\nChat: {sanitize(self.session['title'])}")
        print(f"Session: {sanitize(self.session['id'])}")
        print("Model: Claude through Amazon Bedrock\n")
        problem = self._recovery_block(self.api.chat_recovery(str(self.session["id"])))
        if problem:
            print(f"warning: {problem}; this chat will not be replayed automatically\n")

    def has_unfinished_provider_attempt(self) -> bool:
        return self.api.chat_recovery(str(self.session["id"]))[
            "unfinishedProviderAttempt"
        ]

    def command(self, value: str) -> bool:
        command, _, argument = value.partition(" ")
        if command == "/quit":
            return True
        if command == "/help":
            print("/new  /sessions  /use SESSION_ID  /history  /history more  /help  /quit")
            return False
        if command == "/new":
            self._ensure_current_inactive()
            self._create_and_switch()
            self.header()
            return False
        if command == "/sessions":
            self.show_sessions()
            return False
        if command == "/use" and argument.strip():
            self.use(argument.strip())
            return False
        if command == "/history":
            self.show_history(more=argument.strip() == "more")
            return False
        print("unknown command; use /help")
        return False

    def use(self, value: str) -> None:
        try:
            self.switch(self.api.get_chat_session(value))
            self.header()
            self.show_recent()
            return
        except NotFoundError:
            pass
        response = self.api.list_chat_sessions()
        sessions = [
            item
            for item in response["data"]
            if self._eligible(item) and str(item.get("id", "")).startswith(value)
        ]
        if len(sessions) != 1:
            label = "not found" if not sessions else "ambiguous"
            raise BedrockError(f"chat session prefix is {label}: {value}")
        self.switch(sessions[0])
        self.header()
        self.show_recent()

    def show_sessions(self) -> None:
        response = self.api.list_chat_sessions()
        sessions = [item for item in response["data"] if self._eligible(item)]
        if not sessions:
            print("no chat sessions")
            return
        for session in sessions:
            created = session.get("time", {}).get("created", "")
            print(
                f"{sanitize(session['id'])}\t{sanitize(session['title'])}\t{sanitize(created)}"
            )

    def show_recent(self) -> None:
        assert self.session is not None
        messages, _ = self._message_batch(complete_turns=10)
        visible = self._recent_complete(messages)
        if not visible:
            return
        self._render(visible)
        print()

    def show_history(self, more: bool) -> None:
        assert self.session is not None
        cursor = self.history_cursor if more else None
        if more and cursor is None:
            print("no older history")
            return
        messages, self.history_cursor = self._message_batch(cursor=cursor)
        visible = self._visible(messages)
        self._render(visible)
        if self.history_cursor:
            print("(use /history more for older messages)")

    def submit(self, prompt: str) -> None:
        assert self.session is not None
        if len(prompt.encode("utf-8")) > PROMPT_BYTES:
            raise BedrockError("message exceeds the 1 MiB UTF-8 transport limit")
        session_id = str(self.session["id"])
        self.validate(self.api.get_chat_session(session_id))
        self.recover_attachment()
        if session_id in self.api.active_chat_sessions():
            raise BedrockError(
                "chat session is still generating; wait or interrupt it before sending"
            )
        problem = self._recovery_block(self.api.chat_recovery(session_id))
        if problem:
            raise BedrockError(
                f"chat is blocked because {problem}; use /new to avoid replaying uncertain work"
            )
        message_id = f"msg_{uuid.uuid4().hex}"
        self.validate(self.api.get_chat_session(session_id))
        pending = {
            "session_id": session_id,
            "message_id": message_id,
            "prompt": prompt,
            "delivery": "queue",
        }
        stream: Any | None = None
        stop_stream = threading.Event()
        event_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=64)
        reader: threading.Thread | None = None
        if not self.no_stream and sys.stdout.isatty():
            try:
                stream = self.api.events()

                def read_events() -> None:
                    target_attempt: str | None = None
                    target_assistant: str | None = None
                    target_text_ids: set[str] = set()
                    captured_bytes = 0
                    assert stream is not None
                    try:
                        for event in stream:
                            if stop_stream.is_set():
                                return
                            if not isinstance(event, dict):
                                raise TransportError(
                                    "OpenCode event stream returned an invalid event"
                                )
                            data = event.get("data", {})
                            if not isinstance(data, dict):
                                raise TransportError(
                                    "OpenCode event stream returned an invalid event"
                                )
                            if data.get("sessionID") != session_id:
                                continue
                            event_type = event.get("type")
                            if (
                                event_type == "session.next.provider-attempt.started"
                                and message_id in data.get("inputMessageIDs", [])
                            ):
                                target_attempt = str(data.get("attemptID"))
                                continue
                            if (
                                event_type == "session.next.step.started"
                                and target_attempt
                                and target_assistant is None
                            ):
                                target_assistant = str(data.get("assistantMessageID"))
                                continue
                            if (
                                event_type == "session.next.text.started"
                                and data.get("assistantMessageID") == target_assistant
                            ):
                                target_text_ids.add(str(data.get("textID")))
                                continue
                            if (
                                event_type != "session.next.text.delta"
                                or data.get("assistantMessageID") != target_assistant
                                or data.get("textID") not in target_text_ids
                            ):
                                continue
                            delta = sanitize(data.get("delta", ""))
                            captured_bytes += len(delta.encode("utf-8"))
                            if captured_bytes > STREAM_BUFFER_BYTES:
                                stop_stream.set()
                                return
                            try:
                                event_queue.put_nowait(("delta", delta))
                            except queue.Full:
                                stop_stream.set()
                                return
                        error = BedrockError("OpenCode event stream ended")
                    except BedrockError as caught:
                        error = caught
                    if stop_stream.is_set():
                        return
                    try:
                        event_queue.put_nowait(("stream-error", error))
                    except queue.Full:
                        pass

                reader = threading.Thread(target=read_events, daemon=True)
                reader.start()
            except BedrockError as error:
                print(
                    "(live stream unavailable; durable reconciliation will be used: "
                    f"{sanitize(error)})"
                )
        wait_result: list[BaseException] = []
        waiter: threading.Thread | None = None

        def wait() -> None:
            try:
                self.api.wait_chat(session_id)
            except BaseException as error:
                wait_result.append(error)

        streamed = ""
        try:
            self._save_pending_prompt(pending)
            if self._admit_exact(
                pending,
                definite_is_final=True,
                resume=True,
            ):
                print("(prompt admission was confirmed by an exact durable retry)")
            print("claude> ", end="", flush=True)
            waiter = threading.Thread(target=wait, daemon=True)
            waiter.start()
            while waiter.is_alive():
                try:
                    kind, value = event_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if kind == "stream-error":
                    continue
                delta = str(value)
                streamed += delta
                print(delta, end="", flush=True)
        except KeyboardInterrupt:
            self.api.interrupt_chat(session_id)
            if waiter:
                waiter.join(timeout=10)
            if waiter and waiter.is_alive():
                print("\n(interrupt acknowledgement timed out; leaving chat attached state intact)")
                raise
            print("\n(interrupted)")
            return
        finally:
            stop_stream.set()
            if stream:
                stream.close()
            if reader:
                reader.join(timeout=1)
        assert waiter is not None
        waiter.join()
        if wait_result:
            error = wait_result[0]
            if isinstance(error, KeyboardInterrupt):
                raise error
            if isinstance(error, Exception):
                raise error
            raise BedrockError("session wait failed")
        if (
            self.api.chat_recovery(session_id, message_id)["requestedInputStatus"]
            == "settled"
        ):
            self._clear_pending_prompt(pending)
        messages, _ = self._message_batch()
        answer = sanitize(self._answer_after(messages, message_id))
        if streamed and answer.startswith(streamed):
            print(answer[len(streamed) :])
        elif streamed and answer != streamed:
            print(f"\n(reconciled durable response)\n{answer or '(no completed response)'}")
        else:
            print(answer or "(no completed response)")
        previous_title = str(self.session.get("title", ""))
        if previous_title.startswith("New session - ") and answer:
            self.ensure_existing_title()
            if self.session and self.session.get("title") != previous_title:
                print(f"(chat named: {sanitize(self.session['title'])})")
        self.session = self.api.get_chat_session(session_id)

    def _message_batch(
        self,
        cursor: str | None = None,
        order: str = "desc",
        complete_turns: int | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        assert self.session is not None
        messages: list[dict[str, Any]] = []
        next_cursor = cursor
        retained_bytes = 0
        for _ in range(HISTORY_MESSAGES):
            page_cursor = next_cursor
            response = self.api.chat_messages(
                str(self.session["id"]),
                limit=API_MESSAGE_PAGE,
                cursor=next_cursor,
                order=order,
            )
            page = response["data"]
            page_bytes = sum(
                len(
                    json.dumps(
                        message,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                for message in page
            )
            if retained_bytes + page_bytes > MESSAGE_BATCH_BYTES:
                if not messages:
                    raise TransportError(
                        "OpenCode API returned a chat message exceeding the "
                        "8 MiB retained history limit"
                    )
                return messages, page_cursor
            messages.extend(page)
            retained_bytes += page_bytes
            next_cursor = response.get("cursor", {}).get("next")
            if (
                complete_turns is not None
                and len(self._recent_complete(messages)) >= complete_turns * 2
            ):
                break
            if not next_cursor:
                break
        return messages, next_cursor

    def _eligible(self, session: dict[str, Any]) -> bool:
        try:
            self.validate(session)
        except BedrockError:
            return False
        return True

    @staticmethod
    def _visible(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for message in reversed(messages):
            if message.get("type") == "user":
                result.append(("you", sanitize(message.get("text", ""))))
            if message.get("type") == "assistant":
                text = "".join(
                    str(part.get("text", ""))
                    for part in message.get("content", [])
                    if part.get("type") == "text"
                )
                if message.get("error"):
                    text = text or f"[error] {message['error'].get('message', 'unknown error')}"
                if text:
                    result.append(("claude", sanitize(text)))
        return result

    @staticmethod
    def _answer_after(messages: list[dict[str, Any]], message_id: str) -> str:
        chronological = list(reversed(messages))
        index = next(
            (
                position
                for position, message in enumerate(chronological)
                if message.get("type") == "user" and message.get("id") == message_id
            ),
            -1,
        )
        if index < 0:
            return ""
        for message in chronological[index + 1 :]:
            if message.get("type") == "user":
                return ""
            if message.get("type") != "assistant":
                continue
            text = "".join(
                str(part.get("text", ""))
                for part in message.get("content", [])
                if part.get("type") == "text"
            )
            if text:
                return text
            if message.get("error"):
                return f"[error] {message['error'].get('message', 'unknown error')}"
            return ""
        return ""

    @staticmethod
    def _recent_complete(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
        chronological = list(reversed(messages))
        turns: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] | None = None
        for message in chronological:
            visible = Chat._visible([message])
            if message.get("type") == "user":
                current = visible
                continue
            if message.get("type") != "assistant" or current is None:
                continue
            settled = (
                message.get("time", {}).get("completed") is not None
                or message.get("finish") is not None
                or message.get("error") is not None
            )
            if not settled:
                continue
            current.extend(visible)
            turns.append(current)
            current = None
        return [item for turn in turns[-10:] for item in turn]

    @staticmethod
    def _render(messages: list[tuple[str, str]]) -> None:
        output = "\n\n".join(f"{role}> {text}" for role, text in messages)
        encoded = output.encode("utf-8")
        if len(encoded) > HISTORY_BYTES:
            output = encoded[-HISTORY_BYTES:].decode("utf-8", errors="ignore")
            output = "(older content omitted)\n" + output
        lines = output.splitlines()
        if len(lines) > HISTORY_LINES:
            lines = ["(older lines omitted)", *lines[-(HISTORY_LINES - 1) :]]
        print("\n".join(lines))


def run(
    record: Record,
    new: bool = False,
    session_id: str | None = None,
    no_stream: bool = False,
) -> int:
    if not sys.stdin.isatty():
        raise BedrockError("chat requires an interactive terminal on standard input")
    chat = Chat(record, no_stream=no_stream)
    try:
        chat.open(new=new, session_id=session_id)
        return chat.run()
    finally:
        chat.close()
