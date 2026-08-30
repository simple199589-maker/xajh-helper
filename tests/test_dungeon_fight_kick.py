# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.core.dungeon_fight_kick import (
    KICK_RANGE_M,
    note_to_live,
    select_kick_candidate,
)


def _mon(tid: int, dist: float, id64: int = 0x0100000000000001) -> dict:
    return {"id64": id64, "tid": tid, "dist": dist, "x": 0.0, "y": 0.0, "z": 0.0}


class DungeonFightKickCandidateTest(unittest.TestCase):
    def test_selects_nearest_monster_within_range(self) -> None:
        monsters = [_mon(0x14C0B, 18.0), _mon(0x13EA5, 4.8), _mon(0x14C0B, 15.3)]
        cand = select_kick_candidate(monsters)
        self.assertIsNotNone(cand)
        self.assertEqual(int(cand["tid"]), 0x13EA5)
        self.assertAlmostEqual(float(cand["dist"]), 4.8)

    def test_rejects_monster_beyond_range(self) -> None:
        monsters = [_mon(0x14C0B, KICK_RANGE_M + 0.1)]
        self.assertIsNone(select_kick_candidate(monsters))

    def test_accepts_monster_at_exact_range(self) -> None:
        monsters = [_mon(0x14C0B, KICK_RANGE_M)]
        self.assertIsNotNone(select_kick_candidate(monsters))

    def test_rejects_zero_id64(self) -> None:
        monsters = [_mon(0x14C0B, 5.0, id64=0)]
        self.assertIsNone(select_kick_candidate(monsters))

    def test_rejects_negative_distance(self) -> None:
        monsters = [_mon(0x14C0B, -1.0)]
        self.assertIsNone(select_kick_candidate(monsters))

    def test_empty_monster_list_returns_none(self) -> None:
        self.assertIsNone(select_kick_candidate([]))
        self.assertIsNone(select_kick_candidate(None))

    def test_custom_range_cap(self) -> None:
        monsters = [_mon(0x14C0B, 10.0)]
        self.assertIsNone(select_kick_candidate(monsters, range_m=5.0))
        self.assertIsNotNone(select_kick_candidate(monsters, range_m=10.0))


class NoteToLiveTest(unittest.TestCase):
    def test_identity_for_preferred_base(self) -> None:
        self.assertEqual(note_to_live(0x400000, 0x15282D8), 0x15282D8)

    def test_offsets_relative_to_image_base(self) -> None:
        self.assertEqual(note_to_live(0x500000, 0x15282D8), 0x16282D8)


class MaybeKickGateTest(unittest.TestCase):
    """放行看门狗前置门 + 忽略怪候选拉黑状态机（纯内存桩，不碰真实进程）。

    功能边界（2026-08-30 拆分）：maybe_kick 只负责忽略怪（卡怪）放行；
    跟随打怪机器卡诊断见 tests/test_follow_fight_watchdog.py。
    """

    PID = 4321
    IGN = (0x13EA5, 0x18A98)

    def setUp(self) -> None:
        import time

        from app.core import dungeon_fight_kick as dfk
        import app.core.hang_settings as hs

        self._dfk = dfk
        self._hs = hs
        self._time = time
        hs._DUNGEON_TARGET_GUARD_TIDS.pop(self.PID, None)
        dfk.reset_state(self.PID)
        self.addCleanup(hs._DUNGEON_TARGET_GUARD_TIDS.pop, self.PID, None)
        self.addCleanup(dfk.reset_state, self.PID)
        self.session = MagicMock(pid=self.PID, module_base=0x400000)
        self.submits: list[int] = []
        self.logs: list[str] = []
        self._patch("resolve_host_base_rpm", lambda session: 0x2000)
        self._patch("resolve_host_rpm", lambda session, mid=None: 0x3000)
        self._patch("read_selected_target", lambda session, host: 0)
        self._patch("_host_pos", lambda session, host: (1.0, 60.0, 1.0))
        self._patch("walk_aoi_monsters", lambda session, host, mid, mb: [])

        def _fake_fire(session, module_base, host, cand, *, log=None) -> bool:
            self.submits.append(int(cand["id64"]))
            return True

        self._patch("fire_native_pick", _fake_fire)
        from app.core import activity_auto as aa

        self._patch_obj(
            aa,
            "resolve_cec_autoplay_rpm",
            lambda session: {"running": True, "autoplay": 0x1000},
        )

    # -- helpers ------------------------------------------------------------

    def _patch(self, name: str, repl) -> None:
        self._patch_obj(self._dfk, name, repl)

    def _patch_obj(self, obj, name: str, repl) -> None:
        p = patch.object(obj, name, repl)
        p.start()
        self.addCleanup(p.stop)

    def _arm(self) -> None:
        self._hs._DUNGEON_TARGET_GUARD_TIDS[self.PID] = tuple(self.IGN)

    def _ready(self) -> None:
        with self._dfk._state_lock:
            st = self._dfk._state.setdefault(self.PID, {})
            st["still_since"] = self._time.monotonic() - 100.0
            st["cooldown_until"] = 0.0

    def _run(self) -> None:
        self._dfk.maybe_kick(self.session, log=self.logs.append)

    # -- 前置门 ---------------------------------------------------------------

    def test_gate_idle_when_not_armed(self) -> None:
        self._patch(
            "walk_aoi_monsters",
            lambda session, host, mid, mb: [_mon(0x999, 3.0)],
        )
        self._run()  # seed still window（gate 在静止窗口之后）
        self._ready()
        self._run()
        self.assertEqual(self.submits, [])
        self.assertTrue(any("忽略怪功能未开启" in m for m in self.logs))

    def test_no_grab_when_only_normal_monsters(self) -> None:
        self._arm()
        self._patch(
            "walk_aoi_monsters",
            lambda session, host, mid, mb: [_mon(0x999, 3.0), _mon(0x777, 5.0)],
        )
        self._ready()
        self._run()
        self.assertEqual(self.submits, [])
        self.assertTrue(any("AOI 内无忽略怪" in m for m in self.logs))

    def test_disarmed_idle_again(self) -> None:
        self._arm()
        self._patch(
            "walk_aoi_monsters",
            lambda session, host, mid, mb: [_mon(0x13EA5, 8.0)],
        )
        self._ready()
        self._run()
        self.assertEqual(self.submits, [0x0100000000000001])
        self._hs._DUNGEON_TARGET_GUARD_TIDS.pop(self.PID, None)
        self._dfk.reset_state(self.PID)
        self.logs.clear()
        self._ready()
        self._run()
        self.assertEqual(self.submits, [0x0100000000000001])
        self.assertTrue(any("忽略怪功能未开启" in m for m in self.logs))

    # -- 候选与拉黑 -----------------------------------------------------------

    def test_submits_nearest_ignored_monster(self) -> None:
        self._arm()
        self._patch(
            "walk_aoi_monsters",
            lambda session, host, mid, mb: [_mon(0x999, 3.0), _mon(0x13EA5, 8.0)],
        )
        self._ready()
        self._run()
        self.assertEqual(self.submits, [0x0100000000000001])

    def test_fail_ban_switches_to_next_ignored(self) -> None:
        self._arm()
        self._patch(
            "walk_aoi_monsters",
            lambda session, host, mid, mb: [
                _mon(0x13EA5, 8.0, id64=0x1111),
                _mon(0x18A98, 12.0, id64=0x2222),
            ],
        )
        self._ready()
        self._run()
        self.assertEqual(self.submits, [0x1111])
        with self._dfk._state_lock:
            self._dfk._state[self.PID]["cooldown_until"] = 0.0
        self._run()  # 未选中 → 失败 #1，仍重试
        self.assertEqual(self.submits, [0x1111, 0x1111])
        with self._dfk._state_lock:
            self._dfk._state[self.PID]["cooldown_until"] = 0.0
        self._run()  # 失败 #2 → 拉黑，不提交
        self.assertEqual(self.submits, [0x1111, 0x1111])
        self.assertTrue(any("连续2次提交未生效" in m for m in self.logs))
        with self._dfk._state_lock:
            self._dfk._state[self.PID]["cooldown_until"] = 0.0
        self._run()  # 改选次近忽略怪
        self.assertEqual(self.submits[-1], 0x2222)

    def test_selection_clears_fail_record(self) -> None:
        self._arm()
        self._patch(
            "walk_aoi_monsters",
            lambda session, host, mid, mb: [_mon(0x13EA5, 8.0, id64=0x1111)],
        )
        self._ready()
        self._run()
        with self._dfk._state_lock:
            self._dfk._state[self.PID]["cooldown_until"] = 0.0
        self._run()  # 失败 #1
        self._patch("read_selected_target", lambda session, host: 0x1111)
        self._run()  # 选中 → 清失败计数与 last_cand
        with self._dfk._state_lock:
            st = self._dfk._state[self.PID]
        self.assertIsNone(st.get("cand_fails", {}).get(0x1111))
        self.assertIsNone(st.get("last_cand_id64"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
