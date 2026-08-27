# -*- coding: utf-8 -*-
"""Find Chinese names for dXX maps from script.pck zlib streams."""
from __future__ import annotations

import json
import re
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.paths import find_game_root

OUT = ROOT / ".issues" / "recon" / "map_tables"


def decode(data: bytes) -> str | None:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return data.decode(enc)
        except Exception:
            pass
    return None


def main() -> None:
    g = find_game_root()
    data = (g / "package" / "script.pck").read_bytes()
    found_pairs: dict[str, set[str]] = {}
    contexts = []
    i = 0
    n = len(data)
    while i < n - 2:
        if data[i] == 0x78 and data[i + 1] in (0x01, 0x5E, 0x9C, 0xDA):
            dec = None
            try:
                dec = zlib.decompress(data[i:], zlib.MAX_WBITS)
            except Exception:
                for size in (256 * 1024, 1024 * 1024, 4 * 1024 * 1024):
                    try:
                        dec = zlib.decompress(data[i : i + size])
                        break
                    except Exception:
                        continue
            if not dec:
                i += 1
                continue
            if b"d10_1" not in dec and b"base_path" not in dec:
                i += 64
                continue
            text = decode(dec)
            if not text:
                i += 64
                continue

            # name before/after base_path d10*
            for m in re.finditer(
                r'name\s*=\s*"([^"]+)"\s*,?\s*(?:--[^\n]*)?\s*(?:\n|.){0,200}?base_path\s*=\s*"(d\d+[^"]*)"',
                text,
                re.S,
            ):
                path = m.group(2).strip("/\\")
                found_pairs.setdefault(path, set()).add(m.group(1))
            for m in re.finditer(
                r'base_path\s*=\s*"(d\d+[^"]*)"\s*,?\s*(?:--[^\n]*)?\s*(?:\n|.){0,200}?name\s*=\s*"([^"]+)"',
                text,
                re.S,
            ):
                path = m.group(1).strip("/\\")
                found_pairs.setdefault(path, set()).add(m.group(2))

            # broader: any table row-like
            for m in re.finditer(r'"(d\d+_\d+)"\s*,\s*"([^"]+)"', text):
                found_pairs.setdefault(m.group(1), set()).add(m.group(2))
            for m in re.finditer(r'"([^"]+)"\s*,\s*"(d\d+_\d+)"', text):
                # if first looks chinese
                if re.search(r"[一-鿿]", m.group(1)):
                    found_pairs.setdefault(m.group(2), set()).add(m.group(1))

            if "d10_1" in text:
                for m in re.finditer(r".{0,100}d10_1.{0,100}", text):
                    contexts.append(f"@0x{i:x}: " + m.group().replace("\n", " | ")[:240])
            i += 64
        else:
            i += 1

    # merge with existing map_names.json world maps
    base_path = ROOT / "app" / "data" / "map_names.json"
    maps = {}
    if base_path.exists():
        # re-parse from script_1a14f5 with correct utf-8 file already good
        maps = json.loads(base_path.read_text(encoding="utf-8"))

    # ensure world maps from InstInfo file remain correct
    inst = OUT / "script_1a14f5_utf-8.txt"
    if inst.exists():
        cur_name = None
        for line in inst.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.search(r'name\s*=\s*"([^"]+)"', line)
            if m:
                cur_name = m.group(1)
                continue
            m = re.search(r'base_path\s*=\s*"([^"]+)"', line)
            if m and cur_name:
                maps[m.group(1).strip("/\\")] = cur_name
                cur_name = None

    for path, names in found_pairs.items():
        # pick chinese-looking name
        best = None
        for name in names:
            if re.search(r"[一-鿿]", name):
                best = name
                break
        if not best:
            best = sorted(names)[0]
        maps[path] = best
        # also bare d10_1 without slash variants
        maps[path.split("/")[0]] = best

    out_json = ROOT / "app" / "data" / "map_names.json"
    out_json.write_text(json.dumps(maps, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (OUT / "d_map_pairs.json").write_text(
        json.dumps({k: sorted(v) for k, v in found_pairs.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUT / "d10_contexts2.txt").write_text("\n".join(contexts[:200]), encoding="utf-8")
    print("maps total", len(maps))
    print("d pairs", {k: sorted(v) for k, v in found_pairs.items() if k.startswith("d")})
    print("d10_1", maps.get("d10_1"))
    print("d10_2", maps.get("d10_2"))
    for k in sorted(maps):
        if re.match(r"d\d", k):
            print(k, maps[k])
    print("contexts", len(contexts))
    for c in contexts[:20]:
        print(c)


if __name__ == "__main__":
    main()
