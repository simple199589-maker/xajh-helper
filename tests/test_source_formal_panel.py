# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.session_store import SessionStore
from app.ui.app_shell import ShellApp
from app.ui.main_window import WorkbenchApp


class SourceFormalPanelTests(unittest.TestCase):
    def test_workbench_entry_is_source_shell_only(self) -> None:
        opener = lambda **_kwargs: True
        source = SimpleNamespace(
            _standalone=False,
            master=SimpleNamespace(
                _instance_scope="source",
                open_formal_panel_from_workbench=opener,
            ),
        )
        dev = SimpleNamespace(
            _standalone=False,
            master=SimpleNamespace(
                _instance_scope="dev",
                open_formal_panel_from_workbench=opener,
            ),
        )
        standalone = SimpleNamespace(_standalone=True, master=None)
        self.assertTrue(WorkbenchApp._supports_formal_panel_entry(source))
        self.assertFalse(WorkbenchApp._supports_formal_panel_entry(dev))
        self.assertFalse(WorkbenchApp._supports_formal_panel_entry(standalone))

    def test_source_handoff_mounts_without_injecting(self) -> None:
        created: list[tuple] = []
        shell = SimpleNamespace(
            _instance_scope="source",
            _closing=False,
            auth=SimpleNamespace(is_logged_in=True),
            store=SessionStore(),
            _log=lambda _message: None,
            _show_login_window=lambda: None,
            _reconcile_feature_windows=lambda: None,
            _create_feature_window=lambda *args, **kwargs: (
                created.append((args, kwargs)) or object()
            ),
            _open_or_focus_feature=lambda _pid: None,
        )
        with (
            patch("app.ui.app_shell.is_packaged", return_value=False),
            patch(
                "app.ui.app_shell.find_main_hwnd_for_pid",
                return_value=(0x1234, "笑傲江湖OL - 测试角色", "XAJH"),
            ),
        ):
            ok = ShellApp.open_formal_panel_from_workbench(
                shell,
                pid=2468,
                hwnd=0x1000,
                title="old",
                bridge_note="pong build=2026072902",
                ping_ret=2026072902,
            )
        self.assertTrue(ok)
        self.assertEqual(shell.store.pids(), {2468})
        sess = shell.store.get(2468)
        self.assertEqual(sess.hwnd, 0x1234)
        self.assertEqual(sess.ping_ret, 2026072902)
        self.assertEqual(sess.note, "source_workbench_handoff")
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0][1]["bridge_ready"])
        self.assertFalse(created[0][1]["start_hidden"])

    def test_packaged_or_non_source_handoff_is_rejected(self) -> None:
        shell = SimpleNamespace(
            _instance_scope="dev",
            _closing=False,
            auth=SimpleNamespace(is_logged_in=True),
            store=SessionStore(),
            _log=lambda _message: None,
        )
        with patch("app.ui.app_shell.is_packaged", return_value=False):
            ok = ShellApp.open_formal_panel_from_workbench(shell, pid=1)
        self.assertFalse(ok)
        self.assertEqual(shell.store.count(), 0)

    def test_ui_contains_only_unlocked_entry_point(self) -> None:
        text = Path("app/ui/main_window.py").read_text(encoding="utf-8")
        self.assertEqual(text.count('text="进入正式面板"'), 1)
        self.assertNotIn("_open_formal_after_inject", text)
        toolbar = text.split("def _build_ui", 1)[1].split(
            "# ---------- process info", 1
        )[0]
        self.assertIn('text="进入正式面板"', toolbar)


if __name__ == "__main__":
    unittest.main()
