# -*- coding: utf-8 -*-
"""
自动副本 — enter 指定副本循环；活跃模式额外刷活跃点并领宝箱.

Enter (no UI click): 副本列表 packet path
  GetNetRoot(*global+0x2C)+0x1C8 thiscall 0xCC6F80(host_lo, host_hi, inst=169, mode, flag)
  host_lo/hi from GetHostSide()+0x140/+0x144 (same as Btn_Enter backend).

Flow:
  0. Start in city (福州/洛阳; configurable)
  1. Direct instance enter (list function)
  2. Wait leave city into dungeon (game 内挂 clears; we only wait)
  3. Wait return city, CD 60-120s, repeat until target points / max runs
  4. Claim flourish chests: packet 0x8F (20/35/50/70) preferred;
     UI fallback opens Win_InstanceEndlessList -> Rdo_Vibrancy -> Bnt_Bonus01..04

@author by ak
"""
from __future__ import annotations

import ctypes
import json
import math
import random
import re
import struct
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Callable

from app.core.client_build import session_supports
from app.core.automove import (
    MEM_COMMIT,
    MEM_RELEASE,
    MEM_RESERVE,
    PAGE_EXECUTE_READWRITE,
    WAIT_OBJECT_0,
    _open_process,
    _rpm,
    _wpm,
    kernel32,
    read_scene_position,
    remote_call_cdecl_x86,
    PathTarget,
    host_move_to,
)
from app.core.remote_runtime import remote_call_thiscall_x86 as _runtime_thiscall
from app.core.runner import RunnerLifecycle, interruptible_sleep
from app.core.aui_click import (
    click_client_bg,
    human_point_from_rect,
    read_aui_ctrl_rect,
)
from app.core.game_attach import GameAttachSession
from app.core.human_input import human_delay_s
from app.core.map_names import format_scene_display, resolve_scene_id
from app.core.plg_exports import (
    DEFAULT_IMAGE_BASE,
    EXPORT_GET_AUI_OBJ_IS_ENABLE,
    EXPORT_TOGGLE_DLG_SHOW,
    find_xajh_exe,
    resolve_export_rva,
)
from app.core.plg_ui import (
    get_game_ui_dlg,
    is_dlg_show,
    query_dlg_show,
)
from app.core.super_loot import open_attach_session
from app.core.xajh_bridge import (
    ensure_bridge,
)
from ctypes import wintypes

LogFn = Callable[[str], None]
StatusFn = Callable[[str], None]

# Live city scene ids (scene_map / yaolu recon).
FUZHOU_SCENE_IDS = frozenset({68})
LUOYANG_SCENE_IDS = frozenset({12, 55, 75})
# 绿竹 related inst scenes (optional positive match).
LVZHU_SCENE_IDS = frozenset({1001, 1005, 1016, 1112, 1238})
CITY_NAME_KEYS = ("福州", "洛阳")
LVZHU_NAME_KEYS = ("绿竹", "幻想乡", "活动绿竹")

# Instance.Cfg[169] = 绿竹幻想乡 (script.pck).
DEFAULT_INSTANCE_ID = 169
DEFAULT_INSTANCE_NAME = "绿竹幻想乡"
DEFAULT_INSTANCE_DIFFICULTY = 0
DEFAULT_INSTANCE_FLAG = 1
# 进阶篇 map nodes (live table: name-4 = Instance.Cfg id, name+0x50 = list grade).
# Screenshot order: 初级 / 高级 / 超级 / 福利 / 组队.
# Labels on the map are category aliases; enter still uses the real instance id.
MAP_CATEGORY_INSTANCES: tuple[tuple[int, str, int], ...] = (
    (3635, "初级副本", 80),   # 镇嵩宝塔
    (6283, "高级副本", 100),  # 嵩山之路
    (6981, "超级副本", 125),  # 嵩山之巅
    (3048, "福利副本", 45),   # 云上山崖
    (2638, "组队副本", 125),  # 梅庄外围
)
# 「一百四」地图节点别名（副本地图显示名 · Cfg 真名）
CHAPTER_140_INSTANCES: tuple[tuple[int, str, int], ...] = (
    (6230, "140材料本", 140),   # 沙漠古镇 — 切糕
    (1928, "140第二副本", 140),  # 蝎王魔窟
)
MAP_CATEGORY_BY_ID: dict[int, tuple[str, int]] = {
    iid: (alias, grade) for iid, alias, grade in MAP_CATEGORY_INSTANCES
}
CHAPTER_140_BY_ID: dict[int, tuple[str, int]] = {
    iid: (alias, grade) for iid, alias, grade in CHAPTER_140_INSTANCES
}

# 切糕 = 副本地图「一百四」篇节点「140材料本」= Instance 6230 沙漠古镇。
# 流程：进本(若已在本则跳过) → 寻路挂机点 → Alt+R 开内挂 → 等到超时回城。
QIEGAO_INSTANCE_ID = 6230
QIEGAO_INSTANCE_NAME = "沙漠古镇"
QIEGAO_CATEGORY_ALIAS = "140材料本"
QIEGAO_TASK_LABEL = "140材料本"
# 默认挂机点（场景坐标，可在面板改 / 取坐标覆盖）
QIEGAO_DEFAULT_AFK = (-58.0, 62.7, -420.1)
# 主号进本切图稳定后先走的「桥触发点」：走到桥侧触发阶段推进（空气墙消失），
# 再走正常挂机寻路。专治空气墙卡住；本点寻路不做桥区 airwall 脱困/等待。
QIEGAO_BRIDGE_PATH = (-66.0, 61.3, -441.7)
# 沙漠古镇桥面空气墙实机卡点。只有寻路在这一区域确认卡住后，
# 才判断附近双号并同步脱困；进本出生点不参与桥逻辑。
QIEGAO_BRIDGE_STAGE_SCENE_ID = 1524
QIEGAO_BRIDGE_STUCK_X = -71.5
QIEGAO_BRIDGE_STUCK_Z = -424.1
QIEGAO_BRIDGE_STUCK_RADIUS_M = 6.0
QIEGAO_BRIDGE_PAIR_NEAR_M = 18.0

# 普通副本「纯站街」纠偏规则。规则主键按自动副本配置的 cfg.instance_id 匹配，
# 不把副本 ID 当作地图 scene_id。坐标均为该副本地图的场景坐标。
# follow=True 的规则走真实组队跟随接口（开启→寻路→finally 取消跟随）。
# 每个规则用 zones 列表描述卡点区域，可多个；某卡点带 escape 时，在该卡点
# 触发会先线性回撤脱离，再继续（跟随或直走）到 target。
DUNGEON_UNSTICK_RULES: dict[int, dict] = {
    1928: {
        "name": "蝎王魔窟",
        "target": (-57.8, 67.0, -66.1),
        # 卡住后通过组队跟随脱困，并持续守护跟随状态。
        "follow": True,
        "follow_keepalive": True,
        "zones": [
            # 从进本开始：空闲(持续无伤害)时线性走到 zones[1]（第二道墙）。
            # 走到第二道墙后交给它的逻辑，不再关注本区。
            {"from_entry": True, "next_zone": 1},
            # 第二道墙：先回撤脱离再线性到目标。回撤点实机校准：(-53.9, 67.0, -10.9)。
            # no_monster：触发前校验附近没怪才执行。
            {
                "stuck": (-45.9, 67.0, -16.1),
                "escape": (-53.9, 67.0, -10.9),
                "no_monster": True,
            },
        ],
    },
    2638: {
        "name": "梅庄外围",
        "target": (33.5, 16.8, -0.5),
        "follow": True,
        "follow_keepalive": True,
        "zones": [
            {"stuck": (35.6, 16.8, -8.5)},
        ],
    },
    6230: {
        "name": "沙漠古镇",
        "target": (-43.5, 65.6, -420.9),
        "follow": True,
        "follow_keepalive": True,
        # 直接寻路会被梯子挡住：先向左后方走几步脱离桥/梯子附近，再寻路到目标点。
        # 脱离点按实机校准：卡点左后方约 (-51.6, 62.6, -403.5)。
        "zones": [
            {"stuck": (-46.1, 62.6, -400.4), "escape": (-51.6, 62.6, -403.5)},
        ],
    },
}
# 卡点半径（水平距离，米）。
DUNGEON_UNSTICK_RADIUS_M = 2.0
# 静止判定：连续静默多少秒后触发纠偏。
DUNGEON_UNSTICK_STILL_S = 6.0
# 水平移动复位阈值：位移超过该值即视为活动，重置静默窗口。
DUNGEON_UNSTICK_MOVE_EPS_M = 0.8
# 单次纠偏完成后的冷却，期间即使仍在卡点也不重复触发。
DUNGEON_UNSTICK_COOLDOWN_S = 120.0
# 脱困失败后的短冷却：阶段性地持续守护抢点，让角色重新进入纯站街后再次触发。
DUNGEON_UNSTICK_RETRY_COOLDOWN_S = 20.0
# 单次脱困寻路验证超时：内挂可能抢回移动，不能长时间阻塞等待回城循环。
DUNGEON_UNSTICK_PATHFIND_TIMEOUT_S = 15.0
# 进本默认区（from_entry）走到下一个卡点的超时：距离可能较长，放宽到 30s。
DUNGEON_UNSTICK_ENTRY_TIMEOUT_S = 30.0
# 带 no_monster 的卡点：触发脱困前检查附近是否有怪（半径，米），有怪不执行。
DUNGEON_UNSTICK_MONSTER_RADIUS_M = 8.0
# 两段式脱困时「脱离桥/梯子附近」第一段的验证超时（走几步即可，不要求严格到位）。
DUNGEON_UNSTICK_ESCAPE_TIMEOUT_S = 8.0
# 等待回城循环内的纠偏观察间隔。
DUNGEON_UNSTICK_POLL_S = 1.0

# Two qiegao runners normally live in the same helper process. Coordinate
# air-wall recovery so one character does not retreat while the other keeps
# pushing forward into it.
_QIEGAO_UNSTICK_COND = threading.Condition()
_QIEGAO_UNSTICK_ROUNDS: dict[int, dict] = {}
_QIEGAO_CITY_COND = threading.Condition()
_QIEGAO_CITY_ROUNDS: dict[int, dict] = {}
_QIEGAO_ACTIVE_RUNNERS_LOCK = threading.RLock()
_QIEGAO_ACTIVE_RUNNERS: set[int] = set()


def _qiegao_sync_unstick(
    *,
    pid: int,
    scene_id: int | None,
    stop_event: threading.Event,
    wait_s: float = 5.0,
) -> int:
    """Wait briefly for another local qiegao runner before retreating."""
    key = int(scene_id or 0)
    timeout = max(0.0, min(15.0, float(wait_s or 0.0)))
    now = time.monotonic()
    with _QIEGAO_UNSTICK_COND:
        state = _QIEGAO_UNSTICK_ROUNDS.get(key)
        if (
            state is None
            or bool(state.get("released"))
            or now - float(state.get("created", 0.0)) > timeout + 2.0
        ):
            state = {
                "created": now,
                "deadline": now + timeout,
                "pids": set(),
                "released": False,
            }
            _QIEGAO_UNSTICK_ROUNDS[key] = state
        state["pids"].add(int(pid or 0))
        if len(state["pids"]) >= 2:
            state["released"] = True
            _QIEGAO_UNSTICK_COND.notify_all()

        while not bool(state.get("released")) and not stop_event.is_set():
            left = float(state["deadline"]) - time.monotonic()
            if left <= 0:
                state["released"] = True
                _QIEGAO_UNSTICK_COND.notify_all()
                break
            _QIEGAO_UNSTICK_COND.wait(timeout=min(0.2, left))
        return len(state["pids"])


def _qiegao_city_rendezvous(
    *,
    pid: int,
    scene_id: int,
    position: tuple[float, float, float],
    stop_event: threading.Event,
    wait_s: float = 20.0,
    near_m: float = 8.0,
) -> tuple[tuple[float, float, float] | None, int, float | None]:
    """Pair local qiegao runners and return a common midpoint when too far."""
    key = int(scene_id or 0)
    timeout = max(0.0, min(30.0, float(wait_s or 0.0)))
    now = time.monotonic()
    with _QIEGAO_CITY_COND:
        state = _QIEGAO_CITY_ROUNDS.get(key)
        if (
            state is None
            or bool(state.get("released"))
            or now - float(state.get("created", 0.0)) > timeout + 2.0
        ):
            state = {
                "created": now,
                "deadline": now + timeout,
                "positions": {},
                "released": False,
                "target": None,
                "distance": None,
            }
            _QIEGAO_CITY_ROUNDS[key] = state
        state["positions"][int(pid or 0)] = tuple(float(v) for v in position[:3])
        if len(state["positions"]) >= 2:
            points = list(state["positions"].values())[:2]
            a, b = points[0], points[1]
            distance = math.hypot(a[0] - b[0], a[2] - b[2])
            state["distance"] = distance
            if distance > max(1.0, float(near_m)):
                state["target"] = (
                    (a[0] + b[0]) / 2.0,
                    (a[1] + b[1]) / 2.0,
                    (a[2] + b[2]) / 2.0,
                )
            state["released"] = True
            _QIEGAO_CITY_COND.notify_all()

        while not bool(state.get("released")) and not stop_event.is_set():
            left = float(state["deadline"]) - time.monotonic()
            if left <= 0:
                state["released"] = True
                _QIEGAO_CITY_COND.notify_all()
                break
            _QIEGAO_CITY_COND.wait(timeout=min(0.2, left))
        return (
            state.get("target"),
            len(state["positions"]),
            state.get("distance"),
        )


def _qiegao_bridge_stuck_zone_distance(
    scene_id: int | None,
    position: tuple[float, float, float] | list[float] | None,
) -> float | None:
    """Return XZ distance to the measured bridge wall, or None off-zone."""
    if (
        int(scene_id or 0) != int(QIEGAO_BRIDGE_STAGE_SCENE_ID)
        or not isinstance(position, (tuple, list))
        or len(position) < 3
    ):
        return None
    try:
        distance = math.hypot(
            float(position[0]) - float(QIEGAO_BRIDGE_STUCK_X),
            float(position[2]) - float(QIEGAO_BRIDGE_STUCK_Z),
        )
    except Exception:
        return None
    if distance > float(QIEGAO_BRIDGE_STUCK_RADIUS_M):
        return None
    return distance


def _qiegao_active_peer_nearby(
    *,
    pid: int,
    scene_id: int,
    position: tuple[float, float, float],
    near_m: float = QIEGAO_BRIDGE_PAIR_NEAR_M,
) -> tuple[bool, float | None]:
    """Find another active runner that is also at the measured bridge wall."""
    own_pid = int(pid or 0)
    own_scene = int(scene_id or 0)
    radius = max(3.0, min(50.0, float(near_m or QIEGAO_BRIDGE_PAIR_NEAR_M)))
    own_pos = tuple(float(v) for v in position[:3])
    with _QIEGAO_ACTIVE_RUNNERS_LOCK:
        peers = [p for p in _QIEGAO_ACTIVE_RUNNERS if int(p) != own_pid]
    if not peers:
        return False, None
    try:
        from app.core.live_scene_hub import get_live_scene
    except Exception:
        return False, None
    for peer_pid in peers:
        try:
            snap = get_live_scene(int(peer_pid), max_age_s=4.0)
            other = getattr(snap, "pos", None) if snap is not None else None
            if (
                snap is None
                or int(getattr(snap, "scene_id", 0) or 0) != own_scene
                or not isinstance(other, (tuple, list))
                or len(other) < 3
            ):
                continue
            if _qiegao_bridge_stuck_zone_distance(own_scene, other) is None:
                continue
            distance = math.hypot(
                float(own_pos[0]) - float(other[0]),
                float(own_pos[2]) - float(other[2]),
            )
            if distance <= radius:
                return True, distance
        except Exception:
            continue
    return False, None


def _qiegao_prefs_path() -> Path:
    """Disk prefs for qiegao main/alt AFK slots (survive restart). @author by ak"""
    try:
        base = Path(__file__).resolve().parents[2] / "runtime" / "config"
    except Exception:
        base = Path.cwd() / "runtime" / "config"
    return base / "qiegao_prefs.json"


def default_qiegao_afk_xyz() -> tuple[float, float, float]:
    return (float(QIEGAO_DEFAULT_AFK[0]), float(QIEGAO_DEFAULT_AFK[1]), float(QIEGAO_DEFAULT_AFK[2]))


def _coerce_xyz(raw) -> tuple[float, float, float] | None:
    try:
        if raw is None:
            return None
        if isinstance(raw, (list, tuple)) and len(raw) >= 3:
            return float(raw[0]), float(raw[1]), float(raw[2])
        if isinstance(raw, dict):
            return float(raw["x"]), float(raw["y"]), float(raw["z"])
    except Exception:
        return None
    return None


def load_qiegao_prefs(role_id: int | str | None = None) -> dict:
    """
    Load qiegao prefs: is_alt + main/alt AFK slots.

    Shape:
      {"is_alt": bool, "analyze_mob_freq": bool, "main": {"x","y","z"}, "alt": {"x","y","z"}}

    role_id given: per-role file roles/{role_id}/qiegao.json (new split storage),
    falling back to the legacy single qiegao_prefs.json.

    @author by ak
    """
    dx, dy, dz = default_qiegao_afk_xyz()
    out = {
        "is_alt": False,
        "analyze_mob_freq": False,
        "main": {"x": dx, "y": dy, "z": dz},
        "alt": {"x": dx, "y": dy, "z": dz},
    }
    data: dict | None = None
    path = None
    if role_id is not None and str(role_id).strip():
        try:
            from app.core.account_manager import load_role_config

            p = load_role_config(role_id, "qiegao")
            if isinstance(p, dict):
                data = p
        except Exception:
            data = None
    if data is None:
        path = _qiegao_prefs_path()
        try:
            if path.is_file():
                d = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    data = d
        except Exception:
            data = None
    if isinstance(data, dict):
        out["is_alt"] = bool(data.get("is_alt", False))
        out["analyze_mob_freq"] = bool(data.get("analyze_mob_freq", False))
        for slot in ("main", "alt"):
            xyz = _coerce_xyz(data.get(slot))
            if xyz is not None:
                out[slot] = {"x": xyz[0], "y": xyz[1], "z": xyz[2]}
    return out


def save_qiegao_prefs(
    *,
    is_alt: bool | None = None,
    analyze_mob_freq: bool | None = None,
    main: tuple[float, float, float] | None = None,
    alt: tuple[float, float, float] | None = None,
    slot: str | None = None,
    xyz: tuple[float, float, float] | None = None,
    role_id: int | str | None = None,
) -> dict:
    """
    Merge-save qiegao prefs to disk.

    Prefer slot+xyz, or main=/alt= triples; is_alt optional.
    role_id given: write roles/{role_id}/qiegao.json; else legacy single file.

    @author by ak
    """
    cur = load_qiegao_prefs(role_id)
    if is_alt is not None:
        cur["is_alt"] = bool(is_alt)
    if analyze_mob_freq is not None:
        cur["analyze_mob_freq"] = bool(analyze_mob_freq)
    if main is not None:
        cur["main"] = {"x": float(main[0]), "y": float(main[1]), "z": float(main[2])}
    if alt is not None:
        cur["alt"] = {"x": float(alt[0]), "y": float(alt[1]), "z": float(alt[2])}
    if slot in ("main", "alt") and xyz is not None:
        cur[str(slot)] = {"x": float(xyz[0]), "y": float(xyz[1]), "z": float(xyz[2])}
    if role_id is not None and str(role_id).strip():
        try:
            from app.core.account_manager import save_role_config

            save_role_config(role_id, "qiegao", cur)
            return cur
        except Exception:
            pass
    path = _qiegao_prefs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(cur, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass
    return cur


def qiegao_afk_for_role(is_alt: bool, prefs: dict | None = None) -> tuple[float, float, float]:
    """Return AFK xyz for main/alt from prefs (fallback default). @author by ak"""
    data = prefs if isinstance(prefs, dict) else load_qiegao_prefs()
    slot = "alt" if is_alt else "main"
    xyz = _coerce_xyz(data.get(slot))
    if xyz is not None:
        return xyz
    return default_qiegao_afk_xyz()


# ============================================================
# 挂机 = Alt+R「普通挂机 / 战斗操作模式」(FightMode / FightOperation)
# 不是「闭关修炼」(AutoPlay*) —— AutoPlay 一律禁止当挂机开！
# ============================================================
HANG_STATE_DLG_CANDIDATES: tuple[str, ...] = (
    "Win_FightModeMinimize",       # CDlgFightOprModeSelectMini 迷你条
    "Win_FightOperationChoose",    # 战斗操作模式选择
    "Win_SettingFightOperation",   # 战斗操作设置
    "Win_SettingOprConfig",
    "Win_FightHelper",             # 次要
)
# 闭关修炼 / 其它：shown 也绝不能当挂机开
HANG_STATE_FALSE_POSITIVE: frozenset[str] = frozenset(
    {
        "Win_AutoPlayTip",
        "Win_AutoPlayFrame",
        "Win_AutoPlayBase",
        "Win_AutoPlayResult",
        "Win_AutoPlaySkill",
        "Win_AutoPlayRecover",
        "Win_AutoPlayEquip",
        "Win_FightingCircle",
        "Win_CountryFightVS",
        "Win_CountryFight",
        "Win_AutoLock",
        "Win_TargetPlayer",
    }
)
# 挂机按钮父窗（不含闭关修炼 AutoPlay*）
HANG_BTN_PARENT_DLGS: tuple[str, ...] = (
    "Win_Main",
    "Win_QuickBar",
    "Win_MainInfo",
    "Win_MainInfoRight",
    "Win_MainInfoLeft",
    "Win_MainInfoFrame",
    "Win_MainBack",
    "Win_FightModeMinimize",
    "Win_FightOperationChoose",
    "Win_SettingFightOperation",
)
HANG_BTN_CTRL_CANDIDATES: tuple[str, ...] = (
    "Btn_FightMode",
    "Bnt_FightMode",
    "Img_FightMode",
    "Btn_FightOperation",
    "Bnt_FightOperation",
    "Img_FightOperation",
    "Btn_OprMode",
    "Img_OprMode",
    "Btn_Hang",
    "Bnt_Hang",
    "Img_Hang",
    "Chk_Hang",
    "Btn_Normal",
    "Rdo_Normal",
    "Rdo_Hang",
    "Btn_Start",
    "Btn_Stop",
    "Btn_Play",
    "Btn_Pause",
    "Img_On",
    "Img_Off",
    "Img_State",
    "Img_Mode",
)

# Curated 常用本 shown first in UI dropdown (id order not required).
# Full list comes from app/data/instance_names.json (Instance.Cfg dump).
COMMON_INSTANCE_IDS = (
    169,   # 绿竹幻想乡
    # 进阶篇 map categories first so they are selectable without hunting.
    3635,  # 初级副本 / 镇嵩宝塔
    6283,  # 高级副本 / 嵩山之路
    6981,  # 超级副本 / 嵩山之巅
    3048,  # 福利副本 / 云上山崖
    7368,  # 真·武尊堂（副本列表入口；进入后 scene=1541）
    2638,  # 组队副本 / 梅庄外围
    2576,  # 郊外酒肆
    2465,  # 镖局夜战
    1777,  # 青城突破
    1923,  # 东厂盗宝
    1928,  # 蝎王魔窟
    2626,  # 海中孤岛
    3289,  # 连云孤堡
    6230,  # 沙漠古镇
    3042,  # 百毒飞蛾洞
    3043,  # 百毒蜘蛛巢
    3044,  # 百毒蜈蚣穴
    4689,  # 绝情谷入口
    4690,  # 绝情谷深处
    4691,  # 绝情谷禁地
    3046,  # 梅庄地牢
    3049,  # 叛军前哨
    3050,  # 叛军本营
    2083,  # 青城覆灭
    7305,  # 护送飞龙令
    7306,  # 突袭贼首
    7307,  # 截击刺客
    8306,  # 力挽狂沙
    858,   # 龙岭迷窟
)

# Content panel that hosts 活跃度 chests (Bnt_Bonus01..04).
# Opening this alone defaults to 无尽逍遥 instance detail — must switch
# Rdo_Vibrancy (or claim via packet) to hit flourish awards.
DAILY_ACTIVITY_DLG = "Win_InstanceEndlessList"
DAILY_ACTIVITY_DLG_CANDIDATES = (
    "Win_InstanceEndlessList",
    "InstanceEndlessList",
)
# OnInit @ 0x9CC140:
#   Bnt_Bonus0%d (typo Bnt) @ dlg+0x2C0..+0x2CC
#   Rdo_Vibrancy @ dlg+0x30C
#   claimed flags @ dlg+0x320..+0x323 (0 = already claimed)
DAILY_AWARD_BTN_BASE = 0x2C0
DAILY_AWARD_BTN_STRIDE = 0x4
DAILY_AWARD_BTN_COUNT = 4
DAILY_AWARD_CLAIMED_BASE = 0x320
DAILY_RDO_VIBRANCY_OFF = 0x30C
# Rdo_Vibrancy command handler thiscall (dlg, arg) ret 4 — switches tab.
NOTE_VA_RDO_VIBRANCY_CMD = 0x009CA5D0
# Flourish award claim packet: cdecl 0xCCBD20(award_id) -> pkt 0x8F.
NOTE_VA_CLAIM_FLOURISH_AWARD = 0x00CCBD20
# AUIDialog::GetDlgItem(const char*) thiscall.
NOTE_VA_AUI_GET_DLG_ITEM = 0x00E918F0

# Official flourish tiers (Repu thresholds == claim packet award ids).
FLOURISH_AWARD_REPU = (20, 35, 50, 70)
# Flourish.Reputation = 46 (script.pck FameNotice / GetReputation id).
FLOURISH_REPU_ID = 46
# Game root global (preferred image base 0x400000).
NOTE_VA_GAME_ROOT_GLOBAL = 0x015282D8
# GetHostData-ish: *root+0x24 then +0x90 (same as 0x4AE420 body).
HOST_DATA_MID_OFF = 0x24
HOST_DATA_LEAF_OFF = 0x90
# CECAutoPlay 运行标志（RE 2026-07-22 / 范围半径 2026-07-23）：
#   base = *(*(0x15282D8)+0x24)   // 同 0x4AE3B0
#   autoplay = *(base + 0x220)
#   running  = byte[autoplay + 0x08]   // StartAutoPlay 写 1，StopAutoPlay 写 0
#   mode     = byte[autoplay + 0x1C]   // 0=普通模式 1=副本模式（Start/Stop 分支）
#   radius   = byte[autoplay + 0x21]   // 范围半径（UI 显示 5-50；运行时仅近 0.1 下限）
HOST_AUTOPLAY_PTR_OFF = 0x220
AUTOPLAY_RUNNING_OFF = 0x08
AUTOPLAY_MODE_OFF = 0x1C
AUTOPLAY_RADIUS_OFF = 0x21
AUTOPLAY_MODE_NORMAL = 0
AUTOPLAY_MODE_DUNGEON = 1
# UI 文案 (5-50)；运行时战斗比较无硬钳 5，内存可写 1..255
AUTOPLAY_RADIUS_UI_MIN = 5
AUTOPLAY_RADIUS_UI_MAX = 50
AUTOPLAY_RADIUS_MEM_MIN = 1
AUTOPLAY_RADIUS_MEM_MAX = 255
# 普通模式定位点（挂机锚点）：float3@autoplay+0xD0/D4/D8（RE 2026-08-09），
# 开挂时由 StartAutoPlay 从宿主坐标复制；运行中只读不写，可动态改写。
AUTOPLAY_ANCHOR_X_OFF = 0xD0
AUTOPLAY_ANCHOR_Y_OFF = 0xD4
AUTOPLAY_ANCHOR_Z_OFF = 0xD8
# 技能子对象 CECAutoPlay+0x27（size 0x5E，RE 0xC53070 / 门控 0xC55D50）：
#   +0x00 flags
#   +0x04 gate_skill_a  (ap+0x2B)  开挂门：必须 !=0
#   +0x08 gate_skill_b  (ap+0x2F)  开挂门：必须 !=0
#   +0x0C 起 9 槽，步长 8：skill_id(u32) + interval(float)  → 绝对地址 ap+0x33
AUTOPLAY_SKILL_OBJ_OFF = 0x27
AUTOPLAY_SKILL_FLAGS_OFF = 0x27  # abs on CECAutoPlay*
AUTOPLAY_SKILL_GATE_A_OFF = 0x2B
AUTOPLAY_SKILL_GATE_B_OFF = 0x2F
AUTOPLAY_SKILL_SLOT0_OFF = 0x33
AUTOPLAY_SKILL_SLOT_STRIDE = 8
AUTOPLAY_SKILL_SLOT_COUNT = 9
AUTOPLAY_SKILL_DEFAULT_INTERVAL = 1.0
# 实机已配技能时 gate_a 常见 0x8000000N；flags 常见 0x3 / 0x28（空配置 init=0xC）
AUTOPLAY_SKILL_GATE_A_STYLE = 0x80000001
AUTOPLAY_SKILL_FLAGS_FILLED = 0x3
# 恢复道具区 CECAutoPlay+0x85（size ~0x28，RE CheckHPItem / Win_AutoPlayRecover）：
#   +0x85 flags：bit i 启用槽 i（实机常见 0xF = 前 4 槽）
#   +0x89 + i*4：item template id (u32)
# 使用链：按 tid 找包 0x5267C0 → precheck 0xC56090 → UseItem 0x74BF50
# 注意：逻辑是 CheckHPItem（血/蓝药阈值），不是任意道具输出槽。
AUTOPLAY_RECOVER_FLAGS_OFF = 0x85
AUTOPLAY_RECOVER_ITEM0_OFF = 0x89
AUTOPLAY_RECOVER_ITEM_STRIDE = 4
AUTOPLAY_RECOVER_SLOT_COUNT = 4  # 实机 flags 0xF=前4槽；其后像指针/其他字段
# 自动恢复「名单」= 4 个模板 tid（不是自由文本列表）：
#   0 bit0 +0x89  HP 指定药
#   1 bit1 +0x8D  MP 指定药
#   2 bit2 +0x91  HP 替换/食物
#   3 bit3 +0x95  MP 替换/食物
AUTOPLAY_RECOVER_SLOT_ROLES = (
    "第1格·HP指定药",
    "第2格·MP指定药",
    "第3格·攻击/替换",
    "第4格·MP替换/食物",
)
# 攻击药丸（xxx伤害物品）放第 3 格（0-based index=2）
AUTOPLAY_RECOVER_ATTACK_SLOT = 2  # UI 第 3 格
AUTOPLAY_RECOVER_ATTACK_KEYWORDS = (
    "伤害物品",
    "伤害药",
    "攻击药",
    "2200W",
    "2100W",
    "药丸",
)
AUTOPLAY_RECOVER_ATTACK_EXCLUDE_KEYWORDS = ("宝石",)
# 恢复检测频率（ms 计数器，主循环 C5BB3A）：
#   HP: cnt@+0x270  iv@+0x274  call CheckHP 0xC5A080；默认 iv=1000
#   MP: cnt@+0x290  iv@+0x294  call CheckMP 0xC5A3C0；默认 iv=1000
# cnt 累加到 >= iv 才检测一次；成功后 cnt 清 0。
AUTOPLAY_RECOVER_HP_CNT_OFF = 0x270
AUTOPLAY_RECOVER_HP_IV_OFF = 0x274
AUTOPLAY_RECOVER_MP_CNT_OFF = 0x290
AUTOPLAY_RECOVER_MP_IV_OFF = 0x294
AUTOPLAY_RECOVER_IV_DEFAULT_MS = 1000
AUTOPLAY_RECOVER_IV_MIN_MS = 50
AUTOPLAY_RECOVER_IV_MAX_MS = 60000
# 阈值（百分比 0..100）：低于该值才用对应格道具
#   第1格 HP指定药  -> +0x99
#   第2格 MP指定药  -> +0x9A
#   第3格 攻击/替换 -> +0x9B
#   第4格 MP替换    -> +0x9C
# 设 100 ≈ 非满就尝试（实机常见默认 99/40/40/30）
AUTOPLAY_RECOVER_HP_PCT_A_OFF = 0x99
AUTOPLAY_RECOVER_HP_PCT_B_OFF = 0x9B
AUTOPLAY_RECOVER_MP_PCT_A_OFF = 0x9A
AUTOPLAY_RECOVER_MP_PCT_B_OFF = 0x9C
AUTOPLAY_RECOVER_PCT_MIN = 1
AUTOPLAY_RECOVER_PCT_MAX = 100
# slot(0..3) -> abs offset
AUTOPLAY_RECOVER_SLOT_PCT_OFF = (
    AUTOPLAY_RECOVER_HP_PCT_A_OFF,  # 0 第1格
    AUTOPLAY_RECOVER_MP_PCT_A_OFF,  # 1 第2格
    AUTOPLAY_RECOVER_HP_PCT_B_OFF,  # 2 第3格
    AUTOPLAY_RECOVER_MP_PCT_B_OFF,  # 3 第4格
)
NOTE_VA_AUTOPLAY_FIND_BAG_BY_TID = 0x005267C0
NOTE_VA_AUTOPLAY_CHECK_HP_ITEM = 0x00C56090
NOTE_VA_AUTOPLAY_CHECK_HP_MAIN = 0x00C5A080
NOTE_VA_AUTOPLAY_CHECK_MP_MAIN = 0x00C5A3C0
NOTE_VA_GET_HOST_BASE = 0x004AE3B0  # ret *[g+0x24]
NOTE_VA_START_AUTOPLAY = 0x00C5ABE0
NOTE_VA_STOP_AUTOPLAY = 0x00C5B0C0
# 开挂 tip 技能门校验（失败 msg 0x4EEA）
NOTE_VA_AUTOPLAY_SKILL_GATE = 0x00C55D50
# 本地 Start/Stop 无技能门；tip 才 call C55D50。包响应 DB9130 直接 call Start。
NOTE_VA_GAME_START_AUTOPLAY_PKT = 0x00CCA150  # cmd 0x15 cdecl
NOTE_VA_GAME_STOP_AUTOPLAY_PKT = 0x00CCA170   # cmd 0x16 cdecl arg reason
# 设置页范围编辑框钳制：cmp eax,5 / jge；字符串 "5"/"50"
NOTE_VA_AUTOPLAY_RADIUS_UI_CMP = 0x00AFD486  # bytes: 83 F8 05
NOTE_VA_AUTOPLAY_RADIUS_STR_MIN = 0x01290F30  # "5"
NOTE_VA_AUTOPLAY_RADIUS_STR_MAX = 0x012A4F88  # "50"
# GetReputation value path (disasm 0x69D560):
#   host_data = call 0x4AE420
#   container = *(host_data + 0x40)
#   count = *(container + 0x11C); arr = *(container + 0x118)
#   value = arr[repu_id] if repu_id < count else 0
REPU_CONTAINER_OFF = 0x40
REPU_ARRAY_OFF = 0x118
REPU_COUNT_OFF = 0x11C
NOTE_VA_GET_HOST_DATA = 0x004AE420


def _instance_data_path() -> Path:
    """Resolve app/data/instance_names.json (source or frozen). @author by ak"""
    try:
        from common.paths import APP_DATA_DIR

        if APP_DATA_DIR.is_dir():
            p = APP_DATA_DIR / "instance_names.json"
            if p.is_file():
                return p
    except Exception:
        pass
    return Path(__file__).resolve().parents[1] / "data" / "instance_names.json"


@dataclass(frozen=True)
class InstanceInfo:
    """One Instance.Cfg row for UI dropdown. @author by ak"""

    id: int
    name: str
    need_level: int | None = None
    type: int | None = None


@lru_cache(maxsize=1)
def load_instance_catalog() -> dict[int, InstanceInfo]:
    """
    Load Instance.Cfg id -> InstanceInfo from app/data/instance_names.json.

    Supports list of {id,name,need_level?,type?} or legacy {id: name} map.

    @author by ak
    """
    path = _instance_data_path()
    if not path.is_file():
        return {
            DEFAULT_INSTANCE_ID: InstanceInfo(
                DEFAULT_INSTANCE_ID, DEFAULT_INSTANCE_NAME, 30
            )
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            DEFAULT_INSTANCE_ID: InstanceInfo(
                DEFAULT_INSTANCE_ID, DEFAULT_INSTANCE_NAME, 30
            )
        }
    out: dict[int, InstanceInfo] = {}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                iid = int(item.get("id") or 0)
                name = str(item.get("name") or "").strip()
            except Exception:
                continue
            if iid <= 0 or not name:
                continue
            need_level = None
            typ = None
            try:
                if item.get("need_level") is not None:
                    need_level = int(item.get("need_level"))
            except Exception:
                need_level = None
            try:
                if item.get("type") is not None:
                    typ = int(item.get("type"))
            except Exception:
                typ = None
            if need_level is not None and not (1 <= need_level <= 150):
                need_level = None
            out[iid] = InstanceInfo(iid, name, need_level, typ)
    elif isinstance(raw, dict):
        for k, v in raw.items():
            try:
                iid = int(k)
                name = str(v or "").strip()
            except Exception:
                continue
            if iid > 0 and name:
                out[iid] = InstanceInfo(iid, name)
    if DEFAULT_INSTANCE_ID not in out:
        out[DEFAULT_INSTANCE_ID] = InstanceInfo(
            DEFAULT_INSTANCE_ID, DEFAULT_INSTANCE_NAME, 30
        )
    return out


@lru_cache(maxsize=1)
def load_instance_names() -> dict[int, str]:
    """
    Load Instance.Cfg id -> name from app/data/instance_names.json.

    @author by ak
    """
    return {iid: info.name for iid, info in load_instance_catalog().items()}


@lru_cache(maxsize=1)
def load_instance_levels() -> dict[int, int]:
    """id -> need/recommend level (from Desc/Hint). @author by ak"""
    out: dict[int, int] = {}
    for iid, info in load_instance_catalog().items():
        if info.need_level is not None:
            out[iid] = int(info.need_level)
    return out


def instance_label(
    inst_id: int,
    name: str | None = None,
    *,
    need_level: int | None = None,
) -> str:
    """
    Display label: 绿竹幻想乡 Lv30 (169).

    进阶篇 map nodes use category alias first:
      初级副本 · 镇嵩宝塔 Lv80 (3635)

    need_level from catalog when omitted; map-category grade wins for those ids.

    @author by ak
    """
    iid = int(inst_id)
    cat = load_instance_catalog()
    info = cat.get(iid)
    nm = (name or "").strip()
    if not nm:
        nm = (info.name if info else None) or f"副本{iid}"
    # Strip decorative brackets from Cfg names for cleaner UI.
    nm = nm.strip("【】[]")
    cat_alias = MAP_CATEGORY_BY_ID.get(iid) or CHAPTER_140_BY_ID.get(iid)
    if cat_alias is not None:
        alias, map_grade = cat_alias
        # Avoid "初级副本 · 初级副本" if caller already passed the alias.
        if nm and nm != alias and not nm.startswith(f"{alias} "):
            nm = f"{alias} · {nm}"
        else:
            nm = alias
        if need_level is None:
            need_level = int(map_grade)
    lv = need_level
    if lv is None and info is not None:
        lv = info.need_level
    if lv is not None:
        try:
            lv_i = int(lv)
        except Exception:
            lv_i = 0
        if 1 <= lv_i <= 150:
            return f"{nm} Lv{lv_i} ({iid})"
    return f"{nm} ({iid})"


def parse_instance_label(text: str) -> int | None:
    """
    Parse id from dropdown label or raw number.

    Accepts: "绿竹幻想乡 Lv30 (169)", "绿竹幻想乡 (169)", "169", "id=169".

    @author by ak
    """
    s = (text or "").strip()
    if not s:
        return None
    m = re.search(r"\((\d+)\)\s*$", s)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"\d+", s)
    if m:
        return int(s)
    m = re.search(r"(?:id|inst|本)\s*[=:：]?\s*(\d+)", s, re.I)
    if m:
        return int(m.group(1))
    return None


def _priority_instance_ids(cat: dict[int, InstanceInfo]) -> list[int]:
    """
    Front-of-list order matching 进阶篇 / 常用本.

    1. 绿竹幻想乡 (default 活跃本)
    2. 进阶篇 map categories (初级/高级/超级/福利/组队)
    3. curated COMMON_INSTANCE_IDS (progressive list)
    4. remaining type=6 energy-limit 本 by need_level then id

    @author by ak
    """
    out: list[int] = []
    seen: set[int] = set()

    def _add(iid: int) -> None:
        if iid in seen:
            return
        if iid not in cat and iid != DEFAULT_INSTANCE_ID:
            return
        out.append(iid)
        seen.add(iid)

    _add(DEFAULT_INSTANCE_ID)
    # 真·武尊堂是降龙编排入口，置于组队副本分类之前。
    _add(7368)
    for iid, _alias, _grade in MAP_CATEGORY_INSTANCES:
        _add(iid)
    for iid, _alias, _grade in CHAPTER_140_INSTANCES:
        _add(iid)
    for iid in COMMON_INSTANCE_IDS:
        _add(iid)
    # Extra type=6 rows not already curated (energy-limit progressive).
    typed = [
        info
        for info in cat.values()
        if info.type == 6
    ]
    typed.sort(
        key=lambda info: (
            info.need_level if info.need_level is not None else 999,
            info.id,
        )
    )
    for info in typed:
        _add(info.id)
    return out


def list_instance_choices(
    *,
    common_first: bool = True,
    include_all: bool = True,
) -> list[tuple[int, str]]:
    """
    Dropdown choices: (id, label).

    Front: 绿竹 + type=6 progressive list (by level) + curated 常用本;
    then remaining by id. Labels: 名称 LvN (id) when level known.

    @author by ak
    """
    cat = load_instance_catalog()
    seen: set[int] = set()
    out: list[tuple[int, str]] = []
    if common_first:
        for iid in _priority_instance_ids(cat):
            if iid in seen:
                continue
            info = cat.get(iid)
            if info is None and iid != DEFAULT_INSTANCE_ID:
                continue
            if info is None:
                out.append(
                    (
                        iid,
                        instance_label(
                            iid, DEFAULT_INSTANCE_NAME, need_level=30
                        ),
                    )
                )
            else:
                out.append((iid, instance_label(iid)))
            seen.add(iid)
    if include_all:
        for iid in sorted(cat):
            if iid in seen:
                continue
            out.append((iid, instance_label(iid)))
            seen.add(iid)
    if not out:
        out.append(
            (
                DEFAULT_INSTANCE_ID,
                instance_label(
                    DEFAULT_INSTANCE_ID,
                    DEFAULT_INSTANCE_NAME,
                    need_level=30,
                ),
            )
        )
    return out


def default_instance_label() -> str:
    """Default Combobox value (绿竹). @author by ak"""
    return instance_label(DEFAULT_INSTANCE_ID)


@dataclass
class ActivityConfig:
    """自动副本 / 自动活跃 parameters. @author by ak"""

    # activity | dungeon | qiegao
    # activity: slow loop + claim flourish chests
    # dungeon: farm only, no claim
    # qiegao: 140任务一进本 → 阶段后挂机点 → 等到超时回城
    mode: str = "activity"
    # Prefer city gate: fuzhou | luoyang | any_city
    city_gate: str = "any_city"
    instance_id: int = DEFAULT_INSTANCE_ID
    instance_difficulty: int = DEFAULT_INSTANCE_DIFFICULTY
    instance_flag: int = DEFAULT_INSTANCE_FLAG
    enter_timeout_s: float = 45.0
    # Wait dungeon clear + return to city (内挂 fights).
    wait_return_city_s: float = 900.0
    # Poll interval while inside dungeon waiting to return (lower freq).
    return_poll_s: float = 15.0
    # After return: configurable CD before next enter.  UI sets min=max for a
    # deterministic user-entered value; separate bounds remain API-compatible.
    entry_cd_min_s: float = 30.0
    entry_cd_max_s: float = 30.0
    loop_idle_s: float = 1.5
    use_bridge: bool = True
    # Points: prefer live GetReputation(46); points_per_run only for display fallback.
    points_per_run: int = 10
    target_points: int = 70
    # Fallback seed if live read fails (0 = unknown).
    initial_points: int = 0
    max_runs: int = 12
    # Hard cap on total attempts (rounds incl. failed enters); 0 = unlimited.
    max_attempts: int = 0
    # Claim Win_InstanceEndlessList Bnt_Bonus01..04 (enabled tiers only).
    claim_awards: bool = True
    # After each return: try claim so 20/35/50/70 chests open as unlocked.
    claim_after_each_run: bool = True
    # After return-to-city: wait for scene/UI settle before claim (anti d3dx crash).
    claim_settle_s: float = 6.0
    award_btn_count: int = DAILY_AWARD_BTN_COUNT
    allow_cursor_click: bool = False
    # 切糕：进本/过图后再寻路前的额外等待（秒）；map_ready 完成后再计。
    qiegao_stage_wait_s: float = 0.0
    # 过图等待：进本后至少等 min 秒，直到坐标可读且稳定；超时仍继续（会再报寻路错）。
    qiegao_map_load_min_s: float = 8.0
    qiegao_map_load_timeout_s: float = 60.0
    qiegao_map_stable_s: float = 1.5
    # 切糕固定挂机点（场景坐标）；未设置则用 QIEGAO_DEFAULT_AFK。
    qiegao_afk_x: float | None = None
    qiegao_afk_y: float | None = None
    qiegao_afk_z: float | None = None
    # 切糕小号/队员：不进本，城内一直等主号带入；识别到本内后直接挂机。
    qiegao_is_alt: bool = False
    # 开发向：进本后低频分析「大漠射手」减少对普怪刷新的影响（默认关）。
    qiegao_analyze_mob_freq: bool = False
    # 挂机中远程扫图很重：默认至少 45s；与 pathfind 互斥，硬失败后自动停。
    qiegao_mob_freq_interval_s: float = 45.0
    qiegao_mob_freq_radius: float = 200.0
    # 远程调用失败后冷却（秒），期间不再 pathfind 补发 / 扫怪。
    qiegao_remote_cool_s: float = 120.0
    # 挂机期间是否周期性补发寻路（防被撞飞）。
    qiegao_afk_repath_s: float = 90.0
    # 挂机补发门控：距挂机点 ≤ 此值(m) 不补发（默认 2.0m）。
    qiegao_afk_repath_min_dist: float = 2.0
    # 到达挂机点后开启内挂（默认 True）。开/关策略完全吃 hang_settings（含无技能）。
    qiegao_press_hang_hotkey: bool = True
    # 切糕寻路：严格到位半径（默认 1.0m，必须走到点再开挂机）。
    qiegao_arrive_radius: float = 1.0
    # 空气墙未到位：多等一会再重新寻路（秒，默认 10s）。
    qiegao_airwall_retry_s: float = 10.0
    # 兼容旧注释占位（补发间隔/卡住在 pathfind 侧仍可用）
    # qiegao 到位总超时 / 补发间隔 / 卡住判定时间：
    qiegao_path_timeout_s: float = 180.0
    qiegao_path_repath_s: float = 8.0
    # 首次寻路卡在原地（空气墙/阶段门）多久触发脱困；阶段动画~5s，默认 10s+
    qiegao_path_stuck_s: float = 10.0
    # 脱困兜底：从墙面退回桥中部；4m 实机仍可能落在碰撞边缘。
    qiegao_unstick_step_m: float = 6.0
    # 脱困走动后的短等（秒，只等走出动作）
    qiegao_unstick_settle_s: float = 1.2
    # 桥前双号空气墙脱困：先停寻路，等待另一实例也触发卡住后一起回撤。
    qiegao_unstick_sync_wait_s: float = 12.0
    # 回撤后禁止寻路时长（秒）：让两号站街等待阶段推进，避免立刻再挤墙。
    qiegao_unstick_hold_s: float = 25.0
    # 回城后必须先等切图稳定，再调用关挂/停移动，避免加载窗口内远程调用。
    qiegao_return_settle_min_s: float = 5.0
    qiegao_return_stable_s: float = 2.0
    qiegao_return_settle_timeout_s: float = 20.0
    # 回城稳定并关挂后：开启组队跟随，两号距离 ≤ 该值即视为已聚拢。
    qiegao_return_near_m: float = 8.0
    # 组队跟随聚拢等待上限（秒）。
    qiegao_return_peer_wait_s: float = 30.0
    # 挂机热键切换后的沉降等待（关/开之间）。
    qiegao_hang_settle_s: float = 0.9
    # 副本倒计时剩余 ≤ 该值(秒)时关闭内挂，等待系统送出副本；0 = 不提前关。
    qiegao_stop_hang_before_end_s: float = 30.0
    # 寻路已到位后再确认一段时间，防止移动收尾时过早开挂机。
    qiegao_arrival_settle_s: float = 0.8
    # 主号进本切图稳定后先走「桥触发阶段」：寻路到桥触发点触发阶段推进
    # （空气墙消失），期间不做桥区 airwall 脱困/等待逻辑，再开始正常挂机寻路。
    qiegao_bridge_phase_enabled: bool = True
    # 桥触发点（场景坐标）；未设置则用 QIEGAO_BRIDGE_PATH。
    qiegao_bridge_path_x: float | None = None
    qiegao_bridge_path_y: float | None = None
    qiegao_bridge_path_z: float | None = None
    # 桥触发寻路验证超时（实机约 6s）。
    qiegao_bridge_path_timeout_s: float = 12.0
    # 桥触发路径宽松到位半径（未严格到位也接受，停留等阶段推进即可）。
    qiegao_bridge_path_radius: float = 4.0
    # 到位后在桥触发点的停留（触发阶段动画，约 7s）。
    qiegao_bridge_stay_s: float = 7.0
    # 整个桥阶段预留窗口（进图稳定后开始计，到正常挂机寻路 ≥ 该时长）。
    qiegao_bridge_wait_s: float = 15.0
    # 小号进本过图稳定后先站桩等待（秒）主号完成桥触发阶段，再挂机寻路；
    # 0 = 跟随主号 qiegao_bridge_wait_s（默认 15s）。避免两号同时抢过空气墙。
    qiegao_alt_hold_s: float = 0.0

    def entry_cd_sleep_s(self) -> float:
        """
        Randomized post-clear CD.

        @author by ak
        """
        lo = max(0.0, float(self.entry_cd_min_s))
        hi = max(lo, float(self.entry_cd_max_s))
        return random.uniform(lo, hi)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ActivityStepEvent:
    """UI / log event from ActivityRunner. @author by ak"""

    phase: str
    message: str
    ok: bool = True
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _sleep_interruptible(
    seconds: float,
    stop_event: threading.Event | None,
    *,
    step: float = 0.2,
) -> bool:
    """
    Sleep up to seconds; return False if stopped.

    @author by ak
    """
    return interruptible_sleep(seconds, stop_event, interval=step)


def _u32(raw: bytes) -> int:
    return struct.unpack("<I", raw[:4])[0]


def _i32(raw: bytes) -> int:
    return struct.unpack("<i", raw[:4])[0]


def read_flourish_points(
    session,
    *,
    repu_id: int = FLOURISH_REPU_ID,
    log: LogFn | None = None,
) -> int | None:
    """
    Read live 活跃度 (Flourish reputation value).

    Disasm GetReputation @ 0x69D560:
      host_data = GetHostData@0x4AE420  (*0x15282D8 -> +0x24 -> +0x90)
      container = *(host_data + 0x40)
      if repu_id < *(container + 0x11C):
          return *(*(container + 0x118) + repu_id * 4)
      return 0

    Script: Flourish.Reputation = 46.
    @author by ak
    """
    log = log or (lambda _m: None)
    rid = int(repu_id) & 0xFF  # engine uses movzx byte
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        log("flourish read: no module_base")
        return None
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        log("flourish read: no pid")
        return None
    if not session_supports(session, "activity.read"):
        log("flourish read blocked: unknown client build")
        return None

    def note_live(note_va: int) -> int:
        return base + (int(note_va) - DEFAULT_IMAGE_BASE)

    h = 0
    try:
        h = _open_process(pid)
        host_data = 0
        # Prefer remote call 0x4AE420 (same as game).
        try:
            va = note_live(NOTE_VA_GET_HOST_DATA)
            host_data = int(remote_call_cdecl_x86(pid, va, [])) & 0xFFFFFFFF
        except Exception as e:
            log(f"flourish GetHostData call err: {e}")
            host_data = 0
        if not host_data:
            # Memory chain fallback.
            g_live = note_live(NOTE_VA_GAME_ROOT_GLOBAL)
            root = _u32(_rpm(h, g_live, 4))
            if not root:
                log("flourish read: game root null")
                return None
            mid = _u32(_rpm(h, root + HOST_DATA_MID_OFF, 4))
            if not mid:
                log("flourish read: mid null")
                return None
            host_data = _u32(_rpm(h, mid + HOST_DATA_LEAF_OFF, 4))
        if not host_data:
            log("flourish read: host_data null")
            return None
        container = _u32(_rpm(h, host_data + REPU_CONTAINER_OFF, 4))
        if not container:
            log("flourish read: repu container null")
            return None
        count = _u32(_rpm(h, container + REPU_COUNT_OFF, 4))
        if rid >= count:
            log(f"flourish read: id={rid} >= count={count} -> 0")
            return 0
        arr = _u32(_rpm(h, container + REPU_ARRAY_OFF, 4))
        if not arr:
            log("flourish read: repu array null")
            return None
        val = _i32(_rpm(h, arr + rid * 4, 4))
        log(f"flourish read id={rid} value={val} count={count}")
        return int(val)
    except Exception as e:
        log(f"flourish read err: {e}")
        return None
    finally:
        if h:
            try:
                kernel32.CloseHandle(wintypes.HANDLE(h))
            except Exception:
                pass


def _resolve_export_va(session, export_name: str) -> int:
    """Resolve plg export to live VA. @author by ak"""
    base = getattr(session, "module_base", None)
    if not base:
        raise RuntimeError("session has no module_base; attach first")
    pe = find_xajh_exe(getattr(session, "exe_path", None))
    if pe is None:
        raise RuntimeError("cannot locate xajh.exe for export parse")
    rva = resolve_export_rva(pe, export_name)
    if rva is None:
        raise RuntimeError(f"export not found: {export_name}")
    return int(base) + int(rva)


def remote_call_thiscall_x86(
    pid: int,
    func_va: int,
    this_ptr: int,
    args: list[int] | None = None,
    *,
    stack_cleanup: int | None = None,
    caller_cleanup: bool | None = None,
    timeout_ms: int = 5000,
) -> int:
    """
    Call MSVC thiscall via remote_runtime.

    兼容两种参数：
      - caller_cleanup=False  → 被调方 ret N（标准 thiscall）
      - stack_cleanup=None    → 旧语义：caller 清栈（cdecl 风格）
      - stack_cleanup=N>0     → 被调方清栈

    优先 caller_cleanup；未传时按 stack_cleanup 推导。
    @author by ak
    """
    if caller_cleanup is None:
        # 旧 API：stack_cleanup is None → caller cleans stack
        caller_cleanup = stack_cleanup is None
    return int(
        _runtime_thiscall(
            int(pid),
            int(func_va),
            int(this_ptr),
            list(args or []),
            caller_cleanup=bool(caller_cleanup),
            timeout_ms=int(timeout_ms),
        )
    )


def ctypes_get_last() -> int:
    """Last Win32 error. @author by ak"""
    return int(ctypes.get_last_error())


def read_scene_state(
    session,
    *,
    fresh: bool = False,
    log: LogFn | None = None,
) -> tuple[int | None, tuple[float, float, float] | None, str]:
    """
    Return (scene_id, pos, display_name).

    fresh=False: 非马上需要，优先 state/hub 短缓存。
    fresh=True: 跨图/结算后必须时效。

    @author by ak
    """
    log = log or (lambda _m: None)
    if not fresh:
        try:
            from app.core.state_dispatch import StateKind, get_state

            sc = get_state(session, StateKind.SCENE, fresh=False, log=lambda _m: None)
            if isinstance(sc, dict) and (
                sc.get("scene_id") is not None or sc.get("pos") is not None
            ):
                sid = sc.get("scene_id")
                try:
                    sid_i = int(sid) if sid is not None else None
                except Exception:
                    sid_i = None
                pos = sc.get("pos")
                pos_t = None
                if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                    pos_t = (float(pos[0]), float(pos[1]), float(pos[2]))
                label = str(sc.get("scene_label") or "").strip()
                if not label and sid_i is not None:
                    label = format_scene_display(sid_i) or "-"
                return sid_i, pos_t, label or "-"
        except Exception:
            pass
    sp = read_scene_position(session, log=log)
    if not sp.ok:
        return None, None, "-"
    sid = int(sp.scene_id) if sp.scene_id is not None else None
    pos = sp.scene_pos
    label = format_scene_display(sid) if sid is not None else "-"
    if fresh:
        try:
            from app.core.state_dispatch import StateKind, invalidate_states
            from app.core.live_scene_hub import publish_live_scene

            invalidate_states(session, StateKind.SCENE, StateKind.POS)
            pos_t = None
            if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                pos_t = (float(pos[0]), float(pos[1]), float(pos[2]))
            publish_live_scene(
                int(getattr(session, "pid", 0) or 0),
                scene_id=sid,
                scene_label=label if label != "-" else None,
                pos=pos_t,
                source="activity_scene",
            )
        except Exception:
            pass
    return sid, pos, label


def is_fuzhou_scene(scene_id: int | None, name: str | None = None) -> bool:
    """True if Fuzhou city. @author by ak"""
    if scene_id is not None:
        # Concrete id wins. Never let a stale hub label like "福州城" poison a
        # non-city scene_id after map change.
        return int(scene_id) in FUZHOU_SCENE_IDS
    return "福州" in (name or "")


def is_luoyang_scene(scene_id: int | None, name: str | None = None) -> bool:
    """True if Luoyang city. @author by ak"""
    if scene_id is not None:
        return int(scene_id) in LUOYANG_SCENE_IDS
    return "洛阳" in (name or "")


def is_city_scene(
    scene_id: int | None,
    name: str | None,
    gate: str,
) -> bool:
    """
    City gate check for activity loop.

    @author by ak
    """
    g = (gate or "any_city").strip().lower()
    if g in ("fuzhou", "fz", "福州"):
        return is_fuzhou_scene(scene_id, name)
    if g in ("luoyang", "ly", "洛阳"):
        return is_luoyang_scene(scene_id, name)
    return is_fuzhou_scene(scene_id, name) or is_luoyang_scene(scene_id, name)


def is_lvzhu_scene(scene_id: int | None, name: str | None = None) -> bool:
    """True if 绿竹 / fantasy instance map. @author by ak"""
    if scene_id is not None and int(scene_id) in LVZHU_SCENE_IDS:
        return True
    n = name or ""
    if scene_id is not None:
        mid, cn = resolve_scene_id(scene_id)
        n = f"{cn or ''} {mid or ''} {n}"
    return any(k in n for k in LVZHU_NAME_KEYS)


def is_dungeon_scene(
    scene_id: int | None,
    name: str | None,
    *,
    gate: str,
) -> bool:
    """
    True when left city (into instance / 绿竹).

    @author by ak
    """
    if is_city_scene(scene_id, name, gate):
        return False
    if is_lvzhu_scene(scene_id, name):
        return True
    # Any non-city is treated as in-instance (内挂 maps vary).
    if scene_id is None:
        return False
    return True


def enter_instance_list(
    session,
    *,
    inst_id: int,
    difficulty: int = 0,
    flag: int = 1,
    hwnd: int = 0,
    use_bridge: bool = True,
    log: LogFn | None = None,
) -> dict:
    """
    Direct 副本列表 enter (same backend as Btn_Enter, no UI click).

    Bridge CMD_INSTANCE_ENTER -> thiscall 0xCC6F80 packet 0x58.
    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "inst_id": int(inst_id),
        "difficulty": int(difficulty),
        "flag": int(flag),
        "error": None,
    }
    if int(inst_id) <= 0:
        out["error"] = "inst_id invalid"
        return out
    if not use_bridge or not session.pid:
        out["error"] = "需要已注入桥接"
        return out
    try:
        br = ensure_bridge(
            int(session.pid),
            log=log,
            inject_if_needed=False,
            hwnd=int(hwnd) if hwnd else None,
        )
    except Exception as e:
        out["error"] = f"bridge open: {e}"
        return out
    if br is None:
        out["error"] = "桥接未就绪"
        return out
    try:
        r = br.instance_enter(
            int(inst_id),
            difficulty=int(difficulty),
            flag=int(flag),
            hwnd=hwnd or None,
            timeout_ms=4000,
        )
        out["ok"] = bool(r.ok)
        out["ret"] = r.ret
        out["note"] = r.note
        out["error"] = None if r.ok else (r.error or r.note)
        log(
            f"activity INSTANCE_ENTER id={inst_id} diff={difficulty} "
            f"flag={flag} ok={r.ok} note={r.note!r} err={r.error}"
        )
        return out
    except Exception as e:
        log(f"activity INSTANCE_ENTER err: {e}")
        out["error"] = str(e)
        return out
    finally:
        try:
            br.close()
        except Exception:
            pass


def toggle_dlg_show(session, dlg_ptr: int, *, log: LogFn | None = None) -> bool:
    """
    Call plg::ToggleDlgShow(AUIDialog*).

    @author by ak
    """
    log = log or (lambda _m: None)
    ptr = int(dlg_ptr) & 0xFFFFFFFF
    if not ptr:
        return False
    try:
        va = _resolve_export_va(session, EXPORT_TOGGLE_DLG_SHOW)
        remote_call_cdecl_x86(int(session.pid), va, [ptr])
        return True
    except Exception as e:
        log(f"ToggleDlgShow err: {e}")
        return False


def ensure_dlg_shown(
    session,
    name: str,
    *,
    log: LogFn | None = None,
) -> int:
    """
    Ensure dialog is shown; return dlg ptr or 0.

    @author by ak
    """
    log = log or (lambda _m: None)
    r = query_dlg_show(session, name, log=log)
    if r.ok and r.shown and r.dlg_ptr:
        return int(r.dlg_ptr)
    dlg = get_game_ui_dlg(session, name, log=log)
    if not dlg:
        log(f"ensure_dlg_shown: no dlg {name!r}")
        return 0
    if not is_dlg_show(session, dlg, log=log):
        toggle_dlg_show(session, dlg, log=log)
        time.sleep(0.35)
    if is_dlg_show(session, dlg, log=log):
        return int(dlg) & 0xFFFFFFFF
    return 0


def aui_get_dlg_item(
    session,
    dlg_ptr: int,
    ctrl_name: str,
    *,
    log: LogFn | None = None,
) -> int:
    """
    AUIDialog::GetDlgItem(name) thiscall -> control*.

    @author by ak
    """
    log = log or (lambda _m: None)
    dlg = int(dlg_ptr) & 0xFFFFFFFF
    name = (ctrl_name or "").strip()
    if not dlg or not name:
        return 0
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        return 0
    if not session_supports(session, "activity.ui"):
        log("GetDlgItem blocked: unknown client build")
        return 0
    va = base + (NOTE_VA_AUI_GET_DLG_ITEM - DEFAULT_IMAGE_BASE)
    pid = int(session.pid)
    h = 0
    remote = 0
    try:
        payload = name.encode("ascii", "ignore") + b"\x00"
        from app.core.plg_ui import remote_alloc_bytes, remote_free

        h, remote, _sz = remote_alloc_bytes(pid, payload)
        # thiscall GetDlgItem(this, const char*) — callee ret 4
        # MSVC thiscall: callee cleans stack (ret 4) → caller_cleanup=False.
        ret = remote_call_thiscall_x86(
            pid,
            va,
            dlg,
            [int(remote) & 0xFFFFFFFF],
            caller_cleanup=False,
            timeout_ms=3000,
        )
        return int(ret) & 0xFFFFFFFF
    except Exception as e:
        log(f"GetDlgItem({name!r}) err: {e}")
        return 0
    finally:
        if h and remote:
            try:
                from app.core.plg_ui import remote_free

                remote_free(h, remote)
            except Exception:
                pass


def aui_obj_is_enable(session, ctrl_ptr: int, *, log: LogFn | None = None) -> bool:
    """
    plg::GetAUIObjIsEnable(ctrl) -> bool.

    @author by ak
    """
    log = log or (lambda _m: None)
    ptr = int(ctrl_ptr) & 0xFFFFFFFF
    if not ptr:
        return False
    try:
        va = _resolve_export_va(session, EXPORT_GET_AUI_OBJ_IS_ENABLE)
        ret = remote_call_cdecl_x86(int(session.pid), va, [ptr])
        return bool(int(ret) & 0xFF)
    except Exception as e:
        log(f"GetAUIObjIsEnable err: {e}")
        return False



@dataclass
class HangStateResult:
    """Game hang/内挂 state probe. @author by ak"""

    ok: bool
    on: bool | None = None  # True=开 False=关 None=未知
    source: str = ""
    signals: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# AUIDialog::IsShow live flag (aui_click.py): byte[this+0x94]
AUI_OBJ_ISSHOW_OFF = 0x94


def _read_remote_bytes(session, addr: int, size: int) -> bytes | None:
    """Read process memory via pymem or remote_read_bytes. @author by ak"""
    addr = int(addr) & 0xFFFFFFFF
    size = int(size)
    if not addr or size <= 0:
        return None
    pm = getattr(session, "pm", None)
    if pm is not None:
        try:
            import pymem.memory

            return bytes(pymem.memory.read_bytes(pm.process_handle, addr, size))
        except Exception:
            pass
    try:
        from app.core.automove import remote_read_bytes

        pid = int(getattr(session, "pid", 0) or 0)
        if not pid:
            return None
        return bytes(remote_read_bytes(pid, addr, size))
    except Exception:
        return None


def _aui_obj_is_show(session, ctrl_ptr: int, *, log: LogFn | None = None) -> bool | None:
    """
    plg::GetAUIObjIsShow(ctrl) -> bool | None on failure.

    @author by ak
    """
    log = log or (lambda _m: None)
    ptr = int(ctrl_ptr) & 0xFFFFFFFF
    if not ptr:
        return None
    try:
        from app.core.plg_exports import EXPORT_GET_AUI_OBJ_IS_SHOW

        va = _resolve_export_va(session, EXPORT_GET_AUI_OBJ_IS_SHOW)
        ret = remote_call_cdecl_x86(int(session.pid), va, [ptr])
        return bool(int(ret) & 0xFF)
    except Exception as e:
        log(f"GetAUIObjIsShow err: {e}")
        return None


def _aui_raw_isshow_byte(session, obj_ptr: int) -> int | None:
    """
    Direct read AUI object IsShow flag at +0x94 (not IsDlgShow export).

    早期能「偶尔读到」的挂机条，多半是这个内存位；export 路径在部分窗上恒 False。

    @author by ak
    """
    ptr = int(obj_ptr or 0) & 0xFFFFFFFF
    if not ptr:
        return None
    raw = _read_remote_bytes(session, ptr + AUI_OBJ_ISSHOW_OFF, 1)
    if not raw:
        return None
    return int(raw[0])


def _read_aui_ctrl_blob(session, ctrl_ptr: int, size: int = 0xA0) -> bytes | None:
    """Raw control header for hang recon. @author by ak"""
    return _read_remote_bytes(session, int(ctrl_ptr or 0) & 0xFFFFFFFF, size)


def _read_aui_push_checked(session, ctrl_ptr: int) -> bool | None:
    """
    Best-effort AUI button checked/pushed read.

    @author by ak
    """
    ptr = int(ctrl_ptr) & 0xFFFFFFFF
    if not ptr:
        return None
    raw = _read_aui_ctrl_blob(session, ptr, 0xA0)
    if not raw:
        return None
    try:
        # prefer explicit IsShow-ish / state slots commonly 0/1
        for off in (0x94, 0x60, 0x64, 0x58, 0x5C, 0x68, 0x6C, 0x70, 0x50, 0x54):
            if off + 1 > len(raw):
                continue
            b = raw[off]
            if b == 1:
                # require nearby zeros to avoid always-true padding
                nearby = raw[max(0, off - 8) : off + 8]
                if 0 in nearby:
                    return True
        # dword view
        ones = []
        for off in (0x54, 0x58, 0x5C, 0x60, 0x64, 0x68, 0x6C, 0x70, 0x90, 0x94):
            if off + 4 > len(raw):
                continue
            v = struct.unpack_from("<I", raw, off)[0]
            if v == 1:
                ones.append(off)
        if ones:
            for off in (0x94, 0x60, 0x64, 0x58, 0x5C):
                if off in ones:
                    return True
        return False if raw[AUI_OBJ_ISSHOW_OFF : AUI_OBJ_ISSHOW_OFF + 1] == b"\x00" else None
    except Exception:
        return None


def _rect_from_aui_blob(blob: bytes) -> dict | None:
    """
    Parse client rect from AUI object header (x/y/w/h @ 0x9C/0xA0/0xA4/0xA8).

    @author by ak
    """
    if not blob or len(blob) < 0xAC:
        return None
    try:
        x = struct.unpack_from("<i", blob, 0x9C)[0]
        y = struct.unpack_from("<i", blob, 0xA0)[0]
        w = struct.unpack_from("<i", blob, 0xA4)[0]
        h = struct.unpack_from("<i", blob, 0xA8)[0]
    except Exception:
        return None
    if w <= 2 or h <= 2 or w > 4096 or h > 4096:
        return None
    if abs(x) > 10000 or abs(y) > 10000:
        return None
    return {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}


def _fight_hang_icon_state(session, *, log: LogFn | None = None) -> dict:
    """
    定位 Alt+R 战斗挂机图标（非闭关修炼 Img_AutoPlay）。

    优先：Win_FightModeMinimize 整窗 / 其上控件；
    其次：主界面父窗上的 FightMode/Hang 控件。

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "parent": None,
        "ctrl": None,
        "ptr": 0,
        "rect": None,
        "rect_src": None,
        "checked": None,
        "raw_isshow": None,
        "aui_show": None,
        "enable": None,
    }
    quiet = lambda _m: None

    def _fill_from_ptr(parent: str, ctrl: str | None, ptr: int, *, as_dlg: bool = False) -> bool:
        ptr = int(ptr or 0) & 0xFFFFFFFF
        if not ptr:
            return False
        out["parent"] = parent
        out["ctrl"] = ctrl
        out["ptr"] = ptr
        out["raw_isshow"] = _aui_raw_isshow_byte(session, ptr)
        out["aui_show"] = _aui_obj_is_show(session, ptr, log=quiet)
        try:
            out["enable"] = bool(aui_obj_is_enable(session, ptr, log=quiet))
        except Exception:
            out["enable"] = None
        if not as_dlg:
            out["checked"] = _read_aui_push_checked(session, ptr)
        blob = _read_aui_ctrl_blob(session, ptr, 0xC0)
        if blob:
            out["blob_hex"] = blob.hex()
            rect = _rect_from_aui_blob(blob)
            if rect:
                out["rect"] = rect
                out["rect_src"] = "blob"
                return True
        if not as_dlg:
            try:
                from app.core.aui_click import read_aui_ctrl_rect

                rc = read_aui_ctrl_rect(session, ptr, name=ctrl or parent, log=quiet)
                if getattr(rc, "ok", False):
                    out["rect"] = {
                        "x": int(rc.x), "y": int(rc.y),
                        "w": int(rc.w), "h": int(rc.h),
                    }
                    out["rect_src"] = "read_aui_ctrl_rect"
                    return True
            except Exception as e:
                out["rect_err"] = str(e)
        return bool(out.get("ptr"))

    # 1) 迷你挂机条整窗
    for dname in (
        "Win_FightModeMinimize",
        "Win_FightOperationChoose",
        "Win_SettingFightOperation",
    ):
        try:
            dlg = get_game_ui_dlg(session, dname, log=quiet) or 0
        except Exception:
            dlg = 0
        if not dlg:
            continue
        vis = _dlg_visible_dual(session, int(dlg), log=quiet)
        out.setdefault("candidates", []).append({"name": dname, **vis})
        # 控件优先
        for cname in HANG_BTN_CTRL_CANDIDATES:
            try:
                c = aui_get_dlg_item(session, int(dlg), cname, log=quiet) or 0
            except Exception:
                c = 0
            if c and _fill_from_ptr(dname, cname, int(c)):
                return out
        # 整窗
        if _fill_from_ptr(dname, None, int(dlg), as_dlg=True):
            if vis.get("visible") or int(vis.get("raw_isshow") or 0):
                return out

    # 2) 主界面控件
    for parent in HANG_BTN_PARENT_DLGS:
        if parent.startswith("Win_AutoPlay"):
            continue
        try:
            dlg = get_game_ui_dlg(session, parent, log=quiet) or 0
        except Exception:
            dlg = 0
        if not dlg:
            continue
        for cname in HANG_BTN_CTRL_CANDIDATES:
            try:
                c = aui_get_dlg_item(session, int(dlg), cname, log=quiet) or 0
            except Exception:
                c = 0
            if c and _fill_from_ptr(parent, cname, int(c)):
                return out

    # 3) 兜底：右上角区域（用户截图 Alt+R 附近），用 MainInfoRight / Main 的右上 rect
    for parent in ("Win_MainInfoRight", "Win_Main", "Win_MainInfoFrame"):
        try:
            dlg = get_game_ui_dlg(session, parent, log=quiet) or 0
        except Exception:
            dlg = 0
        if not dlg:
            continue
        blob = _read_aui_ctrl_blob(session, int(dlg), 0xC0)
        rect = _rect_from_aui_blob(blob or b"")
        if rect and rect.get("w", 0) > 20:
            # 取该窗右上角 56x40 作挂机键近似采样区
            out["parent"] = parent
            out["ctrl"] = "(top-right-approx)"
            out["ptr"] = int(dlg) & 0xFFFFFFFF
            rx = int(rect["x"] + max(0, rect["w"] - 56))
            ry = int(rect["y"] + 4)
            out["rect"] = {"x": rx, "y": ry, "w": 56, "h": 40}
            out["rect_src"] = f"{parent}_topright_approx"
            return out
    return out


def _img_autoplay_state(session, *, log: LogFn | None = None) -> dict:
    """
    右上角挂机键 Win_AutoPlayTip.Img_AutoPlay 深读（截图区域）。

    开=绿色；关=金/棕（用户截图色）。

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "parent": "Win_AutoPlayTip",
        "ctrl": "Img_AutoPlay",
        "ptr": 0,
        "raw_isshow": None,
        "aui_show": None,
        "enable": None,
        "checked": None,
        "rect": None,
        "rect_src": None,
        "blob_hex": None,
        "blob_bools": {},
    }
    try:
        dlg = get_game_ui_dlg(session, "Win_AutoPlayTip", log=log) or 0
    except Exception:
        dlg = 0
    out["parent_ptr"] = int(dlg) & 0xFFFFFFFF
    if dlg:
        out["parent_raw_isshow"] = _aui_raw_isshow_byte(session, int(dlg))
        out["parent_dlg_show"] = bool(is_dlg_show(session, int(dlg), log=log))
    try:
        ctrl = aui_get_dlg_item(session, int(dlg), "Img_AutoPlay", log=log) if dlg else 0
    except Exception:
        ctrl = 0
    out["ptr"] = int(ctrl or 0) & 0xFFFFFFFF
    if not ctrl:
        # fallback：用 tip 窗本身的 rect 采样（图标在 tip 内）
        if dlg:
            pblob = _read_aui_ctrl_blob(session, int(dlg), 0xC0)
            prect = _rect_from_aui_blob(pblob or b"")
            if prect:
                out["rect"] = prect
                out["rect_src"] = "parent_tip"
        return out
    out["raw_isshow"] = _aui_raw_isshow_byte(session, int(ctrl))
    out["aui_show"] = _aui_obj_is_show(session, int(ctrl), log=log)
    try:
        out["enable"] = bool(aui_obj_is_enable(session, int(ctrl), log=log))
    except Exception:
        out["enable"] = None
    out["checked"] = _read_aui_push_checked(session, int(ctrl))
    # 0xC0 才能盖住 x/y/w/h @ 0x9C..0xA8
    blob = _read_aui_ctrl_blob(session, int(ctrl), 0xC0)
    if blob:
        out["blob_hex"] = blob.hex()
        bools = {}
        for off in range(0, min(len(blob), 0xA0)):
            b = blob[off]
            if b in (0, 1):
                bools[f"+{off:02X}"] = b
        key_slots = {}
        for off in (
            0x48, 0x4C, 0x50, 0x54, 0x58, 0x5C, 0x60, 0x64, 0x68, 0x6C, 0x70,
            0x90, 0x94, 0x9C, 0xA0, 0xA4, 0xA8,
        ):
            if off < len(blob):
                key_slots[f"+{off:02X}"] = int(blob[off])
        out["blob_key_bytes"] = key_slots
        out["blob_ones"] = [k for k, v in bools.items() if v == 1][:40]
        rect = _rect_from_aui_blob(blob)
        if rect:
            out["rect"] = rect
            out["rect_src"] = "ctrl_blob"
    if not out.get("rect"):
        try:
            from app.core.aui_click import read_aui_ctrl_rect

            rc = read_aui_ctrl_rect(session, int(ctrl), name="Img_AutoPlay", log=log)
            if getattr(rc, "ok", False):
                out["rect"] = {
                    "x": int(rc.x),
                    "y": int(rc.y),
                    "w": int(rc.w),
                    "h": int(rc.h),
                }
                out["rect_src"] = "read_aui_ctrl_rect"
            else:
                out["rect_err"] = getattr(rc, "error", None)
        except Exception as e:
            out["rect_err"] = str(e)
    if not out.get("rect") and dlg:
        pblob = _read_aui_ctrl_blob(session, int(dlg), 0xC0)
        prect = _rect_from_aui_blob(pblob or b"")
        if prect:
            out["rect"] = prect
            out["rect_src"] = "parent_tip_fallback"
    return out


def _sample_ctrl_glow(
    session,
    hwnd: int,
    rect: dict | None,
    *,
    log: LogFn | None = None,
) -> dict | None:
    """
    Sample hang button pixels.

    用户线索：开=绿色；关=截图金/棕色。

    @author by ak
    """
    log = log or (lambda _m: None)
    if not hwnd:
        return {"ok": False, "error": "no hwnd"}
    if not rect:
        return {"ok": False, "error": "no rect"}
    try:
        from app.core.win_capture import capture_client_png
        from io import BytesIO
        from PIL import Image
    except Exception as e:
        return {"ok": False, "error": f"capture deps: {e}"}
    try:
        cap = capture_client_png(int(hwnd), log=log)
        if not getattr(cap, "ok", False) or not getattr(cap, "png", None):
            return {"ok": False, "error": getattr(cap, "error", "capture fail")}
        im = Image.open(BytesIO(cap.png)).convert("RGB")
        x, y, w, h = int(rect["x"]), int(rect["y"]), int(rect["w"]), int(rect["h"])
        # 相对/负坐标：部分 AUI 用右锚点，x 可能偏大/负
        if x < 0:
            x = int(im.width + x)
        if y < 0:
            y = int(im.height + y)
        # 控件可能是相对父窗坐标：若完全越界，尝试贴右上角
        if x >= im.width or y >= im.height or x + w <= 0 or y + h <= 0:
            # tip 常在右上：取右上 80x80 兜底采样区
            fw = min(80, im.width)
            fh = min(80, im.height)
            x = max(0, im.width - fw - 40)
            y = max(0, 8)
            w, h = fw, fh
            used_fallback_region = True
        else:
            used_fallback_region = False
        pad_x = max(1, w // 6)
        pad_y = max(1, h // 6)
        x0 = max(0, x + pad_x)
        y0 = max(0, y + pad_y)
        x1 = min(im.width, x + w - pad_x)
        y1 = min(im.height, y + h - pad_y)
        if x1 <= x0 or y1 <= y0:
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(im.width, x + w), min(im.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return {"ok": False, "error": f"bad crop rect xywh=({x},{y},{w},{h}) client={im.width}x{im.height}"}
        crop = im.crop((x0, y0, x1, y1))
        px = list(crop.getdata())
        if not px:
            return {"ok": False, "error": "empty crop"}
        n = float(len(px))
        r = sum(p[0] for p in px) / n
        g = sum(p[1] for p in px) / n
        b = sum(p[2] for p in px) / n
        mean = (r + g + b) / 3.0
        # 绿色优势：G 明显高于 R/B
        green_dom = g - max(r, b)
        # 金/棕：R≈G 且 > B（关）
        goldish = (r + g) / 2.0 - b - abs(r - g)
        # 像素级绿色占比：G 主导且够亮
        green_n = 0
        gold_n = 0
        for pr, pg, pb in px:
            if pg >= 90 and pg > pr + 12 and pg > pb + 12:
                green_n += 1
            if pr >= 70 and pg >= 60 and abs(pr - pg) <= 35 and min(pr, pg) > pb + 15:
                gold_n += 1
        green_ratio = green_n / n
        gold_ratio = gold_n / n
        return {
            "ok": True,
            "mean": round(mean, 1),
            "r": round(r, 1),
            "g": round(g, 1),
            "b": round(b, 1),
            "green_dom": round(green_dom, 1),
            "green_ratio": round(green_ratio, 3),
            "gold_ratio": round(gold_ratio, 3),
            "goldish": round(goldish, 1),
            "crop": [x0, y0, x1, y1],
            "rect_in": [x, y, w, h],
            "fallback_region": used_fallback_region,
            "client": [int(im.width), int(im.height)],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _is_hang_false_positive_dlg(name: str) -> bool:
    """闭关修炼/无关 UI：绝不能当挂机开. @author by ak"""
    n = str(name or "").strip()
    if not n:
        return True
    if n in HANG_STATE_FALSE_POSITIVE:
        return True
    low = n.lower()
    # 闭关修炼整族
    if "autoplay" in low:
        return True
    if "tip" in low or "guide" in low or "autolock" in low:
        return True
    if "countryfight" in low or "fightingcircle" in low:
        return True
    if "targetplayer" in low:
        return True
    return False


def _is_hang_on_signal_dlg(name: str) -> bool:
    """
    Only FightMode / FightOperation dialogs count as hang ON.

    严禁 AutoPlay*（闭关修炼设置）。

    @author by ak
    """
    n = str(name or "").strip()
    if not n or _is_hang_false_positive_dlg(n):
        return False
    low = n.lower()
    # hard ban 闭关修炼
    if "autoplay" in low:
        return False
    if n in HANG_STATE_DLG_CANDIDATES:
        return True
    if "fightmodeminimize" in low:
        return True
    if "fightoperation" in low or "fightopr" in low:
        return True
    if "settingopr" in low or "settingfight" in low:
        return True
    if "fighthelper" in low:
        return True
    return False


def _dlg_visible_dual(
    session: GameAttachSession,
    dlg_ptr: int,
    *,
    log: LogFn | None = None,
) -> dict:
    """
    Triple visibility: IsDlgShow + GetAUIObjIsShow + raw byte[this+0x94].

    实机 IsDlgShow 对挂机条常恒 False；内存 +0x94 是更早能读到的真值。

    @author by ak
    """
    log = log or (lambda _m: None)
    ptr = int(dlg_ptr or 0) & 0xFFFFFFFF
    out = {
        "ptr": ptr,
        "is_dlg_show": None,
        "aui_is_show": None,
        "raw_isshow": None,
        "visible": False,
    }
    if not ptr:
        return out
    try:
        out["is_dlg_show"] = bool(is_dlg_show(session, ptr, log=log))
    except Exception as e:
        out["is_dlg_show_err"] = str(e)
    try:
        out["aui_is_show"] = _aui_obj_is_show(session, ptr, log=log)
    except Exception as e:
        out["aui_is_show_err"] = str(e)
    try:
        out["raw_isshow"] = _aui_raw_isshow_byte(session, ptr)
    except Exception as e:
        out["raw_isshow_err"] = str(e)
    raw_on = out.get("raw_isshow")
    out["visible"] = bool(
        out.get("is_dlg_show")
        or out.get("aui_is_show")
        or (isinstance(raw_on, int) and raw_on != 0)
    )
    return out


def _probe_host_states_hex(session: GameAttachSession) -> str | None:
    """Best-effort host i64 states for A/B hang recon. @author by ak"""
    try:
        from app.core.combat_probe import sample_combat_probe

        s = sample_combat_probe(session, log=lambda _m: None)
        if getattr(s, "ok", False) and getattr(s, "host_states_hex", None):
            return str(s.host_states_hex)
    except Exception:
        pass
    try:
        from app.core.automove import remote_call_cdecl_x86, remote_call_cdecl_x86_ret64
        from app.core.plg_exports import (
            EXPORT_GET_HOST_PLAYER,
            EXPORT_GET_OBJECT_I64_STATES,
            find_xajh_exe,
            resolve_export_rva,
            DEFAULT_IMAGE_BASE,
        )

        base = int(getattr(session, "module_base", 0) or 0)
        pe = find_xajh_exe(getattr(session, "exe_path", None))
        if not base or pe is None:
            return None
        rva_h = resolve_export_rva(pe, EXPORT_GET_HOST_PLAYER)
        rva_s = resolve_export_rva(pe, EXPORT_GET_OBJECT_I64_STATES)
        if rva_h is None or rva_s is None:
            return None
        pid = int(session.pid)
        host = int(remote_call_cdecl_x86(pid, base + int(rva_h), [])) & 0xFFFFFFFF
        if not host:
            return None
        try:
            st = int(remote_call_cdecl_x86_ret64(pid, base + int(rva_s), [host]))
        except Exception:
            st = int(remote_call_cdecl_x86(pid, base + int(rva_s), [host]))
        return f"0x{st & ((1 << 64) - 1):016X}"
    except Exception:
        return None


def _list_all_shown_dlgs(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
    max_n: int = 80,
) -> list[str]:
    """Names of dialogs currently IsDlgShow. @author by ak"""
    log = log or (lambda _m: None)
    shown: list[str] = []
    try:
        from app.core.plg_ui import list_game_ui_dlg_names

        listed = list_game_ui_dlg_names(session, log=log)
        for nm in list(getattr(listed, "names", None) or []):
            try:
                r = query_dlg_show(session, str(nm), log=lambda _m: None)
            except Exception:
                continue
            if r.ok and r.shown and r.dlg_ptr:
                shown.append(str(nm))
                if len(shown) >= max_n:
                    break
    except Exception:
        pass
    return shown


def _rpm_u32(session, addr: int) -> int | None:
    """Read u32 from game process. @author by ak"""
    addr = int(addr or 0) & 0xFFFFFFFF
    if not addr:
        return None
    raw = _read_remote_bytes(session, addr, 4)
    if not raw or len(raw) < 4:
        return None
    return int(struct.unpack_from("<I", raw, 0)[0])


def _rpm_u8(session, addr: int) -> int | None:
    """Read u8 from game process. @author by ak"""
    addr = int(addr or 0) & 0xFFFFFFFF
    if not addr:
        return None
    raw = _read_remote_bytes(session, addr, 1)
    if not raw:
        return None
    return int(raw[0])


def _write_remote_bytes(session, addr: int, data: bytes) -> bool:
    """
    Write game-owned memory through the serialized scene-gated runtime.

    Prefers a direct pymem write (same handle used for reads) and only falls
    back to the CRT/serialized channel when pymem is unavailable.

    @author by ak
    """
    addr = int(addr or 0) & 0xFFFFFFFF
    payload = bytes(data or b"")
    if not addr or not payload:
        return False
    pm = getattr(session, "pm", None)
    if pm is not None:
        try:
            import pymem.memory

            pymem.memory.write_bytes(
                pm.process_handle, addr, payload, len(payload)
            )
            return True
        except Exception:
            pass
    try:
        pid = int(getattr(session, "pid", 0) or 0)
        if not pid:
            return False
        from app.core.remote_runtime import remote_write_bytes

        return remote_write_bytes(pid, addr, payload) == len(payload)
    except Exception:
        return False


def _wpm_u8(session, addr: int, value: int) -> bool:
    """Write one byte. @author by ak"""
    return _write_remote_bytes(session, int(addr), bytes([int(value) & 0xFF]))


def _wpm_u32(session, addr: int, value: int) -> bool:
    """Write little-endian u32. @author by ak"""
    return _write_remote_bytes(
        session, int(addr), struct.pack("<I", int(value) & 0xFFFFFFFF)
    )


def _wpm_f32(session, addr: int, value: float) -> bool:
    """Write IEEE754 float32. @author by ak"""
    return _write_remote_bytes(session, int(addr), struct.pack("<f", float(value)))


def _rpm_f32(session, addr: int) -> float | None:
    """Read float32. @author by ak"""
    raw = _read_remote_bytes(session, int(addr or 0), 4)
    if not raw or len(raw) < 4:
        return None
    return float(struct.unpack_from("<f", raw, 0)[0])


def autoplay_mode_name(mode: int | None) -> str:
    """Human label for CECAutoPlay+0x1C. @author by ak"""
    if mode is None:
        return "?"
    try:
        m = int(mode)
    except Exception:
        return "?"
    if m == int(AUTOPLAY_MODE_NORMAL):
        return "普通模式"
    if m == int(AUTOPLAY_MODE_DUNGEON):
        return "副本模式"
    return f"未知({m})"


def resolve_host_base_rpm(session) -> int:
    """
    Pure RPM: base = *(*(0x15282D8)+0x24)  (0x4AE3B0).

    @author by ak
    """
    try:
        base0 = int(getattr(session, "module_base", 0) or 0)
        if not base0:
            return 0
        # global absolute preferred VA 0x15282D8 -> live = module_base + (note - 0x400000)
        from app.core.plg_exports import DEFAULT_IMAGE_BASE

        g_va = int(base0) + (int(NOTE_VA_GAME_ROOT_GLOBAL) - int(DEFAULT_IMAGE_BASE))
        root = _rpm_u32(session, g_va)
        if not root:
            return 0
        mid = _rpm_u32(session, int(root) + int(HOST_DATA_MID_OFF))
        return int(mid or 0)
    except Exception:
        return 0


def resolve_cec_autoplay_rpm(session) -> dict:
    """
    Resolve CECAutoPlay* and running / mode / radius from memory.

    Offsets (RE):
      +0x08 running
      +0x1C mode 0=普通 1=副本
      +0x21 范围半径 (UI 5-50; runtime min ~0.1)

    @author by ak
    """
    out = {
        "ok": False,
        "host_base": 0,
        "autoplay": 0,
        "running": None,
        "mode": None,
        "mode_name": "?",
        "radius": None,
        "error": None,
    }
    try:
        host_base = resolve_host_base_rpm(session)
        out["host_base"] = int(host_base or 0)
        if not host_base:
            out["error"] = "host_base null"
            return out
        ap = _rpm_u32(session, int(host_base) + int(HOST_AUTOPLAY_PTR_OFF))
        out["autoplay"] = int(ap or 0)
        if not ap:
            out["error"] = "autoplay ptr null"
            return out
        # sanity: heap-like user pointer
        if ap < 0x10000 or ap > 0x7FFFFFFF:
            out["error"] = f"autoplay ptr out of range 0x{ap:X}"
            return out
        run = _rpm_u8(session, int(ap) + int(AUTOPLAY_RUNNING_OFF))
        mode = _rpm_u8(session, int(ap) + int(AUTOPLAY_MODE_OFF))
        radius = _rpm_u8(session, int(ap) + int(AUTOPLAY_RADIUS_OFF))
        out["running"] = None if run is None else bool(int(run) != 0)
        out["mode"] = mode
        out["mode_name"] = autoplay_mode_name(mode)
        out["radius"] = radius
        out["running_byte"] = run
        out["ok"] = run is not None
        if run is None:
            out["error"] = "cannot read running byte"
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def set_autoplay_mode(
    session,
    mode: int,
    *,
    log: LogFn | None = None,
) -> dict:
    """
    写 CECAutoPlay+0x1C 模式：0=普通 1=副本。

    建议在挂机关闭时修改；已开挂时改后宜先关再开以重建策略。

    @author by ak
    """
    log = log or (lambda _m: None)
    want = int(mode)
    if want not in (int(AUTOPLAY_MODE_NORMAL), int(AUTOPLAY_MODE_DUNGEON)):
        return {
            "ok": False,
            "error": f"mode 仅支持 0/1，收到 {want}",
            "mode": None,
            "mode_name": "?",
        }
    mem = resolve_cec_autoplay_rpm(session)
    ap = int(mem.get("autoplay") or 0)
    if not mem.get("ok") or not ap:
        return {
            "ok": False,
            "error": str(mem.get("error") or "autoplay unresolved"),
            "before": mem,
            "mode": None,
            "mode_name": "?",
        }
    before = mem.get("mode")
    addr = int(ap) + int(AUTOPLAY_MODE_OFF)
    if not _wpm_u8(session, addr, want):
        return {
            "ok": False,
            "error": f"WPM mode failed addr=0x{addr:X}",
            "before": before,
            "mode": before,
            "mode_name": autoplay_mode_name(before if isinstance(before, int) else None),
            "autoplay": ap,
        }
    after = _rpm_u8(session, addr)
    ok = after is not None and int(after) == want
    log(
        f"autoplay mode: before={before} -> want={want}({autoplay_mode_name(want)}) "
        f"after={after} ok={ok}"
    )
    return {
        "ok": bool(ok),
        "error": None if ok else f"verify failed after={after}",
        "before": before,
        "mode": after,
        "mode_name": autoplay_mode_name(after),
        "autoplay": ap,
        "addr": addr,
    }


def set_autoplay_radius(
    session,
    radius: int,
    *,
    allow_below_ui_min: bool = True,
    log: LogFn | None = None,
) -> dict:
    """
    写 CECAutoPlay+0x21 范围半径。

    - 内存允许 1..255（战斗侧无硬钳 5）
    - UI 文案为 (5-50)；allow_below_ui_min=False 时拒绝 <5

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        want = int(radius)
    except Exception:
        return {"ok": False, "error": "radius 非法", "radius": None}
    if want < int(AUTOPLAY_RADIUS_MEM_MIN) or want > int(AUTOPLAY_RADIUS_MEM_MAX):
        return {
            "ok": False,
            "error": f"radius 需在 {AUTOPLAY_RADIUS_MEM_MIN}..{AUTOPLAY_RADIUS_MEM_MAX}",
            "radius": None,
        }
    if not allow_below_ui_min and want < int(AUTOPLAY_RADIUS_UI_MIN):
        return {
            "ok": False,
            "error": f"radius < UI 下限 {AUTOPLAY_RADIUS_UI_MIN} 且未允许",
            "radius": None,
        }
    mem = resolve_cec_autoplay_rpm(session)
    ap = int(mem.get("autoplay") or 0)
    if not mem.get("ok") or not ap:
        return {
            "ok": False,
            "error": str(mem.get("error") or "autoplay unresolved"),
            "before": mem,
            "radius": None,
        }
    before = mem.get("radius")
    addr = int(ap) + int(AUTOPLAY_RADIUS_OFF)
    if not _wpm_u8(session, addr, want):
        return {
            "ok": False,
            "error": f"WPM radius failed addr=0x{addr:X}",
            "before": before,
            "radius": before,
            "autoplay": ap,
        }
    after = _rpm_u8(session, addr)
    ok = after is not None and int(after) == want
    log(f"autoplay radius: before={before} -> want={want} after={after} ok={ok}")
    return {
        "ok": bool(ok),
        "error": None if ok else f"verify failed after={after}",
        "before": before,
        "radius": after,
        "autoplay": ap,
        "addr": addr,
        "ui_min": int(AUTOPLAY_RADIUS_UI_MIN),
        "ui_max": int(AUTOPLAY_RADIUS_UI_MAX),
        "note": "UI 仍显示 5-50；内存 1 可被战斗逻辑使用",
    }


def set_autoplay_anchor(
    session,
    x: float,
    y: float,
    z: float,
    *,
    log: LogFn | None = None,
) -> dict:
    """
    写 CECAutoPlay+0xD0/D4/D8 普通模式定位点（挂机锚点）。

    开挂时该字段由 StartAutoPlay 从宿主坐标复制；运行中游戏只读不写，
    直接改写即可动态移动挂机圆心，无需重启内挂。与 set_autoplay_radius
    正交：锚点换圆心、radius 换范围。

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        coords = (float(x), float(y), float(z))
    except Exception:
        return {
            "ok": False,
            "error": "coords 非法",
            "anchor": None,
            "x": None,
            "y": None,
            "z": None,
        }
    if any(v != v or v in (float("inf"), float("-inf")) for v in coords):
        return {
            "ok": False,
            "error": "coords 必须为有限数",
            "anchor": None,
            "x": None,
            "y": None,
            "z": None,
        }
    mem = resolve_cec_autoplay_rpm(session)
    ap = int(mem.get("autoplay") or 0)
    if not mem.get("ok") or not ap:
        return {
            "ok": False,
            "error": str(mem.get("error") or "autoplay unresolved"),
            "before": mem,
            "anchor": None,
            "x": None,
            "y": None,
            "z": None,
        }
    before = {
        "x": _rpm_f32(session, ap + int(AUTOPLAY_ANCHOR_X_OFF)),
        "y": _rpm_f32(session, ap + int(AUTOPLAY_ANCHOR_Y_OFF)),
        "z": _rpm_f32(session, ap + int(AUTOPLAY_ANCHOR_Z_OFF)),
    }
    addrs = {
        "x": ap + int(AUTOPLAY_ANCHOR_X_OFF),
        "y": ap + int(AUTOPLAY_ANCHOR_Y_OFF),
        "z": ap + int(AUTOPLAY_ANCHOR_Z_OFF),
    }
    if not all(
        _wpm_f32(session, addrs[k], coords[i]) for i, k in enumerate(("x", "y", "z"))
    ):
        return {
            "ok": False,
            "error": f"WPM anchor failed ap=0x{ap:X}",
            "before": before,
            "anchor": before,
            "autoplay": ap,
            "x": coords[0],
            "y": coords[1],
            "z": coords[2],
        }
    after = {
        "x": _rpm_f32(session, ap + int(AUTOPLAY_ANCHOR_X_OFF)),
        "y": _rpm_f32(session, ap + int(AUTOPLAY_ANCHOR_Y_OFF)),
        "z": _rpm_f32(session, ap + int(AUTOPLAY_ANCHOR_Z_OFF)),
    }
    ok = all(
        after[k] is not None and abs(after[k] - coords[i]) < 1e-3
        for i, k in enumerate(("x", "y", "z"))
    )
    log(
        f"autoplay anchor: before=({before['x']},{before['y']},{before['z']}) "
        f"-> want=({coords[0]},{coords[1]},{coords[2]}) "
        f"after=({after['x']},{after['y']},{after['z']}) ok={ok}"
    )
    return {
        "ok": bool(ok),
        "error": None if ok else "verify failed",
        "before": before,
        "anchor": after,
        "autoplay": ap,
        "addrs": addrs,
        "note": "普通模式挂机圆心，运行中可动态改写",
    }


def _autoplay_skill_base(session) -> dict:
    """
    Resolve CECAutoPlay* for skill IO.

    @author by ak
    """
    mem = resolve_cec_autoplay_rpm(session)
    ap = int(mem.get("autoplay") or 0)
    ok = bool(mem.get("ok")) and ap > 0
    return {
        "ok": ok,
        "autoplay": ap,
        "mem": mem,
        "error": None if ok else str(mem.get("error") or "autoplay unresolved"),
    }


def read_autoplay_skills(session) -> dict:
    """
    读 CECAutoPlay 技能门 + 九槽。

    布局（绝对偏移，#pragma pack(1) 子对象 @+0x27）:
      +0x27 flags
      +0x2B gate_a  +0x2F gate_b
      +0x33 + i*8 : {skill_id:u32, interval:f32}  i=0..8

    @author by ak
    """
    out: dict = {
        "ok": False,
        "autoplay": 0,
        "flags": None,
        "gate_a": None,
        "gate_b": None,
        "slots": [],
        "slot_ids": [],
        "gate_ok": False,
        "filled_slots": 0,
        "error": None,
    }
    base = _autoplay_skill_base(session)
    if not base.get("ok"):
        out["error"] = base.get("error")
        out["mem"] = base.get("mem")
        return out
    ap = int(base["autoplay"])
    out["autoplay"] = ap
    flags = _rpm_u32(session, ap + int(AUTOPLAY_SKILL_FLAGS_OFF))
    gate_a = _rpm_u32(session, ap + int(AUTOPLAY_SKILL_GATE_A_OFF))
    gate_b = _rpm_u32(session, ap + int(AUTOPLAY_SKILL_GATE_B_OFF))
    if flags is None or gate_a is None or gate_b is None:
        out["error"] = "cannot read skill gate fields"
        return out
    out["flags"] = int(flags)
    out["gate_a"] = int(gate_a)
    out["gate_b"] = int(gate_b)
    slots = []
    ids = []
    for i in range(int(AUTOPLAY_SKILL_SLOT_COUNT)):
        off = int(AUTOPLAY_SKILL_SLOT0_OFF) + i * int(AUTOPLAY_SKILL_SLOT_STRIDE)
        sid = _rpm_u32(session, ap + off)
        iv = _rpm_f32(session, ap + off + 4)
        if sid is None or iv is None:
            out["error"] = f"cannot read slot{i} @+0x{off:X}"
            return out
        sid_i = int(sid) & 0xFFFFFFFF
        slots.append(
            {
                "index": i,
                "off": off,
                "skill_id": sid_i,
                "skill_id_hex": f"0x{sid_i:X}",
                "interval": float(iv),
            }
        )
        ids.append(sid_i)
    out["slots"] = slots
    out["slot_ids"] = ids
    out["filled_slots"] = sum(1 for x in ids if int(x) != 0)
    out["gate_ok"] = bool(int(gate_a) != 0 and int(gate_b) != 0)
    out["ok"] = True
    out["running"] = (base.get("mem") or {}).get("running")
    out["mode"] = (base.get("mem") or {}).get("mode")
    out["radius"] = (base.get("mem") or {}).get("radius")
    return out


def probe_autoplay_skill_gate(session) -> dict:
    """
    开挂预检：tip 路径 0xC55D50 要求 gate_a/gate_b 均非 0。

    不调用游戏函数，纯内存镜像判定 + 当前技能槽摘要。

    @author by ak
    """
    sk = read_autoplay_skills(session)
    out = {
        "ok": bool(sk.get("ok")),
        "can_start_by_gate": bool(sk.get("gate_ok")),
        "gate_a": sk.get("gate_a"),
        "gate_b": sk.get("gate_b"),
        "flags": sk.get("flags"),
        "filled_slots": sk.get("filled_slots"),
        "slot_ids": sk.get("slot_ids"),
        "autoplay": sk.get("autoplay"),
        "error": sk.get("error"),
        "note": (
            "门控通过仅表示 tip 校验可通过；实际输出仍依赖九槽 skill_id。"
            if sk.get("gate_ok")
            else "门控失败：需 set_autoplay_skill_slots / fill 写入 +0x2B/+0x2F（及建议九槽）。"
        ),
        "detail": sk,
    }
    return out


def set_autoplay_skill_slots(
    session,
    skill_ids: list[int] | tuple[int, ...] | None = None,
    *,
    intervals: list[float] | tuple[float, ...] | None = None,
    default_interval: float = AUTOPLAY_SKILL_DEFAULT_INTERVAL,
    gate_a: int | None = None,
    gate_b: int | None = None,
    flags: int | None = AUTOPLAY_SKILL_FLAGS_FILLED,
    clear_empty: bool = True,
    log: LogFn | None = None,
) -> dict:
    """
    写挂机技能门 + 九槽，使 tip 开挂通过技能校验。

    - skill_ids: 最多 9 个；不足补 0（clear_empty=True 时清空剩余槽）
    - gate_b 默认取第一个非 0 skill_id
    - gate_a 默认 0x80000001（实机常见样式）；可显式传 skill_id
    - flags 默认写 0x3；传 None 则不改 flags

    @author by ak
    """
    log = log or (lambda _m: None)
    before = read_autoplay_skills(session)
    if not before.get("ok"):
        return {
            "ok": False,
            "error": before.get("error") or "read skills failed",
            "before": before,
        }
    ap = int(before["autoplay"])
    ids: list[int] = []
    for x in list(skill_ids or []):
        try:
            ids.append(int(x) & 0xFFFFFFFF)
        except Exception:
            return {"ok": False, "error": f"bad skill id {x!r}", "before": before}
    if len(ids) > int(AUTOPLAY_SKILL_SLOT_COUNT):
        ids = ids[: int(AUTOPLAY_SKILL_SLOT_COUNT)]
    while len(ids) < int(AUTOPLAY_SKILL_SLOT_COUNT):
        if clear_empty:
            ids.append(0)
        else:
            # keep existing tail
            prev = list(before.get("slot_ids") or [])
            idx = len(ids)
            ids.append(int(prev[idx]) if idx < len(prev) else 0)

    ivs: list[float] = []
    src_iv = list(intervals or [])
    for i in range(int(AUTOPLAY_SKILL_SLOT_COUNT)):
        if i < len(src_iv):
            try:
                ivs.append(float(src_iv[i]))
            except Exception:
                ivs.append(float(default_interval))
        elif ids[i]:
            ivs.append(float(default_interval))
        else:
            ivs.append(0.0)

    primary = next((int(x) for x in ids if int(x) != 0), 0)
    if gate_b is None:
        want_b = int(primary)
    else:
        want_b = int(gate_b) & 0xFFFFFFFF
    if gate_a is None:
        # 有真实技能时用常见样式；否则若用户只想硬开门可再显式传
        want_a = int(AUTOPLAY_SKILL_GATE_A_STYLE) if primary else 0
    else:
        want_a = int(gate_a) & 0xFFFFFFFF

    # 允许显式 gate_a/gate_b=0 清空门控；仅在两者都走默认且无 skill 时拒绝
    explicit_gate = gate_a is not None or gate_b is not None
    if primary == 0 and not explicit_gate and (want_a == 0 or want_b == 0):
        return {
            "ok": False,
            "error": "需要至少一个 skill_id，或显式提供 gate_a/gate_b",
            "before": before,
        }

    writes_ok = True
    # flags
    if flags is not None:
        writes_ok = _wpm_u32(session, ap + int(AUTOPLAY_SKILL_FLAGS_OFF), int(flags)) and writes_ok
    # gate
    writes_ok = _wpm_u32(session, ap + int(AUTOPLAY_SKILL_GATE_A_OFF), want_a) and writes_ok
    writes_ok = _wpm_u32(session, ap + int(AUTOPLAY_SKILL_GATE_B_OFF), want_b) and writes_ok
    # slots
    for i in range(int(AUTOPLAY_SKILL_SLOT_COUNT)):
        off = int(AUTOPLAY_SKILL_SLOT0_OFF) + i * int(AUTOPLAY_SKILL_SLOT_STRIDE)
        writes_ok = _wpm_u32(session, ap + off, ids[i]) and writes_ok
        writes_ok = _wpm_f32(session, ap + off + 4, ivs[i]) and writes_ok

    after = read_autoplay_skills(session)
    gate_ok = bool(after.get("gate_ok"))
    # 验证 gate 写入值；清空场景下 gate_ok=False 也算成功
    verify_a = after.get("gate_a")
    verify_b = after.get("gate_b")
    gate_match = (
        verify_a is not None
        and verify_b is not None
        and int(verify_a) == int(want_a)
        and int(verify_b) == int(want_b)
    )
    expect_open = int(want_a) != 0 and int(want_b) != 0
    ok = bool(
        writes_ok
        and after.get("ok")
        and gate_match
        and (gate_ok if expect_open else not gate_ok)
    )
    log(
        f"autoplay skills: ok={ok} gate_a=0x{want_a:X} gate_b=0x{want_b:X} "
        f"ids={[hex(x) for x in ids if x]} filled={after.get('filled_slots')}"
    )
    return {
        "ok": ok,
        "error": None
        if ok
        else (
            "WPM failed"
            if not writes_ok
            else (
                "gate verify mismatch"
                if not gate_match
                else ("gate still closed" if expect_open and not gate_ok else "verify failed")
            )
        ),
        "before": before,
        "after": after,
        "wrote": {
            "flags": flags,
            "gate_a": want_a,
            "gate_b": want_b,
            "skill_ids": ids,
            "intervals": ivs,
        },
        "can_start_by_gate": gate_ok,
        "autoplay": ap,
    }


def fill_autoplay_skills_for_gate(
    session,
    skill_ids: list[int] | tuple[int, ...] | None = None,
    *,
    default_interval: float = AUTOPLAY_SKILL_DEFAULT_INTERVAL,
    log: LogFn | None = None,
) -> dict:
    """
    一键灌技能：用给定 skill_id 列表写入门控 + 九槽。

    若 skill_ids 为空，尝试复用当前已有非 0 槽 / gate_b（仅重建门控）。

    @author by ak
    """
    log = log or (lambda _m: None)
    cur = read_autoplay_skills(session)
    if not cur.get("ok"):
        return {"ok": False, "error": cur.get("error"), "before": cur}

    ids = [int(x) & 0xFFFFFFFF for x in list(skill_ids or []) if int(x) != 0]
    if not ids:
        # reuse existing slot ids
        ids = [int(x) for x in (cur.get("slot_ids") or []) if int(x) != 0]
    if not ids:
        gb = int(cur.get("gate_b") or 0)
        if gb and (gb & 0x80000000) == 0:
            ids = [gb]
    if not ids:
        return {
            "ok": False,
            "error": "无可用 skill_id：请传入技能号，或先在游戏里配置一次技能",
            "before": cur,
            "hint": "格式示例 skill_ids=[0x952A] 或十进制",
        }
    return set_autoplay_skill_slots(
        session,
        ids,
        default_interval=float(default_interval),
        log=log,
    )


def clear_autoplay_skills(
    session,
    *,
    flags: int = 0xC,
    log: LogFn | None = None,
) -> dict:
    """
    写成空技能：门控 +0x2B/+0x2F=0，九槽 skill_id/interval 全 0。

    flags 默认 0xC（与 C53070 初始化一致）。清空后门控不通过，tip 开挂会失败。

    @author by ak
    """
    log = log or (lambda _m: None)
    ret = set_autoplay_skill_slots(
        session,
        [],
        intervals=[0.0] * int(AUTOPLAY_SKILL_SLOT_COUNT),
        gate_a=0,
        gate_b=0,
        flags=int(flags),
        clear_empty=True,
        log=log,
    )
    ret["action"] = "clear_skills"
    ret["note"] = "已空技能：gate_a/b=0，九槽全 0；tip 开挂不可用，需再灌技能"
    if ret.get("ok"):
        log("autoplay skills cleared (empty)")
    return ret


def _autoplay_recover_base(session) -> dict:
    """
    Resolve CECAutoPlay* for recover-item IO.

    @author by ak
    """
    mem = resolve_cec_autoplay_rpm(session)
    ap = int(mem.get("autoplay") or 0)
    out = {
        "ok": bool(mem.get("ok") and ap),
        "autoplay": ap,
        "host_base": int(mem.get("host_base") or 0),
        "running": mem.get("running"),
        "error": mem.get("error"),
        "mem": mem,
    }
    if not out["ok"]:
        out["error"] = str(mem.get("error") or "autoplay unresolved")
    return out


def read_autoplay_recover(session) -> dict:
    """
    读内挂恢复槽：flags@+0x85 + 4× item_id@+0x89 步长 4。

    返回每个槽：{slot, enabled, item_id, item_id_hex, addr}

    @author by ak
    """
    out: dict = {
        "ok": False,
        "autoplay": 0,
        "flags": None,
        "flags_hex": None,
        "slots": [],
        "enabled_count": 0,
        "filled_count": 0,
        "error": None,
        "note": (
            "恢复区是 CheckHPItem（血/蓝药）逻辑；写入任意伤害道具 tid 仅改内存，"
            "不保证触发使用。强制使用请走背包 UseItem。"
        ),
    }
    base = _autoplay_recover_base(session)
    ap = int(base.get("autoplay") or 0)
    out["autoplay"] = ap
    if not base.get("ok") or not ap:
        out["error"] = str(base.get("error") or "autoplay unresolved")
        return out
    flags = _rpm_u32(session, ap + int(AUTOPLAY_RECOVER_FLAGS_OFF))
    if flags is None:
        fb = _rpm_u8(session, ap + int(AUTOPLAY_RECOVER_FLAGS_OFF))
        flags = int(fb) if fb is not None else None
    if flags is None:
        out["error"] = "cannot read recover flags"
        return out
    flags_i = int(flags) & 0xFFFFFFFF
    out["flags"] = flags_i
    out["flags_hex"] = f"0x{flags_i:X}"
    slots = []
    enabled_n = 0
    filled_n = 0
    for i in range(int(AUTOPLAY_RECOVER_SLOT_COUNT)):
        addr = ap + int(AUTOPLAY_RECOVER_ITEM0_OFF) + i * int(AUTOPLAY_RECOVER_ITEM_STRIDE)
        tid = _rpm_u32(session, addr)
        tid_i = int(tid or 0) & 0xFFFFFFFF
        en = bool(flags_i & (1 << i))
        if en:
            enabled_n += 1
        if tid_i:
            filled_n += 1
        role = (
            AUTOPLAY_RECOVER_SLOT_ROLES[i]
            if i < len(AUTOPLAY_RECOVER_SLOT_ROLES)
            else f"slot{i}"
        )
        name = ""
        if tid_i:
            try:
                from app.core.item_names import resolve_item_display_name

                name = resolve_item_display_name(int(tid_i), "", learn=True) or ""
            except Exception:
                name = ""
        slots.append(
            {
                "slot": i,
                "role": role,
                "enabled": en,
                "item_id": tid_i,
                "item_id_hex": f"0x{tid_i:X}",
                "name": name,
                "addr": addr,
                "addr_hex": f"0x{addr:X}",
            }
        )
    out["slots"] = slots
    out["enabled_count"] = enabled_n
    out["filled_count"] = filled_n
    out["ok"] = True
    out["error"] = None
    return out



def is_autoplay_attack_recover_item(
    *,
    name: str = "",
    keyword: str = "",
    tid: int = 0,
) -> bool:
    """Identify a consumable xxx伤害物品, never an 外功/内功宝石. @author by ak"""
    text = f"{name or ''} {keyword or ''}".lower()
    if any(k.lower() in text for k in AUTOPLAY_RECOVER_ATTACK_EXCLUDE_KEYWORDS):
        return False
    for k in AUTOPLAY_RECOVER_ATTACK_KEYWORDS:
        if k.lower() in text:
            return True
    # known family tid range seen live: 0x41FE..0x4201
    ti = int(tid or 0) & 0xFFFFFFFF
    if 0x41FE <= ti <= 0x4201:
        return True
    return False


def resolve_autoplay_recover_write_slot(
    slot: int | None = None,
    *,
    name: str = "",
    keyword: str = "",
    tid: int = 0,
    prefer_attack_third: bool = True,
) -> int:
    """
    解析写入槽位。

    - slot 传入时按 0..3 使用
    - 攻击药丸默认第 3 格（index=2），避免误写第 1 格 HP 药位

    @author by ak
    """
    if slot is not None:
        si = int(slot)
        if 0 <= si < int(AUTOPLAY_RECOVER_SLOT_COUNT):
            # 若调用方显式传 0 但道具是攻击类且 prefer：仍纠正到第 3 格
            if (
                prefer_attack_third
                and si == 0
                and is_autoplay_attack_recover_item(name=name, keyword=keyword, tid=tid)
            ):
                return int(AUTOPLAY_RECOVER_ATTACK_SLOT)
            return si
    if prefer_attack_third and is_autoplay_attack_recover_item(
        name=name, keyword=keyword, tid=tid
    ):
        return int(AUTOPLAY_RECOVER_ATTACK_SLOT)
    return 0


def set_autoplay_recover_item(
    session,
    slot: int,
    item_id: int,
    *,
    enabled: bool | None = True,
    log: LogFn | None = None,
) -> dict:
    """
    写恢复槽 item_id，并可开关 flags bit。

    - slot: 0..3
    - item_id: 模板 tid（0=清空）
    - enabled: True 置 bit；False 清 bit；None 不改 flags

    实验向：伤害道具 tid 可写入，但运行时仍走 CheckHPItem，未必会用。

    @author by ak
    """
    log = log or (lambda _m: None)
    before = read_autoplay_recover(session)
    out: dict = {
        "ok": False,
        "slot": int(slot),
        "item_id": int(item_id) & 0xFFFFFFFF,
        "enabled": enabled,
        "before": before,
        "after": None,
        "error": None,
    }
    si = int(slot)
    if si < 0 or si >= int(AUTOPLAY_RECOVER_SLOT_COUNT):
        out["error"] = f"slot 仅支持 0..{int(AUTOPLAY_RECOVER_SLOT_COUNT) - 1}"
        return out
    if not before.get("ok"):
        out["error"] = before.get("error") or "read recover failed"
        return out
    ap = int(before.get("autoplay") or 0)
    tid = int(item_id) & 0xFFFFFFFF
    addr = ap + int(AUTOPLAY_RECOVER_ITEM0_OFF) + si * int(AUTOPLAY_RECOVER_ITEM_STRIDE)
    ok_w = _wpm_u32(session, addr, tid)
    if not ok_w:
        out["error"] = f"write item_id failed @0x{addr:X}"
        return out
    flags_before = int(before.get("flags") or 0) & 0xFFFFFFFF
    flags_after = flags_before
    if enabled is not None:
        bit = 1 << si
        if enabled:
            flags_after = flags_before | bit
        else:
            flags_after = flags_before & (~bit & 0xFFFFFFFF)
        if not _wpm_u8(session, ap + int(AUTOPLAY_RECOVER_FLAGS_OFF), flags_after & 0xFF):
            if not _wpm_u32(session, ap + int(AUTOPLAY_RECOVER_FLAGS_OFF), flags_after):
                out["error"] = "write flags failed"
                after = read_autoplay_recover(session)
                out["after"] = after
                return out
    after = read_autoplay_recover(session)
    out["after"] = after
    slot_after = None
    for s in after.get("slots") or []:
        if int(s.get("slot", -1)) == si:
            slot_after = s
            break
    ok = bool(
        after.get("ok")
        and slot_after
        and int(slot_after.get("item_id") or 0) == tid
        and (enabled is None or bool(slot_after.get("enabled")) is bool(enabled))
    )
    out["ok"] = ok
    out["flags_before"] = flags_before
    out["flags_after"] = int(after.get("flags") or flags_after)
    out["addr"] = addr
    if not ok:
        out["error"] = out.get("error") or "verify failed"
    else:
        out["error"] = None
    log(
        f"autoplay recover set: slot={si} tid=0x{tid:X} enabled={enabled} "
        f"flags=0x{flags_before:X}->0x{out['flags_after']:X} ok={ok}"
    )
    return out




def _recover_pct_off_for_slot(slot: int) -> int | None:
    """slot 0..3 -> percent field offset. @author by ak"""
    si = int(slot)
    if si < 0 or si >= len(AUTOPLAY_RECOVER_SLOT_PCT_OFF):
        return None
    return int(AUTOPLAY_RECOVER_SLOT_PCT_OFF[si])


def read_autoplay_recover_pct(session) -> dict:
    """
    读各恢复格触发百分比（低于该值才用）。

    返回 slots: [{slot, grid, role, pct, off}, ...]

    @author by ak
    """
    out: dict = {
        "ok": False,
        "autoplay": 0,
        "slots": [],
        "by_grid": {},
        "error": None,
        "note": "pct=100 表示血/蓝未满就尝试对应格；默认常见 99/40/40/30",
    }
    base = _autoplay_recover_base(session)
    ap = int(base.get("autoplay") or 0)
    out["autoplay"] = ap
    if not base.get("ok") or not ap:
        out["error"] = str(base.get("error") or "autoplay unresolved")
        return out
    slots = []
    for i in range(int(AUTOPLAY_RECOVER_SLOT_COUNT)):
        off = _recover_pct_off_for_slot(i)
        pct = _rpm_u8(session, ap + int(off)) if off is not None else None
        role = (
            AUTOPLAY_RECOVER_SLOT_ROLES[i]
            if i < len(AUTOPLAY_RECOVER_SLOT_ROLES)
            else f"slot{i}"
        )
        row = {
            "slot": i,
            "grid": i + 1,
            "role": role,
            "pct": pct,
            "off": off,
            "off_hex": f"+0x{int(off):02X}" if off is not None else None,
        }
        slots.append(row)
        out["by_grid"][str(i + 1)] = pct
    out["slots"] = slots
    out["hp_pct_a"] = slots[0]["pct"] if slots else None
    out["mp_pct_a"] = slots[1]["pct"] if len(slots) > 1 else None
    out["hp_pct_b"] = slots[2]["pct"] if len(slots) > 2 else None
    out["mp_pct_b"] = slots[3]["pct"] if len(slots) > 3 else None
    out["ok"] = all(s.get("pct") is not None for s in slots)
    out["error"] = None if out["ok"] else "cannot read some pct fields"
    return out


def set_autoplay_recover_pct(
    session,
    slot: int,
    pct: int,
    *,
    log: LogFn | None = None,
) -> dict:
    """
    写某一恢复格的触发百分比（1..100）。

    slot: 0..3（第1~4格）；pct=100 表示非满即尝试。

    @author by ak
    """
    log = log or (lambda _m: None)
    before = read_autoplay_recover_pct(session)
    out: dict = {
        "ok": False,
        "slot": int(slot),
        "grid": int(slot) + 1,
        "pct": int(pct),
        "before": before,
        "after": None,
        "error": None,
    }
    si = int(slot)
    want = int(pct)
    lo = int(AUTOPLAY_RECOVER_PCT_MIN)
    hi = int(AUTOPLAY_RECOVER_PCT_MAX)
    if si < 0 or si >= int(AUTOPLAY_RECOVER_SLOT_COUNT):
        out["error"] = f"slot 仅支持 0..{int(AUTOPLAY_RECOVER_SLOT_COUNT) - 1}"
        return out
    if want < lo or want > hi:
        out["error"] = f"pct 仅支持 {lo}..{hi}，收到 {want}"
        return out
    if not before.get("ok"):
        out["error"] = before.get("error") or "read pct failed"
        return out
    ap = int(before.get("autoplay") or 0)
    off = _recover_pct_off_for_slot(si)
    if off is None or not ap:
        out["error"] = "pct offset unresolved"
        return out
    ok_w = _wpm_u8(session, ap + int(off), want & 0xFF)
    after = read_autoplay_recover_pct(session)
    out["after"] = after
    got = None
    for s in after.get("slots") or []:
        if int(s.get("slot", -1)) == si:
            got = s.get("pct")
            break
    ok = bool(ok_w and after.get("ok") and int(got or -1) == want)
    out["ok"] = ok
    out["off"] = off
    out["got"] = got
    out["error"] = None if ok else "verify failed"
    role = (
        AUTOPLAY_RECOVER_SLOT_ROLES[si]
        if si < len(AUTOPLAY_RECOVER_SLOT_ROLES)
        else f"slot{si}"
    )
    log(
        f"autoplay recover pct: grid={si+1}({role}) "
        f"{before.get('by_grid', {}).get(str(si+1))}->{want}% ok={ok}"
    )
    return out


def set_autoplay_recover_pct_for_grid(
    session,
    grid: int,
    pct: int,
    *,
    log: LogFn | None = None,
) -> dict:
    """1-based 格号写百分比。 @author by ak"""
    g = int(grid)
    if g < 1 or g > int(AUTOPLAY_RECOVER_SLOT_COUNT):
        return {
            "ok": False,
            "error": f"grid 仅支持 1..{int(AUTOPLAY_RECOVER_SLOT_COUNT)}",
            "grid": g,
            "pct": int(pct),
        }
    return set_autoplay_recover_pct(session, g - 1, int(pct), log=log)


def read_autoplay_recover_interval(session) -> dict:
    """
    读内挂恢复检测间隔（ms）。

    HP: +0x270 cnt / +0x274 interval
    MP: +0x290 cnt / +0x294 interval
    默认均为 1000ms。主循环仅当 cnt>=iv 时 call CheckHP/CheckMP。

    @author by ak
    """
    out: dict = {
        "ok": False,
        "autoplay": 0,
        "hp_cnt": None,
        "hp_interval_ms": None,
        "mp_cnt": None,
        "mp_interval_ms": None,
        "hp_pct_a": None,
        "hp_pct_b": None,
        "mp_pct_a": None,
        "mp_pct_b": None,
        "error": None,
        "note": "改 interval 可加快/减慢回血回蓝检测；过低可能无意义（用药自身 CD）",
    }
    base = _autoplay_recover_base(session)
    ap = int(base.get("autoplay") or 0)
    out["autoplay"] = ap
    if not base.get("ok") or not ap:
        out["error"] = str(base.get("error") or "autoplay unresolved")
        return out
    hp_cnt = _rpm_u32(session, ap + int(AUTOPLAY_RECOVER_HP_CNT_OFF))
    hp_iv = _rpm_u32(session, ap + int(AUTOPLAY_RECOVER_HP_IV_OFF))
    mp_cnt = _rpm_u32(session, ap + int(AUTOPLAY_RECOVER_MP_CNT_OFF))
    mp_iv = _rpm_u32(session, ap + int(AUTOPLAY_RECOVER_MP_IV_OFF))
    if None in (hp_cnt, hp_iv, mp_cnt, mp_iv):
        out["error"] = "cannot read interval fields"
        return out
    out["hp_cnt"] = int(hp_cnt)
    out["hp_interval_ms"] = int(hp_iv)
    out["mp_cnt"] = int(mp_cnt)
    out["mp_interval_ms"] = int(mp_iv)
    out["hp_pct_a"] = _rpm_u8(session, ap + int(AUTOPLAY_RECOVER_HP_PCT_A_OFF))
    out["hp_pct_b"] = _rpm_u8(session, ap + int(AUTOPLAY_RECOVER_HP_PCT_B_OFF))
    out["mp_pct_a"] = _rpm_u8(session, ap + int(AUTOPLAY_RECOVER_MP_PCT_A_OFF))
    out["mp_pct_b"] = _rpm_u8(session, ap + int(AUTOPLAY_RECOVER_MP_PCT_B_OFF))
    out["ok"] = True
    out["error"] = None
    return out


def set_autoplay_recover_interval(
    session,
    *,
    hp_ms: int | None = None,
    mp_ms: int | None = None,
    reset_counters: bool = True,
    log: LogFn | None = None,
) -> dict:
    """
    写内挂恢复检测间隔（ms）。

    - hp_ms / mp_ms: None 表示不改该侧
    - 合法范围 AUTOPLAY_RECOVER_IV_MIN_MS..MAX（默认 50..60000）
    - reset_counters=True：写后把 cnt 清 0，立刻按新周期走

    实机默认 1000；改为 200~500 可明显加快检测。
    注意：只加快「检测」，实际吃药还受道具 CD / 阈值 (+0x99/+0x9B 等) 限制。

    @author by ak
    """
    log = log or (lambda _m: None)
    before = read_autoplay_recover_interval(session)
    out: dict = {
        "ok": False,
        "before": before,
        "after": None,
        "hp_ms": hp_ms,
        "mp_ms": mp_ms,
        "error": None,
    }
    if not before.get("ok"):
        out["error"] = before.get("error") or "read failed"
        return out
    ap = int(before.get("autoplay") or 0)
    lo = int(AUTOPLAY_RECOVER_IV_MIN_MS)
    hi = int(AUTOPLAY_RECOVER_IV_MAX_MS)
    writes_ok = True
    if hp_ms is not None:
        v = int(hp_ms)
        if v < lo or v > hi:
            out["error"] = f"hp_ms 需在 {lo}..{hi}，收到 {v}"
            return out
        writes_ok = _wpm_u32(session, ap + int(AUTOPLAY_RECOVER_HP_IV_OFF), v) and writes_ok
        if reset_counters:
            writes_ok = _wpm_u32(session, ap + int(AUTOPLAY_RECOVER_HP_CNT_OFF), 0) and writes_ok
    if mp_ms is not None:
        v = int(mp_ms)
        if v < lo or v > hi:
            out["error"] = f"mp_ms 需在 {lo}..{hi}，收到 {v}"
            return out
        writes_ok = _wpm_u32(session, ap + int(AUTOPLAY_RECOVER_MP_IV_OFF), v) and writes_ok
        if reset_counters:
            writes_ok = _wpm_u32(session, ap + int(AUTOPLAY_RECOVER_MP_CNT_OFF), 0) and writes_ok
    if hp_ms is None and mp_ms is None:
        out["error"] = "请至少指定 hp_ms 或 mp_ms"
        return out
    after = read_autoplay_recover_interval(session)
    out["after"] = after
    ok = bool(writes_ok and after.get("ok"))
    if ok and hp_ms is not None:
        ok = int(after.get("hp_interval_ms") or -1) == int(hp_ms)
    if ok and mp_ms is not None:
        ok = int(after.get("mp_interval_ms") or -1) == int(mp_ms)
    out["ok"] = ok
    out["error"] = None if ok else (out.get("error") or "verify failed")
    log(
        f"autoplay recover interval: ok={ok} "
        f"hp={before.get('hp_interval_ms')}->{after.get('hp_interval_ms')} "
        f"mp={before.get('mp_interval_ms')}->{after.get('mp_interval_ms')}"
    )
    return out



def set_autoplay_recover_list(
    session,
    items: list[int] | tuple[int, ...] | None = None,
    *,
    enable_bits: int | None = None,
    log: LogFn | None = None,
) -> dict:
    """
    批量写挂机自动恢复物品名单（最多 4 个 tid）。

    items[i] -> 槽 i 的模板 id；不足补 0，超出截断。
    enable_bits: 若给则写 +0x85 flags；None 时对非 0 tid 自动置对应 bit。

    例：set_autoplay_recover_list(sess, [74401, 74402, 0, 0])
      -> HP药=50%血药, MP药=60%蓝药

    @author by ak
    """
    log = log or (lambda _m: None)
    before = read_autoplay_recover(session)
    if not before.get("ok"):
        return {"ok": False, "error": before.get("error") or "read failed", "before": before}
    ids = [int(x) & 0xFFFFFFFF for x in list(items or [])]
    if len(ids) > int(AUTOPLAY_RECOVER_SLOT_COUNT):
        ids = ids[: int(AUTOPLAY_RECOVER_SLOT_COUNT)]
    while len(ids) < int(AUTOPLAY_RECOVER_SLOT_COUNT):
        ids.append(0)
    results = []
    ok_all = True
    for i, tid in enumerate(ids):
        en = bool(tid) if enable_bits is None else bool(int(enable_bits) & (1 << i))
        r = set_autoplay_recover_item(session, i, tid, enabled=en, log=log)
        results.append(r)
        ok_all = ok_all and bool(r.get("ok"))
    if enable_bits is not None:
        ap = int(before.get("autoplay") or 0)
        if ap:
            _wpm_u8(session, ap + int(AUTOPLAY_RECOVER_FLAGS_OFF), int(enable_bits) & 0xFF)
    after = read_autoplay_recover(session)
    out = {
        "ok": ok_all and bool(after.get("ok")),
        "before": before,
        "after": after,
        "items": ids,
        "results": results,
        "error": None if ok_all else "some slots failed",
    }
    log(f"autoplay recover list: ok={out['ok']} items={[hex(x) for x in ids]}")
    return out


def clear_autoplay_recover_slot(
    session,
    slot: int,
    *,
    disable: bool = True,
    log: LogFn | None = None,
) -> dict:
    """清空单个恢复槽（item_id=0，默认关 bit）。 @author by ak"""
    return set_autoplay_recover_item(
        session,
        int(slot),
        0,
        enabled=(False if disable else None),
        log=log,
    )


def find_lab_bag_items(
    session,
    keyword: str = "伤害物品",
    *,
    package_indexes: list[int] | tuple[int, ...] | None = None,
    log: LogFn | None = None,
) -> dict:
    """
    实验：按名称关键字在主包/扩展/仓库搜道具。

    默认关键字匹配「xxx伤害物品」一类，不匹配宝石。

    @author by ak
    """
    log = log or (lambda _m: None)
    from app.core.package_api import (
        DEFAULT_EXCHANGE_PACKAGE_INDEXES,
        list_packages_items,
    )

    key = (keyword or "").strip()
    idxs = list(package_indexes or DEFAULT_EXCHANGE_PACKAGE_INDEXES)
    try:
        all_items = list_packages_items(session, idxs, log=log)
    except Exception as e:
        return {
            "ok": False,
            "keyword": key,
            "items": [],
            "count": 0,
            "error": str(e),
        }
    hits = []
    for it in all_items:
        name = getattr(it, "name", "") or ""
        tid = int(getattr(it, "tid", 0) or 0)
        if key:
            key_l = key.lower()
            matched = (
                key_l in name.lower()
                or key_l in f"{tid}"
                or key_l in f"0x{tid:x}"
            )
            if not matched:
                try:
                    if key.isdigit() and int(key) == tid:
                        matched = True
                    elif key.lower().startswith("0x") and int(key, 16) == tid:
                        matched = True
                except Exception:
                    pass
            if not matched:
                continue
        hits.append(it.to_dict() if hasattr(it, "to_dict") else dict(it))
    out = {
        "ok": True,
        "keyword": key,
        "packages": idxs,
        "items": hits,
        "count": len(hits),
        "error": None,
        "note": "优先用背包 UseItem 强用；恢复槽仅适合血/蓝药类",
    }
    log(f"lab bag search: keyword={key!r} hits={len(hits)} packs={idxs}")
    return out


def force_use_lab_bag_item(
    session,
    *,
    keyword: str | None = None,
    package: int | None = None,
    slot: int | None = None,
    tid: int | None = None,
    package_indexes: list[int] | tuple[int, ...] | None = None,
    log: LogFn | None = None,
    prefer_bridge: bool = True,
) -> dict:
    """
    实验：强制使用一次背包道具（桥接 UseItem 优先）。

    定位优先级：
      1) 显式 package+slot
      2) tid 精确匹配
      3) keyword 名称/tid 子串（取第一个）

    @author by ak
    """
    log = log or (lambda _m: None)
    from app.core.package_api import (
        DEFAULT_EXCHANGE_PACKAGE_INDEXES,
        list_packages_items,
        use_item_in_package,
    )

    out: dict = {
        "ok": False,
        "action": "force_use_bag",
        "target": None,
        "use": None,
        "error": None,
    }
    pack = None if package is None else int(package)
    sl = None if slot is None else int(slot)
    want_tid = None if tid is None else int(tid) & 0xFFFFFFFF
    key = (keyword or "").strip()

    target = None
    if pack is not None and sl is not None:
        target = {"package": pack, "slot": sl, "tid": want_tid, "via": "explicit_slot"}
    else:
        idxs = list(package_indexes or DEFAULT_EXCHANGE_PACKAGE_INDEXES)
        try:
            items = list_packages_items(session, idxs, log=log)
        except Exception as e:
            out["error"] = f"list bag failed: {e}"
            return out
        for it in items:
            it_tid = int(getattr(it, "tid", 0) or 0)
            name = getattr(it, "name", "") or ""
            if want_tid is not None and it_tid != want_tid:
                continue
            if key:
                kl = key.lower()
                if (
                    kl not in name.lower()
                    and kl not in f"{it_tid}"
                    and kl not in f"0x{it_tid:x}"
                ):
                    continue
            if want_tid is None and not key:
                continue
            target = {
                "package": int(it.package),
                "slot": int(it.slot),
                "tid": it_tid,
                "count": int(getattr(it, "count", 0) or 0),
                "name": name,
                "bind": int(getattr(it, "bind", -1)),
                "via": "search",
            }
            break
        if target is None:
            out["error"] = (
                f"未找到道具 keyword={key!r} tid={want_tid!r} packs={idxs}"
            )
            return out

    out["target"] = target
    try:
        uret = use_item_in_package(
            session,
            int(target["package"]),
            int(target["slot"]),
            log=log,
            prefer_bridge=prefer_bridge,
            verify_bag=True,
        )
        use_d = uret.to_dict() if hasattr(uret, "to_dict") else {
            "ok": getattr(uret, "ok", False),
            "message": getattr(uret, "message", ""),
            "error": getattr(uret, "error", None),
            "ret": getattr(uret, "ret", None),
        }
        out["use"] = use_d
        out["ok"] = bool(getattr(uret, "ok", False) or use_d.get("ok"))
        if not out["ok"]:
            out["error"] = (
                getattr(uret, "error", None)
                or use_d.get("error")
                or use_d.get("message")
                or "use failed"
            )
        else:
            out["error"] = None
        log(
            f"lab force use: ok={out['ok']} pack={target.get('package')} "
            f"slot={target.get('slot')} tid=0x{int(target.get('tid') or 0):X} "
            f"name={target.get('name')!r}"
        )
    except Exception as e:
        out["error"] = str(e)
        log(f"lab force use failed: {e}")
    return out


def inject_item_tid_to_skill_slot_lab(
    session,
    item_id: int,
    *,
    slot: int = 0,
    log: LogFn | None = None,
) -> dict:
    """
    对照实验：把道具 tid 写进技能九槽（应无效，技能查找路径不是道具）。

    @author by ak
    """
    log = log or (lambda _m: None)
    before = read_autoplay_skills(session)
    if not before.get("ok"):
        return {
            "ok": False,
            "error": before.get("error") or "read skills failed",
            "before": before,
            "note": "技能槽对照实验",
        }
    ids = []
    for s in before.get("slots") or []:
        ids.append(int(s.get("skill_id") or 0))
    while len(ids) < int(AUTOPLAY_SKILL_SLOT_COUNT):
        ids.append(0)
    si = max(0, min(int(slot), int(AUTOPLAY_SKILL_SLOT_COUNT) - 1))
    ids[si] = int(item_id) & 0xFFFFFFFF
    ret = set_autoplay_skill_slots(session, ids, log=log)
    ret["action"] = "inject_item_tid_to_skill_slot"
    ret["note"] = (
        "仅对照：道具 tid 写入技能槽不会变成可放技能；战斗仍走 skill 表查找。"
    )
    log(
        f"lab inject item_tid->skill[{si}]=0x{int(item_id) & 0xFFFFFFFF:X} "
        f"ok={ret.get('ok')}"
    )
    return ret


def start_autoplay_force(
    session,
    *,
    send_packet: bool = False,
    log: LogFn | None = None,
) -> dict:
    """
    强开挂机：直接 thiscall CECAutoPlay::StartAutoPlay (0xC5ABE0)。

    - **不走** tip/UI，**不校验** +0x2B/+0x2F 技能门（空技能也能开）
    - 与包响应 DB9130 同路径：ecx=autoplay → call Start → [ap+0x08]=1
    - send_packet=True 时额外 cdecl 调 Game_StartAutoPlay 包 0xCCA150（cmd 0x15），
      更接近 tip 发网流程；默认 False，纯本地开挂更稳、无技能依赖

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "via": "StartAutoPlay",
        "autoplay": 0,
        "before_running": None,
        "after_running": None,
        "ret": None,
        "packet": None,
        "skills": None,
        "error": None,
    }
    mem = resolve_cec_autoplay_rpm(session)
    ap = int(mem.get("autoplay") or 0)
    out["autoplay"] = ap
    out["before_running"] = mem.get("running")
    out["skills"] = read_autoplay_skills(session)
    if not mem.get("ok") or not ap:
        out["error"] = str(mem.get("error") or "autoplay unresolved")
        return out
    if mem.get("running") is True:
        out["ok"] = True
        out["after_running"] = True
        out["error"] = None
        out["note"] = "already running"
        log("autoplay force start: already on")
        return out

    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        out["error"] = "no pid"
        return out
    start_va = _note_to_live_va(session, int(NOTE_VA_START_AUTOPLAY))
    if not start_va:
        out["error"] = "module_base missing"
        return out

    if send_packet:
        try:
            from app.core.remote_runtime import remote_call_cdecl_x86

            pkt_va = _note_to_live_va(session, int(NOTE_VA_GAME_START_AUTOPLAY_PKT))
            pret = remote_call_cdecl_x86(pid, int(pkt_va), [], timeout_ms=3000)
            out["packet"] = {"ok": True, "ret": pret & 0xFFFFFFFF, "va": pkt_va}
            log(f"autoplay force start packet 0x15 ret=0x{pret & 0xFFFFFFFF:X}")
        except Exception as e:
            out["packet"] = {"ok": False, "error": str(e)}
            log(f"autoplay force start packet fail: {e}")

    try:
        # StartAutoPlay: thiscall, 无栈参，ret（callee no arg cleanup）
        ret = remote_call_thiscall_x86(
            pid,
            int(start_va),
            int(ap),
            [],
            caller_cleanup=False,
            timeout_ms=5000,
        )
        out["ret"] = int(ret) & 0xFFFFFFFF
    except Exception as e:
        out["error"] = f"StartAutoPlay call failed: {e}"
        log(out["error"])
        return out

    after = resolve_cec_autoplay_rpm(session)
    out["after_running"] = after.get("running")
    out["after"] = after
    ok = after.get("running") is True
    out["ok"] = bool(ok)
    if not ok:
        out["error"] = (
            f"Start called but running still {after.get('running_byte')!r} "
            f"(ret=0x{(out.get('ret') or 0):X})"
        )
    else:
        out["error"] = None
    sk = out.get("skills") or {}
    log(
        f"autoplay force start: ok={ok} running={out['after_running']} "
        f"gate_ok={sk.get('gate_ok')} filled={sk.get('filled_slots')} "
        f"ret=0x{(out.get('ret') or 0):X}"
    )
    return out


def start_autoplay_force_follow(
    session,
    *,
    hwnd: int = 0,
    settle_s: float = 1.5,
    log: LogFn | None = None,
) -> dict:
    """Function-start autoplay and repair a captain's dungeon follow target.

    This path never opens or clicks AutoPlayFrame. Empty-skill and dungeon
    starts use CECAutoPlay::StartAutoPlay directly; a dungeon captain then
    enters the native StateFollowTarget state with a real party member.

    @author by ak
    """
    log = log or (lambda _m: None)
    hwnd_i = int(hwnd or getattr(session, "hwnd", 0) or 0)
    before = resolve_cec_autoplay_rpm(session)
    out: dict = {
        "ok": False,
        "via": "StartAutoPlay",
        "before_running": before.get("running"),
        "after_running": None,
        "follow_target_id": None,
        "follow_target_name": None,
        "error": None,
    }
    if not before.get("ok"):
        out["error"] = str(before.get("error") or "autoplay unresolved")
        return out

    follow_target_id = 0
    if int(before.get("mode") or 0) == 1:
        try:
            from app.core.plg_ui import host_team_role
            from app.core.team_ops import read_cecteam_members

            role = host_team_role(session, log=lambda _m: None) or {}
            out["team_role"] = str(role.get("role") or "unknown")
            if role.get("role") == "leader" or role.get("is_leader") is True:
                members = read_cecteam_members(session, log=lambda _m: None)
                target = next(
                    (
                        item
                        for item in members
                        if not bool(item.get("is_self"))
                        and int(item.get("obj_id") or 0) > 0
                    ),
                    None,
                )
                if target is None:
                    log("autoplay force start: no follow target, start without follow")
                else:
                    follow_target_id = int(target.get("obj_id") or 0)
                    out["follow_target_id"] = follow_target_id
                    out["follow_target_name"] = str(target.get("name") or "")
        except Exception as e:
            out["error"] = f"读取副本跟随目标失败: {e}"
            return out

    started = start_autoplay_force(session, send_packet=False, log=log)
    out["start"] = started
    out["after_running"] = started.get("after_running")
    if not bool(started.get("ok")):
        out["error"] = str(started.get("error") or "StartAutoPlay failed")
        return out
    out["ok"] = True

    if follow_target_id:
        bridge = None
        try:
            from app.core.xajh_bridge import ensure_bridge

            bridge = ensure_bridge(
                int(getattr(session, "pid", 0) or 0),
                log=log,
                inject_if_needed=True,
                hwnd=hwnd_i or None,
            )
            if bridge is None:
                out["ok"] = False
                out["error"] = "副本跟随桥接未就绪"
            else:
                seeded = None
                deadline = time.monotonic() + max(0.5, min(3.0, float(settle_s)))
                while time.monotonic() < deadline:
                    seeded = bridge.autoplay_seed_follow(
                        follow_target_id,
                        hwnd=hwnd_i or None,
                        timeout_ms=4000,
                    )
                    if seeded.ok:
                        break
                    err = str(seeded.error or seeded.note or "")
                    if "snapshot unavailable" not in err.lower():
                        break
                    time.sleep(0.15)
                out["follow_seed"] = (
                    seeded.to_dict() if seeded is not None else {"ok": False}
                )
                if seeded is None or not seeded.ok:
                    out["ok"] = False
                    out["error"] = str(
                        getattr(seeded, "error", None)
                        or getattr(seeded, "note", None)
                        or "副本模式跟随目标初始化失败"
                    )
        except Exception as e:
            out["ok"] = False
            out["error"] = f"副本模式跟随目标初始化失败: {e}"
        finally:
            if bridge is not None:
                try:
                    bridge.close()
                except Exception:
                    pass

    if not out["ok"]:
        try:
            out["cleanup"] = stop_autoplay_force(
                session,
                reason=0,
                send_packet=False,
                hwnd=hwnd_i,
                log=log,
            )
        except Exception as e:
            out["cleanup_error"] = str(e)
    log(
        "autoplay function start: "
        f"ok={out['ok']} running={out.get('after_running')} "
        f"follow={out.get('follow_target_name') or '-'} "
        f"seeded={bool((out.get('follow_seed') or {}).get('ok'))} "
        f"err={out.get('error')}"
    )
    return out


def start_autoplay_via_settings_ui(
    session,
    *,
    hwnd: int = 0,
    settle_s: float = 1.5,
    log: LogFn | None = None,
) -> dict:
    """Start closed-door training through the game's own settings button.

    ``StartAutoPlay`` alone can set CECAutoPlay+0x08 without completing the
    dialog-owned initialization. The bridge invokes the actual
    ``CDlgAutoPlayFrame::Btn_Start`` handler on the UI thread. Native code
    bypasses only the empty-skill and dungeon-leader rejection edges while
    preserving the global feature gate and the handler's complete start path.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "via": "autoplay_settings_ui_bypass",
        "dialog": "Win_AutoPlayFrame",
        "control": "Btn_Start",
        "dispatched": False,
        "before_running": None,
        "after_running": None,
        "error": None,
        "follow_target_id": None,
        "follow_target_name": None,
    }
    hwnd_i = int(hwnd or getattr(session, "hwnd", 0) or 0)

    before = resolve_cec_autoplay_rpm(session)
    out["before_running"] = before.get("running")
    if not before.get("ok"):
        out["error"] = str(before.get("error") or "autoplay unresolved")
        return out
    if before.get("running") is True:
        out["error"] = "autoplay still running before UI start"
        return out

    # Dungeon mode is a native follow-leader state machine. When the host is
    # captain, bypassing only the UI rejection initializes its cached target as
    # self and the game correctly remains idle. Pick a real member now; after
    # the server start acknowledgement creates the state machine, the bridge
    # seeds this target through the game's own snapshot/transition methods.
    follow_target_id = 0
    if int(before.get("mode") or 0) == 1:
        try:
            from app.core.plg_ui import host_team_role
            from app.core.team_ops import read_cecteam_members

            role = host_team_role(session, log=lambda _m: None) or {}
            out["team_role"] = str(role.get("role") or "unknown")
            if role.get("role") == "leader" or role.get("is_leader") is True:
                members = read_cecteam_members(session, log=lambda _m: None)
                target = next(
                    (
                        item
                        for item in members
                        if not bool(item.get("is_self"))
                        and int(item.get("obj_id") or 0) > 0
                    ),
                    None,
                )
                if target is None:
                    out["error"] = "副本模式队长没有可用的在线队员作为跟随目标"
                    return out
                follow_target_id = int(target.get("obj_id") or 0)
                out["follow_target_id"] = follow_target_id
                out["follow_target_name"] = str(target.get("name") or "")
        except Exception as e:
            out["follow_target_error"] = str(e)

    dlg = ensure_dlg_shown(session, "Win_AutoPlayFrame", log=log)
    if not dlg:
        out["error"] = "Win_AutoPlayFrame unavailable"
        return out
    out["dlg_ptr"] = int(dlg) & 0xFFFFFFFF

    try:
        start_ctrl = aui_get_dlg_item(session, dlg, "Btn_Start", log=log)
    except Exception as e:
        start_ctrl = 0
        out["control_error"] = str(e)
    out["ctrl_ptr"] = int(start_ctrl or 0) & 0xFFFFFFFF
    if not start_ctrl:
        out["error"] = "Win_AutoPlayFrame.Btn_Start unavailable"
        try:
            if is_dlg_show(session, dlg, log=lambda _m: None):
                toggle_dlg_show(session, dlg, log=lambda _m: None)
        except Exception:
            pass
        return out

    bridge = None
    try:
        from app.core.xajh_bridge import ensure_bridge

        bridge = ensure_bridge(
            int(getattr(session, "pid", 0) or 0),
            log=log,
            inject_if_needed=True,
            hwnd=hwnd_i or None,
        )
        if bridge is None:
            out["error"] = "Btn_Start UI-thread bridge unavailable"
        else:
            ret = bridge.autoplay_start_bypass(
                hwnd=hwnd_i or None,
                timeout_ms=5000,
            )
            out["bridge"] = ret.to_dict()
            out["dispatched"] = bool(ret.ok)
            if not ret.ok:
                out["error"] = str(
                    ret.error or ret.note or "Btn_Start handler failed"
                )
    except Exception as e:
        out["error"] = f"Btn_Start handler failed: {e}"

    deadline = time.monotonic() + max(2.5, min(5.0, float(settle_s)))
    while out["dispatched"] and time.monotonic() < deadline:
        time.sleep(0.10)
        state = resolve_cec_autoplay_rpm(session)
        out["after_running"] = state.get("running")
        if state.get("running") is True:
            out["ok"] = True
            out["error"] = None
            break

    if out["ok"] and follow_target_id:
        try:
            seeded = bridge.autoplay_seed_follow(
                follow_target_id,
                hwnd=hwnd_i or None,
                timeout_ms=4000,
            )
            out["follow_seed"] = seeded.to_dict()
            if not seeded.ok:
                out["ok"] = False
                out["error"] = str(
                    seeded.error
                    or seeded.note
                    or "副本模式跟随目标初始化失败"
                )
                # The native button's running branch sends the normal stop
                # packet, avoiding a false-running state after a failed seed.
                cleanup = bridge.autoplay_start_bypass(
                    hwnd=hwnd_i or None,
                    timeout_ms=5000,
                )
                out["cleanup"] = cleanup.to_dict()
        except Exception as e:
            out["ok"] = False
            out["error"] = f"副本模式跟随目标初始化失败: {e}"
    if bridge is not None:
        try:
            bridge.close()
        except Exception:
            pass
    try:
        if is_dlg_show(session, dlg, log=lambda _m: None):
            toggle_dlg_show(session, dlg, log=lambda _m: None)
    except Exception:
        pass

    if not out["ok"] and out.get("error") is None:
        out["error"] = "Btn_Start handler completed but autoplay remained off"
    log(
        "autoplay settings start: "
        f"ok={out['ok']} dispatched={out['dispatched']} "
        f"running={out['after_running']} follow={out.get('follow_target_name') or '-'} "
        f"seeded={bool((out.get('follow_seed') or {}).get('ok'))} "
        f"err={out.get('error')}"
    )
    return out


def stop_autoplay_force(
    session,
    *,
    reason: int = 0,
    send_packet: bool = False,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> dict:
    """
    强关挂机：在游戏 UI 线程 thiscall CECAutoPlay::StopAutoPlay(reason)。

    不走 Alt+R / tip，也不发送停挂包或等待服务器确认。StopAutoPlay 会直接刷新
    游戏内挂机状态和 UI；不能由 CreateRemoteThread 调用，因为该路径实测会在函数
    返回后触发 ACCESS_VIOLATION。

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "via": "StopAutoPlay_UIThread",
        "autoplay": 0,
        "before_running": None,
        "after_running": None,
        "ret": None,
        "packet": None,
        "reason": int(reason) & 0xFF,
        "error": None,
    }
    mem = resolve_cec_autoplay_rpm(session)
    ap = int(mem.get("autoplay") or 0)
    out["autoplay"] = ap
    out["before_running"] = mem.get("running")
    if not mem.get("ok") or not ap:
        out["error"] = str(mem.get("error") or "autoplay unresolved")
        return out
    if mem.get("running") is False:
        out["ok"] = True
        out["after_running"] = False
        out["note"] = "already stopped"
        log("autoplay force stop: already off")
        return out

    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        out["error"] = "no pid"
        return out
    if send_packet:
        out["packet"] = {
            "ok": True,
            "sent": False,
            "note": "StopAutoPlay UI-thread path intentionally sends no server packet",
        }

    try:
        from app.core.xajh_bridge import ensure_bridge

        br = ensure_bridge(
            pid,
            log=log,
            inject_if_needed=True,
            hwnd=int(hwnd or getattr(session, "hwnd", 0) or 0) or None,
        )
        if br is None:
            out["error"] = (
                "UI-thread bridge unavailable; StopAutoPlay was not called "
                "(no unsafe remote-thread fallback)"
            )
            log(f"autoplay force stop blocked: {out['error']}")
            return out
        try:
            ret = br.autoplay_stop(
                int(reason) & 0xFF,
                hwnd=int(hwnd or getattr(session, "hwnd", 0) or 0) or None,
                timeout_ms=5000,
            )
        finally:
            br.close()
        out["bridge"] = ret.to_dict()
        out["ret"] = None if ret.ret is None else int(ret.ret) & 0xFFFFFFFF
        if not ret.ok:
            out["error"] = str(ret.error or ret.note or "StopAutoPlay UI-thread call failed")
            log(f"autoplay force stop failed: {out['error']}")
            return out
    except Exception as e:
        out["error"] = f"StopAutoPlay UI-thread call failed: {e}"
        log(out["error"])
        return out

    after = resolve_cec_autoplay_rpm(session)
    out["after_running"] = after.get("running")
    out["after"] = after
    ok = after.get("running") is False
    out["ok"] = bool(ok)
    if not ok:
        out["error"] = f"Stop called but running still {after.get('running_byte')!r}"
    else:
        out["error"] = None
    log(
        f"autoplay force stop: ok={ok} running={out['after_running']} "
        f"reason={int(reason) & 0xFF} ret=0x{(out.get('ret') or 0):X}"
    )
    return out


def _note_to_live_va(session, note_va: int) -> int:
    """Map note VA (image base 0x400000) to live VA. @author by ak"""
    base0 = int(getattr(session, "module_base", 0) or 0)
    if not base0:
        return 0
    from app.core.plg_exports import DEFAULT_IMAGE_BASE

    return int(base0) + (int(note_va) - int(DEFAULT_IMAGE_BASE))


def patch_autoplay_radius_ui_min(
    session,
    new_min: int = 1,
    *,
    log: LogFn | None = None,
) -> dict:
    """
    运行时 patch 设置页半径钳制：cmp eax,5 → cmp eax,new_min。

    地址 NOTE 0xAFD486（83 F8 05）。打开挂机设置页时不再把内存半径 1 显示/回写成 5。
    可选同步把强制字符串 "5" 改成 str(new_min)（单字符 1..9）。

    @author by ak
    """
    log = log or (lambda _m: None)
    want = int(new_min)
    if want < 1 or want > 9:
        return {"ok": False, "error": "new_min 仅支持 1..9（单 ASCII 字符替换）"}
    cmp_va = _note_to_live_va(session, int(NOTE_VA_AUTOPLAY_RADIUS_UI_CMP))
    str_va = _note_to_live_va(session, int(NOTE_VA_AUTOPLAY_RADIUS_STR_MIN))
    if not cmp_va:
        return {"ok": False, "error": "module_base missing"}
    before = _read_remote_bytes(session, cmp_va, 3)
    if not before or len(before) < 3:
        return {"ok": False, "error": f"cannot read cmp @0x{cmp_va:X}"}
    # expect 83 F8 xx  (cmp eax, imm8)
    if before[0] != 0x83 or before[1] != 0xF8:
        return {
            "ok": False,
            "error": f"unexpected bytes at cmp: {before.hex()} (want 83f8xx)",
            "addr": cmp_va,
            "before": before.hex(),
        }
    old_imm = int(before[2])
    payload = bytes([0x83, 0xF8, want & 0xFF])
    ok_cmp = _write_remote_bytes(session, cmp_va, payload)
    after = _read_remote_bytes(session, cmp_va, 3)
    str_before = _read_remote_bytes(session, str_va, 2) if str_va else None
    ok_str = False
    str_after = None
    if str_va and str_before and len(str_before) >= 1:
        # only patch single digit ASCII
        if 0x30 <= str_before[0] <= 0x39:
            ok_str = _write_remote_bytes(session, str_va, bytes([0x30 + want]))
            str_after = _read_remote_bytes(session, str_va, 2)
    ok = bool(ok_cmp and after and after[2] == (want & 0xFF))
    log(
        f"autoplay radius UI patch: cmp 0x{cmp_va:X} {before.hex()} -> "
        f"{(after or b'').hex()} ok={ok} str_ok={ok_str}"
    )
    return {
        "ok": ok,
        "error": None if ok else "patch verify failed",
        "cmp_addr": cmp_va,
        "before": before.hex(),
        "after": (after or b"").hex(),
        "old_min": old_imm,
        "new_min": want,
        "str_addr": str_va,
        "str_before": (str_before.hex() if str_before else None),
        "str_after": (str_after.hex() if str_after else None),
        "str_ok": ok_str,
        "note": "进程级 patch，重开游戏失效；与 set_autoplay_radius(1) 配合使用",
    }


def read_autoplay_radius_ui_patch_state(session) -> dict:
    """读当前半径 UI cmp 即时字节。 @author by ak"""
    cmp_va = _note_to_live_va(session, int(NOTE_VA_AUTOPLAY_RADIUS_UI_CMP))
    if not cmp_va:
        return {"ok": False, "error": "module_base missing"}
    raw = _read_remote_bytes(session, cmp_va, 3)
    if not raw or len(raw) < 3:
        return {"ok": False, "error": f"cannot read @0x{cmp_va:X}"}
    imm = int(raw[2]) if raw[0] == 0x83 and raw[1] == 0xF8 else None
    return {
        "ok": True,
        "addr": cmp_va,
        "bytes": raw.hex(),
        "ui_min_imm": imm,
        "patched": imm is not None and imm != int(AUTOPLAY_RADIUS_UI_MIN),
    }


def read_instance_countdown_remain_s(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
) -> dict:
    """
    读副本结束倒计时（内存/AUI 文本，不靠截图）。

    实机（沙漠古镇）：Win_InstanceConfig.Txt_Time 文案
      "倒计时177:03"  => 总分钟:秒（177*60+3），每秒递减。
    辅：Win_InstanceExitTime.Txt_ExitTime 模板
      "副本将在XX秒后关闭"（临近结束才有有效数字）。

    返回:
      {ok, remain_s, text, source, shown, error}

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "remain_s": None,
        "text": "",
        "source": "",
        "shown": False,
        "error": None,
    }
    try:
        from app.core.plg_ui import query_dlg_show
        from app.core.task_api import _aui_caption_from_ctrl
    except Exception as e:
        out["error"] = f"import: {e}"
        return out

    def _parse_cd(text: str) -> int | None:
        t = (text or "").strip()
        if not t:
            return None
        # 倒计时177:03 / 倒计时0：09秒 / 倒计时2:59:01
        m = re.search(r"倒计时\s*(\d+)[:：](\d{2})(?:[:：](\d{2}))?", t)
        if m:
            a, b, c = m.group(1), m.group(2), m.group(3)
            try:
                if c is not None:
                    return int(a) * 3600 + int(b) * 60 + int(c)
                # 长本常见：总分钟:秒（分钟可 >59）
                return int(a) * 60 + int(b)
            except Exception:
                return None
        # 副本将在123秒后关闭 / 还剩 123 秒
        m2 = re.search(r"(\d+)\s*秒", t)
        if m2:
            try:
                return int(m2.group(1))
            except Exception:
                return None
        m3 = re.search(r"(\d+)\s*分(?:钟)?\s*(\d+)?\s*秒?", t)
        if m3:
            try:
                mins = int(m3.group(1))
                sec = int(m3.group(2) or 0)
                return mins * 60 + sec
            except Exception:
                return None
        return None

    # primary: Win_InstanceConfig / Txt_Time
    try:
        q = query_dlg_show(session, "Win_InstanceConfig", log=log)
        out["shown"] = bool(getattr(q, "shown", False))
        dlg = int(getattr(q, "dlg_ptr", 0) or 0)
        if dlg:
            ctrl = aui_get_dlg_item(session, dlg, "Txt_Time", log=log) or 0
            if ctrl:
                cap = _aui_caption_from_ctrl(session, int(ctrl)) or ""
                out["text"] = str(cap)
                remain = _parse_cd(cap)
                if remain is not None:
                    out["ok"] = True
                    out["remain_s"] = int(remain)
                    out["source"] = "Win_InstanceConfig.Txt_Time"
                    return out
    except Exception as e:
        out["error"] = f"InstanceConfig: {e}"

    # secondary: exit-time dialog
    try:
        q2 = query_dlg_show(session, "Win_InstanceExitTime", log=log)
        dlg2 = int(getattr(q2, "dlg_ptr", 0) or 0)
        if dlg2:
            ctrl2 = aui_get_dlg_item(session, dlg2, "Txt_ExitTime", log=log) or 0
            if ctrl2:
                cap2 = _aui_caption_from_ctrl(session, int(ctrl2)) or ""
                if cap2:
                    out["text"] = str(cap2)
                remain2 = _parse_cd(cap2)
                # ignore template XX
                if remain2 is not None and "XX" not in str(cap2):
                    out["ok"] = True
                    out["remain_s"] = int(remain2)
                    out["source"] = "Win_InstanceExitTime.Txt_ExitTime"
                    out["shown"] = bool(getattr(q2, "shown", False))
                    return out
    except Exception as e:
        if not out.get("error"):
            out["error"] = f"InstanceExitTime: {e}"

    if not out.get("error"):
        out["error"] = "countdown text not found"
    return out


def format_instance_remain(remain_s: float | int | None) -> str:
    """Humanize remaining seconds as game-style total_minutes:SS. @author by ak"""
    if remain_s is None:
        return "?"
    try:
        sec = max(0, int(remain_s))
    except Exception:
        return "?"
    total_m, s = divmod(sec, 60)
    return f"{total_m}:{s:02d}"


def probe_hang_state_mem(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
) -> HangStateResult:
    """
    内存直读挂机状态（CECAutoPlay+0x08），不依赖 UI。

    @author by ak
    """
    log = log or (lambda _m: None)
    mem = resolve_cec_autoplay_rpm(session)
    out = HangStateResult(ok=False, on=None, source="mem_autoplay", signals=[], detail={"mem": mem})
    if not mem.get("ok"):
        out.error = str(mem.get("error") or "mem probe failed")
        out.source = "mem_error"
        return out
    running = mem.get("running")
    out.ok = True
    out.on = bool(running)
    out.source = "mem_cec_autoplay"
    out.signals = [
        f"CECAutoPlay+0x08={'1' if running else '0'}",
        f"ptr=0x{int(mem.get('autoplay') or 0):X}",
        f"mode={mem.get('mode')}({mem.get('mode_name') or autoplay_mode_name(mem.get('mode'))})",
        f"radius(+0x21)={mem.get('radius')}",
    ]
    return out


def probe_hang_state(
    session: GameAttachSession,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> HangStateResult:
    """
    Detect whether game 内挂/挂机 is currently ON.

    优先：内存 CECAutoPlay+0x08（Start/StopAutoPlay 真标志）。
    UI 仅作 recon 附件，不再作为主判定。

    @author by ak
    """
    log = log or (lambda _m: None)
    out = HangStateResult(ok=False, on=None, source="", signals=[], detail={})
    on_signals: list[str] = []
    hidden_exist: list[str] = []
    missing: list[str] = []
    ignored_shown: list[str] = []
    dual_rows: list[dict] = []
    try:
        # ---- 主路径：内存 ----
        mem_st = probe_hang_state_mem(session, log=log)
        out.detail["mem"] = (mem_st.detail or {}).get("mem") or {}
        if mem_st.ok and mem_st.on is not None:
            # 仍附带少量 recon，方便对照
            try:
                hs = _probe_host_states_hex(session)
                if hs:
                    out.detail["host_states"] = hs
            except Exception:
                pass
            out.ok = True
            out.on = bool(mem_st.on)
            out.source = mem_st.source
            out.signals = list(mem_st.signals or [])
            out.detail["note"] = (
                "挂机状态来自 CECAutoPlay+0x08 内存直读；"
                "与闭关修炼设置窗(AutoPlayFrame)是否打开无关"
            )
            return out

        # 内存失败才走下面 UI recon（结果仍尽量 unknown，避免误判）
        out.detail["mem_fallback"] = mem_st.error or mem_st.source
        hs = _probe_host_states_hex(session)
        if hs:
            out.detail["host_states"] = hs

        for name in HANG_STATE_DLG_CANDIDATES:
            try:
                dlg = get_game_ui_dlg(session, name, log=log) or 0
            except Exception as e:
                missing.append(f"{name}:err:{e}")
                continue
            if not dlg:
                missing.append(name)
                continue
            vis = _dlg_visible_dual(session, int(dlg), log=log)
            dual_rows.append({"name": name, **vis})
            if vis.get("visible"):
                if _is_hang_on_signal_dlg(name):
                    tag = name
                    if vis.get("aui_is_show") and not vis.get("is_dlg_show"):
                        tag = f"{name}:aui_show"
                    elif vis.get("is_dlg_show") and not vis.get("aui_is_show"):
                        tag = f"{name}:dlg_show"
                    on_signals.append(tag)
                else:
                    ignored_shown.append(name)
            else:
                hidden_exist.append(name)
        out.detail["dlg_dual"] = dual_rows
        out.detail["dlg_on_signals"] = list(on_signals)
        out.detail["dlg_hidden"] = list(hidden_exist)
        out.detail["dlg_missing"] = list(missing)
        out.detail["dlg_ignored_shown"] = list(ignored_shown)

        # full currently-shown list (recon: 开/关对比用)
        all_shown = _list_all_shown_dlgs(session, log=log)
        out.detail["all_shown"] = all_shown
        out.detail["all_shown_n"] = len(all_shown)

        # scan live dialog list — allowlisted hang windows
        try:
            from app.core.plg_ui import list_game_ui_dlg_names

            listed = list_game_ui_dlg_names(session, log=log)
            names = list(getattr(listed, "names", None) or [])
            out.detail["dlg_listed_n"] = len(names)
            # 只扫战斗操作/挂机，不扫 autoplay（闭关修炼）
            keys = ("fightmode", "fightoperation", "fightopr", "fighthelper", "settingopr")
            for nm in names:
                sn = str(nm)
                low = sn.lower()
                if not any(k in low for k in keys):
                    continue
                try:
                    dlg = get_game_ui_dlg(session, sn, log=lambda _m: None) or 0
                except Exception:
                    continue
                if not dlg:
                    continue
                vis = _dlg_visible_dual(session, int(dlg), log=lambda _m: None)
                if vis.get("visible"):
                    if _is_hang_on_signal_dlg(sn):
                        tag = f"listed:{sn}"
                        if tag not in on_signals and sn not in on_signals:
                            on_signals.append(tag)
                    else:
                        tag = f"ignored_shown:{sn}"
                        if tag not in ignored_shown:
                            ignored_shown.append(tag)
                else:
                    tag = f"listed_hidden:{sn}"
                    if tag not in hidden_exist:
                        hidden_exist.append(tag)
        except Exception as e:
            out.detail["dlg_list_err"] = str(e)
        out.detail["dlg_on_signals"] = list(on_signals)
        out.detail["dlg_hidden"] = list(hidden_exist)
        out.detail["dlg_ignored_shown"] = list(ignored_shown)
        out.detail["dlg_shown"] = list(on_signals)

        # control probes — checked / enable 差异
        ctrl_hits: list[dict] = []
        quiet = lambda _m: None
        for parent in HANG_BTN_PARENT_DLGS:
            try:
                dlg = get_game_ui_dlg(session, parent, log=quiet) or 0
            except Exception:
                dlg = 0
            if not dlg:
                continue
            parent_vis = _dlg_visible_dual(session, int(dlg), log=quiet)
            for cname in HANG_BTN_CTRL_CANDIDATES:
                try:
                    ctrl = aui_get_dlg_item(session, dlg, cname, log=quiet)
                except Exception:
                    ctrl = 0
                if not ctrl:
                    continue
                shown_c = _aui_obj_is_show(session, ctrl, log=quiet)
                enabled_c = None
                try:
                    enabled_c = bool(aui_obj_is_enable(session, ctrl, log=quiet))
                except Exception:
                    enabled_c = None
                checked = _read_aui_push_checked(session, ctrl)
                hit = {
                    "parent": parent,
                    "parent_shown": bool(parent_vis.get("visible")),
                    "parent_is_dlg_show": parent_vis.get("is_dlg_show"),
                    "parent_aui_show": parent_vis.get("aui_is_show"),
                    "ctrl": cname,
                    "ptr": int(ctrl) & 0xFFFFFFFF,
                    "ctrl_shown": shown_c,
                    "ctrl_enable": enabled_c,
                    "checked": checked,
                }
                ctrl_hits.append(hit)
                if checked is True:
                    on_signals.append(f"{parent}.{cname}:checked")
                # 某些版本挂机开时 Img_AutoPlay 仍 show，但 enable/checked 会变；
                # 仅 checked 作硬信号，show 只进 detail 供对比
        out.detail["controls"] = ctrl_hits

        # 战斗挂机 UI 深读（非闭关修炼 AutoPlay）
        fight_icon = _fight_hang_icon_state(session, log=log)
        out.detail["fight_hang_icon"] = {
            k: v for k, v in fight_icon.items() if k not in ("blob_hex",)
        }
        if fight_icon.get("blob_hex"):
            out.detail["fight_hang_icon"]["blob_hex_head"] = str(fight_icon["blob_hex"])[:64]
        glow = None
        try:
            glow = _sample_ctrl_glow(
                session,
                int(hwnd or 0),
                fight_icon.get("rect") if isinstance(fight_icon.get("rect"), dict) else None,
                log=log,
            )
        except Exception as e:
            glow = {"ok": False, "error": str(e)}
        if glow:
            out.detail["fight_hang_glow"] = glow

        # 兼容字段：标明 AutoPlay 是闭关修炼，不参与判定
        out.detail["note_autoplay"] = (
            "Win_AutoPlay* = 闭关修炼设置，不是 Alt+R 挂机；已从挂机信号中排除"
        )

        # --- hard ON signals（仅 FightMode / FightOperation）---
        if on_signals:
            # 二次过滤：防止 autoplay 漏网
            on_signals = [s for s in on_signals if "autoplay" not in str(s).lower()]
        if on_signals:
            out.ok = True
            out.on = True
            out.source = "ui_shown"
            out.signals = list(on_signals)
            return out

        # FightModeMinimize / FightOperationChoose 可见或 raw isshow
        for row in dual_rows:
            nm = str(row.get("name") or "")
            if "autoplay" in nm.lower():
                continue
            if int(row.get("raw_isshow") or 0) != 0 or row.get("visible"):
                if _is_hang_on_signal_dlg(nm):
                    out.ok = True
                    out.on = True
                    out.source = "fightmode_raw"
                    out.signals = [f"{nm}:visible"]
                    return out

        # 控件 checked
        if fight_icon.get("checked") is True:
            out.ok = True
            out.on = True
            out.source = "fight_icon_checked"
            out.signals = [f"{fight_icon.get('parent')}.{fight_icon.get('ctrl')}:checked"]
            return out

        # 像素：开=绿色；关=金/棕（用户线索，截图区域）
        if isinstance(glow, dict) and glow.get("ok"):
            gdom = float(glow.get("green_dom") or 0.0)
            gr = float(glow.get("green_ratio") or 0.0)
            goldr = float(glow.get("gold_ratio") or 0.0)
            mean = float(glow.get("mean") or 0.0)
            gv = float(glow.get("g") or 0.0)
            rv = float(glow.get("r") or 0.0)
            bv = float(glow.get("b") or 0.0)
            if gr >= 0.18 and gdom >= 10.0 and gv >= rv + 8.0 and gv >= bv + 8.0:
                out.ok = True
                out.on = True
                out.source = "fight_icon_green"
                out.signals = [
                    f"hang-icon green gr={gr:.2f} gdom={gdom:.1f} rgb=({rv:.0f},{gv:.0f},{bv:.0f})"
                ]
                return out
            if goldr >= 0.15 and gr <= 0.12 and gdom <= 8.0:
                out.ok = True
                out.on = False
                out.source = "fight_icon_gold_off"
                out.signals = [
                    f"hang-icon gold gr={gr:.2f} goldr={goldr:.2f} rgb=({rv:.0f},{gv:.0f},{bv:.0f})"
                ]
                return out
            if gv >= 110.0 and gv >= rv + 20.0 and gv >= bv + 20.0 and gr >= 0.12:
                out.ok = True
                out.on = True
                out.source = "fight_icon_green_soft"
                out.signals = [
                    f"hang-icon green-soft gr={gr:.2f} rgb=({rv:.0f},{gv:.0f},{bv:.0f})"
                ]
                return out
            out.detail["color_hint"] = (
                f"rgb=({rv:.0f},{gv:.0f},{bv:.0f}) gr={gr:.2f} goldr={goldr:.2f} "
                f"gdom={gdom:.1f} mean={mean:.1f} rect_src={fight_icon.get('rect_src')}"
            )

        # 无硬信号 → unknown（不把闭关修炼窗当开）
        if hidden_exist or ctrl_hits or fight_icon.get("ptr") or fight_icon.get("rect"):
            out.ok = True
            out.on = None
            out.source = "unknown_ui"
            out.signals = list(hidden_exist[:4])
            out.error = (
                "未识别到战斗挂机(Alt+R)开/关硬信号；"
                "请开/关挂机后对比 detail.fight_hang_icon / fight_hang_glow "
                "（闭关修炼 AutoPlay 已忽略）"
            )
            out.detail["note"] = out.error
            if ignored_shown:
                out.detail["ignored"] = list(ignored_shown[:6])
            return out

        # No hang dialogs resolved — unknown (do not guess)
        out.ok = True
        out.on = None
        out.source = "unknown"
        out.signals = []
        out.error = "no hang UI signals resolved"
        if ignored_shown:
            out.detail["note"] = (
                "only tip/unrelated UIs shown: " + ",".join(ignored_shown[:4])
            )
        return out
    except Exception as e:
        out.ok = False
        out.on = None
        out.source = "error"
        out.error = str(e)
        log(f"probe_hang_state err: {e}")
        return out


def format_hang_state(st: HangStateResult) -> str:
    """One-line hang state for logs. @author by ak"""
    if st.on is True:
        tag = "开"
    elif st.on is False:
        tag = "关"
    else:
        tag = "未知"
    sig = ",".join(st.signals[:4]) if st.signals else "-"
    return f"挂机={tag} src={st.source or '-'} sig={sig}"


# ============================================================
# 挂机快捷键读取：Alt+R 只是默认值，游戏内可自定义
# ============================================================
HANG_HOTKEY_DEFAULT = "Alt+R"
HANG_HOTKEY_TIP_DLG = "Win_AutoPlayTip"
HANG_HOTKEY_TIP_CTRL = "Img_AutoPlay"
HANG_HOTKEY_CAPTION_RE = re.compile(r"快捷键\s*[:：]\s*(.*)")
# AUI header wide-string caption pointer slots (same list as portal captions).
HANG_HOTKEY_CAPTION_OFFS: tuple[int, ...] = (
    0xB8, 0xBC, 0xC0, 0xB0, 0xA8, 0xC4, 0xD0, 0xD4,
)


def _aui_ctrl_caption(session, ctrl: int) -> str:
    """
    Best-effort wide-string caption from AUI header pointer fields.

    Reads the live control blob and follows each known caption pointer slot;
    returns the first readable UTF-16LE string (no remote GetText call).

    @author by ak
    """
    ctrl = int(ctrl or 0) & 0xFFFFFFFF
    if not ctrl:
        return ""
    blob = _read_remote_bytes(session, ctrl, 0xE0)
    if not blob or len(blob) < 0xE0:
        return ""
    for off in HANG_HOTKEY_CAPTION_OFFS:
        p = struct.unpack_from("<I", blob, off)[0]
        if not (0x10000 < p < 0x7FFE0000):
            continue
        raw = _read_remote_bytes(session, p, 96) or b""
        chars: list[str] = []
        for i in range(0, len(raw) - 1, 2):
            code = raw[i] | (raw[i + 1] << 8)
            if code == 0:
                break
            chars.append(chr(code))
        text = "".join(chars).strip()
        if text:
            return text
    return ""


def read_autoplay_tip_hotkey(
    session,
    *,
    log: LogFn | None = None,
) -> str:
    """
    Read the hang hotkey displayed on Win_AutoPlayTip.Img_AutoPlay.

    The hang shortcut (default Alt+R) is user-configurable in game settings;
    the tip window renders the active key on Img_AutoPlay as ``快捷键：<key>``
    (live-confirmed). Returns the key string (e.g. ``Alt+R`` / ``/``), or
    HANG_HOTKEY_DEFAULT when the tip is unavailable.

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        dlg = int(get_game_ui_dlg(session, HANG_HOTKEY_TIP_DLG, log=log) or 0) & 0xFFFFFFFF
        if not dlg:
            return HANG_HOTKEY_DEFAULT
        ctrl = aui_get_dlg_item(session, dlg, HANG_HOTKEY_TIP_CTRL, log=log)
        if not ctrl:
            return HANG_HOTKEY_DEFAULT
        cap = _aui_ctrl_caption(session, int(ctrl))
        if not cap:
            return HANG_HOTKEY_DEFAULT
        m = HANG_HOTKEY_CAPTION_RE.search(cap)
        hotkey = (m.group(1) or "").strip() if m else ""
        log(f"hang hotkey from tip: {hotkey!r} (caption={cap!r})")
        return hotkey or HANG_HOTKEY_DEFAULT
    except Exception as e:
        log(f"read_autoplay_tip_hotkey err: {e}")
        return HANG_HOTKEY_DEFAULT



def ensure_hang_desired(
    session: GameAttachSession,
    desired_on: bool,
    *,
    pid: int | None = None,
    hwnd: int = 0,
    settle_s: float = 0.55,
    max_tries: int = 2,
    log: LogFn | None = None,
) -> tuple[bool, str]:
    """
    把当前号内挂同步到 desired_on（True=开 / False=关）。

    - 已达目标：跳过热键
    - 未达目标：后台按 tip 显示的挂机快捷键切换并复检（最多 max_tries 次）
    - 状态未知：不盲切，返回失败

    @author by ak
    """
    log = log or (lambda _m: None)
    want = bool(desired_on)
    want_s = "开" if want else "关"
    try:
        st0 = probe_hang_state_mem(session, log=log)
    except Exception as e:
        return False, f"读挂机失败: {e}"
    if not st0.ok or st0.on is None:
        return False, f"挂机状态未知，无法同步到{want_s}"
    if bool(st0.on) == want:
        log(f"hang: already {want_s} skip")
        return True, f"已是{want_s}，跳过"

    pid_i = int(pid or getattr(session, "pid", 0) or 0)
    if not pid_i:
        return False, "无 pid，无法发挂机热键"
    hwnd_i = int(hwnd or getattr(session, "hwnd", 0) or 0)

    try:
        from app.core.bg_input import press_bg_chord_once
    except Exception as e:
        return False, f"热键模块不可用: {e}"

    hotkey = read_autoplay_tip_hotkey(session, log=log)

    last_msg = ""
    for i in range(max(1, int(max_tries))):
        try:
            r = press_bg_chord_once(
                pid_i,
                hotkey,
                hwnd=hwnd_i,
                hold_ms=55,
                allow_softsend=False,
                log=log,
            )
            ok_key = bool(r.get("ok"))
            last_msg = f"{hotkey}#{i + 1}={'OK' if ok_key else (r.get('error') or 'fail')}"
            log(f"hang: {last_msg} want={want_s}")
        except Exception as e:
            last_msg = f"{hotkey} 异常: {e}"
            log(f"hang: {last_msg}")
            return False, last_msg
        time.sleep(max(0.2, float(settle_s)))
        try:
            st = probe_hang_state_mem(session, log=log)
        except Exception as e:
            return False, f"复检失败: {e}"
        if st.ok and st.on is not None and bool(st.on) == want:
            return True, f"已同步到{want_s}（{last_msg}）"
    try:
        stf = probe_hang_state_mem(session, log=log)
        cur = "开" if stf.on is True else ("关" if stf.on is False else "未知")
    except Exception:
        cur = "未知"
    return False, f"未能同步到{want_s}（现={cur}；{last_msg}）"



def _award_btn_name(index_1based: int) -> str:
    """
    Control name for flourish chest i (1..4) -> Bnt_Bonus01..04.

    Client format is Bnt_Bonus0%d with ebx starting at 1 (typo: Bnt not Btn).

    @author by ak
    """
    return f"Bnt_Bonus0{int(index_1based)}"


def _award_btn_ptr_cached(session, dlg_ptr: int, index_1based: int) -> int:
    """
    Read Bnt_Bonus0N cached ptr from dialog (dlg+0x2C0 + (n-1)*4).

    @author by ak
    """
    idx = int(index_1based) - 1
    if idx < 0 or idx >= DAILY_AWARD_BTN_COUNT:
        return 0
    addr = (
        (int(dlg_ptr) & 0xFFFFFFFF)
        + DAILY_AWARD_BTN_BASE
        + idx * DAILY_AWARD_BTN_STRIDE
    )
    pm = getattr(session, "pm", None)
    if pm is None:
        return 0
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(pm.process_handle, addr, 4)
        return struct.unpack_from("<I", raw, 0)[0]
    except Exception:
        return 0


def _award_claimed_flag(session, dlg_ptr: int, index_1based: int) -> bool | None:
    """
    Read per-tier claimed byte at dlg+0x320+(n-1). 0 = already claimed.

    @author by ak
    """
    idx = int(index_1based) - 1
    if idx < 0 or idx >= DAILY_AWARD_BTN_COUNT:
        return None
    addr = (int(dlg_ptr) & 0xFFFFFFFF) + DAILY_AWARD_CLAIMED_BASE + idx
    pm = getattr(session, "pm", None)
    if pm is None:
        return None
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(pm.process_handle, addr, 1)
        # 0 => claimed (handler skips claim packet); non-zero => can claim.
        return int(raw[0]) == 0
    except Exception:
        return None


def ensure_activity_dlg_shown(session, *, log: LogFn | None = None) -> tuple[int, str]:
    """
    Open Win_InstanceEndlessList (hosts 活跃宝箱 on Rdo_Vibrancy tab).

    @author by ak
    """
    log = log or (lambda _m: None)
    for name in DAILY_ACTIVITY_DLG_CANDIDATES:
        dlg = ensure_dlg_shown(session, name, log=log)
        if dlg:
            return int(dlg) & 0xFFFFFFFF, name
    return 0, ""


def _note_live_va(session, note_va: int) -> int:
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        return 0
    return base + (int(note_va) - DEFAULT_IMAGE_BASE)


def switch_activity_vibrancy_tab(
    session,
    dlg_ptr: int,
    *,
    hwnd: int = 0,
    allow_cursor: bool = False,
    log: LogFn | None = None,
) -> bool:
    """
    Switch Win_InstanceEndlessList to 活跃度 tab (Rdo_Vibrancy).

    Prefer game command handler 0x9CA5D0; fall back to UI click.

    @author by ak
    """
    log = log or (lambda _m: None)
    dlg = int(dlg_ptr) & 0xFFFFFFFF
    if not dlg:
        return False
    # 1) Native tab command (reliable; no click coords).
    try:
        if session_supports(session, "activity.ui"):
            va = _note_live_va(session, NOTE_VA_RDO_VIBRANCY_CMD)
            if va:
                remote_call_thiscall_x86(
                    int(session.pid),
                    va,
                    dlg,
                    [0],
                    stack_cleanup=4,
                    timeout_ms=3000,
                )
                time.sleep(0.35)
                log("activity claim: Rdo_Vibrancy cmd ok")
                return True
    except Exception as e:
        log(f"activity claim: Rdo_Vibrancy cmd err: {e}")
    # 2) Click radio control.
    try:
        rdo = aui_get_dlg_item(session, dlg, "Rdo_Vibrancy", log=log)
        if not rdo:
            # Cached ptr at dlg+0x30C
            pm = getattr(session, "pm", None)
            if pm is not None:
                import pymem.memory

                raw = pymem.memory.read_bytes(
                    pm.process_handle,
                    dlg + DAILY_RDO_VIBRANCY_OFF,
                    4,
                )
                rdo = struct.unpack_from("<I", raw, 0)[0]
        if not rdo:
            log("activity claim: Rdo_Vibrancy null")
            return False
        rect = read_aui_ctrl_rect(session, rdo, name="Rdo_Vibrancy", log=log)
        if not rect.ok:
            log(f"activity claim: Rdo_Vibrancy bad rect {rect.error}")
            return False
        pt = human_point_from_rect(rect) or rect.center
        ok = click_client_bg(
            session,
            hwnd,
            int(pt[0]),
            int(pt[1]),
            prefer_bridge=True,
            prefer_post=True,
            allow_cursor=bool(allow_cursor),
            humanize=True,
            log=log,
        )
        if ok:
            time.sleep(0.35)
            log("activity claim: Rdo_Vibrancy click ok")
        return bool(ok)
    except Exception as e:
        log(f"activity claim: Rdo_Vibrancy click err: {e}")
        return False


def claim_flourish_awards_packet(
    session,
    *,
    points: int | None = None,
    log: LogFn | None = None,
) -> dict:
    """
    Claim flourish chests via client packet 0x8F (cdecl 0xCCBD20).

    Award id == tier threshold (20/35/50/70). No UI required.
    Safe to re-send already-claimed tiers (server rejects).

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "method": "packet",
        "claimed": [],
        "skipped": [],
        "error": None,
    }
    if not session_supports(session, "activity.read"):
        out["error"] = "claim packet blocked: unknown client build"
        return out
    pts = int(points) if points is not None else None
    if pts is None:
        live = read_flourish_points(session, log=log)
        pts = int(live) if live is not None else 0
    va = _note_live_va(session, NOTE_VA_CLAIM_FLOURISH_AWARD)
    if not va:
        out["error"] = "no module_base for claim packet"
        return out
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        out["error"] = "no pid"
        return out
    any_ok = False
    for i, tier in enumerate(FLOURISH_AWARD_REPU, start=1):
        name = _award_btn_name(i)
        if pts < int(tier):
            out["skipped"].append(
                {"name": name, "reason": "locked", "tier": int(tier), "points": pts}
            )
            continue
        try:
            remote_call_cdecl_x86(pid, va, [int(tier) & 0xFFFFFFFF])
            out["claimed"].append(
                {"name": name, "tier": int(tier), "award_id": int(tier), "ok": True}
            )
            any_ok = True
            log(f"activity claim packet tier={tier} ok")
            human_delay_s(0.25, 0.5)
        except Exception as e:
            out["skipped"].append(
                {"name": name, "reason": f"packet err: {e}", "tier": int(tier)}
            )
            log(f"activity claim packet tier={tier} err: {e}")
    out["ok"] = True
    out["any_click"] = any_ok
    out["points"] = pts
    return out


def claim_daily_activity_awards(
    session,
    *,
    hwnd: int = 0,
    btn_count: int = DAILY_AWARD_BTN_COUNT,
    allow_cursor: bool = False,
    log: LogFn | None = None,
    passes: int = 2,
    points: int | None = None,
) -> dict:
    """
    Claim 活跃宝箱 (20/35/50/70).

    Prefer packet path (no UI / no wrong panel). Fall back to:
      open Win_InstanceEndlessList -> Rdo_Vibrancy -> click Bnt_Bonus01..04.

    @author by ak
    """
    log = log or (lambda _m: None)
    pts = None if points is None else int(points)
    if pts is None:
        live = read_flourish_points(session, log=log)
        if live is not None:
            pts = int(live)

    # --- Primary: packet claim (avoids opening 无尽逍遥 detail panel) ---
    pkt = claim_flourish_awards_packet(session, points=pts, log=log)
    if pkt.get("ok") and pkt.get("any_click"):
        log(
            f"activity claim awards method=packet "
            f"claimed={len(pkt.get('claimed') or [])} "
            f"skipped={len(pkt.get('skipped') or [])}"
        )
        return {
            "ok": True,
            "dlg": "",
            "dlg_name": "",
            "method": "packet",
            "clicked": list(pkt.get("claimed") or []),
            "skipped": list(pkt.get("skipped") or []),
            "any_click": True,
            "error": None,
            "points": pkt.get("points", pts),
        }

    # --- Fallback: UI path ---
    out: dict = {
        "ok": False,
        "dlg": DAILY_ACTIVITY_DLG,
        "dlg_name": "",
        "method": "ui",
        "clicked": [],
        "skipped": list(pkt.get("skipped") or []) if isinstance(pkt, dict) else [],
        "error": None,
        "packet": pkt,
    }
    dlg, dlg_name = ensure_activity_dlg_shown(session, log=log)
    if not dlg:
        out["error"] = (
            "无法打开 "
            f"{DAILY_ACTIVITY_DLG} "
            f"(packet also empty: {pkt.get('error')})"
        )
        return out
    out["dlg"] = dlg_name or DAILY_ACTIVITY_DLG
    out["dlg_name"] = dlg_name
    time.sleep(0.35)
    switch_activity_vibrancy_tab(
        session,
        dlg,
        hwnd=hwnd,
        allow_cursor=allow_cursor,
        log=log,
    )
    time.sleep(0.3)
    n = max(1, min(int(btn_count), DAILY_AWARD_BTN_COUNT))
    any_click = False
    n_pass = max(1, min(int(passes), 4))
    for pass_i in range(n_pass):
        if pass_i > 0:
            time.sleep(0.3)
            dlg2, name2 = ensure_activity_dlg_shown(session, log=log)
            if dlg2:
                dlg = dlg2
                if name2:
                    out["dlg_name"] = name2
                    out["dlg"] = name2
                switch_activity_vibrancy_tab(
                    session,
                    dlg,
                    hwnd=hwnd,
                    allow_cursor=allow_cursor,
                    log=log,
                )
                time.sleep(0.25)
        pass_clicks = 0
        for i in range(1, n + 1):
            name = _award_btn_name(i)
            ctrl = aui_get_dlg_item(session, dlg, name, log=log)
            if not ctrl:
                ctrl = _award_btn_ptr_cached(session, dlg, i)
            if not ctrl:
                if pass_i == n_pass - 1:
                    out["skipped"].append({"name": name, "reason": "null"})
                continue
            claimed = _award_claimed_flag(session, dlg, i)
            if claimed is True:
                if pass_i == n_pass - 1:
                    out["skipped"].append({"name": name, "reason": "claimed"})
                continue
            enabled = aui_obj_is_enable(session, ctrl, log=log)
            tier_need = (
                FLOURISH_AWARD_REPU[i - 1]
                if i - 1 < len(FLOURISH_AWARD_REPU)
                else None
            )
            unlocked = (
                pts is not None and tier_need is not None and pts >= int(tier_need)
            )
            if not enabled and not unlocked:
                if pass_i == n_pass - 1:
                    rect = read_aui_ctrl_rect(session, ctrl, name=name, log=log)
                    out["skipped"].append(
                        {
                            "name": name,
                            "reason": "disabled",
                            "rect": rect.to_dict() if rect.ok else None,
                            "tier": tier_need,
                        }
                    )
                continue
            rect = read_aui_ctrl_rect(session, ctrl, name=name, log=log)
            if not rect.ok:
                if pass_i == n_pass - 1:
                    out["skipped"].append(
                        {"name": name, "reason": rect.error or "bad rect"}
                    )
                continue
            pt = human_point_from_rect(rect) or rect.center
            ok = click_client_bg(
                session,
                hwnd,
                int(pt[0]),
                int(pt[1]),
                prefer_bridge=True,
                prefer_post=True,
                allow_cursor=bool(allow_cursor),
                humanize=True,
                log=log,
            )
            out["clicked"].append(
                {
                    "name": name,
                    "ok": ok,
                    "xy": list(pt),
                    "pass": pass_i + 1,
                    "enabled": enabled,
                    "unlocked": unlocked,
                    "tier": tier_need,
                }
            )
            if ok:
                any_click = True
                pass_clicks += 1
                human_delay_s(0.45, 0.9)
        if pass_i == 0 and pass_clicks == 0:
            continue
        if pass_i > 0 and pass_clicks == 0:
            break
    out["ok"] = True
    out["any_click"] = any_click
    log(
        f"activity claim awards method=ui dlg={out.get('dlg_name')!r} "
        f"clicked={len(out['clicked'])} skipped={len(out['skipped'])} "
        f"any_click={any_click}"
    )
    return out


def _pid_alive(pid: int | None) -> bool:
    """
    True if game process still exists.

    @author by ak
    """
    if not pid:
        return True
    try:
        import psutil

        return bool(psutil.pid_exists(int(pid)))
    except Exception:
        try:
            import ctypes

            k = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED
            if not h:
                return False
            k.CloseHandle(h)
            return True
        except Exception:
            return True


def _is_process_gone_error(err: object) -> bool:
    """
    Heuristic: memory/process API failures after the game client died.

    @author by ak
    """
    s = str(err or "").lower()
    if not s:
        return False
    # Common post-crash spam: VirtualAllocEx/OpenProcess access denied (err=5),
    # RPM partial copy (err=299).
    if "err=5" in s and (
        "virtualalloc" in s or "openprocess" in s or "writeprocess" in s
    ):
        return True
    if "err=299" in s or "readprocessmemory failed" in s:
        return True
    if "virtualallocex failed" in s or "openprocess failed" in s:
        return True
    return any(
        k in s
        for k in (
            "process has exited",
            "no such process",
            "process not found",
            "handle is invalid",
            "invalid handle",
        )
    )


def wait_map_ready(
    session: GameAttachSession,
    cfg: ActivityConfig,
    *,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
    require_dungeon: bool = True,
) -> bool:
    """
    过图等待：进本/切图后场景坐标可读且短暂稳定再继续。

    - 至少等待 qiegao_map_load_min_s
    - 轮询 read_scene_state，坐标连续 stable_s 秒变化 < 0.8m
    - 超时返回 False（调用方可选择仍继续）

    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    min_s = max(0.0, float(getattr(cfg, "qiegao_map_load_min_s", 8.0) or 0.0))
    timeout = max(min_s, float(getattr(cfg, "qiegao_map_load_timeout_s", 60.0) or 60.0))
    stable_need = max(0.3, float(getattr(cfg, "qiegao_map_stable_s", 1.5) or 1.5))
    gate = getattr(cfg, "city_gate", "any_city")
    t0 = time.time()
    deadline = t0 + timeout
    stable_t0: float | None = None
    last_pos: tuple[float, float, float] | None = None
    last_sid: int | None = None
    status(f"过图等待中… 最少 {min_s:.0f}s / 超时 {timeout:.0f}s")
    log(f"activity map_ready start min={min_s:.1f}s timeout={timeout:.1f}s stable={stable_need:.1f}s")
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
        elapsed = time.time() - t0
        sid, pos, label = read_scene_state(session, fresh=True, log=log)
        pos_ok = pos is not None and len(pos) >= 3
        dungeon_ok = True
        if require_dungeon:
            dungeon_ok = is_dungeon_scene(sid, label, gate=gate)
        if pos_ok and dungeon_ok:
            if last_pos is not None:
                try:
                    dx = float(pos[0]) - float(last_pos[0])
                    dz = float(pos[2]) - float(last_pos[2])
                    step = math.hypot(dx, dz)
                except Exception:
                    step = 999.0
                same_scene = last_sid is None or sid is None or int(sid) == int(last_sid)
                if same_scene and step < 0.8:
                    if stable_t0 is None:
                        stable_t0 = time.time()
                    if (time.time() - stable_t0) >= stable_need and elapsed >= min_s:
                        msg = (
                            f"过图完成 scene={label or sid} "
                            f"pos=({pos[0]:.1f},{pos[1]:.1f},{pos[2]:.1f}) "
                            f"用时 {elapsed:.1f}s"
                        )
                        status(msg)
                        log(f"activity map_ready ok {msg}")
                        return True
                else:
                    stable_t0 = time.time()
            else:
                stable_t0 = time.time()
            last_pos = (float(pos[0]), float(pos[1]), float(pos[2]))
            last_sid = int(sid) if sid is not None else None
        else:
            stable_t0 = None
            last_pos = None
            why = []
            if not pos_ok:
                why.append("坐标未就绪")
            if require_dungeon and not dungeon_ok:
                why.append(f"未在副本({label or sid})")
            status(
                f"过图等待… {elapsed:.0f}/{timeout:.0f}s "
                + ("/".join(why) if why else "")
            )
        if stop_event is not None:
            if stop_event.wait(0.5):
                return False
        else:
            time.sleep(0.5)
    status(f"过图等待超时({timeout:.0f}s)，仍继续")
    log("activity map_ready timeout")
    return False


def wait_enter_dungeon(
    session,
    cfg: ActivityConfig,
    *,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
) -> bool:
    """
    Wait until not in city (entered instance).

    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    deadline = time.time() + float(cfg.enter_timeout_s)
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
        pid = getattr(session, "pid", None)
        if not _pid_alive(pid):
            status(f"游戏进程已退出 pid={pid}")
            return False
        try:
            # Must-live: enter detection cannot recycle header/hub stale city.
            sid, _pos, label = read_scene_state(session, fresh=True, log=log)
        except Exception as e:
            if _is_process_gone_error(e) or not _pid_alive(pid):
                status(f"游戏进程已退出 pid={pid}")
                return False
            raise
        if is_dungeon_scene(sid, label, gate=cfg.city_gate):
            status(f"已进本 {label} scene={sid}")
            return True
        status(f"等待进本… {label} scene={sid}")
        if not _sleep_interruptible(1.0, stop_event):
            return False
    return False


def wait_return_city(
    session,
    cfg: ActivityConfig,
    *,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
    on_tick: Callable | None = None,
) -> bool:
    """
    Wait until back in city gate.

    on_tick is a low-frequency dungeon-only unstick observation callback invoked
    once per DUNGEON_UNSTICK_POLL_S with (session, scene_id, scene_label, pos).
    Activity/qiegao callers leave it None so the loop keeps its original poll.

    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    deadline = time.time() + float(cfg.wait_return_city_s)
    poll = max(1.0, float(getattr(cfg, "return_poll_s", 15.0) or 15.0))
    # Dungeon-only unstick tick: when on_tick is set the loop wakes each second
    # to observe 纯站街 while the normal poll drives status/return cadence.
    tick_every = float(DUNGEON_UNSTICK_POLL_S) if on_tick is not None else 0.0
    last_status = 0.0
    next_tick = 0.0
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
        pid = getattr(session, "pid", None)
        if not _pid_alive(pid):
            status(f"游戏进程已退出 pid={pid}")
            return False
        try:
            sid, pos, label = read_scene_state(session, fresh=True, log=log)
        except Exception as e:
            if _is_process_gone_error(e) or not _pid_alive(pid):
                status(f"游戏进程已退出 pid={pid}")
                return False
            raise
        if is_city_scene(sid, label, cfg.city_gate):
            status(f"已回城 {label} scene={sid}")
            return True
        now = time.time()
        if on_tick is not None and now >= next_tick:
            next_tick = now + tick_every
            try:
                on_tick(session, scene_id=sid, scene_label=label, pos=pos)
            except Exception:
                pass
            if stop_event is not None and stop_event.is_set():
                return False
        if on_tick is None or now - last_status >= poll:
            status(f"等待回城… {label} scene={sid}")
            last_status = now
        if not _sleep_interruptible(tick_every if on_tick is not None else poll, stop_event):
            return False
    return False


class DungeonUnstickGuard:
    """
    Pure-stationing (纯站街) unstick state machine for ordinary dungeon mode.

    Only active while cfg.mode=="dungeon". Rules are keyed by the auto-dungeon
    config's cfg.instance_id (never the map scene_id). Trigger requires all of:
      - host inside the rule's stuck zone (horizontal <= radius)
      - horizontal position still for DUNGEON_UNSTICK_STILL_S
      - no host self-dealt damage (native self_damage_seq unchanged)
    Attack pose is NOT a stillness signal (the hang casts skills even with no
    monsters around); only position motion and self-dealt damage reset the
    window. After the window completes, retained rules pathfind directly.

    Combat reads go through the read-only bridge monitor; if the monitor cannot
    be armed or read (unsupported build / hook install failure), the guard fails
    closed and never unsticks. State is bound to one dungeon runner lifetime.

    @author by ak
    """

    def __init__(
        self,
        *,
        instance_id: int,
        on_event: Callable | None = None,
        log: LogFn | None = None,
        stop_event: threading.Event | None = None,
        hwnd: int = 0,
        use_bridge: bool = True,
    ):
        self.instance_id = int(instance_id or 0)
        self._rule = DUNGEON_UNSTICK_RULES.get(self.instance_id)
        # 卡点 zone 列表（规则用 zones 数组描述）。from_entry 卡点 = 进本默认区，
        # 覆盖「进本到 next_zone 之前」的任意位置；其余卡点用 stuck(+可选
        # escape/radius/next_zone)。
        self._zones: list[dict] = []
        if self._rule is not None:
            for z in self._rule.get("zones") or []:
                if z.get("from_entry"):
                    entry: dict = {"from_entry": True}
                    if z.get("next_zone") is not None:
                        entry["next_zone"] = int(z["next_zone"])
                    self._zones.append(entry)
                    continue
                entry = {"stuck": tuple(z["stuck"])}
                if z.get("escape"):
                    entry["escape"] = tuple(z["escape"])
                if z.get("radius"):
                    entry["radius"] = float(z["radius"])
                if z.get("next_zone") is not None:
                    entry["next_zone"] = int(z["next_zone"])
                if z.get("no_monster"):
                    entry["no_monster"] = True
                self._zones.append(entry)
        self._emit_event = on_event or (lambda _p, _m, **_kw: None)
        self._log = log or (lambda _m: None)
        self._stop = stop_event
        self._hwnd = int(hwnd or 0)
        self._use_bridge = bool(use_bridge)
        self._armed = False
        self._arm_failed_until = 0.0
        self._moving = False
        self._cooldown_until = 0.0
        self._last_scene_id: int | None = None
        self._last_pos: tuple[float, float, float] | None = None
        self._last_damage_seq: int | None = None
        self._still_t0: float | None = None
        # 当前所在卡点 zone 下标（-1 = 不在任何卡点）。
        self._zone_idx: int = -1
        # 进本默认区（from_entry）：已到达 next_zone 后置 True，之后不再关注该区。
        self._entry_reached: bool = False
        # 1928 组队跟随守护：周期性检查跟随状态（掉了就补）的节流时间。
        self._follow_check_until: float = 0.0
        # 低频诊断（skip）事件节流，避免战斗信号瞬时不可读时刷屏。
        self._skip_until: float = 0.0
        # 卡队检测：追踪自己位置与静止持续时间。
        self._stuck_last_own: tuple[float, float, float] | None = None
        self._stuck_still_since: float | None = None
        self._stuck_last_toggle: float = 0.0

    # -- geometry helpers ----------------------------------------------------

    @staticmethod
    def _hz(a, b) -> float | None:
        """Horizontal XZ distance, or None on bad input. @author by ak"""
        try:
            return float(
                math.hypot(float(a[0]) - float(b[0]), float(a[2]) - float(b[2]))
            )
        except Exception:
            return None

    def _resolve_zone(self, pos3) -> int:
        """Index of the stuck zone the position is inside, or -1.

        先匹配具体卡点（stuck）；无命中时若存在进本默认区（from_entry）且尚未
        到达其 next_zone，则返回该默认区。

        @author by ak
        """
        for idx, zone in enumerate(self._zones):
            if zone.get("from_entry"):
                continue
            radius = float(zone.get("radius", DUNGEON_UNSTICK_RADIUS_M))
            dist = self._hz(pos3, zone["stuck"])
            if dist is not None and dist <= radius:
                return idx
        if self._entry_reached:
            return -1
        for idx, zone in enumerate(self._zones):
            if zone.get("from_entry"):
                return idx
        return -1

    def _maybe_mark_entry_reached(self, pos3) -> None:
        """from_entry 默认区：一旦进入 next_zone 半径即视为已到达，之后不再关注。@author by ak"""
        if self._entry_reached:
            return
        for zone in self._zones:
            if not zone.get("from_entry") or zone.get("next_zone") is None:
                continue
            try:
                next_pt = tuple(self._zones[int(zone["next_zone"])]["stuck"])
            except Exception:
                continue
            dist = self._hz(pos3, next_pt)
            if dist is not None and dist <= DUNGEON_UNSTICK_RADIUS_M:
                self._entry_reached = True
                return

    def reset_run(self) -> None:
        """每次进本开始时调用：重置跨趟状态（进本默认区等）。@author by ak"""
        self._entry_reached = False
        self._zone_idx = -1
        self._last_pos = None
        self._reset_window()

    def has_rule(self) -> bool:
        """True when the configured instance id has an unstick rule. @author by ak"""
        return self._rule is not None

    # -- lifecycle ----------------------------------------------------------

    def _emit(self, phase: str, message: str, ok: bool = True, **detail) -> None:
        detail.setdefault("instance_id", int(self.instance_id))
        # UI 的 _apply_event 已打印「自动副本 [phase] …」，这里不再让 runner 的
        # log 再打一条「activity [phase] …」，避免同一事件在日志里重复。
        detail.setdefault("log_event", False)
        try:
            self._emit_event(phase, message, ok=ok, **detail)
        except Exception:
            pass

    def _emit_skip(self, message: str, **detail) -> None:
        """Throttled diagnostic skip event (~30s). @author by ak"""
        now = time.monotonic()
        if now < self._skip_until:
            return
        self._skip_until = now + 30.0
        self._emit("dungeon_unstick_skip", message, ok=False, **detail)

    def _reset_window(self, combat=None) -> None:
        """Clear the stillness window and snapshot the self-damage baseline.

        Attack pose (attack_seq) is intentionally NOT a stillness signal: the
        hang casts skills even with no monsters around, so only position motion
        and self-dealt damage reset the pure-standing window.

        @author by ak
        """
        self._still_t0 = None
        if combat is not None:
            self._last_damage_seq = int(getattr(combat, "self_damage_seq", 0))

    def _rearm(self, *, scene_id: int | None) -> None:
        """Reset window + cooldown; re-install counters on scene change. @author by ak"""
        if (
            self._last_scene_id is not None
            and scene_id is not None
            and int(scene_id) != int(self._last_scene_id)
        ):
            self._armed = False
        self._cooldown_until = 0.0
        self._last_scene_id = scene_id
        self._last_pos = None
        self._zone_idx = -1
        self._reset_window()

    def _ensure_armed(self, session) -> bool:
        """Install the native combat counters once per scene. @author by ak"""
        if self._armed:
            return True
        if self._rule is None:
            return False
        now = time.monotonic()
        if now < self._arm_failed_until:
            return False
        if not self._use_bridge:
            self._arm_failed_until = now + 300.0
            self._emit(
                "dungeon_unstick_error",
                f"副本纠偏需桥接但 use_bridge=off，失败关闭 inst={self.instance_id}",
                ok=False,
            )
            return False
        try:
            from app.core.combat_monitor import arm_combat_monitor

            armed = arm_combat_monitor(session, log=self._log)
        except Exception as e:
            self._log(f"dungeon unstick arm error: {e}")
            armed = None
        if armed is None or not armed.ok or not armed.resolved:
            self._arm_failed_until = now + 300.0
            self._emit(
                "dungeon_unstick_error",
                "战斗监视不可用（攻击子系统未解析），副本纠偏失败关闭"
                + (f"：{armed.error}" if armed is not None else ""),
                ok=False,
            )
            return False
        self._armed = True
        self._reset_window(armed)
        self._last_pos = None
        self._last_scene_id = None
        self._emit(
            "dungeon_unstick_armed",
            f"副本纠偏已武装 inst={self.instance_id}（{self._rule['name']}）",
            ok=True,
            rule_name=self._rule["name"],
        )
        return True

    def disarm(self, session) -> None:
        """Release counters + temp follow state when the runner stops. @author by ak"""
        self._armed = False
        self._moving = False
        self._cooldown_until = 0.0
        self._last_scene_id = None
        self._last_pos = None
        self._zone_idx = -1
        self._reset_window()
        if session is not None:
            try:
                from app.core.combat_monitor import disarm_combat_monitor

                disarm_combat_monitor(session, log=self._log)
            except Exception:
                pass

    # -- host state ---------------------------------------------------------

    def _host_dead(self, session) -> bool:
        """Dead check through the shared hang death state machine. @author by ak"""
        try:
            from app.core.hang_settings import (
                get_hang_dead_state,
                refresh_hang_dead_state,
            )

            cached = get_hang_dead_state(session)
            if cached is not None:
                return bool(cached)
            return bool(
                refresh_hang_dead_state(session, log=lambda _m: None, force=True)
            )
        except Exception:
            return False

    def _has_nearby_monster(self, session, pos3) -> bool:
        """附近是否有怪（no_monster 卡点用）。纯 RPM 探测，无 CRT/远程调用。

        走 probe_nearby_class2_rpm：CECNPC 哈希表按 CECNPCMonster vftable 过滤，
        只读内存。探测失败时保守按"有怪"处理（不误脱困）。

        @author by ak
        """
        try:
            from app.core.hang_settings import probe_nearby_class2_rpm

            result = probe_nearby_class2_rpm(
                int(getattr(session, "pid", 0) or 0),
                tuple(pos3),
                radius=float(DUNGEON_UNSTICK_MONSTER_RADIUS_M),
            )
            if result.get("known") is False:
                return True
            return bool(result.get("has_monster"))
        except Exception:
            return True

    # -- observation --------------------------------------------------------

    def _follow_keepalive(self, session, pos=None) -> None:
        """组队跟随守护：卡队检测（1928 / 6230）。

        自己停留 > 5s 且 AOI 中有队友 > 15m 时，取消一次跟随重开。
        不依赖跟随当前状态，不管开没开都执行。

        @author by ak
        """
        if self._rule is None or not self._rule.get("follow_keepalive"):
            return
        now = time.monotonic()
        if now < self._follow_check_until:
            return
        self._follow_check_until = now + 5.0

        if not (isinstance(pos, (tuple, list)) and len(pos) >= 3):
            return
        pos3 = (float(pos[0]), float(pos[1]), float(pos[2]))

        # 判定因子 1：自己是否停留 > 5s。
        prev = self._stuck_last_own
        if prev is not None:
            moved = math.hypot(
                float(pos3[0]) - float(prev[0]),
                float(pos3[2]) - float(prev[2]),
            )
            if moved > 0.5:
                self._stuck_still_since = None
        self._stuck_last_own = pos3

        if self._stuck_still_since is None:
            self._stuck_still_since = now
            return

        still_dur = now - self._stuck_still_since
        if still_dur < 5.0:
            return

        # 判定因子 2：AOI 中是否有队友 > 15m。
        try:
            from app.core.team_ops import list_party_members
            from app.core.plg_objects import CLASS_PLAYER, list_class_objects

            party = list_party_members(session, fresh=True, log=lambda _m: None)
            if not party or len(party) < 2:
                return
            names: set[str] = set()
            for member in party:
                if bool(member.get("is_self")):
                    continue
                name = str(member.get("name") or "").strip()
                if name:
                    names.add(name)
            if not names:
                return

            def _name_key(v):
                return "".join(str(v or "").split()).strip().casefold()

            wanted_keys = {_name_key(n) for n in names}
            objects = list_class_objects(
                session,
                CLASS_PLAYER,
                radius=60.0,
                limit=96,
                read_name=True,
                read_tid=False,
                log=lambda _m: None,
            )
            has_far = False
            for obj in objects or []:
                name = str(getattr(obj, "name", "") or "").strip()
                if not name or _name_key(name) not in wanted_keys:
                    continue
                x = getattr(obj, "x", None)
                z = getattr(obj, "z", None)
                if x is None or z is None:
                    continue
                dist = math.hypot(float(x) - pos3[0], float(z) - pos3[2])
                if dist > 15.0:
                    has_far = True
                    break
        except Exception:
            return

        if not has_far:
            self._stuck_still_since = None
            return

        # 防抖 30s。
        if now - self._stuck_last_toggle < 30.0:
            return
        self._stuck_last_toggle = now
        self._stuck_still_since = None

        from app.core.team_ops import set_team_follow

        self._emit(
            "dungeon_unstick_move",
            f"卡队检测：自己停留 {still_dur:.0f}s + 队友>15m，取消跟随重开",
            ok=True,
        )
        # 不管跟随当前开没开，先取消再重开。
        try:
            set_team_follow(
                session, enabled=False, use_ui_click=False, log=self._log
            )
        except Exception:
            pass
        time.sleep(1.0)
        try:
            r2 = set_team_follow(
                session, enabled=True, use_ui_click=False, log=self._log
            )
        except Exception:
            return
        if r2.ok:
            self._emit(
                "dungeon_unstick_move",
                "卡队：组队跟随已重新开启",
                ok=True,
            )
        else:
            self._emit(
                "dungeon_unstick_skip",
                f"卡队：重开跟随失败: {r2.message}",
                ok=False,
            )

    def tick(self, session, *, scene_id=None, scene_label=None, pos=None) -> None:
        """Low-frequency observation; never blocks the runner loop. @author by ak"""
        if self._rule is None or (self._stop is not None and self._stop.is_set()):
            return
        if not self._ensure_armed(session):
            return
        try:
            sid = int(scene_id) if scene_id is not None else None
        except Exception:
            sid = None
        # Scene change (re-enter / manual map change): re-arm, never trigger.
        if self._last_scene_id is not None and sid != self._last_scene_id:
            self._rearm(scene_id=sid)
            return
        if self._moving:
            return
        # 1928：组队跟随守护（跟随掉了就补上），与是否在卡点区无关。
        self._follow_keepalive(session, pos=pos)
        if not (isinstance(pos, (tuple, list)) and len(pos) >= 3):
            # Coordinates unreadable: fail closed for this tick.
            self._reset_window()
            self._last_scene_id = sid if sid is not None else self._last_scene_id
            return
        pos3 = (float(pos[0]), float(pos[1]), float(pos[2]))
        self._maybe_mark_entry_reached(pos3)
        zone_idx = self._resolve_zone(pos3)
        if zone_idx < 0:
            # 不在任何卡点：窗口 + 冷却复位（re-arm）。
            self._rearm(scene_id=sid)
            return
        if zone_idx != self._zone_idx:
            # 从上一个卡点区进入新的卡点区：重新累计。
            self._zone_idx = zone_idx
            self._reset_window()
            self._last_pos = None
        try:
            from app.core.combat_monitor import read_combat_activity

            combat = read_combat_activity(session, log=self._log)
        except Exception as e:
            self._reset_window()
            self._emit_skip(f"战斗信号读取异常，跳过本轮: {e}")
            self._last_scene_id = sid if sid is not None else self._last_scene_id
            return
        if not combat.ok:
            self._reset_window()
            self._emit_skip(
                f"战斗信号不可读，跳过本轮: {combat.error or 'unknown'}"
            )
            self._last_scene_id = sid if sid is not None else self._last_scene_id
            return
        if not combat.resolved:
            self._armed = False
            self._arm_failed_until = time.monotonic() + 300.0
            self._emit(
                "dungeon_unstick_error",
                "攻击子系统未解析，副本纠偏失败关闭，不进行纠偏",
                ok=False,
            )
            return
        if self._host_dead(session):
            self._reset_window(combat)
            self._last_scene_id = sid if sid is not None else self._last_scene_id
            return
        moved = (
            self._last_pos is not None
            and (self._hz(self._last_pos, pos3) or 0.0) > DUNGEON_UNSTICK_MOVE_EPS_M
        )
        damaged = (
            self._last_damage_seq is not None
            and int(combat.self_damage_seq) != int(self._last_damage_seq)
        )
        # 攻击姿态不作为站街信号：没怪时内挂也会放技能，只有位移和本角色伤害
        # 结算能重置纯站街窗口。
        if moved or damaged:
            self._reset_window(combat)
            self._last_pos = pos3
            self._last_scene_id = sid if sid is not None else self._last_scene_id
            return
        if self._last_pos is None:
            # First readable silent sample inside the zone: seed + start window.
            self._reset_window(combat)
            self._last_pos = pos3
            self._last_scene_id = sid if sid is not None else self._last_scene_id
            self._still_t0 = time.monotonic()
            return
        self._last_pos = pos3
        self._last_scene_id = sid if sid is not None else self._last_scene_id
        if self._still_t0 is None:
            self._still_t0 = time.monotonic()
            return
        now = time.monotonic()
        if now - self._still_t0 < DUNGEON_UNSTICK_STILL_S:
            return
        if now < self._cooldown_until:
            return
        zone = self._zones[self._zone_idx] if 0 <= self._zone_idx < len(self._zones) else None
        # 该卡点要求"附近没怪"才执行脱困：有怪时跳过本轮，等怪清完再判。
        if zone is not None and zone.get("no_monster") and self._has_nearby_monster(
            session, pos3
        ):
            self._emit_skip("附近有怪，暂不脱困（等怪清完再判）")
            self._reset_window(combat)
            self._last_pos = pos3
            self._last_scene_id = sid if sid is not None else self._last_scene_id
            return
        self._trigger(session, pos=pos3, stuck_for_s=now - self._still_t0, zone=zone)

    # -- execution ----------------------------------------------------------

    def _trigger(self, session, *, pos, stuck_for_s: float, zone: dict | None = None) -> None:
        rule = self._rule
        zone = zone or (self._zones[0] if self._zones else None)
        # from_entry 默认区没有 stuck，展示用 next_zone 的卡点坐标。
        display_stuck = zone.get("stuck") if zone else None
        if display_stuck is None and zone is not None and zone.get("next_zone") is not None:
            try:
                display_stuck = tuple(
                    self._zones[int(zone["next_zone"])]["stuck"]
                )
            except Exception:
                display_stuck = None
        self._moving = True
        self._emit(
            "dungeon_unstick_trigger",
            f"检测到纯站街 inst={self.instance_id}（{rule['name']}）"
            f" 静止{stuck_for_s:.1f}s，执行脱困",
            ok=True,
            rule_name=rule["name"],
            stuck=tuple(display_stuck) if display_stuck else None,
            target=tuple(rule["target"]),
            stuck_for_s=round(stuck_for_s, 1),
        )
        arrived = False
        try:
            # 分流：
            #  - 带 next_zone（如 1928 第 0 卡点）：持续无伤害时线性走到下一个
            #    卡点，走到后交给该卡点逻辑，不再关注本卡点。
            #  - 卡点带回撤点且规则要求跟随（1928 卡点2）：先线性回撤，再走组队跟随到目标。
            #  - 卡点带回撤点但不要求跟随（6230）：线性回撤 + 线性直走到目标。
            #  - 无回撤点且规则要求跟随（1928 卡点1）：直接组队跟随到目标。
            #  - 其余保留规则：线性直走到目标。
            next_pt = None
            if zone is not None and zone.get("next_zone") is not None:
                try:
                    next_pt = tuple(
                        self._zones[int(zone["next_zone"])]["stuck"]
                    )
                except Exception:
                    next_pt = None
            if next_pt is not None:
                arrived = self._run_pathfind_rule(
                    session, rule, zone, pos, tgt_override=next_pt
                )
            elif zone is not None and zone.get("escape") and rule.get("follow"):
                arrived = self._run_follow_rule(
                    session, rule, pos, escape=zone.get("escape")
                )
            elif zone is not None and zone.get("escape"):
                arrived = self._run_pathfind_rule(session, rule, zone, pos)
            elif rule.get("follow"):
                arrived = self._run_follow_rule(session, rule, pos)
            else:
                arrived = self._run_pathfind_rule(session, rule, zone, pos)
        finally:
            self._moving = False
        # 成功(已离开卡点)用长冷却；失败用短冷却，便于阶段性地持续守护抢点重试。
        self._cooldown_until = time.monotonic() + (
            DUNGEON_UNSTICK_COOLDOWN_S
            if arrived
            else DUNGEON_UNSTICK_RETRY_COOLDOWN_S
        )
        # 每次触发后重新累计静止窗口，避免成功/失败后立刻连续重触发。
        self._reset_window()

    def _issue_host_move(self, session, *, x: float, y: float, z: float, mode: int) -> bool:
        """Single HostMove: bridge first, remote fallback. @author by ak"""
        quiet = lambda _m: None  # noqa: E731
        try:
            from app.core.xajh_bridge import CMD_HOST_MOVE, ensure_bridge

            br = ensure_bridge(
                int(getattr(session, "pid", 0) or 0),
                log=quiet,
                inject_if_needed=False,
                hwnd=int(self._hwnd or 0) or None,
            )
            if br is not None:
                try:
                    r = br.call(
                        CMD_HOST_MOVE,
                        x=float(x),
                        y=float(y),
                        z=float(z),
                        mode=int(mode),
                        hwnd=int(self._hwnd or 0) or None,
                        timeout_ms=4000,
                    )
                    if bool(getattr(r, "ok", False)):
                        return True
                finally:
                    try:
                        br.close()
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            from app.core.automove import PathTarget, host_move_to

            res = host_move_to(
                session,
                PathTarget(x=float(x), y=float(y), z=float(z), mode=int(mode)),
                log=quiet,
                fallback_mode0=(int(mode) == 0),
            )
            return bool(getattr(res, "ok", False))
        except Exception:
            return False

    def _linear_walk(
        self, session, *, tgt, timeout_s: float, emit_label: str
    ) -> dict:
        """
        线性分步直走：从当前位置向目标点分段 HostMove，段内不重发（顺畅）。

        只在该重发时才重发：无航点 / 接近航点 / 被内挂抢离航点（距离增大）。
        段长 8m，行走中不再反复重定向，避免一卡一卡；被抢位时重新从当前位置
        瞄准目标。距离不再下降则判停滞放弃，交给短冷却重触发。

        返回 dict（ok/arrived/verified/via/last_distance/last_position/error）。
        @author by ak
        """
        from app.core.automove import read_scene_position

        quiet = lambda _m: None  # noqa: E731
        scene_mode = 0
        if self._last_scene_id is not None and int(self._last_scene_id) != 0:
            scene_mode = int(self._last_scene_id)
        radius = float(DUNGEON_UNSTICK_RADIUS_M)
        step_m = 8.0
        reissue_dist = 3.0  # 距当前航点 ≤ 3m 视为本段走完
        # 到位判定：进入目标 near_r 内不再重发航点，连续采样稳定即视为已到。
        arrive_r = 2.5
        near_r = 4.0
        poll = 0.3
        stagnant_max = 4
        deadline = time.monotonic() + max(0.5, float(timeout_s))
        last_distance: float | None = None
        last_position: tuple[float, float, float] | None = None
        best = float("inf")
        stagnant = 0
        waypoint: tuple[float, float, float] | None = None
        last_wp_dist: float | None = None
        near_reads = 0
        self._emit(
            "dungeon_unstick_move",
            f"{emit_label}（线性走） → ({tgt[0]:.1f},{tgt[1]:.1f},{tgt[2]:.1f})"
            f" mode={scene_mode}",
            ok=True,
            target=tuple(tgt),
            mode=scene_mode,
        )

        def _abort() -> dict:
            return {
                "ok": False,
                "arrived": False,
                "verified": False,
                "error": "linear walk stopped",
                "last_distance": last_distance,
                "last_position": list(last_position) if last_position else None,
                "via": "linear",
            }

        while time.monotonic() < deadline:
            if self._stop is not None and self._stop.is_set():
                return _abort()
            sp = read_scene_position(session, log=quiet)
            if not getattr(sp, "ok", False) or not getattr(sp, "scene_pos", None):
                if not _sleep_interruptible(0.2, self._stop):
                    break
                continue
            pos3 = tuple(float(v) for v in sp.scene_pos)
            last_position = pos3
            last_distance = math.hypot(
                pos3[0] - float(tgt[0]), pos3[2] - float(tgt[2])
            )
            # —— 到位判定：到达就是到达，不再重发、不回头 ——
            if last_distance <= arrive_r:
                return {
                    "ok": True,
                    "arrived": True,
                    "verified": True,
                    "last_distance": last_distance,
                    "last_position": list(pos3),
                    "via": "linear",
                }
            if last_distance <= near_r:
                # 已到目标附近：停住不再重发，连续几次稳定即视为到位。
                near_reads += 1
                if near_reads >= 3:
                    return {
                        "ok": True,
                        "arrived": True,
                        "verified": True,
                        "last_distance": last_distance,
                        "last_position": list(pos3),
                        "via": "linear",
                    }
            else:
                near_reads = 0
                # —— 远离目标：才重发下一段航点 ——
                need_reissue = waypoint is None
                wp_dist: float | None = None
                if waypoint is not None:
                    wp_dist = math.hypot(
                        pos3[0] - waypoint[0], pos3[2] - waypoint[2]
                    )
                    if wp_dist <= reissue_dist or (
                        last_wp_dist is not None
                        and wp_dist > last_wp_dist + 1.0
                    ):
                        need_reissue = True
                if need_reissue:
                    dx = float(tgt[0]) - pos3[0]
                    dz = float(tgt[2]) - pos3[2]
                    d = math.hypot(dx, dz)
                    if d > 1e-6:
                        step = min(float(step_m), max(0.5, d - radius))
                        nx = pos3[0] + dx / d * step
                        nz = pos3[2] + dz / d * step
                        ok = self._issue_host_move(
                            session, x=nx, y=float(tgt[1]), z=nz, mode=scene_mode
                        )
                        if ok:
                            waypoint = (nx, float(tgt[1]), nz)
                            last_wp_dist = math.hypot(
                                pos3[0] - nx, pos3[2] - nz
                            )
                            # 新段起点：重置停滞基线。
                            best = last_distance
                            stagnant = 0
                        else:
                            waypoint = None
                            last_wp_dist = None
                            if not _sleep_interruptible(0.3, self._stop):
                                break
                            continue
                    else:
                        waypoint = None
                        last_wp_dist = None
                else:
                    last_wp_dist = wp_dist
                    # 停滞判定：连续若干次距离不下降则放弃本段（真卡住）。
                    if last_distance < best - 0.4:
                        best = last_distance
                        stagnant = 0
                    else:
                        stagnant += 1
                        if stagnant >= stagnant_max:
                            return {
                                "ok": False,
                                "arrived": False,
                                "verified": False,
                                "error": "linear walk stagnant",
                                "last_distance": last_distance,
                                "last_position": list(pos3),
                                "via": "linear",
                            }
            if not _sleep_interruptible(poll, self._stop):
                break
        return {
            "ok": False,
            "arrived": False,
            "verified": False,
            "error": "linear walk timeout",
            "last_distance": last_distance,
            "last_position": list(last_position) if last_position else None,
            "via": "linear",
        }

    def _run_pathfind_rule(
        self, session, rule: dict, zone: dict | None, pos, *, tgt_override=None
    ) -> bool:
        """Retained rules: 线性直走，无挂机停止 / 无跟随。

        当前卡点 zone 带 escape 时先线性回撤脱离墙/梯子附近，再线性走到目标点
        （默认 rule["target"]；带 next_zone 的卡点用 tgt_override 走到下一个
        卡点）。回撤段不要求严格到位，直接进入目标段；目标段失败走短冷却重触发。

        @author by ak
        """
        target = tuple(tgt_override) if tgt_override else tuple(rule["target"])
        emit_target = "线性走到下一个卡点" if tgt_override else "脱困寻路"
        # 进本默认区走到下一个卡点距离较长，用更长超时。
        target_timeout = (
            DUNGEON_UNSTICK_ENTRY_TIMEOUT_S
            if tgt_override
            else DUNGEON_UNSTICK_PATHFIND_TIMEOUT_S
        )
        stages: list[tuple[str, tuple, float]] = []
        escape = zone.get("escape") if zone else None
        if escape:
            stages.append(
                ("escape", tuple(escape), DUNGEON_UNSTICK_ESCAPE_TIMEOUT_S)
            )
        stages.append(("target", target, target_timeout))
        for label, tgt, timeout_s in stages:
            if self._stop is not None and self._stop.is_set():
                self._emit(
                    "dungeon_unstick_skip",
                    "脱困寻路期间任务停止，忽略结果",
                    ok=False,
                )
                return False
            emit_label = (
                "脱困先走几步脱离桥附近" if label == "escape" else emit_target
            )
            try:
                d = self._linear_walk(
                    session, tgt=tgt, timeout_s=timeout_s, emit_label=emit_label
                )
            except Exception as e:
                self._emit(
                    "dungeon_unstick_error",
                    f"{emit_label}异常: {e}",
                    ok=False,
                )
                return False
            arrived = bool(d and (d.get("arrived") or d.get("verified")))
            if label == "escape":
                # 脱离段走几步即可；内挂可能抢位，不阻塞直接进入目标段。
                self._emit(
                    "dungeon_unstick_move",
                    f"脱离桥附近{'完成' if arrived else '未到位'}，继续目标寻路"
                    + ("（可能被内挂抢位）" if not arrived else ""),
                    ok=True,
                    arrived=arrived,
                    target=tuple(tgt),
                )
                continue
            self._emit(
                "dungeon_unstick_done",
                f"脱困寻路{'到位' if arrived else '未到位'}"
                f" via={d.get('via') if d else '?'}"
                f" dist={d.get('last_distance') if d else '?'}"
                + (f" err={d.get('error')}" if d and d.get("error") else ""),
                ok=arrived,
                arrived=arrived,
                via=(d or {}).get("via"),
                distance=(d or {}).get("last_distance"),
            )
            return arrived
        return False

    def _run_follow_rule(
        self, session, rule: dict, pos, *, escape=None
    ) -> bool:
        """1928: ensure team follow on, walk to target, keep follow on.

        本副本必须保持组队跟随否则会掉队：纠偏后不取消跟随，交给跟随守护在
        tick 中持续检查补充。带 escape（1928 第二卡点）时先线性回撤脱离墙/梯子
        附近，再走组队跟随到目标。

        @author by ak
        """
        from app.core.team_ops import set_team_follow

        if escape:
            d0 = None
            try:
                d0 = self._linear_walk(
                    session,
                    tgt=tuple(escape),
                    timeout_s=float(DUNGEON_UNSTICK_ESCAPE_TIMEOUT_S),
                    emit_label="脱困先回撤脱离",
                )
            except Exception as e:
                self._emit(
                    "dungeon_unstick_error",
                    f"回撤异常: {e}",
                    ok=False,
                )
                return False
            escaped = bool(d0 and (d0.get("arrived") or d0.get("verified")))
            self._emit(
                "dungeon_unstick_move",
                f"回撤脱离{'完成' if escaped else '未到位'}，再走组队跟随",
                ok=True,
                arrived=escaped,
                target=tuple(escape),
            )
            if self._stop is not None and self._stop.is_set():
                self._emit(
                    "dungeon_unstick_skip",
                    "回撤期间任务停止，忽略结果",
                    ok=False,
                )
                return False

        on = set_team_follow(
            session, enabled=True, use_ui_click=False, log=self._log
        )
        if not on.ok:
            self._emit(
                "dungeon_unstick_error",
                f"开启组队跟随失败，不寻路: {on.message}",
                ok=False,
            )
            return False
        self._emit(
            "dungeon_unstick_move",
            "组队跟随已开启，开始脱困寻路",
            ok=True,
        )
        d = None
        err: str | None = None
        try:
            d = self._linear_walk(
                session,
                tgt=tuple(rule["target"]),
                timeout_s=float(DUNGEON_UNSTICK_PATHFIND_TIMEOUT_S),
                emit_label="脱困寻路",
            )
        except Exception as e:
            err = str(e)
            self._emit(
                "dungeon_unstick_error",
                f"脱困寻路异常: {e}",
                ok=False,
            )
        # 本副本必须保持组队跟随否则会掉队：纠偏后不取消跟随，
        # 由 tick 里的跟随守护持续检查、掉了就补。
        if err is not None:
            return False
        if self._stop is not None and self._stop.is_set():
            self._emit(
                "dungeon_unstick_skip",
                "脱困寻路期间任务停止，忽略结果",
                ok=False,
            )
            return False
        arrived = bool(d and (d.get("arrived") or d.get("verified")))
        self._emit(
            "dungeon_unstick_done",
            f"脱困寻路{'到位' if arrived else '未到位'}（保持跟随）"
            f" via={d.get('via') if d else '?'}"
            f" dist={d.get('last_distance') if d else '?'}"
            + (f" err={d.get('error')}" if d and d.get("error") else ""),
            ok=arrived,
            arrived=arrived,
            via=(d or {}).get("via"),
            distance=(d or {}).get("last_distance"),
        )
        return arrived


class ActivityRunner:
    """
    Background full-auto loop for 自动副本.

    mode=activity: enter + claim flourish chests until target points.
    mode=dungeon: enter/clear loop only (no claim; max_runs<=0 means until stop).

    @author by ak
    """

    def __init__(
        self,
        *,
        pid: int,
        hwnd: int = 0,
        role_id: int | str | None = None,
        cfg: ActivityConfig | None = None,
        hang_settings: dict | None = None,
        on_event: Callable[[ActivityStepEvent], None] | None = None,
        log: LogFn | None = None,
        pause_event: threading.Event | None = None,
    ):
        self.pid = int(pid)
        self.hwnd = int(hwnd or 0)
        self._role_id = str(role_id or "").strip()
        self.cfg = cfg or ActivityConfig()
        # Snapshot settings-panel values so auto-dungeon and Hang Start/Stop
        # use the same configuration for this run.
        self._hang_settings = dict(hang_settings or {})
        self.on_event = on_event or (lambda _e: None)
        self.log = log or (lambda _m: None)
        self._pause_event = pause_event
        self._lifecycle = RunnerLifecycle(f"xajh-activity-{self.pid}")
        self._stop = self._lifecycle.stop_event
        self._thread: threading.Thread | None = None
        self._session: GameAttachSession | None = None
        self.running = False
        self._rounds = 0
        self._ok_count = 0
        self._fail_count = 0
        # Will be refreshed from live GetReputation(46) after attach.
        self._points = max(0, int(self.cfg.initial_points))
        self._points_live = False
        # 切糕内挂软件侧估计：True=可能开着 / False=已关 / None=未知
        self._hang_likely_on: bool | None = None
        # 切糕死亡暂停只影响软件侧寻路，不关闭游戏内挂机。
        self._qiegao_dead_paused = False
        # 普通副本纯站街纠偏（只在 mode=dungeon 时惰性创建）。
        self._dungeon_unstick_guard: DungeonUnstickGuard | None = None

    def request_pause(self) -> None:
        """Request a cooperative pause at the next safe node (after a round). @author by ak"""
        if self._pause_event is not None:
            self._pause_event.set()

    def _pause_requested(self) -> bool:
        return bool(self._pause_event is not None and self._pause_event.is_set())

    def start(self) -> None:
        """Start background loop. @author by ak"""
        if self.running:
            return
        is_qiegao = self._is_qiegao_mode()
        if is_qiegao:
            with _QIEGAO_ACTIVE_RUNNERS_LOCK:
                _QIEGAO_ACTIVE_RUNNERS.add(int(self.pid))
        thread = self._lifecycle.start(self._loop)
        if thread is not None:
            self._thread = thread
            self.running = True
        elif is_qiegao:
            with _QIEGAO_ACTIVE_RUNNERS_LOCK:
                _QIEGAO_ACTIVE_RUNNERS.discard(int(self.pid))

    def stop(self) -> bool:
        """Request stop; also halt in-game hang/path. @author by ak"""
        # signal worker first so loops exit
        try:
            self._lifecycle.stop(wait=False)
        except Exception:
            pass
        try:
            self._game_side_stop()
        except Exception as e:
            try:
                self.log(f"activity game_side_stop: {e}")
            except Exception:
                pass
        stopped = self._lifecycle.stop(wait=True)
        self.running = False
        return stopped

    def is_running(self) -> bool:
        return bool(self.running and self._lifecycle.is_running())

    def _scene_prefer_hub(
        self,
        session: GameAttachSession | None = None,
        *,
        max_age_s: float = 2.5,
        force_read: bool = False,
    ):
        """
        Prefer session LiveSceneHub cache (header single poller); fallback RPM.

        max_age_s must stay short so hang/副本 never sits on a dead city sample.
        force_read/fresh always samples memory and repushes hub.

        Returns (scene_id, pos, label) like read_scene_state.

        @author by ak
        """
        if not force_read:
            try:
                from app.core.live_scene_hub import DEFAULT_LIVE_MAX_AGE_S, get_live_scene

                age = float(max_age_s if max_age_s is not None else DEFAULT_LIVE_MAX_AGE_S)
                snap = get_live_scene(int(self.pid), max_age_s=age)
                if snap is not None and (
                    snap.scene_label or snap.scene_id is not None or snap.pos is not None
                ):
                    return snap.scene_id, snap.pos, snap.scene_label or ""
            except Exception:
                pass
        sess = session or self._session
        if sess is None:
            return None, None, ""
        # force_read / hub miss: must-live sample + hub publish
        return read_scene_state(sess, fresh=True, log=self.log)

    def _emit(
        self,
        phase: str,
        message: str,
        ok: bool = True,
        *,
        log_event: bool = True,
        **detail,
    ) -> None:
        """
        Push UI/log event; always attach points/target when known.

        @author by ak
        """
        if "points" not in detail:
            detail["points"] = int(self._points)
        if "target_points" not in detail:
            detail["target_points"] = int(self.cfg.target_points)
        if "points_live" not in detail:
            detail["points_live"] = bool(self._points_live)
        ev = ActivityStepEvent(phase=phase, message=message, ok=ok, detail=detail)
        try:
            self.on_event(ev)
        except Exception:
            pass
        if log_event:
            self.log(f"activity [{phase}] {message}")

    def _status(self, msg: str) -> None:
        self._emit("status", msg, ok=True)

    def _refresh_points(self, session: GameAttachSession) -> int:
        """
        Read live flourish points; fall back to last known on failure.

        @author by ak
        """
        live = read_flourish_points(session, log=self.log)
        if live is not None:
            self._points = max(0, int(live))
            self._points_live = True
        return int(self._points)

    def _emit_scene(
        self,
        phase: str,
        message: str,
        session: GameAttachSession,
        *,
        ok: bool = True,
        **detail,
    ) -> None:
        """
        Emit with live scene_id / map label for GUI header.

        @author by ak
        """
        sid, _pos, label = read_scene_state(session, log=self.log)
        detail.setdefault("scene_id", sid)
        detail.setdefault("scene_label", label)
        self._emit(phase, message, ok=ok, **detail)

    def _mode_key(self) -> str:
        m = str(getattr(self.cfg, "mode", "activity") or "activity").strip().lower()
        if m in ("qiegao", "切糕", "qg", "cake"):
            return "qiegao"
        if m in ("dungeon", "副本", "fb"):
            return "dungeon"
        return "activity"

    def _is_dungeon_mode(self) -> bool:
        """True when farming instances without flourish target/claim. @author by ak"""
        return self._mode_key() == "dungeon"

    def _dungeon_unstick_ensure_guard(self) -> DungeonUnstickGuard | None:
        """Create the dungeon unstick guard lazily (dungeon mode + known rule). @author by ak"""
        if not self._is_dungeon_mode():
            return None
        guard = self._dungeon_unstick_guard
        if guard is None:
            if int(getattr(self.cfg, "instance_id", 0) or 0) in DUNGEON_UNSTICK_RULES:
                guard = DungeonUnstickGuard(
                    instance_id=int(getattr(self.cfg, "instance_id", 0) or 0),
                    on_event=self._emit,
                    log=self.log,
                    stop_event=self._stop,
                    hwnd=int(self.hwnd or 0),
                    use_bridge=bool(getattr(self.cfg, "use_bridge", True)),
                )
                self._dungeon_unstick_guard = guard
            else:
                return None
        return guard

    def _dungeon_unstick_prepare_run(self) -> None:
        """每次进本开始时重置守护的跨趟状态（进本默认区等）。@author by ak"""
        guard = self._dungeon_unstick_ensure_guard()
        if guard is not None:
            try:
                guard.reset_run()
            except Exception as e:
                try:
                    self.log(f"dungeon unstick prepare run: {e}")
                except Exception:
                    pass

    def _dungeon_unstick_tick(
        self, session, *, scene_id=None, scene_label=None, pos=None
    ) -> None:
        """
        Low-frequency 纯站街 observation callback for the dungeon wait loop.

        Bound to the ordinary dungeon runner only; activity/qiegao never pass
        this callback to wait_return_city.

        @author by ak
        """
        guard = self._dungeon_unstick_ensure_guard()
        if guard is None:
            return
        try:
            guard.tick(session, scene_id=scene_id, scene_label=scene_label, pos=pos)
        except Exception as e:
            try:
                self.log(f"dungeon unstick tick error: {e}")
            except Exception:
                pass

    def _handle_return_wait_failed(self, session, cfg) -> None:
        """
        Handle wait_return_city failure after the in-game hang cleared a dungeon.

        In dungeon mode the dungeon was already cleared but the character did
        not make it back to the city (e.g. stuck standing after clear). Count
        this round as complete so max_runs is honored; otherwise the runner
        would restart from round 1 and re-clear the same dungeon.

        Activity/qiegao callers keep the legacy 等待回城中断 hint.

        @author by ak
        """
        if not self._is_dungeon_mode():
            self._emit("stop", "等待回城中断", ok=False)
            return
        if self._stop.is_set():
            # 用户手动停止：不计入完成，保持原提示。
            self._emit("stop", "等待回城中断", ok=False)
            return
        self._ok_count += 1
        try:
            sid2, _pos2, label2 = read_scene_state(session, log=self.log)
        except Exception:
            sid2, label2 = None, ""
        self._emit(
            "enter_ok",
            f"第 {self._ok_count} 次完成（副本已清·回城等待中断）",
            ok=True,
            ok_count=self._ok_count,
            scene_id=sid2,
            scene_label=label2,
        )
        if self._done_target():
            self._emit(
                "done",
                f"副本完成 · 共成功{self._ok_count}/{cfg.max_runs}",
                ok=True,
            )
        else:
            self._emit(
                "stop",
                "回城等待超时，副本已清但未回城，停止后续",
                ok=False,
            )
        self._stop.set()

    def _is_qiegao_mode(self) -> bool:
        """True for 切糕 / 140副本任务一 hang-point flow. @author by ak"""
        return self._mode_key() == "qiegao"

    def _is_points_mode(self) -> bool:
        return self._mode_key() == "activity"

    def _done_target(self) -> bool:
        # max_runs<=0 means unlimited (dungeon/qiegao default).
        max_r = int(self.cfg.max_runs)
        runs_done = max_r > 0 and int(self._ok_count) >= max_r
        if self._is_dungeon_mode() or self._is_qiegao_mode():
            return runs_done
        return int(self._points) >= int(self.cfg.target_points) or runs_done

    def _abort_if_game_dead(self, where: str = "") -> bool:
        """
        If game pid is gone: emit game_dead, stop runner, return True.

        Also writes structured 游戏崩溃 report (exit code / native crash point).
        @author by ak
        """
        if _pid_alive(self.pid):
            return False
        msg = f"游戏进程已退出 pid={self.pid}"
        if where:
            msg = f"{msg}（{where}）"
        try:
            from app.core.crash_capture import report_game_crash

            report_game_crash(
                int(self.pid),
                reason=f"activity:{where or 'game_dead'}",
            )
        except Exception:
            pass
        self._emit("game_dead", msg, ok=False)
        self._stop.set()
        return True

    def _claim_awards(self, session: GameAttachSession, *, reason: str) -> dict:
        """
        Open Win_InstanceEndlessList and click enabled Bnt_Bonus01..04.

        Safe to call repeatedly: disabled/already-claimed buttons are skipped.

        @author by ak
        """
        cfg = self.cfg
        if not cfg.claim_awards:
            return {"ok": False, "skipped": True, "reason": "claim_awards off"}
        self._refresh_points(session)
        self._emit("claim", f"领取活跃宝箱（{reason}）")
        claim = claim_daily_activity_awards(
            session,
            hwnd=self.hwnd,
            btn_count=int(cfg.award_btn_count),
            allow_cursor=bool(cfg.allow_cursor_click),
            log=self.log,
            points=int(self._points),
        )
        clicked = claim.get("clicked") or []
        skipped = claim.get("skipped") or []
        self._emit(
            "claim",
            f"领取完成 clicked={len(clicked)} skipped={len(skipped)} "
            f"err={claim.get('error')}",
            ok=bool(claim.get("ok")) and not claim.get("error"),
            clicked=clicked,
            skipped=skipped,
            any_click=bool(claim.get("any_click")),
            reason=reason,
        )
        return claim

    def _finish_done(
        self,
        session: GameAttachSession | None,
        *,
        message: str,
    ) -> None:
        """
        Claim awards (if enabled) then stop — always claim before done.

        @author by ak
        """
        if session is not None and self.cfg.claim_awards:
            self._claim_awards(session, reason="目标达成")
        self._emit(
            "done",
            message,
            ok=True,
            points=int(self._points),
        )
        self._stop.set()

    def _sleep_cd(self, total_s: float) -> bool:
        """
        Interruptible CD with per-second entry_cd ticks for GUI countdown.

        Logs once at start; subsequent ticks only go to on_event (status bar).

        @author by ak
        """
        total = max(0.0, float(total_s))
        end = time.time() + total
        logged = False
        while True:
            if self._stop.is_set():
                return False
            left = end - time.time()
            if left <= 0:
                return True
            msg = f"冷却 {left:.0f}s 后继续"
            ev = ActivityStepEvent(
                phase="entry_cd",
                message=msg,
                ok=True,
                detail={"sleep_s": total, "remain_s": left},
            )
            try:
                self.on_event(ev)
            except Exception:
                pass
            if not logged:
                self.log(f"activity [entry_cd] 冷却 {total:.1f}s 后继续")
                logged = True
            if not _sleep_interruptible(min(1.0, left), self._stop):
                return False

    def _qiegao_afk_target(self, session: GameAttachSession | None = None) -> PathTarget | None:
        """
        Build hang-point target; fall back to QIEGAO_DEFAULT_AFK.

        mode 使用当前 scene_id（与 chest_pathfind / 成品寻路一致）；
        读不到场景时再退回 0。

        @author by ak
        """
        cfg = self.cfg
        xs = getattr(cfg, "qiegao_afk_x", None)
        ys = getattr(cfg, "qiegao_afk_y", None)
        zs = getattr(cfg, "qiegao_afk_z", None)
        using_default = False
        if xs is None or ys is None or zs is None:
            try:
                xs, ys, zs = QIEGAO_DEFAULT_AFK
                using_default = True
            except Exception:
                return None
        try:
            x = float(xs)
            y = float(ys)
            z = float(zs)
        except Exception:
            return None
        # 合法性：有限数、非原点占位；与当前坐标同图时水平距离不应离谱
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            self.log(f"activity qiegao afk invalid non-finite xyz=({xs},{ys},{zs})")
            return None
        if abs(x) < 1e-3 and abs(y) < 1e-3 and abs(z) < 1e-3:
            self.log("activity qiegao afk invalid near-origin (0,0,0)")
            return None
        mode = 0
        try:
            if session is not None:
                sid, pos, _lab = self._read_live_pos(session)
                if sid is not None and int(sid) != 0:
                    mode = int(sid)
                if pos is not None:
                    dist = math.hypot(float(pos[0]) - x, float(pos[2]) - z)
                    if dist > 800.0:
                        self.log(
                            f"activity qiegao afk warn: dist={dist:.1f}m 过大"
                            f" target=({x:.1f},{y:.1f},{z:.1f})"
                            f" cur=({pos[0]:.1f},{pos[1]:.1f},{pos[2]:.1f})"
                            + (" default" if using_default else "")
                        )
        except Exception:
            pass
        return PathTarget(x=x, y=y, z=z, mode=mode)

    def _hz_dist_to_afk(
        self,
        pos: tuple[float, float, float] | None,
        tgt: PathTarget,
    ) -> float | None:
        """Horizontal XZ distance to hang point. @author by ak"""
        if pos is None:
            return None
        try:
            return float(math.hypot(float(pos[0]) - float(tgt.x), float(pos[2]) - float(tgt.z)))
        except Exception:
            return None

    def _read_live_pos(
        self, session: GameAttachSession, *, fresh: bool = False
    ) -> tuple[int | None, tuple[float, float, float] | None, str | None]:
        """scene_id, (x,y,z), label. @author by ak"""
        try:
            sid, pos, label = read_scene_state(
                session, fresh=bool(fresh), log=self.log
            )
            return sid, pos, label
        except Exception:
            return None, None, None


    def _qiegao_host_dead(
        self, session: GameAttachSession, *, force: bool = False
    ) -> bool | None:
        """Read host death through hang's shared, throttled state machine.

        The state machine serializes CRT through SafeDispatch, so Activity does
        not create a parallel death probe for the same game pid.
        @author by ak
        """
        try:
            from app.core.hang_settings import (
                get_hang_dead_state,
                refresh_hang_dead_state,
            )

            cached = get_hang_dead_state(session)
            if cached is not None and not force:
                return bool(cached)
            return refresh_hang_dead_state(
                session, log=lambda _m: None, force=bool(force)
            )
        except Exception:
            return None

    def _qiegao_wait_until_revived(
        self, session: GameAttachSession, *, reason: str
    ) -> bool:
        """Pause path work while dead; retain task-level death/Roll handling.

        False means the runner stopped or the character left the dungeon.
        @author by ak
        """
        poll = max(1.0, min(3.0, float(getattr(self.cfg, "return_poll_s", 15.0))))
        emitted = False
        rearm_attempted = False
        rearmed_for_revive = False
        while not self._stop.is_set():
            dead = self._qiegao_host_dead(session, force=not emitted)
            if dead is False:
                if emitted or self._qiegao_dead_paused:
                    self._emit("afk_hang", "角色已复活，恢复切糕寻路流程", ok=True)
                if rearmed_for_revive:
                    self._ensure_hang_off(
                        session,
                        reason="复活后恢复寻路关挂机",
                        force=True,
                        keep_task_guard=True,
                    )
                self._qiegao_dead_paused = False
                return True
            if dead is True and not emitted:
                self._emit(
                    "afk_hang",
                    f"角色死亡，任务守护继续处理拾取，暂停{reason}并等待复活",
                    ok=True,
                )
                emitted = True
                self._qiegao_dead_paused = True
            if dead is True and not rearm_attempted:
                rearm_attempted = True
                sid_r, _pos_r, label_r = self._scene_prefer_hub(
                    session, max_age_s=1.5
                )
                if is_dungeon_scene(sid_r, label_r, gate=self.cfg.city_gate):
                    on = self._read_hang_on(session)
                    if on is False:
                        self._emit(
                            "afk_hang",
                            "死亡时内挂已中断，任务内重启一次以触发游戏副本复活",
                            ok=True,
                        )
                        rearmed_for_revive = bool(self._ensure_hang_on(session))
                else:
                    self._emit(
                        "afk_hang",
                        f"死亡状态下未确认仍在副本（{label_r or sid_r}），不重启内挂",
                        ok=True,
                    )
                self._ensure_qiegao_task_guard(session)
            try:
                sid, _pos, label = self._scene_prefer_hub(session, max_age_s=2.5)
                has_map = bool(label) or sid is not None
                if has_map and (
                    is_city_scene(sid, label, self.cfg.city_gate)
                    or not is_dungeon_scene(sid, label, gate=self.cfg.city_gate)
                ):
                    self._qiegao_dead_paused = False
                    return False
            except Exception:
                pass
            if not _sleep_interruptible(poll, self._stop):
                return False
        return False


    def _qiegao_unstick_nudge(
        self,
        session: GameAttachSession,
        *,
        last_pos: tuple[float, float, float],
        target: tuple[float, float, float],
        scene_id: int | None = None,
        stuck_for_s: float = 0.0,
        attempt: int = 1,
        sync_with_peer: bool = False,
        peer_distance: float | None = None,
    ) -> bool:
        """
        空气墙/阶段门卡住脱困：先停在原地，再退回桥中部附近。

        实机 4m 仍可能处于墙体碰撞边缘，兜底沿来路回撤约 6m。
        过阶段动画约 5s；调用方应在卡住 ≥10s 后才触发本逻辑。
        走完后静待，再由 pathfind 重新 HostMove 回挂机点。

        @author by ak
        """
        if self._qiegao_host_dead(session) is True:
            self._qiegao_dead_paused = True
            self._emit(
                "afk_hang",
                "脱困前检测到死亡：跳过回撤，等待复活",
                ok=True,
            )
            return False
        cfg = self.cfg
        # Stop the current HostMove before joining the local barrier. The
        # actual retreat is released together once the peer reports the wall.
        try:
            from app.core.automove import stop_automove_best_effort

            stop_automove_best_effort(session, log=lambda _m: None)
        except Exception:
            pass
        peers = 1
        if sync_with_peer:
            try:
                sync_wait = float(
                    getattr(cfg, "qiegao_unstick_sync_wait_s", 12.0) or 0.0
                )
            except Exception:
                sync_wait = 12.0
            peers = _qiegao_sync_unstick(
                pid=int(getattr(session, "pid", self.pid) or self.pid),
                scene_id=scene_id,
                stop_event=self._stop,
                wait_s=sync_wait,
            )
        if self._stop.is_set():
            return False
        if self._qiegao_host_dead(session) is True:
            self._qiegao_dead_paused = True
            self._emit(
                "afk_hang",
                "脱困同步等待期间角色死亡：取消回撤并等待复活",
                ok=True,
            )
            return False
        try:
            step = float(getattr(cfg, "qiegao_unstick_step_m", 6.0) or 6.0)
        except Exception:
            step = 6.0
        # Keep the character on the bridge while leaving the wall collision.
        step = max(5.0, min(7.0, step))
        step = max(5.0, min(7.0, step * random.uniform(0.95, 1.05)))
        try:
            settle = float(getattr(cfg, "qiegao_unstick_settle_s", 1.2) or 1.2)
        except Exception:
            settle = 1.2
        settle = max(0.6, min(3.0, settle))

        px, py, pz = float(last_pos[0]), float(last_pos[1]), float(last_pos[2])
        tx, ty, tz = float(target[0]), float(target[1]), float(target[2])
        dx = px - tx
        dz = pz - tz
        dist = math.hypot(dx, dz)
        if dist < 0.15:
            ang = random.uniform(0.0, math.tau if hasattr(math, "tau") else (2.0 * math.pi))
            ux, uz = math.cos(ang), math.sin(ang)
        else:
            ux, uz = dx / dist, dz / dist  # 远离目标 = 身后小回撤
        # 两号都沿远离目标的方向回撤；只保留很小偏转，避免互相横向挤压。
        yaw = random.uniform(-0.08, 0.08)
        c, s = math.cos(yaw), math.sin(yaw)
        rx, rz = ux * c - uz * s, ux * s + uz * c
        nx, ny, nz = px + rx * step, py, pz + rz * step

        mode = 0
        try:
            if scene_id is not None and int(scene_id) != 0:
                mode = int(scene_id)
            else:
                sid, _p, _l = self._read_live_pos(session)
                if sid is not None and int(sid) != 0:
                    mode = int(sid)
        except Exception:
            mode = 0

        self._emit(
            "afk_move",
            f"卡住脱困#{attempt}（已原地{stuck_for_s:.0f}s，"
            f"{'双号同步' if peers >= 2 else ('同伴未同步' if sync_with_peer else '单号')}）"
            f" 回撤到桥中 {step:.1f}m → ({nx:.1f},{ny:.1f},{nz:.1f}) mode={mode}",
            ok=True,
            stuck_for_s=stuck_for_s,
            attempt=attempt,
            peer_distance=peer_distance,
        )

        # 再停一次，覆盖协调等待期间可能到达的迟发移动。
        try:
            from app.core.automove import stop_automove_best_effort

            stop_automove_best_effort(session, log=lambda _m: None)
        except Exception:
            pass
        if self._stop.is_set():
            return False
        if not _sleep_interruptible(0.25, self._stop):
            return False
        if self._qiegao_host_dead(session) is True:
            self._qiegao_dead_paused = True
            self._emit("afk_hang", "脱困发送前角色死亡：取消回撤", ok=True)
            return False

        ok = False
        via = "?"
        # bridge HostMove 优先
        try:
            from app.core.xajh_bridge import CMD_HOST_MOVE, ensure_bridge

            br = ensure_bridge(
                int(session.pid),
                log=self.log,
                inject_if_needed=False,
                hwnd=int(self.hwnd or 0) or None,
            )
            if br is not None:
                try:
                    r = br.call(
                        CMD_HOST_MOVE,
                        x=float(nx),
                        y=float(ny),
                        z=float(nz),
                        mode=int(mode),
                        hwnd=int(self.hwnd or 0) or None,
                        timeout_ms=4000,
                    )
                    ok = bool(getattr(r, "ok", False))
                    via = "bridge"
                finally:
                    try:
                        br.close()
                    except Exception:
                        pass
        except Exception as e:
            self.log(f"activity unstick bridge: {e}")

        if not ok:
            try:
                tgt = PathTarget(x=float(nx), y=float(ny), z=float(nz), mode=int(mode))
                res = host_move_to(session, tgt, log=self.log)
                ok = bool(getattr(res, "ok", False))
                via = "remote"
            except Exception as e:
                self.log(f"activity unstick remote: {e}")
                ok = False

        if self._qiegao_host_dead(session) is True:
            self._qiegao_dead_paused = True
            try:
                from app.core.automove import stop_automove_best_effort

                stop_automove_best_effort(session, log=lambda _m: None)
            except Exception:
                pass
            self._emit(
                "afk_hang",
                "脱困调用期间角色死亡：已立即停移动，不再进入脱困等待",
                ok=True,
            )
            return False

        self._emit(
            "afk_move",
            f"脱困走动{'OK' if ok else '失败'} via={via}，短等 {settle:.1f}s",
            ok=ok,
        )
        if not _sleep_interruptible(settle + random.uniform(0.0, 0.3), self._stop):
            return False
        # 阶段等待由 pathfind 的 hold 窗口统一执行；双号没有先后顺序。
        return ok

    def _move_to_qiegao_afk(

        self,
        session: GameAttachSession,
        *,
        wait_arrive: bool = True,
    ) -> bool:
        """
        切糕挂机点寻路 — 直接复用项目成品 pathfind_to_clue。

        mode 使用当前 scene_id（与 chest_pathfind / 任务寻路一致）。
        本场景走路：HostMoveToScenePosition(scene_id, x, y, z)。

        @author by ak
        """
        if self._qiegao_host_dead(session) is True:
            self._emit(
                "afk_move",
                "角色死亡，跳过寻路包；任务守护继续处理拾取并等待复活",
                ok=True,
            )
            self._qiegao_dead_paused = True
            return False
        tgt = self._qiegao_afk_target(session)
        if tgt is None:
            self._emit("afk_move", "未设置挂机点，跳过寻路（原地等到超时）", ok=True)
            return False

        cfg = self.cfg
        radius = max(0.5, float(getattr(cfg, "qiegao_arrive_radius", 1.0) or 1.0))
        timeout = max(5.0, float(getattr(cfg, "qiegao_path_timeout_s", 180.0) or 180.0))
        if not wait_arrive:
            timeout = min(timeout, 3.0)

        # Do not reuse the pre-move scene cache for path submission/arrival.
        sid, pos, label = self._read_live_pos(session, fresh=True)
        # 强制用当前场景 id 作为 HostMove mode；读不到时让 pathfind 自取
        move_mode: int | None
        try:
            move_mode = int(sid) if sid is not None and int(sid) != 0 else None
        except Exception:
            move_mode = None
        if move_mode is not None:
            try:
                tgt.mode = int(move_mode)
            except Exception:
                pass

        dist0 = self._hz_dist_to_afk(pos, tgt)
        pos_s = (
            f"({pos[0]:.1f},{pos[1]:.1f},{pos[2]:.1f})"
            if pos is not None
            else "?"
        )
        # 默认点合法性摘要（几何上同图约几十米则正常）
        coord_note = ""
        if dist0 is not None:
            if dist0 <= 300.0:
                coord_note = f" 坐标合法(距{dist0:.1f}m)"
            elif dist0 <= 800.0:
                coord_note = f" 坐标偏远(距{dist0:.1f}m，仍尝试)"
            else:
                coord_note = f" 坐标异常偏远(距{dist0:.1f}m)"
        self._emit(
            "afk_move",
            f"成品寻路→挂机点 ({tgt.x:.1f},{tgt.y:.1f},{tgt.z:.1f})"
            f" 当前{pos_s} 距{dist0 if dist0 is not None else '?'}m"
            f" mode={move_mode if move_mode is not None else 'auto'}"
            f" · {label or sid}"
            + coord_note
            + ("" if wait_arrive else " · 单次补发"),
            ok=True,
            afk=[tgt.x, tgt.y, tgt.z],
            scene_id=sid,
            scene_label=label,
            distance=dist0,
            mode=move_mode,
        )

        # 已在阈值内：
        # - 首次到位：用 arrive_radius（宜严，默认 1.0m）
        # - 挂机补发：用 repath_min_dist（宜松，默认 3.0m，可大于首次半径）
        skip_r = float(radius)
        if not wait_arrive:
            try:
                skip_r = float(
                    getattr(cfg, "qiegao_afk_repath_min_dist", 2.0) or 2.0
                )
            except Exception:
                skip_r = 2.0
            skip_r = max(float(radius), float(skip_r))  # 补发绝不比首次更严
        if dist0 is not None and dist0 <= skip_r:
            tag = "补发" if not wait_arrive else ""
            self._emit(
                "afk_move",
                f"已在挂机点附近 距{dist0:.1f}m≤{skip_r:.1f}m，跳过{tag}寻路",
                ok=True,
                distance=dist0,
            )
            return True

        try:
            from app.core.task_api import pathfind_to_clue
        except Exception as e:
            self._emit("afk_move", f"寻路模块不可用: {e}", ok=False)
            return False

        clue = {
            "x": float(tgt.x),
            "y": float(tgt.y),
            "z": float(tgt.z),
            "name": "切糕挂机点",
            "clue": "qiegao_afk",
        }
        if move_mode is not None:
            clue["scene_id"] = int(move_mode)

        def _run_once(*, reason: str, mode_arg: int | None) -> dict:
            mode_s = str(mode_arg) if mode_arg is not None else "auto(scene)"
            self._emit(
                "afk_move",
                f"调用 pathfind_to_clue（{reason}）mode={mode_s}",
                ok=True,
                mode=mode_arg,
            )
            # 首次/等待到位：卡住≥10s 脱困；补发单次不等待，不启用
            stuck_s = 0.0
            unstick_cb = None
            death_abort_announced = False

            def abort_check():
                nonlocal death_abort_announced
                if self._qiegao_host_dead(session) is not True:
                    return False
                self._qiegao_dead_paused = True
                if not death_abort_announced:
                    death_abort_announced = True
                    self._emit(
                        "afk_hang",
                        "寻路中检测到死亡：死亡优先，立即取消寻路/脱困并等待复活",
                        ok=True,
                    )
                    try:
                        from app.core.automove import stop_automove_best_effort

                        stop_automove_best_effort(session, log=lambda _m: None)
                    except Exception:
                        pass
                return "host_dead"

            if wait_arrive:
                try:
                    stuck_s = float(getattr(cfg, "qiegao_path_stuck_s", 10.0) or 0.0)
                except Exception:
                    stuck_s = 10.0
                stuck_s = max(0.0, stuck_s)

                def unstick_cb(
                    _sess,
                    *,
                    last_pos=None,
                    target=None,
                    scene_id=None,
                    stuck_for_s=0.0,
                    attempt=1,
                    **_kw,
                ):
                    if last_pos is None or target is None:
                        return False
                    if abort_check():
                        return False
                    zone_distance = _qiegao_bridge_stuck_zone_distance(
                        scene_id, last_pos
                    )
                    if zone_distance is None:
                        # Defensive only: stuck_check prevents this callback
                        # outside the measured bridge zone.
                        return False
                    paired = False
                    peer_distance = None
                    paired, peer_distance = _qiegao_active_peer_nearby(
                        pid=int(getattr(session, "pid", self.pid) or self.pid),
                        scene_id=int(scene_id or 0),
                        position=tuple(last_pos),
                    )
                    if paired:
                        self._emit(
                            "afk_move",
                            f"桥前卡住（距实测卡点{zone_distance:.1f}m），"
                            f"附近同伴{peer_distance:.1f}m，等待双方同步脱困",
                            ok=True,
                            bridge_zone_distance=zone_distance,
                            peer_distance=peer_distance,
                        )
                    else:
                        self._emit(
                            "afk_move",
                            f"桥前卡住（距实测卡点{zone_distance:.1f}m），"
                            "附近无同伴，按单号脱困",
                            ok=True,
                            bridge_zone_distance=zone_distance,
                        )
                    return self._qiegao_unstick_nudge(
                        session,
                        last_pos=tuple(last_pos),
                        target=tuple(target),
                        scene_id=scene_id,
                        stuck_for_s=float(stuck_for_s or 0.0),
                        attempt=int(attempt or 1),
                        sync_with_peer=paired,
                        peer_distance=peer_distance,
                    )

                def stuck_check(
                    _sess,
                    *,
                    last_pos=None,
                    scene_id=None,
                    **_kw,
                ):
                    return (
                        _qiegao_bridge_stuck_zone_distance(scene_id, last_pos)
                        is not None
                    )

            hold_s = 0.0
            if wait_arrive:
                try:
                    hold_s = float(
                        getattr(cfg, "qiegao_unstick_hold_s", 25.0) or 0.0
                    )
                except Exception:
                    hold_s = 25.0
                hold_s = max(0.0, hold_s)

            return pathfind_to_clue(
                session,
                clue,
                mode=mode_arg,
                hwnd=int(self.hwnd or 0),
                use_bridge=bool(getattr(cfg, "use_bridge", True)),
                allow_remote_fallback=True,
                arrive_radius=float(radius),
                verify_timeout_s=float(timeout),
                poll_s=0.5,
                stop_event=self._stop,
                log=self.log,
                stuck_s=float(stuck_s),
                stuck_move_eps=0.8,
                stuck_check=stuck_check if wait_arrive else None,
                unstick=unstick_cb if wait_arrive else None,
                unstick_hold_s=float(hold_s),
                abort_check=abort_check if wait_arrive else None,
            )

        d = _run_once(reason="首次", mode_arg=move_mode)
        if self._stop.is_set():
            return False

        arrived = bool(d.get("arrived") or d.get("verified"))
        cmd_ok = bool(d.get("command_ok") or d.get("ok"))
        last_dist = d.get("last_distance")
        via = d.get("via")
        err = d.get("error")
        used_mode = d.get("scene_id", move_mode)

        if str(d.get("abort_reason") or "") == "host_dead":
            self._emit(
                "afk_move",
                "寻路已因死亡中断，不执行脱困或寻路失败重试",
                ok=True,
            )
            return False

        # 未到位：关挂机后仍用 scene_id 重试（不再锁 mode=0）
        if wait_arrive and not arrived:
            self._emit(
                "afk_move",
                f"寻路未到位 mode={used_mode} via={via} dist={last_dist} err={err}，关挂机后重试",
                ok=True,
            )
            self._ensure_hang_off(
                session,
                reason="寻路未到位关挂机",
                force=True,
                keep_task_guard=True,
            )
            if self._stop.is_set():
                return False
            d = _run_once(reason="关挂机后重试", mode_arg=move_mode)
            if self._stop.is_set():
                return False
            arrived = bool(d.get("arrived") or d.get("verified"))
            cmd_ok = bool(d.get("command_ok") or d.get("ok"))
            last_dist = d.get("last_distance")
            via = d.get("via")
            err = d.get("error")
            used_mode = d.get("scene_id", move_mode)

        if not wait_arrive:
            ok = bool(cmd_ok)
            self._emit(
                "afk_move",
                f"补发寻路{'OK' if ok else '失败'} mode={used_mode} via={via} err={err}",
                ok=ok,
                afk=[tgt.x, tgt.y, tgt.z],
                via=via,
                mode=used_mode,
            )
            return ok

        ok = bool(arrived)
        self._emit(
            "afk_move",
            (
                f"成品寻路{'到位' if ok else '失败'}"
                f" mode={used_mode} via={via}"
                f" dist={last_dist if last_dist is not None else '?'}"
                f" pos={d.get('last_position')}"
                + (f" err={err}" if err else "")
            ),
            ok=ok,
            afk=[tgt.x, tgt.y, tgt.z],
            distance=last_dist,
            via=via,
            arrived=arrived,
            mode=used_mode,
        )
        return ok

    def _press_hang_hotkey(self, *, note: str = "") -> bool:
        """
        Press the hang hotkey shown on Win_AutoPlayTip (default Alt+R).

        后台 KEY_HOLD 组合键（与鼠标/键盘区同一路径）；键位从 tip 读取。

        @author by ak
        """
        if not bool(getattr(self.cfg, "qiegao_press_hang_hotkey", True)):
            return False
        try:
            from app.core.bg_input import press_bg_chord_once
        except Exception as e:
            self._emit("afk_hang", f"挂机热键模块不可用: {e}", ok=False)
            return False
        hotkey = read_autoplay_tip_hotkey(self._session, log=self.log) if self._session else HANG_HOTKEY_DEFAULT
        try:
            r = press_bg_chord_once(
                int(self.pid),
                hotkey,
                hwnd=int(self.hwnd or 0),
                hold_ms=55,
                allow_softsend=False,
                log=self.log,
            )
            ok = bool(r.get("ok"))
            msg = f"后台 {hotkey}"
            if note:
                msg += f"（{note}）"
            if ok:
                msg += " 已发送"
            else:
                msg += f" 失败: {r.get('error') or 'unknown'}"
            self._emit("afk_hang", msg, ok=ok)
            # fallback FG SendInput if bg failed
            if not ok:
                return self._press_hang_hotkey_fg(hotkey=hotkey, note=note or "fg回退")
            return True
        except Exception as e:
            self._emit("afk_hang", f"后台 {hotkey} 异常: {e}", ok=False)
            return self._press_hang_hotkey_fg(hotkey=hotkey, note=note or "fg回退")

    def _press_hang_hotkey_fg(self, *, hotkey: str = HANG_HOTKEY_DEFAULT, note: str = "") -> bool:
        """Foreground SendInput hang hotkey fallback. @author by ak"""
        try:
            from app.core.bg_input import parse_key_bind_list
            from app.core.sys_input import focus_window, send_key_event
        except Exception as e:
            self._emit("afk_hang", f"FG挂机热键不可用: {e}", ok=False)
            return False
        keys = parse_key_bind_list(hotkey)
        if not keys:
            self._emit("afk_hang", f"FG挂机热键无效: {hotkey!r}", ok=False)
            return False
        hwnd = int(self.hwnd or 0)
        try:
            if hwnd:
                focus_window(hwnd, log=self.log)
            for vk in keys:
                send_key_event(int(vk), key_up=False, hwnd=hwnd or 0)
                time.sleep(0.04)
            time.sleep(0.06)
            for vk in reversed(keys):
                send_key_event(int(vk), key_up=True, hwnd=hwnd or 0)
                time.sleep(0.03)
            msg = f"前台 {hotkey}"
            if note:
                msg += f"（{note}）"
            self._emit("afk_hang", msg + " 已发送", ok=True)
            return True
        except Exception as e:
            for vk in reversed(keys):
                try:
                    send_key_event(int(vk), key_up=True, hwnd=hwnd or 0)
                except Exception:
                    pass
            self._emit("afk_hang", f"前台 {hotkey} 失败: {e}", ok=False)
            return False

    def _hang_settle(self) -> bool:
        """Wait after hang toggle. @author by ak"""
        settle = max(0.2, float(getattr(self.cfg, "qiegao_hang_settle_s", 0.9) or 0.9))
        return _sleep_interruptible(settle, self._stop)

    def _read_hang_on(self, session: GameAttachSession | None = None) -> bool | None:
        """
        Live hang state: True=开 / False=关 / None=未知.

        正式路径只走内存 CECAutoPlay+0x08（UI 不可靠，不参与判定）。

        @author by ak
        """
        sess = session or self._session
        if sess is None:
            return None
        try:
            st = probe_hang_state_mem(sess, log=self.log)
        except Exception as e:
            self._emit("afk_hang", f"挂机状态识别异常: {e}", ok=False)
            return None
        self._emit(
            "afk_hang",
            f"识别{format_hang_state(st)}",
            ok=True if st.ok else False,
            hang_on=st.on,
            hang_source=st.source,
            hang_signals=list(st.signals or []),
        )
        if st.on is True:
            self._hang_likely_on = True
        elif st.on is False:
            self._hang_likely_on = False
        if not st.ok or st.on is None:
            return None
        return bool(st.on)

    def _use_empty_skill_hang(self) -> bool:
        """是否按无技能强开/强关：只看 hang_settings 角色配置。 @author by ak"""
        try:
            hcfg = getattr(self, "_hang_cfg_used", None)
            if hcfg is not None and bool(getattr(hcfg, "empty_skill", False)):
                return True
        except Exception:
            pass
        return False

    def _resolve_hang_cfg(self, session: GameAttachSession | None = None):
        """Build HangConfig from the settings-panel snapshot and character prefs.

        常规自动副本必须使用内挂副本模式，确保副本卡怪守卫与游戏的副本
        选怪路径同时生效。这个仅是本次运行的有效配置，不会写回用户偏好。
        切糕纠偏依赖普通模式挂机圆心（CECAutoPlay 锚点）：强制普通模式，
        不读保存缓存，否则被打飞后的圆心改写纠偏不生效。无技能、半径、
        拾取、维修、活力同样全部服从挂机设置。

        @author by ak
        """
        from app.core.hang_settings import get_hang_config

        sess = session if session is not None else self._session
        cfg = get_hang_config(
            self._hang_settings or None,
            char_id=self._role_id or None,
        )
        # ActivityRunner(mode=dungeon) is the common path used by both the
        # automatic-dungeon page and scheduled dungeon tasks. The target guard
        # is intentionally gated by the game autoplay mode, so do not let a
        # stale normal-mode preference silently bypass it for this run.
        if self._is_dungeon_mode():
            cfg = replace(cfg, mode=int(AUTOPLAY_MODE_DUNGEON))
        # 切糕：锚点圆心纠偏仅普通模式生效，强制普通模式（不写回用户偏好）。
        if self._is_qiegao_mode():
            cfg = replace(cfg, mode=int(AUTOPLAY_MODE_NORMAL))
        try:
            self._hang_cfg_used = cfg
        except Exception:
            pass
        return cfg

    def _ensure_qiegao_task_guard(
        self, session: GameAttachSession | None = None
    ) -> bool:
        """Keep death/Roll maintenance alive while qiegao temporarily stops hang."""
        if self._stop.is_set() or not self._is_qiegao_mode():
            return False
        sess = session if session is not None else self._session
        if sess is None or not getattr(sess, "pid", None):
            return False
        try:
            from app.core.hang_settings import start_hang_guard

            hcfg = getattr(self, "_hang_cfg_used", None) or self._resolve_hang_cfg(sess)
            ret = start_hang_guard(
                sess,
                hcfg,
                log=self.log,
                startup_maintain=False,
            )
            ok = bool(ret.get("ok"))
            if ok and not bool(ret.get("reused")):
                self._emit(
                    "afk_hang",
                    "切糕任务守护已保持：内挂关闭时仍检测死亡并处理拾取",
                    ok=True,
                )
            return ok
        except Exception as e:
            self._emit("afk_hang", f"切糕任务守护启动失败: {e}", ok=False)
            return False

    def _ensure_hang_off(
        self,
        session: GameAttachSession | None = None,
        *,
        reason: str = "",
        force: bool = False,
        keep_task_guard: bool = False,
    ) -> bool:
        """
        关闭挂机：统一走 hang_settings.stop_hang。

        自动判定无技能（cfg / 内存推断 / Alt+R 失败 force 兜底）。

        @author by ak
        """
        from app.core.hang_settings import stop_hang

        tag = reason or "关闭挂机"
        sess = session if session is not None else self._session
        if sess is None or not getattr(sess, "pid", None):
            self._emit("afk_hang", f"{tag}：无 session，无法关挂", ok=False)
            return False

        on = self._read_hang_on(sess)
        # force=True（停止/无技能）时即使读成关也再 stop 一次：
        # 读失败或 force-open 与 running 字节不同步时，跳过会关不掉。
        if on is False and not force:
            self._emit("afk_hang", f"{tag}：已识别挂机=关，跳过", ok=True)
            self._hang_likely_on = False
            if keep_task_guard:
                self._ensure_qiegao_task_guard(sess)
            else:
                try:
                    from app.core.hang_settings import stop_hang_guard

                    stop_hang_guard(sess, log=self.log)
                except Exception:
                    pass
            return True
        if on is None and not force and self._hang_likely_on is not True:
            self._emit(
                "afk_hang",
                f"{tag}：挂机状态未知，跳过盲切（避免误开）",
                ok=True,
            )
            if keep_task_guard:
                self._ensure_qiegao_task_guard(sess)
            return False

        hcfg = getattr(self, "_hang_cfg_used", None) or self._resolve_hang_cfg(sess)
        # 若本轮用过无技能，或页勾选无技能，保证 stop 走 force
        if self._use_empty_skill_hang():
            try:
                from dataclasses import replace

                hcfg = replace(hcfg, empty_skill=True)
            except Exception:
                pass

        settle = max(0.2, float(getattr(self.cfg, "qiegao_hang_settle_s", 0.9) or 0.9))
        hwnd_i = int(getattr(sess, "hwnd", 0) or 0)
        self._emit(
            "afk_hang",
            f"{tag}：hang_settings.stop_hang empty={bool(getattr(hcfg, 'empty_skill', False))}",
            ok=True,
        )
        try:
            ret = stop_hang(
                sess,
                hcfg,
                hwnd=hwnd_i,
                settle_s=settle,
                log=self.log,
            )
        except Exception as e:
            self._emit("afk_hang", f"{tag}：stop_hang 异常 {e}", ok=False)
            if keep_task_guard:
                self._ensure_qiegao_task_guard(sess)
            return False

        if not self._hang_settle():
            if keep_task_guard:
                self._ensure_qiegao_task_guard(sess)
            return False
        on2 = self._read_hang_on(sess)
        ok = on2 is False or bool(ret.get("ok"))
        self._hang_likely_on = not ok
        via = ret.get("via") or ("force" if self._use_empty_skill_hang() else "altr")
        self._emit(
            "afk_hang",
            f"{tag}：{'OK' if ok else '失败'} via={via} run={on2} {ret.get('message') or ''}",
            ok=ok,
        )
        if keep_task_guard and not self._stop.is_set():
            self._ensure_qiegao_task_guard(sess)
        return bool(ok)

    def _ensure_hang_on(self, session: GameAttachSession | None = None) -> bool:
        """
        开启挂机：统一走 hang_settings.start_hang（含 prepare/maintain/guard）。

        @author by ak
        """
        from app.core.hang_settings import start_hang

        if not bool(getattr(self.cfg, "qiegao_press_hang_hotkey", True)):
            return False
        sess = session if session is not None else self._session
        if sess is None or not getattr(sess, "pid", None):
            self._emit("afk_hang", "开挂：无 session", ok=False)
            return False

        hcfg = self._resolve_hang_cfg(sess)
        settle = max(0.2, float(getattr(self.cfg, "qiegao_hang_settle_s", 0.9) or 0.9))
        hwnd_i = int(getattr(sess, "hwnd", 0) or 0)
        self._emit(
            "afk_hang",
            f"开挂：hang_settings.start_hang mode={getattr(hcfg, 'mode', '?')} "
            f"empty={bool(getattr(hcfg, 'empty_skill', False))} r={getattr(hcfg, 'radius', '?')} "
            f"dungeon_guard={'requested' if (int(getattr(hcfg, 'mode', -1)) == int(AUTOPLAY_MODE_DUNGEON) and bool(getattr(hcfg, 'ignore_dungeon_stuck', False))) else 'off'}",
            ok=True,
        )
        try:
            ret = start_hang(
                sess,
                hcfg,
                hwnd=hwnd_i,
                settle_s=settle,
                maintain=True,
                log=self.log,
            )
        except Exception as e:
            self._emit("afk_hang", f"start_hang 异常: {e}", ok=False)
            self._hang_likely_on = False
            return False

        if not self._hang_settle():
            return False
        on2 = self._read_hang_on(sess)
        ok = on2 is True or bool(ret.get("ok"))
        self._hang_likely_on = bool(ok)
        via = ret.get("via") or ("force" if getattr(hcfg, "empty_skill", False) else "altr")
        self._emit(
            "afk_hang",
            f"开挂{'成功' if ok else '失败'} via={via} run={on2} {ret.get('message') or ''}",
            ok=ok,
        )
        return bool(ok)

    def _game_side_stop(self) -> None:
        """
        停止时同步停止游戏侧：识别后关挂机 + 停寻路.

        不依赖可能被 worker finally 清空的 self._session：
        始终用 self.pid 重新 attach 一次独立 session。

        注意：本函数禁止再局部 import open_attach_session（会遮蔽模块导入，
        触发 UnboundLocalError，导致无技能挂机关不掉）。

        @author by ak
        """
        pid = int(self.pid or 0)
        session = None
        owned = False
        if pid > 0 and _pid_alive(pid):
            try:
                session = open_attach_session(pid, log=self.log)
                owned = True
            except Exception as e:
                self.log(f"activity stop re-attach: {e}")
                session = self._session
                try:
                    if session is not None and not getattr(session, "pid", None):
                        session.pid = pid
                except Exception:
                    pass
        else:
            session = self._session

        # 无技能 / 软件侧认为开着：即使读状态失败也必须 force 关一次
        must_force = bool(self._use_empty_skill_hang() or self._hang_likely_on is True)
        try:
            if session is not None and getattr(session, "pid", None):
                on = self._read_hang_on(session)
                if on is True or must_force:
                    why = (
                        "识别挂机=开"
                        if on is True
                        else (
                            "无技能/软件侧认为开着"
                            if must_force
                            else "状态未知"
                        )
                    )
                    self._emit("afk_hang", f"停止：{why}，关闭", ok=True)
                    ok = self._ensure_hang_off(
                        session, reason="停止关挂机", force=bool(must_force or on is True)
                    )
                    # busy / 强关失败时再试一次 force
                    if not ok and must_force:
                        self._ensure_hang_off(
                            session, reason="停止关挂机重试", force=True
                        )
                elif on is False:
                    self._emit("afk_hang", "停止：识别挂机=关，无需关", ok=True)
                    try:
                        from app.core.hang_settings import stop_hang_guard

                        stop_hang_guard(session, log=self.log)
                    except Exception:
                        pass
                    self._hang_likely_on = False
                else:
                    self._emit(
                        "afk_hang",
                        "停止：挂机状态未知且软件侧未开，跳过",
                        ok=True,
                    )
            elif must_force and pid > 0 and _pid_alive(pid):
                try:
                    s2 = open_attach_session(int(pid), log=self.log)
                    try:
                        self._ensure_hang_off(s2, reason="停止关挂机", force=True)
                    finally:
                        try:
                            s2.close()
                        except Exception:
                            pass
                except Exception as e:
                    self.log(f"停止关挂失败: {e}")
                self._hang_likely_on = False
        except Exception as e:
            self.log(f"activity stop hang: {e}")
        try:
            if session is not None and getattr(session, "pid", None):
                from app.core.automove import stop_automove_best_effort

                r = stop_automove_best_effort(session, log=self.log)
                ok = bool(getattr(r, "ok", False))
                self._emit(
                    "afk_move",
                    "停止：已尝试取消寻路(停到当前位置)"
                    + ("" if ok else f" fail={getattr(r, 'error', None)}"),
                    ok=ok,
                )
            else:
                self._emit(
                    "afk_move",
                    f"停止：无法停寻路（无可用 session pid={pid}）",
                    ok=False,
                )
        except Exception as e:
            self.log(f"activity stop move: {e}")
        finally:
            if owned and session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def _qiegao_alt_entry_hold_s(self) -> float:
        """
        小号进本过图稳定后的站桩等待时长（等主号完成桥触发阶段）。

        0 = 跟随主号 qiegao_bridge_wait_s（默认 15s）；配置 qiegao_alt_hold_s
        > 0 时用该值覆盖。

        @author by ak
        """
        hold = 0.0
        try:
            v = getattr(self.cfg, "qiegao_alt_hold_s", 0.0) or 0.0
            hold = max(0.0, float(v))
        except Exception:
            hold = 0.0
        if hold <= 0:
            hold = max(
                0.0, float(getattr(self.cfg, "qiegao_bridge_wait_s", 15.0) or 0.0)
            )
        return hold

    def _qiegao_enter_bridge_phase(self, session: GameAttachSession) -> bool:
        """
        主号进本切图稳定后先走「桥触发阶段」：寻路到桥触发点触发阶段推进。

        主号先到桥侧并停留数秒让阶段动画推进（桥空气墙消失），再交给正常挂机
        寻路（_qiegao_start_hang_phase）。本阶段专治空气墙卡住，因此不再启用
        桥区 airwall 脱困/等待逻辑（stuck_s=0 / 不传 stuck_check/unstick）。

        小号/队员流程不调用本方法，仍走原「等主号带入 → 过图稳定 → 挂机」。

        Returns False when stopped / aborted; True otherwise（路径未严格到位也
        接受，停留等待阶段推进即可）。

        @author by ak
        """
        try:
            enabled = bool(getattr(self.cfg, "qiegao_bridge_phase_enabled", True))
        except Exception:
            enabled = True
        if not enabled:
            return True
        bx = getattr(self.cfg, "qiegao_bridge_path_x", None)
        by = getattr(self.cfg, "qiegao_bridge_path_y", None)
        bz = getattr(self.cfg, "qiegao_bridge_path_z", None)
        if bx is None or by is None or bz is None:
            bx, by, bz = QIEGAO_BRIDGE_PATH
        try:
            bx, by, bz = float(bx), float(by), float(bz)
        except Exception:
            self.log(f"activity qiegao bridge invalid xyz=({bx},{by},{bz})")
            return True
        window_s = max(0.0, float(getattr(self.cfg, "qiegao_bridge_wait_s", 15.0) or 0.0))
        stay_s = max(0.0, float(getattr(self.cfg, "qiegao_bridge_stay_s", 7.0) or 0.0))
        path_timeout = max(5.0, float(getattr(self.cfg, "qiegao_bridge_path_timeout_s", 12.0) or 12.0))
        radius = max(2.0, float(getattr(self.cfg, "qiegao_bridge_path_radius", 4.0) or 4.0))

        t0 = time.monotonic()
        self._emit(
            "afk_bridge",
            f"进图稳定：主号先寻路到桥触发点 ({bx:.1f},{by:.1f},{bz:.1f})"
            f" 触发墙面消失（窗口{window_s:.0f}s 停留{stay_s:.0f}s）",
            ok=True,
            afk=[bx, by, bz],
        )
        # 走路前关内挂（只关识别到开着的）
        self._ensure_hang_off(
            session,
            reason="桥触发段关挂机",
            force=False,
            keep_task_guard=True,
        )
        if self._stop.is_set():
            return False
        if self._qiegao_host_dead(session) is True:
            if not self._qiegao_wait_until_revived(session, reason="桥触发"):
                return False

        near = False
        try:
            _sid, pos, _lab = self._read_live_pos(session, fresh=True)
            if pos is not None and len(pos) >= 3:
                near = math.hypot(float(pos[0]) - bx, float(pos[2]) - bz) <= radius
        except Exception:
            near = False

        if not near:
            try:
                from app.core.task_api import pathfind_to_clue
            except Exception as e:
                self._emit("afk_bridge", f"寻路模块不可用: {e}", ok=False)
                return True
            clue = {
                "x": bx,
                "y": by,
                "z": bz,
                "name": "切糕桥触发点",
                "clue": "qiegao_bridge_phase",
            }
            move_mode = None
            try:
                _sid2, _p2, _l2 = self._read_live_pos(session, fresh=True)
                if _sid2 is not None and int(_sid2) != 0:
                    move_mode = int(_sid2)
                if move_mode is not None:
                    clue["scene_id"] = int(move_mode)
            except Exception:
                pass
            death_abort_announced = False

            def abort_check():
                nonlocal death_abort_announced
                if self._qiegao_host_dead(session) is not True:
                    return False
                self._qiegao_dead_paused = True
                if not death_abort_announced:
                    death_abort_announced = True
                    self._emit(
                        "afk_bridge",
                        "桥触发寻路中检测到死亡：暂停寻路并等待复活",
                        ok=True,
                    )
                return "host_dead"

            d = pathfind_to_clue(
                session,
                clue,
                mode=move_mode,
                hwnd=int(self.hwnd or 0),
                use_bridge=bool(getattr(self.cfg, "use_bridge", True)),
                allow_remote_fallback=True,
                arrive_radius=radius,
                verify_timeout_s=path_timeout,
                poll_s=0.5,
                stop_event=self._stop,
                stuck_s=0.0,
                abort_check=abort_check,
                log=self.log,
            )
            if self._stop.is_set():
                return False
            if str(d.get("abort_reason") or "") == "host_dead":
                return False
            arrived = bool(d.get("arrived") or d.get("verified"))
            dist = d.get("last_distance")
            err = d.get("error")
            self._emit(
                "afk_bridge",
                f"桥触发寻路{'到位' if arrived else '已发出'} "
                f"dist={dist if dist is not None else '?'}m"
                + (f" err={err}" if err else ""),
                ok=arrived,
                distance=dist,
                afk=[bx, by, bz],
            )
        else:
            self._emit(
                "afk_bridge",
                "已在桥触发点附近，直接停留等待阶段推进",
                ok=True,
            )

        if self._stop.is_set():
            return False
        if self._qiegao_host_dead(session) is True:
            if not self._qiegao_wait_until_revived(session, reason="桥触发停留"):
                return False
        # 桥触发停留：触发阶段动画推进（墙面消失）
        if stay_s > 0:
            if not _sleep_interruptible(stay_s, self._stop):
                return False
        # 预留窗口补齐（整个桥阶段 ≥ 配置窗口）
        used = time.monotonic() - t0
        remain = window_s - used
        if remain > 0:
            self._emit(
                "afk_bridge",
                f"桥阶段预留窗口剩余 {remain:.0f}s",
                ok=True,
                remain_s=remain,
            )
            if not _sleep_interruptible(remain, self._stop):
                return False
        return True

    def _qiegao_start_hang_phase(
        self,
        session: GameAttachSession,
    ) -> bool:
        """
        切糕挂机段：识别挂机 → 若开则关 → 寻路到位 → 开挂机 → 等到回城.

        注意：本函数假定「已经在副本地图内」。
        - 城内开：调用前须先 wait_enter + 过图就绪
        - 已在本内：直接调用，不做过图等待

        @author by ak
        """
        self._emit(
            "afk_hang",
            "[v3] 切糕挂机：识别挂机 → 关挂机 → 寻路到位 → 开挂机",
            ok=True,
        )
        # 额外阶段等待（配置项，默认 0；过图等待不在此处）
        stage_s = max(0.0, float(getattr(self.cfg, "qiegao_stage_wait_s", 0.0) or 0.0))
        if stage_s > 0:
            self._emit("afk_wait", f"额外等待 {stage_s:.0f}s 再寻路", ok=True)
            if not _sleep_interruptible(stage_s, self._stop):
                return False
        if self._qiegao_host_dead(session, force=True) is True:
            if not self._qiegao_wait_until_revived(session, reason="寻路"):
                return False
        # 寻路前：只有识别到开着才关（挂机开着会干扰走路）
        self._ensure_hang_off(
            session,
            reason="寻路前关挂机",
            force=False,
            keep_task_guard=True,
        )
        # 必须先走到挂机点，再开挂机（未到位绝不开内挂）
        cfg = self.cfg
        try:
            path_timeout = max(
                30.0, float(getattr(cfg, "qiegao_path_timeout_s", 180.0) or 180.0)
            )
        except Exception:
            path_timeout = 180.0
        try:
            arrive_r = max(
                0.5, float(getattr(cfg, "qiegao_arrive_radius", 1.0) or 1.0)
            )
        except Exception:
            arrive_r = 1.0
        try:
            air_wait = max(
                2.0, float(getattr(cfg, "qiegao_airwall_retry_s", 10.0) or 10.0)
            )
        except Exception:
            air_wait = 10.0
        path_deadline = time.time() + path_timeout
        arrived = False
        attempt = 0
        last_dist = None
        while time.time() < path_deadline:
            if self._stop.is_set():
                return False
            if self._qiegao_host_dead(session) is True:
                if not self._qiegao_wait_until_revived(session, reason="寻路"):
                    return False
                # Revived: re-enter this iteration before sending a path packet.
                continue
            attempt += 1
            moved_ok = self._move_to_qiegao_afk(session, wait_arrive=True)
            if self._stop.is_set():
                return False
            if self._qiegao_host_dead(session) is True:
                if not self._qiegao_wait_until_revived(session, reason="寻路"):
                    return False
                continue
            # 严格实时距离：必须 ≤ 1m 且经过收尾稳定等待才开挂。不能读
            # state_dispatch 的旧坐标，否则 pathfind 已到位仍会误判在原地。
            try:
                tgt = self._qiegao_afk_target(session)
                _sid, pos, _lab = self._read_live_pos(session, fresh=True)
                dist = self._hz_dist_to_afk(pos, tgt) if tgt else None
            except Exception:
                dist = None
            last_dist = dist
            if dist is not None and dist <= arrive_r:
                try:
                    arrival_settle = max(
                        0.2,
                        float(
                            getattr(cfg, "qiegao_arrival_settle_s", 0.8) or 0.8
                        ),
                    )
                except Exception:
                    arrival_settle = 0.8
                self._emit(
                    "afk_move",
                    f"首次到位 距{dist:.1f}m≤{arrive_r:.1f}m，"
                    f"稳定等待 {arrival_settle:.1f}s 后复核",
                    ok=True,
                    distance=dist,
                )
                if not _sleep_interruptible(arrival_settle, self._stop):
                    return False
                if self._qiegao_host_dead(session) is True:
                    if not self._qiegao_wait_until_revived(session, reason="到位后"):
                        return False
                    continue
                try:
                    tgt = self._qiegao_afk_target(session)
                    _sid, pos, _lab = self._read_live_pos(session, fresh=True)
                    settled_dist = self._hz_dist_to_afk(pos, tgt) if tgt else None
                except Exception:
                    settled_dist = None
                last_dist = settled_dist
                if settled_dist is None or settled_dist > arrive_r:
                    self._emit(
                        "afk_move",
                        f"到位后复核未通过 dist="
                        f"{settled_dist if settled_dist is not None else '?'}m，继续寻路",
                        ok=False,
                        distance=settled_dist,
                    )
                    dist = settled_dist
                else:
                    dist = settled_dist
                    self._emit(
                        "afk_move",
                        f"已确认走到挂机点并稳定 距{dist:.1f}m≤{arrive_r:.1f}m，准备开挂机",
                        ok=True,
                        distance=dist,
                    )
                    arrived = True
                    break

            if dist is not None and dist <= arrive_r:
                self._emit(
                    "afk_move",
                    "到位复核异常，继续寻路",
                    ok=False,
                    distance=dist,
                )
            if moved_ok and dist is not None and dist > arrive_r:
                self._emit(
                    "afk_move",
                    f"寻路回报到位但实测距{dist:.1f}m>{arrive_r:.1f}m，"
                    f"空气墙等待 {air_wait:.0f}s 后再寻路",
                    ok=False,
                    distance=dist,
                )
            else:
                self._emit(
                    "afk_move",
                    f"第{attempt}次寻路未到位 dist={dist if dist is not None else '?'}m"
                    f"（严格{arrive_r:.1f}m），空气墙等待 {air_wait:.0f}s 后再寻路"
                    f"（剩余{max(0.0, path_deadline - time.time()):.0f}s）",
                    ok=False,
                )
            # 每次重试前确保挂机关着
            self._ensure_hang_off(
                session,
                reason="未到位重试前关挂机",
                force=False,
                keep_task_guard=True,
            )
            # 空气墙：多等一会再寻路，避免连续顶墙
            if not _sleep_interruptible(float(air_wait), self._stop):
                return False

        if not arrived:
            self._emit(
                "afk_move",
                f"超时{path_timeout:.0f}s 仍未走到挂机点（严格{arrive_r:.1f}m，最后距"
                f"{last_dist if last_dist is not None else '?'}m），"
                "不开挂机，本段失败",
                ok=False,
            )
            return False

        started = self._ensure_hang_on(session)
        if self._stop.is_set():
            return False
        if not started:
            # The first toggle can race the final movement/UI settle. Recheck
            # the real position, then retry once; never enter the long wait
            # loop with the hang actually off.
            self._emit("afk_hang", "开启挂机未确认，原地稳定后重试一次", ok=False)
            if not _sleep_interruptible(0.8, self._stop):
                return False
            try:
                tgt = self._qiegao_afk_target(session)
                _sid, pos, _lab = self._read_live_pos(session, fresh=True)
                retry_dist = self._hz_dist_to_afk(pos, tgt) if tgt else None
            except Exception:
                retry_dist = None
            if retry_dist is None or retry_dist > arrive_r:
                self._emit(
                    "afk_hang",
                    f"开启挂机取消：重试前位置未确认 dist="
                    f"{retry_dist if retry_dist is not None else '?'}m",
                    ok=False,
                )
                return False
            started = self._ensure_hang_on(session)
            if self._stop.is_set():
                return False
        if not started:
            self._emit("afk_hang", "开启挂机失败，停止本段（不站桩等待）", ok=False)
            return False
        if not _sleep_interruptible(0.3, self._stop):
            return False
        return self._qiegao_hang_until_return(session)

    def _qiegao_settle_city_return(
        self,
        session: GameAttachSession,
    ) -> None:
        """Wait for city loading to settle, then stop hang and movement."""
        try:
            min_s = max(
                2.0,
                float(getattr(self.cfg, "qiegao_return_settle_min_s", 5.0) or 5.0),
            )
        except Exception:
            min_s = 5.0
        try:
            stable_need = max(
                1.0,
                float(getattr(self.cfg, "qiegao_return_stable_s", 2.0) or 2.0),
            )
        except Exception:
            stable_need = 2.0
        try:
            timeout = max(
                min_s,
                float(
                    getattr(self.cfg, "qiegao_return_settle_timeout_s", 20.0)
                    or 20.0
                ),
            )
        except Exception:
            timeout = 20.0

        self._emit(
            "afk_wait",
            f"已回城，等待切图稳定后再关挂（至少{min_s:.0f}s）",
            ok=True,
        )
        started = time.time()
        deadline = started + timeout
        stable_since: float | None = None
        last_sid: int | None = None
        settled = False
        while time.time() < deadline and not self._stop.is_set():
            try:
                sid, pos, label = self._read_live_pos(session, fresh=True)
                in_city = is_city_scene(sid, label, self.cfg.city_gate)
                pos_ok = pos is not None and len(pos) >= 3
                if in_city and pos_ok:
                    same_scene = (
                        last_sid is None
                        or sid is None
                        or int(last_sid) == int(sid)
                    )
                    # The hang may legitimately move in town. Loading is
                    # considered stable once the same city scene and readable
                    # coordinates persist; exact position is irrelevant.
                    if same_scene:
                        if stable_since is None:
                            stable_since = time.time()
                    else:
                        stable_since = None
                    last_sid = int(sid) if sid is not None else None
                    if (
                        stable_since is not None
                        and time.time() - stable_since >= stable_need
                        and time.time() - started >= min_s
                    ):
                        settled = True
                        break
                else:
                    stable_since = None
                    last_sid = None
            except Exception:
                stable_since = None
                last_sid = None
            if not _sleep_interruptible(0.5, self._stop):
                return

        if self._stop.is_set():
            return
        if not settled:
            self._emit(
                "afk_wait",
                "回城切图稳定等待超时，本轮不调用关挂或移动函数",
                ok=False,
            )
            return
        self._emit(
            "afk_hang",
            "回城切图已稳定，关闭挂机并停止移动",
            ok=True,
        )
        stopped = self._ensure_hang_off(
            session,
            reason="回城关挂机",
            force=True,
            keep_task_guard=True,
        )
        if not stopped and not self._stop.is_set():
            if _sleep_interruptible(0.4, self._stop):
                self._ensure_hang_off(
                    session,
                    reason="回城关挂机重试",
                    force=True,
                    keep_task_guard=True,
                )
        try:
            from app.core.automove import stop_automove_best_effort

            stop_automove_best_effort(session, log=lambda _m: None)
        except Exception:
            pass
        if _sleep_interruptible(0.5, self._stop):
            try:
                from app.core.automove import stop_automove_best_effort

                stop_automove_best_effort(session, log=lambda _m: None)
            except Exception:
                pass
        if self._stop.is_set():
            return

        try:
            near_m = max(
                5.0,
                min(
                    10.0,
                    float(getattr(self.cfg, "qiegao_return_near_m", 8.0) or 8.0),
                ),
            )
        except Exception:
            near_m = 8.0
        try:
            peer_wait = max(
                2.0,
                float(
                    getattr(self.cfg, "qiegao_return_peer_wait_s", 30.0) or 30.0
                ),
            )
        except Exception:
            peer_wait = 30.0
        # 回城稳定后：开启组队跟随，游戏会把两号自动拉到一起（不再手动走中点）。
        follow_ok = False
        try:
            from app.core.team_ops import set_team_follow

            fr = set_team_follow(
                session, enabled=True, use_ui_click=False, log=self.log
            )
            follow_ok = bool(fr.ok)
            if follow_ok:
                self._emit(
                    "afk_move",
                    "回城稳定，已开启组队跟随，等待两号聚拢",
                    ok=True,
                )
            else:
                self._emit(
                    "afk_move",
                    f"开启组队跟随失败: {fr.message}",
                    ok=False,
                )
        except Exception as e:
            self._emit("afk_move", f"开启组队跟随失败: {e}", ok=False)

        converged = False
        if follow_ok:
            deadline = time.time() + peer_wait
            while time.time() < deadline and not self._stop.is_set():
                try:
                    city_sid, city_pos, city_label = self._read_live_pos(
                        session, fresh=True
                    )
                    if (
                        not is_city_scene(
                            city_sid, city_label, self.cfg.city_gate
                        )
                        or city_pos is None
                        or len(city_pos) < 3
                    ):
                        break
                    current = tuple(float(v) for v in city_pos[:3])
                    _target, peers, separation = _qiegao_city_rendezvous(
                        pid=int(getattr(session, "pid", self.pid) or self.pid),
                        scene_id=int(city_sid or 0),
                        position=current,
                        stop_event=self._stop,
                        wait_s=min(2.0, max(0.0, deadline - time.time())),
                        near_m=near_m,
                    )
                    if (
                        int(peers or 0) >= 2
                        and separation is not None
                        and float(separation) <= near_m
                    ):
                        converged = True
                        break
                except Exception:
                    break
                if not _sleep_interruptible(1.0, self._stop):
                    break
        if self._stop.is_set():
            return
        if converged:
            self._emit(
                "afk_move",
                f"回城两号已聚拢（≤{near_m:.1f}m），保持原地",
                ok=True,
            )
        elif not follow_ok:
            self._emit("afk_move", "回城未开启组队跟随，保持原地", ok=True)
        else:
            self._emit(
                "afk_move",
                f"回城等待两号聚拢超时（{peer_wait:.0f}s），保持原地",
                ok=False,
            )

    def _qiegao_hang_until_return(self, session: GameAttachSession) -> bool:
        """
        Stay at the hang point until the game moves the character out of the dungeon.

        Periodically re-issue move so knockback does not strand the character.

        @author by ak
        """
        cfg = self.cfg
        repath = max(0.0, float(getattr(cfg, "qiegao_afk_repath_s", 90.0) or 0.0))
        poll = max(1.0, float(getattr(cfg, "return_poll_s", 15.0) or 15.0))
        arrive_r = max(0.5, float(getattr(cfg, "qiegao_arrive_radius", 1.0) or 1.0))
        # 补发阈值宜松：默认 3.0m（偏移不大不回正）；勿用 min(arrive) 压成更严
        repath_min = float(getattr(cfg, "qiegao_afk_repath_min_dist", 2.0) or 2.0)
        repath_min = max(arrive_r, repath_min)  # 补发 2m，严格到位 1m
        remote_cool_s = max(0.0, float(getattr(cfg, "qiegao_remote_cool_s", 120.0) or 0.0))
        # 副本倒计时剩余 ≤ 该值即关闭内挂（0 = 不提前关）。
        end_stop_s = max(0.0, float(getattr(cfg, "qiegao_stop_hang_before_end_s", 30.0) or 0.0))
        hang_stopped_for_end = False
        next_repath = 0.0
        remote_cool_until = 0.0
        death_rearm_attempted = False
        analyzer = None
        if bool(getattr(cfg, "qiegao_analyze_mob_freq", False)):
            try:
                from app.core.mob_freq_analyze import MobFreqAnalyzer

                # 挂机站桩期间强制不低于 30s，降低 CreateRemoteThread 压力
                mf_iv = float(
                    getattr(cfg, "qiegao_mob_freq_interval_s", 45.0) or 45.0
                )
                mf_iv = max(30.0, mf_iv)
                analyzer = MobFreqAnalyzer(
                    interval_s=mf_iv,
                    radius=float(
                        getattr(cfg, "qiegao_mob_freq_radius", 200.0) or 200.0
                    ),
                    log=self.log,
                )
                self._emit(
                    "afk_hang",
                    f"分析怪频：已开启（间隔≥{mf_iv:.0f}s，与补发寻路互斥）",
                    ok=True,
                )
            except Exception as e:
                self._emit("afk_hang", f"分析怪频初始化失败: {e}", ok=False)
                analyzer = None
        self._emit(
            "afk_hang",
            "挂机中（内挂·普通挂机）等待离开副本",
            ok=True,
            ok_count=int(self._ok_count),
            run_index=int(self._ok_count) + 1,
        )
        try:
            if analyzer is not None:
                try:
                    msg0 = analyzer.maybe_tick(session, force=True)
                    if msg0:
                        self._emit("mob_freq", msg0, ok=True)
                except Exception as e:
                    self._emit("mob_freq", f"分析怪频首采样失败: {e}", ok=False)
            while not self._stop.is_set():
                if not _pid_alive(self.pid):
                    self._emit(
                        "game_dead",
                        f"游戏进程已退出 pid={self.pid}",
                        ok=False,
                    )
                    return False
                try:
                    # 优先读标题栏统一监听缓存，避免挂机循环重复 RPM 扫图
                    sid, _scene_pos, label = self._scene_prefer_hub(
                        session, max_age_s=2.5
                    )
                except Exception as e:
                    if _is_process_gone_error(e) or not _pid_alive(self.pid):
                        self._emit(
                            "game_dead",
                            f"游戏进程已退出 pid={self.pid}",
                            ok=False,
                        )
                        return False
                    raise
                # 回城，或明确切到非副本地图（手动退出/被踢）；地图未知时不误判
                has_map = bool(label) or sid is not None
                in_city = is_city_scene(sid, label, cfg.city_gate)
                left_dungeon = has_map and (not in_city) and (
                    not is_dungeon_scene(sid, label, gate=cfg.city_gate)
                )
                if in_city or left_dungeon:
                    if analyzer is not None:
                        try:
                            self._emit("mob_freq", analyzer.final_report(), ok=True)
                        except Exception:
                            pass
                    why = (
                        f"已回城 {label}"
                        if in_city
                        else f"已离开副本地图 {label or sid}"
                    )
                    self._emit(
                        "wait_return",
                        f"{why} scene={sid}",
                        ok=True,
                        scene_id=sid,
                        scene_label=label,
                    )
                    if in_city:
                        self._qiegao_settle_city_return(session)
                    return True
                now = time.time()
                dead = self._qiegao_host_dead(session)
                if dead is True:
                    if not self._qiegao_dead_paused:
                        self._emit(
                            "afk_hang",
                            "角色死亡，任务守护继续处理拾取，暂停补发寻路/扫怪并等待复活",
                            ok=True,
                        )
                        self._qiegao_dead_paused = True
                    if not death_rearm_attempted:
                        death_rearm_attempted = True
                        on = self._read_hang_on(session)
                        if on is False and not hang_stopped_for_end:
                            self._emit(
                                "afk_hang",
                                "死亡打断了游戏内挂，任务内重启一次以触发副本复活",
                                ok=True,
                            )
                            self._ensure_hang_on(session)
                        self._ensure_qiegao_task_guard(session)
                    if not _sleep_interruptible(
                        poll, self._stop
                    ):
                        return False
                    continue
                if dead is False and self._qiegao_dead_paused:
                    self._qiegao_dead_paused = False
                    death_rearm_attempted = False
                    self._emit(
                        "afk_hang",
                        "角色已复活，恢复挂机补发寻路/扫怪",
                        ok=True,
                    )
                # 远程失败冷却：不再改写圆心 / 扫怪，避免继续 CRT 打崩客户端
                if remote_cool_until > 0 and now < remote_cool_until:
                    cool_left = remote_cool_until - now
                    if repath > 0 and now >= next_repath:
                        self._emit(
                            "afk_hang",
                            f"远程调用冷却中 {cool_left:.0f}s（跳过圆心改写/扫怪）",
                            ok=True,
                        )
                        next_repath = now + max(repath, 30.0)
                else:
                    # 挂机纠偏：仅当离开挂机半径（被撞飞）才真正改写挂机圆心
                    if repath > 0 and now >= next_repath:
                        tgt = self._qiegao_afk_target(session)
                        if tgt is not None:
                            dist_now = None
                            pos3 = None
                            try:
                                _s3, pos3, _l3 = self._scene_prefer_hub(
                                    session, max_age_s=2.5
                                )
                                dist_now = self._hz_dist_to_afk(pos3, tgt)
                            except Exception:
                                dist_now = None
                            next_repath = now + repath
                            if dist_now is not None and dist_now <= repath_min:
                                # Normal in-range heartbeat: no action and no
                                # log/event.  Only an actual correction below
                                # should enter the long-running activity log.
                                pass
                            else:
                                # 挂机纠偏：被打飞后回到固定挂机点，不改写
                                # 挂机圆心，也不重新执行主号桥触发阶段。
                                self._emit(
                                    "afk_move",
                                    f"挂机纠偏：偏离固定挂机点 {dist_now:.1f}m，关闭挂机并回挂机点",
                                    ok=True,
                                    distance=dist_now,
                                )
                                self._ensure_hang_off(
                                    session,
                                    reason="挂机纠偏前关挂机",
                                    force=False,
                                    keep_task_guard=True,
                                )
                                if not self._stop.is_set():
                                    moved_back = self._move_to_qiegao_afk(
                                        session, wait_arrive=True
                                    )
                                    if moved_back and not self._stop.is_set():
                                        started_back = self._ensure_hang_on(session)
                                        self._emit(
                                            "afk_move",
                                            "挂机纠偏：已回到固定挂机点并恢复挂机",
                                            ok=bool(started_back),
                                        )
                                    else:
                                        self._emit(
                                            "afk_move",
                                            "挂机纠偏：返回固定挂机点失败",
                                            ok=False,
                                        )
                    # 分析怪频：失败硬停后关闭本局
                    if analyzer is not None:
                        try:
                            host_pos = None
                            try:
                                _sid2, pos2, _lab2 = self._scene_prefer_hub(
                                    session, max_age_s=2.5
                                )
                                if pos2 and len(pos2) >= 3:
                                    host_pos = (
                                        float(pos2[0]),
                                        float(pos2[1]),
                                        float(pos2[2]),
                                    )
                            except Exception:
                                host_pos = None
                            from app.core.mob_freq_analyze import MobFreqHardStop

                            msg = analyzer.maybe_tick(session, host_pos=host_pos)
                            if msg:
                                self._emit("mob_freq", msg, ok=True)
                        except MobFreqHardStop as e:
                            self._emit(
                                "mob_freq",
                                f"分析怪频硬停（停止本局扫怪）: {e}",
                                ok=False,
                            )
                            try:
                                self._emit(
                                    "mob_freq", analyzer.final_report(), ok=True
                                )
                            except Exception:
                                pass
                            analyzer = None
                            remote_cool_until = now + remote_cool_s
                        except Exception as e:
                            es = str(e)
                            self._emit(
                                "mob_freq", f"分析怪频采样失败: {es}", ok=False
                            )
                            low = es.lower()
                            if any(
                                k in low
                                for k in (
                                    "hard-stop",
                                    "virtualalloc",
                                    "access is denied",
                                    "denied",
                                )
                            ):
                                try:
                                    self._emit(
                                        "mob_freq",
                                        analyzer.final_report(),
                                        ok=True,
                                    )
                                except Exception:
                                    pass
                                analyzer = None
                                remote_cool_until = now + remote_cool_s
                # 游戏内副本结束倒计时（Win_InstanceConfig.Txt_Time）
                inst_left = None
                inst_txt = ""
                try:
                    cd = read_instance_countdown_remain_s(session, log=lambda _m: None)
                    if cd.get("ok") and cd.get("remain_s") is not None:
                        inst_left = int(cd["remain_s"])
                        inst_txt = str(cd.get("text") or "")
                except Exception:
                    inst_left = None
                if (
                    end_stop_s > 0
                    and inst_left is not None
                    and inst_left <= end_stop_s
                    and not hang_stopped_for_end
                ):
                    hang_stopped_for_end = True
                    self._emit(
                        "afk_hang",
                        f"副本倒计时 {inst_left}s≤{end_stop_s:.0f}s，关闭内挂等待离开副本",
                        ok=True,
                        instance_remain_s=inst_left,
                        countdown_stop=True,
                    )
                    self._ensure_hang_off(
                        session,
                        reason="副本倒计时关内挂",
                        force=False,
                        keep_task_guard=True,
                    )
                if inst_left is not None:
                    msg = (
                        f"挂机中… 副本倒计时 {format_instance_remain(inst_left)}"
                        f"（{inst_left}s）· {label or sid}"
                    )
                else:
                    msg = f"挂机中… 等待离开副本 · {label or sid}"
                self._emit(
                    "afk_hang",
                    msg,
                    ok=True,
                    log_event=False,
                    scene_id=sid,
                    scene_label=label,
                    instance_remain_s=inst_left,
                    instance_remain_text=inst_txt,
                )
                if not _sleep_interruptible(poll, self._stop):
                    if analyzer is not None:
                        try:
                            self._emit("mob_freq", analyzer.final_report(), ok=True)
                        except Exception:
                            pass
                    return False
            if analyzer is not None:
                try:
                    self._emit("mob_freq", analyzer.final_report(), ok=True)
                except Exception:
                    pass
            return False
        finally:
            pass

    def _one_round(self, session: GameAttachSession) -> None:
        cfg = self.cfg
        self._rounds += 1
        pts = self._refresh_points(session)
        src = "live" if self._points_live else "cache"
        # 用户语义：第 N 次 = 下一趟（已完成 ok_count 次后的第 ok_count+1 次）
        # 勿用 _rounds（内部循环次数，含中断重试，易被当成「进了 N 次本」）
        run_n = int(self._ok_count) + 1
        if self._is_qiegao_mode():
            alt_tag = "·小号" if bool(getattr(cfg, "qiegao_is_alt", False)) else ""
            round_msg = f"准备第 {run_n} 次（切糕·{QIEGAO_TASK_LABEL}{alt_tag}）"
        elif self._is_dungeon_mode():
            round_msg = f"准备第 {run_n} 次（副本）"
        else:
            round_msg = (
                f"准备第 {run_n} 次 points={pts}/{cfg.target_points} ({src})"
            )
        self._emit_scene(
            "round",
            round_msg,
            session,
            points=pts,
            ok_count=self._ok_count,
            run_index=run_n,
            points_live=self._points_live,
        )

        if self._done_target():
            if self._is_qiegao_mode():
                msg = f"切糕完成 · 共成功{self._ok_count}/{cfg.max_runs}"
            elif self._is_dungeon_mode():
                msg = f"副本完成 · 共成功{self._ok_count}/{cfg.max_runs}"
            else:
                msg = f"活跃目标已达成 points={pts}/{cfg.target_points}"
            self._finish_done(session, message=msg)
            return

        sid, pos, label = read_scene_state(session, fresh=True, log=self.log)

        # 切糕小号（队员）：先检测是否已在本内；
        # 已在 → 直接挂机；未在 → 不进本，一直等主号带入后再挂机。
        if self._is_qiegao_mode() and bool(getattr(cfg, "qiegao_is_alt", False)):
            in_dungeon = is_dungeon_scene(sid, label, gate=cfg.city_gate)
            self._emit(
                "gate",
                (
                    f"小号检测：已在副本 {label} → 跳过等待，直接挂机"
                    if in_dungeon
                    else f"小号检测：未在副本（当前 {label or sid}）→ 等待主号带入（不进本）"
                ),
                ok=True,
                scene_id=sid,
                scene_label=label,
            )
            if in_dungeon:
                self._emit(
                    "afk_move",
                    f"小号已在副本 {label}，直接切糕挂机",
                    ok=True,
                    scene_id=sid,
                    scene_label=label,
                )
            else:
                poll = max(3.0, float(getattr(cfg, "return_poll_s", 15.0) or 15.0))
                self._emit(
                    "enter_wait",
                    f"小号/队员：等待主号带入副本（不进本）· 当前 {label or sid}",
                    ok=True,
                    scene_id=sid,
                    scene_label=label,
                )
                entered = False
                while not self._stop.is_set():
                    if self._abort_if_game_dead("小号等待进本"):
                        return
                    sid2, _pos2, lab2 = read_scene_state(
                        session, fresh=True, log=self.log
                    )
                    if is_dungeon_scene(sid2, lab2, gate=cfg.city_gate):
                        sid, label = sid2, lab2
                        entered = True
                        self._emit(
                            "enter_ok",
                            f"小号已进入副本 {lab2}，开始切糕挂机",
                            ok=True,
                            scene_id=sid2,
                            scene_label=lab2,
                        )
                        break
                    self._emit(
                        "enter_wait",
                        f"小号/队员：等待主号带入… 当前 {lab2 or sid2}",
                        ok=True,
                        scene_id=sid2,
                        scene_label=lab2,
                    )
                    if not _sleep_interruptible(poll, self._stop):
                        self._emit("stop", "小号等待进本中断", ok=False)
                        return
                if not entered:
                    return
                self._emit(
                    "afk_wait",
                    "小号进本：等待过图完成（坐标稳定后再寻路）",
                    ok=True,
                )
                ready = wait_map_ready(
                    session,
                    cfg,
                    stop_event=self._stop,
                    log=self.log,
                    status=self._status,
                    require_dungeon=True,
                )
                if self._stop.is_set():
                    self._emit("stop", "小号过图等待中断", ok=False)
                    return
                sid, _pos, label = read_scene_state(
                    session, fresh=True, log=self.log
                )
                if not is_dungeon_scene(sid, label, gate=cfg.city_gate):
                    self._fail_count += 1
                    self._emit(
                        "enter_fail",
                        f"小号过图后仍未识别在副本内 scene={label or sid}",
                        ok=False,
                        scene_id=sid,
                        scene_label=label,
                    )
                    return
                self._emit(
                    "afk_wait",
                    (
                        f"小号过图完成，开始切糕挂机 · {label}"
                        if ready
                        else f"小号过图等待超时，但仍在副本 {label}，继续切糕挂机"
                    ),
                    ok=True,
                    scene_id=sid,
                    scene_label=label,
                )
                # 小号：先站桩不动，等主号完成桥触发阶段（空气墙消失），
                # 再挂机寻路，避免两号同时抢过空气墙。
                hold_s = self._qiegao_alt_entry_hold_s()
                if hold_s > 0:
                    self._ensure_hang_off(
                        session,
                        reason="小号站桩等待关挂机",
                        force=False,
                        keep_task_guard=True,
                    )
                    if self._stop.is_set():
                        self._emit("stop", "小号站桩等待中断", ok=False)
                        return
                    self._emit(
                        "afk_wait",
                        f"小号站桩等待主号过桥触发阶段 {hold_s:.0f}s（先不动）",
                        ok=True,
                        remain_s=hold_s,
                    )
                    if not _sleep_interruptible(hold_s, self._stop):
                        self._emit("stop", "小号站桩等待中断", ok=False)
                        return
                    self._emit("afk_wait", "小号站桩结束，开始挂机寻路", ok=True)
            if not self._qiegao_start_hang_phase(session):
                self._emit("stop", "切糕挂机/回城中断", ok=False)
                return
            self._ok_count += 1
            cd = cfg.entry_cd_sleep_s()
            if not self._sleep_cd(cd):
                return
            return

        if is_dungeon_scene(sid, label, gate=cfg.city_gate):
            if self._is_qiegao_mode():
                # 主号已在本内：不过图等待，直接切糕挂机（寻路/开挂机）。
                self._emit(
                    "afk_move",
                    f"当前已在副本 {label}，跳过过图等待，直接切糕挂机",
                    ok=True,
                    scene_id=sid,
                    scene_label=label,
                )
                if not self._qiegao_start_hang_phase(session):
                    self._emit("stop", "切糕挂机/回城中断", ok=False)
                    return
                self._ok_count += 1
                # 回城后若还要继续下一轮，走冷却；若仍在本内则下一轮再检测
                cd = cfg.entry_cd_sleep_s()
                if not self._sleep_cd(cd):
                    return
                return
            self._emit(
                "wait_return",
                f"当前在副本 {label}，等待回城",
                ok=True,
                scene_id=sid,
                scene_label=label,
            )
            if self._is_dungeon_mode():
                self._dungeon_unstick_prepare_run()
            if not wait_return_city(
                session,
                cfg,
                stop_event=self._stop,
                log=self.log,
                status=self._status,
                on_tick=self._dungeon_unstick_tick
                if self._is_dungeon_mode()
                else None,
            ):
                if self._abort_if_game_dead("等待回城"):
                    return
                if self._is_dungeon_mode():
                    self._handle_return_wait_failed(session, cfg)
                    return
                self._emit("stop", "等待回城中断", ok=False)
                return
            sid, pos, label = read_scene_state(session, fresh=True, log=self.log)

        if not is_city_scene(sid, label, cfg.city_gate):
            self._emit(
                "gate",
                f"不在城市场景 scene={sid} {label}（gate={cfg.city_gate}），等待…",
                ok=False,
                scene_id=sid,
                scene_label=label,
            )
            _sleep_interruptible(3.0, self._stop)
            return

        self._emit(
            "gate",
            f"城市场景 OK {label} scene={sid}",
            ok=True,
            scene_id=sid,
            scene_label=label,
        )

        self._emit(
            "path_open",
            f"直接进本 inst={cfg.instance_id} "
            f"diff={cfg.instance_difficulty} (副本列表函数)",
            ok=True,
        )
        er = enter_instance_list(
            session,
            inst_id=int(cfg.instance_id),
            difficulty=int(cfg.instance_difficulty),
            flag=int(cfg.instance_flag),
            hwnd=self.hwnd,
            use_bridge=bool(cfg.use_bridge),
            log=self.log,
        )
        if not er.get("ok"):
            self._fail_count += 1
            self._emit(
                "path_open",
                f"列表进本失败: {er.get('error')}",
                ok=False,
            )
            _sleep_interruptible(3.0, self._stop)
            return

        wait_msg = (
            "等待识别进入副本地图（过图中…）"
            if self._is_qiegao_mode()
            else "等待进入副本（内挂清本）"
        )
        self._emit("enter_wait", wait_msg)
        if not wait_enter_dungeon(
            session,
            cfg,
            stop_event=self._stop,
            log=self.log,
            status=self._status,
        ):
            if self._abort_if_game_dead("等待进本"):
                return
            self._fail_count += 1
            self._emit("enter_fail", "进本超时", ok=False)
            _sleep_interruptible(3.0, self._stop)
            return

        self._emit("enter_ok", "已检测到离开城镇/进入副本", ok=True)

        if self._is_qiegao_mode():
            # 城内开：先确认已在副本地图且坐标就绪（过图），再挂机流程。
            # 已在本内启动不会走到这里。
            self._emit(
                "afk_wait",
                "城内进本：等待过图完成（识别到本内且坐标稳定）",
                ok=True,
            )
            ready = wait_map_ready(
                session,
                cfg,
                stop_event=self._stop,
                log=self.log,
                status=self._status,
                require_dungeon=True,
            )
            if self._stop.is_set():
                self._emit("stop", "过图等待中断", ok=False)
                return
            sid_h, _ph, lab_h = read_scene_state(session, fresh=True, log=self.log)
            if not is_dungeon_scene(sid_h, lab_h, gate=cfg.city_gate):
                self._fail_count += 1
                self._emit(
                    "enter_fail",
                    f"过图后仍未识别在副本内 scene={lab_h or sid_h}",
                    ok=False,
                    scene_id=sid_h,
                    scene_label=lab_h,
                )
                _sleep_interruptible(3.0, self._stop)
                return
            if not ready:
                self._emit(
                    "afk_wait",
                    f"过图等待超时，但已在副本 {lab_h}，继续切糕挂机",
                    ok=True,
                    scene_id=sid_h,
                    scene_label=lab_h,
                )
            else:
                self._emit(
                    "afk_wait",
                    f"过图完成，已在副本 {lab_h}，开始切糕挂机",
                    ok=True,
                    scene_id=sid_h,
                    scene_label=lab_h,
                )
            # 主号：切图稳定后先走桥触发阶段（触发空气墙消失），再正常挂机寻路。
            if not self._qiegao_enter_bridge_phase(session):
                self._emit("stop", "切糕桥触发阶段中断", ok=False)
                return
            if not self._qiegao_start_hang_phase(session):
                self._emit("stop", "切糕挂机/回城中断", ok=False)
                return
        else:
            if self._is_dungeon_mode():
                self._dungeon_unstick_prepare_run()
            if not wait_return_city(
                session,
                cfg,
                stop_event=self._stop,
                log=self.log,
                status=self._status,
                on_tick=self._dungeon_unstick_tick
                if self._is_dungeon_mode()
                else None,
            ):
                if self._abort_if_game_dead("等待回城"):
                    return
                if self._is_dungeon_mode():
                    self._handle_return_wait_failed(session, cfg)
                    return
                self._emit("stop", "等待回城中断", ok=False)
                return

        self._ok_count += 1
        pts = self._refresh_points(session)
        if not self._points_live:
            # Live read failed — estimate from last known + per-run.
            self._points = min(
                int(cfg.target_points),
                int(self._points) + int(cfg.points_per_run),
            )
            pts = int(self._points)
        src = "live" if self._points_live else "est"
        sid2, _pos2, label2 = read_scene_state(session, log=self.log)
        if self._is_qiegao_mode():
            ret_msg = f"第 {self._ok_count} 次完成（已回城·切糕）"
        elif self._is_dungeon_mode():
            ret_msg = f"第 {self._ok_count} 次完成（已回城·副本）"
        else:
            ret_msg = (
                f"第 {self._ok_count} 次完成 "
                f"points={pts}/{cfg.target_points} ({src})"
            )
        self._emit(
            "enter_ok",
            ret_msg,
            ok=True,
            points=pts,
            ok_count=self._ok_count,
            points_live=self._points_live,
            scene_id=sid2,
            scene_label=label2,
        )

        # Claim after each return so tier chests open as soon as unlocked;
        # also claim once more path when target just reached (same call).
        # Settle first: immediate claim after city load has crashed d3dx9 (0xc0000005).
        if (
            self._is_points_mode()
            and cfg.claim_awards
            and (cfg.claim_after_each_run or self._done_target())
        ):
            settle = max(0.0, float(getattr(cfg, "claim_settle_s", 6.0) or 0.0))
            if settle > 0:
                self._emit(
                    "claim",
                    f"回城稳定等待 {settle:.0f}s 后领取…",
                    ok=True,
                )
                if not _sleep_interruptible(settle, self._stop):
                    return
                if self._abort_if_game_dead("领取前"):
                    return
            self._claim_awards(
                session,
                reason=(
                    "目标达成" if self._done_target() else f"第{self._ok_count}轮回城"
                ),
            )

        if self._done_target():
            # Awards already claimed above (activity); stop without a second pass.
            if self._is_qiegao_mode():
                done_msg = (
                    f"切糕完成 · 共成功{self._ok_count}/{cfg.max_runs}"
                )
            elif self._is_dungeon_mode():
                done_msg = (
                    f"副本完成 · 共成功{self._ok_count}/{cfg.max_runs}"
                )
            else:
                done_msg = (
                    f"活跃目标达成 points={self._points}/{cfg.target_points} "
                    f"runs={self._ok_count}"
                )
            self._emit("done", done_msg, ok=True)
            self._stop.set()
            return

        cd = cfg.entry_cd_sleep_s()
        if not self._sleep_cd(cd):
            return

    def _loop(self) -> None:
        try:
            self._session = open_attach_session(self.pid, log=self.log)
            # 非马上需要：活动/副本页开跑预热（open_attach 已 SCENE/POS；这里补 bag/money）
            try:
                from app.core.state_dispatch import StateKind, warmup_session

                warmup_session(
                    self._session,
                    (
                        StateKind.SCENE,
                        StateKind.POS,
                        StateKind.BAG,
                        StateKind.MONEY,
                    ),
                    log=lambda m: self.log(f"activity warmup: {m}") if m else None,
                )
            except Exception as e:
                self.log(f"activity warmup skip: {e}")
            self._emit("attach", f"attach pid={self.pid} hwnd=0x{self.hwnd:X}")
            pts0 = self._refresh_points(self._session)
            sid0, _p0, lab0 = read_scene_state(
                self._session, fresh=True, log=self.log
            )
            if self._is_qiegao_mode():
                attach_msg = (
                    f"切糕模式 attach pid={self.pid} "
                    f"inst={self.cfg.instance_id} scene={lab0 or sid0}"
                )
            elif self._is_dungeon_mode():
                attach_msg = (
                    f"副本模式 attach pid={self.pid} "
                    f"scene={lab0 or sid0}"
                )
            else:
                attach_msg = (
                    f"活跃度 points={pts0} "
                    f"({'live' if self._points_live else 'fallback'})"
                )
            self._emit(
                "attach",
                attach_msg,
                ok=True if (self._is_dungeon_mode() or self._is_qiegao_mode()) else self._points_live,
                points=pts0,
                scene_id=sid0,
                scene_label=lab0,
            )
            if bool(self.cfg.use_bridge):
                try:
                    br = ensure_bridge(
                        self.pid,
                        log=self.log,
                        inject_if_needed=True,
                        hwnd=self.hwnd or None,
                        force_reinject=False,
                    )
                    if br is None:
                        self._emit(
                            "gate",
                            "桥接未就绪：请先 Delete 注入",
                            ok=False,
                        )
                    else:
                        try:
                            br.close()
                        except Exception:
                            pass
                except Exception as e:
                    self._emit("gate", f"bridge 检查失败: {e}", ok=False)

            while not self._stop.is_set():
                if not _pid_alive(self.pid):
                    self._emit(
                        "game_dead",
                        f"游戏进程已退出 pid={self.pid}",
                        ok=False,
                    )
                    break
                try:
                    from app.core.safe_dispatch import session_blocked

                    blocked, brsn = session_blocked(self.pid)
                except Exception:
                    blocked, brsn = False, ""
                if blocked:
                    self._emit(
                        "remote_blocked",
                        f"远程不可用已停止 pid={self.pid} ({brsn})",
                        ok=False,
                        reason=brsn,
                    )
                    break
                if self._pause_requested():
                    self._emit("paused", "已暂停（当前本结束后）", ok=True)
                    break
                max_att = int(self.cfg.max_attempts or 0)
                if (
                    max_att > 0
                    and int(self._rounds) >= max_att
                    and not self._done_target()
                ):
                    self._emit(
                        "done",
                        f"达到尝试上限 {max_att} 次，停止",
                        ok=False,
                        attempts=int(self._rounds),
                    )
                    self._stop.set()
                    break
                if self._done_target():
                    if self._is_qiegao_mode():
                        fin_msg = (
                            f"切糕完成 · 共成功{self._ok_count}/"
                            f"{self.cfg.max_runs}"
                        )
                    elif self._is_dungeon_mode():
                        fin_msg = (
                            f"副本完成 · 共成功{self._ok_count}/"
                            f"{self.cfg.max_runs}"
                        )
                    else:
                        fin_msg = (
                            f"目标已达成 points={self._points}/"
                            f"{self.cfg.target_points}"
                        )
                    self._finish_done(self._session, message=fin_msg)
                    break
                try:
                    self._one_round(self._session)
                except Exception as e:
                    if (not _pid_alive(self.pid)) or _is_process_gone_error(e):
                        self._emit(
                            "game_dead",
                            f"游戏进程已退出 pid={self.pid} ({e})",
                            ok=False,
                        )
                        break
                    self._emit("error", str(e), ok=False)
                    if not _sleep_interruptible(2.0, self._stop):
                        break
                if self._pause_requested():
                    self._emit("paused", "已暂停（当前本结束后）", ok=True)
                    break
                if not _sleep_interruptible(float(self.cfg.loop_idle_s), self._stop):
                    break
        finally:
            self.running = False
            if self._is_qiegao_mode():
                with _QIEGAO_ACTIVE_RUNNERS_LOCK:
                    _QIEGAO_ACTIVE_RUNNERS.discard(int(self.pid))
            if self._is_qiegao_mode():
                try:
                    from app.core.hang_settings import stop_hang_guard

                    stop_hang_guard(self.pid, log=self.log)
                except Exception as e:
                    self.log(f"activity final guard stop: {e}")
            # 副本纠偏只绑定 dungeon runner 生命周期：停止时解除监视与临时跟随。
            if self._is_dungeon_mode() and self._dungeon_unstick_guard is not None:
                try:
                    self._dungeon_unstick_guard.disarm(self._session)
                except Exception as e:
                    self.log(f"dungeon unstick disarm: {e}")
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None
            self._emit(
                "stopped",
                f"已停止 · 完成{self._ok_count}次 · 失败{self._fail_count}次"
                f" · 活跃{self._points}"
                f"{'' if self._points_live else '(估)'}",
                ok=True,
                ok_count=int(self._ok_count),
                fail_count=int(self._fail_count),
                points=int(self._points),
            )
