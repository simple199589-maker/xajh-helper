# -*- coding: utf-8 -*-
"""Fixed daily-task acceptance orchestration and packet diagnostics.

The task ids are intentionally static, while NPC metadata, world positions,
accepted state, and packet context are resolved per game process at runtime.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

from app.core.task_api import (
    find_nearby_task_npc,
    get_task_npc_world,
    find_task_clue_targets,
    choose_task_route_clue,
    list_nearby_npcs,
    list_portal_npc_candidates,
    classify_task_portal_kind,
    resolve_dungeon_tier,
    dungeon_open_packet_for,
    dungeon_talk_packet_for,
    pathfind_task,
    list_accepted_task_ids,
    pathfind_to_clue,
    read_task_npc_tids,
)

LogFn = Callable[[str], None]

# 用户确认首版所有账号固定使用已验证可接任务的默认尾部。
# 捕获结果只用于诊断和后续推导，不参与当前自动发送。
DEFAULT_PACKET_SUFFIX = bytes.fromhex("FFFFFF")

# 已由正常接任务包确认的账号尾部；未知账号暂按用户指定的 FFFFFF 兜底。
KNOWN_PACKET_SUFFIXES: dict[str, bytes] = {
    "19636225": bytes.fromhex("FFFFFF"),
    "19644417": bytes.fromhex("FF51B6"),
    "20230145": bytes.fromhex("FF50B8"),
}

# NPC route coordinates discovered from task clues are reusable within one game
# process. The arrival check remains mandatory before any packet is sent.
_DAILY_NPC_ROUTE_CACHE: dict[tuple[int, int], dict] = {}
_DAILY_NPC_ROUTE_CACHE_LOCK = threading.RLock()
_DAILY_NPC_ROUTE_FILE = Path.cwd() / ".issues" / "cache" / "daily_npc_routes.json"


def _daily_route_account_key(session) -> str:
    role_id = str(getattr(session, "role_id", "") or "").strip()
    return role_id or f"pid:{int(getattr(session, 'pid', 0) or 0)}"


def _load_daily_route_cache(session, npc_tid: int) -> dict:
    account_key = _daily_route_account_key(session)
    try:
        raw = json.loads(_DAILY_NPC_ROUTE_FILE.read_text(encoding="utf-8"))
        value = raw.get(account_key, {}).get(str(int(npc_tid)), {})
        return dict(value) if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_daily_route_cache(session, npc_tid: int, route: dict) -> None:
    account_key = _daily_route_account_key(session)
    try:
        _DAILY_NPC_ROUTE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw = json.loads(_DAILY_NPC_ROUTE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        account_routes = raw.setdefault(account_key, {})
        if not isinstance(account_routes, dict):
            account_routes = {}
            raw[account_key] = account_routes
        account_routes[str(int(npc_tid))] = {
            key: route.get(key)
            for key in ("x", "y", "z", "scene_id", "tid", "name", "source")
            if route.get(key) is not None
        }
        tmp = _DAILY_NPC_ROUTE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_DAILY_NPC_ROUTE_FILE)
    except OSError:
        pass

DAILY_TASKS: tuple[dict, ...] = (
    {"task_id": 10010, "name": "初级地宫龙傲天", "npc_group": "dungeon_primary"},
    {"task_id": 10007, "name": "初级地宫打怪", "npc_group": "dungeon_primary"},
    {"task_id": 10008, "name": "中级地宫BOSS", "npc_group": "dungeon_middle"},
    {"task_id": 10012, "name": "中级地宫打怪", "npc_group": "dungeon_middle"},
    {"task_id": 10006, "name": "高级地宫BOSS", "npc_group": "dungeon_high"},
    {"task_id": 10009, "name": "高级地宫打怪", "npc_group": "dungeon_high"},
    {"task_id": 10011, "name": "升级地宫BOSS", "npc_group": "dungeon_leveling"},
    {"task_id": 10026, "name": "宋瑶140杀怪", "npc_group": "songyao"},
    {"task_id": 10028, "name": "宋瑶140副本1", "npc_group": "songyao"},
    {"task_id": 10027, "name": "宋瑶140副本2", "npc_group": "songyao"},
    {"task_id": 10020, "name": "每日组队本", "npc_group": "bubai"},
    {"task_id": 10021, "name": "每日霸刀", "npc_group": "bubai"},
    {"task_id": 10025, "name": "每日武尊堂", "npc_group": "bubai"},
)

TASK_ACCEPT_INTERVAL_S = 1.5
NEXT_NPC_GROUP_INTERVAL_S = 2.0


NPC_DIALOG_PACKETS: dict[str, tuple[bytes, ...]] = {
    "songyao": (bytes.fromhex("0A00EA07000000000001"), bytes.fromhex("0C00EA07000000000001")),
    "bubai": (bytes.fromhex("0A00E907000000000001"), bytes.fromhex("0C00E907000000000001")),
}

DAILY_DUNGEON_TIERS: dict[str, str] = {
    "dungeon_primary": "初级",
    "dungeon_middle": "中级",
    "dungeon_high": "高级",
    "dungeon_leveling": "升级",
}


def daily_npc_dialog_packets(group: str, delv_tid: int | None = None) -> tuple[bytes, ...] | None:
    """Resolve the two NPC packets for one daily-task NPC."""
    key = str(group or "")
    if key in DAILY_DUNGEON_TIERS:
        portal = {"tid": int(delv_tid or 0), "name": f"{DAILY_DUNGEON_TIERS[key]}地宫传送"}
        open_packet = dungeon_open_packet_for(portal)
        talk_packet = dungeon_talk_packet_for(portal)
        if open_packet is None or talk_packet is None:
            return None
        return open_packet, talk_packet
    return NPC_DIALOG_PACKETS.get(key)

# The final three bytes may vary by account. 首版默认使用 DEFAULT_PACKET_SUFFIX.
TASK_PACKET_PREFIX: dict[int, bytes] = {
    10008: bytes.fromhex("0E00071C270000000000000000000000000000"),
    10012: bytes.fromhex("0E000718270000000000000000000000000000"),
    10010: bytes.fromhex("0E00071A270000000000000000000000000000"),
    10007: bytes.fromhex("0E000717270000000000000000000000000000"),
    10006: bytes.fromhex("0E000716270000000000000000000000000000"),
    10009: bytes.fromhex("0E000719270000000000000000000000000000"),
    10011: bytes.fromhex("0E00071B270000000000000000000000000000"),
    10026: bytes.fromhex("0E00072A270000000000000000000000000000"),
    10028: bytes.fromhex("0E00072C270000000000000000000000000000"),
    10027: bytes.fromhex("0E00072B270000000000000000000000000000"),
    10020: bytes.fromhex("0E000724270000000000000000000000000000"),
    10021: bytes.fromhex("0E000725270000000000000000000000000000"),
    10025: bytes.fromhex("0E000729270000000000000000000000000000"),
}


@dataclass(frozen=True)
class PacketContext:
    pid: int
    role_id: str = ""
    suffix: bytes = b""
    source: str = ""
    verified: bool = False


@dataclass
class DailyAcceptResult:
    ok: bool
    accepted: list[int]
    skipped: list[int]
    failed: list[int]
    error: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def daily_task_definitions() -> list[dict]:
    return [dict(row) for row in DAILY_TASKS]


def build_daily_accept_packet(task_id: int, context: PacketContext) -> bytes:
    """Build a fixed-format daily accept packet for the current first version."""
    tid = int(task_id)
    prefix = TASK_PACKET_PREFIX.get(tid)
    if prefix is None:
        raise ValueError(f"unsupported daily task id {tid}")
    if not context.verified or len(context.suffix) != 3:
        raise RuntimeError("daily accept packet context is unverified")
    return prefix + context.suffix


def resolve_daily_accept_packet_context(session, *, log: LogFn | None = None) -> PacketContext:
    """Return the fixed FFFFFF context selected for the first release."""
    log = log or (lambda _m: None)
    pid = int(getattr(session, "pid", 0) or 0)
    role_id = str(getattr(session, "role_id", "") or "").strip()
    suffix = KNOWN_PACKET_SUFFIXES.get(role_id, DEFAULT_PACKET_SUFFIX)
    source = "known_role_mapping" if role_id in KNOWN_PACKET_SUFFIXES else "fixed_default"
    ctx = PacketContext(
        pid=pid,
        role_id=role_id,
        suffix=suffix,
        source=source,
        verified=True,
    )
    log(f"daily accept packet context pid={pid} source={ctx.source} suffix={ctx.suffix.hex().upper()}")
    return ctx


def _role_id(session) -> str:
    value = str(getattr(session, "role_id", "") or "").strip()
    return value


def _task_id_from_packet(data: bytes) -> int | None:
    raw = bytes(data or b"")
    if len(raw) < 5 or raw[:2] != b"\x0e\x00":
        return None
    # opcode byte at offset 3 and task opcode at offset 4 identify this family.
    for task_id, prefix in TASK_PACKET_PREFIX.items():
        if raw.startswith(prefix) and len(raw) == len(prefix) + 3:
            return task_id
    return None


def _packet_suffix(data: bytes) -> bytes:
    return bytes(data[-3:]) if len(data) >= 3 else b""


def analyze_daily_accept_records(records: list[dict], *, pid: int, role_id: str = "") -> dict:
    """Extract task packets and report differing suffixes without guessing."""
    tasks: list[dict] = []
    dialogs: list[str] = []
    suffixes: set[bytes] = set()
    for index, rec in enumerate(records or []):
        data = bytes(rec.get("data") or b"")
        hx = data.hex().upper()
        if data[:2] == b"\x0a\x00" or data[:2] == b"\x0c\x00":
            dialogs.append(hx)
        tid = _task_id_from_packet(data)
        if tid is not None:
            suffix = _packet_suffix(data)
            suffixes.add(suffix)
            tasks.append({"index": index, "task_id": tid, "hex": hx, "suffix": suffix.hex().upper(), "tick": rec.get("tick")})
    result = {
        "pid": int(pid), "role_id": str(role_id or ""), "dialogs": dialogs,
        "tasks": tasks, "suffixes": sorted(x.hex().upper() for x in suffixes),
        "verified": len(suffixes) == 1 and bool(tasks),
        "note": "single stable suffix" if len(suffixes) == 1 and tasks else "未捕获任务包或动态尾部不一致",
    }
    return result


def save_daily_accept_capture(report: dict, *, directory: Path | None = None) -> Path:
    target = directory or (Path.cwd() / ".issues" / "packets" / "daily_accept")
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"daily_accept_{int(report.get('pid') or 0)}_{int(time.time())}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _wait(stop_event: threading.Event | None, seconds: float) -> bool:
    if stop_event is not None:
        return not stop_event.wait(max(0.0, seconds))
    time.sleep(max(0.0, seconds))
    return True


def _send_packet_or_raise(send_packet, pid: int, payload: bytes, *, label: str, log: LogFn) -> int:
    result = int(send_packet(pid, payload, timeout_ms=3000, log=log))
    log(f"daily accept send label={label} ret={result} hex={bytes(payload).hex().upper()}")
    if result != 1:
        raise RuntimeError(f"{label}发包入口拒绝 ret={result}")
    return result

def _resolve_daily_npc_route(session, row: dict, delv_tid: int, *, hwnd: int = 0, log: LogFn) -> dict | None:
    pid = int(getattr(session, "pid", 0) or 0)
    is_dungeon_portal = int(delv_tid or 0) in {100218, 100219, 100220, 100221}
    cache_key = (pid, int(delv_tid or 0))
    if is_dungeon_portal:
        # DelvNPC 100218..100221 is the 福州城传送口 service TID, not a
        # dungeon-map NPC coordinate. Never reuse the old generic route cache.
        try:
            from app.core.automove import read_scene_position
            scene = read_scene_position(session, log=lambda _m: None)
            scene_id = int(getattr(scene, "scene_id", 0) or 0)
            if scene_id != 68:
                log(
                    f"daily accept 当前场景不是福州城 scene={scene_id}; "
                    f"先按任务线索寻福州城，再找地宫传送口 tid={delv_tid}"
                )
                try:
                    clues = find_task_clue_targets(
                        session,
                        {"task_id": int(row["task_id"]), "can_finish": False},
                        log=log,
                    )
                    city_route = next(
                        (
                            dict(item)
                            for item in clues
                            if int(item.get("scene_id") or 0) == 68
                            and item.get("x") is not None
                            and item.get("z") is not None
                        ),
                        None,
                    )
                except Exception as exc:
                    log(f"daily accept 福州城任务线索解析失败: {exc}")
                    city_route = None
                if city_route is None:
                    log("daily accept 未找到福州城 scene=68 任务线索，停止避免误跑")
                    return None
                city_route["clue"] = "福州城地宫传送口"
                city_route["name"] = city_route.get("name") or "福州城"
                city_route["source"] = "fuzhou_task_clue"
                moved_city = pathfind_to_clue(
                    session,
                    city_route,
                    hwnd=int(hwnd or 0),
                    arrive_radius=18.0,
                    verify_timeout_s=120.0,
                    log=log,
                )
                if not moved_city.get("ok"):
                    log(f"daily accept 寻福州城失败: {moved_city.get('error') or moved_city.get('note')}")
                    return None
                log("daily accept 已到达福州城，继续扫描地宫传送口")
            kind = classify_task_portal_kind(row) or "dungeon_upper"
            tier = resolve_dungeon_tier(row, prefer_tid=int(delv_tid))
            nearby = list_nearby_npcs(session, radius=200.0, limit=64, log=log)
            candidates = list_portal_npc_candidates(
                nearby,
                kind,
                tier=tier,
                prefer_tid=int(delv_tid),
                max_n=1,
            )
        except Exception as exc:
            log(f"daily accept 福州地宫传送口扫描失败 tid={delv_tid}: {exc}")
            candidates = []
        if not candidates:
            log(
                f"daily accept 未找到福州城地宫传送口 tid={delv_tid}; "
                "不会寻路到地宫地图NPC"
            )
            return None
        portal = dict(candidates[0])
        portal["tid"] = int(delv_tid)
        portal["source"] = "fuzhou_portal_live"
        with _DAILY_NPC_ROUTE_CACHE_LOCK:
            _DAILY_NPC_ROUTE_CACHE[cache_key] = dict(portal)
        _save_daily_route_cache(session, delv_tid, portal)
        log(
            f"daily accept 使用福州城传送口 tid={delv_tid} "
            f"name={portal.get('name')} xyz=({portal.get('x')},{portal.get('y')},{portal.get('z')})"
        )
        return portal

    with _DAILY_NPC_ROUTE_CACHE_LOCK:
        cached = dict(_DAILY_NPC_ROUTE_CACHE.get(cache_key) or {})
    if not cached:
        cached = _load_daily_route_cache(session, delv_tid)
        if cached:
            with _DAILY_NPC_ROUTE_CACHE_LOCK:
                _DAILY_NPC_ROUTE_CACHE[cache_key] = dict(cached)
    if cached.get("x") is not None and cached.get("z") is not None:
        cached["source"] = "daily_route_cache"
        log(
            f"daily accept cached NPC route tid={delv_tid} "
            f"scene={cached.get('scene_id')} xyz=({cached.get('x')},{cached.get('y')},{cached.get('z')})"
        )
        return cached
    cfg = get_task_npc_world(session, delv_tid, log=log) if delv_tid else None
    if cfg and cfg.get("ok") and cfg.get("x") is not None and cfg.get("z") is not None:
        route = {
            "clue": "接任务NPC",
            "kind": "cfg_npc",
            "name": str(row.get("npc_group") or "接任务NPC"),
            "x": cfg.get("x"),
            "y": cfg.get("y") or 0.0,
            "z": cfg.get("z"),
            "scene_id": cfg.get("scene_id"),
            "tid": int(delv_tid),
            "source": "cfg_object",
        }
        with _DAILY_NPC_ROUTE_CACHE_LOCK:
            _DAILY_NPC_ROUTE_CACHE[cache_key] = dict(route)
        _save_daily_route_cache(session, delv_tid, route)
        return route
    log(f"daily accept static NPC position unavailable tid={delv_tid}; trying task clue route")
    try:
        clues = find_task_clue_targets(
            session,
            {"task_id": int(row["task_id"]), "can_finish": False},
            log=log,
        )
        route = choose_task_route_clue(
            clues, for_complete=False, npc_tid=int(delv_tid or 0)
        )
    except Exception as exc:
        log(f"daily accept task clue route failed tid={delv_tid}: {exc}")
        return None
    if not route or route.get("x") is None or route.get("z") is None:
        log(f"daily accept no verified fallback route tid={delv_tid}")
        return None
    route = dict(route)
    route["tid"] = int(delv_tid)
    with _DAILY_NPC_ROUTE_CACHE_LOCK:
        _DAILY_NPC_ROUTE_CACHE[cache_key] = dict(route)
    _save_daily_route_cache(session, delv_tid, route)
    log(
        f"daily accept fallback route cached tid={delv_tid} source={route.get('source')} "
        f"name={route.get('name')} scene={route.get('scene_id')} "
        f"xyz=({route.get('x')},{route.get('y')},{route.get('z')})"
    )
    return route

def _move_daily_to_npc(
    session,
    row: dict,
    delv_tid: int,
    *,
    hwnd: int,
    stop_event: threading.Event | None,
    log: LogFn,
) -> dict:
    """Move to the daily NPC in Fuzhou, matching the live NPC TID."""
    try:
        from app.core.automove import read_scene_position
        scene = read_scene_position(session, log=lambda _m: None)
        scene_id = int(getattr(scene, "scene_id", 0) or 0)
    except Exception:
        scene_id = 0
    if scene_id != 68:
        try:
            clues = find_task_clue_targets(
                session,
                {"task_id": int(row["task_id"]), "can_finish": False},
                log=log,
            )
            city_route = next(
                (
                    dict(item)
                    for item in clues
                    if int(item.get("scene_id") or 0) == 68
                    and item.get("x") is not None
                    and item.get("z") is not None
                ),
                None,
            )
        except Exception as exc:
            log(f"daily accept 福州城路线解析失败 task={row.get('task_id')}: {exc}")
            city_route = None
        if city_route is None:
            return {"ok": False, "error": "未找到福州城路线，停止避免跑到其他地图NPC"}
        city_route["clue"] = "福州城日常接任务NPC"
        moved_city = pathfind_to_clue(
            session,
            city_route,
            hwnd=int(hwnd or 0),
            stop_event=stop_event,
            arrive_radius=18.0,
            verify_timeout_s=120.0,
            log=log,
        )
        if not moved_city.get("ok"):
            return moved_city
    if int(delv_tid or 0) in {100218, 100219, 100220, 100221}:
        clue = _resolve_daily_npc_route(
            session, row, delv_tid, hwnd=hwnd, log=log
        )
        if clue is None:
            return {"ok": False, "error": f"未找到福州城地宫传送口 tid={delv_tid}"}
    else:
        live = find_nearby_task_npc(
            session, int(delv_tid), radius=200.0, log=log
        )
        if live is None:
            try:
                clues = find_task_clue_targets(
                    session,
                    {"task_id": int(row["task_id"]), "can_finish": False},
                    log=log,
                )
                clue = next(
                    (
                        dict(item)
                        for item in clues
                        if int(item.get("scene_id") or 0) == 68
                        and int(item.get("tid") or 0) == int(delv_tid)
                        and item.get("x") is not None
                        and item.get("z") is not None
                    ),
                    None,
                )
            except Exception as exc:
                log(f"daily accept 福州城NPC线索解析失败 tid={delv_tid}: {exc}")
                clue = None
            if clue is None:
                return {
                    "ok": False,
                    "error": f"福州城未找到日常NPC tid={delv_tid}，停止避免跑其他地图",
                }
        else:
            clue = dict(live)
            clue["source"] = "fuzhou_live_task_npc"
    return pathfind_to_clue(
        session,
        clue,
        hwnd=int(hwnd or 0),
        stop_event=stop_event,
        arrive_radius=12.0,
        verify_timeout_s=90.0,
        log=log,
    )



def accept_daily_tasks_routed(session, *, hwnd: int = 0, stop_event: threading.Event | None = None,
                              log: LogFn | None = None) -> DailyAcceptResult:
    """Accept all fixed daily tasks using per-account state and packet context."""
    log = log or (lambda _m: None)
    context = resolve_daily_accept_packet_context(session, log=log)
    accepted = set(int(value) for value in list_accepted_task_ids(session, log=log))
    pending = [dict(row) for row in DAILY_TASKS if int(row["task_id"]) not in accepted]
    skipped = [int(row["task_id"]) for row in DAILY_TASKS if int(row["task_id"]) in accepted]
    log(
        f"daily accept initial accepted={sorted(accepted)} "
        f"pending_count={len(pending)} "
        f"pending={[int(row['task_id']) for row in pending]}"
    )
    done: list[int] = []
    failed: list[int] = []
    from app.core.game_send import send_raw_packet

    groups: list[tuple[str, list[dict]]] = []
    for row in pending:
        key = str(row["npc_group"])
        found = next((item for item in groups if item[0] == key), None)
        if found is None:
            groups.append((key, [row]))
        else:
            found[1].append(row)

    for group_index, (group, rows) in enumerate(groups):
        if stop_event is not None and stop_event.is_set():
            return DailyAcceptResult(False, done, skipped, failed, "stopped")
        task_npcs = {
            int(row["task_id"]): int(
                read_task_npc_tids(session, int(row["task_id"]), log=log).get("delv_tid") or 0
            )
            for row in rows
        }
        npc_tids = set(task_npcs.values())
        if 0 in npc_tids or len(npc_tids) != 1:
            return DailyAcceptResult(
                False,
                done,
                skipped,
                failed,
                f"同组接任务NPC不一致 group={group} tids={task_npcs}",
            )
        delv_tid = next(iter(npc_tids))
        dialogs = daily_npc_dialog_packets(group, delv_tid)
        if not dialogs:
            return DailyAcceptResult(False, done, skipped, failed, f"缺少NPC对话包 group={group}")
        moved = _move_daily_to_npc(
            session,
            rows[0],
            delv_tid,
            hwnd=hwnd,
            stop_event=stop_event,
            log=log,
        )
        if not moved.get("ok"):
            return DailyAcceptResult(
                False, done, skipped, failed, str(moved.get("error") or "NPC寻路失败")
            )
        try:
            for dialog_index, dialog in enumerate(dialogs, start=1):
                _send_packet_or_raise(
                    send_raw_packet,
                    int(session.pid),
                    dialog,
                    label=f"NPC对话#{dialog_index}",
                    log=log,
                )
        except Exception as exc:
            return DailyAcceptResult(False, done, skipped, failed, str(exc))
        if not _wait(stop_event, 0.2):
            return DailyAcceptResult(False, done, skipped, failed, "stopped")
        for index, row in enumerate(rows):
            tid = int(row["task_id"])
            try:
                _send_packet_or_raise(
                    send_raw_packet,
                    int(session.pid),
                    build_daily_accept_packet(tid, context),
                    label=f"任务#{tid}",
                    log=log,
                )
                if index < len(rows) - 1:
                    delay_s = TASK_ACCEPT_INTERVAL_S
                elif group_index < len(groups) - 1:
                    delay_s = NEXT_NPC_GROUP_INTERVAL_S
                else:
                    delay_s = 0.0
                if delay_s and not _wait(stop_event, delay_s):
                    return DailyAcceptResult(False, done, skipped, failed, "stopped")
                done.append(tid)
            except Exception as exc:
                failed.append(tid)
                return DailyAcceptResult(False, done, skipped, failed, str(exc))
    return DailyAcceptResult(True, done, skipped, failed, note="日常任务接取完成")

__all__ = ["DAILY_TASKS", "PacketContext", "DailyAcceptResult", "daily_task_definitions",
           "build_daily_accept_packet", "resolve_daily_accept_packet_context",
           "analyze_daily_accept_records", "save_daily_accept_capture", "accept_daily_tasks_routed"]
