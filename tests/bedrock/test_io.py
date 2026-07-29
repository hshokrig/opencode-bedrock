from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opencode_bedrock.errors import BedrockError, JSONWriteError
from opencode_bedrock.io import (
    PRIVATE_JSON_LIMIT,
    locked,
    read_private_json_object,
    unlink_durable,
    write_json,
)


class IOTests(unittest.TestCase):
    def test_lock_rejects_symlinks_and_non_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.touch(mode=0o600)
            link = root / "link"
            link.symlink_to(target)
            fifo = root / "fifo"
            os.mkfifo(fifo, 0o600)

            with self.assertRaisesRegex(BedrockError, "cannot open private lock"):
                with locked(link):
                    pass
            with self.assertRaisesRegex(BedrockError, "not a regular file"):
                with locked(fifo):
                    pass

    def test_lock_hardens_existing_file_permissions_through_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.lock"
            path.touch(mode=0o644)
            os.chmod(path, 0o644)

            with locked(path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_private_json_reader_is_bounded_and_rejects_invalid_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scalar = root / "scalar.json"
            scalar.write_text("[]", encoding="utf-8")
            malformed = root / "malformed.json"
            malformed.write_text("{", encoding="utf-8")
            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * (PRIVATE_JSON_LIMIT + 1))
            for path in (scalar, malformed, oversized):
                os.chmod(path, 0o600)

            with self.assertRaisesRegex(BedrockError, "root must be a JSON object"):
                read_private_json_object(scalar)
            with self.assertRaisesRegex(BedrockError, "cannot read private JSON state"):
                read_private_json_object(malformed)
            with self.assertRaisesRegex(BedrockError, "exceeds"):
                read_private_json_object(oversized)

    def test_private_json_reader_distinguishes_missing_from_empty_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            self.assertIsNone(read_private_json_object(path, missing=None))
            write_json(path, {})
            self.assertEqual(read_private_json_object(path, missing=None), {})

    def test_private_json_reader_rejects_symlinks_and_unsafe_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "state.json"
            target.write_text("{}", encoding="utf-8")
            os.chmod(target, 0o600)
            link = root / "state-link.json"
            link.symlink_to(target)

            with self.assertRaisesRegex(BedrockError, "cannot open private JSON state"):
                read_private_json_object(link)

            os.chmod(target, 0o640)
            with self.assertRaisesRegex(BedrockError, "mode-0600 regular file"):
                read_private_json_object(target)

            fifo = root / "state-fifo.json"
            os.mkfifo(fifo, 0o600)
            with self.assertRaisesRegex(BedrockError, "mode-0600 regular file"):
                read_private_json_object(fifo)

    def test_atomic_write_and_unlink_fsync_the_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            calls: list[int] = []
            real_fsync = os.fsync

            def observe(descriptor: int) -> None:
                calls.append(descriptor)
                real_fsync(descriptor)

            with patch("opencode_bedrock.io.os.fsync", side_effect=observe):
                write_json(path, {"safe": True})
                self.assertTrue(path.exists())
                self.assertGreaterEqual(len(calls), 2)
                calls.clear()
                unlink_durable(path)
                self.assertFalse(path.exists())
                self.assertEqual(len(calls), 1)

    def test_write_error_reports_whether_atomic_replacement_committed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            with patch(
                "opencode_bedrock.io.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaises(JSONWriteError) as before:
                    write_json(path, {"version": 1})
            self.assertFalse(before.exception.committed)
            self.assertFalse(path.exists())

            with patch(
                "opencode_bedrock.io._fsync_directory",
                side_effect=OSError("directory fsync failed"),
            ):
                with self.assertRaises(JSONWriteError) as after:
                    write_json(path, {"version": 2})
            self.assertTrue(after.exception.committed)
            self.assertEqual(read_private_json_object(path), {"version": 2})

    def test_lock_and_write_translate_filesystem_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            with patch(
                "opencode_bedrock.io.tempfile.mkstemp",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertRaisesRegex(BedrockError, "cannot prepare"):
                    write_json(path, {})
            with patch(
                "opencode_bedrock.io.ensure_private_directory",
                side_effect=OSError("permission denied"),
            ):
                with self.assertRaisesRegex(BedrockError, "lock directory"):
                    with locked(path.with_suffix(".lock")):
                        pass


if __name__ == "__main__":
    unittest.main()
