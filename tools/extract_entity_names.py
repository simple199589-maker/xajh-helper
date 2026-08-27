# -*- coding: utf-8 -*-
"""Extract possible NPC/monster/item display names from script/configs packs."""
from __future__ import annotations

import json
import re
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.paths import find_game_root

OUT = ROOT / "app" / "data"
OUT.mkdir(parents=True, exist_ok=True)

NAME_RE = re.compile(r"[一-鿿]{2,12}")
# lua-ish name fields
FIELD_RE = re.compile(
    r'(?i)(?:name|npcname|monstername|itemname|title|showname)\s*=\s*"([^"]{2,24})"'
)


def decode(b: bytes) -> str | None:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return b.decode(enc)
        except Exception:
            pass
    return None


def main():
    g = find_game_root()
    names = {"monster": set(), "npc": set(), "matter": set(), "all": set()}
    samples = []
    for rel in ["package/script.pck", "package/configs.pck", "package/data.pck"]:
        p = g / rel
        if not p.exists():
            continue
        data = p.read_bytes()
        print("scan", rel, len(data))
        i = 0
        interesting = 0
        while i < len(data) - 2 and interesting < 500:
            if data[i] == 0x78 and data[i + 1] in (0x01, 0x5E, 0x9C, 0xDA):
                dec = None
                try:
                    dec = zlib.decompress(data[i:], zlib.MAX_WBITS)
                except Exception:
                    for size in (64 * 1024, 256 * 1024, 1024 * 1024):
                        try:
                            dec = zlib.decompress(data[i : i + size])
                            break
                        except Exception:
                            continue
                if not dec:
                    i += 1
                    continue
                text = decode(dec)
                if not text:
                    i += 64
                    continue
                # field names
                for m in FIELD_RE.finditer(text):
                    n = m.group(1).strip()
                    if not NAME_RE.fullmatch(n) and not re.search(r"[一-鿿]", n):
                        continue
                    if not re.search(r"[一-鿿]", n):
                        continue
                    names["all"].add(n)
                    lowctx = text[max(0, m.start() - 40) : m.end() + 40].lower()
                    if any(k in lowctx for k in ("monster", "mob", "怪")):
                        names["monster"].add(n)
                    elif any(k in lowctx for k in ("npc", "tasknpc")):
                        names["npc"].add(n)
                    elif any(k in lowctx for k in ("item", "matter", "drop", "道具")):
                        names["matter"].add(n)
                # if chunk mentions monster/npc tables
                if any(k in text for k in ("Monster", "NPC", "Matter", "怪物", "道具")):
                    interesting += 1
                    for n in NAME_RE.findall(text):
                        if 2 <= len(n) <= 10:
                            names["all"].add(n)
                i += 64
            else:
                i += 1
        print(" interesting", interesting)

    serial = {k: sorted(v) for k, v in names.items()}
    path = OUT / "entity_names.json"
    path.write_text(json.dumps(serial, ensure_ascii=False, indent=2), encoding="utf-8")
    print("all", len(serial["all"]), "monster", len(serial["monster"]), "npc", len(serial["npc"]))
    print("sample all", serial["all"][:30])
    print("sample monster", serial["monster"][:30])
    print("wrote", path)


if __name__ == "__main__":
    main()
