# -*- coding: utf-8 -*-
"""私聊控制面（组队前预检查/离队）协议层测试。

显示层样本来自 2026-08-29 实机收包（chat_tap ch=9 原文）。
@author by ak
"""
import unittest
from unittest.mock import patch

from app.core.private_team_link import (
    build_master_pleave,
    build_slave_pleft,
    handle_pleave_commands,
    match_known_sender,
    notify_slaves_leave,
    parse_master_pleave,
    parse_slave_pleft,
    split_private_display,
)
from app.core.team_chat import make_msg_id

# 实机显示层原文（发送方带 ^u&# 噪声包装）
DISPLAY_RECV_1 = "^u&#     44385十丶三&^u 对你说：PRIV-OK1"
DISPLAY_RECV_2 = "^u&#     44401初一&^u 对你说：PRIV-OK2"


class BuildParseTests(unittest.TestCase):
    def test_pleave_roundtrip_with_render_noise(self) -> None:
        msg_id = make_msg_id()
        raw = f"{DISPLAY_RECV_1.rsplit('：', 1)[0]}对你说：{build_master_pleave(msg_id)}"
        p = parse_master_pleave(raw, ["十丶三", "初一", "苦寒未曾来"])
        self.assertIsNotNone(p)
        self.assertEqual(p["sender"], "十丶三")
        self.assertEqual(p["msg_id"], msg_id)

    def test_pleft_roundtrip_with_render_noise(self) -> None:
        msg_id = make_msg_id()
        raw = f"{DISPLAY_RECV_2.rsplit('：', 1)[0]}对你说：{build_slave_pleft(msg_id, ok=True)}"
        p = parse_slave_pleft(raw, ["十丶三", "初一"])
        self.assertIsNotNone(p)
        self.assertEqual(p["sender"], "初一")
        self.assertEqual(p["msg_id"], msg_id)
        self.assertTrue(p["ok"])

    def test_pleft_ok_zero(self) -> None:
        msg_id = make_msg_id()
        raw = f"某某&^u 对你说：{build_slave_pleft(msg_id, ok=False)}"
        p = parse_slave_pleft(raw, ["某某"])
        self.assertIsNotNone(p)
        self.assertFalse(p["ok"])

    def test_rejects_plain_chat(self) -> None:
        self.assertIsNone(parse_master_pleave(DISPLAY_RECV_1, ["十丶三"]))
        self.assertIsNone(parse_slave_pleft(DISPLAY_RECV_2, ["初一"]))

    def test_sender_must_be_known(self) -> None:
        msg_id = make_msg_id()
        raw = f"陌生人&^u 对你说：{build_master_pleave(msg_id)}"
        p = parse_master_pleave(raw, ["十丶三"])
        self.assertIsNotNone(p)
        self.assertEqual(p["sender"], "")

    def test_split_display_local_echo(self) -> None:
        sender, body = split_private_display("你对 ^u&#  0初一&^u 说：hi")
        self.assertEqual(body, "hi")
        self.assertIn("初一", sender)


class MatchKnownSenderTests(unittest.TestCase):
    def test_matches_real_display_noise(self) -> None:
        self.assertEqual(
            match_known_sender("^u&#     44385十丶三&^u", ["十丶三"]), "十丶三"
        )

    def test_longest_name_wins(self) -> None:
        self.assertEqual(
            match_known_sender("noise唐家军1&^u", ["唐家军", "唐家军1"]), "唐家军1"
        )

    def test_unknown_returns_empty(self) -> None:
        self.assertEqual(match_known_sender("^u&#99路人&^u", ["十丶三"]), "")


class NotifySlavesLeaveTests(unittest.TestCase):
    """生产主控路径：只管发，不等回执。@author by ak"""

    def test_sends_to_each_target_without_waiting(self) -> None:
        sent: list[tuple[int, int, str, str]] = []

        def fake_send(pid, rid, name, text, *, log=None):
            sent.append((pid, rid, name, text))
            return {"ok": True}

        with patch("app.core.private_team_link.send_private_message", fake_send):
            n = notify_slaves_leave(
                6456,
                [
                    {"name": "初一", "obj_id": 0x012BA001},
                    {"name": "", "obj_id": 1},  # 非法条目跳过
                    {"name": "苦寒未曾来", "obj_id": 647169},
                ],
                log=lambda m: None,
            )
        self.assertEqual(n, 2)
        self.assertEqual([s[2] for s in sent], ["初一", "苦寒未曾来"])
        for _pid, _rid, _name, text in sent:
            self.assertTrue(text.startswith("[主P]PLEAVE@"))

    def test_counts_only_successful_sends(self) -> None:
        def fake_send(pid, rid, name, text, *, log=None):
            return {"ok": False, "error": "mailbox busy"}

        with patch("app.core.private_team_link.send_private_message", fake_send):
            n = notify_slaves_leave(
                6456, [{"name": "初一", "obj_id": 0x012BA001}], log=lambda m: None
            )
        self.assertEqual(n, 0)


class HandlePleaveTests(unittest.TestCase):
    def _run(self, poll_texts, leave_result, *, reply=True):
        calls: list[dict] = []

        class FakeWatch:
            def poll_messages(self):
                return [{"text": t, "channel": 9} for t in poll_texts]

        def fake_leave(session, *, log=None):
            return leave_result

        def fake_send(pid, rid, name, text, *, log=None):
            calls.append({"pid": pid, "rid": rid, "name": name, "text": text})
            return {"ok": True}

        roster = [
            {"name": "十丶三", "obj_id": 0x012B4001},
            {"name": "初一", "obj_id": 0x012BA001},
        ]
        with (
            patch("app.core.private_team_link.send_private_message", fake_send),
            patch("app.core.private_team_link.leave_team", fake_leave, create=True),
        ):
            n = handle_pleave_commands(
                35952,
                session=object(),
                watch=FakeWatch(),
                roster=roster,
                seen=set(),
                leave_fn=fake_leave,
                reply=reply,
                log=lambda m: None,
            )
        return n, calls

    def test_dispatches_leave_and_replies_to_sender(self) -> None:
        msg_id = make_msg_id()
        raw = f"{DISPLAY_RECV_1.rsplit('：', 1)[0]}对你说：{build_master_pleave(msg_id)}"
        n, calls = self._run([raw], type("R", (), {"ok": True})())
        self.assertEqual(n, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["rid"], 0x012B4001)
        self.assertEqual(calls[0]["name"], "十丶三")
        self.assertIn(f"PLEFT@{msg_id}", calls[0]["text"])
        self.assertIn("OK=1", calls[0]["text"])

    def test_dedups_same_msg_id(self) -> None:
        msg_id = make_msg_id()
        raw = f"{DISPLAY_RECV_1.rsplit('：', 1)[0]}对你说：{build_master_pleave(msg_id)}"
        # 同一批 poll 里出现两次同 msg_id（重复渲染）只处理一次。
        n, calls = self._run([raw, raw], type("R", (), {"ok": True})())
        self.assertEqual(n, 1)
        self.assertEqual(len(calls), 1)

    def test_ignores_unknown_sender(self) -> None:
        msg_id = make_msg_id()
        raw = f"陌生人&^u 对你说：{build_master_pleave(msg_id)}"
        n, calls = self._run([raw], type("R", (), {"ok": True})())
        self.assertEqual(n, 0)
        self.assertEqual(calls, [])

    def test_production_reply_off_still_executes_leave(self) -> None:
        msg_id = make_msg_id()
        raw = f"{DISPLAY_RECV_1.rsplit('：', 1)[0]}对你说：{build_master_pleave(msg_id)}"
        n, calls = self._run(
            [raw], type("R", (), {"ok": True})(), reply=False
        )
        self.assertEqual(n, 1)  # 离队照常执行
        self.assertEqual(calls, [])  # 不回 PLEFT

    def test_default_leave_fn_resolves_to_team_ops(self) -> None:
        """不传 leave_fn 时必须解析到 team_ops.leave_team（回归：NoneType 崩溃）。"""
        msg_id = make_msg_id()
        raw = f"{DISPLAY_RECV_1.rsplit('：', 1)[0]}对你说：{build_master_pleave(msg_id)}"
        leave_calls: list = []

        class FakeWatch:
            def poll_messages(self):
                return [{"text": raw, "channel": 9, "tick_ms": 0}]

        def fake_leave_team(session, *, log=None):
            leave_calls.append(session)
            return type("R", (), {"ok": True})()

        roster = [{"name": "十丶三", "obj_id": 0x012B4001}]
        with (
            patch("app.core.private_team_link.send_private_message"),
            patch("app.core.team_ops.leave_team", fake_leave_team),
        ):
            n = handle_pleave_commands(
                35952,
                session=object(),
                watch=FakeWatch(),
                roster=roster,
                seen=set(),
                reply=False,
                log=lambda m: None,
            )
        self.assertEqual(n, 1)
        self.assertEqual(len(leave_calls), 1)

    def test_stale_pleave_is_skipped(self) -> None:
        """超过 PRIVATE_LEAVE_STALE_S 的旧命令不执行（重启重放防护）。"""
        from app.core.private_team_link import PRIVATE_LEAVE_STALE_S
        from app.core.team_chat import _now_tick_ms

        msg_id = make_msg_id()
        raw = f"{DISPLAY_RECV_1.rsplit('：', 1)[0]}对你说：{build_master_pleave(msg_id)}"
        stale_tick = (_now_tick_ms() - int(PRIVATE_LEAVE_STALE_S * 1000) - 5000) & 0xFFFFFFFF
        leave_calls: list = []

        class FakeWatch:
            def poll_messages(self):
                return [
                    {"text": raw, "channel": 9, "tick_ms": stale_tick},
                    {"text": raw, "channel": 9, "tick_ms": _now_tick_ms()},
                ]

        def fake_leave_team(session, *, log=None):
            leave_calls.append(session)
            return type("R", (), {"ok": True})()

        roster = [{"name": "十丶三", "obj_id": 0x012B4001}]
        with (
            patch("app.core.private_team_link.send_private_message"),
            patch("app.core.team_ops.leave_team", fake_leave_team),
        ):
            n = handle_pleave_commands(
                35952,
                session=object(),
                watch=FakeWatch(),
                roster=roster,
                seen=set(),
                reply=False,
                log=lambda m: None,
            )
        # 旧的被时效丢弃，新的执行一次（msg_id 相同，旧的那条没进 seen）。
        self.assertEqual(n, 1)
        self.assertEqual(len(leave_calls), 1)

    def test_module_seen_dedups_across_instances(self) -> None:
        """同一 pid 的多个页面实例共享去重：同一条命令只执行一次。"""
        from app.core.private_team_link import private_seen_reset

        private_seen_reset()
        msg_id = make_msg_id()
        raw = f"{DISPLAY_RECV_1.rsplit('：', 1)[0]}对你说：{build_master_pleave(msg_id)}"
        leave_calls: list = []

        class FakeWatch:
            def poll_messages(self):
                return [{"text": raw, "channel": 9, "tick_ms": 0}]

        def fake_leave_team(session, *, log=None):
            leave_calls.append(session)
            return type("R", (), {"ok": True})()

        roster = [{"name": "十丶三", "obj_id": 0x012B4001}]
        common = dict(
            session=object(),
            watch=FakeWatch(),
            roster=roster,
            reply=False,
            log=lambda m: None,
        )
        with (
            patch("app.core.private_team_link.send_private_message"),
            patch("app.core.team_ops.leave_team", fake_leave_team),
        ):
            n1 = handle_pleave_commands(35952, seen=None, **common)
            n2 = handle_pleave_commands(35952, seen=None, **common)  # 另一实例
        private_seen_reset()
        self.assertEqual((n1, n2), (1, 0))
        self.assertEqual(len(leave_calls), 1)

    def test_lazy_session_factory(self) -> None:
        """session 传惰性工厂：只在收到命令时挂载；失败则离队按失败处理。"""
        msg_id = make_msg_id()
        raw = f"{DISPLAY_RECV_1.rsplit('：', 1)[0]}对你说：{build_master_pleave(msg_id)}"
        calls: list = []

        class FakeWatch:
            def poll_messages(self):
                return [{"text": raw, "channel": 9, "tick_ms": 0}]

        def fake_leave_team(session, *, log=None):
            calls.append(("leave", session))
            return type("R", (), {"ok": True})()

        roster = [{"name": "十丶三", "obj_id": 0x012B4001}]
        with (
            patch("app.core.private_team_link.send_private_message"),
            patch("app.core.team_ops.leave_team", fake_leave_team),
        ):
            # 工厂正常返回
            n1 = handle_pleave_commands(
                35952,
                session=lambda: "ATTACHED",
                watch=FakeWatch(),
                roster=roster,
                seen=set(),
                reply=False,
                log=lambda m: None,
            )
            # 工厂失败
            def boom():
                raise RuntimeError("attach fail")

            n2 = handle_pleave_commands(
                35952,
                session=boom,
                watch=FakeWatch(),
                roster=roster,
                seen=set(),
                reply=False,
                log=lambda m: None,
            )
        self.assertEqual(n1, 1)
        self.assertEqual(calls, [("leave", "ATTACHED")])
        self.assertEqual(n2, 1)  # 计数包含，但离队未执行


if __name__ == "__main__":
    unittest.main()
