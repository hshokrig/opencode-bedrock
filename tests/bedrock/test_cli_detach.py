from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from tests.bedrock.support import fake_opencode, git_repository, isolated_environment


@unittest.skipUnless(Path("/usr/bin/bwrap").exists(), "bubblewrap is not installed")
class DetachedCliTests(unittest.TestCase):
    def test_service_survives_launcher_exit_and_stops_from_another_process(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        with isolated_environment() as root:
            workspace = git_repository(root)
            env = os.environ.copy()
            executable = fake_opencode(root)
            command = [
                "python3",
                "-m",
                "opencode_bedrock",
                "start",
                "--workspace",
                str(workspace),
                "--region",
                "eu-north-1",
                "--inference-profile",
                "test-profile",
                "--opencode-bin",
                str(executable),
            ]
            started = subprocess.run(
                command,
                cwd=repo,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(started.returncode, 0, started.stderr)

            try:
                status = subprocess.run(
                    [
                        "python3",
                        "-m",
                        "opencode_bedrock",
                        "status",
                        "--workspace",
                        str(workspace),
                    ],
                    cwd=repo,
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertIn("running", status.stdout)
                self.assertIn(str(workspace), status.stdout)
            finally:
                stopped = subprocess.run(
                    [
                        "python3",
                        "-m",
                        "opencode_bedrock",
                        "stop",
                        "--workspace",
                        str(workspace),
                    ],
                    cwd=repo,
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            self.assertEqual(stopped.returncode, 0, stopped.stderr)


if __name__ == "__main__":
    unittest.main()
