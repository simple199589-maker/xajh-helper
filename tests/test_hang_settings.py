# -*- coding: utf-8 -*-
"""Unit tests for hang_settings prefs / config merge (no live game)."""
from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.hang_settings import (
    HangConfig,
    get_hang_config,
    load_hang_prefs,
    load_hang_prefs_store,
    normalize_hang_char_id,
    save_hang_disk_from_config,
    save_hang_prefs,
    write_hang_config_to_settings,
)
import app.core.hang_settings as hang_settings


class RoleHangPersistenceTests(unittest.TestCase):
    def test_save_hang_config_writes_current_role_directory(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from app.core.account_manager import load_role_hang
        from app.core.hang_settings import HangConfig, save_hang_disk_from_config

        with TemporaryDirectory() as td:
            root = Path(td)
            with patch("app.core.account_manager.config_root", return_value=root), patch(
                "app.core.hang_settings._hang_prefs_path",
                return_value=root / "hang_prefs.json",
            ):
                saved = save_hang_disk_from_config(
                    HangConfig(radius=11, jianglong_hang=True, auto_open_monster=True, open_monster_rows=2),
                    char_id="300001",
                    char_name="角色甲",
                )

                self.assertEqual(saved["radius"], 11)
                self.assertTrue(saved["auto_open_monster"])
                self.assertEqual(saved["open_monster_rows"], 2)
                self.assertTrue((root / "roles" / "300001" / "hang.json").is_file())
                self.assertEqual(load_role_hang("300001")["radius"], 11)
                self.assertTrue(load_role_hang("300001")["auto_open_monster"])
                self.assertEqual(load_role_hang("300001")["open_monster_rows"], 2)
                self.assertEqual(load_hang_prefs("300001")["radius"], 11)
                self.assertEqual(get_hang_config(char_id="300001").radius, 11)
                self.assertEqual(get_hang_config(char_id="300001").open_monster_rows, 2)

class HangSettingsConfigTests(unittest.TestCase):
    def test_control_packet_helpers_send_fixed_switch_packets(self) -> None:
        session = object()
        with patch.object(
            hang_settings,
            "send_raw_c2s_packet",
            return_value=1,
        ) as send_packet:
            started = hang_settings.send_hang_start_packet(session)
            stopped = hang_settings.send_hang_stop_packet(session)

        self.assertTrue(started["ok"])
        self.assertEqual(started["packet"], "1500")
        self.assertTrue(stopped["ok"])
        self.assertEqual(stopped["packet"], "160002")
        self.assertEqual(
            [call.args[1] for call in send_packet.call_args_list],
            [bytes.fromhex("1500"), bytes.fromhex("160002")],
        )

    def test_defaults_and_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "hang_prefs.json"
            roles = Path(td) / "roles"
            with patch("app.core.hang_settings._hang_prefs_path", return_value=path), patch(
                "app.core.account_manager.roles_root", return_value=roles
            ), patch(
                "app.core.account_manager.global_dir", return_value=Path(td) / "global"
            ):
                cfg = get_hang_config()
                self.assertEqual(cfg.mode, 1)
                self.assertEqual(cfg.radius, 5)
                self.assertTrue(cfg.enable_pickup)
                self.assertFalse(cfg.empty_skill)
                self.assertFalse(cfg.youfeng_hang)

                save_hang_prefs(
                    radius=8,
                    repair_below_pct=33,
                    auto_vitality=False,
                    youfeng_hang=True,
                )
                cfg2 = get_hang_config()
                self.assertEqual(cfg2.radius, 8)
                self.assertEqual(cfg2.repair_below_pct, 33.0)
                self.assertFalse(cfg2.auto_vitality)
                self.assertTrue(cfg2.youfeng_hang)

                cfg3 = get_hang_config(
                    {"hang_radius": 12, "hang_mode": 0, "hang_empty_skill": True}
                )
                self.assertEqual(cfg3.radius, 12)
                self.assertEqual(cfg3.mode, 0)
                self.assertTrue(cfg3.empty_skill)

                cfg4 = get_hang_config(
                    {"hang_radius": 12},
                    overrides={"radius": 3, "enable_pickup": False},
                )
                self.assertEqual(cfg4.radius, 3)
                self.assertFalse(cfg4.enable_pickup)
                self.assertEqual(load_hang_prefs().get("radius"), 8)

                cfg5 = get_hang_config(
                    overrides={"wanzi_hang": True, "youfeng_hang": True}
                )
                self.assertFalse(cfg5.wanzi_hang)
                self.assertTrue(cfg5.youfeng_hang)

    def test_char_id_prefs_and_name_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "hang_prefs.json"
            roles = Path(td) / "roles"
            with patch("app.core.hang_settings._hang_prefs_path", return_value=path), patch(
                "app.core.account_manager.roles_root", return_value=roles
            ), patch(
                "app.core.account_manager.global_dir", return_value=Path(td) / "global"
            ):
                self.assertEqual(normalize_hang_char_id("角色甲"), "")
                self.assertEqual(normalize_hang_char_id(100001), "100001")
                self.assertEqual(normalize_hang_char_id("100001"), "100001")
                self.assertEqual(normalize_hang_char_id(0), "")

                # legacy flat -> default
                path.write_text(
                    json.dumps(
                        {
                            "radius": 7,
                            "auto_repair": False,
                            "repair_below_pct": 40,
                            "auto_vitality": True,
                            "vitality_below_pct": 15,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                d = load_hang_prefs()
                self.assertEqual(d["radius"], 7)
                self.assertFalse(d["auto_repair"])

                # save by character id
                save_hang_prefs(
                    char_id=100001,
                    char_name="角色甲",
                    mode=0,
                    radius=9,
                    enable_pickup=False,
                    empty_skill=True,
                    auto_repair=True,
                    repair_below_pct=55,
                    auto_vitality=False,
                    vitality_below_pct=25,
                )
                r1 = load_hang_prefs(100001)
                self.assertEqual(r1["mode"], 0)
                self.assertEqual(r1["radius"], 9)
                self.assertFalse(r1["enable_pickup"])
                self.assertTrue(r1["empty_skill"])
                self.assertFalse(r1["auto_vitality"])
                self.assertEqual(r1.get("name"), "角色甲")
                store = load_hang_prefs_store()
                self.assertEqual(store["by_id"]["100001"].get("name"), "角色甲")
                # name is first key for human scan
                self.assertEqual(next(iter(store["by_id"]["100001"].keys())), "name")

                # other id inherits default
                r2 = load_hang_prefs(100002)
                self.assertEqual(r2["radius"], 7)
                self.assertFalse(r2["auto_repair"])

                # chinese name must not become a key / must not clobber default
                before = load_hang_prefs().get("radius")
                save_hang_prefs(role="角色甲", radius=99)
                store = load_hang_prefs_store()
                self.assertNotIn("角色甲", store.get("by_id") or {})
                self.assertEqual(load_hang_prefs().get("radius"), before)

                # overrides temporary
                cfg = get_hang_config(
                    char_id=100001, overrides={"radius": 2, "empty_skill": False}
                )
                self.assertEqual(cfg.radius, 2)
                self.assertFalse(cfg.empty_skill)
                self.assertEqual(load_hang_prefs(100001)["radius"], 9)

                store = load_hang_prefs_store()
                self.assertEqual(store["version"], 7)
                self.assertIn("100001", store["by_id"])

                cfg2 = HangConfig(
                    mode=1,
                    radius=6,
                    enable_pickup=True,
                    empty_skill=False,
                    auto_repair=False,
                    repair_below_pct=44,
                    auto_vitality=True,
                    vitality_below_pct=18,
                )
                save_hang_disk_from_config(cfg2, char_id=100002)
                r3 = load_hang_prefs(100002)
                self.assertEqual(r3["radius"], 6)
                self.assertFalse(r3["auto_repair"])

                settings: dict = {}
                write_hang_config_to_settings(settings, cfg2)
                self.assertEqual(settings["hang_radius"], 6)

    def test_party_auto_need_preserves_other_pick_flags(self) -> None:
        from app.core import hang_settings as hs

        session = object()
        mem = {"autoplay": 0x123400}
        # 0x85 = bits 0x80|0x04|0x01 (does not include the team auto-need 0x20 bit)
        with patch.object(hs, "resolve_cec_autoplay_rpm", return_value=mem), patch.object(
            hs, "_rpm_u8", side_effect=[0x85, 0xA5]
        ), patch.object(hs, "_wpm_u8", return_value=True) as write:
            out = hs._apply_party_auto_need_unlocked(session, True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["before"], 0x85)
        self.assertEqual(out["after"], 0xA5)
        write.assert_called_once_with(
            session,
            0x123400 + hs.AUTOPLAY_PICK_FLAGS_OFF,
            0xA5,
        )

        with patch.object(hs, "resolve_cec_autoplay_rpm", return_value=mem), patch.object(
            hs, "_rpm_u8", side_effect=[0xB5, 0x95]
        ), patch.object(hs, "_wpm_u8", return_value=True):
            out = hs._apply_party_auto_need_unlocked(session, False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["after"], 0x95)

    def test_apply_hang_prepare_writes_selected_mode(self) -> None:
        from app.core import hang_settings as hs

        cfg = HangConfig(
            mode=0,
            radius=7,
            enable_pickup=False,
            empty_skill=False,
            wanzi_hang=False,
        )
        with patch.object(hs, "set_autoplay_mode", return_value={"ok": True}), patch.object(
            hs, "set_autoplay_radius", return_value={"ok": True}
        ), patch.object(
            hs, "apply_party_auto_need", return_value={"ok": True}
        ) as pickup:
            out = hs.apply_hang_prepare(object(), cfg)
        self.assertTrue(out["ok"])
        self.assertIn("模式=普通模式", out["message"])
        pickup.assert_called_once_with(unittest.mock.ANY, False, log=unittest.mock.ANY)

    def test_apply_hang_prepare_refuses_writes_during_scene_transition(self) -> None:
        from app.core import hang_settings as hs

        class Session:
            pid = 4242

        with patch(
            "app.core.remote_runtime.wait_pid_scene_stable",
            side_effect=RuntimeError("scene transition"),
        ), patch.object(hs, "set_autoplay_mode") as set_mode:
            out = hs.apply_hang_prepare(Session(), HangConfig())
        self.assertFalse(out["ok"])
        self.assertIn("scene gate", out["message"])
        set_mode.assert_not_called()

    def test_youfeng_takes_priority_over_empty_skill_clear(self) -> None:
        from app.core import hang_settings as hs

        cfg = HangConfig(empty_skill=True, youfeng_hang=True)
        with patch.object(hs, "set_autoplay_mode", return_value={"ok": True}), patch.object(
            hs, "set_autoplay_radius", return_value={"ok": True}
        ), patch.object(hs, "apply_party_auto_need", return_value={"ok": True}), patch.object(
            hs, "prepare_youfeng_hang", return_value={"ok": True, "message": "有凤已准备"}
        ), patch.object(hs, "clear_autoplay_skills") as clear:
            out = hs.apply_hang_prepare(object(), cfg)
        self.assertTrue(out["ok"])
        clear.assert_not_called()
        self.assertIn("忽略无技能挂机", out["message"])


class HangMaintainIntervalTests(unittest.TestCase):
    def test_check_due_and_backoff(self) -> None:
        from app.core import hang_settings as hs

        class _S:
            pid = 424242

        sess = _S()
        hs._HANG_PID_LAST.pop(424242, None)
        due, remain = hs._hang_check_due(
            sess, "repair_check", interval_s=1800.0, force=False
        )
        self.assertTrue(due)
        hs._mark_hang_cooldown(sess, "repair_check")
        due2, remain2 = hs._hang_check_due(
            sess, "repair_check", interval_s=1800.0, force=False
        )
        self.assertFalse(due2)
        self.assertGreater(remain2, 1790.0)

    def test_maintain_skips_rv_when_interval_not_due(self) -> None:
        from app.core import hang_settings as hs
        from app.core.hang_settings import HangConfig

        class _S:
            pid = 424243
            hwnd = 0

        sess = _S()
        hs._HANG_PID_LAST.pop(424243, None)
        hs._HANG_PID_OWNER.pop(424243, None)
        hs._mark_hang_cooldown(sess, "repair_check")
        hs._mark_hang_cooldown(sess, "vitality_check")
        cfg = HangConfig(auto_repair=True, auto_vitality=True, enable_pickup=True)
        with patch.object(hs, "read_equipment_durability_pct") as rd, patch.object(
            hs, "read_vitality_pct"
        ) as rv, patch.object(hs, "abandon_all_loot_rolls") as ab, patch.object(
            hs, "_probe_remote_callable", return_value=(True, "")
        ), patch.object(hs, "_hang_pid", return_value=424243):
            out = hs._hang_maintain_once_unlocked(
                sess, cfg, force_rv=False, log=lambda m: None
            )
            rd.assert_not_called()
            rv.assert_not_called()
            ab.assert_not_called()
            self.assertEqual(out["repair"]["reason"], "check_interval")

    def test_wanzi_prepare_uses_direct_packets_without_recover_slot(self) -> None:
        from app.core import hang_settings as hs
        from app.core.wanzi_packet import (
            P1_NEIGONG,
            P1_PACKAGE_OFF,
            P1_SLOT_OFF,
            P1_WAIGONG,
            WanziPacketRunner,
            build_wanzi_p1,
        )

        cfg = HangConfig(wanzi_hang=True, wanzi_interval_ms=40)
        out = hs.prepare_wanzi_hang(object(), cfg)
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "direct_packet")
        self.assertFalse(out["slot_write"])
        self.assertEqual(out["interval_ms"], 40)
        self.assertEqual(out["low_rate_seconds"], 6.0)
        self.assertEqual(out["low_rate_pairs_per_second"], 3.0)
        self.assertEqual(out["active_window_s"], 300.0)
        tuned = hs.prepare_wanzi_hang(
            object(),
            HangConfig(
                wanzi_hang=True,
                wanzi_low_rate_seconds=9,
                wanzi_low_rate_pairs_per_second=1.5,
                wanzi_active_window_s=420,
            ),
        )
        self.assertEqual(tuned["low_rate_seconds"], 9.0)
        self.assertEqual(tuned["low_rate_pairs_per_second"], 1.5)
        self.assertEqual(tuned["active_window_s"], 420.0)
        self.assertEqual(
            P1_NEIGONG.hex().upper(),
            "1F0000000000005E0A01036300004200000000000000000000AC9303C262FD72428A7481C201",
        )
        self.assertEqual(WanziPacketRunner(1, 40, kind="neigong").p1, P1_NEIGONG)
        self.assertEqual(WanziPacketRunner(1, 40, kind="waigong").p1, P1_WAIGONG)
        custom_p1 = build_wanzi_p1("waigong", 3, 17)
        self.assertEqual(custom_p1[P1_PACKAGE_OFF], 3)
        self.assertEqual(custom_p1[P1_SLOT_OFF], 17)
        self.assertEqual(build_wanzi_p1("waigong", 3, 1), P1_WAIGONG)
        self.assertEqual(
            WanziPacketRunner(1, 40, kind="waigong", p1_payload=custom_p1).p1,
            custom_p1,
        )

        inner = hs.get_hang_config(
            overrides={"wanzi_neigong_hang": True, "wanzi_waigong_hang": False}
        )
        outer = hs.get_hang_config(overrides={"wanzi_hang": True})
        self.assertEqual(hs.wanzi_kind_from_config(inner), hs.WANZI_KIND_NEIGONG)
        self.assertEqual(hs.wanzi_kind_from_config(outer), hs.WANZI_KIND_WAIGONG)

    def test_wanzi_interval_floor_dev_free_prod_140(self) -> None:
        from app.core import hang_settings as hs

        # Source/dev builds keep the 1ms floor so the interval can be squeezed.
        self.assertEqual(hs._clamp_interval_ms(40), 40)
        self.assertEqual(hs._clamp_interval_ms(1), 1)
        # Production packages enforce the measured-safe 140ms floor.
        with patch.object(hs, "is_dev_build", return_value=False):
            self.assertEqual(hs._clamp_interval_ms(40), 140)
            self.assertEqual(hs._clamp_interval_ms(139), 140)
            self.assertEqual(hs._clamp_interval_ms(140), 140)
            self.assertEqual(hs._clamp_interval_ms(150), 150)

    def test_wanzi_preflight_accepts_selected_pill_anywhere_in_first_extension_bag(self) -> None:
        from types import SimpleNamespace

        from app.core import hang_settings as hs
        from app.core.package_api import PackageItem

        session = SimpleNamespace(pid=515186, module_base=0x400000)
        items = [
            PackageItem(
                hs.WANZI_FIRST_BAG_PACKAGE,
                17,
                0x12345678,
                tid=hs.WANZI_ITEM_TID_WAIGONG,
                count=1,
                name="2200W外功伤害物品",
            )
        ]
        class _Dispatch:
            def read_cached(self, _pid, _key, producer, **kwargs):
                self.kwargs = kwargs
                return producer()

        dispatch = _Dispatch()
        with patch("app.core.safe_dispatch.get_dispatch", return_value=dispatch), patch.object(
            hs, "list_package_items_rpm", return_value=items
        ) as scan:
            out = hs._wanzi_inventory_preflight(session, hs.WANZI_KIND_WAIGONG)
        self.assertTrue(out["ok"])
        self.assertEqual(out["damage"][0]["slot"], 17)
        self.assertEqual(out["damage"][0]["package"], 2)
        self.assertEqual(hs.WANZI_P1_PACKAGE, 3)
        scan.assert_called_once_with(
            session, hs.WANZI_FIRST_BAG_PACKAGE, log=unittest.mock.ANY
        )
        self.assertEqual(dispatch.kwargs["op"], "wanzi_startup_bag")
        self.assertEqual(dispatch.kwargs["max_age"], 0)

    def test_wanzi_preflight_rejects_missing_selected_pill_from_first_extension_bag(self) -> None:
        from types import SimpleNamespace

        from app.core import hang_settings as hs
        from app.core.package_api import PackageItem

        session = SimpleNamespace(pid=515185, module_base=0x400000)
        # A pill in a different bag must not satisfy the first-bag rule.
        items = [
            PackageItem(3, 1, 0x12345678, tid=hs.WANZI_ITEM_TID_WAIGONG, count=1)
        ]
        class _Dispatch:
            def read_cached(self, _pid, _key, producer, **_kwargs):
                return producer()

        with patch("app.core.safe_dispatch.get_dispatch", return_value=_Dispatch()), patch.object(
            hs, "list_package_items_rpm", return_value=items
        ):
            out = hs._wanzi_inventory_preflight(session, hs.WANZI_KIND_WAIGONG)
        self.assertFalse(out["ok"])
        self.assertTrue(out["checked"])
        self.assertEqual(out["message"], hs.WANZI_FIRST_BAG_MISSING_MESSAGE)

    def test_wanzi_preflight_reports_dispatch_allocation_failure(self) -> None:
        from types import SimpleNamespace

        from app.core import hang_settings as hs

        session = SimpleNamespace(pid=515182, module_base=0x400000)
        logs: list[str] = []

        class _Dispatch:
            calls = 0

            def read_cached(self, *_args, **_kwargs):
                self.calls += 1
                raise TimeoutError(
                    "SafeDispatch submit timeout pid=515182 op=wanzi_startup_bag"
                )

        dispatch = _Dispatch()
        with patch(
            "app.core.safe_dispatch.get_dispatch", return_value=dispatch
        ):
            out = hs._wanzi_inventory_preflight(
                session, hs.WANZI_KIND_WAIGONG, log=logs.append
            )
        self.assertFalse(out["ok"])
        self.assertFalse(out["checked"])
        self.assertEqual(out["attempts"], 2)
        self.assertEqual(dispatch.calls, 2)
        self.assertIn("SafeDispatch submit timeout", out["error"])
        self.assertIn("调度中心背包内存读取失败", out["message"])
        self.assertEqual(len(logs), 2)
        self.assertIn("立即重试一次", logs[0])
        self.assertEqual(logs[1], out["message"])

    def test_wanzi_preflight_retries_dispatch_failure_once_then_uses_result(self) -> None:
        from types import SimpleNamespace

        from app.core import hang_settings as hs
        from app.core.package_api import PackageItem

        session = SimpleNamespace(pid=515181, module_base=0x400000)
        logs: list[str] = []
        items = [
            PackageItem(
                2, 9, 0x12345678, tid=hs.WANZI_ITEM_TID_WAIGONG, count=1
            )
        ]

        class _Dispatch:
            calls = 0

            def read_cached(self, _pid, _key, producer, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("worker unavailable")
                return producer()

        dispatch = _Dispatch()
        with patch(
            "app.core.safe_dispatch.get_dispatch", return_value=dispatch
        ), patch.object(hs, "list_package_items_rpm", return_value=items) as scan:
            out = hs._wanzi_inventory_preflight(
                session, hs.WANZI_KIND_WAIGONG, log=logs.append
            )
        self.assertTrue(out["ok"])
        self.assertEqual(dispatch.calls, 2)
        scan.assert_called_once()
        self.assertTrue(any("立即重试一次" in line for line in logs))

    def test_wanzi_preflight_reads_pid_only_session_by_rpm_in_dispatch_task(self) -> None:
        from types import SimpleNamespace

        from app.core import hang_settings as hs
        from app.core.package_api import PackageItem

        pid = 515180
        session = SimpleNamespace(pid=pid)
        items = [
            PackageItem(
                2, 11, 0x12345678, tid=hs.WANZI_ITEM_TID_WAIGONG, count=1
            )
        ]

        class _Dispatch:
            def read_cached(self, _pid, _key, producer, **_kwargs):
                return producer()

        logs: list[str] = []
        with patch(
            "app.core.safe_dispatch.get_dispatch", return_value=_Dispatch()
        ), patch.object(
            hs, "list_package_items_rpm", return_value=items
        ) as scan:
            out = hs._wanzi_inventory_preflight(
                session, hs.WANZI_KIND_WAIGONG, log=logs.append
            )
        self.assertTrue(out["ok"])
        scan.assert_called_once_with(
            session, hs.WANZI_FIRST_BAG_PACKAGE, log=unittest.mock.ANY
        )
        self.assertFalse(any("临时附加" in line for line in logs))

    def test_wanzi_manual_does_not_start_sender_when_first_extension_has_no_pill(self) -> None:
        from types import SimpleNamespace

        from app.core import hang_settings as hs

        pid = 515184
        session = SimpleNamespace(pid=pid)
        hs._HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
        hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)
        try:
            with patch.object(
                hs, "probe_hang_state_mem", return_value=SimpleNamespace(ok=True, on=False)
            ), patch.object(
                hs, "_wanzi_inventory_preflight",
                return_value={"ok": False, "checked": True, "message": "丸子未找到"},
            ), patch.object(hs, "WanziPacketRunner") as runner_cls:
                out = hs.start_wanzi_packet_manual(session, 140)
            self.assertFalse(out["ok"])
            self.assertFalse(out["running"])
            self.assertNotIn(pid, hs._HANG_WANZI_PACKET_OWNERS)
            runner_cls.assert_not_called()
        finally:
            hs._HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
            hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)

    def test_wanzi_manual_does_not_start_sender_when_dispatch_allocation_fails(self) -> None:
        from types import SimpleNamespace

        from app.core import hang_settings as hs

        pid = 515187
        session = SimpleNamespace(pid=pid)
        failed_preflight = {
            "ok": False,
            "checked": False,
            "error": "SafeDispatch submit timeout pid=515187 op=wanzi_startup_bag",
            "message": "丸子背包预检失败 pid=515187: SafeDispatch submit timeout",
        }
        hs._HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
        hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)
        try:
            with patch.object(
                hs, "probe_hang_state_mem", return_value=SimpleNamespace(ok=True, on=False)
            ), patch.object(
                hs, "_wanzi_inventory_preflight", return_value=failed_preflight
            ), patch.object(hs, "WanziPacketRunner") as runner_cls:
                out = hs.start_wanzi_packet_manual(session, 140)
            self.assertFalse(out["ok"])
            self.assertFalse(out["running"])
            self.assertEqual(out["inventory_preflight"], failed_preflight)
            self.assertIn("SafeDispatch submit timeout", out["message"])
            self.assertNotIn(pid, hs._HANG_WANZI_PACKET_OWNERS)
            runner_cls.assert_not_called()
        finally:
            hs._HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
            hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)

    def test_wanzi_manual_freezes_startup_found_coordinate_in_p1(self) -> None:
        from types import SimpleNamespace

        from app.core import hang_settings as hs
        from app.core.wanzi_packet import P1_PACKAGE_OFF, P1_SLOT_OFF

        pid = 515183
        session = SimpleNamespace(pid=pid)
        preflight = {
            "ok": True,
            "checked": True,
            "kind": hs.WANZI_KIND_WAIGONG,
            "damage": [{"package": 2, "slot": 17, "count": 1}],
        }
        runner = MagicMock()
        runner.is_running.return_value = True
        runner.stop.return_value = True
        hs._HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
        hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)
        try:
            with patch.object(
                hs, "probe_hang_state_mem", return_value=SimpleNamespace(ok=True, on=False)
            ), patch.object(
                hs, "_wanzi_inventory_preflight", return_value=preflight
            ) as scan, patch.object(hs, "WanziPacketRunner", return_value=runner) as runner_cls:
                out = hs.start_wanzi_packet_manual(session, 140)
            self.assertTrue(out["ok"])
            payload = runner_cls.call_args.kwargs["p1_payload"]
            self.assertEqual(payload[P1_PACKAGE_OFF], 3)
            self.assertEqual(payload[P1_SLOT_OFF], 17)
            scan.assert_called_once()
        finally:
            hs._HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
            hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)

    def test_wanzi_manual_and_hang_share_one_runner_by_owner(self) -> None:
        from app.core import hang_settings as hs

        pid = 515188
        runner = MagicMock()
        runner.is_running.return_value = True
        runner.stop.return_value = True
        hs._HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
        hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)
        hs._HANG_WANZI_PACKET_HANG_PAUSED.discard(pid)
        try:
            reserved = hs._reserve_wanzi_packet_hang(pid)
            self.assertTrue(reserved["ok"])
            with patch.object(
                hs,
                "probe_hang_state_mem",
                return_value=MagicMock(ok=True, on=False),
            ), patch.object(hs, "_wanzi_inventory_preflight", return_value={"ok": True, "checked": True, "kind": hs.WANZI_KIND_WAIGONG, "damage": [{"package": 2, "slot": 1}]}), patch.object(hs, "WanziPacketRunner", return_value=runner):
                manual = hs.start_wanzi_packet_manual(pid, 40)
            self.assertTrue(manual["ok"])
            self.assertEqual(set(manual["owners"]), {"hang", "manual"})
            runner.start.assert_called_once()

            # A scene-style hang pause only closes the hang lease.  Manual
            # remains enabled and keeps the shared runner sending directly.
            with patch.object(hs, "_cancel_wanzi_aoi_gate"):
                paused = hs.stop_wanzi_packet_hang(pid, release=False)
            self.assertTrue(paused["ok"])
            self.assertFalse(paused["enabled"])
            self.assertTrue(hs.get_wanzi_packet_state(pid, owner="manual")["enabled"])
            self.assertFalse(hs.get_wanzi_packet_state(pid, owner="hang")["enabled"])
            self.assertTrue(hs._wanzi_shared_can_send(pid))
            runner.stop.assert_not_called()
            hs._reserve_wanzi_packet_hang(pid)

            # Releasing manual must preserve both the runner and hang lease.
            manual_stop = hs.stop_wanzi_packet_manual(pid)
            self.assertTrue(manual_stop["ok"])
            self.assertEqual(manual_stop["owners"], ["hang"])
            runner.stop.assert_not_called()
            self.assertIs(hs._HANG_WANZI_PACKET_RUNNERS[pid], runner)

            # Add manual again, then release hang.  The same serialized runner
            # remains alive for manual and only the hang AOI lease disappears.
            with patch.object(
                hs,
                "probe_hang_state_mem",
                return_value=MagicMock(ok=True, on=False),
            ):
                manual_again = hs.start_wanzi_packet_manual(pid, 40)
            self.assertTrue(manual_again["reused"])
            self.assertEqual(set(manual_again["owners"]), {"hang", "manual"})
            hang_stop = hs.stop_wanzi_packet_hang(pid, release=True)
            self.assertTrue(hang_stop["ok"])
            self.assertEqual(hang_stop["owners"], ["manual"])
            runner.stop.assert_not_called()
            self.assertIs(hs._HANG_WANZI_PACKET_RUNNERS[pid], runner)

            # The final owner is the only release that tears down the runner.
            final_stop = hs.stop_wanzi_packet_manual(pid)
            self.assertTrue(final_stop["ok"])
            self.assertEqual(final_stop["owners"], [])
            runner.stop.assert_called_once()
            self.assertNotIn(pid, hs._HANG_WANZI_PACKET_RUNNERS)
            self.assertNotIn(pid, hs._HANG_WANZI_PACKET_OWNERS)
        finally:
            hs.stop_wanzi_packet_hang(pid, release=True)
            hs.stop_wanzi_packet_manual(pid)
            hs._HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
            hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)
            hs._HANG_WANZI_PACKET_HANG_PAUSED.discard(pid)

    def test_wanzi_manual_sender_allows_unknown_live_hang_state(self) -> None:
        from types import SimpleNamespace

        from app.core import hang_settings as hs

        pid = 515189
        sess = SimpleNamespace(pid=pid)
        runner = MagicMock()
        runner.is_running.return_value = True
        runner.stop.return_value = True
        hs._HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
        hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)
        try:
            with patch.object(
                hs,
                "probe_hang_state_mem",
                return_value=SimpleNamespace(ok=False, on=None),
            ), patch.object(hs, "_wanzi_inventory_preflight", return_value={"ok": True, "checked": True, "kind": hs.WANZI_KIND_WAIGONG, "damage": [{"package": 2, "slot": 1}]}), patch.object(hs, "WanziPacketRunner", return_value=runner):
                out = hs.start_wanzi_packet_manual(sess, 40)
            self.assertTrue(out["ok"])
            self.assertTrue(out["running"])
            self.assertIn("挂机状态未确认", out["warning"])
            runner.start.assert_called_once()
        finally:
            hs.stop_wanzi_packet_manual(pid)

    def test_wanzi_last_owner_stop_serializes_a_new_owner_start(self) -> None:
        import threading

        from app.core import hang_settings as hs

        pid = 515187
        old_runner = MagicMock()
        old_runner.is_running.return_value = True
        stop_entered = threading.Event()
        allow_stop = threading.Event()

        def _stop_old() -> bool:
            stop_entered.set()
            self.assertTrue(allow_stop.wait(2.0))
            return True

        old_runner.stop.side_effect = _stop_old
        new_runner = MagicMock()
        new_runner.is_running.return_value = True
        hs._HANG_WANZI_PACKET_RUNNERS[pid] = old_runner
        hs._HANG_WANZI_PACKET_OWNERS[pid] = {"manual"}
        hs._HANG_WANZI_PACKET_HANG_PAUSED.discard(pid)
        stopped: dict = {}
        started: dict = {}

        def _stop() -> None:
            stopped.update(hs.stop_wanzi_packet_manual(pid))

        def _start() -> None:
            started.update(hs.start_wanzi_packet_manual(pid, 40))

        stop_thread = threading.Thread(target=_stop, daemon=True)
        start_thread = threading.Thread(target=_start, daemon=True)
        try:
            with patch.object(
                hs,
                "probe_hang_state_mem",
                return_value=MagicMock(ok=True, on=False),
            ), patch.object(hs, "_wanzi_inventory_preflight", return_value={"ok": True, "checked": True, "kind": hs.WANZI_KIND_WAIGONG, "damage": [{"package": 2, "slot": 1}]}), patch.object(hs, "WanziPacketRunner", return_value=new_runner):
                stop_thread.start()
                self.assertTrue(stop_entered.wait(1.0))
                start_thread.start()
                # The new acquisition must wait for the old final-owner stop;
                # otherwise two sender threads could overlap for this PID.
                start_thread.join(0.05)
                self.assertTrue(start_thread.is_alive())
                self.assertFalse(started)
                allow_stop.set()
                stop_thread.join(1.0)
                start_thread.join(1.0)

            self.assertFalse(stop_thread.is_alive())
            self.assertFalse(start_thread.is_alive())
            self.assertTrue(stopped["ok"])
            self.assertTrue(started["ok"])
            old_runner.stop.assert_called_once()
            new_runner.start.assert_called_once()
            self.assertIs(hs._HANG_WANZI_PACKET_RUNNERS[pid], new_runner)
            self.assertEqual(hs._HANG_WANZI_PACKET_OWNERS[pid], {"manual"})
        finally:
            allow_stop.set()
            stop_thread.join(1.0)
            start_thread.join(1.0)
            hs._HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
            hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)
            hs._HANG_WANZI_PACKET_HANG_PAUSED.discard(pid)

    def test_wanzi_hang_aoi_is_dispatch_produced_and_fail_closed(self) -> None:
        from types import SimpleNamespace

        from app.core import hang_settings as hs

        class _Dispatch:
            def __init__(self):
                self.fns = {}
                self.cancelled = []

            def schedule_periodic(self, _pid, _job, _interval, fn, **_kwargs):
                self.fns[_job] = fn
                return _job

            def cancel_periodic(self, pid, job):
                self.cancelled.append((pid, job))

        pid = 515190
        sess = SimpleNamespace(pid=pid)
        cfg = HangConfig(
            radius=5,
            wanzi_hang=True,
            wanzi_waigong_hang=True,
            wanzi_interval_ms=40,
            wanzi_low_rate_seconds=7,
            wanzi_low_rate_pairs_per_second=1.5,
            wanzi_active_window_s=420,
        )
        dispatch = _Dispatch()
        logs = []
        runner = MagicMock()
        runner.is_running.return_value = True
        runner.stop.return_value = True
        hs._HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
        hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)
        try:
            with patch("app.core.safe_dispatch.get_dispatch", return_value=dispatch), patch.object(
                hs, "WanziPacketRunner", return_value=runner
            ) as runner_cls, patch.object(
                hs,
                "probe_nearby_class2_rpm",
                return_value={"known": True, "has_monster": True, "radius": 18.0},
            ) as probe, patch.object(
                hs, "_wanzi_inventory_preflight", return_value={"ok": True, "checked": True, "kind": hs.WANZI_KIND_WAIGONG, "damage": [{"package": 2, "slot": 1}]}
            ):
                out = hs.start_wanzi_packet_hang(sess, cfg, log=logs.append)
                self.assertTrue(out["ok"])
                can_send = runner_cls.call_args.kwargs["can_send"]
                self.assertEqual(runner_cls.call_args.kwargs["low_rate_seconds"], 7.0)
                self.assertEqual(
                    runner_cls.call_args.kwargs["low_rate_pairs_per_second"], 1.5
                )
                self.assertEqual(runner_cls.call_args.kwargs["active_window_s"], 420.0)
                # Startup has no AOI result yet: soft-unknown must send.
                self.assertTrue(can_send())
                aoi_tick = dispatch.fns[hs._wanzi_aoi_job_id(pid)]
                aoi_tick()
                aoi_tick()
                self.assertTrue(can_send())
                self.assertEqual(probe.call_count, 2)
                self.assertTrue(
                    all(call.args == (pid, None) and call.kwargs == {"radius": 18.0}
                        for call in probe.call_args_list)
                )
                decision_logs = [line for line in logs if "AOI 决策" in line]
                self.assertEqual(len(decision_logs), 1)
                self.assertIn("放包", decision_logs[0])
                hs.stop_wanzi_packet_hang(pid, release=True)
            self.assertTrue(dispatch.cancelled)
        finally:
            hs._HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
            hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)
            hs.clear_wanzi_aoi_state(pid)

    def test_wanzi_aoi_cache_smooths_unknown_and_confirms_no_monster(self) -> None:
        from app.core import wanzi_packet as wp

        pid = 515191
        wp.clear_wanzi_aoi_state(pid)
        try:
            self.assertTrue(wp.wanzi_aoi_can_send(pid))

            # One empty sample is not enough to close the gate.
            first = wp.update_wanzi_aoi_sample(
                pid, {"known": True, "has_monster": False, "reason": "empty1"}
            )
            self.assertFalse(first["known"])
            self.assertTrue(wp.wanzi_aoi_can_send(pid))

            # The second consecutive clean empty sample becomes stable.
            second = wp.update_wanzi_aoi_sample(
                pid,
                {"known": True, "has_monster": False, "reason": "empty2"},
                no_monster_confirm=2,
            )
            self.assertTrue(second["known"])
            self.assertFalse(wp.wanzi_aoi_can_send(pid))

            # A transient read miss keeps the fresh stable cache instead of
            # toggling the sender for one frame.
            cached = wp.update_wanzi_aoi_sample(
                pid, {"known": False, "has_monster": False, "reason": "rpm_busy"}
            )
            self.assertTrue(cached["cached"])
            self.assertFalse(wp.wanzi_aoi_can_send(pid))

            # A positive sample opens immediately.
            wp.update_wanzi_aoi_sample(
                pid, {"known": True, "has_monster": True, "reason": "monster"}
            )
            self.assertTrue(wp.wanzi_aoi_can_send(pid))

            # Scene transition is the only unknown that hard-blocks.
            wp.update_wanzi_aoi_sample(
                pid,
                {
                    "known": False,
                    "has_monster": False,
                    "hard_block": True,
                    "reason": "scene_unstable",
                },
            )
            self.assertFalse(wp.wanzi_aoi_can_send(pid))

            # A generic miss cannot clear a scene-transition hard block.
            wp.update_wanzi_aoi_sample(
                pid, {"known": False, "has_monster": False, "reason": "no_snapshot"}
            )
            self.assertFalse(wp.wanzi_aoi_can_send(pid))

            # A new authoritative AOI result proves the scene is usable again.
            wp.update_wanzi_aoi_sample(
                pid, {"known": True, "has_monster": True, "reason": "settled"}
            )
            self.assertTrue(wp.wanzi_aoi_can_send(pid))
        finally:
            wp.clear_wanzi_aoi_state(pid)

    def test_wanzi_aoi_probe_finds_nearby_class2_with_pure_reads(self) -> None:
        from app.core import wanzi_packet as wp

        root, mid, host, scene, manager, buckets = (
            0x200000,
            0x300000,
            0x380000,
            0x400000,
            0x500000,
            0x600000,
        )
        npc_node, monster_node = 0x680000, 0x681000
        nearby_npc, near_monster = 0x700000, 0x710000
        memory = {
            wp.ROOT_GLOBAL: struct.pack("<I", root),
            root + 0x24: struct.pack("<I", mid),
            mid + wp.HOST_SIDE_OFF: struct.pack("<I", host),
            host + wp.OBJ_POS_OFF: struct.pack("<fff", 0.0, 0.0, 0.0),
            mid + 0x0C: struct.pack("<I", scene),
            scene + wp.NPC_MANAGER_OFF: struct.pack("<I", manager),
            manager + wp.NPC_ARRAY_COUNT_OFF: struct.pack("<I", 2),
            manager + wp.NPC_HASH_BUCKETS_OFF: struct.pack("<I", buckets),
            manager + wp.NPC_HASH_BUCKET_COUNT_OFF: struct.pack("<I", 2),
            buckets: struct.pack("<II", npc_node, monster_node),
            npc_node: struct.pack("<II", 0, nearby_npc),
            monster_node: struct.pack("<II", 0, near_monster),
            nearby_npc: struct.pack("<I", 0x01260F9C),
            near_monster: struct.pack("<I", wp.CECNPC_MONSTER_VFT),
            # The NPC is closer, but it must not open the monster gate.
            nearby_npc + wp.OBJ_POS_OFF: struct.pack("<fff", 1.0, 0.0, 0.0),
            near_monster + wp.OBJ_POS_OFF: struct.pack("<fff", 3.0, 4.0, 0.0),
        }

        def _read(_handle, address, size):
            value = memory[address]
            self.assertEqual(len(value), size)
            return value

        with patch.object(wp, "open_process", return_value=99), patch.object(
            wp, "read_process", side_effect=_read
        ), patch.object(wp.kernel32, "CloseHandle"):
            out = wp.probe_nearby_class2_rpm(1234, None, radius=5.0)
        self.assertTrue(out["known"])
        self.assertTrue(out["has_monster"])
        self.assertEqual(out["object"], near_monster)
        self.assertEqual(out["pos_source"], "rpm_host")
        self.assertAlmostEqual(out["nearest_m"], 5.0)

    def test_wanzi_aoi_keeps_corpse_while_it_remains_in_aoi(self) -> None:
        from app.core import wanzi_packet as wp

        root, mid, host, scene, manager, buckets = (
            0x200000,
            0x300000,
            0x380000,
            0x400000,
            0x500000,
            0x600000,
        )
        dead_monster = 0x710000
        dead_node = 0x718000
        memory = {
            wp.ROOT_GLOBAL: struct.pack("<I", root),
            root + 0x24: struct.pack("<I", mid),
            mid + wp.HOST_SIDE_OFF: struct.pack("<I", host),
            host + wp.OBJ_POS_OFF: struct.pack("<fff", 0.0, 0.0, 0.0),
            mid + 0x0C: struct.pack("<I", scene),
            scene + wp.NPC_MANAGER_OFF: struct.pack("<I", manager),
            manager + wp.NPC_ARRAY_COUNT_OFF: struct.pack("<I", 1),
            manager + wp.NPC_HASH_BUCKETS_OFF: struct.pack("<I", buckets),
            manager + wp.NPC_HASH_BUCKET_COUNT_OFF: struct.pack("<I", 1),
            buckets: struct.pack("<I", dead_node),
            dead_node: struct.pack("<II", 0, dead_monster),
            dead_monster: struct.pack("<I", wp.CECNPC_MONSTER_VFT),
            dead_monster + wp.OBJ_POS_OFF: struct.pack("<fff", 1.0, 0.0, 0.0),
            # 尸体血量：2F8=0（HP归零）
        }

        def _read(_handle, address, size):
            value = memory[address]
            self.assertEqual(len(value), size)
            return value

        with patch.object(wp, "open_process", return_value=99), patch.object(
            wp, "read_process", side_effect=_read
        ), patch.object(wp.kernel32, "CloseHandle"):
            out = wp.probe_nearby_class2_rpm(1234, None, radius=5.0)
        self.assertTrue(out["known"])
        self.assertTrue(out["has_monster"])
        self.assertEqual(out["reason"], "nearby_monster")

    def test_wanzi_aoi_live_monster_still_opens_gate_with_dead_sibling(self) -> None:
        from app.core import wanzi_packet as wp

        root, mid, host, scene, manager, buckets = (
            0x200000,
            0x300000,
            0x380000,
            0x400000,
            0x500000,
            0x600000,
        )
        dead_monster, live_monster = 0x710000, 0x720000
        dead_node, live_node = 0x718000, 0x728000
        memory = {
            wp.ROOT_GLOBAL: struct.pack("<I", root),
            root + 0x24: struct.pack("<I", mid),
            mid + wp.HOST_SIDE_OFF: struct.pack("<I", host),
            host + wp.OBJ_POS_OFF: struct.pack("<fff", 0.0, 0.0, 0.0),
            mid + 0x0C: struct.pack("<I", scene),
            scene + wp.NPC_MANAGER_OFF: struct.pack("<I", manager),
            manager + wp.NPC_ARRAY_COUNT_OFF: struct.pack("<I", 2),
            manager + wp.NPC_HASH_BUCKETS_OFF: struct.pack("<I", buckets),
            manager + wp.NPC_HASH_BUCKET_COUNT_OFF: struct.pack("<I", 2),
            buckets: struct.pack("<II", dead_node, live_node),
            dead_node: struct.pack("<II", 0, dead_monster),
            live_node: struct.pack("<II", 0, live_monster),
            dead_monster: struct.pack("<I", wp.CECNPC_MONSTER_VFT),
            live_monster: struct.pack("<I", wp.CECNPC_MONSTER_VFT),
            dead_monster + wp.OBJ_POS_OFF: struct.pack("<fff", 1.0, 0.0, 0.0),
            live_monster + wp.OBJ_POS_OFF: struct.pack("<fff", 3.0, 4.0, 0.0),
        }

        def _read(_handle, address, size):
            value = memory[address]
            self.assertEqual(len(value), size)
            return value

        with patch.object(wp, "open_process", return_value=99), patch.object(
            wp, "read_process", side_effect=_read
        ), patch.object(wp.kernel32, "CloseHandle"):
            out = wp.probe_nearby_class2_rpm(1234, None, radius=5.0)
        self.assertTrue(out["known"])
        self.assertTrue(out["has_monster"])
        self.assertIn(out["object"], {dead_monster, live_monster})

    def test_wanzi_aoi_unreadable_hp_still_counts_as_monster(self) -> None:
        from app.core import wanzi_packet as wp

        root, mid, host, scene, manager, buckets = (
            0x200000,
            0x300000,
            0x380000,
            0x400000,
            0x500000,
            0x600000,
        )
        monster = 0x710000
        node = 0x718000
        memory = {
            wp.ROOT_GLOBAL: struct.pack("<I", root),
            root + 0x24: struct.pack("<I", mid),
            mid + wp.HOST_SIDE_OFF: struct.pack("<I", host),
            host + wp.OBJ_POS_OFF: struct.pack("<fff", 0.0, 0.0, 0.0),
            mid + 0x0C: struct.pack("<I", scene),
            scene + wp.NPC_MANAGER_OFF: struct.pack("<I", manager),
            manager + wp.NPC_ARRAY_COUNT_OFF: struct.pack("<I", 1),
            manager + wp.NPC_HASH_BUCKETS_OFF: struct.pack("<I", buckets),
            manager + wp.NPC_HASH_BUCKET_COUNT_OFF: struct.pack("<I", 1),
            buckets: struct.pack("<I", node),
            node: struct.pack("<II", 0, monster),
            monster: struct.pack("<I", wp.CECNPC_MONSTER_VFT),
            monster + wp.OBJ_POS_OFF: struct.pack("<fff", 3.0, 4.0, 0.0),
            # 血量字段缺失 => 读不到血量，仍按有怪处理（fail-open）。
        }

        def _read(_handle, address, size):
            value = memory.get(address)
            if value is None:
                raise OSError("no read")
            self.assertEqual(len(value), size)
            return value

        with patch.object(wp, "open_process", return_value=99), patch.object(
            wp, "read_process", side_effect=_read
        ), patch.object(wp.kernel32, "CloseHandle"):
            out = wp.probe_nearby_class2_rpm(1234, None, radius=5.0)
        self.assertTrue(out["known"])
        self.assertTrue(out["has_monster"])
        self.assertEqual(out["object"], monster)

    def test_wanzi_aoi_accepts_large_address_aware_heap_pointers(self) -> None:
        from app.core import wanzi_packet as wp

        self.assertTrue(wp._valid_ptr(0x8301B940))
        self.assertTrue(wp._valid_ptr(0xF0001000))
        self.assertFalse(wp._valid_ptr(0))
        self.assertFalse(wp._valid_ptr(0xFFFF1000))

    def test_wanzi_control_probe_reads_host_gate_triplet_by_pure_rpm(self) -> None:
        from app.core import wanzi_packet as wp

        root, mid, host = 0x200000, 0x300000, 0x380000
        memory = {
            wp.ROOT_GLOBAL: struct.pack("<I", root),
            root + 0x24: struct.pack("<I", mid),
            mid + wp.HOST_SIDE_OFF: struct.pack("<I", host),
            host + wp.HOST_SESSION_GATE_OFF: struct.pack("<III", 5, 800, 6115),
        }

        def _read(_handle, address, size):
            value = memory[address]
            self.assertEqual(len(value), size)
            return value

        with patch.object(wp, "open_process", return_value=99), patch.object(
            wp, "read_process", side_effect=_read
        ), patch.object(wp.kernel32, "CloseHandle"):
            out = wp.probe_wanzi_control_rpm(1234)

        self.assertTrue(out["known"])
        self.assertEqual((out["gate0"], out["gate1"], out["gate2"]), (5, 800, 6115))

    def test_wanzi_control_gate_blocks_all_captured_stages_then_recovers(self) -> None:
        from app.core import wanzi_packet as wp

        pid = 515199
        wp.clear_wanzi_control_state(pid)
        try:
            for index, gate1 in enumerate((50, 800, 450)):
                with patch.object(wp.time, "monotonic", return_value=10.0 + index * 0.01):
                    state = wp.update_wanzi_control_sample(
                        pid,
                        {"known": True, "gate0": 5, "gate1": gate1, "gate2": 6115},
                    )
                    self.assertTrue(state["active"])
                    self.assertFalse(wp.wanzi_control_can_send(pid))

            # Clearing the triplet starts, rather than skips, the 50ms fence.
            with patch.object(wp.time, "monotonic", return_value=10.10):
                state = wp.update_wanzi_control_sample(
                    pid, {"known": True, "gate0": 0, "gate1": 0, "gate2": 0}
                )
                self.assertTrue(state["blocked"])
                self.assertFalse(wp.wanzi_control_can_send(pid))
            with patch.object(wp.time, "monotonic", return_value=10.16):
                state = wp.update_wanzi_control_sample(
                    pid, {"known": True, "gate0": 0, "gate1": 0, "gate2": 0}
                )
                self.assertFalse(state["blocked"])
                self.assertTrue(wp.wanzi_control_can_send(pid))
        finally:
            wp.clear_wanzi_control_state(pid)

    def test_wanzi_control_gate_does_not_block_other_session_signature(self) -> None:
        from app.core import wanzi_packet as wp

        pid = 515200
        wp.clear_wanzi_control_state(pid)
        try:
            state = wp.update_wanzi_control_sample(
                pid,
                {"known": True, "gate0": 5, "gate1": 1000, "gate2": 2685},
            )
            self.assertFalse(state["active"])
            self.assertTrue(wp.wanzi_control_can_send(pid))
        finally:
            wp.clear_wanzi_control_state(pid)

    def test_wanzi_control_gate_blocks_ground_recovery_until_space_clear(self) -> None:
        from app.core import wanzi_packet as wp

        pid = 515203
        wp.clear_wanzi_control_state(pid)
        try:
            # Captured while deliberately remaining down without pressing
            # Space: 1000 -> 4000 -> 1400, all under gate2=6104.
            for index, gate1 in enumerate((1000, 4000, 1400)):
                with patch.object(wp.time, "monotonic", return_value=30.0 + index):
                    state = wp.update_wanzi_control_sample(
                        pid,
                        {"known": True, "gate0": 5, "gate1": gate1, "gate2": 6104},
                    )
                    self.assertTrue(state["active"])
                    self.assertFalse(wp.wanzi_control_can_send(pid))

            # Space recovery clears the session triplet; retain only the
            # configured 50ms post-clear fence.
            with patch.object(wp.time, "monotonic", return_value=33.0):
                state = wp.update_wanzi_control_sample(
                    pid, {"known": True, "gate0": 0, "gate1": 0, "gate2": 0}
                )
                self.assertTrue(state["blocked"])
                self.assertFalse(wp.wanzi_control_can_send(pid))
            with patch.object(wp.time, "monotonic", return_value=33.06):
                state = wp.update_wanzi_control_sample(
                    pid, {"known": True, "gate0": 0, "gate1": 0, "gate2": 0}
                )
                self.assertFalse(state["blocked"])
                self.assertTrue(wp.wanzi_control_can_send(pid))
        finally:
            wp.clear_wanzi_control_state(pid)

    def test_wanzi_control_gate_caches_one_missed_read_but_cannot_stick(self) -> None:
        from app.core import wanzi_packet as wp

        pid = 515201
        wp.clear_wanzi_control_state(pid)
        try:
            with patch.object(wp.time, "monotonic", return_value=20.0):
                wp.update_wanzi_control_sample(
                    pid,
                    {"known": True, "gate0": 5, "gate1": 800, "gate2": 6115},
                )
            with patch.object(wp.time, "monotonic", return_value=20.10):
                state = wp.update_wanzi_control_sample(
                    pid, {"known": False, "reason": "rpm_busy"}
                )
                self.assertTrue(state["cached"])
                self.assertFalse(wp.wanzi_control_can_send(pid))
            with patch.object(wp.time, "monotonic", return_value=20.40):
                state = wp.update_wanzi_control_sample(
                    pid, {"known": False, "reason": "rpm_busy"}
                )
                self.assertFalse(state["active"])
                self.assertTrue(wp.wanzi_control_can_send(pid))
        finally:
            wp.clear_wanzi_control_state(pid)

    def test_wanzi_control_gate_blocks_manual_owner_from_shared_sender(self) -> None:
        from app.core import hang_settings as hs
        from app.core import wanzi_packet as wp

        pid = 515202
        hs._HANG_WANZI_PACKET_OWNERS[pid] = {hs.WANZI_PACKET_OWNER_MANUAL}
        try:
            wp.update_wanzi_control_sample(
                pid,
                {"known": True, "gate0": 5, "gate1": 50, "gate2": 6115},
            )
            self.assertFalse(hs._wanzi_shared_can_send(pid))
        finally:
            hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)
            wp.clear_wanzi_control_state(pid)

    def test_wanzi_default_140ms_text_includes_measured_rate(self) -> None:
        from app.core import hang_settings as hs

        self.assertEqual(hs.DEFAULT_WANZI_INTERVAL_MS, 140)
        text = hs._wanzi_cadence_text(
            {
                "low_rate_seconds": 6.0,
                "low_rate_pairs_per_second": 3.0,
                "active_window_s": 300.0,
            },
            140,
        )
        self.assertIn("每140ms释放1次", text)
        self.assertIn("约350次/分钟", text)

    def test_wanzi_hang_control_schedules_background_space_recovery(self) -> None:
        import threading

        from app.core import hang_settings as hs

        pid = 515204
        called = threading.Event()
        logs: list[str] = []
        sent: dict = {}
        hs._HANG_WANZI_PACKET_OWNERS[pid] = {hs.WANZI_PACKET_OWNER_HANG}
        hs._HANG_WANZI_PACKET_HANG_PAUSED.discard(pid)

        def _press(*args, **kwargs):
            sent.update(args=args, kwargs=kwargs)
            called.set()
            return {"ok": True}

        try:
            with patch.object(
                hs, "HANG_WANZI_RECOVERY_INITIAL_DELAY_S", 0.0
            ), patch.object(
                hs, "HANG_WANZI_RECOVERY_RETRY_S", 0.01
            ), patch.object(
                hs, "HANG_WANZI_RECOVERY_MAX_PULSES", 1
            ), patch.object(
                hs,
                "get_wanzi_control_state",
                return_value={"active": True, "gate0": 5, "gate1": 800, "gate2": 6115},
            ), patch(
                "app.core.bg_input.press_bg_chord_once", side_effect=_press
            ):
                self.assertTrue(hs._schedule_wanzi_auto_recovery(pid, log=logs.append))
                self.assertTrue(called.wait(1.0))
            self.assertTrue(
                any("KEY_HOLD Hook+InjectKey" in line for line in logs)
            )
            self.assertEqual(sent["args"][1], [0x20])
            self.assertEqual(sent["kwargs"]["hold_ms"], 80)
            self.assertTrue(sent["kwargs"]["allow_softsend"])
        finally:
            hs._cancel_wanzi_auto_recovery(pid)
            hs._HANG_WANZI_PACKET_OWNERS.pop(pid, None)
            hs._HANG_WANZI_PACKET_HANG_PAUSED.discard(pid)

    def test_wanzi_control_timer_rebuilds_when_token_outlives_periodic(self) -> None:
        from app.core import hang_settings as hs

        class _Dispatch:
            def __init__(self):
                self.jobs = {}
                self.schedules = 0

            def list_periodics(self, _pid):
                return [
                    {"job_id": job_id, "alive": True}
                    for job_id in self.jobs
                ]

            def schedule_periodic(self, _pid, job_id, _interval, fn, **_kwargs):
                self.schedules += 1
                self.jobs[job_id] = fn
                return job_id

            def cancel_periodic(self, _pid, job_id):
                self.jobs.pop(job_id, None)

        pid = 515205
        dispatch = _Dispatch()
        logs: list[str] = []
        try:
            with patch("app.core.safe_dispatch.get_dispatch", return_value=dispatch):
                hs._schedule_wanzi_control_gate(pid, log=logs.append)
                self.assertEqual(dispatch.schedules, 1)
                # Reproduce the live failure: dispatcher timer disappears but
                # the feature's logical token remains present.
                dispatch.jobs.clear()
                hs._schedule_wanzi_control_gate(pid, log=logs.append)
                self.assertEqual(dispatch.schedules, 2)
                self.assertTrue(any("已重建" in line for line in logs))
        finally:
            with patch("app.core.safe_dispatch.get_dispatch", return_value=dispatch):
                hs._cancel_wanzi_control_gate(pid)

    def test_wanzi_runner_low_rate_step_sends_three_pairs_per_second(self) -> None:
        from app.core import wanzi_packet as wp

        runner = wp.WanziPacketRunner(
            515195,
            40,
            can_send=lambda: True,
        )
        with patch.object(wp, "_send_packet", return_value=1) as send, patch.object(
            runner, "_wait", return_value=False
        ) as wait:
            self.assertFalse(runner._run_low_rate_step())

        self.assertEqual(
            [call.args[1] for call in send.call_args_list],
            [runner.p1, wp.P2],
        )
        self.assertAlmostEqual(wait.call_args_list[0].args[0], 1.0 / 3.0)
        stats = runner.stats()
        self.assertEqual(stats["low_rate_pairs"], 1)
        self.assertEqual(stats["active_pairs"], 0)

    def test_wanzi_runner_active_step_sends_one_pair_then_user_interval(self) -> None:
        from app.core import wanzi_packet as wp

        runner = wp.WanziPacketRunner(515196, 50, can_send=lambda: True)
        with patch.object(wp, "_send_packet", return_value=1) as send, patch.object(
            runner, "_wait", return_value=False
        ) as wait:
            self.assertFalse(runner._run_active_step())

        self.assertEqual(
            [call.args[1] for call in send.call_args_list],
            [runner.p1, wp.P2],
        )
        wait.assert_called_once_with(0.05)
        stats = runner.stats()
        self.assertEqual(stats["pairs"], 1)
        self.assertEqual(stats["active_pairs"], 1)

    def test_wanzi_runner_closed_aoi_gate_waits_without_sending(self) -> None:
        from app.core import wanzi_packet as wp

        runner = wp.WanziPacketRunner(515198, 50, can_send=lambda: False)
        with patch.object(wp, "_send_packet") as send, patch.object(
            runner, "_wait", return_value=True
        ) as wait:
            self.assertTrue(runner._run_active_step())

        send.assert_not_called()
        wait.assert_called_once_with(wp.WANZI_AOI_GATE_WAIT_S)
        self.assertEqual(runner.stats()["gate_skips"], 1)

    def test_wanzi_runner_enters_active_after_low_rate_phase(self) -> None:
        from app.core import wanzi_packet as wp

        runner = wp.WanziPacketRunner(
            515197,
            40,
            low_rate_seconds=6.0,
            active_window_s=300.0,
        )

        def _finish_active_step() -> bool:
            runner._stop.set()
            return False

        with patch.object(runner, "_run_low_rate_step", return_value=False) as low, patch.object(
            runner, "_run_active_step", side_effect=_finish_active_step
        ) as active, patch.object(
            wp.time, "monotonic", side_effect=[0.0, 0.0, 6.0, 6.0, 6.0]
        ):
            runner._run()

        low.assert_called_once()
        active.assert_called_once()
        self.assertEqual(runner.stats()["phase"], "active")

    def test_youfeng_writes_only_last_autoplay_skill_slot(self) -> None:
        from app.core import hang_settings as hs

        before_slots = [
            {"skill_id": 0x1000 + i, "interval": 1.0 + i / 10.0}
            for i in range(9)
        ]
        before = {
            "ok": True,
            "autoplay": 0x500000,
            "slots": before_slots,
            "gate_ok": True,
            "gate_a": 0x80000001,
            "gate_b": 0x1000,
            "flags": 0x23,
        }
        after_slots = [dict(slot) for slot in before_slots]
        after_slots[8] = {
            "skill_id": hs.YOUFENG_SKILL_ID,
            "interval": 0.275,
        }
        after = {"ok": True, "slots": after_slots}
        session = MagicMock(pid=4242)
        with patch.object(
            hs, "read_autoplay_skills", side_effect=[before, after]
        ), patch.object(
            hs,
            "remote_write_bytes",
            return_value=8,
        ) as write:
            out = hs.prepare_youfeng_hang(
                session,
                HangConfig(youfeng_hang=True, wanzi_interval_ms=275),
            )

        self.assertTrue(out["ok"])
        pid, address, payload = write.call_args.args
        self.assertEqual(pid, 4242)
        self.assertEqual(
            address,
            0x500000 + hs.AUTOPLAY_SKILL_SLOT0_OFF + 8 * hs.AUTOPLAY_SKILL_SLOT_STRIDE,
        )
        skill_id, interval = struct.unpack("<If", payload)
        self.assertEqual(skill_id, hs.YOUFENG_SKILL_ID)
        self.assertAlmostEqual(interval, 0.275, places=4)
        self.assertEqual(out["interval_ms"], 275)
        self.assertNotIn("pct", out)
        self.assertNotIn("write_warning", out)

    def test_youfeng_hang_runner_observes_autoplay_and_stops_with_hang(self) -> None:
        from app.core import hang_settings as hs

        class Session:
            pid = 515190
            hwnd = 0x1234

        bridge = MagicMock()
        runner = MagicMock()
        runner.start.return_value = True
        runner.wait_ready.return_value = True
        runner.stats.return_value = {"error": ""}
        runner.stop.return_value = True
        runner.is_running.return_value = True
        hs._HANG_YOUFENG_RUNNERS.pop(Session.pid, None)
        with patch(
            "app.core.xajh_bridge.ensure_bridge", return_value=bridge
        ), patch.object(hs, "YoufengChainRunner", return_value=runner) as runner_cls:
            started = hs.start_youfeng_hang(
                Session(), HangConfig(youfeng_hang=True), log=lambda _m: None
            )
            stopped = hs.stop_youfeng_hang(Session.pid, log=lambda _m: None)

        self.assertTrue(started["ok"])
        self.assertTrue(stopped["ok"])
        runner_cls.assert_called_once_with(
            Session.pid,
            Session.hwnd,
            interrupt_mode=hs.INTERRUPT_QINGGONG_PULSE,
            drive_casts=False,
            observe_ultimate=True,
            pulse_stop_ms=0,
            suppress_lift=False,
            log=unittest.mock.ANY,
            on_status=unittest.mock.ANY,
        )
        runner.stop.assert_called_once_with()
        runner.wait_ready.assert_called_once_with(3.0)
        bridge.close.assert_called_once_with()

    def test_manual_youfeng_hook_survives_hang_owner_stop(self) -> None:
        from app.core import hang_settings as hs

        class Session:
            pid = 515192
            hwnd = 0x2468

        bridge = MagicMock()
        runner = MagicMock()
        runner.start.return_value = True
        runner.wait_ready.return_value = True
        runner.is_running.return_value = True
        runner.stop.return_value = True
        hs._HANG_YOUFENG_RUNNERS.pop(Session.pid, None)
        hs._HANG_YOUFENG_OWNERS.pop(Session.pid, None)
        try:
            with patch(
                "app.core.xajh_bridge.ensure_bridge", return_value=bridge
            ), patch.object(hs, "YoufengChainRunner", return_value=runner) as runner_cls:
                manual = hs.start_youfeng_key_hook(
                    Session(),
                    owner=hs.YOUFENG_HOOK_OWNER_MANUAL,
                    log=lambda _m: None,
                )
                hang = hs.start_youfeng_hang(
                    Session(), HangConfig(youfeng_hang=True), log=lambda _m: None
                )
                hang_stopped = hs.stop_youfeng_hang(
                    Session.pid, log=lambda _m: None
                )
                manual_state = hs.get_youfeng_key_hook_state(
                    Session.pid, owner=hs.YOUFENG_HOOK_OWNER_MANUAL
                )

                self.assertTrue(manual["ok"])
                self.assertTrue(hang["reused"])
                self.assertTrue(hang_stopped["retained"])
                self.assertTrue(manual_state["enabled"])
                runner.stop.assert_not_called()

                manual_stopped = hs.stop_youfeng_key_hook(
                    Session.pid,
                    owner=hs.YOUFENG_HOOK_OWNER_MANUAL,
                    log=lambda _m: None,
                )
                self.assertTrue(manual_stopped["ok"])
                self.assertFalse(manual_stopped["running"])
                runner.stop.assert_called_once_with()
                runner_cls.assert_called_once()
        finally:
            hs._HANG_YOUFENG_RUNNERS.pop(Session.pid, None)
            hs._HANG_YOUFENG_OWNERS.pop(Session.pid, None)

    def test_package_build_rebuilds_and_verifies_bridge(self) -> None:
        script = Path("tools/build_gui.bat").read_text(encoding="utf-8")
        self.assertIn('call "native\\xajh_bridge\\_build_once.bat"', script)
        self.assertNotIn(
            'if not exist "native\\bin\\xajh_bridge.dll" (\n'
            '  call "native\\xajh_bridge\\_build_once.bat"',
            script,
        )
        self.assertIn("tools\\_verify_packaged_bridge.py", script)
        self.assertNotIn("Get-FileHash", script)

        verifier = Path("tools/_verify_packaged_bridge.py").read_text(encoding="utf-8")
        self.assertIn("hashlib.sha256()", verifier)
        self.assertIn("packaged bridge hash mismatch", verifier)

    def test_youfeng_hook_stop_timeout_keeps_live_owner_state(self) -> None:
        from app.core import hang_settings as hs

        pid = 515193
        runner = MagicMock()
        runner.stop.return_value = False
        runner.is_running.return_value = True
        hs._HANG_YOUFENG_RUNNERS[pid] = runner
        hs._HANG_YOUFENG_OWNERS[pid] = {hs.YOUFENG_HOOK_OWNER_MANUAL}
        try:
            result = hs.stop_youfeng_key_hook(
                pid,
                owner=hs.YOUFENG_HOOK_OWNER_MANUAL,
                log=lambda _m: None,
            )
            state = hs.get_youfeng_key_hook_state(
                pid, owner=hs.YOUFENG_HOOK_OWNER_MANUAL
            )
            self.assertFalse(result["ok"])
            self.assertTrue(result["enabled"])
            self.assertTrue(state["enabled"])
        finally:
            hs._HANG_YOUFENG_RUNNERS.pop(pid, None)
            hs._HANG_YOUFENG_OWNERS.pop(pid, None)

    def test_skip_dungeon_story_pref_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "hang_prefs.json"
            roles = Path(td) / "roles"
            with patch("app.core.hang_settings._hang_prefs_path", return_value=path), patch(
                "app.core.account_manager.roles_root", return_value=roles
            ), patch(
                "app.core.account_manager.global_dir", return_value=Path(td) / "global"
            ):
                save_hang_disk_from_config(
                    HangConfig(skip_dungeon_story=True, wanzi_hang=False),
                    char_id=200001,
                    char_name="测跳过",
                )
                cfg = get_hang_config(char_id=200001)
                self.assertTrue(cfg.skip_dungeon_story)
                write_hang_config_to_settings({}, cfg)
                raw = load_hang_prefs(200001)
                self.assertTrue(raw.get("skip_dungeon_story"))

    def test_ignore_dungeon_stuck_persists_and_migrates_legacy_value(self) -> None:
        """The renamed dungeon-only setting persists per role and defaults off."""
        from pathlib import Path
        from unittest.mock import patch

        import tempfile

        from app.core.hang_settings import (
            HangConfig,
            get_hang_config,
            load_hang_prefs,
            save_hang_disk_from_config,
        )

        # legacy/missing -> defaults off
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "hang_prefs.json"
            roles = Path(td) / "roles"
            with patch("app.core.hang_settings._hang_prefs_path", return_value=path), patch(
                "app.core.account_manager.roles_root", return_value=roles
            ), patch(
                "app.core.account_manager.global_dir", return_value=Path(td) / "global"
            ):
                cfg = get_hang_config(char_id=300001)
                self.assertFalse(cfg.ignore_dungeon_stuck)

                save_hang_disk_from_config(
                    HangConfig(ignore_dungeon_stuck=True),
                    char_id=300001,
                )
                cfg2 = get_hang_config(char_id=300001)
                self.assertTrue(cfg2.ignore_dungeon_stuck)
                raw = load_hang_prefs(300001)
                self.assertTrue(raw.get("ignore_dungeon_stuck"))
                settings = {}
                write_hang_config_to_settings(settings, cfg2)
                self.assertTrue(settings.get("hang_ignore_dungeon_stuck"))

                path.write_text(
                    '{"version": 6, "default": {}, "by_id": {"300002": {"ignore_on_start": true}}}',
                    encoding="utf-8",
                )
                cfg3 = get_hang_config(char_id=300002)
                self.assertTrue(cfg3.ignore_dungeon_stuck)

    def test_plot_skip_is_event_driven_not_polled(self) -> None:
        """hang_guard must not CRT-scan plot dialogs every tick."""
        from pathlib import Path
        import app.core.hang_settings as hs

        src = Path(hs.__file__).read_text(encoding="utf-8")
        self.assertNotIn("Periodic story skip while hang is running", src)
        # scene rearm / hang start keep event hooks
        self.assertIn("Scene-edge event only", src)
        self.assertIn("skip_dungeon_story_once", src)
        self.assertIn("schedule_skip_dungeon_story_async", src)
        self.assertIn("scheduled async", src)
        self.assertIn("cg hook armed", src)
        self.assertNotIn("query_dlg_show(session, name", src.split("def _hang_guard_tick")[-1].split("def stop_hang_guard")[0] if "def _hang_guard_tick" in src else "")

    def test_skip_dungeon_story_once_force_pulses_keys(self) -> None:
        from app.core import hang_settings as hs

        class Session:
            pid = 9001
            hwnd = 1

        class _CG:
            ok = True
            ret = 0
            note = "CG_SKIP on=1"
            error = ""

        br = MagicMock()
        br.cg_skip.return_value = _CG()
        pulses = []

        def _pulse(pid, chord, **kwargs):
            pulses.append((pid, list(chord), dict(kwargs)))
            return {"ok": True, "via": "key_hold"}

        with patch("app.core.xajh_bridge.ensure_bridge", return_value=br), patch(
            "app.core.plg_ui.query_dlg_show", side_effect=RuntimeError("no dlg")
        ), patch(
            "app.core.bg_input.press_bg_chord_once", side_effect=_pulse
        ):
            # force=True arms CG hook but suppresses Esc when no dialog is visible.
            out = hs.skip_dungeon_story_once(Session(), force=True, log=lambda _m: None)
        self.assertTrue(out["ok"])
        self.assertEqual(int(out.get("esc") or 0), 0)
        self.assertEqual(int(out.get("space") or 0), 0)
        self.assertFalse(pulses)
        self.assertTrue((out.get("cg_hook") or {}).get("ok"))
        br.ui_key.assert_not_called()
        br.cg_skip.assert_called()

    def test_skip_dungeon_story_arm_only_no_keys(self) -> None:
        from app.core import hang_settings as hs

        class Session:
            pid = 9002
            hwnd = 1

        class _CG:
            ok = True
            ret = 0
            note = "CG_SKIP on=1"
            error = ""

        br = MagicMock()
        br.cg_skip.return_value = _CG()
        with patch("app.core.xajh_bridge.ensure_bridge", return_value=br):
            out = hs.skip_dungeon_story_once(
                Session(), force=True, arm_only=True, log=lambda _m: None
            )
        self.assertTrue(out["ok"])
        self.assertTrue(out.get("arm_only"))
        self.assertEqual(int(out.get("esc") or 0), 0)
        self.assertEqual(int(out.get("space") or 0), 0)
        br.ui_key.assert_not_called()
        br.cg_skip.assert_called_with(mode=1, timeout_ms=1500)

    def test_update_hang_guard_config_hot_toggle_is_arm_only(self) -> None:
        from app.core import hang_settings as hs

        class Sess:
            pid = 9003
            hwnd = 0x11

        calls = []

        def _sched(*_a, **kw):
            calls.append(dict(kw))
            return {"ok": True, "scheduled": True}

        with hs._HANG_GUARD_CFG_LOCK:
            hs._HANG_GUARD_CFG[9003] = hs.HangConfig(skip_dungeon_story=False)
            hs._HANG_GUARD_SESSION[9003] = Sess()
        try:
            with patch.object(hs, "schedule_skip_dungeon_story_async", side_effect=_sched):
                out = hs.update_hang_guard_config(
                    9003, hs.HangConfig(skip_dungeon_story=True), log=lambda _m: None
                )
            self.assertTrue(out.get("ok"))
            self.assertTrue(calls)
            self.assertTrue(calls[0].get("arm_only"))
        finally:
            with hs._HANG_GUARD_CFG_LOCK:
                hs._HANG_GUARD_CFG.pop(9003, None)
                hs._HANG_GUARD_SESSION.pop(9003, None)

    def test_plot_skip_arms_cg_hook_source(self) -> None:
        from pathlib import Path
        import app.core.hang_settings as hs

        src = Path(hs.__file__).read_text(encoding="utf-8")
        self.assertIn("bridge.cg_skip(mode=1", src)
        self.assertIn("CMD_CG_SKIP", Path("native/xajh_bridge/bridge_protocol.h").read_text(encoding="utf-8"))
        self.assertIn("Hook_PlayCG", Path("native/xajh_bridge/dllmain.cpp").read_text(encoding="utf-8"))

    def test_wanzi_and_youfeng_share_interval_in_status(self) -> None:
        from app.core import hang_settings as hs

        wanzi = hs.hang_start_warnings(
            HangConfig(wanzi_hang=True, wanzi_interval_ms=345)
        )
        youfeng = hs.hang_start_warnings(
            HangConfig(youfeng_hang=True, wanzi_interval_ms=345)
        )
        self.assertTrue(any("345ms" in tip for tip in wanzi))
        self.assertTrue(any("345ms" in tip for tip in youfeng))
        self.assertFalse(any("100%" in tip for tip in youfeng))

    def test_hang_start_stop_bind_youfeng_lifecycle(self) -> None:
        from app.core import hang_settings as hs

        class Session:
            pid = 515191
            hwnd = 0x5678

        cfg = HangConfig(mode=0, youfeng_hang=True)
        order = []
        with patch.object(
            hs,
            "_start_hang_unlocked",
            side_effect=lambda *_a, **_k: (
                order.append("hang") or {"ok": True, "message": "started"}
            ),
        ), patch.object(
            hs,
            "start_youfeng_hang",
            side_effect=lambda *_a, **_k: (
                order.append("observer") or {"ok": True, "running": True}
            ),
        ) as start_yf:
            started = hs.start_hang(Session(), cfg, log=lambda _m: None)
        self.assertTrue(started["ok"])
        self.assertEqual(order, ["observer", "hang"])
        start_yf.assert_called_once_with(
            unittest.mock.ANY,
            cfg,
            hwnd=0,
            log=unittest.mock.ANY,
        )

        with patch.object(hs, "_start_uses_force_function", return_value=False), patch.object(
            hs, "resolve_empty_skill_for_action", return_value=False
        ), patch.object(
            hs, "_stop_hang_unlocked", return_value={"ok": True, "message": "stopped"}
        ), patch.object(
            hs, "stop_youfeng_hang", return_value={"ok": True}
        ) as stop_yf:
            stopped = hs.stop_hang(Session(), cfg, log=lambda _m: None)
        self.assertTrue(stopped["ok"])
        stop_yf.assert_called_once_with(unittest.mock.ANY, log=unittest.mock.ANY)



class HangGuardScheduleTests(unittest.TestCase):
    """Hang owns cadence; SafeDispatch only hosts the timer. @author by ak"""

    def test_start_stop_hang_guard(self) -> None:
        from app.core import hang_settings as hs
        from app.core.hang_settings import HangConfig
        from app.core.safe_dispatch import get_dispatch, reset_dispatch_for_tests

        reset_dispatch_for_tests()

        class _S:
            pid = 515151
            hwnd = 0

        sess = _S()
        cfg = HangConfig(enable_pickup=False, auto_repair=False, auto_vitality=False)
        with patch.object(hs, "_iter_active_loot_rolls", return_value=[]), patch.object(
            hs, "hang_maintain_once"
        ) as maint:
            ret = hs.start_hang_guard(
                sess, cfg, interval_s=hs.HANG_GUARD_TICK_S * 0.05, log=lambda m: None
            )
            # use short interval only for test speed; production uses HANG_GUARD_TICK_S
            ret = hs.start_hang_guard(sess, cfg, interval_s=0.05, log=lambda m: None)
            self.assertTrue(ret.get("ok"))
            import time

            time.sleep(0.16)
            self.assertEqual(maint.call_count, 0)
            jobs = get_dispatch().list_periodics(515151)
            self.assertTrue(any(j["job_id"] == hs.HANG_GUARD_JOB_ID for j in jobs))
            hs.stop_hang_guard(sess, log=lambda m: None)
            jobs2 = get_dispatch().list_periodics(515151)
            self.assertFalse(any(j["job_id"] == hs.HANG_GUARD_JOB_ID for j in jobs2))
        reset_dispatch_for_tests()

    def test_start_stop_hang_guard_with_ignore_has_no_release_job(self) -> None:
        from app.core import hang_settings as hs
        from app.core.hang_settings import HangConfig
        from app.core.safe_dispatch import get_dispatch, reset_dispatch_for_tests

        reset_dispatch_for_tests()

        class _S:
            pid = 515152
            hwnd = 0

        sess = _S()
        cfg = HangConfig(
            enable_pickup=False,
            auto_repair=False,
            auto_vitality=False,
            ignore_dungeon_stuck=True,
        )
        with patch.object(hs, "_iter_active_loot_rolls", return_value=[]), patch.object(
            hs, "hang_maintain_once"
        ) as maint:
            hs.start_hang_guard(sess, cfg, interval_s=0.05, log=lambda m: None)
            jobs = get_dispatch().list_periodics(515152)
            self.assertEqual(
                [j for j in jobs if j["job_id"] != hs.HANG_GUARD_JOB_ID], []
            )
            hs.stop_hang_guard(sess, log=lambda m: None)
            jobs2 = get_dispatch().list_periodics(515152)
            self.assertEqual(jobs2, [])
        reset_dispatch_for_tests()

    def test_hang_guard_triggers_maintain_on_pending_rolls(self) -> None:
        from app.core import hang_settings as hs
        from app.core.hang_settings import HangConfig
        from app.core.safe_dispatch import reset_dispatch_for_tests

        reset_dispatch_for_tests()

        class _S:
            pid = 515152
            hwnd = 0

        sess = _S()
        cfg = HangConfig(enable_pickup=False, auto_repair=False, auto_vitality=False)
        pending = [{"id0": 1, "id1": 0x02000000, "id2": 0}]
        with patch.object(hs, "_iter_active_loot_rolls", return_value=pending), patch.object(
            hs, "hang_maintain_once", return_value={"ok": True}
        ) as maint:
            hs.start_hang_guard(sess, cfg, interval_s=0.05, log=lambda m: None)
            import time

            time.sleep(0.18)
            self.assertGreaterEqual(maint.call_count, 1)
            hs.stop_hang_guard(sess, log=lambda m: None)
        reset_dispatch_for_tests()

    def test_guard_owns_session_after_caller_attach_closes(self) -> None:
        from app.core import hang_settings as hs
        from app.core.game_attach import GameAttachSession
        from app.core.hang_settings import HangConfig

        class _Dispatch:
            tick = None

            def read_cached(self, _pid, _key, producer, **_kwargs):
                return producer()

            def schedule_periodic(self, _pid, _job, _interval, fn, **_kwargs):
                self.tick = fn

            def cancel_periodic(self, *_args, **_kwargs):
                pass

            def invalidate(self, *_args, **_kwargs):
                pass

        pid = 515158
        caller = GameAttachSession()
        caller.pid = pid
        caller.hwnd = 0
        disp = _Dispatch()
        seen_pids = []

        def fake_attach(sess, attach_pid):
            sess.pid = int(attach_pid)
            sess.module_base = 0x400000

        cfg = HangConfig(enable_pickup=False, auto_repair=False, auto_vitality=False)
        with patch("app.core.safe_dispatch.get_dispatch", return_value=disp), patch.object(
            GameAttachSession, "attach", fake_attach
        ), patch.object(
            hs,
            "_iter_active_loot_rolls",
            side_effect=lambda sess, **_kwargs: seen_pids.append(sess.pid) or [],
        ):
            out = hs.start_hang_guard(caller, cfg, log=lambda _m: None)
            self.assertTrue(out["ok"])
            owned = hs._HANG_GUARD_SESSION[pid]
            self.assertIsNot(owned, caller)
            caller.close()
            self.assertIsNone(caller.pid)
            disp.tick()
            self.assertEqual(seen_pids, [pid])
            hs.stop_hang_guard(pid, log=lambda _m: None)
            self.assertIsNone(owned.pid)

    def test_hang_guard_death_edge_forces_rv_once_after_revival(self) -> None:
        """A death edge waits for revival, then bypasses RV intervals once."""
        import time

        from app.core import hang_settings as hs
        from app.core.hang_settings import HangConfig

        class _S:
            pid = 515155
            hwnd = 0

        class _Dispatch:
            tick = None

            def read_cached(self, *_args, **_kwargs):
                return []

            def schedule_periodic(self, _pid, _job, _interval, fn, **_kwargs):
                self.tick = fn

            def cancel_periodic(self, *_args, **_kwargs):
                pass

            def invalidate(self, *_args, **_kwargs):
                pass

        sess = _S()
        pid = sess.pid
        for table in (
            hs._HANG_PID_LAST,
            hs._HANG_DEAD_STATE,
            hs._HANG_GUARD_DEATH_SEEN_TS,
        ):
            table.pop(pid, None)
        hs._HANG_GUARD_DEATH_MAINTAIN.discard(pid)
        hs._mark_hang_cooldown(sess, "repair_check")
        hs._HANG_DEAD_STATE[pid] = {
            "dead": True,
            "prev": False,
            "ts": time.monotonic(),
            "source": "test",
        }
        disp = _Dispatch()
        cfg = HangConfig(auto_repair=True, auto_vitality=False, enable_pickup=True)
        with patch("app.core.safe_dispatch.get_dispatch", return_value=disp), patch.object(
            hs, "hang_maintain_once", return_value={"ok": True, "busy": False}
        ) as maint:
            hs.start_hang_guard(sess, cfg, log=lambda _m: None)
            disp.tick()
            self.assertEqual(maint.call_count, 0)
            self.assertIn(pid, hs._HANG_GUARD_DEATH_MAINTAIN)
            hs._HANG_DEAD_STATE[pid] = {
                "dead": False,
                "prev": True,
                "ts": time.monotonic(),
                "source": "test_revived",
            }
            disp.tick()
            self.assertEqual(maint.call_count, 1)
            self.assertTrue(maint.call_args.kwargs["force_rv"])
            disp.tick()
            self.assertEqual(maint.call_count, 1)
            hs.stop_hang_guard(sess, log=lambda _m: None)
        hs._HANG_DEAD_STATE.pop(pid, None)
        hs._HANG_PID_LAST.pop(pid, None)

    def test_repeated_start_reuses_guard_and_preserves_death_work(self) -> None:
        from app.core import hang_settings as hs
        from app.core.hang_settings import HangConfig

        class _S:
            pid = 515159
            hwnd = 0

        class _Dispatch:
            tick = None
            schedules = 0

            def list_periodics(self, _pid):
                if self.tick is None:
                    return []
                return [{"job_id": hs.HANG_GUARD_JOB_ID, "alive": True}]

            def schedule_periodic(self, _pid, _job, _interval, fn, **_kwargs):
                self.schedules += 1
                self.tick = fn

            def cancel_periodic(self, *_args, **_kwargs):
                self.tick = None

            def invalidate(self, *_args, **_kwargs):
                pass

        sess = _S()
        pid = sess.pid
        disp = _Dispatch()
        first_cfg = HangConfig(enable_pickup=False)
        next_cfg = HangConfig(enable_pickup=True)
        with patch("app.core.safe_dispatch.get_dispatch", return_value=disp):
            first = hs.start_hang_guard(sess, first_cfg, log=lambda _m: None)
            first_token = hs._HANG_GUARD_TOKEN[pid]
            hs._HANG_GUARD_DEATH_SEEN_TS[pid] = 123.0
            hs._HANG_GUARD_DEATH_MAINTAIN.add(pid)

            second = hs.start_hang_guard(sess, next_cfg, log=lambda _m: None)

            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertTrue(second["reused"])
            self.assertEqual(disp.schedules, 1)
            self.assertIs(hs._HANG_GUARD_TOKEN[pid], first_token)
            self.assertIs(hs._HANG_GUARD_CFG[pid], next_cfg)
            self.assertEqual(hs._HANG_GUARD_DEATH_SEEN_TS[pid], 123.0)
            self.assertIn(pid, hs._HANG_GUARD_DEATH_MAINTAIN)
            hs.stop_hang_guard(pid, log=lambda _m: None)

    def test_repeated_start_rebuilds_missing_guard_timer(self) -> None:
        from app.core import hang_settings as hs
        from app.core.hang_settings import HangConfig

        class _S:
            pid = 515160
            hwnd = 0

        class _Dispatch:
            tick = None
            schedules = 0

            def list_periodics(self, _pid):
                return []

            def schedule_periodic(self, _pid, _job, _interval, fn, **_kwargs):
                self.schedules += 1
                self.tick = fn

            def cancel_periodic(self, *_args, **_kwargs):
                pass

            def invalidate(self, *_args, **_kwargs):
                pass

        sess = _S()
        disp = _Dispatch()
        with patch("app.core.safe_dispatch.get_dispatch", return_value=disp):
            first = hs.start_hang_guard(sess, HangConfig(), log=lambda _m: None)
            second = hs.start_hang_guard(sess, HangConfig(), log=lambda _m: None)
            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertNotIn("reused", second)
            self.assertEqual(disp.schedules, 2)
            hs.stop_hang_guard(sess, log=lambda _m: None)

    def test_repair_is_deferred_while_dead(self) -> None:
        from app.core import hang_settings as hs
        from app.core.hang_settings import HangConfig

        class _S:
            pid = 515156
            hwnd = 0

        sess = _S()
        hs._HANG_PID_LAST.pop(sess.pid, None)
        cfg = HangConfig(
            auto_repair=True,
            repair_below_pct=50,
            auto_vitality=False,
            enable_pickup=False,
        )
        durability = {"ready": True, "pct": 60.0, "min_pct": 40.0}
        with patch.object(
            hs, "read_equipment_durability_pct", return_value=durability
        ), patch.object(hs, "get_hang_dead_state", return_value=True), patch.object(
            hs, "_use_repair_box"
        ) as repair, patch.object(
            hs,
            "abandon_all_loot_rolls",
            return_value={"ok": True, "skipped": True, "message": "无待处理"},
        ):
            out = hs._hang_maintain_once_unlocked(
                sess, cfg, force_rv=True, log=lambda _m: None
            )
        repair.assert_not_called()
        self.assertEqual(out["repair"]["reason"], "dead_deferred")
        due, remain = hs._hang_check_due(
            sess,
            "repair_check",
            interval_s=hs.HANG_REPAIR_CHECK_INTERVAL_S,
            force=False,
        )
        self.assertFalse(due)
        self.assertLessEqual(remain, hs.HANG_RV_BUSY_BACKOFF_S)
        hs._HANG_PID_LAST.pop(sess.pid, None)

    def test_failed_repair_uses_short_backoff(self) -> None:
        from app.core import hang_settings as hs
        from app.core.hang_settings import HangConfig

        class _S:
            pid = 515157
            hwnd = 0

        sess = _S()
        hs._HANG_PID_LAST.pop(sess.pid, None)
        cfg = HangConfig(
            auto_repair=True,
            repair_below_pct=50,
            auto_vitality=False,
            enable_pickup=False,
        )
        durability = {"ready": True, "pct": 60.0, "min_pct": 40.0}
        failed = {"ok": False, "reason": "use_failed", "message": "use failed"}
        with patch.object(
            hs, "read_equipment_durability_pct", return_value=durability
        ), patch.object(hs, "get_hang_dead_state", return_value=False), patch.object(
            hs, "_use_repair_box", return_value=failed
        ), patch.object(
            hs,
            "abandon_all_loot_rolls",
            return_value={"ok": True, "skipped": True, "message": "无待处理"},
        ):
            out = hs._hang_maintain_once_unlocked(
                sess, cfg, force_rv=True, log=lambda _m: None
            )
        self.assertFalse(out["ok"])
        due, remain = hs._hang_check_due(
            sess,
            "repair_check",
            interval_s=hs.HANG_REPAIR_CHECK_INTERVAL_S,
            force=False,
        )
        self.assertFalse(due)
        self.assertLessEqual(remain, hs.HANG_RV_BUSY_BACKOFF_S)
        self.assertGreater(remain, hs.HANG_RV_BUSY_BACKOFF_S - 2.0)
        hs._HANG_PID_LAST.pop(sess.pid, None)

    def test_guard_tick_constant_lives_in_hang_business(self) -> None:
        from app.core import hang_settings as hs
        from app.core import safe_dispatch as sd

        self.assertTrue(hasattr(hs, "HANG_GUARD_TICK_S"))
        self.assertTrue(hasattr(hs, "HANG_ROLL_POLL_CACHE_S"))
        self.assertFalse(hasattr(sd, "HANG_GUARD_INTERVAL_S"))
        self.assertFalse(hasattr(sd, "HANG_ROLL_POLL_CACHE_S"))

    def test_stopped_guard_callback_cannot_run_late(self) -> None:
        """A timer callback selected before cancellation must become a no-op."""
        from app.core import hang_settings as hs
        from app.core.hang_settings import HangConfig

        class _S:
            pid = 515153
            hwnd = 0

        class _Dispatch:
            tick = None

            def schedule_periodic(self, _pid, _job, _interval, fn, **_kwargs):
                self.tick = fn

            def cancel_periodic(self, *_args, **_kwargs):
                pass

            def invalidate(self, *_args, **_kwargs):
                pass

        sess = _S()
        disp = _Dispatch()
        with patch("app.core.safe_dispatch.get_dispatch", return_value=disp), patch.object(
            hs, "_iter_active_loot_rolls", return_value=[]
        ) as poll:
            hs.start_hang_guard(sess, HangConfig(), log=lambda _m: None)
            hs.stop_hang_guard(sess, log=lambda _m: None)
            self.assertIsNotNone(disp.tick)
            disp.tick()
            poll.assert_not_called()

    def test_failed_stop_keeps_guard_running(self) -> None:
        """Do not drop automated maintenance when the game did not stop."""
        from types import SimpleNamespace

        from app.core import hang_settings as hs
        from app.core.hang_settings import HangConfig

        class _S:
            pid = 515154
            hwnd = 0

        with patch.object(hs, "resolve_empty_skill_for_action", return_value=False), patch.object(
            hs, "_stop_hang_unlocked", return_value={"ok": False, "message": "missed"}
        ), patch.object(
            hs, "probe_hang_state_mem", return_value=SimpleNamespace(ok=False, on=None)
        ), patch.object(hs, "stop_hang_guard") as stop_guard:
            out = hs.stop_hang(_S(), HangConfig(empty_skill=False), log=lambda _m: None)
            self.assertFalse(out["ok"])
            stop_guard.assert_not_called()




class HangDeadNeedTests(unittest.TestCase):
    def tearDown(self) -> None:
        from app.core import hang_settings as hs
        from app.core import remote_runtime

        for pid in (424260, 424261, 424262, 424263):
            hs._HANG_DEAD_STATE.pop(pid, None)
            remote_runtime.clear_pid_remote_state(pid)

    def test_stale_dead_state_requires_refresh(self) -> None:
        from app.core import hang_settings as hs

        pid = 424249
        hs._HANG_DEAD_STATE[pid] = {
            "dead": True,
            "ts": 0.0,
            "source": "test",
        }
        self.assertIsNone(hs.get_hang_dead_state(pid))
        hs._HANG_DEAD_STATE.pop(pid, None)

    def test_dead_poll_uses_ui_host_snapshot_tri_state(self) -> None:
        from types import SimpleNamespace

        from app.core import hang_settings as hs

        class _S:
            pid = 424260
            hwnd = 123

        for tid, expected in ((0, False), (1, True)):
            with self.subTest(tid=tid):
                hs._HANG_DEAD_STATE.pop(_S.pid, None)
                bridge = MagicMock()
                bridge.open.return_value = True
                bridge.host_snapshot.return_value = SimpleNamespace(
                    ok=True,
                    ret=1,
                    mode=68,
                    tid=tid,
                    error=None,
                    note=f"HOST_SNAPSHOT ok dead={tid}",
                )
                with patch(
                    "app.core.xajh_bridge.XajhBridge", return_value=bridge
                ), patch(
                    "app.core.remote_runtime.is_pid_scene_snapshot_stable",
                    return_value=True,
                ), patch.object(hs, "remote_call_cdecl_x86") as crt:
                    self.assertIs(
                        hs.refresh_hang_dead_state(_S(), force=True), expected
                    )
                bridge.host_snapshot.assert_called_once_with(
                    hwnd=123, timeout_ms=700
                )
                bridge.close.assert_called_once()
                crt.assert_not_called()
                self.assertEqual(
                    hs._HANG_DEAD_STATE[_S.pid]["source"], "ui_host_snapshot"
                )

    def test_dead_poll_is_unknown_during_scene_transition_or_settle(self) -> None:
        import time
        from types import SimpleNamespace

        from app.core import hang_settings as hs

        class _S:
            pid = 424261
            hwnd = 0

        samples = (
            SimpleNamespace(
                ok=True, ret=0, mode=0, tid=-1, error=None, note="no_host"
            ),
            SimpleNamespace(
                ok=True, ret=1, mode=69, tid=1, error=None, note="ok"
            ),
        )
        for sample, stable in ((samples[0], False), (samples[1], False)):
            with self.subTest(ret=sample.ret, scene=sample.mode):
                hs._HANG_DEAD_STATE[_S.pid] = {
                    "dead": True,
                    "ts": time.monotonic(),
                    "source": "previous_scene",
                }
                bridge = MagicMock()
                bridge.open.return_value = True
                bridge.host_snapshot.return_value = sample
                with patch(
                    "app.core.xajh_bridge.XajhBridge", return_value=bridge
                ), patch(
                    "app.core.remote_runtime.is_pid_scene_snapshot_stable",
                    return_value=stable,
                ):
                    self.assertIsNone(
                        hs.refresh_hang_dead_state(_S(), force=True)
                    )
                self.assertIsNone(hs._HANG_DEAD_STATE[_S.pid]["dead"])

    def test_dead_poll_rejects_snapshot_from_bridge_without_death_marker(self) -> None:
        from types import SimpleNamespace

        from app.core import hang_settings as hs

        class _S:
            pid = 424263
            hwnd = 0

        bridge = MagicMock()
        bridge.open.return_value = True
        bridge.host_snapshot.return_value = SimpleNamespace(
            ok=True,
            ret=1,
            mode=68,
            tid=0,
            error=None,
            note="HOST_SNAPSHOT ok",
        )
        with patch(
            "app.core.xajh_bridge.XajhBridge", return_value=bridge
        ), patch(
            "app.core.remote_runtime.is_pid_scene_snapshot_stable",
            return_value=True,
        ):
            self.assertIsNone(hs.refresh_hang_dead_state(_S(), force=True))
        self.assertIsNone(hs._HANG_DEAD_STATE[_S.pid]["dead"])

    def test_dead_poll_snapshot_error_is_unknown_without_fallback(self) -> None:
        from types import SimpleNamespace

        from app.core import hang_settings as hs

        class _S:
            pid = 424262
            hwnd = 0

        bridge = MagicMock()
        bridge.open.return_value = True
        bridge.host_snapshot.return_value = SimpleNamespace(
            ok=False,
            ret=None,
            mode=0,
            tid=-1,
            error="BRIDGE_SNAPSHOT_TIMEOUT",
            note="",
        )
        with patch(
            "app.core.xajh_bridge.XajhBridge", return_value=bridge
        ), patch.object(hs, "remote_call_cdecl_x86") as crt:
            self.assertIsNone(hs.refresh_hang_dead_state(_S(), force=True))
        crt.assert_not_called()
        self.assertIsNone(hs._HANG_DEAD_STATE[_S.pid]["dead"])

    def test_dead_poll_source_has_no_crt_or_export_fallback(self) -> None:
        import inspect

        from app.core import hang_settings as hs

        source = inspect.getsource(hs.refresh_hang_dead_state)
        self.assertNotIn("remote_call_cdecl_x86", source)
        self.assertNotIn("IsHostPlayerDead", source)
        self.assertNotIn("resolve_export_rva", source)
        self.assertNotIn("get_dispatch", source)

    def test_maintain_dead_need_when_pickup(self) -> None:
        from app.core import hang_settings as hs
        from app.core.hang_settings import HangConfig

        class _S:
            pid = 424250
            hwnd = 0
            module_base = 0x400000

        sess = _S()
        hs._HANG_PID_LAST.pop(424250, None)
        hs._HANG_PID_OWNER.pop(424250, None)
        hs._HANG_DEAD_STATE.pop(424250, None)
        cfg = HangConfig(enable_pickup=True, auto_repair=False, auto_vitality=False)
        with patch.object(hs, "refresh_hang_dead_state", return_value=True) as rd, patch.object(
            hs, "need_all_loot_rolls", return_value={"ok": True, "message": "需求已发 1/1", "sent": 1}
        ) as need, patch.object(hs, "abandon_all_loot_rolls") as ab:
            out = hs._hang_maintain_once_unlocked(sess, cfg, force_rv=False, log=lambda m: None)
            rd.assert_called()
            need.assert_called()
            ab.assert_not_called()
            self.assertIn("需求", out["loot"]["message"])

    def test_maintain_alive_skips_need(self) -> None:
        from app.core import hang_settings as hs
        from app.core.hang_settings import HangConfig

        class _S:
            pid = 424251
            hwnd = 0

        sess = _S()
        cfg = HangConfig(enable_pickup=True, auto_repair=False, auto_vitality=False)
        with patch.object(hs, "refresh_hang_dead_state", return_value=False), patch.object(
            hs, "need_all_loot_rolls"
        ) as need, patch.object(hs, "abandon_all_loot_rolls") as ab:
            out = hs._hang_maintain_once_unlocked(sess, cfg, force_rv=False, log=lambda m: None)
            need.assert_not_called()
            ab.assert_not_called()
            self.assertEqual(out["loot"]["reason"], "alive_auto_need")


class DungeonTargetGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.core import hang_settings as hs

        self.hs = hs
        with hs._DUNGEON_TARGET_GUARD_LOCK:
            hs._DUNGEON_TARGET_GUARD_ARMED.clear()

    def tearDown(self) -> None:
        with self.hs._DUNGEON_TARGET_GUARD_LOCK:
            self.hs._DUNGEON_TARGET_GUARD_ARMED.clear()

    @staticmethod
    def _session(pid: int = 515153):
        return type("Session", (), {"pid": pid, "hwnd": 0})()

    def test_normal_mode_never_reads_rules_or_opens_bridge(self) -> None:
        cfg = self.hs.HangConfig(
            mode=self.hs.AUTOPLAY_MODE_NORMAL, ignore_dungeon_stuck=True
        )
        with patch(
            "app.core.dungeon_target_policy.configured_dungeon_tids"
        ) as rules, patch("app.core.xajh_bridge.ensure_bridge") as bridge:
            out = self.hs._arm_dungeon_target_guard(self._session(), cfg)
        self.assertTrue(out["ok"])
        self.assertFalse(out["enabled"])
        rules.assert_not_called()
        bridge.assert_not_called()

    def test_unchecked_setting_never_reads_rules_or_opens_bridge(self) -> None:
        cfg = self.hs.HangConfig(
            mode=self.hs.AUTOPLAY_MODE_DUNGEON, ignore_dungeon_stuck=False
        )
        with patch(
            "app.core.dungeon_target_policy.configured_dungeon_tids"
        ) as rules, patch("app.core.xajh_bridge.ensure_bridge") as bridge:
            out = self.hs._arm_dungeon_target_guard(self._session(), cfg)
        self.assertTrue(out["ok"])
        self.assertFalse(out["enabled"])
        rules.assert_not_called()
        bridge.assert_not_called()

    def test_empty_effective_list_never_opens_bridge(self) -> None:
        cfg = self.hs.HangConfig(
            mode=self.hs.AUTOPLAY_MODE_DUNGEON, ignore_dungeon_stuck=True
        )
        with patch.object(self.hs, "_resolve_hang_char_id", return_value=123), patch(
            "app.core.dungeon_target_policy.configured_dungeon_tids", return_value=()
        ), patch("app.core.xajh_bridge.ensure_bridge") as bridge:
            out = self.hs._arm_dungeon_target_guard(self._session(), cfg)
        self.assertTrue(out["ok"])
        self.assertFalse(out["enabled"])
        self.assertEqual(out["reason"], "no dungeon target TIDs")
        bridge.assert_not_called()

    def test_syncs_persistent_rules_before_arming_native_guard(self) -> None:
        from types import SimpleNamespace

        cfg = self.hs.HangConfig(
            mode=self.hs.AUTOPLAY_MODE_DUNGEON, ignore_dungeon_stuck=True
        )
        bridge = MagicMock()
        bridge.dungeon_target_rules.return_value = SimpleNamespace(
            ok=True, error="", note=""
        )
        bridge.target_submit_trace.return_value = SimpleNamespace(
            ok=True, error="", note="armed"
        )
        with patch.object(self.hs, "_resolve_hang_char_id", return_value=123), patch(
            "app.core.dungeon_target_policy.configured_dungeon_tids",
            return_value=(0x13EA5, 0x18A98),
        ), patch("app.core.xajh_bridge.ensure_bridge", return_value=bridge):
            out = self.hs._arm_dungeon_target_guard(self._session(), cfg)
        self.assertTrue(out["ok"])
        self.assertTrue(out["enabled"])
        self.assertEqual(
            bridge.method_calls,
            [
                unittest.mock.call.dungeon_target_rules(mode=1, timeout_ms=1200),
                unittest.mock.call.dungeon_target_rules(mode=2, tid=0x13EA5, timeout_ms=1200),
                unittest.mock.call.dungeon_target_rules(mode=2, tid=0x18A98, timeout_ms=1200),
                unittest.mock.call.target_submit_trace(mode=3, timeout_ms=1500),
                unittest.mock.call.close(),
            ],
        )

    def test_disarm_keeps_ownership_when_bridge_is_unavailable(self) -> None:
        session = self._session()
        with self.hs._DUNGEON_TARGET_GUARD_LOCK:
            self.hs._DUNGEON_TARGET_GUARD_ARMED.add(session.pid)
        with patch("app.core.xajh_bridge.ensure_bridge", return_value=None):
            out = self.hs._disarm_dungeon_target_guard(session)
        self.assertFalse(out["ok"])
        with self.hs._DUNGEON_TARGET_GUARD_LOCK:
            self.assertIn(session.pid, self.hs._DUNGEON_TARGET_GUARD_ARMED)

    def test_start_aborts_when_requested_guard_cannot_arm(self) -> None:
        session = self._session()
        cfg = self.hs.HangConfig(
            mode=self.hs.AUTOPLAY_MODE_DUNGEON, ignore_dungeon_stuck=True
        )
        with patch.object(
            self.hs,
            "_arm_dungeon_target_guard",
            return_value={"ok": False, "enabled": False, "error": "bridge unavailable"},
        ):
            out = self.hs._start_hang_unlocked(
                session, cfg, hwnd=0, settle_s=0, maintain=False, log=lambda _m: None
            )
        self.assertFalse(out["ok"])
        self.assertIn("副本卡怪拦截未就绪", out["message"])


class DungeonSceneGateTests(unittest.TestCase):
    def setUp(self) -> None:
        hang_settings._KNOWN_DUNGEON_SCENE_IDS = None

    def tearDown(self) -> None:
        hang_settings._KNOWN_DUNGEON_SCENE_IDS = None

    def test_stable_known_dungeon_scene_allows_catalog_scene(self) -> None:
        with patch(
            "app.core.remote_runtime.get_pid_scene_snapshot",
            return_value={"scene_id": 1508},
        ), patch(
            "app.core.remote_runtime.is_pid_scene_snapshot_stable",
            return_value=True,
        ):
            allowed, scene_id, reason = hang_settings._stable_known_dungeon_scene(1)
        self.assertTrue(allowed)
        self.assertEqual(scene_id, 1508)
        self.assertEqual(reason, "")

    def test_stable_known_dungeon_scene_rejects_city_and_unstable_scene(self) -> None:
        with patch(
            "app.core.remote_runtime.get_pid_scene_snapshot",
            return_value={"scene_id": 68},
        ), patch(
            "app.core.remote_runtime.is_pid_scene_snapshot_stable",
            return_value=True,
        ):
            allowed, scene_id, reason = hang_settings._stable_known_dungeon_scene(1)
        self.assertFalse(allowed)
        self.assertEqual(scene_id, 68)
        self.assertEqual(reason, "unknown_or_non_dungeon_scene")

        with patch(
            "app.core.remote_runtime.get_pid_scene_snapshot",
            return_value={"scene_id": 1508},
        ), patch(
            "app.core.remote_runtime.is_pid_scene_snapshot_stable",
            return_value=False,
        ):
            allowed, scene_id, reason = hang_settings._stable_known_dungeon_scene(1)
        self.assertFalse(allowed)
        self.assertEqual(scene_id, 1508)
        self.assertEqual(reason, "scene_unstable")


if __name__ == "__main__":
    unittest.main()




