# -*- coding: utf-8 -*-
"""
plg AUI dialog state: open/show checks from process memory exports.

Uses existing remote cdecl calls (same as automove). Dialog existence is
queried via GetGameUIDlg + IsDlgShow, not screenshot/timer heuristics.

@author by ak
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from dataclasses import asdict, dataclass
from typing import Callable

from app.core.remote_runtime import (
    MEM_COMMIT,
    MEM_RELEASE,
    MEM_RESERVE,
    PAGE_EXECUTE_READWRITE,
    WAIT_OBJECT_0,
    _open_process,
    _rpm,
    _wpm,
    kernel32,
    remote_call_cdecl_x86,
)
from app.core.plg_exports import (
    EXPORT_GET_DLG_NAME,
    EXPORT_GET_GAME_STATE,
    EXPORT_GET_GAME_UI_DLG,
    EXPORT_GET_GAME_UI_DLG_NUM,
    EXPORT_GET_GAME_UI_DLGS_NAME,
    EXPORT_GET_HOST_PLAYER,
    EXPORT_GET_HOST_PLAYER_TEAM,
    EXPORT_IS_DLG_SHOW,
    find_xajh_exe,
    resolve_export_rva,
)

LogFn = Callable[[str], None]


def _pid_blocked(session) -> tuple[bool, str]:
    try:
        from app.core.safe_dispatch import session_blocked

        return session_blocked(session)
    except Exception:
        return False, ""


def _note_remote_hard(session, exc: BaseException | str) -> None:
    try:
        pid = int(getattr(session, "pid", 0) or 0)
        if pid <= 0:
            return
        from app.core.safe_dispatch import get_dispatch

        if isinstance(exc, BaseException):
            get_dispatch().note_exception(pid, exc)
        else:
            get_dispatch().note_exception(pid, OSError(str(exc)))
    except Exception:
        pass


# Live captcha for 活动限时答题 / 九层妖楼 entry (CDlgActivityQuestion).
# Confirmed live via GetGameUIDlgsName + GetGameUIDlg (2026-07-16):
#   Win_ActivityQuestion  -> valid AUIDialog*
#   Win_Question / Win_Question3D also exist (other question UIs).
# Bare names without Win_ return null.
CAPTCHA_DIALOG_NAME_CANDIDATES = (
    "Win_ActivityQuestion",
    "Win_Question",
    "Win_Question3D",
)


@dataclass
class DlgShowResult:
    """
    One dialog open/show query.

    @author by ak
    """

    ok: bool
    name: str = ""
    shown: bool = False
    dlg_ptr: int = 0
    error: str | None = None
    method: str = "GetGameUIDlg+IsDlgShow"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DlgListResult:
    """
    Snapshot of UI dialog names known to the game.

    @author by ak
    """

    ok: bool
    names: list[str]
    count: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _resolve_va(session, export_name: str) -> int:
    """Resolve plg export to live VA. @author by ak"""
    base = getattr(session, "module_base", None)
    if not base:
        raise RuntimeError("session has no module_base; attach first")
    pe = find_xajh_exe(getattr(session, "exe_path", None))
    if pe is None:
        raise RuntimeError("cannot locate xajh.exe for export parse")
    rva = resolve_export_rva(pe, export_name)
    if rva is None:
        raise RuntimeError(f"export not found: {export_name}")
    return int(base) + int(rva)


def remote_alloc_bytes(
    pid: int,
    data: bytes,
    *,
    extra: int = 0,
) -> tuple[int, int, int]:
    """
    Alloc remote RWX, write data. Returns (handle, addr, size).

    Caller must close handle and free addr.
    @author by ak
    """
    h = _open_process(pid)
    size = len(data) + max(0, int(extra))
    size = max(size, 16)
    remote = int(
        kernel32.VirtualAllocEx(
            wintypes.HANDLE(h),
            None,
            size,
            MEM_COMMIT | MEM_RESERVE,
            PAGE_EXECUTE_READWRITE,
        )
        or 0
    )
    if not remote:
        kernel32.CloseHandle(wintypes.HANDLE(h))
        raise OSError(f"VirtualAllocEx failed err={ctypes.get_last_error()}")
    try:
        _wpm(h, remote, data + (b"\x00" * max(0, size - len(data))))
    except Exception:
        kernel32.VirtualFreeEx(
            wintypes.HANDLE(h), ctypes.c_void_p(remote), 0, MEM_RELEASE
        )
        kernel32.CloseHandle(wintypes.HANDLE(h))
        raise
    return h, remote, size


def remote_free(h: int, addr: int) -> None:
    """Free remote alloc and process handle. @author by ak"""
    try:
        if addr:
            kernel32.VirtualFreeEx(
                wintypes.HANDLE(h), ctypes.c_void_p(addr), 0, MEM_RELEASE
            )
    finally:
        if h:
            kernel32.CloseHandle(wintypes.HANDLE(h))


def read_remote_c_string(
    pid: int,
    addr: int,
    *,
    max_len: int = 128,
    encoding: str = "ascii",
) -> str | None:
    """
    Read null-terminated C string from remote process.

    @author by ak
    """
    if not addr:
        return None
    h = _open_process(pid)
    try:
        raw = _rpm(h, int(addr) & 0xFFFFFFFF, max(1, int(max_len)))
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h))
    if not raw:
        return ""
    end = raw.find(b"\x00")
    if end >= 0:
        raw = raw[:end]
    if not raw:
        return ""
    for enc in (encoding, "gbk", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("latin-1", "ignore")


def get_game_state(session, *, log: LogFn | None = None) -> int | None:
    """
    Call plg::GetGameState() -> int.

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        va = _resolve_va(session, EXPORT_GET_GAME_STATE)
        ret = remote_call_cdecl_x86(
            int(session.pid), va, [], skip_scene_gate=True
        )
        return int(ret)
    except Exception as e:
        log(f"GetGameState err: {e}")
        return None


def get_game_ui_dlg(
    session,
    name: str,
    *,
    log: LogFn | None = None,
) -> int:
    """
    Call plg::GetGameUIDlg(const char* name) -> AUIDialog*.

    Returns 0 if missing / error.
    @author by ak
    """
    log = log or (lambda _m: None)
    name = (name or "").strip()
    if not name:
        return 0
    pid = int(session.pid)
    h = 0
    remote = 0
    try:
        va = _resolve_va(session, EXPORT_GET_GAME_UI_DLG)
        # name as remote C string (ASCII dialog ids)
        payload = name.encode("ascii", "ignore") + b"\x00"
        h, remote, _sz = remote_alloc_bytes(pid, payload)
        # temporary: free only after call; remote_call opens its own handle
        # Keep alloc alive across call: pass handle free after
        # remote_call_cdecl_x86 uses separate OpenProcess — OK
        ptr = int(remote) & 0xFFFFFFFF
        ret = remote_call_cdecl_x86(
            pid, va, [ptr], skip_scene_gate=True
        )
        return int(ret) & 0xFFFFFFFF
    except Exception as e:
        log(f"GetGameUIDlg({name!r}) err: {e}")
        return 0
    finally:
        if h and remote:
            remote_free(h, remote)
        elif h:
            kernel32.CloseHandle(wintypes.HANDLE(h))


def is_dlg_show(
    session,
    dlg_ptr: int,
    *,
    log: LogFn | None = None,
) -> bool:
    """
    Call plg::IsDlgShow(AUIDialog*) -> bool.

    @author by ak
    """
    log = log or (lambda _m: None)
    ptr = int(dlg_ptr) & 0xFFFFFFFF
    if not ptr:
        return False
    try:
        pid_raw = getattr(session, "pid", None)
        if pid_raw is None:
            log(f"IsDlgShow(0x{ptr:X}) skip: session.pid is None")
            return False
        va = _resolve_va(session, EXPORT_IS_DLG_SHOW)
        ret = remote_call_cdecl_x86(
            int(pid_raw), va, [ptr], skip_scene_gate=True
        )
        return bool(int(ret) & 0xFF)
    except Exception as e:
        log(f"IsDlgShow(0x{ptr:X}) err: {e}")
        return False


def get_dlg_name(
    session,
    dlg_ptr: int,
    *,
    log: LogFn | None = None,
) -> str | None:
    """
    Call plg::GetDlgName(AUIDialog*) -> const char*.

    @author by ak
    """
    log = log or (lambda _m: None)
    ptr = int(dlg_ptr) & 0xFFFFFFFF
    if not ptr:
        return None
    try:
        va = _resolve_va(session, EXPORT_GET_DLG_NAME)
        ret = remote_call_cdecl_x86(
            int(session.pid), va, [ptr], skip_scene_gate=True
        )
        addr = int(ret) & 0xFFFFFFFF
        if not addr:
            return None
        return read_remote_c_string(int(session.pid), addr, max_len=96)
    except Exception as e:
        log(f"GetDlgName(0x{ptr:X}) err: {e}")
        return None


def query_dlg_show(
    session,
    name: str,
    *,
    log: LogFn | None = None,
) -> DlgShowResult:
    """
    Resolve dialog by name and report IsDlgShow.

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        dlg = get_game_ui_dlg(session, name, log=log)
        if not dlg:
            return DlgShowResult(ok=True, name=name, shown=False, dlg_ptr=0)
        shown = is_dlg_show(session, dlg, log=log)
        return DlgShowResult(ok=True, name=name, shown=shown, dlg_ptr=dlg)
    except Exception as e:
        return DlgShowResult(ok=False, name=name, shown=False, error=str(e))


def list_game_ui_dlg_names(
    session,
    *,
    max_n: int = 256,
    log: LogFn | None = None,
) -> DlgListResult:
    """
    Call GetGameUIDlgNum + GetGameUIDlgsName to list dialog ids.

    GetGameUIDlgsName(char const** outNames, int maxCount) fills outNames
    with pointers to internal C strings (caller provides pointer table).
    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(session.pid)
    h = 0
    remote = 0
    try:
        va_num = _resolve_va(session, EXPORT_GET_GAME_UI_DLG_NUM)
        n_live = int(remote_call_cdecl_x86(pid, va_num, []))
        n = max(0, min(int(max_n), max(int(n_live), 32)))
        if n <= 0:
            return DlgListResult(ok=True, names=[], count=0)

        va_names = _resolve_va(session, EXPORT_GET_GAME_UI_DLGS_NAME)
        # remote pointer table: n * 4 bytes, zeroed
        table = b"\x00" * (n * 4)
        h, remote, _sz = remote_alloc_bytes(pid, table)
        filled = int(remote_call_cdecl_x86(pid, va_names, [int(remote), int(n)]))
        # re-read table
        raw = _rpm(h, remote, n * 4)
        names: list[str] = []
        use = max(0, min(n, int(filled) if filled > 0 else n))
        for i in range(use):
            p = struct.unpack_from("<I", raw, i * 4)[0]
            if not p:
                continue
            s = read_remote_c_string(pid, p, max_len=96)
            if s:
                names.append(s)
        # de-dup keep order
        seen: set[str] = set()
        uniq: list[str] = []
        for s in names:
            if s in seen:
                continue
            seen.add(s)
            uniq.append(s)
        return DlgListResult(ok=True, names=uniq, count=int(n_live))
    except Exception as e:
        log(f"list_game_ui_dlg_names err: {e}")
        return DlgListResult(ok=False, names=[], count=0, error=str(e))
    finally:
        if h and remote:
            remote_free(h, remote)
        elif h:
            kernel32.CloseHandle(wintypes.HANDLE(h))


def find_shown_dialog(
    session,
    names: tuple[str, ...] | list[str],
    *,
    log: LogFn | None = None,
) -> DlgShowResult | None:
    """
    Return first name that is currently shown, else None.

    @author by ak
    """
    log = log or (lambda _m: None)
    for name in names:
        r = query_dlg_show(session, name, log=log)
        if r.ok and r.shown:
            return r
    return None


def is_captcha_dialog_open(
    session,
    *,
    names: tuple[str, ...] | list[str] | None = None,
    log: LogFn | None = None,
) -> DlgShowResult:
    """
    True if 活动限时答题 / ActivityQuestion dialog is shown in memory.

    Also falls back to scanning listed dialog names for ActivityQuestion.
    @author by ak
    """
    log = log or (lambda _m: None)
    cands = tuple(names) if names else CAPTCHA_DIALOG_NAME_CANDIDATES
    hit = find_shown_dialog(session, cands, log=log)
    if hit is not None:
        return hit

    # Fallback: enumerate and match ActivityQuestion / Question
    listed = list_game_ui_dlg_names(session, log=log)
    if listed.ok and listed.names:
        keys = ("activityquestion", "question")
        for nm in listed.names:
            low = nm.lower()
            if any(k in low for k in keys):
                r = query_dlg_show(session, nm, log=log)
                if r.ok and r.shown:
                    return r
        # Also report known candidate ptr even if not shown (debug)
    # none shown
    return DlgShowResult(
        ok=True,
        name=cands[0] if cands else "",
        shown=False,
        dlg_ptr=0,
        method="plg_ui",
    )


def wait_dlg_show(
    session,
    names: tuple[str, ...] | list[str],
    *,
    timeout_s: float = 20.0,
    poll_s: float = 0.35,
    stop_event=None,
    log: LogFn | None = None,
    status: LogFn | None = None,
    abort_probe=None,
) -> DlgShowResult:
    """
    Poll until one of names is shown or timeout.

    @author by ak
    """
    import time

    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    deadline = time.time() + max(0.2, float(timeout_s))
    last = DlgShowResult(ok=True, shown=False, error="timeout")
    n = 0
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return DlgShowResult(ok=False, shown=False, error="stopped")
        if abort_probe is not None:
            try:
                abort_reason = abort_probe()
            except Exception as e:
                log(f"dialog abort probe err: {e}")
                abort_reason = None
            if abort_reason:
                text = str(abort_reason)
                status(f"副本开启失败: {text}")
                log(f"dialog wait aborted by entry block: {text}")
                return DlgShowResult(
                    ok=False,
                    shown=False,
                    error=f"entry_block:{text}",
                    method="entry_block_probe",
                )
        n += 1
        status(f"内存检测弹窗… #{n}")
        hit = find_shown_dialog(session, names, log=log)
        if hit is not None and hit.shown:
            status(f"弹窗已打开 {hit.name} ptr=0x{hit.dlg_ptr:X}")
            return hit
        # cheap full scan every few polls
        if n % 4 == 0:
            cap = is_captcha_dialog_open(session, names=names, log=log)
            if cap.shown:
                status(f"弹窗已打开 {cap.name} ptr=0x{cap.dlg_ptr:X}")
                return cap
            last = cap
        time.sleep(max(0.05, float(poll_s)))
    return last


# Live-confirmed AUIDialog layout (x86, 2026-07-16 multi-dialog probe):
#   +0x04C: const char* name ("Win_xxx")
#   +0x09C: x (client)
#   +0x0A0: y
#   +0x0A4: width
#   +0x0A8: height
#   +0x0AC / +0x0B0: related layout (often width/height variants)
AUI_DLG_OFF_NAME = 0x04C
AUI_DLG_OFF_X = 0x09C
AUI_DLG_OFF_Y = 0x0A0
AUI_DLG_OFF_W = 0x0A4
AUI_DLG_OFF_H = 0x0A8


@dataclass
class DlgRect:
    """
    Client-relative AUI dialog rectangle from object memory.

    @author by ak
    """

    ok: bool
    name: str = ""
    dlg_ptr: int = 0
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    shown: bool = False
    error: str | None = None

    @property
    def right(self) -> int:
        return int(self.x) + int(self.w)

    @property
    def bottom(self) -> int:
        return int(self.y) + int(self.h)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["right"] = self.right
        d["bottom"] = self.bottom
        return d


def read_dlg_rect(
    session,
    dlg_ptr: int,
    *,
    name: str = "",
    log: LogFn | None = None,
) -> DlgRect:
    """
    Read AUIDialog client rect from object fields.

    @author by ak
    """
    log = log or (lambda _m: None)
    ptr = int(dlg_ptr) & 0xFFFFFFFF
    if not ptr:
        return DlgRect(ok=False, name=name, error="null dlg")
    pm = getattr(session, "pm", None)
    if pm is None:
        return DlgRect(ok=False, name=name, dlg_ptr=ptr, error="no pymem")
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(pm.process_handle, ptr, 0xC0)
        x = struct.unpack_from("<i", raw, AUI_DLG_OFF_X)[0]
        y = struct.unpack_from("<i", raw, AUI_DLG_OFF_Y)[0]
        w = struct.unpack_from("<i", raw, AUI_DLG_OFF_W)[0]
        h = struct.unpack_from("<i", raw, AUI_DLG_OFF_H)[0]
        nm = name
        if not nm:
            p_name = struct.unpack_from("<I", raw, AUI_DLG_OFF_NAME)[0]
            if p_name:
                nm = read_remote_c_string(int(session.pid), p_name, max_len=64) or ""
        if w <= 8 or h <= 8 or w > 4096 or h > 4096:
            return DlgRect(
                ok=False,
                name=nm or name,
                dlg_ptr=ptr,
                x=x,
                y=y,
                w=w,
                h=h,
                error=f"bad rect {w}x{h}",
            )
        shown = is_dlg_show(session, ptr, log=log)
        return DlgRect(
            ok=True,
            name=nm or name,
            dlg_ptr=ptr,
            x=int(x),
            y=int(y),
            w=int(w),
            h=int(h),
            shown=bool(shown),
        )
    except Exception as e:
        log(f"read_dlg_rect err: {e}")
        return DlgRect(ok=False, name=name, dlg_ptr=ptr, error=str(e))


def get_captcha_dlg_rect(
    session,
    *,
    names: tuple[str, ...] | list[str] | None = None,
    log: LogFn | None = None,
) -> DlgRect:
    """
    Resolve captcha dialog and read its client rect from memory.

    @author by ak
    """
    log = log or (lambda _m: None)
    cands = tuple(names) if names else CAPTCHA_DIALOG_NAME_CANDIDATES
    for name in cands:
        r = query_dlg_show(session, name, log=log)
        if not r.ok or not r.dlg_ptr:
            continue
        if not r.shown:
            continue
        rect = read_dlg_rect(session, r.dlg_ptr, name=name, log=log)
        if rect.ok:
            return rect
    # not shown: still try primary for debug
    r0 = query_dlg_show(session, cands[0], log=log)
    if r0.dlg_ptr:
        rect = read_dlg_rect(session, r0.dlg_ptr, name=cands[0], log=log)
        rect.shown = False
        if rect.ok:
            rect.error = "dialog not shown"
            rect.ok = False
        return rect
    return DlgRect(ok=False, name=cands[0] if cands else "", error="dialog not found")


# Live-confirmed team layout (x86, 2026-07-16):
#   GetHostPlayerTeam -> CECTeam*  (null if not in party)
#   CECTeam +0x10 / +0x14 : leader player id (int64 lo/hi)
#   GetHostPlayer object +0x140 / +0x144 : host player id (int64, same as GetObjectID)
#   side+0x18CC also holds the same CECTeam*
TEAM_OFF_LEADER_ID = 0x10
HOST_OFF_OBJECT_ID = 0x140


def get_host_team_ptr(session, *, log: LogFn | None = None) -> int:
    """
    Call plg::GetHostPlayerTeam() -> CECTeam*.

    Non-zero means currently in a team.
    Gate: session_blocked + ensure_callable(CRT_READ), same as GetHostPlayer.
    Prefer StateKind.PARTY_MEMBERS / IN_TEAM via state_dispatch for business reads.
    @author by ak
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        log(f"GetHostPlayerTeam blocked: {brsn}")
        return 0
    try:
        from app.core.safe_dispatch import OpKind, get_dispatch

        get_dispatch().ensure_callable(int(session.pid), kind=OpKind.CRT_READ)
    except Exception as e:
        log(f"GetHostPlayerTeam gate: {e}")
        _note_remote_hard(session, e)
        return 0
    try:
        va = _resolve_va(session, EXPORT_GET_HOST_PLAYER_TEAM)
        ret = remote_call_cdecl_x86(int(session.pid), va, [], timeout_ms=2500)
        return int(ret) & 0xFFFFFFFF
    except Exception as e:
        log(f"GetHostPlayerTeam err: {e}")
        _note_remote_hard(session, e)
        return 0


def is_host_in_team(session, *, log: LogFn | None = None) -> bool:
    """
    True if host has a CECTeam object (in party).

    @author by ak
    """
    return bool(get_host_team_ptr(session, log=log))


def get_host_player_ptr(session, *, log: LogFn | None = None) -> int:
    """
    Call plg::GetHostPlayer() -> host object*.

    @author by ak
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        log(f"GetHostPlayer blocked: {brsn}")
        return 0
    try:
        from app.core.safe_dispatch import OpKind, get_dispatch

        get_dispatch().ensure_callable(int(session.pid), kind=OpKind.CRT_READ)
    except Exception as e:
        log(f"GetHostPlayer gate: {e}")
        _note_remote_hard(session, e)
        return 0
    try:
        va = _resolve_va(session, EXPORT_GET_HOST_PLAYER)
        ret = remote_call_cdecl_x86(int(session.pid), va, [], timeout_ms=2500)
        return int(ret) & 0xFFFFFFFF
    except Exception as e:
        log(f"GetHostPlayer err: {e}")
        _note_remote_hard(session, e)
        return 0


def _read_u32_pair(session, addr: int) -> tuple[int, int] | None:
    """
    Read int64 lo/hi dwords from remote process via pymem.

    @author by ak
    """
    ptr = int(addr) & 0xFFFFFFFF
    if not ptr:
        return None
    pm = getattr(session, "pm", None)
    if pm is None:
        return None
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(pm.process_handle, ptr, 8)
        lo = struct.unpack_from("<I", raw, 0)[0]
        hi = struct.unpack_from("<I", raw, 4)[0]
        return int(lo) & 0xFFFFFFFF, int(hi) & 0xFFFFFFFF
    except Exception:
        return None


def get_host_player_id(
    session,
    *,
    host_ptr: int | None = None,
    log: LogFn | None = None,
) -> tuple[int, int]:
    """
    Host player id (int64 lo/hi) from GetHostPlayer()+0x140.

    Pass host_ptr to avoid a second GetHostPlayer CRT.
    Returns (0, 0) on failure.
    @author by ak
    """
    log = log or (lambda _m: None)
    host = int(host_ptr or 0) & 0xFFFFFFFF
    if not host:
        host = get_host_player_ptr(session, log=log)
    if not host:
        return 0, 0
    pair = _read_u32_pair(session, host + HOST_OFF_OBJECT_ID)
    if pair is None:
        log("read host id failed")
        return 0, 0
    return pair


def get_host_player_name(
    session,
    *,
    host_ptr: int | None = None,
    log: LogFn | None = None,
    max_chars: int = 24,
) -> str | None:
    """
    Live host role display name via GetHostPlayer + GetObjectName.

    This is the in-game character name (e.g. 十丶一), not server/window title.
    GetObjectName is CRT-sensitive under high remote load; gate + short timeout.

    @author by ak
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        log(f"GetHostPlayer name blocked: {brsn}")
        return None
    host = int(host_ptr or 0) & 0xFFFFFFFF
    if not host:
        host = get_host_player_ptr(session, log=log)
    if not host:
        return None
    try:
        from app.core.plg_exports import EXPORT_GET_OBJECT_NAME
        from app.core.plg_objects import read_wstr
        from app.core.safe_dispatch import OpKind, get_dispatch

        # Known hang hotspot (export GetObjectName); refuse when pid already stressed.
        get_dispatch().ensure_callable(int(session.pid), kind=OpKind.CRT_READ)
        va = _resolve_va(session, EXPORT_GET_OBJECT_NAME)
        name_ptr = int(
            remote_call_cdecl_x86(
                int(session.pid), va, [host], timeout_ms=2000
            )
        ) & 0xFFFFFFFF
        pm = getattr(session, "pm", None)
        if pm is None or not name_ptr:
            return None
        name = (read_wstr(pm, name_ptr, max_chars=int(max_chars)) or "").strip()
        if not name:
            return None
        return name
    except Exception as e:
        log(f"GetHostPlayer name err: {e}")
        _note_remote_hard(session, e)
        return None


def get_team_leader_id(
    session,
    *,
    team_ptr: int | None = None,
    log: LogFn | None = None,
) -> tuple[int, int]:
    """
    Team leader id (int64 lo/hi) from CECTeam+0x10.

    Returns (0, 0) if not in team / read fail.
    @author by ak
    """
    log = log or (lambda _m: None)
    team = int(team_ptr) if team_ptr is not None else get_host_team_ptr(session, log=log)
    team = int(team) & 0xFFFFFFFF
    if not team:
        return 0, 0
    pair = _read_u32_pair(session, team + TEAM_OFF_LEADER_ID)
    if pair is None:
        log("read team leader id failed")
        return 0, 0
    return pair


def is_host_team_leader(session, *, log: LogFn | None = None) -> bool:
    """
    True if host is in a team and host id == team leader id.

    Host id prefers GetHostPlayer()+0x140; falls back to team side-obj +0x240
    (same field invite packets use) when +0x140 is unreadable.
    @author by ak
    """
    log = log or (lambda _m: None)
    team = get_host_team_ptr(session, log=log)
    if not team:
        return False
    host_id = get_host_player_id(session, log=log)
    if host_id == (0, 0):
        # Fallback: net side-host id (invite/leave serializers use this).
        try:
            from app.core.team_ops import (
                get_host_player_for_team,
                read_host_side_id_pair,
            )

            side = get_host_player_for_team(session, log=log)
            if side:
                host_id = read_host_side_id_pair(session, side, log=log)
        except Exception as e:
            log(f"is_host_team_leader side-id fallback err: {e}")
    leader_id = get_team_leader_id(session, team_ptr=team, log=log)
    if host_id == (0, 0) or leader_id == (0, 0):
        # Ambiguous: do NOT claim "not leader" (caller must not auto-leave).
        return False
    return host_id == leader_id


def host_team_role(session, *, log: LogFn | None = None) -> dict:
    """
    Snapshot team role for safe invite decisions.

    role: solo | leader | member | unknown
    unknown = in team but id/leader unreadable (must not auto-leave).
    solo 时也填 host_lo/hi，方便日志对照。
    队长判定：GetHostPlayer+0x140 或 side+0x240 任一等于 leader 即视为队长
    （防止 id 源不一致时误判 member 后 leave 把队长交出去）。
    @author by ak
    """
    log = log or (lambda _m: None)
    out = {
        "in_team": False,
        "is_leader": False,
        "role": "solo",
        "host_lo": 0,
        "host_hi": 0,
        "leader_lo": 0,
        "leader_hi": 0,
    }

    def _side_id() -> tuple[int, int]:
        try:
            from app.core.team_ops import (
                get_host_player_for_team,
                read_host_side_id_pair,
            )

            side = get_host_player_for_team(session, log=log)
            if side:
                return read_host_side_id_pair(session, side, log=log)
        except Exception:
            pass
        return 0, 0

    host_id = get_host_player_id(session, log=log)
    side_id = _side_id()
    # prefer non-zero; prefer +0x140 when both present
    if host_id == (0, 0) and side_id != (0, 0):
        host_id = side_id
    out["host_lo"], out["host_hi"] = int(host_id[0]), int(host_id[1])

    team = get_host_team_ptr(session, log=log)
    if not team:
        return out
    out["in_team"] = True
    leader_id = get_team_leader_id(session, team_ptr=team, log=log)
    out["leader_lo"], out["leader_hi"] = int(leader_id[0]), int(leader_id[1])
    if leader_id == (0, 0):
        out["role"] = "unknown"
        out["is_leader"] = False
        return out
    ids = set()
    if host_id != (0, 0):
        ids.add(host_id)
    if side_id != (0, 0):
        ids.add(side_id)
    if not ids:
        out["role"] = "unknown"
        out["is_leader"] = False
        return out
    if leader_id in ids:
        out["role"] = "leader"
        out["is_leader"] = True
    else:
        out["role"] = "member"
        out["is_leader"] = False
    return out


@dataclass
class InteractGateResult:
    """
    Pre/post interact gate from memory state (team / captcha open).

    Reasons are machine tags, not free text timers.
    @author by ak
    """

    ok: bool
    in_team: bool = False
    is_leader: bool = False
    captcha_open: bool = False
    captcha_name: str = ""
    captcha_ptr: int = 0
    team_ptr: int = 0
    host_id_lo: int = 0
    host_id_hi: int = 0
    leader_id_lo: int = 0
    leader_id_hi: int = 0
    reason: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def check_interact_gate(
    session,
    *,
    require_team_leader: bool = True,
    require_solo: bool | None = None,
    names: tuple[str, ...] | list[str] | None = None,
    log: LogFn | None = None,
) -> InteractGateResult:
    """
    Memory gate before/after entry interact.

    - require_team_leader: must be in party AND host id == CECTeam leader id
      (九层妖楼暗道: need team + be captain)
    - require_solo: legacy inverted flag; if True forces require_team_leader=False
      and rejects when in team (kept for old call sites only)
    - captcha_open: IsDlgShow(Win_ActivityQuestion)
    @author by ak
    """
    log = log or (lambda _m: None)
    # Legacy: old require_solo=True meant "reject if in team".
    if require_solo is True:
        require_team_leader = False
    try:
        team = get_host_team_ptr(session, log=log)
        in_team = bool(team)
        host_lo, host_hi = get_host_player_id(session, log=log) if in_team else (0, 0)
        lead_lo, lead_hi = (
            get_team_leader_id(session, team_ptr=team, log=log) if in_team else (0, 0)
        )
        is_leader = bool(
            in_team
            and (host_lo or host_hi)
            and (host_lo, host_hi) == (lead_lo, lead_hi)
        )
        cap = is_captcha_dialog_open(session, names=names, log=log)
        base_kw = dict(
            in_team=in_team,
            is_leader=is_leader,
            captcha_open=bool(cap.shown),
            captcha_name=cap.name,
            captcha_ptr=cap.dlg_ptr,
            team_ptr=team,
            host_id_lo=host_lo,
            host_id_hi=host_hi,
            leader_id_lo=lead_lo,
            leader_id_hi=lead_hi,
        )
        # Legacy solo path (not used by 妖楼 entry).
        if require_solo is True and in_team:
            return InteractGateResult(ok=False, reason="in_team", **base_kw)
        if require_team_leader:
            if not in_team:
                return InteractGateResult(ok=False, reason="not_in_team", **base_kw)
            if not is_leader:
                return InteractGateResult(ok=False, reason="not_leader", **base_kw)
        return InteractGateResult(
            ok=True,
            reason="ok" if not cap.shown else "captcha_already_open",
            **base_kw,
        )
    except Exception as e:
        return InteractGateResult(ok=False, reason="error", error=str(e))
