# -*- coding: utf-8 -*-
"""Deep extract map id/name tables from configs.pck / script.pck zlib streams."""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.paths import find_game_root

OUT = ROOT / ".issues" / "recon" / "map_tables"
OUT.mkdir(parents=True, exist_ok=True)


def decode_best(data: bytes) -> tuple[str, str]:
    for enc in ("gbk", "gb18030", "utf-8", "utf-16le"):
        try:
            return data.decode(enc), enc
        except Exception:
            continue
    return data.decode("latin1", "replace"), "latin1"


def decompress_all(data: bytes, max_streams: int = 2000):
    i = 0
    n = len(data)
    count = 0
    while i < n - 2 and count < max_streams:
        if data[i] == 0x78 and data[i + 1] in (0x01, 0x5E, 0x9C, 0xDA):
            # try decompressobj for stream
            for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
                try:
                    dec = zlib.decompress(data[i:], wbits)
                    yield i, dec
                    count += 1
                    i += 2
                    break
                except Exception:
                    continue
            else:
                # fixed window tries
                ok = False
                for size in (4 * 1024, 64 * 1024, 256 * 1024, 1024 * 1024, 4 * 1024 * 1024):
                    if i + size > n:
                        size = n - i
                    try:
                        dec = zlib.decompress(data[i : i + size])
                        yield i, dec
                        count += 1
                        i += max(2, size // 8)
                        ok = True
                        break
                    except Exception:
                        continue
                if not ok:
                    i += 1
        else:
            i += 1


def main():
    g = find_game_root()
    all_hits = []
    map_pairs = {}  # id -> names

    for rel in ["package/configs.pck", "package/script.pck", "package/data.pck"]:
        p = g / rel
        if not p.exists():
            continue
        data = p.read_bytes()
        print("scanning", rel, len(data))
        interesting = 0
        for off, dec in decompress_all(data, max_streams=800):
            # quick filter
            if not (
                b"d10" in dec
                or b"Map" in dec
                or b"scene" in dec.lower()
                or b"maps\\" in dec
                or b"maps/" in dec
                or "地图".encode("gbk") in dec
                or "地图".encode("utf-8") in dec
            ):
                continue
            text, enc = decode_best(dec)
            # save chunk if looks like table
            score = 0
            if "d10_1" in text or "d10_1" in dec.decode("latin1", "ignore"):
                score += 5
            if re.search(r"(?i)map\s*name|mapname|地图|SceneName|scene_name|InstanceName", text):
                score += 3
            if re.search(r"d\d+_\d+", text):
                score += 2
            if score <= 0:
                continue
            interesting += 1
            fname = OUT / f"{Path(rel).stem}_{off:x}_{enc}.txt"
            fname.write_text(text[:200000], encoding="utf-8", errors="replace")

            # extract patterns
            # 1) d10_1 = 中文 / "d10_1","中文"
            for m in re.finditer(r'(d\d+_\d+)\s*[=:,]\s*[\"\']?([一-鿿]{2,20})', text):
                map_pairs.setdefault(m.group(1), set()).add(m.group(2))
            for m in re.finditer(r'([\"\'])(d\d+_\d+)\1\s*[,=]\s*([\"\'])([一-鿿]{2,20})\3', text):
                map_pairs.setdefault(m.group(2), set()).add(m.group(4))
            for m in re.finditer(r'([一-鿿]{2,20})\s*[=:,]\s*[\"\']?(d\d+_\d+)', text):
                map_pairs.setdefault(m.group(2), set()).add(m.group(1))
            # ini style
            for m in re.finditer(r'(?im)^id\s*=\s*(d\d+_\d+).{0,40}?name\s*=\s*([一-鿿A-Za-z0-9_\-]+)', text):
                map_pairs.setdefault(m.group(1), set()).add(m.group(2))
            for m in re.finditer(r'(?im)^name\s*=\s*([一-鿿A-Za-z0-9_\-]+).{0,40}?path\s*=\s*maps[\\/](d\d+_\d+)', text):
                map_pairs.setdefault(m.group(2), set()).add(m.group(1))
            for m in re.finditer(r'(?im)maps[\\/](d\d+_\d+)[\\/]?[^\n]{0,40}([一-鿿]{2,20})', text):
                map_pairs.setdefault(m.group(1), set()).add(m.group(2))

            # lines containing d10_1
            for line in text.splitlines():
                if "d10_1" in line or re.search(r"d\d+_\d+", line):
                    all_hits.append(f"{rel}@0x{off:x}: {line.strip()[:240]}")

        print(" interesting chunks", interesting)

    # also scan live? no
    pairs_path = OUT / "map_id_names.json"
    import json

    serial = {k: sorted(v) for k, v in sorted(map_pairs.items())}
    pairs_path.write_text(json.dumps(serial, ensure_ascii=False, indent=2), encoding="utf-8")
    hits_path = OUT / "map_lines.txt"
    hits_path.write_text("\n".join(all_hits[:2000]), encoding="utf-8", errors="replace")
    print("pairs", len(serial), "->", pairs_path)
    print("lines", len(all_hits), "->", hits_path)
    # show d10*
    for k, v in serial.items():
        if k.startswith("d10"):
            print(k, v)
    # show any
    for k, v in list(serial.items())[:30]:
        print(k, v)


if __name__ == "__main__":
    main()
