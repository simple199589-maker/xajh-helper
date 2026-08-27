# -*- coding: utf-8 -*-
"""
System-level keyboard / mouse injection (Win32).

Mirrors native bridge UI_KEY strategy for process-external use:
  1) SendInput scan-code (DirectInput-friendly)
  2) SendInput VK (GetAsyncKeyState path)
  3) keybd_event fallback
  4) optional PostMessage WM_KEY* to target hwnd

This is the phase-1 "foreground / system" path. Injected CMD_UI_KEY remains
the phase-2 background path.

@author by ak
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)

LogFn = Callable[[str], None]

# ---- VK helpers ----
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_RBUTTON = 0x02
VK_Q = 0x51
VK_X = 0x58
VK_E = 0x45
VK_C = 0x43
VK_V = 0x56
VK_F = 0x46

# ---- SendInput ----
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MAPVK_VK_TO_VSC = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


@dataclass
class SysKeyResult:
    ok: bool
    sent: int = 0
    note: str = ""
    error: str = ""


def normalize_vk(vk: int) -> int:
    """Map generic modifiers to left variants (stable scan codes)."""
    v = int(vk or 0) & 0xFF
    if v == VK_SHIFT:
        return VK_LSHIFT
    if v == VK_CONTROL:
        return VK_LCONTROL
    if v == VK_MENU:
        return VK_LMENU
    return v


def is_extended_vk(vk: int) -> bool:
    v = int(vk) & 0xFF
    if v in (
        VK_RCONTROL,
        VK_RMENU,
        0x2D,  # INSERT
        0x2E,  # DELETE
        0x21,  # PRIOR
        0x22,  # NEXT
        0x23,  # END
        0x24,  # HOME
    ):
        return True
    return 0x25 <= v <= 0x28  # arrows


def map_scan(vk: int) -> int:
    v = normalize_vk(vk)
    return int(user32.MapVirtualKeyW(v, MAPVK_VK_TO_VSC) or 0) & 0xFF


def _lp_key(sc: int, *, key_up: bool) -> int:
    lp = 1 | ((sc & 0xFF) << 16)
    if key_up:
        lp |= (1 << 30) | (1 << 31)
    return lp


def send_key_event(
    vk: int,
    *,
    key_up: bool = False,
    hwnd: int = 0,
    post_message: bool = False,
    use_keybd_event: bool = False,
) -> SysKeyResult:
    """
    System-level one-shot key down or up.

    Default path matches Logitech G HUB style: one SendInput pair
    (scan-code + VK). Optional keybd_event / PostMessage are off by default
    to avoid double-trigger and sticky keys.
    """
    v = normalize_vk(vk)
    if not v or v == VK_RBUTTON:
        return SysKeyResult(ok=False, error="invalid vk")
    sc = map_scan(v)
    ext = KEYEVENTF_EXTENDEDKEY if is_extended_vk(v) else 0
    up = KEYEVENTF_KEYUP if key_up else 0

    ins = (INPUT * 2)()
    # 0: scan-code only (DirectInput-friendly)
    ins[0].type = INPUT_KEYBOARD
    ins[0].u.ki = KEYBDINPUT(0, sc, KEYEVENTF_SCANCODE | up | ext, 0, 0)
    # 1: VK + scan (GetAsyncKeyState / Win32)
    ins[1].type = INPUT_KEYBOARD
    ins[1].u.ki = KEYBDINPUT(v, sc, up | ext, 0, 0)
    n = int(user32.SendInput(2, ctypes.byref(ins), ctypes.sizeof(INPUT)))

    if use_keybd_event:
        ke = KEYEVENTF_KEYUP if key_up else 0
        if is_extended_vk(v):
            ke |= KEYEVENTF_EXTENDEDKEY
        user32.keybd_event(ctypes.c_byte(v), ctypes.c_byte(sc), ke, 0)
        # Mirror generic Shift for binds that poll VK_SHIFT.
        if v in (VK_LSHIFT, VK_RSHIFT):
            user32.keybd_event(
                ctypes.c_byte(VK_SHIFT), ctypes.c_byte(sc), ke, 0
            )

    if post_message and hwnd:
        try:
            if user32.IsWindow(wintypes.HWND(hwnd)):
                lp = _lp_key(sc, key_up=key_up)
                msg = WM_KEYUP if key_up else WM_KEYDOWN
                user32.PostMessageW(wintypes.HWND(hwnd), msg, v, lp)
                if v in (VK_LSHIFT, VK_RSHIFT):
                    user32.PostMessageW(
                        wintypes.HWND(hwnd), msg, VK_SHIFT, lp
                    )
        except Exception:
            pass

    if n <= 0:
        return SysKeyResult(
            ok=False,
            sent=0,
            error="SendInput=0",
            note="SendInput failed",
        )
    return SysKeyResult(ok=True, sent=n, note="sys key ok")


def send_key_press(
    vk: int,
    *,
    hold_ms: int = 30,
    hwnd: int = 0,
    post_message: bool = False,
    use_keybd_event: bool = False,
    stop_event: threading.Event | None = None,
) -> SysKeyResult:
    """System-level press: down → short hold → up (G HUB-like)."""
    down = send_key_event(
        vk,
        key_up=False,
        hwnd=hwnd,
        post_message=post_message,
        use_keybd_event=use_keybd_event,
    )
    if not down.ok:
        return down
    # Floor 12ms so short holds still register; avoid sticky long presses.
    wait = max(0.012, max(0, int(hold_ms)) / 1000.0)
    if stop_event is not None:
        if stop_event.wait(wait):
            send_key_event(
                vk,
                key_up=True,
                hwnd=hwnd,
                post_message=post_message,
                use_keybd_event=use_keybd_event,
            )
            return SysKeyResult(ok=False, error="stopped", note="stopped")
    else:
        time.sleep(wait)
    up = send_key_event(
        vk,
        key_up=True,
        hwnd=hwnd,
        post_message=post_message,
        use_keybd_event=use_keybd_event,
    )
    if not up.ok:
        return up
    return SysKeyResult(
        ok=True,
        sent=int(down.sent) + int(up.sent),
        note="sys press ok",
    )


def send_mouse_button(
    *,
    right: bool = False,
    key_up: bool = False,
    hwnd: int = 0,
    client_x: int = 0,
    client_y: int = 0,
    post_message: bool = True,
) -> SysKeyResult:
    """
    System-level mouse button down/up at current cursor (or PostMessage at client).
    """
    if post_message and hwnd and client_x >= 0 and client_y >= 0:
        try:
            if user32.IsWindow(wintypes.HWND(hwnd)):
                lp = (int(client_y) << 16) | (int(client_x) & 0xFFFF)
                if key_up:
                    msg = WM_RBUTTONUP if right else WM_LBUTTONUP
                    mk = 0
                else:
                    msg = WM_RBUTTONDOWN if right else WM_LBUTTONDOWN
                    mk = MK_RBUTTON if right else MK_LBUTTON
                user32.PostMessageW(
                    wintypes.HWND(hwnd), WM_MOUSEMOVE, 0, lp
                )
                user32.PostMessageW(
                    wintypes.HWND(hwnd), msg, mk, lp
                )
        except Exception:
            pass

    flags = 0
    if right:
        flags = MOUSEEVENTF_RIGHTUP if key_up else MOUSEEVENTF_RIGHTDOWN
    else:
        flags = MOUSEEVENTF_LEFTUP if key_up else MOUSEEVENTF_LEFTDOWN
    ins = INPUT()
    ins.type = INPUT_MOUSE
    ins.u.mi = MOUSEINPUT(0, 0, 0, flags, 0, 0)
    n = int(user32.SendInput(1, ctypes.byref(ins), ctypes.sizeof(INPUT)))
    if n <= 0:
        return SysKeyResult(ok=False, sent=0, error="mouse SendInput=0")
    return SysKeyResult(ok=True, sent=n, note="sys mouse ok")


def send_mouse_click(
    *,
    right: bool = False,
    hold_ms: int = 40,
    hwnd: int = 0,
    client_x: int = 0,
    client_y: int = 0,
    stop_event: threading.Event | None = None,
) -> SysKeyResult:
    """System-level mouse click (down + hold + up)."""
    down = send_mouse_button(
        right=right,
        key_up=False,
        hwnd=hwnd,
        client_x=client_x,
        client_y=client_y,
    )
    if not down.ok:
        return down
    wait = max(0, int(hold_ms)) / 1000.0
    if wait > 0:
        if stop_event is not None:
            if stop_event.wait(wait):
                send_mouse_button(
                    right=right,
                    key_up=True,
                    hwnd=hwnd,
                    client_x=client_x,
                    client_y=client_y,
                )
                return SysKeyResult(ok=False, error="stopped")
        else:
            time.sleep(wait)
    up = send_mouse_button(
        right=right,
        key_up=True,
        hwnd=hwnd,
        client_x=client_x,
        client_y=client_y,
    )
    if not up.ok:
        return up
    return SysKeyResult(ok=True, sent=int(down.sent) + int(up.sent), note="sys click ok")


def focus_window(hwnd: int, *, log: LogFn | None = None) -> bool:
    """Best-effort bring hwnd to foreground (AttachThreadInput trick)."""
    if not hwnd:
        return False
    try:
        from app.core.win_capture import bring_to_foreground

        return bool(bring_to_foreground(int(hwnd), log=log))
    except Exception as e:
        if log:
            log(f"sys_input focus err: {e}")
        return False
