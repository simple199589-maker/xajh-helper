from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import _pack_upx


class UpxPackTests(unittest.TestCase):
    def test_pinned_archive_hash_is_sha256(self) -> None:
        self.assertEqual(len(_pack_upx.UPX_ARCHIVE_SHA256), 64)
        int(_pack_upx.UPX_ARCHIVE_SHA256, 16)

    def test_find_upx_prefers_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            upx = Path(temp_dir) / "upx.exe"
            upx.write_bytes(b"MZ")
            found = _pack_upx.find_upx(str(upx), allow_download=False)
            self.assertEqual(found, upx.resolve())

    def test_pack_executable_uses_overlay_copy_and_tests_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "app.exe"
            upx = root / "upx.exe"
            target.write_bytes(b"MZ" + b"before")
            upx.write_bytes(b"tool")

            commands: list[list[str]] = []

            def fake_run(command: list[str]):
                commands.append(command)
                if "--overlay=copy" in command:
                    target.write_bytes(b"MZ" + b"packed")
                return type("Result", (), {"stdout": "upx 5.2.0\n"})()

            report_path = root / "report.json"
            with (
                patch.object(_pack_upx, "_run", side_effect=fake_run),
                patch.object(_pack_upx, "REPORT_PATH", report_path),
                patch.object(_pack_upx, "ROOT", root),
            ):
                report = _pack_upx.pack_executable(target, upx)

            pack_command = commands[1]
            self.assertIn("--force", pack_command)
            self.assertIn("--lzma", pack_command)
            self.assertIn("--overlay=copy", pack_command)
            self.assertEqual(commands[2][1], "-t")
            self.assertEqual(report["upx_test"], "ok")
            self.assertTrue(report_path.is_file())
            self.assertEqual(
                report["after_sha256"], hashlib.sha256(target.read_bytes()).hexdigest()
            )

    def test_pack_rejects_non_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "payload.dll"
            upx = root / "upx.exe"
            target.write_bytes(b"MZ")
            upx.write_bytes(b"tool")
            with self.assertRaisesRegex(ValueError, "must be an .exe"):
                _pack_upx.pack_executable(target, upx)


if __name__ == "__main__":
    unittest.main()
