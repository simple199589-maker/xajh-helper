from __future__ import annotations

import types
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import diag_log
from memory.proc_inspect import find_procs, proc_snapshot


class _Proc:
    def __init__(self, pid: int, name: str, exe: str = ""):
        self.pid = pid
        self.info = {"pid": pid, "name": name, "exe": exe}


class DiagnosticsAndProcessTests(unittest.TestCase):
    def test_log_dir_is_fixed_beside_executable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("common.paths.app_root", return_value=root):
                self.assertEqual(diag_log._resolve_log_dir(), root / "logs")

    def test_captcha_debug_dir_is_fixed_beside_executable(self) -> None:
        from app.core.yaolu_auto import _debug_dir

        with tempfile.TemporaryDirectory() as td, patch(
            "common.paths.app_root", return_value=Path(td)
        ):
            self.assertEqual(_debug_dir(), Path(td) / "captures" / "captcha")

    def test_tk_callback_exception_is_persisted(self) -> None:
        from app.ui.app_shell import ShellApp

        try:
            raise RuntimeError("callback failed")
        except RuntimeError:
            import sys

            exc_type, exc_value, exc_tb = sys.exc_info()
            with patch(
                "app.core.crash_capture.report_tk_callback_exception"
            ) as report:
                ShellApp.report_callback_exception(
                    object(), exc_type, exc_value, exc_tb
                )
        report.assert_called_once()
        args = report.call_args.args
        self.assertIs(args[0], exc_type)
        self.assertIs(args[1], exc_value)

    def test_info_notifies_listener_when_file_logging_off(self) -> None:
        lines: list[str] = []
        diag_log.add_listener(lines.append)
        with patch.object(diag_log, "_file_enabled", False), patch.object(diag_log, "_append_line") as append:
            line = diag_log.info("offline-unit-test", tag="TEST")
        self.assertIn("offline-unit-test", line)
        self.assertTrue(any("offline-unit-test" in item for item in lines))
        append.assert_not_called()
        diag_log._listeners.remove(lines.append)

    def test_process_alive_rejects_invalid_pid(self) -> None:
        self.assertFalse(diag_log.process_alive(0))
        self.assertFalse(diag_log.process_alive(-1))

    @patch("memory.proc_inspect.psutil.process_iter")
    def test_find_processes_filters_and_deduplicates(self, process_iter) -> None:
        process_iter.return_value = [
            _Proc(1, "xajh.exe", r"D:\game\bin\xajh.exe"),
            _Proc(1, "xajh.exe", r"D:\game\bin\xajh.exe"),
            _Proc(2, "other.exe", r"D:\other.exe"),
        ]
        self.assertEqual([p.pid for p in find_procs()], [1])
        self.assertEqual([p.pid for p in find_procs("xajh")], [1])

    def test_process_snapshot_tolerates_optional_failures(self) -> None:
        p = types.SimpleNamespace(
            pid=7,
            name=lambda: "xajh.exe",
            exe=lambda: r"D:\game\xajh.exe",
            cwd=lambda: r"D:\game",
            cmdline=lambda: ["xajh.exe"],
            create_time=lambda: 1.0,
            username=lambda: "tester",
            memory_info=lambda: types.SimpleNamespace(rss=10, vms=20),
            num_threads=lambda: 3,
            net_connections=lambda kind: [],
            memory_maps=lambda grouped=False: [],
        )
        snap = proc_snapshot(p)
        self.assertEqual(snap["pid"], 7)
        self.assertEqual(snap["memory"], {"rss": 10, "vms": 20})
        self.assertEqual(snap["threads"], 3)


if __name__ == "__main__":
    unittest.main()
