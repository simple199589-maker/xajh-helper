from __future__ import annotations

import unittest

from app.core.dungeon_board_catalog import BoardTimeline, lookup_boards, task_signature


class DungeonBoardCatalogTests(unittest.TestCase):
    def test_lua_percent_progress_matches_live_ui_progress(self):
        self.assertEqual(
            task_signature("消灭三个大漠射手(%d/3)"),
            task_signature("消灭三个大漠射手(2/3)"),
        )

    def test_unique_sand_town_task_resolves_real_board(self):
        result = lookup_boards(1524, ("消灭三个大漠射手(2/3)",))
        self.assertEqual(result.instance_id, 6230)
        self.assertEqual(result.candidates, (12,))
        self.assertEqual(result.board, 12)
        self.assertTrue(result.exact)

    def test_northern_mad_beggar_resolves_real_board_twenty_six(self):
        result = lookup_boards(1524, ("击杀北疆疯丐(0/1)",))
        self.assertEqual(result.instance_id, 6230)
        self.assertEqual(result.candidates, (26,))
        self.assertEqual(result.board, 26)
        self.assertTrue(result.exact)

    def test_repeated_task_text_returns_candidates_not_a_guess(self):
        result = lookup_boards(1524, ("前进，击溃所有阻碍",))
        self.assertEqual(
            result.candidates,
            (1, 2, 10, 11, 13, 15, 17, 18, 19, 21, 22, 23, 25),
        )
        self.assertIsNone(result.board)
        self.assertFalse(result.exact)

    def test_multiple_visible_tasks_resolve_meizhuang_outer_board_two(self):
        result = lookup_boards(
            1516,
            ("消灭梅庄入门弟子(9/16)", "消灭梅庄力士(1/2)"),
        )
        self.assertEqual(result.instance_id, 2638)
        self.assertEqual(result.candidates, (2,))

    def test_scorpion_cave_unique_task_resolves_real_board(self):
        result = lookup_boards(1508, ("击杀黑血毒蝎",))
        self.assertEqual(result.instance_id, 1928)
        self.assertEqual(result.candidates, (11,))
        self.assertEqual(result.board, 11)

    def test_scorpion_cave_repeated_task_returns_candidates(self):
        result = lookup_boards(1508, ("击溃所有敌人,片甲不留",))
        self.assertEqual(result.candidates, (5, 7))
        self.assertIsNone(result.board)

    def test_timeline_advances_after_board_twelve_completion(self):
        timeline = BoardTimeline()
        archers = lookup_boards(1524, ("消灭三个大漠射手(2/3)",))
        initial = timeline.update(scene_id=1524, lookup=archers, event="stage_started")
        self.assertEqual(initial.board, 12)
        self.assertEqual(initial.evidence, "client_task")

        completed = timeline.update(
            scene_id=1524,
            lookup=archers,
            event="transition_countdown",
        )
        self.assertEqual(completed.board, 12)
        self.assertTrue(completed.awaiting_next)

        generic = lookup_boards(1524, ("前进，击溃所有阻碍",))
        next_board = timeline.update(
            scene_id=1524,
            lookup=generic,
            event="stage_changed",
        )
        self.assertEqual(next_board.board, 13)
        self.assertEqual(next_board.evidence, "timeline")
        self.assertFalse(next_board.awaiting_next)

        bomb = lookup_boards(1524, ("小心炸弹！！",))
        exact = timeline.update(scene_id=1524, lookup=bomb, event="stage_changed")
        self.assertEqual(exact.board, 14)
        self.assertEqual(exact.evidence, "client_task")

    def test_timeline_keeps_last_board_during_ui_repaint(self):
        timeline = BoardTimeline()
        archers = lookup_boards(1524, ("消灭三个大漠射手(2/3)",))
        timeline.update(scene_id=1524, lookup=archers, event="stage_started")
        repaint = timeline.update(
            scene_id=1524,
            lookup=lookup_boards(1524, ()),
            event=None,
        )
        self.assertEqual(repaint.board, 12)
        self.assertEqual(repaint.evidence, "cached")


if __name__ == "__main__":
    unittest.main()
