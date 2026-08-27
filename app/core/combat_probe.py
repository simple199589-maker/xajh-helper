# -*- coding: utf-8 -*-
"""
Dev-only combat probes: host states, skill pointers, position, memory dump.

Read-only remote plg calls for knockback / skill action-lock research.
Does NOT install permanent suppress hooks.

@author by ak
"""
from __future__ import annotations

import struct
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from app.core.automove import (
    remote_call_cdecl_x86,
    remote_call_cdecl_x86_ret64,
    remote_read_bytes,
)
from app.core.plg_exports import (
    EXPORT_GET_GAME_STATE,
    EXPORT_GET_HOST_PLAYER,
    EXPORT_GET_OBJECT_I64_STATES,
    EXPORT_GET_USER_SKILL,
    EXPORT_GET_USER_SKILL_SEQUENCE,
    EXPORT_IS_HOST_PLAYER_DEAD,
    find_xajh_exe,
    resolve_export_rva,
)

LogFn = Callable[[str], None]

# Default dump size for skill / sequence / host objects (research window).
SKILL_DUMP_SIZE = 0x100
SEQ_DUMP_SIZE = 0x200
HOST_DUMP_SIZE = 0x200
# Nested pointer dumps (chase first N plausible heap ptrs from skill/seq).
NEST_DUMP_SIZE = 0x80
NEST_MAX = 4


@dataclass
class CombatProbeSample:
    """One snapshot of host combat-related probes. @author by ak"""

    ok: bool
    ts: float = 0.0
    game_state: int | None = None
    host_ptr: int | None = None
    host_states: int | None = None
    host_states_hex: str = ""
    skill_ptr: int | None = None
    skill_seq_ptr: int | None = None
    host_dead: bool | None = None
    pos: tuple[float, float, float] | None = None
    scene_id: int | None = None
    skill_dump: bytes = field(default_factory=bytes, repr=False)
    seq_dump: bytes = field(default_factory=bytes, repr=False)
    host_dump: bytes = field(default_factory=bytes, repr=False)
    nest_dumps: dict = field(default_factory=dict, repr=False)
    skill_dump_hex: str = ""
    seq_dump_hex: str = ""
    host_dump_hex: str = ""
    error: str | None = None
    note: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.pos is not None:
            d["pos"] = list(self.pos)
        # keep hex, drop raw bytes for json friendliness
        d.pop("skill_dump", None)
        d.pop("seq_dump", None)
        d.pop("host_dump", None)
        d.pop("nest_dumps", None)
        return d


@dataclass
class CombatProbeDiff:
    """Diff of two samples (e.g. before/after knock or cast). @author by ak"""

    ok: bool
    a: dict = field(default_factory=dict)
    b: dict = field(default_factory=dict)
    states_changed: bool = False
    states_xor: int | None = None
    pos_delta: tuple[float, float, float] | None = None
    skill_changed_offs: list[int] = field(default_factory=list)
    seq_changed_offs: list[int] = field(default_factory=list)
    host_changed_offs: list[int] = field(default_factory=list)
    nest_changed: dict = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.pos_delta is not None:
            d["pos_delta"] = list(self.pos_delta)
        return d


def _resolve_va(session, export_name: str) -> int:
    """Resolve live VA for a plg export. @author by ak"""
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


def _call_i32(session, export_name: str, args: list | None = None) -> int:
    """Remote cdecl call returning EAX as signed i32. @author by ak"""
    va = _resolve_va(session, export_name)
    return int(remote_call_cdecl_x86(int(session.pid), va, list(args or [])))


def _call_ptr(session, export_name: str, args: list | None = None) -> int:
    """Remote cdecl call returning pointer in EAX. @author by ak"""
    return _call_i32(session, export_name, args) & 0xFFFFFFFF


def _call_i64(session, export_name: str, args: list | None = None) -> int:
    """Remote cdecl call returning EDX:EAX as u64. @author by ak"""
    va = _resolve_va(session, export_name)
    return int(remote_call_cdecl_x86_ret64(int(session.pid), va, list(args or [])))


def _hex_preview(data: bytes, max_bytes: int = 64) -> str:
    """Compact hex for UI. @author by ak"""
    if not data:
        return ""
    chunk = data[:max_bytes]
    h = chunk.hex()
    spaced = " ".join(h[i : i + 8] for i in range(0, len(h), 8))
    if len(data) > max_bytes:
        spaced += f" …(+{len(data) - max_bytes}B)"
    return spaced


def _changed_dword_offs(a: bytes, b: bytes) -> list[int]:
    """Offsets of 4-byte words that differ. @author by ak"""
    n = min(len(a), len(b))
    out: list[int] = []
    for off in range(0, n - 3, 4):
        if a[off : off + 4] != b[off : off + 4]:
            out.append(off)
    return out


def _plausible_heap_ptrs(data: bytes, *, max_n: int = NEST_MAX) -> list[tuple[int, int]]:
    """
    Collect (offset, ptr) for values that look like userland heap pointers.

    Heuristic: 0x01000000 .. 0x7FFE0000, aligned, not all-ASCII-looking only.
    @author by ak
    """
    found: list[tuple[int, int]] = []
    seen: set[int] = set()
    for off in range(0, len(data) - 3, 4):
        p = struct.unpack_from("<I", data, off)[0]
        if p < 0x01000000 or p > 0x7FFE0000:
            continue
        if p & 3:
            continue
        if p in seen:
            continue
        seen.add(p)
        found.append((off, p))
        if len(found) >= max_n:
            break
    return found


def format_dword_table(data: bytes, *, max_rows: int = 24) -> str:
    """
    Format memory as off | u32 | i32 | float lines for lab UI.

    @author by ak
    """
    if not data:
        return "(empty)"
    lines = ["off   u32        i32         float"]
    rows = 0
    for off in range(0, len(data) - 3, 4):
        raw = data[off : off + 4]
        u = struct.unpack("<I", raw)[0]
        i = struct.unpack("<i", raw)[0]
        f = struct.unpack("<f", raw)[0]
        f_s = f"{f:.3f}" if abs(f) < 1e6 else "n/a"
        lines.append(f"+{off:03X}  {u:08X}  {i:11d}  {f_s}")
        rows += 1
        if rows >= max_rows:
            lines.append(f"... ({len(data)} bytes total)")
            break
    return "\n".join(lines)


def sample_combat_probe(
    session,
    *,
    log: LogFn | None = None,
    with_pos: bool = True,
    with_dumps: bool = False,
    skill_dump_size: int = SKILL_DUMP_SIZE,
    seq_dump_size: int = SEQ_DUMP_SIZE,
) -> CombatProbeSample:
    """
    Snapshot host game_state / i64 states / skill ptrs / optional pos / dumps.

    Safe-ish CRT into plg exports (same path as automove). Prefer short bursts.
    @author by ak
    """
    log = log or (lambda _m: None)
    ts = time.time()
    if not getattr(session, "pid", None) or not getattr(session, "module_base", None):
        return CombatProbeSample(ok=False, ts=ts, error="no attach session")

    out = CombatProbeSample(ok=True, ts=ts)
    pid = int(session.pid)
    try:
        out.game_state = _call_i32(session, EXPORT_GET_GAME_STATE)
    except Exception as e:
        log(f"probe GetGameState err={e}")
        out.note += f"game_state:{e}; "

    try:
        host = _call_ptr(session, EXPORT_GET_HOST_PLAYER)
        out.host_ptr = host if host else None
        if host:
            st = _call_i64(session, EXPORT_GET_OBJECT_I64_STATES, [host])
            out.host_states = int(st) & ((1 << 64) - 1)
            out.host_states_hex = f"0x{out.host_states:016X}"
    except Exception as e:
        log(f"probe host/states err={e}")
        out.note += f"host:{e}; "

    try:
        out.skill_ptr = _call_ptr(session, EXPORT_GET_USER_SKILL) or None
        if out.skill_ptr == 0:
            out.skill_ptr = None
    except Exception as e:
        log(f"probe GetUserSkill err={e}")
        out.note += f"skill:{e}; "

    try:
        # Export name is misspelled Senquence in the binary.
        sp = _call_ptr(session, EXPORT_GET_USER_SKILL_SEQUENCE)
        out.skill_seq_ptr = sp if sp else None
    except Exception as e:
        log(f"probe GetUserSkillSequence err={e}")
        out.note += f"seq:{e}; "

    try:
        dead = _call_i32(session, EXPORT_IS_HOST_PLAYER_DEAD) & 0xFF
        out.host_dead = bool(dead)
    except Exception as e:
        log(f"probe IsHostPlayerDead err={e}")
        out.note += f"dead:{e}; "

    if with_pos:
        try:
            from app.core.automove import read_scene_position

            pos_r = read_scene_position(session, log=log)
            if getattr(pos_r, "ok", False):
                out.scene_id = getattr(pos_r, "scene_id", None)
                sp = getattr(pos_r, "scene_pos", None)
                if sp is not None and len(sp) >= 3:
                    out.pos = (float(sp[0]), float(sp[1]), float(sp[2]))
        except Exception as e:
            log(f"probe pos err={e}")
            out.note += f"pos:{e}; "

    if with_dumps:
        try:
            if out.host_ptr:
                out.host_dump = remote_read_bytes(pid, out.host_ptr, HOST_DUMP_SIZE)
                out.host_dump_hex = _hex_preview(out.host_dump, max_bytes=96)
            if out.skill_ptr:
                out.skill_dump = remote_read_bytes(pid, out.skill_ptr, skill_dump_size)
                out.skill_dump_hex = _hex_preview(out.skill_dump, max_bytes=96)
            if out.skill_seq_ptr:
                out.seq_dump = remote_read_bytes(pid, out.skill_seq_ptr, seq_dump_size)
                out.seq_dump_hex = _hex_preview(out.seq_dump, max_bytes=96)
            # Chase nested ptrs from skill + seq (likely active unit / play info).
            nest: dict[str, bytes] = {}
            for tag, blob in (("skill", out.skill_dump), ("seq", out.seq_dump)):
                for off, p in _plausible_heap_ptrs(blob, max_n=NEST_MAX):
                    key = f"{tag}+{off:X}->{p:X}"
                    try:
                        nest[key] = remote_read_bytes(pid, p, NEST_DUMP_SIZE)
                    except Exception:
                        nest[key] = b""
            out.nest_dumps = nest
        except Exception as e:
            log(f"probe dump err={e}")
            out.note += f"dump:{e}; "

    if out.host_ptr is None and out.game_state is None and out.error is None:
        if out.note:
            out.ok = False
            out.error = out.note.strip("; ")
    log(
        f"probe ok={out.ok} gs={out.game_state} host=0x{(out.host_ptr or 0):X} "
        f"states={out.host_states_hex or '-'} skill=0x{(out.skill_ptr or 0):X} "
        f"seq=0x{(out.skill_seq_ptr or 0):X} dead={out.host_dead} "
        f"pos={out.pos} scene={out.scene_id} dumps={with_dumps} "
        f"skill_n={len(out.skill_dump)} seq_n={len(out.seq_dump)} "
        f"host_n={len(out.host_dump)} nest={len(out.nest_dumps)}"
    )
    if with_dumps:
        if out.skill_dump:
            log(f"probe skill_hex {out.skill_dump_hex}")
            log("probe skill_dwords:\n" + format_dword_table(out.skill_dump, max_rows=16))
        else:
            log("probe skill_hex (empty)")
        if out.seq_dump:
            log(f"probe seq_hex {out.seq_dump_hex}")
            log("probe seq_dwords:\n" + format_dword_table(out.seq_dump, max_rows=20))
        else:
            log("probe seq_hex (empty)")
        if out.host_dump:
            log(f"probe host_hex {out.host_dump_hex}")
            log("probe host_dwords:\n" + format_dword_table(out.host_dump, max_rows=16))
        for k, blob in (out.nest_dumps or {}).items():
            if blob:
                log(f"probe nest {k} n={len(blob)} hex={_hex_preview(blob, 48)}")
    return out


def diff_samples(a: CombatProbeSample, b: CombatProbeSample) -> CombatProbeDiff:
    """Compare two samples for state/pos/dump changes. @author by ak"""
    d = CombatProbeDiff(ok=bool(a.ok and b.ok), a=a.to_dict(), b=b.to_dict())
    if a.host_states is not None and b.host_states is not None:
        d.states_xor = int(a.host_states) ^ int(b.host_states)
        d.states_changed = d.states_xor != 0
    if a.pos is not None and b.pos is not None:
        d.pos_delta = (
            float(b.pos[0]) - float(a.pos[0]),
            float(b.pos[1]) - float(a.pos[1]),
            float(b.pos[2]) - float(a.pos[2]),
        )
    if a.skill_dump and b.skill_dump:
        d.skill_changed_offs = _changed_dword_offs(a.skill_dump, b.skill_dump)
    if a.seq_dump and b.seq_dump:
        d.seq_changed_offs = _changed_dword_offs(a.seq_dump, b.seq_dump)
    if a.host_dump and b.host_dump:
        d.host_changed_offs = _changed_dword_offs(a.host_dump, b.host_dump)
    # nest keys present in both
    na, nb = a.nest_dumps or {}, b.nest_dumps or {}
    for k in sorted(set(na) & set(nb)):
        if na[k] and nb[k]:
            offs = _changed_dword_offs(na[k], nb[k])
            if offs:
                d.nest_changed[k] = offs

    bits = []
    if d.states_changed:
        bits.append(f"states_xor=0x{d.states_xor:016X}")
    if d.pos_delta is not None:
        dx, dy, dz = d.pos_delta
        bits.append(f"dpos=({dx:.3f},{dy:.3f},{dz:.3f})")
    if d.skill_changed_offs:
        offs = ",".join(f"+{o:X}" for o in d.skill_changed_offs[:12])
        bits.append(f"skillΔ[{offs}]")
    if d.seq_changed_offs:
        offs = ",".join(f"+{o:X}" for o in d.seq_changed_offs[:12])
        bits.append(f"seqΔ[{offs}]")
    if d.host_changed_offs:
        offs = ",".join(f"+{o:X}" for o in d.host_changed_offs[:12])
        bits.append(f"hostΔ[{offs}]")
    if d.nest_changed:
        bits.append("nestΔ=" + ",".join(d.nest_changed.keys()))
    d.note = " ".join(bits) if bits else "no state/pos/dump delta"
    return d


def format_sample_line(s: CombatProbeSample) -> str:
    """One-line human summary. @author by ak"""
    pos = "-"
    if s.pos is not None:
        pos = f"({s.pos[0]:.2f},{s.pos[1]:.2f},{s.pos[2]:.2f})"
    return (
        f"gs={s.game_state} host=0x{(s.host_ptr or 0):X} "
        f"states={s.host_states_hex or '-'} "
        f"skill=0x{(s.skill_ptr or 0):X} seq=0x{(s.skill_seq_ptr or 0):X} "
        f"dead={s.host_dead} pos={pos} scene={s.scene_id}"
        + (f" err={s.error}" if s.error else "")
    )


def format_dump_diff_note(d: CombatProbeDiff) -> str:
    """Extra multi-line note for skill/seq/host memory diffs. @author by ak"""
    lines = [d.note]
    if d.skill_changed_offs:
        lines.append(
            "skill changed dwords @ "
            + ", ".join(f"+0x{o:X}" for o in d.skill_changed_offs[:20])
        )
    if d.seq_changed_offs:
        lines.append(
            "seq changed dwords @ "
            + ", ".join(f"+0x{o:X}" for o in d.seq_changed_offs[:20])
        )
    if d.host_changed_offs:
        lines.append(
            "host changed dwords @ "
            + ", ".join(f"+0x{o:X}" for o in d.host_changed_offs[:20])
        )
    if d.nest_changed:
        for k, offs in d.nest_changed.items():
            lines.append(
                f"nest {k} changed @ "
                + ", ".join(f"+0x{o:X}" for o in offs[:12])
            )
    if (
        not d.skill_changed_offs
        and not d.seq_changed_offs
        and not d.host_changed_offs
        and not d.nest_changed
    ):
        lines.append(
            "skill/seq/host/nest: no dword change — likely missed cast window, "
            "or action lock lives on another object (use 连采施法)"
        )
    return "\n".join(lines)


def burst_sample_until_change(
    session,
    baseline: CombatProbeSample,
    *,
    log: LogFn | None = None,
    rounds: int = 20,
    interval_s: float = 0.08,
) -> tuple[CombatProbeSample | None, CombatProbeDiff | None]:
    """
    Poll dumps for a short window; return first sample that differs from baseline.

    Call after pressing skill. Returns (sample, diff) or (None, None).
    @author by ak
    """
    log = log or (lambda _m: None)
    log(f"burst start rounds={rounds} interval={interval_s}s vs baseline")
    for i in range(int(rounds)):
        s = sample_combat_probe(
            session, log=lambda _m: None, with_pos=True, with_dumps=True
        )
        d = diff_samples(baseline, s)
        changed = bool(
            d.states_changed
            or d.skill_changed_offs
            or d.seq_changed_offs
            or d.host_changed_offs
            or d.nest_changed
        )
        log(f"burst[{i+1}/{rounds}] {d.note}")
        if changed:
            log(f"burst HIT at round {i+1}: {d.note}")
            return s, d
        time.sleep(float(interval_s))
    log("burst done: no change vs baseline")
    return None, None


def format_changed_dwords_detail(
    a_dump: bytes,
    b_dump: bytes,
    offs: list[int],
    *,
    label: str,
    max_rows: int = 24,
) -> str:
    """
    Detail lines: +off A=... B=... for changed dwords.

    @author by ak
    """
    if not offs:
        return f"{label}: (no dword change)"
    lines = [f"{label} changed ({len(offs)} dwords):"]
    for i, off in enumerate(offs[:max_rows]):
        if off + 4 > len(a_dump) or off + 4 > len(b_dump):
            continue
        au = struct.unpack_from("<I", a_dump, off)[0]
        bu = struct.unpack_from("<I", b_dump, off)[0]
        af = struct.unpack_from("<f", a_dump, off)[0]
        bf = struct.unpack_from("<f", b_dump, off)[0]
        lines.append(
            f"  +0x{off:03X}: A={au:08X}({af:.3f}) -> B={bu:08X}({bf:.3f})"
        )
    if len(offs) > max_rows:
        lines.append(f"  ... +{len(offs) - max_rows} more")
    return "\n".join(lines)


def write_lab_capture_report(
    a: CombatProbeSample,
    b: CombatProbeSample,
    d: CombatProbeDiff,
    *,
    out_dir: "Path | None" = None,
) -> str:
    """
    Write A/B probe + dumps + diff to .issues/lab markdown; return path.

    @author by ak
    """
    from pathlib import Path

    if out_dir is None:
        from common.paths import app_root

        dest_dir = app_root() / ".issues" / "lab"
    else:
        dest_dir = Path(out_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = dest_dir / f"lab_capture_{stamp}.md"

    lines = [
        f"# lab capture {stamp}",
        "",
        f"A: {format_sample_line(a)}",
        f"B: {format_sample_line(b)}",
        "",
        f"## diff note",
        "",
        format_dump_diff_note(d),
        "",
    ]
    if a.skill_dump and b.skill_dump and d.skill_changed_offs:
        lines.append(
            format_changed_dwords_detail(
                a.skill_dump, b.skill_dump, d.skill_changed_offs, label="skill"
            )
        )
        lines.append("")
    if a.seq_dump and b.seq_dump and d.seq_changed_offs:
        lines.append(
            format_changed_dwords_detail(
                a.seq_dump, b.seq_dump, d.seq_changed_offs, label="seq"
            )
        )
        lines.append("")

    def _dump_section(title: str, ptr: int | None, data: bytes, hex_s: str) -> list[str]:
        return [
            f"## {title}",
            "",
            f"ptr=0x{(ptr or 0):X} len={len(data)}",
            f"hex: {hex_s or (data[:64].hex() if data else '')}",
            "```",
            format_dword_table(data, max_rows=48),
            "```",
            "",
        ]

    lines.extend(_dump_section("A skill dump", a.skill_ptr, a.skill_dump, a.skill_dump_hex))
    lines.extend(_dump_section("B skill dump", b.skill_ptr, b.skill_dump, b.skill_dump_hex))
    lines.extend(_dump_section("A seq dump", a.skill_seq_ptr, a.seq_dump, a.seq_dump_hex))
    lines.extend(_dump_section("B seq dump", b.skill_seq_ptr, b.seq_dump, b.seq_dump_hex))
    lines.extend(_dump_section("A host dump", a.host_ptr, a.host_dump, a.host_dump_hex))
    lines.extend(_dump_section("B host dump", b.host_ptr, b.host_dump, b.host_dump_hex))

    if d.host_changed_offs and a.host_dump and b.host_dump:
        lines.append(
            format_changed_dwords_detail(
                a.host_dump, b.host_dump, d.host_changed_offs, label="host"
            )
        )
        lines.append("")
    if d.nest_changed:
        lines.append("## nest diffs")
        lines.append("")
        na, nb = a.nest_dumps or {}, b.nest_dumps or {}
        for k, offs in d.nest_changed.items():
            lines.append(
                format_changed_dwords_detail(
                    na.get(k, b""), nb.get(k, b""), offs, label=f"nest {k}"
                )
            )
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
