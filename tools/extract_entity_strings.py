# -*- coding: utf-8 -*-
"""Extract entity-related strings from xajh.exe."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.paths import client_exe

data = client_exe().read_bytes()
ascii_re = re.compile(rb"[\x20-\x7e]{4,}")
keywords = (
    "npc",
    "monster",
    "matter",
    "item",
    "drop",
    "entity",
    "hostplayer",
    "cecnpc",
    "cecmonster",
    "cecmatter",
    "cecplayer",
    "manager",
    "objectman",
    "world",
    "around",
    "elist",
    "mob",
    "loot",
)
out = []
seen = set()
for m in ascii_re.finditer(data):
    t = m.group().decode("ascii", "ignore")
    low = t.lower()
    if any(k in low for k in keywords):
        if t not in seen:
            seen.add(t)
            out.append(t)

dest = ROOT / "tools" / ".issues" / "recon" / "entity_strings.txt"
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text("\n".join(out), encoding="utf-8")
print("count", len(out))
# print focused
focus = []
for t in out:
    low = t.lower()
    if any(
        x in low
        for x in (
            "cecnpc",
            "cecmonster",
            "cecmatter",
            "cecplayer",
            "hostplayer",
            "npcman",
            "monsterman",
            "matterman",
            "objectman",
            "getnpc",
            "getmonster",
            "getmatter",
            "around",
            "nearest",
        )
    ):
        focus.append(t)
focus_path = dest.with_name("entity_strings_focus.txt")
focus_path.write_text("\n".join(focus), encoding="utf-8")
print("focus", len(focus), "->", focus_path)
for t in focus[:120]:
    print(t[:200])
