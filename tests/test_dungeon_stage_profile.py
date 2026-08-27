from __future__ import annotations

import unittest

from app.core.dungeon_stage_profile import (
    append_stage,
    empty_profiles,
    match_stage,
    reset_profile,
)


class DungeonStageProfileTests(unittest.TestCase):
    def test_records_progress_as_one_stage(self):
        data = empty_profiles()
        profile = reset_profile(data, scene_id=1524, name="沙漠古镇")
        self.assertEqual(append_stage(profile, ("消灭三个大漠射手(2/3)",)), 1)
        self.assertEqual(append_stage(profile, ("消灭三个大漠射手(3/3)",)), 1)
        self.assertEqual(len(profile["stages"]), 1)

    def test_force_records_same_task_on_next_board(self):
        data = empty_profiles()
        profile = reset_profile(data, scene_id=1524, name="沙漠古镇")
        self.assertEqual(append_stage(profile, ("清理守卫(0/3)",)), 1)
        self.assertEqual(
            append_stage(profile, ("清理守卫(0/3)",), force=True),
            2,
        )
        self.assertEqual(len(profile["stages"]), 2)

    def test_match_uses_forward_occurrence_for_repeated_task(self):
        data = empty_profiles()
        profile = reset_profile(data, scene_id=1524, name="沙漠古镇")
        append_stage(profile, ("清理守卫(0/3)",))
        append_stage(profile, ("打开机关门",))
        append_stage(profile, ("清理守卫(0/3)",))

        stage, complete = match_stage(
            data,
            scene_id=1524,
            targets=("清理守卫(2/3)",),
            previous_stage=2,
        )
        self.assertEqual(stage, 3)
        self.assertTrue(complete)

    def test_repeated_task_is_ambiguous_without_previous_stage(self):
        data = empty_profiles()
        profile = reset_profile(data, scene_id=1524, name="沙漠古镇")
        append_stage(profile, ("清理守卫(0/3)",))
        append_stage(profile, ("打开机关门",))
        append_stage(profile, ("清理守卫(0/3)",))

        stage, complete = match_stage(
            data,
            scene_id=1524,
            targets=("清理守卫(2/3)",),
        )
        self.assertIsNone(stage)
        self.assertTrue(complete)

    def test_unknown_scene_does_not_match(self):
        stage, complete = match_stage(
            empty_profiles(), scene_id=9999, targets=("击败守卫",)
        )
        self.assertIsNone(stage)
        self.assertFalse(complete)


if __name__ == "__main__":
    unittest.main()
