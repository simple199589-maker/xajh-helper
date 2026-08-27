# -*- coding: utf-8 -*-
"""
Minimal bidirectional TCP proxy for game packet capture.

Usage:
  python packet/tcp_proxy.py --listen 127.0.0.1:16598 --target 193.112.93.158:6598
  python packet/tcp_proxy.py --from-game-config

Notes:
  - Does not decrypt TLS/custom crypto; records raw bytes only.
  - For real client traffic you must point the client at the listen address
    (hosts/firewall redirect, or modify serverlist if the client allows it).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.paths import CAPTURE_DIR, read_current_server  # noqa: E402


def ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i : i + width]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{i:08x}  {hexpart:<{width*3}}  {asc}")
    return "\n".join(lines)


class SessionRecorder:
    """Append-only jsonl + side-by-side raw bin writer."""

    def __init__(self, out_dir: Path, session_id: str):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self.jsonl = (out_dir / f"{session_id}.jsonl").open("a", encoding="utf-8")
        self.raw_c2s = (out_dir / f"{session_id}.c2s.bin").open("ab")
        self.raw_s2c = (out_dir / f"{session_id}.s2c.bin").open("ab")
        self.seq = 0

    def write(self, direction: str, data: bytes) -> None:
        self.seq += 1
        rec = {
            "seq": self.seq,
            "t": time.time(),
            "dir": direction,
            "len": len(data),
            "hex": data.hex(),
        }
        self.jsonl.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.jsonl.flush()
        if direction == "c2s":
            self.raw_c2s.write(data)
            self.raw_c2s.flush()
        else:
            self.raw_s2c.write(data)
            self.raw_s2c.flush()
        print(f"[{self.session_id}] {direction} #{self.seq} len={len(data)}")
        print(hexdump(data[:256]))
        if len(data) > 256:
            print(f"... ({len(data) - 256} more bytes)")

    def close(self) -> None:
        for f in (self.jsonl, self.raw_c2s, self.raw_s2c):
            try:
                f.close()
            except Exception:
                pass


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, direction: str, rec: SessionRecorder):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            rec.write(direction, data)
            writer.write(data)
            await writer.drain()
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def handle_client(
    local_reader: asyncio.StreamReader,
    local_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
    out_dir: Path,
):
    peer = local_writer.get_extra_info("peername")
    session_id = f"sess_{ts()}_{peer[1] if peer else 0}"
    rec = SessionRecorder(out_dir, session_id)
    print(f"client {peer} -> {target_host}:{target_port} session={session_id}")
    try:
        remote_reader, remote_writer = await asyncio.open_connection(target_host, target_port)
    except Exception as e:
        print("connect target failed:", e)
        local_writer.close()
        rec.close()
        return

    t1 = asyncio.create_task(pipe(local_reader, remote_writer, "c2s", rec))
    t2 = asyncio.create_task(pipe(remote_reader, local_writer, "s2c", rec))
    await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in (t1, t2):
        t.cancel()
    rec.close()
    print("session closed", session_id)


async def run_proxy(listen_host: str, listen_port: int, target_host: str, target_port: int, out_dir: Path):
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, target_host, target_port, out_dir),
        listen_host,
        listen_port,
    )
    sockets = ", ".join(str(s.getsockname()) for s in (server.sockets or []))
    print(f"listening on {sockets} -> {target_host}:{target_port}")
    print(f"captures -> {out_dir}")
    async with server:
        await server.serve_forever()


def parse_hostport(value: str) -> tuple[str, int]:
    host, port_s = value.rsplit(":", 1)
    return host, int(port_s)


def main() -> None:
    ap = argparse.ArgumentParser(description="Game TCP capture proxy")
    ap.add_argument("--listen", default="127.0.0.1:16598", help="local listen host:port")
    ap.add_argument("--target", default=None, help="upstream host:port")
    ap.add_argument("--from-game-config", action="store_true", help="read target from userdata/currentserver.ini")
    ap.add_argument("--out", default=str(CAPTURE_DIR / "tcp"), help="capture output directory")
    args = ap.parse_args()

    if args.from_game_config or not args.target:
        cfg = read_current_server()
        target_host = cfg["_ip"]
        target_port = int(cfg["_port"])
        print("target from game config:", target_host, target_port, cfg.get("CurrentServer"))
    else:
        target_host, target_port = parse_hostport(args.target)

    listen_host, listen_port = parse_hostport(args.listen)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_proxy(listen_host, listen_port, target_host, target_port, out_dir))


if __name__ == "__main__":
    main()
