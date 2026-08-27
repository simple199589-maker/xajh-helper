from __future__ import annotations

import unittest
import struct
from types import SimpleNamespace
from unittest.mock import patch


class TeamOpsTests(unittest.TestCase):
    def test_batch_invite_can_send_exactly_one_round_without_defaults(self) -> None:
        from app.core.team_ops import TeamMemberTarget, TeamOpResult, invite_members_by_targets

        target = TeamMemberTarget(name="队员甲", obj_id=101, source="multi")
        sent = TeamOpResult(ok=True, action="invite", message="sent")
        with patch(
            "app.core.plg_ui.host_team_role",
            return_value={"in_team": False, "role": "solo"},
        ), patch(
            "app.core.team_ops.read_host_identity", return_value=("队长", 1)
        ), patch(
            "app.core.team_ops.invite_player_by_name", return_value=sent
        ) as invite, patch(
            "app.core.team_ops.apply_captain_team_defaults"
        ) as defaults:
            result = invite_members_by_targets(
                object(),
                [target],
                skip_already_in_party=False,
                pause_s=0.0,
                rounds=1,
                apply_defaults=False,
            )
        self.assertTrue(result.ok)
        invite.assert_called_once()
        defaults.assert_not_called()

    def test_team_follow_rejects_current_character_when_not_in_team(self) -> None:
        from app.core.team_ops import set_team_follow

        with patch("app.core.team_ops._pid_blocked", return_value=(False, "")), patch(
            "app.core.plg_ui.is_host_in_team", return_value=False
        ), patch("app.core.team_ops.remote_call_thiscall_x86") as remote:
            result = set_team_follow(object(), enabled=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_in_team")
        remote.assert_not_called()

    def test_team_follow_ret_zero_is_not_reported_as_success(self) -> None:
        from app.core.team_ops import set_team_follow

        session = type("Session", (), {"pid": 9, "module_base": 0x400000})()
        with patch("app.core.team_ops._pid_blocked", return_value=(False, "")), patch(
            "app.core.plg_ui.is_host_in_team", return_value=True
        ), patch(
            "app.core.team_ops.get_host_player_for_team", return_value=0x1234
        ), patch("app.core.xajh_bridge.ensure_bridge", return_value=None), patch(
            "app.core.team_ops.remote_call_thiscall_x86", return_value=0
        ):
            result = set_team_follow(session, enabled=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "follow_ret0")

    def test_team_follow_prefers_ui_callback(self) -> None:
        from app.core.team_ops import set_team_follow

        session = type("Session", (), {"pid": 9, "module_base": 0x400000})()
        bridge = type(
            "Bridge",
            (), {"team_follow": lambda _self, *_args, **_kw: type("R", (), {"ok": True, "ret": 1, "note": "ok"})()},
        )()
        with patch("app.core.team_ops._pid_blocked", return_value=(False, "")), patch(
            "app.core.plg_ui.is_host_in_team", return_value=True
        ), patch(
            "app.core.team_ops.get_host_player_for_team", return_value=0x1234
        ), patch("app.core.xajh_bridge.ensure_bridge", return_value=bridge), patch(
            "app.core.team_ops.remote_call_thiscall_x86"
        ) as remote:
            result = set_team_follow(session, enabled=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.detail["path"], "bridge_ui_callback")
        remote.assert_not_called()

    def test_follow_ui_click_uses_confirm_button_center(self) -> None:
        from app.core.team_ops import (
            FOLLOW_OK_FRAC,
            _click_follow_button_point,
        )

        rect = SimpleNamespace(ok=True, x=100, y=200, w=290, h=113, error=None)
        with patch("app.core.aui_click.read_aui_ctrl_rect", return_value=rect), patch(
            "app.core.aui_click.click_client_bg", return_value=True
        ) as click:
            ok = _click_follow_button_point(
                object(), 99, 0x1234, *FOLLOW_OK_FRAC, label="ok"
            )

        self.assertTrue(ok)
        self.assertEqual(click.call_args_list[0].args[1:4], (99, 187, 281))

    def test_follow_ui_click_reports_bad_dialog_rect_as_failure(self) -> None:
        from app.core.team_ops import _click_follow_button_point

        rect = SimpleNamespace(ok=False, x=0, y=0, w=0, h=0, error="missing")
        with patch("app.core.aui_click.read_aui_ctrl_rect", return_value=rect), patch(
            "app.core.aui_click.click_client_bg"
        ) as click:
            result = _click_follow_button_point(
                object(), 99, 0x1234, 0.3, 0.72, label="ok"
            )

        self.assertFalse(result)
        click.assert_not_called()

    def test_follow_ui_click_requires_confirm_dialog_to_close(self) -> None:
        from app.core.team_ops import click_team_follow_button

        session = type("Session", (), {"pid": 9, "module_base": 0x400000})()
        with patch("app.core.team_ops._resolve_main_hwnd", return_value=99), patch(
            "app.core.team_ops._find_shown_dialog", side_effect=[0x1234, 0]
        ), patch(
            "app.core.team_ops._click_follow_button_point", return_value=True
        ) as click:
            result = click_team_follow_button(session, enabled=True)

        self.assertTrue(result)
        self.assertEqual(click.call_args.args[3:5], (0.30, 0.72))

    def test_follow_ui_cancel_uses_bind_status_path(self) -> None:
        from app.core.team_ops import click_team_follow_button

        with patch(
            "app.core.team_ops.click_team_follow_cancel_status", return_value=True
        ) as cancel_status, patch("app.core.team_ops._resolve_main_hwnd") as hwnd:
            result = click_team_follow_button(object(), enabled=False)

        self.assertTrue(result)
        cancel_status.assert_called_once()
        hwnd.assert_not_called()

    def test_follow_ui_cancel_clicks_only_visible_bind_status_row(self) -> None:
        from app.core.team_ops import click_team_follow_cancel_status

        button_rect = SimpleNamespace(ok=True, center=(229, 236), error=None)
        rows = [
            {
                "row": 1,
                "tip": 0x1000,
                "text": "组队跟随",
                "button": 0x2000,
                "shown": True,
                "rect": button_rect,
            }
        ]
        with patch("app.core.team_ops._resolve_main_hwnd", return_value=99), patch(
            "app.core.plg_ui.get_game_ui_dlg", return_value=0x1234
        ), patch("app.core.plg_ui.is_dlg_show", return_value=True), patch(
            "app.core.team_ops._collect_follow_status_rows", return_value=rows
        ), patch("app.core.aui_click.click_client_bg", return_value=True) as click, patch(
            "app.core.team_ops._team_ui_ctrl_shown", return_value=False
        ):
            result = click_team_follow_cancel_status(object())

        self.assertTrue(result)
        self.assertEqual(click.call_args.args[1:4], (99, 229, 236))

    def test_follow_visibility_uses_game_export(self) -> None:
        from app.core.team_ops import _team_ui_ctrl_shown

        with patch(
            "app.core.activity_auto._aui_obj_is_show", return_value=True
        ):
            self.assertTrue(_team_ui_ctrl_shown(object(), 0x2000))

    def test_follow_quit_rect_uses_live_button_geometry(self) -> None:
        from app.core.team_ops import _read_follow_quit_rect

        raw = bytearray(0xA4)
        struct.pack_into("<I", raw, 0x10, 0x1234)
        struct.pack_into("<ffff", raw, 0x94, 117.0, 27.0, 22.0, 22.0)
        dlg_rect = SimpleNamespace(ok=True, x=1218, y=665, error=None)
        with patch(
            "app.core.team_ops._read_team_ui_bytes", return_value=bytes(raw)
        ), patch("app.core.aui_click.read_aui_ctrl_rect", return_value=dlg_rect):
            rect = _read_follow_quit_rect(
                object(), 0x1234, 0x2000, name="Btn_Quit1"
            )

        self.assertTrue(rect.ok)
        self.assertEqual((rect.x, rect.y, rect.w, rect.h), (1335, 692, 22, 22))
        self.assertEqual(rect.center, (1346, 703))

    def test_follow_ui_cancel_rejects_missing_live_button_rect(self) -> None:
        from app.core.team_ops import click_team_follow_cancel_status

        rows = [
            {
                "row": 1,
                "tip": 0x1000,
                "text": "组队跟随",
                "button": 0x2000,
                "shown": True,
                "rect": SimpleNamespace(ok=False, error="bad geometry"),
            }
        ]
        with patch("app.core.team_ops._resolve_main_hwnd", return_value=99), patch(
            "app.core.plg_ui.get_game_ui_dlg", return_value=0x1234
        ), patch("app.core.plg_ui.is_dlg_show", return_value=True), patch(
            "app.core.team_ops._collect_follow_status_rows", return_value=rows
        ), patch("app.core.aui_click.click_client_bg") as click:
            result = click_team_follow_cancel_status(object())

        self.assertFalse(result)
        click.assert_not_called()

    def test_team_follow_function_path_skips_ui_click(self) -> None:
        from app.core.team_ops import set_team_follow

        session = type("Session", (), {"pid": 9, "module_base": 0x400000})()
        bridge = type(
            "Bridge",
            (),
            {
                "team_follow": lambda _self, *_args, **_kw: type(
                    "R", (), {"ok": True, "ret": 1, "note": "ok"}
                )()
            },
        )()
        with patch("app.core.team_ops.click_team_follow_button") as ui_click, patch(
            "app.core.team_ops._pid_blocked", return_value=(False, "")
        ), patch("app.core.plg_ui.is_host_in_team", return_value=True), patch(
            "app.core.team_ops.get_host_player_for_team", return_value=0x1234
        ), patch("app.core.xajh_bridge.ensure_bridge", return_value=bridge):
            result = set_team_follow(session, enabled=True, use_ui_click=False)

        self.assertTrue(result.ok)
        self.assertEqual(result.detail["path"], "bridge_ui_callback")
        ui_click.assert_not_called()


    def test_target_already_in_party_matches_member_by_id(self) -> None:
        from app.core.team_ops import TeamMemberTarget, target_already_in_party

        party = [{"name": "队员甲", "obj_id": 101}]
        t = TeamMemberTarget(name="队员甲", obj_id=101, source="multi")
        self.assertTrue(target_already_in_party(t, party))
        other = TeamMemberTarget(name="队员乙", obj_id=102, source="multi")
        self.assertFalse(target_already_in_party(other, party))

    def test_target_already_in_party_false_when_party_empty_or_none(self) -> None:
        from app.core.team_ops import TeamMemberTarget, target_already_in_party

        t = TeamMemberTarget(name="队员甲", obj_id=101, source="multi")
        self.assertFalse(target_already_in_party(t, []))
        self.assertFalse(target_already_in_party(t, None))

    def test_target_already_in_party_matches_literal_id_token(self) -> None:
        from app.core.team_ops import TeamMemberTarget, target_already_in_party

        party = [{"name": "队员甲", "obj_id": 101}]
        t = TeamMemberTarget(name="101", obj_id=0, source="literal_id")
        self.assertTrue(target_already_in_party(t, party))

    def test_filter_targets_not_in_party_skips_only_in_party_members(self) -> None:
        from app.core.team_ops import TeamMemberTarget, filter_targets_not_in_party

        party = [{"name": "已在队", "obj_id": 501}]
        a = TeamMemberTarget(name="已在队", obj_id=501, source="multi")
        b = TeamMemberTarget(name="待邀", obj_id=502, source="protocol")
        need, skipped = filter_targets_not_in_party([a, b], party)
        self.assertEqual([t.obj_id for t in need], [502])
        self.assertEqual(skipped, ["已在队(multi)"])

    def test_invite_members_by_targets_skips_in_party_but_invites_others(self) -> None:
        from unittest.mock import ANY

        from app.core.team_ops import TeamMemberTarget, TeamOpResult, invite_members_by_targets

        in_party = TeamMemberTarget(name="已在队", obj_id=501, source="multi")
        need_invite = TeamMemberTarget(name="待邀", obj_id=502, source="protocol")
        party = [{"name": "已在队", "obj_id": 501}, {"name": "队长", "obj_id": 1}]
        sent = TeamOpResult(ok=True, action="invite", message="sent")
        with patch(
            "app.core.plg_ui.host_team_role",
            return_value={"in_team": True, "role": "leader"},
        ), patch(
            "app.core.team_ops.read_host_identity", return_value=("队长", 1)
        ), patch(
            "app.core.team_ops.invite_player_by_name", return_value=sent
        ) as invite, patch(
            "app.core.team_ops.apply_captain_team_defaults"
        ) as defaults:
            result = invite_members_by_targets(
                object(),
                [in_party, need_invite],
                skip_already_in_party=True,
                party=party,
                pause_s=0.0,
                rounds=1,
                apply_defaults=False,
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.detail["invited"], ["待邀#502"])
        self.assertEqual(result.detail["skipped_in_party"], ["已在队(multi)"])
        invite.assert_called_once()
        self.assertEqual(invite.call_args.args[1], "待邀")
        defaults.assert_not_called()

    def test_team_form_service_audit_ready_when_all_present(self) -> None:
        from app.core.team_ops import TeamFormService, TeamMemberTarget

        target = TeamMemberTarget(name="队员甲", obj_id=101, source="multi")
        party = [
            {"name": "队长", "obj_id": 1, "is_self": True},
            {"name": "队员甲", "obj_id": 101, "is_self": False},
            {"name": "额外队员", "obj_id": 202, "is_self": False},
        ]
        service = TeamFormService(log=lambda _m: None)
        with patch(
            "app.core.team_ops.list_party_members", return_value=party
        ) as lp:
            audit = service.audit_party(object(), [target], fresh=True)
        lp.assert_called_once()
        self.assertTrue(audit["ready"])
        self.assertEqual(audit["missing"], [])

    def test_team_form_service_audit_missing_member(self) -> None:
        from app.core.team_ops import TeamFormService, TeamMemberTarget

        target = TeamMemberTarget(name="未到队员", obj_id=303, source="multi")
        party = [{"name": "队长", "obj_id": 1, "is_self": True}]
        service = TeamFormService(log=lambda _m: None)
        with patch(
            "app.core.team_ops.list_party_members", return_value=party
        ):
            audit = service.audit_party(object(), [target], fresh=True)
        self.assertFalse(audit["ready"])
        self.assertEqual(audit["missing"], ["未到队员"])

    def test_team_form_service_noop_when_party_conforms(self) -> None:
        from app.core.team_ops import TeamFormService, TeamMemberTarget

        target = TeamMemberTarget(name="队员甲", obj_id=101, source="multi")
        party = [
            {"name": "队长", "obj_id": 1, "is_self": True},
            {"name": "队员甲", "obj_id": 101, "is_self": False},
        ]
        service = TeamFormService(
            log=lambda _m: None,
            on_slave_leave=lambda _m: called.append("leave"),
            on_slave_fly=lambda _m: called.append("fly"),
        )
        called: list[str] = []
        with patch(
            "app.core.team_ops.list_party_members", return_value=party
        ), patch(
            "app.core.team_ops.leave_team"
        ) as leave, patch(
            "app.core.team_ops.invite_members_by_targets"
        ) as invite:
            result = service.form(object(), [target], exclude_pid=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.detail["reformed"], False)
        leave.assert_not_called()
        invite.assert_not_called()
        self.assertEqual(called, [])  # no group-control publish on no-op

    def test_team_form_service_missing_blocks(self) -> None:
        from app.core.team_ops import TeamFormService, TeamMemberTarget

        target = TeamMemberTarget(name="未到队员", obj_id=303, source="multi")
        service = TeamFormService(log=lambda _m: None)
        # First audit finds nobody in party, invite is skipped (no targets join).
        with patch(
            "app.core.team_ops.list_party_members", return_value=[]
        ), patch(
            "app.core.team_ops.leave_team"
        ), patch(
            "app.core.team_ops.invite_members_by_targets"
        ), patch("time.sleep", return_value=None), patch(
            "time.monotonic", side_effect=[0.0, 100.0, 100.0, 200.0]
        ):
            result = service.form(object(), [target], exclude_pid=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "missing_members")


    def test_team_form_service_reinvites_missing_until_ready(self) -> None:
        from types import SimpleNamespace

        from app.core.team_ops import TeamFormService, TeamMemberTarget, TeamOpResult

        a = TeamMemberTarget(name="队员甲", obj_id=101, source="multi")
        b = TeamMemberTarget(name="队员乙", obj_id=202, source="multi")
        sent = TeamOpResult(ok=True, action="invite", message="sent")
        called: list[str] = []
        service = TeamFormService(
            log=lambda _m: None,
            on_slave_leave=lambda _m: called.append("leave"),
            on_slave_fly=lambda _m: called.append("fly"),
        )
        # 首轮观察超时后，甲乙各补邀一次并实到。
        party_sequences = [
            [],
            [],
            [{"name": "队员甲", "obj_id": 101}, {"name": "队员乙", "obj_id": 202}],
        ]
        with patch(
            "app.core.team_ops.list_party_members",
            side_effect=party_sequences,
        ), patch("app.core.team_ops.leave_team"), patch(
            "app.core.team_ops.invite_members_by_targets",
            return_value=sent,
        ) as invite, patch(
            "time.sleep", return_value=None
        ), patch(
            "time.monotonic", side_effect=[0.0, 100.0, 100.0]
        ), patch(
            "app.core.map_fly.fly_official_preset",
            return_value=SimpleNamespace(ok=True, message="fly", detail={"verified_move": True}),
        ):
            result = service.form(object(), [a, b], exclude_pid=1)

        self.assertTrue(result.ok)
        self.assertTrue(result.detail["reformed"])
        self.assertEqual(result.detail["missing"], [])
        self.assertEqual(called, ["leave", "fly"])
        # 1 次批量 + 2 次逐个补邀
        self.assertEqual(invite.call_count, 3)
        batch_call = invite.call_args_list[0]
        self.assertEqual(batch_call.args[1], [a, b])
        self.assertEqual(batch_call.kwargs["rounds"], 1)
        re_a = invite.call_args_list[1]
        self.assertEqual(re_a.args[1], [a])
        self.assertEqual(re_a.kwargs["pause_s"], 0.0)
        self.assertEqual(re_a.kwargs["rounds"], 1)
        self.assertIs(re_a.kwargs["apply_defaults"], False)
        re_b = invite.call_args_list[2]
        self.assertEqual(re_b.args[1], [b])

    def test_team_form_service_reinvites_each_missing_target_only_once(self) -> None:
        from app.core.team_ops import TeamFormService, TeamMemberTarget, TeamOpResult

        a = TeamMemberTarget(name="顽固队员", obj_id=101, source="multi")
        b = TeamMemberTarget(name="后续队员", obj_id=202, source="multi")
        sent = TeamOpResult(ok=True, action="invite", message="sent")
        service = TeamFormService(log=lambda _m: None)
        party_sequences = [
            [],  # initial audit
            [],  # initial settle expires with both missing
            [{"name": "后续队员", "obj_id": 202}],  # final audit: A still missing
        ]
        with patch(
            "app.core.team_ops.list_party_members", side_effect=party_sequences
        ), patch("app.core.team_ops.leave_team"), patch(
            "app.core.team_ops.invite_members_by_targets", return_value=sent
        ) as invite, patch("time.sleep", return_value=None), patch(
            "time.monotonic", side_effect=[0.0, 100.0, 100.0, 200.0]
        ):
            result = service.form(object(), [a, b], exclude_pid=1)

        self.assertFalse(result.ok)
        self.assertEqual(result.detail["missing"], ["顽固队员"])
        retried = [call.args[1] for call in invite.call_args_list[1:]]
        self.assertEqual(retried, [[a], [b]])

    def test_team_form_service_waits_for_late_join_before_reinvite(self) -> None:
        from types import SimpleNamespace

        from app.core.team_ops import TeamFormService, TeamMemberTarget, TeamOpResult

        target = TeamMemberTarget(name="稍后入队", obj_id=303, source="multi")
        party = [{"name": target.name, "obj_id": target.obj_id}]
        sent = TeamOpResult(ok=True, action="invite", message="sent")
        service = TeamFormService(log=lambda _m: None)
        with patch(
            "app.core.team_ops.list_party_members", side_effect=[[], [], party]
        ), patch("app.core.team_ops.leave_team"), patch(
            "app.core.team_ops.invite_members_by_targets", return_value=sent
        ) as invite, patch("time.sleep", return_value=None) as sleep, patch(
            "time.monotonic", side_effect=[0.0, 1.0]
        ), patch(
            "app.core.map_fly.fly_official_preset",
            return_value=SimpleNamespace(
                ok=True, message="fly", detail={"verified_move": True}
            ),
        ):
            result = service.form(object(), [target], exclude_pid=1)

        self.assertTrue(result.ok)
        self.assertEqual(invite.call_count, 1)
        self.assertIn(((0.75,), {}), [(c.args, c.kwargs) for c in sleep.call_args_list])

    def test_team_form_service_initial_invites_pause_after_three_targets(self) -> None:
        from types import SimpleNamespace

        from app.core.team_ops import TeamFormService, TeamMemberTarget, TeamOpResult

        targets = [
            TeamMemberTarget(name=f"队员{i}", obj_id=100 + i, source="multi")
            for i in range(5)
        ]
        party = [{"name": t.name, "obj_id": t.obj_id} for t in targets]
        sent = TeamOpResult(ok=True, action="invite", message="sent")
        service = TeamFormService(log=lambda _m: None)
        with patch(
            "app.core.team_ops.list_party_members",
            side_effect=[[], party],
        ), patch("app.core.team_ops.leave_team"), patch(
            "app.core.team_ops.invite_members_by_targets", return_value=sent
        ) as invite, patch("time.sleep", return_value=None) as sleep, patch(
            "time.monotonic", side_effect=[0.0]
        ), patch(
            "app.core.map_fly.fly_official_preset",
            return_value=SimpleNamespace(
                ok=True, message="fly", detail={"verified_move": True}
            ),
        ):
            result = service.form(object(), targets, exclude_pid=1)

        self.assertTrue(result.ok)
        self.assertEqual(invite.call_count, 2)
        self.assertEqual(invite.call_args_list[0].args[1], targets[:3])
        self.assertIs(invite.call_args_list[0].kwargs["apply_defaults"], False)
        self.assertEqual(invite.call_args_list[1].args[1], targets[3:])
        self.assertIs(invite.call_args_list[1].kwargs["apply_defaults"], True)
        self.assertIn(((3.0,), {}), [(c.args, c.kwargs) for c in sleep.call_args_list])


if __name__ == "__main__":
    unittest.main()
