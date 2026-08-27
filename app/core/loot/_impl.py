# -*- coding: utf-8 -*-
"""
Super loot: interact with a named matter (e.g. 福利活动宝箱).

Chests need open/cast-bar (AutoClickMatter), not ground PickItem.

Rule:
  - target within interact_range -> open (AutoClickMatter / SetTarget)
  - beyond range -> HostMove, wait in range, then open

Bridge UI-thread is required for open; PickItem alone does not open chests.

@author by ak
"""
from __future__ import annotations

import math
import random
import struct
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from app.core.automove import PathTarget, host_move_to, read_scene_position
from app.core.remote_runtime import remote_read_bytes
from app.core.runner import RunnerLifecycle, interruptible_sleep
from app.core.chest_pathfind import (
    CHEST_TID_WELFARE,
    DEFAULT_CLUSTER_RADIUS,
    DEFAULT_ISOLATED_MAX_ADJ,
    DEFAULT_PLAYER_RADIUS,
    ChestInfo,
    count_players_near,
    is_isolated_chest,
    is_welfare_chest,
    list_player_positions,
    pick_target_by_neighbor_density,
)
from app.core.game_attach import GameAttachSession
from app.core.plg_exports import (
    CAST_FIELD_ELAPSED_OFF,
    CAST_FIELD_EXTRA_FLAGS_OFF,
    CAST_FIELD_FLAGS_OFF,
    CAST_FIELD_SKILL_ID_B_OFF,
    CAST_FIELD_SKILL_ID_OFF,
    HOST_SESSION_STATE_OFF,
)
from app.core.plg_interact import InteractTarget, get_object_id64, pick_item, set_target
from app.core.plg_objects import CLASS_MATTER, get_object_count, list_class_objects
from app.core.xajh_bridge import (
    CMD_AUTO_CLICK_DYN_MATTER,
    CMD_AUTO_CLICK_MATTER,
    CMD_HOST_MOVE,
    CMD_PICK_ITEM,
    CMD_PING,
    CMD_SET_TARGET,
    ensure_bridge,
)

LogFn = Callable[[str], None]

# Defaults aligned with live chest recon
DEFAULT_ITEM_NAME = "福利活动宝箱"
# Hard stand-open only. Live: ~2.5m always gets real cast; 3.x–4.x often
# MatterInteract-ok with no cast bar (wastes grace + skip). Beyond this: walk
# in first, then open — never soft stand-open at 3–5m.
DEFAULT_PICK_RANGE = 2.5
# Absolute face priority: d <= this always opens first, even if isolated.
DEFAULT_STAND_OPEN_PRIORITY_M = 2.5
DEFAULT_SCAN_RADIUS = 80.0
# GetObjects cap is 512; keep match list small for dense maps (10000+ chests).
DEFAULT_MATTER_LIMIT = 64
DEFAULT_ARRIVE_TIMEOUT_S = 18.0
DEFAULT_LOOP_IDLE_S = 0.35
# A chest interaction must still wait for its real cast bar to finish.
# Retain the historically used start window; cache chaining, not this timer,
# is what made the earlier collector continuous.
DEFAULT_CAST_START_GRACE_S = 1.2
DEFAULT_CAST_WAIT_S = 6.5  # upper bound only; returns as soon as cast is idle
# Consecutive active samples before trusting cast-start (debounce false spikes).
DEFAULT_CAST_ACTIVE_HITS = 2
# Consecutive idle samples after active before treating cast as finished.
DEFAULT_CAST_IDLE_HITS = 2
DEFAULT_CAST_POLL_S = 0.12
# A path is complete only after consecutive in-range samples are stationary.
DEFAULT_ARRIVE_STOP_DELTA_M = 0.20
DEFAULT_ARRIVE_STOP_HITS = 2
# After cast finished: near-zero gap before next open/path (same pocket).
DEFAULT_LOCAL_OPEN_IDLE_S = 0.05
# Consecutive CRT/attach hard fails before runner backs off / reattaches
DEFAULT_SCAN_FAIL_BACKOFF_S = 5.0
DEFAULT_SCAN_FAIL_REATTACH_N = 2
# Dense map: throttle full AOI scan only (not local open chain).
# Public chest fields commonly contain ~280 matters. Treat them as dense so
# the scanner uses the bounded 16-candidate / 12-template-read path instead
# of spending ~20 seconds inspecting a 64-candidate sparse shortlist.
DEFAULT_DENSE_COUNT = 128
DEFAULT_DENSE_LOOP_IDLE_S = 2.0  # idle only when full_scan / no local
DEFAULT_DENSE_SCAN_INTERVAL_S = 6.0  # min seconds between full GetObjects
DEFAULT_SPARSE_SCAN_INTERVAL_S = 1.8
# Soft TTL: only used when cache has no guideable work left near host.
DEFAULT_LOCAL_CACHE_TTL_S = 8.0  # face-local list max age; public chests turn over fast
DEFAULT_LOCAL_SKIP_SCAN_N = 1  # if >=N guideable remain, skip full scan
# Left the working pocket (no nearby cache hits) => force rescan.
# Walking inside one chest field must NOT force rescan every few meters.
DEFAULT_CACHE_HOST_MOVE_M = 40.0
DEFAULT_DENSE_MATTER_LIMIT = 16  # public dense: small shortlist, rescan often
DEFAULT_DENSE_BATCH_IDLE_S = 1.5
DEFAULT_DENSE_BATCH_RESET_MOVE_M = 8.0
# After VirtualAllocEx err=5: no full GetObjects / GetObjectCount for this long
DEFAULT_CRT_COOLDOWN_S = 25.0
DEFAULT_REATTACH_MAX = 3  # then stop if process still dead
DEFAULT_BRIDGE_COOLDOWN_S = 8.0  # after OpenFileMapping fail / 桥接未就绪
DEFAULT_BRIDGE_REINJECT_MAX = 2  # auto reinject attempts per collapse
# Manual fly / teleport: pause open+CRT so we don't interact mid-air.
DEFAULT_MOTION_PAUSE = True
DEFAULT_MOTION_SPEED_MPS = 18.0  # horizontal m/s between samples
DEFAULT_MOTION_JUMP_M = 26.0  # single-sample XZ jump ( > near_prefer path)
DEFAULT_MOTION_Y_JUMP_M = 8.0  # vertical spike (air / hard drop)
DEFAULT_MOTION_PAUSE_S = 2.5  # hold after motion; no open / no CRT
DEFAULT_MOTION_POLL_S = 0.35  # sleep slice while motion-paused
# Sentinel: resolve from GetCurrentScenePosition.scene_id (dev panel path mode)
DEFAULT_MOVE_MODE = -1
DEFAULT_MOVE_NUDGE_S = 4.0  # re-issue HostMove if position stuck (far path)
DEFAULT_NEAR_MOVE_NUDGE_S = 1.2  # soft-walk / short hop: re-issue sooner
DEFAULT_MAX_STUCK_NUDGES = 3  # then skip chest (HostMove ret=1 can still mean no walk)
DEFAULT_NEAR_MAX_STUCK_NUDGES = 2
DEFAULT_NEAR_ARRIVE_TIMEOUT_S = 8.0  # short hops should not burn full 18s
# Soft band disabled for open: live 3.x stand-open is no_cast spam. Keep 0 so
# soft_pr == pick_range; path always when d > pick_range.
DEFAULT_SOFT_OPEN_EXTRA = 0.0
# Remaining distance band treated as "near hop" (faster nudge / shorter timeout).
DEFAULT_NEAR_HOP_EXTRA = 4.0
# Prefer any workable chest in front of host before chasing a far dense cluster.
# Soft band is only pick_range+1.5 (~5.5m); without this, dense@40m beats near@8–20m.
DEFAULT_NEAR_PREFER_PATH = 22.0
DEFAULT_CLUSTER_RADIUS_LOOT = DEFAULT_CLUSTER_RADIUS
DEFAULT_PLAYER_RADIUS_LOOT = DEFAULT_PLAYER_RADIUS
DEFAULT_ISOLATED_MAX_ADJ_LOOT = DEFAULT_ISOLATED_MAX_ADJ
# HostMove stuck / ret=0: soft skip this id/XZ only (dense open fields).
# Was 300s — false path-stuck + dual keys wiped whole shortlists for minutes.
DEFAULT_UNREACHABLE_S = 45.0
# A path failure can survive an AOI refresh with a new object id.  Exclude the
# same failed XZ and its immediate approach area, but not an entire dense field.
DEFAULT_UNREACHABLE_NEAR_M = 1.5
PATH_FAILURE_BACKOFF_S = (30.0, 90.0, 300.0, 900.0, 1800.0)
# Soft runtime bans below this remaining TTL are auto-purged when every scan hit
# is filtered (recover open fields). Manual right-click uses ~3600s; path-stuck
# was historically 300s so keep threshold above that.
SOFT_SKIP_MAX_REMAIN_S = 600.0
# Max path distance for a solitary (adj<=isolated_max) chest; farther => abandon
DEFAULT_MAX_ISOLATED_PATH = 28.0
# Path-stuck near host: short ban only (long TTL wiped whole fields).
DEFAULT_PATH_STUCK_NEAR_S = 12.0
DEFAULT_NO_CAST_RETRY_LIMIT = 3

# interact_mode
MODE_OPEN = "open"  # chest / gather cast-bar via AutoClickMatter
MODE_PICK = "pick"  # ground loot PickItem (legacy, not for chests)
MODE_AUTO = "auto"  # open if name/tid looks like chest, else pick


@dataclass
class SuperLootConfig:
    """
    Super-loot run parameters.

    @author by ak
    """

    item_name: str = DEFAULT_ITEM_NAME
    tid: int | None = CHEST_TID_WELFARE
    match_tid_fallback: bool = True
    pick_range: float = DEFAULT_PICK_RANGE
    scan_radius: float = DEFAULT_SCAN_RADIUS
    matter_limit: int = DEFAULT_MATTER_LIMIT
    arrive_timeout_s: float = DEFAULT_ARRIVE_TIMEOUT_S
    loop_idle_s: float = DEFAULT_LOOP_IDLE_S
    # Seconds after open-ok to observe the cast bar starting.
    cast_start_grace_s: float = DEFAULT_CAST_START_GRACE_S
    # Max cast bar wait after real start; memory idle ends the wait early.
    cast_wait_s: float = DEFAULT_CAST_WAIT_S
    # Entry-like interactions only need proof that the real cast has started;
    # their completion is verified by a subsequent game dialog instead.
    confirm_cast_start_only: bool = False
    # After cast finishes: tiny gap before next (do not re-apply cast_wait).
    local_open_idle_s: float = DEFAULT_LOCAL_OPEN_IDLE_S
    # -1 = auto from live scene_id (same as dev panel「读场景」填 mode)
    move_mode: int = DEFAULT_MOVE_MODE
    use_bridge: bool = True
    prefer_nearest: bool = True  # local in-range: nearest first
    # nearest | random — random picks among matches within random_pool_radius
    # (or pick_range when radius<=0). Used by 妖楼 multi-entry stands.
    select_mode: str = "nearest"
    # Candidate radius for select_mode=random (XZ meters). 0 => use pick_range.
    random_pool_radius: float = 0.0
    prefer_dense_when_far: bool = True  # far path: densest + fewer players
    # When nothing in soft range: still finish nearby work before far dense.
    near_prefer_path: float = DEFAULT_NEAR_PREFER_PATH
    cluster_radius: float = DEFAULT_CLUSTER_RADIUS_LOOT
    player_radius: float = DEFAULT_PLAYER_RADIUS_LOOT
    isolated_max_adj: int = DEFAULT_ISOLATED_MAX_ADJ_LOOT
    interact_mode: str = MODE_OPEN  # open | pick | auto
    use_dyn_click: bool = False
    # After open ok: short id skip only (opened chests despawn in-world; full
    # idle scan drops them). Avoid long blacklist / near-radius wipe.
    skip_opened_s: float = 3.0
    # MatterInteract ok but no cast/session proof: soft skip only (not path-stuck).
    # Do NOT use skip_unreachable_s / near wipe — false no_cast used to ban clusters.
    skip_no_cast_s: float = 4.0
    no_cast_retry_limit: int = DEFAULT_NO_CAST_RETRY_LIMIT
    # Legacy finite timeout, retained for callers that explicitly need it.
    skip_unreachable_s: float = DEFAULT_UNREACHABLE_S
    # Legacy finite near timeout, retained for callers that explicitly need it.
    path_stuck_near_s: float = DEFAULT_PATH_STUCK_NEAR_S
    filter_unreachable: bool = True
    # Drop other chests within this XZ radius of a stuck point
    unreachable_near_m: float = DEFAULT_UNREACHABLE_NEAR_M
    # Do not chase a solitary chest beyond this distance when no multi-cluster
    max_isolated_path: float = DEFAULT_MAX_ISOLATED_PATH
    # Dense-map scan throttle (avoid CRT storm when thousands of chests)
    dense_count: int = DEFAULT_DENSE_COUNT
    dense_loop_idle_s: float = DEFAULT_DENSE_LOOP_IDLE_S
    dense_scan_interval_s: float = DEFAULT_DENSE_SCAN_INTERVAL_S
    sparse_scan_interval_s: float = DEFAULT_SPARSE_SCAN_INTERVAL_S
    local_cache_ttl_s: float = DEFAULT_LOCAL_CACHE_TTL_S
    local_skip_scan_n: int = DEFAULT_LOCAL_SKIP_SCAN_N
    cache_host_move_m: float = DEFAULT_CACHE_HOST_MOVE_M
    dense_matter_limit: int = DEFAULT_DENSE_MATTER_LIMIT
    dense_batch_idle_s: float = DEFAULT_DENSE_BATCH_IDLE_S
    dense_batch_reset_move_m: float = DEFAULT_DENSE_BATCH_RESET_MOVE_M
    # If True: while pocket still has guideable cache hits, never full-scan
    skip_scan_while_local: bool = True
    # Pause full CRT scan after VirtualAllocEx / OpenProcess collapse
    crt_cooldown_s: float = DEFAULT_CRT_COOLDOWN_S
    reattach_max: int = DEFAULT_REATTACH_MAX
    bridge_cooldown_s: float = DEFAULT_BRIDGE_COOLDOWN_S
    bridge_reinject_max: int = DEFAULT_BRIDGE_REINJECT_MAX
    # Pause open + CRT while host is flying / teleporting / high-speed.
    motion_pause: bool = DEFAULT_MOTION_PAUSE
    motion_speed_mps: float = DEFAULT_MOTION_SPEED_MPS
    motion_jump_m: float = DEFAULT_MOTION_JUMP_M
    motion_y_jump_m: float = DEFAULT_MOTION_Y_JUMP_M
    motion_pause_s: float = DEFAULT_MOTION_PAUSE_S
    motion_poll_s: float = DEFAULT_MOTION_POLL_S

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanCache:
    """
    Last matching pocket plus bounded dense-scan progress.

    Hits are consumed without rescanning. Dense fields also retain inspected
    pointers so an empty 12-object batch advances instead of repeatedly
    checking the same nearest objects.
    @author by ak
    """

    hits: list[SuperLootTarget]
    host_pos: tuple[float, float, float] | None
    ts: float
    matter_count: int = 0
    dense: bool = False
    scene_id: int | None = None
    inspected_ptrs: set[int] = field(default_factory=set)
    candidate_count: int = 0
    batch_complete: bool = False

    def age(self, now: float | None = None) -> float:
        """Seconds since last full scan. @author by ak"""
        t = float(now if now is not None else time.time())
        return max(0.0, t - float(self.ts))


@dataclass
class SuperLootTarget:
    """One matching matter candidate. @author by ak"""

    name: str
    ptr: int
    obj_id: int | None = None
    tid: int | None = None
    dist: float | None = None
    x: float | None = None
    y: float | None = None
    z: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_interact(self) -> InteractTarget:
        return InteractTarget(
            name=self.name,
            ptr=self.ptr,
            obj_id=self.obj_id,
            tid=self.tid,
            dist=self.dist,
            x=self.x,
            y=self.y,
            z=self.z,
            kind="pickup",
            note="super_loot",
        )


@dataclass
class SuperLootStepResult:
    """One loop-step outcome. @author by ak"""

    ok: bool
    action: str  # idle | open | path_open | none | error | stop
    message: str = ""
    target: dict | None = None
    host_pos: list[float] | None = None
    scene_id: int | None = None
    interact: dict | None = None
    move: dict | None = None
    matched: int = 0
    error: str | None = None
    # Dense-map scan throttle state (not always serializable in UI logs)
    scan_cache: ScanCache | None = None
    dense: bool = False
    scan_source: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        # Drop non-UI cache object; keep flags only
        d.pop("scan_cache", None)
        return d


def _dist3(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    *,
    horizontal: bool = False,
) -> float:
    if horizontal:
        return math.sqrt((a[0] - b[0]) ** 2 + (a[2] - b[2]) ** 2)
    return math.sqrt(
        (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2
    )


def evaluate_motion_gate(
    prev: tuple[float, float, float] | None,
    prev_ts: float,
    cur: tuple[float, float, float],
    now: float,
    *,
    speed_mps: float,
    jump_m: float,
    y_jump_m: float,
) -> tuple[bool, str]:
    """
    Detect high-speed / teleport / vertical spike between two host samples.

    Used by SuperLootRunner to pause open+CRT during manual fly mid-air.
    Returns (should_pause, reason_tag).
    @author by ak
    """
    if prev is None:
        return False, ""
    try:
        dt = max(0.05, float(now) - float(prev_ts))
        dh = _dist3(cur, prev, horizontal=True)
        dy = abs(float(cur[1]) - float(prev[1]))
        speed = float(dh) / dt
    except Exception:
        return False, ""
    if float(jump_m) > 0 and dh >= float(jump_m):
        return True, f"jump_h={dh:.1f}m"
    if float(y_jump_m) > 0 and dy >= float(y_jump_m):
        return True, f"jump_y={dy:.1f}m"
    if float(speed_mps) > 0 and speed >= float(speed_mps):
        return True, f"speed={speed:.1f}m/s dt={dt:.2f}s"
    return False, ""


def name_matches(name: str, needle: str) -> bool:
    """Substring match on display name. @author by ak"""
    n = (name or "").strip()
    k = (needle or "").strip()
    if not k:
        return False
    return k in n


def tid_matches(tid: int | None, want: int | None) -> bool:
    """Exact template id match. @author by ak"""
    if tid is None or want is None:
        return False
    return int(tid) == int(want)


def looks_like_chest(name: str | None, tid: int | None = None) -> bool:
    """True if target should use open/cast-bar path. @author by ak"""
    return is_welfare_chest(name, tid) or bool(
        name and any(k in name for k in ("箱", "礼盒", "宝匣"))
    )


def resolve_interact_mode(cfg: SuperLootConfig, target: SuperLootTarget) -> str:
    """Resolve open vs pick for one target. @author by ak"""
    mode = (cfg.interact_mode or MODE_OPEN).lower()
    if mode in (MODE_OPEN, MODE_PICK):
        return mode
    if looks_like_chest(target.name, target.tid):
        return MODE_OPEN
    return MODE_PICK


def _target_to_chest(t: SuperLootTarget) -> ChestInfo | None:
    """Convert loot target to ChestInfo for density helpers. @author by ak"""
    if t.x is None or t.z is None:
        return None
    return ChestInfo(
        name=t.name or "",
        address=int(t.ptr or 0),
        tid=int(t.tid) if t.tid is not None else None,
        x=float(t.x),
        y=float(t.y or 0.0),
        z=float(t.z),
        dist=float(t.dist) if t.dist is not None else None,
    )


def _chest_key(c: ChestInfo) -> tuple:
    """Stable match key between ChestInfo and SuperLootTarget. @author by ak"""
    return (int(c.address), round(float(c.x), 2), round(float(c.z), 2))


def _target_key(t: SuperLootTarget) -> tuple:
    return (
        int(t.ptr or 0),
        round(float(t.x or 0.0), 2),
        round(float(t.z or 0.0), 2),
    )


def _coord_skip_key(x: float, z: float) -> int:
    """
    Pack XZ into a synthetic skip key (high bit avoids low32 obj_id clash).

    Round to 0.01; 16-bit each axis (two's complement style via mask).
    @author by ak
    """
    xi = int(round(float(x) * 100.0)) & 0xFFFF
    zi = int(round(float(z) * 100.0)) & 0xFFFF
    return 0x70000000 | (xi << 16) | zi


def target_skip_key(t: SuperLootTarget) -> int | None:
    """
    Primary skip-map key for a loot target (compat).

    Prefer obj_id; else fine XZ key. Prefer target_skip_keys for dual-key use.
    @author by ak
    """
    keys = target_skip_keys(t)
    return keys[0] if keys else None


def target_skip_keys(t: SuperLootTarget) -> list[int]:
    """
    All skip keys for one target: obj_id and fine XZ (both when available).

    Building-stuck chests often keep the same XZ while obj_id churns (or the
    reverse); dual keys prevent re-select after one failed path.
    @author by ak
    """
    keys: list[int] = []
    if t.obj_id is not None and int(t.obj_id) > 0:
        keys.append(int(t.obj_id))
    if t.x is not None and t.z is not None:
        keys.append(_coord_skip_key(float(t.x), float(t.z)))
    return keys


def _no_cast_key(
    target: SuperLootTarget,
    scene_id: int | None,
) -> tuple[int, int]:
    """Stable per-scene key for bounded no-cast retries. @author by ak"""
    key = target_skip_key(target)
    if key is None:
        key = int(target.ptr or 0)
    return int(scene_id or 0), int(key or 0)


def _purge_no_cast_cooldowns(
    cooldowns: dict[tuple[int, int], float] | None,
    *,
    now: float,
) -> None:
    if not cooldowns:
        return
    for key in list(cooldowns.keys()):
        if float(cooldowns[key]) <= float(now):
            del cooldowns[key]


def _filter_no_cast_cooldowns(
    hits: list[SuperLootTarget],
    cooldowns: dict[tuple[int, int], float] | None,
    *,
    scene_id: int | None,
    now: float,
) -> tuple[list[SuperLootTarget], list[SuperLootTarget]]:
    """Filter only the exact chest under a short no-cast cooldown. @author by ak"""
    _purge_no_cast_cooldowns(cooldowns, now=now)
    if not hits or not cooldowns:
        return list(hits), []
    keep: list[SuperLootTarget] = []
    blocked: list[SuperLootTarget] = []
    for target in hits:
        if _no_cast_key(target, scene_id) in cooldowns:
            blocked.append(target)
        else:
            keep.append(target)
    return keep, blocked


def purge_skip_ids(skip_ids: dict[int, float] | None, *, now: float | None = None) -> None:
    """Drop expired skip entries. @author by ak"""
    if not skip_ids:
        return
    t = float(now if now is not None else time.time())
    for k in list(skip_ids.keys()):
        if float(skip_ids[k]) <= t:
            del skip_ids[k]


def purge_skip_points(
    skip_points: list[tuple[float, float, float]] | None,
    *,
    now: float | None = None,
) -> None:
    """Drop expired stuck XZ points in-place. @author by ak"""
    if not skip_points:
        return
    t = float(now if now is not None else time.time())
    keep = [(x, z, exp) for x, z, exp in skip_points if float(exp) > t]
    skip_points[:] = keep


def purge_soft_runtime_skips(
    skip_ids: dict[int, float] | None,
    skip_points: list[tuple[float, float, float]] | None,
    *,
    now: float | None = None,
    soft_max_remain_s: float = SOFT_SKIP_MAX_REMAIN_S,
    log: LogFn | None = None,
) -> int:
    """
    Drop short object-id runtime bans, but retain path-failure XZ bans.

    `skip_points` is written only by path-failure branches.  It must not be
    cleared here: a fresh AOI scan may recreate the same unreachable chest with
    a different object id, which otherwise immediately re-enters selection.
    Expired points are removed by `purge_skip_points` before each scan.
    @author by ak
    """
    log = log or (lambda _m: None)
    t = float(now if now is not None else time.time())
    soft = max(0.0, float(soft_max_remain_s))
    n = 0
    if skip_ids:
        for k in list(skip_ids.keys()):
            remain = float(skip_ids[k]) - t
            if remain <= soft:
                del skip_ids[k]
                n += 1
    if n:
        log(f"super_loot soft-skip purge ids={n}; keep path-stuck points")
    return n


def filter_reachable_hits(
    hits: list[SuperLootTarget],
    skip_ids: dict[int, float] | None,
    *,
    now: float | None = None,
    near_m: float = DEFAULT_UNREACHABLE_NEAR_M,
    skip_points: list[tuple[float, float, float]] | None = None,
) -> list[SuperLootTarget]:
    """
    Remove opened / unreachable / near-stuck targets from candidates.

    Matches any dual skip key, or XZ within near_m of a stuck point.
    @author by ak
    """
    if not hits:
        return []
    tnow = float(now if now is not None else time.time())
    if skip_ids:
        purge_skip_ids(skip_ids, now=tnow)
    if skip_points is not None:
        purge_skip_points(skip_points, now=tnow)
    points = list(skip_points or [])
    if not skip_ids and not points:
        return list(hits)
    skip_map = skip_ids or {}
    radius = max(float(near_m), 0.0)
    out: list[SuperLootTarget] = []
    for h in hits:
        blocked = False
        for key in target_skip_keys(h):
            if int(key) in skip_map:
                blocked = True
                break
        if not blocked and points and h.x is not None and h.z is not None:
            hx, hz = float(h.x), float(h.z)
            for px, pz, _exp in points:
                dx = hx - float(px)
                dz = hz - float(pz)
                if (dx * dx + dz * dz) <= radius * radius:
                    blocked = True
                    break
        if blocked:
            continue
        out.append(h)
    return out


def mark_unreachable(
    skip_ids: dict[int, float] | None,
    target: SuperLootTarget,
    *,
    ttl_s: float,
    reason: str = "",
    log: LogFn | None = None,
    skip_points: list[tuple[float, float, float]] | None = None,
    record_near_point: bool = False,
) -> None:
    """
    Blacklist a target (opened / path-stuck / manual).

    Always writes obj_id + fine XZ keys into skip_ids.
    record_near_point=True also records XZ in skip_points so neighbors within
    unreachable_near_m are filtered. Default near radius is 0: open dense
    fields must not wipe neighbors. Prefer dual id/XZ keys alone.
    Opened / no_cast must NEVER set near wipe.
    @author by ak
    """
    log = log or (lambda _m: None)
    if skip_ids is None and skip_points is None:
        return
    # Honor short soft skips (opened/no_cast); only pad tiny positive TTLs.
    ttl = max(float(ttl_s), 0.5)
    exp = time.time() + ttl
    keys = target_skip_keys(target)
    if skip_ids is not None:
        for key in keys:
            skip_ids[int(key)] = exp
    if (
        bool(record_near_point)
        and skip_points is not None
        and target.x is not None
        and target.z is not None
    ):
        skip_points.append((float(target.x), float(target.z), exp))
        if len(skip_points) > 256:
            del skip_points[:-200]
    if not keys and (target.x is None or target.z is None):
        return
    log(
        f"super_loot unreachable skip keys={keys} ttl={ttl:.0f}s "
        f"d={target.dist if target.dist is not None else -1:.1f} "
        f"name={target.name!r} {reason}"
        f"{' near' if record_near_point else ''}".rstrip()
    )


def _resolve_target_from_seed(
    seed: ChestInfo,
    ordered: list[SuperLootTarget],
    t_by_key: dict[tuple, SuperLootTarget],
) -> SuperLootTarget | None:
    """Map density seed ChestInfo back to SuperLootTarget. @author by ak"""
    tgt = t_by_key.get(_chest_key(seed))
    if tgt is not None:
        return tgt
    for t in ordered:
        if t.x is None or t.z is None:
            continue
        if (
            abs(float(t.x) - float(seed.x)) < 0.05
            and abs(float(t.z) - float(seed.z)) < 0.05
        ):
            return t
    return None


def select_loot_target(
    hits: list[SuperLootTarget],
    cfg: SuperLootConfig,
    *,
    host_pos: tuple[float, float, float] | None,
    session: GameAttachSession | None = None,
    skip_ids: dict[int, float] | None = None,
    skip_points: list[tuple[float, float, float]] | None = None,
    log: LogFn | None = None,
) -> tuple[SuperLootTarget | None, str]:
    """
    Choose which chest to open / walk to.

    Policy:
      1) Face stand-open (d <= pick_range ~2.5m): nearest first, including
         isolated — no move; never skip for density.
      2) Soft band empty when SOFT_OPEN_EXTRA=0 (walk when d > pick_range).
      3) Near work (pr < d <= near_prefer_path): nearest non-isolated first.
         Isolated skipped only when multi-chest exists elsewhere (anti-strand).
      4) Beyond near band: multi-chest dense cluster (adj>=2) + fewer players.
      5) Only solitary left: nearest within max_isolated_path preferred.
      6) If only farther isolates remain: last-resort nearest (never idle
         forever while any matched chest is still visible).

    Unreachable / recently skipped ids are filtered when filter_unreachable.
    If every candidate is unreachable, returns (None, "all_unreachable").

    Returns (target, reason_tag).
    @author by ak
    """
    log = log or (lambda _m: None)
    if not hits:
        raise ValueError("select_loot_target: empty hits")

    pool = list(hits)
    if bool(cfg.filter_unreachable):
        filtered = filter_reachable_hits(
            pool,
            skip_ids,
            near_m=float(cfg.unreachable_near_m),
            skip_points=skip_points,
        )
        if len(filtered) < len(pool):
            log(
                f"super_loot filter unreachable drop={len(pool) - len(filtered)} "
                f"keep={len(filtered)}"
            )
        pool = filtered
        if not pool:
            log("super_loot all candidates unreachable/skipped; idle")
            return None, "all_unreachable"

    pr = float(cfg.pick_range)
    soft = pr + float(DEFAULT_SOFT_OPEN_EXTRA)
    cr = float(cfg.cluster_radius)
    max_iso = float(cfg.max_isolated_path)
    iso_max_adj = int(cfg.isolated_max_adj)
    near_prefer = max(float(cfg.near_prefer_path), soft)

    # Refresh horizontal distances
    for t in pool:
        if host_pos is not None and t.x is not None and t.z is not None:
            t.dist = _dist3(
                host_pos,
                (float(t.x), float(t.y or 0.0), float(t.z)),
                horizontal=True,
            )
    ordered = sorted(pool, key=lambda t: t.dist if t.dist is not None else 1e18)
    # Strict open range for priority; soft only used later by open path
    local = [t for t in ordered if t.dist is not None and t.dist <= pr]

    chests: list[ChestInfo] = []
    t_by_key: dict[tuple, SuperLootTarget] = {}
    for t in ordered:
        c = _target_to_chest(t)
        if c is None:
            continue
        chests.append(c)
        t_by_key[_chest_key(c)] = t

    # 0) Optional random among nearby matched entities (妖楼 4 暗道等).
    # Does not affect default chest farming (select_mode=nearest).
    sel_mode = str(getattr(cfg, "select_mode", "nearest") or "nearest").strip().lower()
    if sel_mode in ("random", "rand", "shuffle") and ordered:
        pool_r = float(getattr(cfg, "random_pool_radius", 0.0) or 0.0)
        r = max(float(pr), pool_r) if pool_r > 1e-6 else float(pr)
        cand = [
            t
            for t in ordered
            if t.dist is not None and float(t.dist) <= r + 1e-6
        ]
        if not cand:
            cand = list(ordered)
        if cand:
            pick = random.choice(cand)
            dist_bits = ",".join(
                f"{float(t.dist):.1f}"
                for t in cand[:12]
                if t.dist is not None
            )
            def _oid(t: SuperLootTarget) -> int:
                try:
                    return int(getattr(t, "obj_id", None) or 0)
                except (TypeError, ValueError):
                    return 0

            id_bits = ",".join(f"0x{_oid(t):X}" for t in cand[:6])
            log(
                f"super_loot select local-random d={float(pick.dist or -1):.1f} "
                f"pool={len(cand)}/{len(ordered)} r={r:.1f} "
                f"name={pick.name!r} id=0x{_oid(pick):X} "
                f"dists=[{dist_bits}] ids=[{id_bits}]"
            )
            return pick, "local_random"

    # 1) Face / hard stand-open: nearest always — isolated included.
    # ~2.5m needs no HostMove; never skip for a far multi-chest pocket.
    if local and bool(cfg.prefer_nearest):
        pick = local[0]
        log(
            f"super_loot select local-nearest d={pick.dist:.1f} "
            f"name={pick.name!r}"
        )
        return pick, "local_nearest"

    # 2) Soft band only if SOFT_OPEN_EXTRA > 0 (currently 0 = disabled).
    soft_local = [
        t for t in ordered if t.dist is not None and pr < t.dist <= soft
    ]
    if soft_local and bool(cfg.prefer_nearest) and soft > pr + 1e-6:
        pick = soft_local[0]
        log(
            f"super_loot select soft-walk d={pick.dist:.1f} "
            f"name={pick.name!r}"
        )
        return pick, "soft_walk"

    players: list[tuple[float, float, float]] = []
    if session is not None:
        try:
            players = list_player_positions(session, host_pos=host_pos, log=log)
        except Exception as e:
            log(f"super_loot players skip: {e}")

    # Local isolation radius: neighbors within ~soft band of seed (~6–8m).
    # Full cr (20m) wrongly merges a lonely@8m with a pocket@25m.
    iso_r = min(float(cr), max(float(soft) + 1.0, 8.0))

    # 3) Near pocket: nearest non-isolated. Keep original anti-strand rule —
    # do not path a lonely nearer box when multi-chest work exists elsewhere
    # (far dense step 4). Not densest-first: densest@7–12m beat face@5–6m.
    near_hits = [
        t for t in ordered if t.dist is not None and soft < t.dist <= near_prefer
    ]
    if near_hits and bool(cfg.prefer_nearest):
        has_multi = False
        for t in ordered:
            if t.dist is None:
                continue
            c = _target_to_chest(t)
            if c is None:
                continue
            if not is_isolated_chest(
                c,
                chests,
                cluster_radius=iso_r,
                isolated_max_adj=iso_max_adj,
            ):
                has_multi = True
                break
        for t in near_hits:
            c = _target_to_chest(t)
            if c is None:
                continue
            iso = is_isolated_chest(
                c,
                chests,
                cluster_radius=iso_r,
                isolated_max_adj=iso_max_adj,
            )
            if iso and has_multi:
                log(
                    f"super_loot skip near-isolated d={t.dist:.1f} "
                    f"name={t.name!r} (prefer multi-chest)"
                )
                continue
            tag = "near_sparse" if iso else "near_path"
            log(
                f"super_loot select {tag} d={t.dist:.1f} "
                f"name={t.name!r}"
            )
            return t, tag
        # All near hits were isolated and multi exists farther → fall through
        # to dense (step 4) instead of walking the lonely near box.

    # 4) Multi-chest density + low player pressure (nothing near host).
    # Density pool = non-isolated under tight iso_r. Large cr (20m) would
    # otherwise treat a lonely@8m as "adj=4" of a pocket@25m and path there.
    dense_tgt: SuperLootTarget | None = None
    dense_adj = 0
    dense_pc = 0
    if bool(cfg.prefer_dense_when_far) and chests:
        multi_chests = [
            c
            for c in chests
            if not is_isolated_chest(
                c,
                chests,
                cluster_radius=iso_r,
                isolated_max_adj=iso_max_adj,
            )
        ]
        dense_pool = multi_chests if multi_chests else chests
        seed, _members, adj_n = pick_target_by_neighbor_density(
            dense_pool,
            cluster_radius=cr,
            host_pos=host_pos,
            players=players,
            player_radius=float(cfg.player_radius),
        )
        if seed is not None:
            tgt = _resolve_target_from_seed(seed, ordered, t_by_key)
            if tgt is not None:
                dense_tgt = tgt
                dense_adj = int(adj_n)
                dense_pc = (
                    count_players_near(
                        (seed.x, seed.y, seed.z),
                        players,
                        radius=float(cfg.player_radius),
                    )
                    if players
                    else 0
                )

    # Real multi-chest cluster: prefer over solitary far boxes
    if dense_tgt is not None and dense_adj >= 2:
        log(
            f"super_loot select dense adj={dense_adj} players~{dense_pc} "
            f"d={dense_tgt.dist if dense_tgt.dist is not None else -1:.1f} "
            f"name={dense_tgt.name!r}"
        )
        return dense_tgt, f"dense_adj{dense_adj}_p{dense_pc}"

    # 5) Only sparse / isolated candidates: prefer within max_isolated_path.
    # Far isolates are deferred (not chosen first) so multi pockets win when
    # present; if NOTHING else remains, last-resort still takes nearest.
    far_iso: list[tuple[float, SuperLootTarget]] = []
    for t in ordered:
        c = _target_to_chest(t)
        if c is None:
            continue
        d = float(t.dist) if t.dist is not None else 1e18
        isolated = is_isolated_chest(
            c,
            chests,
            cluster_radius=cr,
            isolated_max_adj=iso_max_adj,
        )
        if isolated and d > max_iso:
            log(
                f"super_loot skip isolated-far d={d:.1f}>{max_iso:.0f} "
                f"name={t.name!r} (defer)"
            )
            far_iso.append((d, t))
            continue
        tag = "near_sparse" if isolated else "nearest"
        log(
            f"super_loot select {tag} d={d if d < 1e17 else -1:.1f} "
            f"name={t.name!r}"
        )
        return t, tag

    # 6) Last resort: only far isolates left — go nearest so runner does not
    # spin "附近暂无目标" while UI still shows 扫到 N 个.
    if far_iso:
        far_iso.sort(key=lambda it: it[0])
        d, t = far_iso[0]
        log(
            f"super_loot select last_resort_isolated d={d:.1f} "
            f"name={t.name!r} (only isolates left)"
        )
        return t, "last_resort_isolated"

    log("super_loot skip all far isolated; idle")
    return None, "skip_isolated_far"


def open_attach_session(
    pid: int,
    *,
    log: LogFn | None = None,
    warmup: bool = True,
) -> GameAttachSession:
    """Open long-lived pymem attach for loot loop. @author by ak

    warmup=True: 非马上需要 SCENE/POS 软预热（不 force-fresh；不扫全内存）。
    """
    log = log or (lambda _m: None)
    sess = GameAttachSession(log=log)
    sess.attach(int(pid))
    if warmup:
        try:
            from app.core.state_dispatch import StateKind, warmup_session

            warmup_session(
                sess,
                (StateKind.SCENE, StateKind.POS),
                log=lambda m: log(f"attach warmup: {m}") if m else None,
            )
        except Exception as e:
            log(f"attach warmup skip: {e}")
    return sess


def _prune_cache_hits(
    cache: ScanCache | None,
    target: SuperLootTarget,
) -> ScanCache | None:
    """
    Drop opened / failed target from scan cache so local chain advances.

    @author by ak
    """
    if cache is None or not cache.hits:
        return cache
    keys = set(target_skip_keys(target))
    addr = int(target.ptr or 0)
    kept: list[SuperLootTarget] = []
    for h in cache.hits:
        drop = False
        if addr and int(h.ptr or 0) == addr:
            drop = True
        else:
            for k in target_skip_keys(h):
                if k in keys:
                    drop = True
                    break
        if not drop:
            kept.append(h)
    return ScanCache(
        hits=kept,
        host_pos=cache.host_pos,
        ts=cache.ts,
        matter_count=cache.matter_count,
        dense=cache.dense,
        scene_id=cache.scene_id,
        inspected_ptrs=set(cache.inspected_ptrs),
        candidate_count=cache.candidate_count,
        batch_complete=cache.batch_complete,
    )


def _keep_face_local_cache(
    cache: ScanCache | None,
    *,
    host_pos: tuple[float, float, float] | None,
    pick_range: float,
    log: LogFn | None = None,
) -> ScanCache | None:
    """
    After open/fail: keep only face-range hits (d <= pick_range).

    - Face still has chests → next loop opens without full_scan (original logic).
    - Face empty → return None so next loop realtime light-scans (public chests).
    Never keep a long far list that others may already have opened.
    @author by ak
    """
    log = log or (lambda _m: None)
    if cache is None or not cache.hits:
        return None
    pr = max(0.5, float(pick_range))
    hits = _refresh_hit_dists(list(cache.hits), host_pos)
    face: list[SuperLootTarget] = []
    for h in hits:
        if h.dist is None:
            continue
        if float(h.dist) <= pr:
            face.append(h)
    if not face:
        log(f"super_loot face-cache empty (pr={pr:.1f}) → next full_scan")
        return None
    face.sort(key=lambda t: float(t.dist) if t.dist is not None else 1e9)
    log(f"super_loot face-cache keep n={len(face)} pr={pr:.1f}")
    return ScanCache(
        hits=face,
        host_pos=host_pos if host_pos is not None else cache.host_pos,
        ts=float(cache.ts),
        matter_count=int(cache.matter_count or 0),
        dense=bool(cache.dense),
        scene_id=cache.scene_id,
        inspected_ptrs=set(cache.inspected_ptrs),
        candidate_count=cache.candidate_count,
        batch_complete=cache.batch_complete,
    )


def is_attach_hard_error(msg: str) -> bool:
    """
    Detect CRT/attach collapse (Access Denied / VirtualAllocEx).

    After dense-map spam the process often denies further remote alloc;
    reattach + backoff is safer than tight none-loops.
    @author by ak
    """
    m = (msg or "").lower()
    return (
        "err=5" in m
        or "virtualallocex" in m
        or "createremotethread failed" in m
        or "access is denied" in m
        or "openprocess failed" in m
        or "could not open process" in m
        or "process is not open" in m
        or "hard_dead" in m
        or "remote hung" in m
        or "remote blocked" in m
    )


def _note_loot_hard_error(session, exc: BaseException | str) -> None:
    """Push hard scan failure into platform gate (business still owns cadence)."""
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


def is_bridge_error(msg: str) -> bool:
    """
    Detect missing / dead UI bridge (OpenFileMapping / 未注入).

    @author by ak
    """
    m = (msg or "").lower()
    return (
        "openfilemapping" in m
        or "桥接未就绪" in (msg or "")
        or "bridge" in m
        and ("not" in m or "fail" in m or "skip" in m or "未" in (msg or ""))
        or "请先注入" in (msg or "")
    )


def _refresh_hit_dists(
    hits: list[SuperLootTarget],
    host_pos: tuple[float, float, float] | None,
) -> list[SuperLootTarget]:
    """
    Recompute horizontal dist from current host without rescan.

    @author by ak
    """
    if host_pos is None:
        return list(hits)
    out: list[SuperLootTarget] = []
    for t in hits:
        nt = SuperLootTarget(
            name=t.name,
            ptr=t.ptr,
            obj_id=t.obj_id,
            tid=t.tid,
            dist=t.dist,
            x=t.x,
            y=t.y,
            z=t.z,
        )
        if nt.x is not None and nt.z is not None:
            nt.dist = _dist3(
                host_pos,
                (float(nt.x), float(nt.y or 0.0), float(nt.z)),
                horizontal=True,
            )
        out.append(nt)
    out.sort(key=lambda t: t.dist if t.dist is not None else 1e18)
    return out


def _count_hits_within(
    hits: list[SuperLootTarget],
    *,
    host_pos: tuple[float, float, float] | None,
    radius: float,
) -> int:
    """Count cache hits within horizontal radius of host. @author by ak"""
    if not hits:
        return 0
    r = float(radius)
    n = 0
    refreshed = _refresh_hit_dists(hits, host_pos)
    for t in refreshed:
        if t.dist is not None and t.dist <= r:
            n += 1
    return n


def _count_local_hits(
    hits: list[SuperLootTarget],
    *,
    host_pos: tuple[float, float, float] | None,
    pick_range: float,
) -> int:
    """
    Count unopened hits still in open/soft range (immediate open chain).

    @author by ak
    """
    soft = float(pick_range) + float(DEFAULT_SOFT_OPEN_EXTRA)
    return _count_hits_within(hits, host_pos=host_pos, radius=soft)


def _guide_work_radius(cfg: SuperLootConfig) -> float:
    """
    Range where client can still auto-guide / we keep working one pocket.

    Wider than pick_range: open chain + short path inside one chest field.
    @author by ak
    """
    pr = float(cfg.pick_range)
    cr = float(cfg.cluster_radius)
    sr = float(cfg.scan_radius) if cfg.scan_radius else 80.0
    # Prefer a pocket around cluster size; never larger than scan_radius.
    return min(sr, max(pr + float(DEFAULT_SOFT_OPEN_EXTRA), cr * 1.5, 24.0))


def should_reuse_scan_cache(
    cache: ScanCache | None,
    cfg: SuperLootConfig,
    *,
    host_pos: tuple[float, float, float] | None,
    skip_ids: dict[int, float] | None = None,
    skip_points: list[tuple[float, float, float]] | None = None,
    now: float | None = None,
    log: LogFn | None = None,
) -> tuple[bool, list[SuperLootTarget], str]:
    """
    Decide whether to skip full AOI scan and reuse cache.

    Reuse the current scan while the active chest pocket still has guideable
    work. This is the original continuous-loot behavior: scanning resumes
    only after the current pocket is exhausted or the host leaves it.

    Returns (reuse, hits_or_empty, reason).
    @author by ak
    """
    log = log or (lambda _m: None)
    tnow = float(now if now is not None else time.time())
    if cache is None or not cache.hits:
        return False, [], "no_cache"
    age = cache.age(tnow)

    hits = _refresh_hit_dists(cache.hits, host_pos)
    if bool(cfg.filter_unreachable) and (skip_ids or skip_points):
        hits = filter_reachable_hits(
            hits,
            skip_ids,
            now=tnow,
            near_m=float(cfg.unreachable_near_m),
            skip_points=skip_points,
        )
    if not hits:
        return False, [], "cache_empty_after_filter"

    guide_r = _guide_work_radius(cfg)
    local_n = _count_local_hits(
        hits, host_pos=host_pos, pick_range=float(cfg.pick_range)
    )
    work_n = _count_hits_within(hits, host_pos=host_pos, radius=guide_r)
    need = max(1, int(cfg.local_skip_scan_n))

    # Host left the pocket with nothing guideable nearby => rescan for new area.
    if host_pos is not None and cache.host_pos is not None:
        moved = _dist3(host_pos, cache.host_pos, horizontal=True)
        if moved > float(cfg.cache_host_move_m) and work_n < need:
            return False, [], f"host_moved {moved:.1f}"

    if not bool(cfg.skip_scan_while_local):
        # Explicit opt-out: fall back to interval throttle only.
        interval = (
            float(cfg.dense_scan_interval_s)
            if cache.dense
            else float(cfg.sparse_scan_interval_s)
        )
        if age < interval:
            return True, hits, f"interval_{age:.1f}"
        return False, hits, "need_rescan"

    # Immediate face-local chain: do not scan between adjacent chests.
    if local_n >= need:
        log(
            f"super_loot skip-scan local={local_n} cache_age={age:.1f}s "
            f"hits={len(hits)}"
        )
        return True, hits, f"local_{local_n}"

    interval = (
        float(cfg.dense_scan_interval_s)
        if cache.dense
        else float(cfg.sparse_scan_interval_s)
    )
    # Keep walking/opening the already-scanned pocket. This removes the
    # per-chest CRT rescan introduced by the later face-only cache policy.
    # Scan volume remains bounded by the current dense limits and hard-error
    # cooldowns in find_named_targets/resolve_hits_for_step.
    if work_n >= need:
        log(
            f"super_loot skip-scan work={work_n}/{len(hits)} "
            f"guide_r={guide_r:.0f} cache_age={age:.1f}s"
        )
        return True, hits, f"work_{work_n}"

    if age < interval and work_n < need:
        log(
            f"super_loot skip-scan idle-wait age={age:.1f}<{interval:.1f} "
            f"dense={cache.dense} hits={len(hits)}"
        )
        return True, hits, f"idle_wait_{age:.1f}"

    log(
        f"super_loot idle-rescan age={age:.1f}s local={local_n} work={work_n} "
        f"hits={len(hits)} dense={cache.dense}"
    )
    return False, [], "idle_rescan"


def _path_failure_key(target: SuperLootTarget) -> int:
    """Use XZ first so an AOI object-id refresh does not reset the penalty."""
    if target.x is not None and target.z is not None:
        return _coord_skip_key(float(target.x), float(target.z))
    key = target_skip_key(target)
    return int(key or 0)


def mark_weighted_path_failure(
    skip_ids: dict[int, float] | None,
    skip_points: list[tuple[float, float, float]] | None,
    failure_counts: dict[int, int] | None,
    target: SuperLootTarget,
    *,
    reason: str,
    log: LogFn | None = None,
) -> tuple[int, float]:
    """Increase the session penalty for an unreachable path target.

    Backoff is 30s, 90s, 5m, 15m, then capped at 30m.  Counts are retained
    for the lifetime of the helper session, even after an individual ban expires.
    """
    key = _path_failure_key(target)
    attempt = int((failure_counts or {}).get(key, 0)) + 1
    if failure_counts is not None:
        failure_counts[key] = attempt
    ttl_s = PATH_FAILURE_BACKOFF_S[min(attempt - 1, len(PATH_FAILURE_BACKOFF_S) - 1)]
    mark_unreachable(
        skip_ids,
        target,
        ttl_s=ttl_s,
        reason=f"{reason} fail#{attempt} backoff={ttl_s:.0f}s",
        log=log,
        skip_points=skip_points,
        record_near_point=True,
    )
    return attempt, ttl_s


def find_named_targets(
    session: GameAttachSession,
    cfg: SuperLootConfig,
    *,
    host_pos: tuple[float, float, float] | None = None,
    log: LogFn | None = None,
    dense: bool | None = None,
    exclude_ptrs: set[int] | None = None,
    scan_meta_out: dict | None = None,
) -> list[SuperLootTarget]:
    """
    Scan matter AOI and filter by item name (and optional tid).

    Dense / public chests: light realtime scan only — nearest K TemplateID CRT
    (default inspect<=12), stamp cfg.tid for MatterInteract. Face-local open
    chain reuses cache without calling this; face-empty forces a fresh call.

    @author by ak
    """
    log = log or (lambda _m: None)
    # Platform gate: skip full CRT scan when pid already dead/hung
    try:
        pid = int(getattr(session, "pid", 0) or 0)
    except Exception:
        pid = 0
    if pid > 0:
        try:
            from app.core.safe_dispatch import OpKind, get_dispatch

            get_dispatch().ensure_callable(pid, kind=OpKind.CRT_READ)
        except Exception as e:
            if is_attach_hard_error(str(e)):
                _note_loot_hard_error(session, e)
                raise OSError(f"super_loot scan blocked: {e}") from e
            # non-gate errors: ignore and let scan path surface real failures
    # The runner already supplies this value. Manual scan calls this common
    # entry directly, so classify its chest field here before choosing the
    # expensive object/template scan limits.
    if dense is None:
        dense = False
        if pid > 0:
            try:
                matter_n = int(get_object_count(session, CLASS_MATTER))
                dense = matter_n >= int(cfg.dense_count)
                log(
                    f"super_loot scan matter_count={matter_n} dense={dense}"
                )
            except Exception as e:
                log(f"super_loot scan matter_count skip: {e}")
    else:
        dense = bool(dense)
    needle = (cfg.item_name or "").strip()
    want_tid = (
        int(cfg.tid)
        if cfg.match_tid_fallback and cfg.tid is not None
        else None
    )
    # A configured template id is authoritative and avoids name CRT calls.
    # Without one, match by name first and read TID only for matched objects.
    use_tid_fast = want_tid is not None
    name_keys = (needle,) if needle else None
    base_lim = int(cfg.matter_limit)
    if dense:
        base_lim = min(base_lim, int(cfg.dense_matter_limit))
    # Public dense: small AOI shortlist; realtime rescan replaces long lists.
    lim = max(8, min(base_lim, 16 if dense else 160))
    # Dense inspect was 48–256 CRT/loop (crash vector). Cap hard.
    if dense:
        inspect = min(12, max(lim, 8))
    else:
        inspect = min(96, max(lim * 2, 24))
    # Known welfare TID: match by TID on nearest K only (no name CRT storm).
    read_tid = bool(
        use_tid_fast
        or str(cfg.interact_mode or "").lower() in (MODE_OPEN, MODE_AUTO)
    )
    # A configured TID is already the authoritative filter. For name-only
    # targets, list_class_objects reads the name first and fetches TID only
    # after the name matches, because interaction still requires a live TID.
    read_name = bool(name_keys) and not use_tid_fast
    if dense and use_tid_fast:
        log(
            f"super_loot light-scan dense tid={want_tid} "
            f"lim={lim} inspect={inspect} name_crt=0"
        )
    objs = list_class_objects(
        session,
        CLASS_MATTER,
        host_pos=host_pos,
        radius=float(cfg.scan_radius) if cfg.scan_radius else None,
        limit=lim,
        log=log,
        want_tid=want_tid if use_tid_fast else None,
        want_name_keys=name_keys if read_name else None,
        read_name=read_name,
        # MatterInteract needs tid: prefer live read; stamp cfg.tid if missing.
        read_tid=read_tid,
        read_dist_api=False,
        max_inspect=inspect,
        max_crt_failures=3 if dense else 6,
        exclude_ptrs=exclude_ptrs,
        scan_meta_out=scan_meta_out,
    )
    out: list[SuperLootTarget] = []
    for o in objs:
        by_tid = tid_matches(o.tid, want_tid) if want_tid is not None else False
        by_name = name_matches(o.name or "", needle) if needle else False
        if use_tid_fast:
            if not by_tid and not (bool(cfg.match_tid_fallback) and by_name):
                continue
            # Keep the configured display name for a TID-only hit.
            disp = o.name or needle or f"tid:{o.tid}"
        else:
            if needle:
                if not by_name:
                    continue
            elif not by_tid:
                continue
            disp = o.name or ""

        # obj_id is RPM (object+0x140), not CRT — safe on dense maps
        oid = get_object_id64(session, o.ptr)
        dist = o.dist
        if dist is None and host_pos is not None and o.x is not None:
            dist = _dist3(host_pos, (float(o.x), float(o.y), float(o.z)))
        out.append(
            SuperLootTarget(
                name=disp,
                ptr=int(o.ptr),
                obj_id=oid,
                # Name fallback is only used with an explicitly configured TID.
                # MatterInteract requires that TID even if this individual read
                # was unavailable.
                tid=(
                    int(o.tid)
                    if o.tid is not None
                    else (
                        int(want_tid)
                        if want_tid is not None
                        and (by_name or by_tid or use_tid_fast)
                        else None
                    )
                ),
                dist=float(dist) if dist is not None else None,
                x=float(o.x) if o.x is not None else None,
                y=float(o.y) if o.y is not None else None,
                z=float(o.z) if o.z is not None else None,
            )
        )
    out.sort(key=lambda t: t.dist if t.dist is not None else 1e18)
    log(
        f"super_loot match name={needle!r} tid={cfg.tid} "
        f"hits={len(out)} matter={len(objs)} inspect~{inspect} dense={bool(dense)}"
    )
    return out


def resolve_hits_for_step(
    session: GameAttachSession,
    cfg: SuperLootConfig,
    *,
    host_pos: tuple[float, float, float] | None,
    scene_id: int | None = None,
    scan_cache: ScanCache | None = None,
    skip_ids: dict[int, float] | None = None,
    skip_points: list[tuple[float, float, float]] | None = None,
    crt_cooldown_until: float = 0.0,
    log: LogFn | None = None,
) -> tuple[list[SuperLootTarget], ScanCache | None, str, bool]:
    """
    Get candidate hits for one loop step, with dense-map throttle.

    Returns (hits, new_or_same_cache, source_tag, dense_flag).
    source_tag: cache_* | full_scan | cooldown_cache | cooldown_idle
    On CRT hard-fail: re-raise only when no cache left; else return cache.
    @author by ak
    """
    log = log or (lambda _m: None)
    now = time.time()
    if (
        scan_cache is not None
        and scene_id is not None
        and scan_cache.scene_id is not None
        and int(scene_id) != int(scan_cache.scene_id)
    ):
        log(
            f"super_loot cache-reset scene {scan_cache.scene_id}->{scene_id}"
        )
        scan_cache = None
    reuse, cached_hits, why = should_reuse_scan_cache(
        scan_cache,
        cfg,
        host_pos=host_pos,
        skip_ids=skip_ids,
        skip_points=skip_points,
        now=now,
        log=log,
    )
    if reuse:
        dense = bool(scan_cache.dense) if scan_cache else False
        return cached_hits, scan_cache, f"cache:{why}", dense

    # CRT collapse: prefer face-local cache; do not stop the runner.
    # Full GetObjectTemplateID storm stays paused until cooldown ends.
    if float(crt_cooldown_until) > now:
        remain = float(crt_cooldown_until) - now
        dense = bool(scan_cache.dense) if scan_cache else True
        if scan_cache and scan_cache.hits:
            hits = _refresh_hit_dists(list(scan_cache.hits), host_pos)
            if bool(cfg.filter_unreachable) and (skip_ids or skip_points):
                hits = filter_reachable_hits(
                    hits,
                    skip_ids,
                    now=now,
                    near_m=float(cfg.unreachable_near_m),
                    skip_points=skip_points,
                )
            pr = float(cfg.pick_range)
            face = [
                t
                for t in hits
                if t.dist is not None and float(t.dist) <= pr
            ]
            if face:
                face.sort(
                    key=lambda t: float(t.dist) if t.dist is not None else 1e9
                )
                log(
                    f"super_loot crt-cooldown {remain:.1f}s; "
                    f"face-open n={len(face)} (no full CRT)"
                )
                return face, scan_cache, f"cooldown_face:{remain:.0f}", dense
        log(
            f"super_loot crt-cooldown {remain:.1f}s; "
            f"no face cache — wait (runner keeps running)"
        )
        return [], scan_cache, f"cooldown_idle:{remain:.0f}", dense

    # Continue a dense shortlist from the next uninspected pointer. Reset the
    # cursor when the scene changes, the player leaves the scan origin, or the
    # previous pass exhausted every candidate.
    continue_batch = False
    inspected_ptrs: set[int] = set()
    if scan_cache and scan_cache.dense and not scan_cache.batch_complete:
        same_scene = (
            scene_id is None
            or scan_cache.scene_id is None
            or int(scene_id) == int(scan_cache.scene_id)
        )
        moved = 0.0
        if host_pos is not None and scan_cache.host_pos is not None:
            moved = _dist3(host_pos, scan_cache.host_pos, horizontal=True)
        if same_scene and moved <= float(cfg.dense_batch_reset_move_m):
            continue_batch = True
            inspected_ptrs = set(scan_cache.inspected_ptrs)
        else:
            log(
                f"super_loot batch-reset scene_same={same_scene} moved={moved:.1f}"
            )

    # Cheap density probe: count only once per batch sequence.
    matter_n = int(scan_cache.matter_count or 0) if continue_batch else 0
    dense = True if continue_batch else bool(scan_cache.dense) if scan_cache else False
    if not continue_batch:
        try:
            matter_n = int(get_object_count(session, CLASS_MATTER))
            dense = matter_n >= int(cfg.dense_count)
            log(f"super_loot matter_count={matter_n} dense={dense}")
        except Exception as e:
            log(f"super_loot matter_count skip: {e}")
            if is_attach_hard_error(str(e)):
                # Prefer surviving on cache over raising (raise kills loop + reattach storm).
                if scan_cache and scan_cache.hits:
                    hits = _refresh_hit_dists(list(scan_cache.hits), host_pos)
                    if bool(cfg.filter_unreachable) and (skip_ids or skip_points):
                        hits = filter_reachable_hits(
                            hits,
                            skip_ids,
                            now=now,
                            near_m=float(cfg.unreachable_near_m),
                            skip_points=skip_points,
                        )
                    if hits:
                        log(
                            f"super_loot count hard-fail; cache fallback hits={len(hits)}"
                        )
                        return (
                            hits,
                            scan_cache,
                            "cache:count_hard_fail",
                            True,
                        )
                raise

    scan_meta: dict = {}
    try:
        hits = find_named_targets(
            session,
            cfg,
            host_pos=host_pos,
            log=log,
            dense=dense,
            exclude_ptrs=inspected_ptrs if dense else None,
            scan_meta_out=scan_meta if dense else None,
        )
    except Exception as e:
        log(f"super_loot full_scan fail: {e}")
        if is_attach_hard_error(str(e)):
            _note_loot_hard_error(session, e)
        if is_attach_hard_error(str(e)) and scan_cache and scan_cache.hits:
            hits = _refresh_hit_dists(list(scan_cache.hits), host_pos)
            if bool(cfg.filter_unreachable) and (skip_ids or skip_points):
                hits = filter_reachable_hits(
                    hits,
                    skip_ids,
                    now=now,
                    near_m=float(cfg.unreachable_near_m),
                    skip_points=skip_points,
                )
            if hits:
                log(f"super_loot scan hard-fail; cache fallback hits={len(hits)}")
                return hits, scan_cache, "cache:scan_hard_fail", True
        raise

    newly_inspected = {
        int(p) for p in scan_meta.get("inspected_ptrs", ()) if int(p or 0)
    }
    if dense:
        inspected_ptrs.update(newly_inspected)
    candidate_count = int(scan_meta.get("candidate_count", matter_n) or 0)
    batch_complete = bool(dense and int(scan_meta.get("remaining", 0) or 0) <= 0)
    new_cache = ScanCache(
        hits=list(hits),
        host_pos=host_pos,
        ts=now,
        matter_count=matter_n,
        dense=dense,
        scene_id=scene_id,
        inspected_ptrs=inspected_ptrs if dense else set(),
        candidate_count=candidate_count,
        batch_complete=batch_complete,
    )
    if dense:
        source = (
            "full_scan"
            if batch_complete
            else f"full_scan_batch:{len(inspected_ptrs)}/{candidate_count}"
        )
        log(
            f"super_loot batch-progress inspected={len(inspected_ptrs)} "
            f"candidates={candidate_count} complete={batch_complete} hits={len(hits)}"
        )
    else:
        source = "full_scan"
    return hits, new_cache, source, dense


def _sleep_interruptible(
    seconds: float,
    stop_event: threading.Event | None,
    *,
    slice_s: float = 0.1,
) -> bool:
    """
    Sleep up to seconds; return False if stop_event is set.

    @author by ak
    """
    return interruptible_sleep(
        seconds, stop_event, interval=max(0.05, float(slice_s))
    )


def _u32_rpm(session: GameAttachSession, addr: int) -> int | None:
    """Read remote u32; None on fail. @author by ak"""
    if not addr or not getattr(session, "pid", None):
        return None
    try:
        raw = remote_read_bytes(int(session.pid), int(addr) & 0xFFFFFFFF, 4)
        if len(raw) < 4:
            return None
        return int(struct.unpack("<I", raw)[0])
    except Exception:
        return None


def _format_cast_state(st: dict) -> str:
    """Compact cast snapshot for open/wait diagnostics. @author by ak"""
    return (
        f"readable={bool(st.get('readable'))} "
        f"active={bool(st.get('active'))} "
        f"session=0x{int(st.get('session_state') or 0):X} "
        f"busy={bool(cast_session_busy(st))} "
        f"cast=0x{int(st.get('cast') or 0):X} "
        f"id=0x{int(st.get('skill_id') or 0):X}/"
        f"0x{int(st.get('skill_id_b') or 0):X} "
        f"flags=0x{int(st.get('flags') or 0):X} "
        f"elapsed={int(st.get('elapsed') or 0)}"
    )


def cast_session_busy(st: dict | None) -> bool:
    """
    True if open/entry cast session looks busy in memory.

    skill cast-this (active) OR host+0x41C session gate (session_active).
    Matter open / entry often lights session_active while skill flags stay 0.
    @author by ak
    """
    if not st:
        return False
    return bool(st.get("active") or st.get("session_active"))


def read_host_cast_state(
    session: GameAttachSession | None,
    *,
    log: LogFn | None = None,
) -> dict:
    """
    Read host cast-this fields for open-bar detection.

    Returns cast fields plus host session_state/session_active used by
    CancelSession / matter open. Unreadable memory is explicitly marked and
    never treated as a confirmed idle acknowledgement.
    Unreadable memory => active=False (never assume casting).
    @author by ak
    """
    quiet = log or (lambda _m: None)
    out = {
        "readable": False,
        "active": False,
        "session_readable": False,
        "session_active": False,
        "session_state": 0,
        "cast": 0,
        "skill_id": 0,
        "skill_id_b": 0,
        "flags": 0,
        "elapsed": 0,
        "extra_flags": 0,
    }
    if session is None or not getattr(session, "pid", None):
        return out
    try:
        from app.core.skill_cast_probe import resolve_cast_this

        # Hot path: never CRT (seed happens once in wait_open_cast_bar).
        probe = resolve_cast_this(
            session, log=lambda _m: None, with_dumps=False, allow_crt=False
        )
    except Exception as e:
        quiet(f"super_loot cast-probe err: {e}")
        return out
    cast = int(probe.cast_this or 0)
    host = int(getattr(probe, "host_ptr", 0) or 0)
    session_raw = (
        _u32_rpm(session, host + int(HOST_SESSION_STATE_OFF)) if host else None
    )
    out.update(
        {
            "session_readable": session_raw is not None,
            "session_active": bool(session_raw),
            "session_state": int(session_raw or 0),
        }
    )
    if not cast:
        return out

    skill_raw = _u32_rpm(session, cast + int(CAST_FIELD_SKILL_ID_OFF))
    skill_b_raw = _u32_rpm(session, cast + int(CAST_FIELD_SKILL_ID_B_OFF))
    flags_raw = _u32_rpm(session, cast + int(CAST_FIELD_FLAGS_OFF))
    elapsed_raw = _u32_rpm(session, cast + int(CAST_FIELD_ELAPSED_OFF))
    extra_raw = _u32_rpm(session, cast + int(CAST_FIELD_EXTRA_FLAGS_OFF))
    readable = all(
        value is not None
        for value in (skill_raw, skill_b_raw, flags_raw, elapsed_raw)
    )
    skill = int(skill_raw or 0)
    skill_b = int(skill_b_raw or 0)
    flags = int(flags_raw or 0)
    elapsed = int(elapsed_raw or 0)
    extra = int(extra_raw or 0)
    # Real open/cast bar: skill id present plus progress (flags/elapsed) or
    # cast+0x4A0 busy bit (lab: armed during open/skill). skill-only spikes
    # without any progress bit are ignored.
    active = bool(
        (skill or skill_b)
        and (flags or elapsed or (extra & 1))
    )
    out.update(
        {
            "readable": readable,
            "active": active,
            "cast": cast,
            "skill_id": int(skill),
            "skill_id_b": int(skill_b),
            "flags": int(flags),
            "elapsed": int(elapsed),
            "extra_flags": int(extra),
        }
    )
    return out


def host_cast_active(
    session: GameAttachSession | None,
    *,
    log: LogFn | None = None,
) -> bool:
    """
    True if host open/skill session looks busy.

    skill cast-this (host+0x1A88 fields) OR host+0x41C session gate.
    False when unreadable (do not assume casting).
    @author by ak
    """
    return cast_session_busy(read_host_cast_state(session, log=log))


def wait_open_cast_bar(
    cfg: SuperLootConfig,
    *,
    stop_event: threading.Event | None = None,
    name: str = "",
    session: GameAttachSession | None = None,
    log: LogFn | None = None,
) -> str:
    """
    Wait until open cast finishes, then return so next open starts ASAP.

    With session (loot loop):
      1) Poll up to cast_start_grace_s for real cast/session (memory, debounced).
      2) If busy starts -> poll until idle (or cast_wait_s timeout).
      3) While cast runs: only poll cast state through RPM. Do not issue CRT.
      4) If never busy -> no_cast (MatterInteract ok but no session).
      5) Timeout without idle -> cast_timeout (not success).

    Busy = cast-this active OR host+0x41C session_active (matter open path).
    Without session (compat / unit / yaolu zero-wait callers):
      Returns no_cast — never invent success without memory proof.

    Returns: done | no_cast | cast_timeout | grace_stop | wait_stop | skip | started.
    @author by ak
    """
    log = log or (lambda _m: None)
    grace = max(0.0, float(cfg.cast_start_grace_s))
    duration = max(0.0, float(cfg.cast_wait_s))
    if grace <= 0 and duration <= 0:
        return "skip"
    label = name or "?"
    can_probe = session is not None and getattr(session, "pid", None)
    poll_s = float(DEFAULT_CAST_POLL_S)
    need_active = int(DEFAULT_CAST_ACTIVE_HITS)
    need_idle = int(DEFAULT_CAST_IDLE_HITS)

    if not can_probe:
        # No live session: cannot prove cast bar. Never count as open success.
        log(f"super_loot cast-no-session name={label!r}")
        return "no_cast"

    # Seed host/cast via pure RPM only. CRT GetHostPlayer during open hangs the
    # client (remote call va~0x827260 exceeded 2500ms → crash).
    try:
        from app.core.skill_cast_probe import resolve_cast_this

        seed = resolve_cast_this(
            session, log=lambda _m: None, with_dumps=False, allow_crt=False
        )
        if seed.ok:
            log(
                f"super_loot cast-seed ok host=0x{int(seed.host_ptr or 0):X} "
                f"cast=0x{int(seed.cast_this or 0):X} name={label!r}"
            )
        else:
            log(
                f"super_loot cast-seed miss name={label!r} "
                f"err={seed.error or seed.note or '?'}"
            )
    except Exception as e:
        log(f"super_loot cast-seed err name={label!r} {e}")

    # Wait for real cast/session; do not burn full bar on fake MatterInteract-ok.
    start_win = max(grace, 0.8)
    log(f"super_loot cast-start-grace {start_win:.1f}s name={label!r}")
    deadline = time.time() + start_win
    active_hits = 0
    saw = False
    last_st: dict = {}
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            log(f"super_loot cast-wait stopped (grace) name={label!r}")
            return "grace_stop"
        st = read_host_cast_state(session, log=log)
        last_st = st
        if cast_session_busy(st):
            active_hits += 1
            if active_hits >= need_active:
                saw = True
                break
        else:
            active_hits = 0
        time.sleep(poll_s)
    if not saw:
        log(
            f"super_loot cast-missed name={label!r} "
            f"{_format_cast_state(last_st)}"
        )
        return "no_cast"
    log(
        f"super_loot cast-started name={label!r} "
        f"{_format_cast_state(last_st)}"
    )
    if bool(cfg.confirm_cast_start_only):
        return "started"

    # Poll until cast/session returns idle; duration is a hard upper bound only.
    # Keep this phase RPM-only. Cache chaining supplies the next target after
    # the cast, so a speculative CRT scan here adds crash risk without removing
    # any user-visible wait.
    wait_cap = max(duration, 1.0)
    log(f"super_loot cast-wait max {wait_cap:.1f}s name={label!r}")
    end_deadline = time.time() + wait_cap
    idle_hits = 0
    while time.time() < end_deadline:
        if stop_event is not None and stop_event.is_set():
            log(f"super_loot cast-wait stopped name={label!r}")
            return "wait_stop"
        st = read_host_cast_state(session, log=log)
        last_st = st
        if not cast_session_busy(st):
            idle_hits += 1
            if idle_hits >= need_idle:
                log(
                    f"super_loot cast-done name={label!r} "
                    f"{_format_cast_state(last_st)}"
                )
                return "done"
        else:
            idle_hits = 0
        time.sleep(poll_s)
    log(
        f"super_loot cast-timeout name={label!r} "
        f"{_format_cast_state(last_st)}"
    )
    return "cast_timeout"


def _split_id(obj_id: int) -> tuple[int, int]:
    """Split int64 object id into lo/hi dwords. @author by ak"""
    oid = int(obj_id)
    return oid & 0xFFFFFFFF, (oid >> 32) & 0xFFFFFFFF


def _normalize_id(obj_id: int, log: LogFn) -> tuple[int, int, int]:
    """Return (oid_for_log, lo, hi) with dirty high stripped. @author by ak"""
    oid = int(obj_id)
    lo, hi = _split_id(oid)
    if hi and not (0x01000000 <= hi <= 0x03FFFFFF):
        log(f"super_loot drop dirty high 0x{hi:X}; use low32")
        return lo, lo, 0
    return oid, lo, hi


def _try_bridge(
    pid: int,
    hwnd: int,
    log: LogFn,
    *,
    inject_if_needed: bool = False,
):
    """
    Open shared-memory bridge; optionally reinject if mapping missing.

    @author by ak
    """
    try:
        return ensure_bridge(
            int(pid),
            log=log,
            inject_if_needed=bool(inject_if_needed),
            hwnd=int(hwnd) if hwnd else None,
            force_reinject=False,
        )
    except Exception as e:
        log(f"super_loot bridge open skip: {e}")
        return None


def open_target(
    session: GameAttachSession,
    target: SuperLootTarget,
    *,
    hwnd: int = 0,
    use_bridge: bool = True,
    use_dyn: bool = False,
    allow_reinject: bool = False,
    log: LogFn | None = None,
) -> dict:
    """
    Open / interact chest: SetTarget + AutoClickMatter on UI thread.

    PickItem is NOT used (chests need cast-bar session).
    allow_reinject: when mapping missing, try Delete inject once.

    @author by ak
    """
    log = log or (lambda _m: None)
    if target.obj_id is None or int(target.obj_id) <= 0:
        return {"ok": False, "error": "缺少 obj_id", "name": target.name, "mode": "open"}
    if target.tid is None:
        log(
            f"super_loot open aborted: missing tid name={target.name!r} "
            f"ptr=0x{int(target.ptr or 0):X} obj_id={target.obj_id}"
        )
        return {
            "ok": False,
            "error": "缺少 tid（AutoClickMatter 需要模板 id）",
            "name": target.name,
            "mode": "open",
        }

    oid, lo, hi = _normalize_id(int(target.obj_id), log)
    tid = int(target.tid) & 0xFFFFFFFF
    result: dict = {
        "ok": False,
        "mode": "open",
        "obj_id": oid,
        "tid": tid,
        "name": target.name,
        "dist": target.dist,
        "via": "",
        "attempts": [],
        "bridge_dead": False,
    }

    if not use_bridge or not session.pid:
        result["error"] = "开箱需要已注入桥接（主线程 AutoClickMatter）"
        result["bridge_dead"] = True
        log("super_loot open aborted: bridge required")
        return result

    br = _try_bridge(int(session.pid), hwnd, log, inject_if_needed=False)
    if br is None and allow_reinject:
        log("super_loot bridge missing; try reinject")
        br = _try_bridge(int(session.pid), hwnd, log, inject_if_needed=True)
    if br is None:
        result["error"] = "桥接未就绪，请先注入"
        result["bridge_dead"] = True
        return result

    try:
        st = br.call(
            CMD_SET_TARGET,
            id_lo=lo,
            id_hi=hi,
            hwnd=hwnd or None,
            timeout_ms=4000,
        )
        result["attempts"].append({"cmd": "SET_TARGET", **st.to_dict()})
        log(
            f"super_loot SetTarget ok={st.ok} ret={st.ret} status={st.status} "
            f"note={st.note!r} error={st.error!r} proto={st.protocol_version} "
            f"caps=0x{int(st.capabilities):X}"
        )

        # Native MatterInteract(this, lo, hi, tid) behind AutoClickMatter.
        # hi must be object tag (often 0x02000000); do not zero it.
        click_cmd = CMD_AUTO_CLICK_DYN_MATTER if use_dyn else CMD_AUTO_CLICK_MATTER
        ac = br.call(
            click_cmd,
            id_lo=lo,
            id_hi=hi,
            tid=tid,
            hwnd=hwnd or None,
            timeout_ms=5000,
        )
        result["attempts"].append(
            {
                "cmd": "MATTER_INTERACT_DYN" if use_dyn else "MATTER_INTERACT",
                **ac.to_dict(),
            }
        )
        log(
            f"super_loot MatterInteract ok={ac.ok} ret={ac.ret} "
            f"status={ac.status} note={ac.note!r} error={ac.error!r} "
            f"proto={ac.protocol_version} caps=0x{int(ac.capabilities):X}"
        )
        result["via"] = "bridge"
        result["ok"] = bool(ac.ok and int(ac.ret or 0) != 0)
        result["ret"] = ac.ret
        result["error"] = (
            None
            if result["ok"]
            else (ac.error or ac.note or "MatterInteract failed")
        )
        result["note"] = (
            "已发开箱/交互（MatterInteract）；请看角色是否读条"
            if result["ok"]
            else result["error"]
        )
        log(
            f"super_loot open ok={result['ok']} name={target.name!r} "
            f"id={oid} lo=0x{lo:X} hi=0x{hi:X} tid={tid} ret={ac.ret} "
            f"note={ac.note!r}"
        )
        return result
    except Exception as e:
        log(f"super_loot open err: {e}")
        result["error"] = str(e)
        result["attempts"].append({"error": str(e)})
        if is_bridge_error(str(e)):
            result["bridge_dead"] = True
        return result
    finally:
        try:
            br.close()
        except Exception:
            pass


def pick_ground_target(
    session: GameAttachSession,
    target: SuperLootTarget,
    *,
    hwnd: int = 0,
    use_bridge: bool = True,
    allow_remote_fallback: bool = False,
    log: LogFn | None = None,
) -> dict:
    """
    Ground loot path: SetTarget + PickItem.

    Prefer open_target for chests.

    @author by ak
    """
    log = log or (lambda _m: None)
    if target.obj_id is None or int(target.obj_id) <= 0:
        return {"ok": False, "error": "缺少 obj_id", "name": target.name, "mode": "pick"}

    oid, lo, hi = _normalize_id(int(target.obj_id), log)
    result: dict = {
        "ok": False,
        "mode": "pick",
        "obj_id": oid,
        "name": target.name,
        "dist": target.dist,
        "via": "",
        "attempts": [],
    }

    if use_bridge and session.pid:
        br = _try_bridge(int(session.pid), hwnd, log)
        if br is not None:
            try:
                st = br.call(
                    CMD_SET_TARGET,
                    id_lo=lo,
                    id_hi=hi,
                    hwnd=hwnd or None,
                    timeout_ms=4000,
                )
                result["attempts"].append({"cmd": "SET_TARGET", **st.to_dict()})
                pk = br.call(
                    CMD_PICK_ITEM,
                    id_lo=lo,
                    id_hi=hi,
                    hwnd=hwnd or None,
                    timeout_ms=4000,
                )
                result["attempts"].append({"cmd": "PICK_ITEM", **pk.to_dict()})
                result["via"] = "bridge"
                result["ok"] = bool(pk.ok or st.ok)
                result["ret"] = pk.ret
                result["error"] = None if result["ok"] else (pk.error or st.error)
                return result
            except Exception as e:
                result["attempts"].append({"error": str(e)})
            finally:
                try:
                    br.close()
                except Exception:
                    pass

    if not allow_remote_fallback:
        result["error"] = (
            "bridge pickup unavailable; raw remote action fallback is disabled"
        )
        result["bridge_dead"] = True
        return result

    st = set_target(session, lo if hi == 0 else oid, log=log)
    result["attempts"].append(st.to_dict())
    pr = pick_item(
        session,
        lo if hi == 0 else oid,
        name=target.name,
        tid=target.tid,
        dist=target.dist,
        kind="pickup",
        log=log,
    )
    result["attempts"].append(pr.to_dict())
    result["via"] = "remote"
    result["ok"] = bool(st.ok or pr.ok)
    result["ret"] = pr.ret if pr else st.ret
    result["error"] = None if result["ok"] else (pr.error or st.error)
    return result


def interact_target(
    session: GameAttachSession,
    target: SuperLootTarget,
    cfg: SuperLootConfig,
    *,
    hwnd: int = 0,
    allow_reinject: bool = False,
    log: LogFn | None = None,
) -> dict:
    """Dispatch open vs pick for one target. @author by ak"""
    log = log or (lambda _m: None)
    mode = resolve_interact_mode(cfg, target)
    if mode == MODE_OPEN:
        return open_target(
            session,
            target,
            hwnd=hwnd,
            use_bridge=cfg.use_bridge,
            use_dyn=cfg.use_dyn_click,
            allow_reinject=bool(allow_reinject),
            log=log,
        )
    return pick_ground_target(
        session,
        target,
        hwnd=hwnd,
        use_bridge=cfg.use_bridge,
        log=log,
    )


def _host_move_accepted(ret: int | None, status_ok: bool) -> bool:
    """
    HostMove success = bridge/status ok and game ret != 0.

    ret!=0 is only "call accepted"; still must observe displacement.
    @author by ak
    """
    if not status_ok:
        return False
    if ret is None:
        return True
    return int(ret) != 0


def resolve_host_move_mode(
    session: GameAttachSession | None,
    move_mode: int | None,
    *,
    scene_id: int | None = None,
    log: LogFn | None = None,
) -> int:
    """
    Resolve HostMove first arg like the dev workbench path panel.

    Policy (aligned with chest densest plan +「读场景」):
      - move_mode >= 0: use explicit value
      - else use live scene_id from GetCurrentScenePosition
      - if still unknown: 0

    Dev panel fills mode from scene_id when reading scene; densest plan
    uses mode_arg = move_mode or scene_id. Do not hardcode 0 only.
    @author by ak
    """
    log = log or (lambda _m: None)
    if move_mode is not None and int(move_mode) >= 0:
        m = int(move_mode)
        log(f"super_loot move_mode explicit={m}")
        return m
    sid = int(scene_id) if scene_id is not None else None
    if (sid is None or sid <= 0) and session is not None:
        try:
            sp = read_scene_position(session, log=lambda _m: None)
            if sp.ok and sp.scene_id is not None:
                sid = int(sp.scene_id)
        except Exception as e:
            log(f"super_loot resolve mode scene fail: {e}")
    if sid is not None and sid > 0:
        log(f"super_loot move_mode scene_id={sid}")
        return sid
    log("super_loot move_mode fallback=0")
    return 0


def host_move_mode_candidates(primary: int) -> list[int]:
    """
    Ordered modes to try for one hop.

    Prefer real scene mode first; then 0 (legacy live note).
    @author by ak
    """
    p = int(primary)
    out: list[int] = []
    for m in (p, 0):
        if m not in out:
            out.append(m)
    return out


def _approach_xyz(
    target: SuperLootTarget,
    host_pos: tuple[float, float, float] | None,
    *,
    stop_before: float = 1.5,
    lateral: float = 0.0,
    use_host_y: bool = True,
) -> tuple[float, float, float]:
    """
    Walk destination: stop slightly before matter on XZ plane.

    use_host_y: keep host height (matter Y often floats / unpathable).
    @author by ak
    """
    tx, ty, tz = float(target.x), float(target.y or 0.0), float(target.z)
    if host_pos is None:
        return tx, ty, tz
    hx, hy, hz = float(host_pos[0]), float(host_pos[1]), float(host_pos[2])
    dx, dz = tx - hx, tz - hz
    dist = math.sqrt(dx * dx + dz * dz)
    if dist < 0.05:
        return tx, (hy if use_host_y else ty), tz
    px, pz = -dz / dist, dx / dist
    sb = float(stop_before)
    if dist <= max(sb, 0.5) + 0.2:
        ax, az = tx, tz
    else:
        scale = (dist - sb) / dist
        ax, az = hx + dx * scale, hz + dz * scale
    if abs(float(lateral)) > 1e-3:
        ax += px * float(lateral)
        az += pz * float(lateral)
    ay = hy if use_host_y else (ty if ty else hy)
    return ax, ay, az


def _move_variants(
    target: SuperLootTarget,
    host_pos: tuple[float, float, float] | None,
    *,
    attempt: int = 0,
) -> list[tuple[float, float, float, str]]:
    """
    Ordered HostMove destinations for stuck recovery.

    attempt 0: normal approach; later attempts vary stop / lateral / center.
    @author by ak
    """
    variants: list[tuple[float, float, float, str]] = []
    specs: list[tuple[float, float, str]]
    if attempt <= 0:
        # Short first hop: stop closer so soft-walk (d~5) actually enters pick_range.
        specs = [(0.6, 0.0, "approach0.6"), (0.0, 0.0, "center")]
    elif attempt == 1:
        specs = [(0.0, 0.0, "center"), (1.2, 0.0, "approach1.2"), (1.5, 1.0, "left1")]
    elif attempt == 2:
        specs = [
            (0.4, 1.0, "left0.4"),
            (0.4, -1.0, "right0.4"),
            (0.0, 0.0, "center"),
            (2.0, 0.0, "approach2"),
        ]
    else:
        specs = [
            (1.5, 1.5, "left1.5"),
            (1.5, -1.5, "right1.5"),
            (0.0, 0.0, "center"),
            (2.5, 0.0, "approach2.5"),
        ]
    seen: set[tuple[float, float, float]] = set()
    for sb, lat, tag in specs:
        x, y, z = _approach_xyz(
            target, host_pos, stop_before=sb, lateral=lat, use_host_y=True
        )
        key = (round(x, 2), round(y, 2), round(z, 2))
        if key in seen:
            continue
        seen.add(key)
        variants.append((x, y, z, tag))
    return variants


def _issue_host_move_xyz(
    session: GameAttachSession,
    *,
    x: float,
    y: float,
    z: float,
    hwnd: int,
    mode: int,
    use_bridge: bool,
    prefer_remote: bool,
    tag: str,
    log: LogFn,
) -> dict:
    """
    Single HostMove(mode,x,y,z) via bridge and/or remote.

    Tries scene mode first, then 0 (dev-panel style).
    @author by ak
    """
    quiet = lambda _m: None  # noqa: E731
    out: dict = {
        "ok": False,
        "x": float(x),
        "y": float(y),
        "z": float(z),
        "mode": int(mode),
        "via": "",
        "variant": tag,
        "ret": None,
    }

    def bridge_once(m: int) -> dict | None:
        if not use_bridge or not session.pid:
            return None
        br = _try_bridge(int(session.pid), hwnd, quiet)
        if br is None:
            return None
        try:
            r = br.call(
                CMD_HOST_MOVE,
                x=float(x),
                y=float(y),
                z=float(z),
                mode=int(m),
                hwnd=hwnd or None,
                timeout_ms=4000,
            )
            accepted = _host_move_accepted(r.ret, bool(r.ok))
            local = {
                "ok": accepted,
                "via": "bridge",
                "ret": r.ret,
                "mode": int(m),
                "x": float(x),
                "y": float(y),
                "z": float(z),
                "variant": tag,
                "error": None if accepted else (r.error or f"HostMove ret={r.ret}"),
            }
            log(
                f"super_loot bridge move ok={accepted} ret={r.ret} "
                f"({x:.1f},{y:.1f},{z:.1f}) mode={m} var={tag}"
            )
            return local
        except Exception as e:
            log(f"super_loot bridge move err: {e}")
            return {
                "ok": False,
                "via": "bridge",
                "error": str(e),
                "variant": tag,
                "x": float(x),
                "y": float(y),
                "z": float(z),
            }
        finally:
            try:
                br.close()
            except Exception:
                pass

    def remote_once(m: int) -> dict:
        tgt = PathTarget(
            x=float(x),
            y=float(y),
            z=float(z),
            mode=int(m),
            map_hint=f"loot:{tag}",
        )
        r = host_move_to(
            session,
            tgt,
            log=quiet,
            fallback_mode0=(int(m) == 0),
        )
        d = r.to_dict()
        d["via"] = "remote"
        d["x"], d["y"], d["z"] = float(x), float(y), float(z)
        d["variant"] = tag
        d["mode"] = int(m)
        d["ok"] = bool(r.ok and _host_move_accepted(r.ret, True))
        if not d["ok"] and r.error is None:
            d["error"] = f"HostMove ret={r.ret}"
        log(
            f"super_loot remote move ok={d['ok']} ret={r.ret} "
            f"({x:.1f},{y:.1f},{z:.1f}) mode={m} var={tag}"
        )
        return d

    # Formal business never falls through from the UI-thread bridge to a raw
    # remote action. `prefer_remote` remains a compatibility hint only when
    # bridge use was explicitly disabled by a lab caller.
    order = ("bridge",) if use_bridge else ("remote",)
    modes = host_move_mode_candidates(int(mode))
    last: dict = out
    for via in order:
        if via == "bridge":
            for m in modes:
                got = bridge_once(m)
                if got is None:
                    break
                last = got
                out.update(got)
                if got.get("ok"):
                    return out
        else:
            for m in modes:
                got = remote_once(m)
                last = got
                out.update(got)
                if got.get("ok"):
                    return out
    return last if last else out


def move_to_target(
    session: GameAttachSession,
    target: SuperLootTarget,
    *,
    hwnd: int = 0,
    move_mode: int = DEFAULT_MOVE_MODE,
    use_bridge: bool = True,
    host_pos: tuple[float, float, float] | None = None,
    scene_id: int | None = None,
    attempt: int = 0,
    prefer_remote: bool = False,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> dict:
    """
    One-shot HostMove to approach point (no multi-hop).

    mode: live scene_id when move_mode < 0 (dev panel path mode).
    stuck recovery varies approach point via attempt.
    @author by ak
    """
    log = log or (lambda _m: None)
    quiet = lambda _m: None  # noqa: E731
    if target.x is None or target.y is None or target.z is None:
        return {"ok": False, "error": "目标无坐标", "name": target.name}

    if host_pos is None:
        sp = read_scene_position(session, log=quiet)
        if sp.ok and sp.scene_pos:
            host_pos = sp.scene_pos
            if scene_id is None and sp.scene_id is not None:
                scene_id = sp.scene_id
    if host_pos is None:
        return {"ok": False, "error": "无法读取宿主坐标", "name": target.name}

    mode = resolve_host_move_mode(session, move_mode, scene_id=scene_id, log=log)
    variants = _move_variants(target, host_pos, attempt=attempt)
    if not variants:
        return {"ok": False, "error": "无可用接近点", "name": target.name}

    last: dict = {
        "ok": False,
        "name": target.name,
        "attempt": int(attempt),
        "mode": mode,
        "tx": float(target.x),
        "ty": float(target.y or 0.0),
        "tz": float(target.z),
    }
    for x, y, z, tag in variants:
        if stop_event is not None and stop_event.is_set():
            break
        out = _issue_host_move_xyz(
            session,
            x=x,
            y=y,
            z=z,
            hwnd=hwnd,
            mode=mode,
            use_bridge=use_bridge,
            prefer_remote=prefer_remote or attempt >= 2,
            tag=tag,
            log=log,
        )
        out["name"] = target.name
        out["attempt"] = int(attempt)
        out["tx"] = float(target.x)
        out["ty"] = float(target.y or 0.0)
        out["tz"] = float(target.z)
        out["host_pos"] = list(host_pos)
        last = out
        if out.get("ok"):
            return out
    return last


def wait_until_in_range(
    session: GameAttachSession,
    target: SuperLootTarget,
    *,
    pick_range: float,
    timeout_s: float,
    hwnd: int = 0,
    move_mode: int = DEFAULT_MOVE_MODE,
    use_bridge: bool = True,
    scene_id: int | None = None,
    nudge_s: float = DEFAULT_MOVE_NUDGE_S,
    max_nudges: int = DEFAULT_MAX_STUCK_NUDGES,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> tuple[bool, float | None, tuple[float, float, float] | None]:
    """
    Poll host position until near target or timeout/stop.

    Re-issues one-shot HostMove with alternate points when stuck.
    Near hops use shorter nudge_s / max_nudges from caller.
    @author by ak
    """
    log = log or (lambda _m: None)
    quiet = lambda _m: None  # noqa: E731
    if target.x is None:
        return False, None, None
    tpos = (float(target.x), float(target.y or 0.0), float(target.z or 0.0))
    deadline = time.time() + max(float(timeout_s), 0.5)
    last_dist: float | None = None
    last_host: tuple[float, float, float] | None = None
    stuck_anchor: tuple[float, float, float] | None = None
    stuck_since = 0.0
    nudge_count = 0
    in_range_hits = 0
    stopped_hits = 0
    previous_in_range: tuple[float, float, float] | None = None
    ns = max(float(nudge_s), 0.6)
    # First re-issue sooner than historical 4s so soft-walk d~5 is not idle long.
    next_nudge = time.time() + ns
    stuck_need = max(ns * 0.75, 0.7)
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False, last_dist, last_host
        sp = read_scene_position(session, log=quiet)
        if sp.ok and sp.scene_pos:
            last_host = sp.scene_pos
            if scene_id is None and sp.scene_id is not None:
                scene_id = sp.scene_id
            last_dist = _dist3(sp.scene_pos, tpos, horizontal=True)
            if last_dist <= float(pick_range):
                in_range_hits += 1
                if previous_in_range is not None and _dist3(
                    last_host, previous_in_range, horizontal=True
                ) <= float(DEFAULT_ARRIVE_STOP_DELTA_M):
                    stopped_hits += 1
                else:
                    stopped_hits = 0
                previous_in_range = last_host
                # A range hit alone may be a moving player passing the target.
                # Require three in-range samples and two stationary deltas.
                if in_range_hits >= 3 and stopped_hits >= int(DEFAULT_ARRIVE_STOP_HITS):
                    log(
                        f"super_loot arrived+stopped d={last_dist:.2f} "
                        f"<= {pick_range}"
                    )
                    return True, last_dist, last_host
                time.sleep(0.15)
                continue
            in_range_hits = 0
            stopped_hits = 0
            previous_in_range = None
            if stuck_anchor is None:
                stuck_anchor = last_host
                stuck_since = time.time()
            else:
                moved = _dist3(last_host, stuck_anchor, horizontal=True)
                if moved > 0.25:
                    stuck_anchor = last_host
                    stuck_since = time.time()
            idle = time.time() - stuck_since
            if time.time() >= next_nudge and idle >= stuck_need:
                if nudge_count >= int(max_nudges):
                    log(
                        f"super_loot stuck give-up nudges={nudge_count} "
                        f"last_d={last_dist:.1f}"
                    )
                    return False, last_dist, last_host
                nudge_count += 1
                log(
                    f"super_loot move nudge#{nudge_count} stuck_d={last_dist:.1f} "
                    f"idle={idle:.1f}s"
                )
                move_to_target(
                    session,
                    target,
                    hwnd=hwnd,
                    move_mode=move_mode,
                    use_bridge=use_bridge,
                    host_pos=last_host,
                    scene_id=scene_id,
                    attempt=nudge_count,
                    prefer_remote=(nudge_count >= 2),
                    stop_event=stop_event,
                    log=log,
                )
                next_nudge = time.time() + ns
                stuck_anchor = last_host
                stuck_since = time.time()
            # Poll faster when already close (soft-walk / almost in range).
            sleep_s = 0.2 if last_dist <= float(pick_range) + 3.0 else 0.4
        else:
            in_range_hits = 0
            sleep_s = 0.4
        time.sleep(sleep_s)
    log(f"super_loot arrive timeout last_d={last_dist}")
    return False, last_dist, last_host


def super_loot_step(
    session: GameAttachSession,
    cfg: SuperLootConfig,
    *,
    hwnd: int = 0,
    stop_event: threading.Event | None = None,
    skip_ids: dict[int, float] | None = None,
    skip_points: list[tuple[float, float, float]] | None = None,
    path_failure_counts: dict[int, int] | None = None,
    no_cast_attempts: dict[tuple[int, int], int] | None = None,
    no_cast_cooldowns: dict[tuple[int, int], float] | None = None,
    scan_cache: ScanCache | None = None,
    crt_cooldown_until: float = 0.0,
    bridge_cooldown_until: float = 0.0,
    allow_bridge_reinject: bool = False,
    log: LogFn | None = None,
) -> SuperLootStepResult:
    """
    One cycle: find named item -> open / path+open.

    skip_ids / skip_points / scan_cache: see prior docs.
    crt_cooldown_until: skip full CRT scan (and scene CRT when hard).
    bridge_cooldown_until: do not hammer open when bridge mapping is dead.
    allow_bridge_reinject: one-shot reinject permission for this step.
    @author by ak
    """
    log = log or (lambda _m: None)
    if stop_event is not None and stop_event.is_set():
        return SuperLootStepResult(ok=False, action="stop", message="已停止")

    now = time.time()
    host_pos = None
    scene_id = None
    # Skip scene CRT while cooling down — VirtualAllocEx err=5 spam kills process.
    if float(crt_cooldown_until or 0.0) > now:
        if scan_cache and scan_cache.host_pos is not None:
            host_pos = scan_cache.host_pos
        log("super_loot skip scene read (crt cooldown)")
    else:
        try:
            sp = read_scene_position(session, log=log)
            if sp.ok and sp.scene_pos:
                host_pos = sp.scene_pos
                scene_id = sp.scene_id
            elif sp.error and is_attach_hard_error(str(sp.error)):
                log(f"super_loot scene pos hard: {sp.error}")
                if scan_cache and scan_cache.host_pos is not None:
                    host_pos = scan_cache.host_pos
        except Exception as e:
            if is_attach_hard_error(str(e)):
                log(f"super_loot scene pos hard: {e}")
                if scan_cache and scan_cache.host_pos is not None:
                    host_pos = scan_cache.host_pos
            else:
                log(f"super_loot scene pos: {e}")

    # Bridge dead: do not open/move; wait for reinject outside.
    if float(bridge_cooldown_until or 0.0) > now:
        remain = float(bridge_cooldown_until) - now
        log(f"super_loot bridge-cooldown {remain:.1f}s left; skip open")
        return SuperLootStepResult(
            ok=False,
            action="error",
            message=f"桥接冷却中 {remain:.0f}s，请注入或等待自动恢复",
            error="bridge_cooldown",
            host_pos=list(host_pos) if host_pos else None,
            scene_id=scene_id,
            scan_cache=scan_cache,
            dense=bool(scan_cache.dense) if scan_cache else False,
            scan_source="bridge_cooldown",
        )

    dense_flag = bool(scan_cache.dense) if scan_cache else False
    new_cache = scan_cache
    src = "none"
    try:
        hits, new_cache, src, dense_flag = resolve_hits_for_step(
            session,
            cfg,
            host_pos=host_pos,
            scene_id=scene_id,
            scan_cache=scan_cache,
            skip_ids=skip_ids,
            skip_points=skip_points,
            crt_cooldown_until=float(crt_cooldown_until or 0.0),
            log=log,
        )
    except Exception as e:
        # Preserve cache for next step; runner will enter cooldown.
        return SuperLootStepResult(
            ok=False,
            action="error",
            message=f"扫描失败: {e}",
            error=str(e),
            host_pos=list(host_pos) if host_pos else None,
            scene_id=scene_id,
            scan_cache=scan_cache,
            dense=dense_flag,
            scan_source="error",
        )

    now = time.time()
    purge_skip_ids(skip_ids, now=now)
    purge_skip_points(skip_points, now=now)
    raw_n = len(hits)
    if bool(cfg.filter_unreachable) and (skip_ids or skip_points):
        filtered = filter_reachable_hits(
            hits,
            skip_ids,
            now=now,
            near_m=float(cfg.unreachable_near_m),
            skip_points=skip_points,
        )
        if len(filtered) < len(hits):
            log(
                f"super_loot prefilter unreachable "
                f"drop={len(hits) - len(filtered)} keep={len(filtered)}"
            )
        # Dense field recovery: scan still sees chests but soft bans wiped all.
        if not filtered and raw_n > 0:
            purged = purge_soft_runtime_skips(
                skip_ids, skip_points, now=now, log=log
            )
            if purged:
                filtered = filter_reachable_hits(
                    hits,
                    skip_ids,
                    now=now,
                    near_m=float(cfg.unreachable_near_m),
                    skip_points=skip_points,
                )
                log(
                    f"super_loot prefilter after soft-purge "
                    f"keep={len(filtered)} raw={raw_n}"
                )
        hits = filtered

    hits, no_cast_blocked = _filter_no_cast_cooldowns(
        hits,
        no_cast_cooldowns,
        scene_id=scene_id,
        now=now,
    )
    if no_cast_blocked:
        for blocked in no_cast_blocked:
            new_cache = _prune_cache_hits(new_cache, blocked)
        log(
            f"super_loot no-cast cooldown drop={len(no_cast_blocked)} "
            f"keep={len(hits)}"
        )

    if not hits:
        msg = (
            f"附近无「{cfg.item_name}」"
            if raw_n == 0
            else f"附近「{cfg.item_name}」均不可达/已过滤 (raw={raw_n})"
        )
        keep_batch_cache = bool(
            new_cache and new_cache.dense and not new_cache.batch_complete
        )
        return SuperLootStepResult(
            ok=False,
            action="none",
            message=msg,
            matched=raw_n,
            host_pos=list(host_pos) if host_pos else None,
            scene_id=scene_id,
            # An unfinished dense batch must retain its inspected-pointer cursor
            # so the next loop advances beyond the nearest 12 candidates.
            scan_cache=new_cache if keep_batch_cache else None,
            dense=dense_flag,
            scan_source=src,
        )

    target, select_tag = select_loot_target(
        hits,
        cfg,
        host_pos=host_pos,
        session=session,
        skip_ids=skip_ids,
        skip_points=skip_points,
        log=log,
    )
    if target is None and select_tag == "all_unreachable" and raw_n > 0:
        purged = purge_soft_runtime_skips(
            skip_ids, skip_points, now=now, log=log
        )
        if purged:
            target, select_tag = select_loot_target(
                hits if hits else [],
                cfg,
                host_pos=host_pos,
                session=session,
                skip_ids=skip_ids,
                skip_points=skip_points,
                log=log,
            ) if hits else (None, select_tag)
            # hits may already be hard-filtered empty; re-filter from cache raw.
            if target is None and new_cache and new_cache.hits:
                recovered = filter_reachable_hits(
                    list(new_cache.hits),
                    skip_ids,
                    now=now,
                    near_m=float(cfg.unreachable_near_m),
                    skip_points=skip_points,
                )
                if recovered:
                    hits = recovered
                    target, select_tag = select_loot_target(
                        hits,
                        cfg,
                        host_pos=host_pos,
                        session=session,
                        skip_ids=skip_ids,
                        skip_points=skip_points,
                        log=log,
                    )
    if target is None:
        if select_tag == "skip_isolated_far":
            msg = f"仅有远距孤立箱，已放弃 (raw={raw_n})"
        else:
            msg = f"附近「{cfg.item_name}」均不可达/已过滤 (raw={raw_n})"
        # Soft bans / stale pocket: drop cache so next loop full-scans again.
        if raw_n > 0 and (skip_ids is not None or skip_points is not None):
            purge_soft_runtime_skips(
                skip_ids, skip_points, now=time.time(), log=log
            )
        return SuperLootStepResult(
            ok=False,
            action="none",
            message=msg,
            matched=raw_n,
            host_pos=list(host_pos) if host_pos else None,
            scene_id=scene_id,
            scan_cache=(
                new_cache
                if new_cache and new_cache.dense and not new_cache.batch_complete
                else None
            ),
            dense=dense_flag,
            scan_source=src,
        )
    pr = float(cfg.pick_range)
    # Live host sample right before open/path — cache host from scan can lag a
    # step after cast, making a face-box look like d>pick_range and force walk.
    if float(crt_cooldown_until or 0.0) <= time.time():
        try:
            sp_live = read_scene_position(session, log=lambda _m: None)
            if sp_live.ok and sp_live.scene_pos:
                host_pos = sp_live.scene_pos
                if sp_live.scene_id is not None:
                    scene_id = sp_live.scene_id
        except Exception:
            pass
    if host_pos is not None and target.x is not None:
        dist = _dist3(
            host_pos,
            (float(target.x), float(target.y or 0), float(target.z or 0)),
            horizontal=True,
        )
        target.dist = dist
    else:
        dist = target.dist
    tdict = target.to_dict()
    tdict["select"] = select_tag
    imode = resolve_interact_mode(cfg, target)
    verb = "开箱" if imode == MODE_OPEN else "拾取"

    def _do_interact(act: str) -> SuperLootStepResult:
        ix = interact_target(
            session,
            target,
            cfg,
            hwnd=hwnd,
            allow_reinject=bool(allow_bridge_reinject),
            log=log,
        )
        ok = bool(ix.get("ok"))
        cast_st = "skip"
        no_cast_attempt = 0
        no_cast_backoff = False
        retry_key = _no_cast_key(target, scene_id)
        # Blacklist opened or hard-failed ids so loop walks to next chest.
        cache_after = new_cache
        # MatterInteract ok alone is not open success — need real cast bar.
        # Exception: cast_wait disabled (grace=0 & wait=0) is for callers like
        # yaolu that verify completion via dialog, not cast-this.
        cast_required = (
            float(cfg.cast_start_grace_s) > 0.0 or float(cfg.cast_wait_s) > 0.0
        )
        if ok and imode == MODE_OPEN:
            cast_st = wait_open_cast_bar(
                cfg,
                stop_event=stop_event,
                name=str(target.name or ""),
                session=session,
                log=log,
            )
            if cast_st == "skip" and not cast_required:
                # Explicit zero-wait path: keep interact ok for dialog listeners.
                ok = True
            elif cast_st == "started":
                # Entry path: memory has proven the cast actually started.
                ok = True
            elif cast_st == "done":
                # Only memory-proven cast start+idle counts as open success.
                ok = True
            elif cast_st == "no_cast":
                ok = False
                limit = max(1, int(cfg.no_cast_retry_limit))
                if no_cast_attempts is not None:
                    no_cast_attempt = int(no_cast_attempts.get(retry_key, 0)) + 1
                    if no_cast_attempt >= limit:
                        no_cast_attempts.pop(retry_key, None)
                        if no_cast_cooldowns is not None:
                            no_cast_cooldowns[retry_key] = (
                                time.time() + max(0.5, float(cfg.skip_no_cast_s))
                            )
                        cache_after = _prune_cache_hits(cache_after or new_cache, target)
                        no_cast_backoff = True
                        log(
                            f"super_loot no-cast backoff attempt={no_cast_attempt}/{limit} "
                            f"ttl={float(cfg.skip_no_cast_s):.1f}s name={target.name!r}"
                        )
                    else:
                        no_cast_attempts[retry_key] = no_cast_attempt
                        cache_after = cache_after or new_cache
                        log(
                            f"super_loot no-cast retry attempt={no_cast_attempt}/{limit} "
                            f"name={target.name!r}"
                        )
                else:
                    # Stateless/manual callers retain the candidate; the runner
                    # supplies retry maps for bounded automatic retries.
                    cache_after = cache_after or new_cache
            else:
                # grace_stop / wait_stop / cast_timeout: not success.
                ok = False
                if cast_st == "cast_timeout" and (
                    skip_ids is not None or skip_points is not None
                ):
                    mark_unreachable(
                        skip_ids,
                        target,
                        ttl_s=max(float(cfg.skip_opened_s), float(cfg.skip_no_cast_s)),
                        reason="cast-timeout",
                        log=log,
                        skip_points=None,
                        record_near_point=False,
                    )
                if cast_st == "cast_timeout":
                    cache_after = _keep_face_local_cache(
                        _prune_cache_hits(cache_after or new_cache, target),
                        host_pos=host_pos,
                        pick_range=float(cfg.pick_range),
                        log=log,
                    )
        if skip_ids is not None or skip_points is not None:
            if ok:
                # Opened: skip this chest only — never near-radius wipe.
                mark_unreachable(
                    skip_ids,
                    target,
                    ttl_s=float(cfg.skip_opened_s),
                    reason="opened",
                    log=log,
                    skip_points=None,
                    record_near_point=False,
                )
                # Keep the short-range cached pocket; the next loop can walk
                # to an already-scanned nearby chest without another CRT scan.
                cache_after = _prune_cache_hits(cache_after or new_cache, target)
            elif cast_st not in ("no_cast", "cast_timeout") and (
                ix.get("ret") == 0 or (ix.get("error") or "").find("false") >= 0
            ):
                mark_unreachable(
                    skip_ids,
                    target,
                    ttl_s=min(float(cfg.skip_opened_s), 6.0),
                    reason="interact-false",
                    log=log,
                    skip_points=None,
                    record_near_point=False,
                )
                cache_after = _keep_face_local_cache(
                    _prune_cache_hits(cache_after or new_cache, target),
                    host_pos=host_pos,
                    pick_range=float(cfg.pick_range),
                    log=log,
                )
            elif ix.get("bridge_dead") or is_bridge_error(str(ix.get("error") or "")):
                # Do not burn the same chest while bridge is dead.
                pass
        if cast_st != "no_cast" and no_cast_attempts is not None:
            no_cast_attempts.pop(retry_key, None)
        if ok:
            if no_cast_attempts is not None:
                no_cast_attempts.pop(retry_key, None)
            if no_cast_cooldowns is not None:
                no_cast_cooldowns.pop(retry_key, None)
        if ok and imode == MODE_OPEN and cast_st == "started":
            msg_ok = "已确认读条开始"
        elif ok and imode == MODE_OPEN and cast_st == "done":
            msg_ok = "读条完成"
        elif ok and imode == MODE_OPEN and cast_st == "skip":
            msg_ok = "已交互"
        elif cast_st == "no_cast" and no_cast_backoff:
            msg_ok = f"连续{no_cast_attempt}次未见读条，短暂跳过"
        elif cast_st == "no_cast":
            limit = max(1, int(cfg.no_cast_retry_limit))
            msg_ok = (
                f"未见读条，重试 {no_cast_attempt}/{limit}"
                if no_cast_attempt
                else "未见读条，重试"
            )
        elif cast_st == "cast_timeout":
            msg_ok = "读条超时"
        elif imode == MODE_OPEN:
            msg_ok = "失败"
        else:
            # Ground pick: API ok is not counted as open success in UI.
            msg_ok = "已调用拾取" if ok else "失败"
        err = (
            ("no_cast_backoff" if no_cast_backoff else "no_cast")
            if cast_st == "no_cast"
            else (
                "cast_timeout"
                if cast_st == "cast_timeout"
                else (None if ok else (ix.get("error") or cast_st))
            )
        )
        fail_detail = str(ix.get("error") or cast_st or "失败")
        show_msg = (
            msg_ok
            if ok or cast_st in ("no_cast", "cast_timeout")
            else fail_detail
        )
        return SuperLootStepResult(
            ok=ok,
            action=act,
            message=(
                f"{verb} d={dist:.1f} 「{target.name}」 {show_msg}"
                if dist is not None
                else f"{verb} 「{target.name}」 {show_msg}"
            ),
            target=tdict,
            host_pos=list(host_pos) if host_pos else None,
            scene_id=scene_id,
            interact=ix,
            matched=len(hits),
            error=err,
            scan_cache=cache_after,
            dense=dense_flag,
            scan_source=src,
        )

    if dist is not None and dist <= pr:
        log(
            f"super_loot in-range {imode} d={dist:.2f} "
            f"select={select_tag} name={target.name!r} src={src}"
        )
        return _do_interact("open" if imode == MODE_OPEN else "pick")

    # d > pick_range (~2.5m): always walk in first. Soft stand-open at 3–5m
    # is MatterInteract-ok without cast (log: 无读条 then 补开/寻路 thrash).
    dd = f"{dist:.1f}" if dist is not None else "n/a"
    near_hop = dist is not None and dist <= (
        pr + float(DEFAULT_NEAR_HOP_EXTRA)
    )
    arrive_to = (
        min(float(cfg.arrive_timeout_s), float(DEFAULT_NEAR_ARRIVE_TIMEOUT_S))
        if near_hop
        else float(cfg.arrive_timeout_s)
    )
    nudge_s = (
        float(DEFAULT_NEAR_MOVE_NUDGE_S)
        if near_hop
        else float(DEFAULT_MOVE_NUDGE_S)
    )
    max_nudges = (
        int(DEFAULT_NEAR_MAX_STUCK_NUDGES)
        if near_hop
        else int(DEFAULT_MAX_STUCK_NUDGES)
    )
    log(
        f"super_loot path then {imode} d={dd} select={select_tag} "
        f"name={target.name!r} near_hop={near_hop}"
    )
    mv = move_to_target(
        session,
        target,
        hwnd=hwnd,
        move_mode=cfg.move_mode,
        use_bridge=cfg.use_bridge,
        host_pos=host_pos,
        scene_id=scene_id,
        attempt=0,
        stop_event=stop_event,
        log=log,
    )
    if not mv.get("ok"):
        # Still wait a bit: some clients walk with ret=0, or path delayed.
        log(
            f"super_loot move not accepted ret={mv.get('ret')} "
            f"mode={mv.get('mode')} err={mv.get('error')}; still wait for movement"
        )

    arrived, last_d, last_host = wait_until_in_range(
        session,
        target,
        pick_range=pr,
        timeout_s=arrive_to,
        hwnd=hwnd,
        move_mode=cfg.move_mode,
        use_bridge=cfg.use_bridge,
        scene_id=scene_id,
        nudge_s=nudge_s,
        max_nudges=max_nudges,
        stop_event=stop_event,
        log=log,
    )
    if stop_event is not None and stop_event.is_set():
        return SuperLootStepResult(
            ok=False,
            action="stop",
            message="寻路中停止",
            target=tdict,
            move=mv,
            matched=len(hits),
            scan_cache=new_cache,
            dense=dense_flag,
            scan_source=src,
        )
    if not arrived:
        ld = f"{last_d:.1f}" if last_d is not None else "n/a"
        # Empirical unreachable: HostMove ret ok but no displacement / timeout.
        # Only ban this id/XZ — never near-wipe dense open fields.
        if bool(cfg.filter_unreachable):
            mark_weighted_path_failure(
                skip_ids,
                skip_points,
                path_failure_counts,
                target,
                reason=f"path-stuck last_d={ld}",
                log=log,
            )
        # Last resort: only open if already inside true pick_range (soft margin
        # MatterInteract-ok often has no cast bar — do not fake-open at 5–7m).
        if last_d is not None and imode == MODE_OPEN and last_d <= pr:
            log(f"super_loot last-chance open d={last_d:.1f}")
            target.dist = last_d
            chance = _do_interact("open")
            chance.move = mv
            chance.host_pos = list(last_host) if last_host else chance.host_pos
            if chance.ok:
                return chance
        return SuperLootStepResult(
            ok=False,
            action="path",
            message=f"不可达/卡住 last_d={ld}，已过滤",
            target=tdict,
            host_pos=list(last_host) if last_host else None,
            scene_id=scene_id,
            move=mv,
            matched=len(hits),
            error="unreachable/stuck",
            scan_cache=new_cache,
            dense=dense_flag,
            scan_source=src,
        )

    # Fresh scene sample right before open (not only the last wait poll).
    # Path must finish first: never open while still mid-walk.
    quiet = lambda _m: None  # noqa: E731
    sp_final = read_scene_position(session, log=quiet)
    if sp_final.ok and sp_final.scene_pos and target.x is not None:
        last_host = sp_final.scene_pos
        last_d = _dist3(
            last_host,
            (float(target.x), float(target.y or 0), float(target.z or 0)),
            horizontal=True,
        )
    elif last_host is not None and target.x is not None:
        last_d = _dist3(
            last_host,
            (float(target.x), float(target.y or 0), float(target.z or 0)),
            horizontal=True,
        )
    if last_d is None or last_d > pr:
        ld = f"{last_d:.1f}" if last_d is not None else "n/a"
        log(f"super_loot arrive recheck fail d={ld} > pr={pr:.1f}; no open")
        if bool(cfg.filter_unreachable):
            mark_weighted_path_failure(
                skip_ids,
                skip_points,
                path_failure_counts,
                target,
                reason=f"arrive-recheck last_d={ld}",
                log=log,
            )
        return SuperLootStepResult(
            ok=False,
            action="path",
            message=f"寻路后仍偏远 last_d={ld}，已过滤",
            target=tdict,
            host_pos=list(last_host) if last_host else None,
            scene_id=scene_id,
            move=mv,
            matched=len(hits),
            error="arrive recheck fail",
            scan_cache=new_cache,
            dense=dense_flag,
            scan_source=src,
        )
    target.dist = float(last_d)
    dist = float(last_d)
    tdict["dist"] = float(last_d)
    log(
        f"super_loot arrived d={last_d:.2f} <= range={pr:.2f}; "
        f"now interact name={target.name!r} tid={target.tid}"
    )
    res = _do_interact("path_open" if imode == MODE_OPEN else "path_pick")
    res.move = mv
    res.host_pos = list(last_host) if last_host else res.host_pos
    return res


def open_specific_target(
    session: GameAttachSession,
    target: SuperLootTarget,
    cfg: SuperLootConfig,
    *,
    hwnd: int = 0,
    stop_event: threading.Event | None = None,
    skip_ids: dict[int, float] | None = None,
    skip_points: list[tuple[float, float, float]] | None = None,
    path_failure_counts: dict[int, int] | None = None,
    log: LogFn | None = None,
) -> SuperLootStepResult:
    """
    Path/open a user-selected match (UI double-click).

    Same open/path rules as one auto step, but target is fixed.
    @author by ak
    """
    log = log or (lambda _m: None)
    if stop_event is not None and stop_event.is_set():
        return SuperLootStepResult(ok=False, action="stop", message="已停止")

    host_pos = None
    scene_id = None
    sp = read_scene_position(session, log=log)
    if sp.ok and sp.scene_pos:
        host_pos = sp.scene_pos
        scene_id = sp.scene_id

    if host_pos is not None and target.x is not None:
        target.dist = _dist3(
            host_pos,
            (float(target.x), float(target.y or 0), float(target.z or 0)),
            horizontal=True,
        )
    pr = float(cfg.pick_range)
    dist = target.dist
    tdict = target.to_dict()
    tdict["select"] = "manual"
    imode = resolve_interact_mode(cfg, target)
    verb = "开箱" if imode == MODE_OPEN else "拾取"

    def _do_interact(act: str) -> SuperLootStepResult:
        ix = interact_target(session, target, cfg, hwnd=hwnd, log=log)
        ok = bool(ix.get("ok"))
        cast_st = "skip"
        cast_required = (
            float(cfg.cast_start_grace_s) > 0.0 or float(cfg.cast_wait_s) > 0.0
        )
        if ok and imode == MODE_OPEN:
            cast_st = wait_open_cast_bar(
                cfg,
                stop_event=stop_event,
                name=str(target.name or ""),
                session=session,
                log=log,
            )
            if cast_st == "skip" and not cast_required:
                ok = True
            elif cast_st == "started":
                ok = True
            elif cast_st == "done":
                ok = True
            elif cast_st == "no_cast":
                ok = False
                if skip_ids is not None or skip_points is not None:
                    mark_unreachable(
                        skip_ids,
                        target,
                        ttl_s=float(cfg.skip_no_cast_s),
                        reason="no-cast",
                        log=log,
                        skip_points=None,
                        record_near_point=False,
                    )
            else:
                ok = False
                if cast_st == "cast_timeout" and (
                    skip_ids is not None or skip_points is not None
                ):
                    mark_unreachable(
                        skip_ids,
                        target,
                        ttl_s=max(float(cfg.skip_opened_s), float(cfg.skip_no_cast_s)),
                        reason="cast-timeout",
                        log=log,
                        skip_points=None,
                        record_near_point=False,
                    )
        if skip_ids is not None or skip_points is not None:
            if ok:
                mark_unreachable(
                    skip_ids,
                    target,
                    ttl_s=float(cfg.skip_opened_s),
                    reason="opened",
                    log=log,
                    skip_points=None,
                    record_near_point=False,
                )
            elif cast_st not in ("no_cast", "cast_timeout") and (
                ix.get("ret") == 0 or (ix.get("error") or "").find("false") >= 0
            ):
                mark_unreachable(
                    skip_ids,
                    target,
                    ttl_s=min(float(cfg.skip_opened_s), 6.0),
                    reason="interact-false",
                    log=log,
                    skip_points=None,
                    record_near_point=False,
                )
        if ok and imode == MODE_OPEN and cast_st == "started":
            msg_ok = "已确认读条开始"
        elif ok and imode == MODE_OPEN and cast_st == "done":
            msg_ok = "读条完成"
        elif ok and imode == MODE_OPEN and cast_st == "skip":
            msg_ok = "已交互"
        elif cast_st == "no_cast":
            msg_ok = "无读条，跳过"
        elif cast_st == "cast_timeout":
            msg_ok = "读条超时"
        elif imode == MODE_OPEN:
            msg_ok = "失败"
        else:
            msg_ok = "已调用拾取" if ok else "失败"
        fail_detail = str(ix.get("error") or cast_st or "失败")
        show_msg = msg_ok if ok or cast_st in ("no_cast", "cast_timeout") else fail_detail
        return SuperLootStepResult(
            ok=ok,
            action=act,
            message=(
                f"{verb} d={dist:.1f} 「{target.name}」 {show_msg}"
                if dist is not None
                else f"{verb} 「{target.name}」 {show_msg}"
            ),
            target=tdict,
            host_pos=list(host_pos) if host_pos else None,
            scene_id=scene_id,
            interact=ix,
            matched=1,
            error=(
                "no_cast"
                if cast_st == "no_cast"
                else (
                    "cast_timeout"
                    if cast_st == "cast_timeout"
                    else (None if ok else (ix.get("error") or cast_st))
                )
            ),
        )

    if dist is not None and dist <= pr:
        log(f"super_loot manual in-range {imode} d={dist:.2f} name={target.name!r}")
        return _do_interact("open" if imode == MODE_OPEN else "pick")

    # Beyond pick_range: walk in first (no soft stand-open).
    dd = f"{dist:.1f}" if dist is not None else "n/a"
    near_hop = dist is not None and dist <= (
        pr + float(DEFAULT_NEAR_HOP_EXTRA)
    )
    arrive_to = (
        min(float(cfg.arrive_timeout_s), float(DEFAULT_NEAR_ARRIVE_TIMEOUT_S))
        if near_hop
        else float(cfg.arrive_timeout_s)
    )
    nudge_s = (
        float(DEFAULT_NEAR_MOVE_NUDGE_S)
        if near_hop
        else float(DEFAULT_MOVE_NUDGE_S)
    )
    max_nudges = (
        int(DEFAULT_NEAR_MAX_STUCK_NUDGES)
        if near_hop
        else int(DEFAULT_MAX_STUCK_NUDGES)
    )
    log(f"super_loot manual path then {imode} d={dd} name={target.name!r}")
    mv = move_to_target(
        session,
        target,
        hwnd=hwnd,
        move_mode=cfg.move_mode,
        use_bridge=cfg.use_bridge,
        host_pos=host_pos,
        scene_id=scene_id,
        attempt=0,
        stop_event=stop_event,
        log=log,
    )
    arrived, last_d, last_host = wait_until_in_range(
        session,
        target,
        pick_range=pr,
        timeout_s=arrive_to,
        hwnd=hwnd,
        move_mode=cfg.move_mode,
        use_bridge=cfg.use_bridge,
        scene_id=scene_id,
        nudge_s=nudge_s,
        max_nudges=max_nudges,
        stop_event=stop_event,
        log=log,
    )
    if stop_event is not None and stop_event.is_set():
        return SuperLootStepResult(
            ok=False,
            action="stop",
            message="寻路中停止",
            target=tdict,
            move=mv,
            matched=1,
        )
    if not arrived:
        ld = f"{last_d:.1f}" if last_d is not None else "n/a"
        if bool(cfg.filter_unreachable):
            mark_weighted_path_failure(
                skip_ids,
                skip_points,
                path_failure_counts,
                target,
                reason=f"manual-stuck last_d={ld}",
                log=log,
            )
        return SuperLootStepResult(
            ok=False,
            action="path",
            message=f"手动寻路未到达 last_d={ld}，已过滤",
            target=tdict,
            host_pos=list(last_host) if last_host else None,
            scene_id=scene_id,
            move=mv,
            matched=1,
            error="manual path stuck",
        )
    # Fresh scene sample right before open (not only the last wait poll).
    quiet = lambda _m: None  # noqa: E731
    sp_final = read_scene_position(session, log=quiet)
    if sp_final.ok and sp_final.scene_pos and target.x is not None:
        last_host = sp_final.scene_pos
        last_d = _dist3(
            last_host,
            (float(target.x), float(target.y or 0), float(target.z or 0)),
            horizontal=True,
        )
    elif last_host is not None and target.x is not None:
        last_d = _dist3(
            last_host,
            (float(target.x), float(target.y or 0), float(target.z or 0)),
            horizontal=True,
        )
    if last_d is None or last_d > pr:
        ld = f"{last_d:.1f}" if last_d is not None else "n/a"
        log(f"super_loot manual arrive recheck fail d={ld} > pr={pr:.1f}")
        if bool(cfg.filter_unreachable):
            mark_weighted_path_failure(
                skip_ids,
                skip_points,
                path_failure_counts,
                target,
                reason=f"manual-recheck last_d={ld}",
                log=log,
            )
        return SuperLootStepResult(
            ok=False,
            action="path",
            message=f"手动寻路后仍偏远 last_d={ld}，已过滤",
            target=tdict,
            host_pos=list(last_host) if last_host else None,
            scene_id=scene_id,
            move=mv,
            matched=1,
            error="manual arrive recheck fail",
        )
    target.dist = float(last_d)
    dist = float(last_d)
    tdict["dist"] = float(last_d)
    log(f"super_loot manual path-open d={last_d:.2f} name={target.name!r}")
    res = _do_interact("path_open" if imode == MODE_OPEN else "path_pick")
    res.move = mv
    res.host_pos = list(last_host) if last_host else res.host_pos
    return res


class SuperLootRunner:
    """Background loop for continuous super-loot. @author by ak"""

    def __init__(
        self,
        *,
        pid: int,
        hwnd: int = 0,
        cfg: SuperLootConfig | None = None,
        on_step: Callable[[SuperLootStepResult], None] | None = None,
        log: LogFn | None = None,
    ):
        self.pid = int(pid)
        self.hwnd = int(hwnd or 0)
        self.cfg = cfg or SuperLootConfig()
        self.on_step = on_step or (lambda _r: None)
        self.log = log or (lambda _m: None)
        self._lifecycle = RunnerLifecycle(f"xajh-super-loot-{self.pid}")
        self._stop = self._lifecycle.stop_event
        self._thread: threading.Thread | None = None
        self._session: GameAttachSession | None = None
        self._skip_ids: dict[int, float] = {}
        # Stuck XZ points for near-radius filter (building-interior clusters)
        self._skip_points: list[tuple[float, float, float]] = []
        self._path_failure_counts: dict[int, int] = {}
        self._no_cast_attempts: dict[tuple[int, int], int] = {}
        self._no_cast_cooldowns: dict[tuple[int, int], float] = {}
        self._scan_cache: ScanCache | None = None
        self._hard_fail_streak = 0
        self._crt_cooldown_until = 0.0
        self._bridge_cooldown_until = 0.0
        self._bridge_fail_streak = 0
        self._bridge_reinject_n = 0
        self._reattach_fails = 0
        # Motion gate: last host sample for fly/teleport pause.
        self._motion_pos: tuple[float, float, float] | None = None
        self._motion_ts: float = 0.0
        self._motion_pause_until: float = 0.0
        self._motion_pause_logged: bool = False
        self.running = False

    def start(self) -> None:
        """Start background loop. @author by ak"""
        if self.running:
            return
        # Keep manual/runtime blacklist across start; only clear opened-style
        # short skips if needed later. Manual blacklist uses long TTL.
        thread = self._lifecycle.start(self._loop)
        if thread is not None:
            self._thread = thread
            self.running = True

    def stop(self) -> bool:
        """Request stop. @author by ak"""
        stopped = self._lifecycle.stop(wait=True)
        self.running = False
        return stopped

    def is_running(self) -> bool:
        return bool(self.running and self._lifecycle.is_running())

    def skip_ids(self) -> dict[int, float]:
        """Shared blacklist map (obj/coord key -> expire_ts). @author by ak"""
        return self._skip_ids

    def skip_points(self) -> list[tuple[float, float, float]]:
        """Shared stuck XZ points (x, z, expire_ts). @author by ak"""
        return self._skip_points

    def path_failure_counts(self) -> dict[int, int]:
        """Session-scoped consecutive path-failure weights by stable XZ."""
        return self._path_failure_counts

    def add_blacklist(
        self,
        target: SuperLootTarget | dict,
        *,
        ttl_s: float = 3600.0,
        reason: str = "manual",
    ) -> None:
        """
        Manually blacklist a match (UI right-click).

        @author by ak
        """
        if isinstance(target, dict):
            t = SuperLootTarget(
                name=str(target.get("name") or ""),
                ptr=int(target.get("ptr") or 0),
                obj_id=target.get("obj_id"),
                tid=target.get("tid"),
                dist=target.get("dist"),
                x=target.get("x"),
                y=target.get("y"),
                z=target.get("z"),
            )
        else:
            t = target
        # Manual blacklist: dual keys only. Near-wipe is off by default
        # (unreachable_near_m=0); do not write skip_points for dense fields.
        mark_unreachable(
            self._skip_ids,
            t,
            ttl_s=float(ttl_s),
            reason=reason,
            log=self.log,
            skip_points=None,
            record_near_point=False,
        )

    def clear_blacklist(self) -> None:
        """Clear all skip keys and stuck points. @author by ak"""
        self._skip_ids.clear()
        self._skip_points.clear()
        self._path_failure_counts.clear()
        self._no_cast_attempts.clear()
        self._no_cast_cooldowns.clear()
        self.log("super_loot blacklist cleared")

    def _idle_after_step(self, res: SuperLootStepResult) -> float:
        """
        Gap before next loop step.

        After successful open: cast_wait already finished inside the step —
        return near-zero idle so the next chest starts immediately.
        Full AOI scan / empty / CRT cooldown still throttle as needed.
        @author by ak
        """
        base = float(self.cfg.loop_idle_s)
        src = str(res.scan_source or "")
        used_cache = src.startswith("cache:") or src.startswith("cooldown_cache")
        # local_* = immediate open range; work_* = same pocket still guideable
        pocket_chain = used_cache and (
            "local_" in src or "work_" in src
        )
        dense = bool(res.dense) or (
            self._scan_cache is not None and bool(self._scan_cache.dense)
        )
        cooldown = src.startswith("cooldown_")

        if res.ok and res.action in ("open", "path_open", "pick", "path_pick"):
            # Cast bar already waited inside the step; chain next immediately.
            if pocket_chain or used_cache:
                return max(0.0, float(self.cfg.local_open_idle_s))
            # After full_scan open: still no second cast wait — tiny settle only.
            return max(base, float(self.cfg.local_open_idle_s))

        if cooldown or src.startswith("bridge_cooldown"):
            return max(base, float(self.cfg.bridge_cooldown_s), 5.0)

        if res.action == "error" and (
            self._is_attach_hard_error(str(res.error or res.message or ""))
            or is_bridge_error(str(res.error or res.message or ""))
        ):
            return max(
                base,
                float(self.cfg.crt_cooldown_s) * 0.4,
                float(self.cfg.bridge_cooldown_s),
                6.0,
            )

        if res.action == "none":
            if src.startswith("full_scan_batch:"):
                return max(base, float(self.cfg.dense_batch_idle_s))
            # Idle: next step should full-scan; short gap, not a long dense sleep.
            # (Opened chests despawn; fresh AOI replaces stale cache.)
            if cooldown:
                return max(base, float(self.cfg.dense_scan_interval_s))
            if dense:
                return max(base, min(float(self.cfg.dense_scan_interval_s), 2.5))
            return max(base, 1.2)

        if res.action in ("path", "error"):
            if dense:
                return max(base, float(self.cfg.dense_loop_idle_s))
            return max(base, 0.8)

        # Default / stop
        if dense and not used_cache:
            return max(base, float(self.cfg.dense_loop_idle_s))
        return base

    @staticmethod
    def _is_attach_hard_error(msg: str) -> bool:
        """Runner-facing alias of is_attach_hard_error. @author by ak"""
        return is_attach_hard_error(msg)

    @staticmethod
    def _process_alive(pid: int) -> bool:
        """True if target pid still exists. @author by ak"""
        try:
            import psutil

            return bool(psutil.pid_exists(int(pid)))
        except Exception:
            try:
                import ctypes

                k = ctypes.WinDLL("kernel32", use_last_error=True)
                h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED
                if not h:
                    return False
                k.CloseHandle(h)
                return True
            except Exception:
                return True  # unknown — allow retry

    def _close_session(self) -> None:
        """Close attach session best-effort. @author by ak"""
        if self._session is None:
            return
        try:
            self._session.close()
        except Exception:
            pass
        self._session = None

    def _reattach(self) -> bool:
        """
        Drop and reopen GameAttachSession after hard CRT failure.

        Stops hammering if process is dead (user restart required).
        @author by ak
        """
        self._close_session()
        if not self._process_alive(self.pid):
            self.log(
                f"super_loot reattach abort: pid={self.pid} 已退出，请重启游戏后重新破0/注入"
            )
            return False
        try:
            self._session = open_attach_session(self.pid, log=self.log)
            self.log(f"super_loot reattach pid={self.pid}")
            if bool(self.cfg.use_bridge):
                try:
                    br = ensure_bridge(
                        self.pid,
                        log=self.log,
                        inject_if_needed=True,
                        hwnd=self.hwnd or None,
                        force_reinject=False,
                    )
                    if br is not None:
                        try:
                            br.close()
                        except Exception:
                            pass
                except Exception as e:
                    self.log(f"super_loot reattach bridge: {e}")
            return True
        except Exception as e:
            self.log(f"super_loot reattach fail: {e}")
            self._session = None
            return False

    def _enter_crt_cooldown(self, reason: str = "") -> None:
        """
        Pause full CRT scans after VirtualAllocEx / OpenProcess collapse.

        @author by ak
        """
        cool = max(float(self.cfg.crt_cooldown_s), 5.0)
        self._crt_cooldown_until = time.time() + cool
        self.log(
            f"super_loot crt-cooldown {cool:.0f}s "
            f"{('reason=' + reason) if reason else ''}".rstrip()
        )

    def _enter_bridge_cooldown(self, reason: str = "") -> None:
        """
        Pause open attempts when shared-memory bridge is missing.

        @author by ak
        """
        cool = max(float(self.cfg.bridge_cooldown_s), 3.0)
        self._bridge_cooldown_until = time.time() + cool
        self.log(
            f"super_loot bridge-cooldown {cool:.0f}s "
            f"{('reason=' + reason) if reason else ''}".rstrip()
        )

    def _try_reinject_bridge(self) -> bool:
        """
        Best-effort reinject when OpenFileMapping fails.

        Limited attempts so we do not inject-storm a dying process.
        @author by ak
        """
        if self._bridge_reinject_n >= int(self.cfg.bridge_reinject_max):
            self.log(
                f"super_loot bridge reinject cap={self.cfg.bridge_reinject_max}"
            )
            return False
        if not self._process_alive(self.pid):
            return False
        self._bridge_reinject_n += 1
        self.log(
            f"super_loot bridge reinject try#{self._bridge_reinject_n} "
            f"pid={self.pid}"
        )
        try:
            br = ensure_bridge(
                self.pid,
                log=self.log,
                inject_if_needed=True,
                hwnd=self.hwnd or None,
                force_reinject=False,
            )
            if br is None:
                return False
            try:
                p = br.call(CMD_PING, hwnd=self.hwnd or None, timeout_ms=3000)
                ok = bool(p.ok)
                self.log(f"super_loot bridge reinject ping ok={ok}")
                return ok
            finally:
                try:
                    br.close()
                except Exception:
                    pass
        except Exception as e:
            self.log(f"super_loot bridge reinject fail: {e}")
            return False


    def _note_host_motion(
        self, host_pos: tuple[float, float, float] | list[float] | None
    ) -> None:
        """
        Update motion baseline from a known host position (no CRT).
        @author by ak
        """
        if host_pos is None:
            return
        try:
            p = (float(host_pos[0]), float(host_pos[1]), float(host_pos[2]))
        except Exception:
            return
        self._motion_pos = p
        self._motion_ts = time.time()

    def _sample_host_pos_for_motion(
        self,
    ) -> tuple[float, float, float] | None:
        """
        One scene-position sample for motion gate (GetCurrentScenePosition CRT).
        Avoids full matter scan / interact; used only when not already paused.
        @author by ak
        """
        if self._session is None:
            return None
        try:
            sp = read_scene_position(self._session, log=lambda _m: None)
            if sp.ok and sp.scene_pos:
                return (
                    float(sp.scene_pos[0]),
                    float(sp.scene_pos[1]),
                    float(sp.scene_pos[2]),
                )
        except Exception as e:
            self.log(f"super_loot motion sample: {e}")
        return None

    def _motion_gate_should_pause(self) -> bool:
        """
        True => skip this loop iteration (no open, no matter CRT).

        Triggers on horizontal jump / vertical spike / high speed vs last sample.
        While paused: pure sleep (no CRT). After hold: one sample; if still
        moving, extend pause.
        @author by ak
        """
        if not bool(getattr(self.cfg, "motion_pause", True)):
            return False
        now = time.time()
        pause_s = max(0.5, float(getattr(self.cfg, "motion_pause_s", 2.5)))
        # Already in pause window: no CRT, just hold.
        if now < float(self._motion_pause_until):
            return True

        cur = self._sample_host_pos_for_motion()
        if cur is None:
            return False

        hit, reason = evaluate_motion_gate(
            self._motion_pos,
            float(self._motion_ts or 0.0),
            cur,
            now,
            speed_mps=float(getattr(self.cfg, "motion_speed_mps", 18.0)),
            jump_m=float(getattr(self.cfg, "motion_jump_m", 26.0)),
            y_jump_m=float(getattr(self.cfg, "motion_y_jump_m", 8.0)),
        )
        self._motion_pos = cur
        self._motion_ts = now
        if hit:
            self._motion_pause_until = now + pause_s
            if not self._motion_pause_logged:
                self.log(
                    f"super_loot motion-pause {pause_s:.1f}s "
                    f"({reason}) — skip open/CRT until settle"
                )
                self._motion_pause_logged = True
            else:
                self.log(f"super_loot motion-pause extend {pause_s:.1f}s ({reason})")
            return True
        # Settled.
        if self._motion_pause_logged:
            self.log("super_loot motion-pause end — resume open")
            self._motion_pause_logged = False
        self._motion_pause_until = 0.0
        return False

    def _loop(self) -> None:
        try:
            if not self._process_alive(self.pid):
                self.log(f"super_loot start abort: pid={self.pid} 不存在")
                return
            try:
                self._session = open_attach_session(self.pid, log=self.log)
            except Exception as e:
                self.log(f"super_loot attach fail: {e}")
                return
            self.log(f"super_loot runner attach pid={self.pid}")
            # Open/move on UI thread require live bridge (minimized-safe).
            if bool(self.cfg.use_bridge):
                try:
                    br = ensure_bridge(
                        self.pid,
                        log=self.log,
                        inject_if_needed=True,
                        hwnd=self.hwnd or None,
                        force_reinject=False,
                    )
                    if br is None:
                        self.log(
                            "super_loot: 桥接未就绪，最小化开箱/寻路会失败，请先 Delete 注入"
                        )
                    else:
                        try:
                            p = br.call(
                                CMD_PING, hwnd=self.hwnd or None, timeout_ms=3000
                            )
                            self.log(
                                f"super_loot bridge ping ok={p.ok} note={p.note!r}"
                            )
                        finally:
                            try:
                                br.close()
                            except Exception:
                                pass
                except Exception as e:
                    self.log(f"super_loot bridge check: {e}")
            while not self._stop.is_set():
                if not self._process_alive(self.pid):
                    self.log(
                        f"super_loot stop: pid={self.pid} 已退出（游戏崩溃或关闭）"
                    )
                    break
                # Platform remote gate: hard_dead stop; hung cools CRT without storm.
                try:
                    from app.core.safe_dispatch import session_blocked

                    blocked, brsn = session_blocked(self.pid)
                except Exception:
                    blocked, brsn = False, ""
                if blocked:
                    if "hard_dead" in str(brsn):
                        self.log(
                            f"super_loot stop: 远程不可用 pid={self.pid} ({brsn})"
                        )
                        break
                    self._enter_crt_cooldown(str(brsn or "remote_blocked"))
                    end = time.time() + max(float(self.cfg.crt_cooldown_s), 5.0)
                    while time.time() < end and not self._stop.is_set():
                        time.sleep(0.2)
                    continue
                if self._session is None:
                    if self._reattach_fails >= int(self.cfg.reattach_max):
                        self.log(
                            f"super_loot stop: reattach 失败 {self._reattach_fails} 次，"
                            f"请重启游戏后重新破0/注入"
                        )
                        break
                    if not self._reattach():
                        self._reattach_fails += 1
                        self._enter_crt_cooldown("reattach_fail")
                        end = time.time() + max(
                            float(self.cfg.crt_cooldown_s), 10.0
                        )
                        while time.time() < end and not self._stop.is_set():
                            time.sleep(0.2)
                        continue
                    self._reattach_fails = 0
                # High-speed / air / teleport: pause open + matter CRT.
                if self._motion_gate_should_pause():
                    poll = max(0.1, float(getattr(self.cfg, "motion_poll_s", 0.35)))
                    end = time.time() + poll
                    while time.time() < end and not self._stop.is_set():
                        time.sleep(0.05)
                    continue
                allow_re = (
                    self._bridge_fail_streak > 0
                    and self._bridge_reinject_n < int(self.cfg.bridge_reinject_max)
                    and time.time() >= float(self._bridge_cooldown_until)
                )
                try:
                    res = super_loot_step(
                        self._session,
                        self.cfg,
                        hwnd=self.hwnd,
                        stop_event=self._stop,
                        skip_ids=self._skip_ids,
                        skip_points=self._skip_points,
                        path_failure_counts=self._path_failure_counts,
                        no_cast_attempts=self._no_cast_attempts,
                        no_cast_cooldowns=self._no_cast_cooldowns,
                        scan_cache=self._scan_cache,
                        crt_cooldown_until=self._crt_cooldown_until,
                        bridge_cooldown_until=self._bridge_cooldown_until,
                        allow_bridge_reinject=allow_re,
                        log=self.log,
                    )
                except Exception as e:
                    res = SuperLootStepResult(
                        ok=False,
                        action="error",
                        message=str(e),
                        error=str(e),
                    )
                if res.scan_cache is not None:
                    self._scan_cache = res.scan_cache
                # Baseline for next motion gate (prefer step result; no extra CRT).
                if res.host_pos is not None and not self._motion_pause_logged:
                    self._note_host_motion(res.host_pos)

                err_blob = str(res.error or res.message or "")
                ix = res.interact or {}
                bridge_dead = bool(ix.get("bridge_dead")) or is_bridge_error(
                    err_blob
                )
                hard = False
                if res.action == "error" and self._is_attach_hard_error(err_blob):
                    hard = True
                elif res.action == "none" and self._is_attach_hard_error(err_blob):
                    hard = True
                elif str(res.scan_source or "").startswith("cache:") and (
                    "hard_fail" in str(res.scan_source)
                ):
                    hard = True

                if bridge_dead and not res.ok:
                    self._bridge_fail_streak += 1
                    self.log(
                        f"super_loot bridge-dead streak={self._bridge_fail_streak} "
                        f"msg={res.message!r}"
                    )
                    # First failures: try reinject then cool down open spam.
                    if self._bridge_fail_streak == 1:
                        if self._try_reinject_bridge():
                            self._bridge_fail_streak = 0
                            self._bridge_cooldown_until = 0.0
                        else:
                            self._enter_bridge_cooldown("openfilemapping")
                    else:
                        self._enter_bridge_cooldown("bridge_dead")
                    idle = max(
                        float(self.cfg.loop_idle_s),
                        float(self.cfg.bridge_cooldown_s),
                        5.0,
                    )
                elif hard:
                    self._hard_fail_streak += 1
                    self._enter_crt_cooldown(str(res.message or res.action)[:80])
                    # Also stop open spam if CRT is dead (bridge often dies with it).
                    self._enter_bridge_cooldown("crt_hard")
                    self.log(
                        f"super_loot hard-fail streak={self._hard_fail_streak} "
                        f"msg={res.message!r} src={res.scan_source!r}"
                    )
                    no_cache = not (
                        self._scan_cache and self._scan_cache.hits
                    )
                    if (
                        no_cache
                        and self._hard_fail_streak
                        >= int(DEFAULT_SCAN_FAIL_REATTACH_N)
                        and self._process_alive(self.pid)
                    ):
                        if self._reattach():
                            self._reattach_fails = 0
                            self._hard_fail_streak = 0
                        else:
                            self._reattach_fails += 1
                    idle = max(
                        float(self.cfg.loop_idle_s),
                        float(self.cfg.crt_cooldown_s) * 0.5,
                        8.0,
                    )
                else:
                    if res.ok:
                        self._hard_fail_streak = 0
                        self._bridge_fail_streak = 0
                        self._reattach_fails = 0
                    elif res.action not in ("error",):
                        self._hard_fail_streak = 0
                    idle = self._idle_after_step(res)
                try:
                    self.on_step(res)
                except Exception:
                    pass
                end = time.time() + idle
                while time.time() < end:
                    if self._stop.is_set():
                        break
                    time.sleep(0.1)
        finally:
            self.running = False
            self._close_session()
            self.log("super_loot runner stopped")


# Backward-compatible alias (chests must use open_target)
pick_target = open_target
