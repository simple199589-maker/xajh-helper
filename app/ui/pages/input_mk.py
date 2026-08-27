# -*- coding: utf-8 -*-
"""鼠标/键盘 feature page (formal). @author by ak"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from app.core.bg_input import (
    MODE_CYCLE,
    MODE_HOLD,
    BgKeyBindRunner,
    FgKeyBindRunner,
    MouseClickerConfig,
    MouseClickerRunner,
    ShiftHoldConfig,
    ShiftHoldRunner,
    ensure_key_hold_hooks,
    format_vk_label,
    format_vk_list,
    parse_key_bind_chords,
    parse_key_bind_list,
    format_key_bind_chords,
    parse_vk_label,
    key_hold_probe,
    probe_key_paths,
)
from app.core.xajh_bridge import UI_CLICK_LEFT, UI_CLICK_RIGHT
from app.ui.pages.base import FeaturePage
from app.core.user_event_log import CAT_OP
from app.ui.theme import make_scrollable_body, section

__all__ = ["InputMkPage"]


class InputMkPage(FeaturePage):
    """
    Formal mouse/keyboard tools: Shift free-aim reticle, background keys, clicker.

    @author by ak
    """

    title = "鼠标/键盘"
    key = "input_mk"

    _SHIFT_MODE_UI = {
        "一直按下": MODE_HOLD,
        "单次循环": MODE_CYCLE,
    }
    _SHIFT_MODE_REV = {v: k for k, v in _SHIFT_MODE_UI.items()}
    _KEY_MODE_UI = {
        "连发(轮流)": BgKeyBindRunner.MODE_TAP,
        "连发(同时)": BgKeyBindRunner.MODE_TAP_ALL,
        # legacy load-only labels (not shown in combobox)
        "连发": BgKeyBindRunner.MODE_TAP,
        "按住": BgKeyBindRunner.MODE_TAP,  # hold removed from product UI
        "一直按住": BgKeyBindRunner.MODE_TAP,
    }
    _KEY_MODE_REV = {
        BgKeyBindRunner.MODE_TAP: "连发(轮流)",
        BgKeyBindRunner.MODE_TAP_ALL: "连发(同时)",
        # legacy hold settings fall back to 连发(轮流)
        BgKeyBindRunner.MODE_HOLD: "连发(轮流)",
    }
    _KEY_PRESETS = (
        ("1", "1"),
        ("123", "1,2,3"),
        ("1-5", "1,2,3,4,5"),
        ("Q", "Q"),
        ("Space", "Space"),
        ("Shift", "LShift"),
        ("Alt+R", "Alt+R"),
        ("F1", "F1"),
    )
    _FG_SLOT_N = 4  # at least 3 candidates; 4th optional
    _FG_MODE_UI = {
        "按下": FgKeyBindRunner.MODE_ONCE,
        "一直": FgKeyBindRunner.MODE_HOLD,
        "连发": FgKeyBindRunner.MODE_TAP,
    }
    _FG_MODE_REV = {v: k for k, v in _FG_MODE_UI.items()}

    def _build(self) -> None:
        self._shift_runner: ShiftHoldRunner | None = None
        self._click_l_runner: MouseClickerRunner | None = None
        self._click_r_runner: MouseClickerRunner | None = None
        self._key_runner: BgKeyBindRunner | None = None
        self._fg_runner: FgKeyBindRunner | None = None
        self._fg_capture_stop: threading.Event | None = None
        self._ui_q: queue.Queue = queue.Queue()
        self._hooks_armed = False

        outer, body = make_scrollable_body(self)
        outer.pack(fill=tk.BOTH, expand=True)


        # ---- Shift free-aim reticle (product KEY_HOLD) ----
        box_shift = section(body, "Shift 准星（自由瞄准）")
        box_shift.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            box_shift,
            text=(
                "后台按住 Shift 出 free-aim 准星（KEY_HOLD 进程内 Hook，不 SoftSend、不抢焦点）。\n"
                "用法：角色技能可自由瞄准时点「开始准星」；用完点「停止」。单开更稳。"
            ),
            style="Panel.Muted.TLabel",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 4))
        self.var_skill_id = tk.StringVar(
            value=str(int(self.settings.get("bg_skill_id") or 0))
        )
        self.var_skill_slot = tk.StringVar(
            value=str(int(self.settings.get("bg_skill_slot") or 0))
        )
        row_sm = ttk.Frame(box_shift, style="Panel.TFrame")
        row_sm.pack(fill=tk.X, pady=1)
        ttk.Label(row_sm, text="模式", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        saved_mode = str(self.settings.get("bg_shift_mode") or MODE_HOLD)
        self.var_shift_mode = tk.StringVar(
            value=self._SHIFT_MODE_REV.get(saved_mode, "一直按下")
        )
        ttk.Combobox(
            row_sm,
            textvariable=self.var_shift_mode,
            values=("一直按下", "单次循环"),
            width=10,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(row_sm, text="按下ms", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        self.var_shift_hold_ms = tk.StringVar(
            value=str(int(self.settings.get("bg_shift_hold_ms") or 800))
        )
        ttk.Entry(row_sm, textvariable=self.var_shift_hold_ms, width=6).pack(
            side=tk.LEFT, padx=(4, 8)
        )
        ttk.Label(row_sm, text="松开ms", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        self.var_shift_release_ms = tk.StringVar(
            value=str(int(self.settings.get("bg_shift_release_ms") or 200))
        )
        ttk.Entry(row_sm, textvariable=self.var_shift_release_ms, width=6).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        row_sb = ttk.Frame(box_shift, style="Panel.TFrame")
        row_sb.pack(fill=tk.X, pady=(4, 0))
        self.btn_shift_start = ttk.Button(
            row_sb,
            text="开始准星",
            style="Accent.TButton",
            width=10,
            command=self._on_shift_start,
        )
        self.btn_shift_start.pack(side=tk.LEFT)
        self.btn_shift_stop = ttk.Button(
            row_sb,
            text="停止",
            width=8,
            command=self._on_shift_stop,
            state=tk.DISABLED,
        )
        self.btn_shift_stop.pack(side=tk.LEFT, padx=(6, 0))
        self.btn_shift_probe = ttk.Button(
            row_sb, text="测按键", width=8, command=self._on_shift_probe
        )
        self.btn_shift_probe.pack(side=tk.LEFT, padx=(6, 0))
        self.var_shift_status = tk.StringVar(value="待命")
        ttk.Label(
            row_sb, textvariable=self.var_shift_status, style="Panel.Mono.TLabel"
        ).pack(side=tk.LEFT, padx=(10, 0))

        # ---- background keys ----
        box_key = section(body, "后台按键（解法2 · 进程内 Hook）")
        box_key.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            box_key,
            text="支持多键与组合键：逗号分隔多个绑定；"
            "组合用 + 连接（如 Alt+R、Ctrl+Shift+1）。"
            "「连发(轮流)」按绑定轮流；「连发(同时)」每轮全部绑定一起按。不抢焦点。",
            style="Panel.Muted.TLabel",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 4))

        row_k1 = ttk.Frame(box_key, style="Panel.TFrame")
        row_k1.pack(fill=tk.X, pady=1)
        ttk.Label(row_k1, text="按键", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        self.var_bg_keys = tk.StringVar(
            value=str(self.settings.get("bg_key_bind") or "1")
        )
        self.ent_bg_keys = ttk.Entry(row_k1, textvariable=self.var_bg_keys)
        self.ent_bg_keys.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8))
        ttk.Label(row_k1, text="模式", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        saved_km = str(self.settings.get("bg_key_mode") or BgKeyBindRunner.MODE_TAP)
        # product UI no longer exposes always-hold; migrate old setting
        if saved_km in ("hold", "press", "always", "hold_all"):
            saved_km = BgKeyBindRunner.MODE_TAP
        self.var_bg_key_mode = tk.StringVar(
            value=self._KEY_MODE_REV.get(saved_km, "连发(轮流)")
        )
        self.cmb_bg_key_mode = ttk.Combobox(
            row_k1,
            textvariable=self.var_bg_key_mode,
            values=("连发(轮流)", "连发(同时)"),
            width=10,
            state="readonly",
        )
        self.cmb_bg_key_mode.pack(side=tk.LEFT, padx=(4, 0))
        self.cmb_bg_key_mode.bind(
            "<<ComboboxSelected>>", lambda _e: self._on_bg_key_mode_change()
        )

        row_preset = ttk.Frame(box_key, style="Panel.TFrame")
        row_preset.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row_preset, text="预设", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        self._preset_btns: list[ttk.Button] = []
        for lab, val in self._KEY_PRESETS:
            b = ttk.Button(
                row_preset,
                text=lab,
                width=max(4, len(lab) + 1),
                command=lambda v=val: self._on_key_preset(v, replace=True),
            )
            b.pack(side=tk.LEFT, padx=(0, 4))
            self._preset_btns.append(b)
        self.btn_key_append = ttk.Button(
            row_preset,
            text="+追加",
            width=6,
            command=self._on_key_append_dialog,
        )
        self.btn_key_append.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_key_clear = ttk.Button(
            row_preset,
            text="清空",
            width=5,
            command=lambda: self.var_bg_keys.set(""),
        )
        self.btn_key_clear.pack(side=tk.LEFT, padx=(4, 0))

        row_k2 = ttk.Frame(box_key, style="Panel.TFrame")
        row_k2.pack(fill=tk.X, pady=(4, 0))
        self.lbl_bg_interval = ttk.Label(
            row_k2, text="间隔ms", style="Panel.Muted.TLabel", width=8
        )
        self.lbl_bg_interval.pack(side=tk.LEFT)
        self.var_bg_key_interval = tk.StringVar(
            value=str(int(self.settings.get("bg_key_interval_ms") or 200))
        )
        self.ent_bg_interval = ttk.Entry(
            row_k2, textvariable=self.var_bg_key_interval, width=6
        )
        self.ent_bg_interval.pack(side=tk.LEFT, padx=(4, 8))
        self.lbl_bg_hold = ttk.Label(
            row_k2, text="按下ms", style="Panel.Muted.TLabel"
        )
        self.lbl_bg_hold.pack(side=tk.LEFT)
        self.var_bg_key_hold = tk.StringVar(
            value=str(int(self.settings.get("bg_key_hold_ms") or 40))
        )
        self.ent_bg_hold = ttk.Entry(row_k2, textvariable=self.var_bg_key_hold, width=6)
        self.ent_bg_hold.pack(side=tk.LEFT, padx=(4, 8))
        self.lbl_bg_refresh = ttk.Label(
            row_k2, text="续按ms", style="Panel.Muted.TLabel"
        )
        self.var_bg_key_refresh = tk.StringVar(
            value=str(int(self.settings.get("bg_key_refresh_ms") or 200))
        )
        self.ent_bg_refresh = ttk.Entry(
            row_k2, textvariable=self.var_bg_key_refresh, width=6
        )
        # SoftSend hidden from product UI; always off for formal path.
        self.var_bg_key_soft = tk.BooleanVar(value=False)

        row_k2b = ttk.Frame(box_key, style="Panel.TFrame")
        row_k2b.pack(fill=tk.X, pady=(2, 0))
        self.var_bg_key_preview = tk.StringVar(value="")
        ttk.Label(
            row_k2b,
            textvariable=self.var_bg_key_preview,
            style="Panel.Mono.TLabel",
            wraplength=520,
        ).pack(side=tk.LEFT)
        try:
            self.var_bg_keys.trace_add("write", lambda *_a: self._refresh_key_preview())
            self.var_bg_key_mode.trace_add(
                "write", lambda *_a: self._refresh_key_preview()
            )
        except Exception:
            pass

        row_k3 = ttk.Frame(box_key, style="Panel.TFrame")
        row_k3.pack(fill=tk.X, pady=(4, 0))
        self.btn_key_start = ttk.Button(
            row_k3,
            text="开始",
            style="Accent.TButton",
            width=8,
            command=self._on_key_start,
        )
        self.btn_key_start.pack(side=tk.LEFT)
        self.btn_key_stop = ttk.Button(
            row_k3,
            text="停止",
            width=8,
            command=self._on_key_stop,
            state=tk.DISABLED,
        )
        self.btn_key_stop.pack(side=tk.LEFT, padx=(6, 0))
        self.var_key_status = tk.StringVar(value="待命")
        ttk.Label(
            row_k3, textvariable=self.var_key_status, style="Panel.Mono.TLabel"
        ).pack(side=tk.LEFT, padx=(10, 0))
        self._on_bg_key_mode_change()
        self._refresh_key_preview()


        # ---- mouse clicker ----
        box_clk = section(body, "鼠标连点")
        box_clk.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            box_clk,
            text="后台连点（进程内鼠标键状态 + 坐标消息，可最小化）。「选点」后坐标记在内部，不再手填 X/Y。",
            style="Panel.Muted.TLabel",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 4))
        # hidden vars for coords (compat)
        self.var_click_cx = tk.StringVar(
            value=str(int(self.settings.get("bg_click_cx") or 0))
        )
        self.var_click_cy = tk.StringVar(
            value=str(int(self.settings.get("bg_click_cy") or 0))
        )
        row_clk = ttk.Frame(box_clk, style="Panel.TFrame")
        row_clk.pack(fill=tk.X, pady=1)
        ttk.Label(row_clk, text="周期ms", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        self.var_click_interval = tk.StringVar(
            value=str(int(self.settings.get("bg_click_interval_ms") or 100))
        )
        ttk.Entry(row_clk, textvariable=self.var_click_interval, width=6).pack(
            side=tk.LEFT, padx=(4, 6)
        )
        ttk.Label(row_clk, text="按住ms", style="Panel.Muted.TLabel").pack(side=tk.LEFT)
        self.var_click_hold = tk.StringVar(
            value=str(int(self.settings.get("bg_click_hold_ms") or 40))
        )
        ttk.Entry(row_clk, textvariable=self.var_click_hold, width=5).pack(
            side=tk.LEFT, padx=(4, 8)
        )
        ttk.Button(row_clk, text="选点", width=5, command=self._on_pick_click_pos).pack(
            side=tk.LEFT
        )
        ttk.Button(
            row_clk, text="中心", width=5, command=self._on_click_pos_center
        ).pack(side=tk.LEFT, padx=(4, 0))

        # Separate row so 右连点 is not clipped on narrow windows.
        row_clk_btn = ttk.Frame(box_clk, style="Panel.TFrame")
        row_clk_btn.pack(fill=tk.X, pady=(4, 0))
        self.btn_click_l_start = ttk.Button(
            row_clk_btn,
            text="左连点",
            style="Accent.TButton",
            width=8,
            command=lambda: self._on_click_start("left"),
        )
        self.btn_click_l_start.pack(side=tk.LEFT)
        self.btn_click_l_stop = ttk.Button(
            row_clk_btn,
            text="停左",
            width=6,
            command=lambda: self._on_click_stop("left"),
            state=tk.DISABLED,
        )
        self.btn_click_l_stop.pack(side=tk.LEFT, padx=(4, 10))
        self.btn_click_r_start = ttk.Button(
            row_clk_btn,
            text="右连点",
            style="Accent.TButton",
            width=8,
            command=lambda: self._on_click_start("right"),
        )
        self.btn_click_r_start.pack(side=tk.LEFT)
        self.btn_click_r_stop = ttk.Button(
            row_clk_btn,
            text="停右",
            width=6,
            command=lambda: self._on_click_stop("right"),
            state=tk.DISABLED,
        )
        self.btn_click_r_stop.pack(side=tk.LEFT, padx=(4, 0))
        row_clk2 = ttk.Frame(box_clk, style="Panel.TFrame")
        row_clk2.pack(fill=tk.X, pady=(2, 0))
        self.var_click_pos_tip = tk.StringVar(value=self._click_pos_tip_text())
        self.var_click_l_status = tk.StringVar(value="左:待命")
        self.var_click_r_status = tk.StringVar(value="右:待命")
        ttk.Label(
            row_clk2, textvariable=self.var_click_pos_tip, style="Panel.Mono.TLabel"
        ).pack(side=tk.LEFT)
        ttk.Label(
            row_clk2, textvariable=self.var_click_l_status, style="Panel.Mono.TLabel"
        ).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Label(
            row_clk2, textvariable=self.var_click_r_status, style="Panel.Mono.TLabel"
        ).pack(side=tk.LEFT, padx=(8, 0))


        # ---- foreground keys (session-only, not persisted) ----
        box_fg = section(body, "前台按键（SendInput）")
        box_fg.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            box_fg,
            text="需游戏在前台。点击候选框后按键录入（含 Esc）；焦点离开即确认，未按键则清空。"
            "临时有效（重启后清空）。模式：按下 / 一直 / 连发。",
            style="Panel.Muted.TLabel",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 4))

        self.var_fg_slots: list[tk.StringVar] = []
        self._fg_slot_entries: list[ttk.Entry] = []
        self._fg_armed_idx: int | None = None  # which slot is waiting for key
        row_slots = ttk.Frame(box_fg, style="Panel.TFrame")
        row_slots.pack(fill=tk.X, pady=1)
        for i in range(self._FG_SLOT_N):
            cell = ttk.Frame(row_slots, style="Panel.TFrame")
            cell.pack(side=tk.LEFT, padx=(0 if i == 0 else 10, 0))
            ttk.Label(
                cell, text=f"候选{i+1}", style="Panel.Muted.TLabel"
            ).pack(anchor="w")
            var = tk.StringVar(value="")
            self.var_fg_slots.append(var)
            ent = ttk.Entry(cell, textvariable=var, width=9, justify="center")
            ent.pack(anchor="w", pady=(2, 0))
            self._fg_slot_entries.append(ent)
            ent.bind("<FocusIn>", lambda e, idx=i: self._on_fg_slot_focus_in(idx, e))
            ent.bind("<FocusOut>", lambda e, idx=i: self._on_fg_slot_focus_out(idx, e))
            ent.bind("<KeyPress>", lambda e, idx=i: self._on_fg_slot_key(idx, e))
            # prevent paste/type free text
            ent.bind("<<Paste>>", lambda e: "break")

        row_fm = ttk.Frame(box_fg, style="Panel.TFrame")
        row_fm.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row_fm, text="模式", style="Panel.Muted.TLabel", width=8).pack(
            side=tk.LEFT
        )
        self.var_fg_mode = tk.StringVar(value="连发")
        self.cmb_fg_mode = ttk.Combobox(
            row_fm,
            textvariable=self.var_fg_mode,
            values=("按下", "一直", "连发"),
            width=8,
            state="readonly",
        )
        self.cmb_fg_mode.pack(side=tk.LEFT, padx=(4, 8))
        self.cmb_fg_mode.bind(
            "<<ComboboxSelected>>", lambda _e: self._on_fg_mode_change()
        )
        self.lbl_fg_interval = ttk.Label(
            row_fm, text="间隔ms", style="Panel.Muted.TLabel"
        )
        self.lbl_fg_interval.pack(side=tk.LEFT)
        self.var_fg_interval = tk.StringVar(value="200")
        self.ent_fg_interval = ttk.Entry(
            row_fm, textvariable=self.var_fg_interval, width=6
        )
        self.ent_fg_interval.pack(side=tk.LEFT, padx=(4, 8))
        self.lbl_fg_hold = ttk.Label(
            row_fm, text="按下ms", style="Panel.Muted.TLabel"
        )
        self.lbl_fg_hold.pack(side=tk.LEFT)
        self.var_fg_hold = tk.StringVar(value="40")
        self.ent_fg_hold = ttk.Entry(row_fm, textvariable=self.var_fg_hold, width=6)
        self.ent_fg_hold.pack(side=tk.LEFT, padx=(4, 0))

        row_fb = ttk.Frame(box_fg, style="Panel.TFrame")
        row_fb.pack(fill=tk.X, pady=(4, 0))
        self.btn_fg_start = ttk.Button(
            row_fb,
            text="开始",
            style="Accent.TButton",
            width=8,
            command=self._on_fg_start,
        )
        self.btn_fg_start.pack(side=tk.LEFT)
        self.btn_fg_stop = ttk.Button(
            row_fb,
            text="停止",
            width=8,
            command=self._on_fg_stop,
            state=tk.DISABLED,
        )
        self.btn_fg_stop.pack(side=tk.LEFT, padx=(6, 0))
        self.var_fg_status = tk.StringVar(value="待命")
        ttk.Label(
            row_fb, textvariable=self.var_fg_status, style="Panel.Mono.TLabel"
        ).pack(side=tk.LEFT, padx=(10, 0))
        self.var_fg_preview = tk.StringVar(value="")
        ttk.Label(
            box_fg,
            textvariable=self.var_fg_preview,
            style="Panel.Mono.TLabel",
            wraplength=520,
        ).pack(anchor="w", pady=(2, 0))
        self._on_fg_mode_change()
        self._refresh_fg_preview()

        row_save = ttk.Frame(body, style="Panel.TFrame")
        row_save.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(
            row_save, text="保存本页设置", command=self._save_settings, width=12
        ).pack(side=tk.LEFT)
        self.var_status = tk.StringVar(value="待命")
        ttk.Label(
            row_save, textvariable=self.var_status, style="Panel.Mono.TLabel"
        ).pack(side=tk.LEFT, padx=(10, 0))

        self._schedule_ui_drain(200)

    # ---- lifecycle ----
    def on_bridge_ready(self) -> None:
        """Keep hooks lazy; explicit input actions install them when needed."""
        self._hooks_armed = False

    def _stop_all_runners(self) -> None:
        """Stop all page runners (hide-safe helper + quit path). @author by ak"""
        try:
            self._on_fg_capture_cancel()
        except Exception:
            pass
        try:
            self._on_key_stop()
        except Exception:
            pass
        try:
            self._on_fg_stop()
        except Exception:
            pass
        try:
            self._on_shift_stop()
        except Exception:
            pass
        try:
            self._on_click_stop("left")
        except Exception:
            pass
        try:
            self._on_click_stop("right")
        except Exception:
            pass

    def shutdown(self) -> None:
        """Stop runners on full app quit. @author by ak"""
        self._cancel_ui_drain()
        self._stop_all_runners()

    def destroy(self) -> None:
        self._stop_all_runners()
        super().destroy()

    # ---- save / parse ----
    def _save_settings(self) -> None:
        chords = parse_key_bind_chords(self.var_bg_keys.get() or "")
        keys = parse_key_bind_list(self.var_bg_keys.get() or "")
        # keep raw but normalize display when valid
        raw_keys = (self.var_bg_keys.get() or "").strip()
        if keys:
            pretty = format_key_bind_chords(chords) if chords else format_vk_list(keys)
            self.settings["bg_key_bind"] = pretty
            try:
                self.var_bg_keys.set(pretty)
            except Exception:
                pass
        else:
            self.settings["bg_key_bind"] = raw_keys or "1"
        mode_label = (self.var_bg_key_mode.get() or "连发(轮流)").strip()
        mode = self._KEY_MODE_UI.get(mode_label, BgKeyBindRunner.MODE_TAP)
        if mode == BgKeyBindRunner.MODE_HOLD:
            mode = BgKeyBindRunner.MODE_TAP
        self.settings["bg_key_mode"] = mode
        self.settings["bg_key_interval_ms"] = self._parse_int(
            self.var_bg_key_interval.get(), 200, lo=20, hi=10000
        )
        self.settings["bg_key_hold_ms"] = self._parse_int(
            self.var_bg_key_hold.get(), 40, lo=0, hi=2000
        )
        self.settings["bg_key_refresh_ms"] = self._parse_int(
            self.var_bg_key_refresh.get(), 200, lo=100, hi=1000
        )
        # Product path: never SoftSend (avoids FG pollution / multi-client bleed).
        self.settings["bg_key_softsend"] = False
        try:
            self.var_bg_key_soft.set(False)
        except Exception:
            pass
        # foreground keys are session-only (not persisted)
        try:
            self._refresh_fg_preview()
        except Exception:
            pass
        sm = (self.var_shift_mode.get() or "一直按下").strip()
        self.settings["bg_shift_mode"] = self._SHIFT_MODE_UI.get(sm, MODE_HOLD)
        self.settings["bg_shift_engine"] = "hold"
        self.settings["bg_shift_softsend"] = False
        self.settings["bg_shift_hold_ms"] = self._parse_int(
            self.var_shift_hold_ms.get(), 800, lo=50, hi=10000
        )
        self.settings["bg_shift_release_ms"] = self._parse_int(
            self.var_shift_release_ms.get(), 200, lo=20, hi=10000
        )
        self.settings["bg_skill_id"] = self._parse_int(
            self.var_skill_id.get(), 0, lo=0, hi=0x7FFFFFFF
        )
        self.settings["bg_skill_slot"] = self._parse_int(
            self.var_skill_slot.get(), 0, lo=0, hi=64
        )
        self.settings["bg_click_interval_ms"] = self._parse_int(
            self.var_click_interval.get(), 100, lo=20, hi=10000
        )
        self.settings["bg_click_hold_ms"] = self._parse_int(
            self.var_click_hold.get(), 40, lo=0, hi=500
        )
        self.settings["bg_click_cx"] = self._parse_int(
            self.var_click_cx.get(), 0, lo=0, hi=10000
        )
        self.settings["bg_click_cy"] = self._parse_int(
            self.var_click_cy.get(), 0, lo=0, hi=10000
        )
        try:
            self.var_click_pos_tip.set(self._click_pos_tip_text())
        except Exception:
            pass
        self.var_status.set("已保存")
        self.log("鼠标/键盘: 设置已保存")

    @staticmethod
    def _parse_int(raw, default: int, *, lo: int = 0, hi: int = 10_000) -> int:
        try:
            v = int(float(str(raw or "").strip() or str(default)))
        except Exception:
            v = int(default)
        return max(int(lo), min(int(hi), v))

    def _push(self, kind: str, payload=None) -> None:
        self._ui_q.put((kind, payload))

    def _drain_ui(self) -> None:
        try:
            while True:
                kind, payload = self._ui_q.get_nowait()
                if kind == "key_status":
                    self.var_key_status.set(str(payload or ""))
                elif kind == "fg_status":
                    self.var_fg_status.set(str(payload or ""))
                elif kind == "shift_status":
                    self.var_shift_status.set(str(payload or ""))
                elif kind == "click_l_status":
                    self.var_click_l_status.set(str(payload or ""))
                elif kind == "click_r_status":
                    self.var_click_r_status.set(str(payload or ""))
                elif kind == "hooks":
                    # silent product path: only log, don't clutter status
                    pass
                elif kind == "log":
                    self.log(str(payload or ""))
        except queue.Empty:
            pass
        try:
            self._schedule_ui_drain(200)
        except Exception:
            pass

    # ---- hooks auto ----
    def _auto_install_hooks(self) -> None:
        if self._hooks_armed:
            return
        if not bool(self.settings.get("bridge_ready", True)):
            return
        pid, hwnd = self._game_pid_hwnd()
        if not pid:
            return

        def work() -> None:
            ok, note = ensure_key_hold_hooks(pid, hwnd, log=lambda m: self._push("log", m))
            self._hooks_armed = bool(ok)
            if ok:
                self._push("log", f"鼠标/键盘: hooks 就绪 | {note}")
            else:
                self._push("log", f"鼠标/键盘: hooks 未就绪 | {note}")

        threading.Thread(target=work, name="auto-key-hooks", daemon=True).start()

    def _on_hooks_install(self) -> None:
        self._hooks_armed = False
        self._auto_install_hooks()

    def _game_pid_hwnd(self) -> tuple[int, int]:
        """Resolve mounted game pid/hwnd without forcing bridge-ready gate."""
        sess = self.selected_session()
        if sess is None:
            return 0, 0
        return int(getattr(sess, "pid", 0) or 0), int(getattr(sess, "hwnd", 0) or 0)

    def _publish_activity_key(self, key: str, running: bool) -> None:
        """Publish synthetic activity key for game title tag. @author by ak"""
        k = (key or "").strip()
        if not k:
            return
        try:
            master = self.winfo_toplevel()
            if hasattr(master, "notify_activity"):
                master.notify_activity(k, bool(running))
        except Exception:
            pass

    # ---- background keys helpers ----
    def _current_key_mode(self) -> str:
        lab = (self.var_bg_key_mode.get() or "连发(轮流)").strip()
        return self._KEY_MODE_UI.get(lab, BgKeyBindRunner.MODE_TAP)

    def _on_bg_key_mode_change(self) -> None:
        """Tap modes only: keep interval + press duration visible. @author by ak"""
        try:
            self.lbl_bg_refresh.pack_forget()
            self.ent_bg_refresh.pack_forget()
            self.lbl_bg_interval.configure(text="间隔ms")
            if not self.lbl_bg_interval.winfo_ismapped():
                self.lbl_bg_interval.pack(side=tk.LEFT)
            if not self.ent_bg_interval.winfo_ismapped():
                self.ent_bg_interval.pack(side=tk.LEFT, padx=(4, 8))
            if not self.lbl_bg_hold.winfo_ismapped():
                self.lbl_bg_hold.pack(side=tk.LEFT)
            if not self.ent_bg_hold.winfo_ismapped():
                self.ent_bg_hold.pack(side=tk.LEFT, padx=(4, 8))
        except Exception:
            pass
        self._refresh_key_preview()

    def _refresh_key_preview(self) -> None:
        try:
            chords = parse_key_bind_chords(self.var_bg_keys.get() or "")
            mode = self._current_key_mode()
            mode_cn = self._KEY_MODE_REV.get(mode, mode)
            if not chords:
                self.var_bg_key_preview.set("（无有效按键）")
                return
            lab = format_key_bind_chords(chords)
            tip = {
                BgKeyBindRunner.MODE_TAP: "按绑定轮流（组合键完整按下/抬起）",
                BgKeyBindRunner.MODE_TAP_ALL: "每轮全部绑定一起按",
            }.get(mode, "")
            self.var_bg_key_preview.set(f"[{lab}] ×{len(chords)}  | {mode_cn} — {tip}")
        except Exception:
            pass

    def _on_key_preset(self, raw: str, *, replace: bool = True) -> None:
        s = str(raw or "").strip()
        if not s:
            return
        if replace:
            self.var_bg_keys.set(s)
        else:
            cur = (self.var_bg_keys.get() or "").strip()
            self.var_bg_keys.set(f"{cur},{s}" if cur else s)
        self._on_key_normalize()

    def _on_key_append_dialog(self) -> None:
        """Append one more key token from a tiny prompt. @author by ak"""
        try:
            top = tk.Toplevel(self)
            top.title("追加按键")
            top.transient(self.winfo_toplevel())
            top.grab_set()
            frm = ttk.Frame(top, padding=10)
            frm.pack(fill=tk.BOTH, expand=True)
            ttk.Label(frm, text="输入键名（如 3 / Q / Space / F2）").pack(anchor="w")
            var = tk.StringVar(value="")
            ent = ttk.Entry(frm, textvariable=var, width=24)
            ent.pack(fill=tk.X, pady=(6, 8))
            ent.focus_set()

            def ok() -> None:
                tok = (var.get() or "").strip()
                top.destroy()
                if not tok:
                    return
                if not parse_vk_label(tok, 0):
                    self.var_key_status.set(f"无效键: {tok}")
                    return
                self._on_key_preset(tok, replace=False)

            def cancel() -> None:
                top.destroy()

            row = ttk.Frame(frm)
            row.pack(fill=tk.X)
            ttk.Button(row, text="追加", style="Accent.TButton", command=ok).pack(
                side=tk.LEFT
            )
            ttk.Button(row, text="取消", command=cancel).pack(side=tk.LEFT, padx=(6, 0))
            ent.bind("<Return>", lambda _e: ok())
            ent.bind("<Escape>", lambda _e: cancel())
        except Exception as e:
            self.var_key_status.set(f"追加失败: {e}")

    def _on_key_normalize(self) -> None:
        chords = parse_key_bind_chords(self.var_bg_keys.get() or "")
        if not chords:
            self.var_key_status.set("按键无效")
            self._refresh_key_preview()
            return
        pretty = format_key_bind_chords(chords)
        self.var_bg_keys.set(pretty)
        self.var_key_status.set(f"已解析 {len(keys)} 键")
        self._refresh_key_preview()

    # ---- background keys ----
    def _set_key_running(self, running: bool) -> None:
        try:
            self.btn_key_start.configure(state=tk.DISABLED if running else tk.NORMAL)
            self.btn_key_stop.configure(state=tk.NORMAL if running else tk.DISABLED)
        except Exception:
            pass
        self._set_key_editors_enabled(not running)
        self._publish_activity_key("bg_key", running)

    def _set_key_editors_enabled(self, enabled: bool) -> None:
        """Lock bind/mode/presets while runner is active to avoid mid-run edits. @author by ak"""
        st_entry = tk.NORMAL if enabled else tk.DISABLED
        st_combo = "readonly" if enabled else "disabled"
        st_btn = tk.NORMAL if enabled else tk.DISABLED
        for name in ("ent_bg_keys", "ent_bg_interval", "ent_bg_hold", "ent_bg_refresh"):
            w = getattr(self, name, None)
            if w is None:
                continue
            try:
                w.configure(state=st_entry)
            except Exception:
                pass
        for name in ("btn_key_append", "btn_key_clear"):
            w = getattr(self, name, None)
            if w is None:
                continue
            try:
                w.configure(state=st_btn)
            except Exception:
                pass
        try:
            self.cmb_bg_key_mode.configure(state=st_combo)
        except Exception:
            pass
        for b in getattr(self, "_preset_btns", []) or []:
            try:
                b.configure(state=st_btn)
            except Exception:
                pass

    def _on_key_start(self) -> None:
        self._save_settings()
        if self._key_runner and self._key_runner.is_running():
            self.var_key_status.set("已在运行")
            return
        sess = self._require_session()
        if sess is None:
            self.var_key_status.set("未挂载/注入中")
            return
        pid, hwnd = int(sess.pid or 0), int(sess.hwnd or 0)
        if not pid:
            self.var_key_status.set("未挂载")
            return
        chords = parse_key_bind_chords(self.settings.get("bg_key_bind") or "1")
        if not chords:
            self.var_key_status.set("无有效按键")
            return
        mode = str(self.settings.get("bg_key_mode") or BgKeyBindRunner.MODE_TAP)
        if mode in (
            BgKeyBindRunner.MODE_HOLD,
            "press",
            "always",
            "hold_all",
        ):
            mode = BgKeyBindRunner.MODE_TAP
            self.settings["bg_key_mode"] = mode
            try:
                self.var_bg_key_mode.set("连发(轮流)")
            except Exception:
                pass
        labels = format_key_bind_chords(chords)
        self._key_runner = BgKeyBindRunner(
            pid,
            hwnd=hwnd,
            chords=chords,
            mode=mode,
            interval_ms=int(self.settings.get("bg_key_interval_ms") or 200),
            hold_ms=int(self.settings.get("bg_key_hold_ms") or 40),
            refresh_ms=int(self.settings.get("bg_key_refresh_ms") or 300),
            allow_softsend=False,
            log=lambda m: self._push("log", m),
            on_status=lambda m: self._push("key_status", m),
        )
        if not self._key_runner.start():
            self.var_key_status.set("启动失败")
            self._key_runner = None
            try:
                self.user_log("操作：后台按键启动失败", category=CAT_OP, source="鼠标/键盘")
            except Exception:
                pass
            return
        self._set_key_running(True)
        mode_cn = self._KEY_MODE_REV.get(mode, mode)
        self.var_key_status.set(f"运行中 [{mode_cn}] {labels}")
        try:
            self.user_log(
                f"操作：后台按键已开始 · {mode_cn} · {labels}",
                category=CAT_OP,
                source="鼠标/键盘",
            )
        except Exception:
            pass
        self.after(400, self._watch_key)

    def _watch_key(self) -> None:
        r = self._key_runner
        if r is None:
            self._set_key_running(False)
            return
        if r.is_running():
            self.after(400, self._watch_key)
            return
        self._key_runner = None
        self._set_key_running(False)
        self.var_key_status.set("已停止")

    def _on_key_stop(self) -> None:
        r = self._key_runner
        if r is not None:
            try:
                r.stop()
            except Exception:
                pass
        self._key_runner = None
        self._set_key_running(False)
        self.var_key_status.set("已停止")
        try:
            self.user_log("操作：后台按键已停止", category=CAT_OP, source="鼠标/键盘")
        except Exception:
            pass


    # ---- foreground keys ----
    def _fg_keys_from_slots(self) -> list[int]:
        keys: list[int] = []
        seen: set[int] = set()
        for var in getattr(self, "var_fg_slots", []) or []:
            tok = (var.get() or "").strip()
            if not tok or tok in ("…", "...", "按键…", "等待"):
                continue
            vk = parse_vk_label(tok, 0)
            if not vk or vk in seen:
                continue
            seen.add(vk)
            keys.append(vk)
        return keys

    def _refresh_fg_preview(self) -> None:
        try:
            keys = self._fg_keys_from_slots()
            mode = self._FG_MODE_UI.get(
                (self.var_fg_mode.get() or "连发").strip(),
                FgKeyBindRunner.MODE_TAP,
            )
            mode_cn = self._FG_MODE_REV.get(mode, mode)
            if not keys:
                self.var_fg_preview.set("候选: （空）— 点框后按键录入")
                return
            tip = {
                FgKeyBindRunner.MODE_ONCE: "各键点一次后自动停",
                FgKeyBindRunner.MODE_HOLD: "全部按住直到点停止",
                FgKeyBindRunner.MODE_TAP: "按间隔循环连发",
            }.get(mode, "")
            self.var_fg_preview.set(
                f"候选: [{format_vk_list(keys)}] ×{len(keys)} | {mode_cn} — {tip}"
            )
        except Exception:
            pass

    def _on_fg_mode_change(self) -> None:
        mode = self._FG_MODE_UI.get(
            (self.var_fg_mode.get() or "连发").strip(), FgKeyBindRunner.MODE_TAP
        )
        try:
            if mode == FgKeyBindRunner.MODE_HOLD:
                self.lbl_fg_interval.pack_forget()
                self.ent_fg_interval.pack_forget()
                self.lbl_fg_hold.pack_forget()
                self.ent_fg_hold.pack_forget()
            elif mode == FgKeyBindRunner.MODE_ONCE:
                self.lbl_fg_interval.pack_forget()
                self.ent_fg_interval.pack_forget()
                self.lbl_fg_hold.pack(side=tk.LEFT)
                self.ent_fg_hold.pack(side=tk.LEFT, padx=(4, 0))
            else:
                self.lbl_fg_interval.pack(side=tk.LEFT)
                self.ent_fg_interval.pack(side=tk.LEFT, padx=(4, 8))
                self.lbl_fg_hold.pack(side=tk.LEFT)
                self.ent_fg_hold.pack(side=tk.LEFT, padx=(4, 0))
        except Exception:
            pass
        self._refresh_fg_preview()

    def _on_fg_slot_focus_in(self, idx: int, _event=None) -> None:
        if self._fg_runner and self._fg_runner.is_running():
            return
        self._fg_armed_idx = idx
        try:
            # waiting state: clear so focus-out without key becomes empty
            self.var_fg_slots[idx].set("")
            self.var_fg_status.set(f"候选{idx+1}: 等待按键…")
        except Exception:
            pass

    def _on_fg_slot_focus_out(self, idx: int, _event=None) -> None:
        # Focus left without a key => empty (already cleared on focus-in if no key set)
        if self._fg_armed_idx == idx:
            self._fg_armed_idx = None
        try:
            tok = (self.var_fg_slots[idx].get() or "").strip()
            if not tok or tok in ("等待", "按键…"):
                self.var_fg_slots[idx].set("")
            elif not parse_vk_label(tok, 0):
                self.var_fg_slots[idx].set("")
        except Exception:
            pass
        try:
            if not (self._fg_runner and self._fg_runner.is_running()):
                if (self.var_fg_status.get() or "").startswith("候选"):
                    self.var_fg_status.set("待命")
        except Exception:
            pass
        self._refresh_fg_preview()

    def _on_fg_slot_key(self, idx: int, event) -> str:
        """Capture one key while the candidate entry is focused. @author by ak"""
        if self._fg_runner and self._fg_runner.is_running():
            return "break"
        # Ignore pure modifiers alone if desired; still allow Shift as bind target.
        keysym = str(getattr(event, "keysym", "") or "")
        keycode = int(getattr(event, "keycode", 0) or 0)
        # Tab only moves focus (not a bind). Esc/Enter are valid recordable keys.
        if keysym == "Tab":
            return None  # allow normal focus traversal

        vk = 0
        # Prefer keysym labels for letters/digits/F-keys
        if keysym:
            # Map common Tk keysyms
            sym = keysym
            if sym.startswith("KP_"):
                sym = sym[3:]
            if len(sym) == 1:
                vk = parse_vk_label(sym, 0)
            else:
                alias = {
                    "space": "Space",
                    "Escape": "Esc",
                    "Return": "Enter",
                    "BackSpace": "Backspace",
                    "Shift_L": "LShift",
                    "Shift_R": "RShift",
                    "Control_L": "LCtrl",
                    "Control_R": "RCtrl",
                    "Alt_L": "LAlt",
                    "Alt_R": "RAlt",
                    "Prior": "PageUp",
                    "Next": "PageDown",
                    "Left": "Left",
                    "Right": "Right",
                    "Up": "Up",
                    "Down": "Down",
                }.get(sym, sym)
                vk = parse_vk_label(alias, 0)
        if not vk and keycode:
            # Windows: keycode often equals VK for A-Z 0-9
            if 0x08 <= keycode <= 0xFE:
                vk = keycode & 0xFF
        if not vk:
            self.var_fg_status.set(f"候选{idx+1}: 无法识别")
            return "break"

        lab = format_vk_label(vk)
        try:
            self.var_fg_slots[idx].set(lab)
        except Exception:
            pass
        self._fg_armed_idx = None
        self.var_fg_status.set(f"候选{idx+1}: {lab}")
        self._refresh_fg_preview()
        # move focus away so slot is "saved"
        try:
            self.focus_set()
        except Exception:
            pass
        return "break"

    def _on_fg_capture_cancel(self) -> None:
        self._fg_armed_idx = None
        self._fg_capture_stop = None

    def _set_fg_running(self, running: bool) -> None:
        try:
            self.btn_fg_start.configure(state=tk.DISABLED if running else tk.NORMAL)
            self.btn_fg_stop.configure(state=tk.NORMAL if running else tk.DISABLED)
        except Exception:
            pass
        self._set_fg_editors_enabled(not running)
        self._publish_activity_key("fg_key", running)

    def _set_fg_editors_enabled(self, enabled: bool) -> None:
        st_entry = tk.NORMAL if enabled else tk.DISABLED
        st_combo = "readonly" if enabled else "disabled"
        for name in ("ent_fg_interval", "ent_fg_hold"):
            w = getattr(self, name, None)
            if w is None:
                continue
            try:
                w.configure(state=st_entry)
            except Exception:
                pass
        try:
            self.cmb_fg_mode.configure(state=st_combo)
        except Exception:
            pass
        for ent in getattr(self, "_fg_slot_entries", []) or []:
            try:
                ent.configure(state=st_entry)
            except Exception:
                pass

    def _on_fg_start(self) -> None:
        if self._fg_runner and self._fg_runner.is_running():
            self.var_fg_status.set("已在运行")
            return
        self._on_fg_capture_cancel()
        keys = self._fg_keys_from_slots()
        if not keys:
            self.var_fg_status.set("请先录入按键")
            return
        mode_label = (self.var_fg_mode.get() or "连发").strip()
        mode = self._FG_MODE_UI.get(mode_label, FgKeyBindRunner.MODE_TAP)
        interval_ms = self._parse_int(self.var_fg_interval.get(), 200, lo=20, hi=10000)
        hold_ms = self._parse_int(self.var_fg_hold.get(), 40, lo=0, hi=2000)
        hwnd = 0
        try:
            sess = self.selected_session()
            if sess is not None:
                hwnd = int(getattr(sess, "hwnd", 0) or 0)
        except Exception:
            hwnd = 0
        labels = format_vk_list(keys)

        # Thread-safe: never pass self.log (Tk) into worker.
        self._fg_runner = FgKeyBindRunner(
            keys,
            hwnd=hwnd,
            mode=mode,
            interval_ms=interval_ms,
            hold_ms=hold_ms,
            log=lambda m: self._push("log", m),
            on_status=lambda m: self._push("fg_status", m),
        )
        self._set_fg_running(True)
        self.var_fg_status.set(f"启动中 [{mode_label}] {labels}")
        if not self._fg_runner.start():
            self.var_fg_status.set("启动失败")
            self._fg_runner = None
            self._set_fg_running(False)
            try:
                self.user_log("操作：前台按键启动失败", category=CAT_OP, source="鼠标/键盘")
            except Exception:
                pass
            return
        try:
            self.user_log(
                f"操作：前台按键已开始 · {mode_label} · {labels}",
                category=CAT_OP,
                source="鼠标/键盘",
            )
        except Exception:
            pass
        self.after(300, self._watch_fg)

    def _watch_fg(self) -> None:
        r = self._fg_runner
        if r is None:
            self._set_fg_running(False)
            return
        if r.is_running():
            self.after(300, self._watch_fg)
            return
        self._fg_runner = None
        self._set_fg_running(False)
        try:
            cur = self.var_fg_status.get() or ""
            if "已停止" not in cur and "完成" not in cur:
                self.var_fg_status.set("已停止")
        except Exception:
            pass

    def _on_fg_stop(self) -> None:
        r = self._fg_runner
        self._fg_runner = None
        if r is not None:
            try:
                r.stop()
            except Exception:
                pass
        self._set_fg_running(False)
        try:
            self.var_fg_status.set("已停止")
        except Exception:
            pass
        try:
            self.user_log("操作：前台按键已停止", category=CAT_OP, source="鼠标/键盘")
        except Exception:
            pass

    # ---- shift (ported) ----
    # ---- shift (ported) ----
    def _set_shift_running(self, running: bool) -> None:
        try:
            self.btn_shift_start.configure(state=tk.DISABLED if running else tk.NORMAL)
            self.btn_shift_stop.configure(state=tk.NORMAL if running else tk.DISABLED)
        except Exception:
            pass
        self._publish_activity_key("bg_shift", running)

    def _shift_cfg_from_ui(self) -> ShiftHoldConfig:
        self._save_settings()
        return ShiftHoldConfig(
            mode=str(self.settings.get("bg_shift_mode") or MODE_HOLD),
            hold_ms=int(self.settings.get("bg_shift_hold_ms") or 800),
            release_ms=int(self.settings.get("bg_shift_release_ms") or 200),
            refresh_ms=int(self.settings.get("bg_shift_refresh_ms") or 400),
            allow_softsend=False,
            engine="hold",
            skill_id=int(self.settings.get("bg_skill_id") or 0),
            bar_slot=int(self.settings.get("bg_skill_slot") or 0),
        )

    def _on_shift_probe(self) -> None:
        pid, hwnd = self._game_pid_hwnd()
        if not pid:
            self.var_shift_status.set("未挂载")
            return
        self.var_shift_status.set("测 KEY_HOLD Shift…")

        def work() -> None:
            # Product path probe: KEY_HOLD VK_SHIFT without SoftSend.
            lines = key_hold_probe(
                pid,
                hwnd=hwnd,
                vk=0x10,
                allow_softsend=False,
                hold_ms=400,
                log=lambda m: self._push("log", m),
            )
            last = lines[-1] if lines else "完成"
            self._push("shift_status", last[:80])
            for ln in lines:
                self._push("log", ln)

        threading.Thread(target=work, name="shift-probe", daemon=True).start()

    def _on_shift_start(self) -> None:
        if self._shift_runner and self._shift_runner.is_running():
            self.var_shift_status.set("已在运行")
            return
        sess = self._require_session()
        if sess is None:
            self.var_shift_status.set("未挂载/注入中")
            return
        pid, hwnd = int(sess.pid or 0), int(sess.hwnd or 0)
        if not pid:
            self.var_shift_status.set("未挂载")
            return
        cfg = self._shift_cfg_from_ui()
        self._shift_runner = ShiftHoldRunner(
            pid,
            hwnd=hwnd,
            cfg=cfg,
            log=lambda m: self._push("log", m),
            on_status=lambda m: self._push("shift_status", m),
        )
        if not self._shift_runner.start():
            self.var_shift_status.set("启动失败")
            self._shift_runner = None
            return
        self._set_shift_running(True)
        self.var_shift_status.set("运行中…")
        try:
            mode = str(self.settings.get("bg_shift_mode") or MODE_HOLD)
            self.user_log(
                f"Shift准星已开始（KEY_HOLD · {mode}）",
                category=CAT_OP,
                source="鼠标/键盘",
            )
        except Exception:
            pass
        self.after(400, self._watch_shift)

    def _watch_shift(self) -> None:
        r = self._shift_runner
        if r is None:
            self._set_shift_running(False)
            return
        if r.is_running():
            self.after(400, self._watch_shift)
            return
        self._shift_runner = None
        self._set_shift_running(False)
        self.var_shift_status.set("已停止")

    def _on_shift_stop(self) -> None:
        r = self._shift_runner
        if r is not None:
            try:
                r.stop()
            except Exception:
                pass
        self._shift_runner = None
        self._set_shift_running(False)
        self.var_shift_status.set("已停止")
        try:
            self.user_log(
                "Shift准星已停止",
                category=CAT_OP,
                source="鼠标/键盘",
            )
        except Exception:
            pass

    # ---- mouse clicker ----
    def _set_click_running(self, side: str, running: bool) -> None:
        is_right = side == "right"
        if is_right:
            btn_s, btn_t = self.btn_click_r_start, self.btn_click_r_stop
            act_key = "bg_click_r"
        else:
            btn_s, btn_t = self.btn_click_l_start, self.btn_click_l_stop
            act_key = "bg_click_l"
        try:
            btn_s.configure(state=tk.DISABLED if running else tk.NORMAL)
            btn_t.configure(state=tk.NORMAL if running else tk.DISABLED)
        except Exception:
            pass
        self._publish_activity_key(act_key, running)

    def _click_cfg_from_ui(self, button: int) -> MouseClickerConfig:
        self._save_settings()
        return MouseClickerConfig(
            button=int(button),
            interval_ms=int(self.settings.get("bg_click_interval_ms") or 100),
            hold_ms=int(self.settings.get("bg_click_hold_ms") or 40),
            cx=int(self.settings.get("bg_click_cx") or 0),
            cy=int(self.settings.get("bg_click_cy") or 0),
        )

    def _click_pos_tip_text(self) -> str:
        cx = int(self.settings.get("bg_click_cx") or 0)
        cy = int(self.settings.get("bg_click_cy") or 0)
        if cx <= 0 and cy <= 0:
            return "点位: 窗口中心"
        return f"点位: ({cx},{cy})"

    def _on_click_pos_center(self) -> None:
        self.var_click_cx.set("0")
        self.var_click_cy.set("0")
        self.settings["bg_click_cx"] = 0
        self.settings["bg_click_cy"] = 0
        self.var_click_pos_tip.set(self._click_pos_tip_text())

    def _on_pick_click_pos(self) -> None:
        """Pick client coords by moving mouse into game then LMB. @author by ak"""
        pid, hwnd = self._game_pid_hwnd()
        if not pid or not hwnd:
            self.var_click_pos_tip.set("点位: 未挂载")
            return
        self.var_click_pos_tip.set("点位: 移到目标后点左键…")
        tip = None
        try:
            tip = tk.Toplevel(self)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            ttk.Label(
                tip,
                text="移到游戏目标位置 → 左键确认 / Esc取消",
                style="Panel.TLabel",
            ).pack(padx=10, pady=6)
            tip.update_idletasks()
            tip.geometry(f"+{self.winfo_rootx() + 40}+{self.winfo_rooty() + 40}")
        except Exception:
            tip = None
        self._pick_tip = tip

        def work() -> None:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            VK_LBUTTON = 0x01
            VK_ESCAPE = 0x1B

            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            # wait release
            t0 = time.time()
            while time.time() - t0 < 8.0:
                if user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
                    time.sleep(0.02)
                    continue
                break
            ok = False
            cx = cy = 0
            t0 = time.time()
            while time.time() - t0 < 15.0:
                if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
                    break
                if user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
                    pt = POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    # screen -> client
                    pt_c = POINT(pt.x, pt.y)
                    user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(pt_c))
                    cx, cy = int(pt_c.x), int(pt_c.y)
                    ok = True
                    # wait release
                    while user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
                        time.sleep(0.02)
                    break
                time.sleep(0.02)
            def done() -> None:
                try:
                    if self._pick_tip is not None:
                        self._pick_tip.destroy()
                except Exception:
                    pass
                self._pick_tip = None
                if not ok:
                    self.var_click_pos_tip.set("点位: 已取消")
                    return
                self.var_click_cx.set(str(cx))
                self.var_click_cy.set(str(cy))
                self.settings["bg_click_cx"] = cx
                self.settings["bg_click_cy"] = cy
                self.var_click_pos_tip.set(self._click_pos_tip_text())
                self.log(f"鼠标/键盘: 选点 ({cx},{cy})")

            try:
                self.after(0, done)
            except Exception:
                done()

        threading.Thread(target=work, name="pick-click-pos", daemon=True).start()

    def _on_click_start(self, side: str) -> None:
        is_right = side == "right"
        runner = self._click_r_runner if is_right else self._click_l_runner
        if runner and runner.is_running():
            return
        sess = self._require_session()
        if sess is None:
            (self.var_click_r_status if is_right else self.var_click_l_status).set(
                "未挂载/注入中"
            )
            return
        pid, hwnd = int(sess.pid or 0), int(sess.hwnd or 0)
        if not pid:
            (self.var_click_r_status if is_right else self.var_click_l_status).set(
                "未挂载"
            )
            return
        btn = UI_CLICK_RIGHT if is_right else UI_CLICK_LEFT
        cfg = self._click_cfg_from_ui(btn)
        r = MouseClickerRunner(
            pid,
            hwnd=hwnd,
            cfg=cfg,
            log=lambda m: self._push("log", m),
            on_status=lambda m, right=is_right: self._push(
                "click_r_status" if right else "click_l_status", m
            ),
        )
        if is_right:
            self._click_r_runner = r
            self.var_click_r_status.set("启动中…")
        else:
            self._click_l_runner = r
            self.var_click_l_status.set("启动中…")
        if not r.start():
            if is_right:
                self.var_click_r_status.set("启动失败")
                self._click_r_runner = None
            else:
                self.var_click_l_status.set("启动失败")
                self._click_l_runner = None
            try:
                side_cn = "右键" if is_right else "左键"
                self.user_log(
                    f"操作：{side_cn}连点启动失败",
                    category=CAT_OP,
                    source="鼠标/键盘",
                )
            except Exception:
                pass
            return
        self._set_click_running(side, True)
        try:
            side_cn = "右键" if is_right else "左键"
            self.user_log(
                f"操作：{side_cn}连点已开始",
                category=CAT_OP,
                source="鼠标/键盘",
            )
        except Exception:
            pass
        self.after(400, lambda s=side: self._watch_click(s))

    def _watch_click(self, side: str) -> None:
        r = self._click_r_runner if side == "right" else self._click_l_runner
        if r is None:
            self._set_click_running(side, False)
            return
        if r.is_running():
            self.after(400, lambda s=side: self._watch_click(s))
            return
        if side == "right":
            self._click_r_runner = None
            self.var_click_r_status.set("已停止")
        else:
            self._click_l_runner = None
            self.var_click_l_status.set("已停止")
        self._set_click_running(side, False)

    def _on_click_stop(self, side: str) -> None:
        if side == "right":
            r = self._click_r_runner
            self._click_r_runner = None
            status_var = self.var_click_r_status
        else:
            r = self._click_l_runner
            self._click_l_runner = None
            status_var = self.var_click_l_status
        if r is not None:
            try:
                r.stop()
            except Exception:
                pass
        self._set_click_running(side, False)
        try:
            status_var.set("已停止")
        except Exception:
            pass
        try:
            side_cn = "右键" if side == "right" else "左键"
            self.user_log(
                f"操作：{side_cn}连点已停止",
                category=CAT_OP,
                source="鼠标/键盘",
            )
        except Exception:
            pass
