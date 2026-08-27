# -*- coding: utf-8 -*-
"""
Reticle / free-aim state probe (read-only + optional KEY_FORCE diag).

STATUS: product free-aim uses KEY_HOLD (no SoftSend) via ShiftHoldRunner.
Lab KEY_FORCE / multi-sample remain for diagnosis. User-confirmed HOLD shows reticle.

Use from workbench Lab tab to compare:
  A = idle / no free-aim
  B = real hand Shift (reticle VISIBLE on screen) OR KEY_FORCE on

Key path (partial):
  gate [root+0x4D4], GAKS, input table, GetModMask — can look healthy while
  no reticle is drawn.

UI / beyond keys (current gap):
  root cursor current +0x4B8, slots +0x4BC.. (channel 2 used by 0x484290)
  aim path 0x72D7C0 -> IsKeyTable -> 0x86C690 -> 0x729910 / cursor mode 0x30
  skill obj +0x290 gates cursor mode 0x30 vs ground-aim branch
  controller = *[input_this+0x140]; flags +0x243D/2490/24A8
  host session +0x41C, host+0x1B8, cast-this skill id/flags

@author by ak
"""
from __future__ import annotations

import ctypes
import re
import struct
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from app.core.plg_exports import (
    CAST_FIELD_ELAPSED_OFF,
    CAST_FIELD_EXTRA_FLAGS_OFF,
    CAST_FIELD_FLAGS_OFF,
    CAST_FIELD_SKILL_ID_B_OFF,
    CAST_FIELD_SKILL_ID_OFF,
    DEFAULT_IMAGE_BASE,
    HOST_SESSION_STATE_OFF,
    HOST_SKILL_THIS_OFF,
    HOST_SIDE_INV_OFF,
)
from app.core.remote_runtime import remote_read_bytes
from app.core.skill_cast_probe import resolve_cast_this, resolve_host_side_rpm

LogFn = Callable[[str], None]

_POLL_RECORD_RE = re.compile(
    r"^POLL hit=(\d+) b1=(-?\d+) in=([0-9A-Fa-f]{1,8}) "
    r"ctrl=([0-9A-Fa-f]{1,8}) gate=(-?\d+) "
    r"f=(-?\d+)/(-?\d+)/(-?\d+)/(-?\d+) d6=(\d+)$"
)
_POLL_RECORD_MAX_LEN = 127

NOTE_VA_GAME_ROOT_GLOBAL = 0x015282D8
HOST_SIDE_MID_OFF = 0x24
HOST_SIDE_LEAF_OFF = 0x8C
MID_INPUT_THIS_OFF = 0x78
ROOT_KEY_GATE_OFF = 0x4D4
# Cursor type set by 0x484290(root, mode, channel): current @+0x4B8, slots @+0x4BC
ROOT_CURSOR_CUR_OFF = 0x4B8
ROOT_CURSOR_SLOT_OFF = 0x4BC  # slot i at +0x4BC + 4*i; free-aim uses channel 2
ROOT_CURSOR_CHANNELS = 6
INPUT_KEY_TABLE_OFF = 0x2C
INPUT_MOD_MASK_OFF = 0x134
# Factory 0x4BEDAF: controller* stored at [input_this + 0x140].
INPUT_CONTROLLER_OFF = 0x140

# Input controller (CheckModBind / InputPoll this) offsets of interest.
CTRL_INPUT_PTR_OFF = 0x4  # ctor stores input_this here
CTRL_FLAG_243D = 0x243D  # InputPoll enable (tick 0x4BD011)
CTRL_FLAG_243E = 0x243E
CTRL_FLAG_2490 = 0x2490
CTRL_FLAG_24A8 = 0x24A8
CTRL_FLAG_24DC = 0x24DC
CTRL_FLAG_24DD = 0x24DD
# host side flags used by ControllerTick before free-aim poll
HOST_FLAG_1B8_OFF = 0x1B8
# SkillAim 0x72D7C0 on host-side:
#   aim buffer @ host+0x1A10
#   active skill obj* @ host+0x1A84  then byte [obj+0x290] gates cursor mode 0x30
#   cast-this @ host+0x1A88 (existing HOST_SKILL_THIS_OFF)
HOST_AIM_BUF_OFF = 0x1A10
HOST_ACTIVE_SKILL_OBJ_OFF = 0x1A84
SKILL_OBJ_FLAG_290_OFF = 0x290

_user32 = ctypes.WinDLL("user32", use_last_error=True)


@dataclass
class ReticleProbeSample:
    """One reticle-related snapshot. @author by ak"""

    ok: bool
    label: str = ""
    t_ms: int = 0
    # chain
    root: int = 0
    mid: int = 0
    input_this: int = 0
    host: int = 0
    controller: int = 0
    cast_this: int = 0
    # key layer
    gate: int = -1
    gaks10: int = 0
    gaksA0: int = 0
    tab10: int = -1
    tabA0: int = -1
    tabA1: int = -1
    mask: int = 0
    # host / cast
    host_session: int = 0
    cast_skill_id: int = 0
    cast_skill_id_b: int = 0
    cast_flags: int = 0
    cast_elapsed: int = 0
    cast_extra: int = 0
    host_flags_1b8: int = 0
    # controller flags (if found)
    ctrl_243d: int = -1
    ctrl_243e: int = -1
    ctrl_2490: int = -1
    ctrl_24a8: int = -1
    ctrl_24dc: int = -1
    ctrl_24dd: int = -1
    # cursor UI (0x484290) — strongest cheap proxy for visible reticle
    cursor_cur: int = -1
    cursor_ch0: int = -1
    cursor_ch1: int = -1
    cursor_ch2: int = -1
    cursor_ch3: int = -1
    cursor_ch4: int = -1
    cursor_ch5: int = -1
    # SkillAim path (0x72D7C0) — beyond key layer
    active_skill: int = 0
    skill_290: int = -1  # byte; non-zero -> cursor mode 0x30
    aim_u20: int = 0  # host+0x1A10+0x20
    aim_u24: int = 0
    aim_u28: int = 0  # often -1 idle
    aim_f10: int = 0  # float bits
    aim_f14: int = 0
    aim_f18: int = 0
    aim_f1c: int = 0
    # optional bridge KEY_FORCE diag note
    force_note: str = ""
    force_ret: int | None = None
    note: str = ""
    error: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary_line(self) -> str:
        """One-line human summary. @author by ak"""
        if not self.ok:
            return f"{self.label or 'sample'} FAIL {self.error or ''}".strip()
        return (
            f"{self.label or 'sample'} "
            f"gate={self.gate} gaks={self.gaks10:04X}/{self.gaksA0:04X} "
            f"tab={self.tab10}/{self.tabA0}/{self.tabA1} mask={self.mask:X} "
            f"cur={self.cursor_cur}/{self.cursor_ch2} "
            f"sk290={self.skill_290} aim28=0x{self.aim_u28 & 0xFFFFFFFF:X} "
            f"hostSess=0x{self.host_session:X} "
            f"cast=id:{self.cast_skill_id}/idB:{self.cast_skill_id_b} "
            f"fl=0x{self.cast_flags:X} el={self.cast_elapsed} ex=0x{self.cast_extra:X} "
            f"h1b8=0x{self.host_flags_1b8:X} "
            f"ctrl=0x{self.controller:X} "
            f"f243d={self.ctrl_243d} f2490={self.ctrl_2490} f24a8={self.ctrl_24a8}"
        )


@dataclass(frozen=True)
class ReticlePollResult:
    """Authoritative InputPoll diagnostic result. @author by ak"""

    hit: int
    bind1: int
    input_this: int
    controller: int
    gate: int
    ctrl_243d: int
    ctrl_243e: int
    ctrl_2490: int
    ctrl_24a8: int
    d6: int
    raw: str = ""

    def classification(self) -> str:
        """Classify the first proven failure layer. @author by ak"""
        if self.hit <= 0:
            return "NO_POLL"
        if self.bind1 <= 0:
            return "BIND_REJECTED"
        if self.d6 <= 0:
            return "DOWNSTREAM_REJECTED"
        return "DISPATCH_REACHED"

    def summary_line(self) -> str:
        """Format a compact authoritative result summary. @author by ak"""
        return (
            f"POLL {self.classification()} hit={self.hit} b1={self.bind1} "
            f"d6={self.d6} gate={self.gate} "
            f"f={self.ctrl_243d}/{self.ctrl_243e}/{self.ctrl_2490}/{self.ctrl_24a8}"
        )


def parse_reticle_poll_record(text: str) -> ReticlePollResult:
    """Parse one bounded authoritative POLL record. @author by ak"""
    raw = str(text or "").strip()
    if not raw or len(raw) > _POLL_RECORD_MAX_LEN:
        raise ValueError("invalid POLL record length")
    match = _POLL_RECORD_RE.fullmatch(raw)
    if match is None:
        raise ValueError("invalid POLL record")
    values = [int(match.group(i), 10) for i in (1, 2)]
    addresses = [int(match.group(i), 16) for i in (3, 4)]
    tail = [int(match.group(i), 10) for i in range(5, 11)]
    return ReticlePollResult(
        hit=values[0],
        bind1=values[1],
        input_this=addresses[0],
        controller=addresses[1],
        gate=tail[0],
        ctrl_243d=tail[1],
        ctrl_243e=tail[2],
        ctrl_2490=tail[3],
        ctrl_24a8=tail[4],
        d6=tail[5],
        raw=raw,
    )




def capture_freeaim_diag(
    session,
    *,
    timeout_s: float = 8.0,
    log: LogFn | None = None,
) -> tuple[bool, str, Any]:
    """
    Capture authoritative free-aim skill path (IsKeyTable 0x10 / 0x72D7C0).

    This is the path confirmed against live reticle (not InputPoll bind1/0xD6).
    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        pid = int(getattr(session, "pid", 0) or 0)
        base = int(getattr(session, "module_base", 0) or 0)
    except Exception:
        pid, base = 0, 0
    if not pid or not base:
        return False, "need attach (pid+module_base)", None
    try:
        import importlib.util
        from pathlib import Path as _P

        helper = (
            _P(__file__).resolve().parents[2] / "tools" / "_capture_freeaim_diag.py"
        )
        spec = importlib.util.spec_from_file_location(
            "xajh_capture_freeaim_diag", helper
        )
        if spec is None or spec.loader is None:
            return False, f"freeaim helper missing: {helper}", None
        mod = importlib.util.module_from_spec(spec)
        # dataclass needs the module present in sys.modules during class body.
        import sys as _sys
        _sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        run_capture = getattr(mod, "run_freeaim_capture")
    except Exception as e:
        return False, f"freeaim helper import fail: {e}", None
    # Wait window: idle rejects must not end capture early (see freeaim-lite).
    to = max(3.0, min(6.0, float(timeout_s)))
    log(
        f"reticle AIM capture arm pid={pid} base=0x{base:X} "
        f"timeout={to:.1f}s wait_for_shift need_ok=2"
    )
    ok, raw, result = run_capture(
        pid,
        base,
        timeout_s=to,
        max_entry_hits=4,
        success_hits=2,
    )
    if result is not None:
        log(f"reticle AIM capture {result.summary_line()}")
    else:
        log(f"reticle AIM capture no-record ok={ok}")
    return bool(ok), str(raw or ""), result

def capture_inputpoll_diag(
    session,
    *,
    timeout_s: float = 8.0,
    log: LogFn | None = None,
) -> tuple[bool, str, ReticlePollResult | None]:
    """
    Capture authoritative InputPoll bind1/d6 evidence for the attached session.

    Uses temporary CDB breakpoints only. Does not dispatch action 0xD6.
    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        pid = int(getattr(session, "pid", 0) or 0)
        base = int(getattr(session, "module_base", 0) or 0)
    except Exception:
        pid, base = 0, 0
    if not pid or not base:
        return False, "need attach (pid+module_base)", None
    try:
        import importlib.util
        from pathlib import Path

        helper = (
            Path(__file__).resolve().parents[2]
            / "tools"
            / "_capture_inputpoll_diag.py"
        )
        spec = importlib.util.spec_from_file_location(
            "xajh_capture_inputpoll_diag", helper
        )
        if spec is None or spec.loader is None:
            return False, f"capture helper missing: {helper}", None
        mod = importlib.util.module_from_spec(spec)
        # dataclass needs the module present in sys.modules during class body.
        import sys as _sys
        _sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        run_capture = getattr(mod, "run_inputpoll_capture")
    except Exception as e:
        return False, f"capture helper import fail: {e}", None
    log(
        f"reticle POLL capture arm pid={pid} base=0x{base:X} "
        f"timeout={float(timeout_s):.1f}s"
    )
    # Primary: free-aim skill path (live reticle proven independent of bind1).
    aim_ok, aim_raw, aim_result = capture_freeaim_diag(
        session, timeout_s=float(timeout_s), log=log
    )
    if aim_result is None:
        log(f"reticle AIM unavailable, fallback InputPoll: {aim_raw}")
    if aim_result is not None:
        # Map free-aim AIM result into legacy POLL fields for existing UI.
        # hit: aim tick seen; bind1: IsKeyTable(0x10) ok; d6: aim consumer call.
        synthetic = None
        try:
            seen = int(aim_result.hit_entry) > 0 or int(aim_result.hit_tab10) > 0
            hit = (
                max(1, int(aim_result.hit_entry or aim_result.hit_tab10))
                if seen
                else 0
            )
            b1 = 1 if int(aim_result.tab10_ok) > 0 else 0
            d6 = 1 if int(aim_result.hit_aim_call) > 0 else 0
            raw_line = (
                f"POLL hit={hit} b1={b1} in={int(aim_result.last_input):X} "
                f"ctrl=0 gate=-1 f=-1/-1/-1/-1 d6={d6}"
            )
            synthetic = parse_reticle_poll_record(raw_line)
        except Exception:
            synthetic = None
        merged = (
            f"{aim_raw}\n"
            f"=== mapped-from-AIM ===\n"
            f"{aim_result.summary_line()}\n"
            f"AIM_CLASS {aim_result.classification()}\n"
        )
        log(f"reticle AIM primary {aim_result.summary_line()}")
        return bool(aim_ok), merged, synthetic

    # Fallback: old InputPoll bind1/d6 capture
    ok, raw, result = run_capture(pid, base, timeout_s=float(timeout_s))
    if result is not None:
        log(f"reticle POLL capture {result.summary_line()}")
    else:
        log(f"reticle POLL capture no-record ok={ok}")
    return bool(ok), str(raw or ""), result


def _u32(session, addr: int) -> int | None:
    if not addr:
        return None
    try:
        raw = remote_read_bytes(int(session.pid), int(addr) & 0xFFFFFFFF, 4)
        if len(raw) < 4:
            return None
        return struct.unpack("<I", raw)[0]
    except Exception:
        return None


def _u8(session, addr: int) -> int | None:
    if not addr:
        return None
    try:
        raw = remote_read_bytes(int(session.pid), int(addr) & 0xFFFFFFFF, 1)
        if not raw:
            return None
        return int(raw[0])
    except Exception:
        return None


def _heap_like(p: int | None) -> bool:
    if not p:
        return False
    return 0x01000000 <= int(p) <= 0x7FFE0000


def _live(session, note_va: int) -> int:
    base = int(getattr(session, "module_base", 0) or 0)
    return int(base) + (int(note_va) - int(DEFAULT_IMAGE_BASE))


def _gaks(vk: int) -> int:
    try:
        return int(_user32.GetAsyncKeyState(int(vk)) & 0xFFFF)
    except Exception:
        return 0


def resolve_input_chain(session) -> tuple[int, int, int, int]:
    """
    Resolve root, mid, input_this, gate.

    root = *[0x15282D8]
    mid  = root+0x24
    input= mid+0x78
    gate = root+0x4D4
    @author by ak
    """
    root = _u32(session, _live(session, NOTE_VA_GAME_ROOT_GLOBAL)) or 0
    if not root:
        return 0, 0, 0, -1
    mid = _u32(session, int(root) + HOST_SIDE_MID_OFF) or 0
    inp = 0
    if mid:
        inp = _u32(session, int(mid) + MID_INPUT_THIS_OFF) or 0
        if not _heap_like(inp):
            inp = 0
    gate = _u8(session, int(root) + ROOT_KEY_GATE_OFF)
    return int(root), int(mid or 0), int(inp or 0), int(gate if gate is not None else -1)


def _controller_looks_valid(session, ctrl: int, input_this: int) -> bool:
    """Validate controller object against input_this. @author by ak"""
    if not _heap_like(ctrl) or not input_this:
        return False
    leaf = _u32(session, int(ctrl) + CTRL_INPUT_PTR_OFF)
    if leaf is None or (int(leaf) & 0xFFFFFFFF) != (int(input_this) & 0xFFFFFFFF):
        return False
    # Must have readable enable flags region (object is large, ~0x24E0).
    return _u8(session, int(ctrl) + CTRL_FLAG_243D) is not None


def resolve_controller_from_input(session, mid: int, input_this: int) -> int:
    """
    Resolve InputPoll/CheckModBind controller (this).

    Primary (factory 0x4BEDAF):
      controller = *[input_this + 0x140]
      and [controller + 4] == input_this

    Fallback: scan mid dword fields for the same shape (legacy).
    @author by ak
    """
    if not input_this:
        return 0
    # Primary path from outer factory store.
    p = _u32(session, int(input_this) + INPUT_CONTROLLER_OFF) or 0
    if p and _controller_looks_valid(session, int(p), int(input_this)):
        return int(p)

    if not mid:
        return 0
    try:
        raw = remote_read_bytes(int(session.pid), int(mid) & 0xFFFFFFFF, 0x300)
    except Exception:
        return 0
    if len(raw) < 8:
        return 0
    want = int(input_this) & 0xFFFFFFFF
    for off in range(0, len(raw) - 3, 4):
        cand = struct.unpack_from("<I", raw, off)[0]
        if _controller_looks_valid(session, int(cand), want):
            return int(cand)
    return 0


def sample_reticle_state(
    session,
    *,
    label: str = "",
    log: LogFn | None = None,
    bridge_force_note: str = "",
    bridge_force_ret: int | None = None,
) -> ReticleProbeSample:
    """
    Read reticle-related state (keys + cast + controller flags).

    @author by ak
    """
    log = log or (lambda _m: None)
    out = ReticleProbeSample(
        ok=False,
        label=str(label or ""),
        t_ms=int(time.time() * 1000) & 0x7FFFFFFF,
        force_note=str(bridge_force_note or ""),
        force_ret=bridge_force_ret,
    )
    if not getattr(session, "pid", None) or not getattr(session, "module_base", None):
        out.error = "need attach (pid+module_base)"
        return out
    try:
        root, mid, inp, gate = resolve_input_chain(session)
        out.root = root
        out.mid = mid
        out.input_this = inp
        out.gate = gate
        if root:
            def _ci(off: int) -> int:
                v = _u32(session, root + off)
                return int(v) if v is not None else -1

            # signed-ish display: 0xFFFFFFFF -> treat as -1 for summary readability
            def _cs(off: int) -> int:
                v = _ci(off)
                if v == 0xFFFFFFFF:
                    return -1
                if v > 0x7FFFFFFF:
                    return v - 0x100000000
                return v

            out.cursor_cur = _cs(ROOT_CURSOR_CUR_OFF)
            out.cursor_ch0 = _cs(ROOT_CURSOR_SLOT_OFF + 0)
            out.cursor_ch1 = _cs(ROOT_CURSOR_SLOT_OFF + 4)
            out.cursor_ch2 = _cs(ROOT_CURSOR_SLOT_OFF + 8)  # free-aim channel
            out.cursor_ch3 = _cs(ROOT_CURSOR_SLOT_OFF + 12)
            out.cursor_ch4 = _cs(ROOT_CURSOR_SLOT_OFF + 16)
            out.cursor_ch5 = _cs(ROOT_CURSOR_SLOT_OFF + 20)
        out.gaks10 = _gaks(0x10)
        out.gaksA0 = _gaks(0xA0)
        if inp:
            out.tab10 = int(_u8(session, inp + INPUT_KEY_TABLE_OFF + 0x10) or 0)
            out.tabA0 = int(_u8(session, inp + INPUT_KEY_TABLE_OFF + 0xA0) or 0)
            out.tabA1 = int(_u8(session, inp + INPUT_KEY_TABLE_OFF + 0xA1) or 0)
            out.mask = int(_u32(session, inp + INPUT_MOD_MASK_OFF) or 0)

        host = resolve_host_side_rpm(session)
        out.host = int(host or 0)
        if host:
            out.host_session = int(_u32(session, host + HOST_SESSION_STATE_OFF) or 0)
            out.host_flags_1b8 = int(_u32(session, host + HOST_FLAG_1B8_OFF) or 0)
            # SkillAim buffer @ host+0x1A10 (written after 0x86C690 success)
            ab = host + HOST_AIM_BUF_OFF
            out.aim_f10 = int(_u32(session, ab + 0x10) or 0)
            out.aim_f14 = int(_u32(session, ab + 0x14) or 0)
            out.aim_f18 = int(_u32(session, ab + 0x18) or 0)
            out.aim_f1c = int(_u32(session, ab + 0x1C) or 0)
            out.aim_u20 = int(_u32(session, ab + 0x20) or 0)
            out.aim_u24 = int(_u32(session, ab + 0x24) or 0)
            out.aim_u28 = int(_u32(session, ab + 0x28) or 0)
            # Active skill object used by free-aim mode branch
            sk = int(_u32(session, host + HOST_ACTIVE_SKILL_OBJ_OFF) or 0)
            out.active_skill = sk if _heap_like(sk) else 0
            if out.active_skill:
                b290 = _u8(session, out.active_skill + SKILL_OBJ_FLAG_290_OFF)
                out.skill_290 = int(b290) if b290 is not None else -1
            cast = _u32(session, host + HOST_SKILL_THIS_OFF) or 0
            if _heap_like(cast):
                out.cast_this = int(cast)
                out.cast_skill_id = int(_u32(session, cast + CAST_FIELD_SKILL_ID_OFF) or 0)
                out.cast_skill_id_b = int(
                    _u32(session, cast + CAST_FIELD_SKILL_ID_B_OFF) or 0
                )
                out.cast_flags = int(_u32(session, cast + CAST_FIELD_FLAGS_OFF) or 0)
                out.cast_elapsed = int(_u32(session, cast + CAST_FIELD_ELAPSED_OFF) or 0)
                out.cast_extra = int(
                    _u32(session, cast + CAST_FIELD_EXTRA_FLAGS_OFF) or 0
                )
            inv = _u32(session, host + HOST_SIDE_INV_OFF) or 0
            out.extra["inv_this"] = int(inv or 0)

        ctrl = resolve_controller_from_input(session, mid, inp)
        out.controller = int(ctrl or 0)
        if ctrl:
            def b(off: int) -> int:
                v = _u8(session, ctrl + off)
                return int(v) if v is not None else -1

            out.ctrl_243d = b(CTRL_FLAG_243D)
            out.ctrl_243e = b(CTRL_FLAG_243E)
            out.ctrl_2490 = b(CTRL_FLAG_2490)
            out.ctrl_24a8 = b(CTRL_FLAG_24A8)
            out.ctrl_24dc = b(CTRL_FLAG_24DC)
            out.ctrl_24dd = b(CTRL_FLAG_24DD)

        out.ok = True
        out.note = (
            f"root=0x{root:X} mid=0x{mid:X} input=0x{inp:X} "
            f"host=0x{out.host:X} cast=0x{out.cast_this:X} ctrl=0x{out.controller:X}"
        )
        if bridge_force_note:
            out.note += f" | force={bridge_force_note}"
        log(f"reticle probe {out.summary_line()}")
        return out
    except Exception as e:
        out.error = str(e)
        log(f"reticle probe fail: {e}")
        return out


def diff_reticle_samples(a: ReticleProbeSample, b: ReticleProbeSample) -> list[str]:
    """
    List field changes A -> B (skip identity/time).

    @author by ak
    """
    skip = {"label", "t_ms", "note", "error", "extra", "force_note", "force_ret"}
    da = a.to_dict()
    db = b.to_dict()
    lines: list[str] = []
    for k in sorted(set(da) | set(db)):
        if k in skip:
            continue
        va, vb = da.get(k), db.get(k)
        if va != vb:
            if isinstance(va, int) and isinstance(vb, int):
                lines.append(f"{k}: 0x{int(va):X} -> 0x{int(vb):X} ({va} -> {vb})")
            else:
                lines.append(f"{k}: {va!r} -> {vb!r}")
    if a.force_note != b.force_note and (a.force_note or b.force_note):
        lines.append(f"force_note: {a.force_note!r} -> {b.force_note!r}")
    return lines


def format_reticle_sample(s: ReticleProbeSample) -> str:
    """Multi-line detail for Text dump. @author by ak"""
    if not s.ok:
        return f"FAIL {s.error or ''}\n"
    lines = [
        s.summary_line(),
        f"  chain root=0x{s.root:X} mid=0x{s.mid:X} input=0x{s.input_this:X}",
        f"  host=0x{s.host:X} cast=0x{s.cast_this:X} ctrl=0x{s.controller:X}",
        f"  keys gate={s.gate} gaks10=0x{s.gaks10:04X} gaksA0=0x{s.gaksA0:04X} "
        f"tab10/A0/A1={s.tab10}/{s.tabA0}/{s.tabA1} mask=0x{s.mask:X}",
        f"  cursor cur={s.cursor_cur} ch={s.cursor_ch0}/{s.cursor_ch1}/"
        f"{s.cursor_ch2}/{s.cursor_ch3}/{s.cursor_ch4}/{s.cursor_ch5}",
        f"  skillAim active=0x{s.active_skill:X} +290={s.skill_290} "
        f"(nonzero=>mode 0x30)",
        f"  aimBuf@+1A10 u20=0x{s.aim_u20:X} u24=0x{s.aim_u24:X} "
        f"u28=0x{s.aim_u28 & 0xFFFFFFFF:X} "
        f"f10/14/18/1c={s.aim_f10:08X}/{s.aim_f14:08X}/{s.aim_f18:08X}/{s.aim_f1c:08X}",
        f"  host_session(+0x41C)=0x{s.host_session:X} host_1b8=0x{s.host_flags_1b8:X}",
        f"  cast id=0x{s.cast_skill_id:X} idB=0x{s.cast_skill_id_b:X} "
        f"flags=0x{s.cast_flags:X} elapsed={s.cast_elapsed} extra=0x{s.cast_extra:X}",
        f"  ctrl flags 243d={s.ctrl_243d} 243e={s.ctrl_243e} "
        f"2490={s.ctrl_2490} 24a8={s.ctrl_24a8} 24dc={s.ctrl_24dc} 24dd={s.ctrl_24dd}",
    ]
    if s.force_note:
        lines.append(f"  force: ret={s.force_ret} note={s.force_note}")
    return "\n".join(lines) + "\n"


def key_force_diag(
    pid: int,
    hwnd: int = 0,
    *,
    down: bool = True,
    level_only: bool = False,
    log: LogFn | None = None,
) -> tuple[bool, int | None, str]:
    """
    Call bridge KEY_FORCE and return (ok, ret, note).

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        from app.core.xajh_bridge import ensure_bridge

        br = ensure_bridge(
            int(pid),
            log=log,
            inject_if_needed=False,
            hwnd=int(hwnd or 0) or None,
            force_reinject=False,
        )
        if br is None:
            return False, None, "bridge not ready"
        r = br.key_force(
            0x10,
            down=bool(down),
            level_only=bool(level_only),
            hwnd=int(hwnd or 0) or None,
            timeout_ms=3000,
        )
        note = (r.note or r.error or "").strip()
        try:
            br.close()
        except Exception:
            pass
        return bool(r.ok), (int(r.ret) if r.ret is not None else None), note
    except Exception as e:
        return False, None, str(e)


def key_diag_run(
    pid: int,
    hwnd: int = 0,
    *,
    start: bool = True,
    snapshot: bool = False,
    log: LogFn | None = None,
) -> tuple[bool, int | None, str]:
    """
    Run message/controller diagnostics for free-aim Shift.

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        from app.core.xajh_bridge import ensure_bridge

        br = ensure_bridge(
            int(pid),
            log=log,
            inject_if_needed=False,
            hwnd=int(hwnd or 0) or None,
            force_reinject=False,
        )
        if br is None:
            return False, None, "bridge not ready"
        r = br.key_diag(
            start=bool(start),
            snapshot=bool(snapshot),
            hwnd=int(hwnd or 0) or None,
            timeout_ms=3000,
        )
        note = (r.note or r.error or "").strip()
        try:
            br.close()
        except Exception:
            pass
        return bool(r.ok), (int(r.ret) if r.ret is not None else None), note
    except Exception as e:
        return False, None, str(e)


def shift_layer_active(sample: ReticleProbeSample) -> bool:
    """
    True when key layer looks like Shift is held (GAKS or input table).

    Used by auto-capture so user can keep holding Shift without clicking UI.
    @author by ak
    """
    if not sample or not sample.ok:
        return False
    if int(sample.gaks10 or 0) & 0x8000:
        return True
    if int(sample.gaksA0 or 0) & 0x8000:
        return True
    if int(sample.tab10 or 0) == 1 or int(sample.tabA0 or 0) == 1:
        return True
    if int(sample.mask or 0) & 0x1:
        return True
    return False


def wait_reticle_on_shift(
    session,
    *,
    label: str = "B_AUTO",
    timeout_s: float = 8.0,
    interval_s: float = 0.05,
    require_shift: bool = True,
    hold_s: float = 0.45,
    stop_event: Any | None = None,
    log: LogFn | None = None,
) -> ReticleProbeSample:
    """
    Poll until Shift key layer stays active, then return a late sample.

    Lab UX: user must NOT click workbench while holding Shift (focus conflict).
    Arm once here, switch to game, hold Shift with reticle visible.
    hold_s keeps sampling after first hit so we avoid keydown edge only.
    @author by ak
    """
    log = log or (lambda _m: None)
    deadline = time.time() + max(0.2, float(timeout_s))
    last = ReticleProbeSample(ok=False, label=str(label or "B_AUTO"), error="not started")
    rounds = 0
    hold_need = max(0.0, float(hold_s))
    hold_start: float | None = None
    best: ReticleProbeSample | None = None
    log(
        f"reticle wait_shift arm label={label} "
        f"timeout={timeout_s:.1f}s hold={hold_need:.2f}s "
        f"require_shift={require_shift}"
    )
    while time.time() < deadline:
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            last = ReticleProbeSample(
                ok=False, label=str(label or "B_AUTO"), error="cancelled"
            )
            log(f"reticle wait_shift cancelled after {rounds} polls")
            return last
        s = sample_reticle_state(session, label=str(label or "B_AUTO"), log=lambda _m: None)
        last = s
        rounds += 1
        active = bool(s.ok and (not require_shift or shift_layer_active(s)))
        if active:
            best = s
            if hold_start is None:
                hold_start = time.time()
                log(
                    f"reticle wait_shift edge after {rounds} polls "
                    f"(hold {hold_need:.2f}s) ({s.summary_line()})"
                )
            if (time.time() - hold_start) >= hold_need:
                log(
                    f"reticle wait_shift HIT after {rounds} polls "
                    f"({s.summary_line()})"
                )
                return s
        else:
            hold_start = None
        time.sleep(max(0.01, float(interval_s)))
    if best is not None and best.ok:
        # Partial hold before timeout — still better than nothing.
        log(
            f"reticle wait_shift PARTIAL after {rounds} polls "
            f"({best.summary_line()})"
        )
        return best
    if last.ok:
        last.error = f"timeout after {rounds} polls (shift never seen)"
        last.ok = False
    else:
        last.error = last.error or f"timeout after {rounds} polls"
    log(f"reticle wait_shift MISS: {last.error}")
    return last


def capture_reticle_after_delay(
    session,
    *,
    label: str = "B_DELAY",
    delay_s: float = 5.0,
    stop_event: Any | None = None,
    log: LogFn | None = None,
) -> ReticleProbeSample:
    """
    Sleep delay_s then take one sample. No click while holding Shift.

    Flow: click arm on workbench -> switch to game -> hold Shift/reticle
    until timer fires. Focus may stay on game the whole time.
    @author by ak
    """
    log = log or (lambda _m: None)
    delay = max(0.2, float(delay_s))
    log(f"reticle delay_capture arm label={label} delay={delay:.1f}s")
    end = time.time() + delay
    while time.time() < end:
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            log("reticle delay_capture cancelled")
            return ReticleProbeSample(
                ok=False, label=str(label or "B_DELAY"), error="cancelled"
            )
        left = end - time.time()
        # Coarse countdown logs so UI feels alive.
        if left > 0.05:
            time.sleep(min(0.25, left))
        else:
            break
    s = sample_reticle_state(session, label=str(label or "B_DELAY"), log=lambda _m: None)
    if s.ok:
        log(f"reticle delay_capture HIT ({s.summary_line()})")
    else:
        log(f"reticle delay_capture FAIL ({s.error})")
    return s


# Region dumps for A/B when field-level proxy misses visible reticle.
# @author by ak
_RETICLE_DUMP_REGIONS = (
    ("root_cursor", "root", 0x4A0, 0x80),
    ("host_aim", "host", 0x1A00, 0x120),
    ("host_skillmeta", "host", 0x1900, 0x100),
    ("host_1b0", "host", 0x1B0, 0x40),
    ("ctrl_flags", "ctrl", 0x2430, 0xC0),
    ("cast_head", "cast", 0x0, 0x80),
    ("active_skill", "askill", 0x280, 0x40),
)


def reticle_visibility_score(s: ReticleProbeSample, baseline: ReticleProbeSample | None = None) -> int:
    """
    Heuristic score for free-aim reticle active.

    Live evidence (hand Shift, reticle visible): cursor ch2 may stay -1 and
    skill_290 may stay 0; the strong signals are aimBuf floats / aim_u28 and
    controller +0x24DC.
    @author by ak
    """
    if not s or not s.ok:
        return -10**9
    score = 0
    # Proven live signals (2026-07-21 hand reticle A/B)
    if int(getattr(s, "ctrl_24dc", -1) or 0) == 1:
        score += 60
    if baseline and baseline.ok:
        for name in ("aim_f10", "aim_f14", "aim_f18", "aim_f1c", "aim_u28"):
            if getattr(s, name, None) != getattr(baseline, name, None):
                score += 25
        if int(getattr(s, "ctrl_24dc", -1)) != int(getattr(baseline, "ctrl_24dc", -1)):
            score += 30
    else:
        for f in (s.aim_f10, s.aim_f14, s.aim_f18, s.aim_f1c):
            if int(f) not in (0, 0xFFFFFFFF):
                score += 4
        if (int(s.aim_u28) & 0xFFFFFFFF) not in (0, 0xFFFFFFFF):
            score += 8
    # Secondary / alternate path (mode 0x30 UI cursor)
    if int(s.cursor_ch2) not in (-1, 0xFFFFFFFF):
        score += 40
        if int(s.cursor_ch2) == 0x30:
            score += 60
    if int(s.skill_290) > 0:
        score += 30
    if int(s.aim_u20) or int(s.aim_u24):
        score += 10
    if baseline and baseline.ok:
        for name in (
            "cursor_cur",
            "cursor_ch2",
            "skill_290",
            "aim_u20",
            "aim_u24",
            "cast_skill_id",
            "cast_flags",
            "host_session",
            "ctrl_2490",
            "ctrl_24a8",
        ):
            if getattr(s, name, None) != getattr(baseline, name, None):
                score += 3
    # key layer alone is weak
    if shift_layer_active(s):
        score += 1
    return score


def dump_reticle_regions(session, sample: ReticleProbeSample) -> dict[str, bytes]:
    """
    Read fixed memory windows for deep A/B diff.
    @author by ak
    """
    out: dict[str, bytes] = {}
    bases = {
        "root": int(sample.root or 0),
        "host": int(sample.host or 0),
        "ctrl": int(sample.controller or 0),
        "cast": int(sample.cast_this or 0),
        "askill": int(sample.active_skill or 0),
    }
    for name, base_key, off, size in _RETICLE_DUMP_REGIONS:
        base = bases.get(base_key) or 0
        if not base:
            continue
        try:
            raw = remote_read_bytes(session, int(base + off), int(size))
            if raw:
                out[f"{name}@{base_key}+{off:X}/{size:X}"] = bytes(raw)
        except Exception:
            continue
    return out


def format_region_diffs(
    a_regs: dict[str, bytes], b_regs: dict[str, bytes], *, max_hits: int = 80
) -> list[str]:
    """
    Dword-level diffs of region dumps.
    @author by ak
    """
    lines: list[str] = []
    keys = sorted(set(a_regs) | set(b_regs))
    hits = 0
    for k in keys:
        ba, bb = a_regs.get(k), b_regs.get(k)
        if not ba or not bb or ba == bb:
            if ba and not bb:
                lines.append(f"{k}: only in A ({len(ba)}B)")
            elif bb and not ba:
                lines.append(f"{k}: only in B ({len(bb)}B)")
            continue
        n = min(len(ba), len(bb)) & ~3
        local = 0
        for i in range(0, n, 4):
            va = struct.unpack_from("<I", ba, i)[0]
            vb = struct.unpack_from("<I", bb, i)[0]
            if va == vb:
                continue
            lines.append(f"{k}+0x{i:X}: 0x{va:08X} -> 0x{vb:08X}")
            local += 1
            hits += 1
            if hits >= max_hits:
                lines.append(f"... truncated at {max_hits} dword diffs")
                return lines
        if local == 0 and ba != bb:
            lines.append(f"{k}: bytes differ but no aligned dword (lenA={len(ba)} lenB={len(bb)})")
    if not lines:
        lines.append("(no region dword diffs)")
    return lines


def capture_reticle_window(
    session,
    *,
    label: str = "B_WIN",
    delay_s: float = 5.0,
    sample_tail_s: float = 2.0,
    interval_s: float = 0.12,
    baseline: ReticleProbeSample | None = None,
    stop_event: Any | None = None,
    log: LogFn | None = None,
) -> tuple[ReticleProbeSample, dict[str, bytes], list[str]]:
    """
    Wait delay_s, multi-sample during the last sample_tail_s, pick best score.

    Returns (best_sample, region_dump, score_log_lines).
    Solves single-shot miss while reticle is visibly on screen.
    @author by ak
    """
    log = log or (lambda _m: None)
    delay = max(0.3, float(delay_s))
    tail = max(0.2, min(delay, float(sample_tail_s)))
    pre = max(0.0, delay - tail)
    log(
        f"reticle window arm label={label} delay={delay:.1f}s "
        f"tail={tail:.1f}s interval={interval_s:.2f}s"
    )
    end_pre = time.time() + pre
    while time.time() < end_pre:
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            return (
                ReticleProbeSample(ok=False, label=label, error="cancelled"),
                {},
                ["cancelled"],
            )
        time.sleep(min(0.2, max(0.05, end_pre - time.time())))

    end = time.time() + tail
    best: ReticleProbeSample | None = None
    best_score = -10**9
    best_regs: dict[str, bytes] = {}
    scores: list[str] = []
    n = 0
    while time.time() < end:
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            break
        s = sample_reticle_state(session, label=str(label or "B_WIN"), log=lambda _m: None)
        n += 1
        sc = reticle_visibility_score(s, baseline)
        scores.append(f"#{n} score={sc} {s.summary_line()}")
        if sc >= best_score and s.ok:
            best = s
            best_score = sc
            best_regs = dump_reticle_regions(session, s)
        time.sleep(max(0.03, float(interval_s)))

    if best is None:
        best = sample_reticle_state(session, label=str(label or "B_WIN"), log=lambda _m: None)
        best_regs = dump_reticle_regions(session, best) if best.ok else {}
        best_score = reticle_visibility_score(best, baseline)
        scores.append(f"#final score={best_score} {best.summary_line()}")

    log(
        f"reticle window HIT n={n} best_score={best_score} ({best.summary_line()})"
    )
    return best, best_regs, scores


