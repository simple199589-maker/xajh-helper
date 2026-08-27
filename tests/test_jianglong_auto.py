# -*- coding: utf-8 -*-
from __future__ import annotations

import struct
import threading
import time
import unittest

from app.core.jianglong_auto import (
    JIANG_LONG_PACKET_SIZE,
    JIANG_LONG_HEADER,
    JIANG_LONG_CONFIG_MID,
    JIANG_LONG_TAIL,
    JianglongRotationRunner,
    jianglong_entry_settle_remaining,
    begin_local_jianglong_collection,
    build_jianglong_packet,
    clear_participants,
    drop_participant,
    end_local_jianglong_collection,
    eligible_participants,
    local_jianglong_collection_active,
    participant_snapshot,
    report_participant,
)
from app.core.task_sync import (
    ACTION_JIANGLONG_CAST,
    ACTION_JIANGLONG_QUERY,
    VALID_ACTIONS,
)
from app.core.hang_settings import (
    DEFAULT_JIANGLONG_HANG,
    HangConfig,
    get_hang_config,
    save_hang_disk_from_config,
    write_hang_config_to_settings,
)


class TestEntrySettle(unittest.TestCase):
    def test_wuzun_waits_ten_seconds_after_stability(self):
        self.assertEqual(
            jianglong_entry_settle_remaining(1541, 100.0, now=100.0), 10.0
        )
        self.assertEqual(
            jianglong_entry_settle_remaining(1255, 100.0, now=110.0), 0.0
        )

    def test_non_wuzun_does_not_arm_entry_gate(self):
        self.assertIsNone(
            jianglong_entry_settle_remaining(1001, 100.0, now=100.0)
        )

    def test_explicit_test_mode_bypasses_entry_wait(self):
        self.assertEqual(
            jianglong_entry_settle_remaining(
                1541, 100.0, now=100.0, test_mode=True
            ),
            0.0,
        )

class TestBuildPacket(unittest.TestCase):
    def test_packet_size_and_tail(self):
        pos = (100.5, 200.25, 300.75)
        pkt = build_jianglong_packet(pos, 0x7E3)
        self.assertEqual(len(pkt), JIANG_LONG_PACKET_SIZE)
        self.assertTrue(pkt.startswith(JIANG_LONG_HEADER + struct.pack("<I", 0x7E3) + JIANG_LONG_CONFIG_MID))
        self.assertTrue(pkt.endswith(JIANG_LONG_TAIL))

    def test_packet_xyz_bytes(self):
        pos = (1.0, -2.5, 3.0)
        pkt = build_jianglong_packet(pos, 0x7E4)
        xyz_off = len(JIANG_LONG_HEADER) + 4 + len(JIANG_LONG_CONFIG_MID)
        self.assertEqual(struct.unpack("<fff", pkt[xyz_off: xyz_off + 12]), pos)
        self.assertEqual(struct.unpack_from("<I", pkt, len(JIANG_LONG_HEADER))[0], 0x7E4)

    def test_rejects_invalid_runtime_config(self):
        with self.assertRaises(ValueError):
            build_jianglong_packet((1.0, 2.0, 3.0), 0)

    def test_round_trip_user_sample(self):
        sample = (
            "1F000726010000052301FFFFFF00000000E307000000000001"
            "807B16C268F77242000038C201"
        )
        raw = bytes.fromhex(sample)
        self.assertEqual(len(raw), JIANG_LONG_PACKET_SIZE)
        self.assertTrue(raw.startswith(JIANG_LONG_HEADER))
        self.assertTrue(raw.endswith(JIANG_LONG_TAIL))

    def test_cast_refuses_when_runtime_resolution_fails(self):
        from unittest import mock

        import app.core.jianglong_auto as jl

        with mock.patch(
            "app.core.jianglong_runtime.resolve_jianglong_runtime_config",
            return_value={"ok": False, "error": "not found"},
        ), mock.patch.object(jl, "send_jianglong_packet") as send:
            result = jl.cast_jianglong_once(7777, log=lambda _m: None)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not found")
        send.assert_not_called()

    def test_cast_resolves_skill_then_uses_native_client_path(self):
        from unittest import mock

        import app.core.jianglong_auto as jl
        from app.core.xajh_bridge import BridgeResult

        bridge = mock.MagicMock()
        bridge.cast_jianglong_native.return_value = BridgeResult(
            ok=False, cmd=18, ret=103, note="CAST_SKILL accepted"
        )
        with mock.patch(
            "app.core.jianglong_runtime.resolve_jianglong_runtime_config",
            return_value={"ok": True, "skill_id": 0x2305},
        ), mock.patch.object(jl, "press_block_x", return_value=True) as interrupt, mock.patch(
            "app.core.xajh_bridge.ensure_bridge", return_value=bridge
        ), mock.patch.object(jl, "send_jianglong_packet") as send, mock.patch.object(
            jl, "read_host_pos_xyz"
        ) as read_pos:
            result = jl.cast_jianglong_once(7777, hwnd=0, log=lambda _m: None)

        interrupt.assert_called_once()
        bridge.cast_jianglong_native.assert_called_once_with(0x2305, hwnd=None)
        bridge.close.assert_called_once()
        send.assert_not_called()
        read_pos.assert_not_called()
        self.assertTrue(result.get("ok"))
        self.assertTrue(result.get("interrupt_ok"))
        self.assertEqual(result.get("skill_id"), 0x2305)
        self.assertEqual(result.get("ret"), 103)

class TestParticipants(unittest.TestCase):
    def setUp(self):
        clear_participants()
        end_local_jianglong_collection()

    def tearDown(self):
        clear_participants()
        end_local_jianglong_collection()

    def test_local_collection_is_explicit_and_closeable(self):
        self.assertFalse(local_jianglong_collection_active())
        begin_local_jianglong_collection(1001, window_s=1.0)
        self.assertTrue(local_jianglong_collection_active())
        end_local_jianglong_collection(1001)
        self.assertFalse(local_jianglong_collection_active())


    def test_report_and_snapshot(self):
        report_participant(
            1001,
            name="甲",
            role="master",
            enabled=True,
            in_wuzun=True,
            hang_running=True,
        )
        report_participant(
            1002,
            name="乙",
            role="slave",
            enabled=True,
            in_wuzun=True,
            hang_running=True,
        )
        snap = participant_snapshot()
        pids = [int(p["pid"]) for p in snap]
        self.assertIn(1001, pids)
        self.assertIn(1002, pids)

    def test_eligible_filters(self):
        report_participant(
            1001,
            name="甲",
            role="master",
            enabled=True,
            in_wuzun=True,
            hang_running=True,
        )
        # 不在武尊堂
        report_participant(
            1002,
            name="乙",
            role="slave",
            enabled=True,
            in_wuzun=False,
            hang_running=True,
        )
        # 未勾选
        report_participant(
            1003,
            name="丙",
            role="slave",
            enabled=False,
            in_wuzun=True,
            hang_running=True,
        )
        elig = eligible_participants()
        pids = [int(p["pid"]) for p in elig]
        self.assertEqual(pids, [1001])

    def test_drop(self):
        report_participant(
            1001, name="甲", role="master", enabled=True,
            in_wuzun=True, hang_running=True,
        )
        drop_participant(1001)
        self.assertNotIn(1001, [int(p["pid"]) for p in participant_snapshot()])


class TestRotationRunner(unittest.TestCase):
    def test_round_robin_interval(self):
        cast_log: list[int] = []

        def roster_fn():
            return [
                {"pid": 1001, "name": "甲", "hwnd": 0},
                {"pid": 1002, "name": "乙", "hwnd": 0},
            ]

        def cast_fn(target):
            cast_log.append(int(target["pid"]))

        runner = JianglongRotationRunner(
            1001,
            roster_fn=roster_fn,
            cast_fn=cast_fn,
            cooldown_s=0.2,
            cooldown_guard_s=0.0,
            min_interval_s=0.05,
            log=lambda _m: None,
        )
        runner.start()
        time.sleep(0.35)
        runner.stop()
        # interval = cooldown / 2 = 0.1s; 0.35s 窗口应至少 3 轮
        self.assertGreaterEqual(len(cast_log), 3)
        self.assertIn(1001, cast_log)
        self.assertIn(1002, cast_log)
        self.assertEqual(cast_log[0], 1001)
        self.assertEqual(cast_log[1], 1002)

    def test_round_robin_mixed_local_remote(self):
        """local(带 pid) + 跨机(pid=0 带 name) 混合轮转。@author by ak"""
        cast_log: list[tuple[int, str]] = []

        def roster_fn():
            return [
                {"pid": 1001, "name": "本主", "hwnd": 0},
                {"pid": 0, "name": "甲", "hwnd": 0, "remote": True},
                {"pid": 0, "name": "乙", "hwnd": 0, "remote": True},
            ]

        def cast_fn(target):
            cast_log.append((int(target.get("pid") or 0), str(target.get("name") or "")))

        runner = JianglongRotationRunner(
            1001,
            roster_fn=roster_fn,
            cast_fn=cast_fn,
            cooldown_s=0.45,
            cooldown_guard_s=0.0,
            min_interval_s=0.05,
            log=lambda _m: None,
        )
        runner.start()
        time.sleep(0.95)
        runner.stop()
        names = [n for _, n in cast_log]
        # interval = 0.45/3 = 0.15s；0.95s 窗口应多轮覆盖三目标
        self.assertGreaterEqual(len(cast_log), 5)
        self.assertIn("本主", names)
        self.assertIn("甲", names)
        self.assertIn("乙", names)
        # local 条目先于 remote 条目
        local_first = next(i for i, (_, n) in enumerate(cast_log) if n == "本主")
        remote_first = min(
            i for i, (_, n) in enumerate(cast_log) if n in ("甲", "乙")
        )
        self.assertLess(local_first, remote_first)

    def test_interval_includes_cooldown_safety_margin(self):
        runner = JianglongRotationRunner(
            1001,
            roster_fn=lambda: [],
            cast_fn=lambda _target: None,
            cooldown_s=60.0,
            cooldown_guard_s=2.0,
            min_interval_s=0.05,
        )
        # X 清动作与原生施法需要少量前置时间：在 2 秒保护余量内提前 1 秒。
        self.assertEqual(runner._interval_for_roster_size(1), 61.0)
        self.assertEqual(runner._interval_for_roster_size(2), 30.5)
        self.assertEqual(runner._interval_for_roster_size(5), 12.2)

    def test_roster_is_collected_once_before_rotation(self):
        """入本等待结束后锁定已报名名单，后续名单变化不制造空轮。"""
        cast_log: list[int] = []
        roster_calls = 0

        def roster_fn():
            nonlocal roster_calls
            roster_calls += 1
            if roster_calls == 1:
                return [{"pid": 1001, "name": "甲"}]
            return [{"pid": 1001, "name": "甲"}, {"pid": 1002, "name": "乙"}]

        runner = JianglongRotationRunner(
            1001,
            roster_fn=roster_fn,
            cast_fn=lambda target: cast_log.append(int(target["pid"])),
            cooldown_s=0.2,
            cooldown_guard_s=0.0,
            min_interval_s=0.05,
            roster_settle_s=0.05,
            log=lambda _m: None,
        )
        runner.start()
        time.sleep(0.28)
        runner.stop()

        self.assertEqual(roster_calls, 1)
        self.assertTrue(cast_log)
        self.assertEqual(set(cast_log), {1001})

    def test_empty_roster_idles(self):
        runner = JianglongRotationRunner(
            1001,
            roster_fn=lambda: [],
            cast_fn=lambda t: None,
            log=lambda _m: None,
        )
        runner.start()
        time.sleep(0.2)
        runner.stop()
        self.assertEqual(runner.stats().get("turns", 0), 0)


class TestSyncAction(unittest.TestCase):
    def test_action_registered(self):
        self.assertIn(ACTION_JIANGLONG_CAST, VALID_ACTIONS)
        self.assertIn(ACTION_JIANGLONG_QUERY, VALID_ACTIONS)


class TestHangConfigField(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(DEFAULT_JIANGLONG_HANG)
        self.assertFalse(HangConfig().jianglong_hang)

    def test_settings_overlay(self):
        cfg = HangConfig(jianglong_hang=True)
        settings = {}
        write_hang_config_to_settings(settings, cfg)
        self.assertIs(settings["hang_jianglong_hang"], True)
        merged = get_hang_config(settings=settings)
        self.assertIs(merged.jianglong_hang, True)

    def test_disk_round_trip(self):
        cfg = HangConfig(jianglong_hang=True)
        out = save_hang_disk_from_config(cfg, char_id="42", char_name="测试")
        self.assertEqual(int(out["jianglong_hang"]), 1)
        loaded = get_hang_config(char_id="42")
        self.assertIs(loaded.jianglong_hang, True)


if __name__ == "__main__":
    unittest.main()
