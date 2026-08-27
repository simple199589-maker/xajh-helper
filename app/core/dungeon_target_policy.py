# -*- coding: utf-8 -*-
"""Dungeon card-monster target policy: packaged baseline plus role rules."""
from __future__ import annotations

from app.core.ignore_rules import load_char_rules

# Confirmed client-side auto-target outliers.  Keep these in Python rather
# than runtime/config so every released package carries them for every role.
# Additional targets remain editable through the assistant's per-role list.
PACKAGED_DUNGEON_TARGET_RULES: tuple[tuple[int, str], ...] = (
    (0x13EA5, "北疆疯丐"),
    (0x18A98, "上官霸刀"),
)
PACKAGED_DUNGEON_TARGET_TIDS = frozenset(tid for tid, _name in PACKAGED_DUNGEON_TARGET_RULES)


def configured_dungeon_tids(char_id: int | str | None) -> tuple[int, ...]:
    """Return packaged card-monster TIDs plus this role's dynamic rules.

    The two confirmed baseline rules are deliberately release-bundled.
    Additions and removals in the assistant's ignore UI are applied on the
    next dungeon start without a DLL rebuild.
    """
    tids: set[int] = set(PACKAGED_DUNGEON_TARGET_TIDS)
    try:
        tids.update(int(row.get("tid") or 0) & 0xFFFFFFFF for row in load_char_rules(char_id))
    except Exception:
        pass
    tids.discard(0)
    return tuple(sorted(tids))
__all__ = [
    "PACKAGED_DUNGEON_TARGET_RULES",
    "PACKAGED_DUNGEON_TARGET_TIDS",
    "configured_dungeon_tids",
]
