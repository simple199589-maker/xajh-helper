# -*- coding: utf-8 -*-
"""Persistent task-sequence profiles for dungeon stage numbering."""
from __future__ import annotations

import json
from pathlib import Path

from app.core.dungeon_stage import normalize_stage_text, stage_signature


DEFAULT_PROFILE_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "dungeon_stage_profiles.json"
)


def empty_profiles() -> dict:
    return {"version": 1, "profiles": {}}


def load_profiles(path: Path | str = DEFAULT_PROFILE_PATH) -> dict:
    source = Path(path)
    if not source.is_file():
        return empty_profiles()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return empty_profiles()
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        return empty_profiles()
    data.setdefault("version", 1)
    return data


def save_profiles(data: dict, path: Path | str = DEFAULT_PROFILE_PATH) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def reset_profile(
    data: dict,
    *,
    scene_id: int,
    name: str = "",
    complete_from_entry: bool = True,
) -> dict:
    profiles = data.setdefault("profiles", {})
    profile = {
        "scene_id": int(scene_id),
        "name": str(name or "").strip(),
        "complete_from_entry": bool(complete_from_entry),
        "stages": [],
    }
    profiles[str(int(scene_id))] = profile
    return profile


def append_stage(
    profile: dict,
    targets: tuple[str, ...] | list[str],
    *,
    force: bool = False,
) -> int:
    clean = [normalize_stage_text(text) for text in targets]
    clean = [text for text in clean if text]
    signature = stage_signature(tuple(clean))
    if not signature:
        return 0
    stages = profile.setdefault("stages", [])
    if (
        not force
        and stages
        and str(stages[-1].get("signature") or "") == signature
    ):
        return int(stages[-1].get("stage") or len(stages))
    stage_no = len(stages) + 1
    stages.append(
        {
            "stage": stage_no,
            "signature": signature,
            "targets": clean,
        }
    )
    return stage_no


def get_profile(data: dict, scene_id: int) -> dict | None:
    profile = (data.get("profiles") or {}).get(str(int(scene_id)))
    return profile if isinstance(profile, dict) else None


def match_stage(
    data: dict,
    *,
    scene_id: int,
    targets: tuple[str, ...] | list[str],
    previous_stage: int | None = None,
) -> tuple[int | None, bool]:
    profile = get_profile(data, scene_id)
    signature = stage_signature(tuple(targets))
    if not profile or not signature:
        return None, False
    matches = [
        int(row.get("stage") or 0)
        for row in profile.get("stages", [])
        if isinstance(row, dict) and str(row.get("signature") or "") == signature
    ]
    matches = [stage for stage in matches if stage > 0]
    if not matches:
        return None, bool(profile.get("complete_from_entry"))
    if previous_stage is None:
        if len(matches) > 1:
            return None, bool(profile.get("complete_from_entry"))
        return matches[0], bool(profile.get("complete_from_entry"))
    forward = [stage for stage in matches if stage >= int(previous_stage)]
    return (
        forward[0] if forward else matches[-1],
        bool(profile.get("complete_from_entry")),
    )
