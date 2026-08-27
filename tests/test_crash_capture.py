# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import crash_capture, diag_log


class CrashCaptureTests(unittest.TestCase):
    def test_describe_exit_code_access_violation(self) -> None:
        s = crash_capture.describe_exit_code(0xC0000005)
        self.assertIn("ACCESS_VIOLATION", s)
        self.assertIn("C0000005", s.upper())

    def test_format_crash_point_from_exception(self) -> None:
        try:
            raise RuntimeError("boom-point")
        except RuntimeError:
            import sys

            et, ev, tb = sys.exc_info()
            point = crash_capture.format_crash_point(et, ev, tb)
        self.assertIn("boom-point", point)
        self.assertIn("RuntimeError", point)

    def test_helper_and_game_crash_go_to_error_log_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td) / "logs"
            log_dir.mkdir()
            err = log_dir / "xajh_helper_error_20260724.log"

            def fake_error_path() -> Path:
                return err

            with patch.object(crash_capture, "_error_path", side_effect=fake_error_path), patch.object(
                diag_log, "error_log_path", side_effect=fake_error_path
            ), patch.object(diag_log, "_resolve_log_dir", return_value=log_dir), patch.object(
                diag_log, "_file_enabled", False
            ), patch.object(diag_log, "_error_header_done", False), patch.object(
                diag_log, "_error_log_path", err
            ), patch.object(diag_log, "_error_day", "20260724"):
                try:
                    raise ValueError("helper-down")
                except ValueError:
                    import sys

                    et, ev, tb = sys.exc_info()
                    point = crash_capture.report_helper_crash(
                        "unit", exc_type=et, exc=ev, tb=tb
                    )
                crash_capture.clear_game_crash_dedupe()
                # seed a native-looking block already in error log
                err.write_text(
                    err.read_text(encoding="utf-8")
                    + "\n=== XAJH GAME_CRASH kind=游戏崩溃 ===\n"
                    + "pid=4242\n"
                    + "crash_point=0x00401234 xajh.exe+0x1234\n",
                    encoding="utf-8",
                )
                with patch.object(
                    crash_capture, "process_exit_code", return_value=0xC0000005
                ):
                    gpoint = crash_capture.report_game_crash(
                        4242, title="unit", reason="unit_test"
                    )
                body = err.read_text(encoding="utf-8")

        self.assertIn("helper-down", point)
        self.assertIn("HELPER_CRASH", body)
        self.assertIn("助手崩溃", body)
        self.assertIn("xajh.exe+0x1234", gpoint)
        self.assertIn("GAME_CRASH", body)
        # no separate crash files
        self.assertFalse(any(log_dir.glob("xajh_helper_crash_*")))
        self.assertFalse(any(log_dir.glob("xajh_game_crash_*")))

    def test_native_game_crash_path_is_error_log(self) -> None:
        p = crash_capture.native_game_crash_path(123)
        self.assertTrue(p.name.startswith("xajh_helper_error_"))
        self.assertTrue(p.name.endswith(".log"))

    def test_note_bridge_context(self) -> None:
        crash_capture.note_bridge_context(99, cmd=7, err="seh", note="x")
        ctx = crash_capture.get_bridge_context(99)
        self.assertEqual(ctx.get("cmd"), 7)
        self.assertEqual(ctx.get("err"), "seh")

    def test_parse_native_crash_point(self) -> None:
        text = "foo\ncrash_point=0x1 module+0x2\nbar\n"
        self.assertEqual(
            crash_capture.parse_native_crash_point(text), "0x1 module+0x2"
        )


class NormalLogRotationTests(unittest.TestCase):
    def test_normal_log_rolls_after_20mb(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td) / "logs"
            log_dir.mkdir()
            day = "20260724"
            part1 = log_dir / f"xajh_helper_{day}.log"
            part1.write_text("seed\n", encoding="utf-8")  # must exist to be considered full
            # just under / over threshold without writing 20MB of real data:
            # patch size checker
            sizes = {str(part1): diag_log.NORMAL_LOG_MAX_BYTES}

            def fake_size(path: Path) -> int:
                return int(sizes.get(str(path), 0))

            with patch.object(diag_log, "_resolve_log_dir", return_value=log_dir), patch.object(
                diag_log, "_file_size", side_effect=fake_size
            ), patch.object(diag_log, "_log_path", None), patch.object(
                diag_log, "_log_day", ""
            ), patch.object(diag_log, "_log_part", 1), patch.object(
                diag_log, "_session_started", "t"
            ), patch.object(diag_log, "_initialized", True), patch.object(
                diag_log, "_file_enabled", True
            ), patch.object(diag_log, "_error_header_done", True), patch(
                "app.core.diag_log.datetime"
            ) as dt:
                class _D:
                    @staticmethod
                    def now():
                        class _N:
                            def strftime(self, fmt):
                                if fmt == "%Y%m%d":
                                    return day
                                return "2026-07-24 00:00:00.000"

                        return _N()

                dt.now = _D.now
                # first pick sees part1 full -> should go to _2
                sizes[str(part1)] = diag_log.NORMAL_LOG_MAX_BYTES
                path = diag_log._pick_normal_log_path(day)
                self.assertEqual(path.name, f"xajh_helper_{day}_2.log")

                # writing path rotation helper
                sizes[str(path)] = diag_log.NORMAL_LOG_MAX_BYTES
                diag_log._log_path = path
                diag_log._log_part = 2
                diag_log._maybe_roll_normal_after_write(path)
                self.assertEqual(diag_log._log_part, 3)
                self.assertEqual(
                    diag_log._log_path.name, f"xajh_helper_{day}_3.log"
                )

    def test_normal_log_name(self) -> None:
        self.assertEqual(diag_log._normal_log_name("20260724", 1), "xajh_helper_20260724.log")
        self.assertEqual(diag_log._normal_log_name("20260724", 2), "xajh_helper_20260724_2.log")


if __name__ == "__main__":
    unittest.main()
