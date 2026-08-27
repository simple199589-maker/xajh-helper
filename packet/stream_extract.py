# -*- coding: utf-8 -*-
"""
Extract game TCP payloads from pktmon etl2txt dumps or reconstructed frames.

Usage:
  python packet/stream_extract.py captures/tcp/action_xxx.txt
  python packet/stream_extract.py captures/tcp/action_xxx.txt --local-port 61032
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.paths import CAPTURE_DIR  # noqa: E402

# pktmon etl2txt layout (Wi-Fi):
#   13:52:33.191101400 PktGroupId ... OriginalSize 113
#       ... ethertype IPv4 ... 192.168.0.2.51193 > 193.112.93.158.6598: Flags [P.], ... length 41
#       0x0000:  ...
_TS_RE = re.compile(r"^(?P<ts>\d{2}:\d{2}:\d{2}\.\d+)\b")
_SUMMARY_RE = re.compile(
    r"(?P<src_ip>\d+\.\d+\.\d+\.\d+)\.(?P<src_port>\d+)\s*>\s*"
    r"(?P<dst_ip>\d+\.\d+\.\d+\.\d+)\.(?P<dst_port>\d+):\s*"
    r"Flags\s*\[(?P<flags>[^\]]*)\].*?length\s+(?P<length>\d+)",
    re.IGNORECASE,
)
_HEX_LINE_RE = re.compile(r"^\s*0x[0-9a-fA-F]+:\s+((?:[0-9a-fA-F]{2}\s*)+)")


@dataclass
class TcpSeg:
    """One TCP segment with application payload."""

    ts: str
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int
    flags: str
    payload: bytes
    frame_len: int

    @property
    def flow_key(self) -> str:
        a = f"{self.src_ip}:{self.src_port}"
        b = f"{self.dst_ip}:{self.dst_port}"
        return "->".join(sorted([a, b]))

    def direction(self, server_ip: str, server_port: int) -> str:
        if self.dst_ip == server_ip and self.dst_port == server_port:
            return "c2s"
        if self.src_ip == server_ip and self.src_port == server_port:
            return "s2c"
        return "other"


def parse_hex_block(lines: list[str]) -> bytes:
    """Parse consecutive pktmon hex dump lines into raw frame bytes."""
    out = bytearray()
    for line in lines:
        m = _HEX_LINE_RE.match(line)
        if not m:
            break
        out.extend(bytes.fromhex(m.group(1)))
    return bytes(out)


def extract_ipv4_tcp_payload(frame: bytes) -> bytes | None:
    """Locate IPv4+TCP in a Wi-Fi/Ethernet frame and return TCP payload."""
    # Find a valid IPv4 header start, including packets with IPv4 options.
    ip_off = -1
    for i in range(0, max(0, len(frame) - 40 + 1)):
        version = frame[i] >> 4
        ihl_words = frame[i] & 0x0F
        if version == 4 and ihl_words >= 5 and i + ihl_words * 4 <= len(frame):
            # basic sanity: total length field
            total = struct.unpack_from("!H", frame, i + 2)[0]
            if 20 <= total <= len(frame) - i + 64:
                proto = frame[i + 9]
                if proto == 6:  # TCP
                    ip_off = i
                    break
    if ip_off < 0:
        return None

    ihl = (frame[ip_off] & 0x0F) * 4
    if ihl < 20:
        return None
    total_len = struct.unpack_from("!H", frame, ip_off + 2)[0]
    tcp_off = ip_off + ihl
    if tcp_off + 20 > len(frame):
        return None
    data_off = ((frame[tcp_off + 12] >> 4) & 0x0F) * 4
    if data_off < 20:
        return None
    payload_off = tcp_off + data_off
    # Prefer IP total length; fall back to remaining frame bytes
    end = ip_off + total_len
    if end < payload_off or end > len(frame):
        end = len(frame)
    if payload_off >= end:
        return b""
    return frame[payload_off:end]


def read_text_auto(path: Path) -> str:
    """Read pktmon text dumps (often UTF-16 LE with BOM on Chinese Windows)."""
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    # Heuristic: many NULs in first 200 bytes => utf-16le without reliable BOM handling
    sample = raw[:200]
    if sample and sample[1:2] == b"\x00":
        return raw.decode("utf-16le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def parse_etl2txt(
    path: Path,
    server_ip: str | None = None,
    server_port: int | None = None,
    local_port: int | None = None,
) -> list[TcpSeg]:
    """Parse pktmon etl2txt --hex output into TCP payload segments."""
    text = read_text_auto(path)
    lines = text.splitlines()
    segs: list[TcpSeg] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        ts_m = _TS_RE.match(line)
        if not ts_m:
            i += 1
            continue
        ts_val = ts_m.group("ts")
        # Summary may be on this line or the next indented line.
        m = _SUMMARY_RE.search(line)
        j = i + 1
        if not m and j < len(lines):
            m = _SUMMARY_RE.search(lines[j])
            if m:
                j += 1
        if not m:
            i += 1
            continue
        length = int(m.group("length"))
        # collect following hex lines
        hex_lines: list[str] = []
        while j < len(lines) and _HEX_LINE_RE.match(lines[j]):
            hex_lines.append(lines[j])
            j += 1
        frame = parse_hex_block(hex_lines) if hex_lines else b""
        payload = extract_ipv4_tcp_payload(frame) if frame else None
        if payload is None:
            payload = b""
        src_port = int(m.group("src_port"))
        dst_port = int(m.group("dst_port"))
        if local_port is not None and local_port not in (src_port, dst_port):
            i = j
            continue
        if server_ip is not None:
            ips = {m.group("src_ip"), m.group("dst_ip")}
            if server_ip not in ips:
                i = j
                continue
        if server_port is not None and server_port not in (src_port, dst_port):
            i = j
            continue
        # Prefer actual payload length; summary length is IP total minus headers-ish
        segs.append(
            TcpSeg(
                ts=ts_val,
                src_ip=m.group("src_ip"),
                src_port=src_port,
                dst_ip=m.group("dst_ip"),
                dst_port=dst_port,
                flags=m.group("flags"),
                payload=payload,
                frame_len=len(frame) if frame else length,
            )
        )
        i = j
    return segs


def group_streams(
    segs: list[TcpSeg],
    server_ip: str,
    server_port: int,
) -> dict[str, dict[str, bytes]]:
    """
    Group payloads by local flow key.

    Returns:
      { "192.168.0.2:61032": {"c2s": bytes, "s2c": bytes} }
    """
    out: dict[str, dict[str, bytearray]] = defaultdict(lambda: {"c2s": bytearray(), "s2c": bytearray()})
    for s in segs:
        if not s.payload:
            continue
        d = s.direction(server_ip, server_port)
        if d == "c2s":
            key = f"{s.src_ip}:{s.src_port}"
            out[key]["c2s"].extend(s.payload)
        elif d == "s2c":
            key = f"{s.dst_ip}:{s.dst_port}"
            out[key]["s2c"].extend(s.payload)
    return {k: {"c2s": bytes(v["c2s"]), "s2c": bytes(v["s2c"])} for k, v in out.items()}


def write_stream_bins(streams: dict[str, dict[str, bytes]], out_dir: Path, prefix: str) -> list[Path]:
    """Write per-flow c2s/s2c raw bins."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for flow, dirs in streams.items():
        safe = flow.replace(":", "_").replace(".", "_")
        for dname, blob in dirs.items():
            if not blob:
                continue
            p = out_dir / f"{prefix}_{safe}.{dname}.bin"
            p.write_bytes(blob)
            written.append(p)
    return written


def segs_to_jsonl(segs: list[TcpSeg], server_ip: str, server_port: int, path: Path) -> None:
    """Write segment-level jsonl (one TCP payload per line)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for idx, s in enumerate(segs, 1):
            if not s.payload:
                continue
            rec = {
                "seq": idx,
                "ts": s.ts,
                "dir": s.direction(server_ip, server_port),
                "src": f"{s.src_ip}:{s.src_port}",
                "dst": f"{s.dst_ip}:{s.dst_port}",
                "flags": s.flags,
                "len": len(s.payload),
                "hex": s.payload.hex(),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def summarize(segs: list[TcpSeg], server_ip: str, server_port: int) -> dict:
    c2s = [s for s in segs if s.direction(server_ip, server_port) == "c2s" and s.payload]
    s2c = [s for s in segs if s.direction(server_ip, server_port) == "s2c" and s.payload]
    return {
        "segments_total": len(segs),
        "c2s_with_payload": len(c2s),
        "s2c_with_payload": len(s2c),
        "c2s_bytes": sum(len(s.payload) for s in c2s),
        "s2c_bytes": sum(len(s.payload) for s in s2c),
        "c2s_sizes": [len(s.payload) for s in c2s[:50]],
        "s2c_sizes": [len(s.payload) for s in s2c[:50]],
        "c2s_heads": [s.payload[:16].hex() for s in c2s[:12]],
        "s2c_heads": [s.payload[:16].hex() for s in s2c[:12]],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract TCP payloads from pktmon etl2txt")
    ap.add_argument("path", help="path to .txt produced by pktmon etl2txt --hex")
    ap.add_argument("--server-ip", default=None)
    ap.add_argument("--server-port", type=int, default=None)
    ap.add_argument("--local-port", type=int, default=None, help="filter one client local port")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--prefix", default="stream")
    args = ap.parse_args()

    path = Path(args.path)
    server_ip = args.server_ip
    server_port = args.server_port
    if server_ip is None or server_port is None:
        from common.paths import read_current_server

        cfg = read_current_server()
        server_ip = server_ip or cfg["_ip"]
        server_port = server_port or int(cfg["_port"])

    segs = parse_etl2txt(path, server_ip=server_ip, server_port=server_port, local_port=args.local_port)
    out_dir = Path(args.out_dir) if args.out_dir else path.parent
    streams = group_streams(segs, server_ip, server_port)
    written = write_stream_bins(streams, out_dir, args.prefix)
    jsonl = out_dir / f"{args.prefix}.jsonl"
    segs_to_jsonl(segs, server_ip, server_port, jsonl)
    summary = summarize(segs, server_ip, server_port)
    summary_path = out_dir / f"{args.prefix}_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"server={server_ip}:{server_port}")
    print(f"segments={summary['segments_total']} c2s_payload={summary['c2s_with_payload']} "
          f"s2c_payload={summary['s2c_with_payload']}")
    print(f"c2s_bytes={summary['c2s_bytes']} s2c_bytes={summary['s2c_bytes']}")
    print("c2s heads:", summary["c2s_heads"][:5])
    print("s2c heads:", summary["s2c_heads"][:5])
    print("wrote", jsonl)
    print("wrote", summary_path)
    for p in written:
        print("wrote", p, p.stat().st_size)


if __name__ == "__main__":
    main()
