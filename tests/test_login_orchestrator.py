# -*- coding: utf-8 -*-
"""Tests for the one-click login orchestration module. @author by ak"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core import login_bridge as lb
from app.core.login_orchestrator import (
    ERR_APPLY,
    ERR_LAUNCHER_MISSING,
    ERR_LOGIN,
    ERR_NO_CHARACTER,
    ERR_NO_GAME_HWND,
    LoginProgress,
    P_DONE,
    schedule_post_login,
    start_launcher_and_wait_server,
)


class ScaleClientCoordTests(unittest.TestCase):
    def test_identity_at_reference(self) -> None:
        self.assertEqual(lb.scale_client_coord((841, 804), (1600, 900), (1600, 900)), (841, 804))

    def test_scale_to_1280x720(self) -> None:
        x, y = lb.scale_client_coord((841, 804), (1600, 900), (1280, 720))
        self.assertEqual(x, 672)
        self.assertEqual(y, 643)

    def test_scale_to_larger(self) -> None:
        x, y = lb.scale_client_coord((501, 169), (1600, 900), (1920, 1080))
        self.assertEqual(x, 601)
        self.assertEqual(y, 202)

    def test_aspect_mismatch_16_9_zero(self) -> None:
        self.assertAlmostEqual(lb.client_aspect_mismatch((1600, 900)), 0.0, places=6)

    def test_aspect_mismatch_non_16_9(self) -> None:
        self.assertGreater(lb.client_aspect_mismatch((1366, 768)), 0.0)


class DismissLauncherUpdateTests(unittest.TestCase):
    def test_no_dialog_returns_found_false(self) -> None:
        class _Empty:
            def GetWindowThreadProcessId(self, _h, _p):
                return 0

            def GetClassNameW(self, h, buf, n):
                buf.value = ""
                return 0

            def GetWindowTextW(self, h, buf, n):
                buf.value = ""
                return 0

            def EnumWindows(self, cb, _lp):
                return 1

        with patch("ctypes.WinDLL", return_value=_Empty()):
            got = lb.dismiss_launcher_update_dialog(33168, timeout_s=0.5)
        self.assertFalse(got["found"])


class OrchestratorTests(unittest.TestCase):
    def test_progress_emit_calls_callback(self) -> None:
        events = []

        def cb(stage, info):
            events.append((stage, info))

        prog = LoginProgress()
        prog.emit(cb, P_DONE, "ok", role_id="123")
        self.assertEqual(events[0][0], P_DONE)
        self.assertEqual(events[0][1]["role_id"], "123")

    def test_missing_launcher_returns_error(self) -> None:
        with patch("app.core.login_orchestrator._default_log", lambda m: None), patch(
            "app.core.account_manager.remembered_launcher", return_value=""
        ):
            got = start_launcher_and_wait_server("a", "b", slot=1)
        self.assertFalse(got["ok"])
        self.assertEqual(got["error"], ERR_LAUNCHER_MISSING)

    def test_server_confirm_failure_propagates(self) -> None:
        def fake_auto_login(*_a, **_k):
            return {"ok": False, "stage": "credentials", "error": None}

        with patch("app.core.login_orchestrator._default_log", lambda m: None), patch(
            "app.core.account_manager.remembered_launcher", return_value=r"D:\x\launcher.exe"
        ), patch(
            "app.core.login_bridge.drive_launcher_to_xajh",
            return_value={"ok": True, "launcher_pid": 123, "xajh_pid": 77, "hwnd": 0x1234},
        ), patch(
            "app.core.login_bridge.inject_login_bridge", return_value=True
        ), patch(
            "app.core.game_attach.GameAttachSession"
        ), patch(
            "app.core.login_bridge.auto_login_flow", side_effect=fake_auto_login
        ):
            got = start_launcher_and_wait_server("a", "b", slot=1)
        self.assertFalse(got["ok"])
        self.assertEqual(got["error"], ERR_LOGIN)
        self.assertEqual(got["pid"], 77)

    def test_enter_world_false_stays_on_char_select(self) -> None:
        def fake_auto_login(*_a, **_k):
            return {"ok": True, "stage": "character_select", "error": None}

        with patch("app.core.login_orchestrator._default_log", lambda m: None), patch(
            "app.core.account_manager.remembered_launcher", return_value=r"D:\x\launcher.exe"
        ), patch(
            "app.core.login_bridge.drive_launcher_to_xajh",
            return_value={"ok": True, "launcher_pid": 123, "xajh_pid": 77, "hwnd": 0x1234},
        ), patch(
            "app.core.login_bridge.inject_login_bridge", return_value=True
        ), patch(
            "app.core.game_attach.GameAttachSession"
        ), patch(
            "app.core.login_bridge.auto_login_flow", side_effect=fake_auto_login
        ), patch(
            "app.core.login_bridge.read_char_select_roles",
            return_value={"ok": True, "roles": [
                {"slot": 1, "name": "甲", "level": "唐门 140级", "role_id": 1},
                {"slot": 2, "name": "", "level": "", "role_id": 0},
                {"slot": 3, "name": "", "level": "", "role_id": 0},
            ]},
        ) as read_roles, patch(
            "app.core.login_bridge.enter_world_from_char_select"
        ) as enter_world:
            got = start_launcher_and_wait_server(
                "a", "b", slot=1, enter_world=False
            )
        self.assertTrue(got["ok"])
        self.assertEqual(got["error"], None)
        self.assertEqual(got["message"], "已停留在选角页")
        self.assertEqual(got["stage"], "character_select")
        self.assertEqual(got["role_id"], "")
        enter_world.assert_not_called()

    def test_role_identity_from_char_select_when_identity_read_fails(self) -> None:
        """进世界后 read_host_identity 失败：从选角列表 + slot 推导角色归属。"""
        def fake_auto_login(*_a, **_k):
            return {"ok": True, "stage": "character_select", "error": None}

        def fake_read_host_identity(_attach, *, need_name=False, log=None):
            return ("", "")

        with patch("app.core.login_orchestrator._default_log", lambda m: None), patch(
            "app.core.account_manager.remembered_launcher", return_value=r"D:\x\launcher.exe"
        ), patch(
            "app.core.login_bridge.drive_launcher_to_xajh",
            return_value={"ok": True, "launcher_pid": 123, "xajh_pid": 77, "hwnd": 0x1234},
        ), patch(
            "app.core.login_bridge.inject_login_bridge", return_value=True
        ), patch(
            "app.core.game_attach.GameAttachSession"
        ), patch(
            "app.core.login_bridge.auto_login_flow", side_effect=fake_auto_login
        ), patch(
            "app.core.login_bridge.read_char_select_roles",
            return_value={"ok": True, "roles": [
                {"slot": 1, "name": "张三", "level": "唐门 140级", "role_id": 1001},
                {"slot": 2, "name": "李四", "level": "唐门 140级", "role_id": 1002},
                {"slot": 3, "name": "王五", "level": "唐门 140级", "role_id": 1003},
            ]},
        ), patch(
            "app.core.login_bridge.enter_world_from_char_select",
            return_value={"ok": True, "stage": "in_world"},
        ), patch(
            "app.core.team_ops.read_host_identity", side_effect=fake_read_host_identity
        ):
            got = start_launcher_and_wait_server("a", "b", slot=2)
        self.assertTrue(got["ok"])
        # 进世界后读身份失败，但仍能从选角列表推出 slot=2 的角色。
        self.assertEqual(got["role_id"], "1002")
        self.assertEqual(got["role_name"], "李四")

    def test_launcher_drive_failure_propagates(self) -> None:
        with patch("app.core.login_orchestrator._default_log", lambda m: None), patch(
            "app.core.account_manager.remembered_launcher", return_value=r"D:\x\launcher.exe"
        ), patch(
            "app.core.login_bridge.drive_launcher_to_xajh",
            return_value={"ok": False, "error": "no_game_hwnd", "message": "等待游戏窗口超时"},
        ):
            got = start_launcher_and_wait_server("a", "b", slot=1)
        self.assertFalse(got["ok"])
        self.assertEqual(got["error"], ERR_NO_GAME_HWND)

    def test_schedule_post_login_returns_immediately(self) -> None:
        """登录后延迟编排：立即返回安排信息，不阻塞（线程异步执行）。"""
        with patch("app.core.login_orchestrator._default_log", lambda m: None):
            got = schedule_post_login(
                "accX", 123, 0x1234,
                inject_delay_s=0.0, hang_delay_s=0.0,
            )
        self.assertTrue(got["ok"])
        self.assertIn("注入", got["note"])
        # 无 pid -> 直接失败，不开线程。
        bad = schedule_post_login("accX", 0, 0, log=lambda m: None)
        self.assertFalse(bad["ok"])

    def test_schedule_post_login_default_timings(self) -> None:
        """默认延迟注入15s；挂机默认不勾选。"""
        with patch("app.core.login_orchestrator._default_log", lambda m: None):
            got = schedule_post_login("accD", 123, 0x1234, log=lambda m: None)
        self.assertTrue(got["ok"])
        self.assertIn("15s", got["note"])
        # 挂机默认未勾选 -> note 不含开挂
        self.assertNotIn("开挂", got["note"])
        with patch("app.core.login_orchestrator._default_log", lambda m: None):
            got2 = schedule_post_login(
                "accD", 123, 0x1234, hang_enabled=True, log=lambda m: None
            )
        self.assertIn("15s", got2["note"])
        self.assertIn("5s 开挂", got2["note"])

    def test_schedule_post_login_without_hang_note(self) -> None:
        """未勾选挂机时只安排注入，不执行其它账号属性业务。"""
        with patch("app.core.login_orchestrator._default_log", lambda m: None):
            got = schedule_post_login(
                "accT", 123, 0x1234,
                hang_enabled=False,
                log=lambda m: None,
            )
        self.assertTrue(got["ok"])
        self.assertNotIn("开挂", got["note"])
        self.assertNotIn("队内控", got["note"])


if __name__ == "__main__":
    unittest.main()
