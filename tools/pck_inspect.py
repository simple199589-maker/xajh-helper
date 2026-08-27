# -*- coding: utf-8 -*-
"""
First-pass inspector for Angelica-like .pck packages used by XAJH.

Observed header:
  magic: F0 34 DB 5E
  then fields + zlib (78 01 ...) payload region

This is a research stub: it dumps header fields and searches for zlib streams
so we can reverse the full directory table next.
"""
from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path


def find_zlib_streams(data: bytes, limit: int = 20) -> list[dict]:
    hits = []
    i = 0
    n = len(data)
    while i < n - 2 and len(hits) < limit:
        if data[i] == 0x78 and data[i + 1] in (0x01, 0x5E, 0x9C, 0xDA):
            # try decompress with incremental growth
            for size in (4 * 1024, 64 * 1024, 256 * 1024, 1024 * 1024):
                chunk = data[i : i + size]
                try:
                    dec = zlib.decompress(chunk)
                    hits.append(
                        {
                            "offset": i,
                            "cmf_flg": data[i : i + 2].hex(),
                            "trial_in": size,
                            "out_len": len(dec),
                            "preview": dec[:80],
                        }
                    )
                    i += max(2, size // 4)
                    break
                except zlib.error:
                    continue
            else:
                i += 1
        else:
            i += 1
    return hits


def inspect_pck(path: Path, max_read: int = 8_000_000) -> None:
    data = path.read_bytes()[:max_read]
    full_size = path.stat().st_size
    print(f"file={path} size={full_size} read={len(data)}")
    print("head_hex", data[:64].hex())
    if data[:4] != bytes.fromhex("f034db5e"):
        print("WARN: unexpected magic, expected f034db5e")
    # dump first dwords
    print("u32s:")
    for i in range(0, 32, 4):
        if i + 4 <= len(data):
            (v,) = struct.unpack_from("<I", data, i)
            print(f"  +{i:02x}: {v} (0x{v:08x})")
    zhits = find_zlib_streams(data[: min(len(data), 2_000_000)], limit=10)
    print(f"zlib candidates: {len(zhits)}")
    for h in zhits:
        prev = h["preview"]
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in prev)
        print(
            f"  off=0x{h['offset']:x} cmf={h['cmf_flg']} out={h['out_len']} preview={asc!r}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="path to .pck")
    ap.add_argument("--game-script", action="store_true", help="open package/script.pck from install")
    ap.add_argument("--max-read", type=int, default=8_000_000)
    args = ap.parse_args()

    if args.game_script or not args.path:
        import sys

        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        from common.paths import find_game_root

        path = find_game_root() / "package" / "script.pck"
    else:
        path = Path(args.path)
    inspect_pck(path, max_read=args.max_read)


if __name__ == "__main__":
    main()
