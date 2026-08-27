from __future__ import annotations

import struct
import threading
import unittest
from unittest.mock import MagicMock, patch

from app.core.qshop_buy import (
    DEFAULT_MALL_PILL_BAG_TID,
    DEFAULT_MALL_PILL_PACK_TID,
    DEFAULT_MALL_PILL_PRESENT_ID,
    MAX_QSHOP_BUY_COUNT,
    MAX_QSHOP_PILL_GROUPS,
    QSHOP_PILL_GROUP_SIZE,
    QSHOP_PILL_GROUPS_PER_ORDER,
    QShopBuySnapshot,
    buy_qshop_present,
    normalize_qshop_buy_count,
    normalize_qshop_pill_groups,
    pack_present_info,
    qshop_pill_batch_counts,
    qshop_pill_count_from_groups,
    qshop_delivery_complete,
)


class QShopBuyTests(unittest.TestCase):
    def test_fixed_revive_pill_ids_and_large_count(self) -> None:
        self.assertEqual(DEFAULT_MALL_PILL_PRESENT_ID, 635)
        self.assertEqual(DEFAULT_MALL_PILL_PACK_TID, 81997)
        self.assertEqual(DEFAULT_MALL_PILL_BAG_TID, 44374)
        self.assertEqual(QSHOP_PILL_GROUP_SIZE, 9999)
        self.assertEqual(QSHOP_PILL_GROUPS_PER_ORDER, 6)
        self.assertEqual(qshop_pill_count_from_groups(15), 149985)
        self.assertEqual(qshop_pill_batch_counts(10), [59994, 39996])
        self.assertEqual(qshop_pill_batch_counts(15), [59994, 59994, 29997])
        self.assertEqual(qshop_delivery_complete(59994, 59994), (True, 59994))
        self.assertEqual(qshop_delivery_complete(59994, 29917), (False, 29917))
        self.assertEqual(normalize_qshop_pill_groups("15"), 15)
        self.assertEqual(
            normalize_qshop_pill_groups(MAX_QSHOP_PILL_GROUPS + 1),
            MAX_QSHOP_PILL_GROUPS,
        )
        self.assertEqual(normalize_qshop_buy_count(50000), 50000)
        self.assertEqual(
            normalize_qshop_buy_count(MAX_QSHOP_BUY_COUNT + 1),
            MAX_QSHOP_BUY_COUNT,
        )
        self.assertEqual(normalize_qshop_buy_count("bad"), 1)

    def test_present_info_uses_direct_ids(self) -> None:
        info = pack_present_info(
            DEFAULT_MALL_PILL_PRESENT_ID,
            DEFAULT_MALL_PILL_PACK_TID,
        )
        self.assertEqual(struct.unpack_from("<I", info, 0)[0], 635)
        self.assertEqual(struct.unpack_from("<I", info, 4)[0], 81997)

    def test_qshop_stop_sets_current_purchase_event(self) -> None:
        from app.ui.pages._impl import GroceryPage

        page = type(
            "Page",
            (),
            {
                "_busy": True,
                "_active_job_title": "商城买丸",
                "_work_stop": MagicMock(),
                "var_status": MagicMock(),
                "btn_qshop_stop": MagicMock(),
                "_push": MagicMock(),
            },
        )()
        GroceryPage._on_qshop_stop(page)
        page._work_stop.set.assert_called_once()
        page.btn_qshop_stop.configure.assert_called_once_with(state="disabled")
        self.assertIn("当前笔结束后", page.var_status.set.call_args.args[0])

    def test_qshop_stop_does_not_cancel_another_grocery_job(self) -> None:
        from app.ui.pages._impl import GroceryPage

        page = type(
            "Page",
            (),
            {
                "_busy": True,
                "_active_job_title": "刷新背包",
                "_work_stop": MagicMock(),
                "var_status": MagicMock(),
            },
        )()
        GroceryPage._on_qshop_stop(page)
        page._work_stop.set.assert_not_called()

    def test_qshop_stop_prevents_the_next_batch(self) -> None:
        from app.ui.pages._impl import GroceryPage

        stop_event = threading.Event()
        captured_job = None

        def capture_job(_self, _title, job):
            nonlocal captured_job
            captured_job = job

        page = type(
            "Page",
            (),
            {
                "_qshop_groups_from_ui": lambda _self: 15,
                "_run_job": capture_job,
                "_push": MagicMock(),
            },
        )()
        GroceryPage._on_qshop_buy(page)
        self.assertIsNotNone(captured_job)

        delivered = type(
            "Result", (), {"ok": True, "message": "complete", "error": ""}
        )()

        def buy_first_batch(*_args, **_kwargs):
            stop_event.set()
            return delivered

        with patch("app.ui.pages._impl.buy_qshop_present", side_effect=buy_first_batch) as buy, patch(
            "app.ui.pages._impl.refresh_bag", return_value=[]
        ), patch("app.ui.pages._impl.read_money_text", return_value="0"):
            captured_job(object(), MagicMock(), stop_event)

        self.assertEqual(buy.call_count, 1)
        self.assertEqual(buy.call_args.kwargs["count"], 59994)

    def test_explicit_ids_ignore_current_selected_goods_and_keep_six_groups(self) -> None:
        session = type("Session", (), {"pid": 123, "module_base": 0x400000})()
        selected_other_goods = QShopBuySnapshot(
            ok=False,
            qshop_open=False,
            check_open=False,
            present_id=999,
            goods_tid=888,
            count=1,
            check_dlg=0x1234,
        )
        with patch("app.core.qshop_buy._pid_blocked", return_value=(False, "")), patch(
            "app.core.qshop_buy.snapshot_qshop_buy",
            return_value=selected_other_goods,
        ), patch(
            "app.core.qshop_buy.ensure_qshop_check_open",
            return_value=(False, "stop after capture", 0x1234),
        ) as ensure:
            result = buy_qshop_present(
                session,
                count=qshop_pill_batch_counts(15)[0],
                present_id=DEFAULT_MALL_PILL_PRESENT_ID,
                goods_tid=DEFAULT_MALL_PILL_PACK_TID,
                settle_s=0,
            )

        self.assertFalse(result.ok)
        self.assertEqual(ensure.call_args.kwargs["present_id"], 635)
        self.assertEqual(ensure.call_args.kwargs["goods_tid"], 81997)
        self.assertEqual(ensure.call_args.kwargs["count"], 59994)


if __name__ == "__main__":
    unittest.main()
