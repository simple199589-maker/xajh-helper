# -*- coding: utf-8 -*-
"""Game-window layout helpers.

The automation and captcha pipeline operate in client coordinates.  Keep the
normalization here so both the tray shell and the development workbench use
the same, PID-scoped Win32 behavior.
"""
from __future__ import annotations

import ctypes
import math
import os
import re
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.core import win_utils

# Fixed client baseline used by the current Yaolu/captcha workflow.
# Physical client pixels at the game HWND's monitor DPI (not scaled logical units).
DEFAULT_GAME_CLIENT_WIDTH = 1427
DEFAULT_GAME_CLIENT_HEIGHT = 801
# Win32 AdjustWindowRectEx + DPI/DWM often lands ±1px off the requested client.
CLIENT_SIZE_TOLERANCE_PX = 1
# Aspect-ratio distance under which a client already counts as 16:9.
ASPECT_16_9_TOL = 0.01
# Smallest 16:9 step (320x240) normalize_game_window still accepts.
MIN_NEAREST_16X9_N = 20
# Typical Win32 caption+frame overhead subtracted when pre-clamping the game's
# saved render size against the primary work area (ini stores client pixels).
RENDER_INI_CLAMP_MARGIN = (16, 40)
# One corrective SetWindowPos after the first miss is usually enough.
NORMALIZE_CORRECTIVE_PASSES = 2
# Standard 100% display DPI; used as fallback and scale reference.
USER_DEFAULT_SCREEN_DPI = 96

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
MONITOR_DEFAULTTONEAREST = 2


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", win_utils.RECT),
        ("rcWork", win_utils.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class WindowNormalizeResult:
    """Outcome of one guarded game-window normalization attempt."""

    ok: bool
    changed: bool = False
    reason: str = ""
    hwnd: int = 0
    pid: int = 0
    before_window: tuple[int, int, int, int] = (0, 0, 0, 0)
    before_client: tuple[int, int] = (0, 0)
    after_client: tuple[int, int] = (0, 0)
    target_client: tuple[int, int] = (0, 0)
    dpi: int = 0


def get_window_dpi(hwnd: int) -> int:
    """Return the per-monitor DPI for hwnd (96 = 100% scale)."""
    if not hwnd:
        return USER_DEFAULT_SCREEN_DPI
    prev = win_utils._dpi_ctx_per_monitor()
    try:
        fn = getattr(win_utils.user32, "GetDpiForWindow", None)
        if fn is not None:
            try:
                dpi = int(fn(wintypes.HWND(hwnd)) or 0)
            except TypeError:
                dpi = int(fn(int(hwnd)) or 0)
            if dpi > 0:
                return dpi
        sys_fn = getattr(win_utils.user32, "GetDpiForSystem", None)
        if sys_fn is not None:
            dpi = int(sys_fn() or 0)
            if dpi > 0:
                return dpi
    except Exception:
        pass
    finally:
        win_utils._dpi_ctx_restore(prev)
    return USER_DEFAULT_SCREEN_DPI


def get_client_size(hwnd: int) -> tuple[int, int] | None:
    """Return physical client width/height under per-monitor DPI awareness."""
    if not hwnd or not win_utils.user32.IsWindow(wintypes.HWND(hwnd)):
        return None
    prev = win_utils._dpi_ctx_per_monitor()
    try:
        rc = win_utils.RECT()
        if not win_utils.user32.GetClientRect(
            wintypes.HWND(hwnd), ctypes.byref(rc)
        ):
            return None
        width = int(rc.right - rc.left)
        height = int(rc.bottom - rc.top)
        if width <= 0 or height <= 0:
            return None
        return width, height
    finally:
        win_utils._dpi_ctx_restore(prev)


def client_size_matches(
    actual: tuple[int, int] | None,
    target: tuple[int, int],
    *,
    tolerance: int = CLIENT_SIZE_TOLERANCE_PX,
) -> bool:
    """True when actual client size is within tolerance of target."""
    if actual is None:
        return False
    tol = max(0, int(tolerance))
    return (
        abs(int(actual[0]) - int(target[0])) <= tol
        and abs(int(actual[1]) - int(target[1])) <= tol
    )


def _window_long(hwnd: int, index: int) -> int:
    fn = getattr(win_utils.user32, "GetWindowLongPtrW", None)
    if fn is None:
        fn = win_utils.user32.GetWindowLongW
    return int(fn(wintypes.HWND(hwnd), index))


def _is_maximized_or_fullscreen(hwnd: int, rect: tuple[int, int, int, int]) -> bool:
    if bool(win_utils.user32.IsZoomed(wintypes.HWND(hwnd))):
        return True
    style = _window_long(hwnd, GWL_STYLE)
    # Borderless windows occupying the monitor are treated as full-screen.
    if style & (WS_CAPTION | WS_THICKFRAME):
        return False
    monitor_from_window = getattr(win_utils.user32, "MonitorFromWindow", None)
    get_monitor_info = getattr(win_utils.user32, "GetMonitorInfoW", None)
    if monitor_from_window is None or get_monitor_info is None:
        return False
    monitor = monitor_from_window(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
    if not monitor:
        return False
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not get_monitor_info(monitor, ctypes.byref(info)):
        return False
    return rect == (
        int(info.rcMonitor.left),
        int(info.rcMonitor.top),
        int(info.rcMonitor.right),
        int(info.rcMonitor.bottom),
    )


def _process_is_xajh(pid: int) -> bool:
    path = win_utils.query_process_image_path(int(pid))
    if path:
        return os.path.basename(path.replace("/", "\\")).lower() == "xajh.exe"
    try:
        import psutil

        return (psutil.Process(int(pid)).name() or "").lower() == "xajh.exe"
    except Exception:
        return False


def _outer_size_for_client(
    hwnd: int,
    width: int,
    height: int,
    *,
    dpi: int | None = None,
) -> tuple[int, int] | None:
    """Map desired client size to outer window size at the HWND's DPI."""
    style = _window_long(hwnd, GWL_STYLE)
    exstyle = _window_long(hwnd, GWL_EXSTYLE)
    rect = win_utils.RECT(0, 0, int(width), int(height))
    dpi_val = int(dpi or get_window_dpi(hwnd) or USER_DEFAULT_SCREEN_DPI)
    # Prefer DPI-aware frame metrics so 125%/150% displays do not mis-size.
    adjust_dpi = getattr(win_utils.user32, "AdjustWindowRectExForDpi", None)
    if adjust_dpi is not None:
        try:
            ok = bool(
                adjust_dpi(
                    ctypes.byref(rect),
                    int(style),
                    False,
                    int(exstyle),
                    int(dpi_val),
                )
            )
        except TypeError:
            ok = False
        except Exception:
            ok = False
        if ok:
            return int(rect.right - rect.left), int(rect.bottom - rect.top)
    adjust = getattr(win_utils.user32, "AdjustWindowRectEx", None)
    if adjust is not None and adjust(
        ctypes.byref(rect), style, False, exstyle
    ):
        return int(rect.right - rect.left), int(rect.bottom - rect.top)
    # Conservative fallback for test doubles and old user32 shims.
    current = win_utils.get_window_rect(hwnd)
    client = get_client_size(hwnd)
    if client is None or current == (0, 0, 0, 0):
        return None
    return (
        int(width) + (current[2] - current[0] - client[0]),
        int(height) + (current[3] - current[1] - client[1]),
    )


def normalize_game_window(
    hwnd: int,
    pid: int,
    *,
    client_width: int = DEFAULT_GAME_CLIENT_WIDTH,
    client_height: int = DEFAULT_GAME_CLIENT_HEIGHT,
    log: Callable[[str], None] | None = None,
) -> WindowNormalizeResult:
    """Set one xajh window to the requested physical client size.

    The target is fixed physical pixels (captcha baseline). Outer frame size is
    computed with per-monitor DPI so 125%/150% displays do not drift. Invalid /
    cross-process HWNDs, unknown processes and maximized/full-screen windows
    are never modified.
    """
    log = log or (lambda _message: None)
    target = (int(client_width), int(client_height))
    base = dict(hwnd=int(hwnd or 0), pid=int(pid or 0), target_client=target)
    if target[0] < 320 or target[1] < 240:
        return WindowNormalizeResult(False, reason="invalid_target_client", **base)
    if not hwnd or not win_utils.user32.IsWindow(wintypes.HWND(hwnd)):
        return WindowNormalizeResult(False, reason="invalid_hwnd", **base)
    owner_pid, _tid = win_utils.get_window_thread_process_id(hwnd)
    if int(owner_pid) != int(pid):
        return WindowNormalizeResult(False, reason="hwnd_pid_mismatch", **base)
    if not _process_is_xajh(pid):
        return WindowNormalizeResult(False, reason="target_process_not_xajh", **base)

    # Match game process coordinate space for GetClientRect / SetWindowPos.
    prev_dpi = win_utils._dpi_ctx_per_monitor()
    try:
        dpi = get_window_dpi(hwnd)
        base["dpi"] = int(dpi)
        before_window = win_utils.get_window_rect(hwnd)
        before_client = get_client_size(hwnd)
        if before_client is None:
            return WindowNormalizeResult(
                False, reason="client_rect_unavailable", **base
            )
        base.update(before_window=before_window, before_client=before_client)
        if _is_maximized_or_fullscreen(hwnd, before_window):
            return WindowNormalizeResult(
                True,
                reason="maximized_or_fullscreen",
                after_client=before_client,
                **base,
            )
        if client_size_matches(before_client, target):
            return WindowNormalizeResult(
                True,
                reason="already_target",
                after_client=before_client,
                **base,
            )

        left, top = before_window[0], before_window[1]
        flags = SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
        after = before_client
        changed = False
        # First pass uses AdjustWindowRectExForDpi. Later passes correct outer
        # size by measured client delta (DWM off-by-one). Final accept ±1px.
        for pass_idx in range(int(NORMALIZE_CORRECTIVE_PASSES) + 1):
            if after == target:
                break
            if pass_idx == 0:
                outer = _outer_size_for_client(hwnd, *target, dpi=dpi)
            else:
                current_window = win_utils.get_window_rect(hwnd)
                if current_window == (0, 0, 0, 0) or after is None:
                    outer = None
                else:
                    outer_w = (current_window[2] - current_window[0]) + (
                        target[0] - after[0]
                    )
                    outer_h = (current_window[3] - current_window[1]) + (
                        target[1] - after[1]
                    )
                    outer = (int(outer_w), int(outer_h))
                    left, top = current_window[0], current_window[1]
            if outer is None or outer[0] <= 0 or outer[1] <= 0:
                return WindowNormalizeResult(
                    False,
                    reason="outer_size_unavailable",
                    after_client=after,
                    **base,
                )
            moved = bool(
                win_utils.user32.SetWindowPos(
                    wintypes.HWND(hwnd),
                    0,
                    int(left),
                    int(top),
                    int(outer[0]),
                    int(outer[1]),
                    flags,
                )
            )
            after = get_client_size(hwnd) or (0, 0)
            changed = True
            if not moved:
                return WindowNormalizeResult(
                    False,
                    reason="set_window_pos_failed",
                    after_client=after,
                    **base,
                )

        ok = client_size_matches(after, target)
        result = WindowNormalizeResult(
            ok,
            changed=changed,
            reason="normalized" if ok else "client_size_mismatch",
            after_client=after,
            **base,
        )
        log(
            f"game window normalize pid={pid} hwnd=0x{int(hwnd):X} dpi={dpi} "
            f"before_window={before_window} before_client={before_client} "
            f"target_client={target} after_client={after} reason={result.reason}"
        )
        return result
    finally:
        win_utils._dpi_ctx_restore(prev_dpi)


def nearest_16x9_n(width: int, height: int) -> int:
    """Integer step ``n`` whose (16n, 9n) client is nearest to (width, height).

    Minimizes the squared pixel distance ``(w - 16n)^2 + (h - 9n)^2``. The
    convex quadratic is extremal at ``n* = (16w + 9h) / 337``, so the integer
    optimum is always floor(n*) or ceil(n*).

    @author by ak
    """
    w = max(1, int(width))
    h = max(1, int(height))
    n_star = (16 * w + 9 * h) / 337.0
    candidates = {max(1, int(math.floor(n_star))), max(1, int(math.ceil(n_star)))}
    return min(candidates, key=lambda n: (w - 16 * n) ** 2 + (h - 9 * n) ** 2)


def monitor_work_area(hwnd: int) -> tuple[int, int, int, int] | None:
    """Work-area rect (l, t, r, b) of the monitor owning hwnd, or None.

    @author by ak
    """
    fn = getattr(win_utils.user32, "MonitorFromWindow", None)
    get_info = getattr(win_utils.user32, "GetMonitorInfoW", None)
    if fn is None or get_info is None or not hwnd:
        return None
    try:
        monitor = int(fn(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST) or 0)
    except Exception:
        return None
    if not monitor:
        return None
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not get_info(monitor, ctypes.byref(info)):
        return None
    rc = info.rcWork
    return (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))


def normalize_game_window_to_nearest_16x9(
    hwnd: int,
    pid: int,
    *,
    log: Callable[[str], None] | None = None,
) -> WindowNormalizeResult:
    """Snap a non-16:9 game window to its nearest 16:9 physical client size.

    Login click coordinates assume a 16:9 client. When the live client ratio
    deviates by more than ``ASPECT_16_9_TOL``, resize to the closest (16n, 9n)
    client derived from the CURRENT size (minimal change). When the target
    outer window would not fit the monitor work area, shrink in whole 16:9
    steps until it does. Already-proportional, maximized/full-screen and
    invalid windows are never modified.

    @author by ak
    """
    log = log or (lambda _message: None)
    base = dict(hwnd=int(hwnd or 0), pid=int(pid or 0))
    current = get_client_size(hwnd)
    if current is None:
        return WindowNormalizeResult(False, reason="client_rect_unavailable", **base)
    dpi = get_window_dpi(hwnd)
    mismatch = abs((current[0] / current[1]) - (16.0 / 9.0))
    if mismatch <= ASPECT_16_9_TOL:
        return WindowNormalizeResult(
            True,
            reason="already_16x9",
            before_client=current,
            after_client=current,
            target_client=current,
            dpi=int(dpi),
            **base,
        )

    n = nearest_16x9_n(*current)
    work = monitor_work_area(hwnd)
    if work is not None:
        avail_w = max(0, int(work[2] - work[0]))
        avail_h = max(0, int(work[3] - work[1]))
        settled = False
        while n >= MIN_NEAREST_16X9_N:
            outer = _outer_size_for_client(hwnd, 16 * n, 9 * n, dpi=dpi)
            if outer is None:
                # Frame metrics unavailable: skip clamping and let
                # normalize_game_window report the precise failure.
                settled = True
                break
            if outer[0] <= avail_w and outer[1] <= avail_h:
                settled = True
                break
            n -= 1
        if not settled:
            return WindowNormalizeResult(
                False,
                reason="no_fitting_16x9_size",
                before_client=current,
                after_client=current,
                target_client=(16 * n, 9 * n),
                dpi=int(dpi),
                **base,
            )

    target = (16 * n, 9 * n)
    result = normalize_game_window(
        hwnd,
        pid,
        client_width=target[0],
        client_height=target[1],
        log=log,
    )
    return WindowNormalizeResult(
        ok=result.ok,
        changed=result.changed,
        reason=result.reason,
        hwnd=result.hwnd,
        pid=result.pid,
        before_window=result.before_window,
        before_client=result.before_client,
        after_client=result.after_client,
        target_client=target,
        dpi=result.dpi or int(dpi),
    )


def _primary_work_area() -> tuple[int, int, int, int] | None:
    """Primary monitor work area (l, t, r, b) via SPI_GETWORKAREA.

    Runs under per-monitor DPI context so DPI-virtualized processes still get
    physical pixel metrics.

    @author by ak
    """
    fn = getattr(win_utils.user32, "SystemParametersInfoW", None)
    if fn is None:
        return None
    rc = win_utils.RECT()
    SPI_GETWORKAREA = 0x0030
    prev = win_utils._dpi_ctx_per_monitor()
    try:
        if not fn(SPI_GETWORKAREA, 0, ctypes.byref(rc), 0):
            return None
    except Exception:
        return None
    finally:
        win_utils._dpi_ctx_restore(prev)
    return (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))


def _rewrite_render_size_line(raw: str, key: str, value: int) -> str:
    """Replace the numeric value of one ini key line, keeping its format.

    @author by ak
    """
    pattern = rf"(?s)^(\s*{re.escape(key)}\s*=\s*)(\d+)(.*)$"
    return re.sub(pattern, lambda m: f"{m.group(1)}{value}{m.group(3)}", raw)


def align_game_render_size_to_nearest_16x9(
    game_root: str | Path,
    *,
    log: Callable[[str], None] | None = None,
) -> dict:
    """Align the game's SAVED windowed render size to its nearest 16:9 step.

    Rewrites ``[Video] RenderWid/RenderHei`` in ``userdata/systemsettings.ini``
    (GBK, original formatting preserved) so the client creates its own
    correctly laid-out window at the next launch. External resizing after
    window creation only changes the frame, not the game's render target,
    which scrambles the login UI. Fullscreen mode, already-proportional
    sizes and unreadable files are left untouched. Returns
    ``{"ok", "changed", "reason", "path", "before", "after"}``.

    @author by ak
    """
    log = log or (lambda _message: None)
    out: dict = {
        "ok": False,
        "changed": False,
        "reason": "",
        "path": "",
        "before": None,
        "after": None,
    }
    ini = Path(game_root) / "userdata" / "systemsettings.ini"
    out["path"] = str(ini)
    try:
        # bytes -> decode: no universal-newline translation, so the original
        # CRLF/LF line endings survive the rewrite byte-for-byte.
        text = ini.read_bytes().decode("gbk")
    except Exception as e:
        out["reason"] = f"read_failed: {e}"
        return out

    section = ""
    fullscreen = False
    width = height = 0
    lines = text.splitlines(keepends=True)
    for raw in lines:
        line = raw.strip()
        if line.startswith("["):
            section = line.strip("[]").strip().lower()
            continue
        if section != "video" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "renderwid":
            try:
                width = int(value)
            except ValueError:
                width = 0
        elif key == "renderhei":
            try:
                height = int(value)
            except ValueError:
                height = 0
        elif key == "fullscreen":
            fullscreen = value not in ("0", "", "false")
    if fullscreen:
        out["ok"] = True
        out["reason"] = "fullscreen_mode"
        return out
    if width <= 0 or height <= 0:
        out["reason"] = "render_size_unavailable"
        return out
    out["before"] = (width, height)
    if abs((width / height) - (16.0 / 9.0)) <= ASPECT_16_9_TOL:
        out["ok"] = True
        out["reason"] = "already_16x9"
        out["after"] = (width, height)
        return out

    n = nearest_16x9_n(width, height)
    work = _primary_work_area()
    if work is not None:
        avail_w = max(0, int(work[2] - work[0]) - RENDER_INI_CLAMP_MARGIN[0])
        avail_h = max(0, int(work[3] - work[1]) - RENDER_INI_CLAMP_MARGIN[1])
        while n > MIN_NEAREST_16X9_N and (16 * n > avail_w or 9 * n > avail_h):
            n -= 1
    new_w, new_h = 16 * n, 9 * n

    new_lines = []
    seen_w = seen_h = False
    section = ""
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("["):
            section = stripped.strip("[]").strip().lower()
        if section == "video" and "=" in stripped:
            key = stripped.partition("=")[0].strip().lower()
            if key == "renderwid" and not seen_w:
                raw = _rewrite_render_size_line(raw, "RenderWid", new_w)
                seen_w = True
            elif key == "renderhei" and not seen_h:
                raw = _rewrite_render_size_line(raw, "RenderHei", new_h)
                seen_h = True
        new_lines.append(raw)
    if not (seen_w and seen_h):
        out["reason"] = "render_size_keys_missing"
        return out

    try:
        ini.write_bytes("".join(new_lines).encode("gbk"))
    except Exception as e:
        out["reason"] = f"write_failed: {e}"
        return out
    out["ok"] = True
    out["changed"] = True
    out["reason"] = "aligned"
    out["after"] = (new_w, new_h)
    log(
        f"game render size aligned: {width}x{height} -> {new_w}x{new_h} "
        f"({ini})"
    )
    return out


def prompt_and_normalize_game_window(
    hwnd: int,
    pid: int,
    *,
    client_width: int,
    client_height: int,
    ask: Callable[[str], bool],
    prompt: str,
    log: Callable[[str], None] | None = None,
) -> WindowNormalizeResult:
    """Require a physical client size, asking before changing a live game window."""
    target = (int(client_width), int(client_height))
    dpi = get_window_dpi(hwnd) if hwnd else USER_DEFAULT_SCREEN_DPI
    current = get_client_size(hwnd)
    if current is None:
        return WindowNormalizeResult(
            False,
            reason="client_rect_unavailable",
            hwnd=int(hwnd or 0),
            pid=int(pid or 0),
            target_client=target,
            dpi=int(dpi),
        )
    if client_size_matches(current, target):
        return WindowNormalizeResult(
            True,
            reason="already_target",
            hwnd=int(hwnd or 0),
            pid=int(pid or 0),
            before_client=current,
            after_client=current,
            target_client=target,
            dpi=int(dpi),
        )
    if not bool(ask(prompt)):
        return WindowNormalizeResult(
            False,
            reason="user_declined",
            hwnd=int(hwnd or 0),
            pid=int(pid or 0),
            before_client=current,
            after_client=current,
            target_client=target,
            dpi=int(dpi),
        )
    result = normalize_game_window(
        hwnd,
        pid,
        client_width=target[0],
        client_height=target[1],
        log=log,
    )
    if result.ok and not client_size_matches(result.after_client, target):
        return WindowNormalizeResult(
            False,
            changed=result.changed,
            reason="client_size_mismatch",
            hwnd=result.hwnd,
            pid=result.pid,
            before_window=result.before_window,
            before_client=result.before_client,
            after_client=result.after_client,
            target_client=target,
            dpi=result.dpi or int(dpi),
        )
    return result
