# -*- coding: utf-8 -*-
"""Build full map_names.json from InstInfo_Common map_name/base_path."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
src = ROOT / ".issues" / "recon" / "map_tables" / "script_19e417_gbk.txt"
text = src.read_text(encoding="utf-8", errors="replace")

# block oriented
block_re = re.compile(r"InstInfo_Common\[(\d+)\]\s*=\s*\{(.*?)\n\}", re.S)
maps: dict[str, str] = {}
meta: dict[str, dict] = {}
for m in block_re.finditer(text):
    idx = int(m.group(1))
    body = m.group(2)
    bp = re.search(r'base_path\s*=\s*"([^"]+)"', body)
    mn = re.search(r'map_name\s*=\s*"([^"]+)"', body)
    if not bp or not mn:
        continue
    path = bp.group(1).strip("/\\")
    name = mn.group(1)
    maps[path] = name
    meta[path] = {"id": idx, "name": name, "base_path": path}

# also sequential fallback
cur = {}
for line in text.splitlines():
    m = re.search(r'base_path\s*=\s*"([^"]+)"', line)
    if m:
        cur["base_path"] = m.group(1).strip("/\\")
    m = re.search(r'map_name\s*=\s*"([^"]+)"', line)
    if m:
        cur["map_name"] = m.group(1)
    if "base_path" in cur and "map_name" in cur:
        maps[cur["base_path"]] = cur["map_name"]
        cur = {}

out = ROOT / "app" / "data"
out.mkdir(parents=True, exist_ok=True)
(out / "map_names.json").write_text(json.dumps(maps, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
(out / "map_info.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
print("count", len(maps))
print("d10_1", maps.get("d10_1"))
print("d10_2", maps.get("d10_2"))
for k in sorted(maps):
    if k.startswith("d10") or k.startswith("d0") or k.startswith("d1"):
        print(k, "=>", maps[k])
