"""Stable automatic-loot API grouped by responsibility."""
from app.core.loot.actions import (
    interact_target,
    move_to_target,
    open_specific_target,
    open_target,
    pick_ground_target,
    super_loot_step,
)
from app.core.loot.models import (
    ScanCache,
    SuperLootConfig,
    SuperLootStepResult,
    SuperLootTarget,
)
from app.core.loot.policy import (
    filter_reachable_hits,
    find_named_targets,
    mark_unreachable,
    select_loot_target,
    target_skip_key,
    target_skip_keys,
)
from app.core.loot.runner import SuperLootRunner
from app.core.loot._impl import (
    DEFAULT_CAST_WAIT_S,
    DEFAULT_ITEM_NAME,
    DEFAULT_PICK_RANGE,
    DEFAULT_SCAN_RADIUS,
    MODE_OPEN,
    open_attach_session,
    read_host_cast_state,
)

__all__ = [
    "ScanCache",
    "DEFAULT_CAST_WAIT_S",
    "DEFAULT_ITEM_NAME",
    "DEFAULT_PICK_RANGE",
    "DEFAULT_SCAN_RADIUS",
    "MODE_OPEN",
    "SuperLootConfig",
    "SuperLootRunner",
    "SuperLootStepResult",
    "SuperLootTarget",
    "filter_reachable_hits",
    "find_named_targets",
    "interact_target",
    "mark_unreachable",
    "move_to_target",
    "open_specific_target",
    "open_attach_session",
    "read_host_cast_state",
    "open_target",
    "pick_ground_target",
    "select_loot_target",
    "super_loot_step",
    "target_skip_key",
    "target_skip_keys",
]
