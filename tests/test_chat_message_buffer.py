# -*- coding: utf-8 -*-
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core.chat_history import (
    ChatMessageBuffer,
    get_chat_message_buffer,
    make_chat_identity,
)
from app.core.game_sys_msg import (
    CAPTCHA_FAIL_TEXT,
    _probe_feedback_hist_delta,
    _probe_feedback_ui_only,
    dump_chat_messages,
)


def _row(text: str, addr: int, seq: int = 0) -> dict:
    row = {
        "text": text,
        "addr": addr,
        "seq": seq,
        "channel": "系统",
        "channel_id": 10,
        "source": "hist:rich",
    }
    row.update(
        make_chat_identity(
            text=text,
            addr=addr,
            channel_id=10,
            seq=seq,
            source="hist:rich",
        )
    )
    return row


class ChatMessageBufferTests(unittest.TestCase):
    def test_dump_defaults_cover_all_message_sources(self) -> None:
        sig = inspect.signature(dump_chat_messages)
        self.assertTrue(sig.parameters["include_heap"].default)
        self.assertTrue(sig.parameters["include_general"].default)
        self.assertEqual(sig.parameters["max_lines"].default, 200)
        self.assertEqual(sig.parameters["budget_s"].default, 2.5)

    def test_appends_only_suffix_after_baseline(self) -> None:
        buf = ChatMessageBuffer()
        base = [_row("消息甲", 0x70001000), _row("消息乙", 0x70001100, 1)]
        cursor = buf.prime(base)
        added = buf.ingest(base + [_row("消息丙", 0x70001200, 2)])
        self.assertEqual([r["text"] for r in added], ["消息丙"])
        self.assertEqual([r["text"] for r in buf.read_after(cursor)], ["消息丙"])

    def test_repeated_same_text_is_preserved(self) -> None:
        text = "答案错误，请大侠重新来过"
        buf = ChatMessageBuffer()
        first = _row(text, 0x70001000)
        cursor = buf.prime([first])
        buf.ingest([first, _row(text, 0x70001200, 1)])
        events = buf.read_after(cursor)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["text"], text)

    def test_buffer_relocation_does_not_replay_history(self) -> None:
        buf = ChatMessageBuffer()
        old = [_row("消息甲", 0x70001000), _row("消息乙", 0x70001100, 1)]
        cursor = buf.prime(old)
        moved = [_row("消息甲", 0x7C001000), _row("消息乙", 0x7C001100, 1)]
        self.assertEqual(buf.ingest(moved), [])
        self.assertEqual(buf.read_after(cursor), [])
        added = buf.ingest(moved + [_row("消息丙", 0x7C001200, 2)])
        self.assertEqual([r["text"] for r in added], ["消息丙"])

    def test_rolling_window_overlap_keeps_new_tail(self) -> None:
        buf = ChatMessageBuffer()
        old = [
            _row("消息甲", 0x70001000),
            _row("消息乙", 0x70001100, 1),
            _row("消息丙", 0x70001200, 2),
        ]
        cursor = buf.prime(old)
        current = [old[1], old[2], _row("消息丁", 0x70001300, 3)]
        buf.ingest(current)
        self.assertEqual([r["text"] for r in buf.read_after(cursor)], ["消息丁"])

    def test_partial_scan_does_not_move_baseline_backwards(self) -> None:
        buf = ChatMessageBuffer()
        old = [
            _row("消息甲", 0x70001000),
            _row("消息乙", 0x70001100, 1),
            _row("消息丙", 0x70001200, 2),
        ]
        cursor = buf.prime(old)
        self.assertEqual(buf.ingest(old[:2]), [])
        self.assertEqual(buf.ingest(old), [])
        self.assertEqual(buf.read_after(cursor), [])

    def test_unrelated_blob_switch_rebaselines_without_replay(self) -> None:
        buf = ChatMessageBuffer()
        cursor = buf.prime([_row("旧消息甲", 0x70001000), _row("旧消息乙", 0x70001100, 1)])
        unrelated = [_row("另一窗口甲", 0x78001000), _row("另一窗口乙", 0x78001100, 1)]
        self.assertEqual(buf.ingest(unrelated), [])
        self.assertEqual(buf.read_after(cursor), [])

    def test_feedback_probe_uses_journal_not_plain_hit_count(self) -> None:
        old = _row("普通旧消息", 0x70001000)
        session = SimpleNamespace(pm=object(), _feedback_hist_baseline={"counts": {}})
        buf = get_chat_message_buffer(session)
        session._feedback_message_cursor = buf.prime([old])
        after = {
            "counts": {"答案错误，请大侠重新来过": 9},
            "instances": [("答案错误，请大侠重新来过", 0x59001000, "residual")],
            "history_rows": [old],
            "sig_hash": "changed-by-plain-residual",
        }
        with patch(
            "app.core.chat_history.snapshot_feedback_fingerprint",
            return_value=after,
        ):
            hit = _probe_feedback_hist_delta(session, log=lambda _m: None)
        self.assertIsNone(hit)

    def test_feedback_probe_reads_repeated_new_rich_message(self) -> None:
        text = "答案错误，请大侠重新来过"
        old = _row(text, 0x70001000)
        new = _row(text, 0x70001200, 1)
        session = SimpleNamespace(pm=object(), _feedback_hist_baseline={"counts": {text: 4}})
        buf = get_chat_message_buffer(session)
        session._feedback_message_cursor = buf.prime([old])
        after = {
            # Count is intentionally unchanged: address-count scanning used to miss this.
            "counts": {text: 4},
            "instances": [(text, old["addr"], "")],
            "history_rows": [old, new],
            "sig_hash": "same-count",
        }
        with patch(
            "app.core.chat_history.snapshot_feedback_fingerprint",
            return_value=after,
        ):
            hit = _probe_feedback_hist_delta(session, log=lambda _m: None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.kind, "fail")
        self.assertEqual(hit.method, "hist_journal_fail")
        with patch(
            "app.core.chat_history.snapshot_feedback_fingerprint",
            return_value=after,
        ):
            repeated = _probe_feedback_hist_delta(session, log=lambda _m: None)
        self.assertIsNone(repeated)

    def test_resident_chat_text_cannot_bypass_active_journal(self) -> None:
        session = SimpleNamespace(pm=object(), _feedback_hist_baseline={"counts": {}})
        scans = [(None, ""), (None, ""), (CAPTCHA_FAIL_TEXT, "chat:Win_ChatInfo")]
        with patch("app.core.game_sys_msg._scan_feedback_ui_text", side_effect=scans):
            hit = _probe_feedback_ui_only(session, allow_heap=False, log=lambda _m: None)
        self.assertIsNone(hit)

    def test_current_toast_remains_valid_fallback(self) -> None:
        session = SimpleNamespace(pm=object(), _feedback_hist_baseline={"counts": {}})
        scans = [(None, ""), (None, ""), (CAPTCHA_FAIL_TEXT, "dlg:Win_SystemInfo")]
        with patch("app.core.game_sys_msg._scan_feedback_ui_text", side_effect=scans):
            hit = _probe_feedback_ui_only(session, allow_heap=False, log=lambda _m: None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.kind, "fail")


if __name__ == "__main__":
    unittest.main()
