# -*- coding: utf-8 -*-
"""Rebuild instance_names.json with need_level from Desc + Cfg Hint."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".issues" / "recon" / "map_tables" / "script_18c1a1_utf-8.txt"
OUT = ROOT / "app" / "data" / "instance_names.json"


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "gb18030", "utf-8-sig"):
        try:
            t = raw.decode(enc)
        except Exception:
            continue
        if "绿竹" in t or "Instance.Cfg" in t:
            return t
    return raw.decode("utf-8", errors="replace")


def _block_end(text: str, start: int) -> int:
    nxt = re.search(r"Instance\.(?:Desc|Cfg)\[\d+\]\s*=", text[start:])
    if nxt:
        return start + nxt.start()
    return min(len(text), start + 12000)


def _pick_level(s: str) -> int | None:
    """
    Prefer list-facing grades from Hint/Content:
      1. 需求等级 / 最低等级 / 需要等级 / 进入等级 / 等级要求
      2. 推荐等级 range low (列表 Hint 常用 15-24)
      3. 推荐等级 single
      4. 需要组队N级 / N级以上
    """
    s = (s or "").replace("\\r", "\n").replace("\\n", "\n")

    def _ok(n: int) -> bool:
        return 1 <= n <= 150

    for pat in (
        r"需求等级[：:\s]*(\d+)\s*[-~～—]\s*(\d+)",
        r"最低等级[：:\s]*(\d+)",
        r"需求等级[：:\s]*(\d+)",
        r"需要等级[：:\s]*(\d+)",
        r"进入等级[：:\s]*(\d+)",
        r"等级要求[：:\s]*(\d+)",
    ):
        m = re.search(pat, s)
        if m:
            a = int(m.group(1))
            if _ok(a):
                return a

    m = re.search(r"推荐等级[：:\s]*(\d+)\s*[-~～—]\s*(\d+)", s)
    if m:
        a = int(m.group(1))
        if _ok(a):
            return a

    m = re.search(r"推荐等级[：:\s]*(\d+)", s)
    if m:
        a = int(m.group(1))
        if _ok(a):
            return a

    for pat in (
        r"(?:需要组队|组队)(\d+)\s*级",
        r"(\d+)\s*级以上",
        r"(\d+)\s*级(?:玩家|可|才能|开启)",
    ):
        m = re.search(pat, s)
        if m:
            a = int(m.group(1))
            if _ok(a):
                return a
    return None


def main() -> int:
    text = _decode(SCRIPT.read_bytes())
    names: dict[int, str] = {}
    levels: dict[int, int] = {}
    types: dict[int, int] = {}

    for m in re.finditer(r"Instance\.Cfg\[(\d+)\]\s*=", text):
        iid = int(m.group(1))
        end = _block_end(text, m.end())
        chunk = text[m.start() : end]
        nm = re.search(
            r'\["Name"\]\s*=\s*(?:--\[\[!AUTO_\d+\]\])?"([^"]+)"',
            chunk,
        )
        if nm:
            names[iid] = nm.group(1)
        # Hint is the list-panel grade (推荐/最低).
        lv = _pick_level(chunk)
        if lv is not None:
            levels[iid] = lv

    for m in re.finditer(r"Instance\.Desc\[(\d+)\]\s*=\s*\{", text):
        iid = int(m.group(1))
        end = _block_end(text, m.end())
        chunk = text[m.start() : end]
        lv = _pick_level(chunk)
        if lv is not None and iid not in levels:
            # Cfg Hint wins when present; Desc fills gaps.
            levels[iid] = lv
        mm = re.search(r'\["type"\]\s*=\s*(\d+)', chunk)
        if mm:
            types[iid] = int(mm.group(1))

    data = []
    for iid in sorted(names):
        item: dict = {"id": iid, "name": names[iid]}
        if iid in levels:
            item["need_level"] = levels[iid]
        if iid in types:
            item["type"] = types[iid]
        data.append(item)

    OUT.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with_lv = sum(1 for x in data if "need_level" in x)
    print(f"wrote {OUT} total={len(data)} with_level={with_lv}")
    for iid in (
        169,
        2576,
        2465,
        1777,
        1923,
        1928,
        3042,
        587,
        7368,
        858,
        626,
        7630,
        8306,
        4689,
        7305,
    ):
        nm = names.get(iid, "")
        print(
            f"  {iid} {nm.encode('unicode_escape').decode()} "
            f"lv={levels.get(iid)} type={types.get(iid)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
