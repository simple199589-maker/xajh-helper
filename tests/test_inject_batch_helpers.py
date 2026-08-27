# -*- coding: utf-8 -*-
"""Unit tests for 一键登录 batch-inject failure classification (no live game)."""
from __future__ import annotations

import unittest

from app.core.inject_gate import batch_failure_category, is_batch_retryable


class BatchFailureClassificationTests(unittest.TestCase):
    def test_retryable_are_fast_transients_only(self) -> None:
        for code in (
            "BRIDGE_PING_TIMEOUT",
            "PING_TIMEOUT_NO_HOOK",
            "HOOK_TIMEOUT",
            "HOOK_NO_HWND",
            "SHM_NOT_OPEN",
            "UNKNOWN",
            "PING_TIMEOUT_SOMETHING",
        ):
            self.assertTrue(is_batch_retryable(code), code)

    def test_blocking_failures_are_terminal(self) -> None:
        # Long-running / fatal codes must NOT hold the queue up.
        for code in (
            "GAME_NOT_READY",
            "INJECT_TIMEOUT",
            "GAME_CRASH",
            "GAME_DEAD",
            "OPENPROCESS_DENIED",
            "OPENPROCESS_ACCESS_DENIED",
            "LEGACY_BRIDGE_RESTART_GAME",
            "STALE_BRIDGE_RESTART_GAME",
            "BRIDGE_MISSING",
            "NO_GAME",
            "ARCH_MISMATCH",
            "DLL_PATH_LONG",
            "GREEN_NO_HWND",
            "INJECT_EXE_FAIL",
            "CREATEREMOTETHREAD_FAIL",
            "LOADLIBRARY_FAIL",
        ):
            self.assertFalse(is_batch_retryable(code), code)

    def test_categories(self) -> None:
        self.assertEqual(batch_failure_category("GAME_NOT_READY", "host_context=0"), "未进角色")
        self.assertEqual(
            batch_failure_category("GAME_NOT_READY", "process_age=3.0s<8.0s"), "游戏未就绪"
        )
        self.assertEqual(batch_failure_category("INJECT_TIMEOUT"), "注入超时")
        self.assertEqual(batch_failure_category("GAME_CRASH"), "游戏崩溃/退出")
        self.assertEqual(batch_failure_category("OPENPROCESS_DENIED"), "权限/拦截")
        self.assertEqual(batch_failure_category("BRIDGE_MISSING"), "环境问题")
        self.assertEqual(batch_failure_category("GREEN_NO_HWND"), "绿端无窗口")
        self.assertEqual(batch_failure_category("BRIDGE_PING_TIMEOUT"), "桥接超时")
        self.assertEqual(batch_failure_category("WEIRD_CODE"), "注入失败")


if __name__ == "__main__":
    unittest.main()
