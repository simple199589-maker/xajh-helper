# -*- coding: utf-8 -*-
"""
Foreground-window helpers for game-scoped hotkeys.

@author by ak
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

VK_DELETE = 0x2E
GA_ROOT = 2

# Process-name cache: pid -> (name_lower, expire_monotonic)
_PID_NAME_CACHE: dict[int, tuple[str, float]] = {}
_PID_NAME_TTL_S = 5.0


def get_foreground_hwnd() -> int:
    """Return current foreground window handle. @author by ak"""
    return int(user32.GetForegroundWindow() or 0)


def get_window_pid(hwnd: int) -> int:
    """PID that owns hwnd. @author by ak"""
    if not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value or 0)


def _process_name(pid: int) -> str:
    """
    Lowercase process image name for pid (cached briefly).

    @author by ak
    """
    if not pid:
        return ""
    now = time.monotonic()
    hit = _PID_NAME_CACHE.get(pid)
    if hit is not None and hit[1] > now:
        return hit[0]
    name = ""
    try:
        import psutil

        name = (psutil.Process(pid).name() or "").lower()
    except Exception:
        name = ""
    _PID_NAME_CACHE[pid] = (name, now + _PID_NAME_TTL_S)
    return name


def is_xajh_process(pid: int) -> bool:
    """True if pid is xajh.exe. @author by ak"""
    return _process_name(pid) == "xajh.exe"


def is_xajh_foreground() -> tuple[bool, int, int]:
    """
    True if foreground top-level window belongs to xajh.exe.

    Returns (ok, pid, hwnd).
    @author by ak
    """
    hwnd = get_foreground_hwnd()
    if not hwnd:
        return False, 0, 0
    root = int(user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT) or hwnd)
    if not root:
        root = hwnd
    pid = get_window_pid(root)
    if not pid:
        pid = get_window_pid(hwnd)
    if not pid:
        return False, 0, root
    if not is_xajh_process(pid):
        return False, pid, root
    return True, pid, root


class DeleteKeyWatcher:
    """
    Level-triggered Delete press detector (down edge).

    More reliable than GetAsyncKeyState LSB (0x0001), which is often
    cleared by the game / missed between 80ms polls.

    @author by ak
    """

    def __init__(self) -> None:
        self._was_down = False
        self._cooldown_until = 0.0

    def reset(self) -> None:
        """Clear edge state. @author by ak"""
        self._was_down = False

    def arm_cooldown(self, seconds: float = 0.45) -> None:
        """
        Ignore further edges briefly after a successful handle.

        Prevents double-open while focus is switching.
        @author by ak
        """
        self._cooldown_until = time.monotonic() + max(0.0, float(seconds))

    def poll_press(self) -> bool:
        """
        True once when Delete transitions released -> pressed.

        @author by ak
        """
        state = int(user32.GetAsyncKeyState(VK_DELETE) or 0)
        down = (state & 0x8000) != 0
        pressed = down and not self._was_down
        self._was_down = down
        if not pressed:
            return False
        if time.monotonic() < self._cooldown_until:
            return False
        return True


# Module-level watcher for simple callers
_DEFAULT_WATCHER = DeleteKeyWatcher()


def delete_key_edge() -> bool:
    """
    True once per Delete key press (level edge via module watcher).

    Prefer DeleteKeyWatcher in long-lived UI.
    @author by ak
    """
    return _DEFAULT_WATCHER.poll_press()
