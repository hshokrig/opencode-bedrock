from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import unittest
from pathlib import Path

from tests.bedrock.support import fake_opencode, isolated_environment


class PackagingTests(unittest.TestCase):
    def test_artifact_manifest_checksums_and_offline_install(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        with isolated_environment() as root:
            output = root / "artifacts"
            executable = fake_opencode(root)
            env = os.environ.copy()
            env["OPENCODE_BIN"] = str(executable)
            env["HOME"] = str(root / "home")
            build = subprocess.run(
                [str(repo / "scripts" / "build-offline.sh"), str(output)],
                cwd=repo,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            archive = Path(build.stdout.strip().splitlines()[-1])
            self.assertTrue(archive.is_file())
            self.assertTrue(archive.with_suffix(archive.suffix + ".sha256").is_file())
            self.assertEqual(
                archive.with_suffix(archive.suffix + ".sha256")
                .read_text(encoding="utf-8")
                .split()[1],
                archive.name,
            )
            installer = output / "install-opencode-bedrock.sh"
            self.assertTrue(installer.is_file())
            self.assertTrue(installer.with_suffix(".sh.sha256").is_file())
            self.assertEqual(
                installer.with_suffix(".sh.sha256").read_text(encoding="utf-8").split()[1],
                installer.name,
            )

            with tarfile.open(archive) as bundle:
                manifest_name = next(
                    name for name in bundle.getnames() if name.endswith("/manifest.json")
                )
                manifest_file = bundle.extractfile(manifest_name)
                self.assertIsNotNone(manifest_file)
                assert manifest_file is not None
                manifest = json.load(manifest_file)
            self.assertEqual(
                manifest["upstream_commit"],
                "7565e03536d19e850f9996c407f9bf5e932b5f7a",
            )

            prefix = root / "installed"
            install = subprocess.run(
                [str(installer), str(archive), str(prefix)],
                cwd=repo,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            wrapper = prefix / "0.1.0" / "bin" / "opencode-bedrock"
            version = subprocess.run(
                [str(wrapper), "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout.strip(), "0.1.0")
            self.assertTrue(
                (prefix / "0.1.0" / "share" / "policies" / "sagemaker-bedrock-iam.json").is_file()
            )
            verifier = Path(env["HOME"]) / ".local" / "bin" / "opencode-bedrock-verify-aws"
            blocked = subprocess.run(
                [str(verifier)],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("paid Amazon Bedrock calls", blocked.stderr)

            tampered = output / "tampered.tar.gz"
            shutil.copyfile(archive, tampered)
            shutil.copyfile(
                archive.with_suffix(archive.suffix + ".sha256"),
                tampered.with_suffix(".gz.sha256"),
            )
            with tampered.open("ab") as handle:
                handle.write(b"tampered")
            rejected = subprocess.run(
                [str(installer), str(tampered), str(root / "bad")],
                cwd=repo,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("checksum mismatch", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
