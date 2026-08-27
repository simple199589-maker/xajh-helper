from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core.grocery_auto import (
    GroceryConfig,
    auto_use_selected,
    grocery_package_indexes,
    item_matches_want,
)
from app.core.package_api import (
    GOLD_UNIT,
    PackageActionResult,
    PackageItem,
    _clean_item_name,
    _is_good_item_name,
    _note_va,
    ensure_full_gold,
    format_gold,
    gold_to_copper,
    is_revive_pill_name,
    pills_needed_for_gold,
)


class PackageAndGroceryTests(unittest.TestCase):
    def test_fixed_package_symbols_reject_unknown_client(self) -> None:
        from app.core.plg_exports import NOTE_VA_SELL_FROM_PACKAGE

        session = type(
            "Session",
            (),
            {"module_base": 0x400000, "exe_path": r"D:\missing\xajh.exe"},
        )()
        with self.assertRaisesRegex(RuntimeError, "package.sell"):
            _note_va(session, NOTE_VA_SELL_FROM_PACKAGE)

    def test_package_models_serialize(self) -> None:
        item = PackageItem(2, 3, 0x10, tid=9, count=4, name="测试物品")
        self.assertEqual(item.to_dict()["slot"], 3)
        result = PackageActionResult(True, "use", item=item.to_dict(), ret=1)
        self.assertEqual(result.to_dict()["item"]["tid"], 9)

    def test_rpm_package_reader_uses_verified_chain_without_game_export(self) -> None:
        from app.core import package_api as pa

        base = 0x400000
        root = 0x200000
        mid = 0x210000
        host = 0x220000
        # Regression: LARGEADDRESSAWARE clients may allocate the package
        # manager above 0x80000000; RPM must decide whether it is readable.
        manager = 0x9785DC20
        package = 0x240000
        array = 0x250000
        item = 0x260000
        memory = {
            base + (pa.NOTE_VA_GAME_ROOT_GLOBAL - pa.DEFAULT_IMAGE_BASE): root,
            root + pa.PKG_ROOT_MID_OFF: mid,
            mid + pa.PKG_ROOT_HOST_OFF: host,
            host + pa.PKG_HOST_MANAGER_OFF: manager,
            manager + pa.PKG_MANAGER_FIRST_PACKAGE_OFF + 3 * 4: package,
            package + pa.PKG_CAP_OFF: 0,
            package + pa.PKG_SIZE_OFF: 2,
            package + pa.PKG_ARR_OFF: array,
            array: item,
            array + 4: 0,
            item + pa.ITEM_TID_OFF: 0x4201,
            item + pa.ITEM_COUNT_OFF: 3,
        }

        def read_u32(_handle, address):
            return memory.get(address, 0)

        with patch.object(pa, "_module_base_for_rpm", return_value=base), patch.object(
            pa, "_open_read_only_process", return_value=99
        ), patch.object(pa, "_rpm_u32", side_effect=read_u32), patch.object(
            pa.kernel32, "CloseHandle"
        ), patch.object(pa, "get_package_ptr") as get_package, patch.object(
            pa, "remote_call_cdecl_x86"
        ) as remote_call:
            out = pa.list_package_items_rpm(SimpleNamespace(pid=515179), 3)

        self.assertEqual(
            [(item.package, item.slot, item.tid, item.count) for item in out],
            [(3, 0, 0x4201, 3)],
        )
        get_package.assert_not_called()
        remote_call.assert_not_called()

    def test_rpm_pointer_filter_rejects_only_top_guard(self) -> None:
        from app.core.package_api import _sane_user_ptr

        self.assertEqual(_sane_user_ptr(0x9785DC20), 0x9785DC20)
        self.assertEqual(_sane_user_ptr(0xFFFF0000), 0)

    def test_item_name_cleanup_and_quality(self) -> None:
        self.assertEqual(_clean_item_name("\ue000\x01白云熊胆丸"), "白云熊胆丸")
        self.assertTrue(_is_good_item_name("白云熊胆丸"))
        self.assertFalse(_is_good_item_name("ab"))
        self.assertFalse(_is_good_item_name("测试한글"))

    def test_gold_format_and_revive_names(self) -> None:
        self.assertEqual(format_gold(None), "n/a")
        self.assertIn("10金", format_gold(100000))
        self.assertTrue(is_revive_pill_name("白云熊胆丸"))
        self.assertTrue(is_revive_pill_name("绑定白云熊胆丸"))
        self.assertTrue(is_revive_pill_name("白云熊丹丸"))  # user alias
        self.assertTrue(is_revive_pill_name("白云高级复活丹"))  # legacy note name
        self.assertTrue(is_revive_pill_name("tid=44374", 0))
        self.assertTrue(is_revive_pill_name("", 44374))
        self.assertFalse(is_revive_pill_name("普通药丸"))
        self.assertFalse(is_revive_pill_name("tid=12345"))

    def test_pills_needed_formula(self) -> None:
        # 目标 45000 金，当前 0 → 需 4500 个（10金/个）
        target = gold_to_copper(45000)
        self.assertEqual(pills_needed_for_gold(0, target), 4500)
        # 目标 45000 金，当前 44990 金 → 差 10 金 → 1 个
        self.assertEqual(
            pills_needed_for_gold(gold_to_copper(44990), target), 1
        )
        # 目标 3000 金，当前 0 → 300 个，绝不是 3000 个
        self.assertEqual(pills_needed_for_gold(0, gold_to_copper(3000)), 300)
        # 已足够
        self.assertEqual(pills_needed_for_gold(target, target), 0)

    def test_item_name_map_resolve(self) -> None:
        from app.core.item_names import (
            learn_item_name,
            lookup_item_name,
            resolve_item_display_name,
        )

        learn_item_name(44374, "白云熊胆丸")
        self.assertEqual(lookup_item_name(44374), "白云熊胆丸")
        self.assertEqual(
            resolve_item_display_name(44374, ""),
            "白云熊胆丸",
        )
        self.assertEqual(
            resolve_item_display_name(99999901, ""),
            "tid=99999901",
        )
        self.assertEqual(
            resolve_item_display_name(1, "混元硬气-80"),
            "混元硬气-80",
        )

    def test_want_matches_name_and_tid(self) -> None:
        item = PackageItem(2, 1, 1, tid=123, name="精致宝箱")
        self.assertTrue(item_matches_want(item, ["宝箱"]))
        self.assertTrue(item_matches_want(item, ["tid=123"]))
        self.assertFalse(item_matches_want(item, ["tid=999"]))

    def test_grocery_default_bag_includes_capacity_expansions_not_warehouse(self) -> None:
        self.assertEqual(grocery_package_indexes(GroceryConfig()), (2, 3, 4))
        self.assertNotIn(11, grocery_package_indexes(GroceryConfig()))

    @patch("app.core.grocery_auto.list_packages_items")
    def test_refresh_bag_reads_main_and_capacity_expansion_packages(
        self, list_packages
    ) -> None:
        from app.core.grocery_auto import refresh_bag

        expected = [PackageItem(3, 7, 1, tid=7, name="扩展包物品")]
        list_packages.return_value = expected
        out = refresh_bag(object(), GroceryConfig(), use_cache=False)

        self.assertEqual(out, expected)
        self.assertEqual(list_packages.call_args.args[1], (2, 3, 4))

    @patch("app.core.grocery_auto.time.sleep", return_value=None)
    @patch("app.core.grocery_auto.use_item_in_package")
    @patch("app.core.grocery_auto._list_grocery_items")
    def test_use_selected_key_disambiguates_same_slot_across_packages(
        self, list_items, use_item, _sleep
    ) -> None:
        from app.core.grocery_auto import auto_use_or_open_selected

        list_items.return_value = [
            PackageItem(2, 0, 1, tid=1, name="主包同槽"),
            PackageItem(3, 0, 2, tid=2, name="帝王背包扩展物品"),
        ]
        use_item.return_value = PackageActionResult(True, "use")

        out = auto_use_or_open_selected(
            object(), GroceryConfig(), item_keys=[(3, 0)]
        )

        self.assertTrue(out.ok)
        use_item.assert_called_once()
        self.assertEqual(use_item.call_args.args[1:3], (3, 0))

    @patch("app.core.grocery_auto._sleep_interruptible", return_value=False)
    @patch("app.core.grocery_auto.sell_item_from_package")
    @patch("app.core.grocery_auto._list_grocery_items")
    def test_auto_sell_uses_and_verifies_expansion_package_identity(
        self, list_items, sell_item, _sleep
    ) -> None:
        from app.core.grocery_auto import auto_sell_selected

        target = PackageItem(4, 0, 2, tid=88, count=3, name="扩展出售物")
        list_items.side_effect = [[target], []]
        sell_item.return_value = PackageActionResult(True, "sell", ret=1)

        out = auto_sell_selected(
            object(), GroceryConfig(sell_names=["扩展出售物"]), continuous=False
        )

        self.assertTrue(out.ok)
        self.assertEqual(sell_item.call_args.args[1:4], (4, 0, 3))

    def test_auto_use_requires_selection(self) -> None:
        out = auto_use_selected(object(), GroceryConfig())
        self.assertFalse(out.ok)
        self.assertIn("未选择", out.message)

    def test_open_box_requires_selection(self) -> None:
        from app.core.grocery_auto import auto_open_box

        out = auto_open_box(object(), GroceryConfig())
        self.assertFalse(out.ok)
        self.assertIn("未选择", out.message)

    def test_looks_like_box_item(self) -> None:
        from app.core.grocery_auto import looks_like_box_item

        self.assertTrue(looks_like_box_item("福利活动宝箱"))
        self.assertTrue(looks_like_box_item("新春礼盒"))
        self.assertTrue(looks_like_box_item("tid=12345"))
        self.assertFalse(looks_like_box_item("疗伤的妙药"))
        self.assertFalse(looks_like_box_item("白云熊胆丸"))

    def test_sleep_interruptible_no_negative(self) -> None:
        import time as _t

        from app.core.grocery_auto import _sleep_interruptible

        t0 = _t.time()
        # should not raise on near-zero remaining
        self.assertFalse(_sleep_interruptible(0.0, None))
        self.assertFalse(_sleep_interruptible(-1.0, None))
        self.assertLess(_t.time() - t0, 0.5)

    @patch("app.core.grocery_auto.time.sleep", return_value=None)
    @patch("app.core.grocery_auto.use_item_in_package")
    @patch("app.core.grocery_auto.list_package_items")
    def test_use_or_open_once_per_slot(
        self, list_items, use_item, _sleep
    ) -> None:
        from app.core.grocery_auto import auto_use_or_open_selected

        list_items.return_value = [
            PackageItem(2, 1, 1, tid=1, count=1, name="疗伤的妙药"),
            PackageItem(2, 2, 2, tid=2, count=3, name="青铜宝箱"),
        ]
        use_item.return_value = PackageActionResult(True, "use")
        out = auto_use_or_open_selected(
            object(), GroceryConfig(), slots=[1, 2]
        )
        self.assertTrue(out.ok)
        # 每个槽只 UseItem 一次（含箱子）
        self.assertEqual(use_item.call_count, 2)
        slots_hit = [c.args[2] for c in use_item.call_args_list]
        self.assertEqual(slots_hit, [1, 2])

    @patch("app.core.grocery_auto.time.sleep", return_value=None)
    @patch("app.core.grocery_auto.use_item_in_package")
    @patch("app.core.grocery_auto.list_package_items")
    def test_auto_use_continuous_stops(self, list_items, use_item, _sleep) -> None:
        import threading

        from app.core.grocery_auto import auto_use_selected

        stop = threading.Event()
        n = {"i": 0}

        def _list(*_a, **_k):
            n["i"] += 1
            if n["i"] >= 3:
                stop.set()
            return [PackageItem(2, 1, 1, tid=1, count=1, name="疗伤的妙药")]

        list_items.side_effect = _list
        use_item.return_value = PackageActionResult(True, "use")
        out = auto_use_selected(
            object(),
            GroceryConfig(use_names=["疗伤的妙药"], use_delay_s=0.01),
            stop_event=stop,
            continuous=True,
            idle_s=0.01,
        )
        self.assertTrue(out.ok)
        self.assertGreaterEqual(use_item.call_count, 2)
        self.assertIn("停止", out.message)

    @patch("app.core.grocery_auto.time.sleep", return_value=None)
    @patch("app.core.grocery_auto.use_item_in_package")
    @patch("app.core.grocery_auto.list_package_items")
    def test_open_box_uses_selected_slots(self, list_items, use_item, _sleep) -> None:
        from app.core.grocery_auto import auto_open_box

        list_items.side_effect = [
            [
                PackageItem(2, 3, 1, tid=1, count=1, name="箱A"),
                PackageItem(2, 5, 2, tid=2, count=1, name="箱B"),
            ],
            [PackageItem(2, 5, 2, tid=2, count=1, name="箱B")],
            [],
        ]
        use_item.return_value = PackageActionResult(True, "use")
        out = auto_open_box(object(), GroceryConfig(open_box_max=10), slots=[3, 5])
        self.assertTrue(out.ok)
        used_slots = [c.args[2] for c in use_item.call_args_list]
        self.assertIn(3, used_slots)

    @patch("app.core.grocery_auto.time.sleep", return_value=None)
    @patch("app.core.grocery_auto.time.time", side_effect=[0, 0.2, 0.2, 0.4, 0.4, 1.0])
    @patch("app.core.grocery_auto.use_item_in_package")
    @patch("app.core.grocery_auto.read_package_slot")
    def test_open_first_slot_fixed_zero(self, read_slot, use_item, _t, _sleep) -> None:
        from app.core.grocery_auto import auto_open_first_slot

        read_slot.return_value = PackageItem(2, 0, 1, tid=9, count=2, name="首格箱")
        use_item.return_value = PackageActionResult(True, "use")
        cfg = GroceryConfig(open_first_delay_s=0.1, open_box_max=10)
        out = auto_open_first_slot(
            object(), cfg, slot=0, continuous=False, idle_s=0.01
        )
        self.assertTrue(out.ok)
        for c in use_item.call_args_list:
            self.assertEqual(c.args[2], 0)
        # fire path: no bag verify, quiet, reuses cadence only
        kwargs = use_item.call_args.kwargs
        self.assertFalse(kwargs.get("verify_bag", True))

    @patch("app.core.grocery_auto.time.sleep", return_value=None)
    @patch("app.core.grocery_auto.use_item_in_package")
    @patch("app.core.grocery_auto.list_package_items")
    def test_auto_use_only_matching_items(self, list_items, use_item, _sleep) -> None:
        list_items.return_value = [
            PackageItem(2, 1, 1, tid=10, name="目标箱"),
            PackageItem(2, 2, 2, tid=20, name="其它物品"),
        ]
        use_item.return_value = PackageActionResult(True, "use")
        out = auto_use_selected(
            object(),
            GroceryConfig(use_names=["目标"]),
            names=None,
            continuous=False,
        )
        self.assertTrue(out.ok)
        use_item.assert_called_once()
        self.assertEqual(use_item.call_args.args[2], 1)

    @patch("app.core.package_api.get_money")
    def test_full_gold_short_circuits_when_sufficient(self, get_money) -> None:
        # one-shot: 当前已达目标则直接返回
        cur = gold_to_copper(44990)
        get_money.return_value = PackageActionResult(True, "money", money=cur)
        out = ensure_full_gold(object(), min_money=cur, continuous=False)
        self.assertTrue(out.ok)
        self.assertEqual(out.money, cur)
        self.assertIn("金充足", out.message)

    @patch("app.core.package_api.list_package_items", return_value=[])
    @patch("app.core.package_api.get_money")
    def test_full_gold_reports_missing_pills(self, get_money, _items) -> None:
        get_money.return_value = PackageActionResult(True, "money", money=0)
        out = ensure_full_gold(
            object(), min_money=gold_to_copper(44990), continuous=False
        )
        self.assertFalse(out.ok)
        self.assertIn("无白云熊胆丸", out.message)
        self.assertEqual(out.money, 0)

    def test_default_full_gold_min_is_44990_gold_in_copper(self) -> None:
        from app.core.package_api import (
            DEFAULT_FULL_GOLD_MIN,
            DEFAULT_FULL_GOLD_MIN_GOLD,
            DEFAULT_REVIVE_PILL_GOLD,
        )

        self.assertEqual(DEFAULT_FULL_GOLD_MIN_GOLD, 44990)
        self.assertEqual(DEFAULT_FULL_GOLD_MIN, 44990 * GOLD_UNIT)
        self.assertEqual(DEFAULT_REVIVE_PILL_GOLD, 10)

    def test_sane_money_filters_garbage_u64(self) -> None:
        from app.core.package_api import _sane_money

        self.assertEqual(_sane_money(998500), 998500)
        self.assertEqual(_sane_money(0), 0)
        # EDX tag mis-combined u64 from live log
        self.assertIsNone(_sane_money(35098938086))
        self.assertIsNone(_sane_money(-1))

    def test_money_bind_is_plus18_trade_is_plus10(self) -> None:
        """绑定=package+0x18 / 非绑=package+0x10."""
        from unittest.mock import MagicMock, patch

        from app.core.package_api import _money_pair_from_package

        blob = bytearray(0x20)
        import struct

        # trade 99.85金, bind 2988.45金 (live pid=6752)
        struct.pack_into("<I", blob, 0x10, 998500)
        struct.pack_into("<I", blob, 0x14, 0)
        struct.pack_into("<I", blob, 0x18, 29884577)
        sess = MagicMock()
        sess.pid = 1
        with (
            patch("app.core.package_api.get_package_ptr", return_value=0x1000),
            patch("app.core.package_api._open_process", return_value=1),
            patch("app.core.package_api._safe_rpm", return_value=bytes(blob)),
            patch("app.core.package_api.kernel32.CloseHandle"),
        ):
            bind, trade, src = _money_pair_from_package(sess, 2)
        self.assertEqual(bind, 29884577)
        self.assertEqual(trade, 998500)
        self.assertIn("bind", src)

        from app.core.package_api import _money_from_package_slot

        with (
            patch("app.core.package_api.get_package_ptr", return_value=0x1000),
            patch("app.core.package_api._open_process", return_value=1),
            patch("app.core.package_api._safe_rpm", return_value=bytes(blob)),
            patch("app.core.package_api.kernel32.CloseHandle"),
        ):
            m, _ = _money_from_package_slot(sess, 2)
        self.assertEqual(m, 29884577)  # 卖满金 primary = bind

    @patch("app.core.package_api.list_package_items")
    @patch("app.core.package_api.sell_item_from_package")
    @patch("app.core.package_api.get_money")
    def test_full_gold_loops_until_target(
        self, get_money, sell_item, list_items
    ) -> None:
        """Sell loops: 0 -> +10金 each call until target 30金 (=3 pills)."""
        price = 10 * GOLD_UNIT
        target = 30 * GOLD_UNIT
        monies = [0, 0, price, 2 * price, 3 * price, 3 * price]
        get_money.side_effect = [
            PackageActionResult(True, "money", money=m) for m in monies
        ]
        sell_item.return_value = PackageActionResult(True, "sell", ret=1)
        list_items.return_value = [
            PackageItem(2, 1, 1, tid=44374, count=99, name="白云熊胆丸")
        ]
        out = ensure_full_gold(
            object(),
            min_money=target,
            pill_price=price,
            continuous=False,
        )
        self.assertTrue(out.ok)
        self.assertGreaterEqual(sell_item.call_count, 3)
        self.assertEqual(out.money, 3 * price)

    def test_full_gold_stop_event_breaks_loop(self) -> None:
        import threading
        from unittest.mock import patch

        stop = threading.Event()
        stop.set()
        with patch("app.core.package_api.get_money") as get_money:
            get_money.return_value = PackageActionResult(True, "money", money=0)
            out = ensure_full_gold(
                object(),
                min_money=gold_to_copper(44990),
                stop_event=stop,
                continuous=True,
            )
        self.assertFalse(out.ok)
        self.assertIn("停止", out.message)

    @patch("app.core.package_api.list_package_items")
    @patch("app.core.package_api.sell_item_from_package")
    @patch("app.core.package_api.get_money")
    def test_full_gold_continuous_refills_after_spend(
        self, get_money, sell_item, list_items
    ) -> None:
        """Guardian: full -> spend -> sell again until stop."""
        import threading

        price = 10 * GOLD_UNIT
        target = 30 * GOLD_UNIT
        # start full, then drop, sell to full, stop mid-guard
        monies = [
            target,  # initial
            target,  # idle recheck still full
            target - price,  # spent
            target,  # after sell
            target,  # idle
        ]
        calls = {"i": 0}
        stop = threading.Event()

        def _gm(*_a, **_k):
            i = calls["i"]
            calls["i"] = i + 1
            m = monies[min(i, len(monies) - 1)]
            if i >= 4:
                stop.set()
            return PackageActionResult(True, "money", money=m)

        get_money.side_effect = _gm
        sell_item.return_value = PackageActionResult(True, "sell", ret=1)
        list_items.return_value = [
            PackageItem(2, 1, 1, tid=44374, count=99, name="白云熊胆丸")
        ]
        out = ensure_full_gold(
            object(),
            min_money=target,
            pill_price=price,
            stop_event=stop,
            continuous=True,
            poll_s=0.01,
            retry_s=0.01,
        )
        self.assertIn("停止", out.message)
        self.assertGreaterEqual(sell_item.call_count, 1)


if __name__ == "__main__":
    unittest.main()
