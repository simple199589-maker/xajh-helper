# -*- coding: utf-8 -*-
"""Bounded, process-local no-recovery chain for 有凤来仪."""
from __future__ import annotations

import threading
import time
import struct
from typing import Callable


LogFn = Callable[[str], None]

YOUFENG_SKILL_ID = 0x9563
YOUFENG_CONFIG_ID = 0x034D
YOUFENG_ULTIMATE_CONFIG_ID = 0x195E
YOUFENG_ULTIMATE_INTERVAL_S = 11.35
VK_YOUFENG = 0x36
VK_ALT = 0x12
VK_BLOCK = 0x58
VK_JUMP = 0x20
INTERRUPT_MACRO = "macro"
INTERRUPT_INTERNAL67 = "internal67"
INTERRUPT_NEXT_GATE = "next_gate"
INTERRUPT_MASH_CAST = "mash_cast"
INTERRUPT_FRESH_GATE = "fresh_gate"
INTERRUPT_EXACT_CANCEL = "exact_cancel"
INTERRUPT_PHASE_GATE = "phase_gate"
INTERRUPT_QINGGONG_GATE = "qinggong_gate"
INTERRUPT_QINGGONG_PRECAST = "qinggong_precast"
INTERRUPT_QINGGONG_PULSE = "qinggong_pulse"
INTERRUPT_QINGGONG_FORCE = "qinggong_force"
INTERRUPT_QINGGONG_LONG = "qinggong_long"
CALLER_SESSION_START = 0x007639F0
DEFAULT_IMAGE_BASE = 0x00400000
NOTE_VA_GAME_ROOT_GLOBAL = 0x015282D8
HOST_SIDE_MID_OFF = 0x24
HOST_SIDE_LEAF_OFF = 0x8C
HOST_SKILL_THIS_OFF = 0x1A88
HOST_PERFORM_MGR_OFF = 0x270
HOST_SESSION_GATE_OFF = 0x41C
PERFORM_MGR_CURRENT_OFF = 0x08


def analyze_next_gate_trace(
    rows: list[dict],
    *,
    min_transitions: int = 10,
    fresh_identity: bool = False,
    qinggong_gate: bool = False,
    packet_rows: list[dict] | None = None,
) -> dict:
    """Score no-action recasts from native trace rows, one session at a time."""
    ordered = sorted(
        (dict(row) for row in rows), key=lambda row: int(row.get("seq") or 0)
    )

    def target_active(row: dict) -> bool:
        return bool(
            row.get("kind") == "active"
            and int(row.get("caller") or 0) == CALLER_SESSION_START
            and int(row.get("s0") or 0) == YOUFENG_SKILL_ID
            and int(row.get("s10") or 0) == YOUFENG_CONFIG_ID
        )

    def target_perform(row: dict) -> bool:
        return bool(
            row.get("kind") == "perform"
            and int(row.get("ret") or 0) == 0
            and int(row.get("s0") or 0) == YOUFENG_SKILL_ID
            and int(row.get("s10") or 0) == YOUFENG_CONFIG_ID
        )

    e07_rows = [
        row
        for row in ordered
        if row.get("kind") in ("core", "active", "perform")
        and (
            int(row.get("sea4") or 0) == 0x0E07
            or int(row.get("s10") or 0) == 0x0E07
        )
    ]
    sessions = [row for row in ordered if target_active(row)]
    packets = sorted(
        (dict(row) for row in (packet_rows or [])),
        key=lambda row: int(row.get("seq") or 0),
    )
    target_session_packets = [
        row
        for row in packets
        if str(row.get("payload") or "").upper().startswith(
            "2227261F0063950000004D03"
        )
    ]
    transitions: list[dict] = []
    for index in range(1, len(sessions)):
        previous = sessions[index - 1]
        current = sessions[index]
        current_seq = int(current.get("seq") or 0)
        previous_seq = int(previous.get("seq") or 0)
        tick = int(current.get("tick") or 0)
        next_seq = (
            int(sessions[index + 1].get("seq") or 0)
            if index + 1 < len(sessions)
            else 0x7FFFFFFF
        )
        before_start = [
            row
            for row in ordered
            if previous_seq < int(row.get("seq") or 0) < current_seq
        ]
        same_tick_after = [
            row
            for row in ordered
            if current_seq < int(row.get("seq") or 0) < next_seq
            and int(row.get("tick") or 0) == tick
        ]
        gates = [row for row in before_start if row.get("kind") == "next_gate"]
        fresh_markers = [
            row
            for row in before_start
            if row.get("kind") in ("fresh_gate", "exact_cancel", "phase_gate")
            and int(row.get("c10b") or 0) == YOUFENG_SKILL_ID
            and int(row.get("c18b") or 0) == YOUFENG_CONFIG_ID
            and int(row.get("c10a") or 0) == 0
            and int(row.get("c18a") or 0) == 0
        ]
        qinggong_markers = [
            row
            for row in before_start
            if row.get("kind") == "qinggong_gate"
            and int(row.get("ret") or 0) == 1
            and int(row.get("sea4") or 0) == YOUFENG_CONFIG_ID
        ]
        qinggong_packets = []
        if index < len(target_session_packets):
            packet_seq = int(target_session_packets[index].get("seq") or 0)
            previous_packet_seq = (
                int(target_session_packets[index - 1].get("seq") or 0)
                if index > 0
                else 0
            )
            qinggong_packets = [
                row
                for row in packets
                if previous_packet_seq < int(row.get("seq") or 0) < packet_seq
                and str(row.get("payload") or "").upper().startswith(
                    "2204039000"
                )
            ]
        cores = [
            row
            for row in before_start
            if row.get("kind") == "core"
            and int(row.get("tick") or 0) == tick
            and int(row.get("ret") or 0) == 0
            and int(row.get("sea4") or 0) == YOUFENG_CONFIG_ID
        ]
        requests = [
            row
            for row in same_tick_after
            if row.get("kind") == "request"
            and int(row.get("ret") or 0) == 0x69
            and int(row.get("sea4") or 0) == YOUFENG_CONFIG_ID
        ]
        performs = [
            row
            for row in ordered
            if current_seq < int(row.get("seq") or 0) < next_seq
            and target_perform(row)
        ]
        perform_dt_ms = [int(row.get("tick") or 0) - tick for row in performs]
        fresh_core = any(
            int(row.get("c10b") or 0) == 0
            and int(row.get("c18b") or 0) == 0
            for row in cores
        )
        perform_immediate = bool(
            len(perform_dt_ms) >= 2
            and perform_dt_ms[0] <= 600
            and perform_dt_ms[1] <= 900
        )
        transition_pass = bool(
            qinggong_markers
            and qinggong_packets
            and cores
            and requests
            and len(performs) >= 2
            and perform_immediate
        ) if qinggong_gate else bool(
            fresh_markers
            and fresh_core
            and cores
            and requests
            and len(performs) >= 2
            and perform_immediate
        ) if fresh_identity else bool(
            gates and cores and requests and len(performs) >= 2
        )
        transitions.append(
            {
                "session_seq": current_seq,
                "tick": tick,
                "gate": bool(gates),
                "fresh_marker": bool(fresh_markers),
                "qinggong_marker": bool(qinggong_markers),
                "qinggong_packet_before_session": bool(qinggong_packets),
                "fresh_core": fresh_core,
                "core": bool(cores),
                "request": bool(requests),
                "perform_count": len(performs),
                "perform_dt_ms": perform_dt_ms,
                "perform_immediate": perform_immediate,
                "pass": transition_pass,
            }
        )

    required = max(1, int(min_transitions))
    scored = transitions[:required]
    passed = bool(
        len(scored) == required
        and all(item["pass"] for item in scored)
        and not e07_rows
    )
    return {
        "pass": passed,
        "required_transitions": required,
        "session_count": len(sessions),
        "transition_count": len(transitions),
        "passed_transitions": sum(1 for item in transitions if item["pass"]),
        "e07_events": len(e07_rows),
        "fresh_identity": bool(fresh_identity),
        "qinggong_gate": bool(qinggong_gate),
        "target_session_packet_count": len(target_session_packets),
        "transitions": transitions,
    }


def run_next_gate_validation(
    pid: int,
    hwnd: int,
    *,
    transitions: int = 10,
    timeout_s: float = 35.0,
    interrupt_mode: str = INTERRUPT_NEXT_GATE,
    pulse_stop_ms: int = 24,
    log: LogFn | None = None,
) -> dict:
    """Run a bounded no-action chain and return trace-backed acceptance data."""
    from app.core.game_attach import GameAttachSession
    from app.core.skill_action_trace import parse_native_trace, parse_packet_trace
    from app.core.skill_recovery_lab import snapshot_cast, snapshot_host_perform
    from app.core.xajh_bridge import ensure_bridge

    log = log or (lambda _message: None)
    required = max(1, int(transitions))
    result: dict = {
        "ok": False,
        "pid": int(pid),
        "hwnd": int(hwnd or 0),
        "required_transitions": required,
        "runner": {},
        "analysis": {},
        "native_path": "",
        "final_state": {},
        "error": "",
    }
    bridge = ensure_bridge(
        int(pid),
        log=log,
        inject_if_needed=False,
        hwnd=int(hwnd or 0) or None,
        force_reinject=False,
    )
    if bridge is None:
        result["error"] = "bridge unavailable"
        return result
    session = GameAttachSession(log=lambda _message: None)
    runner: YoufengChainRunner | None = None
    trace_armed = False
    try:
        session.attach(int(pid))
        session.hwnd = int(hwnd or 0)
        armed = bridge.skill_action_trace(
            mode=1,
            skill_id=YOUFENG_SKILL_ID,
            config_id=YOUFENG_CONFIG_ID,
            hwnd=int(hwnd or 0) or None,
            timeout_ms=3000,
        )
        if not armed.ok:
            raise RuntimeError(str(armed.error or armed.note or "trace arm failed"))
        trace_armed = True
        runner = YoufengChainRunner(
            int(pid),
            int(hwnd or 0),
            interrupt_mode=interrupt_mode,
            pulse_stop_ms=pulse_stop_ms,
            log=log,
        )
        runner.start()
        deadline = time.monotonic() + max(8.0, float(timeout_s))
        target_cycles = required + 1
        while time.monotonic() < deadline:
            stats = runner.stats()
            if stats.get("error"):
                raise RuntimeError(str(stats["error"]))
            if int(stats.get("cycles") or 0) >= target_cycles:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError(
                f"validation timeout cycles={runner.stats().get('cycles', 0)}/"
                f"{target_cycles}"
            )
        result["runner"] = runner.stats()
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        if runner is not None:
            runner.stop()
            result["runner"] = runner.stats()

        idle_deadline = time.monotonic() + 5.5
        while time.monotonic() < idle_deadline:
            try:
                watch, _cast, _host = snapshot_cast(
                    session, log=lambda _message: None
                )
                perform = snapshot_host_perform(
                    session, log=lambda _message: None
                )
                final_state = {
                    "skill": int(watch.get("+10") or 0),
                    "config": int(watch.get("+18") or 0),
                    "busy": int(watch.get("+4A0") or 0),
                    "perform_type": int(perform.get("perform_type") or 0),
                    "gate0": int(perform.get("session_gate") or 0),
                    "gate1": int(perform.get("session_gate1") or 0),
                    "gate2": int(perform.get("session_gate2") or 0),
                }
                result["final_state"] = final_state
                if YoufengChainRunner._idle(final_state):
                    break
            except Exception:
                pass
            time.sleep(0.05)

        if trace_armed:
            try:
                stopped = bridge.skill_action_trace(
                    mode=0,
                    hwnd=int(hwnd or 0) or None,
                    timeout_ms=3000,
                )
                if stopped.ok:
                    result["native_path"] = str(stopped.note or "")
                elif not result["error"]:
                    result["error"] = str(
                        stopped.error or stopped.note or "trace stop failed"
                    )
            except Exception as exc:
                if not result["error"]:
                    result["error"] = f"trace cleanup: {exc}"
        bridge.close()
        session.close()

    rows = parse_native_trace(result["native_path"]) if result["native_path"] else []
    packets = (
        parse_packet_trace(result["native_path"])
        if result["native_path"]
        else []
    )
    result["analysis"] = analyze_next_gate_trace(
        rows,
        min_transitions=required,
        fresh_identity=interrupt_mode in (
            INTERRUPT_FRESH_GATE,
            INTERRUPT_EXACT_CANCEL,
            INTERRUPT_PHASE_GATE,
        ),
        qinggong_gate=interrupt_mode in (
            INTERRUPT_QINGGONG_GATE,
            INTERRUPT_QINGGONG_PRECAST,
            INTERRUPT_QINGGONG_PULSE,
            INTERRUPT_QINGGONG_FORCE,
            INTERRUPT_QINGGONG_LONG,
        ),
        packet_rows=packets,
    )
    final_idle = YoufengChainRunner._idle(result.get("final_state") or {})
    runner_error = str((result.get("runner") or {}).get("error") or "")
    result["ok"] = bool(
        not result["error"]
        and not runner_error
        and result["analysis"].get("pass")
        and final_idle
    )
    return result


def run_fresh_gate_validation(
    pid: int,
    hwnd: int,
    *,
    transitions: int = 3,
    timeout_s: float = 18.0,
    log: LogFn | None = None,
) -> dict:
    """Run the fresh-identity experiment with strict server-perform timing."""
    return run_next_gate_validation(
        pid,
        hwnd,
        transitions=transitions,
        timeout_s=timeout_s,
        interrupt_mode=INTERRUPT_FRESH_GATE,
        log=log,
    )


def run_exact_cancel_validation(
    pid: int,
    hwnd: int,
    *,
    transitions: int = 3,
    timeout_s: float = 18.0,
    log: LogFn | None = None,
) -> dict:
    """Run exact-id CancelSession plus fresh local identity validation."""
    return run_next_gate_validation(
        pid,
        hwnd,
        transitions=transitions,
        timeout_s=timeout_s,
        interrupt_mode=INTERRUPT_EXACT_CANCEL,
        log=log,
    )


def run_phase_gate_validation(
    pid: int,
    hwnd: int,
    *,
    transitions: int = 3,
    timeout_s: float = 18.0,
    log: LogFn | None = None,
) -> dict:
    """Run the target-config phase-packet experiment with strict timing."""
    return run_next_gate_validation(
        pid,
        hwnd,
        transitions=transitions,
        timeout_s=timeout_s,
        interrupt_mode=INTERRUPT_PHASE_GATE,
        log=log,
    )


def run_qinggong_gate_validation(
    pid: int,
    hwnd: int,
    *,
    transitions: int = 3,
    timeout_s: float = 18.0,
    log: LogFn | None = None,
) -> dict:
    """Validate native Space/QingGong hook ordering and server perform timing."""
    return run_next_gate_validation(
        pid,
        hwnd,
        transitions=transitions,
        timeout_s=timeout_s,
        interrupt_mode=INTERRUPT_QINGGONG_GATE,
        log=log,
    )


def run_qinggong_precast_validation(
    pid: int,
    hwnd: int,
    *,
    transitions: int = 3,
    timeout_s: float = 18.0,
    log: LogFn | None = None,
) -> dict:
    """Validate pre-cast native QingGong call followed by a normal key."""
    return run_next_gate_validation(
        pid,
        hwnd,
        transitions=transitions,
        timeout_s=timeout_s,
        interrupt_mode=INTERRUPT_QINGGONG_PRECAST,
        log=log,
    )


def run_qinggong_pulse_validation(
    pid: int,
    hwnd: int,
    *,
    transitions: int = 3,
    timeout_s: float = 18.0,
    pulse_stop_ms: int = 24,
    log: LogFn | None = None,
) -> dict:
    """Validate a bounded native QingGong start/stop pulse before normal input."""
    return run_next_gate_validation(
        pid,
        hwnd,
        transitions=transitions,
        timeout_s=timeout_s,
        interrupt_mode=INTERRUPT_QINGGONG_PULSE,
        pulse_stop_ms=pulse_stop_ms,
        log=log,
    )


class YoufengChainRunner:
    """Run full 有凤 segments, then trigger the selected next-cast path."""

    def __init__(
        self,
        pid: int,
        hwnd: int = 0,
        *,
        hold_ms: int = 80,
        poll_ms: int = 6,
        gate_timeout_s: float = 2.2,
        interrupt_mode: str = INTERRUPT_MACRO,
        drive_casts: bool = True,
        include_ultimate: bool = False,
        ultimate_spam: bool = False,
        observe_ultimate: bool = False,
        suppress_lift: bool = False,
        pulse_stop_ms: int = 24,
        lift_limit_m: float = 0.10,
        landing_tolerance_m: float = 0.05,
        log: LogFn | None = None,
        on_status: LogFn | None = None,
    ) -> None:
        self.pid = int(pid)
        self.hwnd = int(hwnd or 0)
        self.hold_ms = max(25, min(120, int(hold_ms)))
        self.poll_s = max(0.004, min(0.03, int(poll_ms) / 1000.0))
        self.gate_timeout_s = max(1.0, min(5.0, float(gate_timeout_s)))
        self.drive_casts = bool(drive_casts)
        self.include_ultimate = bool(include_ultimate)
        self.ultimate_spam = bool(ultimate_spam)
        if self.include_ultimate and not self.drive_casts:
            raise ValueError("ultimate mixed loop requires drive_casts")
        if self.ultimate_spam and not self.drive_casts:
            raise ValueError("ultimate spam requires drive_casts")
        if self.ultimate_spam and self.include_ultimate:
            raise ValueError("ultimate spam and mixed loop are mutually exclusive")
        self.observe_ultimate = bool(
            observe_ultimate or self.include_ultimate or self.ultimate_spam
        )
        self.suppress_lift = bool(suppress_lift)
        pulse_ms = int(pulse_stop_ms)
        if pulse_ms not in (0, 4, 8, 24):
            raise ValueError(f"unsupported pulse stop: {pulse_stop_ms}ms")
        self.pulse_stop_ms = pulse_ms
        self.lift_limit_m = max(0.05, min(3.0, float(lift_limit_m)))
        self.landing_tolerance_m = max(0.03, min(0.3, float(landing_tolerance_m)))
        self._ground_y: float | None = None
        mode = str(interrupt_mode or INTERRUPT_MACRO).strip().lower()
        if mode not in (
            INTERRUPT_MACRO,
            INTERRUPT_INTERNAL67,
            INTERRUPT_NEXT_GATE,
            INTERRUPT_MASH_CAST,
            INTERRUPT_FRESH_GATE,
            INTERRUPT_EXACT_CANCEL,
            INTERRUPT_PHASE_GATE,
            INTERRUPT_QINGGONG_GATE,
            INTERRUPT_QINGGONG_PRECAST,
            INTERRUPT_QINGGONG_PULSE,
            INTERRUPT_QINGGONG_FORCE,
            INTERRUPT_QINGGONG_LONG,
        ):
            raise ValueError(f"unsupported interrupt mode: {interrupt_mode}")
        self.interrupt_mode = mode
        self._log = log or (lambda _m: None)
        self._on_status = on_status or (lambda _m: None)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._bridge = None
        self._session = None
        self._lock = threading.Lock()
        self._stats: dict = {
            "cycles": 0,
            "last_gate_ms": None,
            "last_cycle_ms": None,
            "last_press_gap_ms": None,
            "error": "",
            "interrupt_mode": self.interrupt_mode,
            "ground_y": None,
            "height_y": None,
            "lift_m": None,
            "landing_waits": 0,
            "cast_retries": 0,
            "ultimate_casts": 0,
        }

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def start(self) -> bool:
        if self.is_running():
            return True
        self._stop.clear()
        self._ready.clear()
        with self._lock:
            self._stats = {
                "cycles": 0,
                "last_gate_ms": None,
                "last_cycle_ms": None,
                "last_press_gap_ms": None,
                "error": "",
                "interrupt_mode": self.interrupt_mode,
                "ground_y": None,
                "height_y": None,
                "lift_m": None,
                "landing_waits": 0,
                "cast_retries": 0,
                "ultimate_casts": 0,
            }
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"youfeng-chain-{self.pid}",
        )
        self._thread.start()
        return True

    def wait_ready(self, timeout_s: float = 3.0) -> bool:
        """Wait until native observers are armed and the runner enters its loop."""
        if not self._ready.wait(max(0.0, float(timeout_s))):
            return False
        with self._lock:
            error = str(self._stats.get("error") or "")
        return self.is_running() and not error

    def stop(self) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._release_all()
        return not bool(thread and thread.is_alive())

    def _status(self, message: str) -> None:
        self._log(message)
        self._on_status(message)

    def _set_stats(self, **values) -> None:
        with self._lock:
            self._stats.update(values)

    def _read_remote_u32(self, address: int) -> int:
        session = self._session
        pm = getattr(session, "pm", None)
        if pm is None or not address:
            return 0
        try:
            import pymem.memory

            raw = pymem.memory.read_bytes(
                pm.process_handle, int(address) & 0xFFFFFFFF, 4
            )
            return int(struct.unpack_from("<I", raw, 0)[0]) & 0xFFFFFFFF
        except Exception:
            return 0

    def _snapshot(self) -> dict:
        session = self._session
        module_base = int(getattr(session, "module_base", 0) or 0)
        root_global = module_base + (NOTE_VA_GAME_ROOT_GLOBAL - DEFAULT_IMAGE_BASE)
        root = self._read_remote_u32(root_global)
        mid = self._read_remote_u32(root + HOST_SIDE_MID_OFF) if root else 0
        host = self._read_remote_u32(mid + HOST_SIDE_LEAF_OFF) if mid else 0
        cast = self._read_remote_u32(host + HOST_SKILL_THIS_OFF) if host else 0
        mgr = self._read_remote_u32(host + HOST_PERFORM_MGR_OFF) if host else 0
        current = self._read_remote_u32(mgr + PERFORM_MGR_CURRENT_OFF) if mgr else 0
        return {
            "skill": self._read_remote_u32(cast + 0x10) if cast else 0,
            "config": self._read_remote_u32(cast + 0x18) if cast else 0,
            "busy": self._read_remote_u32(cast + 0x4A0) if cast else 0,
            "perform_type": self._read_remote_u32(current + 0x04) if current else 0,
            "gate0": self._read_remote_u32(host + HOST_SESSION_GATE_OFF) if host else 0,
            "gate1": self._read_remote_u32(host + HOST_SESSION_GATE_OFF + 4) if host else 0,
            "gate2": self._read_remote_u32(host + HOST_SESSION_GATE_OFF + 8) if host else 0,
        }

    @staticmethod
    def _idle(state: dict) -> bool:
        return bool(
            int(state.get("skill") or 0) in (0, 0xFFFFFFFF)
            and int(state.get("config") or 0) in (0, 0xFFFFFFFF)
            and int(state.get("busy") or 0) == 0
            and int(state.get("perform_type") or 0) in (2, 3)
            and not int(state.get("gate0") or 0)
            and not int(state.get("gate1") or 0)
            and not int(state.get("gate2") or 0)
        )

    def _tap(self, vk: int) -> None:
        bridge = self._bridge
        if bridge is None:
            raise RuntimeError("bridge unavailable")
        down = bridge.key_hold(
            int(vk),
            down=True,
            allow_softsend=False,
            hwnd=self.hwnd or None,
            timeout_ms=2000,
        )
        if not down.ok:
            raise RuntimeError(str(down.error or down.note or "key down failed"))
        if self._stop.wait(self.hold_ms / 1000.0):
            try:
                bridge.key_hold(
                    int(vk), down=False, allow_softsend=False,
                    hwnd=self.hwnd or None, timeout_ms=1200,
                )
            finally:
                raise InterruptedError("stopped")
        up = bridge.key_hold(
            int(vk),
            down=False,
            allow_softsend=False,
            hwnd=self.hwnd or None,
            timeout_ms=2000,
        )
        if not up.ok:
            raise RuntimeError(str(up.error or up.note or "key up failed"))

    def _tap_chord(self, keys: tuple[int, ...]) -> None:
        bridge = self._bridge
        if bridge is None:
            raise RuntimeError("bridge unavailable")
        pressed: list[int] = []
        try:
            for vk in keys:
                result = bridge.key_hold(
                    int(vk), down=True, allow_softsend=False,
                    hwnd=self.hwnd or None, timeout_ms=2000,
                )
                if not result.ok:
                    raise RuntimeError(
                        str(result.error or result.note or "chord key down failed")
                    )
                pressed.append(int(vk))
            if self._stop.wait(self.hold_ms / 1000.0):
                raise InterruptedError("stopped")
        finally:
            for vk in reversed(pressed):
                try:
                    bridge.key_hold(
                        vk, down=False, allow_softsend=False,
                        hwnd=self.hwnd or None, timeout_ms=1200,
                    )
                except Exception:
                    pass

    def _release_all(self) -> None:
        if not self.drive_casts:
            return
        bridge = self._bridge
        if bridge is None:
            return
        try:
            bridge.key_hold(
                VK_YOUFENG,
                clear_all=True,
                hwnd=self.hwnd or None,
                timeout_ms=1500,
            )
        except Exception:
            pass

    def _wait_initial_idle(self) -> None:
        deadline = time.monotonic() + 4.0
        while not self._stop.is_set() and time.monotonic() < deadline:
            state = self._snapshot()
            if self._idle(state):
                return
            time.sleep(0.05)
        if self._stop.is_set():
            raise InterruptedError("stopped")
        raise RuntimeError(f"initial state not idle: {self._snapshot()}")

    def _sample_height(self) -> float:
        bridge = self._bridge
        if bridge is None:
            raise RuntimeError("bridge unavailable")
        result = bridge.host_snapshot(
            hwnd=self.hwnd or None,
            timeout_ms=1800,
        )
        if not result.ok:
            raise RuntimeError(
                str(result.error or result.note or "host height snapshot failed")
            )
        return float(result.y)

    def _wait_landing_if_needed(self) -> None:
        if not self.suppress_lift:
            return
        y = self._sample_height()
        ground = self._ground_y
        if ground is None or y < ground:
            ground = y
            self._ground_y = y
        lift = y - ground
        self._set_stats(ground_y=ground, height_y=y, lift_m=lift)
        if lift <= self.lift_limit_m:
            return

        with self._lock:
            waits = int(self._stats.get("landing_waits") or 0) + 1
        self._set_stats(landing_waits=waits)
        self._status(
            f"有凤连发: 高度 +{lift:.2f}m，暂停至落地后继续"
        )
        deadline = time.monotonic() + 4.0
        stable = 0
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self._stop.wait(0.08):
                raise InterruptedError("stopped")
            y = self._sample_height()
            if y < ground:
                ground = y
                self._ground_y = y
            lift = y - ground
            self._set_stats(ground_y=ground, height_y=y, lift_m=lift)
            stable = stable + 1 if lift <= self.landing_tolerance_m else 0
            if stable >= 2:
                self._status(f"有凤连发: 已落地，继续（等待{waits}次）")
                return
        if self._stop.is_set():
            raise InterruptedError("stopped")
        raise RuntimeError(
            f"landing timeout y={y:.3f} ground={ground:.3f} lift={lift:.3f}"
        )

    def _wait_final_segment(
        self,
        *,
        require_transition: bool = False,
        expected_config: int = YOUFENG_CONFIG_ID,
    ) -> int:
        started = time.monotonic()
        deadline = started + self.gate_timeout_s
        left_previous_final = not require_transition
        while not self._stop.is_set() and time.monotonic() < deadline:
            state = self._snapshot()
            skill = int(state.get("skill") or 0)
            config = int(state.get("config") or 0)
            if (
                self.drive_casts
                and skill
                and (skill != YOUFENG_SKILL_ID or config != int(expected_config))
            ):
                raise RuntimeError(
                    f"unexpected skill 0x{skill:X}/0x{config:X}; chain stopped"
                )
            is_final = bool(
                skill == YOUFENG_SKILL_ID
                and config == int(expected_config)
                and int(state.get("perform_type") or 0) == 4
                and int(state.get("gate1") or 0) >= 1600
            )
            if not is_final:
                left_previous_final = True
            if is_final and left_previous_final:
                return int((time.monotonic() - started) * 1000.0)
            time.sleep(self.poll_s)
        if self._stop.is_set():
            raise InterruptedError("stopped")
        raise RuntimeError(f"final segment timeout: {self._snapshot()}")

    def _wait_interrupt_idle(self) -> None:
        deadline = time.monotonic() + 0.8
        while not self._stop.is_set() and time.monotonic() < deadline:
            state = self._snapshot()
            if self._idle(state):
                return
            time.sleep(self.poll_s)
        if self._stop.is_set():
            raise InterruptedError("stopped")
        raise RuntimeError(f"internal interrupt did not settle: {self._snapshot()}")

    def _interrupt_tail(self) -> bool:
        if self.interrupt_mode == INTERRUPT_MACRO:
            self._tap(VK_BLOCK)
            if self._stop.wait(0.035):
                raise InterruptedError("stopped")
            self._tap(VK_JUMP)
            return False
        bridge = self._bridge
        if bridge is None:
            raise RuntimeError("bridge unavailable")
        if self.interrupt_mode == INTERRUPT_NEXT_GATE:
            result = bridge.youfeng_next_gate(
                YOUFENG_SKILL_ID,
                YOUFENG_CONFIG_ID,
                mode=1,
                hwnd=self.hwnd or None,
                timeout_ms=2000,
            )
            if not result.ok:
                raise RuntimeError(
                    str(result.error or result.note or "next-action gate failed")
                )
            return False
        if self.interrupt_mode == INTERRUPT_FRESH_GATE:
            result = bridge.youfeng_fresh_gate(
                YOUFENG_SKILL_ID,
                YOUFENG_CONFIG_ID,
                hwnd=self.hwnd or None,
                timeout_ms=2000,
            )
            if not result.ok:
                raise RuntimeError(
                    str(result.error or result.note or "fresh-action gate failed")
                )
            return False
        if self.interrupt_mode == INTERRUPT_EXACT_CANCEL:
            result = bridge.youfeng_exact_cancel_gate(
                YOUFENG_SKILL_ID,
                YOUFENG_CONFIG_ID,
                hwnd=self.hwnd or None,
                timeout_ms=2000,
            )
            if not result.ok:
                raise RuntimeError(
                    str(result.error or result.note or "exact cancel gate failed")
                )
            return False
        if self.interrupt_mode == INTERRUPT_PHASE_GATE:
            result = bridge.youfeng_phase_gate(
                YOUFENG_SKILL_ID,
                YOUFENG_CONFIG_ID,
                hwnd=self.hwnd or None,
                timeout_ms=2000,
            )
            if not result.ok:
                raise RuntimeError(
                    str(result.error or result.note or "phase gate failed")
                )
            if self._stop.wait(0.16):
                raise InterruptedError("stopped")
            return False
        if self.interrupt_mode == INTERRUPT_QINGGONG_GATE:
            result = bridge.youfeng_qinggong_gate(
                YOUFENG_SKILL_ID,
                YOUFENG_CONFIG_ID,
                hwnd=self.hwnd or None,
                timeout_ms=2000,
            )
            if not result.ok:
                raise RuntimeError(
                    str(result.error or result.note or "qinggong gate failed")
                )
            return False
        if self.interrupt_mode == INTERRUPT_QINGGONG_PRECAST:
            result = bridge.youfeng_qinggong_gate(
                YOUFENG_SKILL_ID,
                YOUFENG_CONFIG_ID,
                mode=2,
                hwnd=self.hwnd or None,
                timeout_ms=2000,
            )
            if not result.ok:
                raise RuntimeError(
                    str(result.error or result.note or "qinggong pre-cast failed")
                )
            if self._stop.wait(0.045):
                raise InterruptedError("stopped")
            return False
        if self.interrupt_mode == INTERRUPT_QINGGONG_PULSE:
            pulse_mode = {24: 5, 0: 6, 4: 7, 8: 8}[self.pulse_stop_ms]
            result = bridge.youfeng_qinggong_gate(
                YOUFENG_SKILL_ID,
                YOUFENG_CONFIG_ID,
                mode=pulse_mode,
                hwnd=self.hwnd or None,
                timeout_ms=2000,
            )
            if not result.ok:
                raise RuntimeError(
                    str(result.error or result.note or "qinggong pulse failed")
                )
            if self._stop.wait(0.045):
                raise InterruptedError("stopped")
            return False
        if self.interrupt_mode in (
            INTERRUPT_QINGGONG_FORCE,
            INTERRUPT_QINGGONG_LONG,
        ):
            mode = 3 if self.interrupt_mode == INTERRUPT_QINGGONG_FORCE else 4
            result = bridge.youfeng_qinggong_gate(
                YOUFENG_SKILL_ID,
                YOUFENG_CONFIG_ID,
                mode=mode,
                hwnd=self.hwnd or None,
                timeout_ms=2000,
            )
            if not result.ok:
                raise RuntimeError(
                    str(result.error or result.note or "qinggong variant failed")
                )
            if self._stop.wait(0.045):
                raise InterruptedError("stopped")
            return False
        if self.interrupt_mode == INTERRUPT_MASH_CAST:
            result = bridge.youfeng_mash_cast(
                YOUFENG_SKILL_ID,
                YOUFENG_CONFIG_ID,
                hwnd=self.hwnd or None,
                timeout_ms=2000,
            )
            if not result.ok:
                raise RuntimeError(
                    str(result.error or result.note or "native mash cast failed")
                )
            return True
        result = bridge.youfeng_internal_chain(
            YOUFENG_SKILL_ID,
            YOUFENG_CONFIG_ID,
            hwnd=self.hwnd or None,
            timeout_ms=2000,
        )
        if not result.ok:
            raise RuntimeError(
                str(result.error or result.note or "internal action chain failed")
            )
        self._wait_interrupt_idle()
        return False

    def _run(self) -> None:
        from app.core.game_attach import GameAttachSession
        from app.core.xajh_bridge import ensure_bridge

        try:
            session = GameAttachSession(log=lambda _m: None)
            session.attach(self.pid)
            session.hwnd = self.hwnd
            self._session = session
            bridge = ensure_bridge(
                self.pid,
                log=self._log,
                inject_if_needed=False,
                hwnd=self.hwnd or None,
                force_reinject=False,
            )
            if bridge is None:
                raise RuntimeError("bridge not ready")
            self._bridge = bridge
            ultimate_tail_armed = False
            ultimate_trace_armed = False
            if self.drive_casts:
                installed = bridge.key_hold(
                    VK_YOUFENG,
                    install_only=True,
                    allow_softsend=False,
                    hwnd=self.hwnd or None,
                    timeout_ms=2500,
                )
                if not installed.ok:
                    raise RuntimeError(
                        str(installed.error or installed.note or "KEY_HOLD install failed")
                    )
                self._wait_initial_idle()
            if self.observe_ultimate:
                try:
                    bridge.skill_action_experiment(
                        YOUFENG_SKILL_ID,
                        YOUFENG_ULTIMATE_CONFIG_ID,
                        mode=12,
                        hwnd=self.hwnd or None,
                        timeout_ms=1500,
                    )
                except Exception:
                    pass
                if self.ultimate_spam:
                    traced = bridge.skill_action_trace(
                        mode=1,
                        skill_id=YOUFENG_SKILL_ID,
                        config_id=YOUFENG_ULTIMATE_CONFIG_ID,
                        hwnd=self.hwnd or None,
                        timeout_ms=2500,
                    )
                    if not traced.ok:
                        raise RuntimeError(
                            str(
                                traced.error
                                or traced.note
                                or "ultimate action trace arm failed"
                            )
                        )
                    ultimate_trace_armed = True
                ultimate_mode = 13 if self.ultimate_spam else 11
                armed = bridge.skill_action_experiment(
                    YOUFENG_SKILL_ID,
                    YOUFENG_ULTIMATE_CONFIG_ID,
                    mode=ultimate_mode,
                    hwnd=self.hwnd or None,
                    timeout_ms=2500,
                )
                if not armed.ok:
                    raise RuntimeError(
                        str(armed.error or armed.note or "ultimate tail arm failed")
                    )
                ultimate_tail_armed = True
            if self.suppress_lift:
                self._ground_y = self._sample_height()
                self._set_stats(
                    ground_y=self._ground_y,
                    height_y=self._ground_y,
                    lift_m=0.0,
                )
            interrupt_label = (
                "X/Space 宏"
                if self.interrupt_mode == INTERRUPT_MACRO
                else (
                    "内部 E07 动作链"
                    if self.interrupt_mode == INTERRUPT_INTERNAL67
                    else (
                        "旧 identity gate"
                        if self.interrupt_mode == INTERRUPT_NEXT_GATE
                        else (
                            "fresh identity gate"
                            if self.interrupt_mode == INTERRUPT_FRESH_GATE
                            else (
                                "exact perform cancel"
                                if self.interrupt_mode == INTERRUPT_EXACT_CANCEL
                                else (
                                    "有凤自身 phase 包"
                                    if self.interrupt_mode == INTERRUPT_PHASE_GATE
                                    else (
                                        "原生 Space/轻功 hook"
                                        if self.interrupt_mode == INTERRUPT_QINGGONG_GATE
                                        else (
                                            "原生轻功预调用"
                                            if self.interrupt_mode == INTERRUPT_QINGGONG_PRECAST
                                            else (
                                                "原生轻功短脉冲"
                                                if self.interrupt_mode == INTERRUPT_QINGGONG_PULSE
                                                else (
                                                    "原生轻功强制 stop"
                                                    if self.interrupt_mode == INTERRUPT_QINGGONG_FORCE
                                                    else (
                                                        "原生轻功长间隔"
                                                        if self.interrupt_mode == INTERRUPT_QINGGONG_LONG
                                                        else "有凤原生 mash"
                                                    )
                                                )
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
            source_label = "后台按键" if self.drive_casts else "内挂最后一槽"
            if self.include_ultimate:
                source_label = "真绝+普通自动混合"
            elif self.ultimate_spam:
                source_label = "真绝无后摇连发"
            elif self.observe_ultimate:
                source_label = "内挂普通+真绝"
            self._ready.set()
            if self.ultimate_spam:
                self._status(
                    "真绝无后摇连发: 已启动（完整第二段去尾，持续清精确本地CD并放行动作门）"
                )
            else:
                self._status(
                    f"有凤连发: 已启动（{source_label}），完整两段后用{interrupt_label}去尾段"
                )
            if self.ultimate_spam:
                requests = 0
                while not self._stop.is_set():
                    self._tap_chord((VK_ALT, VK_YOUFENG))
                    requests += 1
                    self._set_stats(cycles=requests, ultimate_casts=requests)
                    if requests == 1 or requests % 20 == 0:
                        self._status(f"真绝无后摇连发: 已发送 {requests} 次 Alt+6")
                    if self._stop.wait(0.045):
                        break
                return
            last_press: float | None = None
            current_press_gap_ms: int | None = None
            cast_prestarted = False
            require_transition = False
            consecutive_cast_retries = 0
            expected_config = YOUFENG_CONFIG_ID
            next_ultimate_due = 0.0
            while not self._stop.is_set():
                cycle_started = time.monotonic()
                if cast_prestarted:
                    cast_prestarted = False
                elif self.drive_casts:
                    require_transition = last_press is not None
                    self._wait_landing_if_needed()
                    use_ultimate = bool(
                        self.include_ultimate
                        and time.monotonic() >= next_ultimate_due
                    )
                    if use_ultimate:
                        expected_config = YOUFENG_ULTIMATE_CONFIG_ID
                        self._tap_chord((VK_ALT, VK_YOUFENG))
                    else:
                        expected_config = YOUFENG_CONFIG_ID
                        self._tap(VK_YOUFENG)
                    pressed = time.monotonic()
                    if use_ultimate:
                        next_ultimate_due = (
                            pressed + YOUFENG_ULTIMATE_INTERVAL_S
                        )
                        with self._lock:
                            ultimate_casts = int(
                                self._stats.get("ultimate_casts") or 0
                            ) + 1
                        self._set_stats(ultimate_casts=ultimate_casts)
                    current_press_gap_ms = (
                        int((pressed - last_press) * 1000.0)
                        if last_press is not None
                        else None
                    )
                    last_press = pressed
                else:
                    require_transition = int(self._stats.get("cycles") or 0) > 0
                try:
                    gate_ms = self._wait_final_segment(
                        require_transition=require_transition,
                        expected_config=expected_config,
                    )
                except RuntimeError as exc:
                    if not self.drive_casts and str(exc).startswith("final segment timeout:"):
                        require_transition = False
                        continue
                    # A background tap can occasionally be consumed during the
                    # landing animation without creating a session. Retry only
                    # when the complete cast state is already idle; any busy or
                    # foreign-skill timeout remains fatal.
                    if not str(exc).startswith("final segment timeout:"):
                        raise
                    state = self._snapshot()
                    if not self._idle(state):
                        raise
                    with self._lock:
                        retries = int(self._stats.get("cast_retries") or 0) + 1
                    consecutive_cast_retries += 1
                    self._set_stats(cast_retries=retries)
                    if consecutive_cast_retries > 3:
                        raise RuntimeError(
                            "skill key did not create a session after 3 retries"
                        ) from exc
                    self._status(
                        f"有凤连发: 落地后按键未建立技能，第{retries}次重试"
                    )
                    require_transition = False
                    if self._stop.wait(0.10):
                        break
                    continue
                consecutive_cast_retries = 0
                if expected_config == YOUFENG_ULTIMATE_CONFIG_ID:
                    self._wait_interrupt_idle()
                    started_next = False
                else:
                    started_next = self._interrupt_tail()
                cycle_ms = int((time.monotonic() - cycle_started) * 1000.0)
                with self._lock:
                    cycles = int(self._stats.get("cycles") or 0) + 1
                self._set_stats(
                    cycles=cycles,
                    last_gate_ms=gate_ms,
                    last_cycle_ms=cycle_ms,
                    last_press_gap_ms=current_press_gap_ms,
                )
                if cycles == 1 or cycles % 100 == 0:
                    self._status(
                        f"有凤连发: 第{cycles}轮 "
                        f"{'真绝' if expected_config == YOUFENG_ULTIMATE_CONFIG_ID else '普通'} "
                        f"gate={gate_ms}ms "
                        f"cycle={cycle_ms}ms gap={current_press_gap_ms if current_press_gap_ms is not None else '-'}ms"
                    )
                if started_next:
                    pressed = time.monotonic()
                    current_press_gap_ms = (
                        int((pressed - last_press) * 1000.0)
                        if last_press is not None
                        else None
                    )
                    last_press = pressed
                    cast_prestarted = True
                    require_transition = True
                if self._stop.wait(0.045):
                    break
        except InterruptedError:
            pass
        except Exception as exc:
            self._set_stats(error=str(exc))
            self._status(f"有凤连发: 停机 {exc}")
        finally:
            self._ready.set()
            self._release_all()
            bridge = self._bridge
            self._bridge = None
            if bridge is not None:
                if self.observe_ultimate and ultimate_tail_armed:
                    try:
                        stopped = bridge.skill_action_experiment(
                            YOUFENG_SKILL_ID,
                            YOUFENG_ULTIMATE_CONFIG_ID,
                            mode=12,
                            hwnd=self.hwnd or None,
                            timeout_ms=1500,
                        )
                        self._status(
                            str(stopped.note or "真绝尾段观察器已停止")
                        )
                    except Exception:
                        pass
                if self.ultimate_spam and ultimate_trace_armed:
                    try:
                        dumped = bridge.skill_action_trace(
                            mode=0,
                            skill_id=YOUFENG_SKILL_ID,
                            config_id=YOUFENG_ULTIMATE_CONFIG_ID,
                            hwnd=self.hwnd or None,
                            timeout_ms=3000,
                        )
                        self._status(
                            f"真绝全链路跟踪: {dumped.note or dumped.error or 'dump failed'}"
                        )
                    except Exception as exc:
                        self._status(f"真绝全链路跟踪: 落盘失败 {exc}")
                if self.interrupt_mode in (
                    INTERRUPT_NEXT_GATE,
                    INTERRUPT_FRESH_GATE,
                    INTERRUPT_EXACT_CANCEL,
                    INTERRUPT_PHASE_GATE,
                    INTERRUPT_QINGGONG_GATE,
                ):
                    try:
                        bridge.youfeng_next_gate(
                            YOUFENG_SKILL_ID,
                            YOUFENG_CONFIG_ID,
                            mode=0,
                            hwnd=self.hwnd or None,
                            timeout_ms=1500,
                        )
                    except Exception:
                        pass
                try:
                    bridge.close()
                except Exception:
                    pass
            session = self._session
            self._session = None
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            self._status("有凤连发: 已停止")
