# -*- coding: utf-8 -*-
"""私聊控制面 —— 组队前的跨设备预检查/离队链路。

背景（2026-08-29 会话定案）：自动整队前需要通知所有副控"在队就离队"，
但跨设备时群控（本机 IPC）不通、云控未配置、队内控依赖已组队 —— 全部不可用。
私聊（0x60 封包，channel=9）是唯一不依赖队伍关系的控制面，本模块在其上实现：

  主控 → 副控:  [主P]PLEAVE@<msg_id>        （在队则离队）
  副控 → 主控:  [副G]PLEFT@<msg_id> OK=<1|0> （回执；OK=1 已离队/本来就没队）

消息文本借道游戏聊天显示层（AddChatMessage），因此带色码渲染前缀
（如 ``^u&#     44385十丶三&^u 对你说：…``）；发送方名字用花名册匹配校验，
避免解析渲染噪声。接收优先读 v3 DLL 的私聊专用 ring，回退主 ring 过滤 ch=9。

@author by ak
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable

from app.core.chat_tap import ChatTapReader
from app.core.team_chat import (
    MASTER_TAG,
    SLAVE_TAG,
    make_msg_id,
    send_private_message,
)

LogFn = Callable[[str], None]

CMD_PLEAVE = "PLEAVE"
CMD_PLEFT = "PLEFT"

# 主控发完 PLEAVE 后的固定离队缓冲：副控收命令→执行离队需要几秒，
# 生产主控只管发不等回执，靠这段固定等待给离队留时间。@author by ak
PRIVATE_LEAVE_SETTLE_S = 5.0

# PLEAVE 时效：超过该秒数的命令视为旧消息丢弃（重启/重连重放防护）。
# 副控轮询周期 1.5s + 发送限频窗口，正常到达不会超过此值。@author by ak
PRIVATE_LEAVE_STALE_S = 10.0

_MSG_ID_RE = r"[0-9A-Za-z]{4,16}"
# 副控回执：OK=1（已离队/本无队） / OK=0（离队失败）
_PLEFT_RE = re.compile(
    rf"^{re.escape(SLAVE_TAG)}{CMD_PLEFT}@({_MSG_ID_RE})(?:\s+OK=(\d))?\s*$"
)
_PLEAVE_RE = re.compile(
    rf"^{re.escape(MASTER_TAG)}{CMD_PLEAVE}@({_MSG_ID_RE})\s*$"
)
# 显示层发送者名：…<markup>对你说：BODY（接收）/ 你对<markup> 说：BODY（本机回显）
_BODY_SPLIT_RE = re.compile(r"说：")
_MARKUP_RE = re.compile(r"\^u&#.*?&\^u", re.S)


def build_master_pleave(msg_id: str | None = None) -> str:
    """主控私聊命令：在队则离队。@author by ak"""
    return f"{MASTER_TAG}{CMD_PLEAVE}@{msg_id or make_msg_id()}"


def build_slave_pleft(msg_id: str, *, ok: bool = True) -> str:
    """副控私聊回执：离队完成。@author by ak"""
    return f"{SLAVE_TAG}{CMD_PLEFT}@{msg_id} OK={1 if ok else 0}"


def split_private_display(text: str) -> tuple[str, str]:
    """把显示层私聊文本拆成 (发送者原文, 正文)。

    接收格式 ``X 对你说：BODY``；本机回显 ``你对 X 说：BODY``（X 含色码）。
    统一按第一个 ``说：`` 切分；控制面正文是本模块协议串，不含该词。
    无法识别时返回 ("", 原文)。
    @author by ak
    """
    parts = _BODY_SPLIT_RE.split(str(text or ""), maxsplit=1)
    if len(parts) != 2:
        return "", str(text or "")
    return parts[0].strip(), parts[1].strip()


def match_known_sender(raw_sender: str, known_names: list[str]) -> str:
    """从带渲染噪声的发送者原文里匹配已知角色名。

    显示层名字带 ``^u&#<噪声>名字&^u`` 包装，噪声含数字，无法无先验切分；
    控制面收发双方都在花名册里，直接按已知名匹配最可靠。
    未匹配到已知名时返回 ""（调用方丢弃，避免来历不明的指令）。
    @author by ak
    """
    raw = str(raw_sender or "")
    for name in sorted((str(n) for n in known_names if n), key=len, reverse=True):
        if name and name in raw:
            return name
    return ""


def parse_slave_pleft(text: str, known_names: list[str]) -> dict | None:
    """解析副控 PLEFT 回执 → {sender, msg_id, ok}；非回执返回 None。@author by ak"""
    raw_sender, body = split_private_display(text)
    m = _PLEFT_RE.match(body.strip())
    if not m:
        return None
    sender = match_known_sender(raw_sender, known_names)
    return {
        "sender": sender,
        "msg_id": m.group(1),
        "ok": (m.group(2) or "1") == "1",
        "raw": str(text or ""),
    }


def parse_master_pleave(text: str, known_names: list[str]) -> dict | None:
    """解析主控 PLEAVE 命令 → {sender, msg_id}；非命令返回 None。@author by ak"""
    raw_sender, body = split_private_display(text)
    m = _PLEAVE_RE.match(body.strip())
    if not m:
        return None
    sender = match_known_sender(raw_sender, known_names)
    return {
        "sender": sender,
        "msg_id": m.group(1),
        "raw": str(text or ""),
    }


class PrivateChatWatch:
    """一个 pid 的私聊接收游标（优先 v3 私聊 ring，回退主 ring 过滤 ch=9）。

    v2 DLL（旧尺寸映射）打开失败时 reader=None，poll 返回空 —— 部署 v3 后
    自动切到专用 ring，调用方无需感知。
    @author by ak
    """

    def __init__(self, pid: int) -> None:
        self.pid = int(pid or 0)
        self._reader = ChatTapReader.open(self.pid) if self.pid else None
        self._private_cursor: int | None = None
        self._main_cursor: int | None = None
        if self._reader is None:
            return
        # 首次挂上游标直接跳到当前最新：启动/重连不重放历史私聊
        # （否则会把重启前遗留的 PLEAVE 全部执行一遍）。@author by ak
        self._sync_cursor_to_latest()

    def _sync_cursor_to_latest(self) -> None:
        """把游标对齐到当前最新（私聊 ring 优先，失败回退主 ring）。"""
        self._private_cursor = None
        self._main_cursor = None
        if self._reader is None:
            return
        try:
            self._private_cursor = self._reader.latest_private_cursor()
        except Exception:
            self._private_cursor = None

    @property
    def ok(self) -> bool:
        return self._reader is not None

    def poll_messages(self) -> list[dict]:
        """返回自上次 poll 以来的私聊消息 [{text, channel, tick_ms}]。@author by ak"""
        if self._reader is None:
            return []
        try:
            if self._private_cursor is not None:
                events, consumed, _lost, _h = self._reader.read_private_after(
                    self._private_cursor
                )
                self._private_cursor = consumed
                return [
                    {
                        "text": e.get("text") or "",
                        "channel": e.get("channel"),
                        "tick_ms": e.get("tick_ms"),
                    }
                    for e in events
                ]
            if self._main_cursor is None:
                self._main_cursor = self._reader.latest_cursor
            events, consumed, _lost, _h = self._reader.read_after(self._main_cursor)
            self._main_cursor = consumed
            return [
                {
                    "text": e.get("text") or "",
                    "channel": e.get("channel"),
                    "tick_ms": e.get("tick_ms"),
                }
                for e in events
                if int(e.get("channel") or 0) == 9
            ]
        except Exception:
            # 映射消失（游戏退出/换号）时重开一次，游标重新对齐最新（不重放）。
            try:
                self._reader.close()
            except Exception:
                pass
            self._reader = ChatTapReader.open(self.pid)
            self._sync_cursor_to_latest()
            return []

    def close(self) -> None:
        try:
            if self._reader is not None:
                self._reader.close()
        except Exception:
            pass
        self._reader = None


def notify_slaves_leave(
    pid: int,
    targets: list[dict],
    *,
    log: LogFn | None = None,
) -> int:
    """主控侧（生产路径）：对名单逐个私聊 PLEAVE —— 只管发，不等回执。

    副控收到就执行离队；没收到、没离队都不阻断（后续邀请会兜底暴露）。
    调用方在发送后自行固定等待 PRIVATE_LEAVE_SETTLE_S 作为离队缓冲。
    返回成功发出的条数。@author by ak
    """
    log = log or (lambda _m: None)
    sent = 0
    for t in targets or []:
        name = str((t or {}).get("name") or "").strip()
        rid = int((t or {}).get("obj_id") or 0)
        if not name or not rid:
            continue
        res = send_private_message(pid, rid, name, build_master_pleave(), log=log)
        if res.get("ok"):
            sent += 1
    log(f"私聊 [预检查] PLEAVE 已发 {sent} 条（不等待回执）")
    return sent


def request_slaves_leave(
    pid: int,
    targets: list[dict],
    *,
    timeout_s: float = 6.0,
    log: LogFn | None = None,
) -> dict[str, dict]:
    """主控侧（测试/诊断用）：发送 PLEAVE 并等待回执。

    targets: [{name, obj_id(rid)}]（team_verified_roster 条目即可，跨设备下
    本机没有副控 pid，全靠名字+rid 走服务端投递）。
    返回 {name: {sent, replied, ok}}；未回执的项 replied=False（主控按
    "离队可能未生效" 记日志后继续，后续邀请失败会兜底）。
    生产整队走 notify_slaves_leave（只管发+固定缓冲）；本函数保留给
    tools/test_private_e2e.py 等需要确认回执的场景。@author by ak
    """
    log = log or (lambda _m: None)
    result: dict[str, dict] = {}
    pending: dict[str, tuple[str, float]] = {}
    watch = PrivateChatWatch(pid)
    try:
        for t in targets or []:
            name = str((t or {}).get("name") or "").strip()
            rid = int((t or {}).get("obj_id") or 0)
            if not name or not rid:
                continue
            msg_id = make_msg_id()
            res = send_private_message(
                pid, rid, name, build_master_pleave(msg_id), log=log
            )
            result[name] = {
                "sent": bool(res.get("ok")),
                "replied": False,
                "ok": False,
            }
            if res.get("ok"):
                pending[name] = (msg_id, time.monotonic())
        if not pending:
            return result

        deadline = time.monotonic() + max(1.0, float(timeout_s))
        names = list(pending)
        while pending and time.monotonic() < deadline:
            for msg in watch.poll_messages():
                p = parse_slave_pleft(msg.get("text") or "", names)
                if not p or p["msg_id"] != pending.get(p["sender"], ("",))[0]:
                    continue
                result[p["sender"]]["replied"] = True
                result[p["sender"]]["ok"] = bool(p["ok"])
                pending.pop(p["sender"], None)
                log(
                    f"私聊 [预检查] {p['sender']} 离队回执 ok={p['ok']}"
                )
            if pending:
                time.sleep(0.25)
    finally:
        watch.close()
    for name, (_mid, t0) in pending.items():
        result[name]["replied"] = False
        log(
            f"私聊 [预检查] {name} 回执超时（{time.monotonic() - t0:.1f}s）"
            f"——可能不在线或版本过旧，继续整队流程"
        )
    return result


def handle_pleave_commands(
    pid: int,
    session,
    *,
    watch: PrivateChatWatch,
    roster: list[dict],
    seen: set[str] | None = None,
    leave_fn=None,
    reply: bool = True,
    log: LogFn | None = None,
) -> int:
    """副控侧单次轮询：处理主控 PLEAVE —— 在队则离队。

    reply=True（默认）回 PLEFT 给命令来源（测试/诊断路径用）；生产副控
    主控不收回执，传 reply=False 免去私聊回执噪声。
    roster: 花名册（{name, obj_id}），用于校验命令来源并取得回执目标的
    rid —— 跨设备下副控只知道名单，回执按"谁发来的就回给谁"。
    返回本次处理的命令数。leave_fn 缺省用 team_ops.leave_team。
    @author by ak
    """
    log = log or (lambda _m: None)
    seen = seen if seen is not None else set()
    if leave_fn is None:
        from app.core.team_ops import leave_team as leave_fn

    roster_by_name = {
        str((r or {}).get("name") or "").strip(): int((r or {}).get("obj_id") or 0)
        for r in (roster or [])
        if (r or {}).get("name")
    }
    known = list(roster_by_name)
    handled = 0
    now_tick: int | None = None
    for msg in watch.poll_messages():
        # 时效防护：旧 PLEAVE（重启重放/游标异常）不执行。
        # 32 位 GetTickCount 回绕安全。游戏进程 tick 与本机 helper 同 boot 基准。
        tick = int(msg.get("tick_ms") or 0)
        if tick:
            if now_tick is None:
                from app.core.team_chat import _now_tick_ms

                now_tick = _now_tick_ms()
            if now_tick and ((now_tick - tick) & 0xFFFFFFFF) > int(
                PRIVATE_LEAVE_STALE_S * 1000
            ):
                continue
        p = parse_master_pleave(msg.get("text") or "", known)
        if not p or not p["sender"]:
            continue
        master_rid = roster_by_name.get(p["sender"]) or 0
        if not master_rid:
            continue
        if p["msg_id"] in seen:
            continue
        seen.add(p["msg_id"])
        if len(seen) > 64:
            seen.difference_update(sorted(seen)[:-32])
        handled += 1
        ok = False
        try:
            res = leave_fn(session, log=log)
            ok = bool(getattr(res, "ok", True))
        except Exception as e:
            log(f"私聊 [离队] 执行失败: {e}")
        if reply:
            reply_text = build_slave_pleft(p["msg_id"], ok=ok)
            send_private_message(int(pid), master_rid, p["sender"], reply_text, log=log)
        log(f"私聊 [离队] 主控={p['sender']} msg_id={p['msg_id']} 离队={ok}")
    return handled
