# -*- coding: utf-8 -*-
"""Role business lifecycle detection tests. @author by ak"""
from __future__ import annotations

import queue
import threading
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import patch
from app.core.inject_gate import (
    InjectOutcome,
    _verify_bridge_host_context,
    run_delete_inject_async,
)
from app.ui.app_shell import ShellApp
from app.ui.pages import FeaturePage
from app.ui.session_window import SessionFeatureWindow
from app.core.session_store import SessionStore


class SessionBusinessLifecycleTests(unittest.TestCase):
    def test_async_inject_publishes_before_inner_thread_cleanup(self) -> None:
        inner_release = threading.Event()
        delivered = threading.Event()
        results: list[InjectOutcome] = []

        def fake_run_delete_inject(*, on_result, **_kwargs):
            out = InjectOutcome(ok=True, pid=19000, ping_ret=123)
            on_result(out)
            inner_release.wait(timeout=2.0)
            return out

        def on_done(out: InjectOutcome) -> None:
            results.append(out)
            delivered.set()

        try:
            with patch(
                "app.core.inject_gate.run_delete_inject",
                side_effect=fake_run_delete_inject,
            ):
                run_delete_inject_async(on_done, preferred_pid=19000)
                self.assertTrue(delivered.wait(timeout=0.5))
        finally:
            inner_release.set()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].pid, 19000)

    def test_tray_quit_queue_does_not_reschedule_after_shutdown(self) -> None:
        calls: list[str] = []
        fake = SimpleNamespace(
            _queue_job="old",
            _closing=False,
            msg_q=queue.Queue(),
            _quit_app=lambda: (calls.append("quit"), setattr(fake, "_closing", True)),
            after=lambda *_args: calls.append("after"),
            _log=lambda _message: None,
        )
        fake.msg_q.put(("__TRAY_QUIT__",))
        ShellApp._drain_queue(fake)
        self.assertEqual(calls, ["quit"])
        self.assertIsNone(fake._queue_job)

    def test_feature_page_drain_job_is_cancelled_on_shutdown(self) -> None:
        cancelled: list[str] = []
        fake = SimpleNamespace(
            _ui_drain_closed=False,
            _ui_drain_job=None,
            _drain_ui=lambda: None,
            after=lambda _delay, _callback: "after-1",
            after_cancel=lambda job: cancelled.append(job),
        )
        FeaturePage._schedule_ui_drain(fake, 120)
        self.assertEqual(fake._ui_drain_job, "after-1")
        FeaturePage._cancel_ui_drain(fake)
        self.assertTrue(fake._ui_drain_closed)
        self.assertEqual(cancelled, ["after-1"])

    def test_inject_error_dialog_uses_visible_feature_window_owner(self) -> None:
        calls: list[object] = []
        owner = SimpleNamespace(
            winfo_exists=lambda: True,
            deiconify=lambda: calls.append("deiconify"),
            lift=lambda: calls.append("lift"),
            attributes=lambda *args: calls.append(args),
            focus_force=lambda: calls.append("focus"),
            update_idletasks=lambda: calls.append("update"),
        )
        fake = SimpleNamespace(
            _hidden_to_tray=True,
            _closing=False,
            _hide_to_tray=lambda: calls.append("hide-root"),
        )
        with patch("app.ui.app_shell.messagebox.showerror") as showerror:
            ShellApp._show_inject_message(
                fake,
                "连接游戏",
                "连接失败",
                parent=owner,
            )
        showerror.assert_called_once_with("连接游戏", "连接失败", parent=owner)
        self.assertIn(("-topmost", True), calls)
        self.assertIn(("-topmost", False), calls)
        self.assertNotIn("hide-root", calls)

    def test_reconcile_destroys_feature_window_without_mounted_session(self) -> None:
        calls: list[str] = []

        class FakeWindow:
            def __init__(self) -> None:
                self.session = SimpleNamespace(pid=19003)

            def winfo_exists(self) -> bool:
                return True

            def shutdown(self) -> None:
                calls.append("shutdown")

            def destroy(self) -> None:
                calls.append("destroy")

        win = FakeWindow()
        fake = SimpleNamespace(
            store=SimpleNamespace(pids=lambda: set()),
            _feature_wins={19003: win},
            winfo_children=lambda: [win],
            _feature_windows_for_pid=lambda _pid: [win],
            _shutdown_destroy_feature_window=(
                ShellApp._shutdown_destroy_feature_window
            ),
            _log=lambda message: calls.append(str(message)),
        )
        with patch("app.ui.app_shell.SessionFeatureWindow", FakeWindow):
            ShellApp._reconcile_feature_windows(fake)
        self.assertNotIn(19003, fake._feature_wins)
        self.assertEqual(calls[:2], ["shutdown", "destroy"])
        self.assertTrue(any("孤儿功能窗" in line for line in calls))

    def test_reconcile_restores_ready_panel_while_game_is_alive(self) -> None:
        calls: list[str] = []

        class FakeWindow:
            def __init__(self) -> None:
                self.session = SimpleNamespace(pid=19005)
                self._bridge_ready = True
                self._active_keys = ["loot"]

            def winfo_exists(self) -> bool:
                return True

            def shutdown(self) -> None:
                calls.append("shutdown")

            def destroy(self) -> None:
                calls.append("destroy")

        win = FakeWindow()
        mounted: list[object] = []
        fake = SimpleNamespace(
            store=SimpleNamespace(
                pids=lambda: set(),
                mount=lambda session: mounted.append(session),
            ),
            _feature_wins={},
            winfo_children=lambda: [win],
            _feature_windows_for_pid=lambda _pid: [win],
            _feature_window_rank=ShellApp._feature_window_rank,
            _shutdown_destroy_feature_window=(
                ShellApp._shutdown_destroy_feature_window
            ),
            _log=lambda message: calls.append(str(message)),
        )
        with (
            patch("app.ui.app_shell.SessionFeatureWindow", FakeWindow),
            patch("app.core.diag_log.process_alive", return_value=True),
        ):
            ShellApp._reconcile_feature_windows(fake)
        self.assertEqual(mounted, [win.session])
        self.assertIs(fake._feature_wins[19005], win)
        self.assertNotIn("shutdown", calls)
        self.assertNotIn("destroy", calls)
        self.assertTrue(any("已恢复脱离会话" in line for line in calls))

    def test_delete_failure_shows_modal_despite_stale_batch_flags(self) -> None:
        calls: list[object] = []
        win = SimpleNamespace(
            shutdown=lambda: calls.append("shutdown"),
            destroy=lambda: calls.append("destroy"),
        )
        fake = SimpleNamespace(
            _closing=False,
            _inject_busy=True,
            _inject_current_source="Delete",
            _inject_hide_after=True,
            _inject_batch_stats={"ok": 0, "fail": 0, "errors": []},
            _feature_wins={19006: win},
            _log=lambda message: calls.append(str(message)),
            _feature_win_alive=lambda _pid: win,
            store=SimpleNamespace(unmount=lambda pid: calls.append(("unmount", pid))),
            _shutdown_destroy_feature_window=(
                ShellApp._shutdown_destroy_feature_window
            ),
            _show_inject_message=lambda *args, **kwargs: calls.append(
                ("dialog", args, kwargs)
            ),
            _del_watch=SimpleNamespace(reset=lambda: calls.append("reset")),
        )
        ShellApp._apply_inject(
            fake,
            {
                "ok": False,
                "pid": 19006,
                "error": "stale bridge",
                "fail_code": "STALE_BRIDGE_RESTART_GAME",
            },
        )
        dialogs = [item for item in calls if isinstance(item, tuple) and item[0] == "dialog"]
        self.assertEqual(len(dialogs), 1)
        self.assertIsNone(dialogs[0][2]["parent"])
        self.assertLess(calls.index("destroy"), calls.index(dialogs[0]))
        self.assertEqual(fake._inject_current_source, "")

    def test_inject_failure_never_stops_existing_business_panel(self) -> None:
        calls: list[object] = []
        session = SimpleNamespace(pid=19007)
        win = SimpleNamespace(
            session=session,
            _bridge_ready=True,
            _active_keys=["activity"],
            shutdown=lambda: calls.append("shutdown"),
            destroy=lambda: calls.append("destroy"),
        )
        fake = SimpleNamespace(
            _closing=False,
            _inject_busy=True,
            _inject_current_source="Delete",
            _inject_hide_after=False,
            _inject_batch_stats=None,
            _feature_wins={19007: win},
            _log=lambda message: calls.append(str(message)),
            _feature_win_alive=lambda _pid: win,
            store=SimpleNamespace(
                mount=lambda mounted: calls.append(("mount", mounted)),
                unmount=lambda pid: calls.append(("unmount", pid)),
            ),
            _shutdown_destroy_feature_window=(
                ShellApp._shutdown_destroy_feature_window
            ),
            _show_inject_message=lambda *args, **kwargs: calls.append(
                ("dialog", args, kwargs)
            ),
            _del_watch=SimpleNamespace(reset=lambda: calls.append("reset")),
        )
        ShellApp._apply_inject(
            fake,
            {
                "ok": False,
                "pid": 19007,
                "error": "stale bridge",
                "fail_code": "STALE_BRIDGE_RESTART_GAME",
            },
        )
        self.assertIn(("mount", session), calls)
        self.assertNotIn("shutdown", calls)
        self.assertNotIn("destroy", calls)
        self.assertNotIn(("unmount", 19007), calls)

    def test_live_process_without_game_window_unloads_after_two_polls(self) -> None:
        unloaded: list[tuple[int, str]] = []
        session = SimpleNamespace(pid=19004, hwnd=20, title="game")
        fake = SimpleNamespace(
            _health_job="old",
            _closing=False,
            _session_invalid_counts={},
            store=SimpleNamespace(list=lambda: [session]),
            _reconcile_feature_windows=lambda: None,
            _session_game_window_alive=lambda _session: False,
            _log=lambda _message: None,
            _refresh_role_live=lambda: None,
            unload_feature_for_pid=lambda pid, reason="": unloaded.append(
                (pid, reason)
            ),
            after=lambda _delay, _callback: "next",
            _poll_mounted_game_health=lambda: None,
        )
        with patch("app.core.diag_log.process_alive", return_value=True):
            ShellApp._poll_mounted_game_health(fake)
            self.assertEqual(unloaded, [])
            ShellApp._poll_mounted_game_health(fake)
        self.assertEqual(unloaded, [(19004, "game_window_missing")])

    def test_destroyed_feature_page_detaches_session_store_listener(self) -> None:
        store = SessionStore()

        class Listener:
            def refresh_sessions(self) -> None:
                pass

        listener = Listener()
        store.on_change(listener.refresh_sessions)
        fake = SimpleNamespace(
            store=store,
            refresh_sessions=listener.refresh_sessions,
            _store_listener_attached=True,
        )
        FeaturePage._detach_store_listener(fake)
        self.assertFalse(fake._store_listener_attached)
        self.assertEqual(store._listeners, [])

    def test_inject_gate_rejects_ping_only_bridge_without_role(self) -> None:
        missing = SimpleNamespace(
            host_context=lambda **_kwargs: SimpleNamespace(
                ok=True, ret=0, error=None, note=""
            )
        )
        present = SimpleNamespace(
            host_context=lambda **_kwargs: SimpleNamespace(
                ok=True, ret=1, error=None, note=""
            )
        )
        self.assertFalse(_verify_bridge_host_context(missing)[0])
        self.assertTrue(_verify_bridge_host_context(present)[0])

    def test_missing_host_never_starts_role_memory_sampling(self) -> None:
        callbacks: list[object] = []

        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self) -> None:
                self.target()

        bridge = SimpleNamespace(
            host_snapshot=lambda **_kwargs: SimpleNamespace(ok=True, ret=0),
            close=lambda: None,
        )
        fake = SimpleNamespace(
            _closed=False,
            _header_live_job=None,
            _header_live_busy=False,
            _active_keys=[],
            session=SimpleNamespace(pid=19001, hwnd=20),
            winfo_exists=lambda: True,
            after=lambda _delay, callback: callbacks.append(callback),
        )
        with (
            patch("app.core.xajh_bridge.ensure_bridge", return_value=bridge),
            patch("app.ui.session_window.threading.Thread", ImmediateThread),
            patch("app.core.live_scene_hub.sample_host_live") as sample,
        ):
            SessionFeatureWindow._header_live_tick(fake)
        sample.assert_not_called()
        self.assertEqual(len(callbacks), 1)

    def test_live_host_poll_uses_atomic_snapshot_not_foreign_crt(self) -> None:
        callbacks: list[object] = []
        applied_samples: list[dict] = []

        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self) -> None:
                self.target()

        bridge = SimpleNamespace(
            host_snapshot=lambda **_kwargs: SimpleNamespace(
                ok=True,
                ret=1,
                mode=74,
                x=12.5,
                y=3.0,
                z=44.25,
            ),
            close=lambda: None,
        )
        fake = SimpleNamespace(
            _closed=False,
            _header_live_job=None,
            _header_live_busy=False,
            _active_keys=[],
            session=SimpleNamespace(
                pid=19002,
                hwnd=21,
                title="笑傲江湖OL - 华东一区 - 角色乙 [GUI]",
                original_title="",
            ),
            winfo_exists=lambda: True,
            after=lambda _delay, callback: callbacks.append(callback),
            _apply_header_live_result=lambda present, sample: applied_samples.append(
                {"host_present": present, **sample}
            ),
        )
        with (
            patch("app.core.xajh_bridge.ensure_bridge", return_value=bridge),
            patch("app.ui.session_window.threading.Thread", ImmediateThread),
            patch("app.core.live_scene_hub.sample_host_live") as sample,
        ):
            SessionFeatureWindow._header_live_tick(fake)
        sample.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(len(applied_samples), 1)
        applied = applied_samples[0]
        self.assertTrue(applied["host_present"])
        self.assertEqual(applied["scene_id"], 74)
        self.assertEqual(applied["pos"], (12.5, 3.0, 44.25))
        self.assertEqual(applied["role_name"], "角色乙")

    def test_missing_bridge_host_notifies_and_stops_header_poll(self) -> None:
        calls: list[tuple[int, str]] = []
        fake = SimpleNamespace(
            _closed=False,
            _business_invalid_notified=False,
            _on_business_invalid=lambda pid, reason: calls.append((pid, reason)),
            _stop_header_live=lambda: None,
            _log=lambda _message: None,
            session=SimpleNamespace(pid=19001),
            winfo_exists=lambda: True,
        )

        SessionFeatureWindow._apply_header_live_result(
            fake,
            False,
            {},
        )
        self.assertTrue(fake._business_invalid_notified)
        self.assertEqual(calls, [(19001, "role_instance_missing")])

    def test_bridge_error_keeps_last_caption_without_publishing_stale_sample(self) -> None:
        callbacks: list[object] = []
        fake = SimpleNamespace(
            _closed=False,
            _header_live_busy=True,
            _header_live_job=None,
            _HEADER_LIVE_MS=1000,
            _header_live_tick=lambda: None,
            winfo_exists=lambda: True,
            after=lambda delay, callback: callbacks.append((delay, callback)),
        )
        with patch.object(SessionFeatureWindow, "_apply_header_live_sample") as apply_sample:
            SessionFeatureWindow._apply_header_live_result(fake, None, {})
        apply_sample.assert_not_called()
        self.assertFalse(fake._header_live_busy)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0][0], 1000)

    def test_batch_failure_not_in_role_continues_queue(self) -> None:
        """未进角色/超时等终态失败必须记录并继续下一个实例，不堵死队列。"""
        logs: list[str] = []
        scheduled: list[tuple[int, object]] = []
        unmounted: list[int] = []

        class FakeStore:
            def unmount(self, pid: int) -> None:
                unmounted.append(pid)

        fake = SimpleNamespace(
            _closing=False,
            _inject_busy=False,
            _log=lambda m: logs.append(str(m)),
            _inject_current_source="一键登录",
            _inject_hide_after=True,
            _inject_batch_stats={"ok": 0, "fail": 0, "errors": []},
            _inject_batch_total=2,
            _inject_batch_index=1,
            _inject_queue=[],
            _inject_current_meta={"pid": 19010, "retried": False},
            _feature_wins={},
            _feature_win_alive=lambda _pid: None,
            store=FakeStore(),
            _del_watch=SimpleNamespace(reset=lambda: None),
            _shutdown_destroy_feature_window=lambda _win: None,
            _show_inject_message=None,
            _pump_inject_queue=lambda: None,
            after=lambda delay, *args: scheduled.append((delay, args)),
        )
        fake._record_inject_batch = MethodType(ShellApp._record_inject_batch, fake)
        ShellApp._apply_inject(
            fake,
            {
                "ok": False,
                "pid": 19010,
                "error": "游戏当前没有可用角色上下文，已取消挂载",
                "fail_code": "GAME_NOT_READY",
            },
        )
        self.assertEqual(fake._inject_batch_stats["fail"], 1)
        self.assertTrue(any("未进角色" in line for line in fake._inject_batch_stats["errors"]))
        self.assertTrue(any("失败[未进角色]" in line for line in logs))
        self.assertEqual(unmounted, [19010])
        self.assertTrue(any(delay == 50 for delay, _ in scheduled))

    def test_batch_timeout_failure_is_terminal_and_continues(self) -> None:
        """一键登录批量硬超时不应自动重试，避免单实例占用队列约 180s。"""
        logs: list[str] = []
        scheduled: list[tuple[int, object]] = []

        fake = SimpleNamespace(
            _closing=False,
            _inject_busy=False,
            _log=lambda m: logs.append(str(m)),
            _inject_current_source="一键登录",
            _inject_hide_after=True,
            _inject_batch_stats={"ok": 0, "fail": 0, "errors": []},
            _inject_batch_total=3,
            _inject_batch_index=2,
            _inject_queue=[],
            _inject_current_meta={"pid": 19011, "retried": False},
            _feature_wins={},
            _feature_win_alive=lambda _pid: None,
            store=SimpleNamespace(unmount=lambda _pid: None),
            _del_watch=SimpleNamespace(reset=lambda: None),
            _shutdown_destroy_feature_window=lambda _win: None,
            _show_inject_message=None,
            _pump_inject_queue=lambda: None,
            after=lambda delay, *args: scheduled.append((delay, args)),
        )
        fake._record_inject_batch = MethodType(ShellApp._record_inject_batch, fake)
        ShellApp._apply_inject(
            fake,
            {
                "ok": False,
                "pid": 19011,
                "error": "注入超时（后台桥接未在 90s 内就绪）",
                "fail_code": "INJECT_TIMEOUT",
            },
        )
        self.assertEqual(fake._inject_batch_stats["fail"], 1)
        self.assertTrue(any("注入超时" in line for line in fake._inject_batch_stats["errors"]))
        # no 1.2s retry reschedule, straight to next instance
        self.assertTrue(any(delay == 50 for delay, _ in scheduled))
        self.assertFalse(any(delay == 1200 for delay, _ in scheduled))


if __name__ == "__main__":
    unittest.main()
