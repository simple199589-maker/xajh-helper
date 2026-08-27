# -*- coding: utf-8 -*-
from __future__ import annotations
import sys

sys.path.insert(0, ".")


def main() -> int:
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    from app.core.grocery_auto import open_attach_session
    from app.core.plg_objects import (
        CLASS_NPC,
        get_object_count,
        get_object_ptrs,
        read_object_template_id,
        read_object_pos,
    )

    attach = open_attach_session(pid, log=lambda m: print(m))
    import time
    time.sleep(1.5)
    try:
        count = get_object_count(attach, CLASS_NPC)
        ptrs = get_object_ptrs(attach, CLASS_NPC, capacity=min(max(count + 4, 8), 512))
        rows = []
        for ptr in ptrs:
            tid = read_object_template_id(attach, ptr)
            pos = read_object_pos(attach.pm, ptr)
            if tid or pos:
                rows.append((ptr, tid, pos))
        print(f"pid={pid} base=0x{attach.module_base:X} count={count} ptrs={len(ptrs)} readable={len(rows)}")
        for ptr, tid, pos in rows[:32]:
            print(f"ptr=0x{ptr:X} tid={tid} pos={pos}")
        return 0 if rows else 1
    finally:
        attach.close()


if __name__ == "__main__":
    raise SystemExit(main())
