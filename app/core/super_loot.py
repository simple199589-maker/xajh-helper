# -*- coding: utf-8 -*-
"""Compatibility facade for the responsibility-grouped app.core.loot API."""
from app.core.loot._impl import *  # noqa: F401,F403

# Preserve every historical non-private import while new code uses app.core.loot.
__all__ = sorted(name for name in globals() if not name.startswith("_"))
