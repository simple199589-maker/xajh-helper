# -*- coding: utf-8 -*-
"""Capture game TCP via dumpcap/Npcap (when available). @author by ak"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.paths import CAPTURE_DIR, ISSUE_DIR, read_current_server  # noqa: E402


DEFAULT_DUMPCAP = Path(r"D:\software\Wireshark\dumpcap.exe")
DEFAULT_TSHARK = Path(r"D:\software\Wireshark\tshark.exe")


def ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def find_dumpcap() -> Path:
    for p in (DEFAULT_DUMPCAP, Path(shutil.which("dumpcap") or "")):
        if p and p.is_file():
            return p
    raise RuntimeError("dumpcap not found")


def list_ifaces(dumpcap: Path) -> str:
    r = subprocess.run([str(dumpcap), "-D"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (r.stdout or "") + (r.stderr or "")


def pick_iface(dumpcap: Path, prefer: str | None = None) -> str:
    """Return dumpcap interface index as string. Prefer WLAN / not loopback. @author by ak"""
    text = list_ifaces(dumpcap)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and ln[0].isdigit()]
    if prefer:
        for ln in lines:
            if prefer.lower() in ln.lower():
                return ln.split(".", 1)[0].strip()
    for key in ("WLAN", "Wi-Fi", "WiFi", "以太网", "Ethernet"):
        for ln in lines:
            if key.lower() in ln.lower() or key in ln:
                return ln.split(".", 1)[0].strip()
    if not lines:
        raise RuntimeError("no dumpcap interfaces:\n" + text)
    return lines[0].split(".", 1)[0].strip()


def run_capture(
    action: str,
    duration: float,
    pid: int | None,
    iface: str | None,
    notes: str,
) -> dict:
    dumpcap = find_dumpcap()
    cfg = read_current_server()
    sip, sport = cfg["_ip"], int(cfg["_port"])
    iface_id = iface or pick_iface(dumpcap)
    stamp = ts()
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in action) or "action"
    prefix = f"act_{safe}_{stamp}_dumpcap"
    out_dir = CAPTURE_DIR / "tcp"
    out_dir.mkdir(parents=True, exist_ok=True)
    pcap = out_dir / f"{prefix}.pcapng"

    # resolve local port for display only
    local_port = None
    if pid:
        try:
            import psutil

            for c in psutil.Process(pid).net_connections(kind="inet"):
                if c.status == "ESTABLISHED" and c.raddr and c.raddr.ip == sip and c.raddr.port == sport:
                    local_port = c.laddr.port
                    break
        except Exception:
            pass

    bpf = f"host {sip} and tcp port {sport}"
    print(f"dumpcap={dumpcap}")
    print(f"iface={iface_id} filter={bpf}")
    print(f"action={action} duration={duration}s pid={pid} local_port={local_port}")
    print(f"out={pcap}")
    print(">>> DO THE CHEST/OPEN ACTION IN GAME NOW <<<")

    cmd = [
        str(dumpcap),
        "-i",
        str(iface_id),
        "-f",
        bpf,
        "-a",
        f"duration:{int(max(1, duration))}",
        "-w",
        str(pcap),
        "-q",
    ]
    print("+", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout)
    print(r.stderr)
    if r.returncode not in (0, 1):  # dumpcap sometimes 1 with stats
        print("warn returncode", r.returncode)

    # summarize with tshark if present
    summary: dict = {"pcap": str(pcap), "bytes": pcap.stat().st_size if pcap.exists() else 0}
    tshark = DEFAULT_TSHARK if DEFAULT_TSHARK.is_file() else Path(shutil.which("tshark") or "")
    if tshark and tshark.is_file() and pcap.exists():
        def fields(disp_filter: str) -> list[tuple[str, str, str]]:
            rr = subprocess.run(
                [
                    str(tshark),
                    "-r",
                    str(pcap),
                    "-Y",
                    disp_filter,
                    "-T",
                    "fields",
                    "-e",
                    "frame.time_relative",
                    "-e",
                    "tcp.len",
                    "-e",
                    "tcp.payload",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            out = []
            for line in (rr.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    out.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
            return out

        c2s = fields(f"ip.dst=={sip} and tcp.dstport=={sport} and tcp.len>0")
        s2c = fields(f"ip.src=={sip} and tcp.srcport=={sport} and tcp.len>0")
        summary["c2s_count"] = len(c2s)
        summary["s2c_count"] = len(s2c)
        summary["c2s_lens"] = [int(x[1]) for x in c2s if x[1].isdigit()]
        summary["s2c_lens"] = [int(x[1]) for x in s2c if x[1].isdigit()]
        summary["c2s_samples"] = [
            {"t": a, "len": b, "hex": (c or "").replace(":", "")[:64]} for a, b, c in c2s[:30]
        ]
        summary["s2c_samples"] = [
            {"t": a, "len": b, "hex": (c or "").replace(":", "")[:64]} for a, b, c in s2c[:15]
        ]
        print(f"tshark c2s_with_payload={len(c2s)} s2c_with_payload={len(s2c)}")
        for row in summary["c2s_samples"][:12]:
            print(f"  c2s t={row['t']} len={row['len']} head={row['hex'][:24]}")

    meta = {
        "t": stamp,
        "action": action,
        "backend": "dumpcap",
        "server_ip": sip,
        "server_port": sport,
        "local_port": local_port,
        "pid": pid,
        "iface": iface_id,
        "duration": duration,
        "notes": notes,
        "summary": summary,
        "pcap": str(pcap),
    }
    meta_path = out_dir / f"{prefix}_meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    note_path = ISSUE_DIR / "packets" / f"{prefix}.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "\n".join(
            [
                f"# dumpcap capture: {action}",
                "",
                f"- time: {stamp}",
                f"- server: {sip}:{sport}",
                f"- pid: {pid} local_port: {local_port}",
                f"- iface: {iface_id}",
                f"- duration: {duration}s",
                f"- pcap: `{pcap}`",
                f"- c2s: {summary.get('c2s_count')} s2c: {summary.get('s2c_count')}",
                "",
                "## notes",
                notes or "",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print("wrote", meta_path)
    print("wrote", note_path)
    return meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", default="open_chest")
    ap.add_argument("--duration", type=float, default=25.0)
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--iface", default=None, help="dumpcap interface index")
    ap.add_argument("--notes", default="")
    args = ap.parse_args()
    run_capture(args.action, args.duration, args.pid, args.iface, args.notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
