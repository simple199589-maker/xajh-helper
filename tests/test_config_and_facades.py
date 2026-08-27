from __future__ import annotations

import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class ConfigAndFacadeTests(unittest.TestCase):
    def test_captcha_api_key_prefs_round_trip(self) -> None:
        from app.core.captcha_prefs import load_captcha_api_key, save_captcha_api_key

        with tempfile.TemporaryDirectory() as td, patch(
            "common.paths.ensure_writable_dir", return_value=Path(td)
        ):
            path = save_captcha_api_key("  answer-key  ")
            self.assertEqual(path.name, "captcha_prefs.json")
            self.assertEqual(load_captcha_api_key(), "answer-key")
            self.assertIn(
                '"captcha_api_key": "answer-key"',
                path.read_text(encoding="utf-8"),
            )

    def test_session_window_exposes_formal_task_page(self) -> None:
        from app.core.session_store import GameSession, SessionStore
        from app.ui.session_window import SessionFeatureWindow

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        store = SessionStore()
        session = store.mount(GameSession(123, 456, title="test"))
        win = None
        try:
            win = SessionFeatureWindow(root, store, session)
            self.assertIn("task", win._pages)
            self.assertEqual(win._page_task.key, "task")
            self.assertEqual(str(win._page_task.btn_accept["state"]), "normal")
            self.assertEqual(str(win._page_task.btn_complete["state"]), "normal")
            self.assertEqual(
                str(win._page_settings.btn_hang_start["state"]), "normal"
            )
            youfeng = win._page_settings.btn_youfeng_hook
            win.update_idletasks()
            self.assertEqual(str(youfeng["text"]), "开启有凤")
            self.assertLessEqual(
                youfeng.winfo_x() + youfeng.winfo_width(),
                youfeng.master.winfo_width(),
            )
            self.assertFalse(hasattr(win._page_settings, "btn_trace_start"))
            self.assertEqual(win._current, "settings")
        finally:
            if win is not None:
                win.shutdown()
                win.destroy()
            root.destroy()

    def test_admin_session_shows_auto_loot(self) -> None:
        from app.core.session_store import GameSession, SessionStore
        from app.ui.session_window import SessionFeatureWindow

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        store = SessionStore()
        session = store.mount(GameSession(124, 457, title="admin-test"))
        auth = SimpleNamespace(
            auto_loot_allowed=True,
            session=SimpleNamespace(
                key="admin",
                token="",
                local_mock_card=True,
            ),
        )
        win = None
        try:
            win = SessionFeatureWindow(root, store, session, auth=auth)
            self.assertIn("loot", win._pages)
            self.assertIn("loot", win._nav)
            self.assertIsNotNone(win._page_loot)
            self.assertEqual(win._current, "settings")
            self.assertEqual(win.var_title.get(), "快捷设置 · 无控")
        finally:
            if win is not None:
                win.shutdown()
                win.destroy()
            root.destroy()

    def test_captcha_key_sources_and_prod_rejection(self) -> None:
        from app.core import build_profile
        from common import dotenv_load

        with patch.dict(os.environ, {"XAJH_CAPTCHA_API_KEY": "local-test"}):
            build_profile.clear_profile_cache()
            self.assertEqual(build_profile.default_captcha_api_key(), "local-test")
        with patch.dict(os.environ, {}, clear=True), patch.object(
            build_profile, "_read_profile_file", return_value={}
        ), patch.object(dotenv_load, "ensure_dotenv_loaded"):
            build_profile.clear_profile_cache()
            self.assertEqual(build_profile.default_captcha_api_key(), "")
        with patch.dict(os.environ, {}, clear=True), patch.object(
            build_profile,
            "_read_profile_file",
            return_value={
                "channel": "dev",
                "captcha_api_key": "built-dev-key",
            },
        ), patch.object(dotenv_load, "ensure_dotenv_loaded"):
            build_profile.clear_profile_cache()
            self.assertEqual(
                build_profile.default_captcha_api_key(), "built-dev-key"
            )
        with patch.dict(os.environ, {}, clear=True), patch.object(
            build_profile,
            "_read_profile_file",
            return_value={
                "channel": "prod",
                "captcha_api_key": "must-not-load",
            },
        ), patch.object(dotenv_load, "ensure_dotenv_loaded"):
            build_profile.clear_profile_cache()
            self.assertEqual(build_profile.default_captcha_api_key(), "")
        with patch.dict(
            os.environ,
            {"XAJH_CAPTCHA_BASE_URL": "https://api.example.test/"},
            clear=True,
        ), patch.object(
            build_profile, "_read_profile_file", return_value={}
        ), patch.object(dotenv_load, "ensure_dotenv_loaded"):
            build_profile.clear_profile_cache()
            self.assertEqual(
                build_profile.default_captcha_base_url(),
                "https://api.example.test",
            )
        with patch.dict(os.environ, {}, clear=True), patch.object(
            build_profile,
            "_read_profile_file",
            return_value={
                "channel": "dev",
                "captcha_base_url": "https://from.profile",
            },
        ), patch.object(dotenv_load, "ensure_dotenv_loaded"):
            build_profile.clear_profile_cache()
            self.assertEqual(
                build_profile.default_captcha_base_url(),
                "https://from.profile",
            )
        build_profile.clear_profile_cache()

    def test_new_and_legacy_facades_are_identical(self) -> None:
        from app.core.loot import SuperLootConfig
        from app.core.super_loot import SuperLootConfig as LegacyConfig
        from app.ui import features
        from app.ui import pages

        self.assertIs(SuperLootConfig, LegacyConfig)
        for name in (
            "FeaturePage",
            "SuperLootPage",
            "GroceryPage",
            "YaoluPage",
            "ActivityPage",
            "TaskPage",
            "SettingsPage",
        ):
            with self.subTest(page=name):
                self.assertIs(getattr(pages, name), getattr(features, name))


if __name__ == "__main__":
    unittest.main()
