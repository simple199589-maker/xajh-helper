# -*- coding: utf-8 -*-
"""Disk-backed user preferences for the Yaolu page."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


YAOLU_RISK_CONSTRAINT_DEFAULT = 80


def yaolu_prefs_path() -> Path:
    from common.paths import ensure_writable_dir

    return ensure_writable_dir("runtime", "config") / "yaolu_prefs.json"


def normalize_yaolu_risk_constraint(
    value,
    default: int = YAOLU_RISK_CONSTRAINT_DEFAULT,
) -> int:
    """Return a stable 1..100 integer percentage."""
    try:
        pct = int(float(str(value).strip().rstrip("%")))
    except (TypeError, ValueError):
        pct = int(default)
    return max(1, min(100, pct))


def load_yaolu_risk_constraint(default: int = YAOLU_RISK_CONSTRAINT_DEFAULT) -> int:
    path = yaolu_prefs_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return normalize_yaolu_risk_constraint(default)
        return normalize_yaolu_risk_constraint(
            data.get("risk_constraint_pct"), default=default
        )
    except Exception:
        return normalize_yaolu_risk_constraint(default)


def save_yaolu_risk_constraint(value) -> Path:
    pct = normalize_yaolu_risk_constraint(value)
    path = yaolu_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(
            {
                "risk_constraint_pct": pct,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temp.replace(path)
    return path
