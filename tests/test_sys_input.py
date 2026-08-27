# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.core import sys_input
from app.core.sys_input import (
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    normalize_vk,
    is_extended_vk,
    map_scan,
    send_key_event,
    send_key_press,
)


class SysInputTests(unittest.TestCase):
    def test_normalize_modifiers(self) -> None:
        self.assertEqual(normalize_vk(0x10), 0xA0)
        self.assertEqual(normalize_vk(0x11), 0xA2)
        self.assertEqual(normalize_vk(0x12), 0xA4)
        self.assertEqual(normalize_vk(0x51), 0x51)

    def test_extended_arrows(self) -> None:
        self.assertTrue(is_extended_vk(0x25))
        self.assertFalse(is_extended_vk(0x51))
        self.assertFalse(is_extended_vk(0x20))

    def test_map_scan_space(self) -> None:
        sc = map_scan(0x20)
        self.assertGreaterEqual(sc, 0)
        self.assertLessEqual(sc, 0xFF)

    @patch.object(sys_input.user32, "PostMessageW", return_value=1)
    @patch.object(sys_input.user32, "IsWindow", return_value=True)
    @patch.object(sys_input.user32, "keybd_event")
    @patch.object(sys_input.user32, "SendInput", return_value=2)
    @patch.object(sys_input.user32, "MapVirtualKeyW", return_value=0x10)
    def test_send_key_event_down(
        self, _map, send_input, keybd, _is_win, post
    ) -> None:
        # default: SendInput only (no double-fire)
        r = send_key_event(0x51, key_up=False, hwnd=123)
        self.assertTrue(r.ok)
        self.assertEqual(r.sent, 2)
        send_input.assert_called_once()
        keybd.assert_not_called()
        post.assert_not_called()

    @patch.object(sys_input.user32, "PostMessageW", return_value=1)
    @patch.object(sys_input.user32, "IsWindow", return_value=True)
    @patch.object(sys_input.user32, "keybd_event")
    @patch.object(sys_input.user32, "SendInput", return_value=2)
    @patch.object(sys_input.user32, "MapVirtualKeyW", return_value=0x10)
    def test_send_key_press(
        self, _map, send_input, keybd, _is_win, post
    ) -> None:
        r = send_key_press(0x20, hold_ms=0, hwnd=1)
        self.assertTrue(r.ok)
        # down + up
        self.assertEqual(send_input.call_count, 2)
        keybd.assert_not_called()

    @patch.object(sys_input.user32, "SendInput", return_value=0)
    @patch.object(sys_input.user32, "MapVirtualKeyW", return_value=0x10)
    @patch.object(sys_input.user32, "keybd_event")
    def test_send_input_fail(self, _ke, _map, _si) -> None:
        r = send_key_event(0x51, post_message=False)
        self.assertFalse(r.ok)
        self.assertIn("SendInput", r.error or "")

    def test_invalid_vk(self) -> None:
        r = send_key_event(0)
        self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main()
