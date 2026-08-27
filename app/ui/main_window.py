# -*- coding: utf-8 -*-
"""
Main visual workbench for XAJH client security testing.

破0: drag crosshair onto game window -> HWND/PID attach -> map + position recon.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import sys

ROOT = Path(__file__).resolve().parents[1]
try:
    from common.paths import PROJECT_ROOT as _PR

    PROJECT_ROOT = _PR
except Exception:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.automove import (
    PathTarget,
    host_move_to,
    read_scene_position,
    stop_automove_best_effort,
)
from app.core.task_api import (
    accept_task_routed,
    complete_task_routed,
    find_task_clue_targets,
    list_accepted_tasks,
    list_available_tasks,
    pathfind_to_clue,
    pathfind_task,
    resolve_task_patterns,
)
from app.core.chest_pathfind import (
    ACTION_AUTO_PATH_CENTER,
    ACTION_SKIP_LOCAL,
    DEFAULT_CLUSTER_RADIUS,
    DEFAULT_LOCAL_RANGE,
    plan_chest_path,
    run_chest_business,
)
from app.core.entity_scan import scan_nearby_entities
from app.core.packet_intercept import (
    InterceptState,
    format_annotated_record,
    format_record,
    packet_detail_text,
    read_recording,
    replay_records,
    replay_packet,
    write_recording,
)
from app.core.game_attach import (
    AttachResult,
    GameAttachSession,
    PosCandidate,
    attach_and_recon,
)
from app.core.map_names import format_map_display, format_scene_display
from app.core.map_fly import (
    fly_by_slot,
    fly_to_preset,
    find_fly_items,
    list_ui_transmit_points,
    open_transmit_flag,
    read_default_point_ids,
    read_death_point,
    research_coord_fly,
    research_death_coord_entry,
    research_record_then_fly_elsewhere,
    research_summary,
    probe_sign_then_ready_fly,
    send_transmit_packet,
    sign_current_to_slot,
    click_ui_trans_slot,
)
from app.core.plg_interact import (
    KIND_MARK,
    classify_matter_name,
    execute_target,
    kind_label,
    nearest_actionable_matter,
    scan_nearby_matters,
    suggest_action,
)
from app.core.combat_probe import (
    burst_sample_until_change,
    diff_samples,
    format_changed_dwords_detail,
    format_dword_table,
    format_dump_diff_note,
    format_sample_line,
    sample_combat_probe,
    write_lab_capture_report,
)
from app.core.game_sys_msg import dump_chat_messages
from app.core.skill_cast_probe import (
    CAST_CANCEL_BUSY_BIT,
    CAST_CANCEL_GATE_MINIMAL,
    burst_cast_this,
    clear_cast_session,
    clear_cast_session_when_active,
    diff_cast_probes,
    extract_cast_watch,
    resolve_cast_this,
    timeline_cast_this,
    write_cast_capture_report,
    write_cast_timeline_report,
    write_onskill_stopped_report,
)
from app.core.skill_recovery_lab import (
    ALL_METHODS,
    METHOD_LABELS,
    METHOD_ONSKILL,
    METHOD_PERFORM_STOP65,
    METHOD_ONSKILL_PERFORM_STOP65,
    METHOD_STOP65_ONSKILL_HOLD,
    RECOVERY_CANDIDATES,
    run_recovery_matrix,
    run_recovery_suppress_loop,
    run_hand_interrupt_probe,
    run_recovery_trial,
    METHOD_HOSTSTOP_LOCAL,
    METHOD_HOSTSTOP_BIT1,
    METHOD_SOFT_NEXT,
    METHOD_KEY_INTERRUPT,
    METHOD_HAND_PROBE,
    METHOD_CAST_INTERRUPT,
)
from app.core.skill_action_trace import (
    SCENARIO_BASELINE,
    SCENARIO_CHARGED_CHANNEL,
    SCENARIO_LABELS as SKILL_TRACE_SCENARIO_LABELS,
    SCENARIO_REAL_X,
    SCENARIO_SAME_SKILL,
    run_skill_action_trace,
)
from app.core.youfeng_chain import (
    INTERRUPT_FRESH_GATE,
    INTERRUPT_INTERNAL67,
    INTERRUPT_QINGGONG_PRECAST,
    INTERRUPT_QINGGONG_PULSE,
    YoufengChainRunner,
)
from app.core.reticle_probe import (
    capture_inputpoll_diag,
    capture_reticle_after_delay,
    capture_reticle_window,
    diff_reticle_samples,
    dump_reticle_regions,
    format_region_diffs,
    format_reticle_sample,
    key_diag_run,
    key_force_diag,
    sample_reticle_state,
    wait_reticle_on_shift,
)
from app.core.bg_input import (
    KeyHoldRunner,
    key_hold_probe,
    parse_vk_label,
)
from app.core.win_utils import (
    WindowInfo,
    get_cursor_pos,
    inspect_window_at_cursor,
    inspect_window_at_cursor_skip_self,
    is_self_window_info,
)


class DragPicker:
    """
    Drag-to-pick window handle.

    Starts on toolbar button press: tracks cursor across the desktop until
    mouse-left is released, then resolves HWND under cursor.
    Never commits this workbench's own windows (self PID).
    """

    def __init__(self, master: tk.Tk, on_pick):
        self.master = master
        self.on_pick = on_pick
        self._self_pid = os.getpid()
        self._last_info: WindowInfo | None = None  # last valid non-self target
        self._over_self = False
        self._tip = tk.Toplevel(master)
        self._tip.overrideredirect(True)
        self._tip.attributes("-topmost", True)
        self._tip.configure(bg="#0d1117")
        self._label = tk.Label(
            self._tip,
            text="拖到游戏窗口后松开（不可选本软件）",
            font=("Microsoft YaHei UI", 10),
            fg="#c9d1d9",
            bg="#0d1117",
            padx=10,
            pady=6,
            justify=tk.LEFT,
        )
        self._label.pack()
        self._poll = None
        # Started from toolbar ButtonPress: require seeing LBUTTON down at least
        # once before treating release as commit. Prevents after(10) race where
        # the click already released and picker immediately finishes empty.
        self._saw_press = False
        self._started = time.monotonic()
        self.master.configure(cursor="crosshair")
        self.master.bind_all("<Escape>", self._cancel, add="+")
        # Hide workbench while picking so cursor more easily hits the game.
        try:
            self.master.attributes("-alpha", 0.35)
        except tk.TclError:
            pass
        self._tick()

    def _tick(self):
        import ctypes

        # if left button released -> commit
        VK_LBUTTON = 0x01
        state = ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON)
        pressed = (state & 0x8000) != 0
        try:
            raw = inspect_window_at_cursor(prefer_root=True)
            if is_self_window_info(raw, self_pid=self._self_pid):
                self._over_self = True
                # keep previous valid target; do not overwrite with self
                if self._last_info:
                    info = self._last_info
                    exe = Path(info.exe_path).name if info.exe_path else ""
                    self._label.configure(
                        text=(
                            f"[忽略本软件] 仍保持上一个目标\n"
                            f"HWND=0x{info.hwnd:X}  PID={info.pid}\n"
                            f"{info.title or '(no title)'} | {exe}\n"
                            f"请拖到游戏窗口后松开 | ESC取消"
                        ),
                        fg="#f0a060",
                    )
                else:
                    self._label.configure(
                        text="当前是本软件窗口，已忽略\n请拖到游戏窗口（xajh）后松开 | ESC取消",
                        fg="#f0a060",
                    )
            else:
                self._over_self = False
                info = raw
                if info:
                    self._last_info = info
                    exe = Path(info.exe_path).name if info.exe_path else ""
                    self._label.configure(
                        text=(
                            f"HWND=0x{info.hwnd:X}  PID={info.pid}\n"
                            f"{info.title or '(no title)'} | {info.class_name}\n"
                            f"{exe}\n松开确认 | ESC取消"
                        ),
                        fg="#c9d1d9",
                    )
                else:
                    self._label.configure(
                        text="未命中窗口\n拖到游戏窗口后松开 | ESC取消",
                        fg="#c9d1d9",
                    )
            # follow cursor
            x, y = get_cursor_pos()
            self._tip.geometry(f"+{x + 18}+{y + 18}")
        except Exception as e:
            self._label.configure(text=f"读取失败: {e}", fg="#f85149")

        if pressed:
            self._saw_press = True
        if not pressed:
            # Grace: ignore the first ~120ms so ButtonPress->after(10) races
            # cannot commit an empty pick. Wait until we observed a real press.
            age = time.monotonic() - float(getattr(self, "_started", 0) or 0)
            if (not self._saw_press) and age < 0.45:
                self._poll = self.master.after(30, self._tick)
                return
            if not self._saw_press and self._last_info is None:
                # Never pressed / never hovered a target — cancel cleanly.
                self._finish(commit=False)
                return
            self._finish(commit=True)
            return
        self._poll = self.master.after(30, self._tick)

    def _cancel(self, _evt=None):
        self._finish(commit=False)

    def _restore_master(self):
        try:
            self.master.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
        try:
            self.master.configure(cursor="")
        except Exception:
            pass

    def _finish(self, commit: bool):
        if self._poll is not None:
            try:
                self.master.after_cancel(self._poll)
            except Exception:
                pass
            self._poll = None
        try:
            self.master.unbind_all("<Escape>")
        except Exception:
            pass
        self._restore_master()
        try:
            self._tip.destroy()
        except Exception:
            pass
        if not commit:
            return
        # Prefer last valid non-self target; never commit self.
        info = self._last_info
        if info is None:
            info = inspect_window_at_cursor_skip_self(
                prefer_root=True, self_pid=self._self_pid
            )
        if info is None or is_self_window_info(info, self_pid=self._self_pid):
            # silent reject — caller/UI already guided user
            return
        self.on_pick(info)


class WorkbenchApp(tk.Toplevel):
    """
    Legacy recon workbench (破0 / 寻路 / 交互 / 封包).

    Can run standalone (hidden Tk root) or as a child window under the shell.
    @author by ak
    """

    def __init__(self, master: tk.Misc | None = None):
        if master is None:
            self._standalone_root = tk.Tk()
            self._standalone_root.withdraw()
            super().__init__(self._standalone_root)
            self._standalone = True
        else:
            self._standalone_root = None
            self._standalone = False
            super().__init__(master)
        self.title("XAJH Workbench")
        self.geometry("780x520")
        self.minsize(700, 420)

        self.msg_q: queue.Queue = queue.Queue()
        self.session: GameAttachSession | None = None
        self.current_window: WindowInfo | None = None
        self.current_result: AttachResult | None = None
        self._poll_job = None
        # live host pos (plg GetCurrentScenePosition)
        self._pos_live_job = None
        self._pos_live_busy = False
        self._pos_live_interval_ms = 400
        # packet action capture state
        self._pkt_stop = threading.Event()  # stop action window only
        self._pkt_abort = threading.Event()  # abort whole capture (incl. baseline)
        self._pkt_busy = False
        self._pkt_phase = "idle"  # idle | baseline | action | finishing
        self._last_pkt_meta: dict | None = None
        self._last_pkt_analyze: dict | None = None
        self._pkt_analyze_busy = False
        # lab: combat probe A/B samples (knockback / cast research)
        self._lab_sample_a = None
        self._lab_sample_b = None
        self._lab_poll_job = None
        self._lab_poll_busy = False
        self._lab_cast_a = None
        self._lab_recovery_busy = False
        self._lab_skill_trace_busy = False
        self._lab_youfeng_chain_runner = None
        self._lab_youfeng_internal_runner = None
        self._lab_youfeng_gate_runner = None
        self._lab_youfeng_ultimate_runner = None
        self._lab_youfeng_ultimate_spam_runner = None
        self._lab_suppress_stop = None
        self._lab_suppress_thread = None
        self._lab_input_limit_original: dict[tuple[int, int], int] = {}
        self._lab_cast_b = None
        self._lab_ret_a = None
        self._lab_ret_b = None
        self._lab_ret_poll_job = None
        self._lab_ret_poll_busy = False
        self._lab_ret_auto_busy = False
        self._lab_ret_a_regs = {}
        self._lab_ret_b_regs = {}
        self._lab_ret_auto_stop = None
        self._lab_ret_poll_cap_busy = False
        self._yaolu_lab_busy = False
        self._yaolu_lab_stop = None
        # One-minute damage item consumption measurement (development panel).
        self._damage_stat_running = False
        self._damage_stat_stop = None
        # Passive target-scoped one-minute 木人桩 damage measurement.
        self._dummy_damage_running = False
        self._dummy_damage_stop = None
        # inject gate (Delete)
        self._unlocked = False
        self._bridge_ready = False
        self._inject_busy = False
        self._inject_started_at = 0.0
        self._break0_busy = False
        self._hotkey_job = None
        self._welcome = None
        self._game_list = None
        self._game_rows = []
        self._game_refresh_job = None
        self._last_inject_payload: dict = {}

        self._build_style()
        self._build_welcome()
        self.after(100, self._drain_queue)
        self.after(200, self._poll_delete_hotkey)
        self.after(300, self._refresh_game_client_list)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TButton", padding=(6, 2), font=("Microsoft YaHei UI", 9))
        style.configure("Header.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Welcome.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Mono.TLabel", font=("Consolas", 9))
        style.configure("TNotebook.Tab", padding=(8, 3), font=("Microsoft YaHei UI", 9))
        style.configure("TLabelframe", padding=4)
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 9))

    def _build_welcome(self) -> None:
        """
        Landing page until workbench lab inject succeeds.

        Lists live game clients so「解锁工作台」可指定 pid.
        @author by ak
        """
        self.geometry("860x620")
        self._welcome = ttk.Frame(self, padding=16)
        self._welcome.pack(fill=tk.BOTH, expand=True)
        ttk.Label(self._welcome, text="XAJH Workbench", style="Welcome.TLabel").pack(
            pady=(8, 6)
        )
        self.var_welcome = tk.StringVar(
            value=(
                "1. 启动并登录游戏\n"
                "2. 可在列表中选择目标后点「解锁工作台」注入实验页\n"
                "3. 或切到游戏窗口按 Delete 自动注入\n"
                "（解锁后可从工作台顶部进入正式功能窗）"
            )
        )
        ttk.Label(
            self._welcome,
            textvariable=self.var_welcome,
            style="Mono.TLabel",
            justify=tk.CENTER,
        ).pack(pady=4)
        self.var_welcome_status = tk.StringVar(value="等待注入解锁工作台 …")
        ttk.Label(
            self._welcome, textvariable=self.var_welcome_status, style="Header.TLabel"
        ).pack(pady=(8, 4))

        glist = ttk.LabelFrame(
            self._welcome,
            text="游戏实例（用于工作台实验注入）",
            padding=6,
        )
        glist.pack(fill=tk.BOTH, expand=False, pady=(4, 6))
        list_row = ttk.Frame(glist)
        list_row.pack(fill=tk.BOTH, expand=True)
        self._game_list = tk.Listbox(
            list_row,
            height=6,
            font=("Consolas", 9),
            activestyle="dotbox",
            exportselection=False,
        )
        self._game_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        gsb = ttk.Scrollbar(list_row, orient=tk.VERTICAL, command=self._game_list.yview)
        gsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._game_list.configure(yscrollcommand=gsb.set)

        brow = ttk.Frame(self._welcome)
        brow.pack(pady=8)
        ttk.Button(
            brow,
            text="刷新列表",
            command=lambda: self._refresh_game_client_list(force=True),
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(brow, text="解锁工作台", command=self._manual_inject).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(brow, text="退出", command=self._on_close).pack(side=tk.LEFT, padx=4)

        self.welcome_log = tk.Text(
            self._welcome,
            height=7,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg="#0d1117",
            fg="#c9d1d9",
            insertbackground="white",
        )
        self.welcome_log.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self._wlog(
            "欢迎页已就绪。选择游戏后先「解锁工作台」；"
            "正式面板入口位于解锁后的顶部工具栏。"
        )

    def _supports_formal_panel_entry(self) -> bool:
        """Only the unfrozen source shell exposes the workbench handoff."""
        if bool(getattr(self, "_standalone", False)):
            return False
        master = getattr(self, "master", None)
        return bool(
            master is not None
            and str(getattr(master, "_instance_scope", "") or "") == "source"
            and callable(
                getattr(master, "open_formal_panel_from_workbench", None)
            )
        )

    def _dispatch_formal_panel(self) -> bool:
        """Hand the current bridged game to the source shell without reinjecting."""
        if not self._supports_formal_panel_entry():
            self._wlog("进入正式面板: 仅源码命令行工作台可用")
            return False
        info = getattr(self, "current_window", None)
        pid = int(getattr(info, "pid", 0) or 0)
        hwnd = int(getattr(info, "hwnd", 0) or 0)
        if not pid or not bool(getattr(self, "_bridge_ready", False)):
            self._wlog("进入正式面板: 当前工作台尚未完成桥接")
            return False
        payload = dict(getattr(self, "_last_inject_payload", None) or {})
        opener = getattr(self.master, "open_formal_panel_from_workbench")
        ok = bool(
            opener(
                pid=pid,
                hwnd=hwnd,
                title=str(getattr(info, "title", "") or ""),
                bridge_note=str(payload.get("bridge_note") or "workbench"),
                ping_ret=payload.get("ping_ret"),
            )
        )
        self._wlog(
            f"进入正式面板: {'已打开' if ok else '未打开'} pid={pid}"
        )
        return ok

    def _open_formal_panel(self) -> None:
        """Open the formal panel for the workbench's current bridged game."""
        if not self._supports_formal_panel_entry():
            self._wlog("进入正式面板: 当前运行通道不支持")
            return
        if not bool(getattr(self, "_unlocked", False)) or not bool(
            getattr(self, "_bridge_ready", False)
        ):
            self._wlog("进入正式面板: 请先解锁工作台")
            return
        self._dispatch_formal_panel()

    def _wlog(self, msg: str, *, to_console: bool = True) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        # Welcome/inject path mirrors to console (skip when already printed by worker).
        if to_console:
            try:
                self._console_attach_log(str(msg), tag="INJECT")
            except Exception:
                try:
                    print(line, flush=True)
                except Exception:
                    pass
        try:
            cache = getattr(self, "_welcome_log_cache", None)
            if cache is None:
                self._welcome_log_cache = []
                cache = self._welcome_log_cache
            cache.append(line)
            if len(cache) > 300:
                del cache[: len(cache) - 300]
        except Exception:
            pass
        try:
            if hasattr(self, "welcome_log") and self.welcome_log.winfo_exists():
                self.welcome_log.insert(tk.END, line + "\n")
                self.welcome_log.see(tk.END)
        except Exception:
            pass
        try:
            if getattr(self, "_unlocked", False) and hasattr(self, "log_text"):
                self.log(msg)
        except Exception:
            pass

    def _list_game_clients(self) -> list[dict]:
        """Live xajh clients for workbench inject target selection. @author by ak"""
        out: list[dict] = []
        try:
            from app.core.inject_gate import find_main_hwnd_for_pid, find_xajh_processes

            for p in find_xajh_processes() or []:
                pid = int(p.get("pid") or 0)
                if not pid:
                    continue
                hwnd, title, _cls = find_main_hwnd_for_pid(pid)
                out.append(
                    {
                        "pid": pid,
                        "hwnd": int(hwnd or 0),
                        "title": title or f"xajh pid={pid}",
                        "create_time": float(p.get("create_time") or 0),
                    }
                )
        except Exception as e:
            self._wlog(f"枚举游戏进程失败: {e}")
        return out

    def _refresh_game_client_list(self, force: bool = False) -> None:
        """Refresh welcome game list while locked. @author by ak"""
        try:
            if self._game_refresh_job is not None:
                try:
                    self.after_cancel(self._game_refresh_job)
                except Exception:
                    pass
                self._game_refresh_job = None
        except Exception:
            pass

        lb = getattr(self, "_game_list", None)
        if lb is None:
            return
        try:
            if not lb.winfo_exists():
                return
        except Exception:
            return

        rows = self._list_game_clients()
        self._game_rows = rows
        try:
            sel = lb.curselection()
            sel_i = int(sel[0]) if sel else -1
        except Exception:
            sel_i = -1
        try:
            lb.delete(0, tk.END)
        except Exception:
            return
        if not rows:
            lb.insert(tk.END, "（未发现 xajh.exe）")
        else:
            for r in rows:
                pid = int(r.get("pid") or 0)
                hwnd = int(r.get("hwnd") or 0)
                title = str(r.get("title") or "")[:40]
                lb.insert(tk.END, f"pid={pid:<6}  hwnd=0x{hwnd:X}  {title}")
        if 0 <= sel_i < lb.size():
            try:
                lb.selection_set(sel_i)
                lb.see(sel_i)
            except Exception:
                pass
        if force:
            self._wlog(f"游戏列表已刷新 · {len(rows)} 个实例")

        if not getattr(self, "_unlocked", False):
            try:
                self._game_refresh_job = self.after(
                    2000, lambda: self._refresh_game_client_list(force=False)
                )
            except Exception:
                pass

    def _selected_game_row(self) -> dict | None:
        """Current list selection row. @author by ak"""
        lb = getattr(self, "_game_list", None)
        rows = list(getattr(self, "_game_rows", None) or [])
        if lb is None or not rows:
            return None
        try:
            sel = lb.curselection()
            if not sel:
                return None
            i = int(sel[0])
            if 0 <= i < len(rows):
                return rows[i]
        except Exception:
            return None
        return None

    def _poll_delete_hotkey(self) -> None:
        """Poll Delete key edge while locked. @author by ak"""
        try:
            from app.core.win_focus import DeleteKeyWatcher, is_xajh_foreground

            if not hasattr(self, "_del_watch") or self._del_watch is None:
                self._del_watch = DeleteKeyWatcher()
            if (
                (not self._unlocked)
                and self._del_watch.poll_press()
            ):
                ok, pid, hwnd = is_xajh_foreground()
                if ok and pid:
                    self._start_inject_flow(
                        source="Delete",
                        preferred_pid=int(pid),
                        preferred_hwnd=int(hwnd or 0) or None,
                    )
        except Exception:
            pass
        if not self._unlocked:
            self._hotkey_job = self.after(50, self._poll_delete_hotkey)

    def _manual_inject(self) -> None:
        """Unlock workbench lab pages (破0/寻路…). Does not open formal feature UI."""
        if self._unlocked:
            self._wlog("工作台已解锁，无需重复注入")
            return
        self._start_inject_flow(source="解锁工作台")

    def _inject_log(self, msg: str) -> None:
        """Thread-safe inject log (worker must not touch Tk widgets). @author by ak"""
        # Print immediately from worker thread (do not wait for UI drain).
        self._console_attach_log(msg, tag="INJECT")
        try:
            # to_console=False: avoid double print when UI drains this.
            self.msg_q.put(("__WLOG__", str(msg), False))
        except Exception:
            pass

    def _inject_phase(self, key: str, text: str) -> None:
        """Thread-safe welcome status phase updates. @author by ak"""
        try:
            self.msg_q.put(("__INJECT_PHASE__", str(key), str(text)))
        except Exception:
            pass

    def _start_inject_flow(
        self,
        source: str = "Delete",
        *,
        preferred_pid: int | None = None,
        preferred_hwnd: int | None = None,
    ) -> None:
        """Begin inject + unlock pipeline for workbench lab pages. @author by ak"""
        if self._unlocked:
            self._wlog(f"{source}: 工作台已解锁，跳过")
            return
        # Stuck busy from a hung previous attempt: auto-clear after 95s.
        if self._inject_busy:
            started = float(getattr(self, "_inject_started_at", 0) or 0)
            age = (time.monotonic() - started) if started > 0 else 9999.0
            if age < 95.0:
                self._wlog(
                    f"{source}: 注入仍在进行中（已 {age:.0f}s），请稍候；"
                    f"超过 95s 可再点一次强制重试"
                )
                try:
                    self.var_welcome_status.set(
                        f"{source}: 注入进行中… {age:.0f}s"
                    )
                except Exception:
                    pass
                return
            self._wlog(f"{source}: 上次注入超时未回执，清除 busy 后重试")
            self._inject_busy = False
        self._inject_busy = True
        self._inject_started_at = time.monotonic()
        if not preferred_pid:
            row = self._selected_game_row()
            if row is not None:
                preferred_pid = int(row.get("pid") or 0) or None
                preferred_hwnd = int(row.get("hwnd") or 0) or None
        if not preferred_pid:
            # Fall back to first listed live client so button always does work.
            try:
                rows = list(getattr(self, "_game_rows", None) or [])
                if rows:
                    preferred_pid = int(rows[0].get("pid") or 0) or None
                    preferred_hwnd = int(rows[0].get("hwnd") or 0) or None
                    self._wlog(
                        f"{source}: 未选手动行，默认列表首个 "
                        f"pid={preferred_pid}"
                    )
            except Exception:
                pass
        self.var_welcome_status.set(f"{source}: 正在注入…")
        if preferred_pid:
            self._wlog(
                f"{source} 触发注入 pid={int(preferred_pid)} "
                f"hwnd=0x{int(preferred_hwnd or 0):X}"
            )
        else:
            self._wlog(f"{source} 触发注入（自动选择运行中的 xajh）")

        def on_done(out):
            self.msg_q.put(
                ("__INJECT__", out.to_dict() if hasattr(out, "to_dict") else out)
            )

        from app.core.inject_gate import run_delete_inject_async

        run_delete_inject_async(
            on_done,
            preferred_pid=int(preferred_pid) if preferred_pid else None,
            preferred_hwnd=int(preferred_hwnd) if preferred_hwnd else None,
            # NEVER pass self._wlog: inject runs off the UI thread.
            log=self._inject_log,
            on_phase=self._inject_phase,
        )

    def _apply_inject(self, d: dict) -> None:
        """Handle inject result on UI thread. @author by ak"""
        self._inject_busy = False
        self._inject_started_at = 0.0
        if not d or not d.get("ok"):
            err = (d or {}).get("error") or "未知错误"
            code = str((d or {}).get("fail_code") or "")
            self.var_welcome_status.set(
                f"注入失败: {err}" + (f" [{code}]" if code else "")
            )
            self._wlog(f"注入失败: {err}" + (f" [{code}]" if code else ""))
            return
        pid = int(d.get("pid") or 0)
        hwnd = int(d.get("hwnd") or 0)
        title = str(d.get("title") or "")
        reused = bool(d.get("reused"))
        did_inject = bool(d.get("did_inject"))
        self._last_inject_payload = dict(d)
        self._bridge_ready = True
        mode = "复用已有桥接" if reused and not did_inject else "新注入桥接"
        self.var_welcome_status.set(f"{mode} pid={pid}，正在附加…")
        self._wlog(
            f"注入成功({mode}) pid={pid} hwnd=0x{hwnd:X} ping={d.get('ping_ret')} "
            f"note={d.get('bridge_note')!r} reused={reused} did_inject={did_inject}"
        )
        self.current_window = WindowInfo(
            hwnd=hwnd or 1,
            pid=pid,
            tid=0,
            title=title or "xajh",
            class_name="XAJH",
            rect=(0, 0, 0, 0),
            exe_path="",
        )
        try:
            self._unlock_workbench()
        except Exception as e:
            import traceback

            self._attach_log(f"解锁工作台 UI 构建异常: {e}", tag="UI")
            traceback.print_exc()
            self._unlocked = True
        try:
            if hasattr(self, "var_pid"):
                self.var_pid.set(str(pid) if pid else "-")
            if hasattr(self, "var_hwnd"):
                self.var_hwnd.set(f"0x{hwnd:08X}" if hwnd else "-")
            if hasattr(self, "var_title"):
                self.var_title.set(self._short_text(title or f"xajh pid={pid}", 48))
            if hasattr(self, "var_map"):
                self.var_map.set("附加中…")
            if hasattr(self, "var_pos"):
                self.var_pos.set("附加中…")
            if hasattr(self, "var_scene"):
                self.var_scene.set("…")
            if hasattr(self, "var_role"):
                self.var_role.set("…")
        except Exception:
            pass
        self._attach_log(
            f"桥接就绪({mode}) pid={pid} hwnd=0x{hwnd:X}。开始破0附加…",
            tag="INJECT",
        )
        # Bridge already ensured by inject_gate; only attach/recon here.
        try:
            self._run_break0(ensure_bridge=False, source="解锁工作台")
        except Exception as e:
            import traceback

            self._attach_log(f"解锁后破0启动失败: {e}", tag="ATTACH")
            traceback.print_exc()
            self._break0_busy = False

    def _unlock_workbench(self) -> None:
        """Destroy welcome and build full UI once. @author by ak"""
        if self._unlocked:
            return
        self._unlocked = True
        try:
            if self._game_refresh_job is not None:
                self.after_cancel(self._game_refresh_job)
        except Exception:
            pass
        self._game_refresh_job = None
        self._game_list = None
        if self._hotkey_job is not None:
            try:
                self.after_cancel(self._hotkey_job)
            except Exception:
                pass
            self._hotkey_job = None

        # Snapshot welcome inject logs BEFORE destroying the welcome page.
        # Prefer cache (survives widget destroy races) and merge widget text.
        welcome_lines: list[str] = []
        seen: set[str] = set()
        try:
            for ln in list(getattr(self, "_welcome_log_cache", []) or []):
                t = str(ln or "").strip()
                if t and t not in seen:
                    seen.add(t)
                    welcome_lines.append(str(ln))
        except Exception:
            pass
        try:
            if hasattr(self, "welcome_log") and self.welcome_log.winfo_exists():
                raw = self.welcome_log.get("1.0", tk.END)
                for ln in str(raw or "").splitlines():
                    t = ln.strip()
                    if t and t not in seen:
                        seen.add(t)
                        welcome_lines.append(ln)
        except Exception:
            pass

        if self._welcome is not None:
            try:
                self._welcome.destroy()
            except Exception:
                pass
            self._welcome = None
        self.geometry("980x640")
        self.minsize(860, 540)
        self._build_ui()
        # Restore inject trace into 日志 tab so user can still read it.
        try:
            if hasattr(self, "log_text"):
                for ln in welcome_lines[-120:]:
                    self.log_text.insert(tk.END, ln + "\n")
                self.log_text.see(tk.END)
        except Exception:
            pass
        self.log("功能页已解锁。")
        self.log(
            "注入完成：破0 / 寻路 / 交互 / 封包 可用。"
            "欢迎页注入日志已复制到本页（若有）。"
        )
        self.status.set("BRIDGED")

    def _build_ui(self):
        """
        Compact workbench: toolbar + slim process strip + notebook tabs.
        Tabs: 破0 | 寻路 | 交互 | 任务 | 封包 | 实验 | 妖楼 | 聊天 | 日志.
        @author by ak
        """
        root = ttk.Frame(self, padding=8)
        root.pack(fill=tk.BOTH, expand=True)

        # ---------- top toolbar ----------
        bar = ttk.Frame(root)
        bar.pack(fill=tk.X, pady=(0, 6))

        self.drag_btn = tk.Button(
            bar,
            text="✚ 取句柄",
            width=9,
            bg="#1f6feb",
            fg="white",
            activebackground="#388bfd",
            relief=tk.RAISED,
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="crosshair",
            padx=4,
            pady=1,
        )
        self.drag_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.drag_btn.bind("<ButtonPress-1>", self._start_drag_pick)

        ttk.Button(bar, text="刷新坐标", command=self._refresh_pos).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="导出", command=self._export_result).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="断开", command=self._detach).pack(side=tk.LEFT, padx=2)
        if self._supports_formal_panel_entry():
            ttk.Button(
                bar,
                text="进入正式面板",
                command=self._open_formal_panel,
            ).pack(side=tk.LEFT, padx=(8, 2))

        # ---------- process info (compact) ----------
        info = ttk.LabelFrame(root, text="进程", padding=4)
        info.pack(fill=tk.X, pady=(0, 6))

        self.var_hwnd = tk.StringVar(value="-")
        self.var_pid = tk.StringVar(value="-")
        self.var_title = tk.StringVar(value="-")
        self.var_class = tk.StringVar(value="-")
        self.var_exe = tk.StringVar(value="-")
        self.var_map = tk.StringVar(value="-")
        self.var_pos = tk.StringVar(value="-")
        self.var_role = tk.StringVar(value="-")
        self.var_scene = tk.StringVar(value="-")
        # Full strings for tooltip (title/path often too long for the strip).
        self._info_full: dict[str, str] = {"title": "", "exe": "", "class": ""}
        # Live plg coordinates always on while attached (no user toggle).
        self.pos_live = tk.BooleanVar(value=True)

        # Two rows:
        #   1) title | path (ellipsis + hover full text)
        #   2) short live fields
        # @author by ak
        row1 = ttk.Frame(info)
        row1.pack(fill=tk.X)
        self._info_labels: dict[str, ttk.Label] = {}
        for col, lab, var, tip_key, weight in (
            (0, "标题", self.var_title, "title", 3),
            (1, "路径", self.var_exe, "exe", 2),
        ):
            cell = ttk.Frame(row1)
            cell.grid(row=0, column=col, sticky="ew", padx=3, pady=0)
            ttk.Label(cell, text=lab + ":", width=5).pack(side=tk.LEFT)
            lbl = ttk.Label(cell, textvariable=var, style="Mono.TLabel", anchor="w")
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._info_labels[lab] = lbl
            self._bind_info_tooltip(lbl, tip_key)
            row1.columnconfigure(col, weight=weight)

        grid = ttk.Frame(info)
        grid.pack(fill=tk.X, pady=(3, 0))
        rows = [
            ("HWND", self.var_hwnd, 1),
            ("PID", self.var_pid, 1),
            ("scene", self.var_scene, 1),
            ("角色", self.var_role, 2),
            ("地图", self.var_map, 2),
            ("坐标", self.var_pos, 2),
        ]
        for i, (lab, var, weight) in enumerate(rows):
            cell = ttk.Frame(grid)
            cell.grid(row=0, column=i, sticky="ew", padx=3, pady=0)
            w = 5 if lab != "scene" else 5
            ttk.Label(cell, text=lab + ":", width=w).pack(side=tk.LEFT)
            lbl = ttk.Label(cell, textvariable=var, style="Mono.TLabel", anchor="w")
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._info_labels[lab] = lbl
            grid.columnconfigure(i, weight=weight)

        # ---------- main notebook ----------
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        tab_break0 = ttk.Frame(self.notebook, padding=4)
        tab_path = ttk.Frame(self.notebook, padding=4)
        tab_interact = ttk.Frame(self.notebook, padding=4)
        tab_task = ttk.Frame(self.notebook, padding=4)
        tab_pkt = ttk.Frame(self.notebook, padding=4)
        tab_lab = ttk.Frame(self.notebook, padding=4)
        tab_yaolu = ttk.Frame(self.notebook, padding=4)
        tab_chat = ttk.Frame(self.notebook, padding=4)
        tab_log = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(tab_break0, text="破0")
        self.notebook.add(tab_path, text="寻路")
        self.notebook.add(tab_interact, text="交互")
        self.notebook.add(tab_task, text="任务")
        self.notebook.add(tab_pkt, text="封包")
        self.notebook.add(tab_lab, text="实验")
        self.notebook.add(tab_yaolu, text="妖楼")
        self.notebook.add(tab_chat, text="聊天")
        self.notebook.add(tab_log, text="日志")

        # Status early so 取句柄/破0 never AttributeError if a later tab fails.
        self.status = tk.StringVar(value="IDLE")

        def _safe_tab(name: str, builder) -> None:
            try:
                builder()
            except Exception as e:
                import traceback

                err = f"构建页失败 [{name}]: {e}"
                print(f"[UI] {err}", flush=True)
                traceback.print_exc()
                try:
                    self._console_attach_log(err, tag="UI")
                except Exception:
                    pass
                tab_map = {
                    "破0": tab_break0,
                    "寻路": tab_path,
                    "交互": tab_interact,
                    "任务": tab_task,
                    "封包": tab_pkt,
                    "实验": tab_lab,
                    "妖楼": tab_yaolu,
                    "聊天": tab_chat,
                    "日志": tab_log,
                }
                try:
                    ttk.Label(
                        tab_map.get(name, tab_log),
                        text=err,
                        wraplength=800,
                        foreground="#b00020",
                    ).pack(anchor="w", padx=8, pady=8)
                except Exception:
                    pass

        _safe_tab("破0", lambda: self._build_tab_break0(tab_break0))
        _safe_tab("寻路", lambda: self._build_tab_path(tab_path))
        _safe_tab("交互", lambda: self._build_tab_interact(tab_interact))
        _safe_tab("任务", lambda: self._build_tab_task(tab_task))
        _safe_tab("封包", lambda: self._build_tab_packet(tab_pkt))
        _safe_tab("实验", lambda: self._build_tab_lab(tab_lab))
        _safe_tab("妖楼", lambda: self._build_tab_yaolu(tab_yaolu))
        _safe_tab("聊天", lambda: self._build_tab_chat(tab_chat))
        _safe_tab("日志", lambda: self._build_tab_log(tab_log))

        ttk.Label(root, textvariable=self.status, style="Mono.TLabel").pack(anchor="w", pady=(2, 0))

    def _build_tab_chat(self, parent: ttk.Frame) -> None:
        """
        Developer chat dump: multi-path live messages + channel labels.

        hist always + optional heap/dlg so new feedback lines still show up.

        @author by ak
        """
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(bar, text="聊天消息", style="Header.TLabel").pack(side=tk.LEFT)
        self.var_chat_heap = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="含堆扫", variable=self.var_chat_heap).pack(
            side=tk.LEFT, padx=(10, 0)
        )
        self.var_chat_general = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="扫描普通消息", variable=self.var_chat_general).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Label(bar, text="预算(s)").pack(side=tk.LEFT, padx=(10, 2))
        self.var_chat_budget = tk.StringVar(value="2.5")
        ttk.Entry(bar, textvariable=self.var_chat_budget, width=6).pack(side=tk.LEFT)
        ttk.Label(bar, text="上限").pack(side=tk.LEFT, padx=(8, 2))
        self.var_chat_max = tk.StringVar(value="200")
        ttk.Entry(bar, textvariable=self.var_chat_max, width=6).pack(side=tk.LEFT)
        ttk.Button(bar, text="刷新", command=self._chat_refresh).pack(
            side=tk.LEFT, padx=(10, 2)
        )
        ttk.Button(bar, text="反馈指纹", command=self._chat_feedback_fingerprint).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(bar, text="对比上次", command=self._chat_feedback_diff).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(bar, text="导出", command=self._chat_export).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="清空", command=self._chat_clear).pack(side=tk.LEFT, padx=2)
        self.var_chat_status = tk.StringVar(value="未刷新")
        ttk.Label(bar, textvariable=self.var_chat_status, style="Mono.TLabel").pack(
            side=tk.RIGHT
        )

        tip = ttk.Label(
            parent,
            text=(
                "内存直读左下角聊天缓冲（高堆 rich hist）。"
                "默认勾选「扫描普通消息」= 完整频道；关掉=只显示答案/神罚等完整反馈句。"
                "「含堆扫」仅在 hist 缺完整反馈句时用高堆 hitmap 补全，不会扫碎片。"
            ),
            wraplength=980,
            justify="left",
        )
        tip.pack(anchor="w", pady=(0, 4))

        filt = ttk.Frame(parent)
        filt.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(filt, text="类别过滤").pack(side=tk.LEFT)
        self.var_chat_filter = tk.StringVar(value="全部")
        self.cmb_chat_filter = ttk.Combobox(
            filt,
            textvariable=self.var_chat_filter,
            values=["全部", "系统", "其他", "世界", "附近", "队伍", "帮派", "私聊", "未知"],
            width=10,
            state="readonly",
        )
        self.cmb_chat_filter.pack(side=tk.LEFT, padx=(4, 8))
        self.cmb_chat_filter.bind("<<ComboboxSelected>>", lambda _e: self._chat_apply_filter())
        ttk.Label(filt, text="关键词").pack(side=tk.LEFT)
        self.var_chat_keyword = tk.StringVar(value="")
        ent_kw = ttk.Entry(filt, textvariable=self.var_chat_keyword, width=24)
        ent_kw.pack(side=tk.LEFT, padx=(4, 4))
        ent_kw.bind("<Return>", lambda _e: self._chat_apply_filter())
        ttk.Button(filt, text="筛选", command=self._chat_apply_filter).pack(side=tk.LEFT)

        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        cols = ("channel", "cid", "text", "seq", "uid", "source", "addr")
        self.tree_chat = ttk.Treeview(
            tree_frame,
            columns=cols,
            show="headings",
            selectmode="browse",
        )
        self.tree_chat.heading("channel", text="类别")
        self.tree_chat.heading("cid", text="ID")
        self.tree_chat.heading("text", text="内容")
        self.tree_chat.heading("seq", text="序")
        self.tree_chat.heading("uid", text="UID(mem)")
        self.tree_chat.heading("source", text="来源")
        self.tree_chat.heading("addr", text="地址")
        self.tree_chat.column("channel", width=54, anchor="center", stretch=False)
        self.tree_chat.column("cid", width=40, anchor="center", stretch=False)
        self.tree_chat.column("text", width=360, anchor="w", stretch=True)
        self.tree_chat.column("seq", width=40, anchor="center", stretch=False)
        self.tree_chat.column("uid", width=150, anchor="w", stretch=False)
        self.tree_chat.column("source", width=110, anchor="w", stretch=False)
        self.tree_chat.column("addr", width=90, anchor="center", stretch=False)
        ysb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree_chat.yview)
        xsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.tree_chat.xview)
        self.tree_chat.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.tree_chat.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self._chat_rows: list[dict] = []
        self._chat_busy = False
        self._chat_fp_last: dict | None = None
        self._chat_fp_prev: dict | None = None

    def _chat_clear(self) -> None:
        """Clear chat dump table. @author by ak"""
        self._chat_rows = []
        if hasattr(self, "tree_chat"):
            for iid in self.tree_chat.get_children():
                self.tree_chat.delete(iid)
        if hasattr(self, "var_chat_status"):
            self.var_chat_status.set("已清空")

    def _chat_apply_filter(self) -> None:
        """Re-render chat tree from cached rows with filter. @author by ak"""
        if not hasattr(self, "tree_chat"):
            return
        for iid in self.tree_chat.get_children():
            self.tree_chat.delete(iid)
        ch_f = (self.var_chat_filter.get() or "全部").strip()
        kw = (self.var_chat_keyword.get() or "").strip().lower()
        shown = 0
        for row in list(getattr(self, "_chat_rows", None) or []):
            ch = str(row.get("channel") or "未知")
            text = str(row.get("text") or "")
            if ch_f and ch_f != "全部" and ch != ch_f:
                continue
            if kw and kw not in text.lower() and kw not in str(row.get("source") or "").lower():
                continue
            addr = int(row.get("addr") or 0)
            addr_s = f"0x{addr:X}" if addr else "-"
            seq = row.get("seq", -1)
            try:
                seq_s = str(int(seq)) if int(seq) >= 0 else "-"
            except Exception:
                seq_s = "-"
            cid = row.get("channel_id", -1)
            try:
                cid_i = int(cid)
                cid_s = str(cid_i) if cid_i >= 0 else "-"
            except Exception:
                cid_s = "-"
            uid = str(row.get("uid") or row.get("mem_key") or row.get("content_key") or "-")
            self.tree_chat.insert(
                "",
                tk.END,
                values=(ch, cid_s, text, seq_s, uid, str(row.get("source") or ""), addr_s),
            )
            shown += 1
        total = len(getattr(self, "_chat_rows", None) or [])
        if hasattr(self, "var_chat_status"):
            base = getattr(self, "_chat_status_base", "") or ""
            self.var_chat_status.set(
                f"{base} | 显示 {shown}/{total}" if base else f"显示 {shown}/{total}"
            )

    def _chat_refresh(self) -> None:
        """Refresh chat dump on background thread. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        if getattr(self, "_chat_busy", False):
            self.log("聊天: 刷新进行中，请稍候")
            return
        try:
            budget = float((self.var_chat_budget.get() or "2.5").strip() or "2.5")
        except Exception:
            budget = 2.5
        try:
            max_lines = int(float((self.var_chat_max.get() or "200").strip() or "200"))
        except Exception:
            max_lines = 200
        include_heap = bool(self.var_chat_heap.get())
        include_general = True
        if hasattr(self, "var_chat_general"):
            include_general = bool(self.var_chat_general.get())
        budget = max(0.3, min(8.0, budget))
        max_lines = max(10, min(1000, max_lines))
        # Feedback-only still needs enough budget for high-heap hist.
        if not include_general:
            budget = min(max(budget, 1.0), 2.5)
            max_lines = min(max_lines, 120)
        self._chat_busy = True
        self.var_chat_status.set("刷新中…")
        self.status.set("CHAT_DUMP")
        self.log(
            f"聊天: 开始刷新 heap={'on' if include_heap else 'off'} "
            f"general={'on' if include_general else 'off'} "
            f"budget={budget:.1f}s max={max_lines}"
        )

        def work() -> None:
            err = ""
            rows: list[dict] = []
            t0 = time.monotonic()
            try:
                rows = dump_chat_messages(
                    sess,
                    budget_s=budget,
                    max_lines=max_lines,
                    include_heap=include_heap,
                    include_general=include_general,
                    log=self.log,
                )
            except Exception as e:
                err = str(e)
            elapsed = time.monotonic() - t0

            def done() -> None:
                self._chat_busy = False
                if err:
                    self._chat_rows = []
                    self._chat_apply_filter()
                    self.var_chat_status.set(f"失败: {err}")
                    self.log(f"聊天: 刷新失败 {err}")
                    self.status.set("ERROR")
                    return
                self._chat_rows = list(rows or [])
                # channel summary + exact multi-instance counts
                counts: dict[str, int] = {}
                text_counts: dict[str, int] = {}
                for r in self._chat_rows:
                    c = str(r.get("channel") or "未知")
                    counts[c] = counts.get(c, 0) + 1
                    t = str(r.get("text") or "")
                    if t:
                        text_counts[t] = text_counts.get(t, 0) + 1
                summary = ",".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "-"
                multi = {
                    k: v
                    for k, v in text_counts.items()
                    if v > 1
                    and any(
                        x in k
                        for x in ("答案", "神罚", "捕羽", "不稳定", "进入活动", "重新来过")
                    )
                }
                multi_s = (
                    ",".join(f"{k[:8]}×{v}" for k, v in list(multi.items())[:4])
                    if multi
                    else "-"
                )
                n_hist = sum(
                    1
                    for r in self._chat_rows
                    if str(r.get("source") or "").startswith("hist:")
                )
                n_heap = sum(
                    1
                    for r in self._chat_rows
                    if str(r.get("source") or "").startswith("heap:")
                    or str(r.get("source") or "").startswith("hitmap:")
                )
                n_high = sum(
                    1 for r in self._chat_rows if int(r.get("addr") or 0) >= 0x70000000
                )
                self._chat_status_base = (
                    f"lines={len(self._chat_rows)} hist={n_hist} heap={n_heap} "
                    f"high={n_high} {summary} multi={multi_s} {elapsed:.2f}s"
                )
                self._chat_apply_filter()
                self.log(
                    f"聊天: 刷新完成 lines={len(self._chat_rows)} "
                    f"hist={n_hist} heap={n_heap} high={n_high} "
                    f"channels={{{summary}}} multi={{{multi_s}}} "
                    f"elapsed={elapsed:.2f}s"
                )
                # Preview a few lines into main log for quick eyeballing.
                for r in self._chat_rows[:12]:
                    addr = int(r.get("addr") or 0)
                    addr_s = f"0x{addr:X}" if addr else "-"
                    self.log(
                        f"  [{r.get('channel')}] {r.get('text')}  "
                        f"via={r.get('source')} addr={addr_s}"
                    )
                if len(self._chat_rows) > 12:
                    self.log(f"  ... 另有 {len(self._chat_rows) - 12} 条，见表格")
                self.status.set("ATTACHED")

            try:
                self.after(0, done)
            except Exception:
                done()

        threading.Thread(target=work, name="chat-dump", daemon=True).start()

    def _chat_feedback_fingerprint(self) -> None:
        """
        Snapshot feedback hit map (same source as yaolu hist baseline/delta).

        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        if getattr(self, "_chat_busy", False):
            self.log("聊天: 忙碌中，稍后再取指纹")
            return
        try:
            budget = float((self.var_chat_budget.get() or "1.0").strip() or "1.0")
        except Exception:
            budget = 1.0
        budget = max(0.3, min(3.0, budget))
        self._chat_busy = True
        self.var_chat_status.set("取反馈指纹…")
        self.log(f"聊天: 反馈指纹扫描 budget={budget:.2f}s")

        def work() -> None:
            err = ""
            fp: dict = {}
            t0 = time.monotonic()
            try:
                from app.core.chat_history import snapshot_feedback_fingerprint

                fp = snapshot_feedback_fingerprint(
                    sess, budget_s=budget, log=None
                )
            except Exception as e:
                err = str(e)
            elapsed = time.monotonic() - t0

            def done() -> None:
                self._chat_busy = False
                if err:
                    self.var_chat_status.set(f"指纹失败: {err}")
                    self.log(f"聊天: 反馈指纹失败 {err}")
                    return
                prev = getattr(self, "_chat_fp_last", None)
                self._chat_fp_prev = prev
                self._chat_fp_last = dict(fp or {})
                counts = (fp or {}).get("counts") or {}
                inst = (fp or {}).get("instances") or []
                self._chat_status_base = (
                    f"fp counts={counts} inst={len(inst)} "
                    f"hash={fp.get('sig_hash')} {elapsed:.2f}s"
                )
                self.var_chat_status.set(self._chat_status_base)
                self.log(
                    f"聊天: 反馈指纹 counts={counts} inst={len(inst)} "
                    f"hash={fp.get('sig_hash')} elapsed={elapsed:.2f}s"
                )
                for it in list(inst)[:12]:
                    try:
                        t, a = it[0], int(it[1])
                        c = it[2] if len(it) >= 3 else ""
                        self.log(f"  fp [{t}] addr=0x{a:X} ctx={c}")
                    except Exception:
                        self.log(f"  fp {it!r}")
                if len(inst) > 12:
                    self.log(f"  ... 另有 {len(inst) - 12} 个实例")
                if prev:
                    self.log("聊天: 已保存为 last；可点「对比上次」看 delta")

            try:
                self.after(0, done)
            except Exception:
                done()

        threading.Thread(target=work, name="chat-fp", daemon=True).start()

    def _chat_feedback_diff(self) -> None:
        """
        Diff last two feedback fingerprints (dev mirror of hist_delta).

        @author by ak
        """
        before = getattr(self, "_chat_fp_prev", None)
        after = getattr(self, "_chat_fp_last", None)
        if not isinstance(after, dict):
            self.log("聊天: 无反馈指纹，请先点「反馈指纹」")
            messagebox.showinfo("提示", "请先点两次「反馈指纹」（确认前/后各一次）")
            return
        if not isinstance(before, dict):
            self.log("聊天: 只有一份指纹，请再取一次后对比")
            messagebox.showinfo("提示", "需要两份指纹：确认前一次 + 确认后再一次")
            return
        try:
            from app.core.chat_history import (
                diff_feedback_count_new,
                diff_feedback_new_instances,
            )

            delta = diff_feedback_count_new(before, after)
            new_inst = diff_feedback_new_instances(before, after)
        except Exception as e:
            self.log(f"聊天: 对比失败 {e}")
            return
        self.log(
            f"聊天: 反馈对比 count_delta={delta or {}} "
            f"new_inst={len(new_inst)} "
            f"hash {before.get('sig_hash')} -> {after.get('sig_hash')}"
        )
        for t, a in new_inst[:16]:
            self.log(f"  +NEW [{t}] addr=0x{int(a):X}")
        if not delta and not new_inst:
            self.log("聊天: 对比结果为空（无 count 增长 / 无新实例）— 业务也会判 none")
        else:
            texts = [t for t, _ in new_inst]
            for k, n in (delta or {}).items():
                if int(n) > 0 and k not in texts:
                    texts.append(str(k))
            got_ok = any("答案正确" in t for t in texts)
            got_fail = any("答案错误" in t or "重新来过" in t for t in texts)
            got_hard = any("神罚" in t or "捕羽" in t for t in texts)
            self.log(
                f"聊天: 对比判定 ok={int(got_ok)} fail={int(got_fail)} "
                f"hard={int(got_hard)} texts={texts[:6]}"
            )
        if hasattr(self, "var_chat_status"):
            self.var_chat_status.set(
                f"delta counts={delta or {}} new_inst={len(new_inst)}"
            )

    def _chat_export(self) -> None:
        """Export current chat dump rows to json. @author by ak"""
        rows = list(getattr(self, "_chat_rows", None) or [])
        if not rows:
            messagebox.showinfo("提示", "没有可导出的聊天消息，请先刷新")
            return
        out_dir = PROJECT_ROOT / ".issues" / "chat_dump"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"chat_{time.strftime('%Y%m%d_%H%M%S')}.json"
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(rows),
            "rows": rows,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.log(f"聊天: 已导出 {len(rows)} 条 -> {path}")
        if hasattr(self, "var_chat_status"):
            self.var_chat_status.set(f"已导出 {path.name}")
        messagebox.showinfo("导出完成", f"已导出 {len(rows)} 条\n{path}")

    def _build_tab_log(self, parent: ttk.Frame) -> None:
        """
        Dedicated log tab — full height for long recon / packet traces.
        @author by ak
        """
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(bar, text="运行日志", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Button(bar, text="清空", command=self._clear_log).pack(side=tk.RIGHT)

        log_frame = ttk.Frame(parent)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(
            log_frame,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg="#0d1117",
            fg="#c9d1d9",
            insertbackground="white",
        )
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_tab_break0(self, parent: ttk.Frame) -> None:
        """Break0 recon + entity scan. @author by ak"""
        mid = ttk.Panedwindow(parent, orient=tk.HORIZONTAL)
        mid.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(mid)
        right = ttk.Frame(mid)
        mid.add(left, weight=3)
        mid.add(right, weight=2)

        ttk.Label(left, text="候选 / 实体", style="Header.TLabel").pack(anchor="w")
        self.tree = ttk.Treeview(
            left,
            columns=("kind", "value", "addr", "score"),
            show="headings",
            height=12,
        )
        self.tree.heading("kind", text="类型")
        self.tree.heading("value", text="值")
        self.tree.heading("addr", text="地址")
        self.tree.heading("score", text="分")
        self.tree.column("kind", width=50, anchor="center")
        self.tree.column("value", width=260)
        self.tree.column("addr", width=90, anchor="center")
        self.tree.column("score", width=40, anchor="center")
        sy = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=2)
        sy.pack(side=tk.RIGHT, fill=tk.Y, pady=2)

        tools = ttk.LabelFrame(right, text="工具", padding=6)
        tools.pack(fill=tk.BOTH, expand=True)
        ttk.Button(tools, text="复制坐标", command=self._copy_pos).pack(fill=tk.X, pady=2)
        ttk.Button(tools, text="验证位移", command=self._diff_lock_pos).pack(fill=tk.X, pady=2)
        ttk.Button(tools, text="区服配置", command=self._read_server_cfg).pack(fill=tk.X, pady=2)
        ttk.Separator(tools).pack(fill=tk.X, pady=6)
        ttk.Label(tools, text="周围实体").pack(anchor="w")
        ent_box = ttk.Frame(tools)
        ent_box.pack(fill=tk.X, pady=2)
        self.entity_mode = tk.StringVar(value="查看周围怪物")
        self.entity_combo = ttk.Combobox(
            ent_box,
            textvariable=self.entity_mode,
            values=("查看周围怪物", "查看周围物品", "查看周围NPC"),
            state="readonly",
            width=14,
            font=("Microsoft YaHei UI", 9),
        )
        self.entity_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(ent_box, text="扫描", command=self._scan_nearby).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(
            tools,
            text="取句柄(自动破0) → 坐标自动刷 → 扫描",
            style="Mono.TLabel",
            wraplength=220,
        ).pack(anchor="w", pady=(8, 0))

    def _build_tab_path(self, parent: ttk.Frame) -> None:
        """AutoMove / pathfinding tab. @author by ak"""
        path = ttk.LabelFrame(parent, text="寻路 (HostMoveToScenePosition)", padding=8)
        path.pack(fill=tk.X, anchor="n")

        prow = ttk.Frame(path)
        prow.pack(fill=tk.X)
        ttk.Label(prow, text="X").pack(side=tk.LEFT)
        self.path_x = tk.StringVar(value="")
        ttk.Entry(prow, textvariable=self.path_x, width=10).pack(side=tk.LEFT, padx=(2, 6))
        ttk.Label(prow, text="Y").pack(side=tk.LEFT)
        self.path_y = tk.StringVar(value="")
        ttk.Entry(prow, textvariable=self.path_y, width=10).pack(side=tk.LEFT, padx=(2, 6))
        ttk.Label(prow, text="Z").pack(side=tk.LEFT)
        self.path_z = tk.StringVar(value="")
        ttk.Entry(prow, textvariable=self.path_z, width=10).pack(side=tk.LEFT, padx=(2, 6))
        ttk.Label(prow, text="mode").pack(side=tk.LEFT)
        self.path_mode = tk.StringVar(value="0")  # live: 0 walks; scene_id often no-op
        ttk.Entry(prow, textvariable=self.path_mode, width=5).pack(side=tk.LEFT, padx=(2, 4))
        ttk.Label(prow, text="(建议0)").pack(side=tk.LEFT)

        brow = ttk.Frame(path)
        brow.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(brow, text="当前坐标", command=self._path_from_host).pack(side=tk.LEFT, padx=2)
        ttk.Button(brow, text="选中候选", command=self._path_from_tree).pack(side=tk.LEFT, padx=2)
        ttk.Button(brow, text="读场景", command=self._path_read_scene).pack(side=tk.LEFT, padx=2)
        ttk.Button(brow, text="开始", command=self._path_start).pack(side=tk.LEFT, padx=2)
        ttk.Button(brow, text="停止", command=self._path_stop).pack(side=tk.LEFT, padx=2)

        crow = ttk.Frame(path)
        crow.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(crow, text="交互R").pack(side=tk.LEFT)
        self.chest_local_range = tk.StringVar(value=str(int(DEFAULT_LOCAL_RANGE)))
        ttk.Entry(crow, textvariable=self.chest_local_range, width=4).pack(
            side=tk.LEFT, padx=(2, 6)
        )
        ttk.Label(crow, text="密集R").pack(side=tk.LEFT)
        self.chest_cluster_r = tk.StringVar(value=str(int(DEFAULT_CLUSTER_RADIUS)))
        ttk.Entry(crow, textvariable=self.chest_cluster_r, width=4).pack(
            side=tk.LEFT, padx=(2, 6)
        )
        ttk.Button(crow, text="扫宝箱业务", command=self._chest_scan_densest).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(crow, text=">R寻路中心", command=self._chest_pathfind_densest).pack(
            side=tk.LEFT, padx=2
        )

        self.var_path = tk.StringVar(
            value="先破0 | d<=R 不做处理 | d>R 密集中心自动寻路"
        )
        ttk.Label(path, textvariable=self.var_path, style="Mono.TLabel", wraplength=880).pack(
            anchor="w", pady=(8, 0)
        )

        # --- 地图飞行调研 (飞行旗 / 飞行棋) ---
        fly = ttk.LabelFrame(parent, text="地图飞行调研 (飞行旗 Win_TransmitFlag / c2s 0x86)", padding=8)
        fly.pack(fill=tk.BOTH, expand=True, anchor="n", pady=(8, 0))

        sum_row = ttk.Frame(fly)
        sum_row.pack(fill=tk.X)
        self.var_fly_summary = tk.StringVar(
            value=(
                "道具=飞行旗 | 默认页 page=255(0xFF) slot0福州/1师门/2死亡点 | "
                "自定义=先定位再飞 | 0x86无xyz不能直接传坐标 | 死亡点服权威"
            )
        )
        ttk.Label(
            sum_row,
            textvariable=self.var_fly_summary,
            style="Mono.TLabel",
            wraplength=920,
            justify=tk.LEFT,
        ).pack(anchor="w", fill=tk.X)

        f1 = ttk.Frame(fly)
        f1.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(f1, text="刷新结论", command=self._fly_refresh_summary).pack(side=tk.LEFT, padx=2)
        ttk.Button(f1, text="查背包飞行旗", command=self._fly_scan_items).pack(side=tk.LEFT, padx=2)
        ttk.Button(f1, text="打开飞行旗", command=self._fly_open_ui).pack(side=tk.LEFT, padx=2)
        ttk.Button(f1, text="读默认点ID", command=self._fly_read_default_ids).pack(side=tk.LEFT, padx=2)
        ttk.Button(f1, text="读取点位文本", command=self._fly_list_points).pack(side=tk.LEFT, padx=2)

        f2 = ttk.Frame(fly)
        f2.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(f2, text="固定点").pack(side=tk.LEFT)
        ttk.Button(f2, text="飞福州城", command=lambda: self._fly_preset("fuzhou")).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(f2, text="飞师门", command=lambda: self._fly_preset("shimen")).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(f2, text="飞死亡点", command=lambda: self._fly_preset("death")).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Label(f2, text="  page").pack(side=tk.LEFT, padx=(8, 0))
        self.fly_page = tk.StringVar(value="255")  # 默认页 Rdo_0 → page=0xFF
        ttk.Entry(f2, textvariable=self.fly_page, width=5).pack(side=tk.LEFT, padx=2)
        ttk.Label(f2, text="slot").pack(side=tk.LEFT)
        self.fly_slot = tk.StringVar(value="0")
        ttk.Entry(f2, textvariable=self.fly_slot, width=4).pack(side=tk.LEFT, padx=2)
        ttk.Label(f2, text="action").pack(side=tk.LEFT)
        self.fly_action = tk.StringVar(value="2")
        ttk.Entry(f2, textvariable=self.fly_action, width=4).pack(side=tk.LEFT, padx=2)
        ttk.Button(f2, text="发包0x86", command=self._fly_send_packet).pack(side=tk.LEFT, padx=2)
        ttk.Button(f2, text="点Btn_Trans", command=self._fly_click_trans).pack(side=tk.LEFT, padx=2)

        f3 = ttk.Frame(fly)
        f3.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(f3, text="坐标调研 X").pack(side=tk.LEFT)
        self.fly_x = tk.StringVar(value="")
        ttk.Entry(f3, textvariable=self.fly_x, width=10).pack(side=tk.LEFT, padx=2)
        ttk.Label(f3, text="Y").pack(side=tk.LEFT)
        self.fly_y = tk.StringVar(value="")
        ttk.Entry(f3, textvariable=self.fly_y, width=10).pack(side=tk.LEFT, padx=2)
        ttk.Label(f3, text="Z").pack(side=tk.LEFT)
        self.fly_z = tk.StringVar(value="")
        ttk.Entry(f3, textvariable=self.fly_z, width=10).pack(side=tk.LEFT, padx=2)
        ttk.Button(f3, text="填当前坐标", command=self._fly_fill_host_pos).pack(side=tk.LEFT, padx=2)
        ttk.Button(f3, text="未记录直飞结论", command=self._fly_coord_research).pack(side=tk.LEFT, padx=2)
        ttk.Button(f3, text="记录后异地飞结论", command=self._fly_record_elsewhere_research).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(f3, text="死亡点结论", command=self._fly_death_research).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(f3, text="读死亡点缓存", command=self._fly_read_death).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(f3, text="定位当前→槽", command=self._fly_sign_probe).pack(side=tk.LEFT, padx=2)
        ttk.Button(f3, text="飞该槽(异地)", command=self._fly_slot_only).pack(side=tk.LEFT, padx=2)

        self.var_fly = tk.StringVar(
            value="流程: 破0 → 打开飞行旗 → page=255 飞福州/师门/死亡点 | 未记录点先定位再飞 | 死亡点不可伪造"
        )
        ttk.Label(fly, textvariable=self.var_fly, style="Mono.TLabel", wraplength=920).pack(
            anchor="w", pady=(6, 0)
        )

        tree_fr = ttk.Frame(fly)
        tree_fr.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        cols = ("slot", "name", "tid", "raw")
        self.fly_tree = ttk.Treeview(
            tree_fr, columns=cols, show="headings", height=6, selectmode="browse"
        )
        self.fly_tree.heading("slot", text="slot")
        self.fly_tree.heading("name", text="名称")
        self.fly_tree.heading("tid", text="默认ID")
        self.fly_tree.heading("raw", text="原始文本")
        self.fly_tree.column("slot", width=48, anchor="center")
        self.fly_tree.column("name", width=160, anchor="w")
        self.fly_tree.column("tid", width=80, anchor="center")
        self.fly_tree.column("raw", width=420, anchor="w")
        ysb = ttk.Scrollbar(tree_fr, orient=tk.VERTICAL, command=self.fly_tree.yview)
        self.fly_tree.configure(yscrollcommand=ysb.set)
        self.fly_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ysb.pack(side=tk.RIGHT, fill=tk.Y)
        self.fly_tree.bind("<<TreeviewSelect>>", self._fly_on_select)
        self.fly_tree.bind("<Double-1>", lambda _e: self._fly_slot_only())

    def _build_tab_interact(self, parent: ttk.Frame) -> None:
        """
        Scan matters, pick target from combobox on this tab, execute by kind.
        @author by ak
        """
        box = ttk.LabelFrame(parent, text="交互 (下拉选目标 → 按类型执行)", padding=8)
        box.pack(fill=tk.BOTH, expand=True, anchor="n")

        row = ttk.Frame(box)
        row.pack(fill=tk.X)
        ttk.Label(row, text="扫描R").pack(side=tk.LEFT)
        self.ix_scan_r = tk.StringVar(value="40")
        ttk.Entry(row, textvariable=self.ix_scan_r, width=5).pack(side=tk.LEFT, padx=(2, 8))
        ttk.Label(row, text="交互maxD").pack(side=tk.LEFT)
        self.ix_max_dist = tk.StringVar(value="12")
        ttk.Entry(row, textvariable=self.ix_max_dist, width=5).pack(side=tk.LEFT, padx=(2, 8))
        self.ix_prefer_actionable = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            row, text="可执行优先", variable=self.ix_prefer_actionable
        ).pack(side=tk.LEFT)

        sel = ttk.Frame(box)
        sel.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(sel, text="目标").pack(side=tk.LEFT)
        self.ix_target_var = tk.StringVar(value="")
        self.ix_target_combo = ttk.Combobox(
            sel,
            textvariable=self.ix_target_var,
            state="readonly",
            width=72,
            font=("Microsoft YaHei UI", 9),
        )
        self.ix_target_combo.pack(side=tk.LEFT, padx=(4, 0), fill=tk.X, expand=True)
        self.ix_target_combo.bind("<<ComboboxSelected>>", self._ix_on_combo_selected)

        brow = ttk.Frame(box)
        brow.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(brow, text="扫描", command=self._ix_scan).pack(side=tk.LEFT, padx=2)
        ttk.Button(brow, text="选最近", command=self._ix_nearest).pack(side=tk.LEFT, padx=2)
        ttk.Button(brow, text="执行选中", command=self._ix_execute_selected).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(brow, text="走近选中", command=self._ix_move_to_selected).pack(
            side=tk.LEFT, padx=2
        )

        self.var_ix = tk.StringVar(value="先扫描 → 下拉选目标 → 执行选中")
        ttk.Label(box, textvariable=self.var_ix, style="Mono.TLabel", wraplength=900).pack(
            anchor="w", pady=(8, 0)
        )
        ttk.Label(
            box,
            text="安全执行: 仅 SetTarget(+可选PickItem)。AutoClick 已关（远程会闪退）。",
            style="Mono.TLabel",
            wraplength=900,
        ).pack(anchor="w", pady=(6, 0))

        self._ix_targets: list[dict] = []
        self._ix_combo_labels: list[str] = []
        self._ix_selected_idx: int | None = None

    def _build_tab_task(self, parent: ttk.Frame) -> None:
        """
        Dev test: accepted task list / accept / complete (no packets).

        @author by ak
        """
        box = ttk.LabelFrame(parent, text="任务（测试 · 已接 / 可接 / 接 / 交）", padding=8)
        box.pack(fill=tk.BOTH, expand=True, anchor="n")

        row = ttk.Frame(box)
        row.pack(fill=tk.X)
        ttk.Button(row, text="刷新已接", command=self._task_refresh_accepted).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(row, text="刷新可接", command=self._task_refresh_available).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(row, text="所有任务", command=self._task_refresh_all).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(row, text="刷新交任务NPC", command=self._task_refresh_npcs).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(row, text="特征校验", command=self._task_check_patterns).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(row, text="接选中任务", command=self._task_accept_selected).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(row, text="交选中任务", command=self._task_complete_selected).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(row, text="寻路到线索", command=self._task_pathfind_selected_clue).pack(
            side=tk.LEFT, padx=2
        )

        row2 = ttk.Frame(box)
        row2.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row2, text="任务ID").pack(side=tk.LEFT)
        self.task_id_var = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=self.task_id_var, width=12).pack(
            side=tk.LEFT, padx=(4, 10)
        )
        ttk.Label(row2, text="交任务NPC").pack(side=tk.LEFT)
        self.task_npc_var = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=self.task_npc_var, width=18).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        mid = ttk.Frame(box)
        mid.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)
        mid.columnconfigure(2, weight=1)
        mid.rowconfigure(1, weight=1)

        ttk.Label(mid, text="已接任务（双击=线索/寻路）", style="Header.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(mid, text="任务线索（双击寻路）", style="Header.TLabel").grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        ttk.Label(mid, text="交任务NPC（附近）", style="Header.TLabel").grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )

        lf = ttk.Frame(mid)
        lf.grid(row=1, column=0, sticky="nsew", padx=(0, 4))
        self.task_accepted_list = tk.Listbox(
            lf,
            height=12,
            font=("Microsoft YaHei UI", 9),
            bg="#0d1117",
            fg="#c9d1d9",
            selectbackground="#1f6feb",
            activestyle="none",
            exportselection=False,
        )
        sb1 = ttk.Scrollbar(lf, orient=tk.VERTICAL, command=self.task_accepted_list.yview)
        self.task_accepted_list.configure(yscrollcommand=sb1.set)
        self.task_accepted_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb1.pack(side=tk.RIGHT, fill=tk.Y)
        self.task_accepted_list.bind("<<ListboxSelect>>", self._task_on_accepted_select)
        self.task_accepted_list.bind("<Double-Button-1>", self._task_on_accepted_double)

        rf = ttk.Frame(mid)
        rf.grid(row=1, column=1, sticky="nsew", padx=(4, 4))
        self.task_available_list = tk.Listbox(
            rf,
            height=12,
            font=("Microsoft YaHei UI", 9),
            bg="#0d1117",
            fg="#c9d1d9",
            selectbackground="#1f6feb",
            activestyle="none",
            exportselection=False,
        )
        sb2 = ttk.Scrollbar(rf, orient=tk.VERTICAL, command=self.task_available_list.yview)
        self.task_available_list.configure(yscrollcommand=sb2.set)
        self.task_available_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb2.pack(side=tk.RIGHT, fill=tk.Y)
        self.task_available_list.bind(
            "<<ListboxSelect>>", self._task_on_available_select
        )
        # reuse available list as clue list (label updated above)
        self.task_clue_list = self.task_available_list
        self.task_clue_list.bind("<Double-Button-1>", self._task_on_clue_double)

        nf = ttk.Frame(mid)
        nf.grid(row=1, column=2, sticky="nsew", padx=(4, 0))
        self.task_npc_list = tk.Listbox(
            nf,
            height=12,
            font=("Microsoft YaHei UI", 9),
            bg="#0d1117",
            fg="#c9d1d9",
            selectbackground="#1f6feb",
            activestyle="none",
            exportselection=False,
        )
        sb3 = ttk.Scrollbar(nf, orient=tk.VERTICAL, command=self.task_npc_list.yview)
        self.task_npc_list.configure(yscrollcommand=sb3.set)
        self.task_npc_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb3.pack(side=tk.RIGHT, fill=tk.Y)
        self.task_npc_list.bind("<<ListboxSelect>>", self._task_on_npc_select)

        self.var_task = tk.StringVar(
            value="双击已接任务→解析附近线索；双击线索→HostMove寻路。可交=原生CanFinish，非SUCCESS位alone。"
        )
        ttk.Label(
            box, textvariable=self.var_task, style="Mono.TLabel", wraplength=900
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            box,
            text="列表格式：已接=编号/进度/名称；交任务NPC=名称/距离/编号。点选自动填入任务ID或NPC名。",
            style="Mono.TLabel",
            wraplength=900,
        ).pack(anchor="w", pady=(4, 0))

        self._task_accepted_rows: list[dict] = []
        self._task_available_rows: list[dict] = []
        self._task_npc_rows: list[dict] = []
        self._task_clue_rows: list[dict] = []

    def _task_require_session(self):
        """Return attach session or None with log. @author by ak"""
        if not self.session or not getattr(self.session, "module_base", None):
            self.log("任务: 请先破0附加（需要 module_base）")
            messagebox.showinfo("任务", "请先破0附加游戏进程")
            return None
        return self.session

    def _task_check_patterns(self) -> None:
        """Log PE pattern resolve for task APIs. @author by ak"""
        try:
            pe = getattr(self.session, "exe_path", None) if self.session else None
            info = resolve_task_patterns(pe)
            self.log(f"任务特征: 通过={info.get('ok')} pe={info.get('pe')}")
            self.log(
                f"  接口RVA={info.get('get_task_iface_rva')} "
                f"接任务RVA={info.get('accept_call_rva')} "
                f"交任务RVA={info.get('complete_call_rva')} "
                f"管理器偏移={info.get('task_mgr_this_off')}"
            )
            for e in info.get("errors") or []:
                self.log(f"  警告: {e}")
            self.var_task.set(
                f"特征校验 通过={info.get('ok')} 接任务RVA={info.get('accept_call_rva')}"
            )
        except Exception as e:
            self.log(f"任务特征校验失败: {e}")
            self.var_task.set(f"特征校验失败: {e}")

    def _task_refresh_accepted(self) -> None:
        """Refresh accepted task list in background. @author by ak"""
        sess = self._task_require_session()
        if not sess:
            return
        self.status.set("TASK")
        self.var_task.set("正在刷新已接任务…")
        self.log("任务: 正在刷新已接任务列表…")

        def worker() -> None:
            try:
                rows = list_accepted_tasks(sess, log=self.log)
                data = [t.to_dict() if hasattr(t, "to_dict") else t for t in rows]
                self.msg_q.put(("__TASK_ACCEPTED__", data))
            except Exception as e:
                self.msg_q.put(("__ERROR__", f"任务已接: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _task_refresh_available(self) -> None:
        """Refresh available tasks (may be empty on this build). @author by ak"""
        sess = self._task_require_session()
        if not sess:
            return
        self.status.set("TASK")
        self.var_task.set("正在刷新可接任务…")
        self.log("任务: 正在刷新可接任务列表…")

        def worker() -> None:
            try:
                rows = list_available_tasks(sess, log=self.log)
                data = [t.to_dict() if hasattr(t, "to_dict") else t for t in rows]
                self.msg_q.put(("__TASK_AVAILABLE__", data))
            except Exception as e:
                self.msg_q.put(("__ERROR__", f"任务可接: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _task_refresh_all(self) -> None:
        """Enumerate the FULL quest-config table from memory (black-shield style). @author by ak"""
        sess = self._task_require_session()
        if not sess:
            return
        self.status.set("TASK")
        self.var_task.set("正在枚举所有任务…")
        self.log("任务: 正在枚举全量任务表（内存直读）…")

        def worker() -> None:
            try:
                from app.core.task_api import enumerate_all_tasks

                rows = enumerate_all_tasks(sess, log=self.log)
                self.msg_q.put(("__TASK_ALL__", rows))
            except Exception as e:
                self.msg_q.put(("__ERROR__", f"所有任务: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _task_fmt_accepted_line(self, d: dict) -> str:
        """Chinese line for one accepted task (status first). @author by ak"""
        tid = d.get("task_id")
        prog = d.get("progress")
        name = (d.get("name") or "").strip() or f"编号{tid}"
        status = (d.get("status_text") or "").strip()
        if not status:
            if d.get("can_finish"):
                status = "可交"
            elif d.get("is_finished"):
                status = "已完成"
            elif prog is not None and int(prog) > 0:
                status = f"进行中({prog})"
            else:
                status = "进行中"
        prog_s = "—" if prog is None else str(prog)
        return f"[{status}] {name}  编号{tid}  进度{prog_s}"

    def _task_fmt_available_line(self, d: dict) -> str:
        """Chinese line for one available task. @author by ak"""
        tid = d.get("task_id")
        name = (d.get("name") or "").strip()
        if name:
            return f"{name}  编号{tid}"
        return f"编号{tid}  （无名称）"

    def _task_fmt_clue_line(self, d: dict) -> str:
        """Chinese line for one task clue / 目标. @author by ak"""
        clue = (d.get("clue") or "线索").strip()
        name = (d.get("name") or "（无名）").strip()
        dist = d.get("dist")
        try:
            dist_s = f"{float(dist):.1f}米" if dist is not None else "距离—"
        except (TypeError, ValueError):
            dist_s = "距离—"
        tid = d.get("tid")
        tid_s = f" tid{tid}" if tid else ""
        sid = d.get("scene_id")
        scene_s = f" 场景{int(sid)}" if sid else ""
        src = d.get("source") or ""
        cross = " ·可跨图" if src in ("reach_site", "cfg_object") and (sid or src == "cfg_object") else ""
        xyz_ok = d.get("x") is not None and d.get("z") is not None
        path_s = "" if xyz_ok else " ·无坐标"
        return f"[{clue}] {name}{tid_s}{scene_s}  {dist_s}{cross}{path_s}"

    def _task_fmt_npc_line(self, d: dict) -> str:
        """Chinese line for one complete-task NPC. @author by ak"""
        name = (d.get("name") or "").strip() or "（无名）"
        dist = d.get("dist")
        if dist is None:
            dist = d.get("distance")
        try:
            dist_s = f"{float(dist):.1f}米" if dist is not None else "距离—"
        except (TypeError, ValueError):
            dist_s = "距离—"
        oid = d.get("obj_id")
        if oid is None and d.get("id_lo") is not None:
            lo = int(d.get("id_lo") or 0) & 0xFFFFFFFF
            hi = int(d.get("id_hi") or 0) & 0xFFFFFFFF
            oid = lo | (hi << 32)
        oid_s = f"ID{oid}" if oid is not None else "ID—"
        return f"{name}  {dist_s}  {oid_s}"

    def _task_apply_accepted(self, rows: list) -> None:
        """Fill accepted listbox (Chinese). @author by ak"""
        self._task_accepted_rows = list(rows or [])
        self.task_accepted_list.delete(0, tk.END)
        if not self._task_accepted_rows:
            self.task_accepted_list.insert(tk.END, "  （暂无已接任务）")
            self.var_task.set("已接任务 0 条")
            self.status.set("ATTACHED" if self.session else "IDLE")
            return
        # 可交置顶显示
        self._task_accepted_rows.sort(
            key=lambda d: (
                0 if d.get("can_finish") else 1,
                0 if d.get("is_finished") else 1,
                int(d.get("task_id") or 0),
            )
        )
        for d in self._task_accepted_rows:
            self.task_accepted_list.insert(tk.END, self._task_fmt_accepted_line(d))
        can_n = sum(1 for d in self._task_accepted_rows if d.get("can_finish"))
        can_names = [
            (d.get("name") or str(d.get("task_id")))
            for d in self._task_accepted_rows
            if d.get("can_finish")
        ]
        tip = f"已接 {len(self._task_accepted_rows)} · 可交 {can_n}"
        if can_names:
            tip += "：" + "、".join(can_names[:4])
            if len(can_names) > 4:
                tip += "…"
        tip += " · 未完成不可交"
        self.var_task.set(tip)
        self.status.set("ATTACHED" if self.session else "IDLE")

    def _task_apply_available(self, rows: list) -> None:
        """Fill available listbox (Chinese). @author by ak"""
        self._task_available_rows = list(rows or [])
        self.task_available_list.delete(0, tk.END)
        if not self._task_available_rows:
            self.task_available_list.insert(tk.END, "  （暂无可接 / 未解析）")
            self.var_task.set("可接任务 0 条（当前版本可接列表未解析）")
            self.status.set("ATTACHED" if self.session else "IDLE")
            return
        for d in self._task_available_rows:
            self.task_available_list.insert(tk.END, self._task_fmt_available_line(d))
        self.var_task.set(f"可接任务 {len(self._task_available_rows)} 条")
        self.status.set("ATTACHED" if self.session else "IDLE")

    def _task_apply_all(self, rows: list) -> None:
        """Fill middle listbox with the FULL quest table (memory enumerate). @author by ak"""
        self._task_available_rows = list(rows or [])
        self._task_clue_rows = []
        self.task_available_list.delete(0, tk.END)
        if not self._task_available_rows:
            self.task_available_list.insert(tk.END, "  （全量任务表为空 / 读取失败）")
            self.var_task.set("所有任务 0 条")
            self.status.set("ATTACHED" if self.session else "IDLE")
            return
        for d in self._task_available_rows:
            self.task_available_list.insert(tk.END, self._task_fmt_available_line(d))
        self.var_task.set(
            f"所有任务 {len(self._task_available_rows)} 条（内存直读，点选填入ID）"
        )
        self.status.set("ATTACHED" if self.session else "IDLE")

    def _task_apply_clues(self, rows: list) -> None:
        """Fill middle list as task clues. @author by ak"""
        self._task_clue_rows = list(rows or [])
        # clear available cache when showing clues
        self._task_available_rows = []
        self.task_available_list.delete(0, tk.END)
        if not self._task_clue_rows:
            self.task_available_list.insert(
                tk.END, "  （附近无线索目标；靠近任务区后再双击任务）"
            )
            self.var_task.set("任务线索 0 条")
            self.status.set("ATTACHED" if self.session else "IDLE")
            return
        for d in self._task_clue_rows:
            self.task_available_list.insert(tk.END, self._task_fmt_clue_line(d))
        self.var_task.set(
            f"任务线索 {len(self._task_clue_rows)} 条 · 双击寻路 / 点「寻路到线索」"
        )
        self.status.set("ATTACHED" if self.session else "IDLE")

    def _task_apply_npcs(self, rows: list) -> None:
        """Fill complete-task NPC listbox (Chinese). @author by ak"""
        self._task_npc_rows = list(rows or [])
        self.task_npc_list.delete(0, tk.END)
        if not self._task_npc_rows:
            self.task_npc_list.insert(tk.END, "  （附近无NPC）")
            self.var_task.set("交任务NPC 0 个")
            self.status.set("ATTACHED" if self.session else "IDLE")
            return
        for d in self._task_npc_rows:
            self.task_npc_list.insert(tk.END, self._task_fmt_npc_line(d))
        self.var_task.set(f"交任务NPC {len(self._task_npc_rows)} 个（点选填入）")
        self.status.set("ATTACHED" if self.session else "IDLE")

    def _task_on_accepted_select(self, _evt=None) -> None:
        """Copy selected accepted id into entry. @author by ak"""
        try:
            sel = self.task_accepted_list.curselection()
            if not sel:
                return
            idx = int(sel[0])
            if 0 <= idx < len(self._task_accepted_rows):
                tid = self._task_accepted_rows[idx].get("task_id")
                if tid is not None:
                    self.task_id_var.set(str(tid))
        except Exception:
            pass

    def _task_on_available_select(self, _evt=None) -> None:
        """Copy selected available id or clue NPC name. @author by ak"""
        try:
            sel = self.task_available_list.curselection()
            if not sel:
                return
            idx = int(sel[0])
            if getattr(self, "_task_clue_rows", None) and 0 <= idx < len(
                self._task_clue_rows
            ):
                clue = self._task_clue_rows[idx]
                name = (clue.get("name") or "").strip()
                if name and clue.get("kind") == "npc":
                    self.task_npc_var.set(name)
                tid = clue.get("task_id")
                if tid is not None:
                    self.task_id_var.set(str(tid))
                return
            if 0 <= idx < len(self._task_available_rows):
                tid = self._task_available_rows[idx].get("task_id")
                if tid is not None:
                    self.task_id_var.set(str(tid))
        except Exception:
            pass

    def _task_on_accepted_double(self, _evt=None) -> None:
        """Double-click accepted task -> load nearby clues. @author by ak"""
        try:
            sel = self.task_accepted_list.curselection()
            if not sel:
                return
            idx = int(sel[0])
            if not (0 <= idx < len(self._task_accepted_rows)):
                return
            row = self._task_accepted_rows[idx]
            tid = row.get("task_id")
            if tid is not None:
                self.task_id_var.set(str(tid))
            self._task_load_clues_for(row)
        except Exception as e:
            self.log(f"任务线索: {e}")

    def _task_on_clue_double(self, _evt=None) -> None:
        """Double-click clue -> pathfind. @author by ak"""
        self._task_pathfind_selected_clue()

    def _task_load_clues_for(self, task_row: dict) -> None:
        """Background scan clues for one task. @author by ak"""
        sess = self._task_require_session()
        if not sess:
            return
        tid = task_row.get("task_id")
        self.status.set("TASK")
        self.var_task.set(f"解析任务线索 编号{tid}…")
        self.log(
            f"任务: 解析线索 id={tid} status={task_row.get('status_text')} "
            f"state=0x{int(task_row.get('state') or 0):X}"
        )

        def worker() -> None:
            try:
                clues = find_task_clue_targets(sess, task_row, radius=200.0, log=self.log)
                self.msg_q.put(("__TASK_CLUES__", clues))
            except Exception as e:
                self.msg_q.put(("__ERROR__", f"任务线索: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _task_pathfind_selected_clue(self) -> None:
        """Resolve the selected task's own route and pathfind. @author by ak"""
        sess = self._task_require_session()
        if not sess:
            return
        tid = self._task_parse_id()
        row = self._task_row_by_id(tid) if tid is not None else None
        if row is None:
            messagebox.showinfo("任务", "请先选择一条已接任务")
            return
        self.status.set("TASK")
        self.var_task.set(f"解析并寻路 → {row.get('name') or tid}")
        self.log(f"任务: 自动解析寻路 编号={tid}")

        def worker() -> None:
            try:
                res = pathfind_task(sess, row, log=self.log)
                self.msg_q.put(("__TASK_PATH__", res))
            except Exception as e:
                self.msg_q.put(("__ERROR__", f"任务寻路: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _task_apply_path(self, d: dict) -> None:
        """Show pathfind result. @author by ak"""
        if not isinstance(d, dict):
            return
        ok = d.get("ok")
        self.log(
            f"任务寻路 ok={ok} ret={d.get('ret')} name={d.get('name')} "
            f"err={d.get('error')} note={d.get('note')}"
        )
        self.var_task.set(
            f"寻路{'成功' if ok else '失败'} → {d.get('clue') or ''} {d.get('name') or ''}"
            + (f" · {d.get('error')}" if d.get("error") else "")
        )
        self.status.set("ATTACHED" if self.session else "IDLE")

    def _task_on_npc_select(self, _evt=None) -> None:
        """Fill complete-task NPC name from list selection. @author by ak"""
        try:
            sel = self.task_npc_list.curselection()
            if not sel:
                return
            idx = int(sel[0])
            if 0 <= idx < len(self._task_npc_rows):
                name = (self._task_npc_rows[idx].get("name") or "").strip()
                if name:
                    self.task_npc_var.set(name)
        except Exception:
            pass

    def _task_parse_id(self) -> int | None:
        """Parse task id entry. @author by ak"""
        raw = (self.task_id_var.get() or "").strip()
        if not raw:
            return None
        try:
            if raw.lower().startswith("0x"):
                return int(raw, 16)
            return int(raw)
        except ValueError:
            return None

    def _task_selected_npc_row(self) -> dict | None:
        """Return selected NPC row, or match by name entry. @author by ak"""
        try:
            sel = self.task_npc_list.curselection()
            if sel:
                idx = int(sel[0])
                if 0 <= idx < len(self._task_npc_rows):
                    return self._task_npc_rows[idx]
        except Exception:
            pass
        name = (self.task_npc_var.get() or "").strip()
        if not name:
            return None
        for row in getattr(self, "_task_npc_rows", []) or []:
            n = str(row.get("name") or "")
            if name == n or name in n or n in name:
                return row
        return None

    def _task_entity_to_dict(self, h) -> dict:
        """Normalize entity hit to dict for complete. @author by ak"""
        if hasattr(h, "to_dict"):
            d = h.to_dict()
            if isinstance(d, dict):
                return d
        if isinstance(h, dict):
            return h
        return {
            "name": getattr(h, "name", None) or "",
            "obj_id": getattr(h, "obj_id", None),
            "ptr": getattr(h, "ptr", None),
            "dist": getattr(h, "dist", None),
            "id_lo": getattr(h, "id_lo", None),
            "id_hi": getattr(h, "id_hi", None),
        }

    def _task_refresh_npcs(self) -> None:
        """Scan nearby NPCs for complete-task picker. @author by ak"""
        sess = self._task_require_session()
        if not sess:
            return
        self.status.set("TASK")
        self.var_task.set("刷新交任务NPC…")
        self.log("任务: 刷新附近交任务NPC…")

        def worker() -> None:
            try:
                from app.core.entity_scan import scan_nearby_entities

                hits = scan_nearby_entities(
                    sess, kinds=("npc",), radius=80.0, log=self.log
                )
                rows: list[dict] = []
                for h in hits or []:
                    d = self._task_entity_to_dict(h)
                    name = (d.get("name") or "").strip()
                    if not name:
                        continue
                    rows.append(d)
                # nearer first when dist available
                def _key(r: dict):
                    dist = r.get("dist")
                    if dist is None:
                        dist = r.get("distance")
                    try:
                        return float(dist)
                    except (TypeError, ValueError):
                        return 9999.0

                rows.sort(key=_key)
                self.msg_q.put(("__TASK_NPCS__", rows))
            except Exception as e:
                self.msg_q.put(("__ERROR__", f"交任务NPC: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _task_accept_selected(self) -> None:
        """Accept task id from entry / selection. @author by ak"""
        sess = self._task_require_session()
        if not sess:
            return
        tid = self._task_parse_id()
        if tid is None:
            messagebox.showinfo("任务", "请填写或点选一条任务（任务ID）")
            return
        hwnd = 0
        if self.current_window:
            hwnd = int(getattr(self.current_window, "hwnd", 0) or 0)
        self.status.set("TASK")
        self.var_task.set(f"正在接任务 编号{tid}…")
        self.log(f"任务: 接任务 编号={tid}")

        def worker() -> None:
            try:
                res = accept_task_routed(sess, tid, hwnd=hwnd, log=self.log)
                self.msg_q.put(
                    ("__TASK_OP__", res.to_dict() if hasattr(res, "to_dict") else res)
                )
            except Exception as e:
                self.msg_q.put(("__ERROR__", f"接任务: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _task_find_npc_by_name(self, name: str) -> dict | None:
        """Find nearby NPC from task NPC list / interact cache / live scan. @author by ak"""
        name = (name or "").strip()
        if not name:
            return None
        for t in getattr(self, "_task_npc_rows", []) or []:
            n = str(t.get("name") or "")
            if name == n or name in n or n in name:
                return t
        for t in getattr(self, "_ix_targets", []) or []:
            n = str(t.get("name") or "")
            if name in n or n in name:
                return t
        try:
            from app.core.entity_scan import scan_nearby_entities

            hits = scan_nearby_entities(
                self.session, kinds=("npc",), radius=80.0, log=self.log
            )
            for h in hits:
                d = self._task_entity_to_dict(h)
                n = str(d.get("name") or "")
                if name in n or n in name:
                    return d
        except Exception as e:
            self.log(f"任务: 交任务NPC扫描失败 {e}")
        return None

    def _task_row_by_id(self, tid: int) -> dict | None:
        """Find accepted-task row by id. @author by ak"""
        for r in getattr(self, "_task_accepted_rows", []) or []:
            try:
                if int(r.get("task_id") or 0) == int(tid):
                    return r
            except (TypeError, ValueError):
                continue
        return None

    def _task_complete_selected(self) -> None:
        """Resolve the task's AwardNPC and complete it. @author by ak"""
        sess = self._task_require_session()
        if not sess:
            return
        tid = self._task_parse_id()
        if tid is None:
            messagebox.showinfo("任务", "请填写或点选一条任务（任务ID）")
            return
        row = self._task_row_by_id(tid)
        can_ok = False
        if row is not None:
            if row.get("can_finish") is not None:
                can_ok = bool(row.get("can_finish"))
            else:
                can_ok = bool(row.get("is_success")) and bool(row.get("is_finished"))
        if row is not None and not can_ok:
            name = (row.get("name") or "").strip() or f"编号{tid}"
            st = (row.get("status_text") or "进行中").strip()
            messagebox.showinfo(
                "任务",
                f"「{name}」当前是【{st}】，未完成不能交。\n"
                "只有【可交】的任务才能交（原生 CanFinish=真，如小有所成、镖局第一式）。",
            )
            self.var_task.set(f"{name} 未完成，不能交")
            self.log(
                f"任务: 拒绝交 编号={tid} status={st} can_finish={row.get('can_finish')} "
                f"state=0x{int(row.get('state') or 0):X}"
            )
            return
        hwnd = 0
        if self.current_window:
            hwnd = int(getattr(self.current_window, "hwnd", 0) or 0)
        self.status.set("TASK")
        self.var_task.set(f"正在解析NPC并交任务 编号{tid}…")
        self.log(f"任务: 自动解析NPC并交任务 编号={tid}")

        def worker() -> None:
            try:
                res = complete_task_routed(sess, row or tid, hwnd=hwnd, log=self.log)
                self.msg_q.put(
                    ("__TASK_OP__", res.to_dict() if hasattr(res, "to_dict") else res)
                )
            except Exception as e:
                self.msg_q.put(("__ERROR__", f"交任务: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _task_apply_op(self, d: dict) -> None:
        """Log accept/complete result and refresh accepted list. @author by ak"""
        if not isinstance(d, dict):
            return
        ok = d.get("ok")
        action = d.get("action")
        tid = d.get("task_id")
        err = d.get("error")
        note = d.get("note")
        action_cn = {
            "accept": "接任务",
            "complete": "交任务",
        }.get(str(action or ""), str(action or "操作"))
        ok_cn = "成功" if ok else "失败"
        self.log(
            f"任务 {action_cn} 编号={tid} 结果={ok_cn} ret={d.get('ret')} "
            f"接前={d.get('before_ids')} 接后={d.get('after_ids')} "
            f"err={err} note={note}"
        )
        self.var_task.set(
            f"{action_cn} 编号{tid} {ok_cn}" + (f" · {err}" if err else "")
        )
        self.status.set("ATTACHED" if self.session else "IDLE")
        if self.session:
            self.after(200, self._task_refresh_accepted)

    def _build_tab_packet(self, parent: ttk.Frame) -> None:
        """
        Packet tab: core hooks + auto unpack from x32dbg logs + pktmon capture.

        Goal: after one-time debugger setup, only watch feature response + logs
        to locate plain/cipher without re-debugging.

        @author by ak
        """
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(wrap, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=body, anchor="nw")

        def _on_cfg(_e=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas(e) -> None:
            canvas.itemconfigure(win, width=e.width)

        def _wheel(e) -> None:
            delta = int(getattr(e, "delta", 0) or 0)
            if delta:
                canvas.yview_scroll(int(-1 * (delta / 120)), "units")

        def _bind_wheel(_e=None) -> None:
            canvas.bind_all("<MouseWheel>", _wheel)

        def _unbind_wheel(_e=None) -> None:
            try:
                canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass

        body.bind("<Configure>", _on_cfg)
        canvas.bind("<Configure>", _on_canvas)
        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)
        self._pkt_canvas = canvas

        # --- core points ---
        core = ttk.LabelFrame(body, text="核心点（明文 / 密文 / 钩子）", padding=8)
        core.pack(fill=tk.X, padx=2, pady=(2, 6))
        self.var_pkt_core = tk.StringVar(
            value=(
                "明文: ARCFour Update 入口 0x00DAFAF0 (日志 PLAIN)  |  "
                "密文: ws2_32.send (日志 SEND10)  |  "
                "管道: marshal → Update → send_wrapper 0xD0C130 → send"
            )
        )
        ttk.Label(
            core,
            textvariable=self.var_pkt_core,
            style="Mono.TLabel",
            wraplength=920,
            justify=tk.LEFT,
        ).pack(anchor="w", fill=tk.X)
        row_core = ttk.Frame(core)
        row_core.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(row_core, text="刷新核心点", command=self._pkt_refresh_core).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(row_core, text="复制核心点", command=self._pkt_copy_core).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(row_core, text="打开 CORE_POINTS", command=self._pkt_open_core_md).pack(
            side=tk.LEFT, padx=(0, 4)
        )

        # --- auto analyze ---
        ana = ttk.LabelFrame(body, text="自动分析解包（x32dbg / 运行时日志）", padding=8)
        ana.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 6))

        row_a1 = ttk.Frame(ana)
        row_a1.pack(fill=tk.X)
        ttk.Label(row_a1, text="日志").pack(side=tk.LEFT)
        self.pkt_log_path = tk.StringVar(value="")
        ttk.Entry(row_a1, textvariable=self.pkt_log_path).pack(
            side=tk.LEFT, padx=4, fill=tk.X, expand=True
        )
        ttk.Button(row_a1, text="选择…", command=self._pkt_pick_log, width=8).pack(
            side=tk.LEFT, padx=2
        )

        row_a2 = ttk.Frame(ana)
        row_a2.pack(fill=tk.X, pady=(6, 0))
        self.pkt_ana_btn = tk.Button(
            row_a2,
            text="分析日志",
            command=self._pkt_analyze_log,
            bg="#1f6feb",
            fg="white",
            activebackground="#388bfd",
            activeforeground="white",
            font=("Microsoft YaHei UI", 9, "bold"),
            width=10,
            relief=tk.RAISED,
            padx=4,
            pady=2,
        )
        self.pkt_ana_btn.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(row_a2, text="分析最新", command=self._pkt_analyze_latest).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(row_a2, text="生成 x32dbg 脚本", command=self._pkt_write_recipes).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(row_a2, text="打开报告", command=self._pkt_open_report).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(row_a2, text="issues 目录", command=self._pkt_open_issues_dir).pack(
            side=tk.LEFT, padx=2
        )
        self.var_pkt_ana = tk.StringVar(
            value="配置一次钩子 → 触发功能 → 保存日志 → 分析（明文/密文自动对齐）"
        )
        ttk.Label(
            ana, textvariable=self.var_pkt_ana, style="Mono.TLabel", wraplength=920
        ).pack(anchor="w", fill=tk.X, pady=(6, 0))

        list_fr = ttk.Frame(ana)
        list_fr.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.pkt_ana_list = tk.Listbox(
            list_fr,
            height=12,
            font=("Consolas", 9),
            activestyle="dotbox",
        )
        sb = ttk.Scrollbar(list_fr, orient=tk.VERTICAL, command=self.pkt_ana_list.yview)
        self.pkt_ana_list.configure(yscrollcommand=sb.set)
        self.pkt_ana_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        # --- pktmon capture (existing) ---
        pkt = ttk.LabelFrame(body, text="网络封包捕获（pktmon，全量普通包）", padding=8)
        pkt.pack(fill=tk.X, padx=2, pady=(0, 4))

        row1 = ttk.Frame(pkt)
        row1.pack(fill=tk.X)
        ttk.Label(row1, text="动作").pack(side=tk.LEFT)
        self.pkt_action = tk.StringVar(value="open_chest")
        self.pkt_action_combo = ttk.Combobox(
            row1,
            textvariable=self.pkt_action,
            values=("pickup", "open_chest", "gather", "pathfind", "boss_summon", "custom"),
            state="readonly",
            width=12,
        )
        self.pkt_action_combo.pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(row1, text="baseline").pack(side=tk.LEFT)
        self.pkt_baseline = tk.StringVar(value="5")
        ttk.Entry(row1, textvariable=self.pkt_baseline, width=5).pack(
            side=tk.LEFT, padx=(4, 10)
        )
        ttk.Label(row1, text="动作窗s").pack(side=tk.LEFT)
        self.pkt_duration = tk.StringVar(value="60")
        ttk.Entry(row1, textvariable=self.pkt_duration, width=5).pack(
            side=tk.LEFT, padx=(4, 10)
        )
        self.pkt_manual_stop = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            row1,
            text="手动结束",
            variable=self.pkt_manual_stop,
        ).pack(side=tk.LEFT)

        row2 = ttk.Frame(pkt)
        row2.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row2, text="备注").pack(side=tk.LEFT)
        self.pkt_notes = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=self.pkt_notes).pack(
            side=tk.LEFT, padx=4, fill=tk.X, expand=True
        )

        row_ctrl = ttk.Frame(pkt)
        row_ctrl.pack(fill=tk.X, pady=(8, 0))
        self.pkt_start_btn = tk.Button(
            row_ctrl,
            text="开始捕获",
            command=self._start_pkt_capture,
            bg="#1f6feb",
            fg="white",
            activebackground="#388bfd",
            activeforeground="white",
            font=("Microsoft YaHei UI", 9, "bold"),
            width=8,
            relief=tk.RAISED,
            padx=6,
            pady=3,
        )
        self.pkt_start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.pkt_stop_btn = tk.Button(
            row_ctrl,
            text="结束",
            command=self._stop_pkt_capture,
            bg="#6e7681",
            fg="white",
            activebackground="#f85149",
            activeforeground="white",
            font=("Microsoft YaHei UI", 9, "bold"),
            width=8,
            relief=tk.RAISED,
            padx=6,
            pady=3,
            state=tk.DISABLED,
        )
        self.pkt_stop_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(row_ctrl, text="捕获目录", command=self._open_pkt_dir).pack(
            side=tk.LEFT, padx=2
        )
        self.var_pkt_phase = tk.StringVar(value="阶段: 空闲")
        ttk.Label(row_ctrl, textvariable=self.var_pkt_phase, style="Mono.TLabel").pack(
            side=tk.LEFT, padx=(10, 0)
        )

        row3 = ttk.Frame(pkt)
        row3.pack(fill=tk.X, pady=(6, 0))
        self.var_pkt = tk.StringVar(value="取句柄 → 开始 → baseline → 动作 → 结束（密文侧差分）")
        ttk.Label(
            row3, textvariable=self.var_pkt, style="Mono.TLabel", wraplength=900
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # --- CD1740 game-side interception + replay ---
        pki = ttk.LabelFrame(body, text="系统指令拦截（CD1740，仅覆盖系统指令）", padding=8)
        pki.pack(fill=tk.X, padx=2, pady=(0, 4))
        row_i1 = ttk.Frame(pki)
        row_i1.pack(fill=tk.X)
        self.pkti_start_btn = tk.Button(
            row_i1,
            text="开始拦截",
            command=self._pkti_start,
            bg="#1f6feb",
            fg="white",
            activebackground="#388bfd",
            activeforeground="white",
            font=("Microsoft YaHei UI", 9, "bold"),
            width=8,
            relief=tk.RAISED,
            padx=6,
            pady=3,
        )
        self.pkti_start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.pkti_stop_btn = tk.Button(
            row_i1,
            text="停止并导出",
            command=self._pkti_stop,
            bg="#6e7681",
            fg="white",
            activebackground="#f85149",
            activeforeground="white",
            font=("Microsoft YaHei UI", 9, "bold"),
            width=8,
            relief=tk.RAISED,
            padx=6,
            pady=3,
            state=tk.DISABLED,
        )
        self.pkti_stop_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(row_i1, text="测试发送", command=self._pkti_test_send).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(row_i1, text="重发选中", command=self._pkti_resend).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(row_i1, text="保存动作", command=self._pkti_save).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(row_i1, text="打开记录", command=self._pkti_open).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(row_i1, text="回放动作", command=self._pkti_replay).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(row_i1, text="清空列表", command=self._pkti_clear).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        self.var_pkti_status = tk.StringVar(value="空闲")
        ttk.Label(row_i1, textvariable=self.var_pkti_status, style="Mono.TLabel").pack(
            side=tk.LEFT, padx=(10, 0)
        )
        row_i1b = ttk.Frame(pki)
        row_i1b.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(row_i1b, text="标记点击前", command=self._pkti_mark_before).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(row_i1b, text="只看点击后", command=self._pkti_show_after).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(row_i1b, text="显示全部", command=self._pkti_show_all).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(row_i1b, text="普通包：切到网络捕获", command=self._pkti_prepare_network_capture).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        list_i = ttk.Frame(pki)
        list_i.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.pkti_list = tk.Listbox(
            list_i,
            height=9,
            font=("Consolas", 9),
            activestyle="dotbox",
        )
        sb_i = ttk.Scrollbar(list_i, orient=tk.VERTICAL, command=self.pkti_list.yview)
        self.pkti_list.configure(yscrollcommand=sb_i.set)
        self.pkti_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_i.pack(side=tk.RIGHT, fill=tk.Y)
        self.pkti_list.bind("<<ListboxSelect>>", self._pkti_on_select)

        row_i2 = ttk.Frame(pki)
        row_i2.pack(fill=tk.X, pady=(6, 0))
        self.var_pkti = tk.StringVar(
            value="拦截 CD1740 出口包（字节在入口处捕获，原始发送缓冲可复用/重发）；"
                  "选中一条 → 重发选中 走游戏内 CD1740 发送"
        )
        ttk.Label(
            row_i2, textvariable=self.var_pkti, style="Mono.TLabel", wraplength=900
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        detail_i = ttk.Frame(pki)
        detail_i.pack(fill=tk.X, pady=(6, 0))
        self.var_pkti_detail = tk.StringVar(value="选中一条封包查看解析标注")
        ttk.Label(
            detail_i, textvariable=self.var_pkti_detail, style="Mono.TLabel",
            wraplength=900, justify=tk.LEFT,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._pkti_state: InterceptState | None = None
        self._pkti_records: list[dict] = []
        self._pkti_after_id: str | None = None
        self._pkti_recording_path: Path | None = None
        self._pkti_replay_stop = threading.Event()
        self._pkti_mark_index = 0
        self._pkti_view_start = 0

        # default log path if present
        try:
            from packet.auto_analyze import latest_log

            lp = latest_log()
            if lp:
                self.pkt_log_path.set(str(lp))
        except Exception:
            pass
        try:
            self._pkt_refresh_core()
        except Exception:
            pass
        try:
            _on_cfg()
        except Exception:
            pass
        try:
            self.after(250, _on_cfg)
        except Exception:
            pass

    def _build_tab_lab(self, parent: ttk.Frame) -> None:
        """
        Dev lab: knockback + skill action-lock probes; page scrolls.

        @author by ak
        """
        # Scrollable body (lab content is tall)
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(wrap, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        body = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=body, anchor="nw")

        def _on_body_cfg(_e=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_cfg(e) -> None:
            try:
                canvas.itemconfigure(win_id, width=e.width)
            except Exception:
                pass

        body.bind("<Configure>", _on_body_cfg)
        canvas.bind("<Configure>", _on_canvas_cfg)

        def _wheel(e) -> None:
            # Windows / trackpad
            delta = int(getattr(e, "delta", 0) or 0)
            if delta:
                canvas.yview_scroll(int(-1 * (delta / 120)), "units")

        def _bind_wheel(_e=None) -> None:
            canvas.bind_all("<MouseWheel>", _wheel)

        def _unbind_wheel(_e=None) -> None:
            try:
                canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass

        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)
        self._lab_canvas = canvas

        ttk.Label(
            body,
            text=(
                "实验页可滚动。仅探针，不写永久击飞免疫/去后摇 hook。\n"
                "技能目标：放完站着播完才能再放（后摇/动作锁）。"
                "击飞：采 A→被打→采 B。动作锁：闲置采 A → 放技能立刻采 B → 比内存。"
            ),
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6), padx=2)

        input_limit = ttk.LabelFrame(
            body, text="心法资质输入拦截（Win_InputNO 实验）", padding=6
        )
        input_limit.pack(fill=tk.X, pady=(0, 8), padx=2)
        input_row = ttk.Frame(input_limit)
        input_row.pack(fill=tk.X)
        ttk.Button(
            input_row, text="读取当前弹窗", command=self._lab_probe_input_limit
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            input_row,
            text="最大=剩余资质",
            command=self._lab_patch_input_limit,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            input_row, text="恢复原上限", command=self._lab_restore_input_limit
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.var_lab_input_limit_status = tk.StringVar(value="等待读取")
        ttk.Label(
            input_row,
            textvariable=self.var_lab_input_limit_status,
            style="Mono.TLabel",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        wall = ttk.LabelFrame(body, text="穿墙开关（0x86829D JNZ↔JE）", padding=6)
        wall.pack(fill=tk.X, pady=(0, 8), padx=2)
        wall_row = ttk.Frame(wall)
        wall_row.pack(fill=tk.X)
        ttk.Button(wall_row, text="穿墙 开", command=self._lab_wall_on).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(wall_row, text="穿墙 关", command=self._lab_wall_off).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(wall_row, text="读取状态", command=self._lab_wall_read).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        self.var_lab_wall_status = tk.StringVar(value="等待读取")
        ttk.Label(
            wall_row,
            textvariable=self.var_lab_wall_status,
            style="Mono.TLabel",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        wall_gate = tk.Text(
            wall, height=3, wrap="char", bd=0, highlightthickness=0, relief="flat"
        )
        wall_gate.pack(fill=tk.X, pady=(4, 0))

        def _wg_btn(text: str, command, sep: str = "  "):
            b = ttk.Button(wall_gate, text=text, command=command)
            wall_gate.window_create("end", window=b)
            wall_gate.insert("end", sep)
            wall_gate.configure(state="disabled")

        _wg_btn("区域闸门开(0x86828B)", lambda: self._lab_gate_set("region", True))
        _wg_btn("区域闸门关", lambda: self._lab_gate_set("region", False))
        _wg_btn("地面判定开(0x86834B)", lambda: self._lab_gate_set("ground", True))
        _wg_btn("地面判定关", lambda: self._lab_gate_set("ground", False))
        _wg_btn("地面位置开(0x8681FF)", lambda: self._lab_gate_set("ground_pos", True))
        _wg_btn("地面位置关", lambda: self._lab_gate_set("ground_pos", False))
        _wg_btn("读取闸门状态", self._lab_gate_read, sep="")

        tp = ttk.LabelFrame(body, text="宿主坐标直写（绕过移动判定）", padding=6)
        tp.pack(fill=tk.X, pady=(0, 8), padx=2)
        tp_row = ttk.Frame(tp)
        tp_row.pack(fill=tk.X)
        ttk.Label(tp_row, text="X").pack(side=tk.LEFT)
        self.var_lab_tp_x = tk.StringVar(value="")
        ttk.Entry(tp_row, textvariable=self.var_lab_tp_x, width=10).pack(
            side=tk.LEFT, padx=(2, 6)
        )
        ttk.Label(tp_row, text="Y").pack(side=tk.LEFT)
        self.var_lab_tp_y = tk.StringVar(value="")
        ttk.Entry(tp_row, textvariable=self.var_lab_tp_y, width=10).pack(
            side=tk.LEFT, padx=(2, 6)
        )
        ttk.Label(tp_row, text="Z").pack(side=tk.LEFT)
        self.var_lab_tp_z = tk.StringVar(value="")
        ttk.Entry(tp_row, textvariable=self.var_lab_tp_z, width=10).pack(
            side=tk.LEFT, padx=(2, 6)
        )
        ttk.Button(
            tp_row,
            text="读取当前坐标",
            command=self._lab_tp_read,
        ).pack(side=tk.LEFT, padx=(4, 6))
        ttk.Button(
            tp_row,
            text="传送(写host+0x158)",
            command=self._lab_tp_go,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(tp_row, text="距离").pack(side=tk.LEFT)
        self.var_lab_tp_dist = tk.StringVar(value="40")
        ttk.Entry(tp_row, textvariable=self.var_lab_tp_dist, width=6).pack(
            side=tk.LEFT, padx=(2, 4)
        )
        ttk.Button(
            tp_row,
            text="朝朝向寻路移动",
            command=self._lab_tp_facing,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            tp_row,
            text="自动移动 开",
            command=self._lab_tp_auto_toggle,
        ).pack(side=tk.LEFT, padx=(0, 6))
        self.var_lab_tp_auto = tk.StringVar(value="自动移动: 关")
        ttk.Label(
            tp_row,
            textvariable=self.var_lab_tp_auto,
            style="Mono.TLabel",
        ).pack(side=tk.LEFT, padx=(0, 6))
        self.var_lab_tp_status = tk.StringVar(value="等待")
        ttk.Label(
            tp_row,
            textvariable=self.var_lab_tp_status,
            style="Mono.TLabel",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        sutra_level = ttk.LabelFrame(
            body, text="心法等级 200 前端拦截（升级请求实验）", padding=6
        )
        sutra_level.pack(fill=tk.X, pady=(0, 8), padx=2)
        sutra_level_row = ttk.Frame(sutra_level)
        sutra_level_row.pack(fill=tk.X)
        ttk.Button(
            sutra_level_row,
            text="读取等级分支",
            command=self._lab_probe_sutra_level_cap,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            sutra_level_row,
            text="解除等级200上限",
            command=self._lab_patch_sutra_level_cap,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            sutra_level_row,
            text="恢复等级上限",
            command=self._lab_restore_sutra_level_cap,
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.var_lab_sutra_level_cap_status = tk.StringVar(value="等待读取")
        ttk.Label(
            sutra_level_row,
            textvariable=self.var_lab_sutra_level_cap_status,
            style="Mono.TLabel",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # --- hang / 内挂 state probe (mem primary + Alt+R + mode/radius RE) ---
        hg = ttk.LabelFrame(
            body,
            text="挂机 / 内挂（CECAutoPlay：+0x08开/关 · +0x1C模式 · +0x21半径 · 技能门/九槽）",
            padding=6,
        )
        hg.pack(fill=tk.X, pady=(0, 8), padx=2)
        hg_row = ttk.Frame(hg)
        hg_row.pack(fill=tk.X)
        ttk.Button(
            hg_row,
            text="读内存状态",
            command=self._lab_probe_hang,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            hg_row,
            text="Alt+R 切换",
            command=self._lab_toggle_hang_altr,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            hg_row,
            text="强开(无视技能)",
            command=self._lab_force_hang_on,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            hg_row,
            text="强关",
            command=self._lab_force_hang_off,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            hg_row,
            text="切换后再读",
            command=self._lab_toggle_hang_altr_and_probe,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            hg_row,
            text="列出相关UI",
            command=self._lab_list_hang_ui,
        ).pack(side=tk.LEFT, padx=(0, 6))
        self.var_lab_hang = tk.StringVar(value="挂机=未检测")
        ttk.Label(
            hg_row,
            textvariable=self.var_lab_hang,
            style="Mono.TLabel",
        ).pack(side=tk.LEFT, padx=(8, 0))

        # mode / radius controls (RE 2026-07-23)
        hg_cfg = ttk.Frame(hg)
        hg_cfg.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(hg_cfg, text="模式").pack(side=tk.LEFT)
        self.var_lab_hang_mode = tk.StringVar(value="1 副本模式")
        self.cmb_lab_hang_mode = ttk.Combobox(
            hg_cfg,
            textvariable=self.var_lab_hang_mode,
            values=("0 普通模式", "1 副本模式"),
            width=12,
            state="readonly",
        )
        self.cmb_lab_hang_mode.pack(side=tk.LEFT, padx=(4, 8))
        ttk.Button(
            hg_cfg,
            text="应用模式",
            command=self._lab_apply_hang_mode,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_cfg,
            text="→普通",
            command=lambda: self._lab_apply_hang_mode(0),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_cfg,
            text="→副本",
            command=lambda: self._lab_apply_hang_mode(1),
        ).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(hg_cfg, text="半径").pack(side=tk.LEFT)
        self.var_lab_hang_radius = tk.StringVar(value="1")
        ttk.Entry(hg_cfg, textvariable=self.var_lab_hang_radius, width=5).pack(
            side=tk.LEFT, padx=(4, 4)
        )
        ttk.Button(
            hg_cfg,
            text="应用半径",
            command=self._lab_apply_hang_radius,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_cfg,
            text="半径=1",
            command=lambda: self._lab_apply_hang_radius(1),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_cfg,
            text="半径=5",
            command=lambda: self._lab_apply_hang_radius(5),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_cfg,
            text="半径=10",
            command=lambda: self._lab_apply_hang_radius(10),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_cfg,
            text="半径UI下限→1",
            command=self._lab_patch_hang_radius_ui,
        ).pack(side=tk.LEFT, padx=(0, 4))

        # skill gate / slots (RE 2026-07-23)
        hg_sk = ttk.Frame(hg)
        hg_sk.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(hg_sk, text="技能ID").pack(side=tk.LEFT)
        self.var_lab_hang_skills = tk.StringVar(value="")
        ttk.Entry(hg_sk, textvariable=self.var_lab_hang_skills, width=36).pack(
            side=tk.LEFT, padx=(4, 6)
        )
        ttk.Button(
            hg_sk,
            text="读技能门",
            command=self._lab_probe_hang_skills,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_sk,
            text="一键灌技能",
            command=self._lab_fill_hang_skills,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_sk,
            text="清空技能",
            command=self._lab_clear_hang_skills,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_sk,
            text="开挂预检",
            command=self._lab_probe_hang_gate,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(
            hg,
            text="技能ID：逗号/空格分隔，支持 0x。灌技能空=复用已有；「清空技能」写成全 0（门不过，tip 不能开挂）。",
        ).pack(anchor="w", pady=(2, 0))

        # recover / bag force-use lab (RE 2026-07-23 CheckHPItem + UseItem)
        hg_rc = ttk.Frame(hg)
        hg_rc.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(hg_rc, text="道具关键字").pack(side=tk.LEFT)
        self.var_lab_hang_item_key = tk.StringVar(value="伤害物品")
        ttk.Entry(hg_rc, textvariable=self.var_lab_hang_item_key, width=16).pack(
            side=tk.LEFT, padx=(4, 6)
        )
        ttk.Label(hg_rc, text="恢复格(1-4)").pack(side=tk.LEFT)
        self.var_lab_hang_recover_slot = tk.StringVar(value="3")  # 1-based：第3格=攻击药丸
        ttk.Entry(hg_rc, textvariable=self.var_lab_hang_recover_slot, width=3).pack(
            side=tk.LEFT, padx=(4, 6)
        )
        ttk.Button(
            hg_rc,
            text="读恢复槽",
            command=self._lab_probe_hang_recover,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_rc,
            text="搜背包",
            command=self._lab_search_hang_bag_item,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_rc,
            text="强制使用一次",
            command=self._lab_force_use_hang_bag_item,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_rc,
            text="写入恢复槽",
            command=self._lab_write_hang_recover_item,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_rc,
            text="tid→技能槽对照",
            command=self._lab_inject_item_tid_to_skill,
        ).pack(side=tk.LEFT, padx=(0, 4))

        # recover check interval (RE: +0x274 HP / +0x294 MP, default 1000ms)
        hg_iv = ttk.Frame(hg)
        hg_iv.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(hg_iv, text="恢复间隔ms").pack(side=tk.LEFT)
        self.var_lab_hang_recover_iv = tk.StringVar(value="500")
        ttk.Entry(hg_iv, textvariable=self.var_lab_hang_recover_iv, width=6).pack(
            side=tk.LEFT, padx=(4, 4)
        )
        ttk.Button(
            hg_iv,
            text="读间隔",
            command=self._lab_probe_hang_recover_iv,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_iv,
            text="应用间隔",
            command=self._lab_apply_hang_recover_iv,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_iv,
            text="200",
            command=lambda: self._lab_apply_hang_recover_iv(200),
        ).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Button(
            hg_iv,
            text="500",
            command=lambda: self._lab_apply_hang_recover_iv(500),
        ).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Button(
            hg_iv,
            text="1000",
            command=lambda: self._lab_apply_hang_recover_iv(1000),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(
            hg,
            text=(
                "道具实验：搜背包→「强制使用一次」(UseItem，推荐路径)；"
                "「写入恢复」：攻击药丸默认第3格；血/蓝药用第1/2格；"
                "「tid→技能槽」是负对照。"
            ),
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            hg,
            text=(
                "恢复间隔：HP +0x274 / MP +0x294（默认 1000ms）。"
                "改小=更勤检测吃药；过低受药 CD/阈值限制，收益有限。"
            ),
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(2, 0))

        # recover trigger percent per grid (RE +0x99/+0x9A/+0x9B/+0x9C)
        hg_pct = ttk.Frame(hg)
        hg_pct.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(hg_pct, text="触发%").pack(side=tk.LEFT)
        self.var_lab_hang_recover_pct = tk.StringVar(value="100")
        ttk.Entry(hg_pct, textvariable=self.var_lab_hang_recover_pct, width=5).pack(
            side=tk.LEFT, padx=(4, 4)
        )
        ttk.Label(hg_pct, text="应用到格").pack(side=tk.LEFT)
        # reuse 恢复格 var (1-4)
        ttk.Button(
            hg_pct,
            text="读阈值",
            command=self._lab_probe_hang_recover_pct,
        ).pack(side=tk.LEFT, padx=(6, 4))
        ttk.Button(
            hg_pct,
            text="应用阈值",
            command=self._lab_apply_hang_recover_pct,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            hg_pct,
            text="100%",
            command=lambda: self._lab_apply_hang_recover_pct(100),
        ).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Button(
            hg_pct,
            text="80%",
            command=lambda: self._lab_apply_hang_recover_pct(80),
        ).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Button(
            hg_pct,
            text="50%",
            command=lambda: self._lab_apply_hang_recover_pct(50),
        ).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Button(
            hg_pct,
            text="40%",
            command=lambda: self._lab_apply_hang_recover_pct(40),
        ).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Label(
            hg,
            text=(
                "触发%：血/蓝低于该值才用对应格。格1→HP药(+0x99) 格2→MP药(+0x9A) "
                "格3→攻击/替换(+0x9B) 格4→MP替换(+0x9C)。100%=未满就尝试。"
            ),
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(2, 0))

        stat_row = ttk.Frame(hg)
        stat_row.pack(fill=tk.X, pady=(6, 0))
        self.btn_lab_damage_stat = ttk.Button(
            stat_row,
            text="开始统计 60 秒",
            command=self._lab_start_damage_item_stat,
        )
        self.btn_lab_damage_stat.pack(side=tk.LEFT, padx=(0, 8))
        self.var_lab_damage_stat = tk.StringVar(value="伤害物品统计：待开始")
        ttk.Label(
            stat_row,
            textvariable=self.var_lab_damage_stat,
            style="Mono.TLabel",
            wraplength=650,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        dummy_row = ttk.Frame(hg)
        dummy_row.pack(fill=tk.X, pady=(6, 0))
        self.btn_lab_dummy_damage = ttk.Button(
            dummy_row,
            text="统计木人桩 60 秒伤害",
            command=self._lab_start_dummy_damage_stat,
        )
        self.btn_lab_dummy_damage.pack(side=tk.LEFT, padx=(0, 8))
        self.var_lab_dummy_damage = tk.StringVar(value="木人桩伤害：待开始")
        ttk.Label(
            dummy_row,
            textvariable=self.var_lab_dummy_damage,
            style="Mono.TLabel",
            wraplength=650,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.txt_lab_hang = tk.Text(hg, height=12, wrap=tk.WORD, font=("Consolas", 9))
        self.txt_lab_hang.pack(fill=tk.X, pady=(6, 0))
        self.txt_lab_hang.insert(
            "1.0",
            "主路径：CECAutoPlay 内存（与正式自动副本相同）。\n"
            "  +0x08 开/关 · +0x1C 模式(0普通/1副本) · +0x21 范围半径\n"
            "  技能：+0x2B/+0x2F 门控 · +0x33 九槽(id+interval)\n"
            "开关挂机统一发送服务端控制封包。模式/半径/技能可内存写入。\n"
            "半径=1：先点「半径UI下限→1」再设半径，避免打开设置页被钳回 5。\n"
            "开挂封包不校验本地技能门。\n"
            "空技能开挂：点「强开(无视技能)」也发送开挂封包。\n"
            "「清空技能」= 全 0；灌技能仅影响战斗输出，不是强开前提。\n"
            "恢复槽 +0x85 flags / +0x89 item_id：CheckHPItem 血蓝药；伤害道具请用「强制使用一次」。\n"
            "恢复频率：HP iv@+0x274 / MP iv@+0x294（ms，默认1000）。\n""触发%：格1 +0x99 / 格2 +0x9A / 格3 +0x9B / 格4 +0x9C（1~100，100=未满即用）。\n",
        )
        self.txt_lab_hang.configure(state=tk.DISABLED)




        # --- team follow probe (captain Btn_TeamFollow / c2s 0x1286) ---
        tf = ttk.LabelFrame(
            body,
            text="组队跟随（队长侧真实 UI 回调 · c2s 0x1286）",
            padding=6,
        )
        tf.pack(fill=tk.X, pady=(0, 8), padx=2)
        tf_probe_row = ttk.Frame(tf)
        tf_probe_row.pack(fill=tk.X)
        ttk.Button(
            tf_probe_row,
            text="读队伍/角色",
            command=self._lab_probe_team_follow,
        ).pack(side=tk.LEFT, padx=(0, 6))
        self.var_lab_team_follow = tk.StringVar(value="组队跟随=未检测")
        ttk.Label(
            tf_probe_row,
            textvariable=self.var_lab_team_follow,
            style="Mono.TLabel",
        ).pack(side=tk.LEFT, padx=(8, 0))

        tf_ui_row = ttk.Frame(tf)
        tf_ui_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(tf_ui_row, text="真实 UI：", width=10).pack(side=tk.LEFT)
        ttk.Button(
            tf_ui_row,
            text="组队跟随",
            command=lambda: self._lab_set_team_follow(True),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            tf_ui_row,
            text="取消跟随",
            command=lambda: self._lab_set_team_follow(False),
        ).pack(side=tk.LEFT, padx=(0, 6))
        self.txt_lab_team_follow = tk.Text(
            tf, height=10, wrap=tk.WORD, font=("Consolas", 9)
        )
        self.txt_lab_team_follow.pack(fill=tk.X, pady=(6, 0))
        self.txt_lab_team_follow.insert(
            "1.0",
            "用途：单独验证队长「组队跟随」发包是否真生效。\n"
            "真实 UI 开启：打开 Game_TeamFollow，并后台点击确定。\n"
            "真实 UI 取消：点击右下角 Win_BindStatus 中「组队跟随」行的退出按钮。\n"
            "UI 点击失败会直接报告失败，不执行其他路径。\n"
            "点击位置来自游戏控件的实时矩形，可适配窗口尺寸和 UI 布局变化。\n",
        )
        self.txt_lab_team_follow.configure(state=tk.DISABLED)

        # --- knockback / state probe ---
        kb = ttk.LabelFrame(body, text="击飞 / 控制态探针", padding=6)
        kb.pack(fill=tk.X, pady=(0, 8), padx=2)

        row = ttk.Frame(kb)
        row.pack(fill=tk.X)
        ttk.Button(row, text="采样 A（前）", command=lambda: self._lab_sample("A")).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(row, text="采样 B（后）", command=lambda: self._lab_sample("B")).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(row, text="对比 A/B", command=self._lab_diff).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(row, text="单次快照", command=self._lab_once).pack(side=tk.LEFT, padx=(0, 4))

        row2 = ttk.Frame(kb)
        row2.pack(fill=tk.X, pady=(4, 0))
        self.lab_poll = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row2,
            text="轮询 500ms（states/坐标）",
            variable=self.lab_poll,
            command=self._lab_toggle_poll,
        ).pack(side=tk.LEFT)
        ttk.Button(row2, text="清空 A/B", command=self._lab_clear).pack(side=tk.LEFT, padx=(10, 0))

        self.var_lab_a = tk.StringVar(value="A: -")
        self.var_lab_b = tk.StringVar(value="B: -")
        self.var_lab_diff = tk.StringVar(value="diff: -")
        self.var_lab_live = tk.StringVar(value="live: -")
        ttk.Label(kb, textvariable=self.var_lab_a, style="Mono.TLabel", wraplength=840).pack(
            anchor="w", pady=(4, 0)
        )
        ttk.Label(kb, textvariable=self.var_lab_b, style="Mono.TLabel", wraplength=840).pack(
            anchor="w"
        )
        ttk.Label(kb, textvariable=self.var_lab_diff, style="Mono.TLabel", wraplength=840).pack(
            anchor="w"
        )
        ttk.Label(kb, textvariable=self.var_lab_live, style="Mono.TLabel", wraplength=840).pack(
            anchor="w"
        )

        # --- skill action lock / recovery ---
        sk = ttk.LabelFrame(
            body, text="技能后摇 / 动作锁（放完站着，播完才能再放）", padding=6
        )
        sk.pack(fill=tk.X, pady=(0, 8), padx=2)

        ttk.Label(
            sk,
            text=(
                "无 CD ≠ 能连放。卡点是当前 SkillSequence 的 PerfTime/动画未结束。\n"
                "推荐：站桩「闲置采 A」→ 点技能 → 立刻「连采施法」(约1.6s 自动抓变化) → 看报告。\n"
                "也会 dump host + 嵌套指针。去后摇 hook 未实现。"
            ),
            wraplength=840,
            justify=tk.LEFT,
        ).pack(anchor="w")

        row_sk = ttk.Frame(sk)
        row_sk.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(row_sk, text="读技能指针", command=self._lab_skill_ptrs).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(
            row_sk, text="闲置采 A（含 dump）", command=lambda: self._lab_sample("A", dumps=True)
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_sk, text="施法中采 B（含 dump）", command=lambda: self._lab_sample("B", dumps=True)
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(row_sk, text="连采施法(≈1.6s)", command=self._lab_burst_cast).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(row_sk, text="对比内存", command=self._lab_diff_dumps).pack(
            side=tk.LEFT, padx=(0, 4)
        )

        self.var_lab_skill = tk.StringVar(value="skill: -")
        ttk.Label(
            sk, textvariable=self.var_lab_skill, style="Mono.TLabel", wraplength=840
        ).pack(anchor="w", pady=(4, 0))

        dump_row = ttk.Frame(sk)
        dump_row.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        left = ttk.LabelFrame(dump_row, text="skill 对象 dump（dword）", padding=4)
        right = ttk.LabelFrame(dump_row, text="seq 对象 dump（dword）", padding=4)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.txt_lab_skill_dump = tk.Text(
            left, height=12, width=42, font=("Consolas", 8), wrap=tk.NONE
        )
        self.txt_lab_seq_dump = tk.Text(
            right, height=12, width=42, font=("Consolas", 8), wrap=tk.NONE
        )
        sy1 = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.txt_lab_skill_dump.yview)
        sy2 = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.txt_lab_seq_dump.yview)
        self.txt_lab_skill_dump.configure(yscrollcommand=sy1.set)
        self.txt_lab_seq_dump.configure(yscrollcommand=sy2.set)
        sy1.pack(side=tk.RIGHT, fill=tk.Y)
        sy2.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_lab_skill_dump.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.txt_lab_seq_dump.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.txt_lab_skill_dump.insert("1.0", "(先采 A 或 B 且勾 dump)")
        self.txt_lab_seq_dump.insert("1.0", "(先采 A 或 B 且勾 dump)")
        self.txt_lab_skill_dump.configure(state=tk.DISABLED)
        self.txt_lab_seq_dump.configure(state=tk.DISABLED)

        self.var_lab_dump_diff = tk.StringVar(value="内存对比: -")
        ttk.Label(
            sk, textvariable=self.var_lab_dump_diff, style="Mono.TLabel", wraplength=840
        ).pack(anchor="w", pady=(4, 0))

        flags = ttk.Frame(sk)
        flags.pack(fill=tk.X, pady=(8, 0))
        self.lab_flag_kb = tk.BooleanVar(value=False)
        self.lab_flag_cast = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            flags,
            text="计划：吞 host SmallCtrlState（击飞，未实现）",
            variable=self.lab_flag_kb,
            state=tk.DISABLED,
        ).pack(anchor="w")
        ttk.Checkbutton(
            flags,
            text="计划：PerfTime/后摇=0 或强制 CanCastNextUnit（未实现）",
            variable=self.lab_flag_cast,
            state=tk.DISABLED,
        ).pack(anchor="w")
        ttk.Label(
            flags,
            text="上两项仅占位。先用内存对比找变化偏移，再谈 hook。",
            wraplength=840,
        ).pack(anchor="w", pady=(4, 0))

        # --- cast-this (0x1A88 / notes 0x755F10 path) ---
        ct = ttk.LabelFrame(
            body,
            text="施法 this 探针（host+0x1A88 / 笔记 0x755F10 入口）",
            padding=6,
        )
        ct.pack(fill=tk.X, pady=(0, 8), padx=2)
        ttk.Label(
            ct,
            text=(
                "只读 [host+0x1A88]/[host+0x1A84] 与真实施法调用。"
                "写入、自动循环与猜测式重放入口已下线。"
            ),
            wraplength=840,
            justify=tk.LEFT,
        ).pack(anchor="w")
        row_ct = ttk.Frame(ct)
        row_ct.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(row_ct, text="解析 cast-this", command=self._lab_cast_resolve).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(row_ct, text="闲置采 cast A", command=self._lab_cast_sample_a).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(row_ct, text="连采 cast(≈2.4s)", command=self._lab_cast_burst).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(row_ct, text="时间线(≈3s)", command=self._lab_cast_timeline).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(row_ct, text="对比 cast", command=self._lab_cast_diff).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        row_trace = ttk.Frame(ct)
        row_trace.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row_trace, text="动作路径对照:").pack(side=tk.LEFT, padx=(0, 6))
        self.var_lab_skill_trace_scenario = tk.StringVar(
            value=SKILL_TRACE_SCENARIO_LABELS[SCENARIO_SAME_SKILL]
        )
        ttk.Combobox(
            row_trace,
            textvariable=self.var_lab_skill_trace_scenario,
            values=tuple(SKILL_TRACE_SCENARIO_LABELS.values()),
            width=30,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_trace,
            text="开始一次对照",
            command=self._lab_skill_action_trace_once,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(
            row_trace,
            text="短时记录真实动作请求 / 技能会话，结束自动卸载 Hook。",
            style="Panel.Muted.TLabel",
            wraplength=360,
        ).pack(side=tk.LEFT)
        row_trace_quick = ttk.Frame(ct)
        row_trace_quick.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row_trace_quick, text="快捷实验:").pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(
            row_trace_quick,
            text="稳定去后摇（一次）",
            command=self._lab_skill_stable_recovery_once,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_trace_quick,
            text="蓄力/持续去后摇（一次）",
            command=self._lab_skill_charged_recovery_once,
        ).pack(side=tk.LEFT, padx=(0, 4))
        self.var_lab_youfeng_chain = tk.StringVar(value="有凤宏对照: OFF")
        ttk.Button(
            row_trace_quick,
            textvariable=self.var_lab_youfeng_chain,
            command=self._lab_youfeng_chain_toggle,
        ).pack(side=tk.LEFT, padx=(0, 4))
        self.var_lab_youfeng_internal = tk.StringVar(value="有凤E07对照: OFF")
        ttk.Button(
            row_trace_quick,
            textvariable=self.var_lab_youfeng_internal,
            command=self._lab_youfeng_internal_toggle,
        ).pack(side=tk.LEFT, padx=(0, 4))
        row_youfeng_gate = ttk.Frame(ct)
        row_youfeng_gate.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row_youfeng_gate, text="原生抢招:").pack(
            side=tk.LEFT, padx=(0, 6)
        )
        self.var_lab_youfeng_gate = tk.StringVar(value="有凤轻功抢招: OFF")
        ttk.Button(
            row_youfeng_gate,
            textvariable=self.var_lab_youfeng_gate,
            command=self._lab_youfeng_gate_toggle,
        ).pack(side=tk.LEFT, padx=(0, 4))
        self.var_lab_youfeng_ultimate = tk.StringVar(value="真绝+普通循环: OFF")
        ttk.Button(
            row_youfeng_gate,
            textvariable=self.var_lab_youfeng_ultimate,
            command=self._lab_youfeng_ultimate_toggle,
        ).pack(side=tk.LEFT, padx=(0, 4))
        self.var_lab_youfeng_ultimate_spam = tk.StringVar(
            value="真绝无后摇连发: OFF"
        )
        ttk.Button(
            row_youfeng_gate,
            textvariable=self.var_lab_youfeng_ultimate_spam,
            command=self._lab_youfeng_ultimate_spam_toggle,
        ).pack(side=tk.LEFT, padx=(0, 4))
        # Big live prompt in this panel (user does not watch bottom log).
        self.var_lab_recovery_prompt = tk.StringVar(
            value="动作跟踪: 选择场景后点「开始一次对照」，按本行提示操作"
        )
        ttk.Label(
            ct,
            textvariable=self.var_lab_recovery_prompt,
            style="Mono.TLabel",
            wraplength=900,
            foreground="#c9a227",
        ).pack(anchor="w", pady=(4, 0))
        self.var_lab_cast = tk.StringVar(value="cast-this: -")
        ttk.Label(
            ct, textvariable=self.var_lab_cast, style="Mono.TLabel", wraplength=840
        ).pack(anchor="w", pady=(4, 0))
        self.var_lab_cast_diff = tk.StringVar(value="cast diff: -")
        ttk.Label(
            ct, textvariable=self.var_lab_cast_diff, style="Mono.TLabel", wraplength=840
        ).pack(anchor="w")
        self.txt_lab_cast_dump = tk.Text(
            ct, height=10, width=90, font=("Consolas", 8), wrap=tk.NONE
        )
        sy_ct = ttk.Scrollbar(ct, orient=tk.VERTICAL, command=self.txt_lab_cast_dump.yview)
        self.txt_lab_cast_dump.configure(yscrollcommand=sy_ct.set)
        sy_ct.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_lab_cast_dump.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.txt_lab_cast_dump.insert("1.0", "(点「解析 cast-this」或「闲置采 cast A」)")
        self.txt_lab_cast_dump.configure(state=tk.DISABLED)


        # --- solution-2 KEY_HOLD (process-local hooks, no focus) ---
        kh = ttk.LabelFrame(
            body,
            text="后台按键 · 解法2 KEY_HOLD（进程内 Hook · 默认无 SoftSend · 不抢焦点）",
            padding=6,
        )
        kh.pack(fill=tk.X, pady=(0, 8), padx=2)
        ttk.Label(
            kh,
            text=(
                "原理：IAT hook GetAsyncKeyState/GetKeyState + inline IsKeyTable/GetModMask，\n"
                "进程内伪造键态；默认不 SendInput，前台应用不应被脏键。需先 Delete 注入本机构建桥接。\n"
                "验收：note 中 hooks=1、view 高位、tab=1；softsend 关闭时 real 可仍为 0000。"
            ),
            wraplength=840,
            justify=tk.LEFT,
        ).pack(anchor="w")
        row_kh = ttk.Frame(kh)
        row_kh.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row_kh, text="VK").pack(side=tk.LEFT)
        self.var_lab_hold_vk = tk.StringVar(value="Space")
        ttk.Entry(row_kh, textvariable=self.var_lab_hold_vk, width=10).pack(
            side=tk.LEFT, padx=(4, 8)
        )
        self.var_lab_hold_soft = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row_kh, text="允许 SoftSend(污染前台)", variable=self.var_lab_hold_soft
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(row_kh, text="安装Hook/状态", command=self._lab_hold_install).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(row_kh, text="测一次(ON/HOLD/OFF)", command=self._lab_hold_probe).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        row_kh2 = ttk.Frame(kh)
        row_kh2.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(row_kh2, text="HOLD 开始", command=self._lab_hold_start).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(row_kh2, text="HOLD 停止", command=self._lab_hold_stop).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(row_kh2, text="清空全部 force", command=self._lab_hold_clear).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        self.var_lab_hold = tk.StringVar(value="KEY_HOLD: 空闲")
        ttk.Label(
            kh, textvariable=self.var_lab_hold, style="Mono.TLabel", wraplength=840
        ).pack(anchor="w", pady=(4, 0))

        # --- reticle / free-aim probe (beyond KEY_FORCE) ---
        rt = ttk.LabelFrame(
            body,
            text="准星 / 自由瞄准（研究中 · 未产品化 · 键层≠可见准星）",
            padding=6,
        )
        rt.pack(fill=tk.X, pady=(0, 8), padx=2)
        ttk.Label(
            rt,
            text=(
                "准星状态在 aimBuf(host+0x1A10) + ctrl 标志，不是光标 ch2。\n"
                "手按对照：A+5s采B。假键对照：KEY_FORCE 或 HOLD Shift+采B(无Soft)。\n"
                "测假键时双手离开 Shift；测完 FORCE OFF / HOLD OFF。单开客户端。\n"
                "验收：aim_f*/aim_u28 变 + 你确认屏幕准星（不只看日志）。"
            ),
            wraplength=840,
            justify=tk.LEFT,
        ).pack(anchor="w")
        row_rt = ttk.Frame(rt)
        row_rt.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(row_rt, text="采 A（闲置）", command=lambda: self._lab_ret_sample("A")).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(
            row_rt,
            text="A+5s采B（推荐）",
            command=lambda: self._lab_ret_ab_delayed(5.0),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_rt,
            text="5秒后采B",
            command=lambda: self._lab_ret_delay_b(5.0),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_rt,
            text="等Shift自动采B",
            command=self._lab_ret_auto_b,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_rt, text="采 B（立即）", command=lambda: self._lab_ret_sample("B")
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(row_rt, text="对比 A/B", command=self._lab_ret_diff).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(row_rt, text="单次快照", command=self._lab_ret_once).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        row_rt2 = ttk.Frame(rt)
        row_rt2.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(
            row_rt2, text="KEY_FORCE ON+采 B", command=lambda: self._lab_ret_force(True)
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_rt2, text="KEY_FORCE OFF+采", command=lambda: self._lab_ret_force(False)
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_rt2,
            text="HOLD Shift+采B(无Soft)",
            command=lambda: self._lab_ret_hold_shift(True),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_rt2,
            text="HOLD OFF",
            command=lambda: self._lab_ret_hold_shift(False),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(row_rt2, text="清空 A/B", command=self._lab_ret_clear).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        self.lab_ret_poll = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row_rt2,
            text="轮询 400ms",
            variable=self.lab_ret_poll,
            command=self._lab_ret_toggle_poll,
        ).pack(side=tk.LEFT, padx=(8, 0))
        row_rt3 = ttk.Frame(rt)
        row_rt3.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(
            row_rt3,
            text="DIAG ON (消息钩)",
            command=lambda: self._lab_ret_diag(True),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_rt3,
            text="DIAG OFF (看结果)",
            command=lambda: self._lab_ret_diag(False),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_rt3,
            text="CTRL 快照",
            command=self._lab_ret_ctrl_snapshot,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_rt3,
            text="POLL/AIM 5s",
            command=self._lab_ret_poll_capture,
        ).pack(side=tk.LEFT, padx=(0, 4))
        self.var_lab_ret_a = tk.StringVar(value="retA: -")
        self.var_lab_ret_b = tk.StringVar(value="retB: -")
        self.var_lab_ret_diff = tk.StringVar(value="retDiff: -")
        self.var_lab_ret_live = tk.StringVar(value="retLive: -")
        ttk.Label(
            rt, textvariable=self.var_lab_ret_a, style="Mono.TLabel", wraplength=840
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            rt, textvariable=self.var_lab_ret_b, style="Mono.TLabel", wraplength=840
        ).pack(anchor="w")
        ttk.Label(
            rt, textvariable=self.var_lab_ret_diff, style="Mono.TLabel", wraplength=840
        ).pack(anchor="w")
        ttk.Label(
            rt, textvariable=self.var_lab_ret_live, style="Mono.TLabel", wraplength=840
        ).pack(anchor="w")
        self.txt_lab_ret_dump = tk.Text(
            rt, height=12, width=90, font=("Consolas", 8), wrap=tk.NONE
        )
        sy_rt = ttk.Scrollbar(rt, orient=tk.VERTICAL, command=self.txt_lab_ret_dump.yview)
        self.txt_lab_ret_dump.configure(yscrollcommand=sy_rt.set)
        sy_rt.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_lab_ret_dump.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.txt_lab_ret_dump.insert(
            "1.0",
            "用法（不用边按边点）:\n"
            "1. 挂载后点「A+5s采B（推荐）」\n"
            "2. 立刻切游戏，按住左 Shift 让准星一直亮着（焦点留在游戏）\n"
            "3. 等 5 秒自动采完，回本窗看 retA/retB，点「对比 A/B」把 dump 贴回\n"
            "4. 已有 A 时也可只点「5秒后采B」\n"
            "5. KEY_FORCE：单开点 ON+采B，看屏幕；测完 OFF。勿连点 POLL/AIM。\n",
        )
        self.txt_lab_ret_dump.configure(state=tk.DISABLED)

        # spacer so last controls not flush against bottom
        ttk.Frame(body, height=12).pack(fill=tk.X)

    def _lab_set_text(self, widget: tk.Text, text: str) -> None:
        """Replace Text widget content. @author by ak"""
        try:
            widget.configure(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.insert("1.0", text or "")
            widget.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _lab_show_dumps(self, s) -> None:
        """Update dump text areas from sample. @author by ak"""
        if not hasattr(self, "txt_lab_skill_dump"):
            return
        sk = getattr(s, "skill_dump", b"") or b""
        sq = getattr(s, "seq_dump", b"") or b""
        if sk:
            self._lab_set_text(
                self.txt_lab_skill_dump,
                f"ptr=0x{(s.skill_ptr or 0):X}\n" + format_dword_table(sk),
            )
        else:
            self._lab_set_text(self.txt_lab_skill_dump, "(no skill dump)")
        if sq:
            self._lab_set_text(
                self.txt_lab_seq_dump,
                f"ptr=0x{(s.skill_seq_ptr or 0):X}\n" + format_dword_table(sq),
            )
        else:
            self._lab_set_text(self.txt_lab_seq_dump, "(no seq dump)")


    # ==================================================================
    # 妖楼分步实验室（只调试，不进正式 YaoluRunner 主循环）
    # ==================================================================
    def _build_tab_yaolu(self, parent: ttk.Frame) -> None:
        """
        Yaolu step lab: each pipeline stage is a separate button.

        Debug one step at a time; do not wire into YaoluRunner until green.

        @author by ak
        """
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(wrap, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=body, anchor="nw")

        def _on_body_cfg(_e=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_cfg(e) -> None:
            try:
                canvas.itemconfigure(win_id, width=e.width)
            except Exception:
                pass

        body.bind("<Configure>", _on_body_cfg)
        canvas.bind("<Configure>", _on_canvas_cfg)

        def _wheel(e) -> None:
            delta = int(getattr(e, "delta", 0) or 0)
            if delta:
                canvas.yview_scroll(int(-1 * (delta / 120)), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind(
            "<Leave>",
            lambda _e: canvas.unbind_all("<MouseWheel>") if True else None,
        )

        ttk.Label(
            body,
            text=(
                "妖楼分步实验室：每一步独立跑，日志看主「日志」页 / 底部状态。\n"
                "流程顺序建议：状态 → 寻路入口 → 开暗道 → 等弹窗 → 验证码/关UI → "
                "反馈 baseline/probe → 清读条/取消会话。\n"
                "全部单步通过后再考虑接回正式 YaoluRunner（本页不启动全自动）。"
            ),
            wraplength=920,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6), padx=2)

        top = ttk.Frame(body)
        top.pack(fill=tk.X, pady=(0, 4), padx=2)
        # Dev-only: random 2-cell answer for fail-path recon (not shown on formal UI).
        self.var_yaolu_debug = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            top,
            text="调试：随机点2格/跳过识别API（仅开发面板）",
            variable=self.var_yaolu_debug,
            command=self._on_yaolu_lab_debug_toggle,
        ).pack(side=tk.LEFT)
        self.var_yaolu_status = tk.StringVar(value="idle")
        ttk.Label(top, textvariable=self.var_yaolu_status, style="Mono.TLabel").pack(
            side=tk.RIGHT
        )
        ttk.Button(top, text="停止当前步骤", command=self._yaolu_lab_stop_step).pack(
            side=tk.RIGHT, padx=8
        )

        def _grp(title: str) -> ttk.LabelFrame:
            fr = ttk.LabelFrame(body, text=title, padding=6)
            fr.pack(fill=tk.X, pady=(0, 6), padx=2)
            return fr

        def _row(parent_fr: ttk.Frame) -> ttk.Frame:
            r = ttk.Frame(parent_fr)
            r.pack(fill=tk.X, pady=2)
            return r

        def _btn(row: ttk.Frame, text: str, cmd, *, pad=(0, 4)) -> None:
            ttk.Button(row, text=text, command=cmd).pack(side=tk.LEFT, padx=pad)

        # --- A 状态 ---
        g = _grp("A · 状态读取")
        r = _row(g)
        _btn(r, "读场景坐标", lambda: self._yaolu_lab_run("读场景坐标", self._yaolu_step_scene))
        _btn(r, "读cast/session", lambda: self._yaolu_lab_run("读cast", self._yaolu_step_cast))
        _btn(r, "查验证码弹窗", lambda: self._yaolu_lab_run("查弹窗", self._yaolu_step_captcha_open))

        # --- B 寻路 ---
        g = _grp("B · 寻路入口")
        r = _row(g)
        _btn(r, "寻路到入口锚点", lambda: self._yaolu_lab_run("寻路入口", self._yaolu_step_path_entry))
        _btn(r, "等待到达入口", lambda: self._yaolu_lab_run("等到达", self._yaolu_step_wait_arrival))
        _btn(r, "寻路+等到达", lambda: self._yaolu_lab_run("寻路+到达", self._yaolu_step_path_and_wait))

        # --- C 开暗道 ---
        g = _grp("C · 开暗道 / 等弹窗")
        r = _row(g)
        _btn(r, "交互开暗道", lambda: self._yaolu_lab_run("开暗道", self._yaolu_step_open_entry))
        _btn(r, "仅等弹窗(内存)", lambda: self._yaolu_lab_run("等弹窗", self._yaolu_step_wait_dialog))
        _btn(r, "开暗道+等弹窗", lambda: self._yaolu_lab_run("开+等弹窗", self._yaolu_step_open_and_wait_dialog))

        # --- D 验证码 UI ---
        g = _grp("D · 验证码 UI（关窗用函数，不随机错答污染 audit）")
        r = _row(g)
        _btn(r, "导出验证码图", lambda: self._yaolu_lab_run("导出图", self._yaolu_step_export_captcha))
        _btn(r, "关验证码UI", lambda: self._yaolu_lab_run("关UI", self._yaolu_step_close_captcha))
        _btn(r, "Show隐藏UI", lambda: self._yaolu_lab_run("Show隐藏UI", self._yaolu_step_show_hide_ui))
        _btn(r, "点取消(几何)", lambda: self._yaolu_lab_run("点取消", self._yaolu_step_click_cancel))
        _btn(r, "点确定(几何)", lambda: self._yaolu_lab_run("点确定", self._yaolu_step_click_ok))
        r2 = _row(g)
        _btn(r2, "调试：随机点2格(不确认)", lambda: self._yaolu_lab_run("随机点格", self._yaolu_step_debug_pick_cells))
        _btn(r2, "调试：随机点格+确认", lambda: self._yaolu_lab_run("随机答题", self._yaolu_step_debug_answer_confirm))

        # --- E 反馈 ---
        g = _grp("E · 系统反馈（baseline / probe）")
        r = _row(g)
        _btn(r, "打feedback baseline", lambda: self._yaolu_lab_run("baseline", self._yaolu_step_fb_baseline))
        _btn(r, "probe一次", lambda: self._yaolu_lab_run("probe", self._yaolu_step_fb_probe))
        _btn(r, "反馈指纹", lambda: self._yaolu_lab_run("指纹", self._yaolu_step_fb_fingerprint))
        _btn(r, "等待反馈12s", lambda: self._yaolu_lab_run("等反馈", self._yaolu_step_fb_wait))

        # --- F 清理 ---
        g = _grp("F · 清理读条 / 会话（与 UI 分开）")
        r = _row(g)
        _btn(r, "扫读条UI", lambda: self._yaolu_lab_run("扫读条UI", self._yaolu_step_scan_progress_bar))
        _btn(r, "清0%读条", lambda: self._yaolu_lab_run("清读条", self._yaolu_step_clear_bar))
        _btn(r, "读UI mgr", lambda: self._yaolu_lab_run("读UImgr", self._yaolu_step_ui_mgr))
        _btn(r, "原生0x87C890", lambda: self._yaolu_lab_run("原生清条", self._yaolu_step_native_clear_bar))
        r2 = _row(g)
        _btn(r2, "读perform态", lambda: self._yaolu_lab_run("读perform", self._yaolu_step_perform_state))
        _btn(r2, "停perform/解锁", lambda: self._yaolu_lab_run("停perform", self._yaolu_step_stop_perform))
        _btn(r2, "恢复移动基座", lambda: self._yaolu_lab_run("恢复基座", self._yaolu_step_restore_base))
        _btn(r2, "测HostMove", lambda: self._yaolu_lab_run("测移动", self._yaolu_step_probe_move))
        r3 = _row(g)
        _btn(r3, "cancel会话(含关UI)", lambda: self._yaolu_lab_run("cancel会话", self._yaolu_step_cancel_session))
        _btn(r3, "关UI+清读条", lambda: self._yaolu_lab_run("关UI+清读条", self._yaolu_step_close_and_clear))

        # --- result ---
        out_fr = ttk.LabelFrame(body, text="最近一步结果", padding=4)
        out_fr.pack(fill=tk.BOTH, expand=True, pady=(0, 4), padx=2)
        self.txt_yaolu_lab = tk.Text(
            out_fr,
            height=12,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg="#0d1117",
            fg="#c9d1d9",
            insertbackground="white",
        )
        ysb2 = ttk.Scrollbar(out_fr, orient=tk.VERTICAL, command=self.txt_yaolu_lab.yview)
        self.txt_yaolu_lab.configure(yscrollcommand=ysb2.set)
        self.txt_yaolu_lab.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ysb2.pack(side=tk.RIGHT, fill=tk.Y)

    def _yaolu_lab_hwnd(self) -> int:
        """Resolve game hwnd for bridge clicks. @author by ak"""
        for obj in (
            getattr(self, "current_window", None),
            getattr(self, "session", None),
            getattr(self, "current_result", None),
        ):
            try:
                h = int(getattr(obj, "hwnd", 0) or 0)
                if h:
                    return h
            except Exception:
                pass
        try:
            s = (self.var_hwnd.get() or "").strip()
            if s.startswith("0x") or s.startswith("0X"):
                return int(s, 16)
            if s and s != "-":
                return int(s)
        except Exception:
            pass
        return 0

    def _yaolu_lab_cfg(self):
        """Lab YaoluConfig (debug flags from panel). @author by ak"""
        from app.core.plg_ui import CAPTCHA_DIALOG_NAME_CANDIDATES
        from app.core.yaolu_auto import YaoluConfig

        cfg = YaoluConfig()
        cfg.debug_random_answer = bool(
            getattr(self, "var_yaolu_debug", tk.BooleanVar(value=True)).get()
        )
        cfg.debug_save_crops = True
        cfg.use_bridge = True
        cfg.cancel_use_esc = False
        # ensure dialog name list exists for lab helpers
        if not getattr(cfg, "captcha_dlg_names", None):
            try:
                cfg.captcha_dlg_names = tuple(CAPTCHA_DIALOG_NAME_CANDIDATES)
            except Exception:
                pass
        return cfg

    def _yaolu_lab_set_out(self, text: str) -> None:
        """Show step result in panel. @author by ak"""
        try:
            w = getattr(self, "txt_yaolu_lab", None)
            if w is None:
                return
            w.delete("1.0", tk.END)
            w.insert(tk.END, text or "")
        except Exception:
            pass

    def _on_yaolu_lab_debug_toggle(self) -> None:
        """Reflect debug-random captcha checkbox to console/log. @author by ak"""
        try:
            on = bool(self.var_yaolu_debug.get())
        except Exception:
            on = False
        msg = f"妖楼实验室调试随机答题: {'ON' if on else 'OFF'}"
        try:
            self._attach_log(msg, tag="YAOLU")
        except Exception:
            try:
                self.log(msg)
            except Exception:
                print(msg, flush=True)

    def _yaolu_lab_stop_step(self) -> None:
        """Signal current lab step to stop. @author by ak"""
        ev = getattr(self, "_yaolu_lab_stop", None)
        if ev is not None:
            try:
                ev.set()
            except Exception:
                pass
        self.log("妖楼lab: 已请求停止当前步骤")
        if hasattr(self, "var_yaolu_status"):
            self.var_yaolu_status.set("stopping…")

    def _yaolu_lab_run(self, title: str, fn) -> None:
        """Run one lab step on a worker thread. @author by ak"""
        if getattr(self, "_yaolu_lab_busy", False):
            self.log(f"妖楼lab: 忙碌中，忽略 {title}")
            return
        sess = self._lab_require_session()
        if sess is None:
            return
        self._yaolu_lab_busy = True
        stop = threading.Event()
        self._yaolu_lab_stop = stop
        if hasattr(self, "var_yaolu_status"):
            self.var_yaolu_status.set(f"running: {title}")
        self.status.set("YAOLU_LAB")
        self.log(f"妖楼lab ▶ {title}")

        def work() -> None:
            err = ""
            detail = ""
            t0 = time.monotonic()
            try:
                detail = fn(sess, stop) or ""
            except Exception as e:
                err = str(e)
                detail = f"ERROR: {e}"
            elapsed = time.monotonic() - t0

            def done() -> None:
                self._yaolu_lab_busy = False
                self._yaolu_lab_stop = None
                if err:
                    self.log(f"妖楼lab ✖ {title} {elapsed:.2f}s: {err}")
                    if hasattr(self, "var_yaolu_status"):
                        self.var_yaolu_status.set(f"fail: {title}")
                    self.status.set("ERROR")
                else:
                    self.log(f"妖楼lab ✔ {title} {elapsed:.2f}s")
                    if hasattr(self, "var_yaolu_status"):
                        self.var_yaolu_status.set(f"ok: {title} {elapsed:.1f}s")
                    self.status.set("ATTACHED")
                self._yaolu_lab_set_out(
                    f"[{title}] {elapsed:.2f}s\n"
                    f"{'ERROR: ' + err if err else 'OK'}\n"
                    f"{'—' * 40}\n"
                    f"{detail}"
                )

            try:
                self.after(0, done)
            except Exception:
                done()

        threading.Thread(target=work, name=f"yaolu-lab-{title}", daemon=True).start()


    def _yaolu_step_scene(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.yaolu_auto import read_scene_state

        st = read_scene_state(sess, log=self.log)
        if hasattr(st, "to_dict"):
            return str(st.to_dict())
        return f"scene={st}"

    def _yaolu_step_cast(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.loot import read_host_cast_state
        from app.core.yaolu_auto import _cast_session_busy_state, _cast_state_text

        st = read_host_cast_state(sess, log=self.log)
        if not isinstance(st, dict):
            st = dict(st) if st else {}
        busy = _cast_session_busy_state(st)
        return f"busy={busy}\n{_cast_state_text(st)}\nraw={st}"

    def _yaolu_dlg_names(self, cfg):
        """@author by ak"""
        names = getattr(cfg, "captcha_dlg_names", None)
        if names:
            return tuple(names)
        from app.core.plg_ui import CAPTCHA_DIALOG_NAME_CANDIDATES

        return tuple(CAPTCHA_DIALOG_NAME_CANDIDATES)

    def _yaolu_step_captcha_open(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.plg_ui import is_captcha_dialog_open

        cfg = self._yaolu_lab_cfg()
        names = self._yaolu_dlg_names(cfg)
        hit = is_captcha_dialog_open(sess, names=names, log=self.log)
        return (
            f"shown={hit.shown} name={hit.name!r} "
            f"ptr=0x{int(hit.dlg_ptr or 0):X} ok={hit.ok} err={hit.error!r}"
        )

    def _yaolu_step_path_entry(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.yaolu_auto import path_to_entry_anchor

        cfg = self._yaolu_lab_cfg()
        hwnd = self._yaolu_lab_hwnd()
        out = path_to_entry_anchor(
            sess,
            cfg,
            hwnd=hwnd,
            stop_event=stop,
            log=self.log,
        )
        self._yaolu_lab_last_path = out if isinstance(out, dict) else None
        return str(out)

    def _yaolu_step_wait_arrival(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.yaolu_auto import (
            DEFAULT_ENTRY_ANCHOR,
            ENTRY_PATH_SCENE_ID,
            wait_for_entry_arrival,
        )

        cfg = self._yaolu_lab_cfg()
        move = getattr(self, "_yaolu_lab_last_path", None)
        if not isinstance(move, dict) or not (
            move.get("target_xyz") or move.get("anchor")
        ):
            ax, ay, az = DEFAULT_ENTRY_ANCHOR
            move = {
                "ok": True,
                "target_xyz": (float(ax), float(ay), float(az)),
                "path_scene_id": int(
                    getattr(cfg, "entry_path_scene_id", 0) or ENTRY_PATH_SCENE_ID
                ),
            }
        ok, pos, dist = wait_for_entry_arrival(
            sess,
            move,
            cfg,
            stop_event=stop,
            log=self.log,
            on_progress=lambda m: self.log(f"  status: {m}"),
        )
        return f"arrived={ok} pos={pos} dist={dist}\nmove={move}"

    def _yaolu_step_path_and_wait(self, sess, stop) -> str:
        """@author by ak"""
        a = self._yaolu_step_path_entry(sess, stop)
        if stop.is_set():
            return a + "\nstopped"
        b = self._yaolu_step_wait_arrival(sess, stop)
        return f"path:\n{a}\n\narrive:\n{b}"

    def _yaolu_step_open_entry(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.yaolu_auto import open_entry

        cfg = self._yaolu_lab_cfg()
        hwnd = self._yaolu_lab_hwnd()
        out = open_entry(
            sess,
            cfg,
            hwnd=hwnd,
            stop_event=stop,
            log=self.log,
            status=lambda m: self.log(f"  status: {m}"),
            prepath=False,
        )
        if hasattr(out, "to_dict"):
            return str(out.to_dict())
        return str(out)

    def _yaolu_step_wait_dialog(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.yaolu_auto import wait_for_captcha_dialog_mem

        cfg = self._yaolu_lab_cfg()
        out = wait_for_captcha_dialog_mem(
            sess,
            cfg,
            stop_event=stop,
            log=self.log,
            status=lambda m: self.log(f"  status: {m}"),
        )
        if hasattr(out, "to_dict"):
            return str(out.to_dict())
        return (
            f"ok={getattr(out, 'ok', None)} shown={getattr(out, 'shown', None)} "
            f"name={getattr(out, 'name', None)!r} err={getattr(out, 'error', None)!r}"
        )

    def _yaolu_step_open_and_wait_dialog(self, sess, stop) -> str:
        """@author by ak"""
        a = self._yaolu_step_open_entry(sess, stop)
        if stop.is_set():
            return a + "\nstopped"
        b = self._yaolu_step_wait_dialog(sess, stop)
        return f"open:\n{a}\n\ndialog:\n{b}"

    def _yaolu_step_export_captcha(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.yaolu_auto import capture_captcha_dialog

        cfg = self._yaolu_lab_cfg()
        hwnd = self._yaolu_lab_hwnd()
        out = capture_captcha_dialog(
            hwnd,
            cfg,
            session=sess,
            log=self.log,
            save_tag="lab",
        )
        if hasattr(out, "to_dict"):
            d = out.to_dict()
            if d.get("png"):
                d["png_len"] = len(d.get("png") or b"")
                d.pop("png", None)
            return str(d)
        return (
            f"ok={getattr(out, 'ok', None)} err={getattr(out, 'error', None)!r} "
            f"method={getattr(out, 'method', None)!r} "
            f"png_len={len(getattr(out, 'png', None) or b'')}"
        )

    def _yaolu_step_close_captcha(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.aui_click import close_captcha_dialog

        cfg = self._yaolu_lab_cfg()
        hwnd = self._yaolu_lab_hwnd()
        names = self._yaolu_dlg_names(cfg)
        out = close_captcha_dialog(
            sess,
            hwnd=hwnd,
            prefer_bridge=True,
            prefer_post=True,
            allow_cursor=False,
            humanize=True,
            names=names,
            attempts=max(1, min(3, int(getattr(cfg, "captcha_close_attempts", 2) or 2))),
            settle_s=float(getattr(cfg, "captcha_close_settle_s", 0.2) or 0.2),
            use_esc=False,
            prefer_show_hide=True,
            log=self.log,
        )
        return str(out)

    def _yaolu_step_show_hide_ui(self, sess, stop) -> str:
        """Lab-only: pure AUI Show(0,0,1) on captcha dlg. @author by ak"""
        from app.core.aui_click import hide_aui_dialog
        from app.core.plg_ui import is_captcha_dialog_open

        cfg = self._yaolu_lab_cfg()
        names = self._yaolu_dlg_names(cfg)
        hit = is_captcha_dialog_open(sess, names=names, log=self.log)
        if not hit.shown or not hit.dlg_ptr:
            return f"no captcha open shown={hit.shown} name={hit.name!r} ptr=0x{int(hit.dlg_ptr or 0):X}"
        hr = hide_aui_dialog(sess, int(hit.dlg_ptr), force=True, show_args=(0, 0, 1), try_variants=True, log=self.log)
        hit2 = is_captcha_dialog_open(sess, names=names, log=lambda _m: None)
        return (
            f"before name={hit.name!r} ptr=0x{int(hit.dlg_ptr):X} "
            f"hide={hr} after_open={hit2.shown} after_name={hit2.name!r}"
        )

    def _yaolu_step_click_cancel(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.aui_click import click_captcha_btn_cancel
        from app.core.plg_ui import is_captcha_dialog_open

        cfg = self._yaolu_lab_cfg()
        hwnd = self._yaolu_lab_hwnd()
        names = self._yaolu_dlg_names(cfg)
        ok = click_captcha_btn_cancel(
            sess,
            hwnd,
            prefer_bridge=True,
            prefer_post=True,
            allow_cursor=False,
            humanize=True,
            names=names,
            log=self.log,
        )
        time.sleep(0.35)
        hit = is_captcha_dialog_open(sess, names=names, log=lambda _m: None)
        return f"click_ok={ok} still_open={hit.shown} name={hit.name!r}"

    def _yaolu_step_click_ok(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.aui_click import click_captcha_btn_ok
        from app.core.plg_ui import is_captcha_dialog_open

        cfg = self._yaolu_lab_cfg()
        hwnd = self._yaolu_lab_hwnd()
        names = self._yaolu_dlg_names(cfg)
        ok = click_captcha_btn_ok(
            sess,
            hwnd,
            prefer_bridge=True,
            prefer_post=True,
            allow_cursor=False,
            pure_submit=False,
            humanize=True,
            names=names,
            log=self.log,
        )
        time.sleep(0.35)
        hit = is_captcha_dialog_open(sess, names=names, log=lambda _m: None)
        return f"click_ok={ok} still_open={hit.shown} name={hit.name!r}"

    def _yaolu_step_debug_pick_cells(self, sess, stop) -> str:
        """Random 2 cells only, no confirm. @author by ak"""
        import random

        from app.core.aui_click import click_captcha_cells
        from app.core.plg_ui import is_captcha_dialog_open

        cfg = self._yaolu_lab_cfg()
        hwnd = self._yaolu_lab_hwnd()
        names = self._yaolu_dlg_names(cfg)
        hit = is_captcha_dialog_open(sess, names=names, log=self.log)
        if not hit.shown:
            return "dialog not open"
        pos = random.sample(list(range(1, 9)), 2)
        self.log(f"妖楼lab random cells pos={pos}")
        results = click_captcha_cells(
            sess,
            hwnd,
            pos,
            prefer_bridge=True,
            prefer_post=True,
            allow_cursor=False,
            humanize=True,
            names=names,
            log=self.log,
        )
        return f"pos={pos} clicks={results}"

    def _yaolu_step_debug_answer_confirm(self, sess, stop) -> str:
        """Random cells + confirm (no identify API). @author by ak"""
        a = self._yaolu_step_debug_pick_cells(sess, stop)
        if stop.is_set() or a == "dialog not open":
            return a
        time.sleep(0.25)
        base = self._yaolu_step_fb_baseline(sess, stop)
        b = self._yaolu_step_click_ok(sess, stop)
        return f"pick:\n{a}\n\nbaseline:\n{base}\n\nconfirm:\n{b}"

    def _yaolu_step_fb_baseline(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.game_sys_msg import snapshot_feedback_heap_baseline

        out = snapshot_feedback_heap_baseline(sess, log=self.log)
        hist = getattr(sess, "_feedback_hist_baseline", None)
        return f"heap_base={out}\nhist_base={hist}"

    def _yaolu_step_fb_probe(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.game_sys_msg import probe_captcha_answer_text

        hit = probe_captcha_answer_text(
            sess,
            log=self.log,
            heavy=False,
            trust_chat_hard=False,
        )
        if hit is None:
            return "probe=None"
        return (
            f"kind={hit.kind} method={hit.method} "
            f"text={hit.text!r} error={hit.error!r}"
        )

    def _yaolu_step_fb_fingerprint(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.chat_history import snapshot_feedback_fingerprint

        fp = snapshot_feedback_fingerprint(sess, budget_s=1.0, log=None)
        lines = [
            f"counts={fp.get('counts')}",
            f"inst={len(fp.get('instances') or [])}",
            f"hash={fp.get('sig_hash')}",
        ]
        for it in list(fp.get("instances") or [])[:16]:
            try:
                t, a = it[0], int(it[1])
                c = it[2] if len(it) >= 3 else ""
                lines.append(f"  {t} @0x{a:X} ctx={c}")
            except Exception:
                lines.append(f"  {it!r}")
        return "\n".join(lines)

    def _yaolu_step_fb_wait(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.game_sys_msg import wait_captcha_answer_feedback

        fb = wait_captcha_answer_feedback(
            sess,
            timeout_s=12.0,
            poll_s=0.25,
            stop_event=stop,
            log=self.log,
            status=lambda m: self.log(f"  status: {m}"),
            stage2_hold_s=4.0,
        )
        return (
            f"kind={fb.kind} method={fb.method} "
            f"text={fb.text!r} error={fb.error!r}"
        )

    def _yaolu_step_scan_progress_bar(self, sess, stop) -> str:
        """Scan Win_Prgs2 / MagicProgress* interact bars. @author by ak"""
        from app.core.yaolu_auto import query_interact_progress_bars

        entry = query_interact_progress_bars(sess, entry_only=True, log=self.log)
        all_bars = query_interact_progress_bars(sess, entry_only=False, log=self.log)
        lines = [
            f"entry_bars={len(entry)} all_shown_progress={len(all_bars)}",
        ]
        for b in all_bars:
            rect = b.get("rect") or {}
            lines.append(
                f"  [{'ENTRY' if b.get('entry_match') else 'other'}] "
                f"{b.get('name')} shown={b.get('shown')} "
                f"text={b.get('text')!r} ptr=0x{int(b.get('dlg_ptr') or 0):X} "
                f"rect={rect}"
            )
        if not all_bars:
            lines.append("  (no progress dlg currently shown)")
        return "\n".join(lines)

    def _yaolu_step_ui_mgr(self, sess, stop) -> str:
        """Dump game UI mgr + progress slots. @author by ak"""
        from app.core.yaolu_auto import get_game_ui_mgr, query_interact_progress_bars

        gi = get_game_ui_mgr(sess, log=self.log, prefer_call=True)
        bars = query_interact_progress_bars(sess, entry_only=False, log=self.log)
        lines = [
            f"mgr_ok={gi.get('ok')} mgr=0x{int(gi.get('mgr') or 0):X} via={gi.get('method')} err={gi.get('error')!r}",
            f"slots={gi.get('slots')}",
            f"bars={len(bars)}",
        ]
        for b in bars:
            lines.append(
                f"  {b.get('name')} shown={b.get('shown')} text={b.get('text')!r} "
                f"ptr=0x{int(b.get('dlg_ptr') or 0):X}"
            )
        return "\n".join(lines)

    def _yaolu_step_native_clear_bar(self, sess, stop) -> str:
        """Only thiscall 0x87C890 (no sustain). @author by ak"""
        from app.core.yaolu_auto import (
            get_game_ui_mgr,
            native_cancel_progress_ui,
            query_interact_progress_bars,
            _entry_progress_still_shown,
        )
        import time

        gi = get_game_ui_mgr(sess, log=self.log, prefer_call=True)
        before = query_interact_progress_bars(sess, entry_only=True, log=self.log)
        nr = native_cancel_progress_ui(sess, mgr=int(gi.get("mgr") or 0), log=self.log)
        time.sleep(0.05)
        mid = _entry_progress_still_shown(sess, before, log=lambda _m: None)
        time.sleep(0.30)
        late = _entry_progress_still_shown(sess, before, log=lambda _m: None)
        lines = [
            f"mgr=0x{int(gi.get('mgr') or 0):X} via={gi.get('method')}",
            f"native ok={nr.get('ok')} ret={nr.get('ret')} err={nr.get('error')!r} fn=0x{int(nr.get('fn') or 0):X}",
            f"before={[{'n': b.get('name'), 't': b.get('text')} for b in before]}",
            f"after_50ms still={mid}",
            f"after_300ms still={late} flash_back={bool(not mid and late)}",
        ]
        return "\n".join(lines)

    def _yaolu_step_perform_state(self, sess, stop) -> str:
        """Dump host perform + lock hints. @author by ak"""
        from app.core.yaolu_auto import read_host_perform_state, query_interact_progress_bars

        st = read_host_perform_state(sess, log=self.log)
        bars = query_interact_progress_bars(sess, entry_only=True, log=self.log)
        lines = [
            f"ok={st.get('ok')} active={st.get('active')} locked_hint={st.get('locked_hint')} "
            f"is_matter={st.get('is_matter')} is_base={st.get('is_base')} "
            f"base_missing={st.get('base_missing')} err={st.get('error')!r}",
            f"host=0x{int(st.get('host') or 0):X} mgr=0x{int(st.get('perform_mgr') or 0):X} "
            f"curr=0x{int(st.get('curr_perform') or 0):X} side=0x{int(st.get('side') or 0):X} "
            f"entity=0x{int(st.get('entity') or 0):X} move_ctrl=0x{int(st.get('move_ctrl') or 0):X}",
            f"type={st.get('perform_type')} subtype={st.get('perform_subtype')} "
            f"flag2c/2d={st.get('perform_flag_2c')}/{st.get('perform_flag_2d')} "
            f"side_flags={st.get('flag0')}/{st.get('flag1')} "
            f"gate={st.get('session_gate')}/{st.get('session_gate1')}/{st.get('session_gate2')}",
            f"entry_bars={len(bars)}",
            "note: type=2 is normal locomotion; locked_hint only matter/gate/missing-base",
        ]
        for b in bars:
            lines.append(
                f"  bar {b.get('name')} text={b.get('text')!r} ptr=0x{int(b.get('dlg_ptr') or 0):X}"
            )
        return "\n".join(lines)

    def _yaolu_step_stop_perform(self, sess, stop) -> str:
        """Stop matter perform and unlock move/input. @author by ak"""
        from app.core.yaolu_auto import (
            read_host_perform_state,
            stop_host_perform,
            query_interact_progress_bars,
            probe_host_move,
        )
        from app.core.aui_click import hide_aui_dialog
        import time

        before = read_host_perform_state(sess, log=self.log)
        bars_before = query_interact_progress_bars(sess, entry_only=True, log=self.log)
        hwnd = self._yaolu_lab_hwnd()
        # force_clear_slot only affects matter types; base type=2 is never nulled.
        st = stop_host_perform(
            sess,
            force_clear_slot=True,
            restore_base=True,
            hwnd=hwnd,
            use_bridge_cancel=True,
            log=self.log,
        )
        for b in bars_before:
            ptr = int(b.get("dlg_ptr") or 0)
            if ptr:
                hide_aui_dialog(
                    sess, ptr, force=True, show_args=(0, 0, 0), try_variants=True, log=self.log
                )
        time.sleep(0.08)
        mid = read_host_perform_state(sess, log=lambda _m: None)
        mid_bars = query_interact_progress_bars(sess, entry_only=True, log=lambda _m: None)
        time.sleep(0.40)
        late = read_host_perform_state(sess, log=lambda _m: None)
        late_bars = query_interact_progress_bars(sess, entry_only=True, log=lambda _m: None)
        mv = probe_host_move(sess, dist=2.5, mode=0, wait_s=1.0, log=self.log)
        lines = [
            f"before active={before.get('active')} locked={before.get('locked_hint')} "
            f"matter={before.get('is_matter')} base={before.get('is_base')} "
            f"type={before.get('perform_type')}/{before.get('perform_subtype')} "
            f"gate={before.get('session_gate')} bars={len(bars_before)}",
            f"stop ok={st.get('ok')} unlocked={st.get('unlocked')} method={st.get('method')!r}",
            f"  slot_cleared={st.get('slot_cleared')} restored_base={st.get('restored_base')} "
            f"gate_cleared={st.get('gate_cleared')} cancel21={st.get('cancel21')} "
            f"clear_target={st.get('clear_target')}",
            f"  note={st.get('note')!r} err={st.get('error')!r}",
            f"after_80ms type={mid.get('perform_type')} locked={mid.get('locked_hint')} "
            f"base_missing={mid.get('base_missing')} bars={len(mid_bars)}",
            f"after_480ms type={late.get('perform_type')} locked={late.get('locked_hint')} "
            f"base_missing={late.get('base_missing')} bars={len(late_bars)}",
            f"move_probe moved={mv.get('moved')} delta={float(mv.get('delta') or 0):.3f} "
            f"ret={mv.get('ret')} err={mv.get('error')!r}",
            f"steps={st.get('steps')}",
            f"late_bars={[{'n': b.get('name'), 't': b.get('text')} for b in late_bars]}",
        ]
        if not mv.get("moved"):
            lines.append(
                "WARN: HostMove API may return ok but coords unchanged => locomotion still broken "
                "(if previously bare-cleared slot, try 恢复移动基座; may need relog)"
            )
        return "\n".join(lines)

    def _yaolu_step_restore_base(self, sess, stop) -> str:
        """Reinstall type=2 locomotion perform. @author by ak"""
        from app.core.yaolu_auto import (
            ensure_base_locomotion_perform,
            read_host_perform_state,
            probe_host_move,
        )

        before = read_host_perform_state(sess, log=self.log)
        rb = ensure_base_locomotion_perform(sess, log=self.log)
        after = rb.get("after") or read_host_perform_state(sess, log=lambda _m: None)
        mv = probe_host_move(sess, dist=2.5, mode=0, wait_s=1.0, log=self.log)
        lines = [
            f"before type={before.get('perform_type')} active={before.get('active')} "
            f"base_missing={before.get('base_missing')} locked={before.get('locked_hint')}",
            f"restore ok={rb.get('ok')} method={rb.get('method')!r} ret={rb.get('ret')} "
            f"note={rb.get('note')!r} err={rb.get('error')!r}",
            f"after type={after.get('perform_type')} curr=0x{int(after.get('curr_perform') or 0):X} "
            f"base_missing={after.get('base_missing')} locked={after.get('locked_hint')}",
            f"move_probe moved={mv.get('moved')} delta={float(mv.get('delta') or 0):.3f} ret={mv.get('ret')}",
        ]
        if rb.get("ok") and not mv.get("moved"):
            lines.append(
                "type=2 installed but HostMove still no delta — deeper lock/corruption; "
                "prefer relog this role if it was bare-cleared earlier"
            )
        return "\n".join(lines)

    def _yaolu_step_probe_move(self, sess, stop) -> str:
        """Short HostMove and measure coordinate delta. @author by ak"""
        from app.core.yaolu_auto import probe_host_move, read_host_perform_state

        st = read_host_perform_state(sess, log=self.log)
        mv = probe_host_move(sess, dist=3.0, mode=0, wait_s=1.1, log=self.log)
        lines = [
            f"perform type={st.get('perform_type')} active={st.get('active')} "
            f"matter={st.get('is_matter')} base={st.get('is_base')} "
            f"base_missing={st.get('base_missing')} locked={st.get('locked_hint')}",
            f"pos0={mv.get('before_pos')} pos1={mv.get('after_pos')} scene={mv.get('scene_id')}",
            f"moved={mv.get('moved')} delta={float(mv.get('delta') or 0):.3f} "
            f"api_ok={mv.get('move_ok')} ret={mv.get('ret')} err={mv.get('error')!r}",
        ]
        return "\n".join(lines)

    def _yaolu_step_clear_bar(self, sess, stop) -> str:


        """Clear 0% entry bar; ok only after delayed recheck. @author by ak"""
        from app.core.yaolu_auto import clear_entry_interact_bar

        cfg = self._yaolu_lab_cfg()
        hwnd = self._yaolu_lab_hwnd()
        out = clear_entry_interact_bar(
            sess,
            cfg,
            hwnd=hwnd,
            stop_event=stop,
            log=self.log,
            status=lambda m: self.log(f"  status: {m}"),
        )
        still = out.get("still_shown") or []
        pb = out.get("perform_before") or {}
        ps = out.get("perform_stop") or {}
        pa = (ps.get("after") or {}) if isinstance(ps, dict) else {}
        lines = [
            f"ok={out.get('ok')} flash_back={out.get('flash_back')} "
            f"sustain_hides={out.get('sustain_hides')} "
            f"native_ok={out.get('native_ok')}/{out.get('native_calls')} "
            f"ui_mgr=0x{int(out.get('ui_mgr') or 0):X}",
            f"0x21x{out.get('pulses')} queued={out.get('queued')} "
            f"nudge_n={out.get('nudge_count')} bar_before={out.get('bar_before')} "
            f"bar_after={out.get('bar_after')}",
            f"perform_before active={pb.get('active')} type={pb.get('perform_type')}/"
            f"{pb.get('perform_subtype')} curr=0x{int(pb.get('curr_perform') or 0):X}",
            f"perform_stop ok={ps.get('ok') if isinstance(ps, dict) else None} "
            f"method={ps.get('method') if isinstance(ps, dict) else None} "
            f"after_active={pa.get('active')} slot_cleared={ps.get('slot_cleared') if isinstance(ps, dict) else None}",
            f"note={out.get('note')!r}",
            f"err={out.get('error')!r}",
            f"still={still}",
            f"verify={out.get('verify_delays')}",
            f"sample={out.get('sample')}",
        ]
        return "\n".join(lines)

    def _yaolu_step_cancel_session(self, sess, stop) -> str:
        """@author by ak"""
        from app.core.yaolu_auto import cancel_active_entry_session

        cfg = self._yaolu_lab_cfg()
        hwnd = self._yaolu_lab_hwnd()
        out = cancel_active_entry_session(
            sess,
            cfg,
            hwnd=hwnd,
            stop_event=stop,
            log=self.log,
            force=True,
            close_captcha=True,
        )
        return str(out)

    def _yaolu_step_close_and_clear(self, sess, stop) -> str:
        """@author by ak"""
        a = self._yaolu_step_close_captcha(sess, stop)
        if stop.is_set():
            return a
        b = self._yaolu_step_clear_bar(sess, stop)
        return f"close:\n{a}\n\nclear_bar:\n{b}"


    def _lab_set_hang_detail(self, text: str) -> None:
        """Fill hang probe detail box. @author by ak"""
        w = getattr(self, "txt_lab_hang", None)
        if w is None:
            return
        try:
            w.configure(state=tk.NORMAL)
            w.delete("1.0", tk.END)
            w.insert("1.0", text or "")
            w.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _lab_hwnd(self) -> int:
        """Parse lab HWND from UI. @author by ak"""
        try:
            s = (self.var_hwnd.get() or "").strip()
            if s and s not in ("-", "0", "0x0"):
                return int(s, 16) if s.lower().startswith("0x") else int(s)
        except Exception:
            pass
        return 0

    def _lab_probe_hang(self) -> None:
        """
        读挂机状态：主路径内存 CECAutoPlay+0x08（与正式自动副本相同）。

        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import (
                format_hang_state,
                probe_hang_state,
                probe_hang_state_mem,
                resolve_cec_autoplay_rpm,
            )
            import json

            hwnd = self._lab_hwnd()
            mem = resolve_cec_autoplay_rpm(sess)
            st_mem = probe_hang_state_mem(sess, log=self.log)
            st = probe_hang_state(sess, hwnd=int(hwnd or 0), log=self.log)
            line = format_hang_state(st)
            if st.on is True:
                tag = "开"
            elif st.on is False:
                tag = "关"
            else:
                tag = "未知"
            rb = mem.get("running_byte")
            ap = int(mem.get("autoplay") or 0)
            mode_n = mem.get("mode_name") or mem.get("mode")
            rad = mem.get("radius")
            gate_s = "?"
            filled_s = "?"
            try:
                from app.core.activity_auto import probe_autoplay_skill_gate

                g = probe_autoplay_skill_gate(sess)
                if g.get("ok"):
                    gate_s = "可开" if g.get("can_start_by_gate") else "门未过"
                    filled_s = str(g.get("filled_slots"))
            except Exception:
                pass
            ui = (
                f"挂机={tag} · +0x08={rb} · mode={mem.get('mode')}({mode_n}) "
                f"· radius={rad} · 技能门={gate_s} 槽={filled_s} "
                f"· ap=0x{ap:X} · src={st.source}"
            )
            try:
                self.var_lab_hang.set(ui)
            except Exception:
                pass
            detail = {
                "summary": ui,
                "mem_only": st_mem.to_dict() if hasattr(st_mem, "to_dict") else {},
                "full": st.to_dict() if hasattr(st, "to_dict") else {},
                "chain": {
                    "host_base": hex(int(mem.get("host_base") or 0)),
                    "autoplay": hex(ap),
                    "running_byte": rb,
                    "mode": mem.get("mode"),
                    "mode_name": mem.get("mode_name"),
                    "radius": mem.get("radius"),
                    "radius_off": "+0x21",
                    "mode_off": "+0x1C",
                    "ok": mem.get("ok"),
                    "error": mem.get("error"),
                },
            }
            pretty = json.dumps(detail, ensure_ascii=False, indent=2, default=str)
            self._lab_set_hang_detail(pretty)
            try:
                self._lab_sync_hang_cfg_from_mem(mem)
            except Exception:
                pass
            self.log(f"实验挂机检测: {ui}")
            self.log(f"实验挂机详情: {pretty}")
        except Exception as e:
            try:
                self.var_lab_hang.set(f"挂机检测失败: {e}")
            except Exception:
                pass
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机检测失败: {e}")

    def _lab_toggle_hang_altr(self) -> None:
        """
        后台 Alt+R 切换挂机（与正式面板同一路径 press_bg_chord_once）。

        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        pid = int(getattr(sess, "pid", 0) or 0)
        hwnd = self._lab_hwnd()
        if not pid:
            self.log("实验 Alt+R: 无 pid")
            return
        try:
            from app.core.bg_input import press_bg_chord_once

            r = press_bg_chord_once(
                pid,
                "Alt+R",
                hwnd=int(hwnd or 0),
                hold_ms=55,
                allow_softsend=False,
                log=self.log,
            )
            ok = bool(r.get("ok"))
            msg = f"实验 Alt+R: {'OK' if ok else '失败'} {r}"
            self.log(msg)
            try:
                self.var_lab_hang.set(
                    f"已发送 Alt+R · {'OK' if ok else '失败'} · 点「读内存状态」核对"
                )
            except Exception:
                pass
            if not ok:
                self._lab_set_hang_detail(str(r))
        except Exception as e:
            self.log(f"实验 Alt+R 失败: {e}")
            try:
                self.var_lab_hang.set(f"Alt+R 失败: {e}")
            except Exception:
                pass
            self._lab_set_hang_detail(str(e))

    def _lab_toggle_hang_altr_and_probe(self) -> None:
        """
        Alt+R 切换后稍等再读内存，方便来回验证。

        @author by ak
        """
        self._lab_toggle_hang_altr()
        try:
            # MainWindow is a Frame; use widget.after
            self.after(450, self._lab_probe_hang)
        except Exception:
            import time

            time.sleep(0.45)
            self._lab_probe_hang()

    def _lab_list_hang_ui(self) -> None:
        """
        List hang-related dialogs + currently shown UI names for recon.

        请分别在「挂机关 / 挂机开」各点一次，对比 all_shown 与 controls。

        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import (
                HANG_BTN_CTRL_CANDIDATES,
                HANG_BTN_PARENT_DLGS,
                HANG_STATE_DLG_CANDIDATES,
                HANG_STATE_FALSE_POSITIVE,
                _dlg_visible_dual,
                _is_hang_on_signal_dlg,
                aui_get_dlg_item,
                aui_obj_is_enable,
                _aui_obj_is_show,
                _read_aui_push_checked,
                _probe_host_states_hex,
            )
            from app.core.plg_ui import (
                get_game_ui_dlg,
                list_game_ui_dlg_names,
                query_dlg_show,
            )

            from app.core.activity_auto import resolve_cec_autoplay_rpm

            quiet = lambda _m: None
            lines: list[str] = []
            mem = resolve_cec_autoplay_rpm(sess)
            lines.append("=== 内存主路径 CECAutoPlay ===")
            lines.append(
                f"  host_base=0x{int(mem.get('host_base') or 0):X} "
                f"autoplay=0x{int(mem.get('autoplay') or 0):X} "
                f"running_byte(+0x08)={mem.get('running_byte')} "
                f"mode(+0x1C)={mem.get('mode')}({mem.get('mode_name')}) "
                f"radius(+0x21)={mem.get('radius')} "
                f"ok={mem.get('ok')} err={mem.get('error')}"
            )
            lines.append(
                f"  => 挂机={'开' if mem.get('running') else '关' if mem.get('running') is False else '未知'}"
                f" · {mem.get('mode_name') or '?'} · 半径={mem.get('radius')}"
            )
            hs = _probe_host_states_hex(sess)
            lines.append(f"=== host_states={hs or '-'} ===")
            lines.append("=== 候选挂机对话框（仅 recon，不作为主判定）===")
            for name in HANG_STATE_DLG_CANDIDATES:
                dlg = get_game_ui_dlg(sess, name, log=quiet) or 0
                vis = _dlg_visible_dual(sess, int(dlg), log=quiet) if dlg else {
                    "ptr": 0, "is_dlg_show": None, "aui_is_show": None, "visible": False
                }
                lines.append(
                    f"  {name}: ptr=0x{int(vis.get('ptr') or 0):X}"
                    f" dlg_show={vis.get('is_dlg_show')} aui_show={vis.get('aui_is_show')}"
                    f" visible={vis.get('visible')}"
                )
            lines.append("=== 已知误报 ===")
            for name in sorted(HANG_STATE_FALSE_POSITIVE):
                r = query_dlg_show(sess, name, log=quiet)
                lines.append(
                    f"  {name}: shown={getattr(r, 'shown', None)} ptr=0x{int(getattr(r, 'dlg_ptr', 0) or 0):X}"
                )
            lines.append("=== 父窗+控件（有 ptr 的才列出）===")
            ctrl_n = 0
            for parent in HANG_BTN_PARENT_DLGS:
                dlg = get_game_ui_dlg(sess, parent, log=quiet) or 0
                if not dlg:
                    lines.append(f"  {parent}: ptr=0")
                    continue
                pvis = _dlg_visible_dual(sess, int(dlg), log=quiet)
                lines.append(
                    f"  {parent}: ptr=0x{int(dlg):X} visible={pvis.get('visible')}"
                    f" dlg={pvis.get('is_dlg_show')} aui={pvis.get('aui_is_show')}"
                )
                for cname in HANG_BTN_CTRL_CANDIDATES:
                    ctrl = aui_get_dlg_item(sess, dlg, cname, log=quiet) or 0
                    if not ctrl:
                        continue
                    ctrl_n += 1
                    cshow = _aui_obj_is_show(sess, ctrl, log=quiet)
                    cen = aui_obj_is_enable(sess, ctrl, log=quiet)
                    cchk = _read_aui_push_checked(sess, ctrl)
                    lines.append(
                        f"    .{cname}: ptr=0x{int(ctrl):X} show={cshow} enable={cen} checked={cchk}"
                    )
            lines.append(f"=== 命中控件数 {ctrl_n} ===")
            lines.append("=== 当前全部 shown 对话框（开/关对比关键）===")
            listed = list_game_ui_dlg_names(sess, log=self.log)
            names = list(getattr(listed, "names", None) or [])
            shown_n = 0
            for nm in names:
                r = query_dlg_show(sess, str(nm), log=quiet)
                if getattr(r, "ok", False) and getattr(r, "shown", False):
                    shown_n += 1
                    tag = ""
                    if _is_hang_on_signal_dlg(str(nm)):
                        tag = " [ON候选]"
                    elif str(nm) in HANG_STATE_FALSE_POSITIVE or "tip" in str(nm).lower():
                        tag = " [误报]"
                    lines.append(
                        f"  SHOWN {nm}{tag} ptr=0x{int(getattr(r, 'dlg_ptr', 0) or 0):X}"
                    )
            text_out = "\n".join(lines)
            self._lab_set_hang_detail(text_out)
            try:
                self.var_lab_hang.set(
                    f"UI枚举 · shown={shown_n} 控件={ctrl_n} 注册={len(names)}"
                )
            except Exception:
                pass
            self.log("实验挂机UI枚举:\n" + text_out)
        except Exception as e:
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机UI枚举失败: {e}")

    def _lab_sync_hang_cfg_from_mem(self, mem: dict | None) -> None:
        """
        将内存中的模式/半径同步到实验台控件（读状态后调用）。

        @author by ak
        """
        mem = mem or {}
        try:
            mode = mem.get("mode")
            if mode is not None:
                m = int(mode)
                if m == 0:
                    self.var_lab_hang_mode.set("0 普通模式")
                elif m == 1:
                    self.var_lab_hang_mode.set("1 副本模式")
        except Exception:
            pass
        try:
            rad = mem.get("radius")
            if rad is not None:
                self.var_lab_hang_radius.set(str(int(rad)))
        except Exception:
            pass

    def _lab_parse_hang_mode(self, mode=None) -> int | None:
        """Parse mode from arg or combobox. @author by ak"""
        if mode is not None:
            try:
                return int(mode)
            except Exception:
                return None
        raw = (self.var_lab_hang_mode.get() or "").strip()
        if raw.startswith("0"):
            return 0
        if raw.startswith("1"):
            return 1
        try:
            return int(raw.split()[0])
        except Exception:
            return None

    def _lab_apply_hang_mode(self, mode=None) -> None:
        """
        实验：写 CECAutoPlay+0x1C 模式（0 普通 / 1 副本）。

        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        want = self._lab_parse_hang_mode(mode)
        if want is None:
            self.log("实验挂机: 模式无效（需要 0/1）")
            return
        try:
            from app.core.activity_auto import (
                autoplay_mode_name,
                resolve_cec_autoplay_rpm,
                set_autoplay_mode,
            )
            import json

            before = resolve_cec_autoplay_rpm(sess)
            ret = set_autoplay_mode(sess, want, log=self.log)
            after = resolve_cec_autoplay_rpm(sess)
            ok = bool(ret.get("ok"))
            name = autoplay_mode_name(want)
            self.var_lab_hang.set(
                f"模式写入{'OK' if ok else '失败'} → {want}({name}) "
                f"· 现 radius={after.get('radius')} run={after.get('running_byte')}"
            )
            detail = {
                "action": "set_mode",
                "want": want,
                "want_name": name,
                "result": ret,
                "before": before,
                "after": after,
                "tip": "已开挂时改模式建议 Alt+R 关→开 以重建策略",
            }
            pretty = json.dumps(detail, ensure_ascii=False, indent=2, default=str)
            self._lab_set_hang_detail(pretty)
            try:
                self._lab_sync_hang_cfg_from_mem(after)
            except Exception:
                pass
            self.log(f"实验挂机设模式: ok={ok} want={want}({name}) ret={ret}")
        except Exception as e:
            self.var_lab_hang.set(f"模式写入失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机设模式失败: {e}")

    def _lab_apply_hang_radius(self, radius=None) -> None:
        """
        实验：写 CECAutoPlay+0x21 范围半径（允许 1，突破 UI 5-50）。

        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        if radius is None:
            raw = (self.var_lab_hang_radius.get() or "").strip()
            try:
                want = int(raw)
            except Exception:
                self.log(f"实验挂机: 半径无效 {raw!r}")
                return
        else:
            try:
                want = int(radius)
            except Exception:
                self.log("实验挂机: 半径无效")
                return
            try:
                self.var_lab_hang_radius.set(str(want))
            except Exception:
                pass
        try:
            from app.core.activity_auto import (
                resolve_cec_autoplay_rpm,
                set_autoplay_radius,
            )
            import json

            before = resolve_cec_autoplay_rpm(sess)
            ret = set_autoplay_radius(
                sess,
                want,
                allow_below_ui_min=True,
                log=self.log,
            )
            after = resolve_cec_autoplay_rpm(sess)
            ok = bool(ret.get("ok"))
            self.var_lab_hang.set(
                f"半径写入{'OK' if ok else '失败'} → {want} "
                f"· 现 mode={after.get('mode')}({after.get('mode_name')}) "
                f"run={after.get('running_byte')}"
            )
            detail = {
                "action": "set_radius",
                "want": want,
                "result": ret,
                "before": before,
                "after": after,
                "tip": "UI 文案仍为 5-50；内存 1 可被战斗比较使用。打开设置保存可能被 UI 回写。",
            }
            pretty = json.dumps(detail, ensure_ascii=False, indent=2, default=str)
            self._lab_set_hang_detail(pretty)
            try:
                self._lab_sync_hang_cfg_from_mem(after)
            except Exception:
                pass
            self.log(f"实验挂机设半径: ok={ok} want={want} ret={ret}")
        except Exception as e:
            self.var_lab_hang.set(f"半径写入失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机设半径失败: {e}")

    def _lab_parse_hang_skill_ids(self) -> list[int] | None:
        """Parse skill id field: comma/space separated, 0x ok. Empty -> []. @author by ak"""
        raw = ""
        try:
            raw = (self.var_lab_hang_skills.get() or "").strip()
        except Exception:
            raw = ""
        if not raw:
            return []
        parts = []
        for chunk in raw.replace(";", ",").replace("，", ",").split(","):
            for p in chunk.split():
                p = p.strip()
                if p:
                    parts.append(p)
        ids: list[int] = []
        for p in parts:
            try:
                ids.append(int(p, 0) & 0xFFFFFFFF)
            except Exception:
                self.log(f"实验挂机: 技能ID无效 {p!r}")
                return None
        return ids

    def _lab_probe_hang_skills(self) -> None:
        """读技能门 + 九槽。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import (
                probe_autoplay_skill_gate,
                read_autoplay_skills,
            )
            import json

            sk = read_autoplay_skills(sess)
            gate = probe_autoplay_skill_gate(sess)
            ok = bool(sk.get("ok"))
            filled = sk.get("filled_slots")
            gok = bool(sk.get("gate_ok"))
            self.var_lab_hang.set(
                f"技能门={'通过' if gok else '未过'} · 槽={filled} "
                f"· a=0x{int(sk.get('gate_a') or 0):X} b=0x{int(sk.get('gate_b') or 0):X}"
            )
            # sync entry with non-zero ids for convenience
            try:
                ids = [x for x in (sk.get("slot_ids") or []) if int(x)]
                if ids:
                    self.var_lab_hang_skills.set(
                        ",".join(f"0x{int(x):X}" for x in ids)
                    )
            except Exception:
                pass
            detail = {"action": "read_skills", "skills": sk, "gate": gate}
            pretty = json.dumps(detail, ensure_ascii=False, indent=2, default=str)
            self._lab_set_hang_detail(pretty)
            self.log(
                f"实验挂机技能: ok={ok} gate_ok={gok} filled={filled} "
                f"a={sk.get('gate_a')} b={sk.get('gate_b')}"
            )
        except Exception as e:
            self.var_lab_hang.set(f"读技能失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机读技能失败: {e}")

    def _lab_probe_hang_gate(self) -> None:
        """开挂技能门预检。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import probe_autoplay_skill_gate
            import json

            ret = probe_autoplay_skill_gate(sess)
            can = bool(ret.get("can_start_by_gate"))
            self.var_lab_hang.set(
                f"开挂预检={'可开' if can else '不可开'} · 槽={ret.get('filled_slots')} "
                f"· a=0x{int(ret.get('gate_a') or 0):X} b=0x{int(ret.get('gate_b') or 0):X}"
            )
            pretty = json.dumps(
                {"action": "probe_gate", "result": ret},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            self._lab_set_hang_detail(pretty)
            self.log(f"实验挂机预检: can={can} ret={ret}")
        except Exception as e:
            self.var_lab_hang.set(f"预检失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机预检失败: {e}")

    def _lab_fill_hang_skills(self) -> None:
        """一键灌技能到门控+九槽。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        ids = self._lab_parse_hang_skill_ids()
        if ids is None:
            return
        try:
            from app.core.activity_auto import (
                fill_autoplay_skills_for_gate,
                probe_autoplay_skill_gate,
            )
            import json

            ret = fill_autoplay_skills_for_gate(sess, ids, log=self.log)
            gate = probe_autoplay_skill_gate(sess)
            ok = bool(ret.get("ok"))
            can = bool(gate.get("can_start_by_gate"))
            self.var_lab_hang.set(
                f"灌技能{'OK' if ok else '失败'} · 门控={'可开' if can else '未过'} "
                f"· 槽={gate.get('filled_slots')}"
            )
            detail = {
                "action": "fill_skills",
                "input_ids": [hex(x) for x in ids],
                "result": ret,
                "gate": gate,
                "tip": "灌入后可用 Alt+R 开挂；已开挂建议关→开",
            }
            pretty = json.dumps(detail, ensure_ascii=False, indent=2, default=str)
            self._lab_set_hang_detail(pretty)
            self.log(f"实验挂机灌技能: ok={ok} can={can} ret_ok={ret.get('ok')} err={ret.get('error')}")
        except Exception as e:
            self.var_lab_hang.set(f"灌技能失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机灌技能失败: {e}")

    def _lab_clear_hang_skills(self) -> None:
        """写成空技能：门控+九槽全 0。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import (
                clear_autoplay_skills,
                probe_autoplay_skill_gate,
                read_autoplay_skills,
            )
            import json

            before = read_autoplay_skills(sess)
            ret = clear_autoplay_skills(sess, log=self.log)
            after = read_autoplay_skills(sess)
            gate = probe_autoplay_skill_gate(sess)
            ok = bool(ret.get("ok"))
            self.var_lab_hang.set(
                f"清空技能{'OK' if ok else '失败'} · 门控={'可开' if gate.get('can_start_by_gate') else '空/未过'} "
                f"· 槽={after.get('filled_slots')}"
            )
            try:
                self.var_lab_hang_skills.set("")
            except Exception:
                pass
            detail = {
                "action": "clear_skills",
                "before": before,
                "result": ret,
                "after": after,
                "gate": gate,
                "tip": "空技能 = gate_a/b=0 + 九槽全 0；tip 开挂会失败，需再灌技能",
            }
            pretty = json.dumps(detail, ensure_ascii=False, indent=2, default=str)
            self._lab_set_hang_detail(pretty)
            self.log(
                f"实验挂机清空技能: ok={ok} gate_ok={gate.get('can_start_by_gate')} "
                f"filled={after.get('filled_slots')} err={ret.get('error')}"
            )
        except Exception as e:
            self.var_lab_hang.set(f"清空技能失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机清空技能失败: {e}")

    def _lab_force_hang_on(self) -> None:
        """强开挂机：发送开挂封包，不校验本地技能门。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import (
                probe_autoplay_skill_gate,
                resolve_cec_autoplay_rpm,
            )
            from app.core.hang_settings import get_hang_config, start_hang
            import json

            before = resolve_cec_autoplay_rpm(sess)
            gate = probe_autoplay_skill_gate(sess)
            ret = start_hang(sess, get_hang_config(), hwnd=getattr(sess, "hwnd", 0), log=self.log)
            after = resolve_cec_autoplay_rpm(sess)
            ok = bool(ret.get("ok"))
            self.var_lab_hang.set(
                f"强开{'OK' if ok else '失败'} · run={after.get('running_byte')} "
                f"· 技能门={'可开' if gate.get('can_start_by_gate') else '空/未过(已无视)'} "
                f"· 槽={gate.get('filled_slots')}"
            )
            detail = {
                "action": "force_start",
                "before": before,
                "gate_before": gate,
                "result": ret,
                "after": after,
                "tip": "发送开挂封包，不校验本地技能门；空技能也能开",
            }
            self._lab_set_hang_detail(
                json.dumps(detail, ensure_ascii=False, indent=2, default=str)
            )
            self.log(f"实验挂机封包开挂: ok={ok} ret={ret}")
        except Exception as e:
            self.var_lab_hang.set(f"强开失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机强开失败: {e}")

    def _lab_force_hang_off(self) -> None:
        """实验页：发送关挂机封包。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import (
                resolve_cec_autoplay_rpm,
            )
            from app.core.hang_settings import get_hang_config, stop_hang
            import json

            before = resolve_cec_autoplay_rpm(sess)
            ret = stop_hang(sess, get_hang_config(), hwnd=getattr(sess, "hwnd", 0), log=self.log)
            after = resolve_cec_autoplay_rpm(sess)
            ok = bool(ret.get("ok"))
            self.var_lab_hang.set(
                f"强关{'OK' if ok else '失败'} · run={after.get('running_byte')} "
                f"· mode={after.get('mode')} radius={after.get('radius')}"
            )
            detail = {
                "action": "control_packet_stop",
                "before": before,
                "result": ret,
                "after": after,
            }
            self._lab_set_hang_detail(
                json.dumps(detail, ensure_ascii=False, indent=2, default=str)
            )
            self.log(f"实验挂机封包关挂: ok={ok} ret={ret}")
        except Exception as e:
            self.var_lab_hang.set(f"强关失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机强关失败: {e}")


    def _lab_probe_hang_recover_pct(self) -> None:
        """读各恢复格触发百分比。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import read_autoplay_recover_pct
            import json

            ret = read_autoplay_recover_pct(sess)
            ok = bool(ret.get("ok"))
            parts = []
            for s in ret.get("slots") or []:
                parts.append(f"格{s.get('grid')}={s.get('pct')}%")
            self.var_lab_hang.set(
                f"恢复阈值{'OK' if ok else '失败'} · " + (" ".join(parts) if parts else "?")
            )
            # sync current grid pct into entry if possible
            try:
                g = int((self.var_lab_hang_recover_slot.get() or "3").strip())
                if 1 <= g <= 4:
                    self.var_lab_hang_recover_pct.set(
                        str(int(ret.get("by_grid", {}).get(str(g)) or 0))
                    )
            except Exception:
                pass
            self._lab_set_hang_detail(
                json.dumps(ret, ensure_ascii=False, indent=2, default=str)
            )
            self.log(f"实验挂机恢复阈值: ok={ok} {parts}")
        except Exception as e:
            self.var_lab_hang.set(f"读恢复阈值失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机读恢复阈值失败: {e}")

    def _lab_apply_hang_recover_pct(self, pct=None) -> None:
        """
        写当前「恢复格」的触发百分比。

        例：恢复格=3，触发%=100 → 第3格（攻击/替换）未满血就尝试。

        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import (
                read_autoplay_recover_pct,
                set_autoplay_recover_pct_for_grid,
            )
            import json

            if pct is None:
                pct = int((self.var_lab_hang_recover_pct.get() or "100").strip())
            else:
                pct = int(pct)
                self.var_lab_hang_recover_pct.set(str(pct))
            raw_g = (self.var_lab_hang_recover_slot.get() or "3").strip()
            grid = int(raw_g)
            # accept 0-based typed by mistake
            if grid == 0:
                grid = 1
            if grid < 1 and 0 <= grid <= 3:
                grid = grid + 1
            ret = set_autoplay_recover_pct_for_grid(sess, grid, pct, log=self.log)
            after = read_autoplay_recover_pct(sess)
            ok = bool(ret.get("ok"))
            parts = [
                f"格{s.get('grid')}={s.get('pct')}%" for s in (after.get("slots") or [])
            ]
            self.var_lab_hang.set(
                f"阈值{'OK' if ok else '失败'} · 格{grid}→{pct}% · " + " ".join(parts)
            )
            detail = {
                "action": "set_recover_pct",
                "grid": grid,
                "want_pct": pct,
                "result": ret,
                "after": after,
                "tip": "低于该百分比才用该格道具；100%=未满即尝试",
            }
            self._lab_set_hang_detail(
                json.dumps(detail, ensure_ascii=False, indent=2, default=str)
            )
            self.log(f"实验挂机设恢复阈值: ok={ok} grid={grid} pct={pct} ret={ret}")
        except Exception as e:
            self.var_lab_hang.set(f"设恢复阈值失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机设恢复阈值失败: {e}")

    def _lab_probe_hang_recover_iv(self) -> None:
        """读恢复 HP/MP 检测间隔。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import read_autoplay_recover_interval
            import json

            ret = read_autoplay_recover_interval(sess)
            ok = bool(ret.get("ok"))
            self.var_lab_hang.set(
                f"恢复间隔{'OK' if ok else '失败'} · "
                f"HP={ret.get('hp_interval_ms')}ms(cnt={ret.get('hp_cnt')}) "
                f"MP={ret.get('mp_interval_ms')}ms(cnt={ret.get('mp_cnt')}) "
                f"· 阈值HP {ret.get('hp_pct_a')}/{ret.get('hp_pct_b')} "
                f"MP {ret.get('mp_pct_a')}/{ret.get('mp_pct_b')}"
            )
            if ret.get("hp_interval_ms") is not None:
                try:
                    self.var_lab_hang_recover_iv.set(str(int(ret.get("hp_interval_ms"))))
                except Exception:
                    pass
            self._lab_set_hang_detail(
                json.dumps(ret, ensure_ascii=False, indent=2, default=str)
            )
            self.log(
                f"实验挂机恢复间隔: ok={ok} hp={ret.get('hp_interval_ms')} "
                f"mp={ret.get('mp_interval_ms')}"
            )
        except Exception as e:
            self.var_lab_hang.set(f"读恢复间隔失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机读恢复间隔失败: {e}")

    def _lab_apply_hang_recover_iv(self, ms=None) -> None:
        """写恢复检测间隔（HP+MP 同值，或入口指定）。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import (
                read_autoplay_recover_interval,
                set_autoplay_recover_interval,
            )
            import json

            if ms is None:
                raw = (self.var_lab_hang_recover_iv.get() or "").strip()
                ms = int(raw)
            else:
                ms = int(ms)
                self.var_lab_hang_recover_iv.set(str(ms))
            ret = set_autoplay_recover_interval(
                sess, hp_ms=ms, mp_ms=ms, reset_counters=True, log=self.log
            )
            after = read_autoplay_recover_interval(sess)
            ok = bool(ret.get("ok"))
            self.var_lab_hang.set(
                f"恢复间隔{'OK' if ok else '失败'} · 设={ms}ms · "
                f"HP={after.get('hp_interval_ms')} MP={after.get('mp_interval_ms')}"
            )
            detail = {
                "action": "set_recover_interval",
                "want_ms": ms,
                "result": ret,
                "after": after,
                "tip": "默认1000；200~500 更勤。阈值未改（HP +0x9B 等）。",
            }
            self._lab_set_hang_detail(
                json.dumps(detail, ensure_ascii=False, indent=2, default=str)
            )
            self.log(f"实验挂机设恢复间隔: ok={ok} want={ms} ret={ret}")
        except Exception as e:
            self.var_lab_hang.set(f"设恢复间隔失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机设恢复间隔失败: {e}")

    def _lab_parse_recover_slot(self, *, tid: int = 0, name: str = "", keyword: str = "") -> int:
        """
        Parse recover grid from lab entry.

        UI 使用 1-based 第1~4格；内部转 0-based。
        攻击/伤害药丸默认第 3 格（即使填了 1/空）。

        @author by ak
        """
        from app.core.activity_auto import (
            AUTOPLAY_RECOVER_ATTACK_SLOT,
            is_autoplay_attack_recover_item,
            resolve_autoplay_recover_write_slot,
        )

        raw = (self.var_lab_hang_recover_slot.get() or "").strip()
        si = None
        try:
            n = int(raw)
            # 兼容：用户填 1..4 视为格号；填 0 也当第1格
            if 1 <= n <= 4:
                si = n - 1
            elif n == 0:
                si = 0
            elif 0 < n < 4:
                si = n
        except Exception:
            si = None
        out = resolve_autoplay_recover_write_slot(
            si,
            name=name,
            keyword=keyword,
            tid=tid,
            prefer_attack_third=True,
        )
        # 同步显示为 1-based 格号
        try:
            self.var_lab_hang_recover_slot.set(str(int(out) + 1))
        except Exception:
            pass
        return int(out)

    def _lab_probe_hang_recover(self) -> None:
        """读内挂恢复槽 flags/item_id。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import read_autoplay_recover
            import json

            ret = read_autoplay_recover(sess)
            ok = bool(ret.get("ok"))
            slots = ret.get("slots") or []
            brief = []
            for s in slots:
                if not (s.get("enabled") or int(s.get("item_id") or 0)):
                    continue
                role = s.get("role") or f"#{s.get('slot')}"
                nm = s.get("name") or ""
                brief.append(
                    f"[{s.get('slot')}:{role}]"
                    f"{'ON' if s.get('enabled') else 'off'}"
                    f"=0x{int(s.get('item_id') or 0):X}"
                    + (f" {nm}" if nm else "")
                )
            self.var_lab_hang.set(
                f"恢复槽{'OK' if ok else '失败'} · flags={ret.get('flags_hex')} "
                f"· en={ret.get('enabled_count')} filled={ret.get('filled_count')} "
                f"· {' '.join(brief) if brief else '(空)'}"
            )
            self._lab_set_hang_detail(
                json.dumps(ret, ensure_ascii=False, indent=2, default=str)
            )
            self.log(
                f"实验挂机读恢复槽: ok={ok} flags={ret.get('flags_hex')} "
                f"en={ret.get('enabled_count')} filled={ret.get('filled_count')}"
            )
        except Exception as e:
            self.var_lab_hang.set(f"读恢复槽失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机读恢复槽失败: {e}")

    def _lab_search_hang_bag_item(self) -> None:
        """按关键字搜背包道具。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import find_lab_bag_items
            import json

            key = (self.var_lab_hang_item_key.get() or "").strip() or "伤害物品"
            ret = find_lab_bag_items(sess, key, log=self.log)
            ok = bool(ret.get("ok"))
            n = int(ret.get("count") or 0)
            first = (ret.get("items") or [None])[0]
            if first:
                self.var_lab_hang.set(
                    f"搜包{'OK' if ok else '失败'} · {n}件 · 首="
                    f"pack{first.get('package')}:{first.get('slot')} "
                    f"tid=0x{int(first.get('tid') or 0):X} {first.get('name')!r}"
                )
            else:
                self.var_lab_hang.set(f"搜包{'OK' if ok else '失败'} · 0件 · key={key!r}")
            self._lab_set_hang_detail(
                json.dumps(ret, ensure_ascii=False, indent=2, default=str)
            )
            self.log(f"实验挂机搜背包: ok={ok} key={key!r} count={n}")
        except Exception as e:
            self.var_lab_hang.set(f"搜背包失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机搜背包失败: {e}")

    def _lab_force_use_hang_bag_item(self) -> None:
        """强制使用一次关键字匹配的背包道具。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import force_use_lab_bag_item
            import json

            key = (self.var_lab_hang_item_key.get() or "").strip() or "伤害物品"
            ret = force_use_lab_bag_item(sess, keyword=key, log=self.log)
            ok = bool(ret.get("ok"))
            tgt = ret.get("target") or {}
            self.var_lab_hang.set(
                f"强制使用{'OK' if ok else '失败'} · pack{tgt.get('package')}:{tgt.get('slot')} "
                f"tid=0x{int(tgt.get('tid') or 0):X} {tgt.get('name')!r}"
            )
            self._lab_set_hang_detail(
                json.dumps(ret, ensure_ascii=False, indent=2, default=str)
            )
            self.log(f"实验挂机强制使用: ok={ok} ret={ret}")
        except Exception as e:
            self.var_lab_hang.set(f"强制使用失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机强制使用失败: {e}")

    def _lab_start_damage_item_stat(self) -> None:
        """Start one fixed 60-second measurement of observed item consumption."""
        if self._damage_stat_running:
            return
        sess = self._lab_require_session()
        if sess is None:
            return
        import threading

        self._damage_stat_running = True
        self._damage_stat_stop = threading.Event()
        key = (self.var_lab_hang_item_key.get() or "").strip() or "伤害物品"
        self.var_lab_damage_stat.set("初始化 60 秒有效释放统计…")
        try:
            self.btn_lab_damage_stat.configure(state=tk.DISABLED)
        except Exception:
            pass

        def _worker() -> None:
            try:
                from app.core.damage_item_stats import measure_damage_item_frequency

                result = measure_damage_item_frequency(
                    sess,
                    keyword=key,
                    duration_s=60.0,
                    poll_s=0.5,
                    stop_event=self._damage_stat_stop,
                    on_update=lambda update: self.msg_q.put(("__LAB_DAMAGE_STAT__", update)),
                    log=lambda message: self.msg_q.put(f"[DAMAGE_STAT] {message}"),
                )
                self.msg_q.put(("__LAB_DAMAGE_STAT_DONE__", result))
            except Exception as e:
                self.msg_q.put(("__LAB_DAMAGE_STAT_DONE__", {"ok": False, "error": str(e)}))

        threading.Thread(target=_worker, name="damage-item-stat", daemon=True).start()

    def _lab_apply_damage_item_stat(self, update: dict) -> None:
        """Apply background one-minute-stat updates on the Tk thread."""
        phase = str(update.get("phase") or "")
        if phase == "started":
            items = update.get("target_items") or []
            names = ", ".join(
                f"0x{int(item.get('tid') or 0):X}" for item in items
            ) or "-"
            self.var_lab_damage_stat.set(f"统计中 60s · 目标 {names} · 成功 0")
            return
        if phase == "running":
            left = max(0, int(round(float(update.get("remaining_s") or 0.0))))
            successes = int(update.get("successes") or 0)
            self.var_lab_damage_stat.set(f"统计中 剩余{left}s · 成功{successes}")

    def _lab_finish_damage_item_stat(self, result: dict) -> None:
        """Render one completed 60-second effective-release measurement."""
        self._damage_stat_running = False
        self._damage_stat_stop = None
        try:
            self.btn_lab_damage_stat.configure(state=tk.NORMAL)
        except Exception:
            pass
        if not result.get("ok"):
            error = str(result.get("error") or "统计失败")
            self.var_lab_damage_stat.set(f"伤害物品统计失败：{error}")
            self.log(f"伤害物品统计失败: {error}")
            return

        success = int(result.get("successes") or 0)
        attempts = int(result.get("attempts") or 0)
        elapsed = float(result.get("elapsed_s") or 0.0)
        freq = float(result.get("frequency_per_min") or 0.0)
        self.var_lab_damage_stat.set(
            f"有效释放 {success} 次 / {elapsed:.1f}s = {freq:.1f} 次/分"
            f"（尝试 {attempts} 次）"
        )
        self.var_lab_hang.set(f"伤害物品统计完成：{success}次，{freq:.1f}次/分")
        try:
            import json

            self._lab_set_hang_detail(
                json.dumps(result, ensure_ascii=False, indent=2, default=str)
            )
        except Exception:
            pass
        self.log(
            f"伤害物品统计完成: success={success} elapsed={elapsed:.1f}s "
            f"freq={freq:.1f}/min"
        )

    def _lab_start_dummy_damage_stat(self) -> None:
        """Arm or cancel the passive first-hit plus 60-second dummy counter."""
        if self._dummy_damage_running:
            if self._dummy_damage_stop is not None:
                self._dummy_damage_stop.set()
            self.var_lab_dummy_damage.set("正在停止木人桩伤害统计…")
            try:
                self.btn_lab_dummy_damage.configure(state=tk.DISABLED)
            except Exception:
                pass
            return
        sess = self._lab_require_session()
        if sess is None:
            return
        import threading

        self._dummy_damage_running = True
        self._dummy_damage_stop = threading.Event()
        self.var_lab_dummy_damage.set("正在建立木人桩战斗 UI 基线…")
        try:
            self.btn_lab_dummy_damage.configure(text="停止统计", state=tk.NORMAL)
        except Exception:
            pass

        def _worker() -> None:
            try:
                from app.core.dummy_damage_stats import measure_dummy_damage

                result = measure_dummy_damage(
                    sess,
                    poll_s=0.15,
                    stop_event=self._dummy_damage_stop,
                    on_update=lambda update: self.msg_q.put(
                        ("__LAB_DUMMY_DAMAGE__", update)
                    ),
                    log=lambda message: self.msg_q.put(
                        f"[DUMMY_DAMAGE] {message}"
                    ),
                )
                self.msg_q.put(("__LAB_DUMMY_DAMAGE_DONE__", result))
            except Exception as exc:
                self.msg_q.put(
                    ("__LAB_DUMMY_DAMAGE_DONE__", {"ok": False, "error": str(exc)})
                )

        threading.Thread(
            target=_worker, name="dummy-damage-stat", daemon=True
        ).start()

    def _lab_apply_dummy_damage_stat(self, update: dict) -> None:
        """Render a read-only battle-UI damage snapshot on the Tk thread."""
        from app.core.dummy_damage_stats import format_damage_amount

        phase = str(update.get("phase") or "")
        target = update.get("target") or {}
        distance = target.get("dist")
        dist_text = f" · {float(distance):.1f}m" if distance is not None else ""
        if phase == "waiting":
            self.var_lab_dummy_damage.set(
                f"战斗 UI 基线就绪{dist_text} · 等待第一次木人桩伤害"
            )
            return
        if phase == "running":
            remaining = max(0, int(round(float(update.get("remaining_s") or 0.0))))
            total = int(update.get("total_damage") or 0)
            hits = int(update.get("hits") or 0)
            elapsed = max(0.001, float(update.get("elapsed_ms") or 0) / 1000.0)
            self.var_lab_dummy_damage.set(
                f"统计中 剩余{remaining}s · 总伤害 {format_damage_amount(total)} · "
                f"DPS {format_damage_amount(total / elapsed)}/s · 结算{hits}次"
            )

    def _lab_finish_dummy_damage_stat(self, result: dict) -> None:
        """Finish and render one battle-UI dummy measurement."""
        from app.core.dummy_damage_stats import format_damage_amount

        self._dummy_damage_running = False
        self._dummy_damage_stop = None
        try:
            self.btn_lab_dummy_damage.configure(
                text="统计木人桩 60 秒伤害", state=tk.NORMAL
            )
        except Exception:
            pass
        if not result.get("ok"):
            error = str(result.get("error") or "统计失败")
            self.var_lab_dummy_damage.set(f"木人桩伤害统计失败：{error}")
            self.log(f"木人桩伤害统计失败: {error}")
            return

        total = int(result.get("total_damage") or 0)
        hits = int(result.get("hits") or 0)
        elapsed = float(result.get("elapsed_s") or 0.0)
        dps = float(result.get("dps") or 0.0)
        prefix = "已停止" if result.get("stopped") else "60 秒完成"
        if not result.get("started"):
            prefix = "已停止（尚未受到伤害）"
        self.var_lab_dummy_damage.set(
            f"{prefix} · 总伤害 {format_damage_amount(total)} · "
            f"DPS {format_damage_amount(dps)}/s · "
            f"结算{hits}次 / {elapsed:.1f}s"
        )
        self.log(
            f"木人桩伤害统计: total={total} dps={dps:.1f} "
            f"hits={hits} elapsed={elapsed:.1f}s stopped={bool(result.get('stopped'))}"
        )

    def _lab_write_hang_recover_item(self) -> None:
        """把搜到的首个道具 tid 写入恢复槽（实验，CheckHPItem 路径）。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import (
                find_lab_bag_items,
                set_autoplay_recover_item,
            )
            import json

            key = (self.var_lab_hang_item_key.get() or "").strip() or "伤害物品"
            found = find_lab_bag_items(sess, key, log=self.log)
            items = found.get("items") or []
            if not items:
                self.var_lab_hang.set(f"写入恢复失败: 未找到 {key!r}")
                self._lab_set_hang_detail(
                    json.dumps(found, ensure_ascii=False, indent=2, default=str)
                )
                return
            tid = int(items[0].get("tid") or 0)
            name = items[0].get("name") or ""
            slot = self._lab_parse_recover_slot(tid=tid, name=name, keyword=key)
            # 攻击药丸写入第3格时，若第1格仍是同 tid，清掉避免占 HP 位
            from app.core.activity_auto import (
                clear_autoplay_recover_slot,
                is_autoplay_attack_recover_item,
                read_autoplay_recover,
            )
            if is_autoplay_attack_recover_item(name=name, keyword=key, tid=tid) and slot == 2:
                cur = read_autoplay_recover(sess)
                for s in cur.get("slots") or []:
                    if int(s.get("slot", -1)) == 0 and int(s.get("item_id") or 0) == tid:
                        clear_autoplay_recover_slot(sess, 0, disable=True, log=self.log)
                        break
            ret = set_autoplay_recover_item(
                sess, slot, tid, enabled=True, log=self.log
            )
            ok = bool(ret.get("ok"))
            self.var_lab_hang.set(
                f"写恢复{'OK' if ok else '失败'} · 第{slot+1}格(slot={slot}) "
                f"tid=0x{tid:X} {name!r} · flags={ret.get('flags_after')}"
            )
            detail = {
                "action": "write_recover_slot",
                "keyword": key,
                "picked": items[0],
                "result": ret,
                "tip": "仅内存写入；内挂是否真正使用取决于 CheckHPItem（血/蓝阈值）",
            }
            self._lab_set_hang_detail(
                json.dumps(detail, ensure_ascii=False, indent=2, default=str)
            )
            self.log(f"实验挂机写恢复槽: ok={ok} slot={slot} tid=0x{tid:X} ret={ret}")
        except Exception as e:
            self.var_lab_hang.set(f"写恢复槽失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机写恢复槽失败: {e}")

    def _lab_inject_item_tid_to_skill(self) -> None:
        """对照：道具 tid 写入技能槽（预期无战斗效果）。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import (
                find_lab_bag_items,
                inject_item_tid_to_skill_slot_lab,
            )
            import json

            key = (self.var_lab_hang_item_key.get() or "").strip() or "伤害物品"
            found = find_lab_bag_items(sess, key, log=self.log)
            items = found.get("items") or []
            if not items:
                self.var_lab_hang.set(f"tid→技能失败: 未找到 {key!r}")
                self._lab_set_hang_detail(
                    json.dumps(found, ensure_ascii=False, indent=2, default=str)
                )
                return
            tid = int(items[0].get("tid") or 0)
            ret = inject_item_tid_to_skill_slot_lab(sess, tid, slot=0, log=self.log)
            ok = bool(ret.get("ok"))
            self.var_lab_hang.set(
                f"tid→技能槽{'OK' if ok else '失败'} · tid=0x{tid:X} "
                f"(负对照，不会当技能放)"
            )
            detail = {
                "action": "inject_item_tid_to_skill",
                "picked": items[0],
                "result": ret,
            }
            self._lab_set_hang_detail(
                json.dumps(detail, ensure_ascii=False, indent=2, default=str)
            )
            self.log(f"实验挂机 tid→技能槽: ok={ok} tid=0x{tid:X}")
        except Exception as e:
            self.var_lab_hang.set(f"tid→技能失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机 tid→技能失败: {e}")

    def _lab_patch_hang_radius_ui(self) -> None:

        """patch 设置页半径下限 5→1。 @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.activity_auto import (
                patch_autoplay_radius_ui_min,
                read_autoplay_radius_ui_patch_state,
                set_autoplay_radius,
            )
            import json

            before = read_autoplay_radius_ui_patch_state(sess)
            ret = patch_autoplay_radius_ui_min(sess, 1, log=self.log)
            # re-apply radius=1 so memory stays 1 after user opens settings
            try:
                raw = (self.var_lab_hang_radius.get() or "1").strip()
                want = int(raw)
            except Exception:
                want = 1
            rret = set_autoplay_radius(
                sess, want, allow_below_ui_min=True, log=self.log
            )
            after = read_autoplay_radius_ui_patch_state(sess)
            ok = bool(ret.get("ok"))
            self.var_lab_hang.set(
                f"半径UI patch{'OK' if ok else '失败'} · min={ret.get('new_min')} "
                f"· 内存半径写回={rret.get('radius')}"
            )
            detail = {
                "action": "patch_radius_ui_min",
                "patch": ret,
                "before_state": before,
                "after_state": after,
                "radius_rewrite": rret,
                "tip": "进程级；重开客户端需再点。配合半径=1。",
            }
            pretty = json.dumps(detail, ensure_ascii=False, indent=2, default=str)
            self._lab_set_hang_detail(pretty)
            self.log(f"实验挂机半径UI patch: ok={ok} ret={ret}")
        except Exception as e:
            self.var_lab_hang.set(f"半径UI patch失败: {e}")
            self._lab_set_hang_detail(str(e))
            self.log(f"实验挂机半径UI patch失败: {e}")

    def _lab_probe_input_limit(self) -> None:
        """Read the live maximum from the shown generic numeric dialog."""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.input_limit_lab import probe_shown_input_limit

            ret = probe_shown_input_limit(sess, log=self.log)
            if ret.get("ok"):
                self.var_lab_input_limit_status.set(
                    f"{ret.get('dialog')} · 当前上限={ret.get('limit')} "
                    f"· ctrl=0x{int(ret.get('control_ptr') or 0):X}"
                )
            else:
                self.var_lab_input_limit_status.set(str(ret.get("error") or "读取失败"))
            self.log(f"实验输入上限读取: {ret}")
        except Exception as e:
            self.var_lab_input_limit_status.set(f"读取失败: {e}")
            self.log(f"实验输入上限读取失败: {e}")

    def _lab_patch_input_limit(self) -> None:
        """Set the shown input maximum to the live remaining aptitude."""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.input_limit_lab import set_shown_input_limit_to_remaining

            ret = set_shown_input_limit_to_remaining(sess, log=self.log)
            if ret.get("ok"):
                key = (
                    int(getattr(sess, "pid", 0) or 0),
                    int(ret.get("control_ptr") or 0),
                )
                self._lab_input_limit_original.setdefault(
                    key, int(ret.get("old_limit") or 0)
                )
                self.var_lab_input_limit_status.set(
                    f"最大={ret.get('new_limit')}（剩余资质）"
                )
            else:
                self.var_lab_input_limit_status.set(str(ret.get("error") or "修改失败"))
            self.log(f"实验输入上限修改: {ret}")
        except Exception as e:
            self.var_lab_input_limit_status.set(f"修改失败: {e}")
            self.log(f"实验输入上限修改失败: {e}")

    def _lab_wall_apply(self, enabled: bool, label: str) -> None:
        """Patch the wall-clip byte on/off. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        pid = int(getattr(sess, "pid", 0) or 0)
        if not pid:
            self.var_lab_wall_status.set("无 PID")
            return
        try:
            from app.core.wall_clip import set_wall_clip, wall_clip_state

            set_wall_clip(pid, bool(enabled))
            st = wall_clip_state(pid)
            self.var_lab_wall_status.set(
                f"{label}: byte=0x{0x74 if st else 0x75:02X} state={'开' if st else '关'}"
            )
            self.log(f"穿墙{label}: state={'开' if st else '关'}")
        except Exception as e:  # noqa: BLE001
            self.var_lab_wall_status.set(f"失败: {e}")
            self.log(f"穿墙{label}失败: {e}")

    def _lab_wall_on(self) -> None:
        self._lab_wall_apply(True, "开")

    def _lab_wall_off(self) -> None:
        self._lab_wall_apply(False, "关")

    def _lab_wall_read(self) -> None:
        sess = self._lab_require_session()
        if sess is None:
            return
        pid = int(getattr(sess, "pid", 0) or 0)
        if not pid:
            self.var_lab_wall_status.set("无 PID")
            return
        try:
            from app.core.wall_clip import read_wall_byte, wall_clip_state

            b = read_wall_byte(pid)
            st = wall_clip_state(pid)
            self.var_lab_wall_status.set(
                f"byte=0x{b:02X} state={'开' if st else '关'}"
            )
        except Exception as e:  # noqa: BLE001
            self.var_lab_wall_status.set(f"读取失败: {e}")

    def _lab_gate_set(self, which: str, enabled: bool) -> None:
        """Patch/unpatch a movement gate for the 进墙 experiment.

        which: 'region' (0x86828B), 'ground' (0x86834B) or 'movemap' (0x85DFB0).
        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        pid = int(getattr(sess, "pid", 0) or 0)
        if not pid:
            return
        try:
            from app.core.wall_clip import (
                set_ground_gate,
                set_ground_pos_gate,
                set_region_gate,
            )

            label = {
                "region": "区域闸门",
                "ground": "地面判定",
                "ground_pos": "地面位置",
            }.get(which, which)
            if which == "region":
                set_region_gate(pid, enabled)
            elif which == "ground":
                set_ground_gate(pid, enabled)
            elif which == "ground_pos":
                set_ground_pos_gate(pid, enabled)
            else:
                raise ValueError(f"unknown gate {which}")
            self.log(f"进墙实验 {label}: {'开' if enabled else '关'}")
            self._lab_gate_read()
        except Exception as e:  # noqa: BLE001
            self.log(f"进墙实验失败: {e}")

    def _lab_gate_read(self) -> None:
        """Read and log all movement-gate states. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        pid = int(getattr(sess, "pid", 0) or 0)
        if not pid:
            return
        try:
            from app.core.wall_clip import (
                ground_gate_state,
                ground_pos_gate_state,
                region_gate_state,
            )

            r = region_gate_state(pid)
            g = ground_gate_state(pid)
            gp = ground_pos_gate_state(pid)
            self.log(
                f"进墙实验状态: 区域闸门={'开' if r else '关' if r is False else '未知'} "
                f"· 地面判定={'开' if g else '关' if g is False else '未知'} "
                f"· 地面位置={'开' if gp else '关' if gp is False else '未知'}"
            )
        except Exception as e:  # noqa: BLE001
            self.log(f"读取闸门状态失败: {e}")

    def _lab_tp_read(self) -> None:
        """Read current host pos into the X/Y/Z fields. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.teleport import read_host_pos

            r = read_host_pos(sess, log=self.log)
            if r.get("ok"):
                x, y, z = r["pos"]
                self.var_lab_tp_x.set(f"{x:.2f}")
                self.var_lab_tp_y.set(f"{y:.2f}")
                self.var_lab_tp_z.set(f"{z:.2f}")
                self.var_lab_tp_status.set(f"host=0x{r.get('host', 0):X}")
                self.log(f"当前坐标: ({x:.2f},{y:.2f},{z:.2f})")
            else:
                self.var_lab_tp_status.set(str(r.get("error") or "读取失败"))
        except Exception as e:  # noqa: BLE001
            self.var_lab_tp_status.set(f"读取失败: {e}")

    def _lab_tp_go(self) -> None:
        """Write host +0x158 to the entered XYZ. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            x = float(self.var_lab_tp_x.get())
            y = float(self.var_lab_tp_y.get())
            z = float(self.var_lab_tp_z.get())
        except ValueError:
            self.var_lab_tp_status.set("坐标格式错误")
            return
        try:
            from app.core.teleport import write_host_pos

            r = write_host_pos(sess, (x, y, z), log=self.log)
            if r.get("ok"):
                px, py, pz = r["pos"]
                self.var_lab_tp_status.set(f"已写入 ({px:.1f},{py:.1f},{pz:.1f})")
                self.log(
                    f"传送完成: ({px:.1f},{py:.1f},{pz:.1f}) 服务端可能拉回"
                )
            else:
                self.var_lab_tp_status.set(str(r.get("error") or "传送失败"))
                self.log(f"传送失败: {r}")
        except Exception as e:  # noqa: BLE001
            self.var_lab_tp_status.set(f"传送失败: {e}")

    def _lab_tp_facing(self) -> None:
        """Pathfind forward in the host's current facing direction. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            dist = float(self.var_lab_tp_dist.get())
        except ValueError:
            self.var_lab_tp_status.set("距离格式错误")
            return
        try:
            from app.core.teleport import host_move_facing, read_host_facing

            f = read_host_facing(sess, log=self.log)
            if not f.get("ok"):
                self.var_lab_tp_status.set(str(f.get("error") or "读朝向失败"))
                return
            r = host_move_facing(sess, dist, log=self.log)
            if r.get("ok"):
                tx, ty, tz = r["target"]
                self.var_lab_tp_status.set(f"寻路中 -> ({tx:.1f},{ty:.1f},{tz:.1f})")
                self.log(f"朝朝向寻路: facing={r.get('facing')} target=({tx:.1f},{ty:.1f},{tz:.1f})")
            else:
                self.var_lab_tp_status.set(str(r.get("error") or "寻路失败"))
                self.log(f"朝朝向寻路失败: {r}")
        except Exception as e:  # noqa: BLE001
            self.var_lab_tp_status.set(f"寻路失败: {e}")

    def _lab_tp_auto_toggle(self) -> None:
        """Toggle continuous auto-move in the host's facing direction. @author by ak"""
        now = bool(getattr(self, "_lab_tp_auto_on", False))
        if now:
            self._lab_tp_auto_on = False
            self.var_lab_tp_auto.set("自动移动: 关")
            self.log("自动移动: 已停止")
            return
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            dist = float(self.var_lab_tp_dist.get())
        except ValueError:
            self.var_lab_tp_status.set("距离格式错误")
            return
        self._lab_tp_auto_on = True
        self.var_lab_tp_auto.set("自动移动: 开")
        self.log(f"自动移动: 开启，每段 {dist:.0f}，持续沿朝向移动")

        def worker():
            from app.core.teleport import host_move_facing, read_host_facing

            while getattr(self, "_lab_tp_auto_on", False):
                try:
                    f = read_host_facing(sess, log=self.log)
                    if not f.get("ok"):
                        time.sleep(1.0)
                        continue
                    r = host_move_facing(sess, dist, log=self.log)
                    if not r.get("ok"):
                        self.log(f"自动移动段失败: {r}")
                    time.sleep(2.5)
                except Exception as e:  # noqa: BLE001
                    self.log(f"自动移动异常: {e}")
                    time.sleep(1.0)

        threading.Thread(target=worker, daemon=True).start()

    def _lab_restore_input_limit(self) -> None:
        """Restore the original maximum captured for the currently shown control."""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.input_limit_lab import (
                probe_shown_input_limit,
                set_shown_input_limit,
            )

            current = probe_shown_input_limit(sess, log=self.log)
            if not current.get("ok"):
                self.var_lab_input_limit_status.set(
                    str(current.get("error") or "当前弹窗不可用")
                )
                return
            key = (
                int(getattr(sess, "pid", 0) or 0),
                int(current.get("control_ptr") or 0),
            )
            if key not in self._lab_input_limit_original:
                self.var_lab_input_limit_status.set("当前弹窗没有可恢复的原值")
                return
            original = int(self._lab_input_limit_original[key])
            ret = set_shown_input_limit(sess, original, log=self.log)
            if ret.get("ok"):
                self._lab_input_limit_original.pop(key, None)
                self.var_lab_input_limit_status.set(f"已恢复原上限={original}")
            else:
                self.var_lab_input_limit_status.set(str(ret.get("error") or "恢复失败"))
            self.log(f"实验输入上限恢复: {ret}")
        except Exception as e:
            self.var_lab_input_limit_status.set(f"恢复失败: {e}")
            self.log(f"实验输入上限恢复失败: {e}")

    def _lab_probe_sutra_level_cap(self) -> None:
        """Read the client-side capped-level upgrade branch state."""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.input_limit_lab import probe_sutra_level_cap_patch

            ret = probe_sutra_level_cap_patch(sess, log=self.log)
            if ret.get("ok"):
                state = "已解除" if ret.get("state") == "patched" else "原始限制"
                self.var_lab_sutra_level_cap_status.set(
                    f"{state} · addr=0x{int(ret.get('patch_addr') or 0):X}"
                )
            else:
                self.var_lab_sutra_level_cap_status.set(
                    str(ret.get("error") or "读取失败")
                )
            self.log(f"实验心法等级上限读取: {ret}")
        except Exception as e:
            self.var_lab_sutra_level_cap_status.set(f"读取失败: {e}")
            self.log(f"实验心法等级上限读取失败: {e}")

    def _lab_patch_sutra_level_cap(self) -> None:
        """Allow a capped-level click to reach the original upgrade request."""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.input_limit_lab import set_sutra_level_cap_bypass

            ret = set_sutra_level_cap_bypass(sess, True, log=self.log)
            if ret.get("ok"):
                self.var_lab_sutra_level_cap_status.set(
                    "已解除：等级200点击将继续发送升级请求"
                )
            else:
                self.var_lab_sutra_level_cap_status.set(
                    str(ret.get("error") or "修改失败")
                )
            self.log(f"实验心法等级上限修改: {ret}")
        except Exception as e:
            self.var_lab_sutra_level_cap_status.set(f"修改失败: {e}")
            self.log(f"实验心法等级上限修改失败: {e}")

    def _lab_restore_sutra_level_cap(self) -> None:
        """Restore the original capped-level return branch."""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            from app.core.input_limit_lab import set_sutra_level_cap_bypass

            ret = set_sutra_level_cap_bypass(sess, False, log=self.log)
            if ret.get("ok"):
                self.var_lab_sutra_level_cap_status.set("已恢复原始等级上限")
            else:
                self.var_lab_sutra_level_cap_status.set(
                    str(ret.get("error") or "恢复失败")
                )
            self.log(f"实验心法等级上限恢复: {ret}")
        except Exception as e:
            self.var_lab_sutra_level_cap_status.set(f"恢复失败: {e}")
            self.log(f"实验心法等级上限恢复失败: {e}")


    def _lab_set_team_follow_detail(self, text: str) -> None:
        """Fill team-follow probe detail box. @author by ak"""
        w = getattr(self, "txt_lab_team_follow", None)
        if w is None:
            return
        try:
            w.configure(state=tk.NORMAL)
            w.delete("1.0", tk.END)
            w.insert("1.0", text or "")
            w.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _lab_collect_team_follow_snapshot(self, sess, *, label: str = "") -> dict:
        """Capture role/party/CECTeam bytes for follow experiments. @author by ak"""
        import struct
        from app.core.plg_ui import get_host_team_ptr, host_team_role
        from app.core.remote_runtime import remote_read_bytes
        from app.core.team_ops import (
            format_party_members_label,
            get_host_player_for_team,
            list_party_members,
            read_host_identity,
        )

        out: dict = {"label": label}
        try:
            role = host_team_role(sess, log=self.log) or {}
            out["role"] = role
        except Exception as e:
            out["role_error"] = str(e)
            role = {}
        try:
            host_name, host_id = read_host_identity(sess, log=self.log)
            out["host_name"] = host_name
            out["host_id"] = int(host_id or 0)
        except Exception as e:
            out["host_error"] = str(e)
        try:
            host = get_host_player_for_team(sess, log=self.log)
            out["host_side"] = hex(int(host or 0))
        except Exception as e:
            out["host_side_error"] = str(e)
            host = 0
        try:
            party = list_party_members(
                sess,
                fresh=True,
                log=self.log,
            )
            out["party_count"] = len(party or [])
            out["party"] = format_party_members_label(party)
            out["party_raw"] = party
        except Exception as e:
            out["party_error"] = str(e)
            party = []
        try:
            team = get_host_team_ptr(sess, log=self.log)
            out["team_ptr"] = hex(int(team or 0))
            if team:
                raw = remote_read_bytes(int(sess.pid), int(team), 0x80)
                words = [
                    struct.unpack_from("<I", raw, i)[0] for i in range(0, len(raw), 4)
                ]
                out["team_dump"] = {
                    f"+{i*4:02X}": f"0x{w:08X}" for i, w in enumerate(words)
                }
                # common suspects: small flags near head / after leader id
                out["team_bytes_hex"] = raw.hex()
        except Exception as e:
            out["team_dump_error"] = str(e)
        out["summary"] = (
            f"role={role.get('role') if isinstance(role, dict) else '?'} "
            f"in_team={role.get('in_team') if isinstance(role, dict) else '?'} "
            f"party={out.get('party') or '-'} "
            f"team={out.get('team_ptr') or '0'}"
        )
        return out

    def _lab_probe_team_follow(self) -> None:
        """Read captain/party/CECTeam snapshot for follow debugging. @author by ak"""
        import json

        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            snap = self._lab_collect_team_follow_snapshot(sess, label="probe")
            pretty = json.dumps(snap, ensure_ascii=False, indent=2, default=str)
            self._lab_set_team_follow_detail(pretty)
            self.var_lab_team_follow.set(f"读状态 · {snap.get('summary')}")
            self.log(f"实验组队跟随读状态: {snap.get('summary')}")
            self.log(f"实验组队跟随详情:\n{pretty}")
        except Exception as e:
            self.var_lab_team_follow.set(f"读状态失败: {e}")
            self._lab_set_team_follow_detail(str(e))
            self.log(f"实验组队跟随读状态失败: {e}")

    def _lab_set_team_follow(
        self,
        enabled: bool = True,
    ) -> None:
        """Run the isolated real-UI team-follow path and dump before/after state."""
        import json
        import threading

        sess = self._lab_require_session()
        if sess is None:
            return

        def worker() -> None:
            try:
                from app.core.team_ops import (
                    TeamOpResult,
                    click_team_follow_button,
                )

                before = self._lab_collect_team_follow_snapshot(sess, label="before")
                clicked = click_team_follow_button(
                    sess, bool(enabled), log=self.log
                )
                fr = TeamOpResult(
                    ok=bool(clicked),
                    action="follow_ui_click",
                    message=(
                        "已点击组队跟随确认"
                        if enabled and clicked
                        else "已点击取消跟随"
                        if clicked
                        else "组队跟随 UI 点击失败"
                    ),
                    error=None if clicked else "ui_click_failed",
                    detail={
                        "enabled": bool(enabled),
                        "path": "precise_ui_click",
                    },
                )
                after = self._lab_collect_team_follow_snapshot(
                    sess, label="after_ui_click"
                )
                # byte-level diff on CECTeam dump
                diff = {}
                b_dump = (before.get("team_dump") or {}) if isinstance(before, dict) else {}
                a_dump = (after.get("team_dump") or {}) if isinstance(after, dict) else {}
                keys = sorted(set(b_dump) | set(a_dump))
                for k in keys:
                    bv, av = b_dump.get(k), a_dump.get(k)
                    if bv != av:
                        diff[k] = {"before": bv, "after": av}
                payload = {
                    "trigger": "ui_click",
                    "enabled": bool(enabled),
                    "send_ok": bool(getattr(fr, "ok", False)),
                    "send_message": str(getattr(fr, "message", "") or ""),
                    "send_error": getattr(fr, "error", None),
                    "send_detail": getattr(fr, "detail", None),
                    "before": before,
                    "after": after,
                    "team_dump_diff": diff,
                    "note": "UI 路径的 ok 表示目标控件点击后状态已变化。",
                }
                pretty = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
                label = "开启" if enabled else "关闭"
                trigger_label = "UI"
                ok = bool(getattr(fr, "ok", False))
                ui = (
                    f"{trigger_label}{label}跟随 · {'OK' if ok else '失败'} · "
                    f"diff={len(diff)} · {after.get('summary')}"
                )

                def apply() -> None:
                    try:
                        self.var_lab_team_follow.set(ui)
                    except Exception:
                        pass
                    self._lab_set_team_follow_detail(pretty)
                    self.log(f"实验组队跟随{trigger_label}{label}: {ui}")
                    self.log(f"实验组队跟随详情:\n{pretty}")

                try:
                    self.after(0, apply)
                except Exception:
                    apply()
            except Exception as e:
                def fail() -> None:
                    self.var_lab_team_follow.set(f"跟随实验失败: {e}")
                    self._lab_set_team_follow_detail(str(e))
                    self.log(f"实验组队跟随失败: {e}")

                try:
                    self.after(0, fail)
                except Exception:
                    fail()

        threading.Thread(target=worker, daemon=True).start()

    def _lab_require_session(self):
        """Return attach session or log error. @author by ak"""
        if self.session is None or not getattr(self.session, "pid", None):
            self.log("实验: 请先取句柄挂载")
            self.status.set("NEED_ATTACH")
            return None
        if not getattr(self.session, "module_base", None):
            self.log("实验: 无 module_base")
            return None
        return self.session

    def _lab_once(self) -> None:
        """Single snapshot into log + live line. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        self.status.set("LAB")
        try:
            s = sample_combat_probe(sess, log=self.log, with_dumps=False)
            line = format_sample_line(s)
            self.var_lab_live.set(f"live: {line}")
            self.log(f"实验快照 {line}")
            self.status.set("ATTACHED")
        except Exception as e:
            self.log(f"实验快照失败: {e}")
            self.status.set("ERROR")

    def _lab_sample(self, which: str, dumps: bool = False) -> None:
        """Store sample A or B; optional skill/seq memory dump. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        which = (which or "A").upper()
        try:
            self.log(
                f"实验采样{which} 开始 dumps={dumps} pid={sess.pid} "
                f"base=0x{int(sess.module_base):X}"
            )
            s = sample_combat_probe(sess, log=self.log, with_dumps=bool(dumps))
            line = format_sample_line(s)
            if which == "A":
                self._lab_sample_a = s
                self.var_lab_a.set(f"A: {line}" + (" [dump]" if dumps else ""))
            else:
                self._lab_sample_b = s
                self.var_lab_b.set(f"B: {line}" + (" [dump]" if dumps else ""))
            self.var_lab_live.set(f"live: {line}")
            if dumps:
                self._lab_show_dumps(s)
                self.var_lab_skill.set(
                    f"skill=0x{(s.skill_ptr or 0):X} seq=0x{(s.skill_seq_ptr or 0):X} "
                    f"gs={s.game_state} states={s.host_states_hex or '-'}"
                )
                self.log(
                    f"实验采样{which} dump完成 skill_n={len(s.skill_dump or b'')} "
                    f"seq_n={len(s.seq_dump or b'')} skill=0x{(s.skill_ptr or 0):X} "
                    f"seq=0x{(s.skill_seq_ptr or 0):X}"
                )
                if s.skill_dump:
                    self.log(
                        f"实验采样{which} skill表:\n"
                        + format_dword_table(s.skill_dump, max_rows=16)
                    )
                if s.seq_dump:
                    self.log(
                        f"实验采样{which} seq表:\n"
                        + format_dword_table(s.seq_dump, max_rows=20)
                    )
            self.log(f"实验采样{which} 完成 dumps={dumps} {line}")
            self.status.set("ATTACHED")
        except Exception as e:
            self.log(f"实验采样{which}失败: {e}")
            self.status.set("ERROR")

    def _lab_diff(self) -> None:
        """Diff stored A/B samples (states/pos). @author by ak"""
        a, b = self._lab_sample_a, self._lab_sample_b
        if a is None or b is None:
            self.var_lab_diff.set("diff: 请先采 A 和 B")
            self.log("实验对比: 缺少 A 或 B")
            return
        d = diff_samples(a, b)
        self.var_lab_diff.set(f"diff: {d.note}")
        self.log(f"实验对比 {d.note}")
        if d.states_xor:
            self.log(f"实验 states_xor=0x{d.states_xor:016X}")

    def _lab_diff_dumps(self) -> None:
        """Diff A/B including skill/seq memory dumps; log + write report. @author by ak"""
        a, b = self._lab_sample_a, self._lab_sample_b
        if a is None or b is None:
            self.var_lab_dump_diff.set("内存对比: 请先「闲置采 A」和「施法中采 B」")
            self.log("实验内存对比: 缺少 A 或 B")
            return
        if not (getattr(a, "skill_dump", b"") or getattr(a, "seq_dump", b"")):
            self.var_lab_dump_diff.set("内存对比: A 无 dump，请用「闲置采 A（含 dump）」")
            self.log("实验内存对比: A 无 dump")
            return
        if not (getattr(b, "skill_dump", b"") or getattr(b, "seq_dump", b"")):
            self.var_lab_dump_diff.set("内存对比: B 无 dump，请用「施法中采 B（含 dump）」")
            self.log("实验内存对比: B 无 dump")
            return
        d = diff_samples(a, b)
        note = format_dump_diff_note(d)
        self.var_lab_dump_diff.set("内存对比:\n" + note)
        self.var_lab_diff.set(f"diff: {d.note}")
        self.log("======== 实验内存对比 开始 ========")
        self.log("A " + format_sample_line(a))
        self.log("B " + format_sample_line(b))
        self.log(note.replace("\n", " | "))
        if a.skill_dump and b.skill_dump:
            self.log(
                format_changed_dwords_detail(
                    a.skill_dump, b.skill_dump, d.skill_changed_offs, label="skill"
                )
            )
        if a.seq_dump and b.seq_dump:
            self.log(
                format_changed_dwords_detail(
                    a.seq_dump, b.seq_dump, d.seq_changed_offs, label="seq"
                )
            )
        try:
            path = write_lab_capture_report(a, b, d)
            self.log(f"实验内存对比 已写报告: {path}")
        except Exception as e:
            self.log(f"实验内存对比 写报告失败: {e}")
        self.log("======== 实验内存对比 结束 ========")
        # show B dump as latest
        self._lab_show_dumps(b)

    def _lab_burst_cast(self) -> None:
        """
        After idle A: poll dumps for ~1.6s while user casts; pick first change as B.

        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        a = self._lab_sample_a
        if a is None or not (getattr(a, "skill_dump", b"") or getattr(a, "seq_dump", b"")):
            self.log("实验连采: 请先「闲置采 A（含 dump）」")
            if hasattr(self, "var_lab_dump_diff"):
                self.var_lab_dump_diff.set("连采: 请先闲置采 A（含 dump）")
            return
        self.status.set("LAB_BURST")
        self.log(
            "实验连采: 请在 1～2 秒内放技能；后台将轮询 dump 直到 skill/seq/host/nest 有变化"
        )

        def work() -> None:
            try:
                b, d = burst_sample_until_change(
                    sess, a, log=lambda m: self.msg_q.put(f"[LAB] {m}"), rounds=20, interval_s=0.08
                )
                self.msg_q.put(("__LAB_BURST__", {"b": b, "d": d}))
            except Exception as e:
                self.msg_q.put(("__LAB_BURST__", {"error": str(e)}))

        threading.Thread(target=work, daemon=True).start()

    def _lab_apply_burst(self, payload: dict) -> None:
        """Handle burst result on UI thread. @author by ak"""
        if payload.get("error"):
            self.log(f"实验连采失败: {payload['error']}")
            self.status.set("ERROR")
            return
        b = payload.get("b")
        d = payload.get("d")
        if b is None or d is None:
            self.log(
                "实验连采: 窗口内 skill/seq/host/nest 均无变化 — "
                "请确认已放技能；或锁在未 dump 对象上"
            )
            if hasattr(self, "var_lab_dump_diff"):
                self.var_lab_dump_diff.set("连采: 无变化（未抓到动作锁窗口）")
            self.status.set("ATTACHED")
            return
        self._lab_sample_b = b
        line = format_sample_line(b)
        self.var_lab_b.set(f"B: {line} [burst]")
        self.var_lab_live.set(f"live: {line}")
        self._lab_show_dumps(b)
        note = format_dump_diff_note(d)
        if hasattr(self, "var_lab_dump_diff"):
            self.var_lab_dump_diff.set("连采命中:\n" + note)
        self.var_lab_diff.set(f"diff: {d.note}")
        self.log("======== 实验连采 命中 ========")
        self.log("A " + format_sample_line(self._lab_sample_a))
        self.log("B " + line)
        self.log(note.replace("\n", " | "))
        if d.skill_changed_offs and self._lab_sample_a.skill_dump and b.skill_dump:
            self.log(
                format_changed_dwords_detail(
                    self._lab_sample_a.skill_dump,
                    b.skill_dump,
                    d.skill_changed_offs,
                    label="skill",
                )
            )
        if d.seq_changed_offs and self._lab_sample_a.seq_dump and b.seq_dump:
            self.log(
                format_changed_dwords_detail(
                    self._lab_sample_a.seq_dump,
                    b.seq_dump,
                    d.seq_changed_offs,
                    label="seq",
                )
            )
        if d.host_changed_offs and self._lab_sample_a.host_dump and b.host_dump:
            self.log(
                format_changed_dwords_detail(
                    self._lab_sample_a.host_dump,
                    b.host_dump,
                    d.host_changed_offs,
                    label="host",
                )
            )
        try:
            path = write_lab_capture_report(self._lab_sample_a, b, d)
            self.log(f"实验连采 已写报告: {path}")
        except Exception as e:
            self.log(f"实验连采 写报告失败: {e}")
        self.log("======== 实验连采 结束 ========")
        self.status.set("ATTACHED")

    def _lab_cast_show(self, p) -> None:
        """Show cast-this dump in text box. @author by ak"""
        if not hasattr(self, "txt_lab_cast_dump"):
            return
        if not p or not getattr(p, "cast_dump", b""):
            self._lab_set_text(
                self.txt_lab_cast_dump,
                f"cast_this=0x{(getattr(p, 'cast_this', 0) or 0):X}\n"
                f"err={getattr(p, 'error', None)}\nnote={getattr(p, 'note', '')}\n"
                + (
                    format_dword_table(getattr(p, "host_tail", b"") or b"", max_rows=16)
                    if getattr(p, "host_tail", b"")
                    else "(no dump)"
                ),
            )
            return
        watch = extract_cast_watch(p.cast_dump or b"")
        watch_line = " ".join(f"{k}=0x{v:X}" for k, v in watch.items())
        text = (
            f"cast_this=0x{(p.cast_this or 0):X} host=0x{(p.host_ptr or 0):X} "
            f"inv=0x{(p.inv_this or 0):X}\n"
            f"from_host=0x{(p.cast_this_from_host or 0):X}\n"
            f"watch: {watch_line}\n"
            f"hex: {p.cast_dump_hex}\n"
            + format_dword_table(p.cast_dump, max_rows=24)
        )
        self._lab_set_text(self.txt_lab_cast_dump, text)

    def _lab_cast_resolve(self) -> None:
        """Resolve cast-this once and show dump. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        self.status.set("LAB_CAST")
        try:
            p = resolve_cast_this(sess, log=self.log, with_dumps=True)
            self._lab_cast_a = p
            self.var_lab_cast.set(
                f"cast=0x{(p.cast_this or 0):X} host=0x{(p.host_ptr or 0):X} "
                f"[+1A88]=0x{(p.cast_this_from_host or 0):X} "
                f"[+1A84]=0x{(p.inv_this or 0):X} ok={p.ok} err={p.error}"
            )
            self._lab_cast_show(p)
            self.log(
                f"实验 cast 解析 ok={p.ok} cast=0x{(p.cast_this or 0):X} "
                f"err={p.error}"
            )
            self.status.set("ATTACHED")
        except Exception as e:
            self.log(f"实验 cast 解析失败: {e}")
            self.status.set("ERROR")

    def _lab_cast_sample_a(self) -> None:
        """Idle baseline for cast-this. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            p = resolve_cast_this(sess, log=self.log, with_dumps=True)
            self._lab_cast_a = p
            self.var_lab_cast.set(
                f"A cast=0x{(p.cast_this or 0):X} ok={p.ok} n={len(p.cast_dump or b'')}"
            )
            self._lab_cast_show(p)
            self.log(f"实验 cast A ok={p.ok} cast=0x{(p.cast_this or 0):X}")
            self.status.set("ATTACHED")
        except Exception as e:
            self.log(f"实验 cast A 失败: {e}")
            self.status.set("ERROR")

    def _lab_cast_burst(self) -> None:
        """Burst-poll cast-this after user presses skill. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        a = self._lab_cast_a
        if a is None or not (getattr(a, "cast_dump", b"") or getattr(a, "host_tail", b"")):
            self.log("实验 cast 连采: 请先「闲置采 cast A」或「解析 cast-this」")
            self.var_lab_cast_diff.set("cast: 请先采 A")
            return
        self.status.set("LAB_CAST_BURST")
        self.log("实验 cast 连采: 1～2 秒内放技能…")

        def work() -> None:
            try:
                b, d = burst_cast_this(
                    sess,
                    a,
                    log=lambda m: self.msg_q.put(f"[CAST] {m}"),
                    rounds=48,
                    interval_s=0.05,
                )
                self.msg_q.put(("__LAB_CAST_BURST__", {"b": b, "d": d}))
            except Exception as e:
                self.msg_q.put(("__LAB_CAST_BURST__", {"error": str(e)}))

        threading.Thread(target=work, daemon=True).start()

    def _lab_cast_timeline(self) -> None:
        """Sample cast watch fields for ~3s while user casts. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        self.status.set("LAB_CAST_TL")
        self.log("实验 cast 时间线: 点后立刻放技能，采 ~3s…")

        def work() -> None:
            try:
                frames = timeline_cast_this(
                    sess,
                    log=lambda m: self.msg_q.put(f"[CAST_TL] {m}"),
                    rounds=60,
                    interval_s=0.05,
                )
                self.msg_q.put(("__LAB_CAST_TL__", {"frames": frames}))
            except Exception as e:
                self.msg_q.put(("__LAB_CAST_TL__", {"error": str(e)}))

        threading.Thread(target=work, daemon=True).start()

    def _lab_apply_cast_timeline(self, payload: dict) -> None:
        """UI-thread handle cast timeline. @author by ak"""
        if payload.get("error"):
            self.log(f"实验 cast 时间线失败: {payload['error']}")
            self.status.set("ERROR")
            return
        frames = payload.get("frames") or []
        active = [
            f
            for f in frames
            if f.get("skill_id")
            or f.get("flags")
            or f.get("sess_20c")
            or f.get("sess_200")
            or f.get("skill_id_b")
        ]
        self.log(
            f"======== 实验 cast 时间线 结束 frames={len(frames)} active={len(active)} ========"
        )
        if active:
            first, last = active[0], active[-1]
            self.log(
                f"active t={first.get('t')}..{last.get('t')}s "
                f"id=0x{int(first.get('skill_id') or 0):X} "
                f"flags_first=0x{int(first.get('flags') or 0):X} "
                f"flags_last=0x{int(last.get('flags') or 0):X} "
                f"4a0=0x{int(first.get('extra_4a0') or 0):X}.."
                f"0x{int(last.get('extra_4a0') or 0):X} "
                f"20c=0x{int(first.get('sess_20c') or 0):X}.."
                f"0x{int(last.get('sess_20c') or 0):X}"
            )
            for tag, fr in (("first", first), ("last", last)):
                self.log(
                    f"  {tag}: +18={fr.get('ms_018')} +1C={fr.get('ms_01C')} "
                    f"+20={fr.get('ms_020')} +70={fr.get('ms_070')} "
                    f"+78={fr.get('ms_078')} flags=0x{int(fr.get('flags') or 0):X} "
                    f"idB=0x{int(fr.get('skill_id_b') or 0):X} "
                    f"4a0=0x{int(fr.get('extra_4a0') or 0):X} "
                    f"20c=0x{int(fr.get('sess_20c') or 0):X} "
                    f"200=0x{int(fr.get('sess_200') or 0):X}"
                )
            self.var_lab_cast_diff.set(
                f"tl: active={len(active)} t={first.get('t')}..{last.get('t')}s "
                f"id=0x{int(first.get('skill_id') or 0):X} "
                f"20c=0x{int(last.get('sess_20c') or 0):X}"
            )
        else:
            self.log("时间线无 active 帧（可能没放技能 / 窗口外）")
            self.var_lab_cast_diff.set("tl: no active frames")
        try:
            path = write_cast_timeline_report(frames)
            self.log(f"实验 cast 时间线报告: {path}")
        except Exception as e:
            self.log(f"实验 cast 时间线写报告失败: {e}")
        self.status.set("ATTACHED")

    def _lab_apply_cast_burst(self, payload: dict) -> None:
        """UI-thread handle cast burst. @author by ak"""
        if payload.get("error"):
            self.log(f"实验 cast 连采失败: {payload['error']}")
            self.status.set("ERROR")
            return
        b, d = payload.get("b"), payload.get("d")
        if b is None or d is None:
            self.log("实验 cast 连采: 窗口内无变化")
            self.var_lab_cast_diff.set("cast: 无变化")
            self.status.set("ATTACHED")
            return
        self._lab_cast_b = b
        self.var_lab_cast.set(
            f"B cast=0x{(b.cast_this or 0):X} ok={b.ok} n={len(b.cast_dump or b'')}"
        )
        self.var_lab_cast_diff.set("cast: " + d.get("note", ""))
        self._lab_cast_show(b)
        self.log("======== 实验 cast 连采 命中 ========")
        self.log(d.get("note", ""))
        if d.get("cast_changed_offs") and self._lab_cast_a.cast_dump and b.cast_dump:
            self.log(
                format_changed_dwords_detail(
                    self._lab_cast_a.cast_dump,
                    b.cast_dump,
                    d["cast_changed_offs"],
                    label="cast-this",
                )
            )
        try:
            path = write_cast_capture_report(self._lab_cast_a, b, d)
            self.log(f"实验 cast 报告: {path}")
        except Exception as e:
            self.log(f"实验 cast 写报告失败: {e}")
        self.log("======== 实验 cast 连采 结束 ========")
        self.status.set("ATTACHED")

    def _lab_cast_diff(self) -> None:
        """Diff stored cast A/B. @author by ak"""
        a, b = self._lab_cast_a, self._lab_cast_b
        if a is None or b is None:
            self.var_lab_cast_diff.set("cast: 请先 A 再连采/采 B")
            self.log("实验 cast 对比: 缺 A 或 B")
            return
        d = diff_cast_probes(a, b)
        self.var_lab_cast_diff.set("cast: " + d.get("note", ""))
        self.log("实验 cast 对比 " + d.get("note", ""))
        try:
            path = write_cast_capture_report(a, b, d)
            self.log(f"实验 cast 报告: {path}")
        except Exception as e:
            self.log(f"实验 cast 写报告失败: {e}")

    def _lab_apply_cast_clear(self, r: dict, *, tag: str = "清会话") -> None:
        """Apply clear_cast result to UI. @author by ak"""
        if r.get("skipped_idle"):
            self.var_lab_cast_diff.set("clear: 已是idle，请后摇中再点")
            self.log(f"实验 cast {tag}: {r.get('error')}")
            self.status.set("ATTACHED")
            return
        if not r.get("ok"):
            self.var_lab_cast_diff.set(f"clear: FAIL {r.get('error')}")
            self.log(f"实验 cast {tag}失败: {r.get('error')}")
            self.status.set("ERROR")
            return
        before, after = r.get("before") or {}, r.get("after") or {}
        self.var_lab_cast_diff.set(
            f"clear: id 0x{int(before.get('+10') or 0):X}->0x{int(after.get('+10') or 0):X} "
            f"4a0 0x{int(before.get('+4A0') or 0):X}->0x{int(after.get('+4A0') or 0):X}"
        )
        self.log(
            f"实验 cast {tag} OK "
            f"before id=0x{int(before.get('+10') or 0):X} "
            f"4a0=0x{int(before.get('+4A0') or 0):X} "
            f"wrote=" + ",".join(r.get("wrote") or [])
        )
        sess = self._lab_require_session()
        if sess is not None:
            p = resolve_cast_this(sess, log=self.log, with_dumps=True)
            self._lab_cast_b = p
            self._lab_cast_show(p)
            w = extract_cast_watch(p.cast_dump or b"")
            self.var_lab_cast.set(
                f"cleared cast=0x{(p.cast_this or 0):X} "
                f"id=0x{int(w.get('+10') or 0):X} "
                f"4a0=0x{int(w.get('+4A0') or 0):X} ok={p.ok}"
            )
        self.status.set("ATTACHED")

    def _lab_cast_clear_session(
        self, variant: str = CAST_CANCEL_BUSY_BIT
    ) -> None:
        """
        WriteProcessMemory-clear cast session fields (lab recovery skip test).

        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            r = clear_cast_session(
                sess,
                log=self.log,
                require_active=True,
                variant=variant,
            )
            self._lab_apply_cast_clear(r, tag=f"取消实验[{variant}]")
        except Exception as e:
            self.log(f"实验 cast 清会话异常: {e}")
            self.status.set("ERROR")

    def _lab_cast_clear_when_active(
        self, variant: str = CAST_CANCEL_BUSY_BIT
    ) -> None:
        """Poll until cast active then clear (easier timing). @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        self.status.set("LAB_CAST_CLEAR")
        self.log(
            f"实验 cast 等active再写 variant={variant}: 点后 4s 内放技能…"
        )

        def work() -> None:
            try:
                r = clear_cast_session_when_active(
                    sess,
                    log=lambda m: self.msg_q.put(f"[CAST_CLR] {m}"),
                    rounds=80,
                    interval_s=0.05,
                    variant=variant,
                )
                self.msg_q.put(("__LAB_CAST_CLEAR__", r))
            except Exception as e:
                self.msg_q.put(("__LAB_CAST_CLEAR__", {"ok": False, "error": str(e)}))

        threading.Thread(target=work, daemon=True).start()

    def _lab_on_skill_stopped(self) -> None:
        """
        Bridge-call OnSkillStopped identity-match for lab recovery-skip.

        Sends CMD_ONSKILL_STOPPED via the injected DLL. Native handler
        snapshots cast+0x10/+0x14/+0x18 and calls
        CECHostSkillHdl::OnSkillStopped@0x75F750 on the UI thread so
        0x582350 can clear the identity block (mutating local state only).
        Guarded behind XAJH_ENABLE_UNSAFE_SKILL_WRITE=1 + skill.probe.

        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        from app.core.skill_cast_probe import _unsafe_skill_write_block_reason

        blocked = _unsafe_skill_write_block_reason(sess)
        if blocked:
            self.log(f"OnSkillStopped 实验被阻止: {blocked}")
            self.var_lab_cast_diff.set(f"onskill_stopped: BLOCKED {blocked}")
            return
        from app.core.xajh_bridge import ensure_bridge

        pid = int(sess.pid)
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        bridge = ensure_bridge(
            pid,
            log=self.log,
            inject_if_needed=True,
            hwnd=hwnd,
            force_reinject=False,
        )
        if bridge is None:
            self.log("OnSkillStopped 实验: bridge 不可用")
            self.var_lab_cast_diff.set("onskill_stopped: bridge unavailable")
            return
        self.status.set("LAB_ONSKILL_STOPPED")
        self.log(
            "实验 OnSkillStopped identity-match (桥接): "
            "快照 cast+0x10/14/18 后调用 CECHostSkillHdl::OnSkillStopped…"
        )
        # Read cast-this baseline before the flush
        try:
            before = resolve_cast_this(sess, log=self.log, with_dumps=True)
            bw = extract_cast_watch(before.cast_dump or b"")
        except Exception:
            bw = {}
        result = bridge.on_skill_stopped(
            hwnd=hwnd,
            timeout_ms=3000,
        )
        if not result.ok:
            err = result.error or "bridge error"
            self.log(f"实验 OnSkillStopped FAIL: {err}")
            self.var_lab_cast_diff.set(f"onskill_stopped: FAIL {err}")
            try:
                path = write_onskill_stopped_report(
                    before=bw,
                    after={},
                    bridge_ok=False,
                    bridge_note=str(result.note or ""),
                    bridge_error=str(err),
                    cast_this=int(getattr(before, "cast_this", 0) or 0),
                    host_ptr=int(getattr(before, "host_ptr", 0) or 0),
                )
                self.log(f"实验 OnSkillStopped 报告: {path}")
            except Exception as e:
                self.log(f"实验 OnSkillStopped 报告写入失败: {e}")
            self.status.set("ERROR")
            return
        self.log(
            f"实验 OnSkillStopped identity-match OK note={result.note!r} "
            f"before id=0x{bw.get('+10', 0):X} "
            f"+14=0x{bw.get('+14', 0):X} +18=0x{bw.get('+18', 0):X} "
            f"4a0=0x{bw.get('+4A0', 0):X}"
        )
        # Read after to show the effect
        try:
            after = resolve_cast_this(sess, log=self.log, with_dumps=True)
            aw = extract_cast_watch(after.cast_dump or b"")
        except Exception:
            after = None
            aw = {}
        id_cleared = bool(bw.get("+10")) and int(aw.get("+10") or 0) == 0
        summary = (
            f"onskill_stopped identity-match: before id=0x{bw.get('+10', 0):X} "
            f"+14=0x{bw.get('+14', 0):X} 4a0=0x{bw.get('+4A0', 0):X} "
            f"-> after id=0x{aw.get('+10', 0):X} "
            f"4a0=0x{aw.get('+4A0', 0):X} cleared={id_cleared}"
        )
        self.var_lab_cast_diff.set(summary)
        self.log(f"实验 {summary}")
        try:
            path = write_onskill_stopped_report(
                before=bw,
                after=aw,
                bridge_ok=True,
                bridge_note=str(result.note or ""),
                bridge_error="",
                cast_this=int(getattr(before, "cast_this", 0) or 0),
                host_ptr=int(getattr(before, "host_ptr", 0) or 0),
            )
            self.log(f"实验 OnSkillStopped 报告: {path}")
        except Exception as e:
            self.log(f"实验 OnSkillStopped 报告写入失败: {e}")
        if after is not None:
            self._lab_cast_show(after)
        self.status.set("ATTACHED")

    def _lab_skill_action_trace_once(self) -> None:
        """Run one bounded real-action trace and apply its report on the UI thread."""
        if getattr(self, "_lab_skill_trace_busy", False):
            self.log("技能动作跟踪: 已在运行")
            return
        sess = self._lab_require_session()
        if sess is None:
            self.log("技能动作跟踪: 未挂载会话，请先取句柄")
            return
        label = (self.var_lab_skill_trace_scenario.get() or "").strip()
        scenario = next(
            (key for key, value in SKILL_TRACE_SCENARIO_LABELS.items() if value == label),
            SCENARIO_SAME_SKILL,
        )
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        self._lab_skill_trace_busy = True
        self.status.set("LAB_SKILL_ACTION_TRACE")
        self._lab_recovery_dump_line("", clear=True)
        self._lab_recovery_set_prompt(f"动作跟踪已启动: {label}")
        self._lab_recovery_dump_line(f"==== 动作路径对照 START: {label} ====")
        self.log(f"技能动作跟踪 START scenario={scenario}")

        def work() -> None:
            try:
                result = run_skill_action_trace(
                    sess,
                    scenario,
                    hwnd=hwnd,
                    log=lambda m: self.msg_q.put(f"[SKILL_TRACE] {m}"),
                    prompt_fn=lambda m: self.msg_q.put(
                        ("__LAB_RECOVERY_PROMPT__", str(m))
                    ),
                )
            except Exception as exc:
                result = {
                    "ok": False,
                    "scenario": scenario,
                    "label": label,
                    "error": str(exc),
                    "summary": {},
                }
            self.msg_q.put(("__LAB_SKILL_ACTION_TRACE__", result))

        threading.Thread(
            target=work, daemon=True, name="lab-skill-action-trace"
        ).start()

    def _lab_skill_stable_recovery_once(self) -> None:
        """Run the proven deferred-restop scenario from one lab button."""
        if getattr(self, "_lab_skill_trace_busy", False):
            self.log("稳定去后摇: 已在运行")
            return
        self.var_lab_skill_trace_scenario.set(
            SKILL_TRACE_SCENARIO_LABELS[SCENARIO_SAME_SKILL]
        )
        self._lab_skill_action_trace_once()

    def _lab_skill_charged_recovery_once(self) -> None:
        """Run the charge/channel auto-continuation suppression scenario."""
        if getattr(self, "_lab_skill_trace_busy", False):
            self.log("蓄力/持续去后摇: 已在运行")
            return
        self.var_lab_skill_trace_scenario.set(
            SKILL_TRACE_SCENARIO_LABELS[SCENARIO_CHARGED_CHANNEL]
        )
        self._lab_skill_action_trace_once()

    def _lab_youfeng_chain_toggle(self) -> None:
        """Start/stop the process-local 6 -> X -> Space chain runner."""
        runner = getattr(self, "_lab_youfeng_chain_runner", None)
        if runner is not None and runner.is_running():
            runner.stop()
            self.var_lab_youfeng_chain.set("有凤宏对照: OFF")
            self._lab_recovery_set_prompt("有凤连发: 已停止")
            return
        for attr in (
            "_lab_youfeng_internal_runner",
            "_lab_youfeng_gate_runner",
            "_lab_youfeng_ultimate_runner",
            "_lab_youfeng_ultimate_spam_runner",
        ):
            other = getattr(self, attr, None)
            if other is not None and other.is_running():
                other.stop()
        sess = self._lab_require_session()
        if sess is None:
            self.log("有凤连发: 未挂载会话，请先取句柄")
            return
        pid = int(getattr(sess, "pid", 0) or 0)
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        if not pid:
            self.log("有凤连发: pid 无效")
            return
        runner = YoufengChainRunner(
            pid,
            hwnd,
            log=lambda m: self.msg_q.put(f"[YOUFENG] {m}"),
            on_status=lambda m: self.msg_q.put(
                ("__LAB_YOUFENG_CHAIN_STATUS__", str(m))
            ),
        )
        self._lab_youfeng_chain_runner = runner
        if runner.start():
            self.var_lab_youfeng_chain.set("有凤宏对照: ON")
            self._lab_recovery_set_prompt("有凤连发: 启动中")
        else:
            self.var_lab_youfeng_chain.set("有凤宏对照: OFF")

    def _lab_youfeng_internal_toggle(self) -> None:
        """Start/stop the keyless internal 0x67 chain runner."""
        runner = getattr(self, "_lab_youfeng_internal_runner", None)
        if runner is not None and runner.is_running():
            runner.stop()
            self.var_lab_youfeng_internal.set("有凤E07对照: OFF")
            self._lab_recovery_set_prompt("有凤E07对照: 已停止")
            return
        for attr in (
            "_lab_youfeng_chain_runner",
            "_lab_youfeng_gate_runner",
            "_lab_youfeng_ultimate_runner",
            "_lab_youfeng_ultimate_spam_runner",
        ):
            other = getattr(self, attr, None)
            if other is not None and other.is_running():
                other.stop()
        sess = self._lab_require_session()
        if sess is None:
            self.log("有凤内断连发: 未挂载会话，请先取句柄")
            return
        pid = int(getattr(sess, "pid", 0) or 0)
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        if not pid:
            self.log("有凤内断连发: pid 无效")
            return
        runner = YoufengChainRunner(
            pid,
            hwnd,
            interrupt_mode=INTERRUPT_INTERNAL67,
            log=lambda m: self.msg_q.put(f"[YOUFENG67] {m}"),
            on_status=lambda m: self.msg_q.put(
                ("__LAB_YOUFENG_INTERNAL_STATUS__", str(m))
            ),
        )
        self._lab_youfeng_internal_runner = runner
        if runner.start():
            self.var_lab_youfeng_internal.set("有凤E07对照: ON")
            self._lab_recovery_set_prompt("有凤E07对照: 启动中")
        else:
            self.var_lab_youfeng_internal.set("有凤E07对照: OFF")

    def _lab_youfeng_gate_toggle(self) -> None:
        """Start/stop the native QingGong pre-cast experiment."""
        runner = getattr(self, "_lab_youfeng_gate_runner", None)
        if runner is not None and runner.is_running():
            runner.stop()
            self.var_lab_youfeng_gate.set("有凤轻功抢招: OFF")
            self._lab_recovery_set_prompt("有凤轻功抢招: 已停止")
            return
        for attr in (
            "_lab_youfeng_chain_runner",
            "_lab_youfeng_internal_runner",
            "_lab_youfeng_ultimate_runner",
            "_lab_youfeng_ultimate_spam_runner",
        ):
            other = getattr(self, attr, None)
            if other is not None and other.is_running():
                other.stop()
        sess = self._lab_require_session()
        if sess is None:
            self.log("有凤轻功抢招: 未挂载会话，请先取句柄")
            return
        pid = int(getattr(sess, "pid", 0) or 0)
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        if not pid:
            self.log("有凤轻功抢招: pid 无效")
            return
        runner = YoufengChainRunner(
            pid,
            hwnd,
            interrupt_mode=INTERRUPT_QINGGONG_PRECAST,
            log=lambda m: self.msg_q.put(f"[YOUFENG-GATE] {m}"),
            on_status=lambda m: self.msg_q.put(
                ("__LAB_YOUFENG_GATE_STATUS__", str(m))
            ),
        )
        self._lab_youfeng_gate_runner = runner
        if runner.start():
            self.var_lab_youfeng_gate.set("有凤轻功抢招: ON")
            self._lab_recovery_set_prompt("有凤轻功抢招: 启动中")
        else:
            self.var_lab_youfeng_gate.set("有凤轻功抢招: OFF")

    def _lab_youfeng_ultimate_toggle(self) -> None:
        """Start/stop automatic ultimate + normal Youfeng experiment."""
        runner = getattr(self, "_lab_youfeng_ultimate_runner", None)
        if runner is not None and runner.is_running():
            runner.stop()
            self.var_lab_youfeng_ultimate.set("真绝+普通循环: OFF")
            self._lab_recovery_set_prompt("真绝+普通循环: 已停止")
            return
        for attr in (
            "_lab_youfeng_chain_runner",
            "_lab_youfeng_internal_runner",
            "_lab_youfeng_gate_runner",
            "_lab_youfeng_ultimate_spam_runner",
        ):
            other = getattr(self, attr, None)
            if other is not None and other.is_running():
                other.stop()
        sess = self._lab_require_session()
        if sess is None:
            self.log("真绝+普通循环: 未挂载会话，请先取句柄")
            return
        pid = int(getattr(sess, "pid", 0) or 0)
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        if not pid:
            self.log("真绝+普通循环: pid 无效")
            return
        runner = YoufengChainRunner(
            pid,
            hwnd,
            interrupt_mode=INTERRUPT_QINGGONG_PULSE,
            include_ultimate=True,
            pulse_stop_ms=0,
            log=lambda m: self.msg_q.put(f"[YOUFENG-ULT] {m}"),
            on_status=lambda m: self.msg_q.put(
                ("__LAB_YOUFENG_ULTIMATE_STATUS__", str(m))
            ),
        )
        self._lab_youfeng_ultimate_runner = runner
        if runner.start():
            self.var_lab_youfeng_ultimate.set("真绝+普通循环: ON")
            self._lab_recovery_set_prompt("真绝+普通循环: 启动中")
        else:
            self.var_lab_youfeng_ultimate.set("真绝+普通循环: OFF")

    def _lab_youfeng_ultimate_spam_toggle(self) -> None:
        """Repeat full ultimate casts with native tail cancel and exact local CD clear."""
        runner = getattr(self, "_lab_youfeng_ultimate_spam_runner", None)
        if runner is not None and runner.is_running():
            runner.stop()
            self.var_lab_youfeng_ultimate_spam.set("真绝无后摇连发: OFF")
            self._lab_recovery_set_prompt("真绝无后摇连发: 已停止")
            return
        for attr in (
            "_lab_youfeng_chain_runner",
            "_lab_youfeng_internal_runner",
            "_lab_youfeng_gate_runner",
            "_lab_youfeng_ultimate_runner",
        ):
            other = getattr(self, attr, None)
            if other is not None and other.is_running():
                other.stop()
        sess = self._lab_require_session()
        if sess is None:
            self.log("真绝无后摇连发: 未挂载会话，请先取句柄")
            return
        pid = int(getattr(sess, "pid", 0) or 0)
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        if not pid:
            self.log("真绝无后摇连发: pid 无效")
            return
        runner = YoufengChainRunner(
            pid,
            hwnd,
            interrupt_mode=INTERRUPT_QINGGONG_PULSE,
            ultimate_spam=True,
            pulse_stop_ms=0,
            log=lambda m: self.msg_q.put(f"[YOUFENG-ULT-SPAM] {m}"),
            on_status=lambda m: self.msg_q.put(
                ("__LAB_YOUFENG_ULTIMATE_SPAM_STATUS__", str(m))
            ),
        )
        self._lab_youfeng_ultimate_spam_runner = runner
        if runner.start():
            self.var_lab_youfeng_ultimate_spam.set("真绝无后摇连发: ON")
            self._lab_recovery_set_prompt("真绝无后摇连发: 启动中")
        else:
            self.var_lab_youfeng_ultimate_spam.set("真绝无后摇连发: OFF")

    def _lab_apply_youfeng_chain_status(self, message: str) -> None:
        runner = getattr(self, "_lab_youfeng_chain_runner", None)
        running = bool(runner and runner.is_running())
        if "已停止" in str(message) or "停机" in str(message) or (
            not running and "已启动" not in str(message)
        ):
            self.var_lab_youfeng_chain.set("有凤宏对照: OFF")
        self._lab_recovery_set_prompt(str(message))
        self.log(str(message))

    def _lab_apply_youfeng_internal_status(self, message: str) -> None:
        runner = getattr(self, "_lab_youfeng_internal_runner", None)
        running = bool(runner and runner.is_running())
        if "已停止" in str(message) or "停机" in str(message) or (
            not running and "已启动" not in str(message)
        ):
            self.var_lab_youfeng_internal.set("有凤E07对照: OFF")
        self._lab_recovery_set_prompt(str(message))
        self.log(str(message))

    def _lab_apply_youfeng_gate_status(self, message: str) -> None:
        runner = getattr(self, "_lab_youfeng_gate_runner", None)
        running = bool(runner and runner.is_running())
        if "已停止" in str(message) or "停机" in str(message) or (
            not running and "已启动" not in str(message)
        ):
            self.var_lab_youfeng_gate.set("有凤轻功抢招: OFF")
        self._lab_recovery_set_prompt(str(message))
        self.log(str(message))

    def _lab_apply_youfeng_ultimate_status(self, message: str) -> None:
        runner = getattr(self, "_lab_youfeng_ultimate_runner", None)
        running = bool(runner and runner.is_running())
        if "已停止" in str(message) or "停机" in str(message) or (
            not running and "已启动" not in str(message)
        ):
            self.var_lab_youfeng_ultimate.set("真绝+普通循环: OFF")
        self._lab_recovery_set_prompt(str(message))
        self.log(str(message))

    def _lab_apply_youfeng_ultimate_spam_status(self, message: str) -> None:
        runner = getattr(self, "_lab_youfeng_ultimate_spam_runner", None)
        running = bool(runner and runner.is_running())
        if "已停止" in str(message) or "停机" in str(message) or (
            not running and "已启动" not in str(message)
        ):
            self.var_lab_youfeng_ultimate_spam.set("真绝无后摇连发: OFF")
        self._lab_recovery_set_prompt(str(message))
        self.log(str(message))

    def _lab_apply_skill_action_trace(self, result: dict) -> None:
        if not isinstance(result, dict):
            result = {}
        self._lab_skill_trace_busy = False
        summary = result.get("summary") or {}
        verdict = str(summary.get("verdict") or "trace_failed")
        verdict_cn = {
            "entered_new_active_session": "已建立新的真实技能会话",
            "action_request_rejected": "重按已进入真实动作请求，但返回前未建立新技能会话",
            "perform_suppressed_no_request": "旧 OnPerform 已被抑制，但重按仍未进入真实请求",
            "perform_refill_only": "只有首次技能的 OnPerform 回填，没有第二次技能启动",
            "cancast_only_no_active": "仅观察到 CanCast 查询，未建立真实技能会话",
            "outer_only_no_active": "仅到 CanCast 外层，未建立真实技能会话",
            "no_action_request_or_active": "未观察到真实动作请求或新技能会话",
            "trace_incomplete": "调用链不完整，需要复测",
        }.get(verdict, verdict)
        line = (
            f"结果: {verdict_cn} | outer={summary.get('outer_after')} "
            f"core={summary.get('core_after')} active={summary.get('active_after')} "
            f"request={summary.get('request_after')} "
            f"start={summary.get('session_start_after')} refill={summary.get('perform_refill_after')} "
            f"suppressed={summary.get('suppressed_refill_after')} "
            f"perform_suppressed={summary.get('suppressed_perform_after')} "
            f"restop={summary.get('restop_after')} "
            f"ret={summary.get('core_return_counts') or summary.get('core_returns')}"
        )
        strict = result.get("strict_recovery") or {}
        if strict:
            line += (
                f" | strict={'PASS' if strict.get('pass') else 'FAIL'}"
                f"({strict.get('verdict')})"
                f" old={strict.get('old_suppressed_perform')}/"
                f"{strict.get('old_successful_restop')}"
                f" fresh={strict.get('fresh_request')}/"
                f"{strict.get('fresh_session')}/"
                f"{strict.get('fresh_perform')}"
            )
        if result.get("error"):
            line += f" | err={result.get('error')}"
        self._lab_recovery_set_prompt(line)
        self._lab_recovery_dump_line(line)
        self.var_lab_cast_diff.set(line[:220])
        paths = result.get("paths") or {}
        if paths.get("md"):
            self._lab_recovery_dump_line(f"报告: {paths['md']}")
            self.log(f"技能动作跟踪报告: {paths['md']}")
        if paths.get("json"):
            self.log(f"技能动作跟踪 JSON: {paths['json']}")
        self.log(line)
        strict_failed = bool(strict) and not bool(strict.get("pass"))
        self.status.set(
            "ATTACHED" if result.get("ok") and not strict_failed else "ERROR"
        )

    def _lab_recovery_method_key(self) -> str:
        """Map UI label back to method key."""
        label = (self.var_lab_recovery_method.get() or "").strip()
        for k, v in METHOD_LABELS.items():
            if v == label:
                return k
        return METHOD_ONSKILL


    def _lab_hand_probe(self) -> None:
        """Lab: user hand-presses X/Space; snapshot only (no inject). @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        hwnd = 0
        try:
            hwnd = int(getattr(self, "hwnd", 0) or 0)
        except Exception:
            hwnd = 0
        try:
            # same hwnd source as other labs
            if hasattr(self, "_current_hwnd"):
                hwnd = int(self._current_hwnd() or hwnd or 0)
        except Exception:
            pass
        try:
            hwnd = int(getattr(sess, "hwnd", 0) or hwnd or 0)
        except Exception:
            pass
        self.log(
            "======== 手按采样 ======== 先放技能 → 黄字提示后【手按 X 再空格】"
            "（工具不注入，抓通用清后摇差分）"
        )
        try:
            self._lab_recovery_set_prompt("【手按采样】请先放一个技能（等出招）…")
        except Exception:
            pass
        self.var_lab_suppress_status.set("采样: 等出招")

        def work() -> None:
            try:
                r = run_hand_interrupt_probe(
                    sess,
                    log=lambda m: self.msg_q.put(f"[PROBE] {m}"),
                    hwnd=hwnd,
                    status_fn=lambda m: self.msg_q.put(
                        ("__LAB_SUPPRESS_STATUS__", m)
                    ),
                    prompt_fn=lambda m: self.msg_q.put(
                        ("__LAB_RECOVERY_PROMPT__", str(m))
                    ),
                )
                self.msg_q.put(("__LAB_HAND_PROBE_DONE__", r))
            except Exception as e:
                self.msg_q.put(
                    ("__LAB_HAND_PROBE_DONE__", {"ok": False, "error": str(e)})
                )

        import threading

        threading.Thread(target=work, daemon=True, name="lab-hand-probe").start()

    def _lab_apply_hand_probe_done(self, r: dict) -> None:
        if not isinstance(r, dict):
            r = {}
        note = r.get("note") or r.get("error") or ""
        paths = r.get("paths") or {}
        self.log(f"实验 hand_probe: {note}")
        if paths.get("md"):
            self.log(f"实验 hand_probe 报告: {paths.get('md')}")
        if paths.get("json"):
            self.log(f"实验 hand_probe JSON: {paths.get('json')}")
        try:
            self._lab_recovery_set_prompt(f"手按采样结束: {str(note)[:90]}")
        except Exception:
            pass
        self.var_lab_suppress_status.set("采样: 结束")

    def _lab_suppress_toggle(self) -> None:
        """Lab-only continuous recovery suppress ON/OFF. @author by ak"""
        want = bool(self.var_lab_suppress_on.get())
        if want:
            self._lab_suppress_start()
        else:
            self._lab_suppress_stop_now(reason="user_off")

    def _lab_suppress_stop_now(self, reason: str = "stop") -> None:
        ev = getattr(self, "_lab_suppress_stop", None)
        if ev is not None:
            try:
                ev.set()
            except Exception:
                pass
        self._lab_suppress_stop = None
        try:
            self.var_lab_suppress_on.set(False)
        except Exception:
            pass
        try:
            self.var_lab_suppress_status.set(f"压制: OFF ({reason})")
        except Exception:
            pass
        self.log(f"持续压制: OFF ({reason})")

    def _lab_suppress_start(self) -> None:
        """Start background suppress loop. @author by ak"""
        # stop previous if any
        old = getattr(self, "_lab_suppress_stop", None)
        if old is not None and not old.is_set():
            self.log("持续压制: 已在运行")
            try:
                self.var_lab_suppress_on.set(True)
            except Exception:
                pass
            return
        sess = self._lab_require_session()
        if sess is None:
            self.var_lab_suppress_on.set(False)
            self.log("持续压制: 未挂载会话，请先取句柄")
            self.var_lab_suppress_status.set("压制: OFF 无会话")
            return
        import threading

        stop_ev = threading.Event()
        self._lab_suppress_stop = stop_ev
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        from app.core.skill_cast_probe import _unsafe_skill_write_block_reason
        blocked = _unsafe_skill_write_block_reason(sess)
        if blocked:
            self.var_lab_suppress_on.set(False)
            self.var_lab_suppress_status.set(f"压制: OFF 门禁")
            self.log(f"持续压制: 【未开权限】{blocked}")
            try:
                self._lab_recovery_set_prompt(
                    f"【未开权限】持续压制需要: {blocked}"
                )
            except Exception:
                pass
            return

        if bool(self.var_lab_suppress_full.get()):
            mode = "full"
            mode_cn = "含OnSkill强拆"
        elif bool(getattr(self, "var_lab_suppress_key", None) and self.var_lab_suppress_key.get()):
            mode = "key"
            mode_cn = "X+空格(等后摇再断)"
        else:
            mode = "soft"
            mode_cn = "软本地WPM"
        self.var_lab_suppress_status.set(f"压制: ON v4.7/{mode} 启动中…")
        self.log(
            f"======== 持续压制 ON v4.7 mode={mode} ======== "
            f"{mode_cn}；点按放技能(勿长按)；再点开关关闭"
        )
        self.status.set("LAB_SUPPRESS_ON")
        try:
            self._lab_recovery_set_prompt(
                f"持续压制 ON v4.7/{mode} — 点按技能后立刻再点；{mode_cn}"
            )
        except Exception:
            pass

        def work() -> None:
            try:
                stats = run_recovery_suppress_loop(
                    sess,
                    stop_fn=stop_ev.is_set,
                    log=lambda m: self.msg_q.put(f"[SUPPRESS] {m}"),
                    hwnd=hwnd,
                    poll_s=0.010,
                    min_full_s=0.55,
                    min_stop65_s=0.10,
                    min_strip_s=0.03,
                    mode=mode,
                    status_fn=lambda m: self.msg_q.put(("__LAB_SUPPRESS_STATUS__", m)),
                )
                self.msg_q.put(("__LAB_SUPPRESS_DONE__", stats))
            except Exception as e:
                self.msg_q.put(
                    ("__LAB_SUPPRESS_DONE__", {"ok": False, "error": str(e)})
                )

        th = threading.Thread(target=work, daemon=True, name="lab-suppress")
        self._lab_suppress_thread = th
        th.start()

    def _lab_apply_suppress_status(self, text: str) -> None:
        try:
            self.var_lab_suppress_status.set(str(text or ""))
        except Exception:
            pass

    def _lab_apply_suppress_done(self, stats: dict) -> None:
        if not isinstance(stats, dict):
            stats = {}
        err = stats.get("error") or ""
        summary = (
            f"压制结束 mode={stats.get('mode')} edges={stats.get('edges')} "
            f"key={stats.get('key')} soft={stats.get('soft')} full={stats.get('full')} "
            f"onskill={stats.get('onskills')} stop65={stats.get('stop65')} "
            f"strip={stats.get('strips')} gate={stats.get('gate_clears')}"
        )
        if err:
            summary += f" err={err!r}"
        self.log(f"实验 {summary}")
        self.log(
            "压制已停。若 key 模式仍不能立刻下招：把 POST_DIAG 贴回来。"
            "本地 soft 已证伪；勿再指望纯 WPM。"
        )
        try:
            self.var_lab_suppress_on.set(False)
            self.var_lab_suppress_status.set(
                f"压制: OFF e={stats.get('edges')} s={stats.get('strips')}"
            )
        except Exception:
            pass
        if self.status.get() == "LAB_SUPPRESS_ON":
            self.status.set("ATTACHED")


    def _lab_recovery_set_prompt(self, text: str) -> None:
        """Show one-line recovery guidance in the cast-this panel. @author by ak"""
        msg = str(text or "").strip()
        if not msg:
            return
        try:
            if hasattr(self, "var_lab_recovery_prompt"):
                self.var_lab_recovery_prompt.set(msg)
        except Exception:
            pass
        try:
            if hasattr(self, "var_lab_cast_diff"):
                self.var_lab_cast_diff.set(msg[:220])
        except Exception:
            pass

    def _lab_recovery_dump_line(self, text: str, *, clear: bool = False) -> None:
        """Append recovery progress into the cast dump box (same panel). @author by ak"""
        msg = str(text or "").rstrip()
        if not msg and not clear:
            return
        box = getattr(self, "txt_lab_cast_dump", None)
        if box is None:
            return
        try:
            box.configure(state=tk.NORMAL)
            if clear:
                box.delete("1.0", tk.END)
            if msg:
                box.insert(tk.END, msg + "\n")
                box.see(tk.END)
            box.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _lab_recovery_live(self, raw: str) -> None:
        """Map recovery_lab log lines into big Chinese prompts. @author by ak"""
        s = str(raw or "")
        if s.startswith("[RECOVERY] "):
            s = s[len("[RECOVERY] "):]
        low = s.lower()
        prompt = None
        if ("【现在放技能】" in s) or ("idle baseline ok" in low) or ("cast now" in low and "wait" not in low):
            prompt = "【现在放技能】空闲基线OK — 马上按技能键！"
        elif ("【站着别放】" in s) or ("wait rising-edge" in low) or ("wait armed" in low):
            prompt = "① 站着别放技能 — 等空闲基线…"
        elif "wait-idle" in low:
            prompt = "① 站着别放 — 等待角色空闲（id/busy/type 干净）…"
        elif "wait-edge" in low:
            prompt = "【现在放技能】已空闲 — 马上按技能键！"
        elif ("【已抓住】" in s) or ("hit[" in low):
            prompt = f"③ 已抓住技能，正在取消后摇…  {s[:100]}"
        elif "recovery_lab: fire" in low:
            prompt = f"③ 开火处理中…  {s[:100]}"
        elif "cast a skill now" in low or "cast now" in low:
            prompt = "【现在放技能】马上按技能键！"
        elif "matrix" in low and ("cast a skill" in low or "next=" in low):
            prompt = "【矩阵】下一方法 — 现在放技能！"
        elif "timeout" in low or "fail:" in low:
            prompt = f"失败: {s[:160]}"
        if prompt:
            self._lab_recovery_set_prompt(prompt)
        self._lab_recovery_dump_line(s)
        self.log(f"[RECOVERY] {s}")

    def _lab_recovery_auto_once(self) -> None:
        """
        Auto lab: wait recovery window, fire one method, score local fields.

        User only casts a skill after clicking. Lab-only, no product promote.
        @author by ak
        """
        if getattr(self, "_lab_recovery_busy", False):
            self.log("自动后摇实验: 检测到忙锁，已强制重置（可再点）")
            self._lab_recovery_busy = False
        sess = self._lab_require_session()
        if sess is None:
            self.log("自动后摇实验: 未挂载会话，请先取句柄")
            return
        from app.core.skill_cast_probe import _unsafe_skill_write_block_reason
        blocked = _unsafe_skill_write_block_reason(sess)
        if blocked:
            msg = f"【未开权限】{blocked} — 请设环境变量后重启工具"
            try:
                self._lab_recovery_set_prompt(msg)
                self._lab_recovery_dump_line(msg, clear=True)
            except Exception:
                pass
            self.log(f"自动后摇实验: {msg}")
            return
        method = self._lab_recovery_method_key()
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        self._lab_recovery_busy = True
        self.status.set("LAB_RECOVERY")
        label = METHOD_LABELS.get(method, method)
        self._lab_recovery_dump_line("", clear=True)
        self._lab_recovery_set_prompt(
            f"① 已开始 [{label}] — 先站着别放，等提示【现在放技能】"
        )
        self._lab_recovery_dump_line(
            f"==== 自动后摇 START method={label} ===="
        )
        self._lab_recovery_dump_line(
            "步骤: 空闲基线 → 【现在放技能】→ 抓住后自动取消 → 看结果"
        )
        self.log(f"自动后摇实验: method={label} — 看面板黄字提示，不要只看底部日志")


        def work() -> None:
            try:
                trial = run_recovery_trial(
                    sess,
                    method,
                    log=lambda m: self.msg_q.put(f"[RECOVERY] {m}"),
                    hwnd=hwnd,
                    wait_rounds=500,
                    interval_s=0.01,
                )
                self.msg_q.put(("__LAB_RECOVERY__", trial))
            except Exception as e:
                self.msg_q.put(
                    ("__LAB_RECOVERY__", {"ok": False, "error": str(e), "method": method})
                )
            finally:
                self.msg_q.put(("__LAB_RECOVERY_DONE__", None))

        threading.Thread(target=work, daemon=True).start()

    def _lab_recovery_auto_matrix(self) -> None:
        """
        Auto matrix of recovery candidates (4 anti-refill variants).

        User must recast for each method when log prompts CAST NOW.
        @author by ak
        """
        # Force-reset stuck busy from a previous hung trial so the button
        # never appears dead.
        if getattr(self, "_lab_recovery_busy", False):
            self.log("自动后摇矩阵: 检测到上次未清忙锁，已强制重置后重开")
            self._lab_recovery_busy = False
        sess = self._lab_require_session()
        if sess is None:
            self.log("自动后摇矩阵: 未挂载会话，请先取句柄")
            return
        from app.core.skill_cast_probe import _unsafe_skill_write_block_reason
        blocked = _unsafe_skill_write_block_reason(sess)
        if blocked:
            msg = f"【未开权限】{blocked} — 请设环境变量后重启工具"
            try:
                self._lab_recovery_set_prompt(msg)
                self._lab_recovery_dump_line(msg, clear=True)
            except Exception:
                pass
            self.log(f"自动后摇矩阵: {msg}")
            return
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        self._lab_recovery_busy = True
        self.status.set("LAB_RECOVERY_MATRIX")
        self._lab_recovery_dump_line("", clear=True)
        self._lab_recovery_set_prompt(
            "【矩阵】开始 — 每次提示【现在放技能】时放一技能"
        )
        self._lab_recovery_dump_line("==== 自动后摇矩阵 START ====")
        self._lab_recovery_dump_line(
            "每方法一轮：空闲 → 【现在放技能】→ 自动取消 → 下一方法"
        )
        self.log("======== 自动后摇矩阵 START ======== 看面板黄字提示")


        def work() -> None:
            try:
                matrix = run_recovery_matrix(
                    sess,
                    RECOVERY_CANDIDATES,
                    log=lambda m: self.msg_q.put(f"[RECOVERY] {m}"),
                    hwnd=hwnd,
                    wait_rounds=500,
                    interval_s=0.01,
                )
                self.msg_q.put(("__LAB_RECOVERY_MATRIX__", matrix))
            except Exception as e:
                self.msg_q.put(
                    ("__LAB_RECOVERY_MATRIX__", {"ok": False, "error": str(e)})
                )
            finally:
                self.msg_q.put(("__LAB_RECOVERY_DONE__", None))

        threading.Thread(target=work, daemon=True).start()

    def _lab_apply_recovery_trial(self, trial: dict) -> None:
        """UI-thread apply single recovery trial. @author by ak"""
        if not isinstance(trial, dict):
            return
        method = trial.get("method") or "?"
        label = trial.get("label") or method
        score = trial.get("score") or {}
        verdict = score.get("verdict") or ("FAIL" if not trial.get("ok") else "?")
        err = trial.get("error") or ""
        summary = (
            f"recovery {label}: {verdict} "
            f"id_imm={score.get('id_cleared_immediate')} "
            f"id_late={score.get('id_cleared')} "
            f"reverted={score.get('id_reverted')} "
            f"refill_t={score.get('id_refill_t_s')} "
            f"busy={score.get('busy_cleared')} "
            f"native={score.get('native_cleared')} "
            f"perform_cleared={score.get('perform_cleared')} "
            f"ptype={score.get('perform_type_before')}->{score.get('perform_type_after')} "
            f"err={err!r}"
        )
        hit = trial.get("hit_meta") or {}
        result_line = (
            f"④ 结果: {verdict} | imm_id={score.get('id_cleared_immediate')} "
            f"late_id={score.get('id_cleared')} refill={score.get('id_refill_t_s')} "
            f"perf={score.get('perform_type_before')}->{score.get('perform_type_after')} "
            f"busy0={hit.get('busy')} type={hit.get('ptype')} +20=0x{int(hit.get('p20') or 0):X}"
        )
        if err:
            result_line += f" | err={err}"
        self._lab_recovery_set_prompt(result_line)
        self._lab_recovery_dump_line(result_line)
        self._lab_recovery_dump_line(summary)
        self.var_lab_cast_diff.set(summary[:220])
        self.log(f"实验 {summary}")
        if trial.get("report_path"):
            self.log(f"实验 recovery 报告: {trial['report_path']}")
            self._lab_recovery_dump_line(f"报告: {trial['report_path']}")
        if trial.get("report_json"):
            self.log(f"实验 recovery JSON: {trial['report_json']}")
        after = trial.get("after") or {}
        imm = trial.get("after_immediate") or {}
        if after or imm:
            self.var_lab_cast.set(
                f"recovery t0 id=0x{int(imm.get('+10') or 0):X} "
                f"4a0=0x{int(imm.get('+4A0') or 0):X} | "
                f"late id=0x{int(after.get('+10') or 0):X} "
                f"4a0=0x{int(after.get('+4A0') or 0):X}"
            )
        self.status.set("ATTACHED" if trial.get("ok") else "ERROR")

    def _lab_apply_recovery_matrix(self, matrix: dict) -> None:
        """UI-thread apply recovery matrix. @author by ak"""
        if not isinstance(matrix, dict):
            return
        rows = matrix.get("summary") or []
        parts = [
            f"{r.get('method')}={r.get('verdict')}"
            f"(imm={r.get('id_cleared_immediate')},"
            f"rev={r.get('id_reverted')},t={r.get('id_refill_t_s')},"
            f"perf={r.get('perform_cleared')},"
            f"pt={r.get('perform_type_before')}->{r.get('perform_type_after')})"
            for r in rows
        ]
        summary = "recovery_matrix: " + (" | ".join(parts) if parts else "empty")
        if matrix.get("error"):
            summary += f" err={matrix.get('error')!r}"
        self.var_lab_cast_diff.set(summary)
        self.log(f"实验 {summary}")
        if matrix.get("report_path"):
            self.log(f"实验 recovery 矩阵报告: {matrix['report_path']}")
        self.status.set("ATTACHED" if matrix.get("ok") else "ERROR")

    def _lab_ret_once(self) -> None:
        """Single reticle snapshot to live line + log. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            s = sample_reticle_state(sess, label="live", log=self.log)
            line = s.summary_line()
            if hasattr(self, "var_lab_ret_live"):
                self.var_lab_ret_live.set(f"retLive: {line}")
            if hasattr(self, "txt_lab_ret_dump"):
                self._lab_set_text(self.txt_lab_ret_dump, format_reticle_sample(s))
            self.log(f"准星探针 {line}")
        except Exception as e:
            self.log(f"准星探针失败: {e}")

    def _lab_ret_auto_b(self) -> None:
        """Wait until Shift held, then sample B (no workbench click). @author by ak"""
        self._lab_ret_arm_capture(mode="wait_shift", also_sample_a=False)

    def _lab_ret_delay_b(self, delay_s: float = 5.0) -> None:
        """
        Countdown then sample B. User keeps focus on game while holding Shift.
        @author by ak
        """
        self._lab_ret_arm_capture(
            mode="delay",
            delay_s=float(delay_s),
            also_sample_a=False,
        )

    def _lab_ret_ab_delayed(self, delay_s: float = 5.0) -> None:
        """
        Sample idle A now, then countdown-sample B. One primary lab path.
        @author by ak
        """
        self._lab_ret_arm_capture(
            mode="delay",
            delay_s=float(delay_s),
            also_sample_a=True,
        )

    def _lab_ret_arm_capture(
        self,
        *,
        mode: str = "delay",
        delay_s: float = 5.0,
        also_sample_a: bool = False,
    ) -> None:
        """
        Shared arm for delayed / wait-shift B capture without focus conflict.
        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        if self._lab_ret_auto_busy:
            self.log("准星采B: 已在等待中，请按住 Shift 等结束（或稍后）")
            return

        if also_sample_a:
            try:
                a = sample_reticle_state(sess, label="A", log=self.log)
                self._lab_ret_a = a
                self._lab_ret_a_regs = dump_reticle_regions(sess, a) if a.ok else {}
                if hasattr(self, "var_lab_ret_a"):
                    self.var_lab_ret_a.set(f"retA: {a.summary_line()}")
                self.log(
                    f"准星采样 A(武装时闲置): {a.summary_line()} "
                    f"regs={len(self._lab_ret_a_regs)}"
                )
            except Exception as e:
                self.log(f"准星采样 A 失败: {e}")
                return

        self._lab_ret_auto_busy = True
        stop_ev = threading.Event()
        self._lab_ret_auto_stop = stop_ev
        delay_s = max(0.5, float(delay_s))
        mode = str(mode or "delay")

        if mode == "wait_shift":
            live = "retLive: 等待手按 Shift… 切游戏按住出准星（约8s，不用回点）"
            self.log(
                "准星等Shift采B: 已武装 — 立刻切游戏按住左 Shift 出准星；"
                "检测到按住约0.45s后自动采（不用回本窗点）"
            )
        else:
            live = f"retLive: {delay_s:.0f}s 后采B… 立刻切游戏按住 Shift 出准星"
            self.log(
                f"准星延时采B: 已武装 {delay_s:.0f}s — 立刻切游戏，"
                f"按住左 Shift 让准星亮着直到采完（焦点留游戏，不要回点）"
            )
        if hasattr(self, "var_lab_ret_live"):
            self.var_lab_ret_live.set(live)

        try:
            hwnd_i = int(
                getattr(self.current_window, "hwnd", 0)
                or getattr(sess, "hwnd", 0)
                or 0
            )
        except Exception:
            hwnd_i = 0
        pid = int(sess.pid)

        def work() -> None:
            try:
                if mode == "wait_shift":
                    s = wait_reticle_on_shift(
                        sess,
                        label="B_AUTO",
                        timeout_s=8.0,
                        interval_s=0.05,
                        require_shift=True,
                        hold_s=0.45,
                        stop_event=stop_ev,
                        log=lambda m: self.msg_q.put(f"[RET] {m}"),
                    )
                else:
                    # countdown + multi-sample tail (best of many) while reticle held
                    end = time.time() + delay_s
                    while time.time() < end:
                        if stop_ev.is_set():
                            raise RuntimeError("cancelled")
                        left = max(0.0, end - time.time())
                        self.msg_q.put(
                            (
                                "__LAB_RET_COUNTDOWN__",
                                f"retLive: {left:.1f}s… 按住 Shift 保持准星(尾段连采)",
                            )
                        )
                        # leave last ~2s for multi-sample
                        if left <= 2.05:
                            break
                        time.sleep(min(0.2, max(0.05, left - 2.0)))
                    base = getattr(self, "_lab_ret_a", None)
                    s, regs, scores = capture_reticle_window(
                        sess,
                        label="B_WIN",
                        delay_s=max(0.2, end - time.time()),
                        sample_tail_s=2.0,
                        interval_s=0.12,
                        baseline=base if getattr(base, "ok", False) else None,
                        stop_event=stop_ev,
                        log=lambda m: self.msg_q.put(f"[RET] {m}"),
                    )
                    self.msg_q.put(
                        (
                            "__LAB_RET_AUTO__",
                            {
                                "sample": s,
                                "ctrl_note": "",
                                "regs": regs,
                                "scores": scores,
                            },
                        )
                    )
                    return
                ctrl_note = ""
                if s.ok:
                    _ok, _ret, ctrl_note = key_diag_run(
                        pid,
                        hwnd_i,
                        snapshot=True,
                        log=lambda m: self.msg_q.put(f"[CTRL] {m}"),
                    )
                self.msg_q.put(
                    ("__LAB_RET_AUTO__", {"sample": s, "ctrl_note": ctrl_note})
                )
            except Exception as e:
                self.msg_q.put(("__LAB_RET_AUTO__", {"error": str(e)}))

        threading.Thread(target=work, name="ret-auto-b", daemon=True).start()

    def _lab_apply_ret_auto(self, payload) -> None:
        """UI-thread handle auto Shift capture result. @author by ak"""
        self._lab_ret_auto_busy = False
        self._lab_ret_auto_stop = None
        if isinstance(payload, dict) and payload.get("error"):
            self.log(f"准星自动采B失败: {payload['error']}")
            if hasattr(self, "var_lab_ret_live"):
                self.var_lab_ret_live.set(f"retLive: auto FAIL {payload['error']}")
            return
        ctrl_note = ""
        if isinstance(payload, dict) and "sample" in payload:
            ctrl_note = str(payload.get("ctrl_note") or "")
            s = payload.get("sample")
        else:
            s = payload
        if not getattr(s, "ok", False):
            err = getattr(s, "error", None) or "no sample"
            self.log(f"准星自动采B: 未命中 — {err}")
            if hasattr(self, "var_lab_ret_live"):
                self.var_lab_ret_live.set(f"retLive: auto MISS {err}")
            if hasattr(self, "txt_lab_ret_dump") and s is not None:
                self._lab_set_text(
                    self.txt_lab_ret_dump,
                    f"=== auto B MISS ===\n{format_reticle_sample(s)}",
                )
            return
        self._lab_ret_b = s
        regs = {}
        scores = []
        if isinstance(payload, dict):
            regs = payload.get("regs") or {}
            scores = payload.get("scores") or []
        if regs:
            self._lab_ret_b_regs = regs
        elif getattr(s, "ok", False):
            # fallback region dump on UI thread if worker did not
            try:
                sess = self._lab_require_session()
                if sess is not None:
                    self._lab_ret_b_regs = dump_reticle_regions(sess, s)
            except Exception:
                pass
        if hasattr(self, "var_lab_ret_b"):
            self.var_lab_ret_b.set(f"retB: {s.summary_line()}")
        if hasattr(self, "var_lab_ret_live"):
            self.var_lab_ret_live.set(f"retLive: auto HIT {s.summary_line()}")
        if hasattr(self, "txt_lab_ret_dump"):
            body = (
                f"=== sample {getattr(s, 'label', 'B') or 'B'} (no click while holding) ===\n"
                f"{format_reticle_sample(s)}"
            )
            if scores:
                body += "=== multi-sample scores ===\n" + "\n".join(str(x) for x in scores[-12:]) + "\n"
            if ctrl_note:
                body += f"  ctrl_auto: {ctrl_note}\n"
            # auto region diff if A regs present
            a_regs = getattr(self, "_lab_ret_a_regs", None) or {}
            b_regs = getattr(self, "_lab_ret_b_regs", None) or {}
            if a_regs and b_regs:
                rd = format_region_diffs(a_regs, b_regs, max_hits=60)
                body += "=== region DIFF A->B (deep) ===\n" + "\n".join(rd) + "\n"
            self._lab_set_text(self.txt_lab_ret_dump, body)
        label = str(getattr(s, "label", "") or "")
        self.log(f"准星采B完成 [{label}]: {s.summary_line()}")
        if scores:
            self.log(f"准星连采: {len(scores)} 次，已选最高分样本")
        self.log(
            "准星: 已拍下。若屏幕当时有准星，请直接点「对比 A/B」把全文贴回"
            "（含 region DIFF）。无需再确认焦点操作。"
        )
        if ctrl_note:
            self.log(f"准星自动 CTRL 快照: {ctrl_note}")
        if getattr(self, "_lab_ret_a", None) is not None:
            self.log("准星: 已有 A+B，直接点「对比 A/B」即可")

    def _lab_ret_sample(self, which: str) -> None:
        """Store reticle sample A or B. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        tag = (which or "A").strip().upper()
        if tag not in ("A", "B"):
            tag = "A"
        try:
            s = sample_reticle_state(sess, label=tag, log=self.log)
            regs = dump_reticle_regions(sess, s) if s.ok else {}
            if tag == "A":
                self._lab_ret_a = s
                self._lab_ret_a_regs = regs
                if hasattr(self, "var_lab_ret_a"):
                    self.var_lab_ret_a.set(f"retA: {s.summary_line()}")
            else:
                self._lab_ret_b = s
                self._lab_ret_b_regs = regs
                if hasattr(self, "var_lab_ret_b"):
                    self.var_lab_ret_b.set(f"retB: {s.summary_line()}")
            if hasattr(self, "txt_lab_ret_dump"):
                self._lab_set_text(
                    self.txt_lab_ret_dump,
                    f"=== sample {tag} ===\n{format_reticle_sample(s)}",
                )
            self.log(f"准星采样 {tag}: {s.summary_line()} regs={len(regs)}")
        except Exception as e:
            self.log(f"准星采样 {tag} 失败: {e}")

    def _lab_ret_diff(self) -> None:
        """Diff reticle A vs B and show fields that changed. @author by ak"""
        a = getattr(self, "_lab_ret_a", None)
        b = getattr(self, "_lab_ret_b", None)
        if a is None or b is None:
            self.log("准星对比: 请先采 A 和 B")
            if hasattr(self, "var_lab_ret_diff"):
                self.var_lab_ret_diff.set("retDiff: need A and B")
            return
        lines = diff_reticle_samples(a, b)
        a_regs = getattr(self, "_lab_ret_a_regs", None) or {}
        b_regs = getattr(self, "_lab_ret_b_regs", None) or {}
        region_lines = []
        if a_regs and b_regs:
            region_lines = format_region_diffs(a_regs, b_regs, max_hits=80)
            lines = list(lines) + ["--- region ---"] + region_lines
        if not lines:
            text = "no field change (keys+cast+ctrl same)"
        else:
            text = "\n".join(lines)
        if hasattr(self, "var_lab_ret_diff"):
            self.var_lab_ret_diff.set(
                f"retDiff: {len(lines)} fields changed"
                if lines
                else "retDiff: no change"
            )
        if hasattr(self, "txt_lab_ret_dump"):
            body = (
                f"=== retA ===\n{format_reticle_sample(a)}"
                f"=== retB ===\n{format_reticle_sample(b)}"
                f"=== DIFF A->B ===\n{text}\n"
            )
            self._lab_set_text(self.txt_lab_ret_dump, body)
        self.log(f"准星对比: {len(lines)} 项变化")
        for ln in lines[:30]:
            self.log(f"  {ln}")


    def _lab_hold_pid_hwnd(self):
        """Resolve lab target pid/hwnd from attach session. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return 0, 0
        pid = int(getattr(sess, "pid", 0) or 0)
        hwnd = int(getattr(sess, "hwnd", 0) or 0)
        return pid, hwnd


    def _lab_hold_vk(self) -> int:
        raw = ""
        try:
            raw = self.var_lab_hold_vk.get()
        except Exception:
            raw = "Space"
        return parse_vk_label(raw, 0x20)

    def _lab_hold_soft(self) -> bool:
        try:
            return bool(self.var_lab_hold_soft.get())
        except Exception:
            return False

    def _lab_hold_set_status(self, msg: str) -> None:
        try:
            self.var_lab_hold.set(str(msg))
        except Exception:
            pass
        try:
            self.log(str(msg))
        except Exception:
            pass

    def _lab_hold_install(self) -> None:
        """Install KEY_HOLD hooks / print status. @author by ak"""
        pid, hwnd = self._lab_hold_pid_hwnd()
        if not pid:
            self._lab_hold_set_status("KEY_HOLD: 请先取句柄挂载")
            return
        try:
            from app.core.xajh_bridge import ensure_bridge
            br = ensure_bridge(pid, hwnd=hwnd or None, log=self.log, inject_if_needed=True)
            if br is None:
                self._lab_hold_set_status("KEY_HOLD: 桥接失败")
                return
            r = br.key_hold(
                self._lab_hold_vk(),
                install_only=True,
                allow_softsend=self._lab_hold_soft(),
                hwnd=hwnd or None,
                timeout_ms=3000,
            )
            note = (r.note or r.error or "").strip()
            self._lab_hold_set_status(
                f"KEY_HOLD hooks {'ok' if r.ok else 'FAIL'} ret={r.ret} | {note}"
            )
        except Exception as e:
            self._lab_hold_set_status(f"KEY_HOLD hooks 异常: {e}")

    def _lab_hold_probe(self) -> None:
        """One-shot ON/HOLD/OFF probe. @author by ak"""
        pid, hwnd = self._lab_hold_pid_hwnd()
        if not pid:
            self._lab_hold_set_status("KEY_HOLD: 请先取句柄挂载")
            return
        lines = key_hold_probe(
            pid,
            hwnd=hwnd,
            vk=self._lab_hold_vk(),
            allow_softsend=self._lab_hold_soft(),
            hold_ms=500,
            log=self.log,
        )
        if lines:
            self._lab_hold_set_status(lines[-1])

    def _lab_hold_start(self) -> None:
        """Start continuous KEY_HOLD. @author by ak"""
        pid, hwnd = self._lab_hold_pid_hwnd()
        if not pid:
            self._lab_hold_set_status("KEY_HOLD: 请先取句柄挂载")
            return
        runner = getattr(self, "_lab_hold_runner", None)
        if runner is not None and runner.is_running():
            self._lab_hold_set_status("KEY_HOLD: 已在运行")
            return
        runner = KeyHoldRunner(
            pid,
            hwnd=hwnd,
            vk=self._lab_hold_vk(),
            allow_softsend=self._lab_hold_soft(),
            refresh_ms=300,
            log=self.log,
            on_status=self._lab_hold_set_status,
        )
        self._lab_hold_runner = runner
        runner.start()

    def _lab_hold_stop(self) -> None:
        """Stop continuous KEY_HOLD. @author by ak"""
        runner = getattr(self, "_lab_hold_runner", None)
        if runner is None:
            self._lab_hold_set_status("KEY_HOLD: 未运行")
            return
        runner.stop()
        self._lab_hold_runner = None
        self._lab_hold_set_status("KEY_HOLD: 已停止")

    def _lab_hold_clear(self) -> None:
        """Clear all forced vks. @author by ak"""
        self._lab_hold_stop()
        pid, hwnd = self._lab_hold_pid_hwnd()
        if not pid:
            return
        try:
            from app.core.xajh_bridge import ensure_bridge
            br = ensure_bridge(pid, hwnd=hwnd or None, log=self.log, inject_if_needed=False)
            if br is None:
                self._lab_hold_set_status("KEY_HOLD: 桥接未就绪，无法清空")
                return
            r = br.key_hold(0x20, clear_all=True, hwnd=hwnd or None, timeout_ms=2000)
            self._lab_hold_set_status(
                f"KEY_HOLD CLEAR {'ok' if r.ok else 'FAIL'} | {(r.note or r.error or '').strip()}"
            )
        except Exception as e:
            self._lab_hold_set_status(f"KEY_HOLD CLEAR 异常: {e}")

    def _lab_ret_force(self, down: bool) -> None:
        """
        KEY_FORCE on/off then sample.

        ON: SoftSend hold + multi-sample aimBuf/24dc window (same as hand B).
        OFF: release SoftSend (must after tests; dual-client will leak otherwise).

        Success proxy (hand proven): aim_f* / aim_u28 change + ctrl_24dc=1
        — NOT cursor ch2 / skill_290.

        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        hwnd = 0
        try:
            hwnd = int(getattr(self.current_window, "hwnd", 0) or 0)
        except Exception:
            hwnd = 0
        if not hwnd:
            try:
                hwnd = int(getattr(sess, "hwnd", 0) or 0)
            except Exception:
                hwnd = 0
        pid = int(sess.pid)

        if not down:
            ok, ret, note = key_force_diag(
                pid, hwnd, down=False, level_only=False, log=self.log
            )
            self.log(f"准星 KEY_FORCE OFF: ok={ok} ret={ret} note={note}")
            try:
                s = sample_reticle_state(
                    sess,
                    label="FORCE_OFF",
                    log=self.log,
                    bridge_force_note=note,
                    bridge_force_ret=ret,
                )
                if hasattr(self, "var_lab_ret_live"):
                    self.var_lab_ret_live.set(f"retLive: {s.summary_line()}")
                if hasattr(self, "txt_lab_ret_dump"):
                    self._lab_set_text(self.txt_lab_ret_dump, format_reticle_sample(s))
            except Exception as e:
                self.log(f"准星 force OFF 采样失败: {e}")
            return

        if self._lab_ret_auto_busy:
            self.log("准星 KEY_FORCE: 已有采集在跑，先等结束")
            return

        # Ensure idle A exists for force vs idle / hand comparison.
        if getattr(self, "_lab_ret_a", None) is None:
            try:
                a = sample_reticle_state(sess, label="A", log=self.log)
                self._lab_ret_a = a
                self._lab_ret_a_regs = dump_reticle_regions(sess, a) if a.ok else {}
                if hasattr(self, "var_lab_ret_a"):
                    self.var_lab_ret_a.set(f"retA: {a.summary_line()}")
                self.log(f"准星采样 A(force前闲置): {a.summary_line()}")
            except Exception as e:
                self.log(f"准星采样 A 失败: {e}")
                return

        ok, ret, note = key_force_diag(
            pid, hwnd, down=True, level_only=False, log=self.log
        )
        self.log(f"准星 KEY_FORCE ON: ok={ok} ret={ret} note={note}")
        if not ok:
            return

        self._lab_ret_auto_busy = True
        stop_ev = threading.Event()
        self._lab_ret_auto_stop = stop_ev
        if hasattr(self, "var_lab_ret_live"):
            self.var_lab_ret_live.set(
                "retLive: KEY_FORCE 连采中… 切游戏前台(勿手按Shift) 约2s"
            )
        self.log(
            "KEY_FORCE+采B: 已 ON，约2s 连采 aimBuf/24dc（单开、勿手按、焦点可在游戏）"
        )
        base = self._lab_ret_a

        def work() -> None:
            try:
                # short settle so SoftSend/UpdateKeys land
                time.sleep(0.15)
                s, regs, scores = capture_reticle_window(
                    sess,
                    label="B_FORCE",
                    delay_s=2.0,
                    sample_tail_s=2.0,
                    interval_s=0.12,
                    baseline=base if getattr(base, "ok", False) else None,
                    stop_event=stop_ev,
                    log=lambda m: self.msg_q.put(f"[RET] {m}"),
                )
                if s.ok:
                    s.force_note = note
                    s.force_ret = ret
                self.msg_q.put(
                    (
                        "__LAB_RET_AUTO__",
                        {
                            "sample": s,
                            "ctrl_note": f"KEY_FORCE {note}",
                            "regs": regs,
                            "scores": scores,
                        },
                    )
                )
            except Exception as e:
                self.msg_q.put(("__LAB_RET_AUTO__", {"error": str(e)}))

        threading.Thread(target=work, name="ret-force-b", daemon=True).start()


    def _lab_ret_hold_shift(self, down: bool) -> None:
        """
        Process-local KEY_HOLD on VK_SHIFT (default NO SoftSend) + multi-sample.

        Cleaner than KEY_FORCE SoftSend: avoids system-wide Shift pollution and
        the suspicious ctrl_24a8 side-path. Hands must stay OFF Shift.
        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            hwnd = int(
                getattr(self.current_window, "hwnd", 0)
                or getattr(sess, "hwnd", 0)
                or 0
            )
        except Exception:
            hwnd = 0
        pid = int(sess.pid)

        from app.core.xajh_bridge import ensure_bridge

        br = ensure_bridge(
            pid,
            hwnd=hwnd or None,
            log=self.log,
            inject_if_needed=True,
        )
        if br is None:
            self.log("准星 KEY_HOLD: 桥接失败")
            return

        if not down:
            try:
                r = br.key_hold(0x10, down=False, allow_softsend=False, hwnd=hwnd or None)
                note = (r.note or r.error or "").strip()
                self.log(f"准星 KEY_HOLD OFF: ok={r.ok} ret={r.ret} note={note}")
                s = sample_reticle_state(
                    sess,
                    label="HOLD_OFF",
                    log=self.log,
                    bridge_force_note=note,
                    bridge_force_ret=int(r.ret) if r.ret is not None else None,
                )
                if hasattr(self, "var_lab_ret_live"):
                    self.var_lab_ret_live.set(f"retLive: {s.summary_line()}")
                if hasattr(self, "txt_lab_ret_dump"):
                    self._lab_set_text(self.txt_lab_ret_dump, format_reticle_sample(s))
            except Exception as e:
                self.log(f"准星 KEY_HOLD OFF 失败: {e}")
            finally:
                try:
                    br.close()
                except Exception:
                    pass
            return

        if self._lab_ret_auto_busy:
            self.log("准星 KEY_HOLD: 已有采集在跑")
            try:
                br.close()
            except Exception:
                pass
            return

        # idle A if missing
        if getattr(self, "_lab_ret_a", None) is None:
            try:
                a = sample_reticle_state(sess, label="A", log=self.log)
                self._lab_ret_a = a
                self._lab_ret_a_regs = dump_reticle_regions(sess, a) if a.ok else {}
                if hasattr(self, "var_lab_ret_a"):
                    self.var_lab_ret_a.set(f"retA: {a.summary_line()}")
                self.log(f"准星采样 A(HOLD前闲置): {a.summary_line()}")
            except Exception as e:
                self.log(f"准星采样 A 失败: {e}")
                try:
                    br.close()
                except Exception:
                    pass
                return

        try:
            # ensure hooks installed
            br.key_hold(0x10, install_only=True, allow_softsend=False, hwnd=hwnd or None)
            r = br.key_hold(0x10, down=True, allow_softsend=False, hwnd=hwnd or None)
            note = (r.note or r.error or "").strip()
            self.log(f"准星 KEY_HOLD ON Shift: ok={r.ok} ret={r.ret} note={note}")
            if not r.ok:
                try:
                    br.close()
                except Exception:
                    pass
                return
        except Exception as e:
            self.log(f"准星 KEY_HOLD ON 失败: {e}")
            try:
                br.close()
            except Exception:
                pass
            return
        try:
            br.close()
        except Exception:
            pass

        self._lab_ret_auto_busy = True
        stop_ev = threading.Event()
        self._lab_ret_auto_stop = stop_ev
        if hasattr(self, "var_lab_ret_live"):
            self.var_lab_ret_live.set(
                "retLive: KEY_HOLD 连采… 手不要按Shift，可切游戏前台 约2s"
            )
        self.log(
            "KEY_HOLD+采B: 进程内假 Shift（无 SoftSend）— "
            "双手离开键盘 Shift，只看工具是否出准星"
        )
        base = self._lab_ret_a
        ret_v = int(r.ret) if r.ret is not None else None

        def work() -> None:
            try:
                time.sleep(0.15)
                s, regs, scores = capture_reticle_window(
                    sess,
                    label="B_HOLD",
                    delay_s=2.0,
                    sample_tail_s=2.0,
                    interval_s=0.12,
                    baseline=base if getattr(base, "ok", False) else None,
                    stop_event=stop_ev,
                    log=lambda m: self.msg_q.put(f"[RET] {m}"),
                )
                if s.ok:
                    s.force_note = note
                    s.force_ret = ret_v
                self.msg_q.put(
                    (
                        "__LAB_RET_AUTO__",
                        {
                            "sample": s,
                            "ctrl_note": f"KEY_HOLD {note}",
                            "regs": regs,
                            "scores": scores,
                        },
                    )
                )
            except Exception as e:
                self.msg_q.put(("__LAB_RET_AUTO__", {"error": str(e)}))

        threading.Thread(target=work, name="ret-hold-b", daemon=True).start()

    def _lab_ret_clear(self) -> None:
        """Clear reticle A/B only. @author by ak"""
        self._lab_ret_a = None
        self._lab_ret_b = None
        self._lab_ret_a_regs = {}
        self._lab_ret_b_regs = {}
        if hasattr(self, "var_lab_ret_a"):
            self.var_lab_ret_a.set("retA: -")
        if hasattr(self, "var_lab_ret_b"):
            self.var_lab_ret_b.set("retB: -")
        if hasattr(self, "var_lab_ret_diff"):
            self.var_lab_ret_diff.set("retDiff: -")
        if hasattr(self, "txt_lab_ret_dump"):
            self._lab_set_text(self.txt_lab_ret_dump, "(cleared)")
        self.log("准星探针: 已清空 A/B")

    def _lab_ret_diag(self, start: bool) -> None:
        """
        Start/stop WH_GETMESSAGE diag hook to count WM_KEYDOWN for Shift.

        ON: install hook; start KEY_FORCE or press real Shift.
        OFF: remove hook, report msgs/shift_wm/syskey counts.
        Runs in worker thread; result via msg_q.
        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            hwnd_i = int(getattr(self.current_window, "hwnd", 0) or getattr(sess, "hwnd", 0) or 0)
        except Exception:
            hwnd_i = 0
        pid = int(sess.pid)
        if start:
            self.log("KEY_DIAG ON: 已装消息钩子 — 按真 Shift 或 KEY_FORCE ON 后点 DIAG OFF")

        def work() -> None:
            try:
                ok, ret, note = key_diag_run(
                    pid, hwnd_i, start=bool(start),
                    log=lambda m: self.msg_q.put(f"[DIAG] {m}"),
                )
                self.msg_q.put(("__LAB_RET_DIAG__", {"ok": ok, "ret": ret, "note": note, "start": start}))
            except Exception as e:
                self.msg_q.put(("__LAB_RET_DIAG__", {"error": str(e), "start": start}))

        threading.Thread(target=work, name="ret-diag", daemon=True).start()

    def _lab_ret_ctrl_snapshot(self) -> None:
        """
        Capture CheckModBind(1) and controller gates immediately.

        Use once while real Shift reticle is visible, and once during KEY_FORCE.
        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            hwnd_i = int(getattr(self.current_window, "hwnd", 0) or getattr(sess, "hwnd", 0) or 0)
        except Exception:
            hwnd_i = 0
        pid = int(sess.pid)

        def work() -> None:
            try:
                ok, ret, note = key_diag_run(
                    pid, hwnd_i, snapshot=True,
                    log=lambda m: self.msg_q.put(f"[CTRL] {m}"),
                )
                self.msg_q.put(("__LAB_RET_CTRL__", {"ok": ok, "ret": ret, "note": note}))
            except Exception as e:
                self.msg_q.put(("__LAB_RET_CTRL__", {"error": str(e)}))

        threading.Thread(target=work, name="ret-ctrl", daemon=True).start()

    def _lab_apply_ret_ctrl(self, payload: dict) -> None:
        """UI-thread handle controller diagnostic snapshot. @author by ak"""
        if payload.get("error"):
            self.log(f"CTRL 快照失败: {payload['error']}")
            return
        note = payload.get("note", "")
        self.log(f"CTRL 快照: ok={payload.get('ok')} {note}")
        if hasattr(self, "var_lab_ret_diff"):
            self.var_lab_ret_diff.set(f"retCtrl: {note}")

    def _lab_apply_ret_diag(self, payload: dict) -> None:
        """UI-thread handle KEY_DIAG result. @author by ak"""
        start = payload.get("start", False)
        if payload.get("error"):
            self.log(f"KEY_DIAG ({'ON' if start else 'OFF'}) 失败: {payload['error']}")
            return
        ok = payload.get("ok")
        ret = payload.get("ret")
        note = payload.get("note", "")
        tag = "ON" if start else "OFF"
        self.log(f"KEY_DIAG {tag}: ok={ok} shift_wm={ret} note={note}")
        if not start and hasattr(self, "var_lab_ret_diff"):
            # note is like "KEY_DIAG msgs=1708 shift_wm=0 syskey=0"
            clean = (note or "").replace("KEY_DIAG ", "").strip()
            self.var_lab_ret_diff.set(f"retDiag: {clean or note}")

    def _lab_ret_poll_capture(self) -> None:
        """
        Arm an 8s authoritative InputPoll capture via temporary CDB breakpoints.

        Flow: click once on workbench, switch to game, hold real Shift or run
        KEY_FORCE. Capture ends automatically; no click while holding Shift.
        @author by ak
        """
        sess = self._lab_require_session()
        if sess is None:
            return
        if getattr(self, "_lab_ret_poll_cap_busy", False):
            self.log("POLL 捕获: 已在进行中（约8s），请在游戏内保持测试状态")
            return
        self._lab_ret_poll_cap_busy = True
        if hasattr(self, "var_lab_ret_live"):
            self.var_lab_ret_live.set(
                "retLive: POLL 捕获中… 请切游戏按住 Shift 或 KEY_FORCE（约8s）"
            )
        self.log(
            "POLL/AIM: 已武装 — 5秒内切游戏按住Shift，或先 KEY_FORCE ON 再点本按钮；勿连点"
        )

        def work() -> None:
            try:
                ok, raw, result = capture_inputpoll_diag(
                    sess,
                    timeout_s=5.0,
                    log=lambda m: self.msg_q.put(f"[POLL] {m}"),
                )
                self.msg_q.put(
                    (
                        "__LAB_RET_POLL__",
                        {
                            "ok": ok,
                            "raw": raw,
                            "summary": (
                                result.summary_line() if result is not None else ""
                            ),
                            "classification": (
                                result.classification()
                                if result is not None
                                else "NO_RECORD"
                            ),
                            "record": result.raw if result is not None else "",
                        },
                    )
                )
            except Exception as e:
                self.msg_q.put(("__LAB_RET_POLL__", {"error": str(e)}))

        threading.Thread(target=work, name="ret-poll-cap", daemon=True).start()

    def _lab_apply_ret_poll(self, payload: dict) -> None:
        """UI-thread handle authoritative InputPoll capture. @author by ak"""
        self._lab_ret_poll_cap_busy = False
        if payload.get("error"):
            self.log(f"POLL 捕获失败: {payload['error']}")
            if hasattr(self, "var_lab_ret_live"):
                self.var_lab_ret_live.set(f"retLive: POLL FAIL {payload['error']}")
            return
        summary = str(payload.get("summary") or "")
        classification = str(payload.get("classification") or "NO_RECORD")
        record = str(payload.get("record") or "")
        raw = str(payload.get("raw") or "")
        ok = bool(payload.get("ok"))
        if summary:
            line = summary
        elif ok:
            line = "POLL NO_RECORD (no InputPoll hit in window)"
        else:
            line = f"POLL FAIL {raw[:160]}"
        if hasattr(self, "var_lab_ret_diff"):
            self.var_lab_ret_diff.set(f"retPoll: {classification} {line}")
        if hasattr(self, "var_lab_ret_live"):
            self.var_lab_ret_live.set(f"retLive: {line}")
        if hasattr(self, "txt_lab_ret_dump"):
            body = (
                "=== POLL capture (authoritative InputPoll) ===\n"
                f"class={classification}\n"
                f"summary={line}\n"
            )
            if record:
                body += f"record={record}\n"
            if raw:
                body += "--- raw ---\n"
                body += raw[-4000:]
                if not raw.endswith("\n"):
                    body += "\n"
            self._lab_set_text(self.txt_lab_ret_dump, body)
        self.log(f"POLL 捕获: ok={ok} {line}")
        aim_class = ""
        for line in str(raw or "").splitlines():
            if line.startswith("AIM_CLASS "):
                aim_class = line.split(" ", 1)[1].strip()
            if line.startswith("AIM ") and " entry=" in line:
                self.log(f"AIM 摘要: {line}")
        # KEY_FORCE+AIM payload may put AIM class directly in classification.
        if not aim_class and classification in {
            "NO_AIM_TICK",
            "AIM_EARLY_OUT",
            "TAB10_REJECTED",
            "TAB10_OK",
            "SHIFT_FLAG_SET",
            "AIM_TICK_NO_SHIFT",
        }:
            aim_class = classification
        if aim_class:
            self.log(f"AIM 解读 class={aim_class}")
            if aim_class == "NO_AIM_TICK":
                self.log("AIM 解读: 技能自由瞄准 tick(0x72D7C0) 未跑 — 场景/技能状态或最小化")
            elif aim_class == "AIM_EARLY_OUT":
                self.log("AIM 解读: 进了 0x72D7C0 但未到 IsKeyTable(0x10) — 前段 gate")
            elif aim_class == "TAB10_REJECTED":
                self.log("AIM 解读: IsKeyTable(0x10)=0 — 瞄准相位键表无 Shift（KEY_FORCE/真键未生效）")
            elif aim_class == "TAB10_OK":
                self.log("AIM 解读: IsKeyTable(0x10)=1（未见 flag 采样）— 键表层，不等于可见准星")
            elif aim_class == "SHIFT_FLAG_SET":
                self.log("AIM 解读: 已 OR 0x80000000 — 仅标志位，不等于可见准星；准星仍未通，继续查 UI/光标槽/0x290")
            elif aim_class == "AIM_TICK_NO_SHIFT":
                self.log("AIM 解读: 瞄准 tick 在跑但无 Shift 标志（常态）；出准星需要 tab10=1")
        elif classification == "NO_POLL":
            self.log("POLL 解读: 未命中瞄准 tick（旧 InputPoll 回退）")
        elif classification == "BIND_REJECTED":
            self.log("POLL 解读: 旧 bind1=0（注意 bind1 不是准星；看 AIM_CLASS）")
        elif classification == "DOWNSTREAM_REJECTED":
            self.log("POLL 解读: tab/bind 过了但未到消费者")
        elif classification == "DISPATCH_REACHED":
            self.log("POLL 解读: 已到消费者调用")

    def _lab_ret_toggle_poll(self) -> None:
        """Start/stop 400ms reticle live poll. @author by ak"""
        if getattr(self, "lab_ret_poll", None) and self.lab_ret_poll.get():
            self._lab_ret_poll_tick()
        else:
            job = getattr(self, "_lab_ret_poll_job", None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
            self._lab_ret_poll_job = None

    def _lab_ret_poll_tick(self) -> None:
        """One poll iteration. @author by ak"""
        if not (getattr(self, "lab_ret_poll", None) and self.lab_ret_poll.get()):
            return
        if getattr(self, "_lab_ret_poll_busy", False):
            self._lab_ret_poll_job = self.after(400, self._lab_ret_poll_tick)
            return
        self._lab_ret_poll_busy = True
        try:
            sess = self._lab_require_session()
            if sess is not None:
                s = sample_reticle_state(sess, label="poll", log=lambda _m: None)
                if hasattr(self, "var_lab_ret_live"):
                    self.var_lab_ret_live.set(f"retLive: {s.summary_line()}")
        except Exception:
            pass
        finally:
            self._lab_ret_poll_busy = False
            if getattr(self, "lab_ret_poll", None) and self.lab_ret_poll.get():
                self._lab_ret_poll_job = self.after(400, self._lab_ret_poll_tick)

    def _lab_clear(self) -> None:
        """Clear A/B samples. @author by ak"""
        self._lab_sample_a = None
        self._lab_sample_b = None
        self._lab_cast_a = None
        self._lab_cast_b = None
        self._lab_ret_a = None
        self._lab_ret_b = None
        self.var_lab_a.set("A: -")
        self.var_lab_b.set("B: -")
        self.var_lab_diff.set("diff: -")
        if hasattr(self, "var_lab_dump_diff"):
            self.var_lab_dump_diff.set("内存对比: -")
        if hasattr(self, "var_lab_cast"):
            self.var_lab_cast.set("cast-this: -")
        if hasattr(self, "var_lab_cast_diff"):
            self.var_lab_cast_diff.set("cast diff: -")
        if hasattr(self, "var_lab_ret_a"):
            self.var_lab_ret_a.set("retA: -")
            self.var_lab_ret_b.set("retB: -")
            self.var_lab_ret_diff.set("retDiff: -")
        if hasattr(self, "txt_lab_skill_dump"):
            self._lab_set_text(self.txt_lab_skill_dump, "(cleared)")
            self._lab_set_text(self.txt_lab_seq_dump, "(cleared)")
        if hasattr(self, "txt_lab_cast_dump"):
            self._lab_set_text(self.txt_lab_cast_dump, "(cleared)")
        if hasattr(self, "txt_lab_ret_dump"):
            self._lab_set_text(self.txt_lab_ret_dump, "(cleared)")
        self.log("实验: 已清空 A/B")

    def _lab_skill_ptrs(self) -> None:
        """Probe skill / sequence pointers only. @author by ak"""
        sess = self._lab_require_session()
        if sess is None:
            return
        try:
            s = sample_combat_probe(sess, log=self.log, with_pos=False, with_dumps=False)
            self.var_lab_skill.set(
                f"skill=0x{(s.skill_ptr or 0):X} seq=0x{(s.skill_seq_ptr or 0):X} "
                f"gs={s.game_state} states={s.host_states_hex or '-'}"
            )
            self.log(f"实验技能指针 {self.var_lab_skill.get()}")
            self.status.set("ATTACHED")
        except Exception as e:
            self.log(f"实验技能指针失败: {e}")
            self.status.set("ERROR")

    def _lab_toggle_poll(self) -> None:
        """Start/stop 500ms poll. @author by ak"""
        if self.lab_poll.get():
            self.log("实验: 开始轮询 500ms")
            self._lab_poll_tick()
        else:
            self._lab_stop_poll()
            self.log("实验: 停止轮询")

    def _lab_stop_poll(self) -> None:
        """Cancel lab poll job. @author by ak"""
        if self._lab_poll_job is not None:
            try:
                self.after_cancel(self._lab_poll_job)
            except Exception:
                pass
            self._lab_poll_job = None
        self._lab_poll_busy = False
        try:
            if hasattr(self, "lab_poll"):
                self.lab_poll.set(False)
        except Exception:
            pass

    def _lab_poll_tick(self) -> None:
        """Periodic probe while checkbox on. @author by ak"""
        if not getattr(self, "lab_poll", None) or not self.lab_poll.get():
            self._lab_poll_job = None
            return
        if self._lab_poll_busy:
            self._lab_poll_job = self.after(500, self._lab_poll_tick)
            return
        sess = self.session
        if sess is None or not getattr(sess, "pid", None):
            self.var_lab_live.set("live: 未挂载")
            self._lab_poll_job = self.after(500, self._lab_poll_tick)
            return

        self._lab_poll_busy = True

        def work() -> None:
            try:
                s = sample_combat_probe(sess, log=lambda _m: None)
                self.msg_q.put(("__LAB_LIVE__", format_sample_line(s)))
            except Exception as e:
                self.msg_q.put(("__LAB_LIVE__", f"err={e}"))
            finally:
                self.msg_q.put(("__LAB_POLL_DONE__", None))

        threading.Thread(target=work, daemon=True).start()
        self._lab_poll_job = self.after(500, self._lab_poll_tick)

    # ---------- logging ----------
    def log(self, msg: str) -> None:
        """Workbench UI log queue only (unchanged). @author by ak"""
        ts = time.strftime("%H:%M:%S")
        self.msg_q.put(f"[{ts}] {msg}")

    def _console_attach_log(self, msg: str, *, tag: str = "ATTACH") -> None:
        """
        Always echo inject / 取句柄 / 破0 traces to the process console.

        Workbench feature logs keep using self.log() only; this path is for
        diagnosing attach hangs when the 日志 tab is empty or hard to reach.
        @author by ak
        """
        try:
            ts = time.strftime("%H:%M:%S")
            line = f"[{ts}] [{tag}] {msg}"
            print(line, flush=True)
        except Exception:
            try:
                sys.stderr.write(f"[{tag}] {msg}\n")
                sys.stderr.flush()
            except Exception:
                pass

    def _attach_log(self, msg: str, *, tag: str = "ATTACH") -> None:
        """Console + workbench log for inject/handle only. @author by ak"""
        self._console_attach_log(msg, tag=tag)
        try:
            self.log(msg)
        except Exception:
            pass

    def _clear_log(self):
        if hasattr(self, "log_text"):
            self.log_text.delete("1.0", tk.END)

    # ---------- drag pick ----------
    def _start_drag_pick(self, _evt=None):
        """Start crosshair drag-pick (hold LMB, release on game). @author by ak"""
        try:
            self._attach_log("取句柄: 按住左键拖到游戏窗口后松开（ESC 取消）", tag="HANDLE")
        except Exception:
            pass
        try:
            self.status.set("PICK")
        except Exception:
            pass
        self.after(10, lambda: DragPicker(self, self._on_window_picked))

    def _on_window_picked(self, info: WindowInfo):
        # hard reject self even if picker missed a case
        if is_self_window_info(info):
            self.log("忽略：不能选择本软件自己的窗口，请拖到游戏客户端")
            self.status.set("IDLE")
            messagebox.showinfo("提示", "不能选择本软件窗口，请拖到游戏（xajh）窗口")
            return
        self.current_window = info
        self.var_hwnd.set(f"0x{info.hwnd:08X}")
        self.var_pid.set(str(info.pid))
        title_full = (info.title or "(empty)").strip() or "(empty)"
        exe_full = (info.exe_path or "").strip()
        class_full = (info.class_name or "").strip()
        self._info_full["title"] = title_full
        self._info_full["exe"] = exe_full or "-"
        self._info_full["class"] = class_full or "-"
        self.var_title.set(self._short_text(title_full, 48))
        self.var_exe.set(self._short_path(exe_full or "-", 40))
        self.var_class.set(self._short_text(class_full or "-", 36))
        # Clear stale recon so user sees progress instead of old "-" forever.
        try:
            self.var_scene.set("…")
            self.var_map.set("附加中…")
            self.var_pos.set("附加中…")
            self.var_role.set("…")
        except Exception:
            pass
        self._attach_log(
            f"选中窗口 HWND=0x{info.hwnd:X} PID={info.pid} title={info.title!r} class={info.class_name!r}",
            tag="HANDLE",
        )
        if info.exe_path:
            self._attach_log(f"exe={info.exe_path}", tag="HANDLE")
        # Pick handle: reuse healthy bridge if present, else inject, then 破0.
        self._run_break0(ensure_bridge=True, source="取句柄")

    # ---------- break0 ----------
    def _worker_log(self, msg: str) -> None:
        """Queue log lines from background workers (never touch Tk). @author by ak"""
        text = str(msg)
        # Worker threads of 取句柄/破0/ensure_bridge all go through here.
        self._console_attach_log(text, tag="ATTACH")
        try:
            self.msg_q.put(text)
        except Exception:
            pass

    def _run_break0(
        self,
        *,
        ensure_bridge: bool = True,
        source: str = "破0",
    ) -> None:
        """
        Attach + recon for current_window.

        Order (important — avoids 附加中 forever):
          1) OpenProcess attach (hard timeout)
          2) Fast recon only (plg scene / role hint; NO full map string scan)
          3) Push __RESULT__ so UI leaves 附加中 immediately
          4) Optional ensure_bridge in background (reuse healthy / else inject)
        Bridge failure must NOT block break0 UI update.

        @author by ak
        """
        if not self.current_window:
            messagebox.showinfo("提示", "请先拖动十字准星选取游戏窗口")
            return
        info = self.current_window
        pid = int(getattr(info, "pid", 0) or 0)
        hwnd = int(getattr(info, "hwnd", 0) or 0)
        if pid <= 0:
            self._attach_log(f"{source}: 无效 pid，无法附加", tag="ATTACH")
            self.status.set("ERROR")
            return
        if bool(getattr(self, "_break0_busy", False)):
            started = float(getattr(self, "_break0_started_at", 0) or 0)
            age = (time.monotonic() - started) if started > 0 else 9999.0
            if age < 20.0:
                self._attach_log(f"{source}: 附加仍在进行（{age:.0f}s），请稍候", tag="ATTACH")
                return
            self._attach_log(f"{source}: 上次附加超时，强制重试", tag="ATTACH")
            self._break0_busy = False
        self._break0_busy = True
        self._break0_started_at = time.monotonic()
        # Log BEFORE any UI attr access so console always shows entry.
        self._attach_log(
            f"{source}: 开始准备目标 pid={pid} hwnd=0x{hwnd:X} "
            f"ensure_bridge={bool(ensure_bridge)}",
            tag="ATTACH",
        )
        try:
            if hasattr(self, "status"):
                self.status.set("RECON")
        except Exception:
            pass
        try:
            if hasattr(self, "var_map"):
                self.var_map.set("附加中…")
            if hasattr(self, "var_pos"):
                self.var_pos.set("附加中…")
            if hasattr(self, "var_scene"):
                self.var_scene.set("…")
            if hasattr(self, "var_role"):
                self.var_role.set("…")
        except Exception:
            pass

        def worker():
            session = None
            bridge_meta = {
                "reused": False,
                "did_inject": False,
                "bridge_ok": False,
                "note": "",
            }
            try:
                log = self._worker_log

                # --- 1) Attach with hard timeout (pymem can stall on list_modules) ---
                log(f"{source}: attach begin pid={pid}")
                attach_box: dict = {}

                def _attach_job():
                    try:
                        sess = GameAttachSession(log=log)
                        sess.attach(pid)
                        attach_box["session"] = sess
                    except Exception as e:
                        attach_box["error"] = e

                at = threading.Thread(
                    target=_attach_job, daemon=True, name=f"xajh-attach-{pid}"
                )
                at.start()
                at.join(timeout=8.0)
                if at.is_alive():
                    self.msg_q.put(
                        (
                            "__ERROR__",
                            f"{source}: OpenProcess/模块枚举超时 8s "
                            f"(pid={pid}) — 进程可能受保护或未就绪",
                        )
                    )
                    return
                if "error" in attach_box:
                    raise attach_box["error"]
                session = attach_box.get("session")
                if session is None:
                    raise RuntimeError("attach returned no session")

                # Leave 附加中 ASAP even before recon finishes.
                self.msg_q.put(
                    (
                        "__BREAK0_PROGRESS__",
                        {
                            "phase": "attached",
                            "source": source,
                            "pid": pid,
                            "hwnd": hwnd,
                            "exe_path": getattr(session, "exe_path", "") or "",
                            "map": "侦察中…",
                            "pos": "侦察中…",
                        },
                    )
                )

                # --- 2) Fast recon only (no full heap string scan) ---
                log(f"{source}: recon_fast begin pid={pid}")
                recon_box: dict = {}

                def _recon_job():
                    try:
                        recon_box["result"] = session.recon_fast()
                    except Exception as e:
                        recon_box["error"] = e

                rt = threading.Thread(
                    target=_recon_job, daemon=True, name=f"xajh-recon-{pid}"
                )
                rt.start()
                # plg path: 5s wait + 2s grace + mutex slack ≈ keep under 12s
                rt.join(timeout=12.0)
                if rt.is_alive():
                    log(f"{source}: recon_fast 超时 12s，先完成附加界面")
                    result = AttachResult(
                        ok=True,
                        pid=pid,
                        exe_path=getattr(session, "exe_path", None)
                        or getattr(info, "exe_path", "")
                        or "",
                        module_base=getattr(session, "module_base", None),
                        module_size=getattr(session, "module_size", None),
                        error="recon_fast timeout",
                    )
                elif "error" in recon_box:
                    log(f"{source}: recon_fast warn: {recon_box['error']}")
                    result = AttachResult(
                        ok=True,
                        pid=pid,
                        exe_path=getattr(session, "exe_path", None)
                        or getattr(info, "exe_path", "")
                        or "",
                        module_base=getattr(session, "module_base", None),
                        module_size=getattr(session, "module_size", None),
                        error=str(recon_box["error"]),
                    )
                else:
                    result = recon_box.get("result") or AttachResult(
                        ok=True,
                        pid=pid,
                        exe_path=getattr(session, "exe_path", None) or "",
                        module_base=getattr(session, "module_base", None),
                        module_size=getattr(session, "module_size", None),
                    )

                result.hwnd = hwnd
                result.title = getattr(info, "title", "") or ""
                result.exe_path = (
                    result.exe_path
                    or getattr(session, "exe_path", None)
                    or getattr(info, "exe_path", "")
                    or ""
                )

                # --- 3) UI result FIRST (must leave 附加中 before bridge) ---
                self.msg_q.put(
                    (
                        "__RESULT__",
                        session,
                        result,
                        {
                            "source": source,
                            "bridge": dict(bridge_meta),
                            "bridge_pending": bool(ensure_bridge),
                        },
                    )
                )

                # --- 4) Bridge reuse/inject in background (does not block UI) ---
                if ensure_bridge:
                    log(
                        f"{source}: ensure_bridge begin pid={pid} "
                        f"hwnd=0x{hwnd:X} (已注入则复用，否则新注入；限时20s)"
                    )
                    box: dict = {}

                    def _bridge_job():
                        try:
                            from app.core.xajh_bridge import (
                                ensure_bridge as _ensure_bridge,
                                last_ensure_bridge_failure,
                                last_ensure_meta,
                            )

                            br = _ensure_bridge(
                                pid,
                                log=log,
                                inject_if_needed=True,
                                hwnd=hwnd or None,
                                force_reinject=False,
                            )
                            meta = last_ensure_meta(pid, clear=True) or {}
                            box["br"] = br
                            box["meta"] = meta
                            if br is None:
                                box["fail"] = (
                                    last_ensure_bridge_failure(pid, clear=True)
                                    or "BRIDGE_FAIL"
                                )
                        except Exception as e:
                            box["fail"] = str(e)

                    bt = threading.Thread(
                        target=_bridge_job, daemon=True, name=f"xajh-br-{pid}"
                    )
                    bt.start()
                    bt.join(timeout=20.0)
                    if bt.is_alive():
                        bridge_meta["note"] = "BRIDGE_TIMEOUT"
                        log(
                            f"{source}: ensure_bridge 超时 20s "
                            f"（桥接线程后台继续；破0已完成）"
                        )
                    else:
                        meta = box.get("meta") or {}
                        bridge_meta["reused"] = bool(meta.get("reused"))
                        bridge_meta["did_inject"] = bool(meta.get("did_inject"))
                        br = box.get("br")
                        if br is None:
                            code = str(box.get("fail") or "BRIDGE_FAIL")
                            bridge_meta["note"] = code
                            log(
                                f"{source}: 桥接未就绪 [{code}] "
                                f"— 破0内存附加仍可用"
                            )
                        else:
                            try:
                                br.close()
                            except Exception:
                                pass
                            bridge_meta["bridge_ok"] = True
                            mode = (
                                "复用已有桥接"
                                if bridge_meta["reused"]
                                and not bridge_meta["did_inject"]
                                else "新注入桥接"
                            )
                            log(f"{source}: 桥接就绪 · {mode} meta={meta!r}")
                    self.msg_q.put(
                        (
                            "__BRIDGE_STATUS__",
                            {
                                "source": source,
                                "bridge": dict(bridge_meta),
                            },
                        )
                    )
                else:
                    bridge_meta["bridge_ok"] = True
                    bridge_meta["reused"] = True
                    log(f"{source}: 跳过 ensure_bridge（上游已验证）")
                    self.msg_q.put(
                        (
                            "__BRIDGE_STATUS__",
                            {
                                "source": source,
                                "bridge": dict(bridge_meta),
                            },
                        )
                    )
            except Exception as e:
                try:
                    if session is not None:
                        session.close()
                except Exception:
                    pass
                self.msg_q.put(("__ERROR__", f"{source}: 附加异常 {e}"))

        threading.Thread(
            target=worker, daemon=True, name=f"xajh-break0-{pid}"
        ).start()

    def _apply_result(
        self,
        session: GameAttachSession,
        result: AttachResult,
        meta: dict | None = None,
    ):
        self._break0_busy = False
        meta = meta if isinstance(meta, dict) else {}
        bridge = meta.get("bridge") if isinstance(meta.get("bridge"), dict) else {}
        source = str(meta.get("source") or "破0")
        # replace old session
        if self.session is not None and self.session is not session:
            try:
                self.session.close()
            except Exception:
                pass
        self.session = session
        self.current_result = result
        self.status.set("ATTACHED" if result.ok else "ERROR")

        if result.exe_path:
            try:
                self._info_full["exe"] = str(result.exe_path)
                self.var_exe.set(self._short_path(str(result.exe_path), 40))
            except Exception:
                pass
        def _rlog(m: str) -> None:
            # 取句柄 / 解锁 / 破0 结果同时打控制台，其它来源保持原 log
            if any(k in source for k in ("取句柄", "解锁", "破0")):
                self._attach_log(m, tag="ATTACH")
            else:
                self.log(m)

        if meta.get("bridge_pending"):
            _rlog(f"{source}: 破0界面已就绪，桥接后台进行中…")
        elif bridge:
            mode = (
                "复用已有桥接"
                if bridge.get("reused") and not bridge.get("did_inject")
                else ("新注入桥接" if bridge.get("did_inject") else "桥接")
            )
            _rlog(f"{source}: {mode} ok={bool(bridge.get('bridge_ok'))}")

        if not result.ok:
            _rlog(f"破0 失败: {result.error}")
            try:
                self.var_pos.set("附加失败")
                self.var_map.set("-")
                self.var_scene.set("-")
            except Exception:
                pass
            return

        if result.map_display:
            self.var_map.set(self._short_text(result.map_display, 48))
        elif result.map_name:
            self.var_map.set(self._short_text(format_map_display(result.map_name), 48))
        else:
            self.var_map.set("(等待 scene)")
        if result.pos:
            self.var_pos.set(
                f"{result.pos.x:.3f}, {result.pos.y:.3f}, {result.pos.z:.3f}"
            )
            # if pos note carries scene id, show it
            note = result.pos.note or ""
            if "scene=" in note:
                try:
                    sid = int(note.split("scene=", 1)[1].split()[0].strip(",;"))
                    self.var_scene.set(str(sid))
                    disp = format_scene_display(sid)
                    if disp and disp != "-":
                        self.var_map.set(self._short_text(disp, 48))
                        result.map_display = disp
                except Exception:
                    pass
        else:
            self.var_pos.set("(等待 plg 实时)")
            self.var_scene.set("-")

        self.var_role.set(self._format_role_hint(result.role_hint or {}))
        if result.module_base is not None:
            self.log(f"module base={hex(result.module_base)} size={result.module_size}")

        # fill tree
        for i in self.tree.get_children():
            self.tree.delete(i)
        for m in result.map_candidates:
            disp = format_map_display(m.text)
            self.tree.insert("", tk.END, values=("MAP", disp[:80], hex(m.address), m.score))
        for p in result.pos_candidates:
            self.tree.insert(
                "",
                tk.END,
                values=(
                    "POS",
                    f"{p.x:.3f},{p.y:.3f},{p.z:.3f} ({p.note})",
                    hex(p.address),
                    p.score,
                ),
            )
        if any(k in source for k in ("取句柄", "解锁", "破0")):
            self._attach_log(
                "破0 完成。坐标/scene 由 plg 实时刷新；"
                "地图优先 scene_id 表（字符串扫描仅候选）。",
                tag="ATTACH",
            )
        else:
            self.log(
                "破0 完成。坐标/scene 由 plg 实时刷新；"
                "地图优先 scene_id 表（字符串扫描仅候选）。"
            )
        # always start live pos after attach
        self.pos_live.set(True)
        self._start_pos_live()

    def _format_role_hint(self, role: dict) -> str:
        """Compact role line: roleid + server only. @author by ak"""
        if not role:
            return "-"
        rid = role.get("roleid") or role.get("userid") or ""
        srv = (
            role.get("server_CurrentServer")
            or role.get("server")
            or role.get("servergroup")
            or ""
        )
        parts = []
        if rid:
            parts.append(str(rid))
        if srv:
            parts.append(self._short_text(str(srv), 28))
        return " / ".join(parts) if parts else "-"

    @staticmethod
    def _short_text(s: str, max_len: int = 40) -> str:
        s = (s or "").replace("\r", " ").replace("\n", " ").strip()
        if len(s) <= max_len:
            return s
        return s[: max_len - 1] + "…"

    @staticmethod
    def _short_path(path: str, max_len: int = 56) -> str:
        """
        Compact absolute paths for the process strip.
        Prefer drive + … + parent/name so deep WeGame paths stay one line.
        Full path is kept in _info_full for hover tooltip.
        @author by ak
        """
        path = (path or "").strip()
        if not path or path == "-":
            return "-"
        p = Path(path)
        parts = list(p.parts)
        if len(parts) >= 3:
            drive = parts[0].rstrip("\\/")
            tail = f"{parts[-2]}/{parts[-1]}"
            out = f"{drive}/…/{tail}"
        elif p.parent.name:
            out = f"{p.parent.name}/{p.name}"
        else:
            out = p.name or path
        if len(out) <= max_len:
            return out
        if len(path) <= max_len:
            return path
        return "…" + path[-(max_len - 1) :]

    def _bind_info_tooltip(self, widget: tk.Widget, key: str) -> None:
        """Hover shows full title/path stored in _info_full. @author by ak"""
        tip: dict[str, tk.Toplevel | None] = {"win": None}

        def show(_evt=None):
            text = (self._info_full.get(key) or "").strip()
            if not text or text == "-":
                return
            hide()
            win = tk.Toplevel(self)
            win.wm_overrideredirect(True)
            try:
                win.attributes("-topmost", True)
            except tk.TclError:
                pass
            lbl = tk.Label(
                win,
                text=text,
                justify=tk.LEFT,
                background="#161b22",
                foreground="#c9d1d9",
                relief=tk.SOLID,
                borderwidth=1,
                font=("Consolas", 9),
                padx=8,
                pady=4,
                wraplength=720,
            )
            lbl.pack()
            x = widget.winfo_rootx() + 8
            y = widget.winfo_rooty() + widget.winfo_height() + 2
            win.geometry(f"+{x}+{y}")
            tip["win"] = win

        def hide(_evt=None):
            w = tip.get("win")
            if w is not None:
                try:
                    w.destroy()
                except Exception:
                    pass
                tip["win"] = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)
        widget.bind("<ButtonPress>", hide)

    def _start_pos_live(self) -> None:
        """Begin periodic plg scene position poll (always-on while attached). @author by ak"""
        self._stop_pos_live()
        if not self.session or not getattr(self.session, "module_base", None):
            return
        self.pos_live.set(True)
        self.log(
            f"实时坐标 interval={self._pos_live_interval_ms}ms "
            f"(plg GetCurrentScenePosition)"
        )
        self._pos_live_tick()

    def _stop_pos_live(self) -> None:
        """Cancel live pos timer. @author by ak"""
        if self._pos_live_job is not None:
            try:
                self.after_cancel(self._pos_live_job)
            except Exception:
                pass
            self._pos_live_job = None
        self._pos_live_busy = False

    def _pos_live_tick(self) -> None:
        """Schedule one live read; re-arm on completion. @author by ak"""
        self._pos_live_job = None
        if not self.pos_live.get():
            return
        if not self.session or not getattr(self.session, "module_base", None):
            return
        if self._pos_live_busy:
            self._pos_live_job = self.after(
                self._pos_live_interval_ms, self._pos_live_tick
            )
            return
        self._pos_live_busy = True
        session = self.session

        def worker():
            try:
                r = read_scene_position(session, log=lambda _m: None)
                self.msg_q.put(("__POS_LIVE__", r.to_dict() if hasattr(r, "to_dict") else {}))
            except Exception as e:
                self.msg_q.put(("__POS_LIVE_ERR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_pos_live(self, d: dict) -> None:
        """Apply plg live position + scene-based map to UI. @author by ak"""
        self._pos_live_busy = False
        ok = bool(d.get("ok"))
        scene_pos = d.get("scene_pos")
        scene_id = d.get("scene_id")
        if ok and scene_pos and len(scene_pos) >= 3:
            x, y, z = float(scene_pos[0]), float(scene_pos[1]), float(scene_pos[2])
            self.var_pos.set(f"{x:.3f}, {y:.3f}, {z:.3f}")
            if scene_id is not None:
                sid = int(scene_id)
                self.var_scene.set(str(sid))
                disp = format_scene_display(sid)
                if disp and disp != "-":
                    self.var_map.set(self._short_text(disp, 48))
                    if self.current_result is not None:
                        self.current_result.map_display = disp
                        mid, cn = None, None
                        try:
                            from app.core.map_names import resolve_scene_id

                            mid, cn = resolve_scene_id(sid)
                        except Exception:
                            pass
                        if mid:
                            self.current_result.map_name = mid
                        if cn:
                            self.current_result.map_name_cn = cn
            # keep current_result.pos in sync for pathfind / entity scan
            if self.current_result is not None:
                prev = self.current_result.pos
                addr = int(prev.address) if prev else 0
                self.current_result.pos = PosCandidate(
                    address=addr,
                    x=x,
                    y=y,
                    z=z,
                    score=100,
                    note=f"plg_live scene={scene_id}",
                )
        elif d.get("error"):
            # only surface occasional errors via status, avoid log spam
            self.var_scene.set("err")
        # re-arm
        if self.pos_live.get() and self.session:
            self._pos_live_job = self.after(
                self._pos_live_interval_ms, self._pos_live_tick
            )

    def _refresh_pos(self):
        """One-shot plg scene position refresh (also used by pathfind). @author by ak"""
        if not self.session or not self.session.pm:
            messagebox.showinfo("提示", "尚未附加进程，请先取句柄破0")
            return
        self.status.set("REFRESH")

        def worker():
            try:
                r = read_scene_position(self.session, log=self.log)
                self.msg_q.put(
                    ("__POS_LIVE__", r.to_dict() if hasattr(r, "to_dict") else {})
                )
                self.msg_q.put(("__STATUS__", "ATTACHED"))
            except Exception as e:
                self.msg_q.put(("__ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _detach(self):
        """
        Drop attach session. Safe on welcome page (before _build_ui).

        @author by ak
        """
        try:
            self._lab_stop_poll()
        except Exception:
            pass
        self._stop_pos_live()
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass
        self.session = None
        self.current_result = None
        if hasattr(self, "status"):
            try:
                self.status.set("IDLE")
            except Exception:
                pass
        if hasattr(self, "var_pos"):
            try:
                self.var_pos.set("-")
            except Exception:
                pass
        if hasattr(self, "var_scene"):
            try:
                self.var_scene.set("-")
            except Exception:
                pass
        try:
            self.log("已断开附加")
        except Exception:
            self._wlog("已断开附加")

    # ---------- helpers ----------
    def _list_xajh(self):
        try:
            import psutil
        except ImportError:
            self.log("需要 psutil")
            return
        found = []
        for p in psutil.process_iter(["pid", "name", "exe"]):
            try:
                if (p.info.get("name") or "").lower() == "xajh.exe":
                    found.append(p)
            except Exception:
                continue
        self.log(f"xajh.exe count={len(found)}")
        for p in found:
            try:
                conns = []
                for c in p.net_connections(kind="inet"):
                    if c.raddr:
                        conns.append(f"{c.raddr.ip}:{c.raddr.port}")
                self.log(f"  pid={p.pid} exe={p.info.get('exe')} remote={conns[:3]}")
            except Exception as e:
                self.log(f"  pid={p.pid} err={e}")

    def _read_server_cfg(self):
        try:
            from common.paths import read_current_server

            cfg = read_current_server()
            self.log("currentserver.ini => " + json.dumps(cfg, ensure_ascii=False))
        except Exception as e:
            self.log(f"read server cfg failed: {e}")

    def _show_module(self):
        if not self.session:
            self.log("未附加")
            return
        self.log(
            f"base={hex(self.session.module_base) if self.session.module_base else None} "
            f"size={self.session.module_size} pid={self.session.pid}"
        )

    def _copy_pos(self):
        txt = self.var_pos.get()
        self.clipboard_clear()
        self.clipboard_append(txt)
        self.log(f"已复制坐标: {txt}")

    def _diff_lock_pos(self):
        """Optional walk check: two plg samples over ~4s (host pos already live). @author by ak"""
        if not self.session or not self.session.pm:
            messagebox.showinfo("提示", "尚未附加进程，请先取句柄破0")
            return
        self.status.set("DIFF1")
        self.log(
            "验证位移：4 秒内走动，对比两次 plg 坐标距离 "
            "（日常坐标已实时刷新，无需本按钮）"
        )

        def worker():
            try:
                from app.core.game_attach import AttachResult, diff_lock_positions

                moved = diff_lock_positions(
                    self.session.pm,
                    log=self.log,
                    wait_sec=4.0,
                    pool_limit=500,
                    session=self.session,
                )
                self.log(f"差分命中 {len(moved)} 个（plg 真坐标或启发式地址）")
                prev = self.current_result
                result = AttachResult(
                    ok=True,
                    pid=self.session.pid or 0,
                    hwnd=self.current_window.hwnd if self.current_window else 0,
                    title=self.current_window.title if self.current_window else "",
                    exe_path=self.session.exe_path,
                    module_base=self.session.module_base,
                    module_size=self.session.module_size,
                    map_name=(prev.map_name if prev else None),
                    map_name_cn=(prev.map_name_cn if prev else None),
                    map_display=(prev.map_display if prev else None),
                    map_candidates=(prev.map_candidates if prev else []),
                    pos=moved[0] if moved else None,
                    pos_candidates=moved[:12],
                    role_hint=(prev.role_hint if prev else {}),
                )
                self.msg_q.put(("__RESULT__", self.session, result))
            except Exception as e:
                self.msg_q.put(("__ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _entity_mode_to_kind(self) -> str:
        """Map dropdown label to scan kind. @author by ak"""
        mode = (self.entity_mode.get() or "").strip()
        if "怪物" in mode:
            return "monster"
        if "物品" in mode or "道具" in mode:
            return "matter"
        if "npc" in mode.lower() or "npm" in mode.lower():
            return "npc"
        return "monster"

    def _scan_nearby(self):
        """Scan nearby entities for the selected dropdown kind only (plg AOI)."""
        if not self.session or not self.session.pm or not getattr(self.session, "module_base", None):
            messagebox.showinfo("提示", "尚未附加进程，请先取句柄破0")
            return
        kind = self._entity_mode_to_kind()
        mode_label = self.entity_mode.get()
        host = None
        if self.current_result and self.current_result.pos:
            p = self.current_result.pos
            host = (p.x, p.y, p.z)

        self.status.set("ENTITY")
        if host:
            host_s = f"host=({host[0]:.2f},{host[1]:.2f},{host[2]:.2f})"
        else:
            host_s = "host=none(use plg dist)"
        self.log(
            f"{mode_label} 开始 kind={kind} {host_s} r=80 "
            f"（plg GetObjects 真 AOI，非字符串扫名表）"
        )

        def worker():
            try:
                ents = scan_nearby_entities(
                    self.session,
                    host_pos=host,
                    radius=80.0,
                    limit=60,
                    kinds=[kind],
                    require_pos=False,
                    log=self.log,
                )
                self.msg_q.put(("__ENTITIES__", ents, kind, mode_label))
            except Exception as e:
                self.msg_q.put(("__ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_entities(self, entities, kind: str | None = None, mode_label: str | None = None):
        # clear previous entity rows only
        for i in self.tree.get_children():
            vals = self.tree.item(i, "values")
            if vals and str(vals[0]).startswith(("MON", "NPC", "ITEM", "UNK", "PLY", "PKT", "IX")):
                self.tree.delete(i)

        kind_map = {
            "monster": "MON",
            "npc": "NPC",
            "matter": "ITEM",
            "player": "PLY",
            "unknown": "UNK",
        }
        counts = {"monster": 0, "npc": 0, "matter": 0, "unknown": 0, "player": 0}
        for e in entities:
            counts[e.kind] = counts.get(e.kind, 0) + 1
            k = kind_map.get(e.kind, "UNK")
            if e.x is not None:
                val = f"{e.name} @ ({e.x:.1f},{e.y:.1f},{e.z:.1f})"
                if e.dist is not None:
                    val += f" d={e.dist:.1f}"
            else:
                val = f"{e.name} (nopos)"
                if e.dist is not None:
                    val += f" d={e.dist:.1f}"
            if getattr(e, "tid", None) is not None:
                val += f" tid={e.tid}"
            self.tree.insert("", tk.END, values=(k, val[:90], hex(e.address), e.score))

        label = mode_label or "周围扫描"
        self.log(
            f"{label} 完成: "
            f"怪物={counts.get('monster',0)} NPC={counts.get('npc',0)} "
            f"物品={counts.get('matter',0)} 未知={counts.get('unknown',0)} "
            f"总计={len(entities)}"
        )
        if not entities:
            self.log(
                "无命中：半径内可能无该类型，或 plg 远程调用失败（看日志）。"
                "NPC/怪物同属 class=2；城镇里怪物常为空。"
            )
        else:
            self.log("说明: plg GetObjects 真周围列表；坐标 object+0x158。")

        try:
            out_dir = PROJECT_ROOT / ".issues" / "break0"
            out_dir.mkdir(parents=True, exist_ok=True)
            tag = kind or "all"
            path = out_dir / f"entities_{tag}_{time.strftime('%Y%m%d_%H%M%S')}.json"
            payload = {
                "mode": mode_label,
                "kind": kind,
                "count": len(entities),
                "items": [e.to_dict() for e in entities],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self.log(f"实体结果已导出: {path}")
        except Exception as e:
            self.log(f"实体导出失败: {e}")
        self.status.set("ATTACHED")

    # ---------- interact (PickItem / matter) ----------
    def _host_pos_tuple(self) -> tuple[float, float, float] | None:
        """Current host pos from live result if any. @author by ak"""
        if self.current_result and self.current_result.pos:
            p = self.current_result.pos
            return (float(p.x), float(p.y), float(p.z))
        return None

    def _ix_params(self) -> tuple[float, float, bool]:
        """scan_r, max_dist, prefer_actionable. @author by ak"""
        try:
            scan_r = float(self.ix_scan_r.get() or "40")
        except ValueError:
            scan_r = 40.0
        try:
            max_d = float(self.ix_max_dist.get() or "12")
        except ValueError:
            max_d = 12.0
        prefer = bool(self.ix_prefer_actionable.get())
        return scan_r, max_d, prefer

    def _ix_require_session(self) -> bool:
        if not self.session or not getattr(self.session, "module_base", None):
            messagebox.showinfo("提示", "请先取句柄附加")
            return False
        return True

    def _ix_scan(self) -> None:
        """Scan nearby matters into tree (IX rows). @author by ak"""
        if not self._ix_require_session():
            return
        scan_r, _max_d, prefer = self._ix_params()
        host = self._host_pos_tuple()
        self.status.set("INTERACT")
        self.log(f"交互扫描 matter r={scan_r} prefer_actionable={prefer}")

        def worker():
            try:
                items = scan_nearby_matters(
                    self.session,
                    host,
                    radius=scan_r,
                    limit=40,
                    prefer_actionable=prefer,
                    log=self.log,
                )
                self.msg_q.put(("__IX_SCAN__", [t.to_dict() for t in items]))
            except Exception as e:
                self.msg_q.put(("__ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _ix_nearest(self) -> None:
        """Select nearest non-mark matter into list. @author by ak"""
        if not self._ix_require_session():
            return
        scan_r, _max_d, _prefer = self._ix_params()
        host = self._host_pos_tuple()
        self.status.set("INTERACT")
        self.log(f"交互选最近目标 r={scan_r}")

        def worker():
            try:
                tgt = nearest_actionable_matter(
                    self.session, host, radius=scan_r, log=self.log
                )
                self.msg_q.put(("__IX_NEAREST__", tgt.to_dict() if tgt else None))
            except Exception as e:
                self.msg_q.put(("__ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _ix_format_option(self, t: dict, idx: int) -> str:
        """One combobox line for a scan target. @author by ak"""
        kind = str(t.get("kind") or classify_matter_name(str(t.get("name") or "")))
        name = t.get("name") or "?"
        dist = t.get("dist")
        oid = t.get("obj_id")
        tid = t.get("tid")
        parts = [f"{idx + 1}.", f"[{kind_label(kind)}]", str(name)]
        if dist is not None:
            parts.append(f"d={float(dist):.1f}")
        if oid is not None:
            parts.append(f"id={oid}")
        if tid is not None:
            parts.append(f"tid={tid}")
        return " ".join(parts)

    def _ix_refresh_combo(self, select_idx: int | None = 0) -> None:
        """Rebuild target combobox from _ix_targets. @author by ak"""
        labels: list[str] = []
        for i, t in enumerate(self._ix_targets):
            kind = str(t.get("kind") or classify_matter_name(str(t.get("name") or "")))
            t["kind"] = kind
            labels.append(self._ix_format_option(t, i))
        self._ix_combo_labels = labels
        self.ix_target_combo["values"] = labels
        if not labels:
            self.ix_target_var.set("")
            self._ix_selected_idx = None
            return
        if select_idx is None or select_idx < 0 or select_idx >= len(labels):
            select_idx = 0
        self._ix_selected_idx = int(select_idx)
        self.ix_target_var.set(labels[self._ix_selected_idx])
        try:
            self.ix_target_combo.current(self._ix_selected_idx)
        except Exception:
            pass

    def _ix_on_combo_selected(self, _evt=None) -> None:
        """Track combobox selection index. @author by ak"""
        val = self.ix_target_var.get()
        if val in self._ix_combo_labels:
            self._ix_selected_idx = self._ix_combo_labels.index(val)
        else:
            try:
                self._ix_selected_idx = int(self.ix_target_combo.current())
            except Exception:
                self._ix_selected_idx = None
        tgt = self._ix_selected_target()
        if tgt:
            kl = kind_label(str(tgt.get("kind") or ""))
            self.var_ix.set(
                f"已选: [{kl}] {tgt.get('name')} id={tgt.get('obj_id')} "
                f"d={tgt.get('dist')} action={suggest_action(str(tgt.get('kind') or ''))}"
            )

    def _ix_selected_target(self) -> dict | None:
        """Resolve target from interact combobox. @author by ak"""
        if not self._ix_targets:
            return None
        idx = self._ix_selected_idx
        if idx is None:
            try:
                idx = int(self.ix_target_combo.current())
            except Exception:
                idx = -1
        if idx is None or idx < 0 or idx >= len(self._ix_targets):
            # try match current label
            val = self.ix_target_var.get()
            if val in self._ix_combo_labels:
                idx = self._ix_combo_labels.index(val)
            else:
                return None
        t = dict(self._ix_targets[idx])
        t["kind"] = str(t.get("kind") or classify_matter_name(str(t.get("name") or "")))
        return t

    def _ix_kind_tag(self, kind: str) -> tuple[str, int]:
        """Tree type tag and score for kind (mirror list). @author by ak"""
        k = kind or "other"
        if k == "pickup":
            return "IXP", 80
        if k == "gather":
            return "IXG", 90
        if k == KIND_MARK:
            return "IXM", 15
        return "IX", 50

    def _apply_ix_scan(self, items: list[dict]) -> None:
        """Fill combobox + optional tree mirror. @author by ak"""
        self._ix_targets = list(items or [])
        # optional: also show in break0 tree for inspection
        for i in self.tree.get_children():
            vals = self.tree.item(i, "values")
            if vals and str(vals[0]).startswith("IX"):
                self.tree.delete(i)
        counts: dict[str, int] = {}
        for i, t in enumerate(self._ix_targets):
            kind = str(t.get("kind") or classify_matter_name(str(t.get("name") or "")))
            t["kind"] = kind
            counts[kind] = counts.get(kind, 0) + 1
            tag, score = self._ix_kind_tag(kind)
            label = self._ix_format_option(t, i)
            self.tree.insert(
                "",
                tk.END,
                values=(
                    tag,
                    label[:96],
                    hex(int(t.get("ptr") or 0)),
                    score,
                ),
            )
        self._ix_refresh_combo(select_idx=0 if self._ix_targets else None)
        summary = " ".join(f"{kind_label(k)}={n}" for k, n in sorted(counts.items()))
        if self._ix_targets:
            self.var_ix.set(
                f"扫描 {len(self._ix_targets)} | {summary} | 在「目标」下拉中选择后执行"
            )
        else:
            self.var_ix.set("扫描 0 | 附近无 matter")
        self.log(f"交互扫描完成: {len(self._ix_targets)} ({summary})")
        self.status.set("ATTACHED" if self.session else "IDLE")

    def _apply_ix_nearest(self, tgt: dict | None) -> None:
        """Select nearest in combobox after merge. @author by ak"""
        if not tgt:
            self.var_ix.set("附近无 matter")
            self.log("交互最近: 无目标")
            self.status.set("ATTACHED" if self.session else "IDLE")
            return
        # put nearest first
        ptr = tgt.get("ptr")
        rest = [t for t in self._ix_targets if t.get("ptr") != ptr]
        self._ix_targets = [tgt] + rest
        self._apply_ix_scan(self._ix_targets)
        self._ix_refresh_combo(select_idx=0)
        kl = kind_label(str(tgt.get("kind") or ""))
        self.var_ix.set(
            f"已选最近: [{kl}] {tgt.get('name')} id={tgt.get('obj_id')} "
            f"d={tgt.get('dist')} — 可点「执行选中」"
        )
        self.log(f"交互最近: {self.var_ix.get()}")

    def _ix_execute_selected(self) -> None:
        """
        Execute user-selected target; kind auto-classified for action path.
        @author by ak
        """
        if not self._ix_require_session():
            return
        tgt = self._ix_selected_target()
        if not tgt:
            messagebox.showinfo(
                "提示",
                "请先「扫描」，再在「目标」下拉中选择一项，然后「执行选中」。",
            )
            return
        kind = str(tgt.get("kind") or classify_matter_name(str(tgt.get("name") or "")))
        tgt["kind"] = kind
        label = kind_label(kind)
        action = suggest_action(kind)
        if kind == KIND_MARK or action == "skip":
            messagebox.showinfo(
                "跳过",
                f"「{tgt.get('name')}」判定为标志类，默认不执行。",
            )
            return
        if not tgt.get("obj_id"):
            messagebox.showinfo("提示", "选中目标没有 id，无法执行")
            return
        _scan_r, max_d, _p = self._ix_params()
        dist = tgt.get("dist")
        force_far = False
        if dist is not None and float(dist) > float(max_d):
            if not messagebox.askyesno(
                "距离较远",
                f"dist={float(dist):.2f} > max={max_d:g}\n"
                f"类型={label} 动作={action}\n仍要执行？\n（可先「走近选中」）",
            ):
                return
            force_far = True
        if not messagebox.askyesno(
            "确认执行选中",
            f"目标: {tgt.get('name')}\n"
            f"类型: {label}\n动作: {action}\n"
            f"id={tgt.get('obj_id')} tid={tgt.get('tid')} dist={dist}\n\n"
            "远程调用，仅本机授权测试。",
        ):
            return
        self.status.set("INTERACT")
        self.log(
            f"交互执行 kind={kind}({label}) action={action} "
            f"id={tgt.get('obj_id')} name={tgt.get('name')!r}"
        )

        def worker():
            try:
                r = execute_target(
                    self.session,
                    tgt,
                    max_dist=None if force_far else max_d,
                    log=self.log,
                )
                self.msg_q.put(("__IX_EXEC__", r.to_dict(), tgt))
            except Exception as e:
                self.msg_q.put(("__ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _ix_move_to_selected(self) -> None:
        """HostMove to selected interact target coords. @author by ak"""
        if not self._ix_require_session():
            return
        tgt = self._ix_selected_target()
        if not tgt or tgt.get("x") is None:
            messagebox.showinfo("提示", "请先扫描并在下拉中选择有坐标的目标")
            return
        mode = self._path_resolve_mode()
        pt = PathTarget(
            x=float(tgt["x"]),
            y=float(tgt.get("y") or 0),
            z=float(tgt.get("z") or 0),
            mode=mode,
            map_hint=str(tgt.get("name") or "ix"),
        )
        self.path_x.set(f"{pt.x:.3f}")
        self.path_y.set(f"{pt.y:.3f}")
        self.path_z.set(f"{pt.z:.3f}")
        if not (self.path_mode.get() or "").strip():
            # show resolved mode for user visibility
            self.path_mode.set(str(mode) if mode else "")
        label = kind_label(
            str(tgt.get("kind") or classify_matter_name(str(tgt.get("name") or "")))
        )
        if not messagebox.askyesno(
            "走近选中",
            f"[{label}] {tgt.get('name')}\n"
            f"HostMove mode={mode} → ({pt.x:.2f},{pt.y:.2f},{pt.z:.2f})",
        ):
            return
        self.status.set("AUTOMOVE")
        self.log(f"交互走近 [{label}] {tgt.get('name')} mode={mode} → HostMove")

        def worker():
            try:
                before = None
                try:
                    sp0 = read_scene_position(self.session, log=lambda _m: None)
                    if sp0.ok and sp0.scene_pos:
                        before = sp0.scene_pos
                except Exception:
                    pass
                r = host_move_to(self.session, pt, log=self.log)
                d = r.to_dict()
                # short observe: did host pos change toward target?
                moved = None
                try:
                    import time as _t

                    _t.sleep(1.2)
                    sp1 = read_scene_position(self.session, log=lambda _m: None)
                    if before and sp1.ok and sp1.scene_pos:
                        b, a = before, sp1.scene_pos
                        moved = (
                            (a[0] - b[0]) ** 2
                            + (a[2] - b[2]) ** 2
                        ) ** 0.5
                        d["observe_xz_delta"] = moved
                        d["observe_pos"] = list(a)
                        self.log(
                            f"走近观察 Δxz≈{moved:.2f} "
                            f"pos=({a[0]:.2f},{a[1]:.2f},{a[2]:.2f})"
                        )
                except Exception as e:
                    self.log(f"走近观察失败: {e}")
                self.msg_q.put(("__AUTOMOVE__", d, "ix_move"))
            except Exception as e:
                self.msg_q.put(("__ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_ix_exec(self, res: dict, tgt: dict | None) -> None:
        """Log execute_target result. @author by ak"""
        ok = bool(res.get("ok"))
        err = res.get("error")
        note = res.get("note") or ""
        action = res.get("action") or ""
        kind = str(res.get("kind") or (tgt or {}).get("kind") or "")
        name = (tgt or {}).get("name") or ""
        pick = res.get("pick") or {}
        oid = pick.get("obj_id") or (tgt or {}).get("obj_id")
        label = kind_label(kind)
        if ok:
            self.var_ix.set(
                f"执行OK [{label}/{action}] via {pick.get('method')} "
                f"ret={pick.get('ret')} id={oid} {name}"
            )
            attempts = pick.get("attempts") or []
            if attempts:
                self.log(
                    "交互多路径结果: "
                    + " | ".join(
                        f"{a.get('method')}:ret={a.get('ret')} err={a.get('error')}"
                        for a in attempts
                    )
                )
            self.log(
                "看游戏: 是否选中目标/读条/开箱。ret 全 0 不代表成功交互。"
            )
        else:
            self.var_ix.set(f"执行失败 [{label}/{action}] {err} | {name}")
        self.log(
            f"交互执行 ok={ok} kind={kind} action={action} id={oid} "
            f"name={name!r} err={err} note={note} pick={pick}"
        )
        self.status.set("ATTACHED" if self.session else "IDLE")

    def _selected_pid(self) -> int | None:
        """PID from dragged window / attached session. @author by ak"""
        if self.current_window and self.current_window.pid:
            return int(self.current_window.pid)
        if self.session and self.session.pid:
            return int(self.session.pid)
        return None

    def _pkt_refresh_core(self) -> None:
        """Load core plain/cipher hooks into status line. @author by ak"""
        try:
            from packet.crypto_knowledge import CORE_HOOKS, PIPELINE_TEXT, core_points_markdown
            from packet.auto_analyze import packets_dir

            bits = []
            for h in CORE_HOOKS:
                if h.tag in ("PLAIN", "SEND10", "UPD_OUT"):
                    va = f"0x{h.va:08X}" if h.va else "ws2_32.send"
                    bits.append(f"{h.tag}@{va}")
            self.var_pkt_core.set(
                " | ".join(bits)
                + "  ||  "
                + " ".join(PIPELINE_TEXT.strip().splitlines()[0:1])
            )
            # ensure CORE_POINTS.md exists
            md = packets_dir() / "CORE_POINTS.md"
            if not md.is_file():
                md.write_text(core_points_markdown(), encoding="utf-8")
        except Exception as e:
            self.var_pkt_core.set(f"核心点加载失败: {e}")

    def _pkt_copy_core(self) -> None:
        """Copy core points markdown to clipboard. @author by ak"""
        try:
            from packet.crypto_knowledge import core_points_markdown

            md = core_points_markdown()
            self.clipboard_clear()
            self.clipboard_append(md)
            self.log("已复制核心点 Markdown 到剪贴板")
            self.var_pkt_ana.set("核心点已复制")
        except Exception as e:
            self.log(f"复制核心点失败: {e}")

    def _pkt_open_core_md(self) -> None:
        """Open CORE_POINTS.md in explorer/default app. @author by ak"""
        try:
            from packet.crypto_knowledge import core_points_markdown
            from packet.auto_analyze import packets_dir
            import os

            p = packets_dir() / "CORE_POINTS.md"
            p.write_text(core_points_markdown(), encoding="utf-8")
            os.startfile(str(p))  # type: ignore[attr-defined]
        except Exception as e:
            self.log(f"打开 CORE_POINTS 失败: {e}")

    def _pkt_pick_log(self) -> None:
        """Pick x32dbg runtime log file. @author by ak"""
        from tkinter import filedialog
        from packet.auto_analyze import packets_dir

        initial = packets_dir()
        path = filedialog.askopenfilename(
            title="选择 x32dbg / 运行时日志",
            initialdir=str(initial),
            filetypes=(
                ("Log / Text", "*.log;*.txt"),
                ("All", "*.*"),
            ),
        )
        if path:
            self.pkt_log_path.set(path)
            self.log(f"封包日志: {path}")

    def _pkt_write_recipes(self) -> None:
        """Generate x32dbg scripts + once-setup checklist. @author by ak"""
        try:
            from packet.x32dbg_recipes import write_recipes

            paths = write_recipes()
            self.log("已生成 x32dbg 脚本:")
            for k, v in paths.items():
                self.log(f"  {k}: {v}")
            self.var_pkt_ana.set(
                f"脚本已写: {paths.get('shot_script', '')} | 清单: {paths.get('checklist', '')}"
            )
            # seed listbox with short guide
            self.pkt_ana_list.delete(0, tk.END)
            self.pkt_ana_list.insert(tk.END, "[配置一次] 附加 xajh → 加载 x32dbg_auto_plain_cipher.txt")
            self.pkt_ana_list.insert(tk.END, "[配置一次] 断点日志: PLAIN @0x00DAFAF0 / SEND10 @ws2_32.send")
            self.pkt_ana_list.insert(tk.END, "[日常] 触发功能 → 存日志 → 点「分析日志」")
            self.pkt_ana_list.insert(tk.END, f"[文件] {paths.get('checklist')}")
        except Exception as e:
            self.log(f"生成脚本失败: {e}")
            messagebox.showerror("生成脚本", str(e))

    def _pkt_open_report(self) -> None:
        """Open latest auto_analyze report. @author by ak"""
        try:
            import os
            from pathlib import Path
            from packet.auto_analyze import packets_dir

            cand = None
            if self._last_pkt_analyze:
                files = self._last_pkt_analyze.get("files") or {}
                for k in ("latest_md", "md"):
                    if files.get(k) and Path(files[k]).is_file():
                        cand = Path(files[k])
                        break
            if cand is None:
                p = packets_dir() / "auto_analyze_latest.md"
                cand = p if p.is_file() else packets_dir() / "CORE_POINTS.md"
            if not cand.is_file():
                messagebox.showinfo("报告", "尚无报告，请先分析日志")
                return
            os.startfile(str(cand))  # type: ignore[attr-defined]
            self.log(f"打开报告: {cand}")
        except Exception as e:
            self.log(f"打开报告失败: {e}")

    def _pkt_open_issues_dir(self) -> None:
        """Open .issues/packets. @author by ak"""
        try:
            import os
            from packet.auto_analyze import packets_dir

            d = packets_dir()
            os.startfile(str(d))  # type: ignore[attr-defined]
            self.log(f"打开目录: {d}")
        except Exception as e:
            self.log(f"打开 issues 失败: {e}")

    def _pkt_analyze_latest(self) -> None:
        """Analyze newest discovered log. @author by ak"""
        if self._pkt_analyze_busy:
            messagebox.showinfo("提示", "分析进行中")
            return
        self._pkt_analyze_busy = True
        self.var_pkt_ana.set("正在发现并分析最新日志…")
        self.log("封包自动分析: latest")

        def worker() -> None:
            try:
                from packet.auto_analyze import analyze_latest, ui_rows

                r = analyze_latest(log=lambda m: self.msg_q.put(m if m.startswith("[") else f"[PKT] {m}"))
                self.msg_q.put(("__PKT_ANALYZE__", r.as_dict(), ui_rows(r)))
            except Exception as e:
                self.msg_q.put(("__PKT_ANALYZE__", {"ok": False, "error": str(e), "findings": [str(e)]}, [f"[ERR] {e}"]))

        threading.Thread(target=worker, daemon=True).start()

    def _pkt_analyze_log(self) -> None:
        """Analyze selected log path (or latest if empty). @author by ak"""
        if self._pkt_analyze_busy:
            messagebox.showinfo("提示", "分析进行中")
            return
        path_s = (self.pkt_log_path.get() or "").strip()
        self._pkt_analyze_busy = True
        self.var_pkt_ana.set("分析中…")
        self.log(f"封包自动分析: {path_s or '(latest)'}")

        def worker() -> None:
            try:
                from pathlib import Path
                from packet.auto_analyze import analyze_latest, analyze_paths, ui_rows

                if path_s:
                    r = analyze_paths(
                        [Path(path_s)],
                        log=lambda m: self.msg_q.put(m if isinstance(m, str) else str(m)),
                    )
                else:
                    r = analyze_latest(
                        log=lambda m: self.msg_q.put(m if isinstance(m, str) else str(m)),
                    )
                self.msg_q.put(("__PKT_ANALYZE__", r.as_dict(), ui_rows(r)))
            except Exception as e:
                self.msg_q.put(
                    (
                        "__PKT_ANALYZE__",
                        {"ok": False, "error": str(e), "findings": [str(e)]},
                        [f"[ERR] {e}"],
                    )
                )

        threading.Thread(target=worker, daemon=True).start()

    def _apply_pkt_analyze(self, data: dict, rows: list | None = None) -> None:
        """Apply auto-analyze result to listbox + status. @author by ak"""
        self._pkt_analyze_busy = False
        self._last_pkt_analyze = data if isinstance(data, dict) else {}
        ok = bool(self._last_pkt_analyze.get("ok"))
        summary = self._last_pkt_analyze.get("summary") or self._last_pkt_analyze.get("error") or ""
        self.var_pkt_ana.set(summary if summary else ("OK" if ok else "失败"))
        src = self._last_pkt_analyze.get("source") or ""
        if src:
            self.pkt_log_path.set(str(src))
        self.pkt_ana_list.delete(0, tk.END)
        line_rows = rows or []
        if not line_rows:
            for f in self._last_pkt_analyze.get("findings") or []:
                line_rows.append(f"[结论] {f}")
            for p in (self._last_pkt_analyze.get("pairs") or [])[:30]:
                line_rows.append(
                    f"C:{p.get('send10')} ← P:{p.get('plain_hex10')} {p.get('shell') or ''}"
                )
        for line in line_rows:
            self.pkt_ana_list.insert(tk.END, line)
        # log key findings
        self.log(f"自动解包: {summary}")
        for f in (self._last_pkt_analyze.get("findings") or [])[:6]:
            self.log(f"  · {f}")
        files = self._last_pkt_analyze.get("files") or {}
        if files.get("latest_md"):
            self.log(f"报告: {files['latest_md']}")
        if ok:
            self.status.set("ATTACHED" if self.session else "IDLE")
        else:
            self.status.set("ERROR")

    def _start_pkt_capture(self):
        """Start baseline+action packet capture using selected HWND PID. @author by ak"""
        if self._pkt_busy:
            messagebox.showinfo("提示", "已有封包捕获在进行中")
            return
        pid = self._selected_pid()
        if not pid:
            messagebox.showinfo("提示", "请先拖到游戏窗口取句柄，PID 将直接复用该句柄")
            return

        action = (self.pkt_action.get() or "pickup").strip()
        if action == "custom":
            # use notes first token or fallback
            note = (self.pkt_notes.get() or "").strip()
            action = note.split()[0] if note else "custom"

        try:
            baseline = float(self.pkt_baseline.get() or "0")
        except ValueError:
            messagebox.showerror("参数错误", "baseline 必须是数字秒")
            return
        try:
            duration = float(self.pkt_duration.get() or "0")
        except ValueError:
            messagebox.showerror("参数错误", "动作窗 必须是数字秒")
            return
        if baseline < 0 or duration < 0:
            messagebox.showerror("参数错误", "秒数不能为负")
            return

        manual = bool(self.pkt_manual_stop.get())
        # manual mode: duration is max wait; user can stop early
        duration_arg = duration if duration > 0 else None
        if manual and (duration_arg is None or duration_arg <= 0):
            duration_arg = 300.0  # safety cap 5min

        notes = (self.pkt_notes.get() or "").strip()
        map_hint = ""
        if self.current_result and (self.current_result.map_display or self.current_result.map_name):
            map_hint = self.current_result.map_display or self.current_result.map_name or ""
        if map_hint and map_hint not in notes:
            notes = (notes + f" map={map_hint}").strip()

        self._pkt_stop.clear()
        self._pkt_abort.clear()
        self._pkt_busy = True
        self._pkt_phase = "baseline" if baseline > 0 else "action"
        self.pkt_start_btn.configure(state=tk.DISABLED, bg="#484f58")
        # End button enabled for whole capture so user always has a control;
        # during baseline it aborts; during action it ends the action window.
        self.pkt_stop_btn.configure(state=tk.NORMAL, bg="#da3633")
        self.status.set("PKT_CAP")
        self._set_pkt_phase_ui("baseline" if baseline > 0 else "action", action, pid, baseline)
        self.log(
            f"封包捕获开始: action={action} pid={pid} baseline={baseline}s "
            f"duration={duration_arg} manual_stop={manual}"
        )
        if manual:
            self.log("流程: ①baseline → ②动作窗开箱 → ③结束")
        else:
            self.log(f"流程: 先空闲 baseline → 动作窗固定 {duration_arg}s，请在动作窗内完成操作")

        def _log_phase(m: str) -> None:
            """Mirror capture logs and flip UI phase markers. @author by ak"""
            self.log(m)
            low = m.lower()
            if "[baseline]" in low or "stay idle" in low:
                self._pkt_phase = "baseline"
                self.msg_q.put(("__PKT_PHASE__", "baseline", action, pid, baseline))
            if "[action]" in low or "action running" in low or "_action]" in low:
                self._pkt_phase = "action"
                self.msg_q.put(("__PKT_PHASE__", "action", action, pid, baseline))
            if "capturing for" in low and "action" in self._pkt_phase:
                self.msg_q.put(("__PKT_PHASE__", "action", action, pid, baseline))

        def worker():
            try:
                from packet.action_capture import run_action_capture

                def should_stop_action() -> bool:
                    # Only end the *action* window via stop flag; abort also ends it.
                    return self._pkt_stop.is_set() or self._pkt_abort.is_set()

                def should_stop_baseline() -> bool:
                    # Baseline only aborts on abort flag, not on "结束动作窗".
                    return self._pkt_abort.is_set()

                # Patch: run_action_capture uses one should_stop for both windows.
                # We pass a dual-phase checker that ignores stop during baseline.
                phase = {"name": "baseline" if baseline > 0 else "action"}

                def should_stop() -> bool:
                    if self._pkt_abort.is_set():
                        return True
                    if phase["name"] == "baseline":
                        return False  # 结束动作窗 must NOT cut baseline short
                    return self._pkt_stop.is_set()

                # Wrap log to advance phase when action window starts.
                def log_and_phase(m: str) -> None:
                    if "action]" in m.lower() or m.startswith("[action]") or "_action]" in m:
                        phase["name"] = "action"
                    if "[baseline]" in m.lower() or "stay idle" in m.lower():
                        phase["name"] = "baseline"
                    _log_phase(m)

                meta = run_action_capture(
                    action=action,
                    baseline_sec=baseline,
                    duration_sec=duration_arg,
                    pid=pid,
                    notes=notes,
                    do_diff=True,
                    log=log_and_phase,
                    should_stop=(should_stop if manual else None),
                    wait_enter_for_action=False,
                )
                self.msg_q.put(("__PKT_DONE__", meta))
            except Exception as e:
                self.msg_q.put(("__PKT_ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _set_pkt_phase_ui(
        self, phase: str, action: str = "", pid: int | None = None, baseline: float = 0
    ) -> None:
        """Update phase label / status line for packet capture. @author by ak"""
        self._pkt_phase = phase
        if phase == "baseline":
            self.var_pkt_phase.set("① baseline")
            self.var_pkt.set(
                f"站桩… action={action} pid={pid} {baseline}s（结束=中止全程）"
            )
            self.pkt_stop_btn.configure(text="■ 中止", bg="#6e7681")
        elif phase == "action":
            self.var_pkt_phase.set("② 动作窗")
            self.var_pkt.set(f"动作中… action={action} pid={pid} | 做完点结束")
            self.pkt_stop_btn.configure(text="■ 结束", bg="#da3633", state=tk.NORMAL)
        elif phase == "finishing":
            self.var_pkt_phase.set("收尾…")
            self.var_pkt.set("结束请求已发送，正在 etl2txt / 差分…")
        else:
            self.var_pkt_phase.set("阶段: 空闲")
            self.pkt_stop_btn.configure(text="■ 结束", bg="#6e7681", state=tk.DISABLED)

    def _stop_pkt_capture(self):
        """
        User control for capture windows.

        - baseline phase: abort whole capture
        - action phase: end action window and run diff
        @author by ak
        """
        if not self._pkt_busy:
            return
        phase = self._pkt_phase
        if phase == "baseline":
            self._pkt_abort.set()
            self._pkt_stop.set()
            self.log("已中止捕获（baseline 阶段）…")
            self._set_pkt_phase_ui("finishing")
            return
        self._pkt_stop.set()
        self.log("已请求结束动作窗（请等待 pktmon 转换与差分）…")
        self._set_pkt_phase_ui("finishing")

    def _open_pkt_dir(self):
        """Open captures/tcp in Explorer. @author by ak"""
        try:
            from common.paths import CAPTURE_DIR

            path = CAPTURE_DIR / "tcp"
            path.mkdir(parents=True, exist_ok=True)
            import os

            os.startfile(str(path))  # type: ignore[attr-defined]
            self.log(f"打开目录: {path}")
        except Exception as e:
            self.log(f"打开目录失败: {e}")

    def _pkti_start(self):
        """Arm CD1740 game-side interception on the attached game. @author by ak"""
        if self._pkti_state is not None:
            messagebox.showinfo("封包拦截", "已在拦截中，请先停止")
            return
        # Prefer the actively attached session so we never hook a stale window.
        pid = None
        if self.session and getattr(self.session, "pid", None):
            pid = int(self.session.pid)
        else:
            pid = self._selected_pid()
        if not pid:
            messagebox.showinfo("封包拦截", "请先拖入/破0附加游戏进程获取 PID")
            return
        self.pkti_start_btn.configure(state=tk.DISABLED, bg="#484f58")
        self.pkti_stop_btn.configure(state=tk.NORMAL, bg="#da3633")
        self.var_pkti_status.set("正在附加拦截…")

        def worker():
            try:
                # "system" = CD1740 (safe, matches black shield). NEVER "all"
                # (0xDAFAF0 crypto entry is integrity-checked and crashes the game).
                st = InterceptState(pid, target="system")
                st.start()
                self.msg_q.put(("__PKTI_STARTED__", pid, st))
            except Exception as e:  # noqa: BLE001
                self.msg_q.put(("__PKTI_ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _pkti_poll(self):
        """Refresh captured packets from the game-side ring buffer. @author by ak"""
        self._pkti_after_id = None
        st = self._pkti_state
        if st is None or not st.armed:
            return
        try:
            new = st.poll()
            for rec in new:
                self._pkti_records.append(rec)
            if new:
                self._pkti_render_list()
                self.var_pkti_status.set(f"已拦截 {len(self._pkti_records)} 条")
        except Exception as e:  # noqa: BLE001
            self.log(f"封包拦截轮询错误: {e}")
        try:
            self._pkti_after_id = self.after(800, self._pkti_poll)
        except Exception:  # noqa: BLE001
            pass

    def _pkti_stop(self):
        """Restore hook, read all records, export to file. @author by ak"""
        st = self._pkti_state
        if st is None:
            return
        self.var_pkti_status.set("正在停止并导出…")

        def worker():
            try:
                records = st.stop()
                self.msg_q.put(("__PKTI_STOPPED__", records))
            except Exception as e:  # noqa: BLE001
                self.msg_q.put(("__PKTI_ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _pkti_resend(self):
        """Re-send the selected captured packet via game CD1740. @author by ak"""
        pid = self._selected_pid()
        if pid is None:
            messagebox.showinfo("封包拦截", "没有可用的游戏 PID")
            return
        sel = self.pkti_list.curselection()
        if not sel:
            messagebox.showinfo("封包拦截", "请先在列表中选中一条要重发的封包")
            return
        rec_index = self._pkti_view_start + int(sel[0])
        if rec_index < 0 or rec_index >= len(self._pkti_records):
            return
        rec = self._pkti_records[rec_index]
        data = rec.get("data") or b""

        def worker():
            try:
                ret = replay_packet(pid, data)
                self.msg_q.put(("__PKTI_RESEND__", ret, rec.get("len"),
                                data[:16].hex().upper()))
            except Exception as e:  # noqa: BLE001
                self.msg_q.put(("__PKTI_ERROR__", f"重发失败: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _pkti_record_path(self) -> Path:
        """Return the default JSONL action-recording path."""
        try:
            from packet.auto_analyze import packets_dir

            directory = packets_dir()
        except Exception:  # noqa: BLE001
            directory = Path(PROJECT_ROOT) / ".issues" / "packets"
        directory.mkdir(parents=True, exist_ok=True)
        pid = self._selected_pid() or getattr(self.session, "pid", 0) or 0
        return directory / f"intercept_action_{int(pid)}_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"

    def _pkti_save(self, records=None, *, path: Path | None = None) -> Path | None:
        """Save the current capture as a portable JSONL action recording."""
        rows = list(records if records is not None else self._pkti_records)
        if not rows:
            messagebox.showinfo("封包拦截", "当前没有可保存的封包记录")
            return None
        target = path or self._pkti_recording_path or self._pkti_record_path()
        try:
            pid = self._selected_pid() or getattr(self.session, "pid", 0) or 0
            self._pkti_recording_path = write_recording(target, int(pid), rows, label="开发面板动作")
            self.log(f"动作记录已写入: {self._pkti_recording_path}")
            self.var_pkti_status.set(f"已保存 {len(rows)} 条: {self._pkti_recording_path.name}")
            return self._pkti_recording_path
        except Exception as e:  # noqa: BLE001
            self.log(f"动作记录保存失败: {e}")
            messagebox.showerror("封包拦截", f"保存动作记录失败:\n{e}")
            return None

    def _pkti_open(self):
        """Load a JSONL action recording into the list for inspection/replay."""
        path = filedialog.askopenfilename(
            title="打开封包动作记录",
            initialdir=str(Path(PROJECT_ROOT) / ".issues" / "packets"),
            filetypes=(("JSONL 记录", "*.jsonl"), ("所有文件", "*.*")),
        )
        if not path:
            return
        try:
            meta, records = read_recording(path)
            self._pkti_recording_path = Path(path)
            self._pkti_records = records
            self._pkti_mark_index = len(records)
            self._pkti_view_start = 0
            self._pkti_render_list()
            self.var_pkti_status.set(f"已打开 {len(records)} 条 · pid={meta.get('pid', '?')}")
            self.log(f"动作记录已打开: {path} ({len(records)} 条)")
        except Exception as e:  # noqa: BLE001
            self.log(f"动作记录打开失败: {e}")
            messagebox.showerror("封包拦截", f"打开动作记录失败:\n{e}")

    def _pkti_replay(self):
        """Replay the loaded/current action sequence with captured timing."""
        pid = self._selected_pid() or getattr(self.session, "pid", None)
        if not pid:
            messagebox.showinfo("封包拦截", "没有可用的游戏 PID")
            return
        if not self._pkti_records:
            messagebox.showinfo("封包拦截", "请先拦截、打开或保存一组动作记录")
            return
        if self._pkti_state is not None:
            messagebox.showinfo("封包拦截", "请先停止拦截，再回放动作")
            return
        rows = list(self._pkti_records)
        self._pkti_replay_stop.clear()
        self.var_pkti_status.set(f"回放中 0/{len(rows)}…")

        def worker():
            try:
                results = replay_records(
                    int(pid), rows, gap_scale=1.0, max_gap_s=3.0,
                    min_gap_s=0.01, stop_event=self._pkti_replay_stop,
                )
                self.msg_q.put(("__PKTI_REPLAY_DONE__", len(results), len(rows),
                                self._pkti_replay_stop.is_set()))
            except Exception as e:  # noqa: BLE001
                self.msg_q.put(("__PKTI_ERROR__", f"动作回放失败: {e}"))

        threading.Thread(target=worker, daemon=True, name="pkti-replay").start()

    def _pkti_stop_replay(self):
        """Stop a pending action replay between packets."""
        self._pkti_replay_stop.set()
        self.var_pkti_status.set("已请求停止回放")

    def _pkti_clear(self):
        """Clear the on-screen list (does not disarm). @author by ak"""
        self._pkti_records.clear()
        self._pkti_mark_index = 0
        self._pkti_view_start = 0
        self.pkti_list.delete(0, tk.END)
        self.var_pkti_status.set("列表已清空")
        self.var_pkti_detail.set("选中一条封包查看解析标注")

    def _pkti_prepare_network_capture(self):
        """Prepare the full network capture workflow for ordinary game packets."""
        self.pkt_action.set("boss_summon")
        self.pkt_notes.set("武尊堂 提前召唤Boss")
        self.var_pkti_status.set("已切换：请在上方网络捕获点击开始捕获")
        self.log("封包观察: 普通包请使用 pktmon，动作=boss_summon")
        try:
            self._pkt_canvas.yview_moveto(0.0)
        except Exception:
            pass
    def _pkti_render_list(self) -> None:
        """Render the capture list from the current observation boundary."""
        self.pkti_list.delete(0, tk.END)
        start = max(0, min(self._pkti_view_start, len(self._pkti_records)))
        for index in range(start, len(self._pkti_records)):
            self.pkti_list.insert(tk.END, format_annotated_record(self._pkti_records[index], index))

    def _pkti_mark_before(self):
        """Mark the current capture count before a game-side click."""
        self._pkti_mark_index = len(self._pkti_records)
        self._pkti_view_start = self._pkti_mark_index
        self._pkti_render_list()
        self.var_pkti_status.set(f"已标记点击前 · 等待新增封包（当前 {self._pkti_mark_index} 条）")
        self.log(f"封包观察标记: 点击前 index={self._pkti_mark_index}")

    def _pkti_show_after(self):
        """Show only packets captured after the last pre-click marker."""
        self._pkti_view_start = self._pkti_mark_index
        self._pkti_render_list()
        count = max(0, len(self._pkti_records) - self._pkti_view_start)
        self.var_pkti_status.set(f"只看点击后 · {count} 条候选封包")
        self.log(f"封包观察筛选: 点击后 index>={self._pkti_view_start} count={count}")

    def _pkti_show_all(self):
        """Restore the complete capture list."""
        self._pkti_view_start = 0
        self._pkti_render_list()
        self.var_pkti_status.set(f"显示全部 · {len(self._pkti_records)} 条")
    def _pkti_on_select(self, _e=None):
        """Show the parsed annotation of the selected packet. @author by ak"""
        sel = self.pkti_list.curselection()
        if not sel:
            return
        rec_index = self._pkti_view_start + int(sel[0])
        if rec_index < 0 or rec_index >= len(self._pkti_records):
            return
        rec = self._pkti_records[rec_index]
        try:
            self.var_pkti_detail.set(packet_detail_text(rec))
        except Exception as exc:  # noqa: BLE001
            self.var_pkti_detail.set(f"解析失败: {exc}")

    def _pkti_test_send(self):
        """One-click pipeline check: send the wanzi P1 through game CD1740. If
        interception is armed, the packet should be captured and appear in the
        list on the next poll. @author by ak"""
        pid = self._selected_pid()
        if pid is None:
            messagebox.showinfo("封包拦截", "没有可用的游戏 PID")
            return
        p1 = bytes.fromhex(
            "1F0000000000005F0A0103010001420000000000000000000060C0FCC162FD7242E09E86C201"
        )

        def worker():
            try:
                ret = replay_packet(pid, p1)
                self.msg_q.put(("__PKTI_RESEND__", ret, len(p1), p1[:16].hex().upper()))
                self.log("封包拦截自检: 已发送丸子P1，若拦截生效，1 秒内应出现记录")
            except Exception as e:  # noqa: BLE001
                self.msg_q.put(("__PKTI_ERROR__", f"测试发送失败: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _pkti_apply_started(self, pid: int, st) -> None:
        """Apply __PKTI_STARTED__ on the UI thread. @author by ak"""
        self._pkti_state = st
        self._pkti_records = []
        self.pkti_list.delete(0, tk.END)
        self.var_pkti_status.set(f"拦截中 pid={pid} · 0 条")
        self.log(f"封包拦截已附加: pid={pid} armed={bool(st and st.armed)}")
        try:
            self._pkti_after_id = self.after(800, self._pkti_poll)
        except Exception:  # noqa: BLE001
            pass

    def _pkti_apply_stopped(self, records) -> None:
        """Apply __PKTI_STOPPED__ on the UI thread + export records. @author by ak"""
        self._pkti_state = None
        if self._pkti_after_id:
            try:
                self.after_cancel(self._pkti_after_id)
            except Exception:  # noqa: BLE001
                pass
            self._pkti_after_id = None
        try:
            self.pkti_start_btn.configure(state=tk.NORMAL, bg="#1f6feb")
            self.pkti_stop_btn.configure(state=tk.DISABLED, bg="#6e7681")
        except Exception:  # noqa: BLE001
            pass
        self.var_pkti_status.set(f"已停止，导出 {len(records)} 条")
        self.log(f"封包拦截已停止，共 {len(records)} 条")
        try:
            from packet.auto_analyze import packets_dir

            d = packets_dir()
        except Exception:  # noqa: BLE001
            d = Path(PROJECT_ROOT) / ".issues" / "packets"
        try:
            d.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            out = d / f"intercept_{self._selected_pid() or 0}_{ts}.txt"
            out.write_text(
                "\n".join(format_annotated_record(r, i) for i, r in enumerate(records)) + "\n",
                encoding="utf-8",
            )
            self.log(f"拦截记录已写入: {out}")
        except Exception as e:  # noqa: BLE001
            self.log(f"拦截记录写入失败: {e}")
        self._pkti_save(records)

    def _pkti_apply_resend(self, ret: int, length, head: str) -> None:
        """Apply __PKTI_RESEND__ on the UI thread. @author by ak"""
        self.log(f"封包重发: len={length} head={head} ret=0x{ret:08X}")
        self.var_pkti_status.set(f"已重发: ret=0x{ret:08X}")

    def _pkti_apply_error(self, msg: str) -> None:
        """Apply __PKTI_ERROR__ on the UI thread. @author by ak"""
        self.log(f"封包拦截错误: {msg}")
        if self._pkti_state is None:
            try:
                self.pkti_start_btn.configure(state=tk.NORMAL, bg="#1f6feb")
            except Exception:  # noqa: BLE001
                pass
            self.var_pkti_status.set("空闲")
        else:
            self.var_pkti_status.set("拦截异常，可点停止清理")

    def _apply_pkt_result(self, meta: dict):
        """Show capture/diff summary in tree + status. @author by ak"""
        self._last_pkt_meta = meta
        self._pkt_busy = False
        self._pkt_stop.clear()
        self._pkt_abort.clear()
        self._pkt_phase = "idle"
        self.pkt_start_btn.configure(state=tk.NORMAL, bg="#1f6feb")
        self.pkt_stop_btn.configure(state=tk.DISABLED, bg="#6e7681", text="■ 结束")
        self.var_pkt_phase.set("阶段: 空闲")
        self.status.set("ATTACHED" if self.session else "IDLE")

        action = meta.get("action")
        pid = meta.get("pid")
        local_port = meta.get("local_port")
        prefix = meta.get("prefix")
        files = meta.get("files") or {}
        extract = meta.get("extract") or {}
        base_sum = (extract.get("base") or {}).get("summary") or {}
        act_sum = (extract.get("action") or {}).get("summary") or {}
        self.var_pkt.set(
            f"完成 {action} pid={pid} port={local_port} | "
            f"base c2s={base_sum.get('c2s_with_payload', 0)} "
            f"act c2s={act_sum.get('c2s_with_payload', 0)} | {prefix}"
        )
        self.log(
            f"封包完成: prefix={prefix} local_port={local_port} "
            f"base={base_sum.get('c2s_with_payload')}/{base_sum.get('s2c_with_payload')} "
            f"action={act_sum.get('c2s_with_payload')}/{act_sum.get('s2c_with_payload')}"
        )
        if files.get("diff_json"):
            self.log(f"差分: {files['diff_json']}")
        if files.get("action_jsonl"):
            self.log(f"action jsonl: {files['action_jsonl']}")
        if files.get("notes_md"):
            self.log(f"笔记: {files['notes_md']}")

        # clear previous PKT rows then insert new c2s candidates
        for i in self.tree.get_children():
            vals = self.tree.item(i, "values")
            if vals and str(vals[0]).startswith("PKT"):
                self.tree.delete(i)

        report = meta.get("diff") or {}
        new_c2s = report.get("new_c2s_exact") or []
        size_delta = report.get("size_delta_c2s") or {}
        if size_delta:
            top_sizes = sorted(size_delta.items(), key=lambda kv: abs(int(kv[1])), reverse=True)[:8]
            self.log("c2s size_delta: " + ", ".join(f"{k}:{v:+d}" for k, v in top_sizes))
        for row in new_c2s[:30]:
            head = row.get("hex_head") or (row.get("hex") or "")[:32]
            self.tree.insert(
                "",
                tk.END,
                values=(
                    "PKT",
                    f"c2s len={row.get('len')} {head}",
                    row.get("ts") or "-",
                    row.get("len") or 0,
                ),
            )
        if new_c2s:
            self.log(f"候选列表已填入 {min(len(new_c2s), 30)} 条 action-only c2s（类型=PKT）")
        else:
            self.log("无 exact-new c2s：可能动作窗未操作、PID/端口不对，或包被加密后每次都不同需看 size_delta")

    def _export_result(self):
        if not self.current_result:
            messagebox.showinfo("提示", "没有可导出的结果")
            return
        out_dir = PROJECT_ROOT / ".issues" / "break0"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"break0_{time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(
            json.dumps(self.current_result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.log(f"导出: {path}")

    # ---------- AutoMove / pathfinding ----------
    def _path_resolve_mode(self) -> int:
        """
        Resolve HostMove first arg.
        Live: mode=0 walks; mode=scene_id often returns 0 without move.
        Default 0; only use explicit non-empty field value.
        @author by ak
        """
        raw = (self.path_mode.get() or "").strip()
        if raw == "":
            return 0
        try:
            return int(float(raw))
        except ValueError:
            return 0

    def _path_parse_target(self) -> PathTarget | None:
        """Parse XYZ/mode fields into PathTarget. @author by ak"""
        try:
            x = float(self.path_x.get().strip())
            y = float(self.path_y.get().strip())
            z = float(self.path_z.get().strip())
        except ValueError:
            messagebox.showinfo("提示", "目标 X/Y/Z 必须是数字")
            return None
        mode = self._path_resolve_mode()
        map_hint = ""
        if self.current_result:
            map_hint = self.current_result.map_display or self.current_result.map_name or ""
        return PathTarget(x=x, y=y, z=z, mode=mode, map_hint=map_hint)

    def _path_set_fields(self, x: float, y: float, z: float, mode: int | None = None) -> None:
        """Fill target entries. @author by ak"""
        self.path_x.set(f"{x:.3f}")
        self.path_y.set(f"{y:.3f}")
        self.path_z.set(f"{z:.3f}")
        if mode is not None:
            self.path_mode.set(str(int(mode)))


    # ----- 地图飞行调研 -----
    def _fly_require_session(self):
        """Ensure attached session for fly research. @author by ak"""
        if not getattr(self, "session", None) or not getattr(self.session, "pid", None):
            messagebox.showwarning("地图飞行", "请先破0附着进程")
            return None
        return self.session

    def _fly_refresh_summary(self) -> None:
        """Show static research summary. @author by ak"""
        s = research_summary()
        pkt = s.get("packet") or {}
        coord = s.get("coord_fly") or {}
        page = s.get("default_page") or {}
        death = s.get("death_coord") or {}
        msg = (
            f"dlg={s.get('dlg')} tids={s.get('item_tids')} | "
            f"pkt type=0x{int(pkt.get('type') or 0):X} {pkt.get('layout')} | "
            f"默认页page=0x{int(page.get('packet_page_for_default_tab') or 0xFF):X} | "
            f"坐标直飞={coord.get('supported_by_item_protocol')} | "
            f"死亡伪造={death.get('forge_cache_then_fly_slot2')} "
            f"合法={death.get('legal')}"
        )
        self.var_fly_summary.set(msg)
        self.var_fly.set(msg)
        self.log(f"飞行调研: {msg}")

    def _fly_run_bg(self, title: str, fn) -> None:
        """Run fly research off UI thread. @author by ak"""
        sess = self._fly_require_session()
        if sess is None:
            return
        self.var_fly.set(f"{title}…")
        self.log(f"飞行调研: {title} 开始")

        def work() -> None:
            try:
                r = fn(sess)
                self.msg_q.put(("__FLY__", title, r))
            except Exception as e:
                self.msg_q.put(("__ERROR__", f"飞行调研 {title}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _apply_fly_result(self, title: str, r) -> None:
        """Apply MapFlyResult / dict to UI. @author by ak"""
        d = r.to_dict() if hasattr(r, "to_dict") else (r if isinstance(r, dict) else {"message": str(r)})
        ok = bool(d.get("ok"))
        msg = str(d.get("message") or "")
        self.var_fly.set(f"[{'OK' if ok else 'FAIL'}] {title}: {msg}")
        self.log(f"飞行调研 {title}: ok={ok} {msg}")
        detail = d.get("detail") or {}
        rows = detail.get("rows")
        if isinstance(rows, list):
            self._fly_fill_tree(rows)
        # if preset matched list nested
        nested = (detail.get("list") or {}).get("detail") or {}
        if isinstance(nested.get("rows"), list):
            self._fly_fill_tree(nested.get("rows") or [])

    def _fly_fill_tree(self, rows: list) -> None:
        """Fill fly point tree. @author by ak"""
        tree = getattr(self, "fly_tree", None)
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)
        for row in rows:
            if not isinstance(row, dict):
                continue
            tree.insert(
                "",
                tk.END,
                values=(
                    row.get("slot", ""),
                    row.get("name", ""),
                    row.get("default_tid", ""),
                    row.get("raw_text", ""),
                ),
            )

    def _fly_on_select(self, _evt=None) -> None:
        """Copy selected slot into fly_slot. @author by ak"""
        tree = getattr(self, "fly_tree", None)
        if tree is None:
            return
        sel = tree.selection()
        if not sel:
            return
        vals = tree.item(sel[0], "values") or ()
        if vals:
            self.fly_slot.set(str(vals[0]))

    def _fly_scan_items(self) -> None:
        def fn(sess):
            items = find_fly_items(sess, log=self.log)
            return {
                "ok": True,
                "message": f"找到 {len(items)} 组: " + ", ".join(
                    f"{it.name}x{it.count}(槽{it.slot})" for it in items
                )
                if items
                else "背包无飞行旗/飞行棋",
                "detail": {"items": [it.to_dict() for it in items]},
            }

        self._fly_run_bg("查背包", fn)

    def _fly_open_ui(self) -> None:
        self._fly_run_bg("打开飞行旗", lambda s: open_transmit_flag(s, log=self.log))

    def _fly_read_default_ids(self) -> None:
        def fn(sess):
            ids = read_default_point_ids(sess, log=self.log)
            rows = [
                {"slot": i, "name": "", "default_tid": tid, "raw_text": ""}
                for i, tid in enumerate(ids)
            ]
            return {
                "ok": True,
                "message": f"host+0x7DC ids={ids}",
                "detail": {"rows": rows, "default_ids": ids},
            }

        self._fly_run_bg("读默认点ID", fn)

    def _fly_list_points(self) -> None:
        self._fly_run_bg(
            "读取点位", lambda s: list_ui_transmit_points(s, open_if_needed=True, log=self.log)
        )

    def _fly_preset(self, key: str) -> None:
        page = None
        raw = str(self.fly_page.get() or "").strip()
        if raw != "":
            try:
                page = int(raw) & 0xFF
            except Exception:
                messagebox.showerror("地图飞行", f"page 无效: {raw}")
                return
        # fill slot box with fixed map for visibility
        fixed = {"fuzhou": 0, "shimen": 1, "death": 2}
        if key in fixed:
            self.fly_slot.set(str(fixed[key]))
            self.fly_action.set("2")
        self._fly_run_bg(
            f"飞预设:{key}",
            lambda s, k=key, p=page: fly_to_preset(s, k, page_id=p, log=self.log),
        )

    def _fly_parse_page_slot_action(self) -> tuple[int, int, int] | None:
        try:
            page = int(str(self.fly_page.get()).strip() or "255") & 0xFF
            slot = int(str(self.fly_slot.get()).strip() or "0")
            action = int(str(self.fly_action.get()).strip() or "2") & 0xFF
            return page, slot, action
        except Exception as e:
            messagebox.showerror("地图飞行", f"page/slot/action 无效: {e}")
            return None

    def _fly_send_packet(self) -> None:
        parsed = self._fly_parse_page_slot_action()
        if parsed is None:
            return
        page, slot, action = parsed
        self._fly_run_bg(
            "发包0x86",
            lambda s, a=action, p=page, sl=slot: send_transmit_packet(
                s, a, p, sl, None, log=self.log
            ),
        )

    def _fly_click_trans(self) -> None:
        parsed = self._fly_parse_page_slot_action()
        if parsed is None:
            return
        _page, slot, _a = parsed
        self._fly_run_bg(
            "点Btn_Trans",
            lambda s, sl=slot: click_ui_trans_slot(s, sl, open_if_needed=True, log=self.log),
        )

    def _fly_fill_host_pos(self) -> None:
        sess = self._fly_require_session()
        if sess is None:
            return
        try:
            r = read_scene_position(sess, log=self.log)
            x = y = z = None
            sp = getattr(r, "scene_pos", None)
            if isinstance(sp, (list, tuple)) and len(sp) >= 3:
                x, y, z = sp[0], sp[1], sp[2]
            if x is None:
                d = r.to_dict() if hasattr(r, "to_dict") else {}
                sp2 = d.get("scene_pos")
                if isinstance(sp2, (list, tuple)) and len(sp2) >= 3:
                    x, y, z = sp2[0], sp2[1], sp2[2]
                elif isinstance(d.get("target"), dict):
                    t = d["target"]
                    x, y, z = t.get("x"), t.get("y"), t.get("z")
            if x is None:
                try:
                    x = float(self.path_x.get())
                    y = float(self.path_y.get())
                    z = float(self.path_z.get())
                except Exception:
                    pass
            if x is None:
                self.var_fly.set("读坐标失败")
                self.log("飞行调研: 读坐标失败")
                return
            self.fly_x.set(f"{float(x):.3f}")
            self.fly_y.set(f"{float(y):.3f}")
            self.fly_z.set(f"{float(z):.3f}")
            sid = getattr(r, "scene_id", None)
            self.var_fly.set(
                f"已填当前坐标 scene={sid} ({float(x):.3f},{float(y):.3f},{float(z):.3f})"
            )
        except Exception as e:
            messagebox.showerror("地图飞行", str(e))

    def _fly_coord_research(self) -> None:
        try:
            x = float(str(self.fly_x.get()).strip() or self.path_x.get() or "0")
            y = float(str(self.fly_y.get()).strip() or self.path_y.get() or "0")
            z = float(str(self.fly_z.get()).strip() or self.path_z.get() or "0")
        except Exception as e:
            messagebox.showerror("地图飞行", f"坐标无效: {e}")
            return
        self._fly_run_bg(
            "未记录直飞结论",
            lambda s, xx=x, yy=y, zz=z: research_coord_fly(s, xx, yy, zz, log=self.log),
        )

    def _fly_record_elsewhere_research(self) -> None:
        self._fly_run_bg(
            "记录后异地飞结论",
            lambda s: research_record_then_fly_elsewhere(s, log=self.log),
        )

    def _fly_death_research(self) -> None:
        """死亡坐标是否可当任意飞入口. @author by ak"""
        self._fly_run_bg(
            "死亡点结论",
            lambda s: research_death_coord_entry(s, log=self.log),
        )

    def _fly_read_death(self) -> None:
        """读 fly_mgr 客户端死亡点缓存 (S2C 0x127). @author by ak"""
        self._fly_run_bg(
            "读死亡点缓存",
            lambda s: read_death_point(s, log=self.log),
        )

    def _fly_sign_probe(self) -> None:
        """Sign current pos into page/slot, then user flies from elsewhere. @author by ak"""
        parsed = self._fly_parse_page_slot_action()
        if parsed is None:
            return
        page, slot, _a = parsed
        # custom pages typically start at 1; 255/0 are default-tab
        if page in (0, 0xFF):
            page = 1
            self.fly_page.set("1")
        self.fly_action.set("1")
        self._fly_run_bg(
            "定位当前→槽",
            lambda s, p=page, sl=slot: probe_sign_then_ready_fly(
                s, sl, page_id=p, open_if_needed=True, log=self.log
            ),
        )

    def _fly_sign_slot(self) -> None:
        parsed = self._fly_parse_page_slot_action()
        if parsed is None:
            return
        page, slot, _a = parsed
        # sign usually on custom page; if page=255 fallback 1
        if page == 0xFF:
            page = 1
        self._fly_run_bg(
            "定位sign",
            lambda s, p=page, sl=slot: sign_current_to_slot(
                s, sl, page_id=p, open_if_needed=True, log=self.log
            ),
        )

    def _fly_slot_only(self) -> None:
        parsed = self._fly_parse_page_slot_action()
        if parsed is None:
            return
        page, slot, _a = parsed
        self.fly_action.set("2")
        self._fly_run_bg(
            "飞该槽(异地)",
            lambda s, p=page, sl=slot: fly_by_slot(
                s, sl, page_id=p, open_if_needed=True, log=self.log
            ),
        )

    def _path_from_host(self) -> None:

        """Fill target from break0 host position. @author by ak"""
        pos = None
        if self.current_result and self.current_result.pos:
            pos = self.current_result.pos
        if not pos:
            messagebox.showinfo("提示", "尚无宿主坐标，请先破0（等待实时坐标）")
            return
        self._path_set_fields(pos.x, pos.y, pos.z)
        self.var_path.set(f"目标=宿主 ({pos.x:.3f},{pos.y:.3f},{pos.z:.3f})")
        self.log(f"寻路目标已填宿主坐标 ({pos.x:.3f},{pos.y:.3f},{pos.z:.3f})")

    def _path_from_tree(self) -> None:
        """Fill target from selected tree row with coordinates. @author by ak"""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请在候选列表选中带坐标的 POS/MON/NPC/ITEM 行")
            return
        vals = self.tree.item(sel[0], "values")
        if not vals or len(vals) < 2:
            return
        text = str(vals[1])
        # patterns: "name @ (x,y,z)" or "(x, y, z)"
        import re

        m = re.search(
            r"\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)",
            text,
        )
        if not m:
            messagebox.showinfo("提示", f"选中行无坐标: {text[:60]}")
            return
        x, y, z = float(m.group(1)), float(m.group(2)), float(m.group(3))
        self._path_set_fields(x, y, z)
        self.var_path.set(f"目标=选中 {text[:50]}")
        self.log(f"寻路目标已填选中候选 ({x:.3f},{y:.3f},{z:.3f})")

    def _path_read_scene(self) -> None:
        """Call GetCurrentScenePosition and fill fields. @author by ak"""
        if not self.session or not self.session.pid:
            messagebox.showinfo("提示", "请先附加游戏进程（拖句柄破0）")
            return
        self.status.set("AUTOMOVE")
        self.log("调用 GetCurrentScenePosition…")

        def worker():
            try:
                r = read_scene_position(self.session, log=self.log)
                self.msg_q.put(("__AUTOMOVE__", r.to_dict(), "read_scene"))
            except Exception as e:
                self.msg_q.put(("__ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _path_start(self) -> None:
        """Confirm and remote-call HostMoveToScenePosition. @author by ak"""
        if not self.session or not self.session.pid:
            messagebox.showinfo("提示", "请先附加游戏进程（拖句柄破0）")
            return
        tgt = self._path_parse_target()
        if tgt is None:
            return
        if not messagebox.askokcancel(
            "确认开始寻路",
            "将通过 CreateRemoteThread 调用游戏内导出:\n"
            "plg::HostMoveToScenePosition(mode, x, y, z)\n\n"
            f"mode={tgt.mode}\n"
            f"xyz=({tgt.x:.3f}, {tgt.y:.3f}, {tgt.z:.3f})\n\n"
            "客户端可能有 PerfectProtector；仅在你授权的本机测试环境使用。\n"
            "这不是瞬移，角色应沿路径行走。",
        ):
            return
        self.status.set("AUTOMOVE")
        self.log(
            f"开始寻路 mode={tgt.mode} -> ({tgt.x:.3f},{tgt.y:.3f},{tgt.z:.3f})"
        )

        def worker():
            try:
                r = host_move_to(self.session, tgt, log=self.log)
                self.msg_q.put(("__AUTOMOVE__", r.to_dict(), "start"))
            except Exception as e:
                self.msg_q.put(("__ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _path_stop(self) -> None:
        """Best-effort stop via move-to-current. @author by ak"""
        if not self.session or not self.session.pid:
            messagebox.showinfo("提示", "请先附加游戏进程")
            return
        self.status.set("AUTOMOVE")
        self.log("停止寻路（best-effort: HostMove 到当前场景坐标）…")

        def worker():
            try:
                r = stop_automove_best_effort(self.session, log=self.log)
                self.msg_q.put(("__AUTOMOVE__", r.to_dict(), "stop"))
            except Exception as e:
                self.msg_q.put(("__ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _chest_parse_params(self) -> tuple[float, float] | None:
        """Parse local_range / cluster_radius UI fields. @author by ak"""
        try:
            lr = float((self.chest_local_range.get() or str(DEFAULT_LOCAL_RANGE)).strip())
            cr = float((self.chest_cluster_r.get() or str(DEFAULT_CLUSTER_RADIUS)).strip())
        except ValueError:
            messagebox.showinfo("提示", "交互范围 / 密集半径 必须是数字")
            return None
        if lr < 0 or cr <= 0:
            messagebox.showinfo("提示", "交互范围>=0，密集半径>0")
            return None
        return lr, cr

    def _chest_host_pos(self) -> tuple[float, float, float] | None:
        """Host pos from live break0 result if available. @author by ak"""
        if self.current_result and self.current_result.pos:
            p = self.current_result.pos
            return (float(p.x), float(p.y), float(p.z))
        return None

    def _chest_scan_densest(self) -> None:
        """
        Scan chest business plan (no HostMove).

        Fills path fields + tree; shows action auto_path_pickup | manual_walk.
        @author by ak
        """
        if not self.session or not self.session.pid:
            messagebox.showinfo("提示", "请先附加游戏进程（拖句柄破0）")
            return
        params = self._chest_parse_params()
        if params is None:
            return
        lr, cr = params
        host = self._chest_host_pos()
        self.status.set("CHEST")
        self.log(
            f"福利宝箱业务扫描 interact={lr:g} cluster_r={cr:g} host={host}"
        )

        def worker():
            try:
                plan = plan_chest_path(
                    self.session,
                    local_range=lr,
                    cluster_radius=cr,
                    host_pos=host,
                    log=self.log,
                )
                self.msg_q.put(("__CHEST_PLAN__", plan.to_dict(), "scan"))
            except Exception as e:
                self.msg_q.put(("__ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _chest_pathfind_densest(self) -> None:
        """
        Run chest business: skip if local; else HostMove densest center.

        @author by ak
        """
        if not self.session or not self.session.pid:
            messagebox.showinfo("提示", "请先附加游戏进程（拖句柄破0）")
            return
        params = self._chest_parse_params()
        if params is None:
            return
        lr, cr = params
        host = self._chest_host_pos()
        if not messagebox.askokcancel(
            "确认宝箱业务",
            f"业务规则（范围={lr:g}）：\n"
            f"1) d≤{lr:g}：附近已有箱 → 不做处理\n"
            f"2) d>{lr:g}：标密集区中心（adj 半径 {cr:g}）→ HostMove 自动寻路\n"
            f"3) 目标优先相邻箱子最多，同 adj 再取更近\n\n"
            "范围外才会调用 plg::HostMoveToScenePosition（非瞬移）。\n"
            "仅本机授权测试环境使用。",
        ):
            return
        self.status.set("CHEST")
        self.log(
            f"福利宝箱业务执行 local_range={lr:g} cluster_r={cr:g} host={host}"
        )

        def worker():
            try:
                plan, move_d = run_chest_business(
                    self.session,
                    local_range=lr,
                    cluster_radius=cr,
                    host_pos=host,
                    auto_move_local=False,
                    force_far_move=True,
                    log=self.log,
                )
                self.msg_q.put(
                    ("__CHEST_PLAN__", plan.to_dict(), "business", move_d)
                )
            except Exception as e:
                self.msg_q.put(("__ERROR__", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_chest_plan(
        self,
        d: dict,
        kind: str = "scan",
        move_d: dict | None = None,
    ) -> None:
        """Apply chest business plan to tree + path fields. @author by ak"""
        ok = bool(d.get("ok"))
        mode = str(d.get("mode") or "none")
        action = str(d.get("action") or "")
        note = d.get("note") or ""
        err = d.get("error")
        total = int(d.get("chest_total") or 0)
        local_n = int(d.get("local_count") or 0)
        cluster_n = int(d.get("cluster_count") or 0)
        can_auto = bool(d.get("can_auto_move"))
        can_pick = bool(d.get("can_quick_pickup"))
        tgt = d.get("target") or {}
        cluster = d.get("cluster") or []
        all_chests = d.get("all_chests") or []

        self.var_path.set(
            f"{'OK' if ok else 'FAIL'} chest mode={mode} action={action} "
            f"auto={int(can_auto)} pick={int(can_pick)} "
            f"total={total} local={local_n} adj={cluster_n} | {err or note}"
        )
        self.log(
            f"CHEST kind={kind} ok={ok} mode={mode} action={action} "
            f"can_auto_move={can_auto} can_quick_pickup={can_pick} "
            f"total={total} local={local_n} adj={cluster_n} err={err} note={note}"
        )

        # clear previous chest/entity-ish rows that we own for this view
        for i in self.tree.get_children():
            vals = self.tree.item(i, "values")
            if vals and str(vals[0]).startswith(
                ("CHEST", "ITEM", "MON", "NPC", "UNK", "PLY")
            ):
                self.tree.delete(i)

        # show cluster first (or all when none)
        show = cluster if cluster else all_chests
        for c in show:
            name = c.get("name") or "?"
            x, y, z = c.get("x"), c.get("y"), c.get("z")
            dist = c.get("dist")
            tid = c.get("tid")
            addr = int(c.get("address") or 0)
            if x is not None:
                val = f"{name} @ ({float(x):.1f},{float(y):.1f},{float(z):.1f})"
            else:
                val = f"{name} (nopos)"
            if dist is not None:
                val += f" d={float(dist):.1f}"
            if tid is not None:
                val += f" tid={tid}"
            # tag: within interact vs far
            if dist is not None and dist <= float(d.get("local_range") or DEFAULT_LOCAL_RANGE):
                tag = "CHEST"
            else:
                tag = "CHEST"
            score = 90 if mode == "local" else 80
            if dist is not None:
                score = max(1, int(100 - float(dist)))
            self.tree.insert(
                "",
                tk.END,
                values=(tag, val[:90], hex(addr) if addr else "", score),
            )

        if tgt.get("x") is not None:
            try:
                self._path_set_fields(
                    float(tgt["x"]),
                    float(tgt["y"]),
                    float(tgt["z"]),
                    mode=int(tgt["mode"]) if tgt.get("mode") is not None else None,
                )
                self.log(
                    f"宝箱目标已填 ({float(tgt['x']):.3f},"
                    f"{float(tgt['y']):.3f},{float(tgt['z']):.3f}) "
                    f"mode={tgt.get('mode')} hint={tgt.get('map_hint')} action={action}"
                )
            except Exception as e:
                self.log(f"宝箱目标填写失败: {e}")

        if kind in ("pathfind", "business"):
            if action == ACTION_SKIP_LOCAL or mode == "local":
                self.log("范围内已有宝箱：不做处理（不寻路）")
            elif move_d:
                self._apply_automove(move_d, kind="start")
                if action == ACTION_AUTO_PATH_CENTER or mode == "densest":
                    center = d.get("center")
                    if center and len(center) >= 3:
                        self.log(
                            f"范围外：已自动寻路到密集中心 "
                            f"({float(center[0]):.1f},{float(center[1]):.1f},{float(center[2]):.1f})"
                        )
                    else:
                        self.log("范围外：已自动寻路到密集中心")
                return
            elif ok and (
                action == ACTION_AUTO_PATH_CENTER or mode == "densest"
            ):
                self.log("范围外计划成功但未返回 HostMove 结果")

        self.status.set("ATTACHED" if self.session else "IDLE")

    def _apply_automove(self, d: dict, kind: str = "") -> None:
        """Show AutoMove result on UI. @author by ak"""
        ok = d.get("ok")
        method = d.get("method")
        ret = d.get("ret")
        err = d.get("error")
        note = d.get("note") or ""
        scene_id = d.get("scene_id")
        scene_pos = d.get("scene_pos")
        tgt = d.get("target") or {}
        self.var_path.set(
            f"{'OK' if ok else 'FAIL'} {method} ret={ret} "
            f"scene={scene_id} | {err or note}"
        )
        self.log(
            f"AUTOMOVE kind={kind} ok={ok} method={method} ret={ret} "
            f"va=0x{int(d.get('func_va') or 0):X} err={err} note={note}"
        )
        if kind == "read_scene" and ok and scene_pos and len(scene_pos) >= 3:
            self._path_set_fields(
                float(scene_pos[0]),
                float(scene_pos[1]),
                float(scene_pos[2]),
                mode=int(scene_id) if scene_id is not None else None,
            )
            self.log(
                f"场景坐标已填 mode/scene={scene_id} "
                f"({scene_pos[0]:.3f},{scene_pos[1]:.3f},{scene_pos[2]:.3f})"
            )
        if kind == "start" and ok:
            # optional short pos refresh to observe movement
            self.after(800, self._refresh_pos)
            self.after(2500, self._refresh_pos)
        if self.current_result and self.current_result.pos and tgt.get("x") is not None:
            try:
                hx = self.current_result.pos.x
                hz = self.current_result.pos.z
                dx = float(tgt["x"]) - hx
                dz = float(tgt["z"]) - hz
                dist = (dx * dx + dz * dz) ** 0.5
                self.log(f"宿主到目标水平距≈{dist:.1f} (破0坐标)")
            except Exception:
                pass
        self.status.set("ATTACHED" if self.session else "IDLE")

    def _drain_queue(self):
        try:
            while True:
                item = self.msg_q.get_nowait()
                try:
                    self._handle_queue_item(item)
                except Exception as e:
                    import traceback

                    print(f"[UI] drain item failed: {e!r}", flush=True)
                    traceback.print_exc()
                    try:
                        self._attach_log(f"UI队列处理异常: {e}", tag="UI")
                    except Exception:
                        pass
        except queue.Empty:
            pass
        try:
            self.after(100, self._drain_queue)
        except Exception:
            pass

    def _handle_queue_item(self, item) -> None:
        if isinstance(item, tuple) and item and item[0] == "__INJECT__":
            self._apply_inject(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__WLOG__":
            # ("__WLOG__", msg[, to_console])
            msg = str(item[1]) if len(item) > 1 else ""
            to_console = True if len(item) < 3 else bool(item[2])
            self._wlog(msg, to_console=to_console)
        elif isinstance(item, tuple) and item and item[0] == "__INJECT_PHASE__":
            # ("__INJECT_PHASE__", key, text)
            phase = str(item[1]) if len(item) > 1 else ""
            txt = str(item[2]) if len(item) > 2 else ""
            try:
                if not self._unlocked:
                    self.var_welcome_status.set(
                        f"注入中 · {txt or phase}"
                    )
            except Exception:
                pass
        elif isinstance(item, tuple) and item and item[0] == "__BREAK0_PROGRESS__":
            # Intermediate attach progress so UI leaves 附加中 early.
            d = item[1] if len(item) > 1 and isinstance(item[1], dict) else {}
            try:
                if d.get("exe_path"):
                    self._info_full["exe"] = str(d.get("exe_path"))
                    self.var_exe.set(self._short_path(str(d.get("exe_path")), 40))
                if d.get("map"):
                    self.var_map.set(str(d.get("map")))
                if d.get("pos"):
                    self.var_pos.set(str(d.get("pos")))
                phase = str(d.get("phase") or "")
                src = str(d.get("source") or "破0")
                if phase:
                    self._attach_log(f"{src}: 进度 {phase}", tag="ATTACH")
            except Exception as e:
                self._attach_log(f"break0 progress ui warn: {e}", tag="ATTACH")
        elif isinstance(item, tuple) and item and item[0] == "__BRIDGE_STATUS__":
            d = item[1] if len(item) > 1 and isinstance(item[1], dict) else {}
            bridge = d.get("bridge") if isinstance(d.get("bridge"), dict) else {}
            src = str(d.get("source") or "破0")
            try:
                if bridge.get("bridge_ok"):
                    mode = (
                        "复用已有桥接"
                        if bridge.get("reused") and not bridge.get("did_inject")
                        else (
                            "新注入桥接"
                            if bridge.get("did_inject")
                            else "桥接"
                        )
                    )
                    self._attach_log(f"{src}: {mode} 完成", tag="INJECT")
                    self._bridge_ready = True
                else:
                    note = str(bridge.get("note") or "BRIDGE_FAIL")
                    self._attach_log(
                        f"{src}: 桥接未完成 [{note}]（破0仍可用）",
                        tag="INJECT",
                    )
            except Exception as e:
                self._attach_log(f"bridge status ui warn: {e}", tag="INJECT")
        elif isinstance(item, tuple) and item and item[0] == "__RESULT__":
            # ("__RESULT__", session, result[, meta])
            session = item[1] if len(item) > 1 else None
            result = item[2] if len(item) > 2 else None
            meta = item[3] if len(item) > 3 else None
            if session is not None and result is not None:
                self._apply_result(session, result, meta if isinstance(meta, dict) else None)
        elif isinstance(item, tuple) and item and item[0] == "__POS_LIVE__":
            d = item[1] if len(item) > 1 else {}
            self._apply_pos_live(d if isinstance(d, dict) else {})
            if self.status.get() == "REFRESH":
                self.status.set("ATTACHED" if self.session else "IDLE")
        elif isinstance(item, tuple) and item and item[0] == "__POS_LIVE_ERR__":
            self._pos_live_busy = False
            self.var_scene.set("err")
            if self.pos_live.get() and self.session:
                self._pos_live_job = self.after(
                    self._pos_live_interval_ms, self._pos_live_tick
                )
        elif isinstance(item, tuple) and item and item[0] == "__STATUS__":
            self.status.set(str(item[1]) if len(item) > 1 else "IDLE")
        elif isinstance(item, tuple) and item and item[0] == "__ENTITIES__":
            # (__ENTITIES__, ents[, kind[, mode_label]])
            ents = item[1]
            kind = item[2] if len(item) > 2 else None
            mode_label = item[3] if len(item) > 3 else None
            self._apply_entities(ents, kind=kind, mode_label=mode_label)
        elif isinstance(item, tuple) and item and item[0] == "__IX_SCAN__":
            self._apply_ix_scan(item[1] if len(item) > 1 else [])
        elif isinstance(item, tuple) and item and item[0] == "__IX_NEAREST__":
            self._apply_ix_nearest(item[1] if len(item) > 1 else None)
        elif isinstance(item, tuple) and item and item[0] in ("__IX_PICK__", "__IX_EXEC__"):
            res = item[1] if len(item) > 1 else {}
            tgt = item[2] if len(item) > 2 else None
            # __IX_PICK__ legacy pick dict; __IX_EXEC__ full action result
            if item[0] == "__IX_EXEC__":
                self._apply_ix_exec(
                    res if isinstance(res, dict) else {},
                    tgt if isinstance(tgt, dict) else None,
                )
            else:
                self._apply_ix_exec(
                    {
                        "ok": bool((res or {}).get("ok")),
                        "action": "pickitem",
                        "kind": (tgt or {}).get("kind") or "",
                        "pick": res if isinstance(res, dict) else {},
                        "error": (res or {}).get("error"),
                        "note": (res or {}).get("note") or "",
                    },
                    tgt if isinstance(tgt, dict) else None,
                )
        elif isinstance(item, tuple) and item and item[0] == "__AUTOMOVE__":
            d = item[1] if len(item) > 1 else {}
            kind = item[2] if len(item) > 2 else ""
            self._apply_automove(d if isinstance(d, dict) else {}, kind=str(kind))
        elif isinstance(item, tuple) and item and item[0] == "__CHEST_PLAN__":
            d = item[1] if len(item) > 1 else {}
            kind = item[2] if len(item) > 2 else "scan"
            move_d = item[3] if len(item) > 3 else None
            self._apply_chest_plan(
                d if isinstance(d, dict) else {},
                kind=str(kind),
                move_d=move_d if isinstance(move_d, dict) else None,
            )
        elif isinstance(item, tuple) and item and item[0] == "__PKT_ANALYZE__":
            data = item[1] if len(item) > 1 else {}
            rows = item[2] if len(item) > 2 else None
            self._apply_pkt_analyze(
                data if isinstance(data, dict) else {},
                rows if isinstance(rows, list) else None,
            )
        elif isinstance(item, tuple) and item and item[0] == "__PKT_DONE__":
            self._apply_pkt_result(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__PKT_PHASE__":
            # (__PKT_PHASE__, phase, action, pid, baseline)
            phase = item[1] if len(item) > 1 else "idle"
            action = item[2] if len(item) > 2 else ""
            pid = item[3] if len(item) > 3 else None
            baseline = float(item[4]) if len(item) > 4 else 0.0
            try:
                self._set_pkt_phase_ui(str(phase), str(action), pid, baseline)
            except Exception as e:
                self.log(f"pkt phase ui warn: {e}")
        elif isinstance(item, tuple) and item and item[0] == "__PKT_ERROR__":
            self._pkt_busy = False
            self._pkt_stop.clear()
            self._pkt_abort.clear()
            self._pkt_phase = "idle"
            try:
                self.pkt_start_btn.configure(state=tk.NORMAL, bg="#1f6feb")
                self.pkt_stop_btn.configure(
                    state=tk.DISABLED, bg="#6e7681", text="■ 结束"
                )
                self.var_pkt_phase.set("阶段: 空闲")
            except Exception:
                pass
            if hasattr(self, "status"):
                self.status.set("ERROR")
            self.var_pkt.set(f"失败: {item[1]}")
            self.log(f"封包捕获错误: {item[1]}")
        elif isinstance(item, tuple) and item and item[0] == "__TASK_ACCEPTED__":
            self._task_apply_accepted(item[1] if len(item) > 1 else [])
        elif isinstance(item, tuple) and item and item[0] == "__PKTI_STARTED__":
            # (__PKTI_STARTED__, pid, InterceptState)
            self._pkti_apply_started(item[1] if len(item) > 1 else 0,
                                     item[2] if len(item) > 2 else None)
        elif isinstance(item, tuple) and item and item[0] == "__PKTI_STOPPED__":
            # (__PKTI_STOPPED__, records)
            self._pkti_apply_stopped(item[1] if len(item) > 1 else [])
        elif isinstance(item, tuple) and item and item[0] == "__PKTI_RESEND__":
            # (__PKTI_RESEND__, ret, length, head)
            self._pkti_apply_resend(int(item[1]) if len(item) > 1 else 0,
                                    item[2] if len(item) > 2 else 0,
                                    str(item[3]) if len(item) > 3 else "")
        elif isinstance(item, tuple) and item and item[0] == "__PKTI_REPLAY_DONE__":
            sent = int(item[1]) if len(item) > 1 else 0
            total = int(item[2]) if len(item) > 2 else 0
            stopped = bool(item[3]) if len(item) > 3 else False
            state = "已停止" if stopped else "完成"
            self.var_pkti_status.set(f"回放{state} {sent}/{total}")
            self.log(f"动作回放{state}: {sent}/{total}")
        elif isinstance(item, tuple) and item and item[0] == "__PKTI_ERROR__":
            self._pkti_apply_error(str(item[1]) if len(item) > 1 else "unknown")
        elif isinstance(item, tuple) and item and item[0] == "__TASK_AVAILABLE__":
            self._task_apply_available(item[1] if len(item) > 1 else [])
        elif isinstance(item, tuple) and item and item[0] == "__TASK_ALL__":
            self._task_apply_all(item[1] if len(item) > 1 else [])
        elif isinstance(item, tuple) and item and item[0] == "__TASK_CLUES__":
            self._task_apply_clues(item[1] if len(item) > 1 else [])
        elif isinstance(item, tuple) and item and item[0] == "__TASK_PATH__":
            self._task_apply_path(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__TASK_NPCS__":
            self._task_apply_npcs(item[1] if len(item) > 1 else [])
        elif isinstance(item, tuple) and item and item[0] == "__TASK_OP__":
            self._task_apply_op(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__LAB_LIVE__":
            if hasattr(self, "var_lab_live"):
                self.var_lab_live.set(f"live: {item[1]}")
        elif isinstance(item, tuple) and item and item[0] == "__LAB_POLL_DONE__":
            self._lab_poll_busy = False
        elif isinstance(item, tuple) and item and item[0] == "__LAB_DAMAGE_STAT__":
            self._lab_apply_damage_item_stat(
                item[1] if len(item) > 1 and isinstance(item[1], dict) else {}
            )
        elif isinstance(item, tuple) and item and item[0] == "__LAB_DAMAGE_STAT_DONE__":
            self._lab_finish_damage_item_stat(
                item[1] if len(item) > 1 and isinstance(item[1], dict) else {}
            )
        elif isinstance(item, tuple) and item and item[0] == "__LAB_DUMMY_DAMAGE__":
            self._lab_apply_dummy_damage_stat(
                item[1] if len(item) > 1 and isinstance(item[1], dict) else {}
            )
        elif isinstance(item, tuple) and item and item[0] == "__LAB_DUMMY_DAMAGE_DONE__":
            self._lab_finish_dummy_damage_stat(
                item[1] if len(item) > 1 and isinstance(item[1], dict) else {}
            )
        elif isinstance(item, tuple) and item and item[0] == "__LAB_BURST__":
            self._lab_apply_burst(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__LAB_CAST_BURST__":
            self._lab_apply_cast_burst(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__LAB_CAST_TL__":
            self._lab_apply_cast_timeline(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__LAB_CAST_CLEAR__":
            self._lab_apply_cast_clear(
                item[1] if len(item) > 1 else {}, tag="等active再清"
            )
        elif isinstance(item, tuple) and item and item[0] == "__LAB_SKILL_ACTION_TRACE__":
            self._lab_apply_skill_action_trace(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__LAB_YOUFENG_CHAIN_STATUS__":
            self._lab_apply_youfeng_chain_status(
                item[1] if len(item) > 1 else ""
            )
        elif isinstance(item, tuple) and item and item[0] == "__LAB_YOUFENG_INTERNAL_STATUS__":
            self._lab_apply_youfeng_internal_status(
                item[1] if len(item) > 1 else ""
            )
        elif isinstance(item, tuple) and item and item[0] == "__LAB_YOUFENG_GATE_STATUS__":
            self._lab_apply_youfeng_gate_status(
                item[1] if len(item) > 1 else ""
            )
        elif isinstance(item, tuple) and item and item[0] == "__LAB_YOUFENG_ULTIMATE_STATUS__":
            self._lab_apply_youfeng_ultimate_status(
                item[1] if len(item) > 1 else ""
            )
        elif isinstance(item, tuple) and item and item[0] == "__LAB_YOUFENG_ULTIMATE_SPAM_STATUS__":
            self._lab_apply_youfeng_ultimate_spam_status(
                item[1] if len(item) > 1 else ""
            )
        elif isinstance(item, tuple) and item and item[0] == "__LAB_RECOVERY__":
            self._lab_apply_recovery_trial(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__LAB_RECOVERY_MATRIX__":
            self._lab_apply_recovery_matrix(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__LAB_SUPPRESS_STATUS__":
            self._lab_apply_suppress_status(item[1] if len(item) > 1 else "")
        elif isinstance(item, tuple) and item and item[0] == "__LAB_RECOVERY_PROMPT__":
            self._lab_recovery_set_prompt(item[1] if len(item) > 1 else "")
        elif isinstance(item, tuple) and item and item[0] == "__LAB_HAND_PROBE_DONE__":
            self._lab_apply_hand_probe_done(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__LAB_SUPPRESS_DONE__":
            self._lab_apply_suppress_done(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__LAB_RECOVERY_DONE__":
            self._lab_recovery_busy = False
            try:
                if str(self.status.get() or "").startswith("LAB_RECOVERY"):
                    self.status.set("ATTACHED" if self.session else "IDLE")
            except Exception:
                pass
        elif isinstance(item, tuple) and item and item[0] == "__LAB_RET_COUNTDOWN__":
            txt = item[1] if len(item) > 1 else ""
            if hasattr(self, "var_lab_ret_live") and txt:
                self.var_lab_ret_live.set(str(txt))
        elif isinstance(item, tuple) and item and item[0] == "__LAB_RET_AUTO__":
            self._lab_apply_ret_auto(item[1] if len(item) > 1 else None)
        elif isinstance(item, tuple) and item and item[0] == "__LAB_RET_DIAG__":
            self._lab_apply_ret_diag(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__LAB_RET_CTRL__":
            self._lab_apply_ret_ctrl(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__LAB_RET_POLL__":
            self._lab_apply_ret_poll(item[1] if len(item) > 1 else {})
        elif isinstance(item, tuple) and item and item[0] == "__FLY__":
            try:
                title = item[1] if len(item) > 1 else "fly"
                payload = item[2] if len(item) > 2 else {}
                self._apply_fly_result(str(title), payload)
            except Exception as e:
                self.log(f"飞行调研 UI 更新失败: {e}")
        elif isinstance(item, tuple) and item and item[0] == "__ERROR__":
            self._break0_busy = False
            self._inject_busy = False
            if hasattr(self, "status"):
                self.status.set("ERROR")
            err_msg = f"错误: {item[1]}"
            # Attach/inject failures always echo console for triage.
            self._attach_log(err_msg, tag="ATTACH")
            try:
                if hasattr(self, "var_pos"):
                    self.var_pos.set("附加失败")
                if hasattr(self, "var_map"):
                    self.var_map.set("-")
                if hasattr(self, "var_scene"):
                    self.var_scene.set("-")
            except Exception:
                pass
            try:
                if hasattr(self, "var_task"):
                    self.var_task.set(f"错误: {item[1]}")
            except Exception:
                pass
            try:
                if not self._unlocked and hasattr(self, "var_welcome_status"):
                    self.var_welcome_status.set(f"失败: {item[1]}")
            except Exception:
                pass
        else:
            sitem = str(item)
            if sitem.startswith("[RECOVERY]"):
                try:
                    self._lab_recovery_live(sitem)
                except Exception as e:
                    self.log(f"recovery live ui warn: {e}")
                    if hasattr(self, "log_text"):
                        self.log_text.insert(tk.END, sitem + "\n")
                        self.log_text.see(tk.END)
            elif hasattr(self, "log_text"):
                self.log_text.insert(tk.END, sitem + "\n")
                self.log_text.see(tk.END)
    def _on_close(self):
        """
        Close workbench; safe from welcome page (before full UI built).

        @author by ak
        """
        if self._hotkey_job is not None:
            try:
                self.after_cancel(self._hotkey_job)
            except Exception:
                pass
            self._hotkey_job = None
        for attr in (
            "_lab_youfeng_chain_runner",
            "_lab_youfeng_internal_runner",
            "_lab_youfeng_gate_runner",
            "_lab_youfeng_ultimate_runner",
            "_lab_youfeng_ultimate_spam_runner",
        ):
            runner = getattr(self, attr, None)
            if runner is not None:
                try:
                    runner.stop()
                except Exception:
                    pass
                setattr(self, attr, None)
        self._lab_stop_poll()
        self._stop_pos_live()
        try:
            if self._damage_stat_stop is not None:
                self._damage_stat_stop.set()
        except Exception:
            pass
        try:
            if self._dummy_damage_stop is not None:
                self._dummy_damage_stop.set()
        except Exception:
            pass
        if self._pkt_busy:
            self._pkt_stop.set()
            try:
                self._pkt_abort.set()
            except Exception:
                pass
        if getattr(self, "_pkti_state", None) is not None:
            try:
                self._pkti_state.stop()
            except Exception:
                pass
            self._pkti_state = None
        try:
            self._detach()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        if self._standalone and self._standalone_root is not None:
            try:
                self._standalone_root.destroy()
            except Exception:
                pass

    def mainloop(self, n: int = 0):
        """Run event loop (standalone uses hidden Tk root). @author by ak"""
        if self._standalone and self._standalone_root is not None:
            return self._standalone_root.mainloop(n)
        return super().mainloop(n)


# Backward-compatible alias
App = WorkbenchApp


def main():
    """Legacy entry — prefer app.ui.app_shell.main for the business GUI. @author by ak"""
    app = WorkbenchApp()
    app.mainloop()


if __name__ == "__main__":
    main()
