# -*- coding: utf-8 -*-
"""
Runtime process inspector for local game client (Windows).

Requires: psutil (and optionally pymem for deeper reads).

Usage:
  python memory/proc_inspect.py
  python memory/proc_inspect.py --name xajh.exe
  python memory/proc_inspect.py --pid <PID> --scan-string 张三
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.paths import ISSUE_DIR, find_game_root, read_current_server  # noqa: E402

try:
    import psutil
except ImportError as e:
    raise SystemExit("psutil required: pip install psutil") from e


def find_procs(name: str | None = None) -> list[psutil.Process]:
    """Find game-related processes.

    If ``name`` is given, match process image name exactly (case-insensitive).
    Otherwise only match known client/patcher image names.
    """
    if name:
        want = name.lower()
        if not want.endswith(".exe"):
            want_exe = want + ".exe"
        else:
            want_exe = want
    else:
        want = None
        want_exe = None

    default_names = {"xajh.exe", "patcher.exe", "filesync.exe"}
    out = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            n = (p.info.get("name") or "").lower()
            exe = (p.info.get("exe") or "").lower()
            if want is not None:
                if n == want or n == want_exe or exe.endswith("\\" + want_exe) or exe.endswith("/" + want_exe):
                    out.append(p)
                continue
            if n in default_names or any(n.endswith(x) for x in default_names):
                out.append(p)
            elif exe.replace("/", "\\").find("\\bin\\xajh.exe") >= 0:
                out.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    # dedupe pid
    seen = set()
    uniq = []
    for p in out:
        if p.pid not in seen:
            seen.add(p.pid)
            uniq.append(p)
    return uniq


def proc_snapshot(p: psutil.Process) -> dict:
    info = {
        "pid": p.pid,
        "name": p.name(),
        "exe": None,
        "cwd": None,
        "cmdline": None,
        "create_time": None,
        "username": None,
        "memory": None,
        "connections": [],
        "modules": [],
        "threads": 0,
    }
    try:
        info["exe"] = p.exe()
    except (psutil.AccessDenied, psutil.Error):
        pass
    try:
        info["cwd"] = p.cwd()
    except (psutil.AccessDenied, psutil.Error):
        pass
    try:
        info["cmdline"] = p.cmdline()
    except (psutil.AccessDenied, psutil.Error):
        pass
    try:
        info["create_time"] = p.create_time()
    except (psutil.AccessDenied, psutil.Error):
        pass
    try:
        info["username"] = p.username()
    except (psutil.AccessDenied, psutil.Error):
        pass
    try:
        mi = p.memory_info()
        info["memory"] = {"rss": mi.rss, "vms": mi.vms}
    except (psutil.AccessDenied, psutil.Error):
        pass
    try:
        info["threads"] = p.num_threads()
    except (psutil.AccessDenied, psutil.Error):
        pass
    try:
        for c in p.net_connections(kind="inet"):
            laddr = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else None
            raddr = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else None
            info["connections"].append(
                {
                    "status": c.status,
                    "family": int(c.family),
                    "type": int(c.type),
                    "laddr": laddr,
                    "raddr": raddr,
                }
            )
    except (psutil.AccessDenied, psutil.Error):
        info["connections_error"] = "access denied"
    try:
        # memory_maps may need admin
        maps = p.memory_maps(grouped=False)
        mods = []
        for m in maps:
            path = getattr(m, "path", None) or ""
            if path.lower().endswith((".exe", ".dll")):
                mods.append(
                    {
                        "path": path,
                        "rss": getattr(m, "rss", None),
                        "size": getattr(m, "size", None),
                    }
                )
        # unique by path
        seen = set()
        for m in mods:
            if m["path"] not in seen:
                seen.add(m["path"])
                info["modules"].append(m)
    except (psutil.AccessDenied, psutil.Error) as e:
        info["modules_error"] = str(e)
    return info


def try_string_scan(pid: int, needle: str, max_hits: int = 20) -> list[dict]:
    """Optional pymem unicode/ascii scan."""
    try:
        import pymem
        import pymem.pattern
    except ImportError:
        return [{"error": "pymem not installed"}]

    hits = []
    pm = pymem.Pymem()
    pm.open_process_from_id(pid)
    # ascii and utf-16le
    patterns = [needle.encode("ascii", "ignore"), needle.encode("utf-16le")]
    for pat in patterns:
        if not pat:
            continue
        try:
            addrs = pymem.pattern.pattern_scan_all(pm.process_handle, pat, return_multiple=True)
        except Exception as e:
            hits.append({"pattern": pat.hex(), "error": str(e)})
            continue
        if not addrs:
            continue
        if not isinstance(addrs, list):
            addrs = [addrs]
        for a in addrs[:max_hits]:
            hits.append({"pattern": pat.hex(), "address": hex(a)})
    return hits


def main() -> None:
    ap = argparse.ArgumentParser(description="Inspect local game processes")
    ap.add_argument("--name", default=None, help="process name filter, e.g. xajh.exe")
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--scan-string", default=None, help="optional memory string scan (needs pymem)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    game = None
    try:
        game = str(find_game_root())
        cfg = read_current_server()
    except Exception as e:
        cfg = {"error": str(e)}

    report = {
        "t": time.time(),
        "game_root": game,
        "server_config": cfg,
        "processes": [],
    }

    if args.pid:
        procs = [psutil.Process(args.pid)]
    else:
        procs = find_procs(args.name)

    if not procs:
        print("no matching process. start the game first, or pass --name/--pid")
        print("hint: target client is bin\\xajh.exe")
    for p in procs:
        try:
            snap = proc_snapshot(p)
            if args.scan_string:
                snap["string_scan"] = try_string_scan(p.pid, args.scan_string)
            report["processes"].append(snap)
            print(f"PID {snap['pid']} {snap['name']} exe={snap['exe']}")
            print(f"  mem={snap['memory']} threads={snap['threads']}")
            for c in snap["connections"][:20]:
                print(f"  conn {c['status']:12} {c['laddr']} -> {c['raddr']}")
            print(f"  modules={len(snap['modules'])}")
            for m in snap["modules"][:15]:
                print("   ", m["path"])
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            print("skip", p.pid, e)

    out_dir = Path(args.out) if args.out else (ISSUE_DIR / "runtime")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"proc_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", out)


if __name__ == "__main__":
    main()
