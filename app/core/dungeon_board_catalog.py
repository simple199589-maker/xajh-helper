# -*- coding: utf-8 -*-
"""Map live dungeon task text onto static client ``BoardCond`` values."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.core.dungeon_stage import normalize_stage_text


DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "dungeon_board_catalog.json"
)


@dataclass(frozen=True)
class BoardLookup:
    instance_id: int = 0
    candidates: tuple[int, ...] = ()

    @property
    def board(self) -> int | None:
        return self.candidates[0] if len(self.candidates) == 1 else None

    @property
    def exact(self) -> bool:
        return self.board is not None


@dataclass(frozen=True)
class BoardResolution:
    board: int | None = None
    evidence: str = "unknown"
    candidates: tuple[int, ...] = ()
    awaiting_next: bool = False


class BoardTimeline:
    """Carry a live BoardCond match across task-complete transitions."""

    def __init__(self) -> None:
        self._scene_id = 0
        self._board: int | None = None
        self._awaiting_next = False

    def reset(self, *, scene_id: int = 0) -> None:
        self._scene_id = int(scene_id or 0)
        self._board = None
        self._awaiting_next = False

    def update(
        self,
        *,
        scene_id: int,
        lookup: BoardLookup,
        event: str | None,
    ) -> BoardResolution:
        scene_id = int(scene_id or 0)
        if self._scene_id and scene_id and self._scene_id != scene_id:
            self.reset(scene_id=scene_id)
        elif scene_id:
            self._scene_id = scene_id

        if event == "instance_expired":
            self.reset(scene_id=scene_id)
            return BoardResolution(candidates=lookup.candidates)
        if event in ("transition_countdown", "stage_completed"):
            self._awaiting_next = True

        exact = lookup.board
        if exact is not None:
            # A unique BoardCond task is direct client-side evidence. Keep the
            # complete flag until a different board task actually appears.
            if self._board is None or exact >= self._board:
                if self._board is not None and exact > self._board:
                    self._awaiting_next = False
                self._board = exact
            return BoardResolution(
                board=self._board,
                evidence="client_task",
                candidates=lookup.candidates,
                awaiting_next=self._awaiting_next,
            )

        if lookup.candidates and self._board is not None:
            threshold = self._board + (1 if self._awaiting_next else 0)
            forward = [value for value in lookup.candidates if value >= threshold]
            if forward:
                resolved = forward[0]
                if resolved > self._board:
                    self._board = resolved
                    self._awaiting_next = False
                return BoardResolution(
                    board=self._board,
                    evidence="timeline",
                    candidates=lookup.candidates,
                    awaiting_next=self._awaiting_next,
                )

        # The task panel briefly exposes no rows while AUI rebuilds after a
        # kill/transition. Keep the last confirmed board during that repaint;
        # an empty lookup is not evidence that the run moved backwards or that
        # the current board became unknown.
        if not lookup.candidates and self._board is not None:
            return BoardResolution(
                board=self._board,
                evidence="cached",
                awaiting_next=self._awaiting_next,
            )

        return BoardResolution(
            candidates=lookup.candidates,
            awaiting_next=self._awaiting_next,
        )


def task_signature(text: str) -> str:
    """Match a Lua task template while retaining its required total count."""
    value = normalize_stage_text(text)
    # UI has e.g. ``(2/3)`` while Lua has ``(%d/3)``. The numerator is live
    # progress; the denominator differentiates otherwise similar BoardCond rows.
    return re.sub(r"(?:%d|\d+)\s*/\s*(\d+)", r"#/\1", value)


def load_catalog(path: Path | str = DEFAULT_CATALOG_PATH) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"scene_to_instance": {}, "instances": {}}
    if not isinstance(data, dict):
        return {"scene_to_instance": {}, "instances": {}}
    return data


def lookup_boards(
    scene_id: int,
    targets: tuple[str, ...] | list[str],
    *,
    catalog: dict | None = None,
) -> BoardLookup:
    data = catalog if catalog is not None else load_catalog()
    instance_id = int((data.get("scene_to_instance") or {}).get(str(int(scene_id))) or 0)
    instance = (data.get("instances") or {}).get(str(instance_id)) or {}
    observed = {task_signature(text) for text in targets if task_signature(text)}
    if not instance_id or not observed:
        return BoardLookup(instance_id=instance_id)

    candidates: list[int] = []
    for raw_board, raw_tasks in (instance.get("boards") or {}).items():
        expected = {task_signature(text) for text in raw_tasks if task_signature(text)}
        if expected and observed.issubset(expected):
            try:
                candidates.append(int(raw_board))
            except (TypeError, ValueError):
                continue
    return BoardLookup(instance_id=instance_id, candidates=tuple(sorted(candidates)))
