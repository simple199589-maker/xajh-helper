# -*- coding: utf-8 -*-
"""Hang settings API (per-character-id hang prefs in hang_prefs.json + start/stop/maintain).

Integrated internal business (only via start_hang / hang_maintain_once):
  - mode + radius prepare, party auto-need (pickup), empty-skill force
  - optional wanzi/attack-pill direct packet runner (without recover slots)
  - equip repair box + Game_RepairWithItem confirm (long check interval)
  - vitality potion when below threshold (long check interval)
  - loot Pass when pickup disabled; Need when pickup on + dead (alive: in-game auto-need)
  - start_hang force_rv once; later maintain skips repair/vitality until due

@author by ak
"""
from __future__ import annotations

import json
import inspect
import struct
import time
from dataclasses import asdict, dataclass, fields, replace
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from app.core.activity_auto import (
    AUTOPLAY_MODE_DUNGEON,
    AUTOPLAY_MODE_NORMAL,
    AUTOPLAY_RADIUS_MEM_MAX,
    AUTOPLAY_RADIUS_MEM_MIN,
    AUTOPLAY_RECOVER_IV_MAX_MS,
    AUTOPLAY_SKILL_SLOT0_OFF,
    AUTOPLAY_SKILL_SLOT_STRIDE,
    HOST_DATA_LEAF_OFF,
    HOST_DATA_MID_OFF,
    NOTE_VA_GAME_ROOT_GLOBAL,
    _note_live_va,
    _rpm_u32,
    _rpm_u8,
    _wpm_u8,
    autoplay_mode_name,
    clear_autoplay_skills,
    force_use_lab_bag_item,
    probe_hang_state_mem,
    read_autoplay_skills,
    resolve_cec_autoplay_rpm,
    set_autoplay_mode,
    set_autoplay_radius,
)
from app.core.game_attach import GameAttachSession
from app.core.build_profile import is_dev_build
from app.core.package_api import (
    ITEM_TMPL_OFF,
    find_items_by_name,
    list_package_items,
    list_package_items_rpm,
    list_packages_items,
    read_package_slot,
    use_item_in_package,
)
from app.core.remote_runtime import (
    ensure_pid_remote_callable,
    pid_call_mutex,
    remote_call_cdecl_x86,
    remote_read_bytes,
    remote_write_bytes,
)
from app.core.raw_c2s import send_raw_c2s_packet
import threading
from contextlib import contextmanager

from app.core.youfeng_chain import (
    INTERRUPT_QINGGONG_PULSE,
    YOUFENG_SKILL_ID,
    YOUFENG_ULTIMATE_CONFIG_ID,
    YoufengChainRunner,
)
from app.core.wanzi_packet import (
    WANZI_AOI_DEFAULT_RADIUS,
    WANZI_AOI_MAX_AGE_S,
    WANZI_AOI_SCAN_INTERVAL_S,
    WANZI_ACTIVE_WINDOW_S,
    WANZI_CONTROL_MAX_AGE_S,
    WANZI_CONTROL_RECOVERY_S,
    WANZI_CONTROL_SCAN_INTERVAL_S,
    WANZI_LOW_RATE_PAIRS_PER_SECOND,
    WANZI_LOW_RATE_SECONDS,
    WANZI_KIND_NEIGONG,
    WANZI_KIND_WAIGONG,
    WanziPacketRunner,
    build_wanzi_p1,
    clear_wanzi_aoi_state,
    clear_wanzi_control_state,
    get_wanzi_aoi_state,
    get_wanzi_control_state,
    probe_nearby_class2_rpm,
    probe_wanzi_control_rpm,
    set_wanzi_aoi_state,
    set_wanzi_control_state,
    update_wanzi_aoi_sample,
    update_wanzi_control_sample,
    wanzi_aoi_can_send,
    wanzi_control_can_send,
)

LogFn = Callable[[str], None]

DEFAULT_HANG_MODE = int(AUTOPLAY_MODE_DUNGEON)
DEFAULT_HANG_RADIUS = 5
DEFAULT_ENABLE_PICKUP = True
DEFAULT_EMPTY_SKILL = False
DEFAULT_WANZI_HANG = False
DEFAULT_WANZI_NEIGONG_HANG = False
DEFAULT_WANZI_WAIGONG_HANG = False
DEFAULT_WANZI_INTERVAL_MS = 140
WANZI_INTERVAL_MIN_PROD_MS = 140
DEFAULT_YOUFENG_HANG = False
DEFAULT_JIANGLONG_HANG = False
DEFAULT_AUTO_OPEN_MONSTER = False
DEFAULT_OPEN_MONSTER_ROWS = 0
DEFAULT_SKIP_DUNGEON_STORY = False
YOUFENG_SKILL_SLOT = 8
DEFAULT_AUTO_REPAIR = True
DEFAULT_REPAIR_BELOW_PCT = 50.0
DEFAULT_AUTO_VITALITY = True
DEFAULT_VITALITY_BELOW_PCT = 20.0
DEFAULT_IGNORE_DUNGEON_STUCK = False

REPAIR_BOX_TID = 83249
REPAIR_BOX_NAME = "工匠修理箱"
# Right-click repair box opens confirm dialog (live 2026-07-24).
REPAIR_CONFIRM_DLG = "Game_RepairWithItem"
REPAIR_CONFIRM_BTN_NAMES = (
    "Btn_Ok",
    "Btn_OK",
    "Btn_Confirm",
    "Btn_Yes",
    "Btn_Sure",
)
# Dialog geometry fallback ratios for 确定 (left of 取消).
REPAIR_CONFIRM_RATIO_X = 0.38
REPAIR_CONFIRM_RATIO_Y = 0.82
REPAIR_CONFIRM_WAIT_S = 1.2
VITALITY_ITEM_KEYWORDS = ("活力药", "活力")

# The recovered system commands carry only item template IDs, not an inventory
# pointer or slot. Before starting, search the first in-game bag (package 2)
# for the selected pill. Its particular slot is intentionally irrelevant.
WANZI_ITEM_TID_NEIGONG = 0x4200
WANZI_ITEM_TID_WAIGONG = 0x4201
WANZI_FIRST_BAG_PACKAGE = 2
# The verified P1 field for the first extension bag is always package 3.
# We still use package 2 only for the RPM existence check before startup.
WANZI_P1_PACKAGE = 3
WANZI_FIRST_BAG_MISSING_MESSAGE = "丸子预检失败：未在第一个扩展背包(帝王背包)找到丸子道具"

# --- RE 2026-07-24: CECAutoPlay pick flags / LootRoll / dura / vitality ---
# UI pack (AFD73F) + apply (C55810) + runtime test (C57A99):
#   TeamAutoPick = byte[CECAutoPlay + 0x1D] bit 0x10
AUTOPLAY_PICK_FLAGS_OFF = 0x1D
# +0x1D flag byte checkboxes (top→bottom in game UI):
#   0x01 = ?, 0x02 = ?, 0x04 = ?, 0x08 = ?,
#   0x10 = 宠物拾取能力 (pet pickup)
#   0x20 = 组队情况下对分配物品自动需求 (team auto-need)
AUTOPLAY_TEAM_AUTO_NEED_BIT = 0x20

# LootRoll packet: cdecl 0xCC8A50(id0, id1, id2, choice)
# choice 0=Need, 1=Greed, 2=Pass/放弃 (A3DBB9 / A3DC89 / A3DDC9)
NOTE_VA_LOOT_ROLL_PKT = 0x00CC8A50
LOOT_ROLL_CHOICE_NEED = 0
LOOT_ROLL_CHOICE_GREED = 1
LOOT_ROLL_CHOICE_PASS = 2
HOST_DATA_LOOT_SIDE_OFF = 0x08
LOOT_MGR_PTR_OFF = 0x8C
LOOT_MGR_ARR_OFF = 0x08
LOOT_MGR_COUNT_OFF = 0x0C
LOOT_ENTRY_ID0_OFF = 0x08
LOOT_ENTRY_ID1_OFF = 0x0C
LOOT_ENTRY_ID2_OFF = 0x10
LOOT_ENTRY_TIME_OFF = 0x14
LOOT_ENTRY_TIME_MAX_OFF = 0x18
LOOT_ENTRY_DECIDED_OFF = 0x1C
LOOT_MGR_COUNT_CAP = 160  # live manager retains expired rows; observed 120
# Win_LootRoll derives from AUIDialog; this byte is its live visibility state.
# Only clear it after a fresh manager scan proves that no Roll remains active.
LOOT_ROLL_DLG_NAME = "Win_LootRoll"
LOOT_ROLL_DLG_ISSHOW_OFF = 0x94

# Equip durability (54F100/54F130/54F170):
#   cur = item+0xEE (scaled hundredths)
#   max_pts = *(item+0xE8)+0x19C
#   max_scaled = max_pts * 100
#   pct = cur * 100 / max_scaled
ITEM_DURA_FLAG_OFF = 0xB8
ITEM_DURA_CUR_OFF = 0xEE
ITEM_TMPL_MAX_DURA_OFF = 0x19C
EQUIP_PACKAGE_INDEX = 0
# Ignore fashion-like max_pts=1 noise when averaging "整体"
DURA_MIN_MAX_PTS = 50

# Vitality pool (RE 2026-07-24):
# DEAD: host_data+0x30 -> +0x10 tick counter (+~250/0.25s), +0x14=3000 fake max.
# LIVE: host_data+0x3C resource obj; type==6 uses cur=+0x90 max=+0x94 (53DF20 / 74E6F4).
# Max commonly 3000; live full 3000/3000 stable (not ticking).
HOST_DATA_VITALITY_OBJ_OFF = 0x3C
VITALITY_CUR_OFF = 0x90
VITALITY_MAX_OFF = 0x94
# Keep dead probe constants for regression tools.
HOST_DATA_VITALITY_DEAD_OBJ_OFF = 0x30
VITALITY_DEAD_CUR_OFF = 0x10
VITALITY_DEAD_MAX_OFF = 0x14
VITALITY_RE_READY = True
VITALITY_RE_NOTE = "host_data+0x3C -> +0x90/+0x94 (type6 energy/vitality pool)"
VITALITY_KNOWN_MAX = frozenset({1000, 1500, 2000, 2500, 3000, 3600, 5000})

# --- safety: busy / cooldown ---
# Policy (user 2026-07-24):
# - Only block when this pid already has hang exclusive work, or remote hung.
# - Do NOT skip merely because another module holds shared Call mutex.
# - Non-empty-skill start/stop (Alt+R) does not use busy gate.
# - Never hold Call mutex across use_item/bridge (Win Mutex non-recursive).
HANG_ACTION_MUTEX_NS = "Call"
HANG_ACTION_MUTEX_TIMEOUT_MS = 1  # probe helper only; not a hang skip reason
HANG_ACTION_BUSY_TIMEOUT_MS = 0  # local exclusive: try once, never wait
# Repair/vitality check cadence (no bag/use when not due).
# Startup always queues one immediate check; steady-state checks are deliberately low frequency.
HANG_REPAIR_CHECK_INTERVAL_S = 1800.0  # 30min durability check
HANG_VITALITY_CHECK_INTERVAL_S = 1800.0  # 30min vitality check
HANG_REPAIR_COOLDOWN_S = 60.0  # post-use soft cooldown
HANG_VITALITY_COOLDOWN_S = 60.0  # post-use soft cooldown
# Loot: send packet, then mirror the client's local decision mark so the row
# leaves both our RPM view and the game's normal UI refresh path immediately.
# One wave should cover a typical 10~20 drop burst.
# 2026-07-26: 苦寒 AV 前约 3 分钟高频放弃(CRT 0xCC8A50)。
# 0.35s 波间隔在掉落潮下过密，放宽波/包间隔，降低远程调用压。
HANG_LOOT_COOLDOWN_S = 0.90
HANG_LOOT_INTER_PKT_S = 0.08
HANG_LOOT_MAX_PER_BURST = 24
# After a full clear wave (remain=0), hold longer before next poll-send.
HANG_LOOT_POST_CLEAR_COOLDOWN_S = 1.25
# Cooldown log throttle only.
HANG_LOOT_LOG_THROTTLE_S = 6.0
# 放弃/需求：不等待服务器确认；发包成功后补客户端本地 decided 标记。
HANG_MAINTAIN_MIN_INTERVAL_S = 0.35
# Death state machine (for pickup-on dead Need rolls).
# UI-thread host snapshot only when pending rolls/death watch need it.
HANG_DEAD_POLL_INTERVAL_S = 3.0
HANG_DEAD_UNKNOWN_TTL_S = 8.0
# Hang long-running guard cadence (business-owned; SafeDispatch only runs the timer)
HANG_GUARD_TICK_S = 1.0  # roll empty poll tick
# Dungeon cutscene/talk skip — EVENT-DRIVEN only (no hang_guard CRT scan).
# Triggers: hang start once + scene-change settle once.
# Esc can close CG when CanEscClose; Space advances talk.
HANG_PLOT_SKIP_COOLDOWN_S = 0.80
HANG_PLOT_SKIP_BURST = 3
HANG_PLOT_SKIP_DELAY_S = 1.2  # after hang start / scene settle
HANG_SCENE_WAIT_DEFAULT_S = 4.0
HANG_SCENE_WAIT_WANZI_S = 6.0
# After map change, re-arm wanzi once scene is stable again.
HANG_WANZI_REARM_AFTER_SCENE_S = 1.5
# Kept for optional manual/lab probe only; hang_guard no longer polls these.
PLOT_SKIP_DLG_NAMES = (
    "Win_SystemTaskTalk",
    "Win_NPCTalk",
    "Win_NPCTalk2D",
    "Win_TaskTalk",
    "Win_NPCTask",
    "Win_NPCTaskContent",
)
HANG_ROLL_POLL_CACHE_S = 0.50  # merge concurrent roll RPM within tick
# Soft backoff when repair/vitality check hit busy (avoid retry storm).
HANG_RV_BUSY_BACKOFF_S = 30.0
# Live roll id1 flag (mgr/UI): 0x02000000
LOOT_ROLL_ID1_FLAG = 0x02000000
# Actions that take hang exclusive lock (remote/item/loot).
HANG_HEAVY_ACTIONS = frozenset(
    {
        "start_hang",
        "stop_hang",
    "resolve_empty_skill_for_action",
        "maintain",
        "repair",
        "vitality",
        "loot_abandon",
        "loot_need",
        "party_auto_need",
        "prepare",
    }
)

SESSION_KEY_MODE = "hang_mode"
SESSION_KEY_PICKUP = "hang_enable_pickup"
SESSION_KEY_EMPTY = "hang_empty_skill"
SESSION_KEY_WANZI = "hang_wanzi_hang"
SESSION_KEY_WANZI_NEIGONG = "hang_wanzi_neigong_hang"
SESSION_KEY_WANZI_WAIGONG = "hang_wanzi_waigong_hang"
SESSION_KEY_WANZI_IV = "hang_wanzi_interval_ms"
# Source/dev-only cadence knobs live in the session overlay, never in the
# character preference file.  A packaged build therefore always uses the
# conservative defaults below.
SESSION_KEY_WANZI_LOW_RATE_SECONDS = "hang_wanzi_low_rate_seconds"
SESSION_KEY_WANZI_LOW_RATE_PAIRS_PER_SECOND = "hang_wanzi_low_rate_pairs_per_second"
SESSION_KEY_WANZI_ACTIVE_WINDOW_S = "hang_wanzi_active_window_s"
SESSION_KEY_YOUFENG = "hang_youfeng_hang"
SESSION_KEY_JIANGLONG = "hang_jianglong_hang"
SESSION_KEY_AUTO_OPEN_MONSTER = "hang_auto_open_monster"
SESSION_KEY_OPEN_MONSTER_ROWS = "hang_open_monster_rows"
SESSION_KEY_SKIP_STORY = "hang_skip_dungeon_story"
SESSION_KEY_RADIUS = "hang_radius"
SESSION_KEY_AUTO_REPAIR = "hang_auto_repair"
SESSION_KEY_REPAIR_PCT = "hang_repair_below_pct"
SESSION_KEY_AUTO_VITALITY = "hang_auto_vitality"
SESSION_KEY_VITALITY_PCT = "hang_vitality_below_pct"
SESSION_KEY_IGNORE_DUNGEON_STUCK = "hang_ignore_dungeon_stuck"

# Disk keys (all UI hang fields live under hang_prefs.json).
PREF_KEY_MODE = "mode"
PREF_KEY_RADIUS = "radius"
PREF_KEY_PICKUP = "enable_pickup"
PREF_KEY_EMPTY = "empty_skill"
PREF_KEY_WANZI = "wanzi_hang"
PREF_KEY_WANZI_NEIGONG = "wanzi_neigong_hang"
PREF_KEY_WANZI_WAIGONG = "wanzi_waigong_hang"
PREF_KEY_WANZI_IV = "wanzi_interval_ms"
PREF_KEY_YOUFENG = "youfeng_hang"
PREF_KEY_JIANGLONG = "jianglong_hang"
PREF_KEY_AUTO_OPEN_MONSTER = "auto_open_monster"
PREF_KEY_OPEN_MONSTER_ROWS = "open_monster_rows"
PREF_KEY_SKIP_STORY = "skip_dungeon_story"
PREF_KEY_AUTO_REPAIR = "auto_repair"
PREF_KEY_REPAIR_PCT = "repair_below_pct"
PREF_KEY_AUTO_VITALITY = "auto_vitality"
PREF_KEY_VITALITY_PCT = "vitality_below_pct"
PREF_KEY_IGNORE_DUNGEON_STUCK = "ignore_dungeon_stuck"
# Display-only label for JSON human lookup; never used as map key.
PREF_KEY_NAME = "name"
HANG_PREFS_VERSION = 7
HANG_PREFS_DEFAULT_ID = ""  # empty id bucket = shared default
HANG_PREFS_ALL_KEYS = (
    PREF_KEY_MODE,
    PREF_KEY_RADIUS,
    PREF_KEY_PICKUP,
    PREF_KEY_EMPTY,
    PREF_KEY_WANZI,
    PREF_KEY_WANZI_NEIGONG,
    PREF_KEY_WANZI_WAIGONG,
    PREF_KEY_WANZI_IV,
    PREF_KEY_YOUFENG,
    PREF_KEY_JIANGLONG,
    PREF_KEY_AUTO_OPEN_MONSTER,
    PREF_KEY_OPEN_MONSTER_ROWS,
    PREF_KEY_SKIP_STORY,
    PREF_KEY_AUTO_REPAIR,
    PREF_KEY_REPAIR_PCT,
    PREF_KEY_AUTO_VITALITY,
    PREF_KEY_VITALITY_PCT,
    PREF_KEY_IGNORE_DUNGEON_STUCK,
)

@dataclass
class HangConfig:
    """Hang settings used by start/stop/maintain. @author by ak"""

    mode: int = DEFAULT_HANG_MODE
    radius: int = DEFAULT_HANG_RADIUS
    enable_pickup: bool = DEFAULT_ENABLE_PICKUP
    empty_skill: bool = DEFAULT_EMPTY_SKILL
    wanzi_hang: bool = DEFAULT_WANZI_HANG
    wanzi_neigong_hang: bool = DEFAULT_WANZI_NEIGONG_HANG
    wanzi_waigong_hang: bool = DEFAULT_WANZI_WAIGONG_HANG
    wanzi_interval_ms: int = DEFAULT_WANZI_INTERVAL_MS
    wanzi_low_rate_seconds: float = WANZI_LOW_RATE_SECONDS
    wanzi_low_rate_pairs_per_second: float = WANZI_LOW_RATE_PAIRS_PER_SECOND
    wanzi_active_window_s: float = WANZI_ACTIVE_WINDOW_S
    youfeng_hang: bool = DEFAULT_YOUFENG_HANG
    jianglong_hang: bool = DEFAULT_JIANGLONG_HANG
    auto_open_monster: bool = DEFAULT_AUTO_OPEN_MONSTER
    open_monster_rows: int = DEFAULT_OPEN_MONSTER_ROWS
    skip_dungeon_story: bool = DEFAULT_SKIP_DUNGEON_STORY
    auto_repair: bool = DEFAULT_AUTO_REPAIR
    repair_below_pct: float = DEFAULT_REPAIR_BELOW_PCT
    auto_vitality: bool = DEFAULT_AUTO_VITALITY
    vitality_below_pct: float = DEFAULT_VITALITY_BELOW_PCT
    ignore_dungeon_stuck: bool = DEFAULT_IGNORE_DUNGEON_STUCK

    def to_dict(self) -> dict:
        return asdict(self)


def wanzi_kind_from_config(cfg: HangConfig | None) -> str:
    """Resolve mutually-exclusive 丸子 type, migrating legacy generic=true to 外功."""
    if cfg is None or bool(getattr(cfg, "youfeng_hang", False)):
        return ""
    neigong = bool(getattr(cfg, "wanzi_neigong_hang", False))
    waigong = bool(getattr(cfg, "wanzi_waigong_hang", False))
    if neigong:
        return WANZI_KIND_NEIGONG
    if waigong:
        return WANZI_KIND_WAIGONG
    if bool(getattr(cfg, "wanzi_hang", False)):
        return WANZI_KIND_WAIGONG
    return ""


def _normalize_wanzi_selection(cfg: HangConfig) -> HangConfig:
    kind = wanzi_kind_from_config(cfg)
    cfg.wanzi_neigong_hang = kind == WANZI_KIND_NEIGONG
    cfg.wanzi_waigong_hang = kind == WANZI_KIND_WAIGONG
    cfg.wanzi_hang = bool(kind)
    return cfg


@dataclass
class HangLiveState:
    """Live hang snapshot from memory. @author by ak"""

    ok: bool = False
    running: bool | None = None
    mode: int | None = None
    mode_name: str = "?"
    radius: int | None = None
    autoplay: int = 0
    filled_slots: int | None = None
    gate_ok: bool | None = None
    empty_skill_inferred: bool | None = None
    durability_pct: float | None = None
    vitality_pct: float | None = None
    durability_ready: bool = False
    vitality_ready: bool = False
    pickup_ready: bool = False
    loot_roll_ready: bool = False
    error: str | None = None
    detail: dict | None = None

    def __post_init__(self) -> None:
        if self.detail is None:
            self.detail = {}

    def to_dict(self) -> dict:
        return asdict(self)


def _hang_prefs_path() -> Path:
    """Legacy disk path for hang character prefs (runtime/config/hang_prefs.json).

    New per-role config lives under runtime/config/roles/{role_id}/hang.json;
    this file is now read-only compat: used only as migration source / fallback.

    @author by ak
    """
    try:
        from common.paths import ensure_writable_dir

        base = ensure_writable_dir("runtime", "config")
    except Exception:
        try:
            base = Path(__file__).resolve().parents[2] / "runtime" / "config"
        except Exception:
            base = Path.cwd() / "runtime" / "config"
    return Path(base) / "hang_prefs.json"


def _hang_defaults_path() -> Path:
    """Shared default hang prefs (runtime/config/global/hang_defaults.json). @author by ak"""
    from app.core.account_manager import global_dir

    return global_dir() / "hang_defaults.json"


def _role_hang_path(role_id: str) -> Path:
    """Per-role hang prefs file (runtime/config/roles/{role_id}/hang.json). @author by ak"""
    from app.core.account_manager import role_dir

    return role_dir(role_id) / "hang.json"


def migrate_legacy_hang_prefs() -> int:
    """One-way migrate old hang_prefs.json into the per-role structure.

    Copies flat/store defaults into global/hang_defaults.json and every
    numeric by_id/by_role entry into roles/{role_id}/hang.json (+meta name).
    Idempotent: already-written role files / global defaults are skipped.
    The legacy file stays untouched (read-only compat afterwards).

    @author by ak
    """
    legacy = _hang_prefs_path()
    if not legacy.is_file():
        return 0
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    migrated = 0
    store_like = any(
        k in data for k in ("version", "default", "by_id", "by_role")
    )
    default_raw: dict | None = None
    if store_like:
        if isinstance(data.get("default"), dict):
            default_raw = data["default"]
    elif any(k in data for k in HANG_PREFS_ALL_KEYS):
        # legacy flat single-object format
        default_raw = data
    if default_raw is not None:
        gd = _hang_defaults_path()
        if not gd.exists():
            try:
                gd.parent.mkdir(parents=True, exist_ok=True)
                gd.write_text(
                    json.dumps(
                        {"version": int(HANG_PREFS_VERSION), "default": default_raw},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                migrated += 1
            except Exception:
                pass
    raw_by_id: dict = {}
    if isinstance(data.get("by_id"), dict):
        raw_by_id.update(data["by_id"])
    if isinstance(data.get("by_role"), dict):
        for k, v in data["by_role"].items():
            raw_by_id.setdefault(k, v)
    for kid, pref in raw_by_id.items():
        key = normalize_hang_char_id(kid)
        if not key or not isinstance(pref, dict):
            continue
        hpath = _role_hang_path(key)
        if hpath.exists():
            continue
        try:
            hpath.parent.mkdir(parents=True, exist_ok=True)
            hpath.write_text(
                json.dumps(pref, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            name = str(
                pref.get(PREF_KEY_NAME)
                or pref.get("char_name")
                or pref.get("role_name")
                or ""
            ).strip()
            if name:
                from app.core.account_manager import load_role_meta, save_role_meta

                meta = dict(load_role_meta(key))
                if not meta.get("name"):
                    save_role_meta(key, name=name)
            migrated += 1
        except Exception:
            continue
    return migrated


def default_hang_prefs() -> dict:
    """One role's full hang prefs (all UI fields). @author by ak"""
    return {
        PREF_KEY_MODE: int(DEFAULT_HANG_MODE),
        PREF_KEY_RADIUS: int(DEFAULT_HANG_RADIUS),
        PREF_KEY_PICKUP: bool(DEFAULT_ENABLE_PICKUP),
        PREF_KEY_EMPTY: bool(DEFAULT_EMPTY_SKILL),
        PREF_KEY_WANZI: bool(DEFAULT_WANZI_HANG),
        PREF_KEY_WANZI_NEIGONG: bool(DEFAULT_WANZI_NEIGONG_HANG),
        PREF_KEY_WANZI_WAIGONG: bool(DEFAULT_WANZI_WAIGONG_HANG),
        PREF_KEY_WANZI_IV: int(DEFAULT_WANZI_INTERVAL_MS),
        PREF_KEY_YOUFENG: bool(DEFAULT_YOUFENG_HANG),
        PREF_KEY_JIANGLONG: bool(DEFAULT_JIANGLONG_HANG),
        PREF_KEY_AUTO_OPEN_MONSTER: bool(DEFAULT_AUTO_OPEN_MONSTER),
        PREF_KEY_OPEN_MONSTER_ROWS: int(DEFAULT_OPEN_MONSTER_ROWS),
        PREF_KEY_SKIP_STORY: bool(DEFAULT_SKIP_DUNGEON_STORY),
        PREF_KEY_AUTO_REPAIR: bool(DEFAULT_AUTO_REPAIR),
        PREF_KEY_REPAIR_PCT: float(DEFAULT_REPAIR_BELOW_PCT),
        PREF_KEY_AUTO_VITALITY: bool(DEFAULT_AUTO_VITALITY),
        PREF_KEY_VITALITY_PCT: float(DEFAULT_VITALITY_BELOW_PCT),
        PREF_KEY_IGNORE_DUNGEON_STUCK: bool(DEFAULT_IGNORE_DUNGEON_STUCK),
    }


def normalize_hang_char_id(char_id: int | str | None) -> str:
    """Normalize host character obj_id64 as hang prefs key (digits only).

    Chinese names are unstable; always key by numeric role id.
    Returns "" when missing/invalid.

    @author by ak
    """
    if char_id is None:
        return ""
    if isinstance(char_id, bool):
        return ""
    if isinstance(char_id, int):
        n = int(char_id)
        return str(n) if n > 0 else ""
    s = str(char_id).strip()
    if not s:
        return ""
    # allow pure digits (obj_id64 decimal)
    if s.isdigit():
        n = int(s)
        return str(n) if n > 0 else ""
    # tolerate "id=123" / trailing junk only if whole token int-like
    try:
        n = int(float(s))
        return str(n) if n > 0 else ""
    except Exception:
        return ""


# Backward-compatible alias (historical name-based API).
def normalize_hang_role(role: str | None) -> str:
    """Deprecated alias of normalize_hang_char_id. @author by ak"""
    return normalize_hang_char_id(role)


def _clamp_radius(v: Any, default: int = DEFAULT_HANG_RADIUS) -> int:
    try:
        n = int(float(str(v).strip()))
    except Exception:
        n = int(default)
    return max(int(AUTOPLAY_RADIUS_MEM_MIN), min(int(AUTOPLAY_RADIUS_MEM_MAX), n))


def _clamp_pct(v: Any, default: float) -> float:
    try:
        n = float(str(v).strip())
    except Exception:
        n = float(default)
    if n != n:
        n = float(default)
    return max(0.0, min(100.0, float(n)))


def _clamp_mode(v: Any, default: int = DEFAULT_HANG_MODE) -> int:
    try:
        n = int(float(str(v).strip()))
    except Exception:
        n = int(default)
    if n not in (int(AUTOPLAY_MODE_NORMAL), int(AUTOPLAY_MODE_DUNGEON)):
        return int(default)
    return int(n)


def _clamp_interval_ms(v: Any, default: int = DEFAULT_WANZI_INTERVAL_MS) -> int:
    try:
        n = int(float(str(v).strip()))
    except Exception:
        n = int(default)
    # The shared UI value is now the direct 丸子/有凤 group interval.  Keep
    # sub-50 values (e.g. the validated 40ms test) instead of normalizing them
    # back to the old recovery-slot minimum.
    # Production packages enforce the measured-safe floor (140ms); source/dev
    # builds keep the 1ms floor so the interval can still be squeezed.
    lo = 1 if is_dev_build() else int(WANZI_INTERVAL_MIN_PROD_MS)
    hi = int(AUTOPLAY_RECOVER_IV_MAX_MS)
    return max(lo, min(hi, int(n)))


def _clamp_wanzi_low_rate_seconds(
    v: Any,
    default: float = WANZI_LOW_RATE_SECONDS,
) -> float:
    try:
        value = float(str(v).strip())
    except Exception:
        value = float(default)
    if value != value:
        value = float(default)
    return max(0.0, min(120.0, value))


def _clamp_wanzi_low_rate_pairs_per_second(
    v: Any,
    default: float = WANZI_LOW_RATE_PAIRS_PER_SECOND,
) -> float:
    try:
        value = float(str(v).strip())
    except Exception:
        value = float(default)
    if value != value:
        value = float(default)
    return max(0.1, min(20.0, value))


def _clamp_wanzi_active_window_s(
    v: Any,
    default: float = WANZI_ACTIVE_WINDOW_S,
) -> float:
    try:
        value = float(str(v).strip())
    except Exception:
        value = float(default)
    if value != value:
        value = float(default)
    return max(1.0, min(7200.0, value))


def _wanzi_cadence_from_config(cfg: HangConfig | None) -> dict[str, float | int]:
    """Return validated runner cadence from a config/session overlay."""
    return {
        "low_rate_seconds": _clamp_wanzi_low_rate_seconds(
            getattr(cfg, "wanzi_low_rate_seconds", WANZI_LOW_RATE_SECONDS)
        ),
        "low_rate_pairs_per_second": _clamp_wanzi_low_rate_pairs_per_second(
            getattr(
                cfg,
                "wanzi_low_rate_pairs_per_second",
                WANZI_LOW_RATE_PAIRS_PER_SECOND,
            )
        ),
        "active_window_s": _clamp_wanzi_active_window_s(
            getattr(cfg, "wanzi_active_window_s", WANZI_ACTIVE_WINDOW_S)
        ),
    }


def _wanzi_cadence_text(
    cadence: dict[str, float | int], interval_ms: int
) -> str:
    measured = "（约350次/分钟）" if int(interval_ms) == 140 else ""
    return (
        f"每轮前{float(cadence['low_rate_seconds']):g}秒"
        f"每秒{float(cadence['low_rate_pairs_per_second']):g}次，"
        f"随后每{int(interval_ms)}ms释放1次{measured}"
        f"（持续{float(cadence['active_window_s']):g}秒）"
    )


def _as_bool(v: Any, default: bool) -> bool:
    if v is None:
        return bool(default)
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return bool(default)


def _sanitize_hang_pref_dict(raw: Any, *, base: dict | None = None) -> dict:
    """Clamp/normalize a hang pref dict onto base defaults. @author by ak"""
    out = dict(base if isinstance(base, dict) else default_hang_prefs())
    if not isinstance(raw, dict):
        return out
    if PREF_KEY_MODE in raw or "hang_mode" in raw:
        out[PREF_KEY_MODE] = _clamp_mode(
            raw.get(PREF_KEY_MODE, raw.get("hang_mode")), out[PREF_KEY_MODE]
        )
    if PREF_KEY_RADIUS in raw or "hang_radius" in raw:
        out[PREF_KEY_RADIUS] = _clamp_radius(
            raw.get(PREF_KEY_RADIUS, raw.get("hang_radius")), out[PREF_KEY_RADIUS]
        )
    if PREF_KEY_PICKUP in raw or "hang_enable_pickup" in raw:
        out[PREF_KEY_PICKUP] = _as_bool(
            raw.get(PREF_KEY_PICKUP, raw.get("hang_enable_pickup")),
            out[PREF_KEY_PICKUP],
        )
    if PREF_KEY_EMPTY in raw or "hang_empty_skill" in raw:
        out[PREF_KEY_EMPTY] = _as_bool(
            raw.get(PREF_KEY_EMPTY, raw.get("hang_empty_skill")),
            out[PREF_KEY_EMPTY],
        )
    has_legacy_wanzi = (
        PREF_KEY_WANZI in raw or "hang_wanzi_hang" in raw or "wanzi" in raw
    )
    if has_legacy_wanzi:
        out[PREF_KEY_WANZI] = _as_bool(
            raw.get(PREF_KEY_WANZI, raw.get("hang_wanzi_hang", raw.get("wanzi"))),
            out[PREF_KEY_WANZI],
        )
    has_neigong = PREF_KEY_WANZI_NEIGONG in raw or SESSION_KEY_WANZI_NEIGONG in raw
    has_waigong = PREF_KEY_WANZI_WAIGONG in raw or SESSION_KEY_WANZI_WAIGONG in raw
    if has_neigong:
        out[PREF_KEY_WANZI_NEIGONG] = _as_bool(
            raw.get(PREF_KEY_WANZI_NEIGONG, raw.get(SESSION_KEY_WANZI_NEIGONG)),
            out[PREF_KEY_WANZI_NEIGONG],
        )
    if has_waigong:
        out[PREF_KEY_WANZI_WAIGONG] = _as_bool(
            raw.get(PREF_KEY_WANZI_WAIGONG, raw.get(SESSION_KEY_WANZI_WAIGONG)),
            out[PREF_KEY_WANZI_WAIGONG],
        )
    # Version <=4 had one generic checkbox; migrate it to the already-proven
    # 外功 packet.  New explicit keys always win over this compatibility flag.
    if not has_neigong and not has_waigong and has_legacy_wanzi:
        out[PREF_KEY_WANZI_NEIGONG] = False
        out[PREF_KEY_WANZI_WAIGONG] = bool(out.get(PREF_KEY_WANZI))
    if bool(out.get(PREF_KEY_WANZI_NEIGONG)) and bool(out.get(PREF_KEY_WANZI_WAIGONG)):
        out[PREF_KEY_WANZI_WAIGONG] = False
    out[PREF_KEY_WANZI] = bool(
        out.get(PREF_KEY_WANZI_NEIGONG) or out.get(PREF_KEY_WANZI_WAIGONG)
    )
    if (
        PREF_KEY_WANZI_IV in raw
        or "hang_wanzi_interval_ms" in raw
        or "wanzi_interval" in raw
    ):
        out[PREF_KEY_WANZI_IV] = _clamp_interval_ms(
            raw.get(
                PREF_KEY_WANZI_IV,
                raw.get("hang_wanzi_interval_ms", raw.get("wanzi_interval")),
            ),
            out[PREF_KEY_WANZI_IV],
        )
    if PREF_KEY_YOUFENG in raw or "hang_youfeng_hang" in raw or "youfeng" in raw:
        out[PREF_KEY_YOUFENG] = _as_bool(
            raw.get(
                PREF_KEY_YOUFENG,
                raw.get("hang_youfeng_hang", raw.get("youfeng")),
            ),
            out[PREF_KEY_YOUFENG],
        )
    if (
        PREF_KEY_JIANGLONG in raw
        or "hang_jianglong_hang" in raw
        or "jianglong" in raw
    ):
        out[PREF_KEY_JIANGLONG] = _as_bool(
            raw.get(
                PREF_KEY_JIANGLONG,
                raw.get("hang_jianglong_hang", raw.get("jianglong")),
            ),
            out[PREF_KEY_JIANGLONG],
        )
    if PREF_KEY_AUTO_OPEN_MONSTER in raw or "hang_auto_open_monster" in raw:
        out[PREF_KEY_AUTO_OPEN_MONSTER] = _as_bool(
            raw.get(PREF_KEY_AUTO_OPEN_MONSTER, raw.get("hang_auto_open_monster")),
            out[PREF_KEY_AUTO_OPEN_MONSTER],
        )
    if PREF_KEY_OPEN_MONSTER_ROWS in raw or "hang_open_monster_rows" in raw:
        try:
            rows = int(raw.get(PREF_KEY_OPEN_MONSTER_ROWS, raw.get("hang_open_monster_rows")))
        except (TypeError, ValueError):
            rows = int(out[PREF_KEY_OPEN_MONSTER_ROWS])
        out[PREF_KEY_OPEN_MONSTER_ROWS] = max(0, min(3, rows))
    if (
        PREF_KEY_SKIP_STORY in raw
        or "hang_skip_dungeon_story" in raw
        or "skip_dungeon_story" in raw
        or "skip_story" in raw
    ):
        out[PREF_KEY_SKIP_STORY] = _as_bool(
            raw.get(
                PREF_KEY_SKIP_STORY,
                raw.get(
                    "hang_skip_dungeon_story",
                    raw.get("skip_dungeon_story", raw.get("skip_story")),
                ),
            ),
            out[PREF_KEY_SKIP_STORY],
        )
    if PREF_KEY_AUTO_REPAIR in raw or "hang_auto_repair" in raw:
        out[PREF_KEY_AUTO_REPAIR] = _as_bool(
            raw.get(PREF_KEY_AUTO_REPAIR, raw.get("hang_auto_repair")),
            out[PREF_KEY_AUTO_REPAIR],
        )
    if PREF_KEY_REPAIR_PCT in raw or "hang_repair_below_pct" in raw:
        out[PREF_KEY_REPAIR_PCT] = _clamp_pct(
            raw.get(PREF_KEY_REPAIR_PCT, raw.get("hang_repair_below_pct")),
            out[PREF_KEY_REPAIR_PCT],
        )
    if PREF_KEY_AUTO_VITALITY in raw or "hang_auto_vitality" in raw:
        out[PREF_KEY_AUTO_VITALITY] = _as_bool(
            raw.get(PREF_KEY_AUTO_VITALITY, raw.get("hang_auto_vitality")),
            out[PREF_KEY_AUTO_VITALITY],
        )
    if PREF_KEY_VITALITY_PCT in raw or "hang_vitality_below_pct" in raw:
        out[PREF_KEY_VITALITY_PCT] = _clamp_pct(
            raw.get(PREF_KEY_VITALITY_PCT, raw.get("hang_vitality_below_pct")),
            out[PREF_KEY_VITALITY_PCT],
        )
    if PREF_KEY_IGNORE_DUNGEON_STUCK in raw or "hang_ignore_dungeon_stuck" in raw:
        out[PREF_KEY_IGNORE_DUNGEON_STUCK] = _as_bool(
            raw.get(PREF_KEY_IGNORE_DUNGEON_STUCK, raw.get("hang_ignore_dungeon_stuck")),
            out[PREF_KEY_IGNORE_DUNGEON_STUCK],
        )
    elif "ignore_on_start" in raw or "hang_ignore_on_start" in raw:
        # One-way migration from the retired ordinary SetTarget(0) option.
        out[PREF_KEY_IGNORE_DUNGEON_STUCK] = _as_bool(
            raw.get("ignore_on_start", raw.get("hang_ignore_on_start")),
            out[PREF_KEY_IGNORE_DUNGEON_STUCK],
        )
    # Display name: keep for human JSON lookup; not a config key.
    name = ""
    if isinstance(raw, dict):
        name = str(
            raw.get(PREF_KEY_NAME)
            or raw.get("char_name")
            or raw.get("role_name")
            or ""
        ).strip()
    if not name and isinstance(base, dict):
        name = str(base.get(PREF_KEY_NAME) or "").strip()
    if name:
        out[PREF_KEY_NAME] = name
    elif PREF_KEY_NAME in out:
        # avoid carrying empty placeholder
        out.pop(PREF_KEY_NAME, None)
    return out


def _hang_pref_json_order(pref: dict) -> dict:
    """Put display name first for easy human scan in JSON. @author by ak"""
    if not isinstance(pref, dict):
        return {}
    out: dict[str, Any] = {}
    name = str(pref.get(PREF_KEY_NAME) or "").strip()
    if name:
        out[PREF_KEY_NAME] = name
    for k in HANG_PREFS_ALL_KEYS:
        if k in pref:
            out[k] = pref[k]
    # keep any other unknown keys last (forward compatible)
    for k, v in pref.items():
        if k in out or k == PREF_KEY_NAME:
            continue
        out[k] = v
    return out


def _empty_hang_prefs_store() -> dict:
    return {
        "version": int(HANG_PREFS_VERSION),
        "default": default_hang_prefs(),
        "by_id": {},
    }


def _is_legacy_hang_prefs_flat(data: dict) -> bool:
    """True for old single-object hang_prefs.json (no by_id/default). @author by ak"""
    if not isinstance(data, dict):
        return False
    if "by_id" in data or "by_role" in data or "default" in data:
        return False
    # old keys
    return any(k in data for k in HANG_PREFS_ALL_KEYS) or any(
        k in data
        for k in (
            "radius",
            "auto_repair",
            "repair_below_pct",
            "auto_vitality",
            "vitality_below_pct",
        )
    )


def load_hang_prefs_store() -> dict:
    """Load full hang prefs store from the per-role structure (role dirs + global default).

    Legacy hang_prefs.json is migrated once into roles/{role_id}/hang.json and
    global/hang_defaults.json, then only read as a fallback.

    @author by ak
    """
    store = _empty_hang_prefs_store()
    try:
        migrate_legacy_hang_prefs()
    except Exception:
        pass
    # default bucket: new global file wins; legacy file read-only fallback.
    default_raw: dict | None = None
    try:
        gd = _hang_defaults_path()
        if gd.is_file():
            d = json.loads(gd.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                if isinstance(d.get("default"), dict):
                    default_raw = d["default"]
                elif not any(k in d for k in ("by_id", "version")):
                    default_raw = d
    except Exception:
        default_raw = None
    legacy_default: dict | None = None
    try:
        legacy = _hang_prefs_path()
        if legacy.is_file():
            ld = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(ld, dict):
                if _is_legacy_hang_prefs_flat(ld):
                    legacy_default = ld
                elif isinstance(ld.get("default"), dict):
                    legacy_default = ld["default"]
    except Exception:
        legacy_default = None
    if default_raw is None and legacy_default is not None:
        default_raw = legacy_default
    if default_raw is None:
        default_raw = default_hang_prefs()
    store["default"] = _sanitize_hang_pref_dict(default_raw, base=default_hang_prefs())

    # by_id: per-role dirs are the source of truth.
    by_id: dict[str, dict] = {}
    try:
        from app.core.account_manager import roles_root

        root = roles_root()
        if root.is_dir():
            for child in sorted(root.iterdir(), key=lambda p: str(p.name).zfill(20)):
                if not child.is_dir():
                    continue
                key = normalize_hang_char_id(child.name)
                if not key:
                    continue
                try:
                    hang = json.loads((child / "hang.json").read_text(encoding="utf-8"))
                except Exception:
                    hang = None
                if isinstance(hang, dict):
                    by_id[key] = _hang_pref_json_order(
                        _sanitize_hang_pref_dict(hang, base=store["default"])
                    )
    except Exception:
        pass
    # legacy by_role/by_id fallback for roles not yet migrated (read-only).
    try:
        legacy = _hang_prefs_path()
        if legacy.is_file():
            ld = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(ld, dict):
                raw_id = ld.get("by_id") if isinstance(ld.get("by_id"), dict) else {}
                raw_role = (
                    ld.get("by_role") if isinstance(ld.get("by_role"), dict) else {}
                )
                merged = dict(raw_role)
                merged.update(raw_id)
                for kid, pref in merged.items():
                    key = normalize_hang_char_id(kid)
                    if not key or key in by_id:
                        continue
                    if isinstance(pref, dict):
                        by_id[key] = _hang_pref_json_order(
                            _sanitize_hang_pref_dict(pref, base=store["default"])
                        )
    except Exception:
        pass
    store["by_id"] = by_id
    store["version"] = int(HANG_PREFS_VERSION)
    return store


def save_hang_prefs_store(store: dict) -> dict:
    """Write hang prefs store to per-role dirs + global default (legacy untouched). @author by ak"""
    cur = _empty_hang_prefs_store()
    if isinstance(store, dict):
        # Always stamp current schema version on write.
        cur["version"] = int(HANG_PREFS_VERSION)
        cur["default"] = _sanitize_hang_pref_dict(
            store.get("default"), base=default_hang_prefs()
        )
        by_id_out: dict[str, dict] = {}
        raw = store.get("by_id") if isinstance(store.get("by_id"), dict) else {}
        # also accept leftover by_role numeric keys on write
        raw_role = store.get("by_role") if isinstance(store.get("by_role"), dict) else {}
        merged = dict(raw_role)
        merged.update(raw)
        for kid, pref in merged.items():
            key = normalize_hang_char_id(kid)
            if not key:
                continue
            cleaned = _sanitize_hang_pref_dict(pref, base=cur["default"])
            by_id_out[key] = _hang_pref_json_order(cleaned)
        cur["by_id"] = by_id_out
    try:
        gd = _hang_defaults_path()
        gd.parent.mkdir(parents=True, exist_ok=True)
        gd.write_text(
            json.dumps(
                {
                    "version": int(cur["version"]),
                    "default": cur["default"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass
    from app.core.account_manager import save_role_meta

    for key, pref in cur["by_id"].items():
        try:
            hpath = _role_hang_path(key)
            hpath.parent.mkdir(parents=True, exist_ok=True)
            hpath.write_text(
                json.dumps(pref, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            name = str(pref.get(PREF_KEY_NAME) or "").strip()
            if name:
                save_role_meta(key, name=name)
        except Exception:
            continue
    return cur


def load_hang_prefs(
    char_id: int | str | None = None,
    *,
    role: int | str | None = None,
) -> dict:
    """Load character prefs from roles/{role_id}/hang.json, then legacy fallback."""
    key = normalize_hang_char_id(char_id if char_id is not None else role)
    if key:
        try:
            from app.core.account_manager import load_role_hang

            role_prefs = load_role_hang(key)
            if role_prefs:
                return _sanitize_hang_pref_dict(role_prefs)
        except Exception:
            pass
    store = load_hang_prefs_store()
    base = _sanitize_hang_pref_dict(store.get("default"), base=default_hang_prefs())
    if not key:
        return base
    by_id = store.get("by_id") if isinstance(store.get("by_id"), dict) else {}
    if key in by_id:
        return _sanitize_hang_pref_dict(by_id.get(key), base=base)
    return base


def save_hang_prefs(
    *,
    char_id: int | str | None = None,
    role: int | str | None = None,
    char_name: str | None = None,
    mode: int | None = None,
    radius: int | None = None,
    enable_pickup: bool | None = None,
    empty_skill: bool | None = None,
    wanzi_hang: bool | None = None,
    wanzi_neigong_hang: bool | None = None,
    wanzi_waigong_hang: bool | None = None,
    wanzi_interval_ms: int | None = None,
    youfeng_hang: bool | None = None,
    jianglong_hang: bool | None = None,
    auto_open_monster: bool | None = None,
    open_monster_rows: int | None = None,
    skip_dungeon_story: bool | None = None,
    auto_repair: bool | None = None,
    repair_below_pct: float | None = None,
    auto_vitality: bool | None = None,
    vitality_below_pct: float | None = None,
    ignore_dungeon_stuck: bool | None = None,
    prefs: dict | None = None,
) -> dict:
    """Merge-save hang prefs for one character id (or shared default).

    Temporary API overrides must NOT call this — only UI / explicit save.
    Key is numeric role id, never Chinese display name.

    @author by ak
    """
    store = load_hang_prefs_store()
    raw_key = char_id if char_id is not None else role
    key = normalize_hang_char_id(raw_key)
    # Provided but invalid (e.g. Chinese name): refuse to clobber default.
    if raw_key is not None and str(raw_key).strip() != "" and not key:
        return _sanitize_hang_pref_dict(store.get("default"), base=default_hang_prefs())
    if key:
        cur = load_hang_prefs(key)
    else:
        cur = _sanitize_hang_pref_dict(store.get("default"), base=default_hang_prefs())

    patch: dict[str, Any] = {}
    if isinstance(prefs, dict):
        patch.update(prefs)
    if char_name is not None:
        nm = str(char_name or "").strip()
        if nm:
            patch[PREF_KEY_NAME] = nm
    if mode is not None:
        patch[PREF_KEY_MODE] = mode
    if radius is not None:
        patch[PREF_KEY_RADIUS] = radius
    if enable_pickup is not None:
        patch[PREF_KEY_PICKUP] = enable_pickup
    if empty_skill is not None:
        patch[PREF_KEY_EMPTY] = empty_skill
    if wanzi_hang is not None:
        patch[PREF_KEY_WANZI] = wanzi_hang
    if wanzi_neigong_hang is not None:
        patch[PREF_KEY_WANZI_NEIGONG] = wanzi_neigong_hang
    if wanzi_waigong_hang is not None:
        patch[PREF_KEY_WANZI_WAIGONG] = wanzi_waigong_hang
    if wanzi_interval_ms is not None:
        patch[PREF_KEY_WANZI_IV] = wanzi_interval_ms
    if youfeng_hang is not None:
        patch[PREF_KEY_YOUFENG] = youfeng_hang
    if jianglong_hang is not None:
        patch[PREF_KEY_JIANGLONG] = jianglong_hang
    if auto_open_monster is not None:
        patch[PREF_KEY_AUTO_OPEN_MONSTER] = auto_open_monster
    if open_monster_rows is not None:
        patch[PREF_KEY_OPEN_MONSTER_ROWS] = open_monster_rows
    if skip_dungeon_story is not None:
        patch[PREF_KEY_SKIP_STORY] = skip_dungeon_story
    if auto_repair is not None:
        patch[PREF_KEY_AUTO_REPAIR] = auto_repair
    if repair_below_pct is not None:
        patch[PREF_KEY_REPAIR_PCT] = repair_below_pct
    if auto_vitality is not None:
        patch[PREF_KEY_AUTO_VITALITY] = auto_vitality
    if vitality_below_pct is not None:
        patch[PREF_KEY_VITALITY_PCT] = vitality_below_pct
    if ignore_dungeon_stuck is not None:
        patch[PREF_KEY_IGNORE_DUNGEON_STUCK] = ignore_dungeon_stuck

    cur = _sanitize_hang_pref_dict(patch, base=cur)
    if key:
        by_id = store.get("by_id") if isinstance(store.get("by_id"), dict) else {}
        by_id = dict(by_id)
        by_id[key] = _hang_pref_json_order(cur)
        store["by_id"] = by_id
    else:
        # default bucket usually has no single character name
        store["default"] = _hang_pref_json_order(
            {k: v for k, v in cur.items() if k != PREF_KEY_NAME}
        )
    save_hang_prefs_store(store)
    if key:
        try:
            from app.core.account_manager import save_role_hang

            save_role_hang(key, cur)
        except Exception:
            pass
    return cur


def _cfg_from_pref_dict(prefs: dict) -> HangConfig:
    p = _sanitize_hang_pref_dict(prefs, base=default_hang_prefs())
    return _normalize_wanzi_selection(HangConfig(
        mode=_clamp_mode(p.get(PREF_KEY_MODE), DEFAULT_HANG_MODE),
        radius=_clamp_radius(p.get(PREF_KEY_RADIUS), DEFAULT_HANG_RADIUS),
        enable_pickup=_as_bool(p.get(PREF_KEY_PICKUP), DEFAULT_ENABLE_PICKUP),
        empty_skill=_as_bool(p.get(PREF_KEY_EMPTY), DEFAULT_EMPTY_SKILL),
        wanzi_hang=_as_bool(p.get(PREF_KEY_WANZI), DEFAULT_WANZI_HANG),
        wanzi_neigong_hang=_as_bool(
            p.get(PREF_KEY_WANZI_NEIGONG), DEFAULT_WANZI_NEIGONG_HANG
        ),
        wanzi_waigong_hang=_as_bool(
            p.get(PREF_KEY_WANZI_WAIGONG), DEFAULT_WANZI_WAIGONG_HANG
        ),
        wanzi_interval_ms=_clamp_interval_ms(
            p.get(PREF_KEY_WANZI_IV), DEFAULT_WANZI_INTERVAL_MS
        ),
        youfeng_hang=_as_bool(p.get(PREF_KEY_YOUFENG), DEFAULT_YOUFENG_HANG),
        jianglong_hang=_as_bool(
            p.get(PREF_KEY_JIANGLONG), DEFAULT_JIANGLONG_HANG
        ),
        auto_open_monster=_as_bool(
            p.get(PREF_KEY_AUTO_OPEN_MONSTER), DEFAULT_AUTO_OPEN_MONSTER
        ),
        open_monster_rows=max(0, min(3, int(p.get(PREF_KEY_OPEN_MONSTER_ROWS, DEFAULT_OPEN_MONSTER_ROWS) or 0))),
        skip_dungeon_story=_as_bool(
            p.get(PREF_KEY_SKIP_STORY), DEFAULT_SKIP_DUNGEON_STORY
        ),
        auto_repair=_as_bool(p.get(PREF_KEY_AUTO_REPAIR), DEFAULT_AUTO_REPAIR),
        repair_below_pct=_clamp_pct(p.get(PREF_KEY_REPAIR_PCT), DEFAULT_REPAIR_BELOW_PCT),
        auto_vitality=_as_bool(p.get(PREF_KEY_AUTO_VITALITY), DEFAULT_AUTO_VITALITY),
        vitality_below_pct=_clamp_pct(
            p.get(PREF_KEY_VITALITY_PCT), DEFAULT_VITALITY_BELOW_PCT
        ),
        ignore_dungeon_stuck=_as_bool(
            p.get(PREF_KEY_IGNORE_DUNGEON_STUCK), DEFAULT_IGNORE_DUNGEON_STUCK
        ),
    ))


def _apply_settings_overlay(cfg: HangConfig, settings: dict | None) -> HangConfig:
    """Session settings overlay (does not write disk). @author by ak"""
    s = settings if isinstance(settings, dict) else {}
    if not s:
        return cfg
    if SESSION_KEY_MODE in s:
        cfg.mode = _clamp_mode(s.get(SESSION_KEY_MODE), cfg.mode)
    if SESSION_KEY_PICKUP in s:
        cfg.enable_pickup = _as_bool(s.get(SESSION_KEY_PICKUP), cfg.enable_pickup)
    if SESSION_KEY_EMPTY in s:
        cfg.empty_skill = _as_bool(s.get(SESSION_KEY_EMPTY), cfg.empty_skill)
    if SESSION_KEY_WANZI in s:
        cfg.wanzi_hang = _as_bool(s.get(SESSION_KEY_WANZI), cfg.wanzi_hang)
        if SESSION_KEY_WANZI_NEIGONG not in s and SESSION_KEY_WANZI_WAIGONG not in s:
            cfg.wanzi_neigong_hang = False
            cfg.wanzi_waigong_hang = bool(cfg.wanzi_hang)
    if SESSION_KEY_WANZI_NEIGONG in s:
        cfg.wanzi_neigong_hang = _as_bool(
            s.get(SESSION_KEY_WANZI_NEIGONG), cfg.wanzi_neigong_hang
        )
    if SESSION_KEY_WANZI_WAIGONG in s:
        cfg.wanzi_waigong_hang = _as_bool(
            s.get(SESSION_KEY_WANZI_WAIGONG), cfg.wanzi_waigong_hang
        )
    if SESSION_KEY_WANZI_IV in s:
        cfg.wanzi_interval_ms = _clamp_interval_ms(
            s.get(SESSION_KEY_WANZI_IV), cfg.wanzi_interval_ms
        )
    if SESSION_KEY_WANZI_LOW_RATE_SECONDS in s:
        cfg.wanzi_low_rate_seconds = _clamp_wanzi_low_rate_seconds(
            s.get(SESSION_KEY_WANZI_LOW_RATE_SECONDS), cfg.wanzi_low_rate_seconds
        )
    if SESSION_KEY_WANZI_LOW_RATE_PAIRS_PER_SECOND in s:
        cfg.wanzi_low_rate_pairs_per_second = _clamp_wanzi_low_rate_pairs_per_second(
            s.get(
                SESSION_KEY_WANZI_LOW_RATE_PAIRS_PER_SECOND,
            ),
            cfg.wanzi_low_rate_pairs_per_second,
        )
    if SESSION_KEY_WANZI_ACTIVE_WINDOW_S in s:
        cfg.wanzi_active_window_s = _clamp_wanzi_active_window_s(
            s.get(SESSION_KEY_WANZI_ACTIVE_WINDOW_S), cfg.wanzi_active_window_s
        )
    if SESSION_KEY_YOUFENG in s:
        cfg.youfeng_hang = _as_bool(s.get(SESSION_KEY_YOUFENG), cfg.youfeng_hang)
    if SESSION_KEY_JIANGLONG in s:
        cfg.jianglong_hang = _as_bool(
            s.get(SESSION_KEY_JIANGLONG), cfg.jianglong_hang
        )
    if SESSION_KEY_AUTO_OPEN_MONSTER in s:
        cfg.auto_open_monster = _as_bool(s.get(SESSION_KEY_AUTO_OPEN_MONSTER), cfg.auto_open_monster)
    if SESSION_KEY_OPEN_MONSTER_ROWS in s:
        try:
            cfg.open_monster_rows = max(0, min(3, int(s.get(SESSION_KEY_OPEN_MONSTER_ROWS))))
        except (TypeError, ValueError):
            pass
    if SESSION_KEY_SKIP_STORY in s:
        cfg.skip_dungeon_story = _as_bool(
            s.get(SESSION_KEY_SKIP_STORY), cfg.skip_dungeon_story
        )
    if SESSION_KEY_RADIUS in s:
        cfg.radius = _clamp_radius(s.get(SESSION_KEY_RADIUS), cfg.radius)
    if SESSION_KEY_AUTO_REPAIR in s:
        cfg.auto_repair = _as_bool(s.get(SESSION_KEY_AUTO_REPAIR), cfg.auto_repair)
    if SESSION_KEY_REPAIR_PCT in s:
        cfg.repair_below_pct = _clamp_pct(s.get(SESSION_KEY_REPAIR_PCT), cfg.repair_below_pct)
    if SESSION_KEY_AUTO_VITALITY in s:
        cfg.auto_vitality = _as_bool(s.get(SESSION_KEY_AUTO_VITALITY), cfg.auto_vitality)
    if SESSION_KEY_VITALITY_PCT in s:
        cfg.vitality_below_pct = _clamp_pct(
            s.get(SESSION_KEY_VITALITY_PCT), cfg.vitality_below_pct
        )
    if SESSION_KEY_IGNORE_DUNGEON_STUCK in s:
        cfg.ignore_dungeon_stuck = _as_bool(
            s.get(SESSION_KEY_IGNORE_DUNGEON_STUCK), cfg.ignore_dungeon_stuck
        )
    elif "hang_ignore_on_start" in s:
        cfg.ignore_dungeon_stuck = _as_bool(
            s.get("hang_ignore_on_start"), cfg.ignore_dungeon_stuck
        )
    return _normalize_wanzi_selection(cfg)


def _apply_overrides(cfg: HangConfig, overrides: HangConfig | dict | None) -> HangConfig:
    """Apply one-shot overrides; never persists. @author by ak"""
    if overrides is None:
        return cfg
    if isinstance(overrides, HangConfig):
        return _normalize_wanzi_selection(replace(
            cfg, **{f.name: getattr(overrides, f.name) for f in fields(HangConfig)}
        ))
    if not isinstance(overrides, dict):
        return cfg
    data = dict(overrides)
    alias = {
        "hang_mode": "mode",
        "hang_radius": "radius",
        "hang_enable_pickup": "enable_pickup",
        "hang_empty_skill": "empty_skill",
        "hang_wanzi_hang": "wanzi_hang",
        "hang_wanzi_neigong_hang": "wanzi_neigong_hang",
        "hang_wanzi_waigong_hang": "wanzi_waigong_hang",
        "hang_wanzi_interval_ms": "wanzi_interval_ms",
        "hang_wanzi_low_rate_seconds": "wanzi_low_rate_seconds",
        "hang_wanzi_low_rate_pairs_per_second": "wanzi_low_rate_pairs_per_second",
        "hang_wanzi_active_window_s": "wanzi_active_window_s",
        "hang_youfeng_hang": "youfeng_hang",
        "hang_jianglong_hang": "jianglong_hang",
        "hang_skip_dungeon_story": "skip_dungeon_story",
        "wanzi": "wanzi_hang",
        "wanzi_interval": "wanzi_interval_ms",
        "youfeng": "youfeng_hang",
        "jianglong": "jianglong_hang",
        "skip_story": "skip_dungeon_story",
        "hang_auto_repair": "auto_repair",
        "hang_repair_below_pct": "repair_below_pct",
        "hang_auto_vitality": "auto_vitality",
        "hang_vitality_below_pct": "vitality_below_pct",
        "hang_ignore_dungeon_stuck": "ignore_dungeon_stuck",
        "hang_ignore_on_start": "ignore_dungeon_stuck",
        "ignore_on_start": "ignore_dungeon_stuck",
    }
    for k, v in list(data.items()):
        if k in alias:
            data[alias[k]] = v
    if "mode" in data:
        cfg.mode = _clamp_mode(data.get("mode"), cfg.mode)
    if "radius" in data:
        cfg.radius = _clamp_radius(data.get("radius"), cfg.radius)
    if "enable_pickup" in data:
        cfg.enable_pickup = _as_bool(data.get("enable_pickup"), cfg.enable_pickup)
    if "empty_skill" in data:
        cfg.empty_skill = _as_bool(data.get("empty_skill"), cfg.empty_skill)
    if "wanzi_hang" in data:
        cfg.wanzi_hang = _as_bool(data.get("wanzi_hang"), cfg.wanzi_hang)
        if "wanzi_neigong_hang" not in data and "wanzi_waigong_hang" not in data:
            cfg.wanzi_neigong_hang = False
            cfg.wanzi_waigong_hang = bool(cfg.wanzi_hang)
    if "wanzi_neigong_hang" in data:
        cfg.wanzi_neigong_hang = _as_bool(
            data.get("wanzi_neigong_hang"), cfg.wanzi_neigong_hang
        )
    if "wanzi_waigong_hang" in data:
        cfg.wanzi_waigong_hang = _as_bool(
            data.get("wanzi_waigong_hang"), cfg.wanzi_waigong_hang
        )
    if "wanzi_interval_ms" in data:
        cfg.wanzi_interval_ms = _clamp_interval_ms(
            data.get("wanzi_interval_ms"), cfg.wanzi_interval_ms
        )
    if "wanzi_low_rate_seconds" in data:
        cfg.wanzi_low_rate_seconds = _clamp_wanzi_low_rate_seconds(
            data.get("wanzi_low_rate_seconds"), cfg.wanzi_low_rate_seconds
        )
    if "wanzi_low_rate_pairs_per_second" in data:
        cfg.wanzi_low_rate_pairs_per_second = _clamp_wanzi_low_rate_pairs_per_second(
            data.get(
                "wanzi_low_rate_pairs_per_second",
            ),
            cfg.wanzi_low_rate_pairs_per_second,
        )
    if "wanzi_active_window_s" in data:
        cfg.wanzi_active_window_s = _clamp_wanzi_active_window_s(
            data.get("wanzi_active_window_s"), cfg.wanzi_active_window_s
        )
    if "youfeng_hang" in data:
        cfg.youfeng_hang = _as_bool(data.get("youfeng_hang"), cfg.youfeng_hang)
    if "jianglong_hang" in data:
        cfg.jianglong_hang = _as_bool(
            data.get("jianglong_hang"), cfg.jianglong_hang
        )
    if "auto_open_monster" in data:
        cfg.auto_open_monster = _as_bool(data.get("auto_open_monster"), cfg.auto_open_monster)
    if "open_monster_rows" in data:
        try:
            cfg.open_monster_rows = max(0, min(3, int(data.get("open_monster_rows"))))
        except (TypeError, ValueError):
            pass
    if "skip_dungeon_story" in data:
        cfg.skip_dungeon_story = _as_bool(
            data.get("skip_dungeon_story"), cfg.skip_dungeon_story
        )
    if "auto_repair" in data:
        cfg.auto_repair = _as_bool(data.get("auto_repair"), cfg.auto_repair)
    if "repair_below_pct" in data:
        cfg.repair_below_pct = _clamp_pct(data.get("repair_below_pct"), cfg.repair_below_pct)
    if "auto_vitality" in data:
        cfg.auto_vitality = _as_bool(data.get("auto_vitality"), cfg.auto_vitality)
    if "vitality_below_pct" in data:
        cfg.vitality_below_pct = _clamp_pct(
            data.get("vitality_below_pct"), cfg.vitality_below_pct
        )
    if "ignore_dungeon_stuck" in data:
        cfg.ignore_dungeon_stuck = _as_bool(
            data.get("ignore_dungeon_stuck"), cfg.ignore_dungeon_stuck
        )
    return _normalize_wanzi_selection(cfg)


def get_hang_config(
    settings: dict | None = None,
    overrides: HangConfig | dict | None = None,
    *,
    char_id: int | str | None = None,
    role: int | str | None = None,
) -> HangConfig:
    """Merge hang config without writing disk.

    Order: defaults <- roles/{role_id}/hang.json <- legacy hang_prefs <-
    settings session <- overrides. `role` is a deprecated alias of `char_id`.
    @author by ak
    """
    cid = char_id if char_id is not None else role
    role_prefs: dict = {}
    key = normalize_hang_char_id(cid)
    if key:
        try:
            from app.core.account_manager import load_role_hang

            role_prefs = load_role_hang(key)
        except Exception:
            role_prefs = {}
    prefs = role_prefs if role_prefs else load_hang_prefs(cid)
    cfg = _cfg_from_pref_dict(prefs)
    cfg = _apply_settings_overlay(cfg, settings)
    cfg = _apply_overrides(cfg, overrides)
    return _normalize_wanzi_selection(cfg)


def write_hang_config_to_settings(settings: dict, cfg: HangConfig) -> dict:
    """Mirror cfg into session settings keys (memory only). @author by ak"""
    settings[SESSION_KEY_MODE] = int(cfg.mode)
    settings[SESSION_KEY_RADIUS] = int(cfg.radius)
    settings[SESSION_KEY_PICKUP] = bool(cfg.enable_pickup)
    settings[SESSION_KEY_EMPTY] = bool(cfg.empty_skill)
    settings[SESSION_KEY_WANZI] = bool(cfg.wanzi_hang)
    settings[SESSION_KEY_WANZI_NEIGONG] = bool(cfg.wanzi_neigong_hang)
    settings[SESSION_KEY_WANZI_WAIGONG] = bool(cfg.wanzi_waigong_hang)
    settings[SESSION_KEY_WANZI_IV] = int(cfg.wanzi_interval_ms)
    settings[SESSION_KEY_WANZI_LOW_RATE_SECONDS] = float(
        _clamp_wanzi_low_rate_seconds(cfg.wanzi_low_rate_seconds)
    )
    settings[SESSION_KEY_WANZI_LOW_RATE_PAIRS_PER_SECOND] = float(
        _clamp_wanzi_low_rate_pairs_per_second(cfg.wanzi_low_rate_pairs_per_second)
    )
    settings[SESSION_KEY_WANZI_ACTIVE_WINDOW_S] = float(
        _clamp_wanzi_active_window_s(cfg.wanzi_active_window_s)
    )
    # Remove the retired development-only burst knob from existing sessions.
    settings.pop("hang_wanzi_pairs_per_active_burst", None)
    settings[SESSION_KEY_YOUFENG] = bool(cfg.youfeng_hang)
    settings[SESSION_KEY_JIANGLONG] = bool(cfg.jianglong_hang)
    settings[SESSION_KEY_AUTO_OPEN_MONSTER] = bool(cfg.auto_open_monster)
    settings[SESSION_KEY_OPEN_MONSTER_ROWS] = int(cfg.open_monster_rows)
    settings[SESSION_KEY_SKIP_STORY] = bool(cfg.skip_dungeon_story)
    settings[SESSION_KEY_AUTO_REPAIR] = bool(cfg.auto_repair)
    settings[SESSION_KEY_REPAIR_PCT] = float(cfg.repair_below_pct)
    settings[SESSION_KEY_AUTO_VITALITY] = bool(cfg.auto_vitality)
    settings[SESSION_KEY_VITALITY_PCT] = float(cfg.vitality_below_pct)
    settings[SESSION_KEY_IGNORE_DUNGEON_STUCK] = bool(cfg.ignore_dungeon_stuck)
    return settings


def sync_role_prefs_to_settings(settings: dict, char_id: int | str | None) -> dict:
    """正式面板启动时把角色磁盘配置全量同步进会话 settings（共用配置文件）。

    账号管理写的 roles/{role_id}/ 配置（inject/control/hang）在正式面板
    创建/显示时一次性同步到会话，后续读取以文件为基准。返回 settings（变）。

    @author by ak
    """
    if not isinstance(settings, dict):
        return settings
    from app.core import account_manager as _am

    rid = str(char_id or "").strip()
    if not rid:
        return settings

    # 1) hang：读角色文件全量写入会话 key（不含会话叠加，直接用文件值）。
    try:
        cfg = get_hang_config(char_id=rid)
        write_hang_config_to_settings(settings, cfg)
    except Exception:
        pass

    # 2) control（主副控 + 队内控）：角色文件优先。
    try:
        ctl = _am.load_role_control(rid)
        role = str(ctl.get("role") or "none").strip().lower()
        if role in ("master", "slave", "none"):
            settings["task_control_role"] = role
        if "team" in ctl:
            settings["team_control_enabled"] = bool(ctl.get("team", False))
    except Exception:
        pass

    # 3) inject：角色文件注入预设。
    try:
        settings["inject_enabled"] = bool(_am.load_role_inject(rid).get("enabled", True))
    except Exception:
        pass

    # 4) 组队成员：角色文件配置（roles/{role_id}/team.json）优先同步到会话。
    try:
        members = str(_am.load_role_team(rid).get("members") or "").strip()
        if members:
            settings["team_members"] = members
    except Exception:
        pass
    return settings


def save_hang_disk_from_config(
    cfg: HangConfig,
    *,
    char_id: int | str | None = None,
    role: int | str | None = None,
    char_name: str | None = None,
) -> dict:
    """Persist all hang UI fields in roles/{role_id}/hang.json.

    The legacy aggregate file remains a compatibility mirror only. A character
    id is required so a save never silently falls into the shared default bucket.
    @author by ak
    """
    cid = char_id if char_id is not None else role
    key = normalize_hang_char_id(cid)
    if not key:
        raise ValueError("当前角色 ID 未识别，无法保存挂机角色配置")
    prefs = save_hang_prefs(
        char_id=key,
        char_name=char_name,
        mode=int(cfg.mode),
        radius=int(cfg.radius),
        enable_pickup=bool(cfg.enable_pickup),
        empty_skill=bool(cfg.empty_skill),
        wanzi_hang=bool(cfg.wanzi_hang),
        wanzi_neigong_hang=bool(cfg.wanzi_neigong_hang),
        wanzi_waigong_hang=bool(cfg.wanzi_waigong_hang),
        wanzi_interval_ms=int(cfg.wanzi_interval_ms),
        youfeng_hang=bool(cfg.youfeng_hang),
        jianglong_hang=bool(cfg.jianglong_hang),
        auto_open_monster=bool(cfg.auto_open_monster),
        open_monster_rows=int(cfg.open_monster_rows),
        skip_dungeon_story=bool(cfg.skip_dungeon_story),
        auto_repair=bool(cfg.auto_repair),
        repair_below_pct=float(cfg.repair_below_pct),
        auto_vitality=bool(cfg.auto_vitality),
        vitality_below_pct=float(cfg.vitality_below_pct),
        ignore_dungeon_stuck=bool(cfg.ignore_dungeon_stuck),
    )
    from app.core.account_manager import save_role_hang

    return save_role_hang(key, prefs)


def _infer_empty_skill(skills: dict | None) -> bool | None:
    """True when slots empty and gate closed. @author by ak"""
    if not isinstance(skills, dict) or not skills.get("ok"):
        return None
    filled = int(skills.get("filled_slots") or 0)
    gate_ok = bool(skills.get("gate_ok"))
    return bool(filled == 0 and not gate_ok)


def read_hang_live(session: GameAttachSession, *, log: LogFn | None = None) -> HangLiveState:
    """Read live hang state from memory for UI / diagnostics. @author by ak"""
    log = log or (lambda _m: None)
    st = HangLiveState(ok=False)
    try:
        mem = resolve_cec_autoplay_rpm(session)
        st.detail["mem"] = mem
        st.autoplay = int(mem.get("autoplay") or 0)
        st.running = mem.get("running")
        st.mode = None
        if mem.get("mode") is not None:
            try:
                st.mode = int(mem.get("mode"))
            except Exception:
                st.mode = None
        st.mode_name = str(mem.get("mode_name") or autoplay_mode_name(st.mode))
        try:
            st.radius = int(mem.get("radius")) if mem.get("radius") is not None else None
        except Exception:
            st.radius = None
        st.ok = bool(mem.get("ok"))
        if not st.ok:
            st.error = str(mem.get("error") or "autoplay unresolved")
        skills: dict = {}
        try:
            skills = read_autoplay_skills(session)
        except Exception as e:
            skills = {"ok": False, "error": str(e)}
        st.detail["skills"] = {
            k: skills.get(k)
            for k in ("ok", "filled_slots", "gate_ok", "gate_a", "gate_b", "slot_ids", "error")
        }
        if skills.get("ok"):
            st.filled_slots = int(skills.get("filled_slots") or 0)
            st.gate_ok = bool(skills.get("gate_ok"))
            st.empty_skill_inferred = _infer_empty_skill(skills)
        dur = read_equipment_durability_pct(session, log=log)
        st.detail["durability"] = dur
        st.durability_ready = bool(dur.get("ready"))
        if dur.get("pct") is not None:
            try:
                st.durability_pct = float(dur.get("pct"))
            except Exception:
                st.durability_pct = None
        vit = read_vitality_pct(session, log=log)
        st.detail["vitality"] = vit
        st.vitality_ready = bool(vit.get("ready"))
        if vit.get("pct") is not None:
            try:
                st.vitality_pct = float(vit.get("pct"))
            except Exception:
                st.vitality_pct = None
        st.pickup_ready = bool(probe_party_auto_need_ready(session, log=log).get("ready"))
        st.loot_roll_ready = bool(probe_loot_roll_abandon_ready(session, log=log).get("ready"))
        log(
            f"hang live: run={st.running} mode={st.mode}({st.mode_name}) "
            f"r={st.radius} empty={st.empty_skill_inferred} filled={st.filled_slots}"
        )
        return st
    except Exception as e:
        st.ok = False
        st.error = str(e)
        log(f"hang live err: {e}")
        return st


def format_hang_live_line(st: HangLiveState) -> str:
    """One-line Chinese status for settings UI. @author by ak"""
    if not st.ok and st.running is None:
        return f"挂机未知 · {st.error or '-'}"
    run = "开" if st.running is True else ("关" if st.running is False else "?")
    empty = "是" if st.empty_skill_inferred is True else ("否" if st.empty_skill_inferred is False else "?")
    filled = st.filled_slots if st.filled_slots is not None else "?"
    if st.durability_pct is not None:
        dur = f"{st.durability_pct:.0f}%"
    else:
        dur = "待校准" if not st.durability_ready else "-"
    if st.vitality_pct is not None:
        vit = f"{st.vitality_pct:.0f}%"
    else:
        vit = "待校准" if not st.vitality_ready else "-"
    rad = st.radius if st.radius is not None else "-"
    return (
        f"挂机{run} · {st.mode_name} · 半径{rad} "
        f"· 空技能{empty}(槽{filled}) · 耐久{dur} · 活力{vit}"
    )



# pid -> lock / owner / last action times
_HANG_PID_LOCKS: dict[int, threading.Lock] = {}
_HANG_PID_LOCKS_GUARD = threading.Lock()
_HANG_PID_OWNER: dict[int, tuple[str, int]] = {}
_HANG_PID_LAST: dict[int, dict[str, float]] = {}
_HANG_YOUFENG_RUNNERS: dict[int, YoufengChainRunner] = {}
_HANG_YOUFENG_OWNERS: dict[int, set[str]] = {}
_HANG_YOUFENG_LOCK = threading.Lock()

# Direct 丸子 has one serialized runner per pid.  Manual and hang are logical
# owners of that same runner, mirroring the 有凤 hook lifecycle: releasing one
# owner never tears down the other owner's capability.
_HANG_WANZI_PACKET_RUNNERS: dict[int, WanziPacketRunner] = {}
_HANG_WANZI_PACKET_OWNERS: dict[int, set[str]] = {}
_HANG_WANZI_PACKET_LOCK = threading.Lock()
# Start/stop is rare but spans runner.stop(), which must not hold the state
# lock because the sender callbacks read that lock.  A separate per-PID
# lifecycle lock prevents a last-owner stop racing a new owner acquisition and
# briefly creating two packet threads.
_HANG_WANZI_PACKET_LIFECYCLE_LOCKS: dict[int, threading.RLock] = {}
_HANG_WANZI_PACKET_HANG_PAUSED: set[int] = set()
# 开挂封包窗口（发 1500 → 模式/锚点本地写完）临时冻结 hang-owned 丸子发包：
# raw_c2s 外来线程与游戏封包响应初始化并发，疑似触发游戏 AV（2026-08-30）。
# 自到期设计，调用方无需配对 resume，也不会影响 _HANG_WANZI_PACKET_HANG_PAUSED
# 的其他语义（共享手动 owner 暂停等）。
_HANG_WANZI_PACKET_TRANSITION_UNTIL: dict[int, float] = {}
_WANZI_TRANSITION_PAUSE_S = 3.0
_HANG_WANZI_AOI_TOKENS: dict[int, object] = {}
_HANG_WANZI_AOI_TOKEN_LOCK = threading.Lock()
# Native dungeon target guard is a one-shot lifecycle lease, not a periodic
# producer.  Keep ownership in Python so stop/restart can disarm exactly once.
_DUNGEON_TARGET_GUARD_ARMED: set[int] = set()
# pid → 武装时同步进原生守卫的忽略怪 TID 名单（放行看门狗的前置门 + 候选过滤）。
_DUNGEON_TARGET_GUARD_TIDS: dict[int, tuple[int, ...]] = {}
_DUNGEON_TARGET_GUARD_LOCK = threading.Lock()


def get_dungeon_guard_ignore_tids(pid: int) -> tuple[int, ...]:
    """返回该 pid 武装的忽略怪 TID 名单；未武装返回空（看门狗据此待机）。

    武装前提 = 副本模式 ∧ 「忽略副本卡怪」已勾选 ∧ 名单非空 ∧ 桥接武装成功。
    放行看门狗以此作为启动前置条件，平时绝不抢目标。

    @author by ak
    """
    with _DUNGEON_TARGET_GUARD_LOCK:
        return _DUNGEON_TARGET_GUARD_TIDS.get(int(pid or 0), ())
_HANG_WANZI_CONTROL_TOKENS: dict[int, object] = {}
_HANG_WANZI_CONTROL_TOKEN_LOCK = threading.Lock()
# Sender-side watchdog bookkeeping only.  The sender never performs RPM: it
# merely notices a stale scheduler cache and asks SafeDispatch to recreate the
# missing producer.  This closes the long-running failure mode where
# ``drop_pid``/a dead periodic removed the timer while the logical token and
# packet owner survived.
_HANG_WANZI_GATE_WATCHDOG_AT: dict[int, float] = {}
_HANG_WANZI_GATE_WATCHDOG_LOCK = threading.Lock()
HANG_WANZI_GATE_WATCHDOG_INTERVAL_S = 0.25
_HANG_WANZI_RECOVERY_WORKERS: dict[
    int, tuple[object, threading.Event, threading.Thread]
] = {}
_HANG_WANZI_RECOVERY_LOCK = threading.Lock()
HANG_WANZI_RECOVERY_INITIAL_DELAY_S = 0.18
HANG_WANZI_RECOVERY_RETRY_S = 0.45
HANG_WANZI_RECOVERY_MAX_PULSES = 8
WANZI_PACKET_OWNER_HANG = "hang"
WANZI_PACKET_OWNER_MANUAL = "manual"
HANG_WANZI_AOI_JOB_PREFIX = "hang-wanzi-aoi"
HANG_WANZI_CONTROL_JOB_PREFIX = "wanzi-control"

YOUFENG_HOOK_OWNER_HANG = "hang"
YOUFENG_HOOK_OWNER_MANUAL = "manual"

# Fallback only: a successful LootRoll packet should be mirrored into the game's
# own entry+0x1C decided byte.  If local WPM is unavailable (old snapshot/tests),
# keep a short in-process identity fence so a stale row cannot cause a packet storm.
_HANG_LOOT_DECISIONS: dict[int, set[tuple[int, int, int, int, int]]] = {}
_HANG_LOOT_DECISIONS_LOCK = threading.Lock()


def _hang_pid(session: GameAttachSession | int | None) -> int:
    if session is None:
        return 0
    if isinstance(session, int):
        return int(session)
    return int(getattr(session, "pid", 0) or 0)


def _hang_pid_lock(pid: int) -> threading.Lock:
    pid = int(pid)
    with _HANG_PID_LOCKS_GUARD:
        lk = _HANG_PID_LOCKS.get(pid)
        if lk is None:
            lk = threading.Lock()
            _HANG_PID_LOCKS[pid] = lk
        return lk


def _hang_last_map(pid: int) -> dict[str, float]:
    pid = int(pid)
    m = _HANG_PID_LAST.get(pid)
    if m is None:
        m = {}
        _HANG_PID_LAST[pid] = m
    return m



_HANG_LOG_THROTTLE: dict[str, float] = {}
_HANG_LOG_THROTTLE_LOCK = threading.Lock()


def _hang_log_throttled(
    log: LogFn | None,
    session: GameAttachSession | int | None,
    key: str,
    msg: str,
    *,
    interval_s: float | None = None,
) -> None:
    """Rate-limit high-frequency hang logs (cooldown spam). @author by ak"""
    log = log or (lambda _m: None)
    pid = _hang_pid(session)
    iv = float(HANG_LOOT_LOG_THROTTLE_S if interval_s is None else interval_s)
    full = f"{int(pid)}:{key}"
    now = time.monotonic()
    with _HANG_LOG_THROTTLE_LOCK:
        last = float(_HANG_LOG_THROTTLE.get(full) or 0.0)
        if last > 0 and (now - last) < max(0.2, iv):
            return
        _HANG_LOG_THROTTLE[full] = now
    log(msg)


def is_hang_action_busy(session: GameAttachSession | int | None = None) -> bool:
    """True when this pid already has a hang heavy action in flight. @author by ak"""
    pid = _hang_pid(session)
    if not pid:
        return False
    return pid in _HANG_PID_OWNER and _HANG_PID_OWNER.get(pid) is not None


def hang_action_owner(session: GameAttachSession | int | None = None) -> str | None:
    """Current hang action owner name for pid, or None. @author by ak"""
    pid = _hang_pid(session)
    if not pid:
        return None
    cur = _HANG_PID_OWNER.get(pid)
    if not cur:
        return None
    return str(cur[0])


def hang_action_owner_tid(session: GameAttachSession | int | None = None) -> int | None:
    """Thread id holding hang exclusive lock for pid. @author by ak"""
    pid = _hang_pid(session)
    if not pid:
        return None
    cur = _HANG_PID_OWNER.get(pid)
    if not cur:
        return None
    return int(cur[1])


def hang_cooldown_remain(
    session: GameAttachSession | int | None,
    key: str,
    *,
    cooldown_s: float,
) -> float:
    """Seconds remaining for a named cooldown; 0 if free. @author by ak"""
    pid = _hang_pid(session)
    if not pid or cooldown_s <= 0:
        return 0.0
    last = float(_hang_last_map(pid).get(str(key), 0.0) or 0.0)
    if last <= 0:
        return 0.0
    remain = float(cooldown_s) - (time.monotonic() - last)
    return remain if remain > 0 else 0.0


def _mark_hang_cooldown(
    session: GameAttachSession | int | None,
    key: str,
    *,
    hold_s: float | None = None,
    cooldown_s: float | None = None,
) -> None:
    """Stamp action cooldown.

    ``hold_s`` optionally extends the effective cooldown beyond the caller's
    default ``cooldown_s`` by back-dating the stamp forward. Example: default
    CD=0.9 and hold_s=1.25 stamps last=now+(1.25-0.9) so remain uses 0.9 still
    yields 1.25s hold.

    @author by ak
    """
    pid = _hang_pid(session)
    if not pid:
        return
    now = time.monotonic()
    stamp = now
    if hold_s is not None:
        base = float(cooldown_s if cooldown_s is not None else HANG_LOOT_COOLDOWN_S)
        try:
            extra = max(0.0, float(hold_s) - max(0.0, base))
        except Exception:
            extra = 0.0
        stamp = now + extra
    _hang_last_map(pid)[str(key)] = stamp


def _hang_check_due(
    session: GameAttachSession | int | None,
    key: str,
    *,
    interval_s: float,
    force: bool = False,
) -> tuple[bool, float]:
    """Whether a long-interval check is due. force=True always due.

    Returns (due, remain_s). remain_s>0 only when not due.

    @author by ak
    """
    if force:
        return True, 0.0
    remain = hang_cooldown_remain(session, key, cooldown_s=float(interval_s))
    if remain > 0:
        return False, float(remain)
    return True, 0.0


def _mark_hang_check_backoff(
    session: GameAttachSession | int | None,
    key: str,
    *,
    interval_s: float,
    backoff_s: float,
) -> None:
    """Mark check key so remain ~= backoff_s under interval_s cooldown.

    Used when a check hit busy: soft wait, not full 30min burn.

    @author by ak
    """
    pid = _hang_pid(session)
    if not pid:
        return
    # last = now - interval + backoff  =>  remain = interval - (now-last) = backoff
    _hang_last_map(pid)[str(key)] = (
        time.monotonic() - float(interval_s) + float(backoff_s)
    )


def _probe_remote_callable(pid: int) -> tuple[bool, str]:
    """False if pid is in remote-hung cooldown. @author by ak"""
    try:
        ensure_pid_remote_callable(int(pid))
        return True, ""
    except Exception as e:
        return False, str(e)


def _busy_skip_result(
    *,
    action: str,
    reason: str,
    message: str,
    owner: str | None = None,
    remain_s: float | None = None,
) -> dict:
    out = {
        "ok": True,
        "skipped": True,
        "reason": reason,
        "busy": True,
        "action": action,
        "message": message,
    }
    if owner:
        out["owner"] = owner
    if remain_s is not None:
        out["remain_s"] = float(remain_s)
    return out


def probe_game_call_busy(
    session: GameAttachSession | int | None,
    *,
    timeout_ms: int | None = None,
) -> tuple[bool, str]:
    """
    Probe whether shared pid Call mutex is free.

    Acquires briefly then releases. Does NOT hold across hang work
    (Call mutex is non-recursive; bridge/use_item also take it).

    Returns (busy, detail). busy=True means other module likely in remote/bridge.

    @author by ak
    """
    pid = _hang_pid(session)
    if not pid:
        return True, "no_pid"
    to = int(HANG_ACTION_MUTEX_TIMEOUT_MS if timeout_ms is None else timeout_ms)
    try:
        with pid_call_mutex(pid, timeout_ms=max(1, to), namespace=str(HANG_ACTION_MUTEX_NS)):
            return False, ""
    except TimeoutError:
        return True, "call_mutex_busy"
    except Exception as e:
        return True, f"mutex_error:{e}"


@contextmanager
def hang_action_guard(
    session: GameAttachSession,
    action: str,
    *,
    log: LogFn | None = None,
    require_remote: bool = True,
    check_call_busy: bool = False,
    mutex_timeout_ms: int | None = None,
    allow_nested_same: bool = False,
):
    """
    Hang exclusive busy guard (local per-pid).

    Safety rules:
    - Block only when this pid already has hang exclusive work, or remote is hung.
    - Same-thread nested under maintain/start/stop is allowed for child actions.
    - Other-module Call mutex occupancy is NOT a skip reason by default.
    - Never hold Call mutex across use_item/bridge/remote_call (non-recursive).
    - Yields ticket; ticket["skipped"] True means caller must no-op.

    @author by ak
    """
    log = log or (lambda _m: None)
    pid = _hang_pid(session)
    action = str(action or "hang")
    ticket: dict = {
        "ok": True,
        "skipped": False,
        "reason": "",
        "action": action,
        "pid": pid,
        "acquired_local": False,
    }
    if not pid:
        ticket.update(
            {
                "ok": False,
                "skipped": True,
                "reason": "no_pid",
                "message": "无游戏 pid，跳过",
                "busy": True,
            }
        )
        yield ticket
        return

    # cooldown for maintain tick itself
    if action == "maintain":
        remain = hang_cooldown_remain(
            pid, "maintain", cooldown_s=float(HANG_MAINTAIN_MIN_INTERVAL_S)
        )
        if remain > 0:
            ticket.update(
                _busy_skip_result(
                    action=action,
                    reason="cooldown",
                    message=f"挂机维护冷却中 {remain:.1f}s",
                    remain_s=remain,
                )
            )
            _hang_log_throttled(
                log, pid, "maintain_cd", f"hang guard: {ticket['message']}", interval_s=4.0
            )
            yield ticket
            return

    if require_remote:
        ok_r, err_r = _probe_remote_callable(pid)
        if not ok_r:
            ticket.update(
                _busy_skip_result(
                    action=action,
                    reason="remote_hung",
                    message=f"远程调用冷却中，跳过{action}: {err_r}",
                )
            )
            log(f"hang guard: {ticket['message']}")
            yield ticket
            return

    # Optional Call-mutex probe (off by default). Other modules holding
    # Call must NOT block hang; only enable when caller explicitly wants it.
    if check_call_busy and action in HANG_HEAVY_ACTIONS:
        busy, detail = probe_game_call_busy(
            pid,
            timeout_ms=int(
                HANG_ACTION_MUTEX_TIMEOUT_MS
                if mutex_timeout_ms is None
                else mutex_timeout_ms
            ),
        )
        if busy:
            ticket.update(
                _busy_skip_result(
                    action=action,
                    reason=detail or "call_mutex_busy",
                    message=f"本进程远程调用通道忙，跳过{action}",
                )
            )
            log(f"hang guard: {ticket['message']}")
            yield ticket
            return

    lk = _hang_pid_lock(pid)
    owner_info = _HANG_PID_OWNER.get(pid)
    owner = owner_info[0] if owner_info else None
    owner_tid = int(owner_info[1]) if owner_info else None
    self_tid = int(threading.get_ident())
    got_local = lk.acquire(blocking=False)
    if not got_local:
        same_thread = owner_tid is not None and owner_tid == self_tid
        if same_thread and allow_nested_same and owner == action:
            ticket["reason"] = "nested_same"
            ticket["owner"] = owner
            yield ticket
            return
        # Only SAME thread may nest under parent hang section.
        # Other threads must skip — never pile concurrent hang/remote work.
        if same_thread and owner == "maintain" and action in (
            "repair",
            "vitality",
            "loot_abandon",
            "loot_need",
        ):
            ticket["reason"] = "nested_maintain"
            ticket["owner"] = owner
            yield ticket
            return
        if same_thread and owner in ("start_hang", "stop_hang") and action in (
            "repair",
            "vitality",
            "loot_abandon",
            "loot_need",
            "party_auto_need",
            "prepare",
            "maintain",
        ):
            ticket["reason"] = "nested_start_stop"
            ticket["owner"] = owner
            yield ticket
            return
        ticket.update(
            _busy_skip_result(
                action=action,
                reason="hang_busy",
                message=f"挂机动作繁忙(占用:{owner or '?'})，跳过{action}",
                owner=owner,
            )
        )
        log(f"hang guard: {ticket['message']}")
        yield ticket
        return

    ticket["acquired_local"] = True
    _HANG_PID_OWNER[pid] = (action, self_tid)
    try:
        if require_remote:
            ok_r, err_r = _probe_remote_callable(pid)
            if not ok_r:
                ticket.update(
                    _busy_skip_result(
                        action=action,
                        reason="remote_hung",
                        message=f"远程调用冷却中，跳过{action}: {err_r}",
                    )
                )
                log(f"hang guard: {ticket['message']}")
                yield ticket
                return
        yield ticket
    finally:
        if ticket.get("acquired_local"):
            cur = _HANG_PID_OWNER.get(pid)
            if cur and cur[0] == action:
                _HANG_PID_OWNER.pop(pid, None)
            try:
                lk.release()
            except Exception:
                pass



def _resolve_host_data_rpm(session: GameAttachSession) -> int:
    """RPM path: *(*(NOTE_VA_GAME_ROOT)+0x24)+0x90. @author by ak"""
    g_va = int(_note_live_va(session, NOTE_VA_GAME_ROOT_GLOBAL) or 0)
    if not g_va:
        return 0
    root = _rpm_u32(session, g_va) or 0
    if not root:
        return 0
    mid = _rpm_u32(session, int(root) + int(HOST_DATA_MID_OFF)) or 0
    if not mid:
        return 0
    return int(_rpm_u32(session, int(mid) + int(HOST_DATA_LEAF_OFF)) or 0)


def _resolve_host_data(session: GameAttachSession, *, log: LogFn | None = None) -> int:
    """RPM-only host_data read: *(*(GAME_ROOT)+0x24)+0x90. @author by ak

    远程 GetHostData 回退已禁用：该外来线程调用与游戏封包响应初始化并发时
    会在游戏进程内卡死并 AV（2026-08-30 切糕开挂崩溃 0xC0000005 现场，
    远程线程永不返回且随后进程退出）。RPM 读不到时返回 0，调用方按
    "待校准"跳过即可，不再向游戏注入额外线程。
    """
    log = log or (lambda _m: None)
    hd = _resolve_host_data_rpm(session)
    if not hd:
        log("hang host_data: rpm miss; remote fallback disabled, keep 0")
    return int(hd)


def probe_party_auto_need_ready(
    session: GameAttachSession | None = None, *, log: LogFn | None = None
) -> dict:
    """内挂组队自动需求是否已校准. @author by ak"""
    if session is None:
        return {
            "ready": True,
            "offset": AUTOPLAY_PICK_FLAGS_OFF,
            "bit": AUTOPLAY_TEAM_AUTO_NEED_BIT,
            "note": "CECAutoPlay+0x1D bit0x10",
        }
    try:
        mem = resolve_cec_autoplay_rpm(session)
        ap = int(mem.get("autoplay") or 0)
        if not ap:
            return {"ready": False, "error": str(mem.get("error") or "no autoplay")}
        cur = _rpm_u8(session, ap + int(AUTOPLAY_PICK_FLAGS_OFF))
        if cur is None:
            return {"ready": False, "error": "read flags failed"}
        return {
            "ready": True,
            "autoplay": ap,
            "flags": int(cur),
            "enabled": bool(int(cur) & int(AUTOPLAY_TEAM_AUTO_NEED_BIT)),
            "offset": AUTOPLAY_PICK_FLAGS_OFF,
            "bit": AUTOPLAY_TEAM_AUTO_NEED_BIT,
        }
    except Exception as e:
        return {"ready": False, "error": str(e)}


def apply_party_auto_need(
    session: GameAttachSession,
    enabled: bool,
    *,
    log: LogFn | None = None,
) -> dict:
    """写内挂 Chk_TeamAutoPick（CECAutoPlay+0x1D bit0x10）. @author by ak"""
    log = log or (lambda _m: None)
    with hang_action_guard(
        session,
        "party_auto_need",
        log=log,
        require_remote=False,
        check_call_busy=False,
    ) as gate:
        if gate.get("skipped"):
            return {
                "ok": True,
                "skipped": True,
                "busy": True,
                "reason": gate.get("reason") or "busy",
                "enabled": bool(enabled),
                "message": gate.get("message") or "拾取设置跳过(繁忙)",
            }
        return _apply_party_auto_need_unlocked(session, enabled, log=log)


def _apply_party_auto_need_unlocked(
    session: GameAttachSession,
    enabled: bool,
    *,
    log: LogFn | None = None,
) -> dict:
    """TeamAutoPick write body. @author by ak"""
    log = log or (lambda _m: None)
    try:
        mem = resolve_cec_autoplay_rpm(session)
        ap = int(mem.get("autoplay") or 0)
        if not ap:
            msg = f"无 CECAutoPlay: {mem.get('error') or 'null'}"
            log(f"hang pickup: {msg}")
            return {"ok": False, "error": msg, "enabled": bool(enabled), "message": msg}
        addr = int(ap) + int(AUTOPLAY_PICK_FLAGS_OFF)
        before = _rpm_u8(session, addr)
        if before is None:
            msg = "读拾取标志失败"
            log(f"hang pickup: {msg}")
            return {"ok": False, "error": msg, "enabled": bool(enabled), "message": msg}
        if enabled:
            after_v = int(before) | int(AUTOPLAY_TEAM_AUTO_NEED_BIT)
        else:
            after_v = int(before) & (~int(AUTOPLAY_TEAM_AUTO_NEED_BIT) & 0xFF)
        if after_v == int(before):
            msg = f"组队自动需求已是{'开' if enabled else '关'}"
            log(f"hang pickup: {msg}")
            return {
                "ok": True,
                "skipped": True,
                "reason": "already",
                "enabled": bool(enabled),
                "before": int(before),
                "after": int(after_v),
                "message": msg,
            }
        if not _wpm_u8(session, addr, after_v):
            msg = "写组队自动需求失败"
            log(f"hang pickup: {msg}")
            return {"ok": False, "error": msg, "enabled": bool(enabled), "message": msg}
        readback = _rpm_u8(session, addr)
        ok = readback is not None and int(readback) == int(after_v)
        msg = f"组队自动需求={'开' if enabled else '关'} (flags {before:02X}->{after_v:02X})"
        log(f"hang pickup: ok={ok} {msg}")
        return {
            "ok": bool(ok),
            "enabled": bool(enabled),
            "before": int(before),
            "after": int(after_v),
            "readback": None if readback is None else int(readback),
            "autoplay": int(ap),
            "message": msg,
        }
    except Exception as e:
        msg = f"组队自动需求异常: {e}"
        log(f"hang pickup: {msg}")
        return {"ok": False, "error": str(e), "enabled": bool(enabled), "message": msg}


def _loot_roll_ids_sane(id0: int, id1: int, id2: int) -> bool:
    """Reject garbage triples (dialog-slot padding / stale mem). @author by ak"""
    id0, id1, id2 = int(id0), int(id1), int(id2)
    if id0 <= 0 or id0 > 0x00FFFFFF:
        return False
    if id2 != 0 and id2 > 0x00100000:
        return False
    # Live common form: id1=0x02000000, id2=0
    if id1 == int(LOOT_ROLL_ID1_FLAG) and id2 == 0:
        return True
    if id1 == 0 and id2 == 0 and 1000 < id0 < 200000:
        return True
    return False


def _iter_active_loot_rolls(session: GameAttachSession, *, log: LogFn | None = None) -> list[dict]:
    """List pending loot-roll entries.

    Active = decided==0 and time_cur < time_max, plus sane id triple.
    (Stale pool often keeps decided==0 with time_cur >= time_max.)

    @author by ak
    """
    log = log or (lambda _m: None)
    out: list[dict] = []
    hd = _resolve_host_data(session, log=log)
    if not hd:
        return out
    side = _rpm_u32(session, int(hd) + int(HOST_DATA_LOOT_SIDE_OFF)) or 0
    if not side:
        return out
    mgr = _rpm_u32(session, int(side) + int(LOOT_MGR_PTR_OFF)) or 0
    if not mgr:
        return out
    count = int(_rpm_u32(session, int(mgr) + int(LOOT_MGR_COUNT_OFF)) or 0)
    arr = int(_rpm_u32(session, int(mgr) + int(LOOT_MGR_ARR_OFF)) or 0)
    if not arr or count <= 0:
        return out
    n = min(int(count), int(LOOT_MGR_COUNT_CAP))
    pid = int(session.pid)
    for i in range(n):
        ep = _rpm_u32(session, arr + i * 4) or 0
        if not ep or ep < 0x10000:
            continue
        try:
            raw = remote_read_bytes(pid, int(ep), 0x20)
        except Exception:
            continue
        if len(raw) < 0x20:
            continue
        decided = raw[int(LOOT_ENTRY_DECIDED_OFF)]
        t_cur = int.from_bytes(raw[int(LOOT_ENTRY_TIME_OFF) : int(LOOT_ENTRY_TIME_OFF) + 4], "little")
        t_max = int.from_bytes(
            raw[int(LOOT_ENTRY_TIME_MAX_OFF) : int(LOOT_ENTRY_TIME_MAX_OFF) + 4], "little"
        )
        # Active window only
        if decided != 0 or t_max <= 0 or t_cur >= t_max:
            continue
        id0 = int.from_bytes(raw[int(LOOT_ENTRY_ID0_OFF) : int(LOOT_ENTRY_ID0_OFF) + 4], "little")
        id1 = int.from_bytes(raw[int(LOOT_ENTRY_ID1_OFF) : int(LOOT_ENTRY_ID1_OFF) + 4], "little")
        id2 = int.from_bytes(raw[int(LOOT_ENTRY_ID2_OFF) : int(LOOT_ENTRY_ID2_OFF) + 4], "little")
        if not _loot_roll_ids_sane(id0, id1, id2):
            continue
        out.append(
            {
                "index": i,
                "entry": int(ep),
                "id0": id0,
                "id1": id1,
                "id2": id2,
                "time": t_cur,
                "time_max": t_max,
            }
        )
    return out


def probe_loot_roll_abandon_ready(
    session: GameAttachSession | None = None, *, log: LogFn | None = None
) -> dict:
    """LootRoll 全部放弃是否已校准. @author by ak"""
    base = {
        "ready": True,
        "note": "cc8a50 choice=2 Pass",
        "va": NOTE_VA_LOOT_ROLL_PKT,
        "choice": LOOT_ROLL_CHOICE_PASS,
    }
    if session is None:
        return base
    try:
        pending = _iter_active_loot_rolls(session, log=log)
        base["pending"] = len(pending)
        base["host_data"] = _resolve_host_data(session, log=log)
        return base
    except Exception as e:
        return {"ready": False, "error": str(e)}


def abandon_all_loot_rolls(
    session: GameAttachSession, *, log: LogFn | None = None
) -> dict:
    """Pass/abandon pending loot rolls (choice=2).

    正确枚举 + 按波发包；发包成功后写 entry+0x1C=1 打通客户端 UI 刷新链路。
    不等待服务器确认，游戏超时仍会补救漏点。
    节奏：cooldown + burst cap + inter-packet gap；无条目不 CRT。

    @author by ak
    """
    log = log or (lambda _m: None)
    remain = hang_cooldown_remain(
        session, "loot_abandon", cooldown_s=float(HANG_LOOT_COOLDOWN_S)
    )
    if remain > 0:
        msg = f"全部放弃冷却中 {remain:.1f}s"
        _hang_log_throttled(log, session, "loot_abandon_cd", f"hang loot: {msg}")
        return _busy_skip_result(
            action="loot_abandon", reason="cooldown", message=msg, remain_s=remain
        )
    # 只枚举待掷点并发包；不等待服务器确认，发后补本地 decided。
    try:
        pending = _iter_active_loot_rolls(session, log=log)
    except Exception as e:
        pending = []
        log(f"hang loot: list pending err {e}")
    if not pending:
        msg = "无待掷点条目，跳过全部放弃"
        _hang_log_throttled(log, session, "loot_abandon_empty", f"hang loot: {msg}", interval_s=8.0)
        # Do NOT long-cooldown empty: rolls can appear any second.
        return {
            "ok": True,
            "skipped": True,
            "reason": "no_pending",
            "count": 0,
            "sent": 0,
            "message": msg,
        }
    with hang_action_guard(
        session, "loot_abandon", log=log, require_remote=True, check_call_busy=False
    ) as gate:
        if gate.get("skipped"):
            return {
                "ok": True,
                "skipped": True,
                "busy": True,
                "reason": gate.get("reason") or "busy",
                "message": gate.get("message") or "全部放弃跳过(繁忙)",
            }
        ret = _abandon_all_loot_rolls_unlocked(session, log=log, pending=pending)
        # Always cool down after an attempt (success or partial) to avoid spam.
        # Full clear: hold longer so public-drop storms cannot re-enter CRT at
        # guard-tick frequency. Partial waves keep the normal wave cooldown.
        if int(ret.get("sent") or 0) > 0 and int(ret.get("remain") or 0) == 0:
            _mark_hang_cooldown(
                session,
                "loot_abandon",
                hold_s=float(HANG_LOOT_POST_CLEAR_COOLDOWN_S),
                cooldown_s=float(HANG_LOOT_COOLDOWN_S),
            )
        else:
            _mark_hang_cooldown(session, "loot_abandon")
        return ret


def _abandon_all_loot_rolls_unlocked(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
    pending: list[dict] | None = None,
) -> dict:
    """Loot abandon body (caller holds hang_action_guard). @author by ak"""
    return _send_loot_rolls(
        session,
        choice=int(LOOT_ROLL_CHOICE_PASS),
        action="loot_abandon",
        log=log,
        pending=pending,
    )


def read_equipment_durability_pct(
    session: GameAttachSession, *, log: LogFn | None = None
) -> dict:
    """读已装备整体耐久百分比（均值，过滤 max_pts 过小的时装噪声）. @author by ak"""
    log = log or (lambda _m: None)
    try:
        items = list_package_items(session, int(EQUIP_PACKAGE_INDEX), log=log) or []
    except Exception as e:
        return {
            "ready": False,
            "pct": None,
            "error": f"list equip failed: {e}",
            "note": "装备包枚举失败",
        }
    pid = int(session.pid)
    rows: list[dict] = []
    for it in items:
        ptr = int(getattr(it, "ptr", 0) or 0)
        if not ptr:
            continue
        try:
            raw = remote_read_bytes(pid, ptr, 0x100)
        except Exception:
            continue
        if len(raw) < int(ITEM_DURA_CUR_OFF) + 4:
            continue
        flag = raw[int(ITEM_DURA_FLAG_OFF)] if len(raw) > int(ITEM_DURA_FLAG_OFF) else 0
        if not flag:
            continue
        cur = int.from_bytes(raw[int(ITEM_DURA_CUR_OFF) : int(ITEM_DURA_CUR_OFF) + 4], "little")
        tmpl = int.from_bytes(raw[int(ITEM_TMPL_OFF) : int(ITEM_TMPL_OFF) + 4], "little")
        if not tmpl or tmpl < 0x10000:
            continue
        try:
            tr = remote_read_bytes(pid, tmpl, int(ITEM_TMPL_MAX_DURA_OFF) + 4)
        except Exception:
            continue
        if len(tr) < int(ITEM_TMPL_MAX_DURA_OFF) + 4:
            continue
        max_pts = int.from_bytes(
            tr[int(ITEM_TMPL_MAX_DURA_OFF) : int(ITEM_TMPL_MAX_DURA_OFF) + 4], "little"
        )
        if max_pts <= 0:
            continue
        max_scaled = int(max_pts) * 100
        pct = 100.0 * float(cur) / float(max_scaled) if max_scaled else 0.0
        if pct < 0:
            pct = 0.0
        if pct > 100.0:
            pct = 100.0
        rows.append(
            {
                "slot": int(getattr(it, "slot", -1)),
                "tid": int(getattr(it, "tid", 0) or 0),
                "name": str(getattr(it, "name", "") or ""),
                "cur": int(cur),
                "max_pts": int(max_pts),
                "max_scaled": int(max_scaled),
                "pct": float(pct),
            }
        )
    if not rows:
        return {
            "ready": True,
            "pct": None,
            "count": 0,
            "note": "无带耐久的已装备物品",
            "items": [],
        }
    combat = [r for r in rows if int(r["max_pts"]) >= int(DURA_MIN_MAX_PTS)]
    use = combat or rows
    mean_pct = sum(float(r["pct"]) for r in use) / float(len(use))
    min_pct = min(float(r["pct"]) for r in use)
    log(
        f"hang dura: mean={mean_pct:.1f}% min={min_pct:.1f}% "
        f"n={len(use)}/{len(rows)} (filter max_pts>={DURA_MIN_MAX_PTS})"
    )
    return {
        "ready": True,
        "pct": float(mean_pct),
        "min_pct": float(min_pct),
        "count": len(use),
        "total_with_dura": len(rows),
        "items": use,
        "note": "cur=item+0xEE, max=*(tmpl+0x19C)*100",
    }


# --- host death state machine (per-pid) ---
_HANG_DEAD_STATE: dict[int, dict] = {}
_HANG_DEAD_STATE_LOCK = threading.Lock()


def get_hang_dead_state(session: GameAttachSession | int | None = None) -> bool | None:
    """Read a fresh host-dead hint without doing CRT.

    A cached ``True`` must not live forever, otherwise a player that has revived
    can still receive automated Need packets for later rolls.

    @author by ak
    """
    pid = _hang_pid(session)
    if not pid:
        return None
    with _HANG_DEAD_STATE_LOCK:
        st = _HANG_DEAD_STATE.get(int(pid)) or {}
    dead = st.get("dead")
    if dead is None:
        return None
    try:
        age = time.monotonic() - float(st.get("ts") or 0.0)
    except Exception:
        age = float("inf")
    if age > float(HANG_DEAD_POLL_INTERVAL_S):
        return None
    return bool(dead)


def _set_hang_dead_state(pid: int, dead: bool | None, *, source: str = "") -> bool | None:
    """Update death SM; log transitions. Returns new dead flag. @author by ak"""
    pid = int(pid)
    now = time.monotonic()
    with _HANG_DEAD_STATE_LOCK:
        prev = _HANG_DEAD_STATE.get(pid) or {}
        prev_dead = prev.get("dead")
        entry = {
            "dead": None if dead is None else bool(dead),
            "ts": now,
            "source": str(source or ""),
            "prev": prev_dead,
        }
        _HANG_DEAD_STATE[pid] = entry
    # Mirror into global live hub so map/pos/death share one consumer surface.
    try:
        from app.core.live_scene_hub import publish_live_scene

        publish_live_scene(
            pid,
            dead=None if dead is None else bool(dead),
            source=f"dead:{source or 'sm'}",
            touch_dead=True,
        )
    except Exception:
        pass
    return None if dead is None else bool(dead)


def refresh_hang_dead_state(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
    force: bool = False,
    min_interval_s: float | None = None,
) -> bool | None:
    """Poll the UI-thread host snapshot into the per-pid death state machine.

    Scene loads and role transitions return unknown; no foreign remote thread is
    created and no fallback retry is attempted. Returns the cached/updated dead
    flag (None if unknown).

    @author by ak
    """
    log = log or (lambda _m: None)
    pid = _hang_pid(session)
    if not pid:
        return None
    iv = float(
        HANG_DEAD_POLL_INTERVAL_S if min_interval_s is None else min_interval_s
    )
    now = time.monotonic()
    with _HANG_DEAD_STATE_LOCK:
        cur = dict(_HANG_DEAD_STATE.get(int(pid)) or {})
    last_ts = float(cur.get("ts") or 0.0)
    if (not force) and last_ts > 0 and (now - last_ts) < max(0.2, iv):
        d = cur.get("dead")
        return None if d is None else bool(d)

    def _unknown(
        source: str, detail: str = "", *, preserve_cached: bool = True
    ) -> bool | None:
        age = now - last_ts if last_ts > 0 else 1e9
        if (
            preserve_cached
            and age <= float(HANG_DEAD_UNKNOWN_TTL_S)
            and cur.get("dead") is not None
        ):
            return bool(cur.get("dead"))
        _set_hang_dead_state(pid, None, source=source)
        if detail:
            log(f"hang dead: unknown {source} {detail}")
        return None

    bridge = None
    try:
        from app.core.remote_runtime import (
            is_pid_scene_snapshot_stable,
            note_pid_scene_snapshot,
        )
        from app.core.xajh_bridge import XajhBridge

        bridge = XajhBridge(int(pid), log=log)
        if not bridge.open(quiet=True):
            return _unknown("bridge_unavailable")
        result = bridge.host_snapshot(
            hwnd=int(getattr(session, "hwnd", 0) or 0) or None,
            timeout_ms=700,
        )
        if not result.ok:
            return _unknown(
                "snapshot_error", str(result.error or result.note or "")
            )

        host_present = bool(int(result.ret or 0))
        scene_id = int(getattr(result, "mode", 0) or 0)
        note_pid_scene_snapshot(
            int(pid), host_present=host_present, scene_id=scene_id
        )
        if not host_present or scene_id <= 0:
            return _unknown("scene_transition", preserve_cached=False)
        if not is_pid_scene_snapshot_stable(int(pid)):
            return _unknown("scene_settling", preserve_cached=False)

        dead_state = int(getattr(result, "tid", -1))
        death_marker = str(getattr(result, "note", "") or "")
        if (
            dead_state not in (0, 1)
            or death_marker != f"HOST_SNAPSHOT ok dead={dead_state}"
        ):
            return _unknown("dead_unknown", preserve_cached=False)
        dead_v = bool(dead_state)
    except Exception as e:
        return _unknown("snapshot_exception", str(e))
    finally:
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass

    prev = cur.get("dead")
    _set_hang_dead_state(pid, dead_v, source="ui_host_snapshot")
    if prev is None or bool(prev) != bool(dead_v):
        log(f"hang dead: {prev} -> {dead_v}")
    return bool(dead_v)


def _loot_choice_label(choice: int) -> str:
    c = int(choice)
    if c == int(LOOT_ROLL_CHOICE_NEED):
        return "需求"
    if c == int(LOOT_ROLL_CHOICE_GREED):
        return "贪婪"
    if c == int(LOOT_ROLL_CHOICE_PASS):
        return "放弃"
    return f"choice={c}"


def _loot_roll_key(ent: dict) -> tuple[int, int, int, int, int]:
    """Stable identity for one visible roll row; timer progress is intentionally excluded."""
    return (
        int(ent.get("entry") or 0),
        int(ent.get("id0") or 0),
        int(ent.get("id1") or 0),
        int(ent.get("id2") or 0),
        int(ent.get("time_max") or 0),
    )



def _loot_roll_entry_still_active(
    session: GameAttachSession, ent: dict, *, log: LogFn | None = None
) -> bool:
    """Re-check one manager entry immediately before CRT packet.

    Dense public loot can free/recycle rows between the wave snapshot and the
    actual remote_call. Sending Pass/Need against a recycled entry is a known
    ACCESS_VIOLATION risk for 0xCC8A50.

    @author by ak
    """
    log = log or (lambda _m: None)
    ep = int(ent.get("entry") or 0)
    if ep < 0x10000:
        # Snapshot without entry pointer: cannot revalidate; keep old behavior.
        return True
    pid = int(getattr(session, "pid", 0) or 0)
    if pid <= 0:
        return False
    try:
        from app.core.remote_runtime import remote_read_bytes

        raw = remote_read_bytes(pid, int(ep), 0x20)
    except Exception as e:
        log(f"hang loot: precheck read fail entry=0x{ep:X} err={e}")
        return False
    if not raw or len(raw) < 0x20:
        return False
    decided = int(raw[int(LOOT_ENTRY_DECIDED_OFF)]) & 0xFF
    if decided:
        return False
    t_cur = int.from_bytes(
        raw[int(LOOT_ENTRY_TIME_OFF) : int(LOOT_ENTRY_TIME_OFF) + 4], "little"
    )
    t_max = int.from_bytes(
        raw[int(LOOT_ENTRY_TIME_MAX_OFF) : int(LOOT_ENTRY_TIME_MAX_OFF) + 4], "little"
    )
    if t_max > 0 and t_cur >= t_max:
        return False
    id0 = int.from_bytes(
        raw[int(LOOT_ENTRY_ID0_OFF) : int(LOOT_ENTRY_ID0_OFF) + 4], "little"
    )
    id1 = int.from_bytes(
        raw[int(LOOT_ENTRY_ID1_OFF) : int(LOOT_ENTRY_ID1_OFF) + 4], "little"
    )
    id2 = int.from_bytes(
        raw[int(LOOT_ENTRY_ID2_OFF) : int(LOOT_ENTRY_ID2_OFF) + 4], "little"
    )
    if (id0, id1, id2) != (
        int(ent.get("id0") or 0),
        int(ent.get("id1") or 0),
        int(ent.get("id2") or 0),
    ):
        # Entry slot recycled to another roll id triple.
        return False
    return bool(_loot_roll_ids_sane(id0, id1, id2))


def _filter_undecided_loot_rolls(
    session: GameAttachSession, pending: list[dict]
) -> tuple[list[dict], int]:
    """Prune vanished decisions and return rows that have not received a packet."""
    pid = int(getattr(session, "pid", 0) or 0)
    if pid <= 0:
        return list(pending), 0
    current = {_loot_roll_key(ent) for ent in pending}
    with _HANG_LOOT_DECISIONS_LOCK:
        decided = _HANG_LOOT_DECISIONS.get(pid, set())
        decided.intersection_update(current)
        if decided:
            _HANG_LOOT_DECISIONS[pid] = decided
        else:
            _HANG_LOOT_DECISIONS.pop(pid, None)
        out = [ent for ent in pending if _loot_roll_key(ent) not in decided]
    return out, len(pending) - len(out)


def _remember_loot_decision(session: GameAttachSession, ent: dict) -> None:
    pid = int(getattr(session, "pid", 0) or 0)
    if pid <= 0:
        return
    with _HANG_LOOT_DECISIONS_LOCK:
        _HANG_LOOT_DECISIONS.setdefault(pid, set()).add(_loot_roll_key(ent))


def _mark_loot_roll_local_decided(
    session: GameAttachSession,
    ent: dict,
    *,
    choice: int,
    log: LogFn | None = None,
) -> dict:
    """Mirror the native button handler's local UI decision mark.

    RE evidence (0xA3DB97 / 0xA3DC67 / 0xA3DDA7): the game button handler sets
    entry+0x1C = 1 before calling the same 0xCC8A50 packet routine.  Our direct
    packet call used to skip that local mutation, leaving Win_LootRoll visible
    and causing the guard to enumerate the same row again.  We do not wait for a
    server ack here; we only make the client memory/UI path match a real click.

    @author by ak
    """
    log = log or (lambda _m: None)
    ep = int(ent.get("entry") or 0)
    if ep < 0x10000:
        return {
            "ok": False,
            "skipped": True,
            "reason": "no_entry",
            "choice": int(choice),
        }
    addr = ep + int(LOOT_ENTRY_DECIDED_OFF)
    before = None
    after = None
    try:
        before_v = _rpm_u8(session, addr)
        before = None if before_v is None else int(before_v) & 0xFF
    except Exception:
        before = None
    try:
        ok_w = bool(_wpm_u8(session, addr, 1))
    except Exception as e:
        msg = f"local decided WPM failed: {e}"
        log(f"hang loot: {msg} entry=0x{ep:X}")
        return {
            "ok": False,
            "error": str(e),
            "reason": "wpm_failed",
            "entry": ep,
            "addr": addr,
            "choice": int(choice),
            "before": before,
        }
    try:
        after_v = _rpm_u8(session, addr)
        after = None if after_v is None else int(after_v) & 0xFF
    except Exception:
        after = None
    ok = bool(ok_w and (after is None or after != 0))
    if ok:
        try:
            ent["decided"] = 1
        except Exception:
            pass
    else:
        log(
            f"hang loot: local decided write not verified entry=0x{ep:X} "
            f"before={before} after={after}"
        )
    return {
        "ok": bool(ok),
        "entry": ep,
        "addr": addr,
        "choice": int(choice),
        "before": before,
        "after": after,
        "wrote": bool(ok_w),
    }


def _settle_expired_loot_rolls_local(session: GameAttachSession) -> dict:
    """Mark expired, still-undecided valid manager rows as locally settled.

    The game keeps expired pool rows after their timer has elapsed.  Their
    ``decided`` byte can remain zero forever, which makes Win_LootRoll re-show
    even after the last real choice was sent.  These rows cannot receive a
    choice any more (``time >= time_max``), so this is a local UI cleanup only.

    @author by ak
    """
    out = {"ok": True, "candidates": 0, "written": 0, "failed": 0}
    hd = int(_resolve_host_data(session, log=lambda _m: None) or 0)
    side = int(_rpm_u32(session, hd + HOST_DATA_LOOT_SIDE_OFF) or 0) if hd else 0
    mgr = int(_rpm_u32(session, side + LOOT_MGR_PTR_OFF) or 0) if side else 0
    if not mgr:
        return out
    count = int(_rpm_u32(session, mgr + LOOT_MGR_COUNT_OFF) or 0)
    arr = int(_rpm_u32(session, mgr + LOOT_MGR_ARR_OFF) or 0)
    if count <= 0 or not arr:
        return out
    pid = int(getattr(session, "pid", 0) or 0)
    for i in range(min(count, int(LOOT_MGR_COUNT_CAP))):
        ep = int(_rpm_u32(session, arr + i * 4) or 0)
        if ep < 0x10000:
            continue
        try:
            raw = remote_read_bytes(pid, ep, 0x20)
        except Exception:
            continue
        if len(raw) < 0x20 or raw[int(LOOT_ENTRY_DECIDED_OFF)] != 0:
            continue
        id0 = int.from_bytes(raw[LOOT_ENTRY_ID0_OFF : LOOT_ENTRY_ID0_OFF + 4], "little")
        id1 = int.from_bytes(raw[LOOT_ENTRY_ID1_OFF : LOOT_ENTRY_ID1_OFF + 4], "little")
        id2 = int.from_bytes(raw[LOOT_ENTRY_ID2_OFF : LOOT_ENTRY_ID2_OFF + 4], "little")
        t_cur = int.from_bytes(raw[LOOT_ENTRY_TIME_OFF : LOOT_ENTRY_TIME_OFF + 4], "little")
        t_max = int.from_bytes(raw[LOOT_ENTRY_TIME_MAX_OFF : LOOT_ENTRY_TIME_MAX_OFF + 4], "little")
        if t_max <= 0 or t_cur < t_max or not _loot_roll_ids_sane(id0, id1, id2):
            continue
        out["candidates"] += 1
        try:
            if _wpm_u8(session, ep + LOOT_ENTRY_DECIDED_OFF, 1):
                out["written"] += 1
            else:
                out["failed"] += 1
        except Exception:
            out["failed"] += 1
    out["ok"] = out["failed"] == 0
    return out


def _refresh_empty_loot_roll_ui(
    session: GameAttachSession, *, log: LogFn | None = None
) -> dict:
    """Hide a stale LootRoll window only after a fresh empty manager scan.

    Native Need/Greed/Pass marks ``entry+0x1C`` and sends the packet, but does
    not close the dialog itself. If its UI event is delayed, an empty dialog
    remains painted. The generic AUI Show call is not reliable from this
    worker; the dialog's own IsShow byte is the state the UI reads. This never
    writes while an active Roll remains.

    @author by ak
    """
    log = log or (lambda _m: None)
    out = {
        "ok": True,
        "skipped": True,
        "reason": "",
        "active": 0,
        "shown_before": False,
        "shown_after": False,
        "dlg": 0,
    }
    try:
        expired = _settle_expired_loot_rolls_local(session)
        out["expired"] = expired
    except Exception as e:
        out.update(ok=False, reason="expired_settle_failed", error=str(e))
        return out
    if int(expired.get("failed") or 0) > 0:
        out.update(ok=False, reason="expired_settle_incomplete")
        return out
    try:
        active = _iter_active_loot_rolls(session, log=lambda _m: None)
    except Exception as e:
        out.update(ok=False, reason="active_scan_failed", error=str(e))
        return out
    out["active"] = len(active)
    if active:
        out["reason"] = "active_remaining"
        return out
    try:
        from app.core.plg_ui import query_dlg_show

        hit = query_dlg_show(session, LOOT_ROLL_DLG_NAME, log=lambda _m: None)
    except Exception as e:
        out.update(ok=False, reason="dialog_query_failed", error=str(e))
        return out
    dlg = int(getattr(hit, "dlg_ptr", 0) or 0)
    if dlg < 0x10000:
        out.update(dlg=dlg, shown_before=False)
        out["reason"] = "already_hidden"
        out["shown_after"] = False
        return out
    addr = dlg + int(LOOT_ROLL_DLG_ISSHOW_OFF)
    try:
        before = _rpm_u8(session, addr)
    except Exception:
        before = None
    # IsDlgShow may lag or report false for an AUI dialog that is still painted.
    # The dialog's own +0x94 byte is the authoritative local visibility state.
    shown = (
        bool(int(before) & 0xFF)
        if before is not None
        else bool(getattr(hit, "shown", False))
    )
    out.update(dlg=dlg, shown_before=shown, addr=addr)
    if not shown:
        out["reason"] = "already_hidden"
        out["shown_after"] = False
        return out
    # The first scan happened before resolving the dialog. Recheck immediately
    # before clearing IsShow so a freshly arrived Roll cannot be hidden.
    try:
        active_after_query = _iter_active_loot_rolls(session, log=lambda _m: None)
    except Exception as e:
        out.update(ok=False, reason="active_rescan_failed", error=str(e))
        return out
    if active_after_query:
        out.update(
            skipped=True,
            reason="active_arrived",
            active=len(active_after_query),
        )
        return out
    try:
        wrote = bool(_wpm_u8(session, addr, 0))
        after = _rpm_u8(session, addr)
        hidden = after is not None and (int(after) & 0xFF) == 0
    except Exception as e:
        out.update(ok=False, skipped=False, reason="hide_write_failed", error=str(e))
        return out
    out.update(
        ok=bool(wrote and hidden),
        skipped=False,
        reason="hidden" if wrote and hidden else "hide_not_verified",
        wrote=bool(wrote),
        addr=addr,
        is_show_after=None if after is None else int(after) & 0xFF,
        shown_after=not bool(hidden),
    )
    # Success hide is routine while abandoning rolls; keep only failures.
    if not out["ok"]:
        log(
            f"hang loot: local UI hide not verified dlg=0x{dlg:X} "
            f"wrote={wrote} is_show={out.get('is_show_after')}"
        )
    return out


def _send_loot_rolls(
    session: GameAttachSession,
    *,
    choice: int,
    action: str,
    log: LogFn | None = None,
    pending: list[dict] | None = None,
) -> dict:
    """Paced multi-packet loot rolls for one choice.

    Packet success is followed by local entry+0x1C decided=1, matching the
    native Need/Greed/Pass handlers so the client UI and our next RPM poll stop
    seeing the same row.  No server confirmation wait is performed.

    @author by ak
    """
    log = log or (lambda _m: None)
    label = _loot_choice_label(choice)
    try:
        if pending is None:
            pending = _iter_active_loot_rolls(session, log=log)
        if not pending:
            msg = f"无进行中的 Roll，跳过{label}"
            log(f"hang loot: {msg}")
            return {
                "ok": True,
                "skipped": True,
                "reason": "no_pending",
                "choice": int(choice),
                "count": 0,
                "sent": 0,
                "message": msg,
            }
        pending, already_decided = _filter_undecided_loot_rolls(session, list(pending))
        if not pending:
            msg = f"{label}已发，等待掷骰条目关闭"
            log(f"hang loot: {msg}")
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_decided",
                "choice": int(choice),
                "count": 0,
                "sent": 0,
                "already_decided": int(already_decided),
                "message": msg,
            }
        va = int(_note_live_va(session, NOTE_VA_LOOT_ROLL_PKT) or 0)
        if not va:
            msg = "LootRoll 发包地址无效"
            log(f"hang loot: {msg}")
            return {"ok": False, "error": msg, "message": msg, "choice": int(choice)}
        pid = int(session.pid)
        sent = 0
        errors: list[str] = []
        details: list[dict] = []
        max_burst = max(1, int(HANG_LOOT_MAX_PER_BURST))
        gap = max(0.0, float(HANG_LOOT_INTER_PKT_S))
        # Prefer soon-to-timeout entries first; still cover whole list within burst.
        ordered = sorted(
            list(pending),
            key=lambda e: (
                int(e.get("time_max") or 0) - int(e.get("time") or 0),
                int(e.get("index") or 0),
            ),
        )
        batch = ordered[:max_burst]
        skipped_stale = 0
        for i, ent in enumerate(batch):
            # Keep the configured gap as a start-to-start minimum. A congested
            # CRT call may already consume more than the whole interval; adding
            # another fixed sleep made dense Roll waves unnecessarily slow.
            packet_started = time.monotonic()
            try:
                if not _loot_roll_entry_still_active(session, ent, log=log):
                    skipped_stale += 1
                    _remember_loot_decision(session, ent)
                    details.append(
                        {
                            "ok": False,
                            "skipped": True,
                            "reason": "stale_or_recycled",
                            "choice": int(choice),
                            **ent,
                        }
                    )
                    continue
                remote_call_cdecl_x86(
                    pid,
                    va,
                    [
                        int(ent["id0"]) & 0xFFFFFFFF,
                        int(ent["id1"]) & 0xFFFFFFFF,
                        int(ent["id2"]) & 0xFFFFFFFF,
                        int(choice) & 0xFF,
                    ],
                    timeout_ms=3000,
                )
                sent += 1
                local = _mark_loot_roll_local_decided(
                    session, ent, choice=int(choice), log=log
                )
                _remember_loot_decision(session, ent)
                details.append(
                    {
                        "ok": True,
                        "choice": int(choice),
                        "local_decided": bool(local.get("ok")),
                        "local": local,
                        **ent,
                    }
                )
            except TimeoutError as e:
                errors.append(f"busy/timeout: {e}")
                details.append({"ok": False, "error": str(e), "busy": True, **ent})
                break
            except Exception as e:
                errors.append(str(e))
                details.append({"ok": False, "error": str(e), **ent})
            if gap > 0 and i + 1 < len(batch) and not errors:
                try:
                    elapsed = max(0.0, time.monotonic() - packet_started)
                    remain_gap = max(0.0, gap - elapsed)
                    if remain_gap > 0:
                        time.sleep(remain_gap)
                except Exception:
                    pass
        # 发包成功后已本地标记 decided；不等待服务器确认/回包。
        remain = max(0, len(pending) - len(batch))
        ok = sent > 0 and not errors
        msg = f"{label}已发 {sent}/{len(pending)}"
        if skipped_stale:
            msg += f"，跳过失效{skipped_stale}"
        if remain:
            msg += f"（本波上限{max_burst}，余{remain}下轮）"
        if errors:
            msg += f"，失败{len(errors)}"
        # Routine 放弃/需求 success is high-frequency noise; log failures only.
        if (not ok) or errors:
            log(f"hang loot: pid={pid} ok={ok} action={action} {msg}")
        ui_refresh = None
        if sent > 0:
            # 失效所有 roll 缓存：下一波必须读到 entry+0x1C 的本地标记。
            try:
                from app.core.safe_dispatch import get_dispatch, roll_cache_key
                from app.core.state_dispatch import StateKind, invalidate_states

                get_dispatch().invalidate(int(pid), roll_cache_key())
                invalidate_states(int(pid), StateKind.ROLL)
            except Exception:
                pass
            # Only hide dialog after this snapshot is fully drained. Partial
            # waves still have pending rows; hiding early causes re-show churn.
            if remain == 0:
                ui_refresh = _refresh_empty_loot_roll_ui(session, log=log)
        return {
            "ok": bool(ok or (sent > 0 and not errors)),
            "choice": int(choice),
            "action": action,
            "count": sent,
            "sent": sent,
            "pending": len(pending),
            "already_decided": int(already_decided),
            "skipped_stale": int(skipped_stale),
            "remain": remain,
            "burst_cap": max_burst,
            "errors": errors,
            "details": details,
            "ui_refresh": ui_refresh,
            "message": msg,
        }
    except Exception as e:
        msg = f"{label}异常: {e}"
        log(f"hang loot: {msg}")
        return {"ok": False, "error": str(e), "message": msg, "choice": int(choice)}


def need_all_loot_rolls(
    session: GameAttachSession, *, log: LogFn | None = None
) -> dict:
    """Need/需求 pending loot rolls (choice=0). Used when dead + pickup on.

    与放弃相同：正确发包后补本地 decided，不等待服务器确认。

    @author by ak
    """
    log = log or (lambda _m: None)
    remain = hang_cooldown_remain(
        session, "loot_need", cooldown_s=float(HANG_LOOT_COOLDOWN_S)
    )
    if remain > 0:
        msg = f"死亡需求冷却中 {remain:.1f}s"
        _hang_log_throttled(log, session, "loot_need_cd", f"hang loot: {msg}")
        return _busy_skip_result(
            action="loot_need", reason="cooldown", message=msg, remain_s=remain
        )
    try:
        pending = _iter_active_loot_rolls(session, log=log)
    except Exception as e:
        pending = []
        log(f"hang loot: list pending err {e}")
    if not pending:
        msg = "无待掷点条目，跳过死亡需求"
        _hang_log_throttled(log, session, "loot_need_empty", f"hang loot: {msg}", interval_s=8.0)
        return {
            "ok": True,
            "skipped": True,
            "reason": "no_pending",
            "count": 0,
            "sent": 0,
            "choice": int(LOOT_ROLL_CHOICE_NEED),
            "message": msg,
        }
    with hang_action_guard(
        session, "loot_need", log=log, require_remote=True, check_call_busy=False
    ) as gate:
        if gate.get("skipped"):
            return {
                "ok": True,
                "skipped": True,
                "busy": True,
                "reason": gate.get("reason") or "busy",
                "message": gate.get("message") or "死亡需求跳过(繁忙)",
            }
        ret = _send_loot_rolls(
            session,
            choice=int(LOOT_ROLL_CHOICE_NEED),
            action="loot_need",
            log=log,
            pending=pending,
        )
        if int(ret.get("sent") or 0) > 0 and int(ret.get("remain") or 0) == 0:
            _mark_hang_cooldown(
                session,
                "loot_need",
                hold_s=float(HANG_LOOT_POST_CLEAR_COOLDOWN_S),
                cooldown_s=float(HANG_LOOT_COOLDOWN_S),
            )
        else:
            _mark_hang_cooldown(session, "loot_need")
        return ret



def read_vitality_pct(
    session: GameAttachSession, *, log: LogFn | None = None
) -> dict:
    """读活力百分比：host_data+0x3C 对象 +0x90/+0x94（type6 池）。

    旧 host_data+0x30/+0x10 已废弃（计时器抖动）。
    仅 RPM；失败或校验不过则 ready=False，不驱动补药。

    @author by ak
    """
    log = log or (lambda _m: None)
    if not VITALITY_RE_READY:
        return {
            "ready": False,
            "pct": None,
            "cur": None,
            "max": None,
            "error": VITALITY_RE_NOTE,
            "note": "pending_re",
        }
    try:
        hd = _resolve_host_data(session, log=log)
        if not hd:
            return {
                "ready": False,
                "pct": None,
                "cur": None,
                "max": None,
                "error": "no host_data",
            }
        obj = _rpm_u32(session, int(hd) + int(HOST_DATA_VITALITY_OBJ_OFF)) or 0
        if not obj or obj < 0x10000:
            return {
                "ready": False,
                "pct": None,
                "cur": None,
                "max": None,
                "error": "no vitality obj",
                "host_data": int(hd),
            }
        cur = _rpm_u32(session, int(obj) + int(VITALITY_CUR_OFF))
        mx = _rpm_u32(session, int(obj) + int(VITALITY_MAX_OFF))
        if cur is None or mx is None:
            return {
                "ready": False,
                "pct": None,
                "cur": cur,
                "max": mx,
                "error": "read cur/max failed",
                "obj": int(obj),
            }
        cur_i = int(cur)
        mx_i = int(mx)
        # Sanity: known max pools; reject ticking-timer class values.
        if mx_i <= 0 or cur_i < 0 or cur_i > mx_i:
            return {
                "ready": False,
                "pct": None,
                "cur": cur_i,
                "max": mx_i,
                "error": "vitality range suspicious",
                "obj": int(obj),
                "note": VITALITY_RE_NOTE,
            }
        if mx_i not in VITALITY_KNOWN_MAX and mx_i > 12000:
            return {
                "ready": False,
                "pct": None,
                "cur": cur_i,
                "max": mx_i,
                "error": f"vitality max uncommon ({mx_i})",
                "obj": int(obj),
                "note": VITALITY_RE_NOTE,
            }
        pct = 100.0 * float(cur_i) / float(mx_i)
        # Quiet by default; caller may log detail from return dict.
        return {
            "ready": True,
            "pct": float(pct),
            "cur": cur_i,
            "max": mx_i,
            "obj": int(obj),
            "host_data": int(hd),
            "note": VITALITY_RE_NOTE,
        }
    except Exception as e:
        return {
            "ready": False,
            "pct": None,
            "cur": None,
            "max": None,
            "error": str(e),
        }



def _ensure_session_hwnd(session: GameAttachSession, *, log: LogFn | None = None) -> int:
    """Fill session.hwnd from process main window when missing. @author by ak"""
    log = log or (lambda _m: None)
    try:
        hwnd = int(getattr(session, "hwnd", 0) or 0)
    except Exception:
        hwnd = 0
    if hwnd:
        return hwnd
    pid = _hang_pid(session)
    if not pid:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        found: list[int] = []

        @EnumWindowsProc
        def _cb(hwnd_i, _lp):  # type: ignore[no-untyped-def]
            p = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd_i, ctypes.byref(p))
            if int(p.value) != int(pid) or not user32.IsWindowVisible(hwnd_i):
                return True
            title = (ctypes.c_wchar * 256)()
            user32.GetWindowTextW(hwnd_i, title, 256)
            if title.value:
                found.append(int(hwnd_i))
            return True

        user32.EnumWindows(_cb, 0)
        if found:
            hwnd = int(found[0])
            try:
                session.hwnd = hwnd
            except Exception:
                pass
            return hwnd
    except Exception as e:
        log(f"hang hwnd resolve err: {e}")
    return 0



def _confirm_repair_dialog(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
    wait_s: float = REPAIR_CONFIRM_WAIT_S,
) -> dict:
    """
    Confirm Game_RepairWithItem after using 工匠修理箱.

    Prefer named button rect click; fallback dialog geometry 确定 zone.
    Live: confirm closes dialog, bag -1, equip dura ~100%.

    @author by ak
    """
    log = log or (lambda _m: None)
    _ensure_session_hwnd(session, log=log)
    hwnd = int(getattr(session, "hwnd", 0) or 0)
    out: dict = {
        "ok": False,
        "shown": False,
        "clicked": False,
        "via": "",
        "message": "",
        "dlg": REPAIR_CONFIRM_DLG,
    }
    try:
        from app.core.plg_ui import query_dlg_show, read_dlg_rect
        from app.core.aui_click import (
            get_aui_dlg_item_ptr,
            read_aui_ctrl_rect,
            click_client_via_bridge,
        )
    except Exception as e:
        out["message"] = f"确认模块不可用: {e}"
        log(f"hang repair confirm: {out['message']}")
        return out

    deadline = time.monotonic() + max(0.2, float(wait_s))
    dlg_ptr = 0
    while time.monotonic() < deadline:
        q = query_dlg_show(session, REPAIR_CONFIRM_DLG, log=lambda _m: None)
        if q.ok and q.shown and int(q.dlg_ptr or 0):
            dlg_ptr = int(q.dlg_ptr)
            out["shown"] = True
            break
        time.sleep(0.08)
    if not dlg_ptr:
        out["message"] = "未出现修理确认框"
        log(f"hang repair confirm: {out['message']}")
        return out

    # 1) named buttons
    for name in REPAIR_CONFIRM_BTN_NAMES:
        try:
            ptr = get_aui_dlg_item_ptr(session, dlg_ptr, name, log=lambda _m: None)
            if not ptr:
                continue
            rect = read_aui_ctrl_rect(session, ptr, log=lambda _m: None)
            if not rect or not getattr(rect, "ok", False):
                continue
            w = int(getattr(rect, "w", 0) or 0)
            h = int(getattr(rect, "h", 0) or 0)
            if w <= 2 or h <= 2:
                continue
            cx = int(getattr(rect, "x", 0) + w / 2)
            cy = int(getattr(rect, "y", 0) + h / 2)
            if click_client_via_bridge(session, hwnd, cx, cy, log=log):
                out["clicked"] = True
                out["via"] = f"btn:{name}"
                break
        except Exception as e:
            log(f"hang repair confirm btn {name} err: {e}")

    # 2) geometry fallback (确定 left of 取消)
    if not out["clicked"]:
        try:
            dr = read_dlg_rect(session, dlg_ptr, name=REPAIR_CONFIRM_DLG, log=lambda _m: None)
            if dr and getattr(dr, "ok", False):
                cx = int(dr.x + float(dr.w) * float(REPAIR_CONFIRM_RATIO_X))
                cy = int(dr.y + float(dr.h) * float(REPAIR_CONFIRM_RATIO_Y))
                if click_client_via_bridge(session, hwnd, cx, cy, log=log):
                    out["clicked"] = True
                    out["via"] = "geom"
        except Exception as e:
            log(f"hang repair confirm geom err: {e}")

    if not out["clicked"]:
        out["message"] = "修理确认框点击失败"
        log(f"hang repair confirm: {out['message']}")
        return out

    # wait dialog hide
    hide_deadline = time.monotonic() + 1.5
    closed = False
    while time.monotonic() < hide_deadline:
        q2 = query_dlg_show(session, REPAIR_CONFIRM_DLG, log=lambda _m: None)
        if not (q2.ok and q2.shown):
            closed = True
            break
        time.sleep(0.08)
    out["ok"] = True
    out["closed"] = closed
    out["message"] = (
        f"已确认修理({out['via']})" + (" · 对话框已关" if closed else " · 对话框仍开")
    )
    log(f"hang repair confirm: {out['message']}")
    return out


def _use_repair_box(session: GameAttachSession, *, log: LogFn | None = None) -> dict:
    """Use 工匠修理箱 from bag by tid then name. @author by ak"""
    log = log or (lambda _m: None)
    remain = hang_cooldown_remain(session, "repair", cooldown_s=float(HANG_REPAIR_COOLDOWN_S))
    if remain > 0:
        msg = f"维修冷却中 {remain:.1f}s"
        log(f"hang repair: {msg}")
        return _busy_skip_result(
            action="repair", reason="cooldown", message=msg, remain_s=remain
        )
    with hang_action_guard(session, "repair", log=log, require_remote=True) as gate:
        if gate.get("skipped"):
            return {
                "ok": True,
                "skipped": True,
                "busy": True,
                "reason": gate.get("reason") or "busy",
                "message": gate.get("message") or "维修跳过(繁忙)",
            }
        ret = _use_repair_box_unlocked(session, log=log)
        if ret.get("ok") or ret.get("reason") in ("used", "no_effect"):
            _mark_hang_cooldown(session, "repair")
        return ret


def _use_repair_box_unlocked(session: GameAttachSession, *, log: LogFn | None = None) -> dict:
    """Repair box use body + Game_RepairWithItem confirm. @author by ak"""
    log = log or (lambda _m: None)
    _ensure_session_hwnd(session, log=log)
    try:
        items = list_packages_items(session, log=log)
    except Exception as e:
        return {"ok": False, "error": f"list bag failed: {e}", "message": "列背包失败"}
    target = None
    for it in items or []:
        if int(getattr(it, "tid", 0) or 0) == int(REPAIR_BOX_TID):
            target = it
            break
    if target is None:
        for it in items or []:
            name = str(getattr(it, "name", "") or "")
            if REPAIR_BOX_NAME in name:
                target = it
                break
    if target is None:
        msg = f"背包无{REPAIR_BOX_NAME}"
        log(f"hang repair: {msg}")
        return {"ok": False, "skipped": True, "reason": "no_item", "message": msg}

    dur_before = read_equipment_durability_pct(session, log=lambda _m: None)
    pct_before = None
    try:
        if dur_before.get("pct") is not None:
            pct_before = float(dur_before.get("pct"))
    except Exception:
        pct_before = None

    item_info = {
        "package": int(target.package),
        "slot": int(target.slot),
        "tid": int(getattr(target, "tid", 0) or 0),
        "name": str(getattr(target, "name", "") or ""),
    }

    try:
        # Fire use; bag may not change until confirm.
        r = use_item_in_package(
            session,
            int(target.package),
            int(target.slot),
            log=log,
            prefer_bridge=True,
            verify_bag=False,
            bag_wait_s=0.1,
        )
        ret_i = int(getattr(r, "ret", 0) or 0)
        call_ok = bool(getattr(r, "ok", False)) or bool(ret_i & 0xFF)
        if not call_ok:
            msg = getattr(r, "message", "") or "维修箱使用失败"
            log(f"hang repair: ok=False {msg}")
            return {
                "ok": False,
                "reason": "use_failed",
                "message": msg,
                "pct_before": pct_before,
                "item": item_info,
                "detail": r.to_dict() if hasattr(r, "to_dict") else None,
            }

        conf = _confirm_repair_dialog(session, log=log)
        time.sleep(0.35)
        dur_after = read_equipment_durability_pct(session, log=lambda _m: None)
        pct_after = None
        try:
            if dur_after.get("pct") is not None:
                pct_after = float(dur_after.get("pct"))
        except Exception:
            pct_after = None
        improved = (
            pct_before is not None
            and pct_after is not None
            and (pct_after - pct_before) >= 0.5
        )

        if improved:
            ok = True
            reason = "used"
            msg = f"维修生效 耐久{pct_before:.0f}%→{pct_after:.0f}%"
            if conf.get("via"):
                msg += f" · 确认={conf.get('via')}"
        elif conf.get("ok") and conf.get("closed"):
            # Confirmed and dialog closed — treat as success even if pct read lagging.
            ok = True
            reason = "confirmed"
            msg = conf.get("message") or "已确认修理"
        elif conf.get("shown") and not conf.get("clicked"):
            ok = False
            reason = "confirm_failed"
            msg = conf.get("message") or "修理确认失败"
        elif not conf.get("shown"):
            # No confirm UI: full durability usually skips the dialog.
            if pct_before is not None and pct_before >= 99.0:
                ok = True
                reason = "already_full"
                msg = f"装备耐久已满({pct_before:.0f}%)，无需修理"
            else:
                ok = True
                reason = "no_confirm_ui"
                msg = (
                    f"维修调用已送达(ret={ret_i})但未出现确认框"
                    + (f"（耐久{pct_before:.0f}%）" if pct_before is not None else "")
                )
        else:
            ok = bool(conf.get("ok"))
            reason = "confirm_pending" if conf.get("clicked") else "no_effect"
            msg = conf.get("message") or "维修结果未知"

        log(f"hang repair: ok={ok} reason={reason} {msg}")
        return {
            "ok": ok,
            "skipped": reason in ("no_confirm_ui", "no_effect", "already_full"),
            "reason": reason,
            "message": msg,
            "pct_before": pct_before,
            "pct_after": pct_after,
            "confirm": conf,
            "item": item_info,
            "detail": r.to_dict() if hasattr(r, "to_dict") else None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "message": f"维修异常: {e}"}



def _use_vitality_potion(
    session: GameAttachSession, *, log: LogFn | None = None
) -> dict:
    """Use first bag item matching 活力药 keywords. @author by ak"""
    log = log or (lambda _m: None)
    remain = hang_cooldown_remain(
        session, "vitality", cooldown_s=float(HANG_VITALITY_COOLDOWN_S)
    )
    if remain > 0:
        msg = f"补活力冷却中 {remain:.1f}s"
        log(f"hang vitality: {msg}")
        return _busy_skip_result(
            action="vitality", reason="cooldown", message=msg, remain_s=remain
        )
    with hang_action_guard(session, "vitality", log=log, require_remote=True) as gate:
        if gate.get("skipped"):
            return {
                "ok": True,
                "skipped": True,
                "busy": True,
                "reason": gate.get("reason") or "busy",
                "message": gate.get("message") or "补活力跳过(繁忙)",
            }
        ret = _use_vitality_potion_unlocked(session, log=log)
        if ret.get("ok") or ret.get("reason") in ("used", "no_effect", "already_full"):
            _mark_hang_cooldown(session, "vitality")
        return ret


def _use_vitality_potion_unlocked(
    session: GameAttachSession, *, log: LogFn | None = None
) -> dict:
    """Vitality potion body. @author by ak"""
    log = log or (lambda _m: None)
    _ensure_session_hwnd(session, log=log)

    vit_before = read_vitality_pct(session, log=lambda _m: None)
    pct_before = None
    cur_before = None
    try:
        if vit_before.get("pct") is not None:
            pct_before = float(vit_before.get("pct"))
        if vit_before.get("cur") is not None:
            cur_before = int(vit_before.get("cur"))
    except Exception:
        pass
    # Already full: do not spam use (game will not consume).
    if pct_before is not None and pct_before >= 99.5:
        msg = f"活力已满({pct_before:.0f}%)，跳过用药"
        log(f"hang vitality: {msg}")
        return {
            "ok": True,
            "skipped": True,
            "reason": "already_full",
            "message": msg,
            "pct": pct_before,
            "cur": cur_before,
        }

    try:
        r = force_use_lab_bag_item(
            session,
            keyword=VITALITY_ITEM_KEYWORDS[0],
            log=log,
            prefer_bridge=True,
        )
        if r.get("ok"):
            return {"ok": True, "message": "已使用活力药", "detail": r, "reason": "used"}
    except Exception as e:
        log(f"hang vitality force_use err: {e}")

    try:
        items = list_packages_items(session, log=log)
    except Exception as e:
        return {"ok": False, "error": f"list bag failed: {e}", "message": "列背包失败"}
    target = None
    for it in items or []:
        name = str(getattr(it, "name", "") or "")
        if any(k in name for k in VITALITY_ITEM_KEYWORDS):
            target = it
            break
    if target is None:
        try:
            hits = find_items_by_name(session, "活力", log=log)
            if hits:
                target = hits[0]
        except Exception:
            pass
    if target is None:
        msg = "背包无活力药"
        log(f"hang vitality: {msg}")
        return {"ok": False, "skipped": True, "reason": "no_item", "message": msg}
    try:
        uret = use_item_in_package(
            session,
            int(target.package),
            int(target.slot),
            log=log,
            prefer_bridge=True,
            verify_bag=True,
            bag_wait_s=0.8,
        )
        bag_ok = bool(getattr(uret, "ok", False))
        ret_i = int(getattr(uret, "ret", 0) or 0)
        time.sleep(0.25)
        vit_after = read_vitality_pct(session, log=lambda _m: None)
        pct_after = None
        cur_after = None
        try:
            if vit_after.get("pct") is not None:
                pct_after = float(vit_after.get("pct"))
            if vit_after.get("cur") is not None:
                cur_after = int(vit_after.get("cur"))
        except Exception:
            pass
        rose = (
            cur_before is not None
            and cur_after is not None
            and cur_after > cur_before
        ) or (
            pct_before is not None
            and pct_after is not None
            and (pct_after - pct_before) >= 0.5
        )
        if bag_ok or rose:
            ok = True
            reason = "used"
            msg = "活力药已用"
            if rose and cur_before is not None and cur_after is not None:
                msg = f"补活力生效 {cur_before}→{cur_after}"
        elif ret_i & 0xFF:
            ok = True
            reason = "no_effect"
            msg = (
                f"活力药调用已送达(ret={ret_i})但未消耗"
                + (
                    f"（当前{cur_after if cur_after is not None else cur_before}"
                    f"/{vit_after.get('max') if vit_after.get('max') is not None else vit_before.get('max')}）"
                    if (cur_after is not None or cur_before is not None)
                    else ""
                )
            )
            # treat full pool soft-ok
            if pct_after is not None and pct_after >= 99.5:
                reason = "already_full"
                msg = f"活力已满({pct_after:.0f}%)，游戏未消耗药水"
        else:
            ok = False
            reason = "use_failed"
            msg = getattr(uret, "message", "") or "活力药使用失败"
        log(f"hang vitality: ok={ok} reason={reason} {msg}")
        return {
            "ok": ok,
            "skipped": reason in ("no_effect", "already_full"),
            "reason": reason,
            "message": msg,
            "pct_before": pct_before,
            "pct_after": pct_after,
            "cur_before": cur_before,
            "cur_after": cur_after,
            "item": {
                "package": int(target.package),
                "slot": int(target.slot),
                "tid": int(getattr(target, "tid", 0) or 0),
                "name": str(getattr(target, "name", "") or ""),
            },
            "detail": uret.to_dict() if hasattr(uret, "to_dict") else None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "message": f"补活力异常: {e}"}



def prepare_wanzi_hang(
    session: GameAttachSession,
    cfg: HangConfig,
    *,
    log: LogFn | None = None,
) -> dict:
    """Prepare direct packet mode without touching the recovery-item slots.

    The actual sender is started by :func:`start_wanzi_packet_hang` after the
    game挂机 switch is confirmed on.  This function intentionally performs no
    inventory scan and no remote writes; it is safe to call while saving prefs
    or after a scene transition.
    """
    del session
    log = log or (lambda _m: None)
    kind = wanzi_kind_from_config(cfg)
    enabled = bool(kind)
    kind_name = "内功" if kind == WANZI_KIND_NEIGONG else "外功"
    try:
        interval = max(1, int(float(getattr(cfg, "wanzi_interval_ms", DEFAULT_WANZI_INTERVAL_MS))))
    except Exception:
        interval = int(DEFAULT_WANZI_INTERVAL_MS)
    cadence = _wanzi_cadence_from_config(cfg)
    out = {
        "ok": True,
        "enabled": enabled,
        "mode": "direct_packet" if enabled else "disabled",
        "kind": kind,
        "interval_ms": interval,
        "low_rate_seconds": float(cadence["low_rate_seconds"]),
        "low_rate_pairs_per_second": float(cadence["low_rate_pairs_per_second"]),
        "active_window_s": float(cadence["active_window_s"]),
        "slot_write": False,
        "message": (
            f"{kind_name}丸子直发已准备 · "
            f"{_wanzi_cadence_text(cadence, interval)}"
            if enabled else "丸子挂机未启用"
        ),
    }
    log(f"hang wanzi: {out['message']}")
    return out


def _wanzi_direct_enabled(cfg: HangConfig | None) -> bool:
    return bool(wanzi_kind_from_config(cfg))


def _wanzi_inventory_preflight(
    session: GameAttachSession,
    kind: str,
    *,
    log: LogFn | None = None,
) -> dict:
    """Find the selected 丸子 and freeze its P1 coordinate at enable time.

    The lookup is performed only while creating a new sender (with one retry
    for a dispatch failure). The resulting package/slot bytes are assembled
    into P1, then the packet worker sends that frozen payload without further
    inventory reads.
    """
    log = log or (lambda _m: None)
    pid = _hang_pid(session)
    if not hasattr(session, "pid"):
        message = f"丸子背包预检跳过 pid={pid}: 无附加会话"
        log(message)
        return {"ok": False, "checked": False, "skipped": True, "message": message}
    normal_kind = (
        WANZI_KIND_NEIGONG
        if str(kind or "").strip().lower() == WANZI_KIND_NEIGONG
        else WANZI_KIND_WAIGONG
    )
    damage_tid = (
        WANZI_ITEM_TID_NEIGONG
        if normal_kind == WANZI_KIND_NEIGONG
        else WANZI_ITEM_TID_WAIGONG
    )
    # The client refreshes bags through its normal packet/UI path. This lookup
    # follows the verified package-manager chain with ReadProcessMemory only;
    # it must never call GetPackage or run game code. The coordinate must come
    # from this startup, so bypass a possibly stale bag-cache value.
    from app.core.safe_dispatch import (
        OpKind,
        Priority,
        TTL_BAG_S,
        bag_cache_key,
        get_dispatch,
    )

    def _read_first_bag() -> list:
        return list_package_items_rpm(
            session, WANZI_FIRST_BAG_PACKAGE, log=lambda _m: None
        )

    items = None
    for attempt in range(1, 3):
        try:
            items = get_dispatch().read_cached(
                pid,
                bag_cache_key((WANZI_FIRST_BAG_PACKAGE,)),
                _read_first_bag,
                max_age=0,
                ttl_s=TTL_BAG_S,
                priority=Priority.P1,
                kind=OpKind.RPM,
                op="wanzi_startup_bag",
            )
            break
        except Exception as exc:
            if attempt == 1:
                log(
                    f"丸子调度中心背包内存读取失败 pid={pid}，立即重试一次: {exc}"
                )
                continue
            message = f"丸子调度中心背包内存读取失败 pid={pid}（重试后）: {exc}"
            log(message)
            return {
                "ok": False,
                "checked": False,
                "error": str(exc),
                "attempts": attempt,
                "message": message,
            }

    if items is None:
        raise RuntimeError("wanzi inventory preflight produced no items")

    damage = []
    for item in items:
        memory_package = int(getattr(item, "package", -1))
        if (
            memory_package != WANZI_FIRST_BAG_PACKAGE
            or int(getattr(item, "tid", 0) or 0) != damage_tid
            or int(getattr(item, "count", 0) or 0) <= 0
        ):
            continue
        damage.append(
            {
                "package": memory_package,
                "slot": int(item.slot),
                "count": int(item.count),
                "name": str(item.name or ""),
            }
        )
    ok = bool(damage)
    message = (
        f"丸子背包预检 pid={pid} kind={normal_kind} "
        f"package={WANZI_FIRST_BAG_PACKAGE} "
        f"attack_tid=0x{damage_tid:04X} found={damage or '无'}"
    )
    log(message)
    if damage:
        selected = damage[0]
        log(
            f"丸子发包坐标已锁定 pid={pid} P1 package={WANZI_P1_PACKAGE} "
            f"slot={selected['slot']} tid=0x{damage_tid:04X}；运行中不再读背包"
        )
    if not ok:
        log(WANZI_FIRST_BAG_MISSING_MESSAGE)
    return {
        "ok": ok,
        "checked": True,
        "kind": normal_kind,
        "damage_tid": damage_tid,
        "damage": damage,
        "message": message if ok else WANZI_FIRST_BAG_MISSING_MESSAGE,
    }


def _wanzi_p1_from_preflight(preflight: dict) -> bytes:
    """Freeze the protocol P1 coordinate from the startup bag result."""
    matches = list(preflight.get("damage") or ())
    if not matches:
        raise RuntimeError("wanzi preflight contains no selected item")
    selected = matches[0]
    return build_wanzi_p1(
        str(preflight.get("kind") or WANZI_KIND_WAIGONG),
        WANZI_P1_PACKAGE,
        int(selected["slot"]),
    )


def _log_wanzi_startup_p1(pid: int, payload: bytes, log: LogFn) -> None:
    """Emit the frozen P1 once so packet-coordinate mapping is auditable."""
    log(f"丸子P1已锁定 pid={int(pid)} hex={bytes(payload).hex().upper()}")


def _wanzi_owner_label(owners: set[str]) -> str:
    if len(owners) == 1:
        return next(iter(owners))
    return "shared" if owners else ""


def _wanzi_packet_lifecycle_lock(pid: int) -> threading.RLock:
    with _HANG_WANZI_PACKET_LOCK:
        return _HANG_WANZI_PACKET_LIFECYCLE_LOCKS.setdefault(
            int(pid), threading.RLock()
        )


def _serialize_wanzi_packet_lifecycle(fn):
    """Serialize owner acquisition/release without blocking sender gates."""
    @wraps(fn)
    def _wrapped(session_or_pid, *args, **kwargs):
        pid = _hang_pid(session_or_pid)
        with _wanzi_packet_lifecycle_lock(pid):
            return fn(session_or_pid, *args, **kwargs)

    return _wrapped


def _wanzi_transition_active_locked(pid: int) -> bool:
    """True while the hang packet-transition wanzi freeze is active (self-expiring)."""
    pid = int(pid)
    deadline = _HANG_WANZI_PACKET_TRANSITION_UNTIL.get(pid)
    if deadline is None:
        return False
    if time.monotonic() >= float(deadline):
        _HANG_WANZI_PACKET_TRANSITION_UNTIL.pop(pid, None)
        return False
    return True


def _wanzi_pause_for_transition(pid: int) -> None:
    pid = int(pid or 0)
    if not pid:
        return
    with _HANG_WANZI_PACKET_LOCK:
        _HANG_WANZI_PACKET_TRANSITION_UNTIL[pid] = (
            time.monotonic() + _WANZI_TRANSITION_PAUSE_S
        )


def _wanzi_resume_for_transition(pid: int) -> None:
    pid = int(pid or 0)
    if not pid:
        return
    with _HANG_WANZI_PACKET_LOCK:
        _HANG_WANZI_PACKET_TRANSITION_UNTIL.pop(pid, None)


def _wanzi_shared_can_send(pid: int) -> bool:
    """Read scheduler caches only; repair missing producers without doing RPM."""
    pid = int(pid)
    with _HANG_WANZI_PACKET_LOCK:
        owners = set(_HANG_WANZI_PACKET_OWNERS.get(pid) or ())
        hang_paused = pid in _HANG_WANZI_PACKET_HANG_PAUSED
        transition_paused = _wanzi_transition_active_locked(pid)
        runner = _HANG_WANZI_PACKET_RUNNERS.get(pid)
        runner_log = getattr(runner, "_log", None) if runner is not None else None
    if not owners:
        return False

    control = get_wanzi_control_state(pid)
    control_age_raw = control.get("age_s")
    control_age = (
        float(control_age_raw) if control_age_raw is not None else 1e9
    )
    control_fresh = bool(
        control
        and control_age <= max(
            WANZI_CONTROL_MAX_AGE_S,
            WANZI_CONTROL_SCAN_INTERVAL_S * 4.0,
        )
    )
    now = time.monotonic()
    should_watchdog = False
    with _HANG_WANZI_GATE_WATCHDOG_LOCK:
        last = float(_HANG_WANZI_GATE_WATCHDOG_AT.get(pid) or 0.0)
        if now - last >= HANG_WANZI_GATE_WATCHDOG_INTERVAL_S:
            _HANG_WANZI_GATE_WATCHDOG_AT[pid] = now
            should_watchdog = True
    if should_watchdog:
        _ensure_wanzi_gate_producers(
            pid,
            owners=owners,
            hang_paused=hang_paused,
            log=runner_log if callable(runner_log) else None,
        )

    # A stale/missing control producer must not silently become fail-open.  It
    # is rebuilt above and the sender resumes after the first fresh RPM sample.
    if not control_fresh or not wanzi_control_can_send(pid):
        return False
    if WANZI_PACKET_OWNER_HANG in owners:
        if hang_paused or transition_paused:
            return WANZI_PACKET_OWNER_MANUAL in owners
        return wanzi_aoi_can_send(pid)
    return WANZI_PACKET_OWNER_MANUAL in owners


def get_wanzi_packet_state(
    session_or_pid: GameAttachSession | int,
    *,
    owner: str | None = None,
) -> dict:
    """Return direct 丸子 state for a pid without probing the game."""
    pid = _hang_pid(session_or_pid)
    owner_s = str(owner or "").strip().lower()
    cleanup_control = False
    with _HANG_WANZI_PACKET_LOCK:
        runner = _HANG_WANZI_PACKET_RUNNERS.get(pid)
        owners = set(_HANG_WANZI_PACKET_OWNERS.get(pid) or ())
        hang_paused = pid in _HANG_WANZI_PACKET_HANG_PAUSED
        running = bool(runner is not None and runner.is_running())
        # A failed worker cannot retain the manual lease.  Keep a hang lease
        # so the guard can rebuild it after its normal scene fence.
        if runner is not None and not running:
            owners.discard(WANZI_PACKET_OWNER_MANUAL)
            if owners:
                _HANG_WANZI_PACKET_OWNERS[pid] = owners
            else:
                _HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
                _HANG_WANZI_PACKET_OWNERS.pop(pid, None)
                _HANG_WANZI_PACKET_HANG_PAUSED.discard(pid)
                cleanup_control = True
            runner = None
        owner_label = _wanzi_owner_label(owners)
        stats = runner.stats() if runner is not None else {}
    if cleanup_control:
        _cancel_wanzi_control_gate(pid)
    out = {
        "ok": True,
        "pid": pid,
        "running": running,
        "enabled": bool(
            running
            and (
                not owner_s
                or (
                    owner_s in owners
                    and not (
                        owner_s == WANZI_PACKET_OWNER_HANG and hang_paused
                    )
                )
            )
        ),
        "owner": owner_label,
        "owners": sorted(owners),
        "hang_paused": bool(hang_paused),
        "stats": stats,
    }
    if WANZI_PACKET_OWNER_HANG in owners:
        out["aoi"] = get_wanzi_aoi_state(pid)
    if owners or running:
        out["control"] = get_wanzi_control_state(pid)
    return out


def _wanzi_aoi_job_id(pid: int) -> str:
    return f"{HANG_WANZI_AOI_JOB_PREFIX}-{int(pid)}"


def _wanzi_control_job_id(pid: int) -> str:
    return f"{HANG_WANZI_CONTROL_JOB_PREFIX}-{int(pid)}"


def _wanzi_periodic_job_active(dispatch, pid: int, job_id: str) -> bool:
    """Return whether SafeDispatch still owns a live producer timer."""
    list_periodics = getattr(dispatch, "list_periodics", None)
    if not callable(list_periodics):
        # Older embedded/test adapters have no introspection.  Preserve their
        # legacy token behavior rather than creating duplicate timers.
        return True
    try:
        jobs = list_periodics(int(pid))
    except Exception:
        return True
    return any(
        str(job.get("job_id") or "") == str(job_id)
        and job.get("alive") is not False
        for job in (jobs or [])
        if isinstance(job, dict)
    )


def _wanzi_auto_recovery_allowed(pid: int) -> bool:
    """Automatic Space recovery belongs to the hang owner only."""
    with _HANG_WANZI_PACKET_LOCK:
        owners = set(_HANG_WANZI_PACKET_OWNERS.get(int(pid)) or ())
        paused = int(pid) in _HANG_WANZI_PACKET_HANG_PAUSED
    return bool(WANZI_PACKET_OWNER_HANG in owners and not paused)


def _cancel_wanzi_auto_recovery(pid: int) -> None:
    with _HANG_WANZI_RECOVERY_LOCK:
        current = _HANG_WANZI_RECOVERY_WORKERS.pop(int(pid), None)
    if current is not None:
        current[1].set()


def _wanzi_auto_recovery_worker(
    pid: int,
    token: object,
    stop: threading.Event,
    log: LogFn,
) -> None:
    """Pulse background Space while the scheduler cache remains controlled."""
    first_log = True
    try:
        if stop.wait(HANG_WANZI_RECOVERY_INITIAL_DELAY_S):
            return
        for _attempt in range(HANG_WANZI_RECOVERY_MAX_PULSES):
            if stop.is_set() or not _wanzi_auto_recovery_allowed(pid):
                return
            state = get_wanzi_control_state(pid)
            if not bool(state.get("active")):
                return
            try:
                from app.core.bg_input import press_bg_chord_once
                from app.core.sys_input import VK_SPACE

                # A plain forced key-state (allow_softsend=False) is enough for
                # held modifiers/reticles, but the game's recovery action needs
                # the full KEY_HOLD Hook edge: hooked key state + native
                # InjectKey + real GAKS transition.  Keep it off the control RPM
                # timer by running only in this recovery worker.
                result = press_bg_chord_once(
                    int(pid),
                    [int(VK_SPACE)],
                    hwnd=0,
                    hold_ms=80,
                    allow_softsend=True,
                    clear_all_after=False,
                    log=lambda _m: None,
                )
                if first_log:
                    gates = (
                        f"gate={int(state.get('gate0') or 0)}/"
                        f"{int(state.get('gate1') or 0)}/"
                        f"{int(state.get('gate2') or 0)}"
                    )
                    if bool(result.get("ok")):
                        log(
                            "丸子自动解控：已通过 KEY_HOLD Hook+InjectKey "
                            f"发送 Space · {gates}"
                        )
                    else:
                        log(
                            "丸子自动解控：后台 Space 发送失败 · "
                            f"{result.get('error') or 'unknown'} · {gates}"
                        )
                    first_log = False
            except Exception as exc:
                if first_log:
                    log(f"丸子自动解控：后台 Space 异常 · {exc}")
                    first_log = False
            if stop.wait(HANG_WANZI_RECOVERY_RETRY_S):
                return
    finally:
        with _HANG_WANZI_RECOVERY_LOCK:
            current = _HANG_WANZI_RECOVERY_WORKERS.get(int(pid))
            if current is not None and current[0] is token:
                _HANG_WANZI_RECOVERY_WORKERS.pop(int(pid), None)


def _schedule_wanzi_auto_recovery(
    pid: int,
    *,
    log: LogFn | None = None,
) -> bool:
    """Start one non-blocking recovery worker for the current control episode."""
    pid = int(pid)
    if not _wanzi_auto_recovery_allowed(pid):
        return False
    log = log or (lambda _m: None)
    with _HANG_WANZI_RECOVERY_LOCK:
        current = _HANG_WANZI_RECOVERY_WORKERS.get(pid)
        if current is not None and current[2].is_alive():
            return False
        token = object()
        stop = threading.Event()
        thread = threading.Thread(
            target=_wanzi_auto_recovery_worker,
            args=(pid, token, stop, log),
            daemon=True,
            name=f"wanzi-recover-{pid}",
        )
        _HANG_WANZI_RECOVERY_WORKERS[pid] = (token, stop, thread)
        thread.start()
    return True


def _ensure_wanzi_gate_producers(
    pid: int,
    *,
    owners: set[str],
    hang_paused: bool,
    log: LogFn | None = None,
) -> None:
    """Self-heal scheduler-owned gates; never probes game memory here."""
    log = log or (lambda _m: None)
    pid = int(pid)
    from app.core.safe_dispatch import get_dispatch

    dispatch = get_dispatch()
    control_job = _wanzi_control_job_id(pid)
    control = get_wanzi_control_state(pid)
    control_age_raw = control.get("age_s")
    control_age = (
        float(control_age_raw) if control_age_raw is not None else 1e9
    )
    control_stale = bool(
        not control
        or control_age
        > max(WANZI_CONTROL_MAX_AGE_S, WANZI_CONTROL_SCAN_INTERVAL_S * 4.0)
    )
    if control_stale or not _wanzi_periodic_job_active(
        dispatch, pid, control_job
    ):
        _schedule_wanzi_control_gate(pid, log=log)

    if WANZI_PACKET_OWNER_HANG not in owners or hang_paused:
        return
    aoi_job = _wanzi_aoi_job_id(pid)
    aoi = get_wanzi_aoi_state(pid)
    aoi_age_raw = aoi.get("age_s")
    aoi_age = float(aoi_age_raw) if aoi_age_raw is not None else 1e9
    aoi_stale = bool(
        not aoi
        or aoi_age
        > max(WANZI_AOI_MAX_AGE_S, WANZI_AOI_SCAN_INTERVAL_S * 4.0)
    )
    if aoi_stale or not _wanzi_periodic_job_active(dispatch, pid, aoi_job):
        _schedule_wanzi_aoi_gate(
            pid,
            radius=WANZI_AOI_DEFAULT_RADIUS,
            log=log,
        )


def _cancel_wanzi_control_gate(pid: int, *, clear: bool = True) -> None:
    """Cancel the shared control-state RPM producer."""
    _cancel_wanzi_auto_recovery(int(pid))
    with _HANG_WANZI_CONTROL_TOKEN_LOCK:
        _HANG_WANZI_CONTROL_TOKENS.pop(int(pid), None)
    try:
        from app.core.safe_dispatch import get_dispatch

        get_dispatch().cancel_periodic(int(pid), _wanzi_control_job_id(pid))
    finally:
        if clear:
            clear_wanzi_control_state(int(pid))
        with _HANG_WANZI_GATE_WATCHDOG_LOCK:
            _HANG_WANZI_GATE_WATCHDOG_AT.pop(int(pid), None)


def _schedule_wanzi_control_gate(pid: int, *, log: LogFn | None = None) -> str:
    """Start the shared 50ms pure-RPM control producer once per pid."""
    log = log or (lambda _m: None)
    pid = int(pid)
    from app.core.safe_dispatch import OpKind, Priority, get_dispatch

    dispatch = get_dispatch()
    job_id = _wanzi_control_job_id(pid)

    with _HANG_WANZI_CONTROL_TOKEN_LOCK:
        if pid in _HANG_WANZI_CONTROL_TOKENS and _wanzi_periodic_job_active(
            dispatch, pid, job_id
        ):
            return job_id
        rebuilding = pid in _HANG_WANZI_CONTROL_TOKENS
        token = object()
        _HANG_WANZI_CONTROL_TOKENS[pid] = token

    def _current() -> bool:
        with _HANG_WANZI_CONTROL_TOKEN_LOCK:
            return _HANG_WANZI_CONTROL_TOKENS.get(pid) is token

    if not get_wanzi_control_state(pid):
        set_wanzi_control_state(
            pid,
            {
                "known": False,
                "active": False,
                "blocked": False,
                "reason": "await_first_control_scan",
            },
        )
    last_can_send = True

    def _probe() -> None:
        nonlocal last_can_send
        if not _current():
            return
        try:
            state = update_wanzi_control_sample(
                pid, probe_wanzi_control_rpm(pid)
            )
        except Exception as exc:
            state = update_wanzi_control_sample(
                pid,
                {
                    "known": False,
                    "reason": f"control_probe_error:{exc}",
                },
            )
        can_send = wanzi_control_can_send(pid)
        if can_send == last_can_send:
            return
        last_can_send = can_send
        gates = (
            f"gate={int(state.get('gate0') or 0)}/"
            f"{int(state.get('gate1') or 0)}/"
            f"{int(state.get('gate2') or 0)}"
        )
        if can_send:
            log(f"丸子控制门禁：恢复放包 · {gates}")
        else:
            log(f"丸子控制门禁：停包（角色受控/击飞） · {gates}")
            _schedule_wanzi_auto_recovery(pid, log=log)

    try:
        dispatch.schedule_periodic(
            pid,
            job_id,
            WANZI_CONTROL_SCAN_INTERVAL_S,
            _probe,
            priority=Priority.P1,
            kind=OpKind.RPM,
            replace=True,
        )
    except Exception:
        with _HANG_WANZI_CONTROL_TOKEN_LOCK:
            if _HANG_WANZI_CONTROL_TOKENS.get(pid) is token:
                _HANG_WANZI_CONTROL_TOKENS.pop(pid, None)
        clear_wanzi_control_state(pid)
        raise
    log(
        f"丸子控制门禁{'已重建' if rebuilding else '已启动'} pid={pid} "
        f"（{WANZI_CONTROL_SCAN_INTERVAL_S * 1000:.0f}ms纯RPM/击飞停包/"
        f"恢复缓冲{WANZI_CONTROL_RECOVERY_S * 1000:.0f}ms）"
    )
    return job_id


def _cancel_wanzi_aoi_gate(pid: int, *, clear: bool = True) -> None:
    """Cancel the hang-only AOI producer and invalidate its fail-closed cache."""
    with _HANG_WANZI_AOI_TOKEN_LOCK:
        _HANG_WANZI_AOI_TOKENS.pop(int(pid), None)
    try:
        from app.core.safe_dispatch import get_dispatch

        get_dispatch().cancel_periodic(int(pid), _wanzi_aoi_job_id(pid))
    finally:
        if clear:
            clear_wanzi_aoi_state(int(pid))
        with _HANG_WANZI_GATE_WATCHDOG_LOCK:
            _HANG_WANZI_GATE_WATCHDOG_AT.pop(int(pid), None)


def _schedule_wanzi_aoi_gate(
    pid: int,
    *,
    radius: float,
    log: LogFn | None = None,
) -> str:
    """Schedule a pure-RPM AOI producer; the packet worker never runs it."""
    log = log or (lambda _m: None)
    pid = int(pid)
    radius_f = max(0.1, float(radius or WANZI_AOI_DEFAULT_RADIUS))
    from app.core.safe_dispatch import OpKind, Priority, get_dispatch

    token = object()
    with _HANG_WANZI_AOI_TOKEN_LOCK:
        _HANG_WANZI_AOI_TOKENS[pid] = token

    def _current() -> bool:
        with _HANG_WANZI_AOI_TOKEN_LOCK:
            return _HANG_WANZI_AOI_TOKENS.get(pid) is token

    # No first result is a soft-unknown and therefore allows sends.  Only an
    # explicit scene hard-block or a stable no-monster decision closes it.
    set_wanzi_aoi_state(
        pid,
        {
            "known": False,
            "has_monster": False,
            "reason": "await_first_scan",
            "radius": radius_f,
        },
    )
    last_decision_key = ""
    last_detail_time = 0.0

    def _publish(result: dict) -> None:
        nonlocal last_decision_key, last_detail_time
        if not _current():
            return
        state = update_wanzi_aoi_sample(pid, result, no_monster_confirm=1)
        if state.get("hard_block"):
            decision_key = "block:scene"
            decision = "停包（切图/场景不稳定）"
        elif state.get("interrupt_no_selected"):
            decision_key = "interrupt:no_selected"
            decision = f"停包（{radius_f:g}m内有怪但连续3s无选中）"
        elif state.get("known") and state.get("has_monster"):
            decision_key = "send:monster"
            decision = f"放包（{radius_f:g}m内有怪）"
        elif state.get("known"):
            decision_key = "block:no_monster"
            decision = f"停包（稳定确认{radius_f:g}m内无怪）"
        else:
            decision_key = "send:unknown"
            decision = "放包（AOI暂无有效结果）"
        changed = decision_key != last_decision_key
        if changed:
            last_decision_key = decision_key
        elif time.monotonic() - last_detail_time < 30.0:
            return
        last_detail_time = time.monotonic()
        detail = (
            f"reason={state.get('reason') or '-'} "
            f"sample_known={int(bool(state.get('sample_known')))} "
            f"sample_monster={int(bool(state.get('sample_has_monster')))} "
            f"count={int(state.get('count') or 0)} "
            f"monsters={int(state.get('monsters') or 0)} "
            f"checked={int(state.get('checked') or 0)} "
            f"streak={int(state.get('no_monster_streak') or 0)} "
            f"selected={int(state.get('selected_id64') or 0):016X} "
            f"selected_known={int(bool(state.get('selected_id64_known')))} "
            f"no_selected_age={float(state.get('no_selected_age_s') or 0.0):.2f}s "
            f"static_age={float(state.get('aoi_static_age_s') or 0.0):.2f}s "
            f"distance_static={int(bool(state.get('aoi_distance_static')))} "
            f"cached={int(bool(state.get('cached')))} "
            f"ids={",".join(f"{int(item):016X}" for item in (state.get("nearby_ids") or [])) or "-"} "
            f"pos={state.get('pos_source') or '-'}"
        )
        nearest = state.get("nearest_m")
        if nearest is not None:
            try:
                detail += f" nearest={float(nearest):.2f}m"
            except Exception:
                pass
        log(f"丸子 AOI 决策 pid={pid}: {decision} · {detail}")

    def _probe() -> None:
        if not _current():
            return
        try:
            from app.core.remote_runtime import (
                get_pid_scene_snapshot,
                is_pid_scene_snapshot_stable,
            )

            scene_snapshot = get_pid_scene_snapshot(pid)
            if scene_snapshot and not is_pid_scene_snapshot_stable(pid):
                result = {
                    "known": False,
                    "has_monster": False,
                    "hard_block": True,
                    "reason": "scene_unstable",
                    "radius": radius_f,
                }
            else:
                # Host position comes from the same pure-RPM chain as the AOI
                # manager.  It must not depend on the feature-window header
                # producer, which can pause when the window is hidden.
                result = probe_nearby_class2_rpm(pid, None, radius=radius_f)
                result["scene_stable"] = True
            _publish(result)
        except Exception as exc:
            _publish(
                {
                    "known": False,
                    "has_monster": False,
                    "reason": f"probe_error:{exc}",
                    "radius": radius_f,
                }
            )

    job_id = _wanzi_aoi_job_id(pid)
    try:
        get_dispatch().schedule_periodic(
            pid,
            job_id,
            WANZI_AOI_SCAN_INTERVAL_S,
            _probe,
            priority=Priority.P2,
            kind=OpKind.RPM,
            replace=True,
        )
    except Exception:
        with _HANG_WANZI_AOI_TOKEN_LOCK:
            if _HANG_WANZI_AOI_TOKENS.get(pid) is token:
                _HANG_WANZI_AOI_TOKENS.pop(pid, None)
        clear_wanzi_aoi_state(pid)
        raise
    log(
        f"丸子 AOI 门禁已启动 pid={pid} radius={radius_f:g}m "
        f"（{WANZI_AOI_SCAN_INTERVAL_S * 1000:.0f}ms纯RPM/稳定无怪停包/切图停包）"
    )
    return job_id


@_serialize_wanzi_packet_lifecycle
def _stop_wanzi_packet_runner(
    session_or_pid: GameAttachSession | int,
    *,
    owner: str,
    release: bool = False,
    log: LogFn | None = None,
) -> dict:
    log = log or (lambda _m: None)
    pid = _hang_pid(session_or_pid)
    owner_s = str(owner or "").strip().lower()
    with _HANG_WANZI_PACKET_LOCK:
        owners = set(_HANG_WANZI_PACKET_OWNERS.get(pid) or ())
        runner = _HANG_WANZI_PACKET_RUNNERS.get(pid)
    current_owner = _wanzi_owner_label(owners)
    if owner_s not in owners:
        return {
            "ok": True,
            "enabled": False,
            "running": bool(runner and runner.is_running()),
            "owner": current_owner,
            "owners": sorted(owners),
            "message": "丸子直发未由此功能持有",
        }
    hang_gate = owner_s == WANZI_PACKET_OWNER_HANG
    remaining = set(owners)
    if release:
        remaining.discard(owner_s)
    # A temporary hang pause must not stop a shared manual owner.  The manual
    # owner is explicitly direct, so it may keep the shared serial runner.
    shared_manual_pause = bool(
        hang_gate
        and not release
        and WANZI_PACKET_OWNER_MANUAL in remaining
    )
    if hang_gate:
        # Stop the producer first, but keep an explicit hard block until the
        # sender thread has actually exited.  This closes the cancel/join gap.
        set_wanzi_aoi_state(
            pid,
            {
                "known": False,
                "has_monster": False,
                "hard_block": True,
                "reason": "wanzi_stopping",
            },
        )
        _cancel_wanzi_aoi_gate(pid, clear=False)
        if shared_manual_pause:
            with _HANG_WANZI_PACKET_LOCK:
                _HANG_WANZI_PACKET_HANG_PAUSED.add(pid)
            clear_wanzi_aoi_state(pid)
            return {
                "ok": True,
                "enabled": False,
                "running": bool(runner and runner.is_running()),
                "owner": _wanzi_owner_label(owners),
                "owners": sorted(owners),
                "message": "丸子挂机门禁已暂停，独立释放继续运行",
            }
    if release and remaining:
        with _HANG_WANZI_PACKET_LOCK:
            _HANG_WANZI_PACKET_OWNERS[pid] = remaining
            if hang_gate:
                _HANG_WANZI_PACKET_HANG_PAUSED.discard(pid)
        if hang_gate:
            clear_wanzi_aoi_state(pid)
        return {
            "ok": True,
            "enabled": False,
            "running": bool(runner and runner.is_running()),
            "owner": _wanzi_owner_label(remaining),
            "owners": sorted(remaining),
            "message": "已释放丸子 owner，其他 owner 继续运行",
        }
    if runner is None:
        if hang_gate:
            clear_wanzi_aoi_state(pid)
        if release:
            with _HANG_WANZI_PACKET_LOCK:
                if remaining:
                    _HANG_WANZI_PACKET_OWNERS[pid] = remaining
                else:
                    _HANG_WANZI_PACKET_OWNERS.pop(pid, None)
                    _HANG_WANZI_PACKET_HANG_PAUSED.discard(pid)
        if not remaining:
            _cancel_wanzi_control_gate(pid)
        return {"ok": True, "enabled": False, "running": False,
                "owner": _wanzi_owner_label(remaining),
                "owners": sorted(remaining),
                "message": "丸子直发未运行"}
    try:
        stopped = bool(runner.stop())
    except Exception as exc:
        log(f"wanzi packet stop pid={pid}: {exc}")
        return {"ok": False, "enabled": True,
                "running": bool(runner.is_running()), "owner": current_owner,
                "owners": sorted(owners), "error": str(exc),
                "message": f"丸子直发停止失败: {exc}"}
    if stopped:
        with _HANG_WANZI_PACKET_LOCK:
            _HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
            if release:
                if remaining:
                    _HANG_WANZI_PACKET_OWNERS[pid] = remaining
                else:
                    _HANG_WANZI_PACKET_OWNERS.pop(pid, None)
                    _HANG_WANZI_PACKET_HANG_PAUSED.discard(pid)
        if hang_gate:
            clear_wanzi_aoi_state(pid)
        _cancel_wanzi_control_gate(pid)
    reported_owners = remaining if stopped else owners
    return {"ok": stopped, "enabled": not stopped, "running": not stopped,
            "owner": _wanzi_owner_label(reported_owners),
            "owners": sorted(reported_owners),
            "message": "丸子直发已停止" if stopped else "丸子直发停止超时"}


@_serialize_wanzi_packet_lifecycle
def _reserve_wanzi_packet_hang(session_or_pid: GameAttachSession | int) -> dict:
    pid = _hang_pid(session_or_pid)
    with _HANG_WANZI_PACKET_LOCK:
        owners = set(_HANG_WANZI_PACKET_OWNERS.get(pid) or ())
        owners.add(WANZI_PACKET_OWNER_HANG)
        _HANG_WANZI_PACKET_OWNERS[pid] = owners
        _HANG_WANZI_PACKET_HANG_PAUSED.discard(pid)
    return {"ok": True, "owner": _wanzi_owner_label(owners), "owners": sorted(owners),
            "message": "丸子挂机 owner 已登记"}


@_serialize_wanzi_packet_lifecycle
def start_wanzi_packet_hang(
    session: GameAttachSession,
    cfg: HangConfig,
    *,
    log: LogFn | None = None,
) -> dict:
    """Start the hang-owned direct sender; caller must have enabled config."""
    log = log or (lambda _m: None)
    pid = _hang_pid(session)
    if not _wanzi_direct_enabled(cfg):
        return {"ok": True, "enabled": False, "running": False,
                "message": "丸子挂机直发未启用"}
    try:
        interval = max(1, int(float(getattr(cfg, "wanzi_interval_ms", DEFAULT_WANZI_INTERVAL_MS))))
    except Exception:
        interval = int(DEFAULT_WANZI_INTERVAL_MS)
    cadence = _wanzi_cadence_from_config(cfg)
    # Wanzi damage uses its own fixed 18m monster gate.  Do not couple it to
    # the ordinary autoplay movement/loot radius setting.
    radius = float(WANZI_AOI_DEFAULT_RADIUS)
    kind = wanzi_kind_from_config(cfg)
    with _HANG_WANZI_PACKET_LOCK:
        owners = set(_HANG_WANZI_PACKET_OWNERS.get(pid) or ())
        current = _HANG_WANZI_PACKET_RUNNERS.get(pid)
        if current is not None and current.is_running():
            owners.add(WANZI_PACKET_OWNER_HANG)
            _HANG_WANZI_PACKET_OWNERS[pid] = owners
            _HANG_WANZI_PACKET_HANG_PAUSED.discard(pid)
            try:
                _schedule_wanzi_control_gate(pid, log=log)
                _schedule_wanzi_aoi_gate(
                    pid,
                    radius=radius,
                    log=log,
                )
            except Exception as exc:
                owners.discard(WANZI_PACKET_OWNER_HANG)
                _HANG_WANZI_PACKET_OWNERS[pid] = owners
                return {
                    "ok": False,
                    "enabled": False,
                    "running": True,
                    "owners": sorted(owners),
                    "message": f"丸子门禁启动失败: {exc}",
                }
            return {
                "ok": True,
                "enabled": True,
                "running": True,
                "reused": True,
                "owners": sorted(owners),
                "message": "丸子共享直发已运行，已登记挂机 owner",
            }
        inventory_preflight = _wanzi_inventory_preflight(session, kind, log=log)
        if not bool(inventory_preflight.get("ok")):
            return {
                "ok": False,
                "enabled": False,
                "running": False,
                "inventory_preflight": inventory_preflight,
                "message": str(
                    inventory_preflight.get("message")
                    or WANZI_FIRST_BAG_MISSING_MESSAGE
                ),
            }
        try:
            p1_payload = _wanzi_p1_from_preflight(inventory_preflight)
        except Exception as exc:
            return {
                "ok": False,
                "enabled": False,
                "running": False,
                "inventory_preflight": inventory_preflight,
                "message": f"丸子坐标组包失败: {exc}",
            }
        _log_wanzi_startup_p1(pid, p1_payload, log)
        owners.add(WANZI_PACKET_OWNER_HANG)
        _HANG_WANZI_PACKET_OWNERS[pid] = owners
        _HANG_WANZI_PACKET_HANG_PAUSED.discard(pid)
        runner = WanziPacketRunner(
            pid,
            interval,
            kind=kind,
            p1_payload=p1_payload,
            log=log,
            can_send=lambda pid=pid: _wanzi_shared_can_send(pid),
            **cadence,
        )
        _HANG_WANZI_PACKET_RUNNERS[pid] = runner
        try:
            _schedule_wanzi_control_gate(pid, log=log)
            _schedule_wanzi_aoi_gate(
                pid,
                radius=radius,
                log=log,
            )
        except Exception as exc:
            _HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
            owners.discard(WANZI_PACKET_OWNER_HANG)
            if owners:
                _HANG_WANZI_PACKET_OWNERS[pid] = owners
            else:
                _HANG_WANZI_PACKET_OWNERS.pop(pid, None)
            clear_wanzi_aoi_state(pid)
            _cancel_wanzi_control_gate(pid)
            return {"ok": False, "enabled": False, "running": False,
                    "message": f"丸子门禁启动失败: {exc}"}
        runner.start()
    kind_name = "内功" if kind == WANZI_KIND_NEIGONG else "外功"
    msg = (
        f"{kind_name}丸子挂机直发已启动 · "
        f"{_wanzi_cadence_text(cadence, interval)} · "
        f"{radius:g}m内无怪不发包"
    )
    log(msg)
    return {"ok": True, "enabled": True, "running": True, "interval_ms": interval,
            "owners": sorted(owners), "inventory_preflight": inventory_preflight,
            "message": msg}


def stop_wanzi_packet_hang(
    session_or_pid: GameAttachSession | int,
    *,
    release: bool = True,
    log: LogFn | None = None,
) -> dict:
    return _stop_wanzi_packet_runner(
        session_or_pid, owner=WANZI_PACKET_OWNER_HANG, release=release, log=log
    )


@_serialize_wanzi_packet_lifecycle
def start_wanzi_packet_manual(
    session: GameAttachSession,
    interval_ms: int,
    *,
    kind: str = WANZI_KIND_WAIGONG,
    low_rate_seconds: float = WANZI_LOW_RATE_SECONDS,
    low_rate_pairs_per_second: float = WANZI_LOW_RATE_PAIRS_PER_SECOND,
    active_window_s: float = WANZI_ACTIVE_WINDOW_S,
    log: LogFn | None = None,
) -> dict:
    """Add the manual owner to the pid's shared serialized sender."""
    log = log or (lambda _m: None)
    pid = _hang_pid(session)
    if pid <= 0:
        return {"ok": False, "enabled": False, "running": False, "message": "无有效游戏进程"}
    log(
        f"丸子手动启动请求 pid={pid} kind={kind} interval={interval_ms}ms"
    )
    # A live native hang is not a conflict: manual and hang are owner leases of
    # one serial runner, exactly like the manual/挂机 有凤 hook.  Keep an
    # unavailable live-state probe as useful diagnostics only.
    warning = ""
    try:
        live = probe_hang_state_mem(session, log=lambda _m: None)
        if not bool(getattr(live, "ok", False)) or getattr(live, "on", None) is None:
            warning = "挂机状态未确认，已按独立模式启动"
    except Exception as exc:
        warning = f"挂机状态读取失败，已按独立模式启动: {exc}"
    try:
        iv = max(1, int(float(interval_ms)))
    except Exception:
        iv = int(DEFAULT_WANZI_INTERVAL_MS)
    cadence = _wanzi_cadence_from_config(
        HangConfig(
            wanzi_low_rate_seconds=low_rate_seconds,
            wanzi_low_rate_pairs_per_second=low_rate_pairs_per_second,
            wanzi_active_window_s=active_window_s,
        )
    )
    kind = WANZI_KIND_NEIGONG if str(kind).lower() == WANZI_KIND_NEIGONG else WANZI_KIND_WAIGONG
    with _HANG_WANZI_PACKET_LOCK:
        owners = set(_HANG_WANZI_PACKET_OWNERS.get(pid) or ())
        current = _HANG_WANZI_PACKET_RUNNERS.get(pid)
        if current is not None and current.is_running():
            owners.add(WANZI_PACKET_OWNER_MANUAL)
            _HANG_WANZI_PACKET_OWNERS[pid] = owners
            try:
                _schedule_wanzi_control_gate(pid, log=log)
            except Exception as exc:
                owners.discard(WANZI_PACKET_OWNER_MANUAL)
                if owners:
                    _HANG_WANZI_PACKET_OWNERS[pid] = owners
                else:
                    _HANG_WANZI_PACKET_OWNERS.pop(pid, None)
                return {
                    "ok": False,
                    "enabled": False,
                    "running": True,
                    "owners": sorted(owners),
                    "message": f"丸子控制门禁启动失败: {exc}",
                }
            return {
                "ok": True,
                "enabled": True,
                "running": True,
                "reused": True,
                "owners": sorted(owners),
                "warning": warning,
                "message": "丸子共享直发已运行，已登记独立 owner",
            }
        inventory_preflight = _wanzi_inventory_preflight(session, kind, log=log)
        if not bool(inventory_preflight.get("ok")):
            return {
                "ok": False,
                "enabled": False,
                "running": False,
                "warning": warning,
                "inventory_preflight": inventory_preflight,
                "message": str(
                    inventory_preflight.get("message")
                    or WANZI_FIRST_BAG_MISSING_MESSAGE
                ),
            }
        try:
            p1_payload = _wanzi_p1_from_preflight(inventory_preflight)
        except Exception as exc:
            return {
                "ok": False,
                "enabled": False,
                "running": False,
                "warning": warning,
                "inventory_preflight": inventory_preflight,
                "message": f"丸子坐标组包失败: {exc}",
            }
        _log_wanzi_startup_p1(pid, p1_payload, log)
        owners.add(WANZI_PACKET_OWNER_MANUAL)
        _HANG_WANZI_PACKET_OWNERS[pid] = owners
        runner = WanziPacketRunner(
            pid,
            iv,
            kind=kind,
            p1_payload=p1_payload,
            log=log,
            can_send=lambda pid=pid: _wanzi_shared_can_send(pid),
            **cadence,
        )
        _HANG_WANZI_PACKET_RUNNERS[pid] = runner
        try:
            _schedule_wanzi_control_gate(pid, log=log)
        except Exception as exc:
            _HANG_WANZI_PACKET_RUNNERS.pop(pid, None)
            owners.discard(WANZI_PACKET_OWNER_MANUAL)
            if owners:
                _HANG_WANZI_PACKET_OWNERS[pid] = owners
            else:
                _HANG_WANZI_PACKET_OWNERS.pop(pid, None)
            return {
                "ok": False,
                "enabled": False,
                "running": False,
                "owners": sorted(owners),
                "message": f"丸子控制门禁启动失败: {exc}",
            }
        runner.start()
    kind_name = "内功" if kind == WANZI_KIND_NEIGONG else "外功"
    msg = (
        f"独立释放{kind_name}丸子已启动 · "
        f"{_wanzi_cadence_text(cadence, iv)}"
    )
    if warning:
        msg += f" · {warning}"
    log(msg)
    return {"ok": True, "enabled": True, "running": True, "interval_ms": iv,
            "owners": sorted(owners), "warning": warning,
            "inventory_preflight": inventory_preflight,
            "message": msg}


def stop_wanzi_packet_manual(
    session_or_pid: GameAttachSession | int,
    *,
    log: LogFn | None = None,
) -> dict:
    return _stop_wanzi_packet_runner(
        session_or_pid, owner=WANZI_PACKET_OWNER_MANUAL, release=True, log=log
    )


def prepare_youfeng_hang(
    session: GameAttachSession,
    cfg: HangConfig,
    *,
    log: LogFn | None = None,
) -> dict:
    """Write 有凤来仪 to the last autoplay skill slot, preserving slots 1-8."""
    log = log or (lambda _m: None)
    enabled = bool(getattr(cfg, "youfeng_hang", False))
    interval_ms = _clamp_interval_ms(
        getattr(cfg, "wanzi_interval_ms", DEFAULT_WANZI_INTERVAL_MS),
        DEFAULT_WANZI_INTERVAL_MS,
    )
    interval_s = float(interval_ms) / 1000.0
    out: dict = {
        "ok": not enabled,
        "enabled": enabled,
        "slot": int(YOUFENG_SKILL_SLOT),
        "grid": int(YOUFENG_SKILL_SLOT) + 1,
        "skill_id": int(YOUFENG_SKILL_ID),
        "interval": interval_s,
        "interval_ms": int(interval_ms),
        "before": None,
        "write": None,
        "after": None,
        "error": None,
        "message": "有凤来仪挂机未启用",
    }
    if not enabled:
        return out

    try:
        before = read_autoplay_skills(session)
        out["before"] = before
        if not before.get("ok"):
            raise RuntimeError(str(before.get("error") or "读取挂机招式失败"))
        slots = list(before.get("slots") or [])
        if len(slots) < 9:
            raise RuntimeError(f"挂机招式槽数量异常: {len(slots)}")
        autoplay = int(before.get("autoplay") or 0)
        slot_off = int(AUTOPLAY_SKILL_SLOT0_OFF) + (
            int(YOUFENG_SKILL_SLOT) * int(AUTOPLAY_SKILL_SLOT_STRIDE)
        )
        slot_addr = autoplay + slot_off
        payload = struct.pack("<If", int(YOUFENG_SKILL_ID), interval_s)
        written = remote_write_bytes(int(getattr(session, "pid", 0) or 0), slot_addr, payload)
        write = {
            "ok": int(written) == len(payload),
            "address": slot_addr,
            "bytes": int(written),
        }
        out["write"] = write
        after = read_autoplay_skills(session)
        out["after"] = after
        if not after.get("ok"):
            raise RuntimeError(str(after.get("error") or "回读挂机招式失败"))
        verify = list(after.get("slots") or [])
        if len(verify) <= YOUFENG_SKILL_SLOT:
            raise RuntimeError("回读缺少最后一个招式槽")
        slot = verify[YOUFENG_SKILL_SLOT]
        if int(slot.get("skill_id") or 0) != int(YOUFENG_SKILL_ID):
            raise RuntimeError("最后一个招式 skill_id 回读不一致")
        if abs(float(slot.get("interval") or 0.0) - interval_s) > 0.001:
            raise RuntimeError("最后一个招式频率回读不一致")
        if not write.get("ok"):
            out["write_warning"] = "底层写入状态异常，读回已生效"
    except Exception as e:
        out["error"] = str(e)
        out["message"] = f"有凤来仪准备失败: {e}"
        log(f"hang youfeng: {out['message']}")
        return out

    out["ok"] = True
    out["message"] = (
        f"华山有凤：最后一槽 · {interval_ms}ms · 已集成普通+真绝去后摇"
    )
    log(f"hang youfeng: {out['message']}")
    return out


def apply_hang_prepare(
    session: GameAttachSession,
    cfg: HangConfig,
    *,
    log: LogFn | None = None,
) -> dict:
    """Prepare hang: mode + radius + pickup policy + optional clear skills. @author by ak"""
    log = log or (lambda _m: None)
    out: dict = {
        "ok": True,
        "mode": None,
        "radius": None,
        "pickup": None,
        "skills": None,
        "wanzi": None,
        "youfeng": None,
        "errors": [],
        "messages": [],
    }
    pid = int(getattr(session, "pid", 0) or 0)
    if pid > 0:
        try:
            from app.core.remote_runtime import wait_pid_scene_stable

            wanzi_on = _wanzi_direct_enabled(cfg)
            wait_pid_scene_stable(
                pid,
                timeout_s=float(
                    HANG_SCENE_WAIT_WANZI_S if wanzi_on else HANG_SCENE_WAIT_DEFAULT_S
                ),
            )
        except Exception as e:
            out["ok"] = False
            out["errors"].append(f"scene gate: {e}")
            out["message"] = "；".join(out["errors"])
            log(f"hang prepare: ok=False {out['message']}")
            return out
    try:
        mr = set_autoplay_mode(session, int(cfg.mode), log=log)
        out["mode"] = mr
        if not mr.get("ok"):
            out["ok"] = False
            out["errors"].append(str(mr.get("error") or "set mode failed"))
        else:
            out["messages"].append(f"模式={autoplay_mode_name(cfg.mode)}")
    except Exception as e:
        out["ok"] = False
        out["errors"].append(f"mode: {e}")

    try:
        rr = set_autoplay_radius(session, int(cfg.radius), allow_below_ui_min=True, log=log)
        out["radius"] = rr
        if not rr.get("ok"):
            out["ok"] = False
            out["errors"].append(str(rr.get("error") or "set radius failed"))
        else:
            out["messages"].append(f"半径={int(cfg.radius)}")
    except Exception as e:
        out["ok"] = False
        out["errors"].append(f"radius: {e}")

    try:
        pr = apply_party_auto_need(session, bool(cfg.enable_pickup), log=log)
        out["pickup"] = pr
        if pr.get("skipped"):
            out["messages"].append(str(pr.get("message") or "拾取策略待校准"))
        elif not pr.get("ok"):
            out["errors"].append(str(pr.get("message") or "拾取策略失败"))
        else:
            out["messages"].append("拾取=自动需求" if cfg.enable_pickup else "拾取=关(自建放弃)")
    except Exception as e:
        out["errors"].append(f"pickup: {e}")

    if bool(cfg.empty_skill) and not bool(getattr(cfg, "youfeng_hang", False)):
        try:
            sk = clear_autoplay_skills(session, log=log)
            out["skills"] = sk
            if not sk.get("ok"):
                out["ok"] = False
                out["errors"].append(str(sk.get("error") or "clear skills failed"))
            else:
                out["messages"].append("已清空挂机技能")
        except Exception as e:
            out["ok"] = False
            out["errors"].append(f"skills: {e}")

    if bool(cfg.empty_skill) and bool(getattr(cfg, "youfeng_hang", False)):
        out["messages"].append("有凤来仪已启用，忽略无技能挂机")

    if bool(getattr(cfg, "youfeng_hang", False)):
        try:
            yf = prepare_youfeng_hang(session, cfg, log=log)
            out["youfeng"] = yf
            if not yf.get("ok"):
                out["ok"] = False
                out["errors"].append(
                    str(yf.get("error") or yf.get("message") or "有凤来仪准备失败")
                )
            else:
                out["messages"].append(str(yf.get("message") or "有凤来仪已准备"))
        except Exception as e:
            out["ok"] = False
            out["errors"].append(f"youfeng: {e}")

    if _wanzi_direct_enabled(cfg):
        try:
            wz = prepare_wanzi_hang(session, cfg, log=log)
            out["wanzi"] = wz
            if not wz.get("ok"):
                out["ok"] = False
                out["errors"].append(str(wz.get("error") or wz.get("message") or "丸子挂机准备失败"))
            else:
                out["messages"].append(str(wz.get("message") or "丸子挂机已准备"))
        except Exception as e:
            out["ok"] = False
            out["errors"].append(f"wanzi: {e}")

    out["message"] = "；".join(out["messages"] + out["errors"]) or "prepare done"
    log(f"hang prepare: ok={out['ok']} {out['message']}")
    return out


def get_youfeng_key_hook_state(
    session_or_pid: GameAttachSession | int,
    *,
    owner: str | None = None,
    log: LogFn | None = None,
) -> dict:
    """Return the live shared 有凤 hook state without touching hang state."""
    log = log or (lambda _m: None)
    pid = _hang_pid(session_or_pid)
    with _HANG_YOUFENG_LOCK:
        runner = _HANG_YOUFENG_RUNNERS.get(pid)
        owners = set(_HANG_YOUFENG_OWNERS.get(pid) or ())
        running = bool(runner is not None and runner.is_running())
        if runner is not None and not running:
            _HANG_YOUFENG_RUNNERS.pop(pid, None)
            _HANG_YOUFENG_OWNERS.pop(pid, None)
            owners.clear()
    owner_s = str(owner or "").strip().lower()
    return {
        "ok": True,
        "pid": pid,
        "running": running,
        "enabled": bool(running and (not owner_s or owner_s in owners)),
        "owners": sorted(owners),
    }


def stop_youfeng_key_hook(
    session_or_pid: GameAttachSession | int,
    *,
    owner: str,
    log: LogFn | None = None,
) -> dict:
    """Release one owner; stop the shared hook only after its last owner leaves."""
    log = log or (lambda _m: None)
    pid = _hang_pid(session_or_pid)
    owner_s = str(owner or "").strip().lower()
    if not owner_s:
        return {"ok": False, "running": False, "message": "有凤 hook owner 无效"}
    with _HANG_YOUFENG_LOCK:
        runner = _HANG_YOUFENG_RUNNERS.get(pid)
        owners = _HANG_YOUFENG_OWNERS.get(pid)
        if owners is not None:
            owners.discard(owner_s)
        remaining = set(owners or ())
        if runner is not None and remaining and runner.is_running():
            return {
                "ok": True,
                "enabled": False,
                "running": True,
                "retained": True,
                "owners": sorted(remaining),
                "message": "有凤去后摇仍由其他功能使用",
            }
        _HANG_YOUFENG_RUNNERS.pop(pid, None)
        _HANG_YOUFENG_OWNERS.pop(pid, None)
    if runner is None:
        bridge = None
        try:
            from app.core.xajh_bridge import ensure_bridge

            bridge = ensure_bridge(
                pid,
                log=log,
                inject_if_needed=False,
                force_reinject=False,
            )
            if bridge is not None:
                stopped = bridge.skill_action_experiment(
                    YOUFENG_SKILL_ID,
                    YOUFENG_ULTIMATE_CONFIG_ID,
                    mode=12,
                    timeout_ms=1800,
                )
                log(
                    "youfeng native fallback stop: "
                    f"ok={stopped.ok} {stopped.note or stopped.error or ''}"
                )
        except Exception as e:
            log(f"youfeng native fallback stop: {e}")
        finally:
            if bridge is not None:
                try:
                    bridge.close()
                except Exception:
                    pass
        return {
            "ok": True,
            "enabled": False,
            "running": False,
            "message": "有凤去后摇未运行",
        }
    try:
        ok = bool(runner.stop())
    except Exception as e:
        with _HANG_YOUFENG_LOCK:
            _HANG_YOUFENG_RUNNERS[pid] = runner
            _HANG_YOUFENG_OWNERS[pid] = remaining | {owner_s}
        log(f"hang youfeng stop: {e}")
        return {
            "ok": False,
            "enabled": True,
            "running": bool(runner.is_running()),
            "error": str(e),
            "message": str(e),
        }
    if not ok:
        with _HANG_YOUFENG_LOCK:
            _HANG_YOUFENG_RUNNERS[pid] = runner
            _HANG_YOUFENG_OWNERS[pid] = remaining | {owner_s}
    message = "有凤去后摇已停止" if ok else "有凤去后摇停止超时"
    log(f"hang youfeng stop: pid={pid} ok={ok}")
    return {"ok": ok, "enabled": not ok, "running": not ok, "message": message}


def start_youfeng_key_hook(
    session: GameAttachSession,
    *,
    owner: str,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> dict:
    """Acquire the shared physical-key hook for normal 8 and ultimate Alt+8."""
    log = log or (lambda _m: None)
    pid = _hang_pid(session)
    owner_s = str(owner or "").strip().lower()
    if not owner_s:
        return {"ok": False, "enabled": False, "running": False,
                "message": "有凤 hook owner 无效"}
    if pid <= 0:
        return {"ok": False, "enabled": True, "running": False, "message": "无有效游戏进程"}

    with _HANG_YOUFENG_LOCK:
        current = _HANG_YOUFENG_RUNNERS.get(pid)
        if current is not None and current.is_running():
            owners = _HANG_YOUFENG_OWNERS.setdefault(pid, set())
            owners.add(owner_s)
            return {"ok": True, "enabled": True, "running": True, "reused": True,
                    "owners": sorted(owners), "message": "有凤去后摇已运行"}
        if current is not None:
            _HANG_YOUFENG_RUNNERS.pop(pid, None)
            _HANG_YOUFENG_OWNERS.pop(pid, None)
        if current is not None:
            try:
                current.stop()
            except Exception:
                pass
        hwnd_i = int(hwnd or getattr(session, "hwnd", 0) or 0)
        bridge = None
        try:
            from app.core.xajh_bridge import ensure_bridge

            bridge = ensure_bridge(
                pid,
                log=log,
                inject_if_needed=True,
                hwnd=hwnd_i or None,
                force_reinject=False,
            )
            if bridge is None:
                raise RuntimeError("bridge not ready")
            reset = bridge.skill_action_experiment(
                YOUFENG_SKILL_ID,
                YOUFENG_ULTIMATE_CONFIG_ID,
                mode=12,
                hwnd=hwnd_i or None,
                timeout_ms=1800,
            )
            if not reset.ok:
                raise RuntimeError(
                    str(reset.error or reset.note or "旧有凤 hook 清理失败")
                )
            log(f"youfeng native preclean: {reset.note or 'ok'}")
        except Exception as e:
            return {
                "ok": False,
                "enabled": False,
                "running": False,
                "error": str(e),
                "message": f"有凤去后摇桥接失败: {e}",
            }
        finally:
            if bridge is not None:
                try:
                    bridge.close()
                except Exception:
                    pass

        runner = YoufengChainRunner(
            pid,
            hwnd_i,
            interrupt_mode=INTERRUPT_QINGGONG_PULSE,
            drive_casts=False,
            observe_ultimate=True,
            pulse_stop_ms=0,
            suppress_lift=False,
            log=log,
            on_status=log,
        )
        if not runner.start():
            return {
                "ok": False,
                "enabled": False,
                "running": False,
                "message": "有凤去后摇启动失败",
            }
        if not runner.wait_ready(3.0):
            error = str(runner.stats().get("error") or "observer 未就绪")
            runner.stop()
            return {
                "ok": False,
                "enabled": False,
                "running": False,
                "error": error,
                "message": f"有凤去后摇启动失败: {error}",
            }
        _HANG_YOUFENG_RUNNERS[pid] = runner
        _HANG_YOUFENG_OWNERS[pid] = {owner_s}
    log(f"youfeng key hook start: pid={pid} owner={owner_s} keys=8/Alt+8")
    return {
        "ok": True,
        "enabled": True,
        "running": True,
        "owners": [owner_s],
        "message": "有凤来仪已开启（8 普通 / Alt+8 真绝）",
    }


def stop_youfeng_hang(
    session_or_pid: GameAttachSession | int,
    *,
    log: LogFn | None = None,
) -> dict:
    """Release the hang owner's use of the shared 有凤 hook."""
    return stop_youfeng_key_hook(
        session_or_pid, owner=YOUFENG_HOOK_OWNER_HANG, log=log
    )


def start_youfeng_hang(
    session: GameAttachSession,
    cfg: HangConfig,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> dict:
    """Acquire the shared 有凤 hook when this hang profile enables it."""
    if not bool(getattr(cfg, "youfeng_hang", False)):
        stopped = stop_youfeng_hang(session, log=log)
        return {
            "ok": bool(stopped.get("ok")),
            "enabled": False,
            "running": bool(stopped.get("running")),
            "message": "有凤来仪挂机未启用",
        }
    result = start_youfeng_key_hook(
        session,
        owner=YOUFENG_HOOK_OWNER_HANG,
        hwnd=hwnd,
        log=log,
    )
    if result.get("ok"):
        result["message"] = "有凤去后摇已启动（内挂普通+真绝 · 0ms轻功脉冲）"
    return result


def resolve_empty_skill_for_action(
    session: GameAttachSession,
    cfg: HangConfig | None = None,
    *,
    log: LogFn | None = None,
) -> bool:
    """Whether stop/start should use force empty-skill path.

    True when:
      - cfg.empty_skill is set, or
      - live autoplay skills are empty + gate closed (inferred force hang)

    Avoids Alt+R failing after a force open.

    @author by ak
    """
    log = log or (lambda _m: None)
    if cfg is not None and bool(getattr(cfg, "youfeng_hang", False)):
        return False
    if cfg is not None and bool(cfg.empty_skill):
        return True
    try:
        skills = read_autoplay_skills(session)
        inferred = _infer_empty_skill(skills)
        if inferred is True:
            log("hang: empty_skill inferred from live skills → force path")
            return True
    except Exception as e:
        log(f"hang: empty_skill infer err: {e}")
    return False


HANG_START_PACKET = bytes.fromhex("1500")
HANG_STOP_PACKET = bytes.fromhex("160002")


def _send_hang_control_packet(
    session: GameAttachSession,
    payload: bytes,
    *,
    action: str,
    log: LogFn | None = None,
) -> dict:
    """Send the server挂机 control packet through the 丸子 raw sender."""
    log = log or (lambda _m: None)
    payload = bytes(payload)
    try:
        ret = int(send_raw_c2s_packet(session, payload, log=log))
        ok = ret == 1
        message = f"{action}挂机封包已发送" if ok else f"{action}挂机封包被拒绝(ret={ret})"
        log(f"hang packet: action={action} hex={payload.hex().upper()} ret={ret}")
        return {
            "ok": ok,
            "via": "raw_c2s_packet",
            "packet": payload.hex().upper(),
            "ret": ret,
            "message": message,
        }
    except Exception as exc:
        message = f"{action}挂机封包发送失败: {exc}"
        log(message)
        return {
            "ok": False,
            "via": "raw_c2s_packet",
            "packet": payload.hex().upper(),
            "error": str(exc),
            "message": message,
        }


def _hang_switch_source(source: str | None = None) -> str:
    """Return an auditable caller label for start/stop control logs."""
    if str(source or "").strip():
        return str(source).strip()
    try:
        frame = inspect.currentframe()
        caller = frame.f_back.f_back if frame is not None and frame.f_back else None
        if caller is not None:
            return (
                f"{Path(caller.f_code.co_filename).name}:"
                f"{caller.f_code.co_name}:{caller.f_lineno}"
            )
    except Exception:
        pass
    return "unknown"


def send_hang_start_packet(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
) -> dict:
    """Send the server挂机 start packet without running local preparation."""
    return _send_hang_control_packet(
        session, HANG_START_PACKET, action="开启", log=log
    )


def send_hang_stop_packet(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
) -> dict:
    """Send the server挂机 stop packet without running local cleanup."""
    return _send_hang_control_packet(
        session, HANG_STOP_PACKET, action="关闭", log=log
    )


def _seed_dungeon_follow_target(
    session: GameAttachSession,
    cfg: HangConfig,
    *,
    hwnd: int = 0,
    settle_s: float = 1.5,
    log: LogFn | None = None,
) -> dict:
    """Initialize the native dungeon follow target after packet start."""
    log = log or (lambda _m: None)
    if int(getattr(cfg, "mode", 0) or 0) != 1:
        return {"ok": True, "skipped": True, "reason": "not_dungeon"}
    out: dict = {
        "ok": True,
        "follow_target_id": None,
        "follow_target_name": None,
        "team_role": None,
    }
    try:
        from app.core.plg_ui import host_team_role
        from app.core.team_ops import read_cecteam_members

        role = host_team_role(session, log=lambda _m: None) or {}
        out["team_role"] = str(role.get("role") or "unknown")
        members = read_cecteam_members(session, log=lambda _m: None)
        is_leader = role.get("role") == "leader" or role.get("is_leader") is True
        if not is_leader:
            return out
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
            out["ok"] = False
            out["error"] = "副本模式未找到可跟随队员"
            log(out["error"])
            return out
        target_id = int(target.get("obj_id") or 0)
        out["follow_target_id"] = target_id
        out["follow_target_name"] = str(target.get("name") or "")

        from app.core.xajh_bridge import ensure_bridge

        bridge = ensure_bridge(
            int(getattr(session, "pid", 0) or 0),
            log=log,
            inject_if_needed=True,
            hwnd=int(hwnd or getattr(session, "hwnd", 0) or 0) or None,
        )
        if bridge is None:
            out["ok"] = False
            out["error"] = "副本跟随桥接未就绪"
            return out
        try:
            seeded = None
            deadline = time.monotonic() + max(0.5, min(3.0, float(settle_s)))
            while time.monotonic() < deadline:
                seeded = bridge.autoplay_seed_follow(
                    target_id,
                    hwnd=int(hwnd or getattr(session, "hwnd", 0) or 0) or None,
                    timeout_ms=4000,
                )
                if seeded.ok:
                    break
                error = str(seeded.error or seeded.note or "")
                if "snapshot unavailable" not in error.lower():
                    break
                time.sleep(0.15)
            out["follow_seed"] = seeded.to_dict() if seeded is not None else {"ok": False}
            if seeded is None or not seeded.ok:
                out["ok"] = False
                out["error"] = str(
                    getattr(seeded, "error", None)
                    or getattr(seeded, "note", None)
                    or "副本模式跟随目标初始化失败"
                )
        finally:
            bridge.close()
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"副本模式跟随目标初始化失败: {exc}"
        log(out["error"])
    return out


def stop_hang(
    session: GameAttachSession,
    cfg: HangConfig | None = None,
    *,
    hwnd: int = 0,
    settle_s: float = 0.55,
    source: str | None = None,
    log: LogFn | None = None,
) -> dict:
    """Stop挂机 by sending the server control packet directly."""
    log = log or (lambda _m: None)
    cfg = cfg or HangConfig()
    log(f"hang stop request source={_hang_switch_source(source)}")

    # Quiesce the direct sender before toggling the game's挂机 switch.  Keep
    # the hang owner reserved until the switch is confirmed off, so the manual
    # button cannot start a competing sender during this transition.
    wanzi_pause = stop_wanzi_packet_hang(session, release=False, log=log)
    if not wanzi_pause.get("ok"):
        return {
            "ok": False,
            "message": str(wanzi_pause.get("message") or "丸子直发停止失败"),
            "wanzi": wanzi_pause,
        }

    def _finish(ret: dict) -> dict:
        try:
            from app.core.dungeon_fight_kick import reset_state

            reset_state(int(getattr(session, "pid", 0) or 0))
        except Exception:
            pass
        # Alert 粘滞机器对停止封包无响应（2026-08-30 重现：关闭封包已发送
        # 但机器仍 Attack/Alert）。复刻手动恢复法：重发开启封包同步会话
        # 后再关，最多两轮。
        try:
            from app.core.activity_auto import resolve_cec_autoplay_rpm

            for round_i in range(2):
                time.sleep(0.6)
                if resolve_cec_autoplay_rpm(session).get("running") is not True:
                    break
                log(
                    f"hang stop: 关闭后仍在运行（第{round_i + 1}轮），"
                    "重发开启→关闭恢复序列"
                )
                _send_hang_control_packet(
                    session, HANG_START_PACKET, action="开启(恢复)", log=log
                )
                time.sleep(0.6)
                _send_hang_control_packet(
                    session, HANG_STOP_PACKET, action="关闭(恢复)", log=log
                )
        except Exception as e:
            log(f"hang stop: 恢复序列 err {e}")
        try:
            from app.core.wuzun_open_monster import stop_wuzun_open_monster

            pid = int(getattr(session, "pid", 0) or 0)
            if pid:
                ret["auto_open_monster"] = stop_wuzun_open_monster(pid, log=log)
        except Exception as exc:
            log(f"hang stop: 自动开怪 runner 停止失败: {exc}")
        if ret.get("ok") and not ret.get("skipped"):
            ret["youfeng"] = stop_youfeng_hang(session, log=log)
            ret["wanzi"] = stop_wanzi_packet_hang(session, release=True, log=log)
        else:
            ret["wanzi"] = wanzi_pause
        return ret

    return _finish(
        _stop_hang_unlocked(session, cfg, hwnd=hwnd, settle_s=settle_s, log=log)
    )


def apply_hang_switch(
    session: GameAttachSession,
    cfg: HangConfig,
    desired_on: bool,
    *,
    hwnd: int = 0,
    temporary_mode: int | None = None,
    source: str | None = None,
    log: LogFn | None = None,
) -> dict:
    """唯一开/关挂机管线：挂机设置页、队内控/群控/云控同步、日常 routine、
    登录编排都必须走这里，禁止各自再组 prepare+start/stop 逻辑。

    temporary_mode 仅本次调用生效（内存 replace），绝不写回配置或磁盘。
    已处于目标状态时走快速路径，但必须对账丸子门控（on→幂等重启/挂靠，
    off→确保停止）：否则主控 on/off 抖动后丸子门控会长期缺席。
    @author by ak
    """
    log = log or (lambda _m: None)
    mode_i = int(temporary_mode) if temporary_mode in (0, 1) else None
    if mode_i is not None:
        cfg = replace(cfg, mode=mode_i)
    want_s = "开" if desired_on else "关"
    mode_s = ""
    if desired_on and mode_i in (0, 1):
        mode_s = "（" + ("副本模式" if mode_i == 1 else "普通模式") + "）"
    out: dict = {
        "ok": False,
        "skipped": False,
        "desired_on": bool(desired_on),
        "temporary_mode": mode_i,
        "cfg": cfg,
        "message": "",
    }

    if desired_on:
        prepare = apply_hang_prepare(session, cfg, log=log)
        out["prepare"] = prepare
        if not prepare.get("ok"):
            msg = str(prepare.get("message") or "挂机参数设置失败")
            out["message"] = f"挂机参数设置失败 · {msg}"
            log(f"hang switch: {out['message']}")
            return out

    # Probe fast path: already in the desired state (+ mode match).
    try:
        st = probe_hang_state_mem(session, log=log)
    except Exception as exc:
        st = None
        log(f"hang switch: probe err {exc}")
    if st is not None and st.ok and st.on is not None and bool(st.on) == desired_on:
        live_mode = None
        try:
            live_mode = int(((st.detail or {}).get("mem") or {}).get("mode"))
        except (TypeError, ValueError):
            live_mode = None
        mode_matches = mode_i is None or live_mode is None or live_mode == mode_i
        if mode_matches:
            if desired_on:
                wz = start_wanzi_packet_hang(session, cfg, log=log)
                out["wanzi"] = wz
                if not wz.get("ok"):
                    out["message"] = "已开启，但丸子门控对账失败: " + str(
                        wz.get("message") or "unknown"
                    )
                    log(f"hang switch: {out['message']}")
                    return out
                detail = str(wz.get("message") or "").strip()
                out["ok"] = True
                out["skipped"] = True
                out["message"] = f"已是开启状态{mode_s}" + (f" · {detail}" if detail else "")
            else:
                out["wanzi"] = stop_wanzi_packet_hang(session, release=True, log=log)
                out["ok"] = True
                out["skipped"] = True
                out["message"] = "已是关闭状态"
            log(f"hang switch: want={want_s} skipped · {out['message']}")
            return out

    ret = (
        start_hang(session, cfg, hwnd=hwnd, source=source, log=log)
        if desired_on
        else stop_hang(session, cfg, hwnd=hwnd, source=source, log=log)
    )
    out["switch"] = ret
    out["ok"] = bool(ret.get("ok"))
    out["message"] = str(ret.get("message") or "")
    try:
        live = read_hang_live(session, log=log)
        out["live"] = live.to_dict()
        out["live_line"] = format_hang_live_line(live)
    except Exception as exc:
        log(f"hang switch: live probe err {exc}")
    log(
        f"hang switch: want={want_s} ok={out['ok']} skipped={out['skipped']} "
        f"temporary_mode={mode_i} {out['message']}"
    )
    return out


def _stop_hang_force_unlocked(
    session: GameAttachSession,
    cfg: HangConfig,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> dict:
    """Compatibility wrapper that closes挂机 through the stop packet."""
    wanzi_on = _wanzi_direct_enabled(cfg)
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "via": "raw_c2s_packet",
        "message": "",
        "detail": None,
    }
    ok_scene, scene_err = _wait_hang_scene_stable(
        session, wanzi=wanzi_on, log=log
    )
    if not ok_scene:
        out["message"] = f"关挂前场景未稳定: {scene_err}"
        log(f"hang stop: {out['message']}")
        return out
    try:
        ret = _send_hang_control_packet(
            session, HANG_STOP_PACKET, action="关闭", log=log
        )
        out["detail"] = ret
        out["ok"] = bool(ret.get("ok"))
        out["message"] = (
            "封包关挂机成功"
            if out["ok"]
            else str(ret.get("error") or "封包关挂机失败")
        )
    except Exception as e:
        out["message"] = f"封包关挂机异常: {e}"
        log(out["message"])
    log(f"hang stop: ok={out['ok']} via={out['via']} {out['message']}")
    if out.get("ok"):
        out["target_guard"] = _disarm_dungeon_target_guard(
            session, hwnd=hwnd, log=log
        )
        try:
            from app.core.xajh_bridge import ensure_bridge

            pid = int(getattr(session, "pid", 0) or 0)
            hwnd_i = int(hwnd or getattr(session, "hwnd", 0) or 0)
            if pid > 0:
                bridge = ensure_bridge(
                    pid,
                    log=log,
                    inject_if_needed=True,
                    hwnd=hwnd_i or None,
                    force_reinject=False,
                )
                if bridge is not None:
                    bridge.cg_skip(mode=0, timeout_ms=1200)
        except Exception as e:
            log(f"hang stop: cg hook disarm err {e}")
        try:
            stop_hang_guard(session, log=log)
        except Exception as e:
            log(f"hang stop: guard cancel err: {e}")
    return out


def _stop_hang_unlocked(
    session: GameAttachSession,
    cfg: HangConfig,
    *,
    hwnd: int = 0,
    settle_s: float = 0.55,
    log: LogFn | None = None,
) -> dict:
    """Stop挂机 through the server control packet."""
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "via": "raw_c2s_packet",
        "message": "",
        "detail": None,
    }
    wanzi_on = _wanzi_direct_enabled(cfg)
    ok_scene, scene_err = _wait_hang_scene_stable(
        session, wanzi=wanzi_on, log=log
    )
    if not ok_scene:
        out["message"] = f"关挂前场景未稳定: {scene_err}"
        log(f"hang stop: {out['message']}")
        return out
    try:
        result = _send_hang_control_packet(
            session, HANG_STOP_PACKET, action="关闭", log=log
        )
        out["ok"] = bool(result.get("ok"))
        out["message"] = str(result.get("message") or "")
        out["detail"] = result
    except Exception as e:
        out["ok"] = False
        out["message"] = f"关挂异常: {e}"
        log(out["message"])
    log(f"hang stop: ok={out['ok']} via={out['via']} {out['message']}")
    # A failed stop must leave the guardian alive.  Otherwise the character can
    # still be hanging while repair/roll maintenance has silently been disabled.
    if out.get("ok"):
        out["target_guard"] = _disarm_dungeon_target_guard(
            session, hwnd=hwnd, log=log
        )
        try:
            from app.core.xajh_bridge import ensure_bridge

            pid = int(getattr(session, "pid", 0) or 0)
            hwnd_i = int(hwnd or getattr(session, "hwnd", 0) or 0)
            if pid > 0:
                bridge = ensure_bridge(
                    pid,
                    log=log,
                    inject_if_needed=True,
                    hwnd=hwnd_i or None,
                    force_reinject=False,
                )
                if bridge is not None:
                    bridge.cg_skip(mode=0, timeout_ms=1200)
        except Exception as e:
            log(f"hang stop: cg hook disarm err {e}")
        try:
            stop_hang_guard(session, log=log)
        except Exception as e:
            log(f"hang stop: guard cancel err: {e}")
    return out


def hang_start_warnings(cfg: HangConfig) -> list[str]:
    """User-facing tips for start path limitations. @author by ak"""
    tips: list[str] = []
    if bool(cfg.auto_vitality) and not VITALITY_RE_READY:
        tips.append("活力未校准，暂不自动补")
    kind = wanzi_kind_from_config(cfg)
    if kind:
        iv = _clamp_interval_ms(
            getattr(cfg, "wanzi_interval_ms", DEFAULT_WANZI_INTERVAL_MS),
            DEFAULT_WANZI_INTERVAL_MS,
        )
        cadence = _wanzi_cadence_from_config(cfg)
        tips.append(
            f"{'内功' if kind == WANZI_KIND_NEIGONG else '外功'}丸子："
            f"{_wanzi_cadence_text(cadence, iv)} · "
            f"{WANZI_AOI_DEFAULT_RADIUS:g}m内有怪且角色可释放时生效"
        )
    if bool(getattr(cfg, "youfeng_hang", False)):
        iv = _clamp_interval_ms(
            getattr(cfg, "wanzi_interval_ms", DEFAULT_WANZI_INTERVAL_MS),
            DEFAULT_WANZI_INTERVAL_MS,
        )
        tips.append(f"华山有凤：最后一槽 · {iv}ms · 已集成普通+真绝去后摇")
    if bool(getattr(cfg, "jianglong_hang", False)):
        tips.append(
            "武尊堂自动降龙：5人轮流控场，主控调度；不适用于加吸星大法配合"
        )
    if bool(getattr(cfg, "skip_dungeon_story", False)):
        tips.append("副本跳过剧情：开挂成功后异步装钩子，不堵内挂")
    return tips


def _start_uses_force_function(
    session: GameAttachSession,
    cfg: HangConfig,
    *,
    log: LogFn | None = None,
) -> bool:
    """Whether this role must use the direct AutoPlay function path."""
    log = log or (lambda _m: None)
    if bool(cfg.empty_skill):
        return True
    try:
        skills = read_autoplay_skills(session)
        if skills.get("ok") and int(skills.get("gate_b") or 0) == 0:
            log("hang: 使用招式为空(gate_b=0) -> force function path")
            return True
    except Exception as e:
        log(f"hang: 使用招式检查失败，保持原启动策略: {e}")
    try:
        dungeon = int(cfg.mode) == int(AUTOPLAY_MODE_DUNGEON)
    except Exception:
        dungeon = int(DEFAULT_HANG_MODE) == int(AUTOPLAY_MODE_DUNGEON)
    if not dungeon:
        return False
    try:
        from app.core.plg_ui import host_team_role

        role = host_team_role(session, log=lambda _m: None) or {}
        return bool(
            role.get("role") == "leader" or role.get("is_leader") is True
        )
    except Exception as e:
        log(f"hang: dungeon leader probe failed: {e}")
        return False


def _wait_hang_scene_stable(
    session: GameAttachSession,
    *,
    wanzi: bool = False,
    log: LogFn | None = None,
) -> tuple[bool, str]:
    """Wait for scene fence before start/stop/prepare heavy hang ops."""
    log = log or (lambda _m: None)
    pid = int(getattr(session, "pid", 0) or 0)
    if pid <= 0:
        return True, ""
    timeout_s = float(
        HANG_SCENE_WAIT_WANZI_S if wanzi else HANG_SCENE_WAIT_DEFAULT_S
    )
    try:
        from app.core.remote_runtime import wait_pid_scene_stable

        wait_pid_scene_stable(pid, timeout_s=timeout_s)
        return True, ""
    except Exception as e:
        msg = f"scene gate: {e}"
        log(f"hang scene wait: {msg}")
        return False, msg


def _resolve_hang_char_id(session) -> str:
    """Resolve the host role numeric id ("" when unavailable). @author by ak"""
    try:
        from app.core.team_ops import read_host_identity

        _name, oid = read_host_identity(session, need_name=False, log=lambda _m: None)
        return normalize_hang_char_id(int(oid))
    except Exception:
        return ""


def _arm_dungeon_target_guard(
    session: GameAttachSession,
    cfg: HangConfig,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> dict:
    """Arm the one-shot native dungeon candidate guard before autoplay starts."""
    log = log or (lambda _m: None)
    if int(getattr(cfg, "mode", -1)) != int(AUTOPLAY_MODE_DUNGEON):
        return {"ok": True, "enabled": False, "reason": "not dungeon mode"}
    pid = int(getattr(session, "pid", 0) or 0)
    if pid <= 0:
        return {"ok": False, "enabled": False, "error": "no pid"}
    # The renamed checkbox is deliberately subordinate to dungeon mode.
    if not bool(getattr(cfg, "ignore_dungeon_stuck", False)):
        log("dungeon target guard: disabled (忽略副本卡怪未勾选)")
        return {"ok": True, "enabled": False, "reason": "ignore dungeon stuck off"}
    try:
        from app.core.dungeon_target_policy import configured_dungeon_tids

        # Read the packaged baseline plus the assistant-owned per-role cache
        # before opening/injecting a bridge. An empty effective list must have
        # no game-process side effect.
        rules = configured_dungeon_tids(_resolve_hang_char_id(session))
        if not rules:
            log("dungeon target guard: disabled (内置及角色忽略名单均为空)")
            return {"ok": True, "enabled": False, "reason": "no dungeon target TIDs"}

        from app.core.xajh_bridge import ensure_bridge

        bridge = ensure_bridge(
            pid,
            log=log,
            inject_if_needed=True,
            hwnd=int(hwnd or getattr(session, "hwnd", 0) or 0) or None,
            force_reinject=False,
        )
        if bridge is None:
            return {
                "ok": False,
                "enabled": False,
                "error": "bridge unavailable or stale; restart game required",
            }
        try:
            configured = bridge.dungeon_target_rules(mode=1, timeout_ms=1200)
            if not configured.ok:
                return {"ok": False, "enabled": False, "error": str(configured.error or configured.note)}
            for tid in rules:
                configured = bridge.dungeon_target_rules(mode=2, tid=tid, timeout_ms=1200)
                if not configured.ok:
                    return {"ok": False, "enabled": False, "error": str(configured.error or configured.note)}
            log(f"dungeon target guard: synced TIDs={','.join(f'0x{x:X}' for x in rules)}")
            result = bridge.target_submit_trace(mode=3, timeout_ms=1500)
        finally:
            bridge.close()
        if not result.ok:
            return {
                "ok": False,
                "enabled": False,
                "error": str(result.error or result.note or "target guard arm failed"),
            }
        with _DUNGEON_TARGET_GUARD_LOCK:
            _DUNGEON_TARGET_GUARD_ARMED.add(pid)
            _DUNGEON_TARGET_GUARD_TIDS[pid] = tuple(int(t) & 0xFFFFFFFF for t in rules)
        log("dungeon target guard: armed (packaged/per-role TID, AOI distance, 30m fence)")
        return {"ok": True, "enabled": True, "note": result.note}
    except Exception as exc:
        log(f"dungeon target guard: arm err {exc}")
        return {"ok": False, "enabled": False, "error": str(exc)}


def _disarm_dungeon_target_guard(
    session_or_pid,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> dict:
    """Disable the native dungeon candidate guard once per hang lifecycle."""
    log = log or (lambda _m: None)
    pid = int(getattr(session_or_pid, "pid", session_or_pid) or 0)
    if pid <= 0:
        return {"ok": True, "enabled": False, "reason": "no pid"}
    with _DUNGEON_TARGET_GUARD_LOCK:
        armed = pid in _DUNGEON_TARGET_GUARD_ARMED
    if not armed:
        return {"ok": True, "enabled": False, "reason": "not armed"}
    try:
        from app.core.xajh_bridge import ensure_bridge

        bridge = ensure_bridge(
            pid,
            log=log,
            inject_if_needed=False,
            hwnd=int(hwnd or getattr(session_or_pid, "hwnd", 0) or 0) or None,
            force_reinject=False,
        )
        if bridge is None:
            return {"ok": False, "enabled": False, "error": "bridge unavailable"}
        try:
            result = bridge.target_submit_trace(mode=4, timeout_ms=1500)
        finally:
            bridge.close()
        if not result.ok:
            return {
                "ok": False,
                "enabled": False,
                "error": str(result.error or result.note or "target guard disarm failed"),
            }
        with _DUNGEON_TARGET_GUARD_LOCK:
            _DUNGEON_TARGET_GUARD_ARMED.discard(pid)
            _DUNGEON_TARGET_GUARD_TIDS.pop(pid, None)
        log("dungeon target guard: disarmed")
        return {"ok": True, "enabled": False, "note": result.note}
    except Exception as exc:
        log(f"dungeon target guard: disarm err {exc}")
        return {"ok": False, "enabled": False, "error": str(exc)}


def start_hang(
    session: GameAttachSession,
    cfg: HangConfig | None = None,
    *,
    hwnd: int = 0,
    settle_s: float = 0.7,
    maintain: bool = True,
    temporary: bool = False,
    source: str | None = None,
    log: LogFn | None = None,
) -> dict:
    """Start挂机 through the direct server control packet."""
    log = log or (lambda _m: None)
    cfg = cfg or HangConfig()
    dungeon_mode = int(getattr(cfg, "mode", -1)) == int(AUTOPLAY_MODE_DUNGEON)
    if temporary:
        try:
            stop_hang_guard(session, log=log, disarm_target_guard=False)
        except Exception as exc:
            log(f"hang start temporary guard cancel skipped: {exc}")
    log(
        f"hang start request source={_hang_switch_source(source)} "
        f"mode={int(cfg.mode)} temporary={int(bool(temporary))}"
    )
    # Register the hang owner for the whole transition.  A manual owner may
    # coexist and continues to reference the same serialized sender.
    reserve = _reserve_wanzi_packet_hang(session)
    if not reserve.get("ok"):
        return {"ok": False, "message": str(reserve.get("message") or "丸子挂机 owner 登记失败"),
                "wanzi": reserve}
    wanzi_reserved = True
    prestarted_youfeng = None
    if bool(getattr(cfg, "youfeng_hang", False)):
        prestarted_youfeng = start_youfeng_hang(
            session, cfg, hwnd=hwnd, log=log
        )
        if not prestarted_youfeng.get("ok"):
            if wanzi_reserved:
                stop_wanzi_packet_hang(session, release=True, log=log)
            return {
                "ok": False,
                "message": str(
                    prestarted_youfeng.get("message")
                    or "有凤去后摇启动失败"
                ),
                "youfeng": prestarted_youfeng,
            }
    force_path = False

    def _with_tips(ret: dict) -> dict:
        if ret.get("ok") and not ret.get("skipped") and int(getattr(cfg, "mode", 0) or 0) == 1:
            try:
                ret["follow"] = _seed_dungeon_follow_target(
                    session, cfg, hwnd=hwnd, settle_s=settle_s, log=log
                )
                if not bool(ret["follow"].get("ok")):
                    ret["ok"] = False
                    ret["message"] = str(
                        ret["follow"].get("error") or "副本跟随目标初始化失败"
                    )
            except Exception as exc:
                ret["ok"] = False
                ret["message"] = f"副本跟随目标初始化异常: {exc}"
                log(ret["message"])
        if ret.get("ok") and not ret.get("skipped"):
            yf = prestarted_youfeng or start_youfeng_hang(
                session, cfg, hwnd=hwnd, log=log
            )
            ret["youfeng"] = yf
            if bool(getattr(cfg, "youfeng_hang", False)) and not yf.get("ok"):
                ret["ok"] = False
                base = str(ret.get("message") or "").strip()
                detail = str(yf.get("message") or "有凤去后摇启动失败")
                ret["message"] = f"{base} · {detail}" if base else detail
            if _wanzi_direct_enabled(cfg):
                wz = start_wanzi_packet_hang(session, cfg, log=log)
                ret["wanzi"] = wz
                if not wz.get("ok"):
                    ret["ok"] = False
                    base = str(ret.get("message") or "").strip()
                    detail = str(wz.get("message") or "丸子直发启动失败")
                    ret["message"] = f"{base} · {detail}" if base else detail
        elif not ret.get("ok"):
            try:
                _disarm_dungeon_target_guard(session, hwnd=hwnd, log=log)
            except Exception as e:
                log(f"hang start: target guard cleanup err {e}")
            stop_youfeng_hang(session, log=log)
            if wanzi_reserved:
                stop_wanzi_packet_hang(session, release=True, log=log)
        # 武尊自动开怪 不再在 _with_tips 中立即调用，
        # 统一由 hang guard 在场景稳定后按需触发（首次+切换）。
        tips = hang_start_warnings(cfg)
        if tips:
            ret["warnings"] = tips
            extra = "；".join(tips)
            base = str(ret.get("message") or "").strip()
            ret["message"] = (base + " · " + extra).strip(" ·") if base else extra
            for tip in tips:
                log(f"hang start tip: {tip}")
        return ret

    try:
        from app.core.dungeon_fight_kick import reset_state

        reset_state(int(getattr(session, "pid", 0) or 0))
    except Exception:
        pass
    return _with_tips(
        _start_hang_unlocked(
            session,
            cfg,
            hwnd=hwnd,
            settle_s=settle_s,
            maintain=maintain,
            temporary=temporary,
            force_path=False,
            log=log,
        )
    )


def _start_hang_unlocked(
    session: GameAttachSession,
    cfg: HangConfig,
    *,
    hwnd: int = 0,
    settle_s: float = 0.7,
    maintain: bool = True,
    temporary: bool = False,
    force_path: bool | None = None,
    log: LogFn | None = None,
) -> dict:
    """Start挂机 through the server control packet."""
    log = log or (lambda _m: None)
    dungeon_mode = int(getattr(cfg, "mode", -1)) == int(AUTOPLAY_MODE_DUNGEON)
    force_path = False
    out: dict = {
        "ok": False,
        "via": "raw_c2s_packet",
        "prepare": None,
        "start": None,
        "stop_before": None,
        "maintain": None,
        "message": "",
        "live": None,
    }
    hwnd_i = int(hwnd or getattr(session, "hwnd", 0) or 0)

    wanzi_on = _wanzi_direct_enabled(cfg)
    ok_scene, scene_err = _wait_hang_scene_stable(
        session, wanzi=wanzi_on or bool(force_path), log=log
    )
    if not ok_scene:
        out["message"] = f"开挂前场景未稳定: {scene_err}"
        log(f"hang start: {out['message']}")
        return out

    if _wanzi_direct_enabled(cfg):
        # Restarting挂机 must quiesce the old direct worker first, while the
        # hang owner reservation remains held across the stop/start sequence.
        paused = stop_wanzi_packet_hang(session, release=False, log=log)
        if not paused.get("ok"):
            out["message"] = str(paused.get("message") or "丸子直发停止失败")
            out["wanzi"] = paused
            return out

    try:
        mem0 = probe_hang_state_mem(session, log=log)
        if mem0.ok and mem0.on is True:
            out["stop_before"] = _stop_hang_unlocked(
                session, cfg, hwnd=hwnd_i, settle_s=settle_s, log=log
            )
            if not bool(out["stop_before"].get("ok")):
                out["message"] = "重启前关挂机失败: " + str(
                    out["stop_before"].get("message") or "unknown"
                )
                log(f"hang start: {out['message']}")
                return out
            time.sleep(max(0.2, float(settle_s) * 0.6))
    except Exception as e:
        log(f"hang start: pre-stop probe {e}")

    try:
        if temporary:
            target_guard = {"ok": True, "enabled": False, "reason": "temporary_hang"}
        else:
            target_guard = _arm_dungeon_target_guard(
                session, cfg, hwnd=hwnd_i, log=log
            )
        out["target_guard"] = target_guard
        if not bool(target_guard.get("ok")):
            out["ok"] = False
            out["message"] = "副本卡怪拦截未就绪: " + str(
                target_guard.get("error") or "unknown error"
            )
            log(f"hang start: {out['message']}")
            return out
        # 封包发送前先把 CECAutoPlay 模式写到目标值：状态机按目标模式启动，
        # 不要等启动到一半（封包响应初始化中）再改模式。
        try:
            from app.core.activity_auto import set_autoplay_mode as _set_mode_pre

            if not bool(
                _set_mode_pre(session, int(cfg.mode), log=log).get("ok")
            ):
                log("hang start: pre-packet mode set failed (continuing)")
        except Exception as e:
            log(f"hang start: pre-packet mode set err {e}")
        # 副本模式：封包发送前先本地直调 StartAutoPlay 初始化状态机。
        # 背景：仅靠封包时，服务器响应 + seed follow 的初始化在"队长身份
        # 开挂"场景下跟随快照指向自己，机器会停在 StateAlert 不打怪；
        # 本地直调把机器直接置入 StateAttack（可战斗态），目标由守护里的
        # 放行看门狗补齐。锚点 NaN 由开挂确认后的 anchor repair 兜底。
        # 注：场景门（我们自己的 CRT 围栏）在进本/过图后可能延迟放行，
        # 故先等门（wait_pid_scene_stable）再调——2026-08-30 2.1.1 重现。
        # 曾试验改走桥接 Btn_Start 处理器（UI 线程）：实测只置 running 旗标、
        # 状态机停在 Idle，无法替代本地初始化，已回退。
        if dungeon_mode:
            try:
                from app.core.remote_runtime import wait_pid_scene_stable

                wait_pid_scene_stable(
                    int(getattr(session, "pid", 0) or 0), timeout_s=5.0
                )
            except Exception as e:
                log(f"hang start: scene gate wait 超时，仍尝试本地初始化: {str(e)[:60]}")
            from app.core.activity_auto import start_autoplay_force

            force_ret = start_autoplay_force(
                session, send_packet=False, log=log
            )
        # 开挂封包窗口冻结 hang-owned 丸子发包：raw_c2s 外来线程与游戏
        # 封包响应初始化并发，见 _resolve_host_data 注释（2026-08-30 崩溃）。
        _wanzi_pause_for_transition(_hang_pid(session))
        ret = _send_hang_control_packet(
            session, HANG_START_PACKET, action="开启", log=log
        )
        out["start"] = ret
        ok = bool(ret.get("ok"))
        msg = str(ret.get("message") or "开启挂机封包发送失败")
        out["ok"] = bool(ok)
        out["message"] = str(msg)
    except Exception as e:
        try:
            _disarm_dungeon_target_guard(session, hwnd=hwnd_i, log=log)
        except Exception:
            pass
        out["ok"] = False
        out["message"] = f"开挂异常: {e}"
        log(out["message"])
        return out

    try:
        time.sleep(max(0.15, min(0.6, float(settle_s) * 0.4)))
    except Exception:
        pass
    try:
        live = read_hang_live(session, log=log)
        out["live"] = live.to_dict()
        if live.running is True:
            out["ok"] = True
        elif live.running is False and out["ok"]:
            out["ok"] = False
            out["message"] = (out["message"] or "") + " · 复检仍关"
    except Exception:
        pass

    log(f"hang start: ok={out['ok']} via={out['via']} {out['message']}")
    # Re-apply mode/radius/pickup AFTER the packet has settled,
    # regardless of whether the hang is confirmed running.  The game's
    # autoplay init (or Alt+R handler) may overwrite our pre-start writes
    # with game-side defaults; this ensures the CECAutoPlay object has our
    # intended values even if the hang didn't toggle on this attempt.
    try:
        time.sleep(0.2)
    except Exception:
        pass
    try:
        from app.core.activity_auto import (
            set_autoplay_mode as _set_mode,
            set_autoplay_radius as _set_radius,
        )

        _mr = _set_mode(session, int(cfg.mode), log=log)
        if not _mr.get("ok"):
            log(f"hang start: mode re-apply failed {_mr}")
        else:
            log(f"hang start: mode re-applied={autoplay_mode_name(cfg.mode)}")
        _rr = _set_radius(session, int(cfg.radius), allow_below_ui_min=True, log=log)
        if not _rr.get("ok"):
            log(f"hang start: radius re-apply failed {_rr}")
        else:
            log(f"hang start: radius re-applied={int(cfg.radius)}")
        _pr = apply_party_auto_need(session, bool(cfg.enable_pickup), log=log)
        if not _pr.get("ok"):
            log(f"hang start: pickup re-apply failed {_pr}")
        else:
            log(f"hang start: pickup re-applied={bool(cfg.enable_pickup)}")
    except Exception as _me:
        log(f"hang start: post-start reapply err {_me}")
    # 修复锚点：封包响应处理器设置的锚点可能在坐标未稳定时写入 NaN。
    # 挂机已确认 running，bridge 坐标可靠，补写正确锚点。
    # 普通/副本模式都补：普通模式开挂同样会继承上一轮残留锚点（可能相距
    # 几十米或为 NaN），不给游戏状态机留垃圾数据。
    if out.get("ok"):
        try:
            from app.core.xajh_bridge import ensure_bridge
            from app.core.activity_auto import _wpm_f32, resolve_cec_autoplay_rpm

            br = ensure_bridge(
                int(getattr(session, "pid", 0) or 0),
                log=log,
                inject_if_needed=False,
                hwnd=hwnd_i or None,
                force_reinject=False,
            )
            if br is not None:
                try:
                    snap = br.host_snapshot(timeout_ms=2000)
                    if snap.ok and snap.x is not None:
                        ap_mem = resolve_cec_autoplay_rpm(session)
                        ap = int(ap_mem.get("autoplay") or 0)
                        if ap:
                            _wpm_f32(session, ap + 0x10, float(snap.x))
                            _wpm_f32(session, ap + 0x14, float(snap.y))
                            _wpm_f32(session, ap + 0x18, float(snap.z))
                            log(
                                "hang start: anchor repaired "
                                f"({snap.x:.1f}, {snap.y:.1f}, {snap.z:.1f})"
                            )
                finally:
                    try:
                        br.close()
                    except Exception:
                        pass
        except Exception as e:
            log(f"hang start: anchor repair err {e}")
    _wanzi_resume_for_transition(_hang_pid(session))
    # 副本跳过剧情、武尊自动开怪 不再在开挂时立即调用，
    # 统一由 hang guard 在场景稳定后按需触发（首次+切换）。
    if out.get("ok"):
        try:
            if temporary:
                out["guard"] = {
                    "ok": True,
                    "enabled": False,
                    "reason": "temporary_hang",
                }
            else:
                out["guard"] = start_hang_guard(
                    session, cfg, log=log, startup_maintain=bool(maintain)
                )
                if maintain:
                    out["maintain"] = {
                        "ok": True,
                        "queued": True,
                        "reason": "startup_guard",
                        "message": "启动维护已排队（维修/活力）",
                    }
        except Exception as e:
            out["guard"] = {"ok": False, "error": str(e)}
            log(f"hang start: guard err {e}")
    return out


def hang_maintain_once(
    session: GameAttachSession,
    cfg: HangConfig,
    *,
    force_rv: bool = False,
    log: LogFn | None = None,
) -> dict:
    """One internal maintain tick: repair / vitality / abandon rolls.

    force_rv=True: run repair/vitality check once even if long interval not due
    (used by start_hang). Default path checks every 30min repair / vitality
    and never enumerates bag / use_item when not due.

    @author by ak
    """
    log = log or (lambda _m: None)
    # Outer gate: whole maintain is one exclusive unit.
    # Child repair/vitality/loot nest under maintain owner on same thread.
    with hang_action_guard(
        session, "maintain", log=log, require_remote=True, check_call_busy=False
    ) as gate:
        if gate.get("skipped"):
            msg = gate.get("message") or "挂机维护跳过(繁忙)"
            # Busy: do not mark long repair/vitality intervals; soft backoff only
            # is applied inside unlocked path when a check was attempted.
            log(f"hang maintain: {msg}")
            return {
                "ok": True,
                "skipped": True,
                "busy": True,
                "reason": gate.get("reason") or "busy",
                "force_rv": bool(force_rv),
                "repair": None,
                "vitality": None,
                "loot": None,
                "messages": [msg],
                "message": msg,
            }
        out = _hang_maintain_once_unlocked(
            session, cfg, force_rv=bool(force_rv), log=log
        )
        _mark_hang_cooldown(session, "maintain")
        return out


def _hang_maintain_once_unlocked(
    session: GameAttachSession,
    cfg: HangConfig,
    *,
    force_rv: bool = False,
    log: LogFn | None = None,
) -> dict:
    """Maintain body under hang_action_guard(maintain). @author by ak"""
    log = log or (lambda _m: None)
    out: dict = {
        "ok": True,
        "force_rv": bool(force_rv),
        "repair": None,
        "vitality": None,
        "loot": None,
        "messages": [],
    }

    # --- repair: long check interval; no RPM/bag when not due ---
    if bool(cfg.auto_repair):
        due, remain = _hang_check_due(
            session,
            "repair_check",
            interval_s=float(HANG_REPAIR_CHECK_INTERVAL_S),
            force=bool(force_rv),
        )
        if not due:
            out["repair"] = {
                "ok": True,
                "skipped": True,
                "reason": "check_interval",
                "remain_s": remain,
                "message": f"维修检测冷却中 {remain:.0f}s",
            }
        else:
            try:
                dur = read_equipment_durability_pct(session, log=log)
            except Exception as e:
                dur = {"ready": False, "error": str(e)}
            if not dur.get("ready") or dur.get("pct") is None:
                # Completed a check attempt; mark long interval to avoid spam.
                _mark_hang_cooldown(session, "repair_check")
                out["repair"] = {
                    "ok": False,
                    "skipped": True,
                    "reason": "not_ready",
                    "message": "自动维修：耐久待校准",
                }
                out["messages"].append("维修跳过(待校准)")
            else:
                # 用最低耐久判定：均值 90% 但单件 0% 也必须修。
                try:
                    mean_pct = float(dur.get("pct"))
                except Exception:
                    mean_pct = 100.0
                try:
                    min_pct = float(
                        dur["min_pct"]
                        if dur.get("min_pct") is not None
                        else mean_pct
                    )
                except Exception:
                    min_pct = mean_pct
                pct = float(min_pct)
                thr = float(cfg.repair_below_pct)
                if pct <= thr:
                    # Repair items cannot be used while the host is dead. The
                    # guard keeps the death check queued until revival.
                    if get_hang_dead_state(session) is True:
                        ur = {
                            "ok": True,
                            "skipped": True,
                            "reason": "dead_deferred",
                            "message": "角色死亡，维修延后到复活后",
                        }
                    else:
                        ur = _use_repair_box(session, log=log)
                    ur["pct"] = pct
                    ur["mean_pct"] = mean_pct
                    ur["min_pct"] = min_pct
                    ur["threshold"] = thr
                    out["repair"] = ur
                    repair_retry = (
                        bool(ur.get("busy"))
                        or ur.get("reason") == "dead_deferred"
                        or (not ur.get("ok") and not ur.get("skipped"))
                    )
                    if repair_retry:
                        _mark_hang_check_backoff(
                            session,
                            "repair_check",
                            interval_s=float(HANG_REPAIR_CHECK_INTERVAL_S),
                            backoff_s=float(HANG_RV_BUSY_BACKOFF_S),
                        )
                        out["messages"].append(
                            ur.get("message") or "维修跳过(繁忙)"
                        )
                    else:
                        _mark_hang_cooldown(session, "repair_check")
                        out["messages"].append(
                            ur.get("message")
                            or (
                                f"维修 最低耐久{pct:.0f}%<=阈值{thr:.0f}%"
                                f"(均{mean_pct:.0f}%)"
                                if ur.get("ok")
                                else "维修失败"
                            )
                        )
                    if not ur.get("ok") and not ur.get("skipped"):
                        out["ok"] = False
                else:
                    _mark_hang_cooldown(session, "repair_check")
                    msg = (
                        f"耐久最低{min_pct:.0f}%/均{mean_pct:.0f}%"
                        f">阈值{thr:.0f}%，不修"
                    )
                    out["repair"] = {
                        "ok": True,
                        "skipped": True,
                        "reason": "above_threshold",
                        "pct": pct,
                        "mean_pct": mean_pct,
                        "min_pct": min_pct,
                        "threshold": thr,
                        "message": msg,
                    }
                    # force 启动时也打出来，方便确认检测跑了
                    if force_rv:
                        out["messages"].append(msg)
    else:
        out["repair"] = {"ok": True, "skipped": True, "reason": "disabled"}

    # --- vitality: long check interval; no RPM/bag when not due ---
    if bool(cfg.auto_vitality):
        due, remain = _hang_check_due(
            session,
            "vitality_check",
            interval_s=float(HANG_VITALITY_CHECK_INTERVAL_S),
            force=bool(force_rv),
        )
        if not due:
            out["vitality"] = {
                "ok": True,
                "skipped": True,
                "reason": "check_interval",
                "remain_s": remain,
                "message": f"活力检测冷却中 {remain:.0f}s",
            }
        else:
            try:
                vit = read_vitality_pct(session, log=log)
            except Exception as e:
                vit = {"ready": False, "error": str(e)}
            if not vit.get("ready") or vit.get("pct") is None:
                _mark_hang_cooldown(session, "vitality_check")
                out["vitality"] = {
                    "ok": False,
                    "skipped": True,
                    "reason": "not_ready",
                    "message": "自动补活力：活力待校准",
                }
                out["messages"].append("活力跳过(待校准)")
            else:
                try:
                    pct = float(vit.get("pct"))
                except Exception:
                    pct = 100.0
                thr = float(cfg.vitality_below_pct)
                if pct <= thr:
                    if get_hang_dead_state(session) is True:
                        ur = {
                            "ok": True,
                            "skipped": True,
                            "reason": "dead_deferred",
                            "message": "角色死亡，补活力延后到复活后",
                        }
                    else:
                        ur = _use_vitality_potion(session, log=log)
                    ur["pct"] = pct
                    ur["threshold"] = thr
                    out["vitality"] = ur
                    vitality_retry = (
                        bool(ur.get("busy"))
                        or ur.get("reason") == "dead_deferred"
                        or (not ur.get("ok") and not ur.get("skipped"))
                    )
                    if vitality_retry:
                        _mark_hang_check_backoff(
                            session,
                            "vitality_check",
                            interval_s=float(HANG_VITALITY_CHECK_INTERVAL_S),
                            backoff_s=float(HANG_RV_BUSY_BACKOFF_S),
                        )
                        out["messages"].append(
                            ur.get("message") or "补活力跳过(繁忙)"
                        )
                    else:
                        _mark_hang_cooldown(session, "vitality_check")
                        out["messages"].append(
                            ur.get("message")
                            or (
                                f"补活力 {pct:.0f}%<={thr:.0f}%"
                                if ur.get("ok")
                                else "补活力失败"
                            )
                        )
                    if not ur.get("ok") and not ur.get("skipped"):
                        out["ok"] = False
                else:
                    _mark_hang_cooldown(session, "vitality_check")
                    out["vitality"] = {
                        "ok": True,
                        "skipped": True,
                        "reason": "above_threshold",
                        "pct": pct,
                        "threshold": thr,
                        "message": f"活力{pct:.0f}%>阈值{thr:.0f}%，不补",
                    }
    else:
        out["vitality"] = {"ok": True, "skipped": True, "reason": "disabled"}

    # --- loot rolls ---
    # pickup off  -> Pass/放弃
    # pickup on + dead -> Need/需求（死亡时内挂不 roll）
    # pickup on + alive -> 内挂自动需求，我们不发包
    if not bool(cfg.enable_pickup):
        lr = abandon_all_loot_rolls(session, log=log)
        out["loot"] = lr
        out["messages"].append(str(lr.get("message") or "全部放弃"))
        if not lr.get("ok") and not lr.get("skipped"):
            out["ok"] = False
    else:
        dead = refresh_hang_dead_state(session, log=log, force=False)
        out["dead"] = dead
        if dead is True:
            lr = need_all_loot_rolls(session, log=log)
            out["loot"] = lr
            out["messages"].append(str(lr.get("message") or "死亡需求"))
            if not lr.get("ok") and not lr.get("skipped"):
                out["ok"] = False
        elif dead is False:
            out["loot"] = {
                "ok": True,
                "skipped": True,
                "reason": "alive_auto_need",
                "dead": False,
                "message": "拾取开且存活：依赖内挂自动需求",
            }
        else:
            out["loot"] = {
                "ok": True,
                "skipped": True,
                "reason": "dead_unknown",
                "dead": None,
                "message": "拾取开：死亡态未知，暂不代点需求",
            }

    out["message"] = "；".join(out["messages"]) or "maintain idle"
    # 放弃/需求成功每波都会进 maintain，功能正常时不刷屏。
    # 仍记录：失败、启动 force_rv、以及维修/活力等非掷点结果。
    _msg = str(out.get("message") or "")
    _keep = (not bool(out.get("ok"))) or bool(force_rv) or any(
        key in _msg for key in ("维修", "活力", "补活", "耐久")
    )
    if _keep:
        log(f"hang maintain: ok={out['ok']} force_rv={bool(force_rv)} {out['message']}")
    return out


# ---------------------------------------------------------------------------
# Long-running hang guard via SafeDispatch (roll poll + rare repair/vitality)
# ---------------------------------------------------------------------------

HANG_GUARD_JOB_ID = "hang_guard"
_HANG_GUARD_CFG: dict[int, HangConfig] = {}
_HANG_GUARD_STARTUP_MAINTAIN: set[int] = set()
_HANG_GUARD_DEATH_MAINTAIN: set[int] = set()
_HANG_GUARD_DEATH_SEEN_TS: dict[int, float] = {}
_HANG_GUARD_TOKEN: dict[int, object] = {}
_HANG_GUARD_SESSION: dict[int, GameAttachSession] = {}
_HANG_GUARD_SESSION_OWNED: set[int] = set()
_HANG_GUARD_CFG_LOCK = threading.Lock()
_PLOT_SKIP_ASYNC_LOCK = threading.Lock()
_PLOT_SKIP_ASYNC_GEN: dict[int, int] = {}
_HANG_GUARD_LAST_SCENE: dict[int, int] = {}
_HANG_GUARD_SCENE_REARM: set[int] = set()
_HANG_GUARD_REARM_RETRY: dict[int, int] = {}
# 过图后本地重初始化的重试上限（守护 tick 1s 节奏下 ≈ 5s）
_SCENE_REARM_RETRY_MAX = 5
_KNOWN_DUNGEON_SCENE_IDS: frozenset[int] | None = None


def _known_dungeon_scene_ids() -> frozenset[int]:
    """Return the explicit scene IDs approved for dungeon-only automation."""
    global _KNOWN_DUNGEON_SCENE_IDS
    if _KNOWN_DUNGEON_SCENE_IDS is not None:
        return _KNOWN_DUNGEON_SCENE_IDS
    scene_ids: set[int] = set()
    try:
        from app.core.dungeon_board_catalog import load_catalog

        scene_ids.update(
            int(scene_id)
            for scene_id in (load_catalog().get("scene_to_instance") or {})
        )
    except Exception:
        pass
    try:
        from app.core.jianglong_auto import WUZUN_SCENE_IDS

        scene_ids.update(int(scene_id) for scene_id in WUZUN_SCENE_IDS)
    except Exception:
        pass
    _KNOWN_DUNGEON_SCENE_IDS = frozenset(scene_ids)
    return _KNOWN_DUNGEON_SCENE_IDS


def _stable_known_dungeon_scene(pid: int) -> tuple[bool, int, str]:
    """Fail closed unless the live scene snapshot is stable and explicitly known."""
    try:
        from app.core.remote_runtime import (
            get_pid_scene_snapshot,
            is_pid_scene_snapshot_stable,
        )

        snapshot = get_pid_scene_snapshot(int(pid))
        scene_id = int(snapshot.get("scene_id") or 0) if snapshot else 0
        if not snapshot or not is_pid_scene_snapshot_stable(int(pid)):
            return False, scene_id, "scene_unstable"
        if scene_id not in _known_dungeon_scene_ids():
            return False, scene_id, "unknown_or_non_dungeon_scene"
        return True, scene_id, ""
    except Exception:
        return False, 0, "scene_snapshot_unavailable"


def _disable_dungeon_story_hook(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
) -> None:
    """Disable passive CG skipping after leaving a known dungeon scene."""
    log = log or (lambda _m: None)
    pid = int(getattr(session, "pid", 0) or 0)
    if pid <= 0:
        return
    try:
        from app.core.xajh_bridge import ensure_bridge

        bridge = ensure_bridge(
            pid,
            log=log,
            inject_if_needed=False,
            hwnd=int(getattr(session, "hwnd", 0) or 0) or None,
            force_reinject=False,
        )
        if bridge is not None:
            bridge.cg_skip(mode=0, timeout_ms=1200)
            log("hang plot skip: disabled outside known dungeon")
    except Exception as exc:
        log(f"hang plot skip: disable outside dungeon err {exc}")


def schedule_skip_dungeon_story_async(
    session: GameAttachSession | int | None,
    *,
    hwnd: int = 0,
    delay_s: float | None = None,
    arm_only: bool = False,
    log: LogFn | None = None,
) -> dict:
    """Queue plot skip off the hang-start / guard tick path.

    Hang start must return as soon as autoplay is on. CG hook arm (+ optional
    key pulse) runs later so it never blocks the in-game hang switch.

    arm_only=True: install/enable passive CG hooks only. Used by settings save
    hot-toggle so we never Esc/Space-spam while the player is mid-fight.

    @author by ak
    """
    log = log or (lambda _m: None)
    pid = _hang_pid(session)
    if not pid:
        return {"ok": False, "error": "no_pid", "scheduled": False}
    try:
        delay = float(HANG_PLOT_SKIP_DELAY_S if delay_s is None else delay_s)
    except Exception:
        delay = float(HANG_PLOT_SKIP_DELAY_S)
    delay = max(0.0, min(8.0, delay))
    # Hot-toggle from 保存 can race apply_hang_prepare; keep a tiny settle.
    if arm_only and delay < 0.15:
        delay = 0.15
    hwnd_i = int(hwnd or getattr(session, "hwnd", 0) or 0) if session is not None else int(hwnd or 0)
    arm_only_i = bool(arm_only)
    with _PLOT_SKIP_ASYNC_LOCK:
        gen = int(_PLOT_SKIP_ASYNC_GEN.get(int(pid), 0) or 0) + 1
        _PLOT_SKIP_ASYNC_GEN[int(pid)] = gen

    def _worker() -> None:
        try:
            if delay > 0:
                time.sleep(delay)
        except Exception:
            pass
        with _PLOT_SKIP_ASYNC_LOCK:
            if int(_PLOT_SKIP_ASYNC_GEN.get(int(pid), 0) or 0) != int(gen):
                return
        guard_session = None
        owned = False
        with _HANG_GUARD_CFG_LOCK:
            guard_session = _HANG_GUARD_SESSION.get(int(pid))
        sess = guard_session
        try:
            if sess is None or int(getattr(sess, "pid", 0) or 0) != int(pid):
                sess = GameAttachSession(log=lambda _m: None)
                sess.attach(int(pid))
                sess.hwnd = int(hwnd_i or 0)
                owned = True
            allowed, scene_id, reason = _stable_known_dungeon_scene(pid)
            if not allowed:
                log(
                    "hang plot skip: blocked "
                    f"scene={scene_id or '-'} reason={reason}"
                )
                if reason == "unknown_or_non_dungeon_scene":
                    _disable_dungeon_story_hook(sess, log=log)
                return
            skip_dungeon_story_once(
                sess,
                hwnd=int(hwnd_i or getattr(sess, "hwnd", 0) or 0),
                force=True,
                arm_only=arm_only_i,
                log=log,
            )
        except Exception as e:
            log(f"hang plot skip async err pid={pid}: {e}")
        finally:
            if owned and sess is not None:
                try:
                    sess.close()
                except Exception:
                    pass

    threading.Thread(
        target=_worker,
        daemon=True,
        name=f"hang-plot-skip-{pid}-{gen}",
    ).start()
    kind = "arm-only" if arm_only_i else "full"
    log(f"hang plot skip: scheduled async {kind} delay={delay:.2f}s gen={gen}")
    return {
        "ok": True,
        "scheduled": True,
        "delay_s": delay,
        "gen": int(gen),
        "pid": int(pid),
        "arm_only": arm_only_i,
        "message": f"剧情跳过已异步排队 {delay:.1f}s 后执行({kind})",
    }


def skip_dungeon_story_once(
    session: GameAttachSession,
    *,
    hwnd: int = 0,
    force: bool = True,
    arm_only: bool = False,
    log: LogFn | None = None,
) -> dict:
    """Event-driven CG/talk skip: arm passive CG hook + optional key burst.

    Active hang path only calls this on hang-start / scene-change settle, or
    as arm_only from settings save hot-toggle. It does NOT poll dialogs every
    guard tick (avoids CRT competition).

    Primary path: bridge CMD_CG_SKIP arms PlayCG/PlayBlackEdge detours; when
    the game starts a cinematic the hook immediately StopCG/StopBlackEdge.
    Fallback: Esc (and Space only when a talk dialog is visible).

    force=True (default, production): arm hook; pulse keys for this event.
    force=False (lab/manual): only pulse if a known talk dialog is shown.
    arm_only=True: install/enable hooks only — never Esc/Space. Required for
    快捷设置保存 hot-toggle so combat hang is not spammed with 轻功/关窗.

    Game RE: CGApi.PlayCG@0x846EF0 / PlayBlackEdge@0x8449B0;
    HelpSystem:EscCanCloseALLCG / talk advances on Space.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "forced": bool(force),
        "shown": [],
        "esc": 0,
        "space": 0,
        "cg_hook": None,
        "message": "",
        "error": None,
    }
    pid = int(getattr(session, "pid", 0) or 0)
    if pid <= 0:
        out["error"] = "no_pid"
        out["message"] = "无游戏 pid"
        return out

    arm_only = bool(arm_only)
    out["arm_only"] = arm_only
    shown: list[str] = []
    # One-shot dialog probe (event path only). Never done on guard ticks.
    # Even force mode benefits: Space is 轻功 in open world, so only press it
    # when a talk dialog is actually visible.
    try:
        from app.core.plg_ui import query_dlg_show

        for name in PLOT_SKIP_DLG_NAMES:
            try:
                hit = query_dlg_show(session, name, log=lambda _m: None)
            except Exception:
                continue
            if bool(getattr(hit, "shown", False)):
                shown.append(str(name))
    except Exception as e:
        if not force and not arm_only:
            out["error"] = f"dlg probe: {e}"
    out["shown"] = list(shown)
    if not force and not arm_only and not shown:
        out["ok"] = True
        out["message"] = "无剧情/对话窗"
        return out

    remain = hang_cooldown_remain(
        session, "plot_skip", cooldown_s=float(HANG_PLOT_SKIP_COOLDOWN_S)
    )
    if remain > 0 and not force and not arm_only:
        out["ok"] = True
        out["message"] = f"剧情跳过冷却 {remain:.2f}s"
        return out

    hwnd_i = int(hwnd or getattr(session, "hwnd", 0) or 0)
    bridge = None
    try:
        from app.core.bg_input import press_bg_chord_once
        from app.core.sys_input import VK_ESCAPE, VK_SPACE
        from app.core.xajh_bridge import ensure_bridge

        bridge = ensure_bridge(
            pid,
            log=log,
            inject_if_needed=True,
            hwnd=hwnd_i or None,
            force_reinject=False,
        )
        if bridge is None:
            out["error"] = "bridge not ready"
            out["message"] = "剧情跳过桥接未就绪"
            return out

        # Arm passive CG hooks once per event. No hang_guard tick scanning.
        # mode=1 installs hooks only (no native ForceStopCurrentCg).
        try:
            cg = bridge.cg_skip(mode=1, timeout_ms=1500)
            out["cg_hook"] = {
                "ok": bool(getattr(cg, "ok", False)),
                "ret": getattr(cg, "ret", None),
                "note": str(getattr(cg, "note", "") or getattr(cg, "error", "") or ""),
            }
            if out["cg_hook"]["ok"]:
                log(f"hang plot skip: cg hook armed {out['cg_hook']['note']}")
            else:
                log(
                    "hang plot skip: cg hook arm failed "
                    f"{out['cg_hook']['note'] or out['cg_hook']}"
                )
        except Exception as e:
            out["cg_hook"] = {"ok": False, "error": str(e)}
            log(f"hang plot skip: cg hook err {e}")

        if arm_only:
            hook_ok = bool((out.get("cg_hook") or {}).get("ok"))
            out["ok"] = bool(hook_ok)
            out["message"] = (
                f"剧情跳过 arm-only hook={'on' if hook_ok else 'off'}"
            )
            if out["ok"]:
                # Do not burn the key-pulse cooldown; scene rearm may still need it.
                log(f"hang plot skip: {out['message']}")
            else:
                out["error"] = "cg hook arm failed"
            return out

        # CG hook handles CGs passively. Only pulse Esc/Space when a known
        # talk dialog is actually visible — never spam keys preemptively.
        if not shown:
            hook_ok = bool((out.get("cg_hook") or {}).get("ok"))
            out["ok"] = bool(hook_ok)
            out["message"] = (
                f"剧情跳过 hook={'on' if hook_ok else 'off'}（无当前对话窗）"
            )
            return out

        bursts = max(1, int(HANG_PLOT_SKIP_BURST))
        # Esc closes CG / talk UI. Space advances talk but is also 轻功 — only
        # when a known dialog is on screen.
        want_space = bool(shown)
        for _i in range(bursts):
            kr = press_bg_chord_once(
                pid,
                [int(VK_ESCAPE)],
                hwnd=hwnd_i,
                hold_ms=45,
                allow_softsend=False,
                clear_all_after=False,
                log=log,
            )
            if bool(kr.get("ok")):
                out["esc"] = int(out["esc"]) + 1
            time.sleep(0.05)
            if want_space:
                kr2 = press_bg_chord_once(
                    pid,
                    [int(VK_SPACE)],
                    hwnd=hwnd_i,
                    hold_ms=40,
                    allow_softsend=False,
                    clear_all_after=False,
                    log=log,
                )
                if bool(kr2.get("ok")):
                    out["space"] = int(out["space"]) + 1
                time.sleep(0.04)

        _mark_hang_cooldown(session, "plot_skip")
        hook_ok = bool((out.get("cg_hook") or {}).get("ok"))
        out["ok"] = bool(out["esc"] or out["space"] or hook_ok)
        tag = ",".join(shown) if shown else ("force" if force else "-")
        out["message"] = (
            f"剧情跳过 esc={out['esc']} space={out['space']} "
            f"hook={'on' if hook_ok else 'off'} dlg={tag}"
        )
        if out["ok"]:
            log(f"hang plot skip: {out['message']}")
        else:
            out["error"] = "key pulse failed"
        return out
    except Exception as e:
        out["error"] = str(e)
        out["message"] = f"剧情跳过异常: {e}"
        log(out["message"])
        return out
    finally:
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass


def _hang_guard_cfg(pid: int) -> HangConfig:
    with _HANG_GUARD_CFG_LOCK:
        return _HANG_GUARD_CFG.get(int(pid)) or HangConfig()


def _hang_guard_current(pid: int, token: object) -> bool:
    """Whether a periodic callback still belongs to the active guard."""
    with _HANG_GUARD_CFG_LOCK:
        return _HANG_GUARD_TOKEN.get(int(pid)) is token


def _hang_guard_job_active(dispatch, pid: int) -> bool:
    """Whether SafeDispatch still owns a live hang-guard timer."""
    list_periodics = getattr(dispatch, "list_periodics", None)
    if not callable(list_periodics):
        # Lightweight test/embedded dispatch adapters predate introspection.
        return True
    try:
        jobs = list_periodics(int(pid))
    except Exception:
        return True
    return any(
        str(job.get("job_id") or "") == HANG_GUARD_JOB_ID
        and job.get("alive") is not False
        for job in (jobs or [])
        if isinstance(job, dict)
    )


def _note_hang_guard_death_transition(pid: int) -> bool:
    """Queue one maintenance pass for an unseen alive/unknown -> dead edge.

    The death state is refreshed at most every three seconds.  Its ``prev``
    field remains false while that cached sample is reused, so a per-guard
    timestamp fence is required to consume the transition only once.

    @author by ak
    """
    pid = int(pid)
    with _HANG_DEAD_STATE_LOCK:
        state = dict(_HANG_DEAD_STATE.get(pid) or {})
    if state.get("dead") is not True or state.get("prev") is True:
        return False
    ts = float(state.get("ts") or 0.0)
    if ts <= 0:
        return False
    with _HANG_GUARD_CFG_LOCK:
        if _HANG_GUARD_DEATH_SEEN_TS.get(pid) == ts:
            return False
        _HANG_GUARD_DEATH_SEEN_TS[pid] = ts
        _HANG_GUARD_DEATH_MAINTAIN.add(pid)
    return True



def update_hang_guard_config(
    session: GameAttachSession | int | None,
    cfg: HangConfig,
    *,
    log: LogFn | None = None,
) -> dict:
    """Push live hang config into an already-running guard (hot toggle).

    Arms/disarms CG skip when 副本跳过剧情 flips while hang is active.
    No-op if guard is not running for this pid.

    @author by ak
    """
    log = log or (lambda _m: None)
    pid = _hang_pid(session)
    if not pid:
        return {"ok": False, "error": "no_pid"}
    with _HANG_GUARD_CFG_LOCK:
        if pid not in _HANG_GUARD_CFG:
            return {"ok": True, "updated": False, "reason": "no_guard"}
        prev = _HANG_GUARD_CFG.get(pid)
        guard_session = _HANG_GUARD_SESSION.get(pid)
        _HANG_GUARD_CFG[pid] = cfg
    prev_on = bool(getattr(prev, "skip_dungeon_story", False)) if prev else False
    now_on = bool(getattr(cfg, "skip_dungeon_story", False))
    plot = None
    if now_on and not prev_on and guard_session is not None:
        try:
            # Settings 保存 hot-toggle: hooks only. Never Esc/Space mid-hang.
            plot = schedule_skip_dungeon_story_async(
                guard_session,
                hwnd=int(getattr(guard_session, "hwnd", 0) or 0),
                delay_s=0.35,
                arm_only=True,
                log=log,
            )
        except Exception as e:
            plot = {"ok": False, "error": str(e)}
            log(f"hang guard: live plot arm err {e}")
    elif prev_on and not now_on:
        try:
            from app.core.xajh_bridge import ensure_bridge

            br = ensure_bridge(
                pid,
                log=log,
                inject_if_needed=True,
                hwnd=int(getattr(guard_session, "hwnd", 0) or 0) or None if guard_session else None,
                force_reinject=False,
            )
            if br is not None:
                br.cg_skip(mode=0, timeout_ms=1200)
                plot = {"ok": True, "disarmed": True}
        except Exception as e:
            plot = {"ok": False, "error": str(e)}
            log(f"hang guard: live plot disarm err {e}")
    # The target guard has no periodic work. A live settings change is the only
    # time it may be reconfigured; normal mode always keeps it disarmed.
    try:
        was_armed = int(pid) in _DUNGEON_TARGET_GUARD_ARMED
        should_arm = (
            guard_session is not None
            and not was_armed
            and int(getattr(cfg, "mode", -1)) == int(AUTOPLAY_MODE_DUNGEON)
            and bool(getattr(cfg, "ignore_dungeon_stuck", False))
        )
        # TID 变更按设计在下次开挂时生效；守护 tick 只负责"未武装则武装"，
        # 不重入（mode=3 每次都会重置 trace 并重新 sanitize）。
        if should_arm:
            target_guard = _arm_dungeon_target_guard(
                guard_session,
                cfg,
                hwnd=int(getattr(guard_session, "hwnd", 0) or 0),
                log=log,
            )
        elif was_armed:
            target_guard = _disarm_dungeon_target_guard(
                guard_session or pid,
                hwnd=int(getattr(guard_session, "hwnd", 0) or 0),
                log=log,
            )
        else:
            target_guard = {"ok": True, "enabled": False, "reason": "not requested"}
    except Exception as e:
        target_guard = {"ok": False, "enabled": False, "error": str(e)}
        log(f"hang guard: dungeon target guard hot-toggle err {e}")
    return {
        "ok": True,
        "updated": True,
        "pid": int(pid),
        "plot_skip": plot,
        "target_guard": target_guard,
    }


def start_hang_guard(
    session: GameAttachSession,
    cfg: HangConfig | None = None,
    *,
    log: LogFn | None = None,
    interval_s: float | None = None,
    startup_maintain: bool = False,
) -> dict:
    """
    Start hang's long-running guardian loop.

    Business owns call rates (HANG_GUARD_TICK_S / repair 1800s / loot CD).
    SafeDispatch only orchestrates the timer + shared cache/gate.

    Tick: RPM roll poll (optional short cache); abandon only when pending and
    pickup off. Repair/vitality remain due-driven inside hang_maintain_once.

    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(getattr(session, "pid", 0) or 0)
    if pid <= 0:
        return {"ok": False, "error": "no_pid"}
    cfg = cfg or HangConfig()
    dungeon_mode = int(getattr(cfg, "mode", -1)) == int(AUTOPLAY_MODE_DUNGEON)

    from app.core.safe_dispatch import (
        OpKind,
        Priority,
        RemoteBlockedError,
        get_dispatch,
        roll_cache_key,
    )

    disp = get_dispatch()
    with _HANG_GUARD_CFG_LOCK:
        active_token = _HANG_GUARD_TOKEN.get(pid)
        active_session = _HANG_GUARD_SESSION.get(pid)
        active_session_valid = (
            active_token is not None
            and int(getattr(active_session, "pid", 0) or 0) == pid
        )
    if active_session_valid and _hang_guard_job_active(disp, pid):
        # Qiegao calls this after every temporary hang-off. Keep the original
        # timer/token and death edge so a queued post-revival check is not lost.
        with _HANG_GUARD_CFG_LOCK:
            if (
                _HANG_GUARD_TOKEN.get(pid) is active_token
                and _HANG_GUARD_SESSION.get(pid) is active_session
            ):
                prev = _HANG_GUARD_CFG.get(pid)
                _HANG_GUARD_CFG[pid] = cfg
                if startup_maintain:
                    _HANG_GUARD_STARTUP_MAINTAIN.add(pid)
                # Hot-toggle 副本跳过剧情 while hang already running.
                try:
                    prev_on = bool(getattr(prev, "skip_dungeon_story", False)) if prev else False
                    now_on = bool(dungeon_mode and getattr(cfg, "skip_dungeon_story", False))
                    if now_on and not prev_on:
                        schedule_skip_dungeon_story_async(
                            active_session,
                            hwnd=int(getattr(active_session, "hwnd", 0) or 0),
                            delay_s=0.35,
                            arm_only=True,
                            log=log,
                        )
                    elif prev_on and not now_on:
                        from app.core.xajh_bridge import ensure_bridge

                        br = ensure_bridge(
                            pid,
                            log=log,
                            inject_if_needed=True,
                            hwnd=int(getattr(active_session, "hwnd", 0) or 0) or None,
                            force_reinject=False,
                        )
                        if br is not None:
                            br.cg_skip(mode=0, timeout_ms=1200)
                except Exception as e:
                    log(f"hang guard: plot skip hot-toggle err {e}")
                return {
                    "ok": True,
                    "pid": pid,
                    "interval_s": float(
                        interval_s if interval_s is not None else HANG_GUARD_TICK_S
                    ),
                    "job_id": HANG_GUARD_JOB_ID,
                    "reused": True,
                }

    token = object()
    guard_session = session
    guard_session_owned = False
    with _HANG_GUARD_CFG_LOCK:
        reusable_session = _HANG_GUARD_SESSION.get(pid)
        reusable_owned = pid in _HANG_GUARD_SESSION_OWNED
    if reusable_owned and int(getattr(reusable_session, "pid", 0) or 0) == pid:
        guard_session = reusable_session
        guard_session_owned = True
    # Button handlers close their short-lived attach after start_hang returns.
    # Keep a separate handle for the periodic guard so its pid/pm stay valid.
    elif isinstance(session, GameAttachSession):
        try:
            guard_session = GameAttachSession(log=lambda _m: None)
            guard_session.attach(pid)
            guard_session.hwnd = int(getattr(session, "hwnd", 0) or 0)
            guard_session_owned = True
        except Exception as e:
            try:
                guard_session.close()
            except Exception:
                pass
            log(f"hang guard: attach failed pid={pid} err={e}")
            return {"ok": False, "pid": pid, "error": f"guard_attach_failed: {e}"}
    old_session = None
    old_owned = False
    with _HANG_GUARD_CFG_LOCK:
        old_session = _HANG_GUARD_SESSION.get(pid)
        old_owned = pid in _HANG_GUARD_SESSION_OWNED
        _HANG_GUARD_CFG[pid] = cfg
        _HANG_GUARD_TOKEN[pid] = token
        _HANG_GUARD_SESSION[pid] = guard_session
        if guard_session_owned:
            _HANG_GUARD_SESSION_OWNED.add(pid)
        else:
            _HANG_GUARD_SESSION_OWNED.discard(pid)
        _HANG_GUARD_DEATH_SEEN_TS.pop(pid, None)
        _HANG_GUARD_DEATH_MAINTAIN.discard(pid)
        _HANG_GUARD_LAST_SCENE.pop(pid, None)
        # 标记首次场景检查：第一个稳定 tick 触发副本跳过剧情 / 武尊开怪等
        _HANG_GUARD_SCENE_REARM.add(pid)
        _HANG_GUARD_REARM_RETRY.pop(pid, None)
        if startup_maintain:
            _HANG_GUARD_STARTUP_MAINTAIN.add(pid)
    if old_owned and old_session is not None and old_session is not guard_session:
        try:
            old_session.close()
        except Exception:
            pass

    # Cadence is hang business policy; dispatch only hosts the timer.
    iv = float(interval_s if interval_s is not None else HANG_GUARD_TICK_S)

    def _tick() -> None:
        # cancel_periodic is cooperative.  A callback already selected by the
        # old timer must not do one more poll/maintenance after stop or replace.
        if not _hang_guard_current(pid, token):
            return
        if int(getattr(guard_session, "pid", 0) or 0) != pid:
            log(f"hang guard: session invalid, cancelled pid={pid}")
            stop_hang_guard(pid, log=lambda _m: None)
            return
        cfg_now = _hang_guard_cfg(pid)
        with _HANG_GUARD_CFG_LOCK:
            startup_due = int(pid) in _HANG_GUARD_STARTUP_MAINTAIN
            death_due = int(pid) in _HANG_GUARD_DEATH_MAINTAIN
            scene_rearm_due = int(pid) in _HANG_GUARD_SCENE_REARM

        # Scene fence: map load / 切图期间禁止维护与丸子重装，避免 CRT/开挂崩溃。
        scene_stable = True
        scene_id_now = 0
        try:
            from app.core.remote_runtime import (
                get_pid_scene_snapshot,
                is_pid_scene_snapshot_stable,
            )

            snap = get_pid_scene_snapshot(pid)
            scene_id_now = int(snap.get("scene_id") or 0) if snap else 0
            if snap:
                scene_stable = bool(is_pid_scene_snapshot_stable(pid))
            with _HANG_GUARD_CFG_LOCK:
                prev_sid = int(_HANG_GUARD_LAST_SCENE.get(pid) or 0)
                if scene_id_now > 0 and prev_sid > 0 and scene_id_now != prev_sid:
                    _HANG_GUARD_SCENE_REARM.add(pid)
                    _HANG_GUARD_REARM_RETRY.pop(pid, None)
                    scene_rearm_due = True
                    log(
                        f"hang guard: scene change {prev_sid}->{scene_id_now}, "
                        "queue rearm after settle"
                    )
                    if _wanzi_direct_enabled(cfg_now):
                        try:
                            stop_wanzi_packet_hang(
                                guard_session, release=False, log=log
                            )
                        except Exception as e:
                            log(f"hang guard: wanzi scene-change pause err {e}")
                if scene_id_now > 0:
                    _HANG_GUARD_LAST_SCENE[pid] = scene_id_now
        except Exception:
            scene_stable = True

        if not scene_stable:
            # Quiesce direct packets during map loading; the owner remains
            # reserved and is re-started only after the settle fence below.
            if _wanzi_direct_enabled(cfg_now):
                try:
                    stop_wanzi_packet_hang(guard_session, release=False, log=log)
                except Exception as e:
                    log(f"hang guard: wanzi scene pause err {e}")
            # Still track death edge cheaply is skipped; wait for settle.
            return

        # After map change settles: optional plot skip + wanzi re-prepare.
        if scene_rearm_due:
            # 过图重入洞（事件驱动，无长阻塞）：每 tick 尝试一次本地
            # StartAutoPlay 重初始化——场景门未 settle 时毫秒级失败，
            # 下个 tick 自然重试；成功（running 保持 True）才入队出清。
            # 连续失败超过 _SCENE_REARM_RETRY_MAX 视为放弃，交由
            # 看门狗的 Alert 粘滞告警兜底。
            # 跳剧情/开怪/丸子是场景边动作，仅在终态（成功或放弃）执行
            # 一次；rearm 未就绪期间不重放，避免重复排异步任务。
            rearm_pending = False
            try:
                known_dungeon, scene_id, reason = _stable_known_dungeon_scene(pid)
                rearm_ok = True
                if dungeon_mode:
                    rearm_ok = False
                    try:
                        from app.core.activity_auto import (
                            resolve_cec_autoplay_rpm,
                            start_autoplay_force,
                        )

                        r = start_autoplay_force(
                            guard_session, send_packet=False, force=True, log=log
                        )
                        mem2 = resolve_cec_autoplay_rpm(guard_session)
                        rearm_ok = (
                            bool(r.get("ok"))
                            and mem2.get("running") is True
                        )
                        log(
                            "hang guard: scene rearm autoplay re-init "
                            f"ok={r.get('ok')} "
                            f"ret=0x{(r.get('ret') or 0) & 0xFFFFFFFF:X} "
                            f"running={mem2.get('running')} "
                            f"retry={_HANG_GUARD_REARM_RETRY.get(pid, 0)}"
                        )
                        if mem2.get("running") is not True:
                            # 重初始化把挂机弄停：立即重发开启封包恢复，
                            # 本轮视为失败入队重试。
                            _send_hang_control_packet(
                                guard_session,
                                HANG_START_PACKET,
                                action="开启(恢复)",
                                log=log,
                            )
                            rearm_ok = False
                    except Exception as e:
                        log(f"hang guard: scene rearm re-init 尚未就绪: {str(e)[:70]}")
                        rearm_ok = False
                    if rearm_ok:
                        _HANG_GUARD_REARM_RETRY.pop(pid, None)
                    else:
                        n = _HANG_GUARD_REARM_RETRY.get(pid, 0) + 1
                        _HANG_GUARD_REARM_RETRY[pid] = n
                        if n >= _SCENE_REARM_RETRY_MAX:
                            _HANG_GUARD_REARM_RETRY.pop(pid, None)
                            rearm_ok = True  # 放弃重试，交由 Alert 告警兜底
                            log(
                                "hang guard: scene rearm re-init 放弃（重试上限），"
                                "由 Alert 粘滞告警兜底"
                            )
                        else:
                            rearm_pending = True  # 下个 tick 只重试重初始化
                if not rearm_pending:
                    if dungeon_mode and bool(getattr(cfg_now, "skip_dungeon_story", False)) and known_dungeon:
                        # Scene-edge event only — queue off the guard tick thread.
                        schedule_skip_dungeon_story_async(
                            guard_session,
                            hwnd=int(getattr(guard_session, "hwnd", 0) or 0),
                            delay_s=float(HANG_PLOT_SKIP_DELAY_S),
                            log=log,
                        )
                    elif bool(getattr(cfg_now, "skip_dungeon_story", False)):
                        log(
                            "hang guard: plot skip not rearmed "
                            f"scene={scene_id or '-'} reason={reason}"
                        )
                        _disable_dungeon_story_hook(guard_session, log=log)
                    # 武尊自动开怪：仅在武尊场景（1255/1541）启动，离开时停止
                    if dungeon_mode and bool(getattr(cfg_now, "auto_open_monster", False)):
                        from app.core.wuzun_open_monster import (
                            WUZUN_SCENE_IDS,
                            start_wuzun_open_monster,
                            stop_wuzun_open_monster,
                        )

                        is_wuzun = int(scene_id or 0) in WUZUN_SCENE_IDS
                        if is_wuzun:
                            start_wuzun_open_monster(
                                pid,
                                int(getattr(guard_session, "hwnd", 0) or 0),
                                int(getattr(cfg_now, "open_monster_rows", 0) or 0),
                                log=log,
                            )
                        else:
                            stop_wuzun_open_monster(pid, log=log)
                    if _wanzi_direct_enabled(cfg_now):
                        # Scene already stable here; resume the direct sender.
                        # Do not write recovery slots or sleep on this thread.
                        start_wanzi_packet_hang(guard_session, cfg_now, log=log)
            except Exception as e:
                log(f"hang guard: scene rearm err {e}")
            finally:
                with _HANG_GUARD_CFG_LOCK:
                    # rearm 未就绪则保留标记，下个 tick 继续重试（见上方注释）。
                    if not rearm_pending:
                        _HANG_GUARD_SCENE_REARM.discard(pid)
                try:
                    from app.core.dungeon_fight_kick import reset_state

                    reset_state(pid)
                except Exception:
                    pass

        # Plot skip is event-driven only (scene rearm / hang start). No tick scan.

        # Cheap roll list via cache; producer is pure RPM (no CRT).
        def _poll_rolls():
            return _iter_active_loot_rolls(guard_session, log=lambda _m: None)

        try:
            pending = disp.read_cached(
                pid,
                roll_cache_key(),
                _poll_rolls,
                max_age=HANG_ROLL_POLL_CACHE_S,
                ttl_s=HANG_ROLL_POLL_CACHE_S,
                priority=Priority.P2,
                kind=OpKind.RPM,
                op="hang_roll_poll",
            )
        except RemoteBlockedError as e:
            log(f"hang guard: blocked {e}")
            return
        except Exception as e:
            log(f"hang guard: roll poll err {e}")
            pending = []

        n_pending = len(pending or [])

        # 副本挂机两个独立看门狗（守护 tick 每 1s 各跑一次，异常互不影响）：
        # 功能一：跟随打怪（instance_follow_fight）机器卡诊断——StateAlert ∧
        #   无目标 ∧ 静止≥10s 才提示，队长/队员分文案；独立于忽略怪开关。
        # 功能二：「忽略怪放行」看门狗（卡怪专用）——仅当「忽略副本卡怪」
        #   已武装且 AOI 内存在名单忽略怪时，才提交该忽略怪（≤30m）。
        #   忽略怪不在场时待机，绝不抢目标干扰正常挂机/跟随。
        if dungeon_mode:
            try:
                from app.core.follow_fight_watchdog import watch_follow_fight_stuck

                watch_follow_fight_stuck(guard_session, log=log)
            except Exception as e:
                log(f"hang guard: follow fight stuck watch err {e}")
            try:
                from app.core.dungeon_fight_kick import maybe_kick

                maybe_kick(guard_session, log=log)
            except Exception as e:
                log(f"hang guard: dungeon fight kick err {e}")

        # Maintain only when work is due:
        # - pickup off + pending rolls -> abandon wave
        # - pickup on + dead + pending -> Need wave
        # - repair/vitality long interval due
        #
        # Load model (long-running guard):
        # - every tick: RPM roll poll only (cached 0.5s) — no CRT if empty
        # - death: reuse the short-lived state; CRT only when stale/unknown and pending

        # - Need/Pass CRT: burst<=24, wave CD 0.35s, via hang_action_guard
        # - repair/vitality: long-interval due, plus one pass on each death edge
        need_maintain = bool(startup_due)
        dead_now = None
        watch_death = bool(cfg_now.auto_repair or cfg_now.auto_vitality)
        if watch_death or (bool(cfg_now.enable_pickup) and n_pending > 0):
            # Reuse a fresh state sample; only CRT when it is stale/unknown.
            dead_now = get_hang_dead_state(pid)
            if dead_now is None:
                try:
                    dead_now = refresh_hang_dead_state(
                        guard_session, log=lambda _m: None, force=False
                    )
                except Exception:
                    dead_now = get_hang_dead_state(pid)
        if watch_death and _note_hang_guard_death_transition(pid):
            death_due = True
            log("hang guard: death detected, queued repair/vitality check after revival")
        # Record durability loss on death, but do not use repair/vitality items
        # until the game explicitly reports that the character is alive again.
        death_rv_due = bool(death_due and dead_now is False)
        if death_rv_due:
            need_maintain = True
        # Loot work only when wave cooldown free — avoid 1Hz maintain spam.
        if not bool(cfg_now.enable_pickup) and n_pending > 0:
            if hang_cooldown_remain(
                guard_session, "loot_abandon", cooldown_s=float(HANG_LOOT_COOLDOWN_S)
            ) <= 0:
                need_maintain = True
        if bool(cfg_now.enable_pickup) and dead_now is True and n_pending > 0:
            if hang_cooldown_remain(
                guard_session, "loot_need", cooldown_s=float(HANG_LOOT_COOLDOWN_S)
            ) <= 0:
                need_maintain = True
        if bool(cfg_now.auto_repair):
            if _hang_check_due(
                guard_session,
                "repair_check",
                interval_s=float(HANG_REPAIR_CHECK_INTERVAL_S),
                force=False,
            )[0]:
                need_maintain = True
        if bool(cfg_now.auto_vitality):
            if _hang_check_due(
                guard_session,
                "vitality_check",
                interval_s=float(HANG_VITALITY_CHECK_INTERVAL_S),
                force=False,
            )[0]:
                need_maintain = True

        if not need_maintain:
            return
        if not _hang_guard_current(pid, token):
            return
        try:
            # Invalidate roll cache before abandon wave so list is fresh.
            if n_pending > 0:
                disp.invalidate(pid, roll_cache_key())
            ret = hang_maintain_once(
                guard_session,
                cfg_now,
                force_rv=bool(startup_due or death_rv_due),
                log=log,
            )
            # Keep startup/death checks queued if the guard was busy. A real
            # check, including a harmless "above threshold" result, consumes it.
            if (startup_due or death_rv_due) and not bool(ret.get("busy")):
                with _HANG_GUARD_CFG_LOCK:
                    if startup_due:
                        _HANG_GUARD_STARTUP_MAINTAIN.discard(pid)
                    if death_rv_due:
                        _HANG_GUARD_DEATH_MAINTAIN.discard(pid)
        except RemoteBlockedError as e:
            log(f"hang guard: maintain blocked {e}")
        except Exception as e:
            log(f"hang guard: maintain err {e}")

    disp.schedule_periodic(
        pid,
        HANG_GUARD_JOB_ID,
        iv,
        _tick,
        priority=Priority.P2,
        kind=OpKind.LOCAL,
        replace=True,
    )
    # No Python ignore-release timer: native candidate filtering owns dungeon
    # card-monster suppression before selection.
    log(f"hang guard: scheduled pid={pid} interval={iv:.2f}s")
    return {"ok": True, "pid": pid, "interval_s": iv, "job_id": HANG_GUARD_JOB_ID}


def stop_hang_guard(
    session: GameAttachSession | int | None = None,
    *,
    log: LogFn | None = None,
    disarm_target_guard: bool = True,
) -> dict:
    """Cancel hang guard periodic + drop roll cache. @author by ak"""
    log = log or (lambda _m: None)
    pid = _hang_pid(session)
    if not pid:
        return {"ok": False, "error": "no_pid"}
    from app.core.safe_dispatch import get_dispatch, roll_cache_key

    get_dispatch().cancel_periodic(pid, HANG_GUARD_JOB_ID)
    get_dispatch().invalidate(pid, roll_cache_key())
    if disarm_target_guard:
        try:
            _disarm_dungeon_target_guard(session or pid, log=log)
        except Exception as e:
            log(f"hang guard: target guard disarm err {e}")
    guard_session = None
    guard_owned = False
    with _HANG_GUARD_CFG_LOCK:
        _HANG_GUARD_CFG.pop(int(pid), None)
        _HANG_GUARD_STARTUP_MAINTAIN.discard(int(pid))
        _HANG_GUARD_DEATH_MAINTAIN.discard(int(pid))
        _HANG_GUARD_DEATH_SEEN_TS.pop(int(pid), None)
        _HANG_GUARD_TOKEN.pop(int(pid), None)
        _HANG_GUARD_LAST_SCENE.pop(int(pid), None)
        _HANG_GUARD_SCENE_REARM.discard(int(pid))
        guard_session = _HANG_GUARD_SESSION.pop(int(pid), None)
        guard_owned = int(pid) in _HANG_GUARD_SESSION_OWNED
        _HANG_GUARD_SESSION_OWNED.discard(int(pid))
    if guard_owned and guard_session is not None:
        try:
            guard_session.close()
        except Exception:
            pass
    log(f"hang guard: cancelled pid={pid}")
    return {"ok": True, "pid": int(pid)}




__all__ = [
    "HangConfig",
    "HangLiveState",
    "DEFAULT_HANG_MODE",
    "DEFAULT_HANG_RADIUS",
    "DEFAULT_ENABLE_PICKUP",
    "DEFAULT_EMPTY_SKILL",
    "DEFAULT_WANZI_HANG",
    "DEFAULT_WANZI_NEIGONG_HANG",
    "DEFAULT_WANZI_WAIGONG_HANG",
    "DEFAULT_WANZI_INTERVAL_MS",
    "DEFAULT_YOUFENG_HANG",
    "DEFAULT_JIANGLONG_HANG",
    "DEFAULT_SKIP_DUNGEON_STORY",
    "YOUFENG_SKILL_SLOT",
    "DEFAULT_AUTO_REPAIR",
    "DEFAULT_REPAIR_BELOW_PCT",
    "DEFAULT_AUTO_VITALITY",
    "DEFAULT_VITALITY_BELOW_PCT",
    "REPAIR_BOX_TID",
    "REPAIR_CONFIRM_DLG",
    "HANG_REPAIR_CHECK_INTERVAL_S",
    "HANG_VITALITY_CHECK_INTERVAL_S",
    "HANG_REPAIR_COOLDOWN_S",
    "HANG_VITALITY_COOLDOWN_S",
    "HANG_RV_BUSY_BACKOFF_S",
    "HANG_LOOT_COOLDOWN_S",
    "HANG_LOOT_POST_CLEAR_COOLDOWN_S",
    "HANG_LOOT_INTER_PKT_S",
    "HANG_LOOT_MAX_PER_BURST",
    "LOOT_ROLL_ID1_FLAG",
    "VITALITY_RE_READY",
    "VITALITY_RE_NOTE",
    "normalize_hang_char_id",
    "normalize_hang_role",
    "load_hang_prefs_store",
    "save_hang_prefs_store",
    "load_hang_prefs",
    "save_hang_prefs",
    "get_hang_config",
    "write_hang_config_to_settings",
    "save_hang_disk_from_config",
    "read_hang_live",
    "format_hang_live_line",
    "prepare_wanzi_hang",
    "WANZI_KIND_NEIGONG",
    "WANZI_KIND_WAIGONG",
    "wanzi_kind_from_config",
    "WANZI_PACKET_OWNER_HANG",
    "WANZI_PACKET_OWNER_MANUAL",
    "get_wanzi_packet_state",
    "start_wanzi_packet_hang",
    "stop_wanzi_packet_hang",
    "start_wanzi_packet_manual",
    "stop_wanzi_packet_manual",
    "skip_dungeon_story_once",
    "schedule_skip_dungeon_story_async",
    "prepare_youfeng_hang",
    "apply_hang_prepare",
    "apply_hang_switch",
    "YOUFENG_HOOK_OWNER_HANG",
    "YOUFENG_HOOK_OWNER_MANUAL",
    "get_youfeng_key_hook_state",
    "start_youfeng_key_hook",
    "stop_youfeng_key_hook",
    "start_youfeng_hang",
    "stop_youfeng_hang",
    "start_hang",
    "stop_hang",
    "send_hang_start_packet",
    "send_hang_stop_packet",
    "hang_maintain_once",
    "start_hang_guard",
    "update_hang_guard_config",
    "stop_hang_guard",
    "apply_party_auto_need",
    "abandon_all_loot_rolls",
    "get_hang_dead_state",
    "refresh_hang_dead_state",
    "need_all_loot_rolls",
    "read_equipment_durability_pct",
    "read_vitality_pct",
    "is_hang_action_busy",
    "hang_action_owner",
    "hang_action_guard",
    "hang_cooldown_remain",
    "probe_game_call_busy",
]
