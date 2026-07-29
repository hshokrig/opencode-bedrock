from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from opencode_bedrock.errors import BedrockError
from opencode_bedrock.policy import opencode_config


class PolicyTests(unittest.TestCase):
    def test_default_policy_gates_edits_and_shell(self) -> None:
        config = opencode_config("eu-north-1", "profile-id", None, "approval")
        self.assertEqual(config["permission"]["edit"], "ask")
        self.assertEqual(config["permission"]["bash"]["*"], "ask")
        self.assertEqual(config["permission"]["bash"]["git status --short"], "allow")
        self.assertNotIn("git status*", config["permission"]["bash"])
        self.assertNotIn("git diff", config["permission"]["bash"])
        self.assertEqual(
            config["permission"]["bash"]["git diff --no-ext-diff --no-textconv"],
            "allow",
        )
        self.assertNotIn("rg *", config["permission"]["bash"])
        self.assertEqual(config["permission"]["external_directory"], "deny")
        self.assertEqual(config["permission"]["webfetch"], "deny")
        provider = config["provider"]["amazon-bedrock"]
        self.assertEqual(provider["npm"], "@ai-sdk/amazon-bedrock")
        self.assertEqual(
            provider["models"]["opus"]["limit"],
            {"context": 200_000, "input": 200_000, "output": 20_000},
        )
        self.assertEqual(config["agent"]["chat"]["model"], "amazon-bedrock/opus")
        self.assertFalse(config["agent"]["chat"]["tools_enabled"])
        self.assertFalse(config["agent"]["chat"]["workspace_instructions"])
        self.assertEqual(config["agent"]["chat"]["permission"]["*"], "deny")
        self.assertEqual(
            config["compaction"],
            {
                "auto": True,
                "tail_turns": 10,
                "preserve_recent_tokens": 40_000,
                "reserved": 20_000,
            },
        )

    def test_workspace_write_only_relaxes_edits(self) -> None:
        config = opencode_config("eu-north-1", "profile-id", None, "workspace-write")
        self.assertEqual(config["permission"]["edit"], "allow")
        self.assertEqual(config["permission"]["bash"]["*"], "ask")

    def test_secret_file_patterns_are_denied_after_allow(self) -> None:
        rules = opencode_config("eu-north-1", "profile-id", None, "approval")["permission"]["read"]
        self.assertEqual(next(iter(rules)), "*")
        self.assertEqual(rules["**/.env"], "deny")
        self.assertEqual(rules["**/*.pem"], "deny")

    def test_region_and_profile_are_required(self) -> None:
        with self.assertRaises(BedrockError):
            opencode_config("", "profile-id", None, "approval")
        with self.assertRaises(BedrockError):
            opencode_config("eu-north-1", "", None, "approval")

    def test_workspace_agents_and_model_overrides_are_source_controlled(self) -> None:
        with TemporaryDirectory() as value:
            workspace = Path(value)
            agents = workspace / "AGENTS.md"
            agents.write_text("# Instructions\n", encoding="utf-8")
            config = opencode_config(
                "eu-north-1",
                "primary-profile",
                None,
                "approval",
                workspace,
                {"review": "review-profile"},
            )
        self.assertEqual(config["instructions"], [str(agents)])
        self.assertEqual(
            config["provider"]["amazon-bedrock"]["models"]["agent-review"]["id"],
            "review-profile",
        )
        self.assertEqual(
            config["provider"]["amazon-bedrock"]["models"]["agent-review"]["limit"],
            {"context": 200_000, "input": 200_000, "output": 20_000},
        )
        self.assertEqual(config["agent"]["review"]["model"], "amazon-bedrock/agent-review")

    def test_unknown_agent_model_override_is_rejected(self) -> None:
        with self.assertRaisesRegex(BedrockError, "unsupported agent"):
            opencode_config(
                "eu-north-1",
                "profile-id",
                None,
                "approval",
                agent_models={"unknown": "profile"},
            )


if __name__ == "__main__":
    unittest.main()
