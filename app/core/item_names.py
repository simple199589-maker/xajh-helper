# -*- coding: utf-8 -*-
"""tid -> display name table for bag consumables and other items.

Static table: app/data/item_names.json (from data.pck name+templ_id).
Runtime learn: when live read gets a good Chinese name, cache it for the session
and optionally merge into a writable learn file next to the software.

@author by ak
"""
from __future__ import annotations

import json
import threading
from functools import lru_cache
from pathlib import Path

_lock = threading.RLock()
_runtime: dict[int, str] = {}


def _data_dir() -> Path:
    try:
        from common.paths import APP_DATA_DIR

        if APP_DATA_DIR.is_dir():
            return APP_DATA_DIR
    except Exception:
        pass
    return Path(__file__).resolve().parents[1] / "data"


def _learn_path() -> Path | None:
    """
    Writable learn file beside software; optional.

    @author by ak
    """
    try:
        from common.paths import app_root

        return app_root() / "app" / "data" / "item_names_learn.json"
    except Exception:
        return None


@lru_cache(maxsize=1)
def load_item_names() -> dict[int, str]:
    """
    Load bundled item_names.json as int tid -> name.

    @author by ak
    """
    path = _data_dir() / "item_names.json"
    out: dict[int, str] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    try:
                        tid = int(k)
                    except Exception:
                        continue
                    name = str(v or "").strip()
                    if tid > 0 and name and not name.startswith("tid="):
                        out[tid] = name
        except Exception:
            pass
    # merge learn file if present
    lp = _learn_path()
    if lp is not None and lp.is_file():
        try:
            raw = json.loads(lp.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    try:
                        tid = int(k)
                    except Exception:
                        continue
                    name = str(v or "").strip()
                    if tid > 0 and name and not name.startswith("tid="):
                        out[tid] = name
        except Exception:
            pass
    return out


def clear_item_name_cache() -> None:
    """Drop static table cache after rebuild. @author by ak"""
    load_item_names.cache_clear()


def lookup_item_name(tid: int) -> str:
    """
    Resolve display name for template id; empty if unknown.

    Priority: runtime session cache -> static/learn table.

    @author by ak
    """
    t = int(tid or 0)
    if t <= 0:
        return ""
    with _lock:
        hit = _runtime.get(t)
        if hit:
            return hit
    table = load_item_names()
    return table.get(t, "")


def learn_item_name(tid: int, name: str, *, persist: bool = False) -> None:
    """
    Remember a verified Chinese name for tid (session; optional disk).

    @author by ak
    """
    t = int(tid or 0)
    n = (name or "").strip()
    if t <= 0 or not n or n.startswith("tid="):
        return
    with _lock:
        _runtime[t] = n
    if not persist:
        return
    lp = _learn_path()
    if lp is None:
        return
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, str] = {}
        if lp.is_file():
            try:
                raw = json.loads(lp.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = {str(k): str(v) for k, v in raw.items()}
            except Exception:
                data = {}
        data[str(t)] = n
        lp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        clear_item_name_cache()
    except Exception:
        pass


def resolve_item_display_name(
    tid: int,
    live_name: str = "",
    *,
    learn: bool = True,
) -> str:
    """
    Prefer live good name, else table map, else tid=N.

    When live name is good, optionally learn it for later tid-only stacks.

    @author by ak
    """
    t = int(tid or 0)
    live = (live_name or "").strip()
    if live and not live.startswith("tid="):
        if learn and t > 0:
            learn_item_name(t, live, persist=False)
        return live
    mapped = lookup_item_name(t) if t else ""
    if mapped:
        return mapped
    if t:
        return f"tid={t}"
    return live or ""
