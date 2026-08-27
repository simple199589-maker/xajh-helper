# -*- coding: utf-8 -*-
"""
武尊堂降龙十八掌轮转控场编排 (master-driven rotation).

背景:
- 降龙十八掌 8s 释放、60s 冷却，用技能包直接施法（与丸子同款 CD1740 发送路径）。
- 5 人轮流放，一人放完进入冷却，下一位按间隔继续，不追求无缝。
- 每个窗口「上报自己」：存在 + 在武尊堂 + 副本挂机时勾选自动降龙。
  勾选且属于主副控(云控) 的窗口参与编排，主控统一调度。

调度:
- 主控窗口运行 JianglongRotationRunner 调度线程。
- 每轮从参与者(上报且满足条件)里按 pid 顺序轮流选一个。
- 轮到自己(主控 pid) → 直接施法；轮到副控 → 发 ACTION_JIANGLONG_CAST 定向事件。
- 施法 = 读取该账号的降龙配置 + 发降龙技能包。

@author by ak
"""
from __future__ import annotations

import ctypes
import struct
import threading
import time
from ctypes import wintypes
from typing import Callable

LogFn = Callable[[str], None]

# 降龙技能包（38 字节）：中间 config(u32 LE) 由该角色已加载的技能
# 管理器实时解析，不能按 PID、RID 或样本值写死。
JIANG_LONG_HEADER = bytes.fromhex("1F000726010000052301FFFFFF00000000")
JIANG_LONG_CONFIG_MID = bytes.fromhex("00000001")
JIANG_LONG_TAIL = b"\x01"
JIANG_LONG_PACKET_SIZE = (
    len(JIANG_LONG_HEADER) + 4 + len(JIANG_LONG_CONFIG_MID) + 12 + len(JIANG_LONG_TAIL)
)
JIANG_LONG_COOLDOWN_S = 60.0  # 每人技能冷却（从开始释放起计时）
JIANG_LONG_CAST_S = 8.0  # 释放时长（用于提示/节奏参考）
JIANG_LONG_COOLDOWN_GUARD_S = 2.0  # 冷却到点后的安全余量
JIANG_LONG_DISPATCH_LEAD_S = 1.0  # 提前发送，为后台 X 清动作预留时间
JIANG_LONG_ENTRY_SETTLE_S = 10.0  # 武尊堂场景稳定后的编排预热时间
JIANG_LONG_MIN_INTERVAL_S = 5.0  # 轮转最小间隔

# 武尊堂 / 真·武尊堂 scene id
WUZUN_SCENE_IDS = frozenset({1255, 1541})


def jianglong_entry_settle_remaining(
    scene_id: int | None,
    stable_since: float | None,
    *,
    now: float | None = None,
    settle_s: float = JIANG_LONG_ENTRY_SETTLE_S,
    test_mode: bool = False,
) -> float | None:
    """Return remaining post-stability warmup for a Wuzun entry.

    ``None`` means the current scene is not a Wuzun scene. Test mode bypasses
    the map-entry wait so the explicit test button remains immediate.
    """
    if bool(test_mode):
        return 0.0
    try:
        sid = int(scene_id or 0)
    except (TypeError, ValueError):
        sid = 0
    if sid not in WUZUN_SCENE_IDS:
        return None
    try:
        started = float(stable_since or 0.0)
    except (TypeError, ValueError):
        started = 0.0
    if started <= 0.0:
        return max(0.0, float(settle_s))
    current = time.monotonic() if now is None else float(now)
    return max(0.0, float(settle_s) - (current - started))


VK_X = 0x58

# 纯 RPM 宿主位置读取链（同 wanzi_packet）
ROOT_GLOBAL = 0x015282D8
HOST_SIDE_OFF = 0x8C
OBJ_POS_OFF = 0x158


def build_jianglong_packet(
    pos: tuple[float, float, float],
    runtime_config_id: int,
) -> bytes:
    """Build the packet with this process's runtime-resolved config."""
    config_id = int(runtime_config_id)
    if not 0 < config_id <= 0xFFFFFFFF:
        raise ValueError("invalid Jianglong runtime config")
    x, y, z = (float(v) for v in pos)
    return (
        JIANG_LONG_HEADER
        + struct.pack("<I", config_id)
        + JIANG_LONG_CONFIG_MID
        + struct.pack("<fff", x, y, z)
        + JIANG_LONG_TAIL
    )


def read_host_pos_xyz(pid: int) -> tuple[float, float, float] | None:
    """Read host +0x158 float3 via pure RPM (no CRT). @author by ak"""
    from app.core.remote_runtime import (
        kernel32,
        open_process,
        read_process,
    )

    pid = int(pid)
    handle = 0
    try:
        handle = open_process(pid)
        root = struct.unpack("<I", read_process(handle, ROOT_GLOBAL, 4))[0]
        mid = struct.unpack("<I", read_process(handle, root + 0x24, 4))[0]
        host = struct.unpack("<I", read_process(handle, mid + HOST_SIDE_OFF, 4))[0]
        raw = read_process(handle, host + OBJ_POS_OFF, 12)
        if len(raw) < 12:
            return None
        x, y, z = struct.unpack("<fff", raw[:12])
        if not all(v == v and abs(v) <= 500000.0 for v in (x, y, z)):
            return None
        return (float(x), float(y), float(z))
    except Exception:
        return None
    finally:
        if handle:
            try:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
            except Exception:
                pass


def press_block_x(pid: int, hwnd: int = 0, *, log: LogFn | None = None) -> bool:
    """Send one background X to clear the current skill action before Jianglong."""
    from app.core.bg_input import press_bg_chord_once

    log = log or (lambda _message: None)
    try:
        result = press_bg_chord_once(int(pid), "X", hwnd=int(hwnd or 0), log=log)
        return bool(result.get("ok"))
    except Exception as exc:
        log(f"降龙: 按X清动作失败 {exc}")
        return False

def send_jianglong_packet(pid: int, payload: bytes, *, log: LogFn | None = None) -> int:
    """发降龙技能包（CD1740），返回 EAX(1=接受)。@author by ak"""
    from app.core.game_send import send_raw_packet

    log = log or (lambda _m: None)
    ret = send_raw_packet(int(pid), payload, log=log)
    log(f"降龙: send pid={pid} len={len(payload)} ret={int(ret or 0)}")
    return int(ret or 0)


def cast_jianglong_once(
    pid: int,
    hwnd: int = 0,
    *,
    interrupt: bool = True,
    log: LogFn | None = None,
) -> dict:
    """Cast Jianglong through the client after resolving its loaded skill object."""
    log = log or (lambda _m: None)
    pid = int(pid)
    out: dict = {"ok": False, "pid": pid, "ret": 0, "error": ""}
    from app.core.jianglong_runtime import resolve_jianglong_runtime_config

    runtime = resolve_jianglong_runtime_config(pid, hwnd=hwnd, log=log)
    if not runtime.get("ok"):
        out["error"] = str(runtime.get("error") or "Jianglong skill unavailable")
        log(f"降龙: pid={pid} {out['error']}，拒绝施放")
        return out
    skill_id = int(runtime["skill_id"])
    out["skill_id"] = skill_id
    if interrupt:
        out["interrupt_ok"] = press_block_x(pid, hwnd=hwnd, log=log)
    try:
        from app.core.xajh_bridge import ensure_bridge

        bridge = ensure_bridge(pid, log=log, hwnd=int(hwnd or 0) or None)
        if bridge is None:
            out["error"] = "bridge unavailable"
            return out
        try:
            result = bridge.cast_jianglong_native(skill_id, hwnd=int(hwnd or 0) or None)
        finally:
            bridge.close()
    except Exception as exc:
        out["error"] = str(exc)
        log(f"降龙: pid={pid} 原生施放失败 {exc}")
        return out
    ret = int(result.ret if result.ret is not None else -1)
    # ActionCast returns 103 after it has accepted and started Jianglong.
    # The bridge marks that non-105 return as ST_ERR, but the in-game cast is
    # already in progress and must not be scheduled again.
    accepted = ret in (103, 105)
    out.update(
        {
            "ok": bool(result.ok or accepted),
            "ret": ret if ret >= 0 else 0,
            "note": str(result.note or ""),
        }
    )
    if not out["ok"]:
        out["error"] = str(result.error or result.note or "native cast failed")
        log(f"降龙: pid={pid} {out['error']}")
    return out

# ---------------------------------------------------------------------------
# 参与者上报（进程内共享，覆盖本机多开）
# ---------------------------------------------------------------------------

_PARTICIPANTS: dict[int, dict] = {}
_PARTICIPANTS_LOCK = threading.Lock()
PARTICIPANT_TTL_S = 20.0

# 本地群控报名只在主控启动编排后的短会话内开放。这样副控勾选
# 自动降龙不会在没有主控、没有收集动作时持续注册或刷日志。
_LOCAL_COLLECTION_LOCK = threading.RLock()
_LOCAL_COLLECTION_MASTER_PID = 0
_LOCAL_COLLECTION_UNTIL = 0.0
LOCAL_COLLECTION_WINDOW_S = 8.0


def begin_local_jianglong_collection(
    master_pid: int,
    *,
    window_s: float = LOCAL_COLLECTION_WINDOW_S,
) -> None:
    """Open/refresh the local master collection window. @author by ak"""
    global _LOCAL_COLLECTION_MASTER_PID, _LOCAL_COLLECTION_UNTIL
    master_pid = int(master_pid)
    if master_pid <= 0:
        return
    with _LOCAL_COLLECTION_LOCK:
        _LOCAL_COLLECTION_MASTER_PID = master_pid
        _LOCAL_COLLECTION_UNTIL = time.monotonic() + max(1.0, float(window_s))


def local_jianglong_collection_active() -> bool:
    """Return whether a local master is currently collecting participants."""
    global _LOCAL_COLLECTION_MASTER_PID, _LOCAL_COLLECTION_UNTIL
    with _LOCAL_COLLECTION_LOCK:
        if time.monotonic() <= _LOCAL_COLLECTION_UNTIL:
            return _LOCAL_COLLECTION_MASTER_PID > 0
        _LOCAL_COLLECTION_MASTER_PID = 0
        _LOCAL_COLLECTION_UNTIL = 0.0
        return False


def end_local_jianglong_collection(master_pid: int = 0) -> None:
    """Close the current local collection window, optionally for one master."""
    global _LOCAL_COLLECTION_MASTER_PID, _LOCAL_COLLECTION_UNTIL
    with _LOCAL_COLLECTION_LOCK:
        if master_pid and int(master_pid) != _LOCAL_COLLECTION_MASTER_PID:
            return
        _LOCAL_COLLECTION_MASTER_PID = 0
        _LOCAL_COLLECTION_UNTIL = 0.0

def report_participant(
    pid: int,
    *,
    name: str = "",
    hwnd: int = 0,
    role: str = "",
    enabled: bool = False,
    in_wuzun: bool = False,
    hang_running: bool = False,
) -> None:
    """本窗口上报自身状态。@author by ak"""
    pid = int(pid)
    with _PARTICIPANTS_LOCK:
        _PARTICIPANTS[pid] = {
            "pid": pid,
            "name": str(name or "").strip(),
            "hwnd": int(hwnd or 0),
            "role": str(role or "").strip().lower(),
            "enabled": bool(enabled),
            "in_wuzun": bool(in_wuzun),
            "hang_running": bool(hang_running),
            "ts": time.time(),
        }


def clear_participants() -> None:
    """Clear the local collection registry before a new master collection."""
    with _PARTICIPANTS_LOCK:
        _PARTICIPANTS.clear()

def drop_participant(pid: int) -> None:
    """窗口销毁时移除。@author by ak"""
    with _PARTICIPANTS_LOCK:
        _PARTICIPANTS.pop(int(pid), None)


def participant_snapshot(*, fresh: bool = True) -> list[dict]:
    """当前存活的上报窗口列表（按 pid 升序）。@author by ak"""
    now = time.time()
    with _PARTICIPANTS_LOCK:
        items = list(_PARTICIPANTS.values())
    out = []
    for item in items:
        if fresh and now - float(item.get("ts") or 0) > PARTICIPANT_TTL_S:
            continue
        out.append(dict(item))
    out.sort(key=lambda d: int(d.get("pid") or 0))
    return out


def eligible_participants() -> list[dict]:
    """满足参与条件（勾选 + 武尊堂 + 挂机 + 主副控）的窗口。@author by ak"""
    return [
        p
        for p in participant_snapshot()
        if p.get("enabled")
        and p.get("in_wuzun")
        and p.get("hang_running")
        and p.get("role") in ("master", "slave")
    ]


# ---------------------------------------------------------------------------
# 主控调度线程
# ---------------------------------------------------------------------------


class JianglongRotationRunner:
    """
    主控统一调度：轮转参与者，按（冷却 + 安全余量）/人数间隔轮流放降龙。

    roster_fn() -> 参与者 dict 列表（主控页提供）
    cast_fn(participant) -> 执行施法（主控页提供：自身直接放，副控发事件）
    @author by ak
    """

    def __init__(
        self,
        master_pid: int,
        *,
        roster_fn: Callable[[], list[dict]],
        cast_fn: Callable[[dict], None],
        cooldown_s: float = JIANG_LONG_COOLDOWN_S,
        cooldown_guard_s: float = JIANG_LONG_COOLDOWN_GUARD_S,
        dispatch_lead_s: float = JIANG_LONG_DISPATCH_LEAD_S,
        min_interval_s: float = JIANG_LONG_MIN_INTERVAL_S,
        roster_settle_s: float = 0.0,
        log: LogFn | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.master_pid = int(master_pid)
        self._roster_fn = roster_fn
        self._cast_fn = cast_fn
        self._cooldown_s = max(0.2, float(cooldown_s))
        self._cooldown_guard_s = max(0.0, float(cooldown_guard_s))
        self._dispatch_lead_s = min(
            self._cooldown_guard_s, max(0.0, float(dispatch_lead_s))
        )
        self._min_interval_s = max(0.02, float(min_interval_s))
        self._roster_settle_s = max(0.0, float(roster_settle_s))
        self._log = log or (lambda _m: None)
        self._on_status = on_status or (lambda _m: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stats: dict = {
            "running": False,
            "turns": 0,
            "roster_size": 0,
            "interval_s": 0.0,
            "last_target_pid": None,
            "last_error": "",
        }

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._stats.get("running"))

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        with self._lock:
            self._stats.update(running=True, last_error="", turns=0)
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"jianglong-rotation-{self.master_pid}",
        )
        self._thread.start()
        return True

    def stop(self, timeout_s: float = 2.0) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout_s)))
        with self._lock:
            self._stats["running"] = bool(
                thread is not None and thread.is_alive()
            )
        return not bool(thread is not None and thread.is_alive())

    def _set(self, **values) -> None:
        with self._lock:
            self._stats.update(values)

    def _status(self, message: str) -> None:
        self._log(message)
        self._on_status(message)

    def _run(self) -> None:
        idx = 0
        try:
            if self._roster_settle_s:
                self._status(
                    f"降龙编排: 收集已开启角色 {self._roster_settle_s:.1f}s…"
                )
                if self._stop.wait(self._roster_settle_s):
                    return
            roster = self._normalized_roster(self._roster_fn())
            if roster:
                self._status(
                    "降龙编排: 名单已锁定 "
                    + "、".join(
                        str(item.get("name") or item.get("pid") or "?")
                        for item in roster
                    )
                )
            while not self._stop.is_set():
                n = len(roster)
                if n < 1:
                    self._set(roster_size=0, interval_s=0.0)
                    self._stop.wait(1.0)
                    continue
                interval = self._interval_for_roster_size(n)
                self._set(roster_size=n, interval_s=interval)
                target = roster[idx % n]
                idx += 1
                self._set(last_target_pid=int(target.get("pid") or 0))
                self._status(
                    f"降龙编排: 轮到 pid={target.get('pid') or 0} "
                    f"name={target.get('name') or '?'} interval={interval:.1f}s"
                )
                try:
                    self._cast_fn(target)
                except Exception as e:
                    self._set(last_error=str(e))
                    self._status(f"降龙编排: 施法异常 {e}")
                self._set(turns=int(self._stats.get("turns") or 0) + 1)
                if self._stop.wait(interval):
                    break
        except Exception as e:
            self._set(last_error=str(e))
            self._status(f"降龙编排: 停止 {e}")
        finally:
            with self._lock:
                self._stats["running"] = False

    def _interval_for_roster_size(self, roster_size: int) -> float:
        """Dispatch the next turn early while retaining a cooldown safety margin."""
        count = max(1, int(roster_size))
        cycle_s = (
            self._cooldown_s + self._cooldown_guard_s - self._dispatch_lead_s
        )
        return max(self._min_interval_s, cycle_s / float(count))

    @staticmethod
    def _normalized_roster(roster: list[dict]) -> list[dict]:
        """Keep usable participants in a stable order for one rotation run."""
        out = [
            dict(item)
            for item in roster
            if int(item.get("pid") or 0) > 0
            or str(item.get("name") or "").strip()
        ]
        out.sort(
            key=lambda item: (
                0 if int(item.get("pid") or 0) > 0 else 1,
                int(item.get("pid") or 0),
                str(item.get("name") or ""),
            )
        )
        return out


__all__ = [
    "begin_local_jianglong_collection",
    "end_local_jianglong_collection",
    "local_jianglong_collection_active",
    "JIANG_LONG_HEADER",
    "JIANG_LONG_CONFIG_MID",
    "JIANG_LONG_TAIL",
    "JIANG_LONG_PACKET_SIZE",
    "JIANG_LONG_COOLDOWN_S",
    "JIANG_LONG_CAST_S",
    "JIANG_LONG_COOLDOWN_GUARD_S",
    "JIANG_LONG_DISPATCH_LEAD_S",
    "JIANG_LONG_ENTRY_SETTLE_S",
    "JIANG_LONG_MIN_INTERVAL_S",
    "WUZUN_SCENE_IDS",
    "jianglong_entry_settle_remaining",
    "VK_X",
    "build_jianglong_packet",
    "read_host_pos_xyz",
    "press_block_x",
    "send_jianglong_packet",
    "cast_jianglong_once",
    "clear_participants",
    "report_participant",
    "drop_participant",
    "participant_snapshot",
    "eligible_participants",
    "JianglongRotationRunner",
]
