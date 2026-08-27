# -*- coding: utf-8 -*-
"""Direct, serialized P1/P2 丸子 packet runner.

This is intentionally independent of the recovery-item slots. One logical
pair is sent as two short remote calls: P1 then P2. A runner owns a pid
exclusively, never overlaps pairs, and stops on the first remote-call failure.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import struct
import threading
import time
from ctypes import wintypes
from typing import Callable

from app.core.remote_runtime import (
    MEM_COMMIT,
    MEM_RELEASE,
    MEM_RESERVE,
    PAGE_EXECUTE_READWRITE,
    ensure_pid_remote_callable,
    kernel32,
    note_remote_os_error,
    open_process,
    pid_call_mutex,
    read_process,
    wait_remote_thread_safely,
    write_process,
)


LogFn = Callable[[str], None]

ROOT_GLOBAL = 0x015282D8
THIS_OFFSET = 0x2C
SEND_TARGET = 0x00CD1740
ORIGINAL_TARGET_PREFIX = bytes.fromhex("64A100000000")

WANZI_KIND_WAIGONG = "waigong"
WANZI_KIND_NEIGONG = "neigong"

# 挂机丸子的 AOI 门禁只做 ReadProcessMemory，不调用 CRT / bridge。调度中心
# 周期刷新下面这份缓存，发包线程只读缓存，绝不等待扫描完成。
WANZI_AOI_SCAN_INTERVAL_S = 0.20
WANZI_AOI_MAX_AGE_S = 1.20
WANZI_AOI_GATE_WAIT_S = 0.05
WANZI_AOI_NO_MONSTER_CONFIRM = 5
WANZI_AOI_MAX_OBJECTS = 512
WANZI_AOI_DEFAULT_RADIUS = 18.0
WANZI_NO_SELECTED_INTERRUPT_S = 3.0
WANZI_AOI_STATIC_DISTANCE_EPSILON_M = 0.05
WANZI_AOI_STATIC_INTERRUPT_S = 3.0
# Character control-state gate.  The scheduler samples this by pure RPM; the
# packet worker only reads the cache.  Live captures show knock-up/knock-down
# as host+0x41C = 5/*/6115 for the short airborne phase and 5/*/6104
# for the sustained ground-recovery phase that remains until Space recovery.
WANZI_CONTROL_SCAN_INTERVAL_S = 0.05
WANZI_CONTROL_MAX_AGE_S = 0.30
WANZI_CONTROL_RECOVERY_S = 0.05
WANZI_CONTROL_GATE0 = 5
WANZI_CONTROL_GATE2_HIT_FLY = 6115
WANZI_CONTROL_GATE2_GROUND_RECOVERY = 6104
WANZI_CONTROL_GATE2_VALUES = frozenset(
    (WANZI_CONTROL_GATE2_HIT_FLY, WANZI_CONTROL_GATE2_GROUND_RECOVERY)
)
WANZI_ACTIVE_WINDOW_S = 300.0
WANZI_LOW_RATE_SECONDS = 6.0
# Six-second low-rate fence averages three complete P1/P2 pairs per second.
WANZI_LOW_RATE_PAIRS_PER_SECOND = 3.0
NPC_MANAGER_OFF = 0x74
HOST_SIDE_OFF = 0x8C
NPC_ARRAY_COUNT_OFF = 0x18
NPC_HASH_BUCKETS_OFF = 0x1C
NPC_HASH_BUCKET_COUNT_OFF = 0x28
NPC_HASH_BUCKETS_MAX = 4096
OBJ_POS_OFF = 0x158
OBJ_ID64_OFF = 0x140
MONSTER_HP_OFF = 0x2F8  # legacy compatibility; not used for AOI gating
MONSTER_DEAD_MARKER_OFF = 0x2FC  # legacy compatibility; not used for AOI gating
HOST_SESSION_GATE_OFF = 0x41C
# MSVC RTTI: .?AVCECNPCMonster@@ primary vftable.  CECNPCServer uses
# 0x01260F9C and is deliberately excluded, so nearby service NPCs do not open
# the packet gate.
CECNPC_MONSTER_VFT = 0x01260AF4

_WANZI_AOI_STATE: dict[int, dict] = {}
_WANZI_AOI_LOCK = threading.Lock()
_WANZI_CONTROL_STATE: dict[int, dict] = {}
_WANZI_CONTROL_LOCK = threading.Lock()

P1_WAIGONG = bytes.fromhex(
    "1F0000000000005F0A01030100014200000000000000000000"
    "60C0FCC162FD7242E09E86C201"
)
P1_NEIGONG = bytes.fromhex(
    "1F0000000000005E0A01036300004200000000000000000000"
    "AC9303C262FD72428A7481C201"
)
# Backward-compatible name: the packet recovered first was the 外功丸子.
P1 = P1_WAIGONG
P2 = bytes.fromhex(
    "1F0000000000005D0A01020800FF4100000000000000000000"
    "268B97C28D8A7A42A212C4C301"
)
assert len(P1_WAIGONG) == 38 and len(P1_NEIGONG) == 38 and len(P2) == 38

# The recovered P1 layout stores the bag coordinate in these two bytes.  The
# original external-pill script used 03 01: PACK1 / its second UI slot.
P1_PACKAGE_OFF = 0x0A
P1_SLOT_OFF = 0x0B
def build_wanzi_p1(kind: str, package: int, slot: int) -> bytes:
    """Build one P1 using the selected bag coordinate captured at startup.

    This is intentionally a one-time operation.  The runner retains the
    result, so its high-frequency send loop never reads inventory state.
    """
    try:
        package_i = int(package)
        slot_i = int(slot)
    except Exception as exc:
        raise ValueError(f"invalid wanzi bag coordinate: {exc}") from exc
    if not 0 <= package_i <= 0xFF or not 0 <= slot_i <= 0xFF:
        raise ValueError(
            f"wanzi bag coordinate out of byte range: package={package_i} slot={slot_i}"
        )
    base = P1_NEIGONG if str(kind or "").strip().lower() == WANZI_KIND_NEIGONG else P1_WAIGONG
    payload = bytearray(base)
    payload[P1_PACKAGE_OFF] = package_i
    payload[P1_SLOT_OFF] = slot_i
    return bytes(payload)

kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.ResumeThread.restype = wintypes.DWORD
kernel32.FlushInstructionCache.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, ctypes.c_size_t]
kernel32.FlushInstructionCache.restype = wintypes.BOOL
kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeThread.restype = wintypes.BOOL


def _p32(value: int) -> bytes:
    return struct.pack("<I", int(value) & 0xFFFFFFFF)


def _valid_ptr(value: int) -> bool:
    """Accept the full WOW64/LARGEADDRESSAWARE 32-bit user address range.

    This client legitimately allocates scene/AOI managers above 0x80000000
    (for example 0x8301B940).  ReadProcessMemory remains the authoritative
    readability check; only null/low pointers and the top guard region are
    rejected here.
    """
    pointer = int(value) & 0xFFFFFFFF
    return 0x10000 <= pointer < 0xFFFF0000


def _read_u32(handle: int, address: int) -> int:
    return int(struct.unpack("<I", read_process(handle, int(address), 4))[0])


def probe_nearby_class2_rpm(
    pid: int,
    host_pos: tuple[float, float, float] | None = None,
    *,
    radius: float = WANZI_AOI_DEFAULT_RADIUS,
) -> dict:
    """Pure-RPM nearby class-2 AOI probe.

    The live class-2 table is the game's unified CECNPC hash table.  Entries
    are filtered by the CECNPCMonster vftable, so nearby service NPCs do not
    open the gate.  No names, exports, CRT or bridge calls are made here.
    """
    pid = int(pid)
    try:
        radius_f = max(0.1, float(radius))
    except Exception:
        radius_f = WANZI_AOI_DEFAULT_RADIUS
    supplied_pos: tuple[float, float, float] | None = None
    if host_pos is not None:
        try:
            supplied_pos = (
                float(host_pos[0]),
                float(host_pos[1]),
                float(host_pos[2]),
            )
        except Exception as exc:
            return {
                "known": False,
                "has_monster": False,
                "reason": f"invalid_host_pos:{exc}",
                "count": 0,
                "radius": radius_f,
            }
        if not all(
            math.isfinite(v) and abs(v) <= 500000.0 for v in supplied_pos
        ):
            return {
                "known": False,
                "has_monster": False,
                "reason": "invalid_host_pos",
                "count": 0,
                "radius": radius_f,
            }

    handle = 0
    try:
        handle = open_process(pid)
        root = _read_u32(handle, ROOT_GLOBAL)
        if not _valid_ptr(root):
            raise RuntimeError(f"invalid_root:0x{root:08X}")
        mid = _read_u32(handle, root + 0x24)
        if not _valid_ptr(mid):
            raise RuntimeError(f"invalid_mid:0x{mid:08X}")
        host = _read_u32(handle, mid + HOST_SIDE_OFF)
        if not _valid_ptr(host):
            raise RuntimeError(f"invalid_host:0x{host:08X}")
        selected_id64_known = True
        try:
            selected_id64 = struct.unpack(
                "<Q", read_process(handle, host + 0x19E8, 8)
            )[0]
        except Exception:
            selected_id64 = 0
            selected_id64_known = False
        if supplied_pos is None:
            hx, hy, hz = struct.unpack(
                "<fff", read_process(handle, host + OBJ_POS_OFF, 12)
            )
            pos_source = "rpm_host"
        else:
            hx, hy, hz = supplied_pos
            pos_source = "supplied"
        if not all(math.isfinite(v) and abs(v) <= 500000.0 for v in (hx, hy, hz)):
            raise RuntimeError("invalid_host_pos")
        scene = _read_u32(handle, mid + 0x0C)
        if not _valid_ptr(scene):
            raise RuntimeError(f"invalid_scene:0x{scene:08X}")
        manager = _read_u32(handle, scene + NPC_MANAGER_OFF)
        if not _valid_ptr(manager):
            raise RuntimeError(f"invalid_npc_manager:0x{manager:08X}")

        count = _read_u32(handle, manager + NPC_ARRAY_COUNT_OFF)
        if count > WANZI_AOI_MAX_OBJECTS:
            raise RuntimeError(f"invalid_class2_count:{count}")
        if count == 0:
            return {
                "known": True,
                "has_monster": False,
                "reason": "empty_class2",
                "count": 0,
                "radius": radius_f,
                "pos_source": pos_source,
                "selected_id64": int(selected_id64),
                "selected_id64_known": bool(selected_id64_known),
            }
        buckets = _read_u32(handle, manager + NPC_HASH_BUCKETS_OFF)
        bucket_count = _read_u32(handle, manager + NPC_HASH_BUCKET_COUNT_OFF)
        if not _valid_ptr(buckets):
            raise RuntimeError(f"invalid_class2_buckets:0x{buckets:08X}")
        if not 1 <= bucket_count <= NPC_HASH_BUCKETS_MAX:
            raise RuntimeError(f"invalid_class2_bucket_count:{bucket_count}")
        # Container layout recovered from GetObjects/0x4AFB10 iterator:
        # bucket head -> node{+0 next, +4 CECNPC*}.
        raw = read_process(handle, buckets, int(bucket_count) * 4)
        heads = struct.unpack(f"<{int(bucket_count)}I", raw)
        radius_sq = radius_f * radius_f
        checked = 0
        monsters = 0
        nearby_ids = []
        nearby_objects = 0
        nearest_dist_sq = float("inf")
        nearest_object = 0
        read_errors = 0
        visited: set[int] = set()
        node_limit = max(64, int(count) + 32)
        for head in heads:
            node = int(head)
            while _valid_ptr(node) and node not in visited:
                if len(visited) >= node_limit:
                    raise RuntimeError(
                        f"class2_node_overflow:{len(visited)}/{count}"
                    )
                visited.add(node)
                try:
                    next_node, obj = struct.unpack(
                        "<II", read_process(handle, node, 8)
                    )
                except Exception:
                    read_errors += 1
                    break
                node = int(next_node)
                if not _valid_ptr(obj):
                    continue
                try:
                    vft = _read_u32(handle, obj)
                except Exception:
                    read_errors += 1
                    continue
                if vft != CECNPC_MONSTER_VFT:
                    continue
                monsters += 1

                try:
                    pos_raw = read_process(handle, int(obj) + OBJ_POS_OFF, 12)
                    x, y, z = struct.unpack("<fff", pos_raw)
                except Exception:
                    read_errors += 1
                    continue
                if not all(math.isfinite(v) and abs(v) <= 500000.0 for v in (x, y, z)):
                    continue
                checked += 1
                dist_sq = (x - hx) ** 2 + (y - hy) ** 2 + (z - hz) ** 2
                if dist_sq <= radius_sq:
                    nearby_objects += 1
                    try:
                        ident = struct.unpack("<Q", read_process(handle, int(obj) + OBJ_ID64_OFF, 8))[0]
                    except Exception:
                        read_errors += 1
                        ident = 0
                    if ident:
                        nearby_ids.append(int(ident))
                    if dist_sq < nearest_dist_sq:
                        nearest_dist_sq = dist_sq
                        nearest_object = int(obj)
        return {
            "known": True,
            "has_monster": bool(nearby_objects),
            "reason": "nearby_monster" if nearby_objects else "no_nearby_monster",
            "count": int(count),
            "monsters": monsters,
            "checked": checked,
            "read_errors": read_errors,
            "nearest_m": math.sqrt(nearest_dist_sq) if nearby_objects else None,
            "nearby_ids": nearby_ids,
            "object": nearest_object or None,
            "selected_id64": int(selected_id64),
            "selected_id64_known": bool(selected_id64_known),
            "pos_source": pos_source,
        }
    except Exception as exc:
        return {
            "known": False,
            "has_monster": False,
            "reason": str(exc),
            "count": 0,
            "radius": radius_f,
            "selected_id64": 0,
            "selected_id64_known": False,
        }
    finally:
        if handle:
            kernel32.CloseHandle(wintypes.HANDLE(handle))


def probe_wanzi_control_rpm(pid: int) -> dict:
    """Read the host session-control triplet without hooks or remote calls."""
    pid = int(pid)
    handle = 0
    try:
        handle = open_process(pid)
        root = _read_u32(handle, ROOT_GLOBAL)
        if not _valid_ptr(root):
            raise RuntimeError(f"invalid_root:0x{root:08X}")
        mid = _read_u32(handle, root + 0x24)
        if not _valid_ptr(mid):
            raise RuntimeError(f"invalid_mid:0x{mid:08X}")
        host = _read_u32(handle, mid + HOST_SIDE_OFF)
        if not _valid_ptr(host):
            raise RuntimeError(f"invalid_host:0x{host:08X}")
        gate0, gate1, gate2 = struct.unpack(
            "<III", read_process(handle, host + HOST_SESSION_GATE_OFF, 12)
        )
        return {
            "known": True,
            "reason": "control_triplet",
            "host": int(host),
            "gate0": int(gate0),
            "gate1": int(gate1),
            "gate2": int(gate2),
        }
    except Exception as exc:
        return {
            "known": False,
            "reason": str(exc),
            "gate0": 0,
            "gate1": 0,
            "gate2": 0,
        }
    finally:
        if handle:
            kernel32.CloseHandle(wintypes.HANDLE(handle))


def set_wanzi_control_state(pid: int, state: dict | None) -> dict:
    """Publish one scheduler-owned control-state cache value."""
    value = dict(state or {})
    value.update(pid=int(pid), updated_at=time.monotonic())
    value["known"] = bool(value.get("known"))
    value["active"] = bool(value.get("active"))
    value["blocked"] = bool(value.get("blocked"))
    with _WANZI_CONTROL_LOCK:
        _WANZI_CONTROL_STATE[int(pid)] = value
    return dict(value)


def update_wanzi_control_sample(
    pid: int,
    sample: dict | None,
    *,
    recovery_s: float = WANZI_CONTROL_RECOVERY_S,
) -> dict:
    """Merge one control sample and retain a short post-control safety fence."""
    pid = int(pid)
    now = time.monotonic()
    incoming = dict(sample or {})
    previous = get_wanzi_control_state(pid)
    known = bool(incoming.get("known"))
    gate0 = int(incoming.get("gate0") or 0)
    gate1 = int(incoming.get("gate1") or 0)
    gate2 = int(incoming.get("gate2") or 0)
    active = bool(
        known
        and gate0 == WANZI_CONTROL_GATE0
        and gate2 in WANZI_CONTROL_GATE2_VALUES
    )
    try:
        recovery = max(0.0, float(recovery_s))
    except Exception:
        recovery = WANZI_CONTROL_RECOVERY_S

    prev_decision_at = float(previous.get("decision_at") or 0.0)
    prev_fresh = bool(
        prev_decision_at > 0.0
        and now - prev_decision_at <= WANZI_CONTROL_MAX_AGE_S
    )
    prev_recover_until = float(previous.get("recover_until") or 0.0)
    value = dict(incoming)
    value.update(
        pid=pid,
        updated_at=now,
        sample_known=known,
        active=False,
        blocked=False,
        cached=False,
        recover_until=prev_recover_until,
        decision_at=prev_decision_at,
    )

    if active:
        value.update(
            known=True,
            active=True,
            blocked=True,
            recover_until=0.0,
            decision_at=now,
            reason=f"control_5_x_{gate2}",
        )
    elif known:
        recover_until = prev_recover_until
        if previous.get("active"):
            recover_until = now + recovery
        blocked = now < recover_until
        value.update(
            known=True,
            active=False,
            blocked=blocked,
            recover_until=recover_until if blocked else 0.0,
            decision_at=now,
            reason="control_recovery" if blocked else "control_clear",
        )
    elif prev_fresh and previous.get("active"):
        # A single failed RPM must not open the sender in the middle of a
        # captured control animation.  The cache automatically expires.
        value.update(
            known=True,
            active=True,
            blocked=True,
            cached=True,
            gate0=int(previous.get("gate0") or 0),
            gate1=int(previous.get("gate1") or 0),
            gate2=int(previous.get("gate2") or 0),
            recover_until=0.0,
            decision_at=prev_decision_at,
            reason="control_cached_active",
        )
    elif now < prev_recover_until:
        value.update(
            known=True,
            active=False,
            blocked=True,
            cached=True,
            recover_until=prev_recover_until,
            reason="control_cached_recovery",
        )
    else:
        value.update(
            known=False,
            active=False,
            blocked=False,
            recover_until=0.0,
            reason=incoming.get("reason") or "control_unknown",
        )

    with _WANZI_CONTROL_LOCK:
        _WANZI_CONTROL_STATE[pid] = value
    return dict(value)


def clear_wanzi_control_state(pid: int) -> None:
    with _WANZI_CONTROL_LOCK:
        _WANZI_CONTROL_STATE.pop(int(pid), None)


def get_wanzi_control_state(pid: int) -> dict:
    with _WANZI_CONTROL_LOCK:
        value = dict(_WANZI_CONTROL_STATE.get(int(pid)) or {})
    if value:
        now = time.monotonic()
        value["age_s"] = max(
            0.0, now - float(value.get("updated_at") or 0.0)
        )
        decision_at = float(value.get("decision_at") or 0.0)
        value["decision_age_s"] = (
            max(0.0, now - decision_at) if decision_at > 0.0 else 1e9
        )
        value["recovery_left_s"] = max(
            0.0, float(value.get("recover_until") or 0.0) - now
        )
    return value


def wanzi_control_can_send(
    pid: int,
    *,
    max_age_s: float = WANZI_CONTROL_MAX_AGE_S,
) -> bool:
    """Non-blocking consumer for the scheduler-produced control cache."""
    state = get_wanzi_control_state(pid)
    if state.get("active"):
        decision_age = state.get("decision_age_s")
        if decision_age is None:
            decision_age = 1e9
        return float(decision_age) > max(0.05, float(max_age_s))
    if float(state.get("recovery_left_s") or 0.0) > 0.0:
        return False
    return True


def set_wanzi_aoi_state(pid: int, state: dict | None) -> dict:
    """Publish one scheduler-produced AOI result."""
    value = dict(state or {})
    value.update(pid=int(pid), updated_at=time.monotonic())
    value["known"] = bool(value.get("known"))
    value["has_monster"] = bool(value.get("has_monster"))
    with _WANZI_AOI_LOCK:
        _WANZI_AOI_STATE[int(pid)] = value
    return dict(value)


def update_wanzi_aoi_sample(
    pid: int,
    sample: dict | None,
    *,
    no_monster_confirm: int = WANZI_AOI_NO_MONSTER_CONFIRM,
) -> dict:
    """Merge one AOI sample into a smooth tri-state decision cache.

    Positive monster samples open immediately.  Absence must be confirmed by
    consecutive clean samples.  Transient unknown reads retain the last stable
    decision briefly; with no usable cache, unknown defaults to send.  A
    ``hard_block`` sample (scene load/transition) always blocks immediately.
    """
    pid = int(pid)
    now = time.monotonic()
    incoming = dict(sample or {})
    previous = get_wanzi_aoi_state(pid)
    sample_known = bool(incoming.get("known"))
    sample_has_monster = bool(incoming.get("has_monster"))
    hard_block = bool(incoming.get("hard_block"))
    scene_stable = bool(incoming.get("scene_stable"))
    try:
        confirm_n = max(1, int(no_monster_confirm))
    except Exception:
        confirm_n = WANZI_AOI_NO_MONSTER_CONFIRM

    prev_decision_at = float(previous.get("decision_at") or 0.0)
    prev_decision_fresh = bool(
        previous.get("known")
        and prev_decision_at > 0.0
        and now - prev_decision_at <= WANZI_AOI_MAX_AGE_S
    )
    value = dict(incoming)
    value.update(
        pid=pid,
        updated_at=now,
        sample_known=sample_known,
        sample_has_monster=sample_has_monster,
        hard_block=hard_block,
        scene_stable=scene_stable,
        cached=False,
    )

    if hard_block:
        value.update(
            known=False,
            has_monster=False,
            no_monster_streak=0,
            decision_at=prev_decision_at,
        )
    elif previous.get("hard_block") and not (scene_stable or sample_known):
        # A missing/failed read must not accidentally reopen the sender while
        # the last authoritative scene signal says the map is transitioning.
        value.update(
            known=False,
            has_monster=False,
            hard_block=True,
            no_monster_streak=0,
            decision_at=prev_decision_at,
            cached=True,
        )
    elif sample_known and sample_has_monster:
        value.update(
            known=True,
            has_monster=True,
            no_monster_streak=0,
            decision_at=now,
        )
    elif sample_known:
        prev_no_monster = bool(
            previous.get("sample_known")
            and not previous.get("sample_has_monster")
            and not previous.get("hard_block")
        )
        streak = int(previous.get("no_monster_streak") or 0) + 1 if prev_no_monster else 1
        if streak >= confirm_n:
            value.update(
                known=True,
                has_monster=False,
                no_monster_streak=streak,
                decision_at=now,
            )
        elif prev_decision_fresh:
            value.update(
                known=True,
                has_monster=bool(previous.get("has_monster")),
                no_monster_streak=streak,
                decision_at=prev_decision_at,
                cached=True,
            )
        else:
            value.update(
                known=False,
                has_monster=False,
                no_monster_streak=streak,
                decision_at=prev_decision_at,
            )
    elif prev_decision_fresh:
        value.update(
            known=True,
            has_monster=bool(previous.get("has_monster")),
            no_monster_streak=int(previous.get("no_monster_streak") or 0),
            decision_at=prev_decision_at,
            cached=True,
        )
    else:
        value.update(
            known=False,
            has_monster=False,
            no_monster_streak=0,
            decision_at=prev_decision_at,
        )

    selected_known = bool(value.get("selected_id64_known", True)) if sample_known else bool(previous.get("selected_id64_known", True))
    selected_raw = value.get("selected_id64")
    selected_id64 = int(selected_raw or 0) if sample_known else int(previous.get("selected_id64") or 0)
    value["selected_id64_known"] = selected_known
    value["selected_id64"] = selected_id64
    no_selected_since = previous.get("no_selected_since")
    if value.get("known") and value.get("has_monster") and selected_known and selected_id64 == 0:
        if not no_selected_since:
            no_selected_since = now
        value["no_selected_since"] = float(no_selected_since)
        no_selected_age = max(0.0, now - float(no_selected_since))
        value["no_selected_age_s"] = no_selected_age
    else:
        value["no_selected_since"] = None
        value["no_selected_age_s"] = 0.0

    nearest_m = value.get("nearest_m") if sample_known else previous.get("nearest_m")
    previous_nearest_m = previous.get("nearest_m")
    distance_valid = False
    try:
        distance_valid = nearest_m is not None and math.isfinite(float(nearest_m))
    except (TypeError, ValueError):
        distance_valid = False
    distance_static = False
    if value.get("known") and value.get("has_monster") and distance_valid:
        if previous_nearest_m is None:
            static_since = now
        else:
            try:
                static_since = previous.get("aoi_static_since")
                if abs(float(nearest_m) - float(previous_nearest_m)) > WANZI_AOI_STATIC_DISTANCE_EPSILON_M:
                    static_since = now
                elif not static_since:
                    static_since = now
            except (TypeError, ValueError):
                static_since = now
        value["aoi_static_since"] = float(static_since)
        static_age = max(0.0, now - float(static_since))
        value["aoi_static_age_s"] = static_age
        distance_static = static_age >= WANZI_AOI_STATIC_INTERRUPT_S
    else:
        value["aoi_static_since"] = None
        value["aoi_static_age_s"] = 0.0

    value["aoi_distance_static"] = distance_static
    value["interrupt_no_selected"] = bool(
        value.get("no_selected_age_s", 0.0) >= WANZI_NO_SELECTED_INTERRUPT_S
        and distance_static
    )

    with _WANZI_AOI_LOCK:
        _WANZI_AOI_STATE[pid] = value
    return dict(value)


def clear_wanzi_aoi_state(pid: int) -> None:
    with _WANZI_AOI_LOCK:
        _WANZI_AOI_STATE.pop(int(pid), None)


def get_wanzi_aoi_state(pid: int) -> dict:
    with _WANZI_AOI_LOCK:
        value = dict(_WANZI_AOI_STATE.get(int(pid)) or {})
    if value:
        value["age_s"] = max(0.0, time.monotonic() - float(value.get("updated_at") or 0.0))
        decision_at = float(value.get("decision_at") or 0.0)
        value["decision_age_s"] = (
            max(0.0, time.monotonic() - decision_at) if decision_at > 0.0 else 1e9
        )
    return value


def wanzi_aoi_can_send(pid: int, *, max_age_s: float = WANZI_AOI_MAX_AGE_S) -> bool:
    """Non-blocking consumer: unknown sends; stable no-monster/scene load blocks."""
    state = get_wanzi_aoi_state(pid)
    if state.get("hard_block"):
        return False
    if state.get("interrupt_no_selected"):
        return False
    if not state.get("known"):
        return True
    decision_age = state.get("decision_age_s")
    if decision_age is None:
        decision_age = 1e9
    if float(decision_age) > max(0.05, float(max_age_s)):
        return True
    return bool(state.get("has_monster"))


def _remote_stub(packet_address: int) -> bytes:
    """Build the 24-byte remote-call stub used by the direct sender."""
    code = (
        b"\x6A\x26"                         # push 38
        + b"\x68" + _p32(packet_address)     # push remote packet bytes
        + b"\x8B\x15" + _p32(ROOT_GLOBAL)   # mov edx,[root global]
        + b"\x8B\x4A\x2C"                  # mov ecx,[edx+0x2c]
        + b"\xB8" + _p32(SEND_TARGET)        # mov eax,system command fn
        + b"\xFF\xD0\xC3"                  # call eax; ret
    )
    assert len(code) == 24
    return code


def _trace_wanzi_send(pid: int, payload: bytes, event: str, result: int | None = None) -> None:
    trace_dir = os.environ.get("WANZI_PACKET_TRACE_DIR", "").strip()
    if not trace_dir:
        trace_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".issues", "packets", "wanzi_send"))
    try:
        path = os.path.join(trace_dir, f"wanzi_send_{int(pid)}.jsonl")
        os.makedirs(trace_dir, exist_ok=True)
        record = {
            "t": time.time(),
            "event": event,
            "pid": int(pid),
            "length": len(payload),
            "head_hex": payload[:16].hex().upper(),
        }
        if result is not None:
            record["result"] = int(result)
        with open(path, "a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return


def _send_packet(pid: int, payload: bytes, *, timeout_ms: int = 3000) -> int:
    """Send one P1 or P2 packet. Never free code while its thread is live."""
    pid = int(pid)
    remote = 0
    thread = 0
    finished = False
    handle = 0
    try:
        ensure_pid_remote_callable(pid)
        # Share the app-wide Call mutex: package sends never race other CRT work.
        with pid_call_mutex(pid, timeout_ms=1000, namespace="Call"):
            ensure_pid_remote_callable(pid)
            handle = open_process(pid)
            prefix = read_process(handle, SEND_TARGET, len(ORIGINAL_TARGET_PREFIX))
            if prefix != ORIGINAL_TARGET_PREFIX:
                raise RuntimeError(
                    f"0x{SEND_TARGET:08X} is hooked ({prefix.hex().upper()}); refusing send"
                )
            remote = int(
                kernel32.VirtualAllocEx(
                    wintypes.HANDLE(handle),
                    None,
                    0x1000,
                    MEM_COMMIT | MEM_RESERVE,
                    PAGE_EXECUTE_READWRITE,
                )
                or 0
            )
            if not remote:
                raise OSError(f"VirtualAllocEx failed err={ctypes.get_last_error()}")
            write_process(handle, remote, payload)
            stub = remote + 0x100
            code = _remote_stub(remote)
            write_process(handle, stub, code)
            kernel32.FlushInstructionCache(
                wintypes.HANDLE(handle), ctypes.c_void_p(stub), len(code)
            )
            tid = wintypes.DWORD()
            thread_h = kernel32.CreateRemoteThread(
                wintypes.HANDLE(handle),
                None,
                0,
                ctypes.c_void_p(stub),
                None,
                0x4,  # CREATE_SUSPENDED, matching the recovered chain
                ctypes.byref(tid),
            )
            if not thread_h:
                raise OSError(f"CreateRemoteThread failed err={ctypes.get_last_error()}")
            thread = int(thread_h)
            if kernel32.ResumeThread(wintypes.HANDLE(thread)) == 0xFFFFFFFF:
                raise OSError(f"ResumeThread failed err={ctypes.get_last_error()}")
            finished = wait_remote_thread_safely(
                thread,
                int(timeout_ms),
                operation="wanzi packet",
                pid=pid,
            )
            if not finished:
                raise TimeoutError("丸子远程线程超时；已保留远程内存并停止发送")
            result = wintypes.DWORD()
            if not kernel32.GetExitCodeThread(wintypes.HANDLE(thread), ctypes.byref(result)):
                raise OSError(f"GetExitCodeThread failed err={ctypes.get_last_error()}")
            send_result = int(result.value)
            _trace_wanzi_send(pid, payload, "send_return", send_result)
            return send_result
    except Exception as exc:
        note_remote_os_error(pid, exc)
        raise
    finally:
        if thread:
            kernel32.CloseHandle(wintypes.HANDLE(thread))
        # Timeout deliberately leaks the page: the remote code may still execute.
        if remote and finished and handle:
            kernel32.VirtualFreeEx(wintypes.HANDLE(handle), ctypes.c_void_p(remote), 0, MEM_RELEASE)
        if handle:
            kernel32.CloseHandle(wintypes.HANDLE(handle))


class WanziPacketRunner:
    """One pid, one serial P1/P2 loop with low-rate and active phases."""

    ACTIVE_WINDOW_S = WANZI_ACTIVE_WINDOW_S
    LOW_RATE_SECONDS = WANZI_LOW_RATE_SECONDS
    LOW_RATE_PAIRS_PER_SECOND = WANZI_LOW_RATE_PAIRS_PER_SECOND

    def __init__(
        self,
        pid: int,
        interval_ms: int,
        *,
        kind: str = WANZI_KIND_WAIGONG,
        p1_payload: bytes | None = None,
        log: LogFn | None = None,
        can_send: Callable[[], bool] | None = None,
        low_rate_seconds: float = LOW_RATE_SECONDS,
        low_rate_pairs_per_second: float = LOW_RATE_PAIRS_PER_SECOND,
        active_window_s: float = ACTIVE_WINDOW_S,
    ):
        self.pid = int(pid)
        self.interval_ms = max(1, int(interval_ms))
        self.low_rate_seconds = max(0.0, float(low_rate_seconds))
        self.low_rate_pairs_per_second = max(0.1, float(low_rate_pairs_per_second))
        self.low_rate_interval_s = 1.0 / self.low_rate_pairs_per_second
        self.active_window_s = max(1.0, float(active_window_s))
        self.kind = (
            WANZI_KIND_NEIGONG
            if str(kind or "").strip().lower() == WANZI_KIND_NEIGONG
            else WANZI_KIND_WAIGONG
        )
        default_p1 = P1_NEIGONG if self.kind == WANZI_KIND_NEIGONG else P1_WAIGONG
        if p1_payload is None:
            self.p1 = default_p1
        else:
            self.p1 = bytes(p1_payload)
            if len(self.p1) != len(default_p1):
                raise ValueError(f"invalid wanzi P1 length: {len(self.p1)}")
        self._log = log or (lambda _m: None)
        self._can_send = can_send
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stats: dict = {
            "pid": self.pid,
            "running": False,
            "interval_ms": self.interval_ms,
            "kind": self.kind,
            "phase": "low_rate",
            "low_rate_seconds": self.low_rate_seconds,
            "low_rate_pairs_per_second": self.low_rate_pairs_per_second,
            "low_rate_interval_ms": int(round(self.low_rate_interval_s * 1000.0)),
            "active_window_s": self.active_window_s,
            "low_rate_pairs": 0,
            "active_pairs": 0,
            "pairs": 0,
            "p1_ok": 0,
            "p2_ok": 0,
            "gate_skips": 0,
            "error": "",
            "started_at": 0.0,
            "stopped_at": 0.0,
        }

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._stats.get("running"))

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        with self._lock:
            self._stats.update(running=True, error="", started_at=time.monotonic(), stopped_at=0.0)
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"wanzi-packet-{self.pid}"
        )
        self._thread.start()
        return True

    def stop(self, timeout_s: float = 4.0) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout_s)))
        return not bool(thread is not None and thread.is_alive())

    def _wait(self, seconds: float) -> bool:
        return bool(self._stop.wait(max(0.0, float(seconds))))

    def _record_pair(self, p1: int, p2: int, *, phase: str) -> None:
        with self._lock:
            self._stats["pairs"] = int(self._stats["pairs"]) + 1
            pairs = int(self._stats["pairs"])
            self._stats["p1_ok"] = int(self._stats["p1_ok"]) + (1 if p1 == 1 else 0)
            self._stats["p2_ok"] = int(self._stats["p2_ok"]) + (1 if p2 == 1 else 0)
            pair_key = "low_rate_pairs" if phase == "low_rate" else "active_pairs"
            self._stats[pair_key] = int(self._stats[pair_key]) + 1
        if pairs == 1 or p1 != 1 or p2 != 1:
            self._log(
                f"丸子发包回执 pid={self.pid} pair={pairs} phase={phase} "
                f"p1={p1} p2={p2}"
            )

    def _set_phase(self, phase: str) -> None:
        with self._lock:
            self._stats["phase"] = str(phase)

    def _gate_open(self) -> bool:
        if self._can_send is None:
            return True
        try:
            return bool(self._can_send())
        except Exception:
            return False

    def _wait_for_gate(self) -> bool:
        """Return True when stopping; otherwise wait without scanning."""
        with self._lock:
            self._stats["gate_skips"] = int(self._stats["gate_skips"]) + 1
        return self._wait(WANZI_AOI_GATE_WAIT_S)

    def _send_pair(self, *, phase: str) -> None:
        p1 = _send_packet(self.pid, self.p1)
        p2 = _send_packet(self.pid, P2)
        self._record_pair(p1, p2, phase=phase)
        if p1 != 1 or p2 != 1:
            raise RuntimeError(f"丸子返回异常 p1={p1} p2={p2}")

    def _run_low_rate_step(self) -> bool:
        """Run one low-rate pair, or briefly wait for the scheduler gate."""
        if not self._gate_open():
            return self._wait_for_gate()
        self._send_pair(phase="low_rate")
        return self._wait(self.low_rate_interval_s)

    def _run_active_step(self) -> bool:
        """Send exactly one complete P1/P2 pair, then wait the user interval."""
        if not self._gate_open():
            return self._wait_for_gate()
        self._send_pair(phase="active")
        return self._wait(self.interval_ms / 1000.0)

    def _run(self) -> None:
        try:
            phase = "low_rate"
            phase_started_at = time.monotonic()
            self._set_phase(phase)
            while not self._stop.is_set():
                now = time.monotonic()
                if phase == "low_rate":
                    if now - phase_started_at >= self.low_rate_seconds:
                        phase = "active"
                        phase_started_at = now
                        self._set_phase(phase)
                        continue
                    if self._run_low_rate_step():
                        return
                    continue
                if now - phase_started_at >= self.active_window_s:
                    phase = "low_rate"
                    phase_started_at = now
                    self._set_phase(phase)
                    continue
                if self._run_active_step():
                    return
        except Exception as exc:
            with self._lock:
                self._stats["error"] = str(exc)
            self._log(f"丸子发包停止 pid={self.pid}: {exc}")
        finally:
            with self._lock:
                self._stats["running"] = False
                self._stats["stopped_at"] = time.monotonic()


__all__ = [
    "P1", "P1_WAIGONG", "P1_NEIGONG", "P2",
    "P1_PACKAGE_OFF", "P1_SLOT_OFF", "build_wanzi_p1",
    "WANZI_KIND_WAIGONG", "WANZI_KIND_NEIGONG",
    "WANZI_ACTIVE_WINDOW_S",
    "WANZI_LOW_RATE_SECONDS", "WANZI_LOW_RATE_PAIRS_PER_SECOND",
    "ROOT_GLOBAL", "SEND_TARGET", "ORIGINAL_TARGET_PREFIX",
    "WANZI_AOI_SCAN_INTERVAL_S", "WANZI_AOI_MAX_AGE_S",
    "WANZI_AOI_GATE_WAIT_S", "WANZI_AOI_DEFAULT_RADIUS",
    "WANZI_AOI_NO_MONSTER_CONFIRM", "WANZI_NO_SELECTED_INTERRUPT_S",
    "WANZI_AOI_STATIC_DISTANCE_EPSILON_M", "WANZI_AOI_STATIC_INTERRUPT_S",
    "WANZI_CONTROL_SCAN_INTERVAL_S", "WANZI_CONTROL_MAX_AGE_S",
    "WANZI_CONTROL_RECOVERY_S", "WANZI_CONTROL_GATE0",
    "WANZI_CONTROL_GATE2_HIT_FLY", "WANZI_CONTROL_GATE2_GROUND_RECOVERY",
    "WANZI_CONTROL_GATE2_VALUES", "HOST_SESSION_GATE_OFF",
    "MONSTER_HP_OFF", "MONSTER_DEAD_MARKER_OFF",
    "probe_nearby_class2_rpm", "set_wanzi_aoi_state",
    "update_wanzi_aoi_sample", "clear_wanzi_aoi_state",
    "get_wanzi_aoi_state", "wanzi_aoi_can_send",
    "probe_wanzi_control_rpm", "set_wanzi_control_state",
    "update_wanzi_control_sample", "clear_wanzi_control_state",
    "get_wanzi_control_state", "wanzi_control_can_send",
    "WanziPacketRunner",
]
