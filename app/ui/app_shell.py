# -*- coding: utf-8 -*-
"""
Business GUI shell: login welcome + tray + game-window Delete inject.

Flow:
  1) Small welcome: card-key login + machine unbind
  2) After login: hide to tray; hover shows card-key expiry
  3) 「一键登录」: login then inject every running xajh instance; feature UI stays hidden
     (副控 / task sync / runners still work; Delete restores UI)
  4) When game window is foreground and user presses Delete:
     connect that pid (if needed) and open one feature window per game hwnd

Instance scopes (can coexist):
  source (unfrozen) / dev package / prod package — single-instance within scope only.
  Inject ownership prefers prod > dev > source so local debug does not steal live control.

@author by ak
"""
from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.auth_mock import (
    GAME_CLIENT_DEFAULT_MAX_WINDOWS,
    AuthService,
    is_multi_open_enabled,
    load_login_prefs,
    preferred_login_key,
    save_login_prefs,
)
from app.core.build_profile import is_dev_build
from app.core.license_client import is_soft_license_network_failure
from app.core.inject_gate import (
    INJECT_BATCH_READY_WAIT_S,
    batch_failure_category,
    find_main_hwnd_for_pid,
    find_xajh_processes,
    is_batch_retryable,
    run_delete_inject_async,
)
from app.core.session_store import GameSession, SessionStore
from app.core.single_instance import (
    inject_block_reason,
    instance_scope,
    is_inject_controller,
    scope_label,
    try_acquire_single_instance,
)
from app.core.tray_icon import TrayController
from app.core.win_focus import DeleteKeyWatcher, is_xajh_foreground
from app.core.window_title import (
    GUI_TITLE_SUFFIX,
    ensure_gui_title_suffix,
    get_window_title,
    strip_gui_title_suffix,
)
from app.ui.session_window import SessionFeatureWindow
from app.ui.theme import C, apply_theme, section

# Fast poll: level-edge Delete is easy to miss at 80ms under game load
HOTKEY_POLL_MS = 30
# 一键登录批量：实例间冷却，避免连续注入挤压游戏侧加载/回包。
# 批量在入队前已预筛「已进角色」的实例，游戏侧已稳定，冷却可小于 Delete 场景。
BATCH_INJECT_COOLDOWN_MS = 300


def _one_click_role_id(pid: int, *, log: Callable[[str], None] | None = None) -> int:
    """Best-effort live host role id for the one-click batch pre-filter.

    Read-only attach + GetHostPlayer id probe (no bridge / no LoadLibrary).
    Bypasses the scene-stability CRT fence: the role id is a pure UI query and
    must not be refused right after a scene load, or in-role games would report
    a false "no role" and be wrongly skipped. Returns 0 when the game has no
    in-world role yet, -1 when the probe failed (caller falls back to the normal
    inject gate).

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        from app.core.loot import open_attach_session
        from app.core.plg_ui import EXPORT_GET_HOST_PLAYER, _resolve_va
        from app.core.remote_runtime import (
            remote_call_cdecl_x86,
            remote_read_bytes,
        )

        attach = open_attach_session(pid, warmup=False, log=lambda _m: None)
        if attach is None:
            return -1
        try:
            va = _resolve_va(attach, EXPORT_GET_HOST_PLAYER)
            ret = remote_call_cdecl_x86(
                pid, va, [], timeout_ms=2500, skip_scene_gate=True
            )
            host = int(ret) & 0xFFFFFFFF
            if not host:
                return 0
            raw = remote_read_bytes(pid, host + 0x140, 8)
            if len(raw) != 8:
                return -1
            lo = int.from_bytes(raw[0:4], "little")
            hi = int.from_bytes(raw[4:8], "little")
            return (int(hi) << 32) | int(lo)
        finally:
            try:
                attach.close()
            except Exception:
                pass
    except Exception as e:
        log(f"_one_click_role_id error pid={pid}: {e}")
        return -1


def _win_iconic(hwnd: int) -> bool:
    """True when the window is minimized (no title trust for pre-filter). @author by ak"""
    if not hwnd:
        return False
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        return bool(user32.IsIconic(ctypes.wintypes.HWND(int(hwnd))))
    except Exception:
        return False


def _one_click_restore_window(
    pid: int,
    hwnd: int = 0,
    *,
    log: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Best-effort restore a minimized / hidden game window for the batch inject.

    ``find_main_hwnd_for_pid`` only sees visible windows, so a game minimized to
    tray (or hidden) reports no hwnd, and a minimized window can lag the timer /
    inject path. Restore it (SW_RESTORE, no foreground steal) so its title is
    readable and the window is injectable, then re-resolve.
    Returns (hwnd, title) after restore, or (0, "") when no game window exists.

    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(pid)
    try:
        import ctypes

        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        target = int(hwnd or 0)
        if not target or not user32.IsWindow(wintypes.HWND(target)):
            best = 0
            best_score = -1

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _enum(h, _lp):
                nonlocal best, best_score
                proc = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(h, ctypes.byref(proc))
                if int(proc.value) != pid:
                    return True
                cbuf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(h, cbuf, 256)
                cls = (cbuf.value or "").lower()
                score = 0
                if "xajhelementclient" in cls.replace(" ", ""):
                    score += 100
                elif "xajh" in cls:
                    score += 40
                t = get_window_title(int(h))
                if " - " in t or "—" in t:
                    score += 20
                if score > best_score:
                    best_score = score
                    best = int(h)
                return True

            user32.EnumWindows(_enum, 0)
            target = best
        if not target or not user32.IsWindow(wintypes.HWND(target)):
            return 0, ""
        if user32.IsIconic(wintypes.HWND(target)):
            user32.ShowWindow(wintypes.HWND(target), 9)  # SW_RESTORE
        elif not user32.IsWindowVisible(wintypes.HWND(target)):
            user32.ShowWindow(wintypes.HWND(target), 5)  # SW_SHOW
        time.sleep(0.3)
        h2, t2, _cls = find_main_hwnd_for_pid(pid)
        if h2:
            return int(h2), str(t2 or "")
        return int(target), get_window_title(int(target)) or ""
    except Exception as e:
        log(f"一键登录: 恢复窗口失败 pid={pid}: {e}")
        return 0, ""


def _one_click_skip_not_in_role(
    pid: int,
    title: str,
    *,
    hwnd: int = 0,
    log: Callable[[str], None] | None = None,
) -> bool:
    """True when a queued one-click target should be skipped as not in-role.

    A confirmed in-world role is required to queue the instance; games at login /
    char-select cannot pass the post-inject host_context check anyway, so skipping
    them avoids a wasted inject + ready-wait per instance (the main one-click
    batch slow-down).

    The window title is only a fast-path hint and is never trusted for minimized
    windows or when no hwnd was resolved (minimized in-role games keep their
    segmented title, but a hidden/tray window may report "" or a bare title) —
    those always fall back to the read-only host-id probe. Probe failure returns
    False so the normal inject gate still decides (never wrongly skips a live game).

    @author by ak
    """
    log = log or (lambda _m: None)
    if hwnd and not _win_iconic(hwnd):
        try:
            from app.core.window_title import title_not_in_role

            if title_not_in_role(title):
                log(f"一键登录: pid={pid} 标题无角色段，跳过未进角色注入")
                return True
        except Exception as e:
            log(f"一键登录: 标题判断失败 pid={pid}: {e}")
    oid = _one_click_role_id(pid, log=log)
    if oid < 0:
        return False
    if oid == 0:
        log(f"一键登录: pid={pid} 未检测到角色（oid=0），跳过注入")
        return True
    return False


def is_packaged() -> bool:
    """
    True when running as frozen / packaged binary (PyInstaller etc.).

    @author by ak
    """
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


class ShellApp(tk.Tk):
    """
    Single-instance shell: login-only welcome, tray, game Delete inject.

    @author by ak
    """

    def __init__(self) -> None:
        super().__init__()
        self._instance_scope = instance_scope()
        self._scope_label = scope_label(self._instance_scope)
        # Scope-aware title so 源码 / 测试包 / 正式包 are obvious when both run.
        if self._instance_scope == "prod":
            self.title("XAJH 助手")
        else:
            self.title(f"XAJH 助手 · {self._scope_label}")
        # Original width; height keeps room for wrapped dev button row
        self.geometry("500x490")
        self.minsize(480, 460)
        self.resizable(False, False)

        self.store = SessionStore()
        try:
            from app.core.team_chat import set_session_store

            set_session_store(self.store)
        except Exception:
            pass
        self.auth = AuthService(log=lambda m: self._log(m))
        self.msg_q: queue.Queue = queue.Queue()
        self._closing = False
        self._inject_busy = False
        # Recomputed when peers appear/disappear; False => no Delete/one-click inject.
        self._inject_controller = bool(is_inject_controller())
        # Sequential inject queue for「一键登录」(pid + hwnd); empty when idle.
        self._inject_queue: list[dict] = []
        # When True, feature windows stay hidden after inject (background ready).
        self._inject_hide_after: bool = False
        self._inject_batch_stats: dict | None = None
        self._inject_batch_summary: dict | None = None
        self._inject_current_source = ""
        # One-click batch progress counters for per-instance logs.
        self._inject_batch_total: int = 0
        self._inject_batch_index: int = 0
        # Account manager window (tray entry), created once and reused.
        self._account_win = None
        # Live per-role status for the account list: role_id -> {pid, map, action}.
        self._role_live: dict[str, dict] = {}
        self._hotkey_job = None
        self._del_watch = DeleteKeyWatcher()
        self._feature_wins: dict[int, SessionFeatureWindow] = {}
        self._session_invalid_counts: dict[int, int] = {}
        self._unloading_pids: set[int] = set()
        self._debug_win = None
        self._tray: TrayController | None = None
        self._hidden_to_tray = False
        # Login「开启调试」: last local choice, else build channel (dev on / prod off).
        try:
            from app.core.build_profile import default_debug_mode

            self.debug_mode: bool = bool(default_debug_mode())
        except Exception:
            self.debug_mode = True
        try:
            prefs = load_login_prefs()
            if "debug_mode" in prefs:
                self.debug_mode = bool(prefs.get("debug_mode"))
        except Exception:
            pass

        apply_theme(self)
        self._build_welcome()
        self._build_tray()
        self._refresh_inject_controller(log_change=True)

        self.store.on_change(self._on_sessions_changed)
        self._queue_job = self.after(100, self._drain_queue)
        self.after(HOTKEY_POLL_MS, self._poll_delete_hotkey)
        self._reverify_job = None
        self._license_soft_fail_since: float | None = None
        self._LICENSE_SOFT_FAIL_GRACE_S = 60 * 60  # network soft-fail grace before force logout
        self.after(800, self._startup_license_reverify)
        self._health_job = self.after(1500, self._poll_mounted_game_health)
        self.protocol("WM_DELETE_WINDOW", self._on_user_close)

    def report_callback_exception(self, exc_type, exc_value, exc_tb) -> None:
        """Persist Tk callback failures as structured 助手崩溃 reports."""
        try:
            from app.core.crash_capture import report_tk_callback_exception

            report_tk_callback_exception(exc_type, exc_value, exc_tb)
        except Exception:
            try:
                import traceback

                from app.core import diag_log

                text = "".join(
                    traceback.format_exception(exc_type, exc_value, exc_tb)
                )
                diag_log.error(
                    f"TK_CALLBACK_UNCAUGHT\n{text}", tag="HELPER_CRASH"
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ UI
    def _build_welcome(self) -> None:
        """
        Login / key page — sized so action buttons are never clipped.

        @author by ak
        """
        # Outer: content + fixed footer (footer packs first so it is never covered)
        outer = ttk.Frame(self, style="Content.TFrame")
        outer.pack(fill=tk.BOTH, expand=True)

        # Footer: quit only — no permanent status strip (alerts use messagebox)
        foot = ttk.Frame(outer, style="Content.TFrame", padding=(20, 8))
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        if is_dev_build():
            ttk.Label(foot, text="开发模式", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Button(foot, text="退出", width=8, command=self._quit_app).pack(
            side=tk.RIGHT
        )

        root = ttk.Frame(outer, style="Content.TFrame", padding=(20, 16, 20, 8))
        root.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        ttk.Label(root, text="XAJH 助手", style="Welcome.TLabel").pack(anchor="w")
        ttk.Label(
            root,
            text=(
                f"输入卡密登录 · 当前：{self._scope_label}"
                "（源码/测试包/正式包可并存，仅最高优先级注入）"
            ),
            style="Muted.TLabel",
            wraplength=440,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 12))

        box = section(root, "卡密登录")
        box.pack(fill=tk.X, pady=(0, 10))

        # Key row: entry + show — same control height via shared padding styles
        row_key = ttk.Frame(box, style="Panel.TFrame")
        row_key.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row_key, text="卡密", style="Panel.Muted.TLabel", width=6).pack(
            side=tk.LEFT
        )
        # Prefill: login_prefs.key if logged in before, else dev profile default.
        self.var_key = tk.StringVar(value=preferred_login_key())
        self.ent_key = ttk.Entry(row_key, textvariable=self.var_key, show="*", style="TEntry")
        self.ent_key.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 8))
        self.btn_show_key = ttk.Button(
            row_key, text="显示", width=6, command=self._toggle_key_mask
        )
        self.btn_show_key.pack(side=tk.RIGHT)
        self._key_masked = True

        # Primary actions — same style family so height matches
        row_btn = ttk.Frame(box, style="Panel.TFrame")
        row_btn.pack(fill=tk.X, pady=(0, 2))
        self.btn_login = ttk.Button(
            row_btn,
            text="登录",
            style="Accent.TButton",
            width=10,
            command=self._on_login,
        )
        self.btn_login.pack(side=tk.LEFT)
        self.btn_one_click = ttk.Button(
            row_btn,
            text="一键登录",
            style="Accent.TButton",
            width=10,
            command=self._on_one_click_login,
        )
        self.btn_one_click.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_unbind = ttk.Button(
            row_btn,
            text="机器解绑",
            style="TButton",
            width=10,
            command=self._on_unbind,
        )
        self.btn_unbind.pack(side=tk.LEFT, padx=(8, 0))
        # Back-compat alias for layout smoke tools.
        self.btn_fetch_key = self.btn_unbind
        # Dev-only: label link (not a full button) — click to open workbench
        if is_dev_build():
            self.lbl_dev = ttk.Label(
                row_btn,
                text="开发者面板",
                style="Link.TLabel",
                cursor="hand2",
            )
            self.lbl_dev.pack(side=tk.RIGHT, padx=(8, 0))
            self.lbl_dev.bind("<Button-1>", lambda _e: self._open_debug())
            self.lbl_dev.bind(
                "<Enter>",
                lambda _e: self.lbl_dev.configure(foreground=C["accent_hi"]),
            )
            self.lbl_dev.bind(
                "<Leave>",
                lambda _e: self.lbl_dev.configure(foreground=C["accent"]),
            )

        # Debug mode: default from build channel (dev on / prod off). Crash log always on.
        row_log = ttk.Frame(box, style="Panel.TFrame")
        row_log.pack(fill=tk.X, pady=(8, 0))
        self.var_debug = tk.BooleanVar(value=bool(self.debug_mode))
        # Back-compat alias for any leftover refs.
        self.var_file_log = self.var_debug
        self.chk_file_log = ttk.Checkbutton(
            row_log,
            text="开启调试",
            variable=self.var_debug,
        )
        self.chk_file_log.pack(side=tk.LEFT)
        ttk.Label(
            row_log,
            text="写日志与截图到软件目录",
            style="Panel.Muted.TLabel",
        ).pack(side=tk.LEFT, padx=(8, 0))

        # Game client multi-open unlock (xajh.exe 3-window mutex). Not helper-side.
        row_multi = ttk.Frame(box, style="Panel.TFrame")
        row_multi.pack(fill=tk.X, pady=(6, 0))
        try:
            multi_default = bool(is_multi_open_enabled())
        except Exception:
            multi_default = False
        self.var_multi_open = tk.BooleanVar(value=multi_default)
        self.chk_multi_open = ttk.Checkbutton(
            row_multi,
            text="游戏多开",
            variable=self.var_multi_open,
        )
        self.chk_multi_open.pack(side=tk.LEFT)
        ttk.Label(
            row_multi,
            text=(
                f"默认最多 {GAME_CLIENT_DEFAULT_MAX_WINDOWS} 个窗口；勾选后放宽"
            ),
            style="Panel.Muted.TLabel",
        ).pack(side=tk.LEFT, padx=(8, 0))

        tip = section(root, "说明")
        tip.pack(fill=tk.BOTH, expand=True)
        tip_text = (
            "· 登录后缩到托盘；鼠标悬停可看到期时间\n"
            "· 一键登录：自动连接已开游戏，功能窗默认隐藏（前台按 Delete 再打开）\n"
            "· 普通登录：只登录；在游戏前台按 Delete 连接当前窗口\n"
            "· 游戏刚启动/登录未就绪时按 Delete 会自动延迟注入，避免闪退\n"
            "· 每个游戏窗口各自一套功能，可隐藏，后台继续跑\n"
            "· 游戏多开：放宽游戏窗口数量（需管理员）\n"
            "· 开启调试：写日志/截图到软件目录；关掉则只保留错误日志\n"
            "· 换电脑前可「机器解绑」（每天限 1 次）"
        )
        try:
            if is_dev_build():
                tip_text += "\n· 当前为测试构建：默认开调试，可由环境预填识别密钥"
            else:
                tip_text += "\n· 当前为生产构建：默认关调试，识别密钥请在设置中填写"
        except Exception:
            pass
        if is_dev_build():
            tip_text += "\n· 开发环境可点「开发者面板」打开旧版工作台"
        # Text + right-edge scrollbar (white panel bg; no Canvas gray strip).
        tip_host = ttk.Frame(tip, style="Panel.TFrame")
        tip_host.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        tip_sb = ttk.Scrollbar(tip_host, orient=tk.VERTICAL)
        tip_sb.pack(side=tk.RIGHT, fill=tk.Y)
        tip_view = tk.Text(
            tip_host,
            wrap=tk.WORD,
            height=6,
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=0,
            padx=4,
            pady=2,
            bg=C["panel"],
            fg=C["muted"],
            insertbackground=C["muted"],
            selectbackground=C["select"],
            selectforeground=C["select_fg"],
            font=("Microsoft YaHei UI", 9),
            cursor="arrow",
        )
        tip_view.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tip_view.configure(yscrollcommand=tip_sb.set)
        tip_sb.configure(command=tip_view.yview)
        tip_view.insert("1.0", tip_text)
        tip_view.configure(state=tk.DISABLED)

        def _tip_wheel(event, _w=tip_view):
            try:
                delta = int(event.delta)
            except Exception:
                delta = 0
            if delta:
                _w.yview_scroll(int(-1 * (delta / 120)), "units")
            return "break"

        tip_view.bind("<MouseWheel>", _tip_wheel)
        tip_host.bind("<MouseWheel>", _tip_wheel)

        self.ent_key.bind("<Return>", lambda _e: self._on_login())

    def _build_tray(self) -> None:
        """Start tray icon if backend present. @author by ak"""
        on_dev = None
        if is_dev_build():
            on_dev = lambda: self.msg_q.put(("__TRAY_DEV__",))
        tip = self._tray_tip_text()
        # Distinct tray identity per scope so 源码 + 打包 can sit side by side.
        accent = (0, 120, 212)
        if self._instance_scope == "source":
            accent = (180, 120, 20)  # amber
        elif self._instance_scope == "dev":
            accent = (20, 140, 90)  # green
        self._tray = TrayController(
            on_show=lambda: self.msg_q.put(("__TRAY_SHOW__",)),
            on_quit=lambda: self.msg_q.put(("__TRAY_QUIT__",)),
            on_accounts=lambda: self.msg_q.put(("__TRAY_ACCOUNTS__",)),
            tip=tip,
            on_dev=on_dev,
            icon_name=f"xajh_helper_{self._instance_scope}",
            accent_rgb=accent,
        )
        ok = self._tray.start()
        if not ok:
            self._log("托盘不可用（可选安装 pystray Pillow）；将使用任务栏最小化")
        elif is_dev_build():
            self._log("开发模式: 托盘菜单含「开发者面板」")

    def _toggle_key_mask(self) -> None:
        """Show / hide key characters. @author by ak"""
        self._key_masked = not self._key_masked
        self.ent_key.configure(show="*" if self._key_masked else "")
        self.btn_show_key.configure(text="隐藏" if not self._key_masked else "显示")

    def _apply_debug_on_login(self) -> None:
        """
        Apply login-page「开启调试」after successful login.

        On: full logs + captcha crop files on disk.
        Off: no full logs / no crop files (memory only); crash/FATAL still on disk.
        @author by ak
        """
        want = bool(self.var_debug.get())
        self.debug_mode = want
        try:
            from app.core import diag_log

            path = diag_log.set_file_logging(want)
            err_p = diag_log.error_log_path()
            if want and path is not None:
                self._log(f"调试已开启: 日志 {path}")
                self._log(f"错误/崩溃日志: {err_p}")
                try:
                    from app.core.yaolu_auto import _debug_dir

                    self._log(f"调试: 验证码截图将写入 {_debug_dir()}")
                except Exception:
                    self._log("调试: 验证码截图将写入 .\\captures\\captcha")
            elif not want:
                self._log(
                    f"调试关闭: 不写普通日志/截图；崩溃/FATAL 仍写入 {err_p}"
                )
        except Exception as e:
            self._log(f"切换调试失败: {e}")

    def _apply_file_log_on_login(self) -> None:
        """
        Back-compat alias. @author by ak
        """
        self._apply_debug_on_login()

    def _set_auth_buttons_loading(
        self, *, loading: bool, which: str = "login"
    ) -> None:
        """
        ttk has no spinner — use disabled + text swap as loading state.

        @author by ak
        """
        try:
            if loading:
                if which == "unbind":
                    self.btn_unbind.configure(text="解绑中…", state=tk.DISABLED)
                    self.btn_login.configure(state=tk.DISABLED)
                    self.btn_one_click.configure(state=tk.DISABLED)
                elif which == "one_click":
                    self.btn_one_click.configure(text="登录中…", state=tk.DISABLED)
                    self.btn_login.configure(state=tk.DISABLED)
                    self.btn_unbind.configure(state=tk.DISABLED)
                else:
                    self.btn_login.configure(text="登录中…", state=tk.DISABLED)
                    self.btn_one_click.configure(state=tk.DISABLED)
                    self.btn_unbind.configure(state=tk.DISABLED)
            else:
                self.btn_login.configure(text="登录", state=tk.NORMAL)
                self.btn_one_click.configure(text="一键登录", state=tk.NORMAL)
                self.btn_unbind.configure(text="机器解绑", state=tk.NORMAL)
        except Exception:
            pass
        try:
            self.update_idletasks()
        except Exception:
            pass

    def _on_unbind(self) -> None:
        """Confirm then unbind current machine from card-key (async). @author by ak"""
        if getattr(self, "_auth_busy", False):
            return
        key = (self.var_key.get() or "").strip()
        if not key:
            messagebox.showwarning("机器解绑", "请输入卡密", parent=self)
            return
        if not messagebox.askyesno(
            "机器解绑",
            "是否解除本机绑定？",
            parent=self,
        ):
            return
        self._auth_busy = True
        self._set_auth_buttons_loading(loading=True, which="unbind")

        def worker() -> None:
            try:
                ok, msg = self.auth.unbind(key)
            except Exception as e:
                ok, msg = False, f"解绑异常: {e}"
            self.after(0, lambda: self._finish_unbind(ok, msg))

        threading.Thread(target=worker, daemon=True, name="xajh-unbind").start()

    def _finish_unbind(self, ok: bool, msg: str) -> None:
        """UI-thread completion for machine unbind. @author by ak"""
        self._auth_busy = False
        self._set_auth_buttons_loading(loading=False)
        self._log(f"机器解绑: {'成功' if ok else '失败'} · {msg}")
        if ok:
            messagebox.showinfo("机器解绑", msg or "解绑成功", parent=self)
        else:
            messagebox.showwarning("机器解绑", msg or "解绑失败", parent=self)

    def _on_one_click_login(self) -> None:
        """
        One-click: card-key login then inject every running xajh instance.

        Feature windows are created but kept hidden so 副控 / runners still work.
        @author by ak
        """
        self._on_login(one_click=True)

    def _on_login(self, *, one_click: bool = False) -> None:
        """Card-key login then hide to tray (network off UI thread). @author by ak"""
        if getattr(self, "_auth_busy", False):
            return
        key = str(self.var_key.get() or "").strip()
        try:
            multi = bool(self.var_multi_open.get())
        except Exception:
            multi = False
        try:
            debug_mode = bool(self.var_debug.get())
        except Exception:
            debug_mode = bool(getattr(self, "debug_mode", False))

        self._auth_busy = True
        self._set_auth_buttons_loading(
            loading=True, which=("one_click" if one_click else "login")
        )

        def worker() -> None:
            result: dict = {
                "ok": False,
                "msg": "登录失败",
                "multi": multi,
                "multi_ok": True,
                "multi_detail": "",
                "multi_err": "",
                "one_click": bool(one_click),
            }
            try:
                ok, msg = self.auth.login(key)
                result["ok"] = bool(ok)
                result["msg"] = str(msg or "")
                if ok:
                    try:
                        save_login_prefs(
                            key=key,
                            debug_mode=debug_mode,
                            multi_open=multi,
                        )
                    except Exception:
                        pass
                    # Multi-open memory patch can take noticeable time with
                    # several live clients — keep it off the UI thread.
                    # When a higher-priority helper is already running, skip
                    # patching so local debug login cannot disturb live clients.
                    try:
                        allow_multi_patch = True
                        try:
                            allow_multi_patch = bool(is_inject_controller())
                        except Exception:
                            allow_multi_patch = True
                        if not allow_multi_patch:
                            result["multi_ok"] = True
                            result["multi_detail"] = (
                                "已跳过多开切换：更高优先级通道运行中"
                            )
                        else:
                            from app.core.game_multi_open import (
                                disable_multi_open,
                                enable_multi_open,
                            )

                            if multi:
                                ok_m, detail = enable_multi_open(log=self._log)
                            else:
                                ok_m, detail = disable_multi_open(log=self._log)
                            result["multi_ok"] = bool(ok_m)
                            result["multi_detail"] = str(detail or "")
                    except Exception as e:
                        result["multi_ok"] = False
                        result["multi_err"] = str(e)
                        result["multi_detail"] = str(e)
            except Exception as e:
                result["ok"] = False
                result["msg"] = f"登录异常: {e}"
            self.after(0, lambda r=result: self._finish_login(r))

        threading.Thread(target=worker, daemon=True, name="xajh-login").start()

    def _finish_login(self, result: dict) -> None:
        """UI-thread completion for card-key login. @author by ak"""
        self._auth_busy = False
        # On success window hides to tray; still restore labels for next show.
        self._set_auth_buttons_loading(loading=False)

        ok = bool(result.get("ok"))
        msg = str(result.get("msg") or "")
        multi = bool(result.get("multi"))
        multi_ok = bool(result.get("multi_ok", True))
        detail = str(result.get("multi_detail") or "")
        multi_err = str(result.get("multi_err") or "")

        if not ok:
            self._log(f"登录失败: {msg}")
            messagebox.showwarning("登录", msg or "登录失败", parent=self)
            return

        # Debug mode (logs + crop files) applied on successful login.
        self._apply_debug_on_login()
        self._schedule_license_reverify()

        if multi_err and not detail:
            self._log(f"登录成功 {msg} · 多开切换异常: {multi_err}")
        elif multi:
            self._log(
                f"登录成功 {msg} · 游戏多开=开 · "
                f"{'就绪' if multi_ok else '有告警'} · {detail}"
            )
            if not multi_ok:
                messagebox.showwarning(
                    "游戏多开",
                    "游戏多开未能完全就绪。\n\n"
                    f"{detail}\n\n"
                    "请以管理员运行助手，并先开本助手再启动游戏窗口。\n"
                    "若仍提示窗口数量超限，可关闭该游戏窗口后重试。",
                    parent=self,
                )
            elif "恢复原始" in detail or "校验修复" in detail:
                messagebox.showinfo(
                    "游戏多开",
                    "已处理历史改写痕迹（如有）。\n"
                    "当前只在内存中放宽窗口限制，不会修改游戏文件。\n\n"
                    f"{detail}",
                    parent=self,
                )
        else:
            self._log(
                f"登录成功 {msg} · 游戏多开=关（新开窗口恢复最多 "
                f"{GAME_CLIENT_DEFAULT_MAX_WINDOWS} 开）· {detail}"
            )

        if self._tray is not None:
            try:
                self._tray.update_tip(self._tray_tip_text())
            except Exception:
                pass
        self._hide_to_tray()
        if bool(result.get("one_click")):
            # After tray hide: inject all live game clients with UI hidden.
            # Slightly longer than bare 120ms so multi-open memory patch + window
            # settle finish before the first CreateRemoteThread.
            self.after(450, self._start_one_click_inject_all)

    def _startup_license_reverify(self) -> None:
        """
        On launch: if prefs have a cached card-key and we are logged in,
        re-check; also arm the periodic timer.
        @author by ak
        """
        try:
            if self.auth.is_logged_in:
                self._run_license_reverify(source="startup")
        except Exception as e:
            self._log(f"启动卡密复核异常: {e}")
        self._schedule_license_reverify()

    def _schedule_license_reverify(self) -> None:
        """Arm next periodic card-key re-check. @author by ak"""
        try:
            if self._reverify_job is not None:
                self.after_cancel(self._reverify_job)
        except Exception:
            pass
        try:
            interval_ms = int(max(5.0, float(self.auth.reverify_interval_s())) * 1000)
        except Exception:
            interval_ms = 30 * 60 * 1000
        self._reverify_job = self.after(interval_ms, self._on_license_reverify_tick)

    def _on_license_reverify_tick(self) -> None:
        """Periodic reverify callback. @author by ak"""
        try:
            if self.auth.is_logged_in:
                self._run_license_reverify(source="timer")
        except Exception as e:
            self._log(f"定时卡密复核异常: {e}")
        self._schedule_license_reverify()

    def _force_stop_all_features(self, *, reason: str = "") -> None:
        """
        Stop all business runners / 群控 / feature windows; keep shell alive.

        Used when card-key becomes invalid mid-session so auto features do not
        continue after logout. Does not unload in-game bridge DLL.
        @author by ak
        """
        why = (reason or "授权失效").strip() or "授权失效"
        self._log(f"强制停止全部功能 · {why}")
        self._inject_busy = False
        self._inject_queue = []
        self._inject_hide_after = False
        self._inject_batch_stats = None
        self._inject_batch_summary = None
        self._inject_current_source = ""
        self._inject_batch_total = 0
        self._inject_batch_index = 0
        try:
            self._del_watch.reset()
        except Exception:
            pass
        # Stop runners + leave cloud / task hub via window.shutdown first.
        for pid, win in list(self._feature_wins.items()):
            try:
                if hasattr(win, "shutdown"):
                    win.shutdown()
            except Exception as e:
                self._log(f"停止功能失败 pid={pid}: {e}")
        for s in list(self.store.list()):
            if getattr(s, "hwnd", 0):
                try:
                    strip_gui_title_suffix(int(s.hwnd))
                except Exception:
                    pass
            # Extra safety if window was already closed without shutdown.
            try:
                from app.core.cloud_sync import drop_cloud_sync_bridge
                from app.core.task_sync import get_task_sync_hub

                get_task_sync_hub().unregister(int(s.pid))
                drop_cloud_sync_bridge(int(s.pid))
            except Exception:
                pass
        for pid, win in list(self._feature_wins.items()):
            try:
                win.destroy()
            except Exception:
                pass
        self._feature_wins.clear()
        for s in list(self.store.list()):
            try:
                self.store.unmount(int(s.pid))
            except Exception:
                pass
        if self._debug_win is not None:
            try:
                if hasattr(self._debug_win, "_on_close"):
                    self._debug_win._on_close()
                else:
                    self._debug_win.destroy()
            except Exception:
                try:
                    self._debug_win.destroy()
                except Exception:
                    pass
            self._debug_win = None

    def _run_license_reverify(self, *, source: str) -> None:
        """
        Re-verify session off the UI thread.

        Soft network failures (timeout/unreachable) keep features running for up
        to 1 hour of continuous soft fail; hard auth failures logout immediately.
        @author by ak
        """
        if not self.auth.is_logged_in:
            return
        if getattr(self, "_reverify_busy", False):
            return
        self._reverify_busy = True

        def worker() -> None:
            try:
                ok, msg = self.auth.reverify()
            except Exception as e:
                ok, msg = False, f"复核异常: {e}"
            self.after(
                0,
                lambda o=ok, m=msg: self._finish_license_reverify(
                    source=source, ok=o, msg=m
                ),
            )

        threading.Thread(
            target=worker, daemon=True, name=f"xajh-reverify-{source}"
        ).start()

    def _finish_license_reverify(
        self, *, source: str, ok: bool, msg: str
    ) -> None:
        """UI-thread completion for license re-check. @author by ak"""
        self._reverify_busy = False
        if not self.auth.is_logged_in:
            return
        if ok:
            self._license_soft_fail_since = None
            self._refresh_feature_login_tokens()
            if self._tray is not None:
                try:
                    self._tray.update_tip(self.auth.session.tray_tip())
                except Exception:
                    pass
            self._log(f"卡密复核通过({source}) · {msg}")
            return

        soft = is_soft_license_network_failure(msg)
        now = time.time()
        if soft:
            if self._license_soft_fail_since is None:
                self._license_soft_fail_since = now
            elapsed = max(0.0, now - float(self._license_soft_fail_since))
            grace = float(getattr(self, "_LICENSE_SOFT_FAIL_GRACE_S", 60 * 60) or 3600)
            if elapsed < grace:
                mins = max(1, int(elapsed // 60)) if elapsed >= 60 else 0
                hold = f"已持续约 {mins} 分钟" if mins > 0 else "刚开始"
                self._log(
                    f"卡密复核网络异常({source}) · {msg or '网络异常'} · "
                    f"暂不停功能（{hold}，满 {int(grace // 60)} 分钟将强制退出）"
                )
                return
            self._log(
                f"卡密复核网络异常超时({source}) · {msg or '网络异常'} · "
                f"已持续满 {int(grace // 60)} 分钟，强制退出"
            )
            reason = msg or "网络异常超时，授权无法复核"
        else:
            self._license_soft_fail_since = None
            self._log(f"卡密复核失败({source}) · {msg}")
            reason = msg or "卡密已失效"

        try:
            self._force_stop_all_features(reason=reason)
        except Exception as e:
            self._log(f"强制停止功能异常: {e}")
        try:
            self.auth.logout()
        except Exception:
            pass
        self._license_soft_fail_since = None
        if self._tray is not None:
            try:
                self._tray.update_tip(self.auth.session.tray_tip())
            except Exception:
                pass
        self._show_login_window()
        messagebox.showwarning(
            "登录",
            (reason or "卡密已失效，请重新登录")
            + "\n\n已停止全部运行中的功能（含群控/自动任务等）。",
            parent=self,
        )

    def _refresh_feature_login_tokens(self) -> None:
        """Propagate a renewed in-memory token to existing feature windows."""
        token = str(getattr(self.auth.session, "token", "") or "").strip()
        cloud_allowed = bool(getattr(self.auth, "cloud_control_allowed", False))
        for pid, win in list(self._feature_wins.items()):
            try:
                win.settings["login_token"] = token
                win.settings["cloud_control_available"] = cloud_allowed
                if bool(win.settings.get("cloud_control_enabled")):
                    from app.core.cloud_sync import apply_settings_to_bridge

                    apply_settings_to_bridge(
                        win.settings,
                        int(pid),
                        log=lambda m, w=win: w._log(m),
                    )
            except Exception as e:
                self._log(f"刷新功能窗登录令牌失败 pid={pid}: {e}")

    def _hide_to_tray(self) -> None:
        """Withdraw main window after login. @author by ak"""
        self._hidden_to_tray = True
        try:
            self.withdraw()
        except Exception:
            try:
                self.iconify()
            except Exception:
                pass
        self._log("已隐藏到托盘 · 游戏窗口按 Delete 连接")

    def _show_login_window(self) -> None:
        """Restore welcome/login window from tray. @author by ak"""
        self._hidden_to_tray = False
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            pass
        if self.auth.is_logged_in:
            self._log(f"已显示登录页 · {self.auth.session.expire_text()}")

    # ------------------------------------------------------------- hotkey
    def _tray_tip_text(self) -> str:
        """Tray hover text including scope + inject ownership. @author by ak"""
        base = ""
        try:
            base = str(self.auth.session.tray_tip() or "").strip()
        except Exception:
            base = ""
        if not base:
            base = "XAJH 助手"
        scope = getattr(self, "_scope_label", "") or scope_label()
        ctrl = bool(getattr(self, "_inject_controller", True))
        batch_stats = getattr(self, "_inject_batch_stats", None)
        batch_summary = getattr(self, "_inject_batch_summary", None)
        batch_total = int(getattr(self, "_inject_batch_total", 0) or 0)
        batch_active = isinstance(batch_stats, dict) and batch_total > 0
        if batch_active:
            ok_n = int(batch_stats.get("ok") or 0)
            fail_n = int(batch_stats.get("fail") or 0)
            completed = ok_n + fail_n
            own = (
                f"一键登录：注入中 {completed}/{batch_total}"
                f"（成功 {ok_n} · 失败 {fail_n}）"
            )
        elif isinstance(batch_summary, dict):
            ok_n = int(batch_summary.get("ok") or 0)
            fail_n = int(batch_summary.get("fail") or 0)
            own = f"一键登录：已完成（成功 {ok_n} · 失败 {fail_n}）"
        else:
            own = "注入中" if ctrl else "观察中(不注入)"
        if scope and scope not in base:
            return f"{base}\n[{scope} · {own}]"
        return f"{base}\n[{own}]"

    def _refresh_tray_tip(self) -> None:
        """Refresh tray text after an injection state transition. @author by ak"""
        try:
            if self._tray is not None:
                self._tray.update_tip(self._tray_tip_text())
        except Exception:
            pass

    def _refresh_inject_controller(self, *, log_change: bool = False) -> bool:
        """
        Refresh whether this process owns Delete / auto-inject.

        @author by ak
        """
        prev = bool(getattr(self, "_inject_controller", True))
        try:
            now = bool(is_inject_controller())
        except Exception:
            now = True
        self._inject_controller = now
        if log_change or now != prev:
            if now:
                self._log(
                    f"实例通道={getattr(self, '_scope_label', scope_label())} · 注入控制权=是"
                )
            else:
                reason = ""
                try:
                    reason = inject_block_reason()
                except Exception:
                    reason = ""
                self._log(
                    f"实例通道={getattr(self, '_scope_label', scope_label())} · "
                    f"注入控制权=否（避免干扰其它通道）"
                    + (f" · {reason}" if reason else "")
                )
        try:
            if self._tray is not None:
                self._tray.update_tip(self._tray_tip_text())
        except Exception:
            pass
        return now

    def _ensure_inject_allowed(self, *, action: str = "连接游戏") -> bool:
        """
        Gate Delete / 一键注入 when a higher-priority helper is already running.

        @author by ak
        """
        self._refresh_inject_controller(log_change=False)
        if bool(getattr(self, "_inject_controller", True)):
            return True
        reason = ""
        try:
            reason = inject_block_reason()
        except Exception:
            reason = ""
        msg = reason or (
            f"{action}已禁用：更高优先级的助手通道正在运行，"
            f"避免调试干扰正在使用的功能。"
        )
        self._log(msg)
        try:
            messagebox.showinfo(action, msg, parent=self)
        except Exception:
            pass
        return False

    def _start_one_click_inject_all(self) -> None:
        """
        After「一键登录」: inject every running xajh.exe, keep feature UI hidden.

        Already-mounted sessions only re-hide their feature window. Unmounted
        pids are queued and injected sequentially (inject gate is single-flight).
        @author by ak
        """
        if not self.auth.is_logged_in:
            self._log("一键登录: 未登录，跳过注入")
            return
        if not self._ensure_inject_allowed(action="一键登录注入"):
            return
        try:
            procs = list(find_xajh_processes() or [])
        except Exception as e:
            self._log(f"一键登录: 枚举游戏进程失败: {e}")
            procs = []
        if not procs:
            self._log("一键登录: 未找到游戏进程，仅登录到托盘（开游戏后按 Delete 连接）")
            return

        queue: list[dict] = []
        already = 0
        skipped = 0
        for p in procs:
            try:
                pid = int(p.get("pid") or 0)
            except Exception:
                pid = 0
            if not pid:
                continue
            if self.store.has(pid):
                already += 1
                win = self._feature_win_alive(pid)
                if win is not None:
                    try:
                        if hasattr(win, "hide"):
                            win.hide()
                        else:
                            win.withdraw()
                    except Exception:
                        pass
                continue
            hwnd = 0
            title = ""
            try:
                hwnd, title, _cls = find_main_hwnd_for_pid(pid)
            except Exception:
                hwnd = 0
            # Minimized / hidden game windows: restore once so the title is
            # readable and the window is injectable (a minimized game can report
            # no usable hwnd and its timer path may lag). Then re-resolve.
            if not hwnd or _win_iconic(hwnd):
                rhwnd, rtitle = _one_click_restore_window(pid, hwnd, log=self._log)
                if rhwnd:
                    self._log(
                        f"一键登录: pid={pid} 已恢复最小化/隐藏窗口 hwnd=0x{int(rhwnd):X}"
                    )
                    hwnd = int(rhwnd)
                    title = rtitle
                else:
                    self._log(f"一键登录: pid={pid} 未找到游戏窗口，跳过")
                    skipped += 1
                    continue
            # Pre-filter: only inject games already in-world. Games still at
            # login / char-select cannot pass the post-inject host_context check
            # anyway, so skipping them avoids a wasted inject + ready-wait per
            # instance (the main 一键登录 batch slow-down).
            if _one_click_skip_not_in_role(pid, title, hwnd=hwnd, log=self._log):
                skipped += 1
                continue
            # hwnd is a hint only; pump re-resolves right before inject.
            queue.append(
                {
                    "pid": pid,
                    "hwnd": int(hwnd or 0),
                    "retried": False,
                }
            )

        if already:
            self._log(f"一键登录: 已挂载 {already} 个实例，功能窗保持隐藏")
        if skipped:
            self._log(
                f"一键登录: 跳过 {skipped} 个实例（未进角色/无窗口，进角色后按 Delete 或重试一键登录）"
            )
        if not queue:
            if already or skipped:
                self._log("一键登录: 无需新注入")
            return

        self._inject_queue = queue
        self._inject_hide_after = True
        self._inject_batch_stats = {"ok": 0, "fail": 0, "errors": []}
        self._inject_batch_summary = None
        self._inject_batch_total = len(queue)
        self._inject_batch_index = 0
        refresh_tip = getattr(self, "_refresh_tray_tip", None)
        if callable(refresh_tip):
            refresh_tip()
        self._log(
            f"一键登录: 准备注入 {len(queue)} 个实例（功能窗默认隐藏，"
            f"单实例就绪上限 {INJECT_BATCH_READY_WAIT_S:.0f}s，实例间冷却）"
        )
        self._pump_inject_queue()

    def _pump_inject_queue(self) -> None:
        """
        Start next queued inject when gate is free; summarize batch when done.

        @author by ak
        """
        if self._inject_busy:
            return
        queue = list(getattr(self, "_inject_queue", None) or [])
        if not queue:
            hide_after = bool(getattr(self, "_inject_hide_after", False))
            stats = getattr(self, "_inject_batch_stats", None)
            self._inject_hide_after = False
            self._inject_batch_stats = None
            self._inject_current_source = ""
            self._inject_batch_total = 0
            self._inject_batch_index = 0
            if not hide_after and not stats:
                return
            ok_n = 0
            fail_n = 0
            errors: list[str] = []
            if isinstance(stats, dict):
                try:
                    ok_n = int(stats.get("ok") or 0)
                except Exception:
                    ok_n = 0
                try:
                    fail_n = int(stats.get("fail") or 0)
                except Exception:
                    fail_n = 0
                errors = list(stats.get("errors") or [])
            if ok_n or fail_n:
                self._inject_batch_summary = {
                    "total": ok_n + fail_n,
                    "ok": ok_n,
                    "fail": fail_n,
                }
            if ok_n or fail_n:
                self._log(
                    f"一键登录注入完成: 成功 {ok_n} · 失败 {fail_n}"
                    + (f" · {'; '.join(errors[:3])}" if errors else "")
                )
                if fail_n:
                    try:
                        detail = "\n".join(errors[:5]) if errors else "详见日志"
                        self._show_inject_message(
                            "一键登录",
                            f"已登录。注入成功 {ok_n} 个，失败 {fail_n} 个。\n\n"
                            f"{detail}",
                            kind="warning",
                        )
                    except Exception:
                        pass
            self._refresh_tray_tip()
            return

        item = queue.pop(0)
        self._inject_queue = queue
        try:
            pid = int(item.get("pid") or 0)
        except Exception:
            pid = 0
        try:
            hwnd = int(item.get("hwnd") or 0)
        except Exception:
            hwnd = 0
        if not pid:
            self.after(30, self._pump_inject_queue)
            return
        # Always re-resolve hwnd at inject time (queue snapshot can be stale /
        # splash window while one-click runs in background).
        try:
            live_hwnd, _t, _cls = find_main_hwnd_for_pid(pid)
            if live_hwnd:
                hwnd = int(live_hwnd)
        except Exception:
            pass
        # Stash retried flag for failure recovery in _apply_inject.
        try:
            self._inject_current_meta = {
                "pid": int(pid),
                "retried": bool(item.get("retried")),
            }
        except Exception:
            self._inject_current_meta = {"pid": int(pid), "retried": False}
        # Per-instance progress log (timeout/not-in-role must not hide the queue).
        self._inject_batch_index = int(getattr(self, "_inject_batch_index", 0) or 0) + 1
        total = int(getattr(self, "_inject_batch_total", 0) or 0)
        retried = bool(item.get("retried"))
        self._log(
            f"一键登录: [{self._inject_batch_index}/{total}] 注入 pid={pid} "
            f"hwnd=0x{int(hwnd):X}"
            + ("（失败重试）" if retried else "")
        )
        # Inter-instance cooldown: the game-side bridge/window settles before the
        # next CreateRemoteThread so consecutive clients do not stack up.
        self.after(
            BATCH_INJECT_COOLDOWN_MS,
            self._start_one_click_flow,
            int(pid),
            int(hwnd),
        )

    def _start_one_click_flow(self, pid: int, hwnd: int) -> None:
        """
        Launch one queued 一键登录 inject after the inter-instance cooldown.

        @author by ak
        """
        if bool(getattr(self, "_closing", False)):
            return
        if self._inject_busy:
            self.after(50, self._pump_inject_queue)
            return
        self._start_inject_flow(
            source="一键登录",
            preferred_pid=int(pid or 0),
            preferred_hwnd=int(hwnd or 0),
            ready_wait_s=INJECT_BATCH_READY_WAIT_S,
        )

    def _record_inject_batch(self, *, ok: bool, pid: int = 0, err: str = "", cat: str = "") -> None:
        """Accumulate one-click inject stats. @author by ak"""
        stats = getattr(self, "_inject_batch_stats", None)
        if not isinstance(stats, dict):
            return
        if ok:
            stats["ok"] = int(stats.get("ok") or 0) + 1
        else:
            stats["fail"] = int(stats.get("fail") or 0) + 1
            msg = str(err or "失败").strip()
            if cat:
                msg = f"[{cat}] {msg}"
            if pid:
                msg = f"pid={pid}: {msg}"
            errs = list(stats.get("errors") or [])
            if msg:
                errs.append(msg)
            stats["errors"] = errs[:12]
        refresh_tip = getattr(self, "_refresh_tray_tip", None)
        if callable(refresh_tip):
            refresh_tip()

    def _hide_feature_if_needed(self, pid: int) -> None:
        """Hide feature window when one-click/batch mode is active. @author by ak"""
        if not bool(getattr(self, "_inject_hide_after", False)):
            return
        win = self._feature_win_alive(int(pid))
        if win is None:
            return
        try:
            if hasattr(win, "hide"):
                win.hide()
            else:
                win.withdraw()
        except Exception:
            pass

    def _poll_delete_hotkey(self) -> None:
        """
        Delete only when logged in and foreground is xajh.exe.

        Uses level-edge (0x8000) + short cooldown so one keypress is enough.
        Non-admin sessions only log why inject is blocked (OpenProcess fails).
        @author by ak
        """
        try:
            if self.auth.is_logged_in and self._del_watch.poll_press():
                # Peer packaged helper owns inject: do not steal Delete.
                if not self._refresh_inject_controller(log_change=False):
                    self._log("Delete ignored: 其它通道持有注入控制权")
                # Avoid racing Delete with one-click multi-inject queue.
                elif self._inject_busy or list(
                    getattr(self, "_inject_queue", None) or []
                ):
                    self._log("Delete ignored: 注入进行中")
                else:
                    ok, pid, hwnd = is_xajh_foreground()
                    if ok and pid:
                        self._start_inject_flow(
                            source="Delete", preferred_pid=pid, preferred_hwnd=hwnd
                        )
                    else:
                        # Swallow accidental Delete outside game without retry spam
                        self._log(
                            f"Delete ignored: foreground is not xajh "
                            f"(ok={ok} pid={pid} hwnd=0x{int(hwnd):X})"
                        )
        except Exception as e:
            self._log(f"hotkey poll error: {e}")
        self._hotkey_job = self.after(HOTKEY_POLL_MS, self._poll_delete_hotkey)

    def _start_inject_flow(
        self,
        source: str = "Delete",
        *,
        preferred_pid: int | None = None,
        preferred_hwnd: int = 0,
        ready_wait_s: float | None = None,
    ) -> None:
        """Inject preferred game pid (or newest free) and open feature window. @author by ak"""
        if bool(getattr(self, "_closing", False)):
            return
        if not self.auth.is_logged_in:
            self._log("连接游戏: 请先登录")
            self._show_login_window()
            messagebox.showwarning("连接游戏", "请先登录", parent=self)
            return
        # source/dev must not inject while a higher-priority package is in use
        if source != "一键登录" and not self._ensure_inject_allowed(action="连接游戏"):
            return
        if self._inject_busy:
            self._log(f"{source}: inject busy, skip")
            return
        try:
            # Recover a detached live panel before creating a provisional one.
            self._reconcile_feature_windows()
        except Exception:
            pass

        pid = int(preferred_pid or 0)
        if not pid:
            mounted = self.store.pids()
            for p in find_xajh_processes():
                cand = int(p["pid"])
                if cand not in mounted:
                    pid = cand
                    break
            if not pid:
                procs = find_xajh_processes()
                if not procs:
                    self._log("连接游戏: 未找到游戏进程")
                    messagebox.showerror("连接游戏", "未找到游戏进程", parent=self)
                    return
                # All mounted: re-open feature window for foreground / first
                if preferred_pid and self.store.has(preferred_pid):
                    if bool(getattr(self, "_inject_hide_after", False)):
                        self._hide_feature_if_needed(int(preferred_pid))
                        self.after(30, self._pump_inject_queue)
                    else:
                        self._open_or_focus_feature(preferred_pid)
                    return
                items = self.store.list()
                if items:
                    if bool(getattr(self, "_inject_hide_after", False)):
                        self._hide_feature_if_needed(int(items[0].pid))
                        self.after(30, self._pump_inject_queue)
                    else:
                        self._open_or_focus_feature(items[0].pid)
                return

        # Already mounted: focus/open unless one-click wants UI hidden
        if self.store.has(pid):
            if bool(getattr(self, "_inject_hide_after", False)):
                self._log(f"{source}: pid={pid} 已挂载，功能窗保持隐藏")
                self._hide_feature_if_needed(pid)
                self.after(30, self._pump_inject_queue)
            else:
                self._log(f"{source}: pid={pid} 已挂载，打开功能窗")
                self._open_or_focus_feature(pid)
            return

        self._inject_current_source = str(source or "")
        self._inject_busy = True
        if str(source or "") != "一键登录":
            self._inject_batch_summary = None
            self._refresh_tray_tip()
        # Avoid double-trigger while inject thread runs
        self._del_watch.arm_cooldown(0.8)
        self._log(f"{source} 注入目标 pid={pid} hwnd=0x{int(preferred_hwnd):X}")
        # Immediate UI: open feature window in loading state (no long blank wait).
        self._open_loading_feature(
            pid=pid,
            hwnd=int(preferred_hwnd or 0),
            hide_ui=bool(getattr(self, "_inject_hide_after", False)),
        )

        def on_done(out) -> None:
            if bool(getattr(self, "_closing", False)):
                return
            self.msg_q.put(
                ("__INJECT__", out.to_dict() if hasattr(out, "to_dict") else out)
            )

        def on_phase(key: str, text: str) -> None:
            if bool(getattr(self, "_closing", False)):
                return
            self.msg_q.put(("__INJECT_PHASE__", int(pid), str(key), str(text)))

        run_delete_inject_async(
            on_done,
            preferred_pid=pid,
            preferred_hwnd=int(preferred_hwnd or 0) or None,
            # Console only — inject_gate already writes file with [INJECT] tag.
            log=lambda m: print(
                f"[{time.strftime('%H:%M:%S')}] {m}", flush=True
            ),
            on_phase=on_phase,
            ready_wait_s=ready_wait_s,
        )

    def _open_loading_feature(
        self, *, pid: int, hwnd: int = 0, hide_ui: bool = False
    ) -> None:
        """
        Open feature window immediately with inject loading gate.

        hide_ui=True: create/update window but keep it withdrawn (一键登录).
        @author by ak
        """
        pid = int(pid)
        hwnd = int(hwnd or 0)
        title = get_window_title(hwnd) if hwnd else ""
        original = title
        # Provisional mount so pages can resolve fixed_pid; bridge_ready=False
        # blocks all feature actions until inject succeeds.
        sess = GameSession(
            pid=pid,
            hwnd=hwnd,
            title=title or f"xajh pid={pid}",
            original_title=original,
            bridge_note="injecting",
            ping_ret=None,
        )
        self.store.mount(sess)
        try:
            from app.core.crash_capture import clear_game_crash_dedupe

            clear_game_crash_dedupe(int(sess.pid))
        except Exception:
            pass
        win = self._feature_win_alive(pid)
        if win is not None:
            if bool(getattr(win, "_bridge_ready", False)):
                try:
                    self.store.mount(win.session)
                except Exception:
                    pass
                self._log(f"恢复已存在功能窗 pid={pid}，跳过重复注入")
                return
            try:
                win.set_bridge_ready(False, text="重新连接中…", session=sess)
                if hide_ui:
                    if hasattr(win, "hide"):
                        win.hide()
                    else:
                        win.withdraw()
                else:
                    win.show_and_focus()
            except Exception:
                pass
            return
        self._create_feature_window(
            pid, sess, bridge_ready=False, start_hidden=bool(hide_ui)
        )


    def _refresh_role_live(self) -> None:
        """
        Sync per-role live status (map / running actions) from feature windows.

        Powers the account-list 状态 column (登录·地图·操作).

        @author by ak
        """
        try:
            for pid, win in list(self._feature_wins.items()):
                try:
                    rid = str(getattr(win.session, "role_id", "") or "").strip()
                except Exception:
                    rid = ""
                if not rid:
                    continue
                try:
                    alive = win.winfo_exists()
                except Exception:
                    alive = False
                if not alive:
                    continue
                try:
                    hdr_map = str(getattr(win, "_hdr_map", "") or "")
                except Exception:
                    hdr_map = ""
                try:
                    actions = "、".join(list(getattr(win, "_active_keys", None) or []))
                except Exception:
                    actions = ""
                self._role_live[rid] = {
                    "pid": int(pid or 0),
                    "map": hdr_map,
                    "action": actions,
                }
            # drop stale entries whose game is no longer mounted
            mounted_rids = {
                str(getattr(win.session, "role_id", "") or "").strip()
                for win in list(self._feature_wins.values())
                if getattr(win.session, "role_id", "")
            }
            for rid in list(self._role_live.keys()):
                if rid not in mounted_rids:
                    self._role_live.pop(rid, None)
        except Exception:
            pass

    def account_live_statuses(self) -> dict[str, dict]:
        """Live per-role status snapshot for the account manager window. @author by ak"""
        try:
            self._refresh_role_live()
        except Exception:
            pass
        return dict(self._role_live)

    def _poll_mounted_game_health(self) -> None:
        """
        Watch mounted game pids; on death capture 游戏崩溃 point and unload.

        @author by ak
        """
        self._health_job = None
        if bool(getattr(self, "_closing", False)):
            return
        self._refresh_role_live()
        try:
            from app.core import diag_log
            from app.core.crash_capture import report_game_crash

            self._reconcile_feature_windows()
            for sess in list(self.store.list()):
                pid = int(getattr(sess, "pid", 0) or 0)
                if pid <= 0:
                    continue
                if diag_log.process_alive(pid):
                    if self._session_game_window_alive(sess):
                        self._session_invalid_counts.pop(pid, None)
                        continue
                    misses = int(self._session_invalid_counts.get(pid, 0)) + 1
                    self._session_invalid_counts[pid] = misses
                    # One transient miss is allowed during window recreation.
                    if misses < 2:
                        continue
                    self._log(
                        f"游戏主窗口已消失 pid={pid}，销毁功能窗并卸载会话"
                    )
                    self.unload_feature_for_pid(
                        pid, reason="game_window_missing"
                    )
                    continue
                self._session_invalid_counts.pop(pid, None)
                title = str(getattr(sess, "title", "") or "")
                try:
                    point = report_game_crash(
                        pid,
                        title=title,
                        reason="session_health_poll",
                    )
                except Exception as e:
                    point = f"report_failed:{e}"
                self._log(
                    f"游戏崩溃 pid={pid} crash_point={point or '?'} — 卸载会话"
                )
                try:
                    self.unload_feature_for_pid(
                        pid, reason=f"game_crash:{point or 'unknown'}"
                    )
                except Exception as e:
                    self._log(f"unload after game crash failed pid={pid}: {e}")
        except Exception as e:
            try:
                self._log(f"game health poll error: {e}")
            except Exception:
                pass
        try:
            if not bool(getattr(self, "_closing", False)):
                self._health_job = self.after(
                    2000, self._poll_mounted_game_health
                )
        except Exception:
            self._health_job = None

    def _session_game_window_alive(self, sess: GameSession) -> bool:
        """Confirm or refresh the mounted top-level game window for a live pid."""
        pid = int(getattr(sess, "pid", 0) or 0)
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        if pid <= 0:
            return False
        if hwnd:
            try:
                import ctypes
                from ctypes import wintypes

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                owner_pid = wintypes.DWORD(0)
                if user32.IsWindow(wintypes.HWND(hwnd)):
                    user32.GetWindowThreadProcessId(
                        wintypes.HWND(hwnd), ctypes.byref(owner_pid)
                    )
                    if int(owner_pid.value) == pid:
                        return True
            except Exception:
                pass
        try:
            live_hwnd, title, _cls = find_main_hwnd_for_pid(pid)
        except Exception:
            live_hwnd, title = 0, ""
        if not live_hwnd:
            return False
        sess.hwnd = int(live_hwnd)
        if title:
            sess.title = str(title)
        return True

    def _feature_windows_for_pid(self, pid: int) -> list[SessionFeatureWindow]:
        """Return tracked and detached feature Toplevels for one game pid."""
        pid = int(pid or 0)
        found: list[SessionFeatureWindow] = []
        seen: set[int] = set()
        tracked = self._feature_wins.get(pid)
        if tracked is not None:
            found.append(tracked)
            seen.add(id(tracked))
        try:
            children = list(self.winfo_children())
        except Exception:
            children = []
        for child in children:
            if not isinstance(child, SessionFeatureWindow):
                continue
            try:
                child_pid = int(getattr(child.session, "pid", 0) or 0)
            except Exception:
                child_pid = 0
            if child_pid == pid and id(child) not in seen:
                found.append(child)
                seen.add(id(child))
        return found

    @staticmethod
    def _feature_window_rank(win: SessionFeatureWindow) -> tuple[int, int]:
        """Prefer a ready/running panel over a provisional loading duplicate."""
        try:
            ready = int(bool(getattr(win, "_bridge_ready", False)))
        except Exception:
            ready = 0
        try:
            active = len(list(getattr(win, "_active_keys", None) or []))
        except Exception:
            active = 0
        return ready, active

    @staticmethod
    def _shutdown_destroy_feature_window(win: SessionFeatureWindow) -> None:
        try:
            win.shutdown()
        except Exception:
            pass
        try:
            win.destroy()
        except Exception:
            pass

    def _reconcile_feature_windows(self) -> None:
        """Destroy detached/duplicate panels and repair missing live references."""
        mounted = set(self.store.pids())
        pids = set(self._feature_wins)
        try:
            children = list(self.winfo_children())
        except Exception:
            children = []
        for child in children:
            if isinstance(child, SessionFeatureWindow):
                try:
                    pids.add(int(getattr(child.session, "pid", 0) or 0))
                except Exception:
                    pass
        for pid in {int(p) for p in pids if int(p or 0) > 0}:
            windows = self._feature_windows_for_pid(pid)
            live: list[SessionFeatureWindow] = []
            for win in windows:
                try:
                    if win.winfo_exists():
                        live.append(win)
                except Exception:
                    pass
            if pid not in mounted:
                alive = False
                try:
                    from app.core import diag_log

                    alive = bool(diag_log.process_alive(pid))
                except Exception:
                    alive = False
                keep_live = any(
                    bool(getattr(win, "_bridge_ready", False))
                    or bool(getattr(win, "_active_keys", None))
                    for win in live
                )
                if alive and live and keep_live:
                    ordered = sorted(
                        live, key=self._feature_window_rank, reverse=True
                    )
                    canonical = ordered[0]
                    try:
                        self.store.mount(canonical.session)
                    except Exception:
                        pass
                    for duplicate in ordered[1:]:
                        self._shutdown_destroy_feature_window(duplicate)
                    self._feature_wins[pid] = canonical
                    self._log(f"已恢复脱离会话的功能窗 pid={pid}")
                else:
                    self._feature_wins.pop(pid, None)
                    for win in live:
                        self._shutdown_destroy_feature_window(win)
                    if live:
                        self._log(f"已销毁孤儿功能窗 pid={pid} count={len(live)}")
                continue
            if not live:
                self._feature_wins.pop(pid, None)
                continue
            ordered = sorted(live, key=self._feature_window_rank, reverse=True)
            canonical = ordered[0]
            for duplicate in ordered[1:]:
                self._shutdown_destroy_feature_window(duplicate)
            # Duplicate destruction invokes on_close, so assign canonical last.
            self._feature_wins[pid] = canonical
            if len(live) > 1:
                self._log(f"已销毁重复功能窗 pid={pid} count={len(live) - 1}")

    def unload_feature_for_pid(self, pid: int, *, reason: str = "") -> None:
        """
        Stop runners, destroy feature window, and unmount one dead client only.

        Used when that game process exits/crashes mid-feature (e.g. activity).
        @author by ak
        """
        pid = int(pid or 0)
        if not pid:
            return
        unloading = getattr(self, "_unloading_pids", None)
        if unloading is None:
            unloading = set()
            self._unloading_pids = unloading
        if pid in unloading:
            return
        unloading.add(pid)
        self._log(
            f"卸载会话 pid={pid}"
            + (f" reason={reason}" if reason else "")
        )
        try:
            windows = self._feature_windows_for_pid(pid)
            self._feature_wins.pop(pid, None)
            for win in windows:
                self._shutdown_destroy_feature_window(win)
            try:
                # Strip title markers if hwnd still listed (usually already dead).
                sess = self.store.get(pid)
                if sess is not None and getattr(sess, "hwnd", None):
                    try:
                        from app.core.window_title import strip_gui_title_suffix

                        strip_gui_title_suffix(int(sess.hwnd))
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                self.store.unmount(pid)
            except Exception:
                pass
            # Complete cleanup even if the window reference was already lost.
            try:
                from app.core.hang_settings import stop_hang_guard

                stop_hang_guard(pid, log=lambda _m: None)
            except Exception:
                pass
            try:
                from app.core.safe_dispatch import get_dispatch

                get_dispatch().drop_pid(pid)
            except Exception:
                pass
            try:
                from app.core.task_sync import get_task_sync_hub
                from app.core.cloud_sync import drop_cloud_sync_bridge

                get_task_sync_hub().unregister(pid)
                drop_cloud_sync_bridge(pid)
            except Exception:
                pass
            try:
                from app.core.live_scene_hub import drop_live_scene_hub

                drop_live_scene_hub(pid)
            except Exception:
                pass
            self._session_invalid_counts.pop(pid, None)
            try:
                self._del_watch.reset()
            except Exception:
                pass
        finally:
            unloading.discard(pid)

    def _create_feature_window(
        self,
        pid: int,
        sess: GameSession,
        *,
        bridge_ready: bool = True,
        start_hidden: bool = False,
    ) -> SessionFeatureWindow | None:
        """Create SessionFeatureWindow for pid. @author by ak"""
        if bool(getattr(self, "_closing", False)):
            return None

        def on_close(closed_pid: int) -> None:
            self._feature_wins.pop(int(closed_pid), None)
            self._del_watch.reset()
            self._log(f"功能窗已销毁 pid={closed_pid}")

        def on_business_invalid(closed_pid: int, reason: str) -> None:
            # The live header runs on Tk's main thread.  Defer destruction until
            # its current callback returns, then use the same full teardown path
            # as a dead game process.
            try:
                self.after(
                    0,
                    lambda: self.unload_feature_for_pid(
                        int(closed_pid), reason=f"business_invalid:{reason}"
                    ),
                )
            except Exception:
                self.unload_feature_for_pid(
                    int(closed_pid), reason=f"business_invalid:{reason}"
                )

        win = None
        try:
            win = SessionFeatureWindow(
                self,
                self.store,
                sess,
                log_fn=self._log,
                on_close=on_close,
                on_business_invalid=on_business_invalid,
                auth=self.auth,
                debug_mode=bool(getattr(self, "debug_mode", True)),
                bridge_ready=bridge_ready,
                start_hidden=bool(start_hidden),
            )
            self._feature_wins[int(pid)] = win
            self._del_watch.arm_cooldown(0.45)
            return win
        except Exception as e:
            self._log(f"创建功能窗失败 pid={pid}: {e}")
            self._feature_wins.pop(int(pid), None)
            # Toplevel is created in SessionFeatureWindow.__init__ before pages
            # finish building — must destroy the orphan or Delete opens a 2nd window.
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass
            else:
                # Best-effort: wipe any unfinished Toplevel children titled for this pid.
                try:
                    needle = f"功能 · {int(pid)}"
                    for child in list(self.winfo_children()):
                        try:
                            title = str(child.title()) if hasattr(child, "title") else ""
                        except Exception:
                            title = ""
                        if needle in title:
                            try:
                                child.destroy()
                            except Exception:
                                pass
                except Exception:
                    pass
            return None

    def _apply_inject_phase(self, pid: int, phase: str, text: str) -> None:
        """Update loading banner on the feature window. @author by ak"""
        win = self._feature_win_alive(int(pid))
        if win is None:
            return
        try:
            win.set_inject_phase(phase, text)
        except Exception:
            pass

    def _apply_inject(self, d: dict) -> None:
        """Finalize mount after inject and unlock feature UI. @author by ak"""
        if bool(getattr(self, "_closing", False)):
            return
        self._inject_busy = False
        try:
            from app.core import diag_log

            diag_log.info(f"apply_inject payload={d!r}", tag="UI")
        except Exception:
            pass
        pid = int((d or {}).get("pid") or 0)
        if not d or not d.get("ok"):
            err = (d or {}).get("error") or "未知错误"
            code = (d or {}).get("fail_code") or ""
            if code:
                self._log(f"注入失败: [{code}] {err}")
            else:
                self._log(f"注入失败: {err}")
            batch = self._inject_current_source == "一键登录" and (
                bool(getattr(self, "_inject_hide_after", False))
                or bool(getattr(self, "_inject_batch_stats", None))
            )
            win = self._feature_win_alive(pid) if pid else None
            preserve_business = bool(
                win is not None
                and (
                    bool(getattr(win, "_bridge_ready", False))
                    or bool(getattr(win, "_active_keys", None))
                )
            )
            if pid:
                if preserve_business and win is not None:
                    try:
                        self.store.mount(win.session)
                    except Exception:
                        pass
                    self._feature_wins[pid] = win
                    self._log(f"注入失败但保留现有业务窗 pid={pid}")
                else:
                    try:
                        self.store.unmount(pid)
                    except Exception:
                        pass
                    # A provisional loading panel must be gone before opening
                    # the modal. Destroying a messagebox owner from a nested Tk
                    # health callback causes a native access violation.
                    if win is not None:
                        self._feature_wins.pop(pid, None)
                        self._shutdown_destroy_feature_window(win)
            if not batch:
                self._inject_busy = True
                try:
                    self._show_inject_message(
                        "连接游戏",
                        f"连接失败：\n{err}",
                        kind="error",
                        parent=None,
                    )
                except Exception as dialog_err:
                    self._log(f"显示注入错误弹窗失败: {dialog_err}")
                finally:
                    self._inject_busy = False
            self._del_watch.reset()
            if batch:
                meta = getattr(self, "_inject_current_meta", None) or {}
                retried = bool(meta.get("retried"))
                cat = batch_failure_category(code, err)
                retriable = is_batch_retryable(code)
                if (not retried) and retriable and pid:
                    self._log(
                        f"一键登录: pid={pid} 首次失败 [{cat}/{code or 'UNKNOWN'}]，"
                        f"1.2s 后自动重试一次"
                    )
                    q = list(getattr(self, "_inject_queue", None) or [])
                    q.insert(
                        0,
                        {
                            "pid": int(pid),
                            "hwnd": 0,
                            "retried": True,
                        },
                    )
                    self._inject_queue = q
                    self.after(1200, self._pump_inject_queue)
                else:
                    # 未进角色 / 超时 / 崩溃 / 权限等：记录后继续下一个实例。
                    self._log(
                        f"一键登录: pid={pid} 失败[{cat}] "
                        f"{str(err)[:120] if err else ''}"
                    )
                    self._record_inject_batch(
                        ok=False, pid=pid, err=str(err), cat=cat
                    )
                    self.after(50, self._pump_inject_queue)
            else:
                self._inject_current_source = ""
            return

        if not pid:
            pid = int(d.get("pid") or 0)
        hwnd = int(d.get("hwnd") or 0)
        if not hwnd and pid:
            hwnd, _t, _cls = find_main_hwnd_for_pid(pid)
        title = str(d.get("title") or "") or get_window_title(hwnd)
        original = title
        if hwnd:
            title = ensure_gui_title_suffix(hwnd)
        sess = GameSession(
            pid=pid,
            hwnd=hwnd,
            title=title,
            original_title=original,
            bridge_note=str(d.get("bridge_note") or ""),
            ping_ret=d.get("ping_ret"),
        )
        self.store.mount(sess)
        try:
            from app.core.crash_capture import clear_game_crash_dedupe

            clear_game_crash_dedupe(int(sess.pid))
        except Exception:
            pass
        reused = bool(d.get("reused"))
        self._log(
            f"挂载成功 pid={pid} hwnd=0x{hwnd:X} title={title!r} "
            f"ping={d.get('ping_ret')} reused={reused}"
        )
        # 注入后绑定：后台读角色 id+名，写入/合并 roles/{role_id}（不影响挂载流程）。
        self._bind_injected_role_async(pid, title=original)
        hide_ui = bool(getattr(self, "_inject_hide_after", False))
        win = self._feature_win_alive(pid)
        if win is not None:
            try:
                win.set_bridge_ready(
                    True,
                    text="连接完成，功能已可用",
                    session=sess,
                )
                if hide_ui:
                    if hasattr(win, "hide"):
                        win.hide()
                    else:
                        win.withdraw()
                else:
                    win.show_and_focus()
            except Exception as e:
                self._log(f"解锁功能窗失败: {e}")
                if not hide_ui:
                    self._open_or_focus_feature(pid)
        else:
            if hide_ui:
                self._create_feature_window(
                    pid, sess, bridge_ready=True, start_hidden=True
                )
            else:
                self._open_or_focus_feature(pid)
        if hide_ui or getattr(self, "_inject_batch_stats", None) is not None:
            self._record_inject_batch(ok=True, pid=pid)
            self.after(50, self._pump_inject_queue)

    def _bind_injected_role_async(self, pid: int, title: str = "") -> None:
        """
        注入后绑定：读角色 id+名，写入/合并 roles/{role_id} 并回填账号槽位。

        Background thread so a slow GetObjectName CRT never blocks the UI /
        one-click queue. Result is posted back to the message queue for 日志.

        @author by ak
        """
        pid = int(pid or 0)
        if pid <= 0:
            return

        def worker() -> None:
            rid = ""
            name = ""
            errors: list[str] = []
            # The injected DLL is the authoritative in-process identity path.
            # HOST_SNAPSHOT returns the host id from the game UI thread, so this
            # does not depend on a foreign CRT call being accepted immediately
            # after LoadLibrary.
            for _attempt in range(3):
                try:
                    from app.core.xajh_bridge import ensure_bridge

                    bridge = ensure_bridge(
                        pid, hwnd=self.store.get(pid).hwnd if self.store.get(pid) else None,
                        inject_if_needed=False, log=lambda _m: None
                    )
                    if bridge is not None:
                        try:
                            snap = bridge.host_snapshot(timeout_ms=1800)
                            oid = (int(snap.id_hi) << 32) | int(snap.id_lo)
                            if snap.ok and oid > 0:
                                rid = str(oid)
                                break
                        finally:
                            try:
                                bridge.close()
                            except Exception:
                                pass
                except Exception as e:
                    errors.append(f"dll={e}")
                time.sleep(0.5)

            if not rid:
                try:
                    from app.core.super_loot import open_attach_session
                    from app.core.team_ops import read_host_identity

                    attach = open_attach_session(pid, log=lambda _m: None)
                    try:
                        name, oid = read_host_identity(attach, log=lambda _m: None)
                    finally:
                        try:
                            attach.close()
                        except Exception:
                            pass
                    oid = int(oid or 0)
                    rid = str(oid) if oid > 0 else ""
                except Exception as e:
                    errors.append(f"rpm={e}")
            if not rid:
                self.msg_q.put(
                    ("__ROLE_BOUND_ERR__", pid, "; ".join(errors) or "role_id=0")
                )
                return
            try:
                from app.core.account_manager import merge_role_from_inject

                merge_role_from_inject(rid, name=str(name or ""), pid=pid)
            except Exception as e:
                self.msg_q.put(("__ROLE_BOUND_ERR__", pid, str(e)))
                return
            self.msg_q.put(("__ROLE_BOUND__", pid, rid, str(name or "")))

        threading.Thread(target=worker, daemon=True, name="xajh-role-bind").start()

    def _feature_win_alive(self, pid: int) -> SessionFeatureWindow | None:
        """
        Return live feature window for pid, or drop stale ref.

        @author by ak
        """
        pid = int(pid)
        win = self._feature_wins.get(pid)
        if win is None:
            return None
        try:
            if win.winfo_exists():
                return win
        except Exception:
            pass
        self._feature_wins.pop(pid, None)
        return None

    def _open_or_focus_feature(self, pid: int) -> None:
        """
        One feature Toplevel per mounted pid.

        Window X only hides; runners keep going. Delete restores the same window
        so auto-open / yaolu / activity stay in their running state.

        @author by ak
        """
        pid = int(pid)
        win = self._feature_win_alive(pid)
        if win is not None:
            try:
                if hasattr(win, "show_and_focus"):
                    win.show_and_focus()
                else:
                    win.deiconify()
                    win.lift()
                    win.attributes("-topmost", True)
                    win.after(200, lambda w=win: self._clear_topmost(w))
                    win.focus_force()
                self._del_watch.arm_cooldown(0.4)
                self._log(f"功能窗已前置 pid={pid}（业务状态保留）")
                return
            except Exception as e:
                self._log(f"功能窗前置失败 pid={pid}: {e}")
                # Only drop ref if window is truly dead; do not stop runners on hide.
                try:
                    if not win.winfo_exists():
                        self._feature_wins.pop(pid, None)
                    else:
                        return
                except Exception:
                    self._feature_wins.pop(pid, None)

        sess = self.store.get(pid)
        if sess is None:
            self._log(f"打开功能窗失败: 无会话 pid={pid}")
            return
        self._create_feature_window(pid, sess, bridge_ready=True)

    def _clear_topmost(self, win: tk.Misc) -> None:
        """Drop temporary topmost used to steal focus. @author by ak"""
        try:
            if win.winfo_exists():
                win.attributes("-topmost", False)
        except Exception:
            pass

    def _show_inject_message(
        self,
        title: str,
        message: str,
        *,
        kind: str = "error",
        parent: tk.Misc | None = None,
    ) -> None:
        """Show an inject result above the game even while the shell is in tray."""
        owner = parent
        try:
            if owner is not None and not owner.winfo_exists():
                owner = None
        except Exception:
            owner = None

        restore_tray = False
        if owner is None:
            owner = self
            restore_tray = bool(getattr(self, "_hidden_to_tray", False))
            try:
                self.deiconify()
            except Exception:
                pass

        try:
            owner.deiconify()
        except Exception:
            pass
        try:
            owner.lift()
            owner.attributes("-topmost", True)
            owner.focus_force()
            owner.update_idletasks()
        except Exception:
            pass

        try:
            if str(kind).lower() == "warning":
                messagebox.showwarning(title, message, parent=owner)
            else:
                messagebox.showerror(title, message, parent=owner)
        finally:
            try:
                owner.attributes("-topmost", False)
            except Exception:
                pass
            if restore_tray and not bool(getattr(self, "_closing", False)):
                self._hide_to_tray()

    # ------------------------------------------------------------- helpers
    def _log(self, msg: str) -> None:
        """
        Console + persistent log file.

        High-frequency package/NPC chatter is filtered to keep overnight
        logs small; errors and milestones still pass through.

        @author by ak
        """
        s = str(msg or "")
        # keep formal panel path quiet even if a page still forwards noise
        noisy = (
            "package_api: package=",
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
        )
        if any(x in s for x in noisy):
            return
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        try:
            from app.core import diag_log

            diag_log.info(msg, tag="UI")
        except Exception:
            pass

    def _on_sessions_changed(self) -> None:
        # Feature windows refresh themselves via store listeners on pages
        pass

    def open_formal_panel_from_workbench(
        self,
        *,
        pid: int,
        hwnd: int = 0,
        title: str = "",
        bridge_note: str = "workbench",
        ping_ret: int | None = None,
    ) -> bool:
        """
        Open the normal per-game feature window for an already-bridged source
        workbench target. This path never injects and is unavailable to frozen
        dev/prod packages, so other running helper scopes remain untouched.
        """
        if str(getattr(self, "_instance_scope", "") or "") != "source" or is_packaged():
            self._log("工作台进入正式面板: 仅源码命令行通道可用")
            return False
        if bool(getattr(self, "_closing", False)):
            return False
        if not self.auth.is_logged_in:
            self._log("工作台进入正式面板: 请先在主窗口登录")
            self._show_login_window()
            messagebox.showwarning(
                "进入正式面板",
                "请先在源码主窗口完成登录，再从工作台进入正式面板。",
                parent=self,
            )
            return False

        pid = int(pid or 0)
        if pid <= 0:
            self._log("工作台进入正式面板: pid 无效")
            return False
        try:
            live_hwnd, live_title, _cls = find_main_hwnd_for_pid(pid)
        except Exception:
            live_hwnd, live_title = 0, ""
        if not live_hwnd:
            self._log(f"工作台进入正式面板: 游戏窗口不存在 pid={pid}")
            messagebox.showerror(
                "进入正式面板",
                f"没有找到 PID {pid} 的游戏窗口。",
                parent=self,
            )
            return False
        hwnd = int(live_hwnd or hwnd or 0)
        title = str(live_title or title or f"xajh pid={pid}")

        try:
            self._reconcile_feature_windows()
        except Exception:
            pass
        existing = self.store.get(pid)
        if existing is not None:
            existing.hwnd = hwnd
            existing.title = title
            if bridge_note:
                existing.bridge_note = str(bridge_note)
            if ping_ret is not None:
                existing.ping_ret = int(ping_ret)
            self.store.mount(existing)
            self._open_or_focus_feature(pid)
            self._log(f"工作台进入正式面板: 已前置 pid={pid}")
            return True

        sess = GameSession(
            pid=pid,
            hwnd=hwnd,
            title=title,
            original_title=title,
            bridge_note=str(bridge_note or "workbench"),
            ping_ret=int(ping_ret) if ping_ret is not None else None,
            note="source_workbench_handoff",
        )
        self.store.mount(sess)
        win = self._create_feature_window(
            pid,
            sess,
            bridge_ready=True,
            start_hidden=False,
        )
        if win is None:
            self.store.unmount(pid)
            return False
        self._log(
            f"工作台进入正式面板: 已打开 pid={pid} hwnd=0x{hwnd:X}，未重复注入"
        )
        return True

    def _open_account_manager(self) -> None:
        """
        Open the standalone account-manager window from tray (logged-in only).

        One shared Toplevel; reopens (focuses) if already alive.

        @author by ak
        """
        if not self.auth.is_logged_in:
            self._log("账号管理: 请先登录")
            return
        win = getattr(self, "_account_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    try:
                        win.deiconify()
                        win.lift()
                        win.focus_force()
                    except Exception:
                        pass
                    win.refresh_all()
                    return
            except Exception:
                win = None
        try:
            from app.ui.pages.account_manager import AccountManagerWindow

            win = AccountManagerWindow(
                master=self,
                log=lambda m: self._log(m),
                live_provider=self.account_live_statuses,
            )

            def _on_close() -> None:
                # 账号管理是独立配置窗口；关闭它不应影响运行中的功能任务。
                try:
                    win.destroy()
                except Exception:
                    pass
                self._account_win = None

            try:
                win.protocol("WM_DELETE_WINDOW", _on_close)
            except Exception:
                pass
            self._account_win = win
            self._log("账号管理已打开")
        except Exception as e:
            self._log(f"打开账号管理失败: {e}")
            messagebox.showerror("账号管理", f"打开失败:\n{e}", parent=self)

    def _open_debug(self) -> None:
        """
        Open legacy workbench (dev only; not shown when packaged).

        @author by ak
        """
        if is_packaged() and not is_dev_build():
            self._log("开发者面板: production package, skip")
            return
        if self._debug_win is not None:
            try:
                if self._debug_win.winfo_exists():
                    self._debug_win.deiconify()
                    self._debug_win.lift()
                    self._debug_win.focus_force()
                    return
            except Exception:
                self._debug_win = None
        try:
            from app.ui.main_window import WorkbenchApp

            win = WorkbenchApp(master=self)
            win.title("XAJH Workbench · 开发调试")
            self._debug_win = win

            def _on_dbg_close() -> None:
                try:
                    win.destroy()
                except Exception:
                    pass
                self._debug_win = None

            try:
                win.protocol("WM_DELETE_WINDOW", _on_dbg_close)
            except Exception:
                pass
            self._log("开发者面板已打开")
        except Exception as e:
            self._log(f"打开开发者面板失败: {e}")
            messagebox.showerror("开发者面板", f"打开失败:\n{e}", parent=self)

    def _drain_queue(self) -> None:
        self._queue_job = None
        if bool(getattr(self, "_closing", False)):
            return
        try:
            while True:
                item = self.msg_q.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__INJECT__":
                    self._apply_inject(item[1] if len(item) > 1 else {})
                elif (
                    isinstance(item, tuple)
                    and item
                    and item[0] == "__INJECT_PHASE__"
                ):
                    # ("__INJECT_PHASE__", pid, phase, text)
                    pid = int(item[1]) if len(item) > 1 else 0
                    phase = str(item[2]) if len(item) > 2 else ""
                    text = str(item[3]) if len(item) > 3 else ""
                    self._apply_inject_phase(pid, phase, text)
                elif isinstance(item, tuple) and item and item[0] == "__TRAY_SHOW__":
                    self._show_login_window()
                elif isinstance(item, tuple) and item and item[0] == "__TRAY_ACCOUNTS__":
                    self._open_account_manager()
                elif isinstance(item, tuple) and item and item[0] == "__TRAY_DEV__":
                    self._open_debug()
                elif isinstance(item, tuple) and item and item[0] == "__ROLE_BOUND__":
                    # ("__ROLE_BOUND__", pid, role_id, name)
                    try:
                        rid = str(item[2] or "").strip()
                        pid = int(item[1] or 0)
                        if rid:
                            name = str(item[3] or "").strip()
                            bound_session = self.store.bind_injected_role(
                                pid, rid, name
                            )
                            if bound_session is None:
                                self._log(
                                    f"注入角色绑定冲突 pid={pid} role_id={rid}，"
                                    "保留当前注入会话身份"
                                )
                                continue
                            self._role_live[rid] = {
                                "pid": pid,
                                "map": "",
                                "action": "",
                            }
                            win = self._feature_win_alive(pid)
                            if win is not None:
                                try:
                                    win.session.role_id = bound_session.role_id
                                    win.session.role_name = bound_session.role_name
                                except Exception:
                                    pass
                                # Default landing page is 快捷设置. Rehydrate
                                # role/control vars as soon as injection identity
                                # is known, before the user can click 保存.
                                try:
                                    pages = getattr(win, "_pages", None) or {}
                                    settings_page = pages.get("settings")
                                    if settings_page is not None and hasattr(
                                        settings_page, "on_page_show"
                                    ):
                                        settings_page.on_page_show()
                                except Exception as e:
                                    self._log(
                                        f"注入后刷新快捷设置失败 pid={pid}: {e}"
                                    )
                    except Exception:
                        pass
                    self._log(
                        f"注入后绑定: pid={item[1]} role_id={item[2]} "
                        f"name={item[3] or ''!r}"
                    )
                elif isinstance(item, tuple) and item and item[0] == "__ROLE_BOUND_ERR__":
                    self._log(f"注入后绑定失败: pid={item[1]} {item[2]}")
                elif isinstance(item, tuple) and item and item[0] == "__TRAY_QUIT__":
                    self._quit_app()
                    return
                else:
                    self._log(str(item))
        except queue.Empty:
            pass
        except Exception as e:
            if not bool(getattr(self, "_closing", False)):
                try:
                    self._log(f"UI 消息队列异常: {e}")
                except Exception:
                    pass
        if not bool(getattr(self, "_closing", False)):
            try:
                self._queue_job = self.after(50, self._drain_queue)
            except Exception:
                self._queue_job = None

    def _on_user_close(self) -> None:
        """
        Close button: if logged in, hide to tray; else quit.

        @author by ak
        """
        if self.auth.is_logged_in:
            self._hide_to_tray()
            return
        self._quit_app()

    def _quit_app(self) -> None:
        """
        Full GUI shutdown.

        Stops feature runners and closes local UI only. Does NOT unload
        xajh_bridge from the game process (game must survive helper exit).
        Bridge DLL is staged under the running software root's `runtime` folder;
        cleanup remains self-contained and build-specific DLL names avoid
        collisions with an older game process.
        @author by ak
        """
        if bool(getattr(self, "_closing", False)):
            return
        self._closing = True
        self._inject_busy = False
        self._inject_queue = []
        self._inject_current_source = ""
        self._inject_batch_total = 0
        self._inject_batch_index = 0
        self._inject_batch_summary = None
        queue_job = getattr(self, "_queue_job", None)
        if queue_job is not None:
            try:
                self.after_cancel(queue_job)
            except Exception:
                pass
            self._queue_job = None
        reverify_job = getattr(self, "_reverify_job", None)
        if reverify_job is not None:
            try:
                self.after_cancel(reverify_job)
            except Exception:
                pass
            self._reverify_job = None
        health_job = getattr(self, "_health_job", None)
        if health_job is not None:
            try:
                self.after_cancel(health_job)
            except Exception:
                pass
            self._health_job = None
        try:
            from app.core.game_multi_open import MultiOpenWatchdog

            # Stop only the live watchdog; keep disk patch state per login prefs.
            import app.core.game_multi_open as _gmo

            wd = getattr(_gmo, "_watchdog", None)
            if wd is not None:
                wd.stop()
                _gmo._watchdog = None
        except Exception:
            pass
        if self._hotkey_job is not None:
            try:
                self.after_cancel(self._hotkey_job)
            except Exception:
                pass
            self._hotkey_job = None
        # Stop loops first so daemon threads release pymem/bridge handles cleanly.
        for pid, win in list(self._feature_wins.items()):
            try:
                if hasattr(win, "shutdown"):
                    win.shutdown()
            except Exception:
                pass
        for s in self.store.list():
            if s.hwnd:
                try:
                    strip_gui_title_suffix(s.hwnd)
                except Exception:
                    pass
        for pid, win in list(self._feature_wins.items()):
            try:
                win.destroy()
            except Exception:
                pass
        self._feature_wins.clear()
        if self._debug_win is not None:
            try:
                if hasattr(self._debug_win, "_on_close"):
                    self._debug_win._on_close()
                else:
                    self._debug_win.destroy()
            except Exception:
                try:
                    self._debug_win.destroy()
                except Exception:
                    pass
            self._debug_win = None
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        try:
            self.destroy()
        except Exception:
            pass


def _require_admin_or_exit() -> bool:
    """
    Block non-elevated start: inject needs admin OpenProcess / Global shm.

    Shows a dialog and offers to relaunch elevated. Returns True if already
    admin (caller continues). Returns False after exit/relaunch path.
    @author by ak
    """
    try:
        from app.core.diag_log import is_admin
    except Exception:
        return True
    if is_admin():
        return True

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    relaunch = messagebox.askyesno(
        "需要管理员权限",
        "本程序必须「以管理员身份运行」才能连接游戏。\n\n"
        "是否立即用管理员权限重新启动？\n"
        "（选「否」将退出）",
        parent=root,
    )
    if relaunch:
        try:
            import ctypes
            from pathlib import Path

            # Prefer packaged exe; fall back to python -m style is not used.
            if is_packaged():
                exe = str(Path(sys.executable).resolve())
                params = ""
            else:
                exe = sys.executable
                # Re-run entry script elevated.
                script = str(Path(__file__).resolve())
                params = f'"{script}"'
            rc = int(
                ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", exe, params or None, None, 1
                )
            )
            # ShellExecuteW > 32 means success.
            if rc <= 32:
                messagebox.showerror(
                    "XAJH 助手",
                    "提权启动失败。\n"
                    "请右键程序 →「以管理员身份运行」。",
                    parent=root,
                )
        except Exception as e:
            messagebox.showerror(
                "XAJH 助手",
                "提权启动失败。\n请右键程序 →「以管理员身份运行」。",
                parent=root,
            )
    root.destroy()
    return False


def main() -> None:
    """
    Entry: admin gate + single-instance shell + crash log bootstrap.

    Login「开启调试」(default on): full logs + captcha crops.
    Crash/FATAL always on disk regardless of debug checkbox.
    @author by ak
    """
    try:
        from app.core import diag_log

        # Always install hooks; crash writes even when debug is off.
        diag_log.install_excepthook()
    except Exception:
        pass

    # Inject requires elevation — stop before UI if not admin.
    if not _require_admin_or_exit():
        return

    # 游戏崩溃转储（WER LocalDumps）随启动自动配置；管理员下才生效。
    try:
        from app.core.crash_capture import ensure_game_crash_dumps

        ensure_game_crash_dumps()
    except Exception:
        pass

    scope = instance_scope()
    label = scope_label(scope)
    lock = try_acquire_single_instance()
    if lock is None:
        try:
            from app.core import diag_log

            # Hard case: still try error log (WARN not always-on; use ERROR).
            diag_log.error(
                f"single-instance: already running scope={scope}", tag="APP"
            )
        except Exception:
            pass
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            "XAJH 助手",
            f"「{label}」通道已在运行（同通道单实例）。\n"
            f"请从托盘或任务栏恢复。\n\n"
            f"说明：源码开发 / 测试包 / 正式包 可同时各开一个，"
            f"互不影响；仅同通道不能重复启动。",
        )
        root.destroy()
        return
    try:
        from app.core import diag_log

        diag_log.info(
            f"single-instance acquired scope={scope} label={label} "
            f"inject_controller={is_inject_controller()}",
            tag="APP",
        )
    except Exception:
        pass

    app = ShellApp()
    try:
        app.mainloop()
    except Exception:
        try:
            from app.core import diag_log

            try:
                from app.core.crash_capture import report_helper_crash
                import traceback as _tb

                report_helper_crash("mainloop", text=_tb.format_exc())
            except Exception:
                diag_log.exception("mainloop crashed", tag="HELPER_CRASH")
        except Exception:
            pass
        raise
    finally:
        try:
            from app.core import diag_log

            if diag_log.is_file_logging_enabled():
                diag_log.section("APP EXIT")
        except Exception:
            pass
        lock.release()


if __name__ == "__main__":
    main()
