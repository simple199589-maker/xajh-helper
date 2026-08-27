# -*- coding: utf-8 -*-
"""
Per-game-window feature host: compact Toplevel bound to one mounted session.

@author by ak
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable

from app.core.auth_mock import AuthService
from app.core.captcha_client import DEFAULT_CAPTCHA_API_KEY, DEFAULT_CAPTCHA_BASE_URL
from app.core.session_store import GameSession, SessionStore
from app.ui.pages import (
    ActivityPage,
    GroceryPage,
    InputMkPage,
    LogPage,
    SettingsPage,
    SuperLootPage,
    TaskPage,
    YaoluPage,
)
from app.core.user_event_log import CAT_SYSTEM, UserEventLog
from app.ui.theme import apply_theme


class SessionFeatureWindow(tk.Toplevel):
    """
    Compact feature UI for a single injected game client.

    @author by ak
    """

    # Top-right live caption poll interval (ms).
    _HEADER_LIVE_MS = 1000
    _HEADER_MAX_LEN = 64

    def __init__(
        self,
        master: tk.Misc,
        store: SessionStore,
        session: GameSession,
        *,
        log_fn: Callable[[str], None] | None = None,
        on_close: Callable[[int], None] | None = None,
        on_business_invalid: Callable[[int, str], None] | None = None,
        auth: AuthService | None = None,
        debug_mode: bool | None = None,
        bridge_ready: bool = True,
        start_hidden: bool = False,
    ) -> None:
        super().__init__(master)
        self.store = store
        self.session = session
        self._log = log_fn or (lambda _m: None)
        # Formal operator log (精简生产日志), independent of debug file log.
        self.user_events = UserEventLog(maxlen=500)
        self._on_close = on_close
        self._on_business_invalid = on_business_invalid
        self.auth = auth
        self._auto_loot_allowed = bool(
            auth is None or getattr(auth, "auto_loot_allowed", True)
        )
        # debug_mode from login「开启调试」: logs + captcha crop files on disk.
        if debug_mode is None:
            try:
                from app.core.build_profile import default_debug_mode

                debug_mode = bool(default_debug_mode())
            except Exception:
                debug_mode = True
        self.debug_mode = bool(debug_mode)
        self._bridge_ready = bool(bridge_ready)
        self._start_hidden = bool(start_hidden)
        # login_card_key is retained for license verify/unbind only. Business
        # requests use the in-memory login_token below.
        _login_card = ""
        _login_token = ""
        _cloud_control_available = True
        try:
            if auth is not None and getattr(auth, "session", None) is not None:
                _login_card = str(getattr(auth.session, "key", "") or "").strip()
                _login_token = str(getattr(auth.session, "token", "") or "").strip()
                _cloud_control_available = not bool(
                    getattr(auth.session, "local_mock_card", False)
                )
        except Exception:
            _login_card = ""
        if not _login_card:
            try:
                from app.core.auth_mock import last_login_key

                _login_card = str(last_login_key() or "").strip()
            except Exception:
                _login_card = ""
        try:
            from app.core.team_ops import load_team_prefs

            _team_prefs = load_team_prefs()
        except Exception:
            _team_prefs = {
                "team_members": "",
                "team_verified_text": "",
                "team_verified_roster": [],
            }
        try:
            from app.core.captcha_prefs import load_captcha_api_key

            _captcha_api_key = load_captcha_api_key() or DEFAULT_CAPTCHA_API_KEY
        except Exception:
            _captcha_api_key = DEFAULT_CAPTCHA_API_KEY
        self.settings: dict = {
            "captcha_base_url": DEFAULT_CAPTCHA_BASE_URL,
            "captcha_api_key": _captcha_api_key,
            "captcha_api_key_dirty": False,
            "login_card_key": _login_card,
            "login_token": _login_token,
            "cloud_control_available": _cloud_control_available,
            "debug_mode": self.debug_mode,
            "debug_save_crops": self.debug_mode,
            # Per-window task multi-control: none | master | slave (default 无控)
            "task_control_role": "none",
            "task_schedule_queue": [],
            "task_schedule_hour": 10,
            "task_schedule_minute": 0,
            "task_schedule_last_run_date": "",
            "task_schedule_enabled": False,
            "task_schedule_owner_id": "",
            "task_schedule_loaded_owner": "",
            "schedule_custom_ids": {},
            # Cloud control: master/slave by master_name; token is injected above.
            "cloud_control_enabled": False,
            "cloud_control_master_name": "",
            # 队内控: 通过游戏队伍频道文本消息进行主/副控同步（与云控互斥）。
            "team_control_enabled": False,
            # Team roster for multi-box invite / gather (disk-backed)
            "team_members": str(_team_prefs.get("team_members") or ""),
            "team_enabled": False,
            # Verified id cache: fingerprint + [{token,name,obj_id,source,is_self,pid}]
            "team_verified_text": str(_team_prefs.get("team_verified_text") or ""),
            "team_verified_roster": list(_team_prefs.get("team_verified_roster") or []),
            # False while Delete inject is still running for this window.
            "bridge_ready": self._bridge_ready,
        }
        self._pages: dict[str, ttk.Frame] = {}
        self._nav: dict[str, ttk.Button] = {}
        self._current = "settings"
        # Live header parts: map · role · pos · scene_id
        self._hdr_map: str = ""
        self._hdr_role: str = ""
        self._hdr_rid: str = str(getattr(session, "role_id", "") or "").strip()
        self._hdr_pos: tuple[float, float, float] | None = None
        self._hdr_scene_id: int | None = None
        self._hdr_dead: bool | None = None
        # Which live-header slots are currently packed: (map, role, pos)
        self._hdr_pack_sig: tuple[bool, bool, bool, bool] = (False, False, False, False)
        self._header_live_job: object | None = None
        self._header_live_busy = False
        self._business_invalid_notified = False
        self._closed = False
        # Running feature keys for game-window title tags (order preserved).
        self._active_keys: list[str] = []
        self._loading_bar: ttk.Frame | None = None
        self._var_loading: tk.StringVar | None = None
        self._progress: ttk.Progressbar | None = None

        apply_theme(self)
        title = session.title or session.original_title or "xajh"
        self.title(f"功能 · {session.pid}" + ("" if self._bridge_ready else " · 注入中"))
        self.geometry("680x480")
        self.minsize(600, 420)
        try:
            self._build()
        except Exception:
            # Avoid orphan Toplevel (looks like two 功能窗 after retry).
            try:
                self.destroy()
            except Exception:
                pass
            raise
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._show("settings")
        if self._bridge_ready:
            self._start_header_live()
        else:
            self.set_inject_phase("prepare", "正在注入桥接，请稍候…")
            self._set_features_enabled(False)
        if self._start_hidden:
            try:
                self.withdraw()
            except Exception:
                pass
            self._log(
                f"功能窗已后台就绪 pid={session.pid} hwnd=0x{session.hwnd:X} "
                f"ready={self._bridge_ready} {title!r}（UI 隐藏）"
            )
        else:
            try:
                self.lift()
                self.attributes("-topmost", True)
                self.after(250, self._clear_own_topmost)
                self.focus_force()
            except Exception:
                pass
            self._log(
                f"功能窗已打开 pid={session.pid} hwnd=0x{session.hwnd:X} "
                f"ready={self._bridge_ready} {title!r}"
            )
        try:
            ready_txt = "已就绪" if self._bridge_ready else "注入中"
            if self._start_hidden:
                self.append_user_log(
                    f"功能窗后台就绪 · {ready_txt} · PID {session.pid}（UI 隐藏）",
                    category=CAT_SYSTEM,
                    source="系统",
                )
            else:
                self.append_user_log(
                    f"功能窗已打开 · {ready_txt} · PID {session.pid}",
                    category=CAT_SYSTEM,
                    source="系统",
                )
        except Exception:
            pass

    def _build(self) -> None:
        """
        Build nav + content; top-right shows live map/role/pos.

        @author by ak
        """
        shell = ttk.Frame(self)
        shell.pack(fill=tk.BOTH, expand=True)

        nav = ttk.Frame(shell, style="Nav.TFrame", width=96)
        nav.pack(side=tk.LEFT, fill=tk.Y)
        nav.pack_propagate(False)

        brand = ttk.Frame(nav, style="Nav.TFrame")
        brand.pack(fill=tk.X, padx=8, pady=(10, 6))
        ttk.Label(brand, text="功能", style="NavBrand.TLabel").pack(anchor="w")
        ttk.Label(
            brand,
            text=f"PID {self.session.pid}",
            style="NavTitle.TLabel",
        ).pack(anchor="w")

        items = [
            ("grocery", "杂货使用"),
            ("yaolu", "九层妖楼"),
            ("activity", "自动副本"),
            ("task", "自动任务"),
            ("input_mk", "鼠标/键盘"),
            ("settings", "快捷设置"),
            ("logs", "系统日志"),
        ]
        if self._auto_loot_allowed:
            items.insert(0, ("loot", "自动宝箱"))
        for key, label in items:
            btn = ttk.Button(
                nav,
                text=label,
                style="Nav.TButton",
                command=lambda k=key: self._show(k),
            )
            btn.pack(fill=tk.X, padx=6, pady=1)
            self._nav[key] = btn

        ttk.Frame(nav, style="Nav.TFrame").pack(fill=tk.BOTH, expand=True)
        ttk.Button(nav, text="关闭", style="Nav.TButton", command=self._close).pack(
            fill=tk.X, padx=6, pady=(0, 8)
        )

        right = ttk.Frame(shell, style="Content.TFrame")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        top = ttk.Frame(right, style="Top.TFrame")
        top.pack(fill=tk.X)
        top_inner = ttk.Frame(top, style="Top.TFrame")
        top_inner.pack(fill=tk.X, padx=10, pady=6)
        self.var_title = tk.StringVar(value="快捷设置")
        ttk.Label(
            top_inner, textvariable=self.var_title, style="Top.Header.TLabel"
        ).pack(side=tk.LEFT)
        # Live: map · 角色名(可点击复制) · x,y,z
        self._hdr_bar = ttk.Frame(top_inner, style="Top.TFrame")
        self._hdr_bar.pack(side=tk.RIGHT)
        self.var_hdr_map = tk.StringVar(value="-")
        self.var_hdr_role = tk.StringVar(value="")
        self.var_hdr_rid = tk.StringVar(value=self._hdr_rid)
        self.var_hdr_pos = tk.StringVar(value="")
        # Back-compat: full composed caption (tests / external readers).
        self.var_map = tk.StringVar(value="-")
        self.lbl_hdr_map = ttk.Label(
            self._hdr_bar, textvariable=self.var_hdr_map, style="Top.TLabel"
        )
        self.lbl_hdr_sep1 = ttk.Label(
            self._hdr_bar, text=" · ", style="Top.TLabel"
        )
        self.lbl_hdr_role = ttk.Label(
            self._hdr_bar,
            textvariable=self.var_hdr_role,
            style="Top.Link.TLabel",
            cursor="hand2",
        )
        self.lbl_hdr_sep_role = ttk.Label(
            self._hdr_bar, text=" (", style="Top.TLabel"
        )
        self.lbl_hdr_rid = ttk.Label(
            self._hdr_bar, textvariable=self.var_hdr_rid, style="Top.Link.TLabel", cursor="hand2"
        )
        self.lbl_hdr_sep_rid = ttk.Label(
            self._hdr_bar, text=")", style="Top.TLabel"
        )
        self.lbl_hdr_sep2 = ttk.Label(
            self._hdr_bar, text=" · ", style="Top.TLabel"
        )
        self.lbl_hdr_pos = ttk.Label(
            self._hdr_bar, textvariable=self.var_hdr_pos, style="Top.TLabel"
        )
        self.lbl_hdr_map.pack(side=tk.LEFT)
        self.lbl_hdr_role.bind("<Button-1>", self._on_copy_role_name)
        self.lbl_hdr_rid.bind("<Button-1>", self._on_copy_role_id)
        self.lbl_hdr_rid.bind("<Enter>", lambda _e: self._set_role_link_hover(True))
        self.lbl_hdr_rid.bind("<Leave>", lambda _e: self._set_role_link_hover(False))
        self.lbl_hdr_role.bind(
            "<Enter>",
            lambda _e: self._set_role_link_hover(True),
        )
        self.lbl_hdr_role.bind(
            "<Leave>",
            lambda _e: self._set_role_link_hover(False),
        )

        ttk.Separator(right, orient=tk.HORIZONTAL).pack(fill=tk.X)

        # Inject loading strip (hidden when bridge ready).
        self._loading_bar = ttk.Frame(right, style="Content.TFrame", padding=(10, 6))
        self._var_loading = tk.StringVar(value="正在连接…")
        load_row = ttk.Frame(self._loading_bar, style="Content.TFrame")
        load_row.pack(fill=tk.X)
        ttk.Label(
            load_row,
            textvariable=self._var_loading,
            style="Muted.TLabel",
        ).pack(side=tk.LEFT, anchor="w")
        self._progress = ttk.Progressbar(
            self._loading_bar, mode="indeterminate", length=200
        )
        self._progress.pack(fill=tk.X, pady=(6, 0))
        if not self._bridge_ready:
            self._loading_bar.pack(fill=tk.X)
            try:
                self._progress.start(12)
            except Exception:
                pass

        self.container = ttk.Frame(
            right, style="Content.TFrame", padding=(10, 8, 10, 8)
        )
        self.container.pack(fill=tk.BOTH, expand=True)

        pid = int(self.session.pid)
        page_kw = dict(
            log_fn=self._log,
            fixed_pid=pid,
            auth=self.auth,
            settings=self.settings,
        )
        self._page_loot = (
            SuperLootPage(self.container, self.store, **page_kw)
            if self._auto_loot_allowed
            else None
        )
        self._page_grocery = GroceryPage(self.container, self.store, **page_kw)
        self._page_yaolu = YaoluPage(self.container, self.store, **page_kw)
        self._page_activity = ActivityPage(self.container, self.store, **page_kw)
        self._page_task = TaskPage(self.container, self.store, **page_kw)
        self._page_logs = LogPage(self.container, self.store, **page_kw)
        self._page_input_mk = InputMkPage(self.container, self.store, **page_kw)
        self._page_settings = SettingsPage(self.container, self.store, **page_kw)
        self._pages = {
            "grocery": self._page_grocery,
            "yaolu": self._page_yaolu,
            "activity": self._page_activity,
            "task": self._page_task,
            "input_mk": self._page_input_mk,
            "settings": self._page_settings,
            "logs": self._page_logs,
        }
        if self._page_loot is not None:
            self._pages["loot"] = self._page_loot


    def append_user_log(
        self,
        message: str,
        *,
        category: str = "op",
        source: str = "",
        dedupe_s: float = 0.8,
    ) -> None:
        """
        Append one formal operator log line for this feature window.

        @author by ak
        """
        try:
            self.user_events.append(
                message,
                category=category,
                source=source,
                dedupe_s=float(dedupe_s),
            )
        except Exception:
            pass

    def apply_external_task_role(self, role: str, team: bool | None = None) -> bool:
        """
        Apply task-control role from peer window (一键副控).

        Updates settings, settings-page UI, hub registration, task listener, title.
        team=None 保留现有队内控标志；True/False 显式设/清（一键副控时启用队内控）。
        @author by ak
        """
        role = str(role or "none").strip().lower()
        if role not in ("none", "master", "slave"):
            role = "none"
        try:
            self.settings["task_control_role"] = role
            if team is not None:
                self.settings["team_control_enabled"] = bool(team)
        except Exception:
            pass
        applied = False
        try:
            pages = getattr(self, "_pages", None) or {}
            sp = pages.get("settings") if isinstance(pages, dict) else None
            if sp is not None and hasattr(sp, "apply_role_from_external"):
                try:
                    sp.apply_role_from_external(role, team=team)
                except TypeError:
                    sp.apply_role_from_external(role)
                applied = True
            else:
                from app.core.task_sync import get_task_sync_hub

                pid = int(getattr(self.session, "pid", 0) or 0)
                if pid:
                    get_task_sync_hub().set_role(pid, role)
                tp = pages.get("task") if isinstance(pages, dict) else None
                if tp is not None and hasattr(tp, "_bind_task_sync"):
                    tp._bind_task_sync()
                    applied = True
        except Exception:
            applied = False
        # Keep window title role tag in sync without forcing page rebuild.
        try:
            role_tag = {"master": "主控", "slave": "副控", "none": "无控"}.get(role, role)
            cur = str(getattr(self, "_current", "") or "settings")
            titles = {
                "loot": "自动宝箱",
                "grocery": "杂货使用",
                "yaolu": "九层妖楼",
                "activity": "自动副本",
                "task": "自动任务",
                "input_mk": "鼠标/键盘",
                "logs": "系统日志",
                "settings": "快捷设置",
            }
            base = titles.get(cur, cur)
            if getattr(self, "var_title", None) is not None:
                self.var_title.set(f"{base} · {role_tag}")
        except Exception:
            pass
        # Settings page already writes 群控角色切换 user log when UI is present.
        if not applied:
            try:
                role_lab = {"master": "主控", "slave": "副控", "none": "无控"}.get(role, role)
                self.append_user_log(
                    f"群控：已由其它窗口一键设为「{role_lab}」并保存",
                    category="control",
                    source="群控",
                    dedupe_s=0.0,
                )
            except Exception:
                pass
        return bool(applied)

    def _show(self, name: str) -> None:
        """Switch feature tab. @author by ak"""
        # 日志页在注入中也可看；其它功能页仍锁定。
        if (
            not self._bridge_ready
            and name != "logs"
            and name != getattr(self, "_current", name)
        ):
            try:
                if self._var_loading is not None:
                    self._var_loading.set("注入未完成，功能暂不可用")
            except Exception:
                pass
            return
        titles = {
            "loot": "自动宝箱",
            "grocery": "杂货使用",
            "yaolu": "九层妖楼",
            "activity": "自动副本",
            "task": "自动任务",
            "input_mk": "鼠标/键盘",
            "logs": "系统日志",
            "settings": "快捷设置",
        }
        for key, frame in self._pages.items():
            try:
                frame.pack_forget()
            except Exception:
                pass
        page = self._pages.get(name) or self._pages["settings"]
        name = name if name in self._pages else "settings"
        page.pack(fill=tk.BOTH, expand=True)
        self._current = name
        role = str(self.settings.get("task_control_role") or "none")
        role_tag = {"master": "主控", "slave": "副控", "none": "无控"}.get(role, role)
        base_title = titles.get(name, name)
        self.var_title.set(f"{base_title} · {role_tag}")
        for key, btn in self._nav.items():
            btn.configure(
                style="NavActive.TButton" if key == name else "Nav.TButton"
            )
        # Re-register task sync whenever user opens 自动任务 / 设置
        if name in ("task", "settings"):
            try:
                tp = self._pages.get("task")
                if tp is not None and hasattr(tp, "_bind_task_sync"):
                    tp._bind_task_sync()
            except Exception:
                pass
        if hasattr(page, "refresh_sessions"):
            try:
                page.refresh_sessions()
            except Exception:
                pass
        # Enter-page hook (e.g. 自动任务 auto-refresh once)
        if hasattr(page, "on_page_show"):
            try:
                page.on_page_show()
            except Exception:
                pass

    def set_inject_phase(self, phase: str, text: str) -> None:
        """
        Update loading banner text during inject (UI thread).

        @author by ak
        """
        msg = (text or phase or "正在注入…").strip() or "正在注入…"
        try:
            if self._var_loading is not None:
                self._var_loading.set(msg)
        except Exception:
            pass
        # 正式日志不刷注入过程（准备/检查/连接…）；仅在最终就绪/失败时记一条
        try:
            ph = str(phase or "").lower()
            if ph in ("ready", "done", "ok", "fail", "error", "failed"):
                self.append_user_log(
                    msg, category=CAT_SYSTEM, source="注入", dedupe_s=2.0
                )
        except Exception:
            pass
        try:
            if self._loading_bar is not None and not self._bridge_ready:
                if not self._loading_bar.winfo_ismapped():
                    self._loading_bar.pack(fill=tk.X, before=self.container)
                if self._progress is not None:
                    try:
                        self._progress.start(12)
                    except Exception:
                        pass
        except Exception:
            pass

    def set_bridge_ready(
        self,
        ready: bool,
        *,
        text: str = "",
        session: GameSession | None = None,
    ) -> None:
        """
        Unlock or re-lock features after inject finishes.

        @author by ak
        """
        ready = bool(ready)
        self._bridge_ready = ready
        self.settings["bridge_ready"] = ready
        if session is not None:
            self.session = session
        try:
            self.title(
                f"功能 · {int(self.session.pid)}"
                + ("" if ready else " · 注入中")
            )
        except Exception:
            pass
        if ready:
            try:
                if self._progress is not None:
                    self._progress.stop()
            except Exception:
                pass
            try:
                if self._loading_bar is not None:
                    self._loading_bar.pack_forget()
            except Exception:
                pass
            self._set_features_enabled(True)
            # The initial default page may have been entered while injection
            # was still in progress, before the live role id was readable.
            # Re-enter the current page now so per-role data (including the
            # persistent ignore list) is refreshed without a manual tab click.
            try:
                self._show(str(self._current or "settings"))
            except Exception:
                pass
            # Clear a stale page status produced while the provisional window
            # was still marked as injecting.
            for page in list(self._pages.values()):
                try:
                    status = getattr(page, "var_status", None)
                    if status is not None and "注入" in str(status.get() or ""):
                        status.set("")
                except Exception:
                    pass
            # Idle-time product hooks (KEY_HOLD) after inject completes.
            for page in list(self._pages.values()):
                cb = getattr(page, "on_bridge_ready", None)
                if callable(cb):
                    try:
                        cb()
                    except Exception:
                        pass
            if not self._header_live_job:
                self._start_header_live()
            if text:
                self._log(text)
            try:
                self.append_user_log(
                    text or "注入完成，功能已解锁",
                    category=CAT_SYSTEM,
                    source="系统",
                )
            except Exception:
                pass
        else:
            self.set_inject_phase("busy", text or "正在注入…")
            self._set_features_enabled(False)
            self._stop_header_live()

    def _set_features_enabled(self, enabled: bool) -> None:
        """
        Enable/disable nav + common action widgets while inject is in flight.

        @author by ak
        """
        st = tk.NORMAL if enabled else tk.DISABLED
        for key, btn in self._nav.items():
            try:
                # 日志始终可点，便于查看注入/群控过程。
                btn.configure(state=tk.NORMAL if (enabled or key == "logs") else tk.DISABLED)
            except Exception:
                pass
        # Common action button attrs on feature pages.
        names = (
            "btn_start",
            "btn_stop",
            "btn_once",
            "btn_scan",
            "btn_accept",
            "btn_complete",
            "btn_pathfind",
            "btn_refresh",
            "btn_buy",
            "btn_use",
            "btn_shift_start",
            "btn_key_start",
            "btn_fg_start",
            "btn_click_l_start",
            "btn_click_r_start",
            "btn_sc_start",
        )
        for page in list(self._pages.values()):
            for name in names:
                w = getattr(page, name, None)
                if w is None:
                    continue
                try:
                    if not enabled:
                        w.configure(state=tk.DISABLED)
                    elif name == "btn_stop":
                        # Stop stays disabled until a runner starts.
                        w.configure(state=tk.DISABLED)
                    else:
                        w.configure(state=tk.NORMAL)
                except Exception:
                    pass
        # Activity tab: pull live flourish points when shown idle.
        if name == "activity" and hasattr(page, "_refresh_live_snapshot"):
            try:
                page._refresh_live_snapshot()
            except Exception:
                pass

    def notify_activity(self, key: str, running: bool) -> None:
        """
        Feature page reports start/stop so the game window title shows activity.

        Idle: ``[GUI]``; running: ``[捡箱子]`` / ``[妖楼]`` / multi ``[捡箱子·妖楼]``.
        @author by ak
        """
        k = (key or "").strip().lower()
        if not k:
            return
        if running:
            if k not in self._active_keys:
                self._active_keys.append(k)
        else:
            self._active_keys = [x for x in self._active_keys if x != k]
        self._sync_game_title_marker()

    def _sync_game_title_marker(self) -> None:
        """
        Apply current activity set to the game client window title.

        Loot includes open-success count when >0: [捡箱子×12].
        Yaolu includes confirmed enter count when >0: [妖楼×12].
        @author by ak
        """
        from app.core.window_title import (
            activity_label_for_key,
            set_game_activity_markers,
        )

        hwnd = int(self.session.hwnd or 0)
        if not hwnd:
            return
        labels: list[str] = []
        for k in self._active_keys:
            lab = activity_label_for_key(k)
            if not lab:
                continue
            if k == "loot":
                try:
                    n = int(getattr(self._page_loot, "_open_ok_count", 0) or 0)
                except Exception:
                    n = 0
                if n > 0:
                    lab = f"{lab}×{n}"
            elif k == "yaolu":
                # Count only confirmed 进本 (scene enter), not captcha-ok alone.
                try:
                    n = int(getattr(self._page_yaolu, "_enter_ok_count", 0) or 0)
                except Exception:
                    n = 0
                if n > 0:
                    lab = f"{lab}×{n}"
            labels.append(lab)
        try:
            set_game_activity_markers(hwnd, labels)
        except Exception:
            pass

    def set_map_label(self, label: str) -> None:
        """
        Update map part of top-right caption (role/pos kept).

        Example map text: "绿竹林 (a11)".
        @author by ak
        """
        text = (label or "").strip()
        if not text or text == "-":
            return
        self._hdr_map = text
        self._render_header()

    def set_live_status(
        self,
        *,
        map_label: str | None = None,
        role_name: str | None = None,
        pos: tuple[float, float, float] | None = None,
        scene_id: int | None = None,
        dead: bool | None = None,
        publish: bool = True,
        source: str = "header",
        touch_dead: bool = False,
    ) -> None:
        """
        Update any subset of the live header parts and re-render.

        Also publishes to LiveSceneHub (single map/pos/death producer) so other
        features can get()/subscribe without re-scanning memory.

        @author by ak
        """
        if map_label is not None:
            t = (map_label or "").strip()
            if t and t != "-":
                self._hdr_map = t
                self.session.map_label = t
            elif scene_id is not None:
                # scene changed with empty label: drop stale map text in UI too
                prev = getattr(self, "_hdr_scene_id", None)
                try:
                    if prev is not None and int(prev) != int(scene_id):
                        self._hdr_map = ""
                except Exception:
                    pass
        if role_name is not None:
            t = (role_name or "").strip()
            if t and t != "-":
                self._hdr_role = t
                self.session.role_name = t
        if pos is not None and len(pos) >= 3:
            try:
                self._hdr_pos = (float(pos[0]), float(pos[1]), float(pos[2]))
            except Exception:
                pass
        if scene_id is not None:
            try:
                new_sid = int(scene_id)
                prev = getattr(self, "_hdr_scene_id", None)
                if prev is not None and int(prev) != new_sid and not (
                    map_label and str(map_label).strip() not in ("", "-")
                ):
                    # avoid keeping old city name when only id flips
                    if map_label is None:
                        self._hdr_map = ""
                self._hdr_scene_id = new_sid
                self.session.scene_id = new_sid
            except Exception:
                pass
        if touch_dead or dead is not None:
            self._hdr_dead = None if dead is None else bool(dead)
        self._render_header()
        if publish:
            try:
                from app.core.live_scene_hub import publish_live_scene

                publish_live_scene(
                    int(self.session.pid),
                    scene_id=getattr(self, "_hdr_scene_id", None),
                    scene_label=self._hdr_map or None,
                    role_name=self._hdr_role or None,
                    pos=self._hdr_pos,
                    dead=getattr(self, "_hdr_dead", None),
                    source=str(source or "header"),
                    touch_dead=bool(touch_dead or dead is not None),
                )
            except Exception:
                pass

    def get_live_scene(self, *, max_age_s: float | None = 45.0):
        """Active query of session live scene cache. @author by ak"""
        try:
            from app.core.live_scene_hub import get_live_scene

            return get_live_scene(int(self.session.pid), max_age_s=max_age_s)
        except Exception:
            return None

    def _render_header(self) -> None:
        """
        Compose top-right caption: map · role(click-to-copy) · x,y,z.

        @author by ak
        """
        map_t = (self._hdr_map or "").strip()
        role_t = (self._hdr_role or "").strip()
        rid_t = (self._hdr_rid or str(getattr(self.session, "role_id", "") or "")).strip()
        pos_t = ""
        if self._hdr_pos is not None:
            x, y, z = self._hdr_pos
            pos_t = f"{x:.1f},{y:.1f},{z:.1f}"

        parts: list[str] = []
        if map_t:
            parts.append(map_t)
        if role_t:
            parts.append(f"{role_t} ({rid_t})" if rid_t else role_t)
        if pos_t:
            parts.append(pos_t)
        full = " · ".join(parts) if parts else "-"
        if len(full) > self._HEADER_MAX_LEN:
            full = full[: self._HEADER_MAX_LEN - 3] + "..."

        try:
            self.var_map.set(full)
            self.var_hdr_map.set(map_t or ("-" if not role_t and not pos_t else ""))
            # Keep role visible for click-copy; truncate only display if needed.
            self.var_hdr_role.set(role_t)
            self.var_hdr_rid.set(rid_t)
            self.var_hdr_pos.set(pos_t)
        except Exception:
            pass

        # Rebuild layout only when visible slots change (avoid 1s flicker).
        show_map = bool(map_t) or (not role_t and not pos_t)
        show_role = bool(role_t)
        show_rid = bool(rid_t)
        show_pos = bool(pos_t)
        sig = (show_map, show_role, show_rid, show_pos)
        if sig != self._hdr_pack_sig:
            self._hdr_pack_sig = sig
            try:
                for w in (
                    self.lbl_hdr_map,
                    self.lbl_hdr_sep1,
                    self.lbl_hdr_role,
                    self.lbl_hdr_sep_role,
                    self.lbl_hdr_rid,
                    self.lbl_hdr_sep_rid,
                    self.lbl_hdr_sep2,
                    self.lbl_hdr_pos,
                ):
                    try:
                        w.pack_forget()
                    except Exception:
                        pass
                if show_map:
                    self.lbl_hdr_map.pack(side=tk.LEFT)
                if show_role:
                    if show_map:
                        self.lbl_hdr_sep1.pack(side=tk.LEFT)
                    self.lbl_hdr_role.pack(side=tk.LEFT)
                    if show_rid:
                        self.lbl_hdr_sep_role.pack(side=tk.LEFT)
                        self.lbl_hdr_rid.pack(side=tk.LEFT)
                        self.lbl_hdr_sep_rid.pack(side=tk.LEFT)
                if show_pos:
                    if show_map or show_role:
                        sep = self.lbl_hdr_sep2 if show_role else self.lbl_hdr_sep1
                        sep.pack(side=tk.LEFT)
                    self.lbl_hdr_pos.pack(side=tk.LEFT)
            except Exception:
                pass

    def _set_role_link_hover(self, hovering: bool) -> None:
        """Accent highlight while hovering the nickname. @author by ak"""
        try:
            from app.ui.theme import C

            color = C["accent_hi"] if hovering else C["accent"]
            self.lbl_hdr_role.configure(foreground=color)
        except Exception:
            pass

    def _on_copy_role_id(self, _event: object | None = None) -> None:
        """Click the visible role id to copy it to clipboard."""
        rid = (self._hdr_rid or str(getattr(self.session, "role_id", "") or "")).strip()
        if not rid:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(rid)
            self.update_idletasks()
            self._log(f"已复制角色 RID: {rid}")
        except Exception as e:
            self._log(f"复制角色 RID 失败: {e}")

    def _on_copy_role_name(self, _event: object | None = None) -> None:
        """
        Click nickname in live header -> copy role name to clipboard.

        @author by ak
        """
        name = (self._hdr_role or "").strip()
        if not name:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(name)
            try:
                # Keep selection on some Windows Tk builds.
                self.update_idletasks()
            except Exception:
                pass
        except Exception as e:
            try:
                self._log(f"复制昵称失败: {e}")
            except Exception:
                pass
            return
        # Brief green flash (does not fight live role text refresh).
        try:
            from app.ui.theme import C

            self.lbl_hdr_role.configure(foreground=C["ok"])
            self.after(700, lambda: self._set_role_link_hover(False))
        except Exception:
            pass
        try:
            self._log(f"已复制昵称: {name}")
        except Exception:
            pass

    def _drop_live_scene_hub(self) -> None:
        """Release per-pid live scene hub when window closes. @author by ak"""
        try:
            from app.core.live_scene_hub import drop_live_scene_hub

            drop_live_scene_hub(int(self.session.pid))
        except Exception:
            pass

    def _start_header_live(self) -> None:
        """
        Begin periodic live header poll (map + role + pos).

        @author by ak
        """
        self._stop_header_live()
        self._header_live_busy = False
        try:
            self._header_live_job = self.after(200, self._header_live_tick)
        except Exception:
            self._header_live_job = None

    def _stop_header_live(self) -> None:
        """Cancel live header timer. @author by ak"""
        job = self._header_live_job
        self._header_live_job = None
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        self._header_live_busy = False

    def _header_live_tick(self) -> None:
        """
        Schedule one background read of scene/role/pos for the header.

        @author by ak
        """
        self._header_live_job = None
        if self._closed:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if self._header_live_busy:
            try:
                self._header_live_job = self.after(
                    self._HEADER_LIVE_MS, self._header_live_tick
                )
            except Exception:
                pass
            return
        self._header_live_busy = True
        pid = int(self.session.pid)
        hwnd = int(getattr(self.session, "hwnd", 0) or 0)

        def worker() -> None:
            sample: dict = {}
            host_present: bool | None = None
            bridge = None
            try:
                from app.core.xajh_bridge import ensure_bridge

                bridge = ensure_bridge(
                    pid,
                    hwnd=hwnd or None,
                    inject_if_needed=False,
                    log=lambda _m: None,
                )
                try:
                    if bridge is not None:
                        result = bridge.host_snapshot(
                            hwnd=hwnd or None, timeout_ms=1500
                        )
                        if result.ok:
                            host_present = bool(int(result.ret or 0))
                            scene_id = int(getattr(result, "mode", 0) or 0)
                            try:
                                from app.core.remote_runtime import note_pid_scene_snapshot

                                note_pid_scene_snapshot(
                                    pid,
                                    host_present=host_present,
                                    scene_id=scene_id,
                                )
                            except Exception:
                                pass
                            if host_present:
                                import math

                                xyz = (
                                    float(getattr(result, "x", 0.0) or 0.0),
                                    float(getattr(result, "y", 0.0) or 0.0),
                                    float(getattr(result, "z", 0.0) or 0.0),
                                )
                                if all(math.isfinite(v) and abs(v) < 1.0e7 for v in xyz):
                                    sample["pos"] = xyz
                                if scene_id > 0:
                                    sample["scene_id"] = scene_id
                                    try:
                                        from app.core.map_names import format_scene_display

                                        label = format_scene_display(scene_id)
                                        if label and label != "-":
                                            sample["scene_label"] = str(label)
                                    except Exception:
                                        pass
                finally:
                    if bridge is not None:
                        bridge.close()
            except Exception:
                host_present = None
            if host_present:
                try:
                    from app.core.window_title import (
                        get_window_title,
                        parse_role_name_from_title,
                    )

                    title = get_window_title(hwnd) if hwnd else ""
                    if not title:
                        title = str(getattr(self.session, "title", "") or "")
                    if not title:
                        title = str(
                            getattr(self.session, "original_title", "") or ""
                        )
                    role = parse_role_name_from_title(title)
                    if role:
                        sample["role_name"] = role
                except Exception:
                    pass
            # HOST_SNAPSHOT performs lifecycle check + scene/position read in
            # one game-UI-thread command. No foreign CRT is started here.
            try:
                self.after(
                    0,
                    lambda p=host_present, s=sample: self._apply_header_live_result(
                        p, s
                    ),
                )
            except Exception:
                self._header_live_busy = False

        threading.Thread(target=worker, daemon=True).start()

    def _apply_header_live_result(
        self, host_present: bool | None, sample: dict | None
    ) -> None:
        """Apply one bridge lifecycle result, then optional idle header sample."""
        if host_present is False:
            self._header_live_busy = False
            if self._closed:
                return
            self._business_invalid_notified = True
            self._stop_header_live()
            self._log(f"角色业务已失效 pid={int(self.session.pid)}，停止并卸载会话")
            callback = self._on_business_invalid
            if callback is not None:
                try:
                    callback(int(self.session.pid), "role_instance_missing")
                except Exception as e:
                    self._log(f"角色业务失效处理失败 pid={int(self.session.pid)}: {e}")
            return
        if host_present is None:
            # Bridge timeout/error is not a fresh sample. Keep the last caption
            # visible, but do not republish it with a new cache timestamp.
            self._header_live_busy = False
            if self._closed:
                return
            try:
                if not self.winfo_exists():
                    return
            except Exception:
                return
            try:
                self._header_live_job = self.after(
                    self._HEADER_LIVE_MS, self._header_live_tick
                )
            except Exception:
                self._header_live_job = None
            return
        self._apply_header_live_sample(sample)

    def _apply_header_live_sample(self, sample: dict | None) -> None:
        """
        Apply one host_live sample on the UI thread and re-arm poll.

        Title-bar is the continuous producer for map/role/pos/death/vitals.

        @author by ak
        """
        self._header_live_busy = False
        if self._closed:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        sample = sample or {}
        map_lab = sample.get("scene_label")
        role = sample.get("role_name")
        pos = sample.get("pos")
        scene_id = sample.get("scene_id")
        dead = sample.get("dead")
        touch_dead = bool(sample.get("touch_dead"))
        # UI caption + hub (publish once via set_live_status scene fields)
        self.set_live_status(
            map_label=map_lab,
            role_name=role,
            pos=pos,
            scene_id=scene_id,
            dead=dead,
            publish=True,
            source="header_live",
            touch_dead=touch_dead,
        )
        # Extended vitals (money/equip/...) into same hub without double scene write.
        try:
            from app.core.live_scene_hub import publish_live_scene

            publish_live_scene(
                int(self.session.pid),
                host_id=sample.get("host_id"),
                vitality_pct=sample.get("vitality_pct"),
                money=sample.get("money"),
                money_bind=sample.get("money_bind"),
                equip_dura_pct=sample.get("equip_dura_pct"),
                equip_dura_min_pct=sample.get("equip_dura_min_pct"),
                in_team=sample.get("in_team"),
                source="header_live",
                touch_vitality=bool(sample.get("touch_vitality")),
                touch_money=bool(sample.get("touch_money")),
                touch_equip=bool(sample.get("touch_equip")),
            )
        except Exception:
            pass
        if self._active_keys:
            try:
                self._sync_game_title_marker()
            except Exception:
                pass
        try:
            self._header_live_job = self.after(
                self._HEADER_LIVE_MS, self._header_live_tick
            )
        except Exception:
            self._header_live_job = None
        return

    def _apply_header_live(
        self,
        map_lab: str | None,
        role: str | None,
        pos: tuple[float, float, float] | None,
        scene_id: int | None = None,
        dead: bool | None = None,
        touch_dead: bool = False,
        *,
        rearm: bool = True,
    ) -> None:
        """
        Apply one live header sample on the UI thread and re-arm poll.

        This is the single continuous producer for map/role/pos/death.

        @author by ak
        """
        if rearm:
            self._header_live_busy = False
        if self._closed:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self.set_live_status(
            map_label=map_lab,
            role_name=role,
            pos=pos,
            scene_id=scene_id,
            dead=dead,
            publish=True,
            source="header_live",
            touch_dead=bool(touch_dead),
        )
        # Game client may overwrite title; re-apply activity markers while busy.
        if self._active_keys:
            try:
                self._sync_game_title_marker()
            except Exception:
                pass
        if rearm:
            try:
                self._header_live_job = self.after(
                    self._HEADER_LIVE_MS, self._header_live_tick
                )
            except Exception:
                self._header_live_job = None

    def shutdown(self) -> None:
        """
        Stop feature runners + header poll; keep mount + game bridge intact.

        Used only on full app quit — not when user clicks the window X.
        Restores game title to idle [GUI] after runners stop.

        @author by ak
        """
        if self._closed:
            return
        self._closed = True
        self._stop_header_live()
        try:
            self._drop_live_scene_hub()
        except Exception:
            pass
        # Stop page-owned workers before dropping their per-pid scheduler state.
        # Otherwise a worker that is still unwinding can recreate cache/dispatch
        # entries after this window has already torn them down.
        for page in list(self._pages.values()):
            if hasattr(page, "_detach_store_listener"):
                try:
                    page._detach_store_listener()
                except Exception:
                    pass
            if hasattr(page, "_cancel_ui_drain"):
                try:
                    page._cancel_ui_drain()
                except Exception:
                    pass
            if hasattr(page, "shutdown"):
                try:
                    page.shutdown()
                except Exception:
                    pass
        try:
            from app.core.hang_settings import stop_hang_guard

            stop_hang_guard(int(self.session.pid), log=lambda _m: None)
        except Exception:
            pass
        try:
            from app.core.safe_dispatch import get_dispatch

            get_dispatch().drop_pid(int(self.session.pid))
        except Exception:
            pass
        try:
            from app.core.task_sync import get_task_sync_hub
            from app.core.cloud_sync import drop_cloud_sync_bridge

            get_task_sync_hub().unregister(int(self.session.pid))
            drop_cloud_sync_bridge(int(self.session.pid))
        except Exception:
            pass
        self._active_keys.clear()
        try:
            self._sync_game_title_marker()
        except Exception:
            pass

    def destroy(self) -> None:
        """Destroy this panel and release all per-session resources exactly once."""
        try:
            self.shutdown()
        except Exception:
            pass
        try:
            super().destroy()
        finally:
            callback = self._on_close
            self._on_close = None
            if callback is not None:
                try:
                    callback(int(self.session.pid))
                except Exception:
                    pass

    def hide(self) -> None:
        """
        Hide feature window without stopping business runners.

        Delete hotkey can deiconify the same window; auto-loot / yaolu / activity
        keep running in the background.

        @author by ak
        """
        try:
            self.withdraw()
        except Exception:
            pass
        try:
            self._log(
                f"功能窗已隐藏 pid={int(self.session.pid)}（业务继续，Delete 可再开）"
            )
        except Exception:
            pass

    def show_and_focus(self) -> None:
        """
        Restore a hidden feature window and bring it to front.

        @author by ak
        """
        try:
            self.deiconify()
        except Exception:
            pass
        try:
            self.lift()
            self.attributes("-topmost", True)
            self.after(200, lambda: self._clear_own_topmost())
            self.focus_force()
        except Exception:
            pass

    def _clear_own_topmost(self) -> None:
        """Drop temporary topmost after restore. @author by ak"""
        try:
            if self.winfo_exists():
                self.attributes("-topmost", False)
        except Exception:
            pass

    def _close(self) -> None:
        """
        Window X / 关闭: hide only; do not stop runners or drop mount.

        Full stop is via each page's 停止 button, or quitting the shell app.
        @author by ak
        """
        self.hide()
