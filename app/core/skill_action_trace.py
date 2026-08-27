# -*- coding: utf-8 -*-
"""Focused, short-lived tracing for the real skill action path."""
from __future__ import annotations

import csv
import ctypes
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable


LogFn = Callable[[str], None]

SCENARIO_SAME_SKILL = "same_skill_after_unlock"
SCENARIO_CHARGED_CHANNEL = "charged_channel_after_unlock"
SCENARIO_REAL_X = "real_x_interrupt"
SCENARIO_BASELINE = "normal_cast_baseline"

SCENARIO_LABELS = {
    SCENARIO_SAME_SKILL: "稳定去后摇：延后重停后重按同技能",
    SCENARIO_CHARGED_CHANNEL: "蓄力/持续去后摇：过滤自动续段后接技",
    SCENARIO_REAL_X: "后摇中真实按 X 再空格",
    SCENARIO_BASELINE: "正常施法基线",
}

CALLER_SET_ACTIVE_SESSION_START = 0x007639F0


def _tick32() -> int:
    return int(ctypes.windll.kernel32.GetTickCount()) & 0xFFFFFFFF


def _beep_signal(count: int) -> None:
    try:
        import winsound

        for index in range(max(0, int(count))):
            winsound.Beep(880, 140)
            if index + 1 < int(count):
                time.sleep(0.10)
    except Exception:
        pass


def _tick_after(value: int, marker: int) -> bool:
    return ((int(value) - int(marker)) & 0xFFFFFFFF) < 0x80000000


def _parse_int(value: str | int | None) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "0").strip()
    try:
        return int(text, 0)
    except ValueError:
        return 0


def parse_native_trace(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    p = Path(path)
    if not p.is_file():
        return rows
    with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
        for raw in csv.DictReader(f, delimiter="\t"):
            row = dict(raw)
            for key in (
                "seq",
                "tick",
                "thread",
                "caller",
                "self",
                "skill",
                "a1",
                "a2",
                "a3",
                "ret",
                "s0",
                "s4",
                "s10",
                "s18",
                "sea4",
                "c10b",
                "c18b",
                "c4a0b",
                "c10a",
                "c18a",
                "c4a0a",
                "ss0",
                "ss1",
                "ss2",
                "ss3",
                "ss4",
                "ss5",
                "ss6",
                "ss7",
                "ss8",
                "ss9",
                "ss10",
                "ss11",
            ):
                row[key] = _parse_int(row.get(key))
            rows.append(row)
    return rows


def packet_trace_path(path: str | Path) -> Path:
    """Return the packet sidecar written with a native skill trace."""
    p = Path(path)
    if p.name.endswith(".packets.tsv"):
        return p
    return p.with_name(f"{p.stem}.packets.tsv")


def parse_packet_trace(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    p = packet_trace_path(path)
    if not p.is_file():
        return rows
    with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
        for raw in csv.DictReader(f, delimiter="\t"):
            row = dict(raw)
            for key in (
                "seq",
                "tick",
                "thread",
                "caller",
                "self",
                "length",
                "captured",
            ):
                row[key] = _parse_int(row.get(key))
            row["payload"] = str(row.get("payload") or "").strip().upper()
            rows.append(row)
    return rows


def align_session_packets(
    action_rows: list[dict],
    packet_rows: list[dict],
    *,
    window_ms: int = 20,
) -> list[dict]:
    """Associate RC4 entries with HostStartSession calls by 32-bit tick."""

    def signed_delta(value: int, marker: int) -> int:
        return ((int(value) - int(marker) + 0x80000000) & 0xFFFFFFFF) - 0x80000000

    window = max(0, int(window_ms))
    aligned: list[dict] = []
    for session in action_rows:
        if session.get("kind") != "session_send":
            continue
        tick = int(session.get("tick") or 0)
        matches = []
        for packet in packet_rows:
            dt = signed_delta(int(packet.get("tick") or 0), tick)
            if abs(dt) <= window:
                candidate = dict(packet)
                candidate["dt_ms"] = dt
                matches.append(candidate)
        matches.sort(key=lambda row: (abs(int(row["dt_ms"])), int(row["seq"])))
        aligned.append(
            {
                "session_seq": int(session.get("seq") or 0),
                "session_tick": tick,
                "session_args": [int(session.get(f"ss{i}") or 0) for i in range(12)],
                "packets": matches,
            }
        )
    return aligned


def summarize_trace(
    rows: list[dict],
    marker_tick: int,
    *,
    target_skill_id: int = 0,
    target_config_id: int = 0,
) -> dict:
    after = [r for r in rows if _tick_after(r.get("tick", 0), marker_tick)]
    outer_all = [r for r in after if r.get("kind") == "outer"]
    core_all = [r for r in after if r.get("kind") == "core"]
    active_all = [r for r in after if r.get("kind") == "active"]
    request_all = [r for r in after if r.get("kind") == "request"]
    perform_all = [r for r in after if r.get("kind") == "perform"]
    restop_all = [r for r in after if r.get("kind") == "restop"]
    next_gate_all = [r for r in after if r.get("kind") == "next_gate"]
    target_skill_id = int(target_skill_id or 0)
    target_config_id = int(target_config_id or 0)

    def _gate_match(row: dict) -> bool:
        return not target_config_id or int(row.get("sea4") or 0) == target_config_id

    def _active_match(row: dict) -> bool:
        if not target_skill_id and not target_config_id:
            return True
        return bool(
            (target_skill_id and int(row.get("s0") or 0) == target_skill_id)
            or (target_config_id and int(row.get("s10") or 0) == target_config_id)
        )

    outer = [r for r in outer_all if _gate_match(r)]
    core = [r for r in core_all if _gate_match(r)]
    request = [r for r in request_all if _gate_match(r)]
    active = [r for r in active_all if _active_match(r)]
    perform = [r for r in perform_all if _active_match(r)]
    session_start = [
        r
        for r in active
        if int(r.get("caller") or 0) == CALLER_SET_ACTIVE_SESSION_START
    ]
    perform_refill = [r for r in active if r not in session_start]
    suppressed_refill = [
        r
        for r in perform_refill
        if int(r.get("caller") or 0) == 0x0076159A
        and int(r.get("ret") or 0) == 1
    ]
    suppressed_perform = [r for r in perform if int(r.get("ret") or 0) > 0]
    if session_start:
        verdict = "entered_new_active_session"
    elif request:
        verdict = "action_request_rejected"
    elif suppressed_perform:
        verdict = "perform_suppressed_no_request"
    elif perform_refill:
        verdict = "perform_refill_only"
    elif core:
        verdict = "cancast_only_no_active"
    elif outer:
        verdict = "outer_only_no_active"
    elif not outer and not core:
        verdict = "no_action_request_or_active"
    else:
        verdict = "trace_incomplete"
    return {
        "verdict": verdict,
        "target_skill_id": target_skill_id,
        "target_config_id": target_config_id,
        "total_events": len(rows),
        "after_marker_events": len(after),
        "outer_all_after": len(outer_all),
        "core_all_after": len(core_all),
        "active_all_after": len(active_all),
        "request_all_after": len(request_all),
        "perform_all_after": len(perform_all),
        "restop_after": len(restop_all),
        "next_gate_after": len(next_gate_all),
        "restop_return_counts": dict(
            sorted(Counter(int(r.get("ret", 0)) for r in restop_all).items())
        ),
        "request_after": len(request),
        "perform_after": len(perform),
        "outer_after": len(outer),
        "core_after": len(core),
        "active_after": len(active),
        "session_start_after": len(session_start),
        "perform_refill_after": len(perform_refill),
        "suppressed_refill_after": len(suppressed_refill),
        "suppressed_perform_after": len(suppressed_perform),
        "perform_return_counts": dict(
            sorted(Counter(int(r.get("ret", 0)) for r in perform).items())
        ),
        "outer_returns": sorted({int(r.get("ret", 0)) for r in outer}),
        "core_returns": sorted({int(r.get("ret", 0)) for r in core}),
        "outer_return_counts": dict(
            sorted(Counter(int(r.get("ret", 0)) for r in outer).items())
        ),
        "core_return_counts": dict(
            sorted(Counter(int(r.get("ret", 0)) for r in core).items())
        ),
        "request_returns": sorted({int(r.get("ret", 0)) for r in request}),
        "request_return_counts": dict(
            sorted(Counter(int(r.get("ret", 0)) for r in request).items())
        ),
        "request_modes": sorted({int(r.get("a1", 0)) for r in request}),
        "request_precomputed_ptrs": sorted(
            {int(r.get("a2", 0)) for r in request if r.get("a2")}
        ),
        "active_skill_ids": [int(r.get("s0", 0)) for r in active],
        "active_config_ids": [int(r.get("s10", 0)) for r in active],
        "callers": sorted(
            {
                int(r.get("caller", 0))
                for r in [*outer, *core, *request, *perform, *active]
                if r.get("caller")
            }
        ),
        "request_callers": sorted(
            {int(r.get("caller", 0)) for r in request if r.get("caller")}
        ),
        "skill_ptrs": sorted(
            {
                int(r.get("skill", 0))
                for r in [*outer, *core, *request, *perform, *active]
                if r.get("skill")
            }
        ),
        "skill_ea4": sorted(
            {
                int(r.get("sea4", 0))
                for r in [*outer, *core, *request]
                if r.get("sea4")
            }
        ),
        "skill_18": sorted(
            {int(r.get("s18", 0)) for r in [*outer, *core] if r.get("s18")}
        ),
        "after_rows": sorted(
            [*outer, *core, *request, *perform, *restop_all, *active],
            key=lambda r: int(r.get("seq") or 0),
        ),
    }


def _report_dir() -> Path:
    try:
        from common.paths import app_root

        root = Path(app_root())
    except Exception:
        root = Path(__file__).resolve().parents[2]
    out = root / ".issues" / "lab"
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_trace_report(result: dict) -> dict[str, str]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    scenario = str(result.get("scenario") or "unknown")
    dest = _report_dir()
    md_path = dest / f"lab_skill_action_trace_{scenario}_{stamp}.md"
    js_path = dest / f"lab_skill_action_trace_{scenario}_{stamp}.json"
    summary = result.get("summary") or {}
    rows = result.get("rows") or []
    lines = [
        f"# skill action trace {stamp}",
        "",
        f"scenario={scenario}",
        f"label={result.get('label')!r}",
        f"ok={bool(result.get('ok'))}",
        f"error={result.get('error')!r}",
        f"native_path={result.get('native_path')!r}",
        f"packet_path={result.get('packet_path')!r}",
        f"packet_events={len(result.get('packets') or [])}",
        f"session_packet_alignment={result.get('session_packets')!r}",
        f"marker_tick={int(result.get('marker_tick') or 0)}",
        f"capture_s={float(result.get('capture_s') or 0):.1f}",
        f"target_skill_id=0x{int(result.get('target_skill_id') or 0):X}",
        f"target_config_id=0x{int(result.get('target_config_id') or 0):X}",
        f"unlock={result.get('unlock')!r}",
        f"settle={result.get('settle')!r}",
        f"identity_cleanup={result.get('identity_cleanup')!r}",
        f"charge={result.get('charge')!r}",
        f"strict_recovery={result.get('strict_recovery')!r}",
        "",
        "## decisive result",
        "",
        f"verdict={summary.get('verdict')}",
        f"events total={summary.get('total_events')} after_marker={summary.get('after_marker_events')}",
        f"outer_after={summary.get('outer_after')} returns={summary.get('outer_return_counts') or summary.get('outer_returns')}",
        f"core_after={summary.get('core_after')} returns={summary.get('core_return_counts') or summary.get('core_returns')}",
        f"request_after={summary.get('request_after')} callers={[hex(v) for v in summary.get('request_callers') or []]} returns={summary.get('request_return_counts') or summary.get('request_returns')}",
        f"request_modes={summary.get('request_modes') or []} precomputed_ptrs={[hex(v) for v in summary.get('request_precomputed_ptrs') or []]}",
        f"perform_after={summary.get('perform_after')} suppressed={summary.get('suppressed_perform_after')} returns={summary.get('perform_return_counts')}",
        f"deferred_restop_after={summary.get('restop_after')} returns={summary.get('restop_return_counts')}",
        f"next_action_gate_after={summary.get('next_gate_after')}",
        f"active_after={summary.get('active_after')} skill_ids={[hex(v) for v in summary.get('active_skill_ids') or []]} config_ids={[hex(v) for v in summary.get('active_config_ids') or []]}",
        f"session_start_after={summary.get('session_start_after')} perform_refill_after={summary.get('perform_refill_after')}",
        f"suppressed_refill_after={summary.get('suppressed_refill_after')}",
        f"callers={[hex(v) for v in summary.get('callers') or []]}",
        f"skill_ptrs={[hex(v) for v in summary.get('skill_ptrs') or []]}",
        f"skill+EA4={[hex(v) for v in summary.get('skill_ea4') or []]}",
        f"skill+18={[hex(v) for v in summary.get('skill_18') or []]}",
        "",
        "## events after marker",
        "",
        "|seq|kind|tick|caller|ptr|a1|a2|a3|ret|s0|s4|s10|s18|sEA4|cast before|cast after|",
        "|---:|---|---:|---|---|---|---|---|---:|---|---|---|---|---|---|---|",
    ]
    for row in summary.get("after_rows") or []:
        lines.append(
            f"|{row.get('seq')}|{row.get('kind')}|{row.get('tick')}|"
            f"0x{int(row.get('caller') or 0):X}|0x{int(row.get('skill') or 0):X}|"
            f"0x{int(row.get('a1') or 0):X}|0x{int(row.get('a2') or 0):X}|"
            f"0x{int(row.get('a3') or 0):X}|{row.get('ret')}|"
            f"0x{int(row.get('s0') or 0):X}|0x{int(row.get('s4') or 0):X}|"
            f"0x{int(row.get('s10') or 0):X}|0x{int(row.get('s18') or 0):X}|"
            f"0x{int(row.get('sea4') or 0):X}|"
            f"{int(row.get('c10b') or 0):X}/{int(row.get('c18b') or 0):X}/{int(row.get('c4a0b') or 0):X}|"
            f"{int(row.get('c10a') or 0):X}/{int(row.get('c18a') or 0):X}/{int(row.get('c4a0a') or 0):X}|"
        )
    lines.extend(
        [
            "",
            "## interpretation",
            "",
            "- entered_new_active_session: SetCurActiveSkill returned to 0x7639F0 after the marker; a new client skill session started.",
            "- action_request_rejected: SkillActionRequest@0x762F10 ran but returned without a new active session; its return code identifies the rejecting branch.",
            "- perform_suppressed_no_request: the matching OnPerformSkill refill was suppressed, but the second input still did not reach SkillActionRequest.",
            "- perform_refill_only: only 0x76159A OnPerformSkill confirmation/refill ran; this is not a second skill start.",
            "- cancast_only_no_active: CanCast queries ran, but no matching active session was established.",
            "- outer_only_no_active: only the outer CanCast layer was observed for the target.",
            "- no_action_request_or_active: neither a real action request nor a matching active session was observed.",
            "- CastOuter/CastCore are polled by the skill bar while idle; they are context, not proof of a user action.",
            "- The trace is observational; it does not prove server acceptance by itself.",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    safe = dict(result)
    safe["rows"] = rows
    safe_summary = dict(summary)
    safe_summary.pop("after_rows", None)
    safe["summary"] = safe_summary
    js_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"md": str(md_path), "json": str(js_path)}


def run_skill_action_trace(
    session,
    scenario: str,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
    prompt_fn: LogFn | None = None,
    capture_s: float = 8.0,
) -> dict:
    """Run one bounded scenario and always remove native hooks on exit."""
    log = log or (lambda _m: None)
    prompt_fn = prompt_fn or log
    scenario = str(scenario or SCENARIO_BASELINE)
    result: dict = {
        "ok": False,
        "scenario": scenario,
        "label": SCENARIO_LABELS.get(scenario, scenario),
        "error": "",
        "marker_tick": 0,
        "capture_s": max(0.5, float(capture_s)),
        "target_skill_id": 0,
        "target_config_id": 0,
        "native_path": "",
        "unlock": {},
        "settle": {},
        "identity_cleanup": {},
        "charge": {},
        "strict_recovery": {},
        "rows": [],
        "summary": {},
    }
    if scenario not in SCENARIO_LABELS:
        result["error"] = f"unknown scenario: {scenario}"
        return result

    from app.core.xajh_bridge import ensure_bridge
    from app.core.skill_recovery_lab import (
        lab_stop_skill_perform,
        snapshot_cast,
        wait_recovery_window,
    )

    pid = int(getattr(session, "pid", 0) or 0)
    bridge = ensure_bridge(
        pid,
        log=log,
        inject_if_needed=True,
        hwnd=int(hwnd or 0) or None,
        force_reinject=False,
    )
    if bridge is None:
        result["error"] = "bridge unavailable"
        return result
    armed = False
    try:
        def arm_trace(*, suppress_mode: int = 0) -> int:
            nonlocal armed
            start = bridge.skill_action_trace(
                mode=int(suppress_mode or 1),
                skill_id=int(result.get("target_skill_id") or 0),
                config_id=int(result.get("target_config_id") or 0),
                hwnd=int(hwnd or 0) or None,
            )
            if not start.ok:
                raise RuntimeError(
                    str(start.error or start.note or "trace start failed")
                )
            armed = True
            log(f"skill_action_trace armed scenario={scenario}")
            return _tick32()

        if scenario == SCENARIO_BASELINE:
            result["marker_tick"] = arm_trace()
            prompt_fn("① 跟踪已开启，先放一个技能")
            _beep_signal(1)
        elif scenario == SCENARIO_CHARGED_CHANNEL:
            prompt_fn("① 听到一声后按住蓄力技能，保持不松键")
            _beep_signal(1)
        else:
            prompt_fn("① 先放一个技能；检测到技能态后才开启动作跟踪")
            _beep_signal(1)
        win = wait_recovery_window(
            session,
            log=log,
            rounds=1500,
            interval_s=0.01,
            accept_already_armed=False,
        )
        if not win.get("ok"):
            raise RuntimeError(str(win.get("error") or "no armed skill window"))
        watch = win.get("watch") or {}
        result["target_skill_id"] = int(watch.get("+10") or 0)
        result["target_config_id"] = int(watch.get("+18") or 0)

        if scenario not in (SCENARIO_BASELINE, SCENARIO_CHARGED_CHANNEL):
            arm_trace(suppress_mode=4 if scenario == SCENARIO_SAME_SKILL else 0)

        if scenario == SCENARIO_CHARGED_CHANNEL:
            from app.core.skill_recovery_lab import snapshot_host_perform

            sustained_since = 0.0
            sustained_deadline = time.monotonic() + 12.0
            sustained_samples = 0
            while time.monotonic() < sustained_deadline:
                charge_watch, _charge_cast, _charge_host = snapshot_cast(
                    session, log=lambda _m: None
                )
                charge_perform = snapshot_host_perform(
                    session, log=lambda _m: None
                )
                target_live = bool(
                    int(charge_watch.get("+10") or 0)
                    == int(result.get("target_skill_id") or 0)
                    and int(charge_watch.get("+18") or 0)
                    == int(result.get("target_config_id") or 0)
                )
                sustained = bool(
                    target_live
                    and (int(charge_watch.get("+4A0") or 0) & 1)
                    and int(charge_perform.get("perform_type") or 0) == 4
                )
                now = time.monotonic()
                sustained_samples += 1
                if sustained:
                    sustained_since = sustained_since or now
                    if now - sustained_since >= 2.2:
                        break
                else:
                    if sustained_since and not target_live:
                        raise RuntimeError(
                            "charged skill ended before sustained hold threshold"
                        )
                    sustained_since = 0.0
                time.sleep(0.02)
            else:
                raise RuntimeError("no 2.2s sustained charged skill state")

            prompt_fn("③ 蓄力态已稳定；听到三声时松键，之后不要补按")
            _beep_signal(3)
            release_tick = _tick32()
            result["charge"] = {
                "release_tick": release_tick,
                "sustained_s": time.monotonic() - sustained_since,
                "sustained_samples": sustained_samples,
            }
            arm_trace(suppress_mode=4)
            settle_started = time.monotonic()
            settle_deadline = settle_started + 3.2
            quiet_since = settle_started
            last_count = -1
            polls = 0
            while time.monotonic() < settle_deadline:
                status = bridge.skill_action_trace(
                    mode=2, hwnd=int(hwnd or 0) or None
                )
                polls += 1
                count = int(status.ret or 0) if status.ok else last_count
                if count != last_count:
                    last_count = count
                    quiet_since = time.monotonic()
                elapsed = time.monotonic() - settle_started
                quiet = time.monotonic() - quiet_since
                if elapsed >= 0.45 and quiet >= 0.22:
                    break
                time.sleep(0.04)
            result["settle"] = {
                "elapsed_s": time.monotonic() - settle_started,
                "quiet_s": time.monotonic() - quiet_since,
                "event_count": last_count,
                "polls": polls,
            }
            settled_watch, _settled_cast, _settled_host = snapshot_cast(
                session, log=lambda _m: None
            )
            settled_skill = int(settled_watch.get("+10") or 0)
            settled_config = int(settled_watch.get("+18") or 0)
            identity_matches = bool(
                settled_skill == int(result.get("target_skill_id") or 0)
                and settled_config == int(result.get("target_config_id") or 0)
            )
            cleanup: dict = {
                "needed": identity_matches,
                "before_skill": settled_skill,
                "before_config": settled_config,
                "ok": True,
                "note": "already clear",
            }
            if identity_matches:
                cleared = bridge.on_skill_stopped(
                    hwnd=int(hwnd or 0) or None,
                    mode=0,
                    timeout_ms=3000,
                )
                cleanup.update(
                    {
                        "ok": bool(cleared.ok),
                        "ret": cleared.ret,
                        "note": str(cleared.note or ""),
                        "error": str(cleared.error or ""),
                    }
                )
            time.sleep(0.35)
            cleaned_watch, _cleaned_cast, _cleaned_host = snapshot_cast(
                session, log=lambda _m: None
            )
            cleaned_perform = snapshot_host_perform(
                session, log=lambda _m: None
            )
            cleanup["after_skill"] = int(cleaned_watch.get("+10") or 0)
            cleanup["after_config"] = int(cleaned_watch.get("+18") or 0)
            cleanup["after_busy"] = int(cleaned_watch.get("+4A0") or 0)
            cleanup["after_perform_type"] = int(
                cleaned_perform.get("perform_type") or 0
            )
            result["identity_cleanup"] = cleanup
            if (
                cleanup["after_skill"] not in (0, 0xFFFFFFFF)
                or cleanup["after_config"] not in (0, 0xFFFFFFFF)
                or cleanup["after_busy"] != 0
                or cleanup["after_perform_type"] != 2
            ):
                raise RuntimeError(
                    "charged cleanup incomplete: "
                    f"skill=0x{cleanup['after_skill']:X} "
                    f"config=0x{cleanup['after_config']:X} "
                    f"busy={cleanup['after_busy']} "
                    f"type={cleanup['after_perform_type']}"
                )
            result["marker_tick"] = _tick32()
            prompt_fn(
                f"② 蓄力持续段已清理；请在 {result['capture_s']:.1f} 秒内按一次下一技能"
            )
            _beep_signal(2)
        elif scenario == SCENARIO_SAME_SKILL:
            unlock = lab_stop_skill_perform(session, hwnd=hwnd, log=log)
            result["unlock"] = unlock
            result["unlock"]["marker_tick"] = _tick32()
            settle_started = time.monotonic()
            settle_deadline = settle_started + 1.5
            quiet_since = settle_started
            last_count = -1
            polls = 0
            while time.monotonic() < settle_deadline:
                status = bridge.skill_action_trace(
                    mode=2, hwnd=int(hwnd or 0) or None
                )
                polls += 1
                count = int(status.ret or 0) if status.ok else last_count
                if count != last_count:
                    last_count = count
                    quiet_since = time.monotonic()
                elapsed = time.monotonic() - settle_started
                quiet = time.monotonic() - quiet_since
                if elapsed >= 0.45 and quiet >= 0.22:
                    break
                time.sleep(0.04)
            result["settle"] = {
                "elapsed_s": time.monotonic() - settle_started,
                "quiet_s": time.monotonic() - quiet_since,
                "event_count": last_count,
                "polls": polls,
            }
            settled_watch, _settled_cast, _settled_host = snapshot_cast(
                session, log=lambda _m: None
            )
            settled_skill = int(settled_watch.get("+10") or 0)
            settled_config = int(settled_watch.get("+18") or 0)
            identity_matches = bool(
                settled_skill == int(result.get("target_skill_id") or 0)
                and settled_config == int(result.get("target_config_id") or 0)
            )
            cleanup: dict = {
                "needed": identity_matches,
                "before_skill": settled_skill,
                "before_config": settled_config,
                "ok": True,
                "note": "already clear",
            }
            if identity_matches:
                cleared = bridge.on_skill_stopped(
                    hwnd=int(hwnd or 0) or None,
                    mode=0,
                    timeout_ms=3000,
                )
                cleanup.update(
                    {
                        "ok": bool(cleared.ok),
                        "ret": cleared.ret,
                        "note": str(cleared.note or ""),
                        "error": str(cleared.error or ""),
                    }
                )
            cleaned_watch, _cleaned_cast, _cleaned_host = snapshot_cast(
                session, log=lambda _m: None
            )
            cleanup["after_skill"] = int(cleaned_watch.get("+10") or 0)
            cleanup["after_config"] = int(cleaned_watch.get("+18") or 0)
            result["identity_cleanup"] = cleanup
            if cleanup["after_skill"] not in (0, 0xFFFFFFFF):
                raise RuntimeError(
                    "identity cleanup incomplete: "
                    f"skill=0x{cleanup['after_skill']:X} "
                    f"config=0x{cleanup['after_config']:X}"
                )
            result["marker_tick"] = _tick32()
            prompt_fn(
                f"② 已解除移动锁，请在 {result['capture_s']:.1f} 秒内立即重按同一个技能一次"
            )
            _beep_signal(2)
        elif scenario == SCENARIO_REAL_X:
            result["marker_tick"] = _tick32()
            prompt_fn(
                f"② 请在 {result['capture_s']:.1f} 秒内真实按 X，再按空格"
            )
            _beep_signal(2)
        else:
            prompt_fn("② 已抓到正常施法，保持不操作，记录基线")

        time.sleep(result["capture_s"])
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        if armed:
            try:
                stop = bridge.skill_action_trace(mode=0, hwnd=int(hwnd or 0) or None)
                if stop.ok:
                    result["native_path"] = str(stop.note or "")
                elif not result.get("error"):
                    result["error"] = str(stop.error or stop.note or "trace stop failed")
            except Exception as exc:
                if not result.get("error"):
                    result["error"] = f"trace cleanup: {exc}"
        try:
            bridge.close()
        except Exception:
            pass

    native_path = result.get("native_path") or ""
    if native_path:
        result["rows"] = parse_native_trace(native_path)
        result["packet_path"] = str(packet_trace_path(native_path))
        result["packets"] = parse_packet_trace(native_path)
        result["session_packets"] = align_session_packets(
            result["rows"], result["packets"], window_ms=20
        )
    result["summary"] = summarize_trace(
        result.get("rows") or [],
        int(result.get("marker_tick") or 0),
        target_skill_id=(
            int(result.get("target_skill_id") or 0)
            if scenario != SCENARIO_REAL_X
            else 0
        ),
        target_config_id=(
            int(result.get("target_config_id") or 0)
            if scenario != SCENARIO_REAL_X
            else 0
        ),
    )
    if scenario == SCENARIO_SAME_SKILL:
        marker_tick = int(result.get("marker_tick") or 0)
        all_rows = result.get("rows") or []
        before_marker = [
            row
            for row in all_rows
            if marker_tick
            and int(row.get("tick") or 0) != marker_tick
            and _tick_after(marker_tick, int(row.get("tick") or 0))
        ]
        after_marker = result["summary"].get("after_rows") or []
        suppressed_old = [
            row
            for row in before_marker
            if row.get("kind") == "perform" and int(row.get("ret") or 0) == 1
        ]
        restopped_old = [
            row
            for row in before_marker
            if row.get("kind") == "restop" and int(row.get("ret") or 0) == 1
        ]
        fresh_requests = [
            row
            for row in after_marker
            if row.get("kind") == "request"
            and int(row.get("ret") or 0) == 0x69
            and int(row.get("c10b") or 0) in (0, 0xFFFFFFFF)
            and int(row.get("c18b") or 0) in (0, 0xFFFFFFFF)
        ]
        fresh_sessions = [
            row
            for row in after_marker
            if row.get("kind") == "active"
            and int(row.get("caller") or 0) == CALLER_SET_ACTIVE_SESSION_START
            and int(row.get("c10b") or 0) in (0, 0xFFFFFFFF)
            and int(row.get("c18b") or 0) in (0, 0xFFFFFFFF)
        ]
        first_fresh_tick = min(
            (int(row.get("tick") or 0) for row in fresh_sessions), default=0
        )
        fresh_performs = [
            row
            for row in after_marker
            if first_fresh_tick
            and row.get("kind") == "perform"
            and int(row.get("ret") or 0) == 0
            and _tick_after(int(row.get("tick") or 0), first_fresh_tick)
        ]
        intervention_proven = bool(suppressed_old and restopped_old)
        strict_pass = bool(
            intervention_proven
            and fresh_requests
            and fresh_sessions
            and fresh_performs
        )
        if strict_pass:
            strict_verdict = "fresh_session_performed_before_natural_end"
        elif not intervention_proven:
            strict_verdict = "invalid_no_old_tail_intervention"
        elif fresh_sessions and not fresh_performs:
            strict_verdict = "fresh_session_without_perform"
        else:
            strict_verdict = "no_fresh_session_after_input"
        result["strict_recovery"] = {
            "pass": strict_pass,
            "verdict": strict_verdict,
            "old_suppressed_perform": len(suppressed_old),
            "old_successful_restop": len(restopped_old),
            "fresh_request": len(fresh_requests),
            "fresh_session": len(fresh_sessions),
            "fresh_perform": len(fresh_performs),
            "fresh_session_dt_ms": (
                (first_fresh_tick - marker_tick) & 0xFFFFFFFF
                if first_fresh_tick
                else None
            ),
            "fresh_perform_dt_ms": [
                (int(row.get("tick") or 0) - marker_tick) & 0xFFFFFFFF
                for row in fresh_performs
            ],
        }
        result["summary"]["strict_verdict"] = strict_verdict
        result["summary"]["strict_pass"] = strict_pass
    if scenario == SCENARIO_CHARGED_CHANNEL:
        release_tick = int((result.get("charge") or {}).get("release_tick") or 0)
        charge_rows = [
            row
            for row in result.get("rows") or []
            if release_tick and _tick_after(int(row.get("tick") or 0), release_tick)
        ]
        auto_continuations = [
            row
            for row in charge_rows
            if row.get("kind") == "active"
            and int(row.get("caller") or 0) == CALLER_SET_ACTIVE_SESSION_START
            and int(row.get("ret") or 0) == 2
        ]
        result.setdefault("charge", {}).update(
            {
                "events_after_release": len(charge_rows),
                "auto_continuation_sessions": len(auto_continuations),
                "auto_continuation_ticks": [
                    int(row.get("tick") or 0) for row in auto_continuations
                ],
            }
        )
    result["ok"] = bool(result.get("rows")) and not bool(result.get("error"))
    result["paths"] = write_trace_report(result)
    log(
        "skill_action_trace result "
        f"scenario={scenario} verdict={result['summary'].get('verdict')} "
        f"outer={result['summary'].get('outer_after')} "
        f"core={result['summary'].get('core_after')} "
        f"active={result['summary'].get('active_after')} "
        f"request={result['summary'].get('request_after')} "
        f"suppressed={result['summary'].get('suppressed_refill_after')} "
        f"perform_suppressed={result['summary'].get('suppressed_perform_after')} "
        f"restop={result['summary'].get('restop_after')} "
        f"next_gate={result['summary'].get('next_gate_after')} "
        f"ret={result['summary'].get('core_return_counts') or result['summary'].get('core_returns')}"
    )
    return result
