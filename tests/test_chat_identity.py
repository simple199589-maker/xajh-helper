# -*- coding: utf-8 -*-
import unittest

from app.core.chat_history import make_chat_identity


class ChatIdentityTests(unittest.TestCase):
    def test_content_key_stable_across_addr_move(self) -> None:
        a = make_chat_identity(text="你获得了100个快乐兑换丹", addr=0x7C08CD9C, channel_id=10)
        b = make_chat_identity(text="你获得了100个快乐兑换丹", addr=0x7C18CD9C, channel_id=10)
        self.assertEqual(a["content_key"], b["content_key"])
        self.assertNotEqual(a["mem_key"], b["mem_key"])
        self.assertEqual(a["uid"], a["mem_key"])

    def test_same_text_different_page_is_different_mem(self) -> None:
        live = make_chat_identity(
            text="答案错误，请大侠重新来过", addr=0x7C08C3F4, channel_id=10, source="hist:rich"
        )
        pool = make_chat_identity(
            text="答案错误，请大侠重新来过", addr=0x592BBCCC, channel_id=10, source="heap:u16"
        )
        self.assertEqual(live["content_key"], pool["content_key"])
        self.assertNotEqual(live["mem_key"], pool["mem_key"])

    def test_item_ids_disambiguate(self) -> None:
        a = make_chat_identity(text="拿到了[item]", addr=1, channel_id=2, item_ids=(101046,))
        b = make_chat_identity(text="拿到了[item]", addr=1, channel_id=2, item_ids=(101047,))
        self.assertNotEqual(a["content_key"], b["content_key"])

    def test_screenshot_neighbor_does_not_change_content_key(self) -> None:
        # 切图是另一行系统消息，不进入本行 content_key
        k1 = make_chat_identity(text="答案正确，请尽快进入活动", addr=0x7C001000, channel_id=10)
        k2 = make_chat_identity(text="答案正确，请尽快进入活动", addr=0x7C001000, channel_id=10)
        self.assertEqual(k1["content_key"], k2["content_key"])


if __name__ == "__main__":
    unittest.main()
