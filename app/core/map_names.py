# -*- coding: utf-8 -*-
"""Map id / path / plg scene_id -> Chinese display name."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path


def _data_dir() -> Path:
    """
    Resolve app/data for source and frozen runs.

    @author by ak
    """
    try:
        from common.paths import APP_DATA_DIR

        if APP_DATA_DIR.is_dir():
            return APP_DATA_DIR
    except Exception:
        pass
    return Path(__file__).resolve().parents[1] / "data"


DATA = _data_dir() / "map_names.json"
SCENE_DATA = _data_dir() / "scene_map.json"


@lru_cache(maxsize=1)
def load_map_names() -> dict[str, str]:
    if not DATA.exists():
        return {}
    try:
        return json.loads(DATA.read_text(encoding="utf-8"))
    except Exception:
        return {}


@lru_cache(maxsize=1)
def load_scene_map() -> dict:
    """
    Load scene_id -> map resource / display name table.

    Built from InstInfo_Client/Common (+ design SID/MAP). See tools/_build_scene_map.py.
    Keys: scene_to_map, scene_to_name.
    @author by ak
    """
    if not SCENE_DATA.exists():
        return {"scene_to_map": {}, "scene_to_name": {}}
    try:
        data = json.loads(SCENE_DATA.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"scene_to_map": {}, "scene_to_name": {}}
        return {
            "scene_to_map": dict(data.get("scene_to_map") or {}),
            "scene_to_name": dict(data.get("scene_to_name") or {}),
        }
    except Exception:
        return {"scene_to_map": {}, "scene_to_name": {}}


def clear_map_caches() -> None:
    """Drop cached map tables after rebuild. @author by ak"""
    load_map_names.cache_clear()
    load_scene_map.cache_clear()


def normalize_map_id(value: str | None) -> str | None:
    if not value:
        return None
    t = value.strip().replace("/", "\\")
    # maps\d10_1\d10_1.dis -> d10_1
    m = re.search(r"(?i)(?:maps\\)?([a-z]\d+(?:_\d+)?)(?:\\|\.|$)", t)
    if m:
        return m.group(1).lower() if m.group(1)[0].isalpha() else m.group(1)
    # already id-like
    m = re.fullmatch(r"(?i)[a-z]\d+(?:_\d+)?", t)
    if m:
        return t
    # basename without ext
    base = Path(t.replace("\\", "/")).stem
    if re.fullmatch(r"(?i)[a-z]\d+(?:_\d+)?", base):
        return base
    return t


def resolve_map_name(map_id_or_path: str | None) -> tuple[str | None, str | None]:
    """
    Returns (map_id, chinese_name).
    chinese_name may be None if unknown.
    """
    mid = normalize_map_id(map_id_or_path)
    if not mid:
        return None, None
    names = load_map_names()
    # exact
    if mid in names:
        return mid, names[mid]
    # case variants
    for k, v in names.items():
        if k.lower() == mid.lower():
            return k, v
    return mid, None


def strip_map_tag(name: str | None) -> str | None:
    """
    Strip InstInfo activity tags like [门派日常] from display names.

    @author by ak
    """
    if not name:
        return None
    t = str(name).strip()
    t = re.sub(r"^\[[^\]]+\]\s*", "", t)
    t = t.replace("\\r", " ").split("\r")[0].split("\n")[0].strip()
    return t or None


def resolve_scene_id(scene_id: int | str | None) -> tuple[str | None, str | None]:
    """
    Resolve plg GetCurrentScenePosition scene_id to (map_id, chinese_name).

    Name source (priority):
    1. scene_map.json scene_to_name (from InstInfo_Client / design city table)
    2. map_names.json for resource id, with [activity] tags stripped
    Never show raw activity titles like [门派日常]华山新 as the city name.

    @author by ak
    """
    if scene_id is None or scene_id == "":
        return None, None
    try:
        sid = str(int(scene_id))
    except (TypeError, ValueError):
        return None, None
    table = load_scene_map()
    mid = table["scene_to_map"].get(sid)
    cn = strip_map_tag(table["scene_to_name"].get(sid))
    if mid:
        mid = normalize_map_id(mid) or mid
    if not cn and mid:
        _, cn2 = resolve_map_name(mid)
        cn = strip_map_tag(cn2)
    return mid, cn


def format_scene_display(scene_id: int | str | None) -> str:
    """Human map label from live scene_id (InstInfo mapping). @author by ak"""
    mid, cn = resolve_scene_id(scene_id)
    if not mid and not cn:
        return "-"
    if cn and mid:
        return f"{cn} ({mid})"
    return cn or mid or "-"


def format_map_display(map_id_or_path: str | None) -> str:
    mid, name = resolve_map_name(map_id_or_path)
    if not mid:
        return "-"
    name = strip_map_tag(name)
    if name:
        return f"{name} ({mid})"
    return mid
