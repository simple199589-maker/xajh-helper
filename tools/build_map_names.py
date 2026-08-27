# -*- coding: utf-8 -*-
"""Parse InstInfo_Client blocks for map id -> Chinese name."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

src = ROOT / ".issues" / "recon" / "map_tables" / "script_1a14f5_utf-8.txt"
# also try other files that may contain more complete table
candidates = list((ROOT / ".issues" / "recon" / "map_tables").glob("script_*utf-8.txt"))
candidates += list((ROOT / ".issues" / "recon" / "map_tables").glob("script_*gbk.txt"))

pattern = re.compile(
    r"InstInfo_Client\[(\d+)\]\s*=\s*\{(.*?)\n\s*\}",
    re.S,
)
# fallback looser: name/base_path pairs in order
name_re = re.compile(r'name\s*=\s*"([^"]+)"')
path_re = re.compile(r'base_path\s*=\s*"([^"]+)"')
zone_re = re.compile(r"zoneid\s*=\s*(\d+)")
world_re = re.compile(r'worldmapfile\s*=\s*"([^"]+)"')

maps = {}
raw_blocks = 0
for f in candidates:
    text = f.read_text(encoding="utf-8", errors="replace")
    if "InstInfo_Client" not in text and "base_path" not in text:
        continue
    # block parse
    # Many dumps may truncate braces; use sliding windows around base_path
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if "InstInfo_Client[" in lines[i] or (i + 1 < len(lines) and "name =" in lines[i] and "base_path" in "\n".join(lines[i : i + 20])):
            window = "\n".join(lines[i : i + 40])
            names = name_re.findall(window)
            paths = path_re.findall(window)
            zones = zone_re.findall(window)
            worlds = world_re.findall(window)
            if names and paths:
                raw_blocks += 1
                name = names[0]
                path = paths[0].strip("/\\")
                zone = int(zones[0]) if zones else None
                world = worlds[0] if worlds else path
                entry = {
                    "name": name,
                    "base_path": path,
                    "zoneid": zone,
                    "worldmapfile": world,
                    "source": f.name,
                }
                maps[path] = entry
                # also key by worldmapfile
                if world and world != path:
                    maps.setdefault(world, entry)
            i += 5
        else:
            i += 1

# specific search for d10
d10 = {k: v for k, v in maps.items() if "d10" in k.lower() or (isinstance(v, dict) and "d10" in str(v).lower())}
print("total map keys", len(maps), "blocks~", raw_blocks)
print("d10 entries", d10)

out = ROOT / "app" / "data"
out.mkdir(parents=True, exist_ok=True)
# compact id->name
id2name = {}
for k, v in maps.items():
    id2name[k] = v["name"]
path = out / "map_names.json"
path.write_text(json.dumps(id2name, ensure_ascii=False, indent=2), encoding="utf-8")
full = out / "map_info.json"
full.write_text(json.dumps(maps, ensure_ascii=False, indent=2), encoding="utf-8")
print("wrote", path)
print("sample", list(id2name.items())[:15])
# print if d10_1 present
print("d10_1 =>", id2name.get("d10_1"))
print("d10_2 =>", id2name.get("d10_2"))
# print all d*
for k, v in sorted(id2name.items()):
    if re.match(r"d\d", k):
        print(k, v)
