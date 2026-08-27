# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.skill_recovery_lab import (
    skill_sequence_summary,
    snapshot_skill_sequence,
    is_valid_cast_skill_id,
    is_armed_cast,

    METHOD_MEM_BUSY,
    METHOD_ONSKILL,
    METHOD_ONSKILL_NOCONT,
    METHOD_ONSKILL_NOCONT_OBJ,
    METHOD_MEM_PERFORM,
    ONSKILL_NATIVE_MODE,
    classify_local,
    classify_series,
    is_recovery_window,
    parse_onskill_native_note,
    run_recovery_trial,
    watch_summary,
    write_recovery_trial_report,
    ALL_METHODS,
    METHOD_LABELS,
    METHOD_PERFORM_STOP65,
    METHOD_ONSKILL_PERFORM_STOP65,
    RECOVERY_CANDIDATES,
    cast_watch_dirty,
    is_cast_active,
    is_armed_cast,
    is_recovery_window,
    perform_is_skillish,
    METHOD_STOP65_ONSKILL_HOLD,
    METHOD_HOSTSTOP_LOCAL,
    METHOD_HOSTSTOP_BIT1,
    METHOD_SOFT_NEXT,
    METHOD_KEY_INTERRUPT,
    METHOD_HAND_PROBE,
    METHOD_CAST_INTERRUPT,
    lab_stop65_onskill_hold,
)


class SkillRecoveryLabTests(unittest.TestCase):
    def test_is_recovery_window_requires_id_and_busy_bit(self) -> None:
        self.assertFalse(is_recovery_window({"+10": 0x9563, "+4A0": 0}))
        self.assertFalse(is_recovery_window({"+10": 0, "+4A0": 1}))
        self.assertTrue(is_recovery_window({"+10": 0x9563, "+4A0": 1}))

    def test_classify_local_full_and_partial(self) -> None:
        full = classify_local(
            {"+10": 0x9563, "+4A0": 1, "+7C": 0x10001},
            {"+10": 0, "+4A0": 0, "+7C": 0},
        )
        self.assertEqual(full["verdict"], "local_full_clear")
        self.assertTrue(full["id_cleared"])
        self.assertTrue(full["busy_cleared"])

        busy_only = classify_local(
            {"+10": 0x9563, "+4A0": 1},
            {"+10": 0x9563, "+4A0": 0},
        )
        self.assertEqual(busy_only["verdict"], "local_busy_only")

    def test_classify_series_detects_id_reverted_and_refill_time(self) -> None:
        before = {"+10": 0x9563, "+4A0": 1, "+7C": 0, "+18": 0x34D}
        series = [
            {"t_s": 0.0, "watch": {"+10": 0, "+4A0": 0, "+7C": 0, "+18": 0}},
            {"t_s": 0.05, "watch": {"+10": 0, "+4A0": 0, "+7C": 0, "+18": 0}},
            {
                "t_s": 0.12,
                "watch": {"+10": 0x9563, "+4A0": 0, "+7C": 0x10001, "+18": 0x34D},
            },
            {
                "t_s": 0.30,
                "watch": {"+10": 0x9563, "+4A0": 0, "+7C": 0x10001, "+18": 0x34D},
            },
        ]
        sc = classify_series(
            before,
            series,
            native={"native_cleared": True},
        )
        self.assertEqual(sc["verdict"], "local_clear_then_id_refilled")
        self.assertTrue(sc["id_cleared_immediate"])
        self.assertFalse(sc["id_cleared"])
        self.assertTrue(sc["id_reverted"])
        self.assertEqual(sc["id_refill_t_s"], 0.12)
        self.assertIn("+10", sc["refill_fields"])
        self.assertEqual(sc["native_vs_python"], "agree_immediate_clear")

    def test_parse_onskill_native_note(self) -> None:
        n = parse_onskill_native_note(
            "ONSKILL_STOPPED ok id=0x9563,0x0,0x34D cleared=1 (identity-match)"
        )
        self.assertTrue(n["native_cleared"])
        self.assertEqual(n["native_id0"], 0x9563)
        self.assertEqual(n["native_id2"], 0x34D)

    def test_trial_report_written(self) -> None:
        trial = {
            "ok": True,
            "method": METHOD_ONSKILL,
            "label": "桥接:OnSkillStopped",
            "before": {"+10": 0x9563, "+14": 0, "+18": 0x34D, "+4A0": 1, "+7C": 1},
            "after": {"+10": 0x9563, "+14": 0, "+18": 0x34D, "+4A0": 0, "+7C": 0x10001},
            "after_immediate": {"+10": 0, "+14": 0, "+18": 0, "+4A0": 0, "+7C": 0},
            "after_series": [
                {"t_s": 0.0, "watch": {"+10": 0, "+4A0": 0}},
                {"t_s": 0.12, "watch": {"+10": 0x9563, "+4A0": 0}},
            ],
            "score": {
                "verdict": "local_clear_then_id_refilled",
                "id_cleared": False,
                "id_cleared_immediate": True,
                "id_reverted": True,
                "id_refill_t_s": 0.12,
                "refill_fields": {"+10": {"before": 0, "after": 0x9563}},
                "busy_cleared": True,
                "busy_cleared_immediate": True,
                "flags_cleared": False,
                "still_active": False,
                "stable_full_clear": False,
                "native_vs_python": "agree_immediate_clear",
            },
            "fire": {
                "ok": True,
                "channel": "bridge",
                "note": "ONSKILL_STOPPED ok id=0x9563,0x0,0x34D cleared=1",
                "error": "",
                "native": {
                    "native_cleared": True,
                    "native_id0": 0x9563,
                    "native_id1": 0,
                    "native_id2": 0x34D,
                },
            },
            "cast_this": 0x1000,
            "host_ptr": 0x2000,
            "wait_rounds": 3,
            "error": "",
            "fire_dt_s": 0.01,
        }
        with tempfile.TemporaryDirectory() as td, patch(
            "common.paths.app_root", return_value=Path(td)
        ):
            paths = write_recovery_trial_report(trial)
            text = Path(paths["md"]).read_text(encoding="utf-8")
            self.assertTrue(Path(paths["json"]).exists())
        self.assertIn("local_clear_then_id_refilled", text)
        self.assertIn("id_refill_t_s=0.12", text)
        self.assertIn("native_cleared=True", text)

    @patch("app.core.skill_recovery_lab._fire_method")
    @patch("app.core.skill_recovery_lab.snapshot_cast")
    @patch("app.core.skill_recovery_lab.wait_recovery_window")
    def test_run_trial_scores_refilled_from_series(self, wait, snap, fire) -> None:
        before = {
            "+10": 0x9563,
            "+14": 0,
            "+18": 0x34D,
            "+4A0": 1,
            "+7C": 0,
            "+20": 1,
        }
        cleared = {"+10": 0, "+14": 0, "+18": 0, "+4A0": 0, "+7C": 0, "+20": 0}
        refilled = {
            "+10": 0x9563,
            "+14": 0,
            "+18": 0x34D,
            "+4A0": 0,
            "+7C": 0x10001,
            "+20": 0x100,
        }
        wait.return_value = {
            "ok": True,
            "wait_rounds": 2,
            "watch": before,
            "cast_this": 0x1000,
            "host_ptr": 0x2000,
        }
        # pre-fire + many post samples (default delays count)
        snaps = [(before, 0x1000, 0x2000), (cleared, 0x1000, 0x2000)]
        snaps += [(refilled, 0x1000, 0x2000)] * 12
        snap.side_effect = snaps
        fire.return_value = {
            "method": METHOD_ONSKILL,
            "ok": True,
            "note": "ONSKILL_STOPPED ok id=0x9563,0x0,0x34D cleared=1",
            "error": "",
            "channel": "bridge",
            "native": {
                "native_cleared": True,
                "native_id0": 0x9563,
                "native_id1": 0,
                "native_id2": 0x34D,
            },
        }
        sess = type("S", (), {"pid": 1, "hwnd": 0})()
        with tempfile.TemporaryDirectory() as td, patch(
            "common.paths.app_root", return_value=Path(td)
        ):
            trial = run_recovery_trial(
                sess,
                METHOD_ONSKILL,
                log=lambda _m: None,
                post_delays_s=(0.0, 0.05, 0.12),
            )
            self.assertTrue(trial["ok"])
            self.assertEqual(trial["score"]["verdict"], "local_clear_then_id_refilled")
            self.assertTrue(trial["score"]["id_cleared_immediate"])
            self.assertTrue(trial["score"]["id_reverted"])
            self.assertEqual(trial["score"]["native_vs_python"], "agree_immediate_clear")
            self.assertTrue(Path(trial["report_path"]).exists())
            self.assertTrue(Path(trial["report_json"]).exists())

    def test_watch_summary_format(self) -> None:
        s = watch_summary({"+10": 0x9563, "+14": 0, "+18": 0x34D, "+4A0": 1, "+7C": 2})
        self.assertIn("id=0x9563", s)
        self.assertIn("+18=0x34D", s)


    def test_onskill_native_modes_map(self) -> None:
        self.assertEqual(ONSKILL_NATIVE_MODE[METHOD_ONSKILL], 0)
        self.assertEqual(ONSKILL_NATIVE_MODE[METHOD_ONSKILL_NOCONT], 1)
        self.assertEqual(ONSKILL_NATIVE_MODE[METHOD_ONSKILL_NOCONT_OBJ], 2)

    def test_full_clear_requires_plus18(self) -> None:
        before = {"+10": 0x1, "+18": 0x34D, "+4A0": 1, "+7C": 1}
        after_tail = {"+10": 0, "+18": 0x34D, "+4A0": 0, "+7C": 0}
        after_full = {"+10": 0, "+18": 0, "+4A0": 0, "+7C": 0}
        self.assertEqual(
            classify_local(before, after_tail)["verdict"],
            "local_id_busy_clear_tail_left",
        )
        self.assertEqual(
            classify_local(before, after_full)["verdict"],
            "local_full_clear",
        )

    def test_parse_onskill_note_cont_mode(self) -> None:
        n = parse_onskill_native_note(
            "ONSKILL_STOPPED ok id=0x1,0x0,0x2 cleared=1 cont=0 clear_obj=1/7c=0x0 mode=2"
        )
        self.assertEqual(n["native_cont"], 0)
        self.assertEqual(n["native_mode"], 2)
        self.assertEqual(n["native_clear_obj"], 1)


    def test_recovery_candidates_include_perform_stop(self) -> None:
        self.assertEqual(
            RECOVERY_CANDIDATES,
            (
                METHOD_CAST_INTERRUPT,
                METHOD_HAND_PROBE,
                METHOD_SOFT_NEXT,
                METHOD_HOSTSTOP_BIT1,
            ),
        )
        self.assertEqual(
            METHOD_LABELS[METHOD_HAND_PROBE],
            "手按X/空格采样(不注入)",
        )
        self.assertIn("0xE07", METHOD_LABELS[METHOD_CAST_INTERRUPT])
        self.assertIn("v2", METHOD_LABELS[METHOD_CAST_INTERRUPT])
        self.assertIn("非通用", METHOD_LABELS[METHOD_KEY_INTERRUPT])
        self.assertEqual(
            METHOD_LABELS[METHOD_SOFT_NEXT],
            "软压:stop65+Seq+清ID(禁unit)",
        )
        self.assertEqual(
            METHOD_LABELS[METHOD_STOP65_ONSKILL_HOLD],
            "组合:stop65+OnSkill+压回填",
        )
        self.assertIn(METHOD_PERFORM_STOP65, ALL_METHODS)
        self.assertIn(METHOD_ONSKILL_PERFORM_STOP65, ALL_METHODS)
        self.assertEqual(
            METHOD_LABELS[METHOD_PERFORM_STOP65],
            "perform:vt+0x10(0x65)",
        )
        self.assertEqual(
            METHOD_LABELS[METHOD_ONSKILL_PERFORM_STOP65],
            "桥接:OnSkill+stop65",
        )

    def test_lab_stop_skill_perform_source_no_cancel21(self) -> None:
        """Contract: skill-era perform stop must not use 0x21 cancel."""
        from pathlib import Path as _P
        import re as _re

        src = _P("app/core/skill_recovery_lab.py").read_text(encoding="utf-8")
        start = src.find("def lab_stop_skill_perform")
        end = src.find("\ndef _fire_method", start)
        self.assertGreater(start, 0)
        self.assertGreater(end, start)
        body = src[start:end]
        # Drop docstring so negative-control wording does not fail the contract.
        code = _re.sub(r'"""[\s\S]*?"""', "", body, count=1)
        self.assertIn("PERFORM_STOP_VT_OFF", code)
        self.assertIn("PERFORM_STOP_ARG", code)
        self.assertIn("ensure_base_locomotion_perform", code)
        self.assertIn("remote_call_thiscall_x86", code)
        self.assertNotIn("cancel_session(", code)
        self.assertNotIn("CMD_CANCEL_SESSION", code)
        self.assertNotIn(".cancel_session", code)


    def test_hold_source_no_cancel21(self) -> None:
        from pathlib import Path as _P
        import re as _re
        src = _P("app/core/skill_recovery_lab.py").read_text(encoding="utf-8")
        start = src.find("def lab_stop65_onskill_hold")
        end = src.find("\ndef _fire_method", start)
        self.assertGreater(start, 0)
        body = src[start:end]
        code = _re.sub(r'"""[\s\S]*?"""', "", body, count=1)
        self.assertIn("lab_stop_skill_perform", code)
        self.assertIn("on_skill_stopped", code)
        self.assertIn("CAST_CANCEL_PERFORM_STRIP", code)
        self.assertNotIn("cancel_session(", code)
        self.assertNotIn(".cancel_session", code)


    def test_cast_dirty_and_skillish_helpers(self) -> None:
        self.assertTrue(cast_watch_dirty({"+10": 1, "+18": 0, "+4A0": 0}))
        self.assertTrue(cast_watch_dirty({"+10": 0, "+18": 0x34D, "+4A0": 0}))
        self.assertTrue(cast_watch_dirty({"+10": 0, "+18": 0, "+4A0": 1}))
        self.assertFalse(cast_watch_dirty({"+10": 0, "+18": 0, "+4A0": 0}))
        self.assertTrue(
            perform_is_skillish(
                {"active": True, "is_base": False, "is_matter": False, "perform_type": 4}
            )
        )
        self.assertFalse(
            perform_is_skillish(
                {"active": True, "is_base": True, "is_matter": False, "perform_type": 2}
            )
        )


    def test_hoststop_local_source_no_cancel21(self) -> None:
        from pathlib import Path as _P
        import re as _re
        src = _P("app/core/skill_recovery_lab.py").read_text(encoding="utf-8")
        start = src.find("def lab_hoststop_local_teardown")
        end = src.find("\ndef lab_stop65_onskill_hold", start)
        if end < 0:
            end = src.find("\ndef _fire_method", start)
        self.assertGreater(start, 0)
        body = src[start:end]
        code = _re.sub(r'"""[\s\S]*?"""', "", body, count=1)
        self.assertIn("VA_CAST_SUB_CLEAR", code)
        self.assertIn("VA_CLEAR_SKILL_OBJ", code)
        self.assertIn("lab_stop_skill_perform", code)
        self.assertIn("lab_clear_host_4a4_flags", code)
        self.assertIn("lab_force_can_cast_next_unit", code)
        self.assertIn("snapshot_cast", code)
        self.assertIn("on_skill_stopped", code)
        self.assertNotIn("cancel_session(", code)
        self.assertNotIn(".cancel_session", code)


    def test_is_cast_active_early(self) -> None:
        # bare id is "active residue" but NOT armed
        self.assertTrue(is_cast_active({"+10": 0x9563, "+4A0": 0, "+18": 0}))
        self.assertTrue(is_cast_active({"+10": 0, "+4A0": 1}))
        self.assertFalse(is_cast_active({"+10": 0, "+4A0": 0, "+18": 0}))
        self.assertTrue(is_recovery_window({"+10": 1, "+4A0": 1}))
        self.assertFalse(is_recovery_window({"+10": 1, "+4A0": 0}))


    def test_is_armed_cast_not_bare_id(self) -> None:
        bare = {"+10": 0x9563, "+4A0": 0, "+20": 0}
        base_perf = {"active": True, "is_base": True, "is_matter": False, "perform_type": 2}
        self.assertTrue(is_cast_active(bare))
        self.assertFalse(is_armed_cast(bare, base_perf))
        self.assertTrue(is_armed_cast({**bare, "+4A0": 1}, base_perf))
        self.assertTrue(
            is_armed_cast(
                {"+10": 0, "+4A0": 0, "+20": 0},
                {"active": True, "is_base": False, "is_matter": False, "perform_type": 4},
            )
        )



    def test_busy_ok_when_before_busy_already_zero(self) -> None:
        # Live 095755 pattern: fire with busy already 0 should not force local_id_only
        sc = classify_local(
            {"+10": 0x9563, "+4A0": 0, "+18": 0x34D, "+7C": 0},
            {"+10": 0, "+4A0": 0, "+18": 0, "+7C": 0},
        )
        self.assertEqual(sc["verdict"], "local_full_clear")
        self.assertTrue(sc.get("busy_ok"))

    def test_hit_meta_and_edge_idle(self) -> None:
        from app.core.skill_recovery_lab import hit_meta, is_edge_idle
        self.assertTrue(
            is_edge_idle(
                {"+10": 0, "+4A0": 0},
                {"active": True, "is_base": True, "perform_type": 2},
            )
        )
        self.assertFalse(
            is_edge_idle(
                {"+10": 1, "+4A0": 0},
                {"active": True, "is_base": True, "perform_type": 2},
            )
        )
        m = hit_meta(
            {"+10": 0x9563, "+1C": 1900, "+20": 100, "+4A0": 1},
            {"perform_type": 4, "is_base": False, "is_matter": False, "active": True},
        )
        self.assertEqual(m["busy"], 1)
        self.assertEqual(m["remain_ms"], 1800)
        self.assertTrue(m["armed"])



    def test_skill_sequence_summary_idle_and_blocked(self) -> None:
        idle = {"ok": True, "seq": 0, "can_cast_next_hint": True}
        self.assertIn("seq=null", skill_sequence_summary(idle))
        blocked = {
            "ok": True,
            "seq": 0x12340000,
            "gate_488": 0,  # >=0 blocks
            "active_490": 2,
            "unit_n": 2,
            "unit0": 1,
            "unit0_code": 0x69,
            "can_cast_next_hint": False,
        }
        s = skill_sequence_summary(blocked)
        self.assertIn("seq=0x12340000", s)
        self.assertIn("hint=False", s)

    def test_hoststop_bit1_label_mentions_seq(self) -> None:
        self.assertIn("Seq", METHOD_LABELS[METHOD_HOSTSTOP_BIT1])



    def test_reject_ghost_ffffffff_skill_id(self) -> None:
        self.assertFalse(is_valid_cast_skill_id(0))
        self.assertFalse(is_valid_cast_skill_id(0xFFFFFFFF))
        self.assertFalse(is_valid_cast_skill_id(-1))
        self.assertTrue(is_valid_cast_skill_id(0x9563))
        self.assertTrue(is_valid_cast_skill_id(0x12609))
        # bare ghost must not arm suppress
        self.assertFalse(
            is_armed_cast(
                {"+10": 0xFFFFFFFF, "+4A0": 0, "+20": 0x100},
                {"perform_type": 2, "is_base": True, "active": True},
            )
        )
        self.assertTrue(
            is_armed_cast(
                {"+10": 0x9563, "+4A0": 1, "+20": 0},
                {"perform_type": 4, "is_base": False, "active": True},
            )
        )


    def test_soft_next_source_no_onskill_no_cancel21(self) -> None:
        """Contract: soft path must not call OnSkill / 0x21 (754BA0 strip ok)."""
        from pathlib import Path as _P
        import re as _re

        src = _P("app/core/skill_recovery_lab.py").read_text(encoding="utf-8")
        start = src.find("def lab_soft_next_unlock")
        end = src.find("\ndef wait_recovery_window", start)
        self.assertGreater(start, 0)
        self.assertGreater(end, start)
        body = src[start:end]
        code = _re.sub(r'"""[\s\S]*?"""', "", body, count=1)
        self.assertIn("lab_stop_skill_perform", code)
        self.assertIn("lab_clear_host_4a4_flags", code)
        self.assertIn("lab_force_can_cast_next_unit", code)
        self.assertIn("snapshot_cast", code)
        self.assertIn("lab_force_can_cast_next_unit", code)
        self.assertIn('mode="safe"', code)
        self.assertIn("CAST_CANCEL_PERFORM_STRIP", code)
        self.assertIn("hard-ban", code)
        self.assertNotIn("remote_write_bytes(pid, unit_ptr", code)
        self.assertNotIn("on_skill_stopped", code)
        self.assertNotIn("cancel_session(", code)
        self.assertNotIn(".cancel_session", code)

    def test_format_post_fire_diag_line_fields(self) -> None:
        from app.core.skill_recovery_lab import format_post_fire_diag

        class _Sess:
            pid = 0

        # offline: no process; still returns structure
        d = format_post_fire_diag(_Sess(), cast_this=0, fire={"ok": True, "channel": "soft_next"})
        self.assertIn("line", d)
        self.assertIn("POST_DIAG", d["line"])
        self.assertIn("live_cc", d)

    def test_suppress_loop_source_default_key_or_soft(self) -> None:
        from pathlib import Path as _P

        src = _P("app/core/skill_recovery_lab.py").read_text(encoding="utf-8")
        start = src.find("def run_recovery_suppress_loop")
        end = src.find("\ndef write_recovery_trial_report", start)
        body = src[start:end]
        self.assertIn('mode: str = "soft"', body)
        self.assertIn("lab_soft_next_unlock", body)
        self.assertIn("format_post_fire_diag", body)
        self.assertIn("v4.7", body)
        self.assertIn("KEY_FIRE", body)
        self.assertIn("key_armed", body)


    def test_key_interrupt_source_no_cancel21_no_unit_write(self) -> None:
        from pathlib import Path as _P
        import re as _re
        src = _P("app/core/skill_recovery_lab.py").read_text(encoding="utf-8")
        start = src.find("def lab_key_interrupt")
        end = src.find("\ndef wait_recovery_window", start)
        self.assertGreater(start, 0)
        body = src[start:end]
        code = _re.sub(r'"""[\s\S]*?"""', "", body, count=1)
        self.assertIn("ui_key", code)
        self.assertIn("key_hold", code)
        self.assertIn("allow_softsend", code)
        self.assertIn("KEY_INTERRUPT_VK_SEQ_DEFAULT", src)
        self.assertIn("0x58", src)
        self.assertIn("0x20", src)
        self.assertNotIn("cancel_session(", code)
        self.assertNotIn("on_skill_stopped", code)

    def test_suppress_default_mode_key(self) -> None:
        from pathlib import Path as _P
        src = _P("app/core/skill_recovery_lab.py").read_text(encoding="utf-8")
        start = src.find("def run_recovery_suppress_loop")
        end = src.find("\ndef write_recovery_trial_report", start)
        body = src[start:end]
        self.assertIn('mode: str = "soft"', body)
        self.assertIn("lab_key_interrupt", body)


    def test_cast_interrupt_source_uses_bridge_cast(self) -> None:
        from pathlib import Path as _P
        import re as _re
        src = _P("app/core/skill_recovery_lab.py").read_text(encoding="utf-8")
        start = src.find("def lab_cast_interrupt_skill")
        end = src.find("\ndef wait_cast_idle", start)
        self.assertGreater(start, 0)
        body = src[start:end]
        code = _re.sub(r'"""[\s\S]*?"""', "", body, count=1)
        self.assertIn("cast_skill", code)
        self.assertIn("INTERRUPT_SKILL_ID_DEFAULT", src)
        self.assertIn("0xE07", src)
        self.assertIn("lab_stop_skill_perform", code)
        self.assertIn("lab_clear_host_4a4_flags", code)
        self.assertIn("lab_force_can_cast_next_unit", code)
        self.assertIn("snapshot_cast", code)
        self.assertNotIn("ui_key", code)
        self.assertNotIn("key_hold", code)
        self.assertNotIn("cancel_session(", code)
        # never write unit arrays
        self.assertNotIn("unit0", code)

    def test_hand_probe_source_no_ui_key_inject(self) -> None:

        from pathlib import Path as _P
        import re as _re
        src = _P("app/core/skill_recovery_lab.py").read_text(encoding="utf-8")
        start = src.find("def run_hand_interrupt_probe")
        end = src.find("def run_recovery_suppress_loop", start)
        self.assertGreater(start, 0)
        body = src[start:end]
        code = _re.sub(r'"""[\s\S]*?"""', "", body, count=1)
        self.assertIn("snapshot_cast", code)
        self.assertIn("prompt_fn", code)
        self.assertIn("natural_end", code)
        self.assertIn("现在手按", src)
        self.assertNotIn("ui_key", code)
        self.assertNotIn("key_hold", code)
        self.assertNotIn("cancel_session(", code)

if __name__ == "__main__":
    unittest.main()
