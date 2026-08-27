# -*- coding: utf-8 -*-
"""
User-facing event log for the formal feature window.

Technical / bridge noise stays in diag_log and shell debug output.
This buffer keeps short Chinese lines for operators.

Categories:
  op       — 主动操作（启停、按钮动作）
  loot     — 自动宝箱精简
  yaolu    — 九层妖楼精简
  activity — 自动副本精简
  control  — 主控 / 副控通知
  task     — 自动任务精简
  grocery  — 杂货使用精简
  system   — 注入 / 窗口等

@author by ak
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable

# Stable keys used by UI filters.
CAT_OP = "op"
CAT_LOOT = "loot"
CAT_YAOLU = "yaolu"
CAT_ACTIVITY = "activity"
CAT_CONTROL = "control"
CAT_TASK = "task"
CAT_GROCERY = "grocery"
CAT_SYSTEM = "system"

CATEGORY_LABELS: dict[str, str] = {
    CAT_OP: "操作",
    CAT_LOOT: "宝箱",
    CAT_YAOLU: "妖楼",
    CAT_ACTIVITY: "副本",
    CAT_CONTROL: "群控",
    CAT_TASK: "任务",
    CAT_GROCERY: "杂货",
    CAT_SYSTEM: "系统",
}

# Nav filter order (「全部」 is UI-only).
FILTER_ORDER: tuple[str, ...] = (
    CAT_OP,
    CAT_YAOLU,
    CAT_ACTIVITY,
    CAT_CONTROL,
    CAT_LOOT,
    CAT_TASK,
    CAT_GROCERY,
    CAT_SYSTEM,
)

Listener = Callable[["UserLogEntry"], None]


@dataclass(frozen=True)
class UserLogEntry:
    """One short operator-facing line."""

    ts: float
    category: str
    message: str
    source: str = ""
    seq: int = 0

    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category or "?")

    def time_text(self) -> str:
        try:
            return time.strftime("%H:%M:%S", time.localtime(self.ts))
        except Exception:
            return "--:--:--"

    def format_line(self) -> str:
        src = f" · {self.source}" if self.source else ""
        return f"[{self.time_text()}] [{self.category_label()}]{src} {self.message}"


class UserEventLog:
    """
    Thread-safe ring buffer + fan-out for formal UI log page.

    @author by ak
    """

    def __init__(self, *, maxlen: int = 500) -> None:
        self._maxlen = max(50, int(maxlen))
        self._lock = threading.RLock()
        self._items: deque[UserLogEntry] = deque(maxlen=self._maxlen)
        self._seq = 0
        self._listeners: list[Listener] = []
        self._last_key: tuple[str, str, str] | None = None
        self._last_ts: float = 0.0

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._last_key = None
            self._last_ts = 0.0

    def subscribe(self, cb: Listener) -> None:
        if cb is None:
            return
        with self._lock:
            if cb not in self._listeners:
                self._listeners.append(cb)

    def unsubscribe(self, cb: Listener) -> None:
        with self._lock:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass

    def append(
        self,
        message: str,
        *,
        category: str = CAT_OP,
        source: str = "",
        dedupe_s: float = 0.8,
    ) -> UserLogEntry | None:
        """
        Append one line. Drops empty / near-duplicate spam within dedupe_s.

        @author by ak
        """
        msg = " ".join(str(message or "").split())
        if not msg:
            return None
        cat = str(category or CAT_OP).strip().lower() or CAT_OP
        if cat not in CATEGORY_LABELS:
            cat = CAT_OP
        src = str(source or "").strip()
        now = time.time()
        key = (cat, src, msg)
        with self._lock:
            if (
                dedupe_s > 0
                and self._last_key == key
                and (now - self._last_ts) < float(dedupe_s)
            ):
                return None
            self._seq += 1
            entry = UserLogEntry(
                ts=now,
                category=cat,
                message=msg,
                source=src,
                seq=self._seq,
            )
            self._items.append(entry)
            self._last_key = key
            self._last_ts = now
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(entry)
            except Exception:
                pass
        return entry

    def snapshot(
        self,
        *,
        category: str | None = None,
        limit: int | None = None,
    ) -> list[UserLogEntry]:
        with self._lock:
            items: Iterable[UserLogEntry] = list(self._items)
        cat = (category or "").strip().lower()
        if cat and cat != "all":
            items = [e for e in items if e.category == cat]
        out = list(items)
        if limit is not None and limit > 0 and len(out) > limit:
            out = out[-int(limit) :]
        return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


_ACTION_CN = {
    "accept": "接取",
    "complete": "交付",
    "path": "寻路",
    "claim_activity": "领活跃宝箱",
    "map_fly": "地图飞行",
    "team_follow": "组队跟随",
    "team_leave": "离队",
    "team_accept": "接受组队",
    "hang_sync": "内挂同步",
    "jianglong_cast": "降龙编排",
}


def action_cn(action: str) -> str:
    """Map task-sync action id to short Chinese. @author by ak"""
    a = str(action or "").strip().lower()
    return _ACTION_CN.get(a, a or "操作")


def role_cn(role: str) -> str:
    """Map control role to Chinese. @author by ak"""
    r = str(role or "").strip().lower()
    return {"master": "主控", "slave": "副控", "none": "无控"}.get(r, r or "无控")
