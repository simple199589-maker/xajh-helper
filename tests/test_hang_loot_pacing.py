# -*- coding: utf-8 -*-
"""Hang loot pacing unit tests. @author by ak"""
from __future__ import annotations

import unittest
import threading
from unittest.mock import ANY, MagicMock, patch

from app.core import hang_settings as hs


class HangLootPacingTests(unittest.TestCase):
    def setUp(self) -> None:
        hs._HANG_LOG_THROTTLE.clear()
        hs._HANG_LOOT_DECISIONS.clear()

    def test_function_start_skips_native_follow_seed_for_member(self) -> None:
        from app.core import activity_auto as aa

        sess = MagicMock(pid=4301, hwnd=0x6791)
        members = [{"name": "队长", "obj_id": 1001, "is_self": False, "is_leader": True}]
        with patch.object(
            aa,
            "resolve_cec_autoplay_rpm",
            return_value={"ok": True, "running": False, "mode": 1},
        ), patch(
            "app.core.plg_ui.host_team_role",
            return_value={"role": "member", "is_leader": False},
        ), patch(
            "app.core.team_ops.read_cecteam_members", return_value=members
        ), patch.object(
            aa, "start_autoplay_force", return_value={"ok": True, "after_running": True}
        ) as direct:
            out = aa.start_autoplay_force_follow(sess, settle_s=1.0)

        self.assertTrue(out["ok"])
        self.assertEqual(out["team_role"], "member")
        self.assertIsNone(out["follow_target_id"])
        direct.assert_called_once_with(sess, send_packet=False, log=ANY)

    def test_send_does_not_reread_result(self) -> None:
        """Abandon path: packets only; no post-wave pending re-read. @author by ak"""
        sess = MagicMock()
        sess.pid = 6161
        pending = [
            {"id0": 1, "id1": 0x02000000, "id2": 0},
            {"id0": 2, "id1": 0x02000000, "id2": 0},
        ]
        with patch.object(hs, "_note_live_va", return_value=0x1000), patch(
            "app.core.hang_settings.remote_call_cdecl_x86", return_value=0
        ) as call, patch("app.core.safe_dispatch.get_dispatch") as gd, patch(
            "app.core.state_dispatch.invalidate_states"
        ), patch.object(hs, "_refresh_empty_loot_roll_ui", return_value={"ok": True}):
            gd.return_value.invalidate = MagicMock()
            out = hs._send_loot_rolls(
                sess,
                choice=hs.LOOT_ROLL_CHOICE_PASS,
                action="loot_abandon",
                pending=pending,
            )
            self.assertTrue(out.get("ok"))
            self.assertEqual(out.get("sent"), 2)
            self.assertEqual(call.call_count, 2)
            self.assertNotIn("live_remain", out)

    def test_slow_packet_call_does_not_add_fixed_inter_packet_sleep(self) -> None:
        """A call slower than the pacing interval proceeds directly to the next row."""
        sess = MagicMock(pid=61610)
        pending = [
            {"id0": 1, "id1": 0x02000000, "id2": 0},
            {"id0": 2, "id1": 0x02000000, "id2": 0},
        ]
        with patch.object(hs, "_note_live_va", return_value=0x1000), patch(
            "app.core.hang_settings.remote_call_cdecl_x86", return_value=0
        ), patch.object(
            hs.time, "monotonic", side_effect=[10.0, 10.2, 10.2]
        ), patch.object(hs.time, "sleep") as sleep, patch(
            "app.core.safe_dispatch.get_dispatch"
        ) as gd, patch("app.core.state_dispatch.invalidate_states"), patch.object(
            hs, "_refresh_empty_loot_roll_ui", return_value={"ok": True}
        ):
            gd.return_value.invalidate = MagicMock()
            out = hs._send_loot_rolls(
                sess,
                choice=hs.LOOT_ROLL_CHOICE_PASS,
                action="loot_abandon",
                pending=pending,
            )

        self.assertTrue(out["ok"])
        self.assertEqual(out["sent"], 2)
        sleep.assert_not_called()

    def test_one_wave_covers_twenty_roll_rows(self) -> None:
        sess = MagicMock(pid=61611)
        pending = [
            {"id0": i + 1, "id1": 0x02000000, "id2": 0}
            for i in range(20)
        ]
        with patch.object(hs, "_note_live_va", return_value=0x1000), patch(
            "app.core.hang_settings.remote_call_cdecl_x86", return_value=0
        ) as call, patch.object(hs.time, "sleep"), patch(
            "app.core.safe_dispatch.get_dispatch"
        ) as gd, patch("app.core.state_dispatch.invalidate_states"), patch.object(
            hs, "_refresh_empty_loot_roll_ui", return_value={"ok": True}
        ):
            gd.return_value.invalidate = MagicMock()
            out = hs._send_loot_rolls(
                sess,
                choice=hs.LOOT_ROLL_CHOICE_PASS,
                action="loot_abandon",
                pending=pending,
            )

        self.assertEqual(out["sent"], 20)
        self.assertEqual(out["remain"], 0)
        self.assertEqual(out["burst_cap"], 24)
        self.assertEqual(call.call_count, 20)

    def test_send_marks_local_decided_memory(self) -> None:
        """Packet path mirrors native UI handler entry+0x1C decided=1. @author by ak"""
        sess = MagicMock()
        sess.pid = 6162
        pending = [
            {
                "index": 0,
                "entry": 0x123450,
                "id0": 11,
                "id1": 0x02000000,
                "id2": 0,
                "time": 100,
                "time_max": 60000,
            }
        ]
        with patch.object(hs, "_note_live_va", return_value=0x1000), patch(
            "app.core.hang_settings.remote_call_cdecl_x86", return_value=0
        ), patch.object(hs, "_loot_roll_entry_still_active", return_value=True), patch.object(
            hs, "_rpm_u8", side_effect=[0, 1]
        ) as rpm, patch.object(
            hs, "_wpm_u8", return_value=True
        ) as wpm, patch("app.core.safe_dispatch.get_dispatch") as gd, patch(
            "app.core.state_dispatch.invalidate_states"
        ), patch.object(hs, "_refresh_empty_loot_roll_ui", return_value={"ok": True}):
            gd.return_value.invalidate = MagicMock()
            out = hs._send_loot_rolls(
                sess,
                choice=hs.LOOT_ROLL_CHOICE_PASS,
                action="loot_abandon",
                pending=pending,
            )
            self.assertTrue(out.get("ok"))
            self.assertEqual(out.get("sent"), 1)
            wpm.assert_called_once_with(sess, 0x123450 + hs.LOOT_ENTRY_DECIDED_OFF, 1)
            self.assertGreaterEqual(rpm.call_count, 1)
            self.assertTrue(out["details"][0].get("local_decided"))

    def test_local_decided_prunes_next_active_poll(self) -> None:
        """Rows with decided byte set are no longer returned by active roll scan. @author by ak"""
        sess = MagicMock()
        sess.pid = 6163
        hd = 0x200000
        side = 0x210000
        mgr = 0x220000
        arr = 0x230000
        entry = 0x240000
        raw = bytearray(0x20)
        raw[hs.LOOT_ENTRY_ID0_OFF : hs.LOOT_ENTRY_ID0_OFF + 4] = (77).to_bytes(4, "little")
        raw[hs.LOOT_ENTRY_ID1_OFF : hs.LOOT_ENTRY_ID1_OFF + 4] = (
            hs.LOOT_ROLL_ID1_FLAG
        ).to_bytes(4, "little")
        raw[hs.LOOT_ENTRY_TIME_OFF : hs.LOOT_ENTRY_TIME_OFF + 4] = (10).to_bytes(4, "little")
        raw[hs.LOOT_ENTRY_TIME_MAX_OFF : hs.LOOT_ENTRY_TIME_MAX_OFF + 4] = (
            60000
        ).to_bytes(4, "little")
        raw[hs.LOOT_ENTRY_DECIDED_OFF] = 1

        def rpm_u32(_sess, addr):
            return {
                hd + hs.HOST_DATA_LOOT_SIDE_OFF: side,
                side + hs.LOOT_MGR_PTR_OFF: mgr,
                mgr + hs.LOOT_MGR_COUNT_OFF: 1,
                mgr + hs.LOOT_MGR_ARR_OFF: arr,
                arr: entry,
            }.get(addr, 0)

        with patch.object(hs, "_resolve_host_data", return_value=hd), patch.object(
            hs, "_rpm_u32", side_effect=rpm_u32
        ), patch("app.core.hang_settings.remote_read_bytes", return_value=bytes(raw)):
            rows = hs._iter_active_loot_rolls(sess)
            self.assertEqual(rows, [])

    def test_empty_roll_dialog_is_hidden_by_local_state(self) -> None:
        """Only an empty Roll manager may clear Win_LootRoll IsShow. @author by ak"""
        sess = MagicMock()
        sess.pid = 6164
        hit = MagicMock(shown=True, dlg_ptr=0x123400)
        with patch.object(hs, "_settle_expired_loot_rolls_local", return_value={"ok": True, "failed": 0}), patch.object(
            hs, "_iter_active_loot_rolls", return_value=[]
        ), patch(
            "app.core.plg_ui.query_dlg_show", return_value=hit
        ), patch.object(hs, "_rpm_u8", side_effect=[1, 0]), patch.object(
            hs, "_wpm_u8", return_value=True
        ) as wpm:
            out = hs._refresh_empty_loot_roll_ui(sess)
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("reason"), "hidden")
        wpm.assert_called_once_with(
            sess, 0x123400 + hs.LOOT_ROLL_DLG_ISSHOW_OFF, 0
        )

    def test_expired_undecided_rows_are_settled_locally(self) -> None:
        sess = MagicMock()
        sess.pid = 6165
        hd, side, mgr, arr, entry = 0x200000, 0x210000, 0x220000, 0x230000, 0x240000
        raw = bytearray(0x20)
        raw[hs.LOOT_ENTRY_ID0_OFF : hs.LOOT_ENTRY_ID0_OFF + 4] = (77).to_bytes(4, "little")
        raw[hs.LOOT_ENTRY_ID1_OFF : hs.LOOT_ENTRY_ID1_OFF + 4] = hs.LOOT_ROLL_ID1_FLAG.to_bytes(4, "little")
        raw[hs.LOOT_ENTRY_TIME_OFF : hs.LOOT_ENTRY_TIME_OFF + 4] = (60000).to_bytes(4, "little")
        raw[hs.LOOT_ENTRY_TIME_MAX_OFF : hs.LOOT_ENTRY_TIME_MAX_OFF + 4] = (60000).to_bytes(4, "little")

        def rpm_u32(_sess, addr):
            return {
                hd + hs.HOST_DATA_LOOT_SIDE_OFF: side,
                side + hs.LOOT_MGR_PTR_OFF: mgr,
                mgr + hs.LOOT_MGR_COUNT_OFF: 1,
                mgr + hs.LOOT_MGR_ARR_OFF: arr,
                arr: entry,
            }.get(addr, 0)

        with patch.object(hs, "_resolve_host_data", return_value=hd), patch.object(
            hs, "_rpm_u32", side_effect=rpm_u32
        ), patch("app.core.hang_settings.remote_read_bytes", return_value=bytes(raw)), patch.object(
            hs, "_wpm_u8", return_value=True
        ) as wpm:
            out = hs._settle_expired_loot_rolls_local(sess)
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("candidates"), 1)
        self.assertEqual(out.get("written"), 1)
        wpm.assert_called_once_with(sess, entry + hs.LOOT_ENTRY_DECIDED_OFF, 1)

    def test_active_roll_dialog_is_never_hidden(self) -> None:
        sess = MagicMock()
        with patch.object(hs, "_settle_expired_loot_rolls_local", return_value={"ok": True, "failed": 0}), patch.object(
            hs, "_iter_active_loot_rolls", return_value=[{"entry": 1}]
        ), patch.object(
            hs, "_wpm_u8"
        ) as wpm:
            out = hs._refresh_empty_loot_roll_ui(sess)
        self.assertTrue(out.get("skipped"))
        self.assertEqual(out.get("reason"), "active_remaining")
        wpm.assert_not_called()

    def test_roll_arriving_during_ui_refresh_is_not_hidden(self) -> None:
        sess = MagicMock()
        sess.pid = 6166
        hit = MagicMock(shown=True, dlg_ptr=0x123400)
        with patch.object(
            hs, "_settle_expired_loot_rolls_local", return_value={"ok": True, "failed": 0}
        ), patch.object(
            hs, "_iter_active_loot_rolls", side_effect=[[], [{"entry": 0x222222}]]
        ), patch("app.core.plg_ui.query_dlg_show", return_value=hit), patch.object(
            hs, "_rpm_u8", return_value=1
        ), patch.object(hs, "_wpm_u8") as wpm:
            out = hs._refresh_empty_loot_roll_ui(sess)
        self.assertTrue(out.get("skipped"))
        self.assertEqual(out.get("reason"), "active_arrived")
        wpm.assert_not_called()

    def test_concurrent_abandon_sends_one_packet_wave(self) -> None:
        """The per-pid hang gate prevents two Roll waves from racing."""
        class _S:
            pid = 6167

        sess = _S()
        hs._HANG_PID_LAST.pop(sess.pid, None)
        hs._HANG_PID_OWNER.pop(sess.pid, None)
        pending = [{"entry": 0x123450, "id0": 11, "id1": 0x02000000, "id2": 0, "time": 1, "time_max": 100}]
        entered = threading.Event()
        release = threading.Event()
        results = []

        def send(*_args, **_kwargs):
            entered.set()
            release.wait(1.0)
            return 0

        def first():
            results.append(hs.abandon_all_loot_rolls(sess, log=lambda _m: None))

        with patch.object(hs, "_iter_active_loot_rolls", return_value=pending), patch.object(
            hs, "_note_live_va", return_value=0x1000
        ), patch("app.core.hang_settings.remote_call_cdecl_x86", side_effect=send) as call, patch.object(
            hs, "_loot_roll_entry_still_active", return_value=True
        ), patch.object(
            hs, "_wpm_u8", return_value=True
        ), patch.object(hs, "_rpm_u8", return_value=1), patch.object(
            hs, "_refresh_empty_loot_roll_ui", return_value={"ok": True}
        ), patch("app.core.safe_dispatch.get_dispatch") as gd, patch(
            "app.core.state_dispatch.invalidate_states"
        ):
            gd.return_value.invalidate = MagicMock()
            th = threading.Thread(target=first)
            th.start()
            self.assertTrue(entered.wait(1.0))
            second = hs.abandon_all_loot_rolls(sess, log=lambda _m: None)
            release.set()
            th.join(1.0)

        self.assertFalse(th.is_alive())
        self.assertEqual(call.call_count, 1)
        self.assertTrue(second.get("busy"))
        self.assertEqual(len(results), 1)

    def test_stale_pending_snapshot_cannot_send_second_choice(self) -> None:
        """A later Need/Pass caller with stale rows must not reverse a decision."""
        sess = MagicMock()
        sess.pid = 6168
        pending = [
            {
                "entry": 0x123450,
                "id0": 11,
                "id1": 0x02000000,
                "id2": 0,
                "time": 1,
                "time_max": 100,
            }
        ]
        with patch.object(hs, "_note_live_va", return_value=0x1000), patch(
            "app.core.hang_settings.remote_call_cdecl_x86", return_value=0
        ) as call, patch.object(hs, "_loot_roll_entry_still_active", return_value=True), patch.object(
            hs, "_wpm_u8", return_value=True
        ), patch.object(
            hs, "_rpm_u8", return_value=1
        ), patch.object(hs, "_refresh_empty_loot_roll_ui", return_value={"ok": True}), patch(
            "app.core.safe_dispatch.get_dispatch"
        ) as gd, patch("app.core.state_dispatch.invalidate_states"):
            gd.return_value.invalidate = MagicMock()
            first = hs._send_loot_rolls(
                sess,
                choice=hs.LOOT_ROLL_CHOICE_PASS,
                action="loot_abandon",
                pending=list(pending),
            )
            second = hs._send_loot_rolls(
                sess,
                choice=hs.LOOT_ROLL_CHOICE_NEED,
                action="loot_need",
                pending=list(pending),
            )
        self.assertTrue(first.get("ok"))
        self.assertTrue(second.get("skipped"))
        self.assertEqual(second.get("reason"), "already_decided")
        self.assertEqual(call.call_count, 1)


    def test_stale_entry_is_not_sent(self) -> None:
        """Recycled/expired manager rows must not receive CRT packets. @author by ak"""
        sess = MagicMock()
        sess.pid = 6169
        pending = [
            {
                "entry": 0x123450,
                "id0": 11,
                "id1": 0x02000000,
                "id2": 0,
                "time": 1,
                "time_max": 100,
            }
        ]
        with patch.object(hs, "_note_live_va", return_value=0x1000), patch(
            "app.core.hang_settings.remote_call_cdecl_x86", return_value=0
        ) as call, patch.object(
            hs, "_loot_roll_entry_still_active", return_value=False
        ), patch("app.core.safe_dispatch.get_dispatch") as gd, patch(
            "app.core.state_dispatch.invalidate_states"
        ), patch.object(hs, "_refresh_empty_loot_roll_ui", return_value={"ok": True}):
            gd.return_value.invalidate = MagicMock()
            out = hs._send_loot_rolls(
                sess,
                choice=hs.LOOT_ROLL_CHOICE_PASS,
                action="loot_abandon",
                pending=pending,
            )
        self.assertFalse(out.get("ok"))
        self.assertEqual(out.get("sent"), 0)
        self.assertEqual(out.get("skipped_stale"), 1)
        call.assert_not_called()

    def test_empty_skill_stop_sends_server_packet(self) -> None:
        sess = MagicMock(pid=4243)
        cfg = hs.HangConfig(empty_skill=True)
        with patch.object(hs, "stop_wanzi_packet_hang", return_value={"ok": True}), patch.object(
            hs, "_wait_hang_scene_stable", return_value=(True, "")
        ), patch.object(hs, "send_raw_c2s_packet", return_value=1) as send:
            out = hs.stop_hang(sess, cfg)
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("via"), "raw_c2s_packet")
        send.assert_called_once_with(sess, bytes.fromhex("160002"), log=ANY)

    def test_dungeon_mode_stop_sends_server_packet(self) -> None:
        sess = MagicMock(pid=4253)
        cfg = hs.HangConfig(mode=hs.AUTOPLAY_MODE_DUNGEON, empty_skill=False)
        with patch.object(hs, "stop_wanzi_packet_hang", return_value={"ok": True}), patch.object(
            hs, "_wait_hang_scene_stable", return_value=(True, "")
        ), patch.object(hs, "send_raw_c2s_packet", return_value=1) as send:
            out = hs.stop_hang(sess, cfg)

        self.assertTrue(out["ok"])
        send.assert_called_once_with(sess, bytes.fromhex("160002"), log=ANY)

    def test_empty_skill_restart_sends_stop_then_start_packets(self) -> None:
        sess = MagicMock(pid=4244, hwnd=0x1234)
        cfg = hs.HangConfig(empty_skill=True)
        on = MagicMock(ok=True, on=True)
        live = MagicMock(running=True)
        live.to_dict.return_value = {"running": True}
        with patch.object(hs, "probe_hang_state_mem", return_value=on), patch.object(
            hs, "_wait_hang_scene_stable", return_value=(True, "")
        ), patch.object(hs, "send_raw_c2s_packet", return_value=1) as send, patch.object(
            hs, "read_hang_live", return_value=live
        ), patch.object(
            hs, "start_hang_guard", return_value={"ok": True}
        ), patch.object(
            hs, "_arm_dungeon_target_guard", return_value={"ok": True, "enabled": True}
        ), patch.object(hs.time, "sleep"):
            out = hs._start_hang_unlocked(sess, cfg, hwnd=0x1234)

        self.assertTrue(out["ok"])
        self.assertEqual(out["via"], "raw_c2s_packet")
        self.assertEqual(
            [call.args[1] for call in send.call_args_list],
            [bytes.fromhex("160002"), bytes.fromhex("1500")],
        )

    def test_empty_skill_restart_aborts_when_pre_stop_fails(self) -> None:
        sess = MagicMock(pid=4245, hwnd=0x1234)
        cfg = hs.HangConfig(empty_skill=True)
        on = MagicMock(ok=True, on=True)
        with patch.object(hs, "probe_hang_state_mem", return_value=on), patch.object(
            hs,
            "_stop_hang_force_unlocked",
            return_value={"ok": False, "message": "still on"},
        ):
            out = hs._start_hang_unlocked(sess, cfg, hwnd=0x1234)

        self.assertFalse(out["ok"])
        self.assertIn("重启前关挂机失败", out["message"])

    def test_empty_skill_ui_start_dispatches_native_button_handler(self) -> None:
        from app.core import activity_auto as aa

        sess = MagicMock(pid=4246, hwnd=0x5678)
        result = MagicMock(ok=True, error=None, note="dispatched", ret=0)
        result.to_dict.return_value = {"ok": True, "ret": 0}
        bridge = MagicMock()
        bridge.autoplay_start_bypass.return_value = result
        with patch.object(
            aa,
            "resolve_cec_autoplay_rpm",
            side_effect=[{"ok": True, "running": False}, {"ok": True, "running": True}],
        ), patch.object(aa, "ensure_dlg_shown", return_value=0x1000), patch.object(
            aa, "aui_get_dlg_item", return_value=0x2000
        ), patch(
            "app.core.xajh_bridge.ensure_bridge", return_value=bridge
        ), patch.object(
            aa, "is_dlg_show", return_value=False
        ), patch.object(aa.time, "sleep"):
            out = aa.start_autoplay_via_settings_ui(sess, settle_s=1.0)

        self.assertTrue(out["ok"])
        self.assertEqual(out["via"], "autoplay_settings_ui_bypass")
        bridge.autoplay_start_bypass.assert_called_once_with(
            hwnd=0x5678, timeout_ms=5000
        )
        bridge.close.assert_called_once()

    def test_runner_ui_start_uses_bridge_when_session_has_no_hwnd(self) -> None:
        from app.core import activity_auto as aa

        sess = MagicMock(pid=4249, hwnd=0)
        result = MagicMock(ok=True, error=None, note="dispatched", ret=0)
        result.to_dict.return_value = {"ok": True, "ret": 0}
        bridge = MagicMock()
        bridge.autoplay_start_bypass.return_value = result
        with patch.object(
            aa,
            "resolve_cec_autoplay_rpm",
            side_effect=[{"ok": True, "running": False}, {"ok": True, "running": True}],
        ), patch.object(aa, "ensure_dlg_shown", return_value=0x1000), patch.object(
            aa, "aui_get_dlg_item", return_value=0x2000
        ), patch(
            "app.core.xajh_bridge.ensure_bridge", return_value=bridge
        ) as ensure, patch.object(
            aa, "is_dlg_show", return_value=False
        ), patch.object(aa.time, "sleep"):
            out = aa.start_autoplay_via_settings_ui(sess, settle_s=1.0)

        self.assertTrue(out["ok"])
        ensure.assert_called_once_with(
            4249, log=ANY, inject_if_needed=True, hwnd=None
        )
        bridge.autoplay_start_bypass.assert_called_once_with(
            hwnd=None, timeout_ms=5000
        )

    def test_dungeon_leader_seeds_member_follow_target_after_start(self) -> None:
        from app.core import activity_auto as aa

        sess = MagicMock(pid=4250, hwnd=0x6789)
        started = MagicMock(ok=True, error=None, note="dispatched", ret=0)
        started.to_dict.return_value = {"ok": True, "ret": 0}
        seeded = MagicMock(ok=True, error=None, note="seeded", ret=1)
        seeded.to_dict.return_value = {"ok": True, "ret": 1}
        bridge = MagicMock()
        bridge.autoplay_start_bypass.return_value = started
        bridge.autoplay_seed_follow.return_value = seeded
        members = [
            {"name": "角色甲", "obj_id": 1001, "is_self": True},
            {"name": "队员甲", "obj_id": 2002, "is_self": False},
        ]
        with patch.object(
            aa,
            "resolve_cec_autoplay_rpm",
            side_effect=[
                {"ok": True, "running": False, "mode": 1},
                {"ok": True, "running": True, "mode": 1},
            ],
        ), patch.object(aa, "ensure_dlg_shown", return_value=0x1000), patch.object(
            aa, "aui_get_dlg_item", return_value=0x2000
        ), patch(
            "app.core.plg_ui.host_team_role",
            return_value={"role": "leader", "is_leader": True},
        ), patch(
            "app.core.team_ops.read_cecteam_members", return_value=members
        ), patch(
            "app.core.xajh_bridge.ensure_bridge", return_value=bridge
        ), patch.object(
            aa, "is_dlg_show", return_value=False
        ), patch.object(aa.time, "sleep"):
            out = aa.start_autoplay_via_settings_ui(sess, settle_s=1.0)

        self.assertTrue(out["ok"])
        self.assertEqual(out["follow_target_id"], 2002)
        self.assertEqual(out["follow_target_name"], "队员甲")
        bridge.autoplay_seed_follow.assert_called_once_with(
            2002, hwnd=0x6789, timeout_ms=4000
        )
        bridge.close.assert_called_once()

    def test_function_start_seeds_dungeon_leader_follow_target(self) -> None:
        from app.core import activity_auto as aa

        sess = MagicMock(pid=4252, hwnd=0x6791)
        seeded = MagicMock(ok=True, error=None, note="seeded", ret=1)
        seeded.to_dict.return_value = {"ok": True, "ret": 1}
        bridge = MagicMock()
        bridge.autoplay_seed_follow.return_value = seeded
        members = [
            {"name": "队长", "obj_id": 1001, "is_self": True},
            {"name": "队员甲", "obj_id": 2002, "is_self": False},
        ]
        with patch.object(
            aa,
            "resolve_cec_autoplay_rpm",
            return_value={"ok": True, "running": False, "mode": 1},
        ), patch(
            "app.core.plg_ui.host_team_role",
            return_value={"role": "leader", "is_leader": True},
        ), patch(
            "app.core.team_ops.read_cecteam_members", return_value=members
        ), patch.object(
            aa,
            "start_autoplay_force",
            return_value={"ok": True, "after_running": True},
        ) as direct, patch(
            "app.core.xajh_bridge.ensure_bridge", return_value=bridge
        ):
            out = aa.start_autoplay_force_follow(sess, settle_s=1.0)

        self.assertTrue(out["ok"])
        self.assertEqual(out["via"], "StartAutoPlay")
        self.assertEqual(out["follow_target_id"], 2002)
        direct.assert_called_once_with(sess, send_packet=False, log=ANY)
        bridge.autoplay_seed_follow.assert_called_once_with(
            2002, hwnd=0x6791, timeout_ms=4000
        )
        bridge.close.assert_called_once()

    def test_dungeon_leader_without_member_fails_before_start(self) -> None:
        from app.core import activity_auto as aa

        sess = MagicMock(pid=4251, hwnd=0x6790)
        with patch.object(
            aa,
            "resolve_cec_autoplay_rpm",
            return_value={"ok": True, "running": False, "mode": 1},
        ), patch(
            "app.core.plg_ui.host_team_role",
            return_value={"role": "leader", "is_leader": True},
        ), patch(
            "app.core.team_ops.read_cecteam_members",
            return_value=[{"name": "角色甲", "obj_id": 1001, "is_self": True}],
        ), patch.object(aa, "ensure_dlg_shown") as show:
            out = aa.start_autoplay_via_settings_ui(sess)

        self.assertFalse(out["ok"])
        self.assertIn("没有可用的在线队员", out["error"])
        show.assert_not_called()

    def test_dungeon_leader_with_skills_sends_server_packet(self) -> None:
        sess = MagicMock(pid=4248, hwnd=0x1234)
        cfg = hs.HangConfig(mode=hs.AUTOPLAY_MODE_DUNGEON, empty_skill=False)
        off = MagicMock(ok=True, on=False)
        live = MagicMock(running=True)
        live.to_dict.return_value = {"running": True}
        with patch.object(hs, "probe_hang_state_mem", return_value=off), patch.object(
            hs, "_wait_hang_scene_stable", return_value=(True, "")
        ), patch.object(hs, "send_raw_c2s_packet", return_value=1) as send, patch.object(
            hs, "read_hang_live", return_value=live
        ), patch.object(
            hs, "start_hang_guard", return_value={"ok": True}
        ), patch.object(
            hs, "_arm_dungeon_target_guard", return_value={"ok": True, "enabled": True}
        ), patch.object(hs.time, "sleep"):
            out = hs._start_hang_unlocked(
                sess, cfg, hwnd=0x1234, force_path=True
            )

        self.assertTrue(out["ok"])
        self.assertEqual(out["via"], "raw_c2s_packet")
        send.assert_called_once_with(sess, bytes.fromhex("1500"), log=ANY)

    def test_force_path_selection_uses_empty_style_or_dungeon_leader(self) -> None:
        sess = MagicMock(pid=4254)
        empty = hs.HangConfig(mode=hs.AUTOPLAY_MODE_NORMAL, empty_skill=True)
        dungeon = hs.HangConfig(
            mode=hs.AUTOPLAY_MODE_DUNGEON, empty_skill=False
        )
        normal = hs.HangConfig(mode=hs.AUTOPLAY_MODE_NORMAL, empty_skill=False)
        youfeng = hs.HangConfig(
            mode=hs.AUTOPLAY_MODE_NORMAL,
            empty_skill=False,
            youfeng_hang=True,
        )

        with patch.object(
            hs,
            "read_autoplay_skills",
            return_value={"ok": True, "gate_b": 0x12602},
        ), patch("app.core.plg_ui.host_team_role") as role:
            self.assertTrue(hs._start_uses_force_function(sess, empty))
            self.assertFalse(hs._start_uses_force_function(sess, youfeng))
            self.assertFalse(hs._start_uses_force_function(sess, normal))
            role.assert_not_called()

        with patch.object(
            hs,
            "read_autoplay_skills",
            return_value={"ok": True, "gate_b": 0},
        ), patch("app.core.plg_ui.host_team_role") as role:
            self.assertTrue(hs._start_uses_force_function(sess, normal))
            role.assert_not_called()

        with patch.object(
            hs,
            "read_autoplay_skills",
            return_value={"ok": True, "gate_b": 0x12602},
        ), patch(
            "app.core.plg_ui.host_team_role",
            return_value={"role": "leader", "is_leader": True},
        ):
            self.assertTrue(hs._start_uses_force_function(sess, dungeon))
        with patch.object(
            hs,
            "read_autoplay_skills",
            return_value={"ok": True, "gate_b": 0x12602},
        ), patch(
            "app.core.plg_ui.host_team_role",
            return_value={"role": "member", "is_leader": False},
        ):
            self.assertFalse(hs._start_uses_force_function(sess, dungeon))

    def test_empty_skill_ui_start_does_not_fall_back_to_direct_function(self) -> None:
        from app.core import activity_auto as aa

        sess = MagicMock(pid=4247, hwnd=0x5678)
        with patch.object(
            aa,
            "resolve_cec_autoplay_rpm",
            return_value={"ok": True, "running": False},
        ), patch.object(aa, "ensure_dlg_shown", return_value=0), patch.object(
            aa, "start_autoplay_force"
        ) as direct:
            out = aa.start_autoplay_via_settings_ui(sess)

        self.assertFalse(out["ok"])
        self.assertIn("Win_AutoPlayFrame", out["error"])
        direct.assert_not_called()


if __name__ == "__main__":
    unittest.main()
