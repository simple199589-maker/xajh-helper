# -*- coding: utf-8 -*-
"""Regression tests for the dungeon unstick rules."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.core.activity_auto import (
    ActivityConfig,
    ActivityRunner,
    DUNGEON_UNSTICK_RULES,
    DUNGEON_UNSTICK_ARRIVE_RADIUS_M,
    DUNGEON_UNSTICK_STILL_S,
    DungeonUnstickGuard,
)


class DungeonUnstickRuleTest(unittest.TestCase):
    def test_all_dungeon_corrections_trigger_after_six_seconds(self) -> None:
        self.assertEqual(DUNGEON_UNSTICK_STILL_S, 6.0)

    def test_arrival_radius_is_shared_by_entry_and_path_logic(self) -> None:
        self.assertEqual(DUNGEON_UNSTICK_ARRIVE_RADIUS_M, 4.0)
        guard = DungeonUnstickGuard(instance_id=1928)
        guard._entry_reached = False
        guard._maybe_mark_entry_reached((-45.9, 67.0, -19.0))
        self.assertTrue(guard._entry_reached)
        # 3m is outside the old 2m threshold but inside the shared 4m
        # measured-arrival threshold; it must resolve as the next card point.
        self.assertEqual(guard._resolve_zone((-45.9, 67.0, -19.0)), 1)

    def test_retained_dungeon_corrections_are_active(self) -> None:
        self.assertEqual(set(DUNGEON_UNSTICK_RULES), {1928, 2638, 6230})
        self.assertEqual(DUNGEON_UNSTICK_RULES[1928]["name"], "蝎王魔窟")
        self.assertEqual(DUNGEON_UNSTICK_RULES[2638]["name"], "梅庄外围")
        self.assertEqual(DUNGEON_UNSTICK_RULES[6230]["name"], "沙漠古镇")
        for instance_id in (1928, 2638, 6230):
            self.assertTrue(DUNGEON_UNSTICK_RULES[instance_id]["follow"])
            self.assertTrue(DUNGEON_UNSTICK_RULES[instance_id]["follow_keepalive"])

    def test_retained_coordinates_keep_expected_first_stuck_points(self) -> None:
        self.assertTrue(DUNGEON_UNSTICK_RULES[1928]["zones"][0]["from_entry"])
        self.assertEqual(
            DUNGEON_UNSTICK_RULES[1928]["zones"][1]["stuck"],
            (-45.9, 67.0, -16.1),
        )
        self.assertEqual(
            DUNGEON_UNSTICK_RULES[6230]["zones"][0]["stuck"],
            (-46.1, 62.6, -400.4),
        )
        self.assertEqual(
            DUNGEON_UNSTICK_RULES[2638]["zones"][0]["stuck"],
            (35.6, 16.8, -8.5),
        )
        self.assertEqual(DUNGEON_UNSTICK_RULES[2638]["target"], (33.5, 16.8, -0.5))

    def test_meizhuang_instance_creates_a_guard(self) -> None:
        runner = ActivityRunner(
            pid=10, cfg=ActivityConfig(mode="dungeon", instance_id=2638)
        )
        with patch.object(DungeonUnstickGuard, "tick") as tick:
            runner._dungeon_unstick_tick(
                MagicMock(pid=10),
                scene_id=1524,
                scene_label="梅庄外围",
                pos=(35.6, 16.8, -8.5),
            )
        self.assertIsInstance(runner._dungeon_unstick_guard, DungeonUnstickGuard)
        tick.assert_called_once()

    def test_retained_instance_creates_a_guard(self) -> None:
        runner = ActivityRunner(
            pid=10, cfg=ActivityConfig(mode="dungeon", instance_id=6230)
        )
        with patch.object(DungeonUnstickGuard, "tick") as tick:
            runner._dungeon_unstick_tick(
                MagicMock(pid=10),
                scene_id=1524,
                scene_label="沙漠古镇",
                pos=(-46.1, 62.6, -400.4),
            )
        self.assertIsInstance(runner._dungeon_unstick_guard, DungeonUnstickGuard)
        tick.assert_called_once()

    def test_non_dungeon_runner_never_creates_a_guard(self) -> None:
        runner = ActivityRunner(
            pid=10, cfg=ActivityConfig(mode="activity", instance_id=1928)
        )
        runner._dungeon_unstick_tick(
            MagicMock(pid=10), scene_id=1, scene_label="x", pos=(0, 0, 0)
        )
        self.assertIsNone(runner._dungeon_unstick_guard)


if __name__ == "__main__":
    unittest.main()
