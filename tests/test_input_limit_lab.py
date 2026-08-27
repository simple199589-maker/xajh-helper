from __future__ import annotations

import struct
import unittest
from unittest.mock import patch

from app.core.input_limit_lab import (
    DEFAULT_IMAGE_BASE,
    HEART_APTITUDE_INPUT_MODE,
    INPUT_CONTROL_MAX_OFFSET,
    INPUT_DIALOG_MODE_OFFSET,
    SUTRA_LEVEL_CAP_PATCHED,
    SUTRA_LEVEL_CAP_PATCH_OFFSET,
    SUTRA_LEVEL_CAP_SIGNATURE_NOTE_VA,
    SUTRA_LEVEL_CAP_SIGNATURE_ORIGINAL,
    probe_shown_input_limit,
    probe_sutra_level_cap_patch,
    probe_sutra_remaining_aptitude,
    set_shown_input_limit,
    set_shown_input_limit_to_remaining,
    set_sutra_level_cap_bypass,
)


class _FakePm:
    def __init__(self, values: dict[int, int]):
        self.values = dict(values)

    def read_bytes(self, addr: int, size: int) -> bytes:
        if size != 4 or addr not in self.values:
            raise RuntimeError(f"unexpected read 0x{addr:X}/{size}")
        return struct.pack("<I", self.values[addr])

    def write_bytes(self, addr: int, data: bytes, size: int) -> None:
        if size != 4 or len(data) != 4:
            raise RuntimeError("unexpected write")
        self.values[addr] = struct.unpack("<I", data)[0]


class _FakeCodePm:
    def __init__(self, address: int, data: bytes):
        self.address = int(address)
        self.data = bytearray(data)

    def read_bytes(self, addr: int, size: int) -> bytes:
        offset = int(addr) - self.address
        if offset < 0 or offset + int(size) > len(self.data):
            raise RuntimeError(f"unexpected read 0x{addr:X}/{size}")
        return bytes(self.data[offset : offset + int(size)])

    def write_bytes(self, addr: int, data: bytes, size: int) -> None:
        payload = bytes(data)
        if len(payload) != int(size):
            raise RuntimeError("unexpected write size")
        offset = int(addr) - self.address
        if offset < 0 or offset + len(payload) > len(self.data):
            raise RuntimeError(f"unexpected write 0x{addr:X}/{size}")
        self.data[offset : offset + len(payload)] = payload


class InputLimitLabTests(unittest.TestCase):
    def _session(self, *, limit: int = 600):
        dlg = 0x50000000
        ctrl = 0x51000000
        pm = _FakePm(
            {
                dlg + INPUT_DIALOG_MODE_OFFSET: HEART_APTITUDE_INPUT_MODE,
                ctrl: 0x012DAED4,
                ctrl + INPUT_CONTROL_MAX_OFFSET: limit,
            }
        )
        session = type("Session", (), {"pid": 123, "pm": pm})()
        return session, ctrl

    def test_probe_reads_live_edit_control_maximum(self) -> None:
        session, ctrl = self._session()
        with patch("app.core.plg_ui.get_game_ui_dlg", return_value=0x50000000), patch(
            "app.core.plg_ui.is_dlg_show", return_value=True
        ), patch("app.core.aui_click.get_aui_dlg_item_ptr", return_value=ctrl):
            out = probe_shown_input_limit(session)

        self.assertTrue(out["ok"])
        self.assertEqual(out["limit"], 600)
        self.assertEqual(out["limit_addr"], ctrl + INPUT_CONTROL_MAX_OFFSET)

    def test_patch_replaces_and_verifies_current_limit(self) -> None:
        session, ctrl = self._session()
        with patch("app.core.plg_ui.get_game_ui_dlg", return_value=0x50000000), patch(
            "app.core.plg_ui.is_dlg_show", return_value=True
        ), patch("app.core.aui_click.get_aui_dlg_item_ptr", return_value=ctrl):
            out = set_shown_input_limit(session, 99_999_999)

        self.assertTrue(out["ok"])
        self.assertEqual(out["old_limit"], 600)
        self.assertEqual(out["new_limit"], 99_999_999)
        self.assertEqual(
            session.pm.values[ctrl + INPUT_CONTROL_MAX_OFFSET], 99_999_999
        )

    def test_patch_accepts_zero_remaining_when_stats_are_already_600(self) -> None:
        session, ctrl = self._session(limit=0)
        with patch("app.core.plg_ui.get_game_ui_dlg", return_value=0x50000000), patch(
            "app.core.plg_ui.is_dlg_show", return_value=True
        ), patch("app.core.aui_click.get_aui_dlg_item_ptr", return_value=ctrl):
            out = set_shown_input_limit(session, 99_999_999)

        self.assertTrue(out["ok"])
        self.assertEqual(out["old_limit"], 0)
        self.assertEqual(out["new_limit"], 99_999_999)

    def test_probe_rejects_another_generic_input_mode(self) -> None:
        session, _ctrl = self._session()
        session.pm.values[0x50000000 + INPUT_DIALOG_MODE_OFFSET] = 0x19
        with patch("app.core.plg_ui.get_game_ui_dlg", return_value=0x50000000), patch(
            "app.core.plg_ui.is_dlg_show", return_value=True
        ):
            out = probe_shown_input_limit(session)

        self.assertFalse(out["ok"])
        self.assertIn("不是心法资质模式", out["error"])

    def test_probe_refuses_when_dialog_is_not_shown(self) -> None:
        session, _ctrl = self._session()
        with patch("app.core.plg_ui.get_game_ui_dlg", return_value=0x50000000), patch(
            "app.core.plg_ui.is_dlg_show", return_value=False
        ):
            out = probe_shown_input_limit(session)

        self.assertFalse(out["ok"])
        self.assertIn("未显示", out["error"])

    def test_reads_remaining_aptitude_from_sutra_left_point(self) -> None:
        session, _ctrl = self._session()
        with patch("app.core.plg_ui.get_game_ui_dlg", return_value=0x52000000), patch(
            "app.core.plg_ui.is_dlg_show", return_value=True
        ), patch(
            "app.core.aui_click.get_aui_dlg_item_ptr", return_value=0x53000000
        ), patch("app.core.map_fly._aui_get_text", return_value="4,420"):
            out = probe_sutra_remaining_aptitude(session)

        self.assertTrue(out["ok"])
        self.assertEqual(out["remaining"], 4420)
        self.assertEqual(out["control"], "Txt_LeftPoint")

    def test_input_maximum_uses_live_remaining_aptitude(self) -> None:
        session, ctrl = self._session(limit=0)
        with patch("app.core.plg_ui.get_game_ui_dlg") as get_dlg, patch(
            "app.core.plg_ui.is_dlg_show", return_value=True
        ), patch("app.core.aui_click.get_aui_dlg_item_ptr") as get_item, patch(
            "app.core.map_fly._aui_get_text", return_value="4420"
        ):
            get_dlg.side_effect = lambda _s, name, **_kw: (
                0x52000000 if name == "Win_SurtraPotential" else 0x50000000
            )
            get_item.side_effect = lambda _s, _dlg, name, **_kw: (
                0x53000000 if name == "Txt_LeftPoint" else ctrl
            )
            out = set_shown_input_limit_to_remaining(session)

        self.assertTrue(out["ok"])
        self.assertEqual(out["remaining"], 4420)
        self.assertEqual(out["new_limit"], 4420)
        self.assertEqual(session.pm.values[ctrl + INPUT_CONTROL_MAX_OFFSET], 4420)

    def test_sutra_level_cap_patch_is_reversible(self) -> None:
        base = 0x500000
        signature_addr = base + (
            SUTRA_LEVEL_CAP_SIGNATURE_NOTE_VA - DEFAULT_IMAGE_BASE
        )
        pm = _FakeCodePm(signature_addr, SUTRA_LEVEL_CAP_SIGNATURE_ORIGINAL)
        session = type("Session", (), {"pid": 123, "module_base": base, "pm": pm})()

        before = probe_sutra_level_cap_patch(session)
        enabled = set_sutra_level_cap_bypass(session, True)
        patched_bytes = bytes(
            pm.data[
                SUTRA_LEVEL_CAP_PATCH_OFFSET : SUTRA_LEVEL_CAP_PATCH_OFFSET + 2
            ]
        )
        disabled = set_sutra_level_cap_bypass(session, False)

        self.assertTrue(before["ok"])
        self.assertEqual(before["state"], "original")
        self.assertTrue(enabled["ok"])
        self.assertEqual(enabled["state"], "patched")
        self.assertEqual(patched_bytes, SUTRA_LEVEL_CAP_PATCHED)
        self.assertEqual(
            pm.data[
                SUTRA_LEVEL_CAP_PATCH_OFFSET : SUTRA_LEVEL_CAP_PATCH_OFFSET + 2
            ],
            bytearray(b"\x6A\xFF"),
        )
        self.assertTrue(disabled["ok"])
        self.assertEqual(disabled["state"], "original")

    def test_sutra_level_cap_patch_refuses_unknown_build(self) -> None:
        base = 0x500000
        signature_addr = base + (
            SUTRA_LEVEL_CAP_SIGNATURE_NOTE_VA - DEFAULT_IMAGE_BASE
        )
        bad = bytearray(SUTRA_LEVEL_CAP_SIGNATURE_ORIGINAL)
        bad[0] ^= 0xFF
        pm = _FakeCodePm(signature_addr, bytes(bad))
        session = type("Session", (), {"pid": 123, "module_base": base, "pm": pm})()

        out = set_sutra_level_cap_bypass(session, True)

        self.assertFalse(out["ok"])
        self.assertEqual(out["state"], "original")
        self.assertNotEqual(
            pm.data[
                SUTRA_LEVEL_CAP_PATCH_OFFSET : SUTRA_LEVEL_CAP_PATCH_OFFSET + 2
            ],
            bytearray(SUTRA_LEVEL_CAP_PATCHED),
        )


if __name__ == "__main__":
    unittest.main()
