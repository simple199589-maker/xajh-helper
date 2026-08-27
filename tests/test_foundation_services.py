from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.core.auth_mock import (
    AuthService,
    is_multi_open_enabled,
    last_login_key,
    preferred_login_key,
    save_login_prefs,
    set_multi_open_enabled,
)
from app.core.human_input import clamp, human_delay_s, human_hold_ms, human_inter_click_gap_s, human_slide_path, jitter_around, point_in_rect
from app.core.map_names import format_scene_display, normalize_map_id, strip_map_tag
from app.core.session_store import GameSession, SessionStore
from app.core.window_title import (
    activity_label_for_key,
    has_gui_title_suffix,
    parse_role_name_from_title,
    strip_helper_markers_text,
    title_not_in_role,
)
from common.paths import (
    captcha_debug_dir,
    ensure_writable_dir,
    parse_server_address,
    read_current_server,
)


class FoundationServicesTests(unittest.TestCase):
    def test_server_address_accepts_both_observed_orders(self) -> None:
        self.assertEqual(parse_server_address("6598:193.112.93.158"), ("193.112.93.158", 6598))
        self.assertEqual(parse_server_address("127.0.0.1:16598"), ("127.0.0.1", 16598))
        with self.assertRaises(ValueError):
            parse_server_address("not-an-address")

    def test_read_current_server_gbk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = root / "userdata" / "currentserver.ini"
            cfg.parent.mkdir()
            cfg.write_text("CurrentServer=测试服\nCurrentServerAddress=6598:10.2.3.4\n", encoding="gbk")
            data = read_current_server(root)
            self.assertEqual(data["_ip"], "10.2.3.4")
            self.assertEqual(data["_port"], "6598")

    def test_writable_dir_prefers_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "nested"
            self.assertEqual(ensure_writable_dir(prefer=target), target)
            self.assertTrue(target.is_dir())

    def test_captcha_dir_is_fixed_beside_executable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("common.paths.app_root", return_value=root):
                self.assertEqual(
                    captcha_debug_dir(), root / "captures" / "captcha"
                )

    def test_auth_login_and_logout_via_verify(self) -> None:
        from app.core.license_client import LicenseResult

        auth = AuthService(base_url="https://example.test", secret="sec")
        self.assertEqual(auth.login(" "), (False, "请输入卡密"))
        with patch(
            "app.core.auth_mock.verify_license"
        ) as ver, patch.object(auth, "_machine_code", return_value="mc-1"):
            ver.return_value = LicenseResult(
                ok=True,
                message="ok",
                expires_at="2026-08-11T12:00:00+00:00",
                token="token-1",
            )
            self.assertTrue(auth.login("DEMO-1")[0])
            self.assertEqual(auth.session.display_name, "已授权")
            self.assertEqual(auth.session.token, "token-1")
            self.assertTrue(auth.auto_loot_allowed)
            self.assertIn("卡密到期", auth.session.expire_text())
            auth.logout()
            self.assertFalse(auth.is_logged_in)
            self.assertTrue(auth.login("paid-key")[0])
            self.assertEqual(auth.session.display_name, "已授权")

    def test_auth_rejects_success_response_without_login_token(self) -> None:
        from app.core.license_client import LicenseResult

        auth = AuthService(base_url="https://example.test", secret="sec")
        with patch("app.core.auth_mock.verify_license") as ver, patch.object(
            auth, "_machine_code", return_value="mc-1"
        ):
            ver.return_value = LicenseResult(ok=True, message="ok")
            self.assertEqual(auth.login("DEMO-1"), (False, "登录服务未返回令牌，请重试"))
        self.assertFalse(auth.is_logged_in)

    def test_local_admin_card_works_in_prod_loot_allowed_no_cloud(self) -> None:
        auth = AuthService(base_url="https://example.test", secret="sec")
        with patch("app.core.auth_mock.verify_license") as verify:
            started_at = datetime.now().astimezone()
            ok, msg = auth.login("admin")
            self.assertTrue(ok)
            self.assertIn("卡密到期", msg)
            verify.assert_not_called()
            self.assertFalse(auth.cloud_control_allowed)
            self.assertTrue(auth.auto_loot_allowed)
            self.assertEqual(auth.session.token, "")
            remaining = auth.session.expire_at - started_at
            self.assertGreaterEqual(remaining, timedelta(days=2, hours=23, minutes=59))
            self.assertLessEqual(remaining, timedelta(days=3, minutes=1))
            auth.session.expire_at = datetime.now().astimezone() - timedelta(seconds=1)
            self.assertEqual(auth.reverify(), (False, "本地测试卡已到期，请重新登录"))
            self.assertEqual(auth.unbind("admin"), (False, "本地测试卡无需解绑"))

    def test_game_multi_open_pref_default_off(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            prefs = Path(td) / "login_prefs.json"
            with patch("app.core.auth_mock._prefs_path", return_value=prefs):
                self.assertFalse(is_multi_open_enabled())
                set_multi_open_enabled(True)
                self.assertTrue(is_multi_open_enabled())
                set_multi_open_enabled(False)
                self.assertFalse(is_multi_open_enabled())

    def test_login_key_prefers_prefs_then_generic_card(self) -> None:
        """Card-key prefill: prefs first; fall back to the local generic admin card."""
        with tempfile.TemporaryDirectory() as td:
            prefs = Path(td) / "login_prefs.json"
            with patch("app.core.auth_mock._prefs_path", return_value=prefs):
                self.assertEqual(last_login_key(), "")
                self.assertEqual(preferred_login_key(), "admin")
                save_login_prefs(key="user-card-key")
                self.assertEqual(last_login_key(), "user-card-key")
                self.assertEqual(preferred_login_key(), "user-card-key")

    def test_session_store_order_replace_and_notifications(self) -> None:

        store = SessionStore()
        changes: list[int] = []
        store.on_change(lambda: changes.append(store.count()))
        store.mount(GameSession(1, 11, title="old", mounted_at=1))
        store.mount(GameSession(2, 22, title="new", mounted_at=2))
        self.assertEqual([s.pid for s in store.list()], [2, 1])
        store.mount(GameSession(1, 33, title="replaced", mounted_at=3))
        self.assertEqual(store.get(1).hwnd, 33)
        self.assertEqual(store.unmount(2).pid, 2)
        self.assertEqual(changes, [1, 2, 2, 1])

    def test_map_normalization_and_scene_74(self) -> None:
        self.assertEqual(normalize_map_id(r"maps\d10_1\d10_1.dis"), "d10_1")
        self.assertEqual(strip_map_tag("[门派日常]华山新"), "华山新")
        self.assertEqual(format_scene_display(74), "华山派 (x62)")

    def test_window_title_helpers(self) -> None:
        title = "笑傲江湖OL - 角色甲 服务器 [捡箱子·妖楼]"
        self.assertTrue(has_gui_title_suffix(title))
        self.assertEqual(strip_helper_markers_text(title), "笑傲江湖OL - 角色甲 服务器")
        self.assertEqual(parse_role_name_from_title(title), "角色甲")
        multi = "笑傲江湖OL - 服务器甲 - 服务器甲 - 角色乙 [GUI]"
        self.assertEqual(parse_role_name_from_title(multi), "角色乙")
        self.assertEqual(activity_label_for_key("LOOT"), "捡箱子")
        self.assertIsNone(parse_role_name_from_title("笑傲江湖OL"))

    def test_title_not_in_role_heuristic(self) -> None:
        # Bare client title (login / char-select) => proven not in role.
        self.assertTrue(title_not_in_role("笑傲江湖OL"))
        # Login page appends a status segment instead of a role name.
        self.assertFalse(title_not_in_role("笑傲江湖OL - 笑傲江湖 - 笑傲江湖 - 未登录"))
        # In-role titles carry server + role segments.
        self.assertFalse(title_not_in_role("笑傲江湖OL - 角色甲 服务器 [捡箱子·妖楼]"))
        self.assertFalse(title_not_in_role("笑傲江湖OL - 服务器甲 - 角色乙 [GUI]"))
        # Empty / unreadable title is inconclusive (caller falls back to probe).
        self.assertFalse(title_not_in_role(""))
        self.assertFalse(title_not_in_role("   "))

    def test_human_input_ranges(self) -> None:
        self.assertEqual(clamp(9, 0, 5), 5.0)
        for _ in range(20):
            self.assertGreaterEqual(human_delay_s(0.01, 0.02), 0.01)
            self.assertLessEqual(human_hold_ms(20, 30), 30)
            self.assertGreaterEqual(human_hold_ms(70, 160), 70)
            self.assertLessEqual(human_hold_ms(70, 160), 160)
            x, y = point_in_rect(10, 20, 100, 50)
            self.assertTrue(10 <= x < 110 and 20 <= y < 70)
            jx, jy = jitter_around(5, 6, radius_x=2, radius_y=3)
            self.assertTrue(3 <= jx <= 7 and 3 <= jy <= 9)
            gap = human_inter_click_gap_s(0.28, 0.35)
            self.assertGreaterEqual(gap, 0.28)
            self.assertLessEqual(gap, 0.28 + 0.35 + 1e-6)

    def test_human_slide_path(self) -> None:
        pts, dur = human_slide_path(
            10,
            10,
            110,
            60,
            min_steps=4,
            max_steps=8,
            duration_min_s=0.08,
            duration_max_s=0.20,
        )
        self.assertGreaterEqual(len(pts), 4)
        self.assertEqual(pts[-1], (110, 60))
        self.assertGreaterEqual(dur, 0.08)
        self.assertLessEqual(dur, 0.20 * 1.45 + 0.05)


    def test_license_client_masks_and_local_errors(self) -> None:
        from app.core.license_client import (
            build_license_signature_headers,
            local_network_message,
            mask_card_key,
            mask_machine_code,
            verify_license,
            unbind_license,
        )

        self.assertEqual(mask_card_key("ABCD-EFGH-IJKL-MNOP")[:4], "ABCD")
        self.assertIn("…", mask_card_key("ABCD-EFGH-IJKL-MNOP"))
        self.assertIn("…", mask_machine_code("a" * 40))
        self.assertIn("超时", local_network_message("timeout after 10s"))

        raw = b'{"card_key":"A","machine_code":"M"}'
        headers = build_license_signature_headers(
            "POST",
            "/api/license/verify",
            raw,
            "test-secret",
            timestamp="1700000000",
            nonce="abc123",
        )
        self.assertEqual(headers["X-Timestamp"], "1700000000")
        self.assertEqual(headers["X-Nonce"], "abc123")
        self.assertEqual(len(headers["X-Signature"]), 64)

        with patch(
            "app.core.license_client._http_json",
            return_value=(401, {"code": 401, "message": "卡密不存在"}, "卡密不存在"),
        ) as http:
            r = verify_license("BAD-KEY", "machine-1", secret="sec")
            self.assertFalse(r.ok)
            self.assertEqual(r.message, "卡密不存在")
            # signed headers + X-API-Key = card_key (login domain)
            call_kw = http.call_args.kwargs
            hdrs = call_kw.get("headers") or {}
            self.assertIn("X-Signature", hdrs)
            self.assertIn("X-Timestamp", hdrs)
            self.assertIn("X-Nonce", hdrs)
            self.assertEqual(hdrs.get("X-API-Key"), "BAD-KEY")
        with patch(
            "app.core.license_client._http_json",
            return_value=(
                200,
                {
                    "code": 0,
                    "data": {
                        "ok": True,
                        "status": "active",
                        "expires_at": "2026-08-11T12:00:00+00:00",
                        "machine_bound": True,
                        "machine_code": "machine-1",
                    },
                },
                None,
            ),
        ):
            r = verify_license("GOOD-KEY", "machine-1", secret="sec")
            self.assertTrue(r.ok)
            self.assertEqual(r.status, "active")
        with patch(
            "app.core.license_client._http_json",
            return_value=(
                429,
                {"code": 429, "message": "该卡密今日已解绑过，请明天再试"},
                "该卡密今日已解绑过，请明天再试",
            ),
        ) as http:
            r = unbind_license("GOOD-KEY", "machine-1", secret="sec")
            self.assertFalse(r.ok)
            self.assertIn("今日已解绑", r.message)
            hdrs = (http.call_args.kwargs.get("headers") or {})
            self.assertEqual(hdrs.get("X-API-Key"), "GOOD-KEY")
            self.assertIn("X-Signature", hdrs)
        # License HMAC is optional; business endpoints use a separate token HMAC.
        with patch("app.core.license_client.DEFAULT_LICENSE_API_SECRET", ""), patch(
            "app.core.license_client._http_json",
            return_value=(200, {"code": 0, "data": {"ok": True}}, None),
        ) as http:
            r = verify_license("GOOD-KEY", "machine-1", secret="")
            self.assertTrue(r.ok)
            hdrs = http.call_args.kwargs.get("headers") or {}
            self.assertEqual(hdrs.get("X-API-Key"), "GOOD-KEY")
            self.assertNotIn("X-Signature", hdrs)
        # missing body fields fail locally
        r = verify_license("", "machine-1", secret="sec")
        self.assertFalse(r.ok)
        self.assertIn("卡密", r.message)
        r = verify_license("GOOD-KEY", "", secret="sec")
        self.assertFalse(r.ok)
        self.assertIn("本机", r.message)
        r = unbind_license("", "machine-1", secret="sec")
        self.assertFalse(r.ok)
        self.assertIn("卡密", r.message)

    def test_auth_service_uses_verify(self) -> None:
        from app.core.license_client import LicenseResult

        with patch("app.core.auth_mock.verify_license") as ver:
            ver.return_value = LicenseResult(
                ok=True,
                message="ok",
                expires_at="2026-08-11T12:00:00+00:00",
                token="token-1",
            )
            auth = AuthService(
                base_url="https://example.test",
                secret="sec",
            )
            with patch.object(auth, "_machine_code", return_value="mc-1"):
                ok, msg = auth.login("CARD-1")
            self.assertTrue(ok)
            self.assertTrue(auth.is_logged_in)
            self.assertIn("卡密到期", msg)
            ver.assert_called()
            # positional card_key is the login business key (X-API-Key domain)
            self.assertEqual(ver.call_args.args[0], "CARD-1")
            kwargs = ver.call_args.kwargs
            self.assertEqual(kwargs.get("secret"), "sec")

    def test_license_secret_not_from_json_profile(self) -> None:
        """HMAC secret must not be accepted from plaintext build_profile.json."""
        from app.core import build_profile

        with patch.dict(os.environ, {}, clear=True), patch.object(
            build_profile, "_license_api_secret_from_env", return_value=""
        ), patch.object(
            build_profile, "_license_api_secret_from_pack", return_value=""
        ), patch.object(
            build_profile,
            "_read_profile_file",
            return_value={
                "channel": "prod",
                "license_api_secret": "should-not-load-from-json",
            },
        ):
            build_profile.clear_profile_cache()
            self.assertEqual(build_profile.default_license_api_secret(), "")
        with patch.dict(os.environ, {}, clear=True), patch.object(
            build_profile, "_license_api_secret_from_env", return_value=""
        ), patch.object(
            build_profile,
            "_license_api_secret_from_pack",
            return_value="from-pack",
        ):
            build_profile.clear_profile_cache()
            self.assertEqual(build_profile.default_license_api_secret(), "from-pack")
        build_profile.clear_profile_cache()

    def test_license_secret_from_env(self) -> None:
        from app.core import build_profile

        with patch.dict(
            os.environ,
            {
                "XAJH_BUILD_CHANNEL": "prod",
                "XAJH_LICENSE_API_SECRET": "from-env-secret",
            },
            clear=False,
        ):
            build_profile.clear_profile_cache()
            self.assertEqual(
                build_profile.default_license_api_secret(), "from-env-secret"
            )
        build_profile.clear_profile_cache()


if __name__ == "__main__":
    unittest.main()
