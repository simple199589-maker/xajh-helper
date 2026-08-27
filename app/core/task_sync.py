# -*- coding: utf-8 -*-
"""
In-process multi-window task sync (master publishes to slaves).

Roles:
  none   — no publish / no receive
  master — local ops fan-out to every slave window
  slave  — receive master events and run on this pid

Team/gather actions may carry `members` roster text; slaves filter by host name.

@author by ak
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

ROLE_NONE = "none"
ROLE_MASTER = "master"
ROLE_SLAVE = "slave"
VALID_ROLES = (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE)

ACTION_ACCEPT = "accept"
ACTION_COMPLETE = "complete"
ACTION_PATH = "path"
ACTION_CLAIM_ACTIVITY = "claim_activity"
ACTION_MAP_FLY = "map_fly"
ACTION_TEAM_FOLLOW = "team_follow"
ACTION_DAILY_FOLLOW = "daily_follow"
ACTION_DAILY_FOLLOW = "daily_follow"
ACTION_TEAM_LEAVE = "team_leave"
# Keep old name as alias for any stale clients / logs
ACTION_TEAM_ACCEPT = "team_accept"
ACTION_HANG_SYNC = "hang_sync"
ACTION_JIANGLONG_QUERY = "jianglong_query"
ACTION_JIANGLONG_CAST = "jianglong_cast"
ACTION_ACCEPT_DAILY_TASKS = "accept_daily_tasks"
ACTION_DAILY_ROUTE = "daily_route"
VALID_ACTIONS = (
    ACTION_ACCEPT,
    ACTION_COMPLETE,
    ACTION_PATH,
    ACTION_CLAIM_ACTIVITY,
    ACTION_MAP_FLY,
    ACTION_TEAM_FOLLOW,
    ACTION_DAILY_FOLLOW,
    ACTION_TEAM_LEAVE,
    ACTION_TEAM_ACCEPT,
    ACTION_HANG_SYNC,
    ACTION_JIANGLONG_QUERY,
    ACTION_JIANGLONG_CAST,
    ACTION_ACCEPT_DAILY_TASKS,
    ACTION_DAILY_ROUTE,
)

# Actions that may use task_id=0
_ZERO_TID_ACTIONS = frozenset(
    {
        ACTION_CLAIM_ACTIVITY,
        ACTION_TEAM_FOLLOW,
        ACTION_DAILY_FOLLOW,
    ACTION_DAILY_FOLLOW,
        ACTION_TEAM_LEAVE,
        ACTION_TEAM_ACCEPT,
        ACTION_MAP_FLY,
        ACTION_HANG_SYNC,
        ACTION_JIANGLONG_QUERY,
        ACTION_JIANGLONG_CAST,
        ACTION_ACCEPT_DAILY_TASKS,
        ACTION_DAILY_ROUTE,
    }
)


@dataclass
class TaskSyncEvent:
    """One master-originated request for slave windows."""

    action: str
    task_id: int
    source_pid: int
    ts: float = field(default_factory=time.time)
    can_finish: bool | None = None
    name: str = ""
    origin: str = "master"  # master | local | cloud
    portal_kind: str = ""  # dungeon_upper | dungeon_lower | tianxiahui | ""
    # Portal path generation.  A slave must not execute an old city/NPC path
    # after its host has already changed scene (for example via team follow).
    origin_scene_id: int | None = None
    portal_tid: int | None = None
    portal_obj_id: int | None = None
    portal_x: float | None = None
    portal_y: float | None = None
    portal_z: float | None = None
    # Flourish points hint for ACTION_CLAIM_ACTIVITY (optional).
    points: int | None = None
    # Team roster filter (顿号分隔角色名). Empty = no filter (legacy all slaves).
    members: str = ""
    # Optional one-shot hang mode override. None keeps each role's saved mode.
    hang_mode: int | None = None
    phase: str = ""
    target_scene_id: int | None = None
    target_tid: int | None = None
    target_x: float | None = None
    target_y: float | None = None
    target_z: float | None = None
    target_round: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


Listener = Callable[[TaskSyncEvent], None]


class TaskSyncHub:
    """
    Process-wide pub/sub for task / team fan-out.

    Each SessionFeatureWindow registers its pid + role. Only master publish
    is fan-out; slaves never re-broadcast (no loops).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._roles: dict[int, str] = {}
        self._listeners: dict[int, Listener] = {}
        self._recent: list[tuple[str, int, int, str, int, float]] = []
        self._dedupe_s = 3.0  # map_fly/team_leave 连点防重

    def set_role(self, pid: int, role: str) -> str:
        pid = int(pid)
        role = str(role or ROLE_NONE).strip().lower()
        if role not in VALID_ROLES:
            role = ROLE_NONE
        with self._lock:
            if role == ROLE_MASTER:
                for other, r in list(self._roles.items()):
                    if other != pid and r == ROLE_MASTER:
                        self._roles[other] = ROLE_NONE
            self._roles[pid] = role
            return role

    def ensure_listener(self, pid: int, listener: Listener) -> None:
        """Idempotent subscribe (alias for clarity at bind sites)."""
        self.subscribe(pid, listener)

    def get_role(self, pid: int) -> str:
        with self._lock:
            return self._roles.get(int(pid), ROLE_NONE)

    def unregister(self, pid: int) -> None:
        pid = int(pid)
        with self._lock:
            self._roles.pop(pid, None)
            self._listeners.pop(pid, None)

    def subscribe(self, pid: int, listener: Listener) -> None:
        with self._lock:
            self._listeners[int(pid)] = listener

    def unsubscribe(self, pid: int) -> None:
        with self._lock:
            self._listeners.pop(int(pid), None)

    def slave_pids(self, *, exclude_pid: int | None = None) -> list[int]:
        with self._lock:
            out = [
                p
                for p, r in self._roles.items()
                if r == ROLE_SLAVE and (exclude_pid is None or p != int(exclude_pid))
            ]
        return sorted(out)

    def master_pids(self) -> list[int]:
        with self._lock:
            return sorted(p for p, r in self._roles.items() if r == ROLE_MASTER)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "roles": dict(self._roles),
                "listeners": sorted(self._listeners.keys()),
                "slaves": self.slave_pids(),
                "masters": self.master_pids(),
            }

    def _is_dup(self, action: str, task_id: int, source_pid: int, phase: str = "", target_round: int | None = None) -> bool:
        now = time.time()
        key = (str(action), int(task_id), int(source_pid), str(phase or ""), int(target_round or 0))
        keep: list[tuple[str, int, int, str, int, float]] = []
        for a, t, p, ph, tr, ts in self._recent:
            if now - ts <= self._dedupe_s:
                keep.append((a, t, p, ph, tr, ts))
        self._recent = keep
        for a, t, p, ph, tr, _ts in self._recent:
            if (a, t, p, ph, tr) == key:
                return True
        self._recent.append((key[0], key[1], key[2], key[3], key[4], now))
        return False

    def publish(
        self,
        *,
        action: str,
        task_id: int,
        source_pid: int,
        can_finish: bool | None = None,
        name: str = "",
        portal_kind: str = "",
        origin_scene_id: int | None = None,
        portal_tid: int | None = None,
        portal_obj_id: int | None = None,
        portal_x: float | None = None,
        portal_y: float | None = None,
        portal_z: float | None = None,
        points: int | None = None,
        members: str = "",
        hang_mode: int | None = None,
        phase: str = "",
        target_scene_id: int | None = None,
        target_tid: int | None = None,
        target_x: float | None = None,
        target_y: float | None = None,
        target_z: float | None = None,
        target_round: int | None = None,
    ) -> int:
        """
        Fan-out from master to all registered slaves. Returns listener count hit.
        """
        action = str(action or "").strip().lower()
        if action not in VALID_ACTIONS:
            return 0
        tid = int(task_id) & 0xFFFFFFFF
        if action not in _ZERO_TID_ACTIONS and not tid:
            return 0
        src = int(source_pid)
        with self._lock:
            if self._roles.get(src) != ROLE_MASTER:
                return 0
            if self._is_dup(action, tid, src, phase=phase, target_round=target_round):
                return 0
            targets = [
                (pid, cb)
                for pid, cb in self._listeners.items()
                if self._roles.get(pid) == ROLE_SLAVE and pid != src
            ]
            orphan_slaves = [
                p
                for p, r in self._roles.items()
                if r == ROLE_SLAVE and p != src and p not in self._listeners
            ]
        event = TaskSyncEvent(
            action=action,
            task_id=tid,
            source_pid=src,
            can_finish=can_finish,
            name=str(name or ""),
            origin="master",
            portal_kind=str(portal_kind or ""),
            origin_scene_id=(
                int(origin_scene_id) if origin_scene_id is not None else None
            ),
            portal_tid=(int(portal_tid) if portal_tid is not None else None),
            portal_obj_id=(
                int(portal_obj_id) if portal_obj_id is not None else None
            ),
            portal_x=(float(portal_x) if portal_x is not None else None),
            portal_y=(float(portal_y) if portal_y is not None else None),
            portal_z=(float(portal_z) if portal_z is not None else None),
            points=(int(points) if points is not None else None),
            members=str(members or ""),
            hang_mode=(int(hang_mode) if hang_mode in (0, 1) else None),
            phase=str(phase or ""),
            target_scene_id=(int(target_scene_id) if target_scene_id is not None else None),
            target_tid=(int(target_tid) if target_tid is not None else None),
            target_x=(float(target_x) if target_x is not None else None),
            target_y=(float(target_y) if target_y is not None else None),
            target_z=(float(target_z) if target_z is not None else None),
            target_round=(int(target_round) if target_round is not None else None),
        )
        n = 0
        for _pid, cb in targets:
            try:
                cb(event)
                n += 1
            except Exception:
                pass
        if orphan_slaves and n == 0:
            return 0
        return n


_HUB: TaskSyncHub | None = None
_HUB_LOCK = threading.Lock()


def get_task_sync_hub() -> TaskSyncHub:
    global _HUB
    with _HUB_LOCK:
        if _HUB is None:
            _HUB = TaskSyncHub()
        return _HUB




