from __future__ import annotations

import os
import unittest
from pathlib import Path

from opencode_bedrock.errors import BedrockError
from opencode_bedrock.service import (
    Record,
    alive,
    client,
    load_record,
    start,
    stop,
)
from opencode_bedrock.tasks import add as add_task
from opencode_bedrock.tasks import list_tasks
from tests.bedrock.support import fake_opencode, git_repository, isolated_environment


@unittest.skipUnless(Path("/usr/bin/bwrap").exists(), "bubblewrap is not installed")
class ServiceTests(unittest.TestCase):
    def test_background_lifecycle_task_state_and_duplicate_prevention(self) -> None:
        with isolated_environment() as root:
            workspace = git_repository(root)
            executable = fake_opencode(root)
            record = start(
                workspace,
                "sample",
                "eu-north-1",
                "test-profile",
                None,
                "approval",
                False,
                None,
                str(executable),
            )
            try:
                self.assertTrue(alive(record))
                self.assertTrue(client(record).health()["healthy"])
                session = client(record).create_session("test")
                task = add_task(record, session["id"], "test prompt", "build")
                self.assertEqual(list_tasks(record)[0]["id"], task["id"])
                with self.assertRaisesRegex(BedrockError, "already"):
                    start(
                        workspace,
                        "other",
                        "eu-north-1",
                        "test-profile",
                        None,
                        "approval",
                        False,
                        None,
                        str(executable),
                    )
            finally:
                stop(record, timeout=5)
            self.assertFalse(alive(record))

    def test_stale_pid_is_detected_and_recovered_on_start(self) -> None:
        with isolated_environment() as root:
            workspace = git_repository(root)
            executable = fake_opencode(root)
            record = start(
                workspace,
                "sample",
                "eu-north-1",
                "test-profile",
                None,
                "approval",
                False,
                None,
                str(executable),
            )
            stop(record, timeout=5)
            directory = (
                Path(os.environ["XDG_STATE_HOME"]) / "opencode-bedrock" / "services" / record.key
            )
            stale = Record(**{**record.__dict__, "pid": os.getpid(), "process_start": 0})
            from opencode_bedrock.io import write_json

            write_json(directory / "service.json", stale.__dict__)
            self.assertFalse(alive(stale))
            recovered = start(
                workspace,
                "sample",
                "eu-north-1",
                "test-profile",
                None,
                "approval",
                False,
                None,
                str(executable),
            )
            try:
                self.assertNotEqual(recovered.pid, stale.pid)
                self.assertEqual(load_record(directory), recovered)
            finally:
                stop(recovered, timeout=5)

    def test_pid_start_time_prevents_signaling_reused_pid(self) -> None:
        record = Record(
            key="test",
            project=None,
            workspace="/tmp/workspace",
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
        self.assertFalse(alive(record))

    def test_foreground_startup_failure_cleans_service_state(self) -> None:
        with isolated_environment() as root:
            workspace = git_repository(root)
            executable = root / "failing-opencode"
            executable.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            executable.chmod(0o755)
            with self.assertRaisesRegex(BedrockError, "exited during startup"):
                start(
                    workspace,
                    "sample",
                    "eu-north-1",
                    "test-profile",
                    None,
                    "approval",
                    True,
                    None,
                    str(executable),
                )
            states = list(
                (Path(os.environ["XDG_STATE_HOME"]) / "opencode-bedrock" / "services").glob(
                    "*/service.json"
                )
            )
            self.assertEqual(states, [])


if __name__ == "__main__":
    unittest.main()
