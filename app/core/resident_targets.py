# -*- coding: utf-8 -*-
from __future__ import annotations

import math

from app.core.remote_runtime import ensure_pid_scene_stable
from app.core.resident_reader import (
    ResidentReader,
    read_current_position,
    read_scene_feature_rows,
)


def read_resident_targets(
    pid: int,
    scene_id: int,
    *,
    target_tid: int = 0,
    log=None,
) -> list[dict]:
    """Read live matching resident rows sorted by current world distance."""
    logger = log or (lambda _message: None)
    reader = None
    try:
        ensure_pid_scene_stable(int(pid), settle_sec=0.0)
        logger(f"resident target read start scene={int(scene_id or 0)} tid={int(target_tid or 0)}")
        reader = ResidentReader(int(pid), scene_id=int(scene_id) if scene_id else None, scan_timeout_s=12.0)
        try:
            result = reader.locate()
            if result is None and scene_id:
                result = read_scene_feature_rows(reader, int(scene_id))
        finally:
            reader.close()
        if not result:
            logger("resident target read empty")
            return []
        _container, rows = result
        wanted = int(target_tid or 0)
        candidates = [dict(row) for row in (rows or []) if not wanted or int(row.get("tid") or 0) == wanted]
        valid = []
        for row in candidates:
            try:
                coords = (
                    float(row.get("world_x", row.get("x"))),
                    float(row.get("world_y", row.get("y"))),
                    float(row.get("world_z", row.get("z"))),
                )
            except (TypeError, ValueError):
                continue
            if all(math.isfinite(value) for value in coords):
                valid.append(row)
        candidates = valid
        if not candidates:
            return []
        origin = read_current_position(int(pid))
        if origin is not None:
            ox, oy, oz = origin
            for row in candidates:
                dx = float(row.get("world_x", row.get("x", 0))) - ox
                dy = float(row.get("world_y", row.get("y", 0))) - oy
                dz = float(row.get("world_z", row.get("z", 0))) - oz
                row["distance"] = math.sqrt(dx * dx + dy * dy + dz * dz)
            candidates.sort(key=lambda row: (row["distance"], int(row.get("tid") or 0)))
        logger(
            f"resident targets count={len(candidates)}"
            + (f" nearest_tid={candidates[0].get('tid')} distance={candidates[0].get('distance', 'n/a')}" if candidates else "")
        )
        return candidates
    except Exception as exc:
        logger(f"resident target read failed: {exc}")
        return []


def read_nearest_resident_target(pid: int, scene_id: int, *, target_tid: int = 0, log=None) -> dict | None:
    """Reuse the resident reader and select the nearest live matching row."""
    rows = read_resident_targets(pid, scene_id, target_tid=target_tid, log=log)
    return rows[0] if rows else None


__all__ = ["read_resident_targets", "read_nearest_resident_target"]
