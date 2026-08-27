# -*- coding: utf-8 -*-
"""
Shared light theme + layout helpers for the business shell.

Normal system-like colors, uniform buttons, grouped sections.

@author by ak
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

# Light, neutral operational palette
C = {
    "bg": "#f3f3f3",
    "panel": "#ffffff",
    "panel2": "#fafafa",
    "border": "#d0d0d0",
    "border_soft": "#e5e5e5",
    "text": "#1a1a1a",
    "muted": "#666666",
    "dim": "#999999",
    "accent": "#0078d4",
    "accent_hi": "#106ebe",
    "accent_dim": "#cce4f7",
    "ok": "#107c10",
    "warn": "#9a6700",
    "err": "#c42b1c",
    "row": "#ffffff",
    "row_alt": "#f7f7f7",
    "select": "#0078d4",
    "select_fg": "#ffffff",
    "btn": "#ffffff",
    "btn_hover": "#eef6fc",
    "btn_border": "#adadad",
    "nav": "#ececec",
    "nav_active": "#ffffff",
    "nav_hover": "#e2e2e2",
    "input": "#ffffff",
    "top": "#ffffff",
    "foot": "#ececec",
}

# Uniform control sizes
BTN_PAD = (14, 5)
BTN_PAD_SM = (10, 4)
CTRL_HEIGHT_HINT = 28


def apply_theme(root: tk.Misc) -> ttk.Style:
    """
    Apply clam-based light theme with uniform buttons.

    @author by ak
    """
    try:
        root.configure(bg=C["bg"])
    except tk.TclError:
        pass

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    font_ui = ("Microsoft YaHei UI", 9)
    font_ui_b = ("Microsoft YaHei UI", 9, "bold")
    font_title = ("Microsoft YaHei UI", 16, "bold")
    font_h = ("Microsoft YaHei UI", 11, "bold")
    font_sec = ("Microsoft YaHei UI", 9, "bold")
    font_mono = ("Consolas", 9)

    style.configure(".", background=C["bg"], foreground=C["text"], font=font_ui)
    style.configure("TFrame", background=C["bg"])
    style.configure("Panel.TFrame", background=C["panel"])
    style.configure("Panel2.TFrame", background=C["panel2"])
    style.configure("Nav.TFrame", background=C["nav"])
    style.configure("Top.TFrame", background=C["top"])
    style.configure("Foot.TFrame", background=C["foot"])
    style.configure("Content.TFrame", background=C["bg"])

    style.configure("TLabel", background=C["bg"], foreground=C["text"], font=font_ui)
    style.configure("Panel.TLabel", background=C["panel"], foreground=C["text"])
    style.configure("Top.TLabel", background=C["top"], foreground=C["text"])
    style.configure("Foot.TLabel", background=C["foot"], foreground=C["muted"], font=font_mono)
    style.configure("Header.TLabel", background=C["bg"], foreground=C["text"], font=font_h)
    style.configure(
        "Panel.Header.TLabel",
        background=C["panel"],
        foreground=C["text"],
        font=font_h,
    )
    style.configure(
        "Top.Header.TLabel",
        background=C["top"],
        foreground=C["text"],
        font=font_h,
    )
    style.configure(
        "Welcome.TLabel",
        background=C["bg"],
        foreground=C["text"],
        font=font_title,
    )
    style.configure(
        "Muted.TLabel",
        background=C["bg"],
        foreground=C["muted"],
        font=font_ui,
    )
    style.configure(
        "Panel.Muted.TLabel",
        background=C["panel"],
        foreground=C["muted"],
        font=font_ui,
    )
    style.configure(
        "Mono.TLabel",
        background=C["bg"],
        foreground=C["text"],
        font=font_mono,
    )
    style.configure(
        "Panel.Mono.TLabel",
        background=C["panel"],
        foreground=C["text"],
        font=font_mono,
    )
    style.configure(
        "Section.TLabel",
        background=C["panel"],
        foreground=C["text"],
        font=font_sec,
    )
    style.configure(
        "Badge.TLabel",
        background=C["accent_dim"],
        foreground=C["accent"],
        font=("Microsoft YaHei UI", 8),
        padding=(6, 1),
    )
    style.configure(
        "NavBrand.TLabel",
        background=C["nav"],
        foreground=C["text"],
        font=("Microsoft YaHei UI", 10, "bold"),
    )
    style.configure(
        "NavTitle.TLabel",
        background=C["nav"],
        foreground=C["muted"],
        font=("Microsoft YaHei UI", 8),
    )
    style.configure(
        "NavGroup.TLabel",
        background=C["nav"],
        foreground=C["dim"],
        font=("Microsoft YaHei UI", 8),
        padding=(4, 2),
    )
    style.configure(
        "Ok.TLabel",
        background=C["bg"],
        foreground=C["ok"],
        font=font_ui,
    )
    style.configure(
        "Warn.TLabel",
        background=C["bg"],
        foreground=C["warn"],
        font=font_ui,
    )
    style.configure(
        "Err.TLabel",
        background=C["bg"],
        foreground=C["err"],
        font=font_ui,
    )

    # --- buttons: same height/padding family ---
    # Share one layout so Accent / default / Ghost paint the same height
    # (clam can otherwise size Accent differently from TButton).
    _btn_layout = style.layout("TButton")
    style.configure(
        "TButton",
        background=C["btn"],
        foreground=C["text"],
        bordercolor=C["btn_border"],
        lightcolor=C["btn"],
        darkcolor=C["btn"],
        focuscolor=C["accent_dim"],
        padding=BTN_PAD,
        font=font_ui,
        borderwidth=1,
    )
    style.map(
        "TButton",
        background=[("active", C["btn_hover"]), ("disabled", C["panel2"])],
        foreground=[("disabled", C["dim"])],
        bordercolor=[("active", C["accent"]), ("disabled", C["border_soft"])],
    )
    style.layout("Accent.TButton", _btn_layout)
    style.configure(
        "Accent.TButton",
        background=C["accent"],
        foreground="#ffffff",
        bordercolor=C["accent"],
        lightcolor=C["accent"],
        darkcolor=C["accent"],
        focuscolor=C["accent_dim"],
        padding=BTN_PAD,
        font=font_ui,
        borderwidth=1,
    )
    style.map(
        "Accent.TButton",
        background=[("active", C["accent_hi"]), ("disabled", C["accent_dim"])],
        foreground=[("disabled", C["muted"])],
        bordercolor=[("active", C["accent_hi"]), ("disabled", C["border"])],
    )
    style.configure(
        "Ghost.TButton",
        background=C["btn"],
        foreground=C["text"],
        bordercolor=C["btn_border"],
        lightcolor=C["btn"],
        darkcolor=C["btn"],
        padding=BTN_PAD,
        font=font_ui,
    )
    style.map(
        "Ghost.TButton",
        background=[("active", C["btn_hover"])],
        bordercolor=[("active", C["accent"])],
    )
    # Compact toolbar buttons (super-loot / 妖楼 action row, etc.)
    # Same layout + font as base TButton family so Accent/default share height.
    style.layout("Compact.TButton", _btn_layout)
    style.configure(
        "Compact.TButton",
        background=C["btn"],
        foreground=C["text"],
        bordercolor=C["btn_border"],
        lightcolor=C["btn"],
        darkcolor=C["btn"],
        focuscolor=C["accent_dim"],
        padding=BTN_PAD_SM,
        font=font_ui,
        borderwidth=1,
    )
    style.map(
        "Compact.TButton",
        background=[("active", C["btn_hover"]), ("disabled", C["panel2"])],
        foreground=[("disabled", C["dim"])],
        bordercolor=[("active", C["accent"]), ("disabled", C["border_soft"])],
    )
    style.layout("Compact.Accent.TButton", _btn_layout)
    style.configure(
        "Compact.Accent.TButton",
        background=C["accent"],
        foreground="#ffffff",
        bordercolor=C["accent"],
        lightcolor=C["accent"],
        darkcolor=C["accent"],
        focuscolor=C["accent_dim"],
        padding=BTN_PAD_SM,
        # Keep same font weight as Compact.TButton — bold makes primary look taller.
        font=font_ui,
        borderwidth=1,
    )
    style.map(
        "Compact.Accent.TButton",
        background=[("active", C["accent_hi"]), ("disabled", C["accent_dim"])],
        foreground=[("disabled", C["muted"])],
        bordercolor=[("active", C["accent_hi"]), ("disabled", C["border"])],
    )
    # Compact label-style tabs (not full buttons)
    style.configure(
        "Tab.TLabel",
        background=C["panel"],
        foreground=C["muted"],
        font=font_ui,
        padding=(6, 1),
        cursor="hand2",
    )
    style.configure(
        "TabActive.TLabel",
        background=C["panel"],
        foreground=C["accent"],
        font=font_ui_b,
        padding=(6, 1),
        cursor="hand2",
    )
    # Clickable link-style label (dev panel, etc.)
    style.configure(
        "Link.TLabel",
        background=C["panel"],
        foreground=C["accent"],
        font=font_ui,
        padding=(2, 2),
        cursor="hand2",
    )
    style.configure(
        "Link.Muted.TLabel",
        background=C["bg"],
        foreground=C["accent"],
        font=font_ui,
        padding=(2, 2),
        cursor="hand2",
    )
    style.configure(
        "Top.Link.TLabel",
        background=C["top"],
        foreground=C["accent"],
        font=font_ui,
        padding=(0, 0),
        cursor="hand2",
    )
    style.configure(
        "Nav.TButton",
        background=C["nav"],
        foreground=C["text"],
        bordercolor=C["nav"],
        lightcolor=C["nav"],
        darkcolor=C["nav"],
        padding=(8, 5),
        anchor="w",
        font=font_ui,
        width=9,
    )
    style.map(
        "Nav.TButton",
        background=[("active", C["nav_hover"])],
        foreground=[("active", C["text"])],
        bordercolor=[("active", C["nav_hover"])],
    )
    style.configure(
        "NavActive.TButton",
        background=C["nav_active"],
        foreground=C["accent"],
        bordercolor=C["border"],
        lightcolor=C["nav_active"],
        darkcolor=C["nav_active"],
        padding=(8, 5),
        anchor="w",
        font=font_ui_b,
        width=9,
    )
    style.map(
        "NavActive.TButton",
        background=[("active", C["nav_active"])],
        foreground=[("active", C["accent"])],
    )

    style.configure(
        "TCheckbutton",
        background=C["panel"],
        foreground=C["text"],
        focuscolor=C["panel"],
        font=font_ui,
        padding=(0, 3),
    )
    style.map(
        "TCheckbutton",
        background=[("active", C["panel"])],
        foreground=[("disabled", C["dim"])],
    )
    # Soft role chips for 主控/副控/无控 (card-like checkbuttons)
    style.configure(
        "Role.TCheckbutton",
        background=C["panel2"],
        foreground=C["text"],
        focuscolor=C["panel2"],
        font=font_ui,
        padding=(10, 8),
    )
    style.map(
        "Role.TCheckbutton",
        background=[("active", C["accent_dim"]), ("selected", C["accent_dim"])],
        foreground=[("selected", C["accent"]), ("disabled", C["dim"])],
    )
    style.configure(
        "RoleCard.TFrame",
        background=C["panel2"],
        relief="flat",
    )
    style.configure(
        "RoleCardActive.TFrame",
        background=C["accent_dim"],
        relief="flat",
    )
    style.configure(
        "RoleTitle.TLabel",
        background=C["panel2"],
        foreground=C["text"],
        font=font_ui_b,
    )
    style.configure(
        "RoleTitleActive.TLabel",
        background=C["accent_dim"],
        foreground=C["accent"],
        font=font_ui_b,
    )
    style.configure(
        "RoleDesc.TLabel",
        background=C["panel2"],
        foreground=C["muted"],
        font=("Microsoft YaHei UI", 8),
    )
    style.configure(
        "RoleDescActive.TLabel",
        background=C["accent_dim"],
        foreground=C["accent"],
        font=("Microsoft YaHei UI", 8),
    )

    style.configure(
        "TLabelframe",
        background=C["panel"],
        foreground=C["text"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        relief="solid",
        borderwidth=1,
        padding=(8, 6),
    )
    style.configure(
        "TLabelframe.Label",
        background=C["panel"],
        foreground=C["text"],
        font=font_sec,
    )

    style.configure(
        "TCombobox",
        fieldbackground=C["input"],
        background=C["btn"],
        foreground=C["text"],
        arrowcolor=C["text"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        padding=4,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", C["input"])],
        foreground=[("readonly", C["text"])],
        selectbackground=[("readonly", C["select"])],
        selectforeground=[("readonly", C["select_fg"])],
    )

    # Match entry visual height with adjacent buttons (login / show)
    style.configure(
        "TEntry",
        fieldbackground=C["input"],
        foreground=C["text"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        insertcolor=C["text"],
        padding=(8, 6),
        font=font_ui,
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", C["accent"])],
        lightcolor=[("focus", C["accent"])],
        darkcolor=[("focus", C["accent"])],
    )

    style.configure("TSeparator", background=C["border"])
    style.configure(
        "TScrollbar",
        background=C["border_soft"],
        troughcolor=C["panel"],
        bordercolor=C["panel"],
        arrowcolor=C["muted"],
        lightcolor=C["border_soft"],
        darkcolor=C["border_soft"],
    )
    style.map("TScrollbar", background=[("active", C["border"])])

    style.configure(
        "Treeview",
        background=C["row"],
        fieldbackground=C["row"],
        foreground=C["text"],
        bordercolor=C["border"],
        rowheight=26,
        font=font_ui,
    )
    style.configure(
        "Treeview.Heading",
        background=C["panel2"],
        foreground=C["muted"],
        relief="flat",
        font=font_ui_b,
    )
    style.map(
        "Treeview",
        background=[("selected", C["select"])],
        foreground=[("selected", C["select_fg"])],
    )

    return style


def make_text(parent: tk.Misc, *, height: int = 8, mono: bool = True) -> tk.Text:
    """
    Light log / notes text widget.

    @author by ak
    """
    font = ("Consolas", 9) if mono else ("Microsoft YaHei UI", 9)
    return tk.Text(
        parent,
        height=height,
        wrap=tk.WORD,
        font=font,
        bg=C["row"],
        fg=C["text"],
        insertbackground=C["text"],
        selectbackground=C["select"],
        selectforeground=C["select_fg"],
        relief=tk.FLAT,
        borderwidth=0,
        highlightthickness=1,
        highlightbackground=C["border"],
        highlightcolor=C["accent"],
        padx=8,
        pady=6,
    )


def make_listbox(parent: tk.Misc, *, height: int = 8) -> tk.Listbox:
    """
    Light listbox with consistent selection.

    @author by ak
    """
    return tk.Listbox(
        parent,
        height=height,
        font=("Consolas", 9),
        bg=C["row"],
        fg=C["text"],
        selectbackground=C["select"],
        selectforeground=C["select_fg"],
        activestyle="none",
        relief=tk.FLAT,
        borderwidth=0,
        highlightthickness=1,
        highlightbackground=C["border"],
        highlightcolor=C["accent"],
        exportselection=False,
    )


def pack_scrollable_list(parent: tk.Misc, listbox: tk.Listbox) -> None:
    """
    Pack listbox + vertical scrollbar into parent (pack only).

    listbox must already be a child of parent. Parent may already have
    pack-managed siblings (e.g. tab row); do not mix grid here.

    Mouse wheel over the list scrolls the list itself and stops propagation
    so an outer page scroller does not steal the event.

    @author by ak
    """
    sb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=listbox.yview)
    listbox.configure(yscrollcommand=sb.set)
    # Keep caller-set height (e.g. TaskPage height=36/72). Only force height=1
    # when unset/1 so expand can fill leftover space under tabs.
    try:
        cur_h = int(listbox.cget("height") or 0)
    except Exception:
        cur_h = 0
    if cur_h <= 1:
        try:
            listbox.configure(height=1)
        except tk.TclError:
            pass
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    sb.pack(side=tk.RIGHT, fill=tk.Y)

    def _list_wheel(event) -> str:
        try:
            delta = int(getattr(event, "delta", 0) or 0)
        except Exception:
            delta = 0
        if delta == 0:
            return "break"
        steps = int(-1 * (delta / 120))
        if steps == 0:
            steps = -1 if delta > 0 else 1
        try:
            listbox.yview_scroll(steps, "units")
        except Exception:
            pass
        return "break"

    try:
        listbox.bind("<MouseWheel>", _list_wheel, add="+")
        # Also when pointer is on the scrollbar of this list
        sb.bind("<MouseWheel>", _list_wheel, add="+")
    except Exception:
        pass


def pack_scrollable_text(parent: tk.Misc, text: tk.Text) -> None:
    """
    Pack text + vertical scrollbar into parent (pack only).

    text must already be a child of parent. Avoid grid when siblings use pack.

    @author by ak
    """
    sb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=text.yview)
    text.configure(yscrollcommand=sb.set)
    try:
        text.configure(height=1)
    except tk.TclError:
        pass
    text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    sb.pack(side=tk.RIGHT, fill=tk.Y)


def make_scrollable_body(
    parent: tk.Misc,
    *,
    style: str = "Content.TFrame",
) -> tuple[ttk.Frame, ttk.Frame]:
    """
    Build a Canvas-backed vertical scroll region.

    Returns (outer_frame, body_frame). Pack widgets into body_frame; pack
    outer_frame into parent with fill=BOTH expand=True.

    Mouse wheel scrolls when the pointer is over the body (or children).
    @author by ak
    """
    outer = ttk.Frame(parent, style=style)
    canvas = tk.Canvas(
        outer,
        highlightthickness=0,
        borderwidth=0,
        bg=C["bg"],
    )
    sb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
    body = ttk.Frame(canvas, style=style)
    body_id = canvas.create_window((0, 0), window=body, anchor="nw")

    def _on_body_configure(_event=None) -> None:
        try:
            canvas.configure(scrollregion=canvas.bbox("all"))
        except Exception:
            pass

    def _on_canvas_configure(event) -> None:
        try:
            canvas.itemconfigure(body_id, width=max(1, int(event.width)))
        except Exception:
            pass

    def _is_nested_scroller(widget) -> bool:
        """List/Text/Tree keep their own wheel; outer page must not steal it."""
        try:
            cls = widget.winfo_class()
        except Exception:
            return False
        if cls in ("Listbox", "Text", "Treeview", "Canvas"):
            # Outer canvas itself is ok to bind; nested Canvas rare.
            try:
                if widget is canvas:
                    return False
            except Exception:
                pass
            return True
        return False

    def _wheel(event) -> str | None:
        # Do not scroll the page when pointer is over a nested list/text.
        try:
            w = event.widget
            cur = w
            for _ in range(12):
                if cur is None:
                    break
                if _is_nested_scroller(cur) and cur is not canvas:
                    return None
                try:
                    cur = cur.master
                except Exception:
                    break
        except Exception:
            pass
        # Windows: event.delta is multiple of 120
        try:
            delta = int(event.delta)
        except Exception:
            delta = 0
        if delta == 0:
            return None
        steps = int(-1 * (delta / 120))
        if steps == 0:
            steps = -1 if delta > 0 else 1
        try:
            canvas.yview_scroll(steps, "units")
        except Exception:
            pass
        return "break"

    def _bind_wheel(widget) -> None:
        if _is_nested_scroller(widget):
            return
        try:
            widget.bind("<MouseWheel>", _wheel, add="+")
        except Exception:
            pass

    body.bind("<Configure>", _on_body_configure)
    canvas.bind("<Configure>", _on_canvas_configure)
    _bind_wheel(canvas)
    _bind_wheel(body)
    # Also bind on outer so empty margins scroll
    _bind_wheel(outer)

    # Recursively bind new children so wheel works over labels/entries
    def _bind_tree(w) -> None:
        if _is_nested_scroller(w):
            return
        _bind_wheel(w)
        try:
            for ch in w.winfo_children():
                _bind_tree(ch)
        except Exception:
            pass

    def _on_map(_event=None) -> None:
        try:
            _bind_tree(body)
            canvas.configure(scrollregion=canvas.bbox("all"))
        except Exception:
            pass

    body.bind("<Map>", _on_map, add="+")
    # After widgets pack, caller can call body.update_idletasks; also rebind
    # when children are added via bind_class is heavy — rebind on Configure.
    body.bind("<Configure>", lambda e: _bind_tree(body), add="+")

    canvas.configure(yscrollcommand=sb.set)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    sb.pack(side=tk.RIGHT, fill=tk.Y)
    return outer, body


def pill_tabs(
    parent: tk.Misc,
    labels: list[str],
    on_change: Callable[[str], None],
    *,
    initial: str | None = None,
) -> tuple[ttk.Frame, tk.StringVar, Callable[[str], None]]:
    """
    Compact label-style tabs (regular / blacklist switcher).

    Returns (row_frame, active_var, set_active).
    @author by ak
    """
    row = ttk.Frame(parent, style="Panel.TFrame")
    row.pack(fill=tk.X, pady=(0, 2))
    var = tk.StringVar(value=initial or (labels[0] if labels else ""))
    labels_w: dict[str, ttk.Label] = {}

    def _paint() -> None:
        cur = var.get()
        for name, lbl in labels_w.items():
            style = "TabActive.TLabel" if name == cur else "Tab.TLabel"
            try:
                lbl.configure(style=style)
            except tk.TclError:
                pass

    def set_active(name: str) -> None:
        if name not in labels_w:
            return
        var.set(name)
        _paint()
        try:
            on_change(name)
        except Exception:
            pass

    def _bind(name: str) -> Callable[[tk.Event], None]:
        return lambda _e: set_active(name)

    for i, name in enumerate(labels):
        lbl = ttk.Label(row, text=name, style="Tab.TLabel", cursor="hand2")
        lbl.pack(side=tk.LEFT, padx=(0 if i == 0 else 8, 0))
        lbl.bind("<Button-1>", _bind(name))
        labels_w[name] = lbl
        if i < len(labels) - 1:
            ttk.Label(row, text="|", style="Panel.Muted.TLabel").pack(
                side=tk.LEFT, padx=(8, 0)
            )
    _paint()
    return row, var, set_active

def set_dot(canvas: tk.Canvas, color_key: str, bg_key: str = "foot") -> None:
    """Update status dot fill. @author by ak"""
    try:
        canvas.configure(bg=C.get(bg_key, C["foot"]))
        canvas.delete("all")
        canvas.create_oval(1, 1, 9, 9, fill=C.get(color_key, C["dim"]), outline="")
    except Exception:
        pass


def section(parent: tk.Misc, title: str) -> ttk.LabelFrame:
    """
    Grouped content block (分类区块).

    @author by ak
    """
    return ttk.LabelFrame(parent, text=title)


def action_bar(parent: tk.Misc) -> ttk.Frame:
    """
    Bottom-aligned action row for uniform buttons.

    Packs to bottom first so primary actions stay visible on short windows.
    @author by ak
    """
    bar = ttk.Frame(parent)
    bar.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
    return bar


def page_header(
    parent: tk.Misc,
    title: str,
    subtitle: str = "",
    badge: str = "",
) -> ttk.Frame:
    """
    Feature page title row (compact).

    @author by ak
    """
    head = ttk.Frame(parent)
    head.pack(fill=tk.X, pady=(0, 10))
    row = ttk.Frame(head)
    row.pack(anchor="w", fill=tk.X)
    ttk.Label(row, text=title, style="Header.TLabel").pack(side=tk.LEFT)
    if badge:
        ttk.Label(row, text=badge, style="Badge.TLabel").pack(side=tk.LEFT, padx=(8, 0))
    if subtitle:
        ttk.Label(head, text=subtitle, style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
    return head


def session_selector(
    parent: tk.Misc,
    store,
    on_pick: Callable[[int | None], None],
) -> tuple[ttk.Combobox, tk.StringVar, Callable[[], None]]:
    """
    Session combobox inside a「目标」group-friendly row.

    Returns (combo, var, refresh_fn).
    @author by ak
    """
    row = ttk.Frame(parent, style="Panel.TFrame")
    row.pack(fill=tk.X)
    ttk.Label(row, text="客户端", style="Panel.Muted.TLabel", width=8).pack(side=tk.LEFT)
    var = tk.StringVar(value="")
    combo = ttk.Combobox(row, textvariable=var, state="readonly")
    combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
    pid_by_label: dict[str, int] = {}

    def refresh() -> None:
        items = store.list()
        labels = [s.label() for s in items]
        pid_by_label.clear()
        for s in items:
            pid_by_label[s.label()] = s.pid
        combo["values"] = labels
        if not labels:
            var.set("（尚未挂载）")
            on_pick(None)
            return
        cur = var.get()
        if cur not in labels:
            var.set(labels[0])
            on_pick(pid_by_label.get(labels[0]))
        else:
            on_pick(pid_by_label.get(cur))

    def _on_selected(_evt=None) -> None:
        on_pick(pid_by_label.get(var.get()))

    combo.bind("<<ComboboxSelected>>", _on_selected)
    return combo, var, refresh
