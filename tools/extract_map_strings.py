# -*- coding: utf-8 -*-
from pathlib import Path
import re

hits = Path(r"d:/work/python/game-get/tools/.issues/recon/xajh_exe_hits.txt")
sample = Path(r"d:/work/python/game-get/tools/.issues/recon/xajh_exe_strings_sample.txt")
# also full interesting if any
paths = [hits, sample]
# deep strings sample is only 2000; hits is filtered network. Need broader dump.
# Re-extract from exe focusing on map/pos
import sys
sys.path.insert(0, r"d:/work/python/game-get")
from common.paths import client_exe

pat = re.compile(
    rb"[\x20-\x7e]{4,}"
)
keys = (
    b"map", b"Map", b"MAP", b"scene", b"Scene", b"world", b"World",
    b"pos", b"Pos", b"coord", b"Coord", b"player", b"Player", b"Host",
    b"Instance", b"instance", b"Move", b"move", b"position", b"Position",
    b"GetPos", b"SetPos", b"CECHost", b"HostPlayer", b"LocalPlayer",
    b"curmap", b"CurMap", b"mapid", b"MapID", b"map_id",
)
data = client_exe().read_bytes()
out = []
seen = set()
for m in pat.finditer(data):
    s = m.group()
    if any(k in s for k in keys):
        try:
            t = s.decode("ascii")
        except Exception:
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
dest = Path(r"d:/work/python/game-get/tools/.issues/recon/map_pos_strings.txt")
dest.write_text("\n".join(out), encoding="utf-8")
print("count", len(out), "->", dest)
for t in out[:120]:
    print(t[:160])
