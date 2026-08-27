from __future__ import annotations

import unittest

from app.core.damage_item_stats import find_damage_items, parse_item_use_trace
from app.core.package_api import PackageItem


class DamageItemStatsTests(unittest.TestCase):
    def test_finder_keeps_damage_items_and_excludes_gems(self) -> None:
        gem = PackageItem(2, 0, 1, tid=0x4210, count=1, name="2200W外功宝石")
        pill = PackageItem(2, 1, 2, tid=0x4210, count=8, name="2200W外功伤害物品")

        found = find_damage_items([gem, pill])

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].slot, 1)

    def test_parser_reads_native_useitem_counters(self) -> None:
        note = "ITEM_USE_TRACE pack=2 slot=0 attempts=17 success=12 armed=1"
        self.assertEqual(parse_item_use_trace(note), (17, 12))
        self.assertIsNone(parse_item_use_trace("bridge no trace"))

    def test_trace_is_gated_by_exact_client_capability(self) -> None:
        from app.core.bridge_protocol import BridgeCommand, required_build_capability

        self.assertEqual(
            required_build_capability(BridgeCommand.ITEM_USE_TRACE),
            "item.use.trace",
        )
