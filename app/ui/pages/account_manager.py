# -*- coding: utf-8 -*-
"""
账号管理窗口（托盘独立入口，仅登录后可用）。

功能：
- 账号 / 三角色槽 CRUD（列表右键菜单；空列表只有「添加账号」）
-   写入 roles/{role_id}/ 下的系统设置，正式面板直接读取
- 本机登录器路径记忆（人工选一次，重启保留）
- 统一登录：勾选多个账号后依次自动登录（一个进世界后再下一个）；右键可单独登录
- 停止登陆：立即停止整个统一登录队列（含当前账号）
- 右键「关闭游戏」：结束该账号在线游戏（程序 pid / live / 标题匹配）
- 账号列表「状态」列实时显示登录阶段（启动登录器/选区/账密/选角/进世界）与注入后动态

@author by ak
"""
from __future__ import annotations

import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from app.core import account_manager as am
from app.ui.theme import apply_theme


def _slot_text(slots: list[dict], active_rid: str = "") -> str:
    """角色列摘要（每个账号一个角色）：'角色1·张三(1001)' / '未启用·张三'. @author by ak"""
    active_rid = str(active_rid or "").strip()
    bound: list[str] = []
    active_tag = ""
    for slot in slots or []:
        try:
            ri = int(slot.get("role_index") or am.ROLE_INDEX_NONE)
        except Exception:
            ri = am.ROLE_INDEX_NONE
        rid = str(slot.get("role_id") or "").strip()
        name = str(slot.get("name") or "").strip()
        tag = am.ROLE_INDEX_LABELS.get(ri, f"角色{ri}")
        if ri in am.ROLE_INDEX_ACTIVE:
            if rid and name:
                text = f"{tag}·{name}({rid})"
            elif rid:
                text = f"{tag}·({rid})"
            else:
                text = f"{tag}·待绑定"
            # 优先展示当前登录的角色（live 命中），其次第一个启用槽。
            if active_rid and rid == active_rid:
                return text
            if not active_tag:
                active_tag = text
        # 未启用但已绑定真实角色：记录用于兜底展示。
        if rid or name:
            bound.append(name or rid)
    if active_tag:
        return active_tag
    if bound:
        # 全未启用：仍展示绑定的角色名，避免「未启用」看起来像被清空。
        return f"未启用·{bound[0]}" + (f"(+{len(bound) - 1})" if len(bound) > 1 else "")
    return "未启用"


def _clip(s: str, n: int) -> str:
    """Truncate long cell text so Treeview columns never overflow visually. @author by ak"""
    s = str(s or "")
    return s if len(s) <= n else s[: max(1, n - 1)] + "…"


class AccountManagerWindow(tk.Toplevel):
    """
    Standalone account manager window opened from the tray menu (logged-in only).

    @author by ak
    """

    def __init__(self, master=None, *, log=None, live_provider=None):
        super().__init__(master)
        self.title("账号管理")
        self.geometry("800x520")
        self.minsize(720, 460)
        self._log = log or (lambda m: None)
        # 运行通道：源码 / 测试 / 正式。轮询测试选角等开发功能仅源码通道显示。
        try:
            self._instance_scope = str(
                getattr(getattr(self, "master", None), "_instance_scope", None)
                or ""
            ).strip()
        except Exception:
            self._instance_scope = ""
        if not self._instance_scope:
            try:
                from app.core.single_instance import instance_scope

                self._instance_scope = str(instance_scope() or "").strip()
            except Exception:
                self._instance_scope = ""
        # live_provider() -> {role_id: {"pid","map","action"}} for the 状态 column
        self._live_provider = live_provider
        self._status = tk.StringVar(value="就绪")
        self._checked: set[str] = set()
        # 统一登录并发/生命周期守卫：登录中禁止重复点击，关窗后回调不再触碰控件。
        self._login_busy = False
        self._login_wait_until = 0
        self._login_tick_job = None
        self._login_last_stage = "launcher"
        self._btn_launch = None
        # 批量统一登录：待登录账号队列 + 每个账号的当前状态（登录阶段/完成/失败）。
        self._login_queue: list[str] = []
        self._login_status: dict[str, str] = {}
        self._login_current = ""
        self._login_batch_total = 0
        self._login_cancel = False
        # 停止登陆：真正中止编排线程的事件（传给 start_launcher_and_wait_server）。
        self._login_stop_event = None
        self._build()
        self.after(80, self.refresh_all)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        apply_theme(self)
        outer = ttk.Frame(self, style="Content.TFrame", padding=(12, 10))
        outer.pack(fill=tk.BOTH, expand=True)

        head = ttk.Frame(outer, style="Content.TFrame")
        head.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(head, text="账号管理", style="Welcome.TLabel").pack(anchor="w")
        ttk.Label(
            head,
            text=(
                "右键账号可 添加/编辑/删除、单独「登录账号」。"
                "勾选多个账号后点「统一登录」依次自动登录（一个进世界后再下一个）。"
                "状态列实时显示登录阶段与注入后动态。"
            ),
            style="Muted.TLabel",
            wraplength=720,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(outer, style="Content.TFrame")
        body.pack(fill=tk.BOTH, expand=True)

        panel = ttk.Frame(body, style="Panel.TFrame", padding=(8, 8))
        panel.pack(fill=tk.BOTH, expand=True)
        ttk.Label(panel, text="账号（右键操作；勾选用于批量操作）", style="Section.TLabel").pack(anchor="w")

        cols = ("sel", "idx", "pid", "account_id", "inject", "slots", "status")
        tree = ttk.Treeview(panel, columns=cols, show="headings", height=10)
        tree.column("#0", width=0, minwidth=0, stretch=False)
        for c, text, width, stretch in (
            ("sel", "全选", 56, False),
            ("idx", "序号", 46, False),
            ("pid", "PID", 64, False),
            ("account_id", "账号", 120, True),
            ("inject", "注入", 46, False),
            ("slots", "角色", 210, True),
            ("status", "状态", 250, True),
        ):
            tree.heading(c, text=text, anchor="w")
            tree.column(
                c, width=width, minwidth=40, stretch=bool(stretch), anchor="w"
            )
        tree.heading("sel", command=self._toggle_select_all)
        sb = ttk.Scrollbar(panel, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(4, 4))
        sb.pack(side=tk.RIGHT, fill=tk.Y, pady=(4, 4))
        tree.bind("<Button-3>", self._on_account_context)
        tree.bind("<Button-1>", self._on_tree_click, add="+")
        tree.bind("<Double-1>", self._on_account_double_click)
        self._acc_tree = tree

        foot = self._build_footer(outer)
        foot.pack(fill=tk.X, pady=(8, 0))

        ttk.Label(
            outer, textvariable=self._status, style="Muted.TLabel"
        ).pack(fill=tk.X, pady=(6, 0))

    def _build_footer(self, parent: tk.Misc) -> ttk.Frame:
        # 单行按钮组（右对齐）：路径居左，选择/统一登录/停止登陆/刷新靠右。
        foot = ttk.Frame(parent, style="Content.TFrame")
        self._path_var = tk.StringVar(value="（未设置）")
        ttk.Label(foot, text="登录器路径", style="Muted.TLabel").pack(side=tk.LEFT)
        self._path_lbl = ttk.Label(
            foot, textvariable=self._path_var, style="Mono.TLabel",
            width=24, anchor="w",
        )
        self._path_lbl.pack(side=tk.LEFT, padx=(6, 8))
        # 轮询测试选角：仅源码（开发）通道显示，正式/测试包隐藏。
        if str(getattr(self, "_instance_scope", "") or "") == "source":
            ttk.Button(
                foot, text="轮询测试选角", style="Compact.TButton",
                command=self._poll_test_char_select,
            ).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(
            foot, text="刷新", style="Compact.TButton", command=self.refresh_all
        ).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(
            foot, text="关闭选中", style="Compact.TButton",
            command=self._on_close_selected,
        ).pack(side=tk.RIGHT, padx=(4, 0))
        self._btn_launch = ttk.Button(
            foot, text="统一登录", style="Compact.Accent.TButton",
            command=self._on_launch,
        )
        self._btn_launch.pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(
            foot, text="选择…", style="Compact.TButton", command=self._on_pick_path
        ).pack(side=tk.RIGHT, padx=(4, 0))
        return foot

    # ------------------------------------------------------------- refresh
    def refresh_all(self) -> None:
        """Reload accounts / path (called on open too). @author by ak"""
        try:
            self._refresh_path()
            self._refresh_accounts()
        except Exception as e:
            self._status.set(f"刷新失败: {e}")

    def _refresh_path(self) -> None:
        path = am.remembered_launcher()
        self._path_var.set(path or "（未设置）")

    def _refresh_accounts(self) -> None:
        tree = self._acc_tree
        for iid in tree.get_children():
            tree.delete(iid)
        live = {}
        try:
            live = dict(self._live_provider() or {}) if self._live_provider else {}
        except Exception:
            live = {}
        self._live = live
        self._online = self._online_account_ids(live)
        checked = set(self._checked)
        for idx, acc in enumerate(am.list_accounts(), start=1):
            account_id = str(acc.get("account_id") or "")
            pid_s = self._account_pid(acc, live)
            active_rid = ""
            for slot in acc.get("slots") or []:
                rid = str(slot.get("role_id") or "").strip()
                if rid and live.get(rid):
                    active_rid = rid
                    break
            if not active_rid:
                try:
                    active_rid = am.active_role_id_for_account(account_id)
                except Exception:
                    active_rid = ""
            tree.insert(
                "",
                "end",
                iid=account_id,
                values=(
                    "☑" if account_id in checked else "☐",
                    str(idx),
                    pid_s,
                    _clip(account_id, 18),
                    "开" if acc.get("inject", False) else "关",
                    _clip(_slot_text(acc.get("slots"), active_rid), 42),
                    _clip(self._account_status(acc, live), 24),
                ),
            )

    def _account_pid(self, acc: dict, live: dict) -> str:
        """在线 PID（动态）：live（按 role_id）> 窗口标题角色名匹配；窗口消失即无。@author by ak"""
        account_id = str(acc.get("account_id") or "")
        pid = 0
        for slot in acc.get("slots") or []:
            rid = str(slot.get("role_id") or "").strip()
            if not rid or slot.get("role_index") not in am.ROLE_INDEX_ACTIVE:
                continue
            info = live.get(rid)
            p = int((info or {}).get("pid") or 0)
            if p > 0:
                pid = p
                break
        if not pid:
            try:
                pids = am.running_pids_for_account(account_id)
                if pids:
                    pid = pids[-1]
            except Exception:
                pid = 0
        return str(pid) if pid > 0 else ""

    def _online_account_ids(self, live: dict) -> set[str]:
        """已登录账号集合：注入实例按 role_id 判定，未注入按窗口标题角色名判定。@author by ak"""
        online: set[str] = set()
        for rid in live:
            acc = am.find_account_by_role_id(rid)
            if acc:
                online.add(acc)
        try:
            for name in am.scan_running_role_names():
                acc = am.find_account_by_role_name(name)
                if acc:
                    online.add(acc)
        except Exception:
            pass
        return online

    def _account_status(self, acc: dict, live: dict) -> str:
        """Short status: login stage > live 登录·地图·操作, else online / bound / 未登录. @author by ak"""
        account_id = str(acc.get("account_id") or "")
        stage = self._login_status.get(account_id)
        live_hits = []
        bound = 0
        for slot in acc.get("slots") or []:
            rid = str(slot.get("role_id") or "").strip()
            if not rid or slot.get("role_index") not in am.ROLE_INDEX_ACTIVE:
                continue
            bound += 1
            info = live.get(rid)
            if info:
                parts = ["登录"]
                if info.get("map"):
                    parts.append(str(info["map"]))
                if info.get("action"):
                    parts.append(str(info["action"]))
                live_hits.append("·".join(parts))
        # 登录进行中 / 刚完成：显示当前阶段（比已注入的实时状态优先级更高）。
        if stage and live_hits and str(stage).startswith("已进世界"):
            return f"已进世界 · {' · '.join(live_hits)}"
        if stage:
            return str(stage)
        if live_hits:
            return " · ".join(live_hits)
        online = set(getattr(self, "_online", None) or set())
        if account_id in online:
            return "已登录"
        if bound:
            return f"已绑{bound}角色"
        return "未登录"

    # ----------------------------------------------------------- checkbox
    def _on_tree_click(self, event) -> str | None:
        """Toggle the checkbox (sel column) and support header select-all. @author by ak"""
        tree = self._acc_tree
        try:
            col = tree.identify_column(int(getattr(event, "x", 0) or 0))
            region = tree.identify_region(int(getattr(event, "x", 0) or 0), int(getattr(event, "y", 0) or 0))
        except Exception:
            return None
        if col == "#1":
            if region == "heading":
                self._toggle_select_all()
                return "break"
            if region == "cell":
                row = tree.identify_row(int(getattr(event, "y", 0) or 0))
                if row:
                    self._toggle_one(str(row))
                    try:
                        tree.selection_set(row)
                    except Exception:
                        pass
                return "break"
        return None

    def _on_account_double_click(self, event) -> str | None:
        """双击账号行：该账号有在线 pid 时把对应游戏窗口拉到前台。@author by ak"""
        tree = self._acc_tree
        try:
            row_id = str(tree.identify_row(int(getattr(event, "y", 0) or 0)) or "")
        except Exception:
            row_id = ""
        if not row_id:
            return None
        pid = 0
        try:
            acc = am.get_account(row_id)
            if acc is not None:
                pid = int(
                    self._account_pid(acc, getattr(self, "_live", {}) or {}) or 0
                )
        except Exception:
            pid = 0
        if pid <= 0:
            self._status.set(f"账号 {row_id} 当前无在线游戏窗口")
            return "break"
        try:
            from app.core.inject_gate import find_main_hwnd_for_pid

            hwnd = int(find_main_hwnd_for_pid(pid)[0] or 0)
        except Exception:
            hwnd = 0
        if not hwnd:
            self._status.set(f"账号 {row_id} 未找到游戏窗口 pid={pid}")
            return "break"
        try:
            from app.core.win_capture import ensure_foreground

            ok = ensure_foreground(hwnd)
        except Exception:
            ok = False
        self._status.set(
            f"已将账号 {row_id} 的游戏窗口置前 pid={pid}"
            if ok
            else f"置前失败 pid={pid}"
        )
        return "break"

    def _toggle_one(self, account_id: str) -> None:
        if account_id in self._checked:
            self._checked.discard(account_id)
        else:
            self._checked.add(account_id)
        self._paint_checkboxes()

    def _toggle_select_all(self) -> None:
        all_ids = [str(iid) for iid in self._acc_tree.get_children()]
        if all_ids and all(i in self._checked for i in all_ids):
            for iid in all_ids:
                self._checked.discard(iid)
        else:
            for iid in all_ids:
                self._checked.add(iid)
        self._paint_checkboxes()

    def _paint_checkboxes(self) -> None:
        tree = self._acc_tree
        for iid in tree.get_children():
            cur = list(tree.item(iid, "values"))
            if not cur:
                continue
            cur[0] = "☑" if str(iid) in self._checked else "☐"
            try:
                tree.item(iid, values=tuple(cur))
            except Exception:
                pass

    # ------------------------------------------------------------- context
    def _on_account_context(self, event) -> str:
        """Right-click menu on the account list (empty list => only 添加). @author by ak"""
        tree = self._acc_tree
        try:
            row_id = tree.identify_row(int(getattr(event, "y", 0) or 0))
        except Exception:
            row_id = ""
        if row_id:
            try:
                tree.selection_set(row_id)
            except Exception:
                pass
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="添加账号", command=self._on_add_account)
        if row_id:
            menu.add_separator()
            menu.add_command(label="登录账号", command=self._on_login_single)
            menu.add_command(label="关闭游戏", command=self._on_close_single_game)
            menu.add_command(label="编辑账号", command=self._on_edit_account)
            menu.add_command(label="删除账号", command=self._on_delete_account)
        try:
            menu.tk_popup(
                int(getattr(event, "x_root", 0) or 0),
                int(getattr(event, "y_root", 0) or 0),
            )
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass
        return "break"

    def _selected_account_id(self) -> str:
        sel = self._acc_tree.selection()
        if not sel:
            return ""
        return str(sel[0] or "").strip()

    # ------------------------------------------------------------- account
    def _on_add_account(self) -> None:
        self._open_account_dialog()

    def _on_close_selected(self) -> None:
        """关闭选中（勾选）账号对应的在线游戏。@author by ak"""
        if not self._checked:
            self._status.set("请先勾选要关闭的账号")
            return
        self._close_account_games(sorted(self._checked))

    def _on_login_single(self) -> None:
        """账号右键：单独登录该账号（不进队列）。@author by ak"""
        account_id = self._selected_account_id()
        if not account_id:
            self._status.set("请先在账号列表选择一行")
            return
        if getattr(self, "_login_busy", False):
            self._status.set("一键登录：正在登录中，请稍候")
            return
        if not am.remembered_launcher():
            messagebox.showinfo(
                "账号管理", "尚未设置登录器路径，请先选择。", parent=self
            )
            self._on_pick_path()
            return
        self._start_login_batch([account_id])

    def _on_close_single_game(self) -> None:
        """账号右键：单独关闭该账号的在线游戏（临时 pid / live / 标题匹配）。@author by ak"""
        account_id = self._selected_account_id()
        if not account_id:
            self._status.set("请先在账号列表选择一行")
            return
        self._close_account_games([account_id])

    def _close_account_games(self, account_ids: list[str]) -> None:
        """Collect pids for the given accounts and kill them（动态匹配，匹配不到跳过)."""
        live = {}
        try:
            live = dict(self._live_provider() or {}) if self._live_provider else {}
        except Exception:
            live = {}
        pids: list[int] = []
        labels: list[str] = []
        seen_pids: set[int] = set()
        for account_id in account_ids:
            acc = am.get_account(account_id)
            if acc is None:
                continue
            acc_pids: list[int] = []
            # 1) 已注入：按槽位 role_id 匹配 live 在线 pid。
            for slot in acc.get("slots") or []:
                rid = str(slot.get("role_id") or "").strip()
                if not rid or slot.get("role_index") not in am.ROLE_INDEX_ACTIVE:
                    continue
                info = live.get(rid)
                pid = int((info or {}).get("pid") or 0)
                if pid > 0:
                    acc_pids.append(pid)
            # 2) 动态扫描运行中的 xajh.exe（窗口标题角色名匹配）。
            try:
                for pid in am.running_pids_for_account(account_id):
                    acc_pids.append(pid)
            except Exception:
                pass
            for pid in acc_pids:
                if pid > 0 and pid not in seen_pids:
                    seen_pids.add(pid)
                    pids.append(pid)
                    labels.append(account_id)
        if not pids:
            self._status.set("该账号没有可关闭的在线游戏（匹配不到就算了）")
            messagebox.showinfo(
                "关闭游戏",
                "没有找到该账号可关闭的在线游戏。\n\n"
                "按在线游戏窗口标题角色名动态匹配；匹配不到就跳过。",
                parent=self,
            )
            return
        shown = []
        for lab in labels:
            if lab not in shown:
                shown.append(lab)
        if not messagebox.askyesno(
            "关闭游戏",
            f"将关闭 {len(pids)} 个在线游戏：\n\n" + "\n".join(shown[:8]),
            parent=self,
        ):
            return
        self._status.set("正在关闭游戏…")

        def worker() -> None:
            ok, msg = am.kill_pids(pids, log=self._log)
            self.after(0, lambda: self._finish_close_selected(ok, msg))

        threading.Thread(target=worker, daemon=True, name="xajh-account-close").start()

    def _on_edit_account(self) -> None:
        account_id = self._selected_account_id()
        if not account_id:
            self._status.set("请先在账号列表选择一行")
            return
        self._open_account_dialog(account_id=account_id)

    def _on_delete_account(self) -> None:
        account_id = self._selected_account_id()
        if not account_id:
            self._status.set("请先在账号列表选择一行")
            return
        if not messagebox.askyesno(
            "删除账号",
            f"删除账号 {account_id} 的配置？\n\n仅删除配置，不影响运行中的游戏进程。",
            parent=self,
        ):
            return
        if am.delete_account(account_id):
            self._checked.discard(account_id)
            self._status.set(f"已删除账号 {account_id}（未结束进程）")
            self._log(f"账号管理: 删除账号 {account_id}")
            self._refresh_accounts()

    def _open_account_dialog(self, account_id: str = "") -> None:
        acc = am.get_account(account_id) if account_id else None
        dlg = AccountDialog(
            self,
            account_id=account_id,
            password=acc["password"] if acc else "",
            slots=acc["slots"] if acc else am._empty_slots(),
            inject=acc["inject"] if acc else False,
            dungeon_hang=acc["dungeon_hang"] if acc else False,
            hang_enabled=acc["hang_enabled"] if acc else False,
        )
        self.wait_window(dlg)
        data = dlg.result
        if not data:
            return
        new_id = str(data.get("account_id") or "").strip()
        if not new_id:
            self._status.set("账号不能为空")
            return
        try:
            am.save_account(
                new_id,
                password=str(data.get("password") or ""),
                slots=data.get("slots"),
                inject=bool(data.get("inject", False)),
                dungeon_hang=bool(data.get("dungeon_hang", False)),
                hang_enabled=bool(data.get("hang_enabled", False)),
            )
        except ValueError as e:
            messagebox.showwarning("账号管理", str(e), parent=self)
            return
        if account_id and account_id != new_id:
            am.delete_account(account_id)
        self._status.set(f"已保存账号 {new_id}")
        self._log(f"账号管理: 保存账号 {new_id}")
        self._refresh_accounts()

    # --------------------------------------------------------------- path
    def _on_pick_path(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self,
            title="选择登录器 / 客户端（人工选一次，重启保留）",
            filetypes=[("程序", "*.exe"), ("所有文件", "*.*")],
        )
        chosen = str(chosen or "").strip()
        if not chosen:
            return
        try:
            am.save_game_path("launcher", chosen)
            p = Path(chosen).resolve()
            if str(p).lower().replace("\\", "/").endswith("bin/xajh.exe"):
                am.save_game_path("client", chosen)
        except Exception as e:
            self._status.set(f"保存路径失败: {e}")
            return
        self._status.set(f"已记住登录器路径: {chosen}")
        self._log(f"账号管理: 记住登录器路径 {chosen}")
        self._refresh_path()

    def _on_launch(self) -> None:
        """统一登录：勾选多个账号后依次自动登录（一个进世界后再下一个）。@author by ak"""
        if getattr(self, "_login_busy", False):
            self._status.set("一键登录：正在登录中，请稍候")
            return
        if not am.remembered_launcher():
            messagebox.showinfo(
                "账号管理", "尚未设置登录器路径，请先选择。", parent=self
            )
            self._on_pick_path()
            return
        targets = self._login_targets()
        if not targets:
            self._status.set("请先勾选（或选中）要登录的账号")
            messagebox.showinfo(
                "统一登录", "请先勾选（或选中）要登录的账号。", parent=self
            )
            return
        self._start_login_batch(targets)

    def _login_targets(self) -> list[str]:
        """统一登录目标：已勾选账号优先（按列表顺序），否则当前选中行。@author by ak"""
        if self._checked:
            rows = [str(iid) for iid in self._acc_tree.get_children()]
            return [a for a in rows if a in self._checked]
        sel = self._selected_account_id()
        return [sel] if sel else []

    def _start_login_batch(self, targets: list[str]) -> None:
        """开启统一登录队列：跳过已登录账号，其余校验后依次登录。@author by ak"""
        targets = [a for a in targets if a]
        if not targets:
            return
        missing = [
            a
            for a in targets
            if not str((am.get_account(a) or {}).get("password") or "")
        ]
        if missing:
            self._status.set("部分账号未设置密码")
            messagebox.showwarning(
                "统一登录",
                "以下账号未设置密码，无法登录：\n" + "\n".join(missing[:6]),
                parent=self,
            )
            return
        # 跳过已登录账号：注入实例按 role_id、未注入按窗口标题角色名。
        # 每次登录前重新扫描，避免使用陈旧状态。
        live = {}
        try:
            live = dict(self._live_provider() or {}) if self._live_provider else {}
        except Exception:
            live = {}
        online = self._online_account_ids(live)
        self._online = online
        skipped = [a for a in targets if a in online]
        targets = [a for a in targets if a not in online]
        self._login_status = {a: "排队中" for a in targets}
        for a in skipped:
            self._login_status[a] = "已登录(跳过)"
        if not targets:
            self._login_queue = []
            self._login_batch_total = len(skipped)
            self._login_busy = True
            self._login_cancel = False
            self._set_launch_busy(True)
            self._status.set(f"统一登录：所选账号均已登录（跳过 {len(skipped)}）")
            self._log(
                f"账号管理: 统一登录跳过全部已登录账号: " + ", ".join(skipped)
            )
            self._refresh_accounts()
            self._finish_login_batch()
            return
        self._login_queue = list(targets)
        self._login_batch_total = len(self._login_queue) + len(skipped)
        self._login_busy = True
        self._login_cancel = False
        self._login_stop_event = threading.Event()
        self._set_launch_busy(True)
        tail = f"；已跳过 {len(skipped)} 个已登录" if skipped else ""
        self._status.set(
            f"统一登录：{len(self._login_queue)} 个账号依次登录中…{tail}"
        )
        self._log(
            f"账号管理: 统一登录队列 {len(self._login_queue)} 个账号"
            + (f"（跳过 {len(skipped)} 已登录: " + ", ".join(skipped) + "）" if skipped else "")
            + ": " + ", ".join(targets)
        )
        self._refresh_accounts()
        self._pump_login_queue()

    def _set_launch_busy(self, busy: bool) -> None:
        """统一登录 / 停止登陆 单按钮切换显示。@author by ak"""
        try:
            if self._btn_launch is not None:
                self._btn_launch.configure(
                    text="停止登陆" if busy else "统一登录",
                    command=(self._on_stop_login if busy else self._on_launch),
                    state=tk.NORMAL,
                )
        except Exception:
            pass

    def _on_stop_login(self) -> None:
        """停止登陆：真正中止编排线程（stop_event）+ 清空队列 + 收尾。

        编排各阶段/轮询循环检查 stop_event 后返回 cancelled，worker 线程随即
        退出；队列被清空后不再启动下一个。@author by ak
        """
        self._login_cancel = True
        ev = getattr(self, "_login_stop_event", None)
        if ev is not None:
            try:
                ev.set()
            except Exception:
                pass
        queue = list(getattr(self, "_login_queue", None) or [])
        current = str(getattr(self, "_login_current", "") or "")
        self._login_queue = []
        for a in queue:
            self._login_status[a] = "已取消"
            self._update_row_status(a, "已取消")
            try:
                am.clear_account_runtime_pid(a)
            except Exception:
                pass
        if current:
            self._login_status[current] = "已取消"
            self._update_row_status(current, "已取消")
            try:
                am.clear_account_runtime_pid(current)
            except Exception:
                pass
        self._cancel_login_tick()
        self._login_wait_until = 0
        self._login_busy = False
        self._login_current = ""
        self._set_launch_busy(False)
        try:
            if self.winfo_exists():
                self._status.set(
                    f"已停止统一登录（取消 {len(queue)} 个待登录"
                    + (f" + 当前 {current}" if current else "")
                    + "）"
                )
        except Exception:
            pass
        self._log(
            f"账号管理: 停止统一登录 待登录={len(queue)} "
            + (f"当前={current} " if current else "")
            + (", ".join(queue) if queue else "")
        )

    def _pump_login_queue(self) -> None:
        """启动下一个队列账号；队列空则收尾。@author by ak"""
        if not getattr(self, "_login_busy", False):
            return
        queue = list(getattr(self, "_login_queue", None) or [])
        if not queue:
            self._finish_login_batch()
            return
        if getattr(self, "_login_cancel", False):
            self._login_queue = []
            self._finish_login_batch()
            return
        account_id = queue.pop(0)
        self._login_queue = queue
        self._login_current = account_id
        self._login_status[account_id] = "启动登录器"
        self._update_row_status(account_id, "启动登录器")
        self._start_login_one(account_id)

    def _start_login_one(self, account_id: str) -> None:
        """启动单个账号的登录编排（工作线程）。@author by ak"""
        acc = am.get_account(account_id)
        if acc is None:
            self._finish_login(
                account_id,
                {"ok": False, "error": "no_account", "message": f"账号 {account_id} 配置缺失"},
            )
            return
        password = str(acc.get("password") or "")
        if not password:
            self._finish_login(
                account_id,
                {"ok": False, "error": "no_password", "message": f"账号 {account_id} 未设置密码"},
            )
            return
        # 选角槽位：取该账号第一个已启用的槽位（role_index）。
        # 全部「未启用」时 enter_world=False，停留在选角页，不乱选角色。
        slot = 0
        for s in acc.get("slots") or []:
            try:
                ri = int(s.get("role_index") or am.ROLE_INDEX_NONE)
            except Exception:
                ri = am.ROLE_INDEX_NONE
            if ri in am.ROLE_INDEX_ACTIVE:
                slot = ri
                break
        enter_world = bool(slot in am.ROLE_INDEX_ACTIVE)
        slot = slot if enter_world else 1
        self._login_last_stage = "启动登录器"
        LOGIN_WAIT_S = 120  # 登录器拉起 + 等待游戏窗口的总等待上限
        self._login_wait_until = time.time() + LOGIN_WAIT_S
        self._status.set(
            f"登录账号 {account_id}：启动登录器 → 选区 → 账密 → 角色{slot}"
        )
        self._log(f"账号管理: 登录 {account_id} slot={slot}")

        def tick() -> None:
            """Tk 倒计时：阶段 + 剩余秒数实时显示在状态栏。@author by ak"""
            self._login_tick_job = None
            if not getattr(self, "_login_wait_until", None):
                return
            left = max(0, int(self._login_wait_until - time.time()))
            stage = getattr(self, "_login_last_stage", "启动登录器")
            if left > 0:
                try:
                    self._status.set(f"登录账号 {account_id}：{stage}… 剩余 {left}s")
                except Exception:
                    pass
                self._login_tick_job = self.after(1000, tick)
            else:
                try:
                    self._status.set("登录账号：等待超时（请手动启动登录器）")
                except Exception:
                    pass

        def worker() -> None:
            from app.core.login_orchestrator import (
                ERR_APPLY,
                ERR_LAUNCHER_MISSING,
                ERR_LAUNCHER_UPDATE,
                ERR_LOGIN,
                ERR_LOGIN_TIMEOUT,
                ERR_NO_CHARACTER,
                ERR_NO_GAME_HWND,
                ERR_ROLE_ENTER,
                ERR_SERVER_CONFIRM,
                P_APPLY,
                P_CHAR_SELECT,
                P_CREDENTIALS,
                P_DONE,
                P_ENTER_WORLD,
                P_LAUNCHER,
                P_SERVER,
                P_UPDATE,
                start_launcher_and_wait_server,
            )
            _ERR_MSG = {
                ERR_LAUNCHER_MISSING: "未设置登录器路径",
                ERR_LAUNCHER_UPDATE: "登录器更新弹窗未能关闭",
                ERR_NO_GAME_HWND: "等待游戏窗口超时",
                ERR_SERVER_CONFIRM: "选区确认失败",
                ERR_LOGIN: "登录失败（账号/密码或网络）",
                ERR_LOGIN_TIMEOUT: "登录超时",
                ERR_ROLE_ENTER: "进世界失败",
                ERR_NO_CHARACTER: "所选槽位无角色",
                ERR_APPLY: "进世界后设置应用失败",
            }

            _STAGE_CN = {
                P_LAUNCHER: "启动登录器",
                P_UPDATE: "处理登录器更新",
                P_SERVER: "等待选区",
                P_CREDENTIALS: "填入账密",
                P_CHAR_SELECT: "选角",
                P_ENTER_WORLD: "进入世界",
                P_DONE: "完成",
            }

            def on_progress(stage: str, info: dict) -> None:
                stage_cn = _STAGE_CN.get(stage, stage)
                msg = str(info.get("message") or stage_cn)
                # 一律回主线程更新（worker 不直接写 Tk 状态/字段）。
                try:
                    self.after(
                        0,
                        lambda s=stage_cn, m=msg, a=account_id: self._apply_login_progress(a, s, m),
                    )
                except Exception:
                    pass

            try:
                result = start_launcher_and_wait_server(
                    account_id,
                    password,
                    slot=slot,
                    enter_world=enter_world,
                    launcher_path=am.remembered_launcher(),
                    xajh_timeout_s=LOGIN_WAIT_S,
                    progress=on_progress,
                    log=self._log,
                    stop_event=getattr(self, "_login_stop_event", None),
                )
            except Exception as e:  # pragma: no cover
                try:
                    self.after(
                        0,
                        lambda: self._finish_login(account_id, {"ok": False, "error": str(e), "message": str(e)}),
                    )
                except Exception:
                    pass
                return
            try:
                self.after(0, lambda: self._finish_login(account_id, result))
            except Exception:
                pass

        self._login_tick_job = self.after(1000, tick)
        threading.Thread(
            target=worker, daemon=True, name=f"xajh-account-login-{account_id}"
        ).start()

    def _apply_login_progress(self, account_id: str, stage: str, msg: str) -> None:
        """主线程更新某账号登录阶段 + 状态列 + 状态栏（窗口已销毁则忽略）。@author by ak"""
        self._login_last_stage = stage
        if account_id:
            self._login_status[account_id] = stage
            self._update_row_status(account_id, stage)
        try:
            if self.winfo_exists():
                self._status.set(f"登录账号 {account_id}：{msg}")
        except Exception:
            pass

    def _update_row_status(self, account_id: str, text: str) -> None:
        """就地更新账号列表某行的状态列，不做全量重建。@author by ak"""
        try:
            tree = self._acc_tree
            if not tree.winfo_exists() or not tree.exists(account_id):
                return
            cur = list(tree.item(account_id, "values"))
            if not cur:
                return
            cur[6] = _clip(str(text), 24)
            tree.item(account_id, values=tuple(cur))
        except Exception:
            pass

    def _finish_login(self, account_id: str, result: dict) -> None:
        """处理单个账号登录结果（UI 线程），然后自动开始队列下一个。@author by ak"""
        self._cancel_login_tick()
        self._login_wait_until = 0
        try:
            alive = self.winfo_exists()
        except Exception:
            alive = False
        # 停止登陆后 worker 才返回：保持该账号「已取消」，不执行成功/继续队列。
        if getattr(self, "_login_cancel", False):
            self._login_status[account_id] = "已取消"
            if alive:
                self._update_row_status(account_id, "已取消")
            return
        ok = bool(result.get("ok"))
        rid = str(result.get("role_id") or "")
        rname = str(result.get("role_name") or "")
        if ok:
            label = f"{rname}({rid})" if rid else "?"
            # 记录登录编排返回的 pid，供「轮询测试选角」按账号定位选角页/世界窗口。
            login_pid = int(result.get("pid") or 0)
            try:
                am.save_account_runtime_pid(account_id, login_pid)
                if login_pid > 0:
                    am.set_pending_launch_account(account_id, login_pid)
            except Exception:
                pass
            # 选角页读回的角色卡片：写回账号槽位（角色下拉可展示真实角色）+ 状态提示。
            char_roles = result.get("char_select_roles") or {}
            roles = char_roles.get("roles") if char_roles.get("ok") else []
            roles = list(roles or [])
            if roles:
                try:
                    am.apply_char_select_roles(account_id, roles)
                except Exception as e:
                    self._log(f"账号管理: 写回选角列表失败 {account_id} {e}")
            # 进世界后：延迟注入正式面板（≥1min 稳定），注入成功后延迟开启挂机。
            if rid:
                def _create_panel_hidden(
                    p_pid: int,
                    p_hwnd: int,
                    p_role_id: str = rid,
                    p_role_name: str = rname,
                ) -> None:
                    # 注入成功后由 shell 后台创建正式面板（功能窗口），保持隐藏，
                    # 不弹到前台；游戏内 Delete / 手动打开时再显示。
                    try:
                        shell = self.master
                        if not shell or not shell.winfo_exists():
                            return
                        from app.core.session_store import GameSession
                        from app.core.window_title import get_window_title

                        h = int(p_hwnd or 0)
                        title = get_window_title(h) if h else ""
                        if not shell.store.has(int(p_pid)):
                            shell.store.mount(
                                GameSession(
                                    pid=int(p_pid),
                                    hwnd=h,
                                    title=title or f"xajh pid={p_pid}",
                                    original_title=title,
                                    bridge_note="post-login",
                                    ping_ret=None,
                                )
                            )
                        bound = shell.store.bind_injected_role(
                            int(p_pid), str(p_role_id or rid), str(p_role_name or rname)
                        )
                        if bound is None:
                            self._log(
                                f"账号管理: 注入角色绑定失败 pid={p_pid} "
                                f"role_id={p_role_id or rid}"
                            )
                            return
                        alive = getattr(shell, "_feature_win_alive", None)
                        if callable(alive) and alive(int(p_pid)) is not None:
                            # 已存在功能窗：保持现状，不重复创建、不前置。
                            return
                        creator = getattr(shell, "_create_feature_window", None)
                        if callable(creator):
                            creator(
                                int(p_pid),
                                shell.store.get(int(p_pid)),
                                bridge_ready=True,
                                start_hidden=True,
                            )
                    except Exception as e:
                        self._log(f"账号管理: 后台创建正式面板失败 pid={p_pid} {e}")

                try:
                    from app.core.login_orchestrator import schedule_post_login

                    acc_cfg = am.get_account(account_id) or {}
                    post = schedule_post_login(
                        account_id,
                        int(result.get("pid") or 0),
                        int(result.get("hwnd") or 0),
                        inject_enabled=bool(acc_cfg.get("inject", False)),
                        hang_enabled=bool(acc_cfg.get("hang_enabled", False)),
                        hang_delay_s=5.0,
                        hang_mode=1 if bool(acc_cfg.get("dungeon_hang", False)) else 0,
                        log=self._log,
                        on_inject_ready=_create_panel_hidden,
                        role_id=rid,
                        role_name=rname,
                    )
                    if post.get("note"):
                        self._log(f"账号管理: {post['note']} role={rid}")
                except Exception as e:
                    self._log(f"账号管理: 延迟注入编排失败 {e}")
            # 账号槽位全「未启用」：停留选角页，不进世界（result.stage=char_select）。
            stayed = (
                str(result.get("stage") or "")
                == "character_select"
            ) and not rid
            if stayed:
                self._login_status[account_id] = "停留选角页"
                if alive:
                    text = f"登录完成：{account_id} 已停留在选角页"
                    if roles:
                        brief = "、".join(
                            f"角色{r.get('slot')}:{r.get('name') or '空'}"
                            for r in roles
                        )
                        text += f" · 选角[{brief}]"
                    self._status.set(text)
                self._log(f"账号管理: 登录完成 {account_id} 停留选角页")
            else:
                self._login_status[account_id] = f"已进世界 {label}"
                if alive:
                    text = f"登录完成：{account_id} 已进世界 {label}"
                    if roles:
                        brief = "、".join(
                            f"角色{r.get('slot')}:{r.get('name') or '空'}"
                            for r in roles
                        )
                        text += f" · 选角[{brief}]"
                    self._status.set(text)
                self._log(f"账号管理: 登录完成 {account_id} role={rid} name={rname}")
        else:
            err = str(result.get("error") or "unknown")
            msg = str(result.get("message") or "登录失败")
            self._login_status[account_id] = f"失败:{_clip(msg, 12)}"
            if alive:
                self._status.set(f"登录失败：{account_id} {msg}")
            self._log(f"账号管理: 登录失败 {account_id} err={err} {msg}")
            if alive and self._login_batch_total <= 1:
                messagebox.showwarning("登录账号", msg, parent=self)
        if alive:
            self._update_row_status(account_id, self._login_status[account_id])
        # 队列还有账号：稍等窗口/登录器落定后继续下一个。
        if list(getattr(self, "_login_queue", None) or []):
            self.after(300, self._pump_login_queue)
        else:
            self._finish_login_batch()

    def _finish_login_batch(self) -> None:
        """整个队列处理完毕：恢复按钮 + 汇总。@author by ak"""
        self._cancel_login_tick()
        self._login_wait_until = 0
        self._login_busy = False
        self._login_current = ""
        self._login_cancel = False
        self._login_stop_event = None
        self._set_launch_busy(False)
        ok_n = sum(
            1 for v in self._login_status.values() if str(v).startswith("已进世界")
        )
        fail_n = sum(
            1 for v in self._login_status.values() if str(v).startswith("失败")
        )
        cancel_n = sum(
            1 for v in self._login_status.values() if str(v).startswith("已取消")
        )
        skip_n = sum(
            1 for v in self._login_status.values() if str(v).startswith("已登录(跳过)")
        )
        tail = ""
        if skip_n:
            tail += f" · 跳过 {skip_n}"
        if cancel_n:
            tail += f" · 取消 {cancel_n}"
        try:
            if self.winfo_exists():
                self._status.set(
                    f"统一登录结束：成功 {ok_n} · 失败 {fail_n}"
                    f"{tail} · 共 {self._login_batch_total}"
                )
                self._refresh_accounts()
        except Exception:
            pass
        self._log(
            f"账号管理: 统一登录队列结束 成功={ok_n} 失败={fail_n} "
            f"跳过={skip_n} 取消={cancel_n} 共={self._login_batch_total}"
        )

    # ------------------------------------------------ 轮询测试选角 ----------
    def _poll_test_char_select(self) -> None:
        """轮询测试：对勾选账号的选角页，按账号登录同款坐标单击角色 1/2/3，
        逐次读取 Rdo_CharN/Img_HeadN 控件字节变化，供人工分析实际选中哪张卡。
        只单击、不进世界、不画光晕。@author by ak"""
        account_id = next(iter(self._checked), None)
        if not account_id:
            self._status.set("请先勾选要测试的账号")
            return
        pid = 0
        try:
            pids = am.running_pids_for_account(account_id) or []
            pid = int(pids[0]) if pids else 0
        except Exception:
            pid = 0
        if not pid:
            self._status.set(f"{account_id} 没有运行中的游戏，请先登录到选角页")
            return
        self._status.set(f"轮询测试选角 pid={pid} …（请观察游戏画面）")
        threading.Thread(
            target=self._poll_char_select_worker, args=(int(pid),), daemon=True
        ).start()

    def _char_snapshot(self, pid: int, dlg: int) -> dict:
        """读 Rdo_CharN / Img_HeadN 控件头部字节快照。@author by ak"""
        from ctypes import c_void_p, windll

        from app.core import login_bridge as lb
        from app.core.remote_runtime import open_process, read_process

        ctrls = lb._char_list_controls(pid, dlg)
        h = open_process(pid)
        try:
            out: dict[str, bytes] = {}
            for n in (1, 2, 3):
                for tag, name in (("Rdo", f"Rdo_Char{n}"), ("Img", f"Img_Head{n}")):
                    ptr = ctrls.get(name) or 0
                    raw = read_process(h, ptr, 0x200) if ptr else b""
                    out[f"{tag}{n}"] = bytes(raw) if raw else b""
            return out
        finally:
            windll.kernel32.CloseHandle(c_void_p(h))

    @staticmethod
    def _char_diff(a: dict, b: dict) -> dict:
        """返回前后快照的字节差异（偏移 -> (旧, 新)），只保留有变化的项。@author by ak"""
        out: dict[str, list[tuple[int, int, int]]] = {}
        for k in a:
            old, new = a[k], b.get(k, b"")
            n = min(len(old), len(new))
            diffs = [(i, old[i], new[i]) for i in range(n) if old[i] != new[i]]
            if diffs:
                out[k] = diffs[:16]
        return out

    def _poll_char_select_worker(self, pid: int) -> None:
        """后台线程：按账号登录同款坐标单击 slot 1/2/3，记录控件变化。@author by ak"""
        import time as _t

        def _log(m: str) -> None:
            try:
                self._log(m)
            except Exception:
                pass

        try:
            from app.core import login_bridge as lb
            from app.core.client_coord import ClientCoordMapper
            from app.core.game_attach import GameAttachSession
            from app.core.inject_gate import find_main_hwnd_for_pid

            hwnd, _t2, _c2 = find_main_hwnd_for_pid(pid) or (0, "", "")
            if not hwnd:
                _log(f"[轮询测试] 无窗口 pid={pid}")
                return
            lb.inject_login_bridge(pid, hwnd=hwnd, log=_log)
            s = GameAttachSession(log=_log)
            s.attach(pid)
            s.hwnd = hwnd
            try:
                probe = lb.probe_login_stage(s, log=_log)
                if probe.stage is not lb.LoginStage.CHARACTER_SELECT:
                    _log(f"[轮询测试] 不在选角页 stage={probe.stage.value}")
                    return
                dlg = lb._find_win_charlist_dialog(pid)
                if not dlg:
                    _log("[轮询测试] 未找到 Win_CharList")
                    return
                roles = lb.read_char_select_roles(s, probe=probe, log=_log)
                _log("[轮询测试] 角色: " + "; ".join(
                    f"slot{r['slot']}={r['name']}({r['role_id']})"
                    for r in (roles.get("roles") or [])))
                mapper = ClientCoordMapper.for_hwnd(hwnd)
                _log(f"[轮询测试] 窗口 {mapper.to_w}x{mapper.to_h}")
                base = self._char_snapshot(pid, dlg)
                for slot in (1, 2, 3):
                    cx, cy = mapper.scale_point((501, 169))
                    cy = cy + (slot - 1) * int(141 * mapper.scale)
                    try:
                        cc = lb.char_card_center(pid, slot, log=_log)
                        if cc:
                            cx, cy = cc
                    except Exception:
                        pass
                    r = lb.ui_click(pid, cx, cy)
                    _log(f"[轮询测试] 单击 slot{slot} ({cx},{cy}) ok={r.ok}")
                    _t.sleep(3.0)
                    now = self._char_snapshot(pid, dlg)
                    diff = self._char_diff(base, now)
                    if diff:
                        _log(f"[轮询测试] slot{slot} 点击后控件变化: "
                             + "; ".join(f"{k}@{off} {old}->{new}" for k, ds in diff.items()
                                         for off, old, new in ds))
                    else:
                        _log(f"[轮询测试] slot{slot} 点击后控件无字节变化")
                    base = now
                _log("[轮询测试] 完成：请对照游戏画面看每次单击实际选中了哪张卡")
            finally:
                s.close()
        except Exception as e:
            _log(f"[轮询测试] 失败: {e}")

    def _cancel_login_tick(self) -> None:
        """Cancel the one-click login countdown after job, if any. @author by ak"""
        job = getattr(self, "_login_tick_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
            self._login_tick_job = None

    def destroy(self) -> None:
        """Cancel login timers before tearing down the window. @author by ak"""
        self._cancel_login_tick()
        ev = getattr(self, "_login_stop_event", None)
        if ev is not None:
            try:
                ev.set()
            except Exception:
                pass
        self._login_wait_until = 0
        self._login_busy = False
        self._login_cancel = False
        self._login_stop_event = None
        for a in list(self._login_status.keys()):
            try:
                am.clear_account_runtime_pid(a)
            except Exception:
                pass
        self._login_queue = []
        self._login_status = {}
        self._login_current = ""
        try:
            super().destroy()
        except Exception:
            pass

    def _finish_close_selected(self, ok: bool, msg: str) -> None:
        self._status.set(f"关闭游戏: {'成功' if ok else '失败'} · {msg}")
        self.after(400, self.refresh_all)


class AccountDialog(tk.Toplevel):
    """
    Modal add/edit account dialog: 账号 / 密码 / 角色(下拉) / 账号属性勾选.

    @author by ak
    """

    def __init__(
        self,
        master,
        *,
        account_id: str = "",
        password: str = "",
        slots: list[dict] | None = None,
        inject: bool = False,
        control: str = "",
        dungeon_hang: bool = False,
        hang_enabled: bool = False,
    ):
        super().__init__(master)
        self.title("账号" if account_id else "添加账号")
        self.geometry("440x360")
        self.resizable(False, False)
        self.result: dict | None = None
        self.transient(master)
        apply_theme(self)

        body = ttk.Frame(self, style="Content.TFrame", padding=(14, 12))
        body.pack(fill=tk.BOTH, expand=True)

        row = ttk.Frame(body, style="Content.TFrame")
        row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row, text="账号", width=8).pack(side=tk.LEFT)
        self._var_id = tk.StringVar(value=account_id)
        ttk.Entry(row, textvariable=self._var_id).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )

        row = ttk.Frame(body, style="Content.TFrame")
        row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row, text="密码", width=8).pack(side=tk.LEFT)
        self._var_pw = tk.StringVar(value=password)
        self._ent_pw = ttk.Entry(row, textvariable=self._var_pw, show="*")
        self._ent_pw.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 角色：下拉框（对应选角页角色位置），只到一个；登录后写入所选角色。
        row = ttk.Frame(body, style="Content.TFrame")
        row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row, text="角色", width=8).pack(side=tk.LEFT)
        self._orig_slots: list[dict] = am._normalize_slots(slots)
        primary = self._orig_slots[0] if self._orig_slots else {}
        try:
            ri0 = int(primary.get("role_index") or am.ROLE_INDEX_NONE)
        except Exception:
            ri0 = am.ROLE_INDEX_NONE
        # 角色下拉：展示已绑定角色的真实名称/ID（写回自选角页）。
        # 未启用（role_index=0）但已绑定的角色，也按槽位位置展示其名/ID。
        self._ridx_labels: list[str] = []
        for k in am.ROLE_INDEX_CHOICES:
            if k == am.ROLE_INDEX_NONE:
                self._ridx_labels.append("未启用")
                continue
            hit = None
            for s in self._orig_slots:
                try:
                    if int(s.get("role_index") or am.ROLE_INDEX_NONE) == k:
                        hit = s
                        break
                except Exception:
                    continue
            if hit is None and 1 <= k <= len(self._orig_slots):
                # 槽位位置 k-1 对应角色 k：未启用时仍展示其绑定角色名/ID。
                hit = self._orig_slots[k - 1]
            rid = str((hit or {}).get("role_id") or "").strip()
            nm = str((hit or {}).get("name") or "").strip()
            if rid and nm:
                self._ridx_labels.append(f"角色{k}·{nm}({rid})")
            elif rid:
                self._ridx_labels.append(f"角色{k}·({rid})")
            else:
                self._ridx_labels.append(f"角色{k}")
        self._label_to_ridx = {
            lab: self._label_index(lab) for lab in self._ridx_labels
        }
        default_label = self._ridx_labels[0]
        for lab in self._ridx_labels:
            if self._label_to_ridx.get(lab) == ri0:
                default_label = lab
                break
        self._var_ri = tk.StringVar(value=default_label)
        ttk.Combobox(
            row,
            textvariable=self._var_ri,
            state="readonly",
            width=12,
            values=self._ridx_labels,
        ).pack(side=tk.LEFT)

        # 账号属性仅保留：注入脚本 / 挂机模式。
        row = ttk.Frame(body, style="Content.TFrame")
        row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row, text="属性", width=8).pack(side=tk.LEFT)
        self._var_inject = tk.BooleanVar(value=bool(inject))
        ttk.Checkbutton(
            row, text="注入脚本", variable=self._var_inject,
            command=self._sync_hang_mode_state,
        ).pack(side=tk.LEFT, padx=(0, 12))
        self._hang_mode_frame = ttk.Frame(body, style="Content.TFrame")
        self._hang_mode_frame.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(self._hang_mode_frame, text="模式", width=8).pack(side=tk.LEFT)
        mode_enabled = bool(inject and hang_enabled)
        self._var_dungeon_hang = tk.BooleanVar(
            value=bool(mode_enabled and dungeon_hang)
        )
        self._var_normal_hang = tk.BooleanVar(
            value=bool(mode_enabled and not dungeon_hang)
        )
        ttk.Checkbutton(
            self._hang_mode_frame, text="开启副本挂机", variable=self._var_dungeon_hang,
            command=self._on_dungeon_hang,
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Checkbutton(
            self._hang_mode_frame, text="开启普通挂机", variable=self._var_normal_hang,
            command=self._on_normal_hang,
        ).pack(side=tk.LEFT)
        self._sync_hang_mode_state()

        ttk.Label(
            body,
            text=(
                "注入脚本：登录完成后等待15秒自动注入正式脚本；"
                "选择挂机模式后，注入成功再等待5秒启动。"
            ),
            style="Muted.TLabel",
            wraplength=400,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))
        btns = ttk.Frame(self, style="Content.TFrame", padding=(14, 0, 14, 12))
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btns, text="保存", style="Accent.TButton", width=10, command=self._save).pack(
            side=tk.RIGHT, padx=(8, 0)
        )
        ttk.Button(btns, text="取消", width=10, command=self.destroy).pack(side=tk.RIGHT)

        try:
            self.grab_set()
        except Exception:
            pass
        try:
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _sync_hang_mode_state(self) -> None:
        enabled = bool(self._var_inject.get())
        if enabled:
            if not self._hang_mode_frame.winfo_ismapped():
                self._hang_mode_frame.pack(fill=tk.X, pady=(0, 4))
        else:
            self._hang_mode_frame.pack_forget()
            self._var_dungeon_hang.set(False)
            self._var_normal_hang.set(False)

    def _on_dungeon_hang(self) -> None:
        if self._var_dungeon_hang.get():
            self._var_normal_hang.set(False)

    def _on_normal_hang(self) -> None:
        if self._var_normal_hang.get():
            self._var_dungeon_hang.set(False)

    @staticmethod
    def _label_index(label: str) -> int:
        """Parse role_index from a dropdown label ('未启用'/'角色N'/'角色N·名(id)'). @author by ak"""
        lab = str(label or "").strip()
        if lab == "未启用" or lab.startswith("未启用"):
            return am.ROLE_INDEX_NONE
        for k in am.ROLE_INDEX_ACTIVE:
            if lab == f"角色{k}" or lab.startswith(f"角色{k}·"):
                return k
        return am.ROLE_INDEX_NONE

    def _save(self) -> None:
        account_id = str(self._var_id.get() or "").strip()
        if not account_id:
            messagebox.showwarning("账号", "账号不能为空", parent=self)
            return
        ri = self._label_to_ridx.get(str(self._var_ri.get()), am.ROLE_INDEX_NONE)
        # 选中槽位：取该 role_index 对应的原槽（保留已绑定的真实角色/名）。
        orig = {}
        for s in self._orig_slots:
            try:
                if int(s.get("role_index") or am.ROLE_INDEX_NONE) == ri:
                    orig = s
                    break
            except Exception:
                continue
        orig_rid = str(orig.get("role_id") or "").strip()
        if ri in am.ROLE_INDEX_ACTIVE:
            # keep an already-bound real role (written after login), else empty
            role_id = (
                orig_rid
                if orig_rid and orig_rid != am.SPECIAL_ROLE_CHAR_SELECT
                else ""
            )
            name = str(orig.get("name") or "").strip() if role_id else ""
        else:
            role_id = ""
            name = ""
        if ri not in am.ROLE_INDEX_ACTIVE:
            # 未启用：所有槽都置 role_index=0（不自动选角/注入），
            # 但保留各槽已绑定的 role_id/name 供 PID/在线匹配用。
            slots = []
            for extra in self._orig_slots:
                slots.append(
                    {
                        "role_index": am.ROLE_INDEX_NONE,
                        "role_id": str(extra.get("role_id") or "").strip(),
                        "name": str(extra.get("name") or "").strip(),
                    }
                )
            while len(slots) < am.ACCOUNT_SLOT_COUNT:
                slots.append(
                    {
                        "role_index": am.ROLE_INDEX_NONE,
                        "role_id": "",
                        "name": "",
                    }
                )
            if len(slots) > am.ACCOUNT_SLOT_COUNT:
                slots = slots[: am.ACCOUNT_SLOT_COUNT]
        else:
            # 构建 3 槽：选中槽更新为 ri；其余槽保留原有绑定（role_id/name），
            # 避免编辑改选角色后把已登录绑定的角色丢弃导致 PID/在线匹配失效。
            slots = [
                {
                    "role_index": int(ri),
                    "role_id": role_id,
                    "name": name,
                }
            ]
            consumed = False
            for extra in self._orig_slots:
                try:
                    extra_ri = int(extra.get("role_index") or am.ROLE_INDEX_NONE)
                except Exception:
                    extra_ri = am.ROLE_INDEX_NONE
                if extra_ri == ri and not consumed:
                    consumed = True  # 选中槽已由 slots[0] 表达，跳过原槽
                    continue
                slots.append(
                    {
                        "role_index": int(extra_ri),
                        "role_id": str(extra.get("role_id") or "").strip(),
                        "name": str(extra.get("name") or "").strip(),
                    }
                )
            while len(slots) < am.ACCOUNT_SLOT_COUNT:
                slots.append(
                    {
                        "role_index": am.ROLE_INDEX_NONE,
                        "role_id": "",
                        "name": "",
                    }
                )
            if len(slots) > am.ACCOUNT_SLOT_COUNT:
                slots = slots[: am.ACCOUNT_SLOT_COUNT]
        self.result = {
            "account_id": account_id,
            "password": str(self._var_pw.get() or ""),
            "inject": bool(self._var_inject.get()),
            "hang_enabled": bool(
                self._var_inject.get()
                and (self._var_dungeon_hang.get() or self._var_normal_hang.get())
            ),
            "dungeon_hang": bool(
                self._var_inject.get() and self._var_dungeon_hang.get()
            ),
            "slots": slots,
        }
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
