# -*- coding: utf-8 -*-
"""Single source of truth for the native bridge binary inventory.

Both the source-run bootstrap and the PyInstaller package build must satisfy
this list, so a missing / stale / wrong-architecture DLL or EXE is caught in one
place instead of surfacing as a cryptic "差 dll" during packaging.

Do not hardcode native\\bin file names anywhere else; import from here (or use
tools\\check_native_bin.py for a one-shot directory check).

@author by ak
"""
from __future__ import annotations

# Fixed-name artifacts that must exist in a native bin directory. All loaders
# and the build pipeline use the plain `xajh_bridge.dll` name (no versioned
# stamp — 2026-08-30 规范化).
NATIVE_BIN_FILES: tuple[str, ...] = (
    "xajh_bridge.dll",
    "xajh_inject.exe",
    "xajh_chat_tap.dll",
    "xajh_team_tap.dll",
    "xajh_login_bridge_v2.dll",
    "xajh_login_inject.exe",
    "dummy_damage_reader.exe",
)


def required_names() -> tuple[str, ...]:
    """Every native file that must be present in a bin directory."""
    return NATIVE_BIN_FILES