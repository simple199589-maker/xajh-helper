# -*- coding: utf-8 -*-
"""
Task list / accept / complete for xajh (no packets).

Read accepted tasks via plg::GetTaskInterface + list thiscall (note VA).
Accept/complete via bridge main-thread cmds (pattern-resolved when possible).

Success for accept/complete is list diff, not call return alone.

Anchors (2026-07-16 xajh.exe preferred base 0x400000):
  GetTaskInterface export
  list thiscall @ 0x493360 (ecx = *[iface+0x30])
  accept body @ 0xCF0550 (this = *[game_root+0x2C])
  complete site @ 0xAB9B40 family (needs NPC; bridge later)

@author by ak
"""
from __future__ import annotations

import ctypes
import math
import re
import struct
import threading
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from app.core.remote_runtime import (
    _open_process,
    _rpm,
    _wpm,
    remote_call_cdecl_x86,
    remote_call_thiscall_x86,
)
from app.core.client_build import profile_for_client, session_supports
from app.core.plg_exports import (
    EXPORT_GET_TASK_INTERFACE,
    NOTE_VA_GET_TASK_NAME_C1,
    NOTE_VA_GET_TASK_NAME_C2,
    NOTE_VA_TASK_ACCEPT,
    NOTE_VA_TASK_CAN_FINISH,
    NOTE_VA_TASK_COMPLETE,
    NOTE_VA_TASK_LIST_ACCEPTED,
    TASK_DESC_AWARD_NPC_OFF,
    TASK_DESC_DELV_NPC_OFF,
    TASK_DESC_REACH_MAX_X_OFF,
    TASK_DESC_REACH_MAX_Y_OFF,
    TASK_DESC_REACH_MAX_Z_OFF,
    TASK_DESC_REACH_MIN_X_OFF,
    TASK_DESC_REACH_MIN_Y_OFF,
    TASK_DESC_REACH_MIN_Z_OFF,
    TASK_DESC_REACH_SCENE_OFF,
    TASK_DESC_REACH_WORLD_OFF,
    TASK_ENTRY_ID_OFF,
    TASK_ENTRY_PROGRESS_OFF,
    TASK_ENTRY_STATE_OFF,
    TASK_ENTRY_STRIDE,
    TASK_ENTRY_TMPL_PTR_OFF,
    TASK_IFACE_LIST_THIS_OFF,
    TASK_MGR_THIS_OFF,
    TASK_NAME_WSTR_OFF,
    TASK_NAME_WSTR_PTR_OFF,
    TASK_DESC_PANEL_TITLE_OFF,
    TASK_DESC_PANEL_TITLE_REAR_PTR_OFF,
    TASK_DESC_STORY_WSTR_PTR_OFF,
    TASK_DESC_OBJECTIVE_WSTR_PTR_OFF,
    TASK_STATE_FINISHED,
    TASK_STATE_SUCCESS,
    find_xajh_exe,
    note_va_to_live,
    resolve_export_rva,
)
from app.core.symbol_resolver import PatternResolver

LogFn = Callable[[str], None]

PORTAL_DIALOG_POST_ARRIVAL_S = 0.75

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_EXECUTE_READWRITE = 0x40
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF

# jieTask long pattern (still hits current client)
_PAT_JIE_TASK = (
    "8B4C24508B5424488B454C518B4D4453528B54245050A1????????"
    "518B48??52E8????????5E5F5BB0015D83C430C3"
)
_PAT_WAN_CHENG = (
    "8B81????????8B15????????8B0D????????535750A1????????"
    "528B15????????505152E8????????"
)


@dataclass
class TaskInfo:
    """One accepted (or available) task row. @author by ak"""

    task_id: int
    name: str = ""
    progress: int | None = None
    addr: int = 0
    kind: str = "accepted"  # accepted | available
    state: int = 0  # TaskState mask at entry+0x27
    is_finished: bool = False  # TASK_STATE_FINISHED bit
    is_success: bool = False  # TASK_STATE_SUCCESS bit (raw)
    can_finish: bool = False  # native CanFinish (UI 可交 / turn-in)
    status_text: str = ""  # Chinese short status

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_task_name(name: str) -> str:
    """Strip level tags / 限次 suffix; keep concrete titles for family match.

    Panel form ``[140]<每日BOSS>东方不败`` normalizes to ``东方不败`` (specific),
    not the generic category ``每日BOSS``. Using only the category would make
    every daily boss look like one family, so after accepting any one the other
    bosses disappear from nearby 可接 and show as 进行中.

    ``每日杀怪-限1次`` still matches ``[140]<每日杀怪>140每日杀怪`` via
    digit-prefix + 限次 strip → both become ``每日杀怪``.
    """
    s = str(name or "").strip()
    if not s:
        return ""
    # Prefer concrete title after last '>' (panel form <分类>具体名).
    if ">" in s:
        tail = s.rsplit(">", 1)[-1].strip()
        if tail:
            s = tail
        else:
            m = re.search(r"<([^>]+)>", s)
            if m and m.group(1).strip():
                s = m.group(1).strip()
    else:
        m = re.search(r"<([^>]+)>", s)
        if m and m.group(1).strip():
            s = m.group(1).strip()
    s = re.sub(r"^\[\d+\]", "", s)
    s = re.sub(r"<\d+>", "", s)
    s = re.sub(r"^\d+", "", s)
    s = re.sub(r"[-_]?限\d+次", "", s)
    s = re.sub(r"\s+", "", s)
    return s


def task_names_match(a: str, b: str) -> bool:
    """True if display names refer to the same quest family."""
    na, nb = normalize_task_name(a), normalize_task_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # shorter base contained in longer (杀怪 vs 每日杀怪)
    if len(na) >= 2 and len(nb) >= 2 and (na in nb or nb in na):
        return True
    return False


def format_task_display_name(name: str, *, task_id: int = 0) -> str:
    """
    Readable task title for GUI lists, matching in-game panel style.

    Template forms often look like:
      ``[140]<每日杀怪>140每日杀怪``
      ``140 <每日杀怪>140每日杀怪``
    Prefer the trailing concrete title (``140每日杀怪``), not the generic
    middle token (``每日杀怪``) — that loses level / location hint.
    @author by ak
    """
    s = str(name or "").strip()
    if not s:
        return f"任务{int(task_id)}" if task_id else ""
    # Prefer text after last '>' (e.g. "...>140每日杀怪")
    if ">" in s:
        tail = s.rsplit(">", 1)[-1].strip()
        if tail:
            s = tail
    else:
        s = re.sub(r"^\[\d+\]", "", s).strip()
        # Only strip a leading bare level when followed by space / angle tag.
        s = re.sub(r"^\d+\s+", "", s).strip()
        s = re.sub(r"^\d+<", "<", s).strip()
        if s.startswith("<") and ">" in s:
            # rare: only angle form left
            m = re.search(r"<([^>]+)>", s)
            if m and m.group(1).strip():
                s = m.group(1).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s or (f"任务{int(task_id)}" if task_id else "")


def _extract_dungeon_place(text: str) -> str:
    """Extract 初级/中级/高级地宫(+层/深处) from story text. @author by ak"""
    s = str(text or "")
    m = re.search(
        r"((?:初级|中级|高级|升级)地宫(?:一层|二层|三层|深处)?)",
        s,
    )
    return m.group(1) if m else ""


def _extract_boss_name(text: str) -> str:
    """
    Extract boss-like subject from story text.

    Examples: 东方不败 / 龙傲天 / 中级地宫深处BOSS
    @author by ak
    """
    s = re.sub(r"\s+", "", str(text or ""))
    if not s:
        return ""
    m = re.search(r"击杀(.+?)(?:。|$)", s)
    if m and 2 <= len(m.group(1)) <= 16:
        return m.group(1)
    # Prefer short subject right before 在X级地宫 (avoid long filler prefixes).
    m = re.search(
        r"的([一-鿿A-Za-z0-9]{2,12})在(?:初级|中级|高级|升级)?地宫",
        s,
    )
    if m:
        return m.group(1)
    m = re.search(
        r"([一-鿿A-Za-z0-9]{2,12})在(?:初级|中级|高级|升级)?地宫",
        s,
    )
    if m:
        return m.group(1)
    place = _extract_dungeon_place(s)
    if place and "BOSS" in s.upper():
        return f"{place}BOSS"
    return ""


def _short_task_detail(text: str, *, max_len: int = 18) -> str:
    """
    Compress story/objective text into a short list suffix.

    Prefer concrete place/monster phrases over full prose.
    @author by ak
    """
    s = re.sub(r"\s+", "", str(text or "").strip())
    if not s:
        return ""
    s = re.sub(r"[。．.]*每日限\d+次$", "", s)
    s = s.rstrip("。．.")
    place = _extract_dungeon_place(s)
    if place:
        return place
    m = re.search(r"(?:去)?(\d+副本)", s)
    if m:
        return m.group(1)
    if "天下会" in s:
        return "天下会"
    m = re.search(r"击杀\d+只?(.+)$", s)
    if m and 2 <= len(m.group(1)) <= max_len:
        return m.group(1)
    m = re.search(r"干掉\d+个?(.+)$", s)
    if m and 2 <= len(m.group(1)) <= max_len:
        return m.group(1)
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def build_task_display_name(
    base_name: str,
    *,
    task_id: int = 0,
    panel_title: str = "",
    panel_rear: str = "",
    objective: str = "",
    story: str = "",
    level: int | str | None = None,
) -> str:
    """
    Build list title matching the in-game task panel.

    Live layout (2026-07-19) + panel compose:
      desc+0x08  specific title (初级地宫杀怪 / 140每日杀怪 / 东方不败)
      desc+0xA88 rear (often empty)
      desc+0xA98 category (每日杀怪 / 每日BOSS)
    Game list form: ``[140]<每日杀怪>初级地宫杀怪`` — we emit
    ``<每日杀怪>初级地宫杀怪`` (and ``[level]`` when known).
    Story/objective are detail only, not list title.
    @author by ak
    """
    _ = objective, story  # kept for call-site compatibility; not used in list title
    category = format_task_display_name(base_name, task_id=task_id)
    front = re.sub(r"\s+", " ", str(panel_title or "").strip())
    rear = re.sub(r"\s+", " ", str(panel_rear or "").strip())
    specific = f"{front}{rear}".strip() if (front or rear) else ""
    # Prefer in-game ``<分类>具体名``
    if specific and category and specific != category:
        title = f"<{category}>{specific}"
    elif specific:
        title = specific
    elif category:
        title = category
    else:
        title = f"任务{int(task_id)}" if task_id else ""
    if not title:
        return ""
    try:
        lv = int(level) if level is not None and str(level).strip() != "" else 0
    except (TypeError, ValueError):
        lv = 0
    if lv > 0 and not title.startswith("["):
        return f"[{lv}]{title}"
    return title


def is_placeholder_npc_name(name: str) -> bool:
    """True when name is empty or a raw tidXXXX placeholder. @author by ak"""
    s = str(name or "").strip()
    if not s:
        return True
    return bool(re.fullmatch(r"tid\d+", s, flags=re.IGNORECASE))


def task_status_from_state(
    state: int,
    progress: int | None = None,
    *,
    can_finish: bool | None = None,
) -> str:
    """
    Map TaskState / CanFinish to Chinese status.

    可交 follows native CanFinish (not SUCCESS bit alone — live all rows may
    have SUCCESS while only a few CanFinish).
    @author by ak
    """
    if can_finish is True:
        return "可交"
    st = int(state) & 0xFFFFFFFF
    if can_finish is None and (st & TASK_STATE_SUCCESS) and (st & TASK_STATE_FINISHED):
        # fallback without CanFinish call: both bits
        return "可交"
    if st & TASK_STATE_FINISHED:
        return "已完成"
    if progress is not None and int(progress) > 0:
        return f"进行中({int(progress)})"
    return "进行中"


@dataclass
class TaskOpResult:
    """Outcome of accept/complete with list evidence. @author by ak"""

    ok: bool
    action: str
    task_id: int
    before_ids: list[int] = field(default_factory=list)
    after_ids: list[int] = field(default_factory=list)
    ret: int | None = None
    error: str | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _pat_to_bytes(pat: str) -> bytes:
    """Convert hex pattern with ?? to regex-friendly bytes for find. @author by ak"""
    s = pat.upper().replace(" ", "")
    out = bytearray()
    i = 0
    while i < len(s):
        if s[i : i + 2] == "??":
            out.append(0)  # placeholder; real match uses mask
            i += 2
        else:
            out.append(int(s[i : i + 2], 16))
            i += 2
    return bytes(out)


def _find_pattern(data: bytes, pat: str) -> list[int]:
    """Return file offsets of wildcard hex pattern. @author by ak"""
    s = pat.upper().replace(" ", "")
    parts: list[tuple[int | None, int]] = []
    i = 0
    while i < len(s):
        if s[i : i + 2] == "??":
            parts.append((None, 1))
            i += 2
        else:
            parts.append((int(s[i : i + 2], 16), 1))
            i += 2
    plen = sum(p[1] for p in parts)
    hits: list[int] = []
    n = len(data)
    for off in range(0, n - plen + 1):
        ok = True
        pos = off
        for val, ln in parts:
            if val is not None and data[pos] != val:
                ok = False
                break
            pos += ln
        if ok:
            hits.append(off)
    return hits


def _pe_sections(data: bytes) -> list[tuple[int, int, int, int]]:
    """Return list of (va, raw, rsz, vsz) for PE32. @author by ak"""
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    size_opt = struct.unpack_from("<H", data, e_lfanew + 20)[0]
    num_sec = struct.unpack_from("<H", data, e_lfanew + 6)[0]
    sec_off = e_lfanew + 24 + size_opt
    out = []
    for i in range(num_sec):
        o = sec_off + i * 40
        vsz, va, rsz, raw = struct.unpack_from("<IIII", data, o + 8)
        out.append((va, raw, rsz, vsz))
    return out


def _file_off_to_rva(data: bytes, off: int) -> int | None:
    """Map file offset to RVA. @author by ak"""
    for va, raw, rsz, vsz in _pe_sections(data):
        if raw <= off < raw + rsz:
            return va + (off - raw)
    return None


def resolve_task_patterns(pe_path: str | Path | None = None) -> dict:
    """
    Resolve task-related RVAs from PE (patterns + export).

    Returns dict with keys:
      get_task_iface_rva, accept_call_rva, accept_site_rva,
      complete_call_rva, list_accepted_rva, task_mgr_this_off, ok, errors
    @author by ak
    """
    pe = Path(pe_path) if pe_path else find_xajh_exe()
    out: dict = {
        "pe": str(pe) if pe else "",
        "get_task_iface_rva": None,
        "accept_call_rva": None,
        "accept_site_rva": None,
        "complete_call_rva": None,
        "list_accepted_rva": None,
        "task_mgr_this_off": TASK_MGR_THIS_OFF,
        "ok": False,
        "errors": [],
    }
    if pe is None or not pe.is_file():
        out["errors"].append("xajh.exe not found")
        return out
    data = pe.read_bytes()
    resolver = PatternResolver(pe)
    exact_profile = profile_for_client(str(pe))

    def exact_rva(key: str) -> int | None:
        entry = exact_profile.symbols.get(key) if exact_profile else None
        if not isinstance(entry, dict) or entry.get("status") not in {
            "verified",
            "experimental",
        }:
            return None
        try:
            return int(str(entry["rva"]), 0)
        except (KeyError, TypeError, ValueError):
            return None

    out["list_accepted_rva"] = exact_rva("task_list_accepted")
    if out["list_accepted_rva"] is None:
        out["errors"].append("accepted-list RVA unavailable for unknown build")
    rva = resolve_export_rva(pe, EXPORT_GET_TASK_INTERFACE)
    if rva is not None:
        out["get_task_iface_rva"] = int(rva)
    else:
        out["errors"].append("GetTaskInterface export missing")

    # accept: pattern → call target
    accept_hits = resolver.find_rvas(_PAT_JIE_TASK)
    if len(accept_hits) == 1:
        site_rva = accept_hits[0]
        site_off = resolver.rva_to_offset(site_rva)
        out["accept_site_rva"] = site_rva
        # find E8 after A1...52
        window = resolver.read_at_rva(site_rva, 64)
        e8 = window.find(b"\xE8")
        # prefer the E8 after the 52 push (second half)
        idx = 0
        call_rva = None
        while True:
            e8 = window.find(b"\xE8", idx)
            if e8 < 0:
                break
            rel = struct.unpack_from("<i", window, e8 + 1)[0]
            if site_rva is not None:
                call_site_rva = site_rva + e8
                call_rva = (call_site_rva + 5 + rel) & 0xFFFFFFFF
            # also parse this off from 8B 48 xx after A1
            idx = e8 + 1
        if call_rva is not None:
            out["accept_call_rva"] = call_rva
        # this displacement
        a1 = window.find(b"\xA1")
        if a1 >= 0 and a1 + 7 < len(window) and window[a1 + 5] == 0x51:
            # 51 8B 48 xx
            if window[a1 + 6] == 0x8B and window[a1 + 7] == 0x48:
                out["task_mgr_this_off"] = int(window[a1 + 8])
    else:
        out["accept_call_rva"] = exact_rva("task_accept")
        reason = "miss" if not accept_hits else f"ambiguous({len(accept_hits)})"
        if out["accept_call_rva"] is not None:
            out["errors"].append(
                f"jieTask pattern {reason}; using exact-build verified profile"
            )
        else:
            out["errors"].append(f"jieTask pattern {reason}; action disabled")

    complete_hits = resolver.find_rvas(_PAT_WAN_CHENG)
    if len(complete_hits) == 1:
        site_rva = complete_hits[0]
        window = resolver.read_at_rva(site_rva, 64)
        e8 = window.rfind(b"\xE8")
        if e8 >= 0 and site_rva is not None:
            rel = struct.unpack_from("<i", window, e8 + 1)[0]
            out["complete_call_rva"] = (site_rva + e8 + 5 + rel) & 0xFFFFFFFF
    else:
        out["complete_call_rva"] = exact_rva("task_complete")
        reason = "miss" if not complete_hits else f"ambiguous({len(complete_hits)})"
        if out["complete_call_rva"] is not None:
            out["errors"].append(
                f"WanCheng pattern {reason}; using exact-build verified profile"
            )
        else:
            out["errors"].append(f"WanCheng pattern {reason}; action disabled")

    out["ok"] = (
        out["get_task_iface_rva"] is not None
        and out["list_accepted_rva"] is not None
    )
    return out


def _remote_thiscall_eax(
    pid: int,
    func_va: int,
    this_ptr: int,
    args: list[int] | None = None,
    *,
    timeout_ms: int = 4000,
) -> int:
    """
    Remote x86 thiscall: ECX=this, stack args right-to-left; return EAX.

    @author by ak
    """
    return remote_call_thiscall_x86(
        int(pid),
        int(func_va),
        int(this_ptr),
        list(args or []),
        caller_cleanup=False,
        timeout_ms=int(timeout_ms),
    )


def get_task_interface_ptr(session, *, log: LogFn | None = None) -> int:
    """
    Call plg::GetTaskInterface() -> CECTaskInterface*.

    @author by ak
    """
    log = log or (lambda _m: None)
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        raise RuntimeError("session has no module_base")
    pe = find_xajh_exe(getattr(session, "exe_path", None))
    if pe is None:
        raise RuntimeError("xajh.exe not found")
    rva = resolve_export_rva(pe, EXPORT_GET_TASK_INTERFACE)
    if rva is None:
        raise RuntimeError("GetTaskInterface export missing")
    va = base + int(rva)
    pid = int(session.pid)
    ptr = remote_call_cdecl_x86(pid, va, [], timeout_ms=3000)
    log(f"task_api GetTaskInterface va=0x{va:X} -> 0x{ptr & 0xFFFFFFFF:X}")
    return int(ptr) & 0xFFFFFFFF


def _read_wstr_h(h, addr: int, max_chars: int = 64) -> str:
    """Read UTF-16LE C string via process handle. @author by ak"""
    addr = int(addr) & 0xFFFFFFFF
    if not addr or addr < 0x10000:
        return ""
    try:
        raw = _rpm(h, addr, max_chars * 2)
    except OSError:
        return ""
    out: list[str] = []
    for i in range(0, len(raw) - 1, 2):
        ch = struct.unpack_from("<H", raw, i)[0]
        if ch == 0:
            break
        if ch < 32 and ch not in (9, 10, 13):
            break
        try:
            out.append(chr(ch))
        except Exception:
            break
    return "".join(out)


def _read_desc_wstr_field(h, desc: int, off: int, *, max_chars: int = 80) -> str:
    """Read wchar* field at desc+off. @author by ak"""
    desc_u = int(desc) & 0xFFFFFFFF
    if not desc_u:
        return ""
    try:
        p_raw = _rpm(h, desc_u + int(off), 4)
        p = struct.unpack("<I", p_raw)[0]
    except OSError:
        return ""
    return _read_wstr_h(h, p, max_chars) if p else ""


def get_task_texts(
    session,
    task_id: int,
    *,
    log: LogFn | None = None,
) -> dict:
    """
    Read task panel title fields from static desc.

    Live (2026-07-19):
      +0x08  panel front (inline wchar) e.g. 初级地宫杀怪 / 140每日杀怪
      +0xA88 panel rear wchar* (often empty)
      +0xA98 category GetTaskName e.g. 每日杀怪
      +0xA9C story, +0xAA0 objective (detail only, not list title)
    @author by ak
    """
    log = log or (lambda _m: None)
    out = {
        "name": "",
        "panel_title": "",
        "panel_rear": "",
        "story": "",
        "objective": "",
        "display": "",
        "desc": 0,
    }
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        return out
    if not session_supports(session, "task.read"):
        log("task_api GetTaskTexts blocked: unknown client build")
        return out
    tid = int(task_id) & 0xFFFFFFFF
    if not tid:
        return out
    pid = int(session.pid)
    c1 = note_va_to_live(base, NOTE_VA_GET_TASK_NAME_C1)
    c2 = note_va_to_live(base, NOTE_VA_GET_TASK_NAME_C2)
    try:
        ctx = remote_call_cdecl_x86(pid, c1, [], timeout_ms=2000)
        ctx_u = int(ctx) & 0xFFFFFFFF
        if not ctx_u:
            log(f"task_api GetTaskTexts id={tid} ctx=null")
            return out
        desc = _remote_thiscall_eax(pid, c2, ctx_u, [tid], timeout_ms=2000)
        desc_u = int(desc) & 0xFFFFFFFF
        if not desc_u:
            log(f"task_api GetTaskTexts id={tid} desc=null")
            return out
        out["desc"] = desc_u
        h = _open_process(pid)
        try:
            # Panel front: inline wchar at desc+8 (not a pointer).
            panel = _read_wstr_h(h, desc_u + TASK_DESC_PANEL_TITLE_OFF, 32)
            rear = _read_desc_wstr_field(
                h, desc_u, TASK_DESC_PANEL_TITLE_REAR_PTR_OFF, max_chars=32
            )
            name = _read_desc_wstr_field(h, desc_u, TASK_NAME_WSTR_PTR_OFF)
            if not name:
                name = _read_wstr_h(h, desc_u + TASK_NAME_WSTR_OFF, 64)
            if not name:
                name = _read_wstr_h(h, desc_u, 64)
            story = _read_desc_wstr_field(
                h, desc_u, TASK_DESC_STORY_WSTR_PTR_OFF, max_chars=96
            )
            objective = _read_desc_wstr_field(
                h, desc_u, TASK_DESC_OBJECTIVE_WSTR_PTR_OFF, max_chars=96
            )
            out["name"] = name
            out["panel_title"] = panel
            out["panel_rear"] = rear
            out["story"] = story
            out["objective"] = objective
            out["display"] = build_task_display_name(
                name,
                task_id=tid,
                panel_title=panel,
                panel_rear=rear,
            )
            log(
                f"task_api GetTaskTexts id={tid} panel={panel!r} "
                f"cat={name!r} disp={out['display']!r}"
            )
            return out
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h))
    except Exception as e:
        log(f"task_api GetTaskTexts id={tid} fail: {e}")
        return out


def get_task_name(
    session,
    task_id: int,
    *,
    log: LogFn | None = None,
) -> str:
    """
    Resolve task list title by id (in-game panel style).

    Uses desc+8 panel front + GetTaskName category; not story text.
    @author by ak
    """
    texts = get_task_texts(session, task_id, log=log)
    return str(texts.get("display") or texts.get("panel_title") or texts.get("name") or "")


def task_can_finish(
    session,
    task_id: int,
    *,
    iface: int | None = None,
    log: LogFn | None = None,
) -> bool:
    """
    Native CanFinish(taskId) via thiscall @ NOTE_VA_TASK_CAN_FINISH (0xC34E90).

    ecx = GetTaskInterface() result; stack arg = task_id; returns AL bool.
    Walks accepted list at [iface+4], finds id, then award/check logic.
    Live: true only for turn-inable accepted tasks (not SUCCESS bit alone).

    @author by ak
    """
    log = log or (lambda _m: None)
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        return False
    if not session_supports(session, "task.read"):
        log("task_api CanFinish blocked: unknown client build")
        return False
    tid = int(task_id) & 0xFFFFFFFF
    if not tid:
        return False
    pid = int(session.pid)
    try:
        this_ptr = int(iface) & 0xFFFFFFFF if iface else get_task_interface_ptr(
            session, log=log
        )
        if not this_ptr:
            return False
        va = note_va_to_live(base, NOTE_VA_TASK_CAN_FINISH)
        ret = _remote_thiscall_eax(pid, va, this_ptr, [tid], timeout_ms=2500)
        # AL only; high garbage from remote thread is common
        ok = bool(int(ret) & 0xFF)
        log(
            f"task_api CanFinish id={tid} va=0x{va:X} "
            f"ret=0x{int(ret) & 0xFFFFFFFF:X} ok={ok}"
        )
        return ok
    except Exception as e:
        log(f"task_api CanFinish id={tid} fail: {e}")
        return False


def list_accepted_tasks(
    session,
    *,
    log: LogFn | None = None,
    max_entries: int = 128,
    resolve_names: bool = True,
    resolve_can_finish: bool = True,
    quiet: bool = False,
) -> list[TaskInfo]:
    """
    Enumerate accepted (in-progress) tasks.

    Chain: GetTaskInterface -> list thiscall @note
    -> byte count at base, stride 0x7e, id at +0x1f, TaskState at +0x27.
    Names via GetTaskName(id). 可交 via native CanFinish(id).

    For accept/complete list-diff polls use resolve_names=False and
    resolve_can_finish=False (memory-only, ~ms instead of multi-second CRT).
    """
    log = log or (lambda _m: None)
    detail_log = log if not quiet else (lambda _m: None)
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        raise RuntimeError("session has no module_base")
    if not session_supports(session, "task.read"):
        log("task_api list_accepted blocked: unknown client build")
        return []
    pid = int(session.pid)
    iface = get_task_interface_ptr(session, log=detail_log)
    if not iface:
        log("task_api list_accepted: iface null")
        return []

    # GetTaskInterface already returns the +0x30 list this (export body).
    list_this = iface
    list_va = note_va_to_live(base, NOTE_VA_TASK_LIST_ACCEPTED)
    arr = _remote_thiscall_eax(pid, list_va, list_this, [], timeout_ms=4000)
    arr_u = int(arr) & 0xFFFFFFFF
    if not arr_u:
        log("task_api list_accepted: array null")
        return []

    h = _open_process(pid)
    try:
        num_b = _rpm(h, arr_u, 1)[0]
        if num_b > max_entries:
            log(f"task_api list_accepted: num={num_b} clamped/skip")
            return []
        out: list[TaskInfo] = []
        for i in range(int(num_b)):
            ent = arr_u + i * TASK_ENTRY_STRIDE
            try:
                tid_raw = _rpm(h, ent + TASK_ENTRY_ID_OFF, 4)
                prog_raw = _rpm(h, ent + TASK_ENTRY_PROGRESS_OFF, 2)
                state_raw = _rpm(h, ent + TASK_ENTRY_STATE_OFF, 4)
            except OSError:
                continue
            task_id = struct.unpack("<I", tid_raw)[0]
            progress = struct.unpack("<H", prog_raw)[0]
            state = struct.unpack("<I", state_raw)[0]
            if task_id == 0:
                continue
            is_fin = bool(state & TASK_STATE_FINISHED)
            is_ok = bool(state & TASK_STATE_SUCCESS)
            out.append(
                TaskInfo(
                    task_id=int(task_id),
                    progress=int(progress),
                    addr=int(ent),
                    kind="accepted",
                    state=int(state),
                    is_finished=is_fin,
                    is_success=is_ok,
                    can_finish=False,
                    status_text="",
                )
            )
        if not quiet:
            log(f"task_api list_accepted count={len(out)} num={num_b}")
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h))

    if resolve_can_finish:
        for t in out:
            try:
                t.can_finish = task_can_finish(
                    session, t.task_id, iface=iface, log=detail_log
                )
            except Exception as e:
                log(f"task_api CanFinish id={t.task_id}: {e}")
                t.can_finish = False
            t.status_text = task_status_from_state(
                t.state, t.progress, can_finish=t.can_finish
            )
    else:
        for t in out:
            t.status_text = task_status_from_state(
                t.state, t.progress, can_finish=None
            )

    if resolve_names and out:
        for t in out:
            try:
                nm = get_task_name(session, t.task_id, log=detail_log)
                if nm:
                    t.name = nm
            except Exception as e:
                log(f"task_api name id={t.task_id}: {e}")
        if not quiet:
            named = sum(1 for t in out if t.name)
            log(f"task_api list_accepted names={named}/{len(out)}")

    # 可交优先，便于对照游戏内任务栏
    out.sort(
        key=lambda t: (
            0 if t.can_finish else 1,
            0 if t.is_finished else 1,
            int(t.task_id),
        )
    )
    if resolve_can_finish and not quiet:
        ok_n = sum(1 for t in out if t.can_finish)
        if ok_n:
            names = ", ".join(
                (t.name or str(t.task_id)) for t in out if t.can_finish
            )
            log(f"task_api list_accepted can_turnin={ok_n}: {names}")
        else:
            log("task_api list_accepted can_turnin=0")
    return out


def list_accepted_task_ids(
    session,
    *,
    log: LogFn | None = None,
) -> list[int]:
    """Fast accepted-id snapshot (memory only, no CanFinish/GetTaskName CRT)."""
    rows = list_accepted_tasks(
        session,
        log=log,
        resolve_names=False,
        resolve_can_finish=False,
        quiet=True,
    )
    return [int(t.task_id) for t in rows]


# Full quest-config table (black-shield style enumeration, verified live 2026-08-06).
TASK_CFG_CTX_OFF = 0x1F8        # *( *(0x015282D8) + 0x1F8 ) = task config context
TASK_CFG_MAP_OFF = 0x28         # ctx+0x28 = hash map: task_id -> desc
TASK_CFG_BUCKET_BEGIN_OFF = 0x14
TASK_CFG_BUCKET_END_OFF = 0x18
TASK_CFG_NODE_NEXT_OFF = 0x00
TASK_CFG_NODE_ID_OFF = 0x08
TASK_CFG_NODE_DESC_OFF = 0x0C
TASK_CFG_DESC_NAME_OFF = 0x08   # inline wchar task name

# Accepted-task list pure-memory chain (verified live 2026-08-06).
TASK_IFACE_ROOT_OBJ_OFF = 0x24   # *(root+0x24)
TASK_IFACE_X_OFF = 0x90          # *(obj+0x90)
TASK_IFACE_OFF = 0x30            # *(x+0x30)
TASK_IFACE_ARR_OFF = 0x04        # *(iface+4) = accepted array
TASK_ENTRY_STRIDE = 0x7E
TASK_ENTRY_ID_OFF = 0x1F
TASK_ENTRY_STATE_OFF = 0x27

# Dev/test quest names to exclude from "all tasks" listing.
TASK_TEST_NAME_PATTERNS = (
    "测试", "张羽", "test", "TEST", "Debug", "debug", "调试",
    "子任务", "子1", "子2", "AwardMail",
)


def _accepted_task_ids_mem(session, *, log: LogFn | None = None) -> list[int]:
    """
    Read accepted task IDs straight from memory (no remote calls / no hooks).

    Chain (verified live 2026-08-06):
      root = *(0x015282D8)
      obj  = *(root + 0x24)
      x    = *(obj + 0x90)
      iface= *(x + 0x30)
      arr  = *(iface + 0x04)          # accepted array
      count = byte at arr[0]; entries at arr + i*0x7E; id @ +0x1F, state @ +0x27

    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(session.pid)
    h = _open_process(pid)
    try:
        def u32a(addr: int) -> int:
            try:
                return struct.unpack("<I", _rpm(h, addr, 4))[0]
            except Exception:
                return 0

        root = u32a(0x015282D8)
        if not root:
            return []
        obj = u32a(root + TASK_IFACE_ROOT_OBJ_OFF)
        x = u32a(obj + TASK_IFACE_X_OFF) if obj else 0
        iface = u32a(x + TASK_IFACE_OFF) if x else 0
        arr = u32a(iface + TASK_IFACE_ARR_OFF) if iface else 0
        if not arr:
            return []
        try:
            cnt = _rpm(h, arr, 1)[0]
        except Exception:
            return []
        ids: list[int] = []
        for i in range(min(int(cnt), 256)):
            try:
                e = _rpm(h, arr + i * TASK_ENTRY_STRIDE, 0x30)
            except Exception:
                continue
            tid = struct.unpack_from("<I", e, TASK_ENTRY_ID_OFF)[0]
            if tid:
                ids.append(tid)
        log(f"task_api accepted_ids_mem n={len(ids)}")
        return ids
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h))


def enumerate_all_tasks(
    session,
    *,
    log: LogFn | None = None,
    limit: int = 20000,
    exclude_accepted: bool = True,
    exclude_test: bool = True,
) -> list[dict]:
    """
    Enumerate the FULL quest-config table straight from game memory (no packets),
    the same way black shield lists every task instantly.

    Chain (verified live 2026-08-06 on PID <PID>, <COUNT> quests):
      root = *(0x015282D8)
      ctx  = *(root + 0x1F8)              # CECTaskConfig context
      map  = ctx + 0x28                   # hash map task_id -> desc
      bucket array at [map+0x14 .. map+0x18], stride 8, head ptr at slot+4
      node: +0x00 next-chain, +0x08 task_id, +0x0C desc ptr
      desc: +0x08 inline wchar task name

    Pure ReadProcessMemory; no remote calls / no hooks.

    exclude_accepted: drop task ids currently in the accepted list (pure memory).
    exclude_test: drop dev/test quest names (see TASK_TEST_NAME_PATTERNS).

    @author by ak
    """
    log = log or (lambda _m: None)
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        raise RuntimeError("session has no module_base")
    pid = int(session.pid)
    accepted_ids: set[int] = set()
    if exclude_accepted:
        accepted_ids = set(_accepted_task_ids_mem(session, log=log))
    h = _open_process(pid)
    try:
        def u32a(addr: int) -> int:
            try:
                return struct.unpack("<I", _rpm(h, addr, 4))[0]
            except Exception:
                return 0

        root = u32a(0x015282D8)
        if not root:
            raise RuntimeError("game root unavailable")
        ctx = u32a(root + TASK_CFG_CTX_OFF)
        if not ctx:
            raise RuntimeError("task config ctx unavailable")
        map_base = ctx + TASK_CFG_MAP_OFF
        bbegin = u32a(map_base + TASK_CFG_BUCKET_BEGIN_OFF)
        bend = u32a(map_base + TASK_CFG_BUCKET_END_OFF)
        if not bbegin or bend <= bbegin:
            log(f"task_api enumerate_all_tasks empty map (bbegin=0x{bbegin:X} bend=0x{bend:X})")
            return []
        nb = (bend - bbegin) // 8
        seen: set[int] = set()
        rows: list[dict] = []
        for i in range(min(nb, 0x20000)):
            try:
                slot = _rpm(h, bbegin + i * 8, 8)
            except Exception:
                continue
            head = struct.unpack("<II", slot)[1]
            node = int(head)
            depth = 0
            while node and node not in seen and depth < 1000:
                seen.add(node)
                try:
                    nd = _rpm(h, node, 0x20)
                except Exception:
                    break
                task_id, desc = struct.unpack_from("<II", nd, TASK_CFG_NODE_ID_OFF)
                if not task_id:
                    node = int(struct.unpack_from("<I", nd, TASK_CFG_NODE_NEXT_OFF)[0])
                    depth += 1
                    continue
                nxt = struct.unpack_from("<I", nd, TASK_CFG_NODE_NEXT_OFF)[0]
                name = ""
                if desc:
                    try:
                        name = _read_wstr_h(h, desc + TASK_CFG_DESC_NAME_OFF, 40)
                    except Exception:
                        name = ""
                if exclude_test and name and any(
                    pat in name for pat in TASK_TEST_NAME_PATTERNS
                ):
                    node = int(nxt)
                    depth += 1
                    continue
                if task_id in accepted_ids:
                    node = int(nxt)
                    depth += 1
                    continue
                rows.append(
                    {"task_id": int(task_id), "name": name,
                     "desc": int(desc), "node": node}
                )
                if len(rows) >= limit:
                    break
                node = int(nxt)
                depth += 1
            if len(rows) >= limit:
                break
        rows.sort(key=lambda r: int(r.get("task_id") or 0))
        log(f"task_api enumerate_all_tasks count={len(rows)} "
            f"(exclude_accepted={exclude_accepted} exclude_test={exclude_test})")
        return rows
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h))


def list_accepted_tasks_light(
    session,
    *,
    prev: list[TaskInfo] | list[dict] | None = None,
    refresh_can_finish: bool = False,
    log: LogFn | None = None,
) -> list[TaskInfo]:
    """
    Light UI refresh after accept/complete.

    Memory list + reuse cached names. By default CanFinish only runs for new ids
    or changed state bits; refresh_can_finish=True rechecks every row while still
    avoiding repeated GetTaskName calls.
    """
    log = log or (lambda _m: None)
    prev_map: dict[int, dict] = {}
    for p in prev or []:
        d = p.to_dict() if hasattr(p, "to_dict") else dict(p or {})
        tid = int(d.get("task_id") or 0)
        if tid:
            prev_map[tid] = d
    rows = list_accepted_tasks(
        session,
        log=log,
        resolve_names=False,
        resolve_can_finish=False,
        quiet=True,
    )
    for t in rows:
        old = prev_map.get(int(t.task_id))
        if old and old.get("name"):
            t.name = str(old.get("name") or "")
        need_finish = bool(refresh_can_finish)
        if old is not None and not need_finish:
            same_state = int(old.get("state") or 0) == int(t.state or 0)
            if same_state and "can_finish" in old:
                t.can_finish = bool(old.get("can_finish"))
                need_finish = False
            else:
                need_finish = True
        elif old is None:
            need_finish = True
        if need_finish:
            try:
                t.can_finish = task_can_finish(
                    session, t.task_id, log=lambda _m: None
                )
            except Exception:
                t.can_finish = bool(old.get("can_finish")) if old else False
        t.status_text = task_status_from_state(
            t.state, t.progress, can_finish=t.can_finish
        )
        if not t.name:
            try:
                nm = get_task_name(session, t.task_id, log=lambda _m: None)
                if nm:
                    t.name = nm
            except Exception:
                pass
    rows.sort(
        key=lambda x: (
            0 if x.can_finish else 1,
            0 if x.is_finished else 1,
            int(x.task_id),
        )
    )
    log(
        f"task_api list_accepted light n={len(rows)} "
        f"can_turnin={sum(1 for x in rows if x.can_finish)}"
    )
    return rows


def enrich_tasks_with_npc(
    session,
    rows: list[TaskInfo] | list[dict],
    *,
    radius: float = 80.0,
    live_scan: bool = True,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> list[dict]:
    """
    Attach Delv/Award NPC name + nearby dist for accepted-list display.

    Prefer AwardNPC when can_finish, else DelvNPC. Nearby live name/dist first;
    fall back to static cfg name without dist. Never leave raw ``tidNNNN`` when
    a better cached / nearby name is available for the same template.
    @author by ak
    """
    log = log or (lambda _m: None)
    # One nearby NPC scan for the whole list (stable names + dist). Accepted-list
    # UI refreshes disable this: role switching destroys these object pointers,
    # so a later GetObjectTemplateID virtual call can hit freed game memory.
    nearby_by_tid: dict[int, dict] = {}
    if live_scan and not (stop_event is not None and stop_event.is_set()):
        try:
            for n in list_nearby_npcs(
                session, radius=float(radius), limit=64, log=lambda _m: None
            ):
                if stop_event is not None and stop_event.is_set():
                    break
                ntid = int(n.get("tid") or 0)
                if not ntid:
                    continue
                prev = nearby_by_tid.get(ntid)
                try:
                    d = float(n.get("dist")) if n.get("dist") is not None else 1e9
                except (TypeError, ValueError):
                    d = 1e9
                if prev is None:
                    nearby_by_tid[ntid] = dict(n)
                    continue
                try:
                    pd = (
                        float(prev.get("dist"))
                        if prev.get("dist") is not None
                        else 1e9
                    )
                except (TypeError, ValueError):
                    pd = 1e9
                if d < pd:
                    nearby_by_tid[ntid] = dict(n)
        except Exception as e:
            log(f"task_api enrich nearby scan: {e}")

    out: list[dict] = []
    for raw in rows or []:
        if stop_event is not None and stop_event.is_set():
            break
        row = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw or {})
        tid = int(row.get("task_id") or 0)
        if not tid:
            out.append(row)
            continue
        # Keep panel compose ``<分类>具体名`` from GetTaskTexts.
        # format_task_display_name would strip the <分类> prefix — do not use here.
        raw_name = str(row.get("name") or "").strip()
        if not raw_name:
            row["name"] = format_task_display_name("", task_id=tid)
        else:
            row["name"] = raw_name
        try:
            meta = read_task_npc_tids(session, tid, log=lambda _m: None)
        except Exception:
            meta = {}
        delv = int(meta.get("delv_tid") or 0)
        award = int(meta.get("award_tid") or 0)
        row["delv_tid"] = delv
        row["award_tid"] = award
        prefer = award if (row.get("can_finish") and award) else (delv or award)
        npc_name = str(row.get("npc_name") or "").strip()
        npc_tid = prefer or int(row.get("npc_tid") or 0)
        dist = row.get("dist")
        # Prefer live nearby name for prefer tid.
        live = nearby_by_tid.get(int(prefer)) if prefer else None
        if live is not None:
            live_name = str(live.get("name") or "").strip()
            if live_name and not is_placeholder_npc_name(live_name):
                npc_name = live_name
            npc_tid = int(live.get("tid") or prefer or 0)
            try:
                dist = float(live.get("dist"))
            except (TypeError, ValueError):
                pass
        elif prefer and live_scan:
            try:
                hit = find_nearby_task_npc(
                    session, prefer, radius=float(radius), log=lambda _m: None
                )
            except Exception:
                hit = None
            if hit is not None:
                live_name = str(hit.get("name") or "").strip()
                if live_name and not is_placeholder_npc_name(live_name):
                    npc_name = live_name
                npc_tid = int(hit.get("tid") or prefer)
                try:
                    dist = float(hit.get("dist"))
                except (TypeError, ValueError):
                    pass
        # Keep previous good name if current is placeholder.
        if is_placeholder_npc_name(npc_name):
            prev = str(row.get("npc_name") or "").strip()
            if prev and not is_placeholder_npc_name(prev):
                npc_name = prev
            else:
                npc_name = ""
        # Do not invent tidNNNN for display — leave empty when unknown.
        row["npc_name"] = npc_name
        row["npc_tid"] = int(npc_tid or 0)
        row["dist"] = dist
        out.append(row)
    log(f"task_api enrich_npc n={len(out)}")
    return out


def get_task_desc_ptr(
    session,
    task_id: int,
    *,
    log: LogFn | None = None,
) -> int:
    """
    Resolve static task desc pointer for task_id (same as GetTaskName c2).

    @author by ak
    """
    log = log or (lambda _m: None)
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        return 0
    if not session_supports(session, "task.read"):
        log("task_api get_task_desc blocked: unknown client build")
        return 0
    tid = int(task_id) & 0xFFFFFFFF
    if not tid:
        return 0
    pid = int(session.pid)
    c1 = note_va_to_live(base, NOTE_VA_GET_TASK_NAME_C1)
    c2 = note_va_to_live(base, NOTE_VA_GET_TASK_NAME_C2)
    try:
        ctx = remote_call_cdecl_x86(pid, c1, [], timeout_ms=2000)
        ctx_u = int(ctx) & 0xFFFFFFFF
        if not ctx_u:
            return 0
        desc = _remote_thiscall_eax(pid, c2, ctx_u, [tid], timeout_ms=2000)
        return int(desc) & 0xFFFFFFFF
    except Exception as e:
        log(f"task_api get_task_desc id={tid} fail: {e}")
        return 0


def read_task_npc_tids(
    session,
    task_id: int,
    *,
    log: LogFn | None = None,
) -> dict:
    """
    Read DelvNPC / AwardNPC template ids from static task desc.

    DelvNPC @ desc+0x138 (接/引导), AwardNPC @ desc+0x13C (交任务, 0xC35900).
    Returns {delv_tid, award_tid, desc}.
    The tid mapping is static per task: cached per pid so offer linking does not
    re-issue a native GetTaskDesc call on every refresh.

    @author by ak
    """
    log = log or (lambda _m: None)
    tid = int(task_id) & 0xFFFFFFFF
    pid = int(getattr(session, "pid", 0) or 0)
    now = time.monotonic()
    with _TASK_NPC_TIDS_LOCK:
        ent = _TASK_NPC_TIDS_CACHE.get(pid)
        if ent and (now - float(ent.get("ts", 0.0))) < _TASK_NPC_TIDS_TTL_S:
            val = ent.get("tasks", {}).get(tid)
            if val is not None:
                return dict(val)
    out = {"delv_tid": 0, "award_tid": 0, "desc": 0}
    desc = get_task_desc_ptr(session, tid, log=log)
    out["desc"] = desc
    if not desc:
        return out
    h = _open_process(pid)
    try:
        try:
            d_raw = _rpm(h, desc + TASK_DESC_DELV_NPC_OFF, 4)
            a_raw = _rpm(h, desc + TASK_DESC_AWARD_NPC_OFF, 4)
            out["delv_tid"] = int(struct.unpack("<I", d_raw)[0])
            out["award_tid"] = int(struct.unpack("<I", a_raw)[0])
        except OSError as e:
            log(f"task_api read npc tids fail: {e}")
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h))
    log(
        f"task_api npc_tids id={tid} delv={out['delv_tid']} "
        f"award={out['award_tid']} desc=0x{desc:X}"
    )
    with _TASK_NPC_TIDS_LOCK:
        ent = _TASK_NPC_TIDS_CACHE.setdefault(pid, {"ts": now, "tasks": {}})
        ent["tasks"][tid] = dict(out)
    return out


def read_task_reach_site(
    session,
    task_id: int,
    *,
    desc: int | None = None,
    log: LogFn | None = None,
) -> dict:
    """
    Read static Reach* fields for cross-map task target pathfind.

    TaskHelp property builder (preferred VA ~0x66FFxx):
      ReachWorldId u32 @ +0x8D1
      ReachSceneId u32 @ +0x91F
      ReachSiteMin_X int @ +0x923 (fild)
      ReachSiteMin_Y/Z f32 @ +0x907 / +0x90B
      ReachSiteMax_X/Y/Z f32 @ +0x90F / +0x913 / +0x917

    Path target uses bbox center. HostMove first arg = ReachSceneId when set
    (same as game StartAutoMove / HostMoveToScenePosition scene).

    Returns dict: world_id, scene_id, x,y,z, min_*, max_*, ok, desc

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "world_id": 0,
        "scene_id": 0,
        "x": None,
        "y": None,
        "z": None,
        "min_x": None,
        "min_y": None,
        "min_z": None,
        "max_x": None,
        "max_y": None,
        "max_z": None,
        "ok": False,
        "desc": 0,
    }
    dptr = int(desc) & 0xFFFFFFFF if desc else get_task_desc_ptr(session, task_id, log=log)
    out["desc"] = dptr
    if not dptr:
        return out
    pid = int(session.pid)
    h = _open_process(pid)
    try:
        try:
            world = struct.unpack("<I", _rpm(h, dptr + TASK_DESC_REACH_WORLD_OFF, 4))[0]
            scene = struct.unpack("<I", _rpm(h, dptr + TASK_DESC_REACH_SCENE_OFF, 4))[0]
            min_x_i = struct.unpack("<i", _rpm(h, dptr + TASK_DESC_REACH_MIN_X_OFF, 4))[0]
            min_y = struct.unpack("<f", _rpm(h, dptr + TASK_DESC_REACH_MIN_Y_OFF, 4))[0]
            min_z = struct.unpack("<f", _rpm(h, dptr + TASK_DESC_REACH_MIN_Z_OFF, 4))[0]
            max_x = struct.unpack("<f", _rpm(h, dptr + TASK_DESC_REACH_MAX_X_OFF, 4))[0]
            max_y = struct.unpack("<f", _rpm(h, dptr + TASK_DESC_REACH_MAX_Y_OFF, 4))[0]
            max_z = struct.unpack("<f", _rpm(h, dptr + TASK_DESC_REACH_MAX_Z_OFF, 4))[0]
        except OSError as e:
            log(f"task_api reach_site read fail: {e}")
            return out
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h))

    out["world_id"] = int(world)
    out["scene_id"] = int(scene)
    # Min_X stored as int; other axes float. Normalize all to float.
    min_x = float(min_x_i)
    out["min_x"] = min_x
    out["min_y"] = float(min_y)
    out["min_z"] = float(min_z)
    out["max_x"] = float(max_x)
    out["max_y"] = float(max_y)
    out["max_z"] = float(max_z)

    # Valid if any non-zero span/coord (games often leaves zeros when unused)
    coords = (min_x, float(min_y), float(min_z), float(max_x), float(max_y), float(max_z))
    if any(abs(c) > 1e-3 for c in coords) or scene or world:
        cx = (min_x + float(max_x)) * 0.5 if abs(float(max_x)) > 1e-3 else min_x
        cy = (float(min_y) + float(max_y)) * 0.5 if abs(float(max_y)) > 1e-3 else float(min_y)
        cz = (float(min_z) + float(max_z)) * 0.5 if abs(float(max_z)) > 1e-3 else float(min_z)
        # if max all zero, use min only
        if abs(float(max_x)) < 1e-3 and abs(float(max_y)) < 1e-3 and abs(float(max_z)) < 1e-3:
            cx, cy, cz = min_x, float(min_y), float(min_z)
        out["x"] = float(cx)
        out["y"] = float(cy)
        out["z"] = float(cz)
        out["ok"] = abs(cx) > 1e-3 or abs(cz) > 1e-3 or abs(cy) > 1e-3
    log(
        f"task_api reach_site id={int(task_id)} scene={out['scene_id']} "
        f"world={out['world_id']} xyz=({out['x']},{out['y']},{out['z']}) ok={out['ok']}"
    )
    return out


def get_task_npc_world(
    session,
    npc_tid: int,
    *,
    log: LogFn | None = None,
) -> dict | None:
    """
    Resolve award/delv NPC tid to static world pos via GetCfgObjectInfo.

    Returns {tid, scene_id, x, y, z, ok} or None.
    @author by ak
    """
    log = log or (lambda _m: None)
    tid = int(npc_tid or 0)
    if not tid:
        return None
    try:
        from app.core.plg_objects import get_cfg_object_info

        return get_cfg_object_info(session, tid, log=log)
    except Exception as e:
        log(f"task_api get_task_npc_world tid={tid}: {e}")
        return None


def _entity_row(h, kind: str) -> dict:
    """Normalize entity hit to dict. @author by ak"""
    if hasattr(h, "to_dict"):
        row = h.to_dict()
    elif isinstance(h, dict):
        row = dict(h)
    else:
        row = {
            "name": getattr(h, "name", ""),
            "x": getattr(h, "x", None),
            "y": getattr(h, "y", None),
            "z": getattr(h, "z", None),
            "dist": getattr(h, "dist", None),
            "tid": getattr(h, "tid", None),
            "address": getattr(h, "address", None),
            "kind": getattr(h, "kind", kind),
        }
    if not row.get("kind"):
        row["kind"] = kind
    return row


def find_task_clue_targets(
    session,
    task: TaskInfo | dict,
    *,
    radius: float = 200.0,
    log: LogFn | None = None,
) -> list[dict]:
    """
    Resolve path/complete clues for one accepted task (in-game 目标 style).

    Priority:
      1) Static ReachSite (scene_id + xyz) — cross-map via HostMove(scene,...)
      2) Static DelvNPC / AwardNPC tids; match nearby entities by tid
      3) Fallback: nearby NPC/monster/matter scan

    Returns list of dicts with clue, x,y,z, scene_id, source, ...

    @author by ak
    """
    log = log or (lambda _m: None)
    if isinstance(task, TaskInfo):
        d = task.to_dict()
    else:
        d = dict(task or {})
    tid = int(d.get("task_id") or 0)
    can_turnin = bool(d.get("can_finish"))
    if not can_turnin and d.get("can_finish") is None:
        st = int(d.get("state") or 0)
        can_turnin = bool(d.get("is_success")) and bool(d.get("is_finished"))

    clues: list[dict] = []
    npc_meta = read_task_npc_tids(session, tid, log=log) if tid else {}
    delv_tid = int(npc_meta.get("delv_tid") or 0)
    award_tid = int(npc_meta.get("award_tid") or 0)
    desc_ptr = int(npc_meta.get("desc") or 0)

    reach = (
        read_task_reach_site(session, tid, desc=desc_ptr or None, log=log)
        if tid
        else {}
    )
    if reach.get("ok") and reach.get("x") is not None and reach.get("z") is not None:
        scene_id = int(reach.get("scene_id") or 0)
        world_id = int(reach.get("world_id") or 0)
        # ReachSite belongs to the static task descriptor.  It is not proof
        # that the current turn-in NPC is at this point after the task flips
        # to CanFinish.
        label = "任务目标坐标"
        name_bits = []
        if scene_id:
            name_bits.append(f"场景{scene_id}")
        if world_id:
            name_bits.append(f"世界{world_id}")
        name_bits.append(
            f"({float(reach['x']):.0f},{float(reach['y'] or 0):.0f},{float(reach['z']):.0f})"
        )
        clues.append(
            {
                "clue": label,
                "kind": "reach",
                "name": " ".join(name_bits) if name_bits else "ReachSite",
                "x": float(reach["x"]),
                "y": float(reach["y"] or 0.0),
                "z": float(reach["z"]),
                "dist": None,
                "tid": award_tid if can_turnin else delv_tid,
                "scene_id": scene_id,
                "world_id": world_id,
                "ptr": None,
                "obj_id": None,
                "task_id": tid,
                "can_turnin": can_turnin,
                "source": "reach_site",
            }
        )

    prefer_tids: list[tuple[int, str]] = []
    if can_turnin and award_tid:
        prefer_tids.append((award_tid, "交任务"))
    if delv_tid:
        prefer_tids.append((delv_tid, "任务目标" if not can_turnin else "接任务NPC"))
    if award_tid and not can_turnin:
        prefer_tids.append((award_tid, "交任务NPC"))

    # Static NPC world from GetCfgObjectInfo(tid) — works even when not nearby
    seen_cfg: set[int] = set()
    for want_tid, clue_label in prefer_tids:
        if not want_tid or want_tid in seen_cfg:
            continue
        seen_cfg.add(want_tid)
        cfg = get_task_npc_world(session, want_tid, log=log)
        if not cfg or not cfg.get("ok"):
            continue
        if cfg.get("x") is None or cfg.get("z") is None:
            continue
        # skip if already have reach_site with coords
        scene_id = int(cfg.get("scene_id") or 0)
        clues.append(
            {
                "clue": clue_label,
                "kind": "cfg_npc",
                "name": f"tid{want_tid}" + (f" 场景{scene_id}" if scene_id else ""),
                "x": float(cfg["x"]),
                "y": float(cfg.get("y") or 0.0),
                "z": float(cfg["z"]),
                "dist": None,
                "tid": want_tid,
                "scene_id": scene_id or None,
                "world_id": None,
                "ptr": None,
                "obj_id": None,
                "task_id": tid,
                "can_turnin": can_turnin,
                "source": "cfg_object",
            }
        )

    try:
        from app.core.entity_scan import scan_nearby_entities

        all_hits: list[dict] = []
        try:
            hits = scan_nearby_entities(
                session,
                radius=float(radius),
                kinds=("npc", "monster", "matter"),
                require_pos=False,
                log=log,
            )
        except Exception as e:
            log(f"task_api clue scan fail: {e}")
            hits = []
        for h in hits or []:
            row = _entity_row(h, getattr(h, "kind", None) or "npc")
            all_hits.append(row)

        matched_keys: set[tuple] = set()
        for want_tid, clue_label in prefer_tids:
            if not want_tid:
                continue
            for row in all_hits:
                etid = row.get("tid")
                try:
                    etid_i = int(etid) if etid is not None else 0
                except (TypeError, ValueError):
                    etid_i = 0
                if etid_i != want_tid:
                    continue
                name = (row.get("name") or "").strip() or f"tid{want_tid}"
                x, y, z = row.get("x"), row.get("y"), row.get("z")
                if x is None and z is None:
                    continue
                key = (row.get("kind"), name, want_tid)
                if key in matched_keys:
                    continue
                matched_keys.add(key)
                clues.append(
                    {
                        "clue": clue_label,
                        "kind": row.get("kind") or "npc",
                        "name": name,
                        "x": x,
                        "y": y,
                        "z": z,
                        "dist": row.get("dist"),
                        "tid": want_tid,
                        "scene_id": None,
                        "ptr": row.get("address") or row.get("ptr"),
                        "obj_id": row.get("obj_id"),
                        "task_id": tid,
                        "can_turnin": can_turnin,
                        "source": "template_tid",
                    }
                )

        has_primary = any(
            c.get("source") in ("reach_site", "cfg_object", "template_tid") for c in clues
        )
        # Portal delv/award tids have no GetCfg/ReachSite coords. Blind-dumping
        # nearby city NPCs makes every task "arrive" at the same nearest point.
        prefer_portal_only = False
        try:
            from app.core.portal_service import PORTAL_TID_TIER as _PTT

            want_ids = [int(t or 0) for t, _ in prefer_tids if int(t or 0)]
            prefer_portal_only = bool(want_ids) and all(t in _PTT for t in want_ids)
        except Exception:
            prefer_portal_only = False

        if not has_primary:
            if prefer_portal_only:
                log(
                    f"task_api clues: no primary for portal delv/award "
                    f"{[t for t, _ in prefer_tids]} — skip blind nearby dump"
                )
            else:
                # Original behavior for normal tasks without static coords.
                for row in all_hits:
                    name = (row.get("name") or "").strip()
                    if not name:
                        continue
                    x, y, z = row.get("x"), row.get("y"), row.get("z")
                    if x is None and y is None and z is None:
                        continue
                    k = row.get("kind") or "npc"
                    if can_turnin:
                        if k != "npc":
                            continue
                        clue = "交任务"
                    elif k == "npc":
                        clue = "做任务(NPC)"
                    elif k == "monster":
                        clue = "做任务(怪)"
                    else:
                        clue = "做任务(物)"
                    clues.append(
                        {
                            "clue": clue,
                            "kind": k,
                            "name": name,
                            "x": x,
                            "y": y,
                            "z": z,
                            "dist": row.get("dist"),
                            "tid": row.get("tid"),
                            "scene_id": None,
                            "ptr": row.get("address") or row.get("ptr"),
                            "obj_id": row.get("obj_id"),
                            "task_id": tid,
                            "can_turnin": can_turnin,
                            "source": "nearby",
                        }
                    )
        # When primary exists: do NOT append random nearby NPCs (route pollution).

        if prefer_tids and not any(c.get("source") == "template_tid" for c in clues):
            if not any(c.get("source") in ("reach_site", "cfg_object") for c in clues):
                for want_tid, clue_label in prefer_tids:
                    if not want_tid:
                        continue
                    clues.append(
                        {
                            "clue": clue_label,
                            "kind": "npc",
                            "name": f"（未在附近）tid={want_tid}",
                            "x": None,
                            "y": None,
                            "z": None,
                            "dist": None,
                            "tid": want_tid,
                            "scene_id": None,
                            "ptr": None,
                            "obj_id": None,
                            "task_id": tid,
                            "can_turnin": can_turnin,
                            "source": "template_tid_miss",
                        }
                    )
    except Exception as e:
        log(f"task_api clue scan fail: {e}")

    def _dk(r: dict) -> float:
        try:
            return float(r.get("dist") if r.get("dist") is not None else 9999)
        except (TypeError, ValueError):
            return 9999.0

    def _pri(r: dict) -> int:
        src = r.get("source") or ""
        clue = r.get("clue") or ""
        if src == "reach_site":
            return 0
        if src == "cfg_object" and "交" in clue:
            return 1
        if src == "cfg_object":
            return 2
        if src == "template_tid" and "交" in clue:
            return 3
        if src == "template_tid":
            return 4
        if src == "template_tid_miss":
            return 5
        if clue == "交任务":
            return 6
        return 7

    clues.sort(key=lambda r: (_pri(r), _dk(r)))
    seen: set[tuple] = set()
    out: list[dict] = []
    for r in clues:
        key = (
            r.get("source"),
            r.get("kind"),
            r.get("name"),
            r.get("tid"),
            r.get("scene_id"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    log(
        f"task_api clues task={tid} n={len(out)} can_turnin={can_turnin} "
        f"delv={delv_tid} award={award_tid} reach_ok={bool(reach.get('ok'))} "
        f"scene={reach.get('scene_id')}"
    )
    return out


def pathfind_to_clue(
    session,
    clue: dict,
    *,
    mode: int | None = None,
    hwnd: int = 0,
    use_bridge: bool = True,
    allow_remote_fallback: bool = False,
    arrive_radius: float = 12.0,
    verify_timeout_s: float = 120.0,
    poll_s: float = 0.5,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    stuck_s: float = 0.0,
    stuck_move_eps: float = 0.8,
    stuck_check: Callable | None = None,
    unstick: Callable | None = None,
    unstick_hold_s: float = 0.0,
    abort_check: Callable[[], object] | None = None,
    position_reader: Callable[[], object] | None = None,
) -> dict:
    """
    HostMoveToScenePosition toward clue xyz (no SetPos), then verify arrival.

    scene/mode:
      - clue.scene_id (ReachSite) preferred for cross-map
      - else explicit mode
      - else current scene from GetCurrentScenePosition

    Success requires two consecutive position samples in the target scene and
    within ``arrive_radius`` horizontally. A successful function call alone is
    only command submission and is never reported as pathfinding success.

    stuck_s > 0: if optional ``stuck_check`` allows the current position and
    horizontal movement stays below stuck_move_eps for stuck_s seconds, call
    ``unstick``, hold ``unstick_hold_s`` without HostMove, then re-issue.

    @author by ak
    """
    log = log or (lambda _m: None)
    from app.core.automove import PathTarget, host_move_to, read_scene_position

    def _read_position_sample(*, verbose: bool = False):
        if position_reader is not None:
            return position_reader()
        return read_scene_position(session, log=log if verbose else (lambda _m: None))

    x = clue.get("x")
    y = clue.get("y")
    z = clue.get("z")
    if x is None or z is None:
        return {"ok": False, "error": "clue missing xyz"}
    if y is None:
        y = 0.0

    scene_mode = mode
    if scene_mode is None:
        sid = clue.get("scene_id")
        if sid is not None and int(sid) != 0:
            scene_mode = int(sid)
        else:
            scene_mode = 0
            try:
                sp = _read_position_sample(verbose=True)
                if hasattr(sp, "scene_id") and sp.scene_id is not None:
                    scene_mode = int(sp.scene_id)
                elif isinstance(sp, dict) and sp.get("scene_id") is not None:
                    scene_mode = int(sp["scene_id"])
            except Exception:
                scene_mode = 0

    tgt = PathTarget(
        x=float(x),
        y=float(y),
        z=float(z),
        mode=int(scene_mode or 0),
        map_hint=str(clue.get("name") or clue.get("clue") or ""),
    )
    # ReachSite cross-map: do not fallback mode=0 (would path on wrong map)
    use_fb = not (
        clue.get("source") in ("reach_site", "cfg_object")
        and int(scene_mode or 0) != 0
    )
    d: dict | None = None
    if use_bridge:
        bridge = None
        try:
            from app.core.xajh_bridge import CMD_HOST_MOVE, ensure_bridge

            bridge = ensure_bridge(
                int(session.pid),
                log=log,
                inject_if_needed=False,
                hwnd=int(hwnd or 0) or None,
            )
            if bridge is None:
                d = {"ok": False, "error": "bridge not ready for task path"}
            else:
                result = bridge.call(
                    CMD_HOST_MOVE,
                    x=float(x),
                    y=float(y),
                    z=float(z),
                    mode=int(scene_mode or 0),
                    hwnd=int(hwnd or 0) or None,
                    timeout_ms=4000,
                )
                d = result.to_dict()
                d["via"] = "bridge"
        except Exception as exc:
            d = {"ok": False, "error": str(exc), "via": "bridge"}
        finally:
            if bridge is not None:
                try:
                    bridge.close()
                except Exception:
                    pass

    if (d is None or not d.get("ok")) and allow_remote_fallback:
        res = host_move_to(session, tgt, log=log, fallback_mode0=use_fb)
        if hasattr(res, "to_dict"):
            d = res.to_dict()
        elif isinstance(res, dict):
            d = res
        else:
            d = {"ok": bool(getattr(res, "ok", False)), "note": str(res)}
        d["via"] = "remote"
    elif d is None:
        d = {
            "ok": False,
            "error": "raw remote task path is disabled; bridge required",
        }
    d["clue"] = clue.get("clue")
    d["name"] = clue.get("name")
    d["scene_id"] = scene_mode
    d["source"] = clue.get("source")
    d["command_ok"] = bool(d.get("ok"))
    d["arrived"] = False
    d["verified"] = False
    if not d["command_ok"]:
        return d

    deadline = time.monotonic() + max(0.01, float(verify_timeout_s))
    radius = max(0.5, float(arrive_radius))
    interval = max(0.01, float(poll_s))
    target_scene = int(scene_mode or 0)
    target_x = float(x)
    target_y = float(y)
    target_z = float(z)
    in_range_hits = 0
    samples = 0
    last_distance: float | None = None
    last_scene: int | None = None
    last_pos: tuple[float, float, float] | None = None
    stuck_gate = max(0.0, float(stuck_s or 0.0))
    stuck_eps = max(0.05, float(stuck_move_eps or 0.8))
    hold_after = max(0.0, float(unstick_hold_s or 0.0))
    stuck_anchor: tuple[float, float, float] | None = None
    stuck_since: float | None = None
    unstick_count = 0
    path_cool_until = 0.0  # after unstick: no HostMove / no stuck until this mono time

    def _abort_requested() -> bool:
        if abort_check is None:
            return False
        try:
            value = abort_check()
        except Exception as exc:
            log(f"task_api path abort_check err: {exc}")
            return False
        if not value:
            return False
        reason = str(value) if not isinstance(value, bool) else "requested"
        d.update(
            ok=False,
            arrived=False,
            verified=False,
            aborted=True,
            abort_reason=reason,
            error=f"path verification aborted: {reason}",
            samples=samples,
            last_distance=last_distance,
            last_scene_id=last_scene,
            last_position=list(last_pos) if last_pos else None,
        )
        log(f"task_api path aborted reason={reason}")
        return True

    def _reissue_host_move(*, reason: str) -> None:
        nonlocal d
        log(
            f"task_api path re-issue HostMove ({reason}) "
            f"mode={int(scene_mode or 0)} xyz=({target_x:.1f},{target_y:.1f},{target_z:.1f})"
        )
        d2: dict | None = None
        if use_bridge:
            bridge = None
            try:
                from app.core.xajh_bridge import CMD_HOST_MOVE, ensure_bridge

                bridge = ensure_bridge(
                    int(session.pid),
                    log=log,
                    inject_if_needed=False,
                    hwnd=int(hwnd or 0) or None,
                )
                if bridge is not None:
                    result = bridge.call(
                        CMD_HOST_MOVE,
                        x=float(target_x),
                        y=float(target_y),
                        z=float(target_z),
                        mode=int(scene_mode or 0),
                        hwnd=int(hwnd or 0) or None,
                        timeout_ms=4000,
                    )
                    d2 = result.to_dict()
                    d2["via"] = "bridge"
            except Exception as exc:
                d2 = {"ok": False, "error": str(exc), "via": "bridge"}
            finally:
                if bridge is not None:
                    try:
                        bridge.close()
                    except Exception:
                        pass
        if (d2 is None or not d2.get("ok")) and allow_remote_fallback:
            res = host_move_to(session, tgt, log=log, fallback_mode0=use_fb)
            if hasattr(res, "to_dict"):
                d2 = res.to_dict()
            elif isinstance(res, dict):
                d2 = res
            else:
                d2 = {"ok": bool(getattr(res, "ok", False))}
            d2["via"] = "remote"
        if d2 is not None:
            # keep verification fields; refresh command meta
            d["command_ok"] = bool(d2.get("ok"))
            d["via"] = d2.get("via", d.get("via"))
            if d2.get("error"):
                d["last_reissue_error"] = d2.get("error")

    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            d.update(
                ok=False,
                error="path verification stopped",
                samples=samples,
                last_distance=last_distance,
                last_scene_id=last_scene,
                last_position=list(last_pos) if last_pos else None,
            )
            return d
        if _abort_requested():
            return d
        sp = _read_position_sample()
        if getattr(sp, "ok", False) and getattr(sp, "scene_pos", None):
            samples += 1
            last_pos = tuple(float(v) for v in sp.scene_pos)
            sid = getattr(sp, "scene_id", None)
            last_scene = int(sid) if sid is not None else None
            last_distance = math.hypot(last_pos[0] - target_x, last_pos[2] - target_z)
            scene_ok = not target_scene or last_scene == target_scene
            if scene_ok and last_distance <= radius:
                in_range_hits += 1
                if in_range_hits >= 2:
                    d.update(
                        ok=True,
                        arrived=True,
                        verified=True,
                        samples=samples,
                        last_distance=last_distance,
                        last_scene_id=last_scene,
                        last_position=list(last_pos),
                        note=(str(d.get("note") or "") + "; arrival verified").lstrip("; "),
                    )
                    log(
                        f"task_api path arrived scene={last_scene} "
                        f"distance={last_distance:.2f} radius={radius:.2f}"
                    )
                    return d
            else:
                in_range_hits = 0
            # 卡住脱困：位移不足 stuck_gate 秒；回撤后 hold 内禁止再寻路（让阶段/空气墙推进）
            now_m = time.monotonic()
            stuck_allowed = True
            if stuck_check is not None:
                try:
                    stuck_allowed = bool(
                        stuck_check(
                            session,
                            last_pos=last_pos,
                            target=(target_x, target_y, target_z),
                            scene_id=last_scene,
                            distance=last_distance,
                        )
                    )
                except Exception as exc:
                    stuck_allowed = False
                    log(f"task_api path stuck_check err: {exc}")
            if path_cool_until > 0 and now_m < path_cool_until:
                # 冷却中：只盯是否意外到位，不 HostMove、不判卡住
                stuck_anchor = None
                stuck_since = None
            elif (
                stuck_gate > 0
                and stuck_allowed
                and last_pos is not None
                and last_distance is not None
                and last_distance > radius
            ):
                if stuck_anchor is None or stuck_since is None:
                    stuck_anchor = last_pos
                    stuck_since = now_m
                else:
                    moved = math.hypot(
                        last_pos[0] - stuck_anchor[0],
                        last_pos[2] - stuck_anchor[2],
                    )
                    if moved > stuck_eps:
                        stuck_anchor = last_pos
                        stuck_since = now_m
                    elif (now_m - stuck_since) >= stuck_gate:
                        if _abort_requested():
                            return d
                        stuck_for = now_m - stuck_since
                        unstick_count += 1
                        log(
                            f"task_api path STUCK {stuck_for:.1f}s "
                            f"moved={moved:.2f}m dist={last_distance:.1f}m "
                            f"pos=({last_pos[0]:.1f},{last_pos[1]:.1f},{last_pos[2]:.1f}) "
                            f"#{unstick_count}"
                        )
                        if unstick is not None:
                            try:
                                unstick(
                                    session,
                                    last_pos=last_pos,
                                    target=(target_x, target_y, target_z),
                                    scene_id=last_scene,
                                    stuck_for_s=stuck_for,
                                    attempt=unstick_count,
                                )
                            except Exception as ue:
                                log(f"task_api path unstick err: {ue}")
                        if _abort_requested():
                            return d
                        # 回撤后静待：不立刻寻路，避免反复挤空气墙
                        if hold_after > 0:
                            path_cool_until = time.monotonic() + hold_after
                            log(
                                f"task_api path HOLD {hold_after:.0f}s after unstick "
                                f"(no HostMove, let phase progress) #{unstick_count}"
                            )
                            # 可中断等待；期间继续采样是否到位
                            hold_end = path_cool_until
                            while time.monotonic() < hold_end:
                                if stop_event is not None and stop_event.is_set():
                                    d.update(
                                        ok=False,
                                        error="path verification stopped",
                                        samples=samples,
                                        last_distance=last_distance,
                                        last_scene_id=last_scene,
                                        last_position=list(last_pos)
                                        if last_pos
                                        else None,
                                    )
                                    return d
                                if _abort_requested():
                                    return d
                                if time.monotonic() >= deadline:
                                    break
                                sp2 = _read_position_sample()
                                if (
                                    getattr(sp2, "ok", False)
                                    and getattr(sp2, "scene_pos", None)
                                ):
                                    samples += 1
                                    last_pos = tuple(
                                        float(v) for v in sp2.scene_pos
                                    )
                                    sid2 = getattr(sp2, "scene_id", None)
                                    last_scene = (
                                        int(sid2) if sid2 is not None else None
                                    )
                                    last_distance = math.hypot(
                                        last_pos[0] - target_x,
                                        last_pos[2] - target_z,
                                    )
                                    scene_ok2 = (
                                        not target_scene
                                        or last_scene == target_scene
                                    )
                                    if (
                                        scene_ok2
                                        and last_distance is not None
                                        and last_distance <= radius
                                    ):
                                        in_range_hits += 1
                                        if in_range_hits >= 2:
                                            d.update(
                                                ok=True,
                                                arrived=True,
                                                verified=True,
                                                samples=samples,
                                                last_distance=last_distance,
                                                last_scene_id=last_scene,
                                                last_position=list(last_pos),
                                                note=(
                                                    str(d.get("note") or "")
                                                    + "; arrival during unstick hold"
                                                ).lstrip("; "),
                                            )
                                            log(
                                                f"task_api path arrived during hold "
                                                f"scene={last_scene} "
                                                f"distance={last_distance:.2f}"
                                            )
                                            return d
                                    else:
                                        in_range_hits = 0
                                left_h = hold_end - time.monotonic()
                                if left_h <= 0:
                                    break
                                w = min(interval, left_h)
                                if stop_event is not None:
                                    if stop_event.wait(w):
                                        continue
                                else:
                                    time.sleep(w)
                        # hold 结束（或 hold=0）再重新寻路
                        if time.monotonic() < deadline and (
                            stop_event is None or not stop_event.is_set()
                        ):
                            _reissue_host_move(
                                reason=f"after_unstick_hold#{unstick_count}"
                            )
                        stuck_anchor = None
                        stuck_since = None
                        path_cool_until = 0.0
                        in_range_hits = 0
            elif not stuck_allowed:
                # Do not carry stationary time from normal path/map loading into
                # a later gated obstacle zone.
                stuck_anchor = None
                stuck_since = None
        if stop_event is not None:
            if stop_event.wait(interval):
                continue
        else:
            time.sleep(interval)

    d.update(
        ok=False,
        error=(
            "path verification timeout"
            if samples
            else "path verification failed: no position samples"
        ),
        samples=samples,
        last_distance=last_distance,
        last_scene_id=last_scene,
        last_position=list(last_pos) if last_pos else None,
    )
    return d


def list_available_tasks(
    session,
    *,
    log: LogFn | None = None,
) -> list[TaskInfo]:
    """
    Global available (可接) tasks — still unresolved on this build.

    Prefer list_nearby_offer_tasks() for NPC-nearby offers.
    """
    log = log or (lambda _m: None)
    log("task_api list_available: not resolved on this build (use nearby offer list)")
    return []


def list_nearby_npcs(
    session,
    *,
    radius: float = 120.0,
    limit: int = 48,
    log: LogFn | None = None,
) -> list[dict]:
    """Scan live nearby NPCs (tid/name/dist/ptr/id64)."""
    log = log or (lambda _m: None)
    from app.core.automove import read_scene_position
    from app.core.plg_interact import get_object_id64
    from app.core.plg_objects import CLASS_NPC, list_class_objects

    host_pos = None
    try:
        scene = read_scene_position(session, log=lambda _m: None)
        if scene.ok and scene.scene_pos:
            host_pos = scene.scene_pos
    except Exception:
        host_pos = None
    rows = list_class_objects(
        session,
        CLASS_NPC,
        host_pos=host_pos,
        radius=float(radius),
        limit=int(limit),
        read_name=True,
        read_tid=True,
        max_inspect=max(96, int(limit) * 3),
        log=log,
    )
    out: list[dict] = []
    for npc in rows:
        tid = int(npc.tid or 0)
        if not tid:
            continue
        oid = 0
        try:
            raw = get_object_id64(session, int(npc.ptr))
            oid = int(raw or 0)
        except Exception:
            oid = 0
        try:
            dist = float(npc.dist) if npc.dist is not None else 1e9
        except (TypeError, ValueError):
            dist = 1e9
        out.append(
            {
                "kind": "npc",
                "name": npc.name or f"tid{tid}",
                "tid": tid,
                "dist": dist,
                "x": npc.x,
                "y": npc.y,
                "z": npc.z,
                "ptr": int(npc.ptr),
                "obj_id": oid,
                "source": "nearby_npc",
            }
        )
    out.sort(key=lambda r: float(r.get("dist") or 1e9))
    log(f"task_api nearby_npcs n={len(out)} r={radius}")
    return out


# 可接探测缓存：task template id → {delv_tid, award_tid, name}。
# 任务→Delv/Award NPC 映射是静态配置，首次探测后长缓存复用，避免每次刷新
# 都对 60 个候选起远程调用（那是任务页“卡住/太重”的主要来源）。
_PROBE_DESC_CACHE: dict[int, dict] = {}
_PROBE_DESC_CACHE_LOCK = threading.RLock()
_PROBE_DESC_TTL_S = 600.0

# 已接任务 → Delv/Award NPC tid 缓存（静态配置，长缓存）。
_TASK_NPC_TIDS_CACHE: dict[int, dict] = {}
_TASK_NPC_TIDS_LOCK = threading.RLock()
_TASK_NPC_TIDS_TTL_S = 600.0


def _probe_delv_offers(
    session,
    *,
    nearby_tids: set[int],
    accepted_ids: set[int],
    candidates: list[int],
    max_scan: int = 60,
    log: LogFn | None = None,
) -> list[dict]:
    """
    Best-effort 可接 probe: reuse GetTaskName c1 once, then c2+RPM per id.

    Only resolve display name when DelvNPC hits a nearby tid (cheap filter).
    Task→NPC mapping is static: per-candidate desc is cached per pid so repeated
    refreshes are memory reads + cache filter (no remote thread storm).

    @author by ak
    """
    log = log or (lambda _m: None)
    if not nearby_tids or not candidates:
        return []
    base = int(getattr(session, "module_base", 0) or 0)
    if not base or not session_supports(session, "task.read"):
        return []
    pid = int(session.pid)
    c1 = note_va_to_live(base, NOTE_VA_GET_TASK_NAME_C1)
    c2 = note_va_to_live(base, NOTE_VA_GET_TASK_NAME_C2)
    try:
        ctx = remote_call_cdecl_x86(pid, c1, [], timeout_ms=2000)
        ctx_u = int(ctx) & 0xFFFFFFFF
    except Exception as e:
        log(f"task_api probe c1 fail: {e}")
        return []
    if not ctx_u:
        return []

    now = time.monotonic()
    with _PROBE_DESC_CACHE_LOCK:
        ent = _PROBE_DESC_CACHE.get(pid)
        if ent is None or (now - float(ent.get("ts", 0.0))) >= _PROBE_DESC_TTL_S:
            ent = {"ts": now, "tasks": {}}
            _PROBE_DESC_CACHE[pid] = ent
        known = ent["tasks"]

    seen: set[int] = set()
    probe_ids: list[int] = []
    for tid in candidates:
        tid = int(tid)
        if tid <= 0 or tid in seen or tid in accepted_ids:
            continue
        seen.add(tid)
        if tid in known:
            continue
        probe_ids.append(tid)
        if len(probe_ids) >= int(max_scan):
            break

    h = _open_process(pid)
    try:
        for tid in probe_ids:
            if tid in known:
                continue
            try:
                desc = _remote_thiscall_eax(pid, c2, ctx_u, [tid], timeout_ms=1500)
            except Exception:
                continue
            desc_u = int(desc) & 0xFFFFFFFF
            if not desc_u:
                continue
            try:
                d_raw = _rpm(h, desc_u + TASK_DESC_DELV_NPC_OFF, 4)
                a_raw = _rpm(h, desc_u + TASK_DESC_AWARD_NPC_OFF, 4)
                delv = int(struct.unpack("<I", d_raw)[0])
                award = int(struct.unpack("<I", a_raw)[0])
            except OSError:
                continue
            name = ""
            try:
                # Panel compose: category @A98 + specific title @desc+8
                p_raw = _rpm(h, desc_u + TASK_NAME_WSTR_PTR_OFF, 4)
                p = struct.unpack("<I", p_raw)[0]
                category = _read_wstr_h(h, p, 48) if p else ""
                if not category:
                    category = _read_wstr_h(h, desc_u + TASK_NAME_WSTR_OFF, 48)
                panel = _read_wstr_h(h, desc_u + TASK_DESC_PANEL_TITLE_OFF, 32)
                rear = ""
                try:
                    pr = _rpm(h, desc_u + TASK_DESC_PANEL_TITLE_REAR_PTR_OFF, 4)
                    rear_p = struct.unpack("<I", pr)[0]
                    rear = _read_wstr_h(h, rear_p, 32) if rear_p else ""
                except OSError:
                    rear = ""
                name = build_task_display_name(
                    category,
                    task_id=tid,
                    panel_title=panel,
                    panel_rear=rear,
                )
            except Exception:
                name = ""
            known[tid] = {
                "task_id": tid,
                "delv_tid": int(delv),
                "award_tid": int(award),
                "name": name,
            }
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h))

    out: list[dict] = []
    for tid in list(known.keys()):
        if tid in accepted_ids:
            continue
        meta = known.get(tid) or {}
        delv = int(meta.get("delv_tid") or 0)
        if not delv or delv not in nearby_tids:
            continue
        name = str(meta.get("name") or "")
        if not name:
            continue
        out.append(
            {
                "kind": "available",
                "task_id": tid,
                "name": name,
                "status_text": "可接",
                "can_finish": False,
                "delv_tid": delv,
                "award_tid": int(meta.get("award_tid") or 0),
                "source": "delv_scan",
                "role": "接",
            }
        )
    log(f"task_api probe_delv scanned={len(probe_ids)} hit={len(out)}")
    return out


def list_nearby_offer_tasks(
    session,
    *,
    radius: float = 15.0,
    limit_npc: int = 48,
    id_scan: tuple[int, int] | None = None,
    max_scan: int = 60,
    probe_offers: bool = True,
    accepted_prev: list | None = None,
    log: LogFn | None = None,
) -> list[dict]:
    """
    Nearby NPC + offers for the formal task page.

    Default radius 15m — only face-distance NPCs (converges list).
    Fast path:
      1) Live NPC instances
      2) Light accepted list → link Delv/Award
      3) Small targeted id probe (地宫 10000-10050 + seeds ±12) via reused c1
    """
    log = log or (lambda _m: None)
    t0 = time.monotonic()
    npcs = list_nearby_npcs(
        session, radius=float(radius), limit=int(limit_npc), log=log
    )
    if not npcs:
        log("task_api nearby_offer: no nearby npcs")
        return []

    instances: list[dict] = []
    by_tid: dict[int, list[dict]] = {}
    for n in npcs:
        tid = int(n.get("tid") or 0)
        if not tid:
            continue
        row = dict(n)
        instances.append(row)
        by_tid.setdefault(tid, []).append(row)
    nearby_tids = set(by_tid.keys())
    has_dungeon_npc = any(
        "地宫" in str(n.get("name") or "") for n in instances
    )

    # Authority: memory accepted ids (not name). Names only for display / family match.
    accepted_ids = set(list_accepted_task_ids(session, log=lambda _m: None))
    for p in accepted_prev or []:
        d = p.to_dict() if hasattr(p, "to_dict") else dict(p or {})
        tid = int(d.get("task_id") or 0)
        if tid:
            accepted_ids.add(tid)

    accepted = list_accepted_tasks_light(
        session, prev=accepted_prev, log=lambda _m: None
    )
    accepted_by_id: dict[int, object] = {int(t.task_id): t for t in accepted}
    for p in accepted_prev or []:
        d = p.to_dict() if hasattr(p, "to_dict") else dict(p or {})
        tid = int(d.get("task_id") or 0)
        if tid and tid not in accepted_by_id:
            accepted_by_id[tid] = type(
                "T",
                (),
                {
                    "task_id": tid,
                    "name": str(d.get("name") or ""),
                    "status_text": str(d.get("status_text") or "进行中"),
                    "can_finish": bool(d.get("can_finish")),
                },
            )()
    # Light/prev list is authoritative for UI even if id snapshot failed.
    for tid in list(accepted_by_id.keys()):
        accepted_ids.add(int(tid))
    for tid in list(accepted_ids):
        if tid not in accepted_by_id:
            accepted_by_id[tid] = type(
                "T",
                (),
                {
                    "task_id": tid,
                    "name": "",
                    "status_text": "进行中",
                    "can_finish": False,
                },
            )()

    def _status_for_accepted(tid: int) -> tuple[str, bool, str]:
        live = accepted_by_id.get(int(tid))
        if live is None:
            return "进行中", False, f"任务{tid}"
        can_fin = bool(getattr(live, "can_finish", False))
        st = "可交" if can_fin else (getattr(live, "status_text", None) or "进行中")
        nm = (getattr(live, "name", None) or "").strip() or f"任务{tid}"
        return st, can_fin, nm

    def _match_accepted_by_name(offer_name: str) -> int | None:
        """
        Map probe display name to an accepted task id (same family).

        e.g. 每日杀怪-限1次 / <每日杀怪>140每日杀怪 vs accepted <每日杀怪>xxx.
        @author by ak
        """
        if not offer_name:
            return None
        for aid, live in accepted_by_id.items():
            an = (getattr(live, "name", None) or "").strip()
            if an and task_names_match(offer_name, an):
                return int(aid)
        return None

    def _panel_name_for(tid: int, fallback: str = "") -> str:
        """Resolve ``<分类>具体名`` for task id; reuse accepted cache first."""
        tid = int(tid or 0)
        if tid:
            live = accepted_by_id.get(tid)
            nm = (getattr(live, "name", None) or "").strip() if live is not None else ""
            if nm and ("<" in nm or len(nm) >= 2):
                return nm
            try:
                nm = get_task_name(session, tid, log=lambda _m: None)
                if nm:
                    return nm
            except Exception:
                pass
        return str(fallback or (f"任务{tid}" if tid else "")).strip()

    offers_by_tid: dict[int, list[dict]] = {}
    seen_task_npc: set[tuple[int, int]] = set()  # (task_id, npc_tid)

    def _add_offer(tid_key: int, payload: dict) -> None:
        tid = int(payload.get("task_id") or 0)
        ntid = int(tid_key)
        # ID 优先：已接 id 禁止再显示为 可接
        if tid and tid in accepted_ids and payload.get("kind") == "available":
            st, can_fin, nm = _status_for_accepted(tid)
            # 附近任务不刷“进行中”；进行中请看已接任务(控)。仅可交时保留关联。
            if not can_fin:
                return
            payload = {
                **payload,
                "kind": "accepted_link",
                "status_text": "可交",
                "can_finish": True,
                "name": _panel_name_for(tid, nm or payload.get("name") or f"任务{tid}"),
                "source": "accepted_id",
                "role": "交",
            }
        # Name family：探测到的“可接”若与已接同具体任务，禁止误标 可接
        elif payload.get("kind") == "available":
            matched = _match_accepted_by_name(str(payload.get("name") or ""))
            if matched:
                st, can_fin, nm = _status_for_accepted(matched)
                if not can_fin:
                    return
                payload = {
                    **payload,
                    "kind": "accepted_link",
                    "task_id": matched,
                    "status_text": "可交",
                    "can_finish": True,
                    "name": _panel_name_for(
                        matched, nm or payload.get("name") or f"任务{matched}"
                    ),
                    "source": "accepted_name",
                    "probed_task_id": tid or None,
                    "role": "交",
                }
                tid = matched
            else:
                # Ensure available rows also use panel compose when only category present.
                payload = {
                    **payload,
                    "name": _panel_name_for(tid, str(payload.get("name") or "")),
                }
        elif tid:
            payload = {
                **payload,
                "name": _panel_name_for(tid, str(payload.get("name") or "")),
            }
        if tid and (tid, ntid) in seen_task_npc:
            return
        if tid:
            seen_task_npc.add((tid, ntid))
        offers_by_tid.setdefault(ntid, []).append(payload)

    # 已接任务：仅把“可交”挂到附近 Award NPC（附近任务不展示进行中）
    for tid in sorted(accepted_ids):
        live = accepted_by_id.get(tid)
        meta = read_task_npc_tids(session, tid, log=lambda _m: None)
        delv = int(meta.get("delv_tid") or 0)
        award = int(meta.get("award_tid") or 0)
        st, can_fin, nm = _status_for_accepted(tid)
        if live is not None and getattr(live, "name", None):
            nm = getattr(live, "name") or nm
        if not can_fin:
            continue
        if not award or award not in nearby_tids:
            continue
        _add_offer(
            award,
            {
                "kind": "accepted_link",
                "task_id": int(tid),
                "name": nm,
                "status_text": "可交",
                "can_finish": True,
                "delv_tid": delv,
                "award_tid": award,
                "source": "accepted_id",
                "role": "交",
            },
        )

    # 可接探测：仅 task_id 不在 accepted_ids 时标 可接
    probe_ids: list[int] = []
    do_probe = bool(probe_offers) or id_scan is not None or has_dungeon_npc
    if do_probe:
        if id_scan is not None:
            lo, hi = int(id_scan[0]), int(id_scan[1])
            candidates = list(range(lo, hi + 1))
        else:
            candidates = []
            candidates.extend(range(10000, 10055))
            for sid in sorted(accepted_ids):
                for d in range(-12, 13):
                    candidates.append(int(sid) + d)
            if has_dungeon_npc:
                candidates = list(range(10000, 10055)) + candidates
        hits = _probe_delv_offers(
            session,
            nearby_tids=nearby_tids,
            accepted_ids=accepted_ids,
            candidates=candidates,
            max_scan=int(max_scan),
            log=log,
        )
        probe_ids = candidates[: int(max_scan)]
        for off in hits:
            delv = int(off.get("delv_tid") or 0)
            tid = int(off.get("task_id") or 0)
            if not tid or not delv:
                continue
            offer_name = str(off.get("name") or "")
            # 硬规则：已接 id 绝不当可接；进行中在附近列表静默跳过
            if tid in accepted_ids:
                st, can_fin, nm = _status_for_accepted(tid)
                if can_fin:
                    award = int(off.get("award_tid") or 0)
                    ntid = award if award and award in nearby_tids else delv
                    _add_offer(
                        ntid,
                        {
                            "kind": "accepted_link",
                            "task_id": tid,
                            "name": nm or offer_name or f"任务{tid}",
                            "status_text": "可交",
                            "can_finish": True,
                            "delv_tid": delv,
                            "award_tid": award,
                            "source": "accepted_id",
                            "role": "交",
                        },
                    )
                continue
            # 同具体任务已接（探测 id 可能不同，如 限次 变体）
            matched = _match_accepted_by_name(offer_name)
            if matched:
                st, can_fin, nm = _status_for_accepted(matched)
                if can_fin:
                    award = int(off.get("award_tid") or 0)
                    ntid = award if award and award in nearby_tids else delv
                    _add_offer(
                        ntid,
                        {
                            "kind": "accepted_link",
                            "task_id": matched,
                            "name": nm or offer_name or f"任务{matched}",
                            "status_text": "可交",
                            "can_finish": True,
                            "delv_tid": delv,
                            "award_tid": award,
                            "source": "accepted_name",
                            "probed_task_id": tid,
                            "role": "交",
                        },
                    )
                # 未可交：同任务已在进行中，附近不重复展示
                continue
            _add_offer(
                delv,
                {
                    **off,
                    "kind": "available",
                    "status_text": "可接",
                    "can_finish": False,
                    "source": "delv_scan",
                },
            )

    out: list[dict] = []
    for npc in instances:
        ntid = int(npc.get("tid") or 0)
        offers = list(offers_by_tid.get(ntid) or [])
        base = {
            "npc_name": npc.get("name") or f"NPC{ntid}",
            "npc_tid": ntid,
            "dist": npc.get("dist"),
            "ptr": npc.get("ptr"),
            "obj_id": npc.get("obj_id"),
            "x": npc.get("x"),
            "y": npc.get("y"),
            "z": npc.get("z"),
        }
        npc_only_row = {
            **base,
            "kind": "npc_only",
            "task_id": 0,
            "name": "",
            "status_text": "附近NPC",
            "can_finish": False,
            "delv_tid": ntid,
            "award_tid": 0,
            "source": "nearby_npc",
            "role": "",
        }
        if not offers:
            out.append(npc_only_row)
            continue
        added = 0
        for off in offers:
            tid = int(off.get("task_id") or 0)
            # 输出前再按 id / 同具体任务 兜底：进行中不进附近列表
            if off.get("kind") == "available":
                if tid and tid in accepted_ids:
                    st, can_fin, nm = _status_for_accepted(tid)
                    if not can_fin:
                        continue
                    off = {
                        **off,
                        "kind": "accepted_link",
                        "status_text": "可交",
                        "can_finish": True,
                        "name": nm or off.get("name"),
                        "source": "accepted_id",
                        "role": "交",
                    }
                else:
                    matched = _match_accepted_by_name(str(off.get("name") or ""))
                    if matched:
                        st, can_fin, nm = _status_for_accepted(matched)
                        if not can_fin:
                            continue
                        off = {
                            **off,
                            "kind": "accepted_link",
                            "task_id": matched,
                            "status_text": "可交",
                            "can_finish": True,
                            "name": nm or off.get("name"),
                            "source": "accepted_name",
                            "probed_task_id": tid or None,
                            "role": "交",
                        }
            elif (
                off.get("kind") == "accepted_link"
                and not off.get("can_finish")
                and str(off.get("status_text") or "").startswith("进行中")
            ):
                # 兜底：任何残留“进行中”关联都不进附近列表
                continue
            row = {**base, **off}
            # Keep panel compose ``<分类>具体名``; never strip with format_task_display_name.
            raw_name = str(row.get("name") or "").strip()
            if not raw_name and tid:
                try:
                    raw_name = get_task_name(session, tid, log=lambda _m: None)
                except Exception:
                    raw_name = ""
            row["name"] = raw_name or (f"任务{tid}" if tid else "")
            out.append(row)
            added += 1
        # 该 NPC 上的任务全被过滤（如仅剩进行中）时，仍保留为附近NPC行
        if added == 0:
            out.append(npc_only_row)

    # Dedupe: one row per (task_id, npc_tid) for linked/available tasks.
    # Prefer closer instance; for same task prefer can_finish / 交 over 接.
    # Keep all pure npc_only rows (different portal instances).
    deduped: list[dict] = []
    best_task: dict[tuple[int, int], dict] = {}
    for r in out:
        kind = str(r.get("kind") or "")
        tid = int(r.get("task_id") or 0)
        ntid = int(r.get("npc_tid") or 0)
        if kind == "npc_only" or not tid:
            deduped.append(r)
            continue
        key = (tid, ntid)
        prev = best_task.get(key)
        if prev is None:
            best_task[key] = r
            continue

        def _rank(row: dict) -> tuple:
            try:
                d = float(row.get("dist")) if row.get("dist") is not None else 1e9
            except (TypeError, ValueError):
                d = 1e9
            return (
                0 if row.get("can_finish") else 1,
                0 if row.get("role") == "交" else 1,
                d,
                0 if str(row.get("kind")) == "accepted_link" else 1,
            )

        if _rank(r) < _rank(prev):
            best_task[key] = r
    out = list(best_task.values()) + deduped

    def sort_key(r: dict):
        kind_rank = {
            "available": 0,
            "accepted_link": 1 if r.get("can_finish") else 2,
            "npc_only": 3,
        }.get(str(r.get("kind")), 9)
        return (
            kind_rank,
            0 if r.get("can_finish") else 1,
            float(r.get("dist") or 1e9),
            str(r.get("npc_name") or ""),
            int(r.get("task_id") or 0),
            int(r.get("ptr") or 0),
        )

    out.sort(key=sort_key)
    n_offer = sum(1 for r in out if r.get("kind") == "available")
    n_link = sum(1 for r in out if r.get("kind") == "accepted_link")
    ms = int((time.monotonic() - t0) * 1000)
    log(
        f"task_api nearby_offer n={len(out)} offer={n_offer} linked={n_link} "
        f"npc_inst={len(instances)} tid={len(nearby_tids)} "
        f"probed={len(probe_ids)} dungeon={has_dungeon_npc} {ms}ms"
    )
    return out


def _task_mgr_this(session, *, log: LogFn | None = None) -> int:
    """
    Resolve task manager this pointer for jieTask.

    Uses same game root as GetTaskInterface helper:
      root = *global; this = *[root + TASK_MGR_THIS_OFF]
    Prefer reading via GetTaskInterface chain: helper at 0x4AE420 returns
    object whose +0x2C is task mgr (from jieTask site).

    @author by ak
    """
    log = log or (lambda _m: None)
    base = int(getattr(session, "module_base", 0) or 0)
    pid = int(session.pid)
    # call 0x4AE420: mov eax,[global]; [eax+0x24]; [eax+0x90] style — returns host-side-ish
    # From disasm: a1 d8825201; 8b 40 24; 8b 80 90 00 00 00
    pe = find_xajh_exe(getattr(session, "exe_path", None))
    info = resolve_task_patterns(pe)
    this_off = int(info.get("task_mgr_this_off") or TASK_MGR_THIS_OFF)
    # Get root via GetTaskInterface internal path: call 0x4AE420 then walk
    # Export GetTaskInterface already does nested read; for mgr use:
    # call helper 0x4AE420 equals:
    #   eax = *0x15282d8; if eax: eax=[eax+0x24]; if: return [eax+0x90]
    # That is NOT task mgr. jieTask uses:
    #   mov eax, [0x15282d8]; mov ecx, [eax+0x2C]
    # So read global 0x15282d8 via PE pattern at accept site.
    global_va = None
    if pe and pe.is_file():
        resolver = PatternResolver(pe)
        hits = resolver.find_rvas(_PAT_JIE_TASK)
        if len(hits) == 1:
            window = resolver.read_at_rva(hits[0], 64)
            a1 = window.find(b"\xA1")
            if a1 >= 0:
                global_va = struct.unpack_from("<I", window, a1 + 1)[0]
    if not global_va:
        profile = profile_for_client(str(pe)) if pe else None
        entry = profile.symbols.get("game_root_global") if profile else None
        if not isinstance(entry, dict) or entry.get("status") != "verified":
            raise RuntimeError("task game root unavailable for unknown client build")
        global_va = int(profile.fingerprint.image_base) + int(str(entry["rva"]), 0)
    h = _open_process(pid)
    try:
        # if ASLR, global is absolute VA in image — rebase
        # Pattern stored absolute preferred; live = base + (g - 0x400000) if in module range
        g = int(global_va)
        if g >= 0x400000 and g < 0x400000 + 0x2000000:
            g_live = base + (g - 0x400000)
        else:
            g_live = g
        root = struct.unpack("<I", _rpm(h, g_live, 4))[0]
        if not root:
            raise RuntimeError("task game_root null")
        this_p = struct.unpack("<I", _rpm(h, root + this_off, 4))[0]
        log(
            f"task_api mgr this root=0x{root:X} off=0x{this_off:X} this=0x{this_p:X} "
            f"global=0x{g_live:X}"
        )
        return int(this_p) & 0xFFFFFFFF
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h))


def accept_task(
    session,
    task_id: int,
    *,
    hwnd: int = 0,
    npc_id_lo: int = 0,
    npc_id_hi: int = 0,
    log: LogFn | None = None,
    wait_s: float | None = None,
    use_bridge: bool = True,
    allow_remote_fallback: bool = False,
    open_npc_dialog: bool = True,
    fast: bool = False,
) -> TaskOpResult:
    """
    Accept task by id; success when id appears in accepted list.

    Prefer bridge main-thread. Opens NPC dialog first when obj id is known —
    bare jieTask often returns ret=1 while server rejects with 无法使用此任务.
    fast=True: short wait, no second retry (群控副控 / known task id).
    """
    log = log or (lambda _m: None)
    if wait_s is None:
        wait_s = 1.5 if fast else 4.0
    tid = int(task_id) & 0xFFFFFFFF
    before = list_accepted_task_ids(session, log=log)
    if tid in before:
        return TaskOpResult(
            ok=True,
            action="accept",
            task_id=tid,
            before_ids=before,
            after_ids=before,
            note="already accepted",
        )

    npc_id64 = (int(npc_id_hi) << 32) | (int(npc_id_lo) & 0xFFFFFFFF)
    if not npc_id64 and npc_id_lo:
        npc_id64 = int(npc_id_lo) & 0xFFFFFFFF

    ret = None
    err = None
    note = ""
    if open_npc_dialog and npc_id64:
        try:
            dlg_note = _open_task_npc_dialog(
                session,
                npc_id=npc_id64,
                hwnd=int(hwnd or 0),
                settle_s=0.15 if fast else 0.3,
                log=log,
            )
            note = (note + " " + dlg_note).strip()
            time.sleep(0.12 if fast else 0.25)
        except Exception as e:
            log(f"task_api accept open dialog: {e}")
            note = (note + f" open_dialog_fail:{e}").strip()

    if use_bridge:
        try:
            from app.core.xajh_bridge import (
                ensure_bridge,
                last_ensure_bridge_failure,
            )

            bridge = ensure_bridge(
                int(session.pid),
                log=log,
                inject_if_needed=False,
                hwnd=int(hwnd or 0) or None,
            )
            if bridge is None:
                fail_code = last_ensure_bridge_failure(int(session.pid)) or ""
                if "STALE" in fail_code or "LEGACY" in fail_code or "RESTART" in fail_code:
                    raise RuntimeError(
                        "bridge 版本过旧，请完全退出游戏后重开再 Delete 注入"
                    )
                raise RuntimeError("bridge 未就绪，请先 Delete 注入")
            try:
                br = bridge.task_accept(
                    tid,
                    hwnd=int(hwnd or 0),
                    timeout_ms=4000,
                )
                ret = br.ret
                note = ((note + " " + (br.note or "")).strip())
                log(
                    f"task_api accept bridge ok={br.ok} status={br.status} "
                    f"ret={br.ret} error={br.error!r} note={br.note!r}"
                )
                if not br.ok and br.error:
                    err = br.error
            finally:
                bridge.close()
        except Exception as e:
            err = str(e)
            log(f"task_api accept bridge fail: {e}")

    if (not use_bridge or err) and allow_remote_fallback:
        try:
            base = int(session.module_base)
            pe = find_xajh_exe(getattr(session, "exe_path", None))
            info = resolve_task_patterns(pe)
            call_rva = info.get("accept_call_rva")
            if call_rva is None:
                raise RuntimeError("task accept symbol unavailable for this client build")
            func_va = base + int(call_rva)
            this_p = _task_mgr_this(session, log=log)
            if not this_p:
                raise RuntimeError("task mgr this null")
            pid = int(session.pid)
            ret = _accept_remote(pid, func_va, this_p, tid)
            note = (note + " remote_accept").strip()
            err = None
        except Exception as e:
            err = str(e)
            log(f"task_api accept remote fail: {e}")
    elif not use_bridge and err is None:
        err = "task accept requires bridge"

    if err is not None and not allow_remote_fallback:
        return TaskOpResult(
            ok=False,
            action="accept",
            task_id=tid,
            before_ids=before,
            after_ids=before,
            ret=ret,
            error=err,
            note=note,
        )

    # ids-only poll (no CanFinish/GetTaskName)
    after = before
    poll = 0.08 if fast else 0.12
    deadline = time.time() + max(0.3, float(wait_s))
    while time.time() < deadline:
        try:
            after = list_accepted_task_ids(session, log=lambda _m: None)
        except Exception:
            after = before
        if tid in after:
            break
        time.sleep(poll)

    # One retry only on non-fast path (server dialog context).
    if (
        not fast
        and tid not in after
        and open_npc_dialog
        and npc_id64
        and use_bridge
        and err is None
    ):
        try:
            log(f"task_api accept retry after dialog id={tid}")
            dlg2 = _open_task_npc_dialog(
                session,
                npc_id=npc_id64,
                hwnd=int(hwnd or 0),
                settle_s=0.2,
                log=log,
            )
            note = (note + " retry " + dlg2).strip()
            time.sleep(0.25)
            from app.core.xajh_bridge import ensure_bridge

            bridge = ensure_bridge(
                int(session.pid),
                log=log,
                inject_if_needed=False,
                hwnd=int(hwnd or 0) or None,
            )
            if bridge is not None:
                try:
                    br2 = bridge.task_accept(
                        tid,
                        hwnd=int(hwnd or 0),
                        timeout_ms=3000,
                    )
                    ret = br2.ret
                    note = ((note + " " + (br2.note or "")).strip())
                    log(
                        f"task_api accept retry bridge ok={br2.ok} ret={br2.ret} "
                        f"note={br2.note!r}"
                    )
                finally:
                    bridge.close()
            deadline2 = time.time() + max(0.6, float(wait_s) * 0.5)
            while time.time() < deadline2:
                try:
                    after = list_accepted_task_ids(session, log=lambda _m: None)
                except Exception:
                    pass
                if tid in after:
                    break
                time.sleep(poll)
        except Exception as e:
            log(f"task_api accept retry fail: {e}")
            note = (note + f" retry_fail:{e}").strip()

    ok = tid in after and tid not in before
    if ok:
        err = None
    elif err is None and tid not in after:
        if ret == 1:
            err = (
                "接取被拒：本地 ret=1 但未进列表"
                "（游戏常提示「无法使用此任务」：等级/前置/次数/需点叹号对话）"
            )
        else:
            err = "accept timeout: not in accepted list"

    return TaskOpResult(
        ok=ok,
        action="accept",
        task_id=tid,
        before_ids=before,
        after_ids=after,
        ret=ret,
        error=err,
        note=note,
    )


def accept_task_routed(
    session,
    task_id: int,
    *,
    hwnd: int = 0,
    npc_radius: float = 120.0,
    npc_wait_s: float | None = None,
    npc_poll_s: float = 0.5,
    path_arrive_radius: float = 12.0,
    path_timeout_s: float | None = None,
    path_poll_s: float = 0.5,
    require_nearby_npc: bool = True,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    fast: bool = False,
) -> TaskOpResult:
    """Accept when DelvNPC is nearby; otherwise path then accept via bridge.

    fast=True (群控副控): known task_id — no pathfind, only nearby NPC for
    dialog if present; game handles movement on slave.
    """
    log = log or (lambda _m: None)
    if npc_wait_s is None:
        npc_wait_s = 3.0 if fast else 20.0
    if path_timeout_s is None:
        path_timeout_s = 90.0
    tid = int(task_id) & 0xFFFFFFFF
    before = list_accepted_task_ids(session, log=log)
    if tid in before:
        return TaskOpResult(
            ok=True,
            action="accept",
            task_id=tid,
            before_ids=before,
            after_ids=before,
            note="already accepted",
        )

    def fail(error: str, note: str = "") -> TaskOpResult:
        return TaskOpResult(
            ok=False,
            action="accept",
            task_id=tid,
            before_ids=before,
            after_ids=before,
            error=error,
            note=note,
        )

    meta = read_task_npc_tids(session, tid, log=log)
    delv_tid = int(meta.get("delv_tid") or 0)
    npc = None
    if delv_tid:
        npc = find_nearby_task_npc(
            session, delv_tid, radius=float(npc_radius), log=log
        )
        if npc is not None:
            log(
                f"task_api accept nearby id={tid} delv_tid={delv_tid} "
                f"dist={npc.get('dist')} npc_ptr=0x{int(npc.get('ptr') or 0):X}"
            )
        elif not fast:
            # 主控 UI：可寻路到 DelvNPC
            cfg = get_task_npc_world(session, delv_tid, log=log)
            if (
                not cfg
                or not cfg.get("ok")
                or cfg.get("x") is None
                or cfg.get("z") is None
            ):
                if require_nearby_npc:
                    return fail(
                        f"DelvNPC tid={delv_tid} not nearby and has no world position"
                    )
            else:
                clue = {
                    "clue": "接任务NPC",
                    "kind": "cfg_npc",
                    "name": f"tid{delv_tid}",
                    "x": float(cfg["x"]),
                    "y": float(cfg.get("y") or 0.0),
                    "z": float(cfg["z"]),
                    "tid": delv_tid,
                    "scene_id": int(cfg.get("scene_id") or 0) or None,
                    "source": "cfg_object",
                }
                moved = pathfind_to_clue(
                    session,
                    clue,
                    hwnd=int(hwnd or 0),
                    arrive_radius=float(path_arrive_radius),
                    verify_timeout_s=float(path_timeout_s),
                    poll_s=float(path_poll_s),
                    stop_event=stop_event,
                    log=log,
                )
                if not moved.get("ok"):
                    return fail(
                        str(moved.get("error") or "failed to reach DelvNPC"),
                        note=f"DelvNPC tid={delv_tid}; command_ok={moved.get('command_ok')}",
                    )

                deadline = time.monotonic() + max(0.1, float(npc_wait_s))
                interval = max(0.05, float(npc_poll_s))
                while time.monotonic() < deadline:
                    if stop_event is not None and stop_event.is_set():
                        return fail("task accept stopped while waiting for DelvNPC")
                    npc = find_nearby_task_npc(
                        session, delv_tid, radius=float(npc_radius), log=lambda _m: None
                    )
                    if npc is not None:
                        break
                    if stop_event is not None:
                        stop_event.wait(interval)
                    else:
                        time.sleep(interval)
        else:
            log(
                f"task_api accept fast id={tid} delv={delv_tid} "
                f"not nearby — direct accept (no pathfind)"
            )
    elif require_nearby_npc and not fast:
        return fail("task has no verified DelvNPC")

    # 主控：附近门控；副控 fast：允许无 NPC 直调（主控已验证任务可用）
    if require_nearby_npc and npc is None and not fast:
        return fail(
            f"DelvNPC tid={delv_tid or '?'} not found nearby",
            note="stand next to the quest NPC then retry",
        )

    npc_id_lo = 0
    npc_id_hi = 0
    if npc is not None:
        oid = int(npc.get("obj_id") or 0)
        npc_id_lo = oid & 0xFFFFFFFF
        npc_id_hi = (oid >> 32) & 0xFFFFFFFF
        log(
            f"task_api accept routed id={tid} delv_tid={delv_tid} "
            f"npc_ptr=0x{int(npc.get('ptr') or 0):X} dist={npc.get('dist')} "
            f"obj_id=0x{oid:X}"
        )
    else:
        log(f"task_api accept direct id={tid} fast={fast}")
    return accept_task(
        session,
        tid,
        hwnd=int(hwnd or 0),
        npc_id_lo=npc_id_lo,
        npc_id_hi=npc_id_hi,
        log=log,
        use_bridge=True,
        allow_remote_fallback=False,
        open_npc_dialog=bool(npc_id_lo or npc_id_hi),
        fast=bool(fast),
        wait_s=1.2 if fast else None,
    )


@dataclass
class TaskRunnerConfig:
    """Conservative automatic task routing and turn-in settings."""

    npc_radius: float = 120.0
    npc_wait_s: float = 120.0
    npc_poll_s: float = 1.0
    idle_s: float = 1.0
    complete_wait_s: float = 5.0
    path_arrive_radius: float = 12.0
    path_timeout_s: float = 120.0
    path_poll_s: float = 0.5

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TaskStepEvent:
    """One observable step from TaskRunner."""

    phase: str
    message: str
    ok: bool = True
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def choose_task_route_clue(
    clues: list[dict],
    *,
    for_complete: bool,
    npc_tid: int = 0,
) -> dict | None:
    """Choose a deterministic route/live NPC clue for one task."""
    rows = [dict(c) for c in clues or []]
    if not rows:
        return None
    want_tid = int(npc_tid or 0)

    def score(c: dict) -> tuple[int, float]:
        src = str(c.get("source") or "")
        clue = str(c.get("clue") or "")
        tid = int(c.get("tid") or 0)
        live = bool(c.get("ptr")) and bool(c.get("obj_id"))
        has_pos = c.get("x") is not None and c.get("z") is not None
        tid_match = not want_tid or tid == want_tid
        if for_complete:
            if live and tid_match:
                pri = 0
            elif src == "template_tid" and tid_match:
                pri = 1
            elif src == "cfg_object" and tid_match:
                pri = 2
            elif src == "reach_site" and has_pos:
                pri = 3
            elif "交任务" in clue and has_pos:
                pri = 4
            else:
                pri = 8
        else:
            # incomplete: Delv/template first; never walk random city NPCs
            if src == "reach_site" and has_pos:
                pri = 0
            elif src == "template_tid" and has_pos and tid_match:
                pri = 1
            elif src == "cfg_object" and has_pos and tid_match:
                pri = 2
            elif src == "template_tid" and has_pos:
                pri = 3
            elif src == "cfg_object" and has_pos:
                pri = 4
            elif src == "nearby" and has_pos and tid_match and want_tid:
                pri = 5
            elif (
                has_pos
                and want_tid
                and want_tid in PORTAL_TID_TIER
                and "地宫传送" in str(c.get("name") or "")
            ):
                pri = 1 if tid_match else 5
            elif has_pos and want_tid and not tid_match:
                # known delv/award but wrong entity → refuse (prevent same-point path)
                pri = 8
            elif has_pos:
                pri = 6
            else:
                pri = 8
        try:
            dist = float(c.get("dist")) if c.get("dist") is not None else 1e9
        except (TypeError, ValueError):
            dist = 1e9
        return pri, dist

    rows.sort(key=score)
    best = rows[0]
    return best if score(best)[0] < 8 else None


def _task_route_trace(
    clues: list[dict],
    *,
    for_complete: bool,
    npc_tid: int,
    selected: dict | None,
) -> list[dict]:
    """Return a compact, serializable record of task-route candidates."""
    chosen = selected or {}
    rows: list[dict] = []
    for clue in clues or []:
        c = dict(clue or {})
        rows.append(
            {
                "selected": c == chosen,
                "source": c.get("source"),
                "clue": c.get("clue"),
                "name": c.get("name"),
                "tid": c.get("tid"),
                "scene_id": c.get("scene_id"),
                "world_id": c.get("world_id"),
                "x": c.get("x"),
                "y": c.get("y"),
                "z": c.get("z"),
                "dist": c.get("dist"),
                "has_live_identity": bool(c.get("ptr")) and bool(c.get("obj_id")),
                "phase": "turnin" if for_complete else "objective",
                "wanted_npc_tid": int(npc_tid or 0),
            }
        )
    return rows


def classify_task_portal_kind(task: TaskInfo | dict) -> str:
    """
    Classify accepted task for portal shortcut (not free teleport).

    Returns one of: dungeon_upper | dungeon_deep | tianxiahui | ""
    - 杀怪 → 地宫上层
    - BOSS → 地宫深处
    - 天下会 → 宋瑶
    """
    row = task.to_dict() if isinstance(task, TaskInfo) else dict(task or {})
    explicit_kind = str(row.get("portal_kind") or "").strip()
    if explicit_kind in ("dungeon_upper", "dungeon_deep", "tianxiahui"):
        return explicit_kind
    name = str(row.get("name") or "")
    npc = str(row.get("npc_name") or "")
    # panel/story fields if present (任务详情「升级地宫深处」)
    extra = " ".join(
        str(row.get(k) or "")
        for k in ("category", "status_text", "story", "desc", "detail", "clue")
    )
    blob = f"{name} {npc} {extra}"
    # Title/category first: 杀怪 always 上层 (clue text often contains 「深处」
    # as flavor and must NOT force deep — live bug 2026-07-24 初级地宫杀怪).
    name_cat = f"{name} {row.get('category') or ''}"
    if "杀怪" in name_cat and not any(
        k in name_cat for k in ("BOSS", "Boss", "boss")
    ):
        return "dungeon_upper"
    # BOSS / explicit deep titles
    if any(
        k in blob
        for k in (
            "BOSS",
            "Boss",
            "boss",
            "地宫BOSS",
            "每日BOSS",
        )
    ):
        return "dungeon_deep"
    # 深处/下层 only when not a 杀怪 title
    if any(k in name_cat for k in ("地宫深处", "升级地宫深处", "深处", "下层")):
        return "dungeon_deep"
    if any(k in blob for k in ("地宫上层", "上层", "副本")):
        return "dungeon_upper"
    # bare 「地宫」 alone is ambiguous — only upper if 杀怪-like already handled
    if "地宫" in name and "BOSS" not in name.upper():
        return "dungeon_upper"
    if "天下会" in name or "天下会" in npc:
        return "tianxiahui"
    if "地宫传送" in npc:
        # NPC alone: do not force upper; leave empty so caller can pass kind
        return ""
    if "宋瑶" in npc and not any(k in name for k in ("杀怪", "BOSS", "地宫")):
        return "tianxiahui"
    return ""


def tier_from_portal_tid(tid: int | None) -> str:
    """Map live portal NPC tid → 初/中/高/升级."""
    try:
        return str(PORTAL_TID_TIER.get(int(tid or 0), "") or "")
    except (TypeError, ValueError):
        return ""


def resolve_dungeon_tier(
    task: TaskInfo | dict | str | None = None,
    prefer_tid: int | None = 0,
) -> str:
    """
    Prefer live DelvNPC/AwardNPC portal tid over task-name heuristics.

    龙傲天 BOSS name has no 初/中/高 → classify falls to 升级, but delv=100219
    is the 初级 portal. Tid wins when known.
    """
    try:
        pt = int(prefer_tid or 0)
    except (TypeError, ValueError):
        pt = 0
    by_tid = tier_from_portal_tid(pt)
    if by_tid:
        return by_tid
    return classify_dungeon_tier(task)


def classify_dungeon_tier(task: TaskInfo | dict | str | None = None) -> str:
    """
    Extract dungeon tier for daily 地宫 tasks.

    Returns one of: 初级 | 中级 | 高级 | 升级 | 练级 | ""
    升级/练级 = 练级地宫 (not 初级/中级/高级每日杀怪).
    """
    if isinstance(task, str):
        blob = task
    else:
        row = task.to_dict() if isinstance(task, TaskInfo) else dict(task or {})
        blob = " ".join(
            str(row.get(k) or "")
            for k in (
                "name",
                "npc_name",
                "category",
                "status_text",
                "story",
                "desc",
                "detail",
                "clue",
            )
        )
    s = str(blob or "")
    # Explicit 升级/练级 portal tasks
    if any(
        k in s
        for k in (
            "升级地宫深处",
            "升级地宫上层",
            "升级地宫传送",
            "练级地宫",
        )
    ):
        return "升级"
    # 每日BOSS: 初级/中级/高级地宫BOSS → 对应传送口；无名BOSS(余沧海)→升级
    if "BOSS" in s.upper() or "每日BOSS" in s:
        for tr in ("初级", "中级", "高级"):
            if tr in s:
                return tr
        return "升级"
    if "练级" in s and "地宫" in s:
        return "升级"
    if "升级地宫" in s and "杀怪" not in s:
        return "升级"
    for tier in ("初级", "中级", "高级"):
        if tier in s:
            return tier
    if "升级" in s and any(k in s for k in ("地宫", "BOSS", "深处")):
        # e.g. 升级地宫深处 BOSS target — not a portal tier pick
        return "升级"
    return ""


# Menu / scene name hints per tier (live scene_map + common 地宫 maps).
# Live delv/award tid → 地宫传送 instance (福州 2026-07-22)
# 中级杀怪10008→100218; 初级BOSS→100219; 高级→100220; 余沧海升级→100221
PORTAL_TID_TIER: dict[int, str] = {
    100218: "中级",
    100219: "初级",
    100220: "高级",
    100221: "升级",
}

# ---------------------------------------------------------------------------
# 地宫入口 NPC 传送封包 (2026-08-06 captured)
#   dialog = 打开该 NPC 对话 ; layer = 通用上层/深处 选择封包。
#   流程：到达 NPC 身边 → 发对话封包 → 发 上层/深处 封包 → 等待场景切换。
#   群控友好：封包为固定字节，仅按角色 pid 发送，主控同步层/档位即可。
# ---------------------------------------------------------------------------
# 福州每日地宫 NPC 的实时服务 ID → 两段会话包。
# 抓包确认：先发 0A，再发同 ID 的 0C；D9~DC 是小端 ID 2009~2012。
DUNGEON_ENTRY_IDS_BY_TIER: dict[str, int] = {
    "中级": 0x07D9,
    "初级": 0x07DA,
    "高级": 0x07DB,
    "升级": 0x07DC,
    "练级": 0x07DC,
}
DUNGEON_ENTRY_OPEN_PACKET_BY_ID: dict[int, bytes] = {
    entry_id: bytes.fromhex(f"0A00{entry_id & 0xFF:02X}{entry_id >> 8:02X}000000000001")
    for entry_id in set(DUNGEON_ENTRY_IDS_BY_TIER.values())
}
DUNGEON_ENTRY_TALK_PACKET_BY_ID: dict[int, bytes] = {
    entry_id: bytes.fromhex(f"0C00{entry_id & 0xFF:02X}{entry_id >> 8:02X}000000000001")
    for entry_id in set(DUNGEON_ENTRY_IDS_BY_TIER.values())
}
DUNGEON_ENTRY_TALK_PACKET_BY_TID: dict[int, bytes] = {
    0x01B4: bytes.fromhex("0C00B401000000000001"),
    0x01B5: bytes.fromhex("0C00B501000000000001"),
    0x01B6: bytes.fromhex("0C00B601000000000001"),
    0x01B7: bytes.fromhex("0C00B701000000000001"),
}
DUNGEON_TALK_PACKET_BY_TIER: dict[str, bytes] = {
    tier: DUNGEON_ENTRY_TALK_PACKET_BY_ID[entry_id]
    for tier, entry_id in DUNGEON_ENTRY_IDS_BY_TIER.items()
}
DUNGEON_OPEN_PACKET_BY_TIER: dict[str, bytes] = {
    tier: DUNGEON_ENTRY_OPEN_PACKET_BY_ID[entry_id]
    for tier, entry_id in DUNGEON_ENTRY_IDS_BY_TIER.items()
}
# 旧模板 tid 仍用于候选筛选；实际会话包按实时 ID 发送。
DUNGEON_ENTRY_TIDS: frozenset[int] = frozenset({0x01B4, 0x01B5, 0x01B6, 0x01B7})

# 通用层封包
DUNGEON_LAYER_PACKET_UPPER = bytes.fromhex("0E000500000000")
DUNGEON_LAYER_PACKET_DEEP = bytes.fromhex("0E000501000000")

# 对话封包发出后、层封包发出前的沉降时间（沿用到达后开对话的节奏）
DUNGEON_PACKET_TALK_SETTLE_S = 1.0


def _dungeon_tier_for_portal(portal: dict) -> str:
    """Resolve the dungeon tier from live ID, task tid, or service name."""
    try:
        oid = int(portal.get("obj_id") or 0) & 0xFFFFFFFF
    except (TypeError, ValueError):
        oid = 0
    for tier, entry_id in DUNGEON_ENTRY_IDS_BY_TIER.items():
        if oid == entry_id:
            return tier
    try:
        tids = (
            int(portal.get("tid") or 0),
            int(portal.get("prefer_tid") or 0),
            int(portal.get("delv_tid") or 0),
        )
    except (TypeError, ValueError):
        tids = ()
    legacy_tier_by_tid = {
        0x01B4: "中级",
        0x01B5: "初级",
        0x01B6: "高级",
        0x01B7: "升级",
    }
    for task_tid in tids:
        tier = PORTAL_TID_TIER.get(task_tid) or legacy_tier_by_tid.get(task_tid)
        if tier:
            return tier
    name = str(portal.get("name") or portal.get("npc_name") or "")
    if "野人峡谷" in name:
        return "升级"
    match = re.search(r"(初级|中级|高级|升级)地宫传送", name)
    return match.group(1) if match else ""


def dungeon_open_packet_for(portal: dict) -> bytes | None:
    """Return the matching 0A pre-dialog packet."""
    talk = dungeon_talk_packet_for(portal)
    if not talk:
        return None
    return bytes([0x0A]) + talk[1:]


def dungeon_talk_packet_for(portal: dict) -> bytes | None:
    """Resolve live service IDs and legacy template tids separately."""
    for key in ("tid", "npc_tid"):
        try:
            value = int(portal.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value in DUNGEON_ENTRY_TALK_PACKET_BY_TID:
            return DUNGEON_ENTRY_TALK_PACKET_BY_TID[value]
    for key in ("tid", "prefer_tid", "delv_tid", "award_tid"):
        try:
            value = int(portal.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        tier = PORTAL_TID_TIER.get(value)
        if tier:
            entry_id = DUNGEON_ENTRY_IDS_BY_TIER.get(tier)
            if entry_id:
                return DUNGEON_ENTRY_TALK_PACKET_BY_ID.get(entry_id)
    try:
        oid = int(portal.get("obj_id") or 0) & 0xFFFFFFFF
    except (TypeError, ValueError):
        oid = 0
    if oid in DUNGEON_ENTRY_TALK_PACKET_BY_TID:
        return DUNGEON_ENTRY_TALK_PACKET_BY_TID[oid]
    nm = str(portal.get("name") or portal.get("npc_name") or "")
    if "野人峡谷" in nm:
        return DUNGEON_ENTRY_TALK_PACKET_BY_TID[0x01B7]
    match = re.search(r"(初级|中级|高级|升级)地宫传送", nm)
    if match:
        tier = match.group(1)
        entry_id = DUNGEON_ENTRY_IDS_BY_TIER.get(tier)
        return DUNGEON_ENTRY_TALK_PACKET_BY_ID.get(entry_id) if entry_id else None
    return None

def dungeon_layer_packet_for(kind: str) -> bytes | None:
    """上层/深处 层封包 by portal kind. @author by ak"""
    k = str(kind or "").strip().lower()
    if k in ("dungeon_deep", "dungeon_lower", "deep", "lower"):
        return DUNGEON_LAYER_PACKET_DEEP
    if k in ("dungeon_upper", "upper"):
        return DUNGEON_LAYER_PACKET_UPPER
    return None


PORTAL_TIER_MAP_KEYS: dict[str, tuple[str, ...]] = {
    "初级": ("初级", "地火", "苦水", "康巴"),
    "中级": ("中级", "野人"),
    "高级": ("高级", "俺答", "汗陵", "西王母"),
    "升级": ("升级", "练级", "西王母"),
    "练级": ("练级", "升级", "西王母"),
}

# NPC name markers for 练级地宫 portal (must not be used for 初/中/高级每日杀怪).
PORTAL_LEVELING_NPC_KEYS: tuple[str, ...] = (
    "练级地宫",
    "练级",
    "升级地宫传送",
    "升级地宫",
)


def _npc_name_is_leveling_portal(name: str) -> bool:
    nm = str(name or "")
    if not nm:
        return False
    if "地宫传送" in nm or "地宫" in nm:
        return any(k in nm for k in PORTAL_LEVELING_NPC_KEYS)
    return any(k in nm for k in ("练级地宫", "练级传送"))


def _portal_texts_match_tier(texts: list[str], tier: str) -> bool | None:
    """
    Soft check for portal *service title* only.

    Live scan mixes noise (镖局…初级…ecp / <每日BOSS>中级地宫BOSS / 其它NPC缓存).
    Only trust compact titles: 「初级|中级|高级|升级地宫传送」.
    Return None when unsure — caller must not hard-fail on None.
    """
    tier = str(tier or "").strip()
    if not tier:
        return None
    titles: list[str] = []
    for raw in texts or []:
        s = str(raw or "").strip()
        if not s or len(s) > 16:
            continue
        # reject obvious noise
        if any(k in s for k in (".ecp", "BOSS", "镖局", "发放任务", "等级", "监工", "天下会")):
            continue
        m = re.search(r"(初级|中级|高级|升级|练级)地宫传送", s)
        if m:
            titles.append(m.group(1))
            continue
        m = re.search(r"^(初级|中级|高级|升级|练级)地宫$", s)
        if m:
            titles.append(m.group(1))
    want = "升级" if tier in ("升级", "练级") else tier
    blob = " ".join(str(x or "") for x in (texts or []))
    # Map destination names are stronger than polluted 「X级地宫传送」titles
    # (e.g. 野人峡谷上层/深处 ⇒ 中级 even if scan also saw 高级地宫传送).
    map_keys = [
        k
        for k in PORTAL_TIER_MAP_KEYS.get(want, ())
        if k not in ("初级", "中级", "高级", "升级", "练级", "地宫")
    ]
    if map_keys and any(k and k in blob for k in map_keys):
        return True
    if not titles:
        return None
    # map 练级 → 升级
    norm = ["升级" if x == "练级" else x for x in titles]
    if want in norm:
        # ok if our title present (even if noise titles also exist)
        return True
    # only other titles
    if norm and want not in norm:
        return False
    return None



def _portal_has_map_layer_line(texts: list[str]) -> bool:
    """Map-named layer option (康巴水寨上层 / 野人峡谷深处). May be task pollution."""
    blob = " ".join(str(x or "") for x in (texts or []))
    for noise in (
        "中级地宫BOSS",
        "初级地宫BOSS",
        "高级地宫BOSS",
        "每日BOSS",
        "每日杀怪",
        "发放任务",
    ):
        blob = blob.replace(noise, "")
    return bool(
        re.search(r"(峡谷|陵|水寨|沼泽|剑池|宫|古城).{0,6}(上层|深处|下层)", blob)
    )


def _portal_dest_lines_ready(texts: list[str]) -> bool:
    """
    Strong dest-ready: BOTH 上层 and 深处 (real submenu / 升级直达).

    Do NOT treat lone map-name (康巴水寨上层) as ready before entry —
    live scan mixes task panel pollution with NPC dialog (2026-07-22).
    """
    blob = " ".join(str(x or "") for x in (texts or []))
    if "发放任务" in blob:
        blob = blob.replace("初级地宫发放任务", "").replace("升级地宫发放任务", "")
    for noise in ("中级地宫BOSS", "初级地宫BOSS", "高级地宫BOSS", "每日BOSS", "每日杀怪"):
        blob = blob.replace(noise, "")
    has_up = bool(re.search(r"(峡谷|地宫|陵|水寨|沼泽|剑池|宫|古城).{0,6}上层", blob)) or (
        "地宫上层" in blob
    )
    has_deep = bool(
        re.search(r"(峡谷|地宫|陵|水寨|沼泽|剑池|宫|古城).{0,6}(深处|下层)", blob)
    ) or ("地宫深处" in blob) or ("升级地宫深处" in blob)
    if has_up and has_deep:
        return True
    ups = sum(1 for x in texts or [] if re.search(r"上层$", str(x or "").strip()))
    deeps = sum(
        1
        for x in texts or []
        if re.search(r"(深处|下层)$", str(x or "").strip())
    )
    return ups >= 1 and deeps >= 1


def _portal_entry_label_for_tier(tier: str) -> list[str]:
    """Intermediate menu option titles (非升级: 先点「高级地宫传送」再选层)."""
    tier = str(tier or "").strip()
    # Never use bare 「X级地宫」— matches 中级地宫BOSS task title pollution.
    if tier in ("初级", "中级", "高级"):
        return [f"{tier}地宫传送", "地宫传送"]
    if tier in ("升级", "练级"):
        return ["升级地宫传送", "练级地宫传送", "地宫传送"]
    return ["地宫传送", "传送服务"]


def _portal_taskish_row_count(texts: list[str]) -> int:
    """How many task-like talk rows sit above 地宫传送 on incomplete NPC mouths."""
    n = 0
    seen: set[str] = set()
    for raw in texts or []:
        s = str(raw or "").strip()
        if not s or s in seen:
            continue
        if any(t in s for t in ("杀怪", "发放任务", "BOSS", "boss", "兑换")):
            # short fragments like 「初级」alone do not count
            if len(s) < 4:
                continue
            seen.add(s)
            n += 1
    return n


def _portal_has_layer_side(texts: list[str], side: str) -> bool:
    side = str(side or "").lower()
    want_deep = side in ("deep", "dungeon_deep", "dungeon_lower", "lower")
    for raw in texts or []:
        s = str(raw or "")
        if want_deep and (("深处" in s) or ("下层" in s)):
            return True
        if (not want_deep) and ("上层" in s):
            return True
    return False


def _portal_has_service_entry_texts(texts: list[str], tier: str = "") -> bool:
    tier = str(tier or "").strip()
    for raw in texts or []:
        s = str(raw or "")
        if any(t in s for t in ("杀怪", "BOSS", "boss", "任务", "发放", "兑换")):
            continue
        if tier and f"{tier}地宫传送" in s:
            return True
        if s in ("地宫传送", "传送服务") or (
            s.endswith("地宫传送") and "BOSS" not in s
        ):
            return True
    return False


def _portal_entry_slot_guess(texts: list[str], *, entry_clicks: int = 0) -> list[int]:
    """
    Guess UI row for 「X级地宫传送」when NPC still has tasks.

    Live incomplete mouths typically list task rows first, then 地宫传送.
    Scan order is NOT UI order — estimate by taskish count, then rotate on retries.
    """
    task_n = _portal_taskish_row_count(texts)
    base = min(4, max(0, int(task_n)))
    # rotate prefer on retries so we do not stick on 杀怪 row0
    rot = [
        base,
        min(4, base + 1),
        max(0, base - 1),
        2,
        1,
        3,
        0,
        4,
    ]
    # shift by entry_clicks
    shift = max(0, int(entry_clicks)) % max(1, len(rot))
    ordered = rot[shift:] + rot[:shift]
    out: list[int] = []
    for i in ordered:
        ii = int(i)
        if 0 <= ii <= 4 and ii not in out:
            out.append(ii)
    return out or [0]


def _portal_is_task_only_panel(texts: list[str], tier: str = "") -> bool:
    """True when panel looks like task accept/kill only, no portal entry/dest."""
    if _portal_has_service_entry_texts(texts, tier):
        return False
    if _portal_dest_lines_ready(texts):
        return False
    if _portal_has_layer_side(texts, "upper") or _portal_has_layer_side(texts, "deep"):
        return False
    blob = " ".join(str(x or "") for x in (texts or []))
    if any(t in blob for t in ("发放任务", "杀怪", "进入")) and "地宫传送" not in blob:
        return True
    return False


def _pick_portal_npc(
    npcs: list[dict],
    kind: str,
    tier: str = "",
    prefer_tid: int = 0,
) -> dict | None:
    """Pick best nearby portal NPC for kind + dungeon tier / delv tid."""
    cands = list_portal_npc_candidates(
        npcs, kind, tier=tier, prefer_tid=prefer_tid
    )
    return cands[0] if cands else None


def list_portal_npc_candidates(
    npcs: list[dict],
    kind: str,
    *,
    tier: str = "",
    prefer_tid: int = 0,
    max_n: int = 6,
) -> list[dict]:
    """
    Rank portal NPCs.

    Live (福州): multiple 地宫传送 share the same display name; distinguish by
    tid. Task DelvNPC/AwardNPC tid (e.g. 中级杀怪 10008 → delv=100218) is the
    authoritative portal instance. Prefer that tid first.
    """
    if not npcs:
        return []
    rows = list(npcs)
    tier = str(tier or "").strip()
    prefer_tid = int(prefer_tid or 0)

    def dist_of(r: dict) -> float:
        try:
            return float(r.get("dist"))
        except (TypeError, ValueError):
            return 1e9

    if kind not in ("dungeon_upper", "dungeon_deep", "dungeon_lower"):
        if kind == "tianxiahui":
            hits = [r for r in rows if "宋瑶" in str(r.get("name") or "")]
            hits.sort(key=dist_of)
            return hits[: max(1, int(max_n))]
        return []

    portals: list[dict] = []
    for r in rows:
        nm = str(r.get("name") or "")
        try:
            rtid = int(r.get("tid") or 0)
        except (TypeError, ValueError):
            rtid = 0
        # Name may fail to resolve (shown as tidXXXX) — still accept known portal tids.
        if (
            rtid in PORTAL_TID_TIER
            or rtid in DUNGEON_ENTRY_TIDS
            or (prefer_tid and rtid == prefer_tid)
            or "地宫传送" in nm
            or "地宫" in nm
            or "练级" in nm
        ):
            portals.append(r)
    if not portals:
        return []

    daily_tiers = ("初级", "中级", "高级")
    scored: list[tuple[int, float, dict]] = []
    for r in portals:
        nm = str(r.get("name") or "")
        try:
            rtid = int(r.get("tid") or 0)
        except (TypeError, ValueError):
            rtid = 0
        is_lv = _npc_name_is_leveling_portal(nm)
        # Daily 初/中/高级 must not use 练级地宫 NPC
        if tier in daily_tiers and is_lv:
            continue
        # Task Delv/Award tid is ground truth (same display name, different tid)
        if prefer_tid and rtid == prefer_tid:
            rank = -100
        elif tier in ("练级", "升级"):
            if is_lv or any(k in nm for k in ("升级", "练级")):
                rank = 0
            elif "地宫传送" in nm:
                rank = 30
            else:
                rank = 40
        else:
            rank = 50
            if tier and tier in nm:
                rank = 0
            elif tier:
                for i, t2 in enumerate(daily_tiers):
                    if t2 != tier and t2 in nm:
                        rank = 20 + i
                        break
            if kind in ("dungeon_deep", "dungeon_lower") and any(
                k in nm for k in ("深处", "下层")
            ):
                rank = min(rank, 5)
            if kind == "dungeon_upper" and "上层" in nm:
                rank = min(rank, 5)
            if "地宫传送" in nm and not is_lv:
                rank = min(rank, 10)
            # slight demote when prefer_tid known but this is another instance
            if prefer_tid and rtid and rtid != prefer_tid:
                rank = max(rank, 15)
        scored.append((rank, dist_of(r), r))

    # If exclusion removed everyone, fall back to non-leveling only, then any
    if not scored:
        for r in portals:
            nm = str(r.get("name") or "")
            if tier in daily_tiers and _npc_name_is_leveling_portal(nm):
                continue
            scored.append((50, dist_of(r), r))
    if not scored:
        scored = [(50, dist_of(r), r) for r in portals]

    scored.sort(key=lambda x: (x[0], x[1]))
    # Prefer-tid is ground truth (福州多口同名). When DelvNPC tid is present
    # and matched, never poll other tiers (中级 must not try 升级/高级口).
    if prefer_tid:
        preferred = [
            (rank, dist, r)
            for rank, dist, r in scored
            if int(r.get("tid") or 0) == prefer_tid
        ]
        if preferred:
            scored = preferred
            max_n = 1
    out: list[dict] = []
    seen: set[int] = set()
    for rank, dist, r in scored:
        key = int(r.get("obj_id") or r.get("ptr") or r.get("tid") or id(r))
        if key in seen:
            continue
        seen.add(key)
        rr = dict(r)
        rr["_portal_rank"] = rank
        out.append(rr)
        if len(out) >= max(1, int(max_n)):
            break
    return out



# NPC portal dialogs / list controls (Angelica AUI live names).
PORTAL_NPC_DLG_NAMES: tuple[str, ...] = (
    "Win_NPC",
    "Win_NPCContent",
    "Win_NPCTrans",
    "Win_NPCTemplate",
    "Win_NPCTalk",
)
PORTAL_LIST_CTRL_NAMES: tuple[str, ...] = (
    "Lst_Main",
    "Lst_Content",
    "Lst_ContentList",
    "Lst_Item",
    "Lst_List",
    "Sub_List",
    "Lst_Transfer1",
    "Lst_Transfer2",
    "Lst_Transfer0",
    "Lst_Transfer",
    "Lst_Quest",
    "Lst_Missions",
)
PORTAL_BTN_CTRL_NAMES: tuple[str, ...] = (
    "Btn_Trans1",
    "Btn_Trans2",
    "Btn_Trans3",
    "Btn_Trans4",
    "Btn_Trans",
    "Btn_Function",
    "Btn_Enter",
    "Btn_Ok",
    "Btn_OK",
)

# Name-based option match (user-confirmed fallback labels).
# Exact client strings may vary (e.g. 初级地宫上层); match by keyword containment.
PORTAL_NAME_UPPER = ("地宫上层", "上层", "一层")
# 升级/高级地宫深处 first — 每日BOSS 余沧海等目标文案含「升级地宫深处」
PORTAL_NAME_DEEP = (
    "升级地宫深处",
    "地宫深处",
    "深处",
    "下层",
    "三层",
)
PORTAL_NAME_ENTRY = ("地宫传送", "传送服务", "传送", "进入地宫", "进入")

# Live scene_map.json (地宫/秘境层): 上层 vs 下层 vs (深处)
PORTAL_UPPER_SCENE_IDS = frozenset({2014, 2016, 2018, 2020, 2022, 2024})
PORTAL_LOWER_SCENE_IDS = frozenset({2015, 2017, 2019, 2021, 2023, 2025})
PORTAL_DEEP_SCENE_IDS = frozenset(
    {
        1527,
        2028,
        2029,
        2030,
        2031,
        2032,
        2033,
        2034,
        2035,
        2036,
        2037,
    }
)


def portal_menu_keywords(kind: str) -> list[list[str]]:
    """
    Ordered keyword stages for portal NPC service menus.

    Name-based judgment (confirmed):
      - 杀怪/上层 → keywords containing 上层
      - BOSS/深处 → keywords containing 深处
    Stage0 opens transfer entry if needed; stage1 picks destination by name.
    """
    k = str(kind or "").strip().lower()
    if k in ("dungeon_deep", "dungeon_lower"):
        return [list(PORTAL_NAME_ENTRY), list(PORTAL_NAME_DEEP)]
    if k == "tianxiahui":
        return [["天下会", "传送", "进入"], ["天下会", "进入", "传送"]]
    # dungeon_upper / default
    return [list(PORTAL_NAME_ENTRY), list(PORTAL_NAME_UPPER)]


def _portal_dest_keywords(kind: str) -> list[str]:
    """Destination option names for kind (上层 vs 深处)."""
    k = str(kind or "").strip().lower()
    if k in ("dungeon_deep", "dungeon_lower"):
        return list(PORTAL_NAME_DEEP)
    if k == "tianxiahui":
        return ["天下会", "进入", "传送"]
    return list(PORTAL_NAME_UPPER)


def _portal_entry_keywords() -> list[str]:
    return list(PORTAL_NAME_ENTRY)


def _scene_id_now(session, *, fresh: bool = False) -> int | None:
    """Positive scene id only; -1/0 during transfer is treated as unknown.

    fresh=False: 非马上需要，可走 state 短缓存；transfer 等待请 fresh=True。
    """
    try:
        from app.core.state_dispatch import StateKind, get_state

        sc = get_state(
            session,
            StateKind.SCENE,
            fresh=bool(fresh),
            max_age=0.0 if fresh else 0.35,
            log=lambda _m: None,
        )
        if isinstance(sc, dict):
            sid = int(sc.get("scene_id") or 0)
            if sid > 0:
                return sid
            if fresh:
                return None
    except Exception:
        pass
    try:
        from app.core.automove import read_scene_position

        sp = read_scene_position(session, log=lambda _m: None)
        if getattr(sp, "ok", False):
            sid = int(getattr(sp, "scene_id", 0) or 0)
            if sid <= 0:
                return None
            return sid
    except Exception:
        return None
    return None


def _portal_dlg_shown(session, *, log: LogFn | None = None) -> list[tuple[str, int]]:
    """Return [(name, dlg_ptr)] for shown portal-related dialogs."""
    log = log or (lambda _m: None)
    out: list[tuple[str, int]] = []
    try:
        from app.core.plg_ui import get_game_ui_dlg, is_dlg_show, list_game_ui_dlg_names

        for nm in PORTAL_NPC_DLG_NAMES:
            try:
                dlg = int(get_game_ui_dlg(session, nm, log=log) or 0) & 0xFFFFFFFF
            except Exception:
                dlg = 0
            if not dlg:
                continue
            try:
                if not is_dlg_show(session, dlg, log=log):
                    continue
            except Exception:
                pass
            out.append((nm, dlg))
        try:
            listed = list_game_ui_dlg_names(session, log=log)
            names = list(listed.names or []) if listed and listed.ok else []
        except Exception:
            names = []
        have = {n for n, _ in out}
        for nm in names:
            if not nm or nm in have:
                continue
            if not (
                nm.startswith("Win_NPC")
                or "Trans" in nm
                or nm in ("Win_TaskTrans", "Win_FactionTrans")
            ):
                continue
            try:
                dlg = int(get_game_ui_dlg(session, nm, log=log) or 0) & 0xFFFFFFFF
            except Exception:
                dlg = 0
            if not dlg:
                continue
            try:
                if not is_dlg_show(session, dlg, log=log):
                    continue
            except Exception:
                pass
            out.append((nm, dlg))
            have.add(nm)
    except Exception as e:
        log(f"task_api portal dlg scan err: {e}")
    return out


def _click_client_xy(
    session,
    cx: int,
    cy: int,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
    double: bool = False,
) -> bool:
    """Background UI click at client coords via bridge, with external fallback."""
    log = log or (lambda _m: None)
    try:
        from app.core.aui_click import click_client_bg

        ok = bool(
            click_client_bg(
                session,
                int(hwnd or 0),
                int(cx),
                int(cy),
                hold_ms=55,
                log=log,
            )
        )
        if ok and double:
            time.sleep(0.08)
            ok = bool(
                click_client_bg(
                    session,
                    int(hwnd or 0),
                    int(cx),
                    int(cy),
                    hold_ms=45,
                    log=log,
                )
            ) or ok
        return ok
    except Exception as e:
        log(f"task_api portal click err: {e}")
        return False


def _click_ctrl_rect(
    session,
    rect,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
    y_bias: float = 0.5,
    double: bool = False,
) -> bool:
    """Click center (or y_bias) of an AuiCtrlRect/DlgRect-like object."""
    try:
        x = int(getattr(rect, "x", 0) or 0)
        y = int(getattr(rect, "y", 0) or 0)
        w = int(getattr(rect, "w", 0) or 0)
        h = int(getattr(rect, "h", 0) or 0)
    except Exception:
        return False
    if w < 6 or h < 6:
        return False
    cx = x + max(1, w // 2)
    cy = y + max(1, int(h * float(y_bias)))
    return _click_client_xy(
        session, cx, cy, hwnd=hwnd, log=log, double=bool(double)
    )


def _resolve_ctrl_rect(session, dlg_ptr: int, name: str, *, log: LogFn | None = None):
    """GetDlgItem + read control rect."""
    log = log or (lambda _m: None)
    try:
        from app.core.aui_click import get_aui_dlg_item_ptr, read_aui_ctrl_rect

        ctrl = get_aui_dlg_item_ptr(session, int(dlg_ptr), name, log=log)
        if not ctrl:
            return None
        rect = read_aui_ctrl_rect(session, ctrl, name=name, log=log)
        if rect is not None and getattr(rect, "ok", False):
            return rect
    except Exception as e:
        log(f"task_api portal ctrl {name}: {e}")
    return None


def _portal_keyword_needles() -> list[str]:
    """Keywords used to anchor real portal menu strings in memory."""
    keys: list[str] = []
    for group in (
        PORTAL_NAME_UPPER,
        PORTAL_NAME_DEEP,
        PORTAL_NAME_ENTRY,
        ("地宫", "天下会", "一层", "二层", "三层", "初级", "中级", "高级", "升级"),
    ):
        for k in group:
            k = str(k or "").strip()
            if k and k not in keys:
                keys.append(k)
    keys.sort(key=len, reverse=True)
    return keys


def _portal_text_is_clean(s: str) -> bool:
    """
    Reject heap garbage (0xCC fills, Hangul noise, PUA) misread as UI text.

    Live symptom: logs full of U+CCxx / private-use junk while usable=False.
    Clean portal options are short CJK phrases (e.g. 地宫上层 / 升级地宫深处).
    """
    s = (s or "").strip()
    if not s or len(s) < 2 or len(s) > 40:
        return False
    # color codes
    s2 = re.sub(r"\^[0-9a-fA-F]{0,6}", "", s).strip()
    if not s2:
        return False
    hangul = sum(1 for c in s2 if 0xAC00 <= ord(c) <= 0xD7A3)
    pua = sum(1 for c in s2 if 0xE000 <= ord(c) <= 0xF8FF)
    # 0xCCCC as UTF-16LE often appears as Hangul syllable 쳌; also reject high surrogates noise
    weird = sum(
        1
        for c in s2
        if (
            0xE000 <= ord(c) <= 0xF8FF
            or 0x1900 <= ord(c) <= 0x1AFF
            or 0x2000 <= ord(c) <= 0x206F
            or 0xFFF0 <= ord(c) <= 0xFFFF
            or ord(c) == 0xCCCC
            or 0xD800 <= ord(c) <= 0xDFFF
        )
    )
    if hangul or pua or weird:
        return False
    # classic debug fill glyphs when mis-decoded
    if "쳌" in s2 or "\ufffd" in s2:
        return False
    cjk = sum(1 for c in s2 if "\u4e00" <= c <= "\u9fff")
    if cjk < 2:
        return False
    # require mostly CJK / ascii digits spaces
    ok_ch = sum(
        1
        for c in s2
        if (
            "\u4e00" <= c <= "\u9fff"
            or c.isalnum()
            or c in " ·_/（）()-[]【】"
        )
    )
    if ok_ch * 2 < len(s2) * 2:  # at least ~100% of printable-ok? require strong ratio
        pass
    if ok_ch < len(s2) * 0.85:
        return False
    if any(x in s2 for x in ("失败", "Error", "failed", "nullptr", "Win_", "0x")):
        return False
    return True


def _portal_text_has_keyword(s: str) -> bool:
    keys = _portal_keyword_needles()
    return any(k and k in (s or "") for k in keys)


def _decode_u16le_cstring(raw: bytes, max_chars: int = 48) -> str:
    if not raw:
        return ""
    chars: list[str] = []
    for i in range(0, min(len(raw) - 1, max_chars * 2), 2):
        code = raw[i] | (raw[i + 1] << 8)
        if code == 0:
            break
        if code < 0x20 and code not in (0x09,):
            break
        # stop early on fill / non-text planes (do not absorb garbage into "string")
        if code == 0xCCCC or 0xE000 <= code <= 0xF8FF or 0xAC00 <= code <= 0xD7A3:
            break
        chars.append(chr(code))
    return "".join(chars).strip()


def _decode_gbk_cstring(raw: bytes, max_bytes: int = 96) -> str:
    if not raw:
        return ""
    end = raw.find(b"\x00")
    if end < 0:
        end = min(len(raw), max_bytes)
    try:
        return raw[:end].decode("gbk", "ignore").strip()
    except Exception:
        return ""


def _extract_u16_window(blob: bytes, hit_off: int, *, max_chars: int = 32) -> str:
    """Expand a UTF-16LE keyword hit into a short option line."""
    if not blob or hit_off < 0 or hit_off >= len(blob) - 1:
        return ""
    if hit_off & 1:
        hit_off -= 1
    start = hit_off
    steps = 0
    while start >= 2 and steps < max_chars:
        code = blob[start - 2] | (blob[start - 1] << 8)
        if code == 0 or (code < 0x20 and code not in (0x09,)):
            break
        if (
            code == 0xCCCC
            or 0xE000 <= code <= 0xF8FF
            or 0xAC00 <= code <= 0xD7A3
        ):
            break
        start -= 2
        steps += 1
    end = hit_off
    steps = 0
    while end + 1 < len(blob) and steps < max_chars:
        code = blob[end] | (blob[end + 1] << 8)
        if code == 0 or (code < 0x20 and code not in (0x09,)):
            break
        if (
            code == 0xCCCC
            or 0xE000 <= code <= 0xF8FF
            or 0xAC00 <= code <= 0xD7A3
        ):
            break
        end += 2
        steps += 1
    chunk = blob[start:end]
    if len(chunk) < 2:
        return ""
    if len(chunk) & 1:
        chunk = chunk[:-1]
    try:
        s = chunk.decode("utf-16le", "ignore")
    except Exception:
        return ""
    s = re.sub(r"\^[0-9a-fA-F]{0,6}", "", s)
    s = "".join(ch for ch in s if ch == " " or ch.isprintable())
    return s.strip()


def _portal_strings_from_blob(blob: bytes, *, max_hits: int = 24) -> list[str]:
    """Keyword-anchored extraction of portal-related UTF-16 / GBK strings only."""
    if not blob:
        return []
    hits: list[str] = []
    seen: set[str] = set()
    keys = _portal_keyword_needles()

    def push(s: str) -> None:
        s = (s or "").strip()
        if not s or s in seen:
            return
        s = re.sub(r"\^[0-9a-fA-F]{0,6}", "", s).strip()
        if not _portal_text_is_clean(s):
            return
        # only keep strings that actually relate to portal menus
        if not _portal_text_has_keyword(s):
            return
        seen.add(s)
        hits.append(s)

    for key in keys:
        try:
            pat = key.encode("utf-16le")
        except Exception:
            continue
        if len(pat) < 4:
            continue
        start = 0
        while start < len(blob) - len(pat) and len(hits) < max_hits:
            idx = blob.find(pat, start)
            if idx < 0:
                break
            if idx & 1:
                start = idx + 1
                continue
            push(_extract_u16_window(blob, idx, max_chars=28))
            push(key)
            start = idx + len(pat)

    for key in keys:
        try:
            pat = key.encode("gbk", "ignore")
        except Exception:
            continue
        if len(pat) < 2:
            continue
        start = 0
        while start < len(blob) - len(pat) and len(hits) < max_hits:
            idx = blob.find(pat, start)
            if idx < 0:
                break
            lo = max(0, idx - 24)
            hi = min(len(blob), idx + 48)
            push(_decode_gbk_cstring(blob[lo:hi] + b"\x00", max_bytes=hi - lo))
            push(key)
            start = idx + len(pat)

    return hits


def _read_remote_blob(session, addr: int, size: int) -> bytes:
    pm = getattr(session, "pm", None)
    if pm is None or not addr or size <= 0:
        return b""
    try:
        import pymem.memory

        return pymem.memory.read_bytes(
            pm.process_handle, int(addr) & 0xFFFFFFFF, int(size)
        )
    except Exception:
        return b""


# AUI label/list caption: live Txt_* often stores wchar* near +0xB8 (yaolu verified).
_AUI_CAPTION_STR_OFFS: tuple[int, ...] = (0xB8, 0xBC, 0xC0, 0xB0, 0xA8, 0xC4, 0xD0, 0xD4)
# GetText-ish vtable slots near SetText(+0x48) in fill path.
_AUI_GETTEXT_VT_OFFS: tuple[int, ...] = (0x44, 0x40, 0x48, 0x3C)


def _read_remote_wstring(session, addr: int, max_chars: int = 48) -> str:
    raw = _read_remote_blob(session, int(addr or 0) & 0xFFFFFFFF, max(4, int(max_chars) * 2 + 4))
    if not raw:
        return ""
    s = _decode_u16le_cstring(raw, max_chars=max_chars)
    return s if _portal_text_is_clean(s) or _portal_text_has_keyword(s) else ""


def _aui_caption_from_ctrl(session, ctrl: int) -> str:
    """
    Best-effort caption via wide-string pointer fields only.

    Do NOT call remote GetText here: live hangs (800ms+) leave remote threads
    running and cascade into CreateRemoteThread/VirtualAllocEx err=5 (bridge death).
    """
    ctrl = int(ctrl or 0) & 0xFFFFFFFF
    if not ctrl:
        return ""
    for off in _AUI_CAPTION_STR_OFFS:
        try:
            blob = _read_remote_blob(session, (ctrl + int(off)) & 0xFFFFFFFF, 4)
            if len(blob) < 4:
                continue
            p = struct.unpack_from("<I", blob, 0)[0]
            if p < 0x10000 or p > 0x7FFE0000:
                continue
            s = _read_remote_wstring(session, p, 40)
            if s and (_portal_text_has_keyword(s) or _portal_text_is_clean(s)):
                return s
            b0 = _read_remote_blob(session, p, 8)
            if len(b0) >= 4:
                p2 = struct.unpack_from("<I", b0, 0)[0]
                if 0x10000 < p2 < 0x7FFE0000:
                    s2 = _read_remote_wstring(session, p2, 40)
                    if s2 and (
                        _portal_text_has_keyword(s2) or _portal_text_is_clean(s2)
                    ):
                        return s2
        except Exception:
            continue
    return ""


def _scan_dlg_option_texts(
    session,
    dlg_ptr: int,
    *,
    max_hits: int = 24,
    log: LogFn | None = None,
) -> list[str]:
    """
    Collect clean portal option strings under an AUI dialog.

    Only keyword-bearing clean CJK is returned — never dump 0xCC heap noise
    into logs as fake "texts=".
    """
    log = log or (lambda _m: None)
    hits: list[str] = []
    seen: set[str] = set()
    rejected = 0

    def push(s: str) -> None:
        nonlocal rejected
        s = (s or "").strip()
        if not s or s in seen:
            return
        s = re.sub(r"\^[0-9a-fA-F]{0,6}", "", s).strip()
        if not s or s in seen:
            return
        if not _portal_text_is_clean(s):
            rejected += 1
            return
        # Prefer keyword hits; allow clean short CJK only if already have nothing
        # and string looks like a menu line (handled later). For portal we require
        # keyword so random clean UI chrome does not pollute option matching.
        if not _portal_text_has_keyword(s):
            rejected += 1
            return
        seen.add(s)
        hits.append(s)

    dlg = int(dlg_ptr or 0) & 0xFFFFFFFF
    if not dlg:
        return []

    # --- A) named controls: GetDlgItem + caption / GetText ---
    try:
        from app.core.aui_click import get_aui_dlg_item_ptr

        # Keep short: each GetDlgItem is a remote thread; spam caused err=5 cascade.
        name_cands = (
            "Lst_Main",
            "Lst_Content",
            "Lst_Item",
            "Txt_Content",
            "Txt_Talk",
            "Txt_Info",
            "Btn_Trans1",
            "Btn_Trans2",
            "Btn_Function",
        )
        # dialog itself
        try:
            t_dlg = _aui_caption_from_ctrl(session, dlg)
            if t_dlg:
                push(t_dlg)
                for part in re.split(r"[\r\n|/／·]+", t_dlg):
                    push(part)
        except Exception:
            pass
        found_ctrls = 0
        for nm in name_cands:
            if len(hits) >= max_hits:
                break
            try:
                ctrl = get_aui_dlg_item_ptr(session, dlg, nm, log=lambda _m: None)
            except Exception:
                ctrl = 0
            if not ctrl:
                continue
            found_ctrls += 1
            try:
                tx = _aui_caption_from_ctrl(session, int(ctrl))
            except Exception:
                tx = ""
            if tx:
                push(tx)
                for part in re.split(r"[\r\n|/／·]+", tx):
                    push(part)
                log(f"task_api portal ctrl {nm}=0x{int(ctrl)&0xFFFFFFFF:X} text={tx[:32]!r}")
        if found_ctrls:
            log(f"task_api portal getdlgitem hits={found_ctrls} dlg=0x{dlg:X}")
    except Exception as e:
        log(f"task_api portal gettext scan: {e}")

    # --- B) keyword-anchored memory under dialog + shallow children ---
    root = _read_remote_blob(session, dlg, 0x1000)
    if root:
        for s in _portal_strings_from_blob(root, max_hits=max_hits):
            push(s)
        deadline = time.monotonic() + 0.18
        seen_ptr: set[int] = {dlg}
        child_reads = 0
        for off in range(0, min(len(root) - 4, 0xC00), 4):
            if time.monotonic() >= deadline or len(hits) >= max_hits or child_reads >= 56:
                break
            p = struct.unpack_from("<I", root, off)[0]
            if p < 0x10000 or p > 0x7FFE0000:
                continue
            p &= 0xFFFFFFFF
            if p in seen_ptr:
                continue
            # keyword extract from pointed object only — never push raw u16 garbage
            b0 = _read_remote_blob(session, p, 128)
            if b0:
                for s in _portal_strings_from_blob(b0, max_hits=8):
                    push(s)
            child = _read_remote_blob(session, p, 0x280)
            if not child or len(child) < 0x40:
                continue
            child_reads += 1
            seen_ptr.add(p)
            for s in _portal_strings_from_blob(child, max_hits=12):
                push(s)
            if len(hits) < max_hits and time.monotonic() < deadline:
                for off2 in range(0, min(len(child) - 4, 0x180), 4):
                    p2 = struct.unpack_from("<I", child, off2)[0]
                    if p2 < 0x10000 or p2 > 0x7FFE0000:
                        continue
                    p2 &= 0xFFFFFFFF
                    if p2 in seen_ptr:
                        continue
                    b2 = _read_remote_blob(session, p2, 96)
                    if not b2:
                        continue
                    for s in _portal_strings_from_blob(b2, max_hits=6):
                        push(s)
                    if len(hits) >= max_hits:
                        break

    if hits:
        log(
            f"task_api portal texts dlg=0x{dlg:X} clean={len(hits)} "
            f"rejected={rejected} sample={hits[:8]}"
        )
    else:
        log(
            f"task_api portal texts dlg=0x{dlg:X} clean=0 "
            f"rejected={rejected} reason=no_clean_keyword"
        )
    return hits[:max_hits]


def _portal_option_lines(texts: list[str]) -> list[str]:
    """Reduce raw scan hits to ordered option-like lines for slot mapping."""
    keys = (
        list(PORTAL_NAME_UPPER)
        + list(PORTAL_NAME_DEEP)
        + list(PORTAL_NAME_ENTRY)
        + ["地宫", "天下会"]
    )
    out: list[str] = []
    seen: set[str] = set()
    _bad_punct = ("，", "。", "！", "？", ",", "!", "?")
    # task titles / panel noise — never click as portal options
    _bad_token = (
        "BOSS", "boss", "杀怪", "兑换", "发放", "进行中", "已完成",
        "每日", "任务", "黄金", "武器",
    )
    _weak_only = ("进入", "地宫", "初级", "中级", "高级", "传送")
    for raw in texts or []:
        s = str(raw or "").strip()
        if not s or s in seen:
            continue
        if not _portal_text_is_clean(s):
            continue
        # story / long sentences pollute options
        if len(s) > 16 or any(ch in s for ch in _bad_punct):
            continue
        if any(t in s for t in _bad_token):
            continue
        if not any(k in s for k in keys):
            continue
        # bare fragments pollute slot mapping (e.g. 「进入」「地宫」 alone)
        if s in _weak_only:
            continue
        seen.add(s)
        out.append(s)
    return out


def _portal_dest_slot_from_texts(texts: list[str], kind: str) -> int | None:
    """
    Map portal_kind to UI list row (上层=0, 深处=1).

    Memory-scan hit order is NOT UI order — never return scan-list index.
    """
    options = _portal_option_lines(texts)
    if not options:
        # still allow fixed slot when raw texts mention dest keywords
        dest_kw = _portal_dest_keywords(kind)
        blob = " ".join(str(x or "") for x in (texts or []))
        if any(k and k in blob for k in dest_kw):
            return _dest_slot_for_kind(kind)
        return None
    dest_kw = _portal_dest_keywords(kind)
    if any(_text_matches_keywords(opt, dest_kw) >= 0 for opt in options):
        return _dest_slot_for_kind(kind)
    return None


def _portal_texts_usable(texts: list[str]) -> bool:
    """True only when clean scanned texts contain known portal option keywords."""
    for t in texts or []:
        s = str(t or "")
        if not _portal_text_is_clean(s):
            continue
        if _portal_text_has_keyword(s):
            return True
    return False


def _portal_scene_layer_check(scene_id: int | None, kind: str) -> dict:
    """
    Soft/hard-ish check after scene change: does scene match 上层/深处?

    Uses scene_map Chinese names + known id sets from InstInfo.
    match=None means unknown — do not fail hard.
    """
    out: dict = {"match": None, "label": "", "note": "no scene", "scene_id": scene_id}
    if scene_id is None:
        return out
    try:
        sid = int(scene_id)
    except (TypeError, ValueError):
        return out
    if sid <= 0:
        out["scene_id"] = sid
        out["note"] = f"scene {sid} invalid/transition"
        return out
    out["scene_id"] = sid
    try:
        from app.core.map_names import format_scene_display, resolve_scene_id

        mid, cn = resolve_scene_id(sid)
        label = format_scene_display(sid)
        out["label"] = label
        blob = f"{cn or ''} {mid or ''} {label or ''}"
        k = str(kind or "").strip().lower()

        id_upper = sid in PORTAL_UPPER_SCENE_IDS
        id_lower = sid in PORTAL_LOWER_SCENE_IDS
        id_deep = sid in PORTAL_DEEP_SCENE_IDS
        has_deep = any(x in blob for x in ("深处", "下层", "三层")) or id_lower or id_deep
        has_upper = ("上层" in blob or "一层" in blob or id_upper) and not (
            "下层" in blob or id_lower or id_deep
        )
        # pure 上层 name wins even if id unknown
        if "上层" in blob and "下层" not in blob and "深处" not in blob:
            has_upper = True
            has_deep = False
        if "深处" in blob or id_deep:
            has_deep = True
            if "上层" not in blob:
                has_upper = False
        if "下层" in blob or id_lower:
            has_deep = True
            has_upper = False

        if k in ("dungeon_deep", "dungeon_lower"):
            if has_deep and not has_upper:
                out["match"] = True
                out["note"] = f"scene {sid} looks deep/lower: {label}"
            elif has_upper and not has_deep:
                out["match"] = False
                out["note"] = f"scene {sid} looks UPPER but want deep: {label}"
            elif id_deep or id_lower:
                out["match"] = True
                out["note"] = f"scene {sid} id-deep/lower: {label}"
            elif id_upper:
                out["match"] = False
                out["note"] = f"scene {sid} id-upper but want deep: {label}"
            else:
                out["note"] = f"scene {sid} layer unknown: {label or '-'}"
        elif k in ("dungeon_upper",):
            if has_upper and not has_deep:
                out["match"] = True
                out["note"] = f"scene {sid} looks upper: {label}"
            elif has_deep and not has_upper:
                out["match"] = False
                out["note"] = f"scene {sid} looks DEEP/LOWER but want upper: {label}"
            elif id_upper:
                out["match"] = True
                out["note"] = f"scene {sid} id-upper: {label}"
            elif id_deep or id_lower:
                out["match"] = False
                out["note"] = f"scene {sid} id-deep/lower but want upper: {label}"
            else:
                out["note"] = f"scene {sid} layer unknown: {label or '-'}"
        else:
            out["note"] = f"scene {sid}: {label or '-'}"
    except Exception as e:
        out["note"] = f"scene check err: {e}"
    return out


def _safe_portal_scene_snapshot(
    session,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> dict:
    """Read host/scene atomically through the loaded UI-thread bridge only."""
    log = log or (lambda _m: None)
    bridge = None
    try:
        from app.core.xajh_bridge import ensure_bridge

        bridge = ensure_bridge(
            int(getattr(session, "pid", 0) or 0),
            hwnd=int(hwnd or 0) or None,
            inject_if_needed=False,
            log=lambda _m: None,
        )
        if bridge is None:
            return {"ok": False, "scene_id": None, "error": "bridge not ready"}
        snap = bridge.host_snapshot(hwnd=int(hwnd or 0) or None, timeout_ms=1500)
        if not bool(getattr(snap, "ok", False)):
            return {
                "ok": False,
                "scene_id": None,
                "error": str(getattr(snap, "error", None) or "host snapshot failed"),
            }
        return {
            "ok": True,
            "role_present": bool(int(getattr(snap, "ret", 0) or 0)),
            "scene_id": int(getattr(snap, "mode", 0) or 0),
            "position": (
                float(getattr(snap, "x", 0.0) or 0.0),
                float(getattr(snap, "y", 0.0) or 0.0),
                float(getattr(snap, "z", 0.0) or 0.0),
            ),
        }
    except Exception as exc:
        log(f"task_api portal safe snapshot fail: {exc}")
        return {"ok": False, "scene_id": None, "error": str(exc)}
    finally:
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass


def _portal_scene_generation_state(
    session,
    *,
    hwnd: int,
    origin_scene_id: int,
    portal_kind: str,
    log: LogFn | None = None,
) -> dict:
    """Classify whether a synchronized portal command is still executable."""
    snap = _safe_portal_scene_snapshot(session, hwnd=hwnd, log=log)
    origin = int(origin_scene_id or 0)
    if not snap.get("ok"):
        return {
            **snap,
            "status": "snapshot_unavailable",
            "origin_scene_id": origin or None,
        }
    sid = int(snap.get("scene_id") or 0)
    state = {**snap, "origin_scene_id": origin or None}
    if not snap.get("role_present"):
        state["status"] = "role_missing"
    elif sid <= 0:
        state["status"] = "scene_transition"
    elif _portal_scene_layer_check(sid, portal_kind).get("match") is True:
        state["status"] = "already_in_target_scene"
    elif origin > 0 and sid != origin:
        state["status"] = "stale_scene_generation"
    else:
        state["status"] = "current"
    return state


def _portal_generation_note(state: dict) -> str:
    status = str(state.get("status") or "scene_generation_changed")
    origin = state.get("origin_scene_id")
    live = state.get("scene_id")
    if status == "already_in_target_scene":
        return f"角色已进入目标地宫 scene={live}，旧寻路已结束"
    if status == "stale_scene_generation":
        return f"场景已从 {origin} 变为 {live}，旧寻路已作废"
    if status == "scene_transition":
        return "角色正在过图，旧寻路已作废"
    if status == "role_missing":
        return "角色暂不可用，旧寻路已作废"
    if status == "snapshot_unavailable":
        return "场景安全快照不可用，已跳过旧寻路"
    return status



def _text_matches_keywords(text: str, keywords: list[str] | tuple[str, ...]) -> int:
    """Lower score is better; -1 = no match. Prefer longer keyword hits."""
    s = str(text or "")
    if not s:
        return -1
    best = -1
    best_len = -1
    for i, kw in enumerate(keywords):
        if not kw:
            continue
        if kw in s:
            score_len = len(kw)
            if score_len > best_len:
                best_len = score_len
                best = i
    return best


def _pick_text_indices_for_keywords(
    texts: list[str],
    keywords: list[str] | tuple[str, ...],
) -> list[int]:
    """Return indices of texts matching keywords, best match first."""
    scored: list[tuple[int, int, int]] = []  # (kw_rank, -len, index)
    for ti, t in enumerate(texts):
        rank = _text_matches_keywords(t, keywords)
        if rank < 0:
            continue
        scored.append((rank, -len(t), ti))
    scored.sort()
    out: list[int] = []
    for _, __, ti in scored:
        if ti not in out:
            out.append(ti)
    return out


def _texts_suggest_dest_ready(texts: list[str], kind: str) -> bool:
    """True if option texts already look like upper/deep destinations."""
    dest = _portal_dest_keywords(kind)
    return any(_text_matches_keywords(t, dest) >= 0 for t in texts)


def _texts_suggest_entry(texts: list[str]) -> bool:
    return any(_text_matches_keywords(t, _portal_entry_keywords()) >= 0 for t in texts)


def _dest_slot_for_kind(kind: str) -> int:
    """
    Conservative list-row index when name match is unavailable.

    Game menus typically list 上层 before 深处/下层.
    """
    k = str(kind or "").strip().lower()
    if k in ("dungeon_deep", "dungeon_lower"):
        return 1
    if k == "tianxiahui":
        return 0
    return 0


def _click_list_slots(
    session,
    rect,
    *,
    hwnd: int = 0,
    slots: int = 6,
    prefer_indices: list[int] | None = None,
    max_clicks: int = 1,
    double: bool = True,
    log: LogFn | None = None,
    stop_event: threading.Event | None = None,
    before_scene: int | None = None,
    fill_remaining: bool = False,
    # NPC talk dialogs: skip title chrome; options sit in lower body.
    band_top: float = 0.0,
    band_bottom: float = 1.0,
    micro_adjust: bool = False,
    # Cap micro Y retries for same slot (entry should usually be 1).
    micro_tries: int | None = None,
    # Optional: stop micro loop early when submenu / target appears.
    after_click=None,
    # Absolute Y fracs of full dialog height when set (see dest path).
    y_fracs: list[float] | None = None,
    # Optional X fracs of dialog width (cycle with each Y try). Default 0.42.
    x_fracs: list[float] | None = None,
    # Sleep between tries (entry can be shorter).
    try_wait_s: float = 0.8,
) -> tuple[bool, str]:
    """
    Click vertical list slots (preferred indices first).

    Default: only preferred indices, max_clicks=1 — never spray other rows.
    band_top/band_bottom limit the vertical option area (0..1 of rect height).
    micro_adjust: for the same slot try alternate Y if first fails.
    after_click: optional zero-arg callable → truthy stops further tries.
    Returns (scene_changed_or_after_ok, note).
    """
    log = log or (lambda _m: None)
    try:
        x = int(getattr(rect, "x", 0) or 0)
        y = int(getattr(rect, "y", 0) or 0)
        w = int(getattr(rect, "w", 0) or 0)
        h = int(getattr(rect, "h", 0) or 0)
    except Exception:
        return False, "bad list rect"
    if w < 10 or h < 16:
        return False, "list rect too small"
    n = max(1, min(10, int(slots)))
    order: list[int] = []
    for i in prefer_indices or []:
        ii = int(i)
        if 0 <= ii < n and ii not in order:
            order.append(ii)
    if fill_remaining:
        for i in range(n):
            if i not in order:
                order.append(i)
    if not order:
        order = [0]
    order = order[: max(1, int(max_clicks))]

    bt = max(0.0, min(0.85, float(band_top)))
    bb = max(bt + 0.1, min(1.0, float(band_bottom)))
    band_y = y + int(h * bt)
    band_h = max(20, int(h * (bb - bt)))
    xfs = [float(v) for v in (x_fracs or [0.42])]
    if not xfs:
        xfs = [0.42]
    row_h = max(18, band_h // max(1, n))
    wait_s = max(0.25, float(try_wait_s))

    for idx in order:
        if stop_event is not None and stop_event.is_set():
            return False, "stopped"
        # Primary center of row; optional vertical micro tries (same slot).
        # Deep (higher idx) needs lower hits — live slot1 hit 上层 with 0.45.
        if y_fracs:
            fracs = [float(f) for f in y_fracs]
        else:
            fracs = [0.50]
            if micro_adjust:
                if int(idx) >= 1:
                    fracs = [0.72, 0.88, 0.55, 0.40]
                else:
                    fracs = [0.40, 0.55, 0.28, 0.70]
        if micro_tries is not None:
            fracs = fracs[: max(1, int(micro_tries))]
        for fi, frac in enumerate(fracs):
            xf = xfs[int(fi) % len(xfs)]
            cx = x + max(1, int(w * float(xf)))
            if y_fracs is not None:
                # y_fracs are FULL dialog height fractions (0..1 of rect.h).
                cy = y + int(h * float(frac))
            else:
                cy = band_y + int(row_h * (idx + float(frac)))
            cy = max(y + 4, min(y + h - 6, cy))
            ok = _click_client_xy(
                session, cx, cy, hwnd=hwnd, log=log, double=bool(double)
            )
            log(
                f"task_api portal list click slot={idx}/{n} try={fi} "
                f"xy=({cx},{cy}) band=({bt:.2f}-{bb:.2f}) "
                f"rect=({x},{y},{w},{h}) dbl={bool(double)} ok={ok}"
            )
            if stop_event is not None:
                if stop_event.wait(wait_s):
                    return False, "stopped"
            else:
                time.sleep(wait_s)
            sid = _scene_id_now(session)
            if before_scene is not None and sid and sid != before_scene:
                return (
                    True,
                    f"list slot {idx} try={fi} scene {before_scene}→{sid}",
                )
            if after_click is not None:
                try:
                    if after_click():
                        return True, f"list slot {idx} try={fi} after_click"
                except Exception:
                    pass
    return False, f"list slots clicked {order}"


def _select_portal_npc_menu(
    session,
    *,
    portal_kind: str = "",
    dungeon_tier: str = "",
    portal_tid: int = 0,
    prefer_tid: int = 0,
    hwnd: int = 0,
    before_scene: int | None = None,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    max_rounds: int = 7,
) -> dict:
    """
    After NPCSayHello: click transfer options by name (上层/深处).

    Safety rules:
      - Prefer keyword-matched options only.
      - If menu texts unusable (garbled): click EXACTLY one dest slot by kind
        (上层=0, 深处=1); never multi-row / y-band spray that can pick wrong layer.
      - Scene change alone is success only after a dest-targeted click.
    """
    log = log or (lambda _m: None)
    kind = str(portal_kind or "").strip() or "dungeon_upper"
    tier = str(dungeon_tier or "").strip()
    p_tid = int(portal_tid or 0)
    pref_tid = int(prefer_tid or 0)
    # Trust DelvNPC mouth when prefer matches portal (or prefer alone on map).
    trust_tid = bool(
        (pref_tid and p_tid and pref_tid == p_tid)
        or (
            pref_tid
            and pref_tid in PORTAL_TID_TIER
            and (not p_tid or p_tid == pref_tid)
        )
    )
    dest_kw = _portal_dest_keywords(kind)
    entry_kw = _portal_entry_keywords()
    dest_slot = _dest_slot_for_kind(kind)
    notes: list[str] = []
    clicked: list[str] = []
    entry_clicked = False
    entry_clicks = 0
    dest_clicked = False
    notes.append(
        f"kind={kind} tier={tier or '-'} dest_slot={dest_slot} dest_kw={dest_kw[:4]}"
    )
    log(
        f"task_api portal menu start kind={kind} tier={tier or '-'} "
        f"dest_slot={dest_slot} before_scene={before_scene}"
    )

    def scene_changed() -> bool:
        sid = _scene_id_now(session, fresh=True)
        return bool(before_scene is not None and sid and sid != before_scene)

    def sleep_s(s: float) -> bool:
        if stop_event is not None:
            return bool(stop_event.wait(float(s)))
        time.sleep(float(s))
        return False

    def wait_scene(timeout_s: float = 1.6) -> int | None:
        """Poll scene change after a dest click (faster than full menu rounds)."""
        deadline = time.monotonic() + max(0.2, float(timeout_s))
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return None
            if scene_changed():
                return _scene_id_now(session, fresh=True)
            if sleep_s(0.15):
                return None
        return _scene_id_now(session, fresh=True) if scene_changed() else None

    def finish_dest(sid: int | None, note: str) -> dict:
        """Success path with optional layer name verification."""
        chk = _portal_scene_layer_check(sid, kind)
        notes.append(
            f"layer_check match={chk.get('match')} label={chk.get('label')!r}"
        )
        log(f"task_api portal layer_check {chk.get('note')}")
        if chk.get("match") is False:
            return {
                "ok": False,
                "error": chk.get("note") or "wrong dungeon layer",
                "note": f"{note}; WRONG LAYER: {chk.get('note')}",
                "notes": notes,
                "clicked": clicked,
                "after_scene": sid,
                "wrong_layer": True,
                "layer_check": chk,
            }
        extra = ""
        if chk.get("label"):
            extra = f" | {chk.get('note')}"
        return {
            "ok": True,
            "note": f"{note}{extra}",
            "notes": notes,
            "clicked": clicked,
            "after_scene": sid,
            "layer_check": chk,
        }

    for round_i in range(1, max(1, int(max_rounds)) + 1):
        if stop_event is not None and stop_event.is_set():
            return {
                "ok": False,
                "error": "menu select stopped",
                "notes": notes,
                "clicked": clicked,
            }
        if scene_changed() and dest_clicked:
            sid = _scene_id_now(session)
            return finish_dest(sid, f"scene {before_scene}→{sid} after dest click")

        dlgs = _portal_dlg_shown(session, log=log)
        if not dlgs:
            notes.append(f"r{round_i}:no portal dlg")
            log(f"task_api portal menu r{round_i}: no dlg")
            if sleep_s(0.20):
                return {
                    "ok": False,
                    "error": "menu select stopped",
                    "notes": notes,
                    "clicked": clicked,
                }
            continue

        all_texts: list[str] = []
        for nm, dlg in dlgs:
            all_texts.extend(_scan_dlg_option_texts(session, dlg, log=log))
        uniq_texts: list[str] = []
        for t in all_texts:
            if t not in uniq_texts:
                uniq_texts.append(t)
        all_texts = uniq_texts
        usable = _portal_texts_usable(all_texts)
        notes.append(f"r{round_i}:dlgs={[n for n, _ in dlgs]}")
        if usable:
            notes.append(f"r{round_i}:usable=1 texts={all_texts[:14]}")
            log(
                f"task_api portal menu r{round_i} kind={kind} "
                f"dlgs={[n for n, _ in dlgs]} usable=True texts={all_texts[:10]}"
            )
        else:
            notes.append(
                f"r{round_i}:usable=0 reason=no_clean_keyword "
                f"(refuse garbled sample; dest_slot={dest_slot})"
            )
            log(
                f"task_api portal menu r{round_i} kind={kind} "
                f"dlgs={[n for n, _ in dlgs]} usable=False "
                f"reason=no_clean_keyword dest_slot={dest_slot}"
            )

        # ============================================================
        # 用户确认（2026-07-22 修订）— 按 NPC 身上是否还有任务:
        #   · NPC 无任务（都接了/完成了）→ 对话直接二级面板：xxx上层 / xxx深处
        #   · NPC 还有任务 → 一级面板（含 X级地宫传送 / 任务项）
        #                    点「X级地宫传送」后进入二级：上层 / 深处
        # 升级口通常无任务挂着，常直接二级；初/中/高有任务时才要一级。
        # 判定看当前对话框文案，不要按档位死写「每日必 entry」。
        # ============================================================
        daily_tier = tier in ("初级", "中级", "高级")
        upgrade_tier = tier in ("升级", "练级")
        entry_labels = _portal_entry_label_for_tier(tier)
        option_lines = _portal_option_lines(all_texts)

        # 二级面板证据：同时出现 上层+深处（真实传送子菜单）
        layer2_ready = _portal_dest_lines_ready(all_texts) or any(
            n == "Win_NPCTrans" for n, _ in dlgs
        )
        # 一级入口：干净的「X级地宫传送」（排除 BOSS 任务标题）
        has_service_entry = False
        for opt in option_lines:
            s = str(opt or "")
            if "BOSS" in s or "杀怪" in s or "任务" in s:
                continue
            if tier and f"{tier}地宫传送" in s:
                has_service_entry = True
                break
            if s in ("地宫传送", "传送服务") or (
                s.endswith("地宫传送") and "BOSS" not in s
            ):
                has_service_entry = True
                break
        if not has_service_entry and tier:
            has_service_entry = any(
                f"{tier}地宫传送" in str(x or "")
                and "BOSS" not in str(x or "")
                for x in all_texts
            )

        # 决策（2026-07-22 日志修订）：
        # 1) 真二级：上层+深处都在 → dest
        # 2) 有「X级地宫传送」且还没出二级 → 必须继续 entry
        #    （禁止 entry 点一次就假进 dest；有任务口常点到杀怪行）
        # 3) 已有目标层文案（且无强制 entry）→ dest
        # 4) 仅任务面板（发放任务/杀怪）→ entry 多行试探，勿按 dest 盲点
        # 5) 升级/无任务直达 → dest
        want_deep = kind in ("dungeon_deep", "dungeon_lower")
        has_upper_side = _portal_has_layer_side(all_texts, "upper")
        has_deep_side = _portal_has_layer_side(all_texts, "deep")
        task_only = _portal_is_task_only_panel(all_texts, tier)
        max_entry_tries = 5
        if layer2_ready:
            stage_name = "dest"
            stage_kw = dest_kw
            needs_entry = False
            notes.append(f"r{round_i}:panel=layer2_direct")
        elif has_service_entry and (not layer2_ready) and entry_clicks < max_entry_tries:
            # 仍停在一级：继续点「X级地宫传送」（不因 entry_clicked 强切 dest）
            stage_name = "entry"
            stage_kw = entry_labels
            needs_entry = True
            notes.append(
                f"r{round_i}:panel=layer1_need_entry "
                f"taskish={_portal_taskish_row_count(all_texts)} "
                f"entry_clicks={entry_clicks}"
            )
        elif want_deep and has_deep_side and (not has_service_entry or layer2_ready):
            stage_name = "dest"
            stage_kw = dest_kw
            needs_entry = False
            notes.append(f"r{round_i}:panel=dest_deep_visible")
        elif (not want_deep) and has_upper_side and (not has_service_entry or layer2_ready):
            stage_name = "dest"
            stage_kw = dest_kw
            needs_entry = False
            notes.append(f"r{round_i}:panel=dest_upper_visible")
        elif task_only and entry_clicks < max_entry_tries:
            # 文案只有发放任务/杀怪：多半传送行还在下面，按 entry 多 slot 试
            stage_name = "entry"
            stage_kw = entry_labels
            needs_entry = True
            notes.append(f"r{round_i}:panel=task_only_probe_entry")
        elif entry_clicked and (has_upper_side or has_deep_side):
            # 入口后只扫到半边层文案：仍可按 kind 点，但 deep 无深处则勿假成功
            stage_name = "dest"
            stage_kw = dest_kw
            needs_entry = False
            notes.append(f"r{round_i}:panel=partial_after_entry")
        else:
            # 升级直达 / 文案不清
            stage_name = "dest"
            stage_kw = dest_kw
            needs_entry = False
            notes.append(
                f"r{round_i}:panel=fallback_dest "
                f"task_only={int(task_only)} up={int(has_upper_side)} "
                f"deep={int(has_deep_side)}"
            )
        # deep 目标但面板只有上层、没有深处且仍像一级 → 禁止点 dest slot1
        if (
            stage_name == "dest"
            and want_deep
            and (not has_deep_side)
            and (has_service_entry or has_upper_side)
            and entry_clicks < max_entry_tries
        ):
            stage_name = "entry"
            stage_kw = entry_labels
            needs_entry = True
            notes.append(f"r{round_i}:panel=block_deep_without_dest_text")

        notes.append(
            f"r{round_i}:stage={stage_name} layer2={int(layer2_ready)} "
            f"svc_entry={int(has_service_entry)} "
            f"entry_clicked={int(entry_clicked)} entry_clicks={entry_clicks} "
            f"needs_entry={int(needs_entry)} daily={int(daily_tier)} "
            f"upgrade={int(upgrade_tier)}"
        )

        # Tier check: never hard-fail when delv prefer_tid matches this NPC
        # (live text scan is polluted by other UI / BOSS titles).
        if tier and all_texts:
            mt0 = _portal_texts_match_tier(all_texts, tier)
            notes.append(
                f"r{round_i}:tier={tier} match={mt0} trust_tid={int(trust_tid)}"
            )
            if mt0 is False and not trust_tid:
                log(
                    f"task_api portal wrong tier menu tier={tier} "
                    f"texts={all_texts[:10]}"
                )
                return {
                    "ok": False,
                    "error": f"菜单不像{tier}地宫（见 {all_texts[:6]}）",
                    "note": f"wrong_tier tier={tier} texts={all_texts[:8]}",
                    "notes": notes,
                    "clicked": clicked,
                    "wrong_tier": True,
                }
            if mt0 is False and trust_tid:
                log(
                    f"task_api portal tier text mismatch ignored "
                    f"(trust delv tid={p_tid}) texts={all_texts[:8]}"
                )

        # Click slots: UI row order, NOT memory-scan order.
        # dest: 上层=0 深处=1 (2-row list). entry: usually single row → 0.
        prefer_idx: list[int] = []
        matched_names: list[str] = []
        option_lines = _portal_option_lines(all_texts)
        if option_lines:
            notes.append(f"r{round_i}:options={option_lines[:8]}")
            log(f"task_api portal menu r{round_i} options={option_lines[:8]}")
        if stage_name == "dest":
            # BOSS/deep must click 深处 slot only — never fall back to upper slot0.
            prefer_idx = [int(dest_slot)]
            if kind in ("dungeon_deep", "dungeon_lower") and int(dest_slot) >= 1:
                prefer_idx = [1]
            for opt in option_lines:
                if _text_matches_keywords(opt, stage_kw) >= 0:
                    matched_names.append(opt)
            # 没有深处文案时不要硬点 slot1（交给 stage 回退；此处再兜底）
            if kind in ("dungeon_deep", "dungeon_lower") and not _portal_has_layer_side(
                all_texts, "deep"
            ):
                notes.append(f"r{round_i}:dest_missing_deep_text")
        else:
            prefer_idx = _portal_entry_slot_guess(all_texts, entry_clicks=entry_clicks)
            for opt in option_lines:
                if _text_matches_keywords(opt, stage_kw) >= 0:
                    matched_names.append(opt)
        notes.append(
            f"r{round_i}:prefer_idx={prefer_idx} matched={matched_names[:4]}"
        )
        log(
            f"task_api portal menu r{round_i} stage={stage_name} "
            f"prefer_idx={prefer_idx} matched={matched_names[:4]}"
        )


        # 1) Named transfer buttons — only the target index for dest
        btn_names = list(PORTAL_BTN_CTRL_NAMES)
        if stage_name == "dest":
            # Prefer Btn_Trans{N+1} matching dest_slot
            want = f"Btn_Trans{dest_slot + 1}"
            ordered = [want] + [b for b in btn_names if b != want]
            # Do not try unrelated high indices that can be wrong layer
            if dest_slot == 0:
                ordered = [b for b in ordered if b not in ("Btn_Trans2", "Btn_Trans3", "Btn_Trans4")]
            elif dest_slot == 1:
                ordered = [b for b in ordered if b not in ("Btn_Trans3", "Btn_Trans4")]
                # try Trans2 before Trans1 for deep
                ordered = ["Btn_Trans2", "Btn_Trans1"] + [
                    b for b in ordered if b not in ("Btn_Trans2", "Btn_Trans1")
                ]
            btn_names = ordered

        for nm, dlg in dlgs:
            if stage_name == "dest" and nm not in (
                "Win_NPCTrans",
                "Win_NPC",
                "Win_NPCContent",
                "Win_NPCTemplate",
            ):
                continue
            for btn in btn_names:
                rect = _resolve_ctrl_rect(session, dlg, btn, log=log)
                if rect is None:
                    continue
                # Dest: only one button attempt per round
                if not _click_ctrl_rect(
                    session, rect, hwnd=hwnd, log=log, double=True
                ):
                    continue
                clicked.append(f"{nm}.{btn}")
                notes.append(f"r{round_i}:click {nm}.{btn}")
                log(f"task_api portal menu click {nm}.{btn}")
                if stage_name == "entry":
                    entry_clicked = True
                    entry_clicks += 1
                else:
                    dest_clicked = True
                if sleep_s(0.45):
                    return {
                        "ok": False,
                        "error": "menu select stopped",
                        "notes": notes,
                        "clicked": clicked,
                    }
                if stage_name == "dest":
                    sid = wait_scene(1.8)
                    if sid and before_scene is not None and sid != before_scene:
                        return finish_dest(sid, f"btn {nm}.{btn}")
                # One named button per dest round (avoid second option)
                if stage_name == "dest":
                    break
            if stage_name == "dest" and dest_clicked:
                break

        if scene_changed() and dest_clicked:
            sid = _scene_id_now(session)
            return finish_dest(sid, "scene after btn")

        # 2) List control — single preferred slot only
        list_hit = False
        for nm, dlg in dlgs:
            for lst in PORTAL_LIST_CTRL_NAMES:
                rect = _resolve_ctrl_rect(session, dlg, lst, log=log)
                if rect is None:
                    continue
                list_hit = True
                if stage_name == "entry":
                    n_slots = 5
                elif stage_name == "dest":
                    n_slots = 2
                else:
                    n_slots = 4
                pref = [i for i in prefer_idx if 0 <= int(i) < n_slots] or [0]
                changed, note = _click_list_slots(
                    session,
                    rect,
                    hwnd=hwnd,
                    slots=n_slots,
                    prefer_indices=pref,
                    max_clicks=1,
                    double=True,
                    fill_remaining=False,
                    band_top=0.05,
                    band_bottom=0.95,
                    micro_adjust=(stage_name == "dest"),
                    micro_tries=1,  # one shot; multi-micro is slow and noisy
                    log=log,
                    stop_event=stop_event,
                    before_scene=before_scene if stage_name == "dest" else None,
                )
                notes.append(f"r{round_i}:{nm}.{lst}:{note}")
                clicked.append(f"{nm}.{lst}:slot{prefer_idx[:1]}")
                if stage_name == "entry":
                    entry_clicked = True
                    entry_clicks += 1
                    if sleep_s(0.20):
                        return {
                            "ok": False,
                            "error": "menu select stopped",
                            "notes": notes,
                            "clicked": clicked,
                        }
                else:
                    dest_clicked = True
                if stage_name == "dest":
                    sid = wait_scene(1.8)
                    if sid and before_scene is not None and sid != before_scene:
                        return finish_dest(sid, note)
                break
            if list_hit:
                break

        # 3) Dialog-as-list fallback when named Lst_*/Btn_* not found.
        # NPC talk options live on Win_NPCContent/Win_NPC, not always Lst_Main.
        # Still ONE slot only (prefer_idx / dest_slot) — never multi-row spray.
        if not list_hit:
            notes.append(f"r{round_i}:no named list/btn; dlg-slot fallback")
            log(
                f"task_api portal menu r{round_i}: no Lst_/Btn_ rect; "
                f"dlg-slot fallback prefer_idx={prefer_idx}"
            )
            try:
                from app.core.plg_ui import read_dlg_rect
            except Exception:
                read_dlg_rect = None  # type: ignore
            dlg_order = (
                "Win_NPC",
                "Win_NPCContent",
                "Win_NPCTemplate",
                "Win_NPCTrans",
                "Win_NPCTalk",
            )
            # Prefer content dialogs first
            ordered_dlgs = sorted(
                dlgs,
                key=lambda it: (
                    dlg_order.index(it[0]) if it[0] in dlg_order else 99
                ),
            )
            for nm, dlg in ordered_dlgs:
                if nm not in dlg_order:
                    continue
                drect = None
                if read_dlg_rect is not None:
                    try:
                        drect = read_dlg_rect(session, dlg, name=nm, log=log)
                    except Exception:
                        drect = None
                if drect is None or not getattr(drect, "ok", False):
                    # last resort: try ctrl rect of dialog itself
                    drect = _resolve_ctrl_rect(session, dlg, nm, log=log)
                if drect is None or not getattr(drect, "ok", True):
                    # read_dlg_rect may not set ok on all paths; require size
                    try:
                        if int(getattr(drect, "w", 0) or 0) < 20:
                            continue
                    except Exception:
                        continue
                # 点击几何（用户两层模型）:
                #   entry 会话1: 单行 「X级地宫传送」— 多 Y/X 试探，不看污染 after_click
                #   dest  会话2/升级: 两行 上层=slot0 / 深处=slot1
                # 深处成功实机 y≈399-428 (dialog frac ~0.70-0.80)
                if stage_name == "entry":
                    # 有任务时列表多行：杀怪/发放/传送… 不能当 1 行盲喷
                    b_top, b_bot, n_slots = 0.28, 0.92, 5
                    pref = list(prefer_idx[:1]) or _portal_entry_slot_guess(
                        all_texts, entry_clicks=entry_clicks
                    )[:1]
                    # 每轮只点一个估计 slot，Y 微偏；下一轮换 slot
                    y_fracs = [0.50, 0.58, 0.42]
                    x_fracs = [0.40, 0.46, 0.34]
                    m_tries = 3
                    after_cb = None
                    use_double = True
                    try_wait = 0.35
                else:
                    b_top, b_bot, n_slots = 0.42, 0.92, 2
                    x_fracs = [0.38, 0.42, 0.48, 0.35]
                    if int(dest_slot) >= 1:
                        # 深处 / 下层
                        pref = [1]
                        y_fracs = [0.72, 0.78, 0.68, 0.82]
                        m_tries = 4
                    else:
                        # 上层 — 在深处上方
                        pref = [0]
                        y_fracs = [0.52, 0.58, 0.48, 0.62]
                        m_tries = 4
                    after_cb = None
                    use_double = True
                    try_wait = 0.55
                changed, note = _click_list_slots(
                    session,
                    drect,
                    hwnd=hwnd,
                    slots=n_slots,
                    prefer_indices=pref,
                    max_clicks=1,
                    double=bool(use_double),
                    fill_remaining=False,
                    band_top=b_top,
                    band_bottom=b_bot,
                    micro_adjust=False,
                    micro_tries=m_tries,
                    y_fracs=y_fracs,
                    x_fracs=x_fracs,
                    after_click=after_cb,
                    try_wait_s=try_wait,
                    log=log,
                    stop_event=stop_event,
                    before_scene=before_scene if stage_name == "dest" else None,
                )
                notes.append(f"r{round_i}:dlg-slot {nm}:{note}")
                clicked.append(f"{nm}:slot{pref[:1]}")
                if stage_name == "entry":
                    # 只记点击次数；是否进会话2 看下一轮文案是否 layer2_ready
                    entry_clicked = True
                    entry_clicks += 1
                    notes.append(
                        f"r{round_i}:entry_click slot={pref[:1]} clicks={entry_clicks}"
                    )
                    log(
                        f"task_api portal menu r{round_i}: "
                        f"entry click slot={pref[:1]} clicks={entry_clicks} "
                        f"(wait layer2 texts next round)"
                    )
                    if sleep_s(0.55):
                        return {
                            "ok": False,
                            "error": "menu select stopped",
                            "notes": notes,
                            "clicked": clicked,
                        }
                else:
                    dest_clicked = True
                if stage_name == "dest":
                    sid = wait_scene(1.8)
                    if sid and before_scene is not None and int(sid) != int(before_scene):
                        fin = finish_dest(sid, note)
                        if (
                            fin.get("wrong_layer")
                            and kind in ("dungeon_deep", "dungeon_lower")
                            and dest_slot >= 1
                        ):
                            log(
                                "task_api portal deep retry lower band after wrong upper"
                            )
                            notes.append(f"r{round_i}:deep_retry")
                        return fin
                # one dialog only per round
                break

        if sleep_s(0.20):
            return {
                "ok": False,
                "error": "menu select stopped",
                "notes": notes,
                "clicked": clicked,
            }

    sid = _scene_id_now(session)
    ok = bool(
        dest_clicked
        and before_scene is not None
        and sid
        and sid != before_scene
    )
    if ok:
        return finish_dest(sid, f"menu done scene {before_scene}->{sid}")
    err = "menu clicks done, scene unchanged"
    if kind in ("dungeon_deep", "dungeon_lower") and not any(
        "深处" in str(n) or "下层" in str(n) for n in notes
    ):
        # heuristic: never reached deep submenu
        if entry_clicks > 0 and not dest_clicked:
            err = "有任务一级入口未点进二级（未出现深处），请重试"
        elif entry_clicks > 0:
            err = "已点入口但深处未进图，scene unchanged"
    if not entry_clicks and not dest_clicked:
        err = "NPC菜单未点到传送选项（可能仍是任务面板）"
    return {
        "ok": False,
        "note": err,
        "notes": notes,
        "clicked": clicked,
        "after_scene": sid,
        "error": err,
    }



def _portal_function_needs_reopen(result: dict) -> bool:
    """True only when Hello succeeded but no portal menu became readable."""
    if not isinstance(result, dict) or result.get("ok"):
        return False
    if result.get("clicked"):
        return False
    stage = str(result.get("stage") or "").strip().lower()
    error = str(result.get("error") or result.get("note") or "").lower()
    return stage in ("", "none") and (
        "panel empty" in error or "no portal options" in error
    )


_PORTAL_MANUAL_INPUT_VKS = (
    0x01, 0x02, 0x04,  # mouse buttons
    0x20,  # space
    0x25, 0x26, 0x27, 0x28,  # arrow keys
    0x41, 0x44, 0x53, 0x57,  # A/D/S/W
    0x51, 0x45,  # Q/E
)


def _portal_manual_input_active(hwnd: int) -> bool:
    """True when the foreground game receives an explicit movement/mouse input."""
    hwnd_i = int(hwnd or 0)
    if not hwnd_i:
        return False
    try:
        user32 = ctypes.windll.user32
        if int(user32.GetForegroundWindow() or 0) != hwnd_i:
            return False
        return any(
            int(user32.GetAsyncKeyState(int(vk)) or 0) & 0x8000
            for vk in _PORTAL_MANUAL_INPUT_VKS
        )
    except Exception:
        return False


class _PortalMovementLease:
    """Bounded, serialized HostMove lease used only by synchronized portals."""

    def __init__(
        self,
        session,
        *,
        hwnd: int,
        scene_id: int | None,
        target: tuple[float, float, float],
        stop_event: threading.Event | None,
        timeout_s: float,
        interval_s: float = 0.45,
        log: LogFn | None = None,
    ) -> None:
        self.session = session
        self.hwnd = int(hwnd or 0)
        self.scene_id = int(scene_id or 0)
        self.target = tuple(float(v) for v in target)
        self.stop_event = stop_event
        self.interval_s = max(0.20, float(interval_s))
        lease_s = max(3.0, float(timeout_s))
        self.deadline = time.monotonic() + lease_s
        self.log = log or (lambda _m: None)
        self.active = True
        self.release_reason = ""
        self.last_attempt_at = 0.0
        self.moves = 0
        self.errors = 0
        self.log(
            "task_api portal movement lease acquire "
            f"scene={self.scene_id or '-'} xyz="
            f"({self.target[0]:.1f},{self.target[1]:.1f},{self.target[2]:.1f}) "
            f"timeout={lease_s:.1f}s"
        )

    def release(self, reason: str) -> None:
        if not self.active:
            return
        self.active = False
        self.release_reason = str(reason or "released")
        self.log(
            "task_api portal movement lease release "
            f"reason={self.release_reason} moves={self.moves} errors={self.errors}"
        )

    def to_dict(self) -> dict:
        return {
            "enabled": True,
            "active": bool(self.active),
            "release_reason": self.release_reason,
            "moves": int(self.moves),
            "errors": int(self.errors),
            "scene_id": self.scene_id or None,
            "target": list(self.target),
        }

    def pulse(self, reason: str = "poll", *, force: bool = False) -> bool:
        """Reassert the portal coordinate if due; never runs concurrently."""
        if not self.active:
            return False
        if self.stop_event is not None and self.stop_event.is_set():
            self.release("cancelled")
            return False
        if time.monotonic() >= self.deadline:
            self.release("timeout")
            return False
        if _portal_manual_input_active(self.hwnd):
            self.release("manual_input")
            return False
        now = time.monotonic()
        if not force and (now - self.last_attempt_at) < self.interval_s:
            return True
        self.last_attempt_at = now

        bridge = None
        try:
            from app.core.xajh_bridge import CMD_HOST_MOVE, ensure_bridge

            bridge = ensure_bridge(
                int(self.session.pid),
                hwnd=self.hwnd or None,
                inject_if_needed=False,
                log=lambda _m: None,
            )
            if bridge is None:
                raise RuntimeError("bridge not ready")
            snap = bridge.host_snapshot(hwnd=self.hwnd or None, timeout_ms=1500)
            if not snap.ok:
                raise RuntimeError(snap.error or "host snapshot failed")
            if not bool(int(snap.ret or 0)):
                self.release("role_missing")
                return False
            live_scene = int(getattr(snap, "mode", 0) or 0)
            if self.scene_id and live_scene > 0 and live_scene != self.scene_id:
                self.release("scene_changed")
                return False
            result = bridge.call(
                CMD_HOST_MOVE,
                x=self.target[0],
                y=self.target[1],
                z=self.target[2],
                mode=self.scene_id,
                hwnd=self.hwnd or None,
                timeout_ms=2500,
            )
            if not result.ok:
                raise RuntimeError(result.error or "HostMove failed")
            self.moves += 1
            self.errors = 0
            if self.moves == 1 or self.moves % 5 == 0:
                self.log(
                    "task_api portal movement lease pulse "
                    f"reason={reason} moves={self.moves}"
                )
            return True
        except Exception as exc:
            self.errors += 1
            self.log(
                "task_api portal movement lease error "
                f"reason={reason} errors={self.errors}: {exc}"
            )
            if self.errors >= 3:
                self.release("bridge_error")
            return self.active
        finally:
            if bridge is not None:
                try:
                    bridge.close()
                except Exception:
                    pass


def _dungeon_packet_transfer(
    session,
    *,
    open_packet: bytes | None = None,
    talk_packet: bytes,
    layer_packet: bytes,
    kind: str = "",
    before_scene: int | None = None,
    wait_scene_s: float = 12.0,
    stop_event: threading.Event | None = None,
    movement_pulse: Callable[[str], bool] | None = None,
    scene_reader: Callable[[], object] | None = None,
    log: LogFn | None = None,
) -> dict:
    """
    Packet-based dungeon transfer: 0A 会话前置包 → 0C 对话包 → 层包 → wait scene change.

    Replaces the unstable in-memory NPC dialog text grabbing for known dungeon
    entry NPCs.  Packets are fixed bytes and only depend on the role pid, so
    the same sequence works on every group-control (主/副控) character.
    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {"ok": False, "method": "packet", "packets": []}
    try:
        pid = int(getattr(session, "pid", 0) or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        out["error"] = "dungeon packet missing game pid"
        return out

    def _pulse(reason: str) -> None:
        if movement_pulse is not None:
            try:
                movement_pulse(reason)
            except Exception:
                pass

    def _wait(seconds: float, reason: str) -> bool:
        _pulse(reason)
        if stop_event is not None:
            return bool(stop_event.wait(max(0.0, float(seconds))))
        time.sleep(max(0.0, float(seconds)))
        return False

    try:
        from app.core.game_send import send_raw_packet

        if open_packet:
            log(
                f"task_api packet send open={open_packet.hex().upper()} "
                f"len={len(open_packet)}"
            )
            r0 = send_raw_packet(pid, open_packet, timeout_ms=3000, log=log)
            out["open_ret"] = r0
            out["packets"].append("open")
            if _wait(1.0, "packet_open_settle"):
                out["error"] = "dungeon packet stopped"
                return out
        log(
            f"task_api packet send talk={talk_packet.hex().upper()} "
            f"len={len(talk_packet)}"
        )
        r1 = send_raw_packet(pid, talk_packet, timeout_ms=3000, log=log)
        out["talk_ret"] = r1
        out["packets"].append("talk")
        if _wait(DUNGEON_PACKET_TALK_SETTLE_S, "packet_talk_settle"):
            out["error"] = "dungeon packet stopped"
            return out
        log(
            f"task_api packet send layer={layer_packet.hex().upper()} "
            f"len={len(layer_packet)}"
        )
        r2 = send_raw_packet(pid, layer_packet, timeout_ms=3000, log=log)
        out["layer_ret"] = r2
        out["packets"].append("layer")
    except Exception as e:
        out["error"] = f"dungeon packet send failed: {e}"
        out["packet_error"] = str(e)
        log(f"task_api packet send err: {e}")
        return out

    # Wait for scene change (server transfer); verify the layer afterwards.
    deadline = time.monotonic() + max(1.0, float(wait_scene_s))
    after_scene = before_scene
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            out["error"] = "dungeon packet stopped"
            return out
        _pulse("packet_scene_wait")
        try:
            sp = scene_reader() if scene_reader is not None else None
            if sp is None:
                from app.core.automove import read_scene_position

                sp = read_scene_position(session, log=lambda _m: None)
            if getattr(sp, "ok", False):
                after_scene = int(getattr(sp, "scene_id", 0) or 0)
                if (
                    before_scene is not None
                    and after_scene > 0
                    and after_scene != before_scene
                ):
                    chk = _portal_scene_layer_check(after_scene, kind or "dungeon_upper")
                    if chk.get("match") is False:
                        out["ok"] = False
                        out["wrong_layer"] = True
                        out["after_scene"] = after_scene
                        out["error"] = chk.get("note") or "进错层"
                        out["note"] = out["error"]
                        return out
                    out["ok"] = True
                    out["after_scene"] = after_scene
                    out["note"] = (
                        f"scene {before_scene}→{after_scene} via packet 对话+层"
                    )
                    if chk.get("label"):
                        out["note"] += f" | {chk.get('note')}"
                    return out
        except Exception:
            pass
        if _wait(0.35, "packet_scene_wait_gap"):
            out["error"] = "dungeon packet stopped"
            return out
    out["after_scene"] = after_scene
    out["error"] = (
        f"已到达地宫NPC并发送 对话/层 封包，但场景未变化 "
        f"(scene={after_scene}, want=上层/深处)"
    )
    out["note"] = out["error"]
    return out


def send_dungeon_portal_packets(
    session,
    portal: dict,
    *,
    portal_kind: str = "",
    before_scene: int | None = None,
    wait_scene_s: float = 12.0,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> dict:
    """Send fixed dungeon packets after path verification has returned."""
    kind = str(portal_kind or portal.get("portal_kind") or "").strip()
    open_packet = dungeon_open_packet_for(portal)
    talk_packet = dungeon_talk_packet_for(portal)
    layer_packet = dungeon_layer_packet_for(kind)
    if not open_packet or not talk_packet or not layer_packet:
        return {"ok": False, "error": "dungeon packet mapping unavailable"}
    return _dungeon_packet_transfer(
        session,
        open_packet=open_packet,
        talk_packet=talk_packet,
        layer_packet=layer_packet,
        kind=kind,
        before_scene=before_scene,
        wait_scene_s=float(wait_scene_s or 12.0),
        stop_event=stop_event,
        log=log,
    )

def use_task_portal_npc(
    session,
    portal: dict,
    *,
    hwnd: int = 0,
    arrive_radius: float = 8.0,
    path_timeout_s: float = 45.0,
    wait_scene_s: float = 12.0,
    portal_kind: str = "",
    dungeon_tier: str = "",
    movement_lock: bool = False,
    expected_origin_scene_id: int = 0,
    allow_live_rebind: bool = True,
    arrival_settle_s: float | None = None,
    defer_packet_send: bool = False,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> dict:
    """
    Walk to a live teleport NPC (地宫传送 / 宋瑶), open service, select options, wait transfer.

    Legal path only:
      HostMove → SetTarget + NPCSayHello → click dialog options
      (地宫传送 → 地宫上层/深处, by portal_kind).
    Does not forge free teleport packets. Scene change is the success signal.
    @author by ak
    """
    log = log or (lambda _m: None)
    from app.core.automove import read_scene_position

    name = str(portal.get("name") or portal.get("npc_name") or "传送NPC")
    oid = int(portal.get("obj_id") or 0)
    kind = str(portal_kind or portal.get("portal_kind") or "").strip()
    out: dict = {
        "ok": False,
        "method": "portal_npc",
        "portal_name": name,
        "portal_tid": int(portal.get("tid") or portal.get("npc_tid") or 0),
        "obj_id": oid,
        "portal_kind": kind,
    }
    expected_scene = int(expected_origin_scene_id or 0)
    generation_cache: dict | None = None
    generation_cache_at = 0.0

    def _generation_sample(stage: str, *, force: bool = False) -> dict:
        nonlocal generation_cache, generation_cache_at
        now = time.monotonic()
        if (
            not force
            and generation_cache is not None
            and (now - generation_cache_at) <= 0.12
        ):
            state = dict(generation_cache)
        else:
            state = _portal_scene_generation_state(
                session,
                hwnd=int(hwnd or 0),
                origin_scene_id=expected_scene,
                portal_kind=kind,
                log=log,
            )
            generation_cache = dict(state)
            generation_cache_at = now
        state["stage"] = stage
        return state

    def _generation_stop(stage: str) -> dict | None:
        if expected_scene <= 0:
            return None
        state = _generation_sample(stage)
        status = str(state.get("status") or "")
        if status == "current":
            return None
        already = status == "already_in_target_scene"
        note = _portal_generation_note(state)
        out.update(
            generation=state,
            origin_scene_id=expected_scene,
            after_scene=state.get("scene_id"),
            already_in_target_scene=already,
            stale_scene_generation=status == "stale_scene_generation",
            scene_transition=status == "scene_transition",
            safe_skip=True,
            ok=already,
            note=note,
            error=None if already else note,
        )
        log(
            "task_api portal generation stop "
            f"stage={stage} status={status} origin={expected_scene} "
            f"live={state.get('scene_id')}"
        )
        return out

    def _generation_abort() -> object:
        stopped = _generation_stop("path_poll")
        if stopped is None:
            return False
        return str((stopped.get("generation") or {}).get("status") or "scene_changed")

    def _safe_position_reader():
        from types import SimpleNamespace

        state = _generation_sample("position_sample")
        pos = state.get("position")
        return SimpleNamespace(
            ok=bool(
                state.get("ok")
                and state.get("role_present")
                and int(state.get("scene_id") or 0) > 0
                and pos is not None
            ),
            scene_id=int(state.get("scene_id") or 0),
            scene_pos=tuple(pos) if pos is not None else None,
        )

    stopped = _generation_stop("before_portal")
    if stopped is not None:
        return stopped
    before_scene = expected_scene or None
    if expected_scene <= 0:
        try:
            sp0 = read_scene_position(session, log=lambda _m: None)
            if getattr(sp0, "ok", False):
                _bs = int(getattr(sp0, "scene_id", 0) or 0)
                before_scene = _bs if _bs > 0 else None
        except Exception:
            before_scene = None
    out["before_scene"] = before_scene

    # 1) path to NPC if not already close
    try:
        dist = float(portal.get("dist")) if portal.get("dist") is not None else 99.0
    except (TypeError, ValueError):
        dist = 99.0
    if (
        portal.get("dist") is None
        and expected_scene > 0
        and portal.get("x") is not None
        and portal.get("z") is not None
    ):
        state = _generation_sample("initial_distance")
        pos = state.get("position")
        if str(state.get("status") or "") == "current" and pos is not None:
            try:
                dist = math.hypot(
                    float(pos[0]) - float(portal.get("x")),
                    float(pos[2]) - float(portal.get("z")),
                )
                out["initial_distance_source"] = "scene_snapshot"
                log(
                    "task_api portal derived missing sync distance "
                    f"from scene snapshot dist={dist:.2f}"
                )
            except (IndexError, TypeError, ValueError):
                dist = 99.0
    if dist > float(arrive_radius) and portal.get("x") is not None:
        clue = {
            "clue": "传送NPC",
            "kind": "npc",
            "name": name,
            "x": portal.get("x"),
            "y": portal.get("y"),
            "z": portal.get("z"),
            "tid": portal.get("tid") or portal.get("npc_tid"),
            "ptr": portal.get("ptr"),
            "obj_id": oid,
            "source": "portal_npc",
            "scene_id": expected_scene or None,
        }
        moved = pathfind_to_clue(
            session,
            clue,
            hwnd=int(hwnd or 0),
            arrive_radius=float(arrive_radius),
            verify_timeout_s=float(path_timeout_s),
            poll_s=0.35,
            stop_event=stop_event,
            abort_check=_generation_abort if expected_scene > 0 else None,
            position_reader=_safe_position_reader if expected_scene > 0 else None,
            log=log,
        )
        out["path"] = {
            "ok": bool(moved.get("ok")),
            "error": moved.get("error"),
            "distance": moved.get("last_distance"),
        }
        if not moved.get("ok"):
            stopped = _generation_stop("initial_path_failed")
            if stopped is not None:
                return stopped
            out["error"] = str(moved.get("error") or "failed to reach portal NPC")
            return out
    else:
        out["path"] = {"ok": True, "skipped": True, "distance": dist}
        log(
            f"task_api portal already near dist={dist:.2f}≤{arrive_radius} "
            f"tid={out.get('portal_tid')} oid={oid} — defer to live arrival verification"
        )

    # Face-hug / stale snapshot: rebind live obj_id by portal tid so SayHello works.
    try:
        p_tid = int(portal.get("tid") or portal.get("npc_tid") or out.get("portal_tid") or 0)
    except (TypeError, ValueError):
        p_tid = 0
    stopped = _generation_stop("before_live_rebind")
    if stopped is not None:
        return stopped
    if p_tid and allow_live_rebind:
        try:
            live_list = list_nearby_npcs(session, radius=40.0, limit=64, log=lambda _m: None)
        except Exception:
            live_list = []
        best = None
        best_d = 1e9
        for n in live_list or []:
            try:
                nt = int(n.get("tid") or n.get("npc_tid") or 0)
            except (TypeError, ValueError):
                continue
            if nt != p_tid:
                continue
            try:
                nd = float(n.get("dist")) if n.get("dist") is not None else 1e9
            except (TypeError, ValueError):
                nd = 1e9
            if nd < best_d:
                best_d = nd
                best = n
        if best is not None:
            try:
                live_oid = int(best.get("obj_id") or 0)
            except (TypeError, ValueError):
                live_oid = 0
            if live_oid:
                if live_oid != oid:
                    log(
                        f"task_api portal rebind oid {oid}→{live_oid} "
                        f"dist={best_d:.2f} tid={p_tid}"
                    )
                oid = live_oid
                out["obj_id"] = oid
                portal = dict(portal)
                portal["obj_id"] = oid
                if best.get("x") is not None:
                    portal["x"] = best.get("x")
                    portal["y"] = best.get("y")
                    portal["z"] = best.get("z")
                    portal["dist"] = best_d
                out["live_rebind"] = {"oid": oid, "dist": best_d, "tid": p_tid}

    if not oid:
        out["error"] = "portal npc missing obj_id (face-hug rebind failed)"
        return out

    # The portal action uses the same verified-arrival contract as "寻路NPC".
    # Snapshot distance is advisory only; never start a dialog before a fresh
    # coordinate read has converged on this live NPC.
    vx, vy, vz = portal.get("x"), portal.get("y"), portal.get("z")
    if vx is None or vz is None:
        out["error"] = "portal npc missing live xyz for arrival verification"
        return out
    approach = pathfind_to_clue(
        session,
        {
            "clue": "传送NPC",
            "kind": "npc",
            "name": name,
            "x": vx,
            "y": 0.0 if vy is None else vy,
            "z": vz,
            "tid": p_tid,
            "ptr": portal.get("ptr"),
            "obj_id": oid,
            "source": "portal_npc_live",
            "scene_id": expected_scene or None,
        },
        hwnd=int(hwnd or 0),
        arrive_radius=float(arrive_radius),
        verify_timeout_s=float(path_timeout_s),
        poll_s=0.35,
        stop_event=stop_event,
        abort_check=_generation_abort if expected_scene > 0 else None,
        position_reader=_safe_position_reader if expected_scene > 0 else None,
        log=log,
    )
    out["path"] = {
        "ok": bool(approach.get("ok")),
        "error": approach.get("error"),
        "distance": approach.get("last_distance"),
        "position": approach.get("last_position"),
    }
    if not approach.get("ok"):
        stopped = _generation_stop("approach_failed")
        if stopped is not None:
            return stopped
        out["error"] = str(approach.get("error") or "failed to verify portal arrival")
        return out

    movement_lease: _PortalMovementLease | None = None
    if movement_lock:
        movement_lease = _PortalMovementLease(
            session,
            hwnd=int(hwnd or 0),
            scene_id=before_scene,
            target=(float(vx), float(0.0 if vy is None else vy), float(vz)),
            stop_event=stop_event,
            timeout_s=max(3.0, float(path_timeout_s) + float(wait_scene_s)),
            log=log,
        )
        movement_lease.pulse("arrival", force=True)

    def _movement_pulse(reason: str) -> bool:
        if movement_lease is None:
            return False
        return movement_lease.pulse(reason)

    def _movement_release(reason: str) -> None:
        if movement_lease is not None:
            movement_lease.release(reason)

    def _finish(reason: str) -> dict:
        if movement_lease is not None:
            movement_lease.release(reason)
            out["movement_lock"] = movement_lease.to_dict()
        return out

    def _guarded_wait(seconds: float, reason: str) -> bool:
        if movement_lease is None:
            if stop_event is not None:
                return bool(stop_event.wait(max(0.0, float(seconds))))
            time.sleep(max(0.0, float(seconds)))
            return False
        deadline = time.monotonic() + max(0.0, float(seconds))
        while time.monotonic() < deadline:
            _movement_pulse(reason)
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

    settle_s = (
        PORTAL_DIALOG_POST_ARRIVAL_S
        if arrival_settle_s is None
        else max(0.0, float(arrival_settle_s))
    )
    log(
        f"task_api portal arrival verified; wait {settle_s:.2f}s "
        "before dialog"
    )
    stopped = _generation_stop("before_dialog")
    if stopped is not None:
        return stopped
    if _guarded_wait(settle_s, "post_arrival"):
        out["error"] = "portal open stopped"
        return _finish("cancelled")
    stopped = _generation_stop("after_dialog_wait")
    if stopped is not None:
        return _finish(
            str((stopped.get("generation") or {}).get("status") or "scene_changed")
        )

    if defer_packet_send:
        out["ok"] = True
        out["portal_ready"] = True
        out["note"] = "portal arrival verified; packet send deferred"
        return _finish("packet_deferred")

    # 2p) Packet-based transfer for dungeon entry NPCs (对话封包 → 上层/深处封包).
    # Replaces the unstable in-memory dialog text grabbing when the NPC has a
    # captured talk packet.  For 群控 the same fixed bytes run per role pid.
    layer_kind = str(kind or "").strip()
    open_pkt = dungeon_open_packet_for(portal)
    talk_pkt = dungeon_talk_packet_for(portal)
    layer_pkt = dungeon_layer_packet_for(layer_kind)
    if open_pkt and talk_pkt and layer_pkt:
        pkt_ret = _dungeon_packet_transfer(
            session,
            open_packet=open_pkt,
            talk_packet=talk_pkt,
            layer_packet=layer_pkt,
            kind=layer_kind,
            before_scene=before_scene,
            wait_scene_s=float(wait_scene_s or 12.0),
            stop_event=stop_event,
            movement_pulse=_movement_pulse,
            scene_reader=_safe_position_reader if expected_scene > 0 else None,
            log=log,
        )
        out["packet"] = pkt_ret
        if pkt_ret.get("ok"):
            out["ok"] = True
            out["after_scene"] = pkt_ret.get("after_scene")
            out["note"] = pkt_ret.get("note") or "packet transfer"
            log(f"task_api portal ok packet {out['note']}")
            return _finish("scene_changed")
        # 地宫入口 NPC 已确认对话封包：发包失败即失败，不再回退内存抓文本。
        out["ok"] = False
        out["error"] = str(pkt_ret.get("error") or "dungeon packet transfer failed")
        out["note"] = out["error"]
        log(f"task_api portal packet fail: {out['error']}")
        return _finish("packet_failed")

    # 2) open service panel — retry hello a few times (transfer NPCs often need it)
    # Close stale NPC panel from a previous portal attempt (else SayHello ret≠1).
    try:
        from app.core.sys_input import send_key_press

        send_key_press(0x1B, hwnd=int(hwnd or 0))  # VK_ESCAPE
        _guarded_wait(0.15, "close_stale_dialog")
    except Exception:
        pass
    dlg_notes: list[str] = []
    hello_ok = False
    for attempt in range(1, 3):  # at most 2 hellos; first ok is enough
        if stop_event is not None and stop_event.is_set():
            out["error"] = "portal open stopped"
            return _finish("cancelled")
        stopped = _generation_stop(f"before_hello#{attempt}")
        if stopped is not None:
            return _finish(
                str(
                    (stopped.get("generation") or {}).get("status")
                    or "scene_changed"
                )
            )
        _movement_pulse(f"before_hello#{attempt}")
        try:
            # Face-hug / second open: give dialog a bit more time to fill host table.
            near_mode = bool((out.get("path") or {}).get("skipped"))
            settle = 0.65 if (attempt == 1 and near_mode) else (0.50 if attempt == 1 else 0.40)
            note = _open_task_npc_dialog(
                session,
                npc_id=oid,
                hwnd=int(hwnd or 0),
                settle_s=settle,
                log=log,
            )
            dlg_notes.append(f"try{attempt}:{note}")
            if "NPCSayHello ok=True" in note or "ret=1" in note:
                hello_ok = True
        except Exception as e:
            dlg_notes.append(f"try{attempt}:err:{e}")
            log(f"task_api portal dialog try{attempt} fail: {e}")
        # Short settle; near-mode waits longer for host labels to decode.
        _gap = 0.55 if bool((out.get("path") or {}).get("skipped")) else 0.35
        if _guarded_wait(_gap, f"hello_gap#{attempt}"):
            out["error"] = "portal open stopped"
            out["dialog"] = "; ".join(dlg_notes)
            return _finish("cancelled")
        # Early exit if scene already changed to a real positive layer-matched id.
        try:
            sp = (
                _safe_position_reader()
                if expected_scene > 0
                else read_scene_position(session, log=lambda _m: None)
            )
            if getattr(sp, "ok", False):
                sid = int(getattr(sp, "scene_id", 0) or 0)
                if (
                    before_scene is not None
                    and sid > 0
                    and sid != before_scene
                ):
                    chk = _portal_scene_layer_check(sid, kind or "dungeon_upper")
                    if chk.get("match") is False:
                        log(f"task_api portal early wrong_layer: {chk.get('note')}")
                    else:
                        out["ok"] = True
                        out["after_scene"] = sid
                        out["dialog"] = "; ".join(dlg_notes)
                        out["note"] = f"scene {before_scene}→{sid} via {name}"
                        log(f"task_api portal ok early {out['note']}")
                        return _finish("scene_changed")
        except Exception:
            pass
        if hello_ok:
            break
    out["dialog"] = "; ".join(dlg_notes)

    # 2b) Function path: talk_proc opt_id + bridge UI-thread AA3330.
    # NO geometry clicks. Entry (X级地宫传送) then dest (上层/深处) by option id.
    if not out.get("ok"):
        try:
            from app.core.npc_service_mem import portal_select_by_function

            _kind_mem = kind or (
                "dungeon_deep"
                if any(k in name for k in ("深处", "下层", "BOSS", "boss"))
                else "dungeon_upper"
            )
            _layer_mem = (
                "deep"
                if _kind_mem in ("dungeon_deep", "dungeon_lower", "deep", "lower")
                else "upper"
            )
            _tier_mem = str(dungeon_tier or portal.get("dungeon_tier") or "").strip()
            try:
                _p_tid_fn = int(portal.get("tid") or portal.get("npc_tid") or 0)
            except (TypeError, ValueError):
                _p_tid_fn = 0
            fn = portal_select_by_function(
                session,
                tier=_tier_mem,
                layer=_layer_mem,
                portal_tid=_p_tid_fn,
                hwnd=int(hwnd or 0),
                log=log,
                settle_s=0.30,
                wait_scene_s=float(wait_scene_s or 12.0),
                before_scene=before_scene,
                stop_event=stop_event,
                movement_pulse=_movement_pulse,
                movement_release=_movement_release,
                scene_reader=_safe_position_reader if expected_scene > 0 else None,
            )
            if _portal_function_needs_reopen(fn):
                retry_note = _open_task_npc_dialog(
                    session,
                    npc_id=oid,
                    hwnd=int(hwnd or 0),
                    settle_s=0.70,
                    log=log,
                )
                out["dialog_function_retry"] = retry_note
                log(
                    "task_api portal talk_fn panel empty; reopen same NPC once: "
                    f"{retry_note}"
                )
                if not _guarded_wait(0.35, "reopen_dialog"):
                    fn = portal_select_by_function(
                        session,
                        tier=_tier_mem,
                        layer=_layer_mem,
                        portal_tid=_p_tid_fn,
                        hwnd=int(hwnd or 0),
                        log=log,
                        settle_s=0.45,
                        wait_scene_s=float(wait_scene_s or 12.0),
                        before_scene=before_scene,
                        stop_event=stop_event,
                        movement_pulse=_movement_pulse,
                        movement_release=_movement_release,
                        scene_reader=(
                            _safe_position_reader if expected_scene > 0 else None
                        ),
                    )
            out["menu_fn"] = {
                "ok": bool(fn.get("ok")),
                "stage": fn.get("stage"),
                "clicked": fn.get("clicked") or [],
                "note": fn.get("note") or fn.get("error"),
                "options": (fn.get("options") or [])[:8],
                "notes": (fn.get("notes") or [])[:12],
            }
            log(
                f"task_api portal talk_fn ok={fn.get('ok')} stage={fn.get('stage')} "
                f"clicked={fn.get('clicked')} note={fn.get('note') or fn.get('error')}"
            )
            if fn.get("ok"):
                sid = fn.get("after_scene")
                try:
                    sid_i = int(sid) if sid is not None else 0
                except (TypeError, ValueError):
                    sid_i = 0
                if sid_i <= 0:
                    sid = _scene_id_now(session)
                    try:
                        sid_i = int(sid) if sid is not None else 0
                    except (TypeError, ValueError):
                        sid_i = 0
                if sid_i <= 0:
                    out["ok"] = False
                    out["after_scene"] = sid
                    out["error"] = (
                        f"talk_fn claimed ok but scene invalid ({sid}); "
                        f"notes={fn.get('note') or fn.get('error')}"
                    )
                    out["note"] = out["error"]
                    log(f"task_api portal reject invalid scene: {out['error']}")
                    # fall through to fail handling below
                else:
                    chk = _portal_scene_layer_check(sid_i, kind or "dungeon_upper")
                    if chk.get("match") is False:
                        out["ok"] = False
                        out["wrong_layer"] = True
                        out["after_scene"] = sid_i
                        out["error"] = chk.get("note") or "进错层"
                        out["note"] = out["error"]
                        log(f"task_api portal wrong_layer fn: {out['error']}")
                        return _finish("wrong_layer")
                    # dungeon kinds: require known layer match (not unknown)
                    k = str(kind or "").strip().lower()
                    if k in (
                        "dungeon_deep",
                        "dungeon_lower",
                        "dungeon_upper",
                    ) and chk.get("match") is not True:
                        out["ok"] = False
                        out["after_scene"] = sid_i
                        out["error"] = chk.get("note") or (
                            f"scene {sid_i} layer unknown for {kind}"
                        )
                        out["note"] = out["error"]
                        log(f"task_api portal layer unknown fn: {out['error']}")
                        return _finish("layer_unknown")
                    out["ok"] = True
                    out["after_scene"] = sid_i
                    out["note"] = fn.get("note") or (
                        f"scene {before_scene}→{sid_i} via talk_fn"
                    )
                    log(f"task_api portal ok talk_fn {out['note']}")
                    return _finish("scene_changed")
            out["menu_fn_err"] = fn.get("error")
            out["menu_fn_notes"] = (fn.get("notes") or [])[:16]
            out["menu_fn_texts"] = fn.get("texts") or []
            out["menu_fn_host"] = fn.get("host_poll") or fn.get("host0") or fn.get("host2")
            # Critical: if function path could not select, do not fall into
            # 12s scene-wait pretending we already clicked menu slots.
            if not fn.get("ok"):
                out["ok"] = False
                out["error"] = str(
                    fn.get("error")
                    or fn.get("note")
                    or "talk_fn failed (no opt_id / no select)"
                )
                out["note"] = out["error"]
                log(
                    "task_api portal talk_fn fail: "
                    f"{out['error']} notes={out.get('menu_fn_notes')}"
                )
        except Exception as e:
            out["menu_fn_err"] = str(e)
            log(f"task_api portal talk_fn err: {e}")
            out["ok"] = False
            out["error"] = f"talk_fn exception: {e}"
            out["note"] = out["error"]

    # 2c) Geometry fallback DISABLED by default (coords failed too many times).
    # Set portal["allow_geo_fallback"]=True only for lab diagnosis.
    allow_geo = bool(portal.get("allow_geo_fallback") or False)
    if (not out.get("ok")) and not allow_geo:
        log(
            "task_api portal skip geo fallback "
            f"(talk_fn failed: {out.get('menu_fn_err') or out.get('menu_fn')})"
        )
        # Fail fast: Hello alone is not a transfer.
        if not out.get("error"):
            out["error"] = str(
                out.get("menu_fn_err")
                or "portal talk_fn failed and geo fallback disabled"
            )
            out["note"] = out["error"]
        return _finish("portal_failed")
    if (not out.get("ok")) and allow_geo:
        try:
            try:
                _p_tid = int(portal.get("tid") or portal.get("npc_tid") or 0)
            except (TypeError, ValueError):
                _p_tid = 0
            try:
                _pref = int(
                    portal.get("prefer_tid")
                    or portal.get("delv_tid")
                    or 0
                )
            except (TypeError, ValueError):
                _pref = 0
            menu = _select_portal_npc_menu(
                session,
                portal_kind=kind
                or (
                    "dungeon_deep"
                    if any(k in name for k in ("深处", "下层", "BOSS", "boss"))
                    else "dungeon_upper"
                ),
                dungeon_tier=str(dungeon_tier or ""),
                portal_tid=_p_tid,
                prefer_tid=_pref or _p_tid,
                hwnd=int(hwnd or 0),
                before_scene=before_scene,
                stop_event=stop_event,
                log=log,
            )
            out["menu"] = {
                "ok": bool(menu.get("ok")),
                "note": menu.get("note"),
                "clicked": menu.get("clicked") or [],
                "notes": (menu.get("notes") or [])[:16],
            }
            if menu.get("wrong_layer"):
                # Live: BOSS deep clicked wrong band → 上层; do NOT report success
                out["ok"] = False
                out["wrong_layer"] = True
                out["after_scene"] = menu.get("after_scene")
                out["error"] = menu.get("error") or menu.get("note") or "进错层"
                out["note"] = out["error"]
                log(f"task_api portal wrong_layer: {out['error']}")
                return _finish("wrong_layer")
            if menu.get("wrong_tier"):
                out["wrong_tier"] = True
                out["error"] = menu.get("error") or menu.get("note")
                out["note"] = out["error"]
                log(f"task_api portal wrong_tier: {out['error']}")
                return _finish("wrong_tier")
            if menu.get("ok"):
                sid = menu.get("after_scene") or _scene_id_now(session)
                # double-check layer even if menu said ok
                chk = _portal_scene_layer_check(sid, kind or "dungeon_upper")
                if chk.get("match") is False:
                    out["ok"] = False
                    out["wrong_layer"] = True
                    out["after_scene"] = sid
                    out["error"] = chk.get("note") or "进错层"
                    out["note"] = out["error"]
                    log(f"task_api portal wrong_layer post: {out['error']}")
                    return _finish("wrong_layer")
                out["ok"] = True
                out["after_scene"] = sid
                out["note"] = (
                    f"scene {before_scene}→{sid} via {name} "
                    f"menu={menu.get('note') or 'ok'}"
                )
                log(f"task_api portal ok menu {out['note']}")
                return _finish("scene_changed")
            notes_m = menu.get("note") or "menu incomplete"
            detail = menu.get("notes") or []
            clicked = menu.get("clicked") or []
            log(
                f"task_api portal menu: {notes_m} "
                f"clicked={clicked[:8]} notes={detail[:10]}"
            )
        except Exception as e:
            out["menu"] = {"ok": False, "error": str(e)}
            log(f"task_api portal menu err: {e}")

    # 3) wait for scene change (server transfer)
    deadline = time.monotonic() + max(1.0, float(wait_scene_s))
    after_scene = before_scene
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            out["error"] = "portal wait stopped"
            return _finish("cancelled")
        _movement_pulse("scene_wait")
        try:
            sp = (
                _safe_position_reader()
                if expected_scene > 0
                else read_scene_position(session, log=lambda _m: None)
            )
            if getattr(sp, "ok", False):
                after_scene = int(getattr(sp, "scene_id", 0) or 0)
                # Never accept -1/0 transition as success.
                if (
                    before_scene is not None
                    and after_scene > 0
                    and after_scene != before_scene
                ):
                    chk = _portal_scene_layer_check(
                        after_scene, kind or "dungeon_upper"
                    )
                    if chk.get("match") is False:
                        out["ok"] = False
                        out["wrong_layer"] = True
                        out["after_scene"] = after_scene
                        out["error"] = chk.get("note") or "进错层"
                        out["note"] = out["error"]
                        log(f"task_api portal wrong_layer wait: {out['error']}")
                        return _finish("wrong_layer")
                    k = str(kind or "").strip().lower()
                    if k in (
                        "dungeon_deep",
                        "dungeon_lower",
                        "dungeon_upper",
                    ) and chk.get("match") is not True:
                        # keep waiting for a known layer scene
                        pass
                    else:
                        out["ok"] = True
                        out["after_scene"] = after_scene
                        out["note"] = f"scene {before_scene}->{after_scene} via {name}"
                        if chk.get("label"):
                            out["note"] += f" | {chk.get('note')}"
                        log(f"task_api portal ok {out['note']}")
                        return _finish("scene_changed")
        except Exception:
            pass
        _guarded_wait(0.35, "scene_wait_gap")

    # Dialog opened but scene unchanged — treat as incomplete (not success).
    out["after_scene"] = after_scene
    out["ok"] = False
    out["partial"] = True
    menu_note = ""
    try:
        menu_note = str((out.get("menu") or {}).get("note") or "")
    except Exception:
        menu_note = ""
    want = (
        "地宫深处/下层"
        if kind in ("dungeon_deep", "dungeon_lower")
        else ("天下会" if kind == "tianxiahui" else "地宫上层")
    )
    clicked_any = False
    try:
        clicked_any = bool((out.get("menu") or {}).get("clicked")) or bool(
            (out.get("menu_fn") or {}).get("clicked")
        )
    except Exception:
        clicked_any = False
    out["error"] = (
        f"已到达并打开 {name} 对话，但场景未变化 "
        f"(scene={after_scene}, want={want}, kind={kind or '?'})；"
        + (
            "已尝试菜单选层"
            if clicked_any
            else "但未能取得 talk_proc opt_id / 未执行选层"
        )
        + (f"（{menu_note}）" if menu_note else "")
        + f"；talk_fn={out.get('menu_fn_err') or (out.get('menu_fn') or {}).get('note')}"
    )
    out["note"] = out["error"]
    log(f"task_api portal partial {name} scene={after_scene}")
    return _finish("transfer_timeout")


def pathfind_task(
    session,
    task: TaskInfo | dict,
    *,
    hwnd: int = 0,
    arrive_radius: float = 12.0,
    verify_timeout_s: float = 120.0,
    poll_s: float = 0.5,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    prefer_portal: bool = True,
    portal_radius: float = 80.0,
    portal_move_guard: bool = False,
    portal_sync_guard: bool = False,
    force_portal_route: bool = False,
    portal_origin_scene_id: int = 0,
    sync_route_guard: bool = False,
    portal_only: bool = False,
    portal_arrival_settle_s: float | None = None,
) -> dict:
    """
    Resolve route for an accepted task.

    prefer_portal=True: if task looks like 地宫/天下会, walk to 地宫传送/宋瑶
    and open NPC transfer instead of long HostMove run across the map.
    """
    log = log or (lambda _m: None)
    row = task.to_dict() if isinstance(task, TaskInfo) else dict(task or {})
    tid = int(row.get("task_id") or 0)
    if not tid:
        return {"ok": False, "error": "task id required"}

    # A synchronized ordinary NPC path must consume the master's verified
    # route snapshot directly.  Re-resolving the task on every slave used to
    # enumerate live objects and call plg::GetObjectTemplateID (0x827260) on
    # pointers that can disappear during movement/scene churn.  Production
    # evidence shows that call hanging immediately before STACK_OVERFLOW.
    # Fail closed when an old sender did not include a route snapshot; never
    # fall back to the unsafe live scan on a slave.
    if sync_route_guard and not prefer_portal:
        try:
            route_x = (
                float(row.get("portal_x"))
                if row.get("portal_x") is not None
                else None
            )
            route_y = float(row.get("portal_y") or 0.0)
            route_z = (
                float(row.get("portal_z"))
                if row.get("portal_z") is not None
                else None
            )
            route_scene = int(row.get("origin_scene_id") or 0)
            route_tid = int(row.get("portal_tid") or 0)
            route_obj_id = int(row.get("portal_obj_id") or 0)
        except (TypeError, ValueError):
            route_x = route_z = None
            route_y = 0.0
            route_scene = route_tid = route_obj_id = 0
        if route_x is None or route_z is None:
            return {
                "ok": False,
                "task_id": tid,
                "safe_skip": True,
                "error": "同步NPC指令缺少路线快照，已禁止危险对象扫描",
            }
        route = {
            "clue": "同步任务目标",
            "kind": "npc",
            "name": str(row.get("name") or f"任务#{tid}"),
            "x": route_x,
            "y": route_y,
            "z": route_z,
            "dist": None,
            "tid": route_tid or None,
            "scene_id": route_scene or None,
            "ptr": None,
            "obj_id": route_obj_id or None,
            "task_id": tid,
            "can_turnin": bool(row.get("can_finish")),
            "source": "sync_route_snapshot",
        }
        log(
            "task_api sync route snapshot "
            f"task={tid} tid={route_tid or '-'} scene={route_scene or '-'} "
            f"xyz=({route_x},{route_y},{route_z})"
        )
        result = pathfind_to_clue(
            session,
            route,
            hwnd=int(hwnd or 0),
            arrive_radius=float(arrive_radius),
            verify_timeout_s=float(verify_timeout_s),
            poll_s=float(poll_s),
            stop_event=stop_event,
            log=log,
        )
        result["task_id"] = tid
        result["method"] = result.get("method") or "host_move"
        result["route_phase"] = (
            "turnin" if bool(row.get("can_finish")) else "objective"
        )
        result["route"] = route
        result["route_candidates"] = [{**route, "selected": True}]
        result["sync_route_snapshot"] = True
        return result

    # The task-list row can be older than the moment the user clicks it.  A
    # false -> true transition changes the route from objective/portal to the
    # AwardNPC, so refresh only that safe direction before deciding the phase.
    if not row.get("can_finish") and not portal_sync_guard and not force_portal_route:
        try:
            if task_can_finish(session, tid, log=lambda _m: None):
                row["can_finish"] = True
                row["route_state_refreshed"] = True
                log(f"task_api route state refresh id={tid}: CanFinish false->true")
        except Exception as e:
            log(f"task_api route state refresh id={tid} skipped: {e}")

    # --- Portal shortcut (legal NPC transfer) ---
    # Only for incomplete objectives; turn-in still walks AwardNPC.
    if prefer_portal and (force_portal_route or not row.get("can_finish")):
        kind = classify_task_portal_kind(row)
        if kind:
            origin_scene = int(
                portal_origin_scene_id or row.get("origin_scene_id") or 0
            )
            strict_sync_portal = bool(portal_sync_guard)
            if strict_sync_portal:
                if origin_scene <= 0:
                    return {
                        "ok": False,
                        "task_id": tid,
                        "portal_kind": kind,
                        "safe_skip": True,
                        "error": "同步地宫指令缺少来源场景，已安全跳过",
                    }
                generation = _portal_scene_generation_state(
                    session,
                    hwnd=int(hwnd or 0),
                    origin_scene_id=origin_scene,
                    portal_kind=kind,
                    log=log,
                )
                status = str(generation.get("status") or "")
                if status != "current":
                    already = status == "already_in_target_scene"
                    note = _portal_generation_note(generation)
                    return {
                        "ok": already,
                        "task_id": tid,
                        "method": "portal_npc",
                        "portal_kind": kind,
                        "origin_scene_id": origin_scene,
                        "after_scene": generation.get("scene_id"),
                        "generation": generation,
                        "already_in_target_scene": already,
                        "stale_scene_generation": status
                        == "stale_scene_generation",
                        "scene_transition": status == "scene_transition",
                        "safe_skip": True,
                        "note": note,
                        "error": None if already else note,
                    }
                try:
                    hint_complete = bool(
                        row.get("portal_x") is not None
                        and row.get("portal_z") is not None
                        and int(row.get("portal_obj_id") or 0) > 0
                        and int(row.get("portal_tid") or 0) > 0
                    )
                except (TypeError, ValueError):
                    hint_complete = False
                if not hint_complete:
                    return {
                        "ok": False,
                        "task_id": tid,
                        "portal_kind": kind,
                        "origin_scene_id": origin_scene,
                        "safe_skip": True,
                        "error": "同步地宫指令缺少传送口快照，已禁止危险 NPC 扫描",
                    }
            prefer_tid = 0
            try:
                prefer_tid = int(
                    row.get("portal_tid")
                    or row.get("delv_tid")
                    or row.get("award_tid")
                    or 0
                )
            except (TypeError, ValueError):
                prefer_tid = 0
            if not prefer_tid:
                try:
                    meta = read_task_npc_tids(session, tid, log=log)
                    prefer_tid = int(
                        meta.get("delv_tid") or meta.get("award_tid") or 0
                    )
                except Exception as e:
                    log(f"task_api portal delv tid: {e}")
                    prefer_tid = 0
            # delv tid is ground truth for 初/中/高/升级 portal instance
            tier = resolve_dungeon_tier(row, prefer_tid=prefer_tid)
            # Legal path only: live 地宫传送 NPC in city (e.g. 福州) → menu 上层/深处.
            # HostMove/INSTANCE free-enter is NOT used for daily BOSS (false scene=-1).
            portal_hint = None
            try:
                if (
                    row.get("portal_x") is not None
                    and row.get("portal_z") is not None
                    and int(row.get("portal_obj_id") or 0) > 0
                    and int(row.get("portal_tid") or prefer_tid or 0) > 0
                ):
                    portal_hint = {
                        "name": row.get("portal_name") or "地宫传送",
                        "tid": int(row.get("portal_tid") or prefer_tid or 0),
                        "obj_id": int(row.get("portal_obj_id") or 0),
                        "x": float(row.get("portal_x")),
                        "y": float(row.get("portal_y") or 0.0),
                        "z": float(row.get("portal_z")),
                        "dist": row.get("portal_dist"),
                    }
            except (TypeError, ValueError):
                portal_hint = None
            if strict_sync_portal:
                if portal_hint is None:
                    return {
                        "ok": False,
                        "task_id": tid,
                        "portal_kind": kind,
                        "origin_scene_id": origin_scene,
                        "safe_skip": True,
                        "error": "同步地宫指令缺少传送口快照，已禁止危险 NPC 扫描",
                    }
                # Synchronized portals must never enumerate live NPC templates:
                # GetObjectTemplateID can dereference a freed object during load.
                npcs = [portal_hint]
            else:
                npcs = []
                # 刚过图/飞回某场景时，NPC 对象列表可能尚未枚举完成，瞬时扫到 0 个，
                # 从而误判“附近无传送口”。此处短暂重试几次，等场景 NPC 加载完成。
                npc_retries = 5
                for _attempt in range(npc_retries):
                    try:
                        npcs = list_nearby_npcs(
                            session, radius=float(portal_radius), limit=48, log=log
                        )
                    except Exception as e:
                        npcs = []
                        log(f"task_api portal scan fail: {e}")
                    if npcs:
                        break
                    if stop_event is not None and stop_event.is_set():
                        break
                    if _attempt + 1 >= npc_retries:
                        break
                    time.sleep(2.0)
            _cand_n = 1 if prefer_tid else 5
            cands = list_portal_npc_candidates(
                npcs, kind, tier=tier, prefer_tid=prefer_tid, max_n=_cand_n
            )
            # Name-read failures / far portals: expand once and match by tid.
            if not cands and not strict_sync_portal:
                try:
                    npcs2 = list_nearby_npcs(
                        session,
                        radius=max(float(portal_radius), 200.0),
                        limit=64,
                        log=log,
                    )
                except Exception as e:
                    npcs2 = []
                    log(f"task_api portal rescan fail: {e}")
                if npcs2:
                    npcs = npcs2
                cands = list_portal_npc_candidates(
                    npcs, kind, tier=tier, prefer_tid=prefer_tid, max_n=_cand_n
                )
            if not cands and npcs:
                # Debug why empty: sample nearby names/tids (short).
                sample = ",".join(
                    f"{(c.get('name') or '?')[:12]}#{c.get('tid')}"
                    for c in npcs[:8]
                )
                log(
                    f"task_api portal no-cand sample nearby={sample} "
                    f"prefer_tid={prefer_tid or '-'} tier={tier or '-'}"
                )
            last_pr: dict = {}
            if cands:
                log(
                    f"task_api portal kind={kind} tier={tier or '-'} "
                    f"prefer_tid={prefer_tid or '-'} cands="
                    + ",".join(
                        f"{c.get('name')}@d={c.get('dist')}"
                        f"/tid={c.get('tid')}/r={c.get('_portal_rank')}"
                        for c in cands[:5]
                    )
                    + f" task={tid}"
                )
                for portal in cands:
                    if stop_event is not None and stop_event.is_set():
                        return {
                            "ok": False,
                            "task_id": tid,
                            "error": "portal stopped",
                            "portal_kind": kind,
                            "dungeon_tier": tier,
                        }
                    log(
                        f"task_api portal try pick={portal.get('name')} "
                        f"dist={portal.get('dist')} tid={portal.get('tid')} "
                        f"rank={portal.get('_portal_rank')} tier={tier or '-'}"
                    )
                    portal = dict(portal)
                    portal["prefer_tid"] = prefer_tid
                    portal["delv_tid"] = prefer_tid
                    if portal_only:
                        clue = {
                            "clue": str(portal.get("name") or "传送NPC"),
                            "kind": "npc",
                            "name": str(portal.get("name") or "传送NPC"),
                            "x": portal.get("x"),
                            "y": portal.get("y"),
                            "z": portal.get("z"),
                            "tid": portal.get("tid"),
                            "obj_id": portal.get("obj_id"),
                            "scene_id": origin_scene or portal.get("scene_id"),
                        }
                        move = pathfind_to_clue(
                            session,
                            clue,
                            hwnd=int(hwnd or 0),
                            arrive_radius=float(arrive_radius),
                            verify_timeout_s=float(verify_timeout_s),
                            poll_s=float(poll_s),
                            stop_event=stop_event,
                            log=log,
                        )
                        move["task_id"] = tid
                        move["method"] = "portal_only"
                        move["portal_kind"] = kind
                        move["dungeon_tier"] = tier
                        move["origin_scene_id"] = origin_scene or move.get("before_scene")
                        move["portal_tid"] = portal.get("tid")
                        move["portal_obj_id"] = portal.get("obj_id")
                        move["portal_x"] = portal.get("x")
                        move["portal_y"] = portal.get("y")
                        move["portal_z"] = portal.get("z")
                        move["portal_name"] = portal.get("name")
                        move["route"] = clue
                        return move
                    pr = use_task_portal_npc(
                        session,
                        portal,
                        hwnd=int(hwnd or 0),
                        portal_kind=kind,
                        dungeon_tier=tier,
                        movement_lock=bool(portal_move_guard),
                        expected_origin_scene_id=(origin_scene if strict_sync_portal else 0),
                        allow_live_rebind=not strict_sync_portal,
                        arrival_settle_s=portal_arrival_settle_s,
                        stop_event=stop_event,
                        log=log,
                    )
                    pr["task_id"] = tid
                    pr["portal_kind"] = kind
                    pr["dungeon_tier"] = tier
                    pr["origin_scene_id"] = origin_scene or pr.get("before_scene")
                    pr["portal_x"] = portal.get("x")
                    pr["portal_y"] = portal.get("y")
                    pr["portal_z"] = portal.get("z")
                    pr["portal_dist"] = portal.get("dist")
                    last_pr = pr
                    if pr.get("ok"):
                        return pr
                    # wrong leveling NPC / wrong menu → try next candidate
                    if pr.get("wrong_tier"):
                        log(
                            f"task_api portal skip wrong_tier npc="
                            f"{portal.get('name')}: {pr.get('error')}"
                        )
                        continue
                    # wrong_layer / hard errors: stop. Menu miss: try next cand,
                    # then fall through to normal clue pathfind (do not soft-OK).
                    if pr.get("wrong_layer"):
                        log(
                            f"task_api portal incomplete (no walk fallback): "
                            f"{pr.get('error') or pr.get('note')}"
                        )
                        return pr
                    log(
                        f"task_api portal cand fail → try next/walk: "
                        f"{pr.get('error') or pr.get('note')}"
                    )
                    continue
                log(
                    f"task_api portal all candidates failed tier={tier or '-'} "
                    f"last={last_pr.get('error') or last_pr.get('note')} "
                    f"→ fall through walk route"
                )
                # Do not return success; continue to clue/host_move pathfind.
            else:
                log(
                    f"task_api portal kind={kind} tier={tier or '-'} "
                    f"no nearby portal NPC r={portal_radius}"
                )
            # Portal delv tasks: never blind-walk city NPCs / free HostMove scene.
            if prefer_tid and int(prefer_tid) in PORTAL_TID_TIER:
                err = (
                    (last_pr.get("error") or last_pr.get("note"))
                    if last_pr
                    else f"附近无可用地宫传送口 tid={prefer_tid}（请到福州城练级/对应地宫传送）"
                )
                log(f"task_api portal refuse blind walk: {err}")
                return {
                    "ok": False,
                    "task_id": tid,
                    "portal_kind": kind,
                    "dungeon_tier": tier,
                    "error": err,
                    "menu": (last_pr.get("menu") if last_pr else None),
                }

    clues = find_task_clue_targets(session, row, log=log)
    meta = read_task_npc_tids(session, tid, log=log)
    try:
        delv_tid = int(meta.get("delv_tid") or 0)
    except (TypeError, ValueError):
        delv_tid = 0
    try:
        award_tid = int(meta.get("award_tid") or 0)
    except (TypeError, ValueError):
        award_tid = 0
    for_complete = bool(row.get("can_finish"))
    # Incomplete objectives should prefer DelvNPC; turn-in prefers AwardNPC.
    route_tid = award_tid if for_complete else (delv_tid or award_tid)
    route = choose_task_route_clue(
        clues,
        for_complete=for_complete,
        npc_tid=route_tid,
    )
    route_trace = _task_route_trace(
        clues,
        for_complete=for_complete,
        npc_tid=route_tid,
        selected=route,
    )
    if route is None or route.get("x") is None or route.get("z") is None:
        return {
            "ok": False,
            "task_id": tid,
            "error": "task has no verified route",
            "route_phase": "turnin" if for_complete else "objective",
            "route_candidates": route_trace,
        }
    try:
        log(
            f"task_api route pick src={route.get('source')} "
            f"clue={route.get('clue')} name={route.get('name')} "
            f"tid={route.get('tid')} dist={route.get('dist')} "
            f"xyz=({route.get('x')},{route.get('y')},{route.get('z')}) "
            f"scene={route.get('scene_id')} phase="
            f"{'turnin' if for_complete else 'objective'} candidates="
            + ",".join(
                f"{c.get('source')}#{c.get('tid') or '-'}"
                f"@s{c.get('scene_id') or '-'}"
                for c in route_trace[:6]
            )
        )
    except Exception:
        pass
    result = pathfind_to_clue(
        session,
        route,
        hwnd=int(hwnd or 0),
        arrive_radius=float(arrive_radius),
        verify_timeout_s=float(verify_timeout_s),
        poll_s=float(poll_s),
        stop_event=stop_event,
        log=log,
    )
    result["task_id"] = tid
    result["method"] = result.get("method") or "host_move"
    result["route_phase"] = "turnin" if for_complete else "objective"
    result["route"] = dict(route)
    result["route_candidates"] = route_trace
    return result


def find_nearby_task_npc(
    session,
    npc_tid: int,
    *,
    radius: float = 120.0,
    log: LogFn | None = None,
) -> dict | None:
    """Resolve a nearby NPC by template id with ptr and object id64."""
    log = log or (lambda _m: None)
    want_tid = int(npc_tid or 0)
    if not want_tid:
        return None
    from app.core.automove import read_scene_position
    from app.core.plg_interact import get_object_id64
    from app.core.plg_objects import CLASS_NPC, list_class_objects

    host_pos = None
    try:
        scene = read_scene_position(session, log=lambda _m: None)
        if scene.ok and scene.scene_pos:
            host_pos = scene.scene_pos
    except Exception:
        host_pos = None
    rows = list_class_objects(
        session,
        CLASS_NPC,
        host_pos=host_pos,
        radius=float(radius),
        limit=32,
        want_tid=want_tid,
        read_name=True,
        read_tid=True,
        max_inspect=256,
        log=log,
    )
    best = None
    best_dist = 1e18
    for npc in rows:
        if int(npc.tid or 0) != want_tid:
            continue
        oid = get_object_id64(session, int(npc.ptr))
        if oid is None or int(oid) <= 0:
            log(
                f"task_api nearby npc tid={want_tid} ptr=0x{int(npc.ptr):X} "
                f"skipped: bad id64"
            )
            continue
        try:
            dist = float(npc.dist) if npc.dist is not None else 1e9
        except (TypeError, ValueError):
            dist = 1e9
        if dist < best_dist:
            best_dist = dist
            best = {
                "clue": "交任务",
                "kind": "npc",
                "name": npc.name or f"tid{want_tid}",
                "x": npc.x,
                "y": npc.y,
                "z": npc.z,
                "dist": dist,
                "tid": want_tid,
                "scene_id": None,
                "ptr": int(npc.ptr),
                "obj_id": int(oid),
                "source": "template_tid_live",
            }
    if best is not None:
        log(
            f"task_api nearby npc tid={want_tid} name={best.get('name')} "
            f"dist={best.get('dist'):.1f} ptr=0x{int(best['ptr']):X} "
            f"obj_id=0x{int(best['obj_id']):X}"
        )
    else:
        log(f"task_api nearby npc tid={want_tid} not found r={radius}")
    return best


class TaskRunner:
    """Route incomplete tasks and automatically deliver verified finished tasks."""

    def __init__(
        self,
        *,
        pid: int,
        hwnd: int = 0,
        cfg: TaskRunnerConfig | None = None,
        on_event: Callable[[TaskStepEvent], None] | None = None,
        log: LogFn | None = None,
    ) -> None:
        from app.core.runner import RunnerLifecycle

        self.pid = int(pid)
        self.hwnd = int(hwnd or 0)
        self.cfg = cfg or TaskRunnerConfig()
        self.on_event = on_event or (lambda _e: None)
        self.log = log or (lambda _m: None)
        self._lifecycle = RunnerLifecycle(f"xajh-task-{self.pid}")
        self._stop = self._lifecycle.stop_event
        self._thread: threading.Thread | None = None
        self._session = None
        self.running = False

    def start(self) -> None:
        if self.running:
            return
        thread = self._lifecycle.start(self._loop)
        if thread is not None:
            self._thread = thread
            self.running = True

    def stop(self) -> bool:
        stopped = self._lifecycle.stop(wait=True)
        self.running = False
        return stopped

    def is_running(self) -> bool:
        return bool(self.running and self._lifecycle.is_running())

    def _emit(self, phase: str, message: str, ok: bool = True, **detail) -> None:
        event = TaskStepEvent(phase, message, ok, detail)
        try:
            self.on_event(event)
        except Exception:
            pass
        self.log(f"task [{phase}] {message}")

    def _wait_for_npc(self, npc_tid: int) -> dict | None:
        from app.core.runner import interruptible_sleep

        deadline = time.monotonic() + max(1.0, float(self.cfg.npc_wait_s))
        while not self._stop.is_set() and time.monotonic() < deadline:
            npc = find_nearby_task_npc(
                self._session,
                npc_tid,
                radius=float(self.cfg.npc_radius),
                log=self.log,
            )
            if npc is not None:
                return npc
            left = max(0.0, deadline - time.monotonic())
            self._emit("wait_npc", f"等待交任务NPC tid={npc_tid} {left:.0f}s")
            if not interruptible_sleep(float(self.cfg.npc_poll_s), self._stop):
                break
        return None

    def _deliver(self, task: TaskInfo) -> bool:
        meta = read_task_npc_tids(self._session, task.task_id, log=self.log)
        award_tid = int(meta.get("award_tid") or 0)
        if not award_tid:
            self._emit(
                "blocked",
                f"任务 {task.name or task.task_id} 缺少 AwardNPC TID",
                ok=False,
                task_id=task.task_id,
            )
            return False
        npc = find_nearby_task_npc(
            self._session,
            award_tid,
            radius=float(self.cfg.npc_radius),
            log=self.log,
        )
        if npc is None:
            clues = find_task_clue_targets(self._session, task, log=self.log)
            route = choose_task_route_clue(
                clues, for_complete=True, npc_tid=award_tid
            )
            if route is None or route.get("x") is None or route.get("z") is None:
                self._emit(
                    "blocked",
                    f"任务 {task.name or task.task_id} 无可用交付路线",
                    ok=False,
                    task_id=task.task_id,
                    award_tid=award_tid,
                )
                return False
            self._emit(
                "path",
                f"寻路到交任务NPC {route.get('name') or award_tid}",
                task_id=task.task_id,
                clue=route,
            )
            moved = pathfind_to_clue(
                self._session,
                route,
                hwnd=self.hwnd,
                arrive_radius=float(self.cfg.path_arrive_radius),
                verify_timeout_s=float(self.cfg.path_timeout_s),
                poll_s=float(self.cfg.path_poll_s),
                stop_event=self._stop,
                log=self.log,
            )
            if not moved.get("ok"):
                self._emit(
                    "path_fail",
                    f"交任务寻路失败: {moved.get('error') or moved.get('note')}",
                    ok=False,
                    task_id=task.task_id,
                )
                return False
            npc = self._wait_for_npc(award_tid)
        if npc is None:
            self._emit(
                "blocked",
                f"未发现交任务NPC tid={award_tid}",
                ok=False,
                task_id=task.task_id,
            )
            return False
        oid = int(npc.get("obj_id") or 0)
        result = complete_task(
            self._session,
            task.task_id,
            npc_id_lo=oid & 0xFFFFFFFF,
            npc_id_hi=(oid >> 32) & 0xFFFFFFFF,
            npc_ptr=int(npc.get("ptr") or 0),
            hwnd=self.hwnd,
            wait_s=float(self.cfg.complete_wait_s),
            log=self.log,
        )
        self._emit(
            "complete" if result.ok else "complete_fail",
            (
                f"已交任务 {task.name or task.task_id}"
                if result.ok
                else f"交任务失败 {task.name or task.task_id}: {result.error}"
            ),
            ok=result.ok,
            task_id=task.task_id,
            result=result.to_dict(),
        )
        return bool(result.ok)

    def _route_incomplete(self, task: TaskInfo) -> bool:
        clues = find_task_clue_targets(self._session, task, log=self.log)
        route = choose_task_route_clue(clues, for_complete=False)
        if route is None:
            self._emit(
                "blocked",
                f"任务 {task.name or task.task_id} 无可用目标坐标",
                ok=False,
                task_id=task.task_id,
            )
            return False
        result = pathfind_to_clue(
            self._session,
            route,
            hwnd=self.hwnd,
            arrive_radius=float(self.cfg.path_arrive_radius),
            verify_timeout_s=float(self.cfg.path_timeout_s),
            poll_s=float(self.cfg.path_poll_s),
            stop_event=self._stop,
            log=self.log,
        )
        self._emit(
            "route_ready" if result.get("ok") else "path_fail",
            (
                f"已导航到任务目标 {route.get('name') or route.get('clue')}"
                if result.get("ok")
                else f"任务寻路失败: {result.get('error') or result.get('note')}"
            ),
            ok=bool(result.get("ok")),
            task_id=task.task_id,
            clue=route,
            result=result,
        )
        return bool(result.get("ok"))

    def _loop(self) -> None:
        from app.core.game_attach import GameAttachSession

        try:
            self._session = GameAttachSession(log=self.log)
            self._session.attach(self.pid)
            self._emit("attach", f"attach pid={self.pid}")
            # 非马上需要：进自动任务前预热场景/坐标/背包等，供后续模块复用
            try:
                from app.core.state_dispatch import warmup_session

                warmup_session(
                    self._session,
                    log=lambda m: self.log(f"task warmup: {m}") if m else None,
                )
            except Exception as e:
                self.log(f"task warmup skip: {e}")
            while not self._stop.is_set():
                try:
                    from app.core.safe_dispatch import session_blocked

                    blocked, brsn = session_blocked(self.pid)
                except Exception:
                    blocked, brsn = False, ""
                if blocked:
                    self._emit(
                        "remote_blocked",
                        f"远程不可用已停止 pid={self.pid} ({brsn})",
                        ok=False,
                        reason=brsn,
                    )
                    break
                tasks = list_accepted_tasks(self._session, log=self.log)
                if not tasks:
                    self._emit("idle", "当前没有已接任务")
                    break
                turnins = [task for task in tasks if task.can_finish]
                if turnins:
                    from app.core.client_build import session_supports

                    if not session_supports(self._session, "task.complete.live"):
                        self._emit(
                            "blocked",
                            "存在可交任务，但 task.complete.live 尚未实机授权",
                            ok=False,
                            task_id=turnins[0].task_id,
                        )
                        break
                    if not self._deliver(turnins[0]):
                        break
                    continue
                # Objective execution (combat/dialog/gather) is not verified.
                self._route_incomplete(tasks[0])
                self._emit(
                    "paused",
                    "已发起任务导航；目标动作未验证，自动流程暂停",
                    task_id=tasks[0].task_id,
                )
                break
        except Exception as e:
            self._emit("error", str(e), ok=False)
        finally:
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
            self.running = False
            self._emit("stopped", "自动任务已停止")


def _accept_remote(pid: int, func_va: int, this_ptr: int, task_id: int) -> int:
    """
    Remote: push 0,-1,0,0,0,taskId; thiscall accept.

    @author by ak
    """
    # Runtime reverses args before pushing. This sequence reproduces the RE
    # execution order: push 0,-1,0,0,0,task_id; callee owns stack cleanup.
    return remote_call_thiscall_x86(
        int(pid),
        int(func_va),
        int(this_ptr),
        [int(task_id) & 0xFFFFFFFF, 0, 0, 0, 0xFFFFFFFF, 0],
        caller_cleanup=False,
        timeout_ms=5000,
    )


def _open_task_npc_dialog(
    session,
    *,
    npc_id: int,
    hwnd: int = 0,
    settle_s: float = 0.3,
    log: LogFn | None = None,
) -> str:
    """
    Open the live NPC service panel before turn-in/accept.

    Game complete path needs CurServNPC/dialog context; bare WanCheng often
    returns ret=1 without removing the task until the panel is open.
    """
    log = log or (lambda _m: None)
    oid = int(npc_id)
    if oid <= 0:
        return "npc_id=0"
    notes: list[str] = []
    lo = oid & 0xFFFFFFFF
    hi = (oid >> 32) & 0xFFFFFFFF
    try:
        from app.core.xajh_bridge import CMD_SET_TARGET, ensure_bridge

        bridge = ensure_bridge(
            int(session.pid),
            log=log,
            inject_if_needed=False,
            hwnd=int(hwnd or 0) or None,
        )
        if bridge is not None:
            try:
                st = bridge.call(
                    CMD_SET_TARGET,
                    id_lo=lo,
                    id_hi=hi,
                    hwnd=int(hwnd or 0) or None,
                    timeout_ms=2000,
                )
                notes.append(f"SetTarget ok={st.ok} ret={st.ret}")
            finally:
                try:
                    bridge.close()
                except Exception:
                    pass
        else:
            notes.append("SetTarget skipped: bridge not ready")
    except Exception as e:
        notes.append(f"SetTarget fail:{e}")
    try:
        from app.core.package_api import npc_say_hello

        hello = npc_say_hello(session, oid, log=log)
        notes.append(f"NPCSayHello ok={hello.ok} ret={hello.ret}")
    except Exception as e:
        notes.append(f"NPCSayHello fail:{e}")
    time.sleep(max(0.05, float(settle_s)))
    msg = "; ".join(notes)
    log(f"task_api open npc dialog id=0x{oid:X}: {msg}")
    return msg


def complete_task(
    session,
    task_id: int,
    *,
    npc_id_lo: int = 0,
    npc_id_hi: int = 0,
    npc_ptr: int = 0,
    hwnd: int = 0,
    log: LogFn | None = None,
    wait_s: float | None = None,
    open_npc_dialog: bool = True,
    fast: bool = False,
) -> TaskOpResult:
    """
    Complete/deliver task via bridge when available.

    Requires NPC identity (lo/hi and/or ptr). Success: id leaves accepted list.
    Opens NPC dialog (SetTarget + NPCSayHello) before WanCheng when possible.
    fast=True: short wait (群控副控).
    """
    log = log or (lambda _m: None)
    if wait_s is None:
        wait_s = 1.5 if fast else 4.0
    tid = int(task_id) & 0xFFFFFFFF
    before = list_accepted_task_ids(session, log=log)
    if tid not in before:
        return TaskOpResult(
            ok=False,
            action="complete",
            task_id=tid,
            before_ids=before,
            after_ids=before,
            error="task not in accepted list",
        )
    if not npc_id_lo and not npc_ptr:
        return TaskOpResult(
            ok=False,
            action="complete",
            task_id=tid,
            before_ids=before,
            after_ids=before,
            error="npc required for complete",
        )

    npc_id64 = (int(npc_id_hi) << 32) | (int(npc_id_lo) & 0xFFFFFFFF)
    if not npc_id64 and npc_id_lo:
        npc_id64 = int(npc_id_lo) & 0xFFFFFFFF

    ret = None
    err = None
    note = ""
    if open_npc_dialog and npc_id64:
        try:
            dlg_note = _open_task_npc_dialog(
                session,
                npc_id=npc_id64,
                hwnd=int(hwnd or 0),
                settle_s=0.15 if fast else 0.3,
                log=log,
            )
            note = (note + " " + dlg_note).strip()
        except Exception as e:
            log(f"task_api complete open dialog: {e}")
            note = (note + f" open_dialog_fail:{e}").strip()

    try:
        from app.core.xajh_bridge import (
            ensure_bridge,
            last_ensure_bridge_failure,
        )

        bridge = ensure_bridge(
            int(session.pid),
            log=log,
            inject_if_needed=False,
            hwnd=int(hwnd or 0) or None,
        )
        if bridge is None:
            fail_code = last_ensure_bridge_failure(int(session.pid)) or ""
            if "STALE" in fail_code or "LEGACY" in fail_code or "RESTART" in fail_code:
                raise RuntimeError(
                    "bridge 版本过旧，请完全退出游戏后重开再 Delete 注入"
                )
            raise RuntimeError("bridge 未就绪，请先 Delete 注入")
        try:
            br = bridge.task_complete(
                tid,
                npc_id_lo=int(npc_id_lo) & 0xFFFFFFFF,
                npc_id_hi=int(npc_id_hi) & 0xFFFFFFFF,
                npc_ptr=int(npc_ptr) & 0xFFFFFFFF,
                hwnd=int(hwnd or 0),
                timeout_ms=3500 if fast else 5000,
            )
            ret = br.ret
            note = ((note + " " + (br.note or "")).strip())
            log(
                f"task_api complete bridge ok={br.ok} status={br.status} "
                f"ret={br.ret} error={br.error!r} note={br.note!r}"
            )
            if not br.ok:
                err = br.error or "complete failed"
        finally:
            bridge.close()
    except Exception as e:
        err = str(e)
        log(f"task_api complete: {e}")

    after = before
    poll = 0.08 if fast else 0.12
    deadline = time.time() + max(0.3, float(wait_s))
    while time.time() < deadline:
        try:
            after = list_accepted_task_ids(session, log=lambda _m: None)
        except Exception:
            after = before
        if tid not in after:
            break
        time.sleep(poll)

    ok = tid not in after
    if ok:
        err = None
    elif err is None:
        err = "complete timeout: still in accepted list"

    return TaskOpResult(
        ok=ok,
        action="complete",
        task_id=tid,
        before_ids=before,
        after_ids=after,
        ret=ret,
        error=err,
        note=note,
    )


def complete_task_routed(
    session,
    task: TaskInfo | dict | int,
    *,
    hwnd: int = 0,
    npc_radius: float = 120.0,
    npc_wait_s: float | None = None,
    npc_poll_s: float = 0.5,
    path_arrive_radius: float = 12.0,
    path_timeout_s: float | None = None,
    path_poll_s: float = 0.5,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    fast: bool = False,
) -> TaskOpResult:
    """Resolve AwardNPC, verify arrival and complete with its live identity.

    fast=True (群控副控): no pathfind; only use nearby AwardNPC if present.
    主控 UI 仍寻路到 NPC 再交。
    """
    log = log or (lambda _m: None)
    if npc_wait_s is None:
        npc_wait_s = 3.0 if fast else 20.0
    if path_timeout_s is None:
        path_timeout_s = 90.0
    if isinstance(task, TaskInfo):
        row = task.to_dict()
    elif isinstance(task, dict):
        row = dict(task)
    else:
        row = {"task_id": int(task)}
    tid = int(row.get("task_id") or 0) & 0xFFFFFFFF
    before = list_accepted_task_ids(session, log=log)
    if tid not in before:
        return TaskOpResult(
            False, "complete", tid, before, before, error="task not in accepted list"
        )
    known_can = row.get("can_finish")
    if known_can is False and not fast:
        return TaskOpResult(
            False, "complete", tid, before, before, error="task cannot finish"
        )
    # 主控未知可交时轻量查一次；副控信主控
    if known_can is None and not fast:
        try:
            if not task_can_finish(session, tid, log=lambda _m: None):
                return TaskOpResult(
                    False, "complete", tid, before, before, error="task cannot finish"
                )
            row["can_finish"] = True
        except Exception:
            pass

    def fail(error: str, note: str = "") -> TaskOpResult:
        return TaskOpResult(
            False,
            "complete",
            tid,
            before,
            before,
            error=error,
            note=note,
        )

    meta = read_task_npc_tids(session, tid, log=log)
    award_tid = int(meta.get("award_tid") or 0)
    if not award_tid:
        return fail("task has no verified AwardNPC")
    npc = find_nearby_task_npc(
        session, award_tid, radius=float(npc_radius), log=log
    )
    if npc is not None:
        log(
            f"task_api complete nearby id={tid} award_tid={award_tid} "
            f"dist={npc.get('dist')} npc_ptr=0x{int(npc.get('ptr') or 0):X}"
        )
    if npc is None and not fast:
        clues = find_task_clue_targets(session, row, log=log)
        route = choose_task_route_clue(
            clues, for_complete=True, npc_tid=award_tid
        )
        if route is None or route.get("x") is None or route.get("z") is None:
            return fail(f"AwardNPC tid={award_tid} has no verified route")
        moved = pathfind_to_clue(
            session,
            route,
            hwnd=int(hwnd or 0),
            arrive_radius=float(path_arrive_radius),
            verify_timeout_s=float(path_timeout_s),
            poll_s=float(path_poll_s),
            stop_event=stop_event,
            log=log,
        )
        if not moved.get("ok"):
            return fail(
                str(moved.get("error") or "failed to reach AwardNPC"),
                note=f"AwardNPC tid={award_tid}; command_ok={moved.get('command_ok')}",
            )
        deadline = time.monotonic() + max(0.1, float(npc_wait_s))
        interval = max(0.05, float(npc_poll_s))
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return fail("task complete stopped while waiting for AwardNPC")
            npc = find_nearby_task_npc(
                session, award_tid, radius=float(npc_radius), log=lambda _m: None
            )
            if npc is not None:
                break
            if stop_event is not None:
                stop_event.wait(interval)
            else:
                time.sleep(interval)
    if npc is None:
        if fast:
            return fail(
                f"AwardNPC tid={award_tid} not nearby (副控不寻路)",
                note="slave must already be at turn-in NPC",
            )
        return fail(
            f"AwardNPC tid={award_tid} not found nearby",
            note="stand next to the turn-in NPC then retry",
        )
    oid = int(npc.get("obj_id") or 0)
    ptr = int(npc.get("ptr") or 0)
    if not oid or not ptr:
        return fail(f"AwardNPC tid={award_tid} missing ptr/id64")
    log(
        f"task_api complete routed id={tid} award_tid={award_tid} "
        f"npc_ptr=0x{ptr:X} obj_id=0x{oid:X} dist={npc.get('dist')} fast={fast}"
    )
    return complete_task(
        session,
        tid,
        npc_id_lo=oid & 0xFFFFFFFF,
        npc_id_hi=(oid >> 32) & 0xFFFFFFFF,
        npc_ptr=ptr,
        hwnd=int(hwnd or 0),
        log=log,
        fast=bool(fast),
        wait_s=1.2 if fast else None,
    )
