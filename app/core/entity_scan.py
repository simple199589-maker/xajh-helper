# -*- coding: utf-8 -*-
"""
Nearby entity recon via plg AOI list (GetObjects / GetObjectCount).

Primary path (live-verified):
  class 0 = players
  class 1 = matters / ground items
  class 2 = NPC + monsters (shared CECNPC AOI)

Name/dist/tid from plg; world pos at object+0x158.

Legacy dictionary string-scan is no longer used for "周围" results.

@author by ak
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

from app.core.plg_objects import (
    CLASS_MATTER,
    CLASS_NPC,
    CLASS_PLAYER,
    list_class_objects,
)

LogFn = Callable[[str], None]


def _entity_names_path() -> Path:
    """
    Resolve entity_names.json for source and frozen runs.

    @author by ak
    """
    try:
        from common.paths import APP_DATA_DIR

        p = APP_DATA_DIR / "entity_names.json"
        if p.is_file() or APP_DATA_DIR.is_dir():
            return p
    except Exception:
        pass
    return Path(__file__).resolve().parents[1] / "data" / "entity_names.json"


DATA = _entity_names_path()

KIND_MONSTER = "monster"
KIND_NPC = "npc"
KIND_MATTER = "matter"
KIND_PLAYER = "player"
KIND_ALL = (KIND_MONSTER, KIND_NPC, KIND_MATTER)


@dataclass
class EntityHit:
    kind: str  # monster / npc / matter / player / unknown
    name: str
    address: int
    x: float | None = None
    y: float | None = None
    z: float | None = None
    dist: float | None = None
    score: int = 0
    note: str = ""
    tid: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_MONSTER_HINT = re.compile(
    r"(怪|兽|贼|盗|兵|魔|魂|尸|妖|邪|狼|虎|蛇|蛛|匪|刺客|护法|精英|首领|卫|侍|奴|傀|鬼|精|徒|探子)"
)
_NPC_HINT = re.compile(
    r"(掌门|长老|使者|商人|老板|掌柜|弟子|师傅|师父|镖师|捕头|船家|老板娘|郎中|医师|村民|百姓|店小二|客商|教头|公子|药商)"
)


@lru_cache(maxsize=1)
def load_entity_dict() -> dict[str, str]:
    """name -> kind (monster/npc/matter). @author by ak"""
    out: dict[str, str] = {}
    if not DATA.exists():
        return out
    try:
        raw = json.loads(DATA.read_text(encoding="utf-8"))
    except Exception:
        return out
    for kind in KIND_ALL:
        for n in raw.get(kind) or []:
            if isinstance(n, str) and 1 <= len(n) <= 24:
                out[n] = kind
    return out


def _normalize_kinds(kinds: Iterable[str] | None) -> set[str]:
    if not kinds:
        return set(KIND_ALL)
    out: set[str] = set()
    for k in kinds:
        k = (k or "").strip().lower()
        if k in ("monster", "mon", "怪物"):
            out.add(KIND_MONSTER)
        elif k in ("npc", "npm", "npcs"):
            out.add(KIND_NPC)
        elif k in ("matter", "item", "items", "道具", "物品"):
            out.add(KIND_MATTER)
        elif k in ("player", "ply", "玩家"):
            out.add(KIND_PLAYER)
        elif k in KIND_ALL or k == KIND_PLAYER:
            out.add(k)
    return out or set(KIND_ALL)


def classify_name(name: str, *, class_id: int | None = None) -> str:
    """
    Classify display kind for a live object.

    Dict first; then class_id; then name heuristics.
    @author by ak
    """
    name = (name or "").strip()
    edict = load_entity_dict()
    if name and name in edict:
        return edict[name]
    if class_id == CLASS_PLAYER:
        return KIND_PLAYER
    if class_id == CLASS_MATTER:
        return KIND_MATTER
    if name and _MONSTER_HINT.search(name) and not _NPC_HINT.search(name):
        return KIND_MONSTER
    if name and _NPC_HINT.search(name):
        return KIND_NPC
    # class 2 unknowns: treat as npc for 周围npm (common in town); monsters still match hint/dict
    if class_id == CLASS_NPC:
        return KIND_NPC if name else "unknown"
    return "unknown"


def scan_nearby_entities(
    session_or_pm,
    host_pos: tuple[float, float, float] | None = None,
    radius: float = 80.0,
    limit: int = 80,
    kinds: Iterable[str] | None = None,
    require_pos: bool = True,
    log: LogFn | None = None,
) -> list[EntityHit]:
    """
    Scan nearby entities of selected kinds via plg GetObjects.

    Accepts GameAttachSession (preferred) or raw pymem (legacy; returns empty
    with a log note because plg needs module_base + pid).

    @author by ak
    """
    log = log or (lambda _m: None)
    kind_set = _normalize_kinds(kinds)

    session = session_or_pm
    # duck-type: real session has pid + module_base + pm
    if session is None or not (
        getattr(session, "pid", None)
        and getattr(session, "module_base", None)
        and getattr(session, "pm", None)
    ):
        log(
            "entity scan needs GameAttachSession (pid/module_base); "
            "legacy pymem-only string scan removed"
        )
        return []

    if host_pos is None and require_pos:
        # still allow plg list; radius filter uses GetObjectDistToHost
        log("entity scan: no host_pos; radius filter uses GetObjectDistToHost only")

    # map kinds -> plg class ids to query
    class_ids: list[int] = []
    if kind_set & {KIND_NPC, KIND_MONSTER, "unknown"}:
        class_ids.append(CLASS_NPC)
    if KIND_MATTER in kind_set:
        class_ids.append(CLASS_MATTER)
    if KIND_PLAYER in kind_set:
        class_ids.append(CLASS_PLAYER)
    if not class_ids:
        class_ids = [CLASS_NPC]

    # entity scan success path is high-frequency; omit routine log

    hits: list[EntityHit] = []
    per_class_limit = max(int(limit) * 2, 40)
    for cid in class_ids:
        objs = list_class_objects(
            session,
            cid,
            host_pos=host_pos,
            radius=float(radius) if radius else None,
            limit=per_class_limit,
            log=log,
        )
        for o in objs:
            kind = classify_name(o.name, class_id=o.class_id)
            # class2 holds both npc and monster — filter by requested kinds
            if kind not in kind_set:
                if KIND_NPC in kind_set and kind != KIND_MONSTER:
                    # AOI list minus clear monsters -> npm
                    kind = KIND_NPC
                elif KIND_MATTER in kind_set and kind in ("unknown", KIND_MATTER):
                    kind = KIND_MATTER
                elif KIND_MONSTER in kind_set:
                    continue
                else:
                    continue
            if require_pos and o.x is None and o.dist is None:
                continue
            name = o.name or (f"tid:{o.tid}" if o.tid is not None else f"obj:{o.ptr:X}")
            score = 50
            if o.name:
                score += 20
            if o.dist is not None:
                score += max(0, int(30 - o.dist / 2))
            hits.append(
                EntityHit(
                    kind=kind,
                    name=name,
                    address=int(o.ptr),
                    x=o.x,
                    y=o.y,
                    z=o.z,
                    dist=o.dist,
                    score=score,
                    note=o.note + (f" tid={o.tid}" if o.tid is not None else ""),
                    tid=o.tid,
                )
            )

    def sort_key(h: EntityHit):
        pri = {
            KIND_MONSTER: 0,
            KIND_NPC: 1,
            KIND_MATTER: 2,
            KIND_PLAYER: 3,
            "unknown": 4,
        }.get(h.kind, 5)
        d = h.dist if h.dist is not None else 1e9
        has_pos = 0 if h.x is not None else 1
        return (has_pos, d, pri, -h.score)

    # de-dupe by ptr
    best: dict[int, EntityHit] = {}
    for h in hits:
        if h.address not in best or sort_key(h) < sort_key(best[h.address]):
            best[h.address] = h
    out = sorted(best.values(), key=sort_key)[: int(limit)]
    with_pos = sum(1 for h in out if h.x is not None)
    # entity hits count log omitted (high-frequency)
    return out


def classify_bucket(entities: list[EntityHit]) -> dict[str, list[EntityHit]]:
    buckets: dict[str, list[EntityHit]] = {
        KIND_MONSTER: [],
        KIND_NPC: [],
        KIND_MATTER: [],
        "unknown": [],
        KIND_PLAYER: [],
    }
    for e in entities:
        buckets.setdefault(e.kind, []).append(e)
    return buckets
