# -*- coding: utf-8 -*-
import re
import json
from pathlib import Path

raw = Path(".issues/recon/map_tables/script_18c1a1_utf-8.txt").read_bytes()
# try encodings
for enc in ("utf-8", "gbk", "gb18030", "utf-8-sig"):
    try:
        t = raw.decode(enc)
        if "绿竹" in t or "幻想乡" in t:
            print("enc ok", enc, "绿竹" in t)
            text = t
            break
        # check Cfg[169] name bytes
        m = re.search(r'Instance\.Cfg\[169\]\s*=\s*\{.{0,200}\["Name"\]', t, re.S)
        print("enc", enc, "has Cfg169", bool(m), "sample", repr(t[1070:1150]) if len(t)>1150 else "")
    except Exception as e:
        print("enc fail", enc, e)
else:
    text = raw.decode("utf-8", errors="replace")
    print("fallback utf-8 replace")

# Pattern: ["Name"] = --[[!AUTO_N]]"xxx"  OR ["Name"] = "xxx"
pairs = {}
for m in re.finditer(r"Instance\.Cfg\[(\d+)\]\s*=", text):
    iid = int(m.group(1))
    chunk = text[m.end() : m.end() + 2500]
    nm = re.search(
        r'\["Name"\]\s*=\s*(?:--\[\[!AUTO_\d+\]\])?"([^"]+)"',
        chunk,
    )
    if nm:
        pairs[iid] = nm.group(1)

print("named", len(pairs))
# print using ascii escape to avoid console issues
for iid in sorted(pairs):
    name = pairs[iid]
    print(f"{iid}\t{name.encode('unicode_escape').decode('ascii')}")

out = Path("app/data/instance_names.json")
data = [{"id": i, "name": pairs[i]} for i in sorted(pairs)]
out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("wrote", out, len(data))
print("169", pairs.get(169))
