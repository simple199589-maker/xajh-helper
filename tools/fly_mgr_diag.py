# -*- coding: utf-8 -*-
"""Fly manager pointer chain diagnostic (read-only).

Run while the game is running and a role is logged in:

    python tools\\fly_mgr_diag.py [pid]

Walks the exact GetHostData chain used by map_fly.get_fly_manager_ptr and
prints every stage so a broken link is visible immediately:

    base = module base of xajh.exe
    g    = *(base + NOTE_VA_HOST_ROOT_GLOBAL - IMAGE_BASE)   # global root
    mid  = *(g   + 0x24)
    host = *(mid + 0x90)
    mgr  = *(host + 0x40)                                    # fly manager

When mgr resolves it also walks mgr+0x16C custom-page hash map and prints the
loaded page keys (mirrors map_fly._iter_fly_page_map semantics).

Exit codes: 0 chain ok · 1..5 stage broken (see script) · 6 no process found.
Never writes to or remote-calls the game process.

@author by ak
"""
from __future__ import annotations

import struct
import sys

import pymem
import pymem.process

IMAGE_BASE = 0x00400000
NOTE_VA_HOST_ROOT_GLOBAL = 0x0152_82D8  # same constant as app/core/map_fly.py
FLY_MGR_PAGE_MAP_OFF = 0x16C
# xajh.exe is LARGE_ADDRESS_AWARE: valid heap pointers can exceed 2 GB.
FLY_PTR_MAX = 0xFFFF0000


def _find_xajh_pids() -> list[int]:
    pids: list[int] = []
    try:
        rows = pymem.process.list_processes()
    except Exception:
        return pids
    for row in rows:
        name = str(getattr(row, "name", "") or "")
        if name.lower() != "xajh.exe":
            continue
        pids.append(int(row.pid))
    return pids


def _u32(pm: pymem.Pymem, addr: int) -> int:
    return struct.unpack("<I", pm.read_bytes(addr & 0xFFFFFFFF, 4))[0]


def _hx(v: int) -> str:
    return hex(v) if v else "0(null)"


def _ok_ptr(v: int) -> bool:
    return 0x10000 <= v < FLY_PTR_MAX


def diagnose(pid: int) -> int:
    print(f"==== fly_mgr diag pid={pid} ====")
    pm = pymem.Pymem(pid)
    try:
        base = None
        mod = pymem.process.module_from_name(pm.process_handle, "xajh.exe")
        base = int(mod.lpBaseOfDll) if mod else None
        if not base:
            print("[fail] xajh.exe module not found")
            return 1
        print(f"[stage0] base = {hex(base)}")

        def rd(addr: int) -> int:
            return _u32(pm, addr)

        # -- stage1: global root
        g_va = base + (NOTE_VA_HOST_ROOT_GLOBAL - IMAGE_BASE)
        try:
            g = rd(g_va)
        except Exception as e:
            print(f"[stage1 fail] read global @ {hex(g_va)} error={e!r}")
            return 1
        print(f"[stage1] g    = *{hex(g_va)}      -> {_hx(g)}")
        if not _ok_ptr(g):
            print("[stage1 fail] global root null/out-of-range")
            return 1

        # -- stage2: GetHostData first hop root+0x24
        try:
            mid = rd((g + 0x24))
        except Exception as e:
            print(f"[stage2 fail] read g+0x24 error={e!r}")
            return 2
        print(f"[stage2] mid  = *(g+0x24)        -> {_hx(mid)}")
        if not _ok_ptr(mid):
            print("[stage2 fail] host-side hop empty -> GetHostData() would return null")
            return 2

        # -- stage3: second hop mid+0x90
        try:
            host = rd((mid + 0x90))
        except Exception as e:
            print(f"[stage3 fail] read mid+0x90 error={e!r}")
            return 3
        print(f"[stage3] host = *(mid+0x90)      -> {_hx(host)}")
        if not _ok_ptr(host):
            print(
                "[stage3 fail] GetHostData result invalid on this client build"
            )
            return 3

        # -- stage4: fly manager at host+0x40
        try:
            mgr = rd((host + 0x40))
        except Exception as e:
            print(f"[stage4 fail] read host+0x40 error={e!r}")
            return 4
        print(f"[stage4] mgr  = *(host+0x40)     -> {_hx(mgr)}")
        if not _ok_ptr(mgr):
            print("[stage4 fail] fly manager null/out-of-range")
            return 4
        if mgr >= 0x7FFF0000:
            print("[info] mgr in LAA window (>2GB) — normal for this client")

        # -- stage5: custom page hash map walk at mgr+0x16C
        map_ptr = (mgr + FLY_MGR_PAGE_MAP_OFF) & 0xFFFFFFFF
        try:
            b = rd(map_ptr + 0x14)
            e = rd(map_ptr + 0x18)
            layout = "+0x14/+0x18"
            if not b or not e or e <= b or (e - b) > 0x10000:
                b = rd(map_ptr + 0x10)
                e = rd(map_ptr + 0x14)
                layout = "+0x10/+0x14"
            print(f"[stage5] page map {layout}: begin={_hx(b)} end={_hx(e)}")
            if not b or not e or e <= b or (e - b) > 0x10000 or ((e - b) % 8):
                print("[stage5 fail] bucket array implausible")
                return 5
            n = min((e - b) // 8, 512)
            seen: set[int] = set()
            pages: dict[int, dict[str, object]] = {}
            for i in range(n):
                node = rd(b + i * 8 + 4)
                steps = 0
                while node and node not in seen and steps < 128:
                    seen.add(node)
                    key = rd(node + 8)
                    obj = rd(node + 0xC)
                    if _ok_ptr(obj) and (key & 0xFF) <= 32:
                        k = key & 0xFF
                        if k not in pages:
                            head = pm.read_bytes(obj & 0xFFFFFFFF, 2)
                            slots = []
                            for s in range(10):
                                sp = obj + 0x08 + 0x14 * s
                                sname = rd(sp + 0x10)
                                slots.append(
                                    f"s{s}:ptr={sname:08X}"
                                    if sname
                                    else f"s{s}:empty"
                                )
                            pages[k] = {
                                "obj_pid": int(head[0]) if head else 0,
                                "slots": slots,
                            }
                    node = rd(node)
                    steps += 1
            print(f"[stage5] nodes={len(seen)} keys={sorted(pages)}")
            for k in sorted(pages):
                info = pages[k]
                slots = ", ".join(str(x) for x in info["slots"])  # type: ignore[arg-type]
                print(
                    f"         page{k} obj_pid={info['obj_pid']} slots=[{slots}]"
                )
            if not pages:
                print(
                    "[warn] map walkable but no custom pages loaded "
                    "(role has no custom fly pages yet?)"
                )
        except Exception as exc:
            print(f"[stage5 fail] page map walk error={exc!r}")
            return 5

        print("RESULT: fly chain fully resolved — dropdown data should work")
        return 0
    finally:
        pm.close_process()


def main() -> int:
    targets = [int(sys.argv[1])] if len(sys.argv) > 1 else _find_xajh_pids()
    if not targets:
        print("no xajh.exe process found — start the game / log in first")
        return 6
    worst = 0
    for pid in targets:
        rc = diagnose(pid)
        worst = max(worst, rc)
        print()
    return worst


if __name__ == "__main__":
    sys.exit(main())
