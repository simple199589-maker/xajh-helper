# -*- coding: utf-8 -*-
"""
Welfare chest (福利宝箱) density scan + business path plan.

Business rule (local_range default 10):
  - d <= local_range: no action (already near chests; skip).
  - d >  local_range: pick densest cluster, path to its centroid via HostMove.

Scan logic:
  1. List live matters via plg AOI (class=1).
  2. Filter 福利/宝箱 (name keywords + known tid 101044).
  3. Prefer seed with most adjacent chests (cluster_radius), then nearer host.
  4. Far densest cluster -> target = cluster centroid (center point).

@author by ak
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable

from app.core.automove import PathTarget, host_move_to, read_scene_position
from app.core.plg_objects import (
    CLASS_MATTER,
    CLASS_PLAYER,
    PlgObject,
    list_class_objects,
)

LogFn = Callable[[str], None]

# Live-verified template for 福利活动宝箱
CHEST_TID_WELFARE = 101044
CHEST_NAME_KEYS = ("宝箱", "福利", "礼盒")

DEFAULT_LOCAL_RANGE = 10.0
DEFAULT_CLUSTER_RADIUS = 20.0
DEFAULT_PLAYER_RADIUS = 18.0
# Cap CRT work on dense maps (GetObjects itself maxes at 512 ptrs).
DEFAULT_MATTER_LIMIT = 96
# Neighbor count (incl. self) at/below this => isolated
DEFAULT_ISOLATED_MAX_ADJ = 1

# plan.action
ACTION_SKIP_LOCAL = "skip_local"  # within range: do nothing
ACTION_AUTO_PATH_CENTER = "auto_path_center"  # beyond range: HostMove to densest center
ACTION_NONE = "none"

# legacy aliases (older UI/logs)
ACTION_AUTO_PATH_PICKUP = ACTION_SKIP_LOCAL
ACTION_MANUAL_WALK = ACTION_AUTO_PATH_CENTER


@dataclass
class ChestInfo:
    """One live chest matter. @author by ak"""

    name: str
    address: int
    tid: int | None
    x: float
    y: float
    z: float
    dist: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChestRegionPlan:
    """
    Result of local-or-densest chest scan.

    mode:
      - local: chests already within local_range (skip, no move)
      - densest: no local chests; densest remote cluster + center path
      - none: no chests found in AOI
    action:
      - skip_local | auto_path_center | none
    @author by ak
    """

    ok: bool
    mode: str  # local | densest | none
    local_range: float
    cluster_radius: float
    host_pos: tuple[float, float, float] | None = None
    scene_id: int | None = None
    chest_total: int = 0
    local_count: int = 0
    cluster_count: int = 0
    target: PathTarget | None = None
    cluster: list[ChestInfo] = field(default_factory=list)
    all_chests: list[ChestInfo] = field(default_factory=list)
    action: str = ACTION_NONE
    can_auto_move: bool = False
    can_quick_pickup: bool = False
    center: tuple[float, float, float] | None = None
    note: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.target is not None:
            d["target"] = self.target.to_dict()
        if self.host_pos is not None:
            d["host_pos"] = list(self.host_pos)
        if self.center is not None:
            d["center"] = list(self.center)
        return d


def is_welfare_chest(name: str | None, tid: int | None = None) -> bool:
    """
    True if matter looks like a welfare / loot chest.

    @author by ak
    """
    n = name or ""
    if any(k in n for k in CHEST_NAME_KEYS):
        return True
    if tid is not None and int(tid) == CHEST_TID_WELFARE:
        return True
    return False


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


def _chest_from_obj(
    o: PlgObject,
    host_pos: tuple[float, float, float] | None,
) -> ChestInfo | None:
    if o.x is None or o.y is None or o.z is None:
        return None
    dist = o.dist
    if dist is None and host_pos is not None:
        dist = _dist3(host_pos, (float(o.x), float(o.y), float(o.z)))
    return ChestInfo(
        name=o.name or (f"tid:{o.tid}" if o.tid is not None else f"obj:{o.ptr:X}"),
        address=int(o.ptr),
        tid=int(o.tid) if o.tid is not None else None,
        x=float(o.x),
        y=float(o.y),
        z=float(o.z),
        dist=float(dist) if dist is not None else None,
    )


def list_chests(
    session,
    *,
    host_pos: tuple[float, float, float] | None = None,
    limit: int = DEFAULT_MATTER_LIMIT,
    log: LogFn | None = None,
) -> list[ChestInfo]:
    """
    Enumerate live welfare chests from matter AOI.

    Prefer tid=101044 CRT filter (skip GetObjectName on dense maps).

    @author by ak
    """
    log = log or (lambda _m: None)
    lim = max(8, min(int(limit), 160))
    objs = list_class_objects(
        session,
        CLASS_MATTER,
        host_pos=host_pos,
        radius=None,
        limit=lim,
        log=log,
        want_tid=CHEST_TID_WELFARE,
        read_name=False,
        read_tid=True,
        read_dist_api=False,
        max_inspect=min(512, max(lim * 2, 96)),
        max_crt_failures=6,
    )
    out: list[ChestInfo] = []
    for o in objs:
        if not is_welfare_chest(o.name, o.tid):
            continue
        c = _chest_from_obj(o, host_pos)
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: c.dist if c.dist is not None else 1e9)
    log(f"chest_pathfind listed chests={len(out)} matter={len(objs)}")
    return out


def _neighbor_count(
    seed: ChestInfo,
    items: list[ChestInfo],
    cluster_radius: float,
) -> int:
    """Count chests within horizontal cluster_radius of seed (incl. self). @author by ak"""
    r = float(cluster_radius)
    spos = (seed.x, seed.y, seed.z)
    return sum(
        1
        for c in items
        if _dist3(spos, (c.x, c.y, c.z), horizontal=True) <= r
    )


def _host_dist(
    c: ChestInfo,
    host_pos: tuple[float, float, float] | None,
) -> float:
    """Host distance for ranking; large if unknown. @author by ak"""
    if c.dist is not None:
        return float(c.dist)
    if host_pos is not None:
        return _dist3(host_pos, (c.x, c.y, c.z))
    return 1e18


def list_player_positions(
    session,
    *,
    host_pos: tuple[float, float, float] | None = None,
    limit: int = 128,
    log: LogFn | None = None,
) -> list[tuple[float, float, float]]:
    """
    Live player AOI positions (class=0), excluding invalid coords.

    Used only for density path scoring (prefer fewer people near cluster).
    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        objs = list_class_objects(
            session,
            CLASS_PLAYER,
            host_pos=host_pos,
            radius=None,
            limit=int(limit),
            read_tid=False,  # 玩家无NPC模板ID，RPM读tid会返回None被判为stale跳过
            log=log,
        )
    except Exception as e:
        log(f"chest_pathfind list players skip: {e}")
        return []
    out: list[tuple[float, float, float]] = []
    for o in objs:
        if o.x is None or o.z is None:
            continue
        out.append((float(o.x), float(o.y or 0.0), float(o.z)))
    log(f"chest_pathfind players={len(out)}")
    return out


def count_players_near(
    pos: tuple[float, float, float],
    players: Iterable[tuple[float, float, float]],
    *,
    radius: float = DEFAULT_PLAYER_RADIUS,
) -> int:
    """Count players within horizontal radius of pos. @author by ak"""
    r = float(radius)
    return sum(
        1
        for p in players
        if _dist3(pos, (float(p[0]), float(p[1]), float(p[2])), horizontal=True) <= r
    )


def pick_target_by_neighbor_density(
    chests: Iterable[ChestInfo],
    *,
    cluster_radius: float = DEFAULT_CLUSTER_RADIUS,
    host_pos: tuple[float, float, float] | None = None,
    candidates: Iterable[ChestInfo] | None = None,
    players: Iterable[tuple[float, float, float]] | None = None,
    player_radius: float = DEFAULT_PLAYER_RADIUS,
) -> tuple[ChestInfo | None, list[ChestInfo], int]:
    """
    Prefer densest chest area, then fewer nearby players, then nearer host.

    Primary: neighbor count within cluster_radius (incl. self).
    Secondary: fewer players within player_radius of seed.
    Tertiary: nearer to host.
    candidates: if set, only these may be chosen as the path target seed
    (still count neighbors against full chests list).

    Returns (seed, members_of_seed_cluster, neighbor_count).

    @author by ak
    """
    items = [c for c in chests if c.x is not None]
    if not items:
        return None, [], 0
    pool = [c for c in (candidates if candidates is not None else items) if c.x is not None]
    if not pool:
        return None, [], 0

    r = float(cluster_radius)
    plist = list(players) if players is not None else []
    best: ChestInfo | None = None
    best_n = -1
    best_players = 1 << 30
    best_d = 1e18
    for seed in pool:
        n = _neighbor_count(seed, items, r)
        d = _host_dist(seed, host_pos)
        pc = (
            count_players_near((seed.x, seed.y, seed.z), plist, radius=player_radius)
            if plist
            else 0
        )
        better = False
        if n > best_n:
            better = True
        elif n == best_n and pc < best_players:
            better = True
        elif n == best_n and pc == best_players and d < best_d:
            better = True
        if better:
            best = seed
            best_n = n
            best_players = pc
            best_d = d

    if best is None:
        return None, [], 0

    spos = (best.x, best.y, best.z)
    members = [
        c
        for c in items
        if _dist3(spos, (c.x, c.y, c.z), horizontal=True) <= r
    ]
    # show: seed first, then others by host distance
    others = [c for c in members if c.address != best.address]
    others.sort(key=lambda c: _host_dist(c, host_pos))
    ordered = [best] + others
    return best, ordered, best_n


def is_isolated_chest(
    seed: ChestInfo,
    chests: Iterable[ChestInfo],
    *,
    cluster_radius: float = DEFAULT_CLUSTER_RADIUS,
    isolated_max_adj: int = DEFAULT_ISOLATED_MAX_ADJ,
) -> bool:
    """True if seed has few/no neighboring chests (isolated). @author by ak"""
    n = _neighbor_count(seed, list(chests), float(cluster_radius))
    return n <= int(isolated_max_adj)


def find_densest_cluster(
    chests: Iterable[ChestInfo],
    *,
    cluster_radius: float = DEFAULT_CLUSTER_RADIUS,
    host_pos: tuple[float, float, float] | None = None,
) -> list[ChestInfo]:
    """
    Pick densest chest cluster; seed is the chest with most adjacent chests.

    Primary: neighbor count within cluster_radius.
    Tie-break: seed nearer to host.
    Members ordered: seed first, then nearer host.

    @author by ak
    """
    _seed, members, _n = pick_target_by_neighbor_density(
        chests,
        cluster_radius=cluster_radius,
        host_pos=host_pos,
    )
    return members


def _cluster_center(
    members: list[ChestInfo],
) -> tuple[float, float, float] | None:
    """
    Centroid of cluster members (mean x,y,z).

    @author by ak
    """
    pts = [c for c in members if c.x is not None]
    if not pts:
        return None
    n = float(len(pts))
    return (
        sum(c.x for c in pts) / n,
        sum(c.y for c in pts) / n,
        sum(c.z for c in pts) / n,
    )


def plan_chest_path(
    session,
    *,
    local_range: float = DEFAULT_LOCAL_RANGE,
    cluster_radius: float = DEFAULT_CLUSTER_RADIUS,
    host_pos: tuple[float, float, float] | None = None,
    scene_id: int | None = None,
    move_mode: int | None = None,
    limit: int = DEFAULT_MATTER_LIMIT,
    log: LogFn | None = None,
) -> ChestRegionPlan:
    """
    Plan chest business by local_range.

    - local (any chest d<=local_range): action=skip_local, no target move
    - densest far: action=auto_path_center, target = densest cluster center
    - none: no chests

    Seed density still uses max adjacent count, then nearer host.

    @author by ak
    """
    log = log or (lambda _m: None)
    lr = float(local_range)
    cr = float(cluster_radius)

    # resolve host / scene if missing
    sid = scene_id
    hpos = host_pos
    if hpos is None or sid is None:
        sp = read_scene_position(session, log=log)
        if sp.ok and sp.scene_pos:
            if hpos is None:
                hpos = sp.scene_pos
            if sid is None and sp.scene_id is not None:
                sid = int(sp.scene_id)
        if not sp.ok:
            log(f"chest_pathfind scene pos warn: {sp.error}")

    try:
        chests = list_chests(session, host_pos=hpos, limit=limit, log=log)
    except Exception as e:
        return ChestRegionPlan(
            ok=False,
            mode="none",
            local_range=lr,
            cluster_radius=cr,
            host_pos=hpos,
            scene_id=sid,
            action=ACTION_NONE,
            error=str(e),
            note="list_chests failed",
        )

    if not chests:
        return ChestRegionPlan(
            ok=False,
            mode="none",
            local_range=lr,
            cluster_radius=cr,
            host_pos=hpos,
            scene_id=sid,
            chest_total=0,
            action=ACTION_NONE,
            note="AOI 内无福利/宝箱",
        )

    # refresh dist from current host when possible
    if hpos is not None:
        for c in chests:
            c.dist = _dist3(hpos, (c.x, c.y, c.z))
        chests.sort(key=lambda c: c.dist if c.dist is not None else 1e9)

    local = [c for c in chests if c.dist is not None and c.dist <= lr]
    mode_arg = int(move_mode if move_mode is not None else (sid or 0))

    if local:
        # already near chests -> skip all auto handling
        nearest = local[0]
        dd = f"{nearest.dist:.1f}" if nearest.dist is not None else "n/a"
        note = (
            f"范围 {lr:g} 内已有宝箱 {len(local)} 个（最近 d={dd} {nearest.name}）；"
            f"不做处理"
        )
        log(f"chest_pathfind mode=local action=skip_local {note}")
        return ChestRegionPlan(
            ok=True,
            mode="local",
            local_range=lr,
            cluster_radius=cr,
            host_pos=hpos,
            scene_id=sid,
            chest_total=len(chests),
            local_count=len(local),
            cluster_count=len(local),
            target=None,
            cluster=local,
            all_chests=chests,
            action=ACTION_SKIP_LOCAL,
            can_auto_move=False,
            can_quick_pickup=False,
            center=None,
            note=note,
        )

    players = list_player_positions(session, host_pos=hpos, log=log)
    seed, cluster, adj_n = pick_target_by_neighbor_density(
        chests,
        cluster_radius=cr,
        host_pos=hpos,
        players=players,
        player_radius=DEFAULT_PLAYER_RADIUS,
    )
    if seed is None or not cluster:
        return ChestRegionPlan(
            ok=False,
            mode="none",
            local_range=lr,
            cluster_radius=cr,
            host_pos=hpos,
            scene_id=sid,
            chest_total=len(chests),
            action=ACTION_NONE,
            note="有宝箱但无法构图集群",
            all_chests=chests,
        )

    center = _cluster_center(cluster)
    if center is None:
        return ChestRegionPlan(
            ok=False,
            mode="none",
            local_range=lr,
            cluster_radius=cr,
            host_pos=hpos,
            scene_id=sid,
            chest_total=len(chests),
            action=ACTION_NONE,
            note="密集区无有效坐标",
            all_chests=chests,
            cluster=cluster,
        )

    cx, cy, cz = center
    cdist = _dist3(hpos, center) if hpos is not None else None
    pnear = count_players_near(center, players, radius=DEFAULT_PLAYER_RADIUS) if players else 0
    tgt = PathTarget(
        x=cx,
        y=cy,
        z=cz,
        mode=mode_arg,
        map_hint=f"densest_center adj={adj_n} n={len(cluster)} p={pnear}",
    )
    dd = f"{cdist:.1f}" if cdist is not None else "n/a"
    note = (
        f"范围 {lr:g} 外；密集区 adj={adj_n} r={cr:g} players~{pnear} "
        f"中心=({cx:.1f},{cy:.1f},{cz:.1f}) d={dd} → 自动寻路中心点"
    )
    log(f"chest_pathfind mode=densest action=auto_path_center {note}")
    return ChestRegionPlan(
        ok=True,
        mode="densest",
        local_range=lr,
        cluster_radius=cr,
        host_pos=hpos,
        scene_id=sid,
        chest_total=len(chests),
        local_count=0,
        cluster_count=adj_n,
        target=tgt,
        cluster=cluster,
        all_chests=chests,
        action=ACTION_AUTO_PATH_CENTER,
        can_auto_move=True,
        can_quick_pickup=False,
        center=center,
        note=note,
    )


def pathfind_to_densest_chest(
    session,
    *,
    local_range: float = DEFAULT_LOCAL_RANGE,
    cluster_radius: float = DEFAULT_CLUSTER_RADIUS,
    force_when_local: bool = False,
    force_when_far: bool = False,
    host_pos: tuple[float, float, float] | None = None,
    log: LogFn | None = None,
) -> tuple[ChestRegionPlan, dict | None]:
    """
    Plan then HostMove only for densest center when beyond local_range.

    Default business:
      - local (skip_local): no HostMove (unless force_when_local)
      - densest (auto_path_center): HostMove to cluster center
      - force_when_far kept as alias (densest always moves by default)

    Returns (plan, automove_result_dict_or_None).

    @author by ak
    """
    log = log or (lambda _m: None)
    plan = plan_chest_path(
        session,
        local_range=local_range,
        cluster_radius=cluster_radius,
        host_pos=host_pos,
        log=log,
    )
    if not plan.ok:
        return plan, None

    if plan.action == ACTION_SKIP_LOCAL or plan.mode == "local":
        if force_when_local and plan.target is not None:
            log("chest_pathfind forced HostMove: local skip overridden")
            r = host_move_to(session, plan.target, log=log)
            return plan, r.to_dict()
        log("chest_pathfind skip: local chests present, no move")
        return plan, None

    if plan.action == ACTION_AUTO_PATH_CENTER or plan.mode == "densest":
        if plan.target is None:
            return plan, None
        # densest always auto-moves; force_when_far unused but accepted
        _ = force_when_far
        log("chest_pathfind auto HostMove: densest cluster center")
        r = host_move_to(session, plan.target, log=log)
        return plan, r.to_dict()

    return plan, None


def run_chest_business(
    session,
    *,
    local_range: float = DEFAULT_LOCAL_RANGE,
    cluster_radius: float = DEFAULT_CLUSTER_RADIUS,
    host_pos: tuple[float, float, float] | None = None,
    auto_move_local: bool = False,
    force_far_move: bool = True,
    log: LogFn | None = None,
) -> tuple[ChestRegionPlan, dict | None]:
    """
    Single entry for chest business step.

    - Within local_range: skip (no move).
    - Beyond local_range: densest center + HostMove (force_far_move default True).

    auto_move_local maps to force_when_local for rare override.

    @author by ak
    """
    log = log or (lambda _m: None)
    return pathfind_to_densest_chest(
        session,
        local_range=local_range,
        cluster_radius=cluster_radius,
        force_when_local=bool(auto_move_local),
        force_when_far=bool(force_far_move),
        host_pos=host_pos,
        log=log,
    )
