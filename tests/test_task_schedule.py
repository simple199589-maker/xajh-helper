# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path
from unittest import mock

from app.core.task_schedule import (
    CUSTOM_ACTIVITY_TARGET_POINTS,
    CUSTOM_DEF_ID_ACTIVITY,
    CUSTOM_DEF_DUNGEON_IDS,
    DEFAULT_SCHEDULE_HOUR,
    DEFAULT_SCHEDULE_MINUTE,
    RUNNER_STATE_IDLE,
    SETTING_SCHEDULE_ENABLED,
    SETTING_SCHEDULE_OWNER,
    ScheduleTaskRunner,
    add_custom_to_queue,
    bind_settings_to_profile,
    custom_audit_activity,
    custom_audit_dungeon,
    custom_definition_enabled,
    custom_definition_label,
    custom_queue_item,
    get_custom_definition,
    get_schedule_hm,
    list_custom_definitions,
    load_role_schedule_profile,
    load_schedule_queue,
    mark_profile_schedule_fired,
    mark_schedule_fired,
    move_queue_index,
    normalize_queue_item,
    profile_from_settings,
    queue_item_key,
    queue_item_label,
    remove_queue_index,
    resolve_instance_for_task,
    save_role_schedule_profile,
    save_schedule_queue,
    schedule_profile_should_fire,
    schedule_should_fire,
    set_schedule_hm,
    try_add_task_to_queue,
    update_custom_definition_ids,
)


class _PatchCM:
    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *_exc):
        for p in reversed(self._patches):
            p.stop()


def _temp_schedule_config():
    """Patch schedule storage to a temp dir for hermetic tests. @author by ak"""
    td = Path(tempfile.mkdtemp())
    tmp = td / "schedule_config_v1.json"
    roles = td / "roles"
    return _PatchCM(
        [
            mock.patch(
                "app.core.task_schedule._schedule_config_path", return_value=tmp
            ),
            mock.patch(
                "app.core.account_manager.roles_root", return_value=roles
            ),
        ]
    )


class TaskScheduleTests(unittest.TestCase):
    def test_role_schedule_writes_role_directory(self) -> None:
        with _temp_schedule_config():
            profile = {
                "enabled": True,
                "hour": 9,
                "minute": 30,
                "queue": [],
                "custom_ids": {},
            }
            save_role_schedule_profile("800001", profile)

            from app.core.account_manager import load_role_config, role_dir

            self.assertTrue((role_dir("800001") / "schedule.json").is_file())
            self.assertTrue(load_role_config("800001", "schedule")["enabled"])
            self.assertEqual(load_role_schedule_profile("800001")["hour"], 9)
    def test_resolve_instance_keywords(self) -> None:
        cases = [
            ("初级地宫杀怪", 3635, "dungeon"),
            ("高级副本任务", 6283, "dungeon"),
            ("超级副本", 6981, "dungeon"),
            ("140副本任务一", 6981, "qiegao"),
            ("切糕挂机", 6981, "qiegao"),
            ("福利副本云上", 3048, "dungeon"),
            ("组队梅庄", 2638, "dungeon"),
            ("绿竹幻想乡", 169, "dungeon"),
        ]
        for name, iid, mode in cases:
            r = resolve_instance_for_task({"task_id": 100, "name": name})
            self.assertTrue(r["ok"], name)
            self.assertEqual(r["instance_id"], iid, name)
            self.assertEqual(r["mode"], mode, name)

    def test_resolve_rejects_unmapped(self) -> None:
        r = resolve_instance_for_task({"task_id": 1, "name": "每日BOSS龙傲天"})
        self.assertFalse(r["ok"])
        self.assertIn("无法映射", r["note"])

    def test_queue_add_dedupe_and_remove(self) -> None:
        s: dict = {}
        a = try_add_task_to_queue(s, {"task_id": 11, "name": "初级副本A"})
        self.assertTrue(a["ok"])
        b = try_add_task_to_queue(s, {"task_id": 11, "name": "初级副本A"})
        self.assertFalse(b["ok"])
        c = try_add_task_to_queue(s, {"task_id": 12, "name": "高级副本B"})
        self.assertTrue(c["ok"])
        q = load_schedule_queue(s)
        self.assertEqual(len(q), 2)
        q2 = remove_queue_index(s, 0)
        self.assertEqual(len(q2), 1)
        self.assertEqual(q2[0]["task_id"], 12)
        lab = queue_item_label(q2[0])
        self.assertIn("#12", lab)

    def test_normalize_requires_task_id(self) -> None:
        self.assertIsNone(normalize_queue_item({"name": "初级"}))
        self.assertIsNone(normalize_queue_item({"task_id": 1, "name": "无关任务"}))

    def test_schedule_fire_once_per_day(self) -> None:
        s: dict = {}
        set_schedule_hm(s, 10, 30)
        now = datetime(2026, 7, 21, 10, 30, 5)
        self.assertTrue(schedule_should_fire(s, now=now))
        mark_schedule_fired(s, when=now.date())
        self.assertFalse(schedule_should_fire(s, now=now))
        later = datetime(2026, 7, 22, 10, 30, 0)
        self.assertTrue(schedule_should_fire(s, now=later))
        h, m = get_schedule_hm(s)
        self.assertEqual((h, m), (10, 30))

    # ---- custom definitions ----

    def test_daily_routine_definitions_are_queueable(self) -> None:
        defs = {d["definition_id"]: d for d in list_custom_definitions()}
        for definition_id, task_id, scene_id in (
            ("daily_badao_10021", 10021, 72),
            ("daily_longaotian_10010", 10010, 2036),
        ):
            item = custom_queue_item(defs[definition_id])
            self.assertIsNotNone(item)
            self.assertEqual(item["task_id"], task_id)
            self.assertEqual(item["target_scene_id"], scene_id)
            self.assertEqual(item["kind"], "routine")

    def test_routine_queue_ignores_stale_target_snapshot(self) -> None:
        item = normalize_queue_item(
            {
                "source": "custom",
                "definition_id": "daily_badao_10021",
                "kind": "routine",
                "task_id": 10006,
                "target_scene_id": 2034,
                "target_tid": 100072,
            }
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["task_id"], 10021)
        self.assertEqual(item["target_scene_id"], 72)
        self.assertEqual(item["target_tid"], 101042)

    def test_completed_routine_skips_route_and_resident_scan(self) -> None:
        runner = ScheduleTaskRunner(pid=19048)
        session = SimpleNamespace(pid=19048)
        with mock.patch(
            "app.core.task_schedule.list_accepted_task_ids", return_value=[10021]
        ), mock.patch(
            "app.core.task_schedule.list_accepted_tasks", return_value=[]
        ), mock.patch(
            "app.core.task_schedule.accepted_task_map",
            return_value={10021: {"task_id": 10021, "is_finished": True, "can_finish": False}},
        ), mock.patch.object(runner, "_prepare_badao_route") as route, mock.patch(
            "app.core.task_schedule.read_resident_targets"
        ) as scan:
            result = runner._execute_custom_routine(
                session,
                custom_queue_item(get_custom_definition("daily_badao_10021")),
            )
        self.assertEqual(result, "next")
        route.assert_not_called()
        scan.assert_not_called()

    def test_badao_route_resumes_when_already_in_scene_72(self) -> None:
        runner = ScheduleTaskRunner(pid=19048)
        with mock.patch("app.core.task_schedule.read_scene_position", return_value=SimpleNamespace(ok=True, scene_id=72)), mock.patch.object(runner, "_wait_routine_scene_stable", return_value=True), mock.patch.object(runner, "_start_routine_follow", return_value=True) as follow, mock.patch.object(runner, "_routine_path_clue_retry") as route, mock.patch.object(runner, "_send_routine_packet") as packet:
            result = runner._prepare_badao_route(object(), 10021, "daily_badao_10021")
        self.assertEqual(result["after_scene"], 72)
        self.assertTrue(result["skipped"])
        follow.assert_called_once()
        route.assert_not_called()
        packet.assert_not_called()

    def test_routine_stop_uses_unified_hang_cleanup(self) -> None:
        runner = ScheduleTaskRunner(
            pid=19048,
            hwnd=0x1234,
            hang_settings={"hang_mode": 1, "hang_wanzi_hang": True},
        )
        with mock.patch(
            "app.core.hang_settings.stop_hang",
            return_value={"ok": True, "via": "raw_c2s_packet", "wanzi": {"ok": True}},
        ) as stop_hang, mock.patch(
            "app.core.hang_settings.send_hang_stop_packet"
        ) as raw_stop:
            self.assertTrue(
                runner._stop_routine_autoplay(object(), 10021, "daily_badao_10021")
            )
        stop_hang.assert_called_once()
        self.assertEqual(stop_hang.call_args.kwargs["hwnd"], 0x1234)
        raw_stop.assert_not_called()
    def test_daily_routine_empty_target_returns_next_after_return(self) -> None:
        events = []
        runner = ScheduleTaskRunner(pid=19048, on_event=events.append)
        with mock.patch.object(runner, "_return_routine_fuzhou", return_value=True) as return_fuzhou:
            result = runner._skip_empty_routine(
                object(),
                {"name": "霸刀日常"},
                10021,
                "daily_badao_10021",
            )
        self.assertEqual(result, "next")
        return_fuzhou.assert_called_once()
        self.assertTrue(any(event["phase"] == "routine_no_target_skip" for event in events))
        self.assertTrue(any(event["phase"] == "routine_done" for event in events))

    def test_daily_routine_completion_waits_for_cooldown(self) -> None:
        events = []
        runner = ScheduleTaskRunner(
            pid=19048,
            on_event=events.append,
            activity_entry_cd_s=30.0,
        )
        with mock.patch(
            "app.core.task_schedule.interruptible_sleep", return_value=True
        ) as sleep:
            self.assertTrue(runner._routine_cooldown(10021, "daily_badao_10021"))
        sleep.assert_called_once_with(30.0, runner._stop)
        self.assertEqual(events[-1]["phase"], "routine_cooldown")
        self.assertEqual(events[-1]["detail"]["seconds"], 30.0)

    def test_schedule_outcomes_are_written_to_user_log(self) -> None:
        user_logs = []
        runner = ScheduleTaskRunner(pid=19048, user_log=user_logs.append)
        runner._emit("item_done", "完成 霸刀日常")
        runner._emit("task_skip_not_accepted", "#10010 未接，跳过")
        runner._emit("complete_ok", "交付成功 #10021")
        runner._emit("blocked", "[route] 寻路失败", ok=False)
        runner._emit("done", "计划任务队列执行完毕")
        self.assertEqual(
            user_logs,
            [
                "计划任务：完成 霸刀日常",
                "计划任务：#10010 未接，跳过",
                "计划任务：交付成功 #10021",
                "计划任务：[route] 寻路失败",
                "计划任务：计划任务队列执行完毕",
            ],
        )

    def test_custom_definitions_defaults(self) -> None:
        defs = list_custom_definitions()
        self.assertEqual(len(defs), 9)
        activity = next(d for d in defs if d["definition_id"] == CUSTOM_DEF_ID_ACTIVITY)
        self.assertTrue(activity["enabled"])
        self.assertEqual(activity["kind"], "activity")
        self.assertEqual(activity["target_points"], CUSTOM_ACTIVITY_TARGET_POINTS)
        badao = next(d for d in defs if d["definition_id"] == "daily_badao_10021")
        self.assertEqual((badao["task_id"], badao["target_scene_id"], badao["target_tid"]), (10021, 72, 101042))
        longaotian = next(d for d in defs if d["definition_id"] == "daily_longaotian_10010")
        self.assertEqual((longaotian["task_id"], longaotian["target_scene_id"]), (10010, 2036))
        yucanghai = next(d for d in defs if d["definition_id"] == "daily_yucanghai_10011")
        self.assertEqual((yucanghai["task_id"], yucanghai["target_scene_id"], yucanghai["target_tid"]), (10011, 2030, 101013))
        dongfang = next(d for d in defs if d["definition_id"] == "daily_dongfang_10006")
        self.assertEqual((dongfang["task_id"], dongfang["target_scene_id"], dongfang["target_tid"]), (10006, 2034, 100072))
        self.assertEqual(activity["instance_id"], 169)
        # Dungeon slots use concrete task names and are pre-filled from the map.
        by_id = {d["definition_id"]: d for d in defs}
        self.assertEqual(by_id["daily_dungeon_1"]["name"], "140副本任务一")
        self.assertEqual(by_id["daily_dungeon_1"]["task_id"], 10028)
        self.assertEqual(by_id["daily_dungeon_1"]["instance_id"], 6230)
        self.assertEqual(by_id["daily_dungeon_2"]["name"], "140副本任务二")
        self.assertEqual(by_id["daily_dungeon_2"]["task_id"], 10027)
        self.assertEqual(by_id["daily_dungeon_2"]["instance_id"], 1928)
        self.assertEqual(by_id["daily_dungeon_3"]["name"], "组队本")
        self.assertEqual(by_id["daily_dungeon_3"]["task_id"], 10020)
        self.assertEqual(by_id["daily_dungeon_3"]["instance_id"], 2638)
        self.assertEqual(by_id["daily_dungeon_4"]["name"], "武尊堂")
        self.assertEqual(by_id["daily_dungeon_4"]["task_id"], 10025)
        self.assertEqual(by_id["daily_dungeon_4"]["instance_id"], 7368)
        for d in defs:
            if d["definition_id"] in CUSTOM_DEF_DUNGEON_IDS:
                self.assertTrue(d["enabled"])
                self.assertNotIn("待配置", custom_definition_label(d))

    def test_custom_task_instance_map_content(self) -> None:
        from app.core.task_schedule import CUSTOM_TASK_INSTANCE_MAP

        self.assertEqual(len(CUSTOM_TASK_INSTANCE_MAP), 4)
        self.assertEqual(CUSTOM_TASK_INSTANCE_MAP["daily_task_140_1"]["task_id"], 10028)
        self.assertEqual(CUSTOM_TASK_INSTANCE_MAP["daily_task_140_1"]["instance_id"], 6230)
        self.assertEqual(CUSTOM_TASK_INSTANCE_MAP["daily_task_140_2"]["task_id"], 10027)
        self.assertEqual(CUSTOM_TASK_INSTANCE_MAP["daily_task_140_2"]["instance_id"], 1928)
        self.assertEqual(CUSTOM_TASK_INSTANCE_MAP["daily_task_group"]["task_id"], 10020)
        self.assertEqual(CUSTOM_TASK_INSTANCE_MAP["daily_task_group"]["instance_id"], 2638)
        self.assertEqual(CUSTOM_TASK_INSTANCE_MAP["daily_task_wuzun"]["task_id"], 10025)
        self.assertEqual(CUSTOM_TASK_INSTANCE_MAP["daily_task_wuzun"]["instance_id"], 7368)

    def test_custom_wuzun_slot_queue_item(self) -> None:
        defs = {d["definition_id"]: d for d in list_custom_definitions()}
        item = custom_queue_item(defs["daily_dungeon_4"])
        self.assertIsNotNone(item)
        self.assertEqual(item["name"], "武尊堂")
        self.assertEqual(item["task_id"], 10025)
        self.assertEqual(item["instance_id"], 7368)

    def test_custom_dungeon_no_ids_stays_disabled(self) -> None:
        # A slot whose built-in map entry is missing keeps 待配置.
        d = {
            "definition_id": "daily_dungeon_9",
            "name": "测试副本",
            "kind": "dungeon",
            "task_id": 0,
            "instance_id": 0,
            "enabled": False,
        }
        self.assertFalse(custom_definition_enabled(d))
        self.assertIn("待配置", custom_definition_label(d))
        self.assertIsNone(custom_queue_item(d))

    def test_custom_dungeon_ids_override(self) -> None:
        d1 = update_custom_definition_ids(None, "daily_dungeon_1", task_id=1001, instance_id=6283)
        defs = list_custom_definitions(d1)
        d1def = next(x for x in defs if x["definition_id"] == "daily_dungeon_1")
        self.assertTrue(d1def["enabled"])
        self.assertEqual(d1def["task_id"], 1001)
        self.assertEqual(d1def["instance_id"], 6283)
        d2def = next(x for x in defs if x["definition_id"] == "daily_dungeon_2")
        self.assertTrue(d2def["enabled"])
        self.assertEqual(d2def["instance_id"], 1928)

    def test_custom_queue_item_defaults_enabled(self) -> None:
        defs = {d["definition_id"]: d for d in list_custom_definitions()}
        item = custom_queue_item(defs["daily_dungeon_1"])
        self.assertIsNotNone(item)
        self.assertEqual(item["task_id"], 10028)
        self.assertEqual(item["instance_id"], 6230)
        self.assertIsNotNone(custom_queue_item(defs[CUSTOM_DEF_ID_ACTIVITY]))

    def test_add_custom_to_queue_default_ok(self) -> None:
        s: dict = {}
        res = add_custom_to_queue(s, "daily_dungeon_1")
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["item"]["task_id"], 10028)
        self.assertEqual(res["item"]["instance_id"], 6230)
        self.assertEqual(len(load_schedule_queue(s)), 1)

    def test_add_custom_activity_ok_and_dedupe(self) -> None:
        s: dict = {}
        a = add_custom_to_queue(s, CUSTOM_DEF_ID_ACTIVITY)
        self.assertTrue(a["ok"])
        self.assertEqual(a["item"]["source"], "custom")
        self.assertEqual(a["item"]["definition_id"], CUSTOM_DEF_ID_ACTIVITY)
        self.assertEqual(a["item"]["instance_id"], 169)
        b = add_custom_to_queue(s, CUSTOM_DEF_ID_ACTIVITY)
        self.assertFalse(b["ok"])
        self.assertIn("已有", b.get("error") or "")
        q = load_schedule_queue(s)
        self.assertEqual(len(q), 1)
        self.assertEqual(queue_item_key(q[0]), ("custom", CUSTOM_DEF_ID_ACTIVITY))

    def test_add_custom_dungeon_with_ids(self) -> None:
        s: dict = {}
        cids = update_custom_definition_ids(None, "daily_dungeon_1", task_id=1001, instance_id=6283)
        s["schedule_custom_ids"] = cids
        res = add_custom_to_queue(s, "daily_dungeon_1")
        self.assertTrue(res["ok"], res.get("error"))
        item = res["item"]
        self.assertEqual(item["task_id"], 1001)
        self.assertEqual(item["instance_id"], 6283)

    def test_queue_move_up_down(self) -> None:
        s: dict = {}
        add_custom_to_queue(s, CUSTOM_DEF_ID_ACTIVITY)
        cids = update_custom_definition_ids(None, "daily_dungeon_1", task_id=1001, instance_id=6283)
        s["schedule_custom_ids"] = cids
        add_custom_to_queue(s, "daily_dungeon_1")
        cids2 = update_custom_definition_ids(cids, "daily_dungeon_2", task_id=1002, instance_id=3635)
        s["schedule_custom_ids"] = cids2
        add_custom_to_queue(s, "daily_dungeon_2")
        q = load_schedule_queue(s)
        self.assertEqual(len(q), 3)
        # move index 2 -> 0 (up twice)
        q = move_queue_index(s, 2, -1)
        q = move_queue_index(s, 1, -1)
        self.assertEqual(q[0]["definition_id"], "daily_dungeon_2")
        self.assertEqual(q[1]["definition_id"], CUSTOM_DEF_ID_ACTIVITY)
        self.assertEqual(q[2]["definition_id"], "daily_dungeon_1")
        # down one
        q = move_queue_index(s, 0, 1)
        self.assertEqual(q[0]["definition_id"], CUSTOM_DEF_ID_ACTIVITY)
        self.assertEqual(q[1]["definition_id"], "daily_dungeon_2")

    def test_mixed_queue_custom_and_legacy(self) -> None:
        s: dict = {}
        add_custom_to_queue(s, CUSTOM_DEF_ID_ACTIVITY)
        try_add_task_to_queue(s, {"task_id": 11, "name": "初级副本A"})
        q = load_schedule_queue(s)
        self.assertEqual(len(q), 2)
        kinds = {str(x.get("source") or "legacy") for x in q}
        self.assertEqual(kinds, {"custom", "legacy"})
        self.assertEqual(len({queue_item_key(x) for x in q}), 2)

    # ---- per-role schedule profile ----

    def test_schedule_profile_persist_per_role(self) -> None:
        with _temp_schedule_config():
            role_a = "111111"
            role_b = "222222"
            profile_a = {
                "captain_id": role_a,
                "enabled": True,
                "hour": 9,
                "minute": 15,
                "queue": [
                    {"source": "custom", "definition_id": CUSTOM_DEF_ID_ACTIVITY,
                     "name": "活跃", "kind": "activity", "task_id": 0, "instance_id": 169}
                ],
                "last_run_date": "2026-08-07",
                "custom_ids": {},
            }
            save_role_schedule_profile(role_a, profile_a)
            save_role_schedule_profile(role_b, {"enabled": False})
            loaded_a = load_role_schedule_profile(role_a)
            self.assertEqual(loaded_a["captain_id"], role_a)
            self.assertTrue(loaded_a["enabled"])
            self.assertEqual((loaded_a["hour"], loaded_a["minute"]), (9, 15))
            self.assertEqual(len(loaded_a["queue"]), 1)
            # role B is isolated from A
            loaded_b = load_role_schedule_profile(role_b)
            self.assertFalse(loaded_b["enabled"])
            self.assertEqual(loaded_b["queue"], [])
            # an unknown role gets an empty default plan
            loaded_c = load_role_schedule_profile("333333")
            self.assertEqual(loaded_c["queue"], [])
            self.assertFalse(loaded_c["enabled"])

    def test_schedule_profile_round_trip_settings(self) -> None:
        s: dict = {}
        set_schedule_hm(s, 8, 45)
        s[SETTING_SCHEDULE_ENABLED] = True
        s[SETTING_SCHEDULE_OWNER] = "123"
        add_custom_to_queue(s, CUSTOM_DEF_ID_ACTIVITY)
        profile = profile_from_settings(s)
        self.assertTrue(profile["enabled"])
        self.assertEqual(profile["captain_id"], "123")
        t: dict = {}
        bind_settings_to_profile(t, profile)
        self.assertTrue(t[SETTING_SCHEDULE_ENABLED])
        self.assertEqual(get_schedule_hm(t), (8, 45))
        self.assertEqual(len(load_schedule_queue(t)), 1)

    def test_role_profile_stored_per_role_dir(self) -> None:
        from app.core import account_manager as am

        td = Path(tempfile.mkdtemp())
        roles = td / "roles"
        with mock.patch("app.core.account_manager.roles_root", return_value=roles):
            save_role_schedule_profile("555555", {"enabled": True, "hour": 8})
            p = roles / "555555" / "schedule.json"
            self.assertTrue(p.is_file())
            loaded = load_role_schedule_profile("555555")
            self.assertTrue(loaded["enabled"])
            self.assertEqual(loaded["hour"], 8)

    def test_schedule_profile_should_fire(self) -> None:
        profile = {
            "captain_id": "123",
            "enabled": True,
            "hour": 10,
            "minute": 30,
            "last_run_date": "",
        }
        now = datetime(2026, 8, 7, 10, 30, 5)
        self.assertTrue(schedule_profile_should_fire(profile, now=now))
        mark_profile_schedule_fired(profile, when=now.date())
        self.assertFalse(schedule_profile_should_fire(profile, now=now))
        # next day same time fires again
        later = datetime(2026, 8, 8, 10, 30, 0)
        self.assertTrue(schedule_profile_should_fire(profile, now=later))
        # disabled never fires
        off = {"enabled": False, "hour": 10, "minute": 30}
        self.assertFalse(schedule_profile_should_fire(off, now=now))
        # missed time (already past minute) does not catch up
        missed = {"enabled": True, "hour": 10, "minute": 30, "last_run_date": ""}
        later_min = datetime(2026, 8, 7, 10, 31, 0)
        self.assertFalse(schedule_profile_should_fire(missed, now=later_min))

    def test_audit_helpers(self) -> None:
        act = custom_audit_activity(35, 70)
        self.assertEqual(act["label"], "活跃 35/70")
        act_full = custom_audit_activity(70, 70)
        self.assertEqual(act_full["label"], "活跃 70/70")
        self.assertEqual(custom_audit_dungeon(None)["label"], "未接")
        self.assertEqual(custom_audit_dungeon({"can_finish": True})["label"], "可交")
        self.assertEqual(custom_audit_dungeon({"is_finished": True})["label"], "可交")
        self.assertEqual(custom_audit_dungeon({"can_finish": False})["label"], "进行中")

    def test_runner_initial_state(self) -> None:
        runner = ScheduleTaskRunner(pid=1)
        self.assertEqual(runner.state, RUNNER_STATE_IDLE)
        self.assertIsNone(runner.current_item)
        self.assertEqual(runner.current_index, -1)

    def test_schedule_passes_hang_snapshot_to_activity_runner(self) -> None:
        from app.core.activity_auto import ActivityConfig, ActivityStepEvent

        seen = {}

        class _CompletedActivity:
            def __init__(self, **kwargs):
                seen.update(kwargs)
                self._event = kwargs["on_event"]

            def start(self):
                self._event(ActivityStepEvent(phase="done", message="done"))

            def is_running(self):
                return False

            def stop(self):
                return True

        runner = ScheduleTaskRunner(
            pid=1,
            role_id="900001",
            hang_settings={
                "hang_mode": 0,
                "hang_ignore_dungeon_stuck": True,
            },
        )
        with mock.patch(
            "app.core.task_schedule.ActivityRunner", _CompletedActivity
        ):
            result = runner._run_activity(ActivityConfig(mode="dungeon"))

        self.assertTrue(result["ok"])
        self.assertEqual(seen["hang_settings"], {
            "hang_mode": 0,
            "hang_ignore_dungeon_stuck": True,
        })
        self.assertEqual(seen["role_id"], "900001")
        self.assertFalse(runner.is_running())
        # pause/resume no-op while not running
        runner.pause()
        runner.resume()
        self.assertEqual(runner.state, RUNNER_STATE_IDLE)

    # ---- activity task end → 队内控 notify once ----

    def _activity_task(self, *, team_control_enabled, hub_role, sends, points=70, target=70):
        """Run _execute_custom_activity with mocked run/points; return (runner, outcome, run_cfgs, events)."""
        from app.core import task_sync, team_chat

        events: list = []
        run_cfgs: list = []

        class _FakeHub:
            def get_role(self, pid):
                return hub_role

        def _fake_send(pid, text, log=None, **kwargs):
            sends.append((pid, text))
            return {"ok": True}

        runner = ScheduleTaskRunner(
            pid=1,
            role_id="900001",
            team_control_enabled=team_control_enabled,
            on_event=events.append,
        )
        runner._read_points = lambda session: points
        runner._run_activity = lambda cfg, *, task_id=0, label="": (
            run_cfgs.append(cfg) or {"phase": "done", "ok": True}
        )
        item = {
            "definition_id": CUSTOM_DEF_ID_ACTIVITY,
            "kind": "activity",
            "name": "刷满活跃并领取全部宝箱",
            "target_points": target,
            "instance_id": 0,
        }
        with _PatchCM(
            [
                mock.patch.object(task_sync, "get_task_sync_hub", lambda: _FakeHub()),
                mock.patch.object(team_chat, "send_team_message", _fake_send),
            ]
        ):
            outcome = runner._execute_custom_activity(object(), item)
        return runner, outcome, run_cfgs, events

    def test_activity_task_notifies_slaves_once_via_team_chat(self) -> None:
        from app.core.task_sync import ROLE_MASTER

        sends: list = []
        runner, outcome, run_cfgs, events = self._activity_task(
            team_control_enabled=True, hub_role=ROLE_MASTER, sends=sends
        )
        self.assertEqual(outcome, "next")
        # 本端领箱仍走 runner 复用的本地领箱函数（claim_awards 保持开启）。
        self.assertTrue(run_cfgs[0].claim_awards)
        # 群控只做通知：整个活跃任务收尾只发一次 [主P]领活跃。
        self.assertEqual(len(sends), 1)
        pid, text = sends[0]
        self.assertEqual(pid, 1)
        self.assertTrue(text.startswith("[主P]领活跃:0@"))
        sync_events = [e for e in events if e.get("phase") == "claim_activity_sync"]
        self.assertEqual(len(sync_events), 1)
        self.assertTrue(sync_events[0].get("ok"))

    def test_activity_task_notify_skips_when_team_control_off(self) -> None:
        from app.core.task_sync import ROLE_MASTER

        sends: list = []
        _, outcome, _, _ = self._activity_task(
            team_control_enabled=False, hub_role=ROLE_MASTER, sends=sends
        )
        self.assertEqual(outcome, "next")
        self.assertEqual(sends, [])

    def test_activity_task_notify_skips_when_not_master(self) -> None:
        from app.core.task_sync import ROLE_SLAVE

        sends: list = []
        _, outcome, _, _ = self._activity_task(
            team_control_enabled=True, hub_role=ROLE_SLAVE, sends=sends
        )
        self.assertEqual(outcome, "next")
        self.assertEqual(sends, [])

    def test_activity_task_notify_skips_when_paused(self) -> None:
        from app.core import task_sync, team_chat
        from app.core.task_sync import ROLE_MASTER

        sends: list = []

        class _FakeHub:
            def get_role(self, pid):
                return ROLE_MASTER

        def _fake_send(pid, text, log=None, **kwargs):
            sends.append((pid, text))
            return {"ok": True}

        runner = ScheduleTaskRunner(
            pid=1,
            role_id="900001",
            team_control_enabled=True,
            on_event=lambda ev: None,
        )
        runner._read_points = lambda session: 70
        runner._run_activity = lambda cfg, *, task_id=0, label="": {
            "phase": "paused",
            "ok": True,
        }
        item = {
            "definition_id": CUSTOM_DEF_ID_ACTIVITY,
            "kind": "activity",
            "name": "刷满活跃并领取全部宝箱",
            "target_points": 70,
            "instance_id": 0,
        }
        with _PatchCM(
            [
                mock.patch.object(task_sync, "get_task_sync_hub", lambda: _FakeHub()),
                mock.patch.object(team_chat, "send_team_message", _fake_send),
            ]
        ):
            outcome = runner._execute_custom_activity(object(), item)
        self.assertEqual(outcome, "pause")
        self.assertEqual(sends, [])

    # ---- fast accepted-task / can_finish audit ----

    def _row(self, tid: int, finished: bool = False) -> object:
        class Row:
            def __init__(self, task_id, is_finished):
                self.task_id = task_id
                self.is_finished = is_finished
                self.is_success = False
                self.can_finish = False
                self.state = 0
                self.progress = None
                self.status_text = ""

            def to_dict(self):
                return {
                    "task_id": self.task_id,
                    "is_finished": self.is_finished,
                    "is_success": self.is_success,
                    "can_finish": self.can_finish,
                    "state": self.state,
                    "progress": self.progress,
                    "status_text": self.status_text,
                }

        return Row(tid, finished)

    def test_accepted_task_map_limits_native_calls(self) -> None:
        from app.core.task_schedule import accepted_task_map, invalidate_can_finish_cache

        session = type("S", (), {"pid": 7001})()
        invalidate_can_finish_cache(7001)
        rows = [self._row(10028), self._row(10027, True), self._row(99999)]
        with mock.patch(
            "app.core.task_schedule.list_accepted_tasks", return_value=rows
        ) as la, mock.patch(
            "app.core.task_schedule.task_can_finish", return_value=True
        ) as cf:
            out = accepted_task_map(
                session, refresh_can_finish=True, task_ids={10028}
            )
        self.assertEqual(set(out), {10028, 10027, 99999})
        # Native CanFinish only for the requested task, not every accepted task.
        cf.assert_called_once()
        self.assertEqual(cf.call_args.args[1], 10028)
        la.assert_called_once()

    def test_accepted_task_map_ttl_cache_and_invalidate(self) -> None:
        from app.core.task_schedule import (
            accepted_task_map,
            invalidate_accepted_list_cache,
            invalidate_can_finish_cache,
        )

        session = type("S", (), {"pid": 7002})()
        invalidate_can_finish_cache(7002)
        invalidate_accepted_list_cache(7002)
        rows = [self._row(10028)]
        with mock.patch(
            "app.core.task_schedule.list_accepted_tasks", return_value=rows
        ) as la, mock.patch(
            "app.core.task_schedule.task_can_finish", return_value=True
        ) as cf:
            accepted_task_map(session, refresh_can_finish=True, task_ids={10028})
            accepted_task_map(session, refresh_can_finish=True, task_ids={10028})
        # Second audit reuses both TTL caches — no extra native/list calls.
        self.assertEqual(cf.call_count, 1)
        self.assertEqual(la.call_count, 1)
        # Invalidation forces a fresh native can_finish call.
        invalidate_can_finish_cache(7002, 10028)
        with mock.patch(
            "app.core.task_schedule.list_accepted_tasks", return_value=rows
        ) as la2, mock.patch(
            "app.core.task_schedule.task_can_finish", return_value=True
        ) as cf2:
            accepted_task_map(session, refresh_can_finish=True, task_ids={10028})
        self.assertEqual(cf2.call_count, 1)
        self.assertEqual(la2.call_count, 0)  # list still cached
        # Force-list refresh re-enumerates (post dungeon-run freshness).
        with mock.patch(
            "app.core.task_schedule.list_accepted_tasks", return_value=rows
        ) as la3, mock.patch(
            "app.core.task_schedule.task_can_finish", return_value=True
        ) as cf3:
            accepted_task_map(
                session, refresh_can_finish=True, task_ids={10028}, force_list=True
            )
        self.assertEqual(la3.call_count, 1)


if __name__ == "__main__":
    unittest.main()
