# -*- coding: utf-8 -*-
"""Read and cache a character's loaded Jianglong skill from the game."""
from __future__ import annotations

import threading
import time
from typing import Callable

LogFn = Callable[[str], None]
_LOCK = threading.RLock()
_CONFIG_BY_PID: dict[int, dict] = {}


def get_jianglong_runtime_config(pid: int) -> dict | None:
    with _LOCK:
        value = _CONFIG_BY_PID.get(int(pid))
        return dict(value) if value else None


def resolve_jianglong_runtime_config(
    pid: int,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
    refresh: bool = False,
) -> dict:
    """Resolve loaded Jianglong skill without key input or packet emission."""
    pid = int(pid)
    log = log or (lambda _message: None)
    if not refresh:
        cached = get_jianglong_runtime_config(pid)
        if cached:
            return {"ok": True, **cached, "cached": True, "error": ""}

    from app.core.xajh_bridge import ensure_bridge

    bridge = ensure_bridge(pid, log=log, hwnd=int(hwnd or 0) or None)
    if bridge is None:
        return {"ok": False, "pid": pid, "error": "bridge unavailable"}
    try:
        result = bridge.resolve_jianglong_runtime_config(
            hwnd=int(hwnd or 0) or None,
        )
    finally:
        bridge.close()
    skill_id = int(result.ret or 0)
    if not result.ok or not 0 < skill_id <= 0xFFFFFFFF:
        return {
            "ok": False,
            "pid": pid,
            "error": str(result.error or result.note or "Jianglong skill not found"),
        }
    record = {
        "pid": pid,
        "skill_id": skill_id,
        "action_tag": 0x2305,
        "skill_ptr": int(result.id_hi or 0),
        "source": "loaded_skill_action_tag",
        "captured_at": time.time(),
    }
    with _LOCK:
        _CONFIG_BY_PID[pid] = record
    log(f"降龙: pid={pid} 已定位原生技能 sid=0x{skill_id:X}")
    return {"ok": True, **record, "cached": False, "error": ""}


def clear_jianglong_runtime_config(pid: int) -> None:
    with _LOCK:
        _CONFIG_BY_PID.pop(int(pid), None)


__all__ = [
    "clear_jianglong_runtime_config",
    "get_jianglong_runtime_config",
    "resolve_jianglong_runtime_config",
]
