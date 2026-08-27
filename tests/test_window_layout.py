from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import win_utils
from app.core.window_layout import (
    align_game_render_size_to_nearest_16x9,
    client_size_matches,
    get_window_dpi,
    nearest_16x9_n,
    normalize_game_window,
    normalize_game_window_to_nearest_16x9,
    prompt_and_normalize_game_window,
)


class _FakeUser32:
    def __init__(self, *, client=(800, 600), window=(10, 20, 826, 639), pid=7, dpi=96):
        self.client = tuple(client)
        self.window = tuple(window)
        self.pid = int(pid)
        self.dpi = int(dpi)
        self.valid = True
        self.zoomed = False
        self.style = 0x00CF0000
        self.set_calls = []
        self.set_ok = True
        self.adjust_for_dpi_calls = []

    def IsWindow(self, _hwnd):
        return self.valid

    def GetWindowThreadProcessId(self, _hwnd, out):
        out._obj.value = self.pid
        return 1

    def GetClientRect(self, _hwnd, out):
        out._obj.left = 0
        out._obj.top = 0
        out._obj.right = self.client[0]
        out._obj.bottom = self.client[1]
        return 1

    def GetWindowRect(self, _hwnd, out):
        out._obj.left, out._obj.top, out._obj.right, out._obj.bottom = self.window
        return 1

    def IsZoomed(self, _hwnd):
        return self.zoomed

    def GetWindowLongPtrW(self, _hwnd, index):
        return self.style if index == -16 else 0

    def GetDpiForWindow(self, _hwnd):
        return self.dpi

    def AdjustWindowRectEx(self, out, _style, _menu, _exstyle):
        out._obj.left = -8
        out._obj.top = -31
        out._obj.right = out._obj.right + 8
        out._obj.bottom = out._obj.bottom + 8
        return 1

    def AdjustWindowRectExForDpi(self, out, _style, _menu, _exstyle, dpi):
        # 150% DPI: thicker non-client frame than 96dpi path.
        dpi = int(getattr(dpi, "value", dpi))
        self.adjust_for_dpi_calls.append(dpi)
        pad_x = 10 if dpi >= 144 else 8
        pad_y_top = 39 if dpi >= 144 else 31
        pad_y_bot = 10 if dpi >= 144 else 8
        out._obj.left = -pad_x
        out._obj.top = -pad_y_top
        out._obj.right = out._obj.right + pad_x
        out._obj.bottom = out._obj.bottom + pad_y_bot
        return 1

    def SetWindowPos(self, _hwnd, _insert, left, top, width, height, flags):
        self.set_calls.append((left, top, width, height, flags))
        if not self.set_ok:
            return 0
        self.window = (left, top, left + width, top + height)
        # Optional first-pass DPI/DWM miss: client is off by self.client_bias.
        bias = getattr(self, "client_bias", (0, 0))
        # Mirror AdjustWindowRectExForDpi frame: outer - 2*pad_x, - (top+bot).
        if self.adjust_for_dpi_calls and self.adjust_for_dpi_calls[-1] >= 144:
            frame_x, frame_y = 20, 49
        else:
            frame_x, frame_y = 16, 39
        if getattr(self, "bias_once", False) and len(self.set_calls) == 1:
            self.client = (width - frame_x + bias[0], height - frame_y + bias[1])
        elif getattr(self, "bias_once", False):
            self.client = (width - frame_x, height - frame_y)
        else:
            self.client = (width - frame_x + bias[0], height - frame_y + bias[1])
        return 1


class WindowLayoutTests(unittest.TestCase):
    def _run(self, fake: _FakeUser32, **kwargs):
        with patch.object(win_utils, "user32", fake), patch.object(
            win_utils, "query_process_image_path", return_value=r"D:\game\xajh.exe"
        ):
            return normalize_game_window(100, 7, **kwargs)

    def test_invalid_hwnd_is_rejected(self):
        fake = _FakeUser32()
        fake.valid = False
        result = self._run(fake)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "invalid_hwnd")
        self.assertEqual(fake.set_calls, [])

    def test_cross_process_window_is_rejected(self):
        result = self._run(_FakeUser32(pid=8))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "hwnd_pid_mismatch")

    def test_non_xajh_process_is_rejected(self):
        fake = _FakeUser32()
        with patch.object(win_utils, "user32", fake), patch.object(
            win_utils, "query_process_image_path", return_value=r"D:\other.exe"
        ):
            result = normalize_game_window(100, 7)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "target_process_not_xajh")

    def test_already_target_does_not_resize(self):
        fake = _FakeUser32(client=(1427, 801))
        result = self._run(fake)
        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(result.reason, "already_target")
        self.assertEqual(fake.set_calls, [])

    def test_adjusts_outer_size_to_target_client(self):
        fake = _FakeUser32()
        result = self._run(fake)
        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertEqual(result.before_client, (800, 600))
        self.assertEqual(result.after_client, (1427, 801))
        self.assertEqual(fake.set_calls[0][0:4], (10, 20, 1443, 840))

    def test_maximized_window_is_not_modified(self):
        fake = _FakeUser32()
        fake.zoomed = True
        result = self._run(fake)
        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(result.reason, "maximized_or_fullscreen")
        self.assertEqual(fake.set_calls, [])

    def test_set_window_pos_failure_is_explicit(self):
        fake = _FakeUser32()
        fake.set_ok = False
        result = self._run(fake)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "set_window_pos_failed")

    def test_borderless_monitor_sized_window_is_treated_as_fullscreen(self):
        fake = _FakeUser32(window=(0, 0, 1920, 1080), client=(1920, 1080))
        fake.style = 0

        def monitor_from_window(_hwnd, _flags):
            return 1

        def get_monitor_info(_monitor, out):
            info = out._obj
            info.rcMonitor.left = 0
            info.rcMonitor.top = 0
            info.rcMonitor.right = 1920
            info.rcMonitor.bottom = 1080
            return 1

        fake.MonitorFromWindow = monitor_from_window
        fake.GetMonitorInfoW = get_monitor_info
        result = self._run(fake)
        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(result.reason, "maximized_or_fullscreen")
        self.assertEqual(fake.set_calls, [])

    def test_prompt_helper_skips_prompt_when_already_target(self):
        fake = _FakeUser32(client=(1427, 801))
        asked = []
        with patch.object(win_utils, "user32", fake):
            result = prompt_and_normalize_game_window(
                100,
                7,
                client_width=1427,
                client_height=801,
                ask=lambda prompt: asked.append(prompt) or True,
                prompt="resize",
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "already_target")
        self.assertEqual(asked, [])

    def test_prompt_helper_decline_blocks_without_resize(self):
        fake = _FakeUser32(client=(800, 600))
        asked = []
        with patch.object(win_utils, "user32", fake):
            result = prompt_and_normalize_game_window(
                100,
                7,
                client_width=1427,
                client_height=801,
                ask=lambda prompt: asked.append(prompt) or False,
                prompt="resize",
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "user_declined")
        self.assertEqual(fake.set_calls, [])
        self.assertEqual(asked, ["resize"])

    def test_prompt_helper_accepts_and_verifies_resize(self):
        fake = _FakeUser32(client=(800, 600))
        with patch.object(win_utils, "user32", fake), patch.object(
            win_utils, "query_process_image_path", return_value=r"D:\game\xajh.exe"
        ):
            result = prompt_and_normalize_game_window(
                100,
                7,
                client_width=1427,
                client_height=801,
                ask=lambda _prompt: True,
                prompt="resize",
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.after_client, (1427, 801))
        self.assertEqual(len(fake.set_calls), 1)

    def test_client_size_matches_allows_one_pixel_tolerance(self):
        self.assertTrue(client_size_matches((1428, 802), (1427, 801)))
        self.assertTrue(client_size_matches((1426, 800), (1427, 801)))
        self.assertFalse(client_size_matches((1429, 802), (1427, 801)))
        self.assertFalse(client_size_matches(None, (1427, 801)))

    def test_already_target_within_tolerance_skips_resize(self):
        fake = _FakeUser32(client=(1428, 802))
        result = self._run(fake)
        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(result.reason, "already_target")
        self.assertEqual(fake.set_calls, [])

    def test_corrective_pass_fixes_first_pass_off_by_one(self):
        fake = _FakeUser32(client=(800, 600))
        fake.client_bias = (1, 1)
        fake.bias_once = True
        result = self._run(fake)
        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertEqual(result.reason, "normalized")
        self.assertEqual(result.after_client, (1427, 801))
        self.assertGreaterEqual(len(fake.set_calls), 2)
        # Second pass shrinks outer by the measured +1/+1 client overshoot.
        self.assertEqual(
            fake.set_calls[1][2:4],
            (fake.set_calls[0][2] - 1, fake.set_calls[0][3] - 1),
        )

    def test_persistent_off_by_one_after_resize_is_accepted(self):
        fake = _FakeUser32(client=(800, 600))
        fake.client_bias = (1, 1)
        fake.bias_once = False
        result = self._run(fake)
        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertEqual(result.reason, "normalized")
        self.assertTrue(client_size_matches(result.after_client, (1427, 801)))

    def test_prompt_helper_accepts_near_target_without_prompt(self):
        fake = _FakeUser32(client=(1428, 802))
        asked = []
        with patch.object(win_utils, "user32", fake):
            result = prompt_and_normalize_game_window(
                100,
                7,
                client_width=1427,
                client_height=801,
                ask=lambda prompt: asked.append(prompt) or True,
                prompt="resize",
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "already_target")
        self.assertEqual(asked, [])

    def test_get_window_dpi_uses_per_window_api(self):
        fake = _FakeUser32(dpi=144)
        with patch.object(win_utils, "user32", fake):
            self.assertEqual(get_window_dpi(100), 144)

    def test_high_dpi_uses_adjust_window_rect_ex_for_dpi(self):
        fake = _FakeUser32(dpi=144)
        result = self._run(fake)
        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertEqual(result.dpi, 144)
        self.assertEqual(result.after_client, (1427, 801))
        self.assertEqual(fake.adjust_for_dpi_calls[0], 144)
        # 150% frame: +20x +49 vs 100% +16x +39
        self.assertEqual(fake.set_calls[0][0:4], (10, 20, 1447, 850))


class Nearest16x9Tests(unittest.TestCase):
    def test_nearest_16x9_n_picks_minimal_pixel_distance(self):
        self.assertEqual(nearest_16x9_n(1700, 900), 105)  # -> 1680x945
        self.assertEqual(nearest_16x9_n(1600, 900), 100)  # exact 16:9 stays
        self.assertEqual(nearest_16x9_n(1920, 1080), 120)
        self.assertEqual(nearest_16x9_n(2000, 1000), 122)  # 2:1 -> grows height
        self.assertEqual(nearest_16x9_n(800, 600), 54)  # 4:3 -> 864x486

    def test_snap_already_16x9_client_is_not_modified(self):
        fake = _FakeUser32(client=(1600, 900))
        with patch.object(win_utils, "user32", fake), patch.object(
            win_utils, "query_process_image_path", return_value=r"D:\game\xajh.exe"
        ):
            result = normalize_game_window_to_nearest_16x9(100, 7)
        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(result.reason, "already_16x9")
        self.assertEqual(fake.set_calls, [])

    def test_snap_near_16x9_within_tolerance_is_not_modified(self):
        fake = _FakeUser32(client=(1366, 768))
        with patch.object(win_utils, "user32", fake), patch.object(
            win_utils, "query_process_image_path", return_value=r"D:\game\xajh.exe"
        ):
            result = normalize_game_window_to_nearest_16x9(100, 7)
        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(result.reason, "already_16x9")

    def test_snap_resizes_to_nearest_16x9_of_current_size(self):
        fake = _FakeUser32(client=(1700, 900))
        with patch.object(win_utils, "user32", fake), patch.object(
            win_utils, "query_process_image_path", return_value=r"D:\game\xajh.exe"
        ):
            result = normalize_game_window_to_nearest_16x9(100, 7)
        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertEqual(result.reason, "normalized")
        self.assertEqual(result.target_client, (1680, 945))
        self.assertEqual(result.after_client, (1680, 945))

    def test_snap_clamps_target_into_monitor_work_area(self):
        fake = _FakeUser32(client=(2000, 1000))

        def monitor_from_window(_hwnd, _flags):
            return 1

        def get_monitor_info(_monitor, out):
            info = out._obj
            info.rcWork.left = 0
            info.rcWork.top = 0
            info.rcWork.right = 1920
            info.rcWork.bottom = 1040
            return 1

        fake.MonitorFromWindow = monitor_from_window
        fake.GetMonitorInfoW = get_monitor_info
        with patch.object(win_utils, "user32", fake), patch.object(
            win_utils, "query_process_image_path", return_value=r"D:\game\xajh.exe"
        ):
            result = normalize_game_window_to_nearest_16x9(100, 7)
        # Nearest would be 1952x1098 (outer 1137 > work 1040); largest fitting
        # whole 16:9 step is n=111 -> 1776x999 (outer 1792x1038).
        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertEqual(result.target_client, (1776, 999))
        self.assertEqual(result.after_client, (1776, 999))

    def test_snap_maximized_window_passes_through_unmodified(self):
        fake = _FakeUser32(client=(1700, 900))
        fake.zoomed = True
        with patch.object(win_utils, "user32", fake), patch.object(
            win_utils, "query_process_image_path", return_value=r"D:\game\xajh.exe"
        ):
            result = normalize_game_window_to_nearest_16x9(100, 7)
        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(result.reason, "maximized_or_fullscreen")
        self.assertEqual(fake.set_calls, [])


class RenderSizeIniTests(unittest.TestCase):
    """align_game_render_size_to_nearest_16x9 on userdata/systemsettings.ini."""

    def _write_ini(self, root: Path, body: str) -> Path:
        ini = root / "userdata" / "systemsettings.ini"
        ini.parent.mkdir(parents=True, exist_ok=True)
        ini.write_bytes(body.encode("gbk"))
        return ini

    _TPL = (
        "[Info]\r\ncard = test\r\n\r\n[Video]\r\nRenderWid = {w}\r\n"
        "RenderHei = {h}\r\nFullScreen = {fs}\r\nGammaNew = 100\r\n\r\n"
        "[Game]\r\n备注 = 窗口尺寸\r\nCamMode = 1\r\n"
    )

    def test_aligns_saved_render_size_to_nearest_16x9(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ini = self._write_ini(root, self._TPL.format(w=1700, h=900, fs=0))
            out = align_game_render_size_to_nearest_16x9(root)
            self.assertTrue(out["ok"])
            self.assertTrue(out["changed"])
            self.assertEqual(out["reason"], "aligned")
            self.assertEqual(out["before"], (1700, 900))
            self.assertEqual(out["after"], (1680, 945))
            text = ini.read_text(encoding="gbk")
            self.assertIn("RenderWid = 1680", text)
            self.assertIn("RenderHei = 945", text)
            # untouched keys / sections / GBK text survive byte-format intact
            self.assertIn("GammaNew = 100", text)
            self.assertIn("备注 = 窗口尺寸", text)
            self.assertIn("[Game]", text)
            data = ini.read_bytes()
            self.assertTrue(data.startswith(b"[Info]\r\n"))
            self.assertIn(b"RenderWid = 1680\r\n", data)
            self.assertIn(b"RenderHei = 945\r\n", data)

    def test_already_16x9_saved_size_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ini = self._write_ini(root, self._TPL.format(w=1600, h=900, fs=0))
            before = ini.read_bytes()
            out = align_game_render_size_to_nearest_16x9(root)
            self.assertTrue(out["ok"])
            self.assertFalse(out["changed"])
            self.assertEqual(out["reason"], "already_16x9")
            self.assertEqual(ini.read_bytes(), before)

    def test_fullscreen_saved_mode_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ini = self._write_ini(root, self._TPL.format(w=1700, h=900, fs=1))
            before = ini.read_bytes()
            out = align_game_render_size_to_nearest_16x9(root)
            self.assertTrue(out["ok"])
            self.assertFalse(out["changed"])
            self.assertEqual(out["reason"], "fullscreen_mode")
            self.assertEqual(ini.read_bytes(), before)

    def test_missing_render_keys_fails_without_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ini = self._write_ini(root, "[Video]\r\nFullScreen = 0\r\n")
            out = align_game_render_size_to_nearest_16x9(root)
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "render_size_unavailable")
            self.assertEqual(
                ini.read_bytes(), b"[Video]\r\nFullScreen = 0\r\n"
            )

    def test_missing_ini_file_reports_read_failed(self):
        with tempfile.TemporaryDirectory() as td:
            out = align_game_render_size_to_nearest_16x9(Path(td))
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "read_failed: " + out["reason"].split(": ", 1)[1])


if __name__ == "__main__":
    unittest.main()
