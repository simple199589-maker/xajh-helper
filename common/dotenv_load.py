# -*- coding: utf-8 -*-
"""
Minimal .env loader (no python-dotenv dependency).

- Only fills missing os.environ keys (process env wins).
- Skips frozen packages so secrets are never pulled from shipped layout by accident.
- Supports KEY=value, optional single/double quotes, # comments.

@author by ak
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def _parse_line(line: str) -> tuple[str, str] | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if s.lower().startswith("export "):
        s = s[7:].strip()
    if "=" not in s:
        return None
    key, _, raw = s.partition("=")
    key = key.strip()
    if not key or not all(c.isalnum() or c == "_" for c in key):
        return None
    val = raw.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    else:
        if " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        elif "\t#" in val:
            val = val.split("\t#", 1)[0].rstrip()
    return key, val


def load_dotenv(
    path: Path | str | None = None,
    *,
    override: bool = False,
    only_if_unfrozen: bool = True,
) -> Path | None:
    """
    Load KEY=value pairs into os.environ.

    Returns the path loaded, or None if skipped / missing.

    @author by ak
    """
    if only_if_unfrozen and _is_frozen():
        return None
    if path is None:
        root = Path(__file__).resolve().parents[1]
        path = root / ".env"
    p = Path(path)
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return None
    for line in text.splitlines():
        parsed = _parse_line(line)
        if not parsed:
            continue
        key, val = parsed
        if not override and key in os.environ and str(os.environ.get(key) or "") != "":
            continue
        os.environ[key] = val
    return p


def ensure_dotenv_loaded() -> None:
    """
    Idempotent load of repo-root .env for source/dev runs.

    @author by ak
    """
    if getattr(ensure_dotenv_loaded, "_done", False):
        return
    load_dotenv()
    ensure_dotenv_loaded._done = True  # type: ignore[attr-defined]
