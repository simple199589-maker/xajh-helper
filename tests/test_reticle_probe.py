# -*- coding: utf-8 -*-
"""Unit tests for reticle_probe helpers. @author by ak"""
from __future__ import annotations

import struct
import unittest
from unittest.mock import MagicMock, patch

from app.core.reticle_probe import (
    ReticleProbeSample,
    diff_reticle_samples,
    format_reticle_sample,
    parse_reticle_poll_record,
    shift_layer_active,
    wait_reticle_on_shift,
)


class ReticleProbeTests(unittest.TestCase):
    def test_diff_lists_changed_fields(self) -> None:
        a = ReticleProbeSample(ok=True, label="A", gate=1, host_session=0, cast_skill_id=0)
        b = ReticleProbeSample(
            ok=True, label="B", gate=1, host_session=1, cast_skill_id=0x123
        )
        lines = diff_reticle_samples(a, b)
        joined = "\n".join(lines)
        self.assertIn("host_session", joined)
        self.assertIn("cast_skill_id", joined)
        self.assertNotIn("gate:", joined)

    def test_summary_and_format(self) -> None:
        s = ReticleProbeSample(
            ok=True,
            label="A",
            gate=1,
            gaks10=0x8000,
            tab10=1,
            mask=1,
            host_session=0,
            cast_skill_id=5,
            controller=0x1000,
            ctrl_243d=1,
        )
        line = s.summary_line()
        self.assertIn("gate=1", line)
        self.assertIn("gaks=8000", line)
        text = format_reticle_sample(s)
        self.assertIn("host_session", text)
        self.assertIn("ctrl flags", text)

    def test_sample_needs_attach(self) -> None:
        from app.core.reticle_probe import sample_reticle_state

        sess = MagicMock()
        sess.pid = None
        sess.module_base = None
        s = sample_reticle_state(sess, label="x")
        self.assertFalse(s.ok)
        self.assertIn("need attach", s.error or "")

    def test_shift_layer_active_from_gaks_or_table(self) -> None:
        idle = ReticleProbeSample(ok=True, gaks10=0, tab10=0, mask=0)
        self.assertFalse(shift_layer_active(idle))
        self.assertTrue(
            shift_layer_active(ReticleProbeSample(ok=True, gaks10=0x8000))
        )
        self.assertTrue(shift_layer_active(ReticleProbeSample(ok=True, tab10=1)))
        self.assertTrue(shift_layer_active(ReticleProbeSample(ok=True, mask=1)))

    def test_resolve_controller_uses_input_plus_0x140(self) -> None:
        from app.core.reticle_probe import (
            CTRL_FLAG_243D,
            CTRL_INPUT_PTR_OFF,
            INPUT_CONTROLLER_OFF,
            resolve_controller_from_input,
        )

        input_this = 0x20000000
        ctrl = 0x30000000
        reads = {
            (input_this + INPUT_CONTROLLER_OFF): struct.pack("<I", ctrl),
            (ctrl + CTRL_INPUT_PTR_OFF): struct.pack("<I", input_this),
            (ctrl + CTRL_FLAG_243D): b"\x01",
        }

        def fake_read(_pid: int, addr: int, size: int) -> bytes:
            raw = reads.get(int(addr) & 0xFFFFFFFF)
            if raw is None:
                return b"\x00" * size
            return raw[:size]

        sess = MagicMock(pid=1, module_base=0x400000)
        with patch("app.core.reticle_probe.remote_read_bytes", side_effect=fake_read):
            got = resolve_controller_from_input(sess, mid=0, input_this=input_this)
        self.assertEqual(got, ctrl)

    def test_parse_poll_record_classifies_authoritative_outcomes(self) -> None:
        cases = (
            ("POLL hit=0 b1=-1 in=0 ctrl=0 gate=0 f=-1/-1/-1/-1 d6=0", "NO_POLL"),
            ("POLL hit=3 b1=0 in=30BEB9B0 ctrl=30D2D2D0 gate=1 f=1/1/0/0 d6=0", "BIND_REJECTED"),
            ("POLL hit=3 b1=1 in=30BEB9B0 ctrl=30D2D2D0 gate=1 f=1/1/0/0 d6=0", "DOWNSTREAM_REJECTED"),
            ("POLL hit=3 b1=1 in=30BEB9B0 ctrl=30D2D2D0 gate=1 f=1/1/0/0 d6=1", "DISPATCH_REACHED"),
        )
        for raw, expected in cases:
            with self.subTest(expected=expected):
                result = parse_reticle_poll_record(raw)
                self.assertEqual(result.classification(), expected)
                self.assertIn(expected, result.summary_line())

    def test_parse_poll_record_rejects_malformed_or_overlong(self) -> None:
        with self.assertRaises(ValueError):
            parse_reticle_poll_record("POLL b1=1")
        with self.assertRaises(ValueError):
            parse_reticle_poll_record("POLL " + ("x" * 200))

    def test_capture_inputpoll_diag_requires_attach(self) -> None:
        from app.core.reticle_probe import capture_inputpoll_diag

        ok, note, result = capture_inputpoll_diag(MagicMock(pid=None, module_base=None))
        self.assertFalse(ok)
        self.assertIn("need attach", note)
        self.assertIsNone(result)

    def test_wait_reticle_hits_when_shift_appears(self) -> None:
        idle = ReticleProbeSample(ok=True, label="B_AUTO", gaks10=0, tab10=0)
        hit = ReticleProbeSample(
            ok=True, label="B_AUTO", gaks10=0x8000, tab10=1, mask=1, gate=1
        )
        with patch(
            "app.core.reticle_probe.sample_reticle_state",
            side_effect=[idle, idle, hit],
        ), patch("app.core.reticle_probe.time.sleep", return_value=None):
            s = wait_reticle_on_shift(
                MagicMock(pid=1, module_base=0x400000),
                timeout_s=2.0,
                interval_s=0.01,
            )
        self.assertTrue(s.ok)
        self.assertEqual(s.gaks10, 0x8000)

    def test_wait_reticle_times_out_without_shift(self) -> None:
        idle = ReticleProbeSample(ok=True, label="B_AUTO", gaks10=0, tab10=0)
        with patch(
            "app.core.reticle_probe.sample_reticle_state",
            return_value=idle,
        ), patch("app.core.reticle_probe.time.sleep", return_value=None), patch(
            "app.core.reticle_probe.time.time", side_effect=[0.0, 0.1, 0.2, 9.0]
        ):
            s = wait_reticle_on_shift(
                MagicMock(pid=1, module_base=0x400000),
                timeout_s=1.0,
                interval_s=0.01,
            )
        self.assertFalse(s.ok)
        self.assertIn("timeout", s.error or "")


if __name__ == "__main__":
    unittest.main()
