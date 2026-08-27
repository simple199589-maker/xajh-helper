from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.aui_click import (
    AuiCtrlRect,
    captcha_btn_ok_hit_rect,
    captcha_cell_rect_from_img,
    click_captcha_btn_ok,
)
from app.core.captcha_client import CaptchaIdentifyResult, identify_image, send_feedback
from app.core.captcha_dialog import (
    DialogBox,
    clamp_dialog_confirm_point,
    crop_dialog_png,
    dialog_cell_point,
    dialog_confirm_point,
    dialog_confirm_rect,
)
from app.core.human_input import jitter_around
from app.core.dlg_image_export import (
    _crop_activity_dialog_from_full,
    _save_export_failure_debug,
)
from app.core.game_sys_msg import (
    BUYU_BLOCK_TEXT,
    CAPTCHA_FAIL_TEXT,
    CAPTCHA_OK_MSG_ID,
    CAPTCHA_OK_TEXT,
    CaptchaAnswerFeedback,
    ENTRY_OPEN_BLOCK_TEXT,
    SHENFA_BLOCK_TEXT,
    is_entry_open_block_feedback,
    is_explicit_captcha_ok_feedback,
    is_fresh_entry_open_block_feedback,
    is_shenfa_block_feedback,
    probe_captcha_answer_text,
    probe_entry_open_block_text,
    wait_captcha_answer_feedback,
    _OK_SHORT_TEXT,
    _FAIL_SHORT_TEXT,
    _SHENFA_SHORT_TEXT,
    _BUYU_SHORT_TEXT,
    _OK_SHORT_TAIL,
)
from app.core.plg_ui import DlgRect, wait_dlg_show
from app.core.yaolu_auto import (
    DialogCapture,
    YaoluConfig,
    describe_captcha_error,
    solve_captcha_dialog,
)


class CaptchaAndUiGeometryTests(unittest.TestCase):
    def test_team_entry_block_is_retryable_but_shenfa_is_not(self) -> None:
        soft = CaptchaAnswerFeedback(
            kind="block",
            text=ENTRY_OPEN_BLOCK_TEXT,
            method="entry_u16_hits=2 baseline=1",
        )
        shenfa = CaptchaAnswerFeedback(
            kind="block",
            text="队伍成员处于神罚状态，不能进入副本",
            method="dlg:Win_Popmsg",
        )
        buyu = CaptchaAnswerFeedback(
            kind="block",
            text="队伍成员处于捕羽状态，不能进入副本",
            method="dlg:Win_Popmsg",
        )
        self.assertTrue(is_entry_open_block_feedback(soft))
        self.assertFalse(is_shenfa_block_feedback(soft))
        self.assertFalse(is_entry_open_block_feedback(shenfa))
        self.assertTrue(is_shenfa_block_feedback(shenfa))
        self.assertFalse(is_entry_open_block_feedback(buyu))
        self.assertTrue(is_shenfa_block_feedback(buyu))
        # Screenshot chat form: 神罚状态 mid-string still hard-stops.
        self.assertTrue(
            is_shenfa_block_feedback(
                CaptchaAnswerFeedback(
                    kind="block",
                    text="队伍成员处于神罚状态，不能进入副本",
                    method="dlg:Win_Popmsg",
                )
            )
        )

    @patch("app.core.game_sys_msg.probe_entry_open_block_text")
    @patch("app.core.game_sys_msg._scan_feedback_ui_text")
    def test_captcha_fail_has_priority_over_stale_entry_block(self, ui_scan, entry) -> None:
        entry.return_value = CaptchaAnswerFeedback(
            kind="block", text=ENTRY_OPEN_BLOCK_TEXT, method="entry_u16_hits=2"
        )

        def _ui(_session, keys, **_kwargs):
            if "答案错误" in keys or CAPTCHA_FAIL_TEXT in keys:
                return CAPTCHA_FAIL_TEXT, "dlg:Win_Popmsg"
            return None, ""

        ui_scan.side_effect = _ui
        hit = probe_captcha_answer_text(MagicMock(pm=object()))
        self.assertEqual(hit.kind, "fail")
        self.assertEqual(hit.text, CAPTCHA_FAIL_TEXT)
        entry.assert_not_called()

    @patch("app.core.game_sys_msg.probe_entry_open_block_text", return_value=None)
    @patch("app.core.game_sys_msg._scan_feedback_ui_text", return_value=(None, ""))
    @patch("app.core.game_sys_msg._scan_text_hits")
    def test_stale_fail_hits_ignored_without_delta(self, scan, _ui, _entry) -> None:
        # Absolute fail hits=6 (stale catalog) must NOT invent 答案错误.
        def _scan(_pm, text, limit=12):
            if text == CAPTCHA_FAIL_TEXT:
                return list(range(6))
            if text == CAPTCHA_OK_TEXT:
                return [1]
            return []

        scan.side_effect = _scan
        base = {
            CAPTCHA_FAIL_TEXT: 6,
            CAPTCHA_OK_TEXT: 1,
            SHENFA_BLOCK_TEXT: 0,
            BUYU_BLOCK_TEXT: 0,
            _BUYU_SHORT_TEXT: 0,
            _SHENFA_SHORT_TEXT: 0,
            _OK_SHORT_TEXT: 1,
            _OK_SHORT_TAIL: 1,
            _FAIL_SHORT_TEXT: 6,
        }
        hit = probe_captcha_answer_text(
            MagicMock(pm=object()), answer_baseline=base
        )
        self.assertIsNone(hit)

    @patch("app.core.game_sys_msg.probe_entry_open_block_text", return_value=None)
    @patch("app.core.game_sys_msg._scan_feedback_ui_text", return_value=(None, ""))
    @patch("app.core.game_sys_msg._scan_text_hits")
    def test_ok_then_shenfa_delta_is_block_not_fail(self, scan, _ui, _entry) -> None:
        # Live: 答案正确 + 神罚 new copies; stale fail stays at baseline.
        counts = {
            CAPTCHA_FAIL_TEXT: 6,
            CAPTCHA_OK_TEXT: 2,
            SHENFA_BLOCK_TEXT: 1,
            BUYU_BLOCK_TEXT: 0,
            _BUYU_SHORT_TEXT: 0,
            _SHENFA_SHORT_TEXT: 1,
            _OK_SHORT_TEXT: 2,
            _OK_SHORT_TAIL: 2,
            _FAIL_SHORT_TEXT: 6,
        }

        def _scan(_pm, text, limit=12):
            n = int(counts.get(text, 0))
            return list(range(n))

        scan.side_effect = _scan
        base = {
            CAPTCHA_FAIL_TEXT: 6,
            CAPTCHA_OK_TEXT: 1,
            SHENFA_BLOCK_TEXT: 0,
            BUYU_BLOCK_TEXT: 0,
            _BUYU_SHORT_TEXT: 0,
            _SHENFA_SHORT_TEXT: 0,
            _OK_SHORT_TEXT: 1,
            _OK_SHORT_TAIL: 1,
            _FAIL_SHORT_TEXT: 6,
        }
        hit = probe_captcha_answer_text(
            MagicMock(pm=object()), answer_baseline=base
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.kind, "block")
        self.assertIn("神罚", hit.text or "")
        self.assertNotEqual(hit.kind, "fail")
        # ok_also carried when short/full ok also moved.
        self.assertEqual(hit.error, CAPTCHA_OK_TEXT)
        self.assertFalse(
            is_entry_open_block_feedback(
                CaptchaAnswerFeedback(
                    kind="block",
                    text="队伍成员处于神罚状态，不能进入副本",
                    method="dlg:Win_Popmsg",
                )
            )
        )
        self.assertFalse(
            is_explicit_captcha_ok_feedback(
                CaptchaAnswerFeedback(
                    kind="block",
                    text=ENTRY_OPEN_BLOCK_TEXT,
                    method="entry_u16_hits=2 baseline=1",
                )
            )
        )

    @patch("app.core.game_sys_msg._scan_writable_heap_markers", return_value={})
    @patch("app.core.game_sys_msg.probe_entry_open_block_text", return_value=None)
    @patch("app.core.game_sys_msg._scan_text_hits")
    @patch("app.core.game_sys_msg._scan_feedback_ui_text")
    def test_chat_ui_ok_and_shenfa_same_frame_is_block(
        self, ui_scan, scan, _entry, _heap
    ) -> None:
        """Screenshot form: 系统 答案正确 + 其他 神罚 on chat channels."""

        def _ui(_session, keys, **_kwargs):
            if any(k in keys for k in ("神罚状态", "神罚", SHENFA_BLOCK_TEXT)):
                return SHENFA_BLOCK_TEXT, "chat:Win_ChatInfo"
            if any(k in keys for k in ("答案正确", CAPTCHA_OK_TEXT)):
                return CAPTCHA_OK_TEXT, "chat:Win_ChatInfo"
            return None, ""

        ui_scan.side_effect = _ui
        hit = probe_captcha_answer_text(MagicMock(pm=object()), heavy=False)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.kind, "block")
        self.assertIn("神罚", hit.text or "")
        self.assertTrue(is_shenfa_block_feedback(hit))
        # Proven 答案正确 kept on error so runner can mark identify correct.
        self.assertEqual(hit.error, CAPTCHA_OK_TEXT)
        scan.assert_not_called()

    @patch("app.core.game_sys_msg._scan_writable_heap_markers", return_value={})
    @patch("app.core.game_sys_msg.probe_entry_open_block_text", return_value=None)
    @patch("app.core.game_sys_msg._scan_text_hits")
    @patch("app.core.game_sys_msg._scan_feedback_ui_text")
    def test_chat_residual_shenfa_alone_not_hard_stop(
        self, ui_scan, scan, _entry, _heap
    ) -> None:
        """Old chat 神罚 without concurrent 答案正确 must not invent a hard stop."""

        def _ui(_session, keys, **_kwargs):
            if any(k in keys for k in ("神罚状态", "神罚", SHENFA_BLOCK_TEXT)):
                return SHENFA_BLOCK_TEXT, "chat:Win_ChatInfo"
            return None, ""

        ui_scan.side_effect = _ui
        # No u16 delta either.
        scan.side_effect = lambda _pm, text, limit=12: []
        hit = probe_captcha_answer_text(
            MagicMock(pm=object()),
            answer_baseline={
                CAPTCHA_FAIL_TEXT: 0,
                CAPTCHA_OK_TEXT: 0,
                SHENFA_BLOCK_TEXT: 0,
                BUYU_BLOCK_TEXT: 0,
                _BUYU_SHORT_TEXT: 0,
                _SHENFA_SHORT_TEXT: 0,
                _OK_SHORT_TEXT: 0,
                _OK_SHORT_TAIL: 0,
                _FAIL_SHORT_TEXT: 0,
            },
            heavy=True,
        )
        self.assertIsNone(hit)

        # After stage1 答案正确, trust_chat_hard must hard-stop on chat-only 神罚.
        hit2 = probe_captcha_answer_text(
            MagicMock(pm=object()),
            heavy=False,
            trust_chat_hard=True,
        )
        self.assertIsNotNone(hit2)
        self.assertEqual(hit2.kind, "block")
        self.assertIn("神罚", hit2.text or "")
        self.assertIn("trust_stage1", hit2.method or "")

    def test_heap_marker_hits_classify_ok_and_hard(self) -> None:
        from app.core.game_sys_msg import _feedback_from_marker_hits

        ok = _feedback_from_marker_hits({"答案正确": True, "答案错误": False, "神罚状态": False, "捕羽状态": False, "请尽快进入活动": False})
        self.assertIsNotNone(ok)
        self.assertEqual(ok.kind, "ok")

        hard = _feedback_from_marker_hits(
            {"答案正确": True, "答案错误": False, "神罚状态": True, "捕羽状态": False, "请尽快进入活动": False}
        )
        self.assertEqual(hard.kind, "block")
        self.assertIn("神罚", hard.text or "")
        self.assertEqual(hard.error, CAPTCHA_OK_TEXT)

        residual = _feedback_from_marker_hits(
            {"答案正确": False, "答案错误": False, "神罚状态": True, "捕羽状态": False, "请尽快进入活动": False}
        )
        self.assertIsNone(residual)

        trusted = _feedback_from_marker_hits(
            {"答案正确": False, "答案错误": False, "神罚状态": True, "捕羽状态": False, "请尽快进入活动": False},
            trust_chat_hard=True,
        )
        self.assertEqual(trusted.kind, "block")

        # Live bug: residual ok+hard must not beat a fail delta.
        fail_wins = _feedback_from_marker_hits(
            {
                "答案正确": True,
                "答案错误": True,
                "神罚状态": True,
                "捕羽状态": False,
                "请尽快进入活动": True,
            }
        )
        self.assertIsNotNone(fail_wins)
        self.assertEqual(fail_wins.kind, "fail")



    def test_pre_confirm_baseline_allows_first_tick_delta(self) -> None:
        from unittest.mock import MagicMock, patch
        from app.core.game_sys_msg import _probe_feedback_ui_only

        session = MagicMock()
        session._feedback_hist_baseline = None
        session._feedback_hist_last_delta_empty = False
        session._feedback_heap_last_scan_stats = {}
        session._feedback_heap_baseline = {
            "答案正确": 3,
            "答案错误": 5,
            "神罚状态": 3,
            "捕羽状态": 0,
            "请尽快进入活动": 3,
        }
        session._feedback_heap_baseline_src = "pre_confirm"
        counts = {
            "答案正确": 3,
            "答案错误": 6,
            "神罚状态": 3,
            "捕羽状态": 0,
            "请尽快进入活动": 3,
        }
        samples = {"答案错误": "答案错误，请大侠重新来过"}
        raw = {k: True for k in counts}

        def _scan(session, budget_s=0.55, sample_out=None, count_out=None, **kw):
            if sample_out is not None:
                sample_out.update(samples)
            if count_out is not None:
                count_out.update(counts)
            return raw

        with patch(
            "app.core.game_sys_msg._scan_feedback_ui_text", return_value=(None, "")
        ), patch(
            "app.core.game_sys_msg._scan_writable_heap_markers", side_effect=_scan
        ):
            decision = _probe_feedback_ui_only(
                session, allow_heap=True, log=lambda m: None
            )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.kind, "fail")

    def test_heap_baseline_tick_ignores_residual_together(self) -> None:
        """Baseline tick must not hard-stop on residual ok+hard history."""
        from unittest.mock import MagicMock, patch
        from app.core.game_sys_msg import _probe_feedback_ui_only

        session = MagicMock()
        # No baseline yet.
        session._feedback_heap_baseline = None
        counts = {
            "答案正确": 3,
            "答案错误": 5,
            "神罚状态": 3,
            "捕羽状态": 0,
            "请尽快进入活动": 3,
        }
        samples = {
            "答案正确": "答案正确，请尽快进入活动",
            "神罚状态": "成员处于神罚状态，不能进入副本",
            "__together__": "ok+hard",
        }
        raw = {k: True for k in counts}

        def _scan(session, budget_s=0.55, sample_out=None, count_out=None, **kw):
            if sample_out is not None:
                sample_out.update(samples)
            if count_out is not None:
                count_out.update(counts)
            return raw

        with patch("app.core.game_sys_msg._scan_feedback_ui_text", return_value=(None, "")), patch(
            "app.core.game_sys_msg._scan_writable_heap_markers", side_effect=_scan
        ):
            decision = _probe_feedback_ui_only(session, allow_heap=True, log=lambda m: None)
        self.assertIsNone(decision)
        self.assertIsInstance(session._feedback_heap_baseline, dict)
        self.assertEqual(session._feedback_heap_baseline.get("答案正确"), 3)

    def test_heap_fail_delta_beats_residual_hard_ok(self) -> None:
        """After baseline, only count growth decides; fail > hard+ok."""
        from unittest.mock import MagicMock, patch
        from app.core.game_sys_msg import _probe_feedback_ui_only

        session = MagicMock()
        session._feedback_heap_baseline_src = "pre_confirm"
        session._feedback_hist_baseline = None
        session._feedback_hist_last_delta_empty = False
        session._feedback_heap_last_scan_stats = {}
        session._feedback_heap_baseline = {
            "答案正确": 3,
            "答案错误": 5,
            "神罚状态": 3,
            "捕羽状态": 0,
            "请尽快进入活动": 3,
        }
        # Only 答案错误 grew (this submit failed).
        counts = {
            "答案正确": 3,
            "答案错误": 6,
            "神罚状态": 3,
            "捕羽状态": 0,
            "请尽快进入活动": 3,
        }
        samples = {
            "答案错误": "答案错误，请大侠重新来过",
            "答案正确": "答案正确，请尽快进入活动",
            "神罚状态": "成员处于神罚状态",
            "__together__": "ok+hard",  # residual same-buffer still present
        }
        raw = {k: True for k in counts}

        def _scan(session, budget_s=0.55, sample_out=None, count_out=None, **kw):
            if sample_out is not None:
                sample_out.update(samples)
            if count_out is not None:
                count_out.update(counts)
            return raw

        with patch("app.core.game_sys_msg._scan_feedback_ui_text", return_value=(None, "")), patch(
            "app.core.game_sys_msg._scan_writable_heap_markers", side_effect=_scan
        ):
            decision = _probe_feedback_ui_only(session, allow_heap=True, log=lambda m: None)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.kind, "fail")

    def test_heap_ok_hard_deltas_are_hard_stop(self) -> None:
        """True live ok+hard growth still hard-stops."""
        from unittest.mock import MagicMock, patch
        from app.core.game_sys_msg import _probe_feedback_ui_only

        session = MagicMock()
        session._feedback_heap_baseline_src = "pre_confirm"
        session._feedback_hist_baseline = None
        session._feedback_hist_last_delta_empty = False
        session._feedback_heap_last_scan_stats = {}
        session._feedback_heap_baseline = {
            "答案正确": 3,
            "答案错误": 5,
            "神罚状态": 3,
            "捕羽状态": 0,
            "请尽快进入活动": 3,
        }
        counts = {
            "答案正确": 4,
            "答案错误": 5,
            "神罚状态": 4,
            "捕羽状态": 0,
            "请尽快进入活动": 4,
        }
        samples = {
            "答案正确": "答案正确，请尽快进入活动",
            "神罚状态": "成员处于神罚状态，不能进入副本",
            "__together__": "ok+hard",
        }
        raw = {k: True for k in counts}

        def _scan(session, budget_s=0.55, sample_out=None, count_out=None, **kw):
            if sample_out is not None:
                sample_out.update(samples)
            if count_out is not None:
                count_out.update(counts)
            return raw

        with patch("app.core.game_sys_msg._scan_feedback_ui_text", return_value=(None, "")), patch(
            "app.core.game_sys_msg._scan_writable_heap_markers", side_effect=_scan
        ):
            decision = _probe_feedback_ui_only(session, allow_heap=True, log=lambda m: None)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.kind, "block")
        self.assertIn("神罚", decision.text or "")

    def test_inline_utf16_and_gbk_key_scan(self) -> None:

        from app.core.game_sys_msg import _scan_inline_text_keys

        u16 = ("答案正确，请尽快进入活动").encode("utf-16le")
        self.assertIn(
            "答案正确",
            _scan_inline_text_keys(u16, ("答案正确",)) or "",
        )
        gbk = ("队伍成员处于神罚状态，不能进入副本").encode("gbk")
        self.assertIn(
            "神罚",
            _scan_inline_text_keys(gbk, ("神罚状态", "神罚")) or "",
        )

    @patch("app.core.game_sys_msg.probe_entry_open_block_text", return_value=None)
    @patch("app.core.game_sys_msg._scan_feedback_ui_text", return_value=(None, ""))
    @patch("app.core.game_sys_msg._scan_text_hits")
    def test_short_ok_marker_delta_alone_is_ok(self, scan, _ui, _entry) -> None:
        # Chat may only bump short "答案正确" without a new full-sentence copy.
        counts = {
            CAPTCHA_FAIL_TEXT: 1,
            CAPTCHA_OK_TEXT: 1,
            SHENFA_BLOCK_TEXT: 0,
            BUYU_BLOCK_TEXT: 0,
            _BUYU_SHORT_TEXT: 0,
            _SHENFA_SHORT_TEXT: 0,
            _OK_SHORT_TEXT: 2,
            _OK_SHORT_TAIL: 1,
            _FAIL_SHORT_TEXT: 1,
        }

        def _scan(_pm, text, limit=12):
            return list(range(int(counts.get(text, 0))))

        scan.side_effect = _scan
        base = {
            CAPTCHA_FAIL_TEXT: 1,
            CAPTCHA_OK_TEXT: 1,
            SHENFA_BLOCK_TEXT: 0,
            BUYU_BLOCK_TEXT: 0,
            _BUYU_SHORT_TEXT: 0,
            _SHENFA_SHORT_TEXT: 0,
            _OK_SHORT_TEXT: 1,
            _OK_SHORT_TAIL: 1,
            _FAIL_SHORT_TEXT: 1,
        }
        hit = probe_captcha_answer_text(
            MagicMock(pm=object()), answer_baseline=base
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.kind, "ok")
        self.assertTrue(is_explicit_captcha_ok_feedback(hit))

    def test_bare_cannot_enter_is_not_hard_shenfa(self) -> None:
        soft_tail = CaptchaAnswerFeedback(
            kind="block",
            text="暂时不能进入副本",
            method="dlg:Win_Popmsg",
        )
        self.assertFalse(is_shenfa_block_feedback(soft_tail))
        self.assertTrue(
            is_shenfa_block_feedback(
                CaptchaAnswerFeedback(
                    kind="block",
                    text=BUYU_BLOCK_TEXT,
                    method="dlg:Win_Popmsg",
                )
            )
        )
        self.assertTrue(
            is_explicit_captcha_ok_feedback(
                CaptchaAnswerFeedback(
                    kind="ok",
                    text=CAPTCHA_OK_TEXT,
                    msg_id=CAPTCHA_OK_MSG_ID,
                    method="dlg:Win_Popmsg",
                )
            )
        )

    def test_fresh_entry_block_and_explicit_ok_helpers(self) -> None:
        self.assertTrue(
            is_fresh_entry_open_block_feedback(
                CaptchaAnswerFeedback(
                    kind="block",
                    text=ENTRY_OPEN_BLOCK_TEXT,
                    method="entry_u16_hits=2 baseline=1",
                )
            )
        )
        self.assertFalse(
            is_fresh_entry_open_block_feedback(
                CaptchaAnswerFeedback(
                    kind="block",
                    text=ENTRY_OPEN_BLOCK_TEXT,
                    method="entry_dlg:Win_Popmsg",
                )
            )
        )

    def test_identify_rejects_empty_image_and_missing_api_key(self) -> None:
        self.assertEqual(identify_image(b"", api_key="x").error, "empty image")
        self.assertEqual(identify_image(b"png", api_key="").error, "未配置答题专用 Key")

    def test_identify_rejects_unicode_key_before_http(self) -> None:
        with patch("app.core.captcha_client._http_json") as http:
            out = identify_image(b"png", api_key="这不是纯Key")
        self.assertFalse(out.ok)
        self.assertIn("包含中文", out.error or "")
        http.assert_not_called()

    @patch("app.core.captcha_client._http_json")
    def test_identify_normalizes_success(self, http) -> None:
        http.return_value = (200, {"code": 0, "data": {
            "positions": ["2", 7], "click_centers": [[1, 2], [3.5, 4]],
            "animal": "猫", "confidence": "0.91", "identify_id": "id-1",
        }}, None)
        out = identify_image(b"png", api_key="key", login_token="token")
        self.assertTrue(out.ok)
        self.assertEqual(out.positions, [2, 7])
        self.assertEqual(out.confidence, 0.91)
        headers = http.call_args.kwargs["headers"]
        self.assertEqual(headers["X-API-Key"], "key")
        self.assertNotIn("Authorization", headers)
        self.assertTrue(headers["X-Timestamp"])
        self.assertTrue(headers["X-Nonce"])
        self.assertTrue(headers["X-Signature"])
        from app.core.license_client import build_token_signature_headers

        body = http.call_args.kwargs["body"]
        expected = build_token_signature_headers(
            "POST",
            "/api/identify/image",
            body,
            "key",
            timestamp=headers["X-Timestamp"],
            nonce=headers["X-Nonce"],
        )
        wrong_token = build_token_signature_headers(
            "POST",
            "/api/identify/image",
            body,
            "token",
            timestamp=headers["X-Timestamp"],
            nonce=headers["X-Nonce"],
        )
        self.assertEqual(headers["X-Signature"], expected["X-Signature"])
        self.assertNotEqual(headers["X-Signature"], wrong_token["X-Signature"])

    @patch("app.core.captcha_client._http_json")
    def test_identify_logs_masked_key_fingerprint(self, http) -> None:
        from app.core.captcha_client import api_key_fingerprint

        http.return_value = (
            200,
            {"code": 0, "data": {"positions": [2, 7]}},
            None,
        )
        logs = []
        out = identify_image(b"png", api_key="answer-key", log=logs.append)
        self.assertTrue(out.ok)
        self.assertIn(api_key_fingerprint("answer-key"), logs[0])
        self.assertNotIn("answer-key", logs[0])
        self.assertIn("token=not-sent signature=key-hmac", logs[0])
        headers = http.call_args.kwargs["headers"]
        self.assertNotIn("Authorization", headers)
        self.assertTrue(headers["X-Timestamp"])
        self.assertTrue(headers["X-Nonce"])
        self.assertTrue(headers["X-Signature"])

    @patch("app.core.captcha_client._http_json", return_value=(503, None, "down"))
    def test_identify_reports_transport_failure(self, _http) -> None:
        out = identify_image(b"png", api_key="key")
        self.assertFalse(out.ok)
        self.assertEqual(out.http_status, 503)
        self.assertEqual(out.error, "HTTP 503: down")

    @patch("app.core.captcha_client._http_json")
    def test_identify_keeps_http_status_in_api_error(self, http) -> None:
        http.return_value = (
            500,
            {"code": 500, "message": "识别失败。"},
            "识别失败。",
        )
        logs = []
        out = identify_image(b"png", api_key="key", log=logs.append)
        self.assertFalse(out.ok)
        self.assertEqual(out.http_status, 500)
        self.assertEqual(out.error, "HTTP 500: 识别失败。")
        self.assertTrue(any("API FAIL http=500" in line for line in logs))

    @patch("app.core.captcha_client._http_json")
    def test_identify_http_error_without_code_keeps_status(self, http) -> None:
        http.return_value = (500, {"message": "模型暂不可用"}, "模型暂不可用")
        out = identify_image(b"png", api_key="key")
        self.assertFalse(out.ok)
        self.assertEqual(out.http_status, 500)
        self.assertEqual(out.error, "HTTP 500: 模型暂不可用")

    def test_captcha_timeout_message_is_human_readable(self) -> None:
        self.assertEqual(
            describe_captcha_error("timeout", YaoluConfig(captcha_api_timeout_s=12)),
            "识别服务请求超时（等待 12 秒未返回）",
        )

    def test_feedback_requires_id(self) -> None:
        self.assertEqual(send_feedback("", True).error, "missing identify_id")

    @patch("app.core.captcha_client._http_json")
    def test_feedback_success(self, http) -> None:
        http.return_value = (200, {"code": 0, "data": {"correct": True}}, None)
        out = send_feedback("abc", True, api_key="key")
        self.assertTrue(out.ok)
        self.assertEqual(out.identify_id, "abc")
        headers = http.call_args.kwargs["headers"]
        self.assertEqual(headers["X-API-Key"], "key")
        self.assertNotIn("Authorization", headers)
        self.assertTrue(headers["X-Signature"])

    def test_dialog_geometry_points(self) -> None:
        box = DialogBox(100, 50, 801, 480, score=2.0, method="test")
        self.assertEqual((box.width, box.height), (701, 430))
        self.assertEqual(dialog_confirm_point(box), (450, 447))
        points = [dialog_cell_point(box, i) for i in range(1, 9)]
        self.assertEqual(len(set(points)), 8)
        self.assertIsNone(dialog_cell_point(box, 9))

    def test_random_confirm_points_stay_inside_safe_button_rect(self) -> None:
        box = DialogBox(383, 170, 1043, 630, score=9.0, method="live")
        left, top, right, bottom = dialog_confirm_rect(box)
        for _ in range(1000):
            cx, cy = dialog_confirm_point(box)
            cx, cy = jitter_around(cx, cy, radius_x=20, radius_y=20)
            cx, cy = clamp_dialog_confirm_point(box, cx, cy)
            self.assertGreaterEqual(cx, left + 3)
            self.assertLess(cx, right - 3)
            self.assertGreaterEqual(cy, top + 3)
            self.assertLess(cy, bottom - 3)

    def test_aui_cell_rect_and_data_properties(self) -> None:
        img = AuiCtrlRect(True, "grid", 1, 20, 40, 400, 200)
        first = captcha_cell_rect_from_img(img, 1)
        last = captcha_cell_rect_from_img(img, 8)
        self.assertTrue(first.ok and last.ok)
        self.assertLess(first.x, last.x)
        self.assertLess(first.y, last.y)
        self.assertEqual(first.to_dict()["center"], first.center)
        self.assertFalse(captcha_cell_rect_from_img(img, 0).ok)
        dlg = DlgRect(True, x=10, y=20, w=30, h=40)
        self.assertEqual((dlg.right, dlg.bottom), (40, 60))

    def test_foreground_confirm_uses_shifted_hit_rect(self) -> None:
        rect = AuiCtrlRect(True, "Btn_Ok", 1, 100, 200, 80, 28)
        shifted = captcha_btn_ok_hit_rect(rect, real_mouse_y_offset_px=16)
        self.assertEqual((rect.y, shifted.y, shifted.h), (200, 216, 28))

        with patch("app.core.aui_click.get_captcha_btn_ok_rect", return_value=rect), patch(
            "app.core.aui_click.human_point_from_rect", return_value=None
        ), patch("app.core.aui_click.click_client_bg", return_value=True) as click:
            self.assertTrue(
                click_captcha_btn_ok(
                    MagicMock(),
                    1,
                    humanize=False,
                    prefer_real_mouse=True,
                    real_mouse_y_offset_px=16,
                )
            )
        self.assertEqual(click.call_args.args[3], 230)

    def test_yaolu_foreground_confirm_has_no_default_vertical_offset(self) -> None:
        self.assertEqual(YaoluConfig().captcha_real_mouse_confirm_y_offset_px, 0)

    @patch("app.core.yaolu_auto.snapshot_feedback_heap_baseline")
    @patch("app.core.yaolu_auto._sleep_interruptible", return_value=True)
    @patch("app.core.yaolu_auto.is_captcha_dialog_open")
    @patch("app.core.yaolu_auto.click_captcha_btn_ok", return_value=True)
    @patch("app.core.yaolu_auto.click_client_bg", return_value=True)
    @patch("app.core.yaolu_auto.get_captcha_cell_center")
    @patch("app.core.yaolu_auto.identify_image")
    def test_confirm_click_requires_dialog_to_close(
        self,
        identify,
        cell_center,
        _click_bg,
        _click_ok,
        is_open,
        _sleep,
        _baseline,
    ) -> None:
        identify.return_value = CaptchaIdentifyResult(
            ok=True,
            positions=[1, 2],
            animal="cat",
            confidence=0.25,
            identify_id="id-1",
        )
        cell_center.side_effect = [(200, 200), (300, 200)]
        is_open.return_value = MagicMock(shown=False, name="", dlg_ptr=0)
        cfg = YaoluConfig(
            captcha_humanize_clicks=False,
            captcha_fake_noise_enabled=False,
            debug_save_crops=False,
        )
        result = solve_captcha_dialog(
            1,
            cfg,
            DialogCapture(
                ok=True,
                box=DialogBox(383, 170, 1043, 630, score=9.0, method="test"),
                png=b"png",
            ),
            session=MagicMock(),
        )
        self.assertTrue(result.ok)
        is_open.assert_called_once()

    @patch("app.core.yaolu_auto.submit_activity_question", return_value=True)
    @patch("app.core.yaolu_auto.snapshot_feedback_heap_baseline")
    @patch("app.core.yaolu_auto._sleep_interruptible", return_value=True)
    @patch("app.core.yaolu_auto.is_captcha_dialog_open")
    @patch("app.core.yaolu_auto.click_captcha_btn_ok", return_value=True)
    @patch("app.core.yaolu_auto.click_client_bg", return_value=True)
    @patch("app.core.yaolu_auto.get_captcha_cell_center")
    @patch("app.core.yaolu_auto.identify_image")
    def test_confirm_still_open_retries_then_reports_not_submitted(
        self,
        identify,
        cell_center,
        click_bg,
        _click_ok,
        is_open,
        _sleep,
        _baseline,
        aq_submit,
    ) -> None:
        identify.return_value = CaptchaIdentifyResult(
            ok=True,
            positions=[1, 2],
            animal="cat",
            confidence=0.9,
            identify_id="id-2",
        )
        cell_center.side_effect = [(200, 200), (300, 200)]
        is_open.return_value = MagicMock(
            shown=True,
            name="Win_Question3D",
            dlg_ptr=0x1234,
        )
        cfg = YaoluConfig(
            captcha_humanize_clicks=False,
            captcha_fake_noise_enabled=False,
            debug_save_crops=False,
        )
        result = solve_captcha_dialog(
            1,
            cfg,
            DialogCapture(
                ok=True,
                box=DialogBox(383, 170, 1043, 630, score=9.0, method="test"),
                png=b"png",
            ),
            session=MagicMock(),
        )
        self.assertFalse(result.ok)
        self.assertIn("确认按钮未生效", result.error or "")
        self.assertGreaterEqual(click_bg.call_count, 3)
        aq_submit.assert_called_once()

    def test_crop_dialog_outputs_png_at_training_size(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        img = Image.new("RGB", (900, 600), "navy")
        raw = crop_dialog_png(img, DialogBox(50, 40, 750, 470))
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(raw)) as cropped:
            self.assertEqual(cropped.size, (701, 430))

    def test_memory_dialog_crop_accepts_verified_live_geometry(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        full = Image.new("RGB", (1916, 1006), "white")
        rect = DlgRect(
            True,
            name="Win_Question3D",
            x=628,
            y=273,
            w=660,
            h=460,
            shown=True,
        )
        crop, used, method = _crop_activity_dialog_from_full(
            full,
            mem_rect=rect,
            mem_ref_w=1916,
            mem_ref_h=1006,
        )
        self.assertIsNotNone(crop)
        self.assertEqual(crop.size, (701, 430))
        self.assertEqual((used.x, used.y, used.w, used.h), (628, 273, 660, 460))
        self.assertTrue(method.startswith("mem_rect"))

    def test_memory_dialog_crop_scales_from_fixed_client_size(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        full = Image.new("RGB", (1920, 1080), "white")
        rect = DlgRect(True, x=300, y=180, w=600, h=400, shown=True)
        crop, used, method = _crop_activity_dialog_from_full(
            full,
            mem_rect=rect,
            mem_ref_w=1427,
            mem_ref_h=801,
        )
        self.assertIsNotNone(crop)
        self.assertEqual(crop.size, (701, 430))
        self.assertEqual((used.x, used.y, used.w, used.h), (404, 243, 807, 539))
        self.assertEqual(method, "mem_rect_scaled_live_1427x801")

    def test_failed_export_saves_full_and_raw_memory_crop(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        full = Image.new("RGB", (800, 600), "white")
        rect = DlgRect(True, x=100, y=80, w=400, h=260, shown=True)
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            with patch("common.paths.captcha_debug_dir", return_value=folder):
                saved = _save_export_failure_debug(full, rect)
            self.assertEqual(len(saved), 2)
            self.assertTrue(all(p.is_file() for p in saved))
            self.assertTrue(any(p.name.endswith("_mem_raw.png") for p in saved))

    @patch("app.core.game_sys_msg._dlg_utf16_has_keys", return_value=None)
    @patch("app.core.game_sys_msg._scan_text_hits", return_value=[1, 2])
    def test_entry_block_detects_new_system_text(self, _scan, _dlg) -> None:
        session = MagicMock()
        session.pm = object()
        hit = probe_entry_open_block_text(
            session,
            baseline={ENTRY_OPEN_BLOCK_TEXT: 1},
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.kind, "block")
        self.assertEqual(hit.text, ENTRY_OPEN_BLOCK_TEXT)

    @patch("app.core.game_sys_msg._dlg_utf16_has_keys", return_value=None)
    @patch("app.core.game_sys_msg._scan_text_hits")
    def test_entry_block_heavy_false_skips_process_scan(self, scan, _dlg) -> None:
        """Tight polls must not call pattern_scan_all via entry-block probe."""
        scan.side_effect = AssertionError("process scan must not run")
        session = MagicMock()
        session.pm = object()
        session._entry_block_dialog_names = ()
        hit = probe_entry_open_block_text(
            session,
            baseline={ENTRY_OPEN_BLOCK_TEXT: 0},
            heavy=False,
        )
        self.assertIsNone(hit)
        scan.assert_not_called()

    @patch("app.core.plg_ui.find_shown_dialog", return_value=None)
    def test_dialog_wait_aborts_on_entry_server_block(self, _find) -> None:
        out = wait_dlg_show(
            object(),
            ("Win_Question3D",),
            timeout_s=1.0,
            abort_probe=lambda: ENTRY_OPEN_BLOCK_TEXT,
        )
        self.assertFalse(out.ok)
        self.assertEqual(out.method, "entry_block_probe")
        self.assertTrue(out.error.startswith("entry_block:"))

    def test_default_enter_timeout_is_18_seconds(self) -> None:
        self.assertEqual(YaoluConfig().enter_timeout_s, 18.0)

    @patch("app.core.game_sys_msg.is_captcha_dialog_open")
    @patch("app.core.game_sys_msg._scan_text_hits")
    @patch("app.core.game_sys_msg.probe_captcha_answer_text")
    def test_wait_feedback_is_ui_only_and_counts_down(
        self, probe, scan, is_open
    ) -> None:
        """Timed feedback must stay UI-only and not invent extra windows."""
        statuses: list[str] = []
        clock = {"t": 100.0}
        countdown_ticks = {"n": 0}

        def _mono() -> float:
            return clock["t"]

        def _sleep(sec: float) -> None:
            clock["t"] += float(sec)

        def _probe(*_a, **kwargs):
            self.assertFalse(kwargs.get("heavy", True))
            return None

        probe.side_effect = _probe
        is_open.return_value = MagicMock(shown=False)
        scan.side_effect = AssertionError("process scan must not run")

        def _countdown() -> None:
            countdown_ticks["n"] += 1
            statuses.append(f"剩余 {max(0, int(18 - (clock['t'] - 100.0)))}s")

        with (
            patch("app.core.game_sys_msg.time.monotonic", side_effect=_mono),
            patch("app.core.game_sys_msg.time.sleep", side_effect=_sleep),
        ):
            out = wait_captcha_answer_feedback(
                MagicMock(pm=object()),
                timeout_s=18.0,
                poll_s=0.5,
                countdown=_countdown,
                deadline=118.0,
            )
        self.assertEqual(out.kind, "timeout")
        self.assertGreaterEqual(countdown_ticks["n"], 2)
        self.assertTrue(any("剩余" in s for s in statuses))
        # Never fell into process-wide text scans.
        scan.assert_not_called()

    @patch("app.core.game_sys_msg.is_captcha_dialog_open")
    @patch("app.core.game_sys_msg.probe_captcha_answer_text")
    def test_wait_feedback_stage2_hard_stop_on_delayed_shenfa(
        self, probe, is_open
    ) -> None:
        is_open.return_value = MagicMock(shown=False)
        calls = {"n": 0}

        def _probe(*_a, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return CaptchaAnswerFeedback(
                    kind="ok",
                    text=CAPTCHA_OK_TEXT,
                    msg_id=CAPTCHA_OK_MSG_ID,
                    method="chat:Win_ChatInfo",
                )
            # stage2 with trust_chat_hard
            self.assertTrue(kwargs.get("trust_chat_hard"))
            self.assertFalse(kwargs.get("heavy", True))
            return CaptchaAnswerFeedback(
                kind="block",
                text=SHENFA_BLOCK_TEXT,
                method="chat:Win_ChatInfo+trust_stage1",
                error=CAPTCHA_OK_TEXT,
            )

        probe.side_effect = _probe
        out = wait_captcha_answer_feedback(
            MagicMock(pm=object()),
            timeout_s=18.0,
            poll_s=0.1,
            stage2_hold_s=4.0,
        )
        self.assertEqual(out.kind, "block")
        self.assertIn("神罚", out.text or "")
        self.assertEqual(out.error, CAPTCHA_OK_TEXT)



    def test_hist_delta_conflict_prefers_newer_fail(self) -> None:
        """When ok+fail both grow, prefer the newer instance address."""
        from app.core.game_sys_msg import _probe_feedback_hist_delta, CAPTCHA_FAIL_TEXT

        session = MagicMock()
        before = {
            "counts": {
                "答案正确，请尽快进入活动": 0,
                "答案错误，请大侠重新来过": 0,
            },
            "instances": [],
            "sig_hash": "aaa",
        }
        after = {
            "counts": {
                "答案正确，请尽快进入活动": 1,
                "答案错误，请大侠重新来过": 1,
            },
            "instances": [
                ("答案正确，请尽快进入活动", 0x1000),
                ("答案错误，请大侠重新来过", 0x2000),
            ],
            "sig_hash": "bbb",
        }
        session._feedback_hist_baseline = before

        with patch(
            "app.core.chat_history.snapshot_feedback_fingerprint",
            return_value=after,
        ), patch(
            "app.core.chat_history.diff_feedback_count_new",
            return_value={
                "答案正确，请尽快进入活动": 1,
                "答案错误，请大侠重新来过": 1,
            },
        ):
            hit = _probe_feedback_hist_delta(session, log=lambda _m: None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.kind, "fail")
        self.assertIn("错误", hit.text or "")



if __name__ == "__main__":
    unittest.main()
