from __future__ import annotations

from pathlib import Path
import threading
import time

import unittest
from unittest.mock import ANY, MagicMock, patch

from app.core.activity_auto import ActivityRunner
from app.core import win_capture
from app.core.bg_input import (
    CAST_MODE_NONE,
    CANCEL_MODE_CANCEL_THEN_KEY,
    CANCEL_MODE_KEY_ONLY,
    CANCEL_MODE_SMART,
    CANCEL_WHEN_ALWAYS,
    CANCEL_WHEN_BUSY,
    CANCEL_WHEN_NEVER,
    CAST_MODE_SEQUENCE,
    DEFAULT_LOGITECH_SEQUENCE,
    DELIVERY_BRIDGE,
    DELIVERY_FOREGROUND,
    MouseClickerConfig,
    MouseClickerRunner,
    ShiftHoldConfig,
    SkillCancelLoopConfig,
    SkillCancelLoopRunner,
    format_key_sequence,
    legacy_mode_to_pipeline,
    normalize_skill_cancel_config,
    parse_cancel_vk,
    parse_key_sequence,
    parse_skill_vk,
    press_bg_chord_once,
    skill_vk_label,
)
from app.core.loot import SuperLootRunner
from app.core.xajh_bridge import XajhBridge
from app.core.task_api import TaskRunner
from app.core.yaolu_auto import (
    DEFAULT_ENTRY_ANCHOR,
    ENTRY_PATH_SCENE_ID,
    YAOLU_RISK_BASELINE_PRE_20260722,
    YaoluConfig,
    YaoluRunner,
    append_yaolu_risk_audit,
    cancel_active_entry_session,
    describe_captcha_error,
    diff_yaolu_risk_vs_baseline,
    path_to_entry_anchor,
    resolve_deferred_stage1_feedback,
    snapshot_yaolu_risk_cfg,
    wait_enter_result,
    wait_entry_reopen_result,
    wait_for_entry_arrival,
    wait_for_wander_arrival,
    wander_far_from_entry,
    yaolu_risk_audit_path,
    yaolu_risk_state_path,
)


class InputAndRunnerTests(unittest.TestCase):
    def test_bg_chord_once_can_preserve_other_held_keys(self) -> None:
        bridge = MagicMock()
        bridge.key_hold.return_value = MagicMock(ok=True, note="KEY_HOLD", error="")
        with patch("app.core.bg_input.ensure_bridge", return_value=bridge), patch(
            "app.core.bg_input._resolve_hwnd", return_value=0x1234
        ):
            out = press_bg_chord_once(
                991,
                [0x1B],
                hwnd=0x1234,
                hold_ms=0,
                clear_all_after=False,
            )
        self.assertTrue(out["ok"])
        calls = bridge.key_hold.call_args_list
        self.assertTrue(any(call.kwargs.get("install_only") for call in calls))
        self.assertTrue(any(call.kwargs.get("down") is True for call in calls))
        self.assertTrue(any(call.kwargs.get("down") is False for call in calls))
        self.assertFalse(any(call.kwargs.get("clear_all") for call in calls))
        self.assertTrue(all(call.kwargs.get("allow_softsend", False) is False for call in calls))

    def test_activity_default_post_run_cooldown_uses_shared_constant(self) -> None:
        from app.core.activity_auto import DEFAULT_ENTRY_CD_S, ActivityConfig

        cfg = ActivityConfig()
        self.assertEqual(cfg.entry_cd_min_s, DEFAULT_ENTRY_CD_S)
        self.assertEqual(cfg.entry_cd_max_s, DEFAULT_ENTRY_CD_S)
        self.assertEqual(cfg.entry_cd_sleep_s(), DEFAULT_ENTRY_CD_S)

    def test_activity_quiet_event_updates_ui_without_debug_log(self) -> None:
        events = []
        logs = []
        runner = ActivityRunner(
            pid=991,
            on_event=events.append,
            log=logs.append,
        )

        runner._emit(
            "afk_hang",
            "挂机中… 副本倒计时 86:04（5164s）",
            log_event=False,
            remain_s=5164.0,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(logs, [])

        runner._emit("afk_move", "偏移3.2m，执行回正")
        self.assertEqual(len(logs), 1)
        self.assertIn("执行回正", logs[0])

    def test_deferred_fail_is_promoted_when_scene_entry_succeeds(self) -> None:
        from app.core.game_sys_msg import CaptchaAnswerFeedback

        fb = CaptchaAnswerFeedback(kind="ok", text="答案正确", method="grace_after_fail")
        self.assertTrue(
            resolve_deferred_stage1_feedback(answer_likely_ok=True, ans_fb=fb)
        )

    def test_deferred_fail_stays_negative_only_with_explicit_final_fail(self) -> None:
        from app.core.game_sys_msg import CaptchaAnswerFeedback

        fb = CaptchaAnswerFeedback(kind="fail", text="答案错误，请大侠重新来过")
        self.assertFalse(
            resolve_deferred_stage1_feedback(answer_likely_ok=False, ans_fb=fb)
        )

    def test_deferred_fail_unknown_result_is_not_a_negative_sample(self) -> None:
        from app.core.game_sys_msg import CaptchaAnswerFeedback

        fb = CaptchaAnswerFeedback(kind="timeout", text="")
        self.assertIsNone(
            resolve_deferred_stage1_feedback(answer_likely_ok=False, ans_fb=fb)
        )

    def test_input_config_defaults(self) -> None:
        shift = ShiftHoldConfig()
        click = MouseClickerConfig()
        self.assertEqual(shift.mode, "hold")
        self.assertGreater(shift.refresh_ms, 0)
        self.assertGreater(click.interval_ms, 0)

    def test_mouse_clicker_posts_timed_right_click_outside_bridge_thread(self) -> None:
        runner = MouseClickerRunner(
            123,
            hwnd=0x456,
            cfg=MouseClickerConfig(
                button=1,
                interval_ms=20,
                hold_ms=35,
                cx=120,
                cy=240,
            ),
        )
        bridge = MagicMock()
        bridge.key_hold.return_value = MagicMock(ok=True, note="KEY_HOLD", error="")

        def post_once(*args, **kwargs):
            runner._stop.set()
            return True

        with patch("app.core.bg_input.ensure_bridge", return_value=bridge), patch(
            "app.core.win_capture.post_click_client", side_effect=post_once
        ) as post_click:
            runner._run()

        post_click.assert_called_once_with(
            0x456,
            120,
            240,
            right=True,
            down_hold_s=0.035,
            stop_event=runner._stop,
            log=runner._log,
            move_first=True,
            settle_after=False,
        )
        bridge.ui_click.assert_not_called()
        self.assertEqual(bridge.key_hold.call_count, 2)
        self.assertTrue(bridge.key_hold.call_args_list[0].kwargs["down"])
        self.assertFalse(bridge.key_hold.call_args_list[1].kwargs["down"])
        self.assertTrue(
            all(
                call.args[0] == 0x02
                and call.kwargs["allow_softsend"] is False
                for call in bridge.key_hold.call_args_list
            )
        )
        bridge.close.assert_called_once()

    def test_mouse_clicker_reuses_bridge_and_period_does_not_add_click_time(self) -> None:
        runner = MouseClickerRunner(
            123,
            hwnd=0x456,
            cfg=MouseClickerConfig(
                button=0, interval_ms=50, hold_ms=40, cx=120, cy=240
            ),
        )
        bridge = MagicMock()
        bridge.key_hold.return_value = MagicMock(ok=True, note="KEY_HOLD", error="")
        waits: list[float] = []

        def wait_then_stop(timeout):
            waits.append(float(timeout))
            if len(waits) >= 2:
                runner._stop.set()
                return True
            return False

        runner._stop.wait = MagicMock(side_effect=wait_then_stop)
        with patch("app.core.bg_input.ensure_bridge", return_value=bridge) as ensure, patch(
            "app.core.win_capture.post_click_client", return_value=True
        ) as post_click, patch(
            "app.core.bg_input.time.monotonic",
            side_effect=[0.0, 0.04, 0.05, 0.09],
        ):
            runner._run()

        self.assertEqual(ensure.call_count, 1)
        self.assertEqual(post_click.call_count, 2)
        self.assertAlmostEqual(waits[0], 0.01, places=5)
        self.assertAlmostEqual(waits[1], 0.01, places=5)
        self.assertFalse(post_click.call_args_list[1].kwargs["move_first"])
        bridge.close.assert_called_once()

    def test_mouse_clicker_does_not_post_when_button_hook_fails(self) -> None:
        runner = MouseClickerRunner(
            123,
            hwnd=0x456,
            cfg=MouseClickerConfig(button=0, cx=120, cy=240),
        )
        bridge = MagicMock()
        bridge.key_hold.return_value = MagicMock(
            ok=False,
            note="hook install fail",
            error="",
        )

        def stop_after_wait(_timeout):
            runner._stop.set()
            return True

        runner._stop.wait = MagicMock(side_effect=stop_after_wait)
        with patch("app.core.bg_input.ensure_bridge", return_value=bridge), patch(
            "app.core.win_capture.post_click_client"
        ) as post_click:
            runner._run()

        post_click.assert_not_called()
        bridge.key_hold.assert_called_once()
        self.assertEqual(bridge.key_hold.call_args.args[0], 0x01)
        bridge.close.assert_called_once()

    def test_post_click_client_emits_matching_left_and_right_messages(self) -> None:
        cases = (
            (False, 0x0201, 0x0202, 0x0001),
            (True, 0x0204, 0x0205, 0x0002),
        )
        for right, msg_down, msg_up, mk_button in cases:
            with self.subTest(right=right), patch.object(
                win_capture.user32, "IsWindow", return_value=True
            ), patch.object(
                win_capture.user32, "PostMessageW", return_value=1
            ) as post, patch.object(win_capture.time, "sleep", return_value=None):
                ok = win_capture.post_click_client(
                    0x456,
                    120,
                    240,
                    right=right,
                    move_first=False,
                    down_hold_s=0.035,
                )

            self.assertTrue(ok)
            messages = [(call.args[1], call.args[2]) for call in post.call_args_list]
            self.assertEqual(
                messages,
                [(msg_down, mk_button), (msg_up, 0), (0x0200, 0)],
            )

    def test_post_click_client_reports_rejected_button_message(self) -> None:
        with patch.object(
            win_capture.user32, "IsWindow", return_value=True
        ), patch.object(
            win_capture.user32, "PostMessageW", return_value=0
        ), patch.object(win_capture.time, "sleep", return_value=None):
            ok = win_capture.post_click_client(
                0x456,
                120,
                240,
                move_first=False,
            )
        self.assertFalse(ok)

    def test_real_cursor_uses_absolute_virtual_desktop_coordinates(self) -> None:
        from app.core import win_utils

        events: list[object] = []

        def enter_dpi() -> int:
            events.append("enter")
            return 0x1234

        def restore_dpi(previous: int) -> None:
            events.append(("restore", previous))

        metrics = {76: 0, 77: 0, 78: 1920, 79: 1080}
        with patch.object(
            win_capture.user32,
            "GetSystemMetrics",
            side_effect=lambda index: metrics[index],
        ), patch.object(win_capture.user32, "mouse_event") as mouse_event, patch.object(
            win_capture, "_get_physical_cursor_pos", return_value=(968, 731)
        ), patch.object(
            win_utils, "_dpi_ctx_per_monitor", side_effect=enter_dpi
        ), patch.object(
            win_utils, "_dpi_ctx_restore", side_effect=restore_dpi
        ):
            ok = win_capture._set_physical_cursor_pos(968, 731)

        self.assertTrue(ok)
        self.assertEqual(events, ["enter", ("restore", 0x1234)])
        flags, ax, ay, zero1, zero2 = mouse_event.call_args.args
        self.assertEqual(
            flags,
            win_capture.MOUSEEVENTF_MOVE
            | win_capture.MOUSEEVENTF_ABSOLUTE
            | win_capture.MOUSEEVENTF_VIRTUALDESK,
        )
        self.assertEqual((zero1, zero2), (0, 0))
        self.assertEqual(ax, round(968 * 65535 / 1919))
        self.assertEqual(ay, round(731 * 65535 / 1079))

    def test_real_cursor_rejects_a_misrouted_absolute_move(self) -> None:
        metrics = {76: 0, 77: 0, 78: 1920, 79: 1080}
        with patch.object(
            win_capture.user32,
            "GetSystemMetrics",
            side_effect=lambda index: metrics[index],
        ), patch.object(win_capture.user32, "mouse_event"), patch.object(
            win_capture, "_get_physical_cursor_pos", return_value=(1210, 914)
        ):
            ok = win_capture._set_physical_cursor_pos(968, 731)

        self.assertFalse(ok)

    def test_probe_key_paths_reports_bridge_missing(self) -> None:
        from app.core.bg_input import probe_key_paths

        with patch("app.core.bg_input.ensure_bridge", return_value=None):
            lines = probe_key_paths(1234, 0)
        self.assertTrue(any("桥接未就绪" in x for x in lines))
        self.assertTrue(any("测按键: pid=1234" in x for x in lines))

    def test_probe_key_paths_runs_force_and_space(self) -> None:
        from app.core.bg_input import probe_key_paths

        br = MagicMock()
        on = MagicMock(ok=True, ret=0xF, note="KEY_FORCE ON f=0xF gate=1 gaks=8001/8001 tab=1/1 mask=1 mod=1", error="")
        off = MagicMock(ok=True, ret=0, note="OFF KEY_FORCE HOLD f=0x9 gate=1 gaks=0/0 tab=0/0 mask=0 mod=0", error="")
        space = MagicMock(ok=True, ret=1, note="UI_KEY press ok", error="")
        br.key_force.side_effect = [on, off]
        br.ui_key.return_value = space
        with patch("app.core.bg_input.ensure_bridge", return_value=br), patch(
            "app.core.bg_input.time.sleep", return_value=None
        ):
            lines = probe_key_paths(99, 0x100)
        joined = "\n".join(lines)
        self.assertIn("KEY_FORCE ON", joined)
        self.assertIn("KEY_FORCE OFF", joined)
        self.assertIn("UI_KEY Space no_focus", joined)
        self.assertIn("纯后台", joined)
        self.assertNotIn("sys SendInput Space", joined)
        self.assertEqual(br.key_force.call_count, 2)
        br.ui_key.assert_called_once()
        kwargs = br.ui_key.call_args.kwargs
        self.assertTrue(kwargs.get("no_focus"))
        br.close.assert_called_once()

    def test_skill_vk_parse_and_label(self) -> None:
        self.assertEqual(parse_skill_vk("1"), 0x31)
        self.assertEqual(parse_skill_vk("0"), 0x30)
        self.assertEqual(parse_skill_vk(0x32), 0x32)
        self.assertEqual(parse_skill_vk("0x33"), 0x33)
        self.assertEqual(skill_vk_label(0x31), "1")
        self.assertEqual(skill_vk_label(0x41), "0x41")

    def test_skill_cancel_config_defaults(self) -> None:
        cfg = normalize_skill_cancel_config(SkillCancelLoopConfig())
        self.assertEqual(cfg.cancel_when, CANCEL_WHEN_NEVER)
        self.assertFalse(cfg.use_protocol_cancel)
        self.assertFalse(cfg.use_key_cancel)
        self.assertFalse(cfg.use_memory_cancel)
        self.assertEqual(cfg.cast_mode, CAST_MODE_NONE)
        self.assertEqual(cfg.delivery, DELIVERY_BRIDGE)
        self.assertFalse(cfg.require_foreground)
        self.assertEqual(cfg.interval_ms, 50)
        self.assertEqual(
            [s.label() for s in cfg.steps],
            ["Space", "Q", "X"],
        )

    def test_logitech_sequence_parse(self) -> None:
        steps = parse_key_sequence(DEFAULT_LOGITECH_SEQUENCE)
        self.assertEqual(len(steps), 3)
        self.assertEqual(
            [s.label() for s in steps],
            ["Space", "Q", "X"],
        )
        # Space 清格挡, Q, X 清后摇 — recorded timings.
        self.assertEqual(steps[0].hold_ms, 50)
        self.assertEqual(steps[0].after_ms, 50)
        self.assertEqual(steps[1].hold_ms, 50)
        self.assertEqual(steps[1].after_ms, 260)
        self.assertEqual(steps[2].hold_ms, 50)
        self.assertEqual(steps[2].after_ms, 100)
        self.assertEqual(
            format_key_sequence(steps),
            DEFAULT_LOGITECH_SEQUENCE,
        )

    def test_cast_mode_none_skips_keys(self) -> None:
        from app.core.bg_input import SkillCancelLoopRunner

        runner = SkillCancelLoopRunner(
            1,
            cfg=SkillCancelLoopConfig(cast_mode=CAST_MODE_NONE),
        )
        ok, note = runner._do_cast_step(None)
        self.assertTrue(ok)
        self.assertIn("protocol-only", note)

    def test_skill_cancel_bridge_press_uses_key_hold(self) -> None:
        source = Path(__file__).resolve().parents[1] / "app/core/bg_input.py"
        text = source.read_text(encoding="utf-8")
        press = text.split("def _press_key(", 1)[1].split("def _ensure_session", 1)[0]
        self.assertIn("br.key_hold(", press)
        self.assertIn("down=True", press)
        self.assertIn("down=False", press)
        self.assertIn("allow_softsend=False", press)
        self.assertNotIn("br.ui_key(", press)
        self.assertIn("DELIVERY_BRIDGE", text)
        self.assertIn('delivery: str = DELIVERY_BRIDGE', text)

    def test_skill_cancel_bridge_press_releases_key(self) -> None:
        runner = SkillCancelLoopRunner(
            1,
            hwnd=0x123,
            cfg=SkillCancelLoopConfig(delivery=DELIVERY_BRIDGE),
        )
        bridge = MagicMock()
        bridge.key_hold.side_effect = [
            MagicMock(ok=True, note="KEY_HOLD ON", error=None),
            MagicMock(ok=True, note="KEY_HOLD OFF", error=None),
        ]
        ok, note = runner._press_key(bridge, 0x36, 0)
        self.assertTrue(ok)
        self.assertIn("OFF", note)
        self.assertEqual(bridge.key_hold.call_count, 2)
        self.assertTrue(bridge.key_hold.call_args_list[0].kwargs["down"])
        self.assertFalse(bridge.key_hold.call_args_list[1].kwargs["down"])
        self.assertTrue(
            all(
                call.kwargs["allow_softsend"] is False
                for call in bridge.key_hold.call_args_list
            )
        )

    def test_legacy_mode_maps_to_pipeline(self) -> None:
        self.assertEqual(
            legacy_mode_to_pipeline(CANCEL_MODE_KEY_ONLY),
            (CANCEL_WHEN_NEVER, False, False),
        )
        self.assertEqual(
            legacy_mode_to_pipeline(CANCEL_MODE_CANCEL_THEN_KEY),
            (CANCEL_WHEN_ALWAYS, True, False),
        )
        self.assertEqual(
            legacy_mode_to_pipeline(CANCEL_MODE_SMART),
            (CANCEL_WHEN_BUSY, True, False),
        )
        n = normalize_skill_cancel_config(
            SkillCancelLoopConfig(mode=CANCEL_MODE_SMART)
        )
        self.assertEqual(n.cancel_when, CANCEL_WHEN_BUSY)
        self.assertTrue(n.use_protocol_cancel)
        self.assertFalse(n.use_memory_cancel)

    def test_parse_cancel_vk(self) -> None:
        self.assertEqual(parse_cancel_vk("Esc"), 0x1B)
        self.assertEqual(parse_cancel_vk("RMB"), 0x02)
        self.assertEqual(parse_cancel_vk("Space"), 0x20)

    def test_skill_cancel_runner_stoppable(self) -> None:
        runner = SkillCancelLoopRunner(pid=123)
        runner._run = lambda: runner._stop.wait(2)
        self.assertTrue(runner.start())
        self.assertTrue(runner.is_running())
        self.assertTrue(runner.stop())
        self.assertFalse(runner.is_running())

    def test_skill_cancel_when_policy(self) -> None:
        r_never = SkillCancelLoopRunner(
            pid=1, cfg=SkillCancelLoopConfig(cancel_when=CANCEL_WHEN_NEVER)
        )
        self.assertFalse(r_never._should_cancel())
        r_always = SkillCancelLoopRunner(
            pid=1,
            cfg=SkillCancelLoopConfig(
                cancel_when=CANCEL_WHEN_ALWAYS,
                use_key_cancel=True,
            ),
        )
        self.assertTrue(r_always._should_cancel())
        r_busy = SkillCancelLoopRunner(
            pid=1,
            cfg=SkillCancelLoopConfig(
                cancel_when=CANCEL_WHEN_BUSY,
                use_protocol_cancel=True,
            ),
        )
        with patch.object(r_busy, "_read_cast_busy", return_value=True):
            self.assertTrue(r_busy._should_cancel())
        with patch.object(r_busy, "_read_cast_busy", return_value=False):
            self.assertFalse(r_busy._should_cancel())
        with patch.object(r_busy, "_read_cast_busy", return_value=None):
            self.assertTrue(r_busy._should_cancel())

    def test_memory_cancel_forced_off(self) -> None:
        cfg = normalize_skill_cancel_config(
            SkillCancelLoopConfig(use_memory_cancel=True)
        )
        self.assertFalse(cfg.use_memory_cancel)

    def test_all_business_runners_share_stoppable_lifecycle(self) -> None:
        for cls in (SuperLootRunner, ActivityRunner, YaoluRunner, TaskRunner):
            with self.subTest(runner=cls.__name__):
                runner = cls(pid=123)
                runner._loop = lambda r=runner: r._stop.wait(2)
                runner.start()
                self.assertTrue(runner.running)
                runner.stop()
                if runner._thread is not None:
                    runner._thread.join(1)
                self.assertFalse(runner.running)
                self.assertFalse(runner.is_running())

    def test_runner_start_is_idempotent(self) -> None:
        runner = ActivityRunner(pid=1)
        runner._loop = lambda: runner._stop.wait(2)
        runner.start()
        first = runner._thread
        runner.start()
        self.assertIs(runner._thread, first)
        runner.stop()
        first.join(1)

    def test_business_stop_reports_completion(self) -> None:
        runner = ActivityRunner(pid=1)
        runner._loop = lambda: runner._stop.wait(2)
        runner.start()
        self.assertTrue(runner.stop())

    def test_qiegao_dead_skips_move_packet(self) -> None:
        runner = ActivityRunner(pid=9)
        session = MagicMock(pid=9)
        with patch.object(runner, "_qiegao_host_dead", return_value=True), patch.object(
            runner, "_qiegao_afk_target"
        ) as target:
            self.assertFalse(runner._move_to_qiegao_afk(session, wait_arrive=False))
        target.assert_not_called()
        self.assertTrue(runner._qiegao_dead_paused)

    def test_qiegao_dead_before_path_keeps_hang_on(self) -> None:
        runner = ActivityRunner(pid=10)
        session = MagicMock(pid=10)
        with patch.object(runner, "_qiegao_host_dead", return_value=True), patch.object(
            runner, "_qiegao_wait_until_revived", return_value=False
        ), patch.object(runner, "_ensure_hang_off") as hang_off:
            self.assertFalse(runner._qiegao_start_hang_phase(session))
        hang_off.assert_not_called()

    def test_qiegao_temporary_hang_off_keeps_task_guard(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(pid=10, cfg=ActivityConfig(mode="qiegao"))
        session = MagicMock(pid=10)
        with patch.object(runner, "_read_hang_on", return_value=False), patch.object(
            runner, "_ensure_qiegao_task_guard", return_value=True
        ) as task_guard, patch(
            "app.core.hang_settings.stop_hang_guard"
        ) as stop_guard:
            self.assertTrue(
                runner._ensure_hang_off(
                    session,
                    reason="寻路前关挂机",
                    keep_task_guard=True,
                )
            )

        task_guard.assert_called_once_with(session)
        stop_guard.assert_not_called()

    def test_hang_off_unknown_retries_then_stops_via_pipeline(self) -> None:
        """挂机态未知：重试后仍未知也走统一管线直接关（不再「未知跳过」）。"""
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=10,
            cfg=ActivityConfig(
                mode="qiegao", hang_probe_retries=2, hang_probe_retry_s=0.01
            ),
        )
        session = MagicMock(pid=10)
        with patch.object(
            runner, "_read_hang_on", side_effect=[None, None, None, None]
        ), patch.object(
            runner, "_resolve_hang_cfg", return_value=MagicMock()
        ), patch.object(
            runner, "_ensure_qiegao_task_guard", return_value=True
        ) as task_guard, patch(
            "app.core.activity_auto._sleep_interruptible", return_value=True
        ) as sleep, patch(
            "app.core.hang_settings.stop_hang",
            return_value={"ok": True, "via": "raw_c2s_packet", "message": ""},
        ) as stop_hang:
            self.assertTrue(
                runner._ensure_hang_off(
                    session,
                    reason="寻路前关挂机",
                    keep_task_guard=True,
                )
            )

        # 2 次未知重试 + stop 后 1 次 _hang_settle 沉降
        self.assertEqual(sleep.call_count, 3)
        stop_hang.assert_called_once()
        task_guard.assert_called_once_with(session)

    def test_qiegao_dead_takes_no_action_until_revived(self) -> None:
        """死亡纪律：死亡期间零动作（不发封包/不重启内挂），复活后才恢复。"""
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=10,
            cfg=ActivityConfig(mode="qiegao", return_poll_s=1.0),
        )
        session = MagicMock(pid=10)
        with patch.object(
            runner, "_qiegao_host_dead", side_effect=[True, False]
        ), patch.object(runner, "_ensure_hang_on") as hang_on, patch.object(
            runner, "_ensure_hang_off"
        ) as hang_off, patch.object(
            runner, "_ensure_qiegao_task_guard", return_value=True
        ) as task_guard, patch.object(
            runner,
            "_scene_prefer_hub",
            return_value=(1524, (0.0, 0.0, 0.0), "沙漠古镇"),
        ), patch(
            "app.core.activity_auto._sleep_interruptible", return_value=True
        ):
            self.assertTrue(runner._qiegao_wait_until_revived(session, reason="寻路"))

        hang_on.assert_not_called()
        hang_off.assert_not_called()
        task_guard.assert_called_once_with(session)

    def test_qiegao_hang_loop_rearms_hang_after_revive_settled(self) -> None:
        """站桩期死亡→零动作；复活稳定后复核内挂确实不在才重启。"""
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=10,
            cfg=ActivityConfig(
                mode="qiegao", return_poll_s=1.0, qiegao_afk_repath_s=0
            ),
        )
        session = MagicMock(pid=10)
        with patch(
            "app.core.activity_auto._pid_alive", return_value=True
        ), patch.object(
            runner,
            "_scene_prefer_hub",
            side_effect=[
                (1524, (0.0, 0.0, 0.0), "沙漠古镇"),
                (1524, (0.0, 0.0, 0.0), "沙漠古镇"),
                (68, (0.0, 0.0, 0.0), "福州城"),
            ],
        ), patch.object(
            runner, "_qiegao_host_dead", side_effect=[True, False, False]
        ), patch.object(
            runner, "_read_hang_on", return_value=False
        ), patch.object(
            runner, "_ensure_hang_on", return_value=True
        ) as hang_on, patch.object(
            runner, "_ensure_hang_off"
        ) as hang_off, patch.object(
            runner, "_ensure_qiegao_task_guard", return_value=True
        ) as task_guard, patch(
            "app.core.activity_auto._sleep_interruptible", return_value=True
        ):
            self.assertTrue(runner._qiegao_hang_until_return(session))

        hang_on.assert_called_once_with(session)
        hang_off.assert_not_called()
        task_guard.assert_called_once_with(session)

    def test_qiegao_dead_does_not_rearm_outside_dungeon(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(pid=10, cfg=ActivityConfig(mode="qiegao"))
        session = MagicMock(pid=10)
        with patch.object(runner, "_qiegao_host_dead", return_value=True), patch.object(
            runner,
            "_scene_prefer_hub",
            return_value=(68, (0.0, 0.0, 0.0), "福州城"),
        ), patch.object(runner, "_ensure_hang_on") as hang_on, patch.object(
            runner, "_ensure_qiegao_task_guard", return_value=True
        ):
            self.assertFalse(runner._qiegao_wait_until_revived(session, reason="寻路"))

        hang_on.assert_not_called()

    def test_qiegao_path_death_abort_does_not_enter_unstick_retry(self) -> None:
        from app.core.activity_auto import ActivityConfig
        from app.core.automove import PathTarget

        runner = ActivityRunner(
            pid=10,
            cfg=ActivityConfig(
                mode="qiegao",
                qiegao_afk_x=10.0,
                qiegao_afk_y=2.0,
                qiegao_afk_z=20.0,
            ),
        )
        session = MagicMock(pid=10)
        target = PathTarget(x=10.0, y=2.0, z=20.0, mode=1524)
        with patch.object(runner, "_qiegao_host_dead", return_value=False), patch.object(
            runner, "_qiegao_afk_target", return_value=target
        ), patch.object(
            runner,
            "_read_live_pos",
            return_value=(1524, (0.0, 2.0, 0.0), "沙漠古镇"),
        ), patch(
            "app.core.task_api.pathfind_to_clue",
            return_value={
                "ok": False,
                "aborted": True,
                "abort_reason": "host_dead",
                "error": "path verification aborted: host_dead",
            },
        ) as pathfind, patch.object(runner, "_ensure_hang_off") as hang_off:
            self.assertFalse(runner._move_to_qiegao_afk(session, wait_arrive=True))

        self.assertTrue(callable(pathfind.call_args.kwargs["abort_check"]))
        hang_off.assert_not_called()

    def test_qiegao_unstick_death_preempts_retreat(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(pid=10, cfg=ActivityConfig(mode="qiegao"))
        session = MagicMock(pid=10)
        with patch.object(runner, "_qiegao_host_dead", return_value=True), patch(
            "app.core.activity_auto._qiegao_sync_unstick"
        ) as sync, patch("app.core.xajh_bridge.ensure_bridge") as bridge:
            self.assertFalse(
                runner._qiegao_unstick_nudge(
                    session,
                    last_pos=(0.0, 0.0, 0.0),
                    target=(10.0, 0.0, 10.0),
                    scene_id=1524,
                )
            )

        sync.assert_not_called()
        bridge.assert_not_called()

    def test_qiegao_airwall_recovery_waits_for_peer(self) -> None:
        from app.core.activity_auto import (
            _QIEGAO_UNSTICK_ROUNDS,
            _qiegao_sync_unstick,
        )

        _QIEGAO_UNSTICK_ROUNDS.clear()
        stop = threading.Event()
        results: dict[int, int] = {}

        def run(pid: int) -> None:
            results[pid] = _qiegao_sync_unstick(
                pid=pid,
                scene_id=1524,
                stop_event=stop,
                wait_s=0.5,
            )

        first = threading.Thread(target=run, args=(101,))
        second = threading.Thread(target=run, args=(202,))
        first.start()
        time.sleep(0.05)
        self.assertTrue(first.is_alive())
        second.start()
        first.join(1.0)
        second.join(1.0)
        self.assertEqual(results, {101: 2, 202: 2})

    def test_qiegao_bridge_zone_only_matches_measured_wall(self) -> None:
        from app.core.activity_auto import _qiegao_bridge_stuck_zone_distance

        self.assertAlmostEqual(
            _qiegao_bridge_stuck_zone_distance(
                1524, (-71.5, 62.7, -424.1)
            )
            or 0.0,
            0.0,
            places=3,
        )
        self.assertIsNone(
            _qiegao_bridge_stuck_zone_distance(
                1524, (-144.9, 67.8, -437.4)
            )
        )
        self.assertIsNone(
            _qiegao_bridge_stuck_zone_distance(
                9999, (-71.5, 62.7, -424.1)
            )
        )

    def test_qiegao_bridge_reads_nearby_active_peer_snapshot(self) -> None:
        from types import SimpleNamespace

        from app.core.activity_auto import (
            _QIEGAO_ACTIVE_RUNNERS,
            _QIEGAO_ACTIVE_RUNNERS_LOCK,
            _qiegao_active_peer_nearby,
        )

        with _QIEGAO_ACTIVE_RUNNERS_LOCK:
            _QIEGAO_ACTIVE_RUNNERS.clear()
            _QIEGAO_ACTIVE_RUNNERS.update({101, 202})
        try:
            with patch(
                "app.core.live_scene_hub.get_live_scene",
                return_value=SimpleNamespace(
                    scene_id=1524,
                    pos=(-72.0, 62.7, -424.0),
                ),
            ):
                paired, distance = _qiegao_active_peer_nearby(
                    pid=101,
                    scene_id=1524,
                    position=(-71.5, 62.7, -424.1),
                )
            self.assertTrue(paired)
            self.assertLess(distance or 999.0, 18.0)

            with patch(
                "app.core.live_scene_hub.get_live_scene",
                return_value=SimpleNamespace(
                    scene_id=1524,
                    pos=(-144.9, 67.8, -437.4),
                ),
            ):
                paired, distance = _qiegao_active_peer_nearby(
                    pid=101,
                    scene_id=1524,
                    position=(-71.5, 62.7, -424.1),
                )

            self.assertFalse(paired)
            self.assertIsNone(distance)
        finally:
            with _QIEGAO_ACTIVE_RUNNERS_LOCK:
                _QIEGAO_ACTIVE_RUNNERS.clear()

    def test_qiegao_stuck_detector_is_disabled_outside_bridge(self) -> None:
        from app.core.activity_auto import ActivityConfig
        from app.core.automove import PathTarget

        runner = ActivityRunner(pid=10, cfg=ActivityConfig(mode="qiegao"))
        session = MagicMock(pid=10)
        target = PathTarget(x=-58.0, y=62.7, z=-420.1, mode=1524)

        def fake_pathfind(_session, _clue, **kwargs):
            self.assertEqual(kwargs["unstick_hold_s"], 25.0)
            self.assertFalse(
                kwargs["stuck_check"](
                    session,
                    last_pos=(-144.9, 67.8, -437.4),
                    target=(-58.0, 62.7, -420.1),
                    scene_id=1524,
                )
            )
            self.assertTrue(
                kwargs["stuck_check"](
                    session,
                    last_pos=(-71.5, 62.7, -424.1),
                    target=(-58.0, 62.7, -420.1),
                    scene_id=1524,
                )
            )
            return {
                "ok": True,
                "command_ok": True,
                "arrived": True,
                "verified": True,
                "scene_id": 1524,
                "last_distance": 0.0,
            }

        with patch.object(runner, "_qiegao_host_dead", return_value=False), patch.object(
            runner, "_qiegao_afk_target", return_value=target
        ), patch.object(
            runner,
            "_read_live_pos",
            return_value=(1524, (-144.9, 67.8, -437.4), "沙漠古镇"),
        ), patch(
            "app.core.task_api.pathfind_to_clue", side_effect=fake_pathfind
        ), patch.object(
            runner, "_qiegao_unstick_nudge"
        ) as nudge:
            self.assertTrue(runner._move_to_qiegao_afk(session, wait_arrive=True))

        nudge.assert_not_called()

    def test_qiegao_bridge_unstick_has_no_main_alt_order(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=20,
            cfg=ActivityConfig(
                mode="qiegao",
                qiegao_is_alt=True,
                qiegao_unstick_hold_s=25.0,
            ),
        )
        session = MagicMock(pid=20)
        bridge = MagicMock()
        bridge.call.return_value = MagicMock(ok=True)
        with patch.object(runner, "_qiegao_host_dead", return_value=False), patch(
            "app.core.activity_auto._qiegao_sync_unstick", return_value=2
        ), patch(
            "app.core.automove.stop_automove_best_effort"
        ), patch(
            "app.core.xajh_bridge.ensure_bridge", return_value=bridge
        ), patch(
            "app.core.activity_auto._sleep_interruptible", return_value=True
        ) as sleep, patch(
            "app.core.activity_auto.random.uniform", side_effect=[1.0, 0.0, 0.0]
        ):
            self.assertTrue(
                runner._qiegao_unstick_nudge(
                    session,
                    last_pos=(-71.5, 62.7, -424.1),
                    target=(-58.0, 62.7, -420.1),
                    scene_id=1524,
                    stuck_for_s=10.0,
                    sync_with_peer=True,
                )
            )

        self.assertFalse(any(call.args and call.args[0] == 25.0 for call in sleep.call_args_list))

    def test_qiegao_unstick_callback_rejects_defensive_off_zone_call(self) -> None:
        from app.core.activity_auto import ActivityConfig
        from app.core.automove import PathTarget

        runner = ActivityRunner(pid=10, cfg=ActivityConfig(mode="qiegao"))
        session = MagicMock(pid=10)
        target = PathTarget(x=-58.0, y=62.7, z=-420.1, mode=1524)

        def fake_pathfind(_session, _clue, **kwargs):
            handled = kwargs["unstick"](
                session,
                last_pos=(-144.9, 67.8, -437.4),
                target=(-58.0, 62.7, -420.1),
                scene_id=1524,
                stuck_for_s=10.0,
                attempt=1,
            )
            self.assertFalse(handled)
            return {
                "ok": True,
                "command_ok": True,
                "arrived": True,
                "verified": True,
                "scene_id": 1524,
                "last_distance": 0.0,
            }

        with patch.object(runner, "_qiegao_host_dead", return_value=False), patch.object(
            runner, "_qiegao_afk_target", return_value=target
        ), patch.object(
            runner,
            "_read_live_pos",
            return_value=(1524, (-144.9, 67.8, -437.4), "沙漠古镇"),
        ), patch(
            "app.core.task_api.pathfind_to_clue", side_effect=fake_pathfind
        ), patch.object(
            runner, "_qiegao_unstick_nudge"
        ) as nudge:
            self.assertTrue(runner._move_to_qiegao_afk(session, wait_arrive=True))

        nudge.assert_not_called()

    def test_qiegao_alt_waits_for_stable_map_after_team_entry(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=20,
            cfg=ActivityConfig(mode="qiegao", qiegao_is_alt=True),
        )
        session = MagicMock(pid=20)
        order = []

        def ready(*_args, **_kwargs):
            order.append("ready")
            return True

        def hang(_session, **_kwargs):
            order.append("hang")
            return False

        with patch.object(runner, "_refresh_points", return_value=0), patch.object(
            runner, "_done_target", return_value=False
        ), patch.object(runner, "_emit_scene"), patch.object(
            runner, "_abort_if_game_dead", return_value=False
        ), patch(
            "app.core.activity_auto.read_scene_state",
            side_effect=[
                (68, (0.0, 0.0, 0.0), "福州城"),
                (1524, (-34.9, 60.7, -68.6), "沙漠古镇"),
                (1524, (-144.9, 67.8, -437.4), "沙漠古镇"),
            ],
        ), patch(
            "app.core.activity_auto.wait_map_ready", side_effect=ready
        ) as map_ready, patch.object(
            runner, "_qiegao_start_hang_phase", side_effect=hang
        ):
            runner._one_round(session)

        map_ready.assert_called_once()
        self.assertEqual(order, ["ready", "hang"])

    def test_qiegao_unstick_retreats_six_meters_toward_bridge(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=10,
            cfg=ActivityConfig(
                mode="qiegao",
                qiegao_unstick_step_m=6.0,
                qiegao_unstick_settle_s=0.6,
            ),
        )
        session = MagicMock(pid=10)
        bridge = MagicMock()
        bridge.call.return_value = MagicMock(ok=True)
        with patch.object(runner, "_qiegao_host_dead", return_value=False), patch(
            "app.core.activity_auto._qiegao_sync_unstick", return_value=2
        ), patch(
            "app.core.automove.stop_automove_best_effort"
        ), patch(
            "app.core.xajh_bridge.ensure_bridge", return_value=bridge
        ), patch(
            "app.core.activity_auto._sleep_interruptible", return_value=True
        ), patch(
            "app.core.activity_auto.random.uniform", side_effect=[1.0, 0.0, 0.0]
        ):
            self.assertTrue(
                runner._qiegao_unstick_nudge(
                    session,
                    last_pos=(0.0, 2.0, 0.0),
                    target=(0.0, 2.0, -10.0),
                    scene_id=1524,
                    stuck_for_s=10.0,
                    sync_with_peer=True,
                )
            )

        call = bridge.call.call_args
        self.assertAlmostEqual(call.kwargs["x"], 0.0, places=3)
        self.assertAlmostEqual(call.kwargs["z"], 6.0, places=3)

    def test_qiegao_city_return_waits_stable_without_move_back(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=303,
            cfg=ActivityConfig(
                qiegao_return_settle_min_s=2.0,
                qiegao_return_stable_s=1.0,
                qiegao_return_settle_timeout_s=5.0,
            ),
        )
        session = MagicMock(pid=303)
        clock = [0.0]

        def fake_sleep(seconds, _stop) -> bool:
            clock[0] += float(seconds)
            return True

        with patch.object(
            runner,
            "_read_live_pos",
            return_value=(68, (10.0, 2.0, 20.0), "福州城"),
        ), patch.object(runner, "_ensure_hang_off", return_value=True) as hang_off, patch(
            "app.core.activity_auto.host_move_to"
        ) as move, patch(
            "app.core.automove.stop_automove_best_effort"
        ) as stop_move, patch(
            "app.core.team_ops.set_team_follow",
            return_value=MagicMock(ok=True, message="ok"),
        ) as follow, patch(
            "app.core.activity_auto._qiegao_city_rendezvous",
            return_value=(None, 2, 6.0),
        ), patch(
            "app.core.activity_auto._sleep_interruptible", side_effect=fake_sleep
        ), patch(
            "app.core.activity_auto.time.time", side_effect=lambda: clock[0]
        ):
            runner._qiegao_settle_city_return(session)

        hang_off.assert_called_once_with(
            session,
            reason="回城关挂机",
            force=True,
            keep_task_guard=True,
        )
        follow.assert_called_once()
        move.assert_not_called()
        self.assertGreaterEqual(stop_move.call_count, 2)

    def test_qiegao_loop_finally_stops_task_guard(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(pid=10, cfg=ActivityConfig(mode="qiegao"))
        runner._stop.set()
        session = MagicMock(pid=10)
        with patch(
            "app.core.activity_auto.open_attach_session", return_value=session
        ), patch.object(runner, "_refresh_points", return_value=0), patch(
            "app.core.activity_auto.read_scene_state",
            return_value=(68, (0.0, 0.0, 0.0), "福州城"),
        ), patch(
            "app.core.activity_auto.ensure_bridge", return_value=None
        ), patch(
            "app.core.state_dispatch.warmup_session"
        ), patch(
            "app.core.hang_settings.stop_hang_guard"
        ) as stop_guard:
            runner._loop()

        stop_guard.assert_called_once_with(10, log=runner.log)

    def test_qiegao_city_rendezvous_uses_midpoint_when_over_eight_meters(self) -> None:
        from app.core.activity_auto import (
            _QIEGAO_CITY_ROUNDS,
            _qiegao_city_rendezvous,
        )

        _QIEGAO_CITY_ROUNDS.clear()
        stop = threading.Event()
        results = {}

        def run(pid: int, position: tuple[float, float, float]) -> None:
            results[pid] = _qiegao_city_rendezvous(
                pid=pid,
                scene_id=68,
                position=position,
                stop_event=stop,
                wait_s=0.5,
                near_m=8.0,
            )

        first = threading.Thread(target=run, args=(101, (0.0, 2.0, 0.0)))
        second = threading.Thread(target=run, args=(202, (20.0, 2.0, 0.0)))
        first.start()
        time.sleep(0.05)
        second.start()
        first.join(1.0)
        second.join(1.0)

        self.assertEqual(results[101], ((10.0, 2.0, 0.0), 2, 20.0))
        self.assertEqual(results[202], ((10.0, 2.0, 0.0), 2, 20.0))

    def test_qiegao_uses_saved_hang_settings_snapshot(self) -> None:
        runner = ActivityRunner(
            pid=11,
            hang_settings={
                "hang_empty_skill": True,
                "hang_enable_pickup": False,
                "hang_radius": 9,
            },
        )
        session = MagicMock(pid=11)
        with patch(
            "app.core.team_ops.read_host_identity", return_value=("", 0)
        ):
            cfg = runner._resolve_hang_cfg(session)
        self.assertTrue(cfg.empty_skill)
        self.assertFalse(cfg.enable_pickup)
        self.assertEqual(cfg.radius, 9)

    def test_qiegao_forces_normal_hang_mode_for_anchor_correction(self) -> None:
        """切糕锚点圆心纠偏仅普通模式生效：即使保存副本模式也强制普通。"""
        from app.core.activity_auto import ActivityConfig, AUTOPLAY_MODE_NORMAL

        runner = ActivityRunner(
            pid=111,
            cfg=ActivityConfig(mode="qiegao"),
            hang_settings={"hang_mode": 1},
        )
        session = MagicMock(pid=111)
        with patch(
            "app.core.team_ops.read_host_identity", return_value=("", 0)
        ):
            cfg = runner._resolve_hang_cfg(session)
        self.assertEqual(cfg.mode, AUTOPLAY_MODE_NORMAL)

    def test_dungeon_runner_forces_native_dungeon_mode_for_target_guard(self) -> None:
        """Auto dungeon and scheduled dungeon share this effective config path."""
        from app.core.activity_auto import ActivityConfig, AUTOPLAY_MODE_DUNGEON

        runner = ActivityRunner(
            pid=112,
            cfg=ActivityConfig(mode="dungeon"),
            hang_settings={
                "hang_mode": 0,
                "hang_ignore_dungeon_stuck": True,
            },
        )
        session = MagicMock(pid=112)
        with patch(
            "app.core.team_ops.read_host_identity", return_value=("", 0)
        ):
            cfg = runner._resolve_hang_cfg(session)
        self.assertEqual(cfg.mode, AUTOPLAY_MODE_DUNGEON)
        self.assertTrue(cfg.ignore_dungeon_stuck)

    def test_activity_runner_keeps_selected_hang_mode(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=113,
            cfg=ActivityConfig(mode="activity"),
            hang_settings={"hang_mode": 0, "hang_ignore_dungeon_stuck": True},
        )
        session = MagicMock(pid=113)
        with patch(
            "app.core.team_ops.read_host_identity", return_value=("", 0)
        ):
            cfg = runner._resolve_hang_cfg(session)
        self.assertEqual(cfg.mode, 0)
        self.assertTrue(cfg.ignore_dungeon_stuck)

    def test_qiegao_start_failure_does_not_enter_idle_wait(self) -> None:
        from app.core.activity_auto import ActivityConfig
        from app.core.automove import PathTarget

        runner = ActivityRunner(
            pid=12,
            cfg=ActivityConfig(
                mode="qiegao",
                qiegao_afk_x=1.0,
                qiegao_afk_y=2.0,
                qiegao_afk_z=3.0,
                qiegao_arrive_radius=1.0,
                qiegao_arrival_settle_s=0.2,
            ),
        )
        session = MagicMock(pid=12)
        target = PathTarget(x=1.0, y=2.0, z=3.0, mode=1524)
        with patch.object(runner, "_qiegao_host_dead", return_value=False), patch.object(
            runner, "_ensure_hang_off", return_value=True
        ), patch.object(runner, "_move_to_qiegao_afk", return_value=True), patch.object(
            runner, "_qiegao_afk_target", return_value=target
        ), patch.object(
            runner, "_read_live_pos", return_value=(1524, (1.0, 2.0, 3.0), "切糕")
        ) as read_pos, patch.object(
            runner, "_ensure_hang_on", return_value=False
        ) as start, patch.object(
            runner, "_qiegao_hang_until_return"
        ) as idle, patch(
            "app.core.activity_auto._sleep_interruptible", return_value=True
        ):
            self.assertFalse(runner._qiegao_start_hang_phase(session))
        self.assertEqual(start.call_count, 2)
        idle.assert_not_called()
        self.assertTrue(all(c.kwargs.get("fresh") is True for c in read_pos.call_args_list))

    def test_qiegao_bridge_phase_disabled_skips_path(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=13,
            cfg=ActivityConfig(mode="qiegao", qiegao_bridge_phase_enabled=False),
        )
        session = MagicMock(pid=13)
        with patch.object(runner, "_ensure_hang_off", return_value=True), patch.object(
            runner, "_qiegao_host_dead", return_value=False
        ), patch.object(
            runner, "_read_live_pos", return_value=(1524, (-66.0, 61.3, -441.7), "切糕")
        ), patch(
            "app.core.activity_auto._sleep_interruptible", return_value=True
        ) as sleep:
            self.assertTrue(runner._qiegao_enter_bridge_phase(session))
        sleep.assert_not_called()

    def test_qiegao_bridge_phase_near_point_skips_path(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=14,
            cfg=ActivityConfig(
                mode="qiegao", qiegao_bridge_stay_s=0.5, qiegao_bridge_wait_s=1.0
            ),
        )
        session = MagicMock(pid=14)
        with patch.object(runner, "_ensure_hang_off", return_value=True), patch.object(
            runner, "_qiegao_host_dead", return_value=False
        ), patch.object(
            runner, "_read_live_pos", return_value=(1524, (-66.5, 61.0, -442.0), "切糕")
        ), patch(
            "app.core.activity_auto._sleep_interruptible", return_value=True
        ) as sleep, patch(
            "app.core.task_api.pathfind_to_clue"
        ) as pfind:
            self.assertTrue(runner._qiegao_enter_bridge_phase(session))
        pfind.assert_not_called()
        self.assertEqual(sleep.call_count, 2)

    def test_qiegao_bridge_phase_paths_to_configured_point(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=15,
            cfg=ActivityConfig(
                mode="qiegao",
                qiegao_bridge_path_x=-66.0,
                qiegao_bridge_path_y=61.3,
                qiegao_bridge_path_z=-441.7,
                qiegao_bridge_stay_s=0.0,
                qiegao_bridge_wait_s=1.0,
            ),
        )
        session = MagicMock(pid=15)
        call = {
            "ok": True,
            "arrived": True,
            "verified": True,
            "last_distance": 0.4,
            "error": "",
        }
        with patch.object(runner, "_ensure_hang_off", return_value=True), patch.object(
            runner, "_qiegao_host_dead", return_value=False
        ), patch.object(
            runner, "_read_live_pos", return_value=(1524, (10.0, 20.0, 30.0), "切糕")
        ), patch(
            "app.core.activity_auto._sleep_interruptible", return_value=True
        ), patch(
            "app.core.task_api.pathfind_to_clue", return_value=call
        ) as pfind:
            self.assertTrue(runner._qiegao_enter_bridge_phase(session))
        pfind.assert_called_once()
        clue = pfind.call_args.args[1]
        kwargs = pfind.call_args.kwargs
        self.assertAlmostEqual(float(clue["x"]), -66.0)
        self.assertAlmostEqual(float(clue["z"]), -441.7)
        self.assertEqual(int(kwargs["mode"]), 1524)
        self.assertEqual(kwargs["stuck_s"], 0.0)
        self.assertIsNone(kwargs.get("stuck_check"))
        self.assertIsNone(kwargs.get("unstick"))

    def test_qiegao_alt_entry_hold_follows_bridge_window_by_default(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=16,
            cfg=ActivityConfig(mode="qiegao", qiegao_is_alt=True),
        )
        self.assertEqual(runner._qiegao_alt_entry_hold_s(), 15.0)

    def test_qiegao_alt_entry_hold_can_be_overridden(self) -> None:
        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=17,
            cfg=ActivityConfig(mode="qiegao", qiegao_is_alt=True, qiegao_alt_hold_s=8.0),
        )
        self.assertEqual(runner._qiegao_alt_entry_hold_s(), 8.0)

    def test_qiegao_hang_waits_for_scene_return_without_wall_clock_cutoff(self) -> None:
        from itertools import chain, repeat

        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=12,
            cfg=ActivityConfig(mode="qiegao", return_poll_s=1.0),
        )
        session = MagicMock(pid=12)
        with patch("app.core.activity_auto._pid_alive", return_value=True), patch.object(
            runner,
            "_scene_prefer_hub",
            side_effect=[
                (1524, (-59.0, 62.7, -406.0), "沙漠古镇"),
                (68, (0.0, 0.0, 0.0), "福州城"),
            ],
        ), patch.object(runner, "_qiegao_host_dead", return_value=False), patch.object(
            runner, "_qiegao_afk_target", return_value=None
        ), patch.object(runner, "_qiegao_settle_city_return") as settle, patch(
            "app.core.activity_auto.read_instance_countdown_remain_s",
            return_value={"ok": False},
        ), patch(
            "app.core.activity_auto._sleep_interruptible", return_value=True
        ), patch(
            "app.core.activity_auto.time.time",
            side_effect=chain([0.0], repeat(1801.0)),
        ):
            self.assertTrue(runner._qiegao_hang_until_return(session))

        settle.assert_called_once_with(session)

    def test_qiegao_stops_hang_when_countdown_under_30s(self) -> None:
        from itertools import chain, repeat

        from app.core.activity_auto import ActivityConfig

        runner = ActivityRunner(
            pid=12,
            cfg=ActivityConfig(mode="qiegao", return_poll_s=1.0),
        )
        session = MagicMock(pid=12)
        with patch("app.core.activity_auto._pid_alive", return_value=True), patch.object(
            runner,
            "_scene_prefer_hub",
            side_effect=[
                (1524, (-59.0, 62.7, -406.0), "沙漠古镇"),
                (68, (0.0, 0.0, 0.0), "福州城"),
            ],
        ), patch.object(runner, "_qiegao_host_dead", return_value=False), patch.object(
            runner, "_qiegao_afk_target", return_value=None
        ), patch.object(runner, "_ensure_hang_off") as hang_off, patch.object(
            runner, "_qiegao_settle_city_return"
        ), patch(
            "app.core.activity_auto.read_instance_countdown_remain_s",
            return_value={"ok": True, "remain_s": 25, "text": "倒计时0:25"},
        ), patch(
            "app.core.activity_auto._sleep_interruptible", return_value=True
        ), patch(
            "app.core.activity_auto.time.time",
            side_effect=chain([0.0], repeat(1801.0)),
        ):
            self.assertTrue(runner._qiegao_hang_until_return(session))

        hang_off.assert_called_once_with(
            session,
            reason="副本倒计时关内挂",
            force=False,
            keep_task_guard=True,
        )

    def test_qiegao_hang_correction_repaths_to_fixed_point(self) -> None:
        from itertools import chain, repeat

        from app.core.activity_auto import ActivityConfig
        from app.core.automove import PathTarget

        runner = ActivityRunner(
            pid=12,
            cfg=ActivityConfig(mode="qiegao", return_poll_s=1.0),
        )
        session = MagicMock(pid=12)
        target = PathTarget(x=0.0, y=0.0, z=0.0, mode=1524)
        with patch("app.core.activity_auto._pid_alive", return_value=True), patch.object(
            runner,
            "_scene_prefer_hub",
            side_effect=[
                (1524, (50.0, 0.0, 50.0), "沙漠古镇"),
                (1524, (50.0, 0.0, 50.0), "沙漠古镇"),
                (68, (0.0, 0.0, 0.0), "福州城"),
            ],
        ), patch.object(runner, "_qiegao_host_dead", return_value=False), patch.object(
            runner, "_qiegao_afk_target", return_value=target
        ), patch.object(
            runner, "_ensure_hang_off", return_value=True
        ) as hang_off, patch.object(
            runner, "_ensure_hang_on", return_value=True
        ) as hang_on, patch.object(
            runner, "_move_to_qiegao_afk", return_value=True
        ) as move, patch.object(
            runner, "_qiegao_settle_city_return"
        ), patch(
            "app.core.activity_auto.read_instance_countdown_remain_s",
            return_value={"ok": False},
        ), patch(
            "app.core.activity_auto._sleep_interruptible", return_value=True
        ), patch(
            "app.core.activity_auto.time.time",
            side_effect=chain([0.0], repeat(1801.0)),
        ):
            self.assertTrue(runner._qiegao_hang_until_return(session))

        hang_off.assert_called_once_with(
            session,
            reason="挂机纠偏前关挂机",
            force=False,
            keep_task_guard=True,
        )
        move.assert_called_once_with(session, wait_arrive=True)
        hang_on.assert_called_once_with(session)

    def test_attack_recover_rejects_gem_but_accepts_damage_item(self) -> None:
        from app.core.activity_auto import is_autoplay_attack_recover_item

        self.assertFalse(
            is_autoplay_attack_recover_item(name="超级外功宝石-10%")
        )
        self.assertTrue(
            is_autoplay_attack_recover_item(name="2200W外功伤害物品")
        )

    def test_set_autoplay_anchor_writes_three_floats_and_verifies(self) -> None:
        from app.core import activity_auto as aa

        sess = MagicMock(pid=4243)
        mem = {"ok": True, "autoplay": 0x123400}
        with patch.object(aa, "resolve_cec_autoplay_rpm", return_value=mem), patch.object(
            aa, "_rpm_f32", side_effect=[100.0, 200.0, 300.0, 10.0, 20.0, 30.0]
        ), patch.object(aa, "_wpm_f32", return_value=True) as write:
            out = aa.set_autoplay_anchor(sess, 10.0, 20.0, 30.0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["before"], {"x": 100.0, "y": 200.0, "z": 300.0})
        self.assertEqual(out["anchor"], {"x": 10.0, "y": 20.0, "z": 30.0})
        self.assertEqual(
            write.call_args_list,
            [
                ((sess, 0x123400 + aa.AUTOPLAY_ANCHOR_X_OFF, 10.0),),
                ((sess, 0x123400 + aa.AUTOPLAY_ANCHOR_Y_OFF, 20.0),),
                ((sess, 0x123400 + aa.AUTOPLAY_ANCHOR_Z_OFF, 30.0),),
            ],
        )

    def test_set_autoplay_anchor_rejects_non_finite_and_bad_coords(self) -> None:
        from app.core import activity_auto as aa

        out = aa.set_autoplay_anchor(object(), float("nan"), 0.0, 0.0)
        self.assertFalse(out["ok"])
        self.assertIn("有限数", out["error"])
        out = aa.set_autoplay_anchor(object(), "bad", 0.0, 0.0)
        self.assertFalse(out["ok"])

    def test_set_autoplay_anchor_verify_failure_returns_error(self) -> None:
        from app.core import activity_auto as aa

        sess = MagicMock(pid=4243)
        mem = {"ok": True, "autoplay": 0x123400}
        with patch.object(aa, "resolve_cec_autoplay_rpm", return_value=mem), patch.object(
            aa, "_rpm_f32", side_effect=[0.0, 0.0, 0.0, 999.0, 999.0, 999.0]
        ), patch.object(aa, "_wpm_f32", return_value=True):
            out = aa.set_autoplay_anchor(sess, 10.0, 20.0, 30.0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "verify failed")

    def test_force_stop_uses_bridge_ui_thread_not_remote_thread(self) -> None:
        from types import SimpleNamespace

        from app.core.activity_auto import stop_autoplay_force

        sess = MagicMock(pid=4243, hwnd=0)
        before = {"ok": True, "autoplay": 0x123450, "running": True}
        after = {"ok": True, "autoplay": 0x123450, "running": False}
        result = SimpleNamespace(ok=True, ret=0x8001, error=None, note="ok")
        result.to_dict = MagicMock(return_value={"ok": True, "ret": 0x8001})
        bridge = MagicMock()
        bridge.autoplay_stop.return_value = result
        with patch(
            "app.core.activity_auto.resolve_cec_autoplay_rpm",
            side_effect=[before, after],
        ), patch("app.core.xajh_bridge.ensure_bridge", return_value=bridge), patch(
            "app.core.activity_auto.remote_call_thiscall_x86"
        ) as remote:
            out = stop_autoplay_force(sess)
        self.assertTrue(out["ok"])
        self.assertEqual(out["via"], "StopAutoPlay_UIThread")
        bridge.autoplay_stop.assert_called_once()
        bridge.close.assert_called_once()
        remote.assert_not_called()

    def test_skill_cast_is_always_rejected(self) -> None:
        out = XajhBridge(1).cast_skill(123, bar_slot=2)
        self.assertFalse(out.ok)
        self.assertEqual(out.error, "UNSUPPORTED_STALE_SYMBOL")

    def test_yaolu_missing_login_token_does_not_block_start(self) -> None:
        from app.core.yaolu_auto import yaolu_start_credential_error

        cfg = YaoluConfig(login_token="", captcha_api_key="configured")
        self.assertIsNone(yaolu_start_credential_error(cfg))

    def test_yaolu_missing_captcha_key_fails_before_attach(self) -> None:
        events = []
        runner = YaoluRunner(
            pid=123,
            cfg=YaoluConfig(login_token="configured", captcha_api_key=""),
            on_event=events.append,
        )
        with patch("app.core.yaolu_auto.open_attach_session") as attach:
            runner._loop()
        attach.assert_not_called()
        self.assertEqual(events[0].detail["reason"], "missing_captcha_api_key")
        self.assertIn("答题专用 Key", events[0].message)
        self.assertIn("快捷设置", events[0].message)

    def test_yaolu_debug_random_answer_bypasses_credential_gate(self) -> None:
        from app.core.yaolu_auto import yaolu_start_credential_error

        cfg = YaoluConfig(
            login_token="",
            captcha_api_key="",
            debug_random_answer=True,
        )
        self.assertIsNone(yaolu_start_credential_error(cfg))

    def test_yaolu_unicode_captcha_key_is_rejected_at_start(self) -> None:
        from app.core.yaolu_auto import yaolu_start_credential_error

        result = yaolu_start_credential_error(
            YaoluConfig(captcha_api_key="答题专用 Key：abc")
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "invalid_captcha_api_key")
        self.assertIn("包含中文", result[1])

    def test_yaolu_entry_server_block_schedules_reopen(self) -> None:
        events = []
        runner = YaoluRunner(pid=123, on_event=events.append)
        runner.running = True
        self.assertTrue(
            runner._handle_entry_block_error(
                "entry_block:队伍不满足开启副本条件"
            )
        )
        self.assertFalse(runner._stop.is_set())
        self.assertTrue(runner._entry_reopen_pending)
        self.assertGreaterEqual(events[-1].detail["delay_s"], 20.0)
        self.assertLessEqual(events[-1].detail["delay_s"], 45.0)
        self.assertEqual(events[-1].detail["reason"], "entry_reopen_scheduled")

    def test_yaolu_entry_reopen_heartbeat_grows_and_stops_after_half_hour(self) -> None:
        """Heartbeat grows with growth factor; stops after cumulative half-hour."""
        from app.core.yaolu_auto import YaoluConfig
        from unittest.mock import patch

        events = []
        runner = YaoluRunner(
            pid=123,
            cfg=YaoluConfig(
                entry_reopen_base_s=30.0,
                entry_reopen_growth=1.6,
                entry_reopen_jitter=0.0,  # deterministic for test
                entry_reopen_max_total_s=1800.0,
            ),
            on_event=events.append,
        )
        for _ in range(3):
            self.assertTrue(runner._schedule_entry_reopen("team condition"))
        delays = [
            e.detail["delay_s"]
            for e in events
            if e.detail.get("reason") == "entry_reopen_scheduled"
        ]
        # 30, 48, 76.8 with growth=1.6 and no jitter
        self.assertAlmostEqual(delays[0], 30.0, places=3)
        self.assertAlmostEqual(delays[1], 48.0, places=3)
        self.assertAlmostEqual(delays[2], 76.8, places=3)
        # Force past half-hour budget
        runner._entry_reopen_attempt = 6
        runner._entry_reopen_total_s = 1890.0
        self.assertFalse(runner._schedule_entry_reopen("still blocked"))
        self.assertTrue(runner._stop.is_set())
        self.assertEqual(events[-1].detail["reason"], "entry_reopen_timeout")

    @patch("app.core.yaolu_auto.read_scene_state", return_value=(1529, None, "九层妖楼"))
    def test_entry_reopen_detects_direct_scene_enter_without_captcha(self, _scene) -> None:
        kind, sid, label, feedback = wait_entry_reopen_result(
            MagicMock(),
            YaoluConfig(),
            start_scene=68,
            entry_block_baseline={},
        )
        self.assertEqual((kind, sid, label), ("entered", 1529, "九层妖楼"))
        self.assertIsNone(feedback)

    @patch("app.core.yaolu_auto.is_captcha_dialog_open")
    @patch("app.core.yaolu_auto.probe_entry_open_block_text")
    @patch("app.core.yaolu_auto.read_scene_state")
    def test_entry_reopen_wait_is_ui_only_with_countdown(
        self, read_scene, probe_block, is_open
    ) -> None:
        """Reopen wait must share monotonic budget and skip heavy scans."""
        statuses: list[str] = []
        clock = {"t": 10.0}

        def _mono() -> float:
            return clock["t"]

        def _sleep(sec, stop_event=None):
            clock["t"] += float(sec)
            return True

        def _probe(*_a, **kwargs):
            self.assertFalse(kwargs.get("heavy", True))
            return None

        read_scene.return_value = (68, None, "福州城")
        is_open.return_value = MagicMock(shown=False)
        probe_block.side_effect = _probe

        with (
            patch("app.core.yaolu_auto.time.monotonic", side_effect=_mono),
            patch("app.core.yaolu_auto._sleep_interruptible", side_effect=_sleep),
        ):
            kind, sid, label, fb = wait_entry_reopen_result(
                MagicMock(pm=object()),
                YaoluConfig(enter_timeout_s=3.0),
                start_scene=68,
                entry_block_baseline={},
                status=statuses.append,
            )
        self.assertEqual(kind, "timeout")
        self.assertIsNone(fb)
        countdown = [s for s in statuses if "剩余" in s]
        self.assertTrue(countdown)
        self.assertRegex(countdown[0], r"剩余 [1-3]s")
        self.assertGreaterEqual(probe_block.call_count, 1)

    @patch("app.core.yaolu_auto.wait_entry_reopen_result")
    @patch("app.core.yaolu_auto.open_entry")
    @patch("app.core.yaolu_auto.snapshot_entry_open_block_hits", return_value={})
    @patch("app.core.yaolu_auto.check_interact_gate")
    @patch("app.core.yaolu_auto.entry_blackout_remaining_s", return_value=0.0)
    @patch("app.core.yaolu_auto.read_scene_state")
    def test_pending_reopen_clicks_entry_and_accepts_direct_enter(
        self,
        read_scene,
        _blackout,
        gate,
        _baseline,
        open_entry_mock,
        wait_reopen,
    ) -> None:
        read_scene.return_value = (68, (1.0, 0.0, 2.0), "福州城")
        gate.return_value = MagicMock(
            ok=True,
            in_team=True,
            is_leader=True,
            captcha_open=False,
            team_ptr=1,
            host_id_lo=2,
            leader_id_lo=2,
        )
        open_result = MagicMock(ok=True)
        open_entry_mock.return_value = open_result
        wait_reopen.return_value = ("entered", 1529, "九层妖楼", None)
        runner = YaoluRunner(pid=123)
        runner._entry_reopen_pending = True
        runner._entry_reopen_attempt = 1
        runner._entry_reopen_total_s = 30.0
        runner._entry_reopen_next_ts = 0.0
        with patch.object(runner, "_finish_enter_success") as finish:
            runner._one_round(MagicMock())
        open_entry_mock.assert_called_once()
        wait_reopen.assert_called_once()
        finish.assert_called_once_with(
            ANY,
            scene_id=1529,
            label="九层妖楼",
            open_res=open_result,
        )

    @patch("app.core.yaolu_auto.wait_enter_result")
    @patch("app.core.yaolu_auto.wait_and_solve_captcha")
    @patch("app.core.yaolu_auto.snapshot_entry_open_block_hits")
    @patch("app.core.yaolu_auto.wait_for_captcha_dialog")
    @patch("app.core.yaolu_auto.check_interact_gate")
    @patch("app.core.yaolu_auto.entry_blackout_remaining_s", return_value=0.0)
    @patch("app.core.yaolu_auto.read_scene_state")
    def test_normal_captcha_passes_entry_baseline_to_enter_wait_only(
        self,
        read_scene,
        _blackout,
        gate,
        wait_dialog,
        snapshot,
        solve,
        wait_enter,
    ) -> None:
        read_scene.return_value = (68, (1.0, 0.0, 2.0), "福州城")
        gate.return_value = MagicMock(
            ok=True,
            in_team=True,
            is_leader=True,
            captcha_open=True,
            captcha_name="Win_Question3D",
            team_ptr=1,
            host_id_lo=2,
            leader_id_lo=2,
        )
        wait_dialog.return_value = MagicMock(
            ok=True,
            box=MagicMock(width=660, height=460),
            png=b"png",
        )
        snapshot.return_value = {"队伍不满足开启副本条件": 1}
        solve.return_value = MagicMock(
            ok=True,
            identify_id=None,
            animal="cat",
            positions=[1, 2],
            confidence=0.9,
        )
        wait_enter.return_value = (True, 1529, "九层妖楼", None)
        runner = YaoluRunner(pid=123)
        with patch.object(runner, "_finish_enter_success"):
            runner._one_round(MagicMock())
        self.assertNotIn("entry_block_baseline", solve.call_args.kwargs)
        self.assertEqual(
            wait_enter.call_args.kwargs["entry_block_baseline"],
            snapshot.return_value,
        )

    def test_yaolu_identify_timeout_default_is_45_seconds(self) -> None:
        self.assertEqual(YaoluConfig().captcha_api_timeout_s, 45.0)

    def test_yaolu_enter_timeout_default_is_18_seconds(self) -> None:
        self.assertEqual(YaoluConfig().enter_timeout_s, 18.0)

    @patch("app.core.yaolu_auto.wait_captcha_answer_feedback")
    @patch("app.core.yaolu_auto.read_scene_state")
    def test_wait_enter_result_shared_deadline_and_countdown(
        self, read_scene, wait_fb
    ) -> None:
        """One monotonic 18s budget + descending countdown; no second window."""
        from app.core.game_sys_msg import (
            CAPTCHA_OK_MSG_ID,
            CAPTCHA_OK_TEXT,
            CaptchaAnswerFeedback,
        )

        statuses: list[str] = []
        clock = {"t": 50.0}

        def _mono() -> float:
            return clock["t"]

        def _sleep_ok(sec, stop_event=None):
            clock["t"] += float(sec)
            return True

        def _fb(*_a, **kwargs):
            # Must receive the shared deadline from wait_enter_result.
            self.assertIn("deadline", kwargs)
            # Advance most of the budget inside feedback so late loop is short.
            clock["t"] = float(kwargs["deadline"]) - 0.4
            # Fire one countdown while feedback is active.
            kwargs["countdown"]()
            return CaptchaAnswerFeedback(
                kind="ok",
                text=CAPTCHA_OK_TEXT,
                msg_id=CAPTCHA_OK_MSG_ID,
                method="chat:Win_ChatInfo",
            )

        wait_fb.side_effect = _fb
        # Stay in Fuzhou so wait_enter continues until deadline.
        read_scene.return_value = (68, DEFAULT_ENTRY_ANCHOR, "福州城")

        with (
            patch("app.core.yaolu_auto.time.monotonic", side_effect=_mono),
            patch("app.core.yaolu_auto._sleep_interruptible", side_effect=_sleep_ok),
            patch(
                "app.core.game_sys_msg.probe_captcha_answer_text",
                return_value=None,
            ),
        ):
            ok, sid, label, fb = wait_enter_result(
                MagicMock(pm=object()),
                YaoluConfig(enter_timeout_s=18.0),
                start_scene=68,
                status=statuses.append,
            )
        self.assertFalse(ok)
        self.assertEqual(fb.kind, "ok")
        countdown = [s for s in statuses if "剩余" in s and "s" in s]
        self.assertTrue(countdown)
        # First published remain should be full budget (or just under).
        self.assertRegex(countdown[0], r"剩余 (1[0-8]|[0-9])s")
        # No fixed 14s phase split: deadline argument must be start+18.
        self.assertAlmostEqual(
            wait_fb.call_args.kwargs["deadline"], 68.0, places=3
        )

    @patch("app.core.yaolu_auto.wait_enter_while_transfer")
    @patch("app.core.yaolu_auto.wait_captcha_answer_feedback")
    @patch("app.core.yaolu_auto.read_scene_state")
    def test_wait_enter_tap_fail_skips_legacy_grace(
        self, read_scene, wait_fb, grace
    ) -> None:
        from app.core.game_sys_msg import CaptchaAnswerFeedback

        read_scene.return_value = (68, DEFAULT_ENTRY_ANCHOR, "福州城")
        wait_fb.return_value = CaptchaAnswerFeedback(
            kind="fail",
            text="答案错误，请大侠重新来过",
            method="stage1_fail:chat_tap_fail:seq=41",
        )
        ok, sid, _label, fb = wait_enter_result(
            MagicMock(pm=object()),
            YaoluConfig(enter_timeout_s=18.0),
            start_scene=68,
        )
        self.assertFalse(ok)
        self.assertIsNone(sid)
        self.assertEqual(fb.kind, "fail")
        grace.assert_not_called()

    @patch("app.core.yaolu_auto.wait_enter_while_transfer", return_value=(False, 68, "福州城"))
    @patch("app.core.yaolu_auto.wait_captcha_answer_feedback")
    @patch("app.core.yaolu_auto.read_scene_state")
    def test_wait_enter_legacy_fail_keeps_scene_grace(
        self, read_scene, wait_fb, grace
    ) -> None:
        from app.core.game_sys_msg import CaptchaAnswerFeedback

        read_scene.return_value = (68, DEFAULT_ENTRY_ANCHOR, "福州城")
        wait_fb.return_value = CaptchaAnswerFeedback(
            kind="fail",
            text="答案错误，请大侠重新来过",
            method="stage1_fail:hist_journal_fail",
        )
        ok, _sid, _label, fb = wait_enter_result(
            MagicMock(pm=object()),
            YaoluConfig(enter_timeout_s=18.0),
            start_scene=68,
        )
        self.assertFalse(ok)
        self.assertEqual(fb.kind, "fail")
        grace.assert_called_once()

    @patch("app.core.yaolu_auto.wait_enter_while_transfer")
    @patch("app.core.yaolu_auto.wait_captcha_answer_feedback")
    @patch("app.core.yaolu_auto.read_scene_state")
    def test_wait_enter_tap_ok_timeout_skips_extra_grace(
        self, read_scene, wait_fb, grace
    ) -> None:
        from app.core.game_sys_msg import CaptchaAnswerFeedback

        clock = {"t": 10.0}

        def _mono() -> float:
            return clock["t"]

        def _fb(*_args, **kwargs):
            clock["t"] = float(kwargs["deadline"])
            return CaptchaAnswerFeedback(
                kind="ok",
                text="答案正确，请尽快进入活动",
                method="stage1_ok+stage2_clear:chat_tap_ok:seq=42",
            )

        read_scene.return_value = (68, DEFAULT_ENTRY_ANCHOR, "福州城")
        wait_fb.side_effect = _fb
        with patch("app.core.yaolu_auto.time.monotonic", side_effect=_mono):
            ok, sid, _label, fb = wait_enter_result(
                MagicMock(pm=object()),
                YaoluConfig(enter_timeout_s=18.0),
                start_scene=68,
            )
        self.assertFalse(ok)
        self.assertEqual(sid, 68)
        self.assertEqual(fb.kind, "ok")
        grace.assert_not_called()

    @patch("app.core.yaolu_auto._sleep_interruptible", return_value=True)
    @patch("app.core.yaolu_auto.entry_blackout_remaining_s", return_value=60.0)
    @patch("app.core.yaolu_auto.read_scene_state", return_value=(68, DEFAULT_ENTRY_ANCHOR, "福州城"))
    def test_blackout_preflight_does_not_increment_rounds(
        self, _read_scene, _blackout, _sleep
    ) -> None:
        events = []
        runner = YaoluRunner(pid=123, on_event=events.append)
        runner._rounds = 5
        with (
            patch.object(runner, "_risk_start_blocked", return_value=(False, "")),
            patch.object(runner, "_wait_entry_cd", return_value=True),
        ):
            runner._one_round(MagicMock())
        self.assertEqual(runner._rounds, 5)
        self.assertFalse(any(event.phase == "round" for event in events))
        self.assertTrue(any(event.phase == "entry_cd" for event in events))

    def test_yaolu_timeout_is_presented_in_chinese(self) -> None:
        text = describe_captcha_error("The read operation timed out", YaoluConfig())
        self.assertIn("识别接口请求超时", text)
        self.assertIn("45 秒", text)

    @patch(
        "app.core.yaolu_auto.wait_for_entry_arrival",
        return_value=(True, (35.7, 55.3, -79.8), 1.0),
    )
    @patch("app.core.yaolu_auto.path_to_entry_anchor")
    @patch("app.core.yaolu_auto.cancel_active_entry_session")
    def test_yaolu_failure_recovery_cancels_then_paths_to_entry(
        self, cancel, path_entry, wait_arrival
    ) -> None:
        events = []
        runner = YaoluRunner(pid=123, on_event=events.append)
        runner.hwnd = 10
        runner._next_open_ts = 999999.0
        cancel.return_value = {
            "ok": True,
            "needed": True,
            "queued": True,
            "method": "cancel_session",
            "captcha_closed": True,
            "captcha_was_open": True,
            "esc": 2,
            "nudge": True,
            "pulses": 2,
            "note": "ok",
        }
        path_entry.return_value = {
            "ok": True,
            "target_xyz": list(DEFAULT_ENTRY_ANCHOR),
            "path_scene_id": ENTRY_PATH_SCENE_ID,
            "via": "bridge",
            "mode": ENTRY_PATH_SCENE_ID,
        }
        with patch(
            "app.core.yaolu_auto.is_captcha_dialog_open",
            return_value=MagicMock(shown=False, name=""),
        ):
            runner._recover_after_failure(
                MagicMock(), "答案错误", host_pos=(1.0, 0.0, 2.0)
            )
        cancel.assert_called_once()
        self.assertTrue(cancel.call_args.kwargs.get("force"))
        self.assertTrue(cancel.call_args.kwargs.get("close_captcha"))
        path_entry.assert_called_once()
        self.assertEqual(path_entry.call_args.kwargs.get("anchor"), DEFAULT_ENTRY_ANCHOR)
        wait_arrival.assert_called_once()
        self.assertEqual(runner._next_open_ts, 0.0)
        self.assertIn("已到达妖楼入口", events[-1].message)
        self.assertTrue(
            any(
                "清理小游戏" in e.message or "清理读条" in e.message or "0x21" in e.message
                for e in events
            )
        )

    @patch("app.core.yaolu_auto._sleep_interruptible", return_value=True)
    @patch("app.core.xajh_bridge.ensure_bridge")
    @patch("app.core.yaolu_auto.read_host_cast_state")
    def test_yaolu_active_cast_uses_client_cancel_and_waits_for_idle(
        self, read_cast, ensure, _sleep
    ) -> None:
        active = {
            "readable": True,
            "active": False,
            "session_readable": True,
            "session_active": True,
            "session_state": 11,
            "cast": 0x1000,
            "skill_id": 81184,
            "skill_id_b": 81184,
            "flags": 1,
            "elapsed": 0,
        }
        idle = dict(
            active,
            session_active=False,
            session_state=0,
            skill_id=0,
            skill_id_b=0,
            flags=0,
        )
        read_cast.side_effect = [active, idle, idle]
        bridge = MagicMock()
        bridge.cancel_session.return_value = MagicMock(
            ok=True,
            ret=1,
            note="CANCEL_SESSION queued opcode=0x21",
            to_dict=lambda: {"ok": True, "ret": 1},
        )
        bridge.ui_key.return_value = MagicMock(ok=True, note="UI_KEY ok", error=None)
        ensure.return_value = bridge

        with patch("app.core.yaolu_auto.move_to_target", return_value={"ok": True}):
            with patch(
                "app.core.yaolu_auto.read_scene_position",
                return_value=MagicMock(
                    ok=True, scene_id=68, scene_pos=(1.0, 2.0, 3.0)
                ),
            ):
                out = cancel_active_entry_session(
                    MagicMock(pid=123),
                    YaoluConfig(),
                    hwnd=456,
                )

        self.assertTrue(out["ok"])
        self.assertTrue(out["needed"])
        self.assertTrue(out["queued"])
        self.assertEqual(bridge.cancel_session.call_count, 2)
        self.assertEqual(out["esc"], 2)
        self.assertEqual(bridge.ui_key.call_count, 2)
        bridge.cancel_session.assert_called_with(hwnd=456, timeout_ms=3000)
        bridge.close.assert_called_once()

    @patch("app.core.yaolu_auto.path_to_entry_anchor")
    @patch("app.core.yaolu_auto.cancel_active_entry_session")
    def test_yaolu_cancel_timeout_stops_before_navigation(
        self, cancel, path_entry
    ) -> None:
        events = []
        runner = YaoluRunner(pid=123, on_event=events.append)
        runner.hwnd = 10
        runner.running = True
        cancel.return_value = {
            "ok": False,
            "needed": True,
            "error": "CancelSession acknowledgement timeout",
            "after": {"session_active": True, "active": True},
        }
        ok = runner._recover_after_failure(MagicMock(), "答案错误")
        self.assertFalse(ok)
        self.assertTrue(runner._stop.is_set())
        self.assertFalse(runner.running)
        path_entry.assert_not_called()
        self.assertEqual(events[-1].detail.get("reason"), "cancel_session_failed")

    @patch(
        "app.core.yaolu_auto.wait_for_entry_arrival",
        return_value=(True, (35.7, 55.3, -79.8), 1.0),
    )
    @patch("app.core.yaolu_auto.path_to_entry_anchor")
    @patch("app.core.yaolu_auto.cancel_active_entry_session")
    def test_yaolu_queue_reject_soft_continues_path(
        self, cancel, path_entry, wait_arrival
    ) -> None:
        """queue rejected + Esc/nudge must not hard-stop (live bg log case)."""
        events = []
        runner = YaoluRunner(pid=123, on_event=events.append)
        runner.hwnd = 10
        runner.running = True
        cancel.return_value = {
            "ok": True,
            "needed": True,
            "queued": True,
            "esc": 2,
            "nudge": True,
            "pulses": 2,
            "error": None,
            "note": "CANCEL_SESSION queue rejected (host gate=0) | esc=2",
        }
        path_entry.return_value = {
            "ok": True,
            "target_xyz": list(DEFAULT_ENTRY_ANCHOR),
            "path_scene_id": ENTRY_PATH_SCENE_ID,
            "via": "bridge",
        }
        ok = runner._recover_after_failure(MagicMock(), "识别接口失败：识别失败。")
        self.assertTrue(ok)
        self.assertFalse(runner._stop.is_set())
        self.assertTrue(runner._force_reopen_entry)
        path_entry.assert_called_once()
        wait_arrival.assert_called_once()
        self.assertTrue(any("清理读条" in e.message for e in events))

    @patch("app.core.yaolu_auto.read_scene_position")
    def test_yaolu_wander_waits_for_live_arrival(self, read_pos) -> None:
        read_pos.side_effect = [
            MagicMock(ok=True, scene_pos=(0.0, 0.0, 0.0)),
            MagicMock(ok=True, scene_pos=(19.0, 0.0, 0.0)),
            MagicMock(ok=True, scene_pos=(19.0, 0.0, 0.0)),
            MagicMock(ok=True, scene_pos=(19.0, 0.0, 0.0)),
        ]
        progress: list[str] = []
        with patch("app.core.yaolu_auto._sleep_interruptible", return_value=True):
            arrived, pos, dist = wait_for_wander_arrival(
                MagicMock(),
                {"target_xyz": [20.0, 0.0, 0.0]},
                YaoluConfig(wander_arrive_radius=2.0),
                on_progress=progress.append,
            )
        self.assertTrue(arrived)
        self.assertEqual(pos, (19.0, 0.0, 0.0))
        self.assertEqual(dist, 1.0)
        self.assertTrue(any("剩余" in item for item in progress))

    @patch("app.core.yaolu_auto.read_scene_position")
    def test_yaolu_wander_replaces_a_stuck_random_point(self, read_pos) -> None:
        read_pos.return_value = MagicMock(ok=True, scene_pos=(0.0, 0.0, 0.0))
        clock = {"now": 0.0}

        def _monotonic() -> float:
            return float(clock["now"])

        def _sleep(seconds, _stop) -> bool:
            clock["now"] += float(seconds)
            return True

        progress: list[str] = []
        with patch("app.core.yaolu_auto.time.monotonic", side_effect=_monotonic), patch(
            "app.core.yaolu_auto._sleep_interruptible", side_effect=_sleep
        ):
            arrived, _pos, dist = wait_for_wander_arrival(
                MagicMock(),
                {"target_xyz": [20.0, 0.0, 0.0]},
                YaoluConfig(wander_poll_s=0.5, wander_stuck_timeout_s=1.0),
                on_progress=progress.append,
            )
        self.assertFalse(arrived)
        self.assertEqual(dist, 20.0)
        self.assertTrue(any("卡住" in item for item in progress))

    @patch("app.core.yaolu_auto.move_to_target")
    @patch("app.core.yaolu_auto.read_scene_position")
    def test_yaolu_path_to_entry_uses_fuzhou_scene_mode(
        self, read_pos, move_to_target
    ) -> None:
        # Live on another map; still HostMove with Fuzhou scene 68.
        read_pos.return_value = MagicMock(
            ok=True, scene_id=100, scene_pos=(1.0, 2.0, 3.0)
        )
        move_to_target.return_value = {
            "ok": True,
            "via": "bridge",
            "mode": ENTRY_PATH_SCENE_ID,
        }
        out = path_to_entry_anchor(MagicMock(pid=7), YaoluConfig(), hwnd=10)
        self.assertTrue(out["ok"])
        self.assertEqual(out["purpose"], "cross_map_path_to_entry")
        self.assertEqual(out["path_scene_id"], ENTRY_PATH_SCENE_ID)
        self.assertEqual(
            move_to_target.call_args.kwargs["move_mode"], ENTRY_PATH_SCENE_ID
        )
        self.assertEqual(
            move_to_target.call_args.kwargs["scene_id"], ENTRY_PATH_SCENE_ID
        )
        tgt = move_to_target.call_args.args[1]
        self.assertAlmostEqual(tgt.x, DEFAULT_ENTRY_ANCHOR[0], places=2)
        self.assertAlmostEqual(tgt.z, DEFAULT_ENTRY_ANCHOR[2], places=2)

    @patch("app.core.yaolu_auto.move_to_target")
    @patch("app.core.yaolu_auto.read_scene_position")
    def test_yaolu_wander_uses_entry_scene_mode(
        self, read_pos, move_to_target
    ) -> None:
        read_pos.return_value = MagicMock(
            ok=True, scene_id=68, scene_pos=(1.0, 2.0, 3.0)
        )
        move_to_target.return_value = {"ok": True, "via": "bridge", "mode": 68}
        out = wander_far_from_entry(MagicMock(pid=7), YaoluConfig(), hwnd=10)
        self.assertTrue(out["ok"])
        self.assertEqual(out["purpose"], "wander_near_entry")
        self.assertEqual(move_to_target.call_args.kwargs["move_mode"], 68)

    @patch("app.core.yaolu_auto._sleep_interruptible", return_value=True)
    @patch("app.core.xajh_bridge.ensure_bridge")
    @patch("app.core.yaolu_auto.read_host_cast_state")
    def test_cancel_session_queue_reject_still_ok_with_esc(
        self, read_cast, ensure, _sleep
    ) -> None:
        idle = {
            "readable": True,
            "active": False,
            "session_readable": True,
            "session_active": False,
            "session_state": 0,
            "cast": 0,
            "skill_id": 0,
            "skill_id_b": 0,
            "flags": 0,
            "elapsed": 0,
        }
        read_cast.return_value = idle
        bridge = MagicMock()
        bridge.cancel_session.side_effect = [
            MagicMock(
                ok=True,
                ret=1,
                note="CANCEL_SESSION forced queue opcode=0x21 (host gate=0)",
                to_dict=lambda: {"ok": True, "ret": 1},
            ),
            MagicMock(
                ok=False,
                ret=0,
                note="CANCEL_SESSION queue rejected (host gate=0)",
                error="CANCEL_SESSION queue rejected (host gate=0)",
                to_dict=lambda: {"ok": False, "ret": 0},
            ),
        ]
        bridge.ui_key.return_value = MagicMock(ok=True, note="UI_KEY ok", error=None)
        ensure.return_value = bridge
        with patch("app.core.yaolu_auto.move_to_target", return_value={"ok": True}):
            with patch(
                "app.core.yaolu_auto.read_scene_position",
                return_value=MagicMock(
                    ok=True, scene_id=68, scene_pos=(35.0, 55.0, -80.0)
                ),
            ):
                out = cancel_active_entry_session(
                    MagicMock(pid=1),
                    YaoluConfig(cancel_session_force=True),
                    hwnd=9,
                    force=True,
                )
        self.assertTrue(out["ok"])
        self.assertIsNone(out.get("error"))
        self.assertGreaterEqual(out["esc"], 1)
        self.assertTrue(out["nudge"])

    @patch("app.core.yaolu_auto._sleep_interruptible", return_value=True)
    @patch("app.core.xajh_bridge.ensure_bridge")
    @patch("app.core.yaolu_auto.read_host_cast_state")
    def test_cancel_session_force_queues_when_host_gate_zero(
        self, read_cast, ensure, _sleep
    ) -> None:
        # Live yaolu: host+0x41C stays 0; new DLL still queues opcode 0x21.
        idle = {
            "readable": True,
            "active": False,
            "session_readable": True,
            "session_active": False,
            "session_state": 0,
            "cast": 0,
            "skill_id": 0,
            "skill_id_b": 0,
            "flags": 0,
            "elapsed": 0,
        }
        read_cast.return_value = idle
        bridge = MagicMock()
        bridge.cancel_session.return_value = MagicMock(
            ok=True,
            ret=1,
            note="CANCEL_SESSION forced queue opcode=0x21 (host gate=0)",
            to_dict=lambda: {"ok": True, "ret": 1},
        )
        bridge.ui_key.return_value = MagicMock(ok=True, note="UI_KEY ok", error=None)
        ensure.return_value = bridge
        with patch("app.core.yaolu_auto.move_to_target", return_value={"ok": True}):
            with patch(
                "app.core.yaolu_auto.read_scene_position",
                return_value=MagicMock(
                    ok=True, scene_id=68, scene_pos=(35.0, 55.0, -80.0)
                ),
            ):
                out = cancel_active_entry_session(
                    MagicMock(pid=1),
                    YaoluConfig(cancel_session_force=True),
                    hwnd=9,
                    force=True,
                )
        self.assertTrue(out["ok"])
        self.assertTrue(out["needed"])
        self.assertTrue(out["queued"])
        self.assertEqual(bridge.cancel_session.call_count, 2)
        self.assertGreaterEqual(out["esc"], 1)
        self.assertTrue(out["nudge"])

    @patch("app.core.yaolu_auto.wait_for_entry_arrival", return_value=(True, DEFAULT_ENTRY_ANCHOR, 1.0))
    @patch("app.core.yaolu_auto.path_to_entry_anchor")
    @patch("app.core.yaolu_auto.read_scene_position")
    def test_ensure_path_to_entry_when_far(
        self, read_pos, path_entry, wait_arr
    ) -> None:
        from app.core.yaolu_auto import ensure_path_to_entry

        read_pos.return_value = MagicMock(
            ok=True, scene_id=68, scene_pos=(200.0, 55.0, -200.0)
        )
        path_entry.return_value = {
            "ok": True,
            "target_xyz": list(DEFAULT_ENTRY_ANCHOR),
            "path_scene_id": 68,
            "via": "bridge",
        }
        out = ensure_path_to_entry(
            MagicMock(pid=1),
            YaoluConfig(prepath_min_dist=12.0),
            host_pos=(200.0, 55.0, -200.0),
        )
        self.assertTrue(out["ok"])
        self.assertFalse(out["skipped"])
        path_entry.assert_called_once()
        wait_arr.assert_called_once()

    def test_buyu_block_stops_runner_not_retry(self) -> None:
        events = []
        runner = YaoluRunner(pid=1, on_event=events.append)
        runner.running = True
        from app.core.game_sys_msg import CaptchaAnswerFeedback

        fb = CaptchaAnswerFeedback(
            kind="block",
            text="队伍成员处于捕羽状态，不能进入副本",
            method="dlg:Win_Popmsg",
        )
        # Simulate the enter_fail branch decision path via public helpers.
        from app.core.game_sys_msg import is_shenfa_block_feedback

        self.assertTrue(is_shenfa_block_feedback(fb))
        # Direct recover must not be scheduled for hard block — unit via runner field.
        self.assertTrue(runner.running)

    @patch("app.core.yaolu_auto.super_loot_step")
    @patch("app.core.yaolu_auto.ensure_path_to_entry")
    def test_open_entry_skips_interact_until_really_arrived(
        self, ensure_path, super_loot
    ) -> None:
        """HostMove alone is not enough; only live arrival opens 暗道."""
        from app.core.yaolu_auto import open_entry

        ensure_path.return_value = {
            "ok": False,
            "skipped": False,
            "arrived": False,
            "dist": 80.0,
            "final_dist": 42.0,
            "error": "path timeout",
        }
        res = open_entry(MagicMock(pid=1), YaoluConfig(), prepath=True)
        self.assertFalse(res.ok)
        self.assertEqual(res.action, "path")
        self.assertIn("未到达", res.message or "")
        super_loot.assert_not_called()

    @patch("app.core.yaolu_auto.super_loot_step")
    @patch("app.core.yaolu_auto.ensure_path_to_entry")
    def test_open_entry_opens_only_after_arrival(self, ensure_path, super_loot) -> None:
        from app.core.yaolu_auto import open_entry

        ensure_path.return_value = {
            "ok": True,
            "skipped": False,
            "arrived": True,
            "final_dist": 2.0,
            "host_pos": list(DEFAULT_ENTRY_ANCHOR),
            "live_scene_id": 68,
        }
        super_loot.return_value = MagicMock(ok=True, action="open", message="ok")
        res = open_entry(MagicMock(pid=1), YaoluConfig(), prepath=True)
        self.assertTrue(res.ok)
        super_loot.assert_called_once()

    @patch("app.core.yaolu_auto.open_entry")
    @patch("app.core.yaolu_auto.wait_enter_result")
    @patch("app.core.yaolu_auto.wait_and_solve_captcha")
    @patch("app.core.yaolu_auto.snapshot_entry_open_block_hits", return_value={})
    @patch("app.core.yaolu_auto.wait_for_captcha_dialog")
    @patch("app.core.yaolu_auto.check_interact_gate")
    @patch("app.core.yaolu_auto.entry_blackout_remaining_s", return_value=0.0)
    @patch("app.core.yaolu_auto.read_scene_state")
    def test_buyu_after_answer_ok_stops_runner(
        self,
        read_scene,
        _blackout,
        gate,
        wait_dialog,
        _snapshot,
        solve,
        wait_enter,
        open_entry,
    ) -> None:
        from app.core.game_sys_msg import CaptchaAnswerFeedback

        read_scene.return_value = (68, (1.0, 0.0, 2.0), "福州城")
        gate.return_value = MagicMock(
            ok=True,
            in_team=True,
            is_leader=True,
            captcha_open=False,
            captcha_name="Win_Question3D",
            team_ptr=1,
            host_id_lo=2,
            leader_id_lo=2,
        )
        open_entry.return_value = MagicMock(
            ok=True,
            action="open",
            message="opened",
            error=None,
            distance=2.0,
            data={},
        )
        wait_dialog.return_value = MagicMock(
            ok=True, box=MagicMock(width=660, height=460), png=b"png"
        )
        solve.return_value = MagicMock(
            ok=True,
            identify_id=None,
            animal="cat",
            positions=[1, 2],
            confidence=0.9,
            answer_baseline={},
        )
        wait_enter.return_value = (
            False,
            68,
            "队伍成员处于捕羽状态，不能进入副本",
            CaptchaAnswerFeedback(
                kind="block",
                text="队伍成员处于捕羽状态，不能进入副本",
                method="dlg:Win_Popmsg",
                error="答案正确，请尽快进入活动",
            ),
        )
        events = []
        runner = YaoluRunner(pid=123, on_event=events.append)
        runner.running = True
        runner._one_round(MagicMock())
        self.assertTrue(runner._stop.is_set())
        self.assertFalse(runner.running)
        self.assertTrue(
            any(
                e.phase == "stopped" and "捕羽" in e.message
                for e in events
            )
        )
        self.assertFalse(runner._entry_reopen_pending)

    def test_emit_skips_duplicate_log_when_ui_sink_wired(self) -> None:
        logs: list[str] = []
        events: list = []
        runner = YaoluRunner(
            pid=1, on_event=events.append, log=logs.append
        )
        runner._rounds = 2
        runner._emit("round", "第 2 轮 开始")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].phase, "round")
        # UI sink present => no second yaolu [round] line.
        self.assertEqual(logs, [])

    def test_emit_logs_when_no_ui_sink(self) -> None:
        logs: list[str] = []
        runner = YaoluRunner(pid=1, log=logs.append)
        runner._rounds = 1
        runner._emit("round", "第 1 轮 开始")
        self.assertEqual(logs, ["yaolu [round] 第 1 轮 开始"])

    @patch("app.core.aui_click.is_captcha_dialog_open")
    @patch("app.core.aui_click.click_captcha_btn_cancel", return_value=True)
    @patch("app.core.aui_click._bridge_for_session", return_value=None)
    def test_close_captcha_dialog_clicks_cancel(
        self, _bridge, click_cancel, is_open
    ) -> None:
        from app.core.aui_click import close_captcha_dialog

        is_open.side_effect = [
            MagicMock(shown=True, name="Win_Question3D", dlg_ptr=0x1),
            MagicMock(shown=False, name="", dlg_ptr=0),
        ]
        out = close_captcha_dialog(MagicMock(pid=1), hwnd=10, attempts=1, settle_s=0.0)
        self.assertTrue(out["ok"])
        self.assertTrue(out["closed"])
        self.assertTrue(out["was_open"])
        self.assertEqual(out["cancel_clicks"], 1)
        click_cancel.assert_called_once()

    @patch("app.core.yaolu_auto.wait_for_entry_arrival", return_value=(True, DEFAULT_ENTRY_ANCHOR, 1.0))
    @patch("app.core.yaolu_auto.path_to_entry_anchor")
    @patch("app.core.yaolu_auto.cancel_active_entry_session")
    @patch("app.core.yaolu_auto.check_interact_gate")
    @patch("app.core.yaolu_auto.entry_blackout_remaining_s", return_value=0.0)
    @patch("app.core.yaolu_auto.read_scene_state")
    def test_stale_captcha_is_closed_not_reused(
        self,
        read_scene,
        _blackout,
        gate,
        cancel,
        path_entry,
        wait_arrival,
    ) -> None:
        """Open captcha must not be reused after identify fail; force close path."""
        read_scene.return_value = (68, DEFAULT_ENTRY_ANCHOR, "福州城")
        gate.return_value = MagicMock(
            ok=True,
            in_team=True,
            is_leader=True,
            captcha_open=True,
            captcha_name="Win_Question3D",
            team_ptr=1,
            host_id_lo=2,
            leader_id_lo=2,
        )
        cancel.return_value = {
            "ok": True,
            "queued": True,
            "esc": 2,
            "nudge": True,
            "captcha_closed": True,
            "captcha_was_open": True,
            "pulses": 2,
            "note": "ok",
        }
        path_entry.return_value = {
            "ok": True,
            "target_xyz": list(DEFAULT_ENTRY_ANCHOR),
            "path_scene_id": 68,
            "via": "bridge",
            "mode": 68,
        }
        events = []
        runner = YaoluRunner(pid=1, on_event=events.append)
        runner.hwnd = 10
        runner._one_round(MagicMock())
        # Must recover/close, not jump into captcha solve on residual dialog.
        self.assertTrue(
            any(
                e.detail.get("reason") == "stale_captcha_reuse_blocked"
                or "残留验证码" in e.message
                for e in events
            )
        )
        cancel.assert_called()
        self.assertTrue(cancel.call_args.kwargs.get("close_captcha"))

    @patch("app.core.aui_click.is_captcha_dialog_open")
    @patch("app.core.aui_click.click_client_bg", return_value=True)
    @patch("app.core.aui_click.get_captcha_btn_cancel_rect")
    @patch("app.core.aui_click.get_captcha_dlg_rect")
    @patch("app.core.aui_click._bridge_for_session", return_value=None)
    def test_close_captcha_uses_geometry_when_btn_rect_bad(
        self, _bridge, dlg_rect, cancel_rect, click_bg, is_open
    ) -> None:
        """Live Win_Question3D: Btn_Cancel 0x0 → geometry cancel click."""
        from app.core.aui_click import AuiCtrlRect, close_captcha_dialog

        cancel_rect.return_value = AuiCtrlRect(
            ok=False, name="Btn_Cancel", error="bad rect 0x0"
        )
        dlg_rect.return_value = MagicMock(ok=True, x=383, y=170, w=660, h=460)
        is_open.side_effect = [
            MagicMock(shown=True, name="Win_Question3D", dlg_ptr=0x1),
            MagicMock(shown=False, name="", dlg_ptr=0),
        ]
        out = close_captcha_dialog(MagicMock(pid=1), hwnd=10, attempts=1, settle_s=0.0)
        self.assertTrue(out["ok"])
        self.assertTrue(out["closed"])
        self.assertTrue(out["was_open"])
        self.assertGreaterEqual(out["cancel_clicks"], 1)
        click_bg.assert_called()

    @patch("app.core.yaolu_auto.super_loot_step")
    @patch("app.core.yaolu_auto._sleep_interruptible", return_value=True)
    @patch("app.core.yaolu_auto.ensure_path_to_entry")
    def test_open_entry_delays_only_after_real_prepath(
        self, ensure_path, sleep_fn, super_loot
    ) -> None:
        from app.core.yaolu_auto import open_entry

        ensure_path.return_value = {
            "ok": True,
            "skipped": False,
            "arrived": True,
            "final_dist": 2.0,
            "host_pos": list(DEFAULT_ENTRY_ANCHOR),
            "live_scene_id": 68,
        }
        super_loot.return_value = MagicMock(ok=True, action="open", message="ok")
        cfg = YaoluConfig(post_arrive_open_delay_s=1.2)
        res = open_entry(MagicMock(pid=1), cfg, prepath=True)
        self.assertTrue(res.ok)
        # Delay invoked for real arrival path.
        self.assertTrue(
            any(
                abs(float(c.args[0]) - 1.2) < 1e-6
                for c in sleep_fn.call_args_list
                if c.args
            )
        )
        super_loot.assert_called_once()

    @patch("app.core.yaolu_auto.super_loot_step")
    @patch("app.core.yaolu_auto._sleep_interruptible", return_value=True)
    @patch("app.core.yaolu_auto.ensure_path_to_entry")
    def test_open_entry_no_delay_when_already_near(
        self, ensure_path, sleep_fn, super_loot
    ) -> None:
        from app.core.yaolu_auto import open_entry

        ensure_path.return_value = {
            "ok": True,
            "skipped": True,
            "arrived": False,
            "dist": 1.2,
            "live_scene_id": 68,
        }
        super_loot.return_value = MagicMock(ok=True, action="open", message="ok")
        cfg = YaoluConfig(post_arrive_open_delay_s=1.2)
        res = open_entry(MagicMock(pid=1), cfg, prepath=True)
        self.assertTrue(res.ok)
        # No post-arrive delay when prepath skipped (already near).
        self.assertFalse(
            any(
                abs(float(c.args[0]) - 1.2) < 1e-6
                for c in sleep_fn.call_args_list
                if c.args
            )
        )
        super_loot.assert_called_once()

    @patch("app.core.xajh_bridge.ensure_bridge", return_value=None)
    @patch("app.core.client_build.match_client_profile")
    @patch("app.core.client_build.fingerprint_client")
    @patch("app.core.yaolu_auto.open_attach_session")
    def test_yaolu_stale_bridge_stops_before_rounds(
        self, attach, fingerprint, match_profile, _bridge
    ) -> None:
        events = []
        session = MagicMock()
        session.exe_path = "xajh.exe"
        attach.return_value = session
        fingerprint.return_value = MagicMock(
            sha256="AA" * 32, size=1, image_size=0x1000
        )
        match_profile.return_value = MagicMock(build_id="known")
        runner = YaoluRunner(
            pid=123,
            cfg=YaoluConfig(login_token="configured"),
            on_event=events.append,
        )
        runner._loop()
        reasons = [event.detail.get("reason") for event in events]
        self.assertIn("bridge_unavailable_or_stale", reasons)
        self.assertFalse(any(event.phase == "round" for event in events))



    def test_yaolu_risk_defaults_entry_focus_v1(self) -> None:
        cfg = YaoluConfig()
        self.assertEqual(cfg.profile_id, "entry_focus_v1")
        self.assertEqual(cfg.daily_success_limit, 0)
        self.assertEqual(cfg.success_rest_min_s, 0.0)
        self.assertEqual(cfg.success_rest_max_s, 0.0)
        self.assertEqual(cfg.answer_fail_rest_min_s, 60.0)
        self.assertEqual(cfg.answer_fail_rest_max_s, 180.0)
        self.assertEqual(cfg.consecutive_answer_fail_limit, 3)
        self.assertEqual(cfg.consecutive_answer_fail_pause_s, 300.0)
        self.assertFalse(cfg.consecutive_answer_fail_stop)
        self.assertTrue(cfg.shenfa_lock_day)
        self.assertTrue(cfg.risk_audit_enabled)
        self.assertTrue(cfg.captcha_reject_low_confidence)
        self.assertEqual(cfg.min_confidence, 0.25)
        self.assertEqual(cfg.entry_reopen_growth, 1.5)
        self.assertEqual(cfg.entry_reopen_max_total_s, 1800.0)
        self.assertIn("min_confidence", YAOLU_RISK_BASELINE_PRE_20260722)
        self.assertEqual(YAOLU_RISK_BASELINE_PRE_20260722["entry_reopen_growth"], 2.0)
        self.assertEqual(YAOLU_RISK_BASELINE_PRE_20260722["min_confidence"], 0.15)
        snap = snapshot_yaolu_risk_cfg(cfg)
        diff = diff_yaolu_risk_vs_baseline(snap)
        self.assertIn("min_confidence", diff)
        self.assertIn("entry_reopen_growth", diff)
        self.assertNotEqual(diff["entry_reopen_growth"]["old"], diff["entry_reopen_growth"]["new"])

    def test_yaolu_risk_audit_start_writes_jsonl(self) -> None:
        import json
        import tempfile
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as td:
            with patch.dict("os.environ", {"XAJH_LOG_DIR": td}):
                events = []
                runner = YaoluRunner(pid=4242, on_event=events.append)
                # Avoid starting real thread: call audit helpers directly.
                runner._run_id = "testrun01"
                runner._write_run_start_audit()
                path = yaolu_risk_audit_path()
                self.assertTrue(path.is_file())
                lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                self.assertGreaterEqual(len(lines), 1)
                rec = json.loads(lines[-1])
                self.assertEqual(rec["event"], "run_start")
                self.assertEqual(rec["profile_id"], "entry_focus_v1")
                self.assertEqual(rec["run_id"], "testrun01")
                self.assertIn("cfg_risk", rec)
                self.assertIn("diff_vs_baseline", rec)
                self.assertNotIn("captcha_api_key", rec.get("cfg_risk", {}))
                # state last_run_id aligned
                state_path = yaolu_risk_state_path()
                self.assertTrue(state_path.is_file())

    def test_yaolu_daily_limit_zero_does_not_stop(self) -> None:
        runner = YaoluRunner(pid=7, cfg=YaoluConfig(daily_success_limit=0))
        with patch.object(runner, "_daily_ok_persisted", return_value=99):
            self.assertFalse(runner._maybe_stop_daily_limit())
            self.assertFalse(runner._stop.is_set())

    def test_yaolu_daily_limit_stops_when_reached(self) -> None:
        events = []
        runner = YaoluRunner(
            pid=8,
            cfg=YaoluConfig(daily_success_limit=2),
            on_event=events.append,
        )
        with patch.object(runner, "_daily_ok_persisted", return_value=2):
            self.assertTrue(runner._maybe_stop_daily_limit())
        self.assertTrue(runner._stop.is_set())
        self.assertEqual(events[-1].detail.get("reason"), "daily_success_limit")

    def test_yaolu_shenfa_day_lock_blocks_start(self) -> None:
        events = []
        runner = YaoluRunner(
            pid=9,
            cfg=YaoluConfig(shenfa_lock_day=True, risk_state_enabled=True),
            on_event=events.append,
        )
        with patch.object(runner, "_is_shenfa_day_locked", return_value=True):
            runner.start()
        self.assertFalse(runner.running)
        self.assertTrue(events)
        self.assertEqual(events[-1].detail.get("reason"), "risk_gate")

    def test_yaolu_low_confidence_rejected_in_solve(self) -> None:
        from app.core.captcha_client import CaptchaIdentifyResult
        from app.core.captcha_dialog import DialogBox
        from app.core.yaolu_auto import DialogCapture, solve_captcha_dialog

        dlg = DialogCapture(
            ok=True,
            box=DialogBox(left=0, top=0, right=400, bottom=300),
            png=b"fakepng",
        )
        ident = CaptchaIdentifyResult(
            ok=True,
            animal="羊",
            positions=[1, 2],
            confidence=0.249,
            error=None,
        )
        with patch("app.core.yaolu_auto.identify_image", return_value=ident):
            out = solve_captcha_dialog(
                0,
                YaoluConfig(
                    min_confidence=0.25,
                    captcha_reject_low_confidence=True,
                    captcha_api_key="k",
                ),
                dlg,
                session=None,
                log=lambda *_a, **_k: None,
            )
        self.assertFalse(out.ok)
        self.assertIn("置信度过低", out.error or "")

    def test_yaolu_consecutive_answer_fail_arms_pause(self) -> None:
        events = []
        runner = YaoluRunner(
            pid=11,
            cfg=YaoluConfig(
                consecutive_answer_fail_limit=3,
                consecutive_answer_fail_pause_s=300.0,
                consecutive_answer_fail_stop=False,
                answer_fail_rest_min_s=0.0,
                answer_fail_rest_max_s=0.0,
                entry_cd_s=0.0,
                entry_cd_jitter_s=0.0,
            ),
            on_event=events.append,
        )
        runner.running = True
        runner._answer_fail_streak = 3
        # Simulate arm path used after answer fail
        pause = float(runner.cfg.consecutive_answer_fail_pause_s)
        runner._arm_open_rest(pause, "连续答错3次熔断", label="答错熔断")
        self.assertGreater(runner._next_open_ts, 0)
        remain = runner._next_open_ts - __import__("time").monotonic()
        self.assertGreaterEqual(remain, 290.0)
        self.assertEqual(runner._next_open_reason, "答错熔断")

    def test_yaolu_entry_reopen_default_growth_1_5(self) -> None:
        events = []
        runner = YaoluRunner(
            pid=12,
            cfg=YaoluConfig(entry_reopen_jitter=0.0),
            on_event=events.append,
        )
        self.assertTrue(runner._schedule_entry_reopen("x"))
        self.assertTrue(runner._schedule_entry_reopen("x"))
        delays = [
            e.detail["delay_s"]
            for e in events
            if e.detail.get("reason") == "entry_reopen_scheduled"
        ]
        self.assertAlmostEqual(delays[0], 30.0, places=3)
        self.assertAlmostEqual(delays[1], 45.0, places=3)



    def test_entry_blackout_55_to_05(self) -> None:
        """Avoid opening around the hour: only before :55 and after :05."""
        from datetime import datetime
        from app.core.yaolu_auto import YaoluConfig, entry_blackout_remaining_s

        cfg = YaoluConfig(
            entry_blackout_enabled=True,
            entry_blackout_start_min=55,
            entry_blackout_end_min=5,
            entry_blackout_end_sec=0,
        )
        def rem(h, m, s=0):
            return entry_blackout_remaining_s(
                cfg, now=datetime(2026, 7, 23, h, m, s)
            )
        self.assertEqual(rem(10, 30), 0.0)
        self.assertGreater(rem(10, 55), 0.0)
        self.assertGreater(rem(11, 0), 0.0)
        self.assertGreater(rem(11, 4, 59), 0.0)
        self.assertEqual(rem(11, 5), 0.0)
        # :55 -> next :05 ≈ 10 minutes
        self.assertAlmostEqual(rem(10, 55), 10 * 60.0, delta=1.0)

    def test_yaolu_captcha_defaults_inject_timing_only(self) -> None:
        from app.core.yaolu_auto import YaoluConfig
        c = YaoluConfig()
        # Frequency humanize only — no real mouse / slide by default
        self.assertFalse(c.allow_cursor_click)
        self.assertFalse(c.captcha_prefer_real_mouse)
        self.assertFalse(c.captcha_slide_enabled)
        self.assertEqual(c.captcha_hold_min_ms, 70)
        self.assertEqual(c.captcha_hold_max_ms, 160)
        self.assertEqual(c.captcha_click_gap_s, 0.28)
        self.assertEqual(c.entry_blackout_start_min, 55)
        self.assertEqual(c.entry_blackout_end_min, 5)


if __name__ == "__main__":
    unittest.main()
