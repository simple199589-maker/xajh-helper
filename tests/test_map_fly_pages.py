from __future__ import annotations

import unittest
from unittest.mock import patch


class MapFlyPageTests(unittest.TestCase):
    def test_fixed_page_model_is_default_plus_six_custom_pages(self):
        from app.core.map_fly import (
            FIXED_CUSTOM_FLY_PAGES,
            packet_page_for_radio_index,
            packet_page_to_ui_page,
            ui_page_to_packet_page,
        )

        self.assertEqual(FIXED_CUSTOM_FLY_PAGES, (0, 1, 2, 3, 4, 5))
        self.assertEqual(packet_page_for_radio_index(0), 0xFF)
        for page in FIXED_CUSTOM_FLY_PAGES:
            self.assertEqual(ui_page_to_packet_page(page), page)
            self.assertEqual(packet_page_to_ui_page(page), page)
            self.assertEqual(packet_page_for_radio_index(page + 1), page)

    def test_missing_fly_manager_is_retryable_not_successful_empty_page(self):
        from app.core.map_fly import list_mem_transmit_page_slots

        session = object()
        with patch("app.core.map_fly.get_fly_manager_ptr", return_value=0):
            result = list_mem_transmit_page_slots(session, 2)

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "fly_mgr_unavailable")
        self.assertEqual((result.detail or {}).get("slots"), [])
        self.assertEqual((result.detail or {}).get("ui_page"), 2)

    def test_absent_fixed_page_object_is_a_real_empty_page(self):
        from app.core.map_fly import list_mem_transmit_page_slots

        session = object()
        with patch("app.core.map_fly.get_fly_manager_ptr", return_value=0x12340000), patch(
            "app.core.map_fly._iter_fly_page_map", return_value=[(0, 0x20000000)]
        ), patch("app.core.map_fly._find_page_object_ptr", return_value=0):
            result = list_mem_transmit_page_slots(session, 2)

        self.assertTrue(result.ok)
        self.assertIsNone(result.error)
        slots = (result.detail or {}).get("slots") or []
        self.assertEqual(len(slots), 10)
        self.assertTrue(all(slot.get("empty") for slot in slots))

    def test_empty_page_directory_is_a_real_empty_fixed_page(self):
        from app.core.map_fly import list_mem_transmit_page_slots

        session = object()
        with patch("app.core.map_fly.get_fly_manager_ptr", return_value=0x12340000), patch(
            "app.core.map_fly._iter_fly_page_map", return_value=[]
        ), patch("app.core.map_fly._find_page_object_ptr", return_value=0):
            result = list_mem_transmit_page_slots(session, 1)

        self.assertTrue(result.ok)
        self.assertIsNone(result.error)
        slots = (result.detail or {}).get("slots") or []
        self.assertEqual(len(slots), 10)
        self.assertTrue(all(slot.get("empty") for slot in slots))
        self.assertEqual((result.detail or {}).get("packet_page"), 1)

    def test_out_of_range_custom_page_is_rejected(self):
        from app.core.map_fly import list_transmit_page_slots

        result = list_transmit_page_slots(object(), 7, page_id=6, is_default=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bad_page")


if __name__ == "__main__":
    unittest.main()
