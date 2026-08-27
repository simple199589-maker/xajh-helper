# -*- coding: utf-8 -*-
"""
Window handle utilities: drag-to-pick HWND, resolve PID/title/class/rect.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    tid: int
    title: str
    class_name: str
    rect: tuple[int, int, int, int]
    exe_path: str | None = None

    @property
    def size(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return r - l, b - t


def get_cursor_pos() -> tuple[int, int]:
    pt = POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        raise OSError("GetCursorPos failed")
    return int(pt.x), int(pt.y)


def _dpi_ctx_per_monitor() -> int:
    """
    Enter per-monitor DPI awareness for accurate ScreenToClient vs game hwnd.

    Helper may be DPI-unaware while xajh is per-monitor aware — without this,
    picked client coords drift far from the cursor.
    @author by ak
    """
    # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
    try:
        fn = getattr(user32, "SetThreadDpiAwarenessContext", None)
        if fn is None:
            return 0
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_void_p
        prev = fn(ctypes.c_void_p(-4))
        return int(prev or 0)
    except Exception:
        return 0


def _dpi_ctx_restore(prev: int) -> None:
    """Restore previous DPI awareness context. @author by ak"""
    if not prev:
        return
    try:
        fn = getattr(user32, "SetThreadDpiAwarenessContext", None)
        if fn is None:
            return
        fn(ctypes.c_void_p(prev))
    except Exception:
        pass


def _dpi_ctx_unaware() -> int:
    """
    Enter the DPI-unaware context so GetClientRect returns LOGICAL client units
    (the space a DPI-virtualized window sees), for coordinate diagnostics.
    @author by ak
    """
    # DPI_AWARENESS_CONTEXT_UNAWARE = -1
    try:
        fn = getattr(user32, "SetThreadDpiAwarenessContext", None)
        if fn is None:
            return 0
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_void_p
        prev = fn(ctypes.c_void_p(-1))
        return int(prev or 0)
    except Exception:
        return 0


def screen_to_client(hwnd: int, sx: int, sy: int) -> tuple[int, int] | None:
    """
    Convert screen (sx, sy) to client coordinates of hwnd.

    Uses per-monitor DPI context so coords match a DPI-aware game window.
    @author by ak
    """
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        return None
    prev = _dpi_ctx_per_monitor()
    try:
        pt = POINT(int(sx), int(sy))
        # Prefer physical cursor if available when thread is per-monitor aware
        if not user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(pt)):
            return None
        return int(pt.x), int(pt.y)
    finally:
        _dpi_ctx_restore(prev)


def get_cursor_pos_for_window(hwnd: int = 0) -> tuple[int, int]:
    """
    Cursor position suitable for converting into hwnd client space.

    @author by ak
    """
    prev = _dpi_ctx_per_monitor() if hwnd else 0
    try:
        # Physical cursor when supported (more accurate with DPI games)
        try:
            GetPhysicalCursorPos = getattr(user32, "GetPhysicalCursorPos", None)
            if GetPhysicalCursorPos is not None:
                pt = POINT()
                GetPhysicalCursorPos.argtypes = [ctypes.POINTER(POINT)]
                GetPhysicalCursorPos.restype = wintypes.BOOL
                if GetPhysicalCursorPos(ctypes.byref(pt)):
                    # Map physical -> logical for this hwnd when API exists
                    Logical = getattr(user32, "PhysicalToLogicalPointForPerMonitorDPI", None)
                    if Logical is not None and hwnd:
                        Logical.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
                        Logical.restype = wintypes.BOOL
                        Logical(wintypes.HWND(hwnd), ctypes.byref(pt))
                    return int(pt.x), int(pt.y)
        except Exception:
            pass
        return get_cursor_pos()
    finally:
        _dpi_ctx_restore(prev)


def client_to_screen(hwnd: int, cx: int, cy: int) -> tuple[int, int] | None:
    """
    Convert client (cx, cy) to screen coordinates.

    @author by ak
    """
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        return None
    prev = _dpi_ctx_per_monitor()
    try:
        pt = POINT(int(cx), int(cy))
        if not user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt)):
            return None
        return int(pt.x), int(pt.y)
    finally:
        _dpi_ctx_restore(prev)


def window_from_point(x: int, y: int) -> int:
    pt = POINT(x, y)
    hwnd = user32.WindowFromPoint(pt)
    return int(hwnd or 0)


def get_ancestor_root(hwnd: int) -> int:
    GA_ROOT = 2
    root = user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT)
    return int(root or hwnd)


def get_window_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
    return buf.value


def get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rc = RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rc)):
        return (0, 0, 0, 0)
    return int(rc.left), int(rc.top), int(rc.right), int(rc.bottom)


def get_window_thread_process_id(hwnd: int) -> tuple[int, int]:
    pid = wintypes.DWORD()
    tid = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value), int(tid)


def query_process_image_path(pid: int) -> str | None:
    access = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ
    h = kernel32.OpenProcess(access, False, pid)
    if not h:
        h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        # QueryFullProcessImageNameW
        QueryFullProcessImageNameW = kernel32.QueryFullProcessImageNameW
        QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        QueryFullProcessImageNameW.restype = wintypes.BOOL
        if QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        kernel32.CloseHandle(h)
    return None


def inspect_hwnd(hwnd: int, prefer_root: bool = True) -> WindowInfo | None:
    if not hwnd:
        return None
    target = get_ancestor_root(hwnd) if prefer_root else hwnd
    if not user32.IsWindow(wintypes.HWND(target)):
        return None
    pid, tid = get_window_thread_process_id(target)
    return WindowInfo(
        hwnd=int(target),
        pid=pid,
        tid=tid,
        title=get_window_text(target),
        class_name=get_class_name(target),
        rect=get_window_rect(target),
        exe_path=query_process_image_path(pid),
    )


def inspect_window_at_cursor(prefer_root: bool = True) -> WindowInfo | None:
    x, y = get_cursor_pos()
    hwnd = window_from_point(x, y)
    return inspect_hwnd(hwnd, prefer_root=prefer_root)


def is_same_process_window(hwnd: int, self_pid: int | None = None) -> bool:
    """True if hwnd belongs to this Python/workbench process. @author by ak"""
    if not hwnd:
        return False
    pid, _tid = get_window_thread_process_id(hwnd)
    me = int(self_pid) if self_pid is not None else int(kernel32.GetCurrentProcessId())
    return pid == me


def is_self_window_info(info: WindowInfo | None, self_pid: int | None = None) -> bool:
    """True if WindowInfo is this workbench process. @author by ak"""
    if not info:
        return False
    me = int(self_pid) if self_pid is not None else int(kernel32.GetCurrentProcessId())
    if int(info.pid) == me:
        return True
    # path fallback (rename/launcher edge cases)
    if info.exe_path:
        name = info.exe_path.replace("/", "\\").rsplit("\\", 1)[-1].lower()
        if name in ("python.exe", "pythonw.exe"):
            # only treat as self when PID matches; other python apps are allowed
            return int(info.pid) == me
    return False


def inspect_window_at_cursor_skip_self(
    prefer_root: bool = True,
    self_pid: int | None = None,
) -> WindowInfo | None:
    """
    Resolve window under cursor, but never return this process's own windows.
    Returns None when cursor is over self (caller should keep last valid target).
    @author by ak
    """
    info = inspect_window_at_cursor(prefer_root=prefer_root)
    if is_self_window_info(info, self_pid=self_pid):
        return None
    return info
