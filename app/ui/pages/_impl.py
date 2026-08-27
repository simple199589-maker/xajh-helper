# -*- coding: utf-8 -*-
"""
Feature pages — compact, bound to one game session when fixed_pid is set.

@author by ak
"""
from __future__ import annotations

import queue
import re
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from app.core.captcha_client import (
    DEFAULT_CAPTCHA_API_KEY,
    DEFAULT_CAPTCHA_BASE_URL,
    api_key_fingerprint,
)
from app.core.auth_mock import AuthService
from app.core.session_store import GameSession, SessionStore
from app.core.loot import (
    DEFAULT_CAST_WAIT_S,
    DEFAULT_ITEM_NAME,
    DEFAULT_PICK_RANGE,
    DEFAULT_SCAN_RADIUS,
    MODE_OPEN,
    SuperLootConfig,
    SuperLootRunner,
    SuperLootStepResult,
    SuperLootTarget,
    find_named_targets,
    mark_unreachable,
    open_attach_session,
    open_specific_target,
    super_loot_step,
    target_skip_keys,
)
from app.core.build_profile import is_dev_build
from app.core.activity_auto import (
    ActivityConfig,
    ActivityRunner,
    ActivityStepEvent,
    DEFAULT_INSTANCE_ID,
    FLOURISH_AWARD_REPU,
    QIEGAO_CATEGORY_ALIAS,
    QIEGAO_DEFAULT_AFK,
    QIEGAO_INSTANCE_ID,
    QIEGAO_INSTANCE_NAME,
    QIEGAO_TASK_LABEL,
    claim_daily_activity_awards,
    default_instance_label,
    list_instance_choices,
    load_qiegao_prefs,
    parse_instance_label,
    qiegao_afk_for_role,
    read_scene_state,
    save_qiegao_prefs,
)
from app.core.yaolu_auto import (
    ENTRY_ITEM_NAME,
    YaoluConfig,
    YaoluRunner,
    YaoluStepEvent,
    apply_yaolu_risk_constraint,
    cleanup_captcha_debug,
    normalize_yaolu_risk_constraint,
    yaolu_start_credential_error,
)
from app.core.yaolu_prefs import (
    load_yaolu_risk_constraint,
    save_yaolu_risk_constraint,
)
from app.core.dlg_image_export import cleanup_game_screenshots, game_screenshots_dir
from app.core.window_layout import (
    get_client_size,
    get_window_dpi,
    prompt_and_normalize_game_window,
)
from app.core.grocery_auto import (
    GroceryConfig,
    auto_full_gold,
    auto_open_first_slot,
    auto_sell_selected,
    auto_use_or_open_selected,
    auto_use_selected,
    open_attach_session as grocery_open_attach,
    read_money_text,
    refresh_bag,
)
from app.core.badao_exchange import (
    BadaoExchangeConfig,
    run_badao_exchange,
)
from app.core.qshop_buy import (
    DEFAULT_MALL_PILL_BAG_TID,
    DEFAULT_MALL_PILL_PACK_TID,
    DEFAULT_MALL_PILL_PRESENT_ID,
    MAX_QSHOP_PILL_GROUPS,
    QSHOP_PILL_GROUP_SIZE,
    QSHOP_PILL_GROUPS_PER_ORDER,
    buy_qshop_present,
    normalize_qshop_pill_groups,
    qshop_pill_batch_counts,
    qshop_pill_count_from_groups,
)
from app.core.package_api import (
    DEFAULT_FULL_GOLD_MIN,
    GOLD_UNIT,
    format_gold,
)
from app.core.bg_input import (
    CANCEL_WHEN_ALWAYS,
    CANCEL_WHEN_BUSY,
    CANCEL_WHEN_NEVER,
    CAST_MODE_NONE,
    CAST_MODE_SEQUENCE,
    CAST_MODE_SINGLE,
    DEFAULT_LOGITECH_SEQUENCE,
    DELIVERY_BRIDGE,
    DELIVERY_FOREGROUND,
    PRESET_Q_SPACE,
    PRESET_Q_X,
    PRESET_SKILL_ONLY,
    PRESET_SKILL_SPACE,
    PRESET_SKILL_SPACE_X,
    PRESET_SKILL_X,
    RECORDED_LOGITECH_SEQUENCE,
    SkillCancelLoopConfig,
    SkillCancelLoopRunner,
    cancel_vk_label,
    format_key_sequence,
    legacy_mode_to_pipeline,
    normalize_skill_cancel_config,
    parse_cancel_vk,
    parse_key_sequence,
    parse_skill_vk,
    skill_vk_label,
)
from app.core.task_api import (
    TaskRunner,
    TaskRunnerConfig,
    TaskStepEvent,
    accept_task_routed,
    classify_task_portal_kind,
    complete_task_routed,
    enrich_tasks_with_npc,
    find_nearby_task_npc,
    format_task_display_name,
    get_task_name,
    is_placeholder_npc_name,
    list_accepted_tasks,
    list_accepted_tasks_light,
    list_nearby_offer_tasks,
    pathfind_to_clue,
    pathfind_task,
    read_task_npc_tids,
    send_dungeon_portal_packets,
    resolve_dungeon_tier,
    list_portal_npc_candidates,
    list_nearby_npcs,
    task_status_from_state,
    use_task_portal_npc,
)
from app.core.task_schedule import (
    CUSTOM_STATUS_UNCHEKED,
    DEFAULT_SCHEDULE_HOUR,
    DEFAULT_SCHEDULE_MINUTE,
    RUNNER_STATE_BLOCKED,
    RUNNER_STATE_COMPLETED,
    RUNNER_STATE_PAUSED,
    RUNNER_STATE_PAUSE_PENDING,
    RUNNER_STATE_RUNNING,
    RUNNER_STATE_STOPPED,
    SETTING_SCHEDULE_ENABLED,
    SETTING_SCHEDULE_LOADED_OWNER,
    SETTING_SCHEDULE_OWNER,
    ScheduleTaskRunner,
    add_custom_to_queue,
    bind_settings_to_profile,
    custom_definition_label,
    custom_queue_item,
    get_schedule_hm,
    list_custom_definitions,
    load_role_schedule_profile,
    load_schedule_queue,
    mark_schedule_fired,
    move_queue_index,
    profile_from_settings,
    queue_item_label,
    remove_queue_index,
    resolve_instance_for_task,
    save_role_schedule_profile,
    save_schedule_queue,
    schedule_profile_should_fire,
    set_schedule_hm,
    try_add_task_to_queue,
)
from app.core.hang_settings import (
    DEFAULT_HANG_RADIUS,
    DEFAULT_SKIP_DUNGEON_STORY,
    DEFAULT_WANZI_NEIGONG_HANG,
    DEFAULT_WANZI_WAIGONG_HANG,
    DEFAULT_WANZI_INTERVAL_MS,
    WANZI_INTERVAL_MIN_PROD_MS,
    DEFAULT_YOUFENG_HANG,
    DEFAULT_JIANGLONG_HANG,
    DEFAULT_AUTO_OPEN_MONSTER,
    HangConfig,
    YOUFENG_HOOK_OWNER_MANUAL,
    VITALITY_RE_READY,
    WANZI_KIND_NEIGONG,
    WANZI_KIND_WAIGONG,
    apply_hang_prepare,
    format_hang_live_line,
    get_hang_config,
    get_youfeng_key_hook_state,
    get_wanzi_packet_state,
    hang_start_warnings,
    load_hang_prefs,
    normalize_hang_char_id,
    read_hang_live,
    save_hang_disk_from_config,
    update_hang_guard_config,
    start_hang,
    start_youfeng_key_hook,
    start_wanzi_packet_manual,
    stop_hang,
    stop_youfeng_key_hook,
    stop_wanzi_packet_manual,
    sync_role_prefs_to_settings,
    write_hang_config_to_settings,
)
from app.core.map_fly import FIXED_CUSTOM_FLY_PAGES
from app.core.jianglong_auto import (
    WUZUN_SCENE_IDS,
    jianglong_entry_settle_remaining,
    JianglongRotationRunner,
    begin_local_jianglong_collection,
    cast_jianglong_once,
    clear_participants,
    drop_participant,
    eligible_participants,
    end_local_jianglong_collection,
    local_jianglong_collection_active,
    report_participant,
)
from app.core.live_scene_hub import get_live_scene
from app.core.user_event_log import (
    CAT_ACTIVITY,
    CAT_CONTROL,
    CAT_GROCERY,
    CAT_LOOT,
    CAT_OP,
    CAT_SYSTEM,
    CAT_TASK,
    CAT_YAOLU,
    FILTER_ORDER,
    CATEGORY_LABELS,
    UserEventLog,
    UserLogEntry,
    action_cn,
    role_cn,
)
from app.core.task_sync import (
    ACTION_ACCEPT_DAILY_TASKS,
    ACTION_DAILY_ROUTE,
    ACTION_ACCEPT,
    ACTION_CLAIM_ACTIVITY,
    ACTION_COMPLETE,
    ACTION_HANG_SYNC,
    ACTION_JIANGLONG_QUERY,
    ACTION_JIANGLONG_CAST,
    ACTION_MAP_FLY,
    ACTION_PATH,
    ACTION_TEAM_FOLLOW,
    ACTION_DAILY_FOLLOW,
    ACTION_TEAM_LEAVE,
    ROLE_MASTER,
    ROLE_NONE,
    ROLE_SLAVE,
    TaskSyncEvent,
    get_task_sync_hub,
)
from app.core.daily_accept import (
    accept_daily_tasks_routed,
    analyze_daily_accept_records,
    save_daily_accept_capture,
)
from app.core.cloud_sync import (
    apply_settings_to_bridge,
    drop_cloud_sync_bridge,
    get_cloud_sync_bridge,
    is_cloud_slave_isolated,
)
from app.core.team_chat import (
    TEAM_ALIVE_WINDOW_S,
    TeamMsgSeen,
    _msg_is_stale,
    _now_tick_ms,
    build_master_command,
    build_master_ping,
    build_master_jianglong_query,
    build_slave_jianglong_status,
    build_slave_pong,
    is_team_control_ready,
    is_team_slave_isolated,
    read_team_events_pid,
    send_team_message,
    team_control_flag_enabled,
)
from app.ui.theme import (
    C,
    action_bar,
    make_listbox,
    make_scrollable_body,
    make_text,
    pack_scrollable_list,
    pack_scrollable_text,
    pill_tabs,
    section,
    session_selector,
)



def _is_noisy_debug_log(msg: str) -> bool:
    """Drop high-frequency diagnostic chatter from formal UI log path."""
    s = str(msg or "").strip()
    if not s:
        return True
    noisy_starts = (
        "package_api: package=",
        "package_api: GetPackage(",
        "plg GetObject",
        "plg GetObjects",
        "plg prefilter",
        "entity plg scan",
        "entity hits=",
        "SetTarget ",
        "badao: queue in",
        "badao: service ok",
        "badao: exec ",
        "badao: 正在 ",
        "badao: soft ",
        "badao: auto Hello",
        "badao: auto service",
        "badao: SELL ",
        "badao: ok ",
        "badao: page bag recheck",
    )
    low = s
    for pfx in noisy_starts:
        if low.startswith(pfx) or f"] {pfx}" in low or low.endswith(pfx):
            return True
        if pfx in low and (
            low.startswith("badao:")
            or low.startswith("package_api:")
            or low.startswith("plg ")
            or low.startswith("entity ")
            or low.startswith("SetTarget")
        ):
            return True
    # generic bag list spam mid-line
    if "package_api: package=" in s and "items=" in s:
        return True
    return False


class FeaturePage(ttk.Frame):
    """
    Base feature page bound to one session (or store when unbound).

    @author by ak
    """

    title: str = "功能"
    key: str = ""

    def __init__(
        self,
        master: tk.Misc,
        store: SessionStore,
        log_fn: Callable[[str], None] | None = None,
        fixed_pid: int | None = None,
        *,
        auth: AuthService | None = None,
        settings: dict | None = None,
        **kwargs,
    ):
        super().__init__(master, **kwargs)
        self.store = store
        self._log = log_fn or (lambda _m: None)
        self._fixed_pid = int(fixed_pid) if fixed_pid else None
        self._selected_pid: int | None = self._fixed_pid
        self._running = False
        self._ui_drain_job = None
        self._ui_drain_closed = False
        self.auth = auth
        self.settings: dict = settings if settings is not None else {}
        # Always pin service base from env/packaged profile (ignore stale settings).
        self.settings["captcha_base_url"] = DEFAULT_CAPTCHA_BASE_URL
        # Prod package: DEFAULT_CAPTCHA_API_KEY is empty — do not invent a key.
        if "captcha_api_key" not in self.settings:
            self.settings["captcha_api_key"] = DEFAULT_CAPTCHA_API_KEY
        self._build()
        self.store.on_change(self.refresh_sessions)
        self._store_listener_attached = True

    def _build(self) -> None:
        raise NotImplementedError

    def refresh_sessions(self) -> None:
        """Refresh session-dependent widgets. @author by ak"""
        pass

    def selected_session(self) -> GameSession | None:
        """Currently selected mounted session. @author by ak"""
        if self._fixed_pid is not None:
            return self.store.get(self._fixed_pid)
        if self._selected_pid is None:
            items = self.store.list()
            return items[0] if items else None
        return self.store.get(self._selected_pid)

    def log(self, msg: str) -> None:
        """Forward debug/file log line (not the formal 日志页). @author by ak"""
        if _is_noisy_debug_log(msg):
            return
        self._log(msg)

    def user_log(
        self,
        message: str,
        *,
        category: str | None = None,
        source: str | None = None,
        also_debug: bool = False,
        dedupe_s: float = 0.8,
    ) -> None:
        """
        Append one short Chinese line to the formal feature-window 日志 page.

        Does not replace debug ``log``; call both when file diagnostics still matter.
        @author by ak
        """
        msg = str(message or "").strip()
        if not msg:
            return
        cat = (category or getattr(self, "key", None) or CAT_OP or "op")
        src = source if source is not None else str(getattr(self, "title", "") or "")
        delivered = False
        try:
            top = self.winfo_toplevel()
            if hasattr(top, "append_user_log"):
                top.append_user_log(
                    msg,
                    category=str(cat),
                    source=str(src or ""),
                    dedupe_s=float(dedupe_s),
                )
                delivered = True
            elif hasattr(top, "user_events") and top.user_events is not None:
                top.user_events.append(
                    msg,
                    category=str(cat),
                    source=str(src or ""),
                    dedupe_s=float(dedupe_s),
                )
                delivered = True
        except Exception:
            delivered = False
        if also_debug or not delivered:
            try:
                self.log(f"[用户日志/{cat}] {msg}")
            except Exception:
                pass

    def _require_session(self) -> GameSession | None:
        """Return selected session or write status error. @author by ak"""
        owner_ready = None
        try:
            owner_ready = bool(getattr(self.winfo_toplevel(), "_bridge_ready"))
        except Exception:
            owner_ready = None
        if owner_ready is True:
            # The feature window is authoritative; heal a stale page snapshot
            # left by an earlier probe while injection was still running.
            self.settings["bridge_ready"] = True
        bridge_ready = bool(
            owner_ready if owner_ready is not None else self.settings.get("bridge_ready", True)
        )
        if not bridge_ready:
            msg = "注入进行中 — 请稍候"
            if hasattr(self, "_set_status_line"):
                self._set_status_line(msg)
            elif hasattr(self, "var_status"):
                self.var_status.set(msg)
            self.log(f"{self.title}: bridge not ready")
            return None
        sess = self.selected_session()
        if sess is None:
            msg = "未挂载 — 请在游戏窗口按 Delete 注入"
            if hasattr(self, "_set_status_line"):
                self._set_status_line(msg)
            elif hasattr(self, "var_status"):
                self.var_status.set(msg)
            self.log(f"{self.title}: 无挂载会话")
            return None
        return sess

    def _current_role_id(self) -> str:
        """Current mounted character id (obj_id64) as string; '' when unknown. @author by ak"""
        sess = self._require_session()
        if sess is None:
            return ""
        pid = int(getattr(sess, "pid", 0) or 0)
        if not pid:
            return ""
        # 调度中心在注入后固化 role_id；它必须优先于任何旧窗口缓存。
        try:
            bound = str(getattr(sess, "role_id", "") or "").strip()
            if bound:
                self._role_id_cache = {pid: bound}
                return bound
        except Exception:
            pass
        # 绑定尚未完成时，才允许使用本进程缓存以避免频繁 attach。
        if getattr(self, "_role_id_cache", None) is None:
            self._role_id_cache: dict[int, str] = {}
        if pid in self._role_id_cache:
            return self._role_id_cache[pid]
        attach = None
        try:
            from app.core.loot import open_attach_session
            from app.core.team_ops import read_host_identity

            attach = open_attach_session(pid, log=lambda _m: None)
            _name, oid = read_host_identity(
                attach, need_name=False, log=lambda _m: None
            )
            oid = int(oid or 0)
            rid = str(oid) if oid > 0 else ""
            self._role_id_cache[pid] = rid
            return rid
        except Exception:
            return ""
        finally:
            if attach is not None:
                try:
                    attach.close()
                except Exception:
                    pass

    def _clear_role_id_cache(self) -> None:
        """角色绑定/切换后清空 role_id 缓存（避免拿旧角色）。@author by ak"""
        try:
            self._role_id_cache = {}
        except Exception:
            pass

    def _set_running(self, running: bool) -> None:
        """
        Toggle start/stop widgets and publish activity to game window title.

        @author by ak
        """
        prev = bool(getattr(self, "_running", False))
        running = bool(running)
        self._running = running
        if hasattr(self, "btn_start"):
            self.btn_start.configure(state=tk.DISABLED if running else tk.NORMAL)
        if hasattr(self, "btn_stop"):
            self.btn_stop.configure(state=tk.NORMAL if running else tk.DISABLED)
        if hasattr(self, "btn_once"):
            self.btn_once.configure(state=tk.DISABLED if running else tk.NORMAL)
        if hasattr(self, "btn_scan"):
            self.btn_scan.configure(state=tk.DISABLED if running else tk.NORMAL)
        self._publish_activity(running)
        # Formal 日志: only emit on real transitions (avoid duplicate stop lines).
        if prev != running:
            title = str(getattr(self, "title", "") or getattr(self, "key", "") or "功能")
            if running:
                self.user_log(f"已启动「{title}」", category=CAT_OP)
            else:
                self.user_log(f"已停止「{title}」", category=CAT_OP)

    def _publish_activity(self, running: bool) -> None:
        """
        Tell host feature window which activity is running for game title tag.

        @author by ak
        """
        key = (getattr(self, "key", None) or "").strip()
        if not key:
            return
        try:
            master = self.winfo_toplevel()
            if hasattr(master, "notify_activity"):
                master.notify_activity(key, bool(running))
        except Exception:
            pass

    def _maybe_bind_target(self, parent: ttk.Frame) -> None:
        """
        Only show client picker when not bound to a single pid.

        @author by ak
        """
        if self._fixed_pid is not None:
            self._refresh_combo = lambda: None
            return
        box = section(parent, "目标客户端")
        box.pack(fill=tk.X, pady=(0, 6))
        self._combo, self._combo_var, self._refresh_combo = session_selector(
            box, self.store, self._on_pid
        )

    def _on_pid(self, pid: int | None) -> None:
        if self._fixed_pid is not None:
            self._selected_pid = self._fixed_pid
            return
        self._selected_pid = pid

    def shutdown(self) -> None:
        """
        Stop page runners on full app quit only.

        Feature window X only hides the UI — do not call this on hide, or
        auto-loot / yaolu / activity would abort while the user expects them
        to keep running until 停止 or helper exit.
        Does not unload the in-game bridge.
        @author by ak
        """
        self._cancel_ui_drain()
        self._detach_store_listener()
        if hasattr(self, "_on_stop"):
            try:
                self._on_stop()  # type: ignore[attr-defined]
            except Exception:
                pass

    def _schedule_ui_drain(self, delay_ms: int) -> None:
        """Track the page queue poll so parent-window teardown can cancel it."""
        if self._ui_drain_closed:
            return
        try:
            self._ui_drain_job = self.after(int(delay_ms), self._drain_ui)
        except Exception:
            self._ui_drain_job = None

    def _cancel_ui_drain(self) -> None:
        self._ui_drain_closed = True
        job = self._ui_drain_job
        self._ui_drain_job = None
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass

    def _detach_store_listener(self) -> None:
        if not bool(getattr(self, "_store_listener_attached", False)):
            return
        self._store_listener_attached = False
        try:
            self.store.off_change(self.refresh_sessions)
        except Exception:
            pass

    def destroy(self) -> None:
        self._cancel_ui_drain()
        self._detach_store_listener()
        super().destroy()


    def _sibling_page(self, key: str):
        """
        Resolve another feature page under the same session window.

        @author by ak
        """
        try:
            parent = self.master
            while parent is not None and not hasattr(parent, "_pages"):
                parent = getattr(parent, "master", None)
            pages = getattr(parent, "_pages", None) if parent else None
            if isinstance(pages, dict):
                return pages.get(str(key or ""))
        except Exception:
            return None
        return None

    def _control_role_shared(self) -> str:
        """
        Live hub role for this window (settings may lag exclusive demote).

        @author by ak
        """
        role = str(
            (self.settings or {}).get("task_control_role") or ROLE_NONE
        ).strip().lower()
        pid = int(self._fixed_pid or 0)
        if pid:
            try:
                hub_role = get_task_sync_hub().get_role(pid)
                if hub_role in (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE):
                    if (self.settings or {}).get("task_control_role") != hub_role:
                        self.settings["task_control_role"] = hub_role
                    return hub_role
            except Exception:
                pass
        if role not in (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE):
            return ROLE_NONE
        return role


    def _apply_cloud_control(self, *, bind_listener: bool = False) -> str:
        """
        Apply cloud-control settings for this window pid.

        Master: join + publish path. Slave: join + long-poll receive.
        Uses the current AuthService login token for cloud-control authentication.

        @author by ak
        """
        # Keep business token and the dev-test cloud restriction in sync.
        try:
            card = ""
            auth = getattr(self, "auth", None)
            if auth is not None and getattr(auth, "session", None) is not None:
                card = str(getattr(auth.session, "key", "") or "").strip()
                self.settings["login_token"] = str(
                    getattr(auth.session, "token", "") or ""
                ).strip()
                self.settings["cloud_control_available"] = not bool(
                    getattr(auth.session, "local_mock_card", False)
                )
            if not card:
                from app.core.auth_mock import last_login_key

                card = str(last_login_key() or "").strip()
            if card:
                self.settings["login_card_key"] = card
        except Exception:
            pass
        # Master cloud room name = current game character name (no UI input).
        try:
            if bool(self.settings.get("cloud_control_enabled")):
                role = str(
                    self.settings.get("task_control_role") or ROLE_NONE
                ).lower()
                if role == ROLE_MASTER and hasattr(self, "_cloud_master_name_for_apply"):
                    name = self._cloud_master_name_for_apply()
                    if name:
                        self.settings["cloud_control_master_name"] = name
        except Exception:
            pass
        pid = int(self._fixed_pid or 0)
        if not pid:
            return "云控：未挂载游戏"
        listener = None
        if bind_listener:
            # Prefer TaskPage cloud/local sync callbacks.
            on_ev = getattr(self, "_on_cloud_sync_event", None)
            if callable(on_ev):
                listener = on_ev
            else:
                on_local = getattr(self, "_on_task_sync_event", None)
                if callable(on_local):
                    listener = on_local
        try:
            st = apply_settings_to_bridge(
                self.settings,
                pid,
                log=lambda m: self.log(str(m)),
                listener=listener,
            )
            return st.label()
        except Exception as e:
            self.log(f"云控配置失败: {e}")
            return f"云控：连接失败 · {e}"


class SuperLootPage(FeaturePage):
    """
    自动宝箱 — named chest auto open / path+open.

    Layout: item params + full match list (常规/黑名单 tabs) + status log.
    Bridge open / TID fallback are system defaults (not shown in UI).

    @author by ak
    """

    title = "自动宝箱"
    key = "loot"

    def _build(self) -> None:
        self._maybe_bind_target(self)

        box_item = section(self, "指定物品")
        box_item.pack(fill=tk.X, pady=(0, 6))

        row1 = ttk.Frame(box_item, style="Panel.TFrame")
        row1.pack(fill=tk.X, pady=1)
        ttk.Label(row1, text="名称", style="Panel.Muted.TLabel", width=6).pack(
            side=tk.LEFT
        )
        # Fixed welfare chest name — not user-editable.
        self.var_item = tk.StringVar(value=DEFAULT_ITEM_NAME)
        self.ent_item = ttk.Entry(
            row1, textvariable=self.var_item, width=22, state="readonly"
        )
        self.ent_item.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        row2 = ttk.Frame(box_item, style="Panel.TFrame")
        row2.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row2, text="范围", style="Panel.Muted.TLabel", width=6).pack(
            side=tk.LEFT
        )
        # Default 2.5: stand-open only inside face range; beyond = walk then open.
        self.var_pick_range = tk.StringVar(value=str(DEFAULT_PICK_RANGE))
        ttk.Combobox(
            row2,
            textvariable=self.var_pick_range,
            values=("2", "2.5", "3", "3.5", "4"),
            width=4,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(row2, text="扫描", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        self.var_scan = tk.StringVar(value=str(int(DEFAULT_SCAN_RADIUS)))
        ttk.Combobox(
            row2,
            textvariable=self.var_scan,
            values=("40", "60", "80", "100", "120"),
            width=4,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(row2, text="读条秒", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        # This is a maximum only.  The runner proceeds as soon as the real
        # chest cast returns idle.
        self.var_cast_wait = tk.StringVar(value=str(DEFAULT_CAST_WAIT_S))
        ttk.Combobox(
            row2,
            textvariable=self.var_cast_wait,
            values=("5", "6", "6.5", "7", "8", "9", "10"),
            width=4,
        ).pack(side=tk.LEFT, padx=(4, 0))
        # System defaults (hidden): bridge open + TID fallback always on
        self.var_use_bridge = tk.BooleanVar(value=True)
        self.var_tid_fallback = tk.BooleanVar(value=True)

        # Actions live under 指定物品 so they stay visible on small windows
        bar = ttk.Frame(box_item, style="Panel.TFrame")
        bar.pack(fill=tk.X, pady=(4, 0))
        self.btn_scan = ttk.Button(
            bar, text="扫描", style="Compact.TButton", width=6, command=self._on_scan
        )
        self.btn_scan.pack(side=tk.LEFT)
        self.btn_once = ttk.Button(
            bar,
            text="开箱一次",
            style="Compact.TButton",
            width=7,
            command=self._on_once,
        )
        self.btn_once.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_start = ttk.Button(
            bar,
            text="自动开箱",
            style="Compact.Accent.TButton",
            width=8,
            command=self._on_start,
        )
        self.btn_start.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_stop = ttk.Button(
            bar,
            text="停止",
            style="Compact.TButton",
            width=6,
            command=self._on_stop,
            state=tk.DISABLED,
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_clear_bl = ttk.Button(
            bar,
            text="清空本次忽略",
            style="Compact.TButton",
            width=7,
            command=self._on_clear_blacklist,
        )
        self.btn_clear_bl.pack(side=tk.LEFT, padx=(4, 0))
        # Debug-only: freeze runtime blacklist into permanent JSON.
        self.btn_freeze_bl = ttk.Button(
            bar,
            text="固化黑名单",
            style="Compact.TButton",
            width=8,
            command=self._on_freeze_blacklist,
        )
        if self._is_debug_mode():
            self.btn_freeze_bl.pack(side=tk.LEFT, padx=(4, 0))

        # Match list dominates; status log is a compact side strip
        mid = ttk.Frame(self)
        mid.pack(fill=tk.BOTH, expand=True, pady=(0, 0))
        mid.columnconfigure(0, weight=4, minsize=220)
        mid.columnconfigure(1, weight=1, minsize=140)
        mid.rowconfigure(0, weight=1)

        box_list = section(mid, "匹配")
        box_list.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        _, self._tab_var, self._set_list_tab = pill_tabs(
            box_list,
            ("常规", "黑名单"),
            self._on_list_tab,
            initial="常规",
        )
        # Inner frame so list uses pack only (tabs already pack on box_list)
        list_host = ttk.Frame(box_list, style="Panel.TFrame")
        list_host.pack(fill=tk.BOTH, expand=True)
        self.hit_list = make_listbox(list_host, height=1)
        pack_scrollable_list(list_host, self.hit_list)
        self.hit_list.bind("<Double-Button-1>", self._on_hit_double)
        self.hit_list.bind("<Button-3>", self._on_hit_right)

        box_st = section(mid, "状态")
        box_st.grid(row=0, column=1, sticky="nsew")
        # Status is always the first line of the log (no separate label)
        self.var_status = tk.StringVar(value="待命 · 双击开箱 · 右键拉黑")
        log_host = ttk.Frame(box_st, style="Panel.TFrame")
        log_host.pack(fill=tk.BOTH, expand=True)
        self.status_log = make_text(log_host, height=1, mono=True)
        pack_scrollable_text(log_host, self.status_log)
        self.status_log.configure(state=tk.NORMAL)
        self.status_log.insert("1.0", self.var_status.get() + "\n")
        self.status_log.configure(state=tk.DISABLED)

        self._runner: SuperLootRunner | None = None
        self._ui_q: queue.Queue = queue.Queue()
        self._busy = False
        self._job_gen = 0
        self._scan_gen = 0
        self._work_stop: threading.Event | None = None
        self._hit_rows: list[dict] = []
        self._all_scan_rows: list[dict] = []
        # Successful open/pick count for this run (status log)
        self._open_ok_count = 0
        # Persistent blacklist shared with runner when loop is on
        self._skip_ids: dict[int, float] = {}
        self._skip_points: list[tuple[float, float, float]] = []
        self._path_failure_counts: dict[int, int] = {}
        # Load coord-fixed bans (survive restart/crash).
        try:
            from app.core.loot_fixed_blacklist import apply_fixed_to_skip_ids

            n_fix = apply_fixed_to_skip_ids(self._skip_ids, log=self.log)
            if n_fix:
                self.log(f"自动宝箱: 已载入固定黑名单 keys~{n_fix}")
        except Exception as e:
            self.log(f"自动宝箱: 固定黑名单载入失败 {e}")
        self._schedule_ui_drain(120)
        self.refresh_sessions()

    def refresh_sessions(self) -> None:
        if hasattr(self, "_refresh_combo"):
            self._refresh_combo()

    def _set_running(self, running: bool) -> None:
        """
        Loop running state: label 自动开箱/开箱中; scan stays always clickable.

        @author by ak
        """
        self._running = running
        if hasattr(self, "btn_start"):
            self.btn_start.configure(
                state=tk.DISABLED if running else tk.NORMAL,
                text="开箱中" if running else "自动开箱",
            )
        if hasattr(self, "btn_stop"):
            self.btn_stop.configure(state=tk.NORMAL if running else tk.DISABLED)
        if hasattr(self, "btn_once"):
            self.btn_once.configure(state=tk.DISABLED if running else tk.NORMAL)
        # Scan is always available (does not cancel loop)
        if hasattr(self, "btn_scan"):
            self.btn_scan.configure(state=tk.NORMAL)
        self._publish_activity(running)

    def _is_debug_mode(self) -> bool:
        """True when login「开启调试」/ settings debug_mode is on. @author by ak"""
        try:
            return bool(
                self.settings.get(
                    "debug_mode",
                    self.settings.get("debug_save_crops", False),
                )
            )
        except Exception:
            return False

    def _cfg_from_ui(self) -> SuperLootConfig:
        try:
            pr = float(self.var_pick_range.get() or DEFAULT_PICK_RANGE)
        except ValueError:
            pr = DEFAULT_PICK_RANGE
        try:
            sr = float(self.var_scan.get() or DEFAULT_SCAN_RADIUS)
        except ValueError:
            sr = DEFAULT_SCAN_RADIUS
        try:
            cw = float(self.var_cast_wait.get() or DEFAULT_CAST_WAIT_S)
        except ValueError:
            cw = DEFAULT_CAST_WAIT_S
        cw = max(0.5, min(cw, 30.0))
        # Name is fixed to welfare chest; UI entry is readonly.
        if hasattr(self, "var_item"):
            self.var_item.set(DEFAULT_ITEM_NAME)
        return SuperLootConfig(
            item_name=DEFAULT_ITEM_NAME,
            pick_range=pr,
            scan_radius=sr,
            cast_wait_s=cw,
            match_tid_fallback=True,
            use_bridge=True,
            interact_mode=MODE_OPEN,
        )

    def _set_status_line(self, msg: str) -> None:
        """
        Pin status as the first line of the log panel.

        @author by ak
        """
        line = (msg or "").rstrip() or "—"
        # While auto-loot is running, always keep the open-success counter
        # on the pin so path/cast intermediate lines do not hide it.
        try:
            running = self._runner is not None and self._runner.is_running()
        except Exception:
            running = False
        if running:
            n_ok = int(getattr(self, "_open_ok_count", 0) or 0)
            if "成功×" not in line:
                if line.startswith("开箱中"):
                    # "开箱中 · foo" -> "开箱中 · 成功×N · foo"
                    rest = line[len("开箱中") :].lstrip(" ·")
                    line = (
                        f"开箱中 · 成功×{n_ok}"
                        + (f" · {rest}" if rest else "")
                    )
                else:
                    line = f"开箱中 · 成功×{n_ok} · {line}"
        self.var_status.set(line)
        try:
            self.status_log.configure(state=tk.NORMAL)
            # Ensure at least one line exists
            if float(self.status_log.index("end-1c")) <= 1.0:
                self.status_log.insert("1.0", line + "\n")
            else:
                self.status_log.delete("1.0", "1.end")
                self.status_log.insert("1.0", line)
            self.status_log.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _append_status_log(self, msg: str) -> None:
        """
        Append one history line under the pinned status line.

        Skip duplicate consecutive lines (cast/path spam).
        @author by ak
        """
        line = (msg or "").rstrip()
        if not line:
            return
        try:
            last = getattr(self, "_status_log_last", None)
            if last == line:
                return
            self._status_log_last = line
            self.status_log.configure(state=tk.NORMAL)
            # Keep line 1 as status; history starts at line 2
            if float(self.status_log.index("end-1c")) < 1.0:
                self.status_log.insert("1.0", (self.var_status.get() or "—") + "\n")
            self.status_log.insert(tk.END, line + "\n")
            # keep last ~200 lines but never drop the status line
            total = int(float(self.status_log.index("end-1c").split(".")[0]))
            if total > 220:
                self.status_log.delete("2.0", f"{total - 200}.0")
            self.status_log.see(tk.END)
            self.status_log.configure(state=tk.DISABLED)
        except Exception:
            pass
        # Formal 日志 page: keep the same human lines (already filtered).
        try:
            self.user_log(line, category=CAT_LOOT)
        except Exception:
            pass

    def _human_loot_log(self, msg: str) -> str | None:
        """
        Map engine debug lines to short Chinese status text.

        Returns None to hide noise from the status panel (still goes to file log).
        Keep status sparse: one line per meaningful phase, no bridge/CRT spam.
        @author by ak
        """
        import re

        m = (msg or "").strip()
        if not m:
            return None

        low = m.lower()
        # --- Hard hide: bridge / CRT / raw protocol (file log only) ---
        noise_keys = (
            "virtualallocex",
            "openfilemapping",
            "createremotethread",
            "err=5",
            "last_err=",
            "existing bridge",
            "bridge open",
            "bridge ping",
            "bridge move",
            "bridge reinject",
            "bridge check",
            "bridge rehook",
            "bridge unload",
            "shm_note",
            "matterinteract",
            "base=0x",
            "getcurrentsceneposition",
            "err=none",
            "note='pong'",
            "note=\"pong\"",
            "mode=0",
            "mode=68",
            "hwnd=0x",
            "openfilemapping",
            "mapviewoffile",
            "inject exit",
            "preflight",
            "staged ",
            "prepare_shm",
        )
        if any(k in low for k in noise_keys):
            if "virtualallocex" in low or "err=5" in low:
                return "进程保护触发，已暂停远程调用（冷却中）"
            return None

        is_super = m.startswith("super_loot") or "super_loot" in m[:24]

        # Non-engine lines: only Chinese UI text reaches the panel.
        if not is_super:
            if any("一" <= ch <= "鿿" for ch in m):
                # Drop very technical Chinese+English hybrids from lower layers
                if any(
                    k in m
                    for k in (
                        "GetCurrentScenePosition",
                        "MatterInteract",
                        "HostMove",
                        "OpenFileMapping",
                    )
                ):
                    return None
                return m
            return None

        # --- super_loot: sparse phases only ---
        # Cast: one line while waiting, one when done (skip grace/started chatter)
        if "cast-start-grace" in m or "cast-started" in m:
            return None
        if "cast-missed" in m or "cast-no-session" in m:
            return "未见读条，重试"
        if "cast-timeout" in m:
            return "读条超时"
        if "cast-wait stopped" in m:
            return "读条已中断"
        if "cast-wait" in m and "stopped" not in m:
            sec = re.search(r"([\d.]+)s", m)
            return f"读条中… {sec.group(1)}s" if sec else "读条中…"
        if "cast-done" in m:
            return "读条完成"

        # Scan: hide per-step local chain; show idle / full scan only
        if "skip-scan local=" in m:
            return None
        if "skip-scan work=" in m:
            return None
        if "skip-scan interval" in m or "skip-scan idle-wait" in m:
            return "空闲，稍后重扫"
        if "idle-rescan" in m:
            return "空闲重扫"
        if "matter_count=" in m:
            return None  # noisy every full scan
        if "full_scan" in m and "fail" in m:
            return "扫描失败，改用缓存"
        if "match name=" in m and "hits=" in m:
            n = re.search(r"hits=(\d+)", m)
            return f"扫到 {n.group(1)} 个" if n else "扫描完成"

        # Select / path — one short line each
        if "select local-nearest" in m or "select soft-local" in m:
            d = re.search(r"d=([\d.]+)", m)
            return f"开最近 · {d.group(1)}m" if d else "开最近"
        if "select soft-walk" in m:
            d = re.search(r"d=([\d.]+)", m)
            return f"走近 · {d.group(1)}m" if d else "走近开箱"
        if "select near-dense" in m or "select near-path" in m:
            d = re.search(r"d=([\d.]+)", m)
            return f"开近处 · {d.group(1)}m" if d else "开近处"
        if "select dense" in m:
            d = re.search(r"d=([\d.]+)", m)
            return f"去密区 · {d.group(1)}m" if d else "去密区"
        if "select near_sparse" in m or "select nearest" in m:
            d = re.search(r"d=([\d.]+)", m)
            return f"下一箱 · {d.group(1)}m" if d else "下一箱"
        if "select last_resort_isolated" in m:
            d = re.search(r"d=([\d.]+)", m)
            return f"远孤兜底 · {d.group(1)}m" if d else "远孤兜底"
        if "skip isolated-far" in m:
            return "暂缓远孤箱"
        if "in-range" in m:
            d = re.search(r"d=([\d.]+)", m)
            return f"开箱 · {d.group(1)}m" if d else "开箱"
        if "path then" in m or "manual path then" in m:
            d = re.search(r"d=([\d.]+)", m)
            return f"寻路 · {d.group(1)}m" if d else "寻路"
        if "path-open" in m or "manual path-open" in m:
            d = re.search(r"d=([\d.]+)", m)
            return f"到位开箱 · {d.group(1)}m" if d else "到位开箱"
        if "arrive recheck" in m or "arrive recheck fail" in m:
            return "寻路后偏远，换下一个"
        if "arrived" in m:
            return "到达，开箱"
        if "move nudge" in m:
            return "寻路重试"
        if "stuck give-up" in m or "arrive timeout" in m:
            return "寻路超时，换下一个"
        if "last-chance open" in m or "soft-open" in m:
            return "补开"
        if "open ok=True" in m:
            return None  # cast / step line covers it
        if "open ok=False" in m:
            return "开箱失败"

        # Cooldown / recovery
        if "bridge-cooldown" in m or "bridge_cooldown" in m:
            sec = re.search(r"([\d.]+)s", m)
            return f"桥接冷却 {sec.group(1)}s" if sec else "桥接冷却"
        if "crt-cooldown" in m:
            sec = re.search(r"([\d.]+)s", m)
            return f"扫描冷却 {sec.group(1)}s" if sec else "扫描冷却"
        if "bridge-dead" in m or "桥接未就绪" in m:
            return "桥接未就绪"
        if "reattach" in m and "fail" in m:
            return "重连失败"
        if "reattach" in m:
            return "重连游戏…"
        if "pid=" in m and ("已退出" in m or "不存在" in m):
            return "游戏已退出"
        if "runner stopped" in m:
            return "已停止"
        if "runner attach" in m:
            return "已连接"
        if "blacklist cleared" in m:
            return "黑名单已清空"
        if "cache fallback" in m or "hard-fail" in m:
            return "异常，改用缓存"
        if "count hard-fail" in m or "scan hard-fail" in m:
            return "扫描异常"

        # Known noise keys inside super_loot
        if any(
            k in m
            for k in (
                "unreachable",
                "prefilter",
                "players skip",
                "skip scene",
                "move_mode",
                "drop dirty",
                "bridge move",
                "remote move",
                "move not accepted",
                "filter unreachable",
            )
        ):
            return None

        # Unknown super_loot_* — hide (file log keeps raw)
        return None

    def _push(self, kind: str, payload=None) -> None:
        self._ui_q.put((kind, payload))

    def _drain_ui(self) -> None:
        try:
            while True:
                kind, payload = self._ui_q.get_nowait()
                if kind == "status":
                    self._set_status_line(str(payload or ""))
                elif kind == "log":
                    m = str(payload or "")
                    self.log(m)
                    # Status panel: human Chinese only; raw line stays in file log.
                    human = self._human_loot_log(m)
                    if human:
                        self._append_status_log(human)
                        # Keep pin count visible during cast/path phases.
                        try:
                            if self._runner is not None and self._runner.is_running():
                                self._set_status_line(f"开箱中 · {human}")
                        except Exception:
                            pass
                elif kind == "hits":
                    self._all_scan_rows = list(payload or [])
                    self._refresh_list_view()
                elif kind == "step":
                    self._apply_step(payload)
                elif kind == "busy":
                    self._busy = bool(payload)
                    if not self._runner or not self._runner.is_running():
                        self._set_running(False)
                elif kind == "running":
                    self._set_running(bool(payload))
        except queue.Empty:
            pass
        self._schedule_ui_drain(120)

    def _cancel_running_work(self, *, reason: str = "") -> bool:
        """
        Stop loop + cancel in-flight once/double-click job before a new action.

        @author by ak
        """
        if self._runner is not None:
            try:
                self._skip_ids.update(self._runner.skip_ids())
            except Exception:
                pass
            try:
                for p in list(self._runner.skip_points()):
                    self._skip_points.append(p)
            except Exception:
                pass
            try:
                self._path_failure_counts.update(self._runner.path_failure_counts())
            except Exception:
                pass
            stopped = False
            try:
                stopped = bool(self._runner.stop())
            except Exception:
                stopped = False
            if not stopped and self._runner.is_running():
                self._append_status_log("停止仍在等待底层调用完成，暂不启动新动作")
                self._set_running(True)
                return False
            self._runner = None
            self._set_running(False)
            if reason:
                self._append_status_log(f"已取消自动开箱：{reason}")
        if self._work_stop is not None:
            self._work_stop.set()
        self._job_gen += 1
        self._work_stop = threading.Event()
        self._busy = False
        return True

    def _on_list_tab(self, _name: str) -> None:
        """Pill tab switch: regular matches vs blacklist. @author by ak"""
        self._refresh_list_view()

    def _row_is_blacklisted(self, d: dict) -> bool:
        """True if row is skipped by id/XZ key or near a stuck point. @author by ak"""
        try:
            tgt = self._hit_to_target(d)
            skip = self._active_skip_ids()
            if any(int(k) in skip for k in target_skip_keys(tgt)):
                return True
            # Match engine filter_reachable near-radius (path-stuck clusters).
            pts = self._active_skip_points()
            if not pts or tgt.x is None or tgt.z is None:
                return False
            try:
                from app.core.super_loot import DEFAULT_UNREACHABLE_NEAR_M

                radius = float(DEFAULT_UNREACHABLE_NEAR_M)
            except Exception:
                radius = 3.0
            hx, hz = float(tgt.x), float(tgt.z)
            r2 = radius * radius
            for px, pz, _exp in pts:
                dx = hx - float(px)
                dz = hz - float(pz)
                if dx * dx + dz * dz <= r2:
                    return True
            return False
        except Exception:
            return False

    def _blacklist_rows(self) -> list[dict]:
        """Rows currently blacklisted (from last scan + skip map). @author by ak"""
        out: list[dict] = []
        seen: set[tuple] = set()
        for d in self._all_scan_rows:
            if not self._row_is_blacklisted(d):
                continue
            key = (
                d.get("obj_id"),
                round(float(d["x"]), 2) if d.get("x") is not None else None,
                round(float(d["z"]), 2) if d.get("z") is not None else None,
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
        return out

    def _refresh_list_view(self) -> None:
        """Fill listbox from current tab. @author by ak"""
        tab = self._tab_var.get() if hasattr(self, "_tab_var") else "常规"
        if tab == "黑名单":
            rows = self._blacklist_rows()
            empty = "  （黑名单为空）"
        else:
            rows = [
                d for d in self._all_scan_rows if not self._row_is_blacklisted(d)
            ]
            empty = "  （无匹配）"
        self._fill_hits(rows, empty=empty)

    def _fill_hits(self, rows: list, *, empty: str = "  （无匹配）") -> None:
        """
        Fill match listbox; nearest first to match select order.

        @author by ak
        """
        self.hit_list.delete(0, tk.END)
        self._hit_rows = []
        if not rows:
            self.hit_list.insert(tk.END, empty)
            return
        parsed: list[dict] = []
        for t in rows:
            if hasattr(t, "to_dict"):
                parsed.append(t.to_dict())
            elif isinstance(t, dict):
                parsed.append(dict(t))
        parsed.sort(
            key=lambda r: float(r["dist"]) if r.get("dist") is not None else 1e18
        )
        for d in parsed:
            self._hit_rows.append(d)
            dist = d.get("dist")
            dd = f"{float(dist):.1f}" if dist is not None else "n/a"
            x = d.get("x")
            z = d.get("z")
            xz = (
                f"({float(x):.0f},{float(z):.0f})"
                if x is not None and z is not None
                else ""
            )
            bl = " [黑]" if self._row_is_blacklisted(d) else ""
            self.hit_list.insert(
                tk.END,
                f" d={dd:>5}  {xz:<12}  {d.get('name') or ''}{bl}",
            )

    def _active_skip_ids(self) -> dict[int, float]:
        """Prefer runner blacklist while loop runs. @author by ak"""
        if self._runner is not None and self._runner.is_running():
            return self._runner.skip_ids()
        return self._skip_ids

    def _active_skip_points(self) -> list[tuple[float, float, float]]:
        """Prefer runner stuck points while loop runs. @author by ak"""
        if self._runner is not None and self._runner.is_running():
            try:
                return self._runner.skip_points()
            except Exception:
                pass
        return self._skip_points

    def _active_path_failure_counts(self) -> dict[int, int]:
        if self._runner is not None and self._runner.is_running():
            try:
                return self._runner.path_failure_counts()
            except Exception:
                pass
        return self._path_failure_counts

    def _selected_hit(self) -> dict | None:
        """Current listbox selection as target dict. @author by ak"""
        try:
            sel = self.hit_list.curselection()
            if not sel:
                return None
            idx = int(sel[0])
        except Exception:
            return None
        if idx < 0 or idx >= len(self._hit_rows):
            return None
        return self._hit_rows[idx]

    def _hit_to_target(self, d: dict) -> SuperLootTarget:
        """Build SuperLootTarget from stored row. @author by ak"""
        return SuperLootTarget(
            name=str(d.get("name") or ""),
            ptr=int(d.get("ptr") or 0),
            obj_id=int(d["obj_id"]) if d.get("obj_id") is not None else None,
            tid=int(d["tid"]) if d.get("tid") is not None else None,
            dist=float(d["dist"]) if d.get("dist") is not None else None,
            x=float(d["x"]) if d.get("x") is not None else None,
            y=float(d["y"]) if d.get("y") is not None else None,
            z=float(d["z"]) if d.get("z") is not None else None,
        )

    def _on_hit_double(self, _evt=None) -> None:
        """Double-click: path to selected match and open. @author by ak"""
        d = self._selected_hit()
        if not d:
            self._set_status_line("请先选中匹配项")
            return
        if d.get("x") is None or d.get("z") is None:
            self._set_status_line("该项无坐标，无法寻路")
            return
        sess = self._require_session()
        if not sess:
            return
        if not self._cancel_running_work(reason="双击开箱"):
            return
        cfg = self._cfg_from_ui()
        tgt = self._hit_to_target(d)
        job = self._job_gen
        stop_ev = self._work_stop
        self._busy = True
        dd = f"{tgt.dist:.1f}" if tgt.dist is not None else "?"
        self._set_status_line(f"双击寻路开箱 · 距离 {dd}")
        self._append_status_log(f"双击开箱 「{tgt.name}」 · 距离 {dd}")
        self.log(
            f"自动宝箱双击 name={tgt.name!r} d={tgt.dist} "
            f"id={tgt.obj_id} xyz=({tgt.x},{tgt.y},{tgt.z})"
        )

        def worker() -> None:
            attach = None
            try:
                attach = open_attach_session(
                    sess.pid, log=lambda m: self._push("log", m)
                )
                res = open_specific_target(
                    attach,
                    tgt,
                    cfg,
                    hwnd=sess.hwnd,
                    stop_event=stop_ev,
                    skip_ids=self._active_skip_ids(),
                    skip_points=self._active_skip_points(),
                    path_failure_counts=self._active_path_failure_counts(),
                    log=lambda m: self._push("log", m),
                )
                if job == self._job_gen:
                    self._push("step", res)
            except Exception as e:
                if job == self._job_gen:
                    self._push("status", f"双击执行失败: {e}")
                    self._push("log", f"自动宝箱双击失败: {e}")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass
                if job == self._job_gen:
                    self._push("busy", False)

        threading.Thread(target=worker, daemon=True).start()

    def _on_hit_right(self, evt=None) -> None:
        """Right-click menu: blacklist selected match. @author by ak"""
        try:
            if evt is not None:
                idx = self.hit_list.nearest(evt.y)
                if idx >= 0:
                    self.hit_list.selection_clear(0, tk.END)
                    self.hit_list.selection_set(idx)
                    self.hit_list.activate(idx)
        except Exception:
            pass
        d = self._selected_hit()
        menu = tk.Menu(self, tearoff=0)
        tab = self._tab_var.get() if hasattr(self, "_tab_var") else "常规"
        if d:
            if tab == "黑名单":
                menu.add_command(
                    label="从黑名单移除",
                    command=lambda: self._unblacklist_hit(d),
                )
            else:
                menu.add_command(
                    label="加入黑名单",
                    command=lambda: self._blacklist_hit(d),
                )
            menu.add_command(
                label="寻路开箱",
                command=self._on_hit_double,
            )
        menu.add_separator()
        menu.add_command(label="清空黑名单", command=self._on_clear_blacklist)
        try:
            menu.tk_popup(evt.x_root, evt.y_root)
        finally:
            menu.grab_release()

    def _blacklist_hit(self, d: dict) -> None:
        """Add one match to skip map + fixed file. @author by ak"""
        from app.core.loot_fixed_blacklist import (
            FIXED_EXPIRE_S,
            add_fixed_entry,
            apply_fixed_to_skip_ids,
        )

        tgt = self._hit_to_target(d)
        skip = self._active_skip_ids()
        # Permanent: far-future TTL + JSON file (coord key survives obj_id churn).
        mark_unreachable(
            skip,
            tgt,
            ttl_s=float(FIXED_EXPIRE_S),
            reason="manual-fixed",
            log=self.log,
            skip_points=None,
            record_near_point=False,
        )
        if skip is not self._skip_ids:
            mark_unreachable(
                self._skip_ids,
                tgt,
                ttl_s=float(FIXED_EXPIRE_S),
                reason="manual-fixed",
                log=lambda _m: None,
                skip_points=None,
                record_near_point=False,
            )
        try:
            add_fixed_entry(d, note="manual-ui", log=self.log)
        except Exception as e:
            self.log(f"自动宝箱固定黑名单写入失败: {e}")
        dd = f"{tgt.dist:.1f}" if tgt.dist is not None else "?"
        self._set_status_line(f"已固定拉黑 「{tgt.name}」 · 距离 {dd}")
        self._append_status_log(f"固定拉黑 「{tgt.name}」 · 距离 {dd}")
        self.log(
            f"自动宝箱固定黑名单 + name={tgt.name!r} id={tgt.obj_id} "
            f"keys={target_skip_keys(tgt)}"
        )
        self._refresh_list_view()

    def _unblacklist_hit(self, d: dict) -> None:
        """Remove one match from skip map + fixed file. @author by ak"""
        from app.core.loot_fixed_blacklist import remove_fixed_entry

        tgt = self._hit_to_target(d)
        keys = target_skip_keys(tgt)
        for m in (self._skip_ids, self._active_skip_ids()):
            for k in keys:
                m.pop(int(k), None)
        if tgt.x is not None and tgt.z is not None:
            for pts in (self._skip_points, self._active_skip_points()):
                keep = [
                    p
                    for p in list(pts)
                    if abs(float(p[0]) - float(tgt.x)) > 0.5
                    or abs(float(p[1]) - float(tgt.z)) > 0.5
                ]
                pts[:] = keep
        try:
            remove_fixed_entry(d, log=self.log)
        except Exception as e:
            self.log(f"自动宝箱固定黑名单移除失败: {e}")
        self._set_status_line(f"已移出黑名单 「{tgt.name}」")
        self._append_status_log(f"移出黑名单 「{tgt.name}」")
        self._refresh_list_view()

    def _on_clear_blacklist(self) -> None:
        """Clear runtime skip only; fixed JSON kept. @author by ak"""
        from app.core.loot_fixed_blacklist import apply_fixed_to_skip_ids

        self._skip_ids.clear()
        self._skip_points.clear()
        self._path_failure_counts.clear()
        if self._runner is not None:
            try:
                self._runner.clear_blacklist()
            except Exception:
                pass
        # Re-apply permanent fixed bans after runtime clear.
        try:
            n = apply_fixed_to_skip_ids(self._skip_ids, log=self.log)
            if self._runner is not None:
                apply_fixed_to_skip_ids(self._runner.skip_ids(), log=lambda _m: None)
        except Exception:
            n = 0
        self._set_status_line(f"运行黑名单已清 · 固定保留 {n}")
        self._append_status_log(f"运行黑名单已清 · 固定保留 {n}")
        self.log(f"自动宝箱: 运行黑名单清空 fixed_keys~{n}")
        self._refresh_list_view()

    def _on_freeze_blacklist(self) -> None:
        """
        Snapshot current blacklist tab / skip rows into fixed JSON.

        Debug-only control (login「开启调试」). Use after manually banning
        ~50+ chests at a station (e.g. 十三站).
        @author by ak
        """
        if not self._is_debug_mode():
            self._set_status_line("固化忽略名单仅调试模式可用")
            return
        from app.core.loot_fixed_blacklist import (
            apply_fixed_to_skip_ids,
            fixed_blacklist_path,
            freeze_runtime_blacklist,
        )

        # Prefer runner map while running
        if self._runner is not None:
            try:
                self._skip_ids.update(self._runner.skip_ids())
            except Exception:
                pass
        rows = list(self._blacklist_rows())
        if not rows:
            # Fall back to all scan rows that currently match skip keys
            rows = [d for d in self._all_scan_rows if self._row_is_blacklisted(d)]
        try:
            total = freeze_runtime_blacklist(
                self._skip_ids,
                rows,
                note="freeze-ui",
                log=self.log,
            )
            apply_fixed_to_skip_ids(self._skip_ids, log=self.log)
            if self._runner is not None:
                apply_fixed_to_skip_ids(self._runner.skip_ids(), log=lambda _m: None)
            path = fixed_blacklist_path()
            self._set_status_line(f"已固化黑名单 {total} 个")
            self._append_status_log(f"固化黑名单 {total} 个 → {path}")
            self.log(f"自动宝箱: 固化黑名单 n={total} path={path}")
        except Exception as e:
            self._set_status_line(f"固化失败: {e}")
            self.log(f"自动宝箱固化黑名单失败: {e}")
        self._refresh_list_view()

    def _format_step_status(self, d: dict, *, open_ok_count: int | None = None) -> str:
        """
        One short human line for a super-loot step result.

        open_ok_count: cumulative successful opens/picks for this run (shown on ok).
        @author by ak
        """
        act = str(d.get("action") or "")
        msg = str(d.get("message") or "").strip()
        err = str(d.get("error") or "").strip()
        src = str(d.get("scan_source") or "")
        ok = bool(d.get("ok"))
        tgt = d.get("target") if isinstance(d.get("target"), dict) else {}
        dist = tgt.get("dist") if tgt else None
        dd = f"{float(dist):.1f}" if dist is not None else None

        if act in ("open", "path_open"):
            if ok:
                line = f"开完 · {dd}m" if dd else "开完"
            elif err == "no_cast_backoff" or "短暂跳过" in msg:
                line = "连续未见读条，短暂跳过"
            elif err == "no_cast" or "无读条" in msg:
                line = "未见读条，重试"
            elif err == "cast_timeout" or "读条超时" in msg:
                line = "读条超时"
            else:
                line = "开箱失败"
        elif act in ("pick", "path_pick"):
            # Ground pick is not open success; never treat as open count source.
            line = "已调用拾取" if ok else "取坐标失败"
        elif act == "path":
            line = f"寻路未到 · {dd}m" if dd else (msg or "寻路未到")
        elif act == "none":
            if "不可达" in msg or "过滤" in msg:
                line = "附近暂无可开"
            else:
                line = "附近暂无目标"
        elif act == "stop":
            line = "已停止"
        elif act == "error":
            if err == "bridge_cooldown" or "桥接冷却" in msg:
                line = "桥接冷却中"
            elif "桥接" in msg or "注入" in msg:
                line = "桥接未就绪"
            else:
                # Prefer short Chinese; drop raw English error spam
                if msg and any("一" <= ch <= "鿿" for ch in msg):
                    line = msg.split(":")[0][:40]
                else:
                    line = "出错"
        else:
            if msg and any("一" <= ch <= "鿿" for ch in msg):
                # Strip tid: / long English tails
                line = re.sub(r"\s*['\"]?tid:\d+['\"]?", "", msg)
                line = re.sub(r"\s+", " ", line).strip()[:48]
            else:
                line = act or "—"

        # One short chain hint only (open success count only).
        if ok and act in ("open", "path_open"):
            if "local_" in src:
                line = f"{line} · 连开"
            elif "work_" in src:
                line = f"{line} · 本片"
            if open_ok_count is not None and int(open_ok_count) > 0:
                line = f"{line} · 成功×{int(open_ok_count)}"
        return line

    def _apply_step(self, res) -> None:
        if isinstance(res, SuperLootStepResult):
            d = res.to_dict()
        elif isinstance(res, dict):
            d = res
        else:
            return
        act = str(d.get("action") or "")
        ok = bool(d.get("ok"))
        msg = str(d.get("message") or "")
        # Count only a real cast completion, not a bridge call or a retry.
        cast_done = ok and act in ("open", "path_open") and ("读条完成" in msg)
        if cast_done:
            self._open_ok_count = int(getattr(self, "_open_ok_count", 0) or 0) + 1
        n_ok = int(getattr(self, "_open_ok_count", 0) or 0)
        # History: show cumulative count on success; pin keeps count once only.
        line = self._format_step_status(
            d, open_ok_count=n_ok if (cast_done and n_ok) else None
        )
        if self._runner is not None and self._runner.is_running():
            base = self._format_step_status(d, open_ok_count=None)
            pin = f"开箱中 · 成功×{n_ok} · {base}" if n_ok else f"开箱中 · {base}"
            self._set_status_line(pin)
        else:
            self._set_status_line(line)
        self._append_status_log(line)
        self.log(
            f"自动宝箱 [{d.get('action')}] {d.get('message')} "
            f"matched={d.get('matched')} src={d.get('scan_source')}"
            + (f" open_ok={n_ok}" if cast_done else "")
        )
        # Refresh game title [捡箱子×N] after each successful open.
        if ok and act in ("open", "path_open", "pick", "path_pick"):
            try:
                master = self.winfo_toplevel()
                if hasattr(master, "_sync_game_title_marker"):
                    master._sync_game_title_marker()
            except Exception:
                pass
        tgt = d.get("target")
        if tgt and d.get("action") in ("open", "path_open", "pick", "path_pick", "path"):
            # merge into scan cache for list/blacklist views
            try:
                key = (
                    tgt.get("obj_id"),
                    round(float(tgt["x"]), 2) if tgt.get("x") is not None else None,
                    round(float(tgt["z"]), 2) if tgt.get("z") is not None else None,
                )
                replaced = False
                for i, row in enumerate(self._all_scan_rows):
                    rk = (
                        row.get("obj_id"),
                        round(float(row["x"]), 2) if row.get("x") is not None else None,
                        round(float(row["z"]), 2) if row.get("z") is not None else None,
                    )
                    if rk == key:
                        self._all_scan_rows[i] = dict(tgt)
                        replaced = True
                        break
                if not replaced:
                    self._all_scan_rows.append(dict(tgt))
            except Exception:
                pass
            self._refresh_list_view()

    def _on_scan(self) -> None:
        """
        Manual scan anytime — does not cancel loop / once jobs.

        @author by ak
        """
        sess = self._require_session()
        if not sess:
            return
        cfg = self._cfg_from_ui()
        # Bump only scan generation so stale scan results are dropped; keep loop
        self._scan_gen = getattr(self, "_scan_gen", 0) + 1
        scan_gen = self._scan_gen
        self._set_status_line(f"扫描… 「{cfg.item_name}」")
        self._append_status_log(f"开始扫描 「{cfg.item_name}」")
        self.log(f"自动宝箱扫描 name={cfg.item_name!r} pid={sess.pid}")

        def worker() -> None:
            attach = None
            try:
                from app.core.automove import read_scene_position

                attach = open_attach_session(sess.pid, log=lambda m: self._push("log", m))
                host_pos = None
                try:
                    sp = read_scene_position(attach, log=lambda m: self._push("log", m))
                    if sp.ok and sp.scene_pos:
                        host_pos = sp.scene_pos
                except Exception as e:
                    self._push("log", f"自动宝箱扫描 host 坐标跳过: {e}")
                # host_pos required for dist; without it list shows d=n/a and sort is random.
                hits = find_named_targets(
                    attach,
                    cfg,
                    host_pos=host_pos,
                    log=lambda m: self._push("log", m),
                )
                # Keep full raw list; tab filter applies black list view
                rows = [h.to_dict() if hasattr(h, "to_dict") else h for h in hits]
                if scan_gen != getattr(self, "_scan_gen", 0):
                    return
                self._push("hits", rows)
                if hits:
                    d0 = hits[0].dist
                    dd = f"{d0:.1f}" if d0 is not None else "n/a"
                    status = f"{len(hits)} 个 · 近 d={dd}"
                else:
                    status = f"未找到「{cfg.item_name}」"
                if self._runner is not None and self._runner.is_running():
                    status = f"开箱中 · {status}"
                self._push("status", status)
            except Exception as e:
                if scan_gen == getattr(self, "_scan_gen", 0):
                    self._push("status", f"扫描失败: {e}")
                    self._push("log", f"自动宝箱扫描失败: {e}")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_once(self) -> None:
        sess = self._require_session()
        if not sess:
            return
        if not self._cancel_running_work(reason="开箱一次"):
            return
        cfg = self._cfg_from_ui()
        job = self._job_gen
        stop_ev = self._work_stop
        self._busy = True
        self._set_status_line("开箱一次…")
        self._append_status_log(f"开箱一次 「{cfg.item_name}」")
        self.log(f"自动宝箱开箱一次 name={cfg.item_name!r} pid={sess.pid}")

        def worker() -> None:
            attach = None
            try:
                attach = open_attach_session(sess.pid, log=lambda m: self._push("log", m))
                res = super_loot_step(
                    attach,
                    cfg,
                    hwnd=sess.hwnd,
                    stop_event=stop_ev,
                    skip_ids=self._active_skip_ids(),
                    skip_points=self._active_skip_points(),
                    path_failure_counts=self._active_path_failure_counts(),
                    log=lambda m: self._push("log", m),
                )
                if job == self._job_gen:
                    self._push("step", res)
            except Exception as e:
                if job == self._job_gen:
                    self._push("status", f"执行失败: {e}")
                    self._push("log", f"自动宝箱失败: {e}")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass
                if job == self._job_gen:
                    self._push("busy", False)

        threading.Thread(target=worker, daemon=True).start()

    def _on_start(self) -> None:
        sess = self._require_session()
        if not sess:
            return
        if self._runner and self._runner.is_running():
            return
        if not self._cancel_running_work(reason="启动自动开箱"):
            return
        cfg = self._cfg_from_ui()
        cw = float(cfg.cast_wait_s)
        self._open_ok_count = 0
        self._set_status_line(f"开箱中 · 成功×0 · 「{cfg.item_name}」")
        self._append_status_log(
            f"自动开箱启动 「{cfg.item_name}」 · 读条上限 {cw:g}s · 计数清零"
        )
        self.log(f"自动宝箱自动开箱启动 name={cfg.item_name!r} pid={sess.pid}")

        def on_step(res: SuperLootStepResult) -> None:
            self._push("step", res)

        self._runner = SuperLootRunner(
            pid=sess.pid,
            hwnd=sess.hwnd,
            cfg=cfg,
            on_step=on_step,
            log=lambda m: self._push("log", m),
        )
        # inherit UI + fixed blacklist into runner
        try:
            from app.core.loot_fixed_blacklist import apply_fixed_to_skip_ids

            apply_fixed_to_skip_ids(self._skip_ids, log=lambda _m: None)
        except Exception:
            pass
        for k, v in list(self._skip_ids.items()):
            self._runner.skip_ids()[int(k)] = float(v)
        try:
            rpts = self._runner.skip_points()
            for p in list(self._skip_points):
                rpts.append(p)
        except Exception:
            pass
        self._runner.path_failure_counts().update(self._path_failure_counts)
        self._runner.start()
        self._set_running(True)

    def _on_stop(self) -> None:
        if self._runner is not None:
            # pull blacklist back to page
            try:
                self._skip_ids.update(self._runner.skip_ids())
            except Exception:
                pass
            try:
                for p in list(self._runner.skip_points()):
                    self._skip_points.append(p)
            except Exception:
                pass
            try:
                self._path_failure_counts.update(self._runner.path_failure_counts())
            except Exception:
                pass
            stopped = bool(self._runner.stop())
            if stopped or not self._runner.is_running():
                self._runner = None
        if self._work_stop is not None:
            self._work_stop.set()
        self._job_gen += 1
        self._busy = False
        n_ok = int(getattr(self, "_open_ok_count", 0) or 0)
        stop_line = f"已停止 · 成功×{n_ok}" if n_ok else "已停止"
        self._set_status_line(stop_line)
        self._append_status_log(stop_line)
        self.log(f"自动宝箱: 停止 open_ok={n_ok}")
        self._set_running(False)
        self._refresh_list_view()


class YaoluPage(FeaturePage):
    """
    九层妖楼 — Fuzhou open entry + captcha auto loop.

    @author by ak
    """

    title = "九层妖楼"
    key = "yaolu"
    REQUIRED_CLIENT_WIDTH = 1427
    REQUIRED_CLIENT_HEIGHT = 801

    # phase -> step index (0-based)
    _PHASE_STEP = {
        "gate": 0,
        "path_open": 1,
        "entry_cd": 1,
        "captcha": 2,
        "enter_wait": 3,
        "enter_ok": 3,
        "enter_fail": 3,
        "wait_return": 4,
        "wander": 5,
        "recover": 5,
    }

    def _build(self) -> None:
        self._maybe_bind_target(self)

        # Pack action row to bottom first so primary buttons stay visible.
        bar = action_bar(self)
        _yaolu_btn_w = 8
        _yaolu_btn_pad = (4, 0)
        self.btn_start = ttk.Button(
            bar,
            text="全自动",
            style="Compact.Accent.TButton",
            width=_yaolu_btn_w,
            command=self._on_enter,
        )
        self.btn_start.pack(side=tk.LEFT)
        self.btn_stop = ttk.Button(
            bar,
            text="停止",
            style="Compact.TButton",
            width=_yaolu_btn_w,
            command=self._on_stop,
            state=tk.DISABLED,
        )
        self.btn_stop.pack(side=tk.LEFT, padx=_yaolu_btn_pad)
        ttk.Button(
            bar,
            text="清理截图/缓存",
            style="Compact.TButton",
            width=13,
            command=self._on_cleanup_all,
        ).pack(side=tk.LEFT, padx=_yaolu_btn_pad)

        ttk.Label(bar, text="风控约束", style="Panel.Muted.TLabel").pack(
            side=tk.LEFT, padx=(10, 2)
        )
        saved_risk_pct = self.settings.get("yaolu_risk_constraint_pct")
        if saved_risk_pct is None:
            saved_risk_pct = load_yaolu_risk_constraint()
        saved_risk_pct = normalize_yaolu_risk_constraint(saved_risk_pct)
        self.settings["yaolu_risk_constraint_pct"] = saved_risk_pct
        self.var_risk_constraint = tk.StringVar(value=f"{saved_risk_pct}%")
        self.cmb_risk_constraint = ttk.Combobox(
            bar,
            textvariable=self.var_risk_constraint,
            values=("1%",) + tuple(f"{n}%" for n in range(10, 101, 10)),
            state="readonly",
            width=5,
        )
        self.cmb_risk_constraint.pack(side=tk.LEFT)
        self.cmb_risk_constraint.bind(
            "<<ComboboxSelected>>", self._on_risk_constraint_selected
        )

        # Daily enter-ok cap (0 = unlimited). Default 20.
        ttk.Label(bar, text="日上限", style="Panel.Muted.TLabel").pack(
            side=tk.LEFT, padx=(10, 2)
        )
        self.var_daily_limit = tk.StringVar(
            value=str(self.settings.get("yaolu_daily_success_limit", "20") or "20")
        )
        ent_lim = ttk.Entry(bar, textvariable=self.var_daily_limit, width=4)
        ent_lim.pack(side=tk.LEFT)
        ent_lim.bind("<FocusOut>", lambda _e: self._persist_daily_limit())
        ent_lim.bind("<Return>", lambda _e: self._persist_daily_limit())

        # Foreground real-mouse mode (off = background bridge path).
        self.var_foreground = tk.BooleanVar(
            value=bool(self.settings.get("yaolu_foreground", False))
        )
        ttk.Checkbutton(
            bar,
            text="前台",
            variable=self.var_foreground,
            command=self._on_toggle_foreground,
        ).pack(side=tk.LEFT, padx=(10, 0))

        # Scrollable main area so 前台参数展开后仍可看全。
        self._yaolu_scroll_outer, self._yaolu_body = make_scrollable_body(self)
        self._yaolu_scroll_outer.pack(fill=tk.BOTH, expand=True)
        body = self._yaolu_body

        self.box_flow = section(body, "进入流程")
        box_flow = self.box_flow
        box_flow.pack(fill=tk.X, expand=False, pady=(0, 0))
        self._step_vars: list[tk.StringVar] = []
        self._step_labels = (
            "检测福州城 · 组队且队长",
            "寻路打开入口并等待验证码弹出",
            "识别验证码并点选确认",
            "等待进图结果",
            "等待回到福州城",
            "随机远离入口后继续",
        )
        for i, text_step in enumerate(self._step_labels, start=1):
            row = ttk.Frame(box_flow, style="Panel.TFrame")
            row.pack(fill=tk.X, pady=2)
            mark = tk.StringVar(value="○")
            self._step_vars.append(mark)
            ttk.Label(row, textvariable=mark, style="Panel.Mono.TLabel", width=2).pack(
                side=tk.LEFT
            )
            ttk.Label(row, text=f"{i}. {text_step}", style="Panel.TLabel").pack(
                side=tk.LEFT
            )

        self.box_st = section(body, "状态")
        self.box_st.pack(fill=tk.X, pady=(6, 0))
        self.var_status = tk.StringVar(value="待命")
        ttk.Label(
            self.box_st,
            textvariable=self.var_status,
            style="Panel.Mono.TLabel",
            wraplength=360,
            justify=tk.LEFT,
        ).pack(anchor="w", fill=tk.X)

        # Low-priority tunables: pack at bottom of scroll body only when 前台 on.
        self.box_fg = section(body, "前台鼠标参数（可选微调）")
        self._build_foreground_panel(self.box_fg)
        self.after(0, self._sync_foreground_panel_visible)

        self._runner: YaoluRunner | None = None
        self._current_round = 0
        self._ui_q: queue.Queue = queue.Queue()
        # Confirmed 进本 successes this run (status 成功×N / game title 妖楼×N).
        self._enter_ok_count = 0
        self._daily_ok_count = 0
        self._risk_profile = "entry_focus_v2"
        self._schedule_ui_drain(120)
        self.refresh_sessions()

    def _on_cleanup_all(self) -> None:
        """Clear game screenshots and captcha debug cache with one action."""
        folder = game_screenshots_dir(None)
        result = cleanup_game_screenshots(
            folder=folder,
            keep_newest=0,
            older_than_s=None,
            log=self.log,
        )
        cache_files = cleanup_captcha_debug(
            ttl_s=0.0,
            force_all=True,
            log=self.log,
        )
        if result.get("ok"):
            freed_mb = int(result.get("bytes_freed") or 0) / (1024 * 1024)
            msg = (
                f"已清理截图 {int(result.get('removed') or 0)} 个/"
                f"{freed_mb:.1f}MB，识别缓存 {int(cache_files)} 个"
            )
        else:
            msg = (
                f"截图清理失败: {result.get('error')}；"
                f"识别缓存已清理 {int(cache_files)} 个"
            )
        self.var_status.set(msg)
        self.log(f"九层妖楼: {msg}")

    def refresh_sessions(self) -> None:
        if hasattr(self, "_refresh_combo"):
            self._refresh_combo()

    def _reset_steps(self) -> None:
        for v in self._step_vars:
            v.set("○")

    def _mark_step(self, idx: int, mark: str = "…") -> None:
        if 0 <= idx < len(self._step_vars):
            for i, v in enumerate(self._step_vars):
                if i < idx:
                    if v.get() == "…":
                        v.set("✓")
                elif i == idx:
                    v.set(mark)

    def _persist_daily_limit(self) -> None:
        """Save optional daily enter-ok cap (0 = unlimited)."""
        raw = str(getattr(self, "var_daily_limit", tk.StringVar(value="20")).get() or "20").strip()
        try:
            n = int(float(raw))
        except ValueError:
            n = 20
        if n < 0:
            n = 0
        self.var_daily_limit.set(str(n))
        self.settings["yaolu_daily_success_limit"] = n
        self._save_yaolu_settings()

    def _risk_constraint_from_ui(self) -> int:
        var = getattr(self, "var_risk_constraint", None)
        raw = var.get() if var is not None else "80%"
        return normalize_yaolu_risk_constraint(raw, default=80)

    def _persist_risk_constraint(self) -> int:
        """Normalize and persist the pacing selection for future launches."""
        pct = self._risk_constraint_from_ui()
        self.var_risk_constraint.set(f"{pct}%")
        self.settings["yaolu_risk_constraint_pct"] = pct
        try:
            save_yaolu_risk_constraint(pct)
        except Exception as e:
            self.log(f"九层妖楼保存风控约束失败: {e}")
        self._save_yaolu_settings()
        return pct

    def _on_risk_constraint_selected(self, _event=None) -> None:
        pct = self._persist_risk_constraint()
        msg = f"风控约束已设为 {pct}%（越低等待越短，下次自动复用）"
        self.var_status.set(msg)
        self.log(f"九层妖楼: {msg}")

    def _daily_limit_from_ui(self) -> int:
        raw = str(getattr(self, "var_daily_limit", tk.StringVar(value="20")).get() or "20").strip()
        try:
            n = int(float(raw))
        except ValueError:
            n = 20
        return max(0, n)

    def _save_yaolu_settings(self) -> None:
        """Persist Yaolu page settings if host supports it."""
        try:
            if hasattr(self, "_save_settings"):
                self._save_settings()
        except Exception:
            pass

    def _yaolu_setting(self, key: str, default):
        """Read settings with default."""
        try:
            if key in self.settings:
                return self.settings.get(key)
        except Exception:
            pass
        return default

    def _num_var(self, key: str, default, *, as_int: bool = False) -> tk.StringVar:
        """Create StringVar bound to a numeric setting."""
        raw = self._yaolu_setting(key, default)
        if as_int:
            try:
                val = str(int(float(raw)))
            except (TypeError, ValueError):
                val = str(int(default))
        else:
            try:
                val = str(float(raw))
                if val.endswith(".0"):
                    val = val[:-2]
            except (TypeError, ValueError):
                val = str(default)
        return tk.StringVar(value=val)

    def _parse_float(self, var: tk.StringVar | None, default: float, *, lo: float | None = None, hi: float | None = None) -> float:
        try:
            v = float(str(var.get() if var is not None else default).strip())
        except (TypeError, ValueError, AttributeError):
            v = float(default)
        if lo is not None:
            v = max(float(lo), v)
        if hi is not None:
            v = min(float(hi), v)
        return v

    def _parse_int(self, var: tk.StringVar | None, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
        try:
            v = int(float(str(var.get() if var is not None else default).strip()))
        except (TypeError, ValueError, AttributeError):
            v = int(default)
        if lo is not None:
            v = max(int(lo), v)
        if hi is not None:
            v = min(int(hi), v)
        return v

    def _build_foreground_panel(self, parent) -> None:
        """
        Foreground real-mouse fine-tune controls (bottom of page; only when 前台 checked).

        Defaults follow 2026-07-25 risk analysis (slide + wider timing).
        @author by ak
        """
        # Defaults aligned with YaoluConfig entry_focus_v2 mouse profile.
        self.var_fg_hold_min = self._num_var("yaolu_fg_hold_min_ms", 60, as_int=True)
        self.var_fg_hold_max = self._num_var("yaolu_fg_hold_max_ms", 220, as_int=True)
        self.var_fg_gap = self._num_var("yaolu_fg_gap_s", 0.45)
        self.var_fg_gap_jit = self._num_var("yaolu_fg_gap_jitter_s", 0.90)
        self.var_fg_pre_first_min = self._num_var("yaolu_fg_pre_first_min_s", 0.35)
        self.var_fg_pre_first_max = self._num_var("yaolu_fg_pre_first_max_s", 1.20)
        self.var_fg_pre_confirm_min = self._num_var("yaolu_fg_pre_confirm_min_s", 0.45)
        self.var_fg_pre_confirm_max = self._num_var("yaolu_fg_pre_confirm_max_s", 1.40)
        self.var_fg_slide = tk.BooleanVar(
            value=bool(self._yaolu_setting("yaolu_fg_slide", True))
        )
        self.var_fg_slide_min_steps = self._num_var(
            "yaolu_fg_slide_min_steps", 5, as_int=True
        )
        self.var_fg_slide_max_steps = self._num_var(
            "yaolu_fg_slide_max_steps", 14, as_int=True
        )
        self.var_fg_slide_dur_min = self._num_var("yaolu_fg_slide_dur_min_s", 0.25)
        self.var_fg_slide_dur_max = self._num_var("yaolu_fg_slide_dur_max_s", 0.85)
        self.var_fg_approach = tk.BooleanVar(
            value=bool(self._yaolu_setting("yaolu_fg_approach", True))
        )

        hint = ttk.Label(
            parent,
            text="勾选前台=真鼠标并切前台；不勾选=后台可最小化。",
            style="Panel.Muted.TLabel",
            wraplength=420,
            justify=tk.LEFT,
        )
        hint.pack(anchor="w", pady=(0, 4))

        def _row(label: str):
            fr = ttk.Frame(parent, style="Panel.TFrame")
            fr.pack(fill=tk.X, pady=1)
            ttk.Label(fr, text=label, style="Panel.Muted.TLabel", width=12).pack(
                side=tk.LEFT
            )
            return fr

        def _ent(parent_row, var, width=5):
            e = ttk.Entry(parent_row, textvariable=var, width=width)
            e.pack(side=tk.LEFT, padx=(0, 4))
            e.bind("<FocusOut>", lambda _e: self._persist_foreground_settings())
            e.bind("<Return>", lambda _e: self._persist_foreground_settings())
            return e

        r1 = _row("按住 ms")
        _ent(r1, self.var_fg_hold_min)
        ttk.Label(r1, text="~", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        _ent(r1, self.var_fg_hold_max)
        ttk.Label(r1, text="(建议 60~220)", style="Panel.Muted.TLabel").pack(
            side=tk.LEFT, padx=(4, 0)
        )

        r2 = _row("格间距 s")
        _ent(r2, self.var_fg_gap)
        ttk.Label(r2, text="+抖动", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        _ent(r2, self.var_fg_gap_jit)

        r3 = _row("首点前 s")
        _ent(r3, self.var_fg_pre_first_min)
        ttk.Label(r3, text="~", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        _ent(r3, self.var_fg_pre_first_max)

        r4 = _row("确认前 s")
        _ent(r4, self.var_fg_pre_confirm_min)
        ttk.Label(r4, text="~", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        _ent(r4, self.var_fg_pre_confirm_max)

        r5 = _row("轨迹")
        ttk.Checkbutton(
            r5,
            text="滑轨",
            variable=self.var_fg_slide,
            command=self._persist_foreground_settings,
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            r5,
            text="首点接近",
            variable=self.var_fg_approach,
            command=self._persist_foreground_settings,
        ).pack(side=tk.LEFT, padx=(8, 0))

        r6 = _row("滑轨步数")
        _ent(r6, self.var_fg_slide_min_steps, width=4)
        ttk.Label(r6, text="~", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        _ent(r6, self.var_fg_slide_max_steps, width=4)
        ttk.Label(r6, text="时长s", style="Panel.Muted.TLabel").pack(
            side=tk.LEFT, padx=(6, 2)
        )
        _ent(r6, self.var_fg_slide_dur_min, width=5)
        ttk.Label(r6, text="~", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        _ent(r6, self.var_fg_slide_dur_max, width=5)

        ttk.Button(
            parent,
            text="恢复推荐参数",
            style="Compact.TButton",
            width=12,
            command=self._reset_foreground_defaults,
        ).pack(anchor="e", pady=(4, 0))

    def _sync_foreground_panel_visible(self) -> None:
        """Show/hide foreground param panel at bottom of scroll body."""
        on = bool(getattr(self, "var_foreground", tk.BooleanVar(value=False)).get())
        box = getattr(self, "box_fg", None)
        if box is None:
            return
        try:
            box.pack_forget()
        except Exception:
            pass
        if on:
            # Low priority: always pack last so 流程/状态 keep top focus.
            box.pack(fill=tk.X, pady=(8, 4), side=tk.TOP)
            try:
                # Refresh scrollregion after show/hide.
                body = getattr(self, "_yaolu_body", None)
                if body is not None:
                    body.update_idletasks()
                    body.event_generate("<Configure>")
            except Exception:
                pass

    def _on_toggle_foreground(self) -> None:
        """Toggle 前台 real-mouse mode and persist."""
        on = bool(self.var_foreground.get())
        self.settings["yaolu_foreground"] = on
        self._sync_foreground_panel_visible()
        self._save_yaolu_settings()
        msg = (
            "前台模式：真鼠标+轨迹（游戏窗口需可见）"
            if on
            else "后台模式：Bridge 注入点击（可最小化）"
        )
        self.var_status.set(msg)
        self.log(f"九层妖楼: {msg}")

    def _reset_foreground_defaults(self) -> None:
        """Restore recommended foreground mouse parameters from risk analysis."""
        defaults = {
            "var_fg_hold_min": "60",
            "var_fg_hold_max": "220",
            "var_fg_gap": "0.45",
            "var_fg_gap_jit": "0.90",
            "var_fg_pre_first_min": "0.35",
            "var_fg_pre_first_max": "1.20",
            "var_fg_pre_confirm_min": "0.45",
            "var_fg_pre_confirm_max": "1.40",
            "var_fg_slide_min_steps": "8",
            "var_fg_slide_max_steps": "22",
            "var_fg_slide_dur_min": "0.25",
            "var_fg_slide_dur_max": "0.85",
        }
        for name, val in defaults.items():
            var = getattr(self, name, None)
            if var is not None:
                var.set(val)
        if getattr(self, "var_fg_slide", None) is not None:
            self.var_fg_slide.set(True)
        if getattr(self, "var_fg_approach", None) is not None:
            self.var_fg_approach.set(True)
        self._persist_foreground_settings()
        self.var_status.set("已恢复前台推荐参数（entry_focus_v2）")
        self.log("九层妖楼: 前台参数已恢复推荐值")

    def _persist_foreground_settings(self) -> None:
        """Normalize and save foreground mouse tunables."""
        hold_lo = self._parse_int(getattr(self, "var_fg_hold_min", None), 60, lo=20, hi=800)
        hold_hi = self._parse_int(getattr(self, "var_fg_hold_max", None), 220, lo=20, hi=1000)
        if hold_hi < hold_lo:
            hold_hi = hold_lo
        gap = self._parse_float(getattr(self, "var_fg_gap", None), 0.45, lo=0.05, hi=5.0)
        gap_jit = self._parse_float(
            getattr(self, "var_fg_gap_jit", None), 0.90, lo=0.0, hi=5.0
        )
        pf_lo = self._parse_float(
            getattr(self, "var_fg_pre_first_min", None), 0.35, lo=0.0, hi=5.0
        )
        pf_hi = self._parse_float(
            getattr(self, "var_fg_pre_first_max", None), 1.20, lo=0.0, hi=8.0
        )
        if pf_hi < pf_lo:
            pf_hi = pf_lo
        pc_lo = self._parse_float(
            getattr(self, "var_fg_pre_confirm_min", None), 0.45, lo=0.0, hi=5.0
        )
        pc_hi = self._parse_float(
            getattr(self, "var_fg_pre_confirm_max", None), 1.40, lo=0.0, hi=8.0
        )
        if pc_hi < pc_lo:
            pc_hi = pc_lo
        st_lo = self._parse_int(
            getattr(self, "var_fg_slide_min_steps", None), 8, lo=2, hi=40
        )
        st_hi = self._parse_int(
            getattr(self, "var_fg_slide_max_steps", None), 22, lo=2, hi=60
        )
        if st_hi < st_lo:
            st_hi = st_lo
        sd_lo = self._parse_float(
            getattr(self, "var_fg_slide_dur_min", None), 0.25, lo=0.02, hi=3.0
        )
        sd_hi = self._parse_float(
            getattr(self, "var_fg_slide_dur_max", None), 0.85, lo=0.02, hi=4.0
        )
        if sd_hi < sd_lo:
            sd_hi = sd_lo

        # Reflect normalized values back to UI.
        mapping = {
            "var_fg_hold_min": str(hold_lo),
            "var_fg_hold_max": str(hold_hi),
            "var_fg_gap": f"{gap:g}",
            "var_fg_gap_jit": f"{gap_jit:g}",
            "var_fg_pre_first_min": f"{pf_lo:g}",
            "var_fg_pre_first_max": f"{pf_hi:g}",
            "var_fg_pre_confirm_min": f"{pc_lo:g}",
            "var_fg_pre_confirm_max": f"{pc_hi:g}",
            "var_fg_slide_min_steps": str(st_lo),
            "var_fg_slide_max_steps": str(st_hi),
            "var_fg_slide_dur_min": f"{sd_lo:g}",
            "var_fg_slide_dur_max": f"{sd_hi:g}",
        }
        for name, val in mapping.items():
            var = getattr(self, name, None)
            if var is not None and str(var.get()) != val:
                var.set(val)

        self.settings["yaolu_foreground"] = bool(
            getattr(self, "var_foreground", tk.BooleanVar(value=False)).get()
        )
        self.settings["yaolu_fg_hold_min_ms"] = hold_lo
        self.settings["yaolu_fg_hold_max_ms"] = hold_hi
        self.settings["yaolu_fg_gap_s"] = gap
        self.settings["yaolu_fg_gap_jitter_s"] = gap_jit
        self.settings["yaolu_fg_pre_first_min_s"] = pf_lo
        self.settings["yaolu_fg_pre_first_max_s"] = pf_hi
        self.settings["yaolu_fg_pre_confirm_min_s"] = pc_lo
        self.settings["yaolu_fg_pre_confirm_max_s"] = pc_hi
        self.settings["yaolu_fg_slide"] = bool(
            getattr(self, "var_fg_slide", tk.BooleanVar(value=True)).get()
        )
        self.settings["yaolu_fg_approach"] = bool(
            getattr(self, "var_fg_approach", tk.BooleanVar(value=True)).get()
        )
        self.settings["yaolu_fg_slide_min_steps"] = st_lo
        self.settings["yaolu_fg_slide_max_steps"] = st_hi
        self.settings["yaolu_fg_slide_dur_min_s"] = sd_lo
        self.settings["yaolu_fg_slide_dur_max_s"] = sd_hi
        self._save_yaolu_settings()

    def _cfg_from_ui(self) -> YaoluConfig:
        """
        Build runner config from shared settings + Yaolu page controls.

        - 未勾选「前台」: 后台 Bridge 注入（可最小化），使用 v2 默认拟人时序/滑轨。
        - 勾选「前台」: 真鼠标 + 轨迹，参数取自前台面板微调。
        debug_save_crops follows login「开启调试」; off = memory-only crops.
        @author by ak
        """
        url = str(DEFAULT_CAPTCHA_BASE_URL).strip()
        if not bool(self.settings.get("captcha_api_key_dirty")):
            try:
                from app.core.captcha_prefs import load_captcha_api_key

                saved_key = load_captcha_api_key()
                if saved_key:
                    self.settings["captcha_api_key"] = saved_key
            except Exception as e:
                self.log(f"九层妖楼读取已保存识别 Key 失败: {e}")
        _raw_key = self.settings.get("captcha_api_key")
        if _raw_key is None:
            _raw_key = DEFAULT_CAPTCHA_API_KEY
        key = str(_raw_key or "").strip()
        debug_on = bool(
            self.settings.get(
                "debug_mode",
                self.settings.get("debug_save_crops", False),
            )
        )
        # Persist latest UI numbers before build.
        try:
            self._persist_daily_limit()
        except Exception:
            pass
        try:
            risk_pct = self._persist_risk_constraint()
        except Exception:
            risk_pct = 80
        fg = bool(getattr(self, "var_foreground", tk.BooleanVar(value=False)).get())
        if fg:
            try:
                self._persist_foreground_settings()
            except Exception:
                pass

        cfg = YaoluConfig(
            entry_name=ENTRY_ITEM_NAME,
            captcha_base_url=url or DEFAULT_CAPTCHA_BASE_URL,
            captcha_api_key=key if key else DEFAULT_CAPTCHA_API_KEY,
            login_token=str(self.settings.get("login_token") or "").strip(),
            debug_save_crops=debug_on,
            debug_random_answer=bool(self.settings.get("yaolu_debug_random_answer", False)),
            daily_success_limit=self._daily_limit_from_ui(),
        )
        if fg:
            # Real OS mouse path (visible cursor move + click).
            cfg.allow_cursor_click = True
            cfg.captcha_prefer_real_mouse = True
            cfg.captcha_humanize_clicks = True
            cfg.captcha_hold_min_ms = self._parse_int(
                getattr(self, "var_fg_hold_min", None), 60, lo=20, hi=800
            )
            cfg.captcha_hold_max_ms = self._parse_int(
                getattr(self, "var_fg_hold_max", None), 220, lo=20, hi=1000
            )
            if cfg.captcha_hold_max_ms < cfg.captcha_hold_min_ms:
                cfg.captcha_hold_max_ms = cfg.captcha_hold_min_ms
            cfg.captcha_click_gap_s = self._parse_float(
                getattr(self, "var_fg_gap", None), 0.45, lo=0.05, hi=5.0
            )
            cfg.captcha_click_gap_jitter_s = self._parse_float(
                getattr(self, "var_fg_gap_jit", None), 0.90, lo=0.0, hi=5.0
            )
            cfg.captcha_pre_first_click_min_s = self._parse_float(
                getattr(self, "var_fg_pre_first_min", None), 0.35, lo=0.0, hi=5.0
            )
            cfg.captcha_pre_first_click_max_s = self._parse_float(
                getattr(self, "var_fg_pre_first_max", None), 1.20, lo=0.0, hi=8.0
            )
            cfg.captcha_pre_confirm_min_s = self._parse_float(
                getattr(self, "var_fg_pre_confirm_min", None), 0.45, lo=0.0, hi=5.0
            )
            cfg.captcha_pre_confirm_max_s = self._parse_float(
                getattr(self, "var_fg_pre_confirm_max", None), 1.40, lo=0.0, hi=8.0
            )
            cfg.captcha_slide_enabled = bool(
                getattr(self, "var_fg_slide", tk.BooleanVar(value=True)).get()
            )
            cfg.captcha_approach_slide = bool(
                getattr(self, "var_fg_approach", tk.BooleanVar(value=True)).get()
            )
            cfg.captcha_slide_min_steps = self._parse_int(
                getattr(self, "var_fg_slide_min_steps", None), 8, lo=2, hi=40
            )
            cfg.captcha_slide_max_steps = self._parse_int(
                getattr(self, "var_fg_slide_max_steps", None), 22, lo=2, hi=60
            )
            if cfg.captcha_slide_max_steps < cfg.captcha_slide_min_steps:
                cfg.captcha_slide_max_steps = cfg.captcha_slide_min_steps
            cfg.captcha_slide_duration_min_s = self._parse_float(
                getattr(self, "var_fg_slide_dur_min", None), 0.25, lo=0.02, hi=3.0
            )
            cfg.captcha_slide_duration_max_s = self._parse_float(
                getattr(self, "var_fg_slide_dur_max", None), 0.85, lo=0.02, hi=4.0
            )
            if cfg.captcha_slide_duration_max_s < cfg.captcha_slide_duration_min_s:
                cfg.captcha_slide_duration_max_s = cfg.captcha_slide_duration_min_s
            # Tag profile for audit when foreground path is active.
            cfg.profile_id = "entry_focus_v2_fg"
            cfg.profile_note = "攻防20260725：前台真鼠标+可调轨迹；绿区日限"
        else:
            # Background: keep inject/bridge path (no real mouse / no OS cursor).
            cfg.allow_cursor_click = False
            cfg.captcha_prefer_real_mouse = False
            # v2 defaults already carry slide + wider timing on YaoluConfig.
            cfg.profile_id = "entry_focus_v2"
            cfg.profile_note = "攻防20260725：后台Bridge+轨迹点选；绿区日限"
        return apply_yaolu_risk_constraint(cfg, risk_pct)

    def _push(self, kind: str, payload=None) -> None:
        self._ui_q.put((kind, payload))

    def _drain_ui(self) -> None:
        try:
            while True:
                kind, payload = self._ui_q.get_nowait()
                if kind == "status":
                    self.var_status.set(str(payload or ""))
                elif kind == "log":
                    self.log(str(payload or ""))
                elif kind == "event":
                    self._apply_event(payload)
                elif kind == "running":
                    self._set_running(bool(payload))
        except queue.Empty:
            pass
        self._schedule_ui_drain(120)

    def _sync_enter_ok_title(self) -> None:
        """
        Refresh game window [妖楼×N] after confirmed enter success.

        @author by ak
        """
        try:
            master = self.winfo_toplevel()
            if hasattr(master, "_sync_game_title_marker"):
                master._sync_game_title_marker()
        except Exception:
            pass

    def _note_enter_ok_count(self, detail: dict | None = None) -> int:
        """
        Mirror runner enter-success count into page state.

        Only called on phase enter_ok (scene already in 妖楼).
        @author by ak
        """
        n = None
        if isinstance(detail, dict):
            try:
                if detail.get("ok_count") is not None:
                    n = int(detail.get("ok_count"))
            except (TypeError, ValueError):
                n = None
        if n is None:
            n = int(getattr(self, "_enter_ok_count", 0) or 0) + 1
        self._enter_ok_count = max(0, int(n))
        self._sync_enter_ok_title()
        return int(self._enter_ok_count)

    def _apply_event(self, ev) -> None:
        """Map runner event to step marks + status. @author by ak"""
        if isinstance(ev, YaoluStepEvent):
            d = ev.to_dict()
        elif isinstance(ev, dict):
            d = ev
        else:
            return
        phase = str(d.get("phase") or "")
        msg = str(d.get("message") or phase)
        ok = bool(d.get("ok", True))
        try:
            self._current_round = max(
                int(self._current_round), int((d.get("detail") or {}).get("round") or 0)
            )
        except (TypeError, ValueError):
            pass
        detail = d.get("detail") if isinstance(d.get("detail"), dict) else {}
        if detail.get("profile_id"):
            self._risk_profile = str(detail.get("profile_id") or self._risk_profile)
        if detail.get("daily_ok") is not None:
            try:
                self._daily_ok_count = max(0, int(detail.get("daily_ok")))
            except (TypeError, ValueError):
                pass
        if phase == "enter_ok" and ok:
            # +1 only when runner proved scene enter (not 答案正确 alone).
            self._note_enter_ok_count(detail)
        status_line = self._format_runner_status(phase, msg)
        self.var_status.set(status_line)
        self.log(f"九层妖楼 [{phase}] {msg}")
        # Formal user log is intentionally sparse. Full phase/countdown/recovery
        # detail remains in the production file log above for diagnostics.
        try:
            user_line = self._format_yaolu_user_log(
                phase,
                msg,
                ok=ok,
                detail=detail,
            )
            if user_line:
                self.user_log(user_line, category=CAT_YAOLU, dedupe_s=3.0)
        except Exception:
            pass
        if phase in ("stopped", "stop"):
            self._set_running(False)
            if detail.get("reason") == "captcha_api_key_rejected":
                messagebox.showwarning(
                    "九层妖楼",
                    msg,
                    parent=self.winfo_toplevel(),
                )
            return
        # Transient status/attach chatter must not flash step ×.
        if phase in ("status", "attach", "round"):
            return
        idx = self._PHASE_STEP.get(phase)
        if idx is None:
            return
        if phase in ("enter_ok",) and ok:
            self._mark_step(idx, "✓")
        elif phase in ("enter_fail", "stopped"):
            self._mark_step(idx, "×")
        elif phase == "recover" and not ok:
            self._mark_step(idx, "×")
        elif phase in ("path_open", "captcha", "gate", "enter_wait", "recover", "wander") and ok:
            self._mark_step(idx, "…")
        elif not ok and phase in ("path_open", "captcha", "gate"):
            # Keep … while recovering; only terminal fails mark ×.
            self._mark_step(idx, "…")
        else:
            self._mark_step(idx, "…")

    def _format_yaolu_user_log(
        self,
        phase: str,
        message: str,
        *,
        ok: bool,
        detail: dict | None = None,
    ) -> str | None:
        """Return one operator-facing milestone, or None for diagnostic noise."""
        phase = str(phase or "")
        text = str(message or "").strip()
        detail = detail if isinstance(detail, dict) else {}
        round_no = int(getattr(self, "_current_round", 0) or 0)
        round_prefix = f"第{round_no}轮 · " if round_no > 0 else ""

        if phase in {
            "attach",
            "status",
            "entry_cd",
            "enter_wait",
            "wait_return",
            "wander",
            "recover",
        }:
            return None

        if phase == "round":
            daily = int(detail.get("daily_ok") or 0)
            limit = int(detail.get("daily_limit") or 0)
            daily_text = f"今日{daily}/{limit}" if limit > 0 else f"今日{daily}"
            try:
                risk_pct = self._risk_constraint_from_ui()
            except Exception:
                risk_pct = 80
            return f"第{round_no or '?'}轮开始 · {daily_text} · 风控{risk_pct}%"

        if phase == "captcha":
            if "识别验证码并点选确认" in text:
                return None
            if "已提交" in text:
                animal_hit = re.search(r"动物=([^\s]+)", text)
                conf_hit = re.search(r"conf=([0-9.]+)", text)
                bits = [f"{round_prefix}验证码已提交"]
                if animal_hit:
                    bits.append(animal_hit.group(1))
                if conf_hit:
                    try:
                        bits.append(f"置信度{float(conf_hit.group(1)) * 100:.0f}%")
                    except ValueError:
                        pass
                return " · ".join(bits)
            if "确认未生效" in text or "确认按钮未生效" in text:
                return f"{round_prefix}确认按钮未生效，已恢复并稍后重试"
            if "置信度过低" in text or "已拒绝提交" in text:
                return f"{round_prefix}验证码置信度过低，未提交"
            low = text.lower()
            if "timeout" in low or "timed out" in low or "请求超时" in text:
                return f"{round_prefix}验证码识别超时，将自动重试"
            if any(
                key in text
                for key in (
                    "识别失败",
                    "连接失败",
                    "Key 不存在",
                    "已经失效",
                    "连续失败",
                )
            ):
                return f"{round_prefix}{text[:120]}"
            return None

        if phase == "enter_ok" and ok:
            run_ok = int(getattr(self, "_enter_ok_count", 0) or 0)
            daily = int(detail.get("daily_ok") or getattr(self, "_daily_ok_count", 0) or 0)
            limit = int(detail.get("daily_limit") or 0)
            daily_text = f"今日{daily}/{limit}" if limit > 0 else f"今日{daily}"
            return f"第{round_no or '?'}轮进入成功 · 成功×{run_ok} · {daily_text}"

        if phase == "enter_fail":
            blob = text.lower()
            if "答案错误" in text:
                summary = "验证码答案错误，稍后重试"
            elif "答案正确" in text and ("未进图" in text or "尚未进图" in text):
                summary = "答案正确但未进图，进入免答题重开"
            elif "神罚" in text or "捕羽" in text:
                summary = text[:120]
            elif "timeout" in blob or "超时" in text:
                summary = "进图等待超时，已恢复并稍后重试"
            else:
                summary = "未进入妖楼，已恢复并稍后重试"
            return f"{round_prefix}{summary}"

        if phase == "gate":
            if ok or detail.get("reason") == "team_status":
                return None
            summary = re.sub(r"\s*\([^)]*(?:id|team)=?[^)]*\)", "", text)
            summary = re.sub(r"\s+(?:team|自己id|队长id)=0x[0-9A-Fa-f]+", "", summary)
            return f"{round_prefix}入口条件未满足 · {summary[:100]}"

        if phase == "path_open":
            if ok:
                return None
            reason = str(detail.get("reason") or "")
            if reason == "no_captcha_dialog_retry" or "就地重试" in text or "再开入口" in text:
                return None
            if reason == "no_captcha_dialog" or "内存未确认验证码弹窗" in text:
                return f"{round_prefix}入口未弹验证码，已恢复并稍后重试"
            if "残留验证码" in text:
                return f"{round_prefix}残留验证码未清除，本轮已跳过"
            return None

        if phase == "entry_retry":
            if ok or not any(key in text for key in ("验证码", "门控", "拦截")):
                return None
            return f"{round_prefix}{text[:100]}"

        if phase in ("stopped", "stop"):
            return f"妖楼已停止 · {text[:140]}" if text else "妖楼已停止"

        return None

    def _format_runner_status(self, phase: str, message: str) -> str:
        """Show current round and a readable Chinese phase in the compact status box."""
        labels = {
            "round": "开始新一轮",
            "gate": "检查入口条件",
            "path_open": "寻路并打开入口",
            "entry_cd": "入口冷却",
            "captcha": "识别验证码",
            "enter_wait": "等待进图结果",
            "enter_ok": "已进入妖楼",
            "enter_fail": "进图失败",
            "wait_return": "等待回福州",
            "wander": "寻路到入口",
            "recover": "失败恢复",
            "stopped": "已停止",
            "status": "运行中",
        }
        text = str(message or "")
        lower = text.lower()
        if "timed out" in lower or "timeout" in lower:
            text = "识别服务请求超时，将自动重试"
        elif "connection refused" in lower or "urlopen error" in lower:
            text = "识别服务连接失败，请检查识别服务与网络"
        elif "http error" in lower:
            text = f"识别失败：{text}"
        # Long settle waits: countdown is the message — label it clearly.
        if (
            "等待稳定" in text
            or "不稳定状态" in text
            or "切图后等待" in text
            or "到达后等待" in text
        ):
            label = "等待稳定"
        else:
            label = labels.get(phase, phase or "运行中")
        if phase == "round":
            base = f"第 {self._current_round or '?'} 轮：{label}"
        else:
            prefix = f"第 {self._current_round} 轮 · " if self._current_round else ""
            base = f"{prefix}{label}：{text}"
        n_ok = int(getattr(self, "_enter_ok_count", 0) or 0)
        daily = int(getattr(self, "_daily_ok_count", 0) or 0)
        profile = str(getattr(self, "_risk_profile", "") or "")
        limit = 0
        try:
            limit = int(self._daily_limit_from_ui())
        except Exception:
            limit = 0
        bits = []
        if n_ok > 0:
            bits.append(f"成功×{n_ok}")
        if daily > 0 or limit > 0:
            bits.append(f"今日{daily}" + (f"/{limit}" if limit > 0 else ""))
        if profile:
            bits.append(profile)
        prefix = " · ".join(bits)
        if prefix and "成功×" not in base and profile not in base:
            if phase in ("stopped", "stop") and label == "已停止":
                return f"已停止 · {prefix}" if bits else "已停止"
            return f"{prefix} · {base}" if bits else base
        if n_ok > 0 and "成功×" not in base:
            if phase in ("stopped", "stop"):
                return f"已停止 · 成功×{n_ok}" if label == "已停止" else f"{base} · 成功×{n_ok}"
            return f"成功×{n_ok} · {base}"
        return base

    def _on_enter(self) -> None:
        sess = self._require_session()
        if not sess:
            return
        if self._runner and self._runner.is_running():
            return
        cfg = self._cfg_from_ui()
        credential_error = yaolu_start_credential_error(cfg)
        if credential_error is not None:
            _reason, msg = credential_error
            self.var_status.set(msg)
            self.log(f"九层妖楼: {msg}")
            messagebox.showwarning("九层妖楼", msg, parent=self.winfo_toplevel())
            self._set_running(False)
            return
        if not self._ensure_required_window_size(sess):
            self._set_running(False)
            return
        self._reset_steps()
        self._current_round = 0
        self._enter_ok_count = 0
        self._daily_ok_count = 0
        self._persist_daily_limit()
        self._risk_profile = str(getattr(cfg, "profile_id", "") or "entry_focus_v2")
        lim = int(getattr(cfg, "daily_success_limit", 0) or 0)
        prof = str(getattr(cfg, "profile_id", "") or "")
        mode = "前台真鼠标" if bool(getattr(cfg, "captcha_prefer_real_mouse", False)) else "后台注入"
        risk_pct = int(getattr(cfg, "risk_constraint_pct", 80) or 80)
        self.var_status.set(
            f"成功×0 · {prof} · 风控{risk_pct}% · {mode} · "
            f"日上限{'不限' if lim <= 0 else lim} · 全自动启动 pid={sess.pid}"
        )
        self._sync_enter_ok_title()
        fg_on = bool(getattr(cfg, "captcha_prefer_real_mouse", False))
        if fg_on and sess.hwnd:
            try:
                from app.core.win_capture import ensure_foreground

                ok_fg = ensure_foreground(
                    int(sess.hwnd),
                    retries=4,
                    settle_s=0.08,
                    force=True,
                    log=self.log,
                )
                self.log(
                    f"九层妖楼前台启动切焦点 hwnd=0x{int(sess.hwnd):X} ok={ok_fg}"
                )
            except Exception as e:
                self.log(f"九层妖楼前台启动切焦点失败: {e}")
        self.log(
            f"九层妖楼全自动启动 pid={sess.pid} hwnd=0x{sess.hwnd:X} "
            f"api={cfg.captcha_base_url} foreground={fg_on} "
            f"key_hint=({api_key_fingerprint(cfg.captcha_api_key)}) "
            f"token={'yes' if str(cfg.login_token or '').strip() else 'no'} "
            f"slide={getattr(cfg, 'captcha_slide_enabled', False)} "
            f"daily_limit={lim} risk_constraint={risk_pct}% profile={prof}"
        )

        def on_event(ev: YaoluStepEvent) -> None:
            self._push("event", ev)

        self._runner = YaoluRunner(
            pid=sess.pid,
            hwnd=sess.hwnd,
            cfg=cfg,
            on_event=on_event,
            log=lambda m: self._push("log", m),
        )
        self._runner.start()
        self._set_running(True)

    def _ensure_required_window_size(self, sess: GameSession) -> bool:
        """Require the captcha baseline before starting the Yaolu runner."""
        target = (self.REQUIRED_CLIENT_WIDTH, self.REQUIRED_CLIENT_HEIGHT)
        current = get_client_size(sess.hwnd)
        current_text = (
            f"{current[0]}x{current[1]}" if current is not None else "未知"
        )
        dpi = get_window_dpi(sess.hwnd) if sess.hwnd else 96
        scale_pct = int(round(dpi * 100 / 96.0))
        result = prompt_and_normalize_game_window(
            sess.hwnd,
            sess.pid,
            client_width=target[0],
            client_height=target[1],
            ask=lambda prompt: messagebox.askyesno(
                "九层妖楼",
                prompt,
                parent=self.winfo_toplevel(),
            ),
            prompt=(
                f"当前游戏客户区为 {current_text}（显示器 DPI={dpi} / {scale_pct}%）。\n"
                f"妖楼验证码基线需要物理客户区 {target[0]}x{target[1]}，"
                f"是否按当前 DPI 调整窗口边框并继续？"
            ),
            log=self.log,
        )
        if result.reason == "client_rect_unavailable":
            msg = "无法读取游戏窗口客户区尺寸，已拒绝启动妖楼"
        elif result.reason == "user_declined":
            msg = f"未同意调整为 {target[0]}x{target[1]}，已拒绝启动妖楼"
        elif not result.ok:
            msg = (
                f"游戏窗口无法调整为 {target[0]}x{target[1]}，"
                f"当前为 {result.after_client or result.before_client}，已拒绝启动妖楼"
            )
        else:
            return True
        self.var_status.set(msg)
        self.log(f"九层妖楼: {msg} reason={result.reason}")
        if result.reason != "user_declined":
            messagebox.showwarning("九层妖楼", msg, parent=self.winfo_toplevel())
        return False

    def _on_stop(self) -> None:
        if self._runner is not None:
            stopped = bool(self._runner.stop())
            if stopped or not self._runner.is_running():
                self._runner = None
        n_ok = int(getattr(self, "_enter_ok_count", 0) or 0)
        self.var_status.set(f"已停止 · 成功×{n_ok}" if n_ok else "已停止")
        self.log(f"九层妖楼: 停止 成功×{n_ok}")
        self._set_running(False)




class SettingsPage(FeaturePage):
    """
    Shared settings: captcha API + mock user/expiry + skill-cancel lab.

    Mouse clicker / Shift reticle / background keys live on InputMkPage.

    @author by ak
    """

    title = "快捷设置"
    key = "settings"

    def _build(self) -> None:
        self._skill_cancel_runner: SkillCancelLoopRunner | None = None
        self._ui_q: queue.Queue = queue.Queue()
        self._hang_busy = False
        self._youfeng_hook_busy = False
        self._youfeng_hook_enabled = False
        self._wanzi_packet_busy = False
        self._wanzi_packet_enabled = False
        self._jianglong_runner: JianglongRotationRunner | None = None
        self._jianglong_tick_job = None
        self._jianglong_test_enabled = False
        self._jianglong_entry_gate_state = ""

        # Sticky footer first so 保存 stays visible while the body scrolls.
        foot = action_bar(self)
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(
            foot, text="保存设置", style="Accent.TButton", width=10, command=self._on_save
        ).pack(side=tk.LEFT)
        ttk.Button(
            foot, text="刷新账号", width=10, command=self.refresh_sessions
        ).pack(side=tk.LEFT, padx=(6, 0))
        self.var_status = tk.StringVar(value="")
        ttk.Label(
            foot, textvariable=self.var_status, style="Panel.Muted.TLabel"
        ).pack(side=tk.LEFT, padx=(12, 0))

        # Scrollable body so skill-cancel section is not clipped on short windows.
        outer, body = make_scrollable_body(self)
        outer.pack(fill=tk.BOTH, expand=True)

        box_user = section(body, "账号信息")
        box_user.pack(fill=tk.X, pady=(0, 6))
        self.var_user = tk.StringVar(value="-")
        self.var_expire = tk.StringVar(value="-")
        self.var_key_mask = tk.StringVar(value="-")
        row1 = ttk.Frame(box_user, style="Panel.TFrame")
        row1.pack(fill=tk.X, pady=1)
        ttk.Label(row1, text="用户", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        ttk.Label(row1, textvariable=self.var_user, style="Panel.TLabel").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        row2 = ttk.Frame(box_user, style="Panel.TFrame")
        row2.pack(fill=tk.X, pady=1)
        ttk.Label(row2, text="到期", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        ttk.Label(row2, textvariable=self.var_expire, style="Panel.Mono.TLabel").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        row3 = ttk.Frame(box_user, style="Panel.TFrame")
        row3.pack(fill=tk.X, pady=1)
        ttk.Label(row3, text="卡密", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        ttk.Label(row3, textvariable=self.var_key_mask, style="Panel.Mono.TLabel").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )

        # Service base URL is env / packaged profile only. Authentication uses
        # the in-memory login token, so no separate identify key is exposed.
        self.settings["captcha_base_url"] = DEFAULT_CAPTCHA_BASE_URL


        box_cap = section(body, "识别服务")
        box_cap.pack(fill=tk.X, pady=(0, 6))
        # Service base URL is env / packaged profile only (not user-editable / not shown).
        self.settings["captcha_base_url"] = DEFAULT_CAPTCHA_BASE_URL
        row_k = ttk.Frame(box_cap, style="Panel.TFrame")
        row_k.pack(fill=tk.X, pady=1)
        ttk.Label(row_k, text="识别密钥", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        # Keep empty string in prod (do not coerce with `or` to a non-empty default).
        _ck = self.settings.get("captcha_api_key")
        if _ck is None:
            _ck = DEFAULT_CAPTCHA_API_KEY
        self.var_api_key = tk.StringVar(value=str(_ck if _ck is not None else ""))
        ttk.Entry(row_k, textvariable=self.var_api_key, show="*").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0)
        )
        lbl_get_key = ttk.Label(
            row_k,
            text="获取识别密钥",
            style="Link.TLabel",
            cursor="hand2",
        )
        lbl_get_key.pack(side=tk.LEFT, padx=(8, 0))

        def _open_captcha_key_url(_event=None) -> None:
            url = (DEFAULT_CAPTCHA_BASE_URL or "").strip()
            if not url:
                return
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception as e:
                self.log(f"打开识别服务链接失败: {e}")

        lbl_get_key.bind("<Button-1>", _open_captcha_key_url)
        lbl_get_key.bind(
            "<Enter>",
            lambda _e: lbl_get_key.configure(foreground=C["accent_hi"]),
        )
        lbl_get_key.bind(
            "<Leave>",
            lambda _e: lbl_get_key.configure(foreground=C["accent"]),
        )
        # Captcha key is shared with YaoluPage. Sync while typing so
        # switching pages without pressing the general save button is safe.
        # Base URL is fixed from env/packaged profile (not editable).
        self.settings["captcha_base_url"] = DEFAULT_CAPTCHA_BASE_URL
        def _sync_captcha_key_input(*_args) -> None:
            self.settings["captcha_api_key"] = (self.var_api_key.get() or "").strip()
            self.settings["captcha_api_key_dirty"] = True

        self.var_api_key.trace_add("write", _sync_captcha_key_input)

# ---- Task sync + cloud (merged) ----
        box_role = section(body, "任务同步")
        box_role.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            box_role,
            text="本机多开选主/副控；跨电脑勾云控并填主控名。",
            style="Panel.Muted.TLabel",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6))

        self._ROLE_LABEL = {
            ROLE_NONE: "无控",
            ROLE_MASTER: "主控",
            ROLE_SLAVE: "副控",
        }
        self._ROLE_LABEL_REV = {v: k for k, v in self._ROLE_LABEL.items()}
        # 启动/重建：从角色文件全量同步会话（主副控/队内控/注入/挂机），
        # 保证「上次选的」在源码重启后仍生效（配置以 roles/{rid}/ 文件为基准）。
        try:
            _rid0 = self._current_role_id()
            if _rid0:
                from app.core.hang_settings import sync_role_prefs_to_settings

                sync_role_prefs_to_settings(self.settings, _rid0)
        except Exception:
            pass
        saved_role = str(self.settings.get("task_control_role") or ROLE_NONE).lower()
        if saved_role not in (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE):
            saved_role = ROLE_NONE
        self.var_task_role = tk.StringVar(value=saved_role)
        self.var_role_label = tk.StringVar(
            value=self._ROLE_LABEL.get(saved_role, "无控")
        )
        self._role_cards: dict[str, dict] = {}

        row_role = ttk.Frame(box_role, style="Panel.TFrame")
        row_role.pack(fill=tk.X)
        ttk.Label(row_role, text="控制角色", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        self.cmb_task_role = ttk.Combobox(
            row_role,
            textvariable=self.var_role_label,
            values=["无控", "主控", "副控"],
            state="readonly",
            width=12,
        )
        self.cmb_task_role.pack(side=tk.LEFT, padx=(4, 12))
        self.cmb_task_role.bind("<<ComboboxSelected>>", self._on_role_combo)

        self.var_cloud_enabled = tk.BooleanVar(
            value=bool(self.settings.get("cloud_control_enabled"))
        )
        self.chk_cloud_enabled = ttk.Checkbutton(
            row_role,
            text="启用云控",
            variable=self.var_cloud_enabled,
            command=self._on_cloud_ui_changed,
        )
        self.chk_cloud_enabled.pack(side=tk.LEFT)
        if self.settings.get("cloud_control_available") is False:
            self.var_cloud_enabled.set(False)
            self.chk_cloud_enabled.state(["disabled"])
        self.var_team_control_enabled = tk.BooleanVar(
            value=bool(self.settings.get("team_control_enabled"))
        )
        self.chk_team_control = ttk.Checkbutton(
            row_role,
            text="队内控",
            variable=self.var_team_control_enabled,
            command=self._on_team_control_ui_changed,
        )
        self.chk_team_control.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_team_ping = ttk.Button(
            row_role,
            text="PING",
            width=5,
            command=self._on_team_ping,
        )
        self.btn_team_ping.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_one_click_slaves = ttk.Button(
            row_role,
            text="一键副控",
            width=8,
            command=self._on_one_click_slaves,
        )
        # Visibility: only when 主控 selected (managed by _update_one_click_slaves_btn).

        # Master name: show when cloud on + 副控 (join channel); also for 主控 publish.
        self.row_cloud_name = ttk.Frame(box_role, style="Panel.TFrame")
        ttk.Label(
            self.row_cloud_name, text="主控名称", style="Panel.Muted.TLabel", width=8
        ).pack(side=tk.LEFT)
        self.var_cloud_master_name = tk.StringVar(
            value=str(self.settings.get("cloud_control_master_name") or "")
        )
        ttk.Entry(self.row_cloud_name, textvariable=self.var_cloud_master_name).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0)
        )

        self.var_role_status = tk.StringVar(value="")
        self.lbl_role_status = ttk.Label(
            box_role,
            textvariable=self.var_role_status,
            style="Panel.Mono.TLabel",
            wraplength=520,
        )
        self.lbl_role_status.pack(anchor="w", pady=(6, 0))
        self.var_cloud_status = tk.StringVar(value="")
        self.lbl_cloud_status = ttk.Label(
            box_role,
            textvariable=self.var_cloud_status,
            style="Panel.Mono.TLabel",
            wraplength=520,
        )
        self.lbl_cloud_status.pack(anchor="w", pady=(2, 0))
        self.var_team_control_status = tk.StringVar(value="")
        self.lbl_team_control_status = ttk.Label(
            box_role,
            textvariable=self.var_team_control_status,
            style="Panel.Mono.TLabel",
            wraplength=520,
        )
        self.lbl_team_control_status.pack(anchor="w", pady=(2, 0))

        # Cloud HTTP only after 保存; UI changes only toggle name row / tip.
        self.var_cloud_enabled.trace_add(
            "write", lambda *_a: self._on_cloud_ui_changed()
        )
        self.var_cloud_master_name.trace_add(
            "write", lambda *_a: self._on_cloud_ui_changed()
        )
        self.var_team_control_enabled.trace_add(
            "write", lambda *_a: self._on_team_control_ui_changed()
        )

        self._apply_task_role_ui(saved_role, register=True, apply_cloud=False)
        self._on_cloud_ui_changed()
        self._cloud_ui_dirty = False
        self._cloud_status_job = self.after(1000, self._tick_cloud_status)
        self._team_control_status_job = self.after(
            1000, self._tick_team_control_status
        )

        # ---- 技能取消后摇：序列宏（对齐罗技录制）+ 可选取消通道 ----
        self._CANCEL_WHEN_UI = {
            "不取消": CANCEL_WHEN_NEVER,
            "每轮取消": CANCEL_WHEN_ALWAYS,
            "忙碌时取消": CANCEL_WHEN_BUSY,
        }
        self._CANCEL_WHEN_REV = {v: k for k, v in self._CANCEL_WHEN_UI.items()}
        self._CAST_MODE_UI = {
            "仅协议取消": CAST_MODE_NONE,
            "序列(宏/临时)": CAST_MODE_SEQUENCE,
            "单键(临时)": CAST_MODE_SINGLE,
            "序列(罗技)": CAST_MODE_SEQUENCE,
            "单键": CAST_MODE_SINGLE,
        }
        self._CAST_MODE_REV = {
            CAST_MODE_NONE: "仅协议取消",
            CAST_MODE_SEQUENCE: "序列(宏/临时)",
            CAST_MODE_SINGLE: "单键(临时)",
        }
        self._DELIVERY_UI = {
            "桥接后台": DELIVERY_BRIDGE,
            "系统级发送": DELIVERY_FOREGROUND,
            # legacy labels still map for load
            "桥接后台(试验)": DELIVERY_BRIDGE,
            "前台SendInput": DELIVERY_FOREGROUND,
        }
        # Prefer canonical labels when reversing.
        self._DELIVERY_REV = {
            DELIVERY_BRIDGE: "桥接后台",
            DELIVERY_FOREGROUND: "系统级发送",
        }

        # ---- 挂机设置（按角色落盘 hang_prefs.json）----
        box_hang = section(body, "挂机设置")
        box_hang.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            box_hang,
            text="按角色保存，重启仍有效。群控只同步开关，不同步详细设置。",
            style="Panel.Muted.TLabel",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6))

        _cid0 = ""
        try:
            _cid0 = self._hang_char_id_key()
        except Exception:
            _cid0 = ""
        _hcfg = get_hang_config(self.settings, char_id=_cid0 or None)
        _hp = {"radius": int(_hcfg.radius)}

        row_hm = ttk.Frame(box_hang, style="Panel.TFrame")
        row_hm.pack(fill=tk.X, pady=1)
        ttk.Label(row_hm, text="挂机模式", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        self._HANG_MODE_UI = {"普通模式": 0, "副本模式": 1}
        self._HANG_MODE_REV = {0: "普通模式", 1: "副本模式"}
        _mode0 = int(getattr(_hcfg, "mode", 1) or 1)
        self.var_hang_mode = tk.StringVar(
            value=self._HANG_MODE_REV.get(_mode0, "副本模式")
        )
        self.cmb_hang_mode = ttk.Combobox(
            row_hm,
            textvariable=self.var_hang_mode,
            values=["普通模式", "副本模式"],
            state="readonly",
            width=12,
        )
        self.cmb_hang_mode.pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(row_hm, text="半径", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        self.var_hang_radius = tk.StringVar(
            value=str(int(_hp.get("radius", DEFAULT_HANG_RADIUS) or DEFAULT_HANG_RADIUS))
        )
        ttk.Spinbox(
            row_hm,
            from_=1,
            to=255,
            width=5,
            textvariable=self.var_hang_radius,
        ).pack(side=tk.LEFT, padx=(4, 0))

        row_hf = ttk.Frame(box_hang, style="Panel.TFrame")
        row_hf.pack(fill=tk.X, pady=2)
        self.var_hang_pickup = tk.BooleanVar(value=bool(_hcfg.enable_pickup))
        ttk.Checkbutton(
            row_hf, text="开启拾取（组队自动需求）", variable=self.var_hang_pickup
        ).pack(side=tk.LEFT)
        self.var_hang_empty = tk.BooleanVar(value=bool(_hcfg.empty_skill))
        ttk.Checkbutton(
            row_hf,
            text="无技能挂机",
            variable=self.var_hang_empty,
            command=self._on_hang_empty_toggle,
        ).pack(side=tk.LEFT, padx=(12, 0))

        row_hw = ttk.Frame(box_hang, style="Panel.TFrame")
        row_hw.pack(fill=tk.X, pady=2)
        self._row_hang_wanzi = row_hw
        self.var_hang_wanzi_neigong = tk.BooleanVar(
            value=bool(getattr(_hcfg, "wanzi_neigong_hang", DEFAULT_WANZI_NEIGONG_HANG))
        )
        self.var_hang_wanzi_waigong = tk.BooleanVar(
            value=bool(getattr(_hcfg, "wanzi_waigong_hang", DEFAULT_WANZI_WAIGONG_HANG))
        )
        self.var_hang_youfeng = tk.BooleanVar(
            value=bool(getattr(_hcfg, "youfeng_hang", DEFAULT_YOUFENG_HANG))
        )
        ttk.Checkbutton(
            row_hw,
            text="内功丸子挂机",
            variable=self.var_hang_wanzi_neigong,
            command=self._on_hang_wanzi_neigong_toggle,
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            row_hw,
            text="外功丸子挂机",
            variable=self.var_hang_wanzi_waigong,
            command=self._on_hang_wanzi_waigong_toggle,
        ).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Checkbutton(
            row_hw,
            text="华山 -> 有凤来仪",
            variable=self.var_hang_youfeng,
            command=self._on_hang_youfeng_toggle,
        ).pack(side=tk.LEFT, padx=(12, 0))
        self.frm_hang_wanzi_iv = ttk.Frame(row_hw, style="Panel.TFrame")
        self.frm_hang_wanzi_iv.pack(side=tk.LEFT, padx=(12, 0))
        ttk.Label(
            self.frm_hang_wanzi_iv, text="释放间隔", style="Panel.Muted.TLabel"
        ).pack(side=tk.LEFT)
        self.var_hang_wanzi_iv = tk.StringVar(
            value=str(
                int(
                    getattr(
                        _hcfg,
                        "wanzi_interval_ms",
                        _hp.get("wanzi_interval_ms", DEFAULT_WANZI_INTERVAL_MS),
                    )
                    or DEFAULT_WANZI_INTERVAL_MS
                )
            )
        )
        self.spn_hang_wanzi_iv = ttk.Spinbox(
            self.frm_hang_wanzi_iv,
            from_=1 if is_dev_build() else int(WANZI_INTERVAL_MIN_PROD_MS),
            to=60000,
            width=6,
            textvariable=self.var_hang_wanzi_iv,
        )
        self.spn_hang_wanzi_iv.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(
            self.frm_hang_wanzi_iv, text="ms", style="Panel.Muted.TLabel"
        ).pack(side=tk.LEFT, padx=(2, 0))

        # These cadence values deliberately exist only in the source/dev UI.
        # They are session-only experiments, never character preferences.
        self._wanzi_dev_cadence_enabled = bool(is_dev_build())
        self.frm_hang_wanzi_dev = ttk.Frame(box_hang, style="Panel.TFrame")
        if self._wanzi_dev_cadence_enabled:
            ttk.Label(
                self.frm_hang_wanzi_dev,
                text="开发丸子调度：每轮前",
                style="Panel.Muted.TLabel",
            ).pack(side=tk.LEFT)
            self.var_hang_wanzi_low_s = tk.StringVar(
                value=f"{float(getattr(_hcfg, 'wanzi_low_rate_seconds', 6.0)):g}"
            )
            ttk.Spinbox(
                self.frm_hang_wanzi_dev,
                from_=0,
                to=120,
                increment=0.5,
                width=5,
                textvariable=self.var_hang_wanzi_low_s,
            ).pack(side=tk.LEFT, padx=(3, 0))
            ttk.Label(self.frm_hang_wanzi_dev, text="秒 · 每秒", style="Panel.Muted.TLabel").pack(
                side=tk.LEFT, padx=(2, 5)
            )
            self.var_hang_wanzi_low_rate = tk.StringVar(
                value=f"{float(getattr(_hcfg, 'wanzi_low_rate_pairs_per_second', 3.0)):g}"
            )
            ttk.Spinbox(
                self.frm_hang_wanzi_dev,
                from_=0.1,
                to=20,
                increment=0.1,
                width=5,
                textvariable=self.var_hang_wanzi_low_rate,
            ).pack(side=tk.LEFT)
            ttk.Label(self.frm_hang_wanzi_dev, text="次 · 随后持续", style="Panel.Muted.TLabel").pack(
                side=tk.LEFT, padx=(2, 0)
            )
            self.var_hang_wanzi_active_s = tk.StringVar(
                value=f"{float(getattr(_hcfg, 'wanzi_active_window_s', 300.0)):g}"
            )
            ttk.Spinbox(
                self.frm_hang_wanzi_dev,
                from_=1,
                to=7200,
                increment=1,
                width=6,
                textvariable=self.var_hang_wanzi_active_s,
            ).pack(side=tk.LEFT, padx=(3, 0))
            ttk.Label(self.frm_hang_wanzi_dev, text="秒", style="Panel.Muted.TLabel").pack(
                side=tk.LEFT, padx=(2, 0)
            )

        row_jl = ttk.Frame(box_hang, style="Panel.TFrame")
        row_jl.pack(fill=tk.X, pady=2)
        self.var_hang_jianglong = tk.BooleanVar(
            value=bool(getattr(_hcfg, "jianglong_hang", DEFAULT_JIANGLONG_HANG))
        )
        ttk.Checkbutton(
            row_jl,
            text="武尊堂 -> 自动降龙(轮流控场)",
            variable=self.var_hang_jianglong,
            command=self._on_hang_jianglong_toggle,
        ).pack(side=tk.LEFT)
        self.var_hang_auto_open_monster = tk.BooleanVar(
            value=bool(getattr(_hcfg, "auto_open_monster", DEFAULT_AUTO_OPEN_MONSTER))
        )
        ttk.Checkbutton(
            row_jl,
            text="自动开怪",
            variable=self.var_hang_auto_open_monster,
            command=self._update_hang_tip,
        ).pack(side=tk.LEFT, padx=(14, 0))
        ttk.Label(row_jl, text="排数", style="Panel.Muted.TLabel").pack(
            side=tk.LEFT, padx=(12, 0)
        )
        self.var_hang_open_monster_rows = tk.StringVar(
            value=str(int(getattr(_hcfg, "open_monster_rows", 0) or 0))
        )
        ttk.Combobox(
            row_jl,
            textvariable=self.var_hang_open_monster_rows,
            values=["0", "1", "2", "3"],
            state="readonly",
            width=4,
        ).pack(side=tk.LEFT, padx=(4, 0))

        row_hs = ttk.Frame(box_hang, style="Panel.TFrame")
        row_hs.pack(fill=tk.X, pady=2)
        self.var_hang_skip_story = tk.BooleanVar(
            value=bool(
                getattr(_hcfg, "skip_dungeon_story", DEFAULT_SKIP_DUNGEON_STORY)
            )
        )
        ttk.Checkbutton(
            row_hs,
            text="副本跳过剧情",
            variable=self.var_hang_skip_story,
            command=self._update_hang_tip,
        ).pack(side=tk.LEFT)
        self.var_hang_ignore_dungeon_stuck = tk.BooleanVar(
            value=bool(getattr(_hcfg, "ignore_dungeon_stuck", False))
        )
        ttk.Checkbutton(
            row_hs,
            text="忽略副本卡怪",
            variable=self.var_hang_ignore_dungeon_stuck,
            command=self._on_hang_ignore_dungeon_stuck_toggle,
        ).pack(side=tk.LEFT, padx=(12, 0))

        self.var_hang_tip = tk.StringVar(value="")
        ttk.Label(
            box_hang,
            textvariable=self.var_hang_tip,
            style="Panel.Muted.TLabel",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(2, 2))

        row_hr = ttk.Frame(box_hang, style="Panel.TFrame")
        row_hr.pack(fill=tk.X, pady=2)
        self.var_hang_auto_repair = tk.BooleanVar(
            value=bool(_hp.get("auto_repair", True))
        )
        ttk.Checkbutton(
            row_hr, text="自动维修", variable=self.var_hang_auto_repair
        ).pack(side=tk.LEFT)
        ttk.Label(row_hr, text="耐久≤", style="Panel.Muted.TLabel").pack(
            side=tk.LEFT, padx=(8, 0)
        )
        self.var_hang_repair_pct = tk.StringVar(
            value=str(int(float(_hp.get("repair_below_pct", 50) or 50)))
        )
        ttk.Spinbox(
            row_hr,
            from_=1,
            to=100,
            width=5,
            textvariable=self.var_hang_repair_pct,
        ).pack(side=tk.LEFT, padx=(2, 0))
        ttk.Label(row_hr, text="%", style="Panel.Muted.TLabel").pack(side=tk.LEFT)

        self.var_hang_auto_vitality = tk.BooleanVar(
            value=bool(_hp.get("auto_vitality", True))
        )
        ttk.Checkbutton(
            row_hr, text="自动补活力", variable=self.var_hang_auto_vitality
        ).pack(side=tk.LEFT, padx=(16, 0))
        ttk.Label(row_hr, text="活力≤", style="Panel.Muted.TLabel").pack(
            side=tk.LEFT, padx=(8, 0)
        )
        self.var_hang_vitality_pct = tk.StringVar(
            value=str(int(float(_hp.get("vitality_below_pct", 20) or 20)))
        )
        ttk.Spinbox(
            row_hr,
            from_=1,
            to=100,
            width=5,
            textvariable=self.var_hang_vitality_pct,
        ).pack(side=tk.LEFT, padx=(2, 0))
        ttk.Label(row_hr, text="%", style="Panel.Muted.TLabel").pack(side=tk.LEFT)

        row_hb = ttk.Frame(box_hang, style="Panel.TFrame")
        row_hb.pack(fill=tk.X, pady=(6, 2))
        self.btn_hang_start = ttk.Button(
            row_hb, text="开启挂机", width=8, command=self._on_hang_start
        )
        self.btn_hang_start.pack(side=tk.LEFT)
        self.btn_hang_stop = ttk.Button(
            row_hb, text="关闭挂机", width=8, command=self._on_hang_stop
        )
        self.btn_hang_stop.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(
            row_hb, text="刷新状态", width=8, command=self._on_hang_refresh
        ).pack(side=tk.LEFT, padx=(4, 0))
        self.btn_youfeng_hook = ttk.Button(
            row_hb,
            text="开启有凤",
            width=8,
            command=self._on_youfeng_hook_toggle,
        )
        self.btn_youfeng_hook.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_wanzi_packet = ttk.Button(
            row_hb,
            text="开启释放丸子",
            width=10,
            command=self._on_wanzi_packet_toggle,
        )
        self.btn_wanzi_packet.pack(side=tk.LEFT, padx=(4, 0))

        row_hb2 = ttk.Frame(box_hang, style="Panel.TFrame")
        row_hb2.pack(fill=tk.X, pady=(2, 2))
        self.btn_hang_wall = ttk.Button(
            row_hb2,
            text="穿墙开",
            width=8,
            command=self._on_hang_wall_toggle,
        )
        self.btn_hang_wall.pack(side=tk.LEFT, padx=(0, 0))
        self.btn_jianglong_test = ttk.Button(
            row_hb2,
            text="开启降龙编排",
            width=12,
            command=self._on_jianglong_test_toggle,
        )
        self.btn_jianglong_test.pack(side=tk.LEFT, padx=(4, 0))
        try:
            from app.core.wall_clip import wall_clip_state
            st = wall_clip_state(int(self._fixed_pid or 0)) if self._fixed_pid else None
            self.btn_hang_wall.configure(text="穿墙关" if st else "穿墙开")
        except Exception:
            pass
        self.var_hang_status = tk.StringVar(value="挂机：未刷新")
        ttk.Label(
            box_hang,
            textvariable=self.var_hang_status,
            style="Panel.Mono.TLabel",
            wraplength=520,
        ).pack(anchor="w", pady=(4, 0))
        try:
            self.var_hang_mode.trace_add("write", self._update_hang_tip)
            self.var_hang_empty.trace_add("write", self._update_hang_tip)
            self.var_hang_auto_vitality.trace_add("write", self._update_hang_tip)
            self.var_hang_wanzi_neigong.trace_add("write", self._update_hang_tip)
            self.var_hang_wanzi_waigong.trace_add("write", self._update_hang_tip)
            self.var_hang_wanzi_iv.trace_add("write", self._update_hang_tip)
            if self._wanzi_dev_cadence_enabled:
                self.var_hang_wanzi_low_s.trace_add("write", self._update_hang_tip)
                self.var_hang_wanzi_low_rate.trace_add("write", self._update_hang_tip)
                self.var_hang_wanzi_active_s.trace_add("write", self._update_hang_tip)
            self.var_hang_youfeng.trace_add("write", self._update_hang_tip)
            self.var_hang_jianglong.trace_add("write", self._update_hang_tip)
            self.var_hang_skip_story.trace_add("write", self._update_hang_tip)
            self.var_hang_ignore_dungeon_stuck.trace_add("write", self._update_hang_tip)
            self._update_hang_wanzi_ui()
            self._update_hang_tip()
            try:
                self._tick_jianglong()
            except Exception:
                pass
        except Exception:
            pass

        # ---- 计划任务（每天到点启动整队列一次）----


        box_sched = section(body, "计划任务")
        box_sched.pack(fill=tk.X, pady=(0, 6))
        row_sched_en = ttk.Frame(box_sched, style="Panel.TFrame")
        row_sched_en.pack(fill=tk.X)
        self.var_sched_enabled = tk.BooleanVar(
            value=bool(self.settings.get(SETTING_SCHEDULE_ENABLED, False))
        )
        self.chk_sched_enabled = ttk.Checkbutton(
            row_sched_en,
            text="启用每天定时",
            variable=self.var_sched_enabled,
        )
        self.chk_sched_enabled.pack(side=tk.LEFT)
        ttk.Label(
            row_sched_en,
            text="保存本端计划；勾选启用后到点执行（本端是队长会自然带队）",
            style="Panel.Muted.TLabel",
            wraplength=380,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, padx=(8, 0))
        row_sched = ttk.Frame(box_sched, style="Panel.TFrame")
        row_sched.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row_sched, text="每天", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        _sh, _sm = get_schedule_hm(self.settings)
        self.var_sched_hour = tk.StringVar(value=str(_sh))
        self.var_sched_minute = tk.StringVar(value=str(_sm))
        ttk.Spinbox(
            row_sched,
            from_=0,
            to=23,
            width=4,
            textvariable=self.var_sched_hour,
        ).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(row_sched, text="时", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        ttk.Spinbox(
            row_sched,
            from_=0,
            to=59,
            width=4,
            textvariable=self.var_sched_minute,
        ).pack(side=tk.LEFT, padx=(8, 2))
        ttk.Label(row_sched, text="分  执行", style="Panel.Muted.TLabel").pack(
            side=tk.LEFT
        )

        # ---- 忽略设置（黑名单：按 tid 存储，锁定即取消光标）----
        box_ig = section(body, "忽略设置")
        box_ig.pack(fill=tk.X, pady=(0, 6))
        row_ig_top = ttk.Frame(box_ig, style="Panel.TFrame")
        row_ig_top.pack(fill=tk.X, pady=1)
        ttk.Button(
            row_ig_top, text="加入当前目标", width=10, command=self._on_ignore_add_current
        ).pack(side=tk.LEFT)
        ttk.Button(
            row_ig_top, text="删除选中", width=9, command=self._on_ignore_remove_selected
        ).pack(side=tk.LEFT, padx=(4, 0))
        self.var_ignore_status = tk.StringVar(value="黑名单：未加载")
        ttk.Label(
            box_ig,
            textvariable=self.var_ignore_status,
            style="Panel.Mono.TLabel",
            wraplength=520,
        ).pack(anchor="w", pady=(2, 0))
        tree_frame = ttk.Frame(box_ig, style="Panel.TFrame")
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        cols = ("name", "tid")
        self.ignore_tree = ttk.Treeview(
            tree_frame, columns=cols, show="headings", height=6
        )
        headers = {
            "name": ("名称", 180),
            "tid": ("tid", 120),
        }
        for c, (t, w) in headers.items():
            self.ignore_tree.heading(c, text=t)
            self.ignore_tree.column(c, width=w, anchor="w")
        self.ignore_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ig_vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.ignore_tree.yview)
        ig_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.ignore_tree.configure(yscrollcommand=ig_vsb.set)
        self._ignore_rows: list[dict] = []
        self._on_ignore_refresh()


        box_sc = section(body, "技能后摇")
        box_sc.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            box_sc,
            text="放技能后自动取消后摇；日常选「每轮取消」。",
            style="Panel.Muted.TLabel",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 4))
        saved_when = str(
            self.settings.get("bg_skill_cancel_when") or ""
        ).strip()
        if not saved_when:
            legacy = str(self.settings.get("bg_skill_cancel_mode") or "")
            if legacy:
                saved_when, leg_proto, leg_key = legacy_mode_to_pipeline(legacy)
                if "bg_skill_cancel_protocol" not in self.settings:
                    self.settings["bg_skill_cancel_protocol"] = leg_proto
                if "bg_skill_cancel_key" not in self.settings:
                    self.settings["bg_skill_cancel_key"] = leg_key
            else:
                saved_when = CANCEL_WHEN_NEVER
        if saved_when not in self._CANCEL_WHEN_REV:
            saved_when = CANCEL_WHEN_NEVER

        # Hidden advanced defaults (not shown; save/start still work).
        self.var_sc_delivery = tk.StringVar(value="桥接后台")
        self.var_sc_cast_mode = tk.StringVar(value="序列(宏/临时)")
        saved_vk = parse_skill_vk(self.settings.get("bg_skill_cancel_vk") or 0x31)
        self.var_sc_key = tk.StringVar(value=skill_vk_label(saved_vk))
        saved_seq = str(
            self.settings.get("bg_skill_cancel_sequence") or DEFAULT_LOGITECH_SEQUENCE
        )
        _bad_defaults = {
            "1:30:40,Space:25:35,1:30:40,Space:25:20",
            PRESET_SKILL_SPACE,
            PRESET_SKILL_SPACE_X,
            PRESET_Q_SPACE,
            "Space:40:35,Space:40:35,Q:40:35,Q:120:90,X:40:35,X:55:45",
            "Space:40:35,Space:40:35,Q:40:35,Q:120:90,X:40:35",
            "Space:50:50,Space:50:50,Q:50:50,Q:260:260,X:50:50,X:100:100",
        }
        if saved_seq.strip() in _bad_defaults:
            saved_seq = DEFAULT_LOGITECH_SEQUENCE
        if not parse_key_sequence(saved_seq):
            saved_seq = DEFAULT_LOGITECH_SEQUENCE
        self.var_sc_sequence = tk.StringVar(value=saved_seq)
        self.var_sc_when = tk.StringVar(
            value=self._CANCEL_WHEN_REV.get(saved_when, "不取消")
        )
        self.var_sc_proto = tk.BooleanVar(value=False)
        self.var_sc_key_ch = tk.BooleanVar(value=False)
        self.var_sc_mem = tk.BooleanVar(value=False)
        saved_cvk = parse_cancel_vk(
            self.settings.get("bg_skill_cancel_cancel_vk") or "Esc"
        )
        if cancel_vk_label(saved_cvk) == "Space" and not bool(
            self.settings.get("bg_skill_cancel_key")
        ):
            saved_cvk = parse_cancel_vk("Esc")
        self.var_sc_cancel_key = tk.StringVar(value=cancel_vk_label(saved_cvk))
        self.var_sc_interval = tk.StringVar(
            value=str(int(self.settings.get("bg_skill_cancel_interval_ms") or 50))
        )
        self.var_sc_hold = tk.StringVar(
            value=str(int(self.settings.get("bg_skill_cancel_hold_ms") or 40))
        )
        self.var_sc_cancel_wait = tk.StringVar(
            value=str(int(self.settings.get("bg_skill_cancel_wait_ms") or 40))
        )

        row_sc1 = ttk.Frame(box_sc, style="Panel.TFrame")
        row_sc1.pack(fill=tk.X, pady=1)
        ttk.Label(row_sc1, text="时机", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        ttk.Combobox(
            row_sc1,
            textvariable=self.var_sc_when,
            values=("不取消", "每轮取消", "忙碌时取消"),
            width=10,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(row_sc1, text="技能键", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        ttk.Combobox(
            row_sc1,
            textvariable=self.var_sc_key,
            values=("1", "2", "3", "4", "5", "6", "7", "8", "9", "0"),
            width=4,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(4, 0))

        # 鼠标宏 / 按键序列（保留，方便改链）
        row_sc_seq = ttk.Frame(box_sc, style="Panel.TFrame")
        row_sc_seq.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row_sc_seq, text="鼠标宏", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT, anchor="n"
        )
        ttk.Entry(row_sc_seq, textvariable=self.var_sc_sequence).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4)
        )
        ttk.Button(
            row_sc_seq,
            text="默认",
            width=5,
            command=self._on_sc_seq_default,
        ).pack(side=tk.LEFT)
        row_sc_preset = ttk.Frame(box_sc, style="Panel.TFrame")
        row_sc_preset.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(
            row_sc_preset, text="预设", style="Panel.Muted.TLabel", width=8
        ).pack(side=tk.LEFT)
        for label, seq in (
            ("录制链", RECORDED_LOGITECH_SEQUENCE),
            ("1连按", PRESET_SKILL_ONLY),
            ("1+X", PRESET_SKILL_X),
            ("Q+X", PRESET_Q_X),
            ("1+空格*", PRESET_SKILL_SPACE),
        ):
            ttk.Button(
                row_sc_preset,
                text=label,
                width=8,
                command=lambda s=seq: self._on_sc_seq_preset(s),
            ).pack(side=tk.LEFT, padx=(0, 4))

        row_scb = ttk.Frame(box_sc, style="Panel.TFrame")
        row_scb.pack(fill=tk.X, pady=(6, 0))
        self.btn_sc_start = ttk.Button(
            row_scb,
            text="开始",
            style="Accent.TButton",
            width=8,
            command=self._on_skill_cancel_start,
        )
        self.btn_sc_start.pack(side=tk.LEFT)
        self.btn_sc_stop = ttk.Button(
            row_scb,
            text="停止",
            width=8,
            command=self._on_skill_cancel_stop,
            state=tk.DISABLED,
        )
        self.btn_sc_stop.pack(side=tk.LEFT, padx=(6, 0))
        self.var_sc_status = tk.StringVar(value="待命")
        ttk.Label(
            row_scb, textvariable=self.var_sc_status, style="Panel.Mono.TLabel"
        ).pack(side=tk.LEFT, padx=(10, 0))

        self._pick_job = None
        self._pick_tip: tk.Toplevel | None = None
        self._pick_was_down = False
        self._pick_armed = False
        self._pick_tip_lbl = None
        self._picking = False

        self._schedule_ui_drain(120)
        self.refresh_sessions()

    def refresh_sessions(self) -> None:
        """Refresh account plus live local/cloud control state. @author by ak"""
        auth = self.auth
        if auth is None or not getattr(auth, "is_logged_in", False):
            self.var_user.set("未登录")
            self.var_expire.set("-")
            self.var_key_mask.set("-")
        else:
            sess = auth.session
            self.var_user.set(str(sess.display_name or "用户"))
            self.var_expire.set(sess.expire_text())
            k = str(sess.key or "")
            if len(k) <= 8:
                self.var_key_mask.set(k or "-")
            else:
                self.var_key_mask.set(k[:4] + "…" + k[-4:])
        self._refresh_control_status(rebind=True)

    def _refresh_control_status(self, *, rebind: bool = True) -> None:
        """Pull current Hub counts and CloudSyncBridge connection status."""
        pid = int(self._fixed_pid or 0)
        if rebind and pid > 0:
            try:
                task_page = self._sibling_page("task")
                if task_page is not None and hasattr(task_page, "_bind_task_sync"):
                    task_page._bind_task_sync()
            except Exception as e:
                self.log(f"设置: 刷新群控绑定失败: {e}")

        # _bind_task_sync hydrates settings from control.json; reflect that
        # persisted value in the checkbox on the first page open as well.
        try:
            if getattr(self, "var_team_control_enabled", None) is not None:
                self._team_control_syncing = True
                self.var_team_control_enabled.set(
                    bool(self.settings.get("team_control_enabled", False))
                )
        except Exception as e:
            self.log(f"设置: 回填队内控状态失败: {e}")
        finally:
            self._team_control_syncing = False

        try:
            hub = get_task_sync_hub()
            role = hub.get_role(pid) if pid > 0 else ROLE_NONE
            if role not in (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE):
                role = str(self.settings.get("task_control_role") or ROLE_NONE)
            self._apply_task_role_ui(role, register=False, apply_cloud=False)
        except Exception as e:
            self.log(f"设置: 刷新本机群控状态失败: {e}")

        if getattr(self, "var_cloud_status", None) is None:
            return
        try:
            dirty = bool(getattr(self, "_cloud_ui_dirty", False))
            if not dirty:
                self._apply_cloud_control(bind_listener=True)
            if pid > 0:
                label = get_cloud_sync_bridge(pid, log=self.log).status_label()
            else:
                label = "云控：未挂载游戏"
            if dirty:
                label += " · 上方修改尚未保存"
            self.var_cloud_status.set(label)
        except Exception as e:
            self.var_cloud_status.set(f"云控：状态刷新失败 · {e}")


    def _on_role_combo(self, *_args) -> None:
        """Dropdown role: none / master / slave. @author by ak"""
        label = ""
        try:
            label = str(self.var_role_label.get() or "").strip()
        except Exception:
            label = ""
        role = (getattr(self, "_ROLE_LABEL_REV", {}) or {}).get(label, ROLE_NONE)
        self._on_task_role_pick(role)

    def _on_task_role_pick(self, role: str) -> None:
        """Persist the selected role before re-registering task synchronization.

        _apply_task_role_ui(register=True) immediately asks TaskPage to rebind.
        Therefore control.json must be updated first, otherwise that rebind reads
        the old "none" record and overwrites the just-selected role.
        @author by ak
        """
        role = str(role or ROLE_NONE).lower()
        if role not in (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE):
            role = ROLE_NONE
        self.settings["task_control_role"] = role
        try:
            self.var_task_role.set(role)
            label = (getattr(self, "_ROLE_LABEL", {}) or {}).get(role, "无控")
            self.var_role_label.set(label)
        except Exception:
            pass
        try:
            rid = self._current_role_id()
            if rid:
                from app.core.account_manager import save_role_control

                save_role_control(
                    rid,
                    role=role,
                    team=team_control_flag_enabled(self.settings),
                )
        except Exception as e:
            self.log(f"设置: 群控角色落盘失败: {e}")
        self._apply_task_role_ui(role, register=True, apply_cloud=False)

    def apply_role_from_external(self, role: str, team: bool | None = None) -> None:
        """
        Apply task-control role when another window clicks 一键副控.

        Updates role/team UI vars + hub registration; cloud HTTP still only on 保存.
        @author by ak
        """
        role = str(role or ROLE_NONE).lower()
        if role not in (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE):
            role = ROLE_NONE
        try:
            if getattr(self, "var_task_role", None) is not None:
                self.var_task_role.set(role)
        except Exception:
            pass
        try:
            label = (getattr(self, "_ROLE_LABEL", {}) or {}).get(role, "无控")
            if getattr(self, "var_role_label", None) is not None:
                self.var_role_label.set(label)
        except Exception:
            pass
        if team is not None:
            try:
                self.settings["team_control_enabled"] = bool(team)
                if getattr(self, "var_team_control_enabled", None) is not None:
                    self.var_team_control_enabled.set(bool(team))
                self._update_team_control_status()
            except Exception:
                pass
            try:
                rid = self._current_role_id()
                if rid:
                    from app.core.account_manager import save_role_control

                    save_role_control(rid, role=role, team=bool(team))
            except Exception as e:
                self.log(f"设置: 一键副控保存队内控失败: {e}")
        self._apply_task_role_ui(role, register=True, apply_cloud=False)

    def _find_shell_feature_wins(self) -> dict[int, object]:
        """Locate ShellApp._feature_wins (pid -> SessionFeatureWindow). @author by ak"""
        try:
            top = self.winfo_toplevel()
        except Exception:
            top = None
        cands: list[object] = []
        try:
            if top is not None:
                cands.append(getattr(top, "master", None))
                cands.append(top)
            node = self.master
            for _ in range(10):
                if node is None:
                    break
                cands.append(node)
                node = getattr(node, "master", None)
        except Exception:
            pass
        for c in cands:
            if c is None:
                continue
            wins = getattr(c, "_feature_wins", None)
            if isinstance(wins, dict):
                return wins
        return {}

    def _update_one_click_slaves_btn(self, role: str | None = None) -> None:
        """Show 一键副控 + PING only when current role is 主控. @author by ak"""
        btn = getattr(self, "btn_one_click_slaves", None)
        ping = getattr(self, "btn_team_ping", None)
        if btn is None:
            return
        if role is None:
            role = str(
                getattr(self, "var_task_role", None)
                and self.var_task_role.get()
                or (self.settings or {}).get("task_control_role")
                or ROLE_NONE
            ).lower()
        role = str(role or ROLE_NONE).lower()
        try:
            if role == ROLE_MASTER:
                if not btn.winfo_ismapped():
                    btn.pack(side=tk.LEFT, padx=(10, 0))
            else:
                btn.pack_forget()
        except Exception:
            pass
        # PING 也是主控专属（副控/无控隐藏）。
        if ping is not None:
            try:
                if role == ROLE_MASTER:
                    if not ping.winfo_ismapped():
                        ping.pack(side=tk.LEFT, padx=(4, 0))
                else:
                    ping.pack_forget()
            except Exception:
                pass

    def _on_one_click_slaves(self) -> None:
        """
        Set every other DEL-injected local window to 副控 and persist in settings.

        Does not change this window's role. Skips self and inject-incomplete windows.
        Only available when this window is 主控.
        @author by ak
        """
        cur = str(
            getattr(self, "var_task_role", None)
            and self.var_task_role.get()
            or (self.settings or {}).get("task_control_role")
            or ROLE_NONE
        ).lower()
        if cur != ROLE_MASTER:
            msg = "一键副控仅主控可用，请先选择主控"
            try:
                self.var_status.set(msg)
            except Exception:
                pass
            self.user_log(msg, category=CAT_CONTROL, source="快捷设置", dedupe_s=0.0)
            return
        self_pid = int(self._fixed_pid or 0)
        wins = self._find_shell_feature_wins()
        if not wins:
            msg = "一键副控失败：找不到本机其它功能窗"
            try:
                self.var_status.set(msg)
            except Exception:
                pass
            self.log(f"设置: {msg}")
            self.user_log(msg, category=CAT_CONTROL, source="快捷设置", dedupe_s=0.0)
            return

        targets: list[tuple[int, object]] = []
        for pid, win in list(wins.items()):
            try:
                pid_i = int(pid)
            except Exception:
                continue
            if not pid_i or pid_i == self_pid:
                continue
            if win is None:
                continue
            ready = True
            try:
                ready = bool(getattr(win, "_bridge_ready", True))
            except Exception:
                ready = True
            if not ready:
                try:
                    st = getattr(win, "settings", None) or {}
                    ready = bool(st.get("bridge_ready", False))
                except Exception:
                    ready = False
            if not ready:
                continue
            targets.append((pid_i, win))

        if not targets:
            msg = "一键副控：没有其它已注入窗口"
            try:
                self.var_status.set(msg)
            except Exception:
                pass
            self.log(f"设置: {msg}")
            self.user_log(msg, category=CAT_CONTROL, source="快捷设置", dedupe_s=0.0)
            return

        ok_n = 0
        team_enabled = bool(
            getattr(self, "var_team_control_enabled", None)
            and self.var_team_control_enabled.get()
        )
        if getattr(self, "var_team_control_enabled", None) is None:
            team_enabled = team_control_flag_enabled(self.settings)

        fail: list[str] = []
        for pid_i, win in targets:
            try:
                applied = False
                # 一键副控跟随主控当前队内控开关：开启=队内控+副控，关闭=纯副控。
                if hasattr(win, "apply_external_task_role"):
                    applied = bool(
                        win.apply_external_task_role(ROLE_SLAVE, team=team_enabled)
                    )
                if not applied:
                    # Fallback: settings + hub + task bind
                    st = getattr(win, "settings", None)
                    if isinstance(st, dict):
                        st["task_control_role"] = ROLE_SLAVE
                        st["team_control_enabled"] = team_enabled
                    get_task_sync_hub().set_role(pid_i, ROLE_SLAVE)
                    pages = getattr(win, "_pages", None)
                    if isinstance(pages, dict):
                        sp = pages.get("settings")
                        if sp is not None and hasattr(sp, "apply_role_from_external"):
                            try:
                                sp.apply_role_from_external(ROLE_SLAVE, team=team_enabled)
                            except TypeError:
                                sp.apply_role_from_external(ROLE_SLAVE)
                            applied = True
                        tp = pages.get("task")
                        if tp is not None and hasattr(tp, "_bind_task_sync"):
                            tp._bind_task_sync()
                            applied = True
                    if not applied and isinstance(st, dict):
                        applied = st.get("task_control_role") == ROLE_SLAVE
                if applied:
                    ok_n += 1
                else:
                    fail.append(str(pid_i))
            except Exception as e:
                fail.append(f"{pid_i}:{e}")
                self.log(f"设置: 一键副控 pid={pid_i} 失败: {e}")

        # Refresh local status line (slave counts) without re-register noise.
        try:
            snap = get_task_sync_hub().snapshot()
            n_slave = len(snap.get("slaves") or [])
            n_master = len(snap.get("masters") or [])
            cur = str(
                getattr(self, "var_task_role", None)
                and self.var_task_role.get()
                or self.settings.get("task_control_role")
                or ROLE_NONE
            ).lower()
            labels = {
                ROLE_NONE: "当前：无控（不同步）",
                ROLE_MASTER: f"当前：主控 · 副控 {n_slave} 个",
                ROLE_SLAVE: f"当前：副控 · 主控 {n_master} 个",
            }
            if getattr(self, "var_role_status", None) is not None:
                self.var_role_status.set(labels.get(cur, cur))
        except Exception:
            pass

        if fail:
            msg = f"一键副控：成功 {ok_n} 个，失败 {len(fail)} 个"
        else:
            msg = f"一键副控：已将 {ok_n} 个窗口设为副控并保存"
        try:
            self.var_status.set(msg)
        except Exception:
            pass
        self.log(f"设置: {msg} targets={[p for p, _ in targets]} fail={fail}")
        self.user_log(msg, category=CAT_CONTROL, source="快捷设置", dedupe_s=0.0)

    def _current_game_role_name(self) -> str:
        """
        Current game character name for cloud master channel.

        Prefer live header on SessionFeatureWindow; else memory read; else title.
        @author by ak
        """
        # 1) Live header from parent feature window
        try:
            p = self.master
            while p is not None:
                name = str(getattr(p, "_hdr_role", "") or "").strip()
                if name:
                    return name
                if hasattr(p, "var_hdr_role"):
                    name = str(p.var_hdr_role.get() or "").strip()
                    if name:
                        return name
                p = getattr(p, "master", None)
        except Exception:
            pass
        # 2) Live memory name
        pid = int(self._fixed_pid or 0)
        if pid:
            try:
                from app.core.plg_ui import get_host_player_name
                from app.core.super_loot import open_attach_session

                session = open_attach_session(pid, log=lambda _m: None)
                try:
                    name = str(
                        get_host_player_name(session, log=lambda _m: None) or ""
                    ).strip()
                    if name:
                        return name
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass
            except Exception:
                pass
        # 3) Window title parse
        try:
            sess = self.selected_session()
            if sess is not None:
                from app.core.window_title import parse_role_name_from_title

                title = str(getattr(sess, "title", "") or getattr(sess, "original_title", "") or "")
                name = str(parse_role_name_from_title(title) or "").strip()
                if name:
                    return name
        except Exception:
            pass
        return ""

    def _cloud_master_name_for_apply(self) -> str:
        """
        Cloud room name: master auto = game role; slave = user input.
        @author by ak
        """
        role = str(
            getattr(self, "var_task_role", None)
            and self.var_task_role.get()
            or self.settings.get("task_control_role")
            or ROLE_NONE
        ).lower()
        if getattr(self, "var_cloud_master_name", None) is not None:
            return (self.var_cloud_master_name.get() or "").strip()
        return str(self.settings.get("cloud_control_master_name") or "").strip()

    def _update_cloud_name_row(self) -> None:
        """Show 主控名称 only when cloud is on and role is 副控. @author by ak"""
        row = getattr(self, "row_cloud_name", None)
        if row is None:
            return
        enabled = bool(
            getattr(self, "var_cloud_enabled", None) and self.var_cloud_enabled.get()
        )
        role = str(
            getattr(self, "var_task_role", None)
            and self.var_task_role.get()
            or ROLE_NONE
        ).lower()
        # 开启云控且选了副控：必填名称；主控开云控同样需要房间名。
        # 仅云控 + 副控显示名称框；主控用当前游戏角色名自动上报。
        show = enabled and role == ROLE_SLAVE
        try:
            if show:
                before = getattr(self, "lbl_role_status", None)
                if before is not None:
                    row.pack(fill=tk.X, pady=(6, 0), before=before)
                else:
                    row.pack(fill=tk.X, pady=(6, 0))
            else:
                row.pack_forget()
        except Exception:
            pass

    def _apply_task_role_ui(self, role: str, *, register: bool = False, apply_cloud: bool = False) -> None:
        role = str(role or ROLE_NONE).lower()
        if role not in (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE):
            role = ROLE_NONE
        # Keep the actual save source synchronized with disk/Hub hydration.
        # Saving reads var_task_role, not only settings, so both must agree.
        try:
            if getattr(self, "var_task_role", None) is not None:
                self.var_task_role.set(role)
        except Exception:
            pass
        try:
            label = (getattr(self, "_ROLE_LABEL", {}) or {}).get(role, "无控")
            if getattr(self, "var_role_label", None) is not None:
                self.var_role_label.set(label)
        except Exception:
            pass
        self.settings["task_control_role"] = role
        self._update_one_click_slaves_btn(role)
        self._update_cloud_name_row()
        pid = int(self._fixed_pid or 0)
        hub = get_task_sync_hub()
        if register and pid:
            applied = hub.set_role(pid, role)
            if applied != role:
                role = applied
                self.var_task_role.set(role)
                self.settings["task_control_role"] = role
            # Keep sibling TaskPage listener/role in sync immediately.
            try:
                parent = self.master
                while parent is not None and not hasattr(parent, "_pages"):
                    parent = getattr(parent, "master", None)
                pages = getattr(parent, "_pages", None) if parent else None
                task_page = pages.get("task") if isinstance(pages, dict) else None
                if task_page is not None and hasattr(task_page, "_bind_task_sync"):
                    task_page._bind_task_sync()
                if task_page is not None and hasattr(
                    task_page, "_refresh_team_control_buttons"
                ):
                    task_page._refresh_team_control_buttons()
            except Exception:
                pass
        snap = hub.snapshot()
        n_slave = len(snap.get("slaves") or [])
        n_master = len(snap.get("masters") or [])
        labels = {
            ROLE_NONE: "当前：无控（不同步）",
            ROLE_MASTER: f"当前：主控 · 副控 {n_slave} 个",
            ROLE_SLAVE: f"当前：副控 · 主控 {n_master} 个",
        }
        try:
            self.var_role_status.set(labels.get(role, role))
        except Exception:
            pass
        if register and pid:
            self.log(
                f"设置: 任务角色 pid={pid} → {role} | masters={snap.get('masters')} "
                f"slaves={snap.get('slaves')} listeners={snap.get('listeners')}"
            )
            self.user_log(
                f"群控角色切换为「{role_cn(role)}」"
                + (f" · 副控 {n_slave} 个" if role == ROLE_MASTER else "")
                + (f" · 主控 {n_master} 个" if role == ROLE_SLAVE else ""),
                category=CAT_CONTROL,
                source="设置",
            )
            if apply_cloud:
                try:
                    cloud_label = self._apply_cloud_control(bind_listener=True)
                    if getattr(self, "var_cloud_status", None) is not None:
                        self.var_cloud_status.set(cloud_label)
                except Exception:
                    pass
            else:
                self._on_cloud_ui_changed()

    def _on_cloud_ui_changed(self, *_args) -> None:
        """
        UI-only: show/hide 主控名称, tip that Save is required for cloud HTTP.

        Does not join/leave cloud or call cloud APIs.
        @author by ak
        """
        self._cloud_ui_dirty = True
        # 队内控与云控勾选互斥：云控开 → 队内控关。
        try:
            if (
                getattr(self, "var_cloud_enabled", None) is not None
                and bool(self.var_cloud_enabled.get())
                and getattr(self, "var_team_control_enabled", None) is not None
                and bool(self.var_team_control_enabled.get())
            ):
                self.var_team_control_enabled.set(False)
        except Exception:
            pass
        self._update_cloud_name_row()
        if self.settings.get("cloud_control_available") is False:
            if getattr(self, "var_cloud_status", None) is not None:
                self.var_cloud_status.set("云控：本地测试卡不可用")
            return
        enabled = bool(
            getattr(self, "var_cloud_enabled", None) and self.var_cloud_enabled.get()
        )
        role = str(
            getattr(self, "var_task_role", None)
            and self.var_task_role.get()
            or self.settings.get("task_control_role")
            or ROLE_NONE
        ).lower()
        if not getattr(self, "var_cloud_status", None):
            return
        if not enabled:
            self.var_cloud_status.set("云控：关闭（保存后仅使用本机群控）")
            return
        if role == ROLE_NONE:
            self.var_cloud_status.set("云控：请先选择主控或副控，再保存")
            return
        if role == ROLE_SLAVE:
            name = ""
            if getattr(self, "var_cloud_master_name", None) is not None:
                name = (self.var_cloud_master_name.get() or "").strip()
            if not name:
                self.var_cloud_status.set("云控：副控需要填写主控角色名")
            else:
                self.var_cloud_status.set(
                    f"云控：副控将加入「{name}」通道，保存后连接"
                )
            return
        # master
        gname = self._current_game_role_name()
        if not gname:
            self.var_cloud_status.set("云控：未读取到本角色名，请先挂载游戏")
        else:
            self.var_cloud_status.set(
                f"云控：主控将创建「{gname}」通道，保存后连接"
            )

    def _tick_cloud_status(self) -> None:
        """Refresh saved cloud connection state without hiding unsaved hints."""
        try:
            if not bool(getattr(self, "_cloud_ui_dirty", False)):
                pid = int(self._fixed_pid or 0)
                if pid > 0 and getattr(self, "var_cloud_status", None) is not None:
                    label = get_cloud_sync_bridge(pid, log=self.log).status_label()
                    self.var_cloud_status.set(label)
        except Exception:
            pass
        try:
            self._cloud_status_job = self.after(1000, self._tick_cloud_status)
        except Exception:
            self._cloud_status_job = None

    def _on_cloud_control_changed(self, *_args) -> None:
        """Back-compat alias: UI-only (cloud HTTP only on 保存). @author by ak"""
        self._on_cloud_ui_changed()

    def _on_team_control_ui_changed(self, *_args) -> None:
        """
        Persist the 队内控 choice immediately and refresh its status.

        Team-chat send/read still starts only when orchestration is started.
        @author by ak
        """
        if bool(getattr(self, "_team_control_syncing", False)):
            return
        self._cloud_ui_dirty = True
        team_enabled = False
        try:
            team_enabled = bool(self.var_team_control_enabled.get())
            self.settings["team_control_enabled"] = team_enabled
            if team_enabled and getattr(self, "var_cloud_enabled", None) is not None:
                self.var_cloud_enabled.set(False)
        except Exception:
            pass
        # 队内控是即时配置：点击勾选后立即更新内存并写角色 control.json，
        # 不能等“保存设置”，避免任务页重绑先读到旧值覆盖内存。
        try:
            role = str(
                getattr(self, "var_task_role", None)
                and self.var_task_role.get()
                or self.settings.get("task_control_role")
                or ROLE_NONE
            ).lower()
            rid = self._current_role_id()
            if rid:
                from app.core.account_manager import save_role_control

                saved = save_role_control(rid, role=role, team=team_enabled)
                self.settings["team_control_enabled"] = bool(saved.get("team", team_enabled))
                self.log(
                    f"设置: 队内控即时保存 role_id={rid} role={role} "
                    f"team={int(self.settings["team_control_enabled"])}"
                )
        except Exception as e:
            self.log(f"设置: 队内控即时保存失败: {e}")
        self._update_cloud_name_row()
        self._update_team_control_status()
        try:
            task_page = self._sibling_page("task")
            if task_page is not None and hasattr(task_page, "_bind_task_sync"):
                task_page._bind_task_sync()
        except Exception as e:
            self.log(f"队内控: 切换后重绑失败 {e}")


    def _update_team_control_status(self) -> None:
        """Refresh the 队内控 hint line (no HTTP / no send). @author by ak"""
        if getattr(self, "var_team_control_status", None) is None:
            return
        if self.settings.get("cloud_control_available") is False:
            self.var_team_control_status.set("队内控：本地测试卡不支持云控，但可用队内控")
            return
        enabled = bool(
            getattr(self, "var_team_control_enabled", None)
            and self.var_team_control_enabled.get()
        )
        role = str(
            getattr(self, "var_task_role", None)
            and self.var_task_role.get()
            or self.settings.get("task_control_role")
            or ROLE_NONE
        ).lower()
        if not enabled:
            self.var_team_control_status.set("队内控：关闭（保存后仅使用本机/云控群控）")
            return
        if role == ROLE_NONE:
            self.var_team_control_status.set("队内控：请先选择主控或副控，再保存")
            return
        if role == ROLE_MASTER:
            self.var_team_control_status.set(
                "队内控：主控将通过队伍频道发 [主P] 命令，保存后生效"
            )
        else:
            self.var_team_control_status.set(
                "队内控：副控将读取队伍频道 [主P] 命令并回 [副G] 确认，保存后生效"
            )

    def _on_team_ping(self) -> None:
        """
        队内控 PING：队长（主控）向队伍频道发 [主P]PING，队友收到回 [副G]PONG。

        Only meaningful for master; slave/no-role shows a hint.
        @author by ak
        """
        try:
            self._update_team_control_status()
        except Exception:
            pass
        pid = int(self._fixed_pid or 0)
        role = str(
            getattr(self, "var_task_role", None)
            and self.var_task_role.get()
            or self.settings.get("task_control_role")
            or ROLE_NONE
        ).lower()
        if getattr(self, "var_team_control_status", None) is None:
            return
        if not bool(
            getattr(self, "var_team_control_enabled", None)
            and self.var_team_control_enabled.get()
        ):
            self.var_team_control_status.set("PING：请先勾选队内控并保存")
            return
        if role != ROLE_MASTER:
            self.var_team_control_status.set("PING：只有队长（主控）能发 PING")
            return
        if not pid:
            self.var_team_control_status.set("PING：未挂载游戏")
            return
        text = build_master_ping()
        res = send_team_message(
            pid,
            text,
            log=lambda m: self.log(str(m)),
        )
        if res.get("ok"):
            self.var_team_control_status.set(f"PING 已发送：{text}")
            try:
                self.user_log(
                    f"队内控：队长已发送 PING {text}",
                    category=CAT_CONTROL,
                    source="快捷设置",
                    dedupe_s=0.0,
                )
            except Exception:
                pass
        else:
            err = str(res.get("error") or "发送失败")
            self.var_team_control_status.set(f"PING 发送失败：{err[:60]}")
            self.log(f"队内控 [PING] {text} 失败: {err}")

    def _tick_team_control_status(self) -> None:
        """Refresh saved team-control state without hiding unsaved hints."""
        try:
            if not bool(getattr(self, "_cloud_ui_dirty", False)):
                self._update_team_control_status()
        except Exception:
            pass
        try:
            self._team_control_status_job = self.after(
                1000, self._tick_team_control_status
            )
        except Exception:
            self._team_control_status_job = None

    def destroy(self) -> None:
        try:
            job = getattr(self, "_cloud_status_job", None)
            if job is not None:
                self.after_cancel(job)
                self._cloud_status_job = None
        except Exception:
            pass
        try:
            tjob = getattr(self, "_team_control_status_job", None)
            if tjob is not None:
                self.after_cancel(tjob)
                self._team_control_status_job = None
        except Exception:
            pass
        try:
            jt = getattr(self, "_jianglong_tick_job", None)
            if jt is not None:
                self.after_cancel(jt)
                self._jianglong_tick_job = None
        except Exception:
            pass
        try:
            runner = getattr(self, "_jianglong_runner", None)
            if runner is not None:
                runner.stop()
            self._jianglong_runner = None
        except Exception:
            pass
        try:
            drop_participant(int(self._fixed_pid or 0))
        except Exception:
            pass
        super().destroy()



    def on_page_show(self) -> None:
        """Enter 快捷设置: reload disk prefs into hang controls + refresh live. @author by ak"""
        try:
            self.refresh_sessions()
        except Exception as e:
            self.log(f"设置: 进页刷新账号/群控失败 {e}")
        # 启动/进页：把角色文件配置全量同步进会话（共用配置文件，文件为基准）。
        try:
            rid = self._current_role_id()
            if rid:
                from app.core.hang_settings import sync_role_prefs_to_settings

                sync_role_prefs_to_settings(self.settings, rid)
        except Exception as e:
            self.log(f"设置: 同步角色配置失败 {e}")
        try:
            self._load_hang_prefs_into_ui()
        except Exception as e:
            self.log(f"挂机设置: 载入 prefs 失败 {e}")
        # _load_hang_prefs_into_ui resolves the live role id. Refresh the
        # per-role persistent ignore list after that identity is available.
        try:
            self._on_ignore_refresh()
        except Exception as e:
            self.log(f"忽略列表: 进页刷新失败 {e}")
        try:
            self._on_hang_refresh()
        except Exception as e:
            self.log(f"挂机设置: 进页刷新失败 {e}")
        self._refresh_youfeng_hook_button()
        self._refresh_wanzi_packet_button()

    def _hang_identity(self) -> tuple[str, str]:
        """Return (char_id_key, display_name). id is stable key; name is label only. @author by ak"""
        cid = ""
        name = ""
        # 1) The mounted injection context is the authoritative identity.
        # It is fixed after __ROLE_BOUND__; never replace it with a later RPM read.
        try:
            sess = self._require_session()
            if sess is not None:
                cid = normalize_hang_char_id(getattr(sess, "role_id", ""))
                name = str(getattr(sess, "role_name", "") or "").strip()
        except Exception:
            cid = ""
            name = ""
        if cid:
            if name:
                self.settings["hang_char_name"] = name
            self.settings["hang_char_id"] = int(cid)
            return cid, name
        # 2) Compatibility cache while post-inject binding has not completed.
        try:
            cid = normalize_hang_char_id(self.settings.get("hang_char_id"))
            name = str(self.settings.get("hang_char_name") or "").strip()
        except Exception:
            cid = ""
            name = ""
        if cid and name:
            return cid, name
        # 3) Live memory fallback only while there is no bound session identity.
        try:
            pid = int(getattr(self, "_fixed_pid", 0) or 0)
            if not pid:
                sess = None
                try:
                    sess = self.selected_session()
                except Exception:
                    sess = None
                pid = int(getattr(sess, "pid", 0) or 0)
            if pid:
                from app.core.super_loot import open_attach_session
                from app.core.team_ops import read_host_identity

                attach = open_attach_session(pid, log=lambda _m: None)
                try:
                    live_name, oid = read_host_identity(attach, log=lambda _m: None)
                    key = normalize_hang_char_id(oid)
                    live_name = str(live_name or "").strip()
                    if key:
                        cid = key
                        try:
                            self.settings["hang_char_id"] = int(key)
                        except Exception:
                            pass
                    if live_name:
                        name = live_name
                        try:
                            self.settings["hang_char_name"] = live_name
                        except Exception:
                            pass
                finally:
                    try:
                        attach.close()
                    except Exception:
                        pass
        except Exception:
            pass
        # 3) title/header name fallback (label only)
        if not name:
            try:
                name = str(self._current_game_role_name() or "").strip()
                if name:
                    try:
                        self.settings["hang_char_name"] = name
                    except Exception:
                        pass
            except Exception:
                pass
        return cid, name

    def _hang_char_id_key(self) -> str:
        """Current host character obj_id64 as hang prefs key. @author by ak"""
        try:
            return self._hang_identity()[0]
        except Exception:
            return ""

    def _hang_char_name_label(self) -> str:
        """Current host display name for JSON label only. @author by ak"""
        try:
            return self._hang_identity()[1]
        except Exception:
            return ""

    # compat alias used by older hang_sync snippets
    def _hang_role_key(self) -> str:
        return self._hang_char_id_key()

    def _load_hang_prefs_into_ui(self) -> None:
        """Fill all hang UI fields from character-id disk prefs. @author by ak"""
        role = self._hang_char_id_key()
        cfg = get_hang_config(char_id=role or None)
        hp = {"radius": int(cfg.radius)}
        try:
            self.var_hang_mode.set(self._HANG_MODE_REV.get(int(cfg.mode), "副本模式"))
        except Exception:
            pass
        try:
            self.var_hang_radius.set(str(int(hp.get("radius", DEFAULT_HANG_RADIUS) or DEFAULT_HANG_RADIUS)))
        except Exception:
            pass
        try:
            self.var_hang_pickup.set(bool(cfg.enable_pickup))
            self.var_hang_empty.set(bool(cfg.empty_skill))
            self.var_hang_wanzi_neigong.set(
                bool(getattr(cfg, "wanzi_neigong_hang", False))
            )
            self.var_hang_wanzi_waigong.set(
                bool(getattr(cfg, "wanzi_waigong_hang", False))
            )
            self.var_hang_youfeng.set(bool(getattr(cfg, "youfeng_hang", False)))
            try:
                self.var_hang_jianglong.set(
                    bool(getattr(cfg, "jianglong_hang", DEFAULT_JIANGLONG_HANG))
                )
            except Exception:
                pass
            try:
                self.var_hang_skip_story.set(
                    bool(getattr(cfg, "skip_dungeon_story", False))
                )
            except Exception:
                pass
            try:
                self.var_hang_auto_open_monster.set(bool(getattr(cfg, "auto_open_monster", False)))
                self.var_hang_open_monster_rows.set(str(int(getattr(cfg, "open_monster_rows", 0) or 0)))
            except Exception:
                pass
            self.var_hang_wanzi_iv.set(
                str(
                    int(
                        getattr(
                            cfg,
                            "wanzi_interval_ms",
                            hp.get("wanzi_interval_ms", DEFAULT_WANZI_INTERVAL_MS),
                        )
                        or DEFAULT_WANZI_INTERVAL_MS
                    )
                )
            )
            self._update_hang_wanzi_ui()
        except Exception:
            pass
        try:
            self.var_hang_auto_repair.set(bool(hp.get("auto_repair", True)))
            self.var_hang_repair_pct.set(str(int(float(hp.get("repair_below_pct", 50) or 50))))
            self.var_hang_auto_vitality.set(bool(hp.get("auto_vitality", True)))
            self.var_hang_vitality_pct.set(str(int(float(hp.get("vitality_below_pct", 20) or 20))))
        except Exception:
            pass
        try:
            ig_on = bool(getattr(cfg, "ignore_dungeon_stuck", False))
            self.var_hang_ignore_dungeon_stuck.set(ig_on)
        except Exception:
            pass
        # mirror into session for in-process callers (not a substitute for disk)
        try:
            write_hang_config_to_settings(self.settings, cfg)
        except Exception:
            pass

    def _hang_cfg_from_ui(self) -> HangConfig:
        """Build HangConfig from current widgets. @author by ak"""
        mode_lab = ""
        try:
            mode_lab = str(self.var_hang_mode.get() or "").strip()
        except Exception:
            mode_lab = "副本模式"
        mode = int(self._HANG_MODE_UI.get(mode_lab, 1))
        radius = self._parse_int(self.var_hang_radius.get(), DEFAULT_HANG_RADIUS, lo=1, hi=255)
        repair_pct = float(self._parse_int(self.var_hang_repair_pct.get(), 50, lo=1, hi=100))
        vitality_pct = float(self._parse_int(self.var_hang_vitality_pct.get(), 20, lo=1, hi=100))
        wanzi_iv = self._parse_int(
            self.var_hang_wanzi_iv.get(),
            DEFAULT_WANZI_INTERVAL_MS,
            lo=1 if is_dev_build() else int(WANZI_INTERVAL_MIN_PROD_MS),
            hi=60000,
        )
        low_s = self._parse_float(
            getattr(self, "var_hang_wanzi_low_s", None), 6.0, lo=0.0, hi=120.0
        )
        low_rate = self._parse_float(
            getattr(self, "var_hang_wanzi_low_rate", None), 3.0, lo=0.1, hi=20.0
        )
        active_s = self._parse_float(
            getattr(self, "var_hang_wanzi_active_s", None), 300.0, lo=1.0, hi=7200.0
        )
        return HangConfig(
            mode=mode,
            radius=int(radius),
            enable_pickup=bool(self.var_hang_pickup.get()),
            empty_skill=bool(self.var_hang_empty.get()),
            wanzi_hang=bool(
                self.var_hang_wanzi_neigong.get()
                or self.var_hang_wanzi_waigong.get()
            ),
            wanzi_neigong_hang=bool(self.var_hang_wanzi_neigong.get()),
            wanzi_waigong_hang=bool(self.var_hang_wanzi_waigong.get()),
            wanzi_interval_ms=int(wanzi_iv),
            wanzi_low_rate_seconds=float(low_s),
            wanzi_low_rate_pairs_per_second=float(low_rate),
            wanzi_active_window_s=float(active_s),
            youfeng_hang=bool(self.var_hang_youfeng.get()),
            jianglong_hang=bool(
                getattr(self, "var_hang_jianglong", None).get()
                if getattr(self, "var_hang_jianglong", None) else False
            ),
            auto_open_monster=bool(
                getattr(self, "var_hang_auto_open_monster", None).get()
                if getattr(self, "var_hang_auto_open_monster", None) else False
            ),
            open_monster_rows=max(0, min(3, self._parse_int(
                getattr(self, "var_hang_open_monster_rows", None).get()
                if getattr(self, "var_hang_open_monster_rows", None) else "", 0, lo=0, hi=3
            ))),
            skip_dungeon_story=bool(self.var_hang_skip_story.get()),
            auto_repair=bool(self.var_hang_auto_repair.get()),
            repair_below_pct=repair_pct,
            auto_vitality=bool(self.var_hang_auto_vitality.get()),
            vitality_below_pct=vitality_pct,
            ignore_dungeon_stuck=bool(
                getattr(self, "var_hang_ignore_dungeon_stuck", None).get()
                if getattr(self, "var_hang_ignore_dungeon_stuck", None) else False
            ),
        )


    def _on_hang_ignore_dungeon_stuck_toggle(self) -> None:
        """Refresh the hang tip when dungeon card-monster ignore flips. @author by ak"""
        self._update_hang_tip()

    def _update_hang_wanzi_ui(self, *_args) -> None:
        """Show the shared frequency while either optional feature is enabled."""
        wanzi_on = False
        try:
            wanzi_on = bool(self.var_hang_wanzi_neigong.get()) or bool(
                self.var_hang_wanzi_waigong.get()
            )
            on = wanzi_on or bool(self.var_hang_youfeng.get())
        except Exception:
            on = False
        try:
            if on:
                self.frm_hang_wanzi_iv.pack(side=tk.LEFT, padx=(12, 0))
            else:
                self.frm_hang_wanzi_iv.pack_forget()
        except Exception:
            pass
        try:
            if on:
                cur = str(self.var_hang_wanzi_iv.get() or "").strip()
                if not cur:
                    self.var_hang_wanzi_iv.set(str(int(DEFAULT_WANZI_INTERVAL_MS)))
        except Exception:
            pass
        try:
            if bool(getattr(self, "_wanzi_dev_cadence_enabled", False)) and wanzi_on:
                self.frm_hang_wanzi_dev.pack(
                    fill=tk.X,
                    pady=(0, 2),
                    after=self._row_hang_wanzi,
                )
            else:
                self.frm_hang_wanzi_dev.pack_forget()
        except Exception:
            pass

    def _on_hang_wanzi_neigong_toggle(self) -> None:
        try:
            if bool(self.var_hang_wanzi_neigong.get()):
                self.var_hang_wanzi_waigong.set(False)
                self.var_hang_youfeng.set(False)
        except Exception:
            pass
        self._update_hang_wanzi_ui()
        self._update_hang_tip()

    def _on_hang_wanzi_waigong_toggle(self) -> None:
        try:
            if bool(self.var_hang_wanzi_waigong.get()):
                self.var_hang_wanzi_neigong.set(False)
                self.var_hang_youfeng.set(False)
        except Exception:
            pass
        self._update_hang_wanzi_ui()
        self._update_hang_tip()

    def _on_hang_youfeng_toggle(self) -> None:
        try:
            if bool(self.var_hang_youfeng.get()):
                self.var_hang_empty.set(False)
                self.var_hang_wanzi_neigong.set(False)
                self.var_hang_wanzi_waigong.set(False)
        except Exception:
            pass
        self._update_hang_wanzi_ui()
        self._update_hang_tip()

    def _team_control_enabled_for_jianglong(self) -> bool:
        """Read the live page choice, falling back to hydrated settings."""
        team_var = getattr(self, "var_team_control_enabled", None)
        if team_var is not None:
            try:
                value = bool(team_var.get())
                self.settings["team_control_enabled"] = value
                return value
            except Exception:
                pass
        return team_control_flag_enabled(self.settings)

    def _sync_current_role_control_for_jianglong(self) -> None:
        """Refresh this window from the mounted role's persisted control settings."""
        rid = self._current_role_id()
        if not rid:
            return
        from app.core.hang_settings import sync_role_prefs_to_settings

        sync_role_prefs_to_settings(self.settings, rid)
        team_var = getattr(self, "var_team_control_enabled", None)
        if team_var is not None:
            team_var.set(team_control_flag_enabled(self.settings))
    def _jianglong_control_mode(self) -> str:
        """Return the active orchestration transport: local, team, or none."""
        role = self._control_role_shared()
        if role not in (ROLE_MASTER, ROLE_SLAVE):
            return ""
        return "team" if self._team_control_enabled_for_jianglong() else "local"
    def _on_hang_jianglong_toggle(self) -> None:
        """Refresh tip and orchestration lifecycle when 自动降龙 flips. @author by ak"""
        self._update_hang_tip()
        try:
            self._reconcile_jianglong_runner()
        except Exception as e:
            self.log(f"降龙编排: 切换处理异常 {e}")

    def _jianglong_enabled_for_pid(self, pid: int) -> tuple[bool, dict]:
        """Read this process's character config without relying on page variables."""
        state = {
            "checked": False,
            "char_id": "",
            "name": "",
            "error": "",
        }
        attach = None
        try:
            from app.core.super_loot import open_attach_session
            from app.core.team_ops import read_host_identity

            attach = open_attach_session(int(pid), log=lambda _m: None)
            live_name, object_id = read_host_identity(attach, log=lambda _m: None)
            char_id = normalize_hang_char_id(object_id)
            if not char_id:
                state["error"] = "character id unavailable"
                return False, state
            cfg = get_hang_config(char_id=char_id)
            state.update(
                checked=True,
                char_id=str(char_id),
                name=str(live_name or "").strip(),
            )
            return bool(getattr(cfg, "jianglong_hang", False)), state
        except Exception as exc:
            state["error"] = str(exc)
            return False, state
        finally:
            if attach is not None:
                try:
                    attach.close()
                except Exception:
                    pass

    def _jianglong_presence(
        self,
        *,
        force_hang_refresh: bool = False,
        ignore_scene: bool = False,
    ) -> dict:
        """当前窗口的降龙参与状态（上报自己）。@author by ak"""
        pid = int(self._fixed_pid or 0)
        if not pid:
            return {}
        enabled, jianglong_config = self._jianglong_enabled_for_pid(pid)
        # 测试只绕过地图门槛，仍要求该 PID 自己的角色配置已开启降龙。
        hwnd = 0
        name = str(jianglong_config.get("name") or "").strip()
        role = self._control_role_shared()
        try:
            sess = self.selected_session()
            if sess is not None:
                hwnd = int(getattr(sess, "hwnd", 0) or 0)
        except Exception:
            hwnd = 0
        try:
            name = self._hang_char_name_label()
        except Exception:
            name = ""
        # The test switch belongs to the master that starts orchestration.
        # A slave callback must never inherit it from shared settings or
        # another page instance; it reports only its own live state.
        test_enabled = bool(getattr(self, "_jianglong_test_enabled", False))
        in_wuzun = bool(ignore_scene or test_enabled)
        try:
            snap = get_live_scene(pid)
            if snap is not None and snap.scene_id is not None:
                in_wuzun = in_wuzun or int(snap.scene_id) in WUZUN_SCENE_IDS
        except Exception:
            pass
        hang_running = False
        hang_live: dict = {
            "checked": False,
            "ok": False,
            "running": None,
            "mode": None,
            "error": "",
        }
        # 正常模式只在「勾选 + 位于武尊堂」时读挂机状态；测试模式绕过地图限制，
        # 避免非参与窗口每秒开 session 刷屏。参与窗口仍保留 3s 缓存节流。
        if enabled and in_wuzun:
            try:
                now = time.time()
                last = getattr(self, "_jianglong_hang_checked_at", 0.0)
                if force_hang_refresh or now - float(last) >= 3.0:
                    # 查询回报必须按接收副控自己的 pid 直接附加，不依赖当前
                    # SettingsPage 选中的 session，避免页面焦点/会话刷新造成漏读。
                    attach = None
                    try:
                        from app.core.super_loot import open_attach_session

                        attach = open_attach_session(pid, log=lambda _m: None)
                        st = read_hang_live(attach, log=lambda _m: None)
                        hang_live = {
                            "checked": True,
                            "ok": bool(st.ok),
                            "running": st.running,
                            "mode": st.mode,
                            "error": str(st.error or ""),
                        }
                        self._jianglong_hang_running_cache = bool(st.running)
                    except Exception as exc:
                        hang_live = {
                            "checked": True,
                            "ok": False,
                            "running": None,
                            "mode": None,
                            "error": str(exc),
                        }
                    finally:
                        if attach is not None:
                            try:
                                attach.close()
                            except Exception:
                                pass
                    self._jianglong_hang_checked_at = now
                if force_hang_refresh:
                    # 队内查询必须和「刷新状态」一样只认本次 read_hang_live；
                    # 读取失败不能把旧缓存误当作本次已经开挂。
                    hang_running = hang_live.get("running") is True
                else:
                    hang_running = bool(
                        getattr(self, "_jianglong_hang_running_cache", False)
                    )
            except Exception as exc:
                hang_live = {
                    "checked": True,
                    "ok": False,
                    "running": None,
                    "mode": None,
                    "error": str(exc),
                }
                hang_running = False if force_hang_refresh else bool(
                    getattr(self, "_jianglong_hang_running_cache", False)
                )
        return {
            "pid": pid,
            "name": name,
            "jianglong_config": jianglong_config,
            "hwnd": hwnd,
            "role": role,
            "enabled": enabled,
            "in_wuzun": in_wuzun,
            "hang_running": hang_running,
            "hang_live": hang_live,
        }

    def _report_jianglong_presence(self, *, ignore_scene: bool = False) -> None:
        """上报本窗口参与状态到进程内注册表。@author by ak"""
        info = self._jianglong_presence(ignore_scene=ignore_scene)
        if not info:
            drop_participant(int(self._fixed_pid or 0))
            return
        report_participant(
            int(info["pid"]),
            name=info["name"],
            hwnd=info["hwnd"],
            role=info["role"],
            enabled=info["enabled"],
            in_wuzun=info["in_wuzun"],
            hang_running=info["hang_running"],
        )
        self._push(
            "log",
            f"降龙编排: 本机报名 pid={info['pid']} name={info['name'] or '-'} "
            f"enabled={int(info['enabled'])} scene={int(info['in_wuzun'])} "
            f"hang={int(info['hang_running'])} role={info['role'] or '-'}",
        )

    def _jianglong_roster(self) -> list[dict]:
        """主控侧参与者名单。

        仅使用已上报的参与者（勾选 + 武尊堂 + 挂机 + 主副控）。不能把
        队伍名单直接当作参与名单，否则未开启降龙的角色会占用轮次。@author by ak
        """
        control_mode = str(getattr(self, "_jianglong_roster_mode", "") or "")
        if control_mode not in ("team", "local"):
            control_mode = self._jianglong_control_mode()
        roster: list[dict] = []
        if control_mode == "team":
            # 队内控是完全独立的传输域：名单只来自本主控本次快照和
            # 队伍频道的回报，不能混入同机 TaskSyncHub 的本地报名。
            master = dict(getattr(self, "_jianglong_team_master_state", {}) or {})
            if (
                master.get("enabled")
                and master.get("in_wuzun")
                and master.get("hang_running")
            ):
                roster.append(
                    {
                        "pid": int(master.get("pid") or self._fixed_pid or 0),
                        "name": str(master.get("name") or "").strip(),
                        "hwnd": int(master.get("hwnd") or 0),
                        "role": ROLE_MASTER,
                    }
                )
        else:
            # The runner reads roster once after the fixed query window. Close
            # the local reply window before freezing the collected participants.
            end_local_jianglong_collection(int(self._fixed_pid or 0))
            roster = eligible_participants()
        query_at = float(getattr(self, "_jianglong_query_at", 0.0) or 0.0)
        if control_mode == "team":
            for name, state in dict(getattr(self, "_team_slaves", {}) or {}).items():
                if float(state.get("jianglong_seen_at") or 0.0) < query_at:
                    continue
                if not (
                    state.get("jianglong_enabled")
                    and state.get("jianglong_in_wuzun")
                    and state.get("jianglong_hang_running")
                ):
                    continue
                roster.append(
                    {
                        "pid": 0,
                        "name": str(name or "").strip(),
                        "hwnd": 0,
                        "role": ROLE_SLAVE,
                        "remote": True,
                    }
                )
        # 去重（按 pid 或 name），保持稳定顺序。
        seen_pids: set[int] = set()
        seen_names: set[str] = set()
        out: list[dict] = []
        for item in roster:
            pid_i = int(item.get("pid") or 0)
            nm = str(item.get("name") or "").strip()
            key_pid = pid_i if pid_i else None
            key_name = nm or None
            if key_pid is not None and key_pid in seen_pids:
                continue
            if key_name is not None and key_name in seen_names:
                continue
            if key_pid is not None:
                seen_pids.add(key_pid)
            if key_name is not None:
                seen_names.add(key_name)
            out.append(item)
        out.sort(key=lambda d: (0 if int(d.get("pid") or 0) > 0 else 1, str(d.get("name") or "")))
        names = [str(item.get("name") or item.get("pid") or "?") for item in out]
        roster_text = "、".join(names) if names else "无"
        self._push("log", f"降龙编排: 收集完成，共{len(out)}人：{roster_text}")
        try:
            self.user_log(
                f"降龙编排：收集到参与名单（{len(out)}人）{roster_text}",
                category=CAT_CONTROL,
            )
        except Exception:
            pass
        return out

    def _jianglong_remote_party_members(self, local_names: set[str]) -> list[dict]:
        """读主控本机队伍名单，返回非本机的队伍成员作为跨机目标。

        有 10s 缓存：调度线程轮询 roster 时避免反复 attach 读队伍。@author by ak
        """
        pid = int(self._fixed_pid or 0)
        if not pid:
            return []
        now = time.time()
        last = getattr(self, "_jianglong_team_read_at", 0.0)
        cached = getattr(self, "_jianglong_team_cache", None)
        if now - float(last) < 10.0 and cached is not None:
            return [
                dict(m)
                for m in cached
                if str(m.get("name") or "").strip() not in local_names
            ]
        try:
            from app.core.super_loot import open_attach_session
            from app.core.team_ops import list_party_members

            attach = open_attach_session(pid, log=lambda _m: None)
            try:
                members = list_party_members(
                    attach,
                    store=self.store,
                    exclude_pid=pid,
                    fresh=True,
                    log=lambda _m: None,
                ) or []
            finally:
                try:
                    attach.close()
                except Exception:
                    pass
        except Exception:
            return []
        out: list[dict] = []
        for m in members or []:
            nm = str(m.get("name") or "").strip()
            if not nm or nm in local_names:
                continue
            out.append(
                {
                    "pid": 0,
                    "name": nm,
                    "hwnd": 0,
                    "role": "slave",
                    "remote": True,
                }
            )
        self._jianglong_team_read_at = now
        self._jianglong_team_cache = [dict(m) for m in out]
        return out

    def _jianglong_cast_target(self, target: dict) -> None:
        """主控调度回调：本机窗口直接施法，非本机(云控)发定向事件。@author by ak"""
        target_pid = int(target.get("pid") or 0)
        hwnd = int(target.get("hwnd") or 0)
        name = str(target.get("name") or "").strip()
        # 队内控模式下，主控自己仍直接施法；其他目标统一走队伍频道，
        # 即使目标是本机多开副控，也必须让副控按角色名过滤后执行。
        runner_mode = str(getattr(self, "_jianglong_runner_mode", "") or "")
        team_enabled = (
            runner_mode == "team"
            if runner_mode in ("local", "team")
            else self._team_control_enabled_for_jianglong()
        )
        master_pid = int(self._fixed_pid or 0)
        is_master_target = bool(target_pid and target_pid == master_pid)
        is_local = bool(target_pid and self.store.get(target_pid) is not None)

        def _cast() -> None:
            res = cast_jianglong_once(
                target_pid, hwnd=hwnd, log=lambda m: self._push("log", m)
            )
            ok = bool(res.get("ok"))
            detail = (
                f"ret={res.get('ret')} sid=0x{int(res.get('skill_id') or 0):X} native"
                if ok
                else str(res.get("error") or res.get("note") or "unknown error")
            )
            self._push(
                "log",
                f"降龙编排: pid={target_pid} "
                f"{'成功' if ok else '失败'} {detail}",
            )
            try:
                self.user_log(
                    f"降龙编排：{name or target_pid} 释放降龙"
                    + ("成功" if ok else f"失败 {detail}"),
                    category=CAT_CONTROL,
                )
            except Exception:
                pass

        if is_master_target or (is_local and not team_enabled):
            threading.Thread(target=_cast, daemon=True, name="jianglong-local").start()
            return
        if team_enabled and not is_master_target:
            if not name:
                self._push(
                    "log",
                    f"降龙编排: 队内控目标缺名字 pid={target_pid}，跳过该轮",
                )
                return
            text = build_master_command(
                ACTION_JIANGLONG_CAST,
                [0],
                extra=name,
            )
            result = send_team_message(
                master_pid,
                text,
                log=lambda m: self._push("log", m),
            )
            self._push(
                "log",
                f"降龙编排: 队内控已发送目标 pid={target_pid} name={name} "
                f"ok={result.get('ok')} text={text}",
            )
            try:
                self.user_log(
                    f"主控通知：通知 {name} 释放降龙（队内控）"
                    + ("已发送" if result.get("ok") else "发送失败"),
                    category=CAT_CONTROL,
                )
            except Exception:
                pass
            return
        # 非本机（云控扩展）：必须有名才能定向；缺名静默跳过，避免全副控误放。
        if not name:
            self._push(
                "log",
                f"降龙编排: 云控目标缺名字 pid={target_pid}，跳过该轮",
            )
            return
        task_page = self._sibling_page("task")
        if task_page is not None and hasattr(task_page, "_publish_task_sync"):
            notified = task_page._publish_task_sync(
                ACTION_JIANGLONG_CAST,
                0,
                name=name,
                members=name,
            )
            self._push(
                "log",
                f"降龙编排: 已通知副控 pid={target_pid} name={name or '-'} "
                f"hit={notified}",
            )
            try:
                self.user_log(
                    f"主控通知：通知 {name} 释放降龙（群控）"
                    + ("已发送" if notified else "发送失败"),
                    category=CAT_CONTROL,
                )
            except Exception:
                pass
            return
        threading.Thread(target=_cast, daemon=True, name="jianglong-slave").start()

    def _request_jianglong_team_roster(self) -> None:
        """Ask team-control slaves for their current Jianglong participation state."""
        team_enabled = self._team_control_enabled_for_jianglong()
        role = self._control_role_shared()
        if not team_enabled:
            self._push("log", "降龙编排: 队内控未开启，跳过副控状态查询")
            return
        if role != ROLE_MASTER:
            self._push(
                "log",
                f"降龙编排: 当前角色={role}，只有主控才能发送队内控状态查询",
            )
            return
        pid = int(self._fixed_pid or 0)
        if not pid:
            self._push("log", "降龙编排: 未挂载游戏，无法发送队内控状态查询")
            return
        try:
            self._team_slaves = {}
            self._jianglong_query_at = time.time()
            test_mode = int(bool(getattr(self, "_jianglong_test_enabled", False)))
            query_msg_id = f"{time.strftime('%m%d%H%M%S')}"
            text = build_master_jianglong_query(
                test_mode=bool(test_mode),
                msg_id=query_msg_id,
            )
            self._jianglong_team_query_msg_id = query_msg_id
            result = send_team_message(pid, text, log=lambda m: self._push("log", m))
            self._push(
                "log",
                f"降龙编排: 已发送队内控参与查询 test={test_mode} "
                f"ok={result.get('ok')} wait=8.0s"
            )
        except Exception as e:
            self._push("log", f"降龙编排: 队内控状态查询失败 {e}")

    def _reconcile_jianglong_runner(self) -> None:
        """Master starts one explicit 8-second collection, then rotates it."""
        pid = int(self._fixed_pid or 0)
        if not pid:
            return
        enabled = bool(getattr(self, "_jianglong_test_enabled", False))
        try:
            enabled = enabled or bool(getattr(self, "var_hang_jianglong", None).get())
        except Exception:
            pass
        role = self._control_role_shared()
        control_mode = self._jianglong_control_mode()
        in_wuzun = bool(getattr(self, "_jianglong_test_enabled", False))
        try:
            snap = get_live_scene(pid)
            if snap is not None and snap.scene_id is not None:
                in_wuzun = in_wuzun or int(snap.scene_id) in WUZUN_SCENE_IDS
        except Exception:
            pass
        runner = getattr(self, "_jianglong_runner", None)
        runner_running = bool(runner is not None and runner.is_running())
        test_mode = bool(getattr(self, "_jianglong_test_enabled", False))
        entry_ready = bool(test_mode)
        entry_wait_s: float | None = None
        if not test_mode and in_wuzun:
            try:
                from app.core.remote_runtime import (
                    get_pid_scene_snapshot,
                    is_pid_scene_snapshot_stable,
                )

                scene_state = get_pid_scene_snapshot(pid, max_age_s=2.5)
                scene_id = int(scene_state.get("scene_id") or 0)
                if (
                    scene_id in WUZUN_SCENE_IDS
                    and is_pid_scene_snapshot_stable(pid)
                ):
                    entry_wait_s = jianglong_entry_settle_remaining(
                        scene_id,
                        scene_state.get("stable_since"),
                    )
                    entry_ready = entry_wait_s <= 0.0
            except Exception:
                entry_wait_s = None
        if (
            bool(enabled)
            and role == ROLE_MASTER
            and bool(control_mode)
            and in_wuzun
            and not entry_ready
            and not runner_running
        ):
            gate_key = (
                "stabilizing"
                if entry_wait_s is None
                else f"warmup:{int(max(0.0, entry_wait_s) + 0.999)}"
            )
            if gate_key != str(getattr(self, "_jianglong_entry_gate_state", "") or ""):
                self._jianglong_entry_gate_state = gate_key
                if entry_wait_s is None:
                    self._push("log", "降龙编排: 等待武尊堂场景稳定后再预热10秒…")
                else:
                    self._push(
                        "log",
                        f"降龙编排: 武尊堂场景已稳定，预热 {entry_wait_s:.1f}s 后收集参与者…",
                    )
        else:
            self._jianglong_entry_gate_state = ""
        should_run = (
            bool(enabled)
            and role == ROLE_MASTER
            and bool(control_mode)
            and (in_wuzun or test_mode)
            # 预热只限制首次启动；运行中不因一次快照延迟而中断。
            and (entry_ready or runner_running)
        )
        if should_run and (runner is None or not runner.is_running()):
            if runner is not None:
                try:
                    runner.stop()
                except Exception:
                    pass
            end_local_jianglong_collection(pid)
            clear_participants()
            self._team_slaves = {}
            self._jianglong_query_at = time.time()
            if control_mode == "team":
                # Team mode is exclusive: including same-PC windows, all
                # participant replies and later cast commands use team chat.
                self._jianglong_team_master_state = self._jianglong_presence(
                    force_hang_refresh=True
                )
                self._request_jianglong_team_roster()
            else:
                clear_participants()
                begin_local_jianglong_collection(pid, window_s=8.0)
                # 主控只在自己发起的本地收集窗口内报名一次。
                self._report_jianglong_presence()
                hit = get_task_sync_hub().publish(
                    action=ACTION_JIANGLONG_QUERY,
                    task_id=0,
                    source_pid=pid,
                )
                self._push(
                    "log",
                    f"降龙编排: 主控发起本地群控参与查询 hit={hit} wait=8.0s",
                )
            # 名单在 runner 后台线程冻结；不能在那里读取 Tk 变量，
            # 否则队内控勾选会回退为本地模式。
            self._jianglong_roster_mode = control_mode
            runner = JianglongRotationRunner(
                pid,
                roster_fn=self._jianglong_roster,
                cast_fn=self._jianglong_cast_target,
                roster_settle_s=8.0,
                log=lambda m: self._push("log", m),
                on_status=lambda m: self._push("log", m),
            )
            runner.start()
            self._jianglong_runner = runner
            self._jianglong_runner_mode = control_mode
            self._push(
                "log",
                f"降龙编排: 主控已发起参与收集 pid={pid} mode={control_mode} "
                f"wait=8.0s in_wuzun={int(in_wuzun)} "
                f"test={int(getattr(self, '_jianglong_test_enabled', False))}",
            )
        elif not should_run and runner is not None:
            try:
                runner.stop()
            except Exception:
                pass
            end_local_jianglong_collection(pid)
            clear_participants()
            self._jianglong_team_master_state = {}
            self._jianglong_runner = None
            self._jianglong_runner_mode = ""
            self._jianglong_roster_mode = ""
            self._push(
                "log",
                f"降龙编排: 主控调度已停止 pid={pid} "
                f"(enabled={int(enabled)} role={role} mode={control_mode or '-'} in_wuzun={int(in_wuzun)})",
            )
    def _tick_jianglong(self) -> None:
        """Only maintain master lifecycle; participants reply to explicit queries."""
        try:
            self._reconcile_jianglong_runner()
        except Exception:
            pass
        self._jianglong_tick_job = self.after(1000, self._tick_jianglong)
    def _on_jianglong_test_toggle(self) -> None:
        """Toggle test orchestration, bypassing the Wuzun scene gate. @author by ak"""
        if not bool(getattr(self, "_jianglong_test_enabled", False)):
            try:
                self._sync_current_role_control_for_jianglong()
            except Exception as e:
                self.log(f"降龙编排测试: 刷新当前角色配置失败 {e}")
        self._jianglong_test_enabled = not bool(
            getattr(self, "_jianglong_test_enabled", False)
        )
        try:
            self.btn_jianglong_test.configure(
                text=(
                    "关闭降龙编排"
                    if self._jianglong_test_enabled
                    else "开启降龙编排"
                )
            )
        except Exception:
            pass
        self._push(
            "log",
            "降龙编排测试："
            + ("已开启，忽略武尊堂地图限制" if self._jianglong_test_enabled else "已关闭"),
        )
        try:
            self._reconcile_jianglong_runner()
        except Exception as e:
            self.log(f"降龙编排测试: 切换处理异常 {e}")

    def _on_hang_wall_toggle(self) -> None:
        """穿墙按钮开/关。@author by ak"""
        pid = int(self._fixed_pid or 0)
        if not pid:
            return
        try:
            from app.core.wall_clip import set_wall_clip, wall_clip_state

            cur = wall_clip_state(pid)
            want = not cur
            set_wall_clip(pid, want)
            actual = wall_clip_state(pid)
            try:
                self.btn_hang_wall.configure(
                    text="穿墙关" if actual else "穿墙开"
                )
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            try:
                self.user_log(f"穿墙失败: {e}", source="挂机设置")
            except Exception:
                pass

    def _on_hang_empty_toggle(self) -> None:
        try:
            if bool(self.var_hang_empty.get()):
                self.var_hang_youfeng.set(False)
        except Exception:
            pass
        self._update_hang_wanzi_ui()
        self._update_hang_tip()

    def _update_hang_tip(self, *_args) -> None:
        """Show captain/dungeon Alt+R + vitality readiness tip. @author by ak"""
        try:
            cfg = self._hang_cfg_from_ui()
            tips = hang_start_warnings(cfg)
            self.var_hang_tip.set("；".join(tips) if tips else "")
        except Exception:
            try:
                self.var_hang_tip.set("")
            except Exception:
                pass

    def _apply_hang_cfg_to_session(
        self, cfg: HangConfig | None = None, *, role_id: str | None = None
    ) -> HangConfig:
        """Write UI hang config into session settings and the locked role file."""
        cfg = cfg or self._hang_cfg_from_ui()
        write_hang_config_to_settings(self.settings, cfg)
        try:
            sess = getattr(self, "_session", None) or getattr(self, "session", None)
            pid = int(getattr(sess, "pid", 0) or 0) if sess is not None else 0
            if pid > 0:
                update_hang_guard_config(pid, cfg, log=self.log)
        except Exception:
            pass
        cid = str(role_id or self._current_role_id() or "").strip()
        _cached_id, cname = self._hang_identity()
        try:
            from app.core.account_manager import role_dir

            target_path = role_dir(cid) / "hang.json"
        except Exception:
            target_path = None
        self.log(
            f"挂机设置: 开始落盘 char_id={cid or '-'} name={cname or '-'} "
            f"path={target_path or '-'}"
        )
        try:
            saved = save_hang_disk_from_config(
                cfg,
                char_id=cid,
                char_name=cname or None,
            )
            if target_path is not None:
                self.log(
                    f"挂机设置: 落盘完成 path={target_path} "
                    f"radius={saved.get('radius')} mode={saved.get('mode')}"
                )
        except Exception as e:
            self.log(
                f"挂机设置: 落盘失败 char_id={cid or '-'} name={cname or '-'} "
                f"path={target_path or '-'} {e}"
            )
            raise
        return cfg
    def _apply_saved_hang_attributes(self, cfg: HangConfig) -> None:
        """Apply saved hang properties now; this never toggles the hang switch."""
        mounted = self._require_session()
        if mounted is None:
            return
        pid = int(getattr(mounted, "pid", 0) or 0)
        hwnd = int(getattr(mounted, "hwnd", 0) or 0)
        if not pid:
            return

        def worker() -> None:
            attach = None
            try:
                from app.core.super_loot import open_attach_session

                attach = open_attach_session(pid, log=lambda m: self._push("log", m))
                if attach is None:
                    self._push(
                        "hang_saved_apply",
                        {"ok": False, "message": "保存后应用挂机属性：附加游戏失败"},
                    )
                    return
                attach.hwnd = int(getattr(attach, "hwnd", 0) or hwnd)
                result = apply_hang_prepare(
                    attach, cfg, log=lambda m: self._push("log", m)
                )
                self._push("hang_saved_apply", result)
            except Exception as e:
                self._push(
                    "hang_saved_apply",
                    {"ok": False, "message": f"保存后应用挂机属性异常: {e}"},
                )
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass

        threading.Thread(
            target=worker, daemon=True, name=f"hang-save-apply-{pid}"
        ).start()

    def _open_hang_attach(self):
        """Open GameAttachSession for hang ops. @author by ak"""
        sess = self._require_session()
        if sess is None:
            return None, None
        try:
            from app.core.super_loot import open_attach_session

            attach = open_attach_session(int(sess.pid), log=lambda m: self.log(m))
            if attach is not None and not getattr(attach, "hwnd", 0):
                try:
                    attach.hwnd = int(sess.hwnd or 0)
                except Exception:
                    pass
            return sess, attach
        except Exception as e:
            self.var_hang_status.set(f"附加失败: {e}")
            self.log(f"挂机设置: attach 失败 {e}")
            return sess, None

    def _set_hang_busy(self, busy: bool) -> None:
        """Prevent overlapping start/stop calls from the settings panel."""
        self._hang_busy = bool(busy)
        state = tk.DISABLED if busy else tk.NORMAL
        for name in ("btn_hang_start", "btn_hang_stop"):
            try:
                getattr(self, name).configure(state=state)
            except Exception:
                pass
        try:
            if not self._wanzi_packet_busy:
                self.btn_wanzi_packet.configure(state=state)
        except Exception:
            pass

    def _set_youfeng_hook_button(self, running: bool, *, busy: bool = False) -> None:
        """Render only the manual hook owner; hang ownership is intentionally ignored."""
        self._youfeng_hook_busy = bool(busy)
        self._youfeng_hook_enabled = bool(running)
        try:
            text = "处理中…" if busy else ("关闭有凤" if running else "开启有凤")
            self.btn_youfeng_hook.configure(
                text=text,
                state=tk.DISABLED if busy else tk.NORMAL,
            )
        except Exception:
            pass

    def _refresh_youfeng_hook_button(self) -> None:
        sess = None
        try:
            sess = self.selected_session()
        except Exception:
            pass
        if sess is None:
            self._set_youfeng_hook_button(False)
            return
        state = get_youfeng_key_hook_state(
            int(getattr(sess, "pid", 0) or 0),
            owner=YOUFENG_HOOK_OWNER_MANUAL,
        )
        self._set_youfeng_hook_button(bool(state.get("enabled")))

    def _on_youfeng_hook_toggle(self) -> None:
        """Toggle physical 8/Alt+8 hooks without reading or changing hang state."""
        if self._youfeng_hook_busy:
            return
        sess = self._require_session()
        if sess is None:
            return
        pid = int(getattr(sess, "pid", 0) or 0)
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        enable = not bool(self._youfeng_hook_enabled)
        self._set_youfeng_hook_button(not enable, busy=True)

        def worker() -> None:
            try:
                if enable:
                    result = start_youfeng_key_hook(
                        sess,
                        owner=YOUFENG_HOOK_OWNER_MANUAL,
                        hwnd=hwnd,
                        log=lambda m: self._push("log", m),
                    )
                else:
                    result = stop_youfeng_key_hook(
                        pid,
                        owner=YOUFENG_HOOK_OWNER_MANUAL,
                        log=lambda m: self._push("log", m),
                    )
            except Exception as exc:
                result = {
                    "ok": False,
                    "enabled": not enable,
                    "message": str(exc),
                }
            result["requested_enabled"] = enable
            self._push("youfeng_hook_done", result)

        threading.Thread(
            target=worker,
            daemon=True,
            name=f"youfeng-key-hook-{pid}",
        ).start()

    def _set_wanzi_packet_button(self, running: bool, *, busy: bool = False) -> None:
        """Render the manual direct-packet sender button."""
        self._wanzi_packet_busy = bool(busy)
        self._wanzi_packet_enabled = bool(running)
        try:
            text = "处理中…" if busy else ("关闭释放丸子" if running else "开启释放丸子")
            disabled = bool(busy or getattr(self, "_hang_busy", False))
            self.btn_wanzi_packet.configure(
                text=text,
                state=tk.DISABLED if disabled else tk.NORMAL,
            )
        except Exception:
            pass

    def _refresh_wanzi_packet_button(self) -> None:
        sess = None
        try:
            sess = self.selected_session()
        except Exception:
            pass
        if sess is None:
            self._set_wanzi_packet_button(False)
            return
        try:
            state = get_wanzi_packet_state(
                int(getattr(sess, "pid", 0) or 0), owner="manual"
            )
            self._set_wanzi_packet_button(bool(state.get("enabled")))
        except Exception:
            self._set_wanzi_packet_button(False)

    def _on_wanzi_packet_toggle(self) -> None:
        """Toggle the manual owner of the shared per-PID 丸子 sender."""
        if self._wanzi_packet_busy or getattr(self, "_hang_busy", False):
            return
        sess = self._require_session()
        if sess is None:
            return
        pid = int(getattr(sess, "pid", 0) or 0)
        enable = not bool(self._wanzi_packet_enabled)
        kind = ""
        if bool(self.var_hang_wanzi_neigong.get()):
            kind = WANZI_KIND_NEIGONG
        elif bool(self.var_hang_wanzi_waigong.get()):
            kind = WANZI_KIND_WAIGONG
        if enable and not kind:
            self.var_hang_status.set("请先勾选内功丸子挂机或外功丸子挂机")
            return
        cadence_cfg = self._hang_cfg_from_ui()
        interval = int(cadence_cfg.wanzi_interval_ms)
        self._set_wanzi_packet_button(not enable, busy=True)

        def worker() -> None:
            try:
                if enable:
                    result = start_wanzi_packet_manual(
                        sess,
                        interval,
                        kind=kind,
                        low_rate_seconds=cadence_cfg.wanzi_low_rate_seconds,
                        low_rate_pairs_per_second=cadence_cfg.wanzi_low_rate_pairs_per_second,
                        active_window_s=cadence_cfg.wanzi_active_window_s,
                        log=lambda m: self._push("log", m),
                    )
                else:
                    result = stop_wanzi_packet_manual(
                        pid, log=lambda m: self._push("log", m)
                    )
            except Exception as exc:
                result = {"ok": False, "enabled": not enable, "message": str(exc)}
            result["requested_enabled"] = enable
            self._push("wanzi_packet_done", result)

        threading.Thread(
            target=worker, daemon=True, name=f"wanzi-packet-manual-{pid}"
        ).start()

    def _on_hang_refresh(self) -> None:
        """Refresh live hang status line. @author by ak"""
        sess, attach = self._open_hang_attach()
        if attach is None:
            if sess is None:
                self.var_hang_status.set("挂机状态=无会话")
            return
        try:
            st = read_hang_live(attach, log=lambda m: self.log(m))
            self.var_hang_status.set(format_hang_live_line(st))
            # optional: show inferred empty skill tip only (do not overwrite user checkbox)
        except Exception as e:
            self.var_hang_status.set(f"挂机状态读失败: {e}")
            self.log(f"挂机设置: 刷新失败 {e}")
        try:
            from app.core.wall_clip import wall_clip_state

            ws = wall_clip_state(self._fixed_pid) if self._fixed_pid else None
            if ws is not None:
                self.btn_hang_wall.configure(text="穿墙关" if ws else "穿墙开")
        except Exception:
            pass


    def _ignore_char_id(self) -> str:
        """Numeric role id for ignore rules; '' when unavailable. @author by ak"""
        try:
            return self._hang_char_id_key()
        except Exception:
            return ""

    def _ignore_run(self, fn, *, done_kind: str) -> None:
        """Run one ignore-settings op off the Tk thread. @author by ak"""
        sess, attach = self._open_hang_attach()
        if attach is None:
            self.var_ignore_status.set("忽略列表：无会话")
            return

        def worker() -> None:
            try:
                r = fn(attach)
                self._push(done_kind, r)
            except Exception as e:
                self._push(done_kind, {"ok": False, "error": str(e)})
            finally:
                try:
                    attach.close()
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True, name="ignore-settings").start()

    def _on_ignore_refresh(self) -> None:
        """Reload the blacklist (tid rules) into the tree. @author by ak"""
        try:
            cid = self._ignore_char_id()
            from app.core.dungeon_target_policy import (
                PACKAGED_DUNGEON_TARGET_RULES,
            )
            from app.core.ignore_rules import load_char_rules

            # Packaged baseline (北疆疯丐 / 上官霸刀) is always shipped in the
            # frozen release; show it read-only above the per-role rules so
            # users can see what the dungeon guard actually carries.
            packaged: list[dict] = [
                {
                    "tid": int(tid),
                    "name": f"{name}(内置)",
                    "added_at": 0.0,
                    "packaged": True,
                }
                for tid, name in PACKAGED_DUNGEON_TARGET_RULES
            ]
            dynamic = load_char_rules(cid)
            for row in dynamic:
                row["packaged"] = False
            rules = packaged + dynamic
            self.ignore_tree.delete(*self.ignore_tree.get_children())
            self._ignore_rows = rules
            for rule in rules:
                self.ignore_tree.insert(
                    "",
                    "end",
                    values=(
                        rule.get("name") or "?",
                        f"0x{int(rule.get('tid') or 0):X}",
                    ),
                )
            self.var_ignore_status.set(f"黑名单：{len(rules)} 项")
        except Exception as e:
            self.var_ignore_status.set(f"黑名单加载失败: {e}")

    def _on_ignore_add_current(self) -> None:
        """加入当前锁定的目标为黑名单。@author by ak"""
        cid = self._ignore_char_id()

        def _add(attach) -> dict:
            from app.core import ignore_policy_lab as ipo
            from app.core.ignore_rules import add_char_rule

            # Selected target (pure memory) covers manual selection & any mode.
            cur = ipo.read_selected_target(attach)
            oid = int(cur.get("target_id64") or 0)
            if not oid:
                cur = ipo.read_current_target(attach, crt_fallback=True)
                oid = int(cur.get("target_id64") or 0)
            if not oid:
                return {"ok": False, "reason": "当前目标为 0（未选中任何目标）"}
            obj = ipo.resolve_object_by_id64(attach, oid)
            tid = ipo.object_template_id(attach, obj) if obj else 0
            name = ipo.object_name_of(attach, obj) if obj else ""
            if not tid:
                return {"ok": False, "reason": "无法解析当前目标的 tid", "id64": oid}
            saved = add_char_rule(cid, tid, name=name)
            if not saved.get("ok"):
                return saved
            return {"ok": True, "tid": tid, "name": name, "id64": oid}

        self._ignore_run(_add, done_kind="ignore_add_done")

    def _on_ignore_add_done(self, r: dict) -> None:
        """Render 加入当前目标 result then reload blacklist. @author by ak"""
        if not r.get("ok"):
            self.var_ignore_status.set(
                f"加入当前目标失败：{r.get('reason') or r.get('error') or '未知'}"
            )
        else:
            self.var_ignore_status.set(
                f"已加入黑名单：{r.get('name')} tid=0x{r.get('tid'):X}"
            )
        self._on_ignore_refresh()

    def _ignore_selected_row(self):
        """Return the selected blacklist row dict or None. @author by ak"""
        try:
            sel = self.ignore_tree.selection()
            if not sel:
                return None
            idx = self.ignore_tree.index(sel[0])
            rows = self._ignore_rows
            return rows[idx] if 0 <= idx < len(rows) else None
        except Exception:
            return None

    def _on_ignore_remove_selected(self) -> None:
        """Remove the selected tid from the blacklist. @author by ak"""
        row = self._ignore_selected_row()
        if row is None:
            return
        tid = int(row.get("tid") or 0)
        if not tid:
            self.var_ignore_status.set("删除失败：该行无 tid")
            return
        if bool(row.get("packaged")):
            self.var_ignore_status.set("内置目标不可删除")
            return
        cid = self._ignore_char_id()
        from app.core.ignore_rules import remove_char_rule

        remove_char_rule(cid, tid)
        self.var_ignore_status.set(
            f"已删除：{row.get('name')} tid=0x{tid:X}"
        )
        self._on_ignore_refresh()

    def _on_hang_start(self) -> None:
        """Start hang off the Tk thread; the guard keeps its attached session."""
        if self._hang_busy:
            self.var_hang_status.set("挂机操作进行中…")
            return
        sess = self._require_session()
        if sess is None:
            return
        cfg = self._apply_hang_cfg_to_session()
        pid = int(sess.pid)
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        self._set_hang_busy(True)
        self.var_hang_status.set("开启挂机中…")

        def worker() -> None:
            attach = None
            keep_attach = False
            try:
                from app.core.super_loot import open_attach_session

                attach = open_attach_session(pid, log=lambda m: self._push("log", m))
                attach.hwnd = int(getattr(attach, "hwnd", 0) or hwnd)
                # The Hang Settings start button is a save-and-apply command:
                # persist current widgets above, then set all hang attributes
                # before it changes the running switch.
                prepare = apply_hang_prepare(
                    attach, cfg, log=lambda m: self._push("log", m)
                )
                if not bool(prepare.get("ok")):
                    msg = str(prepare.get("message") or "挂机参数设置失败")
                    self._push(
                        "hang_done",
                        {
                            "op": "start",
                            "ok": False,
                            "line": f"开挂前参数设置失败 · {msg}",
                            "message": msg,
                        },
                    )
                    return
                ret = start_hang(
                    attach,
                    cfg,
                    hwnd=hwnd,
                    log=lambda m: self._push("log", m),
                )
                ret["prepare"] = prepare
                ok = bool(ret.get("ok"))
                msg = str(ret.get("message") or "")
                try:
                    st = read_hang_live(attach, log=lambda m: self._push("log", m))
                    line = format_hang_live_line(st) + f" · {msg}"
                except Exception:
                    line = f"{'开挂成功' if ok else '开挂失败'} · {msg}"
                # The periodic guard closes over this session on success.
                guard = ret.get("guard")
                guard_ok = not isinstance(guard, dict) or bool(guard.get("ok", True))
                keep_attach = bool(ok and guard_ok)
                self._push("hang_done", {"op": "start", "ok": ok, "line": line, "message": msg})
            except Exception as e:
                self._push("hang_done", {"op": "start", "ok": False, "line": f"开挂异常: {e}", "message": str(e)})
            finally:
                if attach is not None and not keep_attach:
                    try:
                        attach.close()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True, name=f"hang-start-{pid}").start()

    def _on_hang_stop(self) -> None:
        """Stop hang off the Tk thread so retry/probe delays do not freeze UI."""
        if self._hang_busy:
            self.var_hang_status.set("挂机操作进行中…")
            return
        sess = self._require_session()
        if sess is None:
            return
        cfg = self._apply_hang_cfg_to_session()
        pid = int(sess.pid)
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        self._set_hang_busy(True)
        self.var_hang_status.set("关闭挂机中…")

        def worker() -> None:
            attach = None
            try:
                from app.core.super_loot import open_attach_session

                attach = open_attach_session(pid, log=lambda m: self._push("log", m))
                attach.hwnd = int(getattr(attach, "hwnd", 0) or hwnd)
                ret = stop_hang(
                    attach,
                    cfg,
                    hwnd=hwnd,
                    log=lambda m: self._push("log", m),
                )
                ok = bool(ret.get("ok"))
                msg = str(ret.get("message") or "")
                self._push("hang_done", {"op": "stop", "ok": ok, "line": f"{'已关挂' if ok else '关挂失败'} · {msg}", "message": msg})
            except Exception as e:
                self._push("hang_done", {"op": "stop", "ok": False, "line": f"关挂异常: {e}", "message": str(e)})
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True, name=f"hang-stop-{pid}").start()

    def _on_save(self) -> None:
        """
        Persist service URL + skill-cancel + task role into shared settings.

        @author by ak
        """
        url = DEFAULT_CAPTCHA_BASE_URL
        self.settings["captcha_base_url"] = url
        save_errors: list[str] = []
        # Lock the injected role before any save-side cache refresh can run.
        save_role_id = str(self._current_role_id() or "").strip()
        if not save_role_id:
            self.var_status.set("保存失败：当前注入角色 ID 未绑定")
            self.log("设置: 保存失败，role_id 未绑定")
            return
        # Captcha identify key (shared with 九层妖楼).
        if getattr(self, "var_api_key", None) is not None:
            self.settings["captcha_api_key"] = (self.var_api_key.get() or "").strip()
        try:
            from app.core.captcha_client import api_key_fingerprint
            from app.core.captcha_prefs import save_captcha_api_key

            captcha_path = save_captcha_api_key(
                str(self.settings.get("captcha_api_key") or "")
            )
            self.settings["captcha_api_key_dirty"] = False
            self.log(
                "设置: 识别 Key 已保存 "
                f"path={captcha_path} "
                f"key_hint=({api_key_fingerprint(self.settings.get('captcha_api_key') or '')})"
            )
        except Exception as e:
            self.settings["captcha_api_key_dirty"] = True
            self.var_status.set(f"识别 Key 保存失败: {e}")
            self.log(f"设置: 识别 Key 保存失败: {e}")
            save_errors.append(f"识别 Key: {e}")
        # Persist挂机 before unrelated role/schedule/cloud UI work can abort this save.
        try:
            early_hcfg = self._apply_hang_cfg_to_session(role_id=save_role_id)
            self._apply_saved_hang_attributes(early_hcfg)
            self.log(f"设置: 挂机配置已保存 role_id={save_role_id}")
        except Exception as e:
            save_errors.append(f"挂机配置: {e}")
            self.log(f"设置: 挂机配置保存失败 role_id={save_role_id} {e}")

        role = str(
            getattr(self, "var_task_role", None)
            and self.var_task_role.get()
            or self.settings.get("task_control_role")
            or ROLE_NONE
        ).lower()
        if role not in (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE):
            role = ROLE_NONE
        if role == ROLE_MASTER:
            self._demote_other_masters()
        self.settings["task_control_role"] = role
        try:
            sh = int(
                (
                    getattr(self, "var_sched_hour", None)
                    and self.var_sched_hour.get()
                    or str(DEFAULT_SCHEDULE_HOUR)
                ).strip()
            )
        except Exception:
            sh = DEFAULT_SCHEDULE_HOUR
        try:
            sm = int(
                (
                    getattr(self, "var_sched_minute", None)
                    and self.var_sched_minute.get()
                    or str(DEFAULT_SCHEDULE_MINUTE)
                ).strip()
            )
        except Exception:
            sm = DEFAULT_SCHEDULE_MINUTE
        set_schedule_hm(self.settings, sh, sm)
        if getattr(self, "var_sched_enabled", None) is not None:
            self.settings[SETTING_SCHEDULE_ENABLED] = bool(self.var_sched_enabled.get())
        # Fix this character as the plan owner (captain) and persist per-role.
        try:
            role_id = save_role_id
            if role_id:
                self.settings[SETTING_SCHEDULE_OWNER] = role_id
                profile = profile_from_settings(self.settings)
                profile["captain_id"] = role_id
                save_role_schedule_profile(role_id, profile)
                self.log(
                    f"设置: 定时计划已保存 角色={role_id} "
                    f"enabled={self.settings.get(SETTING_SCHEDULE_ENABLED)}"
                )
        except Exception as e:
            self.log(f"设置: 定时计划保存失败: {e}")
        if getattr(self, "var_cloud_enabled", None) is not None:
            self.settings["cloud_control_enabled"] = bool(self.var_cloud_enabled.get())
        if bool(self.settings.get("cloud_control_enabled")):
            self.settings["cloud_control_master_name"] = self._cloud_master_name_for_apply()
        else:
            self.settings["cloud_control_master_name"] = ""
        # 队内控与云控互斥：保存时以当前勾选为准。
        if getattr(self, "var_team_control_enabled", None) is not None:
            self.settings["team_control_enabled"] = bool(
                self.var_team_control_enabled.get()
            )
        if bool(self.settings.get("team_control_enabled")):
            self.settings["cloud_control_enabled"] = False
        # role_id belongs to the mounted SessionStore context and is immutable
        # for this injection; do not clear or re-discover it during save.
        # 队内控持久化：主/副控 + 队内控标志写入角色 control.json（重启后仍生效）。
        try:
            rid = self._current_role_id()
            if rid:
                from app.core.account_manager import save_role_control

                save_role_control(
                    rid,
                    role=role,
                    team=team_control_flag_enabled(self.settings),
                )
        except Exception as e:
            self.log(f"设置: 队内控持久化失败: {e}")
        self._apply_task_role_ui(role, register=True, apply_cloud=False)
        # Cloud join/leave/poll only starts here (Save), not on checkbox/role edits.
        cloud_label = self._apply_cloud_control(bind_listener=True)
        if (
            bool(self.settings.get("cloud_control_enabled"))
            and role == ROLE_MASTER
            and not str(self.settings.get("cloud_control_master_name") or "").strip()
        ):
            cloud_label = "云控：未读取到本角色名，请先挂载游戏"
        if getattr(self, "var_cloud_status", None) is not None:
            self.var_cloud_status.set(cloud_label)
        self._cloud_ui_dirty = False
        try:
            self._update_team_control_status()
        except Exception:
            pass

        try:
            task_page = self._sibling_page("task")
            if task_page is not None and hasattr(task_page, "_bind_task_sync"):
                self.after(300, task_page._bind_task_sync)
        except Exception:
            pass
        sc_when_label = (self.var_sc_when.get() or "不取消").strip()
        self.settings["bg_skill_cancel_when"] = self._CANCEL_WHEN_UI.get(
            sc_when_label, CANCEL_WHEN_NEVER
        )
        cast_label = (self.var_sc_cast_mode.get() or "仅协议取消").strip()
        self.settings["bg_skill_cancel_cast_mode"] = self._CAST_MODE_UI.get(
            cast_label, CAST_MODE_NONE
        )
        del_label = (self.var_sc_delivery.get() or "桥接后台").strip()
        # migrate old UI label
        if del_label == "前台SendInput":
            del_label = "系统级发送"
        if del_label == "桥接后台(试验)":
            del_label = "桥接后台"
        self.settings["bg_skill_cancel_delivery"] = self._DELIVERY_UI.get(
            del_label, DELIVERY_BRIDGE
        )
        self.settings["bg_skill_cancel_protocol"] = bool(self.var_sc_proto.get())
        self.settings["bg_skill_cancel_key"] = bool(self.var_sc_key_ch.get())
        self.settings["bg_skill_cancel_memory"] = False
        self.settings["bg_skill_cancel_vk"] = parse_skill_vk(self.var_sc_key.get())
        self.settings["bg_skill_cancel_cancel_vk"] = parse_cancel_vk(
            self.var_sc_cancel_key.get()
        )
        seq_raw = (self.var_sc_sequence.get() or "").strip()
        steps = parse_key_sequence(seq_raw)
        self.settings["bg_skill_cancel_sequence"] = (
            format_key_sequence(steps) if steps else DEFAULT_LOGITECH_SEQUENCE
        )
        if steps:
            self.var_sc_sequence.set(self.settings["bg_skill_cancel_sequence"])
        self.settings["bg_skill_cancel_interval_ms"] = self._parse_int(
            self.var_sc_interval.get(), 50, lo=0, hi=10000
        )
        self.settings["bg_skill_cancel_hold_ms"] = self._parse_int(
            self.var_sc_hold.get(), 40, lo=0, hi=2000
        )
        self.settings["bg_skill_cancel_wait_ms"] = self._parse_int(
            self.var_sc_cancel_wait.get(), 40, lo=0, hi=3000
        )
        # Hang config was persisted before the remaining UI synchronisation.
        if save_errors:
            self.var_status.set("部分设置保存失败：" + "；".join(save_errors))
        else:
            self.var_status.set("已保存设置（云控 + 后摇取消 + 任务角色 + 挂机）")
        self.log(f"设置: 识别服务 URL={url} role={role}")
        try:
            role_lab = role_cn(role)
        except Exception:
            role_lab = {"master": "主控", "slave": "副控", "none": "无控"}.get(role, role)
        cloud_on = bool(self.settings.get("cloud_control_enabled"))
        cname = str(self.settings.get("cloud_control_master_name") or "").strip()
        team_on = team_control_flag_enabled(self.settings)
        parts = [f"角色={role_lab}"]
        if cloud_on:
            parts.append("云控开" + (f" · 通道「{cname}」" if cname else " · 无通道名"))
        elif team_on:
            parts.append("队内控开")
        else:
            parts.append("云控关")
        when_lab = ""
        try:
            when_lab = (self.var_sc_when.get() or "").strip()
        except Exception:
            when_lab = ""
        if when_lab:
            parts.append(f"后摇={when_lab}")
        try:
            if hang_lab:
                parts.append(hang_lab)
        except Exception:
            pass
        try:
            self.user_log(
                "操作：已保存设置 · " + " · ".join(parts),
                category=CAT_OP,
                source="快捷设置",
            )
            cl = str(cloud_label or "").strip()
            if cloud_on or (
                cl
                and cl
                not in (
                    "云控关闭",
                    "未启用",
                    "云控：未开启（本机群控仍可用）",
                )
            ):
                self.user_log(
                    cl or "云控：状态未知",
                    category=CAT_CONTROL,
                    source="快捷设置",
                    dedupe_s=0.0,
                )
        except Exception:
            pass

    @staticmethod
    def _parse_int(raw, default: int, *, lo: int = 0, hi: int = 10_000) -> int:
        """Clamp integer from UI string. @author by ak"""
        try:
            v = int(float(str(raw or "").strip() or str(default)))
        except Exception:
            v = int(default)
        return max(int(lo), min(int(hi), v))

    @staticmethod
    def _parse_float(raw, default: float, *, lo: float = 0.0, hi: float = 10_000.0) -> float:
        """Parse a plain value or Tk variable for source-only cadence fields."""
        try:
            value = raw.get() if hasattr(raw, "get") else raw
            v = float(str(value or "").strip() or str(default))
        except Exception:
            v = float(default)
        if v != v:
            v = float(default)
        return max(float(lo), min(float(hi), v))

    def _push(self, kind: str, payload=None) -> None:
        self._ui_q.put((kind, payload))

    def _drain_ui(self) -> None:
        try:
            while True:
                kind, payload = self._ui_q.get_nowait()
                if kind == "sc_status":
                    self.var_sc_status.set(str(payload or ""))
                elif kind == "sc_running":
                    self._set_skill_cancel_running(bool(payload))
                elif kind == "hang_done":
                    data = payload if isinstance(payload, dict) else {}
                    self._set_hang_busy(False)
                    self._refresh_wanzi_packet_button()
                    self.var_hang_status.set(str(data.get("line") or "挂机操作结束"))
                    op = str(data.get("op") or "")
                    ok = bool(data.get("ok"))
                    msg = str(data.get("message") or "")
                    action = "开启挂机" if op == "start" else "关闭挂机"
                    self.user_log(
                        f"操作：{action if ok else action + '失败'} · {msg}",
                        category=CAT_OP,
                        source="快捷设置",
                    )
                elif kind == "hang_saved_apply":
                    data = payload if isinstance(payload, dict) else {}
                    ok = bool(data.get("ok"))
                    msg = str(data.get("message") or "")
                    self.var_hang_status.set(
                        f"保存后挂机属性{'已应用' if ok else '应用失败'} · {msg}"
                    )
                    self.log(
                        f"挂机设置保存后应用: ok={ok} {msg or data.get('errors') or ''}"
                    )
                elif kind == "ignore_add_done":
                    try:
                        self._on_ignore_add_done(
                            payload if isinstance(payload, dict) else {}
                        )
                    except Exception:
                        pass
                elif kind == "youfeng_hook_done":
                    data = payload if isinstance(payload, dict) else {}
                    ok = bool(data.get("ok"))
                    requested = bool(data.get("requested_enabled"))
                    running = bool(data.get("enabled")) if ok else not requested
                    self._set_youfeng_hook_button(running)
                    msg = str(data.get("message") or "")
                    self.var_hang_status.set(
                        f"有凤来仪：{'已开启' if running else '已关闭'}"
                        + (" · 8 / Alt+8" if running else "")
                        + (f" · {msg}" if msg else "")
                    )
                    self.user_log(
                        f"操作：{'开启' if requested else '关闭'}有凤来仪"
                        f"{'成功' if ok else '失败'} · {msg}",
                        category=CAT_OP,
                        source="快捷设置",
                    )
                elif kind == "wanzi_packet_done":
                    data = payload if isinstance(payload, dict) else {}
                    ok = bool(data.get("ok"))
                    requested = bool(data.get("requested_enabled"))
                    running = bool(data.get("enabled")) if ok else not requested
                    self._set_wanzi_packet_button(running)
                    if not ok:
                        self._refresh_wanzi_packet_button()
                    msg = str(data.get("message") or "")
                    self.log(
                        f"手动释放丸子：请求={'开' if requested else '关'} "
                        f"ok={ok} running={running} {msg}"
                    )
                    self.var_hang_status.set(
                        f"释放丸子：{'已开启' if running else '已关闭'}"
                        + (f" · {msg}" if msg else "")
                    )
                    self.user_log(
                        f"操作：{'开启' if requested else '关闭'}释放丸子"
                        f"{'成功' if ok else '失败'} · {msg}",
                        category=CAT_OP,
                        source="快捷设置",
                    )
                elif kind == "log":
                    self.log(str(payload or ""))
        except queue.Empty:
            pass
        try:
            self._schedule_ui_drain(120)
        except Exception:
            pass



    def _publish_activity_key(self, key: str, running: bool) -> None:
        """
        Publish a synthetic activity key (Shift / 连点) for game title tag.

        @author by ak
        """
        k = (key or "").strip()
        if not k:
            return
        try:
            master = self.winfo_toplevel()
            if hasattr(master, "notify_activity"):
                master.notify_activity(k, bool(running))
        except Exception:
            pass

    def _on_sc_seq_default(self) -> None:
        """Safe park: all cancel channels off until lab proves a recovery path."""
        self.var_sc_sequence.set(DEFAULT_LOGITECH_SEQUENCE)
        self.var_sc_when.set("不取消")
        self.var_sc_cast_mode.set("仅协议取消")
        self.var_sc_proto.set(False)
        self.var_sc_key_ch.set(False)
        self.var_sc_interval.set("50")
        self.var_sc_hold.set("40")
        self.var_sc_cancel_wait.set("40")
        self.var_sc_cancel_key.set("Esc")
        self.var_sc_delivery.set("桥接后台")

    def _on_sc_seq_preset(self, seq: str) -> None:
        """Apply a sequence preset string (keep background delivery)."""
        s = str(seq or DEFAULT_LOGITECH_SEQUENCE).strip()
        if not parse_key_sequence(s):
            s = DEFAULT_LOGITECH_SEQUENCE
        self.var_sc_sequence.set(s)
        self.var_sc_cast_mode.set("序列(宏/临时)")
        # Presets do not force foreground; background is the product default.
        if (self.var_sc_delivery.get() or "").strip() not in (
            "桥接后台",
            "系统级发送",
        ):
            self.var_sc_delivery.set("桥接后台")
        steps = parse_key_sequence(s)
        if steps:
            lab = steps[0].label()
            if lab in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0"):
                self.var_sc_key.set(lab)

    def _skill_cancel_cfg_from_ui(self) -> SkillCancelLoopConfig:
        """Build two-step cancel-loop config from settings form."""
        when_label = (self.var_sc_when.get() or "不取消").strip()
        when = self._CANCEL_WHEN_UI.get(when_label, CANCEL_WHEN_NEVER)
        cast_label = (self.var_sc_cast_mode.get() or "仅协议取消").strip()
        cast_mode = self._CAST_MODE_UI.get(cast_label, CAST_MODE_NONE)
        del_label = (self.var_sc_delivery.get() or "桥接后台").strip()
        if del_label == "前台SendInput":
            del_label = "系统级发送"
        if del_label == "桥接后台(试验)":
            del_label = "桥接后台"
        delivery = self._DELIVERY_UI.get(del_label, DELIVERY_BRIDGE)
        seq = (self.var_sc_sequence.get() or DEFAULT_LOGITECH_SEQUENCE).strip()
        return normalize_skill_cancel_config(
            SkillCancelLoopConfig(
                cancel_when=when,
                use_protocol_cancel=bool(self.var_sc_proto.get()),
                use_key_cancel=bool(self.var_sc_key_ch.get()),
                use_memory_cancel=False,
                cast_mode=cast_mode,
                delivery=delivery,
                skill_vk=parse_skill_vk(self.var_sc_key.get()),
                cancel_vk=parse_cancel_vk(self.var_sc_cancel_key.get()),
                sequence=seq,
                interval_ms=self._parse_int(
                    self.var_sc_interval.get(), 10, lo=0, hi=10000
                ),
                key_hold_ms=self._parse_int(
                    self.var_sc_hold.get(), 40, lo=0, hi=2000
                ),
                cancel_wait_ms=self._parse_int(
                    self.var_sc_cancel_wait.get(), 40, lo=0, hi=3000
                ),
                require_foreground=delivery == DELIVERY_FOREGROUND,
            )
        )

    def _set_skill_cancel_running(self, running: bool) -> None:
        try:
            self.btn_sc_start.configure(
                state=tk.DISABLED if running else tk.NORMAL
            )
            self.btn_sc_stop.configure(
                state=tk.NORMAL if running else tk.DISABLED
            )
        except Exception:
            pass
        self._publish_activity_key("bg_skill_cancel", running)

    def _on_skill_cancel_start(self) -> None:
        """Start Logitech-style sequence / cancel loop."""
        sess = self._require_session()
        if sess is None:
            return
        r = self._skill_cancel_runner
        if r and r.is_running():
            return
        self._on_save()
        cfg = self._skill_cancel_cfg_from_ui()
        if cfg.cancel_when == CANCEL_WHEN_NEVER:
            self.var_sc_status.set("请先选择取消时机")
            self.log("技能取消: 时机为「不取消」")
            return
        if cfg.cast_mode == CAST_MODE_NONE and not cfg.use_protocol_cancel:
            self.var_sc_status.set("配置无效")
            self.log("技能取消: 无有效取消通道")
            return
        if cfg.cast_mode == CAST_MODE_SEQUENCE and not cfg.steps:
            self.var_sc_status.set("序列无效")
            self.log("技能取消: 序列解析失败，点「默认」或检查 键:按住:间隔")
            return

        def on_status(msg: str) -> None:
            self._push("sc_status", msg)
            self._push("log", msg)

        runner = SkillCancelLoopRunner(
            int(sess.pid),
            hwnd=int(sess.hwnd or 0),
            cfg=cfg,
            log=lambda m: self._push("log", m),
            on_status=on_status,
        )
        self._skill_cancel_runner = runner
        self._set_skill_cancel_running(True)
        tip = "启动中…"
        if cfg.cast_mode == CAST_MODE_NONE and cfg.use_protocol_cancel:
            tip = "协议后摇取消运行中（无按键）"
        elif cfg.delivery == DELIVERY_BRIDGE:
            tip = "桥接后台运行中（不抢焦点）"
        elif cfg.delivery == DELIVERY_FOREGROUND:
            tip = "2s 后开始 — 请切到游戏"
        self.var_sc_status.set(tip)
        if not runner.start():
            self._set_skill_cancel_running(False)
            self.var_sc_status.set("启动失败")
            try:
                self.user_log("操作：后摇取消启动失败", category=CAT_OP, source="快捷设置")
            except Exception:
                pass
            return
        try:
            when_lab = (self.var_sc_when.get() or "").strip() or "取消"
            self.user_log(
                f"操作：后摇取消已开始 · {when_lab}",
                category=CAT_OP,
                source="快捷设置",
            )
        except Exception:
            pass
        self.after(200, self._watch_skill_cancel)

    def _watch_skill_cancel(self) -> None:
        r = self._skill_cancel_runner
        if r is None:
            self._set_skill_cancel_running(False)
            return
        if r.is_running():
            self.after(200, self._watch_skill_cancel)
            return
        self._set_skill_cancel_running(False)

    def _on_skill_cancel_stop(self) -> None:
        r = self._skill_cancel_runner
        if r is not None:
            try:
                stopped = bool(r.stop())
                if stopped or not r.is_running():
                    self._skill_cancel_runner = None
            except Exception:
                pass
        self._set_skill_cancel_running(False)
        self.var_sc_status.set("已停止")
        try:
            self.user_log("操作：后摇取消已停止", category=CAT_OP, source="快捷设置")
        except Exception:
            pass

    def shutdown(self) -> None:
        """
        Stop settings-page runners on app quit.

        @author by ak
        """
        try:
            self._on_skill_cancel_stop()
        except Exception:
            pass
        try:
            sess = self.selected_session()
            if sess is not None:
                stop_youfeng_key_hook(
                    int(getattr(sess, "pid", 0) or 0),
                    owner=YOUFENG_HOOK_OWNER_MANUAL,
                    log=lambda _m: None,
                )
        except Exception:
            pass
class ActivityPage(FeaturePage):
    """
    自动副本 — 进本循环；类型可选「活跃 / 副本 / 切糕」。

    活跃：固定绿竹幻想乡，慢节奏刷活跃点 + 协议领取活跃宝箱（可群控同步领箱）。
    副本：自选副本 + 可选上限次数，仅进本/清本/回城，不领宝箱、不同步群控。
    切糕：140副本任务一（组队本面板·超级分类/嵩山之巅）；寻路挂机点，等到超时回城。
    切糕可勾「小号」：队员不进本、城内一直等主号带入，进本后直接挂机；主/小号两套坐标缓存。
    启动后冻结类型/副本等参数，避免运行中误改。

    @author by ak
    """

    title = "自动副本"
    key = "activity"

    # UI city display -> ActivityConfig.city_gate
    _CITY_MAP = {
        "任意城镇": "any_city",
        "福州": "fuzhou",
        "洛阳": "luoyang",
    }
    _CITY_REV = {v: k for k, v in _CITY_MAP.items()}

    _PHASE_STEP = {
        "gate": 0,
        "path_open": 1,
        "enter_wait": 2,
        "enter_ok": 2,
        "enter_fail": 2,
        "wait_return": 3,
        "entry_cd": 4,
        "claim": 5,
        "done": 5,
    }

    _MODE_ACTIVITY = "活跃"
    _MODE_DUNGEON = "副本"
    _MODE_QIEGAO = "切糕"

    _STEP_LABELS_ACTIVITY = (
        "确认在城镇（福州/洛阳）",
        "进入副本",
        "副本内清怪",
        "等待回城",
        "冷却后继续",
        "回城后领活跃宝箱",
    )
    _STEP_LABELS_DUNGEON = (
        "确认在城镇（福州/洛阳）",
        "进入副本",
        "副本内清怪",
        "等待回城",
        "冷却后继续",
    )
    _STEP_LABELS_QIEGAO = (
        "确认在城镇（福州/洛阳）",
        f"进入副本（{QIEGAO_CATEGORY_ALIAS}）",
        "等待进入 / 主号过桥触发（空气墙消失）",
        "走到挂机点并开内挂",
        "挂机至超时回城",
        "冷却后继续",
    )
    _STEP_LABELS_QIEGAO_ALT = (
        "检查是否已在副本",
        "不在本内则等主号带入",
        "进入后走到挂机点",
        "开内挂并站桩",
        "挂机至超时回城",
        "冷却后继续",
    )

    def _build(self) -> None:
        self._maybe_bind_target(self)

        # 底部常驻：状态 + 开始/停止（先 pack 底部，中间再滚动）
        foot = ttk.Frame(self, style="Panel.TFrame")
        foot.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        self.var_status = tk.StringVar(value="待命")
        ttk.Label(
            foot,
            textvariable=self.var_status,
            style="Panel.Mono.TLabel",
            wraplength=420,
            justify=tk.LEFT,
        ).pack(anchor="w", fill=tk.X)
        bar = ttk.Frame(foot, style="Panel.TFrame")
        bar.pack(fill=tk.X, pady=(6, 0))
        self.btn_start = ttk.Button(
            bar, text="开始", style="Accent.TButton", width=8, command=self._on_start
        )
        self.btn_start.pack(side=tk.LEFT)
        self.btn_stop = ttk.Button(
            bar, text="停止", width=8, command=self._on_stop, state=tk.DISABLED
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(6, 0))

        # 中间可滚动区域
        outer, body = make_scrollable_body(self, style="Panel.TFrame")
        outer.pack(fill=tk.BOTH, expand=True)

        box_cfg = section(body, "参数")
        box_cfg.pack(fill=tk.X, pady=(0, 6))
        self._box_cfg = box_cfg
        self.row1 = ttk.Frame(box_cfg, style="Panel.TFrame")
        self.row1.pack(fill=tk.X, pady=1)
        ttk.Label(self.row1, text="类型", style="Panel.Muted.TLabel", width=6).pack(
            side=tk.LEFT
        )
        self.var_mode = tk.StringVar(value=self._MODE_ACTIVITY)
        self.cmb_mode = ttk.Combobox(
            self.row1,
            textvariable=self.var_mode,
            values=(self._MODE_ACTIVITY, self._MODE_DUNGEON, self._MODE_QIEGAO),
            width=6,
            state="readonly",
        )
        self.cmb_mode.pack(side=tk.LEFT, padx=(4, 8))
        self.cmb_mode.bind("<<ComboboxSelected>>", lambda _e: self._on_mode_change())
        ttk.Label(self.row1, text="城镇", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        self.var_city = tk.StringVar(value="任意城镇")
        self.cmb_city = ttk.Combobox(
            self.row1,
            textvariable=self.var_city,
            values=("任意城镇", "福州", "洛阳"),
            width=10,
            state="readonly",
        )
        self.cmb_city.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(self.row1, text="轮次冷却", style="Panel.Muted.TLabel").pack(
            side=tk.LEFT, padx=(10, 0)
        )
        try:
            entry_cd_raw = self.settings.get("activity_entry_cd_s", 30.0)
            if entry_cd_raw is None:
                entry_cd_raw = 30.0
            entry_cd_saved = max(0.0, float(entry_cd_raw))
        except Exception:
            entry_cd_saved = 30.0
        self.var_entry_cd = tk.StringVar(value=f"{entry_cd_saved:g}")
        self.ent_entry_cd = ttk.Entry(
            self.row1, textvariable=self.var_entry_cd, width=5
        )
        self.ent_entry_cd.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(self.row1, text="秒", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        self.ent_entry_cd.bind("<FocusOut>", lambda _e: self._persist_entry_cd_ui())
        self.ent_entry_cd.bind("<Return>", lambda _e: self._persist_entry_cd_ui())

        # 副本类型：选本 + 上限次数（-1/空=不限）
        self.row_dungeon = ttk.Frame(box_cfg, style="Panel.TFrame")
        # Instance.Cfg list: 常用本 first, default 绿竹幻想乡 (169).
        self._inst_choices = list_instance_choices(common_first=True, include_all=True)
        self._inst_labels = [lab for _iid, lab in self._inst_choices]
        self._inst_by_label = {lab: iid for iid, lab in self._inst_choices}
        self.var_inst = tk.StringVar(value=default_instance_label())
        ttk.Label(self.row_dungeon, text="副本", style="Panel.Muted.TLabel", width=6).pack(
            side=tk.LEFT
        )
        self.cmb_inst = ttk.Combobox(
            self.row_dungeon,
            textvariable=self.var_inst,
            values=self._inst_labels,
            width=22,
            state="readonly",
        )
        self.cmb_inst.pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(self.row_dungeon, text="上限", style="Panel.Muted.TLabel").pack(
            side=tk.LEFT
        )
        self.var_max_runs = tk.StringVar(value="")
        self.ent_max_runs = ttk.Entry(
            self.row_dungeon, textvariable=self.var_max_runs, width=6
        )
        self.ent_max_runs.pack(side=tk.LEFT, padx=(4, 4))
        ttk.Label(
            self.row_dungeon,
            text="次（空=不限）",
            style="Panel.Muted.TLabel",
        ).pack(side=tk.LEFT)

        # 切糕：固定 140材料本 + 小号勾选 + 主/小号双坐标 + 上限
        self.row_qiegao = ttk.Frame(box_cfg, style="Panel.TFrame")
        ttk.Label(self.row_qiegao, text="副本", style="Panel.Muted.TLabel", width=6).pack(
            side=tk.LEFT
        )
        self.var_qiegao_inst = tk.StringVar(
            value=f"{QIEGAO_CATEGORY_ALIAS} · {QIEGAO_INSTANCE_NAME} ({QIEGAO_INSTANCE_ID})"
        )
        ttk.Label(
            self.row_qiegao,
            textvariable=self.var_qiegao_inst,
            style="Panel.Mono.TLabel",
        ).pack(side=tk.LEFT, padx=(4, 8))
        self.var_qiegao_is_alt = tk.BooleanVar(value=False)
        self.chk_qiegao_alt = ttk.Checkbutton(
            self.row_qiegao,
            text="小号",
            variable=self.var_qiegao_is_alt,
            style="Panel.TCheckbutton",
            command=self._on_qiegao_alt_toggle,
        )
        self.chk_qiegao_alt.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(self.row_qiegao, text="上限", style="Panel.Muted.TLabel").pack(
            side=tk.LEFT
        )
        self.var_qiegao_max = tk.StringVar(
            value=str(self.settings.get("qiegao_max_runs") or "")
        )
        self.ent_qiegao_max = ttk.Entry(
            self.row_qiegao, textvariable=self.var_qiegao_max, width=5
        )
        self.ent_qiegao_max.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(
            self.row_qiegao, text="次（空=不限）", style="Panel.Muted.TLabel"
        ).pack(side=tk.LEFT)

        self.row_qiegao2 = ttk.Frame(box_cfg, style="Panel.TFrame")
        self.var_afk_role_lab = tk.StringVar(value="主号坐标")
        ttk.Label(
            self.row_qiegao2,
            textvariable=self.var_afk_role_lab,
            style="Panel.Muted.TLabel",
            width=6,
        ).pack(side=tk.LEFT)
        # init dual AFK slots (disk + session settings + legacy keys)
        self._init_qiegao_afk_slots()
        ax_f, ay_f, az_f = self._active_qiegao_afk_xyz()
        self.var_afk_x = tk.StringVar(value=f"{ax_f:.1f}")
        self.var_afk_y = tk.StringVar(value=f"{ay_f:.1f}")
        self.var_afk_z = tk.StringVar(value=f"{az_f:.1f}")
        self.ent_afk_x = ttk.Entry(self.row_qiegao2, textvariable=self.var_afk_x, width=7)
        self.ent_afk_x.pack(side=tk.LEFT, padx=(4, 2))
        self.ent_afk_y = ttk.Entry(self.row_qiegao2, textvariable=self.var_afk_y, width=7)
        self.ent_afk_y.pack(side=tk.LEFT, padx=(2, 2))
        self.ent_afk_z = ttk.Entry(self.row_qiegao2, textvariable=self.var_afk_z, width=7)
        self.ent_afk_z.pack(side=tk.LEFT, padx=(2, 6))
        self.btn_afk_pick = ttk.Button(
            self.row_qiegao2,
            text="取坐标",
            width=7,
            command=self._on_pick_afk_point,
        )
        self.btn_afk_pick.pack(side=tk.LEFT)
        # 开发环境：进本后低频「分析怪频」（默认关）
        self.var_analyze_mob_freq = tk.BooleanVar(value=False)
        self.chk_analyze_mob_freq = None
        try:
            show_dev = bool(is_dev_build())
        except Exception:
            show_dev = False
        if show_dev:
            try:
                saved = bool(self.settings.get("qiegao_analyze_mob_freq", False))
            except Exception:
                saved = False
            self.var_analyze_mob_freq.set(bool(saved))
            self.chk_analyze_mob_freq = ttk.Checkbutton(
                self.row_qiegao2,
                text="分析怪频",
                variable=self.var_analyze_mob_freq,
                style="Panel.TCheckbutton",
                command=self._on_analyze_mob_freq_toggle,
            )
            self.chk_analyze_mob_freq.pack(side=tk.LEFT, padx=(10, 0))
        # stage wait kept internal default 0 (no UI); compatibility field
        self.var_stage_wait = tk.StringVar(value="0")
        try:
            self._sync_afk_role_label()
        except Exception:
            pass

        # 活跃点数整栏（仅「活跃」类型显示）：目标点 + 领宝箱
        self.row_points = ttk.Frame(box_cfg, style="Panel.TFrame")
        self.row_points.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(self.row_points, text="目标点", style="Panel.Muted.TLabel", width=6).pack(
            side=tk.LEFT
        )
        # Live current / full-award target (e.g. 20/70); per-run is built-in fallback only.
        self._points_cur = 0
        self._points_target = int(FLOURISH_AWARD_REPU[-1])
        self.var_points = tk.StringVar(
            value=f"{self._points_cur}/{self._points_target}"
        )
        ttk.Label(
            self.row_points,
            textvariable=self.var_points,
            style="Panel.Mono.TLabel",
            width=10,
        ).pack(side=tk.LEFT, padx=(4, 10))
        self.var_claim = tk.BooleanVar(value=True)
        self.chk_claim = ttk.Checkbutton(
            self.row_points,
            text="领宝箱",
            variable=self.var_claim,
            style="Panel.TCheckbutton",
        )
        self.chk_claim.pack(side=tk.LEFT)

        box_flow = section(body, "流程")
        box_flow.pack(fill=tk.BOTH, expand=True, pady=(0, 0))
        self._box_flow = box_flow
        self._step_vars: list[tk.StringVar] = []
        self._step_rows: list[ttk.Frame] = []
        self._step_text_labels: list[ttk.Label] = []
        self._step_labels = list(self._STEP_LABELS_ACTIVITY)
        for i, step_text in enumerate(self._step_labels, start=1):
            row = ttk.Frame(box_flow, style="Panel.TFrame")
            row.pack(fill=tk.X, pady=2)
            mark = tk.StringVar(value="○")
            self._step_vars.append(mark)
            self._step_rows.append(row)
            ttk.Label(row, textvariable=mark, style="Panel.Mono.TLabel", width=2).pack(
                side=tk.LEFT
            )
            lab = ttk.Label(row, text=f"{i}. {step_text}", style="Panel.TLabel")
            lab.pack(side=tk.LEFT)
            self._step_text_labels.append(lab)

        # 状态/按钮已固定在底部 foot，中间仅参数+流程可滚动

        self._runner: ActivityRunner | None = None
        self._ui_q: queue.Queue = queue.Queue()
        self._snapshot_busy = False
        self._schedule_ui_drain(120)
        self.refresh_sessions()
        self._apply_mode_ui(reset_steps=False)

    def _mode_ui(self) -> str:
        """Current UI mode key: activity|dungeon|qiegao. @author by ak"""
        try:
            lab = (self.var_mode.get() or "").strip()
        except Exception:
            lab = self._MODE_ACTIVITY
        if lab == self._MODE_DUNGEON:
            return "dungeon"
        if lab == self._MODE_QIEGAO:
            return "qiegao"
        return "activity"

    def _is_dungeon_mode_ui(self) -> bool:
        """True when type dropdown is 副本. @author by ak"""
        return self._mode_ui() == "dungeon"

    def _is_qiegao_mode_ui(self) -> bool:
        """True when type dropdown is 切糕. @author by ak"""
        return self._mode_ui() == "qiegao"

    def _on_mode_change(self) -> None:
        """React to 类型 combobox: toggle points/dungeon/qiegao rows. @author by ak"""
        if self._runner and self._runner.is_running():
            try:
                last = getattr(self, "_mode_applied", "activity")
                if last == "dungeon":
                    self.var_mode.set(self._MODE_DUNGEON)
                elif last == "qiegao":
                    self.var_mode.set(self._MODE_QIEGAO)
                else:
                    self.var_mode.set(self._MODE_ACTIVITY)
            except Exception:
                pass
            self.var_status.set("运行中不可切换类型")
            return
        self._apply_mode_ui(reset_steps=True)

    def _apply_mode_ui(self, *, reset_steps: bool = True) -> None:
        """
        活跃 / 副本 / 切糕 切换参数行与流程步骤。

        @author by ak
        """
        mode = self._mode_ui()
        self._mode_applied = mode

        self._mode_applied_dungeon = mode == "dungeon"
        # Always repack param sub-rows in fixed order under box_cfg:
        # row1(类型/城镇) → dungeon | qiegao/coords | points
        try:
            self.row_dungeon.pack_forget()
        except Exception:
            pass
        try:
            self.row_qiegao.pack_forget()
            self.row_qiegao2.pack_forget()
        except Exception:
            pass
        try:
            self.row_points.pack_forget()
        except Exception:
            pass
        try:
            if mode == "dungeon":
                self.row_dungeon.pack(fill=tk.X, pady=(4, 0), after=self.row1)
            elif mode == "qiegao":
                self.row_qiegao.pack(fill=tk.X, pady=(4, 0), after=self.row1)
                self.row_qiegao2.pack(fill=tk.X, pady=(4, 0), after=self.row_qiegao)
            else:
                self.row_points.pack(fill=tk.X, pady=(4, 0), after=self.row1)
        except Exception:
            # fallback without after=
            try:
                if mode == "dungeon":
                    self.row_dungeon.pack(fill=tk.X, pady=(4, 0))
                elif mode == "qiegao":
                    self.row_qiegao.pack(fill=tk.X, pady=(4, 0))
                    self.row_qiegao2.pack(fill=tk.X, pady=(4, 0))
                else:
                    self.row_points.pack(fill=tk.X, pady=(4, 0))
            except Exception:
                pass
        if mode == "dungeon":
            labels = list(self._STEP_LABELS_DUNGEON)
        elif mode == "qiegao":
            is_alt = False
            try:
                is_alt = bool(self.var_qiegao_is_alt.get())
            except Exception:
                is_alt = False
            labels = list(
                self._STEP_LABELS_QIEGAO_ALT if is_alt else self._STEP_LABELS_QIEGAO
            )
        else:
            labels = list(self._STEP_LABELS_ACTIVITY)
        self._step_labels = labels
        # Ensure enough rows for longest flow (切糕含说明行共 7).
        _max_steps = max(
            len(self._STEP_LABELS_ACTIVITY),
            len(self._STEP_LABELS_DUNGEON),
            len(self._STEP_LABELS_QIEGAO),
            len(self._STEP_LABELS_QIEGAO_ALT),
            len(labels),
        )
        while len(self._step_rows) < _max_steps:
            row = ttk.Frame(self._box_flow, style="Panel.TFrame")
            mark = tk.StringVar(value="○")
            self._step_vars.append(mark)
            self._step_rows.append(row)
            ttk.Label(row, textvariable=mark, style="Panel.Mono.TLabel", width=2).pack(
                side=tk.LEFT
            )
            lab = ttk.Label(row, text="", style="Panel.TLabel")
            lab.pack(side=tk.LEFT)
            self._step_text_labels.append(lab)
        for i, row in enumerate(self._step_rows):
            if i < len(labels):
                try:
                    self._step_text_labels[i].configure(
                        text=f"{i + 1}. {labels[i]}"
                    )
                except Exception:
                    pass
                try:
                    row.pack(fill=tk.X, pady=2)
                except Exception:
                    pass
            else:
                try:
                    row.pack_forget()
                except Exception:
                    pass
        if reset_steps:
            self._reset_steps()
            try:
                mode_lab = {
                    "dungeon": self._MODE_DUNGEON,
                    "qiegao": self._MODE_QIEGAO,
                }.get(mode, self._MODE_ACTIVITY)
                self.var_status.set(f"待命 · {mode_lab}")
            except Exception:
                pass

    def on_page_show(self) -> None:
        """
        切回本页时恢复开始/停止按钮与运行态（runner 可能仍在后台跑）。

        @author by ak
        """
        self._sync_runner_ui()
        if not (self._runner and self._runner.is_running()):
            # The page can be built before __ROLE_BOUND__. Rehydrate from this
            # injection's qiegao.json once the fixed role identity is available.
            if self._current_role_id():
                self._init_qiegao_afk_slots()
                xyz = self._active_qiegao_afk_xyz()
                self._write_afk_xyz_to_ui(xyz)
                try:
                    self.var_analyze_mob_freq.set(
                        bool(self.settings.get("qiegao_analyze_mob_freq", False))
                    )
                except Exception:
                    pass
                self._sync_afk_role_label()
            self._refresh_live_snapshot()

    def _sync_runner_ui(self) -> None:
        """Re-bind UI start/stop freeze to live runner. @author by ak"""
        running = False
        try:
            running = bool(self._runner is not None and self._runner.is_running())
        except Exception:
            running = False
        # reclaim from host if local ref lost but host still tracks activity
        if not running:
            try:
                top = self.winfo_toplevel()
                other = getattr(top, "_feature_runners", None) or {}
                r = other.get("activity")
                if r is not None and getattr(r, "is_running", lambda: False)():
                    self._runner = r
                    running = True
            except Exception:
                pass
        self._set_running(running)
        if running:
            try:
                cur = (self.var_status.get() or "").strip()
            except Exception:
                cur = ""
            if (not cur) or cur.startswith("待命") or ("切回" in cur):
                try:
                    self.var_status.set("运行中（已从其它页切回，可点停止）")
                except Exception:
                    pass

    def refresh_sessions(self) -> None:
        if hasattr(self, "_refresh_combo"):
            self._refresh_combo()

    def _refresh_live_snapshot(self) -> None:
        """
        Background read of live flourish + scene for idle UI.

        @author by ak
        """
        if self._snapshot_busy:
            return
        if self._runner and self._runner.is_running():
            return
        sess = self.selected_session()
        if sess is None:
            return
        self._snapshot_busy = True
        pid = int(sess.pid)

        def worker() -> None:
            pts = None
            lab = None
            try:
                from app.core.activity_auto import (
                    read_flourish_points,
                    read_scene_state,
                )
                from app.core.super_loot import open_attach_session

                session = open_attach_session(pid, log=lambda _m: None)
                try:
                    pts = read_flourish_points(session, log=lambda _m: None)
                    _sid, _pos, lab = read_scene_state(session, log=lambda _m: None)
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass
            except Exception:
                pts = None
                lab = None
            self._push(
                "snapshot",
                {
                    "points": pts,
                    "target": int(FLOURISH_AWARD_REPU[-1]),
                    "scene_label": lab,
                },
            )
            self._push("snapshot_done", None)

        threading.Thread(target=worker, daemon=True).start()

    def _reset_steps(self) -> None:
        for v in self._step_vars:
            v.set("○")

    def _mark_step(self, idx: int, mark: str = "…") -> None:
        if 0 <= idx < len(self._step_vars):
            for i, v in enumerate(self._step_vars):
                if i < idx:
                    if v.get() == "…":
                        v.set("✓")
                elif i == idx:
                    v.set(mark)

    def _selected_instance_id(self) -> int:
        """Resolve Combobox label -> instance id (default 绿竹 169). @author by ak"""
        label = ""
        try:
            label = (self.var_inst.get() or "").strip()
        except Exception:
            label = ""
        by = getattr(self, "_inst_by_label", None) or {}
        if label in by:
            return max(1, int(by[label]))
        parsed = parse_instance_label(label)
        if parsed is not None:
            return max(1, int(parsed))
        return int(DEFAULT_INSTANCE_ID)

    def _parse_max_runs_text(self, raw: str) -> int:
        """Parse 上限次数: empty / -1 / 非正数 => 0 (不限). @author by ak"""
        s = (raw or "").strip()
        if not s or s in ("-1", "不限", "无限", "none", "None"):
            return 0
        try:
            n = int(s)
        except Exception:
            return 0
        if n <= 0:
            return 0
        return int(n)

    def _parse_max_runs_ui(self) -> int:
        """Parse 副本上限次数. @author by ak"""
        raw = ""
        try:
            raw = (self.var_max_runs.get() or "").strip()
        except Exception:
            raw = ""
        return self._parse_max_runs_text(raw)

    def _parse_entry_cd_ui(self) -> float:
        """Parse the shared activity/dungeon post-run cooldown. @author by ak"""
        try:
            raw = (self.var_entry_cd.get() or "").strip()
            value = float(raw) if raw else 30.0
        except Exception:
            value = 30.0
        return max(0.0, min(3600.0, float(value)))

    def _persist_entry_cd_ui(self) -> float:
        """Normalize and cache the editable post-run cooldown. @author by ak"""
        value = self._parse_entry_cd_ui()
        try:
            self.var_entry_cd.set(f"{value:g}")
            self.settings["activity_entry_cd_s"] = float(value)
        except Exception:
            pass
        return value

    def _init_qiegao_afk_slots(self) -> None:
        """
        Seed main/alt AFK slots from disk prefs + session settings + legacy keys.

        System keeps two slots; UI fills by 是否小号.

        @author by ak
        """
        prefs = load_qiegao_prefs(self._current_role_id())
        dx, dy, dz = QIEGAO_DEFAULT_AFK

        def _slot_from(src_keys_x, src_keys_y, src_keys_z, fallback):
            for kx, ky, kz in zip(src_keys_x, src_keys_y, src_keys_z):
                try:
                    if (
                        self.settings.get(kx) is not None
                        and self.settings.get(ky) is not None
                        and self.settings.get(kz) is not None
                    ):
                        return (
                            float(self.settings.get(kx)),
                            float(self.settings.get(ky)),
                            float(self.settings.get(kz)),
                        )
                except Exception:
                    pass
            return fallback

        main_fb = qiegao_afk_for_role(False, prefs)
        alt_fb = qiegao_afk_for_role(True, prefs)
        # legacy single triple -> main when main slot empty on disk default only
        legacy = None
        try:
            if (
                self.settings.get("qiegao_afk_x") is not None
                and self.settings.get("qiegao_afk_y") is not None
                and self.settings.get("qiegao_afk_z") is not None
            ):
                legacy = (
                    float(self.settings["qiegao_afk_x"]),
                    float(self.settings["qiegao_afk_y"]),
                    float(self.settings["qiegao_afk_z"]),
                )
        except Exception:
            legacy = None
        if legacy is not None and main_fb == (float(dx), float(dy), float(dz)):
            main_fb = legacy

        # A bound role's qiegao.json is authoritative. Session values are only
        # a pre-bind compatibility fallback, never an override of role storage.
        role_id = self._current_role_id()
        if role_id:
            main_xyz, alt_xyz = main_fb, alt_fb
        else:
            main_xyz = _slot_from(
                ("qiegao_afk_main_x",),
                ("qiegao_afk_main_y",),
                ("qiegao_afk_main_z",),
                main_fb,
            )
            alt_xyz = _slot_from(
                ("qiegao_afk_alt_x",),
                ("qiegao_afk_alt_y",),
                ("qiegao_afk_alt_z",),
                alt_fb,
            )

        # 默认永不勾选「小号」，避免上次残留导致主号误进队员逻辑。
        # 主/小号两套坐标仍缓存；是否小号仅当次会话手动勾选。
        is_alt = False

        self.settings["qiegao_afk_main_x"], self.settings["qiegao_afk_main_y"], self.settings["qiegao_afk_main_z"] = main_xyz
        self.settings["qiegao_afk_alt_x"], self.settings["qiegao_afk_alt_y"], self.settings["qiegao_afk_alt_z"] = alt_xyz
        self.settings["qiegao_is_alt"] = False
        self.settings["qiegao_analyze_mob_freq"] = bool(
            prefs.get("analyze_mob_freq", False)
        )
        try:
            self.var_qiegao_is_alt.set(False)
        except Exception:
            pass
        # effective current = 主号坐标
        cur = main_xyz
        self.settings["qiegao_afk_x"], self.settings["qiegao_afk_y"], self.settings["qiegao_afk_z"] = cur
        # Initialization only hydrates window state. It must not rewrite the
        # current role's qiegao.json or change its saved preference.

    def _qiegao_is_alt_ui(self) -> bool:
        try:
            return bool(self.var_qiegao_is_alt.get())
        except Exception:
            return bool(self.settings.get("qiegao_is_alt", False))

    def _active_qiegao_afk_xyz(self) -> tuple[float, float, float]:
        is_alt = self._qiegao_is_alt_ui()
        prefix = "qiegao_afk_alt_" if is_alt else "qiegao_afk_main_"
        try:
            return (
                float(self.settings.get(prefix + "x")),
                float(self.settings.get(prefix + "y")),
                float(self.settings.get(prefix + "z")),
            )
        except Exception:
            return qiegao_afk_for_role(
                is_alt, load_qiegao_prefs(self._current_role_id())
            )

    def _sync_afk_role_label(self) -> None:
        try:
            self.var_afk_role_lab.set("小号坐标" if self._qiegao_is_alt_ui() else "主号坐标")
        except Exception:
            pass

    def _read_afk_xyz_from_ui(self) -> tuple[float, float, float] | None:
        try:
            xs = (self.var_afk_x.get() or "").strip()
            ys = (self.var_afk_y.get() or "").strip()
            zs = (self.var_afk_z.get() or "").strip()
            if not (xs and ys and zs):
                return None
            return float(xs), float(ys), float(zs)
        except Exception:
            return None

    def _write_afk_xyz_to_ui(self, xyz: tuple[float, float, float]) -> None:
        x, y, z = xyz
        try:
            self.var_afk_x.set(f"{float(x):.1f}")
            self.var_afk_y.set(f"{float(y):.1f}")
            self.var_afk_z.set(f"{float(z):.1f}")
        except Exception:
            pass

    def _persist_qiegao_afk_slot(
        self,
        xyz: tuple[float, float, float],
        *,
        is_alt: bool | None = None,
    ) -> None:
        """Write AFK xyz into active (or given) role slot + disk. @author by ak"""
        alt = self._qiegao_is_alt_ui() if is_alt is None else bool(is_alt)
        x, y, z = float(xyz[0]), float(xyz[1]), float(xyz[2])
        prefix = "qiegao_afk_alt_" if alt else "qiegao_afk_main_"
        self.settings[prefix + "x"] = x
        self.settings[prefix + "y"] = y
        self.settings[prefix + "z"] = z
        # effective current mirrors active slot
        if alt == self._qiegao_is_alt_ui():
            self.settings["qiegao_afk_x"] = x
            self.settings["qiegao_afk_y"] = y
            self.settings["qiegao_afk_z"] = z
        self.settings["qiegao_is_alt"] = bool(self._qiegao_is_alt_ui())
        try:
            save_qiegao_prefs(
                is_alt=bool(self._qiegao_is_alt_ui()),
                slot=("alt" if alt else "main"),
                xyz=(x, y, z),
                role_id=self._current_role_id(),
            )
        except Exception:
            pass

    def _on_analyze_mob_freq_toggle(self) -> None:
        """Dev-only: persist 分析怪频 checkbox. @author by ak"""
        try:
            on = bool(self.var_analyze_mob_freq.get())
        except Exception:
            on = False
        try:
            self.settings["qiegao_analyze_mob_freq"] = bool(on)
            save_qiegao_prefs(
                analyze_mob_freq=bool(on), role_id=self._current_role_id()
            )
        except Exception as e:
            self.log(f"自动副本: 保存分析怪频失败: {e}")
        if on:
            self.var_status.set("分析怪频：开（进本挂机后低频扫 大漠射手/普怪）")
        else:
            self.var_status.set("分析怪频：关")
    def _on_qiegao_alt_toggle(self) -> None:
        """Switch main/alt AFK slots; keep both cached. @author by ak"""
        if self._runner and self._runner.is_running():
            # revert toggle while running
            try:
                prev = bool(self.settings.get("qiegao_is_alt", False))
                self.var_qiegao_is_alt.set(prev)
            except Exception:
                pass
            self.var_status.set("运行中不可切换小号")
            return
        # save current UI coords into previous role slot
        prev_alt = bool(self.settings.get("qiegao_is_alt", False))
        cur_xyz = self._read_afk_xyz_from_ui()
        if cur_xyz is not None:
            self._persist_qiegao_afk_slot(cur_xyz, is_alt=prev_alt)
        new_alt = self._qiegao_is_alt_ui()
        self.settings["qiegao_is_alt"] = bool(new_alt)
        # load target slot into UI
        prefix = "qiegao_afk_alt_" if new_alt else "qiegao_afk_main_"
        try:
            xyz = (
                float(self.settings.get(prefix + "x")),
                float(self.settings.get(prefix + "y")),
                float(self.settings.get(prefix + "z")),
            )
        except Exception:
            xyz = qiegao_afk_for_role(
                new_alt, load_qiegao_prefs(self._current_role_id())
            )
        self._write_afk_xyz_to_ui(xyz)
        self.settings["qiegao_afk_x"], self.settings["qiegao_afk_y"], self.settings["qiegao_afk_z"] = xyz
        try:
            save_qiegao_prefs(is_alt=bool(new_alt), role_id=self._current_role_id())
        except Exception:
            pass
        self._sync_afk_role_label()
        # refresh step labels when already on qiegao mode
        if self._is_qiegao_mode_ui():
            self._apply_mode_ui(reset_steps=True)
        role = "小号" if new_alt else "主号"
        self.var_status.set(f"切糕角色：{role} · 坐标 ({xyz[0]:.1f},{xyz[1]:.1f},{xyz[2]:.1f})")

    def _parse_qiegao_max_runs_ui(self) -> int:
        raw = ""
        try:
            raw = (self.var_qiegao_max.get() or "").strip()
        except Exception:
            raw = ""
        return self._parse_max_runs_text(raw)

    def _parse_stage_wait_ui(self) -> float:
        raw = ""
        try:
            raw = (self.var_stage_wait.get() or "").strip()
        except Exception:
            raw = ""
        if not raw:
            return 120.0
        try:
            v = float(raw)
        except Exception:
            return 120.0
        return max(0.0, v)

    def _afk_coords_from_settings(self):
        """Prefer editable UI fields, then active role slot, then default. @author by ak"""
        xyz = self._read_afk_xyz_from_ui()
        if xyz is not None:
            self._persist_qiegao_afk_slot(xyz)
            return xyz
        try:
            return self._active_qiegao_afk_xyz()
        except Exception:
            return qiegao_afk_for_role(self._qiegao_is_alt_ui())

    def _cfg_from_ui(self) -> ActivityConfig:
        """Build runner config from form. @author by ak"""
        city_label = (self.var_city.get() or "任意城镇").strip()
        city_gate = self._CITY_MAP.get(city_label, "any_city")
        target = int(FLOURISH_AWARD_REPU[-1])
        mode_ui = self._mode_ui()
        entry_cd = self._persist_entry_cd_ui()
        ax = ay = az = None
        stage_wait = 120.0
        if mode_ui == "dungeon":
            mode = "dungeon"
            inst = self._selected_instance_id()
            claim = False
            max_runs = self._parse_max_runs_ui()
            claim_after = False
        elif mode_ui == "qiegao":
            mode = "qiegao"
            inst = int(QIEGAO_INSTANCE_ID)
            claim = False
            max_runs = self._parse_qiegao_max_runs_ui()
            claim_after = False
            # 进本后立刻寻路挂机点（不再等阶段）
            stage_wait = 0.0
            ax, ay, az = self._afk_coords_from_settings()
            try:
                self.settings["qiegao_stage_wait_s"] = 0.0
                self.settings["qiegao_max_runs"] = (
                    "" if max_runs <= 0 else str(int(max_runs))
                )
                self.settings["qiegao_is_alt"] = bool(self._qiegao_is_alt_ui())
            except Exception:
                pass
        else:
            mode = "activity"
            inst = int(DEFAULT_INSTANCE_ID)
            claim = bool(self.var_claim.get())
            max_runs = 12
            claim_after = True
        is_alt = bool(self._qiegao_is_alt_ui()) if mode_ui == "qiegao" else False
        analyze_mob = False
        if mode_ui == "qiegao":
            try:
                analyze_mob = bool(is_dev_build()) and bool(
                    self.var_analyze_mob_freq.get()
                )
            except Exception:
                analyze_mob = False
            try:
                self.settings["qiegao_analyze_mob_freq"] = bool(analyze_mob)
            except Exception:
                pass
        return ActivityConfig(
            mode=mode,
            city_gate=city_gate,
            instance_id=max(1, int(inst)),
            target_points=max(1, target),
            points_per_run=10,
            max_runs=max_runs,
            claim_awards=claim,
            claim_after_each_run=claim_after,
            entry_cd_min_s=float(entry_cd),
            entry_cd_max_s=float(entry_cd),
            return_poll_s=15.0,
            qiegao_stage_wait_s=float(stage_wait),
            qiegao_afk_x=ax,
            qiegao_afk_y=ay,
            qiegao_afk_z=az,
            qiegao_is_alt=is_alt,
            qiegao_analyze_mob_freq=bool(analyze_mob),
            qiegao_afk_repath_s=90.0,
            qiegao_press_hang_hotkey=True,
        )

    def _set_points_ui(self, cur: int | None = None, target: int | None = None) -> None:
        """
        Refresh 目标点 label as current/target (e.g. 20/70).

        @author by ak
        """
        if cur is not None:
            try:
                self._points_cur = max(0, int(cur))
            except Exception:
                pass
        if target is not None:
            try:
                self._points_target = max(1, int(target))
            except Exception:
                pass
        self.var_points.set(f"{self._points_cur}/{self._points_target}")

    def _set_map_header(self, label: str | None) -> None:
        """
        Push map part of host feature window top-right caption.

        Host keeps role name + live coords; this only refreshes the map segment.

        @author by ak
        """
        text = (label or "").strip()
        if not text or text == "-":
            return
        try:
            master = self.winfo_toplevel()
            if hasattr(master, "set_map_label"):
                master.set_map_label(text)
            elif hasattr(master, "set_live_status"):
                master.set_live_status(map_label=text)
        except Exception:
            pass

    def _push(self, kind: str, payload=None) -> None:
        self._ui_q.put((kind, payload))

    def _drain_ui(self) -> None:
        try:
            while True:
                kind, payload = self._ui_q.get_nowait()
                if kind == "status":
                    self.var_status.set(str(payload or ""))
                elif kind == "log":
                    self.log(str(payload or ""))
                elif kind == "event":
                    self._apply_event(payload)
                elif kind == "running":
                    self._set_running(bool(payload))
                elif kind == "snapshot":
                    d = payload if isinstance(payload, dict) else {}
                    pts = d.get("points")
                    tgt = d.get("target")
                    if pts is not None or tgt is not None:
                        self._set_points_ui(
                            int(pts) if pts is not None else None,
                            int(tgt) if tgt is not None else None,
                        )
                    lab = d.get("scene_label")
                    if lab:
                        self._set_map_header(str(lab))
                elif kind == "snapshot_done":
                    self._snapshot_busy = False
                elif kind == "afk_picked":
                    d = payload if isinstance(payload, dict) else {}
                    pos = d.get("pos")
                    err = d.get("error")
                    lab = d.get("scene_label")
                    if err:
                        self.var_status.set(f"取坐标失败: {err}")
                        self.log(f"自动副本 [切糕] 取挂机坐标失败: {err}")
                    elif not pos or len(pos) < 3:
                        self.var_status.set("取坐标失败: 无坐标")
                        self.log("自动副本 [切糕] 取挂机坐标失败: 无坐标")
                    else:
                        try:
                            x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
                        except Exception:
                            self.var_status.set("取坐标失败: 坐标无效")
                        else:
                            self._persist_qiegao_afk_slot((x, y, z))
                            try:
                                self.var_afk_x.set(f"{x:.1f}")
                                self.var_afk_y.set(f"{y:.1f}")
                                self.var_afk_z.set(f"{z:.1f}")
                            except Exception:
                                pass
                            role = "小号" if self._qiegao_is_alt_ui() else "主号"
                            msg = f"{role}挂机坐标已更新 ({x:.1f}, {y:.1f}, {z:.1f})"
                            if lab:
                                msg += f" · {lab}"
                            self.var_status.set(msg)
                            self.log(f"自动副本 [切糕] {msg}")
                            self.user_log(msg, category=CAT_ACTIVITY)
                elif kind == "refresh_snapshot":
                    self._snapshot_busy = False
                    self.after(50, self._refresh_live_snapshot)
        except queue.Empty:
            pass
        self._schedule_ui_drain(120)

    def _apply_event(self, ev) -> None:
        """Map runner event to step marks + status + points/map header. @author by ak"""
        if isinstance(ev, ActivityStepEvent):
            d = ev.to_dict()
        elif isinstance(ev, dict):
            d = ev
        else:
            return
        phase = str(d.get("phase") or "")
        msg = str(d.get("message") or phase)
        ok = bool(d.get("ok", True))
        detail = d.get("detail") if isinstance(d.get("detail"), dict) else {}
        if not detail and isinstance(ev, ActivityStepEvent):
            detail = ev.detail or {}

        # Points: prefer detail; default target is full award tier.
        pts = detail.get("points")
        tgt = detail.get("target_points")
        if pts is not None or tgt is not None:
            self._set_points_ui(
                int(pts) if pts is not None else None,
                int(tgt) if tgt is not None else None,
            )

        # Map header: scene_label or "name (id)"
        lab = detail.get("scene_label")
        sid = detail.get("scene_id")
        if lab:
            self._set_map_header(str(lab))
        elif sid is not None:
            self._set_map_header(f"scene={sid}")

        # Game process died: stop this runner and unload THIS window only.
        if phase == "game_dead":
            self.var_status.set(msg or "游戏已崩溃/退出")
            self.log(f"自动副本 [game_dead] {msg}")
            try:
                self.user_log(
                    f"游戏崩溃/退出，停止自动副本并卸载本窗 · {msg}",
                    category="activity",
                )
            except Exception:
                pass
            self._runner = None
            self._set_running(False)
            try:
                self.after(50, self._unload_self_after_game_dead)
            except Exception:
                try:
                    self._unload_self_after_game_dead()
                except Exception:
                    pass
            return

        # entry_cd ticks: update status only (avoid log spam).
        if phase == "entry_cd":
            remain = detail.get("remain_s") if detail else None
            if remain is not None:
                self.var_status.set(f"冷却 {float(remain):.0f}s 后继续")
            else:
                self.var_status.set(msg)
            idx = self._phase_step_index(phase)
            if idx is not None:
                self._mark_step(idx, "…")
            return
        if phase == "afk_hang":
            inst = detail.get("instance_remain_s") if detail else None
            if inst is not None:
                try:
                    from app.core.activity_auto import format_instance_remain
                    self.var_status.set(
                        f"挂机中… 副本倒计时 {format_instance_remain(inst)}"
                    )
                except Exception:
                    self.var_status.set(msg)
            else:
                self.var_status.set(msg)
            # 正式日志：仅在「开始挂机」里程碑打一次，不刷倒计时/补发
            if (
                "等待离开副本" in (msg or "")
                or "开启挂机成功" in (msg or "")
                or bool((detail or {}).get("countdown_stop"))
            ):
                try:
                    line = self._format_activity_user_log(phase, msg, ok, detail)
                    if line:
                        self.user_log(line, category=CAT_ACTIVITY, dedupe_s=8.0)
                except Exception:
                    pass
            idx = self._phase_step_index(phase)
            if idx is not None:
                self._mark_step(idx, "…")
            return
        if phase == "afk_wait":
            remain = detail.get("remain_s") if detail else None
            if remain is not None:
                self.var_status.set(f"阶段等待… {float(remain):.0f}s")
            else:
                self.var_status.set(msg)
            idx = self._phase_step_index(phase)
            if idx is not None:
                self._mark_step(idx, "…")
            return
        self.var_status.set(msg)
        # 开发日志保留全量；正式面板日志只关心：第几次 / 完成与否
        self.log(f"自动副本 [{phase}] {msg}")
        try:
            line = self._format_activity_user_log(phase, msg, ok, detail)
            if line:
                self.user_log(line, category=CAT_ACTIVITY, dedupe_s=1.5)
        except Exception:
            pass
        # Master: fan-out claim to slaves when a claim pass finishes（仅活跃模式）.
        if (
            phase == "claim"
            and "clicked" in detail
            and self._mode_ui() == "activity"
        ):
            self._publish_claim_activity_sync(detail)
        if phase in ("stopped", "done"):
            # 注意：中间态 phase="stop"（如挂机中断重试）不得清 UI 运行态，
            # 否则切页回来后停止按钮失效、无法停游戏内流程。
            if phase == "done":
                mode = self._mode_ui()
                last_idx = 4 if mode == "dungeon" else 5
                self._mark_step(last_idx, "✓")
            try:
                self._clear_host_runner()
            except Exception:
                pass
            self._runner = None
            self._set_running(False)
            return
        if phase == "stop":
            # 仅提示状态，保持运行中（runner 仍可能继续下一轮/等待）
            try:
                if self._runner and self._runner.is_running():
                    self._set_running(True)
            except Exception:
                pass
            return
        if phase == "mob_freq":
            try:
                short = (msg or "")[:140]
                if short:
                    self.var_status.set(short)
            except Exception:
                pass
            return
        idx = self._phase_step_index(phase)
        if idx is None:
            return
        if phase == "claim" and self._mode_ui() != "activity":
            return
        if phase in ("enter_ok", "claim", "afk_move"):
            self._mark_step(idx, "✓" if ok else "×")
        elif phase in ("enter_fail",) or not ok:
            self._mark_step(idx, "×")
        else:
            self._mark_step(idx, "…")


    def _format_activity_user_log(
        self,
        phase: str,
        msg: str,
        ok: bool,
        detail: dict | None,
    ) -> str | None:
        """
        正式面板「副本」类日志：只保留用户关心的进本次数与完成结果。

        过滤 attach / 寻路细节 / 补发跳过 / 内部中断等噪音。
        「次」= 已成功完成次数 + 进行中的一趟，不是内部循环 _rounds。

        @author by ak
        """
        detail = detail if isinstance(detail, dict) else {}
        msg = str(msg or "").strip()
        phase = str(phase or "")

        def _n_done() -> int | None:
            v = detail.get("ok_count")
            try:
                return int(v) if v is not None else None
            except Exception:
                return None

        def _n_run() -> int | None:
            v = detail.get("run_index")
            try:
                if v is not None:
                    return int(v)
            except Exception:
                pass
            d = _n_done()
            return (d + 1) if d is not None else None

        # —— 里程碑 ——
        if phase == "round":
            # 状态栏已显示；正式日志不刷「准备第N次」（中断重试会误会成又进一次本）
            return None
        if phase in ("attach", "status", "afk_wait", "entry_cd", "mob_freq"):
            return None
        if phase == "afk_move":
            # 仅严重失败
            if not ok and any(
                k in msg
                for k in ("失败", "超时", "未走到", "模块不可用", "异常")
            ):
                short = msg.replace("自动副本", "").strip()
                return f"寻路异常：{short[:100]}"
            return None
        if phase == "afk_hang":
            if bool(detail.get("countdown_stop")):
                return f"副本倒计时关内挂：{msg[:100]}"
            if "等待离开副本" in msg:
                n = _n_run()
                return f"第 {n} 次 · 已到位挂机" if n else "已到位挂机"
            if "开启挂机成功" in msg:
                return None  # 上面里程碑已覆盖
            if not ok and ("失败" in msg or "未能" in msg):
                return f"挂机异常：{msg[:80]}"
            return None
        if phase == "enter_wait":
            if "等待主号" in msg or "小号" in msg:
                n = _n_run()
                return f"第 {n} 次 · 等待主号带入" if n else "等待主号带入"
            return None
        if phase == "enter_ok":
            # 完成回城 与 刚进本 都曾复用 enter_ok
            if "完成" in msg or "回城" in msg:
                n = _n_done()
                return f"第 {n} 次完成（已回城）" if n is not None else "本趟完成（已回城）"
            n = _n_run()
            return f"第 {n} 次进本成功" if n else "进本成功"
        if phase == "enter_fail":
            n = _n_run()
            return f"第 {n} 次进本失败" if n else f"进本失败：{msg[:60]}"
        if phase == "wait_return":
            return None
        if phase == "stop":
            # 内部中断/重试，不刷用户日志
            return None
        if phase == "claim":
            if self._mode_ui() != "activity":
                return None
            if "完成" in msg or "clicked" in detail or "领取" in msg:
                return f"领宝箱：{msg[:90]}"
            return None
        if phase == "done":
            n = _n_done()
            return f"全部完成 · 共成功 {n} 次" if n is not None else f"全部完成 · {msg[:80]}"
        if phase == "stopped":
            n = _n_done()
            fail = detail.get("fail_count")
            try:
                fail_i = int(fail) if fail is not None else None
            except Exception:
                fail_i = None
            if n is not None and fail_i is not None:
                return f"已停止 · 完成 {n} 次 · 失败 {fail_i} 次"
            if "完成" in msg or "停止" in msg:
                return msg[:100]
            return f"已停止 · {msg[:80]}"
        if phase == "error":
            return f"错误：{msg[:100]}"
        if phase == "gate" and not ok:
            return f"未就绪：{msg[:80]}"
        if phase == "game_dead":
            return f"游戏已退出：{msg[:80]}"
        return None

    def _phase_step_index(self, phase: str) -> int | None:
        """Map runner phase -> step row index for current mode. @author by ak"""
        if self._mode_ui() == "qiegao":
            # 小号：0检测 → 1等待带入(可选) → 2寻路 → 3开挂/站桩 → 4回城 → 5冷却
            # 主号：0城门 → 1进本 → 2等待/过图 → 3寻路 → 4挂机回城 → 5冷却
            is_alt = False
            try:
                is_alt = bool(self.var_qiegao_is_alt.get())
            except Exception:
                is_alt = False
            if is_alt:
                qmap = {
                    "gate": 0,
                    "enter_wait": 1,
                    "enter_ok": 1,
                    "enter_fail": 1,
                    "afk_wait": 1,
                    "afk_move": 2,
                    "afk_hang": 3,
                    "wait_return": 4,
                    "entry_cd": 5,
                    "done": 5,
                }
            else:
                qmap = {
                    "gate": 0,
                    "path_open": 1,
                    "enter_wait": 2,
                    "enter_ok": 2,
                    "enter_fail": 2,
                    "afk_wait": 2,
                    "afk_bridge": 2,
                    "afk_move": 3,
                    "afk_hang": 4,
                    "wait_return": 4,
                    "entry_cd": 5,
                    "done": 5,
                }
            return qmap.get(phase)
        return self._PHASE_STEP.get(phase)

    def _publish_claim_activity_sync(self, detail: dict | None = None) -> None:
        """
        Master fan-out: after local 活跃领箱, push claim_activity to slaves.

        副本模式不做群控同步。

        @author by ak
        """
        if self._mode_ui() != "activity":
            return
        detail = detail if isinstance(detail, dict) else {}
        pid = int(self._fixed_pid or 0)
        if not pid:
            return
        role = self._control_role_shared()
        if role != ROLE_MASTER:
            return
        # Skip pure "claim disabled" / soft skips that never attempted.
        if detail.get("skipped") is True and not detail.get("clicked"):
            return
        pts = detail.get("points")
        try:
            pts_i = int(pts) if pts is not None else None
        except (TypeError, ValueError):
            pts_i = None
        if pts_i is None:
            try:
                pts_i = int(self._points_cur)
            except Exception:
                pts_i = None
        reason = str(detail.get("reason") or "")
        hub = get_task_sync_hub()
        # Ensure this pid is registered as master (TaskPage may have set it).
        try:
            hub.set_role(pid, ROLE_MASTER)
        except Exception:
            pass
        n = hub.publish(
            action=ACTION_CLAIM_ACTIVITY,
            task_id=0,
            source_pid=pid,
            name=reason,
            points=pts_i,
        )
        snap = hub.snapshot()
        cloud_ok = False
        try:
            self._apply_cloud_control(bind_listener=False)
            br = get_cloud_sync_bridge(pid, log=self.log)
            cloud_ok = br.publish_event(
                action=ACTION_CLAIM_ACTIVITY,
                task_id=0,
                source_pid=pid,
                name=reason,
                points=pts_i,
            )
        except Exception as e:
            self.log(f"自动副本 [云控] 发送失败: {e}")
        team_ok = False
        try:
            # ActivityPage can hold an older settings snapshot than SettingsPage.
            # For a claim event, read the mounted role control file directly so
            # an already-enabled team checkbox cannot be missed here.
            team_enabled = team_control_flag_enabled(self.settings)
            rid = self._current_role_id()
            if rid:
                from app.core.account_manager import load_role_control

                ctl = load_role_control(rid)
                if "team" in ctl:
                    team_enabled = bool(ctl.get("team"))
                    self.settings["team_control_enabled"] = team_enabled
            self.log(
                f"自动副本 [队内控] 领箱检查 role_id={rid or '-'} "
                f"enabled={int(team_enabled)}"
            )
            if team_enabled:
                extra_team = f"points={pts_i}" if pts_i is not None else None
                team_text = build_master_command(
                    ACTION_CLAIM_ACTIVITY,
                    [0],
                    extra=extra_team,
                )
                tres = send_team_message(
                    pid,
                    team_text,
                    log=lambda m: self.log(str(m)),
                )
                team_ok = bool(tres.get("ok"))
                if not team_ok and str(tres.get("error") or ""):
                    self.log(f"自动副本 [队内控] 领箱发送失败: {tres.get('error')}")
        except Exception as e:
            self.log(f"自动副本 [队内控] 领箱发送失败: {e}")
        if n or cloud_ok or team_ok:
            parts = []
            if n:
                parts.append(f"{n} 本机副控")
            if cloud_ok:
                parts.append("云控")
            if team_ok:
                parts.append("队内控")
            dest = " + ".join(parts)
            self.log(
                f"自动副本 [同步] 主控已发送领宝箱 → {dest} "
                f"points={pts_i} reason={reason or '-'}"
            )
            self.var_status.set(f"已同步领宝箱到 {dest}")
            self.user_log(
                f"主控通知：已同步领活跃宝箱 → {dest}"
                + (f" · 活跃点 {pts_i}" if pts_i is not None else ""),
                category=CAT_CONTROL,
            )
        else:
            self.log(
                f"自动副本 [同步] 主控已领箱但无副控接收 "
                f"slaves={snap.get('slaves')} listeners={snap.get('listeners')} cloud=0"
            )
            self.user_log(
                "主控通知：领活跃宝箱已完成，但当前无在线副控",
                category=CAT_CONTROL,
            )

    def handle_claim_activity_sync(
        self,
        *,
        source_pid: int = 0,
        points: int | None = None,
        reason: str = "",
    ) -> None:
        """
        Slave entry: claim local flourish chests from master event.

        @author by ak
        """
        role = self._control_role_shared()
        if role != ROLE_SLAVE:
            self.log(
                f"自动副本 [副控] 忽略领箱同步：本窗角色={role} "
                f"pid={int(self._fixed_pid or 0)}"
            )
            return
        if self._runner is not None and self._runner.is_running():
            cfg = getattr(self._runner, "cfg", None)
            if cfg is not None and bool(getattr(cfg, "claim_awards", False)):
                # Runner already claims; avoid double packet spam.
                self.log("自动副本 [副控] runner 运行中且已开领宝箱，跳过同步领箱")
                return
        sess = self.selected_session()
        if sess is None:
            self.var_status.set("副控领箱失败：未挂载")
            self.log("自动副本 [副控] 领箱同步失败：无会话")
            return
        pid = int(sess.pid)
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        pts = points
        try:
            pts_i = int(pts) if pts is not None else None
        except (TypeError, ValueError):
            pts_i = None
        self.var_status.set(
            f"副控收到主控{int(source_pid or 0)} 领宝箱"
            + (f" points={pts_i}" if pts_i is not None else "")
        )
        self.user_log(
            f"受控通知：收到主控{int(source_pid or 0)} 领活跃宝箱"
            + (f" · 活跃点 {pts_i}" if pts_i is not None else ""),
            category=CAT_CONTROL,
        )
        self.log(
            f"自动副本 [副控] 收到 master={int(source_pid or 0)} claim_activity "
            f"points={pts_i} reason={reason or '-'}"
        )

        def worker() -> None:
            out = {"ok": False, "error": "not started"}
            try:
                from app.core.super_loot import open_attach_session

                session = open_attach_session(pid, log=lambda m: self._push("log", m))
                try:
                    out = claim_daily_activity_awards(
                        session,
                        hwnd=hwnd,
                        allow_cursor=False,
                        log=lambda m: self._push("log", m),
                        points=pts_i,
                    )
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass
            except Exception as e:
                out = {"ok": False, "error": str(e)}
            clicked = out.get("clicked") or []
            skipped = out.get("skipped") or []
            err = out.get("error")
            ok = bool(out.get("ok")) and not err
            self._push(
                "log",
                f"自动副本 [副控] 领箱完成 ok={ok} clicked={len(clicked)} "
                f"skipped={len(skipped)} err={err}",
            )
            self._push(
                "status",
                f"副控领箱完成 clicked={len(clicked)}"
                + (f" err={err}" if err else ""),
            )
            # Ask UI thread to refresh points after claim.
            self._push("refresh_snapshot", None)

        threading.Thread(target=worker, daemon=True).start()

    def _freeze_param_widgets(self, frozen: bool) -> None:
        """Freeze type/instance/params while runner is active. @author by ak"""
        st_ro = "disabled" if frozen else "readonly"
        st_ent = tk.DISABLED if frozen else tk.NORMAL
        st_btn = tk.DISABLED if frozen else tk.NORMAL
        for w in (
            getattr(self, "cmb_mode", None),
            getattr(self, "cmb_inst", None),
            getattr(self, "cmb_city", None),
        ):
            if w is None:
                continue
            try:
                w.configure(state=st_ro)
            except Exception:
                pass
        for chk_name in ("chk_claim", "chk_qiegao_alt", "chk_analyze_mob_freq"):
            chk = getattr(self, chk_name, None)
            if chk is not None:
                try:
                    chk.configure(state=st_btn)
                except Exception:
                    pass
        for name in (
            "ent_max_runs",
            "ent_entry_cd",
            "ent_qiegao_max",
            "ent_afk_x",
            "ent_afk_y",
            "ent_afk_z",
            "btn_afk_pick",
        ):
            w = getattr(self, name, None)
            if w is None:
                continue
            try:
                w.configure(state=st_btn if name.startswith("btn") else st_ent)
            except Exception:
                pass

    def _set_running(self, running: bool) -> None:
        """Freeze params on start; unlock on stop. @author by ak"""
        super()._set_running(running)
        self._freeze_param_widgets(bool(running))

    def _on_pick_afk_point(self) -> None:
        """Read current character scene xyz into 挂机点. @author by ak"""
        if self._runner and self._runner.is_running():
            self.var_status.set("运行中不可取挂机坐标")
            return
        sess = self._require_session()
        if not sess:
            return
        self.var_status.set("取挂机坐标…")

        def worker() -> None:
            pos = None
            lab = None
            err = None
            try:
                from app.core.super_loot import open_attach_session

                session = open_attach_session(int(sess.pid), log=lambda _m: None)
                try:
                    _sid, pos, lab = read_scene_state(session, log=lambda _m: None)
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass
            except Exception as e:
                err = str(e)
            self._push("afk_picked", {"pos": pos, "scene_label": lab, "error": err})

        threading.Thread(target=worker, daemon=True).start()
    def _plan_page_busy(self) -> bool:
        """True when the schedule plan runner is active (mutual exclusion). @author by ak"""
        try:
            tp = self._sibling_page("task")
            if tp is not None:
                runner = getattr(tp, "_runner", None)
                if runner is not None and runner.is_running():
                    return True
        except Exception:
            pass
        return False

    def _on_start(self) -> None:
        sess = self._require_session()
        if not sess:
            return
        role_id = str(getattr(sess, "role_id", "") or "").strip()
        if not role_id:
            self.var_status.set("角色身份未绑定完成，请稍候后重试")
            self.log("自动副本: blocked role_id not bound")
            return
        sync_role_prefs_to_settings(self.settings, role_id)
        if self._runner and self._runner.is_running():
            return
        if self._plan_page_busy():
            self.var_status.set("计划任务正在执行，请从自动任务页暂停或停止")
            self.log("自动副本: blocked 计划任务运行中")
            return
        if self._is_qiegao_mode_ui():
            ax, ay, az = self._afk_coords_from_settings()
            if ax is None:
                self.var_status.set("挂机坐标无效")
                self.log("自动副本: 切糕挂机坐标无效")
                return
        self._reset_steps()
        cfg = self._cfg_from_ui()
        self._set_points_ui(self._points_cur, cfg.target_points)
        self.var_status.set(f"启动 pid={sess.pid}")
        inst_label = ""
        try:
            inst_label = (self.var_inst.get() or "").strip()
        except Exception:
            inst_label = ""
        mode_ui = self._mode_ui()
        mode_lab = {
            "dungeon": self._MODE_DUNGEON,
            "qiegao": self._MODE_QIEGAO,
        }.get(mode_ui, self._MODE_ACTIVITY)
        if mode_ui == "dungeon":
            lim = "不限" if int(cfg.max_runs) <= 0 else str(int(cfg.max_runs))
            extra = f"max_runs={lim} no-claim"
            inst_show = inst_label or cfg.instance_id
        elif mode_ui == "qiegao":
            lim = "不限" if int(cfg.max_runs) <= 0 else str(int(cfg.max_runs))
            afk = (
                f"({cfg.qiegao_afk_x:.1f},{cfg.qiegao_afk_y:.1f},{cfg.qiegao_afk_z:.1f})"
                if cfg.qiegao_afk_x is not None
                else "none"
            )
            role = "alt" if bool(getattr(cfg, "qiegao_is_alt", False)) else "main"
            afreq = (
                "on"
                if bool(getattr(cfg, "qiegao_analyze_mob_freq", False))
                else "off"
            )
            extra = (
                f"max_runs={lim} role={role} mob_freq={afreq} "
                f"stage={cfg.qiegao_stage_wait_s:.0f}s afk={afk}"
            )
            inst_show = f"{QIEGAO_TASK_LABEL}/{QIEGAO_INSTANCE_NAME}"
        else:
            extra = f"target={cfg.target_points} claim={cfg.claim_awards}"
            inst_show = "绿竹幻想乡"
        self.log(
            f"自动副本启动 pid={sess.pid} "
            f"mode={mode_lab} "
            f"inst={inst_show} "
            f"city={cfg.city_gate} "
            f"{extra}"
        )

        def on_event(ev: ActivityStepEvent) -> None:
            self._push("event", ev)

        self._runner = ActivityRunner(
            pid=sess.pid,
            hwnd=sess.hwnd,
            role_id=role_id,
            cfg=cfg,
            hang_settings=dict(self.settings),
            on_event=on_event,
            log=lambda m: self._push("log", m),
        )
        self._runner.start()
        self._register_host_runner(self._runner)
        self._set_running(True)

    def _unload_self_after_game_dead(self) -> None:
        """
        Stop this feature window and unmount only the dead game pid.

        Does not touch other multi-open clients.
        @author by ak
        """
        pid = int(self._fixed_pid or 0)
        if not pid:
            try:
                sess = self.selected_session()
                pid = int(sess.pid) if sess is not None else 0
            except Exception:
                pid = 0
        self.log(f"自动副本: 游戏已退出，卸载本窗 pid={pid}")
        try:
            self.var_status.set(f"游戏已退出 · 已卸载 pid={pid}")
        except Exception:
            pass
        # Prefer ShellApp.unload_feature_for_pid so feature_wins stays consistent.
        top = None
        try:
            top = self.winfo_toplevel()
        except Exception:
            top = None
        shell = None
        try:
            # SessionFeatureWindow.master is ShellApp; also walk parents as fallback.
            cands = []
            if top is not None:
                cands.append(getattr(top, "master", None))
                cands.append(top)
            node = self.master
            for _ in range(8):
                if node is None:
                    break
                cands.append(node)
                node = getattr(node, "master", None)
            for c in cands:
                if c is not None and hasattr(c, "unload_feature_for_pid"):
                    shell = c
                    break
        except Exception:
            shell = None
        if shell is not None:
            try:
                shell.unload_feature_for_pid(pid, reason="game_dead")
                return
            except Exception as e:
                self.log(f"自动副本: shell 卸载失败: {e}")
        # Fallback: local shutdown + unmount + destroy (this pid only).
        try:
            if top is not None and hasattr(top, "shutdown"):
                top.shutdown()
        except Exception:
            pass
        if pid:
            try:
                self.store.unmount(int(pid))
            except Exception:
                pass
        try:
            if top is not None:
                top.destroy()
        except Exception:
            pass

    def _register_host_runner(self, runner) -> None:
        """Keep a host-level ref so page switch cannot lose stop handle. @author by ak"""
        try:
            top = self.winfo_toplevel()
            reg = getattr(top, "_feature_runners", None)
            if not isinstance(reg, dict):
                reg = {}
                setattr(top, "_feature_runners", reg)
            reg["activity"] = runner
        except Exception:
            pass

    def _clear_host_runner(self) -> None:
        try:
            top = self.winfo_toplevel()
            reg = getattr(top, "_feature_runners", None)
            if isinstance(reg, dict) and reg.get("activity") is self._runner:
                reg.pop("activity", None)
        except Exception:
            pass

    def _on_stop(self) -> None:
        # reclaim host ref if local lost after tab switch
        if self._runner is None:
            try:
                top = self.winfo_toplevel()
                reg = getattr(top, "_feature_runners", None) or {}
                r = reg.get("activity")
                if r is not None:
                    self._runner = r
            except Exception:
                pass
        if self._runner is not None:
            try:
                self._runner.stop()
            except Exception as e:
                self.log(f"自动副本: stop err {e}")
            try:
                still = bool(self._runner.is_running())
            except Exception:
                still = False
            if not still:
                self._clear_host_runner()
                self._runner = None
        self.var_status.set("已停止")
        self.log("自动副本: 停止")
        self._set_running(False)


class GroceryPage(FeaturePage):
    """
    杂货使用 — bag auto-use / auto-sell / keep gold via revive pills.

    @author by ak
    """

    title = "杂货使用"
    key = "grocery"

    def _build(self) -> None:
        self._maybe_bind_target(self)

        # 底部操作栏固定，不参与滚动
        foot = ttk.Frame(self, style="Panel.TFrame")
        foot.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        self.var_status = tk.StringVar(value="待命")
        ttk.Label(
            foot,
            textvariable=self.var_status,
            style="Panel.Muted.TLabel",
            wraplength=420,
            justify=tk.LEFT,
        ).pack(anchor="w", fill=tk.X, pady=(0, 4))
        bar = ttk.Frame(foot, style="Panel.TFrame")
        bar.pack(fill=tk.X)
        self.btn_use_sel = ttk.Button(
            bar,
            text="使用/开箱",
            style="Compact.Accent.TButton",
            width=10,
            command=self._on_use_or_open,
        )
        self.btn_use_sel.pack(side=tk.LEFT)
        self.btn_open_first = ttk.Button(
            bar,
            text="开第一格",
            style="Compact.Accent.TButton",
            width=8,
            command=self._on_open_first,
        )
        self.btn_open_first.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_use = ttk.Button(
            bar,
            text="自动使用",
            style="Compact.TButton",
            width=8,
            command=self._on_auto_use,
        )
        self.btn_use.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_sell = ttk.Button(
            bar,
            text="自动出售",
            style="Compact.TButton",
            width=8,
            command=self._on_auto_sell,
        )
        self.btn_sell.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_full = ttk.Button(
            bar,
            text="卖满金",
            style="Compact.TButton",
            width=8,
            command=self._on_full_gold,
        )
        self.btn_full.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_stop = ttk.Button(
            bar,
            text="停止",
            style="Compact.TButton",
            width=6,
            command=self._on_stop,
            state=tk.NORMAL,
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(8, 0))

        # 主体可滚动：背包/名单完整高度，不够窗口就滚
        outer, body = make_scrollable_body(self, style="Panel.TFrame")
        outer.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        box_gold = section(body, "金钱 / 卖满金")
        box_gold.pack(fill=tk.X, pady=(0, 6))
        row_g = ttk.Frame(box_gold, style="Panel.TFrame")
        row_g.pack(fill=tk.X, pady=1)
        ttk.Label(row_g, text="当前", style="Panel.Muted.TLabel", width=6).pack(
            side=tk.LEFT
        )
        self.var_money = tk.StringVar(value="-")
        ttk.Label(
            row_g, textvariable=self.var_money, style="Panel.Mono.TLabel"
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        row_m = ttk.Frame(box_gold, style="Panel.TFrame")
        row_m.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row_m, text="保底金", style="Panel.Muted.TLabel", width=6).pack(
            side=tk.LEFT
        )
        from app.core.package_api import DEFAULT_FULL_GOLD_MIN_GOLD

        self.var_min_gold = tk.StringVar(value=str(int(DEFAULT_FULL_GOLD_MIN_GOLD)))
        ttk.Entry(row_m, textvariable=self.var_min_gold, width=8).pack(
            side=tk.LEFT, padx=(4, 6)
        )
        ttk.Label(
            row_m,
            text="绑定金不够时自动卖药补",
            style="Panel.Muted.TLabel",
        ).pack(side=tk.LEFT)

        box_bag = section(body, "背包")
        box_bag.pack(fill=tk.X, pady=(0, 6))
        tip = ttk.Label(
            box_bag,
            text="双击勾选。使用/开箱一次；自动使用/出售循环到停止。",
            style="Panel.Muted.TLabel",
            wraplength=420,
            justify=tk.LEFT,
        )
        tip.pack(anchor="w", pady=(0, 4))
        bag_body = ttk.Frame(box_bag, style="Panel.TFrame")
        bag_body.pack(fill=tk.X)
        bag_left = ttk.Frame(bag_body, style="Panel.TFrame")
        bag_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        bag_right = ttk.Frame(bag_body, style="Panel.TFrame")
        bag_right.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        # 固定行高 + 内部滚动，不抢页面高度
        self.bag_list = make_listbox(bag_left, height=10)
        bag_sb = ttk.Scrollbar(bag_left, orient=tk.VERTICAL, command=self.bag_list.yview)
        self.bag_list.configure(yscrollcommand=bag_sb.set)
        self.bag_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        bag_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.bag_list.bind("<Double-Button-1>", self._on_bag_toggle)
        ttk.Button(
            bag_right,
            text="刷新背包",
            style="Compact.TButton",
            width=10,
            command=self._on_refresh_bag,
        ).pack(anchor="n", pady=(0, 4))
        ttk.Button(
            bag_right,
            text="加入使用",
            style="Compact.TButton",
            width=10,
            command=self._on_add_use,
        ).pack(anchor="n", pady=(0, 4))
        ttk.Button(
            bag_right,
            text="加入出售",
            style="Compact.TButton",
            width=10,
            command=self._on_add_sell,
        ).pack(anchor="n", pady=(0, 8))
        ttk.Label(
            bag_right, text="开箱间隔ms", style="Panel.Muted.TLabel"
        ).pack(anchor="w")
        self.var_open_first_ms = tk.StringVar(value="50")
        ttk.Entry(bag_right, textvariable=self.var_open_first_ms, width=8).pack(
            anchor="w", pady=(2, 0)
        )

        mid = ttk.Frame(body, style="Panel.TFrame")
        mid.pack(fill=tk.X, pady=(0, 4))
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)

        box_use = section(mid, "自动使用名单")
        box_use.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        use_row = ttk.Frame(box_use, style="Panel.TFrame")
        use_row.pack(fill=tk.BOTH, expand=True)
        self.use_list = make_listbox(use_row, height=4)
        use_sb = ttk.Scrollbar(use_row, orient=tk.VERTICAL, command=self.use_list.yview)
        self.use_list.configure(yscrollcommand=use_sb.set)
        self.use_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        use_sb.pack(side=tk.RIGHT, fill=tk.Y)
        ttk.Button(
            box_use,
            text="移除",
            style="Compact.TButton",
            width=6,
            command=lambda: self._remove_selected(self.use_list, self._use_names),
        ).pack(anchor="e", pady=(2, 0))

        box_sell = section(mid, "自动出售名单")
        box_sell.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        sell_row = ttk.Frame(box_sell, style="Panel.TFrame")
        sell_row.pack(fill=tk.BOTH, expand=True)
        self.sell_list = make_listbox(sell_row, height=4)
        sell_sb = ttk.Scrollbar(
            sell_row, orient=tk.VERTICAL, command=self.sell_list.yview
        )
        self.sell_list.configure(yscrollcommand=sell_sb.set)
        self.sell_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sell_sb.pack(side=tk.RIGHT, fill=tk.Y)
        ttk.Button(
            box_sell,
            text="移除",
            style="Compact.TButton",
            width=6,
            command=lambda: self._remove_selected(self.sell_list, self._sell_names),
        ).pack(anchor="e", pady=(2, 0))

        # ---- 脚本：换光切糕/快乐丹 → 霸刀牌 ----
        box_script = section(body, "脚本：换光切糕/快乐丹→霸刀牌")
        box_script.pack(fill=tk.X, pady=(0, 6))
        tip_s = ttk.Label(
            box_script,
            text="东方卖材料买残页 → 不败换霸刀牌。目标0=尽量全换，背包多留空。",
            style="Panel.Muted.TLabel",
            wraplength=420,
            justify=tk.LEFT,
        )
        tip_s.pack(anchor="w", pady=(0, 4))
        row_s1 = ttk.Frame(box_script, style="Panel.TFrame")
        row_s1.pack(fill=tk.X, pady=1)
        ttk.Label(row_s1, text="目标牌数", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        self.var_badao_target = tk.StringVar(value="0")
        ttk.Entry(row_s1, textvariable=self.var_badao_target, width=8).pack(
            side=tk.LEFT, padx=(4, 8)
        )
        self.btn_badao = ttk.Button(
            row_s1,
            text="开始换霸刀牌",
            style="Compact.Accent.TButton",
            width=12,
            command=self._on_badao_exchange,
        )
        self.btn_badao.pack(side=tk.LEFT, padx=(4, 0))
        # 运行状态只走页面底部 status，避免与脚本区重复两行

        # ---- 脚本：商城买白云熊胆丸 ----
        box_qshop = section(body, "脚本：商城买白云熊胆丸")
        box_qshop.pack(fill=tk.X, pady=(0, 6))
        tip_q = ttk.Label(
            box_qshop,
            text=(
                f"按固定商品 ID 直买白云熊胆丸；1组={QSHOP_PILL_GROUP_SIZE}个，"
                f"最多{MAX_QSHOP_PILL_GROUPS}组，自动按每笔最多"
                f"{QSHOP_PILL_GROUPS_PER_ORDER}组拆单。"
            ),
            style="Panel.Muted.TLabel",
            wraplength=420,
            justify=tk.LEFT,
        )
        tip_q.pack(anchor="w", pady=(0, 4))
        row_q1 = ttk.Frame(box_qshop, style="Panel.TFrame")
        row_q1.pack(fill=tk.X, pady=1)
        ttk.Label(row_q1, text="购买组数", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        self.var_qshop_groups = tk.StringVar(value="1")
        ttk.Entry(row_q1, textvariable=self.var_qshop_groups, width=8).pack(
            side=tk.LEFT, padx=(4, 8)
        )
        self.btn_qshop = ttk.Button(
            row_q1,
            text="买白云熊胆丸",
            style="Compact.Accent.TButton",
            width=12,
            command=self._on_qshop_buy,
        )
        self.btn_qshop.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_qshop_stop = ttk.Button(
            row_q1,
            text="停止购买",
            style="Compact.TButton",
            width=8,
            command=self._on_qshop_stop,
            state=tk.DISABLED,
        )
        self.btn_qshop_stop.pack(side=tk.LEFT, padx=(4, 0))

        self._bag_rows: list[dict] = []
        self._use_names: list[str] = []
        self._sell_names: list[str] = []
        self._checked: set[int] = set()
        self._ui_q: queue.Queue = queue.Queue()
        self._busy = False
        self._active_job_title = ""
        self._work_stop: threading.Event = threading.Event()
        self._schedule_ui_drain(120)
        self.refresh_sessions()

    def refresh_sessions(self) -> None:
        if hasattr(self, "_refresh_combo"):
            self._refresh_combo()

    def on_page_show(self) -> None:
        """
        Called when user switches to this page; refresh bag once.

        @author by ak
        """
        if self._busy:
            return
        self._on_refresh_bag()

    def _cfg_from_ui(self) -> GroceryConfig:
        """
        Build grocery config from UI.

        保底金: 按「金」填写（默认 44990）。内部 min_money 为铜。
        需卖丸数 = ceil((目标金 - 当前金) / 10)。卖满金持续守护至停止。

        @author by ak
        """
        from app.core.package_api import DEFAULT_FULL_GOLD_MIN_GOLD

        try:
            gold = float(
                str(self.var_min_gold.get() or "").strip()
                or DEFAULT_FULL_GOLD_MIN_GOLD
            )
        except ValueError:
            gold = float(DEFAULT_FULL_GOLD_MIN_GOLD)
        if gold < 0:
            gold = 0.0
        try:
            first_ms = float(str(self.var_open_first_ms.get() or "50").strip())
        except ValueError:
            first_ms = 100.0
        first_ms = max(20.0, min(10000.0, first_ms))
        return GroceryConfig(
            use_names=list(self._use_names),
            sell_names=list(self._sell_names),
            min_money=int(gold * GOLD_UNIT),
            pill_price=10 * GOLD_UNIT,
            open_first_delay_s=first_ms / 1000.0,
        )

    def _push(self, kind: str, payload=None) -> None:
        self._ui_q.put((kind, payload))

    def _drain_ui(self) -> None:
        try:
            while True:
                kind, payload = self._ui_q.get_nowait()
                if kind == "status":
                    self.var_status.set(str(payload or ""))
                elif kind == "log":
                    self.log(str(payload or ""))
                elif kind == "user_log":
                    # worker 线程经队列回主线程写正式日志页
                    try:
                        self.user_log(str(payload or ""), category=CAT_GROCERY)
                    except Exception:
                        self.log(str(payload or ""))
                elif kind == "money":
                    self.var_money.set(str(payload or "-"))
                elif kind == "bag":
                    self._fill_bag(list(payload or []))
                elif kind == "badao_info":
                    if hasattr(self, "var_badao_info"):
                        self.var_badao_info.set(str(payload or ""))
                elif kind == "active_job":
                    self._active_job_title = str(payload or "")
                    if hasattr(self, "btn_qshop_stop"):
                        can_stop_buy = bool(
                            self._busy and self._active_job_title == "商城买丸"
                        )
                        self.btn_qshop_stop.configure(
                            state=tk.NORMAL if can_stop_buy else tk.DISABLED
                        )
                elif kind == "busy":
                    self._busy = bool(payload)
                    st = tk.DISABLED if self._busy else tk.NORMAL
                    for b in (
                        self.btn_use_sel,
                        self.btn_open_first,
                        self.btn_use,
                        self.btn_sell,
                        self.btn_full,
                        getattr(self, "btn_badao", None),
                        getattr(self, "btn_qshop", None),
                    ):
                        if b is not None:
                            b.configure(state=st)
                    # 停止始终可点
                    if hasattr(self, "btn_stop"):
                        self.btn_stop.configure(state=tk.NORMAL)
                    if hasattr(self, "btn_qshop_stop"):
                        can_stop_buy = bool(
                            self._busy and self._active_job_title == "商城买丸"
                        )
                        self.btn_qshop_stop.configure(
                            state=tk.NORMAL if can_stop_buy else tk.DISABLED
                        )
        except queue.Empty:
            pass
        self._schedule_ui_drain(120)

    def _fill_bag(self, rows: list) -> None:
        """
        Fill bag listbox from PackageItem dicts.

        @author by ak
        """
        self.bag_list.delete(0, tk.END)
        self._bag_rows = []
        self._checked.clear()
        if not rows:
            self.bag_list.insert(tk.END, "  （背包为空或未挂载）")
            return
        for d in rows:
            if hasattr(d, "to_dict"):
                d = d.to_dict()
            if not isinstance(d, dict):
                continue
            self._bag_rows.append(d)
            name = str(d.get("name") or "").strip()
            tid = int(d.get("tid") or 0)
            if name and not name.startswith("tid="):
                # named: show Chinese first; keep tid as secondary id
                label = f"{name}  tid={tid}" if tid else name
            elif tid:
                label = f"tid={tid}"
            else:
                label = name or "?"
            package = int(d.get("package") or 2)
            package_label = {2: "主包", 3: "扩展1", 4: "扩展2"}.get(
                package, f"包{package}"
            )
            self.bag_list.insert(
                tk.END,
                f" [ ] {package_label} 槽{int(d.get('slot') or 0):02d}  "
                f"x{int(d.get('count') or 1)}  {label}",
            )

    def _selected_bag_indices(self) -> list[int]:
        """
        Checked slots, or current selection if none checked.

        @author by ak
        """
        if self._checked:
            return sorted(i for i in self._checked if 0 <= i < len(self._bag_rows))
        try:
            return [int(i) for i in self.bag_list.curselection()]
        except Exception:
            return []

    def _on_bag_toggle(self, _evt=None) -> None:
        """
        Double-click toggles check mark.

        @author by ak
        """
        try:
            sel = self.bag_list.curselection()
            if not sel:
                return
            idx = int(sel[0])
        except Exception:
            return
        if idx < 0 or idx >= len(self._bag_rows):
            return
        if idx in self._checked:
            self._checked.discard(idx)
            mark = " "
        else:
            self._checked.add(idx)
            mark = "x"
        d = self._bag_rows[idx]
        name = str(d.get("name") or "").strip()
        tid = int(d.get("tid") or 0)
        if name and not name.startswith("tid="):
            label = f"{name}  tid={tid}" if tid else name
        elif tid:
            label = f"tid={tid}"
        else:
            label = name or "?"
        package = int(d.get("package") or 2)
        package_label = {2: "主包", 3: "扩展1", 4: "扩展2"}.get(
            package, f"包{package}"
        )
        self.bag_list.delete(idx)
        self.bag_list.insert(
            idx,
            f" [{mark}] {package_label} 槽{int(d.get('slot') or 0):02d}  "
            f"x{int(d.get('count') or 1)}  {label}",
        )
        self.bag_list.selection_set(idx)

    def _refresh_name_list(self, listbox: tk.Listbox, names: list[str]) -> None:
        listbox.delete(0, tk.END)
        if not names:
            listbox.insert(tk.END, "  （空）")
            return
        for n in names:
            listbox.insert(tk.END, f"  {n}")

    def _remove_selected(self, listbox: tk.Listbox, names: list[str]) -> None:
        try:
            sel = listbox.curselection()
            if not sel or not names:
                return
            idx = int(sel[0])
            if 0 <= idx < len(names):
                names.pop(idx)
                self._refresh_name_list(listbox, names)
        except Exception:
            pass

    def _bag_token(self, d: dict) -> str:
        """
        Name token for list match; fall back to tid=N for unnamed items.

        @author by ak
        """
        n = str(d.get("name") or "").strip()
        if n and not n.startswith("tid="):
            return n
        tid = int(d.get("tid") or 0)
        if tid:
            return f"tid={tid}"
        return n

    def _add_names_from_bag(self, names: list[str], listbox: tk.Listbox) -> None:
        idxs = self._selected_bag_indices()
        if not idxs:
            self.var_status.set("请先勾选或选中背包物品")
            return
        for i in idxs:
            n = self._bag_token(self._bag_rows[i])
            if n and n not in names:
                names.append(n)
        self._refresh_name_list(listbox, names)
        self.var_status.set(f"名单已更新 count={len(names)}")

    def _selected_bag_item_keys(self) -> list[tuple[int, int]]:
        """
        Unique (package, slot) keys of checked / selected carry-bag rows.

        @author by ak
        """
        out: list[tuple[int, int]] = []
        for i in self._selected_bag_indices():
            if 0 <= i < len(self._bag_rows):
                row = self._bag_rows[i]
                out.append(
                    (
                        int(row.get("package") or 2),
                        int(row.get("slot") or 0),
                    )
                )
        return out

    def _on_add_use(self) -> None:
        self._add_names_from_bag(self._use_names, self.use_list)

    def _on_add_sell(self) -> None:
        self._add_names_from_bag(self._sell_names, self.sell_list)

    def _run_job(self, title: str, fn) -> None:
        """
        Run grocery job on worker thread (cancellable via 停止).

        fn(attach, cfg, stop_event) -> result

        @author by ak
        """
        sess = self._require_session()
        if not sess:
            return
        if self._busy:
            self.var_status.set("忙碌中…")
            return
        cfg = self._cfg_from_ui()
        # 新任务用新 stop event；停止按钮始终绑定最新引用
        stop_ev = threading.Event()
        self._work_stop = stop_ev
        self._busy = True
        self._active_job_title = str(title)
        self._push("active_job", title)
        self._push("busy", True)
        self._push("status", f"{title}…（可随时点停止）")
        self.log(f"杂货使用: {title} pid={sess.pid}")
        self.user_log(f"开始「{title}」", category=CAT_GROCERY)

        def worker() -> None:
            attach = None
            try:
                attach = grocery_open_attach(
                    sess.pid, log=lambda m: self._push("log", m)
                )
                res = fn(attach, cfg, stop_ev)
                if hasattr(res, "to_dict"):
                    d = res.to_dict()
                elif isinstance(res, dict):
                    d = res
                else:
                    d = {"message": str(res)}
                msg = d.get("message") or title
                if stop_ev.is_set() and "停止" not in str(msg):
                    msg = f"已停止 · {msg}"
                self._push("status", msg)
                if d.get("money") is not None:
                    try:
                        from app.core.package_api import format_money_pair

                        self._push(
                            "money",
                            format_money_pair(
                                d.get("money"), d.get("money_trade")
                            ),
                        )
                    except Exception:
                        try:
                            self._push("money", format_gold(int(d.get("money"))))
                        except Exception:
                            pass
                line = f"杂货使用 [{d.get('action')}] {msg}"
                self._push("log", line)
                # 停止/完成/异常结束都进正式日志面板
                if stop_ev.is_set():
                    self._push("user_log", f"已停止「{title}」：{msg}")
                else:
                    self._push("user_log", f"结束「{title}」：{msg}")
            except Exception as e:
                self._push("status", f"{title}失败: {e}")
                self._push("log", f"杂货使用失败: {e}")
                self._push("user_log", f"「{title}」失败：{e}")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass
                # 始终清 busy，避免异常后按钮永久锁死
                try:
                    self._push("active_job", "")
                    self._push("busy", False)
                except Exception:
                    self._busy = False
                    self._active_job_title = ""

        threading.Thread(target=worker, daemon=True, name=f"grocery-{title}").start()

    def _on_stop(self) -> None:
        """
        随时可点：置位当前 worker 的 stop event。

        @author by ak
        """
        ev = self._work_stop
        if ev is not None:
            ev.set()
        if self._busy:
            self.var_status.set("正在停止…")
            self.log("杂货使用: 停止")
            try:
                self.user_log("已请求停止（等待当前步骤结束）", category=CAT_GROCERY)
            except Exception:
                pass
        else:
            self.var_status.set("已停止（无运行任务）")
            self.log("杂货使用: 停止（空闲）")
            try:
                self.user_log("停止（当前无运行任务）", category=CAT_GROCERY)
            except Exception:
                pass

    def _on_refresh_bag(self) -> None:
        """刷新背包并同步金钱。"""

        def job(attach, cfg: GroceryConfig, stop_ev):
            items = refresh_bag(
                attach, cfg, log=lambda m: self._push("log", m)
            )
            self._push("bag", [it.to_dict() for it in items])
            money = read_money_text(
                attach, cfg, log=lambda m: self._push("log", m)
            )
            self._push("money", money)
            return type("R", (), {"to_dict": lambda self: {
                "action": "refresh",
                "message": f"背包 {len(items)} 件 · 金钱 {money}",
                "money": None,
            }})()

        self._run_job("刷新背包", job)

    def _on_use_or_open(self) -> None:
        """
        使用/开箱：每个勾选槽只 UseItem 一次（不循环）。

        @author by ak
        """
        item_keys = self._selected_bag_item_keys()
        if not item_keys and not self._use_names:
            self.var_status.set("请先勾选背包物品，或加入使用名单")
            return

        def job(attach, cfg: GroceryConfig, stop_ev):
            r = auto_use_or_open_selected(
                attach,
                cfg,
                item_keys=item_keys or None,
                names=None if item_keys else list(self._use_names),
                stop_event=stop_ev,
                log=lambda m: self._push("log", m),
            )
            items = refresh_bag(attach, cfg, log=lambda m: self._push("log", m))
            self._push("bag", [it.to_dict() for it in items])
            money = read_money_text(
                attach, cfg, log=lambda m: self._push("log", m)
            )
            self._push("money", money)
            return r

        self._run_job("使用/开箱", job)

    def _on_open_first(self) -> None:
        """
        开第一格：持续守护 slot0，直到点停止。

        @author by ak
        """

        def job(attach, cfg: GroceryConfig, stop_ev):
            r = auto_open_first_slot(
                attach,
                cfg,
                slot=0,
                stop_event=stop_ev,
                continuous=True,
                log=lambda m: self._push("log", m),
            )
            items = refresh_bag(attach, cfg, log=lambda m: self._push("log", m))
            self._push("bag", [it.to_dict() for it in items])
            money = read_money_text(
                attach, cfg, log=lambda m: self._push("log", m)
            )
            self._push("money", money)
            return r

        self._run_job("开第一格(守护)", job)

    def _on_auto_use(self) -> None:
        """自动使用名单：持续循环直到停止。"""
        if not self._use_names:
            self.var_status.set("请先加入自动使用名单")
            return
        self._run_job(
            "自动使用(守护)",
            lambda a, c, s: auto_use_selected(
                a,
                c,
                stop_event=s,
                continuous=True,
                log=lambda m: self._push("log", m),
            ),
        )

    def _on_auto_sell(self) -> None:
        """自动出售名单：持续循环直到停止。"""
        if not self._sell_names:
            self.var_status.set("请先加入自动出售名单")
            return
        self._run_job(
            "自动出售(守护)",
            lambda a, c, s: auto_sell_selected(
                a,
                c,
                stop_event=s,
                continuous=True,
                log=lambda m: self._push("log", m),
            ),
        )

    def _on_full_gold(self) -> None:
        def job(attach, cfg: GroceryConfig, stop_ev):
            return auto_full_gold(
                attach,
                cfg,
                stop_event=stop_ev,
                on_money=lambda m: self._push(
                    "money", f"绑定 {format_gold(int(m))}"
                ),
                continuous=True,
                log=lambda m: self._push("log", m),
            )

        self._run_job("卖满金(守护)", job)


    def _badao_cfg_from_ui(self) -> BadaoExchangeConfig:
        """Build badao exchange config from script UI fields. @author by ak"""
        try:
            target = int(str(self.var_badao_target.get() or "0").strip() or "0")
        except Exception:
            target = 0
        target = max(0, min(999999, target))
        hwnd = 0
        try:
            sess = self.selected_session()
            hwnd = int(getattr(sess, "hwnd", 0) or 0) if sess is not None else 0
        except Exception:
            hwnd = 0
        # tid 内置默认，无需 UI 填写
        return BadaoExchangeConfig(
            target_tokens=target,
            hwnd=hwnd,
        )

    def _on_badao_exchange(self) -> None:
        """Start 换光切糕/快乐丹→霸刀牌 pipeline. @author by ak"""
        bcfg = self._badao_cfg_from_ui()

        def job(attach, cfg: GroceryConfig, stop_ev):
            def on_snap(snap, op) -> None:
                try:
                    from app.core.badao_exchange import format_action_status

                    gained = int(
                        getattr(op, "tokens_gained", None)
                        or getattr(bcfg, "_ui_tokens_gained", 0)
                        or 0
                    )
                    info = format_action_status(
                        snap, bcfg, op, tokens_gained=gained
                    )
                except Exception:
                    try:
                        from app.core.badao_exchange import format_progress_line

                        gained = int(getattr(bcfg, "_ui_tokens_gained", 0) or 0)
                        info = format_progress_line(
                            snap, bcfg, tokens_gained=gained
                        )
                    except Exception:
                        info = "已换 ?/?，预计还可换 -"
                # 底部状态栏实时刷新（脚本区不再重复一行）
                self._push("status", info)
                # 正式面板只在「动作类型变化」时记一笔，避免满屏买残页
                try:
                    from app.core.badao_exchange import describe_op_action

                    act = describe_op_action(op, bcfg)
                    kind = str(getattr(op, "kind", "") or "")
                    last = str(getattr(bcfg, "_ui_last_op_kind", "") or "")
                    if act and kind and kind != last:
                        bcfg._ui_last_op_kind = kind
                        self._push("user_log", f"换霸刀牌：{act}")
                except Exception:
                    pass

            def on_status(m: str) -> None:
                s = str(m or "").strip()
                # 仅底部 status；与 on_snap 同源，避免双行重复
                if s:
                    self._push("status", s)
                # 停止/完成/异常类才刷正式日志（动作切换由 on_snap 负责）
                if s and any(
                    k in s
                    for k in (
                        "已停止",
                        "停止",
                        "完成",
                        "失败",
                        "读包异常",
                        "达到目标",
                        "达到步数",
                        "无可转化",
                        "交易无背包",
                        "读背包连续失败",
                        "交易连续失败",
                        "关店重开",
                        "重开商店",
                        "软刷新",
                        "无背包变化",
                        "长歇",
                        "关店重开已达",
                        "暂停",
                    )
                ):
                    self._push("log", f"badao: {s}")
                    self._push("user_log", f"换霸刀牌：{s}")
                # 从状态串粗提 已换+N
                try:
                    import re as _re

                    mm = _re.search(r"已换\+(\d+)", s)
                    if mm:
                        bcfg._ui_tokens_gained = int(mm.group(1))
                    mm2 = _re.search(r"本轮出牌\+(\d+)", s)
                    if mm2:
                        bcfg._ui_tokens_gained = int(mm2.group(1))
                except Exception:
                    pass

            r = run_badao_exchange(
                attach,
                bcfg,
                stop_event=stop_ev,
                log=lambda m: self._push("log", m),
                on_status=on_status,
                on_snap=on_snap,
            )
            try:
                items = refresh_bag(attach, cfg, log=lambda m: self._push("log", m))
                self._push("bag", [it.to_dict() for it in items])
                money = read_money_text(
                    attach, cfg, log=lambda m: self._push("log", m)
                )
                self._push("money", money)
            except Exception as e:
                self._push("log", f"badao refresh after: {e}")
            return r

        self._run_job("换霸刀牌", job)

    def _qshop_groups_from_ui(self) -> int:
        """Parse revive-pill group count from UI. @author by ak"""
        return normalize_qshop_pill_groups(self.var_qshop_groups.get())

    def _on_qshop_stop(self) -> None:
        """Stop QShop batching after the currently executing order."""
        if not self._busy or self._active_job_title != "商城买丸":
            self.var_status.set("当前没有正在执行的商城购买")
            return
        self._work_stop.set()
        self.var_status.set("正在停止购买…当前笔结束后不再下单")
        self._push("user_log", "商城买丸：手动停止，当前笔结束后不再下单")
        try:
            self.btn_qshop_stop.configure(state=tk.DISABLED)
        except Exception:
            pass


    def _on_qshop_buy(self) -> None:
        """Buy 白云熊胆丸 once with full count (native Popup+OnCommand). @author by ak"""
        groups = self._qshop_groups_from_ui()
        cnt = qshop_pill_count_from_groups(groups)
        batches = qshop_pill_batch_counts(groups)

        def job(attach, cfg: GroceryConfig, stop_ev):
            def _log(m: str) -> None:
                self._push("log", m)

            if stop_ev.is_set():
                self._push("user_log", "商城买丸：已停止")
                return None
            self._push(
                "user_log",
                f"商城买丸：固定ID购买 {groups}组 x {QSHOP_PILL_GROUP_SIZE} = {cnt}个 "
                f"（present={DEFAULT_MALL_PILL_PRESENT_ID} "
                f"tid包={DEFAULT_MALL_PILL_PACK_TID} 丸={DEFAULT_MALL_PILL_BAG_TID}；"
                f"自动拆成{len(batches)}笔）",
            )
            self._push("status", f"商城买丸 {groups}组（{cnt}个）…")
            if stop_ev.is_set():
                self._push("user_log", "商城买丸：已停止")
                return None
            last = None
            completed_groups = 0
            for batch_no, batch_count in enumerate(batches, start=1):
                if stop_ev.is_set():
                    self._push(
                        "user_log",
                        f"商城买丸：已停止，已完成{completed_groups}/{groups}组",
                    )
                    break
                batch_groups = batch_count // QSHOP_PILL_GROUP_SIZE
                self._push(
                    "status",
                    f"商城买丸 第{batch_no}/{len(batches)}笔 "
                    f"{batch_groups}组（{batch_count}个）…",
                )
                settle = min(3.0, 0.6 + batch_count * 0.0004)
                last = buy_qshop_present(
                    attach,
                    count=batch_count,
                    present_id=DEFAULT_MALL_PILL_PRESENT_ID,
                    goods_tid=DEFAULT_MALL_PILL_PACK_TID,
                    pack_tid=DEFAULT_MALL_PILL_PACK_TID,
                    pill_tid=DEFAULT_MALL_PILL_BAG_TID,
                    auto_popup=True,
                    force_popup=True,
                    log=_log,
                    settle_s=settle,
                )
                if not bool(getattr(last, "ok", False)):
                    self._push(
                        "user_log",
                        f"商城买丸：第{batch_no}笔未完整到账，停止后续订单",
                    )
                    break
                completed_groups += batch_groups
            ok = bool(last is not None and completed_groups == groups)
            msg = str(
                getattr(last, "message", None)
                or getattr(last, "error", None)
                or ("成功" if ok else "失败")
            )
            if ok:
                self._push("user_log", f"商城买丸：成功 {groups}组（{cnt}个）· {msg}")
            else:
                self._push(
                    "user_log",
                    f"商城买丸：未完成 {completed_groups}/{groups}组 · {msg}",
                )
            try:
                items = refresh_bag(attach, cfg, log=_log)
                self._push("bag", [it.to_dict() for it in items])
                money = read_money_text(attach, cfg, log=_log)
                self._push("money", money)
            except Exception as e:
                self._push("log", f"qshop buy refresh: {e}")
            self._push(
                "status",
                f"商城买丸完成 {'OK' if ok else 'FAIL'} {groups}组（{cnt}个）",
            )
            return last

        self._run_job("商城买丸", job)



class TaskPage(FeaturePage):
    """Accepted tasks, verified routing, accept-by-id and turn-in."""

    title = "自动任务"
    key = "task"

    def _build(self) -> None:
        self._maybe_bind_target(self)
        self._ui_q: queue.Queue = queue.Queue()
        self._tasks: list[dict] = []
        self._nearby: list[dict] = []
        self._clues: list[dict] = []
        self._schedule_queue: list[dict] = load_schedule_queue(self.settings)
        self._runner: ScheduleTaskRunner | TaskRunner | None = None
        self._busy = False
        self._job_gen = 0
        self._work_stop: threading.Event | None = None
        self._fly_working = False
        self._fly_start_lock = threading.Lock()
        self._fly_page_request_id = 0
        self._fly_page_active_request_id = 0
        self._fly_page_requery_pending = False
        self._team_auto_form_working = False
        self._team_gather_working = False
        self._hang_sync_working = False
        self._hang_sync_lock = threading.Lock()
        self._team_hang_force_started = False
        self._pending_task_sync: list[object] = []
        self._pending_task_sync_scheduled = False
        self._last_sync_map_fly_key = ""
        self._last_sync_map_fly_ts = 0.0
        self._sync_map_fly_dedupe_s = 4.0
        self._sync_map_fly_lock = threading.Lock()
        self._list_tab = "自定义"
        self._sync_hub = get_task_sync_hub()
        self._bind_task_sync()
        self._schedule_tick_job = None
        self._custom_status_job = None
        self._custom_audits: dict[str, dict] = {}
        self._team_chat_tick_job = None
        self._team_chat_polling = False
        # None 表示首次启动：_team_chat_tick 会跳到当前最新，跳过队伍频道历史。
        self._team_chat_cursor = None
        self._last_team_ping_ts = 0.0

        # Page-level scroll: 组队 / 飞行 / 任务操作 / 列表 / 推进按钮 全部可滚。
        # 执行计划/取消 不贴底悬浮，放在任务列表下方，用户下滑即可看到。
        outer, body = make_scrollable_body(self, style="Panel.TFrame")
        outer.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # --- 组队 / 召集（成员名单 · 邀请 · 群控飞福州 + 组队跟随）---
        team_box = section(body, "组队")
        team_box.pack(fill=tk.X, pady=(0, 6))
        team_row1 = ttk.Frame(team_box, style="Panel.TFrame")
        team_row1.pack(fill=tk.X)
        ttk.Label(team_row1, text="成员", style="Panel.Muted.TLabel", width=6).pack(
            side=tk.LEFT
        )
        self.var_team_members = tk.StringVar(
            value=str(self.settings.get("team_members") or "")
        )
        # 输入框可伸缩；刷新队伍紧贴右侧，避免整行按钮挤爆
        self.btn_team_refresh = ttk.Button(
            team_row1,
            text="刷新队伍",
            style="Compact.TButton",
            width=9,
            command=self._on_team_refresh_party,
        )
        # 先 pack 右侧按钮，再让输入框吃剩余宽度，避免溢出裁切
        self.btn_team_refresh.pack(side=tk.RIGHT)
        self.ent_team_members = ttk.Entry(
            team_row1, textvariable=self.var_team_members, width=28
        )
        self.ent_team_members.pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4)
        )
        self.ent_team_members.bind("<FocusOut>", lambda _e: self._save_team_settings())
        try:
            self.var_team_members.trace_add(
                "write", lambda *_a: self._on_team_members_edited()
            )
        except Exception:
            pass
        team_row2 = ttk.Frame(team_box, style="Panel.TFrame")
        team_row2.pack(fill=tk.X, pady=(4, 0))
        # legacy key kept in settings; no longer a checkbox that auto-runs
        self.var_team_enabled = tk.BooleanVar(value=False)
        self.settings["team_enabled"] = False
        self.btn_team_verify = ttk.Button(
            team_row2,
            text="校验队伍",
            style="Compact.TButton",
            width=9,
            command=self._on_team_verify,
        )
        self.btn_team_verify.pack(side=tk.LEFT)
        self.btn_team_auto = ttk.Button(
            team_row2,
            text="自动整队",
            style="Compact.TButton",
            width=9,
            command=self._on_team_auto_form,
        )
        self.btn_team_auto.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_team_invite = ttk.Button(
            team_row2,
            text="全部邀请",
            style="Compact.TButton",
            width=9,
            command=self._on_team_invite_now,
        )
        self.btn_team_invite.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_team_gather = ttk.Button(
            team_row2,
            text="召集",
            style="Compact.Accent.TButton",
            width=8,
            command=self._on_team_gather,
        )
        self.btn_team_gather.pack(side=tk.LEFT, padx=(4, 0))
        team_row3 = ttk.Frame(team_box, style="Panel.TFrame")
        team_row3.pack(fill=tk.X, pady=(4, 0))
        self.btn_team_follow_on = ttk.Button(
            team_row3,
            text="组队跟随",
            style="Compact.TButton",
            width=9,
            command=lambda: self._on_team_follow(True),
        )
        self.btn_team_follow_on.pack(side=tk.LEFT)
        self.btn_team_follow_off = ttk.Button(
            team_row3,
            text="取消跟随",
            style="Compact.TButton",
            width=9,
            command=lambda: self._on_team_follow(False),
        )
        self.btn_team_follow_off.pack(side=tk.LEFT, padx=(4, 0))
        # One-shot override for the next synchronized Hang ON command. This is
        # deliberately not persisted to hang prefs or page settings.
        self.var_team_hang_dungeon = tk.BooleanVar(value=False)
        self.chk_team_hang_dungeon = ttk.Checkbutton(
            team_row3,
            text="副本",
            variable=self.var_team_hang_dungeon,
        )
        self.chk_team_hang_dungeon.pack(side=tk.LEFT, padx=(6, 0))
        self.btn_team_hang = ttk.Button(
            team_row3,
            text="取消/开启内挂",
            style="Compact.TButton",
            width=12,
            command=self._on_team_hang_sync,
        )
        self.btn_team_hang.pack(side=tk.LEFT, padx=(4, 0))
        self.var_team_party = tk.StringVar(value="当前队伍：未读取")
        self.lbl_team_party = ttk.Label(
            team_box,
            textvariable=self.var_team_party,
            style="Panel.Muted.TLabel",
            justify="left",
        )
        self.lbl_team_party.pack(anchor="w", fill=tk.X, pady=(4, 0))
        self.var_team_status = tk.StringVar(value="")
        self.lbl_team_status = ttk.Label(
            team_box,
            textvariable=self.var_team_status,
            style="Panel.Muted.TLabel",
            justify="left",
        )
        self.lbl_team_status.pack(anchor="w", fill=tk.X, pady=(2, 0))
        try:
            self.lbl_team_party.configure(wraplength=560)
            self.lbl_team_status.configure(wraplength=560)
        except Exception:
            pass
        self.lbl_team_help = ttk.Label(
            team_box,
            text="校验后可用。整队需群控 · 召集飞福州并跟随；也可单独点组队跟随/取消跟随",
            style="Panel.Muted.TLabel",
            justify="left",
        )
        self.lbl_team_help.pack(anchor="w", fill=tk.X, pady=(2, 0))
        try:
            # wrap so long lines are not clipped horizontally
            self.lbl_team_help.configure(wraplength=560)
        except Exception:
            pass

        def _fit_team_help(_evt=None) -> None:
            # Debounce Configure storms: wraplength changes can re-enter Configure
            # on some themes and freeze/kill the helper UI thread.
            try:
                job = getattr(self, "_team_help_fit_job", None)
                if job is not None:
                    try:
                        self.after_cancel(job)
                    except Exception:
                        pass
                self._team_help_fit_job = self.after(80, _fit_team_help_apply)
            except Exception:
                _fit_team_help_apply()

        def _fit_team_help_apply() -> None:
            self._team_help_fit_job = None
            try:
                w = int(team_box.winfo_width() or 0)
                if w <= 40:
                    return
                wl = max(200, w - 16)
                last = int(getattr(self, "_team_help_wrap_last", 0) or 0)
                if abs(wl - last) < 8:
                    return
                self._team_help_wrap_last = wl
                self.lbl_team_help.configure(wraplength=wl)
                if hasattr(self, "lbl_team_status"):
                    self.lbl_team_status.configure(wraplength=wl)
                if hasattr(self, "lbl_team_party"):
                    self.lbl_team_party.configure(wraplength=wl)
            except Exception:
                pass

        try:
            team_box.bind("<Configure>", _fit_team_help, add="+")
            self.after(50, _fit_team_help_apply)
        except Exception:
            pass
        try:
            self._refresh_team_verify_status_hint()
        except Exception:
            pass
        try:
            self._refresh_team_control_buttons()
        except Exception:
            pass

        # --- 地图飞行：按钮+页点同一行；冷却与说明同一行 ---
        fly_box = section(body, "地图飞行")
        fly_box.pack(fill=tk.X, pady=(0, 6))

        # row1: quick presets + page/slot + fly
        fly_row1 = ttk.Frame(fly_box, style="Panel.TFrame")
        fly_row1.pack(fill=tk.X)
        self.btn_fly_fuzhou = ttk.Button(
            fly_row1,
            text="飞福州",
            style="Compact.TButton",
            width=8,
            command=lambda: self._on_map_fly("fuzhou"),
        )
        self.btn_fly_fuzhou.pack(side=tk.LEFT)
        self.btn_fly_shimen = ttk.Button(
            fly_row1,
            text="飞师门",
            style="Compact.TButton",
            width=8,
            command=lambda: self._on_map_fly("shimen"),
        )
        self.btn_fly_shimen.pack(side=tk.LEFT, padx=(4, 10))

        # 游戏固定分页：默认页 + 六个自定义页（0..5）。
        self._fly_fixed_pages = [("默认", "default")] + [
            (f"{page}页", page) for page in FIXED_CUSTOM_FLY_PAGES
        ]
        ttk.Label(fly_row1, text="页", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        self.var_fly_page = tk.StringVar(value="")
        self.cmb_fly_page = ttk.Combobox(
            fly_row1,
            textvariable=self.var_fly_page,
            values=[p[0] for p in self._fly_fixed_pages],
            width=6,
            state="readonly",
        )
        self.cmb_fly_page.pack(side=tk.LEFT, padx=(3, 8))
        self.cmb_fly_page.bind("<<ComboboxSelected>>", self._on_fly_page_selected)
        ttk.Label(fly_row1, text="点", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        self.var_fly_slot = tk.StringVar(value="")
        self.cmb_fly_slot = ttk.Combobox(
            fly_row1,
            textvariable=self.var_fly_slot,
            width=16,
            state="readonly",
        )
        self.cmb_fly_slot.pack(side=tk.LEFT, padx=(3, 8))
        self.cmb_fly_slot.bind("<<ComboboxSelected>>", self._on_fly_slot_selected)
        self.btn_fly_selected = ttk.Button(
            fly_row1,
            text="飞行",
            style="Compact.Accent.TButton",
            width=6,
            command=self._on_fly_selected_slot,
            state=tk.DISABLED,
        )
        self.btn_fly_selected.pack(side=tk.LEFT)

        self._fly_catalog: list[dict] = []
        self._fly_page_labels: list[str] = []
        self._fly_slot_meta: list[dict] = []
        self._fly_page_loading = False

        # row2: cooldown + help
        fly_help = ttk.Frame(fly_box, style="Panel.TFrame")
        fly_help.pack(fill=tk.X, pady=(4, 0))
        self.var_fly_cd = tk.StringVar(value="冷却: 就绪")
        ttk.Label(
            fly_help,
            textvariable=self.var_fly_cd,
            style="Panel.Muted.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Label(
            fly_help,
            text=" · 快捷飞福州/师门 · 页点自选可同步副控",
            style="Panel.Muted.TLabel",
        ).pack(side=tk.LEFT)
        self.after(400, self._tick_fly_cd)

        # 任务操作放在 地图飞行 与 任务列表 之间
        controls = section(body, "任务操作")
        controls.pack(fill=tk.X, pady=(0, 6))
        row = ttk.Frame(controls, style="Panel.TFrame")
        row.pack(fill=tk.X)
        ttk.Label(row, text="任务ID", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        self.var_task_id = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.var_task_id, width=10).pack(
            side=tk.LEFT, padx=(5, 6)
        )
        self.btn_accept = ttk.Button(
            row,
            text="接取",
            style="Compact.TButton",
            width=6,
            command=self._on_accept,
        )
        self.btn_accept.pack(side=tk.LEFT)
        self.btn_refresh = ttk.Button(
            row,
            text="刷新",
            style="Compact.TButton",
            width=6,
            command=self._on_refresh,
        )
        self.btn_refresh.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_path_npc = ttk.Button(
            row,
            text="寻路NPC",
            style="Compact.TButton",
            width=8,
            command=self._on_path_npc,
        )
        self.btn_path_npc.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_path_portal = ttk.Button(
            row,
            text="寻路地宫点",
            style="Compact.TButton",
            width=10,
            command=self._on_path_portal,
        )
        self.btn_path_portal.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_complete = ttk.Button(
            row,
            text="交付",
            style="Compact.Accent.TButton",
            width=6,
            command=self._on_complete,
        )
        self.btn_complete.pack(side=tk.LEFT, padx=(4, 0))

        split = ttk.Frame(body)
        split.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
        try:
            # 再加高一倍：列表行数 + 固定视口，方便点选多行任务
            # 紧凑可视高度：约 12 行，多出来的任务在列表内滚动
            split.configure(height=300)
            split.pack_propagate(False)
        except Exception:
            pass
        split.columnconfigure(0, weight=1, uniform="splitcols")
        split.columnconfigure(1, weight=1, uniform="splitcols")
        split.rowconfigure(0, weight=1)

        task_box = section(split, "任务列表")
        task_box.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        _, self._tab_var, self._set_list_tab = pill_tabs(
            task_box,
            ("计划", "自定义", "附近", "已接"),
            self._on_list_tab,
            initial="自定义",
        )
        list_host = ttk.Frame(task_box, style="Panel.TFrame")
        list_host.pack(fill=tk.BOTH, expand=True)
        self.task_list = make_listbox(list_host, height=12)
        pack_scrollable_list(list_host, self.task_list)
        self.task_list.bind("<<ListboxSelect>>", self._on_task_select)
        self.task_list.bind("<Double-Button-1>", self._on_task_double)
        self.task_list.bind("<Button-3>", self._on_task_right)
        # 计划任务 status hint line (shown above the list when tab active)
        self.var_custom_status = tk.StringVar(
            value="计划任务 · 右键加入队列"
        )
        self.lbl_custom_status = ttk.Label(
            task_box,
            textvariable=self.var_custom_status,
            style="Panel.Muted.TLabel",
            justify="left",
        )
        self.lbl_custom_status.pack(anchor="w", fill=tk.X, pady=(2, 0))
        try:
            self.lbl_custom_status.configure(wraplength=520)
        except Exception:
            pass

        sched_box = section(split, "计划任务")
        sched_box.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self.var_schedule_status = tk.StringVar(value="右键加入队列")
        # Pack this first so the list consumes only the remaining space and the
        # hint remains at the bottom of the schedule panel.
        ttk.Label(
            sched_box,
            textvariable=self.var_schedule_status,
            style="Panel.Muted.TLabel",
            justify="left",
        ).pack(side=tk.BOTTOM, anchor="w", fill=tk.X, pady=(4, 0))
        self.clue_list = make_listbox(sched_box, height=12)
        pack_scrollable_list(sched_box, self.clue_list)
        self.clue_list.bind("<Double-Button-1>", self._on_schedule_delete)
        self.clue_list.bind("<Button-3>", self._on_schedule_right)

        # Bottom-pinned controls: execute / pause-resume / cancel / save.
        bar = ttk.Frame(self, style="Panel.TFrame")
        bar.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(4, 8))
        self.btn_start = ttk.Button(
            bar,
            text="执行计划",
            style="Compact.Accent.TButton",
            width=9,
            command=self._on_start_or_pause,
        )
        self.btn_start.pack(side=tk.LEFT)
        self.btn_stop = ttk.Button(
            bar,
            text="取消",
            style="Compact.TButton",
            width=6,
            state=tk.DISABLED,
            command=self._on_stop,
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(4, 0))
        self.var_status = tk.StringVar(value="待命")
        ttk.Label(
            bar,
            textvariable=self.var_status,
            style="Panel.Muted.TLabel",
        ).pack(side=tk.LEFT, padx=(10, 0), fill=tk.X, expand=True)
        self.btn_save_plan = ttk.Button(
            bar,
            text="保存",
            style="Compact.TButton",
            width=6,
            command=self._on_save_plan,
        )
        self.btn_save_plan.pack(side=tk.RIGHT)
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

        self._schedule_ui_drain(120)
        self.after(200, self._paint_schedule_list)
        self.after(200, self._paint_custom_list)
        self.after(1500, self._schedule_tick)
        self.after(1000, self._custom_status_tick)
        self.refresh_sessions()


    def refresh_sessions(self) -> None:
        if hasattr(self, "_refresh_combo"):
            self._refresh_combo()
        self._bind_task_sync()
        # 尝试加载计划配置（页面初始化时游戏可能还未就绪，延迟重试）
        try:
            role_id = self._current_role_id()
            if role_id:
                self._load_schedule_profile_for_role(role_id)
                self._paint_schedule_list()
                self._paint_custom_list()
        except Exception:
            pass

    def on_page_show(self) -> None:
        """
        Repaint cached task state + load schedule profile on tab switch.

        @author by ak
        """
        try:
            role_id = self._current_role_id()
            if role_id:
                self._load_schedule_profile_for_role(role_id)
        except Exception:
            pass
        if self._list_tab == "附近":
            self._paint_nearby_list()
        elif self._list_tab in ("计划", "自定义"):
            self._paint_custom_list()
        else:
            self._paint_accepted_list()
        self._paint_schedule_list()

    def destroy(self) -> None:
        try:
            if getattr(self, "_schedule_tick_job", None) is not None:
                self.after_cancel(self._schedule_tick_job)
                self._schedule_tick_job = None
        except Exception:
            pass
        try:
            if getattr(self, "_custom_status_job", None) is not None:
                self.after_cancel(self._custom_status_job)
                self._custom_status_job = None
        except Exception:
            pass
        try:
            if self._work_stop is not None:
                self._work_stop.set()
        except Exception:
            pass
        try:
            if self._runner is not None:
                self._runner.stop()
                self._runner = None
        except Exception:
            pass
        try:
            pid = int(self._fixed_pid or 0)
            if pid:
                self._sync_hub.unsubscribe(pid)
                drop_cloud_sync_bridge(pid)
        except Exception:
            pass
        try:
            self._stop_team_chat_tick()
        except Exception:
            pass
        super().destroy()

    def _bind_task_sync(self) -> None:
        pid = int(self._fixed_pid or 0)
        if not pid:
            return
        # 绑定可能换角色：清 role_id 缓存后重新解析。
        try:
            self._clear_role_id_cache()
        except Exception:
            pass
        # 从角色配置恢复队内控（正式流程：登录绑定后读 control.json）。
        try:
            rid = self._current_role_id()
            if rid:
                from app.core.account_manager import load_role_control

                ctl = load_role_control(rid)
                cfg_role = str(ctl.get("role") or ROLE_NONE).lower()
                if cfg_role in (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE):
                    self.settings["task_control_role"] = cfg_role
                if "team" in ctl:
                    self.settings["team_control_enabled"] = bool(ctl["team"])
        except Exception:
            pass
        role = str(self.settings.get("task_control_role") or ROLE_NONE).lower()
        if role not in (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE):
            role = ROLE_NONE
        # Hub is source of truth; keep settings aligned with hub after exclusive master.
        applied = self._sync_hub.set_role(pid, role)
        if applied != role:
            role = applied
            self.settings["task_control_role"] = role
        # Apply cloud first so isolation uses latest ready config.
        cloud_label = ""
        try:
            cloud_label = self._apply_cloud_control(bind_listener=True)
            self.log(f"自动任务 [云控] {cloud_label}")
        except Exception as e:
            self.log(f"自动任务 [云控] 绑定失败: {e}")
        # Cloud-enabled slave: stay hub role=slave (UI), but drop local listener.
        cloud_isolated = is_cloud_slave_isolated(self.settings)
        # 队内控副控同样不接本机 hub 事件，只执行队伍频道命令。
        team_isolated = is_team_slave_isolated(self.settings)
        if cloud_isolated or team_isolated:
            self._sync_hub.unsubscribe(pid)
        else:
            self._sync_hub.subscribe(pid, self._on_task_sync_event)
        snap = self._sync_hub.snapshot()
        self.log(
            f"自动任务 [群控] 注册 pid={pid} role={role} "
            f"masters={snap.get('masters')} slaves={snap.get('slaves')} "
            f"listeners={snap.get('listeners')}"
            + (" cloud_isolated=1" if cloud_isolated else "")
            + (" team_isolated=1" if team_isolated else "")
        )
        try:
            if is_team_control_ready(self.settings) and role in (
                ROLE_MASTER,
                ROLE_SLAVE,
            ):
                self._start_team_chat_tick()
            else:
                self._stop_team_chat_tick()
        except Exception:
            pass
        try:
            self._refresh_team_control_buttons()
        except Exception:
            pass

    def _control_role(self) -> str:
        """Prefer live hub role for this pid (settings may lag exclusive demote)."""
        pid = int(self._fixed_pid or 0)
        if pid:
            hub_role = self._sync_hub.get_role(pid)
            if hub_role in (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE):
                if self.settings.get("task_control_role") != hub_role:
                    self.settings["task_control_role"] = hub_role
                return hub_role
        role = str(self.settings.get("task_control_role") or ROLE_NONE).lower()
        if role not in (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE):
            return ROLE_NONE
        return role

    def _publish_task_sync(
        self,
        action: str,
        task_id: int,
        *,
        can_finish: bool | None = None,
        name: str = "",
        portal_kind: str = "",
        origin_scene_id: int | None = None,
        portal_tid: int | None = None,
        portal_obj_id: int | None = None,
        portal_x: float | None = None,
        portal_y: float | None = None,
        portal_z: float | None = None,
        members: str = "",
        hang_mode: int | None = None,
        phase: str = "",
        target_scene_id: int | None = None,
        target_tid: int | None = None,
        target_x: float | None = None,
        target_y: float | None = None,
        target_z: float | None = None,
        target_round: int | None = None,
    ) -> bool:
        pid = int(self._fixed_pid or 0)
        act = str(action or "").strip().lower()
        allow_zero = act in (
            ACTION_CLAIM_ACTIVITY,
            ACTION_TEAM_FOLLOW,
    ACTION_DAILY_FOLLOW,
            ACTION_TEAM_LEAVE,
            ACTION_MAP_FLY,
            ACTION_HANG_SYNC,
            ACTION_JIANGLONG_CAST,
            ACTION_ACCEPT_DAILY_TASKS,
            ACTION_DAILY_ROUTE,
        )
        if not pid or (not task_id and not allow_zero):
            return False
        # Re-bind so late Settings changes are reflected before publish.
        self._bind_task_sync()
        role = self._control_role()
        snap = self._sync_hub.snapshot()
        if role != ROLE_MASTER:
            self._push(
                "log",
                f"自动任务 [同步] 跳过 {action} #{task_id}：本窗 pid={pid} "
                f"角色={role}（需主控）。当前 masters={snap.get('masters')} "
                f"slaves={snap.get('slaves')}",
            )
            if role == ROLE_NONE:
                self._push(
                    "status",
                    "未群控同步（本窗无控）· 请在本窗设置勾选主控",
                )
            return False
        members_s = str(members or "")
        publish_name = str(name or "")
        publish_hang_mode = int(hang_mode) if hang_mode in (0, 1) else None
        if act == ACTION_HANG_SYNC and "|m=" in publish_name:
            base_name, _, mode_raw = publish_name.partition("|m=")
            publish_name = base_name.strip()
            try:
                parsed_mode = int(mode_raw.strip())
            except (TypeError, ValueError):
                parsed_mode = None
            if parsed_mode in (0, 1):
                publish_hang_mode = parsed_mode
        n = self._sync_hub.publish(
            action=action,
            task_id=int(task_id),
            source_pid=pid,
            can_finish=can_finish,
            name=publish_name,
            portal_kind=str(portal_kind or ""),
            origin_scene_id=origin_scene_id,
            portal_tid=portal_tid,
            portal_obj_id=portal_obj_id,
            portal_x=portal_x,
            portal_y=portal_y,
            portal_z=portal_z,
            members=members_s,
            hang_mode=publish_hang_mode,
            phase=str(phase or ""),
            target_scene_id=(int(target_scene_id) if target_scene_id is not None else None),
            target_tid=(int(target_tid) if target_tid is not None else None),
            target_x=(float(target_x) if target_x is not None else None),
            target_y=(float(target_y) if target_y is not None else None),
            target_z=(float(target_z) if target_z is not None else None),
            target_round=(int(target_round) if target_round is not None else None),
        )
        cloud_ok = False
        try:
            # ensure cloud config reflects latest settings/role
            self._apply_cloud_control(bind_listener=True)
            br = get_cloud_sync_bridge(pid, log=lambda m: self._push("log", m))
            cloud_ok = br.publish_event(
                action=action,
                task_id=int(task_id),
                source_pid=pid,
                can_finish=can_finish,
                name=publish_name,
                portal_kind=str(portal_kind or ""),
                origin_scene_id=origin_scene_id,
                portal_tid=portal_tid,
                portal_obj_id=portal_obj_id,
                portal_x=portal_x,
                portal_y=portal_y,
                portal_z=portal_z,
                members=members_s,
                hang_mode=publish_hang_mode,
                phase=str(phase or ""),
                target_scene_id=(int(target_scene_id) if target_scene_id is not None else None),
                target_tid=(int(target_tid) if target_tid is not None else None),
                target_x=(float(target_x) if target_x is not None else None),
                target_y=(float(target_y) if target_y is not None else None),
                target_z=(float(target_z) if target_z is not None else None),
                target_round=(int(target_round) if target_round is not None else None),
            )
        except Exception as e:
            self._push("log", f"自动任务 [云控] 发送失败: {e}")
        team_ok = False
        try:
            if team_control_flag_enabled(self.settings):
                # 降龙也用 name(目标角色名) 作为队内控 extra，副控端按名单过滤。
                if action == ACTION_HANG_SYNC and publish_hang_mode in (0, 1):
                    extra_team = f"{publish_name}|m={publish_hang_mode}"
                else:
                    extra_team = str(name or "") if action in (
                        ACTION_MAP_FLY,
                        ACTION_JIANGLONG_CAST,
                    ) else None
                team_text = build_master_command(
                    action,
                    [int(task_id)],
                    extra=extra_team,
                    route_snapshot=(
                        {
                            "portal_x": portal_x,
                            "portal_y": portal_y,
                            "portal_z": portal_z,
                            "portal_kind": portal_kind,
                            "origin_scene_id": origin_scene_id,
                            "portal_tid": portal_tid,
                            "portal_obj_id": portal_obj_id,
                            "phase": phase,
                            "target_scene_id": target_scene_id,
                            "target_tid": target_tid,
                            "target_x": target_x,
                            "target_y": target_y,
                            "target_z": target_z,
                            "target_round": target_round,
                        }
                        if action == ACTION_PATH
                        or (
                            action == ACTION_DAILY_ROUTE
                            and any(
                                value is not None
                                for value in (
                                    portal_x,
                                    portal_y,
                                    portal_z,
                                    origin_scene_id,
                                    portal_tid,
                                    portal_obj_id,
                                    target_scene_id,
                                    target_tid,
                                    target_x,
                                    target_y,
                                    target_z,
                                    target_round,
                                    portal_kind,
                                )
                            )
                        )
                        else None
                    ),
                )
                tres = send_team_message(
                    pid,
                    team_text,
                    log=lambda m: self._push("log", m),
                )
                team_ok = bool(tres.get("ok"))
                if not team_ok and str(tres.get("error") or ""):
                    self._push("log", f"自动任务 [队内控] 发送失败: {tres.get('error')}")
        except Exception as e:
            self._push("log", f"自动任务 [队内控] 发送失败: {e}")
        act = action_cn(action)
        name_s = f"「{name}」" if name else ""
        tid_s = f" #{int(task_id)}" if int(task_id or 0) else ""
        if n or cloud_ok or team_ok:
            parts = []
            if n:
                parts.append(f"本机副控 {n}")
            if cloud_ok:
                parts.append("云控")
            if team_ok:
                parts.append("队内控")
            dest = " + ".join(parts) if parts else "副控"
            self._push(
                "log",
                f"自动任务 [同步] 已发送 {action} #{task_id} → {dest}"
                + (f" local={n}" if n else "")
                + (" cloud=1" if cloud_ok else "")
                + (" team=1" if team_ok else ""),
            )
            self._push("status", f"已同步 {action} #{task_id} → {dest}")
            self.user_log(
                f"主控通知：已同步{act}{name_s}{tid_s} → {dest}",
                category=CAT_CONTROL,
            )
            return True
        else:
            self._push(
                "log",
                f"自动任务 [同步] 主控已发但无副控接收 {action} #{task_id} "
                f"slaves={snap.get('slaves')} listeners={snap.get('listeners')} "
                f"cloud=0 team=0",
            )
            self._push("status", "主控已操作，但当前没有在线副控（本机/云/队内）")
            self.user_log(
                f"主控通知：{act} 已操作，但当前无在线副控",
                category=CAT_CONTROL,
            )
            return False

    def _on_task_sync_event(self, event: TaskSyncEvent) -> None:
        """Hub callback (any thread) → UI queue."""
        try:
            self._ui_q.put(("sync", event))
        except Exception:
            pass

    def _start_team_chat_tick(self) -> None:
        """Schedule the 队内控 slave receive loop (kept alive by _team_chat_tick)."""
        if getattr(self, "_team_chat_tick_job", None) is not None:
            return
        try:
            self._team_chat_tick_job = self.after(1500, self._team_chat_tick)
        except Exception:
            self._team_chat_tick_job = None

    def _stop_team_chat_tick(self) -> None:
        """Cancel the 队内控 receive loop. @author by ak"""
        job = getattr(self, "_team_chat_tick_job", None)
        self._team_chat_tick_job = None
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        try:
            self._team_chat_polling = False
        except Exception:
            pass

    def _team_chat_tick(self) -> None:
        """Periodic 队内控 receive: poll team channel, execute [主P] commands, reply pong."""
        self._team_chat_tick_job = None
        pid = int(self._fixed_pid or 0)
        role = self._control_role()
        try:
            team_enabled = team_control_flag_enabled(self.settings)
            if not pid or not team_enabled:
                return
            if role not in (ROLE_MASTER, ROLE_SLAVE):
                return
            if getattr(self, "_team_slaves", None) is None:
                self._team_slaves: dict[str, dict] = {}
            if getattr(self, "_team_chat_polling", False):
                return
            self._team_chat_polling = True
            try:
                seen = getattr(self, "_team_seen", None)
                if seen is None:
                    seen = self._team_seen = TeamMsgSeen()
                # 首次启动：游标直接跳到当前最新，跳过队伍频道历史（只认新消息）。
                if getattr(self, "_team_chat_cursor", None) is None:
                    try:
                        from app.core.chat_tap import ChatTapReader

                        _r = ChatTapReader.open(pid)
                        self._team_chat_cursor = (
                            int(_r.latest_team_cursor()) if _r else 0
                        )
                        if _r:
                            _r.close()
                    except Exception:
                        self._team_chat_cursor = 0
                cursor = int(getattr(self, "_team_chat_cursor", 0) or 0)
                events, consumed = read_team_events_pid(
                    pid,
                    cursor=cursor,
                    log=lambda m: self._push("log", m),
                )
                self._team_chat_cursor = consumed
                now_tick = None
                for msg in events:
                    if not seen.consume(msg):
                        # 重复消息静默跳过（同 msg_id 已处理）。
                        continue
                    # 时效过滤：普通队内指令只认 3 秒内消息。降龙状态回报
                    # 属于刚发起的收集会话，游戏聊天渲染可能延迟，匹配本次
                    # 查询号且仍在 8 秒收集窗口时允许接收。
                    is_active_jianglong_status = False
                    if role == ROLE_MASTER and str(msg.get("kind") or "") == "jianglong_status":
                        query_id = str(getattr(self, "_jianglong_team_query_msg_id", "") or "")
                        query_at = float(getattr(self, "_jianglong_query_at", 0.0) or 0.0)
                        is_active_jianglong_status = bool(
                            query_id
                            and str(msg.get("msg_id") or "") == query_id
                            and (time.time() - query_at) <= 8.5
                        )
                    if msg.get("tick_ms") and not is_active_jianglong_status:
                        if now_tick is None:
                            now_tick = _now_tick_ms()
                        if _msg_is_stale(int(msg.get("tick_ms")), now_ms=now_tick):
                            # 过期消息静默跳过（只认最新）。
                            continue
                    self._dispatch_team_message(msg, role=role)
                # 主控：刷新副控在线汇总（基于最近收到的 pong/done/fail）。
                if role == ROLE_MASTER:
                    self._refresh_team_slave_summary()
            except Exception as e:
                self._push("log", f"自动任务 [队内控] 读取失败: {e}")
            finally:
                self._team_chat_polling = False
        except Exception:
            pass
        finally:
            try:
                self._team_chat_tick_job = self.after(1500, self._team_chat_tick)
            except Exception:
                self._team_chat_tick_job = None

    def _record_team_slave_msg(self, sender: str, kind: str, msg: dict) -> None:
        """主控记录副控最近心跳/完成状态（按角色名）。@author by ak"""
        if not sender:
            return
        try:
            state = self._team_slaves.setdefault(sender, {})
        except Exception:
            return
        now = time.time()
        if kind == "pong":
            state["last_heartbeat"] = now
        else:
            state["last_action"] = now
            state["last_kind"] = kind
            state["last_action_verb"] = str(msg.get("action") or "") or None
            state["last_ok"] = msg.get("ok")
        state["last_seen"] = now

    def _refresh_team_slave_summary(self) -> None:
        """主控状态栏汇总：副控在控 N 个 + 各副控最近心跳/完成。@author by ak"""
        try:
            slaves = self._team_slaves
        except Exception:
            return
        if not slaves:
            return
        now = time.time()
        alive = [
            (name, st)
            for name, st in slaves.items()
            if (now - float(st.get("last_seen") or 0)) <= TEAM_ALIVE_WINDOW_S
        ]
        if not alive:
            return
        try:
            bits = []
            for name, st in sorted(alive):
                ago = max(0.0, now - float(st.get("last_seen") or now))
                verb = st.get("last_action_verb") or ""
                tail = f"{verb}{'✓' if st.get('last_ok') else '✗'}" if verb else ""
                bits.append(f"{name} {ago:.0f}s{tail}")
            summary = f"副控在控 {len(alive)} 个：" + "，".join(bits)
            var = getattr(self, "var_team_control_status", None)
            if var is not None:
                var.set(summary)
        except Exception:
            pass

    def _team_send_reply(self, action: str, ok: bool, detail: str = "") -> None:
        """副控向队伍频道发完成回执 [副G]<短动词>done|fail@id。@author by ak"""
        if not is_team_control_ready(self.settings):
            return
        try:
            reply = build_slave_pong(action, ok=bool(ok))
            res = send_team_message(
                int(self._fixed_pid or 0),
                reply,
                log=lambda m: self._push("log", m),
            )
            self._push(
                "log",
                f"自动任务 [队内控] 完成回执 {reply} "
                f"ok={res.get('ok')} {res.get('error') or ''}"
                + (f" detail={detail}" if detail else ""),
            )
        except Exception as e:
            self._push("log", f"自动任务 [队内控] 回执发送失败: {e}")

    def _dispatch_team_message(self, msg: dict, *, role: str) -> None:
        """
        Dispatch one parsed team-channel line.

        Slave: run [主P] commands (accept/path/complete/...), then reply
        [副G]<shortverb>done|fail after execution finishes. Master: observe
        [副G] pong/done/fail (heartbeat + completion) and log.
        @author by ak
        """
        kind = str(msg.get("kind") or "")
        m_role = str(msg.get("role") or "").lower()
        try:
            if role == ROLE_MASTER:
                if m_role == "slave" and kind == "jianglong_status":
                    sender = str(msg.get("sender") or "").strip()
                    if not sender:
                        self._push(
                            "log",
                            "降龙编排: 收到状态但无发送者，拒收 "
                            f"seq={msg.get('seq') or 0} raw={msg.get('raw') or ''}",
                        )
                        return
                    # 队内消息由 TaskPage 接收，但编排名单在 SettingsPage
                    # 冻结；必须写入同一份状态，不能只更新本任务页。
                    roster_page = self._sibling_page("settings")
                    roster_owner = (
                        roster_page
                        if roster_page is not None
                        and hasattr(roster_page, "_team_slaves")
                        else self
                    )
                    if getattr(roster_owner, "_team_slaves", None) is None:
                        roster_owner._team_slaves = {}
                    state = roster_owner._team_slaves.setdefault(sender, {})
                    state.update(
                        jianglong_enabled=bool(msg.get("enabled")),
                        jianglong_in_wuzun=bool(msg.get("in_wuzun")),
                        jianglong_hang_running=bool(msg.get("hang_running")),
                        jianglong_seen_at=time.time(),
                    )
                    self._push(
                        "log",
                        f"降龙编排: 已收集副控状态 {sender} "
                        f"enabled={int(bool(msg.get('enabled')))} "
                        f"scene={int(bool(msg.get('in_wuzun')))} "
                        f"hang={int(bool(msg.get('hang_running')))} "
                        f"msg_id={msg.get('msg_id') or '-'}",
                    )
                    return
                if m_role == "slave" and kind in ("pong", "done", "fail"):
                    sender = str(msg.get("sender") or "").strip()
                    self._record_team_slave_msg(sender, kind, msg)
                    self._push(
                        "log",
                        f"自动任务 [队内控] 收到副控{kind} "
                        f"{sender or '?'} {msg.get('raw') or ''}",
                    )
                return
            if role != ROLE_SLAVE:
                return
            if m_role != "master":
                return
            if kind == "ping":
                pong_text = build_slave_pong()  # [副G]PONG
                pid_local = int(self._fixed_pid or 0)
                try:
                    from app.core.team_chat import _send_ident_confirmed

                    if not pid_local or not _send_ident_confirmed(pid_local):
                        # 身份未就绪时发 PONG 会被服务端丢弃（错误身份），跳过本 tick，
                        # 1.5s 后 _team_chat_tick 重试（主控会重复 PING）。
                        self._push(
                            "log",
                            f"自动任务 [队内控] 收到 PING 但身份未就绪，暂不回 PONG "
                            f"(pid={pid_local})",
                        )
                        return
                except Exception:
                    pass
                _r = send_team_message(
                    pid_local,
                    pong_text,
                    log=lambda m: self._push("log", m),
                )
                self._push(
                    "log",
                    f"自动任务 [队内控] 收到 PING，已回 {pong_text} "
                    f"ok={_r.get('ok')} {_r.get('error') or ''}",
                )
                return
            if kind == "jianglong_query":
                pid_local = int(self._fixed_pid or 0)
                settings_page = self._sibling_page("settings")
                if settings_page is not None and hasattr(
                    settings_page, "_jianglong_presence"
                ):
                    # 与「挂机设置 → 刷新状态」同样读取 read_hang_live；不关心
                    # 挂机由谁开启，也不使用群控/页面缓存来代替本号实时状态。
                    # A team query is an explicit master notification.  It
                    # bypasses only the slave map gate; local Jianglong and
                    # live-hang checks remain mandatory.
                    info = settings_page._jianglong_presence(
                        force_hang_refresh=True, ignore_scene=True
                    )
                else:
                    info = {}
                live = dict(info.get("hang_live") or {})
                self._push(
                    "log",
                    "降龙编排: 副控参与读取 "
                    f"pid={pid_local} jl={int(bool(info.get('enabled')))} "
                    f"jl_char={dict(info.get('jianglong_config') or {}).get('char_id') or '-'} "
                    f"running={live.get('running')} mode={live.get('mode')} "
                    f"ok={live.get('ok')} error={live.get('error') or '-'}",
                )
                participate = bool(
                    info.get("enabled")
                    and info.get("in_wuzun")
                    and info.get("hang_running")
                )
                reasons: list[str] = []
                if not info.get("enabled"):
                    reasons.append("未开启降龙")
                if not info.get("hang_running"):
                    reasons.append("未开启挂机")
                if not participate:
                    reason = "、".join(reasons) or "参与条件不满足"
                    try:
                        self.user_log(
                            f"受控通知：降龙编排参与失败（{reason}）",
                            category=CAT_CONTROL,
                        )
                    except Exception:
                        pass
                    self._push(
                        "log",
                        f"降龙编排: 副控未参与，不回队伍消息 reason={reason} "
                        f"info={info}",
                    )
                    return
                reply = build_slave_jianglong_status(
                    True,
                    True,
                    True,
                    msg_id=str(msg.get("msg_id") or "") or None,
                )
                result = send_team_message(
                    pid_local, reply, log=lambda m: self._push("log", m)
                )
                try:
                    self.user_log(
                        "受控通知：降龙编排参与成功（已回报队伍）"
                        if result.get("ok")
                        else "受控通知：降龙编排参与失败（队伍回报失败）",
                        category=CAT_CONTROL,
                    )
                except Exception:
                    pass
                self._push(
                    "log",
                    f"降龙编排: 副控参与并回报 {reply} ok={result.get('ok')} "
                    f"info={info}",
                )
                return
            if kind != "command":
                return
            action = str(msg.get("action") or "")
            task_ids = list(msg.get("task_ids") or [])
            if not action:
                return
            self._push(
                "log",
                f"自动任务 [队内控] 收到主控命令 {msg.get('raw') or ''}",
            )
            if action == ACTION_ACCEPT and task_ids:
                for tid in task_ids:
                    self._run_accept(
                        int(tid),
                        from_sync=True,
                        on_done=lambda ok, d, a=action: self._team_send_reply(
                            a, ok, d
                        ),
                    )
            elif action == ACTION_COMPLETE and task_ids:
                for tid in task_ids:
                    self._run_complete(
                        {"task_id": int(tid), "can_finish": True},
                        from_sync=True,
                        on_done=lambda ok, d, a=action: self._team_send_reply(
                            a, ok, d
                        ),
                    )
            elif action == ACTION_PATH and task_ids:
                for tid in task_ids:
                    self._run_path(
                        {
                            "task_id": int(tid),
                            "can_finish": False,
                            "portal_kind": msg.get("portal_kind") or "npc",
                            "origin_scene_id": msg.get("origin_scene_id"),
                            "portal_tid": msg.get("portal_tid"),
                            "portal_obj_id": msg.get("portal_obj_id"),
                            "portal_x": msg.get("portal_x"),
                            "portal_y": msg.get("portal_y"),
                            "portal_z": msg.get("portal_z"),
                        },
                        from_sync=True,
                        prefer_portal=(str(msg.get("portal_kind") or "npc").strip().lower() != "npc"),
                        on_done=lambda ok, d, a=action: self._team_send_reply(
                            a, ok, d
                        ),
                    )
            elif action == ACTION_HANG_SYNC:
                # payload = 开/关
                self._run_team_hang_from_command(msg)
            elif action == ACTION_ACCEPT_DAILY_TASKS:
                self._run_daily_accept(
                    from_sync=True,
                    on_done=lambda ok, detail, a=action: self._team_send_reply(
                        a, ok, detail
                    ),
                )
            elif action == ACTION_DAILY_ROUTE:
                self._handle_sync_event(self._team_command_event(msg))
            elif action in (
                ACTION_CLAIM_ACTIVITY,
                ACTION_MAP_FLY,
                ACTION_TEAM_LEAVE,
                ACTION_TEAM_FOLLOW,
    ACTION_DAILY_FOLLOW,
                ACTION_JIANGLONG_CAST,
            ):
                # 复用群控/云控副控执行逻辑：构造 origin="team" 事件走 _handle_sync_event。
                self._handle_sync_event(self._team_command_event(msg))
            else:
                self._push(
                    "log",
                    f"自动任务 [队内控] 未知命令 {action} 已忽略",
                )
                return
            # 即时动作（misc/hang）就地回执完成。降龙必须等实际施法线程结束，
            # 否则未命中名单的角色也会误报 done。
            if action in (
                ACTION_CLAIM_ACTIVITY,
                ACTION_MAP_FLY,
                ACTION_TEAM_LEAVE,
                ACTION_TEAM_FOLLOW,
    ACTION_DAILY_FOLLOW,
                ACTION_HANG_SYNC,
            ):
                self._team_send_reply(action, ok=True)
        except Exception as e:
            self._push("log", f"自动任务 [队内控] 处理失败: {e}")

    def _team_command_event(self, msg: dict) -> dict:
        """把队内控解析的 [主P] 命令转成与群控/云控等价的同步事件 dict。

        origin="team"（_handle_sync_event 已支持队内控隔离分支）；action/task_id/
        name（extra）与主控 publish 一致，复用同一执行逻辑（含 map_fly 名字飞图）。
        @author by ak
        """
        action = str(msg.get("action") or "")
        task_ids = list(msg.get("task_ids") or [])
        tid = int(task_ids[0]) if task_ids else 0
        extra = str(msg.get("extra") or "").strip()
        points = None
        if action == ACTION_CLAIM_ACTIVITY and extra.startswith("points="):
            try:
                points = int(extra.split("=", 1)[1] or 0)
            except (TypeError, ValueError):
                points = None
        data = {
            "action": action,
            "task_id": tid,
            "source_pid": int(self._fixed_pid or 0),
            "can_finish": None,
            "name": extra,
            "origin": "team",
            "portal_kind": "",
            "origin_scene_id": None,
            "portal_tid": None,
            "portal_obj_id": None,
            "portal_x": None,
            "portal_y": None,
            "portal_z": None,
            "points": points,
            "hang_mode": None,
            "phase": "",
            "target_scene_id": None,
            "target_tid": None,
            "target_x": None,
            "target_y": None,
            "target_z": None,
            "target_round": None,
        }
        for key in (
            "phase",
            "portal_kind",
            "origin_scene_id",
            "portal_tid",
            "portal_obj_id",
            "portal_x",
            "portal_y",
            "portal_z",
            "target_scene_id",
            "target_tid",
            "target_x",
            "target_y",
            "target_z",
            "target_round",
        ):
            if msg.get(key) is not None:
                data[key] = msg.get(key)
        # 降龙：主控用 extra 携带目标角色名，作为名单过滤条件，只让目标副控施法。
        data["members"] = extra if action == ACTION_JIANGLONG_CAST else ""
        # map_fly：主控只带 task_id（预设）时，把预设 key 补到 name。
        if action == ACTION_MAP_FLY and not data["name"]:
            data["name"] = {1: "fuzhou", 2: "shimen", 3: "death"}.get(tid, "")
        return data

    def _run_team_hang_from_command(self, msg: dict) -> None:
        """队内控 挂机同步命令：payload 为 开/关。@author by ak"""
        text = str(msg.get("text") or "").strip().lower()
        command_body = text.split("]", 1)[1] if "]" in text else text
        state_text = command_body.rsplit(":", 1)[-1].split("@", 1)[0].strip()
        state_token = state_text.split("|", 1)[0].strip()
        desired_on = state_token in ("开", "on", "1", "true", "yes")
        temporary_mode = None
        marker = "|m="
        if marker in state_text:
            try:
                parsed_mode = int(state_text.split(marker, 1)[1].split()[0])
                if parsed_mode in (0, 1):
                    temporary_mode = parsed_mode
            except (TypeError, ValueError):
                pass
        try:
            self._run_hang_sync(
                desired_on=desired_on,
                from_sync=True,
                temporary_mode=temporary_mode,
            )
        except Exception as e:
            self._push("log", f"自动任务 [队内控] 挂机同步失败: {e}")

    def _run_daily_follow_sync(self, source_pid: int = 0) -> None:
        mounted = self._require_session()
        if mounted is None:
            self._push("log", "自动任务 [副控] 日常跟随失败：未挂载")
            return

        def worker() -> None:
            attach = None
            try:
                from app.core.grocery_auto import open_attach_session
                from app.core.team_ops import list_party_members, quick_team_follow
                attach = open_attach_session(int(mounted.pid), log=lambda m: self._push("log", m))
                party = list_party_members(
                    attach,
                    fresh=True,
                    log=lambda m: self._push("log", m),
                )
                target = next(
                    (
                        member
                        for member in party or []
                        if bool(member.get("is_leader"))
                        and not bool(member.get("is_self"))
                        and int(member.get("obj_id") or 0) > 0
                    ),
                    None,
                )
                if target is None:
                    raise RuntimeError("队伍中未读取到主控用户ID")
                target_id = int(target.get("obj_id") or 0)
                result = quick_team_follow(
                    attach,
                    target_id=target_id,
                    log=lambda m: self._push("log", m),
                )
                ok = bool(getattr(result, "ok", False))
                target_name = str(target.get("name") or "主控")
                self._push(
                    "log",
                    f"自动任务 [副控] 日常主动跟随主控 "
                    f"target={target_name}({target_id}) {'成功' if ok else '失败'}",
                )
            except Exception as exc:
                self._push("log", f"自动任务 [副控] 日常跟随失败: {exc}")
            finally:
                try:
                    if attach is not None:
                        attach.close()
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()
    def _skip_jianglong_for_auto_open(self) -> bool:
        """Return whether this PID is in the active open-monster send window."""
        try:
            from app.core.wuzun_open_monster import is_wuzun_open_monster_active

            return bool(is_wuzun_open_monster_active(int(self._fixed_pid or 0)))
        except Exception:
            return False

    def _run_team_misc_from_command(self, msg: dict) -> None:
        """队内控 其它动作命令：离队 / 领活跃 / 飞图 / 跟随 / 降龙。@author by ak"""
        action = str(msg.get("action") or "")
        try:
            if action == ACTION_TEAM_LEAVE:
                self._run_team_leave(from_sync=True, keep_leader_id=0)
            elif action == ACTION_CLAIM_ACTIVITY:
                page = self._sibling_page("activity")
                if page is not None and hasattr(page, "handle_claim_activity_sync"):
                    page.handle_claim_activity_sync(
                        source_pid=int(self._fixed_pid or 0),
                        points=None,
                        reason="队内控",
                    )
                else:
                    self._push("log", "自动任务 [队内控] 领活跃失败：无 ActivityPage")
            elif action == ACTION_MAP_FLY:
                text = str(msg.get("text") or "").strip()
                extra = str(msg.get("extra") or "").strip()
                # 自定义槽位：extra = slot:p:s:label（主控队内控携带 name）
                if extra.startswith("slot:"):
                    parts = extra.split(":", 3)
                    try:
                        page_i = int(parts[1]) & 0xFF if len(parts) > 1 else 0xFF
                        slot_i = int(parts[2]) if len(parts) > 2 else 0
                    except (TypeError, ValueError):
                        page_i, slot_i = 0xFF, 0
                    label_i = parts[3] if len(parts) > 3 else f"槽{slot_i + 1}"
                    self._push(
                        "log",
                        f"自动任务 [队内控] 飞图自定义 {label_i} (p{page_i}/s{slot_i})",
                    )
                    self._run_map_fly_slot(
                        page_i, slot_i, label=label_i, from_sync=True
                    )
                elif extra.lower() in ("fuzhou", "shimen", "death"):
                    self._run_map_fly(extra.lower(), from_sync=True)
                else:
                    key = ""
                    if ":" in text:
                        key = text.split(":", 1)[1].strip().lower()
                    if key in ("fuzhou", "shimen", "death"):
                        self._run_map_fly(key, from_sync=True)
                    else:
                        # 主控队内控只带 task_id（预设）：1→fuzhou / 2→shimen / 3→death
                        for tid in (msg.get("task_ids") or [])[:1]:
                            try:
                                key = {1: "fuzhou", 2: "shimen", 3: "death"}.get(
                                    int(tid), ""
                                )
                            except (TypeError, ValueError):
                                key = ""
                        if key:
                            self._push(
                                "log",
                                f"自动任务 [队内控] 飞图 task_id→{key} 执行",
                            )
                            self._run_map_fly(key, from_sync=True)
                        else:
                            self._push(
                                "log",
                                f"自动任务 [队内控] 飞图未知目标 {text!r} 已忽略",
                            )
            elif action == ACTION_TEAM_FOLLOW:
                self._push("log", "自动任务 [队内控] 跟随由队长执行" )
            elif action == ACTION_JIANGLONG_CAST:
                if self._skip_jianglong_for_auto_open():
                    return
                res = cast_jianglong_once(
                    int(self._fixed_pid or 0),
                    hwnd=0,
                    log=lambda m: self._push("log", m),
                )
                self._push(
                    "log",
                    f"自动任务 [队内控] 降龙 "
                    f"{'成功' if res.get('ok') else '失败'} "
                    + (
                        f"ret={res.get('ret')} sid=0x{int(res.get('skill_id') or 0):X} native"
                        if res.get("ok")
                        else str(res.get("error") or res.get("note") or "unknown error")
                    ),
                )
        except Exception as e:
            self._push("log", f"自动任务 [队内控] {action} 失败: {e}")

    def _handle_sync_event(self, event) -> None:
        role = self._control_role()
        if role != ROLE_SLAVE:
            self.log(
                f"自动任务 [副控] 忽略同步：本窗角色={role} "
                f"pid={int(self._fixed_pid or 0)}"
            )
            return
        data = event.to_dict() if hasattr(event, "to_dict") else dict(event or {})
        origin = str(data.get("origin") or "").strip().lower()
        # Cloud-enabled slave only executes cloud events (avoid local+cloud fights).
        if is_cloud_slave_isolated(self.settings) and origin != "cloud":
            self.log(
                f"自动任务 [副控] 云控隔离：忽略本机事件 "
                f"origin={origin or 'master'} action={data.get('action') or ''} "
                f"task_id={data.get('task_id') or 0}"
            )
            return
        # 队内控副控只执行队伍频道命令（hub 已退订，防御性兜底）。
        if is_team_slave_isolated(self.settings) and origin != "team":
            self.log(
                f"自动任务 [副控] 队内控隔离：忽略非队伍事件 "
                f"origin={origin or 'master'} action={data.get('action') or ''} "
                f"task_id={data.get('task_id') or 0}"
            )
            return
        action = str(data.get("action") or "").lower()
        if action == ACTION_JIANGLONG_QUERY:
            settings_page = self._sibling_page("settings")
            if settings_page is None or not hasattr(
                settings_page, "_jianglong_control_mode"
            ) or not hasattr(settings_page, "_report_jianglong_presence"):
                self.log("自动任务 [副控] 降龙状态页不可用，未报名")
                return
            if settings_page._jianglong_control_mode() != "local":
                self.log("自动任务 [副控] 忽略本地降龙查询：队内控优先")
                return
            if not local_jianglong_collection_active():
                self.log("自动任务 [副控] 降龙本地查询窗口已结束，忽略")
                return
            # Local group-control notification bypasses only the map gate.
            # The slave must still pass its own Jianglong and live-hang checks.
            info = settings_page._jianglong_presence(
                force_hang_refresh=True, ignore_scene=True
            )
            participate = bool(
                info.get("enabled")
                and info.get("hang_running")
            )
            if not participate:
                reasons: list[str] = []
                if not info.get("enabled"):
                    reasons.append("未开启降龙")
                if not info.get("hang_running"):
                    reasons.append("未开启挂机")
                reason = "、".join(reasons) or "参与条件不满足"
                self.log(
                    f"自动任务 [副控] 降龙本地查询参与失败 reason={reason} info={info}"
                )
                try:
                    self.user_log(
                        f"受控通知：降龙编排参与失败（{reason}）",
                        category=CAT_CONTROL,
                    )
                except Exception:
                    pass
                return
            settings_page._report_jianglong_presence(ignore_scene=True)
            try:
                self.user_log("受控通知：降龙编排参与成功", category=CAT_CONTROL)
            except Exception:
                pass
            self.log(
                f"自动任务 [副控] 收到主控{int(data.get('source_pid') or 0)} "
                "降龙本地参与查询，已报名"
            )
            return
        if action == ACTION_ACCEPT_DAILY_TASKS:
            src = int(data.get("source_pid") or 0)
            self.var_status.set(f"副控收到主控{src} 接日常任务")
            self.log(f"自动任务 [副控] 收到 master={src} accept_daily_tasks")
            self._run_daily_accept(from_sync=True)
            return
        if action == ACTION_DAILY_ROUTE:
            src = int(data.get("source_pid") or 0)
            phase = str(data.get("phase") or "portal").strip().lower()
            task_id = int(data.get("task_id") or 0)
            if phase == "portal":
                if str(data.get("portal_kind") or "").strip().lower() == "badao":
                    def run_badao_transfer(attach, mounted, stop_event) -> None:
                        from app.core.game_send import send_raw_packet
                        ok = bool(
                            send_raw_packet(
                                int(mounted.pid),
                                bytes.fromhex("23000A02000000000002"),
                                timeout_ms=3000,
                                log=lambda m: self._push("log", m),
                            )
                        )
                        self._push("log", f"自动任务 [副控传送] 霸刀传送 ok={ok}")

                    self.var_status.set(f"副控收到主控{src} 霸刀传送")
                    self._attach_job("副控霸刀传送", run_badao_transfer)
                    return
                row = {
                    "task_id": task_id,
                    "name": data.get("name") or "地宫传送",
                    "portal_kind": data.get("portal_kind") or "dungeon_deep",
                    "origin_scene_id": data.get("origin_scene_id"),
                    "portal_tid": data.get("portal_tid"),
                    "portal_obj_id": data.get("portal_obj_id"),
                    "portal_x": data.get("portal_x"),
                    "portal_y": data.get("portal_y"),
                    "portal_z": data.get("portal_z"),
                    "target_scene_id": data.get("target_scene_id"),
                    "portal_name": data.get("name") or "地宫传送",
                }
                self.var_status.set(f"副控收到主控{src} 日常传送")
                self._push("log", f"自动任务 [副控传送] task={task_id} 恢复本地寻路传送")
                self._run_path(row, from_sync=True, prefer_portal=True)
                return
            if phase == "portal_arrive":
                row = {
                    "task_id": task_id,
                    "name": data.get("name") or "日常传送点",
                    "portal_kind": data.get("portal_kind") or "dungeon",
                    "origin_scene_id": data.get("origin_scene_id"),
                    "portal_tid": data.get("portal_tid"),
                    "portal_obj_id": data.get("portal_obj_id"),
                    "portal_x": data.get("portal_x"),
                    "portal_y": data.get("portal_y"),
                    "portal_z": data.get("portal_z"),
                    "portal_name": data.get("portal_name") or "日常传送点",
                }
                self.var_status.set(f"副控收到主控{src} 日常传送点寻路")
                self._run_path(row, from_sync=True, prefer_portal=False)
                return
            if phase == "target":
                self.var_status.set(
                    f"副控收到主控{src} 目标 {task_id}，跟随主控，不独立寻路"
                    + (f" 第{data.get('target_round')}轮" if data.get('target_round') else "")
                )
                self._push(
                    "log",
                    f"自动任务 [副控目标] task={task_id} 跳过独立寻路，等待主控组队跟随",
                )
                return
        # Team roster filter: when members set, only listed characters act.
        members_s = str(data.get("members") or "").strip()
        if members_s and action in (
            ACTION_MAP_FLY,
            ACTION_TEAM_FOLLOW,
    ACTION_DAILY_FOLLOW,
            ACTION_TEAM_LEAVE,
            ACTION_HANG_SYNC,
            ACTION_JIANGLONG_CAST,
            "team_accept",
        ):
            if not self._slave_in_team_roster(members_s):
                self.log(
                    f"自动任务 [副控] 非队伍名单，忽略 {action} members={members_s!r}"
                )
                return
        # Activity claim: route to sibling ActivityPage (no task_id required).
        if action == ACTION_CLAIM_ACTIVITY:
            page = self._sibling_page("activity")
            if page is None or not hasattr(page, "handle_claim_activity_sync"):
                self.log("自动任务 [副控] 领箱同步失败：无 ActivityPage")
                return
            pts = data.get("points")
            try:
                pts_i = int(pts) if pts is not None else None
            except (TypeError, ValueError):
                pts_i = None
            page.handle_claim_activity_sync(
                source_pid=int(data.get("source_pid") or 0),
                points=pts_i,
                reason=str(data.get("name") or ""),
            )
            return
        # Map fly fixed presets (name=fuzhou/shimen/death, task_id=slot+1)
        if action == ACTION_MAP_FLY:
            # 地图飞行独立于任务 busy：仅冷却中会直接不生效
            raw_name = str(data.get("name") or "").strip()
            key = raw_name.lower()
            src = int(data.get("source_pid") or 0)
            # custom: slot:{page}:{slot}:{label}
            if key.startswith("slot:"):
                parts = raw_name.split(":", 3)
                try:
                    page_i = int(parts[1]) & 0xFF if len(parts) > 1 else 0xFF
                    slot_i = int(parts[2]) if len(parts) > 2 else 0
                except (TypeError, ValueError):
                    page_i, slot_i = 0xFF, 0
                label_i = parts[3] if len(parts) > 3 else f"槽{slot_i + 1}"
                self.var_status.set(
                    f"副控收到主控{src} 地图飞行 {label_i} (p{page_i}/s{slot_i})"
                )
                self.log(
                    f"自动任务 [副控] 收到 master={src} map_fly "
                    f"page={page_i} slot={slot_i} name={label_i!r}"
                )
                self.user_log(
                    f"受控通知：收到主控{src} 地图飞行「{label_i}」",
                    category=CAT_CONTROL,
                )
                self._run_map_fly_slot(
                    page_i, slot_i, label=label_i, from_sync=True
                )
                return
            if key not in ("fuzhou", "shimen", "death"):
                try:
                    tid = int(data.get("task_id") or 0)
                except (TypeError, ValueError):
                    tid = 0
                key = {1: "fuzhou", 2: "shimen", 3: "death"}.get(tid, "")
            if not key:
                self.log(
                    f"自动任务 [副控] map_fly 未知预设 name={data.get('name')!r} "
                    f"task_id={data.get('task_id')}"
                )
                return
            self.var_status.set(f"副控收到主控{src} 地图飞行 {key}")
            self.log(f"自动任务 [副控] 收到 master={src} map_fly preset={key}")
            # 防抖：同预设短时间重复同步只执行一次。多个同步回调可能在
            # 不同线程同时投递同一条命令，检查和写入必须保持原子性。
            try:
                now = time.time()
                dedupe_lock = getattr(self, "_sync_map_fly_lock", None)
                if dedupe_lock is None:
                    dedupe_lock = threading.Lock()
                    self._sync_map_fly_lock = dedupe_lock
                with dedupe_lock:
                    last_k = str(getattr(self, "_last_sync_map_fly_key", "") or "")
                    last_ts = float(getattr(self, "_last_sync_map_fly_ts", 0.0) or 0.0)
                    dedupe_s = float(getattr(self, "_sync_map_fly_dedupe_s", 4.0) or 4.0)
                    if last_k == key and (now - last_ts) < dedupe_s:
                        self.log(
                            f"自动任务 [副控] map_fly 防抖跳过 preset={key} "
                            f"dt={now - last_ts:.2f}s"
                        )
                        return
                    self._last_sync_map_fly_key = key
                    self._last_sync_map_fly_ts = now
            except Exception:
                pass
            self.user_log(
                f"受控通知：收到主控{src} 地图飞行「{key}」",
                category=CAT_CONTROL,
            )
            self._run_map_fly(key, from_sync=True)
            return
        # Team leave (解卡队) — follow is leader-side, no slave UI accept needed
        if action == ACTION_TEAM_LEAVE or action == "team_accept":
            src = int(data.get("source_pid") or 0)
            keep_raw = str(data.get("name") or "").strip()
            keep_leader = 0
            if keep_raw.startswith("keep_leader:"):
                try:
                    keep_leader = int(keep_raw.split(":", 1)[1] or 0)
                except Exception:
                    keep_leader = 0
            self.var_status.set(f"副控收到主控{src} 离队")
            self.log(
                f"自动任务 [副控] 收到 master={src} team_leave keep_leader={keep_leader}"
            )
            self.user_log(
                f"受控通知：收到主控{src} 离队（解除卡队）",
                category=CAT_CONTROL,
            )
            self._run_team_leave(from_sync=True, keep_leader_id=keep_leader)
            return
        if action == ACTION_TEAM_FOLLOW:
            src = int(data.get("source_pid") or 0)
            # Leader owns ordinary team-follow; daily route has its own action.
            self.log(f"自动任务 [副控] 收到 master={src} team_follow（跟随由队长执行）" )
            return
        if action == ACTION_DAILY_FOLLOW:
            src = int(data.get("source_pid") or 0)
            self.log(f"自动任务 [副控] 收到 master={src} daily_follow，主动跟随主控")
            self._run_daily_follow_sync(src)
            return
        if action == ACTION_HANG_SYNC:
            src = int(data.get("source_pid") or 0)
            want_raw = str(data.get("name") or "").strip().lower()
            desired_on = want_raw in ("1", "on", "open", "开", "true", "yes")
            # name=off/0/关 → close
            if want_raw in ("0", "off", "close", "关", "false", "no"):
                desired_on = False
            try:
                temporary_mode = (
                    int(data.get("hang_mode"))
                    if data.get("hang_mode") is not None
                    else None
                )
            except (TypeError, ValueError):
                temporary_mode = None
            if temporary_mode not in (0, 1):
                temporary_mode = None
            want_s = "开" if desired_on else "关"
            mode_s = ""
            if desired_on and temporary_mode in (0, 1):
                mode_s = " · " + ("副本模式" if temporary_mode == 1 else "普通模式")
            self.var_status.set(f"副控收到主控{src} 内挂→{want_s}{mode_s}")
            self.log(
                f"自动任务 [副控] 收到 master={src} hang_sync want={want_s} "
                f"temporary_mode={temporary_mode}"
            )
            self.user_log(
                f"受控通知：收到主控{src} 内挂同步→{want_s}{mode_s}",
                category=CAT_CONTROL,
            )
            self._run_hang_sync(
                desired_on=desired_on,
                from_sync=True,
                temporary_mode=temporary_mode,
            )
            return
        if action == ACTION_JIANGLONG_CAST:
            if self._skip_jianglong_for_auto_open():
                return
            src = int(data.get("source_pid") or 0)
            pid = int(self._fixed_pid or 0)
            self.var_status.set(f"副控收到主控{src} 降龙编排")
            self.log(
                f"自动任务 [副控] 收到 master={src} jianglong_cast pid={pid}"
            )
            self.user_log(
                f"受控通知：收到主控{src} 释放降龙",
                category=CAT_CONTROL,
            )

            def _cast() -> None:
                hwnd_i = 0
                try:
                    sess = self.selected_session()
                    hwnd_i = int(getattr(sess, "hwnd", 0) or 0)
                except Exception:
                    hwnd_i = 0
                res = cast_jianglong_once(
                    pid, hwnd=hwnd_i,
                    log=lambda m: self._push("log", m),
                )
                self._push(
                    "log",
                    f"降龙编排 [副控]: pid={pid} "
                    f"{'成功' if res.get('ok') else '失败'} "
                    + (
                        f"ret={res.get('ret')} sid=0x{int(res.get('skill_id') or 0):X} native"
                        if res.get("ok")
                        else str(res.get("error") or res.get("note") or "unknown error")
                    ),
                )
                if str(data.get("origin") or "").lower() == "team":
                    detail = (
                        f"ret={res.get('ret')} sid=0x{int(res.get('skill_id') or 0):X} native"
                        if res.get("ok")
                        else str(res.get("error") or res.get("note") or "unknown error")
                    )
                    self._team_send_reply(
                        ACTION_JIANGLONG_CAST, bool(res.get("ok")), detail
                    )

            threading.Thread(
                target=_cast, daemon=True, name=f"jianglong-slave-{pid}"
            ).start()
            return
        try:
            task_id = int(data.get("task_id") or 0)
        except (TypeError, ValueError):
            task_id = 0
        if not task_id or action not in (
            ACTION_ACCEPT,
            ACTION_COMPLETE,
            ACTION_PATH,
        ):
            return
        runner_busy = bool(self._runner is not None and self._runner.is_running())
        if self._busy or runner_busy:
            pending = self._pending_task_sync
            # Keep task commands ordered while bounding stale/untrusted input.
            if len(pending) >= 64:
                pending.pop(0)
            pending.append(event)
            self.var_status.set(
                f"副控忙碌，同步已排队 {action} #{task_id}（{len(pending)}）"
            )
            self.log(
                f"自动任务 [副控] busy queued {action} #{task_id} "
                f"pending={len(pending)} runner={runner_busy}"
            )
            return
        src = int(data.get("source_pid") or 0)
        self.var_task_id.set(str(task_id))
        self.var_status.set(f"副控收到主控{src} {action} #{task_id}")
        self.log(
            f"自动任务 [副控] 收到 master={src} {action} id={task_id} "
            f"name={data.get('name') or ''} portal={data.get('portal_kind') or ''}"
        )
        act = action_cn(action)
        nm = str(data.get("name") or "").strip()
        name_s = f"「{nm}」" if nm else ""
        self.user_log(
            f"受控通知：收到主控{src} {act}{name_s} #{task_id}",
            category=CAT_CONTROL,
        )
        if action == ACTION_ACCEPT:
            self._run_accept(task_id, from_sync=True)
        elif action == ACTION_PATH:
            sync_kind = str(data.get("portal_kind") or "").strip()
            row = {
                "task_id": task_id,
                "can_finish": data.get("can_finish"),
                "name": data.get("name") or "",
                "portal_kind": "" if sync_kind == "npc" else sync_kind,
                "origin_scene_id": data.get("origin_scene_id"),
                "portal_tid": data.get("portal_tid"),
                "portal_obj_id": data.get("portal_obj_id"),
                "portal_x": data.get("portal_x"),
                "portal_y": data.get("portal_y"),
                "portal_z": data.get("portal_z"),
                "portal_name": data.get("portal_name") or "地宫传送",
            }
            self._run_path(
                row,
                from_sync=True,
                prefer_portal=(sync_kind != "npc"),
            )
        else:
            row = {
                "task_id": task_id,
                "can_finish": data.get("can_finish"),
                "name": data.get("name") or "",
            }
            self._run_complete(row, from_sync=True)

    def _push(self, kind: str, payload=None) -> None:
        self._ui_q.put((kind, payload))

    def _drain_ui(self) -> None:
        try:
            while True:
                kind, payload = self._ui_q.get_nowait()
                if kind == "log":
                    self.log(str(payload or ""))
                elif kind == "status":
                    self.var_status.set(str(payload or ""))
                elif kind == "tasks":
                    self._apply_tasks(list(payload or []))
                elif kind == "nearby":
                    self._apply_nearby(list(payload or []))
                elif kind == "clues":
                    self._apply_clues(list(payload or []))
                elif kind == "event":
                    self._apply_event(payload)
                elif kind == "busy":
                    self._set_busy(bool(payload))
                elif kind == "sync":
                    self._handle_sync_event(payload)
                elif kind == "drop_stale_paths":
                    data = dict(payload or {})
                    self._drop_stale_task_paths(
                        int(data.get("origin_scene_id") or 0),
                        int(data.get("live_scene_id") or 0),
                    )
                elif kind == "fly_catalog":
                    # legacy full-catalog path (unused by fixed pages)
                    try:
                        if hasattr(self, "_apply_fly_catalog"):
                            self._apply_fly_catalog(list(payload or []))
                    except Exception as e:
                        self.log(f"自动任务 [地图飞行] 应用目录失败: {e}")
                elif kind == "fly_page_slots":
                    try:
                        data = dict(payload or {})
                        request_id = int(data.get("request_id") or 0)
                        if request_id != int(
                            getattr(self, "_fly_page_request_id", 0) or 0
                        ):
                            self.log(
                                "自动任务 [地图飞行] 丢弃过期页结果 "
                                f"request={request_id}"
                            )
                            continue
                        page = data.get("page")
                        self._apply_fly_page_slots(
                            dict(page) if isinstance(page, dict) else {}
                        )
                        ok = bool(data.get("ok"))
                        msg = str(data.get("message") or "")
                        self.var_status.set(msg if ok else f"读点失败: {msg}")
                    except Exception as e:
                        self.log(f"自动任务 [地图飞行] 应用页点失败: {e}")
                elif kind == "fly_page_done":
                    request_id = int(payload or 0)
                    if request_id != int(
                        getattr(self, "_fly_page_active_request_id", 0) or 0
                    ):
                        continue
                    self._fly_page_active_request_id = 0
                    self._fly_working = False
                    self._fly_page_loading = False
                    if bool(getattr(self, "_fly_page_requery_pending", False)):
                        self._fly_page_requery_pending = False
                        self._on_fly_page_selected()
                elif kind == "fly_cd":
                    try:
                        left = float(payload or 0.0)
                    except (TypeError, ValueError):
                        left = 0.0
                    if hasattr(self, "var_fly_cd"):
                        if left > 0.05:
                            self.var_fly_cd.set(f"冷却: {left:.1f}s")
                        else:
                            self.var_fly_cd.set("冷却: 就绪")
                elif kind == "team_status":
                    if hasattr(self, "var_team_status"):
                        self.var_team_status.set(str(payload or ""))
                elif kind == "team_party":
                    if hasattr(self, "var_team_party"):
                        self.var_team_party.set(str(payload or ""))
        except queue.Empty:
            pass
        self._schedule_ui_drain(120)

    def _on_list_tab(self, name: str) -> None:
        self._list_tab = str(name or "已接")
        if self._list_tab == "附近":
            self._paint_nearby_list()
        elif self._list_tab in ("计划", "自定义"):
            self._paint_custom_list()
        else:
            self._paint_accepted_list()

    def _load_schedule_profile_for_role(self, role_id: str) -> None:
        """Bind the role's persisted schedule profile into this window's settings. @author by ak"""
        rid = str(role_id or "").strip()
        if not rid:
            return
        loaded = str(self.settings.get(SETTING_SCHEDULE_LOADED_OWNER) or "").strip()
        if loaded and loaded == rid:
            return
        profile = load_role_schedule_profile(rid)
        bind_settings_to_profile(self.settings, profile)
        self.settings[SETTING_SCHEDULE_LOADED_OWNER] = rid
        try:
            self._paint_schedule_list()
            self._paint_custom_list()
        except Exception:
            pass
        self.log(f"自动任务 [计划] 已加载角色 {rid} 的计划配置")

    def _persist_schedule_profile(self, role_id: str) -> None:
        """Persist current settings as the role's schedule profile (captain owner). @author by ak"""
        rid = str(role_id or "").strip()
        if not rid:
            return
        self.settings[SETTING_SCHEDULE_OWNER] = rid
        profile = profile_from_settings(self.settings)
        profile["captain_id"] = rid
        save_role_schedule_profile(rid, profile)
        self.settings[SETTING_SCHEDULE_LOADED_OWNER] = rid
        self.log(f"自动任务 [计划] 已保存角色 {rid} 的计划配置（队长={rid}）")

    def _custom_defs_for_ui(self) -> list[dict]:
        """Custom definitions merged with this window's per-role configured ids. @author by ak"""
        cids = self.settings.get("schedule_custom_ids")
        return list_custom_definitions(cids if isinstance(cids, dict) else None)

    def _paint_custom_list(self) -> None:
        """Refresh the plan or direct-action tab. @author by ak"""
        if self._list_tab not in ("计划", "自定义"):
            return
        if self._list_tab == "自定义":
            defs = [
                {"definition_id": "accept_daily_tasks", "name": "接日常任务", "kind": "daily_accept"},
                *[d for d in self._custom_defs_for_ui() if str(d.get("kind") or "") == "routine"],
            ]
        else:
            defs = self._custom_defs_for_ui()
        try:
            self.task_list.delete(0, tk.END)
        except Exception:
            return
        if not defs:
            self.task_list.insert(tk.END, "  （目录为空）")
            return
        audits = getattr(self._runner, "audits", {}) if self._runner is not None else {}
        for defn in defs:
            definition_id = str(defn.get("definition_id") or "")
            if self._list_tab == "自定义":
                label = str(defn.get("name") or definition_id)
                status_txt = "可执行"
            else:
                label = custom_definition_label(defn)
                status_txt = str((audits.get(definition_id) or {}).get("label") or "")
                if not status_txt:
                    status_txt = CUSTOM_STATUS_UNCHEKED if defn.get("enabled") else ""
            self.task_list.insert(tk.END, f"[{status_txt}] {label}")
        self.var_custom_status.set("自定义 · 右键执行" if self._list_tab == "自定义" else f"计划 {len(defs)} 项 · 右键加入")

    def _custom_status_tick(self) -> None:
        """Periodically repaint custom tab statuses while running. @author by ak"""
        try:
            if self._list_tab == "计划":
                self._paint_custom_list()
            runner = self._runner
            if runner is not None:
                try:
                    self._sync_runner_buttons(runner)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self._custom_status_job = self.after(1000, self._custom_status_tick)
        except Exception:
            self._custom_status_job = None

    def _on_custom_right(self, evt=None) -> None:
        if self._list_tab not in ("计划", "自定义"):
            return
        try:
            if evt is not None:
                idx = self.task_list.nearest(evt.y)
                if idx >= 0:
                    self.task_list.selection_clear(0, tk.END)
                    self.task_list.selection_set(idx)
                    self.task_list.activate(idx)
            selected = self.task_list.curselection()
            idx = int(selected[0]) if selected else -1
        except Exception:
            return
        if self._list_tab == "自定义":
            defs = [{"definition_id": "accept_daily_tasks", "name": "接日常任务", "kind": "daily_accept"}, *[d for d in self._custom_defs_for_ui() if str(d.get("kind") or "") == "routine"]]
            if not (0 <= idx < len(defs)):
                return
            definition_id = str(defs[idx].get("definition_id") or "")
            menu = tk.Menu(self, tearoff=0)
            if definition_id == "accept_daily_tasks":
                menu.add_command(label="执行接日常任务", command=lambda: self._run_daily_accept(from_sync=False))
                menu.add_command(label="捕获当前账号接任务包", command=self._capture_daily_accept_packets)
            else:
                menu.add_command(label="执行日常", command=lambda d=definition_id: self._run_custom_routine_once(d))
            try:
                menu.tk_popup(evt.x_root if evt is not None else 0, evt.y_root if evt is not None else 0)
            finally:
                try:
                    menu.grab_release()
                except Exception:
                    pass
            return
        defs = self._custom_defs_for_ui()
        if not (0 <= idx < len(defs)):
            return
        defn = defs[idx]
        definition_id = str(defn.get("definition_id") or "")
        menu = tk.Menu(self, tearoff=0)
        if defn.get("enabled"):
            menu.add_command(label="加入计划任务", command=lambda: self._add_custom_to_schedule(definition_id))
        else:
            menu.add_command(label=f"加入计划任务（{custom_definition_label(defn)}）", state=tk.DISABLED)
        try:
            menu.tk_popup(evt.x_root if evt is not None else 0, evt.y_root if evt is not None else 0)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _try_persist_schedule(self) -> None:
        """Auto-save the schedule profile if a role is mounted. @author by ak"""
        try:
            role_id = self._current_role_id()
            if role_id:
                self._persist_schedule_profile(role_id)
        except Exception:
            pass

    def _on_save_plan(self) -> None:
        """Manually persist the current plan to disk. @author by ak"""
        try:
            role_id = self._current_role_id()
            if role_id:
                self._persist_schedule_profile(role_id)
                self.var_status.set(f"计划已保存（角色 {role_id}）")
                self.log(f"自动任务 [计划] 手动保存 role={role_id}")
                self.user_log("操作：手动保存计划", category=CAT_TASK, source="自动任务")
            else:
                self.var_status.set("未挂载，无法保存")
        except Exception as e:
            self.var_status.set(f"保存失败: {e}")
            self.log(f"自动任务 [计划] 保存失败: {e}")

    def _add_custom_to_schedule(self, definition_id: str) -> None:
        res = add_custom_to_queue(
            self.settings,
            definition_id,
            custom_ids=self.settings.get("schedule_custom_ids")
            if isinstance(self.settings.get("schedule_custom_ids"), dict)
            else None,
        )
        self._schedule_queue = list(res.get("queue") or [])
        self._paint_schedule_list()
        if res.get("ok"):
            item = res.get("item") or {}
            self.var_status.set(f"已加入计划: {queue_item_label(item)}")
            self.log(
                f"自动任务 [计划] 自定义加入 {item.get('definition_id')} "
                f"inst={item.get('instance_id')}"
            )
        else:
            self.var_status.set(str(res.get("error") or "加入失败"))
            self.log(f"自动任务 [计划] 自定义加入失败: {res.get('error')}")

    def _on_schedule_move(self, delta: int) -> None:
        """Move the selected queue item up/down. @author by ak"""
        idx = self._selected_schedule_index()
        if idx < 0:
            return
        self._schedule_queue = move_queue_index(self.settings, idx, int(delta))
        self._paint_schedule_list()
        try:
            count = len(self._schedule_queue)
            self.clue_list.selection_clear(0, tk.END)
            new_idx = max(0, min(count - 1, idx + int(delta)))
            if count:
                self.clue_list.selection_set(new_idx)
                self.clue_list.activate(new_idx)
                self.clue_list.see(new_idx)
        except Exception:
            pass
        self.log(f"自动任务 [计划] 移动 index={idx} delta={int(delta)}")

    def _set_busy(self, busy: bool) -> None:
        """
        Toggle one-shot job busy state; keep 取消 enabled while running.

        @author by ak
        """
        self._busy = bool(busy)
        state = tk.DISABLED if busy else tk.NORMAL
        for button in (
            self.btn_refresh,
            self.btn_path_npc,
            self.btn_path_portal,
            self.btn_accept,
            self.btn_complete,
        ):
            button.configure(state=state)
        # 地图飞行按钮不受任务 busy/loading 影响，仅冷却时点击无效
        # 执行计划 / 取消：busy 时允许点取消；runner 运行时由 _set_running 接管
        if self._runner is not None and self._runner.is_running():
            return
        if hasattr(self, "btn_start"):
            self.btn_start.configure(state=tk.DISABLED if busy else tk.NORMAL)
        if hasattr(self, "btn_stop"):
            self.btn_stop.configure(state=tk.NORMAL if busy else tk.DISABLED)
        if busy:
            self._publish_activity(True)
        elif not self._running:
            self._publish_activity(False)
        if not busy:
            self._schedule_pending_task_sync()

    def _sync_runner_buttons(self, runner) -> None:
        """
        Refresh start/pause/stop from the runner's live state.
        The execute button toggles between 执行计划 / 暂停 / 恢复.

        @author by ak
        """
        try:
            if runner is None:
                st = ""
                running = False
            else:
                st = str(getattr(runner, "state", "") or "")
                running = bool(runner.is_running()) and bool(
                    getattr(self, "_running", False)
                )
            if hasattr(self, "btn_start"):
                start_text = "执行计划"
                if running:
                    start_text = "恢复" if st in (
                        RUNNER_STATE_PAUSED,
                        RUNNER_STATE_BLOCKED,
                        RUNNER_STATE_PAUSE_PENDING,
                    ) else "暂停"
                self.btn_start.configure(state=tk.NORMAL, text=start_text)
            if hasattr(self, "btn_stop"):
                self.btn_stop.configure(state=tk.NORMAL if running else tk.DISABLED)
        except Exception:
            pass

    def _on_start_or_pause(self) -> None:
        """Start the plan, or pause/resume the active plan."""
        runner = self._runner
        if runner is not None and runner.is_running():
            self._on_pause_toggle()
            return
        self._on_start()

    def _on_pause_toggle(self) -> None:
        """Toggle pause/resume on the schedule runner. @author by ak"""
        runner = self._runner
        if runner is None:
            return
        try:
            st = str(getattr(runner, "state", "") or "")
            if st in (RUNNER_STATE_PAUSED, RUNNER_STATE_BLOCKED, RUNNER_STATE_PAUSE_PENDING):
                runner.resume()
                self.var_status.set("恢复中…")
            else:
                runner.pause()
                self.var_status.set("暂停中…（副本将在当前本结束后暂停）")
        except Exception as e:
            self.var_status.set(f"操作失败: {e}")
            self.log(f"自动任务 [暂停/恢复] err: {e}")

    def _schedule_pending_task_sync(self) -> None:
        """Run the next queued slave task command after current work settles."""
        if self._busy or (self._runner is not None and self._runner.is_running()):
            return
        if bool(getattr(self, "_pending_task_sync_scheduled", False)):
            return
        pending = getattr(self, "_pending_task_sync", None)
        if not pending:
            return
        event = pending.pop(0)
        self._pending_task_sync_scheduled = True

        def apply() -> None:
            self._pending_task_sync_scheduled = False
            if self._busy or (self._runner is not None and self._runner.is_running()):
                pending.insert(0, event)
                return
            self._handle_sync_event(event)

        try:
            self.after(0, apply)
        except Exception:
            apply()

    def _drop_stale_task_paths(
        self, origin_scene_id: int, live_scene_id: int = 0
    ) -> None:
        """Discard queued portal paths belonging to a scene already left."""
        origin = int(origin_scene_id or 0)
        if origin <= 0:
            return
        pending = getattr(self, "_pending_task_sync", None)
        if not pending:
            return
        kept: list[object] = []
        dropped = 0
        for event in pending:
            data = event.to_dict() if hasattr(event, "to_dict") else dict(event or {})
            try:
                event_origin = int(data.get("origin_scene_id") or 0)
            except (TypeError, ValueError):
                event_origin = 0
            if (
                str(data.get("action") or "").lower() == ACTION_PATH
                and event_origin == origin
            ):
                dropped += 1
            else:
                kept.append(event)
        pending[:] = kept
        if dropped:
            self.log(
                f"自动任务 [副控] 场景已变化，丢弃旧寻路 {dropped} 条 "
                f"origin={origin} live={int(live_scene_id or 0)}"
            )

    def _new_stop_event(self) -> threading.Event:
        """
        Create a fresh stop event for the next attach job.

        @author by ak
        """
        if self._work_stop is not None:
            self._work_stop.set()
        self._job_gen += 1
        self._work_stop = threading.Event()
        return self._work_stop

    def _attach_job(self, title: str, fn) -> None:
        """
        Run attach job on a worker; pass stop_event via self._work_stop.

        @author by ak
        """
        mounted = self._require_session()
        if mounted is None:
            return
        if self._busy or (self._runner is not None and self._runner.is_running()):
            self.var_status.set(f"忙碌中，请稍候再{title}")
            return
        stop_event = self._new_stop_event()
        job_gen = int(self._job_gen)
        self._push("busy", True)
        self._push("status", f"{title}…")

        def worker() -> None:
            attach = None
            try:
                if stop_event.is_set():
                    self._push("status", f"{title}已取消")
                    return
                attach = open_attach_session(
                    mounted.pid, log=lambda m: self._push("log", m)
                )
                if stop_event.is_set():
                    self._push("status", f"{title}已取消")
                    return
                fn(attach, mounted, stop_event)
            except Exception as e:
                if stop_event.is_set():
                    self._push("status", f"{title}已取消")
                    self._push("log", f"自动任务 [{title}] cancelled: {e}")
                else:
                    self._push("status", f"{title}失败: {e}")
                    self._push("log", f"自动任务 [{title}] {e}")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass
                # Ignore late completion from a cancelled/superseded job.
                if job_gen == int(self._job_gen):
                    self._push("busy", False)

        threading.Thread(target=worker, daemon=True).start()

    def _selected_task(self) -> dict | None:
        if self._list_tab == "计划":
            return None
        try:
            selected = self.task_list.curselection()
            idx = int(selected[0]) if selected else -1
            if self._list_tab == "附近":
                return self._nearby[idx] if 0 <= idx < len(self._nearby) else None
            return self._tasks[idx] if 0 <= idx < len(self._tasks) else None
        except Exception:
            return None

    def _selected_clue(self) -> dict | None:
        # Right list is 计划任务 now; never map selection to path clues.
        # Nearby pathfind keeps coords in the local job, not this list.
        if len(getattr(self, "_clues", []) or []) == 1:
            c = self._clues[0]
            if c.get("x") is not None and c.get("z") is not None:
                return c
        return None

    def _paint_accepted_list(self) -> None:
        self.task_list.delete(0, tk.END)
        if not self._tasks:
            self.task_list.insert(tk.END, "  （暂无已接）")
            if self._list_tab == "已接":
                self.var_status.set("已接 0 条")
            return
        for row in self._tasks:
            status = row.get("status_text") or "进行中"
            tid = int(row.get("task_id") or 0)
            name = (
                str(row.get("name") or "").strip()
                or format_task_display_name("", task_id=tid)
                or f"任务{tid}"
            )
            # Accepted list: task title only (in-game style), no NPC prefix.
            label = f"[{status}] {name}"
            self.task_list.insert(tk.END, label)
        if self._list_tab == "已接":
            done = sum(1 for row in self._tasks if row.get("can_finish"))
            self.var_status.set(f"已接 {len(self._tasks)} · 可交 {done}")

    def _paint_nearby_list(self) -> None:
        self.task_list.delete(0, tk.END)
        if not self._nearby:
            self.task_list.insert(tk.END, "  （附近暂无可接 / 关联任务）")
            if self._list_tab == "附近":
                self.var_status.set("附近 0 条 · 点刷新扫描附近NPC")
            return
        for row in self._nearby:
            st = row.get("status_text") or "附近"
            tid = int(row.get("task_id") or 0)
            # Same panel form as 已接: <分类>具体名
            name = str(row.get("name") or "").strip()
            if not name and tid:
                name = f"任务{tid}"
            try:
                dist = float(row.get("dist"))
                dist_s = f" {dist:.1f}m"
            except (TypeError, ValueError):
                dist_s = ""
            if tid:
                label = f"[{st}] {name}{dist_s}"
            else:
                npc = str(row.get("npc_name") or "").strip()
                if is_placeholder_npc_name(npc):
                    ntid = int(row.get("npc_tid") or 0)
                    npc = f"NPC{ntid}" if ntid else "NPC"
                label = f"[{st}] {npc}{dist_s}"
            self.task_list.insert(tk.END, label)
        if self._list_tab == "附近":
            n_offer = sum(1 for r in self._nearby if r.get("kind") == "available")
            n_link = sum(1 for r in self._nearby if r.get("kind") == "accepted_link")
            self.var_status.set(
                f"附近 {len(self._nearby)} · 可接 {n_offer} · 已关联 {n_link}"
            )

    def _apply_tasks(self, rows: list) -> None:
        merged: list[dict] = []
        # Preserve last known real NPC name when refresh returns placeholder.
        prev_by_id = {
            int(r.get("task_id") or 0): r for r in (self._tasks or []) if r.get("task_id")
        }
        for raw in rows or []:
            row = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
            tid = int(row.get("task_id") or 0)
            # Keep panel compose from GetTaskTexts: <分类>具体名 / [级]<分类>具体名.
            # Do not re-run format_task_display_name (it would drop the <分类>).
            raw_name = str(row.get("name") or "").strip()
            if not raw_name:
                row["name"] = format_task_display_name("", task_id=tid)
            else:
                row["name"] = raw_name
            npc = str(row.get("npc_name") or "").strip()
            if is_placeholder_npc_name(npc) and tid in prev_by_id:
                old = str(prev_by_id[tid].get("npc_name") or "").strip()
                if old and not is_placeholder_npc_name(old):
                    row["npc_name"] = old
                    if row.get("dist") is None and prev_by_id[tid].get("dist") is not None:
                        row["dist"] = prev_by_id[tid].get("dist")
            elif is_placeholder_npc_name(npc):
                row["npc_name"] = ""
            merged.append(row)
        self._tasks = merged
        self._tasks.sort(
            key=lambda r: (
                0 if r.get("can_finish") else 1,
                int(r.get("task_id") or 0),
            )
        )
        if self._list_tab != "附近":
            self._paint_accepted_list()
        else:
            done = sum(1 for row in self._tasks if row.get("can_finish"))
            # keep nearby paint; only update status if still on accepted
            if self._list_tab == "已接":
                self.var_status.set(f"已接 {len(self._tasks)} · 可交 {done}")

    def _apply_nearby(self, rows: list) -> None:
        self._nearby = [dict(r) for r in (rows or [])]
        if self._list_tab == "附近":
            self._paint_nearby_list()
        else:
            n_offer = sum(1 for r in self._nearby if r.get("kind") == "available")
            self.var_status.set(
                f"附近扫描完成 {len(self._nearby)} 条 · 可接 {n_offer}（可切到附近）"
            )

    def _apply_clues(self, rows: list) -> None:
        # Legacy clue panel replaced by 计划任务 queue; keep data only.
        self._clues = [dict(row) for row in rows or []]

    def _paint_schedule_list(self) -> None:
        """Refresh right-side schedule queue listbox. @author by ak"""
        self._schedule_queue = load_schedule_queue(self.settings)
        try:
            self.clue_list.delete(0, tk.END)
        except Exception:
            return
        if not self._schedule_queue:
            self.clue_list.insert(tk.END, "  （计划任务空 · 右键已接/计划加入）")
            if hasattr(self, "var_schedule_status"):
                self.var_schedule_status.set("队列为空")
            return
        for item in self._schedule_queue:
            self.clue_list.insert(tk.END, queue_item_label(item))
        if hasattr(self, "var_schedule_status"):
            self.var_schedule_status.set(f"队列 {len(self._schedule_queue)} 项 · 右键排序/删除")

    def _selected_schedule_index(self) -> int:
        try:
            selected = self.clue_list.curselection()
            return int(selected[0]) if selected else -1
        except Exception:
            return -1

    def _on_schedule_delete(self, _evt=None) -> None:
        idx = self._selected_schedule_index()
        if idx < 0:
            return
        self._schedule_queue = remove_queue_index(self.settings, idx)
        self._paint_schedule_list()
        self.var_status.set(f"已删除计划任务 · 剩余 {len(self._schedule_queue)}")
        self.log(f"自动任务 [计划] 删除 index={idx}")

    def _on_schedule_right(self, evt=None) -> None:
        try:
            if evt is not None:
                idx = self.clue_list.nearest(evt.y)
                if idx >= 0:
                    self.clue_list.selection_clear(0, tk.END)
                    self.clue_list.selection_set(idx)
                    self.clue_list.activate(idx)
        except Exception:
            pass
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label="上移", command=lambda: self._on_schedule_move(-1)
        )
        menu.add_command(
            label="下移", command=lambda: self._on_schedule_move(1)
        )
        menu.add_separator()
        menu.add_command(label="删除", command=self._on_schedule_delete)
        try:
            menu.tk_popup(evt.x_root, evt.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _on_task_right(self, evt=None) -> None:
        if self._list_tab in ("计划", "自定义"):
            self._on_custom_right(evt)
            return
        if self._list_tab != "已接":
            return
        try:
            if evt is not None:
                idx = self.task_list.nearest(evt.y)
                if idx >= 0:
                    self.task_list.selection_clear(0, tk.END)
                    self.task_list.selection_set(idx)
                    self.task_list.activate(idx)
        except Exception:
            pass
        row = self._selected_task()
        if row is None or not int(row.get("task_id") or 0):
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label="加入计划任务",
            command=lambda r=dict(row): self._add_task_to_schedule(r),
        )
        try:
            menu.tk_popup(evt.x_root, evt.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _add_task_to_schedule(self, row: dict) -> None:
        res = try_add_task_to_queue(self.settings, row)
        self._schedule_queue = list(res.get("queue") or [])
        self._paint_schedule_list()
        if res.get("ok"):
            item = res.get("item") or {}
            self.var_status.set(f"已加入计划: {queue_item_label(item)}")
            self.log(
                f"自动任务 [计划] 加入 #{item.get('task_id')} "
                f"inst={item.get('instance_id')}"
            )
        else:
            err = str(res.get("error") or "加入失败")
            self.var_status.set(err)
            self.log(f"自动任务 [计划] 加入失败: {err}")

    def _activity_page_busy(self) -> bool:
        try:
            page = self._sibling_page("activity")
            if page is None:
                return False
            runner = getattr(page, "_runner", None)
            return bool(runner is not None and runner.is_running())
        except Exception:
            return False

    def _schedule_tick(self) -> None:
        """Daily HH:MM fire once -> start schedule advance (per-window). @author by ak"""
        try:
            if not (
                self._busy
                or (self._runner is not None and self._runner.is_running())
            ):
                role_id = self._current_role_id()
                if role_id:
                    self._load_schedule_profile_for_role(role_id)
                if schedule_profile_should_fire(
                    self._schedule_profile_snapshot(), now=None
                ):
                    q = load_schedule_queue(self.settings)
                    if q:
                        mark_schedule_fired(self.settings)
                        role_id = str(
                            self.settings.get(SETTING_SCHEDULE_OWNER) or ""
                        ).strip()
                        if role_id:
                            self._persist_schedule_profile(role_id)
                        self.log(
                            f"自动任务 [计划] 到点启动 队列={len(q)} "
                            f"hm={get_schedule_hm(self.settings)}"
                        )
                        try:
                            h, m = get_schedule_hm(self.settings)
                            self.user_log(
                                f"定时到点启动 · {h:02d}:{m:02d} · 队列 {len(q)} 项",
                                category=CAT_TASK,
                                source="自动任务",
                            )
                        except Exception:
                            pass
                        self._on_start(from_schedule=True)
        except Exception as e:
            try:
                self.log(f"自动任务 [计划] tick err: {e}")
            except Exception:
                pass
        try:
            self._schedule_tick_job = self.after(20000, self._schedule_tick)
        except Exception:
            self._schedule_tick_job = None

    def _schedule_profile_snapshot(self) -> dict:
        """In-memory profile snapshot for fire checking. @author by ak"""
        return profile_from_settings(self.settings)

    def _nearby_clue_from_row(self, row: dict) -> dict:
        """Build a pathfind clue from a nearby-list row (live NPC instance)."""
        return {
            "clue": "附近NPC",
            "kind": "npc",
            "name": row.get("npc_name")
            or row.get("name")
            or f"tid{row.get('npc_tid')}",
            "x": row.get("x"),
            "y": row.get("y"),
            "z": row.get("z"),
            "tid": row.get("npc_tid"),
            "ptr": row.get("ptr"),
            "obj_id": row.get("obj_id"),
            "dist": row.get("dist"),
            "source": "nearby_npc",
        }

    def _pathfind_nearby_row(self, row: dict) -> None:
        """Double-click on 附近任务 → pathfind to that NPC instance."""
        if row is None:
            return
        tid = int(row.get("task_id") or 0)
        if tid:
            self.var_task_id.set(str(tid))
        clue = self._nearby_clue_from_row(row)
        self._apply_clues([clue])
        if clue.get("x") is None or clue.get("z") is None:
            # Fall back to task NPC path when live pos missing.
            if tid:
                self._on_path_npc()
            else:
                self.var_status.set("该NPC无坐标，无法寻路")
            return

        def run(attach, mounted, stop_event) -> None:
            result = pathfind_to_clue(
                attach,
                clue,
                hwnd=int(getattr(mounted, "hwnd", 0) or 0),
                stop_event=stop_event,
                log=lambda m: self._push("log", m),
            )
            label = clue.get("name") or f"tid{clue.get('tid')}"
            if stop_event is not None and stop_event.is_set():
                self._push("status", "附近寻路已取消")
                return
            msg = (
                f"已到达 → {label}"
                if result.get("ok")
                else f"寻路失败: {result.get('error') or result.get('note')}"
            )
            self._push("status", msg)

        self._attach_job("附近寻路", run)

    def _on_task_select(self, _evt=None) -> None:
        """Select only fills task id; pathfind is double-click only."""
        row = self._selected_task()
        if row is None:
            return
        tid = int(row.get("task_id") or 0)
        if tid:
            self.var_task_id.set(str(tid))

    def _on_task_double(self, _evt=None) -> None:
        if self._list_tab == "计划":
            defs = self._custom_defs_for_ui()
            try:
                selected = self.task_list.curselection()
                idx = int(selected[0]) if selected else -1
            except Exception:
                idx = -1
            if 0 <= idx < len(defs):
                self._add_custom_to_schedule(str(defs[idx].get("definition_id") or ""))
            return
        row = self._selected_task()
        if row is None:
            return
        if self._list_tab == "附近":
            self._pathfind_nearby_row(row)
            return
        # 已接任务(控)双击 → 寻路到任务 NPC / 目标点
        self._on_path_npc()

    def _light_accepted_rows(self, attach, prev=None) -> list[dict]:
        """
        已接任务内存只读刷新：不扫列表、不强制 CanFinish。

        任务状态位随游戏进度自动更新；名称/可交复用上次缓存，仅首次出现的新任务
        才调用一次 GetTaskName。
        @author by ak
        """
        prev_by_id: dict[int, dict] = {}
        for p in prev or []:
            d = p.to_dict() if hasattr(p, "to_dict") else dict(p or {})
            try:
                tid = int(d.get("task_id") or 0)
            except (TypeError, ValueError):
                tid = 0
            if tid:
                prev_by_id[tid] = d
        rows = list_accepted_tasks(
            attach,
            log=lambda m: self._push("log", m),
            resolve_names=False,
            resolve_can_finish=False,
            quiet=True,
        )
        out: list[dict] = []
        for t in rows:
            old = prev_by_id.get(int(t.task_id))
            if old and old.get("name"):
                t.name = str(old["name"])
            elif not t.name:
                try:
                    nm = get_task_name(attach, int(t.task_id), log=lambda _m: None)
                    if nm:
                        t.name = nm
                except Exception:
                    pass
            if old and old.get("can_finish"):
                t.can_finish = True
            t.status_text = task_status_from_state(
                t.state, t.progress, can_finish=(t.can_finish or None)
            )
            if t.status_text == "可交" and not t.can_finish:
                t.can_finish = True  # 双位启发式判定可交，同步 can_finish 以免交付按钮拒绝
            out.append(t.to_dict())
        return out

    def _on_refresh(self) -> None:
        tab = self._list_tab
        if tab == "计划":
            self._paint_custom_list()
            return
        if tab == "自定义":
            self._paint_custom_list()
            return
        prev = list(self._tasks)

        def run(attach, _mounted, stop_event) -> None:
            if stop_event.is_set():
                return
            # 已接：内存只读（状态位自动随进度更新，不强制 CanFinish）
            light = self._light_accepted_rows(attach, prev)
            if stop_event.is_set():
                return
            self._push("tasks", light)
            if tab == "附近":
                # 未接：附近 NPC 内存实体 + 静态任务→NPC 探测缓存，按距离排序。
                # 探测缓存命中后为纯内存过滤，不再起 60 个远程调用。
                rows = list_nearby_offer_tasks(
                    attach,
                    radius=15.0,
                    probe_offers=True,
                    max_scan=60,
                    accepted_prev=prev,
                    log=lambda m: self._push("log", m),
                )
                if stop_event.is_set():
                    return
                self._push("nearby", rows)

        title = "刷新附近任务" if tab == "附近" else "刷新已接(控)"
        try:
            self.user_log(f"操作：{title}", category=CAT_TASK, source="自动任务")
        except Exception:
            pass
        self._attach_job(title, run)

    def _on_path_npc(self, _evt=None) -> None:
        """
        Path to task NPC / objective point (no portal shortcut).

        Prefer selected clue; else walk HostMove route for selected task.
        @author by ak
        """
        clue = self._selected_clue()
        row = self._selected_task()
        if clue is not None and clue.get("x") is not None and clue.get("z") is not None:

            def run_clue(attach, mounted, stop_event) -> None:
                result = pathfind_to_clue(
                    attach,
                    clue,
                    hwnd=int(getattr(mounted, "hwnd", 0) or 0),
                    stop_event=stop_event,
                    log=lambda m: self._push("log", m),
                )
                if stop_event is not None and stop_event.is_set():
                    self._push("status", "寻路NPC已取消")
                    return
                msg = (
                    f"已到达 → {clue.get('name') or clue.get('clue')}"
                    if result.get("ok")
                    else f"寻路失败: {result.get('error') or result.get('note')}"
                )
                self._push("status", msg)
                try:
                    self.user_log(
                        f"操作：寻路NPC · {msg}",
                        category=CAT_TASK,
                        source="自动任务",
                        dedupe_s=0.3,
                    )
                except Exception:
                    pass

            self._attach_job("寻路NPC", run_clue)
            return

        if row is None or not int(row.get("task_id") or 0):
            self.var_status.set("请先选择任务或线索")
            return
        self._run_path(row, from_sync=False, prefer_portal=False)

    def _on_path_portal(self, _evt=None) -> None:
        """
        Path via dungeon / 宋瑶 portal NPC, then open transfer dialog.

        Nearby tab: walk to selected portal NPC and run SayHello transfer.
        Accepted tab: portal shortcut for dungeon / 天下会 tasks.
        @author by ak
        """
        row = self._selected_task()
        if self._list_tab == "附近" and row is not None:
            self._portal_nearby_row(row)
            return
        if row is None or not int(row.get("task_id") or 0):
            self.var_status.set("请先选择已接")
            return
        self._run_path(row, from_sync=False, prefer_portal=True)

    def _portal_nearby_row(self, row: dict) -> None:
        """
        Walk to nearby portal NPC and open transfer (地宫传送 / 宋瑶).

        @author by ak
        """
        if row is None:
            return
        tid = int(row.get("task_id") or 0)
        if tid:
            self.var_task_id.set(str(tid))
        portal = {
            "name": row.get("npc_name") or row.get("name") or "传送NPC",
            "npc_name": row.get("npc_name") or "",
            "tid": row.get("npc_tid") or row.get("tid"),
            "npc_tid": row.get("npc_tid"),
            "ptr": row.get("ptr"),
            "obj_id": row.get("obj_id"),
            "x": row.get("x"),
            "y": row.get("y"),
            "z": row.get("z"),
            "dist": row.get("dist"),
        }
        self._apply_clues([self._nearby_clue_from_row(row)])
        if not int(portal.get("obj_id") or 0):
            self.var_status.set("该NPC无 obj_id，无法打开传送对话")
            return

        def run(attach, mounted, stop_event) -> None:
            # Prefer task classification when linked; else default 上层.
            kind = ""
            try:
                if tid:
                    kind = classify_task_portal_kind(row) or ""
            except Exception:
                kind = ""
            if not kind:
                nm = str(portal.get("name") or "")
                if any(k in nm for k in ("深处", "下层", "BOSS", "boss")):
                    kind = "dungeon_deep"
                elif "宋瑶" in nm or "天下会" in nm:
                    kind = "tianxiahui"
                else:
                    kind = "dungeon_upper"
            result = use_task_portal_npc(
                attach,
                portal,
                hwnd=int(getattr(mounted, "hwnd", 0) or 0),
                portal_kind=kind,
                stop_event=stop_event,
                log=lambda m: self._push("log", m),
            )
            if stop_event is not None and stop_event.is_set():
                self._push("status", "寻路地宫点已取消")
                return
            name = portal.get("name") or "传送NPC"
            if result.get("ok") and not result.get("partial"):
                note = result.get("note") or name
                self._push("status", f"传送捷径: {note}")
                try:
                    self.user_log(
                        f"操作：寻路地宫点 · 传送捷径 {note}",
                        category=CAT_TASK,
                        source="自动任务",
                    )
                except Exception:
                    pass
            else:
                err = result.get("error") or result.get("note") or "传送未完成"
                self._push("status", f"寻路地宫点: {err}")
                try:
                    self.user_log(
                        f"操作：寻路地宫点失败 · {err}",
                        category=CAT_TASK,
                        source="自动任务",
                    )
                except Exception:
                    pass

        self._attach_job("寻路地宫点", run)

    def _run_path(
        self,
        row: dict,
        *,
        from_sync: bool = False,
        prefer_portal: bool = True,
        on_done: Callable[[bool, str], None] | None = None,
    ) -> None:
        """Portal-aware or pure NPC path for accepted task; master syncs to slaves."""
        portal_guard = bool(
            prefer_portal
            and (from_sync or self._control_role() == ROLE_MASTER)
        )

        def run(attach, mounted, stop_event) -> None:
            local_row = dict(row or {})
            if (not from_sync or not prefer_portal) and (not local_row.get("name") or not local_row.get("portal_kind")):
                try:
                    local_tasks = list_accepted_tasks(
                        attach,
                        log=lambda _m: None,
                        resolve_names=True,
                        resolve_can_finish=False,
                        quiet=True,
                    )
                    for task_row in local_tasks or []:
                        task_data = task_row.to_dict() if hasattr(task_row, "to_dict") else dict(task_row)
                        if int(task_data.get("task_id") or 0) == int(local_row.get("task_id") or 0):
                            local_row = {**task_data, **local_row}
                            break
                except Exception:
                    pass
            result = pathfind_task(
                attach,
                local_row,
                hwnd=int(getattr(mounted, "hwnd", 0) or 0),
                log=lambda m: self._push("log", m),
                prefer_portal=bool(prefer_portal),
                portal_move_guard=portal_guard,
                portal_sync_guard=False,
                force_portal_route=bool(from_sync and prefer_portal),
                portal_origin_scene_id=int(row.get("origin_scene_id") or 0),
                sync_route_guard=bool(from_sync and not prefer_portal),
                stop_event=stop_event,
            )
            if stop_event is not None and stop_event.is_set():
                tag = "副控寻路" if from_sync else ("寻路地宫点" if prefer_portal else "寻路NPC")
                self._push("status", f"{tag}已取消")
                if on_done is not None:
                    on_done(False, "cancelled")
                return
            method = str(result.get("method") or "")
            kind = str(result.get("portal_kind") or classify_task_portal_kind(row))
            if result.get("ok"):
                if method == "portal_npc" or result.get("portal_name"):
                    note = result.get("note") or result.get("portal_name") or "传送NPC"
                    msg = f"传送捷径: {note}"
                else:
                    msg = f"已到达任务目标 → {row.get('name') or row.get('task_id')}"
            else:
                msg = f"寻路失败: {result.get('error') or result.get('note')}"
            if prefer_portal:
                tag = "副控寻路地宫点" if from_sync else "寻路地宫点"
            else:
                tag = "副控寻路NPC" if from_sync else "寻路NPC"
            self._push("status", msg)
            self._push("log", f"自动任务 [{tag}] ok={result.get('ok')} {msg}")
            origin_scene = int(
                result.get("origin_scene_id")
                or result.get("before_scene")
                or row.get("origin_scene_id")
                or 0
            )
            live_scene = int(result.get("after_scene") or 0)
            if from_sync and (
                result.get("safe_skip")
                or result.get("already_in_target_scene")
                or result.get("stale_scene_generation")
                or result.get("scene_transition")
                or (origin_scene > 0 and live_scene > 0 and live_scene != origin_scene)
            ):
                self._push(
                    "drop_stale_paths",
                    {
                        "origin_scene_id": origin_scene,
                        "live_scene_id": live_scene,
                    },
                )
            if not from_sync:
                try:
                    tid = int(row.get("task_id") or 0)
                    self.user_log(
                        f"操作：{tag} #{tid} · {msg}",
                        category=CAT_TASK,
                        source="自动任务",
                        dedupe_s=0.3,
                    )
                except Exception:
                    pass
            route_snapshot = dict(result.get("route") or {})
            route_snapshot_ready = bool(
                route_snapshot.get("x") is not None
                and route_snapshot.get("z") is not None
            )
            if route_snapshot_ready and not result.get("ok") and not from_sync:
                self._push(
                    "log",
                    "自动任务 [同步] 主控寻路未到位，但路线已解析，发布路线快照供副控继续执行",
                )
            if (result.get("ok") or route_snapshot_ready) and not from_sync:
                self._publish_task_sync(
                    ACTION_PATH,
                    int(row.get("task_id") or 0),
                    can_finish=row.get("can_finish"),
                    name=str(row.get("name") or ""),
                    portal_kind=kind if prefer_portal else "npc",
                    origin_scene_id=(
                        (origin_scene or None)
                        if prefer_portal
                        else route_snapshot.get("scene_id")
                    ),
                    portal_tid=(
                        result.get("portal_tid")
                        if prefer_portal
                        else route_snapshot.get("tid")
                    ),
                    portal_obj_id=(
                        result.get("obj_id")
                        if prefer_portal
                        else route_snapshot.get("obj_id")
                    ),
                    portal_x=(
                        result.get("portal_x")
                        if prefer_portal
                        else route_snapshot.get("x")
                    ),
                    portal_y=(
                        result.get("portal_y")
                        if prefer_portal
                        else route_snapshot.get("y")
                    ),
                    portal_z=(
                        result.get("portal_z")
                        if prefer_portal
                        else route_snapshot.get("z")
                    ),
                )
            if from_sync and on_done is not None:
                on_done(bool(result.get("ok")), str(result.get("error") or result.get("note") or ""))

        if prefer_portal:
            title = "副控寻路地宫点" if from_sync else "寻路地宫点"
        else:
            title = "副控寻路NPC" if from_sync else "寻路NPC"
        self._attach_job(title, run)

    def _tick_fly_cd(self) -> None:
        """Refresh map-fly cooldown label. @author by ak"""
        try:
            pid = int(self._fixed_pid or self._selected_pid or 0)
            if pid:
                from app.core.map_fly import fly_cooldown_remain

                left = float(fly_cooldown_remain(pid) or 0.0)
            else:
                left = 0.0
            if left > 0.05:
                self.var_fly_cd.set(f"冷却: {left:.1f}s")
            else:
                self.var_fly_cd.set("冷却: 就绪")
        except Exception:
            pass
        try:
            self.after(400, self._tick_fly_cd)
        except Exception:
            pass

    def _task_or_runner_active(self) -> tuple[bool, str]:
        """
        Whether task one-shot busy or 执行计划 is running.

        Returns (active, reason_cn).
        @author by ak
        """
        reasons: list[str] = []
        try:
            if bool(getattr(self, "_busy", False)):
                reasons.append("任务操作进行中")
        except Exception:
            pass
        try:
            if self._runner is not None and self._runner.is_running():
                reasons.append("执行计划已开启")
            elif bool(getattr(self, "_running", False)):
                reasons.append("执行计划已开启")
        except Exception:
            pass
        if not reasons:
            return False, ""
        # unique preserve order
        seen = set()
        out = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                out.append(r)
        return True, "、".join(out)

    def _fly_page_spec(self) -> tuple[bool, int] | None:
        """
        Map combo label -> (is_default, ui_page).

        默认 -> (True, 0xFF)
        0页..5页 -> (False, 0..5)；与客户端 map key / packet page 同号。
        @author by ak
        """
        lab = (self.var_fly_page.get() or "").strip()
        for name, key in getattr(self, "_fly_fixed_pages", []) or []:
            if name != lab:
                continue
            if key == "default" or key is None:
                return True, 0xFF
            return False, int(key) & 0xFF
        if lab in ("默认", "默认页"):
            return True, 0xFF
        m_lab = lab
        if m_lab.endswith("页"):
            m_lab = m_lab[:-1].strip()
        try:
            n = int(m_lab)
            if n in FIXED_CUSTOM_FLY_PAGES:
                return False, n
        except Exception:
            pass
        return None

    def _fly_page_radio_index(self) -> int | None:
        """Rdo: default=0; custom key k → Rdo_(k+1). @author by ak"""
        spec = self._fly_page_spec()
        if spec is None:
            return None
        is_def, ui = spec
        if is_def:
            return 0
        return (int(ui) + 1) if int(ui) >= 0 else None

    def _update_fly_selected_button(self) -> None:
        """Enable 飞行 only when page+non-empty slot both selected. @author by ak"""
        btn = getattr(self, "btn_fly_selected", None)
        if btn is None:
            return
        meta = self._selected_fly_slot_meta()
        ok = bool(
            meta
            and not meta.get("empty")
            and self._fly_page_spec() is not None
            and (self.var_fly_slot.get() or "").strip()
        )
        try:
            btn.configure(state=tk.NORMAL if ok else tk.DISABLED)
        except Exception:
            pass

    def _on_fly_page_selected(self, _evt=None) -> None:
        """
        Fixed page chosen -> query that page slots (cascade).

        @author by ak
        """
        spec = self._fly_page_spec()
        if spec is None:
            self._fly_slot_meta = []
            if hasattr(self, "cmb_fly_slot"):
                self.cmb_fly_slot["values"] = []
            self.var_fly_slot.set("")
            self._update_fly_selected_button()
            return
        is_default, page_id = spec
        idx = 0 if is_default else int(page_id)

        self._fly_page_request_id = int(
            getattr(self, "_fly_page_request_id", 0) or 0
        ) + 1
        request_id = self._fly_page_request_id

        if bool(getattr(self, "_fly_page_loading", False)):
            # Do not lose a fast 0 -> 1 -> 2 selection sequence. The active
            # query will finish normally, but only the latest selected page is
            # queried/applied next.
            self._fly_page_requery_pending = True
            self._fly_slot_meta = []
            if hasattr(self, "cmb_fly_slot"):
                self.cmb_fly_slot["values"] = []
            self.var_fly_slot.set("")
            self._update_fly_selected_button()
            self.var_status.set("切换飞行页中…")
            self.log("自动任务 [地图飞行] 已记录最新页选择，等待当前查询结束")
            return

        if bool(getattr(self, "_fly_working", False)):
            self.log("自动任务 [地图飞行] 忙碌中，稍后再切页")
            return

        mounted = self._require_session()
        if mounted is None:
            return

        self._fly_page_loading = True
        self._fly_working = True
        self._fly_page_active_request_id = request_id
        self._fly_page_requery_pending = False
        page_name = (self.var_fly_page.get() or str(idx)).strip()
        self.var_status.set(f"读取飞行点：页「{page_name}」…")
        self.log(
            f"自动任务 [地图飞行] 查询页 label={page_name} "
            f"default={is_default} page_id={page_id}"
        )
        # clear slots while loading
        self._fly_slot_meta = []
        if hasattr(self, "cmb_fly_slot"):
            self.cmb_fly_slot["values"] = []
        self.var_fly_slot.set("")
        self._update_fly_selected_button()

        def worker() -> None:
            attach = None
            try:
                from app.core.map_fly import list_transmit_page_slots

                attach = open_attach_session(
                    mounted.pid, log=lambda m: self._push("log", m)
                )
                # 自定义页纯内存读取，不打开飞行旗/不点UI（防选页崩溃）
                result = None
                attempts = 1 if is_default else 4
                for attempt in range(1, attempts + 1):
                    if request_id != int(
                        getattr(self, "_fly_page_request_id", 0) or 0
                    ):
                        self._push(
                            "log",
                            "自动任务 [地图飞行] 当前页选择已变化，停止旧页重试",
                        )
                        return
                    result = list_transmit_page_slots(
                        attach,
                        idx,
                        page_id=page_id,
                        is_default=is_default,
                        open_if_needed=bool(is_default),
                        include_empty_slots=True,
                        log=lambda m: self._push("log", m),
                    )
                    if bool(getattr(result, "ok", False)):
                        break
                    error = str(getattr(result, "error", "") or "")
                    if error != "fly_mgr_unavailable" or attempt >= attempts:
                        break
                    self._push(
                        "log",
                        f"自动任务 [地图飞行] 页数据未就绪，重试 {attempt}/{attempts}",
                    )
                    time.sleep(0.45)
                assert result is not None
                detail = getattr(result, "detail", None) or {}
                ok = bool(getattr(result, "ok", False))
                msg = str(getattr(result, "message", "") or "")
                self._push(
                    "fly_page_slots",
                    {
                        "request_id": request_id,
                        "page": detail if isinstance(detail, dict) else {},
                        "ok": ok,
                        "message": msg,
                    },
                )
                self._push(
                    "log",
                    f"自动任务 [地图飞行] page_slots ok={ok} {msg}",
                )
            except Exception as e:
                self._push("log", f"自动任务 [地图飞行] page_slots err: {e}")
                self._push(
                    "fly_page_slots",
                    {
                        "request_id": request_id,
                        "page": {},
                        "ok": False,
                        "message": str(e),
                    },
                )
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass
                self._push("fly_page_done", request_id)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_fly_page_slots(self, page: dict) -> None:
        """Fill slot combobox from one page query result. @author by ak"""
        if not isinstance(page, dict):
            page = {}
        slots = list(page.get("slots") or [])
        page_id = int(page.get("page_id") if page.get("page_id") is not None else 0xFF) & 0xFF
        packet_page = page.get("packet_page")
        try:
            packet_page_i = (
                int(packet_page) & 0xFF if packet_page is not None else page_id
            )
        except Exception:
            packet_page_i = page_id
        logical_page = page.get("logical_page", page.get("ui_page"))
        try:
            logical_i = (
                int(logical_page) & 0xFF if logical_page is not None else page_id
            )
        except Exception:
            logical_i = page_id
        is_default = page_id == 0xFF or str(page.get("label") or "") in ("默认", "默认页")
        if is_default:
            radio_index = 0
            packet_page_i = 0xFF
        else:
            # custom packet k (含 0) → Rdo_(k+1)
            radio_index = (int(packet_page_i) + 1) if int(packet_page_i) != 0xFF else None
            if radio_index == 0:
                radio_index = 1
        page_label = str(page.get("label") or self.var_fly_page.get() or "")
        meta: list[dict] = []
        values: list[str] = []
        for s in slots:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or f"槽{int(s.get('slot') or 0) + 1}")
            empty = bool(s.get("empty"))
            values.append(name)
            sp = s.get("packet_page")
            try:
                sp_i = int(sp) & 0xFF if sp is not None else packet_page_i
            except Exception:
                sp_i = packet_page_i
            if is_default:
                use_page = 0xFF
            else:
                use_page = sp_i
            pos = s.get("pos")
            scene_id = s.get("scene_id")
            meta.append(
                {
                    "slot": int(s.get("slot") or 0),
                    "name": name,
                    "empty": empty,
                    "page_id": int(use_page) & 0xFF,
                    "logical_page": logical_i,
                    "packet_page": int(use_page) & 0xFF,
                    "radio_index": radio_index,
                    "page_label": page_label,
                    "pos": pos,
                    "scene_id": scene_id,
                }
            )
        self._fly_slot_meta = meta
        if hasattr(self, "cmb_fly_slot"):
            self.cmb_fly_slot["values"] = values
        pick = ""
        for i, m in enumerate(meta):
            if not m.get("empty"):
                pick = values[i]
                break
        if not pick and values:
            pick = values[0]
        self.var_fly_slot.set(pick)
        self._update_fly_selected_button()


    def _on_fly_slot_selected(self, _evt=None) -> None:
        """Slot combo changed -> update fly button enable. @author by ak"""
        self._update_fly_selected_button()

    def _selected_fly_slot_meta(self) -> dict | None:
        """Current cascade selection meta. @author by ak"""
        name = (self.var_fly_slot.get() or "").strip()
        meta = list(getattr(self, "_fly_slot_meta", []) or [])
        for m in meta:
            if str(m.get("name") or "") == name:
                return m
        return None

    def _on_fly_selected_slot(self) -> None:
        """Fly the cascade-selected page/slot. @author by ak"""
        meta = self._selected_fly_slot_meta()
        if not meta:
            self.var_status.set("请先选择页，等待读出点位后再选点飞行")
            return
        if meta.get("empty"):
            self.var_status.set("该槽位为空，无法飞行")
            return
        _raw_pid = meta.get("page_id")
        page_id = int(_raw_pid if _raw_pid is not None else 0xFF) & 0xFF
        # page=0 是第一自定义页（霸刀）的合法 packet；默认页才是 0xFF
        slot = int(meta.get("slot") or 0)
        label = str(meta.get("name") or f"槽{slot + 1}")
        ri_raw = meta.get("radio_index")
        try:
            radio_index = int(ri_raw) if ri_raw is not None else None
        except Exception:
            radio_index = None
        # custom packet k → Rdo k+1; never Rdo_0
        if page_id != 0xFF:
            if radio_index is None or int(radio_index) <= 0:
                radio_index = (int(page_id) + 1) if int(page_id) != 0xFF else None
        active, reason = self._task_or_runner_active()
        if active:
            prompt = (
                f"当前{reason}。"
                + "\n\n"
                + f"确定要飞「{label}」(页{radio_index}/page={page_id} 槽{slot}) 吗？"
                + "\n"
                + "（飞行独立执行，不会取消任务，但可能打断寻路/场景）"
            )
            ok = messagebox.askyesno(
                "地图飞行确认",
                prompt,
                parent=self.winfo_toplevel(),
            )
            if not ok:
                self.log(
                    f"自动任务 [地图飞行] 用户取消确认 slot page={page_id} slot={slot}"
                )
                return
        # Master fan-out must not depend on the recorded expected pos/scene
        # verification: a fly that reached the network is still a fly to mirror
        # to slaves, consistent with the preset (fuzhou/shimen/death) path.
        self._run_map_fly_slot(
            page_id,
            slot,
            label=label,
            radio_index=radio_index,
            expected_pos=None,
            expected_scene=None,
            from_sync=False,
        )

    def _on_map_fly(self, preset_key: str) -> None:
        """UI: fly fixed default point (supports master fan-out). @author by ak"""
        key = str(preset_key or "").strip().lower()
        label_map = {"fuzhou": "福州", "shimen": "师门", "death": "死亡点"}
        label = label_map.get(key, key or "目标")

        active, reason = self._task_or_runner_active()
        if active:
            prompt = (
                f"当前{reason}。" + "\n\n"
                + f"确定要飞「{label}」吗？" + "\n"
                + f"（飞行独立执行，不会取消任务，但可能打断寻路/场景）"
            )
            ok = messagebox.askyesno(
                "地图飞行确认",
                prompt,
                parent=self.winfo_toplevel(),
            )
            if not ok:
                self.log(f"自动任务 [地图飞行] 用户取消确认 preset={key} ({reason})")
                return

        self._run_map_fly(key, from_sync=False)

    def _run_map_fly(self, preset_key: str, *, from_sync: bool = False) -> None:
        """
        Independent of task busy/loading: always clickable; on CD no-op.

        Master success still fans out to slaves.

        @author by ak
        """
        key = str(preset_key or "").strip().lower()
        if key not in ("fuzhou", "shimen", "death"):
            if not from_sync:
                self.var_status.set(f"未知飞行预设: {preset_key}")
            return

        # 冷却中：直接不生效（不排队、不改任务状态）
        try:
            from app.core.map_fly import fly_cooldown_remain

            pid = int(self._fixed_pid or self._selected_pid or 0)
            left = float(fly_cooldown_remain(pid) or 0.0) if pid else 0.0
        except Exception:
            left = 0.0
        if left > 0.05:
            if hasattr(self, "var_fly_cd"):
                self.var_fly_cd.set(f"冷却: {left:.1f}s")
            # silent no-op (optional debug log only)
            self.log(f"自动任务 [地图飞行] 冷却中 {left:.1f}s，忽略点击 preset={key}")
            return

        fly_start_lock = getattr(self, "_fly_start_lock", None)
        if fly_start_lock is None:
            fly_start_lock = threading.Lock()
            self._fly_start_lock = fly_start_lock
        with fly_start_lock:
            if bool(getattr(self, "_fly_working", False)):
                # 上次飞行尚未结束：同样不生效
                self.log(f"自动任务 [地图飞行] 进行中，忽略点击 preset={key}")
                return
            mounted = self._require_session()
            if mounted is None:
                return
            self._fly_working = True
        label_map = {"fuzhou": "福州", "shimen": "师门", "death": "死亡点"}
        title = "副控飞行" if from_sync else f"飞{label_map.get(key, key)}"
        # 仅更新飞行相关提示，不触发任务 busy
        if hasattr(self, "var_fly_cd"):
            self.var_fly_cd.set("飞行中…")
        self.log(f"自动任务 [{title}] 开始 preset={key}")

        def worker() -> None:
            attach = None
            try:
                from app.core.map_fly import PRESET_POINTS, fly_page_slot_packet

                attach = open_attach_session(
                    mounted.pid, log=lambda m: self._push("log", m)
                )
                meta = PRESET_POINTS.get(key) or {}
                label = str(meta.get("label") or key)
                slot = int(meta.get("slot") or 0)
                result = fly_page_slot_packet(
                    attach,
                    0xFF,
                    slot,
                    label=label,
                    wait_cooldown=False,  # CD 已在入口拦截；不阻塞等待
                    respect_cooldown=True,
                    stop_event=None,
                    log=lambda m: self._push("log", m),
                )
                ok = bool(getattr(result, "ok", False))
                msg = str(getattr(result, "message", "") or "")
                detail = getattr(result, "detail", None) or {}
                moved = bool(detail.get("verified_move"))
                err = str(getattr(result, "error", "") or "")
                tag = "副控飞行" if from_sync else "地图飞行"
                if err == "cooldown":
                    # 竞态下再次撞 CD：静默不生效
                    self._push("log", f"自动任务 [{tag}] 冷却中，不生效")
                    try:
                        self._push("fly_cd", float(detail.get("remain_s") or detail.get("cd_remain_s") or 0.0))
                    except Exception:
                        pass
                    return
                if ok and moved:
                    status = f"{tag}成功：{label}"
                elif ok:
                    status = f"{tag}已发包：{label}（未确认位移）"
                else:
                    status = f"{tag}失败：{msg or label}"
                self._push("status", status)
                self._push("log", f"自动任务 [{tag}] ok={ok} moved={moved} {msg}")
                try:
                    cd_left = float(detail.get("cd_remain_s") or 0.0)
                    self._push("fly_cd", cd_left)
                except Exception:
                    pass
                if ok and not from_sync and self._control_role() == ROLE_MASTER:
                    self._publish_task_sync(
                        ACTION_MAP_FLY,
                        int(slot) + 1,
                        name=key,
                    )
                if ok:
                    self.user_log(
                        f"{'受控' if from_sync else '操作'}：地图飞行「{label}」"
                        + ("成功" if moved else "已发包"),
                        category=CAT_CONTROL if from_sync else CAT_TASK,
                    )
                elif not from_sync:
                    try:
                        self.user_log(
                            f"操作：地图飞行「{label}」失败 · {msg or err or '未知原因'}",
                            category=CAT_TASK,
                            source="自动任务",
                        )
                    except Exception:
                        pass
            except Exception as e:
                self._push("status", f"{title}失败: {e}")
                self._push("log", f"自动任务 [{title}] {e}")
                if not from_sync:
                    try:
                        self.user_log(
                            f"操作：{title}失败 · {e}",
                            category=CAT_TASK,
                            source="自动任务",
                        )
                    except Exception:
                        pass
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass
                self._fly_working = False
                # refresh cd label ASAP
                try:
                    from app.core.map_fly import fly_cooldown_remain

                    left2 = float(fly_cooldown_remain(int(mounted.pid)) or 0.0)
                    self._push("fly_cd", left2)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _save_team_settings(self) -> None:
        """Persist team member list + verified cache (memory + disk). @author by ak"""
        try:
            members = (self.var_team_members.get() or "").strip()
        except Exception:
            members = ""
        self.settings["team_members"] = members
        # team_enabled is legacy; auto-form is a button now.
        self.settings["team_enabled"] = False
        if "team_verified_text" not in self.settings:
            self.settings["team_verified_text"] = ""
        if "team_verified_roster" not in self.settings:
            self.settings["team_verified_roster"] = []
        try:
            from app.core.team_ops import save_team_prefs

            save_team_prefs(settings=self.settings)
        except Exception:
            pass

    def _team_member_text(self) -> str:
        try:
            return (self.var_team_members.get() or "").strip()
        except Exception:
            return str(self.settings.get("team_members") or "").strip()

    def _on_team_members_edited(self) -> None:
        """Invalidate verified-id cache when roster text changes. @author by ak"""
        try:
            from app.core.team_ops import (
                clear_team_verified_cache,
                is_team_roster_verified,
                roster_fingerprint,
            )
        except Exception:
            return
        members = self._team_member_text()
        self.settings["team_members"] = members
        # light persist of members text (debounce via FocusOut full save too)
        try:
            from app.core.team_ops import save_team_prefs

            save_team_prefs(settings=self.settings)
        except Exception:
            pass
        if is_team_roster_verified(members, self.settings):
            # Pure-id list stays ready while typing other pure ids.
            try:
                from app.core.team_ops import roster_all_literal_ids

                if roster_all_literal_ids(members) and hasattr(self, "var_team_status"):
                    # Keep status light-weight; avoid spamming on every keystroke.
                    pass
            except Exception:
                pass
            return
        # Only clear when a previous cache exists / fingerprint differs.
        cached_fp = str(self.settings.get("team_verified_text") or "")
        cur_fp = roster_fingerprint(members)
        if cached_fp and cached_fp != cur_fp:
            clear_team_verified_cache(self.settings, persist=True)
            if hasattr(self, "var_team_status"):
                self.var_team_status.set("名单已改，请重新校验")
            try:
                from app.core.team_ops import save_team_prefs

                save_team_prefs(settings=self.settings)
            except Exception:
                pass
        elif not members:
            clear_team_verified_cache(self.settings, persist=True)
            try:
                from app.core.team_ops import save_team_prefs

                save_team_prefs(settings=self.settings)
            except Exception:
                pass

    def _team_has_group_control(self) -> bool:
        """True when this window is 主控 (群控已开). @author by ak"""
        try:
            return self._control_role() == ROLE_MASTER
        except Exception:
            return False

    def _refresh_team_control_buttons(self) -> None:
        """
        自动整队 requires 主控; disable otherwise.
        召集 stays clickable (local fly if no group control).
        @author by ak
        """
        has_gc = self._team_has_group_control()
        try:
            if hasattr(self, "btn_team_auto"):
                self.btn_team_auto.configure(
                    state=("normal" if has_gc else "disabled")
                )
        except Exception:
            pass
        try:
            if (
                not has_gc
                and hasattr(self, "var_team_status")
                and not (self.var_team_status.get() or "").strip()
            ):
                self.var_team_status.set("整队需群控主控")
        except Exception:
            pass

    def _team_roster_ready(self, *, show_hint: bool = True) -> bool:
        """
        Gate for 自动整队 / 邀请 / 召集.

        Pure numeric id lists are ready immediately; name lists need 校验队伍.
        @author by ak
        """
        members = self._team_member_text()
        if not members:
            if show_hint and hasattr(self, "var_team_status"):
                self.var_team_status.set("请填写成员")
            return False
        try:
            from app.core.team_ops import (
                invite_targets_for_members,
                is_team_roster_verified,
                roster_all_literal_ids,
            )
        except Exception:
            if show_hint and hasattr(self, "var_team_status"):
                self.var_team_status.set("组队模块加载失败")
            return False
        if is_team_roster_verified(members, self.settings):
            targets = invite_targets_for_members(members, self.settings)
            if targets:
                return True
            if show_hint and hasattr(self, "var_team_status"):
                self.var_team_status.set("没有可邀请成员")
            return False
        if show_hint and hasattr(self, "var_team_status"):
            if roster_all_literal_ids(members):
                self.var_team_status.set("纯数字id名单异常，请检查")
            else:
                self.var_team_status.set("请先「校验队伍」")
        return False

    def _verified_invite_targets(self):
        """Invite targets from pure ids or verified cache. @author by ak"""
        from app.core.team_ops import invite_targets_for_members

        host_name = ""
        host_id = 0
        try:
            # 用当前选中会话身份排除自己（不依赖校验时 is_self）
            sess = self.selected_session() if hasattr(self, "selected_session") else None
            if sess is not None:
                from app.core.loot import open_attach_session
                from app.core.team_ops import read_host_identity

                attach = open_attach_session(int(sess.pid), log=lambda _m: None)
                try:
                    host_name, host_id = read_host_identity(attach, log=lambda _m: None)
                finally:
                    try:
                        attach.close()
                    except Exception:
                        pass
        except Exception:
            pass
        return invite_targets_for_members(
            self._team_member_text(),
            self.settings,
            host_name=host_name or None,
            host_id=int(host_id or 0) or None,
        )

    def _refresh_team_verify_status_hint(self) -> None:
        """Show pure-id / cached verify summary if still valid. @author by ak"""
        if not hasattr(self, "var_team_status"):
            return
        members = self._team_member_text()
        try:
            from app.core.team_ops import (
                invite_targets_for_members,
                is_team_roster_verified,
                roster_all_literal_ids,
            )
        except Exception:
            return
        if not is_team_roster_verified(members, self.settings):
            return
        if roster_all_literal_ids(members):
            targets = invite_targets_for_members(members, self.settings)
            if targets:
                # pure id list: show tokens as entered (no =id clutter)
                names = "、".join(str(t.name or t.obj_id) for t in targets)
                self.var_team_status.set(f"纯id已就绪：{names}")
            return
        roster = self.settings.get("team_verified_roster") or []
        parts: list[str] = []
        for r in roster:
            if not isinstance(r, dict):
                continue
            # 展示填写内容：名显示名；仅当填写的是数字 id 才显示 id
            tok = str(r.get("token") or "").strip()
            name = str(r.get("name") or "").strip()
            try:
                from app.core.team_ops import is_literal_member_id
            except Exception:
                is_literal_member_id = lambda _s: False  # type: ignore
            if is_literal_member_id(tok):
                label = tok
            else:
                label = tok or name
            if label and label not in parts:
                parts.append(label)
        if parts:
            self.var_team_status.set("已校验：" + "、".join(parts))

    def _on_team_verify(self) -> None:
        """UI: resolve member names/ids and cache for invite/gather. @author by ak"""
        try:
            if bool(getattr(self, "_team_verify_working", False)):
                if hasattr(self, "var_team_status"):
                    self.var_team_status.set("校验进行中…")
                return
            self._save_team_settings()
            members = self._team_member_text()
            if not members:
                if hasattr(self, "var_team_status"):
                    self.var_team_status.set("请填写成员")
                return
            mounted = self._require_session()
            if mounted is None:
                if hasattr(self, "var_team_status"):
                    self.var_team_status.set("未挂载，无法校验（游戏内按 Delete 连接）")
                return
            if hasattr(self, "var_team_status"):
                self.var_team_status.set("校验中…")
            self.log(f"自动任务 [组队] verify start members={members!r}")
            self._team_verify_working = True
        except Exception as e:
            try:
                if hasattr(self, "var_team_status"):
                    self.var_team_status.set(f"校验启动失败: {e}")
                self.log(f"自动任务 [组队] verify start err: {e}")
            except Exception:
                pass
            return

        def worker() -> None:
            attach = None
            try:
                from app.core.team_ops import (
                    clear_team_verified_cache,
                    save_team_verified_cache,
                    validate_team_roster,
                )

                attach = open_attach_session(
                    mounted.pid, log=lambda m: self._push("log", m)
                )
                result = validate_team_roster(
                    attach,
                    members,
                    store=self.store,
                    exclude_pid=int(mounted.pid),
                    log=lambda m: self._push("log", m),
                )
                ok = bool(getattr(result, "ok", False))
                msg = str(getattr(result, "message", "") or "")
                detail = getattr(result, "detail", None) or {}
                roster = detail.get("roster") or []

                def apply() -> None:
                    if ok:
                        save_team_verified_cache(self.settings, members, list(roster))
                        self.settings["team_members"] = members
                    else:
                        clear_team_verified_cache(self.settings)
                    if hasattr(self, "var_team_status"):
                        self.var_team_status.set(msg or ("校验通过" if ok else "校验失败"))
                    self._push(
                        "log",
                        f"自动任务 [组队] verify ok={ok} {msg}",
                    )
                    try:
                        self.user_log(
                            f"操作：校验队伍 · {msg or ('成功' if ok else '失败')}",
                            category=CAT_CONTROL,
                            source="自动任务",
                        )
                    except Exception:
                        pass

                try:
                    self.after(0, apply)
                except Exception:
                    apply()
            except Exception as e:
                def fail() -> None:
                    try:
                        from app.core.team_ops import clear_team_verified_cache

                        clear_team_verified_cache(self.settings)
                    except Exception:
                        pass
                    if hasattr(self, "var_team_status"):
                        self.var_team_status.set(f"校验失败: {e}")
                    self._push("log", f"自动任务 [组队] verify err: {e}")

                try:
                    self.after(0, fail)
                except Exception:
                    fail()
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass
                try:
                    self._team_verify_working = False
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _slave_in_team_roster(self, members: str) -> bool:
        """
        True if this window's host name/id is in the roster filter.

        Used so group-control team/gather only hits listed characters.
        Supports pure numeric id lists.
        @author by ak
        """
        members_s = str(members or "").strip()
        if not members_s:
            return True
        mounted = self.selected_session()
        if mounted is None:
            return False
        attach = None
        try:
            from app.core.team_ops import host_in_team_members, read_host_identity

            attach = open_attach_session(mounted.pid, log=lambda _m: None)
            name, oid = read_host_identity(attach, log=lambda _m: None)
            return bool(host_in_team_members(name, members_s, host_id=oid))
        except Exception as e:
            self.log(f"自动任务 [副控] 名单匹配失败: {e}")
            return False
        finally:
            if attach is not None:
                try:
                    attach.close()
                except Exception:
                    pass

    def _on_team_refresh_party(self) -> None:
        """UI: refresh current party member names. @author by ak"""
        mounted = None
        try:
            mounted = self.selected_session()
        except Exception:
            mounted = None
        if mounted is None:
            if hasattr(self, "var_team_party"):
                self.var_team_party.set("当前队伍：未挂载")
            return

        def worker() -> None:
            attach = None
            try:
                from app.core.team_ops import (
                    format_party_members_label,
                    list_party_members,
                )

                attach = open_attach_session(mounted.pid, log=lambda _m: None)
                party = list_party_members(
                    attach,
                    store=self.store,
                    exclude_pid=int(mounted.pid),
                    fresh=True,
                    log=lambda m: self._push("log", m),
                )
                label = format_party_members_label(party)
                self._push("team_party", label)
            except Exception as e:
                self._push("team_party", f"当前队伍：读取失败 ({e})")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()


    def _on_team_hang_sync(self) -> None:
        """
        取消/开启内挂：读本号挂机状态 → 取反为目标 → 本号 + 群控副控同步。

        已是目标状态的号跳过（不重复 Alt+R）。
        无群控时仅操作本号。
        @author by ak
        """
        if bool(getattr(self, "_hang_sync_working", False)):
            if hasattr(self, "var_team_status"):
                self.var_team_status.set("内挂同步进行中…")
            self.log("自动任务 [内挂] 忽略：进行中")
            return
        mounted = self._require_session()
        if mounted is None:
            if hasattr(self, "var_team_status"):
                self.var_team_status.set("内挂同步失败：未挂载")
            return
        members = self._team_member_text()
        try:
            request_dungeon = bool(self.var_team_hang_dungeon.get())
        except Exception:
            request_dungeon = False
        self._hang_sync_working = True
        try:
            if hasattr(self, "btn_team_hang"):
                self.btn_team_hang.configure(state=tk.DISABLED)
        except Exception:
            pass
        if hasattr(self, "var_team_status"):
            self.var_team_status.set("读取挂机状态…")

        def worker() -> None:
            attach = None
            sync_lock = getattr(self, "_hang_sync_lock", None)
            if sync_lock is not None:
                sync_lock.acquire()
            try:
                from app.core.activity_auto import (
                    format_hang_state,
                    probe_hang_state_mem,
                )
                from app.core.hang_settings import get_hang_config, start_hang, stop_hang

                attach = open_attach_session(
                    mounted.pid, log=lambda m: self._push("log", m)
                )
                st = probe_hang_state_mem(
                    attach, log=lambda m: self._push("log", m)
                )
                self._push("log", f"自动任务 [内挂] probe {format_hang_state(st)}")
                if not st.ok or st.on is None:
                    msg = "挂机状态未知，无法同步"
                    self._push("team_status", msg)
                    self._push("status", msg)
                    self._push("log", f"自动任务 [内挂] {msg}")
                    return
                cur_on = bool(st.on)
                desired_on = not cur_on
                want_s = "开" if desired_on else "关"
                cur_s = "开" if cur_on else "关"
                self._push(
                    "team_status",
                    f"内挂同步：本号现={cur_s} → 目标={want_s}…",
                )
                self._push(
                    "log",
                    f"自动任务 [内挂] master cur={cur_s} want={want_s}",
                )
                # 只用本角色ID已保存配置开/关；不同步设置。
                # 会话配置优先（本窗当前保存值），再落到角色磁盘 prefs。
                cid = ""
                try:
                    cid = self._hang_char_id_key()
                except Exception:
                    cid = ""
                cfg = get_hang_config(self.settings, char_id=cid or None)
                hwnd_i = int(getattr(mounted, "hwnd", 0) or 0)
                if desired_on:
                    from dataclasses import replace

                    function_started = False
                    temporary_mode = 1 if request_dungeon else 0
                    cfg = replace(cfg, mode=temporary_mode)
                    prepare = apply_hang_prepare(
                        attach, cfg, log=lambda m: self._push("log", m)
                    )
                    if not prepare.get("ok"):
                        ret = {
                            "ok": False,
                            "message": "挂机参数同步失败: "
                            + str(prepare.get("message") or "unknown"),
                        }
                    else:
                        action_cfg = (
                            # respect captain saved cfg.empty_skill - no force
                            # if request_dungeon and cfg.empty_skill: (removed force per user)
                            cfg  # captain respects saved no-skill setting
                        )
                        ret = start_hang(
                            attach,
                            action_cfg,
                            hwnd=hwnd_i,
                            log=lambda m: self._push("log", m),
                        )
                        function_started = bool(request_dungeon and ret.get("ok") and bool(getattr(cfg, "empty_skill", False)))
                    self._team_hang_force_started = function_started
                else:
                    temporary_mode = None
                    # captain stop respects cfg (no force)
                    ret = stop_hang(
                        attach, cfg, hwnd=hwnd_i, log=lambda m: self._push("log", m)
                    )
                    if ret.get("ok"):
                        self._team_hang_force_started = False
                ok_m = bool(ret.get("ok"))
                msg_m = str(ret.get("message") or "")
                self._push(
                    "log",
                    f"自动任务 [内挂] 本号 ok={ok_m} char_id={cid or '-'} {msg_m}",
                )
                # 副控保留各自配置，只传本次临时模式覆盖。
                notified = False
                if self._control_role() == ROLE_MASTER:
                    notified = self._publish_task_sync(
                        ACTION_HANG_SYNC,
                        1 if desired_on else 0,
                        name="on" if desired_on else "off",
                        members=members,
                        hang_mode=temporary_mode,
                    )
                mode_s = ""
                if desired_on and temporary_mode in (0, 1):
                    mode_s = "（" + (
                        "副本模式" if temporary_mode == 1 else "普通模式"
                    ) + "）"
                status = (
                    f"内挂→{want_s}{mode_s} · "
                    f"本号={'成功' if ok_m else '失败'} {msg_m}"
                )
                if self._control_role() == ROLE_MASTER:
                    status += " · " + ("已通知副控" if notified else "无在线副控")
                self._push("team_status", status)
                self._push("status", status)
                self._push("log", f"自动任务 [内挂] done {status}")
                try:
                    self.user_log(
                        f"操作：内挂同步→{want_s} · 本号={'成功' if ok_m else '失败'}",
                        category=CAT_CONTROL,
                        source="自动任务",
                    )
                except Exception:
                    pass
            except Exception as e:
                self._push("team_status", f"内挂同步失败: {e}")
                self._push("status", f"内挂同步失败: {e}")
                self._push("log", f"自动任务 [内挂] err: {e}")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass
                if sync_lock is not None:
                    try:
                        sync_lock.release()
                    except Exception:
                        pass
                self._hang_sync_working = False

                def _re() -> None:
                    try:
                        if hasattr(self, "btn_team_hang"):
                            self.btn_team_hang.configure(state=tk.NORMAL)
                    except Exception:
                        pass

                try:
                    self.after(0, _re)
                except Exception:
                    _re()

        threading.Thread(target=worker, daemon=True).start()

    def _run_hang_sync(
        self,
        *,
        desired_on: bool,
        from_sync: bool = False,
        temporary_mode: int | None = None,
    ) -> None:
        """
        Apply hang open/close on this window using local role prefs only.

        Group control never overwrites this role's hang settings.

        @author by ak
        """
        mounted = self._require_session()
        if mounted is None:
            return
        want_s = "开" if desired_on else "关"
        tag = "副控内挂" if from_sync else "内挂"

        def worker() -> None:
            attach = None
            sync_lock = getattr(self, "_hang_sync_lock", None)
            if sync_lock is not None:
                sync_lock.acquire()
            try:
                from app.core.activity_auto import probe_hang_state_mem
                from app.core.hang_settings import get_hang_config, start_hang, stop_hang

                attach = open_attach_session(
                    mounted.pid, log=lambda m: self._push("log", m)
                )
                cid = ""
                try:
                    cid = self._hang_char_id_key()
                except Exception:
                    cid = ""
                cfg = get_hang_config(self.settings, char_id=cid or None)
                hwnd_i = int(getattr(mounted, "hwnd", 0) or 0)
                mode_i = int(temporary_mode) if temporary_mode in (0, 1) else None
                # Apply local saved attributes plus this command's temporary
                # mode before probing/skipping. No prefs are written here.
                if desired_on:
                    if mode_i is not None:
                        from dataclasses import replace

                        cfg = replace(cfg, mode=mode_i)
                    prepare = apply_hang_prepare(
                        attach, cfg, log=lambda m: self._push("log", m)
                    )
                    if not prepare.get("ok"):
                        msg = "挂机参数同步失败: " + str(
                            prepare.get("message") or "unknown"
                        )
                        self._push("log", f"自动任务 [{tag}] {msg}")
                        self._push("status", f"{tag}→{want_s} · 失败 {msg}")
                        return
                # A mode transition must be applied even when autoplay is already running.
                try:
                    st = probe_hang_state_mem(attach, log=lambda m: self._push("log", m))
                    live_mode = None
                    try:
                        live_mode = int(((st.detail or {}).get("mem") or {}).get("mode"))
                    except (TypeError, ValueError):
                        live_mode = None
                    mode_matches = mode_i is None or live_mode is None or live_mode == mode_i
                    if st.ok and st.on is not None and bool(st.on) == bool(desired_on) and mode_matches:
                        self._push(
                            "log",
                            f"自动任务 [{tag}] already {want_s}, skip "
                            f"temporary_mode={mode_i}",
                        )
                        mode_s = ""
                        if desired_on and mode_i in (0, 1):
                            mode_s = "（" + (
                                "副本模式" if mode_i == 1 else "普通模式"
                            ) + "）"
                        self._push(
                            "status", f"{tag}→{want_s}{mode_s} · 已是目标状态"
                        )
                        return
                except Exception:
                    pass
                if desired_on:
                    ret = start_hang(
                        attach,
                        cfg,
                        hwnd=hwnd_i,
                        temporary=bool(from_sync and mode_i == 1),
                        log=lambda m: self._push("log", m),
                    )
                else:
                    ret = stop_hang(
                        attach, cfg, hwnd=hwnd_i, log=lambda m: self._push("log", m)
                    )
                ok = bool(ret.get("ok"))
                msg = str(ret.get("message") or "")
                self._push(
                    "log",
                    f"自动任务 [{tag}] want={want_s} ok={ok} char_id={cid or '-'} "
                    f"temporary_mode={mode_i} {msg}",
                )
                mode_s = ""
                if desired_on and mode_i in (0, 1):
                    mode_s = "（" + (
                        "副本模式" if mode_i == 1 else "普通模式"
                    ) + "）"
                self._push(
                    "status",
                    f"{tag}→{want_s}{mode_s} · {'成功' if ok else '失败'} {msg}",
                )
            except Exception as e:
                self._push("log", f"自动任务 [{tag}] err: {e}")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass
                if sync_lock is not None:
                    try:
                        sync_lock.release()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_team_auto_form(self) -> None:
        """
        自动整队：全员离队 → 批量邀请并确认实到 → 稳定 → 统一飞福州。

        需要群控主控；未开群控不允许执行。委托给共享的 TeamFormService。
        @author by ak
        """
        self._save_team_settings()
        self._refresh_team_control_buttons()
        if bool(getattr(self, "_team_auto_form_working", False)):
            if hasattr(self, "var_team_status"):
                self.var_team_status.set("自动整队进行中，请稍候")
            self.log("自动任务 [自动整队] 忽略：进行中")
            return
        if not self._team_has_group_control():
            if hasattr(self, "var_team_status"):
                self.var_team_status.set("自动整队需群控主控")
            self.log("自动任务 [自动整队] 拒绝：未开群控主控")
            return
        members = self._team_member_text()
        if not members:
            if hasattr(self, "var_team_status"):
                self.var_team_status.set("请填写成员")
            return
        if not self._team_roster_ready(show_hint=True):
            return
        if hasattr(self, "var_team_status"):
            self.var_team_status.set("自动整队：全员离队…")
        self.log(f"自动任务 [自动整队] start members={members!r}")
        try:
            self.user_log(
                "操作：自动整队 · 全员离队→逐个邀请→全员实到→飞福州",
                category=CAT_CONTROL,
            )
        except Exception:
            pass

        mounted = self._require_session()
        if mounted is None:
            return
        targets = self._verified_invite_targets()
        service = self._build_team_service()

        self._team_auto_form_working = True
        try:
            if hasattr(self, "btn_team_auto"):
                self.btn_team_auto.configure(state=tk.DISABLED)
        except Exception:
            pass

        def worker() -> None:
            attach = None
            try:
                time.sleep(1.0)
                attach = open_attach_session(
                    mounted.pid, log=lambda m: self._push("log", m)
                )
                result = service.form(
                    attach,
                    targets,
                    members_text=members,
                    exclude_pid=int(mounted.pid),
                    stop_event=None,
                )
                ok = bool(getattr(result, "ok", False))
                msg = str(getattr(result, "message", "") or "")
                detail = getattr(result, "detail", None) or {}
                self._push("team_status", msg)
                self._push("status", msg)
                self._push("log", f"自动任务 [自动整队] ok={ok} {msg}")
                try:
                    from app.core.team_ops import (
                        format_party_members_label,
                        list_party_members,
                    )

                    party = list_party_members(
                        attach,
                        store=self.store,
                        exclude_pid=int(mounted.pid),
                        fresh=True,
                        log=lambda _m: None,
                    )
                    self._push("team_party", format_party_members_label(party))
                except Exception:
                    pass
                if ok:
                    try:
                        self.user_log(
                            f"操作：自动整队完成 · {msg}",
                            category=CAT_CONTROL,
                            source="自动任务",
                        )
                    except Exception:
                        pass
            except Exception as e:
                self._push("team_status", f"自动整队失败: {e}")
                self._push("status", f"自动整队失败: {e}")
                self._push("log", f"自动任务 [自动整队] err: {e}")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass
                self._team_auto_form_working = False

                def _reenable() -> None:
                    try:
                        if hasattr(self, "btn_team_auto"):
                            self.btn_team_auto.configure(state=tk.NORMAL)
                    except Exception:
                        pass

                try:
                    self.after(0, _reenable)
                except Exception:
                    _reenable()

        self.after(250, lambda: threading.Thread(target=worker, daemon=True).start())

    def _on_team_invite_now(self) -> None:
        """
        邀请：只发邀请，不管当前组队状态；邀不到也忽略。

        @author by ak
        """
        self._save_team_settings()
        members = self._team_member_text()
        if not members:
            if hasattr(self, "var_team_status"):
                self.var_team_status.set("请填写成员")
            return
        if not self._team_roster_ready(show_hint=True):
            return
        if hasattr(self, "var_team_status"):
            self.var_team_status.set("正在全部邀请…")
        self._run_team_invite(mode="invite_only")

    def _run_team_invite(self, *, mode: str = "invite_only") -> None:
        """
        Background invite only. Already-in-party members are skipped; if host is already captain, never leave (leaving as leader would hand captain to peer).

        @author by ak
        """
        mounted = self._require_session()
        if mounted is None:
            return
        if not self._team_roster_ready(show_hint=True):
            return
        targets = self._verified_invite_targets()
        tag = "全部邀请"

        def worker() -> None:
            attach = None
            try:
                from app.core.team_ops import (
                    format_party_members_label,
                    invite_members_by_targets,
                    list_party_members,
                )

                attach = open_attach_session(
                    mounted.pid, log=lambda m: self._push("log", m)
                )
                result = invite_members_by_targets(
                    attach,
                    targets,
                    leave_if_teamed=False,
                    # 已在队成员不再重复邀请；已是队长时内部也会强制跳过
                    skip_already_in_party=True,
                    store=self.store,
                    exclude_pid=int(mounted.pid),
                    log=lambda m: self._push("log", m),
                )
                msg = str(getattr(result, "message", "") or "").strip() or "已发送邀请"
                # 邀请不到也不当失败打扰用户；面板只显示短句
                self._push("team_status", msg)
                self._push("status", msg)
                self._push("log", f"自动任务 [{tag}] {msg}")
                try:
                    self.user_log(
                        f"操作：邀请 · {msg}",
                        category=CAT_CONTROL,
                        source="自动任务",
                    )
                except Exception:
                    pass
                try:
                    party = list_party_members(
                        attach,
                        store=self.store,
                        exclude_pid=int(mounted.pid),
                        fresh=True,
                        log=lambda m: self._push("log", m),
                    )
                    self._push("team_party", format_party_members_label(party))
                except Exception:
                    pass
            except Exception as e:
                # still soft: log only
                self._push("team_status", f"邀请已尝试 · {e}")
                self._push("log", f"自动任务 [{tag}] err: {e}")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def _run_team_leave(
        self, *, from_sync: bool = False, keep_leader_id: int = 0
    ) -> None:
        """
        Background: leave current party if any.

        keep_leader_id>0: skip leave when already under that captain.
        @author by ak
        """
        mounted = self._require_session()
        if mounted is None:
            return

        def worker() -> None:
            attach = None
            try:
                from app.core.team_ops import leave_team, leave_team_unless_under_leader

                attach = open_attach_session(
                    mounted.pid, log=lambda m: self._push("log", m)
                )
                if int(keep_leader_id or 0) > 0:
                    result = leave_team_unless_under_leader(
                        attach,
                        int(keep_leader_id),
                        log=lambda m: self._push("log", m),
                    )
                else:
                    result = leave_team(attach, log=lambda m: self._push("log", m))
                ok = bool(getattr(result, "ok", False))
                msg = str(getattr(result, "message", "") or "")
                tag = "副控离队" if from_sync else "离队"
                self._push("team_status", msg)
                self._push("status", f"{tag}：{msg}")
                self._push("log", f"自动任务 [{tag}] ok={ok} {msg}")
                try:
                    from app.core.team_ops import (
                        format_party_members_label,
                        list_party_members,
                    )

                    party = list_party_members(
                        attach,
                        store=self.store,
                        exclude_pid=int(mounted.pid),
                        fresh=True,
                        log=lambda _m: None,
                    )
                    self._push("team_party", format_party_members_label(party))
                except Exception:
                    pass
            except Exception as e:
                self._push("log", f"自动任务 [离队] err: {e}")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_team_gather(self) -> None:
        """
        召集：飞福州 → 当前点击账号发起组队跟随。

        有群控主控：名单内副控一起飞。
        无群控：仅本号飞 + 本号发起跟随（不参与群飞）。
        不离队、不邀请。
        @author by ak
        """
        self._save_team_settings()
        if bool(getattr(self, "_team_gather_working", False)):
            if hasattr(self, "var_team_status"):
                self.var_team_status.set("召集进行中，请稍候")
            self.log("自动任务 [召集] 忽略：进行中")
            return
        members = self._team_member_text()
        if not members:
            if hasattr(self, "var_team_status"):
                self.var_team_status.set("请填写成员")
            return
        if not self._team_roster_ready(show_hint=True):
            return
        has_gc = self._team_has_group_control()
        if hasattr(self, "var_team_status"):
            self.var_team_status.set(
                "召集中：飞福州…" if has_gc else "召集中：本号飞福州…"
            )
        self.log(
            f"自动任务 [召集] start members={members!r} group_control={has_gc}"
        )
        try:
            self.user_log(
                "操作：召集 · 飞福州后由当前账号发起组队跟随",
                category=CAT_CONTROL,
            )
        except Exception:
            pass

        mounted = self._require_session()
        if mounted is None:
            if hasattr(self, "var_team_status"):
                self.var_team_status.set("召集失败：未挂载")
            return

        self._team_gather_working = True
        try:
            if hasattr(self, "btn_team_gather"):
                self.btn_team_gather.configure(state=tk.DISABLED)
        except Exception:
            pass

        def worker() -> None:
            attach = None
            try:
                from app.core.map_fly import fly_page_slot_packet
                from app.core.team_ops import (
                    format_party_members_label,
                    list_party_members,
                    set_team_follow,
                )

                attach = open_attach_session(
                    mounted.pid, log=lambda m: self._push("log", m)
                )
                result = fly_page_slot_packet(
                    attach,
                    0xFF,
                    0,
                    label="福州城",
                    wait_cooldown=False,
                    respect_cooldown=True,
                    stop_event=None,
                    log=lambda m: self._push("log", m),
                )
                ok_fly = bool(getattr(result, "ok", False))
                self._push(
                    "log",
                    f"自动任务 [召集] 队长飞福州 ok={ok_fly} "
                    f"{getattr(result, 'message', '')}",
                )
                if self._control_role() == ROLE_MASTER:
                    slot = 0
                    self._publish_task_sync(
                        ACTION_MAP_FLY,
                        int(slot) + 1,
                        name="fuzhou",
                        members=members,
                    )
                # Wait scene settle, then only the current clicked account sends follow.
                time.sleep(3.0)
                self._push("team_status", "召集：当前账号发起组队跟随…")
                # Raw follow packet is fire-and-forget; no scene-stability wait.
                ok_f = bool(
                    set_team_follow(attach, enabled=True, log=lambda m: self._push("log", m)).ok
                )
                fmsg = "已发起组队跟随(封包)" if ok_f else "组队跟随封包失败"
                try:
                    party = list_party_members(
                        attach,
                        store=self.store,
                        exclude_pid=int(mounted.pid),
                        fresh=True,
                        log=lambda m: self._push("log", m),
                    )
                    self._push("team_party", format_party_members_label(party))
                except Exception:
                    pass
                fly_s = "已飞" if ok_fly else "飞行失败"
                follow_s = "已跟随" if ok_f else "跟随失败"
                msg = f"{fly_s} · {follow_s}"
                self._push(
                    "team_status",
                    ("召集完成 · " if (ok_fly or ok_f) else "召集结束 · ") + msg,
                )
                self._push("status", f"召集：{msg}")
                # 详细跟随结果只进日志
                if fmsg:
                    self._push("log", f"自动任务 [召集] follow detail: {fmsg}")
                self._push("log", f"自动任务 [召集] {msg}")
                try:
                    self.user_log(
                        f"操作：召集 · {msg}",
                        category=CAT_CONTROL,
                        source="自动任务",
                    )
                except Exception:
                    pass
            except Exception as e:
                self._push("team_status", f"召集失败: {e}")
                self._push("status", f"召集失败: {e}")
                self._push("log", f"自动任务 [召集] err: {e}")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass
                self._team_gather_working = False

                def _reenable_gather() -> None:
                    try:
                        if hasattr(self, "btn_team_gather"):
                            self.btn_team_gather.configure(state=tk.NORMAL)
                    except Exception:
                        pass

                try:
                    self.after(0, _reenable_gather)
                except Exception:
                    _reenable_gather()

        threading.Thread(target=worker, daemon=True).start()

    def _on_team_follow(self, enabled: bool = True):
        """Captain-side team follow on/off through the raw c2s packet. @author by ak"""
        self._save_team_settings()
        mounted = self._require_session()
        if mounted is None:
            if hasattr(self, "var_team_status"):
                self.var_team_status.set("组队跟随失败：未挂载")
            return
        label = "组队跟随" if enabled else "取消跟随"
        if hasattr(self, "var_team_status"):
            self.var_team_status.set(f"{label}中…")
        self.log(f"自动任务 [{label}] start pid={getattr(mounted, 'pid', None)}")

        def worker() -> None:
            attach = None
            try:
                from app.core.grocery_auto import open_attach_session
                from app.core.team_ops import (
                    format_party_members_label,
                    list_party_members,
                    set_team_follow,
                )

                attach = open_attach_session(
                    int(mounted.pid), log=lambda m: self._push("log", m)
                )
                # Raw follow packet is fire-and-forget; no scene-stability wait.
                res = set_team_follow(
                    attach,
                    enabled=bool(enabled),
                    log=lambda m: self._push("log", m),
                )
                ok = bool(res.ok)
                msg = str(res.message or "")
                try:
                    party = list_party_members(
                        attach,
                        store=self.store,
                        exclude_pid=int(mounted.pid),
                        fresh=True,
                        log=lambda m: self._push("log", m),
                    )
                    self._push("team_party", format_party_members_label(party))
                except Exception:
                    pass
                self._push("team_status", f"{label} · {msg}")
                self._push("status", f"{label}：{msg}")
                self._push("log", f"自动任务 [{label}] {msg}")
                try:
                    self.user_log(
                        f"操作：{label} · {msg}",
                        category=CAT_CONTROL,
                        source="自动任务",
                    )
                except Exception:
                    pass
            except Exception as e:
                self._push("team_status", f"{label}失败: {e}")
                self._push("status", f"{label}失败: {e}")
                self._push("log", f"自动任务 [{label}] err: {e}")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()


    def _run_map_fly_slot(
        self,
        page_id: int,
        slot: int,
        *,
        label: str = "",
        radio_index: int | None = None,
        expected_pos=None,
        expected_scene=None,
        from_sync: bool = False,
    ) -> None:
        """
        Fly explicit page/slot; independent of task busy; CD no-op.

        Sync name format: slot:{page}:{slot}:{label}
        @author by ak
        """
        page = int(page_id) & 0xFF
        sl = int(slot)
        disp = str(label or f"page={page} slot={sl}")

        try:
            from app.core.map_fly import fly_cooldown_remain

            pid = int(self._fixed_pid or self._selected_pid or 0)
            left = float(fly_cooldown_remain(pid) or 0.0) if pid else 0.0
        except Exception:
            left = 0.0
        if left > 0.05:
            if hasattr(self, "var_fly_cd"):
                self.var_fly_cd.set(f"冷却: {left:.1f}s")
            self.log(
                f"自动任务 [地图飞行] 冷却中 {left:.1f}s，忽略 "
                f"page={page} slot={sl}"
            )
            return
        if bool(getattr(self, "_fly_working", False)):
            self.log(
                f"自动任务 [地图飞行] 进行中，忽略 page={page} slot={sl}"
            )
            return

        mounted = self._require_session()
        if mounted is None:
            return

        self._fly_working = True
        title = "副控飞行" if from_sync else f"飞「{disp}」"
        if hasattr(self, "var_fly_cd"):
            self.var_fly_cd.set("飞行中…")
        self.log(
            f"自动任务 [{title}] 开始 page={page}/0x{page:02X} slot={sl}"
        )

        def worker() -> None:
            attach = None
            try:
                from app.core.map_fly import fly_page_slot_packet

                attach = open_attach_session(
                    mounted.pid, log=lambda m: self._push("log", m)
                )
                result = fly_page_slot_packet(
                    attach,
                    page,
                    sl,
                    label=disp,
                    wait_cooldown=False,
                    respect_cooldown=True,
                    expected_pos=expected_pos,
                    expected_scene=expected_scene,
                    stop_event=None,
                    log=lambda m: self._push("log", m),
                )
                ok = bool(getattr(result, "ok", False))
                msg = str(getattr(result, "message", "") or "")
                detail = getattr(result, "detail", None) or {}
                moved = bool(detail.get("verified_move"))
                err = str(getattr(result, "error", "") or "")
                tag = "副控飞行" if from_sync else "地图飞行"
                if err == "cooldown":
                    self._push("log", f"自动任务 [{tag}] 冷却中，不生效")
                    try:
                        self._push(
                            "fly_cd",
                            float(
                                detail.get("remain_s")
                                or detail.get("cd_remain_s")
                                or 0.0
                            ),
                        )
                    except Exception:
                        pass
                    return
                if ok and moved:
                    status = f"{tag}成功：{disp}"
                elif ok:
                    status = f"{tag}已发包：{disp}（未确认位移）"
                else:
                    status = f"{tag}失败：{msg or disp}"
                self._push("status", status)
                self._push(
                    "log",
                    f"自动任务 [{tag}] ok={ok} moved={moved} "
                    f"page={page} slot={sl} {msg}",
                )
                try:
                    self._push("fly_cd", float(detail.get("cd_remain_s") or 0.0))
                except Exception:
                    pass
                if ok and not from_sync and self._control_role() == ROLE_MASTER:
                    # 群控：自定义点同步 slot:{page}:{slot}:{label}
                    safe_label = (
                        "".join(
                            ch
                            for ch in disp
                            if ch.isprintable() and ch not in ":|"
                        ).strip()
                        or f"p{page}s{sl}"
                    )[:40]
                    sync_name = f"slot:{page}:{sl}:{safe_label}"
                    # Custom-slot tid must not collide with preset tids
                    # (fuzhou/shimen/death = 1..3), or the hub 3s dedupe
                    # silently drops the custom publish.
                    tid = 0x10000 | ((page & 0xFF) << 8) | ((sl + 1) & 0xFF)
                    if tid == 0:
                        tid = 1
                    self._publish_task_sync(ACTION_MAP_FLY, int(tid), name=sync_name)
                    self._push(
                        "log",
                        f"自动任务 [群控] 同步地图飞行 {sync_name}",
                    )
                if ok:
                    self.user_log(
                        f"{'受控' if from_sync else '操作'}：地图飞行「{disp}」"
                        + ("成功" if moved else "已发包"),
                        category=CAT_CONTROL if from_sync else CAT_TASK,
                    )
                elif not from_sync:
                    try:
                        self.user_log(
                            f"操作：地图飞行「{disp}」失败 · {msg or err or '未知原因'}",
                            category=CAT_TASK,
                            source="自动任务",
                        )
                    except Exception:
                        pass
            except Exception as e:
                self._push("status", f"{title}失败: {e}")
                self._push("log", f"自动任务 [{title}] {e}")
            finally:
                if attach is not None:
                    try:
                        attach.close()
                    except Exception:
                        pass
                self._fly_working = False
                try:
                    from app.core.map_fly import fly_cooldown_remain

                    left2 = float(fly_cooldown_remain(int(mounted.pid)) or 0.0)
                    self._push("fly_cd", left2)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_accept(self) -> None:
        raw = (self.var_task_id.get() or "").strip()
        if not raw:
            self.var_status.set("请输入任务ID后再接取")
            return
        try:
            task_id = int(raw, 0)
        except ValueError:
            self.var_status.set("请输入有效任务ID")
            return
        self._run_accept(task_id, from_sync=False)

    def _run_accept(
        self,
        task_id: int,
        *,
        from_sync: bool = False,
        on_done: Callable[[bool, str], None] | None = None,
    ) -> None:
        def run(attach, mounted, stop_event) -> None:
            # 主控：正常寻路接取；副控：fast 不寻路，主控已验证任务 ID
            result = accept_task_routed(
                attach,
                task_id,
                hwnd=int(getattr(mounted, "hwnd", 0) or 0),
                log=lambda m: self._push("log", m),
                fast=bool(from_sync),
                stop_event=stop_event,
            )
            if stop_event is not None and stop_event.is_set():
                self._push("status", "接取已取消")
                if on_done is not None:
                    on_done(False, "cancelled")
                return
            detail = result.error or result.note or ""
            tag = "副控接取" if from_sync else "接取"
            self._push(
                "status",
                f"{tag}成功" if result.ok else f"{tag}失败: {detail}",
            )
            self._push("log", f"自动任务 [{tag}] ok={result.ok} {detail}")
            if not from_sync:
                try:
                    self.user_log(
                        f"操作：接取任务 #{int(task_id)} "
                        + ("成功" if result.ok else f"失败 · {detail or '未知原因'}"),
                        category=CAT_TASK,
                        source="自动任务",
                        dedupe_s=0.3,
                    )
                except Exception:
                    pass
            # 轻量刷新列表（含 NPC）；不扫附近任务
            if not from_sync:
                light = list_accepted_tasks_light(
                    attach, prev=self._tasks, log=lambda _m: None
                )
                self._push(
                    "tasks",
                    enrich_tasks_with_npc(
                        attach,
                        light,
                        radius=80.0,
                        live_scan=False,
                        stop_event=stop_event,
                        log=lambda _m: None,
                    ),
                )
            if result.ok and not from_sync:
                self._publish_task_sync(ACTION_ACCEPT, task_id)
            if from_sync and on_done is not None:
                on_done(bool(result.ok), str(detail))

        title = "副控接取" if from_sync else "接取任务"
        self._attach_job(title, run)

    def _capture_daily_accept_packets(self) -> None:
        """Capture one normal NPC accept interaction through CD1740."""
        def run(attach, mounted, stop_event) -> None:
            from app.core.packet_intercept import InterceptState
            before = []
            try:
                from app.core.task_api import list_accepted_task_ids
                before = list_accepted_task_ids(attach, log=lambda _m: None)
            except Exception:
                pass
            tap = InterceptState(int(mounted.pid), target="system")
            self._push("status", "封包捕获中：请在游戏内正常接取一个日常任务")
            tap.start()
            records = []
            try:
                deadline = time.monotonic() + 45.0
                while time.monotonic() < deadline and not stop_event.is_set():
                    records.extend(tap.poll())
                    if any(bytes(r.get("data") or b"")[:2] == b"\x0e\x00" for r in records):
                        break
                    stop_event.wait(0.2)
            finally:
                records.extend(tap.stop())
            after = []
            try:
                from app.core.task_api import list_accepted_task_ids
                after = list_accepted_task_ids(attach, log=lambda _m: None)
            except Exception:
                pass
            report = analyze_daily_accept_records(records, pid=int(mounted.pid), role_id=self._current_role_id())
            report["accepted_before"] = before
            report["accepted_after"] = after
            path = save_daily_accept_capture(report)
            msg = (
                "捕获完成：已记录稳定任务包尾部（仅诊断）"
                if report.get("verified")
                else "捕获完成：未发现稳定任务包尾部（仅诊断）"
            )
            msg += "；当前自动接取仍固定使用 FFFFFF"
            self._push("status", msg)
            self._push("log", f"自动任务 [接日常诊断] {msg} report={path}")
        self._attach_job("捕获接任务包", run)

    def _run_daily_accept(
        self, *, from_sync: bool, on_done: Callable[[bool, str], None] | None = None
    ) -> None:
        if from_sync and (self._busy or self._work_stop is not None and not self._work_stop.is_set()):
            if self._work_stop is not None:
                self._work_stop.set()
            try:
                self.var_status.set("收到主控接日常，正在中断当前任务…")
                self.after(
                    150,
                    lambda: self._run_daily_accept(
                        from_sync=True,
                        on_done=on_done,
                    ),
                )
            except Exception:
                pass
            return
        if not from_sync:
            # Fan out before local work: every account reads its own task/NPC state
            # and uses its own game process, so one failed role does not block peers.
            self._publish_task_sync(ACTION_ACCEPT_DAILY_TASKS, 0, name="接日常任务")
        def daily_log(message: str) -> None:
            text = str(message or "")
            self._push("log", text)
            if text.startswith("daily accept initial accepted="):
                pending_count = text.split("pending_count=", 1)[-1].split(" ", 1)[0]
                self.user_log(
                    f"执行接任务{pending_count}个" + ("（副控）" if from_sync else ""),
                    category=CAT_TASK,
                    source="自动任务",
                    dedupe_s=0.0,
                )

        def run(attach, mounted, stop_event) -> None:
            result = accept_daily_tasks_routed(
                attach,
                hwnd=int(getattr(mounted, "hwnd", 0) or 0),
                stop_event=stop_event,
                log=daily_log,
            )
            detail = result.error or result.note
            tag = "副控接日常" if from_sync else "接日常"
            self._push("status", f"{tag}成功" if result.ok else f"{tag}失败: {detail}")
            self._push("log", f"自动任务 [{tag}] {result.to_dict()}")
            self.user_log(
                f"执行接任务{len(result.accepted)}个"
                + ("完成" if result.ok else "失败"),
                category=CAT_TASK,
                source="自动任务",
                dedupe_s=0.0,
            )
            if result.ok and not from_sync:
                self._on_refresh()
            if from_sync and on_done is not None:
                on_done(bool(result.ok), str(detail))
        self._attach_job("副控接日常" if from_sync else "接日常任务", run)

    def _run_custom_routine_once(self, definition_id: str) -> None:
        """Run one built-in routine without persisting it to the plan queue."""
        mounted = self._require_session()
        if mounted is None:
            self.var_status.set("未挂载游戏，无法执行日常")
            return
        role_id = str(getattr(mounted, "role_id", "") or "").strip()
        if not role_id:
            self.var_status.set("角色身份未绑定完成，请稍候后重试")
            return
        sync_role_prefs_to_settings(self.settings, role_id)
        if self._busy or (self._runner is not None and self._runner.is_running()):
            self.var_status.set("当前操作进行中，请稍候")
            return
        definition = next(
            (
                row for row in self._custom_defs_for_ui()
                if str(row.get("definition_id") or "") == str(definition_id or "")
            ),
            None,
        )
        item = custom_queue_item(definition)
        if item is None or str(item.get("kind") or "") != "routine":
            self.var_status.set("该日常定义不可执行")
            return
        try:
            afk = tuple(
                float(self.settings.get(key)) if self.settings.get(key) is not None else None
                for key in ("qiegao_afk_x", "qiegao_afk_y", "qiegao_afk_z")
            )
        except (TypeError, ValueError):
            afk = (None, None, None)
        custom_ids = self.settings.get("schedule_custom_ids")
        self._runner = ScheduleTaskRunner(
            pid=int(mounted.pid),
            hwnd=int(getattr(mounted, "hwnd", 0) or 0),
            role_id=role_id,
            queue=[item],
            on_event=lambda event: self._push("event", event),
            log=lambda message: self._push("log", message),
            user_log=lambda message: self.user_log(message, category=CAT_TASK, source="自动任务"),
            qiegao_afk=afk,
            activity_busy_check=self._activity_page_busy,
            custom_ids=custom_ids if isinstance(custom_ids, dict) else None,
            activity_entry_cd_s=float(self.settings.get("activity_entry_cd_s") or 30.0),
            activity_return_poll_s=15.0,
            hang_settings=dict(self.settings),
            team_control_enabled=team_control_flag_enabled(self.settings),
            sync_action=lambda action, task_id, name: self._publish_task_sync(
                action, task_id, name=name
            ),
            sync_route_action=lambda action, task_id, **kwargs: self._publish_task_sync(
                action, task_id, **kwargs
            ),
        )
        self._runner.start()
        if not self._runner.is_running():
            self._runner = None
            self.var_status.set("日常未能启动（见日志）")
            return
        name = str(item.get("name") or definition_id)
        self.log(f"自动任务 [自定义] 手动启动 {name}")
        self.var_status.set(f"正在执行日常：{name}")
        self._set_busy(True)
        self._set_running(True)
        self._sync_runner_buttons(self._runner)

    def _on_complete(self) -> None:
        row = self._selected_task()
        if row is None:
            tid_raw = (self.var_task_id.get() or "").strip()
            if tid_raw:
                try:
                    row = {"task_id": int(tid_raw, 0)}
                except ValueError:
                    row = None
            if row is None:
                self.var_status.set("请先选择已接或填写任务ID")
                return
        if row.get("can_finish") is False:
            name = (row.get("name") or "").strip() or f"#{row.get('task_id')}"
            st = (row.get("status_text") or "进行中").strip()
            self.var_status.set(f"{name} 当前【{st}】不可交")
            return
        self._run_complete(row, from_sync=False)

    def _run_complete(
        self, row: dict, *, from_sync: bool = False, on_done: Callable[[bool, str], None] | None = None
    ) -> None:
        def run(attach, mounted, stop_event) -> None:
            # 主控：寻路+交；副控：不寻路，附近有 AwardNPC 才交
            result = complete_task_routed(
                attach,
                row,
                hwnd=int(getattr(mounted, "hwnd", 0) or 0),
                log=lambda m: self._push("log", m),
                fast=bool(from_sync),
                stop_event=stop_event,
            )
            if stop_event is not None and stop_event.is_set():
                self._push("status", "交付已取消")
                if on_done is not None:
                    on_done(False, "cancelled")
                return
            detail = result.error or result.note or ""
            tag = "副控交付" if from_sync else "交付"
            self._push(
                "status",
                f"{tag}成功" if result.ok else f"{tag}失败: {detail}",
            )
            self._push("log", f"自动任务 [{tag}] ok={result.ok} {detail}")
            if not from_sync:
                try:
                    tid = int(row.get("task_id") or 0)
                    nm = str(row.get("name") or "").strip()
                    who = f"「{nm}」" if nm else f"#{tid}"
                    self.user_log(
                        f"操作：交付任务 {who} "
                        + ("成功" if result.ok else f"失败 · {detail or '未知原因'}"),
                        category=CAT_TASK,
                        source="自动任务",
                        dedupe_s=0.3,
                    )
                except Exception:
                    pass
            if not from_sync:
                light = list_accepted_tasks_light(
                    attach, prev=self._tasks, log=lambda _m: None
                )
                self._push(
                    "tasks",
                    enrich_tasks_with_npc(
                        attach,
                        light,
                        radius=80.0,
                        live_scan=False,
                        stop_event=stop_event,
                        log=lambda _m: None,
                    ),
                )
            if result.ok and not from_sync:
                self._publish_task_sync(
                    ACTION_COMPLETE,
                    int(row.get("task_id") or 0),
                    can_finish=True,
                    name=str(row.get("name") or ""),
                )
            if from_sync and on_done is not None:
                on_done(bool(result.ok), str(detail))

        title = "副控交付" if from_sync else "交付任务"
        self._attach_job(title, run)

    def _apply_event(self, event) -> None:
        data = event.to_dict() if hasattr(event, "to_dict") else dict(event or {})
        phase = str(data.get("phase") or "")
        msg = str(data.get("message") or phase)
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
        idx = detail.get("index") if detail else None
        total = detail.get("total") if detail else None
        if idx is not None and total is not None:
            try:
                self.var_status.set(f"[{int(idx) + 1}/{int(total)}] {msg}")
            except Exception:
                self.var_status.set(msg)
        else:
            self.var_status.set(msg)
        if phase == "accept_ok":
            try:
                tid = int((detail or data).get("task_id") or 0)
            except Exception:
                tid = 0
            if tid and self._control_role() == ROLE_MASTER:
                self._publish_task_sync(ACTION_ACCEPT, tid)
        elif phase == "complete_ok":
            try:
                tid = int((detail or data).get("task_id") or 0)
            except Exception:
                tid = 0
            if tid and self._control_role() == ROLE_MASTER:
                self._publish_task_sync(
                    ACTION_COMPLETE,
                    tid,
                    can_finish=True,
                    name=str((detail or data).get("name") or ""),
                )
        if phase == "custom_status":
            defn_id = str(detail.get("definition_id") or "")
            if defn_id and self._list_tab == "计划":
                try:
                    self._paint_custom_list()
                except Exception:
                    pass
        if phase in ("paused", "resumed", "pause_pending", "blocked", "done", "running"):
            try:
                self._sync_runner_buttons(self._runner)
                if self._list_tab == "计划":
                    self._paint_custom_list()
            except Exception:
                pass
        if phase == "stopped":
            self._runner = None
            self._set_busy(False)
            self._set_running(False)
            self._on_refresh()
            self._paint_schedule_list()
            if self._list_tab == "计划":
                self._paint_custom_list()

    def _publish_fuzhou_fly_sync(self, members: str) -> bool:
        """Publish group-control 飞福州 for roster members. @author by ak"""
        try:
            from app.core.map_fly import PRESET_POINTS

            meta = PRESET_POINTS.get("fuzhou") or {}
            slot = int(meta.get("slot") or 0)
        except Exception:
            slot = 0
        return self._publish_task_sync(
            ACTION_MAP_FLY, int(slot) + 1, name="fuzhou", members=str(members or "")
        )

    def _build_team_service(self):
        """Shared auto-team service wired to this window's group control. @author by ak"""
        from app.core.team_ops import TeamFormService

        return TeamFormService(
            store=self.store,
            on_slave_leave=lambda members: self._publish_task_sync(
                ACTION_TEAM_LEAVE, 0, name="leave", members=str(members or "")
            ),
            on_slave_fly=self._publish_fuzhou_fly_sync,
            log=lambda m: self._push("log", m),
            status=lambda m: self._push("team_status", m),
        )

    def _on_start(self, from_schedule: bool = False) -> None:
        mounted = self._require_session()
        if mounted is None:
            self.var_status.set("未挂载游戏，无法执行计划")
            return
        if self._busy:
            self.var_status.set("当前操作进行中，请稍候")
            return
        if self._runner and self._runner.is_running():
            self.var_status.set("计划任务正在执行")
            return
        role_id = str(getattr(mounted, "role_id", "") or "").strip()
        if not role_id:
            self.var_status.set("角色身份未绑定完成，请稍候后重试")
            self.log("自动任务 [计划] blocked: role_id not bound")
            return
        sync_role_prefs_to_settings(self.settings, role_id)
        queue_rows = load_schedule_queue(self.settings)
        if not queue_rows:
            self.var_status.set("计划任务队列为空，请右键已接/计划加入")
            return
        if self._activity_page_busy():
            self.var_status.set("自动副本页正在运行，请先停止")
            self.log("自动任务 [推进] blocked: activity busy")
            return
        # Load this injection's persisted schedule plan before building the runner.
        self._load_schedule_profile_for_role(role_id)
        queue_rows = load_schedule_queue(self.settings)
        if not queue_rows:
            self.var_status.set("当前角色无计划任务配置")
            return
        self.var_status.set(f"正在启动计划任务（{len(queue_rows)} 项）…")
        self.log(f"自动任务 [计划] 请求启动 n={len(queue_rows)}")
        has_custom = any(
            str(x.get("source") or "").strip().lower() == "custom"
            for x in queue_rows
        )
        team_service = None
        team_targets = []
        team_members = ""
        if has_custom:
            # 队长(群控主控+名单就绪)才自动整队；否则本端单独执行，不带队。
            if self._team_has_group_control() and self._team_roster_ready(
                show_hint=False
            ):
                team_members = self._team_member_text()
                team_targets = self._verified_invite_targets()
                team_service = self._build_team_service()
            else:
                self.log(
                    "自动任务 [推进] 无整队配置（非队长/名单未就绪），仅执行本端任务"
                )
        ax = self.settings.get("qiegao_afk_x")
        ay = self.settings.get("qiegao_afk_y")
        az = self.settings.get("qiegao_afk_z")
        try:
            afk = (
                float(ax) if ax is not None else None,
                float(ay) if ay is not None else None,
                float(az) if az is not None else None,
            )
        except (TypeError, ValueError):
            afk = (None, None, None)
        cids = self.settings.get("schedule_custom_ids")
        entry_cd = float(self.settings.get("activity_entry_cd_s") or 30.0)

        self._runner = ScheduleTaskRunner(
            pid=int(mounted.pid),
            hwnd=int(getattr(mounted, "hwnd", 0) or 0),
            role_id=role_id,
            queue=queue_rows,
            on_event=lambda ev: self._push("event", ev),
            log=lambda message: self._push("log", message),
            user_log=lambda message: self.user_log(message, category=CAT_TASK, source="自动任务"),
            qiegao_afk=afk,
            activity_busy_check=self._activity_page_busy,
            team_service=team_service,
            team_targets=team_targets,
            team_members=team_members,
            custom_ids=cids if isinstance(cids, dict) else None,
            activity_entry_cd_s=entry_cd,
            activity_return_poll_s=15.0,
            hang_settings=dict(self.settings),
            team_control_enabled=team_control_flag_enabled(self.settings),
            sync_action=lambda action, task_id, name: self._publish_task_sync(
                action, task_id, name=name
            ),
            sync_route_action=lambda action, task_id, **kwargs: self._publish_task_sync(
                action, task_id, **kwargs
            ),
        )
        self._runner.start()
        if not self._runner.is_running():
            self._runner = None
            self.var_status.set("推进未能启动（见日志）")
            return
        tag = "定时到点" if from_schedule else "手动"
        self.log(f"自动任务 [计划] 启动({tag}) n={len(queue_rows)} custom={has_custom}")
        self.var_status.set(f"计划任务已启动（{len(queue_rows)} 项）")
        self._set_busy(True)
        self._set_running(True)
        self._sync_runner_buttons(self._runner)

    def _on_stop(self) -> None:
        """
        Cancel in-flight one-shot job or stop schedule/auto runner.

        @author by ak
        """
        cancelled = False
        if self._work_stop is not None and not self._work_stop.is_set():
            self._work_stop.set()
            cancelled = True
        self._job_gen += 1
        if self._runner is not None:
            stopped = bool(self._runner.stop())
            if stopped or not self._runner.is_running():
                self._runner = None
            cancelled = True
        self.var_status.set("已取消" if cancelled else "已停止")
        self._set_busy(False)
        self._set_running(False)
        try:
            self._sync_runner_buttons(None)
        except Exception:
            pass
        if cancelled:
            self.log("自动任务: 用户取消")


class LogPage(FeaturePage):
    """
    Formal 系统日志 — condensed operator-facing history for this game window.

    @author by ak
    """

    title = "系统日志"
    key = "logs"

    _FILTER_ALL = "all"

    def _build(self) -> None:
        bar = ttk.Frame(self, style="Panel.TFrame")
        bar.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(bar, text="系统日志", style="Panel.Header.TLabel").pack(side=tk.LEFT)
        self.var_count = tk.StringVar(value="0 条")
        ttk.Label(bar, textvariable=self.var_count, style="Panel.Muted.TLabel").pack(
            side=tk.LEFT, padx=(8, 0)
        )

        ttk.Button(bar, text="清空", width=6, command=self._on_clear).pack(side=tk.RIGHT)
        ttk.Button(bar, text="刷新", width=6, command=self._reload).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        self.var_autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动滚底", variable=self.var_autoscroll).pack(
            side=tk.RIGHT, padx=(0, 8)
        )

        tip = ttk.Label(
            self,
            text="只显示操作与进度；详细调试见开发者日志。",
            style="Panel.Muted.TLabel",
            wraplength=520,
            justify=tk.LEFT,
        )
        tip.pack(fill=tk.X, pady=(0, 6))

        filt = ttk.Frame(self, style="Panel.TFrame")
        filt.pack(fill=tk.X, pady=(0, 6))
        self._filter = self._FILTER_ALL
        self._filter_pairs = [(self._FILTER_ALL, "全部")] + [
            (k, CATEGORY_LABELS.get(k, k)) for k in FILTER_ORDER
        ]
        self._filter_label_to_key = {lab: key for key, lab in self._filter_pairs}
        self._filter_key_to_label = {key: lab for key, lab in self._filter_pairs}
        ttk.Label(filt, text="类型", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        self.var_filter = tk.StringVar(
            value=self._filter_key_to_label.get(self._FILTER_ALL, "全部")
        )
        self.cmb_filter = ttk.Combobox(
            filt,
            textvariable=self.var_filter,
            values=[lab for _k, lab in self._filter_pairs],
            width=10,
            state="readonly",
        )
        self.cmb_filter.pack(side=tk.LEFT, padx=(4, 0))
        self.cmb_filter.bind("<<ComboboxSelected>>", self._on_filter_selected)

        box = section(self, "记录")
        box.pack(fill=tk.BOTH, expand=True)
        host = ttk.Frame(box, style="Panel.TFrame")
        host.pack(fill=tk.BOTH, expand=True)
        self.text = make_text(host, height=12, mono=True)
        pack_scrollable_text(host, self.text)
        self.text.configure(state=tk.DISABLED)

        self._pending: list[UserLogEntry] = []
        self._buf: UserEventLog | None = None
        self._bound = False
        self._ui_job = None
        self.after(80, self._bind_buffer)
        self.after(200, self._drain_pending)

    def _resolve_buffer(self) -> UserEventLog | None:
        try:
            top = self.winfo_toplevel()
            buf = getattr(top, "user_events", None)
            if isinstance(buf, UserEventLog):
                return buf
        except Exception:
            pass
        return None

    def _bind_buffer(self) -> None:
        if self._bound:
            return
        buf = self._resolve_buffer()
        if buf is None:
            self.after(200, self._bind_buffer)
            return
        self._buf = buf
        try:
            buf.subscribe(self._on_entry)
        except Exception:
            pass
        self._bound = True
        self._reload()

    def _on_entry(self, entry: UserLogEntry) -> None:
        try:
            self._pending.append(entry)
        except Exception:
            pass

    def _drain_pending(self) -> None:
        try:
            if self._pending:
                batch = list(self._pending)
                self._pending.clear()
                filt = self._filter
                for entry in batch:
                    if filt != self._FILTER_ALL and entry.category != filt:
                        continue
                    self._append_line(entry.format_line())
                self._update_count()
        except Exception:
            pass
        try:
            if self.winfo_exists():
                self._ui_job = self.after(200, self._drain_pending)
        except Exception:
            self._ui_job = None

    def _on_filter_selected(self, _event=None) -> None:
        """Combobox 类型筛选. @author by ak"""
        lab = ""
        try:
            lab = (self.var_filter.get() or "").strip()
        except Exception:
            lab = ""
        key = self._filter_label_to_key.get(lab, self._FILTER_ALL)
        self._set_filter(key)

    def _set_filter(self, key: str) -> None:
        self._filter = key or self._FILTER_ALL
        lab = self._filter_key_to_label.get(self._filter, "全部")
        try:
            if (self.var_filter.get() or "").strip() != lab:
                self.var_filter.set(lab)
        except Exception:
            pass
        self._reload()

    def _reload(self) -> None:
        buf = self._buf or self._resolve_buffer()
        self._buf = buf
        lines: list[str] = []
        if buf is not None:
            cat = None if self._filter == self._FILTER_ALL else self._filter
            for entry in buf.snapshot(category=cat):
                lines.append(entry.format_line())
        try:
            self.text.configure(state=tk.NORMAL)
            self.text.delete("1.0", tk.END)
            if lines:
                self.text.insert("1.0", "\n".join(lines) + "\n")
            else:
                self.text.insert(
                    "1.0",
                    "暂无记录。启动自动宝箱 / 妖楼 / 活跃，或主控同步后会出现在这里。\n",
                )
            if bool(self.var_autoscroll.get()):
                self.text.see(tk.END)
            self.text.configure(state=tk.DISABLED)
        except Exception:
            pass
        self._update_count()

    def _append_line(self, line: str) -> None:
        if not line:
            return
        try:
            self.text.configure(state=tk.NORMAL)
            body = self.text.get("1.0", "end-1c")
            if body.startswith("暂无记录"):
                self.text.delete("1.0", tk.END)
            self.text.insert(tk.END, line + "\n")
            total = int(float(self.text.index("end-1c").split(".")[0]))
            if total > 420:
                self.text.delete("1.0", f"{total - 400}.0")
            if bool(self.var_autoscroll.get()):
                self.text.see(tk.END)
            self.text.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _update_count(self) -> None:
        n = 0
        buf = self._buf or self._resolve_buffer()
        if buf is not None:
            cat = None if self._filter == self._FILTER_ALL else self._filter
            n = len(buf.snapshot(category=cat))
        try:
            self.var_count.set(f"{n} 条")
        except Exception:
            pass

    def _on_clear(self) -> None:
        buf = self._buf or self._resolve_buffer()
        if buf is not None:
            try:
                buf.clear()
            except Exception:
                pass
        self._pending.clear()
        self._reload()

    def on_page_show(self) -> None:
        """Refresh when user opens 日志 tab. @author by ak"""
        if not self._bound:
            self._bind_buffer()
        else:
            self._reload()
