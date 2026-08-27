# -*- coding: utf-8 -*-
"""
Rebuild scene_map.json from InstInfo tables + design SID/MAP.

Display name priority:
1. open-world overrides (福州城 etc.)
2. InstInfo_Client[sid].name / InstInfo_Common map_name (strip [tag] prefix)
3. never fall back to activity-tagged resource titles without strip

@author by ak
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECON = ROOT / ".issues/recon/map_tables"


def strip_tag(name: str) -> str:
    """Remove [xxx] activity tags, keep place name. @author by ak"""
    t = (name or "").strip()
    t = re.sub(r"^\[[^\]]+\]\s*", "", t)
    t = t.replace("\\r", " ").split("\r")[0].split("\n")[0].strip()
    return t


def parse_instinfo_client(text: str) -> dict[int, dict]:
    """Parse InstInfo_Client[N] = { name=..., base_path=... }. @author by ak"""
    out: dict[int, dict] = {}
    # blocks
    for m in re.finditer(
        r"InstInfo_Client\[(\d+)\]\s*=\s*\{([^}]{0,1200})\}",
        text,
        re.S,
    ):
        sid = int(m.group(1))
        body = m.group(2)
        rec: dict = {}
        nm = re.search(r"\bname\s*=\s*\"([^\"]+)\"", body)
        if nm:
            rec["name"] = nm.group(1)
        bp = re.search(r"\bbase_path\s*=\s*\"([^\"]+)\"", body)
        if bp:
            rec["base_path"] = bp.group(1).rstrip("/").lower()
        wm = re.search(r"\bworldmapfile\s*=\s*\"([^\"]+)\"", body)
        if wm:
            rec["worldmapfile"] = wm.group(1).rstrip("/").lower()
        if rec:
            out[sid] = rec
    return out


def parse_instinfo_common(text: str) -> dict[int, dict]:
    """Parse InstInfo_Common[N] = { map_name=..., base_path=... }. @author by ak"""
    out: dict[int, dict] = {}
    for m in re.finditer(
        r"InstInfo_Common\[(\d+)\]\s*=\s*\{([^}]{0,1500})\}",
        text,
        re.S,
    ):
        sid = int(m.group(1))
        body = m.group(2)
        rec: dict = {}
        nm = re.search(r"\bmap_name\s*=\s*\"([^\"]+)\"", body)
        if nm:
            rec["map_name"] = nm.group(1)
        bp = re.search(r"\bbase_path\s*=\s*\"([^\"]+)\"", body)
        if bp:
            rec["base_path"] = bp.group(1).rstrip("/").lower()
        if rec:
            out[sid] = rec
    return out


def parse_design_sid_map(text: str) -> dict[int, str]:
    sid_map: dict[int, str] = {}
    for m in re.finditer(r"SID:(\d+)\s+MAP:([A-Za-z0-9_]+)", text):
        sid_map[int(m.group(1))] = m.group(2).lower()
    for m in re.finditer(
        r"城-([^\\\"\s]+)\s+SID:(\d+)\s+MAP:([A-Za-z0-9_]+)",
        text,
    ):
        sid_map[int(m.group(2))] = m.group(3).lower()
    return sid_map


def parse_design_city_cn(text: str) -> dict[int, str]:
    """城-福州城 SID:68 MAP:x59 style. @author by ak"""
    out: dict[int, str] = {}
    for m in re.finditer(
        r"城-([^\\\"\s]+)\s+SID:(\d+)\s+MAP:([A-Za-z0-9_]+)",
        text,
    ):
        cn = m.group(1)
        # strip leading markers like ^be4800
        cn = re.sub(r"^\^[0-9a-fA-F]+", "", cn)
        out[int(m.group(2))] = strip_tag(cn)
    return out


def main() -> int:
    lines = []
    client: dict[int, dict] = {}
    common: dict[int, dict] = {}
    sid_map: dict[int, str] = {}
    city_cn: dict[int, str] = {}

    for p in RECON.glob("*.txt"):
        try:
            t = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "InstInfo_Client[" in t:
            c = parse_instinfo_client(t)
            client.update(c)
            lines.append(f"client from {p.name}: +{len(c)}")
        if "InstInfo_Common[" in t:
            c = parse_instinfo_common(t)
            common.update(c)
            lines.append(f"common from {p.name}: +{len(c)}")
        if "SID:" in t and "MAP:" in t:
            sm = parse_design_sid_map(t)
            sid_map.update(sm)
            city_cn.update(parse_design_city_cn(t))
            lines.append(f"design from {p.name}: sid_map+{len(sm)}")

    # merge map resource id per scene
    scene_to_map: dict[str, str] = {}
    scene_to_name: dict[str, str] = {}

    all_sids = set(client) | set(common) | set(sid_map) | set(city_cn)
    for sid in sorted(all_sids):
        mid = None
        cn = None
        # map id
        if sid in sid_map:
            mid = sid_map[sid]
        if not mid and sid in client:
            mid = client[sid].get("base_path") or client[sid].get("worldmapfile")
        if not mid and sid in common:
            mid = common[sid].get("base_path")
        if mid:
            mid = mid.replace("\\", "/").split("/")[0].lower()

        # display name: city table > client name > common map_name (stripped)
        if sid in city_cn:
            cn = city_cn[sid]
        if not cn and sid in client and client[sid].get("name"):
            cn = strip_tag(client[sid]["name"])
        if not cn and sid in common and common[sid].get("map_name"):
            cn = strip_tag(common[sid]["map_name"])

        if mid:
            scene_to_map[str(sid)] = mid
        if cn:
            scene_to_name[str(sid)] = cn

    # open-world display overrides (player-facing city names)
    OVERRIDES = {
        68: ("x59", "福州城"),
        2: ("x23", "福州城"),
        65: ("x53", "衡山城"),
        12: ("x1", "洛阳"),
        8: ("x37", "余杭"),
        # 华山派 open world: InstInfo says 华山新; UI faction is 华山派
        74: ("x62", "华山派"),
    }
    for sid, (mid, cn) in OVERRIDES.items():
        scene_to_map[str(sid)] = mid
        scene_to_name[str(sid)] = cn

    out = {
        "scene_to_map": dict(sorted(scene_to_map.items(), key=lambda x: int(x[0]))),
        "scene_to_name": dict(sorted(scene_to_name.items(), key=lambda x: int(x[0]))),
        "_meta": {
            "source": "InstInfo_Client/Common + DESIGN_TRANS SID/MAP + overrides",
            "note": "display strips [activity] tags; overrides for major cities",
        },
    }
    path = ROOT / "app/data/scene_map.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    report = ROOT / ".issues/break0/scene_map_rebuild.txt"
    report_lines = lines + [
        f"total map={len(scene_to_map)} name={len(scene_to_name)}",
        f"68 {scene_to_map.get('68')} {scene_to_name.get('68')}",
        f"74 {scene_to_map.get('74')} {scene_to_name.get('74')}",
        f"client_entries={len(client)} common_entries={len(common)}",
    ]
    # sample names that still look like activity
    bad = [
        f"{k}:{v}"
        for k, v in scene_to_name.items()
        if "[" in v or "日常" in v or "副本" in v
    ]
    report_lines.append(f"suspicious_names={bad[:20]}")
    report.write_text("\n".join(report_lines), encoding="utf-8")
    print("wrote", path)
    print("wrote", report)
    print("68", scene_to_map.get("68"), scene_to_name.get("68"))
    print("74", scene_to_map.get("74"), scene_to_name.get("74"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
