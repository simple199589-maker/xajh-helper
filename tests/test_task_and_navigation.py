from __future__ import annotations

import inspect
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

from app.core.activity_auto import is_city_scene, is_dungeon_scene
from app.core.automove import AutomoveResult, PathTarget, host_move_to
from app.core.task_api import (
    TaskInfo,
    TaskOpResult,
    TaskRunner,
    accept_task,
    accept_task_routed,
    choose_task_route_clue,
    complete_task,
    find_nearby_task_npc,
    format_task_display_name,
    is_placeholder_npc_name,
    list_accepted_tasks_light,
    list_available_tasks,
    complete_task_routed,
    enrich_tasks_with_npc,
    pathfind_task,
    pathfind_to_clue,
    resolve_task_patterns,
    task_names_match,
    task_status_from_state,
)
from app.core.yaolu_auto import YaoluConfig, _loot_cfg, is_fuzhou_scene, is_yaolu_scene


class _NoBaseSession:
    pid = 1
    module_base = 0
    exe_path = ""


class TaskAndNavigationTests(unittest.TestCase):
    def test_one_click_slave_persists_cleared_team_control_before_rebind(self) -> None:
        from app.core.task_sync import ROLE_SLAVE
        from app.ui.pages._impl import SettingsPage

        page = SimpleNamespace(
            settings={},
            var_task_role=MagicMock(),
            var_role_label=MagicMock(),
            var_team_control_enabled=MagicMock(),
            _ROLE_LABEL={ROLE_SLAVE: "副控"},
            _current_role_id=MagicMock(return_value="target-role"),
            _update_team_control_status=MagicMock(),
            _apply_task_role_ui=MagicMock(),
            log=MagicMock(),
        )

        with patch("app.core.account_manager.save_role_control") as save_control:
            SettingsPage.apply_role_from_external(page, ROLE_SLAVE, team=False)

        self.assertFalse(page.settings["team_control_enabled"])
        page.var_team_control_enabled.set.assert_called_once_with(False)
        save_control.assert_called_once_with(
            "target-role", role=ROLE_SLAVE, team=False
        )
        page._apply_task_role_ui.assert_called_once_with(
            ROLE_SLAVE, register=True, apply_cloud=False
        )
    def test_role_picker_persists_master_before_task_rebind(self) -> None:
        from app.core.task_sync import ROLE_MASTER
        from app.ui.pages._impl import SettingsPage

        page = SimpleNamespace(
            settings={"team_control_enabled": False},
            var_task_role=MagicMock(),
            var_role_label=MagicMock(),
            _ROLE_LABEL={ROLE_MASTER: "主控"},
            _current_role_id=MagicMock(return_value="captain-role"),
            _apply_task_role_ui=MagicMock(),
            log=MagicMock(),
        )
        with patch("app.core.account_manager.save_role_control") as save_control:
            SettingsPage._on_task_role_pick(page, ROLE_MASTER)

        self.assertEqual(page.settings["task_control_role"], ROLE_MASTER)
        save_control.assert_called_once_with(
            "captain-role", role=ROLE_MASTER, team=False
        )
        page._apply_task_role_ui.assert_called_once_with(
            ROLE_MASTER, register=True, apply_cloud=False
        )

    def test_exclusive_save_demotes_only_other_master_windows(self) -> None:
        from app.core.task_sync import ROLE_MASTER, ROLE_NONE, ROLE_SLAVE
        from app.ui.pages._impl import SettingsPage

        master_page = SimpleNamespace(
            settings={"task_control_role": ROLE_MASTER}
        )
        master_win = SimpleNamespace(
            _pages={"settings": master_page},
            apply_external_task_role=MagicMock(return_value=True),
        )
        slave_page = SimpleNamespace(
            settings={"task_control_role": ROLE_SLAVE},
            apply_role_from_external=MagicMock(),
        )
        stale_page = SimpleNamespace(
            settings={"task_control_role": ROLE_MASTER},
            apply_role_from_external=MagicMock(),
        )
        stale_win = SimpleNamespace(_pages={"settings": stale_page})
        page = SimpleNamespace(
            _fixed_pid=100,
            _find_shell_feature_wins=lambda: {
                100: SimpleNamespace(),
                200: master_win,
                300: SimpleNamespace(_pages={"settings": slave_page}),
                400: stale_win,
            },
            log=MagicMock(),
        )

        SettingsPage._demote_other_masters(page)

        master_win.apply_external_task_role.assert_called_once_with(
            ROLE_NONE, team=None
        )
        slave_page.apply_role_from_external.assert_not_called()
        stale_page.apply_role_from_external.assert_called_once_with(
            ROLE_NONE, team=None
        )

    def test_hydrated_control_role_updates_save_variable(self) -> None:
        from app.core.task_sync import ROLE_MASTER
        from app.ui.pages._impl import SettingsPage

        page = SimpleNamespace(
            settings={},
            var_task_role=MagicMock(),
            var_role_label=MagicMock(),
            _ROLE_LABEL={ROLE_MASTER: "主控"},
            _fixed_pid=0,
            _update_one_click_slaves_btn=MagicMock(),
            _update_cloud_name_row=MagicMock(),
        )

        SettingsPage._apply_task_role_ui(page, ROLE_MASTER)

        page.var_task_role.set.assert_called_once_with(ROLE_MASTER)
        self.assertEqual(page.settings["task_control_role"], ROLE_MASTER)

    def test_inject_binding_refreshes_default_settings_page(self) -> None:
        from app.ui.app_shell import ShellApp

        source = inspect.getsource(ShellApp._drain_queue)
        self.assertIn('settings_page.on_page_show()', source)
        self.assertIn('注入后刷新快捷设置', source)

    def test_starting_jianglong_refreshes_current_role_control(self) -> None:
        from app.ui.pages._impl import SettingsPage

        source = inspect.getsource(SettingsPage._on_jianglong_test_toggle)

        self.assertIn("self._sync_current_role_control_for_jianglong()", source)
        self.assertLess(
            source.index("self._sync_current_role_control_for_jianglong()"),
            source.index("self._jianglong_test_enabled = not bool("),
        )

    def test_auto_open_monster_slave_skips_jianglong_without_reply(self) -> None:
        from app.ui.pages._impl import TaskPage

        page = SimpleNamespace(_fixed_pid=12345)
        self.assertFalse(TaskPage._skip_jianglong_for_auto_open(page))

        dispatch_source = inspect.getsource(TaskPage._handle_sync_event)
        team_source = inspect.getsource(TaskPage._run_team_misc_from_command)
        self.assertIn("if self._skip_jianglong_for_auto_open():", dispatch_source)
        self.assertIn("if self._skip_jianglong_for_auto_open():", team_source)

    def test_jianglong_slave_callback_does_not_inherit_master_test_switch(self) -> None:
        from app.ui.pages._impl import SettingsPage, TaskPage

        presence_source = inspect.getsource(SettingsPage._jianglong_presence)
        dispatch_source = inspect.getsource(TaskPage._dispatch_team_message)

        self.assertIn('getattr(self, "_jianglong_test_enabled", False)', presence_source)
        self.assertNotIn('self.settings.get("jianglong_test_enabled", False)', presence_source)
        self.assertIn("_jianglong_presence(", dispatch_source)
        self.assertIn("force_hang_refresh=True, ignore_scene=True", dispatch_source)
        self.assertNotIn("ignore_scene=bool(msg.get(\"test_mode\"))", dispatch_source)

    def test_jianglong_mode_uses_saved_session_state_after_save(self) -> None:
        from app.core.task_sync import ROLE_MASTER
        from app.ui.pages._impl import SettingsPage

        page = SimpleNamespace(
            settings={"team_control_enabled": False},
            var_team_control_enabled=MagicMock(),
            _control_role_shared=MagicMock(return_value=ROLE_MASTER),
        )
        page.var_team_control_enabled.get.return_value = False
        page._team_control_enabled_for_jianglong = (
            lambda: SettingsPage._team_control_enabled_for_jianglong(page)
        )

        self.assertEqual(SettingsPage._jianglong_control_mode(page), "local")
        page.var_team_control_enabled.get.return_value = True
        self.assertEqual(SettingsPage._jianglong_control_mode(page), "team")

    def test_business_panels_use_bound_role_context_for_role_config(self) -> None:
        from app.ui.pages._impl import ActivityPage, TaskPage

        activity_start = inspect.getsource(ActivityPage._on_start)
        task_start = inspect.getsource(TaskPage._on_start)
        custom_start = inspect.getsource(TaskPage._run_custom_routine_once)

        for source in (activity_start, task_start, custom_start):
            self.assertIn('getattr(', source)
            self.assertIn('"role_id"', source)
            self.assertIn('sync_role_prefs_to_settings(self.settings, role_id)', source)

    def test_team_checkbox_persists_immediately(self) -> None:
        from app.ui.pages._impl import SettingsPage

        source = inspect.getsource(SettingsPage._on_team_control_ui_changed)
        self.assertIn("self.settings[\"team_control_enabled\"] = team_enabled", source)
        self.assertIn("save_role_control(rid, role=role, team=team_enabled)", source)

    def test_save_persists_team_control_before_task_rebind(self) -> None:
        from app.ui.pages._impl import SettingsPage

        source = inspect.getsource(SettingsPage._on_save)
        self.assertLess(
            source.index("save_role_control("),
            source.index("self._apply_task_role_ui(role, register=True, apply_cloud=False)"),
        )

    def test_task_rebind_syncs_false_team_control_from_role_file(self) -> None:
        from app.ui.pages._impl import TaskPage

        source = inspect.getsource(TaskPage._bind_task_sync)

        self.assertIn('if "team" in ctl:', source)
        self.assertIn('self.settings["team_control_enabled"] = bool(ctl["team"])', source)
        self.assertNotIn('if cfg_team:', source)
    def test_jianglong_runner_does_not_restart_for_transport_change(self) -> None:
        from app.ui.pages._impl import SettingsPage

        source = inspect.getsource(SettingsPage._reconcile_jianglong_runner)

        self.assertNotIn("runner_mode != control_mode", source)
        self.assertIn("runner is None or not runner.is_running()", source)
    def test_team_follow_ui_is_the_only_experimental_and_formal_path(self) -> None:
        from app.ui.main_window import WorkbenchApp
        from app.ui.pages._impl import TaskPage

        lab_build = inspect.getsource(WorkbenchApp._build_tab_lab)
        lab_action = inspect.getsource(WorkbenchApp._lab_set_team_follow)
        formal_action = inspect.getsource(TaskPage._on_team_follow)
        gather_action = inspect.getsource(TaskPage._on_team_gather)

        self.assertNotIn("函数调用：", lab_build)
        self.assertNotIn("via_ui=False", lab_build)
        self.assertIn("click_team_follow_button", lab_action)
        self.assertNotIn("set_team_follow,", lab_action)
        self.assertIn("click_team_follow_button", formal_action)
        self.assertNotIn("set_team_follow(", formal_action)
        self.assertIn("click_team_follow_button", gather_action)
        self.assertNotIn("set_team_follow(", gather_action)

    def test_team_hang_does_not_reapply_mode_after_start(self) -> None:
        from app.ui.pages._impl import TaskPage

        captain_action = inspect.getsource(TaskPage._on_team_hang_sync)
        synced_action = inspect.getsource(TaskPage._run_hang_sync)

        # 统一管线契约：开/关挂机一律委托挂机设置页能力（SettingsPage.
        # apply_hang_switch → core apply_hang_switch 唯一管线，含丸子门控
        # 对账），本页不得再自备 start_hang/stop_hang 或 set_autoplay_mode。
        self.assertIn("apply_hang_switch(", captain_action)
        self.assertIn("apply_hang_switch(", synced_action)
        self.assertNotIn("start_hang(", captain_action)
        self.assertNotIn("start_hang(", synced_action)
        self.assertNotIn("stop_hang(", captain_action)
        self.assertNotIn("stop_hang(", synced_action)
        self.assertNotIn("set_autoplay_mode(", captain_action)
        self.assertNotIn("set_autoplay_mode(", synced_action)

    def test_portal_movement_lease_reasserts_and_releases_on_manual_input(self) -> None:
        from app.core.task_api import _PortalMovementLease
        from app.core.xajh_bridge import CMD_HOST_MOVE

        bridge = MagicMock()
        bridge.host_snapshot.return_value = SimpleNamespace(
            ok=True, ret=1, mode=68, error=None
        )
        bridge.call.return_value = SimpleNamespace(ok=True, error=None)
        lease = _PortalMovementLease(
            SimpleNamespace(pid=19001),
            hwnd=20,
            scene_id=68,
            target=(12.0, 3.0, 24.0),
            stop_event=threading.Event(),
            timeout_s=10.0,
        )
        with (
            patch("app.core.xajh_bridge.ensure_bridge", return_value=bridge),
            patch("app.core.task_api._portal_manual_input_active", return_value=False),
        ):
            self.assertTrue(lease.pulse("test", force=True))
        bridge.call.assert_called_once()
        self.assertEqual(bridge.call.call_args.args[0], CMD_HOST_MOVE)
        self.assertEqual(bridge.call.call_args.kwargs["x"], 12.0)
        self.assertEqual(lease.moves, 1)

        with patch("app.core.task_api._portal_manual_input_active", return_value=True):
            self.assertFalse(lease.pulse("manual", force=True))
        self.assertFalse(lease.active)
        self.assertEqual(lease.release_reason, "manual_input")

    def test_portal_movement_lease_releases_before_move_on_scene_change(self) -> None:
        from app.core.task_api import _PortalMovementLease

        bridge = MagicMock()
        bridge.host_snapshot.return_value = SimpleNamespace(
            ok=True, ret=1, mode=1524, error=None
        )
        lease = _PortalMovementLease(
            SimpleNamespace(pid=19002),
            hwnd=21,
            scene_id=68,
            target=(1.0, 2.0, 3.0),
            stop_event=None,
            timeout_s=10.0,
        )
        with (
            patch("app.core.xajh_bridge.ensure_bridge", return_value=bridge),
            patch("app.core.task_api._portal_manual_input_active", return_value=False),
        ):
            self.assertFalse(lease.pulse("scene", force=True))
        bridge.call.assert_not_called()
        self.assertEqual(lease.release_reason, "scene_changed")

    def test_portal_movement_lease_releases_on_cancel_timeout_and_bridge_errors(self) -> None:
        from app.core.task_api import _PortalMovementLease

        stop = threading.Event()
        stop.set()
        cancelled = _PortalMovementLease(
            SimpleNamespace(pid=1),
            hwnd=20,
            scene_id=68,
            target=(1.0, 2.0, 3.0),
            stop_event=stop,
            timeout_s=10.0,
        )
        self.assertFalse(cancelled.pulse(force=True))
        self.assertEqual(cancelled.release_reason, "cancelled")

        timed_out = _PortalMovementLease(
            SimpleNamespace(pid=2),
            hwnd=20,
            scene_id=68,
            target=(1.0, 2.0, 3.0),
            stop_event=None,
            timeout_s=10.0,
        )
        timed_out.deadline = 0.0
        self.assertFalse(timed_out.pulse(force=True))
        self.assertEqual(timed_out.release_reason, "timeout")

        broken = _PortalMovementLease(
            SimpleNamespace(pid=3),
            hwnd=20,
            scene_id=68,
            target=(1.0, 2.0, 3.0),
            stop_event=None,
            timeout_s=10.0,
        )
        with (
            patch("app.core.xajh_bridge.ensure_bridge", return_value=None),
            patch("app.core.task_api._portal_manual_input_active", return_value=False),
        ):
            for _ in range(3):
                broken.pulse("broken", force=True)
        self.assertFalse(broken.active)
        self.assertEqual(broken.release_reason, "bridge_error")

    def test_slave_portal_path_enables_guard_but_local_path_does_not(self) -> None:
        from app.core.npc_service_mem import portal_select_by_function
        from app.ui.pages._impl import TaskPage

        source = inspect.getsource(TaskPage._run_path)
        self.assertIn("from_sync or self._control_role() == ROLE_MASTER", source)
        self.assertIn("portal_move_guard=portal_guard", source)
        menu_source = inspect.getsource(portal_select_by_function)
        self.assertGreaterEqual(
            menu_source.count('release_movement("portal_option_clicked")'), 2
        )

    @patch("app.core.task_api._PortalMovementLease")
    @patch("app.core.npc_service_mem.portal_select_by_function")
    @patch(
        "app.core.task_api._open_task_npc_dialog",
        return_value="NPCSayHello ok=True ret=1",
    )
    @patch("app.core.task_api.pathfind_to_clue")
    @patch("app.core.task_api.list_nearby_npcs")
    @patch("app.core.automove.read_scene_position")
    def test_synced_portal_final_click_releases_movement_lease(
        self, scene, nearby, pathfind, _open, select, lease_cls
    ) -> None:
        from app.core.task_api import use_task_portal_npc

        scene.return_value = SimpleNamespace(ok=True, scene_id=68)
        nearby.return_value = [
            {
                "tid": 100220,
                "obj_id": 9,
                "x": 12.0,
                "y": 3.0,
                "z": 24.0,
                "dist": 1.0,
            }
        ]
        pathfind.return_value = {
            "ok": True,
            "last_distance": 0.4,
            "last_position": [12.0, 3.0, 24.0],
        }
        lease = lease_cls.return_value
        lease.to_dict.return_value = {
            "enabled": True,
            "active": False,
            "release_reason": "portal_option_clicked",
        }

        def selected(*_args, **kwargs):
            kwargs["movement_pulse"]("menu")
            kwargs["movement_release"]("portal_option_clicked")
            return {"ok": True, "after_scene": 2022, "note": "selected"}

        select.side_effect = selected
        result = use_task_portal_npc(
            SimpleNamespace(pid=19003),
            {
                "name": "地宫传送",
                "tid": 5001,
                "obj_id": 9,
                "dist": 1.0,
                "x": 12.0,
                "y": 3.0,
                "z": 24.0,
            },
            hwnd=20,
            portal_kind="dungeon_upper",
            movement_lock=True,
        )
        self.assertTrue(result["ok"])
        lease.release.assert_any_call("portal_option_clicked")
        self.assertEqual(
            result["movement_lock"]["release_reason"], "portal_option_clicked"
        )

    @patch("app.core.task_api.find_nearby_task_npc")
    @patch("app.core.task_api.list_nearby_npcs")
    @patch("app.core.task_api.read_task_npc_tids")
    def test_accepted_refresh_enrichment_can_skip_live_objects(
        self, meta, nearby_list, nearby_one
    ) -> None:
        meta.return_value = {"delv_tid": 91, "award_tid": 92}
        rows = enrich_tasks_with_npc(
            object(),
            [TaskInfo(7, "测试任务", can_finish=False)],
            live_scan=False,
        )
        self.assertEqual(rows[0]["npc_tid"], 91)
        self.assertEqual(rows[0]["delv_tid"], 91)
        nearby_list.assert_not_called()
        nearby_one.assert_not_called()

    @patch("app.core.task_api.get_task_name")
    @patch("app.core.task_api.task_can_finish", return_value=True)
    @patch("app.core.task_api.list_accepted_tasks")
    def test_light_refresh_can_recheck_finish_without_refetching_name(
        self, accepted, can_finish, get_name
    ) -> None:
        accepted.return_value = [TaskInfo(7, state=3, can_finish=False)]
        rows = list_accepted_tasks_light(
            object(),
            prev=[{"task_id": 7, "name": "测试任务", "state": 3, "can_finish": False}],
            refresh_can_finish=True,
        )
        self.assertTrue(rows[0].can_finish)
        self.assertEqual(rows[0].name, "测试任务")
        can_finish.assert_called_once()
        get_name.assert_not_called()

    @patch("app.core.remote_runtime.is_pid_scene_snapshot_stable", return_value=False)
    @patch(
        "app.core.remote_runtime.get_pid_scene_snapshot",
        return_value={"ready": True, "scene_id": 74},
    )
    @patch("app.core.automove.read_scene_position", side_effect=RuntimeError("settling"))
    def test_fly_snapshot_uses_scene_gate_during_transition(
        self, _read, gate, _stable
    ) -> None:
        from app.core.map_fly import _snapshot_scene_pos

        snap = _snapshot_scene_pos(SimpleNamespace(pid=16768), fresh=True)
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["scene_id"], 74)
        self.assertEqual(snap["source"], "scene_gate")
        self.assertTrue(snap["scene_settling"])
        gate.assert_called_once_with(16768, max_age_s=2.0)

    @patch("app.core.remote_runtime.is_pid_scene_snapshot_stable", return_value=False)
    @patch(
        "app.core.remote_runtime.get_pid_scene_snapshot",
        return_value={"ready": True, "scene_id": 74},
    )
    @patch(
        "app.core.automove.read_scene_position",
        return_value=SimpleNamespace(ok=False, scene_id=None, scene_pos=None),
    )
    def test_fly_snapshot_uses_scene_gate_when_live_read_returns_failed(
        self, _read, gate, _stable
    ) -> None:
        from app.core.map_fly import _snapshot_scene_pos

        snap = _snapshot_scene_pos(SimpleNamespace(pid=16768), fresh=True)
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["scene_id"], 74)
        self.assertEqual(snap["source"], "scene_gate")
        gate.assert_called_once_with(16768, max_age_s=2.0)

    def test_official_fly_skips_point_inspection(self) -> None:
        from app.core.map_fly import MapFlyResult, fly_official_preset

        session = SimpleNamespace(pid=16768)
        result = MapFlyResult(
            ok=True,
            action="fly_preset",
            message="ok",
            detail={"verified_move": True},
        )
        with patch("app.core.map_fly.is_transmit_flag_open", return_value=(True, 1)), patch(
            "app.core.map_fly.fly_to_preset", return_value=result
        ) as fly, patch("app.core.map_fly.mark_fly_cooldown", return_value=3.0):
            out = fly_official_preset(session, "shimen", respect_cooldown=False)

        self.assertTrue(out.ok)
        self.assertTrue(out.detail["verified_move"])
        self.assertFalse(fly.call_args.kwargs["inspect_points"])

    def test_preset_fly_stops_after_first_verified_packet(self) -> None:
        from app.core.map_fly import MapFlyResult, fly_to_preset

        before = {"ok": True, "scene_id": 68, "pos": [1.0, 0.0, 1.0]}
        after = {
            "ok": True,
            "scene_id": 74,
            "source": "scene_gate",
            "scene_settling": True,
        }
        sent = MapFlyResult(ok=True, action="packet", message="sent")
        with patch(
            "app.core.map_fly._snapshot_scene_pos", side_effect=[before, after]
        ), patch(
            "app.core.map_fly.send_transmit_packet", return_value=sent
        ) as send, patch(
            "app.core.map_fly.is_transmit_flag_open"
        ) as is_open, patch(
            "app.core.map_fly._note_fly_state_change"
        ):
            out = fly_to_preset(
                SimpleNamespace(pid=16768),
                "shimen",
                open_if_needed=False,
                prefer_packet=True,
                prefer_ui_click=True,
                inspect_points=False,
            )

        self.assertTrue(out.ok)
        self.assertTrue(out.detail["verified_move"])
        self.assertEqual(out.detail["packet_page"], 0xFF)
        send.assert_called_once()
        is_open.assert_not_called()

    def test_preset_single_send_does_not_try_pages_or_ui_fallback(self) -> None:
        from app.core.map_fly import MapFlyResult, fly_to_preset

        before = {"scene_id": 1, "x": 10.0, "y": 0.0, "z": 20.0}
        sent = MapFlyResult(ok=True, action="packet", message="sent")
        session = type("Session", (), {"pid": 7})()
        with patch(
            "app.core.map_fly._snapshot_scene_pos",
            return_value=before,
        ), patch(
            "app.core.map_fly.send_transmit_packet", return_value=sent
        ) as send, patch(
            "app.core.map_fly.is_transmit_flag_open"
        ) as shown:
            out = fly_to_preset(
                session,
                "fuzhou",
                open_if_needed=False,
                prefer_packet=True,
                prefer_ui_click=True,
                inspect_points=False,
                single_send=True,
            )
        self.assertTrue(out.ok)
        self.assertFalse(out.detail["verified_move"])
        send.assert_called_once()
        self.assertEqual(send.call_args.args[2], 0xFF)
        shown.assert_not_called()

    def test_route_choice_prefers_live_award_npc_for_complete(self) -> None:
        clues = [
            {"source": "reach_site", "x": 1, "z": 2, "tid": 8},
            {"source": "cfg_object", "x": 3, "z": 4, "tid": 99},
            {
                "source": "template_tid",
                "x": 5,
                "z": 6,
                "tid": 99,
                "ptr": 0x1000,
                "obj_id": 0x200000001,
            },
        ]
        chosen = choose_task_route_clue(clues, for_complete=True, npc_tid=99)
        self.assertEqual(chosen["ptr"], 0x1000)

    def test_route_choice_prefers_reach_site_for_incomplete(self) -> None:
        clues = [
            {"source": "cfg_object", "x": 3, "z": 4},
            {"source": "reach_site", "x": 1, "z": 2},
        ]
        chosen = choose_task_route_clue(clues, for_complete=False)
        self.assertEqual(chosen["source"], "reach_site")

    @patch("app.core.plg_interact.get_object_id64", return_value=0x200000001)
    @patch("app.core.plg_objects.list_class_objects")
    @patch("app.core.automove.read_scene_position")
    def test_nearby_task_npc_resolves_ptr_and_id64(self, scene, objects, _oid) -> None:
        scene.return_value = SimpleNamespace(ok=True, scene_pos=(0, 0, 0))
        objects.return_value = [
            SimpleNamespace(
                tid=99,
                ptr=0x1234,
                name="交付NPC",
                x=1.0,
                y=2.0,
                z=3.0,
                dist=4.0,
            )
        ]
        npc = find_nearby_task_npc(object(), 99)
        self.assertEqual(npc["ptr"], 0x1234)
        self.assertEqual(npc["obj_id"], 0x200000001)
        self.assertEqual(objects.call_args.kwargs["want_tid"], 99)

    @patch("app.core.task_api.complete_task")
    @patch("app.core.task_api.find_nearby_task_npc")
    @patch("app.core.task_api.read_task_npc_tids")
    def test_runner_delivery_passes_npc_identity(self, meta, nearby, complete) -> None:
        meta.return_value = {"award_tid": 99}
        nearby.return_value = {"ptr": 0x1234, "obj_id": 0x200000001}
        complete.return_value = TaskOpResult(True, "complete", 7)
        runner = TaskRunner(pid=1, hwnd=2)
        runner._session = object()
        self.assertTrue(runner._deliver(TaskInfo(7, "完成任务", can_finish=True)))
        kwargs = complete.call_args.kwargs
        self.assertEqual(kwargs["npc_id_lo"], 1)
        self.assertEqual(kwargs["npc_id_hi"], 2)
        self.assertEqual(kwargs["npc_ptr"], 0x1234)

    @patch("app.core.task_api.pathfind_to_clue")
    @patch("app.core.task_api.find_task_clue_targets")
    def test_runner_incomplete_only_routes(self, clues, pathfind) -> None:
        clues.return_value = [{"source": "reach_site", "x": 1, "y": 2, "z": 3}]
        pathfind.return_value = {"ok": True}
        runner = TaskRunner(pid=1)
        runner._session = object()
        self.assertTrue(runner._route_incomplete(TaskInfo(8, "进行中")))
        pathfind.assert_called_once()
        self.assertIs(pathfind.call_args.kwargs["stop_event"], runner._stop)

    @patch("app.core.automove.read_scene_position")
    @patch("app.core.automove.host_move_to")
    def test_task_path_success_requires_verified_arrival(self, move, scene) -> None:
        move.return_value = AutomoveResult(True, "HostMove", {}, ret=1)
        scene.side_effect = [
            AutomoveResult(True, "read", {}, scene_id=68, scene_pos=(20, 0, 20)),
            AutomoveResult(True, "read", {}, scene_id=68, scene_pos=(3, 0, 4)),
            AutomoveResult(True, "read", {}, scene_id=68, scene_pos=(2, 0, 3)),
        ]
        result = pathfind_to_clue(
            object(),
            {"x": 0, "y": 0, "z": 0, "scene_id": 68, "source": "reach_site"},
            use_bridge=False,
            allow_remote_fallback=True,
            arrive_radius=6,
            verify_timeout_s=1,
            poll_s=0.001,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["arrived"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["samples"], 3)

    @patch("app.core.automove.read_scene_position")
    @patch("app.core.automove.host_move_to")
    def test_task_path_command_is_not_success_without_arrival(self, move, scene) -> None:
        move.return_value = AutomoveResult(True, "HostMove", {}, ret=1)
        scene.return_value = AutomoveResult(
            True, "read", {}, scene_id=68, scene_pos=(100, 0, 100)
        )
        result = pathfind_to_clue(
            object(),
            {"x": 0, "y": 0, "z": 0, "scene_id": 68, "source": "reach_site"},
            use_bridge=False,
            allow_remote_fallback=True,
            verify_timeout_s=0.02,
            poll_s=0.005,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["command_ok"])
        self.assertFalse(result["arrived"])
        self.assertEqual(result["error"], "path verification timeout")

    @patch("app.core.automove.read_scene_position")
    @patch("app.core.automove.host_move_to")
    def test_task_path_abort_preempts_position_and_unstick(self, move, scene) -> None:
        move.return_value = AutomoveResult(True, "HostMove", {}, ret=1)
        unstick = MagicMock()

        result = pathfind_to_clue(
            object(),
            {"x": 0, "y": 0, "z": 0, "scene_id": 68, "source": "reach_site"},
            use_bridge=False,
            allow_remote_fallback=True,
            verify_timeout_s=1.0,
            poll_s=0.001,
            stuck_s=0.001,
            unstick=unstick,
            abort_check=lambda: "host_dead",
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["aborted"])
        self.assertEqual(result["abort_reason"], "host_dead")
        scene.assert_not_called()
        unstick.assert_not_called()

    @patch("app.core.automove.host_move_to")
    def test_task_path_stuck_timer_respects_position_gate(self, move) -> None:
        move.return_value = AutomoveResult(True, "HostMove", {}, ret=1)
        unstick = MagicMock()
        stuck_check = MagicMock(return_value=False)
        position_reader = MagicMock(
            return_value=AutomoveResult(
                True,
                "read",
                {},
                scene_id=1524,
                scene_pos=(-144.9, 67.8, -437.4),
            )
        )

        result = pathfind_to_clue(
            object(),
            {"x": -58.0, "y": 62.7, "z": -420.1, "scene_id": 1524},
            use_bridge=False,
            allow_remote_fallback=True,
            verify_timeout_s=0.03,
            poll_s=0.002,
            stuck_s=0.004,
            stuck_check=stuck_check,
            unstick=unstick,
            position_reader=position_reader,
        )

        self.assertFalse(result["arrived"])
        self.assertGreater(stuck_check.call_count, 1)
        unstick.assert_not_called()

    @patch("app.core.task_api._accept_remote")
    @patch("app.core.task_api.list_accepted_task_ids", return_value=[])
    def test_accept_does_not_use_remote_fallback_by_default(self, _tasks, remote) -> None:
        result = accept_task(object(), 123, use_bridge=False, wait_s=0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "task accept requires bridge")
        remote.assert_not_called()

    @patch("app.core.task_api._open_task_npc_dialog", return_value="dlg")
    @patch("app.core.xajh_bridge.ensure_bridge")
    @patch("app.core.task_api.list_accepted_task_ids")
    def test_accept_uses_real_bridge_object(self, ids, ensure_bridge, _dlg) -> None:
        state = {"n": 0}

        def _ids(*_a, **_k):
            state["n"] += 1
            return [] if state["n"] == 1 else [123]

        ids.side_effect = _ids
        bridge = ensure_bridge.return_value
        bridge.task_accept.return_value = SimpleNamespace(
            ok=True, ret=1, note="TASK_ACCEPT ok", error=None, status=2
        )
        session = SimpleNamespace(pid=9)
        result = accept_task(
            session,
            123,
            hwnd=10,
            wait_s=0.5,
            npc_id_lo=5,
            npc_id_hi=1,
        )
        self.assertTrue(result.ok, msg=f"err={result.error!r} note={result.note!r}")
        bridge.task_accept.assert_called_with(123, hwnd=10, timeout_ms=4000)
        self.assertGreaterEqual(bridge.task_accept.call_count, 1)
        _dlg.assert_called()

    @patch("app.core.task_api.accept_task")
    @patch("app.core.task_api.find_nearby_task_npc")
    @patch("app.core.task_api.read_task_npc_tids")
    @patch("app.core.task_api.list_accepted_task_ids", return_value=[])
    def test_routed_accept_near_npc_closes_with_bridge_diff(
        self, _tasks, meta, nearby, accept
    ) -> None:
        meta.return_value = {"delv_tid": 99}
        # obj_id = (hi<<32)|lo with hi=1, lo=5
        nearby.return_value = {"ptr": 0x1234, "obj_id": 0x100000005}
        accept.return_value = TaskOpResult(True, "accept", 123, [], [123])
        result = accept_task_routed(SimpleNamespace(pid=9), 123, hwnd=10)
        self.assertTrue(result.ok)
        accept.assert_called_once()
        self.assertEqual(accept.call_args.args[1], 123)
        self.assertFalse(accept.call_args.kwargs["allow_remote_fallback"])
        self.assertEqual(accept.call_args.kwargs["npc_id_lo"], 5)
        self.assertEqual(accept.call_args.kwargs["npc_id_hi"], 1)
        self.assertTrue(accept.call_args.kwargs["open_npc_dialog"])

    @patch("app.core.task_api.pathfind_to_clue")
    @patch("app.core.task_api.get_task_npc_world")
    @patch("app.core.task_api.find_nearby_task_npc")
    @patch("app.core.task_api.read_task_npc_tids")
    @patch("app.core.task_api.list_accepted_task_ids", return_value=[])
    def test_routed_accept_refuses_unverified_path(
        self, _tasks, meta, nearby, cfg, pathfind
    ) -> None:
        meta.return_value = {"delv_tid": 99}
        nearby.return_value = None
        cfg.return_value = {"ok": True, "x": 1, "y": 2, "z": 3, "scene_id": 68}
        pathfind.return_value = {
            "ok": False,
            "command_ok": True,
            "error": "path verification timeout",
        }
        result = accept_task_routed(SimpleNamespace(pid=9), 123)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "path verification timeout")

    def test_classify_task_portal_kind(self) -> None:
        from app.core.task_api import classify_task_portal_kind

        self.assertEqual(
            classify_task_portal_kind(
                {"name": "普通名称", "portal_kind": "dungeon_deep"}
            ),
            "dungeon_deep",
        )

        # 杀怪 → 地宫上层
        self.assertEqual(
            classify_task_portal_kind({"name": "每日杀怪", "npc_name": "宋瑶"}),
            "dungeon_upper",
        )
        # BOSS → 地宫深处
        self.assertEqual(
            classify_task_portal_kind({"name": "每日BOSS"}),
            "dungeon_deep",
        )
        self.assertEqual(
            classify_task_portal_kind({"name": "前往天下会", "npc_name": "宋瑶"}),
            "tianxiahui",
        )
        self.assertEqual(classify_task_portal_kind({"name": "风云起"}), "")

    def test_pick_portal_npc_prefers_title(self) -> None:
        from app.core.task_api import _pick_portal_npc

        rows = [
            {"name": "地宫传送【深处】", "dist": 1.0},
            {"name": "地宫传送【上层】", "dist": 2.0},
            {"name": "地宫传送【初级地宫】", "dist": 3.0},
        ]
        p = _pick_portal_npc(rows, "dungeon_upper")
        self.assertEqual(p["name"], "地宫传送【上层】")
        p2 = _pick_portal_npc(rows, "dungeon_deep")
        self.assertEqual(p2["name"], "地宫传送【深处】")

    @patch("app.core.task_api.pathfind_to_clue")
    @patch("app.core.task_api.read_task_npc_tids")
    @patch("app.core.task_api.find_task_clue_targets")
    def test_pathfind_task_chooses_embedded_route(self, clues, meta, pathfind) -> None:
        clues.return_value = [
            {"source": "reach_site", "x": 10, "y": 2, "z": 20, "scene_id": 68}
        ]
        meta.return_value = {"award_tid": 99}
        pathfind.return_value = {"ok": True, "arrived": True}
        result = pathfind_task(
            object(),
            {"task_id": 5364, "can_finish": True},
            prefer_portal=False,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["task_id"], 5364)
        pathfind.assert_called_once()

    @patch("app.core.task_api.task_can_finish")
    @patch("app.core.task_api.find_task_clue_targets")
    @patch("app.core.task_api.pathfind_to_clue")
    def test_synced_npc_path_uses_snapshot_without_live_object_scan(
        self, pathfind, clues, can_finish
    ) -> None:
        pathfind.return_value = {"ok": True, "arrived": True}

        result = pathfind_task(
            object(),
            {
                "task_id": 10010,
                "can_finish": False,
                "name": "<每日BOSS>龙傲天",
                "origin_scene_id": 68,
                "portal_tid": 100219,
                "portal_x": -22.29,
                "portal_y": 60.75,
                "portal_z": -85.58,
            },
            prefer_portal=False,
            sync_route_guard=True,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["sync_route_snapshot"])
        self.assertEqual(result["route"]["source"], "sync_route_snapshot")
        self.assertEqual(result["route"]["tid"], 100219)
        self.assertEqual(result["route"]["scene_id"], 68)
        pathfind.assert_called_once()
        clues.assert_not_called()
        can_finish.assert_not_called()

    @patch("app.core.task_api.find_task_clue_targets")
    def test_synced_npc_path_without_snapshot_fails_closed(self, clues) -> None:
        result = pathfind_task(
            object(),
            {"task_id": 10010, "name": "old sender"},
            prefer_portal=False,
            sync_route_guard=True,
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["safe_skip"])
        self.assertIn("缺少路线快照", result["error"])
        clues.assert_not_called()

    @patch("app.core.task_api.pathfind_to_clue")
    @patch("app.core.task_api.task_can_finish", return_value=True)
    @patch("app.core.task_api.read_task_npc_tids")
    @patch("app.core.task_api.find_task_clue_targets")
    def test_pathfind_task_refreshes_stale_turnin_phase(
        self, clues, meta, can_finish, pathfind
    ) -> None:
        clues.return_value = [
            {"source": "reach_site", "x": 10, "y": 2, "z": 20, "scene_id": 68},
            {"source": "cfg_object", "x": 30, "y": 2, "z": 40, "scene_id": 68, "tid": 99},
        ]
        meta.return_value = {"delv_tid": 88, "award_tid": 99}
        pathfind.return_value = {"ok": True, "arrived": True}

        result = pathfind_task(
            object(),
            {"task_id": 5364, "can_finish": False},
            prefer_portal=False,
        )

        can_finish.assert_called_once()
        self.assertEqual(pathfind.call_args.args[1]["source"], "cfg_object")
        self.assertEqual(result["route_phase"], "turnin")
        self.assertEqual(result["route"]["tid"], 99)
        self.assertEqual(len(result["route_candidates"]), 2)
        self.assertTrue(result["route_candidates"][1]["selected"])

    @patch("app.core.task_api.complete_task")
    @patch("app.core.task_api.find_nearby_task_npc")
    @patch("app.core.task_api.read_task_npc_tids")
    @patch("app.core.task_api.list_accepted_task_ids", return_value=[5364])
    def test_routed_complete_resolves_award_npc_identity(
        self, _ids, meta, nearby, complete
    ) -> None:
        meta.return_value = {"award_tid": 99}
        nearby.return_value = {"ptr": 0x1234, "obj_id": 0x200000001}
        complete.return_value = TaskOpResult(True, "complete", 5364)
        result = complete_task_routed(
            object(), {"task_id": 5364, "can_finish": True}, hwnd=10
        )
        self.assertTrue(result.ok)
        complete.assert_called_once_with(
            ANY,
            5364,
            npc_id_lo=1,
            npc_id_hi=2,
            npc_ptr=0x1234,
            hwnd=10,
            log=ANY,
            fast=False,
            wait_s=None,
        )

    @patch("app.core.task_api._open_task_npc_dialog", return_value="dlg")
    @patch("app.core.xajh_bridge.ensure_bridge")
    @patch("app.core.task_api.list_accepted_task_ids")
    def test_complete_uses_real_bridge_object(
        self, ids, ensure_bridge, _dlg
    ) -> None:
        ids.side_effect = [[123], []]
        bridge = ensure_bridge.return_value
        bridge.task_complete.return_value = SimpleNamespace(
            ok=True, ret=1, note="TASK_COMPLETE ok", error=None
        )
        session = SimpleNamespace(pid=9)
        result = complete_task(
            session,
            123,
            npc_id_lo=1,
            npc_id_hi=2,
            npc_ptr=3,
            hwnd=10,
            wait_s=0.5,
        )
        self.assertTrue(result.ok)
        bridge.task_complete.assert_called_once_with(
            123,
            npc_id_lo=1,
            npc_id_hi=2,
            npc_ptr=3,
            hwnd=10,
            timeout_ms=5000,
        )
        bridge.close.assert_called_once()
        _dlg.assert_called_once()

    def test_task_status_mapping(self) -> None:
        from app.core.task_api import TASK_STATE_FINISHED, TASK_STATE_SUCCESS

        self.assertEqual(task_status_from_state(0, 2), "进行中(2)")
        self.assertEqual(task_status_from_state(TASK_STATE_FINISHED), "已完成")
        both = TASK_STATE_FINISHED | TASK_STATE_SUCCESS
        self.assertEqual(task_status_from_state(both), "可交")
        self.assertEqual(task_status_from_state(0, can_finish=True), "可交")

    def test_format_task_display_name_strips_level_wrappers(self) -> None:
        from app.core.task_api import build_task_display_name

        self.assertEqual(
            format_task_display_name("[140]<每日杀怪>140每日杀怪", task_id=140),
            "140每日杀怪",
        )
        self.assertEqual(
            format_task_display_name("140 <每日副本任务>140副本任务", task_id=141),
            "140副本任务",
        )
        self.assertEqual(
            format_task_display_name(
                "[140]<每日杀怪>初级地宫杀怪", task_id=140
            ),
            "初级地宫杀怪",
        )
        # Panel form: <分类>具体名 (matches game task list).
        self.assertEqual(
            build_task_display_name(
                "每日杀怪",
                panel_title="初级地宫杀怪",
            ),
            "<每日杀怪>初级地宫杀怪",
        )
        self.assertEqual(
            build_task_display_name(
                "每日BOSS",
                panel_title="东方不败",
            ),
            "<每日BOSS>东方不败",
        )
        self.assertEqual(
            build_task_display_name(
                "每日副本任务",
                panel_title="140副本任务二",
            ),
            "<每日副本任务>140副本任务二",
        )
        self.assertEqual(
            build_task_display_name(
                "每日杀怪",
                panel_title="140每日杀怪",
            ),
            "<每日杀怪>140每日杀怪",
        )
        self.assertEqual(
            build_task_display_name(
                "每日杀怪",
                panel_title="初级地宫杀怪",
                level=140,
            ),
            "[140]<每日杀怪>初级地宫杀怪",
        )
        # Story text must NOT leak into list title.
        self.assertEqual(
            build_task_display_name(
                "每日杀怪",
                story="白傀儡在初级地宫二层泛滥成灾，请你去清除一波吧。",
                objective="击杀10000只白傀儡。",
            ),
            "每日杀怪",
        )
        self.assertEqual(format_task_display_name("", task_id=9), "任务9")
        self.assertTrue(is_placeholder_npc_name("tid63351"))
        self.assertTrue(is_placeholder_npc_name(""))
        self.assertFalse(is_placeholder_npc_name("宋瑶"))
        self.assertTrue(
            task_names_match(
                "[140]<每日杀怪>140每日杀怪",
                "每日杀怪-限1次",
            )
        )
        # Different daily bosses must NOT collapse into one family.
        self.assertFalse(
            task_names_match(
                "<每日BOSS>东方不败",
                "<每日BOSS>龙傲天",
            )
        )
        self.assertFalse(
            task_names_match(
                "<每日BOSS>东方不败",
                "<每日BOSS>余沧海",
            )
        )
        self.assertTrue(
            task_names_match(
                "<每日BOSS>东方不败",
                "[140]<每日BOSS>东方不败",
            )
        )
        # Different daily kill targets stay distinct.
        self.assertFalse(
            task_names_match(
                "<每日杀怪>初级地宫杀怪",
                "<每日杀怪>中级地宫杀怪",
            )
        )

    @patch("app.core.task_api._select_portal_npc_menu", return_value={"ok": False, "note": "menu mock"})
    @patch("app.core.task_api._open_task_npc_dialog", return_value="dlg")
    @patch("app.core.automove.read_scene_position")
    def test_use_task_portal_partial_is_not_success(self, scene, _dlg, _menu) -> None:
        from app.core.task_api import use_task_portal_npc

        scene.return_value = SimpleNamespace(ok=True, scene_id=68)
        result = use_task_portal_npc(
            object(),
            {
                "name": "地宫传送【初级地宫】",
                "obj_id": 9,
                "dist": 1.0,
                "tid": 5001,
            },
            portal_kind="dungeon_upper",
            wait_scene_s=0.2,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("partial"))
        self.assertIn("场景未变化", str(result.get("error") or ""))
        self.assertTrue(result.get("menu"))

    def test_portal_menu_keywords(self) -> None:
        from app.core.task_api import portal_menu_keywords

        up = portal_menu_keywords("dungeon_upper")
        deep = portal_menu_keywords("dungeon_deep")
        self.assertTrue(any("上层" in x for stage in up for x in stage))
        self.assertTrue(any("深处" in x for stage in deep for x in stage))

    @patch("app.core.task_api._select_portal_npc_menu")
    @patch("app.core.task_api._open_task_npc_dialog", return_value="dlg")
    @patch("app.core.automove.read_scene_position")
    def test_use_task_portal_menu_success(self, scene, _dlg, menu) -> None:
        from app.core.task_api import use_task_portal_npc

        # first reads before_scene=68; after menu reports change handled by menu.ok
        scene.return_value = SimpleNamespace(ok=True, scene_id=68)
        menu.return_value = {
            "ok": True,
            "note": "list slot 1 scene 68→120",
            "after_scene": 120,
            "clicked": ["Win_NPC.Lst_Main"],
            "notes": [],
        }
        result = use_task_portal_npc(
            object(),
            {
                "name": "地宫传送",
                "obj_id": 9,
                "dist": 1.0,
                "tid": 5001,
            },
            portal_kind="dungeon_deep",
            wait_scene_s=0.1,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("after_scene"), 120)
        self.assertEqual(result.get("portal_kind"), "dungeon_deep")
        menu.assert_called()

    def test_dungeon_packet_resolvers(self) -> None:
        from app.core.task_api import (
            DUNGEON_LAYER_PACKET_DEEP,
            DUNGEON_LAYER_PACKET_UPPER,
            dungeon_layer_packet_for,
            dungeon_talk_packet_for,
        )

        # by live NPC tid (authoritative)
        self.assertEqual(
            dungeon_talk_packet_for({"name": "野人峡谷", "tid": 0x01B7}).hex().upper(),
            "0C00DC07000000000001",
        )
        self.assertEqual(
            dungeon_talk_packet_for({"name": "?", "tid": 0x01B5}).hex().upper(),
            "0C00DA07000000000001",
        )
        self.assertEqual(
            dungeon_talk_packet_for({"name": "?", "tid": 0x01B4}).hex().upper(),
            "0C00D907000000000001",
        )
        self.assertEqual(
            dungeon_talk_packet_for({"name": "?", "tid": 0x01B6}).hex().upper(),
            "0C00DB07000000000001",
        )
        # by NPC service name
        self.assertEqual(
            dungeon_talk_packet_for({"name": "初级地宫传送"}).hex().upper(),
            "0C00DA07000000000001",
        )
        self.assertEqual(
            dungeon_talk_packet_for({"name": "中级地宫传送"}).hex().upper(),
            "0C00D907000000000001",
        )
        self.assertEqual(
            dungeon_talk_packet_for({"name": "高级地宫传送"}).hex().upper(),
            "0C00DB07000000000001",
        )
        self.assertEqual(
            dungeon_talk_packet_for({"name": "升级地宫传送"}).hex().upper(),
            "0C00DC07000000000001",
        )
        # 福州每日口 delv tid → 档位 → 封包
        self.assertEqual(
            dungeon_talk_packet_for({"name": "地宫传送", "tid": 100218}).hex().upper(),
            "0C00D907000000000001",
        )
        self.assertEqual(
            dungeon_talk_packet_for({"name": "地宫传送", "tid": 100219}).hex().upper(),
            "0C00DA07000000000001",
        )
        self.assertEqual(
            dungeon_talk_packet_for({"name": "地宫传送", "tid": 100220}).hex().upper(),
            "0C00DB07000000000001",
        )
        self.assertEqual(
            dungeon_talk_packet_for({"name": "地宫传送", "tid": 100221}).hex().upper(),
            "0C00DC07000000000001",
        )
        # 无入口封包 / 天下会
        self.assertIsNone(
            dungeon_talk_packet_for({"name": "地宫传送【初级地宫】", "tid": 5001})
        )
        self.assertIsNone(dungeon_talk_packet_for({"name": "宋瑶", "tid": 100300}))
        # layer packets
        self.assertEqual(
            dungeon_layer_packet_for("dungeon_upper"), DUNGEON_LAYER_PACKET_UPPER
        )
        self.assertEqual(
            dungeon_layer_packet_for("dungeon_deep"), DUNGEON_LAYER_PACKET_DEEP
        )
        self.assertEqual(
            dungeon_layer_packet_for("dungeon_lower"), DUNGEON_LAYER_PACKET_DEEP
        )
        self.assertIsNone(dungeon_layer_packet_for("tianxiahui"))

    def test_portal_candidates_accept_dungeon_entry_tids(self) -> None:
        from app.core.task_api import list_portal_npc_candidates

        cands = list_portal_npc_candidates(
            [
                {"name": "野人峡谷", "tid": 0x01B7, "obj_id": 1, "dist": 3.0},
                {"name": "无关NPC", "tid": 999, "obj_id": 2, "dist": 1.0},
            ],
            "dungeon_upper",
            tier="升级",
        )
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["tid"], 0x01B7)

    @patch("app.core.task_api._dungeon_packet_transfer")
    @patch("app.core.task_api.time.sleep")
    @patch("app.core.task_api.pathfind_to_clue")
    @patch("app.core.task_api.list_nearby_npcs")
    @patch("app.core.automove.read_scene_position")
    def test_use_task_portal_uses_packet_for_dungeon_entry_npc(
        self, scene, nearby, pathfind, sleep, transfer
    ) -> None:
        from app.core.task_api import use_task_portal_npc

        scene.return_value = SimpleNamespace(ok=True, scene_id=68)
        nearby.return_value = []
        pathfind.return_value = {
            "ok": True,
            "last_distance": 0.4,
            "last_position": [12.0, 3.0, 24.0],
        }
        transfer.return_value = {
            "ok": True,
            "after_scene": 2020,
            "note": "scene 68→2020 via packet 对话+层",
        }
        result = use_task_portal_npc(
            SimpleNamespace(pid=19006),
            {
                "name": "野人峡谷",
                "tid": 0x01B7,
                "obj_id": 9,
                "dist": 1.0,
                "x": 12.0,
                "y": 3.0,
                "z": 24.0,
            },
            portal_kind="dungeon_upper",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("after_scene"), 2020)
        transfer.assert_called_once()
        self.assertEqual(
            transfer.call_args.kwargs["talk_packet"].hex().upper(),
            "0C00DC07000000000001",
        )
        self.assertEqual(
            transfer.call_args.kwargs["layer_packet"].hex().upper(),
            "0E000500000000",
        )

    @patch("app.core.task_api._dungeon_packet_transfer")
    @patch("app.core.task_api.time.sleep")
    @patch("app.core.task_api.pathfind_to_clue")
    @patch("app.core.task_api.list_nearby_npcs")
    @patch("app.core.automove.read_scene_position")
    def test_use_task_portal_uses_packet_for_fuzhou_daily_portal(
        self, scene, nearby, pathfind, sleep, transfer
    ) -> None:
        from app.core.task_api import use_task_portal_npc

        # 福州每日口：tid 是 delv 任务 tid，obj_id 低 32 位是实时模板 0x01B5
        scene.return_value = SimpleNamespace(ok=True, scene_id=68)
        nearby.return_value = []
        pathfind.return_value = {
            "ok": True,
            "last_distance": 0.4,
            "last_position": [12.0, 3.0, 24.0],
        }
        transfer.return_value = {
            "ok": True,
            "after_scene": 2014,
            "note": "scene 68→2014 via packet 对话+层",
        }
        result = use_task_portal_npc(
            SimpleNamespace(pid=19009),
            {
                "name": "地宫传送",
                "tid": 100219,
                "obj_id": 0x01000000000001B5,
                "dist": 1.0,
                "x": 12.0,
                "y": 3.0,
                "z": 24.0,
            },
            portal_kind="dungeon_upper",
        )
        self.assertTrue(result["ok"])
        transfer.assert_called_once()
        self.assertEqual(
            transfer.call_args.kwargs["talk_packet"].hex().upper(),
            "0C00DA07000000000001",
        )

    @patch("app.core.task_api._open_task_npc_dialog")
    @patch("app.core.task_api._dungeon_packet_transfer")
    @patch("app.core.task_api.time.sleep")
    @patch("app.core.task_api.pathfind_to_clue")
    @patch("app.core.task_api.list_nearby_npcs")
    @patch("app.core.automove.read_scene_position")
    def test_use_task_portal_packet_failure_skips_memory_dialog(
        self, scene, nearby, pathfind, sleep, transfer, dlg
    ) -> None:
        from app.core.task_api import use_task_portal_npc

        scene.return_value = SimpleNamespace(ok=True, scene_id=68)
        nearby.return_value = []
        pathfind.return_value = {
            "ok": True,
            "last_distance": 0.4,
            "last_position": [12.0, 3.0, 24.0],
        }
        transfer.return_value = {
            "ok": False,
            "error": "已到达地宫NPC并发送 对话/层 封包，但场景未变化",
        }
        result = use_task_portal_npc(
            SimpleNamespace(pid=19007),
            {
                "name": "初级地宫传送",
                "tid": 0x01B5,
                "obj_id": 9,
                "dist": 1.0,
                "x": 12.0,
                "y": 3.0,
                "z": 24.0,
            },
            portal_kind="dungeon_deep",
        )
        self.assertFalse(result["ok"])
        dlg.assert_not_called()
        self.assertIn("场景未变化", str(result.get("error") or ""))

    @patch("app.core.automove.read_scene_position")
    def test_dungeon_packet_transfer_sends_talk_then_layer(self, scene) -> None:
        from app.core.task_api import (
            DUNGEON_LAYER_PACKET_UPPER,
            _dungeon_packet_transfer,
        )

        sent: list[bytes] = []

        def fake_send(pid: int, payload: bytes, **kwargs) -> int:
            sent.append(bytes(payload))
            return 1

        scene.return_value = SimpleNamespace(ok=True, scene_id=2020)
        with patch(
            "app.core.game_send.send_raw_packet", side_effect=fake_send
        ):
            result = _dungeon_packet_transfer(
                SimpleNamespace(pid=19008),
                open_packet=bytes.fromhex("0A00DC07000000000001"),
                talk_packet=bytes.fromhex("0C00DC07000000000001"),
                layer_packet=DUNGEON_LAYER_PACKET_UPPER,
                kind="dungeon_upper",
                before_scene=68,
                wait_scene_s=0.2,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("after_scene"), 2020)
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0].hex().upper(), "0A00DC07000000000001")
        self.assertEqual(sent[1].hex().upper(), "0C00DC07000000000001")
        self.assertEqual(sent[2].hex().upper(), "0E000500000000")

    def test_available_task_list_is_explicit_stub(self) -> None:
        logs: list[str] = []
        self.assertEqual(list_available_tasks(object(), log=logs.append), [])
        self.assertIn("not resolved", logs[0])

    @patch("app.core.task_api.list_accepted_task_ids", return_value=[10028])
    @patch("app.core.task_api.read_task_npc_tids")
    @patch("app.core.task_api._probe_delv_offers")
    @patch("app.core.task_api.list_accepted_tasks_light")
    @patch("app.core.task_api.list_nearby_npcs")
    def test_nearby_offer_does_not_mark_accepted_as_available(
        self, nearby, accepted_fn, probe, meta, _ids
    ) -> None:
        """已接任务不得再显示为可接。"""
        from app.core.task_api import list_nearby_offer_tasks

        nearby.return_value = [
            {
                "name": "宋瑶",
                "tid": 100300,
                "dist": 14.9,
                "ptr": 0xB1,
                "obj_id": 7,
                "x": 1,
                "y": 0,
                "z": 1,
            }
        ]
        accepted_fn.return_value = [
            TaskInfo(10028, "每日杀怪-限1次", can_finish=False, status_text="进行中")
        ]
        meta.return_value = {"delv_tid": 100300, "award_tid": 100300, "desc": 1}
        probe.return_value = [
            {
                "kind": "available",
                "task_id": 10028,
                "name": "每日杀怪-限1次",
                "status_text": "可接",
                "can_finish": False,
                "delv_tid": 100300,
                "award_tid": 100300,
                "source": "delv_scan",
                "role": "接",
            }
        ]
        rows = list_nearby_offer_tasks(
            object(), radius=15.0, probe_offers=True, max_scan=10
        )
        avail = [r for r in rows if r.get("kind") == "available"]
        self.assertEqual(avail, [])
        linked = [r for r in rows if r.get("kind") == "accepted_link"]
        self.assertTrue(linked)
        self.assertEqual(linked[0]["status_text"], "进行中")

    @patch("app.core.task_api.list_accepted_task_ids", return_value=[10028])
    @patch("app.core.task_api.read_task_npc_tids")
    @patch("app.core.task_api._probe_delv_offers")
    @patch("app.core.task_api.list_accepted_tasks_light")
    @patch("app.core.task_api.list_nearby_npcs")
    def test_nearby_offer_name_family_not_available(
        self, nearby, accepted_fn, probe, meta, _ids
    ) -> None:
        """宋瑶：探测到的可接名与已接同族时不得显示可接。"""
        from app.core.task_api import list_nearby_offer_tasks

        nearby.return_value = [
            {
                "name": "宋瑶",
                "tid": 100300,
                "dist": 13.3,
                "ptr": 0xB1,
                "obj_id": 7,
                "x": 1,
                "y": 0,
                "z": 1,
            }
        ]
        # Accepted id differs from probe id; names are same family.
        accepted_fn.return_value = [
            TaskInfo(10028, "每日杀怪", can_finish=False, status_text="进行中")
        ]
        meta.return_value = {"delv_tid": 100300, "award_tid": 100300, "desc": 1}
        probe.return_value = [
            {
                "kind": "available",
                "task_id": 10099,
                "name": "每日杀怪-限1次",
                "status_text": "可接",
                "can_finish": False,
                "delv_tid": 100300,
                "award_tid": 100300,
                "source": "delv_scan",
                "role": "接",
            }
        ]
        rows = list_nearby_offer_tasks(
            object(), radius=15.0, probe_offers=True, max_scan=10
        )
        avail = [r for r in rows if r.get("kind") == "available"]
        self.assertEqual(avail, [])
        linked = [r for r in rows if r.get("kind") == "accepted_link"]
        self.assertTrue(linked)
        self.assertEqual(linked[0]["task_id"], 10028)
        self.assertEqual(linked[0]["status_text"], "进行中")
        self.assertIn(linked[0].get("source"), ("accepted_name", "accepted_id"))

    @patch("app.core.task_api.list_accepted_task_ids", return_value=[10028])
    @patch("app.core.task_api.read_task_npc_tids")
    @patch("app.core.task_api._probe_delv_offers")
    @patch("app.core.task_api.list_accepted_tasks_light")
    @patch("app.core.task_api.list_nearby_npcs")
    def test_nearby_offer_dedupes_same_task_npc(
        self, nearby, accepted_fn, probe, meta, _ids
    ) -> None:
        """Same accepted task on same NPC should not emit duplicate rows."""
        from app.core.task_api import list_nearby_offer_tasks

        nearby.return_value = [
            {
                "name": "宋瑶",
                "tid": 100300,
                "dist": 5.0,
                "ptr": 0xB1,
                "obj_id": 7,
                "x": 1,
                "y": 0,
                "z": 1,
            }
        ]
        accepted_fn.return_value = [
            TaskInfo(
                10028,
                "[140]<每日杀怪>140每日杀怪",
                can_finish=False,
                status_text="进行中",
            )
        ]
        meta.return_value = {"delv_tid": 100300, "award_tid": 100300, "desc": 1}
        probe.return_value = [
            {
                "kind": "available",
                "task_id": 10028,
                "name": "每日杀怪-限1次",
                "status_text": "可接",
                "can_finish": False,
                "delv_tid": 100300,
                "award_tid": 100300,
                "source": "delv_scan",
                "role": "接",
            },
            {
                "kind": "available",
                "task_id": 10028,
                "name": "每日杀怪",
                "status_text": "可接",
                "can_finish": False,
                "delv_tid": 100300,
                "award_tid": 100300,
                "source": "delv_scan",
                "role": "接",
            },
        ]
        rows = list_nearby_offer_tasks(
            object(), radius=15.0, probe_offers=True, max_scan=10
        )
        linked = [
            r
            for r in rows
            if r.get("kind") == "accepted_link" and int(r.get("task_id") or 0) == 10028
        ]
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0].get("name"), "140每日杀怪")
        self.assertEqual(linked[0].get("status_text"), "进行中")

    @patch("app.core.task_api.list_accepted_task_ids", return_value=[])
    @patch("app.core.task_api._probe_delv_offers")
    @patch("app.core.task_api.list_accepted_tasks_light", return_value=[])
    @patch("app.core.task_api.list_nearby_npcs")
    def test_nearby_offer_links_delv_scan(
        self, nearby, _accepted, probe, _ids
    ) -> None:
        from app.core.task_api import list_nearby_offer_tasks

        nearby.return_value = [
            {
                "kind": "npc",
                "name": "不败姑娘",
                "tid": 100279,
                "dist": 5.0,
                "ptr": 0x1000,
                "obj_id": 9,
                "x": 1.0,
                "y": 2.0,
                "z": 3.0,
            }
        ]
        probe.return_value = [
            {
                "kind": "available",
                "task_id": 10021,
                "name": "测试任务",
                "status_text": "可接",
                "can_finish": False,
                "delv_tid": 100279,
                "award_tid": 100279,
                "source": "delv_scan",
                "role": "接",
            }
        ]
        rows = list_nearby_offer_tasks(
            object(),
            radius=120.0,
            id_scan=(10020, 10022),
            max_scan=10,
            probe_offers=True,
        )
        offers = [r for r in rows if r.get("kind") == "available"]
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["task_id"], 10021)
        self.assertEqual(offers[0]["npc_tid"], 100279)
        self.assertEqual(offers[0]["status_text"], "可接")

    @patch("app.core.task_api.list_accepted_task_ids", return_value=[])
    @patch("app.core.task_api._probe_delv_offers")
    @patch("app.core.task_api.list_accepted_tasks_light", return_value=[])
    @patch("app.core.task_api.list_nearby_npcs")
    def test_nearby_offer_keeps_same_tid_instances(
        self, nearby, _accepted, probe, _ids
    ) -> None:
        """地宫传送等：同名不同实例各自占一行，多任务不合并。"""
        from app.core.task_api import list_nearby_offer_tasks

        nearby.return_value = [
            {
                "name": "地宫传送【初级地宫】",
                "tid": 5001,
                "dist": 3.0,
                "ptr": 0xA1,
                "obj_id": 1,
                "x": 1,
                "y": 0,
                "z": 1,
            },
            {
                "name": "地宫传送【中级地宫】",
                "tid": 5002,
                "dist": 4.0,
                "ptr": 0xA2,
                "obj_id": 2,
                "x": 2,
                "y": 0,
                "z": 2,
            },
            {
                "name": "地宫传送【高级地宫】",
                "tid": 5003,
                "dist": 5.0,
                "ptr": 0xA3,
                "obj_id": 3,
                "x": 3,
                "y": 0,
                "z": 3,
            },
            {
                "name": "地宫传送【升级地宫】",
                "tid": 5004,
                "dist": 6.0,
                "ptr": 0xA4,
                "obj_id": 4,
                "x": 4,
                "y": 0,
                "z": 4,
            },
        ]
        probe.return_value = [
            {
                "kind": "available",
                "task_id": 9001,
                "name": "任务9001",
                "status_text": "可接",
                "can_finish": False,
                "delv_tid": 5001,
                "award_tid": 5001,
                "source": "delv_scan",
                "role": "接",
            },
            {
                "kind": "available",
                "task_id": 9002,
                "name": "任务9002",
                "status_text": "可接",
                "can_finish": False,
                "delv_tid": 5001,
                "award_tid": 5001,
                "source": "delv_scan",
                "role": "接",
            },
            {
                "kind": "available",
                "task_id": 9003,
                "name": "任务9003",
                "status_text": "可接",
                "can_finish": False,
                "delv_tid": 5002,
                "award_tid": 5002,
                "source": "delv_scan",
                "role": "接",
            },
        ]
        rows = list_nearby_offer_tasks(
            object(),
            radius=120.0,
            id_scan=(9001, 9003),
            max_scan=20,
            probe_offers=True,
        )
        # 9001+9002 on 初级, 9003 on 中级, 高级/升级 as npc_only
        # dedupe keeps one row per (task_id, npc_tid) → 3 offers + 2 npc_only
        self.assertEqual(len(rows), 5)
        names = [r.get("npc_name") for r in rows]
        self.assertEqual(names.count("地宫传送【初级地宫】"), 2)
        self.assertEqual(names.count("地宫传送【中级地宫】"), 1)
        self.assertEqual(names.count("地宫传送【高级地宫】"), 1)
        self.assertEqual(names.count("地宫传送【升级地宫】"), 1)
        offers = [r for r in rows if r.get("kind") == "available"]
        self.assertEqual(len(offers), 3)
        self.assertEqual({r["task_id"] for r in offers}, {9001, 9002, 9003})

    @patch("app.core.task_api.list_accepted_task_ids", return_value=[10028])
    @patch("app.core.task_api.read_task_npc_tids")
    @patch("app.core.task_api._probe_delv_offers", return_value=[])
    @patch("app.core.task_api.list_accepted_tasks_light")
    @patch("app.core.task_api.list_nearby_npcs")
    def test_nearby_offer_dedupes_same_task_npc(
        self, nearby, accepted_fn, _probe, meta, _ids
    ) -> None:
        """Same accepted task linked via delv+award should appear once per NPC."""
        from app.core.task_api import list_nearby_offer_tasks

        nearby.return_value = [
            {
                "name": "宋瑶",
                "tid": 100300,
                "dist": 5.0,
                "ptr": 0xB1,
                "obj_id": 7,
                "x": 1,
                "y": 0,
                "z": 1,
            }
        ]
        accepted_fn.return_value = [
            TaskInfo(10028, "每日杀怪", can_finish=False, status_text="进行中")
        ]
        # Same tid as both delv and award → would produce 2 rows without dedupe.
        meta.return_value = {"delv_tid": 100300, "award_tid": 100300, "desc": 1}
        rows = list_nearby_offer_tasks(
            object(), radius=15.0, probe_offers=False, max_scan=5
        )
        linked = [r for r in rows if r.get("kind") == "accepted_link"]
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["task_id"], 10028)
        self.assertEqual(linked[0]["npc_name"], "宋瑶")
        self.assertEqual(
            format_task_display_name("140 <每日杀怪>140每日杀怪", task_id=10028),
            "140每日杀怪",
        )

    def test_format_task_display_name(self) -> None:
        self.assertEqual(
            format_task_display_name("[140]<每日杀怪>140每日杀怪"), "140每日杀怪"
        )
        self.assertEqual(format_task_display_name("每日副本任务"), "每日副本任务")
        self.assertEqual(format_task_display_name("", task_id=7), "任务7")

    def test_current_pe_task_patterns_when_client_exists(self) -> None:
        path = Path(r"D:\WeGameApps\笑傲江湖OL\bin\xajh.exe")
        if not path.is_file():
            self.skipTest("xajh.exe is not installed")
        result = resolve_task_patterns(path)
        self.assertEqual(result.get("pe"), str(path))
        self.assertIn("ok", result)
        self.assertIn("errors", result)

    def test_path_models_serialize(self) -> None:
        target = PathTarget(1, 2, 3, mode=4, map_hint="x62")
        self.assertEqual(target.to_dict()["mode"], 4)
        result = AutomoveResult(True, "read", {}, scene_pos=(1, 2, 3))
        self.assertEqual(result.to_dict()["scene_pos"], [1, 2, 3])

    def test_move_without_module_base_is_clean_failure(self) -> None:
        out = host_move_to(_NoBaseSession(), PathTarget(1, 2, 3))
        self.assertFalse(out.ok)
        self.assertIn("module_base", out.error)

    def test_activity_scene_gates(self) -> None:
        self.assertTrue(is_city_scene(68, None, "fuzhou"))
        self.assertFalse(is_city_scene(68, None, "luoyang"))
        self.assertFalse(is_dungeon_scene(68, None, gate="fuzhou"))
        self.assertTrue(is_dungeon_scene(1529, "妖楼", gate="fuzhou"))
        self.assertFalse(is_dungeon_scene(None, None, gate="any_city"))

    def test_yaolu_scene_detection(self) -> None:
        self.assertTrue(is_fuzhou_scene(68))
        self.assertTrue(is_yaolu_scene(1529))
        self.assertTrue(is_yaolu_scene(None, "a57"))
        self.assertFalse(is_yaolu_scene(None, "福州"))

    def test_yaolu_loot_config_preserves_safe_contract(self) -> None:
        cfg = _loot_cfg(YaoluConfig(entry_name="入口", pick_range=7, use_bridge=False))
        self.assertEqual(cfg.item_name, "入口")
        self.assertEqual(cfg.tid, 81184)
        self.assertTrue(cfg.match_tid_fallback)
        self.assertEqual(cfg.pick_range, 7)
        self.assertEqual(cfg.interact_mode, "open")
        # Captcha dialog is the open proof; cast-this/host+0x41C often stay 0.
        self.assertFalse(cfg.confirm_cast_start_only)
        self.assertEqual(cfg.cast_start_grace_s, 0.0)
        self.assertEqual(cfg.cast_wait_s, 0)
        self.assertFalse(cfg.use_bridge)



    @patch("app.core.task_api.find_task_clue_targets")
    @patch("app.core.task_api.list_nearby_npcs")
    @patch("app.core.task_api.use_task_portal_npc")
    def test_pathfind_portal_menu_fail_no_blind_walk(
        self, portal_fn, nearby, clues
    ) -> None:
        """Portal delv miss must not fake-success or blind-walk nearby NPCs."""
        nearby.return_value = [
            {"name": "地宫传送", "tid": 100221, "x": 1, "z": 2, "dist": 3.0, "obj_id": 9}
        ]
        portal_fn.return_value = {
            "ok": False,
            "method": "portal_npc",
            "partial": True,
            "error": "menu fail",
        }
        clues.return_value = []
        result = pathfind_task(
            object(),
            {
                "task_id": 1,
                "name": "每日BOSS",
                "can_finish": False,
                "delv_tid": 100221,
            },
            prefer_portal=True,
            portal_move_guard=True,
        )
        self.assertFalse(result.get("ok"))
        # Portal delv tasks refuse blind city walk fallback.
        clues.assert_not_called()
        portal_fn.assert_called()
        self.assertTrue(portal_fn.call_args.kwargs["movement_lock"])

    @patch("app.core.task_api.use_task_portal_npc")
    @patch("app.core.task_api.list_nearby_npcs")
    @patch("app.core.task_api._portal_scene_generation_state")
    def test_synced_portal_already_in_target_skips_old_npc_path(
        self, generation, nearby, portal_fn
    ) -> None:
        generation.return_value = {
            "ok": True,
            "role_present": True,
            "scene_id": 2036,
            "status": "already_in_target_scene",
        }
        result = pathfind_task(
            object(),
            {
                "task_id": 10010,
                "name": "<每日BOSS>高级地宫BOSS",
                "can_finish": False,
                "origin_scene_id": 68,
                "portal_tid": 100220,
                "portal_obj_id": 9,
                "portal_x": 1.0,
                "portal_y": 2.0,
                "portal_z": 3.0,
            },
            prefer_portal=True,
            portal_move_guard=True,
            portal_sync_guard=True,
            portal_origin_scene_id=68,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_in_target_scene"])
        nearby.assert_not_called()
        portal_fn.assert_not_called()

    @patch("app.core.task_api.use_task_portal_npc")
    @patch("app.core.task_api.list_nearby_npcs")
    @patch("app.core.task_api._portal_scene_generation_state")
    def test_synced_portal_stale_or_transition_never_scans_npcs(
        self, generation, nearby, portal_fn
    ) -> None:
        base = {
            "task_id": 10010,
            "name": "<每日BOSS>高级地宫BOSS",
            "can_finish": False,
            "origin_scene_id": 68,
            "portal_tid": 100220,
            "portal_obj_id": 9,
            "portal_x": 1.0,
            "portal_y": 2.0,
            "portal_z": 3.0,
        }
        for status, scene_id in (
            ("stale_scene_generation", 777),
            ("scene_transition", 0),
        ):
            generation.return_value = {
                "ok": True,
                "role_present": True,
                "scene_id": scene_id,
                "status": status,
            }
            result = pathfind_task(
                object(),
                base,
                prefer_portal=True,
                portal_sync_guard=True,
                portal_origin_scene_id=68,
            )
            self.assertFalse(result["ok"])
            self.assertTrue(result["safe_skip"])
            self.assertEqual(result["generation"]["status"], status)
        nearby.assert_not_called()
        portal_fn.assert_not_called()

    @patch("app.core.task_api.use_task_portal_npc")
    @patch("app.core.task_api.list_nearby_npcs")
    @patch("app.core.task_api._portal_scene_generation_state")
    def test_synced_portal_uses_published_hint_without_template_scan(
        self, generation, nearby, portal_fn
    ) -> None:
        generation.return_value = {
            "ok": True,
            "role_present": True,
            "scene_id": 68,
            "status": "current",
        }
        portal_fn.return_value = {
            "ok": True,
            "method": "portal_npc",
            "before_scene": 68,
            "after_scene": 2036,
            "obj_id": 9,
            "portal_tid": 100220,
        }
        result = pathfind_task(
            object(),
            {
                "task_id": 10010,
                "name": "<每日BOSS>高级地宫BOSS",
                "can_finish": False,
                "origin_scene_id": 68,
                "portal_tid": 100220,
                "portal_obj_id": 9,
                "portal_x": 11.0,
                "portal_y": 2.0,
                "portal_z": 22.0,
            },
            prefer_portal=True,
            portal_move_guard=True,
            portal_sync_guard=True,
            portal_origin_scene_id=68,
        )
        self.assertTrue(result["ok"])
        nearby.assert_not_called()
        portal_fn.assert_called_once()
        self.assertFalse(portal_fn.call_args.kwargs["allow_live_rebind"])
        self.assertEqual(portal_fn.call_args.kwargs["expected_origin_scene_id"], 68)

    @patch("app.core.task_api.use_task_portal_npc")
    @patch("app.core.task_api.read_task_npc_tids")
    @patch("app.core.task_api.list_nearby_npcs")
    @patch("app.core.task_api._portal_scene_generation_state")
    def test_legacy_synced_portal_without_hint_fails_closed(
        self, generation, nearby, read_tids, portal_fn
    ) -> None:
        generation.return_value = {
            "ok": True,
            "role_present": True,
            "scene_id": 68,
            "status": "current",
        }
        result = pathfind_task(
            object(),
            {
                "task_id": 10010,
                "name": "<每日BOSS>高级地宫BOSS",
                "can_finish": False,
                "origin_scene_id": 68,
            },
            prefer_portal=True,
            portal_sync_guard=True,
            portal_origin_scene_id=68,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["safe_skip"])
        nearby.assert_not_called()
        read_tids.assert_not_called()
        portal_fn.assert_not_called()

    def test_task_page_drops_queued_paths_from_old_scene_generation(self) -> None:
        from app.core.task_sync import ACTION_ACCEPT, ACTION_PATH, TaskSyncEvent
        from app.ui.pages._impl import TaskPage

        page = TaskPage.__new__(TaskPage)
        page.log = MagicMock()
        page._pending_task_sync = [
            TaskSyncEvent(ACTION_PATH, 1, 10, origin_scene_id=68),
            TaskSyncEvent(ACTION_ACCEPT, 2, 10),
            TaskSyncEvent(ACTION_PATH, 3, 10, origin_scene_id=99),
        ]
        page._drop_stale_task_paths(68, 2036)
        self.assertEqual(
            [event.task_id for event in page._pending_task_sync],
            [2, 3],
        )
        page.log.assert_called_once()


    def test_portal_texts_usable_rejects_garbled(self) -> None:
        from app.core.task_api import _dest_slot_for_kind, _portal_texts_usable

        self.assertFalse(_portal_texts_usable(["眘ᦐ", "褠在", "歱䇼歱"]))
        self.assertTrue(_portal_texts_usable(["地宫上层", "地宫深处"]))
        self.assertEqual(_dest_slot_for_kind("dungeon_upper"), 0)
        self.assertEqual(_dest_slot_for_kind("dungeon_deep"), 1)

    @patch("app.core.task_api._click_list_slots")
    @patch("app.core.task_api._resolve_ctrl_rect")
    @patch("app.core.task_api._scan_dlg_option_texts")
    @patch("app.core.task_api._portal_dlg_shown")
    @patch("app.core.task_api._scene_id_now", return_value=68)
    def test_select_portal_menu_no_spray_when_garbled(
        self, _scene, dlg_shown, scan, resolve, click_list
    ) -> None:
        """Garbled texts: one dest slot only, never multi-row fill."""
        from types import SimpleNamespace
        from app.core.task_api import _select_portal_npc_menu

        dlg_shown.return_value = [("Win_NPC", 0x1000)]
        scan.return_value = ["眘ᦐ", "褠在", "歱䇼"]
        resolve.return_value = SimpleNamespace(x=100, y=200, w=80, h=120, ok=True)
        click_list.return_value = (False, "list slots clicked [0]")
        out = _select_portal_npc_menu(
            object(),
            portal_kind="dungeon_upper",
            before_scene=68,
            max_rounds=1,
        )
        self.assertFalse(out.get("ok"))
        self.assertTrue(click_list.called)
        kwargs = click_list.call_args.kwargs
        self.assertEqual(kwargs.get("prefer_indices"), [0])
        self.assertEqual(kwargs.get("max_clicks"), 1)
        self.assertFalse(kwargs.get("fill_remaining"))

    @patch("app.core.task_api._click_list_slots")
    @patch("app.core.task_api._resolve_ctrl_rect")
    @patch("app.core.task_api._scan_dlg_option_texts")
    @patch("app.core.task_api._portal_dlg_shown")
    @patch("app.core.task_api._scene_id_now", return_value=68)
    def test_select_portal_menu_deep_uses_slot1(
        self, _scene, dlg_shown, scan, resolve, click_list
    ) -> None:
        from types import SimpleNamespace
        from app.core.task_api import _select_portal_npc_menu

        dlg_shown.return_value = [("Win_NPC", 0x1000)]
        scan.return_value = ["乱码"]
        resolve.return_value = SimpleNamespace(x=100, y=200, w=80, h=120, ok=True)
        click_list.return_value = (False, "list slots clicked [1]")
        # Blind deep: only dest slot1 — never entry slot0 (avoids 上层 mis-warp)
        _select_portal_npc_menu(
            object(),
            portal_kind="dungeon_deep",
            before_scene=68,
            max_rounds=2,
        )
        self.assertTrue(click_list.called)
        prefs = [
            c.kwargs.get("prefer_indices")
            for c in click_list.call_args_list
            if c.kwargs
        ]
        self.assertTrue(all(p == [1] for p in prefs if p is not None), prefs)
        self.assertNotIn([0], prefs)



    def test_portal_strings_from_blob_utf16(self) -> None:
        from app.core.task_api import (
            _portal_dest_slot_from_texts,
            _portal_option_lines,
            _portal_strings_from_blob,
            _portal_texts_usable,
        )

        # synthetic dialog blob with two option strings as UTF-16LE
        a = "初级地宫上层".encode("utf-16le")
        b = "初级地宫深处".encode("utf-16le")
        pad = b"\x00" * 16
        blob = pad + a + b"\x00\x00" + pad + b + b"\x00\x00" + pad
        hits = _portal_strings_from_blob(blob)
        self.assertTrue(_portal_texts_usable(hits), hits)
        joined = " ".join(hits)
        self.assertIn("上层", joined)
        self.assertIn("深处", joined)
        opts = _portal_option_lines(hits)
        self.assertTrue(any("上层" in x for x in opts), opts)
        self.assertTrue(any("深处" in x for x in opts), opts)
        deep_slot = _portal_dest_slot_from_texts(hits, "dungeon_deep")
        up_slot = _portal_dest_slot_from_texts(hits, "dungeon_upper")
        self.assertIsNotNone(deep_slot)
        self.assertIsNotNone(up_slot)
        self.assertNotEqual(deep_slot, up_slot)



    def test_list_portal_prefer_tid_only(self) -> None:
        from app.core.task_api import list_portal_npc_candidates

        rows = [
            {"name": "地宫传送", "tid": 100218, "dist": 7.8, "obj_id": 1},
            {"name": "地宫传送", "tid": 100221, "dist": 1.1, "obj_id": 2},
            {"name": "地宫传送", "tid": 100220, "dist": 1.8, "obj_id": 3},
            {"name": "地宫传送", "tid": 100219, "dist": 4.8, "obj_id": 4},
        ]
        cands = list_portal_npc_candidates(
            rows, "dungeon_upper", tier="中级", prefer_tid=100218, max_n=5
        )
        self.assertEqual(len(cands), 1, cands)
        self.assertEqual(int(cands[0]["tid"]), 100218)
        self.assertEqual(int(cands[0]["_portal_rank"]), -100)

    def test_portal_option_lines_reject_story(self) -> None:
        from app.core.task_api import _portal_option_lines

        opts = _portal_option_lines(
            [
                "野人峡谷上层",
                "野人峡谷深处",
                "传送",
                "这是一段很长的剧情说明，包含标点。",
                "中级",
            ]
        )
        self.assertIn("野人峡谷上层", opts)
        self.assertIn("野人峡谷深处", opts)
        self.assertTrue(all("，" not in x and "。" not in x for x in opts), opts)

    def test_click_list_y_fracs_are_dialog_absolute(self) -> None:
        """y_fracs must map against full dialog height, not band height."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.core.task_api import _click_list_slots

        rect = SimpleNamespace(x=45, y=164, w=265, h=337, ok=True)
        clicks = []

        def fake_click(session, cx, cy, hwnd=0, log=None, double=True):
            clicks.append((cx, cy))
            return True

        with patch("app.core.task_api._click_client_xy", side_effect=fake_click), patch(
            "app.core.task_api._scene_id_now", return_value=68
        ), patch("time.sleep", return_value=None):
            _click_list_slots(
                object(),
                rect,
                slots=2,
                prefer_indices=[0],
                max_clicks=1,
                double=True,
                band_top=0.30,
                band_bottom=0.82,
                y_fracs=[0.40],
                micro_tries=1,
                before_scene=68,
            )
        self.assertTrue(clicks, "expected click")
        # dialog-abs 0.40 → y=164+int(337*0.40)=298; band-rel wrongly gave 335
        self.assertEqual(clicks[0][1], 298, clicks)

    def test_dest_upper_y_is_below_mid_dialog(self) -> None:
        """上层 must click lower half (deep success was y≈399-428)."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.core.task_api import _click_list_slots

        rect = SimpleNamespace(x=45, y=164, w=265, h=337, ok=True)
        clicks = []

        def fake_click(session, cx, cy, hwnd=0, log=None, double=True):
            clicks.append(cy)
            return True

        with patch("app.core.task_api._click_client_xy", side_effect=fake_click), patch(
            "app.core.task_api._scene_id_now", return_value=68
        ), patch("time.sleep", return_value=None):
            _click_list_slots(
                object(),
                rect,
                slots=2,
                prefer_indices=[0],
                max_clicks=1,
                double=True,
                band_top=0.48,
                band_bottom=0.92,
                y_fracs=[0.56, 0.60, 0.52, 0.64],
                micro_tries=4,
                before_scene=68,
            )
        self.assertTrue(clicks)
        # all upper tries should be below dialog mid (~332) and above deep floor
        for cy in clicks:
            self.assertGreaterEqual(cy, 330, clicks)
            self.assertLess(cy, 400, clicks)

    def test_classify_boss_yucanghai(self) -> None:

        from app.core.task_api import classify_task_portal_kind

        self.assertEqual(
            classify_task_portal_kind({"name": "<每日BOSS>余沧海"}),
            "dungeon_deep",
        )
        self.assertEqual(
            classify_task_portal_kind({"name": "沧海BOSS"}),
            "dungeon_deep",
        )
        self.assertEqual(
            classify_task_portal_kind(
                {"name": "余沧海", "category": "每日BOSS", "story": "去升级地宫深处"}
            ),
            "dungeon_deep",
        )



    def test_portal_text_is_clean_rejects_cc_fill(self) -> None:
        from app.core.task_api import (
            _portal_text_is_clean,
            _portal_texts_usable,
            _portal_strings_from_blob,
        )

        # 0xCC fill as UTF-16LE often becomes Hangul-ish garbage
        self.assertFalse(_portal_text_is_clean("쳌쳌쳌"))
        self.assertFalse(_portal_text_is_clean("譓\u245c嘈\uf18b"))
        self.assertFalse(_portal_texts_usable(["쳌쳌", "譓\u245c嘈"]))
        self.assertTrue(_portal_text_is_clean("地宫上层"))
        self.assertTrue(_portal_text_is_clean("升级地宫深处"))
        # random pointer blob without keyword must not yield usable hits
        junk = bytes([0xCC] * 64) + b"\x00\x00"
        hits = _portal_strings_from_blob(junk)
        self.assertEqual(hits, [])
        self.assertFalse(_portal_texts_usable(hits))

    def test_portal_dest_slot_from_clean_options(self) -> None:
        from app.core.task_api import _portal_dest_slot_from_texts, _portal_option_lines

        texts = ["地宫传送", "地宫上层", "地宫深处"]
        opts = _portal_option_lines(texts)
        self.assertTrue(any("上层" in o for o in opts))
        # UI rows: upper=0 deep=1 (not scan-list index)
        self.assertEqual(_portal_dest_slot_from_texts(texts, "dungeon_upper"), 0)
        self.assertEqual(_portal_dest_slot_from_texts(texts, "dungeon_deep"), 1)

    def test_portal_direct_two_row_host_menu_recovers_missing_label(self) -> None:
        from app.core.npc_service_mem import _infer_direct_layer_host_rows

        rows = {
            "rows": [
                {"index": 4, "field0": 71, "label": "上层", "is_magic": False},
                {"index": 5, "field0": 72, "label": "", "is_magic": False},
            ]
        }
        inferred = _infer_direct_layer_host_rows(rows, ["上层"])
        self.assertEqual([(r["index"], r["label"]) for r in inferred], [(4, "上层"), (5, "深处")])

    def test_portal_direct_host_inference_rejects_task_entry_panel(self) -> None:
        from app.core.npc_service_mem import _infer_direct_layer_host_rows

        rows = {
            "rows": [
                {"index": 0, "field0": 0xC0ABCDEF, "label": "每日杀怪", "is_magic": True},
                {"index": 3, "field0": 71, "label": "中级地宫传送", "is_magic": False},
            ]
        }
        self.assertEqual(_infer_direct_layer_host_rows(rows, ["上层"]), [])

    def test_portal_cache_recovery_requires_host_menu_transition(self) -> None:
        from app.core.npc_service_mem import _can_recover_cached_l2

        before = {
            "rows": [
                {"index": 0, "field0": 0xC0ABCDEF, "is_magic": True},
                {"index": 3, "field0": 71, "is_magic": False},
            ]
        }
        after = {
            "rows": [
                {"index": 0, "field0": 81, "is_magic": False},
                {"index": 1, "field0": 82, "is_magic": False},
            ]
        }
        cached = {"index": 1, "label": "深处", "kind": "host"}
        self.assertTrue(_can_recover_cached_l2(before, after, cached))
        self.assertFalse(_can_recover_cached_l2(before, before, cached))
        self.assertTrue(
            _can_recover_cached_l2(
                before, after, {"index": 0, "label": "上层", "kind": "host"}
            )
        )

    def test_portal_function_reopens_only_before_any_menu_click(self) -> None:
        from app.core.task_api import _portal_function_needs_reopen

        self.assertTrue(
            _portal_function_needs_reopen(
                {"ok": False, "stage": "none", "error": "panel empty after Hello"}
            )
        )
        self.assertTrue(
            _portal_function_needs_reopen(
                {"ok": False, "stage": "none", "error": "no portal options"}
            )
        )
        self.assertFalse(
            _portal_function_needs_reopen(
                {"ok": False, "stage": "layer1", "clicked": ["L1:host"]}
            )
        )

    @patch("app.core.npc_service_mem.portal_select_by_function")
    @patch("app.core.task_api.time.sleep")
    @patch("app.core.task_api._open_task_npc_dialog", return_value="NPCSayHello ok=True ret=1")
    @patch("app.core.task_api.pathfind_to_clue")
    @patch("app.core.task_api.list_nearby_npcs")
    @patch("app.core.automove.read_scene_position")
    def test_portal_dialog_waits_for_live_verified_arrival(
        self, scene, nearby, pathfind, _open, sleep, select
    ) -> None:
        from app.core.task_api import use_task_portal_npc

        scene.return_value = SimpleNamespace(ok=True, scene_id=68)
        nearby.return_value = [
            {"tid": 100220, "obj_id": 9, "x": 12.0, "y": 3.0, "z": 24.0, "dist": 2.0}
        ]
        pathfind.return_value = {
            "ok": True,
            "last_distance": 0.4,
            "last_position": [12.0, 3.0, 24.0],
        }
        select.return_value = {"ok": True, "after_scene": 2022, "note": "selected"}

        result = use_task_portal_npc(
            object(),
            {"name": "地宫传送", "tid": 5001, "obj_id": 9, "dist": 1.0, "x": 12.0, "y": 3.0, "z": 24.0},
            portal_kind="dungeon_upper",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(pathfind.call_args.args[1]["source"], "portal_npc_live")
        self.assertIn(0.75, [call.args[0] for call in sleep.call_args_list])

    @patch("app.core.npc_service_mem.portal_select_by_function")
    @patch("app.core.task_api.time.sleep")
    @patch(
        "app.core.task_api._open_task_npc_dialog",
        return_value="NPCSayHello ok=True ret=1",
    )
    @patch("app.core.task_api.pathfind_to_clue")
    @patch("app.core.task_api._portal_scene_generation_state")
    def test_synced_portal_derives_missing_distance_from_scene_snapshot(
        self, generation, pathfind, _open, _sleep, select
    ) -> None:
        from app.core.task_api import use_task_portal_npc

        generation.return_value = {
            "ok": True,
            "role_present": True,
            "scene_id": 68,
            "position": (12.4, 3.0, 24.3),
            "status": "current",
        }
        pathfind.return_value = {
            "ok": True,
            "last_distance": 0.5,
            "last_position": [12.4, 3.0, 24.3],
        }
        select.return_value = {"ok": True, "after_scene": 2034, "note": "selected"}

        result = use_task_portal_npc(
            SimpleNamespace(pid=19004),
            {
                "name": "地宫传送",
                "tid": 5001,
                "obj_id": 9,
                "dist": None,
                "x": 12.0,
                "y": 3.0,
                "z": 24.0,
            },
            portal_kind="dungeon_deep",
            expected_origin_scene_id=68,
            allow_live_rebind=False,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["initial_distance_source"], "scene_snapshot")
        self.assertEqual(pathfind.call_count, 1)
        self.assertEqual(pathfind.call_args.args[1]["source"], "portal_npc_live")

    @patch("app.core.npc_service_mem._pid_blocked", return_value=(False, ""))
    @patch("app.core.npc_service_mem.collect_portal_menu_texts")
    @patch("app.core.npc_service_mem.dump_host_service_table")
    @patch("app.core.npc_service_mem.dump_talk_proc", return_value=None)
    @patch("app.core.npc_service_mem.time.sleep")
    def test_portal_generic_shell_requests_early_hello_reopen(
        self, _sleep, _talk, host, texts, _blocked
    ) -> None:
        from app.core.npc_service_mem import portal_select_by_function

        host.return_value = {
            "ok": True,
            "count": 2,
            "rows": [
                {"index": 0, "field0": 0, "label": ""},
                {"index": 1, "field0": 0, "label": ""},
            ],
        }
        texts.return_value = ["高级地宫传送", "地宫传送", "传送", "地宫"]

        result = portal_select_by_function(
            SimpleNamespace(pid=19005),
            tier="高级",
            layer="deep",
            portal_tid=100220,
            settle_s=0.01,
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["early_reopen"])
        self.assertEqual(result["stage"], "none")
        self.assertEqual(host.call_count, 2)

    def test_portal_l2_map_name_is_advisory_for_live_host_menu(self) -> None:
        from app.core.npc_service_mem import _l2_label_fits_tier

        # A verified direct L2 menu may legitimately use a map title that the
        # legacy tier-name heuristic does not recognize.  The caller must keep
        # the live host row and verify the resulting upper/deep scene.
        self.assertFalse(_l2_label_fits_tier("西王母宫上层", "初级"))

    def test_portal_scene_layer_check_soft(self) -> None:
        from app.core.task_api import _portal_scene_layer_check
        from unittest.mock import patch

        with patch(
            "app.core.map_names.resolve_scene_id",
            return_value=("x", "高级地宫上层"),
        ), patch(
            "app.core.map_names.format_scene_display",
            return_value="高级地宫上层 (x)",
        ):
            up = _portal_scene_layer_check(2099, "dungeon_upper")
            deep = _portal_scene_layer_check(2099, "dungeon_deep")
        self.assertTrue(up.get("match"))
        self.assertFalse(deep.get("match"))

        # live scene_map ids: 2022=俺答汗陵上层, 2023=下层, 2034=深处
        up2 = _portal_scene_layer_check(2022, "dungeon_upper")
        deep_wrong = _portal_scene_layer_check(2022, "dungeon_deep")
        deep_ok = _portal_scene_layer_check(2023, "dungeon_deep")
        deep_ok2 = _portal_scene_layer_check(2034, "dungeon_deep")
        self.assertTrue(up2.get("match"), up2)
        self.assertFalse(deep_wrong.get("match"), deep_wrong)
        self.assertTrue(deep_ok.get("match"), deep_ok)
        self.assertTrue(deep_ok2.get("match"), deep_ok2)




    def test_classify_dungeon_tier_mid(self) -> None:
        from app.core.task_api import classify_dungeon_tier

        self.assertEqual(
            classify_dungeon_tier({"name": "<每日杀怪>中级地宫杀怪"}),
            "中级",
        )
        self.assertEqual(
            classify_dungeon_tier({"name": "<每日杀怪>初级地宫杀怪"}),
            "初级",
        )
        self.assertEqual(
            classify_dungeon_tier({"name": "练级地宫"}),
            "升级",
        )

    def test_pick_portal_excludes_leveling(self) -> None:
        from app.core.task_api import list_portal_npc_candidates, _portal_texts_match_tier, _portal_dest_lines_ready

        rows = [
            {"name": "升级地宫传送", "dist": 10, "tid": 1, "obj_id": 1},
            {"name": "地宫传送", "dist": 30, "tid": 2, "obj_id": 2},
            {"name": "中级地宫传送", "dist": 40, "tid": 3, "obj_id": 3},
        ]
        cands = list_portal_npc_candidates(rows, "dungeon_upper", tier="中级", max_n=5)
        names = [c["name"] for c in cands]
        self.assertNotIn("升级地宫传送", names)
        self.assertEqual(cands[0]["name"], "中级地宫传送")

        self.assertFalse(
            _portal_texts_match_tier(
                ["升级地宫传送", "野人峡谷上层", "上层"], "中级"
            )
        )
        # map-only noise without title → unsure (None), not hard True
        self.assertIsNone(
            _portal_texts_match_tier(["地宫上层", "野人峡谷上层"], "中级")
        )
        self.assertFalse(
            _portal_texts_match_tier(["高级地宫传送", "地宫传送"], "中级")
        )
        self.assertTrue(
            _portal_texts_match_tier(["中级地宫传送", "地宫传送"], "中级")
        )
        # 镖局「初级」噪声 + 高级地宫传送 → 高级 ok
        self.assertTrue(
            _portal_texts_match_tier(
                ["镖局藏宝阁初级等级3.ecp", "初级", "高级地宫传送", "地宫传送", "高级"],
                "高级",
            )
        )
        # BOSS 任务名噪声不得否决
        self.assertFalse(
            _portal_texts_match_tier(
                ["高级地宫传送", "<每日BOSS>中级地宫BOSS", "中级"],
                "中级",
            )
        )
        self.assertTrue(
            _portal_dest_lines_ready(["野人峡谷上层", "野人峡谷深处", "上层", "深处"])
        )
        self.assertFalse(
            _portal_dest_lines_ready(["高级地宫传送", "地宫传送"])
        )

    def test_pick_portal_prefers_delv_tid(self) -> None:
        """中级杀怪 delv=100218 must beat nearer 高级 100220."""
        from app.core.task_api import list_portal_npc_candidates

        rows = [
            {"name": "地宫传送", "dist": 29.4, "tid": 100220, "obj_id": 11},
            {"name": "地宫传送", "dist": 29.5, "tid": 100219, "obj_id": 12},
            {"name": "地宫传送", "dist": 29.6, "tid": 100221, "obj_id": 13},
            {"name": "地宫传送", "dist": 29.9, "tid": 100218, "obj_id": 14},
        ]
        cands = list_portal_npc_candidates(
            rows, "dungeon_upper", tier="中级", prefer_tid=100218, max_n=4
        )
        self.assertEqual(int(cands[0]["tid"]), 100218)





    def test_portal_candidates_match_tid_without_name(self) -> None:
        """Name-read failure (tidXXXX) must still find known portal tids."""
        from app.core.task_api import list_portal_npc_candidates

        rows = [
            {"name": "tid100220", "dist": 10.0, "tid": 100220, "obj_id": 1},
            {"name": "tid100218", "dist": 12.0, "tid": 100218, "obj_id": 2},
            {"name": "杂货商", "dist": 5.0, "tid": 999, "obj_id": 3},
        ]
        cands = list_portal_npc_candidates(
            rows, "dungeon_upper", tier="中级", prefer_tid=100218
        )
        self.assertTrue(cands)
        self.assertEqual(int(cands[0].get("tid") or 0), 100218)


    def test_route_rejects_random_nearby_when_delv_known(self) -> None:
        from app.core.task_api import choose_task_route_clue

        clues = [
            {"source": "nearby", "name": "杂货商", "x": 1, "z": 1, "dist": 1.0, "tid": 1},
            {"source": "nearby", "name": "打怪点", "x": 2, "z": 2, "dist": 2.0, "tid": 2},
        ]
        # delv=100219 portal tid known → must NOT pick random nearby
        self.assertIsNone(
            choose_task_route_clue(clues, for_complete=False, npc_tid=100219)
        )

    def test_resolve_dungeon_scene_by_tier_layer(self) -> None:
        from app.core.portal_service import resolve_dungeon_scene_ids, _normalize_layer

        self.assertEqual(_normalize_layer("dungeon_deep"), "deep")
        self.assertEqual(_normalize_layer("上层"), "upper")
        ids = resolve_dungeon_scene_ids("初级", "dungeon_deep")
        self.assertTrue(ids)
        self.assertTrue(all(isinstance(i, int) and i > 0 for i in ids))


    def test_choose_route_ignores_empty_when_no_primary(self) -> None:
        from app.core.task_api import choose_task_route_clue

        # only weak nearby rows → still choosable if no delv filtering here;
        # route chooser returns nearest has_pos. Empty list → None.
        self.assertIsNone(choose_task_route_clue([], for_complete=False))

    def test_resolve_dungeon_tier_prefers_delv_tid(self) -> None:
        from app.core.task_api import resolve_dungeon_tier, tier_from_portal_tid

        # 龙傲天 BOSS name alone → 升级, but delv=100219 is 初级 portal
        self.assertEqual(tier_from_portal_tid(100219), "初级")
        self.assertEqual(
            resolve_dungeon_tier(
                {"name": "<每日BOSS>龙傲天", "category": "每日BOSS"},
                prefer_tid=100219,
            ),
            "初级",
        )
        self.assertEqual(
            resolve_dungeon_tier(
                {"name": "<每日BOSS>余沧海", "category": "每日BOSS"},
                prefer_tid=100221,
            ),
            "升级",
        )
        # no tid → fall back to name
        self.assertEqual(
            resolve_dungeon_tier({"name": "<每日BOSS>余沧海", "category": "每日BOSS"}),
            "升级",
        )

    def test_portal_dest_ready_rejects_polluted_upper_only(self) -> None:
        from app.core.task_api import _portal_dest_lines_ready

        polluted = [
            "高级地宫传送",
            "地宫传送",
            "西王母宫上层",
            "上层",
            "初级地宫发放任务",
        ]
        self.assertFalse(_portal_dest_lines_ready(polluted))
        self.assertTrue(
            _portal_dest_lines_ready(["野人峡谷上层", "野人峡谷深处"])
        )

    def test_boss_tier_is_upgrade(self) -> None:
        from app.core.task_api import classify_dungeon_tier, classify_task_portal_kind

        self.assertEqual(classify_task_portal_kind({"name": "<每日BOSS>余沧海"}), "dungeon_deep")
        self.assertEqual(classify_dungeon_tier({"name": "<每日BOSS>余沧海", "category": "每日BOSS"}), "升级")


if __name__ == "__main__":
    unittest.main()
