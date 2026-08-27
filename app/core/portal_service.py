# -*- coding: utf-8 -*-
"""
Dungeon portal via memory + native functions (not NPC dialog geometry clicks).

Preferred path:
  tier + layer(上层/深处) → scene_id → HostMoveToScenePosition(scene,x,y,z)
  optional: bridge INSTANCE_ENTER(inst=scene_id) as secondary attempt

Legal notes (RE 2026-07-22, base 0x400000):
  - BigWorldEnterUnderground @ 0x667B30 / GenerateTransList* build *path lists*
    from transmit objects; they are NOT free "teleport(scene, layer)" APIs.
  - 副本列表 enter is bridge CMD_INSTANCE_ENTER → thiscall 0xCC6F80 (pkt 0x58).
  - HostMoveToScenePosition(scene_id, x,y,z) is the client AutoMove entry and
    is what task pathfinding already uses for cross-map ReachSite.
  - Do NOT forge arbitrary free-teleport packets. Scene change is success.

@author by ak
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from app.core.game_attach import GameAttachSession

LogFn = Callable[[str], None]


def _pid_blocked(session: GameAttachSession | int) -> tuple[bool, str]:
    """SafeDispatch remote gate. @author by ak"""
    try:
        from app.core.safe_dispatch import session_blocked

        return session_blocked(session)
    except Exception:
        return False, ""


def _prefetch_portal_states(session: GameAttachSession, *, log: LogFn | None = None) -> None:
    """非马上需要：进本前预热场景/坐标。@author by ak"""
    log = log or (lambda _m: None)
    try:
        from app.core.state_dispatch import StateKind, warmup_session

        warmup_session(
            session,
            (StateKind.SCENE, StateKind.POS),
            log=lambda m: log(f"portal warmup: {m}") if m else None,
        )
    except Exception as e:
        log(f"portal warmup skip: {e}")


# Live delv tid → 初/中/高/升级 (福州 2026-07-22)
PORTAL_TID_TIER: dict[int, str] = {
    100218: "中级",
    100219: "初级",
    100220: "高级",
    100221: "升级",
}

# Scene id bands used by layer checks (task_api)
PORTAL_UPPER_SCENE_IDS = frozenset({2014, 2016, 2018, 2020, 2022, 2024})
# 2032/2036 etc. are live deep instances outside the classic *15/*17 pair band.
PORTAL_LOWER_SCENE_IDS = frozenset(
    {2015, 2017, 2019, 2021, 2023, 2025, 2028, 2029, 2030, 2031, 2032, 2033, 2034, 2035, 2036, 2037}
)
PORTAL_DEEP_SCENE_IDS = PORTAL_LOWER_SCENE_IDS

# tier → layer → ordered scene candidates
# Source: .issues/break0/all_maps.txt + live layer bands (2026-07-22)
# layer keys: upper | deep  (dungeon_lower aliases deep)
DUNGEON_SCENE_BY_TIER: dict[str, dict[str, list[int]]] = {
    "初级": {
        # 地火 / 苦水 / 康巴 (daily primary band + recon names)
        "upper": [2014, 2016, 2018],
        "deep": [2015, 2017, 2019, 2028, 2029, 2032, 2033],
    },
    "中级": {
        "upper": [2020, 2022],
        "deep": [2021, 2023, 2030, 2031],
    },
    "高级": {
        "upper": [2024, 2026],
        "deep": [2025, 2027, 2034, 2035],
    },
    "升级": {
        # 西王母 / leveling — live map name: 2036=西王母宫(深处), 2024=上层
        "upper": [2024],
        "deep": [2025, 2036, 2037],
    },
    "练级": {
        "upper": [2024],
        "deep": [2025, 2036, 2037],
    },
}

# Default spawn-ish points inside dungeon scenes (d_cmd samples / RE notes).
# Used only as HostMove targets; server still validates enter.

# ---------------------------------------------------------------------------
# NPC 传送参数（RE 汇总 2026-07-22）— 不是免费瞬移 API
#
# A) 福州城「每日地宫传送」口（主路径，已接 task_api）
#    必须参数:
#      1. portal_tid   : 100219初级 / 100218中级 / 100220高级 / 100221升级
#      2. live obj_id  : 附近实体 int64（SayHello 用）
#      3. layer        : upper|deep  （菜单 上层/深处）
#    调用链:
#      HostMove(near npc) → SetTarget(obj) → NPCSayHello(obj_id)
#      → UI 点选 上层/深处 → 等 scene 变化
#    plg 导出仅有:
#      NPCSayHello(int64) / GetHostPlayerCurServNPC()
#    未找到: SelectService / ClickService / EnterDungeon(tid,layer) 导出
#
# B) 脚本 UndergroundTransInfo（curnpclist/elements 传送服务 id，非福州口）
#    fromBigWorld[world_sid] = [entry_tid, ...]  入口模板/服务 id
#    例: 野人峡谷 2020 fromBigWorld[83]={64232}
#    这些是原地图入口 NPC 模板，不是福州 100218 系列。
#
# C) 明确不可当主路径:
#    HostMove(dungeon_scene) → 客户端寻路，服务端可拒 / scene=-1 假成功
#    INSTANCE_ENTER pkt0x58 → 副本列表 Btn_Enter，不是地宫传送
#    GenerateTransList / BigWorldEnterUnderground → 建寻径表，不是瞬移
# ---------------------------------------------------------------------------

# 福州每日口：任务 DelvNPC tid → 档位
PORTAL_FUZHOU_TID: dict[int, str] = dict(PORTAL_TID_TIER)

# 菜单层枚举（与 task_api portal_kind 对齐）
PORTAL_LAYER_UPPER = "upper"
PORTAL_LAYER_DEEP = "deep"

# 脚本层入口（原地图 world_sid → entry template id）
# 来源: script_229050 UndergroundTransInfo.fromBigWorld
# 用途: 文档/对照；福州每日口不走这张表。
UNDERGROUND_ENTRY_FROM_BIGWORLD: dict[int, dict[int, list[int]]] = {
    # dungeon_scene -> { world_scene: [entry_tids] }
    2014: {77: [58892]},          # 地火剑池 上层
    2015: {77: [58892, 58669]},   # 地火 深处(+层内入口)
    2016: {78: [60596]},          # 苦水 上层
    2017: {78: [60596, 60652]},
    2018: {90: [60950]},          # 康巴 上层
    2019: {90: [60950, 62494]},
    2032: {90: [60950]},
    2020: {83: [64232]},          # 野人峡谷 上层
    2021: {83: [64232, 66016]},
    2030: {83: [64232]},          # 野人(深处变体)
    2022: {91: [76132]},          # 俺答 上层
    2023: {91: [76132, 76037]},
    2034: {91: [76132]},
    2024: {110: [84972]},         # 西王母 上层
    2025: {110: [84972, 79795]},
    2036: {110: [84972]},
}

# 福州每日口 → 目标 scene 候选（与 DUNGEON_SCENE_BY_TIER 一致，便于查参）
# 真正进本仍靠 NPC 服务菜单，不能只凭 scene_id 发包。
PORTAL_NPC_REQUIRED_PARAMS = (
    "portal_tid",   # 100218..100221
    "obj_id",       # live entity id64
    "layer",        # upper|deep
    # optional after hello:
    # "cur_serv_npc",  # GetHostPlayerCurServNPC()
    # "menu_option",   # 上层/深处 或 服务选项 index — 协议字段未解
)


def describe_portal_npc_params(
    *,
    portal_tid: int = 0,
    layer: str = "upper",
    tier: str = "",
) -> dict:
    """
    Resolve documented parameters for 福州 NPC portal (no free enter).

    Returns a dict of known ids for logging / future packet RE.
    """
    pt = int(portal_tid or 0)
    t = str(tier or PORTAL_FUZHOU_TID.get(pt, "") or "").strip()
    ly = _normalize_layer(layer)
    scenes = resolve_dungeon_scene_ids(t, ly) if t else []
    entry_map: dict[int, list[int]] = {}
    for sid in scenes:
        row = UNDERGROUND_ENTRY_FROM_BIGWORLD.get(int(sid)) or {}
        for ws, ids in row.items():
            entry_map[int(ws)] = list(ids)
    return {
        "portal_tid": pt,
        "tier": t,
        "layer": ly,
        "target_scene_ids": scenes,
        # 原地图入口模板 id（对照用，非福州 SayHello 参数）
        "script_entry_from_bigworld": entry_map,
        "plg_open": "NPCSayHello(obj_id:int64)",
        "plg_cur_serv": "GetHostPlayerCurServNPC() -> int64",
        "mem_select": {
            "fn": "AA3330 @0xAA3330 (event 0x80000011)",
            "talk_proc": "ptr_talk_proc +0x84 count +0x88 entries stride 0x18",
            "module": "app.core.npc_service_mem",
        },
        "missing_for_direct_enter": [
            "free EnterDungeon(tid,layer) packet",
            "stable public SelectService export",
        ],
        "legal_path": "walk + SayHello + talk_proc opt_id + bridge AA3330 + scene verify",
    }


DEFAULT_DUNGEON_XYZ: dict[int, tuple[float, float, float]] = {
    2014: (44.0, 0.0, -13.0),
    2015: (44.0, 0.0, -13.0),
    2016: (40.0, 0.0, -10.0),
    2017: (40.0, 0.0, -10.0),
    2018: (40.0, 0.0, -10.0),
    2019: (40.0, 0.0, -10.0),
    2020: (40.0, 0.0, -10.0),
    2021: (40.0, 0.0, -10.0),
    2022: (40.0, 0.0, -10.0),
    2023: (40.0, 0.0, -10.0),
    2024: (40.0, 0.0, -10.0),
    2025: (40.0, 0.0, -10.0),
    2028: (40.0, 0.0, -10.0),
    2029: (40.0, 0.0, -10.0),
    2030: (40.0, 0.0, -10.0),
    2031: (40.0, 0.0, -10.0),
    2032: (40.0, 0.0, -10.0),
    2033: (40.0, 0.0, -10.0),
    2034: (40.0, 0.0, -10.0),
    2035: (40.0, 0.0, -10.0),
    2036: (40.0, 0.0, -10.0),
    2037: (40.0, 0.0, -10.0),
}


def _log(log: LogFn | None, msg: str) -> None:
    if log:
        try:
            log(msg)
        except Exception:
            pass


def _normalize_layer(layer: str) -> str:
    s = str(layer or "").strip().lower()
    if s in ("deep", "dungeon_deep", "dungeon_lower", "lower", "深处", "下层"):
        return "deep"
    if s in ("upper", "dungeon_upper", "上层", "一层"):
        return "upper"
    if "deep" in s or "lower" in s or "深处" in str(layer) or "下层" in str(layer):
        return "deep"
    return "upper"


def resolve_dungeon_scene_ids(tier: str, layer: str) -> list[int]:
    """Map tier + upper/deep → candidate scene ids (ordered)."""
    t = str(tier or "").strip()
    ly = _normalize_layer(layer)
    row = DUNGEON_SCENE_BY_TIER.get(t) or {}
    ids = list(row.get(ly) or [])
    if ids:
        return ids
    # fallback by layer band only
    if ly == "deep":
        return sorted(PORTAL_DEEP_SCENE_IDS)
    return sorted(PORTAL_UPPER_SCENE_IDS)


def _scene_id_now(session, *, fresh: bool = False) -> int | None:
    """Positive scene id; uses shared state layer (soft/fresh). @author by ak"""
    try:
        from app.core.state_dispatch import StateKind, get_state

        sc = get_state(
            session,
            StateKind.SCENE,
            fresh=bool(fresh),
            max_age=0.0 if fresh else 0.35,
            log=lambda _m: None,
        )
        if isinstance(sc, dict):
            sid = int(sc.get("scene_id") or 0)
            if sid > 0:
                return sid
            if fresh:
                return None
    except Exception:
        pass
    try:
        from app.core.automove import read_scene_position

        sp = read_scene_position(session, log=lambda _m: None)
        if getattr(sp, "ok", False):
            return int(getattr(sp, "scene_id", 0) or 0) or None
    except Exception:
        pass
    return None


def _wait_scene_change(
    session,
    before: int | None,
    *,
    want_ids: set[int] | None = None,
    timeout_s: float = 12.0,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> dict:
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    last = before
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return {"ok": False, "error": "stopped", "after_scene": last}
        sid = _scene_id_now(session, fresh=True)
        last = sid if sid is not None else last
        if before is not None and sid is not None and int(sid) > 0 and int(sid) != int(before):
            # Reject garbage scene ids (0 / negative) — live saw 68→-1 false success.
            if want_ids and int(sid) not in want_ids:
                # Wrong map/layer is NOT success (prevent upper when want deep).
                _log(
                    log,
                    f"portal_service scene {before}→{sid} rejected "
                    f"(want={sorted(want_ids)[:8]})",
                )
                # keep waiting — do not claim ok
            else:
                return {"ok": True, "after_scene": int(sid), "before_scene": before}
        if stop_event is not None:
            if stop_event.wait(0.35):
                return {"ok": False, "error": "stopped", "after_scene": last}
        else:
            time.sleep(0.35)
    return {
        "ok": False,
        "error": "scene unchanged",
        "after_scene": last,
        "before_scene": before,
    }


def host_move_enter_scene(
    session: GameAttachSession,
    scene_id: int,
    *,
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> dict:
    """
    Call HostMoveToScenePosition(scene_id, x,y,z) via bridge (client AutoMove).

    This is the function path: pass dungeon scene id + coords, no UI.
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return {"ok": False, "error": brsn or "remote_blocked", "message": f"远程不可用: {brsn}"}
    _prefetch_portal_states(session, log=log)
    sid = int(scene_id or 0)
    if sid <= 0:
        return {"ok": False, "error": "scene_id invalid"}
    if x is None or z is None:
        xyz = DEFAULT_DUNGEON_XYZ.get(sid, (40.0, 0.0, -10.0))
        x, y, z = xyz[0], xyz[1], xyz[2]
    if y is None:
        y = 0.0
    out: dict = {
        "ok": False,
        "method": "host_move_scene",
        "scene_id": sid,
        "xyz": [float(x), float(y), float(z)],
    }
    try:
        from app.core.xajh_bridge import CMD_HOST_MOVE, ensure_bridge

        br = ensure_bridge(
            int(session.pid),
            log=log,
            inject_if_needed=False,
            hwnd=int(hwnd or 0) or None,
        )
        if br is None:
            out["error"] = "bridge not ready"
            return out
        try:
            r = br.call(
                CMD_HOST_MOVE,
                x=float(x),
                y=float(y),
                z=float(z),
                mode=int(sid),
                hwnd=int(hwnd or 0) or None,
                timeout_ms=4000,
            )
            out["command_ok"] = bool(r.ok)
            out["ret"] = r.ret
            out["note"] = r.note
            out["error"] = None if r.ok else (r.error or r.note or "HostMove failed")
            out["ok"] = bool(r.ok)
            _log(
                log,
                f"portal_service HostMove scene={sid} xyz=({x:.1f},{y:.1f},{z:.1f}) "
                f"ok={r.ok} note={r.note!r}",
            )
            return out
        finally:
            try:
                br.close()
            except Exception:
                pass
    except Exception as e:
        out["error"] = str(e)
        _log(log, f"portal_service HostMove err: {e}")
        return out


def instance_enter_scene(
    session: GameAttachSession,
    scene_id: int,
    *,
    difficulty: int = 0,
    flag: int = 1,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> dict:
    """
    Secondary: bridge INSTANCE_ENTER (pkt 0x58, 副本列表 Btn_Enter path).

    May or may not accept dungeon scene ids — tried after HostMove fails.
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return {"ok": False, "error": brsn or "remote_blocked", "message": f"远程不可用: {brsn}"}
    _prefetch_portal_states(session, log=log)
    sid = int(scene_id or 0)
    out: dict = {
        "ok": False,
        "method": "instance_enter",
        "scene_id": sid,
        "difficulty": int(difficulty),
        "flag": int(flag),
    }
    if sid <= 0:
        out["error"] = "scene_id invalid"
        return out
    try:
        from app.core.activity_auto import enter_instance_list

        r = enter_instance_list(
            session,
            inst_id=sid,
            difficulty=int(difficulty),
            flag=int(flag),
            hwnd=int(hwnd or 0),
            use_bridge=True,
            log=log,
        )
        out.update(r)
        out["method"] = "instance_enter"
        out["scene_id"] = sid
        _log(
            log,
            f"portal_service INSTANCE_ENTER id={sid} ok={r.get('ok')} "
            f"err={r.get('error')}",
        )
        return out
    except Exception as e:
        out["error"] = str(e)
        _log(log, f"portal_service INSTANCE_ENTER err: {e}")
        return out


def enter_dungeon_by_scene(
    session: GameAttachSession,
    scene_id: int,
    *,
    hwnd: int = 0,
    wait_scene_s: float = 12.0,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    before_scene: int | None = None,
    try_instance_enter: bool = False,
) -> dict:
    """
    Enter one dungeon scene_id via HostMove only by default.
    INSTANCE_ENTER is 副本列表 pkt 0x58 — NOT 地宫传送; off by default.

    Success = scene_id changes (and preferably matches target).
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return {"ok": False, "error": brsn or "remote_blocked", "message": f"远程不可用: {brsn}"}
    _prefetch_portal_states(session, log=log)
    sid = int(scene_id or 0)
    out: dict = {
        "ok": False,
        "method": "enter_dungeon_scene",
        "scene_id": sid,
    }
    if sid <= 0:
        out["error"] = "scene_id invalid"
        return out
    if before_scene is None:
        before_scene = _scene_id_now(session)
    out["before_scene"] = before_scene

    # already there
    if before_scene and int(before_scene) == sid:
        out["ok"] = True
        out["after_scene"] = sid
        out["note"] = f"already in scene {sid}"
        out["method"] = "already"
        return out

    attempts: list[dict] = []

    hm = host_move_enter_scene(session, sid, hwnd=hwnd, log=log)
    attempts.append(hm)
    if hm.get("ok") or hm.get("command_ok"):
        w = _wait_scene_change(
            session,
            before_scene,
            want_ids={sid},
            timeout_s=min(8.0, float(wait_scene_s)),
            stop_event=stop_event,
            log=log,
        )
        if w.get("ok"):
            after = int(w.get("after_scene") or 0)
            out["ok"] = True
            out["after_scene"] = after
            out["method"] = "host_move_scene"
            out["note"] = f"scene {before_scene}→{after} via HostMove({sid})"
            out["attempts"] = attempts
            log(f"portal_service enter ok {out['note']}")
            return out

    if try_instance_enter:
        ie = instance_enter_scene(session, sid, hwnd=hwnd, log=log)
        attempts.append(ie)
        if ie.get("ok"):
            w = _wait_scene_change(
                session,
                before_scene,
                want_ids={sid},
                timeout_s=float(wait_scene_s),
                stop_event=stop_event,
                log=log,
            )
            if w.get("ok"):
                after = int(w.get("after_scene") or 0)
                out["ok"] = True
                out["after_scene"] = after
                out["method"] = "instance_enter"
                out["note"] = f"scene {before_scene}→{after} via INSTANCE_ENTER({sid})"
                out["attempts"] = attempts
                log(f"portal_service enter ok {out['note']}")
                return out

    out["attempts"] = attempts
    out["error"] = (
        f"function enter scene={sid} failed "
        f"(HostMove/INSTANCE no scene change from {before_scene})"
    )
    out["after_scene"] = _scene_id_now(session)
    log(f"portal_service enter fail: {out['error']}")
    return out


def enter_dungeon_by_tier_layer(
    session: GameAttachSession,
    *,
    tier: str,
    layer: str,
    hwnd: int = 0,
    wait_scene_s: float = 12.0,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    before_scene: int | None = None,
    try_instance_enter: bool = False,
) -> dict:
    """
    Optional API: tier + layer → scene_id → HostMove.
    Disabled as primary path for daily BOSS (use 福州城 地宫传送 NPC instead).

    Example:
      enter_dungeon_by_tier_layer(session, tier="初级", layer="dungeon_deep")
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return {"ok": False, "error": brsn or "remote_blocked", "message": f"远程不可用: {brsn}"}
    _prefetch_portal_states(session, log=log)
    t = str(tier or "").strip()
    ly = _normalize_layer(layer)
    candidates = resolve_dungeon_scene_ids(t, ly)
    out: dict = {
        "ok": False,
        "method": "enter_dungeon_tier_layer",
        "tier": t,
        "layer": ly,
        "candidates": candidates[:8],
    }
    if not candidates:
        out["error"] = f"no scene map for tier={t!r} layer={ly!r}"
        return out
    if before_scene is None:
        before_scene = _scene_id_now(session)
    out["before_scene"] = before_scene
    log(
        f"portal_service enter tier={t or '-'} layer={ly} "
        f"candidates={candidates[:6]} before={before_scene}"
    )

    # Prefer first candidate matching layer band; try a few.
    tried: list[dict] = []
    for sid in candidates[:4]:
        if stop_event is not None and stop_event.is_set():
            out["error"] = "stopped"
            return out
        er = enter_dungeon_by_scene(
            session,
            int(sid),
            hwnd=hwnd,
            wait_scene_s=min(6.0, float(wait_scene_s)),
            stop_event=stop_event,
            log=log,
            before_scene=before_scene,
            try_instance_enter=try_instance_enter,
        )
        tried.append(
            {
                "scene_id": sid,
                "ok": er.get("ok"),
                "method": er.get("method"),
                "error": er.get("error"),
                "after_scene": er.get("after_scene"),
            }
        )
        if er.get("ok"):
            out.update(er)
            out["tier"] = t
            out["layer"] = ly
            out["candidates"] = candidates[:8]
            out["tried"] = tried
            return out

    out["tried"] = tried
    out["error"] = (
        f"tier={t} layer={ly} function enter failed for scenes={candidates[:4]}"
    )
    out["after_scene"] = _scene_id_now(session)
    return out


# ---- retained helpers (optional memory option path; not primary) ----

PORTAL_DLG_NAMES: tuple[str, ...] = (
    "Win_NPCContent",
    "Win_NPC",
    "Win_NPCTrans",
    "Win_NPCTemplate",
    "Win_NPCTalk",
)


def list_shown_portal_dialogs(
    session: GameAttachSession, *, log: LogFn | None = None
) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    try:
        from app.core.plg_ui import get_game_ui_dlg, is_dlg_show
    except Exception as e:
        _log(log, f"portal_service dlg import: {e}")
        return out
    for name in PORTAL_DLG_NAMES:
        try:
            dlg = int(get_game_ui_dlg(session, name, log=lambda _m: None) or 0)
        except Exception:
            dlg = 0
        if not dlg:
            continue
        try:
            shown = bool(is_dlg_show(session, dlg, log=lambda _m: None))
        except Exception:
            shown = True
        if shown:
            out.append((name, dlg & 0xFFFFFFFF))
    return out


def cur_serv_npc_id(session: GameAttachSession, *, log: LogFn | None = None) -> int:
    """plg::GetHostPlayerCurServNPC() -> int64 id (best-effort, full edx:eax)."""
    try:
        from app.core.plg_exports import EXPORT_GET_HOST_PLAYER_CUR_SERV_NPC
        from app.core.package_api import _resolve_export_va
        from app.core.remote_runtime import remote_call_cdecl_x86_ret64

        va = int(_resolve_export_va(session, EXPORT_GET_HOST_PLAYER_CUR_SERV_NPC) or 0)
        if not va:
            return 0
        pid = int(getattr(session, "pid", 0) or 0)
        if not pid:
            return 0
        # mangled YA_J = __int64; must use ret64 (32-bit eax alone was e.g. 409)
        ret = remote_call_cdecl_x86_ret64(pid, va, [])
        return int(ret or 0) & 0xFFFFFFFFFFFFFFFF
    except Exception as e:
        _log(log, f"portal_service CurServNPC err: {e}")
        return 0
