# -*- coding: utf-8 -*-
"""
Lab-only skill recovery (后摇) research harness.

Automates: wait recovery window -> fire ONE method -> dense post samples ->
score immediate vs late local fields -> write md+json reports.

Does NOT integrate into product paths.

Live finding (2026-07-20): OnSkillStopped identity-match zeros id at t≈0,
then client may rewrite +10/+18/+7C within ~200ms while +4A0 busy stays clear
when fired late (high +20). Static chain: OnPerformSkill@0x761330 ->
SetCurActiveSkill@0x758DF0 reloads identity and zeros elapsed +20.

Methods
-------
- mem_busy / mem_gate / mem_perform: WPM local clears (baseline)
- onskill_*: bridge CMD_ONSKILL_STOPPED identity-match modes 0..3
- perform_stop65: mgr.vt+0x10(0x65) + clear host gate + restore type=2
  (skill-era perform is type=4; no 0x21)
- onskill_perform_stop65: stop65 then OnSkillStopped mode0
- cancel_session: bridge 0x21 negative control only

Matrix candidates (RECOVERY_CANDIDATES):
  soft_next (default suppress), hoststop_bit1, hoststop_local, stop65_onskill_hold

@author by ak
"""
from __future__ import annotations

import json
import re
import time
from typing import Callable

from app.core.skill_cast_probe import (
    CAST_CANCEL_BUSY_BIT,
    CAST_CANCEL_GATE_MINIMAL,
    CAST_CANCEL_PERFORM_STRIP,
    _unsafe_skill_write_block_reason,
    clear_cast_session,
    extract_cast_watch,
    resolve_cast_this,
)

LogFn = Callable[[str], None]

# HostStopSession local teardown VAs (exact skill.probe profile).
VA_CAST_SUB_CLEAR = 0x0073DE10  # thiscall, this = cast + 0xD8
VA_CLEAR_SKILL_OBJ = 0x00754BA0  # thiscall, this = cast-this (+CC/+60..7C)
# CECSkillSequence (cast+0xCC): CanCastNextUnit gate (0x582E00+)
# Live C handfeel: 754BA0->582BB0 resets +488=-1 but leaves +490/units,
# so next cast still fails until PerfTime ends. Force-unlock those fields.
VA_SEQ_RESET = 0x00582BB0  # thiscall(seq) partial reset
VA_SEQ_CLEAR_TYPE = 0x00582C00  # thiscall(seq, type 0..3) clear units
CAST_SKILL_OBJ_OFF = 0xCC  # SkillSequence*
CAST_SUB_CLEAR_OFF = 0xD8
SEQ_OFF_UNIT_PTR = 0x30
SEQ_OFF_UNIT_N = 0x3C
SEQ_OFF_GATE_488 = 0x488  # signed; >=0 blocks CanCastNextUnit
SEQ_OFF_ACTIVE_490 = 0x490  # byte unit walk count
SEQ_OFF_FLAG_498 = 0x498
SEQ_OFF_STATE_494 = 0x494

METHOD_MEM_BUSY = "mem_busy"
METHOD_MEM_GATE = "mem_gate"
METHOD_MEM_PERFORM = "mem_perform"
METHOD_ONSKILL = "onskill_stopped"
METHOD_ONSKILL_NOCONT = "onskill_nocont"
METHOD_ONSKILL_NOCONT_OBJ = "onskill_nocont_obj"
METHOD_ONSKILL_CLEAROBJ = "onskill_clearobj"
METHOD_PERFORM_STOP65 = "perform_stop65"
METHOD_ONSKILL_PERFORM_STOP65 = "onskill_perform_stop65"
METHOD_STOP65_ONSKILL_HOLD = "stop65_onskill_hold"
METHOD_HOSTSTOP_LOCAL = "hoststop_local"
METHOD_HOSTSTOP_BIT1 = "hoststop_bit1"
METHOD_SOFT_NEXT = "soft_next"
METHOD_KEY_INTERRUPT = "key_interrupt"
METHOD_CAST_INTERRUPT = "cast_interrupt"
METHOD_HAND_PROBE = "hand_probe"
METHOD_CANCEL21 = "cancel_session"
KEY_INTERRUPT_VK_DEFAULT = 0x58  # X — clears recovery (user)
KEY_INTERRUPT_VK_SPACE = 0x20  # Space — clears block after X (user)
# User macro: both Space + X. Order: X (cancel recovery) then Space (clear block).
KEY_INTERRUPT_VK_SEQ_DEFAULT: tuple[int, ...] = (0x58, 0x20)

# Hand_probe 20260722_075413: X interrupt replaces cast+18 with block config 0xE07
# (gate 500/3591). Generic path = cast this skill via 0x53E110, not VK.
INTERRUPT_SKILL_ID_DEFAULT = 0xE07

# 0x75F000 next-skill fail gates on host (=cast+8), NOT skill CD (game has no CD).
# bit1 of host+0x1B8 -> return 0x0F (tester 0x4BA1D0). Set@0x6EAB98 clear@0x6EADF0.
HOST_OFF_FLAGS_1B8 = 0x1B8
HOST_OFF_BYTE_4A4 = 0x4A4
HOST_OFF_FLAGS_3D8 = 0x3D8
HOST_OFF_STATE_193C = 0x193C
HOST_OFF_SESSION_41C = 0x41C
CAST_OFF_TAIL_4A4 = 0x4A4
CAST_OFF_TIMER_4A8 = 0x4A8
CAST_OFF_TIMER_8D4 = 0x8D4
CAST_OFF_TIMER_8D8 = 0x8D8  # bytes 8D8..8DC pack
VA_CAST_TIMER_ZERO = 0x00754CF0  # thiscall(cast, int<=0) zeros +4A4 if flags allow
VA_CAST_FLAG_8D8 = 0x00757740  # thiscall(cast, byte) ; 0 path zeros +4A4

METHOD_LABELS = {
    METHOD_MEM_BUSY: "内存:仅busy bit",
    METHOD_MEM_GATE: "内存:ID+busy",
    METHOD_MEM_PERFORM: "内存:ID+busy+perform",
    METHOD_ONSKILL: "桥接:OnSkillStopped",
    METHOD_ONSKILL_NOCONT: "桥接:OnSkill cont=0",
    METHOD_ONSKILL_NOCONT_OBJ: "桥接:cont0+754BA0",
    METHOD_ONSKILL_CLEAROBJ: "桥接:cont1+754BA0",
    METHOD_PERFORM_STOP65: "perform:vt+0x10(0x65)",
    METHOD_ONSKILL_PERFORM_STOP65: "桥接:OnSkill+stop65",
    METHOD_STOP65_ONSKILL_HOLD: "组合:stop65+OnSkill+压回填",
    METHOD_HOSTSTOP_LOCAL: "组合:HostStop本地拆",
    METHOD_HOSTSTOP_BIT1: "组合:HostStop+清门+Seq解锁",
    METHOD_SOFT_NEXT: "软压:stop65+Seq+清ID(禁unit)",
    METHOD_KEY_INTERRUPT: "对照:真键X+空格(非通用)",
    METHOD_CAST_INTERRUPT: "通用:桥接cast打断v2(0xE07)",
METHOD_HAND_PROBE: "手按X/空格采样(不注入)",
    METHOD_CANCEL21: "桥接:0x21取消(负对照)",
}

# Native mode for CMD_ONSKILL_STOPPED (see dllmain / xajh_bridge).
ONSKILL_NATIVE_MODE = {
    METHOD_ONSKILL: 0,
    METHOD_ONSKILL_NOCONT: 1,
    METHOD_ONSKILL_NOCONT_OBJ: 2,
    METHOD_ONSKILL_CLEAROBJ: 3,
}

ALL_METHODS = (
    METHOD_MEM_BUSY,
    METHOD_MEM_GATE,
    METHOD_MEM_PERFORM,
    METHOD_ONSKILL,
    METHOD_ONSKILL_NOCONT,
    METHOD_ONSKILL_NOCONT_OBJ,
    METHOD_ONSKILL_CLEAROBJ,
    METHOD_PERFORM_STOP65,
    METHOD_ONSKILL_PERFORM_STOP65,
    METHOD_STOP65_ONSKILL_HOLD,
    METHOD_HOSTSTOP_LOCAL,
    METHOD_HOSTSTOP_BIT1,
    METHOD_SOFT_NEXT,
    METHOD_KEY_INTERRUPT,
    METHOD_CAST_INTERRUPT,
    METHOD_HAND_PROBE,
    METHOD_CANCEL21,
)

# Positive recovery candidates (not known whole-cast cancel).
# Prefer anti-refill variants after identity-match baseline.
# Live matrix 2026-07-21: only OnSkill+stop65 got local_full_clear_stable
# (stop65 alone leaves cast id/busy; OnSkill alone refills via type=4 perform).
# Live 2026-07-21:
# - late OnSkill+stop65: full_clear_stable once
# - early OnSkill+stop65: id refill @~80ms (busy stays 0) -> stuck, no next cast
# HOLD = stop65 -> OnSkill -> poll ~0.7s and re-strip id / re-stop type=4
# HostStopSession local order (no 0x21):
# stop65(type4) -> 0x73DE10(cast+0xD8) -> 0x754BA0 -> OnSkill cont=0 -> strip hold
RECOVERY_CANDIDATES = (
    METHOD_CAST_INTERRUPT,
    METHOD_HAND_PROBE,
    METHOD_SOFT_NEXT,
    METHOD_HOSTSTOP_BIT1,
)

# Dense early samples catch id-refill (~100ms) without huge runtime.
DEFAULT_POST_DELAYS_S: tuple[float, ...] = (
    0.0,
    0.016,
    0.032,
    0.048,
    0.080,
    0.120,
    0.200,
    0.300,
    0.500,
    0.800,
)

_SERIES_KEYS = ("+10", "+14", "+18", "+4A0", "+7C", "+20", "+24", "+80", "+2C0", "+2E4", "+3A8", "+3CC", "+60", "+64", "+68", "+6C", "+70", "+74", "+78")


def _u(m: dict, key: str) -> int:
    return int(m.get(key) or 0)


def _hx(m: dict, key: str) -> str:
    return f"0x{_u(m, key):X}"


def watch_summary(watch: dict | None) -> str:
    w = watch or {}
    # 0x754300 busy: [slot+8]!=0 && [slot+0x2C]!=0xFFFFFFFF
    a8, a2c = _u(w, "+2C0"), _u(w, "+2E4")
    b8, b2c = _u(w, "+3A8"), _u(w, "+3CC")
    slot_a = int(bool(a8) and a2c != 0xFFFFFFFF)
    slot_b = int(bool(b8) and b2c != 0xFFFFFFFF)
    return (
        f"id={_hx(w, '+10')} +14={_hx(w, '+14')} +18={_hx(w, '+18')} "
        f"4a0={_hx(w, '+4A0')} +7C={_hx(w, '+7C')} +20={_hx(w, '+20')} "
        f"slotA={slot_a}({_hx(w, '+2C0')}/{_hx(w, '+2E4')}) "
        f"slotB={slot_b}({_hx(w, '+3A8')}/{_hx(w, '+3CC')})"
    )


def slot_busy_flags(watch: dict | None) -> dict:
    """Mirror native 0x754300 on cast+0x2B8 / +0x3A0 sub-slots."""
    w = watch or {}
    a8, a2c = _u(w, "+2C0"), _u(w, "+2E4")
    b8, b2c = _u(w, "+3A8"), _u(w, "+3CC")
    return {
        "slot_a_busy": bool(a8) and a2c != 0xFFFFFFFF,
        "slot_b_busy": bool(b8) and b2c != 0xFFFFFFFF,
        "slot_a_plus8": a8,
        "slot_a_plus2c": a2c,
        "slot_b_plus8": b8,
        "slot_b_plus2c": b2c,
        "any_slot_busy": (bool(a8) and a2c != 0xFFFFFFFF)
        or (bool(b8) and b2c != 0xFFFFFFFF),
    }



def cast_nonzero_dwords(session, cast_this: int, *, end_off: int = 0x8E0) -> dict[str, int]:
    """Read cast-this dwords [0, end) and keep non-trivial values (lab only)."""
    out: dict[str, int] = {}
    cast = int(cast_this or 0) & 0xFFFFFFFF
    if not cast or not getattr(session, "pid", None):
        return out
    try:
        from app.core.remote_runtime import remote_read_bytes
        import struct as _st

        raw = remote_read_bytes(int(session.pid), cast, int(end_off))
        if not raw:
            return out
        n = len(raw) // 4
        for i in range(n):
            v = _st.unpack_from("<I", raw, i * 4)[0]
            if v == 0 or v == 0xFFFFFFFF:
                continue
            # skip obvious float-ish noise? keep all non-trivial
            out[f"+{i*4:X}"] = v
    except Exception:
        return out
    return out


def cast_dump_diff(before: dict[str, int], after: dict[str, int]) -> dict[str, dict]:
    keys = set(before) | set(after)
    diff: dict[str, dict] = {}
    for k in sorted(keys, key=lambda x: int(x[1:], 16)):
        b, a = int(before.get(k) or 0), int(after.get(k) or 0)
        if b != a:
            diff[k] = {"before": b, "after": a}
    return diff


def is_valid_cast_skill_id(skill_id: int | None) -> bool:
    """
    True for a plausible skill identity on cast+0x10.

    Live 20260722 suppress: EDGE on id=0xFFFFFFFF type=2 busy=0 — ghost
    residue, not a real cast. FULL ran with no suppress feel.

    Reject 0, -1/0xFFFFFFFF, and pointer-looking high values.
    Real live ids seen: 0x9563, 0x12609 (all < 0x100000).
    """
    sid = int(skill_id or 0) & 0xFFFFFFFF
    if sid == 0 or sid == 0xFFFFFFFF:
        return False
    if sid >= 0x01000000:
        return False
    return True


def is_recovery_window(watch: dict | None) -> bool:
    """
    Classic recovery window: identity present AND busy bit armed.
    Kept for scoring / matrix compatibility.
    """
    w = watch or {}
    return bool(
        is_valid_cast_skill_id(w.get("+10")) and (int(w.get("+4A0") or 0) & 1)
    )


def is_cast_active(watch: dict | None) -> bool:
    """Any cast identity / busy residue (for dirty strip)."""
    w = watch or {}
    sid = int(w.get("+10") or 0) & 0xFFFFFFFF
    # Ghost 0xFFFFFFFF alone is dirty noise (strip), not a real arm.
    ghost = sid == 0xFFFFFFFF
    return bool(
        is_valid_cast_skill_id(sid)
        or ghost
        or int(w.get("+18") or 0)
        or int(w.get("+80") or 0)
        or (int(w.get("+4A0") or 0) & 1)
        or int(w.get("+7C") or 0)
        or int(w.get("+60") or 0)
    )


def is_armed_cast(watch: dict | None, perform: dict | None = None) -> bool:
    """
    Skill is ARMED enough to cancel — not the id-load gap / ghost id.

    Live 2026-07-21 02:31: firing on bare +10 (busy=0, perform type=2) made
    OnSkill clear id, then real skill armed AFTER as type=4 — no unlock feel.
    Live 2026-07-22: id=0xFFFFFFFF type=2 busy=0 false EDGE — no suppress feel.

    Require valid skill id AND (busy bit OR skill-era perform type=4 OR
    late elapsed on valid id). Never arm on ghost 0xFFFFFFFF / type=2 only.
    """
    w = watch or {}
    sid_ok = is_valid_cast_skill_id(w.get("+10"))
    busy = bool(int(w.get("+4A0") or 0) & 1)
    p = perform or {}
    ptype = int(p.get("perform_type") or 0)
    # skill-era perform is always arm-worthy (id may lag a frame)
    if ptype == 4:
        return True
    if busy and sid_ok:
        return True
    if busy and ptype not in (0, 2):
        return True
    # late-ish elapsed with VALID id (never ghost 0xFFFFFFFF)
    if sid_ok and int(w.get("+20") or 0) >= 0x10 and ptype != 2:
        return True
    if (
        sid_ok
        and p.get("active")
        and not p.get("is_base")
        and not p.get("is_matter")
        and ptype not in (0, 2)
    ):
        return True
    return False


def cast_watch_dirty(watch: dict | None) -> bool:
    """Any cast-side lock field still set."""
    return is_cast_active(watch)


def is_idle_watch(watch: dict | None) -> bool:
    """True when no *valid* skill id and busy bit off (ghost FFFFFFFF = idle)."""
    w = watch or {}
    sid = _u(w, "+10") & 0xFFFFFFFF
    id_clear = (sid == 0) or (sid == 0xFFFFFFFF) or (not is_valid_cast_skill_id(sid))
    return id_clear and ((_u(w, "+4A0") & 1) == 0)




def is_edge_idle(watch: dict | None, perform: dict | None = None) -> bool:
    """
    Fully clean baseline before rising-edge arm.
    id=0, busy=0, not skill-era perform (type!=4/etc).
    """
    if not is_idle_watch(watch):
        return False
    return not perform_is_skillish(perform)


def hit_meta(watch: dict | None, perform: dict | None = None) -> dict:
    """Compact HIT diagnostics for logs/reports (generic, not skill-specific)."""
    w = watch or {}
    p = perform or {}
    total = int(w.get("+1C") or 0)
    elapsed = int(w.get("+20") or 0)
    remain = max(0, total - elapsed) if total > 0 else None
    return {
        "id": int(w.get("+10") or 0),
        "p14": int(w.get("+14") or 0),
        "p18": int(w.get("+18") or 0),
        "busy": int(w.get("+4A0") or 0) & 1,
        "p20": elapsed,
        "p1c": total,
        "remain_ms": remain,
        "ptype": p.get("perform_type"),
        "psub": p.get("perform_subtype"),
        "pbase": p.get("is_base"),
        "pmatter": p.get("is_matter"),
        "armed": is_armed_cast(w, p),
        "skillish": perform_is_skillish(p),
    }


def parse_onskill_native_note(note: str) -> dict:
    """Parse ONSKILL_STOPPED diagnostic from bridge note/err."""
    text = str(note or "")
    out = {
        "native_cleared": None,
        "native_id0": None,
        "native_id1": None,
        "native_id2": None,
        "native_cont": None,
        "native_mode": None,
        "native_clear_obj": None,
        "raw": text,
    }
    m = re.search(
        r"id=0x([0-9A-Fa-f]+),0x([0-9A-Fa-f]+),0x([0-9A-Fa-f]+)\s+cleared=([01])",
        text,
    )
    if not m:
        return out
    out["native_id0"] = int(m.group(1), 16)
    out["native_id1"] = int(m.group(2), 16)
    out["native_id2"] = int(m.group(3), 16)
    out["native_cleared"] = int(m.group(4)) == 1
    mc = re.search(r"cont=(\d+)", text)
    if mc:
        out["native_cont"] = int(mc.group(1))
    mm = re.search(r"mode=(\d+)", text)
    if mm:
        out["native_mode"] = int(mm.group(1))
    mo = re.search(r"clear_obj=(\d+)", text)
    if mo:
        out["native_clear_obj"] = int(mo.group(1))
    return out


def classify_local(before: dict, after: dict) -> dict:
    """Score one after-snapshot against before (local fields only)."""
    b10, a10 = _u(before, "+10"), _u(after, "+10")
    b4a0, a4a0 = _u(before, "+4A0"), _u(after, "+4A0")
    b7c, a7c = _u(before, "+7C"), _u(after, "+7C")
    id_cleared = bool(b10) and a10 == 0
    # busy_ok = after idle; busy_cleared = transition from busy->idle.
    # Live 095755: fire often with before busy already 0 → old logic forced
    # local_id_only even when id fully wiped. Treat after-idle as ok.
    busy_ok = (a4a0 & 1) == 0
    busy_cleared = bool(b4a0 & 1) and busy_ok
    flags_cleared = bool(b7c) and a7c == 0
    still_active = is_recovery_window(after)
    b18, a18 = _u(before, "+18"), _u(after, "+18")
    id_tail_cleared = a18 == 0
    if id_cleared and busy_ok and id_tail_cleared:
        verdict = "local_full_clear"
    elif id_cleared and busy_ok and not id_tail_cleared:
        # gate_minimal historically left +18; not a true idle identity wipe
        verdict = "local_id_busy_clear_tail_left"
    elif busy_cleared and not id_cleared:
        verdict = "local_busy_only"
    elif id_cleared and not busy_ok:
        verdict = "local_id_only"
    elif still_active:
        verdict = "local_still_active"
    else:
        verdict = "local_idle_other"
    return {
        "verdict": verdict,
        "id_cleared": id_cleared,
        "id_tail_cleared": id_tail_cleared,
        "busy_cleared": busy_cleared,
        "busy_ok": busy_ok,
        "flags_cleared": flags_cleared,
        "still_active": still_active,
        "before_id": b10,
        "after_id": a10,
        "before_18": b18,
        "after_18": a18,
        "before_4a0": b4a0,
        "after_4a0": a4a0,
    }


def _field_diff(before: dict, after: dict) -> dict[str, dict]:
    """Return changed watched fields between two snapshots."""
    changed: dict[str, dict] = {}
    for k in _SERIES_KEYS:
        b, a = _u(before, k), _u(after, k)
        if b != a:
            changed[k] = {"before": b, "after": a}
    return changed


def classify_series(
    before: dict,
    series: list[dict],
    *,
    native: dict | None = None,
) -> dict:
    """
    Score immediate vs late samples and pin first id-refill time.
    """
    native = native or {}
    if not series:
        empty = classify_local(before, {})
        empty["verdict"] = "local_no_sample"
        empty["id_reverted"] = False
        empty["stable_full_clear"] = False
        empty["id_refill_t_s"] = None
        empty["refill_fields"] = {}
        empty["native_cleared"] = native.get("native_cleared")
        empty["native_vs_python"] = "no_sample"
        return empty

    scores = []
    for fr in series:
        sc = classify_local(before, fr.get("watch") or {})
        sc["t_s"] = float(fr.get("t_s") or 0)
        scores.append(sc)

    first, last = scores[0], scores[-1]
    first_watch = series[0].get("watch") or {}
    last_watch = series[-1].get("watch") or {}

    id_cleared_any = any(s.get("id_cleared") for s in scores)
    id_cleared_first = bool(first.get("id_cleared"))
    id_cleared_last = bool(last.get("id_cleared"))
    busy_cleared_first = bool(first.get("busy_cleared"))
    busy_cleared_last = bool(last.get("busy_cleared"))
    busy_ok_first = bool(first.get("busy_ok", first.get("busy_cleared")))
    busy_ok_last = bool(last.get("busy_ok", last.get("busy_cleared")))
    id_reverted = id_cleared_first and (not id_cleared_last) and bool(_u(before, "+10"))
    stable_full = all(
        s.get("id_cleared")
        and (s.get("busy_ok") if "busy_ok" in s else s.get("busy_cleared"))
        and s.get("id_tail_cleared", True)
        for s in scores
    )

    # First time id becomes non-zero again after an immediate clear.
    id_refill_t_s = None
    refill_watch = None
    if id_cleared_first:
        for fr in series[1:]:
            w = fr.get("watch") or {}
            if _u(w, "+10") != 0:
                id_refill_t_s = float(fr.get("t_s") or 0)
                refill_watch = w
                break

    refill_fields = {}
    if id_reverted and refill_watch is not None:
        refill_fields = _field_diff(first_watch, refill_watch)

    if stable_full:
        verdict = "local_full_clear_stable"
    elif id_reverted and busy_ok_last:
        verdict = "local_clear_then_id_refilled"
    elif id_cleared_any and busy_ok_last:
        verdict = "local_full_clear_unstable"
    elif busy_cleared_last and not id_cleared_any:
        verdict = "local_busy_only"
    elif id_cleared_last and not busy_ok_last:
        verdict = "local_id_only"
    elif last.get("still_active"):
        verdict = "local_still_active"
    else:
        verdict = last.get("verdict") or "local_idle_other"

    native_cleared = native.get("native_cleared")
    if native_cleared is True and id_cleared_first:
        native_vs = "agree_immediate_clear"
    elif native_cleared is True and not id_cleared_first:
        native_vs = "native_cleared_python_missed_t0"
    elif native_cleared is False and id_cleared_first:
        native_vs = "python_clear_native_said_no"
    elif native_cleared is False:
        native_vs = "agree_not_cleared"
    else:
        native_vs = "native_unknown"

    return {
        "verdict": verdict,
        "id_cleared": id_cleared_last,
        "id_cleared_immediate": id_cleared_first,
        "id_cleared_any": id_cleared_any,
        "id_reverted": id_reverted,
        "id_refill_t_s": id_refill_t_s,
        "refill_fields": refill_fields,
        "busy_cleared": busy_cleared_last,
        "busy_cleared_immediate": busy_cleared_first,
        "flags_cleared": bool(last.get("flags_cleared")),
        "still_active": bool(last.get("still_active")),
        "stable_full_clear": stable_full,
        "before_id": _u(before, "+10"),
        "after_id": last.get("after_id"),
        "before_4a0": _u(before, "+4A0"),
        "after_4a0": last.get("after_4a0"),
        "immediate_verdict": first.get("verdict"),
        "late_verdict": last.get("verdict"),
        "native_cleared": native_cleared,
        "native_vs_python": native_vs,
        "samples": scores,
    }


def snapshot_cast(session, *, log: LogFn | None = None) -> tuple[dict, int, int]:
    """Return (watch, cast_this, host_ptr)."""
    log = log or (lambda _m: None)
    p = resolve_cast_this(session, log=log, with_dumps=True)
    watch = extract_cast_watch(p.cast_dump or b"") if p.ok else {}
    return (
        watch,
        int(getattr(p, "cast_this", 0) or 0),
        int(getattr(p, "host_ptr", 0) or 0),
    )



def snapshot_host_next_skill_gates(
    session,
    *,
    host_ptr: int = 0,
    cast_this: int = 0,
    log: LogFn | None = None,
) -> dict:
    """
    Read host-side fields that 0x75F000 checks before next cast.

    NO-CD game: these are action/session gates, not skill cooldown.
    Key: host+0x1B8 bit1 -> fail code 0x0F (blocks 0x75F000 early).

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "host": 0,
        "cast": 0,
        "h1b8": 0,
        "h1b8_bit1": False,
        "h4a4_u8": 0,
        "h4a4_u32": 0,
        "h4a5_u8": 0,
        "h4a6_u8": 0,
        "h4a7_u8": 0,
        "h1b8_bit7": False,
        "lock_587b00": False,
        "h3d8": 0,
        "h3d8_bit2": False,
        "h193c": 0,
        "h41c": 0,
        "c4a4": 0,
        "error": "",
    }
    try:
        from app.core.remote_runtime import remote_read_bytes

        host = int(host_ptr or 0) & 0xFFFFFFFF
        cast = int(cast_this or 0) & 0xFFFFFFFF
        if not host or not cast:
            _w, c2, h2 = snapshot_cast(session, log=log)
            host = host or (int(h2) & 0xFFFFFFFF)
            cast = cast or (int(c2) & 0xFFFFFFFF)
        if not host:
            out["error"] = "host null"
            return out

        def _ru32(addr: int) -> int:
            raw = remote_read_bytes(int(session.pid), int(addr) & 0xFFFFFFFF, 4)
            if not raw or len(raw) < 4:
                return 0
            return int.from_bytes(raw[:4], "little")

        def _ru8(addr: int) -> int:
            raw = remote_read_bytes(int(session.pid), int(addr) & 0xFFFFFFFF, 1)
            if not raw:
                return 0
            return int(raw[0])

        h1b8 = _ru32(host + HOST_OFF_FLAGS_1B8)
        h3d8 = _ru32(host + HOST_OFF_FLAGS_3D8)
        h4a4 = _ru8(host + HOST_OFF_BYTE_4A4)
        h4a5 = _ru8(host + HOST_OFF_BYTE_4A4 + 1)
        h4a6 = _ru8(host + HOST_OFF_BYTE_4A4 + 2)
        h4a7 = _ru8(host + HOST_OFF_BYTE_4A4 + 3)
        h4a4_u32 = _ru32(host + HOST_OFF_BYTE_4A4)
        bit7 = bool(h1b8 & 0x80)
        # 0x587B00: locked if (1b8&0x80) or 4a6 or 4a4
        lock_587b00 = bool(bit7 or h4a6 or h4a4)
        out.update(
            {
                "ok": True,
                "host": host,
                "cast": cast,
                "h1b8": h1b8,
                "h1b8_bit1": bool(h1b8 & 2),
                "h1b8_bit7": bit7,
                "h4a4_u8": h4a4,
                "h4a4_u32": h4a4_u32,
                "h4a5_u8": h4a5,
                "h4a6_u8": h4a6,
                "h4a7_u8": h4a7,
                "lock_587b00": lock_587b00,
                "h3d8": h3d8,
                "h3d8_bit2": bool((h3d8 >> 2) & 1),
                "h193c": _ru32(host + HOST_OFF_STATE_193C),
                "h41c": _ru32(host + HOST_OFF_SESSION_41C),
                "c4a4": _ru32(cast + CAST_OFF_TAIL_4A4) if cast else 0,
            }
        )
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def host_gates_summary(g: dict | None) -> str:
    g = g or {}
    if not g.get("ok"):
        return f"host_gates err={g.get('error')!r}"
    return (
        f"1b8=0x{int(g.get('h1b8') or 0):X} bit1={int(bool(g.get('h1b8_bit1')))} "
        f"bit7={int(bool(g.get('h1b8_bit7')))} "
        f"4a4-7={int(g.get('h4a4_u8') or 0):02X}{int(g.get('h4a5_u8') or 0):02X}"
        f"{int(g.get('h4a6_u8') or 0):02X}{int(g.get('h4a7_u8') or 0):02X} "
        f"lock587={int(bool(g.get('lock_587b00')))} "
        f"3d8=0x{int(g.get('h3d8') or 0):X} bit2={int(bool(g.get('h3d8_bit2')))} "
        f"193c=0x{int(g.get('h193c') or 0):X} 41c=0x{int(g.get('h41c') or 0):X} "
        f"c4a4=0x{int(g.get('c4a4') or 0):X}"
    )



def snapshot_cast_timer_block(
    session,
    *,
    cast_this: int = 0,
    log: LogFn | None = None,
) -> dict:
    """Read cast timer/lock tail used by 0x754CF0 / 0x757740 / OnSkill 8DA."""
    log = log or (lambda _m: None)
    out = {
        "ok": False,
        "cast": 0,
        "c4a4": 0,
        "c4a8": 0,
        "c8d4": 0,
        "c8d8": 0,
        "c8d9": 0,
        "c8da": 0,
        "c8db": 0,
        "c8dc": 0,
        "error": "",
    }
    try:
        from app.core.remote_runtime import remote_read_bytes

        cast = int(cast_this or 0) & 0xFFFFFFFF
        if not cast:
            _w, cast, _h = snapshot_cast(session, log=log)
            cast = int(cast or 0) & 0xFFFFFFFF
        if not cast:
            out["error"] = "cast null"
            return out

        def ru32(off):
            raw = remote_read_bytes(int(session.pid), (cast + off) & 0xFFFFFFFF, 4)
            return int.from_bytes(raw[:4], "little") if raw and len(raw) >= 4 else 0

        def ru8(off):
            raw = remote_read_bytes(int(session.pid), (cast + off) & 0xFFFFFFFF, 1)
            return int(raw[0]) if raw else 0

        out.update(
            {
                "ok": True,
                "cast": cast,
                "c4a4": ru32(CAST_OFF_TAIL_4A4),
                "c4a8": ru32(CAST_OFF_TIMER_4A8),
                "c8d4": ru32(CAST_OFF_TIMER_8D4),
                "c8d8": ru8(CAST_OFF_TIMER_8D8),
                "c8d9": ru8(CAST_OFF_TIMER_8D8 + 1),
                "c8da": ru8(CAST_OFF_TIMER_8D8 + 2),
                "c8db": ru8(CAST_OFF_TIMER_8D8 + 3),
                "c8dc": ru8(CAST_OFF_TIMER_8D8 + 4),
            }
        )
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def cast_timer_summary(g: dict | None) -> str:
    g = g or {}
    if not g.get("ok"):
        return f"cast_timer err={g.get('error')!r}"
    return (
        f"8d4=0x{int(g.get('c8d4') or 0):X} "
        f"8d8-c={int(g.get('c8d8') or 0):02X}{int(g.get('c8d9') or 0):02X}"
        f"{int(g.get('c8da') or 0):02X}{int(g.get('c8db') or 0):02X}{int(g.get('c8dc') or 0):02X} "
        f"4a4=0x{int(g.get('c4a4') or 0):X} 4a8=0x{int(g.get('c4a8') or 0):X}"
    )


def lab_clear_cast_timer_block(
    session,
    *,
    cast_this: int = 0,
    log: LogFn | None = None,
) -> dict:
    """
    Lab-only: clear cast timer/lock block beyond 0x4B0.

    Static:
    - 0x754CF0(cast, <=0): if 8d9==0, copy 4a4->4a8 and zero 4a4
    - 0x757740(cast, 0): clear 8d9; if 8d4<=0 zero 4a4
    - OnSkillStopped toggles 8da; OnPerformSkill reads 8da
    - Full reinit ~0x764283 zeros 8d4/8d8-8dc/4a0/4a4/4a8/+C

    We WPM-zero the block (no full reinit). Requires unsafe skill write gates.

    @author by ak
    """
    log = log or (lambda _m: None)
    out = {
        "ok": False,
        "cast": 0,
        "before": {},
        "after": {},
        "wrote": [],
        "error": "",
        "note": "",
    }
    blocked = _unsafe_skill_write_block_reason(session)
    if blocked:
        out["error"] = blocked
        return out
    try:
        from app.core.remote_runtime import remote_write_bytes

        before = snapshot_cast_timer_block(session, cast_this=cast_this, log=log)
        cast = int(before.get("cast") or cast_this or 0) & 0xFFFFFFFF
        if not cast:
            out["error"] = "cast null"
            out["before"] = before
            return out
        out["cast"] = cast
        out["before"] = before
        wrote = []
        # zero timer float pair
        n1 = remote_write_bytes(int(session.pid), cast + CAST_OFF_TAIL_4A4, bytes(8))  # 4a4+4a8
        if n1:
            wrote.append("+4A4/+4A8")
        # zero 8d4 dword
        n2 = remote_write_bytes(int(session.pid), cast + CAST_OFF_TIMER_8D4, bytes(4))
        if n2:
            wrote.append("+8D4")
        # zero 8d8..8dc (5 bytes)
        n3 = remote_write_bytes(int(session.pid), cast + CAST_OFF_TIMER_8D8, bytes(5))
        if n3:
            wrote.append("+8D8..8DC")
        after = snapshot_cast_timer_block(session, cast_this=cast, log=log)
        out["after"] = after
        out["wrote"] = wrote
        out["ok"] = bool(wrote) and after.get("ok")
        out["note"] = (
            f"timer {cast_timer_summary(before)} -> {cast_timer_summary(after)} wrote={wrote}"
        )
        log(f"lab_clear_cast_timer_block {out['note']}")
        return out
    except Exception as e:
        out["error"] = str(e)
        return out



def snapshot_skill_sequence(
    session,
    *,
    cast_this: int = 0,
    seq_ptr: int = 0,
    log: LogFn | None = None,
) -> dict:
    """Read CECSkillSequence at cast+0xCC (or explicit seq_ptr)."""
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "cast": 0,
        "seq": 0,
        "unit_n": 0,
        "gate_488": 0,
        "active_490": 0,
        "flag_498": 0,
        "state_494": 0,
        "unit0": 0,
        "unit0_code": 0,
        "can_cast_next_hint": None,
        "error": "",
    }
    try:
        from app.core.remote_runtime import remote_read_bytes

        cast = int(cast_this or 0) & 0xFFFFFFFF
        seq = int(seq_ptr or 0) & 0xFFFFFFFF
        if not seq:
            if not cast:
                _w, cast, _h = snapshot_cast(session, log=log)
                cast = int(cast or 0) & 0xFFFFFFFF
            if cast:
                raw = remote_read_bytes(int(session.pid), (cast + CAST_SKILL_OBJ_OFF) & 0xFFFFFFFF, 4)
                seq = int.from_bytes(raw[:4], "little") if raw and len(raw) >= 4 else 0
        out["cast"] = cast
        out["seq"] = seq
        if not seq or seq < 0x10000:
            out["error"] = "seq null"
            out["ok"] = True  # readable idle
            out["can_cast_next_hint"] = True
            return out

        def ru32(off: int) -> int:
            raw = remote_read_bytes(int(session.pid), (seq + off) & 0xFFFFFFFF, 4)
            return int.from_bytes(raw[:4], "little") if raw and len(raw) >= 4 else 0

        def ru8(off: int) -> int:
            raw = remote_read_bytes(int(session.pid), (seq + off) & 0xFFFFFFFF, 1)
            return int(raw[0]) if raw else 0

        gate = ru32(SEQ_OFF_GATE_488)
        active = ru8(SEQ_OFF_ACTIVE_490)
        unit_n = ru32(SEQ_OFF_UNIT_N)
        unit_ptr = ru32(SEQ_OFF_UNIT_PTR)
        unit0 = 0
        unit0_code = 0
        if unit_ptr and unit_ptr > 0x10000:
            rawu = remote_read_bytes(int(session.pid), unit_ptr & 0xFFFFFFFF, 16)
            if rawu and len(rawu) >= 16:
                unit0 = int.from_bytes(rawu[0:4], "little")
                unit0_code = int.from_bytes(rawu[12:16], "little")
        # Mirror 0x582E00: gate_488>=0 => block; active_490==0 => allow;
        # else unit walk may still block.
        gate_s = gate if gate < 0x80000000 else gate - 0x100000000
        if gate_s >= 0:
            hint = False
        elif active == 0:
            hint = True
        elif unit0 and unit0_code in (0x69, 0x6B, 0x6C, 0x70, 0x71):
            hint = False
        else:
            hint = active == 0 or unit0 == 0
        out.update(
            {
                "ok": True,
                "unit_n": unit_n,
                "gate_488": gate,
                "active_490": active,
                "flag_498": ru8(SEQ_OFF_FLAG_498),
                "state_494": ru32(SEQ_OFF_STATE_494),
                "unit0": unit0,
                "unit0_code": unit0_code,
                "can_cast_next_hint": hint,
            }
        )
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def skill_sequence_summary(g: dict | None) -> str:
    g = g or {}
    if not g.get("ok") and g.get("error"):
        return f"seq err={g.get('error')!r}"
    seq = int(g.get("seq") or 0)
    if not seq:
        return "seq=null (idle/hint=ok)"
    gate = int(g.get("gate_488") or 0)
    gate_s = gate if gate < 0x80000000 else gate - 0x100000000
    return (
        f"seq=0x{seq:X} 488={gate_s} 490={int(g.get('active_490') or 0)} "
        f"n={int(g.get('unit_n') or 0)} u0=0x{int(g.get('unit0') or 0):X}/"
        f"code=0x{int(g.get('unit0_code') or 0):X} "
        f"hint={g.get('can_cast_next_hint')}"
    )


def lab_force_can_cast_next_unit(
    session,
    *,
    cast_this: int = 0,
    seq_ptr: int = 0,
    log: LogFn | None = None,
    mode: str = "safe",
) -> dict:
    """
    Lab-only: force CECSkillSequence toward CanCastNextUnit-ready.

    Static (0x582E00+):
      - [seq+0x488] >= 0  => cannot cast next
      - [seq+0x490] > 0 and unit codes 0x69/6b/6c/70/71 => cannot cast next
    0x754BA0->0x582BB0 sets +488=-1 but leaves +490.

    Live 20260722: aggressive CRT 582C00 + zero unit array made skill icon RED
    and unusable after one suppress. Default mode=safe:
      - ONLY write when cast+0xCC still points at seq (live link)
      - WPM only +488=-1 and +490=0
      - NO remote_call into game (thread race)
      - NO unit-array wipe, NO 582C00 all-types, NO +494 poke
    mode=legacy keeps old aggressive path for explicit lab only.

    Never 0x21. Requires unsafe skill write gates.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "cast": 0,
        "seq": 0,
        "live_cc": 0,
        "mode": str(mode or "safe"),
        "before": {},
        "after": {},
        "steps": [],
        "error": "",
        "note": "",
        "skipped": "",
    }
    blocked = _unsafe_skill_write_block_reason(session)
    if blocked:
        out["error"] = blocked
        return out
    try:
        from app.core.remote_runtime import remote_read_bytes, remote_write_bytes

        pid = int(getattr(session, "pid", 0) or 0)
        cast = int(cast_this or 0) & 0xFFFFFFFF
        seq = int(seq_ptr or 0) & 0xFFFFFFFF
        if not cast:
            _w, cast, _h = snapshot_cast(session, log=log)
            cast = int(cast or 0) & 0xFFFFFFFF
        live_cc = 0
        if cast and pid:
            raw_cc = remote_read_bytes(pid, (cast + CAST_SKILL_OBJ_OFF) & 0xFFFFFFFF, 4)
            live_cc = (
                int.from_bytes(raw_cc[:4], "little") if raw_cc and len(raw_cc) >= 4 else 0
            )
        out["live_cc"] = live_cc
        # Prefer live linked sequence; never write orphan saved ptr after unlink.
        if live_cc and live_cc > 0x10000:
            if seq and seq != live_cc:
                out["steps"].append(
                    {"name": "prefer_live_cc", "saved": hex(seq), "live": hex(live_cc)}
                )
            seq = live_cc
        out["cast"] = cast
        out["seq"] = seq
        out["before"] = snapshot_skill_sequence(
            session, cast_this=cast, seq_ptr=seq, log=log
        )
        if not seq or seq < 0x10000:
            out["ok"] = True
            out["note"] = "seq already null"
            out["after"] = out["before"]
            out["skipped"] = "null"
            return out
        if not pid:
            out["error"] = "pid null"
            return out

        if not live_cc or live_cc != seq:
            out["ok"] = True
            out["skipped"] = "not_live_cc"
            out["note"] = (
                f"seq_unlock SKIP not-live saved=0x{seq:X} live_cc=0x{live_cc:X} "
                f"(avoid orphan write; skill-red fix 20260722)"
            )
            out["after"] = out["before"]
            log(f"lab_force_can_cast_next_unit {out['note']}")
            return out

        def _step(name: str, **kw) -> None:
            row = {"name": name, **kw}
            out["steps"].append(row)
            log(f"seq_unlock step {name} {kw}")

        wrote: list[str] = []
        m = str(mode or "safe").strip().lower()
        if m == "legacy":
            from app.core.remote_runtime import remote_call_thiscall_x86

            try:
                ret = remote_call_thiscall_x86(
                    pid,
                    int(VA_SEQ_RESET) & 0xFFFFFFFF,
                    seq,
                    [],
                    caller_cleanup=False,
                    timeout_ms=1500,
                )
                _step("582BB0", ret=ret)
            except Exception as e:
                _step("582BB0", error=str(e))
            for t in (0, 1, 2, 3):
                try:
                    ret = remote_call_thiscall_x86(
                        pid,
                        int(VA_SEQ_CLEAR_TYPE) & 0xFFFFFFFF,
                        seq,
                        [int(t)],
                        caller_cleanup=False,
                        timeout_ms=1500,
                    )
                    _step(f"582C00_t{t}", ret=ret)
                except Exception as e:
                    _step(f"582C00_t{t}", error=str(e))

        # Minimal fields CanCastNextUnit keys on.
        if remote_write_bytes(
            pid, (seq + SEQ_OFF_GATE_488) & 0xFFFFFFFF, bytes([0xFF, 0xFF, 0xFF, 0xFF])
        ):
            wrote.append("+488=-1")
        if remote_write_bytes(pid, (seq + SEQ_OFF_ACTIVE_490) & 0xFFFFFFFF, bytes([0])):
            wrote.append("+490=0")
        _step("wpm_safe", wrote=wrote)

        after = snapshot_skill_sequence(
            session, cast_this=cast, seq_ptr=seq, log=log
        )
        out["after"] = after
        hint = after.get("can_cast_next_hint")
        out["ok"] = bool(after.get("ok")) and (
            hint is True or int(after.get("active_490") or 0) == 0
        )
        out["note"] = (
            f"seq_unlock[{m}] {skill_sequence_summary(out['before'])} -> "
            f"{skill_sequence_summary(after)} wrote={wrote}"
        )
        log(f"lab_force_can_cast_next_unit {out['note']}")
        return out
    except Exception as e:
        out["error"] = str(e)
        return out



def lab_clear_host_1b8_bit1(
    session,
    *,
    host_ptr: int = 0,
    log: LogFn | None = None,
) -> dict:
    """
    Lab-only: clear host+0x1B8 bit1 (0x75F000 fail 0x0F gate).

    Mirrors native clear at 0x6EADF0 (and dword, ~2). Does not call 0x21.
    Requires unsafe skill write gates (dev + env + profile).

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "host": 0,
        "before": 0,
        "after": 0,
        "cleared": False,
        "error": "",
        "note": "",
    }
    blocked = _unsafe_skill_write_block_reason(session)
    if blocked:
        out["error"] = blocked
        return out
    try:
        from app.core.remote_runtime import remote_read_bytes, remote_write_bytes

        host = int(host_ptr or 0) & 0xFFFFFFFF
        if not host:
            _w, _c, host = snapshot_cast(session, log=log)
            host = int(host or 0) & 0xFFFFFFFF
        if not host:
            out["error"] = "host null"
            return out
        raw = remote_read_bytes(int(session.pid), host + HOST_OFF_FLAGS_1B8, 4)
        if not raw or len(raw) < 4:
            out["error"] = "read 1b8 failed"
            return out
        before = int.from_bytes(raw[:4], "little")
        after = before & ~0x2
        out["host"] = host
        out["before"] = before
        if after == before:
            out["ok"] = True
            out["after"] = after
            out["cleared"] = False
            out["note"] = f"1b8 bit1 already clear (0x{before:X})"
            log(f"lab_clear_host_1b8_bit1 {out['note']}")
            return out
        n = remote_write_bytes(
            int(session.pid),
            host + HOST_OFF_FLAGS_1B8,
            int(after).to_bytes(4, "little"),
        )
        raw2 = remote_read_bytes(int(session.pid), host + HOST_OFF_FLAGS_1B8, 4)
        got = int.from_bytes(raw2[:4], "little") if raw2 and len(raw2) >= 4 else -1
        out["after"] = got
        out["ok"] = bool(n) and (got == after)
        out["cleared"] = bool(out["ok"]) and ((before & 2) != 0) and ((got & 2) == 0)
        out["note"] = f"1b8 0x{before:X}->0x{got:X} wrote={n}"
        log(f"lab_clear_host_1b8_bit1 {out['note']}")
        return out
    except Exception as e:
        out["error"] = str(e)
        return out



def lab_clear_host_4a4_flags(
    session,
    *,
    host_ptr: int = 0,
    log: LogFn | None = None,
) -> dict:
    """
    Lab-only: zero host+0x4A4..+0x4A7.

    Live 090524: after HostStop these become 0x01010101.
    0x587B00 returns locked if 4a4 or 4a6 nonzero (or 1b8 bit7).
    0x75F000 can return 0x10 when host+0x4A4 != 0 (with skill path).

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "host": 0,
        "before": 0,
        "after": 0,
        "cleared": False,
        "error": "",
        "note": "",
    }
    blocked = _unsafe_skill_write_block_reason(session)
    if blocked:
        out["error"] = blocked
        return out
    try:
        from app.core.remote_runtime import remote_read_bytes, remote_write_bytes

        host = int(host_ptr or 0) & 0xFFFFFFFF
        if not host:
            _w, _c, host = snapshot_cast(session, log=log)
            host = int(host or 0) & 0xFFFFFFFF
        if not host:
            out["error"] = "host null"
            return out
        addr = host + HOST_OFF_BYTE_4A4
        raw = remote_read_bytes(int(session.pid), addr, 4)
        if not raw or len(raw) < 4:
            out["error"] = "read 4a4 failed"
            return out
        before = int.from_bytes(raw[:4], "little")
        out["host"] = host
        out["before"] = before
        if before == 0:
            out["ok"] = True
            out["after"] = 0
            out["note"] = "4a4..4a7 already 0"
            log(f"lab_clear_host_4a4_flags {out['note']}")
            return out
        n = remote_write_bytes(int(session.pid), addr, bytes(4))
        raw2 = remote_read_bytes(int(session.pid), addr, 4)
        got = int.from_bytes(raw2[:4], "little") if raw2 and len(raw2) >= 4 else -1
        out["after"] = got
        out["ok"] = bool(n) and got == 0
        out["cleared"] = bool(out["ok"]) and before != 0
        out["note"] = f"4a4..7 0x{before:08X}->0x{got:08X} wrote={n}"
        log(f"lab_clear_host_4a4_flags {out['note']}")
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def lab_hoststop_bit1(
    session,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> dict:
    """
    HostStop local teardown then clear next-skill host gates:
    - host+0x1B8 bit1 (0x75F000 code 0x0F)
    - host+0x4A4..4A7 (0x587B00 / 0x75F000 code 0x10; live 090524 set to 0x01010101)

    @author by ak
    """
    log = log or (lambda _m: None)
    r = lab_hoststop_local_teardown(session, hwnd=hwnd, log=log, hold_s=0.12)
    host = int((r.get("host") or 0) or 0) & 0xFFFFFFFF
    g0 = snapshot_host_next_skill_gates(session, host_ptr=host, log=log)
    host = host or int(g0.get("host") or 0) & 0xFFFFFFFF
    c1 = lab_clear_host_1b8_bit1(session, host_ptr=host, log=log)
    c4 = lab_clear_host_4a4_flags(session, host_ptr=host, log=log)
    # cast timer/lock beyond 0x4B0 (8d4/8d8-8dc/4a4 float) — 092626 residual
    cast_ptr = 0
    try:
        _w, cast_ptr, _h2 = snapshot_cast(session, log=log)
        cast_ptr = int(cast_ptr or 0) & 0xFFFFFFFF
    except Exception:
        cast_ptr = 0
    ct0 = snapshot_cast_timer_block(session, cast_this=cast_ptr, log=log)
    ct = lab_clear_cast_timer_block(session, cast_this=cast_ptr, log=log)
    ct1 = snapshot_cast_timer_block(session, cast_this=cast_ptr, log=log)
    # SkillSequence CanCastNextUnit (cast+0xCC) — true next-skill lock
    seq0 = snapshot_skill_sequence(session, cast_this=cast_ptr, log=log)
    # Prefer seq captured inside hoststop_local (before +CC zeroed)
    seq_saved = int((r.get("seq_ptr_saved") or 0) or 0) & 0xFFFFFFFF
    if not seq_saved:
        seq_saved = int(seq0.get("seq") or 0) & 0xFFFFFFFF
    seq_u = lab_force_can_cast_next_unit(
        session,
        cast_this=cast_ptr,
        seq_ptr=seq_saved,
        log=log,
    )
    seq1 = snapshot_skill_sequence(
        session,
        cast_this=cast_ptr,
        seq_ptr=seq_saved if seq_saved else 0,
        log=log,
    )
    g1 = snapshot_host_next_skill_gates(session, host_ptr=host, log=log)
    out = dict(r)
    out["channel"] = "hoststop_bit1"
    out["method"] = METHOD_HOSTSTOP_BIT1
    out["host_gates_before_bit1"] = g0
    out["host_gates_after_bit1"] = g1
    out["bit1_clear"] = c1
    out["flags_4a4_clear"] = c4
    out["cast_timer_before"] = ct0
    out["cast_timer_clear"] = ct
    out["cast_timer_after"] = ct1
    out["seq_before"] = seq0
    out["seq_unlock"] = seq_u
    out["seq_after"] = seq1
    note = str(r.get("note") or "")
    out["note"] = (
        f"ORDER=hoststop_local->host4a4->cast_timer->seq_unlock; hoststop_ok={r.get('ok')}; "
        f"bit1={c1.get('note')}; f4a4={c4.get('note')}; timer={ct.get('note')}; "
        f"seq={seq_u.get('note')}; "
        f"gates_before={host_gates_summary(g0)}; gates_after={host_gates_summary(g1)}; "
        f"timer_before={cast_timer_summary(ct0)}; timer_after={cast_timer_summary(ct1)}; "
        f"seq_before={skill_sequence_summary(seq0)}; seq_after={skill_sequence_summary(seq1)}; "
        f"base={note}"
    )
    if r.get("ok") and c1.get("ok") and c4.get("ok") and ct.get("ok") and seq_u.get("ok"):
        out["ok"] = True
        out["error"] = ""
    elif not r.get("ok"):
        out["ok"] = False
        out["error"] = str(r.get("error") or "")
    else:
        out["ok"] = False
        out["error"] = str(
            c1.get("error")
            or c4.get("error")
            or ct.get("error")
            or seq_u.get("error")
            or "gate/timer/seq clear failed"
        )
    try:
        out["post_diag"] = format_post_fire_diag(
            session, cast_this=cast_ptr, fire=out, log=log
        )
    except Exception as e:
        out["post_diag"] = {"error": str(e)}
        log(f"hoststop_bit1 POST_DIAG err: {e}")
    log(f"hoststop_bit1 RESULT ok={out.get('ok')} note={out.get('note')!r}")
    return out



def format_post_fire_diag(
    session,
    *,
    cast_this: int = 0,
    fire: dict | None = None,
    log: LogFn | None = None,
) -> dict:
    """
    Untruncated post-fire snapshot for "FULL ok still can't recast" analysis.

    Always reads live cast+0xCC, seq +488/+490/hint, cast watch, perform.
    Prefer this over note[:140] truncation in suppress logs.
    """
    log = log or (lambda _m: None)
    quiet = lambda _m: None
    cast = int(cast_this or 0) & 0xFFFFFFFF
    try:
        w, cast_live, host = snapshot_cast(session, log=quiet)
        cast = cast or int(cast_live or 0) & 0xFFFFFFFF
    except Exception:
        w, host = {}, 0
    try:
        perf = snapshot_host_perform(session, log=quiet)
    except Exception:
        perf = {}
    live_cc = 0
    try:
        from app.core.remote_runtime import remote_read_bytes

        pid = int(getattr(session, "pid", 0) or 0)
        if cast and pid:
            raw = remote_read_bytes(pid, (cast + CAST_SKILL_OBJ_OFF) & 0xFFFFFFFF, 4)
            live_cc = int.from_bytes(raw[:4], "little") if raw and len(raw) >= 4 else 0
    except Exception:
        live_cc = int((w or {}).get("+CC") or 0) & 0xFFFFFFFF
    seq_ptr = live_cc
    fire = fire or {}
    for key in ("seq_ptr_saved", "seq_unlock_early", "seq_unlock"):
        blob = fire.get(key)
        if key == "seq_ptr_saved" and blob:
            seq_ptr = seq_ptr or (int(blob or 0) & 0xFFFFFFFF)
        if isinstance(blob, dict):
            sp = int(blob.get("seq") or 0) & 0xFFFFFFFF
            if sp and not seq_ptr:
                seq_ptr = sp
    try:
        seq = snapshot_skill_sequence(
            session, cast_this=cast, seq_ptr=seq_ptr or live_cc, log=quiet
        )
    except Exception as e:
        seq = {"ok": False, "error": str(e)}
    early = fire.get("seq_unlock_early") if isinstance(fire.get("seq_unlock_early"), dict) else {}
    late = fire.get("seq_unlock") if isinstance(fire.get("seq_unlock"), dict) else {}
    diag = {
        "cast": cast,
        "host": int(host or 0) & 0xFFFFFFFF,
        "live_cc": int(live_cc or 0) & 0xFFFFFFFF,
        "watch": {
            "+10": int((w or {}).get("+10") or 0),
            "+14": int((w or {}).get("+14") or 0),
            "+18": int((w or {}).get("+18") or 0),
            "+20": int((w or {}).get("+20") or 0),
            "+4A0": int((w or {}).get("+4A0") or 0),
            "+7C": int((w or {}).get("+7C") or 0),
            "+CC": int(live_cc or 0),
        },
        "perform_type": perf.get("perform_type"),
        "perform_active": perf.get("active"),
        "perform_base": perf.get("is_base"),
        "seq": seq,
        "seq_early_note": str((early or {}).get("note") or ""),
        "seq_early_skipped": str((early or {}).get("skipped") or ""),
        "seq_late_note": str((late or {}).get("note") or ""),
        "seq_late_skipped": str((late or {}).get("skipped") or ""),
        "can_cast_next_hint": seq.get("can_cast_next_hint"),
        "gate_488": seq.get("gate_488"),
        "active_490": seq.get("active_490"),
        "fire_ok": fire.get("ok"),
        "fire_channel": fire.get("channel") or fire.get("method"),
    }
    line = (
        f"POST_DIAG cast=0x{cast:X} live_cc=0x{int(live_cc or 0):X} "
        f"id=0x{diag['watch']['+10']:X} +18=0x{diag['watch']['+18']:X} "
        f"busy={diag['watch']['+4A0'] & 1} +20=0x{diag['watch']['+20']:X} "
        f"ptype={diag['perform_type']} base={diag['perform_base']} "
        f"seq={skill_sequence_summary(seq)} "
        f"early_skip={(early or {}).get('skipped')!r} "
        f"late_skip={(late or {}).get('skipped')!r} "
        f"early={(early or {}).get('note') or '-'} | "
        f"late={(late or {}).get('note') or '-'}"
    )
    diag["line"] = line
    log(line)
    return diag


def lab_soft_next_unlock(
    session,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
    clear_busy: bool = True,
    strip_identity: bool = True,
    unlink_seq: bool = False,
) -> dict:
    """
    Soft next-skill unlock v4.2 (lab).

    Live soft v4.1 (20260722 01:53) — CRITICAL:
      - unit0 WPM clear u0=0x34D/code=0x69 @ unit_ptr left skills unusable
        even AFTER suppress OFF (shared unit / skill-table corruption).
      - 754BA0 ret=-1 unlinked +CC; second EDGE still happened once.
      - User: 关压制后仍放不出技能.

    Hard ban forever:
      - NEVER write unit array / unit0 / unit codes (skill-red / permanent lock)

    v4.2 order (still NO OnSkill / NO 0x21 / NO unit writes):
      1) stop65
      2) safe Seq +488/+490 only while live
      3) WPM perform_strip identity (optional, default on)
      4) 754BA0 unlink OFF by default (was ret=-1; opt-in only)
      5) cheap host gates

    @author by ak
    """
    log = log or (lambda _m: None)
    quiet = lambda _m: None
    out: dict = {
        "ok": False,
        "channel": "soft_next",
        "method": METHOD_SOFT_NEXT,
        "note": "",
        "error": "",
        "steps": [],
        "native": {},
        "perform_before": {},
        "perform_after": {},
        "seq_unlock_early": {},
        "seq_unlock": {},
        "busy_clear": {},
        "identity_strip": {},
        "unlink_cc": {},
        "unit0_clear": {"ok": True, "skipped": True, "note": "hard-ban unit writes (v4.2)"},
        "post_diag": {},
        "raw": {},
        "variant": "v4.2_no_unit",
    }

    def _step(name: str, **kw) -> None:
        row = {"name": name, **kw}
        out["steps"].append(row)
        log(f"soft_next step {name} {kw}")

    blocked = _unsafe_skill_write_block_reason(session)
    if blocked:
        out["error"] = blocked
        return out

    r1 = lab_stop_skill_perform(session, hwnd=hwnd, log=log)
    out["perform_before"] = r1.get("before") or {}
    out["raw"]["stop65"] = {
        "ok": r1.get("ok"),
        "method": r1.get("method"),
        "note": r1.get("note"),
        "error": r1.get("error"),
    }
    _step("stop65", ok=bool(r1.get("ok")), method=r1.get("method"))

    w, cast, host = snapshot_cast(session, log=quiet)
    cast = int(cast or 0) & 0xFFFFFFFF
    host = int(host or 0) & 0xFFFFFFFF
    if not cast:
        try:
            cur = resolve_cast_this(session, log=quiet, with_dumps=False)
            cast = int(cur.cast_this or 0) & 0xFFFFFFFF
            w = extract_cast_watch(getattr(cur, "cast_dump", None) or b"") or w
        except Exception:
            pass
    out["cast"] = cast
    out["host"] = host
    if not cast:
        out["error"] = "cast_this null"
        out["perform_after"] = snapshot_host_perform(session, log=quiet)
        return out

    seq_saved = 0
    pid = int(getattr(session, "pid", 0) or 0)
    try:
        from app.core.remote_runtime import remote_read_bytes

        raw_cc = remote_read_bytes(pid, (cast + CAST_SKILL_OBJ_OFF) & 0xFFFFFFFF, 4) if pid else b""
        seq_saved = int.from_bytes(raw_cc[:4], "little") if raw_cc and len(raw_cc) >= 4 else 0
    except Exception:
        seq_saved = int((w or {}).get("+CC") or 0) & 0xFFFFFFFF
    out["seq_ptr_saved"] = seq_saved
    out["cc_before"] = seq_saved
    out["seq_before"] = snapshot_skill_sequence(
        session, cast_this=cast, seq_ptr=seq_saved, log=quiet
    )
    _step(
        "pre_unlock",
        cast=hex(cast),
        id=hex(int((w or {}).get("+10") or 0)),
        busy=hex(int((w or {}).get("+4A0") or 0)),
        seq=hex(seq_saved),
        ptype=(out["perform_before"] or {}).get("perform_type"),
        strip=bool(strip_identity),
        unlink=bool(unlink_seq),
        unit0="banned",
    )

    seq_u = lab_force_can_cast_next_unit(
        session, cast_this=cast, seq_ptr=seq_saved, log=log, mode="safe"
    )
    out["seq_unlock_early"] = seq_u
    out["seq_unlock"] = seq_u
    _step(
        "seq_unlock",
        ok=bool(seq_u.get("ok")),
        skipped=seq_u.get("skipped"),
        note=seq_u.get("note"),
    )
    _step("unit0", note="hard-ban (never write unit array)", skipped=True)

    strip_r: dict = {"ok": True, "note": "skipped", "skipped": True}
    if strip_identity:
        strip_r = clear_cast_session(
            session,
            log=quiet,
            require_active=False,
            variant=CAST_CANCEL_PERFORM_STRIP,
        )
        strip_r["skipped"] = False
    elif clear_busy and (int((w or {}).get("+4A0") or 0) & 1):
        strip_r = clear_cast_session(
            session,
            log=quiet,
            require_active=False,
            variant=CAST_CANCEL_BUSY_BIT,
        )
        strip_r["skipped"] = False
    out["identity_strip"] = strip_r
    out["busy_clear"] = strip_r
    _step(
        "identity_strip",
        ok=bool(strip_r.get("ok")),
        wrote=strip_r.get("wrote"),
        variant=strip_r.get("variant"),
    )

    # 754BA0 default OFF (v4.1 ret=-1 + permanent lock reports). Opt-in only.
    unlink_r: dict = {"ok": True, "note": "skipped(default off v4.2)", "skipped": True, "ret": None}
    if unlink_seq and pid and cast:
        try:
            from app.core.remote_runtime import remote_call_thiscall_x86, remote_read_bytes

            raw_cc = remote_read_bytes(pid, (cast + CAST_SKILL_OBJ_OFF) & 0xFFFFFFFF, 4)
            cc = int.from_bytes(raw_cc[:4], "little") if raw_cc and len(raw_cc) >= 4 else 0
            if cc and cc > 0x10000:
                ret = remote_call_thiscall_x86(
                    pid,
                    int(VA_CLEAR_SKILL_OBJ) & 0xFFFFFFFF,
                    int(cast) & 0xFFFFFFFF,
                    [],
                    caller_cleanup=False,
                    timeout_ms=1500,
                )
                unlink_r = {
                    "ok": True,
                    "skipped": False,
                    "ret": ret,
                    "cc_before": cc,
                    "note": f"754BA0 ret={ret} cc_was=0x{cc:X}",
                }
            else:
                unlink_r = {
                    "ok": True,
                    "skipped": True,
                    "note": f"cc already 0 (0x{cc:X})",
                    "cc_before": cc,
                }
        except Exception as e:
            unlink_r = {"ok": False, "error": str(e), "note": str(e), "skipped": False}
    out["unlink_cc"] = unlink_r
    _step("unlink_754BA0", **{k: unlink_r.get(k) for k in ("ok", "note", "ret", "skipped")})

    try:
        c1 = lab_clear_host_1b8_bit1(session, host_ptr=host, log=quiet)
        c4 = lab_clear_host_4a4_flags(session, host_ptr=host, log=quiet)
        ct = lab_clear_cast_timer_block(session, cast_this=cast, log=quiet)
        out["raw"]["bit1"] = c1
        out["raw"]["f4a4"] = c4
        out["raw"]["timer"] = ct
        _step("gates", bit1=c1.get("note"), f4a4=c4.get("note"), timer=ct.get("note"))
    except Exception as e:
        _step("gates", error=str(e))

    seq_u2 = lab_force_can_cast_next_unit(
        session, cast_this=cast, seq_ptr=seq_saved, log=quiet, mode="safe"
    )
    out["seq_unlock"] = seq_u2
    out["seq_after"] = snapshot_skill_sequence(
        session, cast_this=cast, seq_ptr=seq_saved, log=quiet
    )
    try:
        from app.core.remote_runtime import remote_read_bytes as _rr

        raw_cc2 = _rr(pid, (cast + CAST_SKILL_OBJ_OFF) & 0xFFFFFFFF, 4) if pid else b""
        out["cc_after"] = (
            int.from_bytes(raw_cc2[:4], "little") if raw_cc2 and len(raw_cc2) >= 4 else 0
        )
    except Exception:
        out["cc_after"] = 0

    perf_f = snapshot_host_perform(session, log=quiet)
    out["perform_after"] = perf_f
    w_f, _, _ = snapshot_cast(session, log=quiet)
    out["raw"]["final_watch"] = w_f
    out["post_diag"] = format_post_fire_diag(session, cast_this=cast, fire=out, log=log)
    out["ok"] = bool(r1.get("ok") or seq_u.get("ok") or strip_r.get("ok"))
    out["error"] = ""
    out["note"] = (
        f"ORDER=soft_v4.2:stop65->seq_safe->strip(no unit/no 754BA0 default)->gates; "
        f"stop65_ok={r1.get('ok')}; seq_skip={seq_u.get('skipped')}; "
        f"strip={strip_r.get('variant')}:{strip_r.get('wrote')}; "
        f"unlink={unlink_r.get('note')}; "
        f"cc=0x{(out.get('cc_before') or 0):X}->0x{(out.get('cc_after') or 0):X}; "
        f"final_id=0x{int((w_f or {}).get('+10') or 0):X} "
        f"4a0=0x{int((w_f or {}).get('+4A0') or 0):X} "
        f"ptype={perf_f.get('perform_type')} "
        f"hint={out['post_diag'].get('can_cast_next_hint')}"
    )
    log(f"soft_next RESULT ok={out['ok']} note={out['note']!r}")
    return out


def lab_suppress_stop_heal(
    session,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> dict:
    """
    Best-effort local heal when continuous suppress turns OFF.

    Cannot repair skill-table unit corruption from old unit0 writes —
    that needs relog/client restart. Only clears cast busy and restores
    base locomotion if readable.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {"ok": False, "note": "", "steps": [], "error": ""}
    try:
        r_busy = clear_cast_session(
            session,
            log=lambda _m: None,
            require_active=False,
            variant=CAST_CANCEL_BUSY_BIT,
        )
        out["steps"].append({"busy": r_busy.get("wrote") or r_busy.get("note")})
        r65 = lab_stop_skill_perform(session, hwnd=hwnd, log=lambda _m: None)
        out["steps"].append(
            {"stop65": r65.get("method"), "ok": r65.get("ok"), "ptype": (r65.get("after") or {}).get("perform_type")}
        )
        try:
            from app.core.yaolu_auto import ensure_base_locomotion_perform

            rb = ensure_base_locomotion_perform(session, log=lambda _m: None)
            out["steps"].append({"restore_base": bool(rb.get("ok") if isinstance(rb, dict) else rb)})
        except Exception as e:
            out["steps"].append({"restore_base_err": str(e)})
        out["ok"] = True
        out["note"] = (
            "heal: busy clear + stop65 + restore_base attempted; "
            "if skill still dead after unit0 damage → 重登角色/重开客户端"
        )
        log(f"suppress_stop_heal {out['note']} steps={out['steps']}")
    except Exception as e:
        out["error"] = str(e)
        log(f"suppress_stop_heal err={e}")
    return out




def lab_key_interrupt(
    session,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
    vk: int | None = None,
    vks: tuple[int, ...] | list[int] | None = None,
    hold_ms: int = 80,
    gap_ms: int = 50,
    also_stop65: bool = False,
    path: str = "both",
) -> dict:
    """
    Lab: interrupt via real game keys — user macro uses BOTH Space and X.

    User (live):
      - X = 清后摇 (block / interrupt skill)
      - Space = 清格挡
      - Macro uses both; single X alone (v4.3/4.4) may not match their bind combo.

    Default sequence: X (0x58) then Space (0x20).
    Each key: KEY_HOLD pulse (SoftSend+InjectKey) then UI_KEY with FG.

    @author by ak
    """
    log = log or (lambda _m: None)
    quiet = lambda _m: None
    if vks is None:
        if vk is not None:
            seq = (int(vk) & 0xFF,)
        else:
            seq = tuple(int(x) & 0xFF for x in KEY_INTERRUPT_VK_SEQ_DEFAULT)
    else:
        seq = tuple(int(x) & 0xFF for x in vks if int(x) & 0xFF)
    if not seq:
        seq = tuple(int(x) & 0xFF for x in KEY_INTERRUPT_VK_SEQ_DEFAULT)

    out: dict = {
        "ok": False,
        "channel": "key_interrupt",
        "method": METHOD_KEY_INTERRUPT,
        "note": "",
        "error": "",
        "vk": seq[0],
        "vks": list(seq),
        "path": str(path or "both"),
        "steps": [],
        "native": {},
        "perform_before": {},
        "perform_after": {},
        "post_diag": {},
    }
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        out["error"] = "pid null"
        return out

    def _step(name: str, **kw) -> None:
        row = {"name": name, **kw}
        out["steps"].append(row)
        log(f"key_interrupt step {name} {kw}")

    try:
        out["perform_before"] = snapshot_host_perform(session, log=quiet)
        w0, cast0, _h0 = snapshot_cast(session, log=quiet)
        out["cast"] = int(cast0 or 0) & 0xFFFFFFFF
        out["watch_before"] = {
            "+10": int((w0 or {}).get("+10") or 0),
            "+4A0": int((w0 or {}).get("+4A0") or 0),
            "+18": int((w0 or {}).get("+18") or 0),
        }
        _step(
            "pre",
            id=hex(out["watch_before"]["+10"]),
            busy=out["watch_before"]["+4A0"] & 1,
            ptype=(out["perform_before"] or {}).get("perform_type"),
            vks=[hex(v) for v in seq],
            path=str(path or "both"),
        )

        if also_stop65:
            r65 = lab_stop_skill_perform(session, hwnd=hwnd, log=log)
            out["steps"].append({"stop65": r65.get("ok"), "method": r65.get("method")})

        from app.core.xajh_bridge import ensure_bridge

        br = ensure_bridge(
            pid,
            log=log,
            inject_if_needed=True,
            hwnd=int(hwnd or 0) or None,
            force_reinject=False,
        )
        if br is None:
            out["error"] = "bridge unavailable"
            return out

        path_s = str(path or "both").strip().lower()
        do_hold = path_s in ("both", "hold", "hold_pulse", "key_hold")
        do_ui = path_s in ("both", "ui", "ui_key")
        any_ok = False
        notes: list[str] = []

        try:
            # install hooks once for first vk (hooks are global)
            if do_hold:
                try:
                    r_inst = br.key_hold(
                        int(seq[0]) & 0xFF,
                        install_only=True,
                        allow_softsend=True,
                        hwnd=int(hwnd or 0) or None,
                        timeout_ms=2500,
                    )
                    _step(
                        "hold_install",
                        ok=bool(r_inst.ok),
                        note=str(r_inst.note or r_inst.error or ""),
                    )
                except Exception as e:
                    _step("hold_install", error=str(e))

            for i, one_vk in enumerate(seq):
                one_vk = int(one_vk) & 0xFF
                tag = f"vk{i}_0x{one_vk:02X}"
                hold_ok = False
                ui_ok = False

                if do_hold:
                    r_dn = br.key_hold(
                        one_vk,
                        down=True,
                        allow_softsend=True,
                        hwnd=int(hwnd or 0) or None,
                        timeout_ms=2500,
                    )
                    _step(f"{tag}_hold_dn", ok=bool(r_dn.ok), note=str(r_dn.note or r_dn.error or ""))
                    time.sleep(max(0.04, float(hold_ms) / 1000.0))
                    r_up = br.key_hold(
                        one_vk,
                        down=False,
                        allow_softsend=True,
                        hwnd=int(hwnd or 0) or None,
                        timeout_ms=2500,
                    )
                    _step(f"{tag}_hold_up", ok=bool(r_up.ok), note=str(r_up.note or r_up.error or ""))
                    hold_ok = bool(r_dn.ok or r_up.ok)
                    notes.append(
                        f"{tag} hold={'ok' if hold_ok else 'fail'}"
                    )
                    out["native"][f"{tag}_hold"] = (
                        f"dn={r_dn.note or r_dn.error}; up={r_up.note or r_up.error}"
                    )

                if do_ui:
                    res = br.ui_key(
                        one_vk,
                        action=2,
                        hold_ms=max(40, int(hold_ms)),
                        hwnd=int(hwnd or 0) or None,
                        timeout_ms=2500,
                        no_focus=False,
                    )
                    ui_ok = bool(res.ok)
                    _step(
                        f"{tag}_ui_fg",
                        ok=ui_ok,
                        note=str(res.note or res.error or ""),
                        no_focus=False,
                    )
                    notes.append(f"{tag} ui_fg={'ok' if ui_ok else 'fail'}")
                    out["native"][f"{tag}_ui"] = str(res.note or res.error or "")

                if hold_ok or ui_ok:
                    any_ok = True

                # gap between X and Space
                if i + 1 < len(seq):
                    time.sleep(max(0.02, float(gap_ms) / 1000.0))
        finally:
            try:
                br.close()
            except Exception:
                pass

        out["ok"] = bool(any_ok)
        if not out["ok"]:
            out["error"] = "; ".join(notes) or "key paths failed"

        time.sleep(0.06)
        out["perform_after"] = snapshot_host_perform(session, log=quiet)
        w1, cast1, _h1 = snapshot_cast(session, log=quiet)
        out["watch_after"] = {
            "+10": int((w1 or {}).get("+10") or 0),
            "+4A0": int((w1 or {}).get("+4A0") or 0),
            "+18": int((w1 or {}).get("+18") or 0),
        }
        out["post_diag"] = format_post_fire_diag(
            session, cast_this=int(cast1 or cast0 or 0), fire=out, log=log
        )
        out["note"] = (
            f"ORDER=key_v4.5 seq={[hex(v) for v in seq]} path={path_s} "
            f"hold={hold_ms}ms gap={gap_ms}ms (X then Space; HOLD+FG); "
            f"ok={out['ok']}; {'; '.join(notes)}; "
            f"id=0x{out['watch_before']['+10']:X}->0x{out['watch_after']['+10']:X} "
            f"busy={(out['watch_before']['+4A0']&1)}->={(out['watch_after']['+4A0']&1)} "
            f"ptype={(out['perform_before'] or {}).get('perform_type')}->"
            f"{(out['perform_after'] or {}).get('perform_type')}"
        )
        log(f"key_interrupt RESULT ok={out['ok']} note={out['note']!r}")
    except Exception as e:
        out["error"] = str(e)
        log(f"key_interrupt err={e}")
    return out



def wait_recovery_window(
    session,
    *,
    log: LogFn | None = None,
    rounds: int = 400,
    interval_s: float = 0.01,
    stop_fn: Callable[[], bool] | None = None,
    require_idle_first: bool = True,
    accept_already_armed: bool = True,
    idle_need: int = 2,
) -> dict:
    """
    Rising-edge armed window (generic):

    1) Optionally wait clean idle (id=0, busy=0, not type=4) so we do not fire
       on leftover ghost identity after a previous cast.
    2) FIRE on first armed edge: busy bit OR skill-era perform type!=2 OR
       id with elapsed (+20>=0x10). Prefer type=4 / busy over bare id.

    Live 095755: wait @50ms + fire with busy=0/+20=0/type=4 and host gates
    already 0 → user felt "no catch / no suppress". Rising-edge + 10ms poll
    catches the real arm sooner; idle-first avoids post-cast residue.
    """
    log = log or (lambda _m: None)
    interval_s = float(interval_s)
    if interval_s <= 0:
        interval_s = 0.01
    rounds = int(rounds)
    need_idle = max(1, int(idle_need))
    phase = "idle" if require_idle_first else "edge"
    idle_streak = 0
    t0 = time.perf_counter()
    log(
        f"recovery_lab: wait RISING-EDGE (idle→busy|type4|+20) up to "
        f"{rounds * interval_s:.1f}s poll={interval_s * 1000:.0f}ms — 【站着别放】先等空闲…"
    )
    for i in range(rounds):
        if stop_fn and stop_fn():
            return {
                "ok": False,
                "wait_rounds": i,
                "watch": {},
                "cast_this": 0,
                "host_ptr": 0,
                "phase": phase,
                "error": "stopped",
            }
        watch, cast_this, host = snapshot_cast(session)
        perf = snapshot_host_perform(session, log=lambda _m: None)
        meta = hit_meta(watch, perf)
        elapsed_s = time.perf_counter() - t0

        if phase == "idle":
            if is_edge_idle(watch, perf):
                idle_streak += 1
                if idle_streak >= need_idle:
                    phase = "edge"
                    log(
                        f"recovery_lab: IDLE baseline ok @poll[{i + 1}] "
                        f"t={elapsed_s:.2f}s — 【现在放技能】CAST NOW"
                    )
            else:
                idle_streak = 0
                # Legacy manual trials may accept a cast made before the
                # button. Causal traces disable this and require real idle.
                if (
                    accept_already_armed
                    and cast_this
                    and is_armed_cast(watch, perf)
                ):
                    phase = "edge"
                else:
                    if i == 0 or (i + 1) % 40 == 0:
                        log(
                            f"recovery_lab: wait-idle[{i + 1}/{rounds}] "
                            f"{watch_summary(watch) if watch else 'no watch'} "
                            f"type={meta.get('ptype')}"
                        )
                    time.sleep(interval_s)
                    continue

        if phase == "edge" and cast_this and is_armed_cast(watch, perf):
            # If type=4/id but busy=0 and +20==0, peek ~40ms for busy to rise
            # (true mid-cast). Recovery tails keep busy=0 — fire after peek.
            best_w, best_c, best_h, best_p, best_m = watch, cast_this, host, perf, meta
            if (not meta.get("busy")) and int(meta.get("p20") or 0) == 0:
                t_peek = time.perf_counter() + 0.04
                while time.perf_counter() < t_peek:
                    w2, c2, h2 = snapshot_cast(session)
                    p2 = snapshot_host_perform(session, log=lambda _m: None)
                    m2 = hit_meta(w2, p2)
                    if c2 and is_armed_cast(w2, p2):
                        best_w, best_c, best_h, best_p, best_m = w2, c2, h2, p2, m2
                        if m2.get("busy") or int(m2.get("p20") or 0) >= 0x10:
                            break
                    time.sleep(0.005)
            log(
                f"recovery_lab: HIT[{i + 1}] {watch_summary(best_w)} "
                f"type={best_m.get('ptype')} busy={best_m.get('busy')} "
                f"+20=0x{int(best_m.get('p20') or 0):X} remain_ms={best_m.get('remain_ms')} "
                f"t={elapsed_s:.3f}s cast=0x{int(best_c or 0):X} 【已抓住】"
            )
            return {
                "ok": True,
                "wait_rounds": i + 1,
                "watch": best_w,
                "cast_this": best_c,
                "host_ptr": best_h,
                "phase": "edge",
                "edge_elapsed_s": round(elapsed_s, 4),
                "hit_meta": best_m,
                "perform": {
                    "type": best_p.get("perform_type"),
                    "gate": [
                        best_p.get("session_gate"),
                        best_p.get("session_gate1"),
                        best_p.get("session_gate2"),
                    ],
                    "subtype": best_p.get("perform_subtype"),
                    "is_base": best_p.get("is_base"),
                    "is_matter": best_p.get("is_matter"),
                },
            }

        if i == 0 or (i + 1) % 40 == 0:
            log(
                f"recovery_lab: wait-{phase}[{i + 1}/{rounds}] "
                f"{watch_summary(watch) if watch else 'no watch'} "
                f"type={meta.get('ptype')}"
            )
        time.sleep(interval_s)
    return {
        "ok": False,
        "wait_rounds": rounds,
        "watch": {},
        "cast_this": 0,
        "host_ptr": 0,
        "phase": phase,
        "error": (
            "timeout: no rising-edge armed window "
            "(need idle then busy|type4|id+elapsed)"
        ),
    }


def wait_not_recovery_window(
    session,
    *,
    log: LogFn | None = None,
    rounds: int = 80,
    interval_s: float = 0.05,
    stop_fn: Callable[[], bool] | None = None,
) -> dict:
    """
    Wait until recovery window closes (busy bit off).

    Sticky skill id after refill is OK — next cast will re-arm busy.
    Prefer this over full idle between matrix trials.
    """
    log = log or (lambda _m: None)
    for i in range(int(rounds)):
        if stop_fn and stop_fn():
            return {"ok": False, "wait_rounds": i, "error": "stopped"}
        watch, cast_this, host = snapshot_cast(session)
        if not is_recovery_window(watch):
            if i > 0:
                log(f"recovery_lab: not-busy HIT[{i+1}] {watch_summary(watch)}")
            return {
                "ok": True,
                "wait_rounds": i + 1,
                "watch": watch,
                "cast_this": cast_this,
                "host_ptr": host,
            }
        if i == 0 or (i + 1) % 10 == 0:
            log(
                f"recovery_lab: wait not-busy[{i+1}/{rounds}] {watch_summary(watch)}"
            )
        time.sleep(float(interval_s))
    watch, cast_this, host = snapshot_cast(session)
    return {
        "ok": False,
        "wait_rounds": rounds,
        "watch": watch,
        "cast_this": cast_this,
        "host_ptr": host,
        "error": "timeout waiting not-busy",
    }


def lab_cast_interrupt_skill(
    session,
    *,
    skill_id: int = INTERRUPT_SKILL_ID_DEFAULT,
    hwnd: int = 0,
    log: LogFn | None = None,
    clear_block: bool = True,
) -> dict:
    """
    Lab-only generic recovery interrupt without VK.

    Hand_probe: X casts block skill config 0xE07 via normal cast path.
    Replay with bridge CMD_CAST_SKILL -> 0x53E110, then unlock next-skill
    gates that HostStop/stop65 often leave (host+4A4, cast timer, Seq).

    Live 20260727: ret=0 alone is NOT success — require mutation signature
    (cast+18==skill_id or native mut=1 / a18 change). Always clear host 4A4
    after fire because stop65 path re-arms lock587.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "channel": "cast_interrupt",
        "method": METHOD_CAST_INTERRUPT,
        "skill_id": int(skill_id) & 0xFFFFFFFF,
        "steps": [],
        "error": "",
        "note": "",
        "native": {},
        "perform_before": {},
        "perform_after": {},
        "perform_mid": {},
        "post_diag": {},
        "mid_watch": {},
        "post_watch": {},
        "gates": {},
    }
    blocked = _unsafe_skill_write_block_reason(session)
    if blocked:
        out["error"] = blocked
        return out

    sid = int(skill_id or 0) & 0xFFFFFFFF
    if not sid:
        out["error"] = "skill_id=0"
        return out

    try:
        from app.core.xajh_bridge import ensure_bridge
    except Exception as e:
        out["error"] = f"import bridge: {e}"
        return out

    def _step(name: str, **kw) -> None:
        row = {"name": name, **kw}
        out["steps"].append(row)
        log(f"cast_interrupt step {name} {kw}")

    def _watch_now() -> tuple[dict, int, int]:
        try:
            return snapshot_cast(session, log=log)
        except Exception as e:
            return {"error": str(e)}, 0, 0

    try:
        out["perform_before"] = snapshot_host_perform(session, log=log) or {}
    except Exception as e:
        out["perform_before"] = {"error": str(e)}

    w0, cast0, host0 = _watch_now()
    _step(
        "pre",
        cast=hex(int(cast0 or 0) & 0xFFFFFFFF),
        host=hex(int(host0 or 0) & 0xFFFFFFFF),
        id=hex(int((w0 or {}).get("+10") or 0)),
        cfg18=hex(int((w0 or {}).get("+18") or 0)),
        busy=int((w0 or {}).get("+4A0") or 0),
        ptype=(out["perform_before"] or {}).get("perform_type"),
        skill_id=hex(sid),
    )

    pid = int(getattr(session, "pid", 0) or 0)
    br = ensure_bridge(
        pid,
        log=log,
        inject_if_needed=True,
        hwnd=int(hwnd or 0) or None,
        force_reinject=False,
    )
    if br is None:
        out["error"] = "bridge unavailable"
        return out

    try:
        res = br.cast_skill(
            skill_id=sid,
            hwnd=int(hwnd or 0) or None,
            timeout_ms=3000,
        )
    finally:
        try:
            br.close()
        except Exception:
            pass

    note = str(res.note or res.error or "")
    _step(
        "cast_skill",
        ok=bool(res.ok),
        ret=getattr(res, "ret", None),
        note=note,
        skill_id=hex(sid),
    )
    out["native"] = {
        "ok": bool(res.ok),
        "ret": getattr(res, "ret", None),
        "note": note,
        "error": str(res.error or ""),
    }
    if not res.ok:
        out["error"] = str(res.error or note or "CAST_SKILL failed")
        out["note"] = note
        return out

    # Settle briefly, then prove interrupt signature BEFORE stop65.
    time.sleep(0.05)
    w_mid, cast_mid, host_mid = _watch_now()
    try:
        out["perform_mid"] = snapshot_host_perform(session, log=log) or {}
    except Exception as e:
        out["perform_mid"] = {"error": str(e)}
    out["mid_watch"] = w_mid or {}
    mid_cfg = int((w_mid or {}).get("+18") or 0) & 0xFFFFFFFF
    mid_id = int((w_mid or {}).get("+10") or 0) & 0xFFFFFFFF
    mid_ptype = int((out["perform_mid"] or {}).get("perform_type") or 0)
    mid_gate = (out["perform_mid"] or {}).get("gate") or [0, 0, 0]
    try:
        mid_gate2 = int(mid_gate[2] or 0) if isinstance(mid_gate, (list, tuple)) else 0
    except Exception:
        mid_gate2 = 0
    native_mut = ("mut=1" in note) or (f"a18=0x{sid:X}" in note.upper().replace("0X", "0x"))
    # Hand_probe X signature: +18 becomes interrupt skill id, or perform gate id.
    sig = bool(
        mid_cfg == sid
        or mid_gate2 == sid
        or native_mut
        or (mid_cfg != int((w0 or {}).get("+18") or 0) and mid_cfg not in (0, int((w0 or {}).get("+18") or 0)))
    )
    _step(
        "mid",
        cast=hex(int(cast_mid or 0) & 0xFFFFFFFF),
        id=hex(mid_id),
        cfg18=hex(mid_cfg),
        ptype=mid_ptype,
        gate2=hex(mid_gate2),
        sig=sig,
        native_mut=native_mut,
    )

    stop_note = ""
    if clear_block:
        # Only stop65 if we look skill-era; always useful to drop block pose
        # after a real interrupt, and still helps land after local animation.
        try:
            r_stop = lab_stop_skill_perform(session, hwnd=hwnd, log=log)
            stop_note = str(r_stop.get("note") or r_stop.get("method") or "")
            _step(
                "clear_block_stop65",
                ok=bool(r_stop.get("ok")),
                note=stop_note,
                error=str(r_stop.get("error") or ""),
            )
            out["perform_after"] = r_stop.get("after") or {}
        except Exception as e:
            _step("clear_block_stop65", ok=False, error=str(e))
            stop_note = f"stop65 err: {e}"

    # Critical: stop65/HostStop often leaves host+4A4=0x01010101 (lock587).
    # Clear next-skill gates + cast timer + Seq safe unlock.
    host_ptr = int(host_mid or host0 or 0)
    cast_ptr = int(cast_mid or cast0 or 0)
    try:
        g4 = lab_clear_host_4a4_flags(session, host_ptr=host_ptr, log=log)
        _step("host4a4", ok=bool(g4.get("ok")), note=str(g4.get("note") or g4.get("error") or ""))
        out["gates"]["host4a4"] = g4
    except Exception as e:
        _step("host4a4", ok=False, error=str(e))
    try:
        gt = lab_clear_cast_timer_block(session, cast_this=cast_ptr, log=log)
        _step("cast_timer", ok=bool(gt.get("ok")), note=str(gt.get("note") or gt.get("error") or ""))
        out["gates"]["cast_timer"] = gt
    except Exception as e:
        _step("cast_timer", ok=False, error=str(e))
    try:
        gs = lab_force_can_cast_next_unit(
            session, cast_this=cast_ptr, log=log, mode="safe"
        )
        _step(
            "seq_unlock",
            ok=bool(gs.get("ok")),
            note=str(gs.get("note") or gs.get("error") or ""),
            skipped=str(gs.get("skipped") or ""),
        )
        out["gates"]["seq"] = gs
    except Exception as e:
        _step("seq_unlock", ok=False, error=str(e))

    try:
        if not out.get("perform_after"):
            out["perform_after"] = snapshot_host_perform(session, log=log) or {}
    except Exception:
        pass

    w1, cast1, host1 = _watch_now()
    out["post_watch"] = w1 or {}
    try:
        gpost = snapshot_host_next_skill_gates(
            session, host_ptr=int(host1 or host_ptr or 0), cast_this=int(cast1 or cast_ptr or 0), log=log
        )
        out["gates"]["post"] = gpost
    except Exception as e:
        gpost = {"error": str(e)}
        out["gates"]["post"] = gpost

    out["post_diag"] = {
        "cast": hex(int(cast1 or 0) & 0xFFFFFFFF),
        "id": hex(int((w1 or {}).get("+10") or 0)),
        "cfg18": hex(int((w1 or {}).get("+18") or 0)),
        "busy": int((w1 or {}).get("+4A0") or 0),
        "ptype": (out["perform_after"] or {}).get("perform_type"),
        "sig": sig,
        "lock587": bool((gpost or {}).get("lock_587b00")),
        "h4a4": hex(int((gpost or {}).get("h4a4_u32") or 0)),
        "seq_hint": None,
    }
    try:
        # best-effort seq hint from gate helper if present
        out["post_diag"]["seq_hint"] = (out.get("gates") or {}).get("seq", {}).get("note")
    except Exception:
        pass
    _step("post", **{k: v for k, v in out["post_diag"].items() if k != "seq_hint"})

    # ok requires bridge cast ok AND (signature OR at least gates cleared after)
    lock_clear = not bool((gpost or {}).get("lock_587b00"))
    out["ok"] = bool(res.ok) and (sig or lock_clear)
    if not sig:
        out["error"] = (
            out.get("error")
            or "CAST ret ok but no interrupt signature "
            f"(mid +18=0x{mid_cfg:X} expected 0x{sid:X}; note={note[:120]})"
        )
    out["note"] = (
        f"ORDER=cast_interrupt_v2 id=0x{sid:X}->0x53E110"
        f"{'+stop65' if clear_block else ''}+host4a4+timer+seq; "
        f"sig={sig}; cast={note}; block={stop_note or 'skip'}; "
        f"post={out.get('post_diag')}"
    )
    log(f"cast_interrupt RESULT ok={out['ok']} {out['note']}")
    return out


def wait_cast_idle(
    session,
    *,
    log: LogFn | None = None,
    rounds: int = 100,
    interval_s: float = 0.05,
    stop_fn: Callable[[], bool] | None = None,
) -> dict:
    """Wait until cast looks idle so next trial starts clean."""
    log = log or (lambda _m: None)
    for i in range(int(rounds)):
        if stop_fn and stop_fn():
            return {
                "ok": False,
                "wait_rounds": i,
                "watch": {},
                "cast_this": 0,
                "host_ptr": 0,
                "error": "stopped",
            }
        watch, cast_this, host = snapshot_cast(session)
        if is_idle_watch(watch):
            if i > 0:
                log(f"recovery_lab: idle HIT[{i+1}] {watch_summary(watch)}")
            return {
                "ok": True,
                "wait_rounds": i + 1,
                "watch": watch,
                "cast_this": cast_this,
                "host_ptr": host,
            }
        if i == 0 or (i + 1) % 20 == 0:
            log(
                f"recovery_lab: wait idle[{i+1}/{rounds}] {watch_summary(watch)}"
            )
        time.sleep(float(interval_s))
    watch, cast_this, host = snapshot_cast(session)
    return {
        "ok": False,
        "wait_rounds": rounds,
        "watch": watch,
        "cast_this": cast_this,
        "host_ptr": host,
        "error": "timeout waiting idle",
    }



def _perform_summary(st: dict | None) -> str:
    """One-line host perform snapshot for recovery logs."""
    s = st or {}
    if not s:
        return "perform=?"
    return (
        f"type={s.get('perform_type')} sub={s.get('perform_subtype')} "
        f"curr=0x{int(s.get('curr_perform') or 0):X} "
        f"gate={s.get('session_gate')}/{s.get('session_gate1')}/{s.get('session_gate2')} "
        f"base={s.get('is_base')} matter={s.get('is_matter')} "
        f"active={s.get('active')}"
    )


def snapshot_host_perform(session, *, log: LogFn | None = None) -> dict:
    """Thin wrapper around yaolu perform reader (lab only)."""
    log = log or (lambda _m: None)
    try:
        from app.core.yaolu_auto import read_host_perform_state

        return read_host_perform_state(session, log=log)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def lab_stop_skill_perform(
    session,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> dict:
    """
    Lab-only: stop skill-era host perform without 0x21.

    Live cast holds perform type=4 (not locomotion type=2, not matter) and
    host+0x41C gate often mirrors cast +18/+70. Call HostStopSession-style
    mgr.vt+0x10(0x65), clear gate, restore type=2 if slot empty.
    Never bare-nulls non-matter curr. Never sends cancel_session(0x21).

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "channel": "perform",
        "before": {},
        "after": {},
        "method": "",
        "steps": [],
        "error": "",
        "note": "",
    }
    try:
        from app.core.yaolu_auto import (
            HOST_OFF_SESSION_GATE,
            PERFORM_SIDE_OFF_FLAG0,
            PERFORM_SIDE_OFF_FLAG1,
            PERFORM_STOP_ARG,
            PERFORM_STOP_VT_OFF,
            ensure_base_locomotion_perform,
            read_host_perform_state,
            _read_u32,
            _write_u32,
            _write_u8,
        )
        from app.core.remote_runtime import remote_call_thiscall_x86
    except Exception as e:
        out["error"] = f"import: {e}"
        return out

    before = read_host_perform_state(session, log=log)
    out["before"] = before
    if not before.get("ok"):
        out["error"] = str(before.get("error") or "perform unreadable")
        return out

    pid = int(getattr(session, "pid", 0) or 0)
    mgr = int(before.get("perform_mgr") or 0) & 0xFFFFFFFF
    side = int(before.get("side") or 0) & 0xFFFFFFFF
    host = int(before.get("host") or 0) & 0xFFFFFFFF
    ptype = before.get("perform_type")
    steps: list[dict] = []

    def _step(name: str, **kw) -> None:
        row = {"name": name, **kw}
        steps.append(row)
        log(f"lab_stop_skill_perform step {name} {kw}")

    skillish = bool(
        before.get("active")
        and not before.get("is_base")
        and not before.get("is_matter")
    )
    gate_on = bool(
        int(before.get("session_gate") or 0)
        or int(before.get("session_gate1") or 0)
        or int(before.get("session_gate2") or 0)
    )
    if not skillish and not gate_on and not before.get("is_matter"):
        out["ok"] = True
        out["method"] = "noop"
        out["note"] = f"nothing to stop ({_perform_summary(before)})"
        out["after"] = before
        out["steps"] = steps
        log(f"lab_stop_skill_perform noop {out['note']}")
        return out

    if not pid or not mgr:
        out["error"] = "pid/mgr missing"
        out["steps"] = steps
        return out

    try:
        if side:
            _write_u8(session, side + PERFORM_SIDE_OFF_FLAG0, 0)
            _write_u8(session, side + PERFORM_SIDE_OFF_FLAG1, 0)
            _step("side_flags", side=hex(side))

        mvt = _read_u32(session, mgr)
        fn = (
            _read_u32(session, (mvt + PERFORM_STOP_VT_OFF) & 0xFFFFFFFF)
            if mvt
            else 0
        )
        if fn:
            ret = remote_call_thiscall_x86(
                pid,
                int(fn) & 0xFFFFFFFF,
                mgr,
                [int(PERFORM_STOP_ARG)],
                caller_cleanup=False,
                timeout_ms=3000,
            )
            out["method"] = "mgr.vt+0x10(0x65)"
            _step("mgr_stop65", fn=hex(fn), ret=ret, type=ptype)
        else:
            _step("mgr_stop65_skip", reason="vt fn null")
            out["error"] = "mgr.vt+0x10 null"

        if host and gate_on:
            g0 = _read_u32(session, host + HOST_OFF_SESSION_GATE)
            g1 = _read_u32(session, host + HOST_OFF_SESSION_GATE + 4)
            g2 = _read_u32(session, host + HOST_OFF_SESSION_GATE + 8)
            ok0 = _write_u32(session, host + HOST_OFF_SESSION_GATE, 0)
            ok1 = _write_u32(session, host + HOST_OFF_SESSION_GATE + 4, 0)
            ok2 = _write_u32(session, host + HOST_OFF_SESSION_GATE + 8, 0)
            out["method"] = (out.get("method") or "") + "+clear_gate"
            _step(
                "clear_gate",
                before=[hex(g0), hex(g1), hex(g2)],
                ok=bool(ok0 and ok1 and ok2),
            )

        after = read_host_perform_state(session, log=lambda _m: None)
        if after.get("base_missing") or not after.get("active"):
            rb = ensure_base_locomotion_perform(session, log=log)
            out["restore_base"] = {
                "ok": rb.get("ok"),
                "method": rb.get("method"),
                "note": rb.get("note"),
                "error": rb.get("error"),
            }
            out["method"] = (out.get("method") or "") + "+restore_base"
            after = read_host_perform_state(session, log=lambda _m: None)

        out["after"] = after
        out["steps"] = steps
        after_skillish = bool(
            after.get("active")
            and not after.get("is_base")
            and not after.get("is_matter")
        )
        after_gate = bool(
            int(after.get("session_gate") or 0)
            or int(after.get("session_gate1") or 0)
            or int(after.get("session_gate2") or 0)
        )
        called = "mgr.vt+0x10(0x65)" in (out.get("method") or "")
        out["ok"] = bool(called and not after_skillish and not after_gate)
        if out["ok"]:
            out["note"] = (
                f"stop ok before=({_perform_summary(before)}) "
                f"after=({_perform_summary(after)})"
            )
            out["error"] = ""
        elif called:
            # Still useful if mgr stop ran but residual remains.
            out["ok"] = True
            out["note"] = (
                f"mgr stop called residual "
                f"after=({_perform_summary(after)})"
            )
            out["error"] = ""
        elif not out.get("error"):
            out["error"] = f"stop incomplete after=({_perform_summary(after)})"
        log(
            f"lab_stop_skill_perform RESULT ok={out['ok']} method={out.get('method')} "
            f"note={out.get('note')!r} err={out.get('error')!r}"
        )
    except Exception as e:
        out["error"] = str(e)
        out["steps"] = steps
        log(f"lab_stop_skill_perform err: {e}")
    return out




def lab_hoststop_local_teardown(
    session,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
    hold_s: float | None = None,
    poll_s: float = 0.016,
    onskill_mode: int = 0,
    do_hold: bool = True,
) -> dict:
    """
    Lab-only local cast teardown aimed at immediate next-skill unlock.

    Live 2026-07-21 failure:
      Calling 0x73DE10/0x754BA0 BEFORE OnSkillStopped zeroed identity so
      OnSkill failed with identity=0; only WPM strip ran; id refilled @0.8s.
      User felt no cancel; still waited full skill.

    Fixed order (critical):
      1) If perform type skillish (type=4): stop65 first (kill OnPerformSkill driver)
      2) OnSkillStopped identity-match WHILE +10 still set (mode0 default)
      3) Then 0x754BA0 clear skill-obj +CC (optional; never before OnSkill)
      4) Short hold-strip (~60-180ms) with early-exit when idle; anti-refill only
      5) 0x73DE10 only as late optional if still dirty (does not block OnSkill)

    Never sends 0x21.

    @author by ak
    """
    log = log or (lambda _m: None)
    quiet = lambda _m: None
    out: dict = {
        "ok": False,
        "channel": "hoststop_local",
        "method": "hoststop_local",
        "note": "",
        "error": "",
        "steps": [],
        "native": {},
        "perform_before": {},
        "perform_after": {},
        "cc_before": None,
        "cc_after": None,
        "suppress_hits": 0,
        "raw": {},
    }

    def _step(name: str, **kw) -> None:
        row = {"name": name, **kw}
        out["steps"].append(row)
        log(f"hoststop_local step {name} {kw}")

    blocked = _unsafe_skill_write_block_reason(session)
    if blocked:
        out["error"] = blocked
        return out

    from app.core.remote_runtime import remote_call_thiscall_x86, remote_read_bytes
    from app.core.xajh_bridge import ensure_bridge

    # Snapshot cast early for hold duration.
    w_pre, cast_pre, _h = snapshot_cast(session)
    total_ms = int(w_pre.get("+1C") or 0)
    elapsed_ms = int(w_pre.get("+20") or 0)
    remain_s = 0.0
    if total_ms > 0:
        remain_s = max(0.0, (total_ms - elapsed_ms) / 1000.0)
    if hold_s is None:
        # Live 094610: 1.2~2.5s hold made cancel feel half-beat LATE.
        # Only need a short anti-refill window; early-exit when idle.
        hold_s = max(0.06, min(0.18, 0.08 if remain_s <= 0 else min(0.18, remain_s * 0.15)))
    hold_s = float(hold_s)

    # --- 1) stop skill-era perform if present ---
    r1 = lab_stop_skill_perform(session, hwnd=hwnd, log=log)
    out["perform_before"] = r1.get("before") or {}
    out["raw"]["stop65"] = {
        "ok": r1.get("ok"),
        "method": r1.get("method"),
        "note": r1.get("note"),
        "error": r1.get("error"),
    }
    _step("stop65", ok=bool(r1.get("ok")), method=r1.get("method"))

    # Re-read cast AFTER stop65 — OnSkill needs live identity.
    w_mid, cast, _h2 = snapshot_cast(session)
    if not cast:
        cur = resolve_cast_this(session, log=quiet, with_dumps=False)
        cast = int(cur.cast_this or 0) & 0xFFFFFFFF
        w_mid = extract_cast_watch(
            getattr(cur, "cast_dump", None) or b""
        ) if hasattr(cur, "cast_dump") else w_mid
    cast = int(cast or cast_pre or 0) & 0xFFFFFFFF
    pid = int(getattr(session, "pid", 0) or 0)
    if not cast or not pid:
        out["error"] = "cast_this null"
        out["perform_after"] = snapshot_host_perform(session)
        return out

    id_now = int(w_mid.get("+10") or 0)
    # Capture SkillSequence* before OnSkill/754BA0 may zero +CC
    seq_saved = 0
    try:
        from app.core.remote_runtime import remote_read_bytes as _rr

        raw_cc = _rr(pid, (cast + CAST_SKILL_OBJ_OFF) & 0xFFFFFFFF, 4)
        seq_saved = (
            int.from_bytes(raw_cc[:4], "little") if raw_cc and len(raw_cc) >= 4 else 0
        )
    except Exception:
        seq_saved = int(w_mid.get("+CC") or 0)
    out["seq_ptr_saved"] = seq_saved
    out["cc_before"] = seq_saved
    seq_snap0 = snapshot_skill_sequence(
        session, cast_this=cast, seq_ptr=seq_saved, log=quiet
    )
    out["seq_before"] = seq_snap0
    # Unlock while +CC is STILL live (before OnSkill/754BA0 unlinks).
    # Safe mode only WPM +488/+490; never orphan write after unlink.
    seq_early = lab_force_can_cast_next_unit(
        session, cast_this=cast, seq_ptr=seq_saved, log=log, mode="safe"
    )
    out["seq_unlock_early"] = seq_early
    _step(
        "pre_onskill",
        cast=hex(cast),
        id=hex(id_now),
        p18=hex(int(w_mid.get("+18") or 0)),
        busy=hex(int(w_mid.get("+4A0") or 0)),
        seq=hex(seq_saved),
        seq_hint=seq_snap0.get("can_cast_next_hint"),
        seq_early=seq_early.get("note"),
        hold_s=round(hold_s, 3),
    )

    # --- 2) OnSkillStopped FIRST while identity present ---
    mode = int(onskill_mode)
    if mode not in (0, 1, 2, 3):
        mode = 0
    br = ensure_bridge(
        pid,
        log=log,
        inject_if_needed=True,
        hwnd=int(hwnd or 0) or None,
        force_reinject=False,
    )
    if br is None:
        out["error"] = "bridge unavailable"
        out["perform_after"] = snapshot_host_perform(session)
        return out

    onskill_ok = False
    note = ""
    try:
        # If identity already 0, skip native (will strip); else call now.
        if id_now:
            res = br.on_skill_stopped(
                hwnd=int(hwnd or 0) or None,
                timeout_ms=3000,
                mode=mode,
            )
            note = str(res.note or res.error or "")
            onskill_ok = bool(res.ok)
            out["native"] = parse_onskill_native_note(note)
            out["raw"]["onskill"] = {"ok": onskill_ok, "note": note, "mode": mode}
            _step("onskill", ok=onskill_ok, mode=mode, note=note[:140])
            # Retry once if identity=0 failure but id reappeared.
            if (not onskill_ok) and "identity=0" in note:
                w_retry, _, _ = snapshot_cast(session)
                if int(w_retry.get("+10") or 0):
                    res2 = br.on_skill_stopped(
                        hwnd=int(hwnd or 0) or None,
                        timeout_ms=3000,
                        mode=mode,
                    )
                    note = str(res2.note or res2.error or "")
                    onskill_ok = bool(res2.ok)
                    out["native"] = parse_onskill_native_note(note)
                    out["raw"]["onskill_retry"] = {
                        "ok": onskill_ok,
                        "note": note,
                        "mode": mode,
                    }
                    _step("onskill_retry", ok=onskill_ok, note=note[:140])
        else:
            out["raw"]["onskill"] = {"ok": False, "note": "skip identity already 0", "mode": mode}
            _step("onskill_skip", reason="identity already 0")
            note = "skip identity already 0"
    finally:
        try:
            br.close()
        except Exception:
            pass

    # --- 3) clear skill obj AFTER OnSkill ---
    def _read_cc(addr: int) -> int | None:
        try:
            raw = remote_read_bytes(pid, int(addr) + CAST_SKILL_OBJ_OFF, 4)
            if raw and len(raw) == 4:
                return int.from_bytes(raw, "little")
        except Exception:
            pass
        # session fallback (095755: remote_read sometimes reported 0 while dump had +CC)
        try:
            rpm = getattr(session, "read_u32", None) or getattr(session, "ru32", None)
            if callable(rpm):
                return int(rpm(int(addr) + CAST_SKILL_OBJ_OFF) or 0) & 0xFFFFFFFF
        except Exception:
            pass
        try:
            from app.core.skill_cast_probe import read_u32 as _ru32  # type: ignore
            return int(_ru32(session, int(addr) + CAST_SKILL_OBJ_OFF) or 0) & 0xFFFFFFFF
        except Exception:
            return None

    out["cc_before"] = _read_cc(cast)

    try:
        ret_obj = remote_call_thiscall_x86(
            pid,
            int(VA_CLEAR_SKILL_OBJ) & 0xFFFFFFFF,
            cast,
            [],
            caller_cleanup=False,
            timeout_ms=2000,
        )
        _step("clear_obj_754BA0", ret=ret_obj, cc_before=hex(out["cc_before"] or 0))
        out["raw"]["clear_obj"] = {"ok": True, "ret": ret_obj}
    except Exception as e:
        _step("clear_obj_754BA0_fail", err=str(e)[:120])
        out["raw"]["clear_obj"] = {"ok": False, "error": str(e)}

    out["cc_after"] = _read_cc(cast)
    # One retry if +CC still live (refill / race with game thread)
    if out.get("cc_after"):
        try:
            ret_obj2 = remote_call_thiscall_x86(
                pid,
                int(VA_CLEAR_SKILL_OBJ) & 0xFFFFFFFF,
                cast,
                [],
                caller_cleanup=False,
                timeout_ms=1500,
            )
            out["cc_after"] = _read_cc(cast)
            _step(
                "clear_obj_754BA0_retry",
                ret=ret_obj2,
                cc_after=hex(out["cc_after"] or 0),
            )
            out["raw"]["clear_obj_retry"] = {
                "ok": True,
                "ret": ret_obj2,
                "cc_after": out["cc_after"],
            }
        except Exception as e:
            _step("clear_obj_754BA0_retry_fail", err=str(e)[:120])

    # --- 4) immediate strip ---
    w0, _, _ = snapshot_cast(session)
    if cast_watch_dirty(w0):
        rstrip = clear_cast_session(
            session, log=quiet, require_active=False, variant=CAST_CANCEL_PERFORM_STRIP
        )
        _step("strip0", ok=bool(rstrip.get("ok")), id=hex(int(w0.get("+10") or 0)))

    # --- 5) short hold: kill type=4 + strip id + clear host/cast gates ---
    # Early-exit when cast idle AND perform not skillish for 2 polls (fast cancel).
    hits = 0
    s65_n = 0
    idle_polls = 0
    if do_hold and hold_s > 0:
        t_end = time.perf_counter() + hold_s
        poll = max(0.008, float(poll_s))
        last_s65 = 0.0
        last_onskill2 = 0.0
        last_gate_clear = 0.0
        while time.perf_counter() < t_end:
            w, cast_now, host_now = snapshot_cast(session)
            perf = snapshot_host_perform(session, log=quiet)
            skillish = perform_is_skillish(perf)
            dirty = cast_watch_dirty(w)
            if (not skillish) and (not dirty) and int(w.get("+10") or 0) == 0:
                idle_polls += 1
                if idle_polls >= 2:
                    _step("hold_early_exit", idle_polls=idle_polls)
                    break
            else:
                idle_polls = 0
            if skillish and s65_n < 8 and (time.perf_counter() - last_s65 > 0.05):
                lab_stop_skill_perform(session, hwnd=hwnd, log=quiet)
                last_s65 = time.perf_counter()
                s65_n += 1
                hits += 1
            if dirty:
                clear_cast_session(
                    session,
                    log=quiet,
                    require_active=False,
                    variant=CAST_CANCEL_PERFORM_STRIP,
                )
                hits += 1
                if (
                    int(w.get("+10") or 0)
                    and time.perf_counter() - last_onskill2 > 0.25
                ):
                    br2 = ensure_bridge(
                        pid,
                        log=quiet,
                        inject_if_needed=False,
                        hwnd=int(hwnd or 0) or None,
                        force_reinject=False,
                    )
                    if br2 is not None:
                        try:
                            br2.on_skill_stopped(
                                hwnd=int(hwnd or 0) or None,
                                timeout_ms=1500,
                                mode=0,
                            )
                            last_onskill2 = time.perf_counter()
                            hits += 1
                        finally:
                            try:
                                br2.close()
                            except Exception:
                                pass
            # re-clear host 4a4 + cast timer if refilled (cheap, rate-limited)
            if time.perf_counter() - last_gate_clear > 0.04:
                try:
                    lab_clear_host_4a4_flags(
                        session, host_ptr=int(host_now or 0), log=quiet
                    )
                    lab_clear_cast_timer_block(
                        session, cast_this=int(cast_now or cast or 0), log=quiet
                    )
                    last_gate_clear = time.perf_counter()
                except Exception:
                    pass
            time.sleep(poll)

    # --- 5b) final type=4 drain (short) ---
    for i in range(3):
        perf = snapshot_host_perform(session, log=quiet)
        if not perform_is_skillish(perf):
            break
        lab_stop_skill_perform(session, hwnd=hwnd, log=log if i == 0 else quiet)
        s65_n += 1
        hits += 1
        time.sleep(0.02)
    _step("type4_drain", stop65_total=s65_n, hits=hits)

    # --- 6) optional late sub-clear if still dirty ---
    w_f, _, _ = snapshot_cast(session)
    if cast_watch_dirty(w_f):
        try:
            ret_sub = remote_call_thiscall_x86(
                pid,
                int(VA_CAST_SUB_CLEAR) & 0xFFFFFFFF,
                (cast + CAST_SUB_CLEAR_OFF) & 0xFFFFFFFF,
                [],
                caller_cleanup=False,
                timeout_ms=1500,
            )
            _step("late_sub_clear_73DE10", ret=ret_sub)
            out["raw"]["sub_clear"] = {"ok": True, "ret": ret_sub, "when": "late"}
            clear_cast_session(
                session, log=quiet, require_active=False, variant=CAST_CANCEL_PERFORM_STRIP
            )
            w_f, _, _ = snapshot_cast(session)
        except Exception as e:
            out["raw"]["sub_clear"] = {"ok": False, "error": str(e), "when": "late"}

    # Force CanCastNextUnit on saved SkillSequence (even if +CC already zeroed)
    seq_u = lab_force_can_cast_next_unit(
        session,
        cast_this=cast,
        seq_ptr=int(out.get("seq_ptr_saved") or 0),
        log=log,
    )
    out["seq_unlock"] = seq_u
    out["seq_after"] = snapshot_skill_sequence(
        session,
        cast_this=cast,
        seq_ptr=int(out.get("seq_ptr_saved") or 0),
        log=quiet,
    )
    try:
        from app.core.remote_runtime import remote_read_bytes as _rr2

        raw_cc2 = _rr2(pid, (cast + CAST_SKILL_OBJ_OFF) & 0xFFFFFFFF, 4)
        out["cc_after"] = (
            int.from_bytes(raw_cc2[:4], "little") if raw_cc2 and len(raw_cc2) >= 4 else 0
        )
    except Exception:
        out["cc_after"] = int(w_f.get("+CC") or 0)

    out["suppress_hits"] = hits
    perf_f = snapshot_host_perform(session)
    out["perform_after"] = perf_f
    out["raw"]["final_watch"] = w_f
    out["ok"] = bool(onskill_ok or r1.get("ok") or hits > 0 or seq_u.get("ok"))
    out["note"] = (
        f"ORDER=stop65->onskill(mode={mode})->754BA0->seq_unlock->hold{hold_s:.2f}s; "
        f"onskill_ok={onskill_ok}; hits={hits}; "
        f"cc=0x{(out.get('cc_before') or 0):X}->0x{(out.get('cc_after') or 0):X}; "
        f"seq={seq_u.get('note')}; "
        f"final_id=0x{int(w_f.get('+10') or 0):X} "
        f"+18=0x{int(w_f.get('+18') or 0):X} 4a0=0x{int(w_f.get('+4A0') or 0):X} "
        f"ptype={perf_f.get('perform_type')}; onskill={note}"
    )
    if cast_watch_dirty(w_f) or perform_is_skillish(perf_f):
        out["error"] = (
            f"ended dirty id=0x{int(w_f.get('+10') or 0):X} "
            f"type={perf_f.get('perform_type')}"
        )
        out["ok"] = True
    log(f"hoststop_local RESULT ok={out['ok']} note={out['note']!r}")
    return out



def lab_stop65_onskill_hold(
    session,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
    hold_s: float = 0.70,
    poll_s: float = 0.016,
) -> dict:
    """
    Anti-refill combo for early recovery windows.

    Live (2026-07-21 early +20 small):
      stop65 + OnSkill clears cast immediately, but +10/+18 refill ~80ms
      with busy still 0 -> character stuck, cannot recast.
    Late window sometimes stable without hold.

    Recipe (lab only):
      1) mgr.vt+0x10(0x65)+gate  (type 4->2)
      2) OnSkillStopped identity-match mode0
      3) for ~hold_s: if type skillish again -> stop65;
         if +10/+18/busy back -> WPM perform_strip
    Never uses 0x21.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "channel": "perform+bridge+hold",
        "method": "stop65_onskill_hold",
        "note": "",
        "error": "",
        "steps": [],
        "suppress_hits": 0,
        "suppress_rounds": 0,
        "native": {},
        "perform_before": {},
        "perform_after": {},
        "raw": {},
    }

    def _step(name: str, **kw) -> None:
        row = {"name": name, **kw}
        out["steps"].append(row)
        log(f"lab_hold step {name} {kw}")

    def _need_cast_clear(w: dict) -> bool:
        return bool(
            int(w.get("+10") or 0)
            or int(w.get("+18") or 0)
            or (int(w.get("+4A0") or 0) & 1)
        )

    def _skillish(p: dict) -> bool:
        return bool(
            p.get("active")
            and not p.get("is_base")
            and not p.get("is_matter")
            and int(p.get("perform_type") or 0) not in (0, 2)
        )

    # --- 1) stop perform driver ---
    r1 = lab_stop_skill_perform(session, hwnd=hwnd, log=log)
    out["perform_before"] = r1.get("before") or {}
    out["raw"]["stop65_1"] = {
        "ok": r1.get("ok"),
        "method": r1.get("method"),
        "note": r1.get("note"),
        "error": r1.get("error"),
    }
    _step(
        "stop65_1",
        ok=bool(r1.get("ok")),
        method=r1.get("method"),
        note=str(r1.get("note") or "")[:120],
    )

    # --- 2) OnSkillStopped identity-match ---
    blocked = _unsafe_skill_write_block_reason(session)
    if blocked:
        out["error"] = blocked
        out["note"] = str(r1.get("note") or "")
        out["perform_after"] = snapshot_host_perform(session)
        return out

    from app.core.xajh_bridge import ensure_bridge

    pid = int(getattr(session, "pid", 0) or 0)
    br = ensure_bridge(
        pid,
        log=log,
        inject_if_needed=True,
        hwnd=int(hwnd or 0) or None,
        force_reinject=False,
    )
    if br is None:
        out["error"] = "bridge unavailable"
        out["note"] = str(r1.get("note") or "")
        out["perform_after"] = snapshot_host_perform(session)
        return out

    try:
        res = br.on_skill_stopped(
            hwnd=int(hwnd or 0) or None,
            timeout_ms=3000,
            mode=0,
        )
    finally:
        try:
            br.close()
        except Exception:
            pass

    note = str(res.note or res.error or "")
    out["native"] = parse_onskill_native_note(note)
    out["raw"]["onskill"] = {"ok": bool(res.ok), "note": note, "error": str(res.error or "")}
    _step("onskill", ok=bool(res.ok), note=note[:160])

    # --- 3) hold / suppress refill window ---
    hits = 0
    rounds = 0
    t_end = time.perf_counter() + max(0.05, float(hold_s))
    poll = max(0.008, float(poll_s))
    while time.perf_counter() < t_end:
        rounds += 1
        w, _c, _h = snapshot_cast(session)
        perf = snapshot_host_perform(session)
        did = False
        if _skillish(perf):
            r2 = lab_stop_skill_perform(session, hwnd=hwnd, log=log)
            did = True
            hits += 1
            _step(
                "re_stop65",
                type=perf.get("perform_type"),
                ok=bool(r2.get("ok")),
            )
        if _need_cast_clear(w):
            r3 = clear_cast_session(
                session,
                log=log,
                require_active=False,
                variant=CAST_CANCEL_PERFORM_STRIP,
            )
            did = True
            hits += 1
            _step(
                "re_strip",
                id=hex(int(w.get("+10") or 0)),
                p18=hex(int(w.get("+18") or 0)),
                busy=hex(int(w.get("+4A0") or 0)),
                ok=bool(r3.get("ok")),
                err=str(r3.get("error") or "")[:80],
            )
        if not did:
            # keep sampling; early exit if clean for ~3 polls after first 50ms
            if rounds >= 4 and time.perf_counter() + 0.05 >= t_end:
                pass
        time.sleep(poll)

    out["suppress_hits"] = hits
    out["suppress_rounds"] = rounds

    # final ensure base / not skillish
    perf_f = snapshot_host_perform(session)
    if _skillish(perf_f):
        r4 = lab_stop_skill_perform(session, hwnd=hwnd, log=log)
        _step("final_stop65", ok=bool(r4.get("ok")))
        perf_f = snapshot_host_perform(session)
    w_f, _, _ = snapshot_cast(session)
    if _need_cast_clear(w_f):
        r5 = clear_cast_session(
            session,
            log=log,
            require_active=False,
            variant=CAST_CANCEL_PERFORM_STRIP,
        )
        _step("final_strip", ok=bool(r5.get("ok")))
        w_f, _, _ = snapshot_cast(session)

    out["perform_after"] = perf_f
    out["raw"]["final_watch"] = w_f
    clean = (not _need_cast_clear(w_f)) and (not _skillish(perf_f))
    out["ok"] = bool(r1.get("ok") or res.ok)  # research fire succeeded if either core step ran
    if res.ok or r1.get("ok"):
        out["ok"] = True
    out["note"] = (
        f"ORDER=stop65->onskill->hold{hold_s:.2f}s; "
        f"suppress_hits={hits}/{rounds}; "
        f"final_id=0x{int(w_f.get('+10') or 0):X} "
        f"+18=0x{int(w_f.get('+18') or 0):X} "
        f"4a0=0x{int(w_f.get('+4A0') or 0):X} "
        f"ptype={perf_f.get('perform_type')}; "
        f"onskill={note}"
    )
    if not clean and not out.get("error"):
        out["error"] = (
            f"hold ended dirty id=0x{int(w_f.get('+10') or 0):X} "
            f"+18=0x{int(w_f.get('+18') or 0):X} type={perf_f.get('perform_type')}"
        )
        # still ok=True for scoring; dirty final is informative
        out["ok"] = True
    log(f"lab_hold RESULT ok={out['ok']} note={out['note']!r} err={out.get('error')!r}")
    return out


def _fire_method(
    session,
    method: str,
    *,
    log: LogFn | None = None,
    hwnd: int = 0,
) -> dict:
    """Execute one lab method. Returns channel result dict."""
    log = log or (lambda _m: None)
    method = str(method or "").strip().lower()
    out: dict = {
        "method": method,
        "ok": False,
        "note": "",
        "error": "",
        "channel": "",
        "native": {},
    }

    if method in (METHOD_MEM_BUSY, METHOD_MEM_GATE, METHOD_MEM_PERFORM):
        if method == METHOD_MEM_BUSY:
            variant = CAST_CANCEL_BUSY_BIT
        elif method == METHOD_MEM_PERFORM:
            variant = CAST_CANCEL_PERFORM_STRIP
        else:
            variant = CAST_CANCEL_GATE_MINIMAL
        r = clear_cast_session(
            session,
            log=log,
            require_active=False,
            variant=variant,
        )
        out["channel"] = "memory"
        out["ok"] = bool(r.get("ok"))
        out["note"] = ",".join(r.get("wrote") or []) or str(r.get("variant") or "")
        out["error"] = str(r.get("error") or "")
        out["raw"] = {
            k: r.get(k)
            for k in ("variant", "wrote", "skipped_idle", "error", "ok")
        }
        return out

    if method in ONSKILL_NATIVE_MODE:
        blocked = _unsafe_skill_write_block_reason(session)
        if blocked:
            out["channel"] = "bridge"
            out["error"] = blocked
            return out
        from app.core.xajh_bridge import ensure_bridge

        pid = int(session.pid)
        native_mode = int(ONSKILL_NATIVE_MODE.get(method, 0))
        br = ensure_bridge(
            pid,
            log=log,
            inject_if_needed=True,
            hwnd=int(hwnd or 0) or None,
            force_reinject=False,
        )
        if br is None:
            out["channel"] = "bridge"
            out["error"] = "bridge unavailable"
            return out
        try:
            res = br.on_skill_stopped(
                hwnd=int(hwnd or 0) or None,
                timeout_ms=3000,
                mode=native_mode,
            )
        finally:
            try:
                br.close()
            except Exception:
                pass
        note = str(res.note or res.error or "")
        out["channel"] = "bridge"
        out["ok"] = bool(res.ok)
        out["note"] = note
        out["error"] = str(res.error or "") if not res.ok else ""
        out["native"] = parse_onskill_native_note(note)
        out["native_mode"] = native_mode
        return out

    if method == METHOD_PERFORM_STOP65:
        r = lab_stop_skill_perform(session, hwnd=hwnd, log=log)
        out["channel"] = "perform"
        out["ok"] = bool(r.get("ok"))
        out["note"] = str(r.get("note") or r.get("method") or "")
        out["error"] = str(r.get("error") or "")
        out["raw"] = {
            k: r.get(k)
            for k in (
                "method",
                "before",
                "after",
                "steps",
                "restore_base",
                "ok",
                "error",
                "note",
            )
        }
        out["perform_before"] = r.get("before") or {}
        out["perform_after"] = r.get("after") or {}
        return out

    if method == METHOD_KEY_INTERRUPT:
        r = lab_key_interrupt(session, hwnd=hwnd, log=log)
        out["channel"] = "key_interrupt"
        out["ok"] = bool(r.get("ok"))
        out["note"] = str(r.get("note") or "")
        out["error"] = str(r.get("error") or "")
        out["raw"] = r
        out["perform_before"] = r.get("perform_before")
        out["perform_after"] = r.get("perform_after")
        out["post_diag"] = r.get("post_diag")
        out["native"] = r.get("native") or {}
        return out

    if method == METHOD_CAST_INTERRUPT:
        r = lab_cast_interrupt_skill(session, hwnd=hwnd, log=log)
        out["channel"] = "cast_interrupt"
        out["ok"] = bool(r.get("ok"))
        out["note"] = str(r.get("note") or "")
        out["error"] = str(r.get("error") or "")
        out["raw"] = r
        out["perform_before"] = r.get("perform_before")
        out["perform_after"] = r.get("perform_after")
        out["post_diag"] = r.get("post_diag")
        out["native"] = r.get("native") or {}
        # Heal after trial to prevent locked skill for next round
        if not out["ok"] or r.get("sig", False) is False:
            try:
                lab_clear_host_4a4_flags(session, log=log)
                lab_clear_cast_timer_block(session, log=log)
                lab_force_can_cast_next_unit(session, log=log, mode="safe")
                _ = snapshot_cast(session, log=log)  # force refresh
            except Exception:
                pass
        return out

    if method == METHOD_SOFT_NEXT:
        r = lab_soft_next_unlock(session, hwnd=hwnd, log=log)
        out["channel"] = "soft_next"
        out["ok"] = bool(r.get("ok"))
        out["note"] = str(r.get("note") or "")
        out["error"] = str(r.get("error") or "")
        out["raw"] = r
        out["perform_before"] = r.get("perform_before")
        out["perform_after"] = r.get("perform_after")
        out["seq_unlock"] = r.get("seq_unlock")
        out["seq_unlock_early"] = r.get("seq_unlock_early")
        out["post_diag"] = r.get("post_diag")
        out["native"] = {}
        return out

    if method == METHOD_HOSTSTOP_BIT1:
        r = lab_hoststop_bit1(session, hwnd=hwnd, log=log)
        out["channel"] = "hoststop_bit1"
        out["ok"] = bool(r.get("ok"))
        out["note"] = str(r.get("note") or "")
        out["error"] = str(r.get("error") or "")
        out["raw"] = r
        out["perform_before"] = r.get("perform_before")
        out["perform_after"] = r.get("perform_after")
        out["native"] = {
            "native_cleared": r.get("native_cleared"),
            "native_id0": (r.get("native_ids") or (None, None, None))[0]
            if isinstance(r.get("native_ids"), (list, tuple))
            else r.get("native_id0"),
        }
        return out

    if method == METHOD_HOSTSTOP_LOCAL:
        r = lab_hoststop_local_teardown(session, hwnd=hwnd, log=log)
        out["channel"] = "hoststop_local"
        out["ok"] = bool(r.get("ok"))
        out["note"] = str(r.get("note") or "")
        out["error"] = str(r.get("error") or "")
        out["native"] = r.get("native") or {}
        out["native_mode"] = 1
        out["raw"] = r.get("raw") or {}
        out["perform_before"] = r.get("perform_before") or {}
        out["perform_after"] = r.get("perform_after") or {}
        out["suppress_hits"] = r.get("suppress_hits")
        return out

    if method == METHOD_STOP65_ONSKILL_HOLD:
        r = lab_stop65_onskill_hold(session, hwnd=hwnd, log=log)
        out["channel"] = "perform+bridge+hold"
        out["ok"] = bool(r.get("ok"))
        out["note"] = str(r.get("note") or "")
        out["error"] = str(r.get("error") or "")
        out["native"] = r.get("native") or {}
        out["native_mode"] = 0
        out["raw"] = r.get("raw") or {}
        out["perform_before"] = r.get("perform_before") or {}
        out["perform_after"] = r.get("perform_after") or {}
        out["suppress_hits"] = r.get("suppress_hits")
        out["suppress_rounds"] = r.get("suppress_rounds")
        return out

    if method == METHOD_ONSKILL_PERFORM_STOP65:
        r1 = lab_stop_skill_perform(session, hwnd=hwnd, log=log)
        blocked = _unsafe_skill_write_block_reason(session)
        if blocked:
            out["channel"] = "perform+bridge"
            out["ok"] = bool(r1.get("ok"))
            out["error"] = blocked
            out["note"] = str(r1.get("note") or "")
            out["raw"] = {"perform": r1}
            out["perform_before"] = r1.get("before") or {}
            out["perform_after"] = r1.get("after") or {}
            return out
        from app.core.xajh_bridge import ensure_bridge

        pid = int(session.pid)
        br = ensure_bridge(
            pid,
            log=log,
            inject_if_needed=True,
            hwnd=int(hwnd or 0) or None,
            force_reinject=False,
        )
        if br is None:
            out["channel"] = "perform+bridge"
            out["ok"] = bool(r1.get("ok"))
            out["error"] = "bridge unavailable"
            out["note"] = str(r1.get("note") or "")
            out["raw"] = {"perform": r1}
            return out
        try:
            res = br.on_skill_stopped(
                hwnd=int(hwnd or 0) or None,
                timeout_ms=3000,
                mode=0,
            )
        finally:
            try:
                br.close()
            except Exception:
                pass
        note = str(res.note or res.error or "")
        out["channel"] = "perform+bridge"
        out["ok"] = bool(r1.get("ok") or res.ok)
        out["note"] = (
            f"ORDER=stop65_then_onskill; "
            f"perform={r1.get('method') or r1.get('note')}; onskill={note}"
        )
        errs = []
        if not r1.get("ok") and r1.get("error"):
            errs.append(str(r1.get("error")))
        if not res.ok:
            errs.append(str(res.error or "onskill failed"))
        out["error"] = "; ".join(errs)
        out["native"] = parse_onskill_native_note(note)
        out["native_mode"] = 0
        out["raw"] = {"perform": r1, "onskill_ok": bool(res.ok)}
        out["perform_before"] = r1.get("before") or {}
        out["perform_after"] = r1.get("after") or {}
        return out

    if method == METHOD_CANCEL21:
        from app.core.xajh_bridge import ensure_bridge

        pid = int(session.pid)
        br = ensure_bridge(
            pid,
            log=log,
            inject_if_needed=True,
            hwnd=int(hwnd or 0) or None,
            force_reinject=False,
        )
        if br is None:
            out["channel"] = "bridge"
            out["error"] = "bridge unavailable"
            return out
        try:
            res = br.cancel_session(hwnd=int(hwnd or 0) or None, timeout_ms=3000)
        finally:
            try:
                br.close()
            except Exception:
                pass
        out["channel"] = "bridge"
        out["ok"] = bool(res.ok)
        out["note"] = str(res.note or "")
        out["error"] = str(res.error or "") if not res.ok else ""
        out["warning"] = "0x21 cancels whole cast session (negative control)"
        return out

    out["error"] = f"unknown method: {method!r}"
    return out


def run_recovery_trial(
    session,
    method: str,
    *,
    log: LogFn | None = None,
    hwnd: int = 0,
    wait_rounds: int = 200,
    interval_s: float = 0.05,
    post_delays_s: tuple[float, ...] | list[float] | None = None,
    already_in_window: dict | None = None,
    stop_fn: Callable[[], bool] | None = None,
) -> dict:
    """
    One automated trial: wait recovery -> fire method -> dense multi-sample after.
    """
    log = log or (lambda _m: None)
    method = str(method or "").strip().lower()
    label = METHOD_LABELS.get(method, method)
    delays = tuple(post_delays_s) if post_delays_s is not None else DEFAULT_POST_DELAYS_S
    trial: dict = {
        "ok": False,
        "method": method,
        "label": label,
        "before": {},
        "after": {},
        "after_immediate": {},
        "after_series": [],
        "score": {},
        "fire": {},
        "cast_this": 0,
        "host_ptr": 0,
        "wait_rounds": 0,
        "error": "",
        "report_path": "",
        "report_json": "",
    }

    if already_in_window and already_in_window.get("ok"):
        win = already_in_window
    else:
        win = wait_recovery_window(
            session,
            log=log,
            rounds=wait_rounds,
            interval_s=interval_s,
            stop_fn=stop_fn,
        )
    if not win.get("ok"):
        trial["error"] = win.get("error") or "no recovery window"
        log(f"recovery_lab FAIL: {trial['error']}")
        paths = write_recovery_trial_report(trial)
        trial["report_path"] = paths.get("md", "")
        trial["report_json"] = paths.get("json", "")
        return trial

    trial["wait_rounds"] = int(win.get("wait_rounds") or 0)
    trial["before"] = dict(win.get("watch") or {})
    trial["cast_this"] = int(win.get("cast_this") or 0)
    trial["host_ptr"] = int(win.get("host_ptr") or 0)
    trial["hit_meta"] = dict(win.get("hit_meta") or {})
    trial["edge_elapsed_s"] = win.get("edge_elapsed_s")
    trial["wait_phase"] = win.get("phase")

    # Re-read immediately before fire for accurate pre image.
    before, cast_this, host = snapshot_cast(session)
    if cast_this:
        trial["cast_this"] = cast_this
        trial["host_ptr"] = host
    perf_pre = snapshot_host_perform(session, log=lambda _m: None)
    if is_armed_cast(before, perf_pre):
        trial["before"] = before
    elif int((trial.get("before") or {}).get("+10") or 0) or (
        int((trial.get("before") or {}).get("+4A0") or 0) & 1
    ):
        # HIT had id/busy; re-read race (133113) — still fire.
        log(
            "recovery_lab: re-read not armed; keep HIT snapshot "
            f"{watch_summary(trial.get('before') or {})}"
        )
    else:
        trial["error"] = (
            "armed cast window lost before fire (need busy or type=4)"
        )
        log(f"recovery_lab FAIL: {trial['error']}")
        paths = write_recovery_trial_report(trial)
        trial["report_path"] = paths.get("md", "")
        trial["report_json"] = paths.get("json", "")
        return trial

    log(f"recovery_lab: FIRE {label} on {watch_summary(trial['before'])}")
    t_fire = time.perf_counter()
    perf0 = snapshot_host_perform(session, log=lambda _m: None)
    log(f"recovery_lab: perform BEFORE fire {_perform_summary(perf0)}")
    cast_for_dump = int(trial.get("cast_this") or 0)
    dump_before = cast_nonzero_dwords(session, cast_for_dump)
    trial["cast_nonzero_before"] = dump_before
    gates0 = snapshot_host_next_skill_gates(
        session,
        host_ptr=int(trial.get("host_ptr") or 0),
        cast_this=int(cast_for_dump or 0),
        log=log,
    )
    trial["host_gates_before"] = gates0
    log(f"recovery_lab: host_gates BEFORE {host_gates_summary(gates0)}")
    tm0 = snapshot_cast_timer_block(
        session, cast_this=int(cast_for_dump or 0), log=log
    )
    trial["cast_timer_before"] = tm0
    log(f"recovery_lab: cast_timer BEFORE {cast_timer_summary(tm0)}")
    seq0 = snapshot_skill_sequence(
        session, cast_this=int(cast_for_dump or 0), log=log
    )
    trial["seq_before"] = seq0
    trial["seq_ptr_at_hit"] = int(seq0.get("seq") or 0)
    log(f"recovery_lab: skill_seq BEFORE {skill_sequence_summary(seq0)}")
    log(
        f"recovery_lab: cast nonzero before n={len(dump_before)} "
        f"keys={list(dump_before.keys())[:24]}"
    )
    fire = _fire_method(session, method, log=log, hwnd=hwnd)
    # Soft path: only re-stop65 + safe seq; never identity strip / 754BA0 / OnSkill.
    if method == METHOD_SOFT_NEXT:
        t_pf = time.perf_counter() + 0.25
        while time.perf_counter() < t_pf:
            try:
                perf_pf = snapshot_host_perform(session, log=lambda _m: None)
                if perform_is_skillish(perf_pf):
                    lab_stop_skill_perform(session, hwnd=hwnd, log=lambda _m: None)
                w_pf, c_pf, _h = snapshot_cast(session)
                lab_force_can_cast_next_unit(
                    session,
                    cast_this=int(c_pf or 0),
                    seq_ptr=int(trial.get("seq_ptr_at_hit") or fire.get("seq_ptr_saved") or 0),
                    log=lambda _m: None,
                    mode="safe",
                )
            except Exception:
                pass
            time.sleep(0.012)
        try:
            fire["post_diag"] = format_post_fire_diag(
                session,
                cast_this=int(fire.get("cast") or cast_for_dump or 0),
                fire=fire,
                log=log,
            )
        except Exception as e:
            log(f"recovery_lab: soft POST_DIAG err: {e}")
    # Post-fire anti-refill (095755: id refilled @0.3s after type4->2 stop)
    # Cover ~400ms; re-stop65 + strip + 754BA0 if residue returns.
    # NOT for soft_next (identity wipe is the suspected "can't recast" cause).
    if method in (METHOD_HOSTSTOP_LOCAL, METHOD_HOSTSTOP_BIT1, METHOD_STOP65_ONSKILL_HOLD):
        t_pf = time.perf_counter() + 0.40
        last_obj_clear = 0.0
        while time.perf_counter() < t_pf:
            w_pf, c_pf, h_pf = snapshot_cast(session)
            dirty = cast_watch_dirty(w_pf) or int(w_pf.get("+10") or 0)
            if dirty:
                try:
                    clear_cast_session(
                        session,
                        log=lambda _m: None,
                        require_active=False,
                        variant=CAST_CANCEL_PERFORM_STRIP,
                    )
                    lab_clear_host_4a4_flags(
                        session, host_ptr=int(h_pf or 0), log=lambda _m: None
                    )
                    lab_clear_cast_timer_block(
                        session, cast_this=int(c_pf or 0), log=lambda _m: None
                    )
                    lab_force_can_cast_next_unit(
                        session,
                        cast_this=int(c_pf or 0),
                        seq_ptr=int(trial.get("seq_ptr_at_hit") or 0),
                        log=lambda _m: None,
                    )
                except Exception:
                    pass
            perf_pf = snapshot_host_perform(session, log=lambda _m: None)
            if perform_is_skillish(perf_pf):
                try:
                    lab_stop_skill_perform(session, hwnd=hwnd, log=lambda _m: None)
                except Exception:
                    pass
            # Re-clear +CC skill obj if refilled (095755 residual +CC stayed set)
            now = time.perf_counter()
            if c_pf and (now - last_obj_clear) > 0.05:
                try:
                    from app.core.remote_runtime import remote_call_thiscall_x86, remote_read_bytes
                    pid = int(getattr(session, "pid", 0) or 0)
                    raw = remote_read_bytes(pid, int(c_pf) + CAST_SKILL_OBJ_OFF, 4) if pid else b""
                    cc = int.from_bytes(raw, "little") if raw and len(raw) == 4 else 0
                    if cc:
                        remote_call_thiscall_x86(
                            pid,
                            int(VA_CLEAR_SKILL_OBJ) & 0xFFFFFFFF,
                            int(c_pf) & 0xFFFFFFFF,
                            [],
                            caller_cleanup=False,
                            timeout_ms=1200,
                        )
                        last_obj_clear = now
                except Exception:
                    pass
            time.sleep(0.012)
    perf1 = snapshot_host_perform(session, log=lambda _m: None)
    log(f"recovery_lab: perform AFTER fire {_perform_summary(perf1)}")
    if method in (METHOD_HOSTSTOP_LOCAL, METHOD_HOSTSTOP_BIT1, METHOD_SOFT_NEXT) and not fire.get("post_diag"):
        try:
            fire["post_diag"] = format_post_fire_diag(
                session,
                cast_this=int(fire.get("cast") or cast_for_dump or 0),
                fire=fire if isinstance(fire, dict) else {},
                log=log,
            )
        except Exception as e:
            log(f"recovery_lab: POST_DIAG err: {e}")
    dump_after = cast_nonzero_dwords(session, cast_for_dump or int(trial.get("cast_this") or 0))
    trial["cast_nonzero_after"] = dump_after
    trial["cast_nonzero_diff"] = cast_dump_diff(dump_before, dump_after)
    gates1 = snapshot_host_next_skill_gates(
        session,
        host_ptr=int(trial.get("host_ptr") or 0),
        cast_this=int(cast_for_dump or trial.get("cast_this") or 0),
        log=log,
    )
    trial["host_gates_after"] = gates1
    log(f"recovery_lab: host_gates AFTER {host_gates_summary(gates1)}")
    tm1 = snapshot_cast_timer_block(
        session,
        cast_this=int(cast_for_dump or trial.get("cast_this") or 0),
        log=log,
    )
    trial["cast_timer_after"] = tm1
    log(f"recovery_lab: cast_timer AFTER {cast_timer_summary(tm1)}")
    log(
        f"recovery_lab: cast nonzero after n={len(dump_after)} "
        f"diff_n={len(trial['cast_nonzero_diff'])} "
        f"diff_keys={list(trial['cast_nonzero_diff'].keys())[:24]}"
    )
    fire["perform_before"] = fire.get("perform_before") or perf0
    fire["perform_after"] = fire.get("perform_after") or perf1

    trial["fire"] = fire
    trial["fire_dt_s"] = round(time.perf_counter() - t_fire, 4)

    series: list[dict] = []
    last: dict = {}
    t0 = time.perf_counter()
    for d in delays:
        if stop_fn and stop_fn():
            trial["error"] = "stopped during post-sample"
            break
        target = t0 + float(d)
        now = time.perf_counter()
        if target > now:
            time.sleep(target - now)
        w, cthis, _h = snapshot_cast(session)
        elapsed = round(time.perf_counter() - t0, 4)
        series.append(
            {
                "t_s": float(d),
                "t_elapsed_s": elapsed,
                "watch": w,
                "cast_this": cthis,
            }
        )
        last = w
    trial["after_series"] = series
    first_watch = series[0]["watch"] if series else {}
    trial["after_immediate"] = first_watch
    trial["after"] = last if last else first_watch
    trial["score"] = classify_series(
        trial["before"],
        series,
        native=fire.get("native") or {},
    )
    gates_late = snapshot_host_next_skill_gates(
        session,
        host_ptr=int(trial.get("host_ptr") or 0),
        cast_this=int(trial.get("cast_this") or 0),
        log=log,
    )
    trial["host_gates_late"] = gates_late
    tm_late = snapshot_cast_timer_block(
        session, cast_this=int(trial.get("cast_this") or 0), log=log
    )
    trial["cast_timer_late"] = tm_late
    log(f"recovery_lab: cast_timer LATE {cast_timer_summary(tm_late)}")
    seq_late = snapshot_skill_sequence(
        session,
        cast_this=int(trial.get("cast_this") or 0),
        seq_ptr=int(trial.get("seq_ptr_at_hit") or 0),
        log=log,
    )
    trial["seq_late"] = seq_late
    log(f"recovery_lab: skill_seq LATE {skill_sequence_summary(seq_late)}")
    trial["score"]["seq_before"] = trial.get("seq_before") or {}
    trial["score"]["seq_late"] = seq_late
    trial["score"]["seq_can_cast_hint_before"] = (trial.get("seq_before") or {}).get(
        "can_cast_next_hint"
    )
    trial["score"]["seq_can_cast_hint_late"] = seq_late.get("can_cast_next_hint")
    trial["score"]["host_gates_before"] = trial.get("host_gates_before") or {}
    trial["score"]["host_gates_after"] = trial.get("host_gates_after") or {}
    trial["score"]["host_gates_late"] = gates_late
    trial["score"]["host_bit1_before"] = bool((trial.get("host_gates_before") or {}).get("h1b8_bit1"))
    trial["score"]["host_bit1_after"] = bool((trial.get("host_gates_after") or {}).get("h1b8_bit1"))
    trial["score"]["host_bit1_late"] = bool(gates_late.get("h1b8_bit1"))
    trial["score"]["lock587_before"] = bool((trial.get("host_gates_before") or {}).get("lock_587b00"))
    trial["score"]["lock587_after"] = bool((trial.get("host_gates_after") or {}).get("lock_587b00"))
    trial["score"]["lock587_late"] = bool(gates_late.get("lock_587b00"))
    trial["score"]["h4a4_before"] = (trial.get("host_gates_before") or {}).get("h4a4_u32")
    trial["score"]["h4a4_after"] = (trial.get("host_gates_after") or {}).get("h4a4_u32")
    trial["score"]["h4a4_late"] = gates_late.get("h4a4_u32")
    log(f"recovery_lab: host_gates LATE {host_gates_summary(gates_late)}")
    trial["score"]["slots_before"] = slot_busy_flags(trial.get("before") or {})
    trial["score"]["slots_immediate"] = slot_busy_flags(trial.get("after_immediate") or {})
    trial["score"]["slots_late"] = slot_busy_flags(trial.get("after") or {})
    # Attach host-perform delta (type=4 skill-era vs type=2 locomotion).
    pb = fire.get("perform_before") or perf0 or {}
    pa = fire.get("perform_after") or perf1 or {}
    trial["score"]["perform_before"] = {
        "type": pb.get("perform_type"),
        "sub": pb.get("perform_subtype"),
        "gate": [
            pb.get("session_gate"),
            pb.get("session_gate1"),
            pb.get("session_gate2"),
        ],
        "active": pb.get("active"),
        "is_base": pb.get("is_base"),
        "is_matter": pb.get("is_matter"),
        "curr": int(pb.get("curr_perform") or 0) & 0xFFFFFFFF,
    }
    trial["score"]["perform_after"] = {
        "type": pa.get("perform_type"),
        "sub": pa.get("perform_subtype"),
        "gate": [
            pa.get("session_gate"),
            pa.get("session_gate1"),
            pa.get("session_gate2"),
        ],
        "active": pa.get("active"),
        "is_base": pa.get("is_base"),
        "is_matter": pa.get("is_matter"),
        "curr": int(pa.get("curr_perform") or 0) & 0xFFFFFFFF,
    }
    ptype0 = pb.get("perform_type")
    ptype1 = pa.get("perform_type")
    gate0 = bool(
        int(pb.get("session_gate") or 0)
        or int(pb.get("session_gate1") or 0)
        or int(pb.get("session_gate2") or 0)
    )
    gate1 = bool(
        int(pa.get("session_gate") or 0)
        or int(pa.get("session_gate1") or 0)
        or int(pa.get("session_gate2") or 0)
    )
    skillish0 = bool(pb.get("active") and not pb.get("is_base") and not pb.get("is_matter"))
    skillish1 = bool(pa.get("active") and not pa.get("is_base") and not pa.get("is_matter"))
    trial["score"]["perform_type_before"] = ptype0
    trial["score"]["perform_type_after"] = ptype1
    trial["score"]["perform_skillish_before"] = skillish0
    trial["score"]["perform_skillish_after"] = skillish1
    trial["score"]["perform_gate_before"] = gate0
    trial["score"]["perform_gate_after"] = gate1
    trial["score"]["perform_cleared"] = bool(skillish0 and not skillish1 and not gate1)
    trial["score"]["perform_summary"] = (
        f"BEFORE {_perform_summary(pb)} | AFTER {_perform_summary(pa)}"
    )
    # Research trial "ok" = fire succeeded and we produced a score.
    trial["ok"] = bool(fire.get("ok")) and bool(trial["score"].get("verdict"))
    if not fire.get("ok") and fire.get("error"):
        trial["error"] = str(fire.get("error"))
        trial["ok"] = False

    sc = trial["score"]
    log(
        f"recovery_lab: {label} -> {sc.get('verdict')} "
        f"id_imm={sc.get('id_cleared_immediate')} id_late={sc.get('id_cleared')} "
        f"reverted={sc.get('id_reverted')} refill_t={sc.get('id_refill_t_s')} "
        f"busy={sc.get('busy_cleared')} native={sc.get('native_cleared')} "
        f"nvp={sc.get('native_vs_python')} fire_ok={fire.get('ok')} "
        f"perform_cleared={sc.get('perform_cleared')} "
        f"type={sc.get('perform_type_before')}->{sc.get('perform_type_after')} "
        f"bit1={sc.get('host_bit1_before')}->{sc.get('host_bit1_after')}/{sc.get('host_bit1_late')} "
        f"lock587={sc.get('lock587_before')}->{sc.get('lock587_after')}/{sc.get('lock587_late')} "
        f"4a4={sc.get('h4a4_before')}->{sc.get('h4a4_after')}/{sc.get('h4a4_late')} "
        f"timer={cast_timer_summary(trial.get('cast_timer_after') or {})} "
        f"note={fire.get('note')!r}"
    )
    paths = write_recovery_trial_report(trial)
    trial["report_path"] = paths.get("md", "")
    trial["report_json"] = paths.get("json", "")
    if trial["report_path"]:
        log(f"recovery_lab: report {trial['report_path']}")
    return trial


def run_recovery_matrix(
    session,
    methods: tuple[str, ...] | list[str] | None = None,
    *,
    log: LogFn | None = None,
    hwnd: int = 0,
    wait_rounds: int = 200,
    interval_s: float = 0.05,
    between_prompt_s: float = 1.0,
    stop_fn: Callable[[], bool] | None = None,
) -> dict:
    """
    Run each method once. User must recast between methods.

    Between trials wait until idle so sticky id from previous clear does not
    block the next recovery window (needs busy bit again).
    """
    log = log or (lambda _m: None)
    methods = tuple(methods or RECOVERY_CANDIDATES)
    matrix: dict = {
        "ok": False,
        "trials": [],
        "summary": [],
        "report_path": "",
        "report_json": "",
        "error": "",
    }
    log(
        f"recovery_lab MATRIX START methods={list(methods)} "
        f"window~{wait_rounds * interval_s:.0f}s each — "
        f"when you see CAST A SKILL NOW, cast one skill"
    )
    for i, method in enumerate(methods):
        if stop_fn and stop_fn():
            matrix["error"] = "stopped"
            break
        label = METHOD_LABELS.get(method, method)
        if i > 0:
            log(
                f"recovery_lab MATRIX: wait busy-off before "
                f"[{i+1}/{len(methods)}] {label}…"
            )
            # Sticky id after refill is fine; only need busy bit down.
            wait_not_recovery_window(
                session,
                log=log,
                rounds=max(40, wait_rounds // 3),
                interval_s=interval_s,
                stop_fn=stop_fn,
            )
            if between_prompt_s > 0:
                time.sleep(float(between_prompt_s))
        log(
            f"recovery_lab MATRIX [{i+1}/{len(methods)}] next={label} — "
            f">>> CAST A SKILL NOW (need id+busy) <<<"
        )
        trial = run_recovery_trial(
            session,
            method,
            log=log,
            hwnd=hwnd,
            wait_rounds=wait_rounds,
            interval_s=interval_s,
            stop_fn=stop_fn,
        )
        matrix["trials"].append(trial)
        sc = trial.get("score") or {}
        matrix["summary"].append(
            {
                "method": method,
                "label": label,
                "verdict": sc.get("verdict"),
                "id_cleared": sc.get("id_cleared"),
                "id_cleared_immediate": sc.get("id_cleared_immediate"),
                "id_reverted": sc.get("id_reverted"),
                "id_refill_t_s": sc.get("id_refill_t_s"),
                "busy_cleared": sc.get("busy_cleared"),
                "native_cleared": sc.get("native_cleared"),
                "native_vs_python": sc.get("native_vs_python"),
                "perform_cleared": sc.get("perform_cleared"),
                "perform_type_before": sc.get("perform_type_before"),
                "perform_type_after": sc.get("perform_type_after"),
                "fire_ok": (trial.get("fire") or {}).get("ok"),
                "error": trial.get("error") or "",
                "report_path": trial.get("report_path") or "",
            }
        )
    matrix["ok"] = any(t.get("ok") for t in matrix["trials"])
    paths = write_recovery_matrix_report(matrix)
    matrix["report_path"] = paths.get("md", "")
    matrix["report_json"] = paths.get("json", "")
    if matrix["report_path"]:
        log(f"recovery_lab MATRIX report: {matrix['report_path']}")
    return matrix


def _lab_dest():
    from common.paths import app_root

    dest = app_root() / ".issues" / "lab"
    dest.mkdir(parents=True, exist_ok=True)
    return dest



def perform_is_skillish(st: dict | None) -> bool:
    """Skill-era perform (live type=4), not base locomotion / matter."""
    p = st or {}
    if not p.get("active"):
        return False
    if p.get("is_base") or p.get("is_matter"):
        return False
    ptype = int(p.get("perform_type") or 0)
    return ptype not in (0, 2)



def run_hand_interrupt_probe(
    session,
    *,
    log: LogFn | None = None,
    hwnd: int = 0,
    wait_s: float = 12.0,
    after_s: float = 6.0,
    poll_s: float = 0.01,
    stop_fn: Callable[[], bool] | None = None,
    status_fn: Callable[[str], None] | None = None,
    prompt_fn: Callable[[str], None] | None = None,
) -> dict:
    """
    Lab: user presses real X then Space; we only SNAPSHOT diffs (no key inject).

    UI must show a BIG yellow prompt on ARM: 现在手按 X 再空格.
    Natural skill end (type4->2 after full duration) is NOT a success.

    @author by ak
    """
    log = log or (lambda _m: None)
    status_fn = status_fn or (lambda _m: None)
    prompt_fn = prompt_fn or (lambda _m: None)
    stop_fn = stop_fn or (lambda: False)
    poll = max(0.005, float(poll_s))
    out: dict = {
        "ok": False,
        "method": METHOD_HAND_PROBE,
        "channel": "hand_probe",
        "note": "",
        "error": "",
        "before": {},
        "after": {},
        "perform_before": {},
        "perform_after": {},
        "seq_before": {},
        "seq_after": {},
        "diff_watch": {},
        "changed_keys": [],
        "t_arm_s": None,
        "t_change_s": None,
        "paths": {},
        "natural_end": False,
    }

    def _status(msg: str) -> None:
        try:
            status_fn(msg)
        except Exception:
            pass
        log(msg)

    def _prompt(msg: str) -> None:
        try:
            prompt_fn(msg)
        except Exception:
            pass
        try:
            status_fn(msg)
        except Exception:
            pass

    t0 = time.perf_counter()
    _prompt("【手按采样】请先放一个技能（等出招）…")
    _status("hand_probe: 请放一个技能（等 type=4/busy）…")
    armed = None
    while time.perf_counter() - t0 < float(wait_s):
        if stop_fn():
            out["error"] = "stopped"
            return out
        w, cast, host = snapshot_cast(session)
        perf = snapshot_host_perform(session)
        if is_armed_cast(w, perf) and is_valid_cast_skill_id(w.get("+10")):
            armed = (w, cast, host, perf, time.perf_counter())
            break
        time.sleep(poll)
    if not armed:
        out["error"] = "timeout: no armed cast"
        _prompt(f"【手按采样失败】{out['error']}")
        _status(f"hand_probe FAIL: {out['error']}")
        return out

    w0, cast0, host0, perf0, t_arm = armed
    out["t_arm_s"] = t_arm - t0
    out["before"] = dict(w0 or {})
    out["perform_before"] = dict(perf0 or {})
    out["cast"] = int(cast0 or 0) & 0xFFFFFFFF
    out["host"] = int(host0 or 0) & 0xFFFFFFFF
    out["seq_before"] = snapshot_skill_sequence(
        session, cast_this=int(cast0 or 0), log=lambda _m: None
    )
    try:
        out["dump_before"] = cast_nonzero_dwords(session, int(cast0 or 0))
    except Exception as e:
        out["dump_before_err"] = str(e)

    # LOUD prompt — this is what the user looks at
    arm_msg = (
        "【现在手按】先按 X（清后摇）→ 再按 空格（清格挡）"
        " — 工具不注入任何键！有黄字后再按"
    )
    _prompt(arm_msg)
    _status(
        "hand_probe: 【请手按 X 再按空格】工具不注入 — 等打断（勿等技能自己放完）"
    )
    log(
        f"hand_probe ARMED id=0x{int((w0 or {}).get('+10') or 0):X} "
        f"+18=0x{int((w0 or {}).get('+18') or 0):X} busy={int((w0 or {}).get('+4A0') or 0)&1} "
        f"type={perf0.get('perform_type')} cast=0x{int(cast0 or 0):X} "
        f"total_ms={int((w0 or {}).get('+1C') or 0)}"
    )

    changed = False
    natural_end = False
    w1 = w0
    perf1 = perf0
    t_change = None
    t_wait0 = time.perf_counter()
    total0 = int((w0 or {}).get("+1C") or 0)
    id0 = int((w0 or {}).get("+10") or 0)
    p0 = int((perf0 or {}).get("perform_type") or 0)

    while time.perf_counter() - t_wait0 < float(after_s):
        if stop_fn():
            out["error"] = "stopped"
            break
        w1, cast1, host1 = snapshot_cast(session)
        perf1 = snapshot_host_perform(session)
        id1 = int((w1 or {}).get("+10") or 0)
        b0 = int((w0 or {}).get("+4A0") or 0) & 1
        b1 = int((w1 or {}).get("+4A0") or 0) & 1
        p1 = int((perf1 or {}).get("perform_type") or 0)
        elapsed1 = int((w1 or {}).get("+20") or 0)
        wall = time.perf_counter() - t_arm

        # Prefer block interrupt signature (X): type 4 -> 3
        if p0 == 4 and p1 == 3:
            changed = True
            t_change = time.perf_counter()
            out["cast"] = int(cast1 or cast0 or 0) & 0xFFFFFFFF
            out["interrupt_kind"] = "perform_4_to_3"
            break

        # Early identity clear while skill should still be running = interrupt
        if id0 and not id1:
            # natural end: roughly waited full cast time and type back to 2/base
            natural_like = (
                (total0 > 0 and wall >= (total0 / 1000.0) * 0.85)
                or (wall >= 1.6 and p1 in (0, 2) and not b1)
            )
            if natural_like and p1 != 3:
                natural_end = True
                t_change = time.perf_counter()
                out["cast"] = int(cast1 or cast0 or 0) & 0xFFFFFFFF
                out["interrupt_kind"] = "natural_end"
                break
            # early clear
            if wall < 1.5 or (total0 > 0 and wall < (total0 / 1000.0) * 0.75):
                changed = True
                t_change = time.perf_counter()
                out["cast"] = int(cast1 or cast0 or 0) & 0xFFFFFFFF
                out["interrupt_kind"] = "early_id_clear"
                break

        # busy drop mid-skill with id still / or new skill id
        if b0 and (not b1) and wall < 1.2 and p1 not in (0, 2):
            changed = True
            t_change = time.perf_counter()
            out["cast"] = int(cast1 or cast0 or 0) & 0xFFFFFFFF
            out["interrupt_kind"] = "busy_drop"
            break

        # keep prompting every ~0.8s
        if int(wall * 10) % 8 == 0:
            _prompt(
                f"【请手按 X→空格】已等 {wall:.1f}s  id=0x{id1:X} type={p1} +20={elapsed1}"
            )
        time.sleep(poll)

    out["after"] = dict(w1 or {})
    out["perform_after"] = dict(perf1 or {})
    out["seq_after"] = snapshot_skill_sequence(
        session, cast_this=int(out.get("cast") or 0), log=lambda _m: None
    )
    try:
        out["dump_after"] = cast_nonzero_dwords(session, int(out.get("cast") or 0))
    except Exception as e:
        out["dump_after_err"] = str(e)

    keys = set((w0 or {}).keys()) | set((w1 or {}).keys())
    diff = {}
    changed_keys = []
    for k in sorted(keys):
        a = int((w0 or {}).get(k) or 0)
        b = int((w1 or {}).get(k) or 0)
        if a != b:
            diff[k] = {"before": a, "after": b}
            changed_keys.append(k)
    out["diff_watch"] = diff
    out["changed_keys"] = changed_keys
    if t_change is not None:
        out["t_change_s"] = t_change - t_arm
    out["natural_end"] = bool(natural_end)

    # dump diff
    db = out.get("dump_before") or {}
    da = out.get("dump_after") or {}
    if isinstance(db, dict) and isinstance(da, dict):
        try:
            out["dump_diff"] = cast_dump_diff(db, da)
        except Exception:
            out["dump_diff"] = {}

    if natural_end and not changed:
        out["ok"] = False
        out["error"] = (
            "技能自己放完了(natural end)，没有采到 X 打断。"
            "请再试：黄字【现在手按】出现后立刻 X→空格"
        )
        _prompt(f"【采样失败】{out['error']}")
    elif changed:
        out["ok"] = True
        out["error"] = ""
        _prompt(
            f"【采样成功】{out.get('interrupt_kind')} "
            f"type {p0}->{int((perf1 or {}).get('perform_type') or 0)} "
            f"dt={out.get('t_change_s')}"
        )
    elif not out.get("error"):
        out["error"] = "timeout: 没等到 X/空格打断，请看到黄字后再按"
        _prompt(f"【采样超时】{out['error']}")

    out["note"] = (
        f"hand_probe ok={out['ok']} kind={out.get('interrupt_kind')} "
        f"natural={natural_end} keys={changed_keys[:16]} "
        f"id=0x{id0:X}->0x{int((w1 or {}).get('+10') or 0):X} "
        f"busy={int((w0 or {}).get('+4A0') or 0)&1}->{int((w1 or {}).get('+4A0') or 0)&1} "
        f"ptype={p0}->{int((perf1 or {}).get('perform_type') or 0)} "
        f"dt={out.get('t_change_s')}"
    )
    log(f"hand_probe RESULT {out['note']}")
    _status(out["note"])

    try:
        dest = _lab_dest()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        md = dest / f"lab_hand_probe_{stamp}.md"
        js = dest / f"lab_hand_probe_{stamp}.json"
        lines = [
            "# hand interrupt probe (no key inject)",
            "",
            f"note: {out['note']}",
            f"error: {out.get('error')}",
            f"interrupt_kind: {out.get('interrupt_kind')}",
            f"natural_end: {out.get('natural_end')}",
            f"t_arm_s: {out.get('t_arm_s')}",
            f"t_change_s: {out.get('t_change_s')}",
            "",
            "## watch before/after",
            f"before: {watch_summary(out.get('before'))}",
            f"after: {watch_summary(out.get('after'))}",
            f"perform_before: {_perform_summary(out.get('perform_before'))}",
            f"perform_after: {_perform_summary(out.get('perform_after'))}",
            f"seq_before: {skill_sequence_summary(out.get('seq_before'))}",
            f"seq_after: {skill_sequence_summary(out.get('seq_after'))}",
            "",
            "## changed watch keys",
        ]
        for k in changed_keys:
            d = diff[k]
            lines.append(f"- {k}: 0x{d['before']:X} -> 0x{d['after']:X}")
        lines += [
            "",
            "## dump_diff (nonzero)",
            str(out.get("dump_diff") or {}),
            "",
            "## interpretation",
            "- success kind perform_4_to_3 => X became block perform (generic path target).",
            "- natural_end => user did not press in time; re-run.",
            "- Next: map block skill id / 0x75F000 args / HostStop packet.",
            "",
        ]
        md.write_text("\n".join(lines), encoding="utf-8")
        js.write_text(
            json.dumps(out, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        out["paths"] = {"md": str(md), "json": str(js)}
        log(f"hand_probe report {md}")
    except Exception as e:
        out["report_err"] = str(e)

    return out



def run_recovery_suppress_loop(
    session,
    *,
    stop_fn: Callable[[], bool] | None = None,
    log: LogFn | None = None,
    hwnd: int = 0,
    poll_s: float = 0.010,
    idle_clean_polls: int = 8,
    min_full_s: float = 0.25,
    min_stop65_s: float = 0.12,
    min_strip_s: float = 0.030,
    min_onskill_s: float = 0.35,
    max_stop65_per_window: int = 8,
    status_fn: Callable[[str], None] | None = None,
    mode: str = "soft",
    key_min_wall_s: float = 0.38,
    key_min_elapsed_ms: int = 300,
    key_max_wait_s: float = 1.15,
) -> dict:
    """
    Continuous suppress loop (lab only).

    mode:
      - key (default v4.3): EDGE = background UI_KEY X (macro-equivalent
        interrupt via 0x75F000 path). Local WPM alone cannot recast.
      - soft: stop65 + Seq + strip (lab only; proven insufficient for recast).
      - full: lab_hoststop_bit1 (includes OnSkill identity-match).

    Rising edge requires valid skill id + (busy | type=4). Ghost 0xFFFFFFFF
    rejected. Logs untruncated POST_DIAG after each EDGE fire.
    """
    log = log or (lambda _m: None)
    status_fn = status_fn or (lambda _m: None)
    stop_fn = stop_fn or (lambda: False)
    poll = max(0.005, float(poll_s))
    need_clean = max(2, int(idle_clean_polls))
    cd_full = max(0.15, float(min_full_s))
    cd_stop = max(0.05, float(min_stop65_s))
    cd_strip = max(0.02, float(min_strip_s))
    cd_onskill = max(0.20, float(min_onskill_s))
    max_s65 = max(1, int(max_stop65_per_window))
    mode_s = str(mode or "key").strip().lower()
    if mode_s not in ("soft", "full", "key"):
        mode_s = "soft"
    key_wait_min = max(0.15, float(key_min_wall_s))
    key_elapsed_need = max(50, int(key_min_elapsed_ms))
    key_wait_max = max(key_wait_min + 0.1, float(key_max_wait_s))
    quiet = lambda _m: None

    stats = {
        "ok": True,
        "mode": mode_s,
        "version": "v4.2",
        "edges": 0,
        "full": 0,
        "soft": 0,
        "key": 0,
        "strips": 0,
        "stop65": 0,
        "onskills": 0,
        "gate_clears": 0,
        "polls": 0,
        "skipped_stop65_cd": 0,
        "skipped_stop65_cap": 0,
        "skipped_full_cd": 0,
        "last_note": "",
        "last_diag": "",
        "error": "",
        "stopped": False,
    }

    blocked0 = _unsafe_skill_write_block_reason(session)
    if blocked0:
        stats["ok"] = False
        stats["error"] = blocked0
        stats["stopped"] = True
        log(f"suppress_loop BLOCK at start: {blocked0}")
        try:
            status_fn(f"压制: 门禁 {blocked0}")
        except Exception:
            pass
        return stats

    in_window = False
    key_armed = False
    key_fired = False
    key_arm_t = 0.0
    key_saw_busy = False
    clean_streak = need_clean
    window_stop65 = 0
    last_stop65_t = 0.0
    last_onskill_t = 0.0
    last_strip_t = 0.0
    last_full_t = 0.0
    last_gate_t = 0.0
    last_status = ""
    last_progress_log_t = 0.0
    bridge = None

    def _set_status(msg: str) -> None:
        nonlocal last_status
        if msg != last_status:
            last_status = msg
            try:
                status_fn(msg)
            except Exception:
                pass

    def _now() -> float:
        return time.perf_counter()

    def _get_bridge():
        nonlocal bridge
        if bridge is not None:
            return bridge
        from app.core.xajh_bridge import ensure_bridge

        pid = int(getattr(session, "pid", 0) or 0)
        bridge = ensure_bridge(
            pid,
            log=log,
            inject_if_needed=True,
            hwnd=int(hwnd or 0) or None,
            force_reinject=False,
        )
        return bridge

    def _do_stop65(reason: str) -> bool:
        nonlocal last_stop65_t, window_stop65
        t = _now()
        if t - last_stop65_t < cd_stop:
            stats["skipped_stop65_cd"] += 1
            return False
        if window_stop65 >= max_s65:
            stats["skipped_stop65_cap"] += 1
            return False
        r = lab_stop_skill_perform(session, hwnd=hwnd, log=quiet)
        last_stop65_t = _now()
        window_stop65 += 1
        stats["stop65"] += 1
        stats["last_note"] = str(r.get("note") or reason)
        return bool(r.get("ok"))

    def _do_onskill() -> bool:
        nonlocal last_onskill_t
        if mode_s == "soft":
            return False
        t = _now()
        if t - last_onskill_t < cd_onskill:
            return False
        br = _get_bridge()
        if br is None:
            stats["error"] = "bridge unavailable"
            _set_status("压制: bridge 不可用")
            return False
        try:
            res = br.on_skill_stopped(
                hwnd=int(hwnd or 0) or None,
                timeout_ms=2000,
                mode=0,
            )
        except Exception as e:
            stats["error"] = str(e)
            log(f"suppress_loop onskill err: {e}")
            return False
        last_onskill_t = _now()
        stats["onskills"] += 1
        note = str(res.note or res.error or "")
        stats["last_note"] = note
        return bool(res.ok)

    def _do_strip() -> bool:
        """Identity/perform strip. soft v4.1 uses this to kill re-fly refill."""
        nonlocal last_strip_t
        t = _now()
        if t - last_strip_t < cd_strip:
            return False
        r = clear_cast_session(
            session,
            log=quiet,
            require_active=False,
            variant=CAST_CANCEL_PERFORM_STRIP,
        )
        last_strip_t = _now()
        stats["strips"] += 1
        return bool(r.get("ok"))

    def _do_unlink_cc(cast_this: int) -> bool:
        """Re-run 0x754BA0 if cast+0xCC refilled (soft anti re-fly)."""
        if mode_s != "soft":
            return False
        cast = int(cast_this or 0) & 0xFFFFFFFF
        pid = int(getattr(session, "pid", 0) or 0)
        if not cast or not pid:
            return False
        try:
            from app.core.remote_runtime import remote_call_thiscall_x86, remote_read_bytes

            raw = remote_read_bytes(pid, (cast + CAST_SKILL_OBJ_OFF) & 0xFFFFFFFF, 4)
            cc = int.from_bytes(raw[:4], "little") if raw and len(raw) >= 4 else 0
            if not cc or cc < 0x10000:
                return False
            remote_call_thiscall_x86(
                pid,
                int(VA_CLEAR_SKILL_OBJ) & 0xFFFFFFFF,
                cast,
                [],
                caller_cleanup=False,
                timeout_ms=1200,
            )
            stats["gate_clears"] += 1
            return True
        except Exception:
            return False

    def _do_gates(host_ptr: int, cast_this: int) -> None:
        nonlocal last_gate_t
        t = _now()
        if t - last_gate_t < 0.04:
            return
        try:
            lab_clear_host_4a4_flags(
                session, host_ptr=int(host_ptr or 0), log=quiet
            )
            lab_clear_cast_timer_block(
                session, cast_this=int(cast_this or 0), log=quiet
            )
            # keep CanCastNextUnit unlocked while residual seq lives
            lab_force_can_cast_next_unit(
                session, cast_this=int(cast_this or 0), log=quiet, mode="safe"
            )
            last_gate_t = _now()
            stats["gate_clears"] += 1
        except Exception:
            pass

    def _log_diag(fire: dict | None, cast_this: int = 0) -> None:
        try:
            diag = format_post_fire_diag(
                session, cast_this=int(cast_this or 0), fire=fire or {}, log=log
            )
            stats["last_diag"] = str(diag.get("line") or "")
        except Exception as e:
            log(f"suppress_loop POST_DIAG err: {e}")

    def _do_edge_fire(reason: str, *, force: bool = False) -> bool:
        """Rising-edge fire: soft_next or hoststop_bit1."""
        nonlocal last_full_t, last_stop65_t, last_onskill_t, last_strip_t, window_stop65
        t = _now()
        if (not force) and (t - last_full_t < cd_full):
            stats["skipped_full_cd"] += 1
            if mode_s == "key":
                return False  # never partial-stop skill while waiting recovery
            _do_stop65("edge_partial_" + reason)
            if mode_s == "full":
                _do_onskill()
                _do_strip()
            else:
                try:
                    lab_force_can_cast_next_unit(session, log=quiet, mode="safe")
                except Exception:
                    pass
            return False
        try:
            if mode_s == "key":
                r = lab_key_interrupt(session, hwnd=hwnd, log=log)
                stats["key"] += 1
                tag = "KEY"
            elif mode_s == "soft":
                r = lab_soft_next_unlock(session, hwnd=hwnd, log=log)
                stats["soft"] += 1
                tag = "SOFT"
            else:
                r = lab_hoststop_bit1(session, hwnd=hwnd, log=quiet)
                stats["full"] += 1
                stats["onskills"] += 1
                stats["strips"] += int(r.get("suppress_hits") or 0) + 1
                tag = "FULL"
            last_full_t = _now()
            last_stop65_t = last_full_t
            last_onskill_t = last_full_t
            last_strip_t = last_full_t
            window_stop65 = min(window_stop65 + 1, max_s65)
            stats["stop65"] += 1
            stats["last_note"] = str(r.get("note") or reason)
            note = str(r.get("note") or "")
            # Do not truncate below ~400 chars — need seq_unlock detail
            log(
                f"suppress_loop {tag}#{stats['soft'] + stats['full'] + stats['key']} "
                f"ok={r.get('ok')} mode={mode_s} note={note[:480]}"
            )
            cast_ptr = int(r.get("cast") or 0) & 0xFFFFFFFF
            if not cast_ptr:
                try:
                    _w, cast_ptr, _h = snapshot_cast(session, log=quiet)
                    cast_ptr = int(cast_ptr or 0) & 0xFFFFFFFF
                except Exception:
                    cast_ptr = 0
            if not r.get("post_diag"):
                _log_diag(r, cast_ptr)
            else:
                line = (r.get("post_diag") or {}).get("line")
                if line:
                    log(str(line))
                    stats["last_diag"] = str(line)
            return bool(r.get("ok"))
        except Exception as e:
            log(f"suppress_loop EDGE fire err: {e}")
            _do_stop65("edge_fb_stop65")
            if mode_s == "full":
                _do_onskill()
                _do_strip()
            last_full_t = _now()
            return False

    def _truly_idle(w, perf) -> bool:
        return (not cast_watch_dirty(w)) and (not perform_is_skillish(perf))

    def _should_engage(w, perf) -> bool:
        # Real cast only. Live 20260722: id=0xFFFFFFFF type=2 busy=0 false EDGE.
        idv = int(w.get("+10") or 0) & 0xFFFFFFFF
        if not is_valid_cast_skill_id(idv):
            return False
        busy = int(w.get("+4A0") or 0) & 1
        ptype = int((perf or {}).get("perform_type") or 0)
        if busy:
            return True
        # valid id + non-locomotion perform / armed helper
        if is_armed_cast(w, perf) and ptype != 2:
            return True
        return False

    edge_desc = {
        "key": "对照真键(非通用)",
        "soft": "soft:stop65+Seq+strip",
        "full": "HostStop+OnSkill+Seq",
    }.get(mode_s, mode_s)
    log(
        "suppress_loop START v4.7 mode=%s poll=%.0fms edge_cd=%.0fms stop65_cd=%.0fms "
        "strip_cd=%.0fms key_wait=%.0fms/+20≥%d — 点按; 键模式先出手再X+空格 (%s)"
        % (
            mode_s,
            poll * 1000.0,
            cd_full * 1000.0,
            cd_stop * 1000.0,
            cd_strip * 1000.0,
            (key_wait_min * 1000.0) if mode_s == "key" else 0.0,
            key_elapsed_need if mode_s == "key" else 0,
            edge_desc,
        )
    )
    _set_status(f"压制: ON v4.7/{mode_s} 等出招 ({edge_desc})")

    try:
        while not stop_fn():
            stats["polls"] += 1
            w, cast_this, host = snapshot_cast(session)
            perf = snapshot_host_perform(session)

            if _truly_idle(w, perf):
                clean_streak += 1
                if clean_streak >= need_clean:
                    if in_window:
                        in_window = False
                        window_stop65 = 0
                        key_armed = False
                        key_fired = False
                        key_saw_busy = False
                        log(
                            f"suppress_loop idle edges={stats['edges']} "
                            f"soft={stats['soft']} key={stats['key']} full={stats['full']} "
                            f"stop65={stats['stop65']} strip={stats['strips']}"
                        )
                    _set_status(
                        f"压制: ON 空闲 e={stats['edges']} "
                        f"soft={stats['soft']} full={stats['full']} "
                        f"s65={stats['stop65']}"
                    )
                time.sleep(poll)
                continue

            clean_streak = 0
            idv = int(w.get("+10") or 0)
            p18 = int(w.get("+18") or 0)
            busy = int(w.get("+4A0") or 0) & 1
            ptype = perf.get("perform_type")

            if not _should_engage(w, perf):
                _set_status(
                    f"压制: 见态未武装 id=0x{idv:X} type={ptype} 等busy/type4"
                )
                # full mode may strip residual id; soft does not wipe identity
                if mode_s == "full" and cast_watch_dirty(w):
                    _do_strip()
                elif mode_s == "soft":
                    _do_gates(host, cast_this)
                time.sleep(poll)
                continue

            # Rising edge — key mode only ARMs (do not cancel skill windup).
            if not in_window:
                in_window = True
                window_stop65 = 0
                key_armed = True
                key_fired = False
                key_arm_t = _now()
                key_saw_busy = bool(busy)
                stats["edges"] += 1
                log(
                    f"suppress_loop EDGE#{stats['edges']} "
                    f"id=0x{idv:X} +18=0x{p18:X} busy={busy} type={ptype} "
                    f"+20=0x{int(w.get('+20') or 0):X} cast=0x{int(cast_this or 0):X} "
                    f"mode={mode_s}"
                    + (" ARM(wait recovery)" if mode_s == "key" else " FIRE")
                )
                if mode_s == "key":
                    _set_status(
                        f"SUPPRESS ARM#{stats['edges']} id=0x{idv:X} "
                        f"wait>={int(key_wait_min*1000)}ms/+20>={key_elapsed_need}"
                    )
                    time.sleep(poll)
                    continue
                _set_status(
                    f"压制: {mode_s.upper()}拆#{stats['edges']} id=0x{idv:X} "
                    f"busy={busy} type={ptype}"
                )
                _do_edge_fire("edge", force=bool(busy) or ptype == 4)
                time.sleep(poll)
                continue

            # Key mode: wait until skill has progressed, then X+Space once.
            if mode_s == "key" and key_armed and (not key_fired):
                if busy:
                    key_saw_busy = True
                elapsed_ms = int(w.get("+20") or 0)
                total_ms = int(w.get("+1C") or 0)
                wall = _now() - key_arm_t
                # Recovery-like: had busy then free, or enough elapsed/wall.
                recovery_drop = bool(key_saw_busy and not busy and idv)
                late_elapsed = elapsed_ms >= key_elapsed_need
                late_wall = wall >= key_wait_min
                force_late = wall >= key_wait_max
                # If total known, also allow at ~35% cast time (after early windup).
                frac_ok = bool(total_ms > 0 and elapsed_ms >= max(key_elapsed_need, int(total_ms * 0.30)))
                ready = force_late or recovery_drop or (late_wall and (late_elapsed or frac_ok or key_saw_busy))
                if not ready:
                    _set_status(
                        f"SUPPRESS wait e={stats['edges']} wall={wall*1000:.0f}ms "
                        f"+20={elapsed_ms} busy={busy} type={ptype}"
                    )
                    time.sleep(poll)
                    continue
                log(
                    f"suppress_loop KEY_FIRE#{stats['edges']} "
                    f"wall={wall*1000:.0f}ms +20={elapsed_ms}/{total_ms} "
                    f"busy={busy} saw_busy={int(key_saw_busy)} "
                    f"recovery_drop={int(recovery_drop)} force_late={int(force_late)}"
                )
                _set_status(
                    f"SUPPRESS FIRE#{stats['edges']} +20={elapsed_ms} wall={wall*1000:.0f}ms"
                )
                _do_edge_fire("key_recovery", force=True)
                key_fired = True
                time.sleep(poll)
                continue

            # Hold window
            if mode_s == "key":
                # After interrupt, do NOT stop65 immediately (lets X/Space finish).
                # Only if skill id comes back strongly with type=4 long after fire.
                if key_fired and perform_is_skillish(perf) and busy:
                    # rare re-cast while still windowed — leave alone
                    pass
            else:
                if perform_is_skillish(perf):
                    _do_stop65("hold_skillish")
                if cast_watch_dirty(w) or int(w.get("+10") or 0):
                    _do_strip()
                    if mode_s == "full" and int(w.get("+10") or 0):
                        _do_onskill()
                _do_gates(host, cast_this)

            now = _now()
            if now - last_progress_log_t > 0.8:
                last_progress_log_t = now
                _set_status(
                    f"压制: 窗口中 e={stats['edges']} soft={stats['soft']} "
                    f"full={stats['full']} s65={stats['stop65']} "
                    f"type={ptype} id=0x{idv:X}"
                )
            time.sleep(poll)
    except Exception as e:
        stats["ok"] = False
        stats["error"] = str(e)
        log(f"suppress_loop ERR: {e}")
        _set_status(f"压制: 错误 {e}")
    finally:
        stats["stopped"] = True
        try:
            heal = lab_suppress_stop_heal(session, hwnd=hwnd, log=log)
            stats["heal"] = heal
        except Exception as e:
            stats["heal"] = {"ok": False, "error": str(e)}
            log(f"suppress_loop heal err: {e}")
        try:
            if bridge is not None:
                bridge.close()
        except Exception:
            pass
        bridge = None
        log(
            f"suppress_loop STOP mode={mode_s} edges={stats['edges']} "
            f"soft={stats['soft']} key={stats['key']} full={stats['full']} "
            f"onskill={stats['onskills']} stop65={stats['stop65']} "
            f"strip={stats['strips']} gate={stats['gate_clears']} "
            f"skip_edge={stats['skipped_full_cd']} polls={stats['polls']}"
        )
        log(
            "suppress_loop NOTE: 若关压制后仍放不出技能，请重登角色/重开客户端"
            "（v4.1 unit0 写坏技能表无法热修）"
        )
        _set_status(
            f"压制: OFF e={stats['edges']} key={stats['key']} soft={stats['soft']} "
            f"full={stats['full']} s65={stats['stop65']}"
        )
    return stats



def write_recovery_trial_report(trial: dict) -> dict[str, str]:
    """Write one trial markdown + json under .issues/lab."""
    dest = _lab_dest()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    method = str(trial.get("method") or "unknown")
    md_path = dest / f"lab_recovery_{method}_{stamp}.md"
    js_path = dest / f"lab_recovery_{method}_{stamp}.json"
    before = trial.get("before") or {}
    after = trial.get("after") or {}
    imm = trial.get("after_immediate") or {}
    score = trial.get("score") or {}
    fire = trial.get("fire") or {}
    native = fire.get("native") or {}

    lines = [
        f"# lab recovery trial {stamp}",
        "",
        f"method={method}",
        f"label={trial.get('label')!r}",
        f"ok={trial.get('ok')}",
        f"error={trial.get('error')!r}",
        f"cast_this=0x{int(trial.get('cast_this') or 0):X}",
        f"host=0x{int(trial.get('host_ptr') or 0):X}",
        f"wait_rounds={trial.get('wait_rounds')}",
        f"wait_phase={trial.get('wait_phase')!r}",
        f"edge_elapsed_s={trial.get('edge_elapsed_s')}",
        f"fire_dt_s={trial.get('fire_dt_s')}",
        f"hit_meta={trial.get('hit_meta')!r}",
        "",
        "## fire",
        "",
        f"channel={fire.get('channel')!r}",
        f"fire_ok={fire.get('ok')}",
        f"note={fire.get('note')!r}",
        f"fire_error={fire.get('error')!r}",
        f"warning={fire.get('warning')!r}",
        f"native_cleared={native.get('native_cleared')}",
        f"native_ids=({native.get('native_id0')!r},{native.get('native_id1')!r},{native.get('native_id2')!r})",
        "",
        "## host perform (type=4 skill-era / type=2 locomotion)",
        "",
        f"perform_summary={score.get('perform_summary')!r}",
        f"perform_cleared={score.get('perform_cleared')}",
        f"perform_type={score.get('perform_type_before')}->{score.get('perform_type_after')}",
        f"perform_skillish={score.get('perform_skillish_before')}->{score.get('perform_skillish_after')}",
        f"perform_gate={score.get('perform_gate_before')}->{score.get('perform_gate_after')}",
        f"perform_before={score.get('perform_before')!r}",
        f"perform_after={score.get('perform_after')!r}",
        "",
        "## host next-skill gates (0x75F000 / NO-CD)",
        "",
        f"gates_before={host_gates_summary(trial.get('host_gates_before') or {})}",
        f"gates_after={host_gates_summary(trial.get('host_gates_after') or {})}",
        f"gates_late={host_gates_summary(trial.get('host_gates_late') or {})}",
        f"bit1_before={score.get('host_bit1_before')} after={score.get('host_bit1_after')} late={score.get('host_bit1_late')}",
        f"lock587_before={(trial.get('host_gates_before') or {}).get('lock_587b00')} "
        f"after={(trial.get('host_gates_after') or {}).get('lock_587b00')} "
        f"late={(trial.get('host_gates_late') or {}).get('lock_587b00')}",
        f"raw_before={trial.get('host_gates_before')!r}",
        f"raw_after={trial.get('host_gates_after')!r}",
        f"raw_late={trial.get('host_gates_late')!r}",
        "",
        "## cast timer block (+4A4/+4A8/+8D4/+8D8..8DC)",
        "",
        f"timer_before={cast_timer_summary(trial.get('cast_timer_before') or {})}",
        f"timer_after={cast_timer_summary(trial.get('cast_timer_after') or {})}",
        f"timer_late={cast_timer_summary(trial.get('cast_timer_late') or {})}",
        f"raw_timer_before={trial.get('cast_timer_before')!r}",
        f"raw_timer_after={trial.get('cast_timer_after')!r}",
        f"raw_timer_late={trial.get('cast_timer_late')!r}",
        "",
        "## CECSkillSequence CanCastNextUnit (cast+0xCC)",
        "",
        f"seq_before={skill_sequence_summary(trial.get('seq_before') or {})}",
        f"seq_late={skill_sequence_summary(trial.get('seq_late') or {})}",
        f"raw_seq_before={trial.get('seq_before')!r}",
        f"raw_seq_late={trial.get('seq_late')!r}",
        f"seq_unlock={trial.get('fire', {}).get('seq_unlock') if isinstance(trial.get('fire'), dict) else trial.get('seq_unlock')!r}",
        "",
        "## cast slots 0x754300 (2B8 / 3A0)",
        "",
        f"slots_before={score.get('slots_before')!r}",
        f"slots_immediate={score.get('slots_immediate')!r}",
        f"slots_late={score.get('slots_late')!r}",
        "",
        "## cast nonzero dwords (full 0..0x4B0, lab)",
        "",
        f"nonzero_before_n={len(trial.get('cast_nonzero_before') or {})}",
        f"nonzero_after_n={len(trial.get('cast_nonzero_after') or {})}",
        f"nonzero_before={trial.get('cast_nonzero_before')!r}",
        f"nonzero_after={trial.get('cast_nonzero_after')!r}",
        f"nonzero_diff={trial.get('cast_nonzero_diff')!r}",
        "",
        "## score (local memory only)",
        "",
        f"verdict={score.get('verdict')!r}",
        f"id_cleared_immediate={score.get('id_cleared_immediate')}",
        f"id_cleared_late={score.get('id_cleared')}",
        f"id_reverted={score.get('id_reverted')}",
        f"id_refill_t_s={score.get('id_refill_t_s')}",
        f"refill_fields={score.get('refill_fields')!r}",
        f"busy_cleared={score.get('busy_cleared')}",
        f"busy_cleared_immediate={score.get('busy_cleared_immediate')}",
        f"stable_full_clear={score.get('stable_full_clear')}",
        f"flags_cleared={score.get('flags_cleared')}",
        f"still_active={score.get('still_active')}",
        f"immediate_verdict={score.get('immediate_verdict')!r}",
        f"late_verdict={score.get('late_verdict')!r}",
        f"native_vs_python={score.get('native_vs_python')!r}",
        "",
        "## before / after",
        "",
        f"before: {watch_summary(before)}",
        f"after_immediate: {watch_summary(imm)}",
        f"after_late:  {watch_summary(after)}",
        "",
        "| field | before | t0 | late |",
        "|-------|--------|----|------|",
    ]
    for key in _SERIES_KEYS:
        lines.append(
            f"| {key} | {_hx(before, key)} | {_hx(imm, key)} | {_hx(after, key)} |"
        )
    lines.extend(["", "## after series", ""])
    for fr in trial.get("after_series") or []:
        lines.append(
            f"- t={fr.get('t_s')}s elapsed={fr.get('t_elapsed_s')} "
            f"{watch_summary(fr.get('watch') or {})}"
        )
    lines.extend(
        [
            "",
            "## notes",
            "",
            "Local memory score only — does NOT prove recast/damage/animation.",
            "Do not promote to product until repeated gameplay evidence.",
            "If verdict=local_clear_then_id_refilled: next dig who rewrites +10/+18/+7C.",
            "If perform_cleared and type 4->2: driver stopped; watch whether id still refills.",
            "NO-CD: host 4A4 lock helps (no fly); still blocked next cast -> SkillSequence CanCastNextUnit.",
            "hoststop_bit1 = HostStop本地拆 + host门 + cast定时 + Seq解锁(+490/units) (lab).",
            "Seq gate: +488>=0 or active units with code 0x69/6b/6c/70/71 block next cast (0x582E00).",
            "cancel_session(0x21) is a negative control (kills whole cast).",
            "",
            "## one-line",
            "",
            (
                f"recovery {method}: {score.get('verdict')} "
                f"imm_id={score.get('id_cleared_immediate')} "
                f"late_id={score.get('id_cleared')} "
                f"reverted={score.get('id_reverted')} "
                f"refill_t={score.get('id_refill_t_s')} "
                f"busy={score.get('busy_cleared')} "
                f"native={score.get('native_cleared')} "
                f"perform_cleared={score.get('perform_cleared')} "
                f"ptype={score.get('perform_type_before')}->{score.get('perform_type_after')} "
                f"fire_ok={fire.get('ok')}"
            ),
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    # JSON: drop huge raw dumps if any; keep structured trial.
    payload = dict(trial)
    try:
        js_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        js_path = None
    return {
        "md": str(md_path),
        "json": str(js_path) if js_path else "",
    }


def write_recovery_matrix_report(matrix: dict) -> dict[str, str]:
    """Write matrix summary md+json under .issues/lab."""
    dest = _lab_dest()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    md_path = dest / f"lab_recovery_matrix_{stamp}.md"
    js_path = dest / f"lab_recovery_matrix_{stamp}.json"
    lines = [
        f"# lab recovery matrix {stamp}",
        "",
        f"ok={matrix.get('ok')}",
        f"error={matrix.get('error')!r}",
        "",
        "| method | verdict | id_imm | id_late | reverted | refill_t | busy | native | fire_ok | error |",
        "|--------|---------|--------|---------|----------|----------|------|--------|---------|-------|",
    ]
    for row in matrix.get("summary") or []:
        lines.append(
            f"| {row.get('method')} | {row.get('verdict')} | "
            f"{row.get('id_cleared_immediate')} | {row.get('id_cleared')} | "
            f"{row.get('id_reverted')} | {row.get('id_refill_t_s')} | "
            f"{row.get('busy_cleared')} | {row.get('native_cleared')} | "
            f"{row.get('fire_ok')} | {row.get('error')!r} |"
        )
    lines.extend(["", "## per-trial reports", ""])
    for t in matrix.get("trials") or []:
        lines.append(
            f"- {t.get('method')}: {(t.get('score') or {}).get('verdict')} "
            f"-> {t.get('report_path')}"
        )
    lines.extend(
        [
            "",
            "## next research",
            "",
            "If OnSkillStopped shows local_clear_then_id_refilled:",
            "1) note id_refill_t_s / refill_fields",
            "2) static/dynamic who writes cast+0x10/+0x18/+0x7C after stop",
            "3) only then design suppress-refill or correct stop path",
            "",
            "## integration rule",
            "",
            "Only promote a method if: local_full_clear_stable AND recast ok AND no fly "
            "AND damage ok, repeated. 0x21 remains negative control. Lab first.",
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    try:
        # Compact matrix json without full after_series of every trial if huge —
        # keep trials as-is for research.
        js_path.write_text(
            json.dumps(matrix, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        js_path = None
    return {
        "md": str(md_path),
        "json": str(js_path) if js_path else "",
    }
