# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from app.core.daily_accept import (
    NEXT_NPC_GROUP_INTERVAL_S,
    TASK_ACCEPT_INTERVAL_S,
    daily_npc_dialog_packets,
)


class DailyAcceptPacketTests(unittest.TestCase):
    def test_each_dungeon_group_uses_its_own_dialog_packets(self) -> None:
        expected = {
            "dungeon_primary": (100219, "DA"),
            "dungeon_middle": (100218, "D9"),
            "dungeon_high": (100220, "DB"),
            "dungeon_leveling": (100221, "DC"),
        }
        for group, (delv_tid, service_id) in expected.items():
            packets = daily_npc_dialog_packets(group, delv_tid)
            self.assertIsNotNone(packets)
            self.assertEqual(packets[0].hex().upper(), f"0A00{service_id}07000000000001")
            self.assertEqual(packets[1].hex().upper(), f"0C00{service_id}07000000000001")

    def test_accept_timing_constants(self) -> None:
        self.assertEqual(TASK_ACCEPT_INTERVAL_S, 1.5)
        self.assertEqual(NEXT_NPC_GROUP_INTERVAL_S, 2.0)
    def test_non_dungeon_daily_npc_packets_remain_separate(self) -> None:
        self.assertEqual(
            daily_npc_dialog_packets("songyao")[0].hex().upper(),
            "0A00EA07000000000001",
        )
        self.assertEqual(
            daily_npc_dialog_packets("bubai")[0].hex().upper(),
            "0A00E907000000000001",
        )


if __name__ == "__main__":
    unittest.main()
