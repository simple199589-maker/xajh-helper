# -*- coding: utf-8 -*-
"""Unit tests for the follow-fight (组队副本挂机) stuck-state watchdog.

功能一：组队副本挂机（跟随打怪）状态机机器卡诊断。判定收紧为
StateAlert ∧ 无选中目标 ∧ 原地静止 ≥ ALERT_STILL_S，按队长/队员身份
给出不同处置文案；独立于「忽略副本卡怪」开关。
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from app.core import follow_fight_watchdog as fw
from app.core import team_ops

_CAPT = "队长身份开挂的已知粘滞"
_MEMB = "疑似机器卡"


class FollowFightWatchdogTest(unittest.TestCase):
    PID = 4321

    def setUp(self) -> None:
        fw.reset_state(self.PID)
        self.addCleanup(fw.reset_state, self.PID)
        self.session = MagicMock(pid=self.PID, module_base=0x400000)
        self.logs: list[str] = []
        self.alert_vt = fw.note_to_live(0x400000, fw._NOTE_STATE_ALERT)
        self._patch(
            "resolve_cec_autoplay_rpm",
            lambda session: {"running": True, "autoplay": 0x1000},
        )
        self._patch("resolve_host_base_rpm", lambda session: 0x2000)
        self._patch("resolve_host_rpm", lambda session, mid=None: 0x3000)
        self._patch("read_selected_target", lambda session, host: 0)
        self._patch("_host_pos", lambda session, host: (1.0, 60.0, 1.0))
        self._patch("_rpm_u32", lambda session, addr: self.alert_vt)
        self._patch_list([])  # 默认读不到队伍 → 按队长处理

    # -- helpers ------------------------------------------------------------

    def _patch(self, name: str, repl) -> None:
        p = patch.object(fw, name, repl)
        p.start()
        self.addCleanup(p.stop)

    def _patch_list(self, members: list[dict]) -> None:
        p = patch.object(
            team_ops, "list_party_members", lambda session, *, fresh=True, log=None: members
        )
        p.start()
        self.addCleanup(p.stop)

    def _run(self) -> None:
        fw.watch_follow_fight_stuck(self.session, log=self.logs.append)

    def _seed_still(self) -> None:
        self._run()  # seed still window
        with fw._state_lock:
            fw._state[self.PID]["still_since"] = time.monotonic() - 20.0

    # -- 触发与身份 -----------------------------------------------------------

    def test_captain_alert_after_still_window(self) -> None:
        self._patch_list([{"is_self": True, "is_leader": True}])
        self._seed_still()
        self._run()
        self.assertTrue(any(_CAPT in m for m in self.logs))
        self.assertFalse(any(_MEMB in m for m in self.logs))

    def test_member_window_gets_member_text(self) -> None:
        self._patch_list([{"is_self": True, "is_leader": False}])
        self._seed_still()
        self._run()
        self.assertTrue(any(_MEMB in m for m in self.logs))
        self.assertFalse(any(_CAPT in m for m in self.logs))

    def test_alert_throttled(self) -> None:
        self._seed_still()
        self._run()
        n = len([m for m in self.logs if _CAPT in m or _MEMB in m])
        self.assertGreaterEqual(n, 1)
        self._run()
        self.assertEqual(len([m for m in self.logs if _CAPT in m or _MEMB in m]), n)

    # -- 误报收紧 -------------------------------------------------------------

    def test_no_alert_when_target_selected(self) -> None:
        self._patch("read_selected_target", lambda session, host: 0xC)
        self._seed_still()
        self._run()
        self.assertFalse(any(_CAPT in m or _MEMB in m for m in self.logs))

    def test_no_alert_when_moving(self) -> None:
        self._run()  # 建立状态（seed still window + last_pos）
        with fw._state_lock:
            fw._state[self.PID]["last_pos"] = (5.0, 60.0, 5.0)
        self._run()  # 位移 > MOVE_EPS_M → 窗口复位 → 无告警
        self.assertFalse(any(_CAPT in m or _MEMB in m for m in self.logs))

    def test_state_left_alert_no_alert_and_no_throttle_consume(self) -> None:
        self._seed_still()
        self._run()  # 第一次告警，进入 60s 节流
        n = len([m for m in self.logs if _CAPT in m or _MEMB in m])
        # 状态离开 Alert：不告警，且不消耗节流窗口
        self._patch("_rpm_u32", lambda session, addr: 0x123)
        with fw._state_lock:
            fw._state[self.PID]["still_since"] = time.monotonic() - 20.0
            fw._state[self.PID]["alert_cooldown_until"] = 0.0
        self._run()
        self.assertEqual(len([m for m in self.logs if _CAPT in m or _MEMB in m]), n)
        # 回到 Alert：立即再报（节流未被消耗）
        self._patch("_rpm_u32", lambda session, addr: self.alert_vt)
        self._run()
        self.assertEqual(len([m for m in self.logs if _CAPT in m or _MEMB in m]), n + 1)

    def test_autoplay_off_resets_state(self) -> None:
        self._seed_still()
        self._patch(
            "resolve_cec_autoplay_rpm", lambda session: {"running": False, "autoplay": 0}
        )
        self._run()
        with fw._state_lock:
            self.assertNotIn(self.PID, fw._state)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
