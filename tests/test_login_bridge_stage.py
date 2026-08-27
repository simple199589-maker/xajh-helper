# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core import login_bridge as lb


class ClipboardVerifiedTests(unittest.TestCase):
    def test_verified_returns_true_when_readback_matches(self) -> None:
        with patch("app.core.login_bridge._set_clipboard_text", return_value=True), patch(
            "app.core.login_bridge._get_clipboard_text", return_value="abc123"
        ):
            self.assertTrue(lb._set_clipboard_verified("abc123", log=lambda m: None))

    def test_verified_retries_on_mismatch(self) -> None:
        calls = {"set": 0}

        def fake_set(text):
            calls["set"] += 1
            return True

        def fake_get():
            return "other" if calls["set"] < 2 else "abc123"

        with patch("app.core.login_bridge._set_clipboard_text", side_effect=fake_set), patch(
            "app.core.login_bridge._get_clipboard_text", side_effect=fake_get
        ):
            self.assertTrue(lb._set_clipboard_verified("abc123", retries=3, log=lambda m: None))
        self.assertEqual(calls["set"], 2)

    def test_verified_fails_after_retries_exhausted(self) -> None:
        with patch("app.core.login_bridge._set_clipboard_text", return_value=True), patch(
            "app.core.login_bridge._get_clipboard_text", return_value="wrong"
        ):
            self.assertFalse(lb._set_clipboard_verified("abc123", retries=2, log=lambda m: None))

    def test_paste_fill_uses_verified_clipboard(self) -> None:
        mon = SimpleNamespace(start=lambda: None, stop=lambda: None,
                              note_inject=lambda: None,
                              idle_seconds=lambda: 9.0,
                              real_input_since_inject=lambda: False)

        def _get_wpid(_h, pid_buf):
            # ctypes.byref -> CArgObject with _obj pointing at the DWORD buffer.
            try:
                pid_buf._obj.value = 7
            except Exception:
                pass
            return 7

        u32 = SimpleNamespace(
            IsWindow=lambda _h: True,
            GetWindowThreadProcessId=_get_wpid,
        )

        with patch("app.core.login_bridge._user32", u32), patch(
            "app.core.login_bridge._fg_game_no_alt", return_value=True
        ), patch(
            "app.core.login_bridge.ui_click", return_value=SimpleNamespace(ok=True, note="ok")
        ), patch(
            "app.core.login_bridge._set_clipboard_verified", return_value=True
        ) as verified, patch(
            "app.core.login_bridge._native_chord", lambda *a, **k: None
        ), patch(
            "app.core.login_bridge._UserActivityMonitor", return_value=mon
        ):
            got = lb.paste_fill(7, 100, "user123", log=lambda m: None)
        self.assertTrue(got["ok"])
        verified.assert_called_once()


class LoginStageProbeTests(unittest.TestCase):
    def test_server_select_wins_over_loaded_credentials_dialog(self) -> None:
        def query(_session, name, *, log=None):
            return SimpleNamespace(
                ok=True,
                shown=name == "Win_LoginServerList",
                dlg_ptr=0x1234 if name == "Win_LoginServerList" else 0x5678,
            )

        with patch("app.core.plg_ui.query_dlg_show", side_effect=query), patch(
            "app.core.plg_ui.get_game_state", return_value=1
        ):
            got = lb.probe_login_stage(SimpleNamespace(pid=7))

        self.assertEqual(got.stage, lb.LoginStage.SERVER_SELECT)
        self.assertEqual(got.dialog_name, "Win_LoginServerList")
        self.assertEqual(got.dialog_ptr, 0x1234)

    def test_unknown_does_not_turn_into_entering_world(self) -> None:
        with patch(
            "app.core.plg_ui.query_dlg_show",
            return_value=SimpleNamespace(ok=True, shown=False, dlg_ptr=0),
        ), patch("app.core.plg_ui.get_game_state", return_value=None):
            got = lb.probe_login_stage(SimpleNamespace(pid=7))

        self.assertEqual(got.stage, lb.LoginStage.UNKNOWN)
        self.assertFalse(got.shown)

    def test_game_state_one_without_visible_dialog_fails_closed(self) -> None:
        with patch(
            "app.core.plg_ui.query_dlg_show",
            return_value=SimpleNamespace(ok=True, shown=False, dlg_ptr=0),
        ), patch(
            "app.core.login_bridge._discover_login_dialogs", return_value=({}, {"scan": "disabled"})
        ), patch("app.core.plg_ui.get_game_state", return_value=1):
            got = lb.probe_login_stage(SimpleNamespace(pid=7))

        self.assertEqual(got.stage, lb.LoginStage.UNKNOWN)
        self.assertIn("game_state=1", got.error or "")

    def test_verified_login_ui_cache_is_a_live_server_select(self) -> None:
        with patch(
            "app.core.plg_ui.query_dlg_show",
            return_value=SimpleNamespace(ok=True, shown=False, dlg_ptr=0),
        ), patch(
            "app.core.login_bridge._discover_login_dialogs",
            return_value=({"Win_LoginServerList": 0x1234}, {"cache_hits": [{"ptr": "0x1234"}]}),
        ):
            got = lb.probe_login_stage(SimpleNamespace(pid=7))

        self.assertEqual(got.stage, lb.LoginStage.SERVER_SELECT)
        self.assertEqual(got.dialog_ptr, 0x1234)
        self.assertEqual(got.method, "login_vtable_scan")


class ConfirmServerSelectionTests(unittest.TestCase):
    def _ok_result(self):
        return SimpleNamespace(ok=True, error=None, note="ok")

    def _server_select_probe(self):
        return lb.LoginStageProbe(
            lb.LoginStage.SERVER_SELECT, "Win_LoginServerList", 0x1234, True
        )

    def test_ui_input_enter_is_preferred(self) -> None:
        probe = self._server_select_probe()
        after = lb.LoginStageProbe(lb.LoginStage.CREDENTIALS, "Win_Login", 0x2222, True)
        with patch(
            "app.core.login_bridge.probe_login_stage", side_effect=[probe, after]
        ), patch("app.core.login_bridge.ui_input", return_value=self._ok_result()) as ui_in, patch(
            "app.core.login_bridge.ui_dialog_command"
        ) as cmd, patch("app.core.login_bridge.ui_key") as key:
            out = lb.confirm_server_selection(SimpleNamespace(pid=7), settle_s=0)

        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "ui_input_enter")
        ui_in.assert_called_once_with(7, 0x1234, lb.WM_KEYDOWN, lb.VK_RETURN, 1, 1)
        cmd.assert_not_called()
        key.assert_not_called()

    def test_fallback_dialog_command_when_ui_input_fails(self) -> None:
        probe = self._server_select_probe()
        with patch(
            "app.core.login_bridge.probe_login_stage", side_effect=[probe]
        ), patch(
            "app.core.login_bridge.ui_input", return_value=SimpleNamespace(ok=False, error="err", note="")
        ), patch(
            "app.core.login_bridge.ui_dialog_command", return_value=self._ok_result()
        ) as cmd, patch("app.core.login_bridge.ui_key") as key:
            out = lb.confirm_server_selection(SimpleNamespace(pid=7), settle_s=0)

        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "fallback_dialog_command")
        cmd.assert_called_once_with(7, 0x1234, lb.LOGIN_DIALOG_COMMAND_CONFIRM)
        key.assert_not_called()

    def test_enter_is_last_fallback(self) -> None:
        probe = self._server_select_probe()
        with patch(
            "app.core.login_bridge.probe_login_stage", side_effect=[probe]
        ), patch(
            "app.core.login_bridge.ui_input", return_value=SimpleNamespace(ok=False, error="err", note="")
        ), patch(
            "app.core.login_bridge.ui_dialog_command", return_value=SimpleNamespace(ok=False, error="err", note="")
        ), patch("app.core.login_bridge.ui_key", return_value=self._ok_result()) as key:
            out = lb.confirm_server_selection(SimpleNamespace(pid=7), settle_s=0)

        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "enter_fallback")
        key.assert_called_once_with(7, lb.VK_RETURN, no_focus=True)


class UiInputSerializationTests(unittest.TestCase):
    def test_to_vk_mapping(self) -> None:
        cases = {
            "a": (0x41, False),
            "A": (0x41, True),
            "5": (0x35, False),
            " ": (0x20, False),
            "@": (0x32, True),
            ".": (0xBE, False),
        }
        for ch, expected in cases.items():
            self.assertEqual(lb._to_vk(ch), expected, ch)

    def test_ascii_char_to_vk_kept_for_backwards_compat(self) -> None:
        # ascii_char_to_vk returns VK only (None for unmapped) for legacy callers.
        self.assertEqual(lb.ascii_char_to_vk("a"), 0x41)
        self.assertEqual(lb.ascii_char_to_vk("Z"), 0x5A)
        self.assertIsNone(lb.ascii_char_to_vk(""))

    def test_type_ascii_orders_shift_and_keys(self) -> None:
        with patch("app.core.login_bridge.key_hold") as hold, patch(
            "app.core.login_bridge.key_hold_press"
        ) as press:
            lb.type_ascii(7, "Ab", per_key_delay=0)
        # 'A': shift-on, press A, shift-off; 'b': press b
        self.assertEqual(
            [c.args for c in hold.call_args_list],
            [(7, 0x10,), (7, 0x10,)],
        )
        self.assertEqual(hold.call_args_list[0].kwargs, {"on": True})
        self.assertEqual(hold.call_args_list[1].kwargs, {"on": False})
        self.assertEqual([c.args for c in press.call_args_list], [(7, 0x41), (7, 0x42)])


class FillCredentialsTests(unittest.TestCase):
    def test_credentials_not_shown_fails_closed(self) -> None:
        with patch(
            "app.core.login_bridge.probe_login_stage",
            return_value=lb.LoginStageProbe(lb.LoginStage.UNKNOWN),
        ):
            out = lb.fill_credentials(SimpleNamespace(pid=7), "u", "p")

        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "credentials_not_shown")

    def test_fill_credentials_types_and_tabs(self) -> None:
        cred = lb.LoginStageProbe(lb.LoginStage.CREDENTIALS, "Win_Login", 0x1234, True)
        after = lb.LoginStageProbe(lb.LoginStage.CREDENTIALS, "Win_Login", 0x1234, True)
        with patch(
            "app.core.login_bridge.probe_login_stage", side_effect=[cred, after]
        ), patch(
            "app.core.aui_click.get_aui_dlg_item_ptr",
            side_effect=lambda s, d, n, log=None: 0x9999 if n == "Input_1" else 0,
        ), patch(
            "app.core.aui_click.read_aui_ctrl_rect",
            return_value=SimpleNamespace(ok=True, x=10, y=20, w=80, h=40, ctrl_ptr=0x9999, center=(50, 40)),
        ) as rect, patch("app.core.login_bridge.ui_click", return_value=SimpleNamespace(ok=True)) as click, patch(
            "app.core.login_bridge.type_ascii", return_value=[]
        ) as typing, patch("app.core.login_bridge.ui_input", return_value=SimpleNamespace(ok=True)) as ui_in:
            out = lb.fill_credentials(SimpleNamespace(pid=7), "user", "pass", settle_s=0)

        self.assertTrue(out["ok"])
        click.assert_called_once()
        self.assertEqual(typing.call_args_list[0].args[1], "user")
        self.assertEqual(typing.call_args_list[1].args[1], "pass")
        # Tab from account to password
        tab_calls = [c for c in ui_in.call_args_list if c.args[3] == lb.VK_TAB]
        self.assertTrue(tab_calls)

    def test_submit_login_leaves_credentials(self) -> None:
        cred = lb.LoginStageProbe(lb.LoginStage.CREDENTIALS, "Win_Login", 0x1234, True)
        after = lb.LoginStageProbe(lb.LoginStage.CHARACTER_SELECT, "Win_CharList", 0x3333, True)
        with patch(
            "app.core.login_bridge.probe_login_stage", side_effect=[cred, after]
        ), patch("app.core.login_bridge.ui_input", return_value=SimpleNamespace(ok=True)) as ui_in:
            out = lb.submit_login(SimpleNamespace(pid=7), settle_s=0)

        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "ui_input_enter")
        ui_in.assert_called_once_with(7, 0x1234, lb.WM_KEYDOWN, lb.VK_RETURN, 1, 1)


class ReadCharSelectRolesTests(unittest.TestCase):
    def test_not_on_char_select_fails_closed(self) -> None:
        with patch(
            "app.core.login_bridge.probe_login_stage",
            return_value=lb.LoginStageProbe(lb.LoginStage.CREDENTIALS, "Win_Login"),
        ):
            out = lb.read_char_select_roles(SimpleNamespace(pid=7))
        self.assertFalse(out["ok"])
        self.assertIn("not_on_char_select", out["error"] or "")

    def test_no_pid_fails(self) -> None:
        out = lb.read_char_select_roles(SimpleNamespace(pid=0))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "no_pid")

    def test_no_charlist_dialog_fails(self) -> None:
        with patch(
            "app.core.login_bridge.probe_login_stage",
            return_value=lb.LoginStageProbe(
                lb.LoginStage.CHARACTER_SELECT, "Win_CharList", 0x3333, True
            ),
        ), patch(
            "app.core.login_bridge._find_win_charlist_dialog", return_value=0
        ):
            out = lb.read_char_select_roles(SimpleNamespace(pid=7))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "win_charlist_not_found")

    def test_reads_role_cards(self) -> None:
        probe = lb.LoginStageProbe(
            lb.LoginStage.CHARACTER_SELECT, "Win_CharList", 0x3333, True
        )
        # control name -> control obj mapping
        ctrls = {
            "Txt_Name1": 0x1000, "Txt_Name2": 0x2000, "Txt_Name3": 0x3000,
            "Txt_Level1": 0x4000, "Txt_Level2": 0x5000, "Txt_Level3": 0x6000,
            "Img_Head1": 0x7000, "Img_Head2": 0x8000, "Img_Head3": 0x9000,
        }

        def fake_field(pid, addr):
            if addr in (0x1000 + lb._CHARLIST_TEXT_OFF,):
                return 0xA100
            if addr == 0x2000 + lb._CHARLIST_TEXT_OFF:
                return 0xA200
            if addr == 0x3000 + lb._CHARLIST_TEXT_OFF:
                return 0xA300
            if addr == 0x4000 + lb._CHARLIST_TEXT_OFF:
                return 0xA400
            if addr == 0x5000 + lb._CHARLIST_TEXT_OFF:
                return 0xA500
            if addr == 0x6000 + lb._CHARLIST_TEXT_OFF:
                return 0xA600
            if addr == 0x7000 + lb._CHARLIST_ROLE_ID_OFF:
                return 1001
            if addr == 0x8000 + lb._CHARLIST_ROLE_ID_OFF:
                return 1002
            if addr == 0x9000 + lb._CHARLIST_ROLE_ID_OFF:
                return 1003
            return 0

        texts = {
            0xA100: "张三", 0xA200: "李四", 0xA300: "王五",
            0xA400: "唐门 140级", 0xA500: "新手 40级", 0xA600: "新手 40级",
        }
        with patch(
            "app.core.login_bridge.probe_login_stage", return_value=probe
        ), patch(
            "app.core.login_bridge._find_win_charlist_dialog", return_value=0x5000
        ), patch(
            "app.core.login_bridge._char_list_controls", return_value=ctrls
        ), patch(
            "app.core.login_bridge._read_u32_field", side_effect=fake_field
        ), patch(
            "app.core.login_bridge._read_remote_wstr_utf16",
            side_effect=lambda pid, ptr, **kw: texts.get(ptr, ""),
        ):
            out = lb.read_char_select_roles(SimpleNamespace(pid=7))

        self.assertTrue(out["ok"])
        self.assertEqual(
            out["roles"],
            [
                {"slot": 1, "name": "张三", "level": "唐门 140级", "role_id": 1001},
                {"slot": 2, "name": "李四", "level": "新手 40级", "role_id": 1002},
                {"slot": 3, "name": "王五", "level": "新手 40级", "role_id": 1003},
            ],
        )


if __name__ == "__main__":
    unittest.main()
