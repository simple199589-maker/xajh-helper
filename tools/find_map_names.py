# -*- coding: utf-8 -*-
"""Search local install for map id -> Chinese name mapping."""
from __future__ import annotations

import re
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))
from common.paths import find_game_root


def try_decode(data: bytes) -> str | None:
    for enc in ("utf-8", "gbk", "gb18030", "utf-16le"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return None


def main() -> None:
    g = find_game_root()
    out = ROOT / ".issues" / "recon"
    out.mkdir(parents=True, exist_ok=True)
    target_ids = ["d10_1", "d10", "scene", "mapname", "地图"]
    hits = []

    # 1) plain text configs under userdata/support/bin
    text_roots = [g / "userdata", g / "support", g / "bin", g / "patcher"]
    for root in text_roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.stat().st_size > 2_000_000:
                continue
            if p.suffix.lower() not in {".ini", ".txt", ".xml", ".cfg", ".lua", ".json", ".csv", ".dat", ".log"}:
                # still check small files
                if p.stat().st_size > 200_000:
                    continue
            try:
                raw = p.read_bytes()
            except Exception:
                continue
            if b"d10_1" not in raw and "d10_1".encode("utf-16le") not in raw:
                # also collect general map tables later
                if b"map" not in raw.lower() and "地图".encode("gbk") not in raw and "地图".encode("utf-8") not in raw:
                    continue
            text = try_decode(raw)
            if not text:
                continue
            if "d10_1" in text or "地图" in text:
                # extract nearby lines
                for i, line in enumerate(text.splitlines()):
                    if "d10_1" in line or ("map" in line.lower() and ("name" in line.lower() or "id" in line.lower())):
                        hits.append(f"{p.relative_to(g)}:{i+1}: {line.strip()[:200]}")

    # 2) peek package configs/script for d10_1
    for rel in ["package/configs.pck", "package/script.pck", "package/data.pck", "package/interfaces.pck"]:
        p = g / rel
        if not p.exists():
            continue
        data = p.read_bytes()
        print(rel, "size", len(data), "has d10_1", b"d10_1" in data, "utf16", "d10_1".encode("utf-16le") in data)
        # find contexts around d10_1
        for needle in (b"d10_1", "d10_1".encode("utf-16le"), b"maps\\d10", b"maps/d10"):
            start = 0
            c = 0
            while c < 20:
                idx = data.find(needle, start)
                if idx < 0:
                    break
                chunk = data[max(0, idx - 64) : idx + 128]
                ascii_preview = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                hits.append(f"{rel}@0x{idx:x}: {ascii_preview}")
                # try zlib decompress from nearby
                start = idx + 1
                c += 1

        # try decompress zlib streams and search
        zcount = 0
        i = 0
        found_in_z = 0
        while i < min(len(data), 2_000_000) and found_in_z < 30:
            if data[i] == 0x78 and data[i + 1] in (0x01, 0x5E, 0x9C, 0xDA):
                for size in (64 * 1024, 256 * 1024, 1024 * 1024):
                    try:
                        dec = zlib.decompress(data[i : i + size])
                    except Exception:
                        continue
                    if b"d10_1" in dec or "地图".encode("gbk") in dec or b"MapName" in dec or b"mapname" in dec.lower():
                        text = try_decode(dec) or ""
                        # keep lines of interest
                        for line in (text.splitlines() if text else []):
                            if any(k in line for k in ("d10_1", "Map", "map", "地图", "scene", "Scene")):
                                hits.append(f"{rel}:zlib@0x{i:x}: {line.strip()[:220]}")
                                found_in_z += 1
                                if found_in_z >= 30:
                                    break
                        # also dump first occurrence context
                        j = dec.find(b"d10_1")
                        if j >= 0:
                            prev = "".join(chr(b) if 32 <= b < 127 else "." for b in dec[max(0, j - 40) : j + 80])
                            hits.append(f"{rel}:zlibctx@0x{i:x}: {prev}")
                    break
                zcount += 1
                i += 64
            else:
                i += 1
        print(rel, "zlib_scanned_heads", zcount, "found_in_z", found_in_z)

    dest = out / "mapname_hits.txt"
    dest.write_text("\n".join(hits), encoding="utf-8", errors="replace")
    print("hits", len(hits), "->", dest)
    for h in hits[:80]:
        print(h)


if __name__ == "__main__":
    main()
