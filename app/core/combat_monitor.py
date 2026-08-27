# -*- coding: utf-8 -*-
"""Read-only bridge access to the native dungeon combat activity counters.

The bridge arms passive detours on the host's SkillActionRequest and the
per-target damage splitter. Both are build-pinned (fixed-RVA), so an
unsupported client build or a failed hook install reports resolved=False and
callers must fail closed (no dungeon 纯站街 unstick).

Counters returned to Python (monotonic while armed):
  attack_seq        - self-initiated attack/skill action sequence number.
  self_damage_seq   - count of positive damage settlements attributed to the
                      host character (self-source filtered).
  self_damage_total - cumulative self-dealt damage (diagnostic only).

@author by ak
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

LogFn = Callable[[str], None]

# note: "COMBAT_MONITOR a=1 d=2 total=3 t=00000001:00000002 r=1"
_COMBAT_NOTE_RE = re.compile(
    r"COMBAT_MONITOR\s+a=(?P<a>-?\d+)"
    r"\s+d=(?P<d>-?\d+)"
    r"\s+total=(?P<total>-?\d+)"
    r"\s+t=(?P<thi>[0-9A-Fa-f]{8}):(?P<tlo>[0-9A-Fa-f]{8})"
    r"\s+r=(?P<r>-?\d+)"
)


@dataclass
class CombatActivity:
    """One snapshot of the native combat activity counters. @author by ak"""

    attack_seq: int = 0
    self_damage_seq: int = 0
    self_damage_total: int = 0
    host_target_lo: int = 0
    host_target_hi: int = 0
    resolved: bool = False
    ok: bool = False
    error: str = ""

    @property
    def host_target(self) -> int:
        return (int(self.host_target_hi) << 32) | (int(self.host_target_lo) & 0xFFFFFFFF)

    def to_dict(self) -> dict:
        return {
            "attack_seq": int(self.attack_seq),
            "self_damage_seq": int(self.self_damage_seq),
            "self_damage_total": int(self.self_damage_total),
            "host_target_lo": int(self.host_target_lo),
            "host_target_hi": int(self.host_target_hi),
            "host_target": self.host_target,
            "resolved": bool(self.resolved),
            "ok": bool(self.ok),
            "error": self.error,
        }


def parse_combat_monitor_note(note: str) -> CombatActivity | None:
    """Parse the native COMBAT_MONITOR status string. @author by ak"""
    m = _COMBAT_NOTE_RE.search(str(note or ""))
    if not m:
        return None
    return CombatActivity(
        attack_seq=int(m.group("a")),
        self_damage_seq=int(m.group("d")),
        self_damage_total=int(m.group("total")),
        host_target_lo=int(m.group("tlo"), 16),
        host_target_hi=int(m.group("thi"), 16),
        resolved=bool(int(m.group("r"))),
        ok=True,
    )


def _call_combat_monitor(
    session,
    *,
    mode: int,
    log: LogFn | None = None,
) -> CombatActivity:
    log = log or (lambda _m: None)
    try:
        from app.core.xajh_bridge import ensure_bridge

        br = ensure_bridge(
            int(getattr(session, "pid", 0) or 0),
            log=log,
            inject_if_needed=False,
            hwnd=int(getattr(session, "hwnd", 0) or 0) or None,
        )
        if br is None:
            return CombatActivity(ok=False, error="bridge not ready")
        try:
            res = br.combat_monitor(mode=int(mode), timeout_ms=1500)
        finally:
            try:
                br.close()
            except Exception:
                pass
    except Exception as e:
        return CombatActivity(ok=False, error=str(e))

    if not res.ok:
        return CombatActivity(
            ok=False,
            error=str(res.error or "") or f"status={res.status}",
            resolved=False,
        )
    parsed = parse_combat_monitor_note(res.note)
    if parsed is None:
        return CombatActivity(ok=False, error="combat monitor note unparsable")
    return parsed


def arm_combat_monitor(session, *, log: LogFn | None = None) -> CombatActivity:
    """Install/reset the native combat counters (mode=1). @author by ak"""
    return _call_combat_monitor(session, mode=1, log=log)


def read_combat_activity(session, *, log: LogFn | None = None) -> CombatActivity:
    """Read the current native combat counters (mode=2). @author by ak"""
    return _call_combat_monitor(session, mode=2, log=log)


def disarm_combat_monitor(session, *, log: LogFn | None = None) -> CombatActivity:
    """Stop counting while leaving the passive detours installed (mode=0). @author by ak"""
    return _call_combat_monitor(session, mode=0, log=log)
