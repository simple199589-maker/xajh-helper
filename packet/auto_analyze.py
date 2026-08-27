# -*- coding: utf-8 -*-
"""
Automatic x32dbg / packet log unpack analysis.

Turn a single runtime log (or multiple) into:
  - PLAIN / SEND10 / UPD_* event stream
  - plain↔cipher pairs (SEND10 lookback)
  - known shell labels (open_chest 10B etc.)
  - core hook points + "where is plain / where is cipher"
  - markdown + json reports under .issues/packets/

CLI:
  python -m packet.auto_analyze
  python -m packet.auto_analyze path\\to\\log.txt
  python -m packet.auto_analyze --latest

@author by ak
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from packet.crypto_knowledge import (
    CORE_HOOKS,
    ROLE_GUIDE,
    core_points_markdown,
    knowledge_dict,
    match_shell,
)

LogFn = Callable[[str], None]

# Default locations (project + common x32dbg installs)
DEFAULT_LOG_CANDIDATES: tuple[str, ...] = (
    ".issues/packets/x32dbg_runtime.log",
    ".issues/packets/x32dbg_log_132637.txt",
    ".issues/packets/x32dbg_log_081903.txt",
)

# Tag patterns observed in x32dbg GUI log text / scripts
_RE_TAGGED = re.compile(
    r"\b(PLAIN|SEND10|UPD_IN|UPD_OUT|SETKEY|SEND_W)\b\s*[=:]?\s*([0-9a-fA-F]*)",
    re.I,
)
_RE_SEND_HEX_EQ = re.compile(
    r"SEND10\s+hex=([0-9a-fA-F]+)",
    re.I,
)
_RE_HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")


def _default_log(msg: str) -> None:
    print(msg, flush=True)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def packets_dir(root: Path | None = None) -> Path:
    r = root or project_root()
    d = r / ".issues" / "packets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def norm_hex(h: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", h or "").lower()


def load_text(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin1"):
        try:
            return path.read_text(encoding=enc, errors="replace")
        except Exception:
            continue
    return ""


def load_lines(path: Path) -> list[str]:
    return load_text(path).splitlines()


def discover_logs(
    root: Path | None = None,
    extra: Iterable[Path | str] | None = None,
    x32dbg_dirs: Iterable[Path | str] | None = None,
) -> list[Path]:
    """
    Find candidate x32dbg / runtime logs.

    Order: explicit extra → project packets → optional x32dbg install logs.
    @author by ak
    """
    root = root or project_root()
    found: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        try:
            rp = str(p.resolve())
        except Exception:
            rp = str(p)
        if rp in seen or not p.is_file():
            return
        seen.add(rp)
        found.append(p)

    if extra:
        for e in extra:
            add(Path(e))

    pkt = packets_dir(root)
    for name in (
        "x32dbg_runtime.log",
        "x32dbg_log_132637.txt",
        "x32dbg_log_081903.txt",
    ):
        add(pkt / name)

    # dated logs under .issues/packets
    try:
        for p in sorted(pkt.glob("x32dbg_log_*.txt"), key=lambda x: x.stat().st_mtime, reverse=True):
            add(p)
        for p in sorted(pkt.glob("log-*.txt"), key=lambda x: x.stat().st_mtime, reverse=True):
            add(p)
    except Exception:
        pass

    # common x32dbg release log paths on this machine
    defaults = list(x32dbg_dirs or [])
    defaults.extend(
        [
            Path(r"D:\software\x64dbg\release\x32"),
            Path(r"C:\x64dbg\release\x32"),
            Path(r"D:\x64dbg\release\x32"),
        ]
    )
    for d in defaults:
        dp = Path(d)
        if not dp.is_dir():
            continue
        try:
            for p in sorted(dp.glob("log-*.txt"), key=lambda x: x.stat().st_mtime, reverse=True)[:8]:
                add(p)
        except Exception:
            continue

    return found


def latest_log(root: Path | None = None) -> Path | None:
    logs = discover_logs(root)
    if not logs:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime if p.exists() else 0)


@dataclass
class PacketEvent:
    """One tagged log event. @author by ak"""

    line: int
    kind: str  # PLAIN / SEND10 / UPD_IN / UPD_OUT / SETKEY
    hex_full: str
    hex10: str
    byte_len: int
    raw: str
    shell: str = ""
    shell_meaning: str = ""
    role_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PairHit:
    """SEND10 paired with best preceding PLAIN. @author by ak"""

    send10: str
    send_line: int
    plain_hex10: str
    plain_full: str
    plain_line: int
    gap_lines: int
    shell: str
    shell_meaning: str
    same_plain_as_frozen: bool
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnalyzeResult:
    """Full auto-unpack report payload. @author by ak"""

    ok: bool
    source: str
    sources: list[str]
    stamp: str
    counts: dict[str, int]
    events: list[PacketEvent]
    pairs: list[PairHit]
    plain_top: list[tuple[str, int]]
    send_top: list[tuple[str, int]]
    core_markdown: str
    findings: list[str]
    files: dict[str, str] = field(default_factory=dict)
    knowledge: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def summary_line(self) -> str:
        if not self.ok:
            return f"分析失败: {self.error or 'unknown'}"
        p = self.counts.get("PLAIN", 0) + self.counts.get("UPD_IN", 0)
        s = self.counts.get("SEND10", 0)
        hit = sum(1 for x in self.pairs if x.plain_hex10)
        shell_hit = sum(1 for x in self.pairs if x.shell)
        return (
            f"PLAIN/UPD_IN={p} SEND10={s} 配对={hit}/{s} "
            f"已知壳={shell_hit} | {Path(self.source).name if self.source else '-'}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "sources": self.sources,
            "stamp": self.stamp,
            "counts": self.counts,
            "events": [e.as_dict() for e in self.events],
            "pairs": [p.as_dict() for p in self.pairs],
            "plain_top": [{"hex10": h, "n": n} for h, n in self.plain_top],
            "send_top": [{"hex10": h, "n": n} for h, n in self.send_top],
            "findings": self.findings,
            "files": self.files,
            "knowledge": self.knowledge,
            "error": self.error,
            "summary": self.summary_line(),
        }


def parse_events(lines: list[str]) -> list[PacketEvent]:
    """Extract tagged hex events from x32dbg-style log lines. @author by ak"""
    events: list[PacketEvent] = []
    for i, line in enumerate(lines, 1):
        # structured SEND10 hex=
        m_send = _RE_SEND_HEX_EQ.search(line)
        if m_send:
            hx = norm_hex(m_send.group(1))
            events.append(_make_event(i, "SEND10", hx, line))
            continue

        m = _RE_TAGGED.search(line)
        if not m:
            continue
        kind = m.group(1).upper()
        hx = norm_hex(m.group(2) or "")
        # some lines: "PLAIN hex=..." after tag
        if not hx:
            m2 = re.search(r"hex=([0-9a-fA-F]+)", line, re.I)
            if m2:
                hx = norm_hex(m2.group(1))
        if not hx:
            # tag present but no hex — still record for density?
            continue
        if kind == "UPD_IN":
            kind = "PLAIN"  # unify inbound label for pairing
        events.append(_make_event(i, kind, hx, line))
    return events


def _make_event(line_no: int, kind: str, hx: str, raw: str) -> PacketEvent:
    blen = len(hx) // 2
    h10 = hx[:20] if len(hx) >= 20 else hx
    shell = match_shell(h10 if kind in ("PLAIN", "UPD_OUT") else h10)
    role = ROLE_GUIDE.get(kind if kind != "PLAIN" else "PLAIN", "")
    return PacketEvent(
        line=line_no,
        kind=kind,
        hex_full=hx,
        hex10=h10,
        byte_len=blen,
        raw=raw.strip()[:240],
        shell=shell.name if shell else "",
        shell_meaning=shell.meaning if shell else "",
        role_hint=role,
    )


def pair_send10_plain(
    events: list[PacketEvent],
    lookback: int = 80,
) -> list[PairHit]:
    """
    For each SEND10, pick best preceding PLAIN in lookback window.

    Preference: known open_chest shell → starts with 22 → len~10/16 → nearest.
    @author by ak
    """
    plains = [e for e in events if e.kind == "PLAIN"]
    sends = [e for e in events if e.kind == "SEND10"]
    pairs: list[PairHit] = []

    for s in sends:
        cands = [p for p in plains if s.line - lookback <= p.line < s.line]
        cands_near = list(reversed(cands))  # nearest first

        def rank(p: PacketEvent) -> tuple:
            sh = 0
            if p.shell == "open_chest_c2s10":
                sh = 0
            elif p.hex10.startswith("22080721"):
                sh = 1
            elif p.hex10.startswith("22"):
                sh = 2
            else:
                sh = 3
            len_score = 0 if p.byte_len in (10, 16) else (1 if 8 <= p.byte_len <= 24 else 2)
            return (sh, len_score, s.line - p.line)

        ranked = sorted(cands_near, key=rank)
        best = ranked[0] if ranked else None

        # unique type22 / shell heads in window for report
        cand_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for p in ranked[:12]:
            if p.hex10 in seen:
                continue
            seen.add(p.hex10)
            cand_rows.append(
                {
                    "hex10": p.hex10,
                    "full": p.hex_full[:48],
                    "line": p.line,
                    "gap": s.line - p.line,
                    "shell": p.shell,
                }
            )

        shell_name = best.shell if best else ""
        shell_meaning = best.shell_meaning if best else ""
        plain10 = best.hex10 if best else ""
        frozen = plain10.startswith("22080721000100000000")
        pairs.append(
            PairHit(
                send10=s.hex10,
                send_line=s.line,
                plain_hex10=plain10,
                plain_full=(best.hex_full if best else "")[:64],
                plain_line=best.line if best else 0,
                gap_lines=(s.line - best.line) if best else -1,
                shell=shell_name,
                shell_meaning=shell_meaning,
                same_plain_as_frozen=frozen,
                candidates=cand_rows,
            )
        )
    return pairs


def build_findings(
    events: list[PacketEvent],
    pairs: list[PairHit],
    counts: dict[str, int],
) -> list[str]:
    """Human-readable conclusions for UI/log. @author by ak"""
    findings: list[str] = []
    n_plain = counts.get("PLAIN", 0)
    n_send = counts.get("SEND10", 0)
    findings.append(
        f"事件计数: PLAIN={n_plain} SEND10={n_send} "
        f"UPD_OUT={counts.get('UPD_OUT', 0)} SETKEY={counts.get('SETKEY', 0)}"
    )

    if n_plain == 0 and n_send == 0:
        findings.append(
            "未找到 PLAIN/SEND10 标签。请在 x32dbg 对 Update@0x00DAFAF0 与 "
            "ws2_32.send(len=10) 配置日志文本（可用「生成脚本」一键写出）。"
        )
        findings.append("核心点仍可用：明文在 Update 入口，密文在 send 出口。")
        return findings

    if n_plain and not n_send:
        findings.append(
            "有 PLAIN 无 SEND10：明文路径已通；若要开箱密文锚点，补 ws2_32.send 条件日志。"
        )
    if n_send and not n_plain:
        findings.append(
            "有 SEND10 无 PLAIN：密文出口已通；补 bp 0x00DAFAF0 日志文本 PLAIN 才能字段解包。"
        )

    paired = [p for p in pairs if p.plain_hex10]
    if paired:
        plain_ctr = Counter(p.plain_hex10 for p in paired)
        top_plain, top_n = plain_ctr.most_common(1)[0]
        findings.append(
            f"SEND10 成功配对 {len(paired)}/{len(pairs)}；主明文壳 `{top_plain}` ×{top_n}"
        )
        shell = match_shell(top_plain)
        if shell:
            findings.append(f"壳识别: {shell.name} — {shell.meaning}")
            for off, hx, note in shell.fields:
                findings.append(f"  字段 off {off}: `{hx}` → {note}")
        # RC4 check: same plain, different cipher
        if len(set(p.send10 for p in paired)) > 1 and top_n >= 2:
            findings.append(
                "同明文不同密文 → RC4 流状态前进正常；勿试图从单包密文逆推字段。"
            )
        frozen_n = sum(1 for p in paired if p.same_plain_as_frozen)
        if frozen_n:
            findings.append(
                f"冻结壳 open_chest `22080721000100000000` 命中 {frozen_n}/{len(paired)} 次配对"
            )
            findings.append(
                "同图下 10B 壳不变量：目标实体多半不在这 10B 内，由交互上下文决定。"
            )

    # density of type22 plains
    type22 = [e for e in events if e.kind == "PLAIN" and e.hex10.startswith("22")]
    if type22:
        findings.append(f"PLAIN 中 type=0x22 前缀 {len(type22)} 条（业务 Gamedata 候选）")

    findings.append("核心链路: 明文@Update入口(0x00DAFAF0) → 密文@ws2_32.send")
    findings.append("后续只需：触发功能 → 看日志标签 → 本面板自动对齐明文/密文。")
    return findings


def render_markdown(result: AnalyzeResult) -> str:
    """Write human report. @author by ak"""
    lines: list[str] = [
        "# 封包自动分析解包报告",
        "",
        f"时间: {result.stamp}",
        f"源: `{result.source}`",
        f"摘要: {result.summary_line()}",
        "",
        "## 结论（先看这里）",
        "",
    ]
    for f in result.findings:
        lines.append(f"- {f}")
    lines.extend(["", "## 日志标签含义", ""])
    for tag, meaning in ROLE_GUIDE.items():
        lines.append(f"- **{tag}**: {meaning}")

    lines.extend(
        [
            "",
            "## 事件计数",
            "",
            "| 标签 | 次数 |",
            "|------|------|",
        ]
    )
    for k, v in sorted(result.counts.items()):
        lines.append(f"| `{k}` | {v} |")

    lines.extend(["", "## SEND10 ↔ PLAIN 配对", ""])
    if not result.pairs:
        lines.append("（无 SEND10）")
    else:
        lines.append(
            "| # | SEND10 密文 | PLAIN 前10B | 壳 | gap | 行 |"
        )
        lines.append("|---|-------------|-------------|----|-----|-----|")
        for i, p in enumerate(result.pairs, 1):
            lines.append(
                f"| {i} | `{p.send10}` | `{p.plain_hex10 or '-'}` | "
                f"{p.shell or '-'} | {p.gap_lines} | {p.send_line}/{p.plain_line} |"
            )

    lines.extend(["", "## PLAIN hex10 Top", ""])
    if not result.plain_top:
        lines.append("（无）")
    else:
        lines.append("| hex10 | n | 壳 |")
        lines.append("|-------|---|-----|")
        for h, n in result.plain_top[:20]:
            sh = match_shell(h)
            lines.append(f"| `{h}` | {n} | {sh.name if sh else '-'} |")

    lines.extend(["", "## SEND10 Top", ""])
    if not result.send_top:
        lines.append("（无）")
    else:
        lines.append("| hex10 密文 | n |")
        lines.append("|-----------|---|")
        for h, n in result.send_top[:20]:
            lines.append(f"| `{h}` | {n} |")

    lines.extend(["", result.core_markdown, ""])
    lines.extend(
        [
            "## 工作流（免反复调试）",
            "",
            "1. x32dbg 附加 xajh，加载自动生成的脚本或按核心点配置日志。",
            "2. 游戏内触发目标功能（开箱/采集/…）。",
            "3. 保存/复制日志 → 本工具「分析日志」。",
            "4. 只看：**结论** + **配对表** + 业务响应是否成功。",
            "5. 字段/壳已自动标注；无需再手动找明文缓冲。",
            "",
        ]
    )
    return "\n".join(lines)


def analyze_log_text(
    text: str,
    source: str = "<memory>",
    lookback: int = 80,
) -> AnalyzeResult:
    """Analyze raw log text. @author by ak"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = text.splitlines()
    events = parse_events(lines)
    counts = Counter(e.kind for e in events)
    pairs = pair_send10_plain(events, lookback=lookback)
    plain_top = Counter(
        e.hex10 for e in events if e.kind == "PLAIN" and e.hex10
    ).most_common(30)
    send_top = Counter(
        e.hex10 for e in events if e.kind == "SEND10" and e.hex10
    ).most_common(30)
    findings = build_findings(events, pairs, dict(counts))
    return AnalyzeResult(
        ok=True,
        source=source,
        sources=[source],
        stamp=stamp,
        counts=dict(counts),
        events=events,
        pairs=pairs,
        plain_top=plain_top,
        send_top=send_top,
        core_markdown=core_points_markdown(),
        findings=findings,
        knowledge=knowledge_dict(),
    )


def analyze_paths(
    paths: list[Path],
    lookback: int = 80,
    write: bool = True,
    out_dir: Path | None = None,
    log: LogFn | None = None,
) -> AnalyzeResult:
    """
    Analyze one or more log files (concatenated with markers).

    @author by ak
    """
    log = log or _default_log
    if not paths:
        return AnalyzeResult(
            ok=False,
            source="",
            sources=[],
            stamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            counts={},
            events=[],
            pairs=[],
            plain_top=[],
            send_top=[],
            core_markdown=core_points_markdown(),
            findings=["未提供日志文件"],
            knowledge=knowledge_dict(),
            error="no log paths",
        )

    chunks: list[str] = []
    existing: list[Path] = []
    for p in paths:
        if not p.is_file():
            log(f"[auto_analyze] skip missing: {p}")
            continue
        existing.append(p)
        chunks.append(f";;; SOURCE {p}")
        chunks.append(load_text(p))

    if not existing:
        return AnalyzeResult(
            ok=False,
            source=str(paths[0]),
            sources=[str(x) for x in paths],
            stamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            counts={},
            events=[],
            pairs=[],
            plain_top=[],
            send_top=[],
            core_markdown=core_points_markdown(),
            findings=["日志文件不存在"],
            knowledge=knowledge_dict(),
            error="log file not found",
        )

    result = analyze_log_text(
        "\n".join(chunks),
        source=str(existing[0]),
        lookback=lookback,
    )
    result.sources = [str(p) for p in existing]
    # re-parse without SOURCE markers for cleaner line numbers of primary file
    if len(existing) == 1:
        result = analyze_log_text(
            load_text(existing[0]),
            source=str(existing[0]),
            lookback=lookback,
        )
        result.sources = [str(existing[0])]

    if write:
        out = out_dir or packets_dir()
        out.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        md_path = out / f"auto_analyze_{ts}.md"
        json_path = out / f"auto_analyze_{ts}.json"
        latest_md = out / "auto_analyze_latest.md"
        latest_json = out / "auto_analyze_latest.json"
        md = render_markdown(result)
        md_path.write_text(md, encoding="utf-8")
        latest_md.write_text(md, encoding="utf-8")
        payload = result.as_dict()
        # keep events capped in json for size
        if len(payload.get("events") or []) > 500:
            payload["events"] = payload["events"][:500]
            payload["events_truncated"] = True
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        json_path.write_text(raw, encoding="utf-8")
        latest_json.write_text(raw, encoding="utf-8")
        # always refresh core points card
        (out / "CORE_POINTS.md").write_text(core_points_markdown(), encoding="utf-8")
        result.files = {
            "md": str(md_path),
            "json": str(json_path),
            "latest_md": str(latest_md),
            "latest_json": str(latest_json),
            "core_points": str(out / "CORE_POINTS.md"),
        }
        log(f"[auto_analyze] wrote {md_path}")
        log(f"[auto_analyze] {result.summary_line()}")

    return result


def analyze_latest(
    root: Path | None = None,
    write: bool = True,
    log: LogFn | None = None,
) -> AnalyzeResult:
    """Analyze most recently modified discovered log. @author by ak"""
    p = latest_log(root)
    if not p:
        return AnalyzeResult(
            ok=False,
            source="",
            sources=[],
            stamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            counts={},
            events=[],
            pairs=[],
            plain_top=[],
            send_top=[],
            core_markdown=core_points_markdown(),
            findings=["未发现可用日志（.issues/packets 或 x32dbg log-*.txt）"],
            knowledge=knowledge_dict(),
            error="no log found",
        )
    return analyze_paths([p], write=write, log=log)


def ui_rows(result: AnalyzeResult, limit: int = 40) -> list[str]:
    """Short lines for listbox. @author by ak"""
    rows: list[str] = []
    if not result.ok:
        rows.append(f"[ERR] {result.error}")
        for f in result.findings:
            rows.append(f"  {f}")
        return rows
    rows.append(f"[摘要] {result.summary_line()}")
    for f in result.findings[:8]:
        rows.append(f"[结论] {f}")
    rows.append("--- 配对 (密文 ← 明文) ---")
    for i, p in enumerate(result.pairs[:limit], 1):
        mark = "[F]" if p.same_plain_as_frozen else ("[S]" if p.shell else "[ ]")
        rows.append(
            f"{mark} #{i} C:{p.send10} ← P:{p.plain_hex10 or '????'} "
            f"{p.shell or ''} gap={p.gap_lines}"
        )
    if result.plain_top:
        rows.append("--- PLAIN Top ---")
        for h, n in result.plain_top[:10]:
            sh = match_shell(h)
            rows.append(f"  ×{n} {h} {sh.name if sh else ''}")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Auto analyze x32dbg packet logs")
    ap.add_argument("log", nargs="*", help="log file(s)")
    ap.add_argument("--latest", action="store_true", help="use newest discovered log")
    ap.add_argument("--lookback", type=int, default=80)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    if args.log:
        paths = [Path(x) for x in args.log]
        r = analyze_paths(paths, lookback=args.lookback, write=not args.no_write)
    else:
        r = analyze_latest(write=not args.no_write)

    print(r.summary_line())
    for f in r.findings:
        print(" -", f)
    if r.files:
        print("report:", r.files.get("latest_md") or r.files.get("md"))
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
