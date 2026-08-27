# -*- coding: utf-8 -*-
"""Timed dungeon-task queue: accept -> map instance -> ActivityRunner once -> complete.

Also hosts the 自定义任务 plan: versioned per-role schedule config, custom task
definitions, queue ordering, state audit and the pause/resume-capable runner.

@author by ak
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from math import ceil, sqrt
from pathlib import Path
from typing import Any, Callable

from app.core.activity_auto import (
    DEFAULT_INSTANCE_ID,
    QIEGAO_INSTANCE_ID,
    ActivityConfig,
    ActivityRunner,
    ActivityStepEvent,
    instance_label,
    read_flourish_points,
)
from app.core.runner import RunnerLifecycle, interruptible_sleep
from app.core.automove import read_scene_position
from app.core.resident_targets import read_nearest_resident_target, read_resident_targets
from app.core.task_api import (
    TaskInfo,
    classify_task_portal_kind,
    accept_task_routed,
    complete_task_routed,
    list_accepted_task_ids,
    list_accepted_tasks,
    list_accepted_tasks_light,
    read_task_npc_tids,
    list_portal_npc_candidates,
    list_nearby_npcs,
    pathfind_task,
    pathfind_to_clue,
    resolve_dungeon_tier,
    use_task_portal_npc,
    send_dungeon_portal_packets,
    task_can_finish,
)

LogFn = Callable[[str], None]
EventFn = Callable[[dict], None]

# settings keys
SETTING_QUEUE = "task_schedule_queue"
SETTING_HOUR = "task_schedule_hour"
SETTING_MINUTE = "task_schedule_minute"
SETTING_LAST_RUN = "task_schedule_last_run_date"

DEFAULT_SCHEDULE_HOUR = 10
DEFAULT_SCHEDULE_MINUTE = 0

# Ordered keyword rules: first match wins.
# mode_hint: dungeon | qiegao
_INSTANCE_RULES: tuple[tuple[tuple[str, ...], int, str, str], ...] = (
    # qiegao / super first (more specific 140)
    (("切糕", "140副本", "140 副本任务", "副本任务一"), QIEGAO_INSTANCE_ID, "qiegao", "超级副本"),
    (("超级", "嵩山之巅", "嵩山巅"), QIEGAO_INSTANCE_ID, "dungeon", "超级副本"),
    (("高级", "嵩山之路"), 6283, "dungeon", "高级副本"),
    (("初级", "镇嵩", "镇嵩宝塔"), 3635, "dungeon", "初级副本"),
    (("福利", "云上", "云上山崖"), 3048, "dungeon", "福利副本"),
    (("组队", "梅庄"), 2638, "dungeon", "组队副本"),
    (("绿竹", "幻想乡"), DEFAULT_INSTANCE_ID, "dungeon", "绿竹幻想乡"),
)


def resolve_instance_for_task(task: TaskInfo | dict | None) -> dict:
    """
    Map a task row/name to an instance enter target.

    Returns dict:
      ok, instance_id, mode, alias, note, task_id, name
    ok=False when no keyword maps to a known instance.
    """
    row = task.to_dict() if isinstance(task, TaskInfo) else dict(task or {})
    tid = int(row.get("task_id") or 0) & 0xFFFFFFFF
    name = str(row.get("name") or "").strip()
    npc = str(row.get("npc_name") or "").strip()
    blob = f"{name} {npc}"
    out: dict[str, Any] = {
        "ok": False,
        "task_id": tid,
        "name": name,
        "instance_id": 0,
        "mode": "dungeon",
        "alias": "",
        "note": "",
    }
    if not tid:
        out["note"] = "缺少 task_id"
        return out
    for keys, iid, mode, alias in _INSTANCE_RULES:
        if any(k and k in blob for k in keys):
            out["ok"] = True
            out["instance_id"] = int(iid)
            out["mode"] = str(mode)
            out["alias"] = alias
            out["note"] = f"mapped {alias} id={iid} mode={mode}"
            return out
    out["note"] = "无法映射副本实例"
    return out


def normalize_queue_item(
    raw: dict | None, *, custom_ids: dict | None = None
) -> dict | None:
    """Normalize one queue item; None if invalid.

    Custom items (source=custom) are validated against the built-in definitions
    and dropped when the definition is currently disabled. Legacy accepted-task
    items keep the historical resolve-by-keyword behavior.
    """
    row = dict(raw or {})
    if str(row.get("source") or "").strip().lower() == "custom":
        defn_id = str(row.get("definition_id") or "").strip()
        defn = get_custom_definition(defn_id, custom_ids)
        if defn is None:
            return None
        kind = str(defn.get("kind") or row.get("kind") or "activity")
        # Routine definitions are semantic presets, not user-editable snapshots.
        # Always take their task/map/target identity from the current definition;
        # old queue files must not turn one daily into another.
        if kind == "routine":
            tid = int(defn.get("task_id") or 0) & 0xFFFFFFFF
            iid = int(defn.get("instance_id") or 0)
        else:
            # Prefer stored item ids (queue snapshot); fall back to current def ids.
            tid = int(row.get("task_id") or defn.get("task_id") or 0) & 0xFFFFFFFF
            iid = int(row.get("instance_id") or defn.get("instance_id") or 0)
        if kind == "dungeon":
            if not tid or not iid:
                return None
        else:
            iid = iid or DEFAULT_INSTANCE_ID
        mode = str(row.get("mode") or defn.get("mode") or "dungeon").strip().lower()
        if mode not in ("dungeon", "qiegao"):
            mode = "dungeon"
        return {
            "source": "custom",
            "definition_id": defn_id,
            "name": str(row.get("name") or defn.get("name") or defn_id),
            "kind": kind,
            "task_id": tid,
            "instance_id": iid,
            "mode": mode,
            **{
                key: (
                    defn.get(key)
                    if kind == "routine"
                    else row.get(key, defn.get(key))
                )
                for key in ("target_scene_id", "target_tid", "target_name")
                if key in defn or key in row
            },
            "enabled": True,
        }
    tid = int(row.get("task_id") or 0) & 0xFFFFFFFF
    if not tid:
        return None
    iid = int(row.get("instance_id") or 0)
    mode = str(row.get("mode") or "dungeon").strip().lower() or "dungeon"
    if mode not in ("dungeon", "qiegao"):
        mode = "dungeon"
    name = str(row.get("name") or "").strip() or f"任务{tid}"
    alias = str(row.get("alias") or "").strip()
    if not iid:
        resolved = resolve_instance_for_task({"task_id": tid, "name": name})
        if not resolved.get("ok"):
            return None
        iid = int(resolved["instance_id"])
        mode = str(resolved.get("mode") or mode)
        alias = alias or str(resolved.get("alias") or "")
    return {
        "task_id": tid,
        "name": name,
        "instance_id": iid,
        "mode": mode,
        "alias": alias,
    }


def _settings_custom_ids(settings: dict | None, custom_ids: dict | None) -> dict | None:
    """Resolve per-role custom ids from explicit arg or settings. @author by ak"""
    if custom_ids is not None:
        return custom_ids
    s = settings if isinstance(settings, dict) else {}
    cids = s.get("schedule_custom_ids")
    return dict(cids) if isinstance(cids, dict) else None


def load_schedule_queue(
    settings: dict | None, *, custom_ids: dict | None = None
) -> list[dict]:
    s = settings if isinstance(settings, dict) else {}
    raw = s.get(SETTING_QUEUE) or []
    if not isinstance(raw, list):
        return []
    cids = _settings_custom_ids(settings, custom_ids)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        n = normalize_queue_item(item if isinstance(item, dict) else None, custom_ids=cids)
        if n is None:
            continue
        key = queue_item_key(n)
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def save_schedule_queue(
    settings: dict | None,
    queue: list[dict],
    *,
    custom_ids: dict | None = None,
) -> list[dict]:
    s = settings if isinstance(settings, dict) else {}
    cids = _settings_custom_ids(settings, custom_ids)
    cleaned: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in queue or []:
        n = normalize_queue_item(
            item if isinstance(item, dict) else None, custom_ids=cids
        )
        if n is None:
            continue
        key = queue_item_key(n)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(n)
    s[SETTING_QUEUE] = cleaned
    return cleaned


def queue_item_label(item: dict) -> str:
    """UI line for schedule list."""
    if str(item.get("source") or "").strip().lower() == "custom":
        return f"[自定义] {item.get('name') or item.get('definition_id')}"
    alias = str(item.get("alias") or "").strip()
    if not alias:
        try:
            alias = instance_label(int(item.get("instance_id") or 0)).split(" Lv")[0]
        except Exception:
            alias = f"本{item.get('instance_id')}"
    name = str(item.get("name") or "").strip() or f"任务{item.get('task_id')}"
    tid = int(item.get("task_id") or 0)
    return f"[{alias}] {name} #{tid}"


def get_schedule_hm(settings: dict | None) -> tuple[int, int]:
    s = settings if isinstance(settings, dict) else {}
    try:
        h = int(s.get(SETTING_HOUR, DEFAULT_SCHEDULE_HOUR))
    except (TypeError, ValueError):
        h = DEFAULT_SCHEDULE_HOUR
    try:
        m = int(s.get(SETTING_MINUTE, DEFAULT_SCHEDULE_MINUTE))
    except (TypeError, ValueError):
        m = DEFAULT_SCHEDULE_MINUTE
    h = max(0, min(23, h))
    m = max(0, min(59, m))
    return h, m


def set_schedule_hm(settings: dict | None, hour: int, minute: int) -> tuple[int, int]:
    s = settings if isinstance(settings, dict) else {}
    h = max(0, min(23, int(hour)))
    m = max(0, min(59, int(minute)))
    s[SETTING_HOUR] = h
    s[SETTING_MINUTE] = m
    return h, m


def schedule_should_fire(settings: dict | None, *, now: datetime | None = None) -> bool:
    """True when local clock hits configured HH:MM and not already fired today."""
    s = settings if isinstance(settings, dict) else {}
    now = now or datetime.now()
    h, m = get_schedule_hm(s)
    if now.hour != h or now.minute != m:
        return False
    today = now.date().isoformat()
    last = str(s.get(SETTING_LAST_RUN) or "").strip()
    return last != today


def mark_schedule_fired(settings: dict | None, *, when: date | None = None) -> str:
    s = settings if isinstance(settings, dict) else {}
    d = (when or date.today()).isoformat()
    s[SETTING_LAST_RUN] = d
    return d


# ============================================================
# 自定义任务定义（系统维护只读目录）
# ============================================================
CUSTOM_DEF_ID_ACTIVITY = "daily_activity"
CUSTOM_DEF_ID_DAILY_BADAO = "daily_badao_10021"
CUSTOM_DEF_ID_DAILY_LONGAOTIAN = "daily_longaotian_10010"
CUSTOM_DEF_ID_DAILY_YUCANGHAI = "daily_yucanghai_10011"
CUSTOM_DEF_ID_DAILY_DONGFANG_10006 = "daily_dongfang_10006"
CUSTOM_DEF_DUNGEON_IDS = (
    "daily_dungeon_1",
    "daily_dungeon_2",
    "daily_dungeon_3",
    "daily_dungeon_4",
)
CUSTOM_ACTIVITY_TARGET_POINTS = 70

# 内置 日常副本 task_id → instance_id 对照（实机确认 + instance_names.json 匹配）。
# 用于日常副本定义缺 ID 时自动补全；仍可用 update_custom_definition_ids 按角色覆盖。
# mode: 140副本任务一(切糕/沙漠古镇) 走 qiegao 挂机流程，其余普通 dungeon 进本。
CUSTOM_TASK_INSTANCE_MAP: dict[str, dict] = {
    "daily_task_140_1": {
        "name": "140副本任务一",
        "task_id": 10028,
        "instance_id": 6230,  # 【沙漠古镇】
        "mode": "dungeon",
    },
    "daily_task_140_2": {
        "name": "140副本任务二",
        "task_id": 10027,
        "instance_id": 1928,  # 【蝎王魔窟】
        "mode": "dungeon",
    },
    "daily_task_group": {
        "name": "组队本",
        "task_id": 10020,
        "instance_id": 2638,  # 【梅庄外围】
        "mode": "dungeon",
    },
    "daily_task_wuzun": {
        "name": "武尊堂",
        "task_id": 10025,
        "instance_id": 7368,  # 武尊堂 Lv70
        "mode": "dungeon",
    },
}

# 日常副本槽位 → 内置任务 key（顺序即展示顺序；名称用具体任务名）。
_DUNGEON_SLOT_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("daily_dungeon_1", "140副本任务一", "daily_task_140_1"),
    ("daily_dungeon_2", "140副本任务二", "daily_task_140_2"),
    ("daily_dungeon_3", "组队本", "daily_task_group"),
    ("daily_dungeon_4", "武尊堂", "daily_task_wuzun"),
)


def default_custom_definitions() -> list[dict]:
    """System custom tasks: 活跃 + four daily dungeons (pre-filled from built-in map). @author by ak"""
    out: list[dict] = [
        {
            "definition_id": CUSTOM_DEF_ID_ACTIVITY,
            "name": "刷满活跃并领取全部宝箱",
            "kind": "activity",
            "task_id": 0,
            "instance_id": DEFAULT_INSTANCE_ID,
            "enabled": True,
            "target_points": CUSTOM_ACTIVITY_TARGET_POINTS,
        }
    ]
    out.extend([
        {"definition_id": CUSTOM_DEF_ID_DAILY_BADAO, "name": "霸刀日常", "kind": "routine", "task_id": 10021, "target_scene_id": 72, "target_tid": 101042, "target_name": "上官霸刀", "enabled": True},
        {"definition_id": CUSTOM_DEF_ID_DAILY_LONGAOTIAN, "name": "龙傲天日常", "kind": "routine", "task_id": 10010, "target_scene_id": 2036, "target_tid": 0, "target_name": "", "enabled": True},
        {"definition_id": CUSTOM_DEF_ID_DAILY_YUCANGHAI, "name": "余沧海日常", "kind": "routine", "task_id": 10011, "target_scene_id": 2030, "target_tid": 101013, "target_name": "余沧海", "enabled": True},
        {"definition_id": CUSTOM_DEF_ID_DAILY_DONGFANG_10006, "name": "东方不败日常", "kind": "routine", "task_id": 10006, "target_scene_id": 2034, "target_tid": 100072, "target_name": "东方不败", "enabled": True},
    ])
    for defn_id, name, map_key in _DUNGEON_SLOT_DEFAULTS:
        meta = CUSTOM_TASK_INSTANCE_MAP.get(map_key) or {}
        out.append(
            {
                "definition_id": defn_id,
                "name": name,
                "kind": "dungeon",
                "task_id": int(meta.get("task_id") or 0),
                "instance_id": int(meta.get("instance_id") or 0),
                "mode": str(meta.get("mode") or "dungeon"),
                "enabled": True,
            }
        )
    return out


def _merge_custom_ids(defn: dict, custom_ids: dict | None) -> dict:
    """Merge per-role configured task/instance ids into a definition row. @author by ak"""
    out = dict(defn or {})
    cfg = (custom_ids or {}).get(str(out.get("definition_id") or ""))
    if isinstance(cfg, dict):
        try:
            tid = int(cfg.get("task_id") or 0) & 0xFFFFFFFF
            if tid:
                out["task_id"] = tid
        except (TypeError, ValueError):
            pass
        try:
            iid = int(cfg.get("instance_id") or 0)
            if iid:
                out["instance_id"] = iid
        except (TypeError, ValueError):
            pass
    return out


def list_custom_definitions(custom_ids: dict | None = None) -> list[dict]:
    """Custom definitions with per-role ids merged; dungeons need task+instance id. @author by ak"""
    out: list[dict] = []
    for defn in default_custom_definitions():
        row = _merge_custom_ids(defn, custom_ids)
        row["enabled"] = custom_definition_enabled(row)
        out.append(row)
    return out


def get_custom_definition(
    definition_id: str, custom_ids: dict | None = None
) -> dict | None:
    """One merged custom definition by id; None when unknown. @author by ak"""
    for defn in list_custom_definitions(custom_ids):
        if str(defn.get("definition_id") or "") == str(definition_id or ""):
            return dict(defn)
    return None


def custom_definition_enabled(defn: dict | None) -> bool:
    """Dungeon custom tasks require both task_id and instance_id. @author by ak"""
    row = defn or {}
    kind = str(row.get("kind") or "activity")
    if kind in ("activity", "routine"):
        return True
    return bool(int(row.get("task_id") or 0)) and bool(int(row.get("instance_id") or 0))


def custom_definition_label(defn: dict | None) -> str:
    """UI label; dungeons without ids show 待配置, mapped dungeons show task name. @author by ak"""
    row = dict(defn or {})
    name = str(row.get("name") or row.get("definition_id") or "")
    if not custom_definition_enabled(row):
        return f"{name} · 待配置"
    try:
        tid = int(row.get("task_id") or 0)
    except (TypeError, ValueError):
        tid = 0
    for meta in CUSTOM_TASK_INSTANCE_MAP.values():
        if int(meta.get("task_id") or 0) == tid:
            task_name = str(meta.get("name") or "").strip()
            if task_name and task_name != name:
                return f"{name} · {task_name}"
            break
    return name


def custom_queue_item(defn: dict | None) -> dict | None:
    """Build a queue item for one definition; None when disabled. @author by ak"""
    row = dict(defn or {})
    if not custom_definition_enabled(row):
        return None
    return {
        "source": "custom",
        "definition_id": str(row.get("definition_id") or ""),
        "name": str(row.get("name") or row.get("definition_id") or ""),
        "kind": str(row.get("kind") or "activity"),
        "task_id": int(row.get("task_id") or 0) & 0xFFFFFFFF,
        "instance_id": int(row.get("instance_id") or 0),
        "mode": str(row.get("mode") or "dungeon"),
        **{key: row[key] for key in ("target_scene_id", "target_tid", "target_name") if key in row},
        "enabled": True,
    }


def update_custom_definition_ids(
    custom_ids: dict | None,
    definition_id: str,
    *,
    task_id: int = 0,
    instance_id: int = 0,
) -> dict:
    """Set per-role task/instance ids for one dungeon definition. @author by ak"""
    out = dict(custom_ids or {})
    cfg = dict(out.get(definition_id) or {})
    try:
        tid = int(task_id) & 0xFFFFFFFF
    except (TypeError, ValueError):
        tid = 0
    try:
        iid = int(instance_id)
    except (TypeError, ValueError):
        iid = 0
    if tid:
        cfg["task_id"] = tid
    if iid:
        cfg["instance_id"] = iid
    out[definition_id] = cfg
    return out


# ============================================================
# 版本化角色定时配置（原子写入，按角色 ID 落盘）
# ============================================================
SCHEDULE_CONFIG_VERSION = 1
SETTING_SCHEDULE_ENABLED = "task_schedule_enabled"
SETTING_SCHEDULE_OWNER = "task_schedule_owner_id"
SETTING_SCHEDULE_LOADED_OWNER = "task_schedule_loaded_owner"


def _schedule_config_path() -> Path:
    """Disk path for the versioned per-role schedule config. @author by ak"""
    try:
        from common.paths import ensure_writable_dir

        base = ensure_writable_dir("runtime", "config")
    except Exception:
        try:
            base = Path(__file__).resolve().parents[2] / "runtime" / "config"
        except Exception:
            base = Path.cwd() / "runtime" / "config"
    return Path(base) / f"schedule_config_v{SCHEDULE_CONFIG_VERSION}.json"


def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically (temp file + replace). @author by ak"""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.replace(path)
        except Exception:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )


def load_schedule_config() -> dict:
    """Read whole schedule config {version, roles: {role_id: profile}}. @author by ak"""
    out: dict[str, Any] = {"version": SCHEDULE_CONFIG_VERSION, "roles": {}}
    path = _schedule_config_path()
    try:
        if not path.is_file():
            return out
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return out
        roles = data.get("roles")
        if isinstance(roles, dict):
            out["roles"] = roles
    except Exception:
        pass
    return out


def normalize_role_schedule_profile(profile: dict | None) -> dict:
    """Sanitize one role profile (queue items normalized too). @author by ak"""
    p = dict(profile or {})
    owner = str(p.get("captain_id") or p.get("owner_id") or "").strip()
    custom_ids = (
        dict(p.get("custom_ids") or {})
        if isinstance(p.get("custom_ids"), dict)
        else {}
    )

    def _i(v: Any, default: int) -> int:
        try:
            n = int(float(str(v).strip()))
        except Exception:
            n = int(default)
        return n

    hour = max(0, min(23, _i(p.get("hour"), DEFAULT_SCHEDULE_HOUR)))
    minute = max(0, min(59, _i(p.get("minute"), DEFAULT_SCHEDULE_MINUTE)))
    queue: list[dict] = []
    if isinstance(p.get("queue"), list):
        for raw in p["queue"]:
            n = normalize_queue_item(raw, custom_ids=custom_ids)
            if n is not None:
                queue.append(n)
    return {
        "captain_id": owner,
        "enabled": bool(p.get("enabled", False)),
        "hour": hour,
        "minute": minute,
        "queue": queue,
        "last_run_date": str(p.get("last_run_date") or "").strip(),
        "custom_ids": custom_ids,
        "updated_at": str(p.get("updated_at") or ""),
    }


def _default_role_schedule_profile(key: str) -> dict:
    return {
        "captain_id": key,
        "enabled": False,
        "hour": DEFAULT_SCHEDULE_HOUR,
        "minute": DEFAULT_SCHEDULE_MINUTE,
        "queue": [],
        "last_run_date": "",
        "custom_ids": {},
        "updated_at": "",
    }


def load_role_schedule_profile(role_id) -> dict:
    """Load one role's schedule from roles/{role_id}/schedule.json first."""
    key = str(role_id or "").strip()
    if not key:
        return _default_role_schedule_profile("")
    try:
        from app.core.account_manager import load_role_config

        profile = load_role_config(key, "schedule")
        if isinstance(profile, dict):
            p = normalize_role_schedule_profile(profile)
            if not p["captain_id"]:
                p["captain_id"] = key
            return p
    except Exception:
        pass
    data = load_schedule_config()
    roles = data.get("roles") or {}
    profile = roles.get(key)
    if isinstance(profile, dict):
        p = normalize_role_schedule_profile(profile)
        if not p["captain_id"]:
            p["captain_id"] = key
        return p
    return _default_role_schedule_profile(key)


def save_role_schedule_profile(role_id, profile: dict) -> None:
    """Persist one role's schedule in its role directory and legacy mirror."""
    key = str(role_id or "").strip()
    if not key:
        return
    p = normalize_role_schedule_profile(profile)
    if not p["captain_id"]:
        p["captain_id"] = key
    p["updated_at"] = datetime.now().isoformat(timespec="seconds")
    from app.core.account_manager import save_role_config

    save_role_config(key, "schedule", p)
    data = load_schedule_config()
    roles = data.get("roles")
    if not isinstance(roles, dict):
        roles = {}
        data["roles"] = roles
    roles[key] = p
    atomic_write_json(_schedule_config_path(), data)

def profile_from_settings(settings: dict | None) -> dict:
    """Build a role profile from in-memory window settings. @author by ak"""
    s = settings if isinstance(settings, dict) else {}
    h, m = get_schedule_hm(s)
    cids = s.get("schedule_custom_ids")
    return {
        "captain_id": str(s.get(SETTING_SCHEDULE_OWNER) or "").strip(),
        "enabled": bool(s.get(SETTING_SCHEDULE_ENABLED, False)),
        "hour": h,
        "minute": m,
        "queue": load_schedule_queue(s),
        "last_run_date": str(s.get(SETTING_LAST_RUN) or "").strip(),
        "custom_ids": dict(cids) if isinstance(cids, dict) else {},
    }


def bind_settings_to_profile(settings: dict | None, profile: dict | None) -> None:
    """Copy a role profile into in-memory window settings. @author by ak"""
    if not isinstance(settings, dict):
        return
    p = normalize_role_schedule_profile(profile)
    settings[SETTING_SCHEDULE_ENABLED] = bool(p.get("enabled", False))
    settings[SETTING_SCHEDULE_OWNER] = str(p.get("captain_id") or "").strip()
    settings[SETTING_HOUR] = int(p.get("hour", DEFAULT_SCHEDULE_HOUR))
    settings[SETTING_MINUTE] = int(p.get("minute", DEFAULT_SCHEDULE_MINUTE))
    settings[SETTING_QUEUE] = list(p.get("queue") or [])
    settings[SETTING_LAST_RUN] = str(p.get("last_run_date") or "").strip()
    settings["schedule_custom_ids"] = dict(p.get("custom_ids") or {})
    settings[SETTING_SCHEDULE_LOADED_OWNER] = str(p.get("captain_id") or "").strip()


def schedule_profile_should_fire(
    profile: dict | None, *, now: datetime | None = None
) -> bool:
    """True when profile enabled and local clock hits HH:MM and not fired today. @author by ak"""
    p = normalize_role_schedule_profile(profile)
    if not bool(p.get("enabled")):
        return False
    now = now or datetime.now()
    if now.hour != int(p.get("hour")) or now.minute != int(p.get("minute")):
        return False
    today = now.date().isoformat()
    return str(p.get("last_run_date") or "").strip() != today


def mark_profile_schedule_fired(profile: dict | None, *, when: date | None = None) -> str:
    """Record today's fire date on a profile dict. @author by ak"""
    p = profile if isinstance(profile, dict) else {}
    d = (when or date.today()).isoformat()
    p["last_run_date"] = d
    return d


# ============================================================
# 队列兼容：自定义项 vs 旧已接任务项
# ============================================================
def queue_item_key(item: dict | None) -> tuple[str, str]:
    """Dedupe key: custom by definition_id, legacy by task_id. @author by ak"""
    n = dict(item or {})
    if str(n.get("source") or "").strip().lower() == "custom":
        return ("custom", str(n.get("definition_id") or "").strip())
    return ("task", str(int(n.get("task_id") or 0) & 0xFFFFFFFF))


def try_add_task_to_queue(settings: dict | None, task: TaskInfo | dict) -> dict:
    """
    Resolve + append task to schedule queue.

    Returns {ok, item?, error?, queue}
    """
    resolved = resolve_instance_for_task(task)
    queue = load_schedule_queue(settings)
    if not resolved.get("ok"):
        return {
            "ok": False,
            "error": resolved.get("note") or "无法映射副本实例",
            "queue": queue,
        }
    tid = int(resolved["task_id"])
    if any(int(x.get("task_id") or 0) == tid for x in queue):
        return {"ok": False, "error": f"队列已有 #{tid}", "queue": queue}
    item = {
        "task_id": tid,
        "name": str(resolved.get("name") or f"任务{tid}"),
        "instance_id": int(resolved["instance_id"]),
        "mode": str(resolved.get("mode") or "dungeon"),
        "alias": str(resolved.get("alias") or ""),
    }
    queue.append(item)
    save_schedule_queue(settings, queue)
    return {"ok": True, "item": item, "queue": queue}


def remove_task_from_queue(settings: dict | None, task_id: int) -> list[dict]:
    tid = int(task_id or 0) & 0xFFFFFFFF
    queue = [x for x in load_schedule_queue(settings) if int(x.get("task_id") or 0) != tid]
    return save_schedule_queue(settings, queue)


def remove_queue_index(settings: dict | None, index: int) -> list[dict]:
    queue = load_schedule_queue(settings)
    if 0 <= int(index) < len(queue):
        queue.pop(int(index))
    return save_schedule_queue(settings, queue)


def move_queue_index(
    settings: dict | None, index: int, delta: int
) -> list[dict]:
    """Move a queue item up/down (-1 / +1); returns the new queue. @author by ak"""
    queue = load_schedule_queue(settings)
    idx = int(index or 0)
    d = int(delta or 0)
    if not (0 <= idx < len(queue)):
        return queue
    new_idx = max(0, min(len(queue) - 1, idx + d))
    if new_idx == idx:
        return queue
    item = queue.pop(idx)
    queue.insert(new_idx, item)
    return save_schedule_queue(settings, queue)


def add_custom_to_queue(
    settings: dict | None, definition_id: str, *, custom_ids: dict | None = None
) -> dict:
    """Append a custom definition to the schedule queue; disabled definitions rejected. @author by ak"""
    s = settings if isinstance(settings, dict) else {}
    cids = _settings_custom_ids(settings, custom_ids)
    defn = get_custom_definition(definition_id, cids)
    queue = load_schedule_queue(s, custom_ids=cids)
    if defn is None:
        return {"ok": False, "error": f"未知任务定义 {definition_id}", "queue": queue}
    if not custom_definition_enabled(defn):
        return {
            "ok": False,
            "error": "该任务待配置 task_id/instance_id，禁止加入队列",
            "queue": queue,
        }
    key = queue_item_key({"source": "custom", "definition_id": definition_id})
    if any(queue_item_key(x) == key for x in queue):
        return {"ok": False, "error": "队列已有该自定义任务", "queue": queue}
    item = custom_queue_item(defn)
    if item is None:
        return {"ok": False, "error": "任务定义不可用", "queue": queue}
    queue.append(item)
    save_schedule_queue(s, queue, custom_ids=cids)
    return {"ok": True, "item": item, "queue": queue}


@dataclass
class ScheduleStepEvent:
    phase: str
    message: str
    ok: bool = True
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ScheduleTaskRunner states
RUNNER_STATE_IDLE = "idle"
RUNNER_STATE_FORMING_TEAM = "forming_team"
RUNNER_STATE_AUDITING = "auditing"
RUNNER_STATE_RUNNING = "running"
RUNNER_STATE_PAUSE_PENDING = "pause_pending"
RUNNER_STATE_PAUSED = "paused"
RUNNER_STATE_BLOCKED = "blocked"
RUNNER_STATE_STOPPED = "stopped"
RUNNER_STATE_COMPLETED = "completed"


# Custom item live status labels
CUSTOM_STATUS_UNCHEKED = "未核对"
CUSTOM_STATUS_NOT_ACCEPTED = "未接"
CUSTOM_STATUS_IN_PROGRESS = "进行中"
CUSTOM_STATUS_CAN_FINISH = "可交"
CUSTOM_STATUS_DONE = "已完成"
CUSTOM_STATUS_RUNNING = "执行中"
CUSTOM_STATUS_FAILED = "失败"


def custom_audit_activity(
    points: int | None, target: int = CUSTOM_ACTIVITY_TARGET_POINTS
) -> dict:
    """Audit result for an activity custom task. @author by ak"""
    pts = max(0, int(points)) if points is not None else 0
    return {
        "points": int(pts),
        "target": int(target),
        "status": "active",
        "label": f"活跃 {int(pts)}/{int(target)}",
    }


def custom_audit_dungeon(task_row: dict | None) -> dict:
    """Audit result for a dungeon custom task from an accepted-task row. @author by ak"""
    if task_row is None:
        return {"status": "not_accepted", "label": CUSTOM_STATUS_NOT_ACCEPTED}
    if task_row.get("can_finish") or task_row.get("is_finished"):
        return {"status": "can_finish", "label": CUSTOM_STATUS_CAN_FINISH}
    return {"status": "in_progress", "label": CUSTOM_STATUS_IN_PROGRESS}


# Native CanFinish(id) creates a remote thread per call — expensive. Cache per
# pid with a short TTL so repeated audits inside one plan run stay sub-second.
_CAN_FINISH_CACHE: dict[int, dict] = {}
_CAN_FINISH_CACHE_LOCK = threading.RLock()
_CAN_FINISH_TTL_S = 2.0


def invalidate_can_finish_cache(pid: int, task_id: int = 0) -> None:
    """Drop cached CanFinish for one pid (task_id=0 clears the whole pid). @author by ak"""
    pid = int(pid or 0)
    if not pid:
        return
    with _CAN_FINISH_CACHE_LOCK:
        ent = _CAN_FINISH_CACHE.get(pid)
        if ent is None:
            return
        tid = int(task_id or 0)
        if tid:
            ent.get("tasks", {}).pop(tid, None)
        else:
            _CAN_FINISH_CACHE.pop(pid, None)


def _can_finish_cached(session, task_id: int) -> bool:
    """Native CanFinish with a short TTL cache (fast repeated audits). @author by ak"""
    tid = int(task_id) & 0xFFFFFFFF
    pid = int(getattr(session, "pid", 0) or 0)
    now = time.monotonic()
    with _CAN_FINISH_CACHE_LOCK:
        ent = _CAN_FINISH_CACHE.get(pid)
        if ent and (now - float(ent.get("ts", 0.0))) < _CAN_FINISH_TTL_S:
            val = ent.get("tasks", {}).get(tid)
            if val is not None:
                return bool(val)
    try:
        ok = bool(task_can_finish(session, tid, log=lambda _m: None))
    except Exception:
        ok = False
    with _CAN_FINISH_CACHE_LOCK:
        ent = _CAN_FINISH_CACHE.setdefault(pid, {"ts": now, "tasks": {}})
        ent["tasks"][tid] = ok
    return ok


def accepted_task_map(
    session,
    *,
    refresh_can_finish: bool = False,
    log: LogFn | None = None,
    task_ids: set[int] | None = None,
    force_list: bool = False,
) -> dict[int, dict]:
    """Accepted task_id -> row dict for a small set of task_ids.

    The accepted list is read from memory once per pid and cached (TTL) so the
    plan audit and per-dungeon checks reuse it instead of re-enumerating (which
    spawns remote threads each call). Native CanFinish runs only for ``task_ids``
    when provided (fast), else for every accepted task; results are TTL-cached.

    @author by ak
    """
    rows = _accepted_rows_cached(session, force=bool(force_list), log=log)
    want: set[int] | None = None
    if task_ids is not None:
        want = {int(t) & 0xFFFFFFFF for t in (task_ids or []) if int(t or 0)}
    out: dict[int, dict] = {}
    for raw in rows:
        rd = dict(raw or {})
        try:
            tid = int(rd.get("task_id") or 0)
        except (TypeError, ValueError):
            tid = 0
        if not tid:
            continue
        if refresh_can_finish and (want is None or tid in want):
            rd["can_finish"] = _can_finish_cached(session, tid)
        out[tid] = rd
    return out


# Accepted accepted-list rows cache (memory-only enumeration), keyed by pid.
# Avoids spawning GetTaskInterface + list remote threads on every audit.
_ACCEPTED_LIST_CACHE: dict[int, dict] = {}
_ACCEPTED_LIST_CACHE_LOCK = threading.RLock()
_ACCEPTED_LIST_TTL_S = 2.0


def invalidate_accepted_list_cache(pid: int) -> None:
    """Drop the cached accepted-list rows for one pid. @author by ak"""
    pid = int(pid or 0)
    if not pid:
        return
    with _ACCEPTED_LIST_CACHE_LOCK:
        _ACCEPTED_LIST_CACHE.pop(pid, None)


def _accepted_rows_cached(
    session, *, force: bool = False, log: LogFn | None = None
) -> list[dict]:
    """Memory-only accepted rows, cached per pid (TTL) to avoid repeat CRT. @author by ak"""
    pid = int(getattr(session, "pid", 0) or 0)
    now = time.monotonic()
    if not force:
        with _ACCEPTED_LIST_CACHE_LOCK:
            ent = _ACCEPTED_LIST_CACHE.get(pid)
            if ent and (now - float(ent.get("ts", 0.0))) < _ACCEPTED_LIST_TTL_S:
                return [dict(r) for r in (ent.get("rows") or [])]
    try:
        rows = list_accepted_tasks(
            session,
            log=log or (lambda _m: None),
            resolve_names=False,
            resolve_can_finish=False,
            quiet=True,
        )
    except Exception:
        return []
    out = [
        (r.to_dict() if hasattr(r, "to_dict") else dict(r or {})) for r in rows
    ]
    with _ACCEPTED_LIST_CACHE_LOCK:
        _ACCEPTED_LIST_CACHE[pid] = {"ts": now, "rows": out}
    return out


class ScheduleTaskRunner:
    """
    Serial queue runner for the timed task plan.

    Custom items (source=custom): 检查并自动整队 → 核对全部任务 → 按队列执行 → 最终核对.
    Legacy accepted-task items keep the historical accept → ActivityRunner once → complete flow.

    State machine: idle / forming_team / auditing / running / pause_pending / paused /
    blocked / stopped / completed. Pause stops at safe checkpoints; resume re-audits the
    current item; stop clears all run checkpoints; restart re-runs from team forming.

    @author by ak
    """

    def __init__(
        self,
        *,
        pid: int,
        hwnd: int = 0,
        role_id: int | str | None = None,
        queue: list[dict] | None = None,
        on_event: EventFn | None = None,
        log: LogFn | None = None,
        user_log: LogFn | None = None,
        qiegao_afk: tuple[float | None, float | None, float | None] | None = None,
        activity_busy_check: Callable[[], bool] | None = None,
        team_service=None,
        team_targets=None,
        team_members: str = "",
        custom_ids: dict | None = None,
        activity_entry_cd_s: float = 30.0,
        activity_return_poll_s: float = 15.0,
        hang_settings: dict | None = None,
        team_control_enabled: bool = False,
        sync_action: Callable[[str, int, str], bool] | None = None,
        sync_route_action: Callable[..., bool] | None = None,
    ):
        self.pid = int(pid)
        self.hwnd = int(hwnd or 0)
        self._role_id = str(role_id or "").strip()
        self.queue = [dict(x) for x in (queue or [])]
        self.on_event = on_event or (lambda _e: None)
        self.log = log or (lambda _m: None)
        self.user_log = user_log or (lambda _m: None)
        self.qiegao_afk = qiegao_afk or (None, None, None)
        self.activity_busy_check = activity_busy_check
        self.team_service = team_service
        self._team_targets = list(team_targets or [])
        self._team_members = str(team_members or "").strip()
        self._custom_ids = (
            dict(custom_ids or {}) if isinstance(custom_ids, dict) else {}
        )
        self._activity_entry_cd_s = max(0.0, float(activity_entry_cd_s or 30.0))
        self._activity_return_poll_s = max(1.0, float(activity_return_poll_s or 15.0))
        self._hang_settings = dict(hang_settings or {})
        self._team_control_enabled = bool(team_control_enabled)
        self._sync_action = sync_action
        self._sync_route_action = sync_route_action
        self._lifecycle = RunnerLifecycle(f"xajh-task-sched-{self.pid}")
        self._stop = self._lifecycle.stop_event
        self._thread: threading.Thread | None = None
        self._activity: ActivityRunner | None = None
        self.running = False
        self._index = 0
        self._state = RUNNER_STATE_IDLE
        self._current_item: dict | None = None
        self._current_index = -1
        self._audits: dict[str, dict] = {}
        self._pause_requested = False
        self._pause_after_round = False
        self._resume_event = threading.Event()
        self._block_reason = ""
        self._block_step = ""

    @property
    def state(self) -> str:
        return self._state

    @property
    def current_item(self) -> dict | None:
        return self._current_item

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def audits(self) -> dict:
        """definition_id -> {status,label,...} live audit map. @author by ak"""
        return dict(self._audits)

    @property
    def block_reason(self) -> str:
        return self._block_reason

    @property
    def block_step(self) -> str:
        return self._block_step

    def start(self) -> None:
        if self.running:
            return
        if self.activity_busy_check is not None:
            try:
                if self.activity_busy_check():
                    self._emit(
                        "blocked",
                        "自动副本页正在运行，请先停止",
                        ok=False,
                    )
                    return
            except Exception:
                pass
        # 每次启动清空缓存，保证首轮核对是新鲜原生数据（防止上次执行残留）
        invalidate_can_finish_cache(self.pid)
        invalidate_accepted_list_cache(self.pid)
        thread = self._lifecycle.start(self._loop)
        if thread is not None:
            self._thread = thread
            self.running = True
            self._state = RUNNER_STATE_FORMING_TEAM

    def stop(self) -> bool:
        self._stop.set()
        act = self._activity
        if act is not None:
            try:
                act.stop()
            except Exception:
                pass
        self._resume_event.set()
        stopped = self._lifecycle.stop(wait=True, timeout=3.0)
        self.running = False
        self._activity = None
        self._state = RUNNER_STATE_STOPPED
        self._current_item = None
        self._current_index = -1
        self._audits = {}
        return stopped

    def is_running(self) -> bool:
        return bool(self.running and self._lifecycle.is_running())

    def pause(self) -> None:
        """Request a cooperative pause at the next safe checkpoint. @author by ak"""
        if not self.running:
            return
        if self._state in (
            RUNNER_STATE_PAUSED,
            RUNNER_STATE_STOPPED,
            RUNNER_STATE_COMPLETED,
        ):
            return
        self._pause_requested = True
        act = self._activity
        if act is not None and act.is_running():
            self._pause_after_round = True
            self._state = RUNNER_STATE_PAUSE_PENDING
            self._emit("pause_pending", "当前本结束后暂停", ok=True)
            try:
                act.request_pause()
            except Exception:
                pass
        else:
            self._state = RUNNER_STATE_PAUSE_PENDING
            self._emit("pause_pending", "正在暂停…", ok=True)

    def resume(self) -> None:
        """Resume from paused / blocked; the loop re-audits the current step. @author by ak"""
        if not self.running:
            return
        if self._state in (
            RUNNER_STATE_PAUSED,
            RUNNER_STATE_BLOCKED,
            RUNNER_STATE_PAUSE_PENDING,
        ):
            self._resume_event.set()

    def _emit(self, phase: str, message: str, ok: bool = True, **detail) -> None:
        detail.setdefault("index", self._current_index)
        detail.setdefault("total", len(self.queue))
        detail.setdefault("state", self._state)
        ev = ScheduleStepEvent(phase=phase, message=message, ok=ok, detail=detail)
        try:
            self.on_event(ev.to_dict())
        except Exception:
            pass
        self.log(f"sched [{phase}] {message}")
        if phase in {
            "item_start",
            "item_done",
            "skip",
            "accept_skip",
            "task_skip_not_accepted",
            "routine_done",
            "task_completed",
            "complete_ok",
            "complete_skip",
            "complete_fail",
            "blocked",
            "error",
            "done",
        }:
            try:
                self.user_log(f"计划任务：{message}")
            except Exception:
                pass

    def _custom_status(self, definition_id: str, audit: dict) -> None:
        """Publish one custom item live status to the UI. @author by ak"""
        self._audits[str(definition_id or "")] = dict(audit or {})
        self._emit(
            "custom_status",
            str(audit.get("label") or CUSTOM_STATUS_UNCHEKED),
            ok=bool(audit.get("ok", True)),
            definition_id=str(definition_id or ""),
            status=audit.get("status"),
            label=audit.get("label"),
        )

    def _block(self, step: str, reason: str) -> None:
        self._block_step = str(step or "")
        self._block_reason = str(reason or "")
        self._state = RUNNER_STATE_BLOCKED
        self._emit(
            "blocked",
            f"[{self._block_step}] {self._block_reason}",
            ok=False,
            step=self._block_step,
            reason=self._block_reason,
        )

    def _pause_wait(self) -> bool:
        """Block until resume or stop; returns False when stopped. @author by ak"""
        while not self._stop.is_set():
            if self._resume_event.wait(timeout=0.5):
                self._resume_event.clear()
                self._pause_requested = False
                self._pause_after_round = False
                self._state = RUNNER_STATE_RUNNING
                self._emit("resumed", "已恢复，重新核对当前项", ok=True)
                return True
        return False

    # ---- data helpers ----
    def _session_blocked(self) -> tuple[bool, str]:
        """SafeDispatch gate. @author by ak"""
        try:
            from app.core.safe_dispatch import session_blocked

            return session_blocked(self.pid)
        except Exception:
            return False, ""

    def _read_points(self, session) -> int | None:
        """Live 活跃点. @author by ak"""
        try:
            return read_flourish_points(session, log=lambda _m: None)
        except Exception:
            return None

    def _read_accepted(
        self,
        session,
        *,
        refresh: bool = False,
        task_ids: set[int] | None = None,
        force_list: bool = False,
    ) -> dict[int, dict]:
        """Accepted task map. @author by ak"""
        try:
            return accepted_task_map(
                session,
                refresh_can_finish=bool(refresh),
                task_ids=task_ids,
                force_list=bool(force_list),
                log=lambda _m: None,
            )
        except Exception:
            return {}

    # ---- team forming ----
    def _has_custom_items(self) -> bool:
        return any(
            str(x.get("source") or "").strip().lower() == "custom"
            for x in self.queue
        )

    def _form_team(self, session) -> bool:
        targets = self._team_targets or []
        if self.team_service is None or not targets:
            self._emit(
                "team_form_skip",
                "无整队配置，跳过自动整队（仅执行本端任务）",
                ok=True,
            )
            return True
        self._state = RUNNER_STATE_FORMING_TEAM
        self._emit("team_form", "检查并自动整队…", ok=True)
        try:
            res = self.team_service.form(
                session,
                targets,
                members_text=self._team_members,
                exclude_pid=self.pid,
                stop_event=self._stop,
            )
        except Exception as e:
            self._block("forming_team", f"自动整队异常: {e}")
            return False
        if not bool(getattr(res, "ok", False)):
            self._block(
                "forming_team",
                str(getattr(res, "message", "") or "自动整队失败"),
            )
            return False
        self._emit("team_form_ok", "队伍检查通过", ok=True)
        return True

    def _dungeon_task_ids(self) -> set[int]:
        """task_ids of custom dungeon items in the queue (native can_finish targets). @author by ak"""
        out: set[int] = set()
        for item in self.queue:
            if (
                str(item.get("source") or "").strip().lower() == "custom"
                and str(item.get("kind") or "").strip().lower() == "dungeon"
            ):
                try:
                    tid = int(item.get("task_id") or 0) & 0xFFFFFFFF
                except (TypeError, ValueError):
                    tid = 0
                if tid:
                    out.add(tid)
        return out

    # ---- audit ----
    def _audit_all(self, session, *, final: bool = False) -> None:
        self._state = RUNNER_STATE_AUDITING
        self._emit("audit", "核对全部任务…", ok=True)
        accepted: dict[int, dict] = {}
        points: int | None = None
        try:
            accepted = self._read_accepted(
                session,
                refresh=True,  # 指定 task_id 一律做原生 CanFinish
                task_ids=self._dungeon_task_ids(),
            )
        except Exception as e:
            self.log(f"sched audit accepted fail: {e}")
        points = self._read_points(session)
        for item in self.queue:
            self._audit_item(session, item, accepted=accepted, points=points)
        self._emit("audit_ok", "任务核对完成", ok=True)

    def _audit_item(
        self,
        session,
        item,
        *,
        accepted: dict[int, dict] | None = None,
        points: int | None = None,
    ) -> dict:
        n = normalize_queue_item(item, custom_ids=self._custom_ids)
        if n is None:
            return {"status": "invalid", "label": "无效"}
        if str(n.get("source") or "").strip().lower() == "custom":
            defn_id = str(n.get("definition_id") or "")
            kind = str(n.get("kind") or "activity")
            if kind == "activity":
                if points is None:
                    points = self._read_points(session)
                audit = custom_audit_activity(
                    points,
                    int(n.get("target_points") or CUSTOM_ACTIVITY_TARGET_POINTS),
                )
                audit["ok"] = True
                self._custom_status(defn_id, audit)
                return audit
            if accepted is None:
                try:
                    accepted = self._read_accepted(
                        session,
                        refresh=True,
                        task_ids={int(n.get("task_id") or 0)},
                    )
                except Exception:
                    accepted = {}
            row = accepted.get(int(n.get("task_id") or 0))
            audit = custom_audit_dungeon(row)
            audit["ok"] = True
            self._custom_status(defn_id, audit)
            return audit
        return {"status": "legacy", "label": "旧任务"}

    # ---- ActivityRunner helper ----
    def _run_activity(
        self, cfg: ActivityConfig, *, task_id: int = 0, label: str = ""
    ) -> dict:
        """Run one ActivityRunner; return {phase, ok}. @author by ak"""
        act_done = threading.Event()
        act_ok = {"ok": True, "phase": ""}
        pause_ev = threading.Event()

        def _on_act(ev: ActivityStepEvent) -> None:
            phase = str(getattr(ev, "phase", "") or "")
            msg = str(getattr(ev, "message", "") or phase)
            if phase in ("done", "stopped", "error", "blocked", "paused", "game_dead"):
                act_ok["phase"] = phase
                act_ok["ok"] = bool(getattr(ev, "ok", True)) and phase not in (
                    "error",
                    "game_dead",
                )
                act_done.set()
            self._emit(
                "dungeon",
                msg,
                ok=bool(getattr(ev, "ok", True)),
                task_id=task_id,
                activity_phase=phase,
                label=label,
            )

        runner = ActivityRunner(
            pid=self.pid,
            hwnd=self.hwnd,
            role_id=self._role_id,
            cfg=cfg,
            on_event=_on_act,
            log=self.log,
            pause_event=pause_ev,
            hang_settings=self._hang_settings,
        )
        self._activity = runner
        self._emit(
            "dungeon_start",
            f"进本 id={cfg.instance_id} mode={cfg.mode} {label or ''}".strip(),
            task_id=task_id,
            instance_id=cfg.instance_id,
            label=label,
        )
        runner.start()
        while not act_done.is_set():
            if self._stop.is_set():
                try:
                    runner.stop()
                except Exception:
                    pass
                break
            if not runner.is_running():
                act_done.set()
                break
            if not interruptible_sleep(0.5, self._stop):
                try:
                    runner.stop()
                except Exception:
                    pass
                break
        try:
            if runner.is_running():
                runner.stop()
        except Exception:
            pass
        self._activity = None
        return {"phase": act_ok.get("phase"), "ok": bool(act_ok.get("ok", True))}

    # ---- custom execution ----
    def _execute_custom_item(self, session, item) -> str:
        """Execute one custom item; returns 'next' | 'pause' | 'blocked'. @author by ak"""
        n = normalize_queue_item(item, custom_ids=self._custom_ids)
        if n is None:
            self._custom_status(
                str(item.get("definition_id") or ""),
                {"status": "invalid", "label": "无效", "ok": False},
            )
            return "next"
        defn_id = str(n.get("definition_id") or "")
        kind = str(n.get("kind") or "activity")
        self._emit(
            "item_start",
            f"执行 {n.get('name')}（{'活跃' if kind == 'activity' else '副本'}）",
            ok=True,
            definition_id=defn_id,
            item=n,
        )
        if self.activity_busy_check is not None:
            try:
                if self.activity_busy_check():
                    self._block("running", "自动副本页正在运行，请先停止")
                    return "blocked"
            except Exception:
                pass
        if kind == "activity":
            return self._execute_custom_activity(session, n)
        if kind == "routine":
            return self._execute_custom_routine(session, n)
        return self._execute_custom_dungeon(session, n)

    def _execute_custom_activity(self, session, item) -> str:
        defn_id = str(item.get("definition_id") or "")
        target = int(item.get("target_points") or CUSTOM_ACTIVITY_TARGET_POINTS)
        points = self._read_points(session)
        pts = max(0, int(points)) if points is not None else None
        audit = custom_audit_activity(pts, target)
        audit["ok"] = True
        self._custom_status(defn_id, audit)
        if pts is not None and pts >= target:
            self._emit(
                "activity_full",
                f"活跃已达 {pts}/{target}，仅领取宝箱",
                ok=True,
                points=pts,
                definition_id=defn_id,
            )
        pts_now = int(pts or 0)
        need = max(1, int(ceil((int(target) - pts_now) / 10.0)))
        max_runs = need + 3
        cfg = ActivityConfig(
            mode="activity",
            instance_id=int(item.get("instance_id") or 0) or DEFAULT_INSTANCE_ID,
            target_points=target,
            max_runs=max_runs,
            max_attempts=max(10, max_runs * 3),
            claim_awards=True,
            claim_after_each_run=True,
            entry_cd_min_s=self._activity_entry_cd_s,
            entry_cd_max_s=self._activity_entry_cd_s,
            return_poll_s=self._activity_return_poll_s,
        )
        self._custom_status(
            defn_id, {"status": "running", "label": CUSTOM_STATUS_RUNNING, "ok": True}
        )
        res = self._run_activity(cfg, task_id=0, label="活跃")
        if self._stop.is_set():
            return "next"
        if res.get("phase") == "paused":
            return "pause"
        points2 = self._read_points(session)
        pts2 = max(0, int(points2)) if points2 is not None else None
        audit2 = custom_audit_activity(pts2, target)
        audit2["ok"] = pts2 is None or pts2 >= target
        self._custom_status(defn_id, audit2)
        if pts2 is not None and pts2 < target:
            self._block("activity", f"活跃仍不足 {pts2}/{target}，需人工处理")
            return "blocked"
        self._emit(
            "item_done",
            f"活跃任务完成 points={pts2 if pts2 is not None else (pts or 0)}/{target}",
            ok=True,
            definition_id=defn_id,
        )
        return "next"


    def _routine_emit(self, phase: str, message: str, task_id: int, definition_id: str, **detail) -> None:
        self._emit(phase, message, task_id=task_id, definition_id=definition_id, **detail)

    def _routine_user_log(self, message: str) -> None:
        try:
            self.user_log(str(message or ""))
        except Exception:
            pass

    def _wait_routine_scene_stable(self, session, expected_scene: int = 0, timeout_s: float = 30.0, *, previous_pos=None, require_position_change: bool = False) -> bool:
        from app.core.remote_runtime import wait_scene_ready

        result = wait_scene_ready(
            session,
            expected_scene=expected_scene,
            timeout_s=timeout_s,
            previous_pos=previous_pos,
            require_position_change=require_position_change,
            log=self.log,
        )
        return bool(result.get("ok"))

    def _cancel_routine_follow(self, session, task_id: int, definition_id: str) -> bool:
        try:
            from app.core.team_ops import set_team_follow

            result = set_team_follow(session, enabled=False, log=self.log)
            ok = bool(getattr(result, "ok", False))
            if not ok:
                self._routine_emit("routine_blocked", "取消跟随失败", task_id, definition_id, ok=False)
            return ok
        except Exception as exc:
            self._routine_emit("routine_blocked", f"取消跟随失败: {exc}", task_id, definition_id, ok=False)
            return False

    def _start_routine_follow(self, session, task_id: int, definition_id: str) -> bool:
        try:
            from app.core.team_ops import set_team_follow

            if not self._cancel_routine_follow(session, task_id, definition_id):
                return False
            if not interruptible_sleep(0.8, self._stop):
                return False
            result = set_team_follow(session, enabled=True, log=self.log)
            ok = bool(getattr(result, "ok", False))
            self._routine_emit("team_follow_started", "已发起组队跟随" if ok else "组队跟随失败", task_id, definition_id, ok=ok)
            return ok
        except Exception as exc:
            self._routine_emit("routine_blocked", f"组队跟随失败: {exc}", task_id, definition_id, ok=False)
            return False

    def _start_routine_hang_local(self, session, task_id: int, definition_id: str) -> bool:
        from dataclasses import replace
        from app.core.hang_settings import apply_hang_prepare, get_hang_config, start_hang

        cfg = get_hang_config(self._hang_settings, char_id=self._role_id or None)
        normal_cfg = replace(cfg, mode=0)
        prepare = apply_hang_prepare(session, normal_cfg, log=self.log)
        if not bool(prepare.get("ok")):
            self._routine_emit(
                "routine_blocked",
                f"正式挂机参数准备失败: {prepare.get('message') or 'unknown'}",
                task_id,
                definition_id,
                ok=False,
            )
            return False
        result = start_hang(
            session,
            normal_cfg,
            hwnd=self.hwnd,
            log=self.log,
        )
        result["prepare"] = prepare
        ok = bool(result.get("ok"))
        self._routine_emit(
            "hang_started",
            "普通挂机启动" if ok else "普通挂机启动失败",
            task_id,
            definition_id,
            mode=0,
            ok=ok,
        )
        return ok

    def _send_routine_packet(self, session, payload_hex: str) -> bool:
        from app.core.game_send import send_raw_packet
        return bool(send_raw_packet(self.pid, bytes.fromhex(payload_hex), timeout_ms=3000, log=self.log))

    def _fly_routine_fuzhou(self, session, task_id: int, definition_id: str, *, reason: str = "") -> bool:
        try:
            from app.core.map_fly import fly_page_slot_packet
            from app.core.task_sync import ACTION_MAP_FLY, ROLE_MASTER, ROLE_SLAVE, get_task_sync_hub
            hub = get_task_sync_hub()
            role = hub.get_role(self.pid)
            slaves = hub.slave_pids(exclude_pid=self.pid)
            if role == ROLE_MASTER and self._team_control_enabled:
                try:
                    from app.core.team_chat import build_master_command, send_team_message
                    team_text = build_master_command(ACTION_MAP_FLY, [task_id], extra="fuzhou")
                    team_result = send_team_message(self.pid, team_text, log=self.log)
                    self._routine_emit(
                        "return_fly_team_sync_published",
                        f"队内控已发布飞回福州城 ok={bool(team_result.get('ok'))}",
                        task_id,
                        definition_id,
                        ok=bool(team_result.get("ok")),
                        via="team",
                    )
                except Exception as exc:
                    self.log(f"daily routine team sync return fly failed: {exc}")
            elif role == ROLE_MASTER and self._sync_action is not None:
                synced = bool(self._sync_action(ACTION_MAP_FLY, task_id, "fuzhou"))
                self._routine_emit(
                    "return_fly_sync_published",
                    f"本地群控已发布飞回福州城 ok={synced}",
                    task_id,
                    definition_id,
                    listeners=(len(slaves) if synced else 0),
                    expected_slaves=len(slaves),
                    ok=synced,
                    via="local_hub",
                )
            elif role == ROLE_MASTER and slaves:
                sent = hub.publish(action=ACTION_MAP_FLY, task_id=task_id, source_pid=self.pid, name="fuzhou")
                self._routine_emit(
                    "return_fly_sync_published",
                    f"本地群控已发布飞回福州城 n={sent}",
                    task_id,
                    definition_id,
                    listeners=sent,
                    expected_slaves=len(slaves),
                    ok=sent > 0,
                    via="hub",
                )
                if sent <= 0:
                    self.log(
                        f"daily routine return: map_fly sync had no listeners "
                        f"expected={len(slaves)} roles={hub.snapshot().get('roles')}"
                    )
            if role == ROLE_SLAVE:
                message = f"{reason}，当前为从控，等待主控飞回福州城"
                self._routine_emit("return_fly_wait_sync", message, task_id, definition_id)
                self.log(f"daily routine return: {message}")
                self._routine_user_log(f"操作：{message}")
                return True
            else:
                message = (
                    f"{reason}，本地群控已通知副控，当前 PID 执行飞回福州城"
                    if slaves
                    else f"{reason}，未开启群控，当前 PID 独立飞回福州城"
                )
                self._routine_emit("return_fly_local", message, task_id, definition_id)
                self.log(f"daily routine return: {message}")
                self._routine_user_log(f"操作：{message}")
            fly_session = None
            try:
                from app.core.grocery_auto import open_attach_session

                fly_session = open_attach_session(self.pid, log=self.log)
                result = fly_page_slot_packet(
                    fly_session,
                    0xFF,
                    0,
                    label="福州城",
                    log=self.log,
                    stop_event=self._stop,
                )
                ok = bool(getattr(result, "ok", False))
                detail = getattr(result, "detail", None) or {}
                self.log(
                    f"daily routine return: local fly pid={self.pid} "
                    f"ok={ok} moved={bool(detail.get('verified_move'))} "
                    f"packet={detail.get('packet_page', 0xFF)}/0x{int(detail.get('packet_page', 0xFF)) & 0xFF:02X} "
                    f"slot={detail.get('slot', 0)}"
                )
                if not ok:
                    self.log(f"daily routine return: {reason}，飞行棋发送失败")
                    self._routine_user_log(f"操作：{reason}，飞行棋发送失败")
                    return False
                settled = self._wait_routine_scene_stable(fly_session, 68, 15.0)
                self.log(f"daily routine return: {reason}，{'已到福州城' if settled else '福州城地图未就绪'}")
                self._routine_user_log(f"操作：{reason}，{'已到福州城' if settled else '福州城地图未就绪'}")
                return settled
            finally:
                if fly_session is not None:
                    try:
                        fly_session.close()
                    except Exception:
                        pass
        except Exception as exc:
            self.log(f"routine fly fuzhou failed: {exc}")
            return False

    def _stop_routine_autoplay(self, session, task_id: int, definition_id: str) -> bool:
        try:
            from app.core.hang_settings import get_hang_config, stop_hang

            cfg = get_hang_config(self._hang_settings, char_id=self._role_id or None)
            result = stop_hang(session, cfg, hwnd=self.hwnd, log=self.log)
            ok = bool(result.get("ok", True)) if isinstance(result, dict) else bool(getattr(result, "ok", True))
            self._routine_emit("autoplay_stopped", "挂机已停止，准备返回福州城" if ok else "停止挂机失败", task_id, definition_id, ok=ok)
            return ok
        except Exception as exc:
            self._routine_emit("routine_blocked", f"停止挂机失败: {exc}", task_id, definition_id, ok=False)
            return False

    def _sync_return_stop_hang(self, task_id: int, definition_id: str) -> bool:
        """Tell controlled clients to stop挂机 before the return-flight delay."""
        try:
            from app.core.task_sync import ACTION_HANG_SYNC, ROLE_MASTER, get_task_sync_hub

            if get_task_sync_hub().get_role(self.pid) != ROLE_MASTER:
                return True
            if self._team_control_enabled:
                from app.core.team_chat import build_master_command, send_team_message

                command = build_master_command(ACTION_HANG_SYNC, [task_id], extra="off")
                result = send_team_message(self.pid, command, log=self.log)
                ok = bool(result.get("ok"))
                self._routine_emit(
                    "return_stop_hang_sync",
                    "队内控已通知副控关闭挂机",
                    task_id,
                    definition_id,
                    ok=ok,
                    via="team",
                )
                return ok
            if self._sync_route_action is not None:
                result = bool(
                    self._sync_route_action(
                        ACTION_HANG_SYNC,
                        task_id,
                        name="off",
                        phase="return_stop",
                    )
                )
                via = "local_hub"
                if not result:
                    result = bool(
                        self._sync_route_action(
                            ACTION_HANG_SYNC,
                            task_id,
                            name="off",
                            phase="return_stop_retry",
                        )
                    )
            elif self._sync_action is not None:
                result = bool(self._sync_action(ACTION_HANG_SYNC, task_id, "off"))
                via = "hub"
            else:
                result = True
                via = "none"
            self._routine_emit(
                "return_stop_hang_sync",
                "群控已发送关闭挂机通知",
                task_id,
                definition_id,
                ok=result,
                via=via,
            )
            return result
        except Exception as exc:
            self.log(f"daily routine return: sync stop挂机 failed: {exc}")
            return False

    def _sync_daily_phase(self, task_id: int, definition_id: str, *, phase: str, **route) -> bool:
        """Broadcast the portal snapshot; slaves do not need the task locally."""
        if str(route.get("portal_kind") or "").strip().lower() == "badao":
            self.log(f"daily routine sync skipped phase={phase} task={task_id} portal=badao")
            return False
        if self._sync_route_action is None:
            return False
        try:
            from app.core.task_sync import ACTION_DAILY_ROUTE
            payload = {key: value for key, value in route.items() if value is not None}
            ok = bool(
                self._sync_route_action(
                    ACTION_DAILY_ROUTE,
                    task_id,
                    name=definition_id,
                    phase=phase,
                    **payload,
                )
            )
            self.log(f"daily routine sync phase={phase} task={task_id} portal snapshot ok={ok}")
            return ok
        except Exception as exc:
            self.log(f"daily routine sync phase failed phase={phase}: {exc}")
            return False

    def _wait_routine_post_scene_settle(self, task_id: int, definition_id: str) -> bool:
        """Give every controlled client time to finish the scene transition."""
        seconds = 10.0
        try:
            if self._team_control_enabled:
                from app.core.team_chat import send_team_message
                send_team_message(self.pid, f"过图完成，任务#{task_id}统一等待10秒后判断AOI", log=self.log)
            self._routine_emit("scene_settle_wait", "主控过图稳定，统一等待10秒后判断AOI", task_id, definition_id, seconds=seconds)
            return interruptible_sleep(seconds, self._stop)
        except Exception as exc:
            self.log(f"daily routine scene settle wait failed: {exc}")
            return False

    def _wait_routine_party_aoi(self, session, timeout_s: float = 30.0) -> bool:
        """Master-side gate: do not start movement/hang until party AOI is present."""
        if not self._team_control_enabled and self._sync_route_action is None:
            return True
        try:
            from app.core.team_ops import list_party_members
            from app.core.plg_interact import get_object_id64
            from app.core.plg_objects import CLASS_PLAYER, list_class_objects
            from app.core.live_scene_hub import get_live_scene
            party = list_party_members(session, fresh=True, log=lambda _m: None)
            names: set[str] = set()
            party_obj_ids: set[int] = set()
            for member in party or []:
                name = str(member.get("name") or "").strip()
                try:
                    member_pid = int(member.get("pid") or 0)
                except Exception:
                    member_pid = 0
                try:
                    member_obj_id = int(member.get("obj_id") or 0)
                except Exception:
                    member_obj_id = 0
                # Do not identify the local player by role_id: role_id is not
                # guaranteed to be the display name. The party snapshot already
                # provides the authoritative is_self/pid marker.
                if bool(member.get("is_self")) or member_pid == int(self.pid):
                    continue
                if name:
                    names.add(name)
                if member_obj_id:
                    party_obj_ids.add(member_obj_id)
            if not names and not party_obj_ids:
                return True
            deadline = time.monotonic() + max(1.0, float(timeout_s))
            while not self._stop.is_set() and time.monotonic() < deadline:
                objects = list_class_objects(
                    session,
                    CLASS_PLAYER,
                    radius=60.0,
                    limit=96,
                    read_name=True,
                    read_tid=True,
                    log=lambda _m: None,
                )
                def _name_key(value: object) -> str:
                    return "".join(str(value or "").split()).strip().casefold()

                live_state = get_live_scene(self.pid, max_age_s=10.0)
                own_xyz = tuple(getattr(live_state, "pos", None) or ()) if live_state is not None else ()
                if len(own_xyz) != 3:
                    scene_state = read_scene_position(session, log=lambda _m: None, timeout_ms=5000)
                    own_xyz = tuple(getattr(scene_state, "scene_pos", None) or ())
                if len(own_xyz) != 3:
                    own_xyz = None
                eligible = []
                wanted_keys = {_name_key(item) for item in names}
                for obj in objects or []:
                    name = str(getattr(obj, "name", "") or "").strip()
                    if not name or _name_key(name) not in wanted_keys:
                        continue
                    ptr = int(getattr(obj, "ptr", 0) or 0)
                    obj_id = get_object_id64(session, ptr) if ptr else None
                    xyz = [getattr(obj, "x", None), getattr(obj, "y", None), getattr(obj, "z", None)]
                    distance = None
                    if own_xyz is not None and all(value is not None for value in xyz):
                        distance = sqrt(sum((float(xyz[index]) - float(own_xyz[index])) ** 2 for index in range(3)))
                    if distance is not None and distance <= 60.0:
                        eligible.append({"name": name, "obj_id": obj_id, "dist": distance})
                visible_keys = {_name_key(row.get("name")) for row in eligible}
                wanted_keys = {_name_key(name) for name in names}
                visible_obj_ids = set()
                for row in eligible:
                    try:
                        obj_id = int(row.get("obj_id") or 0)
                    except Exception:
                        obj_id = 0
                    if obj_id:
                        visible_obj_ids.add(obj_id)
                names_ready = wanted_keys.issubset(visible_keys)
                ids_ready = party_obj_ids.issubset(visible_obj_ids) if party_obj_ids else False
                if names_ready or ids_ready:
                    return True
                if not interruptible_sleep(0.5, self._stop):
                    return False
            try:
                snapshot = list_class_objects(
                    session,
                    CLASS_PLAYER,
                    radius=None,
                    limit=96,
                    read_name=True,
                    read_tid=True,
                    log=lambda _m: None,
                )
                live_state = get_live_scene(self.pid, max_age_s=10.0)
                live_xyz = tuple(getattr(live_state, "pos", None) or ()) if live_state is not None else ()
                if len(live_xyz) == 3:
                    own_xyz = live_xyz
                else:
                    scene_state = read_scene_position(session, log=lambda _m: None, timeout_ms=5000)
                    own_xyz = tuple(getattr(scene_state, "scene_pos", None) or ())
                if len(own_xyz) != 3:
                    own_xyz = None
                detail = []
                for obj in snapshot or []:
                    ptr = int(getattr(obj, "ptr", 0) or 0)
                    xyz = [getattr(obj, "x", None), getattr(obj, "y", None), getattr(obj, "z", None)]
                    distance = getattr(obj, "dist", None)
                    if distance is None and own_xyz is not None and all(value is not None for value in xyz):
                        distance = sum((float(xyz[index]) - float(own_xyz[index])) ** 2 for index in range(3)) ** 0.5
                    detail.append(
                        {
                            "name": str(getattr(obj, "name", "") or ""),
                            "tid": getattr(obj, "tid", None),
                            "ptr": f"0x{ptr:X}",
                            "obj_id": get_object_id64(session, ptr) if ptr else None,
                            "dist": distance,
                            "xyz": xyz,
                        }
                    )
                self.log(f"daily routine AOI snapshot wanted={sorted(names)} visible={detail}")
            except Exception as exc:
                self.log(f"daily routine AOI snapshot failed: {exc}")
            self.log(f"daily routine AOI wait timeout missing={sorted(names)}")
            return False
        except Exception as exc:
            self.log(f"daily routine AOI wait failed: {exc}")
            return False
    def _sync_start_hang(self, task_id: int, definition_id: str, *, mode: int = 0) -> None:
        try:
            from app.core.task_sync import ACTION_HANG_SYNC
            if self._team_control_enabled:
                from app.core.team_chat import build_master_command, send_team_message
                command = build_master_command(ACTION_HANG_SYNC, [task_id], extra=f"on|m={int(mode)}")
                result = send_team_message(self.pid, command, log=self.log)
                self.log(
                    f"daily routine sync start挂机 mode={int(mode)} team_ok={bool(result.get('ok'))}"
                )
            elif self._sync_route_action is not None:
                result = bool(
                    self._sync_route_action(
                        ACTION_HANG_SYNC,
                        task_id,
                        name=f"on|m={int(mode)}",
                        phase="hang_start",
                        hang_mode=int(mode),
                    )
                )
                self.log(f"daily routine sync start挂机 mode={int(mode)} local_ok={result}")
            elif self._sync_action is not None:
                result = bool(self._sync_action(ACTION_HANG_SYNC, task_id, f"on|m={int(mode)}"))
                self.log(f"daily routine sync start挂机 mode={int(mode)} hub_ok={result}")
        except Exception as exc:
            self.log(f"daily routine start挂机 sync failed: {exc}")

    def _sync_routine_slave_follow(self, task_id: int, definition_id: str) -> bool:
        try:
            from app.core.task_sync import ACTION_DAILY_FOLLOW
            if self._team_control_enabled:
                from app.core.team_chat import build_master_command, send_team_message
                result = send_team_message(
                    self.pid,
                    build_master_command(ACTION_DAILY_FOLLOW, [task_id]),
                    log=self.log,
                )
                return bool(result.get("ok"))
            if self._sync_action is not None:
                return bool(self._sync_action(ACTION_DAILY_FOLLOW, task_id, ""))
            return False
        except Exception as exc:
            self.log(f"daily routine slave follow sync failed: {exc}")
            return False

    def _prepare_routine_slave_follow(self, task_id: int, definition_id: str) -> bool:
        """Notify slaves to follow the master, then wait before ordinary hang."""
        try:
            if not interruptible_sleep(1.0, self._stop):
                return False
            sent = self._sync_routine_slave_follow(task_id, definition_id)
            self._routine_emit(
                "routine_slave_follow",
                "已通知副控主动跟随主控，主控等待3秒后开启普通挂机",
                task_id,
                definition_id,
                ok=sent,
                seconds=3.0,
            )
            return interruptible_sleep(3.0, self._stop)
        except Exception as exc:
            self._routine_emit("routine_blocked", f"副控跟随过渡失败: {exc}", task_id, definition_id, ok=False)
            return False
    def _sync_start_stop_hang(self, task_id: int, definition_id: str) -> None:
        """Stop every account before a controlled route begins."""
        try:
            from app.core.task_sync import ACTION_HANG_SYNC, ROLE_MASTER, get_task_sync_hub
            if get_task_sync_hub().get_role(self.pid) != ROLE_MASTER:
                return
            if self._team_control_enabled:
                from app.core.team_chat import build_master_command, send_team_message
                send_team_message(self.pid, build_master_command(ACTION_HANG_SYNC, [task_id], extra="off"), log=self.log)
            elif self._sync_route_action is not None:
                self._sync_route_action(
                    ACTION_HANG_SYNC,
                    task_id,
                    name="off",
                    phase="entry_stop",
                )
            elif self._sync_action is not None:
                self._sync_action(ACTION_HANG_SYNC, task_id, "off")
        except Exception as exc:
            self.log(f"daily routine start: sync stop挂机 failed: {exc}")

    def _return_routine_fuzhou(self, session, task_id: int, definition_id: str, *, reason: str) -> bool:
        message = f"{reason}，开始返回福州城"
        self._routine_emit("return_fly_started", message, task_id, definition_id)
        self.log(f"daily routine return: {message}")
        self._sync_return_stop_hang(task_id, definition_id)
        if not self._stop_routine_autoplay(session, task_id, definition_id):
            self.log(f"daily routine return: {reason}，本地停止挂机失败，取消返城")
            self._routine_user_log(f"操作：{reason}，停止挂机失败，取消返城")
            return False
        self._routine_emit(
            "return_stop_hang_wait",
            "已同步关闭挂机，等待10秒后执行飞回福州",
            task_id,
            definition_id,
            seconds=10.0,
        )
        if not interruptible_sleep(10.0, self._stop):
            return False
        return self._fly_routine_fuzhou(session, task_id, definition_id, reason=reason)
    def _routine_cooldown(self, task_id: int, definition_id: str) -> bool:
        """Wait before advancing after a daily resident task completes."""
        seconds = max(0.0, float(self._activity_entry_cd_s or 30.0))
        if seconds <= 0.0:
            return True
        self._routine_emit(
            "routine_cooldown",
            f"日常任务完成，冷却 {seconds:.0f}s 后继续",
            task_id,
            definition_id,
            seconds=seconds,
        )
        return interruptible_sleep(seconds, self._stop)

    def _prepare_badao_route(self, session, task_id: int, definition_id: str) -> dict:
        deadline = time.monotonic() + 60.0
        scene_id = 0
        while not self._stop.is_set() and time.monotonic() < deadline:
            state = read_scene_position(session, log=lambda _m: None, timeout_ms=5000)
            scene_id = int(state.scene_id or 0) if state.ok else 0
            if scene_id in (68, 72) and self._wait_routine_scene_stable(session, scene_id, 5.0):
                break
            if not interruptible_sleep(1.0, self._stop):
                return {"blocked": True}
        if scene_id == 72:
            return {"ok": self._start_routine_follow(session, task_id, definition_id), "after_scene": 72, "skipped": True}
        if scene_id != 68:
            self._routine_emit("routine_skip_wrong_scene", f"60秒内未到福州城，当前 scene={scene_id}", task_id, definition_id)
            return {"skip": True}
        if not self._start_routine_follow(session, task_id, definition_id):
            return {"blocked": True}
        from app.core.live_scene_hub import get_live_scene
        before_live = get_live_scene(self.pid, max_age_s=5.0)
        before_pos = getattr(before_live, "pos", None) if before_live is not None else None
        if before_pos is None:
            before_state = read_scene_position(session, log=lambda _m: None, timeout_ms=5000)
            before_pos = before_state.scene_pos if before_state.ok else None
        clue = {"clue": "福州郊外传送点", "kind": "portal", "name": "福州郊外传送点", "x": -185.2, "y": 67.6, "z": -247.3, "scene_id": 68}
        move = pathfind_to_clue(session, clue, hwnd=self.hwnd, arrive_radius=8.0, verify_timeout_s=45.0, stop_event=self._stop, log=self.log)
        if not move.get("ok"):
            return {"blocked": True}
        if not interruptible_sleep(1.5, self._stop):
            return {"blocked": True}
        self._sync_daily_phase(
            task_id,
            definition_id,
            phase="portal",
            portal_kind="badao",
            origin_scene_id=68,
            portal_x=clue["x"],
            portal_y=clue["y"],
            portal_z=clue["z"],
            target_scene_id=72,
        )
        if not self._send_routine_packet(session, "23000A02000000000002"):
            return {"blocked": True}
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and not self._stop.is_set():
            state = read_scene_position(session, log=lambda _m: None, timeout_ms=5000)
            if state.ok and int(state.scene_id or 0) == 72 and self._wait_routine_scene_stable(session, 72, 30.0, previous_pos=before_pos, require_position_change=True):
                return {"ok": True, "after_scene": 72, "skipped": False}
            if not interruptible_sleep(0.5, self._stop):
                break
        return {"blocked": True}

    def _routine_path_clue_retry(self, session, clue: dict, *, label: str, task_id: int, definition_id: str) -> dict:
        result = {"ok": False, "error": "寻路未完成"}
        is_monster = label == "目标怪身边" or clue.get("kind") == "monster"
        radius = 10.0 if is_monster else 8.0
        attempts = 3
        timeout_s = 45.0
        for attempt in range(1, attempts + 1):
            self._routine_user_log(
                f"操作：#{task_id} 目标怪寻路第{attempt}/{attempts}次，最多等待{timeout_s:.0f}秒"
            )
            result = pathfind_to_clue(
                session,
                clue,
                hwnd=self.hwnd,
                arrive_radius=radius,
                verify_timeout_s=timeout_s,
                stop_event=self._stop,
                log=self.log,
            )
            self.log("routine path result " + repr(result))
            path_ok = bool(result.get("ok"))
            for key in ("command_ok", "arrived", "verified"):
                if key in result and not bool(result.get(key)):
                    path_ok = False
            if path_ok and is_monster:
                # The leader reaching the monster is not enough: members must
                # also be visible in the local AOI before formal hang starts.
                # If they are not, resend the route instead of starting combat
                # while the party is split.
                if not self._wait_routine_party_aoi(session, timeout_s=30.0):
                    result = dict(result)
                    result["ok"] = False
                    result["error"] = "目标点队伍 AOI 未到齐"
                    self._routine_user_log(
                        f"操作：#{task_id} 目标怪已到达，但队伍 AOI 未到齐，准备补发寻路"
                    )
                    path_ok = False
            if path_ok:
                self._routine_user_log(
                    f"操作：#{task_id} 目标怪寻路第{attempt}/{attempts}次已到达"
                )
                return result
            if attempt < attempts:
                self._routine_user_log(
                    f"操作：#{task_id} 目标怪寻路第{attempt}/{attempts}次未到达，准备重试"
                )
                if not interruptible_sleep(1.0, self._stop):
                    break
        self._routine_user_log(
            f"操作：#{task_id} 目标怪寻路失败，已尝试{attempt}/{attempts}次，将跳过当前目标"
        )
        return result

    def _finish_routine_after_party_timeout(self, session, task_id: int, definition_id: str) -> str:
        message = "过图后AOI超时，结束当前任务并返回福州城"
        self._routine_emit("routine_party_timeout", message, task_id, definition_id, ok=False)
        self._routine_user_log(f"操作：#{task_id} {message}")
        result = self._return_routine_fuzhou(
            session,
            task_id,
            definition_id,
            reason="过图后AOI超时",
        )
        if result:
            self._routine_emit("routine_done", "过图后AOI超时，已返城结束当前任务", task_id, definition_id, ok=False)
            self._routine_user_log(f"操作：#{task_id} 过图后AOI超时，已返城结束当前任务")
            return "next"
        self._routine_user_log(f"操作：#{task_id} AOI超时返城失败")
        return "blocked"
    def _skip_empty_routine(self, session, item, task_id: int, definition_id: str) -> str:
        self._routine_emit("routine_no_target_skip", "两次扫描均无活体目标，跳过当前日常并返城", task_id, definition_id)
        result = "next" if self._return_routine_fuzhou(session, task_id, definition_id, reason="当前地图无怪") else "blocked"
        if result == "next":
            self._routine_emit("routine_done", "当前日常无怪，已返城跳过", task_id, definition_id)
        return result

    def _skip_unreachable_routine(self, session, task_id: int, definition_id: str) -> str:
        self._routine_emit(
            "routine_all_targets_unreachable",
            "活体目标存在但均不可达，跳过当前日常并返城",
            task_id,
            definition_id,
        )
        result = "next" if self._return_routine_fuzhou(session, task_id, definition_id, reason="目标均不可达") else "blocked"
        if result == "next":
            self._routine_emit("routine_done", "目标均不可达，已返城跳过", task_id, definition_id)
        return result

    def _execute_custom_routine(self, session, item) -> str:
        definition_id = str(item.get("definition_id") or "")
        task_id = int(item.get("task_id") or 0)
        try:
            accepted_ids = set(list_accepted_task_ids(session, log=lambda _m: None))
        except Exception:
            accepted_ids = set()
        if task_id not in accepted_ids:
            self._routine_emit("task_skip_not_accepted", f"#{task_id} 未接，跳过", task_id, definition_id)
            return "next"
        definition = get_custom_definition(definition_id, self._custom_ids)
        if not definition or str(definition.get("kind") or "").lower() != "routine":
            self._routine_emit("routine_blocked", f"未知日常定义 {definition_id}", task_id, definition_id, ok=False)
            return "blocked"
        definition_task_id = int(definition.get("task_id") or 0)
        if definition_task_id != task_id:
            self._routine_emit(
                "routine_blocked",
                f"日常定义与任务 ID 不一致 definition={definition_id} task={task_id} expected={definition_task_id}",
                task_id,
                definition_id,
                ok=False,
            )
            return "blocked"
        rows = list_accepted_tasks(session, log=self.log, resolve_names=True, resolve_can_finish=False, quiet=True)
        row = next((r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in rows if int(getattr(r, "task_id", 0) or r.get("task_id", 0)) == task_id), {"task_id": task_id})
        # A task can remain in the accepted list after it is already complete.
        # Check this before any route or resident scan so a stale queue item cannot
        # send the character back to a previously selected monster.
        current_status = accepted_task_map(
            session,
            refresh_can_finish=True,
            task_ids={task_id},
            force_list=True,
            log=lambda _m: None,
        ).get(task_id) or {}
        if current_status.get("is_finished") or current_status.get("can_finish"):
            self._routine_emit("routine_skip_completed", f"#{task_id} 已完成，跳过寻路和挂机", task_id, definition_id)
            self._routine_emit("routine_done", f"#{task_id} 已完成，无需执行日常", task_id, definition_id)
            return "next"
        target_scene_from_definition = int(definition.get("target_scene_id") or 0)
        target_tid_from_definition = int(definition.get("target_tid") or 0)
        self._routine_emit("entry_stop_hang", "已接任务，开始同步停止挂机", task_id, definition_id)
        self._sync_start_stop_hang(task_id, definition_id)
        if not self._stop_routine_autoplay(session, task_id, definition_id):
            return "blocked"
        portal_payload: dict = {
            "portal_kind": (
                (classify_task_portal_kind(row) or "dungeon_deep")
                if definition_id != CUSTOM_DEF_ID_DAILY_BADAO
                else "badao"
            ),
            "origin_scene_id": 68,
            "portal_tid": None,
            "portal_obj_id": None,
            "portal_x": -185.2 if definition_id == CUSTOM_DEF_ID_DAILY_BADAO else None,
            "portal_y": 67.6 if definition_id == CUSTOM_DEF_ID_DAILY_BADAO else None,
            "portal_z": -247.3 if definition_id == CUSTOM_DEF_ID_DAILY_BADAO else None,
            "target_scene_id": target_scene_from_definition,
        }
        if definition_id == CUSTOM_DEF_ID_DAILY_BADAO:
            if not interruptible_sleep(3.0, self._stop):
                return "blocked"
            route = self._prepare_badao_route(session, task_id, definition_id)
            if route.get("skip"):
                return "next"
            if not route.get("ok"):
                return "blocked"
            scene_id = target_scene_from_definition or 72
            if not self._wait_routine_post_scene_settle(task_id, definition_id):
                return "blocked"
            if not self._wait_routine_party_aoi(session, timeout_s=10.0):
                return self._finish_routine_after_party_timeout(session, task_id, definition_id)
            self._routine_emit("scene_stable", f"scene={scene_id} AOI 到齐，保持组队跟随，开始读取 resident", task_id, definition_id, scene_id=scene_id)
        else:
            state = read_scene_position(session, log=lambda _m: None, timeout_ms=5000)
            scene_id = int(state.scene_id or 0) if state.ok else 0
            target_scene = target_scene_from_definition or int(item.get("target_scene_id") or 2036)
            route_issued = scene_id != target_scene
            from app.core.live_scene_hub import get_live_scene
            if route_issued:
                route = pathfind_task(
                    session,
                    row,
                    hwnd=self.hwnd,
                    log=self.log,
                    stop_event=self._stop,
                    portal_only=True,
                )
                if not route.get("ok"):
                    return "blocked"
                route_data = dict(route.get("route") or {})
                local_portal_ptr = route.get("portal_ptr") or route_data.get("ptr")
                local_portal_name = route.get("portal_name") or route_data.get("name") or "地宫传送"
                portal_payload.update(
                    {
                        "origin_scene_id": route.get("origin_scene_id") or route.get("before_scene") or scene_id,
                        "portal_tid": route.get("portal_tid") or route_data.get("tid"),
                        "portal_obj_id": route.get("portal_obj_id") or route_data.get("obj_id"),
                        "portal_x": route.get("portal_x") or route_data.get("x"),
                        "portal_y": route.get("portal_y") or route_data.get("y"),
                        "portal_z": route.get("portal_z") or route_data.get("z"),
                    }
                )
                if not portal_payload.get("portal_x") or not portal_payload.get("portal_z"):
                    self.log(f"daily routine portal route incomplete task={task_id}")
                    return "blocked"
                self._sync_daily_phase(task_id, definition_id, phase="portal", **portal_payload)
                portal_tid = int(portal_payload.get("portal_tid") or 0)
                portal = {
                    "name": local_portal_name,
                    "ptr": local_portal_ptr,
                    "tid": portal_tid,
                    "obj_id": int(portal_payload.get("portal_obj_id") or 0),
                    "x": portal_payload.get("portal_x"),
                    "y": portal_payload.get("portal_y"),
                    "z": portal_payload.get("portal_z"),
                }
                route = use_task_portal_npc(
                    session,
                    portal,
                    hwnd=self.hwnd,
                    portal_kind=str(portal_payload.get("portal_kind") or "dungeon_deep"),
                    dungeon_tier=resolve_dungeon_tier(row, prefer_tid=portal_tid),
                    arrival_settle_s=1.0,
                    defer_packet_send=True,
                    stop_event=self._stop,
                    log=self.log,
                )
                if not route.get("ok"):
                    return "blocked"
                packet_result = send_dungeon_portal_packets(
                    session,
                    portal,
                    portal_kind=str(portal_payload.get("portal_kind") or "dungeon_deep"),
                    before_scene=int(route.get("before_scene") or scene_id or 0),
                    stop_event=self._stop,
                    log=self.log,
                )
                if not packet_result.get("ok"):
                    return "blocked"
            if not self._wait_routine_scene_stable(session, target_scene, 30.0):
                return "blocked"
            scene_id = target_scene
            if not self._wait_routine_post_scene_settle(task_id, definition_id):
                return "blocked"
            if not self._wait_routine_party_aoi(session):
                return self._finish_routine_after_party_timeout(session, task_id, definition_id)
            if not self._start_routine_follow(session, task_id, definition_id):
                return "blocked"
            self._routine_emit("scene_stable", f"scene={target_scene} AOI 到齐，开始读取 resident", task_id, definition_id, scene_id=target_scene)
        def read_current_task():
            rows_now = list_accepted_tasks(session, log=lambda _m: None, resolve_names=False, resolve_can_finish=True, quiet=True)
            return next((r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in rows_now if int(getattr(r, "task_id", 0) or r.get("task_id", 0)) == task_id), None)

        def route_target(target_row: dict) -> bool:
            target_clue = {
                "clue": target_row.get("name") or "resident",
                "kind": "monster",
                "name": target_row.get("name") or "resident",
                "x": target_row.get("world_x", target_row.get("x")),
                "y": target_row.get("world_y", target_row.get("y")),
                "z": target_row.get("world_z", target_row.get("z")),
                "scene_id": scene_id,
            }
            move_result = self._routine_path_clue_retry(
                session,
                target_clue,
                label="目标怪身边",
                task_id=task_id,
                definition_id=definition_id,
            )
            self.log("routine monster path result=" + repr(move_result))
            if move_result.get("ok") and move_result.get("command_ok") and move_result.get("arrived") and move_result.get("verified"):
                return True
            self._routine_emit(
                "routine_target_unreachable_skip",
                f"目标 {target_row.get('name') or target_row.get('tid') or 'resident'} 不可达，尝试下一个目标",
                task_id,
                definition_id,
                ok=True,
                target=target_row,
                last_distance=move_result.get("last_distance"),
                last_scene_id=move_result.get("last_scene_id"),
            )
            return False

        def read_targets() -> list[dict]:
            rows_now = read_resident_targets(
                self.pid,
                scene_id,
                target_tid=target_tid_from_definition,
                log=self.log,
            )
            if rows_now:
                return rows_now
            interruptible_sleep(1.0, self._stop)
            self._wait_routine_scene_stable(session, scene_id, 5.0)
            return read_resident_targets(
                self.pid,
                scene_id,
                target_tid=target_tid_from_definition,
                log=self.log,
            )

        is_yucanghai = definition_id == CUSTOM_DEF_ID_DAILY_YUCANGHAI
        targets_done = 0
        task_finished = False
        target_candidates = read_targets()
        if not target_candidates:
            return self._skip_empty_routine(session, {"name": definition.get("name")}, task_id, definition_id)

        def choose_reachable(candidates: list[dict]) -> dict | None:
            for candidate in candidates:
                if route_target(candidate):
                    return candidate
            return None

        max_targets = 3 if is_yucanghai else 1
        while targets_done < max_targets and not self._stop.is_set():
            if targets_done > 0:
                # 上一只目标击杀后先停挂机，等待队伍回到主控附近。
                self._routine_emit(
                    "target_switch_prepare",
                    f"余沧海第{targets_done}只目标完成，已同步停挂机，重新衔接下一目标",
                    task_id,
                    definition_id,
                    count=targets_done,
                )
                self._sync_start_stop_hang(task_id, definition_id)
                if not self._stop_routine_autoplay(session, task_id, definition_id):
                    return "blocked"
                if not self._wait_routine_party_aoi(session):
                    return self._finish_routine_after_party_timeout(session, task_id, definition_id)
                if self._team_control_enabled:
                    if not self._start_routine_follow(session, task_id, definition_id):
                        return "blocked"
                target_candidates = read_targets()
                if not target_candidates:
                    self._routine_emit("routine_blocked", f"余沧海第{targets_done + 1}只目标未读取到", task_id, definition_id, ok=False)
                    return "blocked"
            current_target = choose_reachable(target_candidates)
            if current_target is None:
                return self._skip_unreachable_routine(session, task_id, definition_id)
            # 到怪点并完成 AOI 后，取消主控组队跟随，再通知副控主动跟随。
            if not self._cancel_routine_follow(session, task_id, definition_id):
                return "blocked"
            if not self._prepare_routine_slave_follow(task_id, definition_id):
                return "blocked"
            self._sync_start_hang(task_id, definition_id, mode=0)
            if not self._start_routine_hang_local(session, task_id, definition_id):
                return "blocked"
            self._routine_emit("autoplay_started", f"#{task_id} 第{targets_done + 1}只目标已启动普通挂机" if is_yucanghai else f"#{task_id} 已启动普通挂机", task_id, definition_id)
            kill_deadline = time.monotonic() + (180.0 if is_yucanghai else 365 * 24 * 3600)
            target_x = float(current_target.get("world_x", current_target.get("x", 0.0)) or 0.0)
            target_z = float(current_target.get("world_z", current_target.get("z", 0.0)) or 0.0)
            killed = False
            while not self._stop.is_set() and time.monotonic() < kill_deadline:
                current = read_current_task()
                if current and (current.get("is_finished") or current.get("can_finish")):
                    task_finished = True
                    break
                if is_yucanghai:
                    probe = read_nearest_resident_target(self.pid, scene_id, target_tid=target_tid_from_definition, log=lambda _m: None)
                    if probe is None:
                        killed = True
                        break
                    probe_x = float(probe.get("world_x", probe.get("x", 0.0)) or 0.0)
                    probe_z = float(probe.get("world_z", probe.get("z", 0.0)) or 0.0)
                    if ((probe_x - target_x) ** 2 + (probe_z - target_z) ** 2) ** 0.5 > 12.0:
                        killed = True
                        break
                if not is_yucanghai:
                    if not interruptible_sleep(2.0, self._stop):
                        break
                else:
                    if not interruptible_sleep(2.0, self._stop):
                        break
            if not self._stop_routine_autoplay(session, task_id, definition_id):
                return "blocked"
            if task_finished:
                break
            if not is_yucanghai:
                break
            if not killed:
                self._routine_emit("routine_blocked", f"余沧海第{targets_done + 1}只目标击杀未确认", task_id, definition_id, ok=False)
                return "blocked"
            targets_done += 1
            self._routine_emit("target_killed", f"余沧海目标已击杀 {targets_done}/3，检查任务状态", task_id, definition_id, count=targets_done)
            current = read_current_task()
            if current and (current.get("is_finished") or current.get("can_finish")):
                task_finished = True
                break
        if not task_finished:
            self._routine_emit("routine_blocked", "余沧海三只目标已处理但任务仍未完成", task_id, definition_id, ok=False)
            return "blocked"
        self._routine_emit("task_completed", f"#{task_id} 已完成，不自动交付", task_id, definition_id)
        if not self._stop_routine_autoplay(session, task_id, definition_id):
            return "blocked"
        if not self._return_routine_fuzhou(session, task_id, definition_id, reason="任务已完成且挂机已停止"):
            return "blocked"
        return "next" if self._routine_cooldown(task_id, definition_id) else "blocked"

    def _skip_empty_routine_simple(self, session, task_id: int, definition_id: str) -> str:
        self._routine_emit("routine_no_target_skip", "两次扫描均无活体目标，跳过当前日常", task_id, definition_id)
        return "next" if self._return_routine_fuzhou(session, task_id, definition_id, reason="当前地图无怪") else "blocked"

    def _execute_custom_dungeon(self, session, item) -> str:
        defn_id = str(item.get("definition_id") or "")
        tid = int(item.get("task_id") or 0)
        iid = int(item.get("instance_id") or 0)
        accepted: dict[int, dict] = {}
        try:
            accepted = self._read_accepted(session, refresh=True, task_ids={tid})
        except Exception as e:
            self.log(f"sched dungeon audit fail: {e}")
        row = accepted.get(tid)
        if row is None:
            self._custom_status(
                defn_id,
                {"status": "not_accepted", "label": CUSTOM_STATUS_NOT_ACCEPTED, "ok": True},
            )
            self._emit(
                "item_done", f"#{tid} 未接，跳过", ok=True, task_id=tid, definition_id=defn_id
            )
            return "next"
        if row.get("can_finish") or row.get("is_finished"):
            self._custom_status(
                defn_id, {"status": "can_finish", "label": CUSTOM_STATUS_CAN_FINISH, "ok": True}
            )
            self._emit(
                "item_done",
                f"#{tid} 已完成/可交，跳过（不自动交付）",
                ok=True,
                task_id=tid,
                definition_id=defn_id,
            )
            return "next"
        self._custom_status(
            defn_id, {"status": "in_progress", "label": CUSTOM_STATUS_IN_PROGRESS, "ok": True}
        )
        mode = str(item.get("mode") or "dungeon").strip().lower()
        if mode not in ("dungeon", "qiegao"):
            mode = "dungeon"
        cfg = ActivityConfig(
            mode=mode,
            instance_id=iid,
            max_runs=1,
            max_attempts=1,
            claim_awards=False,
            claim_after_each_run=False,
            entry_cd_min_s=self._activity_entry_cd_s,
            entry_cd_max_s=self._activity_entry_cd_s,
            return_poll_s=self._activity_return_poll_s,
        )
        if mode == "qiegao":
            ax, ay, az = self.qiegao_afk
            cfg.qiegao_afk_x = ax
            cfg.qiegao_afk_y = ay
            cfg.qiegao_afk_z = az
        self._custom_status(
            defn_id, {"status": "running", "label": CUSTOM_STATUS_RUNNING, "ok": True}
        )
        res = self._run_activity(cfg, task_id=tid, label=f"副本{iid}")
        if self._stop.is_set():
            return "next"
        if res.get("phase") == "paused":
            return "pause"
        accepted2: dict[int, dict] = {}
        try:
            # 跑完只刷新本任务的 can_finish；已接列表复用初始缓存不重枚举。
            invalidate_can_finish_cache(self.pid, tid)
            accepted2 = self._read_accepted(
                session, refresh=True, task_ids={tid}
            )
        except Exception:
            accepted2 = {}
        row2 = accepted2.get(tid)
        if row2 is not None and (row2.get("can_finish") or row2.get("is_finished")):
            self._custom_status(
                defn_id, {"status": "can_finish", "label": CUSTOM_STATUS_CAN_FINISH, "ok": True}
            )
            self._emit(
                "item_done", f"#{tid} 已完成，不自动交付", ok=True, task_id=tid, definition_id=defn_id
            )
            return "next"
        self._custom_status(
            defn_id, {"status": "failed", "label": CUSTOM_STATUS_FAILED, "ok": False}
        )
        self._block("dungeon", f"#{tid} 执行完成仍未可交，等待人工处理")
        return "blocked"

    # ---- legacy execution ----
    def _execute_legacy_item(self, session, item) -> str:
        """Historical accepted-task queue behavior. @author by ak"""
        n = normalize_queue_item(item)
        if n is None:
            self._emit("skip", "无效队列项", ok=False, item=item)
            return "next"
        tid = int(n["task_id"])
        name = str(n.get("name") or tid)
        iid = int(n["instance_id"])
        mode = str(n.get("mode") or "dungeon")
        self._emit(
            "item_start",
            f"{name} → 本{iid}",
            ok=True,
            task_id=tid,
            instance_id=iid,
            mode=mode,
            item=n,
        )
        try:
            accepted = set(list_accepted_task_ids(session, log=lambda _m: None))
        except Exception as e:
            accepted = set()
            self.log(f"sched list_accepted fail: {e}")
        if tid not in accepted:
            self._emit("accept", f"接取 #{tid} {name}", task_id=tid)
            ar = accept_task_routed(
                session,
                tid,
                hwnd=self.hwnd,
                log=self.log,
                fast=False,
                stop_event=self._stop,
            )
            if self._stop.is_set():
                return "next"
            if not ar.ok:
                self._emit(
                    "accept_fail",
                    f"接取失败 #{tid}: {ar.error or ar.note}",
                    ok=False,
                    task_id=tid,
                )
                self._emit("item_done", f"跳过 {name}", task_id=tid)
                return "next"
            self._emit("accept_ok", f"已接取 #{tid}", task_id=tid)
        else:
            self._emit("accept_skip", f"已接过 #{tid}", task_id=tid)
        if self._stop.is_set():
            return "next"
        if self.activity_busy_check is not None:
            try:
                if self.activity_busy_check():
                    self._emit(
                        "blocked",
                        "自动副本占用中，跳过本项",
                        ok=False,
                        task_id=tid,
                    )
                    self._emit("item_done", f"跳过 {name}", task_id=tid)
                    return "next"
            except Exception:
                pass
        cfg = ActivityConfig(
            mode=mode,
            instance_id=iid,
            max_runs=1,
            claim_awards=False,
            claim_after_each_run=False,
        )
        if mode == "qiegao":
            ax, ay, az = self.qiegao_afk
            cfg.qiegao_afk_x = ax
            cfg.qiegao_afk_y = ay
            cfg.qiegao_afk_z = az
        res = self._run_activity(cfg, task_id=tid, label=name)
        if self._stop.is_set():
            return "next"
        if res.get("phase") == "paused":
            return "pause"
        can_finish = False
        try:
            rows = list_accepted_tasks_light(session, log=lambda _m: None)
            for r in rows:
                rd = r.to_dict() if hasattr(r, "to_dict") else dict(r)
                if int(rd.get("task_id") or 0) == tid and rd.get("can_finish"):
                    can_finish = True
                    break
        except Exception as e:
            self.log(f"sched recheck task fail: {e}")
        if can_finish:
            self._emit("complete", f"交付 #{tid}", task_id=tid)
            cr = complete_task_routed(
                session,
                tid,
                hwnd=self.hwnd,
                log=self.log,
                stop_event=self._stop,
            )
            if self._stop.is_set():
                return "next"
            self._emit(
                "complete_ok" if cr.ok else "complete_fail",
                (
                    f"交付成功 #{tid}"
                    if cr.ok
                    else f"交付失败 #{tid}: {cr.error or cr.note}"
                ),
                ok=bool(cr.ok),
                task_id=tid,
            )
        else:
            self._emit(
                "complete_skip",
                f"#{tid} 暂不可交，进入下一项",
                task_id=tid,
            )
        self._emit(
            "item_done",
            f"完成 {name}",
            task_id=tid,
            instance_id=iid,
        )
        return "next"

    # ---- main loop ----
    def _loop(self) -> None:
        from app.core.game_attach import GameAttachSession

        session: GameAttachSession | None = None
        try:
            queue: list[dict] = []
            for x in self.queue:
                n = normalize_queue_item(x, custom_ids=self._custom_ids)
                if n is not None:
                    queue.append(n)
            if not queue:
                self._block("queue", "计划任务队列为空或全部无效")
                self._pause_wait()
                return
            self.queue = queue
            from app.core.game_attach import GameAttachSession

            session = GameAttachSession(log=self.log)
            session.attach(self.pid)
            self._emit("attach", f"attach pid={self.pid}")
            try:
                from app.core.state_dispatch import warmup_session

                warmup_session(
                    session,
                    log=lambda m: self.log(f"sched warmup: {m}") if m else None,
                )
            except Exception as e:
                self.log(f"sched warmup skip: {e}")

            while not self._stop.is_set():
                if self._has_custom_items():
                    if not self._form_team(session):
                        if not self._pause_wait():
                            return
                        continue
                self._audit_all(session)
                self._state = RUNNER_STATE_RUNNING
                total = len(self.queue)
                i = 0
                while i < total:
                    if self._stop.is_set():
                        break
                    blocked, brsn = self._session_blocked()
                    if blocked:
                        self._emit(
                            "remote_blocked",
                            f"远程不可用已停止 pid={self.pid} ({brsn})",
                            ok=False,
                            reason=brsn,
                        )
                        break
                    item = self.queue[i]
                    self._current_index = i
                    self._current_item = item
                    self._index = i
                    if self._pause_requested:
                        self._state = RUNNER_STATE_PAUSED
                        self._emit("paused", "已暂停，点击恢复继续", ok=True)
                        if not self._pause_wait():
                            break
                        self._audit_item(session, item)
                    if self._stop.is_set():
                        break
                    if (
                        str(item.get("source") or "").strip().lower() == "custom"
                    ):
                        result = self._execute_custom_item(session, item)
                    else:
                        result = self._execute_legacy_item(session, item)
                    if self._stop.is_set():
                        break
                    if result == "pause":
                        self._state = RUNNER_STATE_PAUSED
                        self._emit("paused", "已暂停（安全节点），点击恢复继续", ok=True)
                        if not self._pause_wait():
                            break
                        self._audit_item(session, item)
                        continue
                    if result == "blocked":
                        if not self._pause_wait():
                            break
                        self._audit_item(session, item)
                        continue
                    i += 1
                if self._stop.is_set():
                    return
                self._audit_all(session, final=True)
                self._state = RUNNER_STATE_COMPLETED
                self._emit("done", "计划任务队列执行完毕", ok=True)
                break
        except Exception as e:
            self._block("error", str(e))
            self._emit("error", str(e), ok=False)
            self._pause_wait()
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            if self._state not in (
                RUNNER_STATE_PAUSED,
                RUNNER_STATE_BLOCKED,
                RUNNER_STATE_COMPLETED,
            ):
                self._state = RUNNER_STATE_STOPPED
            self.running = False
            self._activity = None
            self._emit("stopped", "计划任务推进已停止")


__all__ = [
    "SETTING_QUEUE",
    "SETTING_HOUR",
    "SETTING_MINUTE",
    "SETTING_LAST_RUN",
    "DEFAULT_SCHEDULE_HOUR",
    "DEFAULT_SCHEDULE_MINUTE",
    "resolve_instance_for_task",
    "normalize_queue_item",
    "load_schedule_queue",
    "save_schedule_queue",
    "queue_item_label",
    "get_schedule_hm",
    "set_schedule_hm",
    "schedule_should_fire",
    "mark_schedule_fired",
    "try_add_task_to_queue",
    "remove_task_from_queue",
    "remove_queue_index",
    "move_queue_index",
    "add_custom_to_queue",
    "queue_item_key",
    "CUSTOM_DEF_ID_ACTIVITY",
    "CUSTOM_DEF_ID_DAILY_BADAO",
    "CUSTOM_DEF_ID_DAILY_LONGAOTIAN",
    "CUSTOM_DEF_ID_DAILY_YUCANGHAI",
    "CUSTOM_DEF_ID_DAILY_DONGFANG_10006",
    "CUSTOM_DEF_DUNGEON_IDS",
    "CUSTOM_ACTIVITY_TARGET_POINTS",
    "CUSTOM_TASK_INSTANCE_MAP",
    "default_custom_definitions",
    "list_custom_definitions",
    "get_custom_definition",
    "custom_definition_enabled",
    "custom_definition_label",
    "custom_queue_item",
    "update_custom_definition_ids",
    "SCHEDULE_CONFIG_VERSION",
    "SETTING_SCHEDULE_ENABLED",
    "SETTING_SCHEDULE_OWNER",
    "SETTING_SCHEDULE_LOADED_OWNER",
    "load_schedule_config",
    "load_role_schedule_profile",
    "save_role_schedule_profile",
    "profile_from_settings",
    "bind_settings_to_profile",
    "schedule_profile_should_fire",
    "mark_profile_schedule_fired",
    "invalidate_accepted_list_cache",
    "invalidate_can_finish_cache",
    "RUNNER_STATE_IDLE",
    "RUNNER_STATE_FORMING_TEAM",
    "RUNNER_STATE_AUDITING",
    "RUNNER_STATE_RUNNING",
    "RUNNER_STATE_PAUSE_PENDING",
    "RUNNER_STATE_PAUSED",
    "RUNNER_STATE_BLOCKED",
    "RUNNER_STATE_STOPPED",
    "RUNNER_STATE_COMPLETED",
    "CUSTOM_STATUS_UNCHEKED",
    "CUSTOM_STATUS_NOT_ACCEPTED",
    "CUSTOM_STATUS_IN_PROGRESS",
    "CUSTOM_STATUS_CAN_FINISH",
    "CUSTOM_STATUS_DONE",
    "CUSTOM_STATUS_RUNNING",
    "CUSTOM_STATUS_FAILED",
    "custom_audit_activity",
    "custom_audit_dungeon",
    "accepted_task_map",
    "ScheduleStepEvent",
    "ScheduleTaskRunner",
]




