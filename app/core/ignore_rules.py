# -*- coding: utf-8 -*-
"""
Persistent ignore rules (长驻忽略名单) + unified merged list model.

Storage is per character numeric id (never Chinese display name), keyed by
template id (tid).  Names are display-only / compatibility.

File: <runtime>/config/ignore_rules.json
  {
    "schema_version": 1,
    "by_char": {
      "<char_id>": {
        "<tid>": {"tid": 12345, "name": "上官霸刀", "added_at": 123.4}
      }
    }
  }

Unified list model merges:
  - persistent rules (长驻, stored by tid)
  - game current ignore instances (临时, from the attack policy list, keyed by
    instance id64)
Rows carry: name, tid (if known), instance_ids, persistent, in_game, priority.

@author by ak
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

_SCHEMA_VERSION = 1


def _rules_path() -> Path:
    try:
        from common.paths import ensure_writable_dir

        base = ensure_writable_dir("runtime", "config")
    except Exception:
        try:
            base = Path(__file__).resolve().parents[2] / "runtime" / "config"
        except Exception:
            base = Path.cwd() / "runtime" / "config"
    return Path(base) / "ignore_rules.json"


def _role_rules_path(role_id: str) -> Path:
    """Per-role rules file roles/{role_id}/ignore.json (new source of truth). @author by ak"""
    from app.core.account_manager import role_dir

    return role_dir(role_id) / "ignore.json"


def load_rules_store() -> dict:
    """Return the merged rules store (per-role dirs win, legacy file fallback). @author by ak"""
    by_char: dict[str, dict] = {}
    try:
        from app.core.account_manager import roles_root

        root = roles_root()
        if root.is_dir():
            for child in sorted(root.iterdir(), key=lambda p: str(p.name).zfill(20)):
                key = normalize_char_id(child.name)
                if not key or not child.is_dir():
                    continue
                try:
                    rules = json.loads((child / "ignore.json").read_text(encoding="utf-8"))
                except Exception:
                    rules = None
                if isinstance(rules, dict):
                    cur = by_char.setdefault(key, {})
                    cur.update(rules)
    except Exception:
        pass
    try:
        raw = json.loads(_rules_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("by_char"), dict):
            for key, rules in raw["by_char"].items():
                rid = normalize_char_id(key)
                if not rid or not isinstance(rules, dict):
                    continue
                cur = by_char.setdefault(rid, {})
                cur.update(rules)
    except Exception:
        pass
    return {"schema_version": _SCHEMA_VERSION, "by_char": by_char}


def save_rules_store(store: dict) -> None:
    """Persist the merged store per-role (legacy ignore_rules.json untouched). @author by ak"""
    by_char = store.get("by_char") if isinstance(store.get("by_char"), dict) else {}
    for key, rules in by_char.items():
        rid = normalize_char_id(key)
        if not rid or not isinstance(rules, dict):
            continue
        try:
            path = _role_rules_path(rid)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(path)
        except Exception:
            continue


def normalize_char_id(char_id: int | str | None) -> str:
    """Normalize a role id to its numeric string key ("" when invalid). @author by ak"""
    if char_id is None or isinstance(char_id, bool):
        return ""
    if isinstance(char_id, int):
        return str(int(char_id)) if int(char_id) > 0 else ""
    s = str(char_id).strip()
    if s.isdigit() and int(s) > 0:
        return s
    return ""


def _char_rules(store: dict, char_id: int | str | None) -> dict:
    key = normalize_char_id(char_id)
    if not key:
        return {}
    by_char = store.get("by_char") if isinstance(store.get("by_char"), dict) else {}
    rules = by_char.get(key)
    return rules if isinstance(rules, dict) else {}


def load_char_rules(char_id: int | str | None) -> list[dict]:
    """Return persistent rules for one role: [{tid, name, added_at}]. @author by ak"""
    store = load_rules_store()
    rules = _char_rules(store, char_id)
    out: list[dict] = []
    for tid, rule in rules.items():
        if isinstance(rule, dict):
            out.append({
                "tid": int(tid),
                "name": str(rule.get("name") or ""),
                "added_at": float(rule.get("added_at") or 0.0),
            })
    out.sort(key=lambda r: (r["name"] or "", r["tid"]))
    return out


def add_char_rule(char_id: int | str | None, tid: int, *, name: str = "") -> dict:
    """Upsert one persistent rule by tid. Returns the saved rule. @author by ak"""
    tid = int(tid or 0) & 0xFFFFFFFF
    if not tid:
        return {"ok": False, "error": "tid == 0"}
    key = normalize_char_id(char_id)
    if not key:
        return {"ok": False, "error": "invalid char_id"}
    store = load_rules_store()
    by_char = dict(store.get("by_char") or {})
    rules = dict(by_char.get(key) or {})
    rules[str(tid)] = {
        "tid": tid,
        "name": str(name or ""),
        "added_at": time.time(),
    }
    by_char[key] = rules
    store["by_char"] = by_char
    save_rules_store(store)
    return {"ok": True, "tid": tid, "name": str(name or "")}


def remove_char_rule(char_id: int | str | None, tid: int) -> dict:
    """Delete one persistent rule by tid. @author by ak"""
    tid = int(tid or 0) & 0xFFFFFFFF
    key = normalize_char_id(char_id)
    if not key:
        return {"ok": False, "error": "invalid char_id"}
    store = load_rules_store()
    by_char = dict(store.get("by_char") or {})
    rules = dict(by_char.get(key) or {})
    removed = rules.pop(str(tid), None) is not None
    by_char[key] = rules
    store["by_char"] = by_char
    save_rules_store(store)
    return {"ok": True, "tid": tid, "removed": removed}


def clear_char_rules(char_id: int | str | None) -> dict:
    """Delete all persistent rules for one role. @author by ak"""
    key = normalize_char_id(char_id)
    if not key:
        return {"ok": False, "error": "invalid char_id"}
    store = load_rules_store()
    by_char = dict(store.get("by_char") or {})
    removed = key in by_char
    by_char[key] = {}
    store["by_char"] = by_char
    save_rules_store(store)
    return {"ok": True, "removed": removed}


__all__ = [
    "load_rules_store",
    "save_rules_store",
    "normalize_char_id",
    "load_char_rules",
    "add_char_rule",
    "remove_char_rule",
    "clear_char_rules",
]
