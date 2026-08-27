# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest import mock

from app.core.dungeon_target_policy import (
    PACKAGED_DUNGEON_TARGET_RULES,
    configured_dungeon_tids,
)


class DungeonTargetPolicyTest(unittest.TestCase):
    def test_merges_packaged_baseline_and_persistent_assistant_rules(self) -> None:
        with mock.patch(
            "app.core.dungeon_target_policy.load_char_rules",
            return_value=[
                {"tid": 0x18A98},
                {"tid": 0x13EA5},
                {"tid": 0x18A98},
                {"tid": 0},
            ],
        ) as load:
            self.assertEqual(
                configured_dungeon_tids(12345),
                (0x13EA5, 0x18A98),
            )
        load.assert_called_once_with(12345)

    def test_packaged_baseline_contains_confirmed_two_targets(self) -> None:
        self.assertEqual(
            PACKAGED_DUNGEON_TARGET_RULES,
            ((0x13EA5, "北疆疯丐"), (0x18A98, "上官霸刀")),
        )

    def test_bad_storage_keeps_packaged_baseline(self) -> None:
        with mock.patch(
            "app.core.dungeon_target_policy.load_char_rules",
            side_effect=OSError("unavailable"),
        ):
            self.assertEqual(configured_dungeon_tids(12345), (0x13EA5, 0x18A98))


if __name__ == "__main__":
    unittest.main()
