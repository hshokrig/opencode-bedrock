from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from opencode_bedrock.cli import submit_task
from opencode_bedrock.errors import TransportError
from opencode_bedrock.service import Record
from opencode_bedrock.tasks import add, list_tasks
from tests.bedrock.support import isolated_environment


class FakeClient:
    def statuses(self) -> dict:
        return {"ses-wait": {"type": "busy"}, "ses-run": {"type": "busy"}}

    def permissions(self) -> list[dict]:
        return [{"id": "perm", "sessionID": "ses-wait"}]

    def active_chat_sessions(self) -> set[str]:
        return {"ses-run"}


class TaskTests(unittest.TestCase):
    def test_pending_permission_marks_only_its_task_awaiting_approval(self) -> None:
        with isolated_environment():
            record = self.record()
            service = (
                Path(os.environ["XDG_STATE_HOME"]) / "opencode-bedrock" / "services" / record.key
            )
            service.mkdir(parents=True)
            add(record, "ses-wait", "edit", "build")
            add(record, "ses-run", "read", "plan")
            with patch("opencode_bedrock.tasks.client", return_value=FakeClient()):
                tasks = list_tasks(record)
            self.assertEqual(tasks[0]["status"], "awaiting approval")
            self.assertEqual(tasks[1]["status"], "busy")

    def test_submit_uses_exact_durable_identity_after_uncertain_transport(self) -> None:
        class DurableClient(FakeClient):
            def __init__(self) -> None:
                self.created: list[tuple[str, str]] = []
                self.prompts: list[tuple[str, str, str]] = []

            def create_task_session(self, session_id: str, agent: str) -> dict:
                self.created.append((session_id, agent))
                return {"id": session_id}

            def prompt_chat(self, session_id: str, message_id: str, prompt: str) -> dict:
                self.prompts.append((session_id, message_id, prompt))
                if len(self.prompts) == 1:
                    raise TransportError("lost response")
                return {"id": message_id}

        with isolated_environment():
            record = self.record()
            service = (
                Path(os.environ["XDG_STATE_HOME"])
                / "opencode-bedrock"
                / "services"
                / record.key
            )
            service.mkdir(parents=True)
            api = DurableClient()
            with patch("opencode_bedrock.cli.client", return_value=api):
                self.assertEqual(submit_task(record, "durable task", "build"), 0)
            task = list_tasks(record, refresh=False)[0]
            self.assertEqual(api.created, [(task["session_id"], "build")])
            self.assertEqual(
                api.prompts,
                [
                    (task["session_id"], task["message_id"], "durable task"),
                    (task["session_id"], task["message_id"], "durable task"),
                ],
            )
            self.assertEqual(task["status"], "submitted")

    @staticmethod
    def record() -> Record:
        return Record(
            key="sample-key",
            project="sample",
            workspace="/tmp/sample",
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
            log="/tmp/log",
        )


if __name__ == "__main__":
    unittest.main()
