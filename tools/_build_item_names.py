# -*- coding: utf-8 -*-
"""Build app/data/item_names.json from client packs.

Priority (later overwrites only if better/shorter concrete name):
  1) data.pck  <name value=".."/> + <templ_id value="N"/>
  2) any pck zlib XML name/templ_id pairs
  3) script.pck item_ext_desc[N] short title heuristic (fallback only)
  4) manual seeds

@author by ak
"""
from __future__ import annotations

import json
import re
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "app" / "data" / "item_names.json"

RE_BLOCK = re.compile(
    r'<name\s+value="([^"]{1,64})"[^>]*/?>[\s\S]{0,400}?<templ_id\s+value="(\d{1,8})"',
    re.IGNORECASE,
)
RE_BLOCK_REV = re.compile(
    r'<templ_id\s+value="(\d{1,8})"[^>]*/?>[\s\S]{0,400}?<name\s+value="([^"]{1,64})"',
    re.IGNORECASE,
)
RE_ATTR_PAIR = re.compile(
    r'name="([^"]{1,64})"[^>]{0,200}templ[_-]?id="(\d{1,8})"',
    re.IGNORECASE,
)
RE_ATTR_PAIR_REV = re.compile(
    r'templ[_-]?id="(\d{1,8})"[^>]{0,200}name="([^"]{1,64})"',
    re.IGNORECASE,
)
RE_EXT = re.compile(
    r'item_ext_desc\[(\d+)\]\s*=\s*"((?:\\.|[^"\\])*)"',
    re.IGNORECASE,
)
RE_COLOR = re.compile(r"\^[0-9a-fA-F]{6}")
RE_CTRL = re.compile(r"[\x00-\x1f]+")


def _decode(b: bytes) -> str | None:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return None


def _clean_name(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    s = RE_COLOR.sub("", s)
    s = RE_CTRL.sub("", s)
    s = "".join(ch for ch in s if ch.isprintable() or ch in ("·", "—", "-")).strip()
    if len(s) < 2 or len(s) > 32:
        return ""
    if s in ("任务：", "任务:"):
        return ""
    if s.startswith("使用") and len(s) > 16:
        return ""
    cjk = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    if cjk < 1:
        return ""
    return s


def _desc_to_name(raw: str) -> str:
    """Best-effort short title from item_ext_desc (not preferred)."""
    s = (raw or "").strip()
    if not s:
        return ""
    s = s.replace("\\r", "\n").replace("\\n", "\n").replace('\\"', '"')
    s = RE_COLOR.sub("", s)
    for line in s.replace("\r", "\n").split("\n"):
        line = RE_CTRL.sub("", line).strip()
        line = "".join(
            ch for ch in line if ch.isprintable() or ch in ("·", "—", "-")
        ).strip()
        if len(line) < 2:
            continue
        # skip pure lore/desc sentences (。 at end / too long)
        if "。" in line or len(line) > 18:
            # try noun before first 。
            head = line.split("。", 1)[0].strip()
            if 2 <= len(head) <= 16:
                cjk = sum(1 for c in head if "\u4e00" <= c <= "\u9fff")
                if cjk >= 2 and not head.startswith("在") and not head.startswith("可以"):
                    # still often desc; only accept if no verb-ish
                    if not any(x in head for x in ("具有", "使用", "服用", "可以", "立即")):
                        return head
            continue
        cjk = sum(1 for c in line if "\u4e00" <= c <= "\u9fff")
        if cjk >= 2:
            return line[:16]
    return ""


def _put(dest: dict[str, str], tid: str, name: str, *, force: bool = False) -> bool:
    name = _clean_name(name) if not force else (name or "").strip()
    if not name or not tid.isdigit():
        return False
    old = dest.get(tid)
    if old is None:
        dest[tid] = name
        return True
    if force:
        dest[tid] = name
        return True
    # prefer shorter concrete names
    if len(name) < len(old) and len(name) >= 2:
        dest[tid] = name
        return True
    return False


def zlib_iter(data: bytes, max_chunks: int = 12000):
    i = 0
    n = 0
    while i < len(data) - 2 and n < max_chunks:
        if data[i] == 0x78 and data[i + 1] in (0x01, 0x5E, 0x9C, 0xDA):
            dec = None
            try:
                dec = zlib.decompress(data[i:])
            except Exception:
                for size in (64 * 1024, 256 * 1024, 1024 * 1024, 4 * 1024 * 1024):
                    try:
                        dec = zlib.decompress(data[i : i + size])
                        break
                    except Exception:
                        continue
            if dec:
                yield dec
                n += 1
                i += 64
                continue
        i += 1


def ingest_xml(text: str, dest: dict[str, str]) -> int:
    n = 0
    for m in RE_BLOCK.finditer(text):
        if _put(dest, str(int(m.group(2))), m.group(1)):
            n += 1
    for m in RE_BLOCK_REV.finditer(text):
        if _put(dest, str(int(m.group(1))), m.group(2)):
            n += 1
    for m in RE_ATTR_PAIR.finditer(text):
        if _put(dest, str(int(m.group(2))), m.group(1)):
            n += 1
    for m in RE_ATTR_PAIR_REV.finditer(text):
        if _put(dest, str(int(m.group(1))), m.group(2)):
            n += 1
    return n


def ingest_desc(text: str, dest: dict[str, str]) -> int:
    """Fill only missing tids from item_ext_desc short titles."""
    n = 0
    for m in RE_EXT.finditer(text):
        tid = str(int(m.group(1)))
        if tid in dest:
            continue
        name = _desc_to_name(m.group(2))
        if name and _put(dest, tid, name):
            n += 1
    return n


def scan_utf16_name_tid(blob: bytes, dest: dict[str, str]) -> int:
    """
    Heuristic: utf-16le Chinese string followed/preceded by LE u32 tid in window.
    Very conservative to avoid garbage.
    """
    n = 0
    # find CJK runs in utf-16le (at least 2 chars = 4 bytes + null)
    i = 0
    while i < len(blob) - 8:
        # look for potential wchar start
        if blob[i + 1] == 0 and 0x4E <= blob[i] <= 0x9F:
            # try decode short run
            end = i
            chars = []
            while end + 1 < len(blob) and len(chars) < 16:
                lo, hi = blob[end], blob[end + 1]
                if hi == 0 and 0x20 <= lo < 0x7F:
                    chars.append(chr(lo))
                    end += 2
                    continue
                if hi != 0:
                    cp = lo | (hi << 8)
                    if 0x4E00 <= cp <= 0x9FFF or cp in (0x00B7, 0x2014):
                        chars.append(chr(cp))
                        end += 2
                        continue
                break
            if len(chars) >= 2:
                name = "".join(chars)
                cjk = sum(1 for c in name if "\u4e00" <= c <= "\u9fff")
                if cjk >= 2:
                    # scan nearby dwords for plausible tid
                    for off in range(max(0, i - 32), min(len(blob) - 4, end + 32), 4):
                        tid = int.from_bytes(blob[off : off + 4], "little")
                        if 100 <= tid <= 2_000_000:
                            if _put(dest, str(tid), name):
                                n += 1
                                break
            i = max(i + 2, end)
            continue
        i += 2
    return n


def main() -> int:
    from common.paths import find_game_root

    out: dict[str, str] = {}
    g = find_game_root()
    packs = [
        ("data.pck", "xml"),
        ("configs.pck", "xml"),
        ("interfaces.pck", "xml"),
        ("script.pck", "desc"),
    ]
    for pname, mode in packs:
        p = g / "package" / pname
        if not p.is_file():
            print(f"skip {pname}")
            continue
        data = p.read_bytes()
        chunks = 0
        added = 0
        for dec in zlib_iter(data):
            chunks += 1
            text = _decode(dec)
            if not text:
                continue
            if mode == "xml":
                if "templ_id" in text or "templ-id" in text or "<name" in text:
                    added += ingest_xml(text, out)
            else:
                if "item_ext_desc" in text:
                    added += ingest_desc(text, out)
        print(f"{pname}: chunks={chunks} size_now={len(out)} (ops≈{added})")

    # seeds always win
    seeds = {
        "44374": "白云熊胆丸",
    }
    for k, v in seeds.items():
        _put(out, k, v, force=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT} entries={len(out)}")
    for tid in ("44374", "1032", "36029", "231002", "81538", "1002116", "9999"):
        print(f"  {tid} -> {out.get(tid, '(missing)')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
