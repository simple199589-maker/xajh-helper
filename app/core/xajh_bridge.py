# -*- coding: utf-8 -*-
"""
Client for in-process xajh_bridge.dll (main-thread game calls).

Shared memory: Local\\XajhBridge_<pid>
Dispatch: PostMessage(hwnd, WM_APP+0x51, 0, event)

BridgeShared pack(1) offsets:
  0  magic u32
  4  seq u32
  8  cmd u32
  12 status i32
  16 ret i32
  20 module_base u32
  24 hwnd u32
  28 x f32
  32 y f32
  36 z f32
  40 mode i32
  44 id_lo u32
  48 id_hi u32
  52 tid i32
  56 err[128]
  184 protocol_version u32
  188 struct_size u32
  192 capabilities u32
  196 ack_seq u32
  size=200 (v1 readers keep the original 184-byte prefix)

@author by ak
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.core.bridge_protocol import (
    BRIDGE_BUILD_ID,
    BRIDGE_MAGIC,
    LEGACY_SHARED_SIZE,
    PROTOCOL_VERSION,
    SHARED_SIZE,
    BridgeCapability,
    BridgeCommand,
    BridgeStatus,
    OFF_ACK_SEQ,
    OFF_BASE,
    OFF_CAPABILITIES,
    OFF_CMD,
    OFF_ERR,
    OFF_HWND,
    OFF_ID_HI,
    OFF_ID_LO,
    OFF_MAGIC,
    OFF_MODE,
    OFF_PROTOCOL_VERSION,
    OFF_STRUCT_SIZE,
    OFF_RET,
    OFF_SEQ,
    OFF_STATUS,
    OFF_TID,
    OFF_X,
    OFF_Y,
    OFF_Z,
    required_capability,
    required_build_capability,
)
from app.core.client_build import CapabilitySet, ClientBuildProfile, profile_for_client
from app.core.remote_runtime import pid_call_mutex

LogFn = Callable[[str], None]

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

# Explicit prototypes: default c_long truncates 64-bit handles/pointers and
# rejects INVALID_HANDLE_VALUE (0xFFFFFFFFFFFFFFFF) with OverflowError.
kernel32.CreateFileMappingW.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPCWSTR,
]
kernel32.CreateFileMappingW.restype = wintypes.HANDLE
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

FILE_MAP_ALL_ACCESS = 0xF001F
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
WM_APP = 0x8000
BRIDGE_WM = WM_APP + 0x51
ST_OK = int(BridgeStatus.OK)
CMD_PING = int(BridgeCommand.PING)
CMD_SET_TARGET = int(BridgeCommand.SET_TARGET)
CMD_PICK_ITEM = int(BridgeCommand.PICK_ITEM)
CMD_HOST_MOVE = int(BridgeCommand.HOST_MOVE)
CMD_CHOICE_OBJECT = int(BridgeCommand.CHOICE_OBJECT)
CMD_PICKUP_NOTE = int(BridgeCommand.PICKUP_NOTE)
CMD_REHOOK = int(BridgeCommand.REHOOK)
CMD_UNLOAD = int(BridgeCommand.UNLOAD)
# Open chest / cast-bar interact (AutoClickMatter on UI thread)
CMD_AUTO_CLICK_MATTER = int(BridgeCommand.AUTO_CLICK_MATTER)
CMD_AUTO_CLICK_DYN_MATTER = int(BridgeCommand.AUTO_CLICK_DYN_MATTER)
# In-game CaptureScreen -> <game>/Screenshots/*.jpg (no PrintWindow)
CMD_CAPTURE_SCREEN = int(BridgeCommand.CAPTURE_SCREEN)
# UI-thread client click: id_lo=cx, id_hi=cy (background-safe PostMessage).
CMD_UI_CLICK = int(BridgeCommand.UI_CLICK)
# CDlgActivityQuestion Btn_Ok submit thiscall @ 0x8CD2F0; id_lo=dlg*.
CMD_AQ_SUBMIT = int(BridgeCommand.AQ_SUBMIT)
# Task accept / complete on UI thread (see task_api.py)
CMD_TASK_ACCEPT = int(BridgeCommand.TASK_ACCEPT)
CMD_TASK_COMPLETE = int(BridgeCommand.TASK_COMPLETE)
# Instance enter via list packet 0x58 (cc6f80); id_lo=inst_id, mode=diff, tid=flag.
CMD_INSTANCE_ENTER = int(BridgeCommand.INSTANCE_ENTER)
# UI-thread key: id_lo=vk, id_hi=action(0=down,1=up,2=press), mode=hold_ms for press.
CMD_UI_KEY = int(BridgeCommand.UI_KEY)
# Cast skill: id_lo=skill_id or bar slot; mode=0 id / 1 slot; tid/hi optional target.
CMD_CAST_SKILL = int(BridgeCommand.CAST_SKILL)
# Force key via IAT GetAsyncKeyState hook: id_lo=vk, id_hi=0 clear / 1 down.
CMD_KEY_FORCE = int(BridgeCommand.KEY_FORCE)
# Record GetAsyncKeyState/GetKeyState callers: id_hi=1 start / 0 stop+dump; id_lo=vk filter.
CMD_KEY_TRACE = int(BridgeCommand.KEY_TRACE)
CMD_CANCEL_SESSION = int(BridgeCommand.CANCEL_SESSION)
CMD_ONSKILL_STOPPED = int(BridgeCommand.ONSKILL_STOPPED)
CMD_KEY_DIAG = int(BridgeCommand.KEY_DIAG)
CMD_KEY_HOLD = int(BridgeCommand.KEY_HOLD)
# Change scene line via packet 0x7A; id_lo=line_id (0=主线/0线).
CMD_CHANGE_LINE = int(BridgeCommand.CHANGE_LINE)
CMD_USE_ITEM_IN_PACKAGE = int(BridgeCommand.USE_ITEM_IN_PACKAGE)
CMD_TEAM_INVITE = int(BridgeCommand.TEAM_INVITE)
# Game TeamFollow dialog callbacks (0x9A0680 / 0x9A06A0), dispatched by the
# bridge timer on the game's UI thread. id_lo: 1=enable, 0=disable.
CMD_TEAM_FOLLOW = int(BridgeCommand.TEAM_FOLLOW)
CMD_NPC_TALK_SELECT = int(BridgeCommand.NPC_TALK_SELECT)
CMD_NPC_HOST_SELECT = int(BridgeCommand.NPC_HOST_SELECT)
CMD_HOST_CONTEXT = int(BridgeCommand.HOST_CONTEXT)
# CECAutoPlay::StopAutoPlay on the game UI thread; id_lo=reason u8.
CMD_AUTOPLAY_STOP = int(BridgeCommand.AUTOPLAY_STOP)
# Native UseItem return counter: id_lo=package, id_hi=slot, mode 1/2/0.
CMD_ITEM_USE_TRACE = int(BridgeCommand.ITEM_USE_TRACE)
CMD_HOST_SNAPSHOT = int(BridgeCommand.HOST_SNAPSHOT)
CMD_SKILL_ACTION_TRACE = int(BridgeCommand.SKILL_ACTION_TRACE)
# CDlgAutoPlayFrame::Btn_Start with only skill/leader validation bypassed.
CMD_AUTOPLAY_START_BYPASS = int(BridgeCommand.AUTOPLAY_START_BYPASS)
# Exact local perform interruption; does not synthesize X/Space input.
CMD_SKILL_INTERRUPT_67 = int(BridgeCommand.SKILL_INTERRUPT_67)
# Seed the dungeon autoplay state machine with a non-self party member target.
CMD_AUTOPLAY_SEED_FOLLOW = int(BridgeCommand.AUTOPLAY_SEED_FOLLOW)
CMD_QUICK_TEAM_FOLLOW = int(BridgeCommand.QUICK_TEAM_FOLLOW)
CMD_OBJECT_SCAN = int(BridgeCommand.OBJECT_SCAN)
# Exact no-key 有凤 tail transaction: internal E07 action then local cleanup.
CMD_YOUFENG_INTERNAL_CHAIN = int(BridgeCommand.YOUFENG_INTERNAL_CHAIN)
CMD_YOUFENG_PHASE_GATE = int(BridgeCommand.YOUFENG_PHASE_GATE)
CMD_YOUFENG_QINGGONG_GATE = int(BridgeCommand.YOUFENG_QINGGONG_GATE)
CMD_SKILL_ACTION_EXPERIMENT = int(BridgeCommand.SKILL_ACTION_EXPERIMENT)
# Bounded same-skill current-action gate window. No interrupt action/key.
CMD_YOUFENG_NEXT_GATE = int(BridgeCommand.YOUFENG_NEXT_GATE)
# ActionCast mash path with 有凤 itself as the next action; no E07.
CMD_YOUFENG_MASH_CAST = int(BridgeCommand.YOUFENG_MASH_CAST)
# Clear completed 有凤 identity, then arm the bounded normal-input gate.
CMD_YOUFENG_FRESH_GATE = int(BridgeCommand.YOUFENG_FRESH_GATE)
# Cancel exact server perform, clear old identity, then arm normal-input gate.
CMD_YOUFENG_EXACT_CANCEL_GATE = int(BridgeCommand.YOUFENG_EXACT_CANCEL_GATE)
# Read-only one-minute damage accumulator for one exact object id.
CMD_DUMMY_DAMAGE_STAT = int(BridgeCommand.DUMMY_DAMAGE_STAT)
CMD_CG_SKIP = int(BridgeCommand.CG_SKIP)
CMD_SESSION_SEND_BYPASS = int(BridgeCommand.SESSION_SEND_BYPASS)
CMD_COMBAT_MONITOR = int(BridgeCommand.COMBAT_MONITOR)
# Dump UTF-16 caption text under Win_InstanceConfig / Win_TrackFrame on the
# game UI thread (bypasses the scene-settle CRT gate).
# id_lo: 0=Win_InstanceConfig, 1=Win_TrackFrame, 2=both.
CMD_DUMP_UI_TEXT = int(BridgeCommand.DUMP_UI_TEXT)
# Native selected-target submission trace / narrow dungeon guard at 0x7493B0.
CMD_TARGET_SUBMIT_TRACE = int(BridgeCommand.TARGET_SUBMIT_TRACE)
CMD_DUNGEON_TARGET_RULES = int(BridgeCommand.DUNGEON_TARGET_RULES)
CMD_JIANGLONG_RUNTIME_RESOLVE = int(BridgeCommand.JIANGLONG_RUNTIME_RESOLVE)
# Mouse button codes for CMD_UI_CLICK tid field.
UI_CLICK_LEFT = 0
UI_CLICK_RIGHT = 1
# Key action codes for CMD_UI_KEY id_hi field.
UI_KEY_DOWN = 0
UI_KEY_UP = 1
UI_KEY_PRESS = 2


@dataclass
class BridgeResult:
    ok: bool
    cmd: int
    ret: int | None = None
    status: int = 0
    error: str | None = None
    note: str = ""
    hwnd: int = 0
    module_base: int = 0
    protocol_version: int = 0
    capabilities: int = 0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    mode: int = 0
    tid: int = 0
    id_lo: int = 0
    id_hi: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "cmd": self.cmd,
            "ret": self.ret,
            "status": self.status,
            "error": self.error,
            "note": self.note,
            "hwnd": self.hwnd,
            "module_base": self.module_base,
            "protocol_version": self.protocol_version,
            "capabilities": self.capabilities,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "mode": self.mode,
            "tid": self.tid,
            "id_lo": self.id_lo,
            "id_hi": self.id_hi,
        }


def default_bridge_paths() -> tuple[Path, Path]:
    """
    Locate the canonical bridge DLL and injector.

    Prefer only the stamped DLL whose name exactly matches BRIDGE_BUILD_ID.
    Other historical builds remain ignored, while a currently loaded generic
    DLL may stay locked by an older client.
    When packaged, prefer a short path under app_root/native/bin so the
    game process can LoadLibrary without long/special _internal paths.
    @author by ak
    """
    try:
        from common.paths import NATIVE_BIN_DIR, app_root, bundle_root
    except Exception:
        NATIVE_BIN_DIR = Path(__file__).resolve().parents[2] / "native" / "bin"
        app_root = lambda: Path(__file__).resolve().parents[2]  # noqa: E731
        bundle_root = app_root

    # Writable stage first (short path next to helper exe when frozen).
    search_dirs: list[Path] = []
    for d in (
        app_root() / "native" / "bin",
        NATIVE_BIN_DIR,
        bundle_root() / "native" / "bin",
        app_root() / "_internal" / "native" / "bin",
        Path(__file__).resolve().parents[2] / "native" / "bin",
    ):
        if d not in search_dirs:
            search_dirs.append(d)

    inj: Path | None = None
    for bin_dir in search_dirs:
        cand = bin_dir / "xajh_inject.exe"
        if cand.is_file():
            inj = cand
            break
    if inj is None:
        inj = search_dirs[0] / "xajh_inject.exe"

    dll: Path | None = None
    for bin_dir in search_dirs:
        candidate = bin_dir / f"xajh_bridge_{int(BRIDGE_BUILD_ID)}.dll"
        if candidate.is_file():
            dll = candidate
            break
    if dll is None:
        for bin_dir in search_dirs:
            candidate = bin_dir / "xajh_bridge.dll"
            if candidate.is_file():
                dll = candidate
                break
    if dll is None:
        dll = search_dirs[0] / "xajh_bridge.dll"
    return dll, inj


def _inject_stage_dir() -> Path:
    """
    Directory for LoadLibrary copies used by the game process.

    Keep staging under the current software target so the full installation is
    self-contained and removable. A dedicated runtime directory keeps the
    formal bundled binaries separate while build-specific names avoid clashes
    with an older game process that still has a DLL mapped.
    @author by ak
    """
    try:
        from common.paths import app_root

        return app_root() / "runtime" / "native" / "bin"
    except Exception:
        return Path.cwd() / "runtime" / "native" / "bin"


def stage_bridge_for_inject(log: LogFn | None = None) -> tuple[Path, Path]:
    """
    Copy bridge DLL + injector to a short path under the current software root.

    Game LoadLibrary receives this absolute path. The runtime directory is
    deliberately self-contained so uninstall/cleanup does not leave files in
    a user profile directory.
    @author by ak
    """
    log = log or (lambda _m: None)
    dll, inj = default_bridge_paths()
    stage = _inject_stage_dir()
    try:
        stage.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log(f"stage mkdir failed: {e}")
        return dll, inj

    # A loaded DLL keeps its exact path mapped until the target process exits.
    # Use a build-specific filename so a newly launched game can always load
    # the current bridge even while an older game still owns the legacy stage.
    staged_dll = stage / f"xajh_bridge_{int(BRIDGE_BUILD_ID)}.dll"
    staged_inj = stage / "xajh_inject.exe"

    def _copy_if_needed(src: Path, dst: Path) -> Path:
        if not src.is_file():
            return src
        try:
            import hashlib

            def _digest(path: Path) -> bytes:
                value = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        value.update(chunk)
                return value.digest()

            if not dst.is_file() or _digest(dst) != _digest(src):
                import shutil

                shutil.copy2(src, dst)
                log(f"staged {src.name} -> {dst}")
        except Exception as e:
            log(f"stage copy {src.name} failed: {e}")
            return src
        return dst if dst.is_file() else src

    out_dll = _copy_if_needed(dll, staged_dll)
    out_inj = _copy_if_needed(inj, staged_inj)
    return out_dll, out_inj


def is_bridge_built() -> bool:
    dll, inj = default_bridge_paths()
    return dll.is_file() and inj.is_file()


def pe_machine(path: Path) -> int | None:
    """
    Read PE Machine field (0x14C=i386, 0x8664=AMD64). None if unreadable.

    @author by ak
    """
    try:
        raw = Path(path).read_bytes()
        if len(raw) < 0x40 or raw[:2] != b"MZ":
            return None
        e_lfanew = int.from_bytes(raw[0x3C:0x40], "little")
        if e_lfanew <= 0 or e_lfanew + 6 > len(raw):
            return None
        if raw[e_lfanew : e_lfanew + 4] != b"PE\0\0":
            return None
        return int.from_bytes(raw[e_lfanew + 4 : e_lfanew + 6], "little")
    except Exception:
        return None


def probe_open_process(pid: int) -> tuple[bool, int]:
    """
    Try OpenProcess with inject rights. Returns (ok, win32_err).

    @author by ak
    """
    pid = int(pid)
    if pid <= 0:
        return False, 87  # ERROR_INVALID_PARAMETER
    rights = (
        0x0002  # CREATE_THREAD
        | 0x0400  # QUERY_INFORMATION
        | 0x0008  # VM_OPERATION
        | 0x0020  # VM_WRITE
        | 0x0010  # VM_READ
    )
    try:
        h = kernel32.OpenProcess(rights, False, pid)
        err = int(ctypes.get_last_error() or 0)
        if h:
            kernel32.CloseHandle(h)
            return True, 0
        return False, err
    except Exception:
        return False, int(ctypes.get_last_error() or 0)


def preflight_inject(pid: int, *, log: LogFn | None = None) -> dict:
    """
    Collect inject readiness signals for one-shot remote triage.

    Keys: admin, alive, open_process_ok, open_process_err, dll, inj,
    dll_machine, game_exe, game_machine, path_len, arch_ok, ok, fail_code.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "pid": int(pid),
        "admin": False,
        "alive": False,
        "open_process_ok": False,
        "open_process_err": 0,
        "dll": "",
        "inj": "",
        "dll_machine": None,
        "game_exe": "",
        "game_machine": None,
        "path_len": 0,
        "arch_ok": True,
        "ok": False,
        "fail_code": "",
    }
    try:
        from app.core.diag_log import is_admin, process_alive

        out["admin"] = bool(is_admin())
        out["alive"] = bool(process_alive(int(pid)))
    except Exception as e:
        log(f"preflight admin/alive err: {e}")
        out["alive"] = _pid_alive(pid)

    op_ok, op_err = probe_open_process(pid)
    out["open_process_ok"] = op_ok
    out["open_process_err"] = op_err

    dll, inj = stage_bridge_for_inject(log=log)
    out["dll"] = str(dll)
    out["inj"] = str(inj)
    out["path_len"] = len(str(dll.resolve())) if dll.is_file() else 0
    out["dll_machine"] = pe_machine(dll) if dll.is_file() else None

    try:
        import psutil

        proc = psutil.Process(int(pid))
        exe = proc.exe() or ""
        out["game_exe"] = exe
        if exe:
            out["game_machine"] = pe_machine(Path(exe))
    except Exception as e:
        log(f"preflight game pe err: {e}")

    # 0x14C = IMAGE_FILE_MACHINE_I386
    dm = out["dll_machine"]
    gm = out["game_machine"]
    if dm is not None and gm is not None and dm != gm:
        out["arch_ok"] = False
    if dm is not None and dm != 0x14C:
        out["arch_ok"] = False
        log(f"preflight: bridge DLL is not x86 machine=0x{dm:X}")

    if not out["alive"]:
        out["fail_code"] = "GAME_DEAD"
    elif not dll.is_file() or not inj.is_file():
        out["fail_code"] = "BRIDGE_MISSING"
    elif not out["arch_ok"]:
        out["fail_code"] = "ARCH_MISMATCH"
    elif not out["open_process_ok"]:
        # 5=ACCESS_DENIED typical non-admin / protected
        out["fail_code"] = "OPENPROCESS_DENIED" if op_err in (5, 6) else f"OPENPROCESS_{op_err}"
    elif out["path_len"] > 240:
        out["fail_code"] = "DLL_PATH_LONG"
    else:
        out["ok"] = True
        out["fail_code"] = ""

    log(
        f"preflight admin={out['admin']} alive={out['alive']} "
        f"open_ok={out['open_process_ok']} open_err={out['open_process_err']} "
        f"dll_mach={out['dll_machine']!r} game_mach={out['game_machine']!r} "
        f"path_len={out['path_len']} arch_ok={out['arch_ok']} "
        f"fail={out['fail_code']!r}"
    )
    if out["game_exe"]:
        log(f"preflight game_exe={out['game_exe']!r}")
    return out


PAGE_READWRITE = 0x04
# Must be HANDLE, not c_void_p(-1).value (0xFFF... overflows default c_long).
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1)


def _create_world_sd() -> int | None:
    """
    SECURITY_DESCRIPTOR allowing all (helper admin <-> game medium IL).

    Returns integer pointer owned by a kept buffer, or None.
    @author by ak
    """
    try:
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.InitializeSecurityDescriptor.argtypes = [wintypes.LPVOID, wintypes.DWORD]
        adv.InitializeSecurityDescriptor.restype = wintypes.BOOL
        adv.SetSecurityDescriptorDacl.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPVOID,
            wintypes.BOOL,
        ]
        adv.SetSecurityDescriptorDacl.restype = wintypes.BOOL
        # SECURITY_DESCRIPTOR is opaque; allocate 64 bytes.
        sd = (ctypes.c_ubyte * 64)()
        if not adv.InitializeSecurityDescriptor(ctypes.byref(sd), 1):
            return None
        if not adv.SetSecurityDescriptorDacl(
            ctypes.byref(sd), True, None, False
        ):
            return None
        # Keep buffer alive on function attribute.
        _create_world_sd._buf = sd  # type: ignore[attr-defined]
        return ctypes.addressof(sd)
    except Exception:
        return None


def prepare_bridge_shm(
    pid: int,
    hwnd: int = 0,
    *,
    attach_mode: int = 0,
    log: LogFn | None = None,
) -> bool:
    """
    Pre-create Global\\XajhBridge_<pid> before inject (admin helper side).

    Game DLL opens this mapping; hwnd + attach_mode are written before
    LoadLibrary so attach can branch (standard vs green) without EnumWindows.
    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(pid)
    if pid <= 0:
        return False
    names = [f"Global\\XajhBridge_{pid}", f"Local\\XajhBridge_{pid}"]
    size = SHARED_SIZE
    sa = None
    sa_obj = None
    sd_addr = _create_world_sd()
    if sd_addr:
        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL),
            ]

        sa_obj = SECURITY_ATTRIBUTES()
        sa_obj.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa_obj.lpSecurityDescriptor = sd_addr
        sa_obj.bInheritHandle = False
        sa = ctypes.byref(sa_obj)

    h = 0
    used = ""
    for name in names:
        h = kernel32.CreateFileMappingW(
            INVALID_HANDLE_VALUE,
            sa,
            PAGE_READWRITE,
            0,
            size,
            name,
        )
        if h:
            used = name
            break
        err = ctypes.get_last_error()
        log(f"CreateFileMapping {name} failed err={err}")
    if not h:
        return False
    view = kernel32.MapViewOfFile(h, FILE_MAP_ALL_ACCESS, 0, 0, size)
    view_i = int(ctypes.cast(view, ctypes.c_void_p).value or 0) if view else 0
    if not view_i:
        kernel32.CloseHandle(h)
        log(f"MapViewOfFile prepare failed err={ctypes.get_last_error()}")
        return False
    try:
        ctypes.memset(view_i, 0, size)
        ctypes.c_uint32.from_address(view_i + OFF_MAGIC).value = BRIDGE_MAGIC
        ctypes.c_int32.from_address(view_i + OFF_STATUS).value = 0
        # Protocol + struct size must be written here: the game DLL only
        # overwrites them when it creates the mapping fresh (created_fresh), and
        # otherwise preserves helper-written fields. Without them XajhBridge
        # reports protocol_version=0 and rejects every formal command.
        try:
            ctypes.c_uint32.from_address(view_i + OFF_PROTOCOL_VERSION).value = (
                int(PROTOCOL_VERSION) & 0xFFFFFFFF
            )
            ctypes.c_uint32.from_address(view_i + OFF_STRUCT_SIZE).value = (
                int(SHARED_SIZE) & 0xFFFFFFFF
            )
            # Same rationale for capabilities: the DLL writes them only on
            # created_fresh, so a pre-created mapping stays 0 and every formal
            # command (UI_CLICK/KEY_HOLD/...) is rejected with
            # "bridge capability missing".
            try:
                from app.core.bridge_protocol import DEFAULT_CAPABILITIES

                caps = int(DEFAULT_CAPABILITIES) & 0xFFFFFFFF
            except Exception:
                caps = 0x1FFF  # all known capability bits
            ctypes.c_uint32.from_address(view_i + OFF_CAPABILITIES).value = caps
        except Exception:
            pass
        if hwnd:
            ctypes.c_uint32.from_address(view_i + OFF_HWND).value = (
                int(hwnd) & 0xFFFFFFFF
            )
        # Attach policy only; business cmds overwrite mode after ready.
        ctypes.c_int32.from_address(view_i + OFF_MODE).value = int(attach_mode)
        # Keep mapping open for process lifetime so game can open it.
        # Store on module-level dict keyed by pid.
        global _PREPARED_SHM
        old = _PREPARED_SHM.pop(pid, None)
        if old:
            try:
                kernel32.UnmapViewOfFile(ctypes.c_void_p(old[1]))
                kernel32.CloseHandle(wintypes.HANDLE(old[0]))
            except Exception:
                pass
        # Keep sa_obj / world SD alive with the mapping handle.
        _PREPARED_SHM[pid] = (int(h), view_i, used)
        log(
            f"prepare_shm ok name={used} hwnd=0x{int(hwnd):X} "
            f"attach_mode=0x{int(attach_mode) & 0xFFFF:X}"
        )
        return True
    except Exception as e:
        log(f"prepare_shm write failed: {e}")
        try:
            kernel32.UnmapViewOfFile(ctypes.c_void_p(view_i))
            kernel32.CloseHandle(h)
        except Exception:
            pass
        return False


_PREPARED_SHM: dict[int, tuple[int, int, str]] = {}
# Last fail-closed reason from ensure_bridge, keyed by target PID. This lets the
# inject UI distinguish a resident old DLL from a real injector failure.
_ENSURE_BRIDGE_FAILURES: dict[int, str] = {}
# Last native injector failure, keyed by target PID.  The injector prints the
# exact Win32 stage so the UI can report an actionable cause.
_INJECT_FAILURES: dict[int, str] = {}


def last_ensure_bridge_failure(pid: int, *, clear: bool = False) -> str:
    """Return the latest machine-readable ensure failure for one PID."""
    key = int(pid)
    if clear:
        return _ENSURE_BRIDGE_FAILURES.pop(key, "")
    return _ENSURE_BRIDGE_FAILURES.get(key, "")


def last_inject_failure(pid: int, *, clear: bool = False) -> str:
    key = int(pid)
    if clear:
        return _INJECT_FAILURES.pop(key, "")
    return _INJECT_FAILURES.get(key, "")


def inject_bridge(
    pid: int,
    *,
    log: LogFn | None = None,
    hwnd: int | None = None,
    attach_mode: int = 0,
) -> bool:
    """Load xajh_bridge.dll into target pid via injector. @author by ak"""
    log = log or (lambda _m: None)
    _INJECT_FAILURES.pop(int(pid), None)
    dll, inj = stage_bridge_for_inject(log=log)
    if not dll.is_file() or not inj.is_file():
        log(f"bridge not built: need {dll.name} + {inj.name} under native/bin")
        return False
    # Pre-create shm + hwnd + attach_mode before LoadLibrary.
    try:
        prepare_bridge_shm(
            int(pid),
            int(hwnd or 0),
            attach_mode=int(attach_mode or 0),
            log=log,
        )
    except Exception as e:
        log(f"prepare_shm skip: {e}")
    dll_path = str(dll.resolve())
    inj_path = str(inj.resolve())
    log(
        f"inject start pid={pid} dll={dll_path} inj={inj_path} "
        f"attach_mode=0x{int(attach_mode or 0) & 0xFFFF:X}"
    )
    log(f"pre-run alive={_pid_alive(pid)} path_len={len(dll_path)}")
    try:
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = int(subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
        r = subprocess.run(
            [inj_path, str(int(pid)), dll_path],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(dll.parent),
            creationflags=creationflags,
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        log(f"inject exit={r.returncode} {out}")
        alive = _pid_alive(pid)
        log(f"post-run alive={alive}")
        if r.returncode == 0 and not alive:
            _INJECT_FAILURES[int(pid)] = "GAME_CRASH"
            log("inject returned ok but game process is dead")
            return False
        if r.returncode != 0:
            low = out.lower()
            if "openprocess failed" in low:
                code = "OPENPROCESS_DENIED"
            elif "createremotethread failed" in low:
                code = "CREATEREMOTETHREAD_FAIL"
            elif "loadlibrary returned null" in low:
                code = "LOADLIBRARY_FAIL"
            elif "remote loadlibrary incomplete" in low:
                code = "INJECT_TIMEOUT"
            elif "target process exited" in low:
                code = "GAME_CRASH"
            else:
                code = "INJECT_EXE_FAIL"
            _INJECT_FAILURES[int(pid)] = code
        return r.returncode == 0
    except subprocess.TimeoutExpired as e:
        _INJECT_FAILURES[int(pid)] = "INJECT_TIMEOUT"
        log(f"inject timeout: {e}")
        log(f"post-timeout alive={_pid_alive(pid)}")
        return False
    except Exception as e:
        _INJECT_FAILURES[int(pid)] = "INJECT_EXE_FAIL"
        log(f"inject failed: {e}")
        log(f"post-error alive={_pid_alive(pid)}")
        return False


def _pid_alive(pid: int) -> bool:
    """True if pid still running. @author by ak"""
    try:
        from app.core.diag_log import process_alive

        return process_alive(int(pid))
    except Exception:
        pass
    try:
        import psutil

        return psutil.pid_exists(int(pid))
    except Exception:
        return True


class XajhBridge:
    """Open shared memory for an already-injected bridge. @author by ak"""

    def __init__(self, pid: int, log: LogFn | None = None):
        self.pid = int(pid)
        self.log = log or (lambda _m: None)
        self._map = 0
        self._view = 0
        self._hwnd = 0
        self._mapped_size = 0
        self.shm_name: str = ""
        self.client_profile: ClientBuildProfile | None = None
        self.client_capabilities = CapabilitySet()

    @property
    def protocol_version(self) -> int:
        if not self._view or self._mapped_size < SHARED_SIZE:
            return 0
        return self._u32(OFF_PROTOCOL_VERSION)

    @property
    def capabilities(self) -> BridgeCapability:
        if not self._view or self.protocol_version < PROTOCOL_VERSION:
            return BridgeCapability.NONE
        return BridgeCapability(self._u32(OFF_CAPABILITIES))

    def open(self, *, quiet: bool = False) -> bool:
        """
        Map shared memory. Tries Global\\ then Local\\ (admin isolation).

        quiet=True suppresses per-retry OpenFileMapping noise.
        @author by ak
        """
        names = [
            f"Global\\XajhBridge_{self.pid}",
            f"Local\\XajhBridge_{self.pid}",
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
                self.log(
                    f"OpenFileMapping failed pid={self.pid} last_err={last_err}"
                )
            return False
        mapped_size = SHARED_SIZE
        view = kernel32.MapViewOfFile(h, FILE_MAP_ALL_ACCESS, 0, 0, mapped_size)
        if not view:
            mapped_size = LEGACY_SHARED_SIZE
            view = kernel32.MapViewOfFile(h, FILE_MAP_ALL_ACCESS, 0, 0, mapped_size)
        view_i = int(ctypes.cast(view, ctypes.c_void_p).value or 0) if view else 0
        if not view_i:
            kernel32.CloseHandle(h)
            if not quiet:
                self.log(f"MapViewOfFile failed err={ctypes.get_last_error()}")
            return False
        self._map = int(h)
        self._view = view_i
        self._mapped_size = mapped_size
        magic = self._u32(OFF_MAGIC)
        if magic != BRIDGE_MAGIC:
            self.log(f"bad magic 0x{magic:X} name={used}")
            self.close()
            return False
        self._hwnd = self._u32(OFF_HWND)
        self.shm_name = used
        try:
            import psutil

            exe = psutil.Process(self.pid).exe()
            self.client_profile = profile_for_client(str(exe))
            if self.client_profile is not None:
                self.client_capabilities = self.client_profile.capabilities
        except Exception:
            self.client_profile = None
            self.client_capabilities = CapabilitySet()
        if not quiet:
            self.log(
                f"bridge open pid={self.pid} name={used} hwnd=0x{self._hwnd:X} "
                f"base=0x{self._u32(OFF_BASE):X} note={self._err()!r}"
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

    def _u32(self, off: int) -> int:
        return ctypes.c_uint32.from_address(self._view + off).value

    def _i32(self, off: int) -> int:
        return ctypes.c_int32.from_address(self._view + off).value

    def _set_u32(self, off: int, val: int) -> None:
        ctypes.c_uint32.from_address(self._view + off).value = int(val) & 0xFFFFFFFF

    def _set_i32(self, off: int, val: int) -> None:
        ctypes.c_int32.from_address(self._view + off).value = int(val)

    def _set_f32(self, off: int, val: float) -> None:
        ctypes.c_float.from_address(self._view + off).value = float(val)

    def _f32(self, off: int) -> float:
        return float(ctypes.c_float.from_address(self._view + off).value)

    def _err(self) -> str:
        raw = (ctypes.c_char * 128).from_address(self._view + OFF_ERR)
        return bytes(raw).split(b"\x00", 1)[0].decode("utf-8", "ignore")

    def call(
        self,
        cmd: int,
        *,
        id_lo: int = 0,
        id_hi: int = 0,
        tid: int = 0,
        x: float = 0.0,
        y: float = 0.0,
        z: float = 0.0,
        mode: int = 0,
        timeout_ms: int = 5000,
        hwnd: int | None = None,
        err_input: str | None = None,
    ) -> BridgeResult:
        """Serialize the single-slot protocol and submit one command."""
        # KEY_TRACE still needs IAT hooks — fail closed. KEY_FORCE is memory path.
        if int(cmd) == CMD_KEY_TRACE:
            result = BridgeResult(
                ok=False,
                cmd=int(cmd),
                status=int(BridgeStatus.ERROR),
                error="UNSUPPORTED_UNSAFE_HOOK",
                note="runtime IAT/inline key tracing is disabled in formal builds",
                protocol_version=self.protocol_version,
                capabilities=int(self.capabilities),
            )
            self._note_crash_context(result)
            return result
        try:
            with pid_call_mutex(
                self.pid, timeout_ms=max(int(timeout_ms) + 1000, 2000), namespace="Call"
            ):
                result = self._call_unlocked(
                    cmd,
                    id_lo=id_lo,
                    id_hi=id_hi,
                    tid=tid,
                    x=x,
                    y=y,
                    z=z,
                    mode=mode,
                    timeout_ms=timeout_ms,
                    hwnd=hwnd,
                    err_input=err_input,
                )
        except (OSError, TimeoutError) as exc:
            result = BridgeResult(ok=False, cmd=int(cmd), error=str(exc))
        self._note_crash_context(result)
        return result

    def _note_crash_context(self, result: BridgeResult) -> None:
        """Remember last bridge result for game-crash triage. @author by ak"""
        try:
            from app.core.crash_capture import note_bridge_context

            note_bridge_context(
                int(self.pid),
                cmd=getattr(result, "cmd", None),
                status=getattr(result, "status", None),
                ret=getattr(result, "ret", None),
                err=str(getattr(result, "error", "") or ""),
                note=str(getattr(result, "note", "") or ""),
            )
        except Exception:
            pass

    def _call_unlocked(
        self,
        cmd: int,
        *,
        id_lo: int = 0,
        id_hi: int = 0,
        tid: int = 0,
        x: float = 0.0,
        y: float = 0.0,
        z: float = 0.0,
        mode: int = 0,
        timeout_ms: int = 5000,
        hwnd: int | None = None,
        err_input: str | None = None,
    ) -> BridgeResult:
        """
        Submit cmd via shared memory; UI-thread timer in the game runs it.

        No PostMessage / WndProc required (foreign clients crash on subclass).
        @author by ak
        """
        if not self._view:
            return BridgeResult(ok=False, cmd=cmd, error="bridge not open")
        if int(cmd) == CMD_KEY_TRACE:
            return BridgeResult(
                ok=False,
                cmd=int(cmd),
                status=int(BridgeStatus.ERROR),
                error="UNSUPPORTED_UNSAFE_HOOK",
                note="runtime IAT/inline key tracing is disabled in formal builds",
                protocol_version=self.protocol_version,
                capabilities=int(self.capabilities),
            )
        protocol_version = self.protocol_version
        capabilities = int(self.capabilities)
        if protocol_version < PROTOCOL_VERSION and int(cmd) != CMD_PING:
            return BridgeResult(
                ok=False,
                cmd=int(cmd),
                status=int(BridgeStatus.ERROR),
                error="LEGACY_BRIDGE_RESTART_GAME",
                note="bridge v2 is required for formal commands",
                protocol_version=protocol_version,
                capabilities=capabilities,
            )
        build_required = required_build_capability(cmd)
        if build_required and not self.client_capabilities.supports(build_required):
            return BridgeResult(
                ok=False,
                cmd=cmd,
                status=int(BridgeStatus.ERROR),
                error=f"unsupported client build capability: {build_required}",
                protocol_version=protocol_version,
                capabilities=capabilities,
            )
        required = required_capability(cmd)
        if protocol_version >= PROTOCOL_VERSION and required and not (self.capabilities & required):
            return BridgeResult(
                ok=False,
                cmd=cmd,
                status=int(BridgeStatus.ERROR),
                error=f"bridge capability missing: {required.name}",
                protocol_version=protocol_version,
                capabilities=capabilities,
            )
        hwnd_use = int(hwnd or self._hwnd or self._u32(OFF_HWND) or 0)
        if hwnd_use:
            self._hwnd = hwnd_use

        prior_status = self._i32(OFF_STATUS)
        if prior_status == int(BridgeStatus.PENDING):
            prior_cmd = int(self._u32(OFF_CMD) or 0)
            # Only replace a stale diagnostic command with another diagnostic
            # command. A pending HOST_SNAPSHOT (or any business command) owns
            # the single shared slot until it completes; overwriting it with a
            # PING can race the native UI callback and crash the game.
            if prior_cmd in (int(CMD_PING), int(CMD_REHOOK)) and int(cmd) in (
                int(CMD_PING),
                int(CMD_REHOOK),
            ):
                self.log(
                    f"clear stale pending cmd={prior_cmd} before cmd={int(cmd)}"
                )
                self._set_i32(OFF_STATUS, int(BridgeStatus.IDLE))
            else:
                return BridgeResult(
                    ok=False,
                    cmd=int(cmd),
                    status=prior_status,
                    error=(
                        "previous bridge command is still pending; "
                        "refusing to overwrite shared arguments"
                    ),
                    note=self._err(),
                    hwnd=hwnd_use,
                    protocol_version=protocol_version,
                    capabilities=capabilities,
                )

        seq = (self._u32(OFF_SEQ) + 1) & 0xFFFFFFFF
        self._set_u32(OFF_SEQ, seq)
        self._set_u32(OFF_CMD, int(cmd))
        self._set_i32(OFF_RET, 0)
        if hwnd_use:
            self._set_u32(OFF_HWND, hwnd_use)
        self._set_f32(OFF_X, x)
        self._set_f32(OFF_Y, y)
        self._set_f32(OFF_Z, z)
        self._set_i32(OFF_MODE, mode)
        self._set_u32(OFF_ID_LO, id_lo)
        self._set_u32(OFF_ID_HI, id_hi)
        self._set_i32(OFF_TID, tid)
        ctypes.memset(self._view + OFF_ERR, 0, 128)
        if err_input:
            raw_input = str(err_input).encode("utf-8", "ignore")[:127]
            ctypes.memmove(self._view + OFF_ERR, raw_input, len(raw_input))
        # Publish last. The game timer treats PENDING as ownership transfer and
        # may immediately read every argument from this single shared slot.
        self._set_i32(OFF_STATUS, int(BridgeStatus.PENDING))

        # Optional wake: PostMessage is best-effort only (timer is primary).
        if hwnd_use:
            try:
                user32.PostMessageW(wintypes.HWND(hwnd_use), BRIDGE_WM, 0, 0)
            except Exception:
                pass

        deadline = time.monotonic() + (max(int(timeout_ms), 1) / 1000.0)
        soft_timeout_logged = False
        while True:
            status = self._i32(OFF_STATUS)
            if status in (ST_OK, 3):  # OK or ERR
                if protocol_version >= PROTOCOL_VERSION and self._u32(OFF_ACK_SEQ) != seq:
                    time.sleep(0.01)
                    continue
                ret = self._i32(OFF_RET)
                err = self._err()
                base = self._u32(OFF_BASE)
                return BridgeResult(
                    ok=(status == ST_OK),
                    cmd=cmd,
                    ret=ret,
                    status=status,
                    error=None if status == ST_OK else (err or f"status={status}"),
                    note=err,
                    hwnd=hwnd_use,
                    module_base=base,
                    protocol_version=protocol_version,
                    capabilities=capabilities,
                    x=self._f32(OFF_X),
                    y=self._f32(OFF_Y),
                    z=self._f32(OFF_Z),
                    mode=self._i32(OFF_MODE),
                    tid=self._i32(OFF_TID),
                    id_lo=self._u32(OFF_ID_LO),
                    id_hi=self._u32(OFF_ID_HI),
                )
            if time.monotonic() >= deadline and not soft_timeout_logged:
                soft_timeout_logged = True
                # PING / REHOOK are diagnostic / timer-arm helpers. If the UI
                # timer is not armed yet they stay PENDING forever — abandoning
                # them prevents one-click inject from hanging on "注入中".
                abandonable = int(cmd) in (int(CMD_PING), int(CMD_REHOOK))
                if abandonable:
                    self.log(
                        f"bridge cmd={int(cmd)} seq={seq} exceeded "
                        f"{int(timeout_ms)}ms; abandon soft-timeout "
                        f"(timer may be unarmed)"
                    )
                    self._set_i32(OFF_STATUS, int(BridgeStatus.IDLE))
                    err_code = (
                        "BRIDGE_PING_TIMEOUT"
                        if int(cmd) == int(CMD_PING)
                        else "BRIDGE_REHOOK_TIMEOUT"
                    )
                    return BridgeResult(
                        ok=False,
                        cmd=int(cmd),
                        status=int(BridgeStatus.IDLE),
                        error=err_code,
                        note=self._err(),
                        hwnd=hwnd_use,
                        protocol_version=protocol_version,
                        capabilities=capabilities,
                    )
                if int(cmd) == int(CMD_HOST_SNAPSHOT):
                    # Read-only safety probes must never pin a helper worker
                    # throughout a scene load.  Keep ST_PENDING intact: the
                    # game timer may still acknowledge this exact sequence,
                    # and no later caller is allowed to overwrite the slot.
                    self.log(
                        f"bridge host snapshot seq={seq} exceeded "
                        f"{int(timeout_ms)}ms; returning with pending preserved"
                    )
                    return BridgeResult(
                        ok=False,
                        cmd=int(cmd),
                        status=int(BridgeStatus.PENDING),
                        error="BRIDGE_SNAPSHOT_TIMEOUT",
                        note=self._err(),
                        hwnd=hwnd_use,
                        protocol_version=protocol_version,
                        capabilities=capabilities,
                    )
                self.log(
                    f"bridge cmd={int(cmd)} seq={seq} exceeded "
                    f"{int(timeout_ms)}ms; holding until completion"
                )
            if soft_timeout_logged and not _pid_alive(self.pid):
                return BridgeResult(
                    ok=False,
                    cmd=cmd,
                    error="game process exited while bridge command was pending",
                    hwnd=hwnd_use,
                    note=self._err(),
                    status=self._i32(OFF_STATUS),
                    protocol_version=protocol_version,
                    capabilities=capabilities,
                )
            time.sleep(0.02)

    def ui_click(
        self,
        cx: int,
        cy: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 2500,
        hold_ms: int = 0,
        button: int = UI_CLICK_LEFT,
    ) -> BridgeResult:
        """
        Main-thread PostMessage click at client (cx, cy).

        hold_ms: button hold duration on UI thread (0 = bridge default ~55ms).
        button: 0=left, 1=right (tid field).
        @author by ak
        """
        btn = UI_CLICK_RIGHT if int(button) == UI_CLICK_RIGHT else UI_CLICK_LEFT
        return self.call(
            CMD_UI_CLICK,
            id_lo=int(cx) & 0xFFFF,
            id_hi=int(cy) & 0xFFFF,
            mode=max(0, int(hold_ms)),
            tid=int(btn),
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def host_context(
        self,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 1200,
    ) -> BridgeResult:
        """Return ``ret=1`` when the game UI thread still owns a host role."""
        return self.call(
            CMD_HOST_CONTEXT,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def host_snapshot(
        self,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 1500,
    ) -> BridgeResult:
        """Sample host/scene/death on the UI thread; tid=-1/0/1 is unknown/alive/dead."""
        return self.call(
            CMD_HOST_SNAPSHOT,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def dump_ui_text(
        self,
        *,
        dlg: int = 2,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """
        Dump UTF-16 caption lines under a dungeon stage UI dialog.

        Runs on the game UI thread (bypasses the scene-settle CRT gate).
        dlg: 0=Win_InstanceConfig, 1=Win_TrackFrame, 2=both.
        The concatenated caption text is returned in ``error``/``note``.

        @author by ak
        """
        sel = int(dlg) if int(dlg) in (0, 1, 2) else 2
        return self.call(
            CMD_DUMP_UI_TEXT,
            id_lo=sel,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def autoplay_stop(
        self,
        reason: int = 0,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 4000,
    ) -> BridgeResult:
        """Run CECAutoPlay::StopAutoPlay on the game UI thread only."""
        return self.call(
            CMD_AUTOPLAY_STOP,
            id_lo=int(reason) & 0xFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def autoplay_start_bypass(
        self,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 5000,
    ) -> BridgeResult:
        """Run AutoPlayFrame.Btn_Start while bypassing skill/leader rejects."""
        return self.call(
            CMD_AUTOPLAY_START_BYPASS,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def autoplay_seed_follow(
        self,
        member_id: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 4000,
    ) -> BridgeResult:
        """Seed dungeon mode's cached follow target without changing leader."""
        oid = int(member_id or 0)
        if oid <= 0:
            return BridgeResult(
                ok=False,
                cmd=CMD_AUTOPLAY_SEED_FOLLOW,
                error="AUTOPLAY_FOLLOW_TARGET_ID_0",
            )
        return self.call(
            CMD_AUTOPLAY_SEED_FOLLOW,
            id_lo=oid & 0xFFFFFFFF,
            id_hi=(oid >> 32) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def skill_interrupt_67(
        self,
        skill_id: int,
        config_id: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """Stop the matching type-4 perform with the client's reason 0x67 path."""
        return self.call(
            CMD_SKILL_INTERRUPT_67,
            id_lo=int(skill_id) & 0xFFFFFFFF,
            id_hi=int(config_id) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def youfeng_internal_chain(
        self,
        skill_id: int,
        config_id: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """Start the guarded E07 action transaction without X/Space input."""
        return self.call(
            CMD_YOUFENG_INTERNAL_CHAIN,
            id_lo=int(skill_id) & 0xFFFFFFFF,
            id_hi=int(config_id) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def youfeng_next_gate(
        self,
        skill_id: int,
        config_id: int,
        *,
        mode: int = 1,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """Arm/read/close the bounded no-action same-skill gate window."""
        return self.call(
            CMD_YOUFENG_NEXT_GATE,
            id_lo=int(skill_id) & 0xFFFFFFFF,
            id_hi=int(config_id) & 0xFFFFFFFF,
            mode=int(mode),
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def youfeng_mash_cast(
        self,
        skill_id: int,
        config_id: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """Start the next matching skill through the native mash branch."""
        return self.call(
            CMD_YOUFENG_MASH_CAST,
            id_lo=int(skill_id) & 0xFFFFFFFF,
            id_hi=int(config_id) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def youfeng_fresh_gate(
        self,
        skill_id: int,
        config_id: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """Clear the completed cast identity and arm the normal-input gate."""
        return self.call(
            CMD_YOUFENG_FRESH_GATE,
            id_lo=int(skill_id) & 0xFFFFFFFF,
            id_hi=int(config_id) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def youfeng_exact_cancel_gate(
        self,
        skill_id: int,
        config_id: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """Cancel cast+0x6C's server perform, clear identity, and arm gate."""
        return self.call(
            CMD_YOUFENG_EXACT_CANCEL_GATE,
            id_lo=int(skill_id) & 0xFFFFFFFF,
            id_hi=int(config_id) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def youfeng_phase_gate(
        self,
        skill_id: int,
        config_id: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """Send only the target config's phase pair, clear identity, and gate."""
        return self.call(
            CMD_YOUFENG_PHASE_GATE,
            id_lo=int(skill_id) & 0xFFFFFFFF,
            id_hi=int(config_id) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def youfeng_qinggong_gate(
        self,
        skill_id: int,
        config_id: int,
        *,
        mode: int = 1,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """QingGong experiment: 1=hook, 2..4=pre-cast, 5..8=pulse, 10=snapshot."""
        return self.call(
            CMD_YOUFENG_QINGGONG_GATE,
            id_lo=int(skill_id) & 0xFFFFFFFF,
            id_hi=int(config_id) & 0xFFFFFFFF,
            mode=int(mode),
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def skill_action_experiment(
        self,
        skill_id: int,
        config_id: int,
        *,
        mode: int = 10,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """Identity probe; 6..9 cover KuangFeng, 11/12 arm/stop Youfeng ultimate tail."""
        return self.call(
            CMD_SKILL_ACTION_EXPERIMENT,
            id_lo=int(skill_id) & 0xFFFFFFFF,
            id_hi=int(config_id) & 0xFFFFFFFF,
            mode=int(mode),
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def ui_key(
        self,
        vk: int,
        *,
        action: int = UI_KEY_PRESS,
        hold_ms: int = 0,
        hwnd: int | None = None,
        timeout_ms: int = 2500,
        no_focus: bool = False,
    ) -> BridgeResult:
        """
        Main-thread key inject (background / minimized safe).

        action: 0=KEYDOWN, 1=KEYUP, 2=press (down+hold+up).
        hold_ms: only for action=press (0 = bridge default ~40ms).
        no_focus: set mode bit 0x1000 so DLL skips SetForegroundWindow.
        Escape always skips FG steal inside the DLL as well.
        @author by ak
        """
        act = int(action)
        if act not in (UI_KEY_DOWN, UI_KEY_UP, UI_KEY_PRESS):
            act = UI_KEY_PRESS
        mode = max(0, int(hold_ms)) & 0x0FFF
        if no_focus or (int(vk) & 0xFF) == 0x1B:
            mode |= 0x1000
        return self.call(
            CMD_UI_KEY,
            id_lo=int(vk) & 0xFF,
            id_hi=act & 0xFF,
            mode=mode,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def cancel_session(
        self,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """Queue the client's normal CancelSession request on its UI thread."""
        return self.call(
            CMD_CANCEL_SESSION,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def on_skill_stopped(
        self,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
        mode: int = 0,
    ) -> BridgeResult:
        """
        Lab-only: invoke client-side OnSkillStopped (mutating local flush).

        Calls CECHostSkillHdl::OnSkillStopped@0x75F750 in-game on the UI
        thread. ecx = cast-this from [host+0x1A88]. Native DLL snapshots the
        active identity (cast+0x10/+0x14/+0x18) into a zeroed event buffer so
        0x582350 can match and clear the identity block. Python never supplies
        the identity or any pointer.

        mode (lab variants, native g_shm->mode):
          0 = thin-wrapper (event,0,0,0,1) bContinueAtk=1  [default]
          1 = bContinueAtk=0 (also runs present clear 0x755280)
          2 = bContinueAtk=0 then 0x754BA0 clear skill-obj/+60..+7C
          3 = bContinueAtk=1 then 0x754BA0

        Late-window refill is driven by OnPerformSkill@0x761330 ->
        SetCurActiveSkill@0x758DF0 (reloads +10/+18, zeros +20). These modes
        only change local teardown; they do not promote to product.

        This mutates local cast-session state and does NOT send a packet.
        Animation/server may desync; restart the client after each experiment.
        Requires XAJH_ENABLE_UNSAFE_SKILL_WRITE=1, skill.probe profile, and a
        verified client build (enforced by Python caller).

        @author by ak
        """
        return self.call(
            CMD_ONSKILL_STOPPED,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
            mode=int(mode or 0),
        )

    def cast_skill(
        self,
        skill_id: int = 0,
        *,
        bar_slot: int | None = None,
        target_lo: int = 0,
        target_hi: int = 0,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """
        Cast a skill by config id via live outer path 0x53E110 -> 0x75F000.

        Lab primary use: generic recovery interrupt by casting the block skill
        (hand_probe X path, config id 0xE07 on tested Huashan) without VK.

        bar_slot is reserved (mode=1 unsupported). target_* unused for now.

        @author by ak
        """
        if bar_slot is not None:
            return BridgeResult(
                ok=False,
                cmd=CMD_CAST_SKILL,
                status=int(BridgeStatus.ERROR),
                error="CAST_SKILL_BAR_SLOT_UNSUPPORTED",
                note="only skill_id mode=0 is live (0x53E110)",
                protocol_version=self.protocol_version,
                capabilities=int(self.capabilities),
            )
        sid = int(skill_id or 0) & 0xFFFFFFFF
        if not sid:
            return BridgeResult(
                ok=False,
                cmd=CMD_CAST_SKILL,
                status=int(BridgeStatus.ERROR),
                error="CAST_SKILL_ID_0",
                note="skill_id required",
                protocol_version=self.protocol_version,
                capabilities=int(self.capabilities),
            )
        return self.call(
            CMD_CAST_SKILL,
            id_lo=sid,
            id_hi=int(target_hi or 0) & 0xFFFFFFFF,
            tid=int(target_lo or 0),
            mode=0,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def cast_jianglong_native(
        self,
        skill_id: int,
        *,
        action_type: int = 0x12607,
        action_tag: int = 0x2305,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """Use the complete client action pipeline for Jianglong, without a key or bar slot."""
        sid = int(skill_id or 0) & 0xFFFFFFFF
        if not sid:
            return BridgeResult(
                ok=False,
                cmd=CMD_CAST_SKILL,
                status=int(BridgeStatus.ERROR),
                error="CAST_JIANGLONG_ID_0",
                note="skill_id required",
                protocol_version=self.protocol_version,
                capabilities=int(self.capabilities),
            )
        return self.call(
            CMD_CAST_SKILL,
            id_lo=sid,
            id_hi=int(action_type) & 0xFFFFFFFF,
            tid=int(action_tag) & 0xFFFFFFFF,
            mode=2,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )
    def key_force(
        self,
        vk: int = 0x10,
        *,
        down: bool = True,
        level_only: bool = False,
        hwnd: int | None = None,
        timeout_ms: int = 2500,
    ) -> BridgeResult:
        """
        Force Shift held for free-aim reticle (background-safe).

        Path (no IAT): SoftSend LSHIFT so GetAsyncKeyState/GetModMask see
        down process-wide; key table + mod mask hold; one InjectKey edge on
        first arming only. level_only=True skips edges (timer keep-alive).
        @author by ak
        """
        return self.call(
            CMD_KEY_FORCE,
            id_lo=int(vk) & 0xFF,
            id_hi=1 if down else 0,
            mode=1 if level_only else 0,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def key_hold(
        self,
        vk: int = 0x20,
        *,
        down: bool = True,
        level_only: bool = False,
        allow_softsend: bool = False,
        install_only: bool = False,
        clear_all: bool = False,
        hwnd: int | None = None,
        timeout_ms: int = 2500,
    ) -> BridgeResult:
        """
        Solution-2 process-local key force (default NO SoftSend).

        Installs IAT GetAsyncKeyState/GetKeyState + inline IsKeyTable/GetModMask
        hooks, then forces vk in-process. Does not steal focus.

        mode bits: bit0=level_only, bit1=allow_softsend, bit2=install_only flag
        id_hi: 0=off 1=on 2=status 3=clear_all

        @author by ak
        """
        mode = 0
        if level_only:
            mode |= 1
        if allow_softsend:
            mode |= 2
        if install_only:
            mode |= 4
        if clear_all:
            action = 3
        elif install_only:
            action = 2
        else:
            action = 1 if down else 0
        return self.call(
            CMD_KEY_HOLD,
            id_lo=int(vk) & 0xFF,
            id_hi=int(action) & 0xFF,
            mode=int(mode),
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def key_trace(
        self,
        *,
        start: bool = True,
        vk: int = 0,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """
        Start/stop recording of GetAsyncKeyState/GetKeyState call sites.

        Hold real Shift while recording so reticle path is active; stop dumps
        a log under %%TEMP%%\\xajh_key_trace_<pid>.log (path in result.note).
        vk=0 filters Shift group (0x10/0xA0/0xA1).
        @author by ak
        """
        return self.call(
            CMD_KEY_TRACE,
            id_lo=int(vk) & 0xFF,
            id_hi=1 if start else 0,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def key_diag(
        self,
        *,
        start: bool = True,
        snapshot: bool = False,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """
        Message + controller free-aim diagnostics.

        start=True installs WH_GETMESSAGE, False removes + reports counts.
        snapshot=True returns immediate CheckModBind(1)/controller gate state.
        Non-mutating; safe to run alongside KEY_FORCE.
        @author by ak
        """
        return self.call(
            CMD_KEY_DIAG,
            id_hi=2 if snapshot else (1 if start else 0),
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def aq_submit(
        self,
        dlg_ptr: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """
        Main-thread thiscall CDlgActivityQuestion Btn_Ok submit (0x8CD2F0).

        @author by ak
        """
        return self.call(
            CMD_AQ_SUBMIT,
            id_lo=int(dlg_ptr) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def instance_enter(
        self,
        inst_id: int,
        *,
        difficulty: int = 0,
        flag: int = 1,
        hwnd: int | None = None,
        timeout_ms: int = 4000,
    ) -> BridgeResult:
        """
        Main-thread instance enter (副本列表 Btn_Enter path, no UI click).

        Disasm: host ids from GetHostSide()+0x140/0x144; this = GetNetRoot()
        (*global+0x2C)+0x1C8; thiscall 0xCC6F80 -> packet opcode 0x58.
        id_lo=inst_id, mode=difficulty, tid=flag.

        @author by ak
        """
        return self.call(
            CMD_INSTANCE_ENTER,
            id_lo=int(inst_id) & 0xFFFFFFFF,
            mode=int(difficulty),
            tid=int(flag),
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )



    def task_accept(
        self,
        task_id: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 4000,
    ) -> BridgeResult:
        """
        Main-thread accept task (jieTask). id_lo = task_id.

        @author by ak
        """
        return self.call(
            CMD_TASK_ACCEPT,
            id_lo=int(task_id) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def task_complete(
        self,
        task_id: int,
        *,
        npc_id_lo: int,
        npc_id_hi: int = 0,
        npc_ptr: int = 0,
        hwnd: int | None = None,
        timeout_ms: int = 5000,
    ) -> BridgeResult:
        """
        Main-thread complete task. mode=task_id; id_lo/hi=npc; tid=npc_ptr.

        @author by ak
        """
        return self.call(
            CMD_TASK_COMPLETE,
            id_lo=int(npc_id_lo) & 0xFFFFFFFF,
            id_hi=int(npc_id_hi) & 0xFFFFFFFF,
            mode=int(task_id) & 0xFFFFFFFF,
            tid=int(npc_ptr) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )



    def _object_scan_page(
        self,
        *,
        selector: int = 0,
        cursor: int = 0,
        tid: int = 0,
        name: str = "",
        timeout_ms: int = 4000,
    ) -> "BridgeResult":
        packed_mode = (int(cursor) << 8) | (int(selector) & 0xFF)
        return self.call(
            CMD_OBJECT_SCAN,
            mode=packed_mode,
            tid=int(tid),
            err_input=name if int(selector) == 2 else None,
            timeout_ms=int(timeout_ms),
        )

    def scan_objects(
        self,
        *,
        tid: int | None = None,
        name: str = "",
        limit: int = 512,
        timeout_ms: int = 4000,
    ) -> list["BridgeResult"]:
        """Return a read-only NPC/Matter snapshot through the native bridge.

        No filter returns all objects. ``tid`` filters by template ID and
        ``name`` filters by UTF-8 name substring. The bridge pages results with
        a cursor so callers do not create repeated foreign CRT calls.
        """
        if tid is not None and name:
            raise ValueError("scan_objects accepts tid or name, not both")
        selector = 2 if name else 1 if tid is not None else 0
        rows: list[BridgeResult] = []
        cursor = 0
        max_rows = max(1, min(int(limit), 4096))
        for _ in range(max_rows):
            result = self._object_scan_page(
                selector=selector,
                cursor=cursor,
                tid=int(tid or 0),
                name=name,
                timeout_ms=timeout_ms,
            )
            if not result.ok:
                raise RuntimeError(result.error or result.note or "OBJECT_SCAN failed")
            if int(result.ret or 0) == 0:
                break
            rows.append(result)
            next_cursor = int(result.mode or 0)
            if next_cursor <= cursor:
                break
            cursor = next_cursor
        return rows

    def npc_talk_select(
        self,
        opt_id: int,
        *,
        dlg_ptr: int = 0,
        opt_type: int = 0,
        hwnd: int | None = None,
        timeout_ms: int = 4000,
    ) -> BridgeResult:
        """
        UI-thread AA3330 talk option select.

        id_lo=opt_id (talk_proc entry id), mode=opt_type (0 keep / 2 select),
        tid=dlg_ptr (0 = DLL resolves Win_NPCContent).

        @author by ak
        """
        return self.call(
            CMD_NPC_TALK_SELECT,
            id_lo=int(opt_id) & 0xFFFFFFFF,
            mode=int(opt_type) & 0xFFFFFFFF,
            tid=int(dlg_ptr or 0) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )


    def npc_host_select(
        self,
        index: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 4000,
    ) -> BridgeResult:
        """
        UI-thread host service row activate via AA5590(index, 0).

        Used when talk_proc is empty (L1 with unfinished NPC tasks): options live
        in *0x19561F0 host table / Txt_Template{i}, not dlg+0x16c talk_proc.

        id_lo = row index.
        """
        return self.call(
            CMD_NPC_HOST_SELECT,
            id_lo=int(index) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def team_invite(
        self,
        id_lo: int,
        id_hi: int = 0,
        *,
        timeout_ms: int = 4000,
        timeout_s: float | None = None,
    ) -> "BridgeResult":
        """
        Main-thread TeamInvite (zeroed 0x1261 / CD04D0).

        id_lo/id_hi = invitee role id.
        timeout_s kept for backward compat (converted to ms).
        @author by ak
        """
        if timeout_s is not None:
            timeout_ms = int(float(timeout_s) * 1000.0)
        return self.call(
            CMD_TEAM_INVITE,
            id_lo=int(id_lo) & 0xFFFFFFFF,
            id_hi=int(id_hi) & 0xFFFFFFFF,
            timeout_ms=int(timeout_ms),
        )

    def quick_team_follow(
        self,
        target_id: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 4000,
    ) -> "BridgeResult":
        """Run right-panel specified follow on the game UI thread."""
        oid = int(target_id or 0) & 0xFFFFFFFFFFFFFFFF
        if oid <= 0:
            return BridgeResult(
                ok=False,
                cmd=CMD_QUICK_TEAM_FOLLOW,
                error="QUICK_TEAM_FOLLOW_TARGET_ID_0",
            )
        return self.call(
            CMD_QUICK_TEAM_FOLLOW,
            id_lo=oid & 0xFFFFFFFF,
            id_hi=(oid >> 32) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=int(timeout_ms),
        )

    def team_follow(
        self,
        enabled: bool = True,
        *,
        timeout_ms: int = 4000,
    ) -> "BridgeResult":
        """Run the game's team-follow confirm callback on the UI thread."""
        return self.call(
            CMD_TEAM_FOLLOW,
            id_lo=1 if enabled else 0,
            timeout_ms=int(timeout_ms),
        )

    def use_item_in_package(
        self,
        package_index: int,
        slot: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> BridgeResult:
        """
        Main-thread plg::UseItemInPackage(pack, slot) via UI timer.

        Same game path as bag right-click — not CreateRemoteThread.
        id_lo=package_index, id_hi=slot.

        @author by ak
        """
        return self.call(
            CMD_USE_ITEM_IN_PACKAGE,
            id_lo=int(package_index) & 0xFFFFFFFF,
            id_hi=int(slot) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=timeout_ms,
        )

    def item_use_trace(
        self,
        package_index: int,
        slot: int,
        *,
        mode: int = 2,
        timeout_ms: int = 1500,
    ) -> "BridgeResult":
        """Start/read/stop the native successful-UseItem counter for one slot."""
        return self.call(
            CMD_ITEM_USE_TRACE,
            id_lo=int(package_index) & 0xFFFFFFFF,
            id_hi=int(slot) & 0xFFFFFFFF,
            mode=int(mode),
            timeout_ms=int(timeout_ms),
        )


    def cg_skip(
        self,
        *,
        mode: int = 2,
        timeout_ms: int = 1500,
    ) -> "BridgeResult":
        """Arm/disarm/status the passive CG skip hooks.

        mode: 1=arm+install, 2=status, 0=disarm (hooks remain, skip off).
        Passive path: PlayCG / PlayBlackEdge fire then native stop once.
        """
        return self.call(
            CMD_CG_SKIP,
            mode=int(mode),
            timeout_ms=int(timeout_ms),
        )

    def session_send_bypass(
        self,
        *,
        mode: int = 2,
        timeout_ms: int = 1500,
    ) -> "BridgeResult":
        """Arm/disarm/status the SessionSend send-rate gate bypass.

        mode: 1=arm (patch 0xCC6D51 jae->jmp), 2=status, 0=disarm (restore).
        Signature-verified in native; a changed client build fails closed.
        """
        return self.call(
            CMD_SESSION_SEND_BYPASS,
            mode=int(mode),
            timeout_ms=int(timeout_ms),
        )

    def dummy_damage_stat(
        self,
        target_id: int,
        *,
        mode: int = 2,
        timeout_ms: int = 1500,
    ) -> "BridgeResult":
        """Start, read, or stop the passive 60-second target damage counter."""
        oid = int(target_id) & 0xFFFFFFFFFFFFFFFF
        return self.call(
            CMD_DUMMY_DAMAGE_STAT,
            id_lo=oid & 0xFFFFFFFF,
            id_hi=(oid >> 32) & 0xFFFFFFFF,
            mode=int(mode),
            timeout_ms=int(timeout_ms),
        )

    def combat_monitor(
        self,
        *,
        mode: int = 2,
        timeout_ms: int = 1500,
    ) -> "BridgeResult":
        """Arm/read/disarm the read-only dungeon combat activity counters.

        mode: 1=arm/reset (installs SkillActionRequest + damage splitter
        detours), 2=status (refreshes host attack target), 0=disarm (detours
        remain but counting stops). ret=attack_action_seq, tid=self_damage_seq;
        the note string carries attack_seq/self_damage_seq/self_damage_total/
        host_target_lo/hi/resolved for parse_combat_monitor_note().
        """
        return self.call(
            CMD_COMBAT_MONITOR,
            mode=int(mode),
            timeout_ms=int(timeout_ms),
        )

    def target_submit_trace(
        self,
        *,
        mode: int = 2,
        timeout_ms: int = 1500,
    ) -> "BridgeResult":
        """Trace native selected-target writes and control its narrow guard.

        mode=1 installs/signature-checks the 0x7493B0 submit and 0x73E300
        queued-promotion traces and resets their shared 64-entry ring; mode=2
        reads the newest sample; mode=0 stops recording and writes the ring to
        a TSV. mode=3 additionally enables the native dungeon guard for the
        confirmed automatic routes; mode=4 disables that guard without changing
        trace state. The returned ``id_lo/id_hi`` are the newest target id and
        ``mode`` is its caller address.
        """
        return self.call(
            CMD_TARGET_SUBMIT_TRACE,
            mode=int(mode),
            timeout_ms=int(timeout_ms),
        )

    def dungeon_target_rules(
        self,
        *,
        mode: int = 3,
        tid: int = 0,
        timeout_ms: int = 1500,
    ) -> "BridgeResult":
        """Configure/read the native dungeon TID deny set.

        mode=1 clears the set, mode=2 adds ``tid`` (id_lo), mode=3 reads
        status. This is a startup/configuration operation; target hooks only
        consult the in-process array afterwards.
        """
        return self.call(
            CMD_DUNGEON_TARGET_RULES,
            id_lo=int(tid) & 0xFFFFFFFF,
            mode=int(mode),
            timeout_ms=int(timeout_ms),
        )

    def probe_jianglong_runtime_candidate(
        self,
        ordinal: int,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> "BridgeResult":
        """Read one loaded skill record for the standalone Jianglong probe."""
        return self.call(
            CMD_JIANGLONG_RUNTIME_RESOLVE,
            mode=1,
            id_lo=max(1, int(ordinal)),
            hwnd=hwnd,
            timeout_ms=int(timeout_ms),
        )
    def resolve_jianglong_runtime_config(
        self,
        *,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> "BridgeResult":
        """Read this character's loaded Jianglong skill without input or send."""
        return self.call(
            CMD_JIANGLONG_RUNTIME_RESOLVE,
            hwnd=hwnd,
            timeout_ms=int(timeout_ms),
        )
    def skill_action_trace(
        self,
        *,
        mode: int = 2,
        skill_id: int = 0,
        config_id: int = 0,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
    ) -> "BridgeResult":
        """
        Start/read/stop the short-lived action-request + active-session trace.

        mode=1 traces only; mode=3 suppresses matching 0x76159A refills;
        mode=4 additionally keeps suppression across a matching multi-stage or
        charge/channel continuation (session start while the old target cast
        identity is still installed). mode=2
        returns counters and mode=0 dumps a TSV path before cleanup. It records
        SkillActionRequest@0x762F10, OnPerformSkill@0x761330, and
        SetCurActiveSkill@0x758DF0. It never initiates a cast.
        """
        return self.call(
            CMD_SKILL_ACTION_TRACE,
            mode=int(mode),
            id_lo=int(skill_id) & 0xFFFFFFFF,
            id_hi=int(config_id) & 0xFFFFFFFF,
            hwnd=hwnd,
            timeout_ms=int(timeout_ms),
        )


def classify_bridge_fail(
    *,
    pre: dict | None = None,
    note: str = "",
    alive: bool = True,
    inject_ok: bool | None = None,
    shm_open: bool = False,
    ping_err: str = "",
) -> str:
    """
    Stable fail code for logs / UI (one line diagnosis).

    @author by ak
    """
    note = note or ""
    ping_err = ping_err or ""
    if pre and pre.get("fail_code"):
        return str(pre["fail_code"])
    if not alive:
        return "GAME_CRASH"
    if inject_ok is False:
        return "INJECT_EXE_FAIL"
    if "SetWindowLongPtr fail" in note:
        return "HOOK_SETWNDPROC_FAIL"
    if "timer InstallHook SEH" in note or "attach SEH" in note or "dispatch timer SEH" in note:
        return "ATTACH_SEH"
    if "SetTimer fail" in note:
        return "HOOK_SETTIMER_FAIL"
    if "hook: hwnd other pid" in note or "hwnd other pid" in note or "timer: hwnd other pid" in note:
        return "HOOK_HWND_PID"
    if "hook timeout" in note or "timer timeout" in note:
        return "HOOK_TIMEOUT"
    if "no hwnd" in note or "enum failed" in note or "timer: no hwnd" in note:
        return "HOOK_NO_HWND"
    if (
        "await rehook" in note
        or "await hwnd" in note
        or "timer armed" in note
        or "timer slow" in note
    ) and shm_open:
        return "HOOK_NOT_INSTALLED"
    if (
        "BRIDGE_PING_TIMEOUT" in ping_err
        or "wait timeout" in ping_err
        or "WndProc not hooked" in ping_err
        or "UI timer not armed" in ping_err
    ):
        return "BRIDGE_PING_TIMEOUT" if "BRIDGE_PING_TIMEOUT" in ping_err else "PING_TIMEOUT_NO_HOOK"
    if "PostMessage failed" in ping_err:
        return "POSTMESSAGE_FAIL"
    if shm_open and not note:
        return "SHM_OPEN_EMPTY_NOTE"
    if not shm_open and inject_ok:
        return "SHM_NOT_OPEN"
    return "UNKNOWN"


# Last ensure_bridge result meta for inject_gate fast-path (no protocol change).
_LAST_ENSURE_META: dict[int, dict] = {}
_ENSURE_LOCKS: dict[int, object] = {}
_ENSURE_LOCKS_GUARD = threading.RLock()


def _ensure_lock(pid: int):
    with _ENSURE_LOCKS_GUARD:
        lock = _ENSURE_LOCKS.get(int(pid))
        if lock is None:
            lock = threading.RLock()
            _ENSURE_LOCKS[int(pid)] = lock
        return lock


def last_ensure_meta(pid: int, *, clear: bool = False) -> dict:
    """
    Meta from last ensure_bridge for this pid: reused / did_inject / pinged.

    @author by ak
    """
    key = int(pid)
    if clear:
        return dict(_LAST_ENSURE_META.pop(key, {}) or {})
    return dict(_LAST_ENSURE_META.get(key, {}) or {})


def ensure_bridge(
    pid: int,
    **kwargs,
) -> XajhBridge | None:
    """Open or recover one PID bridge with single-flight serialization."""
    lock = _ensure_lock(int(pid))
    with lock:
        return _ensure_bridge_unlocked(int(pid), **kwargs)


def _ensure_bridge_unlocked(
    pid: int,
    *,
    log: LogFn | None = None,
    inject_if_needed: bool = True,
    hwnd: int | None = None,
    force_reinject: bool = False,
    attach_mode: int = 0,
    wait_tries: int | None = None,
) -> XajhBridge | None:
    """
    Open shared-memory bridge for pid (background/minimized safe).

    Semantics (important for minimized loops):
      - force_reinject is retained for API compatibility but never hot-unloads
        a loaded DLL; bridge upgrades require restarting the game process
      - inject_if_needed=True  -> reuse existing bridge if open; inject only if missing
      - inject_if_needed=False -> open existing only; never inject
      - attach_mode: written into shm before inject (0=standard, 0x4743=green)

    Hot paths (captcha capture / loot move / UI click) must NOT force reinject
    every call — that tears down WndProc mid-business.
    Quiting the helper must never unload the game bridge.
    @author by ak
    """
    log = log or (lambda _m: None)
    _ENSURE_BRIDGE_FAILURES.pop(int(pid), None)
    _LAST_ENSURE_META.pop(int(pid), None)
    hwnd_i = int(hwnd or 0)
    attach_mode_i = int(attach_mode or 0)
    # Green clients need longer poll; standard keeps historical 50 tries.
    max_tries = int(wait_tries) if wait_tries is not None else (
        80 if attach_mode_i == 0x4743 else 50
    )

    def _try_open(*, quiet: bool = False) -> XajhBridge | None:
        b = XajhBridge(pid, log=log)
        if not b.open(quiet=quiet):
            return None
        if hwnd_i:
            try:
                b._set_u32(OFF_HWND, hwnd_i)
                b._hwnd = hwnd_i
            except Exception:
                pass
        return b

    existing = _try_open()
    if existing is not None:
        # Health-check: stale shm after crash/partial inject must not block reinject.
        note0 = ""
        try:
            note0 = existing._err()
        except Exception:
            note0 = ""
        healthy = False
        if existing.protocol_version < PROTOCOL_VERSION:
            _ENSURE_BRIDGE_FAILURES[int(pid)] = "LEGACY_BRIDGE_RESTART_GAME"
            log("legacy bridge detected; restart game required for v2")
            try:
                existing.close()
            except Exception:
                pass
            return None
        pr = None
        try:
            if hwnd_i:
                existing._set_u32(OFF_HWND, hwnd_i)
                existing._hwnd = hwnd_i
            pr = existing.call(
                CMD_PING, hwnd=hwnd_i or None, timeout_ms=1200
            )
            healthy = bool(pr.ok)
            if not healthy:
                log(
                    f"existing bridge ping: ok={pr.ok} note={pr.note!r} "
                    f"err={pr.error} shm_note={note0!r}"
                )
        except Exception as e:
            log(f"existing bridge ping skip: {e}")

        # One-click background inject often leaves a loaded DLL whose timer is
        # not armed yet / hwnd was wrong. Before fail-closed "restart game",
        # rewrite hwnd and retry PING for a short recovery window.
        if not healthy:
            loaded_probe = bool(
                existing._u32(OFF_BASE)
                or existing.protocol_version
                or note0
            )
            if loaded_probe:
                log(
                    "existing bridge unhealthy; try hwnd/timer recovery "
                    f"note={note0!r}"
                )
                for attempt in range(1, 9):
                    if not _pid_alive(pid):
                        break
                    # Best-effort refresh hwnd from process windows.
                    if not hwnd_i or attempt in (3, 6):
                        try:
                            import ctypes
                            from ctypes import wintypes

                            user32 = ctypes.WinDLL("user32", use_last_error=True)
                            best = 0
                            best_area = -1

                            @ctypes.WINFUNCTYPE(
                                wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
                            )
                            def _enum(h, _lp):
                                nonlocal best, best_area
                                proc = wintypes.DWORD(0)
                                user32.GetWindowThreadProcessId(
                                    h, ctypes.byref(proc)
                                )
                                if int(proc.value) != int(pid):
                                    return True
                                if not user32.IsWindowVisible(h):
                                    return True
                                class RECT(ctypes.Structure):
                                    _fields_ = [
                                        ("left", wintypes.LONG),
                                        ("top", wintypes.LONG),
                                        ("right", wintypes.LONG),
                                        ("bottom", wintypes.LONG),
                                    ]

                                rc = RECT()
                                if user32.GetClientRect(h, ctypes.byref(rc)):
                                    area = max(0, int(rc.right - rc.left)) * max(
                                        0, int(rc.bottom - rc.top)
                                    )
                                else:
                                    area = 0
                                cbuf = ctypes.create_unicode_buffer(256)
                                user32.GetClassNameW(h, cbuf, 256)
                                cls = (cbuf.value or "").lower()
                                score = area
                                if "xajh" in cls:
                                    score += 10_000_000
                                if score > best_area:
                                    best_area = score
                                    best = int(h)
                                return True

                            user32.EnumWindows(_enum, 0)
                            if best:
                                hwnd_i = int(best)
                        except Exception as e:
                            log(f"recover hwnd enum err: {e}")
                    if hwnd_i:
                        try:
                            existing._set_u32(OFF_HWND, hwnd_i)
                            existing._hwnd = hwnd_i
                        except Exception:
                            pass
                    try:
                        note_now = existing._err()
                    except Exception:
                        note_now = ""
                    low = (note_now or "").lower()
                    timer_ready = (
                        "bridge ready" in low
                        or "rehook ok" in low
                        or " already" in low
                    )
                    # REHOOK itself needs the dispatch timer. Calling it before
                    # ready used to hang inject forever (PENDING never completes).
                    if timer_ready and hwnd_i:
                        try:
                            rh = existing.call(
                                CMD_REHOOK, hwnd=hwnd_i, timeout_ms=1500
                            )
                            log(
                                f"recover rehook#{attempt} ok={rh.ok} "
                                f"note={rh.note!r}"
                            )
                        except Exception as e:
                            log(f"recover rehook#{attempt} err={e}")
                    elif not timer_ready:
                        log(
                            f"recover wait timer#{attempt} note={note_now!r} "
                            f"hwnd=0x{int(hwnd_i or 0):X}"
                        )
                    try:
                        pr = existing.call(
                            CMD_PING,
                            hwnd=hwnd_i or None,
                            timeout_ms=2000 if attempt < 4 else 3500,
                        )
                        log(
                            f"recover ping#{attempt} ok={pr.ok} "
                            f"err={pr.error!r} note={pr.note!r}"
                        )
                        if pr.ok:
                            healthy = True
                            break
                    except Exception as e:
                        log(f"recover ping#{attempt} err={e}")
                    time.sleep(0.35)

        if healthy:
            if int(pr.ret or 0) != int(BRIDGE_BUILD_ID):
                _ENSURE_BRIDGE_FAILURES[int(pid)] = "STALE_BRIDGE_RESTART_GAME"
                log(
                    "stale bridge build detected: "
                    f"loaded={pr.ret!r} required={BRIDGE_BUILD_ID}; "
                    "restart game required"
                )
                try:
                    existing.close()
                except Exception:
                    pass
                return None
            if force_reinject:
                log(
                    "force_reinject ignored for safety; reuse healthy bridge "
                    "and restart game to upgrade DLL"
                )
            _LAST_ENSURE_META[int(pid)] = {
                "reused": True,
                "did_inject": False,
                "pinged": True,
                "ping_ret": int(pr.ret or 0),
            }
            return existing

        loaded_marker = bool(
            existing._u32(OFF_BASE)
            or existing.protocol_version
            or note0
        )
        if loaded_marker:
            _ENSURE_BRIDGE_FAILURES[int(pid)] = (
                "LOADED_BRIDGE_UNHEALTHY_RESTART_GAME"
            )
            log(
                "existing loaded bridge is unhealthy; hot reload disabled, "
                f"restart game required note={note0!r}"
            )
            try:
                existing.close()
            except Exception:
                pass
            return None

        # Helper-prepared mapping with no loaded DLL marker: safe to inject once.
        try:
            existing.close()
        except Exception:
            pass

    # Need inject only when no loaded bridge exists.
    if not inject_if_needed:
        _ENSURE_BRIDGE_FAILURES.setdefault(int(pid), "BRIDGE_NOT_LOADED")
        return None

    log(
        f"ensure_bridge: calling inject_bridge pid={pid} hwnd=0x{hwnd_i:X} "
        f"attach_mode=0x{attach_mode_i & 0xFFFF:X}"
    )
    if not inject_bridge(
        pid, log=log, hwnd=hwnd_i or None, attach_mode=attach_mode_i
    ):
        # Maybe unload failed but old bridge still usable
        old = _try_open()
        if old is not None:
            log("inject failed; falling back to existing bridge")
            _LAST_ENSURE_META[int(pid)] = {
                "reused": True,
                "did_inject": False,
                "pinged": False,
            }
            return old
        code = last_inject_failure(pid, clear=True) or classify_bridge_fail(
            alive=_pid_alive(pid), inject_ok=False, shm_open=False
        )
        _ENSURE_BRIDGE_FAILURES[int(pid)] = code or "INJECT_EXE_FAIL"
        log(f"ensure_bridge fail_code={code}")
        return None

    if not _pid_alive(pid):
        log("abort: game died during inject fail_code=GAME_CRASH")
        return None

    # AttachThreadProc opens shm then arms UI-thread SetTimer (no WndProc).
    # Python must write hwnd ASAP so timer can arm and mark bridge ready.
    b2: XajhBridge | None = None
    last_note = ""
    for i in range(max_tries):
        if not _pid_alive(pid):
            log(f"game died while waiting shm try={i + 1} fail_code=GAME_CRASH")
            if b2 is not None:
                try:
                    b2.close()
                except Exception:
                    pass
            return None
        time.sleep(0.08 if i else 0.12)
        # Re-open each try so we always map a fresh view; close previous.
        if b2 is not None:
            try:
                b2.close()
            except Exception:
                pass
            b2 = None
        quiet = i != 0 and (i + 1) % 5 != 0
        b2 = _try_open(quiet=quiet)
        if b2 is None:
            if i == 0 or (i + 1) % 5 == 0:
                log(f"wait bridge shm... try={i + 1}/{max_tries}")
            continue
        if hwnd_i:
            try:
                b2._set_u32(OFF_HWND, hwnd_i)
                b2._hwnd = hwnd_i
            except Exception as e:
                log(f"set hwnd failed: {e}")
        last_note = b2._err()
        if i == 0 or (i + 1) % 5 == 0 or "bridge ready" in last_note:
            log(f"bridge shm open try={i + 1} note={last_note!r}")
        # Timer armed + ready (standard or green marker).
        ready = (
            "bridge ready" in last_note
            or "rehook ok" in last_note
            or "already" in last_note
        )
        if ready:
            log(f"bridge ready note={last_note!r}")
            _LAST_ENSURE_META[int(pid)] = {
                "reused": False,
                "did_inject": True,
                "pinged": False,
            }
            return b2
        hard = (
            "SetTimer fail" in last_note
            or "attach SEH" in last_note
            or "dispatch timer SEH" in last_note
        )
        if hard and i >= 5:
            code = classify_bridge_fail(
                note=last_note, alive=True, inject_ok=True, shm_open=True
            )
            log(f"ensure_bridge hard fail note={last_note!r} fail_code={code}")
            _LAST_ENSURE_META[int(pid)] = {
                "reused": False,
                "did_inject": True,
                "pinged": False,
            }
            return b2
        if i == 0 or (i + 1) % 5 == 0:
            log(f"wait bridge ready... try={i + 1} note={last_note!r}")
    if b2 is None:
        code = classify_bridge_fail(
            note=last_note, alive=_pid_alive(pid), inject_ok=True, shm_open=False
        )
        log(f"inject claimed ok but shared memory not open fail_code={code}")
        return None

    note = b2._err()
    code = classify_bridge_fail(
        note=note, alive=_pid_alive(pid), inject_ok=True, shm_open=True
    )
    # Soft accept: shm open + hwnd written is enough for timer path; PING will
    # confirm. Avoid PostMessage-dependent REHOOK chicken-egg.
    if hwnd_i and ("await" in note or "no hwnd" in note or not note):
        log(f"ready wait soft-timeout note={note!r} fail_code={code}; keep bridge")
    else:
        log(f"warn: bridge open note={note!r} fail_code={code}")
    _LAST_ENSURE_META[int(pid)] = {
        "reused": False,
        "did_inject": True,
        "pinged": False,
    }
    return b2
