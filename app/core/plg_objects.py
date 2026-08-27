# -*- coding: utf-8 -*-
"""
Live object list via xajh.exe plg exports.

  GetObjectCount(OBJECT_CLASSID) -> int
  GetObjects(OBJECT_CLASSID, void** buf, unsigned capacity) -> int
  GetObjectName(void*) -> const wchar_t*
  GetObjectDistToHost(void*) -> float
  GetObjectTemplateID(void*) -> int
  GetObjectID(void*) -> int64 (low 32 via EAX for now)

OBJECT_CLASSID (from GetObjectCount switch):
  0 = players (ElsePlayer / host peers)
  1 = matters / ground items
  2 = NPC + monsters (CECNPC AOI)

Object world pos observed at this+0x158 (float3), matches GetObjectDistToHost.

@author by ak
"""
from __future__ import annotations

import ctypes
import math
import struct
from ctypes import wintypes
from dataclasses import asdict, dataclass
from typing import Callable, Iterable

from app.core.remote_runtime import (
    _open_process,
    _rpm,
    _wpm,
    ensure_pid_remote_callable,
    ensure_pid_scene_stable,
    pid_call_mutex,
    remote_call_cdecl_x86,
    remote_call_float_cdecl as _runtime_call_float_cdecl,
    wait_remote_thread_safely,
)
from app.core.plg_exports import (
    CFG_OBJ_INFO_SIZE,
    CFG_OBJ_POS_X_OFF,
    CFG_OBJ_POS_Y_OFF,
    CFG_OBJ_POS_Z_OFF,
    CFG_OBJ_SCENE_OFF,
    EXPORT_GET_CFG_OBJECT_INFO,
    find_xajh_exe,
    resolve_export_rva,
)

LogFn = Callable[[str], None]

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_EXECUTE_READWRITE = 0x40
WAIT_OBJECT_0 = 0

EXPORT_GET_OBJECT_COUNT = "?GetObjectCount@plg@@YAHW4OBJECT_CLASSID@1@@Z"
EXPORT_GET_OBJECTS = "?GetObjects@plg@@YAHW4OBJECT_CLASSID@1@PAXI@Z"
EXPORT_GET_OBJECT_NAME = "?GetObjectName@plg@@YAPB_WPAX@Z"
EXPORT_GET_OBJECT_DIST = "?GetObjectDistToHost@plg@@YAMPAX@Z"
EXPORT_GET_OBJECT_TID = "?GetObjectTemplateID@plg@@YAHPAX@Z"
EXPORT_GET_OBJECT_ID = "?GetObjectID@plg@@YA_JPAX@Z"
EXPORT_GET_HOST_PLAYER = "?GetHostPlayer@plg@@YAPAXXZ"

# Live-verified class ids
CLASS_PLAYER = 0
CLASS_MATTER = 1
CLASS_NPC = 2  # NPC + monster share CECNPC list

# Live-verified float3 offset on object / host
OBJ_POS_OFF = 0x158


@dataclass
class PlgObject:
    """One live object from GetObjects. @author by ak"""

    class_id: int
    ptr: int
    name: str = ""
    tid: int | None = None
    obj_id: int | None = None
    dist: float | None = None
    x: float | None = None
    y: float | None = None
    z: float | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _u32(v: int) -> int:
    return int(v) & 0xFFFFFFFF


def _resolve_va(session, export_name: str) -> int:
    base = getattr(session, "module_base", None)
    if not base:
        raise RuntimeError("session has no module_base; attach break0 first")
    pe = find_xajh_exe(getattr(session, "exe_path", None))
    if pe is None:
        raise RuntimeError("cannot locate xajh.exe for export parse")
    rva = resolve_export_rva(pe, export_name)
    if rva is None:
        raise RuntimeError(f"export not found: {export_name}")
    return int(base) + int(rva)


# remote_call_float_cdecl routes to remote_runtime (alias below).

remote_call_float_cdecl = _runtime_call_float_cdecl


def get_object_count(session, class_id: int) -> int:
    """Return GetObjectCount(class_id). @author by ak"""
    va = _resolve_va(session, EXPORT_GET_OBJECT_COUNT)
    return int(remote_call_cdecl_x86(int(session.pid), va, [int(class_id)]))


def get_object_ptrs(
    session,
    class_id: int,
    *,
    capacity: int = 256,
) -> list[int]:
    """
    Call GetObjects(class_id, buf, capacity); return pointer list.

    Serialized with pid_call_mutex; never VirtualFreeEx a still-running stub.
    @author by ak
    """
    capacity = max(1, min(int(capacity), 512))
    va = _resolve_va(session, EXPORT_GET_OBJECTS)
    pid = int(session.pid)
    ensure_pid_remote_callable(pid)
    ensure_pid_scene_stable(pid)
    with pid_call_mutex(pid, timeout_ms=10000):
        ensure_pid_remote_callable(pid)
        ensure_pid_scene_stable(pid)
        h = _open_process(pid)
        remote = thr = 0
        completed = False
        try:
            code_off = 4 + capacity * 4
            total = code_off + 96
            remote = int(
                kernel32.VirtualAllocEx(
                    wintypes.HANDLE(h),
                    None,
                    total,
                    MEM_COMMIT | MEM_RESERVE,
                    PAGE_EXECUTE_READWRITE,
                )
                or 0
            )
            if not remote:
                raise OSError(f"VirtualAllocEx failed err={ctypes.get_last_error()}")
            ret_addr = remote
            buf_addr = remote + 4
            code_addr = remote + code_off
            _wpm(h, remote, b"\x00" * (4 + capacity * 4))
            code = bytearray()
            # cdecl: push capacity, push buf, push class_id
            code += b"\x68" + struct.pack("<I", capacity)
            code += b"\x68" + struct.pack("<I", _u32(buf_addr))
            code += b"\x68" + struct.pack("<I", _u32(class_id))
            code += b"\xb8" + struct.pack("<I", _u32(va))
            code += b"\xff\xd0"
            code += b"\xa3" + struct.pack("<I", _u32(ret_addr))
            code += b"\x83\xc4\x0c"
            code += b"\xc3"
            _wpm(h, code_addr, bytes(code))
            tid = wintypes.DWORD(0)
            thr = int(
                kernel32.CreateRemoteThread(
                    wintypes.HANDLE(h),
                    None,
                    0,
                    ctypes.c_void_p(code_addr),
                    None,
                    0,
                    ctypes.byref(tid),
                )
                or 0
            )
            if not thr:
                raise OSError(f"CreateRemoteThread failed err={ctypes.get_last_error()}")
            completed = wait_remote_thread_safely(
                thr, 8000, operation="GetObjects", pid=pid
            )
            if not completed:
                raise TimeoutError("GetObjects hung; remote page intentionally leaked")
            count = int(struct.unpack("<i", _rpm(h, ret_addr, 4))[0])
            n = max(0, min(count, capacity))
            if n <= 0:
                return []
            raw = _rpm(h, buf_addr, n * 4)
            return list(struct.unpack(f"<{n}I", raw))
        finally:
            if thr:
                kernel32.CloseHandle(wintypes.HANDLE(thr))
            if remote and completed:
                kernel32.VirtualFreeEx(
                    wintypes.HANDLE(h), ctypes.c_void_p(remote), 0, MEM_RELEASE
                )
            kernel32.CloseHandle(wintypes.HANDLE(h))


def read_wstr(pm, addr: int, max_chars: int = 48) -> str:
    """Read UTF-16LE C string from process. @author by ak"""
    addr = _u32(addr)
    if not addr or addr < 0x10000:
        return ""
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(pm.process_handle, addr, max_chars * 2)
    except Exception:
        return ""
    out: list[str] = []
    for i in range(0, len(raw) - 1, 2):
        ch = struct.unpack_from("<H", raw, i)[0]
        if ch == 0:
            break
        if ch < 32 and ch not in (9, 10, 13):
            break
        try:
            out.append(chr(ch))
        except Exception:
            break
    return "".join(out)


def read_object_pos(pm, obj_ptr: int) -> tuple[float, float, float] | None:
    """Read float3 at object+0x158. @author by ak"""
    obj_ptr = _u32(obj_ptr)
    if not obj_ptr:
        return None
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(pm.process_handle, obj_ptr + OBJ_POS_OFF, 12)
        x, y, z = struct.unpack("<fff", raw)
    except Exception:
        return None
    if any(v != v or abs(v) == float("inf") for v in (x, y, z)):
        return None
    if abs(x) > 500000 or abs(y) > 500000 or abs(z) > 500000:
        return None
    return float(x), float(y), float(z)


def _dist3(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _is_crt_hard_error(exc: BaseException) -> bool:
    """
    True when remote CRT/alloc is denied — stop hammering the process.

    err=5 Access Denied / VirtualAllocEx fail often follows UI-thread
    interact on dense maps; further CreateRemoteThread makes it worse.
    @author by ak
    """
    msg = str(exc).lower()
    if "err=5" in msg or "access is denied" in msg:
        return True
    if "virtualallocex failed" in msg:
        return True
    if "openprocess failed" in msg:
        return True
    if "createremotethread failed" in msg and "err=5" in msg:
        return True
    if "hard_dead" in msg or "remote hung" in msg:
        return True
    return False


def _note_hard_error(session, exc: BaseException) -> None:
    """Propagate hard CRT failure into SafeDispatch / remote gate. @author by ak"""
    try:
        pid = int(getattr(session, "pid", 0) or 0)
        if pid <= 0:
            return
        from app.core.safe_dispatch import get_dispatch

        get_dispatch().note_exception(pid, exc)
    except Exception:
        try:
            from app.core.remote_runtime import note_remote_os_error

            note_remote_os_error(int(getattr(session, "pid", 0) or 0), exc)
        except Exception:
            pass


def list_class_objects(
    session,
    class_id: int,
    *,
    host_pos: tuple[float, float, float] | None = None,
    radius: float | None = None,
    limit: int = 80,
    log: LogFn | None = None,
    want_tid: int | None = None,
    want_name_keys: Iterable[str] | None = None,
    read_name: bool | None = None,
    read_tid: bool | None = None,
    read_dist_api: bool = False,
    max_inspect: int | None = None,
    max_crt_failures: int = 8,
    exclude_ptrs: Iterable[int] | None = None,
    scan_meta_out: dict | None = None,
) -> list[PlgObject]:
    """
    Enumerate objects of one class_id with name/tid/dist/pos.

    Dense-map safe path (matter thousands / chests hundreds):
      1) RPM pos for all ptrs (no CRT).
      2) Filter by radius + sort nearer first.
      3) CRT only for shortlist: tid / optional name.
      4) Abort early on CRT hard fail (err=5) so loops do not kill attach.

    want_tid: only keep objects with this template id (CRT GetObjectTemplateID).
    want_name_keys: keep if name contains any key (CRT GetObjectName).
    read_name/read_tid: override; default auto from filters.
    read_dist_api: call GetObjectDistToHost (extra CRT); prefer host_pos RPM.
    max_inspect: max objects to CRT after radius prefilter (default scales).
    exclude_ptrs: pointers already inspected by a previous bounded batch.
    scan_meta_out: optional mutable dict populated with batch progress.

    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(session.pid)
    # Gate: do not start CRT storm when pid already hard_dead/hung
    try:
        from app.core.safe_dispatch import OpKind, get_dispatch

        get_dispatch().ensure_callable(pid, kind=OpKind.CRT_READ)
    except Exception as e:
        if _is_crt_hard_error(e):
            _note_hard_error(session, e)
            raise OSError(f"list_class_objects blocked pid={pid}: {e}") from e
    pm = session.pm
    keys = tuple(k for k in (want_name_keys or ()) if k)
    excluded = {_u32(p) for p in (exclude_ptrs or ()) if int(p or 0)}
    do_tid = bool(read_tid) if read_tid is not None else True
    # Skip name CRT when filtering purely by tid (super_loot welfare path).
    if read_name is None:
        do_name = not (want_tid is not None and not keys)
    else:
        do_name = bool(read_name)
    if want_tid is not None:
        do_tid = True

    try:
        count = get_object_count(session, class_id)
    except Exception as e:
        log(f"GetObjectCount class={class_id} err={e}")
        if _is_crt_hard_error(e):
            _note_hard_error(session, e)
            raise OSError(f"GetObjectCount hard-fail class={class_id}: {e}") from e
        return []
    # log(f"plg GetObjectCount class={class_id} -> {count}")  # high-freq
    if count <= 0:
        return []
    cap = min(max(int(count) + 4, 8), 512)
    try:
        ptrs = get_object_ptrs(session, class_id, capacity=cap)
    except Exception as e:
        log(f"GetObjects class={class_id} err={e}")
        if _is_crt_hard_error(e):
            _note_hard_error(session, e)
            raise OSError(f"GetObjects hard-fail class={class_id}: {e}") from e
        return []
    # log(f"plg GetObjects class={class_id} ptrs={len(ptrs)}")  # high-freq

    # Phase 1: cheap RPM positions + host distance (no CRT).
    pre: list[tuple[int, float | None, tuple[float, float, float] | None]] = []
    for p in ptrs:
        if not p:
            continue
        pos = read_object_pos(pm, p)
        dist: float | None = None
        if pos is not None and host_pos is not None:
            dist = _dist3(host_pos, pos)
        if radius is not None:
            if dist is not None and dist > float(radius):
                continue
            if dist is None and pos is None:
                # no pos -> cannot apply radius; still keep for CRT path
                pass
            elif dist is None and pos is not None and host_pos is not None:
                if _dist3(host_pos, pos) > float(radius):
                    continue
        pre.append((_u32(p), dist, pos))

    pre.sort(key=lambda t: t[1] if t[1] is not None else 1e18)
    eligible = [row for row in pre if row[0] not in excluded]
    inspect_cap = max_inspect
    if inspect_cap is None:
        # Enough nearer candidates for density; avoid CRT on all 512.
        base = max(int(limit) * 3, 96)
        inspect_cap = min(len(eligible), max(base, int(limit)))
    inspect_cap = max(1, min(int(inspect_cap), len(eligible))) if eligible else 0
    shortlist = eligible[:inspect_cap]
    inspected_ptrs: list[int] = []
    if scan_meta_out is not None:
        scan_meta_out.clear()
        scan_meta_out.update(
            {
                "candidate_count": len(pre),
                "excluded_count": len(pre) - len(eligible),
                "inspected_ptrs": inspected_ptrs,
                "remaining": len(eligible),
            }
        )
    # plg prefilter success log omitted (high-frequency)

    va_name = _resolve_va(session, EXPORT_GET_OBJECT_NAME) if do_name else 0
    va_dist = _resolve_va(session, EXPORT_GET_OBJECT_DIST) if read_dist_api else 0
    va_tid = _resolve_va(session, EXPORT_GET_OBJECT_TID) if do_tid else 0

    out: list[PlgObject] = []
    crt_fails = 0
    hard_stop = False
    name_err_logged = 0
    name_first = bool(do_name and do_tid and keys and want_tid is None)
    phases = ("name", "tid") if name_first else ("tid", "name")
    for p, dist, pos in shortlist:
        if hard_stop:
            break
        inspected_ptrs.append(int(p))
        name = ""
        tid = None
        x = y = z = None
        if pos:
            x, y, z = pos

        candidate_ok = True
        for phase in phases:
            if phase == "tid" and do_tid:
                if not va_tid:
                    candidate_ok = False
                    break
                try:
                    tid = int(
                        remote_call_cdecl_x86(pid, va_tid, [p], timeout_ms=2500)
                    )
                    crt_fails = 0
                except Exception as e:
                    crt_fails += 1
                    if _is_crt_hard_error(e) or crt_fails >= int(max_crt_failures):
                        log(
                            f"GetObjectTemplateID abort after fails={crt_fails} "
                            f"p=0x{_u32(p):X} err={e}"
                        )
                        if _is_crt_hard_error(e):
                            _note_hard_error(session, e)
                        hard_stop = True
                    candidate_ok = False
                    break
                if want_tid is not None and int(tid) != int(want_tid):
                    candidate_ok = False
                    break

            if phase == "name" and do_name:
                if not va_name:
                    candidate_ok = False
                    break
                try:
                    name_ptr = _u32(
                        remote_call_cdecl_x86(pid, va_name, [p], timeout_ms=2500)
                    )
                    name = read_wstr(pm, name_ptr)
                    crt_fails = 0
                except Exception as e:
                    crt_fails += 1
                    if name_err_logged < 3:
                        log(f"GetObjectName p=0x{_u32(p):X} err={e}")
                        name_err_logged += 1
                    if _is_crt_hard_error(e) or crt_fails >= int(max_crt_failures):
                        log(
                            f"GetObjectName abort after fails={crt_fails} "
                            f"p=0x{_u32(p):X} err={e}"
                        )
                        if _is_crt_hard_error(e):
                            _note_hard_error(session, e)
                        hard_stop = True
                    candidate_ok = False
                    break
                if keys and not any(k in (name or "") for k in keys):
                    # A configured TID already identifies the object; otherwise
                    # avoid spending a second CRT call on a non-matching name.
                    if want_tid is None:
                        candidate_ok = False
                        break

        if not candidate_ok:
            if hard_stop:
                break
            continue

        if read_dist_api and va_dist:
            try:
                dist_api = float(
                    remote_call_float_cdecl(pid, va_dist, [p], timeout_ms=2500)
                )
                if dist_api == dist_api:  # not NaN
                    dist = dist_api
                crt_fails = 0
            except Exception as e:
                crt_fails += 1
                if _is_crt_hard_error(e) or crt_fails >= int(max_crt_failures):
                    log(f"GetObjectDist abort fails={crt_fails} err={e}")
                    hard_stop = True
                    break

        if dist is None and host_pos is not None and pos is not None:
            dist = _dist3(host_pos, pos)
        if radius is not None and dist is not None and dist > float(radius):
            continue

        note = f"plg class={class_id}"
        if hard_stop:
            note += " crt_abort"
        out.append(
            PlgObject(
                class_id=int(class_id),
                ptr=_u32(p),
                name=name or (f"tid:{tid}" if tid is not None else ""),
                tid=tid,
                dist=dist,
                x=x,
                y=y,
                z=z,
                note=note,
            )
        )
        if len(out) >= int(limit):
            break

    if scan_meta_out is not None:
        scan_meta_out["remaining"] = max(0, len(eligible) - len(inspected_ptrs))
    if hard_stop and not out:
        log(f"plg list_class_objects hard-stop empty class={class_id}")
        raise OSError(
            f"list_class_objects hard-stop class={class_id} "
            f"(CreateRemoteThread/VirtualAlloc denied)"
        )
    out.sort(key=lambda o: (o.dist if o.dist is not None else 1e9, o.name))
    return out


def get_cfg_object_info(
    session,
    tid: int,
    *,
    log: LogFn | None = None,
) -> dict | None:
    """
    Resolve static NPC/template config by tid via plg::GetCfgObjectInfo.

    Live layout (CfgObjInfo out, 2026-07-16):
      +0x00 id, +0x30 scene_id, +0x50/+0x54/+0x58 float x/y/z

    Returns {tid, scene_id, x, y, z, ok} or None on hard failure.

    @author by ak
    """
    log = log or (lambda _m: None)
    tid = int(tid) & 0xFFFFFFFF
    if not tid:
        return None
    try:
        va = _resolve_va(session, EXPORT_GET_CFG_OBJECT_INFO)
    except Exception as e:
        log(f"GetCfgObjectInfo resolve: {e}")
        return None
    pid = int(session.pid)
    ensure_pid_remote_callable(pid)
    ensure_pid_scene_stable(pid)
    with pid_call_mutex(pid, timeout_ms=7000):
        ensure_pid_remote_callable(pid)
        ensure_pid_scene_stable(pid)
        h = _open_process(pid)
        remote = thr = 0
        completed = False
        try:
            total = 4 + CFG_OBJ_INFO_SIZE + 96
            remote = int(
                kernel32.VirtualAllocEx(
                    wintypes.HANDLE(h),
                    None,
                    total,
                    MEM_COMMIT | MEM_RESERVE,
                    PAGE_EXECUTE_READWRITE,
                )
                or 0
            )
            if not remote:
                raise OSError(f"VirtualAllocEx err={ctypes.get_last_error()}")
            ret_addr = remote
            cfg_addr = remote + 4
            code_addr = remote + 4 + CFG_OBJ_INFO_SIZE
            _wpm(h, remote, b"\x00" * (4 + CFG_OBJ_INFO_SIZE))
            code = bytearray()
            code += b"\x68" + struct.pack("<I", _u32(cfg_addr))
            code += b"\x68" + struct.pack("<I", _u32(tid))
            code += b"\xb8" + struct.pack("<I", _u32(va))
            code += b"\xff\xd0"
            code += b"\xa3" + struct.pack("<I", _u32(ret_addr))
            code += b"\x83\xc4\x08"
            code += b"\xc3"
            _wpm(h, code_addr, bytes(code))
            thr_id = wintypes.DWORD(0)
            thr = int(
                kernel32.CreateRemoteThread(
                    wintypes.HANDLE(h),
                    None,
                    0,
                    ctypes.c_void_p(code_addr),
                    None,
                    0,
                    ctypes.byref(thr_id),
                )
                or 0
            )
            if not thr:
                raise OSError(f"CreateRemoteThread err={ctypes.get_last_error()}")
            completed = wait_remote_thread_safely(
                thr, 5000, operation="GetCfgObjectInfo", pid=pid
            )
            if not completed:
                raise TimeoutError(
                    "GetCfgObjectInfo hung; remote page intentionally leaked"
                )
            ret = int(struct.unpack("<i", _rpm(h, ret_addr, 4))[0])
            raw = _rpm(h, cfg_addr, CFG_OBJ_INFO_SIZE)
            ok = bool(ret & 0xFF)
            scene = int(struct.unpack_from("<I", raw, CFG_OBJ_SCENE_OFF)[0])
            x, y, z = struct.unpack_from("<fff", raw, CFG_OBJ_POS_X_OFF)
            out = {
                "tid": tid,
                "ok": ok,
                "scene_id": scene if ok else 0,
                "x": float(x) if ok else None,
                "y": float(y) if ok else None,
                "z": float(z) if ok else None,
                "ret": ret,
            }
            log(
                f"GetCfgObjectInfo tid={tid} ok={ok} scene={out['scene_id']} "
                f"xyz=({out['x']},{out['y']},{out['z']})"
            )
            return out
        except Exception as e:
            log(f"GetCfgObjectInfo tid={tid} fail: {e}")
            return None
        finally:
            if thr:
                kernel32.CloseHandle(wintypes.HANDLE(thr))
            if remote and completed:
                kernel32.VirtualFreeEx(
                    wintypes.HANDLE(h), ctypes.c_void_p(remote), 0, MEM_RELEASE
                )
            kernel32.CloseHandle(wintypes.HANDLE(h))
