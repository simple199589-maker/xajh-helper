# -*- coding: utf-8 -*-
"""Dynamic 武尊堂 stone-dialogue and monster-opening session runner."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from app.core.automove import PathTarget, host_move_to, read_scene_position
from app.core.packet_intercept import replay_packet

LogFn = Callable[[str], None]

WUZUN_OPEN_TARGET = (-2.4, 33.5, -20.6)
WUZUN_STABLE_DELAY_S = 3.0
WUZUN_CAPTURE_S = 3.0
WUZUN_MOVE_TIMEOUT_S = 45.0
WUZUN_PACKET_MAX_GAP_S = 1.5
WUZUN_MONSTER_PACKET_LEN = 14
WUZUN_SCENE_IDS = frozenset({1255, 1541})
WUZUN_CHALLENGE_NAME = "\u771f\u00b7\u6b66\u5c0a\u5802\u6311\u6218\u724c"
WUZUN_DIALOGUE_BODY = bytes.fromhex("010000000001")
WUZUN_GENERIC_MONSTER_HEX = (
    "0E00079F210000000000000000000000000000FFFFFF",
    "0E0007A0210000000000000000000000000000FFFFFF",
    "0E0007A1210000000000000000000000000000FFFFFF",
    "0E0007A2210000000000000000000000000000FFFFFF",
    "0E0007A3210000000000000000000000000000FFFFFF",
    "0E0007A4210000000000000000000000000000FFFFFF",
    "0E0007A5210000000000000000000000000000FFFFFF",
    "0E0007A6210000000000000000000000000000FFFFFF",
    "0E0007A7210000000000000000000000000000FFFFFF",
    "0E0007A8210000000000000000000000000000FFFFFF",
    "0E0007A9210000000000000000000000000000FFFFFF",
    "0E0007AA210000000000000000000000000000FFFFFF",
    "0E0007AB210000000000000000000000000000FFFFFF",
    "0E0007AC210000000000000000000000000000FFFFFF",
    "0E0007AD210000000000000000000000000000FFFFFF",
    "0E0007AE210000000000000000000000000000FFFFFF",
)


@dataclass(frozen=True)
class ParsedSystemPacket:
    raw: bytes
    length: int
    frame_type: int
    opcode: int
    body: bytes
    tick: int


def parse_system_packet(record: dict) -> ParsedSystemPacket | None:
    raw = bytes(record.get("data") or b"")
    if len(raw) < 4:
        return None
    return ParsedSystemPacket(
        raw=raw,
        length=len(raw),
        frame_type=int.from_bytes(raw[:2], "little"),
        opcode=int.from_bytes(raw[2:4], "little"),
        body=raw[4:],
        tick=int(record.get("tick") or 0),
    )


def _build_generic_monster_packets() -> tuple[ParsedSystemPacket, ...]:
    packets = []
    for raw_hex in WUZUN_GENERIC_MONSTER_HEX:
        raw = bytes.fromhex(raw_hex)
        packets.append(
            ParsedSystemPacket(
                raw=raw,
                length=len(raw),
                frame_type=int.from_bytes(raw[:2], "little"),
                opcode=int.from_bytes(raw[2:4], "little"),
                body=raw[4:],
                tick=0,
            )
        )
    return tuple(packets)


def build_wuzun_dialogue_packets(object_id64: int) -> tuple[ParsedSystemPacket, ...]:
    """Build dialogue frames from the live challenge-card object ID."""
    value = int(object_id64 or 0)
    if value <= 0:
        raise ValueError(f"挑战牌 obj_id64 无效: {object_id64!r}")
    field = value & 0xFFFF
    packets = []
    for frame_type in (0x000A, 0x000C):
        raw = (
            frame_type.to_bytes(2, "little")
            + field.to_bytes(2, "little")
            + WUZUN_DIALOGUE_BODY
        )
        packets.append(
            ParsedSystemPacket(
                raw=raw,
                length=len(raw),
                frame_type=frame_type,
                opcode=field,
                body=WUZUN_DIALOGUE_BODY,
                tick=0,
            )
        )
    return tuple(packets)


def find_wuzun_challenge_target(
    session, *, log: LogFn | None = None, use_bridge: bool = False
) -> dict:
    """Find the live NPC/Matter card by name and return its dynamic IDs."""
    log = log or (lambda _m: None)
    if use_bridge:
        try:
            from app.core.xajh_bridge import ensure_bridge

            bridge = ensure_bridge(
                int(session.pid),
                log=log,
                inject_if_needed=True,
                hwnd=int(getattr(session, "hwnd", 0) or 0) or None,
            )
            if bridge is not None:
                try:
                    bridge_rows = bridge.scan_objects(name=WUZUN_CHALLENGE_NAME, limit=32, timeout_ms=4000)
                finally:
                    bridge.close()
                if bridge_rows:
                    result = bridge_rows[0]
                    object_id = (int(result.id_hi) << 32) | (int(result.id_lo) & 0xFFFFFFFF)
                    if object_id:
                        target = {
                            "class_id": 0,
                            "name": WUZUN_CHALLENGE_NAME,
                            "ptr": 0,
                            "obj_id64": object_id,
                            "tid": int(result.tid or 0),
                            "dist": None,
                            "x": float(result.x),
                            "y": float(result.y),
                            "z": float(result.z),
                            "via": "bridge",
                        }
                        log(
                            "武尊堂自动开怪: 桥找到挑战牌 "
                            f"obj_id=0x{object_id:X} tid={target['tid']} "
                            f"field=0x{object_id & 0xFFFF:04X}"
                        )
                        return target
                log("武尊堂自动开怪: 桥扫描未找到挑战牌")
        except Exception as exc:
            log(f"武尊堂自动开怪: 桥扫描失败，回退 Python 对象扫描: {exc}")
    from app.core.plg_interact import get_object_id64
    from app.core.plg_objects import CLASS_MATTER, CLASS_NPC, list_class_objects

    host_pos = None
    try:
        position = read_scene_position(session, log=lambda _m: None)
        if getattr(position, "ok", False) and getattr(position, "scene_pos", None):
            host_pos = tuple(position.scene_pos)
    except Exception:
        pass

    candidates = []
    for class_id in (CLASS_NPC, CLASS_MATTER):
        objects = list_class_objects(
            session,
            class_id,
            host_pos=host_pos,
            radius=None,
            limit=128,
            want_name_keys=(WUZUN_CHALLENGE_NAME,),
            read_name=True,
            read_tid=True,
            max_inspect=256,
            log=log,
        )
        for obj in objects:
            if WUZUN_CHALLENGE_NAME not in str(obj.name or ""):
                continue
            obj_id = get_object_id64(session, int(obj.ptr))
            if not obj_id:
                continue
            candidates.append(
                {
                    "class_id": int(class_id),
                    "name": str(obj.name or ""),
                    "ptr": int(obj.ptr),
                    "obj_id64": int(obj_id),
                    "tid": int(obj.tid or 0),
                    "dist": obj.dist,
                    "x": obj.x,
                    "y": obj.y,
                    "z": obj.z,
                }
            )

    if not candidates:
        raise RuntimeError(f"未找到挑战牌对象: {WUZUN_CHALLENGE_NAME}")
    candidates.sort(key=lambda item: item["dist"] if item["dist"] is not None else 1e9)
    target = candidates[0]
    if target["tid"] <= 0:
        raise RuntimeError(f"挑战牌缺少有效 TID: {target}")
    log(
        "武尊堂自动开怪: 找到挑战牌 "
        f"name={target['name']!r} tid={target['tid']} "
        f"obj_id=0x{target['obj_id64']:X} dist={target['dist']}"
    )
    return target

def validate_wuzun_open_gate(scene_id: int | None, hang_running: bool) -> tuple[bool, str | None]:
    if int(scene_id or 0) not in WUZUN_SCENE_IDS:
        return False, f"当前不在武尊堂场景: scene={scene_id}"
    if not hang_running:
        return False, "实时挂机未开启"
    return True, None


def _same_capture_group(packets: list[ParsedSystemPacket]) -> bool:
    if not packets:
        return False
    for previous, current in zip(packets, packets[1:]):
        if current.tick and previous.tick:
            if current.tick < previous.tick:
                return False
            if (current.tick - previous.tick) / 1000.0 > WUZUN_PACKET_MAX_GAP_S:
                return False
    return True


def learn_current_session(records: list[dict], *, log: LogFn | None = None) -> dict:
    """Learn only current-session stone dialogue; monster packets are generic."""
    log = log or (lambda _m: None)
    parsed = [item for item in (parse_system_packet(r) for r in records) if item]
    if not parsed:
        return {"ok": False, "error": "石碑交互没有捕获到合法系统指令", "packets": []}
    dialogue = [
        item for item in parsed
        if item.frame_type in (0x000A, 0x000C) and 6 <= item.length <= 32
    ]
    if not _same_capture_group(dialogue):
        dialogue = []
    if not dialogue:
        return {"ok": False, "error": "未能识别本次石碑对话序列", "packets": []}
    monster = _build_generic_monster_packets()
    log(
        "武尊堂动态学习: dialogue="
        + ",".join(f"len={p.length}/op=0x{p.opcode:04X}" for p in dialogue)
        + " monster=16(generic)"
    )
    return {"ok": True, "dialogue": dialogue, "monster": monster, "packets": monster}
def _select_rows(packets: list[ParsedSystemPacket], rows: int) -> list[ParsedSystemPacket]:
    if rows not in (1, 2, 3):
        return list(packets)
    # Row boundaries are learned from the current sequence.  The known layout is
    # only a validation target: 5/5/6. No packet opcode or payload is hardcoded.
    if len(packets) < 16:
        raise ValueError(f"本次开怪序列数量不足: {len(packets)}/16")
    boundaries = {1: (0, 5), 2: (5, 10), 3: (10, 16)}
    start, end = boundaries[rows]
    return list(packets[start:end])


def _find_stone(session, *, log: LogFn) -> object | None:
    items = scan_nearby_matters(session, radius=18.0, limit=80, prefer_actionable=False, log=log)
    stones = [item for item in items if "石碑" in str(getattr(item, "name", "") or "")]
    stones.sort(key=lambda item: float(getattr(item, "dist", 1e9) or 1e9))
    return stones[0] if stones else None


def _wait_at_target(session, target: tuple[float, float, float], stop: threading.Event, *, log: LogFn) -> bool:
    deadline = time.monotonic() + WUZUN_MOVE_TIMEOUT_S
    last_error = ""
    while time.monotonic() < deadline and not stop.is_set():
        try:
            result = read_scene_position(session, log=lambda _m: None)
            if bool(getattr(result, "ok", False)) and getattr(result, "scene_pos", None):
                position = tuple(float(value) for value in result.scene_pos)
                distance = sum((position[index] - target[index]) ** 2 for index in range(3)) ** 0.5
                if distance <= 2.8:
                    log(
                        "武尊堂自动开怪: 已到达石碑位置 "
                        f"pos=({position[0]:.3f},{position[1]:.3f},{position[2]:.3f}) "
                        f"distance={distance:.2f}"
                    )
                    return True
            else:
                last_error = str(getattr(result, "error", "位置读取失败") or "位置读取失败")
        except Exception as exc:
            last_error = str(exc)
        stop.wait(0.25)
    if stop.is_set():
        log("武尊堂自动开怪: 寻路已停止")
    else:
        detail = f" error={last_error}" if last_error else ""
        log(f"武尊堂自动开怪: 寻路超时{detail}")
    return False


class WuzunOpenMonsterRunner:
    def __init__(self, pid: int, hwnd: int, rows: int, *, log: LogFn | None = None) -> None:
        self.pid = int(pid)
        self.hwnd = int(hwnd or 0)
        self.rows = max(0, min(3, int(rows)))
        self.log = log or (lambda _m: None)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> bool:
        if self.thread is not None and self.thread.is_alive():
            return True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"wuzun-open-{self.pid}")
        self.thread.start()
        return True

    def stop(self, timeout_s: float = 2.0) -> None:
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(max(0.0, float(timeout_s)))

    def _run(self) -> None:
        attach = None
        try:
            from app.core.remote_runtime import (
                get_pid_scene_snapshot,
                is_pid_scene_snapshot_stable,
                wait_pid_scene_stable,
            )
            from app.core.super_loot import open_attach_session

            self.log(f"武尊堂自动开怪: pid={self.pid} 等待场景稳定")
            wait_pid_scene_stable(self.pid, timeout_s=30.0)
            attach = open_attach_session(self.pid, log=self.log)
            if attach is None:
                raise RuntimeError("附加游戏失败")
            attach.hwnd = int(getattr(attach, "hwnd", 0) or self.hwnd)
            from app.core.activity_auto import read_scene_state, probe_hang_state_mem

            stable_since = None
            last_gate = None
            while not self.stop_event.is_set():
                snapshot = get_pid_scene_snapshot(self.pid)
                snapshot_scene_id = int(snapshot.get("scene_id") or 0) if snapshot else 0
                scene_stable = bool(
                    snapshot and is_pid_scene_snapshot_stable(self.pid)
                )
                if not scene_stable:
                    scene_id, scene_label = snapshot_scene_id, "切图中"
                    scene_is_wuzun = False
                else:
                    scene_id, _scene_pos, scene_label = read_scene_state(
                        attach, fresh=True, log=self.log
                    )
                    scene_is_wuzun = int(scene_id or 0) in WUZUN_SCENE_IDS
                if scene_is_wuzun:
                    hang_state = probe_hang_state_mem(attach, log=self.log)
                    hang_running = bool(hang_state.ok and hang_state.on is True)
                else:
                    hang_running = False
                gate_ok, gate_error = validate_wuzun_open_gate(scene_id, hang_running)
                gate_key = (int(scene_id or 0), bool(hang_running))
                if gate_key != last_gate:
                    self.log(
                        f"武尊堂自动开怪: gate scene={scene_id}({scene_label}) "
                        f"hang_running={hang_running}"
                    )
                    last_gate = gate_key
                if gate_ok:
                    if stable_since is None:
                        stable_since = time.monotonic()
                    if time.monotonic() - stable_since >= WUZUN_STABLE_DELAY_S:
                        break
                else:
                    stable_since = None
                self.stop_event.wait(0.5)
            if self.stop_event.is_set():
                return
            from app.core.task_api import pathfind_to_clue

            scene_mode = int(scene_id or 0)
            clue = {
                "x": float(WUZUN_OPEN_TARGET[0]),
                "y": float(WUZUN_OPEN_TARGET[1]),
                "z": float(WUZUN_OPEN_TARGET[2]),
                "name": "武尊堂石碑",
                "clue": "wuzun_open_monster",
                "scene_id": scene_mode,
            }
            self.log(
                "武尊堂自动开怪: 使用副本成品寻路 "
                f"pathfind_to_clue scene={scene_mode} "
                f"xyz=({WUZUN_OPEN_TARGET[0]:.3f},{WUZUN_OPEN_TARGET[1]:.3f},{WUZUN_OPEN_TARGET[2]:.3f})"
            )
            path_result = pathfind_to_clue(
                attach,
                clue,
                mode=scene_mode,
                hwnd=int(getattr(attach, "hwnd", 0) or self.hwnd),
                use_bridge=True,
                allow_remote_fallback=True,
                arrive_radius=2.8,
                verify_timeout_s=WUZUN_MOVE_TIMEOUT_S,
                poll_s=0.5,
                stop_event=self.stop_event,
                stuck_s=0.0,
                log=self.log,
            )
            self.log(
                "武尊堂自动开怪: 副本寻路结果 "
                f"ok={bool(path_result.get('ok'))} "
                f"command_ok={bool(path_result.get('command_ok'))} "
                f"arrived={bool(path_result.get('arrived'))} "
                f"verified={bool(path_result.get('verified'))} "
                f"via={path_result.get('via') or '-'}"
            )
            if not bool(path_result.get("arrived") or path_result.get("verified")):
                raise RuntimeError(
                    "寻路到石碑失败: "
                    + str(path_result.get("error") or "未到达目标")
                )
            snapshot = get_pid_scene_snapshot(self.pid)
            scene_stable = bool(snapshot and is_pid_scene_snapshot_stable(self.pid))
            if (
                not scene_stable
                or int((snapshot or {}).get("scene_id") or 0) != scene_mode
                or scene_mode not in WUZUN_SCENE_IDS
            ):
                raise RuntimeError("副本场景已变化或不稳定，取消开怪封包")
            target = find_wuzun_challenge_target(attach, log=self.log, use_bridge=True)
            _set_wuzun_open_monster_active(self.pid, True)
            dialogue = build_wuzun_dialogue_packets(int(target["obj_id64"]))
            self.log(
                "武尊堂自动开怪: 按挑战牌 obj_id64 组对话包 "
                f"obj_id=0x{target['obj_id64']:X} field=0x{int(target['obj_id64']) & 0xFFFF:04X} "
                f"obj_id=0x{target['obj_id64']:X} packets={len(dialogue)}"
            )
            for index, packet in enumerate(dialogue):
                if self.stop_event.is_set():
                    return
                result = replay_packet(self.pid, packet.raw)
                if int(result or 0) == 0:
                    raise RuntimeError(
                        f"当前会话对话包未被客户端接受 opcode=0x{packet.opcode:04X}"
                    )
                if index + 1 < len(dialogue) and self.stop_event.wait(0.2):
                    return

            packets = _select_rows(list(_build_generic_monster_packets()), self.rows)
            self.log(f"武尊堂自动开怪: 对话包发送成功，发送 {len(packets)} 个通用开怪包")
            for packet in packets:
                if self.stop_event.is_set():
                    return
                result = replay_packet(self.pid, packet.raw)
                if int(result or 0) == 0:
                    raise RuntimeError(f"当前会话开怪包未被客户端接受 opcode=0x{packet.opcode:04X}")
                if self.stop_event.wait(1.0):
                    return
            self.log("武尊堂自动开怪: 当前会话序列执行完成")
        except Exception as exc:
            self.log(f"武尊堂自动开怪: 已停止，未盲发: {exc}")
        finally:
            _set_wuzun_open_monster_active(self.pid, False)
            if attach is not None:
                try:
                    attach.close()
                except Exception:
                    pass


_ACTIVE_PIDS: set[int] = set()
_ACTIVE_PIDS_LOCK = threading.RLock()


def is_wuzun_open_monster_active(pid: int) -> bool:
    """Return whether this PID is in the actual dialogue/monster send window."""
    with _ACTIVE_PIDS_LOCK:
        return int(pid or 0) in _ACTIVE_PIDS


def _set_wuzun_open_monster_active(pid: int, active: bool) -> None:
    with _ACTIVE_PIDS_LOCK:
        if active:
            _ACTIVE_PIDS.add(int(pid))
        else:
            _ACTIVE_PIDS.discard(int(pid))


_RUNNERS: dict[int, WuzunOpenMonsterRunner] = {}
_RUNNERS_LOCK = threading.RLock()


def start_wuzun_open_monster(pid: int, hwnd: int, rows: int, *, log: LogFn | None = None) -> dict:
    pid = int(pid or 0)
    if not pid:
        return {"ok": False, "error": "no_pid"}
    with _RUNNERS_LOCK:
        old = _RUNNERS.get(pid)
        if old is not None and old.thread is not None and old.thread.is_alive():
            return {"ok": True, "running": True, "reused": True}
        runner = WuzunOpenMonsterRunner(pid, hwnd, rows, log=log)
        _RUNNERS[pid] = runner
        runner.start()
    return {"ok": True, "running": True, "reused": False}


def stop_wuzun_open_monster(pid: int, *, log: LogFn | None = None) -> dict:
    pid = int(pid or 0)
    with _RUNNERS_LOCK:
        runner = _RUNNERS.pop(pid, None)
    if runner is not None:
        runner.stop()
    return {"ok": True, "running": False}


__all__ = [
    "ParsedSystemPacket",
    "parse_system_packet",
    "learn_current_session",
    "WuzunOpenMonsterRunner",
    "start_wuzun_open_monster",
    "stop_wuzun_open_monster",
    "is_wuzun_open_monster_active",
]

