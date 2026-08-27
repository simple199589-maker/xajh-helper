# -*- coding: utf-8 -*-
"""
In-game system message helpers (captcha answer feedback).

Live catalog (UTF-16LE, 2026-07-16):
  msg_id 0x5078  答案错误，请大侠重新来过
  msg_id 0x5079  答案正确，请尽快进入活动

Chat UI reuses these static strings (no extra heap copies), so detection
combines:
  1) captcha dialog closed (IsDlgShow)
  2) scene leave Fuzhou / enter 妖楼
  3) optional scan of Popmsg / transient UTF-16 for fail|ok text

@author by ak
"""
from __future__ import annotations

import re
import struct
import time
from dataclasses import dataclass
from typing import Callable

from app.core.plg_ui import is_captcha_dialog_open

LogFn = Callable[[str], None]

# Live system-message catalog ids + text (UTF-16LE in process).
CAPTCHA_FAIL_MSG_ID = 0x5078
CAPTCHA_OK_MSG_ID = 0x5079
CAPTCHA_FAIL_TEXT = "答案错误，请大侠重新来过"
CAPTCHA_OK_TEXT = "答案正确，请尽快进入活动"
# Team debuff: answer may be correct but enter is permanently blocked (stop).
SHENFA_BLOCK_TEXT = "队伍成员处于神罚状态，不能进入副本"
BUYU_BLOCK_TEXT = "队伍成员处于捕羽状态，不能进入副本"
# Soft / temporary entry refusal (heartbeat retry, do not stop).
ENTRY_OPEN_BLOCK_TEXT = "队伍不满足开启副本条件"

# Substrings that still uniquely identify captcha feedback in chat/toast.
_CAPTCHA_FAIL_KEYS = (
    CAPTCHA_FAIL_TEXT,
    "答案错误",
    "请大侠重新来过",
)
_CAPTCHA_OK_KEYS = (
    CAPTCHA_OK_TEXT,
    "答案正确",
    "请尽快进入活动",
)
# Hard party debuffs that block dungeon enter (stop full-auto; do not retry).
# Do NOT include bare "不能进入副本" — soft party toasts reuse that tail.
_SHENFA_KEYS = (
    SHENFA_BLOCK_TEXT,
    BUYU_BLOCK_TEXT,
    "神罚状态",
    "捕羽状态",
    "处于神罚",
    "处于捕羽",
)
_SHENFA_HARD_MARKERS = ("神罚", "捕羽")
_ENTRY_OPEN_BLOCK_KEYS = (
    ENTRY_OPEN_BLOCK_TEXT,
    "副本无法开启",
    "队伍不满足",
    "距离过远",
)

# Live chat/toast hosts for 系统/其他 channel lines (screenshot form).
# Chat panels stay resident; do not require IsDlgShow==True for these.
_FEEDBACK_TOAST_DLGS = (
    "Win_Popmsg",
    "Win_PopWarningMsg",
    "Win_PopChatMsg",
    "Win_GameEventPop",
    "Win_HintBoard",
    "Win_MessageBox",
    "Win_MsgBox",
    "Win_InstanceTip",
)
_FEEDBACK_CHAT_DLGS = (
    "Win_ChatInfo",
    "Win_Chat",
    "Win_MainInfoLeft",
    "Win_MainInfoMiddle",
    "Win_MainInfoFrame",
    "Win_MainInfoRight",
    "Win_ChatMain",
    "Win_ChatPanel",
    "Win_ChatMsg",
    "Win_BattleFieldChat",
    "Win_XAVSChatInfo",
    "Win_XAVSChat",
)
# Short markers used for delta scans when full sentences are reused from catalog.
_OK_SHORT_TEXT = "答案正确"
_OK_SHORT_TAIL = "请尽快进入活动"
_FAIL_SHORT_TEXT = "答案错误"
_SHENFA_SHORT_TEXT = "神罚状态"
_BUYU_SHORT_TEXT = "捕羽状态"
# Live chat/toast presence keys (UTF-16 on writable heap; avoid full process scan).
_HEAP_FEEDBACK_MARKERS = (
    _OK_SHORT_TEXT,
    _FAIL_SHORT_TEXT,
    _SHENFA_SHORT_TEXT,
    _BUYU_SHORT_TEXT,
    _OK_SHORT_TAIL,
)
# Live chat channel category tags (screenshot: [系统] / [其他]).
_CHAT_CH_SYS_TAGS = ("[系统]", "系统")
_CHAT_CH_OTHER_TAGS = ("[其他]", "其他")


def is_shenfa_block_feedback(feedback: "CaptchaAnswerFeedback | None") -> bool:
    """
    True for hard 神罚/捕羽 enter blocks (stop automation).

    Only permanent party debuffs stop the runner. Soft party conditions
    (距离过远 / 队伍不满足) are retryable and must not match here.
    """
    if feedback is None:
        return False
    text = str(feedback.text or "")
    error = str(feedback.error or "")
    blob = f"{text} {error}"
    # Hard markers only — bare "不能进入副本" alone is not enough.
    if any(m in blob for m in _SHENFA_HARD_MARKERS):
        return True
    if SHENFA_BLOCK_TEXT in blob or BUYU_BLOCK_TEXT in blob:
        return True
    return False


def is_entry_open_block_feedback(feedback: "CaptchaAnswerFeedback | None") -> bool:
    """True for retryable party-condition refusal, not 神罚/捕羽."""
    if feedback is None or feedback.kind != "block":
        return False
    if is_shenfa_block_feedback(feedback):
        return False
    if str(feedback.method or "").startswith("entry_"):
        return True
    text = str(feedback.text or "")
    return any(key in text for key in _ENTRY_OPEN_BLOCK_KEYS)


def is_explicit_captcha_ok_feedback(
    feedback: "CaptchaAnswerFeedback | None",
) -> bool:
    """True only when answer correctness itself was positively observed."""
    if feedback is None or feedback.kind != "ok":
        return False
    if feedback.msg_id == CAPTCHA_OK_MSG_ID:
        return True
    text = str(feedback.text or "")
    return any(key in text for key in _CAPTCHA_OK_KEYS)


def is_fresh_entry_open_block_feedback(
    feedback: "CaptchaAnswerFeedback | None",
) -> bool:
    """True only for an entry-condition text copy added after this submit."""
    if not is_entry_open_block_feedback(feedback):
        return False
    method = str(feedback.method or "")
    if not method.startswith("entry_u16_hits="):
        return False
    try:
        parts = dict(item.split("=", 1) for item in method.split() if "=" in item)
        now = int(parts.get("entry_u16_hits", "0"))
        before = int(parts.get("baseline", "0"))
    except (TypeError, ValueError):
        return False
    return now > before


@dataclass
class CaptchaAnswerFeedback:
    """
    Outcome after captcha confirm.

    kind: ok | fail | block | pending | timeout | stopped
    block = 神罚等硬拦截（应停止整轮自动，不要重开入口）。
    @author by ak
    """

    kind: str
    text: str | None = None
    msg_id: int | None = None
    dialog_open: bool | None = None
    method: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "text": self.text,
            "msg_id": self.msg_id,
            "dialog_open": self.dialog_open,
            "method": self.method,
            "error": self.error,
        }


def _read_wstr(pm, addr: int, max_chars: int = 96) -> str | None:
    """
    Read remote UTF-16LE C string.

    @author by ak
    """
    if not addr or pm is None:
        return None
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(
            pm.process_handle, int(addr) & 0xFFFFFFFF, max_chars * 2
        )
    except Exception:
        return None
    chars: list[str] = []
    for i in range(0, len(raw) - 1, 2):
        code = raw[i] | (raw[i + 1] << 8)
        if code == 0:
            break
        if code < 0x20 and code not in (0x09, 0x0A, 0x0D):
            break
        chars.append(chr(code))
    return "".join(chars) if chars else None


def _read_cstr_gbk(pm, addr: int, max_bytes: int = 160) -> str | None:
    """
    Read remote GBK/ANSI C string (chat lines may not be UTF-16).

    @author by ak
    """
    if not addr or pm is None:
        return None
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(
            pm.process_handle, int(addr) & 0xFFFFFFFF, max(8, int(max_bytes))
        )
    except Exception:
        return None
    end = raw.find(b"\x00")
    if end < 0:
        end = len(raw)
    blob = raw[:end]
    if len(blob) < 2:
        return None
    for enc in ("gbk", "utf-8"):
        try:
            s = blob.decode(enc)
        except Exception:
            continue
        if s and any("一" <= c <= "鿿" for c in s):
            return s
    return None


def _match_keys_in_text(text: str | None, keys: tuple[str, ...]) -> str | None:
    """Return text when any key is a substring."""
    if not text:
        return None
    for k in keys:
        if k and k in text:
            return text
    return None


def _scan_inline_text_keys(raw: bytes, keys: tuple[str, ...]) -> str | None:
    """
    Scan object bytes for inline UTF-16LE / GBK key hits (not only pointers).

    Chat history often embeds line text in control buffers.

    @author by ak
    """
    if not raw or not keys:
        return None
    # UTF-16LE runs
    i = 0
    n = len(raw)
    while i + 3 < n:
        # cheap gate: Chinese BMP often has non-zero high byte on second of pair
        lo = raw[i]
        hi = raw[i + 1]
        if hi == 0 and 0x20 <= lo < 0x7F:
            # ascii-ish utf16 run
            chars: list[str] = []
            j = i
            while j + 1 < n and raw[j + 1] == 0 and raw[j] != 0:
                chars.append(chr(raw[j]))
                j += 2
                if len(chars) >= 96:
                    break
            if len(chars) >= 2:
                hit = _match_keys_in_text("".join(chars), keys)
                if hit:
                    return hit
            i = max(j, i + 2)
            continue
        if hi != 0 or lo >= 0x80:
            chars = []
            j = i
            while j + 1 < n:
                code = raw[j] | (raw[j + 1] << 8)
                if code == 0:
                    break
                if code < 0x20 and code not in (0x09, 0x0A, 0x0D):
                    break
                chars.append(chr(code))
                j += 2
                if len(chars) >= 96:
                    break
            if len(chars) >= 2:
                hit = _match_keys_in_text("".join(chars), keys)
                if hit:
                    return hit
            i = max(j, i + 2)
            continue
        i += 2

    # GBK / multi-byte runs
    try:
        # decode whole buffer as gbk with replace, then substring search
        gbk_text = raw.decode("gbk", errors="ignore")
    except Exception:
        gbk_text = ""
    hit = _match_keys_in_text(gbk_text, keys)
    if hit:
        # return a short window around first key for diagnostics
        for k in keys:
            if k and k in gbk_text:
                idx = gbk_text.find(k)
                return gbk_text[max(0, idx - 8) : idx + len(k) + 24]
    return None


def _scan_text_hits(pm, text: str, *, limit: int = 8) -> list[int]:
    """
    Pattern-scan process for UTF-16LE + GBK text; return up to limit addresses.

    Chat/UI may keep either encoding. Dedup by address.

    @author by ak
    """
    if pm is None or not text:
        return []
    addrs: list[int] = []
    seen: set[int] = set()

    def _add_many(found) -> None:
        if not found:
            return
        if not isinstance(found, (list, tuple)):
            found = [found]
        for a in found:
            try:
                v = int(a) & 0xFFFFFFFF
            except Exception:
                continue
            if v in seen:
                continue
            seen.add(v)
            addrs.append(v)
            if len(addrs) >= limit:
                return

    for enc in ("utf-16le", "gbk"):
        if len(addrs) >= limit:
            break
        try:
            pat = text.encode(enc)
        except Exception:
            continue
        if not pat:
            continue
        try:
            found = pm.pattern_scan_all(pat, return_multiple=True) or []
            _add_many(found)
        except TypeError:
            try:
                a = pm.pattern_scan_all(pat)
                _add_many([a] if a else [])
            except Exception:
                continue
        except Exception:
            continue
    return addrs[:limit]


def _dlg_utf16_has_keys(
    session,
    dlg_name: str,
    keys: tuple[str, ...],
    *,
    require_shown: bool = True,
    obj_size: int = 0x600,
    max_chars: int = 96,
    nest: bool = False,
    nest_depth: int = 1,
    max_ptr_checks: int = 96,
    max_child_reads: int = 32,
    budget_s: float = 0.05,
) -> str | None:
    """
    If dialog object holds matching text (pointer / nested / inline), return it.

    Hard budgets prevent nest walks from stalling the runner thread
    (unbounded pointer fan-out previously blocked countdown for ~60s).

    @author by ak
    """
    try:
        from app.core.plg_ui import query_dlg_show

        r = query_dlg_show(session, dlg_name, log=lambda _m: None)
    except Exception:
        return None
    if not r.dlg_ptr:
        return None
    if require_shown and not r.shown:
        return None
    pm = getattr(session, "pm", None)
    if pm is None:
        return None
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(
            pm.process_handle, int(r.dlg_ptr), max(0x200, int(obj_size))
        )
    except Exception:
        return None

    # Inline first — cheap and catches embedded chat buffers.
    inline = _scan_inline_text_keys(raw, keys)
    if inline:
        return inline

    deadline = time.monotonic() + max(0.01, float(budget_s))
    ptr_checks = 0
    child_reads = 0
    max_ptrs = max(8, int(max_ptr_checks))
    max_children = max(4, int(max_child_reads))
    depth_limit = max(0, int(nest_depth if nest else 0))

    def _match_at(addr: int) -> str | None:
        s = _read_wstr(pm, addr, max_chars)
        hit = _match_keys_in_text(s, keys)
        if hit:
            return hit
        s2 = _read_cstr_gbk(pm, addr, max_bytes=max(32, max_chars * 2))
        return _match_keys_in_text(s2, keys)

    # BFS over a capped set of child objects (no recursive fan-out explosion).
    queue: list[tuple[bytes, int]] = [(raw, 0)]
    seen: set[int] = {int(r.dlg_ptr) & 0xFFFFFFFF}
    while queue:
        if time.monotonic() >= deadline:
            break
        blob, depth = queue.pop(0)
        for off in range(0, len(blob) - 3, 4):
            if time.monotonic() >= deadline:
                break
            if ptr_checks >= max_ptrs:
                break
            p = struct.unpack_from("<I", blob, off)[0]
            if p < 0x10000 or p > 0x7FFE0000:
                continue
            ptr_checks += 1
            hit = _match_at(p)
            if hit:
                return hit
            if depth >= depth_limit or child_reads >= max_children:
                continue
            if (p & 0xFFFFFFFF) in seen:
                continue
            seen.add(p & 0xFFFFFFFF)
            try:
                import pymem.memory

                child = pymem.memory.read_bytes(pm.process_handle, int(p), 0x280)
            except Exception:
                continue
            child_reads += 1
            hit = _scan_inline_text_keys(child, keys)
            if hit:
                return hit
            queue.append((child, depth + 1))
    return None


def _scan_feedback_ui_text(
    session,
    keys: tuple[str, ...],
    *,
    budget_s: float = 0.18,
) -> tuple[str | None, str]:
    """
    Scan toast + chat UI for key text. Returns (matched_text, method_tag).

    Live screenshot form (2026-07-19 / 2026-07-20):
      [系统] 答案正确，请尽快进入活动
      [其他] 队伍成员处于神罚状态，不能进入副本
    These lines live in chat panels more often than Win_Popmsg.

    Whole call is time-budgeted so countdown/stop stay responsive.

    @author by ak
    """
    end = time.monotonic() + max(0.02, float(budget_s))

    def _remaining() -> float:
        return max(0.0, end - time.monotonic())

    for dlg in _FEEDBACK_TOAST_DLGS:
        if _remaining() <= 0:
            break
        s = _dlg_utf16_has_keys(
            session,
            dlg,
            keys,
            require_shown=True,
            obj_size=0x800,
            max_chars=140,
            nest=True,
            nest_depth=2,
            max_ptr_checks=96,
            max_child_reads=28,
            budget_s=min(0.07, _remaining()),
        )
        if s:
            return s, f"dlg:{dlg}"

    chat_names = list(_FEEDBACK_CHAT_DLGS)
    cached = getattr(session, "_feedback_chat_dlg_names", None)
    if cached is None:
        discovered: list[str] = []
        try:
            from app.core.plg_ui import list_game_ui_dlg_names

            listed = list_game_ui_dlg_names(session, log=lambda _m: None)
            for name in listed.names:
                low = str(name).lower()
                if any(k in low for k in ("chat", "msg", "hint", "pop", "notice", "maininfo")):
                    if name not in chat_names and name not in _FEEDBACK_TOAST_DLGS:
                        discovered.append(str(name))
        except Exception:
            discovered = []
        try:
            setattr(session, "_feedback_chat_dlg_names", tuple(discovered[:24]))
        except Exception:
            pass
        cached = discovered[:24]
    for name in cached or ():
        if name not in chat_names:
            chat_names.append(str(name))

    # Prefer known chat hosts; discovered names come after and share the budget.
    for dlg in chat_names:
        if _remaining() <= 0:
            break
        s = _dlg_utf16_has_keys(
            session,
            dlg,
            keys,
            require_shown=False,
            obj_size=0xC00,
            max_chars=160,
            nest=True,
            nest_depth=3,
            max_ptr_checks=120,
            max_child_reads=40,
            budget_s=min(0.09, _remaining()),
        )
        if s:
            tag = "chat" if dlg in _FEEDBACK_CHAT_DLGS else "ui"
            return s, f"{tag}:{dlg}"
    return None, ""


def _scan_writable_heap_markers(
    session,
    markers: tuple[str, ...] = _HEAP_FEEDBACK_MARKERS,
    *,
    budget_s: float = 0.75,
    max_regions: int = 5000,
    chunk_size: int = 0x100000,
    sample_out: dict[str, str] | None = None,
    count_out: dict[str, int] | None = None,
    max_hits_per_marker: int = 8,
) -> dict[str, bool]:
    """
    Presence/count scan for short UTF-16/GBK markers in writable heap memory.

    Live 系统/其他 chat lines for 妖楼 sit in *high* heap (~0x7Exxxxxx).
    Enumerate enough regions (thousands), scan:
      1) previously-hit bases (session cache)
      2) high bases first
    Hard time budget. First post-confirm window should call with budget>=0.75s.

    When a single blob contains both 答案正确 and 神罚/捕羽, sample_out gets
    key "__together__" for same-frame evidence (screenshot form).

    @author by ak
    """
    pm = getattr(session, "pm", None)
    out = {m: False for m in markers if m}
    counts = {m: 0 for m in out}
    if pm is None or not out:
        if count_out is not None:
            count_out.update(counts)
        return out
    try:
        import pymem.memory
        from app.core.game_attach import _iter_writable_regions
    except Exception:
        if count_out is not None:
            count_out.update(counts)
        return out

    pats_u16 = {m: m.encode("utf-16le") for m in out}
    pats_gbk: dict[str, bytes] = {}
    for m in out:
        try:
            pats_gbk[m] = m.encode("gbk")
        except Exception:
            pass
    ok_pats = tuple(
        pats_u16[m]
        for m in (_OK_SHORT_TEXT, _OK_SHORT_TAIL)
        if m in pats_u16
    ) + tuple(
        pats_gbk[m]
        for m in (_OK_SHORT_TEXT, _OK_SHORT_TAIL)
        if m in pats_gbk
    )
    hard_pats = tuple(
        pats_u16[m]
        for m in (_SHENFA_SHORT_TEXT, _BUYU_SHORT_TEXT)
        if m in pats_u16
    ) + tuple(
        pats_gbk[m]
        for m in (_SHENFA_SHORT_TEXT, _BUYU_SHORT_TEXT)
        if m in pats_gbk
    )
    end = time.monotonic() + max(0.05, float(budget_s))
    try:
        regions = list(
            _iter_writable_regions(pm, max_regions=max(500, int(max_regions)))
        )
    except Exception:
        if count_out is not None:
            count_out.update(counts)
        return out

    cached_bases: list[int] = []
    try:
        raw_c = getattr(session, "_feedback_heap_hit_bases", None) or ()
        cached_bases = [int(x) for x in raw_c if int(x) > 0][:24]
    except Exception:
        cached_bases = []

    def _rank(item: tuple[int, int]) -> tuple[int, int, int]:
        base = int(item[0])
        # Prefer last-hit regions, then mid-heap AUI/chat pools (0x48..0x5a),
        # not pure high-address-first (that burned budget and always saw 0 hits).
        if any(abs(base - cb) < 0x200000 for cb in cached_bases):
            return (0, 0, -base)
        if 0x48000000 <= base <= 0x5A000000:
            return (1, 0, -base)
        if 0x40000000 <= base < 0x48000000 or 0x5A000000 < base <= 0x62000000:
            return (2, 0, -base)
        if 0x62000000 < base <= 0x70000000:
            return (3, 0, -base)
        if 0x20000000 <= base < 0x40000000 or base > 0x70000000:
            return (4, 0, -base)
        if 0x01000000 <= base < 0x20000000:
            return (5, 0, -base)
        return (6, 0, -base)

    regions.sort(key=_rank)
    hit_cap = max(1, int(max_hits_per_marker))
    pending = set(out.keys())
    together = False
    new_hit_bases: list[int] = list(cached_bases)
    bytes_scanned = 0
    regions_seen = 0

    for base, size in regions:
        if time.monotonic() >= end or (not pending and together):
            # Keep going briefly only if we still need counts for pending.
            if not pending:
                break
        if time.monotonic() >= end:
            break
        try:
            base_i = int(base)
            size_i = int(size)
        except Exception:
            continue
        if size_i < 0x100 or size_i > 12 * 1024 * 1024:
            continue
        if base_i < 0x10000000:
            continue
        off = 0
        while off < size_i and time.monotonic() < end:
            if not pending and together:
                break
            n = min(int(chunk_size), size_i - off)
            try:
                blob = pymem.memory.read_bytes(pm.process_handle, base_i + off, n)
            except Exception:
                break
            bytes_scanned += len(blob)
            # Same-buffer 答案正确 + 神罚 (live chat form).
            if not together and ok_pats and hard_pats:
                if any(p in blob for p in ok_pats) and any(p in blob for p in hard_pats):
                    together = True
                    if sample_out is not None and "__together__" not in sample_out:
                        try:
                            sample_out["__together__"] = blob[:240].decode(
                                "utf-16le", errors="ignore"
                            )
                        except Exception:
                            sample_out["__together__"] = "ok+hard"
            for m in list(pending):
                for enc_name, pat_map in (("u16", pats_u16), ("gbk", pats_gbk)):
                    pat = pat_map.get(m)
                    if not pat:
                        continue
                    start = 0
                    while counts[m] < hit_cap:
                        j = blob.find(pat, start)
                        if j < 0:
                            break
                        counts[m] += 1
                        out[m] = True
                        if base_i not in new_hit_bases:
                            new_hit_bases.append(base_i)
                        if sample_out is not None and m not in sample_out:
                            try:
                                abs_addr = base_i + off + j
                                raw = pymem.memory.read_bytes(
                                    pm.process_handle, max(0, abs_addr - 8), 220
                                )
                                if enc_name == "gbk":
                                    sample_out[m] = _clean_feedback_sample(
                                        raw.decode("gbk", errors="ignore"), m
                                    )
                                else:
                                    sample_out[m] = _clean_feedback_sample(
                                        raw.decode("utf-16le", errors="ignore"), m
                                    )
                            except Exception:
                                sample_out[m] = m
                        start = j + len(pat)
                    if counts[m] >= hit_cap:
                        break
                if counts[m] >= hit_cap:
                    pending.discard(m)
            off += max(1, n - 64)
        regions_seen += 1

    try:
        setattr(session, "_feedback_heap_hit_bases", tuple(new_hit_bases[:24]))
        setattr(
            session,
            "_feedback_heap_last_scan_stats",
            {
                "regions": regions_seen,
                "bytes": bytes_scanned,
                "pending_left": len(pending),
                "together": int(together),
            },
        )
    except Exception:
        pass
    if sample_out is not None and together:
        sample_out.setdefault("__together__", "1")
    if count_out is not None:
        count_out.update(counts)
    return out


def _clean_feedback_sample(text: str, key: str) -> str:
    """
    Extract a readable window around a marker for logs.

    @author by ak
    """
    s = str(text or "")
    if not s:
        return key
    idx = s.find(key) if key else -1
    if idx < 0:
        # Keep CJK / punctuation runs only.
        cleaned = "".join(
            ch
            for ch in s
            if ("一" <= ch <= "鿿") or ch in "，。！？、：；,.!?:; "
        )
        return cleaned[:64] or key
    # Wider window so [系统]/[其他] tags near the marker are kept.
    left = max(0, idx - 24)
    right = min(len(s), idx + len(key) + 36)
    window = s[left:right]
    cleaned = "".join(
        ch
        for ch in window
        if ("一" <= ch <= "鿿") or ch in "，。！？、：；,.!?:;[]【】 "
    )
    return cleaned or key



def _channel_of_sample(text: str | None) -> str:
    """
    Map chat sample text to channel label 系统/其他 when tags are present.

    @author by ak
    """
    s = str(text or "")
    if not s:
        return ""
    # Prefer bracket form from live HUD.
    if "[系统]" in s or s.startswith("系统") or "系统]" in s:
        # Avoid classifying 其他 line that merely mentions 系统 somewhere rare.
        if "[其他]" in s or "其他]" in s:
            # Both tags: pick by which marker content dominates.
            if any(k in s for k in ("神罚", "捕羽")):
                return "其他"
            if any(k in s for k in ("答案正确", "答案错误", "进入活动")):
                return "系统"
        return "系统"
    if "[其他]" in s or s.startswith("其他") or "其他]" in s:
        return "其他"
    if any(k in s for k in ("神罚", "捕羽", "不能进入副本")) and "答案" not in s:
        return "其他"
    if any(k in s for k in ("答案正确", "答案错误", "请尽快进入活动", "重新来过")):
        return "系统"
    return ""


def _annotate_channel(text: str | None) -> str:
    """Prefix sample with [系统]/[其他] for probe logs. @author by ak"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    ch = _channel_of_sample(raw)
    if ch and f"[{ch}]" not in raw[:8]:
        return f"[{ch}] {raw}"
    return raw


# Live chat channel bracket tags seen in HUD / chat panels.
# Live UI paints channel as a separate colored label (系统/世界/其他);
# body text often has NO "[系统]" / "[世界]" prefix.
_CHAT_CHANNEL_TAG_MAP: tuple[tuple[str, str], ...] = (
    ("[系统]", "系统"),
    ("[其他]", "其他"),
    ("[世界]", "世界"),
    ("[附近]", "附近"),
    ("[队伍]", "队伍"),
    ("[帮派]", "帮派"),
    ("[帮会]", "帮派"),
    ("[家族]", "家族"),
    ("[私聊]", "私聊"),
    ("[当前]", "当前"),
    ("[地图]", "地图"),
    ("[战斗]", "战斗"),
    ("[提示]", "提示"),
)
_CHAT_BARE_CHANNEL_LABELS: tuple[tuple[str, str], ...] = (
    ("系统", "系统"),
    ("其他", "其他"),
    ("世界", "世界"),
    ("附近", "附近"),
    ("队伍", "队伍"),
    ("帮派", "帮派"),
    ("私聊", "私聊"),
)

_CHAT_FEEDBACK_KEYS: tuple[str, ...] = (
    "答案正确",
    "答案错误",
    "请尽快进入活动",
    "请大侠重新来过",
    "神罚状态",
    "捕羽状态",
    "不能进入副本",
    "队伍不满足",
    "副本无法开启",
    "距离过远",
    "不稳定状态",
    "冷却中",
    "请稍后再试",
    "请稍后",
)
_CHAT_SYSTEM_KEYS: tuple[str, ...] = (
    "你获得了",
    "你得到了",
    "恭喜你",
    "系统提示",
    "无法使用",
    "无法进入",
    "条件不足",
    "人数不足",
    "等级不足",
    "正在冷却",
    "操作过于频繁",
    "请重新",
)
# World kill-broadcast / drop shout (screenshot bodies).
_CHAT_WORLD_KEYS: tuple[str, ...] = (
    "击杀了",
    "击败了",
    "斩杀了",
    "击溃了",
    "并且拿到了",
    "拿到了",
    "在福州",
    "郊外",
    "霸服",
    "连斩",
    "首杀",
)
_CHAT_OTHER_KEYS: tuple[str, ...] = (
    "处于神罚",
    "处于捕羽",
    "神罚状态",
    "捕羽状态",
    "不能进入副本",
    "禁止进入",
    "受到惩罚",
)

_CHAT_CONTENT_KEYS: tuple[str, ...] = tuple(
    dict.fromkeys(
        list(_CHAT_FEEDBACK_KEYS)
        + list(_CHAT_SYSTEM_KEYS)
        + list(_CHAT_WORLD_KEYS)
        + list(_CHAT_OTHER_KEYS)
    )
)
_CHAT_HEAP_NEEDLES: tuple[str, ...] = tuple(
    dict.fromkeys(
        [tg for tg, _ in _CHAT_CHANNEL_TAG_MAP]
        + [lb for lb, _ in _CHAT_BARE_CHANNEL_LABELS]
        + list(_CHAT_FEEDBACK_KEYS)
        + list(_CHAT_SYSTEM_KEYS)
        + list(_CHAT_WORLD_KEYS)
        + list(_CHAT_OTHER_KEYS)
    )
)


def _normalize_chat_line(text: str | None) -> str:
    """
    Collapse whitespace and keep printable CJK / punctuation for dump rows.

    @author by ak
    """
    s = str(text or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    s = "".join(
        ch
        for ch in s
        if ch.isprintable()
        or ("一" <= ch <= "鿿")
        or ch in "，。！？、：；,.!?:;[]【】（）()《》<>·—_-|/\\ "
    )
    s = " ".join(s.split())
    return s.strip()


def _cjk_count(text: str) -> int:
    """Count CJK Unified Ideographs. @author by ak"""
    return sum(1 for ch in text if "一" <= ch <= "鿿")


def _is_chat_codeunit(code: int) -> bool:
    """
    True if UTF-16 code unit is plausible in a chat line.

    Tight set avoids treating random object bytes as Chinese text.

    @author by ak
    """
    if code in (0x09, 0x0A, 0x0D):
        return True
    if 0x20 <= code <= 0x7E:
        return True
    # CJK punctuation / common symbols
    if 0x3000 <= code <= 0x303F:
        return True
    if 0xFF00 <= code <= 0xFFEF:
        return True
    # CJK Unified
    if 0x4E00 <= code <= 0x9FFF:
        return True
    # a few general punctuation used in UI
    if code in (0x00B7, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026, 0x30FB):
        return True
    return False


def _is_ascii_mojibake_as_utf16(text: str) -> bool:
    """
    True when CJK-looking text is actually ASCII UI names mis-paired as UTF-16.

    Live junk examples (UTF-16LE decode of ASCII control names):
      慭敧畂瑴湯 <- ImageButton
      敔瑸牁慥   <- TextArea
      啁卉...    <- UI* widget names

    @author by ak
    """
    s = str(text or "")
    if len(s) < 3:
        return False
    try:
        raw = s.encode("utf-16le")
    except Exception:
        return False
    if len(raw) < 6:
        return False
    units = 0
    ascii_pair = 0
    hi_ascii = 0
    for i in range(0, len(raw) - 1, 2):
        lo = raw[i]
        hi = raw[i + 1]
        units += 1
        if 0x20 <= lo <= 0x7E and 0x20 <= hi <= 0x7E:
            ascii_pair += 1
        if 0x20 <= hi <= 0x7E:
            hi_ascii += 1
    if units <= 0:
        return False
    if ascii_pair / units >= 0.55 and hi_ascii / units >= 0.75:
        if not all(0x20 <= b <= 0x7E for b in raw):
            return ascii_pair / units >= 0.80
        ascii_s = raw.decode("ascii", errors="ignore")
        if re.search(r"[A-Za-z]{2,}[A-Z][a-z]{2,}", ascii_s):
            return True
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_ ]{2,}", ascii_s or ""):
            return True
        words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", ascii_s)]
        ui_words = {
            "image", "button", "label", "window", "text", "area", "scroll",
            "view", "panel", "widget", "control", "texture", "failed", "title",
            "preview", "video", "picture", "edit", "list", "item", "dialog",
            "message", "chat", "color", "font", "icon", "frame", "combo",
            "check", "radio", "progress", "slider", "tree", "grid", "table",
            "layer", "sprite", "bitmap", "render", "object", "class", "string",
            "value", "count", "index", "parent", "child", "width", "height",
            "left", "right", "top", "bottom", "name", "type", "data", "info",
            "main", "sub", "btn", "dlg", "wnd", "pic", "msg", "tip", "pop",
            "hint", "board", "rich", "html", "xml", "json", "buff", "skill",
            "quest", "task", "team", "guild", "friend", "system", "other",
            "world", "near", "map", "ui",
        }
        if any(w in ui_words or w.endswith("btn") or w.startswith("ui") for w in words):
            return True
        letters = sum(1 for c in ascii_s if c.isalpha())
        if letters >= 6 and letters / max(1, len(ascii_s)) >= 0.80:
            if any(re.search(r"[aeiouAEIOU]", w) and len(w) >= 4 for w in words):
                return True
    return False


def _is_quest_or_ui_junk_line(text: str) -> bool:
    """
    Reject quest XML / table fragments that share keywords with chat.

    @author by ak
    """
    s = str(text or "")
    if not s:
        return True
    bad_markers = (
        "<TaskName>",
        "</",
        "><",
        "<<",
        ">>",
        "TaskName",
        "TaskId",
        "QuestName",
        "NPCName",
        "Win_",
        "AUI",
    )
    if any(m in s for m in bad_markers):
        return True
    if re.match(r"^\d{1,6}\D", s) and not any(k in s for k in _CHAT_CONTENT_KEYS):
        return True
    if re.fullmatch(r"副本任务[一二三四五六七八九十\d]+", s):
        return True
    if "副本任务" in s and not any(
        k in s for k in ("不能进入", "无法开启", "答案", "神罚", "捕羽", "你获得")
    ):
        return True
    if s.count(">") >= 2 and s.count("<") >= 1:
        return True
    if s.startswith("务>") or s.startswith("称>") or s.endswith("><"):
        return True
    # UI titles that contain bare channel words
    ui_noise = (
        "世界地图",
        "世界频道",
        "世界BOSS",
        "世界boss",
        "系统设置",
        "系统菜单",
        "其他设置",
        "队伍设置",
        "帮派管理",
    )
    if any(n in s for n in ui_noise):
        return True
    return False


def _strip_ui_color_codes(text: str) -> str:
    """
    Drop ^rrggbb color codes and leading markup crumbs for readability.

    @author by ak
    """
    s = str(text or "")
    s = re.sub(r"\^[0-9a-fA-F]{6}", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s and not s.startswith("["):
        start = -1
        for i, ch in enumerate(s):
            if (
                ch == "["
                or ch in "-·~*"
                or ch.isalpha()
                or ch.isdigit()
                or ("一" <= ch <= "鿿")
            ):
                start = i
                break
        if start > 0:
            s = s[start:].strip()
    return s


def _looks_like_player_speech(text: str) -> bool:
    """
    True for "玩家名：内容" world/near chat bodies.

    @author by ak
    """
    s = str(text or "").strip()
    if not s or len(s) < 6:
        return False
    if re.match(r"^[\u4e00-\u9fffA-Za-z0-9_·]{2,12}[：:].{3,}$", s):
        return True
    if re.match(
        r"^[\u4e00-\u9fffA-Za-z0-9_·]{2,12}\[[^\]]{1,12}\][：:].{3,}$", s
    ):
        return True
    return False


def _is_chat_shaped_for_bare_label(text: str, label: str) -> bool:
    """
    When needle is bare 世界/系统/其他, require a chat-shaped window.

    @author by ak
    """
    s = _strip_ui_color_codes(_normalize_chat_line(text))
    if not s or label not in s:
        return False
    if _is_quest_or_ui_junk_line(s):
        return False
    if label == "系统":
        return any(k in s for k in _CHAT_SYSTEM_KEYS) or any(
            k in s for k in _CHAT_FEEDBACK_KEYS
        )
    if label == "其他":
        return any(k in s for k in _CHAT_OTHER_KEYS) or any(
            k in s for k in ("神罚", "捕羽", "不能进入")
        )
    if label == "世界":
        if any(k in s for k in _CHAT_WORLD_KEYS) or _looks_like_player_speech(s):
            return True
        return ("：" in s or ":" in s) and _cjk_count(s) >= 8
    if _looks_like_player_speech(s):
        return True
    return _cjk_count(s) >= 8 and ("：" in s or ":" in s or "，" in s or "。" in s)


def _is_plausible_chat_line(text: str | None, *, strict: bool = True) -> bool:
    """
    Reject binary mis-decoded junk; keep real chat/toast sentences.

    Accepts: channel tags, feedback/system/world/other keys, player speech.

    @author by ak
    """
    s = _normalize_chat_line(text)
    if not s or len(s) < 4 or len(s) > 160:
        return False
    s = _strip_ui_color_codes(s)
    if not s or len(s) < 4:
        return False
    if s.startswith("Win_") or s.startswith("AUI") or s.startswith("Dlg"):
        return False
    if re.fullmatch(r"[0-9A-Fa-fxX\s_\-:.]+", s):
        return False
    if _is_ascii_mojibake_as_utf16(s):
        return False
    if _is_quest_or_ui_junk_line(s):
        return False

    has_tag = any(tag in s for tag, _ in _CHAT_CHANNEL_TAG_MAP)
    has_key = any(k in s for k in _CHAT_CONTENT_KEYS)
    has_speech = _looks_like_player_speech(s)
    cjk = _cjk_count(s)
    letters = sum(1 for ch in s if ("A" <= ch <= "Z") or ("a" <= ch <= "z"))
    digits = sum(1 for ch in s if ch.isdigit())
    space_punct = sum(
        1
        for ch in s
        if ch.isspace()
        or ch in "，。！？、：；,.!?:;[]【】（）()《》<>·—_-|/\\+%=#@*&~`'\""
    )
    other = len(s) - cjk - letters - digits - space_punct
    if other > max(1, len(s) // 8):
        return False
    if s[0] in "<>{}@#$%^&*/~`\\":
        return False

    if has_tag and cjk >= 1:
        return True
    if has_key and cjk >= 2:
        if len(s) < 6 and not has_tag:
            return False
        return True
    if has_speech and cjk >= 6:
        return True
    for lab, _ in _CHAT_BARE_CHANNEL_LABELS:
        if lab in s[:12] and _is_chat_shaped_for_bare_label(s, lab):
            return True

    if strict:
        return False

    if cjk < 6:
        return False
    if cjk / max(1, len(s)) < 0.50:
        return False
    if not any(p in s for p in "，。！？、：；"):
        return False
    return not _is_ascii_mojibake_as_utf16(s)


def _classify_chat_channel(text: str | None) -> str:
    """
    Classify chat/toast line into channel label.

    Body often lacks [系统]/[世界]; infer from content.

    @author by ak
    """
    s = _strip_ui_color_codes(_normalize_chat_line(text))
    if not s:
        return "未知"
    for tag, label in _CHAT_CHANNEL_TAG_MAP:
        if tag in s:
            return label
    for lab, name in _CHAT_BARE_CHANNEL_LABELS:
        if s.startswith(lab + " ") or s.startswith(lab + "：") or s.startswith(
            lab + ":"
        ):
            return name
    fb = _channel_of_sample(s)
    if fb:
        return fb
    if any(k in s for k in ("神罚", "捕羽")) and "答案" not in s:
        return "其他"
    if any(k in s for k in _CHAT_OTHER_KEYS) and "你获得了" not in s:
        return "其他"
    if any(k in s for k in _CHAT_FEEDBACK_KEYS):
        if any(k in s for k in ("神罚", "捕羽", "不能进入副本")):
            return "其他"
        return "系统"
    if any(k in s for k in _CHAT_SYSTEM_KEYS):
        return "系统"
    if any(k in s for k in _CHAT_WORLD_KEYS) or _looks_like_player_speech(s):
        return "世界"
    return "未知"


def _extract_text_runs_from_raw(
    raw: bytes,
    *,
    min_len: int = 4,
    max_len: int = 120,
    require_cjk: bool = True,
) -> list[str]:
    """
    Extract plausible UTF-16LE chat lines from a buffer.

    Only well-formed code-unit runs in a tight charset are kept.
    Does NOT bulk-decode whole buffers as GBK (that produced junk).

    @author by ak
    """
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    n = len(raw)

    # Scan both alignments (0 and 1) for UTF-16LE runs.
    for align in (0, 1):
        i = align
        while i + 3 < n:
            code0 = raw[i] | (raw[i + 1] << 8)
            if not _is_chat_codeunit(code0) or code0 == 0:
                i += 2
                continue
            chars: list[str] = []
            j = i
            while j + 1 < n:
                code = raw[j] | (raw[j + 1] << 8)
                if code == 0:
                    break
                if not _is_chat_codeunit(code):
                    break
                chars.append(chr(code))
                j += 2
                if len(chars) >= max_len:
                    break
            if len(chars) >= min_len:
                s = _normalize_chat_line("".join(chars))
                if s and (not require_cjk or _cjk_count(s) >= 2):
                    if _is_plausible_chat_line(s) and s not in seen:
                        seen.add(s)
                        out.append(s)
            # Advance past this run (or one unit if short).
            i = max(j, i + 2)

    return out


def _split_chat_segments(text: str) -> list[str]:
    """
    Split a decoded window that may contain several toast lines glued together.

    Live heap windows can hold:
      你获得了2个时装玫瑰你获得了100个快乐兑换丹
    without clear separators.

    @author by ak
    """
    s = _strip_ui_color_codes(_normalize_chat_line(text))
    if not s:
        return []
    starters = (
        "[系统]",
        "[其他]",
        "[世界]",
        "[附近]",
        "[队伍]",
        "[帮派]",
        "[私聊]",
        "[提示]",
        "你获得了",
        "你得到了",
        "恭喜你",
        "答案正确",
        "答案错误",
        "请尽快进入活动",
        "请大侠重新来过",
        "队伍成员处于神罚",
        "队伍成员处于捕羽",
        "系统提示",
        # NOTE: do NOT split on 击杀了/拿到了 — they appear mid world-line
        # after player name (张小怪：在福州郊外击杀了...).
    )
    starts: list[int] = []
    for k in starters:
        pos = 0
        while True:
            i = s.find(k, pos)
            if i < 0:
                break
            starts.append(i)
            pos = i + max(1, len(k))
    if not starts:
        return [s]
    starts = sorted(set(starts))
    # Ignore nested starts that sit inside a very short previous span
    segs: list[str] = []
    for i, st in enumerate(starts):
        en = starts[i + 1] if i + 1 < len(starts) else len(s)
        seg = s[st:en].strip()
        if seg:
            segs.append(seg)
    # Prefix before first starter (rare)
    if starts and starts[0] > 0:
        head = s[: starts[0]].strip()
        if head and _cjk_count(head) >= 4:
            segs.insert(0, head)
    # de-dup keep order
    out: list[str] = []
    seen: set[str] = set()
    for seg in segs:
        if seg not in seen:
            seen.add(seg)
            out.append(seg)
    return out or [s]


def _clip_single_toast(text: str, needle: str = "") -> str:
    """
    Keep one toast sentence; drop heap-neighbor glue and UI crumbs.

    @author by ak
    """
    s = _strip_ui_color_codes(_normalize_chat_line(text))
    if not s:
        return ""
    # Keep captcha / hard-stop feedback sentences intact.
    for full in (
        "答案正确，请尽快进入活动",
        "答案错误，请大侠重新来过",
        "队伍成员处于神罚状态，不能进入副本",
        "不稳定状态下无法进行此操作",
    ):
        if full in s:
            return full
    if s.startswith("答案正确") or s.startswith("答案错误"):
        s2 = s.split("\r")[0].split("\n")[0]
        for cut in ("你获得了", "击杀了", "快捷键"):
            j = s2.find(cut, 4)
            if j > 4:
                s2 = s2[:j]
        return s2.strip(" ，,;；|")
    if "神罚" in s and "不能进入" in s:
        j = s.find("不能进入副本")
        if j >= 0:
            # include from 队伍/你/成员 start if present
            k = s.find("队伍成员")
            if k < 0:
                k = s.find("处于神罚")
            if k < 0:
                k = 0
            return s[k : j + len("不能进入副本")].strip()

    segs = _split_chat_segments(s)
    if len(segs) > 1:
        if needle:
            hit = [x for x in segs if needle in x]
            s = min(hit, key=len) if hit else segs[0]
        else:
            s = segs[0]

    cut_marks = (
        "在福州",
        "击杀了",
        "击败了",
        "斩杀了",
        "并且拿到",
        "拿到了",
        "键:",
        "键：",
        "设置 ",
        "快捷键",
        "多倍经验",
        "经验效果",
        "标到聊天",
        "坐标",
        "除恶",
        "换丹(",
        "中幸运",
        "幸运获得",
    )
    for m in cut_marks:
        if needle and (m in needle or needle in m):
            continue
        j = s.find(m)
        if j >= 6:
            head = s[:j].rstrip(" ，,;；|")
            if head and _cjk_count(head) >= 4:
                s = head
                break

    mm = re.search(r"^(.*[\u4e00-\u9fff])\s+\d", s)
    if mm:
        s = mm.group(1).strip()

    while s and (
        s[-1] in "*^#<>[]{}|\\/~`$@%&"
        or (s[-1].isascii() and not s[-1].isalnum() and s[-1] not in "。！？…")
    ):
        s = s[:-1].rstrip()

    m = re.match(r"(你获得了\d+个[\u4e00-\u9fffA-Za-z0-9_·]{1,24})", s)
    if m:
        return m.group(1)
    m = re.match(r"(你得到了\d+个[\u4e00-\u9fffA-Za-z0-9_·]{1,24})", s)
    if m:
        return m.group(1)
    m = re.match(r"(你获得了\d+个)", s)
    if m and _cjk_count(s[m.end() :]) == 0:
        return m.group(1)

    if "：" in s or ":" in s:
        s = s.split("\n")[0].strip()
    return s.strip()


def _toast_clean_score(text: str) -> tuple:
    """Higher is better. Prefer clean single toasts over glued junk. @author by ak"""
    s = str(text or "")
    if not s:
        return (-9999, 0)
    junk = 0
    junk += sum(1 for ch in s if ch in "*^#<>{}|\\/~`$@%")
    junk += len(re.findall(r"\d{3,}", s))
    junk += s.count("ffff") + s.count("设置") + s.count("快捷")
    junk += 3 if ("在福州" in s and "你获得了" in s) else 0
    junk += 3 if ("击杀了" in s and "你获得了" in s) else 0
    clean = 0
    if re.fullmatch(r"你获得了\d+个[\u4e00-\u9fffA-Za-z0-9_·]+", s):
        clean += 50
    if re.fullmatch(r"你获得了\d+个", s):
        clean += 20
    if _looks_like_player_speech(s):
        clean += 40
    if any(k in s for k in _CHAT_FEEDBACK_KEYS):
        clean += 40
    return (clean - junk * 5, len(s) if junk == 0 else -junk)


def _window_text_around_pat(
    blob: bytes,
    idx: int,
    pat: bytes,
    *,
    enc_label: str,
) -> list[str]:
    """
    Decode a tight null-terminated window around a needle hit.

    No large fixed-radius decode (that glued neighbor heap strings).

    @author by ak
    """
    out: list[str] = []
    if not blob or idx < 0 or idx >= len(blob) or not pat:
        return out

    needle_txt = ""
    try:
        if enc_label == "u16":
            needle_txt = pat.decode("utf-16le", errors="ignore")
        else:
            needle_txt = pat.decode("gbk", errors="ignore")
    except Exception:
        needle_txt = ""

    bare = {lb for lb, _ in _CHAT_BARE_CHANNEL_LABELS}

    def _push_raw(s: str) -> None:
        for seg in _split_chat_segments(s):
            seg = _clip_single_toast(seg, needle=needle_txt)
            seg = _strip_ui_color_codes(_normalize_chat_line(seg))
            if not seg:
                continue
            if needle_txt and needle_txt not in bare and needle_txt not in seg:
                continue
            if seg not in out:
                out.append(seg)

    if enc_label == "u16":
        if idx % 2 == 1:
            idx -= 1
            if idx < 0:
                idx = 0

        def _u16_at(off: int) -> int | None:
            if off < 0 or off + 1 >= len(blob):
                return None
            return blob[off] | (blob[off + 1] << 8)

        def _is_hex_unit(code: int) -> bool:
            ch = code & 0xFFFF
            return (
                (0x30 <= ch <= 0x39)
                or (0x41 <= ch <= 0x46)
                or (0x61 <= ch <= 0x66)
            )

        def _skip_color(off: int) -> int:
            code = _u16_at(off)
            if code != 0x5E:
                return off
            nxt = off + 2
            for _ in range(6):
                c = _u16_at(nxt)
                if c is None or not _is_hex_unit(c):
                    return off
                nxt += 2
            return nxt

        left = idx
        steps = 0
        while left >= 2 and steps < 80:
            code = _u16_at(left - 2)
            if code is None or code == 0:
                break
            if not _is_chat_codeunit(code) and code != 0x5E:
                break
            left -= 2
            steps += 1

        right = idx + len(pat)
        if right % 2 == 1:
            right += 1
        steps = 0
        chars_right = 0
        while right + 1 < len(blob) and steps < 120 and chars_right < 72:
            skipped = _skip_color(right)
            if skipped != right:
                right = skipped
                steps += 1
                continue
            code = _u16_at(right)
            if code is None or code == 0:
                break
            if not _is_chat_codeunit(code):
                break
            right += 2
            chars_right += 1
            steps += 1

        window = blob[left:right]
        try:
            raw_s = window.decode("utf-16le", errors="strict")
        except Exception:
            raw_s = window.decode("utf-16le", errors="ignore")
        _push_raw(raw_s)
        return out

    # GBK
    left = idx
    steps = 0
    while left > 0 and steps < 64:
        b = blob[left - 1]
        if b == 0 or b < 0x20:
            break
        left -= 1
        steps += 1
    right = idx + len(pat)
    steps = 0
    while right < len(blob) and steps < 96:
        b = blob[right]
        if b == 0 or b < 0x20:
            if b == 0x5E and right + 7 <= len(blob):
                hx = blob[right + 1 : right + 7]
                if all(
                    (0x30 <= x <= 0x39)
                    or (0x41 <= x <= 0x46)
                    or (0x61 <= x <= 0x66)
                    for x in hx
                ):
                    right += 7
                    steps += 7
                    continue
            break
        right += 1
        steps += 1
    window = blob[left:right]
    try:
        _push_raw(window.decode("gbk", errors="strict"))
    except Exception:
        _push_raw(window.decode("gbk", errors="ignore"))
    return out


def _prefer_longer_chat_rows(rows: list[dict]) -> list[dict]:
    """
    Dedup rows. Prefer cleaner single toasts over longer glued junk.

    Structured hist rows (tag3/channel_id) are kept as-is — do NOT re-run
    aggressive _clip_single_toast (it cuts real 附近/世界 lines at 击杀了).

    @author by ak
    """
    if not rows:
        return []
    by_text: dict[str, dict] = {}

    def _keep(seg: str, r: dict) -> None:
        if not seg:
            return
        prev = by_text.get(seg)
        if prev is None:
            nr = dict(r)
            nr["text"] = seg
            by_text[seg] = nr
            return
        # Prefer hist/tag3 over heap; then higher clean score / longer.
        def _pri(x: dict) -> tuple:
            src = str(x.get("source") or "")
            ls = str(x.get("label_src") or "")
            hist = 1 if src.startswith("hist:") or ls in ("tag3", "color") else 0
            try:
                cid_ok = 1 if int(x.get("channel_id", -1)) >= 0 else 0
            except Exception:
                cid_ok = 0
            return (hist, cid_ok, _toast_clean_score(str(x.get("text") or "")), len(str(x.get("text") or "")))

        cand = dict(r)
        cand["text"] = seg
        if _pri(cand) >= _pri(prev):
            by_text[seg] = cand

    for r in rows:
        raw = _strip_ui_color_codes(_normalize_chat_line(r.get("text")))
        if not raw:
            continue
        src = str(r.get("source") or "")
        ls = str(r.get("label_src") or "")
        try:
            cid = int(r.get("channel_id") if r.get("channel_id") is not None else -1)
        except Exception:
            cid = -1
        structured = src.startswith("hist:") or ls in ("tag3", "color", "tag3?") or cid >= 0
        if structured:
            seg = raw.strip()
            if not seg:
                continue
            # light junk only
            if _is_quest_or_ui_junk_line(seg) or _is_ascii_mojibake_as_utf16(seg):
                continue
            weird = sum(1 for ch in seg if (0xE000 <= ord(ch) <= 0xF8FF) or ord(ch) > 0xFF00)
            if weird >= 2:
                continue
            if _cjk_count(seg) < 2:
                continue
            if not r.get("channel"):
                r = dict(r)
                r["channel"] = _classify_chat_channel(seg)
            _keep(seg, r)
            continue

        for seg in _split_chat_segments(raw) or [raw]:
            seg = _clip_single_toast(seg)
            if not seg or not _is_plausible_chat_line(seg, strict=True):
                continue
            nr = dict(r)
            nr["channel"] = str(r.get("channel") or _classify_chat_channel(seg))
            _keep(seg, nr)

    texts = list(by_text.keys())
    drop: set[str] = set()
    for s in texts:
        for t2 in texts:
            if s == t2 or s in drop or t2 in drop:
                continue
            if s in t2 and len(t2) >= len(s) + 4:
                # drop shorter substring unless shorter is structured hist
                rs, rt = by_text[s], by_text[t2]
                s_hist = str(rs.get("source") or "").startswith("hist:")
                t_hist = str(rt.get("source") or "").startswith("hist:")
                if s_hist and not t_hist:
                    drop.add(t2)
                elif t_hist and not s_hist:
                    drop.add(s)
                else:
                    # prefer cleaner
                    if _toast_clean_score(t2) >= _toast_clean_score(s):
                        drop.add(s)
                    else:
                        drop.add(t2)
    out = [by_text[s] for s in texts if s not in drop]
    return out



def _collect_lines_from_dlg(
    session,
    dlg_name: str,
    *,
    require_shown: bool = False,
    obj_size: int = 0x800,
    nest: bool = True,
    nest_depth: int = 1,
    max_ptr_checks: int = 120,
    max_child_reads: int = 36,
    budget_s: float = 0.12,
    max_lines: int = 80,
) -> list[dict]:
    """
    Pull readable chat/toast lines from one AUI dialog object tree.

    Only null-terminated string pointers + in-object tag/key needles.
    No bulk child UTF-16 decode (widget-name mojibake).

    @author by ak
    """
    rows: list[dict] = []
    try:
        from app.core.plg_ui import query_dlg_show

        r = query_dlg_show(session, dlg_name, log=lambda _m: None)
    except Exception:
        return rows
    if not r.dlg_ptr:
        return rows
    if require_shown and not r.shown:
        return rows
    pm = getattr(session, "pm", None)
    if pm is None:
        return rows
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(
            pm.process_handle, int(r.dlg_ptr), max(0x200, int(obj_size))
        )
    except Exception:
        return rows

    dlg_ptr = int(r.dlg_ptr) & 0xFFFFFFFF
    deadline = time.monotonic() + max(0.01, float(budget_s))
    seen_text: set[str] = set()
    seen_ptr: set[int] = {dlg_ptr}
    ptr_checks = 0
    child_reads = 0
    max_ptrs = max(8, int(max_ptr_checks))
    max_children = max(4, int(max_child_reads))
    depth_limit = max(0, int(nest_depth if nest else 0))
    cap = max(1, int(max_lines))

    def _push(text: str, *, source: str, addr: int | None = None) -> None:
        s = _clip_single_toast(_strip_ui_color_codes(_normalize_chat_line(text)))
        if not _is_plausible_chat_line(s, strict=True):
            return
        if s in seen_text:
            return
        seen_text.add(s)
        rows.append(
            {
                "channel": _classify_chat_channel(s),
                "text": s,
                "source": source,
                "addr": int(addr) & 0xFFFFFFFF if addr else 0,
                "dlg": dlg_name,
                "dlg_ptr": dlg_ptr,
                "shown": bool(getattr(r, "shown", False)),
                "label_src": "infer",
            }
        )

    queue: list[tuple[bytes, int]] = [(raw, 0)]
    while queue and len(rows) < cap:
        if time.monotonic() >= deadline:
            break
        blob, depth = queue.pop(0)
        for tag, _label in _CHAT_CHANNEL_TAG_MAP:
            try:
                pat = tag.encode("utf-16le")
            except Exception:
                continue
            start = 0
            while len(rows) < cap:
                idx = blob.find(pat, start)
                if idx < 0:
                    break
                start = idx + len(pat)
                for s in _window_text_around_pat(blob, idx, pat, enc_label="u16"):
                    _push(s, source=f"dlg:{dlg_name}:tag", addr=(dlg_ptr + idx) & 0xFFFFFFFF)
        for key in _CHAT_CONTENT_KEYS:
            try:
                pat = key.encode("utf-16le")
            except Exception:
                continue
            if len(pat) < 4:
                continue
            idx = blob.find(pat)
            if idx < 0:
                continue
            for s in _window_text_around_pat(blob, idx, pat, enc_label="u16"):
                _push(s, source=f"dlg:{dlg_name}:key", addr=(dlg_ptr + idx) & 0xFFFFFFFF)

        for off in range(0, len(blob) - 3, 4):
            if time.monotonic() >= deadline or len(rows) >= cap:
                break
            if ptr_checks >= max_ptrs:
                break
            p = struct.unpack_from("<I", blob, off)[0]
            if p < 0x10000 or p > 0x7FFE0000:
                continue
            ptr_checks += 1
            s1 = _read_wstr(pm, p, 120)
            if s1:
                _push(s1, source=f"dlg:{dlg_name}:wstr", addr=p)
            s2 = _read_cstr_gbk(pm, p, max_bytes=180)
            if s2:
                _push(s2, source=f"dlg:{dlg_name}:gbk", addr=p)
            if depth >= depth_limit or child_reads >= max_children:
                continue
            if (p & 0xFFFFFFFF) in seen_ptr:
                continue
            seen_ptr.add(p & 0xFFFFFFFF)
            try:
                import pymem.memory

                child = pymem.memory.read_bytes(pm.process_handle, int(p), 0x280)
            except Exception:
                continue
            child_reads += 1
            if depth + 1 <= depth_limit:
                queue.append((child, depth + 1))
    return rows


def _probe_nearby_channel_label(
    blob: bytes,
    idx: int,
    *,
    radius: int = 0x180,
) -> str:
    """
    Try to recover left-rail channel label near a chat body hit.

    Live chat paints 系统/世界/其他 as a separate colored label; the body
    string often does not embed it. List-item / rich-text nodes frequently
    keep the label UTF-16 within a few hundred bytes of the body.

    Returns label like 系统/世界/其他 or "".

    @author by ak
    """
    if not blob or idx < 0:
        return ""
    left = max(0, idx - int(radius))
    right = min(len(blob), idx + int(radius))
    window = blob[left:right]
    # Prefer longer / more specific labels first; avoid bare 队 alone etc.
    # Order: bracket form then bare.
    candidates: list[tuple[int, str, int]] = []  # (priority, label, pos)
    for tag, label in _CHAT_CHANNEL_TAG_MAP:
        for enc in ("utf-16le", "gbk"):
            try:
                pat = tag.encode(enc)
            except Exception:
                continue
            pos = 0
            while True:
                j = window.find(pat, pos)
                if j < 0:
                    break
                candidates.append((0, label, j))
                pos = j + len(pat)
    for lab, name in _CHAT_BARE_CHANNEL_LABELS:
        # Skip ultra-common UI words unless they look isolated-ish.
        for enc in ("utf-16le", "gbk"):
            try:
                pat = lab.encode(enc)
            except Exception:
                continue
            pos = 0
            while True:
                j = window.find(pat, pos)
                if j < 0:
                    break
                # For bare labels require UTF-16 isolation: units before/after
                # not CJK letter (reduce 世界地图 / 系统设置 false bind).
                if enc == "utf-16le":
                    abs_j = left + j
                    # before
                    ok = True
                    if abs_j >= 2:
                        prev = blob[abs_j - 2] | (blob[abs_j - 1] << 8)
                        if 0x4E00 <= prev <= 0x9FFF:
                            ok = False
                    after = abs_j + len(pat)
                    if ok and after + 1 < len(blob):
                        nxt = blob[after] | (blob[after + 1] << 8)
                        if 0x4E00 <= nxt <= 0x9FFF:
                            ok = False
                    if not ok:
                        pos = j + len(pat)
                        continue
                candidates.append((1, name, j))
                pos = j + len(pat)
    if not candidates:
        return ""
    # Closest to body idx wins; bracket beats bare.
    center = idx - left
    candidates.sort(key=lambda it: (it[0], abs(it[2] - center)))
    return candidates[0][1]


def _collect_lines_near_channel_tags_heap(
    session,
    *,
    budget_s: float = 1.2,
    max_regions: int = 4500,
    max_lines: int = 160,
    chunk_size: int = 0x80000,
) -> list[dict]:
    """
    Budgeted writable-heap scan for [系统]/[其他]/… and feedback keys.

    High heap first. Developer chat dump only (not hot path).

    @author by ak
    """
    pm = getattr(session, "pm", None)
    rows: list[dict] = []
    raw_hits: dict[str, int] = {}
    kept_hits = 0
    regions_n = 0
    if pm is None:
        try:
            session._chat_heap_diag = {
                "regions": 0,
                "raw_hits": {},
                "raw_total": 0,
                "kept": 0,
                "rows": 0,
                "error": "no_pm",
            }
        except Exception:
            pass
        return rows
    try:
        import pymem.memory
        from app.core.game_attach import _iter_writable_regions
    except Exception as e:
        try:
            session._chat_heap_diag = {
                "regions": 0,
                "raw_hits": {},
                "raw_total": 0,
                "kept": 0,
                "rows": 0,
                "error": str(e),
            }
        except Exception:
            pass
        return rows

    # Channel tags + feedback/system phrases only (no broad 副本/队伍/进入).
    needles: list[tuple[str, bytes, str]] = []
    for tname in _CHAT_HEAP_NEEDLES:
        for enc, label in (("utf-16le", "u16"), ("gbk", "gbk")):
            try:
                pat = tname.encode(enc)
            except Exception:
                continue
            if pat and len(pat) >= 4:
                needles.append((tname, pat, label))
    if not needles:
        return rows

    end_t = time.monotonic() + max(0.05, float(budget_s))
    try:
        regions = list(
            _iter_writable_regions(pm, max_regions=max(400, int(max_regions)))
        )
    except Exception as e:
        try:
            session._chat_heap_diag = {
                "regions": 0,
                "raw_hits": {},
                "raw_total": 0,
                "kept": 0,
                "rows": 0,
                "error": f"regions:{e}",
            }
        except Exception:
            pass
        return rows

    regions_n = len(regions)

    def _rank(item: tuple[int, int]) -> tuple[int, int]:
        base = int(item[0])
        if 0x20000000 <= base <= 0x7FFE0000:
            return (0, -base)
        if 0x01000000 <= base < 0x20000000:
            return (1, -base)
        return (2, -base)

    regions.sort(key=_rank)
    seen_text: set[str] = set()
    cap = max(1, int(max_lines))
    step = max(0x10000, int(chunk_size))

    for base, size in regions:
        if time.monotonic() >= end_t or len(rows) >= cap:
            break
        base = int(base)
        size = int(size)
        if size <= 0:
            continue
        off = 0
        while off < size and time.monotonic() < end_t and len(rows) < cap:
            take = min(step, size - off)
            addr = base + off
            try:
                blob = pymem.memory.read_bytes(pm.process_handle, addr, take)
            except Exception:
                off += take
                continue
            for tag, pat, enc_label in needles:
                if len(rows) >= cap or time.monotonic() >= end_t:
                    break
                start_i = 0
                hits_this = 0
                # Higher cap: short templates and full toast lines co-exist.
                while hits_this < 24:
                    idx = blob.find(pat, start_i)
                    if idx < 0:
                        break
                    start_i = idx + max(2, len(pat))
                    hits_this += 1
                    raw_hits[tag] = raw_hits.get(tag, 0) + 1
                    hit_addr = (addr + idx) & 0xFFFFFFFF
                    cands = _window_text_around_pat(
                        blob, idx, pat, enc_label=enc_label
                    )
                    for s in cands:
                        s = _clip_single_toast(
                            _strip_ui_color_codes(_normalize_chat_line(s)),
                            needle=str(tag or ""),
                        )
                        if not s:
                            continue
                        bare_labels = {lb for lb, _ in _CHAT_BARE_CHANNEL_LABELS}
                        if tag in bare_labels:
                            if not _is_chat_shaped_for_bare_label(s, tag):
                                continue
                        elif tag not in s and not any(
                            k in s for k in _CHAT_CONTENT_KEYS
                        ):
                            if not _looks_like_player_speech(s):
                                continue
                        if not _is_plausible_chat_line(s, strict=True):
                            continue
                        # Prefer longer: replace shorter prefix already kept.
                        shorter = [
                            x
                            for x in list(seen_text)
                            if x != s and (x in s or s in x)
                        ]
                        for old in shorter:
                            if len(old) <= len(s) and old in s:
                                seen_text.discard(old)
                                rows[:] = [
                                    r
                                    for r in rows
                                    if str(r.get("text") or "") != old
                                ]
                            elif len(s) < len(old) and s in old:
                                s = ""
                                break
                        if not s or s in seen_text:
                            continue
                        seen_text.add(s)
                        kept_hits += 1
                        # Prefer explicit label recovered near the body in memory
                        # (left color-rail). Fall back to content heuristics.
                        near_ch = _probe_nearby_channel_label(blob, idx)
                        ch = near_ch or _classify_chat_channel(s)
                        src = f"heap:{enc_label}:{tag}"
                        if near_ch:
                            src = f"{src}+label:{near_ch}"
                        rows.append(
                            {
                                "channel": ch,
                                "text": s,
                                "source": src,
                                "addr": hit_addr,
                                "dlg": "",
                                "dlg_ptr": 0,
                                "shown": False,
                                "label_src": "near" if near_ch else "infer",
                            }
                        )
                        if len(rows) >= cap * 2:
                            # collect extra then collapse by longest
                            break
            off += take

    rows = _prefer_longer_chat_rows(rows)[:cap]
    try:
        top = sorted(raw_hits.items(), key=lambda kv: -kv[1])[:8]
        session._chat_heap_diag = {
            "regions": regions_n,
            "raw_hits": dict(top),
            "raw_total": int(sum(raw_hits.values())),
            "kept": int(kept_hits),
            "rows": len(rows),
        }
    except Exception:
        pass
    return rows


def dump_chat_messages(
    session,
    *,
    budget_s: float = 2.5,
    max_lines: int = 200,
    include_heap: bool = True,
    include_general: bool = True,
    log: LogFn | None = None,
) -> list[dict]:
    """
    Developer panel chat dump — memory-only.

    - Always reads live rich-history buffer (high heap).
    - include_general=False: show only exact feedback canons from that window.
    - include_heap: only fills missing exact feedback canons via high-heap hitmap
      (never phrase-fragment ordinary chat).

    @author by ak
    """
    log = log or (lambda _m: None)
    t0 = time.monotonic()
    deadline = t0 + max(0.3, float(budget_s))
    cap = max(1, int(max_lines))
    _FULL_FEEDBACK = (
        "答案正确，请尽快进入活动",
        "答案错误，请大侠重新来过",
        "队伍成员处于神罚状态，不能进入副本",
        "队伍成员处于捕羽状态，不能进入副本",
        "不稳定状态下无法进行此操作",
        "入口未打开，目前不能进入",
    )

    def _remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    rows: list[dict] = []
    try:
        from app.core.chat_history import dump_chat_history, make_chat_identity

        hist_budget = min(2.0, max(0.6, _remaining() * 0.85))
        hist = dump_chat_history(
            session,
            budget_s=hist_budget,
            max_lines=max(cap, min(300, cap + 40)),
            log=log,
            mode=("full" if include_general else "feedback"),
        )
        for h in hist:
            text = _strip_ui_color_codes(_normalize_chat_line(h.get("text"))).strip()
            if not text:
                continue
            # collapse only exact embedded canons without garbage
            for full in _FULL_FEEDBACK:
                if full in text and len(text) <= len(full) + 6:
                    text = full
                    break
            try:
                cid = int(
                    h.get("channel_id") if h.get("channel_id") is not None else -1
                )
            except Exception:
                cid = -1
            ch = str(h.get("channel") or "")
            if text in _FULL_FEEDBACK:
                if text.startswith("答案"):
                    ch, cid = "系统", 10
                else:
                    ch, cid = "其他", 11
            if not ch:
                ch = _classify_chat_channel(text) or "未知"
            addr = int(h.get("addr") or 0) & 0xFFFFFFFF
            items = list(h.get("item_ids") or [])
            seq = (
                int(h["seq"])
                if h.get("seq") is not None and str(h.get("seq")) != ""
                else -1
            )
            src = str(h.get("source") or "hist:rich")
            ident = make_chat_identity(
                text=text,
                addr=addr,
                channel_id=cid,
                item_ids=items,
                color=str(h.get("color") or ""),
                seq=seq,
                source=src,
            )
            rows.append(
                {
                    "channel": ch,
                    "text": text,
                    "source": src,
                    "addr": addr,
                    "dlg": "",
                    "dlg_ptr": 0,
                    "shown": True,
                    "label_src": str(h.get("label_src") or "tag3"),
                    "seq": seq,
                    "color": str(h.get("color") or ""),
                    "channel_id": cid,
                    "item_ids": items,
                    "content_key": ident.get("content_key") or "",
                    "mem_key": ident.get("mem_key") or "",
                    "uid": ident.get("uid") or "",
                    "addr_page": int(ident.get("addr_page") or 0),
                }
            )
        log(
            f"聊天dump: mem-hist lines={len(rows)} budget={hist_budget:.2f}s "
            f"remain={_remaining():.2f}s"
        )
    except Exception as e:
        log(f"聊天dump: mem-hist失败 {e}")
        rows = []

    # Optional: missing feedback canons only
    have = {str(r.get("text") or "") for r in rows}
    missing = [c for c in _FULL_FEEDBACK if c not in have]
    if include_heap and missing and _remaining() > 0.15:
        try:
            from app.core.chat_history import collect_feedback_hit_map, make_chat_identity

            hit_map = collect_feedback_hit_map(
                session,
                budget_s=min(0.7, max(0.2, _remaining() * 0.4)),
                log=None,
            )
            added = 0
            for k in missing:
                cands = [
                    (int(a) & 0xFFFFFFFF, ctx)
                    for a, ctx in (hit_map.get(k) or [])
                    if (int(a) & 0xFFFFFFFF) >= 0x70000000
                ]
                if not cands:
                    continue
                cands.sort(key=lambda x: -x[0])
                addr, ctx = cands[0]
                ch, cid = ("系统", 10) if k.startswith("答案") else ("其他", 11)
                ident = make_chat_identity(
                    text=k, addr=addr, channel_id=cid, source="hitmap"
                )
                rows.append(
                    {
                        "channel": ch,
                        "text": k,
                        "source": f"hitmap:u16:{ctx or '-'}",
                        "addr": addr,
                        "dlg": "",
                        "dlg_ptr": 0,
                        "shown": False,
                        "label_src": "hitmap",
                        "seq": len(rows),
                        "color": "9dbfde" if cid == 10 else "dadad9",
                        "channel_id": cid,
                        "item_ids": [],
                        "content_key": ident.get("content_key") or "",
                        "mem_key": ident.get("mem_key") or "",
                        "uid": ident.get("uid") or "",
                        "addr_page": int(ident.get("addr_page") or 0),
                    }
                )
                added += 1
            log(f"聊天dump: fb-hitmap missing={missing} added={added}")
        except Exception as e:
            log(f"聊天dump: fb-hitmap失败 {e}")
    else:
        log(f"聊天dump: hitmap跳过 missing={missing} heap={'on' if include_heap else 'off'}")

    # final filter for feedback-only view
    if not include_general:
        rows = [r for r in rows if str(r.get("text") or "") in _FULL_FEEDBACK]

    # Prefer high-heap when enough
    high = [r for r in rows if int(r.get("addr") or 0) >= 0x70000000]
    if len(high) >= max(2, min(5, len(rows) // 2)):
        rows = high

    # chrono: seq then addr
    def _key(r: dict) -> tuple:
        try:
            seq = int(r.get("seq")) if r.get("seq") is not None else -1
        except Exception:
            seq = -1
        return (0 if seq >= 0 else 1, seq if seq >= 0 else 0, int(r.get("addr") or 0))

    rows.sort(key=_key)
    # Baseline: keep distinct addresses (new rails must not be dropped here).
    # Residual plain/triple collapse already done in chat_history dump path.
    seen_addr: set[int] = set()
    ordered: list[dict] = []
    for r in rows:
        t = str(r.get("text") or "")
        if not t:
            continue
        addr = int(r.get("addr") or 0) & 0xFFFFFFFF
        if addr:
            if addr in seen_addr:
                continue
            seen_addr.add(addr)
        ordered.append(r)
    rows = ordered
    if len(rows) > cap:
        rows = rows[-cap:]
    for i, r in enumerate(rows):
        r["seq"] = i

    elapsed = time.monotonic() - t0
    n_hist = sum(1 for r in rows if str(r.get("source") or "").startswith("hist"))
    n_high = sum(1 for r in rows if int(r.get("addr") or 0) >= 0x70000000)
    log(
        f"聊天dump: lines={len(rows)} hist={n_hist} high={n_high} "
        f"general={'on' if include_general else 'off'} "
        f"heap={'on' if include_heap else 'off'} "
        f"elapsed={elapsed:.2f}s"
    )
    return rows



def _feedback_from_marker_hits(
    hits: dict[str, bool],
    *,
    trust_chat_hard: bool = False,
    method_prefix: str = "heap",
) -> "CaptchaAnswerFeedback | None":
    """
    Classify short-marker presence into ok/fail/block feedback.

    Priority (delta hits only; residual history must not be True here):
      1) 答案错误 fail
      2) 神罚/捕羽 hard (+ok or trust_chat_hard)
      3) 答案正确 ok
    Hard alone without proven ok / trust is ignored (history residual).

    @author by ak
    """
    ok = bool(hits.get(_OK_SHORT_TEXT) or hits.get(_OK_SHORT_TAIL))
    fail = bool(hits.get(_FAIL_SHORT_TEXT))
    hard = bool(hits.get(_SHENFA_SHORT_TEXT) or hits.get(_BUYU_SHORT_TEXT))

    # Fail is decisive for this submit; never lose to residual hard+ok.
    if fail:
        return CaptchaAnswerFeedback(
            kind="fail",
            text=CAPTCHA_FAIL_TEXT,
            msg_id=CAPTCHA_FAIL_MSG_ID,
            method=f"{method_prefix}:fail",
        )
    if hard and (ok or trust_chat_hard):
        text = (
            SHENFA_BLOCK_TEXT
            if hits.get(_SHENFA_SHORT_TEXT)
            else BUYU_BLOCK_TEXT
        )
        return CaptchaAnswerFeedback(
            kind="block",
            text=text,
            method=f"{method_prefix}:hard"
            + ("+ok" if ok else "+trust"),
            error=CAPTCHA_OK_TEXT if ok else None,
        )
    if ok:
        return CaptchaAnswerFeedback(
            kind="ok",
            text=CAPTCHA_OK_TEXT,
            msg_id=CAPTCHA_OK_MSG_ID,
            method=f"{method_prefix}:ok",
        )
    # Hard alone without proven ok / trust → ignore (history residual).
    return None


def snapshot_entry_open_block_hits(session) -> dict[str, int]:
    """Baseline exact entry-block text copies before MatterInteract."""
    pm = getattr(session, "pm", None)
    if pm is None:
        return {ENTRY_OPEN_BLOCK_TEXT: 0}
    return {
        ENTRY_OPEN_BLOCK_TEXT: len(
            _scan_text_hits(pm, ENTRY_OPEN_BLOCK_TEXT, limit=12)
        )
    }


# Live residual 答案错误 copies can exceed 48 after many fails; keep delta room.
_ANSWER_HIT_SCAN_LIMIT = 96

# Full + short markers. Short keys catch chat ring-buffer lines that do not
# always allocate a fresh full-sentence catalog copy.
_ANSWER_BASELINE_TEXTS = (
    CAPTCHA_FAIL_TEXT,
    CAPTCHA_OK_TEXT,
    SHENFA_BLOCK_TEXT,
    BUYU_BLOCK_TEXT,
    _BUYU_SHORT_TEXT,
    _SHENFA_SHORT_TEXT,
    _OK_SHORT_TEXT,
    _OK_SHORT_TAIL,
    _FAIL_SHORT_TEXT,
)


def snapshot_captcha_answer_hits(session) -> dict[str, int]:
    """
    Baseline UTF-16 copy counts for captcha/block toasts before submit.

    Catalog always holds fail/ok templates; only *new* copies after submit are
    treated as live system messages for this answer.
    """
    pm = getattr(session, "pm", None)
    texts = _ANSWER_BASELINE_TEXTS
    if pm is None:
        return {t: 0 for t in texts}
    out: dict[str, int] = {}
    for t in texts:
        out[t] = len(_scan_text_hits(pm, t, limit=_ANSWER_HIT_SCAN_LIMIT))
    return out


def _hit_count_delta(
    pm,
    text: str,
    baseline: dict[str, int] | None,
    *,
    limit: int = _ANSWER_HIT_SCAN_LIMIT,
) -> tuple[int, int, int]:
    """Return (now, before, delta) for one UTF-16 template string."""
    now = len(_scan_text_hits(pm, text, limit=limit))
    before = int((baseline or {}).get(text, 0))
    # If baseline was capped at old limit, treat equality at cap as unknown delta 0
    # but allow growth beyond previous scan window when limit raised.
    return now, before, max(0, now - before)


def probe_entry_open_block_text(
    session,
    *,
    baseline: dict[str, int] | None = None,
    log: LogFn | None = None,
    heavy: bool = True,
) -> CaptchaAnswerFeedback | None:
    """
    Detect a newly displayed server refusal before captcha opens.

    UI toast/dialog text is always checked first (bounded). Process-wide
    UTF-16 copy deltas are optional via heavy=True — never pass heavy=True
    on tight runner polls; pattern_scan_all has no hard deadline.

    @author by ak
    """
    log = log or (lambda _m: None)
    pm = getattr(session, "pm", None)
    if pm is None:
        return None

    dialog_names = [
        "Win_Popmsg",
        "Win_PopWarningMsg",
        "Win_MessageBox",
        "Win_MsgBox",
        "Win_InstanceTip",
        "Win_GameEventPop",
    ]
    cached = getattr(session, "_entry_block_dialog_names", None)
    if cached is None:
        discovered: list[str] = []
        try:
            from app.core.plg_ui import list_game_ui_dlg_names

            listed = list_game_ui_dlg_names(session, log=lambda _m: None)
            keys = (
                "msg",
                "message",
                "pop",
                "tip",
                "instance",
                "dungeon",
                "prompt",
                "notice",
            )
            discovered = [
                name
                for name in listed.names
                if any(k in name.lower() for k in keys)
            ][:48]
        except Exception as e:
            log(f"entry block dialog discovery err: {e}")
        try:
            setattr(session, "_entry_block_dialog_names", tuple(discovered))
        except Exception:
            pass
        cached = discovered
    for name in cached or ():
        if name not in dialog_names:
            dialog_names.append(str(name))

    for dlg in dialog_names:
        s = _dlg_utf16_has_keys(session, dlg, _ENTRY_OPEN_BLOCK_KEYS)
        if s:
            return CaptchaAnswerFeedback(
                kind="block",
                text=s,
                method=f"entry_dlg:{dlg}",
            )

    # Diagnostic / one-shot paths only: full-process UTF-16 delta.
    if not heavy:
        return None

    hits = _scan_text_hits(pm, ENTRY_OPEN_BLOCK_TEXT, limit=12)
    if baseline is not None:
        before = int(baseline.get(ENTRY_OPEN_BLOCK_TEXT, 0))
    else:
        before = 1
    if len(hits) > before:
        log(f"entry open block new text copies before={before} now={len(hits)}")
        return CaptchaAnswerFeedback(
            kind="block",
            text=ENTRY_OPEN_BLOCK_TEXT,
            method=f"entry_u16_hits={len(hits)} baseline={before}",
        )
    return None


def _clip_feedback_text(text: str | None, limit: int = 80) -> str:
    """
    Compact one-line text for probe logs.

    @author by ak
    """
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


def _format_heap_marker_hits(
    hits: dict[str, bool] | None,
    samples: dict[str, str] | None = None,
) -> str:
    """
    Render short-marker presence map for logs.

    @author by ak
    """
    if not hits:
        return "-"
    parts = []
    for key in _HEAP_FEEDBACK_MARKERS:
        if key not in hits:
            continue
        flag = "Y" if hits.get(key) else "N"
        sample = ""
        if samples and hits.get(key) and samples.get(key):
            sample = f"({_clip_feedback_text(samples.get(key), 48)})"
        parts.append(f"{key}={flag}{sample}")
    return ",".join(parts) if parts else "-"


def _log_feedback_probe_snapshot(
    log: LogFn | None,
    *,
    s_hard: str | None,
    m_hard: str,
    s_ok: str | None,
    m_ok: str,
    s_fail: str | None,
    m_fail: str,
    heap_hits: dict[str, bool] | None,
    decision: CaptchaAnswerFeedback | None,
    trust_chat_hard: bool,
    heap_scanned: bool,
    heap_samples: dict[str, str] | None = None,
) -> None:
    """
    Always-visible probe dump: raw 系统/其他-class hits + classification.

    Throttled only for identical empty snapshots so countdown polls stay readable.

    @author by ak
    """
    if log is None:
        return
    lines: list[str] = []
    if s_ok:
        lines.append(
            f"{_clip_feedback_text(_annotate_channel(s_ok))} via={m_ok or '-'}"
        )
    if s_hard:
        lines.append(
            f"{_clip_feedback_text(_annotate_channel(s_hard))} via={m_hard or '-'}"
        )
    if s_fail:
        lines.append(f"[失败] {_clip_feedback_text(s_fail)} via={m_fail or '-'}")
    if heap_scanned:
        lines.append(f"[heap] {_format_heap_marker_hits(heap_hits, heap_samples)}")
    if decision is not None:
        lines.append(
            f"[判定] kind={decision.kind} method={decision.method} "
            f"text={_clip_feedback_text(decision.text)} "
            f"error={_clip_feedback_text(decision.error)} "
            f"trust_hard={int(bool(trust_chat_hard))}"
        )
    elif not lines:
        lines.append(
            f"[空] ui=none heap={'scanned' if heap_scanned else 'skip'} "
            f"trust_hard={int(bool(trust_chat_hard))}"
        )
    else:
        lines.append(
            f"[判定] none trust_hard={int(bool(trust_chat_hard))} "
            f"heap={'scanned' if heap_scanned else 'skip'}"
        )

    blob = " | ".join(lines)
    # Dedup identical empty / same-content dumps for ~0.9s.
    try:
        # session-less throttle key on function attr
        now = time.monotonic()
        last_blob = getattr(_log_feedback_probe_snapshot, "_last_blob", "")
        last_ts = float(getattr(_log_feedback_probe_snapshot, "_last_ts", 0.0) or 0.0)
        is_empty = decision is None and not s_ok and not s_hard and not s_fail
        if is_empty and blob == last_blob and (now - last_ts) < 0.9:
            return
        if (not is_empty) and blob == last_blob and (now - last_ts) < 0.35:
            return
        setattr(_log_feedback_probe_snapshot, "_last_blob", blob)
        setattr(_log_feedback_probe_snapshot, "_last_ts", now)
    except Exception:
        pass
    log(f"yaolu feedback probe: {blob}")



def snapshot_feedback_tap_baseline(
    session,
    *,
    log: LogFn | None = None,
) -> bool:
    """Arm the exact AddChatMessage cursor for the next captcha submit.

    Returns True only while the injected tap reports ACTIVE.  Callers may then
    skip the legacy heap/history snapshots; when the tap is unavailable they
    retain the old baseline path unchanged.
    """
    log = log or (lambda _m: None)
    tap_cursor = None
    active = False
    try:
        from app.core.chat_tap import CHAT_TAP_ACTIVE, ChatTapReader

        tap_reader = getattr(session, "_chat_tap_reader", None)
        if tap_reader is None and getattr(session, "pid", None):
            tap_reader = ChatTapReader.open(int(session.pid))
            if tap_reader is not None:
                setattr(session, "_chat_tap_reader", tap_reader)
        if tap_reader is not None:
            tap_header = tap_reader.header()
            active = int(tap_header.get("status") or 0) == CHAT_TAP_ACTIVE
            if active:
                tap_cursor = tap_reader.latest_cursor
                log(
                    f"yaolu feedback tap baseline cursor={tap_cursor} "
                    f"target=0x{int(tap_header.get('target_va') or 0):08X}"
                )
            else:
                log(
                    f"yaolu feedback tap inactive status={tap_header.get('status')} "
                    f"error={tap_header.get('error')!r}"
                )
    except Exception as e:
        log(f"yaolu feedback tap baseline err: {e}")
        active = False
        tap_cursor = None
    try:
        setattr(session, "_feedback_tap_cursor", tap_cursor)
        setattr(session, "_feedback_tap_fast_active", bool(active))
    except Exception:
        pass
    return bool(active)


def snapshot_feedback_heap_baseline(
    session,
    *,
    budget_s: float = 0.9,
    log: LogFn | None = None,
) -> dict[str, int]:
    """
    Capture writable-heap short-marker *counts* before captcha confirm.

    This is the only reliable way we can tell "new chat line after submit"
    without game message IDs/timestamps: later probes treat count growth as
    this-submit evidence. Residual history stays in baseline and is ignored.

    Limits (honest):
      - no per-line seq/time from client; only marker occurrence counts
      - if a buffer reuses memory without growing count, a new line can be missed
      - full-process UTF-16 scans are intentionally NOT used on the hot path

    @author by ak
    """
    log = log or (lambda _m: None)
    if snapshot_feedback_tap_baseline(session, log=log):
        # Do not carry a previous submit's fallback baseline into this submit.
        # If the tap is unavailable on a later submit, the normal path below
        # captures fresh heap/history state again.
        try:
            setattr(session, "_feedback_heap_baseline", None)
            setattr(session, "_feedback_heap_baseline_src", "tap_fast")
            setattr(session, "_feedback_hist_baseline", None)
            setattr(session, "_feedback_message_cursor", None)
        except Exception:
            pass
        log("yaolu feedback tap active; skip heap/hist pre-confirm baseline")
        return {m: 0 for m in _HEAP_FEEDBACK_MARKERS}
    counts: dict[str, int] = {m: 0 for m in _HEAP_FEEDBACK_MARKERS}
    try:
        _scan_writable_heap_markers(
            session,
            budget_s=max(0.15, float(budget_s)),
            count_out=counts,
        )
    except Exception as e:
        log(f"yaolu feedback heap baseline pre-confirm err: {e}")
    try:
        setattr(session, "_feedback_heap_baseline", dict(counts))
        setattr(session, "_feedback_heap_baseline_ts", time.monotonic())
        setattr(session, "_feedback_heap_baseline_src", "pre_confirm")
    except Exception:
        pass
    try:
        st = getattr(session, "_feedback_heap_last_scan_stats", None)
    except Exception:
        st = None
    if st:
        log(
            f"yaolu feedback heap baseline pre-confirm={dict(counts)} "
            f"scan={st}"
        )
    else:
        log(f"yaolu feedback heap baseline pre-confirm={dict(counts)}")
    # Pair with fast hist sentence counts (preferred delta source).
    try:
        snapshot_feedback_hist_baseline(session, budget_s=1.0, log=log)
    except Exception as e:
        log(f"yaolu feedback hist baseline side-call err: {e}")
    return dict(counts)


def snapshot_feedback_hist_baseline(
    session,
    *,
    budget_s: float = 0.7,
    log: LogFn | None = None,
) -> dict:
    """
    Capture the ordered rich-history cursor before captcha confirm.

    Exact sentence counts remain diagnostic. Decisions use only append-journal
    messages after this cursor, so old residual text and scan-count jitter cannot
    be attributed to the current submission.

    @author by ak
    """
    log = log or (lambda _m: None)
    if snapshot_feedback_tap_baseline(session, log=log):
        try:
            setattr(session, "_feedback_hist_baseline", None)
            setattr(session, "_feedback_message_cursor", None)
        except Exception:
            pass
        log("yaolu feedback tap active; skip rich-history pre-confirm baseline")
        return {}
    try:
        from app.core.chat_history import (
            get_chat_message_buffer,
            snapshot_feedback_fingerprint,
        )

        fp = snapshot_feedback_fingerprint(
            session, budget_s=max(0.35, float(budget_s)), log=None
        )
        message_buffer = get_chat_message_buffer(session)
        message_cursor = message_buffer.prime(fp.get("history_rows") or [])
    except Exception as e:
        log(f"yaolu feedback hist baseline err: {e}")
        fp = {"counts": {}, "texts": tuple(), "sig_hash": ""}
        message_cursor = None
    try:
        setattr(session, "_feedback_hist_baseline", dict(fp))
        setattr(session, "_feedback_hist_baseline_ts", time.monotonic())
        setattr(session, "_feedback_message_cursor", message_cursor)
        # Warm heap scanner toward addresses that already host feedback text.
        bases: list[int] = []
        for it in fp.get("instances") or ():
            try:
                a = int(it[1]) & 0xFFFFFFFF
            except Exception:
                continue
            if a:
                bases.append(a & 0xFFF00000)
        if bases:
            prev = list(getattr(session, "_feedback_heap_hit_bases", ()) or ())
            merged = []
            for b in list(bases) + prev:
                bi = int(b)
                if bi and bi not in merged:
                    merged.append(bi)
            setattr(session, "_feedback_heap_hit_bases", tuple(merged[:24]))
    except Exception:
        pass
    log(
        f"yaolu feedback hist baseline counts={fp.get('counts')} "
        f"inst={len(fp.get('instances') or [])} "
        f"cursor={message_cursor} hist_rows={len(fp.get('history_rows') or [])} "
        f"hash={fp.get('sig_hash')}"
    )
    return dict(fp)


def _probe_feedback_tap_delta(
    session,
    *,
    trust_chat_hard: bool = False,
    log: LogFn | None = None,
) -> CaptchaAnswerFeedback | None:
    """Consume exact AddChatMessage calls after the pre-confirm cursor."""
    log = log or (lambda _m: None)
    cursor = getattr(session, "_feedback_tap_cursor", None)
    reader = getattr(session, "_chat_tap_reader", None)
    if cursor is None or reader is None:
        try:
            setattr(session, "_feedback_tap_fast_active", False)
        except Exception:
            pass
        return None
    try:
        events, consumed, lost, header = reader.read_after(int(cursor))
    except Exception as e:
        log(f"yaolu feedback tap read err: {e}")
        try:
            setattr(session, "_feedback_tap_fast_active", False)
        except Exception:
            pass
        return None
    try:
        from app.core.chat_tap import CHAT_TAP_ACTIVE

        status = header.get("status") if isinstance(header, dict) else None
        healthy = not lost and (
            status is None or int(status or 0) == CHAT_TAP_ACTIVE
        )
        setattr(session, "_feedback_tap_fast_active", bool(healthy))
    except Exception:
        try:
            setattr(session, "_feedback_tap_fast_active", False)
        except Exception:
            pass
    try:
        setattr(session, "_feedback_tap_cursor", consumed)
    except Exception:
        pass
    if lost:
        log(f"yaolu feedback tap overrun lost={lost} cursor={cursor}->{consumed}")
    if not events:
        return None

    ordered: list[tuple[dict, str]] = []
    for event in events:
        raw = str(event.get("text") or "")
        text = _normalize_chat_line(_strip_ui_color_codes(raw))
        if text:
            ordered.append((event, text))
    if not ordered:
        return None
    log(
        "yaolu feedback tap events "
        + " | ".join(
            f"seq={event.get('seq')} ch={event.get('channel')} "
            f"caller=0x{int(event.get('caller_va') or 0):08X} text={text!r}"
            for event, text in ordered
        )
    )

    latest_answer: tuple[str, dict, str] | None = None
    hard: tuple[dict, str] | None = None
    soft: tuple[dict, str] | None = None
    for event, text in ordered:
        if "答案错误" in text or ("重新来过" in text and "答案" in text):
            latest_answer = ("fail", event, text)
        elif "答案正确" in text or "请尽快进入活动" in text:
            latest_answer = ("ok", event, text)
        if ("神罚" in text or "捕羽" in text) and "不能进入" in text:
            hard = (event, text)
        elif any(
            key in text
            for key in ("不稳定状态", "入口未打开", "不能进入副本")
        ):
            soft = (event, text)

    if hard is not None and (
        trust_chat_hard
        or (latest_answer is not None and latest_answer[0] == "ok")
    ):
        event, text = hard
        return CaptchaAnswerFeedback(
            kind="block",
            text=_clip_single_toast(text),
            method=f"chat_tap_hard:seq={event.get('seq')}",
            error=(
                CAPTCHA_OK_TEXT
                if latest_answer is not None and latest_answer[0] == "ok"
                else None
            ),
        )
    if latest_answer is not None:
        kind, event, text = latest_answer
        return CaptchaAnswerFeedback(
            kind=kind,
            text=CAPTCHA_OK_TEXT if kind == "ok" else CAPTCHA_FAIL_TEXT,
            msg_id=CAPTCHA_OK_MSG_ID if kind == "ok" else CAPTCHA_FAIL_MSG_ID,
            method=f"chat_tap_{kind}:seq={event.get('seq')}",
        )
    if hard is not None:
        event, text = hard
        return CaptchaAnswerFeedback(
            kind="block",
            text=_clip_single_toast(text),
            method=f"chat_tap_hard_only:seq={event.get('seq')}",
        )
    if soft is not None:
        event, text = soft
        return CaptchaAnswerFeedback(
            kind="block",
            text=_clip_single_toast(text),
            method=f"chat_tap_soft:seq={event.get('seq')}",
        )
    return None


def _probe_feedback_hist_delta(
    session,
    *,
    trust_chat_hard: bool = False,
    budget_s: float = 0.7,
    log: LogFn | None = None,
) -> CaptchaAnswerFeedback | None:
    """
    Decide ok/fail/block from the live rich-history append journal.

    Production snapshots expose ordered ``history_rows``. They are reconciled
    against the pre-confirm snapshot and consumed by monotonic cursor. The old
    count-delta path is retained only for legacy snapshots and unit fixtures.

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        from app.core.chat_history import (
            FEEDBACK_COUNT_KEYS,
            diff_feedback_count_new,
            get_chat_message_buffer,
            snapshot_feedback_fingerprint,
        )
    except Exception as e:
        log(f"hist delta import fail: {e}")
        return None
    before = getattr(session, "_feedback_hist_baseline", None)
    if not isinstance(before, dict):
        return None
    try:
        after = snapshot_feedback_fingerprint(
            session, budget_s=max(0.25, float(budget_s)), log=None
        )
    except Exception as e:
        log(f"hist delta snapshot fail: {e}")
        return None

    delta = diff_feedback_count_new(before, after)
    hash_changed = str(before.get("sig_hash") or "") != str(after.get("sig_hash") or "")
    message_cursor = getattr(session, "_feedback_message_cursor", None)
    journal_available = "history_rows" in after and message_cursor is not None

    if journal_available:
        message_buffer = get_chat_message_buffer(session)
        appended = message_buffer.ingest(after.get("history_rows") or [])
        journal_events = message_buffer.read_after(int(message_cursor))
        consumed_cursor = message_buffer.latest_cursor
        try:
            setattr(session, "_feedback_message_cursor", consumed_cursor)
        except Exception:
            pass
        ordered_texts: list[str] = []
        grown_inst: list[tuple[str, int]] = []
        for event in journal_events:
            text = str(event.get("text") or "").strip()
            canon = next(
                (key for key in FEEDBACK_COUNT_KEYS if key == text or key in text),
                None,
            )
            if canon is None:
                continue
            ordered_texts.append(canon)
            grown_inst.append((canon, int(event.get("addr") or 0) & 0xFFFFFFFF))
        new_texts = list(dict.fromkeys(ordered_texts))
        if not new_texts:
            try:
                setattr(session, "_feedback_hist_last_delta_empty", True)
                setattr(session, "_feedback_hist_last_fp", after)
            except Exception:
                pass
            log(
                f"yaolu feedback journal empty cursor={message_cursor}->{consumed_cursor} "
                f"appended={len(appended)} stats={message_buffer.stats()}"
            )
            return None
        try:
            setattr(session, "_feedback_hist_last_delta_empty", False)
            setattr(session, "_feedback_hist_last_fp", after)
        except Exception:
            pass
        log(
            f"yaolu feedback journal cursor={message_cursor}->{consumed_cursor} "
            f"new={ordered_texts} "
            f"appended={len(appended)} stats={message_buffer.stats()}"
        )
    else:
        ordered_texts = []

        def _inst_pages(fp: dict) -> set[tuple[str, int]]:
            out: set[tuple[str, int]] = set()
            for it in fp.get("instances") or ():
                try:
                    tt = str(it[0])
                    aa = int(it[1]) & 0xFFFFFFFF
                except Exception:
                    continue
                if tt and aa:
                    out.add((tt, aa & ~0xFFF))
            return out

        b_pages = _inst_pages(before)
        a_pages = _inst_pages(after)
        overlap = b_pages & a_pages if b_pages and a_pages else set()
        # Legacy snapshots do not expose ordered history rows. Keep the old
        # count-based behavior only for compatibility/tests.
        if (
            len(b_pages) >= 2
            and len(a_pages) >= 1
            and not overlap
            and not delta
        ):
            try:
                setattr(session, "_feedback_hist_baseline", dict(after))
                setattr(session, "_feedback_hist_baseline_ts", time.monotonic())
                setattr(session, "_feedback_hist_last_fp", after)
            except Exception:
                pass
            log(
                f"yaolu feedback hist relocate refresh "
                f"counts={after.get('counts')} hash={after.get('sig_hash')}"
            )
            return None

        new_texts = []
        grown_inst = []
        after_inst = list(after.get("instances") or ())
        for k, n in sorted(delta.items(), key=lambda kv: str(kv[0])):
            nn = int(n or 0)
            if nn <= 0:
                continue
            new_texts.append(str(k))
            ks = []
            for it in after_inst:
                try:
                    if str(it[0]) == str(k):
                        ks.append((str(it[0]), int(it[1]) & 0xFFFFFFFF))
                except Exception:
                    continue
            grown_inst.extend(ks[-nn:])

        if not new_texts and not hash_changed:
            try:
                setattr(session, "_feedback_hist_last_delta_empty", True)
                setattr(session, "_feedback_hist_last_fp", after)
            except Exception:
                pass
            return None

        try:
            setattr(session, "_feedback_hist_last_delta_empty", False)
            setattr(session, "_feedback_hist_last_fp", after)
        except Exception:
            pass
        log(
            f"yaolu feedback hist legacy-delta counts={delta or {}} "
            f"new_texts={new_texts} hash_changed={int(hash_changed)}"
        )

    ok_k = "答案正确，请尽快进入活动"
    fail_k = "答案错误，请大侠重新来过"
    hard_keys = (
        "队伍成员处于神罚状态，不能进入副本",
        "队伍成员处于捕羽状态，不能进入副本",
    )

    def _has(keys: tuple[str, ...] | list[str]) -> str | None:
        for k in keys:
            if k in new_texts:
                return k
            for t in new_texts:
                if k in t:
                    return k
        return None

    hard_text = _has(hard_keys)
    if hard_text is None:
        for t in new_texts:
            if ("神罚" in t or "捕羽" in t) and "答案" not in t:
                hard_text = t
                break

    got_ok = _has((ok_k, "答案正确", "请尽快进入活动")) is not None and any(
        "答案错误" not in t for t in new_texts if "答案正确" in t or ok_k in t or "请尽快进入活动" in t
    )
    # simpler:
    got_ok = any(
        (ok_k in t) or (t == "答案正确，请尽快进入活动") or ("答案正确" in t and "错误" not in t)
        for t in new_texts
    )
    got_fail = any(
        (fail_k in t) or ("答案错误" in t) or ("重新来过" in t and "答案" in t)
        for t in new_texts
    )

    # When both ok and fail grow in the same delta (stale heap + new toast),
    # the journal has authoritative order. Legacy snapshots fall back to addr.
    if got_ok and got_fail:
        if ordered_texts:
            last_answer = next(
                (
                    text
                    for text in reversed(ordered_texts)
                    if "答案正确" in text or "答案错误" in text
                ),
                fail_k,
            )
            got_ok = "答案正确" in last_answer and "答案错误" not in last_answer
            got_fail = not got_ok
            log(f"yaolu feedback journal conflict latest={last_answer!r}")
        else:
            def _max_addr(pred) -> int:
                best = -1
                for tx, ad in grown_inst:
                    try:
                        if pred(str(tx)):
                            best = max(best, int(ad) & 0xFFFFFFFF)
                    except Exception:
                        continue
                return best

            fail_addr = _max_addr(
                lambda t: ("答案错误" in t)
                or (fail_k in t)
                or ("重新来过" in t and "答案" in t)
            )
            ok_addr = _max_addr(
                lambda t: (ok_k in t)
                or ("答案正确" in t and "错误" not in t)
            )
            if fail_addr >= ok_addr:
                got_ok = False
            else:
                got_fail = False

    # Stage1 fail always beats residual/concurrent hard noise.
    # Only after proven 答案正确 may hard become stage2 block.
    if got_fail and not got_ok:
        return CaptchaAnswerFeedback(
            kind="fail",
            text=fail_k,
            msg_id=CAPTCHA_FAIL_MSG_ID,
            method="hist_journal_fail" if journal_available else "hist_delta_fail",
        )
    if hard_text and (got_ok or trust_chat_hard):
        return CaptchaAnswerFeedback(
            kind="block",
            text=hard_text if hard_text in hard_keys else (
                hard_text if "神罚" in hard_text or "捕羽" in hard_text else hard_keys[0]
            ),
            method="hist_journal_hard" if journal_available else "hist_delta_hard",
            error=ok_k if got_ok else None,
        )
    if got_ok:
        return CaptchaAnswerFeedback(
            kind="ok",
            text=ok_k,
            msg_id=CAPTCHA_OK_MSG_ID,
            method="hist_journal_ok" if journal_available else "hist_delta_ok",
        )
    if hard_text and not got_ok and not trust_chat_hard:
        return CaptchaAnswerFeedback(
            kind="block",
            text=hard_text if "神罚" in str(hard_text) or "捕羽" in str(hard_text) else hard_keys[0],
            method="hist_journal_hard_only" if journal_available else "hist_delta_hard_only",
        )

    # hash changed but no classified text — keep waiting
    return None


def _probe_feedback_ui_only(

    session,
    *,
    trust_chat_hard: bool = False,
    allow_heap: bool = True,
    log: LogFn | None = None,
    heap_budget_s: float = 0.75,
) -> CaptchaAnswerFeedback | None:
    """
    Fast feedback pass: budgeted toast/chat UI, optional writable-heap short keys.

    Does NOT call pattern_scan_all. Hard block wins when both 答案正确 and 神罚
    are visible. After stage1 答案正确, pass trust_chat_hard=True so a chat-only
    神罚 line on 其他 channel still hard-stops.

    Each probe logs collected 系统/其他-class text so live runs can verify reads.

    @author by ak
    """
    # Three small budgeted walks so concurrent 答案正确 + 神罚 (different
    # chat lines / dialogs) can both be seen without unbounded fan-out.
    s_hard, m_hard = _scan_feedback_ui_text(session, _SHENFA_KEYS, budget_s=0.12)
    s_ok, m_ok = _scan_feedback_ui_text(session, _CAPTCHA_OK_KEYS, budget_s=0.12)
    s_fail, m_fail = _scan_feedback_ui_text(session, _CAPTCHA_FAIL_KEYS, budget_s=0.08)

    decision: CaptchaAnswerFeedback | None = None
    # UI/toast path: hard(+ok/trust) > ok > fail.
    # Residual chat lines can keep old 答案错误 visible, so fail is last here.
    # Heap delta path uses _feedback_from_marker_hits (fail-first on deltas).
    if s_hard and any(m in s_hard for m in _SHENFA_HARD_MARKERS):
        from_toast = str(m_hard or "").startswith("dlg:")
        if from_toast or s_ok or trust_chat_hard:
            decision = CaptchaAnswerFeedback(
                kind="block",
                text=(
                    s_hard
                    if (
                        "副本" in s_hard
                        or any(m in s_hard for m in _SHENFA_HARD_MARKERS)
                    )
                    else SHENFA_BLOCK_TEXT
                ),
                method=m_hard
                + (
                    "+after_ok"
                    if s_ok
                    else ("+trust_stage1" if trust_chat_hard else "")
                ),
                error=s_ok if s_ok else None,
            )
    if decision is None and s_ok:
        decision = CaptchaAnswerFeedback(
            kind="ok",
            text=s_ok if "答案正确" in s_ok or "进入活动" in s_ok else CAPTCHA_OK_TEXT,
            msg_id=CAPTCHA_OK_MSG_ID,
            method=m_ok,
        )
    if decision is None and s_fail:
        decision = CaptchaAnswerFeedback(
            kind="fail",
            text=(
                s_fail
                if ("答案错误" in s_fail or "重新来过" in s_fail)
                else CAPTCHA_FAIL_TEXT
            ),
            msg_id=CAPTCHA_FAIL_MSG_ID,
            method=m_fail,
        )

    heap_hits: dict[str, bool] | None = None
    heap_samples: dict[str, str] = {}
    heap_scanned = False
    # When pre-confirm hist baseline exists, hist multi-instance delta is the
    # sole authority. Heap short-marker *counts* jitter across scans (different
    # region coverage) and have false-fired: residual 神罚+答案正确 growth was
    # treated as stage2 hard while the real toast was 答案错误 (2026-07-20 log).
    hist_base_present = isinstance(
        getattr(session, "_feedback_hist_baseline", None), dict
    ) and bool(
        (getattr(session, "_feedback_hist_baseline", None) or {}).get("counts")
        is not None
    )
    if (
        decision is not None
        and hist_base_present
        and not str(decision.method or "").startswith("dlg:")
    ):
        if log is not None:
            log(
                f"yaolu feedback UI residual discarded: kind={decision.kind} "
                f"method={decision.method} (ordered journal active)"
            )
        decision = None
    if decision is None and allow_heap and hist_base_present:
        if log is not None:
            log(
                "yaolu feedback heap skip: hist baseline present "
                "(heap short-count not authoritative)"
            )
        allow_heap = False
    if decision is None and allow_heap:
        # Writable-heap short markers: live 系统/其他 chat lines (high heap).
        # Only used when hist baseline missing (dev / legacy path).
        heap_scanned = True
        counts: dict[str, int] = {}
        try:
            raw_hits = _scan_writable_heap_markers(
                session,
                budget_s=max(0.4, float(heap_budget_s)),
                sample_out=heap_samples,
                count_out=counts,
            )
        except Exception as e:
            raw_hits = {}
            if log is not None:
                log(f"yaolu feedback heap scan err: {e}")
        # Prefer pre-confirm baseline (set at submit). If missing, first wait
        # tick becomes baseline and does NOT decide (avoids residual history).
        # Later ticks: only count *increments* vs baseline. Residual
        # __together__ without growth is diagnostic only.
        base = getattr(session, "_feedback_heap_baseline", None)
        base_src = str(getattr(session, "_feedback_heap_baseline_src", "") or "")
        if not isinstance(base, dict):
            try:
                setattr(session, "_feedback_heap_baseline", dict(counts))
                setattr(session, "_feedback_heap_baseline_src", "first_wait_tick")
            except Exception:
                pass
            base = dict(counts)
            if log is not None:
                log(f"yaolu feedback heap baseline(first_tick)={base}")
                if heap_samples.get("__together__"):
                    log(
                        "yaolu feedback heap: residual together on baseline tick "
                        "(ignored; wait for count delta)"
                    )
            heap_hits = {m: False for m in (_HEAP_FEEDBACK_MARKERS)}
            for m in counts:
                heap_hits[m] = False
            decision = None
        else:
            heap_hits = {}
            delta_parts: list[str] = []
            for m in _HEAP_FEEDBACK_MARKERS:
                now_n = int(counts.get(m, 0))
                before = int(base.get(m, 0))
                grew = now_n > before
                heap_hits[m] = grew
                # Always show compact before->now for audit.
                flag = "Y" if grew else "N"
                delta_parts.append(f"{m} {before}->{now_n}({flag})")
            if log is not None:
                st = getattr(session, "_feedback_heap_last_scan_stats", None)
                st_txt = f" scan={st}" if st else ""
                log(
                    "yaolu feedback heap delta "
                    f"src={base_src or 'pre_set'} | "
                    + ", ".join(delta_parts)
                    + st_txt
                )
            ok_d = bool(
                heap_hits.get(_OK_SHORT_TEXT) or heap_hits.get(_OK_SHORT_TAIL)
            )
            hard_d = bool(
                heap_hits.get(_SHENFA_SHORT_TEXT)
                or heap_hits.get(_BUYU_SHORT_TEXT)
            )
            fail_d = bool(heap_hits.get(_FAIL_SHORT_TEXT))
            if log is not None and heap_samples.get("__together__"):
                if ok_d and hard_d:
                    log("yaolu feedback heap: together with ok+hard deltas")
                else:
                    log(
                        "yaolu feedback heap: residual together (no dual delta; "
                        f"ok_d={int(ok_d)} hard_d={int(hard_d)} fail_d={int(fail_d)})"
                    )
            decision = _feedback_from_marker_hits(
                heap_hits or {},
                trust_chat_hard=trust_chat_hard,
                method_prefix="heap",
            )
            # Extra guard: if hist just reported empty delta this wait, heap
            # growth is almost certainly scan coverage noise.
            if decision is not None and bool(
                getattr(session, "_feedback_hist_last_delta_empty", False)
            ):
                if log is not None:
                    log(
                        f"yaolu feedback heap {decision.kind} discarded: "
                        f"hist delta empty this tick method={decision.method}"
                    )
                decision = None
        # Promote heap sample text into probe log fields.
        if heap_hits:
            if not s_ok and (
                heap_hits.get(_OK_SHORT_TEXT) or heap_hits.get(_OK_SHORT_TAIL)
            ):
                s_ok = _annotate_channel(
                    heap_samples.get(_OK_SHORT_TEXT)
                    or heap_samples.get(_OK_SHORT_TAIL)
                    or CAPTCHA_OK_TEXT
                )
                m_ok = "heap:ok"
            if not s_hard and (
                heap_hits.get(_SHENFA_SHORT_TEXT) or heap_hits.get(_BUYU_SHORT_TEXT)
            ):
                s_hard = _annotate_channel(
                    heap_samples.get(_SHENFA_SHORT_TEXT)
                    or heap_samples.get(_BUYU_SHORT_TEXT)
                    or SHENFA_BLOCK_TEXT
                )
                m_hard = "heap:hard"
            if not s_fail and heap_hits.get(_FAIL_SHORT_TEXT):
                s_fail = _annotate_channel(
                    heap_samples.get(_FAIL_SHORT_TEXT) or CAPTCHA_FAIL_TEXT
                )
                m_fail = "heap:fail"
            if heap_samples.get("__together__"):
                if log is not None:
                    # already included via snapshot; keep field for clarity
                    pass

    _log_feedback_probe_snapshot(
        log,
        s_hard=s_hard,
        m_hard=m_hard or "",
        s_ok=s_ok,
        m_ok=m_ok or "",
        s_fail=s_fail,
        m_fail=m_fail or "",
        heap_hits=heap_hits,
        decision=decision,
        trust_chat_hard=trust_chat_hard,
        heap_scanned=heap_scanned,
        heap_samples=heap_samples,
    )
    return decision


def _probe_feedback_u16_delta(
    session,
    *,
    answer_baseline: dict[str, int] | None,
    entry_block_baseline: dict[str, int] | None = None,
    log: LogFn | None = None,
) -> CaptchaAnswerFeedback | None:
    """
    Heavy process-wide UTF-16 delta vs submit baseline.

    @author by ak
    """
    log = log or (lambda _m: None)
    pm = getattr(session, "pm", None)
    if pm is None:
        return None

    shenfa_now, shenfa_before, shenfa_d = _hit_count_delta(
        pm, SHENFA_BLOCK_TEXT, answer_baseline
    )
    buyu_now, buyu_before, buyu_d = _hit_count_delta(
        pm, BUYU_BLOCK_TEXT, answer_baseline
    )
    buyu_s_now, buyu_s_before, buyu_s_d = _hit_count_delta(
        pm, _BUYU_SHORT_TEXT, answer_baseline
    )
    shenfa_s_now, shenfa_s_before, shenfa_s_d = _hit_count_delta(
        pm, _SHENFA_SHORT_TEXT, answer_baseline
    )
    ok_now, ok_before, ok_d = _hit_count_delta(pm, CAPTCHA_OK_TEXT, answer_baseline)
    ok_s_now, ok_s_before, ok_s_d = _hit_count_delta(
        pm, _OK_SHORT_TEXT, answer_baseline
    )
    ok_t_now, ok_t_before, ok_t_d = _hit_count_delta(
        pm, _OK_SHORT_TAIL, answer_baseline
    )
    fail_now, fail_before, fail_d = _hit_count_delta(
        pm, CAPTCHA_FAIL_TEXT, answer_baseline
    )
    fail_s_now, fail_s_before, fail_s_d = _hit_count_delta(
        pm, _FAIL_SHORT_TEXT, answer_baseline
    )

    hard_block = (
        shenfa_d > 0
        or buyu_d > 0
        or buyu_s_d > 0
        or shenfa_s_d > 0
    )
    ok_hit = ok_d > 0 or ok_s_d > 0 or ok_t_d > 0
    fail_hit = fail_d > 0 or fail_s_d > 0

    # Priority: fail delta > hard(+ok) > ok.
    if fail_hit:
        return CaptchaAnswerFeedback(
            kind="fail",
            text=CAPTCHA_FAIL_TEXT,
            msg_id=CAPTCHA_FAIL_MSG_ID,
            method=(
                f"u16_delta_fail full={fail_before}->{fail_now} "
                f"short={fail_s_before}->{fail_s_now}"
            ),
        )

    if hard_block:
        if shenfa_d > 0 or shenfa_s_d > 0:
            text = SHENFA_BLOCK_TEXT
        else:
            text = BUYU_BLOCK_TEXT
        return CaptchaAnswerFeedback(
            kind="block",
            text=text,
            method=(
                f"u16_delta_block shenfa={shenfa_d}/{shenfa_before}->{shenfa_now} "
                f"buyu={buyu_d}/{buyu_before}->{buyu_now} "
                f"buyu_s={buyu_s_d}/{buyu_s_before}->{buyu_s_now} "
                f"shenfa_s={shenfa_s_d}/{shenfa_s_before}->{shenfa_s_now}"
                + (f" ok_also={ok_before}->{ok_now}" if ok_hit else "")
            ),
            error=CAPTCHA_OK_TEXT if ok_hit else None,
        )

    if ok_hit:
        return CaptchaAnswerFeedback(
            kind="ok",
            text=CAPTCHA_OK_TEXT,
            msg_id=CAPTCHA_OK_MSG_ID,
            method=(
                f"u16_delta_ok full={ok_before}->{ok_now} "
                f"short={ok_s_before}->{ok_s_now} "
                f"tail={ok_t_before}->{ok_t_now}"
            ),
        )
    if fail_hit:
        return CaptchaAnswerFeedback(
            kind="fail",
            text=CAPTCHA_FAIL_TEXT,
            msg_id=CAPTCHA_FAIL_MSG_ID,
            method=(
                f"u16_delta_fail full={fail_before}->{fail_now} "
                f"short={fail_s_before}->{fail_s_now}"
            ),
        )

    if answer_baseline is None:
        if shenfa_now >= 1 or shenfa_s_now >= 1:
            return CaptchaAnswerFeedback(
                kind="block",
                text=SHENFA_BLOCK_TEXT,
                method=f"u16_abs_shenfa={shenfa_now}/{shenfa_s_now}",
            )
        if buyu_now >= 1 or buyu_s_now >= 1:
            return CaptchaAnswerFeedback(
                kind="block",
                text=BUYU_BLOCK_TEXT,
                method=f"u16_abs_buyu={buyu_now}/{buyu_s_now}",
            )

    entry_block = probe_entry_open_block_text(
        session, baseline=entry_block_baseline, log=log
    )
    if entry_block is not None:
        return entry_block
    return None


def probe_captcha_answer_text(
    session,
    *,
    log: LogFn | None = None,
    entry_block_baseline: dict[str, int] | None = None,
    answer_baseline: dict[str, int] | None = None,
    heavy: bool = True,
    trust_chat_hard: bool = False,
) -> CaptchaAnswerFeedback | None:
    """
    Best-effort read of captcha fail/ok/block system text after submit.

    Strategy:
      1) AddChatMessage tap after the pre-confirm cursor (authoritative)
      2) Rich-hist feedback delta (fallback when tap is unavailable)
      3) Toast + chat UI fallback
      4) UTF-16 heap marker delta (diagnostic/heavy only)

    Live order after correct answer under 神罚 (screenshot 2026-07-19):
      [系统] 答案正确，请尽快进入活动
      [其他] 队伍成员处于神罚状态，不能进入副本
    Both lines may land within ~1s; hard block must win over plain ok.

    trust_chat_hard: after stage1 答案正确, accept chat-only 神罚 without a
    concurrent 答案正确 line (chat history residual is acceptable then).
    """
    log = log or (lambda _m: None)
    try:
        tap_hit = _probe_feedback_tap_delta(
            session,
            trust_chat_hard=trust_chat_hard,
            log=log,
        )
    except Exception as e:
        log(f"chat tap probe err: {e}")
        tap_hit = None
    if tap_hit is not None:
        log(
            f"yaolu feedback hit: kind={tap_hit.kind} method={tap_hit.method} "
            f"text={_clip_feedback_text(tap_hit.text)!r} "
            f"error={_clip_feedback_text(tap_hit.error)!r}"
        )
        return tap_hit
    # An active, lossless tap is the exact source of truth.  Avoid repeatedly
    # walking rich-history/UI/heap every 220ms while simply waiting for its next
    # event.  The flag is cleared on read errors, inactive headers, or overrun,
    # which restores the legacy fallback below.
    if bool(getattr(session, "_feedback_tap_fast_active", False)):
        return None

    pm = getattr(session, "pm", None)
    if pm is None:
        return None

    # Heap is expensive; still scan densely in the first seconds after submit
    # (live 答案正确+神罚 often lands within ~1s). First-run miss was: all
    # markers 0 for 18s while chat already showed both lines.
    now = time.monotonic()
    last_heap = float(getattr(session, "_feedback_heap_scan_ts", 0.0) or 0.0)
    wait_start = float(getattr(session, "_feedback_wait_start_ts", 0.0) or 0.0)
    since_wait = (now - wait_start) if wait_start > 0 else 999.0
    if since_wait <= 5.0:
        heap_gap = 0.35
        heap_budget = 0.85
    elif since_wait <= 12.0:
        heap_gap = 0.7
        heap_budget = 0.7
    else:
        heap_gap = 1.0
        heap_budget = 0.55
    allow_heap = (now - last_heap) >= heap_gap

    # Fast path: hist rich-buffer sentence count delta (no general chat scan).
    last_hist = float(getattr(session, "_feedback_hist_scan_ts", 0.0) or 0.0)
    # Dense early polls — live 答案错误 often lands <1s after confirm.
    hist_gap = 0.15 if since_wait <= 8.0 else 0.40
    if (now - last_hist) >= hist_gap:
        try:
            hist_hit = _probe_feedback_hist_delta(
                session,
                trust_chat_hard=trust_chat_hard,
                budget_s=1.1 if since_wait <= 3.0 else (0.85 if since_wait <= 8.0 else 0.60),
                log=log,
            )
            setattr(session, "_feedback_hist_scan_ts", now)
        except Exception as e:
            log(f"hist delta probe err: {e}")
            hist_hit = None
        if hist_hit is not None:
            log(
                f"yaolu feedback hit: kind={hist_hit.kind} method={hist_hit.method} "
                f"text={_clip_feedback_text(hist_hit.text)!r} "
                f"error={_clip_feedback_text(hist_hit.error)!r}"
            )
            return hist_hit

    ui_hit = _probe_feedback_ui_only(
        session,
        trust_chat_hard=trust_chat_hard,
        allow_heap=allow_heap,
        log=log,
        heap_budget_s=min(heap_budget, 0.55),
    )
    if allow_heap:
        try:
            setattr(session, "_feedback_heap_scan_ts", now)
        except Exception:
            pass
    if ui_hit is not None:
        log(
            f"yaolu feedback hit: kind={ui_hit.kind} method={ui_hit.method} "
            f"text={_clip_feedback_text(ui_hit.text)!r} "
            f"error={_clip_feedback_text(ui_hit.error)!r}"
        )
        return ui_hit
    if not heavy:
        return None
    heavy_hit = _probe_feedback_u16_delta(
        session,
        answer_baseline=answer_baseline,
        entry_block_baseline=entry_block_baseline,
        log=log,
    )
    if heavy_hit is not None:
        log(
            f"yaolu feedback hit(heavy): kind={heavy_hit.kind} "
            f"method={heavy_hit.method} text={_clip_feedback_text(heavy_hit.text)!r} "
            f"error={_clip_feedback_text(heavy_hit.error)!r}"
        )
    else:
        log("yaolu feedback hit(heavy): none")
    return heavy_hit


def _stage2_after_answer_ok(
    session,
    stage1: CaptchaAnswerFeedback,
    *,
    deadline: float,
    hold_s: float,
    poll: float,
    stop_event=None,
    log: LogFn | None = None,
    status: LogFn | None = None,
    countdown: Callable[[], None] | None = None,
    scene_ok_fn: Callable[[], bool] | None = None,
    entry_block_baseline: dict[str, int] | None = None,
    answer_baseline: dict[str, int] | None = None,
) -> CaptchaAnswerFeedback:
    """
    Stage 2 after 「答案正确」: hard 神罚/捕羽, soft refuse, or clear to enter.

    deadline is the shared monotonic post-confirm deadline. UI/chat polling only;
    process-wide pattern scans are diagnostic-only and never run here.

    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    countdown = countdown or (lambda: None)
    status(f"一阶通过: {stage1.text or CAPTCHA_OK_TEXT}，等待二阶进入条件")
    log(
        f"captcha stage1 OK via={stage1.method} text={stage1.text!r}; "
        f"hold stage2 {hold_s:.1f}s"
    )
    hold_deadline = min(float(deadline), time.monotonic() + max(2.0, float(hold_s)))
    while time.monotonic() < hold_deadline:
        countdown()
        if stop_event is not None and stop_event.is_set():
            return CaptchaAnswerFeedback(kind="stopped", error="stopped")
        if scene_ok_fn is not None:
            try:
                if scene_ok_fn():
                    status("已进入妖楼（一阶+进图完成）")
                    log(
                        f"captcha stage1 ok + scene via={stage1.method} "
                        f"text={stage1.text!r}"
                    )
                    return CaptchaAnswerFeedback(
                        kind="ok",
                        text=stage1.text or CAPTCHA_OK_TEXT,
                        msg_id=stage1.msg_id or CAPTCHA_OK_MSG_ID,
                        method=f"stage1_ok+scene:{stage1.method}",
                    )
            except Exception:
                pass
        later = probe_captcha_answer_text(
            session,
            log=log,
            entry_block_baseline=entry_block_baseline,
            answer_baseline=answer_baseline,
            heavy=False,
            trust_chat_hard=True,
        )
        if later is not None and later.kind == "block":
            if is_shenfa_block_feedback(later):
                status(f"二阶硬拦截(神罚/捕羽): {later.text or later.kind}")
                log(
                    f"captcha stage2 HARD via={later.method} "
                    f"text={later.text!r}"
                )
                return CaptchaAnswerFeedback(
                    kind="block",
                    text=later.text or SHENFA_BLOCK_TEXT,
                    method=f"stage1_ok+stage2_hard:{later.method}",
                    error=stage1.text or CAPTCHA_OK_TEXT,
                )
            status(f"二阶软拦截: {later.text or later.kind}（答案已正确）")
            log(f"captcha stage2 SOFT via={later.method} text={later.text!r}")
            return CaptchaAnswerFeedback(
                kind="ok",
                text=stage1.text or CAPTCHA_OK_TEXT,
                msg_id=stage1.msg_id or CAPTCHA_OK_MSG_ID,
                method=f"stage1_ok+stage2_soft:{later.method}",
                error=later.text,
            )
        remaining = max(0.0, hold_deadline - time.monotonic())
        if remaining <= 0:
            break
        time.sleep(min(max(0.10, poll), 0.25, remaining))
    status(f"二阶通过（无后续拦截）: {stage1.text or CAPTCHA_OK_TEXT}，等待进图")
    log(
        f"captcha stage1_ok+stage2_clear via={stage1.method} "
        f"text={stage1.text!r}"
    )
    return CaptchaAnswerFeedback(
        kind="ok",
        text=stage1.text or CAPTCHA_OK_TEXT,
        msg_id=stage1.msg_id or CAPTCHA_OK_MSG_ID,
        method=f"stage1_ok+stage2_clear:{stage1.method}",
    )


def wait_captcha_answer_feedback(
    session,
    *,
    timeout_s: float = 12.0,
    poll_s: float = 0.25,
    stop_event=None,
    log: LogFn | None = None,
    status: LogFn | None = None,
    countdown: Callable[[], None] | None = None,
    deadline: float | None = None,
    scene_ok_fn: Callable[[], bool] | None = None,
    entry_block_baseline: dict[str, int] | None = None,
    answer_baseline: dict[str, int] | None = None,
    stage2_hold_s: float = 4.0,
    on_stage1: Callable[["CaptchaAnswerFeedback"], None] | None = None,
) -> CaptchaAnswerFeedback:
    """
    After captcha confirm: two-stage system feedback.

    Stage 1 (answer only):
      - 「答案错误，请大侠重新来过」 -> fail (re-answer)
      - 「答案正确，请尽快进入活动」 -> stage1 passed, enter stage 2

    Stage 2 (enter condition — only after stage1 ok):
      - 神罚/捕羽 -> hard block (stop permanently)
      - other refuse toast -> kind=ok + error=soft text (temporary cannot enter)
      - no second toast within hold -> kind=ok stage2_clear (can enter)

    scene_ok_fn True is terminal success (already in 妖楼).
    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    countdown = countdown or (lambda: None)
    # Normal automation is UI/chat-only. Global scans remain diagnostic-only.
    base = answer_baseline
    # Keep pre-confirm heap baseline when present (set at Btn_Ok). Only clear
    # stale baseline when missing/invalid so first wait tick can re-arm.
    try:
        cur_base = getattr(session, "_feedback_heap_baseline", None)
        if not isinstance(cur_base, dict):
            setattr(session, "_feedback_heap_baseline", None)
            setattr(session, "_feedback_heap_baseline_src", "")
        # Force immediate first heap tick after confirm.
        setattr(session, "_feedback_heap_scan_ts", 0.0)
        setattr(session, "_feedback_hist_scan_ts", 0.0)
        setattr(session, "_feedback_wait_start_ts", time.monotonic())
        setattr(session, "_feedback_hist_empty_n", 0)
        setattr(session, "_feedback_hist_empty_log_ts", 0.0)
        setattr(session, "_feedback_hist_last_delta_empty", False)
    except Exception:
        pass
    stage1_notified = False

    def _fire_stage1(fb: CaptchaAnswerFeedback) -> None:
        """Notify once when stage1 ok/fail/hard is first known. @author by ak"""
        nonlocal stage1_notified
        if stage1_notified or on_stage1 is None or fb is None:
            return
        if fb.kind not in ("ok", "fail", "block"):
            return
        stage1_notified = True
        try:
            on_stage1(fb)
        except Exception as e:
            log(f"captcha on_stage1 err: {e}")


    def _go_stage2(stage1_fb: CaptchaAnswerFeedback) -> CaptchaAnswerFeedback:
        _fire_stage1(stage1_fb)
        return _stage2_after_answer_ok(
            session,
            stage1_fb,
            deadline=end,
            hold_s=stage2_hold,
            poll=poll,
            stop_event=stop_event,
            log=log,
            status=status,
            countdown=countdown,
            scene_ok_fn=scene_ok_fn,
            entry_block_baseline=entry_block_baseline,
            answer_baseline=base,
        )

    end = (
        float(deadline)
        if deadline is not None
        else time.monotonic() + max(0.5, float(timeout_s))
    )
    poll = max(0.1, float(poll_s))
    stage2_hold = max(3.5, float(stage2_hold_s))
    saw_dialog_open = False
    dialog_closed_at: float | None = None

    while time.monotonic() < end:
        countdown()
        if stop_event is not None and stop_event.is_set():
            return CaptchaAnswerFeedback(kind="stopped", error="stopped")

        # Terminal success: already left Fuzhou into 妖楼.
        if scene_ok_fn is not None:
            try:
                if scene_ok_fn():
                    return CaptchaAnswerFeedback(
                        kind="ok",
                        text=CAPTCHA_OK_TEXT,
                        msg_id=CAPTCHA_OK_MSG_ID,
                        method="scene_entered",
                    )
            except Exception as e:
                log(f"captcha feedback scene_ok err: {e}")

        hit = probe_captcha_answer_text(
            session,
            log=log,
            entry_block_baseline=entry_block_baseline,
            answer_baseline=base,
            heavy=False,
        )
        if hit is not None:
            # ---- Stage 1: answer correctness ----
            if hit.kind == "fail":
                status(f"一阶失败: {hit.text or CAPTCHA_FAIL_TEXT}")
                log(f"captcha stage1 FAIL via={hit.method} text={hit.text!r}")
                fb_fail = CaptchaAnswerFeedback(
                    kind="fail",
                    text=hit.text or CAPTCHA_FAIL_TEXT,
                    msg_id=hit.msg_id or CAPTCHA_FAIL_MSG_ID,
                    method=f"stage1_fail:{hit.method}",
                    dialog_open=hit.dialog_open,
                    error=hit.error,
                )
                _fire_stage1(fb_fail)
                return fb_fail
            if hit.kind == "ok":
                # Must complete stage2 hold; never treat 答案正确 alone as final enter.
                return _go_stage2(hit)
            if hit.kind == "block":
                if is_shenfa_block_feedback(hit):
                    # If UI already saw 答案正确 on same pass, keep it in error.
                    status(f"二阶硬拦截(神罚/捕羽): {hit.text or SHENFA_BLOCK_TEXT}")
                    log(
                        f"captcha stage2 HARD via={hit.method} text={hit.text!r} "
                        f"prior_ok={hit.error!r}"
                    )
                    fb_block = CaptchaAnswerFeedback(
                        kind="block",
                        text=hit.text or SHENFA_BLOCK_TEXT,
                        method=f"stage2_hard:{hit.method}",
                        error=hit.error,
                    )
                    # If stage1 ok already fired, this is no-op (notified once).
                    _fire_stage1(fb_block)
                    return fb_block
                status(f"入口软拦截: {hit.text or hit.kind}")
                log(
                    f"captcha soft-block via={hit.method} text={hit.text!r}"
                )
                return CaptchaAnswerFeedback(
                    kind="block",
                    text=hit.text,
                    method=f"soft_block:{hit.method}",
                    error=hit.error,
                )
            status(f"系统消息: {hit.text or hit.kind}")
            log(
                f"captcha feedback {hit.kind} via={hit.method} "
                f"text={hit.text!r}"
            )
            return hit

        try:
            cap = is_captcha_dialog_open(session, log=lambda _m: None)
            open_now = bool(cap.shown)
        except Exception:
            open_now = False
        if open_now:
            saw_dialog_open = True
            dialog_closed_at = None
        elif saw_dialog_open and dialog_closed_at is None:
            dialog_closed_at = time.monotonic()
            status("验证码弹窗已关闭，等待一阶/二阶系统消息")
            log("captcha feedback: dialog closed")

        # Dialog closed + still not entered: give server ~1.5s then classify.
        if (
            dialog_closed_at is not None
            and (time.monotonic() - dialog_closed_at) >= 1.5
            and scene_ok_fn is not None
        ):
            try:
                if not scene_ok_fn():
                    hit2 = probe_captcha_answer_text(
                        session,
                        log=log,
                        entry_block_baseline=entry_block_baseline,
                        answer_baseline=base,
                        heavy=False,
                    )
                    if hit2 is not None:
                        if hit2.kind == "fail":
                            status(
                                f"一阶失败: {hit2.text or CAPTCHA_FAIL_TEXT}"
                            )
                            log(
                                f"captcha stage1 FAIL via={hit2.method} "
                                f"text={hit2.text!r}"
                            )
                            return CaptchaAnswerFeedback(
                                kind="fail",
                                text=hit2.text or CAPTCHA_FAIL_TEXT,
                                msg_id=hit2.msg_id or CAPTCHA_FAIL_MSG_ID,
                                method=f"stage1_fail:{hit2.method}",
                            )
                        if hit2.kind == "ok":
                            # Still require stage2 after late 答案正确.
                            return _go_stage2(hit2)
                        if hit2.kind == "block":
                            if is_shenfa_block_feedback(hit2):
                                return CaptchaAnswerFeedback(
                                    kind="block",
                                    text=hit2.text or SHENFA_BLOCK_TEXT,
                                    method=f"stage2_hard:{hit2.method}",
                                    error=hit2.error,
                                )
                            return CaptchaAnswerFeedback(
                                kind="block",
                                text=hit2.text,
                                method=f"soft_block:{hit2.method}",
                            )
                        return hit2
                    # No new fail/ok/block copy — do not invent 答案错误.
                    return CaptchaAnswerFeedback(
                        kind="timeout",
                        dialog_open=False,
                        error="dialog closed; no new system text",
                        method="dialog_closed_no_delta",
                    )
            except Exception:
                pass

        remaining = max(0.0, end - time.monotonic())
        if remaining <= 0:
            break
        time.sleep(min(poll, remaining))

    # One final bounded UI-only read at the shared deadline.
    try:
        last = probe_captcha_answer_text(
            session,
            log=log,
            entry_block_baseline=entry_block_baseline,
            answer_baseline=base,
            heavy=False,
        )
    except Exception as e:
        log(f"captcha final UI probe err: {e}")
        last = None
    if last is not None:
        if last.kind == "fail":
            status(f"一阶失败: {last.text or CAPTCHA_FAIL_TEXT}")
            fb_fail = CaptchaAnswerFeedback(
                kind="fail",
                text=last.text or CAPTCHA_FAIL_TEXT,
                msg_id=last.msg_id or CAPTCHA_FAIL_MSG_ID,
                method=f"stage1_fail_final:{last.method}",
            )
            _fire_stage1(fb_fail)
            return fb_fail
        if last.kind == "block":
            if is_shenfa_block_feedback(last):
                status(f"二阶硬拦截(神罚/捕羽): {last.text or SHENFA_BLOCK_TEXT}")
                return CaptchaAnswerFeedback(
                    kind="block",
                    text=last.text or SHENFA_BLOCK_TEXT,
                    method=f"stage2_hard_final:{last.method}",
                    error=last.error,
                )
            return CaptchaAnswerFeedback(
                kind="block",
                text=last.text,
                method=f"soft_block_final:{last.method}",
                error=last.error,
            )
        if last.kind == "ok":
            fb_ok = CaptchaAnswerFeedback(
                kind="ok",
                text=last.text or CAPTCHA_OK_TEXT,
                msg_id=last.msg_id or CAPTCHA_OK_MSG_ID,
                method=f"stage1_ok_deadline:{last.method}",
            )
            _fire_stage1(fb_ok)
            return fb_ok

    try:
        cap = is_captcha_dialog_open(session, log=lambda _m: None)
        still = bool(cap.shown)
    except Exception:
        still = None
    return CaptchaAnswerFeedback(
        kind="timeout",
        dialog_open=still,
        error="no captcha answer feedback",
        method="timeout",
    )
