# -*- coding: utf-8 -*-
"""Matter (class-1) template-id offset probe (read-only).

Goal: find where a matter/ground/chest object stores its template id, so
plg_objects.read_object_template_id can read it via pure RPM (like NPCs) and
drop the remote GetObjectTemplateID CRT call.

Two independent detectors, both read-only:
  1) Raw memory scan: dump obj..obj+span and find the offset whose u32 equals
     the ground-truth TID across every sampled matter object.
  2) Vtable getter scan: for each vtable slot, decode the function's first
     [ecx+disp] load and check obj+disp == TID for all samples. This yields the
     slot index + getter RVA needed for a vtable-verified RPM read.

Ground-truth TIDs are read once via the existing remote pipeline (calibration
only); production code never needs them again.

Run (game open, chests/matter in AOI, role logged in):
    python tools\\object_tid_matter_probe.py [pid]

@maintainer
"""
from __future__ import annotations

import collections
import struct
import sys

sys.path.insert(0, ".")

PE_PATH = r"D:\WeGameApps\笑傲江湖OL\bin\xajh.exe"
IMAGE_BASE = 0x400000
RAW_SPAN = 0x300          # matter struct can be larger than NPC's 0x4F8
VTBLS_MAX_SLOTS = 0x100   # scan 256 vtable slots (1024 bytes)
CODE_DUMP = 64

# (opcode, disp_bytes, width_bytes): first mov/movzx eax|edx, [ecx+disp]
ECX_LOAD_PATTERNS = [
    (b"\x8b\x81", 4, 4),   # mov eax,[ecx+disp32]
    (b"\x8b\x41", 1, 4),   # mov eax,[ecx+disp8]
    (b"\x8b\x91", 4, 4),   # mov edx,[ecx+disp32]
    (b"\x8b\x51", 1, 4),   # mov edx,[ecx+disp8]
    (b"\x0f\xb7\x41", 1, 2),  # movzx eax,word [ecx+disp8]
    (b"\x0f\xb6\x41", 1, 1),  # movzx eax,byte [ecx+disp8]
]


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


def image_size(data: bytes) -> int:
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    opt = e_lfanew + 4 + 20  # PE signature (4) + COFF header (20)
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic not in (0x10B, 0x20B):  # PE32 / PE32+
        raise ValueError(f"bad optional-header magic {magic:#x}")
    return struct.unpack_from("<I", data, opt + 56)[0]  # SizeOfImage


def decode_first_ecx_load(code: bytes) -> tuple[int, int] | None:
    """Return (disp, width_bytes) for the first mov/movzx eax|edx,[ecx+disp]."""
    for pat, dlen, width in ECX_LOAD_PATTERNS:
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
    from app.core.plg_objects import CLASS_MATTER, list_class_objects
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
            CLASS_MATTER,
            radius=None,
            limit=64,
            read_name=False,
            read_tid=True,          # ground truth (remote); calibration only
            max_inspect=128,
            log=lambda m: None,
            scan_meta_out=meta,
        )
        if rows:
            break
        print(f"[wait] scene gate settling (attempt {attempt})")
        time.sleep(0.6)
    samples = [(int(r.ptr), int(r.tid)) for r in rows if r.ptr and r.tid]
    print(f"sampled {len(samples)} matter objects, meta={meta}")
    print("samples (ptr, tid):", samples[:10])
    if len(samples) < 2:
        print("not enough matter with TID; walk near chests/ground loot and retry")
        return 1

    import pymem

    pm = pymem.Pymem(attach.pid)
    pe = open(PE_PATH, "rb").read()
    img_size = image_size(pe)

    # --- detector 1: raw memory scan ---------------------------------------
    votes: dict[int, set[int]] = collections.defaultdict(set)
    hits: dict[int, int] = collections.Counter()
    for ptr, tid in samples:
        try:
            blob = pm.read_bytes(ptr & 0xFFFFFFFF, RAW_SPAN)
        except Exception:
            continue
        seen = set()
        for off in range(0, len(blob) - 3, 4):
            (v,) = struct.unpack_from("<I", blob, off)
            if v == (tid & 0xFFFFFFFF):
                votes[off].add(ptr)
                if off not in seen:
                    hits[off] += 1
                    seen.add(off)
    print("\n[raw scan] offset candidates (off: matched_objects):")
    for off, n in sorted(hits.items(), key=lambda kv: -kv[1]):
        mark = " <== stable" if n == len(samples) else ""
        print(f"  +0x{off:03X}: {n}/{len(samples)}{mark}")

    # --- detector 2: vtable getter scan ------------------------------------
    print(f"\n[vtable] scanning up to {VTBLS_MAX_SLOTS} slots per object...")
    slot_votes: dict[int, list[tuple[int, int, int]]] = collections.defaultdict(list)
    # slot_votes[slot] -> list of (getter_va, disp, width) candidates
    for ptr, tid in samples:
        try:
            vtbl = struct.unpack("<I", pm.read_bytes(ptr & 0xFFFFFFFF, 4))[0]
        except Exception as e:
            print(f"[skip] ptr={ptr:#x} vtbl err={e!r}")
            continue
        for slot in range(VTBLS_MAX_SLOTS):
            try:
                fn = struct.unpack(
                    "<I", pm.read_bytes((vtbl + slot * 4) & 0xFFFFFFFF, 4)
                )[0]
            except Exception:
                break
            if not (IMAGE_BASE <= fn < IMAGE_BASE + img_size):
                continue
            rva = fn - IMAGE_BASE
            try:
                off = rva_to_offset(pe, rva)
            except ValueError:
                continue
            code = pe[off:off + CODE_DUMP]
            dec = decode_first_ecx_load(code)
            if dec is None:
                continue
            disp, width = dec
            try:
                raw = pm.read_bytes((ptr + disp) & 0xFFFFFFFF, width)
                val = int.from_bytes(raw, "little")
            except Exception:
                continue
            if width == 4:
                ok = val == (tid & 0xFFFFFFFF)
            else:
                ok = val == (tid & 0xFFFF) or val == (tid & 0xFF)
            if ok:
                slot_votes[slot].append((fn, disp, width))

    print("\n[vtable] getter candidates (slot -> matches):")
    for slot in sorted(slot_votes):
        cands = slot_votes[slot]
        # count distinct getter RVA + disp pairs
        pairs = collections.Counter((fn, disp, width) for fn, disp, width in cands)
        best = pairs.most_common(1)[0]
        (fn, disp, width), n = best
        rva = fn - IMAGE_BASE
        full = " <== stable" if n == len(samples) else ""
        print(
            f"  vtbl[+0x{slot*4:03X}] getter_rva=0x{rva:06X} "
            f"obj{disp:+#x} width={width} bytes  {n}/{len(samples)}{full}"
        )

    pm.close_process()
    try:
        attach.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())