# -*- coding: utf-8 -*-
"""Dump plg::GetObjectTemplateID (RVA 0x827260) bytes from the on-disk PE.

Read-only local file analysis; prints a hex dump plus naive x86 mov decoding
so the template-id source offset can be read directly from the code.

@maintainer
"""
from __future__ import annotations

import struct
import sys

PE_PATH = r"D:\WeGameApps\笑傲江湖OL\bin\xajh.exe"
RVA = 0x427260  # live VA 0x827260 - image base 0x400000
SIZE = 96

MOV_PATTERNS = {
    # opcode bytes -> (reg, base, size, width)
    b"\x8b\x41": ("eax", "ecx", 4, 32),   # mov eax, [ecx+disp8]
    b"\x8b\x81": ("eax", "ecx", 4, 32),   # mov eax, [ecx+disp32]
    b"\x0f\xb7\x41": ("eax", "ecx", 2, 16),  # movzx eax, word [ecx+disp8]
    b"\x0f\xb6\x41": ("eax", "ecx", 1, 8),   # movzx eax, byte [ecx+disp8]
    b"\x8b\x51": ("edx", "ecx", 4, 32),   # mov edx, [ecx+disp8]
    b"\x8b\x91": ("edx", "ecx", 4, 32),   # mov edx, [ecx+disp32]
}


def rva_to_offset(data: bytes, rva: int) -> int:
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    num_sections = struct.unpack_from("<H", data, e_lfanew + 6)[0]
    opt_size = struct.unpack_from("<H", data, e_lfanew + 20)[0]
    sec0 = e_lfanew + 24 + opt_size
    for i in range(num_sections):
        off = sec0 + 40 * i
        name = bytes(data[off:off + 8]).rstrip(b"\x00")
        vsize, va, rsize, roff = struct.unpack_from("<IIII", data, off + 8)
        if va <= rva < va + max(vsize, rsize):
            print(f"section {name!r} va={va:#x} vsize={vsize:#x}")
            return roff + (rva - va)
    raise ValueError(f"rva {rva:#x} not mapped")


def main() -> int:
    data = open(PE_PATH, "rb").read()
    off = rva_to_offset(data, RVA)
    code = data[off:off + SIZE]
    print(f"PE {PE_PATH}")
    print(f"GetEntry RVA={RVA:#x} file_off={off:#x}")
    for i in range(0, SIZE, 16):
        chunk = code[i:i + 16]
        hexs = " ".join(f"{b:02X}" for b in chunk)
        print(f"  +{i:02X}: {hexs}")
    print("\nnaive mov [ecx+disp] candidates:")
    found = False
    for pat, (reg, base, _sz, width) in MOV_PATTERNS.items():
        start = 0
        while True:
            i = code.find(pat, start)
            if i < 0:
                break
            if pat.endswith(b"\x81"):
                disp = struct.unpack_from("<i", code, i + len(pat))[0]
            else:
                disp = struct.unpack_from("<b", code, i + len(pat))[0]
            print(f"  +{i:02X}: mov {reg}, [{base}{disp:+#x}] (width {width})")
            found = True
            start = i + 1
    if not found:
        print("  none matched — paste the hex dump for manual analysis")
    return 0


if __name__ == "__main__":
    sys.exit(main())
