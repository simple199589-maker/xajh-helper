# -*- coding: utf-8 -*-
"""
Capture game TCP traffic around a player action (pickup / open chest / etc).

Uses Windows built-in pktmon (no Npcap required). Flow:

1. Optional quiet baseline window
2. Action window while you perform pickup/open-chest in game
3. Convert ETL -> txt/pcapng, extract TCP payloads, optional diff

Usage examples:
  # Interactive: baseline 5s, then action until Enter
  python packet/action_capture.py --action pickup --baseline 5

  # Fixed action duration
  python packet/action_capture.py --action open_chest --baseline 3 --duration 8

  # Capture only one xajh.exe local port (recommended when multi-client)
  python packet/action_capture.py --action pickup --pid 13568 --baseline 4 --duration 10

After capture:
  python packet/stream_extract.py captures/tcp/act_pickup_xxx_action.txt
  python packet/action_diff.py --dir captures/tcp --prefix act_pickup_xxx
  python packet/frame_split.py captures/tcp/act_pickup_xxx_action.jsonl --from-jsonl
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.paths import CAPTURE_DIR, ISSUE_DIR, read_current_server  # noqa: E402


def ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


LogFn = Callable[[str], None]


def _default_log(msg: str) -> None:
    print(msg)


def run(
    cmd: list[str],
    check: bool = True,
    log: LogFn | None = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess command and optionally log stdout/stderr. @author by ak"""
    log = log or _default_log
    log("+ " + " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def ensure_pktmon() -> str:
    """Return pktmon path or raise RuntimeError. @author by ak"""
    path = shutil.which("pktmon")
    if not path:
        raise RuntimeError("pktmon not found (Windows 10 1809+ required)")
    return path


def pick_local_port(
    pid: int | None,
    server_ip: str,
    server_port: int,
    log: LogFn | None = None,
) -> int | None:
    """Pick ESTABLISHED local port for game client -> server. @author by ak"""
    log = log or _default_log
    try:
        import psutil
    except ImportError:
        return None
    candidates: list[tuple[int, int]] = []  # (pid, local_port)
    for p in psutil.process_iter(["pid", "name"]):
        try:
            name = (p.info.get("name") or "").lower()
            if pid is not None and p.pid != pid:
                continue
            if pid is None and name != "xajh.exe":
                continue
            for c in p.net_connections(kind="inet"):
                if c.status != "ESTABLISHED" or not c.raddr:
                    continue
                if c.raddr.ip == server_ip and c.raddr.port == server_port and c.laddr:
                    candidates.append((p.pid, c.laddr.port))
        except (psutil.Error, psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    if not candidates:
        return None
    if pid is not None:
        for p, port in candidates:
            if p == pid:
                log(f"local port for pid={pid}: {port}")
                return port
    # prefer first
    p, port = candidates[0]
    if len(candidates) > 1:
        log(f"multiple game sockets found: {candidates}; using pid={p} port={port}")
        log("hint: pass --pid / --local-port to pin one client")
    else:
        log(f"local port for pid={p}: {port}")
    return port


def pktmon_reset(log: LogFn | None = None) -> None:
    """Stop capture and clear filters. @author by ak"""
    run(["pktmon", "stop"], check=False, log=log)
    run(["pktmon", "filter", "remove"], check=False, log=log)


def pktmon_set_filter(server_ip: str, server_port: int, log: LogFn | None = None) -> None:
    """Install TCP filter for game gateway. @author by ak"""
    log = log or _default_log
    pktmon_reset(log=log)
    # Match either direction of game server TCP:6598
    r = run(
        [
            "pktmon",
            "filter",
            "add",
            "XAJHGame",
            "-i",
            server_ip,
            "-t",
            "TCP",
            "-p",
            str(server_port),
        ],
        log=log,
    )
    if r.stdout:
        log(r.stdout.strip())
    if r.stderr:
        log(r.stderr.strip())


def pktmon_start(etl_path: Path, log: LogFn | None = None) -> None:
    """Start full-packet NIC capture into ETL. @author by ak"""
    log = log or _default_log
    etl_path.parent.mkdir(parents=True, exist_ok=True)
    if etl_path.exists():
        etl_path.unlink()
    # --pkt-size 0: full packet; --comp nics: only NIC path to reduce duplicates
    r = run(
        [
            "pktmon",
            "start",
            "--capture",
            "--comp",
            "nics",
            "--pkt-size",
            "0",
            "--file-name",
            str(etl_path),
            "--file-size",
            "256",
        ],
        log=log,
    )
    if r.returncode != 0:
        raise RuntimeError(f"pktmon start failed: {r.stderr or r.stdout}")
    log(f"capturing -> {etl_path}")


def pktmon_stop(log: LogFn | None = None) -> None:
    """Stop pktmon capture session. @author by ak"""
    log = log or _default_log
    r = run(["pktmon", "stop"], check=False, log=log)
    if r.stdout:
        log(r.stdout.strip())
    if r.stderr:
        log(r.stderr.strip())


def convert_outputs(etl_path: Path, log: LogFn | None = None) -> dict[str, Path]:
    """ETL -> txt (hex) + pcapng. @author by ak"""
    log = log or _default_log
    txt = etl_path.with_suffix(".txt")
    pcap = etl_path.with_suffix(".pcapng")
    r1 = run(
        ["pktmon", "etl2txt", str(etl_path), "--out", str(txt), "--hex", "--timestamp"],
        check=False,
        log=log,
    )
    if r1.returncode != 0:
        log("etl2txt warn: " + (r1.stderr or r1.stdout or ""))
    r2 = run(
        ["pktmon", "etl2pcap", str(etl_path), "--out", str(pcap)],
        check=False,
        log=log,
    )
    if r2.returncode != 0:
        log("etl2pcap warn: " + (r2.stderr or r2.stdout or ""))
    return {"etl": etl_path, "txt": txt, "pcapng": pcap}


def extract_stream(
    txt_path: Path,
    prefix: str,
    local_port: int | None,
    server_ip: str,
    server_port: int,
) -> dict:
    """Parse etl2txt and write jsonl/bins/summary. @author by ak"""
    from packet.stream_extract import (
        group_streams,
        parse_etl2txt,
        segs_to_jsonl,
        summarize,
        write_stream_bins,
    )

    segs = parse_etl2txt(
        txt_path,
        server_ip=server_ip,
        server_port=server_port,
        local_port=local_port,
    )
    streams = group_streams(segs, server_ip, server_port)
    out_dir = txt_path.parent
    written = write_stream_bins(streams, out_dir, prefix)
    jsonl = out_dir / f"{prefix}.jsonl"
    segs_to_jsonl(segs, server_ip, server_port, jsonl)
    summary = summarize(segs, server_ip, server_port)
    summary_path = out_dir / f"{prefix}_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "jsonl": str(jsonl),
        "summary": summary,
        "summary_path": str(summary_path),
        "bins": [str(p) for p in written],
        "segments": len(segs),
    }


def capture_window(
    label: str,
    out_dir: Path,
    wait_sec: float | None,
    wait_enter: bool,
    log: LogFn | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Path:
    """
    Capture one window into ETL.

    GUI mode: pass should_stop() True when user clicks stop; do not use wait_enter.
    CLI mode: wait_enter waits for stdin, otherwise sleep wait_sec.
    @author by ak
    """
    log = log or _default_log
    etl = out_dir / f"{label}.etl"
    pktmon_start(etl, log=log)
    try:
        if should_stop is not None:
            # polled stop for GUI / programmatic control
            deadline = None
            if wait_sec is not None and wait_sec > 0:
                deadline = time.time() + float(wait_sec)
            log(f"[{label}] capture running (stop via UI or timeout)")
            while True:
                if should_stop():
                    log(f"[{label}] stop requested")
                    break
                if deadline is not None and time.time() >= deadline:
                    log(f"[{label}] duration reached")
                    break
                time.sleep(0.2)
        elif wait_enter:
            log("")
            log("=" * 60)
            log(f"[{label}] capture running.")
            log("In game: do the action NOW (pickup item / open chest).")
            log("When finished, press Enter here to stop this window.")
            log("=" * 60)
            try:
                input()
            except EOFError:
                time.sleep(wait_sec or 8)
        else:
            sec = float(wait_sec or 5)
            log(f"[{label}] capturing for {sec:.1f}s ...")
            time.sleep(sec)
    finally:
        pktmon_stop(log=log)
    return etl


def write_notes(meta: dict, out_path: Path) -> None:
    """Write markdown notes for a capture session. @author by ak"""
    lines = [
        f"# Action capture: {meta.get('action')}",
        "",
        f"- time: {meta.get('t')}",
        f"- server: {meta.get('server_ip')}:{meta.get('server_port')}",
        f"- local_port: {meta.get('local_port')}",
        f"- pid: {meta.get('pid')}",
        f"- baseline_sec: {meta.get('baseline_sec')}",
        f"- action_mode: {meta.get('action_mode')}",
        "",
        "## Files",
        "",
    ]
    for k, v in (meta.get("files") or {}).items():
        lines.append(f"- {k}: `{v}`")
    lines.extend(
        [
            "",
            "## Next analysis",
            "",
            "```bash",
            f"python packet/action_diff.py --dir {meta.get('out_dir')} --prefix {meta.get('prefix')}",
            f"python packet/frame_split.py {meta.get('files', {}).get('action_jsonl', '')} --from-jsonl",
            "```",
            "",
            "## Operator notes",
            "",
            meta.get("operator_notes") or "TODO: what was picked / which chest / map",
            "",
        ]
    )
    out_path.write_text("\n".join(lines), encoding="utf-8")


def run_action_capture(
    action: str = "pickup",
    baseline_sec: float = 5.0,
    duration_sec: float | None = 10.0,
    pid: int | None = None,
    local_port: int | None = None,
    server_ip: str | None = None,
    server_port: int | None = None,
    out_dir: Path | str | None = None,
    notes: str = "",
    do_diff: bool = True,
    log: LogFn | None = None,
    should_stop: Callable[[], bool] | None = None,
    wait_enter_for_action: bool = False,
) -> dict:
    """
    Programmatic capture API used by CLI and GUI.

    - If wait_enter_for_action and no should_stop: action window waits for Enter.
    - If should_stop provided: polled stop (GUI).
    - Else duration_sec sleeps for action window.
    @author by ak
    """
    log = log or _default_log
    ensure_pktmon()
    cfg = read_current_server()
    server_ip = server_ip or cfg["_ip"]
    server_port = int(server_port or cfg["_port"])
    resolved_port = local_port or pick_local_port(pid, server_ip, server_port, log=log)

    stamp = ts()
    safe_action = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in action) or "action"
    prefix = f"act_{safe_action}_{stamp}"
    out_path = Path(out_dir) if out_dir else (CAPTURE_DIR / "tcp")
    out_path.mkdir(parents=True, exist_ok=True)

    log(f"action={action}")
    log(f"server={server_ip}:{server_port} local_port={resolved_port} pid={pid}")
    log(f"output={out_path / prefix}_*")

    pktmon_set_filter(server_ip, server_port, log=log)
    files: dict[str, str] = {}
    extract_meta: dict = {}

    try:
        if baseline_sec and baseline_sec > 0:
            log(f"[baseline] stay idle for {baseline_sec:.1f}s (no pickup/open)")
            base_etl = capture_window(
                f"{prefix}_base",
                out_path,
                baseline_sec,
                wait_enter=False,
                log=log,
                should_stop=should_stop,
            )
            base_paths = convert_outputs(base_etl, log=log)
            files["base_etl"] = str(base_paths["etl"])
            files["base_txt"] = str(base_paths["txt"])
            files["base_pcapng"] = str(base_paths["pcapng"])
            if base_paths["txt"].exists():
                ex = extract_stream(
                    base_paths["txt"],
                    f"{prefix}_base",
                    resolved_port,
                    server_ip,
                    server_port,
                )
                extract_meta["base"] = ex
                files["base_jsonl"] = ex["jsonl"]
                log(
                    f"[baseline] c2s={ex['summary']['c2s_with_payload']} "
                    f"s2c={ex['summary']['s2c_with_payload']} "
                    f"bytes={ex['summary']['c2s_bytes']}/{ex['summary']['s2c_bytes']}"
                )

        # action window
        log("[action] window starting — perform open_chest / pickup in game now")
        if should_stop is not None:
            act_etl = capture_window(
                f"{prefix}_action",
                out_path,
                duration_sec,
                wait_enter=False,
                log=log,
                should_stop=should_stop,
            )
            action_mode = f"ui_stop duration={duration_sec}"
        elif wait_enter_for_action:
            act_etl = capture_window(
                f"{prefix}_action",
                out_path,
                duration_sec,
                wait_enter=True,
                log=log,
            )
            action_mode = "enter"
        else:
            act_etl = capture_window(
                f"{prefix}_action",
                out_path,
                duration_sec if duration_sec is not None else 10.0,
                wait_enter=False,
                log=log,
            )
            action_mode = f"duration={duration_sec if duration_sec is not None else 10.0}"

        act_paths = convert_outputs(act_etl, log=log)
        files["action_etl"] = str(act_paths["etl"])
        files["action_txt"] = str(act_paths["txt"])
        files["action_pcapng"] = str(act_paths["pcapng"])
        if act_paths["txt"].exists():
            ex = extract_stream(
                act_paths["txt"],
                f"{prefix}_action",
                resolved_port,
                server_ip,
                server_port,
            )
            extract_meta["action"] = ex
            files["action_jsonl"] = ex["jsonl"]
            log(
                f"[action] c2s={ex['summary']['c2s_with_payload']} "
                f"s2c={ex['summary']['s2c_with_payload']} "
                f"bytes={ex['summary']['c2s_bytes']}/{ex['summary']['s2c_bytes']}"
            )
            if ex["summary"]["c2s_heads"]:
                log("  c2s heads: " + str(ex["summary"]["c2s_heads"][:6]))
            if ex["summary"]["s2c_heads"]:
                log("  s2c heads: " + str(ex["summary"]["s2c_heads"][:6]))
    finally:
        pktmon_reset(log=log)

    report = None
    if do_diff and files.get("base_jsonl") and files.get("action_jsonl"):
        from packet.action_diff import compare, load_jsonl, print_report

        report = compare(load_jsonl(Path(files["base_jsonl"])), load_jsonl(Path(files["action_jsonl"])))
        print_report(report)
        diff_path = out_path / f"{prefix}_diff.json"
        diff_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        files["diff_json"] = str(diff_path)
        log(f"wrote {diff_path}")

    meta = {
        "t": stamp,
        "action": action,
        "server_ip": server_ip,
        "server_port": server_port,
        "local_port": resolved_port,
        "pid": pid,
        "baseline_sec": baseline_sec,
        "action_mode": action_mode,
        "out_dir": str(out_path),
        "prefix": prefix,
        "files": files,
        "extract": extract_meta,
        "operator_notes": notes or "",
        "diff": report,
    }
    meta_path = out_path / f"{prefix}_meta.json"
    # strip large nested report from meta file copy size control
    meta_for_file = dict(meta)
    if meta_for_file.get("diff") is not None:
        meta_for_file["diff"] = {"path": files.get("diff_json"), "present": True}
    meta_path.write_text(json.dumps(meta_for_file, ensure_ascii=False, indent=2), encoding="utf-8")
    notes_path = ISSUE_DIR / "packets" / f"{prefix}.md"
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    write_notes(meta, notes_path)
    files["meta_json"] = str(meta_path)
    files["notes_md"] = str(notes_path)
    log(f"wrote {meta_path}")
    log(f"wrote {notes_path}")
    log("Done. Prefer action c2s packets that appear only in action window (see *_diff.json).")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description="Capture packets around loot/chest actions via pktmon")
    ap.add_argument(
        "--action",
        default="pickup",
        help="action tag: pickup | open_chest | gather | custom_name",
    )
    ap.add_argument("--baseline", type=float, default=5.0, help="quiet baseline seconds (0=skip)")
    ap.add_argument(
        "--duration",
        type=float,
        default=None,
        help="action window seconds; default: wait for Enter",
    )
    ap.add_argument("--pid", type=int, default=None, help="xajh.exe pid to pin local port")
    ap.add_argument("--local-port", type=int, default=None)
    ap.add_argument("--server-ip", default=None)
    ap.add_argument("--server-port", type=int, default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--notes", default="", help="free text written into capture notes")
    ap.add_argument("--no-diff", action="store_true")
    args = ap.parse_args()

    wait_enter = args.duration is None
    run_action_capture(
        action=args.action,
        baseline_sec=args.baseline,
        duration_sec=args.duration,
        pid=args.pid,
        local_port=args.local_port,
        server_ip=args.server_ip,
        server_port=args.server_port,
        out_dir=args.out_dir,
        notes=args.notes,
        do_diff=not args.no_diff,
        wait_enter_for_action=wait_enter,
    )


if __name__ == "__main__":
    main()
