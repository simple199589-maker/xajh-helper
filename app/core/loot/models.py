"""Automatic-loot models; compatibility-backed during staged extraction."""
from app.core.loot._impl import (
    ScanCache,
    SuperLootConfig,
    SuperLootStepResult,
    SuperLootTarget,
)

__all__ = ["ScanCache", "SuperLootConfig", "SuperLootStepResult", "SuperLootTarget"]
