# -*- coding: utf-8 -*-
"""
Read-side bridge to the in-game hang attack target.

Reversed 2026-08-08 (xajh.exe build 2026-07-21, base 0x400000):

  CECAutoPlayTargetIgnorePolicy lives on the attack sub-system object that is
  created once the in-game hang enters the attack state.  Field layout (this):
    +0x00  vtable (0x12BEC9C)
    +0x04  log sink (object with +4 virtual)
    +0x14  ignore list  (std::list head; add 0x8BE300 / clear 0x8BEBB0)
    +0x38  current target id64 lo
    +0x3C  current target id64 hi
    +0x40  current target start tick

  The object is located at runtime by scanning autoplay's pointer fields for one
  whose vtable is IGNORE_POLICY_VTABLE (0x12BEC9C).  It only exists while the
  hang is actually attacking, so callers must tolerate "not in attack state".

  The normal path is read-only; the dungeon guard may additionally seed the
  client's own ignore list for a rejected live target.

@author by ak
"""
from __future__ import annotations

import struct
from typing import Callable

from app.core.remote_runtime import (
    remote_call_cdecl_x86,
    remote_call_cdecl_x86_ret64,
    remote_call_float_cdecl,
)

LogFn = Callable[[str], None]

# id64 -> object pointer (cdecl lo, hi). Live 0x4AE4A0 calls 0x4E0920(id64).
OBJECT_BY_ID_VA = 0x004AE4A0
OBJECT_BY_ID_PREFERRED_BASE = 0x00400000
ADD_TO_IGNORE_LIST_VA = 0x00C5E2E0
CLEAR_IGNORE_VA = 0x00C5E380

GET_CURRENT_SELECT_TARGET_VA = 0x00C65F20
IGNORE_POLICY_VTABLE = 0x12BEC9C
TARGET_ID_LO_OFF = 0x38
TARGET_ID_HI_OFF = 0x3C
# Player-selected target id64 on the host-side object (RE 2026-08-09):
#   host_side = [[0x15282D8]+0x24]+0x8C ; selected = u64[host_side+0x19E8]
# Pure RPM, no CRT — GetCurrentSelectTarget (vtable[0x80] @ 0x72F5E0) just
# reads these two dwords.
HOST_SIDE_GLOBAL_VA = 0x15282D8
HOST_SIDE_MID_OFF = 0x24
HOST_SIDE_LEAF_OFF = 0x8C
HOST_SELECT_TARGET_LO_OFF = 0x19E8
HOST_SELECT_TARGET_HI_OFF = 0x19EC
TARGET_IGNORE_LIST_TARGET_LO_OFF = TARGET_ID_LO_OFF
TARGET_IGNORE_LIST_TARGET_HI_OFF = TARGET_ID_HI_OFF


def _read_u32(pm, addr: int) -> int:
    try:
        return struct.unpack("<I", pm.read_bytes(int(addr), 4))[0]
    except Exception:
        return 0


def _read_bytes(pm, addr: int, n: int) -> bytes:
    try:
        return pm.read_bytes(int(addr), int(n))
    except Exception:
        return b""


def _write_u32(pm, addr: int, value: int) -> bool:
    try:
        pm.write_bytes(int(addr), struct.pack("<I", int(value) & 0xFFFFFFFF), 4)
        return True
    except Exception:
        return False


def _valid_ptr(value: int) -> bool:
    return 0x10000 <= (int(value) & 0xFFFFFFFF) < 0xFFFF0000


def resolve_cec_autoplay(session) -> tuple[int, int]:
    """Return (pid, autoplay_va) or (0, 0). @author by ak"""
    pid = int(getattr(session, "pid", 0) or 0)
    base = int(getattr(session, "module_base", 0) or 0)
    if not pid or not base:
        return 0, 0
    try:
        pm = session.pm
    except Exception:
        return 0, 0
    g_va = base + (0x15282D8 - 0x400000)
    root = _read_u32(pm, g_va)
    if not _valid_ptr(root):
        return 0, 0
    mid = _read_u32(pm, root + 0x24)
    if not _valid_ptr(mid):
        return 0, 0
    ap = _read_u32(pm, mid + 0x220)
    if not _valid_ptr(ap):
        return 0, 0
    return pid, ap


def resolve_ignore_policy(session) -> dict:
    """Locate the attack sub-system (ignore policy) object at runtime.

    Scans autoplay pointer fields for an object whose vtable is
    ``IGNORE_POLICY_VTABLE`` (0x12BEC9C).  Returns a dict with 'found' bool and
    reason when unavailable.

    @author by ak
    """
    pid, ap = resolve_cec_autoplay(session)
    if not ap:
        return {"found": False, "pid": pid, "autoplay": 0, "reason": "autoplay unresolved"}
    try:
        pm = session.pm
    except Exception as e:
        return {"found": False, "pid": pid, "autoplay": ap, "reason": f"pm:{e}"}
    for off in range(0x4, 0x800, 4):
        cand = _read_u32(pm, ap + off)
        if not _valid_ptr(cand):
            continue
        vt = _read_u32(pm, cand)
        if vt != IGNORE_POLICY_VTABLE:
            continue
        lo = _read_u32(pm, cand + TARGET_ID_LO_OFF)
        hi = _read_u32(pm, cand + TARGET_ID_HI_OFF)
        return {
            "found": True, "pid": pid, "autoplay": ap,
            "policy": cand, "field_off": off,
            "target_id64": (int(hi) << 32) | int(lo),
            "reason": "ok" if (lo or hi) else "attack system idle (no current target)",
        }
    return {
        "found": False, "pid": pid, "autoplay": ap,
        "reason": "attack sub-system not instantiated (need in-hang combat)",
    }


def read_selected_target(session) -> dict:
    """Read the player's currently selected target id64 via pure memory.

    Reads host_side+0x19E8/0x19EC (same fields GetCurrentSelectTarget returns)
    — no CRT, safe on hot loops / while the game UI is open.  Independent of
    the hang attack subsystem (works for manual selection and dungeon mode).

    @author by ak
    """
    pid = int(getattr(session, "pid", 0) or 0)
    base = int(getattr(session, "module_base", 0) or 0)
    if not pid or not base:
        return {"ok": False, "target_id64": 0, "reason": "no session"}
    try:
        pm = session.pm
    except Exception as e:
        return {"ok": False, "target_id64": 0, "reason": f"pm:{e}"}
    g_va = base + (HOST_SIDE_GLOBAL_VA - 0x400000)
    root = _read_u32(pm, g_va)
    if not _valid_ptr(root):
        return {"ok": False, "target_id64": 0, "reason": "no root"}
    mid = _read_u32(pm, root + HOST_SIDE_MID_OFF)
    if not _valid_ptr(mid):
        return {"ok": False, "target_id64": 0, "reason": "no mid"}
    host = _read_u32(pm, mid + HOST_SIDE_LEAF_OFF)
    if not _valid_ptr(host):
        return {"ok": False, "target_id64": 0, "reason": "no host_side"}
    lo = _read_u32(pm, host + HOST_SELECT_TARGET_LO_OFF)
    hi = _read_u32(pm, host + HOST_SELECT_TARGET_HI_OFF)
    oid = (int(hi) << 32) | int(lo)
    return {
        "ok": True,
        "target_id64": oid,
        "host_side": host,
        "reason": "ok",
    }


def read_current_target(session, *, crt_fallback: bool = True) -> dict:
    """Return the current hang attack target id64 (0 when idle).

    ``crt_fallback=False`` skips the remote GetCurrentSelectTarget fallback so
    callers on hot loops never issue CRT against the game while its UI is open.

    @author by ak
    """
    r = resolve_ignore_policy(session)
    if not r.get("found"):
        if not crt_fallback:
            return r
        # IgnorePolicy only exists during attack. Fall back to player-selected target.
        sel = _read_game_select_target(session)
        if sel:
            r["target_id64"] = sel
            r["target_lo"] = int(sel) & 0xFFFFFFFF
            r["target_hi"] = (int(sel) >> 32) & 0xFFFFFFFF
            r["reason"] = "selected (player target, no attack subsystem)"
            r["found"] = True
        return r
    try:
        pm = session.pm
    except Exception as e:
        r["error"] = str(e)
        return r
    lo = _read_u32(pm, int(r["policy"]) + TARGET_ID_LO_OFF)
    hi = _read_u32(pm, int(r["policy"]) + TARGET_ID_HI_OFF)
    oid = (int(hi) << 32) | int(lo)
    r["target_id64"] = oid
    r["target_lo"] = int(lo)
    r["target_hi"] = int(hi)
    r["reason"] = "ok"
    if not oid and crt_fallback:
        sel = _read_game_select_target(session)
        if sel:
            r["target_id64"] = sel
            r["target_lo"] = int(sel) & 0xFFFFFFFF
            r["target_hi"] = (int(sel) >> 32) & 0xFFFFFFFF
            r["reason"] = "selected (player target, not attack)"
    return r


def add_to_ignore(session, obj_id64: int, *, log: LogFn | None = None) -> dict:
    """Insert an instance id into the client's native attack ignore list.

    The client owns list allocation and expiry semantics; we only seed the
    policy's current id fields and invoke its verified thiscall insertion
    method. Failure is non-fatal because the Python quarantine remains active.
    """
    log = log or (lambda _m: None)
    oid = int(obj_id64) & 0xFFFFFFFFFFFFFFFF
    if not oid:
        return {"ok": False, "error": "obj_id64 == 0"}
    ctx = resolve_ignore_policy(session)
    if not ctx.get("found"):
        return {"ok": False, **ctx}
    policy = int(ctx.get("policy") or 0)
    pm = getattr(session, "pm", None)
    pid = int(getattr(session, "pid", 0) or 0)
    if not policy or not pm or not pid:
        return {"ok": False, "error": "policy/session unavailable", **ctx}
    lo = oid & 0xFFFFFFFF
    hi = (oid >> 32) & 0xFFFFFFFF
    if not (_write_u32(pm, policy + TARGET_ID_LO_OFF, lo) and
            _write_u32(pm, policy + TARGET_ID_HI_OFF, hi)):
        return {"ok": False, "error": "write target id failed", **ctx}
    old_oid = int(ctx.get("target_id64") or 0) & 0xFFFFFFFFFFFFFFFF
    try:
        from app.core.activity_auto import remote_call_thiscall_x86

        ret = remote_call_thiscall_x86(
            pid,
            ADD_TO_IGNORE_LIST_VA,
            policy,
            [],
            caller_cleanup=False,
            timeout_ms=3000,
        )
        log(f"target guard: native ignore add id64=0x{oid:X} ret={ret}")
        return {"ok": True, **ctx, "added_id64": oid, "ret": int(ret)}
    except Exception as e:
        return {"ok": False, **ctx, "error": str(e)}
    finally:
        # AddToIgnoreList consumes the +0x38/+0x3C staging fields. Restore the
        # previous attack identity so seeding cannot retarget the live attack.
        _write_u32(pm, policy + TARGET_ID_LO_OFF, old_oid & 0xFFFFFFFF)
        _write_u32(pm, policy + TARGET_ID_HI_OFF, (old_oid >> 32) & 0xFFFFFFFF)


def _read_game_select_target(session) -> int:
    """Call 0xC65F20 (GetCurrentSelectTarget) to read the player-selected target id64.

    The IgnorePolicy only carries the attack target (set when the hang actively
    attacks).  This function provides the player's selection target — the one
    the user clicked/Tab'd — which is useful before the hang engages.

    Returns 0 on failure or when no target is selected.
    @author by ak
    """
    pid, ap = resolve_cec_autoplay(session)
    if not ap or not pid:
        return 0
    try:
        from app.core.remote_runtime import (
            CallConvention,
            ReturnKind,
            remote_call_x86,
        )

        oid = remote_call_x86(
            pid,
            GET_CURRENT_SELECT_TARGET_VA,
            [],
            convention=CallConvention.THISCALL,
            this_ptr=ap,
            return_kind=ReturnKind.U64,
            timeout_ms=2500,
        )
        if oid:
            return int(oid) & 0xFFFFFFFFFFFFFFFF
    except Exception:
        pass
    # Fallback: cdecl (reads from global / no this)
    try:
        from app.core.remote_runtime import remote_call_cdecl_x86_ret64

        oid = remote_call_cdecl_x86_ret64(
            pid, GET_CURRENT_SELECT_TARGET_VA, [], timeout_ms=2500
        )
        if oid:
            return int(oid) & 0xFFFFFFFFFFFFFFFF
    except Exception:
        pass
    return 0


def resolve_object_by_id64(session, obj_id64: int) -> int:
    """Map an instance id64 to a live CECNPC object pointer.

    The native lookup is preferred. Some dungeon builds return null for a live
    AOI object, so fall back to a read-only class-2 enumeration and compare the
    exported object id64.
    """
    oid = int(obj_id64) & 0xFFFFFFFFFFFFFFFF
    if not oid:
        return 0
    pid = int(getattr(session, "pid", 0) or 0)
    base = int(getattr(session, "module_base", 0) or 0)
    if not pid or not base:
        return 0
    va = base + (OBJECT_BY_ID_VA - OBJECT_BY_ID_PREFERRED_BASE)
    lo = oid & 0xFFFFFFFF
    hi = (oid >> 32) & 0xFFFFFFFF
    try:
        obj = int(remote_call_cdecl_x86(pid, va, [lo, hi], timeout_ms=2500)) & 0xFFFFFFFF
        if obj:
            return obj
    except Exception:
        pass
    try:
        from app.core.plg_exports import EXPORT_GET_OBJECT_ID, find_xajh_exe, resolve_export_rva
        from app.core.plg_objects import get_object_ptrs

        pe = find_xajh_exe(getattr(session, "exe_path", None))
        if pe is None:
            return 0
        id_va = int(getattr(session, "module_base", 0) or 0x400000) + resolve_export_rva(
            pe, EXPORT_GET_OBJECT_ID
        )
        for ptr in get_object_ptrs(session, 2, capacity=256):
            try:
                current = int(
                    remote_call_cdecl_x86_ret64(pid, id_va, [int(ptr)], timeout_ms=2500)
                ) & 0xFFFFFFFFFFFFFFFF
            except Exception:
                continue
            if current == oid:
                return int(ptr) & 0xFFFFFFFF
    except Exception:
        pass
    return 0


def object_template_id(session, obj_ptr: int) -> int:
    """Return the template id (tid) of an object pointer. @author by ak"""
    ptr = int(obj_ptr) & 0xFFFFFFFF
    if not ptr:
        return 0
    try:
        from app.core.plg_objects import read_object_template_id

        return int(read_object_template_id(session, ptr) or 0) & 0xFFFFFFFF
    except Exception:
        return 0


def object_id64(session, obj_ptr: int) -> int:
    """Read an object's live instance id64 through the plg export."""
    ptr = int(obj_ptr) & 0xFFFFFFFF
    if not ptr:
        return 0
    try:
        from app.core.plg_objects import _resolve_va, EXPORT_GET_OBJECT_ID

        pid = int(getattr(session, "pid", 0) or 0)
        return int(
            remote_call_cdecl_x86_ret64(
                pid, _resolve_va(session, EXPORT_GET_OBJECT_ID), [ptr], timeout_ms=2500
            )
        ) & 0xFFFFFFFFFFFFFFFF
    except Exception:
        return 0


def object_distance_to_host(session, obj_ptr: int) -> float | None:
    """Read the exported distance from an object to the host."""
    ptr = int(obj_ptr) & 0xFFFFFFFF
    if not ptr:
        return None
    try:
        from app.core.plg_objects import _resolve_va
        from app.core.plg_exports import EXPORT_GET_OBJECT_DIST

        pid = int(getattr(session, "pid", 0) or 0)
        value = float(
            remote_call_float_cdecl(
                pid,
                _resolve_va(session, EXPORT_GET_OBJECT_DIST),
                [ptr],
                timeout_ms=2500,
            )
        )
        return value if value == value else None
    except Exception:
        return None


def object_name_of(session, obj_ptr: int) -> str:
    """Return the display name of an object pointer. @author by ak"""
    ptr = int(obj_ptr) & 0xFFFFFFFF
    if not ptr:
        return ""
    try:
        from app.core.plg_objects import _resolve_va, read_wstr
        from app.core.plg_exports import EXPORT_GET_OBJECT_NAME

        pm = getattr(session, "pm", None)
        pid = int(getattr(session, "pid", 0) or 0)
        va = _resolve_va(session, EXPORT_GET_OBJECT_NAME)
        name_ptr = int(remote_call_cdecl_x86(pid, va, [ptr], timeout_ms=2500)) & 0xFFFFFFFF
        if pm is not None and name_ptr:
            return read_wstr(pm, name_ptr)
    except Exception:
        pass
    return ""


__all__ = [
    "resolve_cec_autoplay",
    "resolve_ignore_policy",
    "read_selected_target",
    "read_current_target",
    "add_to_ignore",
    "resolve_object_by_id64",
    "object_template_id",
    "object_id64",
    "object_distance_to_host",
    "object_name_of",
    "OBJECT_BY_ID_VA",
]
