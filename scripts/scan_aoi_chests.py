# -*- coding: utf-8 -*-
"""
Standalone scanner: enumerate all matter objects (class-1) in the game AOI
and print their coordinates, names, TIDs, distances, and coord_skip_key.

Usage:
  python scripts/scan_aoi_chests.py
  python scripts/scan_aoi_chests.py --pid 12345
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.game_attach import GameAttachSession
from app.core.plg_objects import (
    CLASS_MATTER,
    get_object_count,
    get_object_ptrs,
    list_class_objects,
    read_object_pos,
    read_object_template_id,
)
from app.core.loot._impl import _coord_skip_key
from app.core.remote_runtime import wait_pid_scene_stable
from app.core.teleport import read_host_pos


def _find_xajh_pid() -> int | None:
    """Find the first xajh.exe process PID. Returns None if not found."""
    try:
        import psutil
    except ImportError:
        raise SystemExit("psutil required: pip install psutil")

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == "xajh.exe":
                return int(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def main():
    parser = argparse.ArgumentParser(description="Scan AOI matter objects (class-1)")
    parser.add_argument("--pid", type=int, default=0, help="Target game PID")
    args = parser.parse_args()

    pid = args.pid or _find_xajh_pid()
    if not pid:
        print("ERROR: xajh.exe not found. Specify --pid or launch the game first.")
        return 1

    print(f"Attaching to xajh.exe pid={pid} ...")
    session = GameAttachSession()
    try:
        session.attach(pid)
    except Exception as e:
        print(f"ERROR: attach failed: {e}")
        return 1

    if session.pm is None:
        print("ERROR: attach succeeded but pm is None")
        session.close()
        return 1

    # Wait for scene to stabilize (required by CRT gate)
    print("Waiting for scene stable ...")
    try:
        wait_pid_scene_stable(pid, timeout_s=5.0)
    except Exception as e:
        print(f"WARNING: scene not stable: {e}")
    else:
        print("Scene stable, proceeding.\n")

    # --- host position ---
    host_info = read_host_pos(session)
    host_pos: tuple[float, float, float] | None = None
    if host_info.get("ok"):
        host_pos = host_info["pos"]
        print(f"Host player position: ({host_pos[0]:.3f}, {host_pos[1]:.3f}, {host_pos[2]:.3f})")
    else:
        print(f"WARNING: host pos unavailable: {host_info.get('error', 'unknown')}")

    # --- diagnostics: raw count + ptrs + TID check ---
    print("\n--- diagnostic ---")
    module_base = getattr(session, "module_base", 0) or 0
    print(f"module_base=0x{module_base:08X}")
    try:
        raw_count = get_object_count(session, CLASS_MATTER)
        print(f"GetObjectCount(class=1) = {raw_count}")
    except Exception as e:
        print(f"GetObjectCount FAIL: {e}")
        raw_count = -1

    if raw_count > 0:
        try:
            ptrs = get_object_ptrs(session, CLASS_MATTER, capacity=min(raw_count + 4, 512))
            print(f"GetObjects returned {len(ptrs)} ptrs")
        except Exception as e:
            print(f"GetObjects FAIL: {e}")
            ptrs = []

        if ptrs:
            # read first 5 object positions
            for i, p in enumerate(ptrs[:5]):
                pos = read_object_pos(session.pm, p)
                tid = read_object_template_id(session, p, class_id=CLASS_MATTER)
                print(f"  ptr 0x{p:08X} pos={pos} tid={tid}")
            # count how many pass TID check
            tid_ok = 0
            for p in ptrs[:50]:
                tid = read_object_template_id(session, p, class_id=CLASS_MATTER)
                if tid is not None:
                    tid_ok += 1
            print(f"TID check ok: {tid_ok}/{min(len(ptrs), 50)}")
    print("--- diagnostic end ---\n")

    # --- enumerate class-1 (matter) objects ---
    print("\nEnumerating class-1 (matter) objects ...")
    try:
        objects = list_class_objects(
            session,
            CLASS_MATTER,
            host_pos=host_pos,
            read_name=True,
            read_tid=True,
            limit=9999,
            max_inspect=9999,
        )
    except Exception as e:
        print(f"ERROR: list_class_objects failed: {e}")
        session.close()
        return 1

    print(f"Found {len(objects)} matter objects.\n")

    if not objects:
        print("No matter objects found in AOI.")
        session.close()
        return 0

    # --- print header ---
    header = (
        f"{'#':>4s}  "
        f"{'Address':>10s}  "
        f"{'Name':<30s}  "
        f"{'TID':>8s}  "
        f"{'ObjID':>10s}  "
        f"{'X':>10s}  "
        f"{'Y':>10s}  "
        f"{'Z':>10s}  "
        f"{'Dist':>10s}  "
        f"{'CoordKey':>10s}"
    )
    print(header)
    print("-" * len(header))

    for i, obj in enumerate(objects, 1):
        addr_str = f"0x{obj.ptr:08X}" if obj.ptr else "N/A"
        name = obj.name or "(no name)"
        tid = f"{obj.tid}" if obj.tid is not None else "N/A"
        oid = f"{obj.obj_id}" if obj.obj_id is not None else "N/A"
        x = f"{obj.x:.3f}" if obj.x is not None else "N/A"
        y = f"{obj.y:.3f}" if obj.y is not None else "N/A"
        z = f"{obj.z:.3f}" if obj.z is not None else "N/A"
        dist = f"{obj.dist:.1f}" if obj.dist is not None else "N/A"

        ck = "N/A"
        if obj.x is not None and obj.z is not None:
            ck = f"0x{_coord_skip_key(float(obj.x), float(obj.z)):08X}"

        print(
            f"{i:>4d}  "
            f"{addr_str:>10s}  "
            f"{name:<30s}  "
            f"{tid:>8s}  "
            f"{oid:>10s}  "
            f"{x:>10s}  "
            f"{y:>10s}  "
            f"{z:>10s}  "
            f"{dist:>10s}  "
            f"{ck:>10s}"
        )

    session.close()
    print(f"\nDone. {len(objects)} matter objects scanned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())