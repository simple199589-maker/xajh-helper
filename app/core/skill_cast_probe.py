# -*- coding: utf-8 -*-
"""
Skill cast-this probe for action-lock research.

Lab 002719 showed skill/seq shells static; host only moved.
Notes: skill cast this lives at [host_side + 0x1A88], cast entry 0x755F10.

This module resolves cast-this, dumps it, and burst-polls for changes.
Does NOT call 0x755F10 to force cast.
Does NOT CRT-call note helpers 0x4AEFA0/0x4AEFB0 (they crash off main thread).

Hot path (open-cast poll): pure RPM via GetHostSide chain + short host cache.
Lab path may still CRT GetHostPlayer once when RPM host is empty.

@author by ak
"""
from __future__ import annotations

import os
import struct
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from app.core.remote_runtime import remote_read_bytes, remote_write_bytes
from app.core.combat_probe import (
    CombatProbeSample,
    _changed_dword_offs,
    _hex_preview,
    _plausible_heap_ptrs,
    format_changed_dwords_detail,
    format_dword_table,
    format_sample_line,
    sample_combat_probe,
)
from app.core.plg_exports import (
    CAST_FIELD_EXTRA_FLAGS_OFF,
    CAST_FIELD_SKILL_ID_B_OFF,
    CAST_FIELD_SKILL_ID_OFF,
    DEFAULT_IMAGE_BASE,
    HOST_SKILL_THIS_OFF,
    HOST_SIDE_INV_OFF,
    NOTE_VA_SKILL_CAST,
)

LogFn = Callable[[str], None]

# Cover cast+0x4A0 (bit3 after 75F000) and session block +0x1F8..+0x210.
CAST_THIS_DUMP_SIZE = 0x500
HOST_TAIL_OFF = 0x1A00  # dump window covering +0x1A84 / +0x1A88
HOST_TAIL_SIZE = 0x100
NEST_SIZE = 0x100
NEST_MAX = 6

# GetHostSide@0x4AE400: *[game_root]+0x24 then +0x8C (no CRT).
NOTE_VA_GAME_ROOT_GLOBAL = 0x015282D8
HOST_SIDE_MID_OFF = 0x24
HOST_SIDE_LEAF_OFF = 0x8C
# Reuse host/cast pointers across open-cast polls (avoid CRT storm).
_HOST_CACHE_TTL_S = 2.5
_host_cache_lock = threading.Lock()
# pid -> (deadline, host_ptr, cast_this, source)
_host_cache: dict[int, tuple[float, int, int, str]] = {}

# Key dword offs on cast-this for action-lock timeline.
# Lab 004320/tl: +10 id, +18/+1C cfg, +20 elapsed, +7C phase.
# Gate static (CAST_GATE_ANALYSIS): +80 id-B, +4A0 flags, +1F8/+200/+20C session SM.
# Gate static 2026-07-21 (NEXT_SKILL_GATE_DEEP):
# cast+0x2B8 / +0x3A0 sub-slots checked by 0x754300:
#   busy iff [slot+8]!=0 && [slot+0x2C]!=-1
# offsets on cast-this: 2B8+8=2C0, 2B8+2C=2E4; 3A0+8=3A8, 3A0+2C=3CC.
CAST_WATCH_OFFS = (
    0x010,
    0x014,
    0x018,
    0x01C,
    0x020,
    0x024,
    0x060,
    0x064,
    0x068,
    0x06C,
    0x070,
    0x074,
    0x078,
    0x07C,
    0x080,
    0x1F8,
    0x1FC,
    0x200,
    0x204,
    0x208,
    0x20C,
    0x210,
    0x2B8,
    0x2C0,  # slotA +8
    0x2E4,  # slotA +0x2C
    0x3A0,
    0x3A8,  # slotB +8
    0x3CC,  # slotB +0x2C
    0x4A0,
)

# host_tail (+1A00) offs that are pose/noise; ignore for burst HIT.
TAIL_POSE_OFFS = frozenset({0x020, 0x024, 0x028, 0x02C})

CAST_CANCEL_BUSY_BIT = "busy_bit"
CAST_CANCEL_GATE_MINIMAL = "gate_minimal"
CAST_CANCEL_PERFORM_STRIP = "perform_strip"
CAST_CANCEL_VARIANTS = frozenset(
    {CAST_CANCEL_BUSY_BIT, CAST_CANCEL_GATE_MINIMAL, CAST_CANCEL_PERFORM_STRIP}
)


def _unsafe_skill_write_block_reason(session) -> str | None:
    """Require an explicit dev-only opt-in and an exact known client build."""
    from app.core.build_profile import is_dev_build, _ensure_dotenv
    from app.core.client_build import profile_for_client

    # Source runs: pick up repo .env (XAJH_ENABLE_UNSAFE_SKILL_WRITE=1).
    try:
        _ensure_dotenv()
    except Exception:
        pass

    if not is_dev_build():
        return "skill memory write is unavailable in production"
    if str(os.environ.get("XAJH_ENABLE_UNSAFE_SKILL_WRITE") or "").strip() != "1":
        return "set XAJH_ENABLE_UNSAFE_SKILL_WRITE=1 for this lab operation"
    exe_path = str(getattr(session, "exe_path", None) or "").strip()
    profile = profile_for_client(exe_path) if exe_path else None
    if profile is None or not profile.capabilities.supports("skill.probe"):
        return "unknown client build; exact skill.probe profile required"
    return None


@dataclass
class CastThisProbe:
    """Resolved skill cast-this and related dumps. @author by ak"""

    ok: bool
    host_ptr: int | None = None
    host_side_a: int | None = None  # unused; helpers disabled (crash-safe)
    host_side_b: int | None = None  # unused; helpers disabled (crash-safe)
    cast_this_from_host: int | None = None  # [host+0x1A88]
    cast_this_from_side: int | None = None  # unused when helpers off
    inv_this: int | None = None  # [host+0x1A84]
    cast_this: int | None = None  # preferred non-zero
    cast_dump: bytes = field(default_factory=bytes, repr=False)
    host_tail: bytes = field(default_factory=bytes, repr=False)
    nest_dumps: dict = field(default_factory=dict, repr=False)
    cast_dump_hex: str = ""
    note: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("cast_dump", None)
        d.pop("host_tail", None)
        d.pop("nest_dumps", None)
        d["cast_n"] = len(self.cast_dump)
        d["tail_n"] = len(self.host_tail)
        d["nest_keys"] = list((self.nest_dumps or {}).keys())
        return d


def _u32(session, addr: int) -> int | None:
    """Read one u32 from remote; None on fail. @author by ak"""
    if not addr:
        return None
    try:
        raw = remote_read_bytes(int(session.pid), int(addr) & 0xFFFFFFFF, 4)
        if len(raw) < 4:
            return None
        return struct.unpack("<I", raw)[0]
    except Exception:
        return None


def _is_heap_like(p: int | None) -> bool:
    """True if value looks like userland heap ptr. @author by ak"""
    if not p:
        return False
    return 0x01000000 <= int(p) <= 0x7FFE0000


def _note_live(session, note_va: int) -> int:
    """preferred-base note VA -> live VA via module_base. @author by ak"""
    base = int(getattr(session, "module_base", 0) or 0)
    return int(base) + (int(note_va) - int(DEFAULT_IMAGE_BASE))


def resolve_host_side_rpm(session) -> int:
    """
    Resolve GetHostSide object via pure RPM (no CreateRemoteThread).

    Chain matches 0x4AE400: *[0x15282D8] -> +0x24 -> +0x8C.
    Returns 0 on any null/unreadable step.
    @author by ak
    """
    if not getattr(session, "pid", None) or not getattr(session, "module_base", None):
        return 0
    try:
        root = _u32(session, _note_live(session, NOTE_VA_GAME_ROOT_GLOBAL))
        if not root:
            return 0
        mid = _u32(session, int(root) + HOST_SIDE_MID_OFF)
        if not mid:
            return 0
        host = _u32(session, int(mid) + HOST_SIDE_LEAF_OFF)
        return int(host or 0) if _is_heap_like(host) else 0
    except Exception:
        return 0


def _cache_get(pid: int) -> tuple[int, int, str] | None:
    """Return (host, cast_this, source) if cache still valid. @author by ak"""
    now = time.time()
    with _host_cache_lock:
        hit = _host_cache.get(int(pid))
        if not hit:
            return None
        deadline, host, cast, src = hit
        if now > float(deadline) or not host:
            _host_cache.pop(int(pid), None)
            return None
        return int(host), int(cast or 0), str(src)


def _cache_put(pid: int, host: int, cast: int, source: str) -> None:
    """Store host/cast for short TTL. @author by ak"""
    if not host:
        return
    with _host_cache_lock:
        _host_cache[int(pid)] = (
            time.time() + float(_HOST_CACHE_TTL_S),
            int(host) & 0xFFFFFFFF,
            int(cast or 0) & 0xFFFFFFFF,
            str(source or ""),
        )


def clear_cast_host_cache(pid: int | None = None) -> None:
    """Drop cached host/cast (map change / reattach). @author by ak"""
    with _host_cache_lock:
        if pid is None:
            _host_cache.clear()
        else:
            _host_cache.pop(int(pid), None)


def resolve_cast_this(
    session,
    *,
    log: LogFn | None = None,
    with_dumps: bool = True,
    allow_crt: bool = False,
) -> CastThisProbe:
    """
    Resolve skill cast-this via host+0x1A88 (RPM-first).

    Hot path (loot open-cast poll, allow_crt=False):
      1) short host/cast cache
      2) pure RPM GetHostSide chain (*global+0x24+0x8C)
      3) RPM [host+0x1A88] / [host+0x1A84]
      Never CreateRemoteThread — CRT during MatterInteract hangs the client.

    Lab path (with_dumps / allow_crt=True): may fall back to GetHostPlayer CRT
    once if RPM host is empty.

    @author by ak
    """
    log = log or (lambda _m: None)
    out = CastThisProbe(ok=False)
    if not getattr(session, "pid", None) or not getattr(session, "module_base", None):
        out.error = "no attach session"
        return out

    pid = int(session.pid)
    host = 0
    source = ""

    cached = _cache_get(pid)
    if cached is not None:
        host, cached_cast, source = cached
        out.host_ptr = host
        out.note = f"host-cache:{source}; "
        if cached_cast and _is_heap_like(cached_cast):
            # Re-read cast fields only; host pointer reused.
            out.cast_this_from_host = _u32(session, host + HOST_SKILL_THIS_OFF)
            out.inv_this = _u32(session, host + HOST_SIDE_INV_OFF)
            live_cast = out.cast_this_from_host
            if _is_heap_like(live_cast):
                out.cast_this = int(live_cast)
            elif _is_heap_like(out.inv_this):
                out.cast_this = int(out.inv_this or 0)
            else:
                out.cast_this = int(cached_cast)
            out.ok = bool(out.cast_this)
            if out.ok and not with_dumps:
                _cache_put(pid, host, int(out.cast_this or 0), source)
                return out

    if not host:
        host = resolve_host_side_rpm(session)
        if host:
            source = "rpm-hostside"
            out.host_ptr = host
            out.note = "host:rpm-GetHostSide; "

    # Optional CRT fallback only for lab / explicit allow (never open-cast poll).
    if not host and (allow_crt or with_dumps):
        try:
            base_sample = sample_combat_probe(
                session, log=lambda _m: None, with_pos=False, with_dumps=False
            )
            host = int(base_sample.host_ptr or 0)
            if host:
                source = "crt-GetHostPlayer"
                out.host_ptr = host
                out.note = "host:crt-GetHostPlayer; "
        except Exception as e:
            out.error = f"host:{e}"
            log(out.error)
            return out

    host = out.host_ptr or 0
    if not host:
        out.error = "host null (RPM GetHostSide empty; CRT disabled on hot path)"
        log(out.error)
        return out

    # pure RPM — no note-helper CRT
    out.cast_this_from_host = _u32(session, host + HOST_SKILL_THIS_OFF)
    out.inv_this = _u32(session, host + HOST_SIDE_INV_OFF)
    out.host_side_a = host if source.startswith("rpm") else None
    out.host_side_b = None
    out.cast_this_from_side = out.cast_this_from_host
    log(
        f"cast-this host=0x{host:X} src={source or '?'} "
        f"[+1A88]=0x{(out.cast_this_from_host or 0):X} "
        f"[+1A84]=0x{(out.inv_this or 0):X}"
    )

    # prefer skill this at +0x1A88; inv at +0x1A84 is fallback for dump only
    for cand in (out.cast_this_from_host, out.inv_this):
        if _is_heap_like(cand):
            out.cast_this = cand
            break

    if not out.cast_this:
        out.error = "cast_this is null (host+0x1A88 empty?)"
        out.note += "try in-combat or after equip skills; helpers disabled (crash-safe)"
        log(out.error)
        if with_dumps:
            try:
                out.host_tail = remote_read_bytes(
                    int(session.pid), host + HOST_TAIL_OFF, HOST_TAIL_SIZE
                )
                log(
                    "host tail @+1A00:\n"
                    + format_dword_table(out.host_tail, max_rows=16)
                )
            except Exception as e:
                log(f"host_tail err={e}")
        return out

    out.ok = True
    _cache_put(pid, host, int(out.cast_this or 0), source or "ok")
    if with_dumps:
        try:
            out.cast_dump = remote_read_bytes(pid, out.cast_this, CAST_THIS_DUMP_SIZE)
            out.cast_dump_hex = _hex_preview(out.cast_dump, 96)
            out.host_tail = remote_read_bytes(
                pid, host + HOST_TAIL_OFF, HOST_TAIL_SIZE
            )
            nest = {}
            for off, p in _plausible_heap_ptrs(out.cast_dump, max_n=NEST_MAX):
                key = f"cast+{off:X}->{p:X}"
                try:
                    nest[key] = remote_read_bytes(pid, p, NEST_SIZE)
                except Exception:
                    nest[key] = b""
            out.nest_dumps = nest
            log(
                f"cast dump n={len(out.cast_dump)} nest={len(nest)} "
                f"hex={out.cast_dump_hex}"
            )
            log("cast dwords:\n" + format_dword_table(out.cast_dump, max_rows=20))
            if out.host_tail:
                log(
                    "host tail @+1A00:\n"
                    + format_dword_table(out.host_tail, max_rows=16)
                )
        except Exception as e:
            out.note += f"dump:{e}; "
            log(f"cast dump err={e}")
    return out


def _u32_at(data: bytes, off: int) -> int | None:
    """Read little-endian u32 at off; None if OOB. @author by ak"""
    if not data or off < 0 or off + 4 > len(data):
        return None
    return struct.unpack_from("<I", data, off)[0]


def extract_cast_watch(cast_dump: bytes) -> dict[str, int]:
    """
    Snapshot watched cast-this dwords as hex-keyed map.

    @author by ak
    """
    out: dict[str, int] = {}
    for off in CAST_WATCH_OFFS:
        v = _u32_at(cast_dump or b"", off)
        if v is not None:
            out[f"+{off:X}"] = int(v)
    return out


def _watch_is_active(watch: dict) -> bool:
    """True if cast session looks busy. @author by ak"""
    return bool(
        int(watch.get("+10") or 0)
        or int(watch.get("+4A0") or 0)
        or int(watch.get("+7C") or 0)
        or int(watch.get("+80") or 0)
    )


def clear_cast_session(
    session,
    *,
    log: LogFn | None = None,
    require_active: bool = True,
    variant: str = CAST_CANCEL_BUSY_BIT,
) -> dict:
    """
    Apply one controlled action-lock write experiment (lab only).

    busy_bit (default): clear only +0x4A0 bit0, preserving bits 1/3/etc.
    gate_minimal: busy_bit plus +0x10/+0x80 ids and +0x24 low byte.
    perform_strip: gate_minimal plus +14/+18/+1C/+20 and perform +60..+7C.

    If require_active and session already idle, refuse (avoids no-op tests).
    Does not call cast functions or touch timing/config fields. May still desync
    animation/server state; restart the client after the experiment.
    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "cast_this": 0,
        "before": {},
        "after": {},
        "wrote": [],
        "variant": str(variant or ""),
        "skipped_idle": False,
        "error": None,
    }
    blocked = _unsafe_skill_write_block_reason(session)
    if blocked:
        out["error"] = blocked
        log(f"clear_cast blocked: {blocked}")
        return out
    mode = str(variant or "").strip().lower()
    if mode not in CAST_CANCEL_VARIANTS:
        out["error"] = f"unsupported variant: {variant!r}"
        return out
    cur = resolve_cast_this(session, log=lambda _m: None, with_dumps=True)
    if not cur.ok or not cur.cast_this:
        out["error"] = cur.error or "cast_this null"
        log(f"clear_cast: {out['error']}")
        return out
    cast = int(cur.cast_this)
    out["cast_this"] = cast
    out["before"] = extract_cast_watch(cur.cast_dump or b"")
    if require_active and not _watch_is_active(out["before"]):
        out["skipped_idle"] = True
        out["error"] = (
            "already idle (id=0 4a0=0) — cast first, click during recovery"
        )
        log(f"clear_cast SKIP: {out['error']}")
        return out
    pid = int(session.pid)
    try:
        old_4a0 = int(out["before"].get("+4A0") or 0)
        new_4a0 = old_4a0 & ~1
        remote_write_bytes(
            pid,
            cast + CAST_FIELD_EXTRA_FLAGS_OFF,
            struct.pack("<I", new_4a0),
        )
        out["wrote"].append(f"+4A0=0x{old_4a0:X}->0x{new_4a0:X}")

        if mode in (CAST_CANCEL_GATE_MINIMAL, CAST_CANCEL_PERFORM_STRIP):
            zero4 = b"\x00\x00\x00\x00"
            for off in (CAST_FIELD_SKILL_ID_OFF, CAST_FIELD_SKILL_ID_B_OFF):
                old = int(out["before"].get(f"+{off:X}") or 0)
                remote_write_bytes(pid, cast + int(off), zero4)
                out["wrote"].append(f"+{off:X}=0x{old:X}->0")
            # +0x24 low byte mirrors busy; preserve the other three bytes.
            raw24 = remote_read_bytes(pid, cast + 0x24, 4)
            if len(raw24) == 4:
                new24 = bytes([0, raw24[1], raw24[2], raw24[3]])
                remote_write_bytes(pid, cast + 0x24, new24)
                out["wrote"].append(
                    f"+24={raw24.hex()}->{new24.hex()}"
                )
            # Also clear +18 identity tail and perform cluster that
            # OnPerformSkill@0x761330 / SetCurActiveSkill@0x758DF0 touch.
            if mode == CAST_CANCEL_PERFORM_STRIP:
                for off in (
                    0x14,
                    0x18,
                    0x1C,
                    0x20,
                    0x60,
                    0x64,
                    0x68,
                    0x6C,
                    0x70,
                    0x74,
                    0x78,
                    0x7C,
                ):
                    old = int(out["before"].get(f"+{off:X}") or 0)
                    remote_write_bytes(pid, cast + int(off), zero4)
                    if old:
                        out["wrote"].append(f"+{off:X}=0x{old:X}->0")
        after = resolve_cast_this(session, log=lambda _m: None, with_dumps=True)
        out["after"] = extract_cast_watch(after.cast_dump or b"")
        out["ok"] = True
        log(
            f"clear_cast ok variant={mode} cast=0x{cast:X} "
            f"before_id=0x{out['before'].get('+10', 0):X} "
            f"after_id=0x{out['after'].get('+10', 0):X} "
            f"before_4a0=0x{out['before'].get('+4A0', 0):X} "
            f"after_4a0=0x{out['after'].get('+4A0', 0):X}"
        )
    except Exception as e:
        out["error"] = str(e)
        log(f"clear_cast err={e}")
    return out


def clear_cast_session_when_active(
    session,
    *,
    log: LogFn | None = None,
    rounds: int = 80,
    interval_s: float = 0.05,
    variant: str = CAST_CANCEL_BUSY_BIT,
) -> dict:
    """
    Poll until cast session is active, then immediately clear it.

    Lab helper: click, then cast skill within ~4s; no need to time manually.
    @author by ak
    """
    log = log or (lambda _m: None)
    log(
        f"clear_when_active: wait busy up to {rounds * interval_s:.1f}s then WPM clear"
    )
    for i in range(int(rounds)):
        cur = resolve_cast_this(session, log=lambda _m: None, with_dumps=True)
        watch = extract_cast_watch(cur.cast_dump or b"")
        # +0x10 is loaded before the actual busy bit. Waiting for generic
        # activity can produce a successful 0->0 write during that short gap.
        busy_armed = bool(int(watch.get("+4A0") or 0) & 1)
        if cur.ok and busy_armed:
            log(
                f"clear_when_active HIT[{i+1}] id=0x{watch.get('+10', 0):X} "
                f"4a0=0x{watch.get('+4A0', 0):X} flags=0x{watch.get('+7C', 0):X}"
            )
            r = clear_cast_session(
                session,
                log=log,
                require_active=False,
                variant=variant,
            )
            r["wait_rounds"] = i + 1
            return r
        if i == 0 or (i + 1) % 10 == 0:
            log(f"clear_when_active wait[{i+1}/{rounds}] busy_bit=0")
        time.sleep(float(interval_s))
    out = {
        "ok": False,
        "error": "timeout: no active cast (id/4a0 stayed 0)",
        "wait_rounds": rounds,
        "before": {},
        "after": {},
        "wrote": [],
    }
    log(f"clear_when_active: {out['error']}")
    return out


def diff_cast_probes(a: CastThisProbe, b: CastThisProbe) -> dict:
    """
    Diff two cast-this probes; return summary dict.

    HIT signal for action-lock: cast object dword changes only.
    Tail pose / host-facing nest are logged but not treated as cast HIT.

    @author by ak
    """
    out = {
        "cast_changed_offs": [],
        "tail_changed_offs": [],
        "tail_pose_offs": [],
        "tail_other_offs": [],
        "nest_changed": {},
        "note": "",
        "cast_this_same": a.cast_this == b.cast_this,
        "cast_hit": False,
        "watch_a": extract_cast_watch(a.cast_dump or b""),
        "watch_b": extract_cast_watch(b.cast_dump or b""),
    }
    if a.cast_dump and b.cast_dump:
        out["cast_changed_offs"] = _changed_dword_offs(a.cast_dump, b.cast_dump)
    if a.host_tail and b.host_tail:
        tail = _changed_dword_offs(a.host_tail, b.host_tail)
        out["tail_changed_offs"] = tail
        out["tail_pose_offs"] = [o for o in tail if o in TAIL_POSE_OFFS]
        out["tail_other_offs"] = [o for o in tail if o not in TAIL_POSE_OFFS]
    na, nb = a.nest_dumps or {}, b.nest_dumps or {}
    for k in sorted(set(na) & set(nb)):
        if na[k] and nb[k]:
            offs = _changed_dword_offs(na[k], nb[k])
            if offs:
                out["nest_changed"][k] = offs
    # Cast HIT: cast body or cast_this ptr changed (ignore pose-only tail/nest).
    out["cast_hit"] = bool(out["cast_changed_offs"]) or not out["cast_this_same"]
    bits = []
    if out["cast_changed_offs"]:
        bits.append(
            "castΔ["
            + ",".join(f"+{o:X}" for o in out["cast_changed_offs"][:12])
            + "]"
        )
    if out["tail_other_offs"]:
        bits.append(
            "tailΔ["
            + ",".join(f"+{o:X}" for o in out["tail_other_offs"][:8])
            + "]"
        )
    elif out["tail_pose_offs"]:
        bits.append(
            "poseΔ["
            + ",".join(f"+{o:X}" for o in out["tail_pose_offs"][:6])
            + "]"
        )
    if out["nest_changed"]:
        bits.append("nestΔ=" + ",".join(list(out["nest_changed"].keys())[:3]))
    if a.cast_this != b.cast_this:
        bits.append(
            f"cast_this A=0x{(a.cast_this or 0):X} B=0x{(b.cast_this or 0):X}"
        )
    out["note"] = " ".join(bits) if bits else "no cast-this/tail/nest delta"
    return out


def burst_cast_this(
    session,
    baseline: CastThisProbe,
    *,
    log: LogFn | None = None,
    rounds: int = 48,
    interval_s: float = 0.05,
) -> tuple[CastThisProbe | None, dict | None]:
    """
    Poll cast-this until cast object changes vs baseline.

    Ignores host_tail pose-only noise (004751 false HIT).
    Default ~2.4s window (48 * 0.05).

    @author by ak
    """
    log = log or (lambda _m: None)
    log(
        f"cast burst rounds={rounds} interval={interval_s} "
        f"(HIT=cast object only, ignore pose)"
    )
    last_note = ""
    for i in range(int(rounds)):
        cur = resolve_cast_this(session, log=lambda _m: None, with_dumps=True)
        d = diff_cast_probes(baseline, cur)
        last_note = d["note"]
        log(f"cast_burst[{i+1}/{rounds}] {d['note']}")
        if d.get("cast_hit"):
            log(f"cast burst HIT: {d['note']}")
            return cur, d
        time.sleep(float(interval_s))
    log(f"cast burst: no cast-object change (last={last_note})")
    return None, None


def timeline_cast_this(
    session,
    *,
    log: LogFn | None = None,
    rounds: int = 60,
    interval_s: float = 0.05,
) -> list[dict]:
    """
    Sample cast watch fields for ~rounds*interval_s seconds.

    Each frame includes skill_id/flags plus session SM (+1F8/+20C) and +4A0.
    Does not early-exit; captures full window for gate analysis.

    @author by ak
    """
    log = log or (lambda _m: None)
    log(f"cast timeline rounds={rounds} interval={interval_s}")
    t0 = time.time()
    frames: list[dict] = []
    for i in range(int(rounds)):
        cur = resolve_cast_this(session, log=lambda _m: None, with_dumps=True)
        dump = cur.cast_dump or b""
        watch = extract_cast_watch(dump)
        fr = {
            "i": i + 1,
            "t": round(time.time() - t0, 3),
            "cast_this": cur.cast_this or 0,
            "ok": bool(cur.ok),
            "watch": watch,
            "skill_id": watch.get("+10", 0),
            "skill_id_b": watch.get("+80", 0),
            "flags": watch.get("+7C", 0),
            "extra_4a0": watch.get("+4A0", 0),
            "sess_20c": watch.get("+20C", 0),
            "sess_200": watch.get("+200", 0),
            "ms_018": watch.get("+18", 0),
            "ms_01C": watch.get("+1C", 0),
            "ms_020": watch.get("+20", 0),
            "ms_070": watch.get("+70", 0),
            "ms_078": watch.get("+78", 0),
        }
        frames.append(fr)
        if fr["skill_id"] or fr["flags"] or fr["sess_20c"] or fr["sess_200"]:
            log(
                f"tl[{fr['i']}/{rounds}] t={fr['t']:.2f}s "
                f"id=0x{fr['skill_id']:X}/0x{fr['skill_id_b']:X} "
                f"flags=0x{fr['flags']:X} 4a0=0x{fr['extra_4a0']:X} "
                f"sm20c=0x{fr['sess_20c']:X} sm200=0x{fr['sess_200']:X} "
                f"ms={fr['ms_018']}/{fr['ms_01C']}/{fr['ms_020']}/"
                f"{fr['ms_070']}/{fr['ms_078']}"
            )
        else:
            if i == 0 or (i + 1) % 10 == 0:
                log(f"tl[{fr['i']}/{rounds}] t={fr['t']:.2f}s idle(cast clear)")
        time.sleep(float(interval_s))
    active = sum(
        1
        for f in frames
        if f.get("skill_id") or f.get("flags") or f.get("sess_20c") or f.get("sess_200")
    )
    log(f"cast timeline done frames={len(frames)} active={active}")
    return frames


def write_cast_timeline_report(frames: list[dict]) -> str:
    """Write cast timeline under .issues/lab. @author by ak"""
    from common.paths import app_root

    dest = app_root() / ".issues" / "lab"
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = dest / f"lab_cast_tl_{stamp}.md"
    lines = [
        f"# lab cast-this timeline {stamp}",
        "",
        f"frames={len(frames)} dump_watch includes +80/+1F8..+210/+4A0",
        "",
        "i | t(s) | id | idB | flags | 4A0 | +20C | +200 | +18 | +1C | +20 | +70 | +78",
        "---|------|----|-----|-------|-----|------|------|-----|-----|-----|-----|----",
    ]
    for fr in frames:
        w = fr.get("watch") or {}
        lines.append(
            f"{fr.get('i')} | {fr.get('t')} | "
            f"0x{int(fr.get('skill_id') or 0):X} | "
            f"0x{int(fr.get('skill_id_b') or 0):X} | "
            f"0x{int(fr.get('flags') or 0):X} | "
            f"0x{int(fr.get('extra_4a0') or 0):X} | "
            f"0x{int(fr.get('sess_20c') or 0):X} | "
            f"0x{int(fr.get('sess_200') or 0):X} | "
            f"{w.get('+18', 0)} | {w.get('+1C', 0)} | {w.get('+20', 0)} | "
            f"{w.get('+70', 0)} | {w.get('+78', 0)}"
        )
    lines.extend(["", "## active frames", ""])

    def _is_active(fr: dict) -> bool:
        return bool(
            fr.get("skill_id")
            or fr.get("flags")
            or fr.get("sess_20c")
            or fr.get("sess_200")
            or fr.get("skill_id_b")
        )

    for fr in frames:
        if _is_active(fr):
            w = fr.get("watch") or {}
            lines.append(
                f"- t={fr.get('t')}s id=0x{int(fr.get('skill_id') or 0):X} "
                f"idB=0x{int(fr.get('skill_id_b') or 0):X} "
                f"flags=0x{int(fr.get('flags') or 0):X} "
                f"4a0=0x{int(fr.get('extra_4a0') or 0):X} "
                f"20c=0x{int(fr.get('sess_20c') or 0):X} "
                f"watch={{{', '.join(f'{k}=0x{v:X}' for k, v in w.items() if v)}}}"
            )
    if not any(_is_active(fr) for fr in frames):
        lines.append("(no active frames — cast window missed or fields wrong)")
    lines.extend(
        [
            "",
            "## notes",
            "",
            "+10/+80: skill id slots (0x53D120 gate).",
            "+7C: phase flags (timeline).",
            "+4A0: extra flags (bit3 tested after 0x75F000).",
            "+1F8..+210: session SM block (0x755E00 / 0x756340).",
            "+18/+1C: config constants; +20: elapsed (not remaining).",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def write_onskill_stopped_report(
    *,
    before: dict | None,
    after: dict | None,
    bridge_ok: bool,
    bridge_note: str = "",
    bridge_error: str = "",
    cast_this: int = 0,
    host_ptr: int = 0,
    recast_ok: bool | None = None,
    damage_ok: bool | None = None,
    anim_note: str = "",
    extra_log: list[str] | None = None,
) -> str:
    """
    Write OnSkillStopped lab result under .issues/lab for copy-back.

    Includes before/after cast watch (with +0x14 identity dword), expected
    identity tuple, whether +0x10 cleared, native diagnostic note, and
    optional user-observed recast/damage notes.

    @author by ak
    """
    from common.paths import app_root

    dest = app_root() / ".issues" / "lab"
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = dest / f"lab_onskill_stopped_{stamp}.md"
    bw = before or {}
    aw = after or {}

    def _hx(m: dict, key: str) -> str:
        return f"0x{int(m.get(key) or 0):X}"

    def _u(m: dict, key: str) -> int:
        return int(m.get(key) or 0)

    id_tuple = (_u(bw, "+10"), _u(bw, "+14"), _u(bw, "+18"))
    id_cleared = bool(bridge_ok) and _u(bw, "+10") != 0 and _u(aw, "+10") == 0
    lines = [
        f"# lab OnSkillStopped {stamp}",
        "",
        f"bridge_ok={bridge_ok}",
        f"bridge_note={bridge_note!r}",
        f"bridge_error={bridge_error!r}",
        f"cast_this=0x{int(cast_this or 0):X}",
        f"host=0x{int(host_ptr or 0):X}",
        f"expected_identity=({_hx(bw, '+10')}, {_hx(bw, '+14')}, {_hx(bw, '+18')})",
        f"identity_tuple={id_tuple!r}",
        f"identity_block_cleared={id_cleared}",
        f"native_diagnostic={bridge_note!r}",
        "",
        "## before / after watch",
        "",
        "| field | before | after |",
        "|-------|--------|-------|",
        f"| +10 skill_id | {_hx(bw, '+10')} | {_hx(aw, '+10')} |",
        f"| +14 identity_b | {_hx(bw, '+14')} | {_hx(aw, '+14')} |",
        f"| +18 identity_c | {_hx(bw, '+18')} | {_hx(aw, '+18')} |",
        f"| +80 skill_id_b | {_hx(bw, '+80')} | {_hx(aw, '+80')} |",
        f"| +4A0 busy | {_hx(bw, '+4A0')} | {_hx(aw, '+4A0')} |",
        f"| +7C flags | {_hx(bw, '+7C')} | {_hx(aw, '+7C')} |",
        f"| +20 elapsed | {_hx(bw, '+20')} | {_hx(aw, '+20')} |",
        f"| +1C | {_hx(bw, '+1C')} | {_hx(aw, '+1C')} |",
        f"| +24 | {_hx(bw, '+24')} | {_hx(aw, '+24')} |",
        f"| +70 | {_hx(bw, '+70')} | {_hx(aw, '+70')} |",
        f"| +78 | {_hx(bw, '+78')} | {_hx(aw, '+78')} |",
        f"| +20C | {_hx(bw, '+20C')} | {_hx(aw, '+20C')} |",
        f"| +200 | {_hx(bw, '+200')} | {_hx(aw, '+200')} |",
        "",
        "## one-line copy",
        "",
        (
            f"onskill_stopped identity-match: before id={_hx(bw, '+10')} "
            f"+14={_hx(bw, '+14')} +18={_hx(bw, '+18')} 4a0={_hx(bw, '+4A0')} "
            f"-> after id={_hx(aw, '+10')} 4a0={_hx(aw, '+4A0')} "
            f"cleared={id_cleared} ok={bridge_ok} note={bridge_note!r}"
        ),
        "",
        "## user observation (fill if known)",
        "",
        f"recast_ok={recast_ok!r}  # True if same skill can cast again immediately",
        f"damage_ok={damage_ok!r}  # True if damage still settles / no server reject",
        f"anim_note={anim_note!r}",
        "",
        "## notes",
        "",
        "CMD_ONSKILL_STOPPED (identity-match): DLL snapshots cast+0x10/+0x14/+0x18",
        "into a zeroed event buffer, then calls 0x75F750 with thin-wrapper scalars",
        "(event, 0, 0, 0, 1). 0x582350 compares those three dwords to clear the",
        "identity block; a zeroed event buffer previously left +0x10 intact.",
        "Local mutating flush only — no 0x21 packet. Restart client after each experiment.",
        "",
    ]
    if extra_log:
        lines.extend(["## log", ""])
        lines.extend(str(x) for x in extra_log)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def write_cast_capture_report(
    a: CastThisProbe,
    b: CastThisProbe,
    d: dict,
    *,
    combat_a: CombatProbeSample | None = None,
    combat_b: CombatProbeSample | None = None,
) -> str:
    """Write cast-this A/B report under .issues/lab. @author by ak"""
    from common.paths import app_root

    dest = app_root() / ".issues" / "lab"
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = dest / f"lab_cast_{stamp}.md"
    lines = [
        f"# lab cast-this {stamp}",
        "",
        f"A cast_this=0x{(a.cast_this or 0):X} host=0x{(a.host_ptr or 0):X} "
        f"sideA=0x{(a.host_side_a or 0):X}",
        f"B cast_this=0x{(b.cast_this or 0):X} host=0x{(b.host_ptr or 0):X}",
        "",
        "## diff",
        "",
        d.get("note", ""),
        "",
    ]
    if combat_a:
        lines.append(f"combat A: {format_sample_line(combat_a)}")
    if combat_b:
        lines.append(f"combat B: {format_sample_line(combat_b)}")
    lines.append("")
    if d.get("cast_changed_offs") and a.cast_dump and b.cast_dump:
        lines.append(
            format_changed_dwords_detail(
                a.cast_dump, b.cast_dump, d["cast_changed_offs"], label="cast-this"
            )
        )
        lines.append("")
    if d.get("tail_changed_offs") and a.host_tail and b.host_tail:
        lines.append(
            format_changed_dwords_detail(
                a.host_tail,
                b.host_tail,
                d["tail_changed_offs"],
                label="host_tail(+1A00)",
            )
        )
        lines.append("")
    for title, ptr, data, hx in (
        ("A cast-this", a.cast_this, a.cast_dump, a.cast_dump_hex),
        ("B cast-this", b.cast_this, b.cast_dump, b.cast_dump_hex),
        ("A host tail +1A00", (a.host_ptr or 0) + HOST_TAIL_OFF, a.host_tail, ""),
        ("B host tail +1A00", (b.host_ptr or 0) + HOST_TAIL_OFF, b.host_tail, ""),
    ):
        lines.extend(
            [
                f"## {title}",
                "",
                f"ptr=0x{(ptr or 0):X} len={len(data)}",
                f"hex: {hx or (data[:64].hex() if data else '')}",
                "```",
                format_dword_table(data, max_rows=40),
                "```",
                "",
            ]
        )
    if d.get("nest_changed"):
        lines.append("## nest diffs")
        lines.append("")
        na, nb = a.nest_dumps or {}, b.nest_dumps or {}
        for k, offs in d["nest_changed"].items():
            lines.append(
                format_changed_dwords_detail(
                    na.get(k, b""), nb.get(k, b""), offs, label=f"nest {k}"
                )
            )
            lines.append("")
    lines.extend(
        [
            "## notes",
            "",
            f"skill cast VA (do not auto-call): 0x{NOTE_VA_SKILL_CAST:X}",
            f"HOST_SKILL_THIS_OFF=+0x{HOST_SKILL_THIS_OFF:X}",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
