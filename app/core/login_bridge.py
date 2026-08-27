# -*- coding: utf-8 -*-
"""
Standalone login-stage bridge client for xajh.exe — drive the account/password
UI in the background using only the injected login bridge DLL (no foreground
switch, no production bridge).

Native side: native\\xajh_login_bridge\\dllmain.cpp
Protocol header: native\\xajh_login_bridge\\login_bridge_protocol.h (v4)

Commands:
  LCMD_PING=1                health check
  LCMD_UI_CLICK=2            background mouse press on the game hwnd
  LCMD_UI_KEY=3              key inject via SendInput + PostMessage
  LCMD_UI_DIALOG_COMMAND=4   AUIDialog native command (IDYES etc, diagnostic)
  LCMD_KEY_HOLD=5            process-local IAT GetAsyncKeyState/GetKeyState force
  LCMD_UI_INPUT=6            same-process call of login UI input processor
                             (preferred VA 0x00E90CC0), thiscall
                             (self, msg, wParam, lParam, extra=1)

Shared memory: Global\\XajhLoginBridgeV2_<pid> / Local\\XajhLoginBridgeV2_<pid>
pack(1) LoginBridgeShared, 184 bytes. ack_seq is written by the native UI
timer after it consumes a PENDING command; the client waits until ack_seq
matches the submitted seq.

@author by ak
"""
from __future__ import annotations

import ctypes
import struct
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

_ctypes = ctypes
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32_GetTickCount = ctypes.WinDLL("kernel32", use_last_error=True).GetTickCount

LOGIN_BRIDGE_MAGIC = 0x4C584A32  # 'LXJ2'
LOGIN_BRIDGE_PROTOCOL_VERSION = 4
LOGIN_BRIDGE_BUILD_ID = 2026081301

# Command ids (native enum LoginBridgeCmdId)
LCMD_IDLE = 0
LCMD_PING = 1
LCMD_UI_CLICK = 2
LCMD_UI_KEY = 3
LCMD_UI_DIALOG_COMMAND = 4
LCMD_KEY_HOLD = 5
LCMD_UI_INPUT = 6
LCMD_IME_OFF = 7
LCMD_INJECT_CHAR = 8
LCMD_UNICODE_INPUT = 9

# Status (enum LoginBridgeStatus)
LST_IDLE = 0
LST_PENDING = 1
LST_OK = 2
LST_ERR = 3

# Offsets into LoginBridgeShared (pack(1), size 184)
_OFF_MAGIC = 0
_OFF_SEQ = 4
_OFF_CMD = 8
_OFF_STATUS = 12
_OFF_RET = 16
_OFF_MODULE_BASE = 20
_OFF_HWND = 24
_OFF_MODE = 28
_OFF_ID_LO = 32
_OFF_ID_HI = 36
_OFF_TID = 40
_OFF_ERR = 44
_OFF_PROTOCOL_VERSION = 172
_OFF_STRUCT_SIZE = 176
_OFF_ACK_SEQ = 180
SHARED_SIZE = 184

# UI_KEY actions
UI_KEY_DOWN = 0
UI_KEY_UP = 1
UI_KEY_PRESS = 2
# UI_CLICK buttons
UI_CLICK_LEFT = 0
UI_CLICK_RIGHT = 1
LOGIN_DIALOG_COMMAND_CONFIRM = 1

FILE_MAP_ALL_ACCESS = 0xF001F
PAGE_READWRITE = 0x04
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

kernel32.OpenFileMappingW.argtypes = [
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
kernel32.OpenFileMappingW.restype = wintypes.HANDLE
kernel32.MapViewOfFile.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_size_t,
]
kernel32.MapViewOfFile.restype = wintypes.LPVOID
kernel32.UnmapViewOfFile.argtypes = [wintypes.LPCVOID]
kernel32.UnmapViewOfFile.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CreateFileMappingW.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPCWSTR,
]
kernel32.CreateFileMappingW.restype = wintypes.HANDLE

_ROOT = Path(__file__).resolve().parents[2]  # .../game-get
_NATIVE_DIR = _ROOT / "native" / "bin"
DLL_NAME = "xajh_login_bridge_v2.dll"
INJECTOR_NAME = "xajh_login_inject.exe"

# VK codes commonly needed by login automation.
VK_RETURN = 0x0D
VK_TAB = 0x09
VK_BACK = 0x08
VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_UP = 0x26

# Win32 messages understood by 0xE90CC0.
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208


@dataclass
class LoginBridgeResult:
    """Result of one login-bridge command call."""

    ok: bool
    cmd: int = 0
    status: int = 0
    ret: int = 0
    error: str | None = None
    note: str = ""
    hwnd: int = 0
    module_base: int = 0
    protocol_version: int = 0
    id_lo: int = 0
    id_hi: int = 0
    mode: int = 0
    tid: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "cmd": self.cmd,
            "status": self.status,
            "ret": self.ret,
            "error": self.error,
            "note": self.note,
            "hwnd": self.hwnd,
            "module_base": self.module_base,
            "protocol_version": self.protocol_version,
            "id_lo": self.id_lo,
            "id_hi": self.id_hi,
            "mode": self.mode,
            "tid": self.tid,
        }


class LoginStage(str, Enum):
    """Observable xajh login stages; UNKNOWN is never treated as ready."""

    UNKNOWN = "unknown"
    SERVER_SELECT = "server_select"
    CREDENTIALS = "credentials"
    CHARACTER_SELECT = "character_select"
    ENTERING_WORLD = "entering_world"
    IN_WORLD = "in_world"


@dataclass(frozen=True)
class LoginStageProbe:
    """One non-destructive login UI probe result."""

    stage: LoginStage
    dialog_name: str = ""
    dialog_ptr: int = 0
    shown: bool = False
    method: str = "aui_component"
    error: str | None = None
    diagnostics: dict = field(default_factory=dict)


_LOGIN_STAGE_DIALOGS = (
    (LoginStage.SERVER_SELECT, "Win_LoginServerList"),
    (LoginStage.SERVER_SELECT, "Win_CharServerList"),
    (LoginStage.CREDENTIALS, "Win_Login"),
    (LoginStage.CREDENTIALS, "Win_LoginTrust"),
    (LoginStage.CHARACTER_SELECT, "Win_CharList"),
    (LoginStage.CHARACTER_SELECT, "Win_CharEnterWorld"),
)
_LOGIN_DIALOG_CACHE: dict[tuple[int, str], int] = {}
_SERVER_SELECT_CONFIRM_NAMES = (
    "confirm",
    "Btn_CurrentServer",
    "Btn_LastLoginServer",
    "Btn_CharServer",
    "Btn_Decide",
    "Btn_Enter",
    "Btn_Confirm",
    "Btn_Ok",
    "Btn_OK",
)
# Credentials page edit controls (Win_Login.xml vocabulary).
_CREDENTIAL_EDIT_NAMES = ("Input_1", "Input_2", "Input_3")
_CREDENTIAL_SUBMIT_NAMES = (
    "Btn_Login",
    "Btn_Ok",
    "Btn_OK",
    "Btn_Enter",
    "Btn_Decide",
    "Btn_Submit",
    "Btn_Confirm",
)

# Prepared shared-memory handles kept alive across prepare/inject.
_PREPARED_SHM: dict[int, tuple[int, int, str]] = {}


def _pid_alive(pid: int) -> bool:
    try:
        import psutil

        return bool(psutil.Process(int(pid)).is_running())
    except Exception:
        try:
            h = kernel32.OpenProcess(0x0400, False, int(pid))
            if h:
                kernel32.CloseHandle(wintypes.HANDLE(h))
                return True
        except Exception:
            pass
        return False


def login_bridge_built() -> bool:
    """True when both native login-bridge artifacts exist. @author by ak"""
    return (_NATIVE_DIR / DLL_NAME).is_file() and (
        _NATIVE_DIR / INJECTOR_NAME
    ).is_file()


class LoginBridge:
    """Open shared memory for an already-injected login bridge."""

    def __init__(self, pid: int, *, log: LogFn | None = None):
        self.pid = int(pid)
        self._log = log or (lambda _m: None)
        self._map = 0
        self._view = 0
        self._mapped_size = 0
        self._hwnd = 0
        self.shm_name: str = ""

    def open(self, *, quiet: bool = False) -> bool:
        """Map existing shared memory (Global then Local). @author by ak"""
        names = [
            f"Global\\XajhLoginBridgeV2_{self.pid}",
            f"Local\\XajhLoginBridgeV2_{self.pid}",
        ]
        h = 0
        used = ""
        last_err = 0
        for name in names:
            h = kernel32.OpenFileMappingW(FILE_MAP_ALL_ACCESS, False, name)
            if h:
                used = name
                break
            last_err = int(ctypes.get_last_error() or 0)
        if not h:
            if not quiet:
                self._log(f"OpenFileMapping failed pid={self.pid} last_err={last_err}")
            return False
        view = kernel32.MapViewOfFile(h, FILE_MAP_ALL_ACCESS, 0, 0, SHARED_SIZE)
        view_i = int(ctypes.cast(view, ctypes.c_void_p).value or 0) if view else 0
        if not view_i:
            kernel32.CloseHandle(wintypes.HANDLE(h))
            if not quiet:
                self._log(f"MapViewOfFile failed err={ctypes.get_last_error()}")
            return False
        self._map = int(h)
        self._view = view_i
        self._mapped_size = SHARED_SIZE
        magic = self._u32(_OFF_MAGIC)
        if magic != LOGIN_BRIDGE_MAGIC:
            self._log(f"bad magic 0x{magic:X} name={used}")
            self.close()
            return False
        self._hwnd = self._u32(_OFF_HWND)
        self.shm_name = used
        if not quiet:
            self._log(
                f"login bridge open pid={self.pid} name={used} hwnd=0x{self._hwnd:X}"
            )
        return True

    def close(self) -> None:
        if self._view:
            kernel32.UnmapViewOfFile(ctypes.c_void_p(self._view))
            self._view = 0
            self._mapped_size = 0
        if self._map:
            kernel32.CloseHandle(wintypes.HANDLE(self._map))
            self._map = 0

    # ---- low-level shm accessors ----
    def _u32(self, off: int) -> int:
        return ctypes.c_uint32.from_address(self._view + off).value

    def _i32(self, off: int) -> int:
        return ctypes.c_int32.from_address(self._view + off).value

    def _set_u32(self, off: int, val: int) -> None:
        ctypes.c_uint32.from_address(self._view + off).value = int(val) & 0xFFFFFFFF

    def _set_i32(self, off: int, val: int) -> None:
        ctypes.c_int32.from_address(self._view + off).value = int(val)

    def _err(self) -> str:
        raw = (ctypes.c_char * 128).from_address(self._view + _OFF_ERR)
        return bytes(raw).split(b"\x00", 1)[0].decode("utf-8", "ignore")

    def call(
        self,
        cmd: int,
        *,
        id_lo: int = 0,
        id_hi: int = 0,
        tid: int = 0,
        mode: int = 0,
        timeout_ms: int = 5000,
    ) -> LoginBridgeResult:
        """Serialize the single-slot protocol and wait for ack. @author by ak"""
        if not self._view:
            return LoginBridgeResult(ok=False, cmd=int(cmd), error="bridge not open")
        seq = (self._u32(_OFF_SEQ) + 1) & 0xFFFFFFFF
        self._set_u32(_OFF_SEQ, seq)
        self._set_u32(_OFF_CMD, int(cmd))
        self._set_i32(_OFF_RET, 0)
        self._set_i32(_OFF_MODE, int(mode))
        self._set_u32(_OFF_ID_LO, int(id_lo) & 0xFFFFFFFF)
        self._set_u32(_OFF_ID_HI, int(id_hi) & 0xFFFFFFFF)
        self._set_i32(_OFF_TID, int(tid))
        ctypes.memset(self._view + _OFF_ERR, 0, 128)
        # Publish last: the native UI timer treats PENDING as ownership transfer.
        self._set_i32(_OFF_STATUS, LST_PENDING)
        self._set_u32(_OFF_ACK_SEQ, 0)

        # Best-effort wake so the game UI timer drains the slot promptly.
        if self._hwnd:
            try:
                user32.PostMessageW(wintypes.HWND(self._hwnd), 0x0200, 0, 0)
            except Exception:
                pass

        deadline = time.monotonic() + (max(int(timeout_ms), 1) / 1000.0)
        while time.monotonic() < deadline:
            status = self._i32(_OFF_STATUS)
            if status in (LST_OK, LST_ERR):
                if self._u32(_OFF_ACK_SEQ) != seq:
                    time.sleep(0.01)
                    continue
                ret = self._i32(_OFF_RET)
                err = self._err()
                return LoginBridgeResult(
                    ok=(status == LST_OK),
                    cmd=int(cmd),
                    status=status,
                    ret=ret,
                    error=None if status == LST_OK else (err or f"status={status}"),
                    note=err,
                    hwnd=self._hwnd,
                    module_base=self._u32(_OFF_MODULE_BASE),
                    protocol_version=self._u32(_OFF_PROTOCOL_VERSION),
                    id_lo=self._u32(_OFF_ID_LO),
                    id_hi=self._u32(_OFF_ID_HI),
                    mode=self._i32(_OFF_MODE),
                    tid=self._i32(_OFF_TID),
                )
            time.sleep(0.01)
        return LoginBridgeResult(
            ok=False, cmd=int(cmd), error="timeout waiting for login bridge"
        )


def _open_bridge(pid: int, *, quiet: bool = True) -> LoginBridge | None:
    br = LoginBridge(pid)
    if not br.open(quiet=quiet):
        br.close()
        return None
    return br


# --------------------------------------------------------------------------
# Dialog / stage probing (login UI is CECLoginUIMan owned, not always
# reachable through GetGameUIDlg).
# --------------------------------------------------------------------------


def _read_cached_login_dialog_candidate(session, ptr: int, expected: str) -> bool:
    """Revalidate a previously observed login dialog with bounded reads."""
    ptr = int(ptr) & 0xFFFFFFFF
    if not ptr:
        return False
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(session.pm.process_handle, ptr, 0xB0)
        name_ptr = struct.unpack_from("<I", raw, 0x4C)[0]
        x, y, w, h = struct.unpack_from("<iiii", raw, 0x9C)
        if raw[0x94] != 1 or not (8 < w <= 4096 and 8 < h <= 4096):
            return False
        if not (-4096 <= x <= 8192 and -4096 <= y <= 8192):
            return False
        name_raw = pymem.memory.read_bytes(session.pm.process_handle, name_ptr, 64)
        return name_raw.split(b"\x00", 1)[0].decode("ascii", "ignore") == expected
    except Exception:
        return False


def _discover_login_dialogs(session, *, log: LogFn | None = None) -> tuple[dict[str, int], dict]:
    """Return visible login dialogs by scanning heap objects for the login UI
    vtable fingerprints (never GetGameUIDlg — the login UI is owned by
    CECLoginUIMan, not the ordinary game UI manager).

    Verified fingerprints (xajh build 2026-08):
      vtable 0x12B0F04 = CECLoginUIMan / credentials page (Win_Login), show@+0x94
      vtable 0x12B12AC = server-select dialog (Win_LoginServerList), show@+0x94
    Any object with a matching vtable and show==1 is the visible page.

    Fast path: the login manager / dialog addresses are stable while the login
    stage lives, so a cached pointer is re-validated first (bounded reads);
    only on a miss does it fall back to a full heap scan.

    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(getattr(session, "pid", 0) or 0)
    diagnostics = {
        "method": "login_vtable_scan",
        "pid": pid,
        "cache_hits": [],
        "scan": "vtable_scan",
    }
    if not pid:
        diagnostics["error"] = "no pid"
        return {}, diagnostics

    # 1) Fast path: the unique CECLoginUIMan (vtable 0x12B0F04). Locating it
    #    costs one ~1.3s heap walk (unique instance, stops at first hit), then
    #    the current page is a bounded 3-read chain (mgr -> +0x34 child ->
    #    +0x78 page). The manager + child are stable across page switches, so
    #    this replaces the slow multi-page scan on cache misses. This is the
    #    authoritative probe: page switches update child+0x78, so it always
    #    reflects the *current* page without relying on stale page caches.
    #    Verified 2026-08-15 with x32dbg on server-select and credentials pages.
    mgr = _discover_login_manager(pid, log=log)
    if mgr:
        hit = _probe_stage_via_manager(pid, mgr)
        if hit:
            name, page = hit
            diagnostics["scan"] = "login_manager"
            diagnostics["manager"] = f"0x{mgr:X}"
            _LOGIN_DIALOG_CACHE[(pid, name)] = page
            diagnostics["cache_hits"].append({"name": name, "ptr": f"0x{page:X}"})
            return {name: page}, diagnostics

    # 2) Fallback: re-validate cached (stage_name, ptr) pairs.
    cached_found: dict[str, int] = {}
    stale_keys: list[tuple[int, str]] = []
    for (c_pid, name), ptr in list(_LOGIN_DIALOG_CACHE.items()):
        if c_pid != pid or not ptr:
            continue
        if _cached_login_page_alive(pid, ptr):
            cached_found[name] = ptr
            diagnostics["cache_hits"].append({"name": name, "ptr": f"0x{ptr:X}"})
        else:
            stale_keys.append((c_pid, name))
    if cached_found:
        diagnostics["scan"] = "cache_revalidate"
        return cached_found, diagnostics
    for key in stale_keys:
        _LOGIN_DIALOG_CACHE.pop(key, None)

    # 3) The manager chain is authoritative: the login manager and its pages
    #    live and die together (verified 2026-08-15: after entering the world
    #    the manager object's vtable reverts to garbage). If the manager is
    #    absent, no login page can exist — skip the slow page scan entirely.
    if not mgr:
        diagnostics["scan"] = "login_manager_absent"
        return {}, diagnostics

    # 4) Slow path: full heap scan (manager found but page chain failed).
    try:
        found = _scan_login_vtable_objects(pid, log=log)
    except Exception as e:
        diagnostics["error"] = f"scan_failed: {e}"
        log(f"_discover_login_dialogs scan error: {e}")
        return {}, diagnostics
    discovered: dict[str, int] = {}
    for name, obj in found:
        if obj:
            discovered[name] = obj
            _LOGIN_DIALOG_CACHE[(pid, name)] = obj
            diagnostics["cache_hits"].append({"name": name, "ptr": f"0x{obj:X}"})
    diagnostics["elapsed_ms"] = 0.0
    return discovered, diagnostics


def _cached_login_page_alive(pid: int, ptr: int) -> bool:
    """Re-validate a cached login page object: vtable in our fingerprint set,
    show flag +0x94 == 1, and +0x34 child vtable == _LOGIN_PAGE_CHILD_VTABLE.
    Bounded reads only — fast.

    The +0x34 child check is the reliable discriminator (verified 2026-08-15):
    shared 0x12D88A4 objects used by non-login controls point +0x34 at a
    different child vtable and are excluded, so a credentials page is never
    mis-detected as char-select. @author by ak"""
    ptr = int(ptr) & 0xFFFFFFFF
    if not ptr:
        return False
    try:
        import ctypes as _c
        import struct as _s

        from app.core.remote_runtime import open_process, read_process

        h = open_process(int(pid))
        try:
            vtable = int.from_bytes(read_process(h, ptr, 4), "little")
            show = int.from_bytes(read_process(h, ptr + 0x94, 1), "little")
            child_raw = read_process(h, ptr + 0x34, 4)
            child = int.from_bytes(child_raw, "little") if len(child_raw) == 4 else 0
            cv = int.from_bytes(read_process(h, child, 4), "little") if child else 0
            child_ok = (cv == _LOGIN_PAGE_CHILD_VTABLE)
        finally:
            _c.windll.kernel32.CloseHandle(_c.c_void_p(h))
        if vtable not in _LOGIN_VTABLE_STAGES:
            return False
        return show == 1 and child_ok
    except Exception:
        return False


# Login-page vtable fingerprints -> (LoginStage, canonical dialog name).
# 0x12DDC2C is the true character-select page (unique instance, verified
# 2026-08-15 via x32dbg on a live char-select page). 0x12D88A4 is a SHARED
# vtable used by many child controls too; it is kept only as a fallback and
# must pass the child-vtable check (+0x34 -> _LOGIN_PAGE_CHILD_VTABLE) to be
# treated as a page.
_LOGIN_VTABLE_STAGES = {
    0x12B0F04: (LoginStage.CREDENTIALS, "Win_Login"),
    0x12B12AC: (LoginStage.SERVER_SELECT, "Win_LoginServerList"),
    0x12DDC2C: (LoginStage.CHARACTER_SELECT, "Win_CharList"),
    0x12D88A4: (LoginStage.CHARACTER_SELECT, "Win_CharList"),
}

# A real login page object (any of the vtable fingerprints above) points its
# +0x34 to a login-page child manager whose vtable is this value. Shared
# vtable 0x12D88A4 objects used by non-login controls have a different +0x34
# child vtable (e.g. 0x18B547F8), so this check separates the real page from
# false hits. Verified 2026-08-15 on server-select (0x4028A720 page, +0x34 ->
# 0x19624948 vtable 0x12702F4). @author by ak
_LOGIN_PAGE_CHILD_VTABLE = 0x12702F4

# CECLoginUIMan is the unique login UI manager. Its +0x34 points to the
# login-page child manager (vtable _LOGIN_PAGE_CHILD_VTABLE), whose +0x78
# holds the currently visible page object. Verified 2026-08-15 with x32dbg:
#   mgr 0x45766B18 (vtable 0x12B0F04) -> +0x34 0x192F8060 (0x12702F4)
#     -> +0x78 page 0x19317A98 (SEL, show=1) and, after Enter on the
#     server-select page, page became 0x45766B18 (the manager itself,
#     CRED). Manager + child stay stable across page switches, so caching
#     them makes every probe a bounded 3-read chain (mgr -> child -> page).
#     On the credentials page the current page IS the manager object
#     (vtable 0x12B0F04 == CREDENTIALS fingerprint). @author by ak
_LOGIN_MANAGER_VTABLE = 0x12B0F04
_LOGIN_MANAGER_CACHE: dict[int, int] = {}


def _cached_login_manager_alive(pid: int, mgr: int) -> bool:
    """Re-validate a cached CECLoginUIMan: vtable 0x12B0F04 and +0x34 child
    vtable 0x12702F4. Bounded reads only — fast. @author by ak"""
    mgr = int(mgr) & 0xFFFFFFFF
    if not mgr:
        return False
    try:
        import ctypes as _c

        from app.core.remote_runtime import open_process, read_process

        h = open_process(int(pid))
        try:
            mv = int.from_bytes(read_process(h, mgr, 4), "little")
            child_raw = read_process(h, mgr + 0x34, 4)
            child = int.from_bytes(child_raw, "little") if len(child_raw) == 4 else 0
            cv = int.from_bytes(read_process(h, child, 4), "little") if child else 0
        finally:
            _c.windll.kernel32.CloseHandle(_c.c_void_p(h))
        return mv == _LOGIN_MANAGER_VTABLE and cv == _LOGIN_PAGE_CHILD_VTABLE
    except Exception:
        return False


def _discover_login_manager(pid: int, *, log: LogFn | None = None) -> int:
    """Locate the single CECLoginUIMan object (vtable 0x12B0F04 with +0x34
    child vtable 0x12702F4). First check the cache, then a heap walk that
    stops at the first valid manager (~1.3s vs a full page scan's 15-25s —
    the manager is a unique instance). Returns 0 when not found. @author by ak"""
    log = log or (lambda _m: None)
    pid = int(pid)
    cached = _LOGIN_MANAGER_CACHE.get(pid)
    if cached and _cached_login_manager_alive(pid, cached):
        return cached
    try:
        import ctypes as _c

        from app.core.remote_runtime import open_process
        from app.core.remote_runtime import read_process as _rp

        h = open_process(pid)
        proc_handle = _c.c_void_p(h)
        k32 = _c.WinDLL("kernel32", use_last_error=True)
        k32.ReadProcessMemory.restype = _c.c_int
        k32.ReadProcessMemory.argtypes = [_c.c_void_p, _c.c_void_p, _c.c_void_p, _c.c_size_t, _c.c_void_p]
        k32.VirtualQueryEx.restype = _c.c_size_t

        class MBI(_c.Structure):
            _fields_ = [
                ("BaseAddress", _c.c_void_p),
                ("AllocationBase", _c.c_void_p),
                ("AllocationProtect", _c.c_uint32),
                ("PartitionId", _c.c_uint32),
                ("RegionSize", _c.c_size_t),
                ("State", _c.c_uint32),
                ("Protect", _c.c_uint32),
                ("Type", _c.c_uint32),
            ]

        import struct as _s

        needle = _s.pack("<I", _LOGIN_MANAGER_VTABLE)
        addr = 0x10000
        guard = 0
        while addr <= 0x7FFFFFFF and guard < 60000:
            m = MBI()
            if not k32.VirtualQueryEx(proc_handle, _c.c_void_p(addr), _c.byref(m), _c.sizeof(m)):
                break
            base = int(m.BaseAddress or 0)
            size = int(m.RegionSize or 0)
            if not size:
                break
            guard += 1
            if int(m.State) == 0x1000 and (int(m.Protect) & 0xFF) in (2, 4, 8, 32, 64, 128) \
                    and int(m.Type) == 0x20000:
                try:
                    buf = _rp(h, base, size)
                except Exception:
                    addr = base + size
                    continue
                i = 0
                while True:
                    i = buf.find(needle, i)
                    if i < 0:
                        break
                    obj = base + i
                    if obj % 4 == 0:
                        p34 = _s.unpack_from("<I", buf, i + 0x34)[0] if i + 0x38 <= len(buf) else 0
                        if p34:
                            try:
                                cv_raw = _rp(h, p34, 4)
                            except Exception:
                                cv_raw = b""
                            cv = _s.unpack("<I", cv_raw)[0] if len(cv_raw) == 4 else 0
                            if cv == _LOGIN_PAGE_CHILD_VTABLE:
                                _LOGIN_MANAGER_CACHE[pid] = obj
                                return obj
                    i += 4
            addr = base + size
        k32.CloseHandle(proc_handle)
        return 0
    except Exception as e:
        log(f"_discover_login_manager error: {e}")
        return 0


def _probe_stage_via_manager(pid: int, mgr: int) -> tuple[str, int] | None:
    """Resolve the current login page from the manager chain: mgr -> +0x34
    child manager -> +0x78 current page. Returns (page_name, page_ptr) or
    None when the page is not a known login fingerprint. Bounded reads. @author by ak"""
    mgr = int(mgr) & 0xFFFFFFFF
    if not mgr:
        return None
    try:
        import ctypes as _c

        from app.core.remote_runtime import open_process, read_process

        h = open_process(int(pid))
        try:
            import struct as _s

            child_raw = read_process(h, mgr + 0x34, 4)
            child = _s.unpack("<I", child_raw)[0] if len(child_raw) == 4 else 0
            if not child:
                return None
            page_raw = read_process(h, child + 0x78, 4)
            page = _s.unpack("<I", page_raw)[0] if len(page_raw) == 4 else 0
            if not page:
                return None
            vt_raw = read_process(h, page, 4)
            vt = _s.unpack("<I", vt_raw)[0] if len(vt_raw) == 4 else 0
            info = _LOGIN_VTABLE_STAGES.get(vt)
            if not info:
                return None
            show_raw = read_process(h, page + 0x94, 1)
            show = int(show_raw[0]) if show_raw else 0
            if show != 1:
                return None
            return (info[1], page)
        finally:
            _c.windll.kernel32.CloseHandle(_c.c_void_p(h))
    except Exception:
        return None


def _scan_login_vtable_objects(pid: int, *, log: LogFn | None = None) -> list[tuple[str, int]]:
    """Scan private heap for objects whose first dword is a login vtable and
    whose show flag (+0x94) is 1. Returns [(stage_name, obj_ptr), ...].

    Uses an independent OpenProcess handle (pymem's handle may lack
    PROCESS_VM_READ on this target).
    @author by ak
    """
    log = log or (lambda _m: None)
    out: list[tuple[str, int]] = []  # (stage_name, obj)
    try:
        import ctypes as _c

        from app.core.remote_runtime import open_process
        from app.core.remote_runtime import read_process as _rp

        h = open_process(int(pid))
        proc_handle = _c.c_void_p(h)
        k32 = _c.WinDLL("kernel32", use_last_error=True)
        k32.ReadProcessMemory.restype = _c.c_int
        k32.ReadProcessMemory.argtypes = [_c.c_void_p, _c.c_void_p, _c.c_void_p, _c.c_size_t, _c.c_void_p]
        k32.VirtualQueryEx.restype = _c.c_size_t

        class MBI(_c.Structure):
            _fields_ = [
                ("BaseAddress", _c.c_void_p),
                ("AllocationBase", _c.c_void_p),
                ("AllocationProtect", _c.c_uint32),
                ("PartitionId", _c.c_uint32),
                ("RegionSize", _c.c_size_t),
                ("State", _c.c_uint32),
                ("Protect", _c.c_uint32),
                ("Type", _c.c_uint32),
            ]

        import struct as _s

        # Scan only unique page/manager vtables. 0x12D88A4 is excluded here:
        # it is a SHARED vtable used by many child controls (verified 2026-08-15),
        # so a full walk over it is both slow and ambiguous. The true
        # character-select page vtable is 0x12DDC2C (unique instance).
        targets = {
            vt: name for vt, (stage, name) in _LOGIN_VTABLE_STAGES.items()
            if vt != 0x12D88A4
        }
        needles = {vt: _s.pack("<I", vt) for vt in targets}
        addr = 0x10000
        guard = 0
        # Walk the full private heap like _discover_login_manager (whole-region
        # reads are far faster than the old 1MB chunking — manager at ~0x4Axxxxxx
        # is past the old 0x46000000 cap). Stop at the first valid page.
        _scan_cap = 0x7FFFFFFF
        while addr <= _scan_cap and guard < 60000:
            m = MBI()
            if not k32.VirtualQueryEx(proc_handle, _c.c_void_p(addr), _c.byref(m), _c.sizeof(m)):
                break
            base = int(m.BaseAddress or 0)
            size = int(m.RegionSize or 0)
            if not size:
                break
            guard += 1
            if int(m.State) == 0x1000 \
                    and (int(m.Protect) & 0xFF) in (2, 4, 8, 32, 64, 128) \
                    and int(m.Type) == 0x20000:
                try:
                    data = _rp(h, base, size)
                except Exception:
                    addr = base + size
                    continue
                for vt, needle in needles.items():
                    i = 0
                    while True:
                        i = data.find(needle, i)
                        if i < 0:
                            break
                        obj = base + i
                        if obj % 4 == 0 and 0x10000 < obj < 0x80000000:
                            show = data[i + 0x94] if i + 0x94 < len(data) else 0
                            if show == 1:
                                # A real login page points +0x34 to the
                                # login-page child manager (vtable
                                # _LOGIN_PAGE_CHILD_VTABLE). Shared vtable
                                # objects used by non-login controls have a
                                # different child vtable and are excluded here.
                                child_ok = False
                                if i + 0x38 <= len(data):
                                    child = _s.unpack_from("<I", data, i + 0x34)[0]
                                    if child:
                                        try:
                                            cv_raw = _rp(h, child, 4)
                                        except Exception:
                                            cv_raw = b""
                                        cv = _s.unpack("<I", cv_raw)[0] if len(cv_raw) == 4 else 0
                                        child_ok = (cv == _LOGIN_PAGE_CHILD_VTABLE)
                                if not child_ok:
                                    i += 1
                                    continue
                                name = targets[vt]
                                out[:] = [(n, o) for n, o in out if n != name]
                                out.append((name, obj))
                                if len(out) >= 1:
                                    k32.CloseHandle(proc_handle)
                                    return out
                        i += 4
            addr = base + size
        k32.CloseHandle(proc_handle)
        return out
    except Exception as e:
        log(f"_scan_login_vtable_objects error: {e}")
        import traceback as _tb

        log("".join(_tb.format_exception(type(e), e, e.__traceback__))[-800:])
        return out


def probe_login_stage(session, *, log: LogFn | None = None) -> LoginStageProbe:
    """Determine visible xajh login stage from AUI, never loaded resources.

    The login UI is owned by CECLoginUIMan (vtable 0x12B0F04), not the ordinary
    game UI manager used by GetGameUIDlg, so the authoritative probe is a heap
    scan for the login vtable fingerprints. GetGameUIDlg remains a secondary
    hint for ordinary dialogs.
    """
    log = log or (lambda _m: None)
    try:
        # 1) Authoritative: the CECLoginUIMan chain (mgr -> child -> current
        #    page), cached per pid. This resolves every login stage in <1ms
        #    once the manager is located (~1.3s first time).
        discovered, diagnostics = _discover_login_dialogs(session, log=log)
        for stage, name in _LOGIN_STAGE_DIALOGS:
            dlg_ptr = int(discovered.get(name, 0) or 0)
            if dlg_ptr:
                return LoginStageProbe(
                    stage,
                    name,
                    dlg_ptr,
                    True,
                    method="login_vtable_scan",
                    diagnostics=diagnostics,
                )
        # 1b) When the login UI manager is gone (already in world), the heap
        #     scans above wasted time confirming nothing exists. Query the
        #     game state now — in_world means the login UI is torn down.
        from app.core.plg_ui import query_dlg_show, get_game_state

        # Cheap in-world check first: the window title carries a role name
        # ("笑傲江湖OL - 角色名 服务器...") only after entering the world. This
        # avoids the remote-call path (VirtualAllocEx err=5) that can fail
        # intermittently once the login manager is already torn down.
        try:
            from app.core.inject_gate import find_main_hwnd_for_pid
            from app.core.window_title import title_not_in_role

            _hwnd, title, _cls = find_main_hwnd_for_pid(pid)
            if _hwnd and title and not title_not_in_role(title):
                return LoginStageProbe(
                    LoginStage.IN_WORLD, method="window_title", diagnostics=diagnostics
                )
        except Exception:
            pass

        state = get_game_state(session, log=log)
        if state == 3:
            return LoginStageProbe(
                LoginStage.IN_WORLD, method="game_state", diagnostics=diagnostics
            )
        # 2) Secondary: ordinary game UI dialogs.
        candidates = (
            (LoginStage.SERVER_SELECT, ("Win_LoginServerList", "Win_CharServerList")),
            (LoginStage.CREDENTIALS, ("Win_Login", "Win_LoginTrust")),
            (LoginStage.CHARACTER_SELECT, ("Win_CharList", "Win_CharEnterWorld")),
        )
        for stage, names in candidates:
            for name in names:
                hit = query_dlg_show(session, name, log=log)
                if hit.ok and hit.shown:
                    return LoginStageProbe(stage, name, int(hit.dlg_ptr), True)
        state_note = f"game_state={state}" if state is not None else "game_state=unavailable"
        return LoginStageProbe(
            LoginStage.UNKNOWN,
            method="login_vtable_scan",
            error=f"visible_login_dialog_not_resolved; {state_note}",
            diagnostics=diagnostics,
        )
    except Exception as e:
        log(f"probe_login_stage error: {e}")
        return LoginStageProbe(LoginStage.UNKNOWN, method="aui_component", error=str(e))


# --------------------------------------------------------------------------
# Character-select page role list reading.
# --------------------------------------------------------------------------

# Win_CharList dialog (the real character-select page) vtable. Shared with some
# child controls, so the fullscreen-rect + Name("Win_CharList") checks decide.
# Verified 2026-08-15 (PID <PID>): the dialog holds a child-control hash table
# (+0x234: +4 bucket count, +8 bucket-array ptr) whose nodes are
# {+0x00 next, +0x04 control obj, +0x08 name}. The 3 role cards are the
# Txt_NameN / Txt_LevelN / Img_HeadN controls:
#   Txt_NameN   +0xB8 -> wchar* 角色名
#   Txt_LevelN  +0xB8 -> wchar* 职业+等级 (e.g. "唐门 140级")
#   Img_HeadN   +0x138 -> u32 role_id
# Card index N=1/2/3 == slot 1/2/3. @author by ak
_WIN_CHARLIST_VTABLE = 0x12D88A4
_WIN_CHARLIST_NAME = b"Win_CharList"
_CHARLIST_HASH_OFF = 0x234
_CHARLIST_NODE_OBJ = 0x04
_CHARLIST_NODE_NAME = 0x08
_CHARLIST_TXT_NAME = "Txt_Name{}"
_CHARLIST_TXT_LEVEL = "Txt_Level{}"
_CHARLIST_IMG_HEAD = "Img_Head{}"
_CHARLIST_TEXT_OFF = 0xB8
_CHARLIST_ROLE_ID_OFF = 0x138
_CHARLIST_SLOT_COUNT = 3


def _read_remote_wstr_utf16(pid: int, ptr: int, *, max_chars: int = 32) -> str:
    """Read a UTF-16LE NUL-terminated wide string from the game. @author by ak"""
    ptr = int(ptr) & 0xFFFFFFFF
    if not ptr:
        return ""
    try:
        from app.core.remote_runtime import open_process, read_process

        h = open_process(int(pid))
        try:
            import ctypes as _c

            raw = read_process(h, ptr, max(2, int(max_chars) * 2))
            out = []
            for j in range(0, len(raw) - 1, 2):
                c = raw[j] | (raw[j + 1] << 8)
                if c == 0:
                    break
                if not (0x20 <= c <= 0x9FFF):
                    c = ord(".")
                out.append(chr(c))
            return "".join(out).strip()
        finally:
            _c.windll.kernel32.CloseHandle(_c.c_void_p(h))
    except Exception:
        return ""


def _char_list_controls(pid: int, dlg: int) -> dict[str, int]:
    """Map control name -> control obj for the Win_CharList hash table.

    Returns {} on failure. Bounded walk of the bucket linked lists. @author by ak
    """
    try:
        import ctypes as _c
        import struct as _s

        from app.core.remote_runtime import open_process, read_process

        h = open_process(int(pid))
        try:
            raw = read_process(h, int(dlg) + _CHARLIST_HASH_OFF, 12)
            if len(raw) < 12:
                return {}
            _nb = _s.unpack_from("<I", raw, 0)[0]
            nbuckets = _s.unpack_from("<I", raw, 4)[0]
            bucket_ptr = _s.unpack_from("<I", raw, 8)[0]
            if not (0 < nbuckets <= 0x10000) or not bucket_ptr:
                return {}
            out: dict[str, int] = {}
            seen: set[int] = set()
            for b in range(int(nbuckets)):
                node_raw = read_process(h, bucket_ptr + b * 4, 4)
                if len(node_raw) < 4:
                    continue
                node = _s.unpack_from("<I", node_raw, 0)[0]
                guard = 0
                while node and guard < 4000:
                    guard += 1
                    if node in seen:
                        break
                    seen.add(node)
                    node_raw = read_process(h, node, 12)
                    if len(node_raw) < 12:
                        break
                    nxt, obj, name_ptr = _s.unpack_from("<III", node_raw, 0)
                    name_raw = read_process(h, name_ptr, 48)
                    z = name_raw.find(b"\x00")
                    if z < 0:
                        z = len(name_raw)
                    name = name_raw[:z].decode("ascii", "ignore")
                    if name:
                        out.setdefault(name, int(obj) & 0xFFFFFFFF)
                    node = int(nxt) & 0xFFFFFFFF
            return out
        finally:
            _c.windll.kernel32.CloseHandle(_c.c_void_p(h))
    except Exception:
        return {}


def read_char_select_roles(
    session,
    *,
    probe: LoginStageProbe | None = None,
    log: LogFn | None = None,
) -> dict:
    """Read the 3 role cards on the character-select page (Win_CharList).

    Returns {"ok", "roles": [{slot, name, level, role_id}, ...], "error"}.
    Only valid while the game is on the character-select page; otherwise
    {"ok": False, "error": "not_on_char_select"}.

    The real Win_CharList dialog is located dynamically (vtable 0x12D88A4,
    Name "Win_CharList", fullscreen rect >= 800x600) because its heap address
    changes every page load. Each role card's info is read from the dialog's
    child hash table:

      Txt_NameN +0xB8  role name (wchar*)
      Txt_LevelN +0xB8 class + level (wchar*, e.g. "唐门 140级")
      Img_HeadN +0x138 role_id (u32)

    Verified 2026-08-15 (PID <PID>): 张三/1001, 李四/1002,
    王五/1003 with 唐门 140级 / 新手 40级 / 新手 40级.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {"ok": False, "roles": [], "error": None}
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        out["error"] = "no_pid"
        return out
    if probe is None:
        probe = probe_login_stage(session, log=log)
    if probe.stage is not LoginStage.CHARACTER_SELECT or not probe.dialog_ptr:
        out["error"] = f"not_on_char_select (stage={probe.stage.value})"
        return out

    # The login-manager chain's current-page object is the Win_CharDelete
    # container, NOT the Win_CharList dialog that owns the role cards. Locate
    # Win_CharList dynamically (fullscreen + name).
    dlg = _find_win_charlist_dialog(pid, log=log)
    if not dlg:
        out["error"] = "win_charlist_not_found"
        return out

    ctrls = _char_list_controls(pid, dlg)
    if not ctrls:
        out["error"] = "char_list_controls_empty"
        return out

    roles: list[dict] = []
    for slot in range(1, _CHARLIST_SLOT_COUNT + 1):
        name_obj = ctrls.get(_CHARLIST_TXT_NAME.format(slot)) or 0
        level_obj = ctrls.get(_CHARLIST_TXT_LEVEL.format(slot)) or 0
        head_obj = ctrls.get(_CHARLIST_IMG_HEAD.format(slot)) or 0
        role = {"slot": slot, "name": "", "level": "", "role_id": 0}
        if name_obj:
            name_ptr = _read_u32_field(pid, int(name_obj) + _CHARLIST_TEXT_OFF)
            role["name"] = _read_remote_wstr_utf16(pid, name_ptr)
        if level_obj:
            level_ptr = _read_u32_field(pid, int(level_obj) + _CHARLIST_TEXT_OFF)
            role["level"] = _read_remote_wstr_utf16(pid, level_ptr)
        if head_obj:
            role["role_id"] = _read_u32_field(pid, int(head_obj) + _CHARLIST_ROLE_ID_OFF)
        roles.append(role)
    out["ok"] = True
    out["roles"] = roles
    log("read_char_select_roles: " + "; ".join(
        f"slot{r['slot']}={r['name']}(id={r['role_id']},{r['level']})" for r in roles
    ))
    return out


def char_card_center(
    pid: int,
    slot: int,
    *,
    log: LogFn | None = None,
) -> tuple[int, int] | None:
    """Client-space center of the slot-N role card (Rdo_CharN), or None.

    Reads the Win_CharList dialog origin + Rdo_CharN relative rect from the
    game's own layout, so the returned point is where the card actually is at
    any DPI / resolution / role count (verified 2026-08-16 as the dead-centre
    click target and glow anchor). @author by ak
    """
    log = log or (lambda _m: None)
    try:
        import ctypes as _ct
        import struct as _s

        from app.core.remote_runtime import open_process
        from app.core.remote_runtime import read_process as _rp

        dlg = _find_win_charlist_dialog(pid, log=log)
        if not dlg:
            return None
        ctrls = _char_list_controls(pid, dlg)
        rdo = int(ctrls.get(f"Rdo_Char{int(slot)}") or 0)
        if not rdo:
            return None
        h = open_process(int(pid))
        try:
            d_off = [
                _s.unpack_from("<I", _rp(h, dlg + off, 4), 0)[0]
                for off in (0x9C, 0xA0, 0xA4, 0xA8)
            ]
            rp = [
                _s.unpack_from("<I", _rp(h, rdo + off, 4), 0)[0]
                for off in (0x84, 0x88, 0x8C, 0x90)
            ]
        finally:
            _ct.windll.kernel32.CloseHandle(_ct.c_void_p(h))
        return (
            d_off[0] + rp[0] + rp[2] // 2,
            d_off[1] + rp[1] + rp[3] // 2,
        )
    except Exception as e:  # pragma: no cover
        log(f"char_card_center error: {e}")
        return None


def _stable_card_center(
    pid: int,
    slot: int,
    *,
    min_wait_s: float = 2.5,
    settle_s: float = 0.5,
    max_wait_s: float = 10.0,
    log: LogFn | None = None,
) -> tuple[int, int] | None:
    """Read the slot-N card centre after the char-select page has settled.

    Right after login the cards animate into their final layout, so clicking a
    one-shot read can hit a transitional position. Wait at least ``min_wait_s``
    AND for two consecutive identical reads before returning the centre.
    @author by ak
    """
    log = log or (lambda _m: None)
    t0 = time.monotonic()
    last = None
    first = None
    deadline = time.monotonic() + max(1.0, float(max_wait_s))
    while time.monotonic() < deadline:
        c = char_card_center(pid, slot, log=log)
        if first is None:
            first = c
        stable = bool(c and c == last and (time.monotonic() - t0) >= max(0.5, float(min_wait_s)))
        if stable:
            if first != c:
                log(f"card centre settled slot={slot} {first} -> {c}")
            return c
        last = c
        time.sleep(max(0.2, float(settle_s)))
    log(f"card centre not settled slot={slot} last={last}")
    return last


def _click_card_enter(
    pid: int,
    hwnd: int,
    cx: int,
    cy: int,
    *,
    slot: int | None = None,
    log: LogFn | None = None,
) -> bool:
    """Enter the world by a background standard double-click on the card.

    Waits for the user to be idle first (no mouse/key activity) so the
    double-click is not interleaved with the user's own clicks — otherwise the
    card only gets selected but the "enter world" double-click is swallowed.

    Verified (2026-08-16): a background standard double-click on the slot-N card
    centre enters the world with that role. @author by ak
    """
    log = log or (lambda _m: None)
    # 双击前等用户空闲：用户正在点鼠标时抢着双击，只会选中卡不会进世界。
    try:
        mon = _UserActivityMonitor()
        mon.start()
        try:
            idle_deadline = time.monotonic() + 60.0
            while mon.idle_seconds() < 0.8:
                if time.monotonic() > idle_deadline:
                    break
                time.sleep(0.1)
        finally:
            mon.stop()
    except Exception:
        pass
    # 先用 bridge 单击选中目标卡（轮询测试验证的准确路径：Rdo_CharN 中心），
    # 再双击进入世界，避免双击落点偏移进错相邻槽。
    s = ui_click(pid, int(cx), int(cy))
    if not s.ok:
        log(f"card select click fail: {s.error or s.note}")
        return False
    time.sleep(0.4)
    r = ui_dblclick(pid, int(cx), int(cy), hwnd=int(hwnd), log=log)
    if not r.ok:
        log(f"card dblclick fail: {r.error or r.note}")
        return False
    log(f"card select+dblclick ({cx},{cy})" + (f" slot={slot}" if slot else ""))
    return True


def _read_u32_field(pid: int, addr: int) -> int:
    """Read a remote u32 field (0 on failure). @author by ak"""
    addr = int(addr) & 0xFFFFFFFF
    if not addr:
        return 0
    try:
        from app.core.remote_runtime import open_process, read_process

        h = open_process(int(pid))
        try:
            import ctypes as _c
            import struct as _s

            raw = read_process(h, addr, 4)
            return _s.unpack_from("<I", raw, 0)[0] if len(raw) == 4 else 0
        finally:
            _c.windll.kernel32.CloseHandle(_c.c_void_p(h))
    except Exception:
        return 0


def _find_win_charlist_dialog(pid: int, *, log: LogFn | None = None) -> int:
    """Locate the real Win_CharList dialog object (vtable 0x12D88A4, Name
    "Win_CharList", show=1, fullscreen rect). Returns 0 when not found.

    The login manager chain reports a Win_CharDelete container as the current
    char-select page; the role-card dialog is a sibling Win_CharList object,
    so it must be located independently. @author by ak
    """
    log = log or (lambda _m: None)
    try:
        import ctypes as _c
        import struct as _s

        from app.core.remote_runtime import open_process
        from app.core.remote_runtime import read_process as _rp

        h = open_process(int(pid))
        proc_handle = _c.c_void_p(h)
        k32 = _c.WinDLL("kernel32", use_last_error=True)
        k32.ReadProcessMemory.restype = _c.c_int
        k32.ReadProcessMemory.argtypes = [_c.c_void_p, _c.c_void_p, _c.c_void_p, _c.c_size_t, _c.c_void_p]
        k32.VirtualQueryEx.restype = _c.c_size_t

        class MBI(_c.Structure):
            _fields_ = [
                ("BaseAddress", _c.c_void_p),
                ("AllocationBase", _c.c_void_p),
                ("AllocationProtect", _c.c_uint32),
                ("PartitionId", _c.c_uint32),
                ("RegionSize", _c.c_size_t),
                ("State", _c.c_uint32),
                ("Protect", _c.c_uint32),
                ("Type", _c.c_uint32),
            ]

        needle = _s.pack("<I", _WIN_CHARLIST_VTABLE)
        addr = 0x10000
        guard = 0
        while addr <= 0x7FFFFFFF and guard < 60000:
            m = MBI()
            if not k32.VirtualQueryEx(proc_handle, _c.c_void_p(addr), _c.byref(m), _c.sizeof(m)):
                break
            base = int(m.BaseAddress or 0)
            size = int(m.RegionSize or 0)
            if not size:
                break
            guard += 1
            if int(m.State) == 0x1000 \
                    and (int(m.Protect) & 0xFF) in (2, 4, 8, 32, 64, 128) \
                    and int(m.Type) == 0x20000:
                try:
                    buf = _rp(h, base, size)
                except Exception:
                    addr = base + size
                    continue
                i = 0
                while True:
                    i = buf.find(needle, i)
                    if i < 0:
                        break
                    obj = base + i
                    if obj % 4 == 0 and 0x10000 < obj < 0x80000000 \
                            and i + 0xB0 <= len(buf):
                        show = buf[i + 0x94]
                        if show != 1:
                            i += 4
                            continue
                        w = _s.unpack_from("<i", buf, i + 0xA4)[0]
                        hh = _s.unpack_from("<i", buf, i + 0xA8)[0]
                        if w < 800 or hh < 600:
                            i += 4
                            continue
                        name_ptr = _s.unpack_from("<I", buf, i + 0x4C)[0]
                        try:
                            name_raw = _rp(h, name_ptr, 16)
                        except Exception:
                            name_raw = b""
                        if name_raw.split(b"\x00", 1)[0] == _WIN_CHARLIST_NAME:
                            k32.CloseHandle(proc_handle)
                            return obj
                    i += 4
            addr = base + size
        k32.CloseHandle(proc_handle)
        return 0
    except Exception as e:
        log(f"_find_win_charlist_dialog error: {e}")
        return 0


# --------------------------------------------------------------------------
# Bridge command helpers (module-level public API).
# --------------------------------------------------------------------------


def ui_input(
    pid: int,
    this_ptr: int,
    msg: int,
    wparam: int,
    lparam: int = 1,
    extra: int = 1,
) -> LoginBridgeResult:
    """Same-process call of login UI input processor (0xE90CC0).

    msg = WM_KEYDOWN(0x100)/WM_KEYUP(0x101)/WM_CHAR(0x102)/mouse msgs.
    wparam = VK or char; lparam = 1 (native); extra is fixed to 1 inside DLL.
    Server-select confirm uses (0x100, VK_RETURN, 1). Credentials typing works
    once an edit control has focus (or via KEY_HOLD).
    """
    del extra  # native fixes extra=1; kept for API clarity
    br = _open_bridge(pid)
    if br is None:
        return LoginBridgeResult(ok=False, cmd=LCMD_UI_INPUT, error="login bridge not open")
    try:
        return br.call(
            LCMD_UI_INPUT,
            id_lo=int(this_ptr) & 0xFFFFFFFF,
            id_hi=int(msg) & 0xFFFFFFFF,
            mode=int(wparam) & 0xFFFFFFFF,
            tid=int(lparam) & 0xFFFFFFFF,
        )
    finally:
        br.close()


def ui_key(
    pid: int,
    vk: int,
    *,
    action: int = UI_KEY_PRESS,
    hold_ms: int = 60,
    no_focus: bool = False,
) -> LoginBridgeResult:
    """Send a key through the login bridge (SendInput path; diagnostic only)."""
    br = _open_bridge(pid)
    if br is None:
        return LoginBridgeResult(ok=False, cmd=LCMD_UI_KEY, error="login bridge not open")
    try:
        mode = max(1, int(hold_ms)) & 0x0FFF
        if no_focus:
            mode |= 0x1000
        return br.call(
            LCMD_UI_KEY, id_lo=int(vk) & 0xFF, id_hi=int(action) & 0xFF, mode=mode
        )
    finally:
        br.close()


def ui_click(
    pid: int, cx: int, cy: int, *, button: int = UI_CLICK_LEFT, hold_ms: int = 55
) -> LoginBridgeResult:
    """Send a background click at client (cx, cy) via the login bridge."""
    br = _open_bridge(pid)
    if br is None:
        return LoginBridgeResult(ok=False, cmd=LCMD_UI_CLICK, error="login bridge not open")
    try:
        return br.call(
            LCMD_UI_CLICK,
            id_lo=int(cx) & 0xFFFF,
            id_hi=int(cy) & 0xFFFF,
            tid=int(button),
            mode=hold_ms,
        )
    finally:
        br.close()


def ui_dblclick(
    pid: int, cx: int, cy: int, *, hwnd: int = 0, log: LogFn | None = None
) -> LoginBridgeResult:
    """Send a background double-click at client (cx, cy).

    Standard double-click sequence (verified 2026-08 on character-select):
      WM_MOUSEMOVE → WM_LBUTTONDOWN → WM_LBUTTONUP → WM_LBUTTONDBLCLK → WM_LBUTTONUP
    The game only treats the full sequence as "enter world"; a bare DBLCLK+UP
    or two plain clicks are ignored. PostMessage to the game hwnd — fully
    background, no foreground / no focus steal. @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(pid)
    if not hwnd:
        try:
            from app.core.inject_gate import find_main_hwnd_for_pid

            hwnd = int(find_main_hwnd_for_pid(pid)[0] or 0)
        except Exception:
            hwnd = 0
    if not hwnd:
        return LoginBridgeResult(ok=False, cmd=LCMD_UI_CLICK, error="no game hwnd")
    import ctypes as _ct
    import ctypes.wintypes as _wt

    u32 = _ct.WinDLL("user32", use_last_error=True)
    lp = (int(cy) << 16) | (int(cx) & 0xFFFF)
    # 间隔放宽到 80ms：太短（30ms）时游戏 UI 线程可能把 DOWN/UP/DBLCLK 合并，
    # 只当成单击选中而不进入世界。80ms 落在 Windows 双击窗口内且足够区分。
    u32.PostMessageW(_wt.HWND(hwnd), 0x0200, 0, lp)  # WM_MOUSEMOVE
    time.sleep(0.08)
    u32.PostMessageW(_wt.HWND(hwnd), 0x0201, 1, lp)  # WM_LBUTTONDOWN
    time.sleep(0.08)
    u32.PostMessageW(_wt.HWND(hwnd), 0x0202, 0, lp)  # WM_LBUTTONUP
    time.sleep(0.08)
    u32.PostMessageW(_wt.HWND(hwnd), 0x0203, 1, lp)  # WM_LBUTTONDBLCLK
    time.sleep(0.08)
    u32.PostMessageW(_wt.HWND(hwnd), 0x0202, 0, lp)  # WM_LBUTTONUP
    return LoginBridgeResult(ok=True, cmd=LCMD_UI_CLICK, note="UI_DBLCLICK ok")


def ui_dialog_command(pid: int, dlg_ptr: int, command: int) -> LoginBridgeResult:
    """Invoke a verified AUIDialog command on the game UI thread (diagnostic)."""
    br = _open_bridge(pid)
    if br is None:
        return LoginBridgeResult(
            ok=False, cmd=LCMD_UI_DIALOG_COMMAND, error="login bridge not open"
        )
    try:
        return br.call(
            LCMD_UI_DIALOG_COMMAND,
            id_lo=int(dlg_ptr) & 0xFFFFFFFF,
            id_hi=int(command) & 0xFFFFFFFF,
        )
    finally:
        br.close()


def key_hold(pid: int, vk: int, *, on: bool = True) -> LoginBridgeResult:
    """Force/clear a key via process-local IAT hook (background, no focus steal)."""
    br = _open_bridge(pid)
    if br is None:
        return LoginBridgeResult(ok=False, cmd=LCMD_KEY_HOLD, error="login bridge not open")
    try:
        return br.call(
            LCMD_KEY_HOLD,
            id_lo=int(vk) & 0xFF,
            id_hi=1 if on else 0,
        )
    finally:
        br.close()


def key_hold_press(pid: int, vk: int, *, hold_ms: int = 60) -> LoginBridgeResult:
    """One background key press via the process-local IAT hook."""
    br = _open_bridge(pid)
    if br is None:
        return LoginBridgeResult(ok=False, cmd=LCMD_KEY_HOLD, error="login bridge not open")
    try:
        # mode bit0 = press (native holds then clears)
        return br.call(
            LCMD_KEY_HOLD,
            id_lo=int(vk) & 0xFF,
            id_hi=1,
            mode=1,
        )
    finally:
        br.close()


def ime_off(pid: int, *, on: bool = False) -> LoginBridgeResult:
    """Disable (or restore) the process IME from the game UI thread.

    A Chinese IME swallows keybd_event ASCII chars; this lets real key input
    reach the credential edit controls. on=True restores the IME.
    @author by ak
    """
    br = _open_bridge(pid)
    if br is None:
        return LoginBridgeResult(ok=False, cmd=LCMD_IME_OFF, error="login bridge not open")
    try:
        return br.call(LCMD_IME_OFF, id_lo=1 if on else 0, timeout_ms=2000)
    finally:
        br.close()


def inject_char(pid: int, ch: str) -> LoginBridgeResult:
    """Background char inject into the focused game input box.

    The bridge installs a GetKeyboardState inline hook, then posts a WM_KEYDOWN
    for the character's VK via 0x4BF9C0(input_this, ..., vk, ...) on the game UI
    thread so the game's key->char translation sees the key down. No SendInput,
    no focus steal. @author by ak
    """
    if not ch:
        return LoginBridgeResult(ok=False, cmd=LCMD_INJECT_CHAR, error="empty char")
    code = ord(ch[0])
    br = _open_bridge(pid)
    if br is None:
        return LoginBridgeResult(ok=False, cmd=LCMD_INJECT_CHAR, error="login bridge not open")
    try:
        return br.call(LCMD_INJECT_CHAR, id_lo=int(code) & 0xFFFF, timeout_ms=2000)
    finally:
        br.close()


def inject_text(pid: int, text: str, *, per_char_delay: float = 0.05) -> list[LoginBridgeResult]:
    """Background type a string into the focused game input box (GKS hook path)."""
    results: list[LoginBridgeResult] = []
    for ch in text:
        r = inject_char(pid, ch)
        results.append(r)
        if not r.ok:
            break
        if per_char_delay > 0:
            import time as _t

            _t.sleep(per_char_delay)
    return results


# --------------------------------------------------------------------------
# Injection
# --------------------------------------------------------------------------


def prepare_login_bridge_shm(pid: int, hwnd: int = 0, *, log: LogFn | None = None) -> bool:
    """
    Pre-create Global\\XajhLoginBridgeV2_<pid> before inject.

    Writes magic, idle status, hwnd, protocol version and struct size. Keeps the
    mapping handles alive so the injected DLL can open and inherit them.
    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(pid)
    if pid <= 0:
        return False
    name = f"Global\\XajhLoginBridgeV2_{pid}"
    h = kernel32.CreateFileMappingW(
        INVALID_HANDLE_VALUE, None, PAGE_READWRITE, 0, SHARED_SIZE, name
    )
    if not h:
        log(f"CreateFileMapping {name} failed err={ctypes.get_last_error()}")
        return False
    view = kernel32.MapViewOfFile(h, FILE_MAP_ALL_ACCESS, 0, 0, SHARED_SIZE)
    view_i = int(ctypes.cast(view, ctypes.c_void_p).value or 0) if view else 0
    if not view_i:
        kernel32.CloseHandle(wintypes.HANDLE(h))
        log(f"MapViewOfFile prepare failed err={ctypes.get_last_error()}")
        return False
    try:
        ctypes.memset(view_i, 0, SHARED_SIZE)
        ctypes.c_uint32.from_address(view_i + _OFF_MAGIC).value = LOGIN_BRIDGE_MAGIC
        ctypes.c_int32.from_address(view_i + _OFF_STATUS).value = LST_IDLE
        if hwnd:
            ctypes.c_uint32.from_address(view_i + _OFF_HWND).value = (
                int(hwnd) & 0xFFFFFFFF
            )
        ctypes.c_uint32.from_address(
            view_i + _OFF_PROTOCOL_VERSION
        ).value = LOGIN_BRIDGE_PROTOCOL_VERSION
        ctypes.c_uint32.from_address(view_i + _OFF_STRUCT_SIZE).value = SHARED_SIZE
        old = _PREPARED_SHM.pop(pid, None)
        if old:
            try:
                kernel32.UnmapViewOfFile(ctypes.c_void_p(old[1]))
                kernel32.CloseHandle(wintypes.HANDLE(old[0]))
            except Exception:
                pass
        _PREPARED_SHM[pid] = (int(h), view_i, name)
        log(f"prepare_login_shm ok name={name} hwnd=0x{int(hwnd):X}")
        return True
    except Exception as e:
        log(f"prepare_login_shm write failed: {e}")
        try:
            kernel32.UnmapViewOfFile(ctypes.c_void_p(view_i))
            kernel32.CloseHandle(wintypes.HANDLE(h))
        except Exception:
            pass
        return False


def inject_login_bridge(pid: int, *, hwnd: int = 0, log: LogFn | None = None) -> bool:
    """Inject the standalone login-stage DLL and wait until PING-able."""
    log = log or (lambda _m: None)
    pid = int(pid)
    dll = _NATIVE_DIR / DLL_NAME
    inj = _NATIVE_DIR / INJECTOR_NAME
    if not dll.is_file() or not inj.is_file():
        log(f"login bridge not built: need {DLL_NAME}+{INJECTOR_NAME} in native/bin")
        return False
    try:
        prepare_login_bridge_shm(pid, hwnd, log=log)
    except Exception as e:
        log(f"prepare_login_shm skip: {e}")
    dll_path = str(dll.resolve())
    inj_path = str(inj.resolve())
    log(f"login inject start pid={pid} dll={dll_path}")
    try:
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = int(subprocess.CREATE_NO_WINDOW)
        r = subprocess.run(
            [inj_path, str(pid), dll_path],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(dll.parent),
            creationflags=creationflags,
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        log(f"login inject exit={r.returncode} {out}")
        if r.returncode != 0:
            return False
        if not _pid_alive(pid):
            log("login inject ok but game process dead")
            return False
    except Exception as e:
        log(f"login inject failed: {e}")
        return False
    br = _open_bridge(pid)
    if br is None:
        return False
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            r = br.call(LCMD_PING, timeout_ms=1500)
            if r.ok:
                log(f"login bridge ping ok ret={r.ret} note={r.note!r}")
                return True
            time.sleep(0.3)
        return False
    finally:
        br.close()


# --------------------------------------------------------------------------
# High-level login actions
# --------------------------------------------------------------------------


def confirm_server_selection(
    session,
    *,
    hwnd: int = 0,
    fallback_enter: bool = True,
    settle_s: float = 0.20,
    log: LogFn | None = None,
) -> dict:
    """Confirm the server list by calling the native input processor (0xE90CC0).

    Product path: ui_input(dialog_ptr, WM_KEYDOWN, VK_RETURN, 1) which mirrors
    the verified real Enter. IDYES dialog command and UI_KEY are diagnostics
    only. Success = server list hidden and Win_Login shown.
    """
    import time as _time

    log = log or (lambda _m: None)
    out = {
        "ok": False,
        "stage": LoginStage.UNKNOWN.value,
        "method": "",
        "error": None,
        "rect": None,
    }
    probe = probe_login_stage(session, log=log)
    out["stage"] = probe.stage.value
    if probe.stage is not LoginStage.SERVER_SELECT or not probe.dialog_ptr:
        out["error"] = "server_select_not_shown"
        return out

    result = ui_input(int(session.pid), probe.dialog_ptr, WM_KEYDOWN, VK_RETURN, 1, 1)
    out["method"] = "ui_input_enter"
    if result.ok:
        _time.sleep(max(0.05, float(settle_s)))
        after = probe_login_stage(session, log=log)
        out["ok"] = after.stage is not LoginStage.SERVER_SELECT
        if not out["ok"]:
            out["error"] = "confirm_command_not_applied"
        return out

    out["error"] = result.error or result.note or "confirm_command_failed"

    # Diagnostic fallbacks (not the product path).
    if fallback_enter:
        command_result = ui_dialog_command(
            int(session.pid), int(probe.dialog_ptr), LOGIN_DIALOG_COMMAND_CONFIRM
        )
        if command_result.ok:
            out["method"] = "fallback_dialog_command"
            out["ok"] = True
            return out
        key_result = ui_key(int(session.pid), VK_RETURN, no_focus=True)
        if key_result.ok:
            out["method"] = "enter_fallback"
            out["ok"] = True
            return out
    return out


def _to_vk(ch: str) -> tuple[int, bool]:
    """Map one ASCII char to (VK, need_shift). None-safe caller checks.

    Letters/digits map directly; upper case letters require shift.
    @author by ak
    """
    o = ord(ch)
    if 0x41 <= o <= 0x5A:
        return o, True
    if 0x61 <= o <= 0x7A:
        return o & 0xDF, False
    if 0x30 <= o <= 0x39:
        return o, False
    simple = {
        " ": 0x20,
        ".": 0xBE,
        "-": 0xBD,
        "@": 0x32,
        "#": 0x33,
        "!": 0x31,
        "?": 0xBF,
        "/": 0xBF,
        ":": 0xBA,
        ";": 0xBA,
        "_": 0xBD,
        "+": 0xBB,
        "=": 0xBB,
        "*": 0x6A,
    }
    shifted = {
        "@": True,
        "#": True,
        "!": True,
        "?": True,
        ":": True,
        "_": True,
        "+": True,
        "*": False,
    }
    if ch in simple:
        return simple[ch], shifted.get(ch, False)
    return 0, False


def ascii_char_to_vk(ch: str) -> int | None:
    """Legacy helper: return the VK for an ASCII char, or None if unmapped.

    Shift state is not conveyed (use _to_vk for that). @author by ak
    """
    if len(ch) != 1:
        return None
    vk, _ = _to_vk(ch)
    return vk or None


def type_ascii(pid: int, text: str, *, per_key_delay: float = 0.05) -> list[LoginBridgeResult]:
    """Type ASCII text via KEY_HOLD per char (background, no focus steal).

    Upper case letters and shifted punctuation hold Shift first. Returns the
    per-key results for diagnostics. @author by ak
    """
    results: list[LoginBridgeResult] = []
    for ch in text:
        vk, need_shift = _to_vk(ch)
        if not vk:
            results.append(
                LoginBridgeResult(ok=False, cmd=LCMD_KEY_HOLD, error=f"unmapped char {ch!r}")
            )
            continue
        if need_shift:
            results.append(key_hold(pid, VK_SHIFT, on=True))
        results.append(key_hold_press(pid, vk))
        if need_shift:
            results.append(key_hold(pid, VK_SHIFT, on=False))
        if per_key_delay > 0:
            time.sleep(per_key_delay)
    return results


def find_credentials_dialog(session, *, log: LogFn | None = None) -> int:
    """Return the shown Win_Login dialog pointer, or 0 if not shown.

    Uses probe_login_stage (AUI query first, then verified cache). Never scans
    the whole process heap. @author by ak
    """
    probe = probe_login_stage(session, log=log)
    if probe.stage is LoginStage.CREDENTIALS and probe.dialog_ptr:
        return int(probe.dialog_ptr)
    return 0


def fill_credentials(
    session,
    account: str,
    password: str,
    *,
    per_key_delay: float = 0.05,
    settle_s: float = 0.20,
    log: LogFn | None = None,
) -> dict:
    """Focus Input_1, type account, Tab, type password on the credentials page.

    Returns {"ok", "stage", "error", "steps": [...]}. This is a diagnostic
    first pass: account focus relies on ui_input WM_LBUTTONDOWN at the Input_1
    rect when geometry is available, otherwise falls back to a direct
    ui_input WM_KEYDOWN Tab chain attempt. @author by ak
    """
    log = log or (lambda _m: None)
    steps: list[str] = []
    out = {"ok": False, "stage": LoginStage.UNKNOWN.value, "error": None, "steps": steps}
    dlg = find_credentials_dialog(session, log=log)
    if not dlg:
        out["error"] = "credentials_not_shown"
        out["stage"] = probe_login_stage(session, log=log).stage.value
        return out
    pid = int(session.pid)

    # 1) Focus the account edit box.
    focused = False
    try:
        from app.core.aui_click import get_aui_dlg_item_ptr, read_aui_ctrl_rect

        edit_ptr = get_aui_dlg_item_ptr(session, dlg, _CREDENTIAL_EDIT_NAMES[0], log=log)
        if edit_ptr:
            rect = read_aui_ctrl_rect(session, edit_ptr, name=_CREDENTIAL_EDIT_NAMES[0], log=log)
            if rect is not None and rect.ok and rect.w > 2 and rect.h > 2:
                cx, cy = rect.center
                r = ui_click(pid, int(cx), int(cy))
                steps.append(f"click Input_1 center=({cx},{cy}) ok={r.ok}")
                focused = r.ok
    except Exception as e:
        log(f"fill_credentials focus Input_1 error: {e}")
        steps.append(f"focus_error={e}")

    if not focused:
        # Tab-chain fallback: Tab into the first edit control.
        for _ in range(3):
            r = ui_input(pid, dlg, WM_KEYDOWN, VK_TAB, 1, 1)
            steps.append(f"tab keydown ok={r.ok}")
            time.sleep(0.05)
        focused = True

    # 2) Type account then Tab then password.
    if account:
        for res in type_ascii(pid, str(account), per_key_delay=per_key_delay):
            steps.append(f"account_key vk={res.cmd} ok={res.ok}")
    time.sleep(max(0.05, float(settle_s)))
    r = ui_input(pid, dlg, WM_KEYDOWN, VK_TAB, 1, 1)
    steps.append(f"tab_to_password ok={r.ok}")
    time.sleep(0.05)
    if password:
        for res in type_ascii(pid, str(password), per_key_delay=per_key_delay):
            steps.append(f"password_key vk={res.cmd} ok={res.ok}")

    after = probe_login_stage(session, log=log)
    out["stage"] = after.stage.value
    out["ok"] = after.stage is LoginStage.CREDENTIALS
    if not out["ok"]:
        out["error"] = "credentials_typing_state_unexpected"
    return out


def submit_login(session, *, settle_s: float = 0.30, log: LogFn | None = None) -> dict:
    """Submit the credentials page via the native input Enter path (0xE90CC0).

    Returns {"ok", "stage", "error", "method"}. Success means the credentials
    page is left (Win_Login hidden) — the next page is character select or an
    error dialog; error text capture is left to the caller probe. @author by ak
    """
    import time as _time

    log = log or (lambda _m: None)
    out = {"ok": False, "stage": LoginStage.UNKNOWN.value, "error": None, "method": ""}
    dlg = find_credentials_dialog(session, log=log)
    if not dlg:
        out["error"] = "credentials_not_shown"
        return out
    pid = int(session.pid)
    r = ui_input(pid, dlg, WM_KEYDOWN, VK_RETURN, 1, 1)
    out["method"] = "ui_input_enter"
    if r.ok:
        _time.sleep(max(0.05, float(settle_s)))
        after = probe_login_stage(session, log=log)
        out["stage"] = after.stage.value
        out["ok"] = after.stage is not LoginStage.CREDENTIALS
        if not out["ok"]:
            out["error"] = "submit_enter_not_applied"
        return out
    out["error"] = r.error or r.note or "submit_enter_failed"
    return out


# Shift VK for type_ascii.
VK_SHIFT = 0x10


class _UserActivityMonitor:
    """Detect real user keyboard/mouse CLICKS (not plain cursor motion).

    GetLastInputInfo advances its tick on pure mouse motion too, so a user who
    keeps moving the mouse would make ``idle_seconds`` stay < threshold forever
    (login waits) and then steal focus the moment they pause. We therefore only
    count a key press or a mouse-button press as real activity — plain cursor
    motion does NOT extend the idle timer.

    Implementation: a daemon thread polls GetAsyncKeyState for the mouse buttons
    plus any key; a transition to "pressed" updates ``_user_active_at``. Our own
    injected events are compensated via ``note_inject()``.

    @author by ak
    """

    _MARGIN_MS = 30  # injected events land within ~30ms of note_inject()
    _MOUSE_VKS = (0x01, 0x02, 0x04, 0x05, 0x06)  # L/M/R/X1/X2

    def __init__(self) -> None:
        self._baseline_tick = 0
        self._lock = threading.Lock()
        self._started = False
        self._user_active_at = 0.0  # monotonic seconds of last REAL user input
        self._poll_thread = None

    @staticmethod
    def _last_input_tick() -> int:
        try:
            class LASTINPUTINFO(_ctypes.Structure):
                _fields_ = [("cbSize", _ctypes.c_uint), ("dwTime", _ctypes.c_uint)]
            li = LASTINPUTINFO()
            li.cbSize = _ctypes.sizeof(LASTINPUTINFO)
            ok = _user32.GetLastInputInfo(_ctypes.byref(li))
            return int(li.dwTime) if ok else 0
        except Exception:
            return 0

    @staticmethod
    def _any_key_down() -> bool:
        """True when a mouse button or any key is currently pressed. @author by ak"""
        try:
            for vk in _UserActivityMonitor._MOUSE_VKS:
                if _user32.GetAsyncKeyState(vk) & 0x8000:
                    return True
            for vk in range(0x08, 0xFF):
                if _user32.GetAsyncKeyState(vk) & 0x8000:
                    return True
        except Exception:
            return False
        return False

    def _poll(self) -> None:
        last_down = False
        while self._started:
            try:
                down = self._any_key_down()
                if down and not last_down:
                    # rising edge = real press (incl. mouse click, not motion).
                    with self._lock:
                        self._user_active_at = time.monotonic()
                last_down = down
            except Exception:
                last_down = False
            time.sleep(0.03)

    def start(self) -> bool:
        """Record baseline and start the activity poller. @author by ak"""
        self._baseline_tick = self._last_input_tick()
        self._user_active_at = time.monotonic()
        self._started = True
        self._poll_thread = threading.Thread(target=self._poll, daemon=True)
        self._poll_thread.start()
        return True

    def note_inject(self) -> None:
        """Call immediately after injecting a key event; rebases the detector. @author by ak"""
        with self._lock:
            self._baseline_tick = self._last_input_tick()
            # our own keybd_event holds a key down briefly; clear the user-activity
            # stamp so the injected press is not mistaken for a user press.
            self._user_active_at = time.monotonic()

    def real_input_since_inject(self) -> bool:
        """True if a REAL user key/mouse press happened since the last inject. @author by ak"""
        with self._lock:
            act = self._user_active_at
            base = self._baseline_tick
        if not act:
            return False
        # baseline tick is the injection time; a user press must come after it.
        g = self._last_input_tick()
        if not g:
            return False
        return g > base + self._MARGIN_MS and (time.monotonic() - act) < 5.0

    def idle_seconds(self) -> float:
        """Seconds since the last REAL user key/mouse press (motion excluded). @author by ak"""
        with self._lock:
            act = self._user_active_at
        if not act:
            return 999.0
        return max(0.0, time.monotonic() - act)

    def stop(self) -> None:
        """Stop the activity poller. @author by ak"""
        self._started = False
        if self._poll_thread is not None:
            try:
                self._poll_thread.join(timeout=0.2)
            except Exception:
                pass
            self._poll_thread = None


def _idle_wait(mon: _UserActivityMonitor, idle_s: float, *, poll: float = 0.1,
               timeout_s: float = 0.0, stop_event=None) -> bool:
    """Throttle: wait until the user has been idle for idle_s consecutive.

    Polls ``mon.idle_seconds()`` until it stays at >= idle_s, or returns False
    once ``timeout_s`` (>0) elapses without reaching idle. True when idle was
    reached (possibly immediately). ``stop_event`` (threading.Event) aborts the
    wait early (returns False). This is the single gate every foreground action
    uses so focus is only stolen while the user is actually idle.
    @author by ak"""
    t0 = time.monotonic()
    while mon.idle_seconds() < idle_s:
        if stop_event is not None and stop_event.is_set():
            return False
        if timeout_s > 0 and time.monotonic() - t0 > timeout_s:
            return False
        time.sleep(poll)
    return True


def interactive_guard_type(
    pid: int,
    hwnd: int,
    text: str,
    *,
    per_char_delay: float = 0.12,
    idle_before_s: float = 1.5,
    buffer_s: float = 2.0,
    settle_s: float = 0.6,
    max_pause_s: float = 60.0,
    log: LogFn | None = None,
) -> dict:
    """Human-cooperative auto-typing into the game login box.

    Respects the user: real keyboard/mouse activity pauses the injection, and
    the game window must be foreground to type a char. Real user input is
    detected with GetLastInputInfo plus an injection-compensation baseline
    (``note_inject()`` after every injected key); any GILI advance beyond our
    last injection is a genuine user action. If the user raises another window,
    we wait ``buffer_s`` after they go idle, then pull the game back and
    resume — never stealing focus mid-action. Aborts after ``max_pause_s`` of
    continuous user activity instead of fighting them.

    Requires the production bridge (xajh_bridge, CMD_KEY_HOLD allow_softsend).
    Returns {"ok", "typed", "paused_s", "error"}.

    @author by ak
    """
    import ctypes as _ct
    import ctypes.wintypes as _wt

    log = log or (lambda _m: None)
    out = {"ok": False, "typed": 0, "paused_s": 0.0, "error": None}
    if not text:
        out["ok"] = True
        return out
    try:
        from app.core.win_capture import bring_to_foreground, is_foreground_hwnd
        from app.core.xajh_bridge import XajhBridge
    except Exception as e:  # pragma: no cover - import env
        out["error"] = f"import_failed: {e}"
        return out

    pid = int(pid)
    hwnd = int(hwnd or 0)
    if not hwnd or not _user32.IsWindow(_wt.HWND(hwnd)):
        out["error"] = "bad_hwnd"
        return out
    _wpid = _wt.DWORD(0)
    _user32.GetWindowThreadProcessId(_wt.HWND(hwnd), _ctypes.byref(_wpid))
    if int(_wpid.value or 0) != pid:
        out["error"] = "hwnd_not_game"
        return out

    mon = _UserActivityMonitor()
    if not mon.start():
        out["error"] = "activity_monitor_fail"
        return out
    try:
        # Phase 0: wait until the user is idle before touching focus.
        _idle_wait(mon, idle_before_s)
        if not bring_to_foreground(hwnd, log=log):
            out["error"] = "game_not_foreground"
            return out
        if settle_s > 0:
            time.sleep(settle_s)

        br = XajhBridge(pid, log=log)
        if not br.open(quiet=True):
            out["error"] = "prod bridge not open"
            return out
        try:
            paused_start = time.monotonic()
            sent = 0
            for ch in text:
                # Phase: ensure the game is foreground AND the user is idle.
                # If the user raised another window, wait buffer_s of user
                # inactivity before pulling the game back.
                wait_start = time.monotonic()
                while True:
                    if not is_foreground_hwnd(hwnd):
                        if mon.idle_seconds() >= buffer_s:
                            if time.monotonic() - wait_start > 3.0:
                                bring_to_foreground(hwnd, log=log)
                                time.sleep(0.15)
                                wait_start = time.monotonic()
                        if time.monotonic() - paused_start > max_pause_s:
                            out["error"] = "user_busy_timeout"
                            break
                        time.sleep(0.1)
                        continue
                    if mon.idle_seconds() >= buffer_s:
                        break
                    if time.monotonic() - paused_start > max_pause_s:
                        out["error"] = "user_busy_timeout"
                        break
                    time.sleep(0.1)
                if out["error"]:
                    break
                if not is_foreground_hwnd(hwnd):
                    out["error"] = "focus_guard_abort"
                    break

                vk, need_shift = _to_vk(ch)
                if not vk:
                    out["error"] = f"unmapped_char:{ch!r}"
                    break
                # Re-check once more right before injecting (real-input pause).
                if mon.real_input_since_inject():
                    wait_start = time.monotonic()
                    while mon.real_input_since_inject():
                        if time.monotonic() - paused_start > max_pause_s:
                            out["error"] = "user_busy_timeout"
                            break
                        time.sleep(0.1)
                    if out["error"]:
                        break
                    if not is_foreground_hwnd(hwnd):
                        out["error"] = "focus_guard_abort"
                        break
                if need_shift:
                    br.key_hold(VK_SHIFT, down=True, allow_softsend=True)
                    time.sleep(0.03)
                mon.note_inject()
                r = br.key_hold(vk, down=True, allow_softsend=True)
                time.sleep(0.05)
                br.key_hold(vk, down=False, allow_softsend=True)
                mon.note_inject()
                time.sleep(0.05)
                if need_shift:
                    br.key_hold(VK_SHIFT, down=False, allow_softsend=True)
                    mon.note_inject()
                    time.sleep(0.03)
                if not r.ok:
                    out["error"] = r.error or r.note or "key_send_failed"
                    break
                sent += 1
                if per_char_delay > 0:
                    time.sleep(per_char_delay)
            try:
                br.key_hold(0, down=False, allow_softsend=True, clear_all=True)
            except Exception:
                pass
            out["paused_s"] = round(time.monotonic() - paused_start, 2)
            out["typed"] = sent
            out["ok"] = sent == len(text) and not out["error"]
            return out
        finally:
            br.close()
    finally:
        mon.stop()


def foreground_guard_type(
    pid: int,
    hwnd: int,
    text: str,
    *,
    per_char_delay: float = 0.12,
    settle_s: float = 0.6,
    log: LogFn | None = None,
) -> dict:
    """Type ``text`` into the foregrounded game window using production-bridge
    SendInput key_hold, with a focus guard: every character is only sent while
    ``GetForegroundWindow() == hwnd``. If focus is lost at any point the typing
    aborts immediately, so characters can never leak into another window.

    Requires the production feature bridge (xajh_bridge, CMD_KEY_HOLD with
    allow_softsend=True / SendInput). Returns {"ok", "typed", "error"}.

    @author by ak
    """
    import ctypes as _ct
    import ctypes.wintypes as _wt

    log = log or (lambda _m: None)
    out = {"ok": False, "typed": 0, "error": None}
    if not text:
        out["ok"] = True
        return out
    try:
        from app.core.win_capture import bring_to_foreground, is_foreground_hwnd
        from app.core.xajh_bridge import XajhBridge
    except Exception as e:  # pragma: no cover - import env
        out["error"] = f"import_failed: {e}"
        return out

    user32 = _ct.WinDLL("user32", use_last_error=True)
    pid = int(pid)
    hwnd = int(hwnd or 0)
    if not hwnd or not user32.IsWindow(_wt.HWND(hwnd)):
        out["error"] = "bad_hwnd"
        return out
    _wpid = _wt.DWORD(0)
    user32.GetWindowThreadProcessId(_wt.HWND(hwnd), _ct.byref(_wpid))
    if int(_wpid.value or 0) != pid:
        out["error"] = "hwnd_not_game"
        return out

    # Focus the game window and require it to actually be foreground.
    if not bring_to_foreground(hwnd, log=log):
        out["error"] = "game_not_foreground"
        return out
    if not is_foreground_hwnd(hwnd):
        out["error"] = "focus_guard_rejected"
        return out
    if settle_s > 0:
        time.sleep(settle_s)
    if not is_foreground_hwnd(hwnd):
        out["error"] = "focus_lost_before_typing"
        return out

    br = XajhBridge(pid, log=log)
    if not br.open(quiet=True):
        out["error"] = "prod bridge not open"
        return out
    try:
        # Ensure foreground again (bridge open may not move focus).
        if not is_foreground_hwnd(hwnd) and not bring_to_foreground(hwnd, log=log):
            out["error"] = "focus_lost_open_bridge"
            return out
        sent = 0
        for ch in text:
            if not is_foreground_hwnd(hwnd):
                out["error"] = "focus_lost_abort"
                break
            vk, need_shift = _to_vk(ch)
            if not vk:
                out["error"] = f"unmapped_char:{ch!r}"
                break
            if need_shift:
                br.key_hold(VK_SHIFT, down=True, allow_softsend=True)
                time.sleep(0.03)
            r = br.key_hold(vk, down=True, allow_softsend=True)
            time.sleep(0.05)
            br.key_hold(vk, down=False, allow_softsend=True)
            time.sleep(0.05)
            if need_shift:
                br.key_hold(VK_SHIFT, down=False, allow_softsend=True)
                time.sleep(0.03)
            if not r.ok:
                out["error"] = r.error or r.note or "key_send_failed"
                break
            sent += 1
            if per_char_delay > 0:
                time.sleep(per_char_delay)
        # Release any dangling forced state defensively.
        try:
            br.key_hold(0, down=False, allow_softsend=True, clear_all=True)
        except Exception:
            pass
        out["typed"] = sent
        out["ok"] = sent == len(text) and not out["error"]
        return out
    finally:
        br.close()


def _get_clipboard_text() -> str:
    """Read current CF_UNICODETEXT clipboard content; '' when unavailable. @author by ak"""
    try:
        import ctypes as _c

        u32 = _c.WinDLL("user32", use_last_error=True)
        k32 = _c.WinDLL("kernel32", use_last_error=True)
        u32.OpenClipboard.argtypes = [wintypes.HWND]
        u32.OpenClipboard.restype = wintypes.BOOL
        u32.GetClipboardData.argtypes = [wintypes.UINT]
        u32.GetClipboardData.restype = wintypes.HANDLE
        u32.CloseClipboard.argtypes = []
        u32.CloseClipboard.restype = wintypes.BOOL
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalLock.restype = wintypes.LPVOID
        k32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        k32.GlobalSize.restype = _c.c_size_t
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.restype = wintypes.BOOL
        if not u32.OpenClipboard(0):
            return ""
        try:
            h = u32.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return ""
            p = k32.GlobalLock(h)
            if not p:
                return ""
            try:
                n = k32.GlobalSize(h)
                raw = _c.string_at(p, max(0, int(n)))
            finally:
                k32.GlobalUnlock(h)
            return raw.decode("utf-16-le", "ignore").split("\x00", 1)[0]
        finally:
            u32.CloseClipboard()
    except Exception:
        return ""


def _set_clipboard_text(text: str) -> bool:
    """Put UTF-16 text on the clipboard (CF_UNICODETEXT). @author by ak"""
    try:
        import ctypes as _c

        u32 = _c.WinDLL("user32", use_last_error=True)
        k32 = _c.WinDLL("kernel32", use_last_error=True)
        u32.OpenClipboard.argtypes = [wintypes.HWND]
        u32.OpenClipboard.restype = wintypes.BOOL
        u32.EmptyClipboard.argtypes = []
        u32.EmptyClipboard.restype = wintypes.BOOL
        u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u32.SetClipboardData.restype = wintypes.HANDLE
        u32.CloseClipboard.argtypes = []
        u32.CloseClipboard.restype = wintypes.BOOL
        k32.GlobalAlloc.argtypes = [wintypes.UINT, _c.c_size_t]
        k32.GlobalAlloc.restype = wintypes.HGLOBAL
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalLock.restype = wintypes.LPVOID
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.restype = wintypes.BOOL
        k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        k32.GlobalFree.restype = wintypes.HGLOBAL
        if not u32.OpenClipboard(0):
            return False
        try:
            if not u32.EmptyClipboard():
                return False
            data = (str(text) + "\x00").encode("utf-16-le")
            h = k32.GlobalAlloc(0x0042, len(data))
            if not h:
                return False
            p = k32.GlobalLock(h)
            if not p:
                k32.GlobalFree(h)
                return False
            _c.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            if not u32.SetClipboardData(13, h):  # CF_UNICODETEXT
                k32.GlobalFree(h)
                return False
            return True
        finally:
            u32.CloseClipboard()
    except Exception:
        return False


def _set_clipboard_verified(text: str, *, retries: int = 3, log: LogFn | None = None) -> bool:
    """Write ``text`` to the clipboard and confirm it reads back exactly.

    Between the set and the paste the clipboard can be stolen by another app
    (a notification / IM / clipboard manager), so the account/password paste
    must verify before Ctrl+V. Returns True only when the read-back equals
    ``text`` (or is a prefix when the game only accepts part — we require exact).

    @author by ak
    """
    log = log or (lambda _m: None)
    want = str(text or "")
    for i in range(max(1, int(retries))):
        if not _set_clipboard_text(want):
            log(f"clipboard set failed attempt {i + 1}")
            time.sleep(0.2)
            continue
        # Clipboard viewers/managers can briefly observe the empty state after
        # EmptyClipboard and before the newly published format is available.
        # Poll briefly so that transient propagation is not treated as a
        # failed write.
        got = ""
        for _ in range(5):
            got = _get_clipboard_text()
            if got == want:
                return True
            time.sleep(0.04)
        log(f"clipboard verify mismatch attempt {i + 1}: got {got!r} want {want!r}")
        time.sleep(0.25)
    return False


def _fg_game_no_alt(hwnd: int, *, retries: int = 4, settle_s: float = 0.6) -> bool:
    """Bring the game window to foreground WITHOUT simulating the Alt key.

    The Alt-key unlock trick used by win_capture.bring_to_foreground fires the
    system hotkey and can pop unrelated windows (e.g. a tray/IM window), which
    is unacceptable during login. Here we only AttachThreadInput + SetForeground
    (plus restore-if-minimized), which is enough for keybd_event on the game.
    @author by ak
    """
    u32 = _user32
    h = wintypes.HWND(hwnd)
    if u32.IsIconic(h):
        u32.ShowWindow(h, 9)
        time.sleep(0.2)
    for _ in range(max(1, int(retries))):
        fg = u32.GetForegroundWindow()
        fg_tid = u32.GetWindowThreadProcessId(wintypes.HWND(fg), None)
        tgt_tid = u32.GetWindowThreadProcessId(h, None)
        attached = False
        if fg_tid != tgt_tid:
            attached = bool(u32.AttachThreadInput(fg_tid, tgt_tid, True))
        u32.BringWindowToTop(h)
        u32.SetForegroundWindow(h)
        try:
            u32.SetActiveWindow(h)
        except Exception:
            pass
        time.sleep(max(0.2, float(settle_s)))
        if attached:
            u32.AttachThreadInput(fg_tid, tgt_tid, False)
        if u32.GetForegroundWindow() == hwnd:
            return True
    return u32.GetForegroundWindow() == hwnd


def _native_chord(*vks, hold_s: float = 0.06) -> None:
    """Press a key chord via foreground keybd_event (no bridge needed).

    Native keybd_event is the only input the login UI accepts (verified):
    SendInput/PostMessage/GAKS hooks are ignored by the game's element engine.
    @author by ak
    """
    for vk in vks:
        _user32.keybd_event(int(vk), 0, 0, 0)  # down
        time.sleep(0.03)
    time.sleep(hold_s)
    for vk in reversed(vks):
        _user32.keybd_event(int(vk), 0, 2, 0)  # up (KEYEVENTF_KEYUP)
        time.sleep(0.03)


def paste_fill(
    pid: int,
    hwnd: int,
    text: str,
    *,
    focus_xy: tuple[int, int] | None = None,
    select_all: bool = True,
    idle_before_s: float = 1.5,
    max_wait_s: float = 60.0,
    log: LogFn | None = None,
    on_before_click: Callable[[int, int, int, bool], None] | None = None,
    stop_event=None,
) -> dict:
    """Fill one login field by clipboard paste (Ctrl+A then Ctrl+V, foreground).

    Focuses the edit box via the login bridge background click, selects all
    (clears the remembered old value), and pastes ``text`` from the clipboard.
    Key chords are sent with native keybd_event — no production bridge needed,
    and this bypasses the Chinese IME (Ctrl+V is the game's own path).

    Human-cooperative: real keyboard/mouse activity (detected with
    GetLastInputInfo, injection-compensated via _UserActivityMonitor) defers
    focus steal until the user goes idle for ``idle_before_s``; if they never
    settle within ``max_wait_s`` the call aborts instead of fighting them.
    ``stop_event`` (threading.Event) aborts any pending wait/click promptly,
    returning {"ok": False, "error": "cancelled"}.

    Returns {"ok", "error", "waited_s"}.

    @author by ak
    """
    import ctypes as _ct
    import ctypes.wintypes as _wt
    log = log or (lambda _m: None)
    out = {"ok": False, "error": None, "waited_s": 0.0}
    pid = int(pid)
    hwnd = int(hwnd or 0)
    if not hwnd or not _user32.IsWindow(_wt.HWND(hwnd)):
        out["error"] = "bad_hwnd"
        return out
    _wpid = _wt.DWORD(0)
    _user32.GetWindowThreadProcessId(_wt.HWND(hwnd), _ctypes.byref(_wpid))
    if int(_wpid.value or 0) != pid:
        out["error"] = "hwnd_not_game"
        return out

    mon = _UserActivityMonitor()
    mon.start()

    def _cancelled():
        try:
            return bool(stop_event is not None and stop_event.is_set())
        except Exception:
            return False

    try:
        # Phase 0: wait until the user is idle before touching focus.
        t0 = time.monotonic()
        while mon.idle_seconds() < idle_before_s:
            if _cancelled():
                out["error"] = "cancelled"
                return out
            if time.monotonic() - t0 > float(max_wait_s):
                out["error"] = "user_busy_timeout"
                return out
            time.sleep(0.1)
        if not _fg_game_no_alt(hwnd):
            out["error"] = "game_not_foreground"
            return out
        if focus_xy:
            if on_before_click is not None:
                try:
                    on_before_click(int(hwnd), int(focus_xy[0]), int(focus_xy[1]), False)
                except Exception:
                    pass
            # ui_click 的 native 实现是 PostMessageW 异步投递，返回 ok 只代表消息
            # 已进游戏消息队列，不代表账号框焦点已落定。补一次点击并短等待确保
            # 最后一次点击确实生效（300ms × 2）。
            for _attempt in range(2):
                if _cancelled():
                    out["error"] = "cancelled"
                    return out
                r = ui_click(pid, int(focus_xy[0]), int(focus_xy[1]))
                mon.note_inject()  # 注入的点击不当作真实用户输入
                if not r.ok:
                    out["error"] = r.note or "click_focus_failed"
                    return out
                time.sleep(0.3)
            time.sleep(0.5)  # 点击后等焦点/输入框就绪
        # Phase: if the user becomes active again while we type, wait them out.
        t0 = time.monotonic()
        while mon.real_input_since_inject() or mon.idle_seconds() < 0.4:
            if _cancelled():
                out["error"] = "cancelled"
                return out
            if time.monotonic() - t0 > float(max_wait_s):
                out["error"] = "user_busy_timeout"
                return out
            time.sleep(0.1)
        # Write the clipboard and VERIFY it reads back exactly, so Ctrl+V never
        # pastes text that was overwritten by another app while we waited.
        if not _set_clipboard_verified(str(text), log=log):
            out["error"] = "clipboard_verify_failed"
            return out
        if select_all:
            _native_chord(0x11, 0x41)  # Ctrl+A
            mon.note_inject()
            time.sleep(0.3)
        _native_chord(0x11, 0x56)  # Ctrl+V
        mon.note_inject()
        time.sleep(0.6)
        out["waited_s"] = round(mon.idle_seconds(), 2)
        out["ok"] = True
        return out
    finally:
        mon.stop()


def auto_login_flow(
    session,
    *,
    account: str,
    password: str,
    main_hwnd: int = 0,
    server_confirm: bool = True,
    submit_timeout_s: float = 30.0,
    poll_s: float = 2.0,
    start_timeout_s: float = 20.0,
    server_confirm_settle_s: float = 2.5,
    log: LogFn | None = None,
    on_before_click: Callable[[int, int, int, bool], None] | None = None,
    stop_event=None,
) -> dict:
    """Drive the full login: server-select confirm -> account/password -> submit
    -> poll until character select / in-world (success) or stay on credentials
    (login error) or timeout (network). Returns a structured result.

    Product path (all verified 2026-08):
      - server confirm: native Enter (foreground keybd_event), not 0xE90CC0
      - account/password: clipboard paste (Ctrl+A + Ctrl+V) via production
        bridge SendInput, which bypasses the Chinese IME
      - submit: foreground Enter
      - outcome: probe_login_stage each poll; leaving credentials = success.

    Returns {"ok", "stage", "error", "method", "polls": [...]}.

    @author by ak
    """
    import ctypes as _ct
    import ctypes.wintypes as _wt

    log = log or (lambda _m: None)
    out = {"ok": False, "stage": LoginStage.UNKNOWN.value, "error": None,
           "method": "", "polls": []}
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        out["error"] = "no_pid"
        return out
    u32 = _ct.WinDLL("user32", use_last_error=True)

    def _fg(hwnd):
        fg0 = u32.GetForegroundWindow()
        fg_tid = u32.GetWindowThreadProcessId(_wt.HWND(fg0), None)
        tgt_tid = u32.GetWindowThreadProcessId(_wt.HWND(hwnd), None)
        if fg_tid != tgt_tid:
            u32.AttachThreadInput(fg_tid, tgt_tid, True)
        u32.BringWindowToTop(_wt.HWND(hwnd))
        u32.SetForegroundWindow(_wt.HWND(hwnd))
        time.sleep(0.6)
        if fg_tid != tgt_tid:
            u32.AttachThreadInput(fg_tid, tgt_tid, False)
        return u32.GetForegroundWindow() == hwnd

    def _probe():
        return probe_login_stage(session, log=log)

    def _stopped():
        try:
            return bool(stop_event is not None and stop_event.is_set())
        except Exception:
            return False

    def _stop_out():
        return {"ok": False, "stage": LoginStage.UNKNOWN.value,
                "error": "cancelled", "method": "cancelled", "polls": out.get("polls", [])}

    hwnd = int(main_hwnd or 0)
    if not hwnd:
        try:
            from app.core.inject_gate import find_main_hwnd_for_pid

            hwnd = int(find_main_hwnd_for_pid(pid)[0] or 0)
        except Exception:
            hwnd = 0
    if not hwnd or not u32.IsWindow(_wt.HWND(hwnd)):
        out["error"] = "no_game_hwnd"
        return out
    # Ensure the login bridge is injected (idempotent) — background clicks below
    # (ui_click / ui_dblclick) go through it. Callers may skip injecting.
    if not inject_login_bridge(pid, hwnd=hwnd, log=log):
        out["error"] = "login_bridge_inject_failed"
        return out
    # show visible (login window may be parked off-screen; do NOT resize)
    u32.ShowWindow(_wt.HWND(hwnd), 9)
    time.sleep(0.3)

    # Verified coordinates are expressed in the 1600x900 logical client space;
    # the login bridge posts mouse messages whose coords are interpreted by the
    # game in that same logical space. Map to the live client size with the
    # unified ClientCoordMapper (aspect-aware: proportional at 16:9, letterbox
    # fit otherwise), shared with the character-select clicks.
    from app.core.client_coord import ClientCoordMapper

    mapper = ClientCoordMapper.for_hwnd(hwnd)
    _cw, _ch = mapper.to_w, mapper.to_h
    _sel_xy = mapper.scale_point((841, 804))   # 选区页确认按钮
    _acc_xy = mapper.scale_point((1000, 438))
    _pwd_xy = mapper.scale_point((1009, 468))
    from app.core.client_coord import client_size_diag

    diag = client_size_diag(hwnd)
    log(f"login coords client {_cw}x{_ch} aspect_mismatch={mapper.aspect_mismatch:.4f} "
        f"dpi={diag.get('dpi')} logical={diag.get('logical')} "
        f"sel={_sel_xy} acc={_acc_xy} pwd={_pwd_xy}")

    stage = _probe()
    out["stage"] = stage.stage.value

    # The game window may be visible before the login UI manager has been
    # created (CECLoginUIMan only exists once the login page is mounted), so
    # an UNKNOWN first probe is expected on a fresh launch. Wait for the login
    # page to appear before deciding the start stage.
    if stage.stage is LoginStage.UNKNOWN:
        deadline = time.monotonic() + max(5.0, float(start_timeout_s))
        while time.monotonic() < deadline:
            if _stopped():
                return _stop_out()
            time.sleep(max(0.5, float(poll_s)))
            stage = _probe()
            out["stage"] = stage.stage.value
            if stage.stage in (LoginStage.SERVER_SELECT, LoginStage.CREDENTIALS):
                break
        if stage.stage is LoginStage.UNKNOWN:
            out["error"] = f"login_page_not_ready; now={stage.stage.value} ({stage.error or 'no_manager'})"
            return out

    # 1) Server-select confirm. Verified product path: a BACKGROUND CLICK on the
    #    confirm button (ui_click posts WM_MOUSEMOVE/DOWN/UP through the login
    #    bridge; coordinates are in the 1600x900 LOGICAL client space). The
    #    native Enter (ui_input) is NOT the confirm path here — the user-facing
    #    confirm is a click. Retry a few times with a settle so the page switch
    #    is not lost to timing.
    if stage.stage is LoginStage.SERVER_SELECT:
        if not server_confirm:
            out["error"] = "server_select_aborted"
            return out
        out["method"] = "server_bg_click"
        clicked = 0
        confirm_deadline = time.monotonic() + 20.0
        while time.monotonic() < confirm_deadline:
            if _stopped():
                return _stop_out()
            if on_before_click is not None:
                try:
                    on_before_click(int(hwnd), _sel_xy[0], _sel_xy[1], False)
                except Exception:
                    pass
            r = ui_click(pid, _sel_xy[0], _sel_xy[1])
            if r.ok:
                clicked += 1
            else:
                # Bridge not responding -> re-inject and retry.
                log(f"server confirm ui_click failed: {r.error or r.note or '?'}")
                inject_login_bridge(pid, hwnd=hwnd, log=log)
                time.sleep(0.6)
                continue
            # Give the game time to switch pages; poll cache-safe.
            _LOGIN_DIALOG_CACHE.clear()
            settle = time.monotonic() + 2.5
            while time.monotonic() < settle:
                if _stopped():
                    return _stop_out()
                stage = _probe()
                out["stage"] = stage.stage.value
                if stage.stage in (LoginStage.CREDENTIALS,
                                   LoginStage.CHARACTER_SELECT,
                                   LoginStage.IN_WORLD):
                    break
                time.sleep(max(0.3, float(poll_s) * 0.6))
            if stage.stage in (LoginStage.CREDENTIALS,
                               LoginStage.CHARACTER_SELECT,
                               LoginStage.IN_WORLD):
                break
            # Still on server-select: click again (page may still be loading).
            time.sleep(max(0.5, float(poll_s)))
        if stage.stage not in (LoginStage.CREDENTIALS, LoginStage.CHARACTER_SELECT,
                               LoginStage.IN_WORLD):
            out["error"] = (f"server_confirm_failed; now={stage.stage.value} "
                            f"clicks={clicked}")
            return out
    elif stage.stage is not LoginStage.CREDENTIALS:
        out["error"] = f"unexpected_start_stage; now={stage.stage.value}"
        return out

    # 1.5) 选区页确认关闭后稳定几秒再填账密：刚切到账密页时输入框可能尚未
    #      完全渲染，立即抢焦点填账号会因 UI 未就绪而失败/乱填。等
    #      server_confirm_settle_s 秒（期间可被 stop_event 中止）。
    if stage.stage is LoginStage.CREDENTIALS and float(server_confirm_settle_s) > 0:
        settle_deadline = time.monotonic() + max(0.0, float(server_confirm_settle_s))
        while time.monotonic() < settle_deadline:
            if _stopped():
                return _stop_out()
            time.sleep(0.25)
        log(f"server-select closed; settled {server_confirm_settle_s:.1f}s for credentials UI")
        stage = _probe()
        out["stage"] = stage.stage.value

    # 2) Fill account + password (paste, foreground). Only when on credentials —
    #    the launcher may remember the last login and land directly on char-select.
    #    paste_fill waits for the user to be idle (idle_before_s) before stealing
    #    focus, so do NOT pre-steal focus here.
    if stage.stage is LoginStage.CREDENTIALS:
        r1 = paste_fill(pid, hwnd, str(account), focus_xy=_acc_xy, log=log,
                        on_before_click=on_before_click, stop_event=stop_event)
        if _stopped():
            return _stop_out()
        if not r1.get("ok"):
            out["error"] = f"account_fill_failed: {r1.get('error')}"
            return out
        time.sleep(0.2)
        # Tab to password (password box default-focused; Tab keeps in order)
        _native_chord(VK_TAB)
        time.sleep(0.3)
        r2 = paste_fill(pid, hwnd, str(password), focus_xy=_pwd_xy, select_all=True, log=log,
                        on_before_click=on_before_click, stop_event=stop_event)
        if _stopped():
            return _stop_out()
        if not r2.get("ok"):
            out["error"] = f"password_fill_failed: {r2.get('error')}"
            return out
        time.sleep(0.3)

        # 3) Submit (foreground Enter). Human-cooperative: wait for user idle
        #    before stealing focus and pressing Enter.
        sub_mon = _UserActivityMonitor()
        sub_mon.start()
        try:
            _submit_quiet_s = max(20.0, float(submit_timeout_s))
            if not _idle_wait(sub_mon, 1.0, timeout_s=_submit_quiet_s,
                              stop_event=stop_event):
                out["error"] = "cancelled" if _stopped() else "user_busy_before_submit"
                return out
            if _stopped():
                return _stop_out()
            if not _fg_game_no_alt(hwnd):
                out["error"] = "no_focus_before_submit"
                return out
            if not _idle_wait(sub_mon, 0.4, timeout_s=_submit_quiet_s,
                              stop_event=stop_event):
                out["error"] = "cancelled" if _stopped() else "user_busy_before_submit"
                return out
            if _stopped():
                return _stop_out()
            u32.keybd_event(0x0D, 0, 0, 0)
            time.sleep(0.05)
            u32.keybd_event(0x0D, 0, 2, 0)
            sub_mon.note_inject()
        finally:
            sub_mon.stop()
        # 提交后等待世界加载：可中断的 sleep，便于停止登陆立即生效。
        for _ in range(8):
            if _stopped():
                return _stop_out()
            time.sleep(0.5)

    # 4) Poll for outcome.
    deadline = time.monotonic() + max(5.0, float(submit_timeout_s))
    final_stage = LoginStage.UNKNOWN
    while time.monotonic() < deadline:
        if _stopped():
            return _stop_out()
        p = _probe()
        final_stage = p.stage
        out["polls"].append({
            "t": round(time.monotonic() - (deadline - submit_timeout_s), 1),
            "stage": final_stage.value,
            "dialog": p.dialog_name,
        })
        if final_stage in (LoginStage.CHARACTER_SELECT, LoginStage.IN_WORLD):
            out["stage"] = final_stage.value
            out["ok"] = True
            out["method"] = "login_success"
            return out
        if final_stage is LoginStage.CREDENTIALS:
            # still on the login page -> keep waiting (network may be slow).
            time.sleep(max(0.2, float(poll_s)))
            continue
        if final_stage is LoginStage.UNKNOWN:
            # transient: page switch / loading in progress -> keep polling
            # instead of treating it as a failure.
            time.sleep(max(0.2, float(poll_s)))
            continue
        # left cred page but landed on an unexpected known stage.
        out["stage"] = final_stage.value
        out["error"] = f"login_left_credentials_to_{final_stage.value}"
        return out
    out["stage"] = final_stage.value
    out["error"] = "login_timeout_or_error" if final_stage is LoginStage.CREDENTIALS else "login_stuck"
    return out


def enter_world_from_char_select(
    session,
    *,
    main_hwnd: int = 0,
    slot: int = 1,
    role_click: tuple[int, int] = (501, 169),
    role_pitch: int = 141,
    poll_s: float = 2.0,
    poll_timeout_s: float = 20.0,
    log: LogFn | None = None,
    on_before_click: Callable[[int, int, int, bool], None] | None = None,
    stop_event=None,
) -> dict:
    """Enter the world from the character-select page.

    Verified 2026-08-14: role cards are entered by a background STANDARD
    double-click (see ui_dblclick). ``role_click`` is the slot-1 card center in
    CLIENT coordinates on the 1600x900 logical client (region-picker coords
    minus 30px title bar, e.g. picker (501,199) -> client (501,169)); cards
    stack vertically with ``role_pitch`` (141px on the 1600x900 reference).
    Coordinates are converted to the live client size via ClientCoordMapper
    (mature path shared with the server-confirm / credentials clicks), so clicks
    land precisely at any resolution. ``on_before_click``, when provided, is
    called with (hwnd, live client x, live client y) right before each
    double-click (e.g. a click-glow overlay for verifying the target).

    If the account has no character, double-clicks leave the game on
    character-select after attempts -> "no_character".

    @author by ak
    """
    import ctypes as _ct
    import ctypes.wintypes as _wt

    log = log or (lambda _m: None)
    out = {"ok": False, "stage": LoginStage.UNKNOWN.value, "error": None}
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        out["error"] = "no_pid"
        return out
    hwnd = int(main_hwnd or 0)
    if not hwnd:
        try:
            from app.core.inject_gate import find_main_hwnd_for_pid

            hwnd = int(find_main_hwnd_for_pid(pid)[0] or 0)
        except Exception:
            hwnd = 0
    if not hwnd:
        out["error"] = "no_game_hwnd"
        return out

    u32 = _ct.WinDLL("user32", use_last_error=True)
    u32.ShowWindow(_wt.HWND(hwnd), 9)
    time.sleep(0.3)

    try:
        slot = max(1, min(3, int(slot or 1)))

        # Convert the slot-N card center from the 1600x900 reference space to
        # the live client size via the unified ClientCoordMapper (same model as
        # the server-confirm / credentials clicks; region-picker recorded the
        # card pitch as 141 on the reference). ``role_click`` is the slot-1
        # center.
        from app.core.client_coord import ClientCoordMapper, client_size_diag

        mapper = ClientCoordMapper.for_hwnd(hwnd)
        cx, cy = mapper.scale_point(role_click)
        cy = cy + (slot - 1) * int(role_pitch * mapper.scale)
        # Prefer the game's own card centre (Rdo_CharN) read from memory — it is
        # the dead-centre click target at any DPI/resolution; fall back to the
        # reference-derived coords when the dialog is not readable. Right after
        # login the cards animate in, so wait for the centre to stabilise first.
        try:
            cc = _stable_card_center(pid, slot, log=log)
            if cc:
                cx, cy = cc
        except Exception:  # pragma: no cover
            pass
        diag = client_size_diag(hwnd)
        log(f"enter_world slot={slot} client {mapper.to_w}x{mapper.to_h} "
            f"dpi={diag.get('dpi')} logical={diag.get('logical')} "
            f"dblclick ({cx},{cy})")

        out["dblclick_xy"] = (int(cx), int(cy))

        # Enter the world by a background single-click (focus) then double-click
        # on the card (verified path). Re-read the target card centre before each
        # attempt so a layout shift cannot aim the click at the wrong slot.
        attempts = 3
        for attempt in range(attempts):
            if stop_event is not None and stop_event.is_set():
                out["error"] = "cancelled"
                return out
            try:
                fresh = char_card_center(pid, slot, log=log)
                if fresh:
                    cx, cy = fresh
            except Exception:  # pragma: no cover
                pass
            out["dblclick_xy"] = (int(cx), int(cy))
            if on_before_click is not None:
                try:
                    on_before_click(int(hwnd), int(cx), int(cy), True)
                except Exception:
                    pass
            if not _click_card_enter(pid, hwnd, cx, cy, slot=slot, log=log):
                out["error"] = "role_card_dblclick_failed"
                return out
            time.sleep(4)

            deadline = time.monotonic() + max(5.0, float(poll_timeout_s))
            entered = False
            while time.monotonic() < deadline:
                if stop_event is not None and stop_event.is_set():
                    out["error"] = "cancelled"
                    return out
                p = probe_login_stage(session, log=log)
                out["stage"] = p.stage.value
                if p.stage in (LoginStage.IN_WORLD, LoginStage.ENTERING_WORLD):
                    entered = True
                    out["ok"] = True
                    out["error"] = None
                    return out
                if p.stage is not LoginStage.CHARACTER_SELECT:
                    break
                time.sleep(max(0.2, float(poll_s)))
            if entered:
                return out
            # still on char-select -> likely no role at this slot; retry
            log(f"enter_world attempt {attempt + 1} still on char-select "
                f"(dblclick {cx},{cy})")
            time.sleep(1.0)

        out["stage"] = LoginStage.CHARACTER_SELECT.value
        out["error"] = "no_character"
        return out
    finally:
        pass


# ---------------------------------------------------------------------------
# Launcher-stage helpers (login orchestration support).
# ---------------------------------------------------------------------------


def _child_buttons(parent_hwnd: int) -> list[int]:
    """Return child Button hwnds of parent_hwnd. @author by ak"""
    import ctypes as _ct
    import ctypes.wintypes as _wt

    u32 = _ct.WinDLL("user32", use_last_error=True)
    out: list[int] = []

    @_ct.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    def _b(h, _lp):
        if _win_class(int(h)) == "Button":
            out.append(int(h))
        return True

    u32.EnumChildWindows(_wt.HWND(parent_hwnd), _b, 0)
    return out


def _launcher_msgboxes(
    launcher_pid: int,
) -> list[dict]:
    """List visible #32770 message boxes owned by the launcher, with button
    texts (so callers can distinguish update vs error dialogs). @author by ak"""
    import ctypes as _ct
    import ctypes.wintypes as _wt

    u32 = _ct.WinDLL("user32", use_last_error=True)
    out: list[dict] = []

    @_ct.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    def _cb(hwnd, _lp):
        p = _wt.DWORD(0)
        u32.GetWindowThreadProcessId(_wt.HWND(hwnd), _ct.byref(p))
        if int(p.value) != int(launcher_pid):
            return True
        if _win_class(int(hwnd)) != "#32770":
            return True
        if not u32.IsWindowVisible(_wt.HWND(hwnd)):
            return True
        title = _win_text(int(hwnd))
        btns: list[str] = []

        @_ct.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
        def _b(h, _lp):
            if _win_class(int(h)) == "Button":
                btns.append(_win_text(int(h)))
            return True

        u32.EnumChildWindows(_wt.HWND(hwnd), _b, 0)
        out.append({"hwnd": int(hwnd), "title": title, "buttons": btns})
        return True

    u32.EnumWindows(_cb, 0)
    return out


def dismiss_launcher_update_dialog(
    launcher_pid: int,
    *,
    prefer_no: bool = True,
    timeout_s: float = 5.0,
    log: LogFn | None = None,
) -> dict:
    """Dismiss the launcher's "update?" message box by choosing 否(&N).

    The 快乐江湖 launcher pops a standard #32770 dialog asking whether to auto
    update the game client. We must NOT auto-update (it would rewrite files the
    injected bridge depends on), so the desired action is the 否 button
    (or Alt+N). If it never appears within the timeout this returns
    {"found": False} so the caller can continue (no update that run).

    Uses PostMessage click (background, no focus steal). The 否 button is found
    by scanning the dialog's child buttons for the '&N' accelerator.

    Returns {"found", "clicked", "btn_hwnd", "error"}.

    @author by ak
    """
    import ctypes as _ct
    import ctypes.wintypes as _wt

    log = log or (lambda _m: None)
    u32 = _ct.WinDLL("user32", use_last_error=True)
    k32 = _ct.WinDLL("kernel32", use_last_error=True)

    def title_of(h):
        n = u32.GetWindowTextLengthW(_wt.HWND(h))
        buf = _ct.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(_wt.HWND(h), buf, n + 1)
        return buf.value

    def class_of(h):
        buf = _ct.create_unicode_buffer(128)
        u32.GetClassNameW(_wt.HWND(h), buf, 128)
        return buf.value

    tops: list[int] = []

    @_ct.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    def _cb(hwnd, _lp):
        p = _wt.DWORD(0)
        u32.GetWindowThreadProcessId(_wt.HWND(hwnd), _ct.byref(p))
        if int(p.value) == int(launcher_pid):
            tops.append(int(hwnd))
        return True

    deadline = time.monotonic() + max(0.5, float(timeout_s))
    while time.monotonic() < deadline:
        tops.clear()
        u32.EnumWindows(_cb, 0)
        for h in tops:
            if class_of(h) != "#32770":
                continue
            # The update prompt dialog — find the 否(&N) / 是(&Y) buttons.
            no_btn = 0
            yes_btn = 0

            @_ct.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
            def _b(hwnd, _lp):
                nonlocal no_btn, yes_btn
                if class_of(hwnd) != "Button":
                    return True
                t = title_of(hwnd)
                if "&N" in t:
                    no_btn = int(hwnd)
                if "&Y" in t:
                    yes_btn = int(hwnd)
                return True

            u32.EnumChildWindows(_wt.HWND(h), _b, 0)
            if not (no_btn or yes_btn):
                continue
            target = no_btn if prefer_no and no_btn else (yes_btn or no_btn)
            rc = _wt.RECT()
            u32.GetClientRect(_wt.HWND(target), _ct.byref(rc))
            cx = int(rc.right / 2)
            cy = int(rc.bottom / 2)
            lp = (cy << 16) | (cx & 0xFFFF)
            u32.PostMessageW(_wt.HWND(target), 0x0201, 1, lp)  # WM_LBUTTONDOWN
            time.sleep(0.03)
            u32.PostMessageW(_wt.HWND(target), 0x0202, 0, lp)  # WM_LBUTTONUP
            log(f"launcher update dialog dismissed: {'否' if target == no_btn else '是'} "
                f"btn=0x{target:X} dlg=0x{h:X}")
            return {"found": True, "clicked": True,
                    "btn_hwnd": int(target), "error": None}
        time.sleep(0.3)
    return {"found": False, "clicked": False, "btn_hwnd": 0, "error": None}


def scale_client_coord(
    xy: tuple[int, int],
    from_size: tuple[int, int],
    to_size: tuple[int, int],
) -> tuple[int, int]:
    """One-shot client-coordinate scaling via the unified ClientCoordMapper.

    Handles both the 16:9 proportional case and the letterbox (non-16:9) case;
    see app.core.client_coord for the model. Kept for backward compatibility.

    @author by ak
    """
    from app.core.client_coord import ClientCoordMapper

    return ClientCoordMapper(from_size, to_size).scale_point(xy)


def client_aspect_mismatch(
    client_size: tuple[int, int],
    *,
    target_aspect: float = 16.0 / 9.0,
    log: LogFn | None = None,
) -> float:
    """Return the aspect-ratio mismatch for a client size, 0.0 when acceptable.

    The verified click coordinates assume a 16:9 client (1600x900). When the
    game window is not 16:9 the caller should either reset the window or fall
    back to the aspect-aware mapping (ClientCoordMapper); this helper reports
    how far off the ratio is.
    @author by ak
    """
    del log
    from app.core.client_coord import client_aspect_mismatch as _cam

    return _cam(client_size, target_aspect=target_aspect)


# ---------------------------------------------------------------------------
# Launcher driving (reuse running launcher, or start one; drive it to xajh).
# ---------------------------------------------------------------------------


def find_launcher_pid() -> int:
    """Return the running 快乐江湖登录器.exe pid, or 0. @author by ak"""
    try:
        import psutil

        for p in psutil.process_iter(["pid", "name"]):
            if "登录器" in (p.info.get("name") or ""):
                return int(p.pid)
    except Exception:
        pass
    return 0


def _win_class(hwnd: int) -> str:
    import ctypes as _ct
    import ctypes.wintypes as _wt

    u32 = _ct.WinDLL("user32", use_last_error=True)
    buf = _ct.create_unicode_buffer(128)
    u32.GetClassNameW(_wt.HWND(hwnd), buf, 128)
    return buf.value


def _win_text(hwnd: int) -> str:
    import ctypes as _ct
    import ctypes.wintypes as _wt

    u32 = _ct.WinDLL("user32", use_last_error=True)
    n = u32.GetWindowTextLengthW(_wt.HWND(hwnd))
    buf = _ct.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(_wt.HWND(hwnd), buf, n + 1)
    return buf.value


def _enum_top_windows(pid: int) -> list[int]:
    """All top-level window handles owned by pid. @author by ak"""
    import ctypes as _ct
    import ctypes.wintypes as _wt

    u32 = _ct.WinDLL("user32", use_last_error=True)
    out: list[int] = []

    @_ct.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    def cb(hwnd, _lp):
        p = _wt.DWORD(0)
        u32.GetWindowThreadProcessId(_wt.HWND(hwnd), _ct.byref(p))
        if int(p.value) == int(pid):
            out.append(int(hwnd))
        return True

    u32.EnumWindows(cb, 0)
    return out


def _post_click(hwnd: int) -> bool:
    """Non-blocking center click on a window via PostMessage. @author by ak"""
    import ctypes as _ct
    import ctypes.wintypes as _wt

    u32 = _ct.WinDLL("user32", use_last_error=True)
    rc = _wt.RECT()
    u32.GetClientRect(_wt.HWND(hwnd), _ct.byref(rc))
    cx = int((rc.right + rc.left) / 2)
    cy = int((rc.bottom + rc.top) / 2)
    lp = (cy << 16) | (cx & 0xFFFF)
    u32.PostMessageW(_wt.HWND(hwnd), 0x0201, 1, lp)  # WM_LBUTTONDOWN
    time.sleep(0.03)
    u32.PostMessageW(_wt.HWND(hwnd), 0x0202, 0, lp)  # WM_LBUTTONUP
    return True


def _find_big_start_button(main_hwnd: int) -> int:
    """Big start-game TsuiImageButton on the launcher main frame (visible+enabled). @author by ak"""
    import ctypes as _ct
    import ctypes.wintypes as _wt

    u32 = _ct.WinDLL("user32", use_last_error=True)
    found: list[tuple[int, bool, bool]] = []

    @_ct.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    def cb(hwnd, _lp):
        if "TsuiImageButton" not in _win_class(int(hwnd)):
            return True
        rc = _wt.RECT()
        u32.GetClientRect(_wt.HWND(hwnd), _ct.byref(rc))
        if rc.right - rc.left >= 150:
            found.append(
                (
                    int(hwnd),
                    bool(u32.IsWindowVisible(_wt.HWND(hwnd))),
                    bool(u32.IsWindowEnabled(_wt.HWND(hwnd))),
                )
            )
        return True

    u32.EnumChildWindows(_wt.HWND(main_hwnd), cb, 0)
    for h, vis, en in found:
        if vis and en:
            return h
    return 0


def _find_partition_confirm(launcher_pid: int) -> int:
    """Confirm button on the visible TfrmSelectServer dialog, else 0. @author by ak"""
    import ctypes as _ct
    import ctypes.wintypes as _wt

    u32 = _ct.WinDLL("user32", use_last_error=True)
    sel = 0
    for h in _enum_top_windows(launcher_pid):
        if _win_class(h) == "TfrmSelectServer" and u32.IsWindowVisible(_wt.HWND(h)):
            sel = h
            break
    if not sel:
        return 0
    btns: list[tuple[int, str, tuple[int, int, int, int]]] = []

    @_ct.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    def cb(hwnd, _lp):
        if "TsuiImageButton" not in _win_class(int(hwnd)):
            return True
        if not u32.IsWindowVisible(_wt.HWND(hwnd)):
            return True
        if not u32.IsWindowEnabled(_wt.HWND(hwnd)):
            return True
        rc = _wt.RECT()
        u32.GetClientRect(_wt.HWND(hwnd), _ct.byref(rc))
        btns.append((int(hwnd), _win_text(int(hwnd)), (rc.left, rc.top, rc.right, rc.bottom)))
        return True

    u32.EnumChildWindows(_wt.HWND(sel), cb, 0)
    if not btns:
        return 0
    for h, t, _rc in btns:
        if t and ("确" in t or "定" in t or "OK" in t.upper()):
            return h
    btns.sort(key=lambda x: x[2][0])
    return btns[0][0]


def _resolve_game_root_for_render_align() -> str:
    """Best-effort game install root for the saved render-size align.

    Prefers the remembered client path (bin/xajh.exe -> install root), then
    falls back to the well-known WeGame install scan. @author by ak
    """
    try:
        from app.core.account_manager import load_game_paths

        client = str((load_game_paths() or {}).get("client") or "").strip()
        if client:
            from pathlib import Path as _P

            p = _P(client)
            if p.name.lower() == "xajh.exe" and p.is_file():
                return str(p.resolve().parent.parent)
            if p.is_file():
                return str(p.resolve().parent)
    except Exception:
        pass
    try:
        from common.paths import find_game_root

        return str(find_game_root())
    except Exception:
        return ""


def drive_launcher_to_xajh(
    launcher_path: str,
    *,
    update_timeout_s: float = 10.0,
    poll_s: float = 0.5,
    settle_s: float = 4.0,
    xajh_timeout_s: float = 120.0,
    log: LogFn | None = None,
    stop_event=None,
) -> dict:
    """Reuse the running launcher (or start it), dismiss any update prompt, and
    drive it (start-game button + partition confirm) until a new xajh window
    appears. Returns {"ok", "launcher_pid", "xajh_pid", "hwnd", "error", "message"}.

    The launcher flow needs: big start button -> frmSelectServer -> confirm ->
    xajh spawns. The update dialog (选否) is polled throughout.

    When the launcher is STARTED fresh (not reused), it is given ``settle_s``
    seconds to stabilize before any UI probing, so an early "start game" button
    is not hit before the launcher is actually ready. The whole drive waits up
    to ``xajh_timeout_s`` for the game window; on timeout the caller should ask
    the user to start the launcher manually. @author by ak
    """
    import ctypes as _ct
    import ctypes.wintypes as _wt
    import os
    import subprocess

    log = log or (lambda _m: None)
    u32 = _ct.WinDLL("user32", use_last_error=True)
    launcher_path = str(launcher_path or "").strip()

    # The game reads its saved windowed render size (systemsettings.ini) at
    # process creation: align a non-16:9 saved size to its nearest 16:9 step
    # BEFORE launching so the login UI lays out natively. External resizing
    # after creation only moves the frame, not the render target, and
    # scrambles the UI. Best-effort; never blocks the login flow.
    try:
        from app.core.window_layout import align_game_render_size_to_nearest_16x9

        game_root = _resolve_game_root_for_render_align()
        if game_root:
            align_game_render_size_to_nearest_16x9(game_root, log=log)
        else:
            log("render size align skipped: game root not found")
    except Exception as e:
        log(f"render size align err: {e}")

    # ---- 0) reuse running launcher, else start one ----
    started_fresh = False
    lp = find_launcher_pid()
    if lp:
        log(f"launcher already running pid={lp}")
    else:
        if not launcher_path:
            return {"ok": False, "error": "launcher_missing", "message": "未设置登录器路径"}
        if not os.path.isfile(launcher_path):
            return {"ok": False, "error": "launcher_missing", "message": f"登录器不存在: {launcher_path}"}
        try:
            subprocess.Popen(
                [launcher_path],
                cwd=os.path.dirname(launcher_path),
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
            log(f"launcher started: {launcher_path}")
        except Exception as e:
            return {"ok": False, "error": "launcher_start_failed", "message": str(e)}
        started_fresh = True
        lp = find_launcher_pid()
        if not lp:
            return {"ok": False, "error": "launcher_start_failed", "message": "登录器启动后未找到进程"}

    # ---- 0.5) let a freshly started launcher stabilize before probing ----
    if started_fresh:
        settle = max(2.0, float(settle_s))
        log(f"launcher started fresh; settling {settle:.1f}s")
        settle_end = time.monotonic() + float(settle)
        while time.monotonic() < settle_end:
            if stop_event is not None and stop_event.is_set():
                log("launcher drive cancelled (settle)")
                return {"ok": False, "error": "cancelled",
                        "message": "已停止", "launcher_pid": lp,
                        "xajh_pid": 0, "hwnd": 0}
            time.sleep(0.25)

    # ---- 1..5) drive the launcher to a new xajh window.
    # Single poll loop, up to xajh_timeout_s: repeatedly dismiss any update
    # dialog (选否), wait TfrmMain -> big start button -> frmSelectServer ->
    # confirm -> xajh. Stage transitions are logged so the caller can show
    # progress; nothing fails early — only a total timeout returns an error.
    from app.core.inject_gate import find_main_hwnd_for_pid, find_xajh_processes

    try:
        known = {int(p.get("pid") or 0) for p in (find_xajh_processes() or []) if p.get("pid")}
    except Exception:
        known = set()

    main_hwnd = 0
    big_clicked = False
    confirm_clicked = False
    last_state = ""
    deadline = time.monotonic() + max(30.0, float(xajh_timeout_s))

    def _state(s: str) -> None:
        nonlocal last_state
        if s != last_state:
            last_state = s
            log(f"launcher stage -> {s}")

    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            log("launcher drive cancelled")
            return {"ok": False, "error": "cancelled",
                    "message": "已停止", "launcher_pid": lp,
                    "xajh_pid": 0, "hwnd": 0}
        # (a) handle ALL visible launcher message boxes: update prompt -> 否,
        #     other error dialogs -> click 确定/OK/关闭. Record counts.
        msgboxes = _launcher_msgboxes(lp)
        for mb in msgboxes:
            btns = mb.get("buttons") or []
            if any("&N" in b or "否" in b for b in btns):
                # update prompt: choose 否 (Alt+N)
                upd = dismiss_launcher_update_dialog(lp, prefer_no=True,
                                                     timeout_s=0.5, log=log)
                if upd.get("found") and upd.get("clicked"):
                    _state("update_dismissed")
                    time.sleep(0.5)
            else:
                # an error / notice box: click 确定/OK/关闭 if present
                target = 0
                for h in _child_buttons(mb.get("hwnd") or 0):
                    t = _win_text(h)
                    if any(k in t for k in ("确", "OK", "关闭", "&Y")):
                        target = h
                        break
                if target:
                    _state("msgbox_dismissed")
                    _post_click(target)
                    time.sleep(0.5)

        # (b) TfrmMain
        if not main_hwnd:
            for h in _enum_top_windows(lp):
                if _win_class(h) == "TfrmMain":
                    main_hwnd = h
                    break
            if main_hwnd:
                _state("main")
                u32.ShowWindow(_wt.HWND(main_hwnd), 9)
                continue

        # (c) big start button — only when no blocking msgbox is visible
        if main_hwnd and not big_clicked:
            if not _launcher_msgboxes(lp):
                big_btn = _find_big_start_button(main_hwnd)
                if big_btn:
                    # hide leftover partition dialogs, then click start
                    for h in _enum_top_windows(lp):
                        if _win_class(h) in ("TfrmSelectServer", "TfrmSelectGroup") \
                                and u32.IsWindowVisible(_wt.HWND(h)):
                            u32.ShowWindow(_wt.HWND(h), 0)
                            time.sleep(0.3)
                    _state("big_start_clicked")
                    _post_click(big_btn)
                    big_clicked = True
                    time.sleep(1.0)
                    continue

        # (d) frmSelectServer -> confirm
        if big_clicked and not confirm_clicked:
            sel_dlg = 0
            for h in _enum_top_windows(lp):
                if _win_class(h) == "TfrmSelectServer" and u32.IsWindowVisible(_wt.HWND(h)):
                    sel_dlg = h
                    break
            if sel_dlg:
                confirm_btn = _find_partition_confirm(lp)
                if confirm_btn:
                    _state("partition_confirm_clicked")
                    _post_click(confirm_btn)
                    confirm_clicked = True
                    time.sleep(1.0)
                    continue

        # (e) a new xajh window appeared?
        try:
            procs = find_xajh_processes() or []
            for pr in procs:
                pid = int(pr.get("pid") or 0)
                if not pid or pid in known:
                    continue
                try:
                    hwnd, _t, _c = find_main_hwnd_for_pid(pid) or (0, "", "")
                except Exception:
                    hwnd = 0
                if hwnd:
                    log(f"new xajh pid={pid} hwnd=0x{hwnd:X}")
                    return {"ok": True, "launcher_pid": lp, "xajh_pid": pid,
                            "hwnd": int(hwnd), "error": None, "message": "xajh up"}
        except Exception as e:  # pragma: no cover
            log(f"wait xajh err: {e}")

        time.sleep(max(0.3, float(poll_s)))

    return {"ok": False, "error": "no_game_hwnd",
            "message": "等待游戏窗口超时：请先手动启动好登录器，保证可以点击「启动游戏」，再使用一键登录",
            "launcher_pid": lp, "xajh_pid": 0, "hwnd": 0}
