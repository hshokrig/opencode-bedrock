from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from opencode_bedrock.sandbox import command, environment
from tests.bedrock.support import fake_opencode, git_repository, isolated_environment


class SandboxTests(unittest.TestCase):
    def test_environment_is_allowlisted_and_offline(self) -> None:
        with isolated_environment() as root:
            service = root / "service"
            with patch.dict(
                os.environ,
                {"AWS_REGION": "eu-north-1", "UNRELATED_SECRET": "must-not-pass"},
                clear=False,
            ):
                env = environment(service, {"model": "amazon-bedrock/opus"}, "password")
            self.assertEqual(env["AWS_REGION"], "eu-north-1")
            self.assertNotIn("UNRELATED_SECRET", env)
            self.assertNotIn("AWS_PROFILE", env)
            self.assertNotIn("AWS_CONFIG_FILE", env)
            self.assertNotIn("AWS_SHARED_CREDENTIALS_FILE", env)
            self.assertEqual(env["OPENCODE_DISABLE_MODELS_FETCH"], "1")
            self.assertEqual(env["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
            self.assertEqual(env["GIT_CONFIG_KEY_0"], "core.fsmonitor")
            self.assertEqual(env["GIT_CONFIG_VALUE_0"], "false")

    @unittest.skipUnless(Path("/usr/bin/bwrap").exists(), "bubblewrap is not installed")
    def test_mount_namespace_hides_outside_files_and_symlink_targets(self) -> None:
        with isolated_environment() as root:
            workspace = git_repository(root, "repo-a")
            other_workspace = git_repository(root, "repo-b")
            service = root / "service"
            service.mkdir()
            opencode = fake_opencode(root)
            outside = root / "outside-secret"
            outside.write_text("secret", encoding="utf-8")
            (workspace / "escape").symlink_to(outside)
            environment(service, {"model": "amazon-bedrock/opus"}, "password")
            args = command(workspace, service, opencode, 45678)
            separator = args.index("--")
            script = (
                "from pathlib import Path; "
                "assert Path('/proc/self/maps').is_file(); "
                "assert Path('/dev/urandom').exists(); "
                f"assert not Path({str(outside)!r}).exists(); "
                f"assert not Path({str(workspace / 'escape')!r}).exists(); "
                f"assert not Path({str(other_workspace)!r}).exists()"
            )
            isolated = args[: separator + 1] + ["/usr/bin/python3", "-c", script]
            result = subprocess.run(
                isolated, check=False, capture_output=True, text=True, timeout=15
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_workspace_binary_is_remounted_read_only(self) -> None:
        with isolated_environment() as root:
            workspace = git_repository(root)
            service = root / "service"
            service.mkdir()
            environment(service, {"model": "amazon-bedrock/opus"}, "password")
            executable = fake_opencode(workspace)
            args = command(workspace, service, executable, 45678)
            self.assertIn(
                ["--ro-bind", str(executable), str(executable)],
                [args[index : index + 3] for index in range(len(args) - 2)],
            )

    @unittest.skipUnless(Path("/usr/bin/bwrap").exists(), "bubblewrap is not installed")
    def test_unattended_git_commands_do_not_run_repository_commands(self) -> None:
        with isolated_environment() as root:
            workspace = git_repository(root)
            (workspace / ".gitattributes").write_text("* diff=evil\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(workspace), "add", ".gitattributes"], check=True)
            subprocess.run(
                ["git", "-C", str(workspace), "commit", "-q", "-m", "attributes"],
                check=True,
            )
            command_script = workspace / "repository-command"
            command_script.write_text(
                "#!/bin/sh\ntouch repository-command-ran\nexit 0\n",
                encoding="utf-8",
            )
            command_script.chmod(0o755)
            subprocess.run(
                ["git", "-C", str(workspace), "config", "core.fsmonitor", str(command_script)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(workspace), "config", "diff.evil.command", str(command_script)],
                check=True,
            )
            (workspace / "README.md").write_text("changed\n", encoding="utf-8")

            service = root / "service"
            service.mkdir()
            executable = fake_opencode(root)
            env = environment(service, {"model": "amazon-bedrock/opus"}, "password")
            args = command(workspace, service, executable, 45678)
            separator = args.index("--")
            for git_args in [
                ["git", "status", "--short"],
                ["git", "diff", "--no-ext-diff", "--no-textconv"],
            ]:
                result = subprocess.run(
                    args[: separator + 1] + git_args,
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((workspace / "repository-command-ran").exists())


if __name__ == "__main__":
    unittest.main()
