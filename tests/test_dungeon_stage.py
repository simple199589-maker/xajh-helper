from __future__ import annotations

import unittest

from app.core.dungeon_stage import (
    DungeonTaskSequenceCounter,
    DungeonStageSnapshot,
    DungeonStageTracker,
    normalize_stage_text,
    parse_instance_countdown,
    parse_transition_seconds,
)


class DungeonStageTests(unittest.TestCase):
    def test_normalize_rejects_bad_unicode_and_strips_color(self):
        self.assertEqual(normalize_stage_text("^FF击败伏兵:4/18"), "击败伏兵:4/18")
        self.assertEqual(normalize_stage_text("숌芣㒄ǘ"), "")

    def test_transition_text(self):
        self.assertEqual(parse_transition_seconds(("5秒进入下一个版面",)), 5)
        self.assertIsNone(parse_transition_seconds(("击败伏兵:18/18",)))

    def test_instance_countdown(self):
        self.assertEqual(parse_instance_countdown("倒计时177:03"), 10623)
        self.assertEqual(parse_instance_countdown("倒计时00:00"), 0)

    def test_stage_identity_ignores_progress_numbers(self):
        a = DungeonStageSnapshot.from_texts(("击败伏兵:4/18",))
        b = DungeonStageSnapshot.from_texts(("击败伏兵:12/18",))
        self.assertEqual(a.signature, b.signature)

    def test_tracker_requires_two_samples_and_ignores_empty_repaint(self):
        tracker = DungeonStageTracker(stable_samples=2)
        start = DungeonStageSnapshot.from_texts(("击败伏兵:4/18",))
        self.assertIsNone(tracker.update(start))
        self.assertEqual(tracker.update(start), "snapshot")
        self.assertIsNone(tracker.update(DungeonStageSnapshot.from_texts(())))
        self.assertEqual(
            tracker.update(DungeonStageSnapshot.from_texts(("击败伏兵:5/18",))),
            None,
        )
        self.assertEqual(
            tracker.update(DungeonStageSnapshot.from_texts(("击败伏兵:5/18",))),
            "stage_progress",
        )

    def test_transition_is_completion_signal(self):
        tracker = DungeonStageTracker(stable_samples=1)
        tracker.update(DungeonStageSnapshot.from_texts(("击败伏兵:18/18",)))
        self.assertEqual(
            tracker.update(
                DungeonStageSnapshot.from_texts(("5秒进入下一个版面",))
            ),
            "transition_countdown",
        )

    def test_expiry_is_not_stage_completion(self):
        tracker = DungeonStageTracker(stable_samples=1)
        tracker.update(
            DungeonStageSnapshot.from_texts(
                ("击败伏兵:4/18",), time_text="倒计时00:01"
            )
        )
        self.assertEqual(
            tracker.update(
                DungeonStageSnapshot.from_texts((), time_text="倒计时00:00")
            ),
            "instance_expired",
        )

    def test_task_sequence_ignores_progress_and_counts_new_task(self):
        counter = DungeonTaskSequenceCounter()
        first = DungeonStageSnapshot.from_texts(("消灭三个大漠射手(2/3)",))
        self.assertEqual(
            counter.update("snapshot", first, entered_while_watching=True),
            "task_sequence_started",
        )
        self.assertIsNone(
            counter.update(
                "stage_progress",
                DungeonStageSnapshot.from_texts(("消灭三个大漠射手(3/3)",)),
            )
        )
        self.assertEqual(counter.progress.sequence_no, 1)

        transition = DungeonStageSnapshot.from_texts(("5秒进入下一个版面",))
        self.assertEqual(
            counter.update("transition_countdown", transition),
            "task_completed",
        )
        self.assertTrue(counter.progress.awaiting_next)

        next_stage = DungeonStageSnapshot.from_texts(("击败守门头目(0/1)",))
        self.assertEqual(
            counter.update("stage_changed", next_stage),
            "task_changed",
        )
        self.assertEqual(counter.progress.sequence_no, 2)
        self.assertFalse(counter.progress.awaiting_next)

    def test_task_sequence_marks_mid_run_as_relative(self):
        counter = DungeonTaskSequenceCounter()
        current = DungeonStageSnapshot.from_texts(("击败伏兵:4/18",))
        counter.update("snapshot", current, entered_while_watching=False)
        self.assertEqual(counter.progress.sequence_no, 1)
        self.assertFalse(counter.progress.absolute_known)


if __name__ == "__main__":
    unittest.main()
