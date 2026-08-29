# -*- coding: utf-8 -*-
"""
队内控 (in-team control) — 通过游戏队伍频道文本消息完成主/副控同步。

与云控(HTTP)/本机群控(进程内 hub)并列的新增传输方式，勾选与云控互斥：

  主控 -> 队伍频道 : [主P]接任务:1111,222     （多任务ID 用英文逗号分隔）
  主控 -> 队伍频道 : [主P]PING                （探测副控是否受控）
  副控 -> 队伍频道 : [副G]接pong              （执行完对应动作后回执）
  副控 -> 队伍频道 : [副G]PONG                （收到 PING 后的回执）

读取：注入的被动 AddChatMessage tap（xajh_chat_tap.dll）过滤队伍频道文本前缀。
发送：构造 Octets 对象 + 明文，远程调用游戏聊天加密包装 0x00D089B0
      （thiscall：this=发送管理器, arg=Octets）。2026-08-15 实机验证可重复发送；
      CD1740 经实测不是聊天发送入口（会套 GNET 壳导致服务端不认）。

@author by ak
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from typing import Callable

from app.core.task_sync import (
    ACTION_ACCEPT,
    ACTION_CLAIM_ACTIVITY,
    ACTION_COMPLETE,
    ACTION_HANG_SYNC,
    ACTION_JIANGLONG_CAST,
    ACTION_ACCEPT_DAILY_TASKS,
    ACTION_DAILY_ROUTE,
    ACTION_MAP_FLY,
    ACTION_PATH,
    ACTION_TEAM_FOLLOW,
    ACTION_DAILY_FOLLOW,
    ACTION_TEAM_LEAVE,
    ROLE_MASTER,
    ROLE_NONE,
    ROLE_SLAVE,
)

LogFn = Callable[[str], None]

TEAM_CHANNEL_ID = 3  # 队伍 channel_id（chat_history._CHANNEL_ID_MAP）

MASTER_TAG = "[主P]"
SLAVE_TAG = "[副G]"
CMD_PING = "PING"
CMD_PONG = "PONG"
CMD_DONE = "DONE"
CMD_FAIL = "FAIL"
CMD_JIANGLONG_QUERY = "降龙查询"
CMD_JIANGLONG_STATUS = "降status"

# 主控命令动词 → action。
ACTION_TEXT: dict[str, str] = {
    ACTION_ACCEPT: "接任务",
    ACTION_COMPLETE: "交任务",
    ACTION_PATH: "寻路",
    ACTION_CLAIM_ACTIVITY: "领活跃",
    ACTION_MAP_FLY: "飞图",
    ACTION_TEAM_FOLLOW: "跟随",
    ACTION_DAILY_FOLLOW: "日随",
    ACTION_TEAM_LEAVE: "离队",
    ACTION_HANG_SYNC: "挂机",
    ACTION_JIANGLONG_CAST: "降龙",
    ACTION_ACCEPT_DAILY_TASKS: "接日常",
    ACTION_DAILY_ROUTE: "日路",
}
TEXT_ACTION: dict[str, str] = {v: k for k, v in ACTION_TEXT.items()}

# 副控回执短动词 → action（回执 = <短动词>pong）。
ACK_VERB: dict[str, str] = {
    "接": ACTION_ACCEPT,
    "交": ACTION_COMPLETE,
    "寻": ACTION_PATH,
    "领": ACTION_CLAIM_ACTIVITY,
    "飞": ACTION_MAP_FLY,
    "跟": ACTION_TEAM_FOLLOW,
    "日随": ACTION_DAILY_FOLLOW,
    "离": ACTION_TEAM_LEAVE,
    "挂": ACTION_HANG_SYNC,
    "降": ACTION_JIANGLONG_CAST,
    "日": ACTION_ACCEPT_DAILY_TASKS,
    "日路": ACTION_DAILY_ROUTE,
}
ACK_VERB_REV: dict[str, str] = {v: k for k, v in ACK_VERB.items()}


# ---------------------------------------------------------------------------
# 设置判定
# ---------------------------------------------------------------------------

def team_control_flag_enabled(settings: dict | None) -> bool:
    """True when UI/settings checkbox says 队内控 on. @author by ak"""
    s = settings if isinstance(settings, dict) else {}
    value = s.get("team_control_enabled")
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value is True or value == 1


def is_team_control_ready(settings: dict | None) -> bool:
    """True when this window is configured for 队内控 (enabled + master/slave)."""
    s = settings if isinstance(settings, dict) else {}
    if not team_control_flag_enabled(s):
        return False
    role = str(s.get("task_control_role") or ROLE_NONE).strip().lower()
    return role in (ROLE_MASTER, ROLE_SLAVE)


def is_team_slave_isolated(settings: dict | None) -> bool:
    """队内控副控：忽略本机 hub 事件，只执行队伍频道里 [主P] 的命令。"""
    s = settings if isinstance(settings, dict) else {}
    role = str(s.get("task_control_role") or ROLE_NONE).strip().lower()
    return role == ROLE_SLAVE and is_team_control_ready(s)


# ---------------------------------------------------------------------------
# 协议编解码
# ---------------------------------------------------------------------------

# 消息一次识别 ID：追加在命令末尾 @MMddHHmmss（10 位：月日时分秒）。
# 同一条命令在游标重置/ring 重读/回声重放时只消费一次（TeamMsgSeen）。
MSG_ID_RE = re.compile(r"@(\d{10})$")


def make_msg_id() -> str:
    """生成 10 位消息 ID：MMddHHmmss（月日时分秒）。@author by ak"""
    return time.strftime("%m%d%H%M%S", time.localtime(time.time()))


def _with_msg_id(text: str, msg_id: str | None) -> str:
    """正文尾部附加 @msg_id（未给则自动生成）。@author by ak"""
    mid = str(msg_id or "").strip()
    if not mid:
        mid = make_msg_id()
    return f"{text}@{mid}"


def split_msg_id(body: str) -> tuple[str, str]:
    """从命令体尾部提取一次识别 ID（@MMddHHmmss）。返回 (剩余body, msg_id 或 '')。@author by ak"""
    b = str(body or "").strip()
    m = MSG_ID_RE.search(b)
    if m:
        return b[: m.start()].strip(), m.group(1)
    return b, ""


def action_to_text(action: str) -> str:
    """action → 主控命令动词，未知动作回原串。@author by ak"""
    return ACTION_TEXT.get(str(action or "").strip().lower(), str(action or ""))


def text_to_action(text: str) -> str:
    """主控命令动词 → action；未识别返回空串。@author by ak"""
    return TEXT_ACTION.get(str(text or "").strip(), "")


def _to_base36(value: int) -> str:
    value = int(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = digits[rem] + out
    return sign + out


def _from_base36(value: str) -> int:
    text = str(value or "").strip().lower()
    sign = -1 if text.startswith("-") else 1
    if sign < 0:
        text = text[1:]
    return sign * int(text or "0", 36)


def _route_number(value, *, scale: int = 10) -> str:
    return _to_base36(round(float(value) * scale))


def _route_float(value: str, *, scale: int = 10) -> float:
    return _from_base36(value) / float(scale)


def build_master_command(
    action: str,
    task_ids: list[int] | int,
    *,
    extra: str | None = None,
    route_snapshot: dict | None = None,
    msg_id: str | None = None,
) -> str:
    """主控命令：[主P]<动词>[:<ids>][:<extra>]@<msg_id>。

    ids 英文逗号分隔；extra 为动作附带参数（如飞图的自定义槽位 slot:p:s:label）。
    msg_id 默认自动生成（10 位时间戳），用于一次识别去重。
    @author by ak
    """
    verb = action_to_text(action)
    if isinstance(task_ids, (int,)):
        task_ids = [int(task_ids)]
    ids_s = ",".join(str(int(i)) for i in task_ids) if task_ids else ""
    base = f"{MASTER_TAG}{verb}"
    if ids_s:
        base = f"{base}:{ids_s}"
    if extra:
        base = f"{base}:{extra}"
    if route_snapshot:
        if str(action or "").strip().lower() == ACTION_PATH:
            route_kind = str(route_snapshot.get("portal_kind") or "").strip().lower()
            if route_kind in ("dungeon_upper", "dungeon_deep", "dungeon_lower"):
                layer_code = "u" if route_kind == "dungeon_upper" else "d"
                compact = f"route=2_p_{layer_code}"
                if len(f"{base}:{compact}@0000000000".encode("utf-8")) <= 255:
                    return _with_msg_id(f"{base}:{compact}", msg_id)
        if str(action or "").strip().lower() == ACTION_DAILY_ROUTE:
            phase_codes = {"portal_arrive": "pa", "portal": "p", "target": "t"}
            phase = phase_codes.get(str(route_snapshot.get("phase") or ""), "")
            if phase in ("p", "pa"):
                layer_code = {"dungeon_upper": "u", "dungeon_deep": "d", "dungeon_lower": "d"}.get(
                    str(route_snapshot.get("portal_kind") or "").strip().lower(), ""
                )
                if phase == "p" and layer_code:
                    compact = f"route=2_p_{layer_code}"
                    if len(f"{base}:{compact}@0000000000".encode("utf-8")) <= 255:
                        return _with_msg_id(f"{base}:{compact}", msg_id)
                values = [
                    "2",
                    phase,
                    layer_code or "-",
                    _to_base36(int(route_snapshot.get("portal_tid") or 0)),
                    _to_base36(int(route_snapshot.get("portal_obj_id") or 0)),
                    _route_number(route_snapshot.get("portal_x") or 0),
                    _route_number(route_snapshot.get("portal_y") or 0),
                    _route_number(route_snapshot.get("portal_z") or 0),
                    _to_base36(int(route_snapshot.get("target_scene_id") or 0)),
                ]
                compact = "route=" + ",".join(values)
                if len(f"{base}:{compact}@0000000000".encode("utf-8")) <= 255:
                    return _with_msg_id(f"{base}:{compact}", msg_id)
            elif phase == "t":
                values = [
                    "2",
                    phase,
                    _to_base36(int(route_snapshot.get("target_tid") or 0)),
                    _route_number(route_snapshot.get("target_x") or 0),
                    _route_number(route_snapshot.get("target_y") or 0),
                    _route_number(route_snapshot.get("target_z") or 0),
                    _to_base36(int(route_snapshot.get("target_scene_id") or 0)),
                ]
                compact = "route=" + ",".join(values)
                if len(f"{base}:{compact}@0000000000".encode("utf-8")) <= 255:
                    return _with_msg_id(f"{base}:{compact}", msg_id)
            compact_keys = (
                ("phase", "p", phase_codes),
                ("origin_scene_id", "s", None),
                ("target_scene_id", "d", None),
                ("portal_tid", "pt", None),
                ("portal_obj_id", "po", None),
                ("portal_x", "px", None),
                ("portal_y", "py", None),
                ("portal_z", "pz", None),
                ("target_tid", "tt", None),
                ("target_x", "tx", None),
                ("target_y", "ty", None),
                ("target_z", "tz", None),
            )
            fields = []
            for source, key, mapping in compact_keys:
                value = route_snapshot.get(source)
                if value is None:
                    continue
                if mapping:
                    value = mapping.get(str(value), str(value))
                fields.append(f"{key}={value}")
            compact = "route=" + "|".join(fields)
            if len(f"{base}:{compact}@0000000000".encode("utf-8")) <= 255:
                return _with_msg_id(f"{base}:{compact}", msg_id)
            phase = phase_codes.get(str(route_snapshot.get("phase") or ""), "")
            fallback = f"route=i|p={phase}" if phase else "route=i"
            return _with_msg_id(f"{base}:{fallback}", msg_id)
        payload = {
            key: route_snapshot.get(key)
            for key in (
                "portal_x",
                "portal_y",
                "portal_z",
                "origin_scene_id",
                "portal_tid",
                "portal_obj_id",
                "target_scene_id",
                "target_tid",
                "target_x",
                "target_y",
                "target_z",
                "target_round",
                "phase",
            )
            if route_snapshot.get(key) is not None
        }
        if payload:
            encoded = base64.urlsafe_b64encode(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            ).decode().rstrip("=")
            base = f"{base}:route={encoded}"
    return _with_msg_id(base, msg_id)


def build_master_ping(*, msg_id: str | None = None) -> str:
    """主控探测：[主P]PING@<msg_id>。@author by ak"""
    return _with_msg_id(f"{MASTER_TAG}{CMD_PING}", msg_id)


def build_master_jianglong_query(
    *, test_mode: bool = False, msg_id: str | None = None
) -> str:
    """查询队内副控的降龙参与状态。"""
    body = f"{MASTER_TAG}{CMD_JIANGLONG_QUERY}"
    if test_mode:
        body += ":1"
    return _with_msg_id(body, msg_id)


def build_slave_jianglong_status(
    enabled: bool,
    in_wuzun: bool,
    hang_running: bool,
    *,
    msg_id: str | None = None,
) -> str:
    """回报副控降龙状态，不触发施法。"""
    body = (
        f"{SLAVE_TAG}{CMD_JIANGLONG_STATUS}:"
        f"{int(bool(enabled))}:{int(bool(in_wuzun))}:{int(bool(hang_running))}"
    )
    return _with_msg_id(body, msg_id)


def build_slave_pong(
    action: str | None = None, *, ok: bool | None = None, msg_id: str | None = None
) -> str:
    """副控回执。

    - ok=None：确认/心跳 → [副G]<短动词>pong@<id> 或 [副G]PONG@<id>
    - ok=True：执行完成 → [副G]<短动词>done@<id>（或 [副G]DONE@<id>）
    - ok=False：执行失败 → [副G]<短动词>fail@<id>（或 [副G]FAIL@<id>）
    @author by ak
    """
    verb = ACK_VERB_REV.get(str(action or "").strip().lower())
    if ok is True:
        suffix = CMD_DONE.lower()
    elif ok is False:
        suffix = CMD_FAIL.lower()
    elif verb:
        # 命令回执用旧格式小写 pong（向后兼容）；纯 PING 回执/心跳用大写 PONG。
        suffix = CMD_PONG.lower()
    else:
        suffix = CMD_PONG
    body = f"{verb}{suffix}" if verb else suffix
    return _with_msg_id(f"{SLAVE_TAG}{body}", msg_id)


def parse_team_message(text: str) -> dict | None:
    """
    解析队伍频道一行文本。返回 {kind, role, action?, task_ids?, sender?, raw} 或 None。

    kind: command | ping | pong | done | fail；role: master | slave。
    sender: 发送者角色名（从 ^u&名字&^u 前缀提取，可能为空）。
    兼容聊天渲染前缀（^u&名字&^u：正文）。
    @author by ak
    """
    t = _strip_render_prefix(str(text or ""))
    t = t.strip()
    if not t:
        return None
    if t.startswith(MASTER_TAG):
        d = _parse_master_body(t[len(MASTER_TAG):].strip(), raw=t)
    elif t.startswith(SLAVE_TAG):
        d = _parse_slave_body(t[len(SLAVE_TAG):].strip(), raw=t)
    else:
        return None
    if d is not None:
        sender = extract_sender_name(text)
        if sender:
            d["sender"] = sender
    return d


def _strip_render_prefix(text: str) -> str:
    """剥离聊天渲染前缀（^u&名字&^u： 等），只留正文。@author by ak"""
    t = str(text or "")
    # ^u&...&^u： 发送者名渲染头（如 ^u&张三&^u：[主P]...）
    t = re.sub(r"\^u&.*?&?\^u\s*[:：]?", "", t, count=1, flags=re.S)
    # 去除残留 ^ 控制码（^RRGGBB 颜色 / ^u / ^x 等）
    t = re.sub(r"\^[0-9a-fA-F]{6}", "", t)
    t = re.sub(r"\^[A-Za-z0-9]?", "", t)
    return t.strip()


_SENDER_RE = re.compile(r"\^u&(.*?)&?\^u", re.S)


def extract_sender_name(text: str) -> str:
    """从渲染前缀 ^u&...&^u 提取发送者角色名。

    名字段可能是 '#<行号><名字>'（如 '#      1002张三'）或纯名字（如 '赵六'）。
    返回真实角色名；无前缀返回空串。
    @author by ak
    """
    t = str(text or "")
    m = _SENDER_RE.search(t)
    if not m:
        return ""
    seg = m.group(1)
    # 去掉行号/前缀残留（#、数字、空白、^ 控制码）
    seg = re.sub(r"^#?\s*\d+\s*", "", seg)
    seg = re.sub(r"\^[0-9a-fA-F]{6}", "", seg)
    seg = re.sub(r"\^[A-Za-z0-9]?", "", seg)
    return seg.strip()


def _parse_master_body(body: str, *, raw: str) -> dict | None:
    body, msg_id = split_msg_id(body)
    if body.upper() == CMD_PING:
        d: dict = {"kind": "ping", "role": "master", "raw": raw, "text": body}
        if msg_id:
            d["msg_id"] = msg_id
        return d
    if body.strip() == CMD_JIANGLONG_QUERY or body.startswith(f"{CMD_JIANGLONG_QUERY}:"):
        _, _, query_param = body.partition(":")
        d = {
            "kind": "jianglong_query",
            "role": "master",
            "test_mode": query_param.strip() == "1",
            "raw": raw,
            "text": body,
        }
        if msg_id:
            d["msg_id"] = msg_id
        return d
    verb, _, params = body.partition(":")
    action = text_to_action(verb.strip())
    if not action:
        return None
    ids: list[int] = []
    extra = ""
    if params:
        # 格式：<ids>[:<extra>]（extra 为飞图自定义槽位等附带参数）
        first, _, extra = params.partition(":")
        for part in first.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
    d = {
        "kind": "command",
        "role": "master",
        "action": action,
        "task_ids": ids,
        "extra": extra.strip(),
        "raw": raw,
        "text": body,
    }
    if extra.startswith("route="):
        try:
            encoded = extra.split("=", 1)[1].strip()
            if encoded.startswith("2_"):
                parts = encoded.split("_")
                if len(parts) >= 3 and parts[1] in ("p", "pa"):
                    route = {
                        "phase": {"p": "portal", "pa": "portal_arrive"}[parts[1]],
                        "portal_kind": {"u": "dungeon_upper", "d": "dungeon_deep"}.get(parts[2], ""),
                        "direct_portal": True,
                    }
                else:
                    route = {}
            elif encoded.startswith("2,"):
                parts = encoded.split(",")
                route = {}
                if len(parts) >= 3:
                    phase = {"pa": "portal_arrive", "p": "portal", "t": "target"}.get(parts[1], parts[1])
                    route["phase"] = phase
                    try:
                        if phase in ("portal", "portal_arrive") and len(parts) >= 9:
                            route.update(
                                {
                                    "portal_kind": {"u": "dungeon_upper", "d": "dungeon_deep"}.get(parts[2], ""),
                                    "portal_tid": _from_base36(parts[3]),
                                    "portal_obj_id": _from_base36(parts[4]),
                                    "portal_x": _route_float(parts[5]),
                                    "portal_y": _route_float(parts[6]),
                                    "portal_z": _route_float(parts[7]),
                                    "target_scene_id": _from_base36(parts[8]),
                                    "origin_scene_id": 68,
                                }
                            )
                        elif phase == "target" and len(parts) >= 7:
                            route.update(
                                {
                                    "target_tid": _from_base36(parts[2]),
                                    "target_x": _route_float(parts[3]),
                                    "target_y": _route_float(parts[4]),
                                    "target_z": _route_float(parts[5]),
                                    "target_scene_id": _from_base36(parts[6]),
                                }
                            )
                    except (TypeError, ValueError):
                        route = {}
                elif len(parts) == 2:
                    route = {
                        "phase": {"pa": "portal_arrive", "p": "portal", "t": "target"}.get(parts[1], parts[1])
                    }
            elif encoded.startswith(("p=", "pa", "p|", "i")) or "|" in encoded:
                reverse_phase = {"pa": "portal_arrive", "p": "portal", "t": "target"}
                route = {}
                for field in encoded.split("|"):
                    key, _, value = field.partition("=")
                    if not _:
                        continue
                    field_map = {
                        "p": "phase",
                        "k": "portal_kind",
                        "s": "origin_scene_id",
                        "d": "target_scene_id",
                        "pt": "portal_tid",
                        "po": "portal_obj_id",
                        "px": "portal_x",
                        "py": "portal_y",
                        "pz": "portal_z",
                        "tt": "target_tid",
                        "tx": "target_x",
                        "ty": "target_y",
                        "tz": "target_z",
                        "r": "target_round",
                    }
                    name = field_map.get(key)
                    if not name:
                        continue
                    if name == "phase":
                        route[name] = reverse_phase.get(value, value)
                    else:
                        try:
                            route[name] = (
                                int(value)
                                if name.endswith("_id") or name.endswith("_tid") or name.endswith("_round")
                                else float(value)
                            )
                        except (TypeError, ValueError):
                            route[name] = value
            else:
                encoded += "=" * (-len(encoded) % 4)
                route = json.loads(base64.urlsafe_b64decode(encoded).decode())
            if isinstance(route, dict):
                d.update(
                    {
                        key: route.get(key)
                        for key in (
                            "portal_x",
                            "portal_y",
                            "portal_z",
                            "origin_scene_id",
                            "portal_tid",
                            "portal_obj_id",
                            "target_scene_id",
                            "target_tid",
                            "target_x",
                            "target_y",
                            "target_z",
                            "target_round",
                            "phase",
                            "portal_kind",
                        )
                    }
                )
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    if msg_id:
        d["msg_id"] = msg_id
    return d


def _parse_slave_body(body: str, *, raw: str) -> dict:
    body, msg_id = split_msg_id(body)
    if body.startswith(f"{CMD_JIANGLONG_STATUS}:"):
        parts = body.split(":", 3)
        values = []
        for value in parts[1:4]:
            try:
                values.append(bool(int(value)))
            except (TypeError, ValueError):
                values.append(False)
        d = {
            "kind": "jianglong_status",
            "role": "slave",
            "enabled": values[0],
            "in_wuzun": values[1],
            "hang_running": values[2],
            "raw": raw,
            "text": body,
        }
        if msg_id:
            d["msg_id"] = msg_id
        return d
    verb = body
    ok: bool | None = None
    if body.upper() == CMD_PONG:
        verb = ""
        ok = None
    elif body.upper().endswith(CMD_DONE):
        verb = body[: -len(CMD_DONE)].strip()
        ok = True
    elif body.upper().endswith(CMD_FAIL):
        verb = body[: -len(CMD_FAIL)].strip()
        ok = False
    elif body.lower().endswith(CMD_PONG.lower()):
        verb = body[: -len(CMD_PONG)].strip()
        ok = None
    action = ACK_VERB.get(str(verb or "").strip(), "")
    kind = "pong" if ok is None else ("done" if ok else "fail")
    d: dict = {
        "kind": kind,
        "role": "slave",
        "action": action or None,
        "ok": ok,
        "raw": raw,
        "text": body,
    }
    if msg_id:
        d["msg_id"] = msg_id
    return d


class TeamMsgSeen:
    """队内控消息一次识别去重。

    同一 (sender, role, msg_id) 只消费一次；无 msg_id 的旧格式消息始终放行。
    容量固定，超限淘汰最旧记录（dict 保持插入序）。每个窗口/消费方各持一份。
    @author by ak
    """

    def __init__(self, capacity: int = 512) -> None:
        self._cap = max(1, int(capacity or 0))
        self._keys: dict[str, None] = {}

    def consume(self, msg: dict | None) -> bool:
        """返回 True 表示应处理（首次见到）；False 表示重复消息应忽略。@author by ak"""
        if not isinstance(msg, dict):
            return True
        mid = str(msg.get("msg_id") or "").strip()
        if not mid:
            return True
        sender = str(msg.get("sender") or "").strip()
        # 副控状态会复用主控查询的 msg_id；有些聊天渲染事件取不到
        # sender，不能让不同副控因此合并。缺 sender 时用 tap 的单条 seq
        # 区分消息实例；同一事件重读仍保留同一 seq。
        identity = sender or f"seq={int(msg.get('seq') or 0)}"
        key = f"{identity}:{str(msg.get('role') or '').strip()}:{mid}"
        if key in self._keys:
            return False
        self._keys[key] = None
        if len(self._keys) > self._cap:
            oldest = next(iter(self._keys))
            del self._keys[oldest]
        return True

    def reset(self) -> None:
        """清空已消费记录。@author by ak"""
        self._keys.clear()


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------

# 聊天加密包装：thiscall 0x00D089B0(this=发送管理器, arg=Octets 对象)。
# Octets 布局: +0 vtable, +4 begin*, +8 end*, +0xC capacity。
TEAM_CHAT_WRAPPER_NOTE_VA = 0x00D089B0
OCTETS_VTABLE_NOTE_VA = 0x0122E228
# 发送管理器是游戏堆对象，地址随会话变化。已用注入 hook（xajh_team_tap.dll）
# 自动发现写入共享内存，resolve_send_mgr 优先读它；未捕获时返回 0（拒绝发送），
# 避免用错误地址 remote_call 导致游戏崩溃。
# TEAM_SEND_MGR_DEFAULT 仅作历史参考，不再用于发送兜底。
TEAM_SEND_MGR_DEFAULT = 0x312E1D60
TEAM_SEND_MGR_ENV = "XAJH_TEAM_SEND_MGR"

# 聊天发送节流（2026-08-15 实机测出：窗口内约 5 条，之后被游戏丢弃，约 10s 恢复）。
TEAM_SEND_INTERVAL_S = 1.0        # 每条固定间隔
TEAM_SEND_WINDOW_MAX = 5          # 每个窗口最多
TEAM_SEND_WINDOW_S = 10.0         # 窗口长度
TEAM_SEND_QUEUE_MAX = 16          # 每 pid 发送队列上限（超出丢最旧）
TEAM_SEND_RETRY_MAX = 10           # 单条消息最多自动重试次数，避免永久占队列

# 队内控消息时效：超过该秒数的历史消息不响应（避免重连/登录后队伍频道
# 历史命令被当新命令触发一串动作）。
TEAM_MSG_MAX_AGE_S = 3.0

# 副控心跳：每 TEAM_HEARTBEAT_S 秒发一次 [副G]PONG@id；主控以
# TEAM_ALIVE_WINDOW_S 秒内收到过副控消息判定其在控。
TEAM_HEARTBEAT_S = 180.0
TEAM_ALIVE_WINDOW_S = 240.0

# 发送管理器自动发现：注入 xajh_team_tap.dll（hook 0x00D089B0，thiscall ecx=发送管理器），
# 把命中时的 ecx + caller 写入共享内存 Local\\XajhTeamTap_<pid>。纯软件，无需 x32dbg。
TEAM_TAP_DLL = "xajh_team_tap.dll"
TEAM_TAP_MAGIC = 0x54544D50  # 'TMTP'
TEAM_TAP_ACTIVE = 1
TEAM_TAP_ERR = 2
# 共享内存 pack(1) v3:
#   magic,version,struct_size,status,target_va,send_mgr,send_mgr_seen,error[128],
#   hit_write_seq, hits[64] (TeamTapHit=seq,self,caller),
#   send_req (TeamSendReq = seq,status,result,len,data[512])
TEAM_TAP_OFF_SEND_MGR = 20
TEAM_TAP_OFF_SEEN = 24
TEAM_TAP_OFF_HIT_SEQ = 156
TEAM_TAP_OFF_HITS = 160
TEAM_TAP_HITS = 64
TEAM_TAP_HIT_SIZE = 12
TEAM_TAP_HITS_BYTES = TEAM_TAP_HIT_SIZE * TEAM_TAP_HITS  # 768
TEAM_TAP_OFF_SEND_REQ = TEAM_TAP_OFF_HITS + TEAM_TAP_HITS_BYTES  # 928
TEAM_SEND_MAX_LEN = 512
# send_req fields: seq,status,result,len,data
_TEAM_REQ_SEQ = 0
_TEAM_REQ_STATUS = 4
_TEAM_REQ_RESULT = 8
_TEAM_REQ_LEN = 12
_TEAM_REQ_DATA = 16
TEAM_TAP_SIZE = TEAM_TAP_OFF_SEND_REQ + 16 + TEAM_SEND_MAX_LEN  # 1456
# dump 槽 + 身份指纹字段（v5）：dump_len@1456, dump@1460(256B),
# send_ident0@1716, ident1@1720, ident2@1724, ident_seen@1728；总大小 1732
TEAM_TAP_OFF_DUMP_LEN = TEAM_TAP_SIZE
TEAM_TAP_OFF_DUMP = TEAM_TAP_OFF_DUMP_LEN + 4  # 1460
TEAM_TAP_OFF_IDENT0 = TEAM_TAP_OFF_DUMP + 256  # 1716
TEAM_TAP_OFF_IDENT1 = TEAM_TAP_OFF_IDENT0 + 4  # 1720
TEAM_TAP_OFF_IDENT2 = TEAM_TAP_OFF_IDENT1 + 4  # 1724
TEAM_TAP_OFF_IDENT_SEEN = TEAM_TAP_OFF_IDENT2 + 4  # 1728
# 精确扫描结果（v5）：vtable + self/config 不变式定位，无需先收到聊天调用。
TEAM_TAP_OFF_EXACT_STATUS = TEAM_TAP_OFF_IDENT_SEEN + 4  # 1732
TEAM_TAP_OFF_EXACT_COUNT = TEAM_TAP_OFF_EXACT_STATUS + 4  # 1736
TEAM_TAP_OFF_EXACT_SEND_MGR = TEAM_TAP_OFF_EXACT_COUNT + 4  # 1740
TEAM_TAP_OFF_EXACT_SEEN = TEAM_TAP_OFF_EXACT_SEND_MGR + 4  # 1744
TEAM_TAP_SIZE = TEAM_TAP_OFF_EXACT_SEEN + 4  # 1748
TEAM_SEND_IDLE = 0
TEAM_SEND_PENDING = 1
TEAM_SEND_DONE = 2
TEAM_SEND_ERR = 3
# mailbox 发送等待上限（DLL 在游戏内线程处理，通常 <50ms）
TEAM_MAILBOX_TIMEOUT_S = 2.0
TEAM_MAILBOX_POLL_S = 0.005
# v5 精确扫描线程约 500ms 一轮；注入后第一条消息要等它完成第一次扫描。
TEAM_EXACT_SCAN_WAIT_S = 2.0
TEAM_EXACT_SCAN_POLL_S = 0.05


def _team_tap_paths() -> tuple:
    """定位 team_tap DLL 与注入器（同 chat_tap 的查找策略）。@author by ak"""
    from pathlib import Path

    try:
        from common.paths import NATIVE_BIN_DIR, app_root, bundle_root
    except Exception:
        root = Path(__file__).resolve().parents[2]
        NATIVE_BIN_DIR = root / "native" / "bin"
        app_root = lambda: root  # noqa: E731
        bundle_root = app_root
    search: list = []
    for d in (
        app_root() / "native" / "bin",
        NATIVE_BIN_DIR,
        bundle_root() / "native" / "bin",
        app_root() / "_internal" / "native" / "bin",
        Path(__file__).resolve().parents[2] / "native" / "bin",
    ):
        if d not in search:
            search.append(d)
    dll = next(
        (d / TEAM_TAP_DLL for d in search if (d / TEAM_TAP_DLL).is_file()),
        search[0] / TEAM_TAP_DLL,
    )
    injector = next(
        (d / "xajh_inject.exe" for d in search if (d / "xajh_inject.exe").is_file()),
        search[0] / "xajh_inject.exe",
    )
    return dll, injector


def _team_tap_read(pid: int) -> dict:
    """读 team_tap 共享内存。返回 dict 或 {}（不存在）。兼容 v1/v2。@author by ak"""
    import ctypes
    import struct
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.restype = wintypes.HANDLE
    k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.MapViewOfFile.restype = wintypes.LPVOID
    k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.c_size_t]
    k32.UnmapViewOfFile.argtypes = [wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    name = f"Local\\XajhTeamTap_{int(pid)}"
    h = k32.OpenFileMappingW(0x0004, False, name)  # FILE_MAP_READ
    if not h:
        return {}
    try:
        # Map the whole section so old (smaller) and v5 (larger) taps both work.
        view = k32.MapViewOfFile(h, 0x0004, 0, 0, 0)
        if not view:
            return {}
        try:
            class MBI(ctypes.Structure):
                _fields_ = [
                    ("BaseAddress", ctypes.c_size_t),
                    ("AllocationBase", ctypes.c_size_t),
                    ("AllocationProtect", wintypes.DWORD),
                    ("RegionSize", ctypes.c_size_t),
                    ("State", wintypes.DWORD),
                    ("Protect", wintypes.DWORD),
                    ("Type", wintypes.DWORD),
                ]

            mbi = MBI()
            mapped_size = TEAM_TAP_SIZE
            if k32.VirtualQuery(ctypes.c_void_p(view), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                mapped_size = min(TEAM_TAP_SIZE, int(mbi.RegionSize))
            raw = ctypes.string_at(view, mapped_size)
            magic = int.from_bytes(raw[0:4], "little")
            declared_size = int.from_bytes(raw[8:12], "little")
            # Never treat page-granular readable bytes as protocol fields.
            if magic == int.from_bytes(b"TMTP", "big") and 28 <= declared_size <= len(raw):
                raw = raw[:declared_size]
            status = int.from_bytes(raw[12:16], "little")
            send_mgr = int.from_bytes(
                raw[TEAM_TAP_OFF_SEND_MGR:TEAM_TAP_OFF_SEND_MGR + 4], "little"
            )
            seen = int.from_bytes(raw[TEAM_TAP_OFF_SEEN:TEAM_TAP_OFF_SEEN + 4], "little")
            # 身份指纹三字节（DLL 从聊天封包 offset 7..9 捕获，如 2b 40 01 / 2b a0 01 / 30 30 01）。
            send_ident = (0x2B, 0x40, 0x01)
            send_ident_seen = 0
            if TEAM_TAP_OFF_IDENT_SEEN + 4 <= len(raw):
                id0 = int.from_bytes(raw[TEAM_TAP_OFF_IDENT0:TEAM_TAP_OFF_IDENT0 + 4], "little")
                id1 = int.from_bytes(raw[TEAM_TAP_OFF_IDENT1:TEAM_TAP_OFF_IDENT1 + 4], "little")
                id2 = int.from_bytes(raw[TEAM_TAP_OFF_IDENT2:TEAM_TAP_OFF_IDENT2 + 4], "little")
                iseen = int.from_bytes(
                    raw[TEAM_TAP_OFF_IDENT_SEEN:TEAM_TAP_OFF_IDENT_SEEN + 4], "little"
                )
                if iseen:
                    send_ident = (id0 & 0xFF, id1 & 0xFF, id2 & 0xFF)
                    send_ident_seen = 1
            hits: list[dict] = []
            if TEAM_TAP_OFF_HIT_SEQ + 4 <= len(raw):
                write_seq = int.from_bytes(
                    raw[TEAM_TAP_OFF_HIT_SEQ:TEAM_TAP_OFF_HIT_SEQ + 4], "little"
                )
                for i in range(TEAM_TAP_HITS):
                    base = TEAM_TAP_OFF_HITS + i * TEAM_TAP_HIT_SIZE
                    if base + TEAM_TAP_HIT_SIZE > len(raw):
                        break
                    seq, self_, caller = struct.unpack_from(
                        "<III", raw, base
                    )
                    if seq:
                        hits.append({"seq": seq, "self": self_, "caller": caller})
            exact_status = exact_count = exact_mgr = exact_seen = 0
            if TEAM_TAP_OFF_EXACT_SEEN + 4 <= len(raw):
                exact_status = int.from_bytes(raw[TEAM_TAP_OFF_EXACT_STATUS:TEAM_TAP_OFF_EXACT_STATUS + 4], "little")
                exact_count = int.from_bytes(raw[TEAM_TAP_OFF_EXACT_COUNT:TEAM_TAP_OFF_EXACT_COUNT + 4], "little")
                exact_mgr = int.from_bytes(raw[TEAM_TAP_OFF_EXACT_SEND_MGR:TEAM_TAP_OFF_EXACT_SEND_MGR + 4], "little")
                exact_seen = int.from_bytes(raw[TEAM_TAP_OFF_EXACT_SEEN:TEAM_TAP_OFF_EXACT_SEEN + 4], "little")
            return {
                "magic": magic, "version": int.from_bytes(raw[4:8], "little"),
                "struct_size": int.from_bytes(raw[8:12], "little"),
                "status": status, "send_mgr": send_mgr,
                "seen": seen, "hits": hits,
                "send_ident": send_ident, "send_ident_seen": send_ident_seen,
                "exact_status": exact_status, "exact_count": exact_count,
                "exact_send_mgr": exact_mgr, "exact_seen": exact_seen,
            }
        finally:
            k32.UnmapViewOfFile(ctypes.c_void_p(view))
    finally:
        k32.CloseHandle(h)


def ensure_team_tap(pid: int, *, log: LogFn | None = None) -> bool:
    """注入 team_tap DLL（发送管理器自动发现）。幂等：已注入则复用。@author by ak"""
    import subprocess

    log = log or (lambda _m: None)
    pid = int(pid or 0)
    if not pid:
        return False
    cur = _team_tap_read(pid)
    if (cur.get("magic") == TEAM_TAP_MAGIC and cur.get("status") == TEAM_TAP_ACTIVE
            and int(cur.get("version") or 0) >= 5):
        return True
    # error/未激活状态也尝试重新注入（新版 DLL 会覆盖共享内存状态）。
    dll, injector = _team_tap_paths()
    if not dll.is_file() or not injector.is_file():
        log(f"队内控 [tap] 缺少 {TEAM_TAP_DLL} / xajh_inject.exe")
        return False
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = int(subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
    try:
        r = subprocess.run(
            [str(injector), str(pid), str(dll.resolve())],
            cwd=str(dll.parent), capture_output=True, text=True,
            timeout=8.0, creationflags=creationflags, check=False,
        )
    except Exception as e:
        log(f"队内控 [tap] 注入失败: {e}")
        return False
    log(f"队内控 [tap] inject rc={r.returncode} {(r.stdout or '').strip()}")
    if r.returncode != 0:
        return False
    cur = _team_tap_read(pid)
    if not cur or cur.get("magic") != TEAM_TAP_MAGIC:
        log("队内控 [tap] 注入后共享内存未就绪")
        return False
    ok = cur.get("status") == TEAM_TAP_ACTIVE
    if ok and int(cur.get("version") or 0) < 5:
        log("队内控 [tap] 旧版 DLL 已驻留（需重启游戏后加载 v5 精确扫描版）")
        return False
    if not ok:
        log(f"队内控 [tap] 未激活 status={cur.get('status')}")
    return ok


def read_send_mgr_from_shm(pid: int) -> int:
    """从 team_tap 共享内存读发送管理器（未捕获返回 0）。@author by ak"""
    cur = _team_tap_read(pid)
    if cur.get("magic") != TEAM_TAP_MAGIC:
        return 0
    if cur.get("status") == TEAM_TAP_ERR:
        return 0
    if not cur.get("seen"):
        return 0
    return int(cur.get("send_mgr") or 0)


def resolve_send_mgr(pid: int, *, ensure: bool = True, log: LogFn | None = None) -> int:
    """返回当前会话的聊天发送管理器地址。

    优先：注入 team_tap 后从共享内存读取（纯软件自动发现，无需 x32dbg）。
    未捕获时返回 0（调用方应拒绝发送，避免用错误地址 remote_call 崩溃）。
    env XAJH_TEAM_SEND_MGR 可显式覆盖（用户主动设置时可信）。
    @author by ak
    """
    import os

    pid = int(pid or 0)
    if ensure and pid:
        try:
            ensure_team_tap(pid, log=log)
        except Exception:
            pass
    if pid:
        mgr = read_send_mgr_from_shm(pid)
        if mgr:
            return mgr
        # v5 的精确扫描在游戏进程内异步执行；刚注入时 status 已 ACTIVE，
        # 但 send_mgr 可能还没写回共享内存。这里短等待一次，避免第一条
        # 队内控命令被误判为“发送管理器未捕获”。
        cur = _team_tap_read(pid)
        if (cur.get("magic") == TEAM_TAP_MAGIC
                and cur.get("status") == TEAM_TAP_ACTIVE
                and int(cur.get("version") or 0) >= 5):
            deadline = time.monotonic() + TEAM_EXACT_SCAN_WAIT_S
            while time.monotonic() < deadline:
                time.sleep(TEAM_EXACT_SCAN_POLL_S)
                mgr = read_send_mgr_from_shm(pid)
                if mgr:
                    return mgr
    raw = str(os.environ.get(TEAM_SEND_MGR_ENV) or "").strip()
    if raw:
        try:
            return int(raw, 0) & 0xFFFFFFFF
        except ValueError:
            pass
    return 0


def _note_to_live(pid: int, note_va: int) -> int:
    """note VA → 进程内实际地址（基于模块基址）。失败返回 0。@author by ak"""
    try:
        from app.core.plg_exports import DEFAULT_IMAGE_BASE
        from app.core.session_store import SessionStore

        base = 0
        try:
            sess = SessionStore().get(int(pid))
            if sess is not None:
                base = int(getattr(sess, "module_base", 0) or 0)
        except Exception:
            base = 0
        if not base:
            base = int(DEFAULT_IMAGE_BASE)
        return (base + (int(note_va) - int(DEFAULT_IMAGE_BASE))) & 0xFFFFFFFF
    except Exception:
        return 0


def build_team_chat_c2s(
    text: str,
    channel_id: int = TEAM_CHANNEL_ID,
    ident: tuple[int, int, int] = (0x2B, 0x40, 0x01),
    prefix_byte: int | None = None,
    *,
    high_byte: int | None = None,
) -> bytes:
    """构建游戏队伍频道聊天 c2s 明文封包。

    布局（2026-08-16 实机抓包确认，张三 ZZ99 / 李四 XX77YY 封包）：
      0x00  0x4F                     fixed header
      0x01  u8  total = text_bytes + 28
      0x02..0x12  17B 固定前缀（offset 6 = 身份高位，指纹@7..9，channel u32@0x0D）
      0x13        u8 text_len (UTF-16LE byte count)
      0x14..      text UTF-16LE
      tail        10 x 0x00

    角色相关字节（offset 6..9）= (rid>>24, rid>>16, rid>>8, 0x01)：
      张三 0x01001001→01 2b 40 01，王五 0x01002001→01 2b a0 01，
      赵六 0x01003001→01 30 30 01，李四 0x00004001→00 09 e0 01。
    ident 为 offset 7..9 三字节；high_byte 为 offset 6（默认 0x01，兼容高字节=01 的号）。
    prefix_byte 为兼容旧参数：仅覆盖 offset 8 单字节。

    例：发 "333"（张三）→ 4f 22 0000000001 2b4001 00000003 0000000001 06 33 00 33 00 33 00 0000000000
    @author by ak
    """
    body = str(text or "").encode("utf-16le")
    n = len(body)
    total = n + 28
    if total > 0xFF:
        raise ValueError(f"chat text too long: {total}B > 255")
    i0, i1, i2 = (int(ident[0]) & 0xFF, int(ident[1]) & 0xFF, int(ident[2]) & 0xFF)
    if prefix_byte is not None:
        i1 = int(prefix_byte) & 0xFF
    hi = int(high_byte) & 0xFF if high_byte is not None else 0x01
    # prefix（不含 4f<len>，17 字节）：00 00 00 00 <hi> <i0> <i1> <i2>
    #   00 00 00 | <channel u32> | 00 01。hi 即 offset 6 = (rid>>24)&0xFF。
    prefix = (
        bytes([0x00, 0x00, 0x00, 0x00, hi, i0, i1, i2])
        + bytes.fromhex("000000030000000001")
    )
    out = bytearray()
    out += bytes([0x4F, total & 0xFF])
    # channel 固定为队伍(3)：03 00 00 00；如需其他频道替换第 13..16 字节。
    out += prefix
    if channel_id != TEAM_CHANNEL_ID:
        import struct

        out[0x0D : 0x11] = struct.pack("<I", int(channel_id) & 0xFFFFFFFF)
    out += bytes([n & 0xFF])                # 0x13
    out += body                             # 0x14..
    out += bytes(10)                        # tail
    return bytes(out)


PRIVATE_CHAT_CMD = 0x60  # 私聊 c2s 命令字节（实机抓包 2026-08-29，与队伍 0x4F 不同）

# 私聊发送限频（服务端按发送者限频：小批量+冷却，与组队邀请 3/批 同量级）。
# 保守取 3 条/10s 滑窗；窗口满时阻塞等待而非丢弃（控制面消息不能丢）。
PRIVATE_SEND_WINDOW_MAX = 3
PRIVATE_SEND_WINDOW_S = 10.0
_PRIVATE_SEND_LOCK = threading.RLock()
_PRIVATE_SEND_TS: dict[int, float] = {}
_PRIVATE_SEND_CNT: dict[int, int] = {}


def private_send_gate_reset() -> None:
    """清空私聊发送限频窗口状态（测试用）。@author by ak"""
    with _PRIVATE_SEND_LOCK:
        _PRIVATE_SEND_TS.clear()
        _PRIVATE_SEND_CNT.clear()


def _private_send_gate(pid: int, *, log: LogFn | None = None) -> float:
    """私聊发送限频门：每发送者滑窗 PRIVATE_SEND_WINDOW_MAX 条 /
    PRIVATE_SEND_WINDOW_S 秒，窗口满时阻塞等待到放行。

    返回本次调用累计等待的秒数（0 表示立即放行）。@author by ak
    """
    waited = 0.0
    while True:
        admitted = False
        wait = 0.0
        with _PRIVATE_SEND_LOCK:
            now = time.time()
            ts = _PRIVATE_SEND_TS.get(pid, 0.0)
            cnt = _PRIVATE_SEND_CNT.get(pid, 0)
            if now - ts >= PRIVATE_SEND_WINDOW_S:
                _PRIVATE_SEND_TS[pid] = now
                _PRIVATE_SEND_CNT[pid] = 0
                ts, cnt = now, 0
            if cnt < PRIVATE_SEND_WINDOW_MAX:
                _PRIVATE_SEND_CNT[pid] = cnt + 1
                admitted = True
            else:
                wait = max(0.05, PRIVATE_SEND_WINDOW_S - (now - ts))
        if admitted:
            return waited
        if log is not None and waited == 0.0:
            log(
                f"私聊 [限频] {PRIVATE_SEND_WINDOW_MAX}条/{PRIVATE_SEND_WINDOW_S:.0f}s"
                f" 窗口满，等待 {wait:.1f}s"
            )
        time.sleep(wait)
        waited += wait


def build_private_chat_c2s(
    text: str,
    sender_rid: int,
    sender_name: str,
    target_rid: int,
    target_name: str,
) -> bytes:
    """构建私聊 c2s 明文封包（命令 0x60）。

    布局（2026-08-29 实机抓包确认，十丶三→苦寒未曾来 ZCAP1 57B /
    十丶三→初一 ZCAP2 51B 双样本定案）：
      0x00      0x60                 cmd（私聊，区别于队伍 0x4F）
      0x01      u8  total = len - 2
      0x02..0x06 00*5
      0x07      0x01
      0x08..0x0B 00*4
      0x0C..0x0F sender_rid          大端 u32（与队伍封包 identity 同序）
      0x10..0x13 00*4
      0x14..0x17 target_rid          大端 u32（私聊目标，核心字段）
      0x18      u8 sender_name_bytes + sender_name UTF-16LE
      …         u8 target_name_bytes + target_name UTF-16LE
      …         00 00
      …         u8 text_bytes + text UTF-16LE
      tail      00 00

    长度公式：total_len = 24 + (1+2n) + (1+2m) + 2 + (1+2k) + 2。
    注意：本封包没有队伍封包的身份指纹字段（offset 6..9 是固定零/标志位），
    发送时禁止走 _team_mailbox_send 的 ident 改写（patch_ident=False）。
    @author by ak
    """
    import struct as _struct

    s_name = str(sender_name or "").encode("utf-16-le")
    t_name = str(target_name or "").encode("utf-16-le")
    body = str(text or "").encode("utf-16-le")
    if len(s_name) > 0xFF or len(t_name) > 0xFF:
        raise ValueError("private chat name too long")
    if not body:
        raise ValueError("private chat empty text")
    total = 24 + 1 + len(s_name) + 1 + len(t_name) + 2 + 1 + len(body) + 2
    if total > 0xFF:
        raise ValueError(f"private chat packet too long: {total}B > 255")
    out = bytearray()
    out += bytes([PRIVATE_CHAT_CMD, (total - 2) & 0xFF])   # 0x01 = len-2（与队伍封包同约定）
    out += bytes(5)                            # 0x02..0x06
    out += bytes([0x01])                       # 0x07
    out += bytes(4)                            # 0x08..0x0B
    out += _struct.pack(">I", int(sender_rid) & 0xFFFFFFFF)   # 0x0C..0x0F
    out += bytes(4)                            # 0x10..0x13
    out += _struct.pack(">I", int(target_rid) & 0xFFFFFFFF)   # 0x14..0x17
    out += bytes([len(s_name) & 0xFF]) + s_name
    out += bytes([len(t_name) & 0xFF]) + t_name
    out += bytes(2)
    out += bytes([len(body) & 0xFF]) + body
    out += bytes(2)
    return bytes(out)


def _invalidate_send_mgr(pid: int) -> None:
    """Drop a sender pointer after native wrapper failure."""
    import ctypes
    from ctypes import wintypes
    name = f"Local\\XajhTeamTap_{int(pid)}"
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.restype = wintypes.HANDLE
    k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.MapViewOfFile.restype = wintypes.LPVOID
    k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    k32.UnmapViewOfFile.argtypes = [wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenFileMappingW(0x0002 | 0x0004, False, name)
    if not h:
        return
    view = k32.MapViewOfFile(h, 0x0002 | 0x0004, 0, 0, TEAM_TAP_SIZE)
    if not view:
        k32.CloseHandle(h)
        return
    try:
        ctypes.memset(ctypes.c_void_p(view + TEAM_TAP_OFF_SEND_MGR), 0, 8)
    finally:
        k32.UnmapViewOfFile(ctypes.c_void_p(view))
        k32.CloseHandle(h)

def _team_mailbox_send(
    pid: int,
    plaintext: bytes,
    *,
    patch_ident: bool = True,
    log: LogFn | None = None,
) -> dict:
    """通过 DLL 内 sender 线程（mailbox）发送聊天封包。

    Python 只把封包写入共享内存 send_req，游戏进程内的 sender 线程用正确
    thiscall 栈帧调用 wrapper 0x00D089B0 发送。绕开 remote_call 栈帧问题。
    patch_ident=True（默认，队伍封包）时自动改写偏移 6..9 为本角色身份指纹；
    私聊封包（0x60）没有该字段，必须传 patch_ident=False 避免破坏固定零位。
    @author by ak
    """
    import ctypes
    from ctypes import wintypes

    log = log or (lambda _m: None)
    if not plaintext or len(plaintext) > TEAM_SEND_MAX_LEN:
        return {"ok": False, "error": f"payload too long: {len(plaintext or b'')}"}
    if patch_ident:
        try:
            i0, i1, i2 = _resolve_send_ident(pid)
            hi = 0x01
            rid = _cached_role_id(pid)
            if rid:
                hi = _role_id_high_byte(rid)
            # 身份字节：offset 6 = 身份高位（(rid>>24)&0xFF），offset 7..9 = <i0> <i1> <i2>。
            if len(plaintext) > 0x0A and (
                plaintext[6:10] != bytes([hi, i0, i1, i2])
            ):
                plaintext = plaintext[:6] + bytes([hi, i0, i1, i2]) + plaintext[10:]
        except Exception:
            pass
    send_mgr = resolve_send_mgr(pid, log=log)
    if not send_mgr:
        log("队内控 [发] 发送管理器精确扫描未命中（已等待2秒）")
        return {"ok": False, "error": "send_mgr not captured"}

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.restype = wintypes.HANDLE
    k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.MapViewOfFile.restype = wintypes.LPVOID
    k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.c_size_t]
    k32.UnmapViewOfFile.argtypes = [wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    name = f"Local\\XajhTeamTap_{int(pid)}"
    h = k32.OpenFileMappingW(0x0002 | 0x0004, False, name)  # FILE_MAP_WRITE|READ
    if not h:
        return {"ok": False, "error": "team_tap mapping missing"}
    view = k32.MapViewOfFile(h, 0x0002 | 0x0004, 0, 0, TEAM_TAP_SIZE)
    if not view:
        k32.CloseHandle(h)
        return {"ok": False, "error": "map view failed"}
    try:
        req_off = TEAM_TAP_OFF_SEND_REQ
        seq_now = int.from_bytes(
            ctypes.string_at(ctypes.c_void_p(view + req_off + _TEAM_REQ_SEQ), 4),
            "little",
        )
        # wait until mailbox is idle
        deadline = time.time() + TEAM_MAILBOX_TIMEOUT_S
        status = int.from_bytes(
            ctypes.string_at(ctypes.c_void_p(view + req_off + _TEAM_REQ_STATUS), 4),
            "little",
        )
        while status == TEAM_SEND_PENDING and time.time() < deadline:
            time.sleep(TEAM_MAILBOX_POLL_S)
            status = int.from_bytes(
                ctypes.string_at(ctypes.c_void_p(view + req_off + _TEAM_REQ_STATUS), 4),
                "little",
            )
        if status == TEAM_SEND_PENDING:
            return {"ok": False, "error": "mailbox busy"}

        next_seq = (int(seq_now) + 1) & 0xFFFFFFFF
        # 只构建 send_req 块（不动 send_mgr/send_mgr_seen 等头部字段）
        req_len = 16 + TEAM_SEND_MAX_LEN
        payload = bytearray(req_len)
        payload[_TEAM_REQ_SEQ:_TEAM_REQ_SEQ + 4] = next_seq.to_bytes(4, "little")
        payload[_TEAM_REQ_STATUS:_TEAM_REQ_STATUS + 4] = TEAM_SEND_PENDING.to_bytes(4, "little")
        payload[_TEAM_REQ_LEN:_TEAM_REQ_LEN + 4] = len(plaintext).to_bytes(4, "little")
        payload[_TEAM_REQ_DATA:_TEAM_REQ_DATA + len(plaintext)] = plaintext
        ctypes.memmove(ctypes.c_void_p(view + req_off), bytes(payload), req_len)

        # wait for completion
        deadline = time.time() + TEAM_MAILBOX_TIMEOUT_S
        result = 0
        done = False
        while time.time() < deadline:
            status = int.from_bytes(
                ctypes.string_at(ctypes.c_void_p(view + req_off + _TEAM_REQ_STATUS), 4),
                "little",
            )
            if status == TEAM_SEND_DONE:
                done = True
                result = int.from_bytes(
                    ctypes.string_at(ctypes.c_void_p(view + req_off + _TEAM_REQ_RESULT), 4),
                    "little",
                )
                break
            time.sleep(TEAM_MAILBOX_POLL_S)
        log(f"队内控 [发] mailbox seq={next_seq} len={len(plaintext)} "
            f"ret=0x{result:08X} done={done} mgr=0x{send_mgr:X}")
        if not done:
            _invalidate_send_mgr(pid)
            return {"ok": False, "error": "mailbox timeout"}
        # wrapper 返回 al=1 表示发送成功；done 只代表线程结束。
        ok = int(result) == 1
        if not ok:
            _invalidate_send_mgr(pid)
        return {"ok": ok, "ret": result, "mgr": send_mgr, "mailbox": True, **({} if ok else {"error": f"wrapper_ret_{result}"})}
    finally:
        k32.UnmapViewOfFile(ctypes.c_void_p(view))
        k32.CloseHandle(h)


def _send_via_wrapper(pid: int, plaintext: bytes, *, log: LogFn | None = None) -> dict:
    """发送聊天封包。优先 DLL 内 sender 线程（mailbox，thiscall 帧正确）；
    旧版 team_tap（无 mailbox）回退 remote_call。@author by ak"""
    log = log or (lambda _m: None)
    if not plaintext:
        return {"ok": False, "error": "empty payload"}
    try:
        r = _team_mailbox_send(pid, plaintext, log=log)
        if r.get("ok") or r.get("error") != "team_tap mapping missing":
            return r
        # mailbox 不可用（旧版 DLL / 未注入）→ 回退旧 remote_call
    except Exception as e:
        log(f"队内控 [发] mailbox 异常: {e}")
    return _send_via_wrapper_legacy(pid, plaintext, log=log)


def _send_via_wrapper_legacy(pid: int, plaintext: bytes, *, log: LogFn | None = None) -> dict:
    """旧版：远程线程 thiscall 调 wrapper（栈帧不匹配，不可靠）。保留作回退。@author by ak"""
    log = log or (lambda _m: None)
    import struct
    from ctypes import wintypes

    try:
        from app.core.automove import remote_call_thiscall_x86
        from app.core.remote_runtime import (
            MEM_COMMIT,
            MEM_RESERVE,
            PAGE_EXECUTE_READWRITE,
            kernel32,
            open_process,
            write_process,
        )
    except Exception as e:
        log(f"队内控 [发] 导入远程调用失败: {e}")
        return {"ok": False, "error": f"import: {e}"}

    send_mgr = resolve_send_mgr(pid, log=log)
    wrapper = _note_to_live(pid, TEAM_CHAT_WRAPPER_NOTE_VA)
    vtable = _note_to_live(pid, OCTETS_VTABLE_NOTE_VA)
    if not wrapper:
        log("队内控 [发] 无法解析聊天包装地址")
        return {"ok": False, "error": "wrapper unresolved"}
    if not send_mgr:
        log("队内控 [发] 发送管理器精确扫描未命中（legacy 回退不可用）")
        return {"ok": False, "error": "send_mgr not captured"}

    handle = open_process(pid)
    obj = 0
    try:
        obj = int(
            kernel32.VirtualAllocEx(
                wintypes.HANDLE(handle), None, 0x1000,
                MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE,
            ) or 0
        )
    except Exception as e:
        log(f"队内控 [发] 分配远程内存失败: {e}")
        return {"ok": False, "error": f"alloc: {e}"}
    if not obj:
        return {"ok": False, "error": "VirtualAllocEx failed"}
    try:
        data_addr = obj + 0x200
        write_process(handle, data_addr, plaintext)
        write_process(handle, obj + 0x00, struct.pack("<I", vtable))
        write_process(handle, obj + 0x04, struct.pack("<I", data_addr))
        write_process(handle, obj + 0x08, struct.pack("<I", data_addr + len(plaintext)))
        write_process(handle, obj + 0x0C, struct.pack("<I", len(plaintext)))
        ret = remote_call_thiscall_x86(
            pid, wrapper, send_mgr, [obj], timeout_ms=4000
        )
        log(f"队内控 [发] legacy wrapper call ret={ret} mgr=0x{send_mgr:X}")
        return {"ok": True, "ret": int(ret or 0), "mgr": send_mgr, "obj": obj}
    except Exception as e:
        log(f"队内控 [发] 远程调用失败: {e}")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 发送节流队列（游戏聊天窗口限频）
# ---------------------------------------------------------------------------

_SEND_LOCK = threading.RLock()
_SEND_QUEUES: dict[int, list[tuple[str, LogFn]]] = {}
_SEND_WORKERS: dict[int, threading.Thread] = {}
_SEND_WINDOW_TS: dict[int, float] = {}
_SEND_WINDOW_CNT: dict[int, int] = {}


def _queue_for(pid: int) -> list:
    with _SEND_LOCK:
        q = _SEND_QUEUES.get(pid)
        if q is None:
            q = []
            _SEND_QUEUES[pid] = q
        return q


# 角色身份指纹/role_id 进程级缓存（pid → 值）。像角色名缓存一样"来了就缓存"，
# 避免每次发送都调 GetHostPlayer（CRT 成本 + 场景门可能拒绝）。正式流程可在
# 角色绑定后预置（cache_role_identity）；零散解析成功后也回写。
_IDENT_CACHE: dict[int, tuple[int, int, int]] = {}
_RID_CACHE: dict[int, int] = {}
_IDENT_CACHE_LOCK = threading.RLock()

# 调度中心 SessionStore（app_shell 初始化后注入）。team_chat 从调度中心拿 role_id，
# 拿不到时读游戏并同步回写；无 store 时跳过该源（与旧行为一致）。
_SESSION_STORE: object | None = None
_SESSION_STORE_LOCK = threading.RLock()


def set_session_store(store: object) -> None:
    """注入调度中心 SessionStore（app_shell 创建后调用一次）。@author by ak"""
    global _SESSION_STORE
    with _SESSION_STORE_LOCK:
        _SESSION_STORE = store


def _get_session_store() -> object | None:
    """返回已注入的 SessionStore；无则 None。@author by ak"""
    with _SESSION_STORE_LOCK:
        return _SESSION_STORE


def _now_tick_ms() -> int:
    """GetTickCount（毫秒，与游戏进程同 boot 基准，用于对比消息 tick_ms）。@author by ak"""
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF)
    except Exception:
        return 0


def _msg_is_stale(tick_ms: int, *, now_ms: int | None = None) -> bool:
    """消息 tick_ms 距今是否超过 TEAM_MSG_MAX_AGE_S（32 位回绕安全）。@author by ak"""
    tick = int(tick_ms or 0)
    if not tick:
        return False
    now = int(now_ms) if now_ms is not None else _now_tick_ms()
    if not now:
        return False
    age = (int(now) - tick) & 0xFFFFFFFF
    return age > int(TEAM_MSG_MAX_AGE_S * 1000)


def cache_role_identity(pid: int, role_id: int) -> None:
    """预置/回写 pid 的角色身份缓存（role_id + 身份指纹）。@author by ak"""
    pid = int(pid or 0)
    rid = int(role_id or 0) & 0xFFFFFFFF
    if not pid or not rid:
        return
    with _IDENT_CACHE_LOCK:
        _RID_CACHE[pid] = rid
        _IDENT_CACHE[pid] = _role_id_to_ident(rid)


def _cached_send_ident(pid: int) -> tuple[int, int, int] | None:
    """读进程级缓存的身份指纹；无返回 None。@author by ak"""
    with _IDENT_CACHE_LOCK:
        return _IDENT_CACHE.get(int(pid or 0))


def _cached_role_id(pid: int) -> int | None:
    """读进程级缓存的 role_id（用于计算封包 offset 6 身份高位）；无返回 None。@author by ak"""
    with _IDENT_CACHE_LOCK:
        return _RID_CACHE.get(int(pid or 0))


def _role_id_high_byte(role_id: int) -> int:
    """role_id → 聊天封包 offset 6 字节（身份高位，`(rid>>24)&0xFF`）。

    实机抓包（2026-08-16）：张三 0x01001001→01，李四 0x00004001→00。
    与身份三字节合起来 = offset 6..9 = (rid>>24, rid>>16, rid>>8, 0x01)。
    @author by ak
    """
    return (int(role_id or 0) & 0xFFFFFFFF) >> 24


def _role_id_to_ident(role_id: int) -> tuple[int, int, int]:
    """role_id → 聊天封包身份指纹（offset 7..9 三字节）。

    实机验证（2026-08-16）：张三 0x01001001→2b 40 01，王五 0x01002001→2b a0 01，
    赵六 0x01003001→30 30 01，李四 0x00004001→09 e0 01。
    ident = ((rid>>16)&0xFF, (rid>>8)&0xFF, 0x01)。第三字节恒为 0x01（早期误用
    (rid>>24)&0xFF，因前三个号高字节恰为 0x01 而掩盖）。
    @author by ak
    """
    rid = int(role_id or 0) & 0xFFFFFFFF
    return (
        (rid >> 16) & 0xFF,
        (rid >> 8) & 0xFF,
        0x01,
    )


def _resolve_send_ident_confirmed(pid: int) -> tuple[tuple[int, int, int], bool]:
    """解析当前角色聊天身份指纹 + 是否"真正确认"。

    优先级与 _resolve_send_ident 相同：
      1. 进程级缓存（_IDENT_CACHE，正式绑定预置或零散解析后回写）
      2. SessionStore（调度中心）缓存的 role_id（正式注入后绑定，最可靠）
      3. GetHostPlayer（skip_scene_gate，纯 UI 查询）+ host+0x140 读 role_id
      4. team_tap 共享内存 send_ident（DLL 捕获，需 DLL 正常 ACTIVE）
    前 4 源任一命中返回 (ident, True)；全失败返回 (默认张三, False)。
    confirmed=False 时调用方不应发送（避免用错身份被服务端丢弃）。
    @author by ak
    """
    pid = int(pid or 0)
    # 1) 进程级缓存（最快，无 CRT）。
    cached = _cached_send_ident(pid)
    if cached:
        return cached, True
    # 2) SessionStore 缓存 role_id（调度中心正式数据，app_shell 注入的 store）。
    try:
        store = _get_session_store()
        if store is not None:
            sess = store.get(pid)
            if sess is not None:
                rid = str(getattr(sess, "role_id", "") or "").strip()
                if rid:
                    try:
                        rid_int = int(rid, 0)
                    except ValueError:
                        rid_int = 0
                    if rid_int:
                        cache_role_identity(pid, rid_int)
                        return _role_id_to_ident(rid_int), True
    except Exception:
        pass
    # 3) GetHostPlayer（skip_scene_gate，纯 UI 查询）+ host+0x140 读 role_id。
    try:
        from app.core.loot import open_attach_session
        from app.core.plg_ui import EXPORT_GET_HOST_PLAYER, _resolve_va
        from app.core.remote_runtime import (
            remote_call_cdecl_x86,
            remote_read_bytes,
        )

        attach = open_attach_session(pid, log=lambda _m: None)
        if attach is not None:
            try:
                va = _resolve_va(attach, EXPORT_GET_HOST_PLAYER)
                ret = remote_call_cdecl_x86(
                    pid, va, [], timeout_ms=2500, skip_scene_gate=True
                )
                host = int(ret) & 0xFFFFFFFF
                if host:
                    raw = remote_read_bytes(pid, host + 0x140, 8)
                    lo = int.from_bytes(raw[0:4], "little")
                    hi = int.from_bytes(raw[4:8], "little")
                    oid = (int(hi) << 32) | int(lo)
                    if oid:
                        cache_role_identity(pid, int(oid & 0xFFFFFFFF))
                        # 同步一次调度中心：把 role_id 写回 SessionStore，
                        # 后续发送/主副控都能从调度中心直接拿到（无需再 attach）。
                        try:
                            store = _get_session_store()
                            if store is not None:
                                sess2 = store.get(pid)
                                if sess2 is not None and not str(
                                    getattr(sess2, "role_id", "") or ""
                                ).strip():
                                    sess2.role_id = str(int(oid & 0xFFFFFFFF))
                        except Exception:
                            pass
                        return _role_id_to_ident(oid), True
            finally:
                try:
                    attach.close()
                except Exception:
                    pass
    except Exception:
        pass
    # 4) DLL 捕获的 ident（需 DLL 正常）。
    try:
        tt = _team_tap_read(pid)
        if (
            int(tt.get("status") or 0) == TEAM_TAP_ACTIVE
            and tt.get("send_mgr")
            and tt.get("send_ident_seen")
        ):
            return tt.get("send_ident") or (0x2B, 0x40, 0x01), True
    except Exception:
        pass
    return (0x2B, 0x40, 0x01), False


def _send_ident_confirmed(pid: int) -> bool:
    """身份指纹是否已可靠解析（可安全发送）。@author by ak"""
    try:
        _, ok = _resolve_send_ident_confirmed(pid)
        return ok
    except Exception:
        return False


def _resolve_send_ident(pid: int) -> tuple[int, int, int]:
    """解析当前角色聊天身份指纹。

    优先级：
      1. 进程级缓存（_IDENT_CACHE，正式绑定预置或零散解析后回写）
      2. SessionStore（调度中心）缓存的 role_id（正式注入后绑定，最可靠）
      3. GetHostPlayer（skip_scene_gate，纯 UI 查询）+ host+0x140 读 role_id
      4. team_tap 共享内存 send_ident（DLL 捕获，需 DLL 正常 ACTIVE）
    解析成功（来源 2/3）回写进程级缓存，避免重复 CRT。
    返回 (i0, i1, i2)；全失败回退张三默认 (0x2B, 0x40, 0x01)。
    @author by ak
    """
    ident, _ok = _resolve_send_ident_confirmed(pid)
    return ident


# 角色名进程级缓存（pid → 名字），私聊封包需要双方名字字段。
_NAME_CACHE: dict[int, str] = {}


def cache_role_name(pid: int, name: str) -> None:
    """预置/回写 pid 的角色名缓存（正式绑定后调用可省一次 CRT）。@author by ak"""
    pid = int(pid or 0)
    name = str(name or "").strip()
    if pid and name:
        _NAME_CACHE[pid] = name


def _resolve_private_sender(pid: int, *, log: LogFn | None = None) -> tuple[int, str | None]:
    """解析私聊发送方身份 (role_id, name)。

    rid 复用队伍封包的解析链（_resolve_send_ident_confirmed 会回写 _RID_CACHE）；
    name 走 GetHostPlayer + GetObjectName（CRT，调度门保护），成功后缓存。
    @author by ak
    """
    log = log or (lambda _m: None)
    rid = _cached_role_id(pid)
    if not rid:
        # 触发完整解析链（内部命中后回写 _RID_CACHE）。
        try:
            _resolve_send_ident_confirmed(pid)
        except Exception:
            pass
        rid = _cached_role_id(pid)
    name = _NAME_CACHE.get(int(pid or 0))
    if not name:
        try:
            from app.core.loot import open_attach_session
            from app.core.plg_ui import get_host_player_name

            attach = open_attach_session(pid, log=lambda _m: None)
            if attach is not None:
                name = get_host_player_name(attach, log=log)
                if name:
                    cache_role_name(pid, name)
        except Exception as e:
            log(f"私聊 [发] 解析角色名失败: {e}")
    return int(rid or 0), (name or None)


def send_private_message(
    pid: int,
    target_rid: int,
    target_name: str,
    text: str,
    *,
    log: LogFn | None = None,
) -> dict:
    """向指定角色发送私聊文本（0x60 封包，team_tap mailbox 通道）。

    未组队也可用 —— 跨设备自动整队前的控制面（预检查/离队通知）走这里。
    返回 {ok, ret, mgr?, error?}；ok=True 表示封包已交给游戏内发送链路
    （wrapper 返回 1），服务端是否投递以接收方 chat_tap ch=9 为准。
    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(pid or 0)
    if not pid:
        return {"ok": False, "error": "no pid"}
    target_rid = int(target_rid or 0) & 0xFFFFFFFF
    target_name = str(target_name or "").strip()
    text = str(text or "")
    if not target_rid or not target_name or not text:
        return {"ok": False, "error": "bad target/text"}
    rid, name = _resolve_private_sender(pid, log=log)
    if not rid:
        # 封包携带的 sender_rid 服务端会与会话校验；身份未就绪时发错包等于白发。
        log("私聊 [发] 发送方 role_id 未解析，拒绝发送")
        return {"ok": False, "error": "sender rid unresolved"}
    if not name:
        log("私聊 [发] 发送方名字未解析，拒绝发送")
        return {"ok": False, "error": "sender name unresolved"}
    try:
        payload = build_private_chat_c2s(text, rid, name, target_rid, target_name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    # 服务端对私聊按发送者限频：窗口满时阻塞等待（控制面消息不能丢）。
    _private_send_gate(pid, log=log)
    res = _team_mailbox_send(pid, payload, patch_ident=False, log=log)
    if res.get("ok"):
        log(
            f"私聊 [发] →{target_name}({target_rid:X}) {text!r} ok=True "
            f"ret={res.get('ret')} mgr=0x{res.get('mgr', 0):X}"
        )
    else:
        log(f"私聊 [发] →{target_name}({target_rid:X}) {text!r} ok=False {res.get('error')}")
    return res


def _start_sender(pid: int) -> None:
    with _SEND_LOCK:
        th = _SEND_WORKERS.get(pid)
        if th is not None and th.is_alive():
            return

        def run() -> None:
            while True:
                item = None
                with _SEND_LOCK:
                    q = _SEND_QUEUES.get(pid)
                    if q:
                        item = q.pop(0)
                    if item is None and not q:
                        # no pending + queue empty -> stop worker
                        _SEND_WORKERS.pop(pid, None)
                        return
                if item is None:
                    return
                if len(item) == 3:
                    text, log, retry_count = item
                else:
                    text, log = item
                    retry_count = 0
                retry = False
                try:
                    ident, confirmed = _resolve_send_ident_confirmed(pid)
                    if not confirmed:
                        # 身份未就绪：保留本条，稍后重试（避免用默认身份发错包）。
                        retry = True
                        if log is not None:
                            log(f"队内控 [发节流] {text!r} 身份未就绪，稍后重试")
                    else:
                        payload = build_team_chat_c2s(str(text), ident=ident)
                        res = _send_via_wrapper(pid, payload, log=log)
                        error = str(res.get("error") or "")
                        retry = not res.get("ok") and error in {
                            "send_mgr not captured",
                            "team_tap mapping missing",
                            "map view failed",
                            "mailbox busy",
                            "mailbox timeout",
                        }
                        if log is not None:
                            log(f"队内控 [发节流] {text!r} ok={res.get('ok')} {error}")
                except Exception as e:
                    retry = True
                    if log is not None:
                        log(f"队内控 [发节流] {text!r} 异常: {e}")
                if retry:
                    if retry_count < TEAM_SEND_RETRY_MAX:
                        with _SEND_LOCK:
                            q = _SEND_QUEUES.get(pid)
                            if q is not None:
                                q.insert(0, (text, log, retry_count + 1))
                    elif log is not None:
                        log(f"队内控 [发节流] {text!r} 重试{TEAM_SEND_RETRY_MAX}次仍未成功，丢弃")
                time.sleep(TEAM_SEND_INTERVAL_S)

        th = threading.Thread(target=run, name=f"team-chat-send-{pid}", daemon=True)
        _SEND_WORKERS[pid] = th
        th.start()


def _respect_window(pid: int) -> bool:
    """窗口限频：每 TEAM_SEND_WINDOW_S 最多 TEAM_SEND_WINDOW_MAX 条。
    达到上限返回 False（丢弃该条）。@author by ak"""
    with _SEND_LOCK:
        now = time.time()
        ts = _SEND_WINDOW_TS.get(pid, 0.0)
        cnt = _SEND_WINDOW_CNT.get(pid, 0)
        if now - ts >= TEAM_SEND_WINDOW_S:
            ts = now
            cnt = 0
        if cnt >= TEAM_SEND_WINDOW_MAX:
            return False
        cnt += 1
        _SEND_WINDOW_TS[pid] = ts
        _SEND_WINDOW_CNT[pid] = cnt
        return True


def send_team_message(pid: int, text: str, *, log: LogFn | None = None) -> dict:
    """向 pid 所属游戏角色的队伍频道发送一条文本消息（异步队列 + 节流）。

    返回 {ok, queued}：ok=True 表示已入队（窗口限频内），实际发送由后台
    worker 按固定间隔串行执行。窗口超限时返回 ok=False（丢弃）。
    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(pid or 0)
    if not pid:
        return {"ok": False, "error": "no pid"}
    text = str(text)
    # 新 PING 的 PONG 只保留最新一条，清掉尚未发送的旧心跳回执，
    # 避免发送管理器恢复后按历史顺序补发过期 PONG。
    if text.startswith(f"{SLAVE_TAG}{CMD_PONG}"):
        with _SEND_LOCK:
            q = _SEND_QUEUES.get(pid)
            if q:
                q.clear()
        if log is not None:
            log(f"队内控 [发节流] 新 PING 回执到达，清空旧待发送消息 pid={pid}")
    if not _respect_window(pid):
        log(f"队内控 [发节流] 限频窗口已满，丢弃 {text!r}")
        return {"ok": False, "error": "rate limited"}
    q = _queue_for(pid)
    if len(q) >= TEAM_SEND_QUEUE_MAX:
        q.pop(0)  # 丢最旧，保最新命令
    q.append((text, log))
    _start_sender(pid)
    return {"ok": True, "queued": True, "text": str(text)}


def reset_team_send_state(pid: int) -> None:
    """测试/重连辅助：清空 pid 的发送队列与窗口计数。@author by ak"""
    pid = int(pid or 0)
    with _SEND_LOCK:
        _SEND_QUEUES.pop(pid, None)
        _SEND_WINDOW_TS.pop(pid, None)
        _SEND_WINDOW_CNT.pop(pid, None)
        _SEND_WORKERS.pop(pid, None)


# ---------------------------------------------------------------------------
# 读取（被动 tap）
# ---------------------------------------------------------------------------

def ensure_team_reader(session) -> object | None:
    """打开（或复用）pid 的被动聊天 tap reader。@author by ak"""
    reader = getattr(session, "_team_chat_reader", None)
    if reader is not None:
        return reader
    try:
        from app.core.chat_tap import ChatTapReader

        pid = int(getattr(session, "pid", 0) or 0)
        if not pid:
            return None
        reader = ChatTapReader.open(pid)
        if reader is not None:
            setattr(session, "_team_chat_reader", reader)
        return reader
    except Exception:
        return None


def read_team_events(session, *, log: LogFn | None = None) -> list[dict]:
    """读取本会话新到的队伍频道控制消息（[主P]/[副G] 前缀），自动推进游标。

    返回按 parse_team_message 解析后的 dict 列表。
    @author by ak
    """
    log = log or (lambda _m: None)
    reader = ensure_team_reader(session)
    if reader is None:
        return []
    cursor = int(getattr(session, "_team_chat_cursor", 0) or 0)
    try:
        events, consumed, lost, _header = reader.read_after(cursor)
    except Exception as e:
        log(f"队内控 [读] tap read err: {e}")
        return []
    try:
        setattr(session, "_team_chat_cursor", int(consumed or 0))
    except Exception:
        pass
    if lost:
        log(f"队内控 [读] tap overrun lost={lost} cursor={cursor}->{consumed}")
    out: list[dict] = []
    for event in events or ():
        text = str(event.get("text") or "")
        if not text.strip():
            continue
        parsed = parse_team_message(text)
        if parsed is not None:
            parsed["seq"] = int(event.get("seq") or 0)
            parsed["channel"] = event.get("channel")
            out.append(parsed)
    return out


def reset_team_chat_cursor(session) -> None:
    """测试/重连辅助：清空会话级 tap 游标。@author by ak"""
    try:
        reader = getattr(session, "_team_chat_reader", None)
        if reader is not None:
            reader.close()
    except Exception:
        pass
    try:
        setattr(session, "_team_chat_reader", None)
        setattr(session, "_team_chat_cursor", 0)
    except Exception:
        pass


def read_team_events_pid(
    pid: int,
    *,
    cursor: int = 0,
    log: LogFn | None = None,
) -> tuple[list[dict], int]:
    """按 pid 读取新到的队伍频道控制消息（持久游标由调用方保存）。

    优先用 chat_tap 的队伍专用 ring（channel==3，大容量，稳定读取）；
    旧版 tap（无 team ring）则回退主 ring 并过滤 ch=3。
    返回 (parsed, consumed_cursor)。
    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(pid or 0)
    if not pid:
        return [], int(cursor or 0)
    try:
        from app.core.chat_tap import ChatTapReader

        reader = ChatTapReader.open(pid)
    except Exception as e:
        log(f"队内控 [读] open err: {e}")
        return [], int(cursor or 0)
    if reader is None:
        return [], int(cursor or 0)
    try:
        # 优先队伍专用 ring（新版 tap）。
        try:
            events, consumed, lost, _header = reader.read_team_after(int(cursor or 0))
        except Exception:
            # 旧版 tap（无 team ring）回退主 ring。
            events, consumed, lost, _header = reader.read_after(int(cursor or 0))
        if lost:
            log(f"队内控 [读] team tap overrun lost={lost} cursor={cursor}->{consumed}")
        out: list[dict] = []
        for event in events or ():
            text = str(event.get("text") or "")
            if not text.strip():
                continue
            parsed = parse_team_message(text)
            if parsed is not None:
                parsed["seq"] = int(event.get("seq") or 0)
                parsed["channel"] = event.get("channel")
                parsed["tick_ms"] = int(event.get("tick_ms") or 0)
                out.append(parsed)
        return out, int(consumed or int(cursor or 0))
    except Exception as e:
        log(f"队内控 [读] read err: {e}")
        return [], int(cursor or 0)
    finally:
        try:
            reader.close()
        except Exception:
            pass


__all__ = [
    "ACTION_TEXT",
    "ACK_VERB",
    "CMD_DONE",
    "CMD_FAIL",
    "CMD_PING",
    "CMD_PONG",
    "MASTER_TAG",
    "SLAVE_TAG",
    "TEAM_CHANNEL_ID",
    "TEAM_HEARTBEAT_S",
    "TEAM_ALIVE_WINDOW_S",
    "TEAM_MSG_MAX_AGE_S",
    "TeamMsgSeen",
    "action_to_text",
    "build_master_command",
    "build_master_ping",
    "build_master_jianglong_query",
    "build_slave_jianglong_status",
    "build_slave_pong",
    "build_team_chat_c2s",
    "ensure_team_reader",
    "ensure_team_tap",
    "extract_sender_name",
    "is_team_control_ready",
    "is_team_slave_isolated",
    "make_msg_id",
    "parse_team_message",
    "read_send_mgr_from_shm",
    "read_team_events",
    "reset_team_chat_cursor",
    "reset_team_send_state",
    "resolve_send_mgr",
    "send_team_message",
    "split_msg_id",
    "team_control_flag_enabled",
    "text_to_action",
]

