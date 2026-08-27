# -*- coding: utf-8 -*-
"""
Chat history rich-text buffer reader.

Live findings (2026-07-20, dig3):
- Rendered chat is a high-heap rich-text history blob (often 0x7c......).
- Message rail form:
    ^{rail_color}\\uE000<3><0><channel_id><1.000000><channel_id>^{body...}
- channel_id (3rd/5th angle tag) is the stable channel enum:
    2  -> 附近   rail aaf6f6
    3  -> 队伍   rail 9bde65
    5  -> 世界   rail fc814a
    10 -> 系统   rail 9dbfde
  (其他 / 帮派 / ... 待更多 live 样本补全)
- Body markup: ^RRGGBB colors, ^u&name&^u, <T0 itemId ...>
- No per-line wall-clock / server msg-id in the rich text itself.
- Identity (memory-level):
    * content_key = hash(channel_id|text|item_ids) — stable across buffer move / 切图
    * mem_key = addr_page + content_key — unique instance in one snapshot
    * absolute addr unique along one live hist blob, but blob relocates
    * residual/catalog copies of same text get different mem_key, not new events alone
    * display: keep distinct addr baseline + mild residual triple collapse (step<=0x40, span<=0x70)
- Freshness:
    * within one snapshot: buffer order / seq (越大越新); dump keeps newest tail
    * across snapshots: ordered suffix overlap feeds a session-local append journal
    * consumers use a monotonic cursor; plain/hitmap residuals never create events
    * no wall-clock expire field; live UI = whatever is still in the buffer
    * client ring counter/message id is still not available

@author by ak
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable

LogFn = Callable[[str], None]

# Live-confirmed channel_id (tag3) -> label.
_CHANNEL_ID_MAP: dict[int, str] = {
    2: "附近",
    3: "队伍",
    4: "帮派",  # live 2026-07-20 mem-dump: 帮派活动倍率
    5: "世界",
    10: "系统",
    11: "其他",  # live 2026-07-20: 神罚 / 进本拦截, rail dadad9
    # ?: "帮派", ?: "综合", ?: "当前"
}

# Rail color fallback when tag3 missing / unknown.
_COLOR_CHANNEL = {
    "9dbfde": "系统",
    "dadad9": "其他",
    "aaf6f6": "附近",
    "b7dfe7": "附近",
    "fc814a": "世界",
    "ff0000": "世界",
    "00ff40": "附近",
    "ffff00": "世界",
    "9bde65": "队伍",
    "ffdc8a": "系统",
    "e1519c": "系统",
    "e63e57": "系统",
    "ce58ff": "系统",
    "ffffff": "系统",
}

_RAIL_RE = re.compile(
    r"\^([0-9a-fA-F]{6})\uE000((?:<[^>\r\n]{1,48}>){1,12})",
    re.I,
)



def make_chat_identity(
    *,
    text: str,
    addr: int = 0,
    channel_id: int | None = -1,
    item_ids: tuple | list | None = None,
    color: str = "",
    seq: int = -1,
    source: str = "",
    ctx_hash: str = "",
) -> dict:
    """
    Build stable / memory-level identity keys for one chat line.

    Findings (live dig3 2026-07-20):
      - Rich rail has NO server msg-id / wall-clock field.
        tags = <3><0><channel_id><1.000000><channel_id> only.
      - Absolute addr is unique *within one live hist blob snapshot*
        (rail starts increase along the buffer), but the whole blob
        relocates (0x7C...... moves) — NOT stable across long sessions.
      - Same plain text may exist at many residual/catalog addrs
        (e.g. 答案正确 at 0x7c.. live + 0x59.. UI pool).
      - 切图 only inserts another system line ("截图已保存到..."); it does
        not rewrite neighboring bodies. content_key ignores neighbors.
      - item template ids (<T0 n>) help disambiguate loot lines.

    Keys:
      content_key : sha1(channel_id|text|item_ids)[:12]  — event content
      mem_key     : {addr_page:08X}:{content_key8}       — memory instance
      uid         : mem_key if addr else content_key[:12]
      ctx_hash    : optional surrounding-bytes hash (hitmap path)

    @author by ak
    """
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    try:
        cid = int(channel_id) if channel_id is not None and str(channel_id) != "" else -1
    except Exception:
        cid = -1
    items = tuple(int(x) for x in (item_ids or ()) if str(x).strip() != "")
    payload = f"{cid}|{t}|{','.join(str(i) for i in items)}".encode("utf-8", "ignore")
    content_key = hashlib.sha1(payload).hexdigest()[:12]
    try:
        a = int(addr or 0) & 0xFFFFFFFF
    except Exception:
        a = 0
    # 32-byte page keeps near-duplicate residual collapses stable enough,
    # while still separating distinct rails a few dozen bytes apart.
    # Use full addr (not coarse page) so consecutive same-text lines get distinct uids.
    page = a & 0xFFFFFFFF if a else 0
    mem_key = f"{page:08X}:{content_key[:8]}" if a else f"NOADDR:{content_key[:8]}"
    # Prefer memory instance for hist; content for pure catalog hits.
    src = str(source or "")
    if a and (src.startswith("hist") or src.startswith("hitmap") or src.startswith("heap") or src.startswith("dlg") or not src):
        uid = mem_key
    else:
        uid = content_key
    out = {
        "content_key": content_key,
        "mem_key": mem_key,
        "uid": uid,
        "addr_page": page,
    }
    if ctx_hash:
        out["ctx_hash"] = str(ctx_hash)
    if seq is not None and int(seq) >= 0:
        # soft order hint — not unique alone (renumbered on dump)
        out["seq_hint"] = int(seq)
    if color:
        out["color"] = str(color)
    return out


@dataclass
class ChatHistoryLine:
    channel: str
    text: str
    seq: int
    addr: int
    color: str = ""
    channel_id: int | None = None
    tags: tuple[str, ...] = ()
    item_ids: tuple[int, ...] = ()
    raw: str = ""
    source: str = "hist:rich"
    label_src: str = "tag3"
    # Identity (see make_chat_identity):
    #   content_key — text-level, stable across buffer move / 切图
    #   mem_key     — memory instance, unique in one snapshot (addr page + content)
    #   uid         — preferred handle = mem_key when addr known else content_key
    content_key: str = ""
    mem_key: str = ""
    uid: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        # keep JSON friendly
        if d.get("channel_id") is None:
            d["channel_id"] = -1
        # fill identity lazily if missing
        if not d.get("uid") or not d.get("content_key"):
            ident = make_chat_identity(
                text=str(d.get("text") or ""),
                addr=int(d.get("addr") or 0),
                channel_id=d.get("channel_id"),
                item_ids=d.get("item_ids") or (),
                color=str(d.get("color") or ""),
                seq=int(d.get("seq") if d.get("seq") is not None else -1),
                source=str(d.get("source") or ""),
            )
            d.update(ident)
        return d


class ChatMessageBuffer:
    """Session-local append-only journal reconciled from ordered chat snapshots."""

    def __init__(self, max_messages: int = 512):
        self.max_messages = max(32, int(max_messages))
        self._lock = threading.RLock()
        self._events: deque[dict] = deque(maxlen=self.max_messages)
        self._snapshot: list[dict] = []
        self._initialized = False
        self._next_cursor = 1
        self._reset_count = 0

    @staticmethod
    def _normalize(rows: list[dict] | tuple[dict, ...] | None) -> list[dict]:
        out: list[dict] = []
        for raw in rows or ():
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
            if not text:
                continue
            row["text"] = text
            if not row.get("content_key") or not row.get("mem_key"):
                row.update(
                    make_chat_identity(
                        text=text,
                        addr=int(row.get("addr") or 0),
                        channel_id=row.get("channel_id", -1),
                        item_ids=row.get("item_ids") or (),
                        color=str(row.get("color") or ""),
                        seq=int(row.get("seq") if row.get("seq") is not None else -1),
                        source=str(row.get("source") or "hist:rich"),
                    )
                )
            out.append(row)
        return out

    @staticmethod
    def _tokens(rows: list[dict], key: str) -> list[str]:
        return [str(row.get(key) or "") for row in rows]

    @staticmethod
    def _contains(haystack: list[str], needle: list[str]) -> bool:
        if not needle or len(needle) > len(haystack):
            return False
        n = len(needle)
        return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))

    @staticmethod
    def _suffix_overlap(previous: list[str], current: list[str]) -> tuple[int, int]:
        """Return (length, current_start) for the longest previous-tail overlap."""
        for size in range(min(len(previous), len(current)), 0, -1):
            tail = previous[-size:]
            for start in range(0, len(current) - size + 1):
                if current[start : start + size] == tail:
                    return size, start
        return 0, -1

    @property
    def latest_cursor(self) -> int:
        with self._lock:
            return self._next_cursor - 1

    def prime(self, rows: list[dict] | tuple[dict, ...] | None) -> int:
        """Set an observation baseline without replaying historical messages."""
        current = self._normalize(rows)
        with self._lock:
            self._snapshot = current
            self._initialized = True
            return self._next_cursor - 1

    def ingest(self, rows: list[dict] | tuple[dict, ...] | None) -> list[dict]:
        """Append only the suffix proven new by ordered snapshot overlap."""
        current = self._normalize(rows)
        if not current:
            return []
        with self._lock:
            if not self._initialized:
                self._snapshot = current
                self._initialized = True
                return []

            previous = self._snapshot
            if not previous:
                # A failed/empty baseline cannot prove that any current row is new.
                self._snapshot = current
                self._reset_count += 1
                return []

            prev_content = self._tokens(previous, "content_key")
            cur_content = self._tokens(current, "content_key")
            prev_mem = self._tokens(previous, "mem_key")
            cur_mem = self._tokens(current, "mem_key")

            if cur_content == prev_content:
                # Refresh physical identities after a buffer relocation.
                self._snapshot = current
                return []

            # A partial scan of an already-known window must not move the baseline
            # backwards; doing so would replay its missing tail on the next scan.
            if self._contains(prev_content, cur_content):
                return []

            shared_mem = bool(set(prev_mem) & set(cur_mem))
            if shared_mem:
                overlap, start = self._suffix_overlap(prev_mem, cur_mem)
            else:
                overlap, start = self._suffix_overlap(prev_content, cur_content)

            if overlap <= 0:
                # Scanner switched to another blob or the buffer was replaced.
                # Re-baseline rather than replaying an unproven history window.
                self._snapshot = current
                self._reset_count += 1
                return []

            fresh_rows = current[start + overlap :]
            self._snapshot = current
            appended: list[dict] = []
            observed_mono = time.monotonic()
            observed_at = time.time()
            for row in fresh_rows:
                event = dict(row)
                event["cursor"] = self._next_cursor
                event["observed_mono"] = observed_mono
                event["observed_at"] = observed_at
                self._next_cursor += 1
                self._events.append(event)
                appended.append(dict(event))
            return appended

    def read_after(self, cursor: int) -> list[dict]:
        with self._lock:
            mark = int(cursor or 0)
            return [dict(event) for event in self._events if int(event["cursor"]) > mark]

    def stats(self) -> dict:
        with self._lock:
            return {
                "cursor": self._next_cursor - 1,
                "retained": len(self._events),
                "snapshot_rows": len(self._snapshot),
                "resets": self._reset_count,
            }


def get_chat_message_buffer(session, *, max_messages: int = 512) -> ChatMessageBuffer:
    """Return the append-only chat journal owned by one attach session."""
    current = getattr(session, "_chat_message_buffer", None)
    if isinstance(current, ChatMessageBuffer):
        return current
    current = ChatMessageBuffer(max_messages=max_messages)
    setattr(session, "_chat_message_buffer", current)
    return current


def _strip_rich(s: str) -> str:
    s = str(s or "")
    s = re.sub(r"\^u&([^&]+)&\^u", r"\1", s)
    s = re.sub(r"\^[0-9a-fA-F]{6}", "", s)
    s = re.sub(r"\^[A-Za-z][^\s^]*", "", s)
    s = re.sub(r"<T0[^>]*>", "[item]", s)
    s = re.sub(r"<T0[^>]*$", "[item]", s)  # truncated tag at window edge
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"<[^>]*$", "", s)
    s = re.sub(r"[\uE000-\uF8FF]", "", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    s = s.replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _infer_channel_by_text(text: str) -> tuple[str, str] | None:
    """Semantic fallback when tag3/color unknown. @author by ak"""
    t = text or ""
    # 答案反馈 is 系统(tag3=10). Check BEFORE 其他 keys; "请尽快进入活动" alone is truncated 答案正确.
    if any(k in t for k in ("答案正确", "答案错误", "请尽快进入活动", "请大侠重新来过")):
        return "系统", "infer"
    if any(k in t for k in ("神罚", "捕羽")) and "答案" not in t:
        return "其他", "infer"
    if any(k in t for k in ("不能进入副本", "不稳定状态", "不稳定状态下无法")):
        return "其他", "infer"
    if any(k in t for k in ("你获得了", "你得到了", "获得了", "上次登录时间", "您成功加入了队伍")):
        return "系统", "infer"
    if any(k in t for k in ("江湖快报", "小道消息", "通告：", "已刷新", "超级BOSS")):
        return "世界", "infer"
    if any(k in t for k in ("组队", "队伍分配", "加入了队伍", "离开了队伍")):
        return "队伍", "infer"
    if any(k in t for k in ("击杀了", "击败了", "并且拿到了", "在天下会", "完成了任务")):
        # nearby activity style (tag3=2 live); not world broadcast
        return "附近", "infer"
    if re.search(r"[\u4e00-\u9fffA-Za-z0-9_·\-]{2,14}[：:]", t):
        return "世界", "infer"
    return None


def _channel_from_rail(
    *,
    channel_id: int | None,
    rail_color: str,
    text: str,
) -> tuple[str, str, int | None]:
    """
    Resolve channel label.

    Priority: tag3 id -> rail color -> text semantics.

    @author by ak
    """
    if channel_id is not None and int(channel_id) in _CHANNEL_ID_MAP:
        return _CHANNEL_ID_MAP[int(channel_id)], "tag3", int(channel_id)

    # Unknown id still returned for UI debugging.
    col = (rail_color or "").lower()
    if col in _COLOR_CHANNEL:
        return _COLOR_CHANNEL[col], "color", channel_id

    inferred = _infer_channel_by_text(text)
    if inferred:
        return inferred[0], inferred[1], channel_id
    if channel_id is not None:
        return f"ch{int(channel_id)}", "tag3?", int(channel_id)
    return "未知", "infer", channel_id


def _parse_tags(tag_blob: str) -> tuple[list[str], int | None]:
    tags = re.findall(r"<([^>]+)>", tag_blob or "")
    cid = None
    if len(tags) >= 3:
        try:
            # 3rd tag is channel id; 5th mirrors it when present.
            cid = int(float(tags[2]))
        except Exception:
            cid = None
        if cid is None and len(tags) >= 5:
            try:
                cid = int(float(tags[4]))
            except Exception:
                cid = None
    return tags, cid



def _extract_feedback_plain_lines(txt: str, base_addr: int = 0) -> list[ChatHistoryLine]:
    """
    Fallback: pull exact feedback sentences from non-rail windows (AUI pools).

    @author by ak
    """
    if not txt:
        return []
    # value = canonical display text
    keys = (
        ("答案正确，请尽快进入活动", "答案正确，请尽快进入活动"),
        ("答案错误，请大侠重新来过", "答案错误，请大侠重新来过"),
        ("队伍成员处于神罚状态，不能进入副本", "队伍成员处于神罚状态，不能进入副本"),
        ("队伍成员处于捕羽状态，不能进入副本", "队伍成员处于捕羽状态，不能进入副本"),
        ("不稳定状态下无法进行此操作", "不稳定状态下无法进行此操作"),
        ("入口未打开，目前不能进入", "入口未打开，目前不能进入"),
        ("处于神罚状态，不能进入副本", "队伍成员处于神罚状态，不能进入副本"),
        ("处于捕羽状态，不能进入副本", "队伍成员处于捕羽状态，不能进入副本"),
    )
    out: list[ChatHistoryLine] = []
    seq = 0
    for needle, canon in keys:
        start = 0
        while True:
            i = txt.find(needle, start)
            if i < 0:
                break
            k = canon
            ch, src, cid = _channel_from_rail(channel_id=None, rail_color="", text=k)
            if k.startswith("答案"):
                ch, src, cid = "系统", "plain", 10
            elif "神罚" in k or "捕羽" in k:
                ch, src, cid = "其他", "plain", 11
            elif "不稳定" in k or "入口未打开" in k:
                ch, src, cid = "其他", "plain", 11
            out.append(
                ChatHistoryLine(
                    channel=ch,
                    text=k,
                    seq=seq,
                    addr=(int(base_addr) + i * 2) & 0xFFFFFFFF,
                    color="dadad9" if cid == 11 else "9dbfde",
                    channel_id=cid,
                    source="hist:plain",
                    label_src=src,
                    raw=k,
                )
            )
            seq += 1
            start = i + len(needle)
    return out


def _parse_rich_blob(txt: str, base_addr: int = 0) -> list[ChatHistoryLine]:
    """
    Parse one rich-history window into lines using rail markers.

    @author by ak
    """
    if not txt:
        return []

    matches = list(_RAIL_RE.finditer(txt))
    lines: list[ChatHistoryLine] = []
    if not matches:
        # Fallback: bare E000 split (legacy / partial windows).
        parts = txt.split("\ue000")
        seq = 0
        for part in parts[1:] if len(parts) > 1 else parts:
            tags, cid = _parse_tags(part[:120])
            body = _strip_rich(part)
            if not _body_ok(body):
                continue
            ch, src, cid2 = _channel_from_rail(channel_id=cid, rail_color="", text=body)
            lines.append(
                ChatHistoryLine(
                    channel=ch,
                    text=body[:160],
                    seq=seq,
                    addr=int(base_addr) & 0xFFFFFFFF,
                    color="",
                    channel_id=cid2,
                    tags=tuple(f"<{t}>" for t in tags[:12]),
                    item_ids=tuple(int(x) for x in re.findall(r"<T0\s+(\d+)", part)[:8]),
                    raw=part[:200],
                    label_src=src,
                )
            )
            seq += 1
        return lines

    for i, m in enumerate(matches):
        rail = (m.group(1) or "").lower()
        tag_blob = m.group(2) or ""
        tags, cid = _parse_tags(tag_blob)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(txt), start + 240)
        body_raw = txt[start:end]
        # Keep only this message body; strip trailing next-rail leftovers already cut.
        item_ids = [int(x) for x in re.findall(r"<T0\s+(\d+)", body_raw)]
        body = _strip_rich(body_raw)
        if not _body_ok(body):
            continue
        # Hard clip glued multi-toast remnants.
        body = _clip_body(body)
        if not _body_ok(body):
            continue
        ch, src, cid2 = _channel_from_rail(channel_id=cid, rail_color=rail, text=body)
        # char offset -> rough byte addr (utf16)
        addr = (int(base_addr) + int(m.start()) * 2) & 0xFFFFFFFF
        lines.append(
            ChatHistoryLine(
                channel=ch,
                text=body[:160],
                seq=i,
                addr=addr,
                color=rail,
                channel_id=cid2,
                tags=tuple(f"<{t}>" for t in tags[:12]),
                item_ids=tuple(item_ids[:8]),
                raw=(m.group(0) + body_raw)[:220],
                label_src=src,
            )
        )
    return lines


def _body_ok(body: str) -> bool:
    if not body or len(body) < 2:
        return False
    if body.startswith("Win_") or "TaskName" in body or "><<" in body:
        return False
    quest_crumbs = ("地宫杀怪", "在线福利", "副本任务", "可以在游戏设置", "战斗辅助", "地宫BOSS>")
    if any(x in body for x in quest_crumbs):
        if not any(k in body for k in ("不能进入", "答案", "神罚", "捕羽")):
            return False
    if not any("一" <= c <= "鿿" for c in body):
        return False
    if "%" in body and any(x in body for x in ("%d", "%s", "%u", "%97")):
        return False
    bad = sum(1 for c in body if ord(c) > 0xFF00 or (0xE000 <= ord(c) <= 0xF8FF))
    if bad >= 2:
        return False
    cjk = sum(1 for c in body if "一" <= c <= "鿿")
    allowed_extra = set(",.!?:;[]()_+-=#@*&~/|\\" ) | set("，。！？、：；【】（）《》·—★☆")
    weird = 0
    for c in body:
        if "一" <= c <= "鿿" or c.isalnum() or c.isspace() or c in allowed_extra:
            continue
        if "㐀" <= c <= "䶿":
            weird += 2
            continue
        weird += 1
    if weird >= max(2, len(body) // 10):
        return False
    if cjk < 2:
        return False
    verbs = (
        "获得", "击杀", "拿到", "登录", "加入", "刷新", "快报", "消息", "通告",
        "队伍", "答案", "神罚", "捕羽", "离开", "完成了", "分配", "宝银", "幸运",
        "不能进入", "重新来过", "无法使用", "进入活动", "不稳定",
    )
    if not any(v in body for v in verbs):
        if cjk < 10 or not any(p in body for p in "，。！？、：；"):
            return False
    return True



def _clip_body(body: str) -> str:
    """Clip obvious glued next-message leftovers. @author by ak"""
    b = body
    # Exact captcha/feedback sentences win over AUI glue noise.
    for full in (
        "答案正确，请尽快进入活动",
        "答案错误，请大侠重新来过",
        "队伍成员处于神罚状态，不能进入副本",
        "队伍成员处于捕羽状态，不能进入副本",
        "不稳定状态下无法进行此操作",
        "入口未打开，目前不能进入",
    ):
        if full in b:
            return full
    # Prefer first complete loot sentence.
    m = re.match(r"(你获得了\d+个[\u4e00-\u9fffA-Za-z0-9_·]{1,24})", b)
    if m and len(b) > len(m.group(1)) + 6:
        tail = b[len(m.group(1)) :]
        if any(k in tail for k in ("击杀", "在福州", "坐标", "快捷", "TaskName", "设置", "你获得了", "江湖", "小道")):
            return m.group(1)
    # Cut at second player-event if glued.
    for key in ("你获得了", "江湖快报", "小道消息", "通告：", "上次登录时间"):
        p = b.find(key, 1)
        if p > 8:
            return b[:p].strip()
    return b[:160]




def _prefer_clean_feedback_lines(lines: list[ChatHistoryLine]) -> list[ChatHistoryLine]:
    """Drop contaminated rows that only embed a cleaner exact feedback sentence. @author by ak"""
    exact = {
        "答案正确，请尽快进入活动",
        "答案错误，请大侠重新来过",
        "队伍成员处于神罚状态，不能进入副本",
        "队伍成员处于捕羽状态，不能进入副本",
        "不稳定状态下无法进行此操作",
        "入口未打开，目前不能进入",
    }
    exact_present = {ln.text for ln in lines if ln.text in exact}
    out: list[ChatHistoryLine] = []
    for ln in lines:
        t = ln.text or ""
        if t in exact:
            out.append(ln)
            continue
        embedded = next((e for e in exact if e in t), None)
        if embedded is not None:
            if embedded in exact_present:
                continue
            ln.text = embedded
            if embedded.startswith("答案"):
                ln.channel, ln.channel_id, ln.color = "系统", 10, "9dbfde"
            elif "神罚" in embedded or "捕羽" in embedded or "不稳定" in embedded:
                ln.channel, ln.channel_id, ln.color = "其他", 11, "dadad9"
            ln.label_src = "clip"
            out.append(ln)
            exact_present.add(embedded)
            continue
        if any(k in t for k in ("答案", "神罚", "捕羽", "不稳定", "进入活动", "重新来过", "你获得了", "击杀了")):
            weird = sum(
                1
                for ch in t
                if (ord(ch) > 0xFF00)
                or (0xE000 <= ord(ch) <= 0xF8FF)
                or (0x3400 <= ord(ch) <= 0x4DBF)
            )
            if weird >= 3 and not any(e in t for e in exact):
                continue
            out.append(ln)
            continue
        weird = sum(
            1
            for ch in t
            if (ord(ch) > 0xFF00)
            or (0xE000 <= ord(ch) <= 0xF8FF)
            or (0x3400 <= ord(ch) <= 0x4DBF)
        )
        if weird >= 2:
            continue
        out.append(ln)
    return out




# Live residual pattern (2026-07-20): each rail often has 3 near copies
# with gaps ~0x2C / 0x38. Mild collapse only — keep the "full addr keep"
# baseline that already surfaces new lines; only strip local residual shadows.
_RESIDUAL_STEP = 0x50   # consecutive same-text gap treated as residual (live 0x4A doubles)
_RESIDUAL_SPAN = 0x90   # max span of one residual cluster (~triple/double)


def _line_pick_score(ln: "ChatHistoryLine") -> tuple:
    """Higher = better row inside a residual cluster."""
    src = str(getattr(ln, "source", "") or "")
    rich = 2 if src.startswith("hist:rich") else (1 if src.startswith("hist") else 0)
    try:
        cid_ok = 1 if ln.channel_id is not None and int(ln.channel_id) >= 0 else 0
    except Exception:
        cid_ok = 0
    tag = 1 if str(getattr(ln, "label_src", "") or "") in ("tag3", "color", "tag3?") else 0
    # Prefer lower addr in cluster (first rail) — higher-addr shadows were
    # residual; picking max addr made some refreshes look "stuck".
    addr = int(getattr(ln, "addr", 0) or 0) & 0xFFFFFFFF
    return (rich, cid_ok, tag, -addr)


def _drop_plain_covered_by_rich(
    lines: list["ChatHistoryLine"],
    *,
    near: int = 0x100,
) -> list["ChatHistoryLine"]:
    """If hist:rich already has same text nearby, drop hist:plain shadow."""
    if not lines:
        return []
    rich_addrs: dict[str, list[int]] = {}
    for ln in lines:
        src = str(getattr(ln, "source", "") or "")
        if not src.startswith("hist:rich"):
            continue
        t = str(getattr(ln, "text", "") or "").strip()
        if not t:
            continue
        rich_addrs.setdefault(t, []).append(int(getattr(ln, "addr", 0) or 0) & 0xFFFFFFFF)
    out: list[ChatHistoryLine] = []
    for ln in lines:
        src = str(getattr(ln, "source", "") or "")
        t = str(getattr(ln, "text", "") or "").strip()
        if src.startswith("hist:plain") and t in rich_addrs:
            a = int(getattr(ln, "addr", 0) or 0) & 0xFFFFFFFF
            if any(abs(a - ra) <= near for ra in rich_addrs[t]):
                continue
        out.append(ln)
    return out


def _collapse_residual_copies(
    lines: list["ChatHistoryLine"],
    *,
    step: int = _RESIDUAL_STEP,
    span: int = _RESIDUAL_SPAN,
) -> list["ChatHistoryLine"]:
    """
    Mild residual collapse on top of full-addr keep baseline.

    Only merges same-text copies that are consecutive within `step` and whose
    cluster span <= `span` (covers 0x2C+0x38 triples). Does NOT merge across
    larger gaps — real consecutive identical UI lines survive.
    @author by ak
    """
    if not lines:
        return []
    step = max(0x10, int(step))
    span = max(step, int(span))
    by_text: dict[str, list[ChatHistoryLine]] = {}
    for ln in lines:
        t = str(getattr(ln, "text", "") or "").strip()
        if not t:
            continue
        by_text.setdefault(t, []).append(ln)

    picked: list[ChatHistoryLine] = []
    for _t, group in by_text.items():
        group = sorted(
            group, key=lambda x: int(getattr(x, "addr", 0) or 0) & 0xFFFFFFFF
        )
        cur: list[ChatHistoryLine] = [group[0]]
        for ln in group[1:]:
            a0 = int(getattr(cur[0], "addr", 0) or 0) & 0xFFFFFFFF
            a_prev = int(getattr(cur[-1], "addr", 0) or 0) & 0xFFFFFFFF
            a = int(getattr(ln, "addr", 0) or 0) & 0xFFFFFFFF
            if a and a_prev and (a - a_prev) <= step and (a - a0) <= span:
                cur.append(ln)
            else:
                picked.append(max(cur, key=_line_pick_score))
                cur = [ln]
        picked.append(max(cur, key=_line_pick_score))

    picked.sort(key=lambda x: int(getattr(x, "addr", 0) or 0) & 0xFFFFFFFF)
    return picked


# Back-compat alias (older call sites / tests)
def _collapse_near_text_instances(
    lines: list["ChatHistoryLine"],
    *,
    near_gap: int = _RESIDUAL_STEP,
) -> list["ChatHistoryLine"]:
    return _collapse_residual_copies(lines, step=near_gap, span=max(near_gap * 2, _RESIDUAL_SPAN))


def _collapse_repeated_history_block(
    lines: list["ChatHistoryLine"],
    *,
    min_run: int = 5,
) -> list["ChatHistoryLine"]:
    """
    Drop a full repeated history block (live: same N texts twice, fixed addr delta).

    Example pid33820: seq0..8 identical to seq9..17 with +0x1110. Keep the later
    block only. Real loot cycles stay (宝银 amount changes => texts differ).
    @author by ak
    """
    if not lines or len(lines) < min_run * 2:
        return lines
    texts = [str(getattr(ln, "text", "") or "").strip() for ln in lines]
    n = len(texts)
    # Prefer longer runs; require exact text equality on the repeated span.
    best_k = 0
    for k in range(n // 2, min_run - 1, -1):
        # whole list is two copies
        if n >= 2 * k and texts[n - 2 * k : n - k] == texts[n - k :]:
            best_k = k
            break
        # prefix == following block (start-aligned double)
        if n == 2 * k and texts[:k] == texts[k:]:
            best_k = k
            break
    if best_k <= 0:
        return lines
    k = best_k
    # Keep prefix before the duplicated pair (if any) + the later copy.
    if n >= 2 * k and texts[n - 2 * k : n - k] == texts[n - k :]:
        head = lines[: n - 2 * k]
        tail = lines[n - k :]
        return list(head) + list(tail)
    if n == 2 * k and texts[:k] == texts[k:]:
        return list(lines[k:])
    return lines


def _rank_chat_region(base: int) -> tuple[int, int]:

    """
    Region priority for chat/feedback scans.

    Live rich history sits in high heap (often 0x7C......). Mid-heap
    0x38..0x5A holds sticky residual/catalog copies that rarely change —
    scanning mid-first makes the dev panel look "stuck on old messages".

    @author by ak
    """
    b = int(base) & 0xFFFFFFFF
    if 0x7C000000 <= b <= 0x7FF00000:
        return (0, -b)  # live hist first
    if 0x70000000 <= b < 0x7C000000:
        return (1, -b)
    if 0x48000000 <= b <= 0x5A000000:
        return (2, -b)  # AUI pools / residuals
    if 0x40000000 <= b < 0x48000000 or 0x5A000000 < b < 0x70000000:
        return (3, -b)
    if b >= 0x20000000:
        return (4, -b)
    return (5, -b)


def _scan_feedback_fast_blobs(
    session,
    *,
    budget_s: float = 0.45,
    max_regions: int = 3500,
    log: LogFn | None = None,
) -> list[ChatHistoryLine]:
    """
    Hot-path captcha feedback scanner.

    Only feedback needles; includes mid-heap AUI pools; plain+rail parse.

    @author by ak
    """
    log = log or (lambda _m: None)
    pm = getattr(session, "pm", None)
    if pm is None:
        return []
    try:
        import pymem.memory
        from app.core.game_attach import _iter_writable_regions
    except Exception as e:
        log(f"chat fb-fast: import fail {e}")
        return []

    needles = [
        "答案正确，请尽快进入活动".encode("utf-16le"),
        "答案错误，请大侠重新来过".encode("utf-16le"),
        "答案正确".encode("utf-16le"),
        "答案错误".encode("utf-16le"),
        "队伍成员处于神罚状态".encode("utf-16le"),
        "神罚状态".encode("utf-16le"),
        "捕羽状态".encode("utf-16le"),
        "不能进入副本".encode("utf-16le"),
        "不稳定状态下无法进行此操作".encode("utf-16le"),
        "入口未打开，目前不能进入".encode("utf-16le"),
        "入口未打开".encode("utf-16le"),
        "请尽快进入活动".encode("utf-16le"),
        "请大侠重新来过".encode("utf-16le"),
    ]
    end = time.monotonic() + max(0.12, float(budget_s))
    try:
        regions = list(_iter_writable_regions(pm, max_regions=max(800, int(max_regions))))
    except Exception as e:
        log(f"chat fb-fast: regions fail {e}")
        return []
    # Live hist first (0x7C), then mid residual — never mid-only.
    regions = [(int(b), int(sz)) for b, sz in regions if int(b) >= 0x04000000]
    regions.sort(key=lambda it: _rank_chat_region(it[0]))

    blobs: list[tuple[int, str]] = []
    seen: set[int] = set()
    blob_cap = 28
    for base, size in regions:
        if time.monotonic() >= end or len(blobs) >= blob_cap:
            break
        # Cap per-region work so one huge region cannot burn the budget.
        size = min(int(size), 0x400000)
        off = 0
        while off < size and time.monotonic() < end and len(blobs) < blob_cap:
            take = min(0x100000, size - off)
            addr = base + off
            try:
                chunk = pymem.memory.read_bytes(pm.process_handle, addr, take)
            except Exception:
                off += take
                continue
            for pat in needles:
                st = 0
                local = 0
                while local < 4 and time.monotonic() < end:
                    j = chunk.find(pat, st)
                    if j < 0:
                        break
                    left = max(0, j - 0x180)
                    right = min(len(chunk), j + 0x200)
                    if left % 2:
                        left -= 1
                    if right % 2:
                        right -= 1
                    key = (addr + left) & ~0x7F
                    if key not in seen:
                        seen.add(key)
                        try:
                            txt = chunk[left:right].decode("utf-16le", "ignore")
                        except Exception:
                            txt = ""
                        if txt:
                            blobs.append((addr + left, txt))
                    st = j + max(2, len(pat))
                    local += 1
            off += take

    all_lines: list[ChatHistoryLine] = []
    for baddr, txt in blobs:
        all_lines.extend(_parse_rich_blob(txt, base_addr=baddr))
        all_lines.extend(_extract_feedback_plain_lines(txt, base_addr=baddr))

    # Exact feedback sentences keep ALL addr instances (delta needs growth 1->2).
    # Other text still dedupe by body to avoid chat spam.
    exact_keys = set(FEEDBACK_COUNT_KEYS)
    exact_lines: list[ChatHistoryLine] = []
    other_best: dict[str, ChatHistoryLine] = {}
    for i, ln in enumerate(all_lines):
        ln.seq = i
        t = str(ln.text or "")
        canon = next((k for k in FEEDBACK_COUNT_KEYS if k == t or k in t), None)
        if canon is not None:
            if t != canon:
                ln.text = canon
            exact_lines.append(ln)
            continue
        prev = other_best.get(t)
        if prev is None or ln.seq >= prev.seq:
            other_best[t] = ln
    # Collapse exact lines only on identical (text, page-aligned addr)
    seen_exact: set[tuple[str, int]] = set()
    kept_exact: list[ChatHistoryLine] = []
    for ln in exact_lines:
        key = (str(ln.text), int(ln.addr) & ~0x1F)
        if key in seen_exact:
            continue
        seen_exact.add(key)
        kept_exact.append(ln)
    lines = kept_exact + list(other_best.values())
    lines = sorted(lines, key=lambda x: x.seq)
    for i, ln in enumerate(lines):
        ln.seq = i
    id_hist: dict[str, int] = {}
    for ln in lines:
        id_hist[ln.channel] = id_hist.get(ln.channel, 0) + 1
    lines = _prefer_clean_feedback_lines(lines)
    # prefer_clean may re-collapse text; re-expand exact by addr uniqueness
    final: list[ChatHistoryLine] = []
    seen2: set[tuple[str, int]] = set()
    for ln in lines:
        t = str(ln.text or "")
        if t in exact_keys:
            key = (t, int(ln.addr) & ~0x1F)
            if key in seen2:
                continue
            seen2.add(key)
        final.append(ln)
    lines = final
    for i, ln in enumerate(lines):
        ln.seq = i
    id_hist = {}
    for ln in lines:
        id_hist[ln.channel] = id_hist.get(ln.channel, 0) + 1
    log(
        f"chat fb-fast: blobs={len(blobs)} lines={len(lines)} "
        f"exact_inst={len(kept_exact)} channels={id_hist}"
    )
    return lines


def _scan_rich_history_blobs(
    session,
    *,
    budget_s: float = 1.8,
    max_lines: int = 200,
    log: LogFn | None = None,
    mode: str = "full",
) -> list[ChatHistoryLine]:
    """
    Memory-only live chat reader.

    Strategy (evidence-locked):
      1) Scan writable high-heap for rich windows (U+E000 rails).
      2) Pick ONE primary blob by parse quality (valid rail lines, high addr).
      3) Emit lines in rail order = left-bottom UI order.
      4) Optionally fill missing *exact* feedback canons from other high-heap
         plain hits (never phrase fragments).

    mode:
      - full: all channels from primary blob
      - feedback: only exact captcha/entry feedback sentences

    @author by ak
    """
    log = log or (lambda _m: None)
    pm = getattr(session, "pm", None)
    if pm is None:
        return []
    try:
        import pymem.memory
        from app.core.game_attach import _iter_writable_regions
    except Exception as e:
        log(f"chat hist: import fail {e}")
        return []

    _FB_CANON = (
        "答案正确，请尽快进入活动",
        "答案错误，请大侠重新来过",
        "队伍成员处于神罚状态，不能进入副本",
        "队伍成员处于捕羽状态，不能进入副本",
        "不稳定状态下无法进行此操作",
        "入口未打开，目前不能进入",
    )
    feedback_needles = [
        "答案正确，请尽快进入活动".encode("utf-16le"),
        "答案错误，请大侠重新来过".encode("utf-16le"),
        "答案正确".encode("utf-16le"),
        "答案错误".encode("utf-16le"),
        "队伍成员处于神罚状态".encode("utf-16le"),
        "神罚状态".encode("utf-16le"),
        "捕羽状态".encode("utf-16le"),
        "不能进入副本".encode("utf-16le"),
        "不稳定状态下无法进行此操作".encode("utf-16le"),
        "入口未打开，目前不能进入".encode("utf-16le"),
        "入口未打开".encode("utf-16le"),
        "请尽快进入活动".encode("utf-16le"),
        "请大侠重新来过".encode("utf-16le"),
    ]
    general_needles = [
        "你获得了".encode("utf-16le"),
        "击杀了".encode("utf-16le"),
        "并且拿到了".encode("utf-16le"),
        "\ue000<".encode("utf-16le"),
        "^9dbfde".encode("utf-16le"),
        "^aaf6f6".encode("utf-16le"),
        "^fc814a".encode("utf-16le"),
        "^dadad9".encode("utf-16le"),
        "^9bde65".encode("utf-16le"),
    ]

    end = time.monotonic() + max(0.25, float(budget_s))
    try:
        regions = list(
            _iter_writable_regions(pm, max_regions=max(3500, 4500))
        )
    except Exception as e:
        log(f"chat hist: regions fail {e}")
        return []
    regions = [(int(b), int(sz)) for b, sz in regions if int(b) >= 0x04000000]
    regions.sort(key=lambda it: _rank_chat_region(it[0]))

    blobs: list[tuple[int, str, int]] = []  # base, text, score
    seen_key: set[int] = set()

    def _push_window(abs_base: int, chunk: bytes, hit_at: int, *, wide: bool) -> None:
        if time.monotonic() >= end:
            return
        left = max(0, hit_at - (0x800 if wide else 0x600))
        right = min(len(chunk), hit_at + (0x5000 if wide else 0x3000))
        if left & 1:
            left -= 1
        if right & 1:
            right -= 1
        window = chunk[left:right]
        try:
            txt = window.decode("utf-16le", errors="ignore")
        except Exception:
            return
        if not txt or txt.count("%d") >= 6 and txt.count("\ue000") < 2:
            return
        # Must look like rich chat or exact feedback.
        has_rail = "\ue000" in txt and ("<" in txt)
        has_fb = any(c in txt for c in _FB_CANON) or any(
            k in txt
            for k in ("答案正确", "答案错误", "神罚状态", "捕羽状态", "不能进入副本")
        )
        has_chat = any(k in txt for k in ("你获得了", "击杀了", "宝银", "并且拿到了"))
        if not (has_rail or has_fb or has_chat):
            return
        rails = txt.count("\ue000")
        score = (
            rails * 5
            + txt.count("你获得了") * 3
            + txt.count("击杀了") * 3
            + txt.count("答案") * 6
            + txt.count("神罚") * 8
            + min(30, txt.count("^"))
        )
        baddr = (int(abs_base) + left) & 0xFFFFFFFF
        key = baddr & ~0xFFF
        if key in seen_key:
            for i, (ba, _tx, sc) in enumerate(blobs):
                if (ba & ~0xFFF) == key and score > sc:
                    blobs[i] = (baddr, txt, score)
                    return
            return
        if len(blobs) >= 24:
            # evict lowest score non-rail-heavy
            worst_i = min(range(len(blobs)), key=lambda i: blobs[i][2])
            if score <= blobs[worst_i][2]:
                return
            old = blobs.pop(worst_i)
            seen_key.discard(old[0] & ~0xFFF)
        seen_key.add(key)
        blobs.append((baddr, txt, score))

    def _scan(needles: list[bytes], *, wide: bool, until: float) -> None:
        for base, size in regions:
            if time.monotonic() >= until:
                break
            base = int(base)
            size = min(int(size), 0x600000)
            # Prefer high heap for live UI buffer
            if base < 0x20000000:
                continue
            off = 0
            while off < size and time.monotonic() < until:
                take = min(0x100000, size - off)
                addr = base + off
                try:
                    chunk = pymem.memory.read_bytes(pm.process_handle, addr, take)
                except Exception:
                    off += take
                    continue
                hits: list[int] = []
                for pat in needles:
                    st = 0
                    nloc = 0
                    while nloc < 8:
                        j = chunk.find(pat, st)
                        if j < 0:
                            break
                        if (j & 1) == 0:
                            hits.append(j)
                        st = j + max(2, len(pat))
                        nloc += 1
                for hit in sorted(set(hits)):
                    _push_window(addr, chunk, hit, wide=wide)
                off += take

    mode_l = str(mode or "full").strip().lower()
    feedback_only = mode_l in ("feedback", "fb", "captcha")
    t0 = time.monotonic()
    # Pass A: feedback windows (wide) — ensure 答案/神罚 pair stays together
    _scan(
        feedback_needles,
        wide=True,
        until=min(end, t0 + max(0.4, float(budget_s) * (0.7 if feedback_only else 0.4))),
    )
    fb_n = sum(
        1
        for _a, tx, _s in blobs
        if any(k in tx for k in ("答案", "神罚", "捕羽", "不稳定", "不能进入"))
    )
    # Pass B: general rails for full dump
    if not feedback_only and time.monotonic() < end:
        _scan(general_needles, wide=False, until=end)

    def _clean_lines(baddr: int, txt: str) -> list[ChatHistoryLine]:
        out: list[ChatHistoryLine] = []
        for ln in _parse_rich_blob(txt, base_addr=baddr):
            t = str(ln.text or "").strip()
            if not t:
                continue
            if t in _FB_CANON:
                out.append(ln)
                continue
            if not _body_ok(t):
                continue
            weird = sum(
                1
                for c in t
                if (0xE000 <= ord(c) <= 0xF8FF)
                or (0xAC00 <= ord(c) <= 0xD7AF)
                or ord(c) > 0xFF00
            )
            if weird:
                continue
            cjk = sum(1 for c in t if "一" <= c <= "鿿")
            if cjk < 2:
                continue
            out.append(ln)
        # plain canons kept as fallback; later _drop_plain_covered_by_rich
        for ln in _extract_feedback_plain_lines(txt, base_addr=baddr):
            t = str(ln.text or "").strip()
            if t in _FB_CANON:
                out.append(ln)
        return out

    def _quality(it: tuple[int, str, int]) -> tuple:
        baddr, txt, sc = it
        a = int(baddr) & 0xFFFFFFFF
        high = 0 if a >= 0x70000000 else (1 if a >= 0x50000000 else 2)
        lines = _clean_lines(baddr, txt)
        rich_n = sum(1 for ln in lines if str(ln.source or "").startswith("hist"))
        # prefer many clean lines + rails + high heap + score
        return (high, -len(lines), -txt.count("\ue000"), -rich_n, -int(sc), -a)

    if not blobs:
        log("chat hist: no blobs")
        return []

    blobs_sorted = sorted(blobs, key=_quality)
    primary = blobs_sorted[0]
    p_addr, p_txt, p_sc = primary
    primary_lines = _clean_lines(p_addr, p_txt)
    # Neighbor merge is OPTIONAL and tightly gated.
    # Live issue: loose merge pulled a second full history copy from another
    # high-heap window (+0xBxxxx), doubling every line and hiding "new" feel.
    # Only absorb lines whose absolute addr sits inside the primary ring span
    # (slightly expanded) — fill holes, never import a parallel copy.
    if primary_lines:
        p_addrs = [
            int(ln.addr or 0) & 0xFFFFFFFF
            for ln in primary_lines
            if int(ln.addr or 0)
        ]
        if p_addrs:
            p_lo, p_hi = min(p_addrs), max(p_addrs)
            # allow a little edge growth for rails cut at window borders
            edge = 0x4000
            span_lo, span_hi = max(0, p_lo - edge), p_hi + edge
            by_addr = {int(ln.addr or 0) & 0xFFFFFFFF: ln for ln in primary_lines}
            # Only consult a few nearby blobs when primary looks truncated.
            need_fill = len(primary_lines) < 8
            for baddr, txt, sc in blobs_sorted[1:6]:
                a0 = int(baddr) & 0xFFFFFFFF
                if a0 < 0x70000000:
                    continue
                if abs(a0 - (int(p_addr) & 0xFFFFFFFF)) > 0x80000:
                    continue
                if not need_fill and not (span_lo <= a0 <= span_hi):
                    continue
                for ln in _clean_lines(baddr, txt):
                    a = int(ln.addr or 0) & 0xFFFFFFFF
                    if not a:
                        continue
                    if a < span_lo or a > span_hi:
                        continue
                    prev = by_addr.get(a)
                    if prev is None or _line_pick_score(ln) >= _line_pick_score(prev):
                        by_addr[a] = ln
            primary_lines = [by_addr[k] for k in sorted(by_addr)]
    # Keep rail/plain order as discovered in primary window (addr order)
    primary_lines.sort(key=lambda ln: (int(ln.addr or 0), int(ln.seq)))
    for i, ln in enumerate(primary_lines):
        ln.seq = i
        if not ln.source:
            ln.source = "hist:rich"

    have = {str(ln.text or "") for ln in primary_lines}
    # Fill missing exact feedback from other high-heap blobs (canon only)
    missing = [c for c in _FB_CANON if c not in have]
    if missing:
        for baddr, txt, sc in blobs_sorted[1:]:
            if (int(baddr) & 0xFFFFFFFF) < 0x70000000:
                continue
            for ln in _extract_feedback_plain_lines(txt, base_addr=baddr):
                t = str(ln.text or "").strip()
                if t not in missing or t in have:
                    continue
                if (int(ln.addr or 0) & 0xFFFFFFFF) < 0x70000000:
                    continue
                ln.seq = len(primary_lines)
                primary_lines.append(ln)
                have.add(t)
                missing = [c for c in _FB_CANON if c not in have]
                log(f"chat hist: +fb-canon 0x{int(baddr)&0xFFFFFFFF:X} {t}")
            if not missing:
                break

    lines = primary_lines
    if feedback_only:
        lines = [ln for ln in lines if str(ln.text or "") in _FB_CANON]
        # if still empty, pull canons from any high blob
        if not lines:
            for baddr, txt, sc in blobs_sorted:
                if (int(baddr) & 0xFFFFFFFF) < 0x70000000:
                    continue
                for ln in _extract_feedback_plain_lines(txt, base_addr=baddr):
                    t = str(ln.text or "")
                    if t in _FB_CANON:
                        lines.append(ln)
            # dedupe keep highest addr
            best: dict[str, ChatHistoryLine] = {}
            for ln in lines:
                t = str(ln.text or "")
                prev = best.get(t)
                if prev is None or int(ln.addr or 0) >= int(prev.addr or 0):
                    best[t] = ln
            lines = sorted(best.values(), key=lambda x: int(x.addr or 0))

    # Baseline (worked): keep every distinct addr so new rails always surface.
    # Mild extensions only: drop plain if rich nearby; collapse residual triples.
    best2: dict[int, ChatHistoryLine] = {}
    noaddr: list[ChatHistoryLine] = []
    for ln in lines:
        t = str(ln.text or "")
        if not t:
            continue
        a = int(ln.addr or 0) & 0xFFFFFFFF
        if not a:
            noaddr.append(ln)
            continue
        prev = best2.get(a)
        if prev is None or _line_pick_score(ln) >= _line_pick_score(prev):
            best2[a] = ln
    lines = sorted(
        list(best2.values()) + noaddr,
        key=lambda x: (int(x.addr or 0), int(x.seq)),
    )
    lines = _prefer_clean_feedback_lines(lines)
    lines = _drop_plain_covered_by_rich(lines)
    lines = _collapse_residual_copies(lines)
    lines = _collapse_repeated_history_block(lines)
    for i, ln in enumerate(lines):
        ln.seq = i

    id_hist: dict[str, int] = {}
    for ln in lines:
        id_hist[ln.channel] = id_hist.get(ln.channel, 0) + 1
    log(
        f"chat hist: mem-primary=0x{int(p_addr)&0xFFFFFFFF:X} score={p_sc} "
        f"blobs={len(blobs)} fb_win={fb_n} lines={len(lines)} channels={id_hist}"
    )
    # Cache the selected live window for high-frequency readers. A later read
    # can use ReadProcessMemory directly and fall back to the full scan if the
    # allocation was moved or released.
    try:
        raw_size = len(str(p_txt or "").encode("utf-16le", "ignore"))
        setattr(
            session,
            "_chat_primary_hint",
            {
                "addr": int(p_addr) & 0xFFFFFFFF,
                "size": max(0x2000, min(0x10000, raw_size)),
                "ts": time.monotonic(),
            },
        )
    except Exception:
        pass
    cap = max(1, int(max_lines))
    lines = lines[-cap:]
    for i, ln in enumerate(lines):
        ln.seq = i
    return lines


def read_chat_history_fast(
    session,
    *,
    max_lines: int = 200,
    log: LogFn | None = None,
) -> list[dict]:
    """Read the cached rich-history window without a process-wide scan."""
    log = log or (lambda _m: None)
    hint = getattr(session, "_chat_primary_hint", None)
    pm = getattr(session, "pm", None)
    if not isinstance(hint, dict) or pm is None:
        return []
    try:
        import pymem.memory
    except Exception:
        return []
    try:
        base = int(hint.get("addr") or 0) & 0xFFFFFFFF
        size = max(0x2000, min(0x10000, int(hint.get("size") or 0x8000)))
    except Exception:
        return []
    if not base:
        return []

    raw = b""
    for take in (size, min(size, 0x8000), min(size, 0x4000), 0x2000):
        try:
            raw = pymem.memory.read_bytes(pm.process_handle, base, int(take))
            if raw:
                break
        except Exception:
            continue
    if not raw:
        return []
    try:
        text = raw.decode("utf-16le", "ignore")
    except Exception:
        return []
    if "\ue000" not in text:
        return []

    lines: list[ChatHistoryLine] = []
    for line in _parse_rich_blob(text, base_addr=base):
        if not str(line.text or "").strip():
            continue
        line.source = "hist:rich:fast"
        lines.append(line)
    if len(lines) < 2:
        return []
    lines = _prefer_clean_feedback_lines(lines)
    best_by_addr: dict[int, ChatHistoryLine] = {}
    for line in lines:
        addr = int(line.addr or 0) & 0xFFFFFFFF
        prev = best_by_addr.get(addr)
        if prev is None or _line_pick_score(line) >= _line_pick_score(prev):
            best_by_addr[addr] = line
    lines = sorted(best_by_addr.values(), key=lambda line: int(line.addr or 0))
    lines = _collapse_residual_copies(lines)
    lines = _collapse_repeated_history_block(lines)
    lines.sort(key=lambda line: (int(line.addr or 0), int(line.seq)))
    cap = max(1, int(max_lines))
    lines = lines[-cap:]
    for i, line in enumerate(lines):
        line.seq = i
    return [line.to_dict() for line in lines]



def dump_chat_history(
    session,
    *,
    budget_s: float = 1.8,
    max_lines: int = 200,
    log: LogFn | None = None,
    mode: str = "full",
) -> list[dict]:
    """
    Dump structured chat history lines with channel_id + seq.

    mode:
      - full: feedback pass + general chat (dev panel live-ui)
      - feedback: feedback needles only (hot path; ~0.3-0.6s)

    Returns the newest max_lines (buffer tail). seq is renumbered 0..n-1
    after the tail cut (larger still ~= newer within the returned window).

    @author by ak
    """
    mode_l = str(mode or "full").strip().lower()
    if mode_l in ("feedback", "fb", "captcha"):
        # Primary: high-heap rich hist (same order as full dump) — this is where
        # live 系统 lines actually land (0x7C......). fb-fast alone stuck on
        # mid-heap residuals and made the dev panel look frozen.
        budget = max(0.2, float(budget_s))
        primary = _scan_rich_history_blobs(
            session,
            budget_s=max(0.25, budget * 0.7),
            max_lines=max_lines,
            log=log,
            mode="feedback",
        )
        # Secondary: multi-instance exact plain hits (may include mid residual).
        extra = _scan_feedback_fast_blobs(
            session,
            budget_s=max(0.15, budget * 0.35),
            log=None,
        )
        by_key: dict[tuple[str, int], ChatHistoryLine] = {}
        seq = 0
        for ln in list(primary) + list(extra):
            t = str(ln.text or "")
            if not t:
                continue
            page = int(ln.addr or 0) & ~0x1F
            k = (t, page)
            prev = by_key.get(k)
            # Prefer tagged rich over plain/hitmap for same page.
            if prev is None:
                ln.seq = seq
                by_key[k] = ln
                seq += 1
            elif (prev.channel_id is None or int(prev.channel_id) < 0) and (
                ln.channel_id is not None and int(ln.channel_id) >= 0
            ):
                ln.seq = prev.seq
                by_key[k] = ln
        rows = sorted(by_key.values(), key=lambda x: x.seq)
        for i, ln in enumerate(rows):
            ln.seq = i
        _log = log or (lambda _m: None)
        _log(
            f"chat hist(feedback): primary={len(primary)} extra={len(extra)} "
            f"merged={len(rows)}"
        )
        # Prefer high-heap live copies, then newest tail.
        high = [r for r in rows if int(r.addr or 0) >= 0x70000000]
        if len(high) >= 3:
            rows = high
        cap = max(1, int(max_lines))
        rows = rows[-cap:]
        for i, ln in enumerate(rows):
            ln.seq = i
        return [r.to_dict() for r in rows]
    rows = _scan_rich_history_blobs(
        session,
        budget_s=budget_s,
        max_lines=max_lines,
        log=log,
        mode=mode_l,
    )
    return [r.to_dict() for r in rows]


def snapshot_chat_fingerprint(
    session, *, budget_s: float = 1.2, max_lines: int = 120
) -> dict:
    """
    Compact snapshot for new-message delta detection.

    Fields:
      count, last_seq, texts, uids, fb_counts, tail, sig_hash

    uids/mem_keys make *second* 答案错误 visible (text-set alone cannot).

    @author by ak
    """
    rows = dump_chat_history(session, budget_s=budget_s, max_lines=max_lines, log=None)
    texts = tuple(str(r.get("text") or "") for r in rows if r.get("text"))
    uids = tuple(
        str(r.get("uid") or r.get("mem_key") or "")
        for r in rows
        if r.get("text")
    )
    addrs = tuple(int(r.get("addr") or 0) & 0xFFFFFFFF for r in rows if r.get("text"))
    tail = texts[-8:] if texts else tuple()
    fb_counts = count_feedback_texts(rows)
    h = hashlib.sha1(
        ("\n".join(texts) + "#" + "|".join(uids)).encode("utf-8", "ignore")
    ).hexdigest()[:16]
    return {
        "count": len(texts),
        "last_seq": int(rows[-1]["seq"]) if rows else -1,
        "texts": texts,
        "uids": uids,
        "addrs": addrs,
        "tail": tail,
        "sig": texts,
        "sig_hash": h,
        "fb_counts": fb_counts,
        "channels": tuple(str(r.get("channel") or "") for r in rows),
        "channel_ids": tuple(int(r.get("channel_id") or -1) for r in rows),
    }


def diff_chat_new_lines(before: dict | None, after: dict | None) -> list[str]:
    """
    New texts from after vs before.

    Prefer uid growth (same 答案错误 xN works). Fall back to text-set for
    legacy fingerprints without uids.

    @author by ak
    """
    b_uids = [u for u in ((before or {}).get("uids") or ()) if u]
    a_uids = [u for u in ((after or {}).get("uids") or ()) if u]
    a_texts = list((after or {}).get("texts") or ())
    if b_uids or a_uids:
        seen = set(b_uids)
        out: list[str] = []
        for u, t in zip(a_uids, a_texts):
            if not t:
                continue
            if u and u not in seen:
                out.append(t)
                seen.add(u)
            elif not u and t not in set((before or {}).get("texts") or ()):
                out.append(t)
        return out
    b = set((before or {}).get("texts") or ())
    out = []
    for t in a_texts:
        if t and t not in b:
            out.append(t)
    return out


def diff_chat_new_rows(before: dict | None, after_rows: list[dict] | None) -> list[dict]:
    """
    Row-level delta. Prefer uid/mem_key not-in-before; else text-set.

    Feedback multi-hit (答案错误 xN) relies on distinct mem uid per addr.

    @author by ak
    """
    b_uids = set(u for u in ((before or {}).get("uids") or ()) if u)
    b_texts = set((before or {}).get("texts") or ())
    # Multiset fallback: how many of each text already seen
    from collections import Counter

    b_text_n = Counter((before or {}).get("texts") or ())
    seen_text_n: Counter = Counter()
    out: list[dict] = []
    for r in after_rows or []:
        t = str(r.get("text") or "")
        if not t:
            continue
        uid = str(r.get("uid") or r.get("mem_key") or "")
        if uid:
            if uid not in b_uids:
                out.append(r)
            continue
        # no uid: allow N-th copy if after has more than before of this text
        seen_text_n[t] += 1
        if seen_text_n[t] > int(b_text_n.get(t) or 0):
            out.append(r)
        elif t not in b_texts:
            out.append(r)
    return out




# Exact feedback sentences used for baseline/delta (no wall-clock needed).
FEEDBACK_COUNT_KEYS: tuple[str, ...] = (
    "答案正确，请尽快进入活动",
    "答案错误，请大侠重新来过",
    "队伍成员处于神罚状态，不能进入副本",
    "队伍成员处于捕羽状态，不能进入副本",
    "不稳定状态下无法进行此操作",
    "入口未打开，目前不能进入",  # live UI 其他/系统 toast
)


def count_feedback_texts(rows: list[dict] | None) -> dict[str, int]:
    """Count exact feedback sentences in hist rows. @author by ak"""
    out = {k: 0 for k in FEEDBACK_COUNT_KEYS}
    for r in rows or []:
        t = str(r.get("text") or "")
        if not t:
            continue
        for k in FEEDBACK_COUNT_KEYS:
            if k in t:
                out[k] += 1
    return out


def _ctx_hash_around(chunk: bytes, off: int, needle_len: int) -> str:
    """Stable short hash of bytes around a UTF-16 match. @author by ak"""
    try:
        left = max(0, int(off) - 24)
        right = min(len(chunk), int(off) + int(needle_len) + 24)
        return hashlib.sha1(chunk[left:right]).hexdigest()[:8]
    except Exception:
        return ""


def collect_feedback_hit_map(
    session,
    *,
    budget_s: float = 0.7,
    max_regions: int = 3200,
    log: LogFn | None = None,
    min_addr: int = 0x70000000,
) -> dict[str, list[tuple[int, str]]]:
    """
    Collect absolute UTF-16 hits of exact feedback sentences.

    Default min_addr=0x70000000: mid-heap catalog residuals used to freeze
    counts (答案错误 always N>=6) so delta never moved — live rails sit high.

    @author by ak
    """
    log = log or (lambda _m: None)
    pm = getattr(session, "pm", None)
    out: dict[str, list[tuple[int, str]]] = {k: [] for k in FEEDBACK_COUNT_KEYS}
    if pm is None:
        return out
    try:
        import pymem.memory
        from app.core.game_attach import _iter_writable_regions
    except Exception as e:
        log(f"fb hit-map import fail: {e}")
        return out

    needles = [(k, k.encode("utf-16le")) for k in FEEDBACK_COUNT_KEYS]
    end = time.monotonic() + max(0.12, float(budget_s))
    min_a = int(min_addr) & 0xFFFFFFFF
    try:
        regions = list(_iter_writable_regions(pm, max_regions=max(1200, int(max_regions))))
    except Exception as e:
        log(f"fb hit-map regions fail: {e}")
        return out

    regions = [(int(b), int(sz)) for b, sz in regions if int(b) >= max(0x04000000, min_a - 0x100000)]
    regions.sort(key=lambda it: _rank_chat_region(it[0]))

    caps = {k: 16 for k in FEEDBACK_COUNT_KEYS}
    seen: set[tuple[str, int]] = set()
    for base, size in regions:
        if time.monotonic() >= end:
            break
        if all(len(out[k]) >= caps[k] for k in FEEDBACK_COUNT_KEYS):
            break
        size = min(int(size), 0x400000)
        off = 0
        while off < size and time.monotonic() < end:
            take = min(0x100000, size - off)
            addr = base + off
            try:
                chunk = pymem.memory.read_bytes(pm.process_handle, addr, take)
            except Exception:
                off += take
                continue
            for key, pat in needles:
                if len(out[key]) >= caps[key]:
                    continue
                st = 0
                local = 0
                while local < 10 and len(out[key]) < caps[key] and time.monotonic() < end:
                    j = chunk.find(pat, st)
                    if j < 0:
                        break
                    if j & 1:
                        st = j + 1
                        continue
                    abs_a = (addr + j) & 0xFFFFFFFF
                    if abs_a < min_a:
                        st = j + max(2, len(pat))
                        local += 1
                        continue
                    # page ~0x20 collapses residual shadows, keeps 0x5A rails
                    page = abs_a & ~0x1F
                    sk = (key, page)
                    if sk not in seen:
                        seen.add(sk)
                        out[key].append((abs_a, _ctx_hash_around(chunk, j, len(pat))))
                    st = j + max(2, len(pat))
                    local += 1
            off += take
    return out


def snapshot_feedback_fingerprint(
    session,
    *,
    budget_s: float = 0.7,
    log: LogFn | None = None,
) -> dict:
    """
    Feedback snapshot for captcha / 同类错误增量.

    Primary = live rich-hist rails (mode=feedback). Count growth of same
    sentence (答案错误 xN) follows new high-heap rail addrs, not sticky
    mid-heap catalog residuals.

    Secondary = high-heap hitmap only if hist empty.

    @author by ak
    """
    log = log or (lambda _m: None)
    budget = max(0.25, float(budget_s))
    rows: list[dict] = []
    history_rows: list[dict] = []
    try:
        # Use FULL primary-hist path (same as dump_chat_messages), then keep
        # only feedback canons. mode=feedback was pulling multi-blob residuals
        # and inflating 答案错误 4 -> 32 on live 十丶一.
        all_rows = dump_chat_history(
            session,
            budget_s=max(0.25, budget * 0.8),
            max_lines=120,
            log=log,
            mode="full",
        )
        # Only ordered rich rails from the selected primary history blob are
        # eligible for freshness decisions. Plain/hitmap rows have no reliable
        # chronology and remain diagnostic-only.
        history_rows = [
            dict(r)
            for r in all_rows
            if str(r.get("source") or "").startswith("hist:rich")
        ]
        rows = []
        for r in all_rows:
            txt = str(r.get("text") or "").strip()
            if not txt:
                continue
            if any(k == txt or k in txt for k in FEEDBACK_COUNT_KEYS):
                rows.append(r)
    except Exception as e:
        log(f"fb fingerprint hist fail: {e}")
        rows = []

    counts = count_feedback_texts(rows)
    instances: list[tuple[str, int, str]] = []
    for r in rows:
        txt = str(r.get("text") or "")
        if not txt:
            continue
        canon = next((k for k in FEEDBACK_COUNT_KEYS if k == txt or k in txt), None)
        if canon is None:
            continue
        try:
            addr = int(r.get("addr") or 0) & 0xFFFFFFFF
        except Exception:
            addr = 0
        uid = str(r.get("uid") or r.get("mem_key") or "")
        instances.append((canon, addr, uid))

    # Fallback / top-up: high-heap hitmap when hist missed a canon
    missing = [k for k in FEEDBACK_COUNT_KEYS if int(counts.get(k) or 0) <= 0]
    hit_map: dict[str, list[tuple[int, str]]] = {k: [] for k in FEEDBACK_COUNT_KEYS}
    if missing and budget > 0.15:
        try:
            hit_map = collect_feedback_hit_map(
                session,
                budget_s=max(0.12, budget * 0.3),
                log=None,
                min_addr=0x70000000,
            )
            for k in missing:
                for addr, ctx in hit_map.get(k) or []:
                    a = int(addr) & 0xFFFFFFFF
                    if a < 0x70000000:
                        continue
                    instances.append((k, a, str(ctx or "")))
                    counts[k] = int(counts.get(k) or 0) + 1
        except Exception as e:
            log(f"fb fingerprint hitmap fail: {e}")

    # Prefer hist row counts when present; hitmap only filled missing keys above.
    texts = tuple(str(r.get("text") or "") for r in rows if r.get("text"))
    if not texts:
        texts = tuple(k for k in FEEDBACK_COUNT_KEYS if int(counts.get(k) or 0) > 0)

    # Dedupe instances by (text, page) keep first
    seen_i: set[tuple[str, int]] = set()
    inst2: list[tuple[str, int, str]] = []
    for t, a, c in sorted(instances, key=lambda it: it[1]):
        page = (int(a) & 0xFFFFFFFF) & ~0x1F
        sk = (t, page)
        if sk in seen_i:
            continue
        seen_i.add(sk)
        inst2.append((t, int(a) & 0xFFFFFFFF, str(c or "")))
    instances = inst2
    # Recompute counts from instances (authoritative multi-hit)
    counts = {k: 0 for k in FEEDBACK_COUNT_KEYS}
    for t, _a, _c in instances:
        if t in counts:
            counts[t] += 1

    inst_key = "|".join(f"{t}@{a:x}:{c}" for t, a, c in instances)
    cnt_key = ";".join(f"{k}={counts.get(k, 0)}" for k in FEEDBACK_COUNT_KEYS)
    return {
        "t": time.time(),
        "mono": time.monotonic(),
        "counts": counts,
        "texts": texts,
        "instances": instances,
        "rows": rows,
        "history_rows": history_rows,
        "sig_hash": hashlib.sha1(
            (cnt_key + "#" + inst_key).encode("utf-8", "ignore")
        ).hexdigest()[:16],
    }


def diff_feedback_count_new(
    before: dict | None, after: dict | None
) -> dict[str, int]:
    """
    Return count growth per feedback key (after - before, floored at 0).

    @author by ak
    """
    b = (before or {}).get("counts") or {}
    a = (after or {}).get("counts") or {}
    out: dict[str, int] = {}
    keys = set(FEEDBACK_COUNT_KEYS) | set(b) | set(a)
    for k in keys:
        d = int(a.get(k) or 0) - int(b.get(k) or 0)
        if d > 0:
            out[str(k)] = d
    return out


def _inst_parts(it) -> tuple[str, int, str]:
    """Normalize instance tuple (text, addr[, ctx]). @author by ak"""
    try:
        t = str(it[0])
        a = int(it[1]) & 0xFFFFFFFF
        c = str(it[2]) if len(it) >= 3 else ""
        return t, a, c
    except Exception:
        return "", 0, ""


def diff_feedback_new_instances(
    before: dict | None, after: dict | None
) -> list[tuple[str, int]]:
    """
    Instances present in after but not in before.

    New if:
      - (text, page) never seen, or
      - same page but context hash changed (in-place rewrite)

    Residual same-text same-page same-ctx is ignored.

    @author by ak
    """
    b_pages: dict[tuple[str, int], set[str]] = {}
    for it in (before or {}).get("instances") or ():
        t, a, c = _inst_parts(it)
        if not t:
            continue
        page = int(a) & ~0x1F
        b_pages.setdefault((t, page), set()).add(c)

    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for it in (after or {}).get("instances") or ():
        t, a, c = _inst_parts(it)
        if not t:
            continue
        page = int(a) & ~0x1F
        key = (t, page)
        old_ctx = b_pages.get(key)
        if old_ctx is None:
            is_new = True
        elif c and c not in old_ctx and any(bool(x) for x in old_ctx):
            # same slot rewritten with different surrounding bytes
            is_new = True
        else:
            is_new = False
        if not is_new:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append((t, int(a) & 0xFFFFFFFF))
    return out


def channel_id_name(channel_id: int | None) -> str:
    """Map live channel id to label. @author by ak"""
    if channel_id is None:
        return "未知"
    try:
        cid = int(channel_id)
    except Exception:
        return "未知"
    return _CHANNEL_ID_MAP.get(cid, f"ch{cid}")
