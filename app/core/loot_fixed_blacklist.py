# -*- coding: utf-8 -*-
"""
Fixed (permanent) loot blacklist: coord-based, survives restart/crash.

Stored under writable config/loot_fixed_blacklist.json.
Entries match by fine XZ skip key (same as runtime dual-key filter).

@author by ak
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from app.core.loot._impl import SuperLootTarget, _coord_skip_key

LogFn = Callable[[str], None]

# Manual fixed ban: expire far future so purge_skip_ids never drops them.
FIXED_EXPIRE_S = 10.0 * 365.0 * 24.0 * 3600.0
FIXED_FILE_NAME = "loot_fixed_blacklist.json"


def fixed_blacklist_path() -> Path:
    """Writable JSON path for permanent chest bans. @author by ak"""
    from common.paths import ensure_writable_dir

    d = ensure_writable_dir("config")
    return d / FIXED_FILE_NAME


def _norm_entry(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one JSON entry; require x/z. @author by ak"""
    try:
        x = float(raw.get("x"))
        z = float(raw.get("z"))
    except Exception:
        return None
    out: dict[str, Any] = {
        "x": x,
        "z": z,
        "name": str(raw.get("name") or ""),
        "tid": int(raw["tid"]) if raw.get("tid") is not None else None,
        "note": str(raw.get("note") or ""),
        "coord_key": int(_coord_skip_key(x, z)),
    }
    if raw.get("obj_id") is not None:
        try:
            out["obj_id"] = int(raw["obj_id"])
        except Exception:
            pass
    return out


def load_fixed_blacklist(*, log: LogFn | None = None) -> list[dict[str, Any]]:
    """Load permanent ban list from disk. @author by ak"""
    log = log or (lambda _m: None)
    path = fixed_blacklist_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"super_loot fixed-bl load err: {e}")
        return []
    raw_list = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        ent = _norm_entry(raw)
        if ent:
            out.append(ent)
    return out


def save_fixed_blacklist(
    entries: list[dict[str, Any]],
    *,
    log: LogFn | None = None,
) -> Path:
    """Write permanent ban list (dedupe by coord_key). @author by ak"""
    log = log or (lambda _m: None)
    path = fixed_blacklist_path()
    by_key: dict[int, dict[str, Any]] = {}
    for raw in entries:
        ent = _norm_entry(raw) if isinstance(raw, dict) else None
        if not ent:
            continue
        by_key[int(ent["coord_key"])] = ent
    payload = {
        "version": 1,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(by_key),
        "entries": list(by_key.values()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log(f"super_loot fixed-bl saved n={len(by_key)} path={path}")
    return path


def apply_fixed_to_skip_ids(
    skip_ids: dict[int, float],
    *,
    entries: list[dict[str, Any]] | None = None,
    log: LogFn | None = None,
) -> int:
    """
    Merge fixed entries into runtime skip_ids (far-future expire).

    Writes coord_key always; obj_id when present.
    @author by ak
    """
    log = log or (lambda _m: None)
    ents = entries if entries is not None else load_fixed_blacklist(log=log)
    exp = time.time() + float(FIXED_EXPIRE_S)
    n = 0
    for ent in ents:
        ck = int(ent.get("coord_key") or 0)
        if ck:
            skip_ids[ck] = exp
            n += 1
        oid = ent.get("obj_id")
        if oid is not None and int(oid) > 0:
            skip_ids[int(oid)] = exp
    if n:
        log(f"super_loot fixed-bl applied entries={len(ents)} keys~{n}")
    return n


def target_to_fixed_entry(
    target: SuperLootTarget | dict,
    *,
    note: str = "",
) -> dict[str, Any] | None:
    """Build fixed JSON entry from a loot target/row. @author by ak"""
    if isinstance(target, dict):
        x = target.get("x")
        z = target.get("z")
        if x is None or z is None:
            return None
        return _norm_entry(
            {
                "x": x,
                "z": z,
                "name": target.get("name") or "",
                "tid": target.get("tid"),
                "obj_id": target.get("obj_id"),
                "note": note or "manual",
            }
        )
    if target.x is None or target.z is None:
        return None
    return _norm_entry(
        {
            "x": target.x,
            "z": target.z,
            "name": target.name or "",
            "tid": target.tid,
            "obj_id": target.obj_id,
            "note": note or "manual",
        }
    )


def add_fixed_entry(
    target: SuperLootTarget | dict,
    *,
    note: str = "manual",
    log: LogFn | None = None,
) -> bool:
    """Append one target to fixed blacklist file. @author by ak"""
    ent = target_to_fixed_entry(target, note=note)
    if not ent:
        return False
    cur = load_fixed_blacklist(log=log)
    cur.append(ent)
    save_fixed_blacklist(cur, log=log)
    return True


def remove_fixed_entry(
    target: SuperLootTarget | dict,
    *,
    log: LogFn | None = None,
) -> bool:
    """Remove matching coord from fixed file. @author by ak"""
    ent = target_to_fixed_entry(target)
    if not ent:
        return False
    ck = int(ent["coord_key"])
    cur = load_fixed_blacklist(log=log)
    nxt = [e for e in cur if int(e.get("coord_key") or 0) != ck]
    if len(nxt) == len(cur):
        return False
    save_fixed_blacklist(nxt, log=log)
    return True


def freeze_runtime_blacklist(
    skip_ids: dict[int, float] | None,
    rows: list[dict] | None = None,
    *,
    note: str = "freeze",
    log: LogFn | None = None,
) -> int:
    """
    Persist current manual/runtime bans that have coordinates.

    Prefers list rows (scan/blacklist tab) for x/z; also keeps existing fixed.
    @author by ak
    """
    log = log or (lambda _m: None)
    cur = load_fixed_blacklist(log=log)
    by_key: dict[int, dict[str, Any]] = {
        int(e["coord_key"]): e for e in cur if e.get("coord_key") is not None
    }
    added = 0
    for row in rows or []:
        ent = target_to_fixed_entry(row, note=note)
        if not ent:
            continue
        ck = int(ent["coord_key"])
        if ck not in by_key:
            added += 1
        by_key[ck] = ent
    # skip_ids alone cannot recover x/z for obj_id-only keys
    save_fixed_blacklist(list(by_key.values()), log=log)
    log(f"super_loot fixed-bl freeze total={len(by_key)} new={added}")
    return len(by_key)
