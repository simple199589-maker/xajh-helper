# -*- coding: utf-8 -*-
"""Vtable-level template-id offset probe.

For live NPC samples, resolve obj->vtbl[0x84] (the fn GetObjectTemplateID tail-
jumps into), dump each distinct fn from the on-disk PE, decode its [ecx+disp]
load, and verify obj+disp == tid via RPM.

Run (elevated, game open):  python tools\\object_tid_vtable_probe.py [pid]
@maintainer
"""
from __future__ import annotations

import collections
import struct
import sys

sys.path.insert(0, ".")
PE_PATH = r"D:\WeGameApps\笑傲江湖OL\bin\xajh.exe"
IMAGE_BASE = 0x400000


def rva_to_offset(data: bytes, rva: int) -> int:
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    num_sections = struct.unpack_from("<H", data, e_lfanew + 6)[0]
    opt_size = struct.unpack_from("<H", data, e_lfanew + 20)[0]
    sec0 = e_lfanew + 24 + opt_size
    for i in range(num_sections):
        off = sec0 + 40 * i
        vsize, va, rsize, roff = struct.unpack_from("<IIII", data, off + 8)
        if va <= rva < va + max(vsize, rsize):
            return roff + (rva - va)
    raise ValueError(f"rva {rva:#x} unmapped")


def decode_tid_load(code: bytes) -> tuple[int, int] | None:
    """Return (disp, width) for the first mov/movzx eax|edx,[ecx+disp]."""
    pats = [
        (b"\x8b\x81", 4, 4),   # mov eax,[ecx+disp32]
        (b"\x8b\x41", 1, 4),   # mov eax,[ecx+disp8]
        (b"\x8b\x91", 4, 4),   # mov edx,[ecx+disp32]
        (b"\x8b\x51", 1, 4),   # mov edx,[ecx+disp8]
        (b"\x0f\xb7\x41", 1, 2),  # movzx eax,word
        (b"\x0f\xb6\x41", 1, 1),  # movzx eax,byte
    ]
    for pat, dlen, width in pats:
        i = code.find(pat)
        if i >= 0:
            if dlen == 4:
                disp = struct.unpack_from("<i", code, i + len(pat))[0]
            else:
                disp = struct.unpack_from("<b", code, i + len(pat))[0]
            return disp, width
    return None


def main() -> int:
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    from app.core.plg_objects import list_class_objects
    from app.core.super_loot import open_attach_session
    from app.core.xajh_bridge import ensure_bridge

    attach = open_attach_session(pid, log=lambda m: None)
    br = ensure_bridge(attach.pid, log=lambda m: None, inject_if_needed=True)
    if br is None:
        print("bridge unavailable; connect the helper for this role first")
        return 2
    br.close()

    import time

    rows: list = []
    meta: dict = {}
    for attempt in range(1, 9):
        rows = list_class_objects(
            attach,
            2,
            radius=400.0,
            limit=24,
            read_name=True,
            read_tid=True,
            max_inspect=64,
            log=lambda m: None,
            scan_meta_out=meta,
        )
        if rows:
            break
        print(f"[wait] scene gate settling (attempt {attempt})")
        time.sleep(0.6)
    samples = [(int(r.ptr), int(r.tid)) for r in rows if r.ptr and r.tid]
    print(f"sampled {len(samples)} npcs")
    if len(samples) < 2:
        return 1

    import pymem

    pm = pymem.Pymem(attach.pid)
    pe = open(PE_PATH, "rb").read()

    fn_count: dict[int, int] = collections.Counter()
    fn_disp: dict[int, tuple[int, int] | None] = {}
    for ptr, tid in samples:
        try:
            vtbl = struct.unpack("<I", pm.read_bytes(ptr & 0xFFFFFFFF, 4))[0]
            fn = struct.unpack("<I", pm.read_bytes((vtbl + 0x84) & 0xFFFFFFFF, 4))[0]
        except Exception as e:
            print(f"[skip] ptr={ptr:#x} vtbl read err={e!r}")
            continue
        fn_count[fn] += 1
        if fn not in fn_disp:
            rva = fn - IMAGE_BASE
            try:
                off = rva_to_offset(pe, rva)
            except ValueError as e:
                print(f"[skip] fn={fn:#x} {e}")
                fn_disp[fn] = None
                continue
            code = pe[off:off + 80]
            fn_disp[fn] = decode_tid_load(code)
    print(f"distinct vtbl[0x84] fns: {len(fn_count)}")
    for fn, n in fn_count.most_common():
        info = fn_disp.get(fn)
        rva = fn - IMAGE_BASE
        if info is None:
            print(f"  fn={fn:#x} rva={rva:#x} objs={n}: decode failed")
            continue
        disp, width = info
        print(f"  fn={fn:#x} rva={rva:#x} objs={n}: obj+{disp:+#x} width={width}")

    # verify across all samples using each fn's own disp
    print("\nRPM verify obj+disp == tid:")
    good = bad = 0
    per_fn_ok: dict[int, int] = collections.Counter()
    for ptr, tid in samples:
        try:
            vtbl = struct.unpack("<I", pm.read_bytes(ptr & 0xFFFFFFFF, 4))[0]
            fn = struct.unpack("<I", pm.read_bytes((vtbl + 0x84) & 0xFFFFFFFF, 4))[0]
            info = fn_disp.get(fn)
            if not info:
                continue
            disp, width = info
            raw = pm.read_bytes((ptr + disp) & 0xFFFFFFFF, width)
            val = int.from_bytes(raw, "little")
            if width == 4:
                ok = val == (tid & 0xFFFFFFFF)
            else:
                ok = val == (tid & 0xFFFF) or val == (tid & 0xFF)
            if ok:
                good += 1
                per_fn_ok[fn] += 1
            else:
                bad += 1
                if bad <= 5:
                    print(f"  MISMATCH ptr={ptr:#x} tid={tid} read={val}")
        except Exception as e:
            print(f"  ERR ptr={ptr:#x}: {e!r}")
    print(f"verified ok={good} bad={bad}")
    pm.close_process()
    try:
        attach.close()
    except Exception:
        pass
    return 0 if good and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
