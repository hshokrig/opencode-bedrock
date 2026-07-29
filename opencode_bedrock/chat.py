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
from .errors import BedrockError, NotFoundError
from .io import write_json
from .paths import ensure_private_directory, services_root
from .service import Record

PURPOSE = "terminal-chat"
AGENT = "chat"
PROVIDER = "amazon-bedrock"
MODEL = "opus"
HISTORY_MESSAGES = 50
HISTORY_LINES = 200
HISTORY_BYTES = 64 * 1024
PROMPT_BYTES = 1024 * 1024
STREAM_BUFFER_BYTES = 1024 * 1024
DURABLE_HISTORY_PAGES = 100
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
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise BedrockError(f"cannot open private chat state: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise BedrockError("private chat state must be a mode-0600 regular file")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise BedrockError(f"cannot read private chat state: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return value if isinstance(value, dict) else {}


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
        directory = ensure_private_directory(services_root() / record.key / "chat-locks")
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
        except Exception:
            os.close(self.descriptor)
            raise

    def close(self) -> None:
        if self.descriptor < 0:
            return
        fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        os.close(self.descriptor)
        self.descriptor = -1


class Chat:
    def __init__(self, record: Record, no_stream: bool = False):
        self.record = record
        self.api = Client(record.port, record.password, Path(record.workspace))
        self.no_stream = no_stream
        self.session: dict[str, Any] | None = None
        self.lock: SessionLock | None = None
        self.history_cursor: str | None = None
        self.uncertain_admission: str | None = None
        self.state_path = services_root() / record.key / "chat.json"

    def open(self, new: bool = False, session_id: str | None = None) -> None:
        if new:
            self.switch(self.create())
            self.ensure_existing_title()
            return
        if session_id:
            self.switch(self.api.get_chat_session(session_id))
            self.ensure_existing_title()
            return
        selected = read_state(self.state_path).get("last_session")
        if isinstance(selected, str):
            try:
                self.switch(self.api.get_chat_session(selected))
                self.ensure_existing_title()
                return
            except (IneligibleChatError, NotFoundError):
                pass
        self.switch(self.create())
        self.ensure_existing_title()

    def create(self) -> dict[str, Any]:
        session_id = f"ses_{uuid.uuid4().hex}"
        session = self.api.create_chat_session(session_id)
        if session.get("id") != session_id:
            raise BedrockError("server did not confirm the requested chat session ID")
        return session

    def switch(self, session: dict[str, Any]) -> None:
        self.validate(session)
        if str(session["id"]) in self.api.active_chat_sessions():
            raise BedrockError(
                f"chat session is currently generating: {session['id']}; "
                "wait or interrupt it before switching"
            )
        next_lock = SessionLock(self.record, str(session["id"]))
        previous = self.lock
        self.lock = next_lock
        self.session = session
        self.history_cursor = None
        self.uncertain_admission = None
        write_json(self.state_path, {"last_session": session["id"]})
        if previous:
            previous.close()

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
            or Path(str(location.get("directory"))).resolve()
            != Path(self.record.workspace).resolve()
        ):
            raise IneligibleChatError("session is not an eligible chat for this workspace")

    def close(self) -> None:
        if self.lock:
            self.lock.close()
            self.lock = None

    def ensure_existing_title(self) -> None:
        assert self.session is not None
        if not str(self.session.get("title", "")).startswith("New session - "):
            return
        messages = self.api.chat_messages(str(self.session["id"]), limit=50, order="asc")["data"]
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
        if self.has_unfinished_provider_attempt():
            print(
                "warning: a previous provider attempt has no durable outcome; "
                "it will not be replayed automatically\n"
            )

    def has_unfinished_provider_attempt(self) -> bool:
        assert self.session is not None
        after: int | None = None
        active: set[str] = set()
        for _ in range(DURABLE_HISTORY_PAGES):
            response = self.api.chat_history(str(self.session["id"]), after=after)
            events = response["data"]
            for event in events:
                data = event.get("data", {})
                attempt_id = data.get("attemptID")
                if not isinstance(attempt_id, str):
                    continue
                if event.get("type") == "session.next.provider-attempt.started":
                    active.add(attempt_id)
                if event.get("type") == "session.next.provider-attempt.ended":
                    active.discard(attempt_id)
            if not response.get("hasMore"):
                return bool(active)
            sequence = events[-1].get("durable", {}).get("seq") if events else None
            if not isinstance(sequence, int) or sequence <= (after or -1):
                raise BedrockError("durable chat history did not advance")
            after = sequence
        raise BedrockError("durable chat history exceeds the bounded recovery scan")

    def command(self, value: str) -> bool:
        command, _, argument = value.partition(" ")
        if command == "/quit":
            return True
        if command == "/help":
            print("/new  /sessions  /use SESSION_ID  /history  /history more  /help  /quit")
            return False
        if command == "/new":
            self.switch(self.create())
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
        response = self.api.chat_messages(str(self.session["id"]), limit=HISTORY_MESSAGES)
        visible = self._recent_complete(response["data"])
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
        response = self.api.chat_messages(
            str(self.session["id"]), limit=HISTORY_MESSAGES, cursor=cursor
        )
        self.history_cursor = response.get("cursor", {}).get("next")
        visible = self._visible(response["data"])
        self._render(visible)
        if self.history_cursor:
            print("(use /history more for older messages)")

    def submit(self, prompt: str) -> None:
        assert self.session is not None
        if len(prompt.encode("utf-8")) > PROMPT_BYTES:
            raise BedrockError("message exceeds the 1 MiB UTF-8 transport limit")
        if self.uncertain_admission:
            raise BedrockError(
                "prompt admission is still outcome-unknown for message "
                f"{self.uncertain_admission}; use /new rather than submitting another message"
            )
        session_id = str(self.session["id"])
        if session_id in self.api.active_chat_sessions():
            raise BedrockError("chat session is still generating; wait or interrupt it before sending")
        if self.has_unfinished_provider_attempt():
            raise BedrockError(
                "chat has an outcome-unknown provider attempt and cannot accept another message; "
                "use /new to avoid replaying uncertain work"
            )
        message_id = f"msg_{uuid.uuid4().hex}"
        self.validate(self.api.get_chat_session(session_id))
        streams: list[Any] = []
        stop_stream = threading.Event()
        event_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=64)
        reader: threading.Thread | None = None
        if not self.no_stream and sys.stdout.isatty():
            try:
                streams.append(self.api.events())

                def read_events() -> None:
                    target_attempt: str | None = None
                    target_assistant: str | None = None
                    target_text_ids: set[str] = set()
                    captured_bytes = 0
                    for attempt in range(2):
                        current = streams[-1]
                        try:
                            for event in current:
                                if stop_stream.is_set():
                                    return
                                data = event.get("data", {})
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
                        if attempt == 0:
                            current.close()
                            try:
                                streams.append(self.api.events())
                                continue
                            except BedrockError as caught:
                                error = caught
                        try:
                            event_queue.put_nowait(("stream-error", error))
                        except queue.Full:
                            pass
                        return

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
            try:
                self.api.prompt_chat(session_id, message_id, prompt)
            except BedrockError as admission_error:
                try:
                    self.api.prompt_chat(session_id, message_id, prompt)
                except BedrockError as recovery_error:
                    self.uncertain_admission = message_id
                    raise BedrockError(
                        f"prompt admission outcome is unknown for message {message_id}: {recovery_error}"
                    ) from admission_error
                print("(prompt admission was confirmed by an exact durable retry)")
            self.uncertain_admission = None
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
            for stream in streams:
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
        response = self.api.chat_messages(session_id, limit=HISTORY_MESSAGES)
        answer = sanitize(self._answer_after(response["data"], message_id))
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
