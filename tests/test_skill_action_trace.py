from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.skill_action_trace import (
    SCENARIO_CHARGED_CHANNEL,
    SCENARIO_REAL_X,
    align_session_packets,
    packet_trace_path,
    parse_packet_trace,
    parse_native_trace,
    run_skill_action_trace,
    summarize_trace,
    write_trace_report,
)
from app.core.youfeng_chain import (
    INTERRUPT_EXACT_CANCEL,
    INTERRUPT_FRESH_GATE,
    INTERRUPT_INTERNAL67,
    INTERRUPT_NEXT_GATE,
    INTERRUPT_MASH_CAST,
    INTERRUPT_PHASE_GATE,
    INTERRUPT_QINGGONG_GATE,
    INTERRUPT_QINGGONG_PRECAST,
    INTERRUPT_QINGGONG_PULSE,
    INTERRUPT_QINGGONG_FORCE,
    INTERRUPT_QINGGONG_LONG,
    VK_ALT,
    VK_YOUFENG,
    YOUFENG_ULTIMATE_CONFIG_ID,
    YOUFENG_ULTIMATE_INTERVAL_S,
    YoufengChainRunner,
    analyze_next_gate_trace,
    run_fresh_gate_validation,
    run_exact_cancel_validation,
    run_next_gate_validation,
    run_qinggong_gate_validation,
    run_qinggong_precast_validation,
    run_qinggong_pulse_validation,
)


class SkillActionTraceTests(unittest.TestCase):
    def test_parse_packet_sidecar_and_align_session(self) -> None:
        action_rows = [
            {
                "seq": 4,
                "kind": "session_send",
                "tick": 0xFFFFFFFA,
                **{f"ss{i}": i for i in range(12)},
            }
        ]
        raw = (
            "seq\ttick\tthread\tcaller\tself\tlength\tcaptured\tpayload\n"
            "1\t0x00000003\t7\t0xDA1234\t0x1000\t4\t4\t0102a0ff\n"
            "2\t50\t7\t0xDA1234\t0x1000\t1\t1\tCC\n"
        )
        with tempfile.TemporaryDirectory() as td:
            main = Path(td) / "skill_action_trace.tsv"
            sidecar = packet_trace_path(main)
            sidecar.write_text(raw, encoding="utf-8")
            packets = parse_packet_trace(main)
        self.assertEqual(packets[0]["payload"], "0102A0FF")
        self.assertEqual(packets[0]["length"], 4)
        aligned = align_session_packets(action_rows, packets, window_ms=20)
        self.assertEqual(aligned[0]["session_args"], list(range(12)))
        self.assertEqual(len(aligned[0]["packets"]), 1)
        self.assertEqual(aligned[0]["packets"][0]["dt_ms"], 9)

    def test_human_action_window_is_not_too_short(self) -> None:
        self.assertEqual(run_skill_action_trace.__kwdefaults__["capture_s"], 8.0)
        source = inspect.getsource(run_skill_action_trace)
        self.assertIn("accept_already_armed=False", source)
        self.assertIn("_beep_signal(1)", source)
        self.assertIn("_beep_signal(2)", source)
        self.assertIn("quiet >= 0.22", source)
        self.assertIn("settle_deadline = settle_started + 1.5", source)
        self.assertIn("identity_matches", source)
        self.assertIn("bridge.on_skill_stopped", source)
        self.assertIn("identity cleanup incomplete", source)
        self.assertIn("suppress_mode=4", source)
        self.assertIn("no 2.2s sustained charged skill state", source)
        self.assertIn("charged cleanup incomplete", source)
        self.assertIn('result["unlock"]["marker_tick"]', source)
        self.assertIn("fresh_session_performed_before_natural_end", source)
        self.assertIn("invalid_no_old_tail_intervention", source)
        self.assertIn("old_suppressed_perform", source)
        self.assertIn("fresh_perform_dt_ms", source)

    def test_skill_panel_exposes_trace_not_legacy_mutators(self) -> None:
        ui = Path("app/ui/main_window.py").read_text(encoding="utf-8")
        start = ui.index('text="施法 this 探针')
        end = ui.index("# --- solution-2 KEY_HOLD", start)
        panel = ui[start:end]
        self.assertIn("动作路径对照", panel)
        self.assertIn("开始一次对照", panel)
        self.assertIn("稳定去后摇（一次）", panel)
        self.assertIn("_lab_skill_stable_recovery_once", panel)
        self.assertIn("蓄力/持续去后摇（一次）", panel)
        self.assertIn("_lab_skill_charged_recovery_once", panel)
        self.assertIn("有凤宏对照: OFF", panel)
        self.assertIn("_lab_youfeng_chain_toggle", panel)
        self.assertIn("有凤E07对照: OFF", panel)
        self.assertIn("_lab_youfeng_internal_toggle", panel)
        self.assertIn("有凤轻功抢招: OFF", panel)
        self.assertIn("_lab_youfeng_gate_toggle", panel)
        self.assertIn("真绝+普通循环: OFF", panel)
        self.assertIn("_lab_youfeng_ultimate_toggle", panel)
        self.assertIn("真绝无后摇连发: OFF", panel)
        self.assertIn("_lab_youfeng_ultimate_spam_toggle", panel)
        for stale in (
            "仅清 busy bit",
            "等 active 清 ID+busy",
            "OnSkillStopped (桥接)",
            "矩阵×4",
            "持续压制",
            "通用:桥接cast打断",
        ):
            self.assertNotIn(stale, panel)

    def test_settings_moves_youfeng_from_macro_panel_to_hang_skills(self) -> None:
        ui = Path("app/ui/pages/_impl.py").read_text(encoding="utf-8")
        panel = ui.split('box_sc = section(body, "技能后摇")', 1)[1].split(
            "self._pick_job = None", 1
        )[0]
        self.assertNotIn("有凤连续抢招", panel)
        self.assertNotIn("压制起飞", panel)
        self.assertNotIn("_on_youfeng_chain_toggle", ui)
        hang_panel = ui.split('box_hang = section(body, "挂机设置")', 1)[1].split(
            'box_sc = section(body, "技能后摇")', 1
        )[0]
        self.assertIn("华山 -> 有凤来仪", hang_panel)
        self.assertIn("开启有凤", hang_panel)
        self.assertIn("command=self._on_youfeng_hook_toggle", hang_panel)
        self.assertIn("开启释放丸子", hang_panel)
        self.assertIn("command=self._on_wanzi_packet_toggle", hang_panel)
        self.assertIn("内功丸子挂机", hang_panel)
        self.assertIn("外功丸子挂机", hang_panel)
        self.assertIn("command=self._on_hang_wanzi_neigong_toggle", hang_panel)
        self.assertIn("command=self._on_hang_wanzi_waigong_toggle", hang_panel)
        self.assertNotIn("最后一槽 · 100%", hang_panel)
        self.assertNotIn('text="招式"', hang_panel)

        toggle = ui.split("def _on_youfeng_hook_toggle", 1)[1].split(
            "def _set_wanzi_packet_button", 1
        )[0]
        self.assertIn("start_youfeng_key_hook", toggle)
        self.assertIn("stop_youfeng_key_hook", toggle)
        self.assertNotIn("start_hang(", toggle)
        self.assertNotIn("stop_hang(", toggle)

        wanzi_toggle = ui.split("def _on_wanzi_packet_toggle", 1)[1].split(
            "def _on_hang_refresh", 1
        )[0]
        self.assertIn("start_wanzi_packet_manual", wanzi_toggle)
        self.assertIn("stop_wanzi_packet_manual", wanzi_toggle)

    def test_youfeng_observer_mode_does_not_drive_skill_key(self) -> None:
        source = inspect.getsource(YoufengChainRunner._run)
        self.assertIn("if self.drive_casts:", source)
        self.assertIn("内挂最后一槽", source)
        self.assertIn("elif self.drive_casts:", source)

    def test_youfeng_product_snapshot_does_not_import_excluded_lab(self) -> None:
        source = inspect.getsource(YoufengChainRunner._snapshot)
        self.assertNotIn("skill_recovery_lab", source)
        self.assertNotIn("skill_cast_probe", source)

        runner = YoufengChainRunner(1, drive_casts=False)
        runner._session = MagicMock(module_base=0x400000)
        root = 0x10000000
        mid = 0x11000000
        host = 0x12000000
        cast = 0x13000000
        mgr = 0x14000000
        current = 0x15000000
        values = {
            0x15282D8: root,
            root + 0x24: mid,
            mid + 0x8C: host,
            host + 0x1A88: cast,
            host + 0x270: mgr,
            mgr + 0x08: current,
            cast + 0x10: 0x9563,
            cast + 0x18: 0x034D,
            cast + 0x4A0: 1,
            current + 0x04: 4,
            host + 0x41C: 10,
            host + 0x420: 1600,
            host + 0x424: 20,
        }
        with patch.object(
            runner, "_read_remote_u32", side_effect=lambda address: values.get(address, 0)
        ):
            snapshot = runner._snapshot()
        self.assertEqual(snapshot["skill"], 0x9563)
        self.assertEqual(snapshot["config"], 0x034D)
        self.assertEqual(snapshot["perform_type"], 4)
        self.assertEqual(snapshot["gate1"], 1600)

        smoke = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("observer._snapshot()", smoke)

    def test_hang_optional_feature_toggles_are_mutually_scoped(self) -> None:
        from app.ui.pages._impl import SettingsPage

        page = object.__new__(SettingsPage)
        page.var_hang_wanzi_neigong = MagicMock()
        page.var_hang_wanzi_waigong = MagicMock()
        page.var_hang_youfeng = MagicMock()
        page.var_hang_empty = MagicMock()
        page._update_hang_wanzi_ui = MagicMock()
        page._update_hang_tip = MagicMock()

        page.var_hang_wanzi_neigong.get.return_value = True
        SettingsPage._on_hang_wanzi_neigong_toggle(page)
        page.var_hang_wanzi_waigong.set.assert_called_once_with(False)
        page.var_hang_youfeng.set.assert_called_once_with(False)
        page.var_hang_empty.set.assert_not_called()

        page.var_hang_youfeng.reset_mock()
        page.var_hang_wanzi_neigong.reset_mock()
        page.var_hang_wanzi_waigong.reset_mock()
        page.var_hang_empty.reset_mock()
        page.var_hang_youfeng.get.return_value = True
        SettingsPage._on_hang_youfeng_toggle(page)
        page.var_hang_wanzi_neigong.set.assert_called_once_with(False)
        page.var_hang_wanzi_waigong.set.assert_called_once_with(False)
        page.var_hang_empty.set.assert_called_once_with(False)

        page.var_hang_youfeng.reset_mock()
        page.var_hang_wanzi_neigong.reset_mock()
        page.var_hang_wanzi_waigong.reset_mock()
        page.var_hang_empty.get.return_value = True
        SettingsPage._on_hang_empty_toggle(page)
        page.var_hang_youfeng.set.assert_called_once_with(False)
        page.var_hang_wanzi_neigong.set.assert_not_called()
        page.var_hang_wanzi_waigong.set.assert_not_called()

    def test_youfeng_chain_uses_real_background_interrupt_path(self) -> None:
        source = inspect.getsource(YoufengChainRunner)
        self.assertIn("YOUFENG_SKILL_ID", source)
        self.assertIn("YOUFENG_CONFIG_ID", source)
        self.assertIn("gate1", source)
        self.assertIn("VK_BLOCK", source)
        self.assertIn("VK_JUMP", source)
        self.assertIn("allow_softsend=False", source)
        self.assertIn("unexpected skill", source)
        self.assertIn("clear_all=True", source)
        self.assertNotIn("cancel_session", source)
        self.assertNotIn("on_skill_stopped", source)
        self.assertNotIn("remote_write", source)

    def test_youfeng_internal_mode_uses_no_x_or_space_tap(self) -> None:
        runner = YoufengChainRunner(1, interrupt_mode=INTERRUPT_INTERNAL67)
        self.assertEqual(runner.interrupt_mode, INTERRUPT_INTERNAL67)
        source = inspect.getsource(YoufengChainRunner._interrupt_tail)
        self.assertIn("youfeng_internal_chain", source)
        self.assertIn("_wait_interrupt_idle", source)
        internal = source.split("bridge = self._bridge", 1)[1]
        self.assertNotIn("VK_BLOCK", internal)
        self.assertNotIn("VK_JUMP", internal)

    def test_youfeng_next_gate_mode_uses_no_interrupt_action(self) -> None:
        runner = YoufengChainRunner(1, interrupt_mode=INTERRUPT_NEXT_GATE)
        self.assertEqual(runner.interrupt_mode, INTERRUPT_NEXT_GATE)
        source = inspect.getsource(YoufengChainRunner._interrupt_tail)
        gate = source.split("if self.interrupt_mode == INTERRUPT_NEXT_GATE:", 1)[1]
        gate = gate.split("result = bridge.youfeng_internal_chain", 1)[0]
        self.assertIn("youfeng_next_gate", gate)
        self.assertNotIn("VK_BLOCK", gate)
        self.assertNotIn("VK_JUMP", gate)
        self.assertNotIn("youfeng_internal_chain", gate)
        wait_source = inspect.getsource(YoufengChainRunner._wait_final_segment)
        self.assertIn("require_transition", wait_source)
        self.assertIn("left_previous_final", wait_source)

    def test_youfeng_mash_mode_starts_next_cast_without_e07(self) -> None:
        runner = YoufengChainRunner(1, interrupt_mode=INTERRUPT_MASH_CAST)
        self.assertEqual(runner.interrupt_mode, INTERRUPT_MASH_CAST)
        source = inspect.getsource(YoufengChainRunner._interrupt_tail)
        mash = source.split("if self.interrupt_mode == INTERRUPT_MASH_CAST:", 1)[1]
        mash = mash.split("result = bridge.youfeng_internal_chain", 1)[0]
        self.assertIn("youfeng_mash_cast", mash)
        self.assertIn("return True", mash)
        self.assertNotIn("youfeng_internal_chain", mash)
        run_source = inspect.getsource(YoufengChainRunner._run)
        self.assertIn("cast_prestarted", run_source)
        self.assertIn("started_next = self._interrupt_tail()", run_source)

    def test_youfeng_fresh_gate_clears_tail_then_uses_normal_next_key(self) -> None:
        runner = YoufengChainRunner(1, interrupt_mode=INTERRUPT_FRESH_GATE)
        self.assertEqual(runner.interrupt_mode, INTERRUPT_FRESH_GATE)
        source = inspect.getsource(YoufengChainRunner._interrupt_tail)
        fresh = source.split("if self.interrupt_mode == INTERRUPT_FRESH_GATE:", 1)[1]
        fresh = fresh.split("if self.interrupt_mode == INTERRUPT_MASH_CAST:", 1)[0]
        self.assertIn("youfeng_fresh_gate", fresh)
        self.assertIn("return False", fresh)
        self.assertNotIn("youfeng_internal_chain", fresh)
        self.assertNotIn("youfeng_mash_cast", fresh)
        self.assertNotIn("VK_BLOCK", fresh)
        self.assertNotIn("VK_JUMP", fresh)
        ui = Path("app/ui/main_window.py").read_text(encoding="utf-8")
        gate_toggle = ui.split("def _lab_youfeng_gate_toggle", 1)[1].split(
            "def _lab_apply_youfeng_chain_status", 1
        )[0]
        self.assertIn("interrupt_mode=INTERRUPT_QINGGONG_PRECAST", gate_toggle)

    def test_youfeng_exact_cancel_mode_uses_no_action_or_interrupt_key(self) -> None:
        runner = YoufengChainRunner(1, interrupt_mode=INTERRUPT_EXACT_CANCEL)
        self.assertEqual(runner.interrupt_mode, INTERRUPT_EXACT_CANCEL)
        source = inspect.getsource(YoufengChainRunner._interrupt_tail)
        exact = source.split(
            "if self.interrupt_mode == INTERRUPT_EXACT_CANCEL:", 1
        )[1].split("if self.interrupt_mode == INTERRUPT_MASH_CAST:", 1)[0]
        self.assertIn("youfeng_exact_cancel_gate", exact)
        self.assertIn("return False", exact)
        self.assertNotIn("youfeng_internal_chain", exact)
        self.assertNotIn("youfeng_mash_cast", exact)
        self.assertNotIn("VK_BLOCK", exact)
        self.assertNotIn("VK_JUMP", exact)

    def test_youfeng_phase_gate_mode_uses_normal_next_key(self) -> None:
        runner = YoufengChainRunner(1, interrupt_mode=INTERRUPT_PHASE_GATE)
        self.assertEqual(runner.interrupt_mode, INTERRUPT_PHASE_GATE)
        source = inspect.getsource(YoufengChainRunner._interrupt_tail)
        phase = source.split(
            "if self.interrupt_mode == INTERRUPT_PHASE_GATE:", 1
        )[1].split("if self.interrupt_mode == INTERRUPT_MASH_CAST:", 1)[0]
        self.assertIn("youfeng_phase_gate", phase)
        self.assertIn("self._stop.wait(0.16)", phase)
        self.assertIn("return False", phase)
        self.assertNotIn("youfeng_internal_chain", phase)
        self.assertNotIn("youfeng_mash_cast", phase)
        self.assertNotIn("VK_BLOCK", phase)
        self.assertNotIn("VK_JUMP", phase)

    def test_youfeng_qinggong_gate_uses_native_path_and_normal_next_key(self) -> None:
        runner = YoufengChainRunner(1, interrupt_mode=INTERRUPT_QINGGONG_GATE)
        self.assertEqual(runner.interrupt_mode, INTERRUPT_QINGGONG_GATE)
        source = inspect.getsource(YoufengChainRunner._interrupt_tail)
        qinggong = source.split(
            "if self.interrupt_mode == INTERRUPT_QINGGONG_GATE:", 1
        )[1].split("if self.interrupt_mode == INTERRUPT_MASH_CAST:", 1)[0]
        self.assertIn("youfeng_qinggong_gate", qinggong)
        self.assertIn("return False", qinggong)
        self.assertNotIn("VK_JUMP", qinggong)
        self.assertNotIn("VK_BLOCK", qinggong)

    def test_youfeng_qinggong_precast_waits_before_normal_next_key(self) -> None:
        runner = YoufengChainRunner(1, interrupt_mode=INTERRUPT_QINGGONG_PRECAST)
        self.assertEqual(runner.interrupt_mode, INTERRUPT_QINGGONG_PRECAST)
        source = inspect.getsource(YoufengChainRunner._interrupt_tail)
        precast = source.split(
            "if self.interrupt_mode == INTERRUPT_QINGGONG_PRECAST:", 1
        )[1].split("if self.interrupt_mode == INTERRUPT_MASH_CAST:", 1)[0]
        self.assertIn("mode=2", precast)
        self.assertIn("self._stop.wait(0.045)", precast)
        self.assertIn("return False", precast)
        self.assertEqual(
            YoufengChainRunner(1, interrupt_mode=INTERRUPT_QINGGONG_FORCE).interrupt_mode,
            INTERRUPT_QINGGONG_FORCE,
        )
        self.assertEqual(
            YoufengChainRunner(1, interrupt_mode=INTERRUPT_QINGGONG_LONG).interrupt_mode,
            INTERRUPT_QINGGONG_LONG,
        )

    def test_youfeng_qinggong_pulse_stops_lift_before_normal_next_key(self) -> None:
        runner = YoufengChainRunner(1, interrupt_mode=INTERRUPT_QINGGONG_PULSE)
        self.assertEqual(runner.interrupt_mode, INTERRUPT_QINGGONG_PULSE)
        source = inspect.getsource(YoufengChainRunner._interrupt_tail)
        pulse = source.split(
            "if self.interrupt_mode == INTERRUPT_QINGGONG_PULSE:", 1
        )[1].split("if self.interrupt_mode in (", 1)[0]
        self.assertIn("pulse_mode = {24: 5, 0: 6, 4: 7, 8: 8}", pulse)
        self.assertIn("mode=pulse_mode", pulse)
        self.assertIn("self._stop.wait(0.045)", pulse)
        self.assertNotIn("VK_BLOCK", pulse)
        self.assertNotIn("VK_JUMP", pulse)

        with patch("app.core.youfeng_chain.run_next_gate_validation") as validate:
            run_qinggong_pulse_validation(7, 8, transitions=4, timeout_s=20.0)
        self.assertEqual(
            validate.call_args.kwargs["interrupt_mode"],
            INTERRUPT_QINGGONG_PULSE,
        )

    def test_youfeng_ultimate_loop_interleaves_complete_normal_casts(self) -> None:
        self.assertEqual(VK_ALT, 0x12)
        self.assertEqual(VK_YOUFENG, 0x36)
        self.assertEqual(YOUFENG_ULTIMATE_CONFIG_ID, 0x195E)
        self.assertEqual(YOUFENG_ULTIMATE_INTERVAL_S, 11.35)
        runner = YoufengChainRunner(
            1,
            interrupt_mode=INTERRUPT_QINGGONG_PULSE,
            include_ultimate=True,
            pulse_stop_ms=0,
        )
        self.assertTrue(runner.include_ultimate)
        self.assertTrue(runner.observe_ultimate)
        source = inspect.getsource(YoufengChainRunner._run)
        self.assertIn("mode=12", source)
        self.assertIn(
            "ultimate_mode = 13 if self.ultimate_spam else 11", source
        )
        self.assertIn("self._tap_chord((VK_ALT, VK_YOUFENG))", source)
        self.assertIn("self._tap(VK_YOUFENG)", source)
        self.assertIn("pressed + YOUFENG_ULTIMATE_INTERVAL_S", source)
        self.assertIn("expected_config=expected_config", source)
        self.assertIn("self._wait_interrupt_idle()", source)
        self.assertIn("started_next = self._interrupt_tail()", source)

        ui = Path("app/ui/main_window.py").read_text(encoding="utf-8")
        gate = ui.split("def _lab_youfeng_gate_toggle", 1)[1].split(
            "def _lab_youfeng_ultimate_toggle", 1
        )[0]
        ultimate = ui.split("def _lab_youfeng_ultimate_toggle", 1)[1].split(
            "def _lab_apply_youfeng_chain_status", 1
        )[0]
        self.assertIn("INTERRUPT_QINGGONG_PRECAST", gate)
        self.assertNotIn("include_ultimate=True", gate)
        self.assertIn("include_ultimate=True", ultimate)
        self.assertIn("pulse_stop_ms=0", ultimate)
        close = ui.split("def _on_close", 1)[1]
        self.assertIn('"_lab_youfeng_ultimate_runner"', close)
        self.assertIn('"_lab_youfeng_ultimate_spam_runner"', close)

        observer = YoufengChainRunner(
            1,
            interrupt_mode=INTERRUPT_QINGGONG_PULSE,
            drive_casts=False,
            observe_ultimate=True,
            pulse_stop_ms=0,
        )
        self.assertFalse(observer.drive_casts)
        self.assertFalse(observer.include_ultimate)
        self.assertTrue(observer.observe_ultimate)
        self.assertNotIn("VK_ALT", inspect.getsource(observer._interrupt_tail))

        spam = YoufengChainRunner(
            1,
            interrupt_mode=INTERRUPT_QINGGONG_PULSE,
            ultimate_spam=True,
            pulse_stop_ms=0,
        )
        self.assertTrue(spam.drive_casts)
        self.assertTrue(spam.ultimate_spam)
        self.assertTrue(spam.observe_ultimate)
        spam_source = inspect.getsource(YoufengChainRunner._run).split(
            "if self.ultimate_spam:", 1
        )[1].split("last_press:", 1)[0]
        self.assertIn("self._tap_chord((VK_ALT, VK_YOUFENG))", spam_source)
        self.assertIn("self._stop.wait(0.045)", spam_source)
        self.assertNotIn("_wait_final_segment", spam_source)
        self.assertIn("bridge.skill_action_trace(", source)
        self.assertIn("ultimate_trace_armed = True", source)
        self.assertIn("真绝全链路跟踪:", source)

        native = Path("native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("mode == 11 || mode == 13", native)
        self.assertIn("ProcessYoufengUltimateCooldownClear();", native)
        self.assertIn("resolved_key != 0xAu", native)
        self.assertIn("*(uint32_t*)(record + 4) != 10000u", native)
        self.assertIn("mode == 13 ? 1 : 0", native)
        self.assertIn("g_youfeng_next_gate_allow_cleared, 2", native)
        self.assertIn("target_identity || cleared_identity", native)
        self.assertIn("if (due == 0u) return;", native)
        self.assertIn("gate=%ld", native)
        self.assertIn("放行动作门", source)

    def test_qinggong_gate_requires_90_before_session_and_fast_performs(self) -> None:
        rows = [
            {"seq": 1, "kind": "active", "tick": 100, "caller": 0x7639F0,
             "s0": 0x9563, "s10": 0x34D},
            {"seq": 2, "kind": "qinggong_gate", "tick": 500, "ret": 1,
             "sea4": 0x34D},
            {"seq": 3, "kind": "core", "tick": 500, "ret": 0,
             "sea4": 0x34D},
            {"seq": 4, "kind": "active", "tick": 500, "caller": 0x7639F0,
             "s0": 0x9563, "s10": 0x34D},
            {"seq": 5, "kind": "request", "tick": 500, "ret": 0x69,
             "sea4": 0x34D},
            {"seq": 6, "kind": "perform", "tick": 625, "ret": 0,
             "s0": 0x9563, "s10": 0x34D},
            {"seq": 7, "kind": "perform", "tick": 938, "ret": 0,
             "s0": 0x9563, "s10": 0x34D},
        ]
        packets = [
            {"seq": 1, "tick": 100,
             "payload": "2227261F0063950000004D03AA"},
            {"seq": 2, "tick": 490, "payload": "220403900001"},
            {"seq": 3, "tick": 500,
             "payload": "2227261F0063950000004D03BB"},
        ]
        result = analyze_next_gate_trace(
            rows,
            min_transitions=1,
            qinggong_gate=True,
            packet_rows=packets,
        )
        self.assertTrue(result["pass"])
        self.assertTrue(result["transitions"][0]["qinggong_packet_before_session"])
        packets[1]["seq"] = 4
        self.assertFalse(
            analyze_next_gate_trace(
                rows,
                min_transitions=1,
                qinggong_gate=True,
                packet_rows=packets,
            )["pass"]
        )

    def test_next_gate_trace_requires_complete_native_transition(self) -> None:
        rows = [
            {"seq": 1, "kind": "active", "tick": 100, "caller": 0x7639F0,
             "s0": 0x9563, "s10": 0x34D},
            {"seq": 2, "kind": "perform", "tick": 160, "ret": 0,
             "s0": 0x9563, "s10": 0x34D},
            {"seq": 3, "kind": "perform", "tick": 420, "ret": 0,
             "s0": 0x9563, "s10": 0x34D},
            {"seq": 4, "kind": "next_gate", "tick": 500, "ret": 1,
             "sea4": 0x34D},
            {"seq": 5, "kind": "core", "tick": 500, "ret": 0,
             "sea4": 0x34D},
            {"seq": 6, "kind": "active", "tick": 500, "caller": 0x7639F0,
             "s0": 0x9563, "s10": 0x34D},
            {"seq": 7, "kind": "request", "tick": 500, "ret": 0x69,
             "sea4": 0x34D},
            {"seq": 8, "kind": "perform", "tick": 570, "ret": 0,
             "s0": 0x9563, "s10": 0x34D},
            {"seq": 9, "kind": "perform", "tick": 850, "ret": 0,
             "s0": 0x9563, "s10": 0x34D},
        ]
        result = analyze_next_gate_trace(rows, min_transitions=1)
        self.assertTrue(result["pass"])
        self.assertEqual(result["passed_transitions"], 1)
        self.assertEqual(result["e07_events"], 0)
        rows.append(
            {"seq": 10, "kind": "perform", "tick": 900, "ret": 0,
             "s0": 0, "s10": 0xE07}
        )
        self.assertFalse(
            analyze_next_gate_trace(rows, min_transitions=1)["pass"]
        )

    def test_bounded_gate_validation_requires_trace_and_final_idle(self) -> None:
        source = inspect.getsource(run_next_gate_validation)
        self.assertIn("target_cycles = required + 1", source)
        self.assertIn("bridge.skill_action_trace", source)
        self.assertIn("analyze_next_gate_trace", source)
        self.assertIn("final_idle", source)
        self.assertIn("interrupt_mode=interrupt_mode", source)

    def test_fresh_gate_requires_zero_identity_and_fast_server_performs(self) -> None:
        rows = [
            {"seq": 1, "kind": "active", "tick": 100, "caller": 0x7639F0,
             "s0": 0x9563, "s10": 0x34D},
            {"seq": 2, "kind": "perform", "tick": 160, "ret": 0,
             "s0": 0x9563, "s10": 0x34D},
            {"seq": 3, "kind": "perform", "tick": 420, "ret": 0,
             "s0": 0x9563, "s10": 0x34D},
            {"seq": 4, "kind": "fresh_gate", "tick": 500, "ret": 1,
             "c10b": 0x9563, "c18b": 0x34D, "c10a": 0, "c18a": 0},
            {"seq": 5, "kind": "core", "tick": 550, "ret": 0,
             "sea4": 0x34D, "c10b": 0, "c18b": 0},
            {"seq": 6, "kind": "active", "tick": 550, "caller": 0x7639F0,
             "s0": 0x9563, "s10": 0x34D},
            {"seq": 7, "kind": "request", "tick": 550, "ret": 0x69,
             "sea4": 0x34D},
            {"seq": 8, "kind": "perform", "tick": 690, "ret": 0,
             "s0": 0x9563, "s10": 0x34D},
            {"seq": 9, "kind": "perform", "tick": 980, "ret": 0,
             "s0": 0x9563, "s10": 0x34D},
        ]
        result = analyze_next_gate_trace(
            rows, min_transitions=1, fresh_identity=True
        )
        self.assertTrue(result["pass"])
        self.assertEqual(result["transitions"][0]["perform_dt_ms"], [140, 430])
        rows[7]["tick"] = 1950
        rows[8]["tick"] = 2250
        self.assertFalse(
            analyze_next_gate_trace(
                rows, min_transitions=1, fresh_identity=True
            )["pass"]
        )

    def test_fresh_gate_validation_uses_fresh_runner_mode(self) -> None:
        source = inspect.getsource(run_fresh_gate_validation)
        self.assertIn("interrupt_mode=INTERRUPT_FRESH_GATE", source)
        self.assertIn("transitions=transitions", source)

    def test_exact_cancel_validation_uses_exact_runner_mode(self) -> None:
        source = inspect.getsource(run_exact_cancel_validation)
        self.assertIn("interrupt_mode=INTERRUPT_EXACT_CANCEL", source)

    def test_qinggong_validation_uses_qinggong_runner_mode(self) -> None:
        source = inspect.getsource(run_qinggong_gate_validation)
        self.assertIn("interrupt_mode=INTERRUPT_QINGGONG_GATE", source)

    def test_qinggong_precast_validation_uses_precast_runner_mode(self) -> None:
        source = inspect.getsource(run_qinggong_precast_validation)
        self.assertIn("interrupt_mode=INTERRUPT_QINGGONG_PRECAST", source)

    def test_parse_and_classify_core_entry(self) -> None:
        raw = (
            "seq\tkind\ttick\tthread\tcaller\tself\tskill\ta1\ta2\ta3\tret\t"
            "s0\ts4\ts10\ts18\tsea4\tc10b\tc18b\tc4a0b\tc10a\tc18a\tc4a0a\n"
            "1\tcore\t120\t7\t0x53E1ED\t0x1000\t0x2000\t0\t0\t0\t16\t"
            "0\t1\t0\t0xE07\t0xE07\t0x9563\t0x34D\t0\t0\t0xE07\t0\n"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trace.tsv"
            p.write_text(raw, encoding="utf-8")
            rows = parse_native_trace(p)
        self.assertEqual(rows[0]["ret"], 16)
        self.assertEqual(rows[0]["s18"], 0xE07)
        summary = summarize_trace(rows, 100)
        self.assertEqual(summary["verdict"], "cancast_only_no_active")
        self.assertEqual(summary["core_returns"], [16])

    def test_classify_upstream_block(self) -> None:
        rows = [
            {"kind": "outer", "tick": 90, "ret": 0, "caller": 1, "skill": 2}
        ]
        summary = summarize_trace(rows, 100)
        self.assertEqual(summary["verdict"], "no_action_request_or_active")

    def test_summary_counts_targeted_next_action_gate(self) -> None:
        rows = [
            {
                "kind": "next_gate",
                "tick": 120,
                "ret": 1,
                "sea4": 0x34D,
                "c10b": 0x9563,
                "c18b": 0x34D,
            }
        ]
        summary = summarize_trace(
            rows, 100, target_skill_id=0x9563, target_config_id=0x34D
        )
        self.assertEqual(summary["next_gate_after"], 1)

    def test_active_event_is_real_session_evidence(self) -> None:
        rows = [
            {
                "kind": "active",
                "tick": 120,
                "caller": 0x7639F0,
                "skill": 0x3000,
                "s0": 0x9563,
                "s10": 0x34D,
            }
        ]
        summary = summarize_trace(
            rows, 100, target_skill_id=0x9563, target_config_id=0x34D
        )
        self.assertEqual(summary["verdict"], "entered_new_active_session")
        self.assertEqual(summary["active_after"], 1)
        self.assertEqual(summary["session_start_after"], 1)

    def test_onperform_refill_is_not_a_new_session(self) -> None:
        rows = [
            {
                "kind": "active",
                "tick": 120,
                "caller": 0x76159A,
                "skill": 0x3000,
                "s0": 0x9563,
                "s10": 0x34D,
            }
        ]
        summary = summarize_trace(rows, 100)
        self.assertEqual(summary["verdict"], "perform_refill_only")
        self.assertEqual(summary["session_start_after"], 0)
        self.assertEqual(summary["perform_refill_after"], 1)
        self.assertEqual(summary["suppressed_refill_after"], 0)

    def test_suppressed_onperform_refill_is_counted(self) -> None:
        rows = [
            {
                "kind": "active",
                "tick": 120,
                "caller": 0x76159A,
                "skill": 0x3000,
                "s0": 0x9563,
                "s10": 0x34D,
                "ret": 1,
            }
        ]
        summary = summarize_trace(rows, 100)
        self.assertEqual(summary["verdict"], "perform_refill_only")
        self.assertEqual(summary["suppressed_refill_after"], 1)

    def test_suppressed_whole_onperform_is_decisive(self) -> None:
        rows = [
            {
                "kind": "perform",
                "tick": 120,
                "caller": 0x7E3DC3,
                "skill": 0x3000,
                "s0": 0x9563,
                "s10": 0x34D,
                "ret": 1,
            }
        ]
        summary = summarize_trace(rows, 100)
        self.assertEqual(summary["verdict"], "perform_suppressed_no_request")
        self.assertEqual(summary["perform_after"], 1)
        self.assertEqual(summary["suppressed_perform_after"], 1)

    def test_deferred_restop_is_reported(self) -> None:
        rows = [{"kind": "restop", "tick": 120, "ret": 1, "self": 0x1000}]
        summary = summarize_trace(rows, 100)
        self.assertEqual(summary["restop_after"], 1)
        self.assertEqual(summary["restop_return_counts"], {1: 1})

    def test_charged_channel_scenario_is_exposed(self) -> None:
        from app.core.skill_action_trace import SCENARIO_LABELS

        self.assertIn(SCENARIO_CHARGED_CHANNEL, SCENARIO_LABELS)
        self.assertIn("蓄力", SCENARIO_LABELS[SCENARIO_CHARGED_CHANNEL])

    def test_action_request_return_is_decisive_without_new_session(self) -> None:
        rows = [
            {
                "kind": "request",
                "tick": 120,
                "caller": 0xAABBCC,
                "self": 0x1000,
                "skill": 0x2000,
                "sea4": 0x34D,
                "a1": 1,
                "a2": 0x3000,
                "ret": 0x67,
            },
            {
                "kind": "active",
                "tick": 121,
                "caller": 0x76159A,
                "skill": 0x3000,
                "s0": 0x9563,
                "s10": 0x34D,
            },
        ]
        summary = summarize_trace(rows, 100, target_config_id=0x34D)
        self.assertEqual(summary["verdict"], "action_request_rejected")
        self.assertEqual(summary["request_after"], 1)
        self.assertEqual(summary["request_return_counts"], {0x67: 1})
        self.assertEqual(summary["request_modes"], [1])
        self.assertEqual(summary["request_precomputed_ptrs"], [0x3000])

    def test_request_for_other_skill_config_is_filtered(self) -> None:
        rows = [
            {
                "kind": "request",
                "tick": 120,
                "caller": 0x123456,
                "skill": 0x2000,
                "sea4": 0xE07,
                "ret": 0x69,
            }
        ]
        summary = summarize_trace(rows, 100, target_config_id=0x34D)
        self.assertEqual(summary["verdict"], "no_action_request_or_active")
        self.assertEqual(summary["request_all_after"], 1)
        self.assertEqual(summary["request_after"], 0)

    def test_report_contains_decisive_fields(self) -> None:
        result = {
            "ok": True,
            "scenario": SCENARIO_REAL_X,
            "label": "真实X",
            "marker_tick": 100,
            "native_path": "trace.tsv",
            "rows": [],
            "summary": {
                "verdict": "entered_new_active_session",
                "total_events": 2,
                "after_marker_events": 1,
                "outer_after": 0,
                "core_after": 1,
                "active_after": 1,
                "outer_returns": [],
                "core_returns": [0x10],
                "active_skill_ids": [0x9563],
                "active_config_ids": [0x34D],
                "callers": [0x53E1ED],
                "skill_ptrs": [0x2000],
                "skill_ea4": [0xE07],
                "skill_18": [0xE07],
                "after_rows": [],
            },
        }
        with tempfile.TemporaryDirectory() as td, patch(
            "common.paths.app_root", return_value=Path(td)
        ):
            paths = write_trace_report(result)
            text = Path(paths["md"]).read_text(encoding="utf-8")
        self.assertIn("entered_new_active_session", text)
        self.assertIn("core_after=1 returns=[16]", text)


if __name__ == "__main__":
    unittest.main()
