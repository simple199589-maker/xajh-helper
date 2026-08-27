# -*- coding: utf-8 -*-
"""Unit tests for the account / role config manager (no live game)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core import account_manager as am


def _temp_root() -> tuple[str, Path]:
    td = tempfile.mkdtemp()
    return td, Path(td)


class AccountEncryptionTests(unittest.TestCase):
    def test_encrypt_decrypt_roundtrip(self) -> None:
        for pw in ("", "a", "secret123", "密码@测试", "a" * 200):
            blob = am.encrypt_password(pw)
            if not pw:
                self.assertEqual(blob, "")
                continue
            self.assertEqual(am.decrypt_password(blob), pw)

    def test_legacy_plain_passthrough(self) -> None:
        self.assertEqual(am.decrypt_password("plain-text"), "plain-text")


class AccountCrudTests(unittest.TestCase):
    def _patch(self, td: str):
        return patch("app.core.account_manager.config_root", return_value=Path(td))

    def test_save_get_delete_account(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            rec = am.save_account("acc1", password="p@ss", slots=[])
            self.assertEqual(rec["account_id"], "acc1")
            self.assertEqual(rec["slots"], am._empty_slots())

            acc = am.get_account("acc1")
            self.assertEqual(acc["account_id"], "acc1")
            self.assertEqual(acc["password"], "p@ss")

            listed = am.list_accounts()
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["account_id"], "acc1")
            # multiple mask dots instead of a single one
            self.assertGreater(len(listed[0]["password_masked"]), 1)

            self.assertTrue(am.delete_account("acc1"))
            self.assertIsNone(am.get_account("acc1"))
            self.assertFalse(am.delete_account("acc1"))

    def test_edit_keeps_password_when_blank(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account("acc1", password="first")
            am.save_account("acc1", slots=am._empty_slots())
            self.assertEqual(am.get_account("acc1")["password"], "first")
            am.save_account("acc1", password="second")
            self.assertEqual(am.get_account("acc1")["password"], "second")

    def test_slots_normalized_to_three(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "acc1",
                password="pw",
                slots=[
                    {"role_id": "1001", "name": "甲"},
                    {"role_id": "1002", "name": "乙"},
                ],
            )
            slots = am.account_slots("acc1")
            self.assertEqual(len(slots), 3)
            self.assertEqual(slots[0]["role_id"], "1001")
            self.assertEqual(slots[2]["role_id"], "")

    def test_edit_keep_other_slot_bindings(self) -> None:
        """编辑改选角色后，其它已绑定槽位（role_id/name）必须保留，避免 PID 落空。"""
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "accE",
                password="pw",
                slots=[
                    {"role_index": 1, "role_id": "9001", "name": "甲"},
                    {"role_index": 0, "role_id": "", "name": ""},
                    {"role_index": 0, "role_id": "", "name": ""},
                ],
            )
            # 模拟 GUI _save：改选角色2(ri=2)，其余槽保留原绑定。
            slots = [
                {"role_index": 2, "role_id": "", "name": ""},
                {"role_index": 1, "role_id": "9001", "name": "甲"},
                {"role_index": 0, "role_id": "", "name": ""},
            ]
            am.save_account("accE", slots=slots)
            acc = am.get_account("accE")
            self.assertEqual(acc["slots"][0]["role_index"], 2)
            # 原角色1 绑定必须保留，PID/在线匹配才能继续命中。
            self.assertEqual(acc["slots"][1]["role_id"], "9001")
            self.assertEqual(acc["slots"][1]["name"], "甲")

    def test_delete_config_does_not_touch_process(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account("acc1", password="pw")
            with patch.object(am, "kill_all_games") as kill:
                self.assertTrue(am.delete_account("acc1"))
            kill.assert_not_called()

    def test_account_level_inject(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account("accI", password="pw", inject=False)
            self.assertFalse(am.get_account("accI")["inject"])
            # not provided on edit -> kept
            am.save_account("accI", password="pw")
            self.assertFalse(am.get_account("accI")["inject"])
            am.save_account("accI", inject=True)
            self.assertTrue(am.get_account("accI")["inject"])

    def test_account_level_control(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account("accC", password="pw", control="slave")
            self.assertEqual(am.get_account("accC")["control"], "slave")
            # not provided on edit -> kept
            am.save_account("accC", password="pw")
            self.assertEqual(am.get_account("accC")["control"], "slave")
            am.save_account("accC", control="none")
            self.assertEqual(am.get_account("accC")["control"], "none")
            # invalid -> fallback none
            am.save_account("accC", control="bogus")
            self.assertEqual(am.get_account("accC")["control"], "none")

    def test_account_level_hang_enabled_and_team_control(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            # 默认：是否开启挂机 与 是否队内控 均不勾选。
            am.save_account("accH", password="pw")
            acc = am.get_account("accH")
            self.assertFalse(acc["hang_enabled"])
            self.assertFalse(acc["team_control"])
            am.save_account(
                "accH", password="pw", hang_enabled=True, team_control=True
            )
            acc = am.get_account("accH")
            self.assertTrue(acc["hang_enabled"])
            self.assertTrue(acc["team_control"])
            # not provided on edit -> kept
            am.save_account("accH", password="pw")
            acc = am.get_account("accH")
            self.assertTrue(acc["hang_enabled"])
            self.assertTrue(acc["team_control"])

    def test_apply_account_attrs_team_and_dungeon_existing_role(self) -> None:
        """已有角色目录时：队内控标志写 control.json，副本挂机写挂机模式；
        角色已有主副控值保留不覆盖。"""
        td, root = _temp_root()
        with self._patch(td):
            from app.core.hang_settings import save_hang_prefs

            am.save_role_inject(6003, True)
            am.save_role_control(6003, "master", team=False)
            save_hang_prefs(char_id=6003, mode=0)
            am.save_account(
                "accT",
                password="pw",
                inject=True,
                control="slave",
                dungeon_hang=True,
                team_control=True,
                slots=[
                    {"role_index": 1, "role_id": "6003", "name": "丙"},
                    {"role_index": 0, "role_id": "", "name": ""},
                    {"role_index": 0, "role_id": "", "name": ""},
                ],
            )
            out = am.apply_account_attrs_to_roles("accT")
            self.assertEqual(out["applied"], ["6003"])
            ctl = am.load_role_control(6003)
            # 角色已有主副控(master)保留；账号副控(slave)不覆盖。
            self.assertEqual(ctl["role"], "master")
            self.assertTrue(ctl["team"])
            self.assertEqual(am.load_role_hang(6003).get("mode"), 1)

    def test_role_team_config_roundtrip(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            self.assertEqual(am.load_role_team(7001), {"members": ""})
            am.save_role_team(7001, "甲、乙、丙")
            self.assertEqual(am.load_role_team(7001), {"members": "甲、乙、丙"})
            self.assertTrue((am.role_dir(7001) / "team.json").is_file())
            # empty save clears
            am.save_role_team(7001, "  ")
            self.assertEqual(am.load_role_team(7001), {"members": ""})

    def test_apply_account_attrs_writes_role_configs(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "accA",
                password="pw",
                inject=True,
                control="slave",
                dungeon_hang=True,
                slots=[
                    {"role_index": 1, "role_id": "6001", "name": "甲"},
                    {"role_index": 0, "role_id": "", "name": ""},
                    {"role_index": 0, "role_id": "", "name": ""},
                ],
            )
            out = am.apply_account_attrs_to_roles("accA")
            self.assertEqual(out["applied"], ["6001"])
            self.assertEqual(am.load_role_inject(6001)["enabled"], True)
            self.assertEqual(am.load_role_control(6001)["role"], "slave")
            hang = am.load_role_hang(6001)
            self.assertEqual(hang.get("mode"), 1)

    def test_apply_account_attrs_skips_when_inject_off(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "accB",
                password="pw",
                inject=False,
                slots=[
                    {"role_index": 1, "role_id": "6002", "name": "乙"},
                    {"role_index": 0, "role_id": "", "name": ""},
                    {"role_index": 0, "role_id": "", "name": ""},
                ],
            )
            out = am.apply_account_attrs_to_roles("accB")
            self.assertEqual(out["applied"], [])
            self.assertEqual(am.load_role_inject(6002)["enabled"], True)

    def test_account_level_dungeon_hang(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            self.assertFalse(am.get_account("accD") is not None and False)
            am.save_account("accD", password="pw", dungeon_hang=True)
            self.assertTrue(am.get_account("accD")["dungeon_hang"])
            # not provided on edit -> kept
            am.save_account("accD", password="pw")
            self.assertTrue(am.get_account("accD")["dungeon_hang"])

    def test_legacy_slot_inject_all_off_migrates_to_account(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            path = am.account_path("legacy1")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "account_id": "legacy1",
                        "password": "pw",
                        "slots": [
                            {"role_index": 1, "role_id": "1", "name": "", "inject": False},
                            {"role_index": 0, "role_id": "", "name": ""},
                            {"role_index": 0, "role_id": "", "name": ""},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertFalse(am.get_account("legacy1")["inject"])


class RoleConfigTests(unittest.TestCase):
    def _patch(self, td: str):
        return patch("app.core.account_manager.config_root", return_value=Path(td))

    def test_role_meta_hang_control_inject(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            self.assertNotIn("1001", [r["role_id"] for r in am.list_roles()])
            am.save_role_meta(1001, name="角色甲")
            am.save_role_hang(1001, {"mode": 0, "radius": 6})
            am.save_role_control(1001, "master")
            am.save_role_inject(1001, False)

            role = am.get_role(1001)
            self.assertEqual(role["role_id"], "1001")
            self.assertEqual(role["name"], "角色甲")
            self.assertEqual(role["hang"].get("radius"), 6)
            self.assertEqual(role["control"], "master")
            self.assertFalse(role["inject"])

            # default control/inject for a fresh role
            am.save_role_meta(1002, name="乙")
            role2 = am.get_role(1002)
            self.assertEqual(role2["control"], "none")
            self.assertTrue(role2["inject"])

            ids = [r["role_id"] for r in am.list_roles()]
            self.assertEqual(ids, ["1001", "1002"])

            self.assertTrue(am.delete_role(1001))
            self.assertFalse(am.delete_role(1001))
            self.assertNotIn("1001", [r["role_id"] for r in am.list_roles()])

    def test_hang_files_land_in_role_dir(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_role_hang(2001, {"mode": 1})
            hang_path = am.role_dir(2001) / "hang.json"
            self.assertTrue(hang_path.is_file())
            data = json.loads(hang_path.read_text(encoding="utf-8"))
            self.assertEqual(data["mode"], 1)

    def test_generic_role_config_roundtrip(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            self.assertIsNone(am.load_role_config(5001, "custom"))
            am.save_role_config(5001, "custom", {"foo": "bar"})
            self.assertEqual(am.load_role_config(5001, "custom"), {"foo": "bar"})
            self.assertTrue((am.role_dir(5001) / "custom.json").is_file())
            with self.assertRaises(ValueError):
                am.role_config_path("", "custom")

    def test_qiegao_prefs_split_to_role_dir(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            from app.core.activity_auto import load_qiegao_prefs, save_qiegao_prefs

            save_qiegao_prefs(
                is_alt=True, analyze_mob_freq=True, role_id=6001
            )
            prefs = load_qiegao_prefs(6001)
            self.assertTrue(prefs["is_alt"])
            self.assertTrue(prefs["analyze_mob_freq"])
            self.assertTrue((am.role_dir(6001) / "qiegao.json").is_file())
            # no role_id -> legacy global file untouched / default
            other = load_qiegao_prefs()
            self.assertFalse(other["is_alt"])

    def test_merge_role_from_inject_syncs_account_slot(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "acc1",
                password="pw",
                slots=[
                    {"role_id": "3001", "name": "旧名"},
                    {"role_id": "", "name": ""},
                    {"role_id": "", "name": ""},
                ],
            )
            merged = am.merge_role_from_inject(3001, name="新名")
            self.assertEqual(merged["role_id"], "3001")
            self.assertEqual(merged["name"], "新名")
            self.assertEqual(merged["account_id"], "acc1")

            acc = am.get_account("acc1")
            self.assertEqual(acc["slots"][0]["name"], "新名")

    def test_find_account_by_role_id(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "acc2",
                password="pw",
                slots=[{"role_id": "4001", "name": "甲"}, {"role_id": "", "name": ""}, {"role_id": "", "name": ""}],
            )
            self.assertEqual(am.find_account_by_role_id(4001), "acc2")
            self.assertEqual(am.find_account_by_role_id(9999), "")

    def test_find_account_by_role_name(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "accN",
                password="pw",
                slots=[{"role_id": "4101", "name": "名字甲"}, {"role_id": "", "name": ""}, {"role_id": "", "name": ""}],
            )
            self.assertEqual(am.find_account_by_role_name("名字甲"), "accN")
            self.assertEqual(am.find_account_by_role_name("不存在"), "")

    def test_apply_char_select_roles_writes_slots(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account("accC1", password="pw")
            am.apply_char_select_roles(
                "accC1",
                [
                    {"slot": 1, "name": "张三", "level": "唐门 140级", "role_id": 1001},
                    {"slot": 2, "name": "李四", "level": "唐门 140级", "role_id": 1002},
                    {"slot": 3, "name": "", "level": "", "role_id": 0},
                ],
            )
            slots = am.account_slots("accC1")
            self.assertEqual(slots[0]["role_id"], "1001")
            self.assertEqual(slots[0]["name"], "张三")
            self.assertEqual(slots[0]["role_index"], 1)
            self.assertEqual(slots[1]["role_id"], "1002")
            self.assertEqual(slots[1]["name"], "李四")
            self.assertEqual(slots[1]["role_index"], 2)
            self.assertEqual(slots[2]["role_id"], "")
            self.assertEqual(slots[2]["role_index"], 3)
            meta = am.load_role_meta(1001)
            self.assertEqual(meta.get("name"), "张三")
            self.assertEqual(meta.get("account_id"), "accC1")

    def test_char_select_backfill_respects_disabled_slots(self) -> None:
        """账号已绑定角色但手动改为未启用：选角回写不覆盖 role_index（保留 0）。"""
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "accD",
                password="pw",
                slots=[
                    {"role_index": 0, "role_id": "7101", "name": "旧名"},
                    {"role_index": 0, "role_id": "", "name": ""},
                    {"role_index": 0, "role_id": "", "name": ""},
                ],
            )
            am.apply_char_select_roles(
                "accD",
                [
                    {"slot": 1, "name": "新甲", "level": "唐门 140级", "role_id": 7101},
                    {"slot": 2, "name": "新乙", "level": "唐门 140级", "role_id": 7102},
                    {"slot": 3, "name": "", "level": "", "role_id": 0},
                ],
            )
            slots = am.account_slots("accD")
            # 未启用保持：role_index 仍为 0，仅同步名称/ID。
            self.assertEqual(slots[0]["role_index"], 0)
            self.assertEqual(slots[0]["role_id"], "7101")
            self.assertEqual(slots[0]["name"], "新甲")
            self.assertEqual(slots[1]["role_index"], 0)
            self.assertEqual(slots[1]["role_id"], "7102")
            self.assertEqual(slots[1]["name"], "新乙")

    def test_active_role_id_by_title_ownership(self) -> None:
        """未注入但已进世界：标题角色名归属账号，返回对应 role_id。"""
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "accA",
                password="pw",
                slots=[
                    {"role_id": "2001", "name": "张三"},
                    {"role_id": "2002", "name": "李四"},
                    {"role_id": "2003", "name": "王五"},
                ],
            )
            # 标题命中槽位2 名字 -> 返回 2002。
            with patch(
                "app.core.inject_gate.find_xajh_processes",
                return_value=[{"pid": 999}],
            ), patch(
                "app.core.inject_gate.find_main_hwnd_for_pid",
                return_value=(99, "笑傲江湖OL - 李四 服务器一", "XAJH"),
            ):
                self.assertEqual(am.active_role_id_for_account("accA"), "2002")
            # 无匹配标题 -> 空。
            with patch(
                "app.core.inject_gate.find_xajh_processes",
                return_value=[{"pid": 999}],
            ), patch(
                "app.core.inject_gate.find_main_hwnd_for_pid",
                return_value=(99, "笑傲江湖OL", "XAJH"),
            ):
                self.assertEqual(am.active_role_id_for_account("accA"), "")

    def test_running_pids_for_account_matches_by_role_name(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "accP",
                password="pw",
                slots=[{"role_id": "5001", "name": "角色甲"}, {"role_id": "", "name": ""}, {"role_id": "", "name": ""}],
            )
            with patch(
                "app.core.inject_gate.find_xajh_processes",
                return_value=[{"pid": 111}, {"pid": 222}],
            ), patch(
                "app.core.inject_gate.find_main_hwnd_for_pid",
                side_effect=[(11, "笑傲江湖OL - 角色甲 服务器一", "XAJH"), (22, "笑傲江湖OL", "XAJH")],
            ):
                pids = am.running_pids_for_account("accP")
            self.assertEqual(pids, [111])
            # no bound roles -> empty
            am.save_account("accX", password="pw")
            self.assertEqual(am.running_pids_for_account("accX"), [])

    def test_running_pids_by_account_ownership_reverse(self) -> None:
        """账号槽位未写 name，但角色 meta 归属该账号：标题角色名反查即命中。"""
        td, root = _temp_root()
        with self._patch(td):
            am.save_account("accO", password="pw")
            am.save_role_meta(5301, name="归属号", account_id="accO")
            with patch(
                "app.core.inject_gate.find_xajh_processes",
                return_value=[{"pid": 555}],
            ), patch(
                "app.core.inject_gate.find_main_hwnd_for_pid",
                return_value=(55, "笑傲江湖OL - 归属号 服务器三", "XAJH"),
            ):
                pids = am.running_pids_for_account("accO")
            self.assertEqual(pids, [555])

    def test_running_pids_dynamic_closed_window_drops_out(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "accR",
                password="pw",
                slots=[{"role_id": "5101", "name": "运行甲"}, {"role_id": "", "name": ""}, {"role_id": "", "name": ""}],
            )
            # No runtime pid file: purely dynamic. Closed/untitled window -> no pid.
            with patch(
                "app.core.inject_gate.find_xajh_processes",
                return_value=[{"pid": 111}],
            ), patch(
                "app.core.inject_gate.find_main_hwnd_for_pid",
                return_value=(11, "笑傲江湖OL", "XAJH"),
            ):
                self.assertEqual(am.running_pids_for_account("accR"), [])
            # In-world title -> pid matched dynamically.
            with patch(
                "app.core.inject_gate.find_xajh_processes",
                return_value=[{"pid": 111}, {"pid": 222}],
            ), patch(
                "app.core.inject_gate.find_main_hwnd_for_pid",
                side_effect=[(11, "笑傲江湖OL - 运行甲 服务器一", "XAJH"), (22, "笑傲江湖OL", "XAJH")],
            ):
                self.assertEqual(am.running_pids_for_account("accR"), [111])

    def test_running_pids_uses_recorded_login_pid_on_char_select(self) -> None:
        """选角页标题无角色名，靠登录时记录的 pid 定位（供轮询测试选角）。"""
        td, root = _temp_root()
        with self._patch(td):
            am.save_account("accS", password="pw")
            am.save_account_runtime_pid("accS", 777)
            # 标题无角色名（选角页），进程存在 -> 记录 pid 命中。
            with patch(
                "app.core.inject_gate.find_xajh_processes",
                return_value=[{"pid": 777}],
            ), patch(
                "app.core.inject_gate.find_main_hwnd_for_pid",
                return_value=(77, "笑傲江湖OL", "XAJH"),
            ):
                self.assertEqual(am.running_pids_for_account("accS"), [777])
            # 进程已退出 -> 记录被清除，返回空。
            with patch(
                "app.core.inject_gate.find_xajh_processes",
                return_value=[{"pid": 888}],
            ), patch(
                "app.core.inject_gate.find_main_hwnd_for_pid",
                return_value=(0, "", ""),
            ):
                self.assertEqual(am.running_pids_for_account("accS"), [])
            self.assertEqual(am.load_account_runtime_pid("accS"), 0)

    def test_running_pids_matches_via_role_meta_name(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            # 账号槽位只有 role_id，无 name：靠 roles/{role_id}/meta.json 的名称命中。
            am.save_account(
                "accM",
                password="pw",
                slots=[{"role_id": "5201", "name": ""}, {"role_id": "", "name": ""}, {"role_id": "", "name": ""}],
            )
            am.save_role_meta(5201, name="元宝号")
            with patch(
                "app.core.inject_gate.find_xajh_processes",
                return_value=[{"pid": 321}],
            ), patch(
                "app.core.inject_gate.find_main_hwnd_for_pid",
                return_value=(31, "笑傲江湖OL - 元宝号 服务器九", "XAJH"),
            ):
                self.assertEqual(am.running_pids_for_account("accM"), [321])

    def test_merge_does_not_fallback_to_arbitrary_account_when_pending_pid_mismatches(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            for account_id in ("accA", "accB"):
                am.save_account(account_id, password="pw", slots=[
                    {"role_index": 1, "role_id": "", "name": ""},
                    {"role_index": 0, "role_id": "", "name": ""},
                    {"role_index": 0, "role_id": "", "name": ""},
                ])
            am.set_pending_launch_account("accA", pid=101)
            am.merge_role_from_inject(7009, name="不会串号", pid=202)
            self.assertEqual(am.account_slots("accA")[0]["role_id"], "")
            self.assertEqual(am.account_slots("accB")[0]["role_id"], "")
            self.assertEqual(am.get_pending_launch_account(pid=101), "")
    def test_merge_fills_pending_account_enabled_slot(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "accA",
                password="pw",
                inject=False,
                slots=[
                    {"role_index": 1, "role_id": "", "name": ""},
                    {"role_index": 0, "role_id": "", "name": ""},
                    {"role_index": 0, "role_id": "", "name": ""},
                ],
            )
            am.set_pending_launch_account("accA")
            role = am.merge_role_from_inject(7001, name="新角色")
            self.assertEqual(role["account_id"], "accA")
            slots = am.account_slots("accA")
            self.assertEqual(slots[0]["role_id"], "7001")
            self.assertEqual(slots[0]["name"], "新角色")
            # 注入脚本是账号属性：账号级 inject=False 应用到角色配置
            self.assertFalse(am.get_role(7001)["inject"])
            self.assertEqual(am.get_pending_launch_account(), "")

    def test_merge_fills_active_role_index_slot_only(self) -> None:
        """未启用(0) 槽不会被写入，只有角色1/2/3 空槽可写。"""
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "accB",
                password="pw",
                slots=[
                    {"role_index": 2, "role_id": "", "name": ""},
                    {"role_index": am.ROLE_INDEX_NONE, "role_id": "", "name": ""},
                    {"role_index": am.ROLE_INDEX_NONE, "role_id": "", "name": ""},
                ],
            )
            am.set_pending_launch_account("accB", pid=0)
            role = am.merge_role_from_inject(7002, name="选角号", pid=0)
            self.assertEqual(role["account_id"], "accB")
            slots = am.account_slots("accB")
            self.assertEqual(slots[0]["role_id"], "7002")
            self.assertEqual(slots[1]["role_id"], "")
            self.assertEqual(slots[2]["role_id"], "")
            self.assertEqual(am.find_account_by_role_id(7002), "accB")

    def test_role_index_stored_roundtrip(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "accR",
                password="pw",
                slots=[
                    {"role_index": 0, "role_id": "", "name": ""},
                    {"role_index": 3, "role_id": "", "name": ""},
                    {"role_index": 0, "role_id": "", "name": ""},
                ],
            )
            slots = am.account_slots("accR")
            self.assertEqual([s["role_index"] for s in slots], [0, 3, 0])

    def test_merge_consumes_pending_marker_even_when_role_already_bound(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            am.save_account(
                "accC",
                password="pw",
                slots=[
                    {"enabled": True, "role_id": "7003", "name": "旧"},
                    {"enabled": False, "role_id": "", "name": ""},
                    {"enabled": False, "role_id": "", "name": ""},
                ],
            )
            am.set_pending_launch_account("accC")
            am.merge_role_from_inject(7003, name="同名")
            self.assertEqual(am.get_pending_launch_account(), "")
            slots = am.account_slots("accC")
            self.assertEqual(slots[0]["name"], "同名")


class GamePathsAndKillTests(unittest.TestCase):
    def _patch(self, td: str):
        return patch("app.core.account_manager.config_root", return_value=Path(td))

    def test_game_paths_persist(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            self.assertEqual(am.remembered_launcher(), "")
            am.save_game_path("launcher", r"D:\game\launcher.exe")
            am.save_game_path("client", r"D:\game\bin\xajh.exe")
            self.assertEqual(
                am.remembered_launcher(), r"D:\game\launcher.exe"
            )
            paths = am.load_game_paths()
            self.assertEqual(paths["client"], r"D:\game\bin\xajh.exe")

    def test_launch_launcher_missing_path(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            ok, msg = am.launch_launcher()
            self.assertFalse(ok)
            self.assertIn("未设置", msg)
            ok, msg = am.launch_launcher(str(Path(td) / "nope.exe"))
            self.assertFalse(ok)

    def test_launch_launcher_calls_popen(self) -> None:
        td, root = _temp_root()
        fake = Path(td) / "launcher.exe"
        fake.write_bytes(b"MZ")
        with self._patch(td):
            with patch.object(am.subprocess, "Popen") as popen:
                popen.return_value = MagicMock()
                ok, msg = am.launch_launcher(str(fake))
                self.assertTrue(ok)
                popen.assert_called_once()

    def test_kill_all_games_targets_only_xajh(self) -> None:
        td, root = _temp_root()
        with self._patch(td):
            completed = MagicMock()
            completed.returncode = 0
            completed.stdout = "SUCCESS"
            completed.stderr = ""
            with patch.object(am.subprocess, "run", return_value=completed) as run:
                ok, _ = am.kill_all_games()
                self.assertTrue(ok)
                args = run.call_args.args[0] if run.call_args.args else run.call_args.kwargs.get("args", [])
                self.assertIn("xajh.exe", args)
                self.assertNotIn("explorer.exe", args)


class LegacyMigrationTests(unittest.TestCase):
    def test_migrate_legacy_hang_prefs(self) -> None:
        td, root = _temp_root()
        legacy = Path(td) / "hang_prefs.json"
        legacy.write_text(
            json.dumps(
                {
                    "version": 6,
                    "default": {"radius": 7},
                    "by_id": {
                        "100001": {"mode": 0, "radius": 9, "name": "角色甲"},
                        "500001": {"wanzi_hang": True, "name": "丸子号"},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with patch("app.core.account_manager.config_root", return_value=Path(td)), patch(
            "app.core.hang_settings._hang_prefs_path", return_value=legacy
        ):
            from app.core.hang_settings import migrate_legacy_hang_prefs

            # default bucket + two role entries
            n = migrate_legacy_hang_prefs()
            self.assertEqual(n, 3)

            hang = am.load_role_hang(100001)
            self.assertEqual(hang.get("radius"), 9)
            meta = am.load_role_meta(100001)
            self.assertEqual(meta.get("name"), "角色甲")

            role = am.get_role(100001)
            self.assertEqual(role["name"], "角色甲")
            self.assertEqual(role["hang"].get("mode"), 0)

            # idempotent
            self.assertEqual(migrate_legacy_hang_prefs(), 0)


if __name__ == "__main__":
    unittest.main()
