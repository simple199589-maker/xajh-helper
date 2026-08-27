# -*- coding: utf-8 -*-
"""
Heuristic splitter for length-prefixed game streams.

Usage:
  python packet/frame_split.py captures/tcp/sess_xxx.c2s.bin
  python packet/frame_split.py captures/tcp/sess_xxx.jsonl --from-jsonl
"""
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path


def try_split(data: bytes, len_size: int = 2, include_len: bool = True, endian: str = "<") -> list[bytes]:
    """Split buffer assuming each frame starts with length field."""
    fmt = {2: endian + "H", 4: endian + "I"}[len_size]
    frames = []
    i = 0
    n = len(data)
    while i + len_size <= n:
        (ln,) = struct.unpack_from(fmt, data, i)
        if include_len:
            total = ln
            payload_off = len_size
            # some protocols: length includes header; others length is payload only
            if total < len_size:
                # treat as payload-only length
                total = ln + len_size
                payload_off = len_size
        else:
            total = ln + len_size
            payload_off = len_size
        if total <= 0 or i + total > n:
            break
        frames.append(data[i : i + total])
        i += total
    return frames


def score_frames(frames: list[bytes], data_len: int) -> dict:
    if not frames:
        return {"count": 0, "coverage": 0.0, "score": 0}
    covered = sum(len(f) for f in frames)
    sizes = [len(f) for f in frames]
    # prefer many reasonable frames and high coverage
    avg = covered / len(frames)
    reasonable = sum(1 for s in sizes if 4 <= s <= 4096)
    score = covered / max(data_len, 1) * 100 + reasonable * 2 - abs(avg - 64) * 0.01
    return {
        "count": len(frames),
        "coverage": covered / max(data_len, 1),
        "avg": avg,
        "min": min(sizes),
        "max": max(sizes),
        "score": score,
    }


def auto_guess(data: bytes) -> list[dict]:
    results = []
    for len_size in (2, 4):
        for endian in ("<", ">"):
            for include_len in (True, False):
                # two interpretations for include_len already handled roughly
                frames = []
                fmt = {2: endian + "H", 4: endian + "I"}[len_size]
                i = 0
                n = len(data)
                ok = True
                while i + len_size <= n:
                    (ln,) = struct.unpack_from(fmt, data, i)
                    if include_len:
                        total = ln
                        if total < len_size:
                            total = ln + len_size
                    else:
                        total = ln + len_size
                    if total < len_size or total > 1_000_000 or i + total > n:
                        ok = False
                        break
                    frames.append(data[i : i + total])
                    i += total
                    if len(frames) > 5000:
                        break
                # allow leftover tail
                sc = score_frames(frames, len(data))
                sc.update(
                    {
                        "len_size": len_size,
                        "endian": "LE" if endian == "<" else "BE",
                        "include_len": include_len,
                        "leftover": len(data) - sum(map(len, frames)),
                        "ok": ok or sc["coverage"] > 0.8,
                    }
                )
                results.append(sc)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="raw .bin stream or .jsonl session")
    ap.add_argument("--from-jsonl", action="store_true")
    ap.add_argument("--dir", choices=["c2s", "s2c", "both"], default="both")
    args = ap.parse_args()
    path = Path(args.path)

    blobs: dict[str, bytes] = {}
    if args.from_jsonl or path.suffix.lower() == ".jsonl":
        c2s = bytearray()
        s2c = bytearray()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            raw = bytes.fromhex(rec["hex"])
            if rec["dir"] == "c2s":
                c2s.extend(raw)
            else:
                s2c.extend(raw)
        blobs = {"c2s": bytes(c2s), "s2c": bytes(s2c)}
    else:
        key = "c2s" if "c2s" in path.name else "s2c" if "s2c" in path.name else "stream"
        blobs = {key: path.read_bytes()}

    for name, data in blobs.items():
        if args.dir != "both" and name != args.dir:
            continue
        print(f"==== {name} bytes={len(data)} ====")
        if not data:
            print("empty")
            continue
        print("head:", data[:32].hex())
        guesses = auto_guess(data)
        for g in guesses[:6]:
            print(
                f"  len={g['len_size']} {g['endian']} include_len={g['include_len']} "
                f"frames={g['count']} cov={g['coverage']:.2%} leftover={g['leftover']} score={g['score']:.1f}"
            )


if __name__ == "__main__":
    main()
