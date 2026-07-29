from __future__ import annotations

import unittest
from pathlib import Path

from opencode_bedrock.errors import BedrockError
from opencode_bedrock.projects import add, get, list_projects
from opencode_bedrock.workspace import (
    canonical_workspace,
    contains,
    service_key,
    validate_mount_separation,
)
from tests.bedrock.support import git_repository, isolated_environment


class ProjectTests(unittest.TestCase):
    def test_add_list_and_get_canonical_project(self) -> None:
        with isolated_environment() as root:
            repo = git_repository(root)
            project = add("sample", str(repo))
            self.assertEqual(project.path, repo)
            self.assertEqual(list_projects(), [project])
            self.assertEqual(get("sample"), project)

    def test_duplicate_workspace_alias_is_rejected(self) -> None:
        with isolated_environment() as root:
            repo = git_repository(root)
            add("first", str(repo))
            with self.assertRaisesRegex(BedrockError, "already registered"):
                add("second", str(repo))

    def test_non_git_requires_explicit_opt_in(self) -> None:
        with isolated_environment() as root:
            directory = root / "plain"
            directory.mkdir()
            with self.assertRaisesRegex(BedrockError, "not a Git"):
                canonical_workspace(str(directory))
            self.assertEqual(
                canonical_workspace(str(directory), allow_non_git=True), directory.resolve()
            )

    def test_relative_workspace_is_rejected(self) -> None:
        with self.assertRaisesRegex(BedrockError, "absolute"):
            canonical_workspace("relative")

    def test_service_key_separates_same_basename(self) -> None:
        self.assertNotEqual(service_key(None, Path("/a/repo")), service_key(None, Path("/b/repo")))

    def test_contains_does_not_accept_sibling_prefix(self) -> None:
        self.assertFalse(contains(Path("/tmp/work"), Path("/tmp/work-other")))

    def test_system_root_cannot_be_mounted_as_a_workspace(self) -> None:
        with self.assertRaisesRegex(BedrockError, "protected system"):
            validate_mount_separation(Path("/usr"), Path("/tmp/service-state"))

    def test_repository_nested_below_system_mount_is_allowed(self) -> None:
        validate_mount_separation(Path("/opt/project"), Path("/tmp/service-state"))

    def test_credential_directory_cannot_be_mounted_as_a_workspace(self) -> None:
        with self.assertRaisesRegex(BedrockError, "credential path"):
            validate_mount_separation(Path.home() / ".aws" / "project", Path("/tmp/state"))


if __name__ == "__main__":
    unittest.main()
