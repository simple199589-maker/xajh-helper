# -*- coding: utf-8 -*-
"""Disk-backed preferences for the user-provided captcha API key."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def captcha_prefs_path() -> Path:
    from common.paths import ensure_writable_dir

    return ensure_writable_dir("runtime", "config") / "captcha_prefs.json"


def load_captcha_api_key() -> str:
    path = captcha_prefs_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ""
        return str(data.get("captcha_api_key") or "").strip()
    except Exception:
        return ""


def save_captcha_api_key(api_key: str) -> Path:
    key = str(api_key or "").strip()
    path = captcha_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(
            {
                "captcha_api_key": key,
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
