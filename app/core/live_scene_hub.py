# -*- coding: utf-8 -*-
"""
会话级实时状态中枢：一处采样，多处分发。

设计：
- 每个游戏 pid 一个 LiveSceneHub（宿主实时状态机缓存）
- 功能窗标题栏轮询是主生产者：必须直接读内存，再 publish
- 其它模块 get()/peek() 消费缓存，或 subscribe() 订阅地图变化
- 禁止业务侧各自重复 open_attach + 场景扫描

状态分层（一轮能拉的优先放进 producer）：
  P0 每 tick：scene_id / label / pos / role / dead(节流 CRT)
  P1 2~3s：vitality / money（RPM 为主）
  P2 10~15s：equip_dura / host_id / in_team
  不进连续 producer：整包 bag、仓库、roll 列表、队伍名册（按需 state_dispatch）

@author by ak
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, fields
from typing import Any, Callable, Iterable

Listener = Callable[["LiveSceneSnapshot", "LiveSceneSnapshot | None"], None]

# Consumers may treat samples younger than this as "live enough".
DEFAULT_LIVE_MAX_AGE_S = 2.5

# Producer cadence hints (header owns timer; sample_host_live honors ages)
HOST_SAMPLE_VITALITY_MAX_AGE_S = 2.5
HOST_SAMPLE_MONEY_MAX_AGE_S = 3.0
HOST_SAMPLE_EQUIP_MAX_AGE_S = 12.0
HOST_SAMPLE_HOST_ID_MAX_AGE_S = 15.0
HOST_SAMPLE_TEAM_MAX_AGE_S = 8.0


@dataclass
class LiveSceneSnapshot:
    """One live host sample for a game pid (scene + vitals). @author by ak"""

    pid: int
    scene_id: int | None = None
    scene_label: str = ""
    role_name: str = ""
    pos: tuple[float, float, float] | None = None
    dead: bool | None = None
    # extended host vitals (optional; filled by header producer over cadence)
    host_id: int | None = None
    vitality_pct: float | None = None
    money: int | None = None
    money_bind: int | None = None
    equip_dura_pct: float | None = None
    equip_dura_min_pct: float | None = None
    in_team: bool | None = None
    updated_at: float = 0.0
    dead_updated_at: float = 0.0
    vitality_updated_at: float = 0.0
    money_updated_at: float = 0.0
    equip_updated_at: float = 0.0
    source: str = ""

    def map_key(self) -> str:
        """Stable key for map-change detection. @author by ak"""
        sid = "" if self.scene_id is None else str(int(self.scene_id))
        lab = (self.scene_label or "").strip()
        return f"{sid}|{lab}"

    def age_s(self, now: float | None = None) -> float:
        now = time.time() if now is None else float(now)
        if not self.updated_at:
            return 1e9
        return max(0.0, now - float(self.updated_at))

    def dead_age_s(self, now: float | None = None) -> float:
        now = time.time() if now is None else float(now)
        if not self.dead_updated_at:
            return 1e9
        return max(0.0, now - float(self.dead_updated_at))

    def field_age_s(self, field_ts_name: str, now: float | None = None) -> float:
        now = time.time() if now is None else float(now)
        ts = float(getattr(self, field_ts_name, 0.0) or 0.0)
        if not ts:
            return 1e9
        return max(0.0, now - ts)

    def to_dict(self) -> dict:
        return asdict(self)


class LiveSceneHub:
    """
    Per-pid live host state cache + fan-out.

    @author by ak
    """

    def __init__(self, pid: int):
        self.pid = int(pid)
        self._lock = threading.RLock()
        self._snap = LiveSceneSnapshot(pid=self.pid)
        self._listeners: list[Listener] = []
        self._last_map_key: str = ""

    def get(self, *, max_age_s: float | None = DEFAULT_LIVE_MAX_AGE_S) -> LiveSceneSnapshot | None:
        """
        Active query. Returns None if empty or older than max_age_s.

        max_age_s=None => always return last snapshot (may be stale).

        @author by ak
        """
        with self._lock:
            snap = LiveSceneSnapshot(**asdict(self._snap))
        if not snap.updated_at:
            return None
        if max_age_s is not None and snap.age_s() > float(max_age_s):
            return None
        if not (
            snap.scene_label
            or snap.scene_id is not None
            or snap.pos is not None
            or snap.dead is not None
            or snap.vitality_pct is not None
            or snap.money is not None
        ):
            return None
        return snap

    def peek(self) -> LiveSceneSnapshot:
        """Return last snapshot even if stale/empty. @author by ak"""
        with self._lock:
            return LiveSceneSnapshot(**asdict(self._snap))

    def subscribe(self, listener: Listener) -> None:
        """Register map-change listener: cb(new_snap, old_snap). @author by ak"""
        if not callable(listener):
            return
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        with self._lock:
            self._listeners = [x for x in self._listeners if x is not listener]

    def publish(
        self,
        *,
        scene_id: int | None = None,
        scene_label: str | None = None,
        role_name: str | None = None,
        pos: tuple[float, float, float] | None = None,
        dead: bool | None = None,
        host_id: int | None = None,
        vitality_pct: float | None = None,
        money: int | None = None,
        money_bind: int | None = None,
        equip_dura_pct: float | None = None,
        equip_dura_min_pct: float | None = None,
        in_team: bool | None = None,
        source: str = "header",
        touch_dead: bool = False,
        touch_vitality: bool = False,
        touch_money: bool = False,
        touch_equip: bool = False,
    ) -> LiveSceneSnapshot:
        """
        Producer push (title-bar / fresh memory sampler).

        Notifies listeners only on map change.
        When scene_id changes, stale labels are cleared unless a new label is given.

        @author by ak
        """
        now = time.time()
        with self._lock:
            old = LiveSceneSnapshot(**asdict(self._snap))
            sid_changed = False
            if scene_id is not None:
                try:
                    new_sid = int(scene_id)
                    if self._snap.scene_id is not None and int(self._snap.scene_id) != new_sid:
                        sid_changed = True
                    self._snap.scene_id = new_sid
                except Exception:
                    pass
            if scene_label is not None:
                t = (scene_label or "").strip()
                if t and t != "-":
                    self._snap.scene_label = t
                elif sid_changed:
                    self._snap.scene_label = ""
            elif sid_changed:
                self._snap.scene_label = ""
            if role_name is not None:
                t = (role_name or "").strip()
                if t and t != "-":
                    self._snap.role_name = t
            if pos is not None and len(pos) >= 3:
                try:
                    self._snap.pos = (float(pos[0]), float(pos[1]), float(pos[2]))
                except Exception:
                    pass
            if host_id is not None:
                try:
                    self._snap.host_id = int(host_id) or self._snap.host_id
                except Exception:
                    pass
            if vitality_pct is not None or touch_vitality:
                if vitality_pct is not None:
                    try:
                        self._snap.vitality_pct = float(vitality_pct)
                    except Exception:
                        pass
                self._snap.vitality_updated_at = now
            if money is not None or money_bind is not None or touch_money:
                if money is not None:
                    try:
                        self._snap.money = int(money)
                    except Exception:
                        pass
                if money_bind is not None:
                    try:
                        self._snap.money_bind = int(money_bind)
                    except Exception:
                        pass
                self._snap.money_updated_at = now
            if equip_dura_pct is not None or equip_dura_min_pct is not None or touch_equip:
                if equip_dura_pct is not None:
                    try:
                        self._snap.equip_dura_pct = float(equip_dura_pct)
                    except Exception:
                        pass
                if equip_dura_min_pct is not None:
                    try:
                        self._snap.equip_dura_min_pct = float(equip_dura_min_pct)
                    except Exception:
                        pass
                self._snap.equip_updated_at = now
            if in_team is not None:
                self._snap.in_team = bool(in_team)

            scene_touched = any(
                x is not None
                for x in (scene_id, scene_label, role_name, pos)
            ) or sid_changed
            if touch_dead or dead is not None:
                self._snap.dead = None if dead is None else bool(dead)
                self._snap.dead_updated_at = now
            # Death-only / vitals-only publishes must NOT refresh scene age alone
            # unless scene fields also changed — keep death/vitals timestamps separate.
            if scene_touched or not self._snap.updated_at:
                self._snap.updated_at = now
                if source:
                    self._snap.source = str(source or "")
            elif source and not self._snap.source:
                self._snap.source = str(source or "")
            # If only vitals updated, still bump source lightly when empty
            if (touch_vitality or touch_money or touch_equip or touch_dead) and source:
                if not scene_touched and self._snap.source in ("", "header"):
                    self._snap.source = str(source)
            new = LiveSceneSnapshot(**asdict(self._snap))
            key = new.map_key()
            changed = bool(key) and key != self._last_map_key and bool(
                new.scene_label or new.scene_id is not None
            )
            if changed:
                self._last_map_key = key
            listeners = list(self._listeners)

        if changed:
            for cb in listeners:
                try:
                    cb(new, old if old.updated_at else None)
                except Exception:
                    pass
        return new


_HUBS: dict[int, LiveSceneHub] = {}
_HUBS_LOCK = threading.RLock()


def get_live_scene_hub(pid: int) -> LiveSceneHub:
    """Get or create hub for game pid. @author by ak"""
    pid = int(pid)
    with _HUBS_LOCK:
        hub = _HUBS.get(pid)
        if hub is None:
            hub = LiveSceneHub(pid)
            _HUBS[pid] = hub
        return hub


def drop_live_scene_hub(pid: int) -> None:
    """Drop hub when feature window closes. @author by ak"""
    with _HUBS_LOCK:
        _HUBS.pop(int(pid), None)


def publish_live_scene(
    pid: int,
    *,
    scene_id: int | None = None,
    scene_label: str | None = None,
    role_name: str | None = None,
    pos: tuple[float, float, float] | None = None,
    dead: bool | None = None,
    host_id: int | None = None,
    vitality_pct: float | None = None,
    money: int | None = None,
    money_bind: int | None = None,
    equip_dura_pct: float | None = None,
    equip_dura_min_pct: float | None = None,
    in_team: bool | None = None,
    source: str = "header",
    touch_dead: bool = False,
    touch_vitality: bool = False,
    touch_money: bool = False,
    touch_equip: bool = False,
) -> LiveSceneSnapshot:
    """Convenience producer API. @author by ak"""
    return get_live_scene_hub(pid).publish(
        scene_id=scene_id,
        scene_label=scene_label,
        role_name=role_name,
        pos=pos,
        dead=dead,
        host_id=host_id,
        vitality_pct=vitality_pct,
        money=money,
        money_bind=money_bind,
        equip_dura_pct=equip_dura_pct,
        equip_dura_min_pct=equip_dura_min_pct,
        in_team=in_team,
        source=source,
        touch_dead=touch_dead,
        touch_vitality=touch_vitality,
        touch_money=touch_money,
        touch_equip=touch_equip,
    )


def get_live_scene(
    pid: int,
    *,
    max_age_s: float | None = DEFAULT_LIVE_MAX_AGE_S,
) -> LiveSceneSnapshot | None:
    """Convenience consumer API. @author by ak"""
    return get_live_scene_hub(int(pid)).get(max_age_s=max_age_s)


def peek_live_scene(pid: int) -> LiveSceneSnapshot:
    """Return last snapshot even if stale. @author by ak"""
    return get_live_scene_hub(int(pid)).peek()


def subscribe_live_scene(pid: int, listener: Listener) -> None:
    get_live_scene_hub(int(pid)).subscribe(listener)


def unsubscribe_live_scene(pid: int, listener: Listener) -> None:
    get_live_scene_hub(int(pid)).unsubscribe(listener)


def sample_host_live(
    session,
    *,
    prev: LiveSceneSnapshot | None = None,
    include: Iterable[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    One-round host sample for the continuous producer (title-bar).

    Always samples: scene/pos/role + death (hang throttle).
    Cadence-gated: vitality / money / equip_dura / host_id / in_team.

    Does NOT open attach; caller provides session.
    Never reads through LiveSceneHub (avoid feedback freeze).

    @author by ak
    """
    log = log or (lambda _m: None)
    want = set(include) if include is not None else {
        "scene", "pos", "role", "dead", "vitality", "money", "equip", "host_id", "team"
    }
    now = time.time()
    out: dict[str, Any] = {
        "scene_id": None,
        "scene_label": None,
        "role_name": None,
        "pos": None,
        "dead": None,
        "touch_dead": False,
        "host_id": None,
        "vitality_pct": None,
        "money": None,
        "money_bind": None,
        "equip_dura_pct": None,
        "equip_dura_min_pct": None,
        "in_team": None,
        "touch_vitality": False,
        "touch_money": False,
        "touch_equip": False,
        "source": "header_live",
    }

    # --- P0: scene + pos ---
    if "scene" in want or "pos" in want:
        try:
            from app.core.automove import read_scene_position
            from app.core.map_names import format_scene_display

            sp = read_scene_position(session, log=lambda _m: None)
            if getattr(sp, "ok", False):
                if getattr(sp, "scene_id", None) is not None:
                    try:
                        out["scene_id"] = int(sp.scene_id)
                    except Exception:
                        out["scene_id"] = None
                pos_t = getattr(sp, "scene_pos", None)
                if pos_t is not None and len(pos_t) >= 3:
                    out["pos"] = (float(pos_t[0]), float(pos_t[1]), float(pos_t[2]))
                if out["scene_id"] is not None:
                    lab = format_scene_display(int(out["scene_id"])) or ""
                    if lab and lab != "-":
                        out["scene_label"] = str(lab)
        except Exception as e:
            log(f"host_live scene err: {e}")

    # --- P0: role name ---
    if "role" in want:
        try:
            from app.core.plg_ui import get_host_player_name

            name = get_host_player_name(session, log=lambda _m: None)
            if name:
                out["role_name"] = str(name).strip() or None
        except Exception as e:
            log(f"host_live role err: {e}")

    # --- P0: death (internal 3s CRT throttle in hang SM) ---
    if "dead" in want:
        try:
            from app.core.hang_settings import refresh_hang_dead_state

            dead = refresh_hang_dead_state(session, log=lambda _m: None, force=False)
            out["dead"] = dead
            out["touch_dead"] = dead is not None
        except Exception as e:
            log(f"host_live dead err: {e}")

    def _stale(ts_attr: str, max_age: float) -> bool:
        if prev is None:
            return True
        return prev.field_age_s(ts_attr, now) >= float(max_age)

    # --- P1: vitality ---
    if "vitality" in want and _stale("vitality_updated_at", HOST_SAMPLE_VITALITY_MAX_AGE_S):
        try:
            from app.core.hang_settings import read_vitality_pct

            vit = read_vitality_pct(session, log=lambda _m: None)
            if vit.get("ready") and vit.get("pct") is not None:
                out["vitality_pct"] = float(vit["pct"])
                out["touch_vitality"] = True
        except Exception as e:
            log(f"host_live vitality err: {e}")

    # --- P1: money ---
    if "money" in want and _stale("money_updated_at", HOST_SAMPLE_MONEY_MAX_AGE_S):
        try:
            from app.core.package_api import get_money

            mr = get_money(session, log=lambda _m: None)
            bind = getattr(mr, "money", None)  # bind primary in package_api
            trade = getattr(mr, "money_trade", None)
            if bind is not None:
                out["money_bind"] = int(bind)
                out["touch_money"] = True
            if trade is not None:
                out["money"] = int(trade)
                out["touch_money"] = True
            elif bind is not None:
                out["money"] = int(bind)
        except Exception as e:
            log(f"host_live money err: {e}")

    # --- P2: equip dura ---
    if "equip" in want and _stale("equip_updated_at", HOST_SAMPLE_EQUIP_MAX_AGE_S):
        try:
            from app.core.hang_settings import read_equipment_durability_pct

            er = read_equipment_durability_pct(session, log=lambda _m: None)
            if er.get("ready") and er.get("pct") is not None:
                out["equip_dura_pct"] = float(er["pct"])
                if er.get("min_pct") is not None:
                    out["equip_dura_min_pct"] = float(er["min_pct"])
                out["touch_equip"] = True
        except Exception as e:
            log(f"host_live equip err: {e}")

    # --- P2: host_id ---
    if "host_id" in want:
        need_id = prev is None or not prev.host_id or _stale("updated_at", HOST_SAMPLE_HOST_ID_MAX_AGE_S)
        # host_id rarely changes; refresh sparsely using scene age as proxy if no dedicated ts
        if need_id and (prev is None or prev.field_age_s("updated_at", now) >= HOST_SAMPLE_HOST_ID_MAX_AGE_S or not prev.host_id):
            try:
                from app.core.plg_ui import get_host_player_id, get_host_player_ptr

                host = get_host_player_ptr(session, log=lambda _m: None)
                lo, hi = get_host_player_id(session, host_ptr=host, log=lambda _m: None)
                if lo or hi:
                    out["host_id"] = (int(hi) << 32) | (int(lo) & 0xFFFFFFFF)
            except Exception as e:
                log(f"host_live host_id err: {e}")

    # --- P2: in_team ---
    if "team" in want and (prev is None or prev.field_age_s("updated_at", now) >= HOST_SAMPLE_TEAM_MAX_AGE_S or prev.in_team is None):
        try:
            from app.core.plg_ui import is_host_in_team

            out["in_team"] = bool(is_host_in_team(session, log=lambda _m: None))
        except Exception as e:
            log(f"host_live team err: {e}")

    return out


def publish_host_sample(pid: int, sample: dict[str, Any], *, source: str = "header_live") -> LiveSceneSnapshot:
    """Publish sample_host_live result into hub. @author by ak"""
    return publish_live_scene(
        int(pid),
        scene_id=sample.get("scene_id"),
        scene_label=sample.get("scene_label"),
        role_name=sample.get("role_name"),
        pos=sample.get("pos"),
        dead=sample.get("dead"),
        host_id=sample.get("host_id"),
        vitality_pct=sample.get("vitality_pct"),
        money=sample.get("money"),
        money_bind=sample.get("money_bind"),
        equip_dura_pct=sample.get("equip_dura_pct"),
        equip_dura_min_pct=sample.get("equip_dura_min_pct"),
        in_team=sample.get("in_team"),
        source=str(sample.get("source") or source),
        touch_dead=bool(sample.get("touch_dead")),
        touch_vitality=bool(sample.get("touch_vitality")),
        touch_money=bool(sample.get("touch_money")),
        touch_equip=bool(sample.get("touch_equip")),
    )


__all__ = [
    "DEFAULT_LIVE_MAX_AGE_S",
    "LiveSceneSnapshot",
    "LiveSceneHub",
    "get_live_scene_hub",
    "drop_live_scene_hub",
    "publish_live_scene",
    "get_live_scene",
    "peek_live_scene",
    "subscribe_live_scene",
    "unsubscribe_live_scene",
    "sample_host_live",
    "publish_host_sample",
    "HOST_SAMPLE_VITALITY_MAX_AGE_S",
    "HOST_SAMPLE_MONEY_MAX_AGE_S",
    "HOST_SAMPLE_EQUIP_MAX_AGE_S",
]
