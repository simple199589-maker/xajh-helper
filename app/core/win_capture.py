# -*- coding: utf-8 -*-
"""
Window client capture + mouse click helpers (Win32).

Used by captcha solve flow for 九层妖楼 dialog screenshots/clicks.

@author by ak
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020
PW_RENDERFULLCONTENT = 0x00000002
BI_RGB = 0
DIB_RGB_COLORS = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_VIRTUALDESK = 0x4000
SW_RESTORE = 9

LogFn = Callable[[str], None]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


@dataclass
class CaptureResult:
    """One client-area screenshot. @author by ak"""

    ok: bool
    png: bytes | None = None
    width: int = 0
    height: int = 0
    client_origin: tuple[int, int] = (0, 0)
    image: object | None = None  # optional PIL.Image to avoid re-decode
    error: str | None = None


def get_client_rect_screen(hwnd: int) -> tuple[int, int, int, int] | None:
    """
    Return client rect in screen coords: (left, top, right, bottom).

    Uses per-monitor DPI awareness so sizes match a DPI-aware game window
    (needed for captcha mem_rect scale vs CaptureScreen / BitBlt).

    @author by ak
    """
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        return None
    from app.core import win_utils

    prev = win_utils._dpi_ctx_per_monitor()
    try:
        rc = RECT()
        if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rc)):
            return None
        pt = POINT(0, 0)
        if not user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt)):
            return None
        left = int(pt.x)
        top = int(pt.y)
        right = left + int(rc.right - rc.left)
        bottom = top + int(rc.bottom - rc.top)
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom
    finally:
        win_utils._dpi_ctx_restore(prev)


def capture_client_png(hwnd: int, *, log: LogFn | None = None) -> CaptureResult:
    """
    Capture hwnd client area as PNG bytes (PrintWindow fallback BitBlt).

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        from io import BytesIO

        from PIL import Image
    except Exception as e:
        return CaptureResult(ok=False, error=f"Pillow required: {e}")

    box = get_client_rect_screen(hwnd)
    if box is None:
        return CaptureResult(ok=False, error="invalid hwnd / client rect")
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    if width < 8 or height < 8:
        return CaptureResult(ok=False, error=f"client too small {width}x{height}")

    hdc_win = user32.GetDC(wintypes.HWND(hwnd))
    if not hdc_win:
        return CaptureResult(ok=False, error="GetDC failed")
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, width, height)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    try:
        ok = bool(
            user32.PrintWindow(
                wintypes.HWND(hwnd),
                hdc_mem,
                PW_RENDERFULLCONTENT,
            )
        )
        if not ok:
            # fallback BitBlt from window DC
            ok = bool(
                gdi32.BitBlt(
                    hdc_mem,
                    0,
                    0,
                    width,
                    height,
                    hdc_win,
                    0,
                    0,
                    SRCCOPY,
                )
            )
        if not ok:
            return CaptureResult(ok=False, error="PrintWindow/BitBlt failed")

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        buf_len = width * height * 4
        buf = (ctypes.c_ubyte * buf_len)()
        got = gdi32.GetDIBits(
            hdc_mem,
            hbmp,
            0,
            height,
            ctypes.byref(buf),
            ctypes.byref(bmi),
            DIB_RGB_COLORS,
        )
        if not got:
            return CaptureResult(ok=False, error="GetDIBits failed")

        # BGRA -> RGB
        raw = bytes(buf)
        img = Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", 0, 1).convert(
            "RGB"
        )
        bio = BytesIO()
        img.save(bio, format="PNG")
        png = bio.getvalue()
        log(f"win_capture ok {width}x{height} png={len(png)}")
        return CaptureResult(
            ok=True,
            png=png,
            width=width,
            height=height,
            client_origin=(left, top),
            image=img,
        )
    except Exception as e:
        return CaptureResult(ok=False, error=str(e))
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(wintypes.HWND(hwnd), hdc_win)


def kernel32_GetCurrentThreadId() -> int:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return int(k32.GetCurrentThreadId())


def is_foreground_hwnd(hwnd: int) -> bool:
    """True if hwnd is the current foreground top-level window. @author by ak"""
    try:
        if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
            return False
        if user32.IsIconic(wintypes.HWND(hwnd)):
            return False
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)
    except Exception:
        return False


def bring_to_foreground(hwnd: int, *, log: LogFn | None = None) -> bool:
    """
    Best-effort restore + foreground switch.

    Uses AttachThreadInput + optional Alt key unlock so helper can steal focus
    when captcha needs real-mouse input.
    @author by ak
    """
    log = log or (lambda _m: None)
    if not hwnd:
        return False
    try:
        h = wintypes.HWND(hwnd)
        if user32.IsIconic(h):
            user32.ShowWindow(h, SW_RESTORE)
        else:
            # Ensure visible even if not minimized.
            user32.ShowWindow(h, 5)  # SW_SHOW

        # Allow this process to set foreground when OS blocks it.
        try:
            user32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY
        except Exception:
            pass

        fg = user32.GetForegroundWindow()
        cur_tid = kernel32_GetCurrentThreadId()
        fg_tid = wintypes.DWORD(0)
        if fg:
            user32.GetWindowThreadProcessId(wintypes.HWND(fg), ctypes.byref(fg_tid))
        tgt_tid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(h, ctypes.byref(tgt_tid))

        attached_fg = False
        attached_tgt = False
        if fg_tid.value and fg_tid.value != cur_tid:
            attached_fg = bool(
                user32.AttachThreadInput(cur_tid, int(fg_tid.value), True)
            )
        if tgt_tid.value and tgt_tid.value != cur_tid and tgt_tid.value != fg_tid.value:
            attached_tgt = bool(
                user32.AttachThreadInput(cur_tid, int(tgt_tid.value), True)
            )

        # Alt press/release often unlocks SetForegroundWindow restrictions.
        try:
            VK_MENU = 0x12
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        except Exception:
            pass

        user32.BringWindowToTop(h)
        user32.SetForegroundWindow(h)
        try:
            user32.SetActiveWindow(h)
        except Exception:
            pass
        try:
            user32.SetFocus(h)
        except Exception:
            pass

        if attached_tgt:
            user32.AttachThreadInput(cur_tid, int(tgt_tid.value), False)
        if attached_fg:
            user32.AttachThreadInput(cur_tid, int(fg_tid.value), False)
        return is_foreground_hwnd(int(hwnd))
    except Exception as e:
        log(f"bring_to_foreground err: {e}")
        return False


def ensure_foreground(
    hwnd: int,
    *,
    retries: int = 4,
    settle_s: float = 0.08,
    force: bool = False,
    log: LogFn | None = None,
) -> bool:
    """
    Ensure game hwnd is foreground before real-mouse work.

    Retries a few times; logs success/failure for captcha audit.
    @author by ak
    """
    log = log or (lambda _m: None)
    if not hwnd:
        return False
    if not force and is_foreground_hwnd(int(hwnd)):
        return True
    ok = False
    n = max(1, int(retries))
    for i in range(n):
        ok = bring_to_foreground(int(hwnd), log=log)
        time.sleep(max(0.02, float(settle_s)))
        if is_foreground_hwnd(int(hwnd)):
            if i > 0:
                log(f"ensure_foreground ok hwnd=0x{int(hwnd):X} try={i + 1}/{n}")
            return True
    log(
        f"ensure_foreground miss hwnd=0x{int(hwnd):X} "
        f"fg=0x{int(user32.GetForegroundWindow() or 0):X} tries={n}"
    )
    return bool(ok)


def click_screen(
    x: int,
    y: int,
    *,
    hwnd: int = 0,
    settle_s: float = 0.05,
    log: LogFn | None = None,
) -> bool:
    """
    Left-click screen coordinate via SendInput absolute move.

    Optionally focuses hwnd first.
    @author by ak
    """
    log = log or (lambda _m: None)
    if hwnd:
        bring_to_foreground(hwnd, log=log)
        time.sleep(0.05)
    try:
        # virtual screen metrics for absolute coords
        vx = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
        vy = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
        vw = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
        vh = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
        if vw <= 0 or vh <= 0:
            vw = int(user32.GetSystemMetrics(0))
            vh = int(user32.GetSystemMetrics(1))
            vx, vy = 0, 0
        ax = int((int(x) - vx) * 65535 / max(vw - 1, 1))
        ay = int((int(y) - vy) * 65535 / max(vh - 1, 1))
        if not _set_physical_cursor_pos(int(x), int(y)):
            log(f"click_screen SetCursorPos failed ({x},{y})")
            return False
        time.sleep(max(0.0, float(settle_s)))
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        log(f"click_screen ({x},{y}) abs=({ax},{ay})")
        return True
    except Exception as e:
        log(f"click_screen err: {e}")
        return False


def click_client(
    hwnd: int,
    cx: int,
    cy: int,
    *,
    log: LogFn | None = None,
) -> bool:
    """
    Click client-relative (cx,cy) by converting to screen coords.

    @author by ak
    """
    log = log or (lambda _m: None)
    box = get_client_rect_screen(hwnd)
    if box is None:
        log("click_client: bad hwnd")
        return False
    left, top, right, bottom = box
    sx = left + int(cx)
    sy = top + int(cy)
    if sx < left or sy < top or sx >= right or sy >= bottom:
        log(f"click_client out of client ({cx},{cy}) -> ({sx},{sy})")
    return click_screen(sx, sy, hwnd=hwnd, log=log)


def _lparam_client(cx: int, cy: int) -> int:
    x = int(cx) & 0xFFFF
    y = int(cy) & 0xFFFF
    return (y << 16) | x


def post_mousemove_client(
    hwnd: int,
    cx: int,
    cy: int,
    *,
    log: LogFn | None = None,
) -> bool:
    """Single background WM_MOUSEMOVE at client (cx,cy). @author by ak"""
    log = log or (lambda _m: None)
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        return False
    WM_MOUSEMOVE = 0x0200
    try:
        user32.PostMessageW(
            wintypes.HWND(hwnd), WM_MOUSEMOVE, 0, _lparam_client(cx, cy)
        )
        return True
    except Exception as e:
        log(f"post_mousemove_client err: {e}")
        return False


def post_path_client(
    hwnd: int,
    points,
    *,
    duration_s: float = 0.15,
    stop_event=None,
    log: LogFn | None = None,
) -> bool:
    """
    Post a sequence of WM_MOUSEMOVE along points (background-safe slide trail).

    duration_s is total move time spread across segments.
    @author by ak
    """
    log = log or (lambda _m: None)
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        log("post_path_client: bad hwnd")
        return False
    pts = [(int(p[0]), int(p[1])) for p in (points or []) if p is not None]
    if not pts:
        return True
    n = len(pts)
    total = max(0.02, float(duration_s))
    weights = [0.65 + 0.7 * ((i + 1) / float(n)) for i in range(n)]
    wsum = sum(weights) or 1.0
    try:
        for i, (px, py) in enumerate(pts):
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                return False
            if not post_mousemove_client(hwnd, px, py, log=log):
                return False
            seg = total * (weights[i] / wsum)
            time.sleep(max(0.004, min(0.08, seg)))
        return True
    except Exception as e:
        log(f"post_path_client err: {e}")
        return False


def post_click_client(
    hwnd: int,
    cx: int,
    cy: int,
    *,
    right: bool = False,
    log: LogFn | None = None,
    move_first: bool = True,
    down_hold_s: float = 0.04,
    path_points=None,
    path_duration_s: float = 0.12,
    stop_event=None,
    settle_after: bool = True,
) -> bool:
    """
    Background-friendly client click via PostMessage (no cursor / no focus).

    Sequence: optional slide path / single move -> button DOWN -> UP -> settle move.
    @author by ak
    """
    log = log or (lambda _m: None)
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        log("post_click_client: bad hwnd")
        return False
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONDOWN = 0x0204
    WM_RBUTTONUP = 0x0205
    MK_LBUTTON = 0x0001
    MK_RBUTTON = 0x0002
    msg_down = WM_RBUTTONDOWN if right else WM_LBUTTONDOWN
    msg_up = WM_RBUTTONUP if right else WM_LBUTTONUP
    mk_button = MK_RBUTTON if right else MK_LBUTTON
    lp = _lparam_client(cx, cy)
    hold = max(0.015, float(down_hold_s))
    try:
        if path_points:
            post_path_client(
                hwnd,
                list(path_points),
                duration_s=float(path_duration_s),
                stop_event=stop_event,
                log=log,
            )
        elif move_first:
            post_mousemove_client(hwnd, cx, cy, log=log)
            time.sleep(max(0.01, min(0.05, hold * 0.30)))
        if not user32.PostMessageW(wintypes.HWND(hwnd), msg_down, mk_button, lp):
            log(f"post_click_client: button down failed right={bool(right)}")
            return False
        time.sleep(hold)
        if not user32.PostMessageW(wintypes.HWND(hwnd), msg_up, 0, lp):
            log(f"post_click_client: button up failed right={bool(right)}")
            return False
        if settle_after:
            time.sleep(max(0.008, min(0.04, hold * 0.18)))
            post_mousemove_client(hwnd, cx, cy, log=log)
        npath = len(path_points or [])
        log(
            f"post_click_client ({cx},{cy}) button={'R' if right else 'L'} "
            f"hold={hold:.3f}s "
            f"path_pts={npath} path_s={float(path_duration_s):.3f}"
        )
        return True
    except Exception as e:
        log(f"post_click_client err: {e}")
        return False

def send_click_client(
    hwnd: int,
    cx: int,
    cy: int,
    *,
    log: LogFn | None = None,
) -> bool:
    """
    Synchronous client click via SendMessage (still no cursor move).

    Prefer post_click first; SendMessage can help when PostMessage is dropped.
    Does not restore/foreground the window.
    @author by ak
    """
    log = log or (lambda _m: None)
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        log("send_click_client: bad hwnd")
        return False
    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    MK_LBUTTON = 0x0001
    x = int(cx) & 0xFFFF
    y = int(cy) & 0xFFFF
    lp = (y << 16) | x
    try:
        user32.SendMessageW(wintypes.HWND(hwnd), WM_MOUSEMOVE, 0, lp)
        user32.SendMessageW(wintypes.HWND(hwnd), WM_LBUTTONDOWN, MK_LBUTTON, lp)
        time.sleep(0.02)
        user32.SendMessageW(wintypes.HWND(hwnd), WM_LBUTTONUP, 0, lp)
        log(f"send_click_client ({cx},{cy})")
        return True
    except Exception as e:
        log(f"send_click_client err: {e}")
        return False



def client_to_screen(hwnd: int, cx: int, cy: int) -> tuple[int, int] | None:
    """Convert one live client point directly to physical screen pixels."""
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        return None
    from app.core import win_utils

    prev = win_utils._dpi_ctx_per_monitor()
    try:
        rc = RECT()
        if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rc)):
            return None
        x, y = int(cx), int(cy)
        if x < 0 or y < 0 or x >= int(rc.right) or y >= int(rc.bottom):
            return None
        pt = POINT(x, y)
        if not user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt)):
            return None
        return int(pt.x), int(pt.y)
    finally:
        win_utils._dpi_ctx_restore(prev)


def _get_physical_cursor_pos() -> tuple[int, int] | None:
    """Read the OS cursor in physical virtual-screen pixels."""
    from app.core import win_utils

    prev = win_utils._dpi_ctx_per_monitor()
    try:
        getter = getattr(user32, "GetPhysicalCursorPos", None)
        if getter is None:
            getter = user32.GetCursorPos
        pt = POINT()
        if not getter(ctypes.byref(pt)):
            return None
        return int(pt.x), int(pt.y)
    except Exception:
        return None
    finally:
        win_utils._dpi_ctx_restore(prev)


def _set_physical_cursor_pos(x: int, y: int) -> bool:
    """Move to physical screen pixels using normalized virtual-desktop input."""
    from app.core import win_utils

    px, py = int(x), int(y)
    prev = win_utils._dpi_ctx_per_monitor()
    try:
        vx = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
        vy = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
        vw = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
        vh = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
        if vw <= 1 or vh <= 1:
            vx, vy = 0, 0
            vw = int(user32.GetSystemMetrics(0))
            vh = int(user32.GetSystemMetrics(1))
        if vw <= 1 or vh <= 1:
            return False
        if px < vx or py < vy or px >= vx + vw or py >= vy + vh:
            return False
        ax = int(round((px - vx) * 65535.0 / float(vw - 1)))
        ay = int(round((py - vy) * 65535.0 / float(vh - 1)))
        flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        user32.mouse_event(flags, ax, ay, 0, 0)
    finally:
        win_utils._dpi_ctx_restore(prev)

    # Reject a virtualized/misrouted move instead of clicking the wrong place.
    actual = _get_physical_cursor_pos()
    if actual is None:
        return True
    return abs(actual[0] - px) <= 2 and abs(actual[1] - py) <= 2


def move_cursor_screen_path(
    points,
    *,
    duration_s: float = 0.15,
    stop_event=None,
    log: LogFn | None = None,
) -> bool:
    """Move real cursor along screen points. @author by ak"""
    log = log or (lambda _m: None)
    pts = [(int(p[0]), int(p[1])) for p in (points or []) if p is not None]
    if not pts:
        return True
    n = len(pts)
    total = max(0.02, float(duration_s))
    weights = [0.65 + 0.7 * ((i + 1) / float(n)) for i in range(n)]
    wsum = sum(weights) or 1.0
    try:
        for i, (sx, sy) in enumerate(pts):
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                return False
            if not _set_physical_cursor_pos(sx, sy):
                log(f"move_cursor_screen_path SetCursorPos failed ({sx},{sy})")
                return False
            time.sleep(max(0.004, min(0.08, total * (weights[i] / wsum))))
        return True
    except Exception as e:
        log(f"move_cursor_screen_path err: {e}")
        return False


def click_client_real_mouse(
    hwnd: int,
    cx: int,
    cy: int,
    *,
    down_hold_s: float = 0.09,
    path_points_client=None,
    path_duration_s: float = 0.15,
    focus: bool = True,
    stop_event=None,
    log: LogFn | None = None,
) -> bool:
    """
    Real OS mouse click at client (cx,cy): optional slide + SetCursorPos + mouse_event.

    Used to reduce "remote / inject click" signatures. Requires game window usable.
    @author by ak
    """
    log = log or (lambda _m: None)
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        log("click_client_real_mouse: bad hwnd")
        return False
    hold = max(0.02, float(down_hold_s))
    try:
        if focus:
            ensure_foreground(
                int(hwnd),
                retries=3,
                settle_s=0.06,
                force=True,
                log=log,
            )
        # Build screen path
        screen_path = []
        for p in (path_points_client or []):
            sc = client_to_screen(hwnd, int(p[0]), int(p[1]))
            if sc is not None:
                screen_path.append(sc)
        target = client_to_screen(hwnd, int(cx), int(cy))
        if target is None:
            log("click_client_real_mouse: client_to_screen fail")
            return False
        if screen_path:
            if screen_path[-1] != target:
                screen_path.append(target)
            move_cursor_screen_path(
                screen_path,
                duration_s=float(path_duration_s),
                stop_event=stop_event,
                log=log,
            )
        else:
            if not _set_physical_cursor_pos(int(target[0]), int(target[1])):
                log(
                    "click_client_real_mouse: SetCursorPos failed "
                    f"screen={target}"
                )
                return False
            time.sleep(max(0.02, min(0.06, hold * 0.35)))
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(hold)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        actual = _get_physical_cursor_pos()
        log(
            f"click_client_real_mouse client=({cx},{cy}) screen={target} "
            f"actual_screen={actual} hold={hold:.3f}s path_pts={len(screen_path)}"
        )
        return True
    except Exception as e:
        log(f"click_client_real_mouse err: {e}")
        return False


def click_client_smart(
    hwnd: int,
    cx: int,
    cy: int,
    *,
    prefer_post: bool = True,
    allow_cursor: bool = False,
    down_hold_s: float | None = None,
    path_points=None,
    path_duration_s: float = 0.12,
    stop_event=None,
    log: LogFn | None = None,
) -> bool:
    """
    Background-first click: PostMessage -> SendMessage -> optional cursor.

    path_points: optional slide trail before the click.
    @author by ak
    """
    log = log or (lambda _m: None)
    hold = 0.04 if down_hold_s is None else max(0.015, float(down_hold_s))
    if prefer_post:
        if post_click_client(
            hwnd,
            cx,
            cy,
            down_hold_s=hold,
            path_points=path_points,
            path_duration_s=path_duration_s,
            stop_event=stop_event,
            log=log,
        ):
            return True
        if send_click_client(hwnd, cx, cy, log=log):
            return True
    if allow_cursor:
        if click_client_real_mouse(
            hwnd,
            cx,
            cy,
            down_hold_s=hold,
            path_points_client=path_points,
            path_duration_s=path_duration_s,
            stop_event=stop_event,
            log=log,
        ):
            return True
        return click_client(hwnd, cx, cy, log=log)
    log(f"click_client_smart: background-only miss ({cx},{cy})")
    return False
