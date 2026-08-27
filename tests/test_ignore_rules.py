# -*- coding: utf-8 -*-
"""Tests for app.core.ignore_rules."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import ignore_rules as ir


class IgnoreRulesStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ignore_rules.json"
        self.roles = Path(self.tmp.name) / "roles"
        patcher = mock.patch.object(ir, "_rules_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        roles_patcher = mock.patch(
            "app.core.account_manager.roles_root", return_value=self.roles
        )
        roles_patcher.start()
        self.addCleanup(roles_patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_add_load_remove_roundtrip(self) -> None:
        r = ir.add_char_rule(12345, 0x1A2B3C, name="上官霸刀")
        self.assertTrue(r.get("ok"))
        self.assertEqual(r["tid"], 0x1A2B3C)

        rules = ir.load_char_rules(12345)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["tid"], 0x1A2B3C)
        self.assertEqual(rules[0]["name"], "上官霸刀")

        r2 = ir.remove_char_rule(12345, 0x1A2B3C)
        self.assertTrue(r2.get("removed"))
        self.assertEqual(ir.load_char_rules(12345), [])

    def test_char_rules_are_isolated(self) -> None:
        ir.add_char_rule(1, 100, name="a")
        ir.add_char_rule(2, 200, name="b")
        self.assertEqual([r["tid"] for r in ir.load_char_rules(1)], [100])
        self.assertEqual([r["tid"] for r in ir.load_char_rules(2)], [200])

    def test_invalid_char_id_refused(self) -> None:
        self.assertFalse(ir.add_char_rule("角色甲", 1, name="x").get("ok"))
        self.assertFalse(ir.add_char_rule(0, 1).get("ok"))

    def test_tid_zero_refused(self) -> None:
        self.assertFalse(ir.add_char_rule(12345, 0).get("ok"))

    def test_rules_land_in_role_dir(self) -> None:
        ir.add_char_rule(777, 0x10, name="x")
        p = self.roles / "777" / "ignore.json"
        self.assertTrue(p.is_file())
        self.assertEqual(ir.load_char_rules(777)[0]["tid"], 0x10)


if __name__ == "__main__":
    unittest.main()
