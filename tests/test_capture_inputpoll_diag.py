# -*- coding: utf-8 -*-
"""Unit tests for temporary InputPoll capture helper. @author by ak"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch
import importlib.util


def _load_helper():
    path = Path(__file__).resolve().parents[1] / "tools" / "_capture_inputpoll_diag.py"
    spec = importlib.util.spec_from_file_location("capture_inputpoll_diag_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CaptureInputPollDiagTests(unittest.TestCase):
    def test_build_script_uses_authoritative_sites(self) -> None:
        mod = _load_helper()
        script = mod.build_cdb_script(0x400000)
        self.assertIn("bp 0x4baffc", script.lower())
        self.assertIn("bp 0x4bb3a6", script.lower())
        self.assertIn("gc", script)
        self.assertIn("qd", script)
        self.assertNotIn("bc *", script)
        self.assertIn('\\"', script)

    def test_parse_and_summarize_prefers_d6(self) -> None:
        mod = _load_helper()
        raw = (
            "noise\n"
            "POLL hit=1 b1=0 in=30BEB9B0 ctrl=30D2D2D0 gate=1 f=1/1/0/0 d6=0\n"
            "POLL hit=1 b1=1 in=00000000 ctrl=00000000 gate=-1 f=-1/-1/-1/-1 d6=1\n"
        )
        records = mod.parse_cdb_output(raw)
        self.assertEqual(len(records), 2)
        result = mod.summarize_poll_records(records)
        self.assertEqual(result.classification(), "DISPATCH_REACHED")
        self.assertEqual(result.hit, 2)
        self.assertEqual(result.d6, 1)
        self.assertEqual(result.bind1, 1)
        self.assertEqual(result.controller, 0x30D2D2D0)

    def test_run_cdb_reports_missing_cdb(self) -> None:
        mod = _load_helper()
        with patch.object(mod, "find_cdb", return_value=None):
            ok, note, result = mod.run_inputpoll_capture_cdb(
                1, 0x400000, timeout_s=1.0
            )
        self.assertFalse(ok)
        self.assertIn("cdb not found", note)
        self.assertIsNone(result)

    def test_find_cdb_prefers_x86_when_present(self) -> None:
        mod = _load_helper()
        # Live search should prefer an x86 package binary when WinDbg is installed.
        hit = mod.find_cdb(prefer_x86=True)
        if hit is None:
            self.skipTest("cdb not installed on this machine")
        low = str(hit).lower().replace("/", "\\")
        self.assertTrue(
            "\\x86\\" in low or low.endswith("cdbx86.exe") or "cdb" in low,
            msg=hit,
        )


if __name__ == "__main__":
    unittest.main()
