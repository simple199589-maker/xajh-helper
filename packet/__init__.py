# -*- coding: utf-8 -*-
"""Packet capture / split / auto-analyze package. @author by ak"""
from __future__ import annotations

__all__ = [
    "analyze_latest",
    "analyze_paths",
    "core_points_markdown",
    "write_recipes",
]

def __getattr__(name: str):
    if name in ("analyze_latest", "analyze_paths"):
        from packet.auto_analyze import analyze_latest, analyze_paths
        return {"analyze_latest": analyze_latest, "analyze_paths": analyze_paths}[name]
    if name == "core_points_markdown":
        from packet.crypto_knowledge import core_points_markdown
        return core_points_markdown
    if name == "write_recipes":
        from packet.x32dbg_recipes import write_recipes
        return write_recipes
    raise AttributeError(name)
