# -*- coding: utf-8 -*-
"""
State precheck / prefetch dispatch.

Business rule (product):
- Many preconditions (map/pos/bag/warehouse/money/host…) can be prefetched and
  shared via SafeDispatch cache + scene hub.
- If a step *actively needs* fresh data, call with fresh=True / max_age=0 so
  producer runs now and cache is updated.

SafeDispatch remains orchestration only (queue/gate/cache/single-flight).
This module classifies *what* may be warm vs must-live.

@author by ak
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

from app.core.safe_dispatch import (
    CACHE_BAG,
    CACHE_SCENE,
    OpKind,
    Priority,
    TTL_BAG_S,
    TTL_SCENE_S,
    bag_cache_key,
    get_dispatch,
)


LogFn = Callable[[str], None]


class StateKind(str, Enum):
    """Prefetchable / shareable session state keys. @author by ak"""

    SCENE = "scene"          # scene_id + label + optional pos (hub/RPM)
    POS = "pos"              # host xyz
    DEAD = "dead"            # host dead flag (hub / hang SM)
    VITALITY = "vitality"    # vitality pct
    EQUIP_DURA = "equip_dura"  # equipped durability mean/min
    BAG = "bag"              # main+ext packages
    WAREHOUSE = "warehouse"  # warehouse package only
    MONEY = "money"          # gold snapshot
    HOST_ID = "host_id"      # host obj id (no GetObjectName)
    IN_TEAM = "in_team"      # host in party
    PARTY_MEMBERS = "party_members"  # CECTeam member list (name/id)
    ROLL = "roll"            # loot roll pending (RPM)


@dataclass(frozen=True)
class StatePolicy:
    """Default freshness policy for one state kind. @author by ak"""

    kind: StateKind
    # soft TTL for warm cache (seconds)
    ttl_s: float
    # default max_age when business does not ask fresh
    max_age_s: float
    # True: may be prefetched in background; False: only on demand
    prefetchable: bool
    priority: Priority = Priority.P2
    kind_op: OpKind = OpKind.RPM
    cache_prefix: str = ""


# Policy table — cadence/ownership still with business; these are defaults only.
STATE_POLICIES: dict[StateKind, StatePolicy] = {
    StateKind.SCENE: StatePolicy(
        StateKind.SCENE, ttl_s=TTL_SCENE_S, max_age_s=TTL_SCENE_S, prefetchable=True,
        priority=Priority.P3, kind_op=OpKind.RPM, cache_prefix=CACHE_SCENE,
    ),
    StateKind.POS: StatePolicy(
        StateKind.POS, ttl_s=0.5, max_age_s=0.5, prefetchable=True,
        priority=Priority.P2, kind_op=OpKind.RPM, cache_prefix="pos",
    ),
    StateKind.DEAD: StatePolicy(
        StateKind.DEAD, ttl_s=3.0, max_age_s=3.0, prefetchable=True,
        priority=Priority.P3, kind_op=OpKind.CRT_READ, cache_prefix="dead",
    ),
    StateKind.VITALITY: StatePolicy(
        StateKind.VITALITY, ttl_s=2.5, max_age_s=2.5, prefetchable=True,
        priority=Priority.P3, kind_op=OpKind.RPM, cache_prefix="vitality",
    ),
    StateKind.EQUIP_DURA: StatePolicy(
        StateKind.EQUIP_DURA, ttl_s=12.0, max_age_s=12.0, prefetchable=True,
        priority=Priority.P3, kind_op=OpKind.RPM, cache_prefix="equip_dura",
    ),
    StateKind.BAG: StatePolicy(
        StateKind.BAG, ttl_s=TTL_BAG_S, max_age_s=TTL_BAG_S, prefetchable=True,
        priority=Priority.P2, kind_op=OpKind.RPM, cache_prefix=CACHE_BAG,
    ),
    StateKind.WAREHOUSE: StatePolicy(
        StateKind.WAREHOUSE, ttl_s=0.5, max_age_s=0.5, prefetchable=True,
        priority=Priority.P3, kind_op=OpKind.RPM, cache_prefix="warehouse",
    ),
    StateKind.MONEY: StatePolicy(
        StateKind.MONEY, ttl_s=0.4, max_age_s=0.4, prefetchable=True,
        priority=Priority.P2, kind_op=OpKind.RPM, cache_prefix="money",
    ),
    StateKind.HOST_ID: StatePolicy(
        StateKind.HOST_ID, ttl_s=5.0, max_age_s=5.0, prefetchable=True,
        priority=Priority.P3, kind_op=OpKind.CRT_READ, cache_prefix="host_id",
    ),
    StateKind.IN_TEAM: StatePolicy(
        StateKind.IN_TEAM, ttl_s=8.0, max_age_s=8.0, prefetchable=True,
        priority=Priority.P3, kind_op=OpKind.CRT_READ, cache_prefix="in_team",
    ),
    # GetHostPlayerTeam is CRT; member array is RPM. Short TTL so UI refresh stays honest.
    StateKind.PARTY_MEMBERS: StatePolicy(
        StateKind.PARTY_MEMBERS, ttl_s=1.5, max_age_s=1.5, prefetchable=True,
        priority=Priority.P2, kind_op=OpKind.CRT_READ, cache_prefix="party_members",
    ),
    StateKind.ROLL: StatePolicy(
        StateKind.ROLL, ttl_s=0.5, max_age_s=0.5, prefetchable=True,
        priority=Priority.P2, kind_op=OpKind.RPM, cache_prefix="loot_roll",
    ),
}


def _pid_of(session_or_pid) -> int:
    if isinstance(session_or_pid, int):
        return int(session_or_pid)
    return int(getattr(session_or_pid, "pid", 0) or 0)


def _as_kind(kind: StateKind | str) -> StateKind:
    if isinstance(kind, StateKind):
        return kind
    return StateKind(str(kind))


def policy_of(kind: StateKind | str) -> StatePolicy:
    return STATE_POLICIES[_as_kind(kind)]


def cache_key_for(kind: StateKind | str, *, extra: str = "") -> str:
    k = _as_kind(kind)
    pol = policy_of(k)
    if k is StateKind.BAG:
        return bag_cache_key() if not extra else bag_cache_key(
            [int(x) for x in str(extra).split(",") if str(x).strip()]
        )
    base = pol.cache_prefix or str(kind)
    return f"{base}:{extra}" if extra else base


def _produce(session, kind: StateKind, log: LogFn) -> Any:
    """Lazy producers (import inside to avoid cycles). @author by ak"""
    if kind is StateKind.SCENE:
        from app.core.live_scene_hub import (
            DEFAULT_LIVE_MAX_AGE_S,
            get_live_scene,
            publish_live_scene,
        )

        pid = int(getattr(session, "pid", 0) or 0)
        # Only trust a *young* hub sample. Older samples used to be recycled forever
        # because the header producer also went through this path (stale loop).
        hub_age = max(float(TTL_SCENE_S), float(DEFAULT_LIVE_MAX_AGE_S))
        snap = get_live_scene(pid, max_age_s=hub_age) if pid else None
        if snap is not None and (
            snap.scene_id is not None or snap.scene_label or snap.pos is not None
        ):
            return {
                "scene_id": snap.scene_id,
                "scene_label": snap.scene_label,
                "pos": snap.pos,
                "role_name": snap.role_name,
                "dead": getattr(snap, "dead", None),
                "source": f"hub:{snap.source}",
                "age_s": snap.age_s(),
            }
        # hub empty: use plg scene-pos export (fast). NEVER recon_map_and_pos —
        # that path scan_map_strings full process memory and can hang minutes.
        try:
            from app.core.automove import read_scene_position

            sp = read_scene_position(session, log=log)
            if getattr(sp, "ok", False):
                pos = getattr(sp, "scene_pos", None)
                pos_t = None
                if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                    pos_t = (float(pos[0]), float(pos[1]), float(pos[2]))
                scene_id = getattr(sp, "scene_id", None)
                scene_label = ""
                try:
                    if scene_id is not None:
                        from app.core.map_names import format_scene_display

                        scene_label = format_scene_display(int(scene_id)) or ""
                except Exception:
                    scene_label = ""
                if pid and (scene_id is not None or pos_t is not None):
                    try:
                        publish_live_scene(
                            pid,
                            scene_id=scene_id,
                            scene_label=scene_label or None,
                            pos=pos_t,
                            source="state_dispatch",
                        )
                    except Exception:
                        pass
                return {
                    "scene_id": scene_id,
                    "scene_label": scene_label,
                    "pos": pos_t,
                    "role_name": "",
                    "source": "scene_pos",
                    "age_s": 0.0,
                }
        except Exception as e:
            log(f"state SCENE scene_pos err: {e}")
        return {
            "scene_id": None,
            "scene_label": "",
            "pos": None,
            "role_name": "",
            "source": "empty",
            "age_s": 1e9,
        }

    if kind is StateKind.POS:
        sc = _produce(session, StateKind.SCENE, log)
        pos = sc.get("pos") if isinstance(sc, dict) else None
        if pos is not None:
            return pos
        # last resort: same fast export path only (no memory scan recon)
        try:
            from app.core.automove import read_scene_position

            sp = read_scene_position(session, log=log)
            pos = getattr(sp, "scene_pos", None) if getattr(sp, "ok", False) else None
            if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                return (float(pos[0]), float(pos[1]), float(pos[2]))
            return pos
        except Exception as e:
            log(f"state POS err: {e}")
        return None

    if kind is StateKind.DEAD:
        from app.core.live_scene_hub import get_live_scene

        snap = get_live_scene(int(session.pid), max_age_s=None)
        if snap is not None and snap.dead is not None and snap.dead_age_s() < 8.0:
            return bool(snap.dead)
        try:
            from app.core.hang_settings import refresh_hang_dead_state

            return refresh_hang_dead_state(session, log=log, force=False)
        except Exception as e:
            log(f"state DEAD err: {e}")
            return None

    if kind is StateKind.VITALITY:
        from app.core.live_scene_hub import get_live_scene

        snap = get_live_scene(int(session.pid), max_age_s=None)
        if (
            snap is not None
            and snap.vitality_pct is not None
            and snap.field_age_s("vitality_updated_at") < 3.0
        ):
            return {
                "ready": True,
                "pct": float(snap.vitality_pct),
                "source": f"hub:{snap.source}",
            }
        try:
            from app.core.hang_settings import read_vitality_pct

            return read_vitality_pct(session, log=log)
        except Exception as e:
            log(f"state VITALITY err: {e}")
            return {"ready": False, "pct": None, "error": str(e)}

    if kind is StateKind.EQUIP_DURA:
        from app.core.live_scene_hub import get_live_scene

        snap = get_live_scene(int(session.pid), max_age_s=None)
        if (
            snap is not None
            and snap.equip_dura_pct is not None
            and snap.field_age_s("equip_updated_at") < 15.0
        ):
            return {
                "ready": True,
                "pct": float(snap.equip_dura_pct),
                "min_pct": snap.equip_dura_min_pct,
                "source": f"hub:{snap.source}",
            }
        try:
            from app.core.hang_settings import read_equipment_durability_pct

            return read_equipment_durability_pct(session, log=log)
        except Exception as e:
            log(f"state EQUIP_DURA err: {e}")
            return {"ready": False, "pct": None, "error": str(e)}

    if kind is StateKind.IN_TEAM:
        from app.core.live_scene_hub import get_live_scene

        snap = get_live_scene(int(session.pid), max_age_s=None)
        if snap is not None and snap.in_team is not None and snap.age_s() < 10.0:
            return bool(snap.in_team)
        try:
            from app.core.plg_ui import is_host_in_team

            return bool(is_host_in_team(session, log=log))
        except Exception as e:
            log(f"state IN_TEAM err: {e}")
            return None

    if kind is StateKind.PARTY_MEMBERS:
        try:
            from app.core.team_ops import read_cecteam_members

            return list(read_cecteam_members(session, log=log) or [])
        except Exception as e:
            log(f"state PARTY_MEMBERS err: {e}")
            return []

    if kind is StateKind.BAG:

        from app.core.package_api import (
            DEFAULT_CARRY_PACKAGE_INDEXES,
            list_packages_items,
        )

        # producer itself bypasses outer cache (max_age=0) when called via require_fresh
        return list_packages_items(
            session,
            list(DEFAULT_CARRY_PACKAGE_INDEXES),
            log=log,
            use_cache=False,
        )

    if kind is StateKind.WAREHOUSE:
        from app.core.package_api import PACKAGE_INDEX_WAREHOUSE, list_package_items

        try:
            return list_package_items(session, int(PACKAGE_INDEX_WAREHOUSE), log=log)
        except Exception as e:
            log(f"state WAREHOUSE err: {e}")
            return []

    if kind is StateKind.MONEY:
        from app.core.live_scene_hub import get_live_scene

        snap = get_live_scene(int(session.pid), max_age_s=None)
        if (
            snap is not None
            and (snap.money is not None or snap.money_bind is not None)
            and snap.field_age_s("money_updated_at") < 4.0
        ):
            return {
                "ok": True,
                "money": snap.money_bind if snap.money_bind is not None else snap.money,
                "money_trade": snap.money,
                "source": f"hub:{snap.source}",
            }
        from app.core.package_api import get_money

        r = get_money(session, log=log)
        return {
            "ok": bool(getattr(r, "ok", True)),
            "money": getattr(r, "money", None),
            "money_trade": getattr(r, "money_trade", None),
            "message": getattr(r, "message", None),
        }

    if kind is StateKind.HOST_ID:
        from app.core.live_scene_hub import get_live_scene

        snap = get_live_scene(int(session.pid), max_age_s=None)
        if snap is not None and snap.host_id:
            return int(snap.host_id)
        from app.core.plg_ui import get_host_player_id, get_host_player_ptr

        host = get_host_player_ptr(session, log=log)
        lo, hi = get_host_player_id(session, host_ptr=host, log=log)
        return (int(hi) << 32) | (int(lo) & 0xFFFFFFFF)

    if kind is StateKind.ROLL:
        from app.core.hang_settings import _iter_active_loot_rolls

        return list(_iter_active_loot_rolls(session, log=log) or [])

    raise ValueError(f"unsupported state kind: {kind}")


def get_state(
    session,
    kind: StateKind | str,
    *,
    fresh: bool = False,
    max_age: float | None = None,
    log: LogFn | None = None,
) -> Any:
    """
    Read one state through dispatch cache.

    - fresh=True or max_age=0: must-live, producer runs now (single-flight) and
      updates cache when ttl>0.
    - otherwise: return warm cache if young enough, else produce.

    @author by ak
    """
    log = log or (lambda _m: None)
    pid = _pid_of(session)
    if pid <= 0:
        raise ValueError("no pid")
    k = _as_kind(kind)
    pol = policy_of(k)
    age = 0.0 if fresh else (pol.max_age_s if max_age is None else float(max_age))
    key = cache_key_for(k)
    d = get_dispatch()

    def _prod():
        return _produce(session, k, log)

    return d.read_cached(
        pid,
        key,
        _prod,
        max_age=age,
        ttl_s=pol.ttl_s,
        priority=pol.priority,
        kind=pol.kind_op,
        op=f"state:{k.value}",
    )


def require_fresh(
    session,
    kind: StateKind | str,
    *,
    log: LogFn | None = None,
) -> Any:
    """Force active refresh + cache update. @author by ak"""
    return get_state(session, kind, fresh=True, max_age=0.0, log=log)


def peek_state(
    session_or_pid,
    kind: StateKind | str,
    *,
    max_age: float | None = None,
) -> Any | None:
    """Non-producing cache peek (None if missing/stale). @author by ak"""
    pid = _pid_of(session_or_pid)
    if pid <= 0:
        return None
    pol = policy_of(kind)
    age = pol.max_age_s if max_age is None else max_age
    return get_dispatch().get_cached(pid, cache_key_for(kind), max_age=age)


def prefetch_states(
    session,
    kinds: Iterable[StateKind | str] | None = None,
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """
    Warm prefetchable states before a business step (fly/trade/team…).

    Does not force must-live fields; each kind uses its soft max_age.
    Failures are logged and skipped so precheck never blocks hard.

    @author by ak
    """
    log = log or (lambda _m: None)
    wanted = list(kinds) if kinds is not None else [
        StateKind.SCENE,
        StateKind.POS,
        StateKind.DEAD,
        StateKind.VITALITY,
        StateKind.BAG,
        StateKind.MONEY,
        StateKind.HOST_ID,
    ]
    out: dict[str, Any] = {}
    for raw in wanted:
        try:
            k = _as_kind(raw)
            pol = policy_of(k)
            if not pol.prefetchable:
                continue
            out[k.value] = get_state(session, k, fresh=False, log=log)
        except Exception as e:
            log(f"prefetch {raw} err: {e}")
            out[str(raw)] = None
    return out


def invalidate_states(
    session_or_pid,
    *kinds: StateKind | str,
) -> None:
    """Drop caches after write / teleport / trade. @author by ak"""
    pid = _pid_of(session_or_pid)
    if pid <= 0:
        return
    d = get_dispatch()
    if not kinds:
        # common write fan-out
        d.invalidate(pid, CACHE_BAG, "warehouse", "money", CACHE_SCENE, "pos")
        return
    for raw in kinds:
        k = _as_kind(raw)
        d.invalidate(pid, cache_key_for(k), policy_of(k).cache_prefix)



def warmup_session(
    session,
    kinds: Iterable[StateKind | str] | None = None,
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """
    Runner attach / 进页：非马上需要状态预热。

    默认 SCENE/POS/BAG/MONEY/HOST_ID。失败不抛，不 force-fresh。
    @author by ak
    """
    return prefetch_states(
        session,
        kinds
        if kinds is not None
        else (
            StateKind.SCENE,
            StateKind.POS,
            StateKind.BAG,
            StateKind.MONEY,
            StateKind.HOST_ID,
        ),
        log=log,
    )


__all__ = [
    "StateKind",
    "StatePolicy",
    "STATE_POLICIES",
    "get_state",
    "require_fresh",
    "peek_state",
    "prefetch_states",
    "warmup_session",
    "invalidate_states",
    "cache_key_for",
    "policy_of",
]
