# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from app.core.team_chat import (
    ACTION_TEXT,
    TeamMsgSeen,
    _role_id_high_byte,
    _role_id_to_ident,
    _strip_render_prefix,
    build_master_command,
    build_master_ping,
    build_master_jianglong_query,
    build_slave_jianglong_status,
    build_slave_pong,
    build_team_chat_c2s,
    extract_sender_name,
    is_team_control_ready,
    is_team_slave_isolated,
    make_msg_id,
    parse_team_message,
    split_msg_id,
    team_control_flag_enabled,
)
from app.core.task_sync import (
    ACTION_ACCEPT,
    ACTION_ACCEPT_DAILY_TASKS,
    ACTION_DAILY_ROUTE,
    ACTION_COMPLETE,
    ACTION_PATH,
    ROLE_MASTER,
    ROLE_NONE,
    ROLE_SLAVE,
)


class TeamControlFlagTests(unittest.TestCase):
    def test_flag_accepts_bool_and_string(self) -> None:
        self.assertTrue(team_control_flag_enabled({"team_control_enabled": True}))
        self.assertTrue(team_control_flag_enabled({"team_control_enabled": "true"}))
        self.assertTrue(team_control_flag_enabled({"team_control_enabled": "1"}))
        self.assertFalse(team_control_flag_enabled({"team_control_enabled": False}))
        self.assertFalse(team_control_flag_enabled({"team_control_enabled": "false"}))
        self.assertFalse(team_control_flag_enabled({"team_control_enabled": "0"}))
        self.assertFalse(team_control_flag_enabled({}))
        self.assertFalse(team_control_flag_enabled(None))

    def test_ready_requires_role(self) -> None:
        base = {"team_control_enabled": True}
        self.assertFalse(is_team_control_ready(base))
        base["task_control_role"] = ROLE_MASTER
        self.assertTrue(is_team_control_ready(base))
        base["task_control_role"] = ROLE_SLAVE
        self.assertTrue(is_team_control_ready(base))
        base["task_control_role"] = ROLE_NONE
        self.assertFalse(is_team_control_ready(base))

    def test_slave_isolated_only_slave(self) -> None:
        base = {"team_control_enabled": True, "task_control_role": ROLE_SLAVE}
        self.assertTrue(is_team_slave_isolated(base))
        base["task_control_role"] = ROLE_MASTER
        self.assertFalse(is_team_slave_isolated(base))
        base["team_control_enabled"] = False
        self.assertFalse(is_team_slave_isolated(base))


class BuildMessageTests(unittest.TestCase):
    def test_build_master_command_accept(self) -> None:
        self.assertEqual(
            build_master_command(ACTION_ACCEPT, [1111, 222], msg_id="0815120000"),
            "[主P]接任务:1111,222@0815120000",
        )
        self.assertEqual(
            build_master_command(ACTION_ACCEPT, 1111, msg_id="0815120000"),
            "[主P]接任务:1111@0815120000",
        )

    def test_build_master_command_complete(self) -> None:
        self.assertEqual(
            build_master_command(ACTION_COMPLETE, 42, msg_id="0815120000"),
            "[主P]交任务:42@0815120000",
        )

    def test_build_and_parse_daily_accept(self) -> None:
        command = build_master_command(
            ACTION_ACCEPT_DAILY_TASKS, 0, msg_id="0815120000"
        )
        self.assertEqual(command, "[主P]接日常:0@0815120000")
        command_msg = parse_team_message(command)
        self.assertEqual(command_msg["action"], ACTION_ACCEPT_DAILY_TASKS)
        self.assertEqual(command_msg["task_ids"], [0])
        reply = build_slave_pong(
            ACTION_ACCEPT_DAILY_TASKS, ok=True, msg_id="0815120000"
        )
        self.assertEqual(reply, "[副G]日done@0815120000")
        reply_msg = parse_team_message(reply)
        self.assertEqual(reply_msg["action"], ACTION_ACCEPT_DAILY_TASKS)
        self.assertTrue(reply_msg["ok"])
    def test_build_and_parse_route_snapshot(self) -> None:
        command = build_master_command(
            ACTION_PATH,
            10025,
            route_snapshot={
                "portal_x": -22.29,
                "portal_y": 60.75,
                "portal_z": -85.58,
                "origin_scene_id": 68,
                "portal_tid": 100219,
                "portal_obj_id": 12345,
            },
            msg_id="0815120000",
        )
        parsed = parse_team_message(command)
        self.assertEqual(parsed["action"], ACTION_PATH)
        self.assertAlmostEqual(parsed["portal_x"], -22.29)
        self.assertEqual(parsed["origin_scene_id"], 68)
        self.assertEqual(parsed["portal_tid"], 100219)
        self.assertEqual(parsed["portal_obj_id"], 12345)

    def test_build_and_parse_daily_route_phase(self) -> None:
        command = build_master_command(
            ACTION_DAILY_ROUTE,
            10011,
            route_snapshot={
                "phase": "target",
                "target_scene_id": 2030,
                "target_tid": 101013,
                "target_x": 1.0,
                "target_y": 2.0,
                "target_z": 3.0,
                "target_round": 2,
            },
            msg_id="0815120001",
        )
        parsed = parse_team_message(command)
        self.assertEqual(parsed["action"], ACTION_DAILY_ROUTE)
        self.assertEqual(parsed["phase"], "target")
        self.assertEqual(parsed["target_scene_id"], 2030)
        self.assertEqual(parsed["target_tid"], 101013)
        self.assertIsNone(parsed["target_round"])

    def test_daily_route_team_message_stays_under_chat_limit(self) -> None:
        command = build_master_command(
            ACTION_DAILY_ROUTE,
            10010,
            route_snapshot={
                "phase": "portal_arrive",
                "origin_scene_id": 68,
                "target_scene_id": 2036,
                "portal_tid": 100219,
                "portal_obj_id": 72057594037929946,
                "portal_x": -22.291519165039062,
                "portal_y": 60.756717681884766,
                "portal_z": -85.586669921875,
            },
            msg_id="0824221148",
        )
        self.assertLessEqual(len(command.encode("utf-8")), 255)
        parsed = parse_team_message(command)
        self.assertEqual(parsed["phase"], "portal_arrive")
        self.assertEqual(parsed["portal_tid"], 100219)
        self.assertEqual(parsed["target_scene_id"], 2036)
        self.assertAlmostEqual(parsed["portal_x"], -22.3, places=1)

    def test_build_auto_msg_id(self) -> None:
        auto = build_master_command(ACTION_ACCEPT, [1])
        self.assertRegex(auto, r"^\[主P\]接任务:1@\d{10}$")

    def test_build_jianglong_query_and_status(self) -> None:
        query = build_master_jianglong_query(test_mode=True, msg_id="0815120000")
        parsed_query = parse_team_message(query)
        self.assertEqual(parsed_query["kind"], "jianglong_query")
        self.assertTrue(parsed_query["test_mode"])
        status = build_slave_jianglong_status(
            True, False, True, msg_id="0815120000"
        )
        parsed_status = parse_team_message(status)
        self.assertEqual(parsed_status["kind"], "jianglong_status")
        self.assertTrue(parsed_status["enabled"])
        self.assertFalse(parsed_status["in_wuzun"])
        self.assertTrue(parsed_status["hang_running"])
        self.assertEqual(parsed_status["msg_id"], "0815120000")

    def test_build_ping_and_pong(self) -> None:
        self.assertEqual(
            build_master_ping(msg_id="0815120000"), "[主P]PING@0815120000"
        )
        self.assertEqual(
            build_slave_pong(msg_id="0815120000"), "[副G]PONG@0815120000"
        )
        self.assertEqual(
            build_slave_pong(ACTION_ACCEPT, msg_id="0815120000"),
            "[副G]接pong@0815120000",
        )
        self.assertEqual(
            build_slave_pong(ACTION_COMPLETE, msg_id="0815120000"),
            "[副G]交pong@0815120000",
        )

    def test_make_msg_id_format(self) -> None:
        self.assertRegex(make_msg_id(), r"^\d{10}$")


class ParseMessageTests(unittest.TestCase):
    def test_parse_master_accept_command(self) -> None:
        msg = parse_team_message("[主P]接任务:1111,222")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["kind"], "command")
        self.assertEqual(msg["role"], "master")
        self.assertEqual(msg["action"], ACTION_ACCEPT)
        self.assertEqual(msg["task_ids"], [1111, 222])

    def test_parse_master_ping(self) -> None:
        msg = parse_team_message("[主P]PING")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["kind"], "ping")
        self.assertEqual(msg["role"], "master")

    def test_parse_slave_pong(self) -> None:
        msg = parse_team_message("[副G]PONG")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["kind"], "pong")
        self.assertEqual(msg["role"], "slave")
        self.assertIsNone(msg["action"])

    def test_parse_slave_accept_pong(self) -> None:
        msg = parse_team_message("[副G]接pong")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["kind"], "pong")
        self.assertEqual(msg["role"], "slave")
        self.assertEqual(msg["action"], ACTION_ACCEPT)

    def test_parse_ignores_unrelated(self) -> None:
        for text in ("", "hello", "[世界]foo", "组队分配方式更换", None):
            self.assertIsNone(parse_team_message(text))

    def test_roundtrip_accept(self) -> None:
        text = build_master_command(ACTION_ACCEPT, [1111, 222], msg_id="0815120000")
        msg = parse_team_message(text)
        self.assertEqual(msg["action"], ACTION_ACCEPT)
        self.assertEqual(msg["task_ids"], [1111, 222])
        self.assertEqual(msg["msg_id"], "0815120000")

    def test_parse_with_msg_id(self) -> None:
        msg = parse_team_message("[主P]接任务:1111,222@0815123456")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["kind"], "command")
        self.assertEqual(msg["msg_id"], "0815123456")
        self.assertEqual(msg["task_ids"], [1111, 222])

    def test_parse_ping_msg_id(self) -> None:
        msg = parse_team_message("[主P]PING@0815123456")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["kind"], "ping")
        self.assertEqual(msg["msg_id"], "0815123456")

    def test_parse_pong_msg_id(self) -> None:
        msg = parse_team_message("[副G]接pong@0815123456")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["kind"], "pong")
        self.assertEqual(msg["action"], ACTION_ACCEPT)
        self.assertEqual(msg["msg_id"], "0815123456")

    def test_parse_slave_done(self) -> None:
        msg = parse_team_message("[副G]接done@0815123456")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["kind"], "done")
        self.assertEqual(msg["role"], "slave")
        self.assertEqual(msg["action"], ACTION_ACCEPT)
        self.assertTrue(msg["ok"])
        self.assertEqual(msg["msg_id"], "0815123456")

    def test_parse_slave_fail(self) -> None:
        msg = parse_team_message("[副G]接fail@0815123456")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["kind"], "fail")
        self.assertEqual(msg["role"], "slave")
        self.assertEqual(msg["action"], ACTION_ACCEPT)
        self.assertIs(msg["ok"], False)
        self.assertEqual(msg["msg_id"], "0815123456")

    def test_parse_slave_done_heartbeat(self) -> None:
        msg = parse_team_message("[副G]DONE@0815123456")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["kind"], "done")
        self.assertIsNone(msg["action"])

    def test_build_slave_done_fail(self) -> None:
        self.assertEqual(
            build_slave_pong(ACTION_ACCEPT, ok=True, msg_id="0815120000"),
            "[副G]接done@0815120000",
        )
        self.assertEqual(
            build_slave_pong(ACTION_ACCEPT, ok=False, msg_id="0815120000"),
            "[副G]接fail@0815120000",
        )
        # 心跳（无 action，ok=None）→ 大写 PONG
        self.assertEqual(
            build_slave_pong(msg_id="0815120000"), "[副G]PONG@0815120000"
        )

    def test_split_msg_id(self) -> None:
        self.assertEqual(split_msg_id("接任务:111,222@0815123456"),
                         ("接任务:111,222", "0815123456"))
        self.assertEqual(split_msg_id("PING"), ("PING", ""))
        self.assertEqual(split_msg_id(""), ("", ""))
        self.assertEqual(split_msg_id(None), ("", ""))
        # 非 13 位数字尾巴不剥离
        self.assertEqual(split_msg_id("接任务:1@12"), ("接任务:1@12", ""))

    def test_parse_with_render_prefix(self) -> None:
        # AddChatMessage 文本带聊天渲染前缀：^u&名字&^u：正文
        raw = "^u&#      1003张三&^u：[主P]接任务:111,222"
        self.assertEqual(
            _strip_render_prefix(raw), "[主P]接任务:111,222"
        )
        msg = parse_team_message(raw)
        self.assertIsNotNone(msg)
        self.assertEqual(msg["kind"], "command")
        self.assertEqual(msg["action"], ACTION_ACCEPT)
        self.assertEqual(msg["task_ids"], [111, 222])

    def test_parse_ping_with_render_prefix(self) -> None:
        raw = "^u&队长甲&^u：[主P]PING"
        msg = parse_team_message(raw)
        self.assertIsNotNone(msg)
        self.assertEqual(msg["kind"], "ping")

    def test_strip_render_prefix_plain(self) -> None:
        self.assertEqual(_strip_render_prefix("[主P]接任务:1"), "[主P]接任务:1")
        self.assertEqual(_strip_render_prefix(""), "")
        self.assertEqual(_strip_render_prefix(None), "")

    def test_parse_includes_sender(self) -> None:
        # 渲染前缀带行号：#      <行号><名字>
        msg = parse_team_message("^u&#      1002张三&^u：[主P]接任务:111,222")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["sender"], "张三")

    def test_parse_sender_with_plain_name(self) -> None:
        msg = parse_team_message("^u&赵六&^u：[主P]PING")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["sender"], "赵六")

    def test_extract_sender_name(self) -> None:
        self.assertEqual(extract_sender_name("^u&#      1002张三&^u：大家好"), "张三")
        self.assertEqual(extract_sender_name("^u&赵六&^u^00FF40在..."), "赵六")
        self.assertEqual(extract_sender_name("[主P]PING"), "")
        self.assertEqual(extract_sender_name(""), "")
        self.assertEqual(extract_sender_name(None), "")

    def test_action_text_map_covers_sync_actions(self) -> None:
        self.assertIn(ACTION_ACCEPT, ACTION_TEXT)
        self.assertIn(ACTION_COMPLETE, ACTION_TEXT)


class TeamMsgSeenTests(unittest.TestCase):
    def test_consume_once(self) -> None:
        seen = TeamMsgSeen()
        msg = parse_team_message("[主P]PING@0815123456")
        self.assertTrue(seen.consume(msg))
        self.assertFalse(seen.consume(msg))

    def test_no_msg_id_always_passes(self) -> None:
        seen = TeamMsgSeen()
        m1 = parse_team_message("[主P]PING")
        m2 = parse_team_message("[主P]PING")
        self.assertIsNotNone(m1)
        self.assertIsNotNone(m2)
        self.assertTrue(seen.consume(m1))
        self.assertTrue(seen.consume(m2))

    def test_role_partitions(self) -> None:
        seen = TeamMsgSeen()
        self.assertTrue(seen.consume({"role": "master", "msg_id": "1"}))
        self.assertTrue(seen.consume({"role": "slave", "msg_id": "1"}))
        self.assertFalse(seen.consume({"role": "master", "msg_id": "1"}))
        self.assertTrue(seen.consume({"role": "slave", "msg_id": "2"}))

    def test_same_slave_message_id_from_different_senders_passes(self) -> None:
        seen = TeamMsgSeen()
        first = {"role": "slave", "sender": "王五", "msg_id": "0821123617"}
        second = {"role": "slave", "sender": "李四", "msg_id": "0821123617"}
        self.assertTrue(seen.consume(first))
        self.assertTrue(seen.consume(second))
        self.assertFalse(seen.consume(first))

    def test_same_slave_message_id_without_sender_uses_event_seq(self) -> None:
        seen = TeamMsgSeen()
        first = {"role": "slave", "msg_id": "0821123617", "seq": 101}
        second = {"role": "slave", "msg_id": "0821123617", "seq": 102}
        self.assertTrue(seen.consume(first))
        self.assertTrue(seen.consume(second))
        self.assertFalse(seen.consume(first))

    def test_capacity_eviction(self) -> None:
        seen = TeamMsgSeen(capacity=3)
        msgs = [{"role": "master", "msg_id": str(i)} for i in range(5)]
        for m in msgs:
            self.assertTrue(seen.consume(m))
        # 最旧的 key 被淘汰，可再次消费
        self.assertTrue(seen.consume(msgs[0]))

    def test_reset(self) -> None:
        seen = TeamMsgSeen()
        msg = {"role": "master", "msg_id": "1"}
        self.assertTrue(seen.consume(msg))
        seen.reset()
        self.assertTrue(seen.consume(msg))

    def test_none_msg_passes(self) -> None:
        seen = TeamMsgSeen()
        self.assertTrue(seen.consume(None))
        self.assertTrue(seen.consume({}))


class BuildPacketTests(unittest.TestCase):
    """聊天 c2s 封包构建（2026-08-15 实机抓包对照）。"""

    def test_build_matches_captured_short(self) -> None:
        # 实机抓包（2026-08-16）：张三发 "测试abc123" → 4f 2c 0000000001 2b4001 ...
        expected = (
            "4f2c00000000012b400100000003000000000110"
            "4b6dd58b610062006300310032003300"
            "00000000000000000000"
        )
        self.assertEqual(build_team_chat_c2s("测试abc123").hex(), expected)

    def test_build_matches_captured_long(self) -> None:
        # 实机抓包（2026-08-16）：张三发 "测试测试测试abc123456"
        expected = (
            "4f3a00000000012b40010000000300000000011e"
            "4b6dd58b4b6dd58b4b6dd58b6100620063003100320033003400350036"
            "0000000000000000000000"
        )
        self.assertEqual(build_team_chat_c2s("测试测试测试abc123456").hex(), expected)

    def test_build_high_byte_by_role(self) -> None:
        # offset 6 = (rid>>24)&0xFF，随角色变化（2026-08-16 抓包确认）。
        # 张三 0x01001001 → offset 6=01；李四 0x00004001 → offset 6=00。
        p1 = build_team_chat_c2s("X", ident=_role_id_to_ident(0x01001001),
                                 high_byte=_role_id_high_byte(0x01001001))
        self.assertEqual(p1[6], 0x01)
        self.assertEqual(p1[7:10], bytes([0x00, 0x10, 0x01]))
        p2 = build_team_chat_c2s("X", ident=_role_id_to_ident(0x00004001),
                                 high_byte=_role_id_high_byte(0x00004001))
        self.assertEqual(p2[6], 0x00)
        self.assertEqual(p2[7:10], bytes([0x00, 0x40, 0x01]))

    def test_build_ascii(self) -> None:
        pkt = build_team_chat_c2s("333")
        self.assertEqual(pkt[0], 0x4F)
        self.assertEqual(pkt[0x13], 6)  # "333" UTF-16LE = 6 字节
        self.assertEqual(pkt[0x14:0x1A], "333".encode("utf-16le"))

    def test_build_auto_length(self) -> None:
        pkt = build_team_chat_c2s("123456")
        self.assertEqual(pkt[1], len(pkt) - 2)  # total = 包长 - 2

    def test_build_rejects_too_long(self) -> None:
        with self.assertRaises(ValueError):
            build_team_chat_c2s("x" * 200)


if __name__ == "__main__":
    unittest.main()
