# -*- coding: utf-8 -*-
"""Build a cleaner entity name dictionary from pack scripts."""
from __future__ import annotations

import json
import re
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.paths import find_game_root

OUT = ROOT / "app" / "data" / "entity_names.json"

# only pure Chinese (optional middle dot / book title marks)
CLEAN_NAME = re.compile(r"^[一-鿿·•]{2,10}$")
FIELD_RE = re.compile(
    r'(?i)\b(name|npc_name|npcname|monster_name|monstername|item_name|itemname|title|showname|map_name)\s*=\s*"([^"]{2,16})"'
)
# table style: Name = "xxx"
TABLE_NAME_RE = re.compile(r'(?i)(?:^|[\s,{])name\s*=\s*"([^"]{2,16})"', re.M)


def decode(b: bytes) -> str | None:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return None


def is_clean(name: str) -> bool:
    if not CLEAN_NAME.match(name):
        return False
    # reject common UI noise
    bad = ("冷却", "小时", "分钟", "确定", "取消", "提示", "错误", "成功", "失败", "测试", "test")
    if any(x in name for x in bad):
        return False
    return True


def main() -> None:
    g = find_game_root()
    buckets = {"monster": set(), "npc": set(), "matter": set(), "all": set()}
    i_total = 0
    for rel in ["package/script.pck", "package/configs.pck"]:
        data = (g / rel).read_bytes()
        i = 0
        while i < len(data) - 2:
            if data[i] == 0x78 and data[i + 1] in (0x01, 0x5E, 0x9C, 0xDA):
                dec = None
                try:
                    dec = zlib.decompress(data[i:], zlib.MAX_WBITS)
                except Exception:
                    for size in (128 * 1024, 512 * 1024, 2 * 1024 * 1024):
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
                # field captures
                for m in FIELD_RE.finditer(text):
                    field, name = m.group(1).lower(), m.group(2).strip()
                    if not is_clean(name):
                        continue
                    buckets["all"].add(name)
                    ctx = text[max(0, m.start() - 60) : m.end() + 60].lower()
                    if "monster" in field or "monster" in ctx or "怪" in ctx:
                        buckets["monster"].add(name)
                    elif "npc" in field or "npc" in ctx:
                        buckets["npc"].add(name)
                    elif "item" in field or "matter" in ctx or "道具" in ctx:
                        buckets["matter"].add(name)
                # generic name=
                for m in TABLE_NAME_RE.finditer(text):
                    name = m.group(1).strip()
                    if is_clean(name):
                        buckets["all"].add(name)
                i_total += 1
                i += 64
            else:
                i += 1

    # keyword classify leftovers
    mon_kw = re.compile(r"(怪|兽|贼|盗|兵|魔|魂|尸|妖|邪|狼|虎|蛇|蛛|匪|刺客|护法|精英|首领|卫兵|侍卫|傀儡|鬼)")
    npc_kw = re.compile(r"(掌门|长老|使者|商人|老板|掌柜|弟子|师傅|师父|镖师|捕头|船家|郎中|医师|村民|店小二|教头|总管)")
    mat_kw = re.compile(r"(箱|袋|药|丹|石|矿|草|花|果|令|符|碎片|宝箱|酒|剑|刀|甲|环|佩)")
    for n in list(buckets["all"]):
        if n in buckets["monster"] or n in buckets["npc"] or n in buckets["matter"]:
            continue
        if mon_kw.search(n):
            buckets["monster"].add(n)
        elif npc_kw.search(n):
            buckets["npc"].add(n)
        elif mat_kw.search(n):
            buckets["matter"].add(n)

    serial = {k: sorted(v) for k, v in buckets.items()}
    OUT.write_text(json.dumps(serial, ensure_ascii=False, indent=2), encoding="utf-8")
    # also write a flat list for quick view
    flat = ROOT / ".issues" / "recon" / "entity_names_clean.txt"
    flat.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for kind in ("monster", "npc", "matter"):
        lines.append(f"## {kind} {len(serial[kind])}")
        lines.extend(serial[kind][:200])
        lines.append("")
    lines.append(f"## all {len(serial['all'])}")
    lines.extend(serial["all"][:300])
    flat.write_text("\n".join(lines), encoding="utf-8")
    print(
        "streams",
        i_total,
        "all",
        len(serial["all"]),
        "monster",
        len(serial["monster"]),
        "npc",
        len(serial["npc"]),
        "matter",
        len(serial["matter"]),
    )
    print("monster sample", serial["monster"][:20])
    print("npc sample", serial["npc"][:20])
    print("wrote", OUT)


if __name__ == "__main__":
    main()
