# -*- coding: utf-8 -*-
"""
Background interact via plg exports.

Workflow:
  scan matter AOI -> user selects one target -> classify kind -> execute:
    pickup  = ground loot (PickItem)
    gather  = production / gather names (PickItem smoke)
    mark    = flags / scenery (skip by default)
    other   = unknown matter (PickItem smoke)

No special-case mapping of dungeon names to "mine".
User picks the target; kind only chooses the execution path.

@author by ak
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Callable

from app.core.client_build import session_supports
from app.core.remote_runtime import remote_call_cdecl_x86
from app.core.plg_exports import (
    EXPORT_GET_OBJECT_ID,
    EXPORT_PICK_ITEM,
    EXPORT_SET_TARGET,
    OBJ_ID_OFF,
    RVA_AUTO_CLICK_DYN_MATTER,
    RVA_AUTO_CLICK_MATTER,
    RVA_CHOICE_OBJECT,
    RVA_PICKUP_MATTER,
    find_xajh_exe,
    resolve_export_rva,
)
from app.core.plg_objects import (
    CLASS_MATTER,
    list_class_objects,
)

LogFn = Callable[[str], None]


def _note_remote_hard(session, exc: BaseException | str) -> None:
    """Push CRT/attach collapse into SafeDispatch gate. @author by ak"""
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


_PICKUP_HINT = re.compile(r"(箱|袋|包|匣|掉落|战利|残骸|尸体|包裹|宝匣|战利品)")
_GATHER_HINT = re.compile(
    r"(矿|铁矿|铜矿|银矿|金矿|煤矿|玉|晶石|矿脉|矿石|药草|灵芝|人参|"
    r"采集|草药|棉花|泉水|水井|草丛|花丛|石堆|木料|原木|矿点)"
)
_MARK_HINT = re.compile(r"(标志|旗帜|旗|碑|告示|路牌|光柱)")

KIND_PICKUP = "pickup"
KIND_GATHER = "gather"
KIND_MARK = "mark"
KIND_OTHER = "other"

KIND_LABEL = {
    KIND_PICKUP: "拾取",
    KIND_GATHER: "采集",
    KIND_MARK: "标志",
    KIND_OTHER: "其它",
}

# Prefer useful interact targets when ranking scan list.
_KIND_RANK = {
    KIND_GATHER: 0,
    KIND_PICKUP: 1,
    KIND_OTHER: 2,
    KIND_MARK: 3,
}


def classify_matter_name(name: str) -> str:
    """
    Classify matter display name into execution kind.
    Heuristic only; user still selects the concrete target.
    @author by ak
    """
    n = (name or "").strip()
    if not n:
        return KIND_OTHER
    if _MARK_HINT.search(n):
        return KIND_MARK
    if _GATHER_HINT.search(n):
        return KIND_GATHER
    if _PICKUP_HINT.search(n):
        return KIND_PICKUP
    return KIND_OTHER


def kind_label(kind: str) -> str:
    """Chinese label for kind. @author by ak"""
    return KIND_LABEL.get(kind or KIND_OTHER, kind or KIND_OTHER)


def suggest_action(kind: str) -> str:
    """
    Execution action id for a classified kind.
    @author by ak
    """
    k = kind or KIND_OTHER
    if k == KIND_MARK:
        return "skip"
    if k == KIND_PICKUP:
        return "pickup_try"
    if k == KIND_GATHER:
        return "gather_try"
    return "other_try"


@dataclass
class InteractTarget:
    """One matter candidate for user-selected interact. @author by ak"""

    name: str
    ptr: int
    obj_id: int | None = None
    tid: int | None = None
    dist: float | None = None
    x: float | None = None
    y: float | None = None
    z: float | None = None
    kind: str = KIND_OTHER
    note: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind_label"] = kind_label(self.kind)
        d["action"] = suggest_action(self.kind)
        return d


@dataclass
class PickItemResult:
    """Outcome of remote PickItem call. @author by ak"""

    ok: bool
    method: str = "PickItem"
    obj_id: int | None = None
    tid: int | None = None
    name: str = ""
    dist: float | None = None
    kind: str = KIND_OTHER
    func_va: int = 0
    ret: int | None = None
    error: str | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InteractActionResult:
    """User-selected execute outcome. @author by ak"""

    ok: bool
    action: str
    kind: str
    target: dict | None = None
    pick: dict | None = None
    error: str | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _resolve_va(session, export_name: str) -> int:
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


def get_object_id64(session, obj_ptr: int) -> int | None:
    """
    Prefer reading u64 at object+0x140 (community notes / live struct).

    High dword from struct is often a type tag (0x01xxxxxx / 0x02xxxxxx).
    Only fall back to plg GetObjectID low32 — never invent high from EDX.
    @author by ak
    """
    ptr = int(obj_ptr) & 0xFFFFFFFF
    if not ptr:
        return None
    pm = getattr(session, "pm", None)
    if pm is not None:
        try:
            import pymem.memory
            import struct

            raw = pymem.memory.read_bytes(pm.process_handle, ptr + OBJ_ID_OFF, 8)
            low, high = struct.unpack("<II", raw)
            if low == 0:
                return None
            # Keep full u64 when high is zero or a small type tag.
            if high == 0 or 0x01000000 <= high <= 0x03FFFFFF:
                return int(low) | (int(high) << 32)
            # High looks like pointer/garbage — low only
            return int(low)
        except Exception:
            pass
    try:
        va = _resolve_va(session, EXPORT_GET_OBJECT_ID)
        low = remote_call_cdecl_x86(int(session.pid), va, [ptr]) & 0xFFFFFFFF
        return int(low)
    except Exception:
        return None


def choice_object(
    session,
    obj_id: int,
    *,
    log: LogFn | None = None,
) -> PickItemResult:
    """
    Remote select via notes ChoiceObject (live: module_base + 0x48F2A0 = 0x88F2A0).

    Preferred path is bridge CMD_CHOICE_OBJECT (thiscall + 0x4AE470 this).
    This CRT remote path is experimental and may SEH without proper this.
    One attempt only if high known; otherwise try tag 0x01000000 then 0.
    Stops on hang/access error (do not cascade).
    @author by ak
    """
    log = log or (lambda _m: None)
    if not session_supports(session, "target.set"):
        return PickItemResult(
            ok=False,
            method="ChoiceObject",
            error="unknown client build; target.set capability required",
        )
    oid = int(obj_id)
    low = oid & 0xFFFFFFFF
    high = (oid >> 32) & 0xFFFFFFFF
    if high and 0x01000000 <= high <= 0x03FFFFFF:
        candidates = [(high, low, "struct_hi_lo")]
    else:
        candidates = [
            (0x01000000, low, "tag01_lo"),
            (0, low, "0_lo"),
        ]

    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        return PickItemResult(ok=False, method="ChoiceObject", error="no module_base")
    # Preferred abs VA 0x88F2A0 -> RVA 0x48F2A0 (was wrongly 0x88F2A0).
    va = base + int(RVA_CHOICE_OBJECT)
    if va > 0x10000000:
        return PickItemResult(
            ok=False,
            method="ChoiceObject",
            error=f"implausible va=0x{va:X} (check RVA)",
        )
    pid = int(session.pid)
    last = None
    for hi, lo, label in candidates:
        log(f"ChoiceObject try {label} va=0x{va:X} hi=0x{hi:X} lo=0x{lo:X}")
        try:
            ret = remote_call_cdecl_x86(pid, va, [lo, hi], timeout_ms=2500)
            log(f"ChoiceObject {label} ret={ret}")
            last = PickItemResult(
                ok=True,
                method=f"ChoiceObject:{label}",
                obj_id=oid,
                func_va=va,
                ret=int(ret),
                note="select via ChoiceObject",
            )
            return last
        except Exception as e:
            log(f"ChoiceObject {label} err={e}")
            last = PickItemResult(
                ok=False,
                method=f"ChoiceObject:{label}",
                obj_id=oid,
                func_va=va,
                error=str(e),
            )
            # timeout / access / denied: stop further ChoiceObject tries
            err_s = str(e)
            if any(
                k in err_s
                for k in (
                    "timeout",
                    "CreateRemoteThread",
                    "VirtualAllocEx",
                    "ReadProcessMemory",
                    "WriteProcessMemory",
                )
            ):
                _note_remote_hard(session, e)
                return last
    return last or PickItemResult(ok=False, method="ChoiceObject", error="no try")


def pickup_matter_call(
    session,
    obj_id: int,
    *,
    name: str = "",
    dist: float | None = None,
    kind: str = KIND_OTHER,
    log: LogFn | None = None,
) -> PickItemResult:
    """
    Experimental remote call to notes pickup helper (live base+0x3267F0).
    Single layout attempt; stop on remote error.
    @author by ak
    """
    log = log or (lambda _m: None)
    if not session_supports(session, "matter.pickup.live"):
        return PickItemResult(
            ok=False,
            method="Pickup7267F0",
            error="matter.pickup.live capability unavailable",
        )
    oid = int(obj_id)
    low = oid & 0xFFFFFFFF
    high = (oid >> 32) & 0xFFFFFFFF
    if not (0x01000000 <= high <= 0x03FFFFFF):
        high = 0x01000000
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        return PickItemResult(ok=False, method="Pickup7267F0", error="no module_base")
    va = base + int(RVA_PICKUP_MATTER)
    if va > 0x10000000:
        return PickItemResult(
            ok=False, method="Pickup7267F0", error=f"implausible va=0x{va:X}"
        )
    pid = int(session.pid)
    log(f"Pickup7267F0 try lo_hi va=0x{va:X} lo=0x{low:X} hi=0x{high:X} name={name!r}")
    try:
        ret = remote_call_cdecl_x86(pid, va, [low, high], timeout_ms=2500)
        log(f"Pickup7267F0 ret={ret}")
        return PickItemResult(
            ok=True,
            method="Pickup7267F0:lo_hi",
            obj_id=oid,
            name=name,
            dist=dist,
            kind=kind,
            func_va=va,
            ret=int(ret),
            note="experimental pickup",
        )
    except Exception as e:
        log(f"Pickup7267F0 err={e}")
        return PickItemResult(
            ok=False,
            method="Pickup7267F0:lo_hi",
            obj_id=oid,
            name=name,
            dist=dist,
            kind=kind,
            func_va=va,
            error=str(e),
        )


def scan_nearby_matters(
    session,
    host_pos: tuple[float, float, float] | None = None,
    *,
    radius: float = 40.0,
    limit: int = 40,
    prefer_actionable: bool = True,
    log: LogFn | None = None,
) -> list[InteractTarget]:
    """
    List nearby matters with kind for user selection.
    prefer_actionable ranks gather/pickup before mark.
    @author by ak
    """
    log = log or (lambda _m: None)
    objs = list_class_objects(
        session,
        CLASS_MATTER,
        host_pos=host_pos,
        radius=float(radius) if radius else None,
        limit=max(int(limit), 8),
        log=log,
    )
    out: list[InteractTarget] = []
    for o in objs:
        oid = get_object_id64(session, o.ptr)
        kind = classify_matter_name(o.name)
        out.append(
            InteractTarget(
                name=o.name or "",
                ptr=int(o.ptr),
                obj_id=oid,
                tid=o.tid,
                dist=o.dist,
                x=o.x,
                y=o.y,
                z=o.z,
                kind=kind,
                note=f"matter {kind}",
            )
        )
    if prefer_actionable:
        out.sort(
            key=lambda t: (
                _KIND_RANK.get(t.kind, 9),
                t.dist if t.dist is not None else 1e9,
                t.name,
            )
        )
    else:
        out.sort(key=lambda t: (t.dist if t.dist is not None else 1e9, t.name))
    log(
        f"interact scan matters={len(out)} "
        f"gather={sum(1 for t in out if t.kind==KIND_GATHER)} "
        f"pickup={sum(1 for t in out if t.kind==KIND_PICKUP)} "
        f"mark={sum(1 for t in out if t.kind==KIND_MARK)} "
        f"other={sum(1 for t in out if t.kind==KIND_OTHER)}"
    )
    return out[: int(limit)]


def nearest_actionable_matter(
    session,
    host_pos: tuple[float, float, float] | None = None,
    *,
    radius: float = 40.0,
    log: LogFn | None = None,
) -> InteractTarget | None:
    """Nearest non-mark matter (distance after kind rank). @author by ak"""
    items = scan_nearby_matters(
        session,
        host_pos,
        radius=radius,
        limit=40,
        prefer_actionable=True,
        log=log,
    )
    if not items:
        return None
    for t in items:
        if t.kind != KIND_MARK:
            return t
    return items[0]


# Alias used by UI "选最近"
nearest_mine_or_matter = nearest_actionable_matter


def pick_item(
    session,
    obj_id: int,
    *,
    name: str = "",
    tid: int | None = None,
    dist: float | None = None,
    kind: str = KIND_OTHER,
    log: LogFn | None = None,
) -> PickItemResult:
    """
    Remote-call plg::PickItem(__int64 id).
    int64 on x86 cdecl = low, high dwords.
    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        oid = int(obj_id)
    except (TypeError, ValueError):
        return PickItemResult(
            ok=False, error="invalid obj_id", name=name, tid=tid, dist=dist, kind=kind
        )
    if oid <= 0:
        return PickItemResult(
            ok=False, error="obj_id <= 0", obj_id=oid, name=name, tid=tid, dist=dist, kind=kind
        )

    low = oid & 0xFFFFFFFF
    high = (oid >> 32) & 0xFFFFFFFF
    try:
        va = _resolve_va(session, EXPORT_PICK_ITEM)
        log(
            f"PickItem va=0x{va:X} id={oid} (lo=0x{low:X} hi=0x{high:X}) "
            f"kind={kind} name={name!r} tid={tid} dist={dist}"
        )
        ret = remote_call_cdecl_x86(int(session.pid), va, [low, high])
        note = "call completed (void export; ret=EAX may be unused)"
        log(f"PickItem done ret_eax={ret} {note}")
        return PickItemResult(
            ok=True,
            obj_id=oid,
            tid=tid,
            name=name,
            dist=dist,
            kind=kind,
            func_va=va,
            ret=int(ret),
            note=note,
        )
    except Exception as e:
        log(f"PickItem failed: {e}")
        return PickItemResult(
            ok=False,
            obj_id=oid,
            tid=tid,
            name=name,
            dist=dist,
            kind=kind,
            error=str(e),
        )


def set_target(
    session,
    obj_id: int,
    *,
    log: LogFn | None = None,
) -> PickItemResult:
    """Remote plg::SetTarget(__int64 id) -> bool. @author by ak"""
    log = log or (lambda _m: None)
    oid = int(obj_id)
    low = oid & 0xFFFFFFFF
    high = (oid >> 32) & 0xFFFFFFFF
    try:
        va = _resolve_va(session, EXPORT_SET_TARGET)
        # log(f"SetTarget va=0x{va:X} id={oid} (lo=0x{low:X} hi=0x{high:X})")  # high-freq
        ret = remote_call_cdecl_x86(int(session.pid), va, [low, high])
        # log(f"SetTarget ret={ret}")
        return PickItemResult(
            ok=bool(ret),
            method="SetTarget",
            obj_id=oid,
            func_va=va,
            ret=int(ret),
            note="bool return in EAX",
        )
    except Exception as e:
        log(f"SetTarget failed: {e}")
        return PickItemResult(ok=False, method="SetTarget", obj_id=oid, error=str(e))


def auto_click_matter(
    session,
    obj_id: int,
    tid: int,
    *,
    name: str = "",
    dist: float | None = None,
    kind: str = KIND_OTHER,
    dyn: bool = False,
    log: LogFn | None = None,
    allow_remote: bool = False,
) -> PickItemResult:
    """
    AutoClickMatter / DynMatter for open-chest / gather cast-bar.

    Default: refuse remote CreateRemoteThread (SEH/Lua crash history).
    Prefer app.core.super_loot / bridge CMD_AUTO_CLICK_MATTER on UI thread.
    Set allow_remote=True only for controlled experiments.

    @author by ak
    """
    log = log or (lambda _m: None)
    method = "AutoClickDynMatter" if dyn else "AutoClickMatter"
    rva = RVA_AUTO_CLICK_DYN_MATTER if dyn else RVA_AUTO_CLICK_MATTER
    base = int(getattr(session, "module_base", 0) or 0)
    va = (base + int(rva)) if base else 0
    if not allow_remote:
        log(
            f"{method} SKIPPED remote (use bridge main-thread); "
            f"va=0x{va:X} id={obj_id} tid={tid} name={name!r}"
        )
        return PickItemResult(
            ok=False,
            method=method,
            obj_id=int(obj_id),
            tid=int(tid or 0),
            name=name,
            dist=dist,
            kind=kind,
            func_va=va,
            error="use bridge CMD_AUTO_CLICK_MATTER (UI thread); remote CRT disabled",
            note="RVA kept; super_loot open path uses bridge",
        )
    log(f"{method} remote EXPERIMENTAL va=0x{va:X} id={obj_id} tid={tid}")
    try:
        # Experimental: two-arg cdecl (id_lo, tid) — may still crash.
        ret = remote_call_cdecl_x86(
            int(session.pid),
            va,
            [int(obj_id) & 0xFFFFFFFF, int(tid) & 0xFFFFFFFF],
            timeout_ms=2500,
        )
        return PickItemResult(
            ok=True,
            method=method,
            obj_id=int(obj_id),
            tid=int(tid or 0),
            name=name,
            dist=dist,
            kind=kind,
            func_va=va,
            ret=int(ret),
            note="remote experimental",
        )
    except Exception as e:
        return PickItemResult(
            ok=False,
            method=method,
            obj_id=int(obj_id),
            tid=int(tid or 0),
            name=name,
            dist=dist,
            kind=kind,
            func_va=va,
            error=str(e),
        )


def execute_target(
    session,
    target: InteractTarget | dict,
    *,
    max_dist: float | None = 12.0,
    log: LogFn | None = None,
    try_autoclick: bool = False,
    try_pickitem: bool = True,
) -> InteractActionResult:
    """
    Execute user-selected target safely.

    Default path (no crash):
      1) SetTarget(id)  — plg export, ret=1 observed live
      2) optional PickItem(id) once — no multi layout spam

    AutoClickMatter is OFF (try_autoclick=False): remote SEH crashed client.
    @author by ak
    """
    log = log or (lambda _m: None)
    if isinstance(target, dict):
        name = str(target.get("name") or "")
        kind = str(target.get("kind") or classify_matter_name(name))
        t = InteractTarget(
            name=name,
            ptr=int(target.get("ptr") or 0),
            obj_id=target.get("obj_id"),
            tid=target.get("tid"),
            dist=target.get("dist"),
            x=target.get("x"),
            y=target.get("y"),
            z=target.get("z"),
            kind=kind,
            note=str(target.get("note") or ""),
        )
    else:
        t = target
        if not t.kind:
            t.kind = classify_matter_name(t.name)

    kind = t.kind or classify_matter_name(t.name)
    t.kind = kind
    tdict = t.to_dict()
    action = suggest_action(kind)
    log(
        f"execute kind={kind}({kind_label(kind)}) action={action} "
        f"name={t.name!r} id={t.obj_id} tid={t.tid} dist={t.dist}"
    )

    if action == "skip" or kind == KIND_MARK:
        return InteractActionResult(
            ok=False,
            action="skip",
            kind=kind,
            target=tdict,
            error="标志类默认不执行，请换目标",
        )

    if t.obj_id is None or int(t.obj_id) <= 0:
        return InteractActionResult(
            ok=False,
            action="none",
            kind=kind,
            target=tdict,
            error="缺少 obj_id，无法执行",
        )

    # Normalize id: keep full u64 if high looks like object tag.
    oid_raw = int(t.obj_id)
    low = oid_raw & 0xFFFFFFFF
    high = (oid_raw >> 32) & 0xFFFFFFFF
    if high and not (0x01000000 <= high <= 0x03FFFFFF):
        log(f"execute drop dirty high 0x{high:X}; use low32 0x{low:X}")
        oid = low
        high = 0
    else:
        oid = oid_raw
    tid = int(t.tid or 0) & 0xFFFFFFFF

    if max_dist is not None and t.dist is not None and float(t.dist) > float(max_dist):
        return InteractActionResult(
            ok=False,
            action="too_far",
            kind=kind,
            target=tdict,
            error=f"太远 dist={float(t.dist):.2f} > max={max_dist}",
            note="可先「走近选中」再执行",
        )

    attempts: list[dict] = []

    def _stop_if_denied(res: PickItemResult) -> bool:
        if not res or not res.error:
            return False
        err_s = str(res.error)
        denied = any(
            k in err_s
            for k in (
                "CreateRemoteThread",
                "VirtualAllocEx",
                "timeout",
                "ReadProcessMemory",
                "WriteProcessMemory",
                "implausible va",
            )
        )
        if denied:
            _note_remote_hard(session, err_s)
        return denied

    # Safe default: plg SetTarget only (export, no hang).
    # ChoiceObject / Pickup7267F0 are experimental and disabled by default
    # after hang+err=5 cascade on wrong VA.
    st = set_target(session, low if high == 0 else oid, log=log)
    attempts.append(st.to_dict())
    if _stop_if_denied(st):
        return InteractActionResult(
            ok=False,
            action=action,
            kind=kind,
            target=tdict,
            pick={"attempts": attempts, **st.to_dict()},
            error=f"remote denied: {st.error}",
            note="stop cascade",
        )

    # Optional experimental paths (off — enable only after main-thread work)
    try_choice = False
    try_pickup_note = False

    co = None
    if try_choice:
        co = choice_object(session, oid if high else low, log=log)
        attempts.append(co.to_dict())
        if _stop_if_denied(co):
            return InteractActionResult(
                ok=bool(st.ok),
                action=action,
                kind=kind,
                target=tdict,
                pick={"attempts": attempts, **co.to_dict()},
                error=f"remote denied: {co.error}",
                note="SetTarget may have worked; ChoiceObject stopped cascade",
            )

    pr_pick = None
    if try_pickup_note and kind == KIND_PICKUP:
        pr_pick = pickup_matter_call(
            session,
            oid if high else low,
            name=t.name,
            dist=t.dist,
            kind=kind,
            log=log,
        )
        attempts.append(pr_pick.to_dict())
        if _stop_if_denied(pr_pick):
            return InteractActionResult(
                ok=bool(st.ok),
                action=action,
                kind=kind,
                target=tdict,
                pick={"attempts": attempts, **pr_pick.to_dict()},
                error="pickup remote denied; stopped",
                note="SetTarget may have worked",
            )

    # AutoClick optional (OFF)
    if try_autoclick:
        ac = auto_click_matter(
            session,
            low,
            tid,
            name=t.name,
            dist=t.dist,
            kind=kind,
            dyn=False,
            log=log,
        )
        attempts.append(ac.to_dict())

    # plg PickItem once (optional baseline; known weak)
    pr = None
    if try_pickitem:
        pr = pick_item(
            session,
            low if high == 0 else oid,
            name=t.name,
            tid=tid,
            dist=t.dist,
            kind=kind,
            log=log,
        )
        attempts.append(pr.to_dict())

    best = st
    for cand in (st, co, pr_pick, pr):
        if cand and cand.ok and cand.ret not in (None, 0):
            best = cand
            break
    note = (
        f"safe path: SetTarget"
        f"{' + PickItem' if try_pickitem else ''}; "
        f"ChoiceObject/Pickup7267F0 OFF until VA+main-thread verified; "
        f"best={best.method if best else None} ret={best.ret if best else None}; "
        f"id low=0x{low:X} high=0x{high:X}. See REF_XAJH_HOOK_ALIGN.md"
    )
    log(note)
    return InteractActionResult(
        ok=bool(best and best.ok),
        action=action,
        kind=kind,
        target={**tdict, "obj_id": oid},
        pick={**(best.to_dict() if best else {}), "attempts": attempts},
        error=None if (best and best.ok) else (best.error if best else "no result"),
        note=note,
    )


def pick_nearest_matter(
    session,
    host_pos: tuple[float, float, float] | None = None,
    *,
    radius: float = 40.0,
    max_dist: float | None = 12.0,
    log: LogFn | None = None,
) -> tuple[InteractTarget | None, PickItemResult]:
    """Nearest actionable then safe execute. @author by ak"""
    log = log or (lambda _m: None)
    tgt = nearest_actionable_matter(session, host_pos, radius=radius, log=log)
    if tgt is None:
        return None, PickItemResult(ok=False, error="no matter in radius")
    act = execute_target(session, tgt, max_dist=max_dist, log=log)
    if act.pick:
        fields = PickItemResult.__dataclass_fields__
        d = {k: act.pick[k] for k in act.pick if k in fields}
        return tgt, PickItemResult(**d)
    return tgt, PickItemResult(
        ok=False,
        error=act.error,
        name=tgt.name,
        tid=tgt.tid,
        dist=tgt.dist,
        kind=tgt.kind,
        note=act.note,
    )
