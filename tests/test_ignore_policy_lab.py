# -*- coding: utf-8 -*-
"""Tests for app.core.ignore_policy_lab (read-side blacklist helpers)."""
from __future__ import annotations

import struct
import unittest
from unittest import mock

from app.core import ignore_policy_lab as ipo


class _FakePM:
    """In-memory process-memory stand-in (address space = dict)."""

    def __init__(self) -> None:
        self.mem: dict[int, bytes] = {}

    def set(self, addr: int, data: bytes) -> None:
        for i, b in enumerate(bytes(data)):
            self.mem[int(addr) + i] = int(b)

    def read_bytes(self, addr: int, n: int) -> bytes:
        out = b""
        for i in range(int(n)):
            b = self.mem.get(int(addr) + i)
            if b is None:
                return out
            out += bytes([int(b)])
        return out

    def write_bytes(self, addr: int, data: bytes) -> None:
        for i, b in enumerate(bytes(data)):
            self.mem[int(addr) + i] = int(b)


def _mk_session(pm, autoplay=0x20000000, root=0x10000000):
    sess = mock.Mock()
    sess.pid = 1234
    sess.module_base = 0x400000
    sess.pm = pm
    g_va = 0x15282D8
    pm.set(g_va, struct.pack("<I", root))
    pm.set(root + 0x24, struct.pack("<I", root + 0x40))
    pm.set(root + 0x40 + 0x220, struct.pack("<I", autoplay))
    return sess


def _mk_policy(pm, policy):
    """Install a policy object (vtable 0x12BEC9C)."""
    pm.set(policy, struct.pack("<I", 0x12BEC9C))
    return policy


class IgnorePolicyResolveTest(unittest.TestCase):
    def test_resolve_autoplay_chain(self) -> None:
        pm = _FakePM()
        sess = _mk_session(pm)
        pid, ap = ipo.resolve_cec_autoplay(sess)
        self.assertEqual(pid, 1234)
        self.assertEqual(ap, 0x20000000)

    def test_resolve_policy_finds_attack_object(self) -> None:
        pm = _FakePM()
        sess = _mk_session(pm)
        policy = 0x21000000
        _mk_policy(pm, policy)
        pm.set(0x20000000 + 0x30, struct.pack("<I", policy))
        r = ipo.resolve_ignore_policy(sess)
        self.assertTrue(r.get("found"))
        self.assertEqual(r["policy"], policy)
        self.assertEqual(r["field_off"], 0x30)

    def test_resolve_policy_missing(self) -> None:
        pm = _FakePM()
        sess = _mk_session(pm)
        r = ipo.resolve_ignore_policy(sess)
        self.assertFalse(r.get("found"))
        self.assertIn("not instantiated", r.get("reason", ""))

    def test_read_current_target(self) -> None:
        pm = _FakePM()
        sess = _mk_session(pm)
        policy = 0x21000000
        _mk_policy(pm, policy)
        pm.set(0x20000000 + 0x30, struct.pack("<I", policy))
        oid = 0x02000000_0000005A
        pm.set(policy + 0x38, struct.pack("<I", oid & 0xFFFFFFFF))
        pm.set(policy + 0x3C, struct.pack("<I", (oid >> 32) & 0xFFFFFFFF))
        r = ipo.read_current_target(sess)
        self.assertTrue(r.get("found"))
        self.assertEqual(r["target_id64"], oid)

    def test_read_selected_target_pure_memory(self) -> None:
        pm = _FakePM()
        sess = _mk_session(pm)
        # host_side = [[0x15282D8]+0x24]+0x8C ; selected = u64[host_side+0x19E8]
        root = 0x10000000
        mid = root + 0x40
        host = 0x70000000
        g_va = 0x15282D8
        pm.set(g_va, struct.pack("<I", root))
        pm.set(root + 0x24, struct.pack("<I", mid))
        pm.set(mid + 0x8C, struct.pack("<I", host))
        oid = 0x01000000_0000ABCD
        pm.set(host + 0x19E8, struct.pack("<I", oid & 0xFFFFFFFF))
        pm.set(host + 0x19EC, struct.pack("<I", (oid >> 32) & 0xFFFFFFFF))
        r = ipo.read_selected_target(sess)
        self.assertTrue(r.get("ok"))
        self.assertEqual(r["target_id64"], oid)

    def test_read_selected_target_empty(self) -> None:
        pm = _FakePM()
        sess = _mk_session(pm)
        r = ipo.read_selected_target(sess)
        self.assertEqual(r["target_id64"], 0)


if __name__ == "__main__":
    unittest.main()
