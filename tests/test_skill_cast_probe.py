from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

from app.core.skill_cast_probe import (
    CAST_CANCEL_BUSY_BIT,
    CAST_CANCEL_GATE_MINIMAL,
    CastThisProbe,
    clear_cast_session,
    clear_cast_session_when_active,
    write_cast_timeline_report,
    write_onskill_stopped_report,
)


def _probe(*, skill_id: int, skill_id_b: int, flags_4a0: int) -> CastThisProbe:
    dump = bytearray(0x500)
    struct.pack_into("<I", dump, 0x10, skill_id)
    struct.pack_into("<I", dump, 0x80, skill_id_b)
    struct.pack_into("<I", dump, 0x7C, 0x10001 if skill_id else 0)
    struct.pack_into("<I", dump, 0x4A0, flags_4a0)
    return CastThisProbe(ok=True, cast_this=0x1000, cast_dump=bytes(dump))


class SkillCastProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._write_gate = patch(
            "app.core.skill_cast_probe._unsafe_skill_write_block_reason",
            return_value=None,
        )
        self._write_gate.start()
        self.addCleanup(self._write_gate.stop)

    def test_timeline_report_is_beside_running_executable(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch(
            "common.paths.app_root", return_value=Path(td)
        ):
            path = Path(write_cast_timeline_report([]))
        self.assertEqual(path.parent, Path(td) / ".issues" / "lab")

    def test_onskill_stopped_report_records_before_after(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch(
            "common.paths.app_root", return_value=Path(td)
        ):
            path = Path(
                write_onskill_stopped_report(
                    before={
                        "+10": 0x12601,
                        "+14": 0xABCD,
                        "+18": 0x55,
                        "+4A0": 1,
                        "+20": 0x614,
                    },
                    after={
                        "+10": 0,
                        "+14": 0,
                        "+18": 0,
                        "+4A0": 0,
                        "+20": 0x614,
                    },
                    bridge_ok=True,
                    bridge_note=(
                        "ONSKILL_STOPPED ok id=0x12601,0xABCD,0x55 cleared=1 "
                        "(identity-match; restart client after lab)"
                    ),
                    cast_this=0x500170C8,
                    host_ptr=0x543DC570,
                )
            )
            text = path.read_text(encoding="utf-8")
        self.assertEqual(path.parent, Path(td) / ".issues" / "lab")
        self.assertIn("+14 identity_b", text)
        self.assertIn("expected_identity=(0x12601, 0xABCD, 0x55)", text)
        self.assertIn("identity_tuple=(75265, 43981, 85)", text)
        self.assertIn("identity_block_cleared=True", text)
        self.assertIn("native_diagnostic=", text)
        self.assertIn("identity-match", text)
        self.assertIn("before id=0x12601", text)
        self.assertIn("after id=0x0", text)
        self.assertIn("cleared=True", text)
        self.assertIn("recast_ok=None", text)
        self.assertIn("damage_ok=None", text)
        self.assertIn("0x582350", text)

    def test_onskill_stopped_report_marks_uncleared_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch(
            "common.paths.app_root", return_value=Path(td)
        ):
            path = Path(
                write_onskill_stopped_report(
                    before={"+10": 0x12601, "+14": 1, "+18": 2, "+4A0": 1},
                    after={"+10": 0x12601, "+14": 1, "+18": 2, "+4A0": 0},
                    bridge_ok=True,
                    bridge_note="ONSKILL_STOPPED ok id=0x12601,0x1,0x2 cleared=0",
                )
            )
            text = path.read_text(encoding="utf-8")
        self.assertIn("identity_block_cleared=False", text)
        self.assertIn("cleared=False", text)

    @patch("app.core.skill_cast_probe.remote_write_bytes")
    @patch("app.core.skill_cast_probe.resolve_cast_this")
    def test_busy_variant_preserves_other_4a0_bits(self, resolve, write) -> None:
        resolve.side_effect = [
            _probe(skill_id=0x9563, skill_id_b=0, flags_4a0=0xB),
            _probe(skill_id=0x9563, skill_id_b=0, flags_4a0=0xA),
        ]
        session = type("Session", (), {"pid": 7})()
        out = clear_cast_session(session, variant=CAST_CANCEL_BUSY_BIT)
        self.assertTrue(out["ok"])
        write.assert_called_once_with(7, 0x14A0, struct.pack("<I", 0xA))
        self.assertEqual(out["variant"], CAST_CANCEL_BUSY_BIT)

    @patch("app.core.skill_cast_probe.remote_read_bytes")
    @patch("app.core.skill_cast_probe.remote_write_bytes")
    @patch("app.core.skill_cast_probe.resolve_cast_this")
    def test_gate_minimal_touches_only_ids_busy_and_mirror(
        self, resolve, write, read
    ) -> None:
        resolve.side_effect = [
            _probe(skill_id=0x9563, skill_id_b=0x9564, flags_4a0=3),
            _probe(skill_id=0, skill_id_b=0, flags_4a0=2),
        ]
        read.return_value = bytes.fromhex("01706c00")
        session = type("Session", (), {"pid": 8})()
        out = clear_cast_session(session, variant=CAST_CANCEL_GATE_MINIMAL)
        self.assertTrue(out["ok"])
        self.assertEqual(
            [call.args[1] for call in write.call_args_list],
            [0x14A0, 0x1010, 0x1080, 0x1024],
        )
        self.assertEqual(write.call_args_list[0].args[2], struct.pack("<I", 2))
        self.assertEqual(write.call_args_list[-1].args[2], bytes.fromhex("00706c00"))

    @patch("app.core.skill_cast_probe.resolve_cast_this")
    def test_idle_session_is_not_modified(self, resolve) -> None:
        resolve.return_value = _probe(skill_id=0, skill_id_b=0, flags_4a0=0)
        session = type("Session", (), {"pid": 9})()
        with patch("app.core.skill_cast_probe.remote_write_bytes") as write:
            out = clear_cast_session(session, variant=CAST_CANCEL_BUSY_BIT)
        self.assertTrue(out["skipped_idle"])
        write.assert_not_called()

    def test_write_is_fail_closed_without_explicit_lab_gate(self) -> None:
        self._write_gate.stop()
        with patch.dict("os.environ", {}, clear=True), patch(
            "app.core.build_profile.is_dev_build", return_value=True
        ):
            out = clear_cast_session(type("Session", (), {"pid": 9})())
        self.assertFalse(out["ok"])
        self.assertIn("XAJH_ENABLE_UNSAFE_SKILL_WRITE", out["error"])

    @patch("app.core.skill_cast_probe.time.sleep", return_value=None)
    @patch("app.core.skill_cast_probe.clear_cast_session")
    @patch("app.core.skill_cast_probe.resolve_cast_this")
    def test_waiter_ignores_id_load_until_busy_bit_is_armed(
        self, resolve, clear, _sleep
    ) -> None:
        resolve.side_effect = [
            _probe(skill_id=0x9563, skill_id_b=0, flags_4a0=0),
            _probe(skill_id=0x9563, skill_id_b=0, flags_4a0=1),
        ]
        clear.return_value = {"ok": True}
        session = type("Session", (), {"pid": 10})()
        out = clear_cast_session_when_active(
            session, rounds=2, interval_s=0, variant=CAST_CANCEL_BUSY_BIT
        )
        self.assertEqual(out["wait_rounds"], 2)
        clear.assert_called_once_with(
            session,
            log=ANY,
            require_active=False,
            variant=CAST_CANCEL_BUSY_BIT,
        )


if __name__ == "__main__":
    unittest.main()
