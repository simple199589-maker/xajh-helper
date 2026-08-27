# -*- coding: utf-8 -*-
"""
NPC service / portal talk path — memory RE notes (updated 2026-07-23 deep dive)

Dialogs:
  Win_NPC / Win_NPCContent / Win_NPCTrans / Win_NPCTemplate
  Live portal panel rect often Win_NPCContent ~ (0,81,265x337)

List select event (CDlgNPC* event map @ AA49xx):
  cmd 0x80000011 -> thiscall AA3330(dlg, "back") @ 0x00AA3330  (ret 4)

Option type on dialog:
  dlg+0x168  read e8cb40 / write e8cbc0
  branches in AA3330:
    2/4 -> talk_proc select path (real option)
    3/6 -> AABF70 rebuild (NOT select; hang/crash risk)
    1   -> close-ish

talk_proc (AUI prop "ptr_talk_proc", dlg+0x16C via e8cc40):
  +0x84 : int count
  +0x88 : entry*
  entry stride 0x18:
    +0x00 opt_id   (matched to selection id)
    +0x04 aux
    +0x0C text-ish
    +0x10 sub_count
    +0x14 sub_entries*  (stride 0x88)
  sub entry 0x88:
    +0x00 type/id  (may be 0x80000006/07 service marker)
    +0x84 text/service content ptr (used by AA3160 / host send)

Selection id storage:
  *0x19561F4 = Txt_Content control (NOT a separate listman class)
  ebc070/ebc0f0 get/set selected id at control+0x12C
  *0x1956208 = Sub_List control
  *0x19561F0 = host service UI (Template2.xml / Win_NPCTemplate via AE7590)

AA3330 type=2/4 select path (authoritative):
  1) talk_proc = e8cc40(dlg)
  2) id = ebc070(*0x19561F4)
  3) match entry where entry+0 == id
  4) AA3160(entry text) -> AC43D0 string rebuild
  5) Txt_Content->vt+0x48 SetText(processed)
  6) ebc0f0(entry+4); AE9C10(host) clear
  7) if sub_count>0: fill host via AE9A50/AE7DF0/AE7B40
  8) AA2B40 UI refresh

Host service table (*0x19561F0):
  +0x2A0 table*   +0x2AC count
  row stride 0xC0
  field0 dword @ row+0x14 ; ptr @ row+0x60
  UI rows: Img_Template%d / Txt_Template%d (AE7700)

Host row click (AA55xx):
  AE7C00 get field0; magic 0x40ABCDEF/0x40FEDCBA special
  else walk 0x80000006/07 markers -> 66D280 build -> 846EF0 send
  NO exported SelectService/EnterDungeon(tid,layer)

Legal chain:
  Hello -> talk_proc opt_id -> bridge AA3330(type2/4)
  -> branch expands host -> dest leaf talk_proc OR host activate
  -> success ONLY if scene_id changes

L1 with unfinished tasks (live 2026-07-24):
  talk_proc@dlg+0x16c stays 0; options are host rows (*0x19561F0).
  Labels on Txt_Template{i} @ ctrl+0xB8.
  Win_NPC.opt_type=1; click path = cdecl AA5590(index,0) (bridge NPC_HOST_SELECT).

Do NOT: CRT AA3330 default; type=6; geo primary; HostMove fake enter.
"""
from __future__ import annotations

import re
import struct
import time
from dataclasses import dataclass, field, asdict
from typing import Callable

from app.core.game_attach import GameAttachSession

LogFn = Callable[[str], None]


def _pid_blocked(session) -> tuple[bool, str]:
    """True when SafeDispatch/remote gate blocks this game pid. @author by ak"""
    try:
        from app.core.safe_dispatch import session_blocked

        return session_blocked(session)
    except Exception:
        return False, ""


def _note_remote_hard(session, exc: BaseException | str) -> None:
    """Push CRT/attach collapse into SafeDispatch gate. @author by ak"""
    try:
        pid = int(getattr(session, "pid", 0) or 0)
        if pid <= 0:
            return
        from app.core.safe_dispatch import get_dispatch

        if isinstance(exc, BaseException):
            get_dispatch().note_exception(pid, exc)
        else:
            get_dispatch().note_exception(pid, OSError(str(exc)))
    except Exception:
        pass


NOTE_BASE = 0x400000
VA_NPC_TALK_SELECT = 0x00AA3330
VA_NPC_CONTENT_OPEN = 0x00AA3800
VA_NPC_CONTENT_REBUILD = 0x00AABF70
VA_NPC_UI_REFRESH = 0x00AA2B40
VA_GLOBAL_HOSTISH = 0x019561F0  # host service UI (Win_NPCTemplate)
VA_GLOBAL_LISTMAN = 0x019561F4  # Txt_Content control
VA_GLOBAL_SUB_LIST = 0x01956208  # Sub_List control
VA_STR_BACK = 0x012993D4
VA_HOST_GET_FIELD = 0x00AE7C00
VA_HOST_GET_ROW = 0x00AE7A50
VA_HOST_GET_COUNT = 0x00AE76F0
VA_HOST_CLEAR = 0x00AE9C10
VA_MSG_BUILD = 0x0066D280
VA_MSG_SEND = 0x00846EF0
OFF_HOST_TABLE = 0x2A0
OFF_HOST_COUNT = 0x2AC
HOST_ROW_STRIDE = 0xC0
HOST_FIELD0_DW = 0x14
HOST_FIELD0_PTR = 0x60
SUB_SVC_MARKERS = (0x80000006, 0x80000007)

OFF_DLG_OPT_TYPE = 0x168
OFF_DLG_TALK_PROC = 0x16C  # e8cc40 returns ptr_talk_proc
OFF_TP_COUNT = 0x84
OFF_TP_ARRAY = 0x88
TP_ENTRY_STRIDE = 0x18
TP_SUB_STRIDE = 0x88

PORTAL_DLG_ORDER: tuple[str, ...] = (
    "Win_NPCContent",
    "Win_NPC",
    "Win_NPCTemplate",
    "Win_NPCTrans",
    "Win_NPCTalk",
)

PORTAL_KEYS: tuple[str, ...] = (
    "地宫传送",
    "初级地宫传送",
    "中级地宫传送",
    "高级地宫传送",
    "上层",
    "深处",
    "下层",
    "地宫",
    "西王母",
    "传送",
    "发放任务",
)


def _clean_portal_label(s: str) -> str:
    """Strip leading garbage from host/UI labels (live: garbled prefix + 初级地宫传送)."""
    s = str(s or "").strip()
    if not s:
        return ""
    start = 0
    for i, ch in enumerate(s):
        if "一" <= ch <= "鿿" or ch.isalnum() or ch in "<>《》":
            start = i
            break
    s = s[start:].strip()
    for key in (
        "西王母宫深处",
        "西王母宫上层",
        "中级地宫传送",
        "初级地宫传送",
        "高级地宫传送",
        "升级地宫传送",
        "练级地宫传送",
        "地宫传送",
        "地宫深处",
        "地宫上层",
        "上层",
        "深处",
        "下层",
    ):
        j = s.find(key)
        if j == 0:
            break
        if 0 < j <= 4:
            s = s[j:]
            break
    return s.strip()


def _log(log: LogFn | None, msg: str) -> None:
    if log:
        try:
            log(msg)
        except Exception:
            pass


def _va(session: GameAttachSession, note_va: int) -> int:
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        return int(note_va)
    return int(base) + (int(note_va) - NOTE_BASE)


def _rpm(session: GameAttachSession, addr: int, n: int) -> bytes:
    pm = getattr(session, "pm", None)
    if not pm or not addr:
        return b""
    try:
        return pm.read_bytes(int(addr) & 0xFFFFFFFF, int(n))
    except Exception:
        return b""


def _wpm_u32(session: GameAttachSession, addr: int, val: int) -> bool:
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid or not addr:
        return False
    raw = struct.pack("<I", int(val) & 0xFFFFFFFF)
    try:
        from app.core.remote_runtime import remote_write_bytes

        return remote_write_bytes(pid, int(addr) & 0xFFFFFFFF, raw) == 4
    except Exception:
        return False


def _u32(b: bytes, off: int) -> int:
    if off < 0 or off + 4 > len(b):
        return 0
    return struct.unpack_from("<I", b, off)[0]


def _i32(b: bytes, off: int) -> int:
    if off < 0 or off + 4 > len(b):
        return 0
    return struct.unpack_from("<i", b, off)[0]


def _clean_label(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\^[0-9a-fA-F]{0,6}", "", s)
    s = "".join(ch for ch in s if ch == " " or (ch.isprintable() and ord(ch) < 0x10000))
    return s.strip()[:64]


def _decode_u16(b: bytes) -> str:
    if not b:
        return ""
    end = 0
    while end + 1 < len(b):
        if b[end] == 0 and b[end + 1] == 0:
            break
        end += 2
    chunk = b[:end]
    if len(chunk) < 2:
        return ""
    if len(chunk) & 1:
        chunk = chunk[:-1]
    try:
        return _clean_label(chunk.decode("utf-16le", "ignore"))
    except Exception:
        return ""


def _cjk_or_portal(s: str) -> bool:
    """Accept only portal/service-like labels, not random heap CJK."""
    if not s:
        return False
    s = _clean_label(s)
    if not s or len(s) > 24:
        return False
    bad = (".gfx", ".xml", ".tga", ".dds", "BM", "##")
    if any(b in s for b in bad):
        return False
    keys = PORTAL_KEYS
    if not any(k in s for k in keys):
        return False
    cjk = sum(1 for c in s if "一" <= c <= "鿿")
    return 2 <= cjk <= 16


def _read_str(session: GameAttachSession, addr: int, n: int = 96) -> str:
    if not addr or not (0x10000 < (int(addr) & 0xFFFFFFFF) < 0x7FFE0000):
        return ""
    b = _rpm(session, int(addr) & 0xFFFFFFFF, n)
    if not b:
        return ""
    t = _decode_u16(b)
    if _cjk_or_portal(t):
        return t
    p = _u32(b, 0)
    if 0x10000 < p < 0x7FFE0000:
        t2 = _decode_u16(_rpm(session, p, n))
        if _cjk_or_portal(t2):
            return t2
    try:
        t3 = _clean_label(b.split(bytes([0]))[0].decode("gbk", "ignore"))
        if _cjk_or_portal(t3):
            return t3
    except Exception:
        pass
    return ""


def _scan_portal_keywords(session: GameAttachSession, addr: int, size: int = 0x140) -> list[str]:
    b = _rpm(session, int(addr) & 0xFFFFFFFF, size)
    if not b:
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for key in PORTAL_KEYS:
        try:
            pat = key.encode("utf-16le")
        except Exception:
            continue
        start_i = 0
        while start_i < len(b) - len(pat):
            idx = b.find(pat, start_i)
            if idx < 0:
                break
            if idx & 1:
                start_i = idx + 1
                continue
            lo = max(0, idx - 8)
            if lo & 1:
                lo += 1
            hi = min(len(b), idx + len(pat) + 24)
            if hi & 1:
                hi -= 1
            s = _decode_u16(b[lo:hi]) or key
            s = _clean_label(s)
            if s and s not in seen and _cjk_or_portal(s):
                seen.add(s)
                hits.append(s)
            start_i = idx + len(pat)
    for off in range(0, min(len(b) - 4, 0x120), 4):
        p = _u32(b, off)
        if not (0x10000 < p < 0x7FFE0000):
            continue
        t = _read_str(session, p, 80)
        if t and t not in seen and _cjk_or_portal(t):
            seen.add(t)
            hits.append(t)
        b2 = _rpm(session, p, 16)
        if len(b2) >= 4:
            p2 = _u32(b2, 0)
            if 0x10000 < p2 < 0x7FFE0000:
                t2 = _read_str(session, p2, 80)
                if t2 and t2 not in seen and _cjk_or_portal(t2):
                    seen.add(t2)
                    hits.append(t2)
    return hits


@dataclass
class TalkSubOption:
    index: int
    raw0: int
    texts: list[str] = field(default_factory=list)
    is_service_marker: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def label(self) -> str:
        for t in self.texts:
            if _cjk_or_portal(t):
                return t
        return self.texts[0] if self.texts else ""


@dataclass
class TalkOption:
    index: int
    opt_id: int
    texts: list[str] = field(default_factory=list)
    fields: list[int] = field(default_factory=list)
    sub_count: int = 0
    sub_ptr: int = 0
    sub_texts: list[str] = field(default_factory=list)
    subs: list[TalkSubOption] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def label(self) -> str:
        for t in self.texts:
            if _cjk_or_portal(t):
                return t
        for t in self.sub_texts:
            if _cjk_or_portal(t):
                return t
        return self.texts[0] if self.texts else ""


@dataclass
class TalkProcSnapshot:
    ok: bool
    talk_proc: int = 0
    count: int = 0
    array: int = 0
    options: list[TalkOption] = field(default_factory=list)
    source: str = ""
    error: str | None = None
    dlg_name: str = ""
    dlg_ptr: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_talk_proc(session: GameAttachSession, obj: int) -> TalkProcSnapshot | None:
    obj = int(obj or 0) & 0xFFFFFFFF
    if not obj:
        return None
    blob = _rpm(session, obj, 0xA0)
    if len(blob) < 0x8C:
        return None
    cnt = _i32(blob, OFF_TP_COUNT)
    arr = _u32(blob, OFF_TP_ARRAY)
    if not (1 <= cnt <= 32) or not (0x10000 < arr < 0x7FFE0000):
        return None
    ab = _rpm(session, arr, cnt * TP_ENTRY_STRIDE)
    if len(ab) < cnt * TP_ENTRY_STRIDE:
        return None
    options: list[TalkOption] = []
    useful = 0
    for i in range(cnt):
        eoff = i * TP_ENTRY_STRIDE
        fields = [_u32(ab, eoff + j) for j in range(0, TP_ENTRY_STRIDE, 4)]
        texts: list[str] = []
        for f in fields:
            if 0x10000 < f < 0x7FFE0000:
                t = _read_str(session, f)
                if t and t not in texts:
                    texts.append(t)
                    if _cjk_or_portal(t):
                        useful += 1
        for t in _scan_portal_keywords(session, arr + eoff, TP_ENTRY_STRIDE + 8):
            if t not in texts:
                texts.append(t)
                useful += 1
        for f in fields:
            if 0x10000 < f < 0x7FFE0000:
                for t in _scan_portal_keywords(session, f, 0x100):
                    if t not in texts:
                        texts.append(t)
                        useful += 1
        sub_cnt = fields[4] if len(fields) > 4 else 0
        sub_ptr = fields[5] if len(fields) > 5 else 0
        sub_texts: list[str] = []
        subs: list[TalkSubOption] = []
        if 1 <= int(sub_cnt) <= 32 and 0x10000 < int(sub_ptr) < 0x7FFE0000:
            for t in _scan_portal_keywords(session, int(sub_ptr), int(sub_cnt) * min(TP_SUB_STRIDE, 0x40)):
                if t not in sub_texts:
                    sub_texts.append(t)
                    useful += 1
            sb = _rpm(session, int(sub_ptr), int(sub_cnt) * TP_SUB_STRIDE)
            for si in range(int(sub_cnt)):
                soff = si * TP_SUB_STRIDE
                chunk = sb[soff : soff + TP_SUB_STRIDE]
                raw0 = _u32(chunk, 0) if len(chunk) >= 4 else 0
                st: list[str] = []
                if len(chunk) >= 0x88:
                    p84 = _u32(chunk, 0x84)
                    if 0x10000 < p84 < 0x7FFE0000:
                        t = _read_str(session, p84)
                        if t and t not in st:
                            st.append(t)
                        for t2 in _scan_portal_keywords(session, p84, 0xC0):
                            if t2 not in st:
                                st.append(t2)
                for off in range(0, len(chunk) - 4, 4):
                    p = _u32(chunk, off)
                    if 0x10000 < p < 0x7FFE0000:
                        t = _read_str(session, p)
                        if t and t not in st and _cjk_or_portal(t):
                            st.append(t)
                            useful += 1
                for t in st:
                    if t not in sub_texts and _cjk_or_portal(t):
                        sub_texts.append(t)
                subs.append(
                    TalkSubOption(
                        index=si,
                        raw0=int(raw0) & 0xFFFFFFFF,
                        texts=st,
                        is_service_marker=(int(raw0) & 0xFFFFFFFF) in SUB_SVC_MARKERS,
                    )
                )
        options.append(
            TalkOption(
                index=i,
                opt_id=int(fields[0]) if fields else 0,
                texts=texts,
                fields=fields,
                sub_count=int(sub_cnt or 0),
                sub_ptr=int(sub_ptr or 0),
                sub_texts=sub_texts,
                subs=subs,
            )
        )
    clean_opts = [o for o in options if o.label and _cjk_or_portal(o.label)]
    if not clean_opts:
        return None
    if obj in (0x3F800000, 0x40000000, 0x3F000000):
        return None
    poison = 0
    for o in options:
        oid = int(o.opt_id) & 0xFFFFFFFF
        if oid in (0, 0xCCCCCCCC, 0xCDCDCDCD, 0xFEEEFEEE, 0xABABABAB, 0xFFFFFFFF):
            poison += 1
        # 0x80000006/07 are service markers (valid on subs); only count pure garbage.
        if oid >= 0xF0000000:
            poison += 1
    if poison >= max(1, (cnt + 1) // 2) and not any(o.label for o in options):
        return None
    if poison >= cnt and cnt > 0:
        return None
    return TalkProcSnapshot(
        ok=True,
        talk_proc=obj,
        count=cnt,
        array=arr,
        options=options,
        source="talk_proc",
    )


def _scan_roots_for_talk_proc(
    session: GameAttachSession, roots: list[int]
) -> TalkProcSnapshot | None:
    seen: set[int] = set()
    best: TalkProcSnapshot | None = None
    best_score = -1
    for root in roots:
        r = int(root or 0) & 0xFFFFFFFF
        if not r or r in seen:
            continue
        seen.add(r)
        direct = _parse_talk_proc(session, r)
        if direct and direct.ok:
            score = sum(1 for o in direct.options if o.label)
            if score > best_score:
                best, best_score = direct, score
        blob = _rpm(session, r, 0x420)
        for off in range(0, max(0, len(blob) - 4), 4):
            p = _u32(blob, off)
            if not (0x10000 < p < 0x7FFE0000) or p in seen:
                continue
            b2 = _rpm(session, p, 0x90)
            if len(b2) < 0x8C:
                continue
            cnt = _i32(b2, OFF_TP_COUNT)
            if not (1 <= cnt <= 24):
                continue
            arr = _u32(b2, OFF_TP_ARRAY)
            if not (0x10000 < arr < 0x7FFE0000):
                continue
            seen.add(p)
            snap = _parse_talk_proc(session, p)
            if not snap or not snap.ok:
                continue
            score = sum(1 for o in snap.options if o.label)
            blob_txt = " ".join(o.label for o in snap.options)
            if any(k in blob_txt for k in PORTAL_KEYS):
                score += 10
            if score > best_score:
                snap.source = f"scan_root+0x{off:X}"
                best, best_score = snap, score
    return best


def get_npc_dialogs(
    session: GameAttachSession, *, log: LogFn | None = None
) -> list[tuple[str, int, bool]]:
    out: list[tuple[str, int, bool]] = []
    try:
        from app.core.plg_ui import get_game_ui_dlg, is_dlg_show
    except Exception as e:
        _log(log, f"npc_service_mem dlg import: {e}")
        return out
    for name in PORTAL_DLG_ORDER:
        try:
            dlg = int(get_game_ui_dlg(session, name, log=lambda _m: None) or 0) & 0xFFFFFFFF
        except Exception:
            dlg = 0
        if not dlg:
            continue
        try:
            shown = bool(is_dlg_show(session, dlg, log=lambda _m: None))
        except Exception:
            shown = False
        out.append((name, dlg, shown))
    return out


def dump_talk_proc(
    session: GameAttachSession, *, log: LogFn | None = None
) -> TalkProcSnapshot:
    roots: list[int] = []
    dlg_name = ""
    dlg_ptr = 0
    for name, dlg, shown in get_npc_dialogs(session, log=log):
        if shown:
            roots.insert(0, dlg)
        else:
            roots.append(dlg)
        if shown or not dlg_ptr:
            dlg_name, dlg_ptr = name, dlg
        try:
            from app.core.aui_click import get_aui_dlg_item_ptr

            for cn in ("Sub_List", "Lst_Main", "Lst_Content", "Sub_NPCContent", "Txt_Content"):
                try:
                    c = int(get_aui_dlg_item_ptr(session, dlg, cn, log=lambda _m: None) or 0)
                except Exception:
                    c = 0
                if c:
                    roots.append(c & 0xFFFFFFFF)
        except Exception:
            pass
    snap = None
    # Prefer AUI property storage dlg+0x16c (RE: e8cc40 returns ptr_talk_proc).
    if dlg_ptr:
        raw_tp = _rpm(session, int(dlg_ptr) + OFF_DLG_TALK_PROC, 4)
        tp = _u32(raw_tp, 0) if raw_tp else 0
        if tp:
            direct = _parse_talk_proc(session, tp)
            if direct and direct.ok:
                snap = direct
                snap.source = "dlg+0x16c"
    if not snap:
        snap = _scan_roots_for_talk_proc(session, roots)
    if not snap:
        return TalkProcSnapshot(
            ok=False, error="talk_proc not found", dlg_name=dlg_name, dlg_ptr=dlg_ptr
        )
    snap.dlg_name = dlg_name
    snap.dlg_ptr = dlg_ptr
    labels = [o.label for o in snap.options][:8]
    _log(
        log,
        f"npc_service_mem talk_proc=0x{snap.talk_proc:X} cnt={snap.count} "
        f"labels={labels} src={snap.source}",
    )
    return snap


def classify_panel_stage(snap: TalkProcSnapshot) -> str:
    labels = [o.label for o in (snap.options if snap else []) if o.label]
    blob = " ".join(labels)
    has_upper = any(("上层" in x) for x in labels)
    has_deep = any(("深处" in x) or ("下层" in x) for x in labels)
    has_entry = any(
        ("地宫传送" in x) or ("传送" in x and "地宫" in x)
        for x in labels
    )
    if has_upper and has_deep:
        return "layer2"
    if has_entry and not (has_upper and has_deep):
        return "layer1"
    if has_upper or has_deep:
        return "layer2_partial"
    if "地宫" in blob:
        return "layer1_maybe"
    return "unknown"


def match_option(
    snap: TalkProcSnapshot,
    needles: list[str] | tuple[str, ...],
) -> TalkOption | None:
    if not snap or not snap.options:
        return None
    ns = [str(n) for n in needles if n]
    if not ns:
        return None
    for o in snap.options:
        lab = o.label or ""
        for n in ns:
            if lab == n:
                return o
    for o in snap.options:
        texts = [o.label or ""] + list(o.texts or []) + list(o.sub_texts or [])
        for t in texts:
            for n in ns:
                if n and n in t:
                    return o
    return None


def _read_list_selected_id(session: GameAttachSession) -> tuple[int, int]:
    g = _va(session, VA_GLOBAL_LISTMAN)
    b = _rpm(session, g, 4)
    if len(b) < 4:
        return 0, -1
    obj = _u32(b, 0)
    if not obj:
        return 0, -1
    blob = _rpm(session, obj, 0x200)
    if len(blob) < 0x130:
        return obj, -1
    sid = _i32(blob, 0x12C)
    return obj, int(sid)


def _write_list_selected_id(session: GameAttachSession, opt_id: int) -> bool:
    obj, _ = _read_list_selected_id(session)
    if not obj:
        return False
    return _wpm_u32(session, obj + 0x12C, int(opt_id) & 0xFFFFFFFF)


def invoke_talk_select(
    session: GameAttachSession,
    *,
    dlg_ptr: int = 0,
    opt_type: int | None = None,
    opt_id: int | None = None,
    log: LogFn | None = None,
    prefer_bridge: bool = True,
    hwnd: int = 0,
) -> dict:
    """
    Select talk option by id via legal AA3330 path.

    Prefer bridge UI-thread call (same as game list event 0x80000011).
    CreateRemoteThread is last-resort only — hangs/crashes on some mouths.
    Do NOT write type=6 (rebuild); real click keeps type 2/4 already set by Hello.
    """
    out: dict = {"ok": False, "va": 0, "dlg": 0, "via": ""}
    if not dlg_ptr:
        for name, dlg, shown in get_npc_dialogs(session, log=log):
            if name in ("Win_NPCContent", "Win_NPC") and dlg:
                dlg_ptr = dlg
                if shown:
                    break
    dlg_ptr = int(dlg_ptr or 0) & 0xFFFFFFFF
    if not dlg_ptr:
        out["error"] = "no_npc_dlg"
        return out
    out["dlg"] = dlg_ptr
    if opt_id is None:
        out["error"] = "no_opt_id"
        return out
    out["opt_id"] = int(opt_id) & 0xFFFFFFFF
    type_arg = 0
    if opt_type is not None and int(opt_type) in (1, 2, 3, 4):
        type_arg = int(opt_type)
        out["wrote_type"] = type_arg

    if prefer_bridge:
        try:
            from app.core.xajh_bridge import ensure_bridge

            bridge = ensure_bridge(
                int(session.pid),
                log=log,
                inject_if_needed=False,
                hwnd=int(hwnd or 0) or None,
            )
            if bridge is not None:
                try:
                    br = bridge.npc_talk_select(
                        int(opt_id),
                        dlg_ptr=int(dlg_ptr),
                        opt_type=int(type_arg),
                        hwnd=int(hwnd or 0) or None,
                        timeout_ms=4000,
                    )
                    out["via"] = "bridge"
                    out["ok"] = bool(br.ok)
                    out["ret"] = br.ret
                    out["note"] = br.note or br.error or ""
                    out["error"] = None if br.ok else (br.error or "bridge talk select failed")
                    _log(
                        log,
                        f"npc_service_mem talk_select bridge ok={br.ok} "
                        f"dlg=0x{dlg_ptr:X} id={opt_id} type={type_arg} "
                        f"note={out['note']!r}",
                    )
                    return out
                finally:
                    try:
                        bridge.close()
                    except Exception:
                        pass
            else:
                out["bridge"] = "not_ready"
        except Exception as e:
            out["bridge_err"] = str(e)
            _log(log, f"npc_service_mem talk_select bridge err: {e}")

    out["via"] = "remote_crt"
    blocked, brsn = _pid_blocked(session)
    if blocked:
        out["error"] = brsn or "remote_blocked"
        out["ok"] = False
        _log(log, f"npc_service_mem AA3330 blocked: {brsn}")
        return out
    if type_arg:
        _wpm_u32(session, dlg_ptr + OFF_DLG_OPT_TYPE, int(type_arg))
    out["wrote_sel"] = bool(_write_list_selected_id(session, int(opt_id)))
    va = _va(session, VA_NPC_TALK_SELECT)
    out["va"] = va
    try:
        from app.core.remote_runtime import remote_call_thiscall_x86

        str_back = _va(session, VA_STR_BACK)
        remote_call_thiscall_x86(
            int(session.pid),
            int(va),
            int(dlg_ptr),
            [int(str_back) & 0xFFFFFFFF],
            caller_cleanup=False,
        )
        out["ok"] = True
        out["note"] = "AA3330 remote CRT"
        _log(log, f"npc_service_mem AA3330 remote dlg=0x{dlg_ptr:X} id={opt_id}")
    except Exception as e:
        out["error"] = str(e)
        _note_remote_hard(session, e)
        _log(log, f"npc_service_mem AA3330 remote err: {e}")
    return out


def select_option_by_needles(
    session: GameAttachSession,
    needles: list[str] | tuple[str, ...],
    *,
    opt_type: int | None = 2,
    log: LogFn | None = None,
    settle_s: float = 0.35,
) -> dict:
    snap = dump_talk_proc(session, log=log)
    out: dict = {
        "ok": False,
        "stage": classify_panel_stage(snap) if snap.ok else "none",
        "labels": [o.label for o in snap.options] if snap.ok else [],
    }
    if not snap.ok:
        out["error"] = snap.error or "no_talk_proc"
        return out
    opt = match_option(snap, needles)
    if not opt:
        out["error"] = f"option not found for {list(needles)[:4]}"
        return out
    out["matched"] = opt.label
    out["opt_id"] = opt.opt_id
    out["index"] = opt.index
    types = [opt_type] if opt_type is not None else [2, 4, 3]
    types = [int(t) for t in types if t is not None]
    last: dict = {}
    for t in types:
        last = invoke_talk_select(
            session,
            dlg_ptr=snap.dlg_ptr,
            opt_type=t,
            opt_id=opt.opt_id,
            log=log,
        )
        time.sleep(max(0.05, float(settle_s)))
        snap2 = dump_talk_proc(session, log=log)
        stage2 = classify_panel_stage(snap2) if snap2.ok else "none"
        out["after_stage"] = stage2
        out["after_labels"] = [o.label for o in snap2.options] if snap2.ok else []
        out["invoke"] = last
        if last.get("ok") and (
            stage2 != out["stage"]
            or any(n in " ".join(out["after_labels"]) for n in needles)
            or stage2 in ("layer2", "layer2_partial")
        ):
            out["ok"] = True
            out["used_type"] = t
            return out
    out["ok"] = bool(last.get("ok"))
    if not out["ok"]:
        out["error"] = last.get("error") or "invoke_failed"
    return out



def collect_portal_menu_texts(session: GameAttachSession, *, log: LogFn | None = None) -> list[str]:
    """Read portal option labels from open Win_NPC* via existing text scanner."""
    out: list[str] = []
    seen: set[str] = set()
    try:
        from app.core.task_api import _scan_dlg_option_texts, _portal_option_lines
    except Exception as e:
        _log(log, f"npc_service_mem text import: {e}")
        return out
    raw_all: list[str] = []
    for name, dlg, shown in get_npc_dialogs(session, log=log):
        if not shown and name not in ("Win_NPCContent", "Win_NPC", "Win_NPCTemplate"):
            continue
        try:
            texts = _scan_dlg_option_texts(session, int(dlg), log=lambda _m: None) or []
        except Exception:
            texts = []
        raw_all.extend(str(x or "") for x in texts)
    # prefer cleaned option lines; fallback to keyword-bearing raw
    try:
        cleaned = _portal_option_lines(raw_all)
    except Exception:
        cleaned = []
    for s in cleaned + raw_all:
        s = str(s or "").strip()
        if not s or s in seen:
            continue
        # drop chat / long noise
        if len(s) > 18 or any(ch in s for ch in ("击杀", "在天下会", "--", "u&", "7u")):
            continue
        if not any(k in s for k in ("地宫", "传送", "上层", "深处", "下层", "西王母", "康巴", "苦水", "地火", "野人", "俺答")):
            continue
        seen.add(s)
        out.append(s)
    return out


def open_service_panel_mem(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
    settle_s: float = 0.15,
    allow_aa3330: bool = False,
) -> dict:
    """
    After NPCSayHello: read current NPC service panel texts (passive).

    Default NEVER calls AA3330 — live 2026-07-22 showed:
      · task-done NPC: type=6 invents mixed layer1+layer2 lines
      · task-pending NPC: AA3330 hangs / CreateRemoteThread err=5 / client crash
    AA3330 remains experimental only (allow_aa3330=True lab tools).
    """
    out: dict = {"ok": False, "texts": [], "invokes": [], "mode": "passive"}
    dlg = 0
    dlg_name = ""
    for name, d, shown in get_npc_dialogs(session, log=log):
        if shown and name in ("Win_NPCContent", "Win_NPC"):
            dlg = int(d)
            dlg_name = name
            if name == "Win_NPCContent":
                break
    if not dlg:
        # try any content ptr even if IsDlgShow false
        for name, d, shown in get_npc_dialogs(session, log=log):
            if name in ("Win_NPCContent", "Win_NPC") and d:
                dlg = int(d)
                dlg_name = name
                break
    if dlg:
        out["dlg"] = hex(dlg)
        out["dlg_name"] = dlg_name

    # Always passive-read first (legal Hello already opened the real panel).
    if settle_s and settle_s > 0:
        time.sleep(max(0.0, float(settle_s)))
    texts = collect_portal_menu_texts(session, log=log)
    out["texts"] = texts
    blob = " ".join(texts)
    if any(k in blob for k in ("地宫传送", "上层", "深处", "西王母", "传送", "地宫")):
        out["ok"] = True
        out["used_type"] = None
        _log(log, f"npc_service_mem open passive texts={texts[:8]}")
        return out

    # Experimental only — never on main portal path.
    if not allow_aa3330:
        out["error"] = "panel texts empty (passive; AA3330 disabled)"
        if not dlg:
            out["error"] = "no_npc_dlg"
        return out

    if not dlg:
        out["error"] = "no_npc_dlg"
        return out
    out["mode"] = "aa3330_experimental"
    for typ in (6,):
        ir = invoke_talk_select(
            session, dlg_ptr=dlg, opt_type=typ, opt_id=None, log=log
        )
        out["invokes"].append({"type": typ, **{k: ir.get(k) for k in ("ok", "error", "note")}})
        time.sleep(max(0.15, 0.35))
        texts = collect_portal_menu_texts(session, log=log)
        out["texts"] = texts
        blob = " ".join(texts)
        if any(k in blob for k in ("地宫传送", "上层", "深处", "西王母", "传送")):
            out["ok"] = True
            out["used_type"] = typ
            _log(log, f"npc_service_mem open experimental type={typ} texts={texts[:8]}")
            return out
    out["error"] = "panel texts empty after experimental AA3330"
    return out


def classify_texts_stage(texts: list[str]) -> str:
    """
    Stage from *live* panel labels (no AA3330 force-fill):
      layer2 / layer2_partial — 上层/深处 present (NPC task-done or after entry)
      layer1 — only X级地宫传送 / 地宫传送 (NPC still has tasks/services)
    Task titles like 高级地宫杀怪 are NOT dest lines.
    """
    labels = [str(x or "") for x in (texts or []) if x]
    # drop task noise from stage decision
    clean = [
        x for x in labels
        if not any(t in x for t in ("杀怪", "BOSS", "boss", "任务", "兑换", "发放"))
    ]
    if not clean:
        clean = labels
    blob = " ".join(clean)
    has_upper = any("上层" in x for x in clean)
    has_deep = any(("深处" in x) or ("下层" in x) for x in clean)
    has_entry = any(
        ("地宫传送" in x) and ("杀怪" not in x) and ("BOSS" not in x)
        for x in clean
    )
    # Prefer real dest lines over residual entry labels
    if has_upper and has_deep:
        return "layer2"
    if has_upper or has_deep:
        return "layer2_partial"
    if has_entry:
        return "layer1"
    if "地宫" in blob or "传送" in blob:
        return "layer1_maybe"
    return "unknown"



def probe_panel_after_hello(
    session: GameAttachSession,
    *,
    tier: str = "",
    layer: str = "deep",
    log: LogFn | None = None,
    settle_s: float = 0.32,
) -> dict:
    """
    Passive post-Hello probe only. Never calls AA3330.

    Returns stage/texts so task_api UI menu can decide:
      · layer2 → click 上层/深处
      · layer1 → click X级地宫传送 then 上层/深处
    Does NOT claim transfer success (need_ui_click always True).
    """
    notes: list[str] = []
    open_r = open_service_panel_mem(
        session, log=log, settle_s=settle_s, allow_aa3330=False
    )
    notes.append(
        "open="
        + str(
            {
                k: open_r.get(k)
                for k in ("ok", "mode", "used_type", "texts", "error", "dlg_name")
            }
        )
    )
    texts = list(open_r.get("texts") or collect_portal_menu_texts(session, log=log))
    stage = classify_texts_stage(texts)
    notes.append(f"stage0={stage} texts={texts[:10]}")

    ly = str(layer or "deep").lower()
    want_deep = ly in ("deep", "dungeon_deep", "dungeon_lower", "lower") or "深" in str(
        layer
    )
    has_upper = any("上层" in x for x in texts)
    has_deep = any(("深处" in x) or ("下层" in x) for x in texts)
    has_entry = any(
        ("地宫传送" in x) and ("杀怪" not in x) and ("BOSS" not in x) for x in texts
    )
    has_dest = has_deep if want_deep else has_upper
    # ok means "panel readable" not "teleport done"
    ok = bool(texts) or bool(open_r.get("ok"))
    return {
        "ok": ok,
        "stage": stage,
        "texts": texts,
        "has_dest": has_dest,
        "has_entry": has_entry,
        "layer": "deep" if want_deep else "upper",
        "tier": str(tier or ""),
        "notes": notes,
        "error": None if ok else (open_r.get("error") or "panel empty after hello"),
        "need_ui_click": True,
        "scene_ready": False,  # never claim scene change from passive probe
    }


def ensure_layer2_then_dest(
    session: GameAttachSession,
    *,
    tier: str = "",
    layer: str = "deep",
    log: LogFn | None = None,
    allow_aa3330: bool = False,
) -> dict:
    """
    Compatibility wrapper for task_api.

    Default = probe_panel_after_hello (passive, safe).
    allow_aa3330=True is lab-only and still does NOT select dest via AA3330
    (selection remains UI click) — expand via type=2 is also disabled by default
    because it hangs/crashes on task-pending NPC mouths.
    """
    if not allow_aa3330:
        return probe_panel_after_hello(
            session, tier=tier, layer=layer, log=log
        )
    # Experimental path retained for tools only.
    notes: list[str] = []
    open_r = open_service_panel_mem(
        session, log=log, settle_s=0.35, allow_aa3330=True
    )
    notes.append(
        "open="
        + str({k: open_r.get(k) for k in ("ok", "mode", "used_type", "texts", "error")})
    )
    texts = list(open_r.get("texts") or [])
    stage = classify_texts_stage(texts)
    ly = str(layer or "deep").lower()
    want_deep = ly in ("deep", "dungeon_deep", "dungeon_lower", "lower") or "深" in str(
        layer
    )
    has_upper = any("上层" in x for x in texts)
    has_deep = any(("深处" in x) or ("下层" in x) for x in texts)
    has_entry = any("地宫传送" in x for x in texts)
    has_dest = has_deep if want_deep else has_upper
    ok = bool(open_r.get("ok")) and bool(texts)
    return {
        "ok": ok,
        "stage": stage,
        "texts": texts,
        "has_dest": has_dest,
        "has_entry": has_entry,
        "layer": "deep" if want_deep else "upper",
        "notes": notes,
        "error": None if ok else (open_r.get("error") or "mem menu incomplete"),
        "need_ui_click": True,
        "scene_ready": False,
    }





def _read_txt_template_label(session: GameAttachSession, host: int, index: int) -> str:
    """Read Txt_Template{i} label (live: text ptr / CJK at control+0xB8)."""
    if not host:
        return ""
    ctrl = 0
    try:
        from app.core.aui_click import get_aui_dlg_item_ptr

        ctrl = int(get_aui_dlg_item_ptr(session, int(host), "Txt_Template%d" % int(index)) or 0)
    except Exception:
        ctrl = 0
    if not ctrl:
        return ""
    blob = _rpm(session, int(ctrl) & 0xFFFFFFFF, 0x160)
    if len(blob) < 0xC0:
        return ""
    # Prefer +0xB8 (live dump), then scan nearby pointer slots.
    cands: list[int] = []
    for off in (0xB8, 0xBC, 0xB0, 0xB4, 0xC0, 0xA8, 0xAC, 0x80, 0x84):
        if off + 4 <= len(blob):
            cands.append(_u32(blob, off))
    for p in cands:
        if not (0x10000 < p < 0x7FFE0000):
            continue
        s = _read_str(session, p)
        if s and _cjk_or_portal(s) and len(s) < 48:
            return s
        # AString / indirect
        for o in (0, 4, 8, 0xC):
            raw = _rpm(session, (p + o) & 0xFFFFFFFF, 4)
            if len(raw) < 4:
                continue
            q = _u32(raw, 0)
            if 0x10000 < q < 0x7FFE0000:
                s2 = _read_str(session, q)
                if s2 and _cjk_or_portal(s2) and len(s2) < 48:
                    return s2
    # Last resort: scan control blob for CJK string pointers
    for off in range(0, min(len(blob) - 4, 0x140), 4):
        p = _u32(blob, off)
        if not (0x10000 < p < 0x7FFE0000):
            continue
        s = _read_str(session, p)
        if s and _cjk_or_portal(s) and len(s) < 48:
            if any(k in s for k in ("地宫", "传送", "上层", "深处", "下层", "BOSS", "任务", "康巴", "西王母")):
                return s
    return ""


def dump_host_service_table(
    session: GameAttachSession, *, log: LogFn | None = None, max_rows: int = 16
) -> dict:
    """Read *0x19561F0 host service table (L1 task-open + L2 expand UI)."""
    out: dict = {
        "ok": False,
        "host": 0,
        "table": 0,
        "count": 0,
        "rows": [],
        "error": None,
    }
    try:
        raw = _rpm(session, _va(session, VA_GLOBAL_HOSTISH), 4)
        host = _u32(raw, 0) if len(raw) >= 4 else 0
    except Exception as e:
        out["error"] = f"host global: {e}"
        return out
    out["host"] = int(host or 0) & 0xFFFFFFFF
    if not host:
        out["error"] = "host null"
        return out
    hb = _rpm(session, host, 0x2C0)
    if len(hb) < OFF_HOST_COUNT + 4:
        out["error"] = "host blob short"
        return out
    table = _u32(hb, OFF_HOST_TABLE)
    count = _i32(hb, OFF_HOST_COUNT)
    out["table"] = int(table or 0) & 0xFFFFFFFF
    out["count"] = int(count or 0)
    if count <= 0 or count > 64 or not table:
        out["error"] = f"empty/invalid count={count} table=0x{table:X}"
        return out
    n = min(int(count), int(max_rows))
    rows: list[dict] = []
    for i in range(n):
        base = (int(table) & 0xFFFFFFFF) + i * HOST_ROW_STRIDE
        rb = _rpm(session, base, HOST_ROW_STRIDE)
        if len(rb) < HOST_ROW_STRIDE:
            break
        field0 = _u32(rb, HOST_FIELD0_DW)
        ptr0 = _u32(rb, HOST_FIELD0_PTR)
        dwords = [_u32(rb, off) for off in range(0, 0x40, 4)]
        labels: list[str] = []
        tlab = _read_txt_template_label(session, host, i)
        if tlab:
            labels.append(tlab)
        for off in (0x08, 0x0C, 0x78, 0x7C, 0xA8, 0xAC):
            if off + 4 > len(rb):
                continue
            p = _u32(rb, off)
            if 0x10000 < p < 0x7FFE0000:
                s = _read_str(session, p)
                if s and _cjk_or_portal(s) and s not in labels:
                    labels.append(s)
        for s in _scan_portal_keywords(session, base, HOST_ROW_STRIDE):
            if s not in labels:
                labels.append(s)
        if ptr0 and 0x10000 < ptr0 < 0x7FFE0000:
            for s in _scan_portal_keywords(session, ptr0, 0xA0):
                if s not in labels:
                    labels.append(s)
        f0 = int(field0) & 0xFFFFFFFF
        is_magic = (f0 & 0x7FFFFFFF) in (0x40ABCDEF, 0x40FEDCBA)
        lab = ""
        # Prefer real UI template text; avoid empty magic rows without text
        cleaned_labels: list[str] = []
        for cand in labels:
            cc = _clean_portal_label(cand)
            if cc and cc not in cleaned_labels:
                cleaned_labels.append(cc)
        if cleaned_labels:
            labels = cleaned_labels
        for cand in labels:
            if _cjk_or_portal(cand):
                lab = _clean_portal_label(cand)
                break
        if not lab and labels:
            lab = _clean_portal_label(labels[0])
        rows.append(
            {
                "index": i,
                "field0": f0,
                "ptr0": int(ptr0) & 0xFFFFFFFF,
                "labels": labels[:8],
                "label": lab,
                "dwords": dwords[:12],
                "is_magic": bool(is_magic),
                "is_service_marker": f0 in SUB_SVC_MARKERS or is_magic,
            }
        )
    out["rows"] = rows
    out["ok"] = bool(rows)
    if not rows:
        out["error"] = "no rows decoded"
    _log(
        log,
        f"npc_service_mem host dump host=0x{out['host']:X} table=0x{out['table']:X} "
        f"count={out['count']} labels={[r.get('label') for r in rows[:6]]}",
    )
    return out


def invoke_host_select(
    session: GameAttachSession,
    *,
    index: int,
    log: LogFn | None = None,
    hwnd: int = 0,
    prefer_bridge: bool = True,
) -> dict:
    """
    Activate host service row by index via UI-thread AA5590.

    This is the legal path for L1 menus when talk_proc is null (tasks unfinished).
    """
    out: dict = {"ok": False, "via": "", "index": int(index)}
    host_dump = dump_host_service_table(session, log=log, max_rows=32)
    out["host_count"] = host_dump.get("count")
    out["host"] = host_dump.get("host")
    if not host_dump.get("ok"):
        out["error"] = host_dump.get("error") or "host dump failed"
        return out
    idx = int(index)
    if idx < 0 or idx >= int(host_dump.get("count") or 0):
        out["error"] = "host index out of range idx=%s count=%s" % (
            idx,
            host_dump.get("count"),
        )
        return out
    row = None
    for r in host_dump.get("rows") or []:
        if int(r.get("index") or -1) == idx:
            row = r
            break
    out["row"] = {
        "index": idx,
        "field0": (row or {}).get("field0"),
        "label": (row or {}).get("label"),
    }

    if prefer_bridge:
        try:
            from app.core.xajh_bridge import ensure_bridge

            bridge = ensure_bridge(
                int(session.pid),
                log=log,
                inject_if_needed=True,
                hwnd=int(hwnd or 0) or None,
            )
            if bridge is not None:
                try:
                    br = bridge.npc_host_select(
                        int(idx),
                        hwnd=int(hwnd or 0) or None,
                        timeout_ms=5000,
                    )
                    out["via"] = "bridge"
                    out["ok"] = bool(br.ok)
                    out["ret"] = br.ret
                    out["note"] = br.note or br.error or ""
                    out["error"] = None if br.ok else (br.error or "bridge host select failed")
                    _log(
                        log,
                        "npc_service_mem host_select bridge ok=%s idx=%s lab=%r note=%r"
                        % (br.ok, idx, (row or {}).get("label"), out["note"]),
                    )
                    return out
                finally:
                    try:
                        bridge.close()
                    except Exception:
                        pass
            else:
                out["bridge"] = "not_ready"
        except Exception as e:
            out["bridge_err"] = str(e)
            _log(log, f"npc_service_mem host_select bridge err: {e}")

    # Fallback: remote cdecl AA5590(index, 0). Prefer bridge UI thread; CRT is lab-only
    # when an older bridge is still mapped (needs game restart for new CMD).
    blocked, brsn = _pid_blocked(session)
    if blocked:
        out["error"] = brsn or "remote_blocked"
        out["via"] = "remote_blocked"
        out["ok"] = False
        _log(log, f"npc_service_mem host_select blocked: {brsn}")
        return out
    try:
        from app.core.remote_runtime import remote_call_cdecl_x86

        va = _va(session, 0x00AA5590)
        # set selected index mirror
        host = int(out.get("host") or 0) & 0xFFFFFFFF
        if host:
            _wpm_u32(session, host + 0x2B8, int(idx) & 0xFFFFFFFF)
        remote_call_cdecl_x86(
            int(session.pid),
            int(va),
            [int(idx) & 0xFFFFFFFF, 0],
            timeout_ms=4000,
        )
        out["via"] = "remote_crt"
        out["ok"] = True
        out["va"] = va
        out["note"] = "AA5590 remote CRT (bridge missing/stale; restart game for NPC_HOST_SELECT)"
        out["error"] = None
        _log(
            log,
            "npc_service_mem host_select CRT ok idx=%s lab=%r va=0x%X"
            % (idx, (row or {}).get("label"), va),
        )
        return out
    except Exception as e:
        out["crt_err"] = str(e)
        _note_remote_hard(session, e)
        _log(log, f"npc_service_mem host_select CRT err: {e}")

    out["error"] = out.get("error") or "host select requires bridge (AA5590 UI thread)"
    out["via"] = out.get("via") or "none"
    return out


def _option_catalog(snap: TalkProcSnapshot) -> list[dict]:
    rows: list[dict] = []
    for o in (snap.options if snap and snap.ok else []) or []:
        lab = o.label or ""
        if lab:
            rows.append(
                {
                    "index": int(o.index),
                    "opt_id": int(o.opt_id) & 0xFFFFFFFF,
                    "label": lab,
                    "kind": "entry",
                    "sub_count": int(o.sub_count or 0),
                    "texts": list(o.texts or [])[:6],
                    "sub_texts": list(o.sub_texts or [])[:6],
                }
            )
        for s in list(getattr(o, "subs", None) or []):
            slab = s.label or ""
            if not slab and not s.is_service_marker:
                continue
            rows.append(
                {
                    "index": int(o.index),
                    "sub_index": int(s.index),
                    "opt_id": int(s.raw0) & 0xFFFFFFFF,
                    "parent_opt_id": int(o.opt_id) & 0xFFFFFFFF,
                    "label": slab or ("sub%d" % s.index),
                    "kind": "sub",
                    "is_service_marker": bool(s.is_service_marker),
                    "texts": list(s.texts or [])[:6],
                    "sub_count": 0,
                    "sub_texts": [],
                }
            )
    return rows


def _needles_for_layer(layer: str) -> list[str]:
    ly = str(layer or "deep").lower()
    if ly in ("deep", "dungeon_deep", "dungeon_lower", "lower") or "深" in str(layer):
        return ["深处", "下层", "地宫深处", "升级地宫深处"]
    return ["上层", "一层", "地宫上层"]


def _needles_for_entry(tier: str) -> list[str]:
    tier = str(tier or "").strip()
    if tier in ("初级", "中级", "高级"):
        return [f"{tier}地宫传送", "地宫传送"]
    if tier in ("升级", "练级"):
        return ["升级地宫传送", "练级地宫传送", "地宫传送"]
    return ["地宫传送", "传送服务"]


def _save_option_cache(tid: int, stage: str, rows: list[dict]) -> None:
    if not tid or not rows:
        return
    # Never poison cache with empty labels (live bug 2026-07-24).
    clean = []
    for r in rows:
        lab = str((r or {}).get("label") or "").strip()
        if not lab:
            continue
        clean.append(dict(r))
    if not clean:
        return
    try:
        import json
        from pathlib import Path as _P

        path = _P(__file__).resolve().parents[2] / "data" / "portal_talk_options.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        key = str(int(tid))
        slot = data.setdefault(key, {})
        slot[str(stage)] = clean
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_option_cache(tid: int, stage: str) -> list[dict]:
    try:
        import json
        from pathlib import Path as _P

        path = _P(__file__).resolve().parents[2] / "data" / "portal_talk_options.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        rows = list((data.get(str(int(tid))) or {}).get(str(stage)) or [])
        return [r for r in rows if str((r or {}).get("label") or "").strip()]
    except Exception:
        return []



def _host_option_catalog(host_dump: dict) -> list[dict]:
    rows: list[dict] = []
    for r in (host_dump.get("rows") if isinstance(host_dump, dict) else None) or []:
        lab = str(r.get("label") or "").strip()
        if not lab:
            # Keep magic/service rows only if field0 useful; still skip empty UI
            continue
        rows.append(
            {
                "index": int(r.get("index") or 0),
                "opt_id": int(r.get("field0") or 0) & 0xFFFFFFFF,
                "label": lab,
                "kind": "host",
                "sub_count": 0,
                # Primary label only — scanned sibling labels can poison matching.
                "texts": [lab] if lab else [],
                "sub_texts": [],
                "is_magic": bool(r.get("is_magic")),
            }
        )
    return rows


def _host_menu_signature(host_dump: dict) -> tuple[tuple[int, int, bool], ...]:
    """Stable-enough menu shape for validating an L1 -> L2 transition."""
    rows = list((host_dump or {}).get("rows") or [])
    return tuple(
        sorted(
            (
                int(row.get("index") or 0),
                int(row.get("field0") or 0) & 0xFFFFFFFF,
                bool(row.get("is_magic")),
            )
            for row in rows
        )
    )


def _cached_layer_host_option(portal_tid: int, layer: str) -> dict | None:
    """Return a cached L2 host row by label, never by cache position alone."""
    rows = _load_option_cache(int(portal_tid or 0), "layer2")
    want_deep = str(layer or "").lower() in (
        "deep",
        "dungeon_deep",
        "dungeon_lower",
        "lower",
    ) or "深" in str(layer or "")
    for row in rows:
        if str(row.get("kind") or "") != "host":
            continue
        label = _clean_portal_label(str(row.get("label") or ""))
        if want_deep and any(token in label for token in ("深处", "下层")):
            return dict(row)
        if not want_deep and ("上层" in label or ("一层" in label and "下层" not in label)):
            return dict(row)
    return None


def _can_recover_cached_l2(
    before_host: dict,
    after_host: dict,
    cached_option: dict | None,
) -> bool:
    """Allow cache recovery only after a verified task-entry menu transition."""
    if not cached_option:
        return False
    before_sig = _host_menu_signature(before_host)
    after_sig = _host_menu_signature(after_host)
    rows = list((after_host or {}).get("rows") or [])
    raw_index = cached_option.get("index")
    try:
        wanted = int(raw_index) if raw_index is not None else -1
    except (TypeError, ValueError):
        wanted = -1
    return bool(
        before_sig
        and after_sig
        and before_sig != after_sig
        and len(rows) == 2
        and wanted >= 0
        and any(
            int(row["index"]) == wanted
            for row in rows
            if row.get("index") is not None
        )
        and not any(bool(row.get("is_magic")) for row in rows)
    )


def _infer_direct_layer_host_rows(host_dump: dict, texts: list[str]) -> list[dict]:
    """Recover a two-row direct layer menu when one UI label failed to decode."""
    raw_rows = list((host_dump or {}).get("rows") or [])
    if len(raw_rows) != 2:
        return []
    ordered = sorted(raw_rows, key=lambda r: int(r.get("index") or 0))
    labels = [_clean_portal_label(str(r.get("label") or "")) for r in ordered]
    blob = " ".join(labels + [str(t or "") for t in (texts or [])])
    if any(
        token in blob
        for token in ("地宫传送", "杀怪", "BOSS", "boss", "任务", "发放", "兑换")
    ):
        return []
    if any(bool(r.get("is_magic")) for r in ordered):
        return []
    if not any(token in blob for token in ("上层", "深处", "下层")):
        return []

    inferred: list[dict] = []
    for slot, row in enumerate(ordered):
        label = labels[slot] or ("上层" if slot == 0 else "深处")
        inferred.append(
            {
                "index": int(row.get("index") or 0),
                "opt_id": int(row.get("field0") or 0) & 0xFFFFFFFF,
                "label": label,
                "kind": "host",
                "sub_count": 0,
                "texts": [label],
                "sub_texts": [],
                "is_magic": False,
                "inferred_direct_layer": True,
            }
        )
    return inferred


def _invoke_portal_option(
    session: GameAttachSession,
    opt: dict,
    *,
    dlg_ptr: int = 0,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> dict:
    """Dispatch talk_proc AA3330 or host AA5590 based on option kind."""
    kind = str((opt or {}).get("kind") or "entry")
    if kind == "host":
        idx = int((opt or {}).get("index") or 0)
        want_lab = _clean_portal_label(str((opt or {}).get("label") or ""))
        # Re-resolve by live host label so we never click idx=0 "上层" when
        # the intended row is "深处" (index drift / polluted catalog).
        if want_lab:
            try:
                host_d = dump_host_service_table(session, log=log, max_rows=32)
                rows = list(host_d.get("rows") or [])
                resolved = None
                # exact
                for r in rows:
                    lab = _clean_portal_label(str(r.get("label") or ""))
                    if lab and lab == want_lab:
                        resolved = int(r.get("index") or 0)
                        break
                # contains (西王母宫深处 / 深处)
                if resolved is None:
                    for r in rows:
                        lab = _clean_portal_label(str(r.get("label") or ""))
                        if lab and (want_lab in lab or lab in want_lab):
                            # avoid opposite layer: if want has 深处, skip 上层-only
                            if "深处" in want_lab or "下层" in want_lab:
                                if "上层" in lab and "深处" not in lab and "下层" not in lab:
                                    continue
                            if "上层" in want_lab and "深处" not in want_lab:
                                if any(k in lab for k in ("深处", "下层")):
                                    continue
                            resolved = int(r.get("index") or 0)
                            break
                if resolved is not None and resolved != idx:
                    _log(
                        log,
                        "npc_service_mem host re-resolve lab=%s idx %s→%s"
                        % (want_lab, idx, resolved),
                    )
                    idx = resolved
                elif resolved is None:
                    # If live labels exist and none match, refuse bare index click.
                    live_labs = [
                        _clean_portal_label(str(r.get("label") or ""))
                        for r in rows
                        if str(r.get("label") or "").strip()
                    ]
                    if live_labs and any(
                        k in want_lab for k in ("上层", "深处", "下层", "地宫传送")
                    ):
                        return {
                            "ok": False,
                            "error": "host live label mismatch want=%s live=%s"
                            % (want_lab, live_labs[:6]),
                            "via": "host_label_guard",
                            "index": idx,
                        }
            except Exception as e:
                _log(log, "npc_service_mem host re-resolve err: %s" % e)
        return invoke_host_select(
            session,
            index=idx,
            log=log,
            hwnd=int(hwnd or 0),
            prefer_bridge=True,
        )
    return invoke_talk_select(
        session,
        dlg_ptr=int(dlg_ptr or 0),
        opt_type=2,
        opt_id=int((opt or {}).get("opt_id") or 0),
        log=log,
        prefer_bridge=True,
        hwnd=int(hwnd or 0),
    )




def _l2_label_fits_tier(label: str, tier: str) -> bool:
    """Reject obviously wrong L2 map names for the requested tier.

    初级/中级 must not click 西王母* leftover panels (live 2026-07-24).
    """
    lab = str(label or "")
    t = str(tier or "").strip()
    if not lab:
        return True
    if t in ("初级", "中级"):
        if "西王母" in lab:
            return False
    if t in ("初级",) and any(k in lab for k in ("野人", "俺答", "汗陵")):
        return False
    if t in ("中级",) and any(k in lab for k in ("俺答", "汗陵")):
        return False
    return True


def _scene_matches_layer(scene_id: int | None, layer: str) -> bool:
    """True if scene_id is in the requested portal layer band."""
    if scene_id is None:
        return False
    try:
        sid = int(scene_id)
    except (TypeError, ValueError):
        return False
    # -1/0 appear during map transition; never treat as a real layer hit.
    if sid <= 0:
        return False
    try:
        from app.core.portal_service import (
            PORTAL_UPPER_SCENE_IDS,
            PORTAL_DEEP_SCENE_IDS,
            resolve_dungeon_scene_ids,
        )
    except Exception:
        return False
    ly = str(layer or "deep").lower()
    deep = ly in ("deep", "dungeon_deep", "dungeon_lower", "lower") or "深" in str(layer)
    band = PORTAL_DEEP_SCENE_IDS if deep else PORTAL_UPPER_SCENE_IDS
    if sid in band:
        return True
    # also accept any resolved candidate list for unknown tiers
    try:
        for tier in ("初级", "中级", "高级", "升级"):
            if sid in set(resolve_dungeon_scene_ids(tier, "deep" if deep else "upper") or []):
                return True
    except Exception:
        pass
    return False


def _mark_scene_success(out: dict, before_scene, after_scene, *, how: str) -> dict:
    out["ok"] = True
    out["before_scene"] = before_scene
    out["after_scene"] = after_scene
    out["note"] = "scene %s→%s via %s" % (before_scene, after_scene, how)
    return out


def portal_select_by_function(
    session: GameAttachSession,
    *,
    tier: str = "",
    layer: str = "deep",
    portal_tid: int = 0,
    hwnd: int = 0,
    log: LogFn | None = None,
    settle_s: float = 0.35,
    wait_scene_s: float = 10.0,
    before_scene: int | None = None,
    stop_event=None,
    movement_pulse: Callable[[str], object] | None = None,
    movement_release: Callable[[str], object] | None = None,
    scene_reader: Callable[[], object] | None = None,
) -> dict:
    """
    Legal portal path — **二级优先** (上层/深处):

      Hello already done
      1) 若面板已是二级（上层/深处）→ 直接 host/talk 选层，不看一级
      2) 若只有一级（X级地宫传送）→ 点 entry 展开/直传，再选二级
      3) 成功条件：scene 变化（entry 直进深处也可）

    talk_proc 空时走 host 表 + AA5590；有 talk_proc 走 AA3330。
    """
    notes: list[str] = []
    out: dict = {
        "ok": False,
        "method": "talk_fn",
        "stage": "none",
        "options": [],
        "clicked": [],
        "notes": notes,
        "policy": "layer2_first",
    }

    def stopped() -> bool:
        return bool(stop_event is not None and stop_event.is_set())

    def sleep(sec: float) -> bool:
        if movement_pulse is None:
            if stop_event is not None:
                return bool(stop_event.wait(float(sec)))
            time.sleep(float(sec))
            return False
        deadline = time.monotonic() + max(0.0, float(sec))
        while time.monotonic() < deadline:
            if movement_pulse is not None:
                try:
                    movement_pulse("portal_menu_wait")
                except Exception:
                    pass
            left = deadline - time.monotonic()
            if left <= 0:
                break
            wait_s = min(0.12, left)
            if stop_event is not None:
                if stop_event.wait(wait_s):
                    return True
            else:
                time.sleep(wait_s)
        return bool(stop_event is not None and stop_event.is_set())

    def release_movement(reason: str) -> None:
        if movement_release is not None:
            try:
                movement_release(reason)
            except Exception:
                pass

    def scene_now() -> int | None:
        """Return positive scene id only. -1/0 mean transition/invalid."""
        try:
            if scene_reader is not None:
                sp = scene_reader()
            else:
                from app.core.automove import read_scene_position

                sp = read_scene_position(session, log=lambda _m: None)
            if not getattr(sp, "ok", False):
                return None
            sid = int(getattr(sp, "scene_id", 0) or 0)
            if sid <= 0:
                return None
            return sid
        except Exception:
            return None

    def synthesize_from_texts(host_dump: dict, texts: list[str]) -> list[dict]:
        """When Txt_Template decode fails, map panel texts onto host indices."""
        rows_h = list(host_dump.get("rows") or [])
        if not rows_h:
            return []
        layer_labs: list[str] = []
        entry_labs: list[str] = []
        for t0 in texts or []:
            t0 = _clean_portal_label(t0)
            if not t0:
                continue
            if any(k in t0 for k in ("上层", "深处", "下层")) and "地宫传送" not in t0:
                if t0 not in layer_labs:
                    layer_labs.append(t0)
            if "地宫传送" in t0 and t0 not in entry_labs:
                entry_labs.append(t0)
        usable = [r for r in rows_h if not r.get("is_magic")]
        if not usable:
            usable = list(rows_h)
        out_r: list[dict] = []
        # Full L2 pair only (上层+深处). Lone 上层 from task pollution is NOT L2.
        has_u = any("上层" in x or ("一层" in x and "下层" not in x) for x in layer_labs)
        has_d = any(any(k in x for k in ("深处", "下层")) for x in layer_labs)
        if layer_labs and has_u and has_d:
            # Pair one upper + one deep label onto the host rows.  texts can
            # carry both the full map name and the short side name (e.g.
            # 西王母宫上层/上层 and 西王母宫深处/深处); slicing the first N
            # layer_labs wrongly picked two upper rows (live 2026-08-03
            # rows=['西王母宫上层','上层']).  Slot order follows
            # _infer_direct_layer_host_rows: slot0=上层, slot1=深处.
            u_lab = next(
                (x for x in layer_labs if "上层" in x or ("一层" in x and "下层" not in x)),
                "上层",
            )
            d_lab = next(
                (x for x in layer_labs if any(k in x for k in ("深处", "下层"))),
                "深处",
            )
            pair_labs = [u_lab, d_lab]
            for i, lab in enumerate(pair_labs[: len(usable)]):
                r = usable[i]
                out_r.append(
                    {
                        "index": int(r.get("index") or i),
                        "opt_id": int(r.get("field0") or 0) & 0xFFFFFFFF,
                        "label": lab,
                        "kind": "host",
                        "sub_count": 0,
                        "texts": [lab],
                        "sub_texts": [],
                        "is_magic": bool(r.get("is_magic")),
                    }
                )
            return out_r
        # Prefer entry when 地宫传送 present (even if a lone 上层 noise exists)
        if entry_labs:
            r = usable[0]
            lab = entry_labs[0]
            out_r.append(
                {
                    "index": int(r.get("index") or 0),
                    "opt_id": int(r.get("field0") or 0) & 0xFFFFFFFF,
                    "label": lab,
                    "kind": "host",
                    "sub_count": 0,
                    "texts": [lab],
                    "sub_texts": [],
                    "is_magic": bool(r.get("is_magic")),
                }
            )
        return out_r

    def merge_catalog(talk_rows: list[dict], host_dump: dict) -> list[dict]:
        """Talk + host; host layer labels win priority for matching later."""
        out_rows: list[dict] = []
        seen: set[str] = set()
        # Prefer host first so L2 Txt_Template is primary when both exist.
        for r in _host_option_catalog(host_dump):
            lab = str(r.get("label") or "")
            key = "h:%s:%s" % (r.get("index"), lab)
            if key in seen:
                continue
            seen.add(key)
            out_rows.append(r)
        for r in talk_rows or []:
            lab = str(r.get("label") or "")
            key = "t:%s:%s" % (r.get("opt_id"), lab)
            if key in seen:
                continue
            seen.add(key)
            out_rows.append(r)
        return out_rows

    def pick(rows_in: list[dict], needles: list[str], *, allow_task: bool = False) -> dict | None:
        """Match by *label only*.

        Host row.texts/labels can be memory-scan polluted (上层 row may also
        contain 深处 bytes). Never match needles against that blob — that
        caused deep tasks to click 西王母宫上层 (idx=0).
        """
        bad = ("杀怪", "BOSS", "发放", "任务", "每日")
        needles = [str(n) for n in (needles or []) if str(n)]
        want_deep = any(any(k in n for k in ("深处", "下层")) for n in needles)
        want_upper = (not want_deep) and any(
            any(k in n for k in ("上层", "一层")) for n in needles
        )

        def lab_of(r: dict) -> str:
            return _clean_portal_label(str((r or {}).get("label") or ""))

        def is_opposite(lab: str) -> bool:
            if not lab:
                return False
            if want_deep:
                if any(k in lab for k in ("深处", "下层")):
                    return False
                return "上层" in lab
            if want_upper:
                if "上层" in lab or ("一层" in lab and "下层" not in lab):
                    return False
                return any(k in lab for k in ("深处", "下层"))
            return False

        def usable(r: dict) -> bool:
            lab = lab_of(r)
            if not lab:
                return False
            if not allow_task and any(b in lab for b in bad):
                return False
            if is_opposite(lab):
                return False
            return True

        # 1) exact label
        for n in needles:
            for r in rows_in:
                if not usable(r):
                    continue
                if lab_of(r) == n:
                    return r
        # 2) needle contained in label only
        for n in needles:
            for r in rows_in:
                if not usable(r):
                    continue
                lab = lab_of(r)
                if n and n in lab:
                    return r
        # 3) soft: any usable row whose label has deep/upper keyword matching intent
        if want_deep or want_upper:
            keys = ("深处", "下层") if want_deep else ("上层",)
            for r in rows_in:
                if not usable(r):
                    continue
                lab = lab_of(r)
                if any(k in lab for k in keys):
                    return r
        return None

    def has_layer_opts(rows_in: list[dict]) -> bool:
        """True only when BOTH upper and deep/lower options exist.

        A lone 「西王母宫上层」 from task pollution must not count as L2
        (else 初级地宫传送 panels are mis-clicked as layer2).
        """
        labs = [str(r.get("label") or "") for r in (rows_in or [])]
        has_u = any(("上层" in lab or ("一层" in lab and "下层" not in lab)) and "地宫传送" not in lab for lab in labs)
        has_d = any(any(k in lab for k in ("深处", "下层")) for lab in labs)
        return bool(has_u and has_d)

    def has_entry_opts(rows_in: list[dict]) -> bool:
        return any("地宫传送" in str(r.get("label") or "") for r in rows_in)

    def wait_scene_ok(how: str) -> bool:
        """Success only on *positive* scene id that matches requested layer.

        Never treat scene_id <= 0 (often -1 mid-transfer) as success — that
        caused 站街 with ok=True / scene 68→-1.
        """
        t_end = time.monotonic() + max(1.0, float(wait_scene_s))
        last_sid = None
        saw_transition = False
        while time.monotonic() < t_end:
            if stopped():
                out["error"] = "portal fn wait stopped"
                return False
            # Peek raw id so we can log transition without accepting -1.
            raw_sid = None
            try:
                if scene_reader is not None:
                    sp = scene_reader()
                else:
                    from app.core.automove import read_scene_position

                    sp = read_scene_position(session, log=lambda _m: None)
                if getattr(sp, "ok", False):
                    raw_sid = int(getattr(sp, "scene_id", 0) or 0)
            except Exception:
                raw_sid = None
            if raw_sid is not None and raw_sid <= 0:
                saw_transition = True
                last_sid = raw_sid
                if sleep(0.25):
                    out["error"] = "portal fn wait stopped"
                    return False
                continue

            sid = scene_now()
            last_sid = sid if sid is not None else last_sid
            if sid is None:
                if sleep(0.25):
                    out["error"] = "portal fn wait stopped"
                    return False
                continue
            if before_scene is not None and int(sid) == int(before_scene):
                if sleep(0.25):
                    out["error"] = "portal fn wait stopped"
                    return False
                continue
            # Real positive scene change.
            if before_scene is None or int(sid) != int(before_scene):
                if _scene_matches_layer(sid, layer):
                    _mark_scene_success(out, before_scene, sid, how=how)
                    notes.append(out["note"])
                    _log(log, "npc_service_mem portal_fn ok %s" % out["note"])
                    return True
                # Stable wrong layer → fail fast (e.g. deep wanted but landed upper)
                opp = "upper" if str(layer or "").lower() in (
                    "deep", "dungeon_deep", "dungeon_lower", "lower"
                ) or "深" in str(layer) else "deep"
                if _scene_matches_layer(sid, opp):
                    out["after_scene"] = sid
                    out["error"] = "wrong layer scene %s→%s want=%s via %s" % (
                        before_scene, sid, layer, how,
                    )
                    notes.append(out["error"])
                    _log(log, "npc_service_mem portal_fn %s" % out["error"])
                    return False
                notes.append(
                    "scene %s→%s off-band layer=%s (keep wait)" % (before_scene, sid, layer)
                )
            if sleep(0.25):
                out["error"] = "portal fn wait stopped"
                return False
        out["after_scene"] = last_sid
        if saw_transition:
            notes.append("wait timeout after transition raw_last=%s" % last_sid)
        return False

    def snapshot_panel() -> tuple[list[dict], dict, list[str]]:
        snap = dump_talk_proc(session, log=log)
        talk_rows = _option_catalog(snap) if snap and snap.ok else []
        host_d = dump_host_service_table(session, log=log)
        texts = collect_portal_menu_texts(session, log=log)
        cat = merge_catalog(talk_rows, host_d)
        # If host/talk catalog has no real L2, but panel texts clearly show both
        # 上层+深处 (true L2 panel / decoded labels empty), synthesize onto host rows.
        # Do NOT treat lone "西王母宫上层" task pollution as L2.
        has_l2_cat = any(
            any(k in str(r.get("label") or "") for k in ("上层", "深处", "下层"))
            for r in cat
        )
        text_blob = " ".join(str(x) for x in (texts or []))
        # True L2 panel must show BOTH sides. Entry+「西王母宫上层」task noise is L1.
        short = [str(x) for x in (texts or []) if x and len(str(x)) <= 14]
        short_u = sum(1 for x in short if "上层" in x and "地宫传送" not in x)
        short_d = sum(1 for x in short if ("深处" in x or "下层" in x))
        texts_look_l2 = bool(short_u and short_d)
        if not texts_look_l2:
            texts_look_l2 = (
                any(k in text_blob for k in ("深处", "下层"))
                and any(k in text_blob for k in ("上层",))
                and "地宫传送" not in text_blob
            )
        if (not has_l2_cat) and texts_look_l2:
            syn = synthesize_from_texts(host_d, texts)
            if syn and any(any(k in str(r.get("label") or "") for k in ("上层", "深处", "下层")) for r in syn):
                cat = list(syn) + [r for r in cat if "地宫传送" not in str(r.get("label") or "")]
                notes.append("synth L2 from texts labs=%s" % [r.get("label") for r in syn[:4]])
        # A task-free mouth is a direct two-row host menu.  Label decoding can
        # lose one row, but its host structure remains authoritative: two
        # non-magic, non-task rows and at least one visible layer label.
        if not has_layer_opts(cat):
            inferred = _infer_direct_layer_host_rows(host_d, texts)
            if inferred:
                cat = inferred
                notes.append(
                    "infer direct L2 host rows=%s"
                    % [r.get("index") for r in inferred]
                )
        elif not cat:
            syn = synthesize_from_texts(host_d, texts)
            if syn:
                cat = list(syn)
                notes.append("synth host from texts labs=%s" % [r.get("label") for r in syn[:4]])
        out["texts"] = texts
        out["host_poll"] = {
            "ok": host_d.get("ok"),
            "count": host_d.get("count"),
            "rows": [
                {"i": r.get("index"), "f0": r.get("field0"), "lab": r.get("label")}
                for r in (host_d.get("rows") or [])[:8]
            ],
        }
        if snap and getattr(snap, "dlg_ptr", 0):
            out["dlg"] = int(snap.dlg_ptr)
        return cat, host_d, texts

    if before_scene is None:
        before_scene = scene_now()
    out["before_scene"] = before_scene

    if sleep(max(0.05, float(settle_s))):
        out["error"] = "portal fn stopped"
        return out

    # ---- poll until L2 / L1 host / talk appears ----
    rows: list[dict] = []
    poll_plan = (0.0, 0.20, 0.40, 0.65, 0.95)
    generic_shell_polls = 0
    for pi, extra in enumerate(poll_plan):
        blocked, brsn = _pid_blocked(session)
        if blocked:
            out["error"] = brsn or "remote_blocked"
            out["ok"] = False
            notes.append(f"remote_blocked:{brsn}")
            return out
        if pi > 0 and sleep(float(extra)):
            out["error"] = "portal fn stopped"
            return out
        rows, host_i, texts_i = snapshot_panel()
        stage = "layer2" if has_layer_opts(rows) else (
            "layer1" if has_entry_opts(rows) else ("host" if rows else "none")
        )
        notes.append(
            "poll%d stage=%s n=%s labs=%s texts=%s"
            % (pi, stage, len(rows), [r.get("label") for r in rows[:6]], texts_i[:6])
        )
        _log(
            log,
            "npc_service_mem portal_fn poll%d stage=%s labs=%s texts=%s"
            % (pi, stage, [r.get("label") for r in rows[:6]], texts_i[:6]),
        )
        out["stage"] = stage
        out["options"] = rows
        if has_layer_opts(rows) or has_entry_opts(rows) or rows:
            break
        text_blob = " ".join(str(x) for x in (texts_i or []))
        generic_shell = bool(
            text_blob
            and ("地宫传送" in text_blob or ("地宫" in text_blob and "传送" in text_blob))
            and not any(k in text_blob for k in ("上层", "深处", "下层"))
        )
        generic_shell_polls = generic_shell_polls + 1 if generic_shell else 0
        if generic_shell_polls >= 2:
            out["early_reopen"] = True
            out["error"] = "no portal options; generic panel needs Hello reopen"
            notes.append("generic portal shell stable for 2 polls; request Hello reopen")
            _log(log, "npc_service_mem portal_fn generic shell; request early Hello reopen")
            return out

    # Cache is diagnostic / label hint only. NEVER drive AA5590 from cache alone
    # when live host/talk catalog is empty (tasks-done L2 vs task-only panels).
    if not rows and portal_tid:
        for st in ("layer2", "host", "layer1"):
            cached = _load_option_cache(int(portal_tid), st)
            if cached and any(str(r.get("label") or "").strip() for r in cached):
                notes.append(
                    "cache_ignored_live_empty stage=%s n=%s labs=%s"
                    % (st, len(cached), [r.get("label") for r in cached[:4]])
                )
                out["cache_hint"] = {
                    "stage": st,
                    "labs": [r.get("label") for r in cached[:6]],
                }
                break

    if not rows:
        texts = out.get("texts") or collect_portal_menu_texts(session, log=log)
        out["texts"] = texts
        # last chance: texts show L2 keywords even if host label decode failed
        if any(k in " ".join(texts) for k in ("上层", "深处", "下层")):
            notes.append("texts show layer but catalog empty — re-dump host")
            rows, _, _ = snapshot_panel()
        if not rows:
            if texts:
                out["error"] = (
                    "no portal options (need L2 上层/深处 or L1 地宫传送); texts=%s"
                    % texts[:8]
                )
            else:
                out["error"] = "panel empty after Hello (no host/talk options)"
            return out

    if portal_tid and rows:
        st = "layer2" if has_layer_opts(rows) else ("layer1" if has_entry_opts(rows) else "host")
        _save_option_cache(int(portal_tid), st, rows)

    layer_needles = _needles_for_layer(layer)
    entry_needles = _needles_for_entry(tier)

    # ========== 1) 二级优先：已有 上层/深处 → 只点二级 ==========
    dest = pick(rows, layer_needles) if has_layer_opts(rows) else None
    if dest is not None and not _l2_label_fits_tier(str(dest.get("label") or ""), tier):
        # In the task-complete state this is the only live menu: no L1 entry
        # exists to re-resolve the route.  The selected portal tid plus the
        # current two-row host panel are authoritative; validate the resulting
        # upper/deep scene instead of rejecting an older map-name heuristic.
        notes.append(
            "L2-first tier-name mismatch accepted tier=%s lab=%s"
            % (tier, dest.get("label"))
        )
        _log(
            log,
            "npc_service_mem portal_fn L2-first tier-name mismatch accepted "
            "tier=%s lab=%s"
            % (tier, dest.get("label")),
        )
    if dest is not None:
        notes.append(
            "L2-ready skip L1 dest=%s idx=%s kind=%s layer=%s needles=%s"
            % (
                dest.get("label"),
                dest.get("index"),
                dest.get("kind"),
                layer,
                layer_needles,
            )
        )
        _log(
            log,
            "npc_service_mem portal_fn L2-first dest=%s idx=%s layer=%s kind=%s"
            % (dest.get("label"), dest.get("index"), layer, dest.get("kind")),
        )
        ir = _invoke_portal_option(
            session,
            dest,
            dlg_ptr=int(out.get("dlg") or 0),
            hwnd=int(hwnd or 0),
            log=log,
        )
        out["dest_invoke"] = ir
        out["clicked"].append(
            "L2:%s:%s:idx=%s" % (dest.get("kind"), dest.get("label"), dest.get("index"))
        )
        notes.append("L2 invoke ok=%s note=%s" % (ir.get("ok"), ir.get("note") or ir.get("error")))
        if not ir.get("ok"):
            out["error"] = ir.get("error") or "L2 select failed"
            return out
        release_movement("portal_option_clicked")
        if sleep(0.25):
            out["error"] = "portal fn stopped after L2"
            return out
        if wait_scene_ok("L2:%s" % dest.get("label")):
            return out
        out["error"] = "L2 selected but scene unchanged lab=%s" % dest.get("label")
        return out

    # ========== 2) 只有一级：点 地宫传送 展开/直传，再找二级 ==========
    if not has_entry_opts(rows):
        out["error"] = (
            "no L2 (上层/深处) and no L1 (地宫传送); rows=%s"
            % [r.get("label") for r in rows[:8]]
        )
        return out

    entry = pick(rows, entry_needles) or pick(rows, ["地宫传送"])
    if entry is None:
        out["error"] = "L1 entry not found needles=%s rows=%s" % (
            entry_needles,
            [r.get("label") for r in rows[:8]],
        )
        return out

    entry_host = host_i
    entry_signature = _host_menu_signature(entry_host)

    notes.append(
        "L1-only → expand entry=%s kind=%s idx=%s (then L2)"
        % (entry.get("label"), entry.get("kind"), entry.get("index"))
    )
    _log(
        log,
        "npc_service_mem portal_fn L1 expand entry=%s" % entry.get("label"),
    )
    ir = _invoke_portal_option(
        session,
        entry,
        dlg_ptr=int(out.get("dlg") or 0),
        hwnd=int(hwnd or 0),
        log=log,
    )
    out["entry_invoke"] = ir
    out["clicked"].append(
        "L1:%s:%s:idx=%s" % (entry.get("kind"), entry.get("label"), entry.get("index"))
    )
    notes.append("L1 invoke ok=%s note=%s" % (ir.get("ok"), ir.get("note") or ir.get("error")))
    if not ir.get("ok"):
        out["error"] = ir.get("error") or "L1 entry select failed"
        return out

    # entry 可能直接进本（中级口 68→2032）
    # Second-click path: wait for L2 UI to rebuild (user: 要点选第二次要适当延迟)
    if sleep(0.70):
        out["error"] = "portal fn stopped after L1"
        return out
    sid1 = scene_now()
    if before_scene is not None and sid1 and sid1 != before_scene:
        if _scene_matches_layer(sid1, layer):
            return _mark_scene_success(
                out, before_scene, sid1, how="L1-direct:%s" % entry.get("label")
            )
        notes.append("L1 changed scene %s→%s off-band layer=%s" % (before_scene, sid1, layer))

    # poll for L2 after expand (longer cadence — UI host rebuild is slow)
    rows2: list[dict] = []
    host2: dict = {}
    for pi, extra in enumerate((0.0, 0.45, 0.65, 0.90, 1.10)):
        if pi > 0 and sleep(float(extra)):
            out["error"] = "portal fn stopped waiting L2"
            return out
        rows2, host2, texts2 = snapshot_panel()
        notes.append(
            "postL1 poll%d labs=%s host_sig=%s texts=%s"
            % (
                pi,
                [r.get("label") for r in rows2[:6]],
                _host_menu_signature(host2),
                texts2[:6],
            )
        )
        _log(
            log,
            "npc_service_mem portal_fn postL1 poll%d labs=%s host_sig=%s"
            % (
                pi,
                [r.get("label") for r in rows2[:6]],
                _host_menu_signature(host2),
            ),
        )
        if has_layer_opts(rows2):
            break
        # scene may change mid-poll
        sidp = scene_now()
        if before_scene is not None and sidp and sidp != before_scene and _scene_matches_layer(sidp, layer):
            return _mark_scene_success(
                out, before_scene, sidp, how="L1-direct-late:%s" % entry.get("label")
            )

    if portal_tid and rows2 and has_layer_opts(rows2):
        _save_option_cache(int(portal_tid), "layer2", rows2)

    dest2 = pick(rows2, layer_needles) if has_layer_opts(rows2) else None
    cached_dest = None
    if dest2 is None:
        cached_dest = _cached_layer_host_option(int(portal_tid or 0), layer)
        if _can_recover_cached_l2(entry_host, host2, cached_dest):
            dest2 = cached_dest
            out["cache_recovery"] = {
                "portal_tid": int(portal_tid or 0),
                "index": int(dest2.get("index") or 0),
                "label": dest2.get("label"),
                "before_host_sig": entry_signature,
                "after_host_sig": _host_menu_signature(host2),
            }
            notes.append(
                "L2 cache recovery tid=%s idx=%s lab=%s"
                % (portal_tid, dest2.get("index"), dest2.get("label"))
            )
            _log(
                log,
                "npc_service_mem portal_fn L2 cache recovery tid=%s idx=%s lab=%s"
                % (portal_tid, dest2.get("index"), dest2.get("label")),
            )
    if dest2 is not None and not _l2_label_fits_tier(str(dest2.get("label") or ""), tier):
        # L1 was selected by the live portal tid and host label immediately
        # before this panel appeared.  Its resulting map label is stronger
        # evidence than an old tier-to-map-name heuristic; scene verification
        # below still rejects the wrong upper/deep layer.
        notes.append(
            "L2 tier-name mismatch accepted after verified L1 tier=%s lab=%s"
            % (tier, dest2.get("label"))
        )
        _log(
            log,
            "npc_service_mem portal_fn L2 tier-name mismatch accepted after L1 "
            "tier=%s lab=%s"
            % (tier, dest2.get("label")),
        )
    if dest2 is None:
        # 没有二级面板：若 entry 已进对的 layer 场景已处理；否则失败
        out["options2"] = rows2
        out["error"] = (
            "L1 clicked but L2 (上层/深处) not opened; rows2=%s texts=%s"
            % ([r.get("label") for r in rows2[:8]], (out.get("texts") or [])[:8])
        )
        return out

    notes.append("L2 after expand dest=%s idx=%s" % (dest2.get("label"), dest2.get("index")))
    ir2 = _invoke_portal_option(
        session,
        dest2,
        dlg_ptr=int(out.get("dlg") or 0),
        hwnd=int(hwnd or 0),
        log=log,
    )
    out["dest_invoke"] = ir2
    out["clicked"].append(
        "L2:%s:%s:idx=%s" % (dest2.get("kind"), dest2.get("label"), dest2.get("index"))
    )
    notes.append("L2 invoke ok=%s note=%s" % (ir2.get("ok"), ir2.get("note") or ir2.get("error")))
    if not ir2.get("ok"):
        out["error"] = ir2.get("error") or "L2 select after expand failed"
        return out
    release_movement("portal_option_clicked")
    # brief settle after second click before scene poll
    if sleep(0.35):
        out["error"] = "portal fn stopped after L2"
        return out
    if wait_scene_ok("L2:%s" % dest2.get("label")):
        return out
    out["error"] = "L2 selected but scene unchanged lab=%s" % dest2.get("label")
    return out



def describe_re() -> dict:
    return {
        "select_fn": hex(VA_NPC_TALK_SELECT),
        "open_content_fn": hex(VA_NPC_CONTENT_OPEN),
        "rebuild_fn": hex(VA_NPC_CONTENT_REBUILD),
        "refresh_fn": hex(VA_NPC_UI_REFRESH),
        "globals": {
            "host": hex(VA_GLOBAL_HOSTISH),
            "txt_content": hex(VA_GLOBAL_LISTMAN),
            "sub_list": hex(VA_GLOBAL_SUB_LIST),
        },
        "host": {
            "table": hex(OFF_HOST_TABLE),
            "count": hex(OFF_HOST_COUNT),
            "row_stride": hex(HOST_ROW_STRIDE),
            "get_field": hex(VA_HOST_GET_FIELD),
            "get_row": hex(VA_HOST_GET_ROW),
            "msg_build": hex(VA_MSG_BUILD),
            "msg_send": hex(VA_MSG_SEND),
        },
        "talk_proc_prop": "ptr_talk_proc",
        "talk_proc_layout": {
            "count": hex(OFF_TP_COUNT),
            "array": hex(OFF_TP_ARRAY),
            "entry_stride": hex(TP_ENTRY_STRIDE),
            "sub_stride": hex(TP_SUB_STRIDE),
        },
        "event_list_select": hex(0x80000011),
        "missing_free_enter": True,
        "aa3330_default": False,
        "legal_chain": (
            "Hello -> talk_proc opt_id -> bridge AA3330(type2/4) -> "
            "if branch: host table fill -> host row activate (AE7C00/66D280) "
            "or leaf talk_proc -> scene change"
        ),
        "host_select": "bridge CMD_NPC_HOST_SELECT -> cdecl AA5590(index,0)",
        "experimental": "L1 unfinished tasks: talk_proc=0, host Txt_Template labels + AA5590; may enter deep directly",
    }
