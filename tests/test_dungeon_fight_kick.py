# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
