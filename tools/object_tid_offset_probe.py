# -*- coding: utf-8 -*-
"""Object template-id offset probe (read-only).

Samples live NPC objects (ptr + tid pairs via the existing safe pipeline),
then RPM-dumps memory around each pointer to find the object-structure offset
where the template id lives. Run while the game is open and a role is logged
in:

    python tools\\object_tid_offset_probe.py [pid]

@maintainer
"""
from __future__ import annotations

import collections
import struct
import sys

sys.path.insert(0, ".")


def main() -> int:
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    from app.core.plg_objects import list_class_objects
    from app.core.super_loot import open_attach_session

    attach = open_attach_session(pid, log=lambda m: None)
    # list_class_objects enumerates via the injected bridge remote runtime;
    # make sure the bridge session exists (the running helper normally owns
    # it; inject only when absent so this probe works standalone).
    try:
        from app.core.xajh_bridge import ensure_bridge

        br = ensure_bridge(attach.pid, log=lambda m: None, inject_if_needed=True)
        if br is None:
            print("bridge unavailable; open/connect the helper for this role first")
            return 2
        br.close()
    except Exception as e:
        print(f"bridge ensure failed: {e}")
        return 2
    meta: dict = {}
    def _log(m: str) -> None:
        print(f"[plg] {m}")
    rows = list_class_objects(
        attach,
        2,  # CLASS_NPC
        radius=400.0,
        limit=24,
        read_name=True,
        read_tid=True,
        max_inspect=64,
        log=_log,
        scan_meta_out=meta,
    )
    print(f"[plg] scan_meta={meta}")
    # Scene gate may refuse CRT right after bridge attach; retry a few times.
    import time as _time

    for _attempt in range(2, 9):
        if rows:
            break
        _time.sleep(0.6)
        try:
            rows = list_class_objects(
                attach,
                2,  # CLASS_NPC
                radius=400.0,
                limit=24,
                read_name=True,
                read_tid=True,
                max_inspect=64,
                log=_log,
                scan_meta_out=meta,
            )
        except Exception as e:
            print(f"[plg] retry {_attempt} err={e!r}")
            continue
        print(f"[plg] retry {_attempt} rows={len(rows)} meta={meta}")
    samples = [(int(r.ptr), int(r.tid)) for r in rows if r.ptr and r.tid]
    print(f"sampled {len(samples)} npcs: {samples[:6]}")
    if len(samples) < 2:
        print("not enough samples; walk to a busy area and retry")
        return 1

    import pymem

    pm = pymem.Pymem(attach.pid)
    span = 0x200
    votes: dict[int, set[int]] = collections.defaultdict(set)
    hits: dict[int, int] = collections.Counter()
    for ptr, tid in samples:
        try:
            blob = pm.read_bytes(ptr & 0xFFFFFFFF, span)
        except Exception:
            continue
        seen_here = set()
        for off in range(0, span - 3, 4):
            (v,) = struct.unpack_from("<I", blob, off)
            if v == (tid & 0xFFFFFFFF):
                votes[off].add(ptr)
                if off not in seen_here:
                    hits[off] += 1
                    seen_here.add(off)
    print("offset candidates (off: matched_objects):")
    ok = False
    for off, n in sorted(hits.items(), key=lambda kv: -kv[1]):
        mark = " <== stable" if n == len(samples) else ""
        if n == len(samples):
            ok = True
        print(f"  +0x{off:03X}: {n}/{len(samples)}{mark}")
    print("RESULT:", "STABLE OFFSET FOUND" if ok else "no stable offset this round")
    pm.close_process()
    try:
        attach.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
