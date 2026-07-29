from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from opencode_bedrock.service import Record
from opencode_bedrock.tasks import add, list_tasks
from tests.bedrock.support import isolated_environment


class FakeClient:
    def statuses(self) -> dict:
        return {"ses-wait": {"type": "busy"}, "ses-run": {"type": "busy"}}

    def permissions(self) -> list[dict]:
        return [{"id": "perm", "sessionID": "ses-wait"}]


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
