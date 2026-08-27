# -*- coding: utf-8 -*-
"""
Compare baseline vs action capture windows to highlight likely action packets.

Usage:
  python packet/action_diff.py captures/tcp/act_pickup_base.jsonl captures/tcp/act_pickup_action.jsonl
  python packet/action_diff.py --dir captures/tcp --prefix act_pickup
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packet.frame_split import auto_guess, try_split  # noqa: E402


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def size_hist(rows: list[dict], direction: str) -> Counter:
    return Counter(r["len"] for r in rows if r.get("dir") == direction)


def head_hist(rows: list[dict], direction: str, n: int = 4) -> Counter:
    """Histogram of first N payload bytes (hex)."""
    c: Counter = Counter()
    for r in rows:
        if r.get("dir") != direction:
            continue
        hx = r.get("hex") or ""
        if not hx:
            continue
        c[hx[: n * 2]] += 1
    return c


def unique_by_exact_hex(action: list[dict], base: list[dict], direction: str) -> list[dict]:
    base_hex = {r["hex"] for r in base if r.get("dir") == direction and r.get("hex")}
    out = []
    seen = set()
    for r in action:
        if r.get("dir") != direction:
            continue
        hx = r.get("hex")
        if not hx or hx in base_hex or hx in seen:
            continue
        seen.add(hx)
        out.append(r)
    return out


def concat_dir(rows: list[dict], direction: str) -> bytes:
    buf = bytearray()
    for r in rows:
        if r.get("dir") == direction and r.get("hex"):
            buf.extend(bytes.fromhex(r["hex"]))
    return bytes(buf)


def frame_heads(data: bytes, limit: int = 20) -> list[dict]:
    """Guess framing then list first frames' heads."""
    if not data:
        return []
    guesses = auto_guess(data)
    if not guesses:
        return []
    best = guesses[0]
    endian = "<" if best["endian"] == "LE" else ">"
    frames = try_split(
        data,
        len_size=best["len_size"],
        include_len=best["include_len"],
        endian=endian,
    )
    out = []
    for fr in frames[:limit]:
        out.append(
            {
                "len": len(fr),
                "head16": fr[:16].hex(),
                "guess": {
                    "len_size": best["len_size"],
                    "endian": best["endian"],
                    "include_len": best["include_len"],
                    "score": best["score"],
                    "coverage": best["coverage"],
                },
            }
        )
    return out


def compare(base_rows: list[dict], action_rows: list[dict]) -> dict:
    report: dict = {
        "base": {
            "c2s": len([r for r in base_rows if r.get("dir") == "c2s"]),
            "s2c": len([r for r in base_rows if r.get("dir") == "s2c"]),
            "c2s_size_hist": dict(size_hist(base_rows, "c2s").most_common(15)),
            "s2c_size_hist": dict(size_hist(base_rows, "s2c").most_common(15)),
            "c2s_head4": dict(head_hist(base_rows, "c2s", 4).most_common(10)),
            "s2c_head4": dict(head_hist(base_rows, "s2c", 4).most_common(10)),
        },
        "action": {
            "c2s": len([r for r in action_rows if r.get("dir") == "c2s"]),
            "s2c": len([r for r in action_rows if r.get("dir") == "s2c"]),
            "c2s_size_hist": dict(size_hist(action_rows, "c2s").most_common(15)),
            "s2c_size_hist": dict(size_hist(action_rows, "s2c").most_common(15)),
            "c2s_head4": dict(head_hist(action_rows, "c2s", 4).most_common(10)),
            "s2c_head4": dict(head_hist(action_rows, "s2c", 4).most_common(10)),
        },
        "new_c2s_exact": [],
        "new_s2c_exact": [],
        "size_delta_c2s": {},
        "size_delta_s2c": {},
        "frame_guess_action_c2s": [],
        "frame_guess_action_s2c": [],
    }

    base_c2s_sizes = size_hist(base_rows, "c2s")
    act_c2s_sizes = size_hist(action_rows, "c2s")
    base_s2c_sizes = size_hist(base_rows, "s2c")
    act_s2c_sizes = size_hist(action_rows, "s2c")
    for k in sorted(set(base_c2s_sizes) | set(act_c2s_sizes)):
        d = act_c2s_sizes[k] - base_c2s_sizes[k]
        if d != 0:
            report["size_delta_c2s"][str(k)] = d
    for k in sorted(set(base_s2c_sizes) | set(act_s2c_sizes)):
        d = act_s2c_sizes[k] - base_s2c_sizes[k]
        if d != 0:
            report["size_delta_s2c"][str(k)] = d

    new_c2s = unique_by_exact_hex(action_rows, base_rows, "c2s")
    new_s2c = unique_by_exact_hex(action_rows, base_rows, "s2c")
    report["new_c2s_exact"] = [
        {"ts": r.get("ts"), "len": r["len"], "hex_head": r["hex"][:64], "hex": r["hex"]}
        for r in new_c2s[:40]
    ]
    report["new_s2c_exact"] = [
        {"ts": r.get("ts"), "len": r["len"], "hex_head": r["hex"][:64], "hex": r["hex"]}
        for r in new_s2c[:40]
    ]

    act_c2s_blob = concat_dir(action_rows, "c2s")
    act_s2c_blob = concat_dir(action_rows, "s2c")
    report["frame_guess_action_c2s"] = frame_heads(act_c2s_blob)
    report["frame_guess_action_s2c"] = frame_heads(act_s2c_blob)
    return report


def print_report(report: dict) -> None:
    print("=== baseline ===")
    print(f"  c2s={report['base']['c2s']} s2c={report['base']['s2c']}")
    print(f"  c2s sizes: {report['base']['c2s_size_hist']}")
    print(f"  s2c sizes: {report['base']['s2c_size_hist']}")
    print("=== action ===")
    print(f"  c2s={report['action']['c2s']} s2c={report['action']['s2c']}")
    print(f"  c2s sizes: {report['action']['c2s_size_hist']}")
    print(f"  s2c sizes: {report['action']['s2c_size_hist']}")
    print("=== size delta (action - base counts) ===")
    print(f"  c2s: {report['size_delta_c2s']}")
    print(f"  s2c: {report['size_delta_s2c']}")
    print(f"=== new exact c2s payloads: {len(report['new_c2s_exact'])} ===")
    for r in report["new_c2s_exact"][:12]:
        print(f"  len={r['len']:4d} head={r['hex_head']}")
    print(f"=== new exact s2c payloads: {len(report['new_s2c_exact'])} ===")
    for r in report["new_s2c_exact"][:12]:
        print(f"  len={r['len']:4d} head={r['hex_head']}")
    if report["frame_guess_action_c2s"]:
        g = report["frame_guess_action_c2s"][0]["guess"]
        print(
            f"=== frame guess c2s: len_size={g['len_size']} {g['endian']} "
            f"include_len={g['include_len']} cov={g['coverage']:.2%}"
        )
        for fr in report["frame_guess_action_c2s"][:8]:
            print(f"  frame len={fr['len']} head={fr['head16']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Diff baseline vs action packet captures")
    ap.add_argument("base_jsonl", nargs="?", help="baseline session jsonl")
    ap.add_argument("action_jsonl", nargs="?", help="action session jsonl")
    ap.add_argument("--dir", default=None, help="capture dir when using --prefix")
    ap.add_argument("--prefix", default=None, help="e.g. act_pickup -> prefix_base/action jsonl")
    ap.add_argument("--out", default=None, help="write report json path")
    args = ap.parse_args()

    if args.prefix:
        d = Path(args.dir or ".")
        base_path = d / f"{args.prefix}_base.jsonl"
        act_path = d / f"{args.prefix}_action.jsonl"
    else:
        if not args.base_jsonl or not args.action_jsonl:
            ap.error("provide base/action jsonl or --prefix")
        base_path = Path(args.base_jsonl)
        act_path = Path(args.action_jsonl)

    base_rows = load_jsonl(base_path)
    act_rows = load_jsonl(act_path)
    report = compare(base_rows, act_rows)
    print_report(report)

    out = Path(args.out) if args.out else act_path.with_name(act_path.stem.replace("_action", "") + "_diff.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", out)


if __name__ == "__main__":
    main()
