# -*- coding: utf-8 -*-
"""Clear dist/<pkg_dir> so PyInstaller can rebuild (no permanent backups).

Target dir defaults to xajh_helper; pass a versioned dir name (or set
XAJH_PKG_DIR) when the default dist folder is locked by a running game so the
build can switch to a fresh folder.

If files are locked (helper running / game loaded staged DLL), rename aside
under dist/_trash_* then best-effort delete. Do not keep xajh_helper_old_*.

@author by ak
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
pkg_dir = str(sys.argv[1] if len(sys.argv) > 1 else "").strip() or str(
    os.environ.get("XAJH_PKG_DIR") or "xajh_helper"
).strip()
dist = root / "dist" / pkg_dir
if not dist.exists():
    print(f"dist/{pkg_dir}: not present")
    raise SystemExit(0)

stamp = time.strftime("%Y%m%d_%H%M%S")


def _rmtree(path: Path) -> bool:
    """Return True if path is gone. @author by ak"""
    try:
        shutil.rmtree(path)
    except OSError as e:
        print(f"rmtree failed: {e}")
    return not path.exists()


if _rmtree(dist):
    print(f"dist/{pkg_dir} removed")
    raise SystemExit(0)

# Locked: move whole tree to trash name so PyInstaller can create a fresh dist.
trash = root / "dist" / f"_trash_{stamp}"
try:
    os.rename(dist, trash)
    print(f"moved locked dist -> {trash.name} (will try delete)")
except OSError as e:
    print(f"rename failed: {e}")
    print("Close xajh_helper.exe and game, then retry.")
    raise SystemExit(1)

if not _rmtree(trash):
    print(
        f"WARNING: locked files remain at {trash} — "
        "close game/helper and delete that folder manually"
    )
# Fresh package target is free either way.
raise SystemExit(0)
