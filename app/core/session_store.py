# -*- coding: utf-8 -*-
"""
In-process multi-game session store (one mount per injected xajh pid).

@author by ak
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class GameSession:
    """
    One injected / mounted game client.

    @author by ak
    """

    pid: int
    hwnd: int
    title: str = ""
    original_title: str = ""
    bridge_note: str = ""
    ping_ret: int | None = None
    mounted_at: float = field(default_factory=time.time)
    note: str = ""
    # Immutable identity captured for this injection. Business runners read this
    # context instead of re-attaching to discover which role owns a config.
    role_id: str = ""
    role_name: str = ""
    # Dynamic live-header fields stay on the mounted session so the scheduler
    # center can inspect map/name/identity from one place.
    map_label: str = ""
    scene_id: int | None = None

    def label(self) -> str:
        """Short list label. @author by ak"""
        t = self.title or self.original_title or "xajh"
        if len(t) > 40:
            t = t[:37] + "..."
        return f"PID {self.pid} | {t}"


class SessionStore:
    """
    Mounted sessions keyed by pid.

    @author by ak
    """

    def __init__(self) -> None:
        self._sessions: dict[int, GameSession] = {}
        self._listeners: list[Callable[[], None]] = []

    def list(self) -> list[GameSession]:
        """All sessions, newest first. @author by ak"""
        items = list(self._sessions.values())
        items.sort(key=lambda s: s.mounted_at, reverse=True)
        return items

    def get(self, pid: int) -> GameSession | None:
        """Lookup by pid. @author by ak"""
        return self._sessions.get(int(pid))

    def pids(self) -> set[int]:
        """Mounted pid set. @author by ak"""
        return set(self._sessions.keys())

    def has(self, pid: int) -> bool:
        """True if pid already mounted. @author by ak"""
        return int(pid) in self._sessions

    def mount(self, session: GameSession) -> GameSession:
        """Add or replace a session. @author by ak"""
        self._sessions[int(session.pid)] = session
        self._notify()
        return session

    def bind_injected_role(
        self, pid: int, role_id: int | str, role_name: str = ""
    ) -> GameSession | None:
        """Persist the role identity captured for this injection in its session.

        A mounted PID represents one injected client. Do not silently replace a
        bound role with another one: callers must unload/re-inject before a new
        character may own that session's runners and per-role configuration.
        @author by ak
        """
        session = self.get(pid)
        rid = str(role_id or "").strip()
        if session is None or not rid:
            return None
        current = str(session.role_id or "").strip()
        if current and current != rid:
            return None
        changed = current != rid
        session.role_id = rid
        name = str(role_name or "").strip()
        if name and session.role_name != name:
            session.role_name = name
            changed = True
        if changed:
            self._notify()
        return session

    def unmount(self, pid: int) -> GameSession | None:
        """Remove a session; returns removed entry if any. @author by ak"""
        old = self._sessions.pop(int(pid), None)
        if old is not None:
            self._notify()
        return old

    def clear(self) -> None:
        """Drop all sessions. @author by ak"""
        if not self._sessions:
            return
        self._sessions.clear()
        self._notify()

    def count(self) -> int:
        """Mounted session count. @author by ak"""
        return len(self._sessions)

    def on_change(self, cb: Callable[[], None]) -> None:
        """Subscribe to mount/unmount. @author by ak"""
        if cb not in self._listeners:
            self._listeners.append(cb)

    def off_change(self, cb: Callable[[], None]) -> None:
        """Unsubscribe a destroyed page/window listener. @author by ak"""
        try:
            self._listeners.remove(cb)
        except ValueError:
            pass

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                pass
