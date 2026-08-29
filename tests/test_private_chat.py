# -*- coding: utf-8 -*-
"""私聊封包构建 / 私聊 ring 解析 / send_private_message 测试。

金样本来自 2026-08-29 实机抓包（tools/watch_private_capture.py）：
  ZCAP1: 十丶三(0x012B4001) → 苦寒未曾来(0x0009E001)，57B
  ZCAP2: 十丶三(0x012B4001) → 初一(0x012BA001)，51B
@author by ak
"""
import struct
import time
import unittest
from unittest.mock import patch

from app.core import team_chat
from app.core.chat_tap import (
    CHAT_TAP_EVENT_SIZE,
    CHAT_TAP_SHARED_SIZE,
    PRIVATE_TAP_CAPACITY,
    PRIVATE_TAP_EVENTS_OFF,
    PRIVATE_TAP_WRITE_SEQ_OFF,
    parse_private_events,
)
from app.core.team_chat import (
    build_private_chat_c2s,
    private_send_gate_reset,
    send_private_message,
)


ZCAP1 = bytes.fromhex(
    "60" "37" "0000000000" "01" "00000000" "012b4001" "00000000" "0009e001"
    "06" "4153364e094e" "0a" "e682d25b2a67fe666567" "0000" "0a"
    "5a004300410050003100" "0000"
)
ZCAP2 = bytes.fromhex(
    "60" "31" "0000000000" "01" "00000000" "012b4001" "00000000" "012ba001"
    "06" "4153364e094e" "04" "1d52004e" "0000" "0a"
    "5a004300410050003200" "0000"
)


class BuildPrivateChatC2sTests(unittest.TestCase):
    def test_golden_zcap1(self) -> None:
        pkt = build_private_chat_c2s(
            "ZCAP1", 0x012B4001, "十丶三", 0x0009E001, "苦寒未曾来"
        )
        self.assertEqual(pkt, ZCAP1)

    def test_golden_zcap2(self) -> None:
        pkt = build_private_chat_c2s("ZCAP2", 0x012B4001, "十丶三", 0x012BA001, "初一")
        self.assertEqual(pkt, ZCAP2)

    def test_total_field_is_len_minus_two(self) -> None:
        pkt = build_private_chat_c2s("hi", 1, "甲", 2, "乙")
        self.assertEqual(pkt[0], 0x60)
        self.assertEqual(pkt[1], len(pkt) - 2)

    def test_rejects_empty_text(self) -> None:
        with self.assertRaises(ValueError):
            build_private_chat_c2s("", 1, "甲", 2, "乙")


def _private_snapshot(rows: list[tuple[int, int, str]]) -> bytes:
    """rows: (seq, channel, text) 写入私聊 ring。"""
    data = bytearray(CHAT_TAP_SHARED_SIZE)
    newest = rows[-1][0] if rows else 0
    struct.pack_into("<I", data, PRIVATE_TAP_WRITE_SEQ_OFF, newest)
    for seq, _ch, text in rows:
        offset = PRIVATE_TAP_EVENTS_OFF + ((seq - 1) % PRIVATE_TAP_CAPACITY) * CHAT_TAP_EVENT_SIZE
        raw = text.encode("utf-16-le")
        struct.pack_into("<7I", data, offset, seq, 100 + seq, 7, 0x500000, 9, 0, len(text))
        data[offset + 28 : offset + 28 + len(raw)] = raw
    return bytes(data)


class ParsePrivateEventsTests(unittest.TestCase):
    def test_reads_after_cursor(self) -> None:
        data = _private_snapshot([(1, 9, "甲 对你说：A"), (2, 9, "乙 对你说：B")])
        events, cursor, lost, header = parse_private_events(data, 1)
        self.assertEqual([e["text"] for e in events], ["乙 对你说：B"])
        self.assertEqual([e["channel"] for e in events], [9])
        self.assertEqual(cursor, 2)
        self.assertEqual(lost, 0)
        self.assertEqual(header["private_write_seq"], 2)

    def test_reports_overrun_within_capacity(self) -> None:
        rows = [(seq, 9, f"m{seq}") for seq in range(1, 21)]
        data = _private_snapshot(rows)
        events, cursor, lost, _h = parse_private_events(data, 0)
        self.assertEqual(events[0]["seq"], 1)
        self.assertEqual(cursor, 20)
        self.assertEqual(lost, 0)

    def test_eviction_reports_lost(self) -> None:
        # 最新 seq 超过容量 512：cursor=0 时最早的 18 条已被覆盖。
        newest = 530
        rows = [(seq, 9, f"m{seq}") for seq in range(500, newest + 1)]
        data = _private_snapshot(rows)
        events, cursor, lost, _h = parse_private_events(data, 0)
        self.assertEqual(events, [])
        self.assertEqual(lost, newest - PRIVATE_TAP_CAPACITY)
        self.assertEqual(cursor, newest - PRIVATE_TAP_CAPACITY)


class PrivateSendGateTests(unittest.TestCase):
    """私聊发送限频门：3条/窗口，满则阻塞等待，按发送者隔离。@author by ak"""

    def setUp(self) -> None:
        team_chat.private_send_gate_reset()

    def tearDown(self) -> None:
        team_chat.private_send_gate_reset()

    def test_admits_up_to_window_then_blocks(self) -> None:
        with patch.object(team_chat, "PRIVATE_SEND_WINDOW_S", 0.4), patch.object(
            team_chat, "PRIVATE_SEND_WINDOW_MAX", 2
        ):
            waited = [
                team_chat._private_send_gate(111),
                team_chat._private_send_gate(111),
            ]
            self.assertEqual(waited, [0.0, 0.0])
            t0 = time.monotonic()
            waited3 = team_chat._private_send_gate(111)
            blocked = time.monotonic() - t0
            self.assertGreaterEqual(waited3, 0.2)
            self.assertGreaterEqual(blocked, 0.2)

    def test_per_pid_isolation(self) -> None:
        with patch.object(team_chat, "PRIVATE_SEND_WINDOW_S", 5.0), patch.object(
            team_chat, "PRIVATE_SEND_WINDOW_MAX", 1
        ):
            self.assertEqual(team_chat._private_send_gate(1), 0.0)
            # pid=1 窗口满，但 pid=2 不受影响
            self.assertEqual(team_chat._private_send_gate(2), 0.0)
            self.assertGreater(team_chat._private_send_gate(1), 0.0)

    def test_send_private_message_passes_gate(self) -> None:
        team_chat.private_send_gate_reset()
        captured: dict = {}

        def fake_mailbox(pid, plaintext, *, patch_ident=True, log=None):
            captured["gate_wait_note"] = True
            return {"ok": True, "ret": 1}

        def fake_resolve(_pid, *, log=None):
            return 0x012B4001, "十丶三"

        with (
            patch.object(team_chat, "PRIVATE_SEND_WINDOW_S", 5.0),
            patch.object(team_chat, "PRIVATE_SEND_WINDOW_MAX", 1),
            patch("app.core.team_chat._team_mailbox_send", fake_mailbox),
            patch("app.core.team_chat._resolve_private_sender", fake_resolve),
        ):
            r1 = send_private_message(9, 2, "乙", "a")
            t0 = time.monotonic()
            r2 = send_private_message(9, 2, "乙", "b")
            self.assertGreaterEqual(time.monotonic() - t0, 0.2)
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertTrue(captured["gate_wait_note"])


class SendPrivateMessageTests(unittest.TestCase):
    def test_refuses_without_target(self) -> None:
        res = send_private_message(1, 0, "某角色", "hi")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "bad target/text")

    def test_refuses_without_pid(self) -> None:
        res = send_private_message(0, 2, "某角色", "hi")
        self.assertFalse(res["ok"])

    def test_builds_payload_without_ident_patch_and_sends(self) -> None:
        captured: dict = {}

        def fake_mailbox(pid, plaintext, *, patch_ident=True, log=None):
            captured["pid"] = pid
            captured["payload"] = plaintext
            captured["patch_ident"] = patch_ident
            return {"ok": True, "ret": 1, "mgr": 0x1234}

        sent: list[dict] = []

        def fake_resolve(_pid, *, log=None):
            return 0x012B4001, "十丶三"

        with (
            patch("app.core.team_chat._team_mailbox_send", fake_mailbox),
            patch("app.core.team_chat._resolve_private_sender", fake_resolve),
        ):
            res = send_private_message(
                6456, 0x012BA001, "初一", "PRIV-OK1", log=lambda m: sent.append(m)
            )
        self.assertTrue(res["ok"])
        self.assertEqual(captured["pid"], 6456)
        self.assertFalse(captured["patch_ident"])
        self.assertEqual(
            captured["payload"],
            build_private_chat_c2s("PRIV-OK1", 0x012B4001, "十丶三", 0x012BA001, "初一"),
        )

    def test_refuses_when_sender_unresolved(self) -> None:
        with (
            patch("app.core.team_chat._team_mailbox_send") as mailbox,
            patch(
                "app.core.team_chat._resolve_private_sender",
                return_value=(0, None),
            ),
        ):
            res = send_private_message(6456, 0x012BA001, "初一", "hi")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "sender rid unresolved")
        mailbox.assert_not_called()


if __name__ == "__main__":
    unittest.main()
