import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core.chat_tap import (
    CHAT_TAP_ACTIVE,
    CHAT_TAP_CAPACITY,
    CHAT_TAP_EVENT_SIZE,
    CHAT_TAP_HEADER_SIZE,
    CHAT_TAP_MAGIC,
    CHAT_TAP_SHARED_SIZE,
    CHAT_TAP_VERSION,
    parse_chat_tap_snapshot,
)
from app.core.game_sys_msg import (
    _probe_feedback_tap_delta,
    probe_captcha_answer_text,
    snapshot_feedback_heap_baseline,
)


def _snapshot(events: list[tuple[int, str]], newest: int | None = None) -> bytes:
    data = bytearray(CHAT_TAP_SHARED_SIZE)
    newest = int(newest if newest is not None else (events[-1][0] if events else 0))
    struct.pack_into(
        "<7I128s",
        data,
        0,
        CHAT_TAP_MAGIC,
        CHAT_TAP_VERSION,
        CHAT_TAP_SHARED_SIZE,
        CHAT_TAP_CAPACITY,
        newest,
        CHAT_TAP_ACTIVE,
        0x885300,
        b"",
    )
    for seq, text in events:
        offset = CHAT_TAP_HEADER_SIZE + ((seq - 1) % CHAT_TAP_CAPACITY) * CHAT_TAP_EVENT_SIZE
        raw = text.encode("utf-16-le")
        struct.pack_into("<7I", data, offset, seq, 100 + seq, 7, 0x500000, 10, 0, len(text))
        data[offset + 28 : offset + 28 + len(raw)] = raw
    return bytes(data)


class ChatTapTests(unittest.TestCase):
    def test_active_tap_skips_legacy_preconfirm_scans(self) -> None:
        class Reader:
            latest_cursor = 37

            @staticmethod
            def header():
                return {"status": CHAT_TAP_ACTIVE, "target_va": 0x885300}

        session = SimpleNamespace(pid=123, _chat_tap_reader=Reader())
        with (
            patch("app.core.game_sys_msg._scan_writable_heap_markers") as heap,
            patch("app.core.chat_history.snapshot_feedback_fingerprint") as hist,
        ):
            snapshot_feedback_heap_baseline(session)
        self.assertEqual(session._feedback_tap_cursor, 37)
        self.assertTrue(session._feedback_tap_fast_active)
        heap.assert_not_called()
        hist.assert_not_called()

    def test_active_tap_without_event_skips_legacy_pollers(self) -> None:
        class Reader:
            @staticmethod
            def read_after(cursor):
                return [], cursor, 0, {"status": CHAT_TAP_ACTIVE}

        session = SimpleNamespace(
            pm=object(),
            _feedback_tap_cursor=10,
            _feedback_tap_fast_active=True,
            _chat_tap_reader=Reader(),
        )
        with (
            patch("app.core.game_sys_msg._probe_feedback_hist_delta") as hist,
            patch("app.core.game_sys_msg._probe_feedback_ui_only") as ui,
        ):
            hit = probe_captcha_answer_text(session)
        self.assertIsNone(hit)
        self.assertTrue(session._feedback_tap_fast_active)
        hist.assert_not_called()
        ui.assert_not_called()

    def test_tap_overrun_restores_legacy_pollers(self) -> None:
        class Reader:
            @staticmethod
            def read_after(cursor):
                return [], cursor, 1, {"status": CHAT_TAP_ACTIVE}

        session = SimpleNamespace(
            pm=object(),
            _feedback_tap_cursor=10,
            _feedback_tap_fast_active=True,
            _chat_tap_reader=Reader(),
            _feedback_hist_baseline=None,
        )
        with (
            patch("app.core.game_sys_msg._probe_feedback_hist_delta", return_value=None),
            patch("app.core.game_sys_msg._probe_feedback_ui_only", return_value=None) as ui,
        ):
            hit = probe_captcha_answer_text(session)
        self.assertIsNone(hit)
        self.assertFalse(session._feedback_tap_fast_active)
        ui.assert_called_once()

    def test_reads_committed_events_after_cursor(self) -> None:
        events, cursor, lost, _header = parse_chat_tap_snapshot(
            _snapshot([(1, "旧"), (2, "答案正确")]), 1
        )
        self.assertEqual([event["text"] for event in events], ["答案正确"])
        self.assertEqual(cursor, 2)
        self.assertEqual(lost, 0)

    def test_stops_before_reserved_but_uncommitted_slot(self) -> None:
        events, cursor, lost, _header = parse_chat_tap_snapshot(
            _snapshot([(1, "已提交")], newest=2), 0
        )
        self.assertEqual([event["seq"] for event in events], [1])
        self.assertEqual(cursor, 1)
        self.assertEqual(lost, 0)

    def test_reports_overrun_and_keeps_last_fifty(self) -> None:
        rows = [(seq, f"m{seq}") for seq in range(11, 61)]
        events, cursor, lost, _header = parse_chat_tap_snapshot(
            _snapshot(rows, newest=60), 1
        )
        self.assertEqual(events[0]["seq"], 11)
        self.assertEqual(events[-1]["seq"], 60)
        self.assertEqual(cursor, 60)
        self.assertEqual(lost, 9)

    @staticmethod
    def _probe(events: list[dict], *, trust_hard: bool = False):
        class Reader:
            def read_after(self, cursor):
                return events, events[-1]["seq"] if events else cursor, 0, {}

        session = SimpleNamespace(
            _feedback_tap_cursor=10,
            _chat_tap_reader=Reader(),
        )
        return _probe_feedback_tap_delta(
            session,
            trust_chat_hard=trust_hard,
        )

    def test_tap_feedback_returns_explicit_fail(self) -> None:
        hit = self._probe(
            [{"seq": 11, "channel": 10, "caller_va": 1, "text": "答案错误，请大侠重新来过"}]
        )
        self.assertEqual(hit.kind, "fail")
        self.assertEqual(hit.method, "chat_tap_fail:seq=11")

    def test_tap_feedback_uses_exact_last_answer_order(self) -> None:
        hit = self._probe(
            [
                {"seq": 11, "channel": 10, "caller_va": 1, "text": "答案错误，请大侠重新来过"},
                {"seq": 12, "channel": 10, "caller_va": 1, "text": "答案正确，请尽快进入活动"},
            ]
        )
        self.assertEqual(hit.kind, "ok")
        self.assertEqual(hit.method, "chat_tap_ok:seq=12")

    def test_tap_feedback_preserves_ok_then_hard_block(self) -> None:
        hit = self._probe(
            [
                {"seq": 11, "channel": 10, "caller_va": 1, "text": "答案正确，请尽快进入活动"},
                {"seq": 12, "channel": 11, "caller_va": 1, "text": "队伍成员处于神罚状态，不能进入副本"},
            ]
        )
        self.assertEqual(hit.kind, "block")
        self.assertEqual(hit.method, "chat_tap_hard:seq=12")
        self.assertIn("答案正确", hit.error)


if __name__ == "__main__":
    unittest.main()
