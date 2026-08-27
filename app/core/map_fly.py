# -*- coding: utf-8 -*-
"""
Map fly / 飞行旗 research helpers.

Live RE (xajh.exe preferred base 0x400000, 2026-07-21):

  UI
    - Win_TransmitFlag / CDlgTransmitFlag
    - Opened by using item class ICID_TRANSMITFLAG (飞行旗)
    - Per-slot controls: Txt_Pos%d / Btn_Trans%d / Btn_Sign%d / Btn_Delete%d (1..10)
    - Default tab Rdo_0; custom pages Rdo_%d

  Item
    - tid 22793 = 高级飞行旗 (consumable aka 飞行棋)
    - tid 22792 = 低级飞行旗

  C2S packet (NOTE_VA_TRANSMIT_FLAG_PKT = 0xCC9310)
    u16 type = 0x86
    u8  action
        0 = delete custom point
        1 = sign/record current pos into slot
        2 = fly/transmit to point
    u8  page_id   (radio userdata; default tab often 0xFF path-tolerant)
    u8  slot      (0..9, Btn_TransN -> N-1)
    u8  name_len
    char name[name_len]  optional (rename)

  Default points (live UI order, confirmed 2026-07-21)
    slot0 = 福州 / 福州城
    slot1 = 师门
    slot2 = 上次死亡地点
    HostPlayer+0x7DC was RE'd as default-id table, but live RPM values look
    non-id garbage — do NOT trust for matching; use fixed slot map instead.

  Coordinate fly (unrecorded)
    Packet 0x86 has NO x/y/z fields. Legal flow is:
      1) stand at target -> action=1 record into free custom slot
      2) later action=2 fly that slot (from ANY other place)
    Cannot: action=1 while standing at B to save coords of A (no xyz in packet).
    Direct float3 teleport via this item protocol is not present.
    Local SetPos is server-authoritative and not a legal fly path.

@author by ak
"""
from __future__ import annotations

import re
import struct
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from app.core.aui_click import (
    click_client_bg,
    get_aui_dlg_item_ptr,
    read_aui_ctrl_rect,
)
from app.core.game_attach import GameAttachSession
from app.core.package_api import (
    DEFAULT_CARRY_PACKAGE_INDEXES,
    DEFAULT_PACKAGE_INDEX,
    find_items_by_name,
    list_package_items,
    use_item_in_package,
)
from app.core.plg_exports import (
    DEFAULT_IMAGE_BASE,
    note_va_to_live,
)
from app.core.plg_ui import (
    get_game_ui_dlg,
    get_host_player_ptr,
    is_dlg_show,
    remote_alloc_bytes,
    remote_free,
)
from app.core.remote_runtime import (
    _open_process,
    _rpm,
    kernel32,
    remote_call_cdecl_x86,
    remote_call_thiscall_x86,
)
from ctypes import wintypes

LogFn = Callable[[str], None]


def _pid_blocked(session: GameAttachSession) -> tuple[bool, str]:
    """True when SafeDispatch/remote gate blocks this game pid. @author by ak"""
    try:
        from app.core.safe_dispatch import session_blocked

        return session_blocked(session)
    except Exception:
        return False, ""


def _prefetch_non_urgent(session: GameAttachSession, log: LogFn | None = None) -> None:
    """
    非马上需要的状态预热：场景/坐标/背包短缓存，供打开飞行旗、扫道具等复用。

    不 force fresh；真正起飞后校验走 _snapshot_scene_pos(fresh=True)。
    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        from app.core.state_dispatch import StateKind, prefetch_states

        prefetch_states(
            session,
            (StateKind.SCENE, StateKind.POS, StateKind.BAG),
            log=lambda m: log(f"map_fly prefetch: {m}") if m else None,
        )
    except Exception as e:
        log(f"map_fly: non-urgent prefetch skip: {e}")


def _note_fly_state_change(
    session: GameAttachSession,
    after: dict | None,
    *,
    log: LogFn | None = None,
) -> None:
    """飞后：失效旧场景/坐标，并把新快照推到 scene hub。@author by ak"""
    log = log or (lambda _m: None)
    try:
        from app.core.state_dispatch import StateKind, invalidate_states

        invalidate_states(session, StateKind.SCENE, StateKind.POS)
    except Exception as e:
        log(f"map_fly: invalidate scene/pos: {e}")
    if not isinstance(after, dict) or not after.get("ok"):
        return
    try:
        from app.core.live_scene_hub import publish_live_scene

        pos = after.get("pos")
        pos_t = None
        if isinstance(pos, (list, tuple)) and len(pos) >= 3:
            pos_t = (float(pos[0]), float(pos[1]), float(pos[2]))
        publish_live_scene(
            int(getattr(session, "pid", 0) or 0),
            scene_id=after.get("scene_id"),
            pos=pos_t,
            source="map_fly",
        )
    except Exception as e:
        log(f"map_fly: publish live scene: {e}")


# --- constants (preferred base 0x400000) ---
NOTE_VA_TRANSMIT_FLAG_PKT = 0x00CC9310  # cdecl action,page,slot,name*
NOTE_VA_TRANSMIT_FLAG_PKT_THIN = 0x00A4D1C0  # stdcall wrapper -> 0xCC9310

TRANSMIT_FLAG_DLG = "Win_TransmitFlag"
TRANSMIT_PKT_TYPE = 0x86

# Host default-point id table (10 x u32)
HOST_DEFAULT_FLY_IDS_OFF = 0x7DC
HOST_DEFAULT_FLY_SLOTS = 10
FIXED_CUSTOM_FLY_PAGE_COUNT = 6
FIXED_CUSTOM_FLY_PAGES = tuple(range(FIXED_CUSTOM_FLY_PAGE_COUNT))

# Bag items (item_names + design table)
FLY_ITEM_TIDS = (22793, 22792)  # 高级 / 低级
FLY_ITEM_NAME_KEYS = ("飞行旗", "飞行棋", "传送旗")

# Packet actions (all go through 0xCC9310 / type 0x86; no xyz fields)
ACTION_DELETE = 0   # Btn_Delete
ACTION_SIGN = 1     # Btn_Sign — server records CURRENT host pos into page/slot
ACTION_FLY = 2      # Btn_Trans — fly to already-recorded / default slot
ACTION_RENAME = 4   # rename-ish with name string (still no xyz)

# Fixed default-page slots (live confirmed 2026-07-21).
# UI order: 1st 福州, 2nd 师门, 3rd 上次死亡地点.
PRESET_POINTS = {
    "fuzhou": {
        "label": "福州城",
        "slot": 0,
        "needles": ("福州城", "福州"),
    },
    "shimen": {
        "label": "师门",
        "slot": 1,
        "needles": ("师门",),
    },
    "death": {
        "label": "上次死亡地点",
        "slot": 2,
        "needles": ("死亡", "上次死亡"),
    },
}

# Default page packet page_id candidates (Rdo userdata; 0 first).
# Live RE: when selected radio index is 0, Btn_Trans uses page=0xFF
# (sub 1 underflows, skip ebc070). Custom pages use 1..N from Rdo userdata.
DEFAULT_PAGE_CANDIDATES = (0xFF, 0, 1)

# AUI still/label GetText-ish vtable slot observed near SetText(+0x48) in fill path.
AUI_VT_GET_TEXT = 0x44
AUI_VT_SET_ENABLE = 0x4C
AUI_VT_SHOW = 0x38

_COLOR_RE = re.compile(r"\^[0-9a-fA-F]{0,6}")


@dataclass
class FlyItemHit:
    package: int
    slot: int
    tid: int
    count: int
    name: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TransmitPointRow:
    """One slot on current / default page."""

    slot: int  # 0-based
    page_id: int | None = None
    name: str = ""
    raw_text: str = ""
    default_tid: int = 0
    btn_trans: str = ""
    enabled_guess: bool | None = None
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MapFlyResult:
    ok: bool
    action: str = ""
    message: str = ""
    error: str | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _log(log: LogFn | None, msg: str) -> None:
    if log:
        log(msg)


def _clean_text(s: str) -> str:
    t = _COLOR_RE.sub("", s or "")
    t = "".join(ch for ch in t if ch == " " or ch.isprintable())
    return t.strip()


def _note_va(session: GameAttachSession, note_va: int) -> int:
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        raise RuntimeError("session has no module_base; attach first")
    return note_va_to_live(base, note_va)


_RPM_RETRY = 3


def _read_u32(session: GameAttachSession, addr: int) -> int:
    pid = int(session.pid)
    last_raw = b""
    for _ in range(_RPM_RETRY):
        h = _open_process(pid)
        try:
            raw = _rpm(h, int(addr) & 0xFFFFFFFF, 4)
            if raw and len(raw) >= 4:
                return struct.unpack("<I", raw)[0]
            last_raw = raw or b""
        except Exception:
            pass
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h))
        # transient RPM failures (e.g. ERROR_PARTIAL_COPY 299 on heap pages)
        # are retried before giving up.
    if last_raw:
        return 0
    return 0


def _read_bytes(session: GameAttachSession, addr: int, size: int) -> bytes:
    pid = int(session.pid)
    last = b""
    for _ in range(_RPM_RETRY):
        h = _open_process(pid)
        try:
            raw = _rpm(h, int(addr) & 0xFFFFFFFF, max(0, int(size)))
            if raw:
                return raw
            last = raw or b""
        except Exception:
            pass
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h))
    return last


def research_summary() -> dict:
    """Static research facts for UI. @author by ak"""
    return {
        "dlg": TRANSMIT_FLAG_DLG,
        "item_tids": list(FLY_ITEM_TIDS),
        "packet": {
            "type": TRANSMIT_PKT_TYPE,
            "note_va": hex(NOTE_VA_TRANSMIT_FLAG_PKT),
            "layout": "u16 type=0x86 | u8 action | u8 page | u8 slot | u8 name_len | name?",
            "actions": {
                "0": "delete custom point",
                "1": "sign/record current position",
                "2": "fly to point",
            },
        },
        "default_points": {
            "host_off": hex(HOST_DEFAULT_FLY_IDS_OFF),
            "host_off_note": "live RPM often garbage; trust fixed UI order",
            "slots": HOST_DEFAULT_FLY_SLOTS,
            "fixed_order": ["福州城@0", "师门@1", "上次死亡地点@2"],
        },
        "coord_fly": {
            "supported_by_item_protocol": False,
            "reason": "0x86 has no xyz; only page/slot (+optional name)",
            "legal_path": "action=1 record at place, then action=2 fly slot",
            "illegal_local_setpos": "server NotifyHostPos authoritative; risk 神罚/拉回",
        },
        "record_then_fly_elsewhere": {
            "record_non_current_pos": False,
            "record_reason": "action=1 packet has no x/y/z; server binds CURRENT auth pos",
            "fly_recorded_from_elsewhere": True,
            "fly_reason": "action=2 only sends page+slot; destination is server-side saved point",
            "legal_flow": [
                "站在目标点 A",
                "自定义页 action=1 定位到空槽",
                "走到/飞到任意地点 B",
                "action=2 飞该槽 → 回到 A",
            ],
        },
        "default_page": {
            "packet_page_for_default_tab": 0xFF,
            "reason": "Btn_Trans when Rdo index==0: page = userdata-1 underflows to 0xFF",
            "candidates": [0xFF, 0, 1],
        },
        "death_coord": {
            "client_cache": "fly_mgr+0x15C scene/u16 +0x15E u8 +0x15F/163/167 f32",
            "filled_by": "S2C type 0x127 @ 0x51BB50",
            "c2s_report_death_xyz": False,
            "forge_cache_then_fly_slot2": False,
            "legal": "真实死亡后服端更新 → 飞默认 slot2",
        },
        "presets": {
            k: {"label": v["label"], "slot": v["slot"]} for k, v in PRESET_POINTS.items()
        },
    }


def find_fly_items(
    session: GameAttachSession,
    *,
    package_index: int | None = None,
    log: LogFn | None = None,
) -> list[FlyItemHit]:
    """Find 飞行旗/飞行棋 stacks in bag.

    package_index=None 时遍历随身所有包（主包 PACK + 扩展 PACK1/PACK2），
    否则只查指定包。帝丶荒等角色把旗放扩展包时，只查主包会找不到（2026-08-16）。
    @author by ak
    """
    log = log or (lambda _m: None)
    out: list[FlyItemHit] = []
    if package_index is None:
        candidates = DEFAULT_CARRY_PACKAGE_INDEXES
    else:
        candidates = (int(package_index),)
    for pack in candidates:
        try:
            items = None
            try:
                from app.core.state_dispatch import StateKind, get_state

                warm = get_state(session, StateKind.BAG, fresh=False, log=lambda _m: None)
                if warm:
                    items = [
                        it
                        for it in warm
                        if int(getattr(it, "package", pack) or pack) == pack
                    ]
            except Exception:
                items = None
            if items is None:
                items = list_package_items(session, int(pack), log=lambda _m: None)
        except Exception as e:
            log(f"map_fly: list package failed: {e}")
            continue
        for it in items:
            tid = int(getattr(it, "tid", 0) or 0)
            name = str(getattr(it, "name", "") or "")
            hit = tid in FLY_ITEM_TIDS or any(k in name for k in FLY_ITEM_NAME_KEYS)
            if not hit:
                continue
            out.append(
                FlyItemHit(
                    package=int(getattr(it, "package", pack) or pack),
                    slot=int(getattr(it, "slot", 0) or 0),
                    tid=tid,
                    count=int(getattr(it, "count", 0) or 0),
                    name=name or f"tid:{tid}",
                )
            )
        if out:
            break
    # prefer higher tid (高级) first
    out.sort(key=lambda x: (-x.tid, -x.count, x.slot))
    log(f"map_fly: fly items={len(out)}")
    return out


def is_transmit_flag_open(
    session: GameAttachSession, *, log: LogFn | None = None
) -> tuple[bool, int]:
    """Return (shown, dlg_ptr). @author by ak"""
    log = log or (lambda _m: None)
    dlg = get_game_ui_dlg(session, TRANSMIT_FLAG_DLG, log=log)
    if not dlg:
        return False, 0
    try:
        shown = bool(is_dlg_show(session, dlg, log=lambda _m: None))
    except Exception:
        shown = False
    return shown, int(dlg) & 0xFFFFFFFF


def open_transmit_flag(
    session: GameAttachSession,
    *,
    package_index: int | None = None,
    prefer_use_item: bool = True,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Open Win_TransmitFlag by using a bag 飞行旗.

    package_index=None 时遍历随身所有包（主包 + 扩展包），否则只查指定包。
    If already open, returns ok with method=already_open.
    @author by ak
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return MapFlyResult(
            ok=False,
            action="open",
            message=f"远程不可用: {brsn}",
            error=brsn or "remote_blocked",
        )
    # 打开界面/查旗：非马上需要 → 预热即可，不强制 live
    _prefetch_non_urgent(session, log=log)
    shown, dlg = is_transmit_flag_open(session, log=log)
    if shown and dlg:
        return MapFlyResult(
            ok=True,
            action="open",
            message=f"已打开 {TRANSMIT_FLAG_DLG} dlg=0x{dlg:X}",
            detail={"dlg": dlg, "method": "already_open"},
        )
    if not prefer_use_item:
        return MapFlyResult(
            ok=False,
            action="open",
            message="界面未打开且未允许使用道具",
            error="not_open",
        )
    items = find_fly_items(session, package_index=package_index, log=log)
    if not items:
        return MapFlyResult(
            ok=False,
            action="open",
            message="背包未找到飞行旗/飞行棋 (22793/22792)",
            error="no_item",
        )
    it = items[0]
    r = use_item_in_package(session, it.package, it.slot, log=log)
    # use_item 已失效 bag 缓存；再清一次 state 分发侧，避免扫旗拿到旧栈
    try:
        from app.core.state_dispatch import StateKind, invalidate_states

        invalidate_states(session, StateKind.BAG)
    except Exception:
        pass
    time.sleep(0.35)
    shown2, dlg2 = is_transmit_flag_open(session, log=log)
    ok = bool(shown2 and dlg2)
    return MapFlyResult(
        ok=ok,
        action="open",
        message=(
            f"使用 {it.name} tid={it.tid} 槽{it.slot} -> "
            f"{'打开成功' if ok else '未检测到界面'} dlg=0x{dlg2:X}"
        ),
        error=None if ok else (r.error or "dlg_not_shown"),
        detail={
            "item": it.to_dict(),
            "use": r.to_dict() if hasattr(r, "to_dict") else asdict(r),
            "dlg": dlg2,
            "method": "use_item",
        },
    )


def send_transmit_packet(
    session: GameAttachSession,
    action: int,
    page_id: int,
    slot: int,
    name: str | None = None,
    *,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Send c2s transmit-flag packet type 0x86 via native 0xCC9310.

    @author by ak
    """
    log = log or (lambda _m: None)
    act = int(action) & 0xFF
    page = int(page_id) & 0xFF
    sl = int(slot) & 0xFF
    if sl >= HOST_DEFAULT_FLY_SLOTS and act == ACTION_FLY:
        return MapFlyResult(
            ok=False,
            action="packet",
            message=f"slot={sl} out of range 0..{HOST_DEFAULT_FLY_SLOTS-1}",
            error="bad_slot",
        )
    pid = int(session.pid)
    h = 0
    remote = 0
    name_ptr = 0
    try:
        va = _note_va(session, NOTE_VA_TRANSMIT_FLAG_PKT)
        if name:
            raw = name.encode("gbk", "ignore") + b"\x00"
            h, remote, _sz = remote_alloc_bytes(pid, raw)
            name_ptr = int(remote) & 0xFFFFFFFF
        ret = remote_call_cdecl_x86(
            pid,
            va,
            [act, page, sl, int(name_ptr) & 0xFFFFFFFF],
            timeout_ms=4000,
        )
        msg = (
            f"pkt 0x86 action={act} page={page}/0x{page:02X} slot={sl} "
            f"name={name!r} va=0x{va:X} ret={ret}"
        )
        log(f"map_fly: {msg}")
        return MapFlyResult(
            ok=True,
            action="packet",
            message=msg,
            detail={
                "type": TRANSMIT_PKT_TYPE,
                "action": act,
                "page": page,
                "slot": sl,
                "name": name,
                "va": va,
                "ret": int(ret or 0),
            },
        )
    except TimeoutError as e:
        # The remote call contract leaves a still-running thread alive after its
        # grace wait. Keep its optional name buffer alive as well.
        if remote:
            log(f"map_fly: packet timeout; retain name buffer=0x{remote:X}")
            remote = 0
        return MapFlyResult(
            ok=False, action="packet", message=str(e), error=str(e)
        )
    except Exception as e:
        log(f"map_fly: send packet failed: {e}")
        return MapFlyResult(
            ok=False, action="packet", message=str(e), error=str(e)
        )
    finally:
        if h and remote:
            try:
                remote_free(h, remote)
            except Exception:
                pass


def read_default_point_ids(
    session: GameAttachSession, *, log: LogFn | None = None
) -> list[int]:
    """
    Read HostPlayer+0x7DC default fly template id table (10 slots).

    @author by ak
    """
    log = log or (lambda _m: None)
    host = get_host_player_ptr(session, log=log)
    if not host:
        log("map_fly: host null")
        return []
    base = (int(host) + HOST_DEFAULT_FLY_IDS_OFF) & 0xFFFFFFFF
    raw = _read_bytes(session, base, HOST_DEFAULT_FLY_SLOTS * 4)
    ids: list[int] = []
    for i in range(HOST_DEFAULT_FLY_SLOTS):
        if len(raw) >= (i + 1) * 4:
            ids.append(struct.unpack_from("<I", raw, i * 4)[0])
        else:
            ids.append(0)
    # Live 2026-07-21: many values look like floats/flags, not tid — keep raw for lab.
    suspicious = [i for i, v in enumerate(ids) if v and (v > 0x100000 or v < 0)]
    log(
        f"map_fly: default ids host=0x{host:X}+0x{HOST_DEFAULT_FLY_IDS_OFF:X} -> {ids}"
        + (f" suspicious_slots={suspicious} (not used for matching)" if suspicious else "")
    )
    return ids


def _aui_get_text(session: GameAttachSession, ctrl: int, *, log: LogFn | None = None) -> str:
    """
    Best-effort AUI control text via vtable GetText (+0x44) -> wchar*/ACString.

    @author by ak
    """
    log = log or (lambda _m: None)
    ctrl = int(ctrl) & 0xFFFFFFFF
    if not ctrl:
        return ""
    try:
        vt = _read_u32(session, ctrl)
        if not vt:
            return ""
        fn = _read_u32(session, (vt + AUI_VT_GET_TEXT) & 0xFFFFFFFF)
        if not fn:
            return ""
        pid = int(session.pid)
        ret = remote_call_thiscall_x86(
            pid, int(fn) & 0xFFFFFFFF, ctrl, [], timeout_ms=2500
        )
        p = int(ret) & 0xFFFFFFFF
        if not p:
            return ""
        # try direct wchar
        wb = _read_bytes(session, p, 128)
        if wb:
            # ACString often: ptr at +0 or inline
            # If looks like pointer, follow once
            maybe = struct.unpack_from("<I", wb, 0)[0] if len(wb) >= 4 else 0
            candidates = [p]
            if 0x10000 < maybe < FLY_PTR_MAX:
                candidates.append(maybe)
            for addr in candidates:
                raw = _read_bytes(session, addr, 160)
                if not raw:
                    continue
                # utf-16le
                try:
                    end = raw.find(b"\x00\x00")
                    chunk = raw[: end + 1] if end >= 0 else raw
                    if len(chunk) % 2 == 1:
                        chunk = chunk[:-1]
                    s = chunk.decode("utf-16le", "ignore").strip("\x00").strip()
                    if s and any("\u4e00" <= c <= "\u9fff" or c.isalnum() for c in s):
                        return _clean_text(s)
                except Exception:
                    pass
                # gbk/c-string
                try:
                    end = raw.find(b"\x00")
                    chunk = raw[:end] if end >= 0 else raw
                    s = chunk.decode("gbk", "ignore").strip()
                    if s and any("\u4e00" <= c <= "\u9fff" or c.isalnum() for c in s):
                        return _clean_text(s)
                except Exception:
                    pass
    except Exception as e:
        log(f"map_fly: get_text 0x{ctrl:X} err: {e}")
    return ""


def list_ui_transmit_points(
    session: GameAttachSession,
    *,
    open_if_needed: bool = True,
    apply_fixed_labels: bool = True,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Read Txt_Pos1..10 (+ default id table) from open Win_TransmitFlag.

    apply_fixed_labels: only for default page; custom pages must pass False
    so empty slots stay empty (not filled with 福州/师门).

    @author by ak
    """
    log = log or (lambda _m: None)
    if open_if_needed:
        op = open_transmit_flag(session, log=log)
        if not op.ok and op.detail.get("method") != "already_open":
            # still try if dialog somehow open
            shown, _ = is_transmit_flag_open(session, log=log)
            if not shown:
                return op
    shown, dlg = is_transmit_flag_open(session, log=log)
    if not shown or not dlg:
        return MapFlyResult(
            ok=False,
            action="list_ui",
            message="Win_TransmitFlag 未打开",
            error="dlg_closed",
        )
    default_ids = read_default_point_ids(session, log=log)
    fixed_labels = (
        {int(v["slot"]): str(v["label"]) for v in PRESET_POINTS.values()}
        if apply_fixed_labels
        else {}
    )
    rows: list[TransmitPointRow] = []
    for i in range(1, HOST_DEFAULT_FLY_SLOTS + 1):
        slot0 = i - 1
        txt_name = f"Txt_Pos{i}"
        btn_name = f"Btn_Trans{i}"
        txt_ptr = get_aui_dlg_item_ptr(session, dlg, txt_name, log=lambda _m: None)
        btn_ptr = get_aui_dlg_item_ptr(session, dlg, btn_name, log=lambda _m: None)
        raw = _aui_get_text(session, txt_ptr, log=lambda _m: None) if txt_ptr else ""
        name = raw
        # strip "name (x, z)" tail if present
        m = re.match(r"^(.*?)(?:\s*\([-\d.\s,]+\))?\s*$", name)
        if m:
            name = m.group(1).strip() or name
        if not name and slot0 in fixed_labels:
            name = fixed_labels[slot0]
            if not raw:
                raw = f"[fixed]{name}"
        tid = default_ids[slot0] if slot0 < len(default_ids) else 0
        # host+0x7DC live values are often non-id garbage; zero out obvious junk
        if tid and (tid > 0x100000 or tid < 0):
            tid = 0
        rows.append(
            TransmitPointRow(
                slot=slot0,
                name=name,
                raw_text=raw,
                default_tid=int(tid or 0),
                btn_trans=btn_name,
                source="fixed_order+ui" if slot0 in fixed_labels else "ui",
            )
        )
    nonempty = [r for r in rows if r.name or r.default_tid]
    msg = f"读取 {len(rows)} 槽，非空约 {len(nonempty)} 个"
    log(f"map_fly: {msg}")
    return MapFlyResult(
        ok=True,
        action="list_ui",
        message=msg,
        detail={
            "dlg": dlg,
            "default_ids": default_ids,
            "rows": [r.to_dict() for r in rows],
        },
    )



def select_transmit_radio(
    session: GameAttachSession,
    rdo_name: str,
    *,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Click a page radio on Win_TransmitFlag (Rdo_0 / Rdo_1 ...).

    @author by ak
    """
    log = log or (lambda _m: None)
    shown, dlg = is_transmit_flag_open(session, log=log)
    if not shown or not dlg:
        return MapFlyResult(
            ok=False, action="select_page", message="界面未开", error="dlg_closed"
        )
    name = str(rdo_name or "").strip()
    if not name:
        return MapFlyResult(
            ok=False, action="select_page", message="空 rdo", error="bad_rdo"
        )
    rp = get_aui_dlg_item_ptr(session, dlg, name, log=log)
    if not rp:
        return MapFlyResult(
            ok=False, action="select_page", message=f"无控件 {name}", error="no_ctrl"
        )
    br = read_aui_ctrl_rect(session, rp, name=name, log=log)
    if not br or not getattr(br, "ok", False):
        return MapFlyResult(
            ok=False, action="select_page", message=f"{name} 无矩形", error="no_rect"
        )
    cx, cy = br.center()
    click_client_bg(session, cx, cy, log=log)
    time.sleep(0.18)
    return MapFlyResult(
        ok=True,
        action="select_page",
        message=f"选页 {name} @({cx},{cy})",
        detail={"rdo": name, "xy": (cx, cy), "dlg": dlg},
    )


def packet_page_for_radio_index(radio_index: int) -> int:
    """
    Map Win_TransmitFlag radio index -> c2s page byte.

    Rdo_0 (default) uses 0xFF; fixed custom Rdo_1..6 map to page 0..5.
    @author by ak
    """
    idx = int(radio_index)
    if idx <= 0:
        return 0xFF
    return (idx - 1) & 0xFF


def ui_page_to_packet_page(ui_page: int, *, is_default: bool = False) -> int:
    """
    Fixed UI custom-page index -> c2s packet page.

    UI 0页 = map key 0 = 游戏第一自定义页 → packet 0
    默认 tab → packet 0xFF（唯一默认页）
    固定六个自定义页的 UI/map/packet 编号均为 0..5。

    @author by ak
    """
    if is_default:
        return 0xFF
    n = int(ui_page) & 0xFF
    if n == 0xFF:
        return 0xFF
    return n


def packet_page_to_ui_page(packet_page: int) -> int | None:
    """Map fixed custom packet 0..5 back to the same UI page. @author by ak"""
    p = int(packet_page) & 0xFF
    if p == 0xFF:
        return None
    return p if p in FIXED_CUSTOM_FLY_PAGES else None



def packet_page_for_map_key(
    session: GameAttachSession,
    map_key: int,
    page_obj: int = 0,
    *,
    log: LogFn | None = None,
) -> int:
    """
    Decide c2s packet page for a map key / page_obj.

    Fixed mapping (aligned with game tabs 默认 + 0..5):
      - default system tab uses packet 0xFF (NOT map key)
      - custom map key K uses packet K (including K=0)

    The hash-map key is authoritative. page_obj[+0] is diagnostic only; using
    it to rewrite the key previously caused adjacent pages to be mixed.

    @author by ak
    """
    log = log or (lambda _m: None)
    mk = int(map_key) & 0xFF
    if mk in FIXED_CUSTOM_FLY_PAGES:
        return mk
    return mk if mk <= 32 else 0


def resolve_fly_packet_pages(
    session: GameAttachSession,
    page_id: int,
    *,
    is_default: bool = False,
    page_id_is_packet: bool = False,
    log: LogFn | None = None,
) -> list[int]:
    """
    Resolve c2s 0x86 page candidates.

    By default `page_id` is UI logical page:
      - default / 0xFF → [0xFF]
      - fixed custom UI 0..5 → the same packet page 0..5

    If page_id_is_packet=True, treat page_id as already-packet (0..5 / 0xFF).

    Protocol truth:
      - default tab packet = 0xFF
      - custom packet = fixed page key 0..5

    @author by ak
    """
    log = log or (lambda _m: None)
    raw = int(page_id) & 0xFF
    if is_default or raw == 0xFF:
        return [0xFF]

    packet = raw if page_id_is_packet else ui_page_to_packet_page(raw)
    if packet not in FIXED_CUSTOM_FLY_PAGES:
        log(
            f"map_fly: resolve packet page invalid raw={raw} "
            f"packet_mode={page_id_is_packet}"
        )
        return []
    log(
        f"map_fly: resolve packet pages raw={raw} "
        f"packet_mode={page_id_is_packet} -> [{packet}]"
    )
    return [packet]


def safe_radio_index_for_page(
    page_id: int,
    *,
    is_default: bool = False,
    radio_index: int | None = None,
    page_id_is_packet: bool = True,
) -> int | None:
    """
    Radio index for UI switch.

    Rdo_0 = 默认 (packet 0xFF)
    Rdo_{k+1} = custom map key/packet k  (霸刀 k=0 → Rdo_1)

    Never return 0 for custom.
    @author by ak
    """
    if is_default or (int(page_id) & 0xFF) == 0xFF:
        return 0
    if radio_index is not None:
        ri = int(radio_index)
        if ri > 0:
            return ri
    pid = int(page_id) & 0xFF
    if not page_id_is_packet:
        pid = ui_page_to_packet_page(pid, is_default=False)
    # packet/custom key k → Rdo_(k+1)
    if 0 <= pid <= 32:
        return (pid + 1) & 0xFF or 1
    return None



def _is_sane_display_name(s: str, *, allow_latin: bool = False) -> bool:
    """Filter mojibake / pointer junk from memory strings. @author by ak"""
    t = (s or "").strip()
    if not t or len(t) > 40:
        return False
    bad_tokens = (
        "String",
        "current",
        "Back",
        "nullptr",
        "std::",
        "class ",
        "icon",
        "inline",
        "\ufffd",
    )
    low = t.lower()
    if any(b.lower() in low for b in bad_tokens):
        return False
    if re.search(r"[\u4e00-\u9fff][A-Za-z]|[A-Za-z][\u4e00-\u9fff]", t):
        return False
    cjk = sum(1 for c in t if "\u4e00" <= c <= "\u9fff")
    latin = sum(1 for c in t if c.isascii() and c.isalpha())
    printable = sum(1 for c in t if c.isprintable())
    if printable < len(t) * 0.9:
        return False
    # Fly-point names are almost always Chinese; pure Latin is usually decode trash.
    if cjk == 0:
        if not allow_latin:
            return False
        if latin < 2:
            return False
        if sum(1 for c in t if ord(c) > 127) > len(t) * 0.3:
            return False
        return True
    ok_ch = sum(
        1
        for c in t
        if ("\u4e00" <= c <= "\u9fff")
        or c.isdigit()
        or c in "()（）[]_·,.-+/ "
    )
    if ok_ch < len(t) * 0.7:
        return False
    return True


def _extract_pure_fly_name(raw: str) -> str:
    """
    Keep leading clean Chinese name; drop coords & trailing junk.

    Does NOT accept pure-Latin mojibake (e.g. Inlineyight).
    @author by ak
    """
    s0 = _clean_text(raw or "")
    if not s0:
        return ""
    if re.search(r"(?i)icon|string|current|null|inline", s0):
        return ""
    # strip trailing coordinate "(x,z)" / "（x,z）"
    s = re.sub(r"\s*[\(（][^）\)]*[\)）]\s*$", "", s0).strip()
    s = re.sub(r"\s*[\(（][^）\)]*$", "", s).strip()
    base = s or s0
    # Prefer continuous CJK run (2..16), allow mid digits/·
    m = re.match(
        r"^([\u4e00-\u9fff]{2,16}(?:[0-9\u4e00-\u9fff·･・]{0,8})?)",
        base,
    )
    if m:
        name = m.group(1).strip("·･・ ")
        # cut if trailing non-name leftovers look like salad
        rest = base[len(m.group(1)) :].strip()
        rest2 = re.sub(r"[\(（].*$", "", rest).strip()
        if rest2 and re.search(r"[A-Za-z]", rest2):
            # latin tail => likely bad decode; still keep leading CJK if solid
            if len(name) >= 2 and _is_sane_display_name(name):
                return name
            return ""
        return name if _is_sane_display_name(name) else ""
    # secondary: any longest CJK run inside string
    runs = re.findall(r"[\u4e00-\u9fff]{2,16}", base)
    if runs:
        runs.sort(key=len, reverse=True)
        for r in runs:
            if _is_sane_display_name(r):
                return r
    return ""


def _format_fly_slot_label(
    *,
    raw_name: str,
    scene_id: int,
    x: float,
    y: float,
    z: float,
    slot: int,
) -> tuple[str, bool]:
    """
    Build UI label for one custom slot. Returns (label, empty).

    @author by ak
    """
    pure = _extract_pure_fly_name(raw_name)
    has_pos = abs(float(x)) + abs(float(z)) > 0.05
    coord = f"({float(x):.0f},{float(z):.0f})" if has_pos else ""
    scene_cn = ""
    if int(scene_id):
        try:
            from app.core.map_names import resolve_scene_id

            _mid, cn = resolve_scene_id(int(scene_id))
            scene_cn = (cn or "").strip()
        except Exception:
            scene_cn = ""

    empty = (not pure) and (not int(scene_id)) and (not has_pos)
    if empty:
        return f"槽{int(slot) + 1} (空)", True

    if pure:
        if coord and coord not in pure:
            return f"{pure}{coord}", False
        return pure, False

    if scene_cn:
        return f"{scene_cn}{coord}" if coord else scene_cn, False

    if int(scene_id):
        base = f"槽{int(slot) + 1} 场景{int(scene_id)}"
        return f"{base}{coord}" if coord else base, False

    return (f"槽{int(slot) + 1}{coord}" if coord else f"槽{int(slot) + 1}"), False


# High-frequency chars in xajh place / fly names (soft prior against mojibake).
_FLY_NAME_COMMON = set(
    "东南西北中上下左右大小新旧金银山水风云龙虎凤鹤仙佛道圣皇王帝将相侯公侯伯"
    "城门关镇乡村府州郡县寨堡岛湖江河海泉溪谷峰岭岩石林树花草竹梅兰菊松柏"
    "宫殿观寺庙庵祠社坛台楼阁轩斋堂院馆店铺庄园坞渡桥街巷坊市集港湾"
    "洛阳明都汴京杭州苏州扬州成都长安开封襄阳太原大同幽州幽燕巴蜀"
    "福州建邺金陵钱塘襄樊汉中西川剑阁峨眉青城少室嵩山华山泰山衡山"
    "沙滩码头客栈酒肆茶馆镖局钱庄当铺医馆书院武馆教坊乐坊"
    "任务日常活动副本帮会帮派帮派战修罗决战英雄大会天下会挂机"
    "一二三四五六七八九十百千万半正副总盟主帮主堂主舵主弟子"
)

def _score_fly_name(s: str) -> int:
    """Higher is better display name. @author by ak"""
    t = (s or "").strip()
    if not t:
        return -10**9
    cjk = [c for c in t if "一" <= c <= "鿿"]
    if len(cjk) < 2:
        return -1000
    # common BMP ideographs
    in_bmp = sum(1 for c in cjk if "一" <= c <= "龥")
    rare = len(cjk) - in_bmp
    latin = sum(1 for c in t if c.isascii() and c.isalpha())
    freq = sum(1 for c in cjk if c in _FLY_NAME_COMMON)
    # mojibake often has low freq hits despite being CJK
    score = in_bmp * 6 + freq * 18 - rare * 12 - latin * 20
    score += max(0, 14 - abs(len(cjk) - 4))
    # repeated weird bigrams (e.g. 廘屿廘屿) → penalty
    if len(cjk) >= 4:
        half = len(cjk) // 2
        if cjk[:half] == cjk[half : half * 2]:
            score -= 40
    for ch in t:
        if ch in "城门镇村府宫殿观寺桥江河湖海山岛关寨堡店楼台院堂馆渡口沙":
            score += 4
    if re.search(r"(?i)inline|string|null|icon|slot", t):
        score -= 50
    # reject obvious garbage if almost no common hits
    if freq == 0:
        score -= 40 if len(cjk) >= 3 else 25
    elif freq * 2 < len(cjk):
        score -= 15
    return score


def _decode_name_blob(raw: bytes) -> list[str]:
    """Decode raw bytes into Chinese name candidates. @author by ak"""
    out: list[str] = []
    if not raw:
        return out

    def _add(s: str) -> None:
        s = _clean_text(s or "")
        if not s or len(s) > 32:
            return
        pure = _extract_pure_fly_name(s)
        if pure and pure not in out:
            out.append(pure)
        cjk_n = sum(1 for c in s if "一" <= c <= "鿿")
        if cjk_n >= 2 and s not in out:
            out.append(s)

    # Detect utf-16-ish buffer (every other byte often in CJK high page)
    def _utf16_hint(b: bytes) -> bool:
        if len(b) < 4:
            return False
        n = min(len(b), 24) & ~1
        if n < 4:
            return False
        hi = 0
        for i in range(1, n, 2):
            if 0x4E <= b[i] <= 0x9F:
                hi += 1
        return hi >= max(2, n // 4)

    # Prefer encoding order based on buffer shape
    orders: list[str]
    if _utf16_hint(raw):
        orders = ["utf-16le", "gbk", "gb18030", "utf-8"]
    else:
        orders = ["gbk", "gb18030", "utf-8", "utf-16le"]

    # null-terminated per encoding
    for enc in orders:
        try:
            if enc == "utf-16le":
                end = raw.find(bytes([0, 0]))
                chunk = raw[:end] if end >= 0 else raw[:40]
                if len(chunk) % 2 == 1:
                    chunk = chunk[:-1]
            else:
                end = raw.find(bytes([0]))
                chunk = raw[:end] if end >= 0 else raw[:32]
                # utf16 misread as gbk often has many zeros
                if chunk.count(0) > max(1, len(chunk) // 5):
                    continue
            if not chunk:
                continue
            try:
                _add(chunk.decode(enc, "strict"))
            except Exception:
                _add(chunk.decode(enc, "ignore"))
        except Exception:
            pass

    # GBK Chinese-run scan (only if not clearly utf16)
    if not _utf16_hint(raw):
        try:
            i = 0
            n = min(len(raw), 48)
            while i < n - 1:
                b0, b1 = raw[i], raw[i + 1]
                if 0x81 <= b0 <= 0xFE and (
                    0x40 <= b1 <= 0x7E or 0x80 <= b1 <= 0xFE
                ):
                    j = i
                    while j < n - 1:
                        x0, x1 = raw[j], raw[j + 1]
                        if 0x81 <= x0 <= 0xFE and (
                            0x40 <= x1 <= 0x7E or 0x80 <= x1 <= 0xFE
                        ):
                            j += 2
                        else:
                            break
                    if j - i >= 4:
                        try:
                            _add(raw[i:j].decode("gbk", "strict"))
                        except Exception:
                            pass
                    i = max(j, i + 1)
                else:
                    i += 1
        except Exception:
            pass

    return out


def _pick_best_fly_name(cands: list[str]) -> str:
    if not cands:
        return ""
    ranked = sorted(
        ((_score_fly_name(s), s) for s in cands),
        key=lambda x: x[0],
        reverse=True,
    )
    best_score, best = ranked[0]
    pure = _extract_pure_fly_name(best)
    pure_score = _score_fly_name(pure) if pure else -10**9
    # Prefer pure Chinese if close
    if pure and pure_score >= best_score - 8:
        best, best_score = pure, pure_score
    # Drop low-confidence mojibake — caller falls back to 场景/坐标
    if best_score < 30:
        return pure if pure and _score_fly_name(pure) >= 30 else ""
    return best


def _read_cstr_name(session: GameAttachSession, ptr: int, *, max_len: int = 48) -> str:
    """Read null-terminated name at pointer (GBK/UTF-16). @author by ak"""
    ptr = int(ptr or 0) & 0xFFFFFFFF
    if not ptr or ptr < 0x10000 or ptr > FLY_PTR_MAX:
        return ""
    raw = _read_bytes(session, ptr, max_len) or b""
    return _pick_best_fly_name(_decode_name_blob(raw))


def _read_acstring(session: GameAttachSession, addr: int, *, log: LogFn | None = None) -> str:
    """
    Read fly/UI name field at addr.

    Fly slot +0x10 is typically a **char*** (or ACString with ptr at +0).
    Do NOT trust ACString length header blindly — wrong len caused 模槽贷 mojibake.

    @author by ak
    """
    log = log or (lambda _m: None)
    addr = int(addr) & 0xFFFFFFFF
    if not addr:
        return ""
    try:
        base = int(getattr(session, "module_base", 0) or 0)
        empty = 0
        if base:
            try:
                from app.core.plg_exports import note_va_to_live

                empty = _read_u32(
                    session, note_va_to_live(base, NOTE_VA_ACSTRING_EMPTY)
                )
            except Exception:
                empty = 0

        cands: list[str] = []

        def _absorb(s: str) -> None:
            if not s:
                return
            for x in (s, _extract_pure_fly_name(s)):
                x = _clean_text(x or "")
                if x and x not in cands:
                    cands.append(x)

        # A) classic pointer at +0 → C-string body (primary)
        data_ptr = _read_u32(session, addr)
        if data_ptr and data_ptr != empty and 0x10000 < data_ptr < FLY_PTR_MAX:
            _absorb(_read_cstr_name(session, data_ptr))
            # also try ACString length-prefixed body when header looks sane
            hdr = _read_bytes(session, (data_ptr - 0xC) & 0xFFFFFFFF, 0xC) or b""
            if len(hdr) >= 0xC:
                for off in (8, 4):
                    ln = struct.unpack_from("<i", hdr, off)[0]
                    if 2 <= ln <= 32:
                        raw = _read_bytes(session, data_ptr, ln + 2) or b""
                        for s in _decode_name_blob(raw[:ln]):
                            _absorb(s)
                        raww = _read_bytes(session, data_ptr, ln * 2 + 2) or b""
                        for s in _decode_name_blob(raww[: ln * 2]):
                            _absorb(s)

        # B) inline bytes at object (SSO / short buf)
        obj = _read_bytes(session, addr, 0x20) or b""
        if obj:
            for s in _decode_name_blob(obj):
                _absorb(s)
            for off in (0, 4, 8, 12):
                if off + 4 <= len(obj):
                    maybe = struct.unpack_from("<I", obj, off)[0]
                    if (
                        maybe
                        and maybe != data_ptr
                        and maybe != empty
                        and 0x10000 < maybe < FLY_PTR_MAX
                    ):
                        _absorb(_read_cstr_name(session, maybe))

        return _pick_best_fly_name(cands)
    except Exception as e:
        log(f"map_fly: acstring @0x{addr:X} err: {e}")
        return ""


def _read_fly_slot_name(
    session: GameAttachSession, slot_base: int, *, log: LogFn | None = None
) -> str:
    """
    Read one fly slot display name.

    Slot layout (0x14): +0 idx, +2 scene, +4/8/C xyz, +0x10 name ptr/ACString.
    @author by ak
    """
    log = log or (lambda _m: None)
    base = int(slot_base) & 0xFFFFFFFF
    # primary: field at +0x10
    name = _read_acstring(session, (base + 0x10) & 0xFFFFFFFF, log=log)
    if name and _score_fly_name(name) >= 10:
        return name
    # fallback: treat +0x10 dword as direct char*
    ptr = _read_u32(session, (base + 0x10) & 0xFFFFFFFF)
    alt = _read_cstr_name(session, ptr)
    if _score_fly_name(alt) > _score_fly_name(name):
        return alt
    return name or alt


def _iter_fly_page_map(
    session: GameAttachSession, *, log: LogFn | None = None
) -> list[tuple[int, int]]:
    """
    Walk fly_mgr+0x16C hash_map; return (map_key, page_obj) sorted by key.

    IMPORTANT: identity is the **hash map key**, not page_obj[+0].
    Rewriting key→obj_pid caused 0页/1页 串页（key0 被折叠成 packet1=小日子）.

    Node: +0 next, +8 key(u32), +0xC value(page_obj*)
    page_obj[+0] is packet hint only (use via resolve / list_mem).

    @author by ak
    """
    log = log or (lambda _m: None)
    out: list[tuple[int, int]] = []
    mgr = get_fly_manager_ptr(session, log=log)
    if not mgr:
        return out
    map_ptr = (int(mgr) + FLY_MGR_PAGE_MAP_OFF) & 0xFFFFFFFF
    try:
        buckets = _read_u32(session, map_ptr + 0x14)
        bend = _read_u32(session, map_ptr + 0x18)
        if not buckets or bend <= buckets:
            buckets = _read_u32(session, map_ptr + 0x10)
            bend = _read_u32(session, map_ptr + 0x14)
        if not buckets or bend <= buckets or bend - buckets > 0x10000:
            log(
                f"map_fly: page map buckets invalid map=0x{map_ptr:X} "
                f"b=0x{buckets:X} e=0x{bend:X}"
            )
            return out
        n = (int(bend) - int(buckets)) // 8
        n = max(0, min(n, 512))
        seen_nodes: set[int] = set()
        meta_log: list[str] = []
        for i in range(n):
            node = _read_u32(session, (buckets + i * 8 + 4) & 0xFFFFFFFF)
            steps = 0
            while node and node not in seen_nodes and steps < 128:
                seen_nodes.add(node)
                key = _read_u32(session, (node + 8) & 0xFFFFFFFF)
                page_obj = _read_u32(session, (node + 0xC) & 0xFFFFFFFF)
                if page_obj and 0x10000 < page_obj < FLY_PTR_MAX:
                    key_u = int(key) & 0xFFFFFFFF
                    # custom pages use small keys 0..32
                    if key_u <= 32:
                        b0 = _read_bytes(session, page_obj, 2) or b"\x00\x00"
                        obj_pid = int(b0[0]) & 0xFF
                        out.append((int(key_u), int(page_obj) & 0xFFFFFFFF))
                        if len(meta_log) < 16:
                            meta_log.append(f"{key_u}:obj={obj_pid}")
                node = _read_u32(session, node)
                steps += 1
        # dedupe by map key keep first
        uniq: dict[int, int] = {}
        for k, obj in out:
            if k not in uniq:
                uniq[k] = obj
        result = sorted(uniq.items(), key=lambda x: x[0])
        log(
            f"map_fly: page map walk map=0x{map_ptr:X} nodes={len(seen_nodes)} "
            f"keys={[k for k, _ in result]} meta=[{', '.join(meta_log)}]"
        )
        return result
    except Exception as e:
        log(f"map_fly: page map walk err: {e}")
        return out


def _find_page_object_ptr(
    session: GameAttachSession,
    page_id: int,
    *,
    also: list[int] | tuple[int, ...] | None = None,
    log: LogFn | None = None,
) -> int:
    """
    Find custom page object pointer for page_id from fly_mgr map.

    `also`: extra keys to try (e.g. UI index and packet page).
    Prefer map walk (no CRT, no throw). @author by ak
    """
    log = log or (lambda _m: None)
    wants: list[int] = []
    for x in (page_id, *(also or ())):
        v = int(x) & 0xFF
        if v not in wants:
            wants.append(v)
    for want in wants:
        for pid, obj in _iter_fly_page_map(session, log=log):
            if int(pid) == want:
                return int(obj) & 0xFFFFFFFF
    return 0


def list_mem_transmit_page_slots(
    session: GameAttachSession,
    page_id: int,
    *,
    include_empty_slots: bool = True,
    page_id_is_ui: bool = True,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Read one custom page's slots from fly_mgr memory (no UI radio needed).

    page_id: by default UI index (0页=0 = 第一页自定义).
    Returned slot meta.page_id / packet_page is the custom page key (0..N).

    @author by ak
    """
    log = log or (lambda _m: None)
    pid_page = int(page_id) & 0xFF
    if pid_page == 0xFF:
        return MapFlyResult(
            ok=False,
            action="mem_page",
            message="默认页请走 UI 文本路径",
            error="use_ui",
            detail={"page_id": pid_page, "slots": []},
        )
    # An unavailable manager is a transient read failure, not an empty page.
    # Returning ok=True/all-empty here made the UI silently show no children,
    # especially while a role was changing scene or had not loaded fly data.
    mgr = get_fly_manager_ptr(session, log=log)
    if not mgr:
        return MapFlyResult(
            ok=False,
            action="mem_page",
            message=f"飞行页数据未就绪（{pid_page}页）",
            error="fly_mgr_unavailable",
            detail={
                "index": pid_page,
                "page_id": ui_page_to_packet_page(pid_page),
                "logical_page": pid_page,
                "ui_page": pid_page,
                "packet_page": ui_page_to_packet_page(pid_page),
                "label": f"{pid_page}页",
                "slots": [],
                "source": "mem",
                "page_obj": 0,
            },
        )

    # A healthy manager may legitimately have an empty page directory (for
    # example, a character that has not created any custom fly page yet).
    # Treat it exactly like six absent fixed page keys, not as a read failure.
    page_map = _iter_fly_page_map(session, log=log)
    loaded_keys = {int(key) & 0xFF for key, _obj in page_map}

    if page_id_is_ui:
        ui_page = pid_page
        # 关键：先按 UI/map key 取页对象（0页 → key 0 = 游戏第一自定义页）
        page_obj = _find_page_object_ptr(session, ui_page, log=log)
        packet_pages = resolve_fly_packet_pages(
            session, ui_page, is_default=False, page_id_is_packet=False, log=log
        )
        # 若对象上有权威 packet id，优先用它
        if page_obj:
            packet_page = packet_page_for_map_key(
                session, ui_page, page_obj, log=log
            )
        else:
            packet_page = (
                int(packet_pages[0])
                if packet_pages
                else ui_page_to_packet_page(ui_page)
            )
            if not page_obj and packet_page is not None:
                page_obj = _find_page_object_ptr(session, packet_page, log=log)
    else:
        ui_page = packet_page_to_ui_page(pid_page)
        if ui_page is None:
            ui_page = max(0, (pid_page - 1) & 0xFF) if pid_page else 0
        packet_pages = resolve_fly_packet_pages(
            session, pid_page, is_default=False, page_id_is_packet=True, log=log
        )
        packet_page = int(packet_pages[0]) if packet_pages else pid_page
        page_obj = _find_page_object_ptr(
            session, packet_page, also=[ui_page, pid_page], log=log
        )

    if not page_obj:
        # The manager is healthy and its directory is authoritative. A missing
        # fixed key therefore means this character has no object for that page,
        # not that the read path failed. Keep the ten empty children visible.
        if page_id_is_ui and int(ui_page) not in loaded_keys:
            use_page = ui_page_to_packet_page(ui_page)
            slots = []
            if include_empty_slots:
                for slot in range(FLY_PAGE_SLOT_COUNT):
                    slots.append(
                        {
                            "slot": slot,
                            "name": f"槽{slot + 1} (空)",
                            "raw_text": "",
                            "empty": True,
                            "page_id": use_page,
                            "logical_page": ui_page,
                            "ui_page": ui_page,
                            "packet_page": use_page,
                            "source": "mem",
                        }
                    )
            return MapFlyResult(
                ok=True,
                action="mem_page",
                message=f"内存页「{ui_page}页」读到 {len(slots)} 槽，有名 0（未建页对象）",
                detail={
                    "index": pid_page,
                    "rdo": f"Rdo_{ui_page + 1}",
                    "page_id": use_page,
                    "logical_page": ui_page,
                    "ui_page": ui_page,
                    "packet_page": use_page,
                    "label": f"{ui_page}页",
                    "slots": slots,
                    "source": "mem",
                    "page_obj": 0,
                },
            )
        return MapFlyResult(
            ok=False,
            action="mem_page",
            message=f"{ui_page}页尚未加载到飞行页目录",
            error="fly_page_not_loaded",
            detail={
                "index": pid_page,
                "rdo": f"Rdo_{(int(packet_page) if packet_page is not None else int(pid_page)) + 1}",
                "page_id": int(packet_page) if packet_page is not None else int(pid_page),
                "logical_page": ui_page if page_id_is_ui else pid_page,
                "ui_page": ui_page if page_id_is_ui else packet_page_to_ui_page(pid_page),
                "packet_page": packet_page,
                "label": f"{ui_page if page_id_is_ui else pid_page}页",
                "slots": [],
                "source": "mem",
                "page_obj": 0,
            },
        )

    page_name_raw = _read_acstring(session, page_obj + 4, log=log)
    if page_name_raw and _is_sane_display_name(page_name_raw):
        page_name = page_name_raw
    else:
        page_name = ""
    slots: list[dict] = []
    for slot in range(FLY_PAGE_SLOT_COUNT):
        base = (page_obj + FLY_PAGE_SLOT_BASE + slot * FLY_PAGE_SLOT_STRIDE) & 0xFFFFFFFF
        raw = _read_bytes(session, base, FLY_PAGE_SLOT_STRIDE) or b""
        if len(raw) < FLY_PAGE_SLOT_STRIDE:
            empty = True
            name = f"槽{slot + 1} (空)"
            scene = 0
            pos = None
            raw_text = ""
        else:
            slot_idx = raw[0]
            scene = struct.unpack_from("<H", raw, 2)[0]
            x = struct.unpack_from("<f", raw, 4)[0]
            y = struct.unpack_from("<f", raw, 8)[0]
            z = struct.unpack_from("<f", raw, 0xC)[0]
            raw_name = _read_fly_slot_name(session, base, log=log)
            disp, empty = _format_fly_slot_label(
                raw_name=raw_name,
                scene_id=int(scene),
                x=float(x),
                y=float(y),
                z=float(z),
                slot=int(slot),
            )
            if empty:
                raw_text = ""
                pos = None
                name = disp
            else:
                raw_text = disp
                pos = [float(x), float(y), float(z)]
                name = disp
        if empty and not include_empty_slots:
            continue
        use_page = int(packet_page) if packet_page is not None else int(ui_page_to_packet_page(ui_page))
        slots.append(
            {
                "slot": slot,
                "name": name if not empty else f"槽{slot + 1} (空)",
                "raw_text": raw_text if not empty else "",
                "empty": empty,
                "page_id": use_page,
                "logical_page": ui_page,
                "ui_page": ui_page,
                "packet_page": use_page,
                "scene_id": int(scene) if not empty else 0,
                "pos": pos,
                "source": "mem",
            }
        )
    nonempty = sum(1 for s in slots if not s.get("empty"))
    ui_lab = ui_page if page_id_is_ui else (packet_page_to_ui_page(pid_page) or pid_page)
    label = page_name or f"{ui_lab}页"
    use_page = (
        int(packet_page)
        if packet_page is not None
        else ui_page_to_packet_page(
            ui_page if page_id_is_ui else max(0, pid_page - 1)
        )
    )
    msg = (
        f"内存页「{label}」读到 {len(slots)} 槽，有名 {nonempty} "
        f"ui={ui_lab} packet={use_page}"
    )
    log(f"map_fly: {msg} page_obj=0x{page_obj:X}")
    return MapFlyResult(
        ok=True,
        action="mem_page",
        message=msg,
        detail={
            "index": int(ui_lab) if isinstance(ui_lab, int) else pid_page,
            "rdo": f"Rdo_{int(use_page) + 1 if int(use_page) != 0xFF else 0}",
            "page_id": use_page,
            "logical_page": ui_lab,
            "ui_page": ui_lab,
            "packet_page": packet_page if packet_page is not None else use_page,
            "label": label,
            "slots": slots,
            "source": "mem",
            "page_obj": page_obj,
            "page_name": page_name,
        },
    )


def list_transmit_page_slots(
    session: GameAttachSession,
    radio_index: int,
    *,
    page_id: int | None = None,
    is_default: bool | None = None,
    open_if_needed: bool = True,
    include_empty_slots: bool = True,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Query one page and return slot names.

    - is_default/True or page_id 0xFF: 默认页 UI
    - page_id 0..N: **UI 自定义页索引**（0页=除默认外第一页），内存优先
    - 返回 detail.page_id = **packet page (1..N / 0xFF)**，供飞行发包

    Strategy:
      - 默认页：UI Txt_Pos
      - 自定义页：优先 UI 切 Rdo_{packet}；失败则内存 map 直读

    @author by ak
    """
    log = log or (lambda _m: None)
    idx = max(0, min(int(radio_index), FIXED_CUSTOM_FLY_PAGE_COUNT))
    if is_default is None:
        is_default = page_id is None and idx == 0
    if page_id is None:
        # radio 0=默认, radio N>=1 → UI page N-1
        if is_default or idx <= 0:
            page_id = 0xFF
            is_default = True
        else:
            page_id = (idx - 1) & 0xFF  # Rdo_1 → UI 0页
            is_default = False
    page_id = int(page_id) & 0xFF  # here: UI index for custom, or 0xFF default
    if is_default or page_id == 0xFF:
        packet_page = 0xFF
        ui_page = None
        rdo = "Rdo_0"
        page_label = "默认"
    else:
        ui_page = page_id
        if ui_page not in FIXED_CUSTOM_FLY_PAGES:
            return MapFlyResult(
                ok=False,
                action="page_slots",
                message=f"自定义页超出固定范围: {ui_page}",
                error="bad_page",
                detail={"ui_page": ui_page, "slots": []},
            )
        packet_page = ui_page_to_packet_page(ui_page)
        # Rdo_0=默认; 自定义 key/packet k → Rdo_(k+1)
        rdo = f"Rdo_{int(packet_page) + 1}"
        page_label = f"{ui_page}页"

    # ---- default page: UI path ----
    if is_default or page_id == 0xFF:
        if open_if_needed:
            op = open_transmit_flag(session, log=log)
            if not op.ok and not is_transmit_flag_open(session, log=log)[0]:
                return MapFlyResult(
                    ok=False,
                    action="page_slots",
                    message=f"无法打开飞行旗: {op.message}",
                    error=op.error or "dlg_closed",
                    detail={"open": op.to_dict()},
                )
        shown, dlg = is_transmit_flag_open(session, log=log)
        if shown and dlg:
            # Prefer reading current default tab texts; avoid remote GetDlgItem/click
            # unless we must switch (skip switch to reduce crash risk).
            listed = list_ui_transmit_points(
                session,
                open_if_needed=False,
                apply_fixed_labels=True,
                log=log,
            )
            rows = list((listed.detail or {}).get("rows") or [])
            slots: list[dict] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                slot = int(row.get("slot") or 0)
                name = _clean_text(str(row.get("name") or ""))
                raw = _clean_text(str(row.get("raw_text") or ""))
                empty = not bool(name or raw)
                if empty and not include_empty_slots:
                    continue
                disp = f"槽{slot + 1} (空)" if empty else (name or raw or f"槽{slot + 1}")
                slots.append(
                    {
                        "slot": slot,
                        "name": disp,
                        "raw_text": raw,
                        "empty": empty,
                        "page_id": 0xFF,
                        "packet_page": 0xFF,
                        "source": "ui",
                    }
                )
            nonempty = sum(1 for s in slots if not s.get("empty"))
            msg = f"页「{page_label}」读到 {len(slots)} 槽，有名 {nonempty}"
            log(f"map_fly: {msg} page_id=0xFF src=ui")
            return MapFlyResult(
                ok=True,
                action="page_slots",
                message=msg,
                detail={
                    "index": 0,
                    "rdo": rdo,
                    "page_id": 0xFF,
                    "packet_page": 0xFF,
                    "label": page_label,
                    "slots": slots,
                    "source": "ui",
                },
            )
        return MapFlyResult(
            ok=False,
            action="page_slots",
            message="默认页需要打开飞行旗界面",
            error="dlg_closed",
        )

    # ---- custom page: MEMORY ONLY (no open-item / no GetDlgItem / no click) ----
    # UI radio path previously crashed the client (stale dlg + remote thiscall).
    # Custom pages are fully readable from fly_mgr+0x16C map.
    log(
        f"map_fly: 页{page_label} 直读内存 ui={ui_page} packet={packet_page} "
        f"(跳过飞行旗UI，防崩溃)"
    )
    try:
        mem = list_mem_transmit_page_slots(
            session,
            int(ui_page) if ui_page is not None else page_id,
            include_empty_slots=include_empty_slots,
            page_id_is_ui=True,
            log=log,
        )
    except Exception as e:
        log(f"map_fly: mem page read err: {e}")
        return MapFlyResult(
            ok=False,
            action="page_slots",
            message=f"内存读页失败: {e}",
            error="mem_exception",
            detail={
                "index": int(ui_page) if ui_page is not None else idx,
                "page_id": packet_page,
                "ui_page": ui_page,
                "packet_page": packet_page,
                "label": page_label,
            },
        )
    detail = dict(mem.detail or {})
    detail["index"] = int(ui_page) if ui_page is not None else idx
    detail["rdo"] = rdo
    detail["ui_error"] = "skipped_ui_for_stability"
    detail["source"] = "mem"
    detail["ui_page"] = ui_page
    if detail.get("packet_page") is None:
        detail["packet_page"] = packet_page
    if detail.get("page_id") is None:
        detail["page_id"] = packet_page
    if not detail.get("label"):
        detail["label"] = page_label
    return MapFlyResult(
        ok=bool(mem.ok),
        action="page_slots",
        message=str(mem.message or "") + " [mem-only]",
        error=None if mem.ok else (mem.error or "mem_failed"),
        detail=detail,
    )



def list_transmit_catalog(
    session: GameAttachSession,
    *,
    open_if_needed: bool = True,
    max_pages: int = FIXED_CUSTOM_FLY_PAGE_COUNT + 1,
    include_empty_slots: bool = True,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Enumerate fly-flag pages (Rdo_*) and slot names (Txt_Pos*).

    Returns detail.pages = [
      {index, rdo, page_id, label, slots:[{slot, name, raw_text, empty}]}
    ]

    @author by ak
    """
    log = log or (lambda _m: None)
    if open_if_needed:
        op = open_transmit_flag(session, log=log)
        if not op.ok and not is_transmit_flag_open(session, log=log)[0]:
            return MapFlyResult(
                ok=False,
                action="catalog",
                message=f"无法打开飞行旗: {op.message}",
                error=op.error or "dlg_closed",
                detail={"open": op.to_dict()},
            )
    shown, dlg = is_transmit_flag_open(session, log=log)
    if not shown or not dlg:
        return MapFlyResult(
            ok=False, action="catalog", message="Win_TransmitFlag 未打开", error="dlg_closed"
        )

    pages: list[dict] = []
    max_n = max(1, min(int(max_pages), 12))
    for i in range(max_n):
        rdo = f"Rdo_{i}"
        rp = get_aui_dlg_item_ptr(session, dlg, rdo, log=lambda _m: None)
        if not rp:
            if i == 0:
                # default radio missing name? still try read current page once
                pass
            else:
                break
        rdo_txt = _aui_get_text(session, rp, log=lambda _m: None) if rp else ""
        rdo_txt = _clean_text(rdo_txt)
        if i == 0:
            page_label = rdo_txt or "默认页"
        else:
            page_label = rdo_txt or f"第{i}页"
        page_id = packet_page_for_radio_index(i)

        if rp:
            sr = select_transmit_radio(session, rdo, log=log)
            if not sr.ok and i > 0:
                log(f"map_fly: skip page {rdo}: {sr.message}")
                continue
            time.sleep(0.12)

        listed = list_ui_transmit_points(
            session,
            open_if_needed=False,
            apply_fixed_labels=(i == 0),
            log=log,
        )
        rows = list((listed.detail or {}).get("rows") or [])
        slots: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            slot = int(row.get("slot") or 0)
            name = _clean_text(str(row.get("name") or ""))
            raw = _clean_text(str(row.get("raw_text") or ""))
            empty = not bool(name or raw)
            if empty and not include_empty_slots:
                continue
            if empty:
                disp = f"槽{slot + 1} (空)"
            else:
                disp = name or raw or f"槽{slot + 1}"
            slots.append(
                {
                    "slot": slot,
                    "name": disp,
                    "raw_text": raw,
                    "empty": empty,
                    "page_id": page_id,
                }
            )
        pages.append(
            {
                "index": i,
                "rdo": rdo,
                "page_id": page_id,
                "label": page_label,
                "slots": slots,
            }
        )
        log(
            f"map_fly: catalog page idx={i} page_id={page_id}/0x{page_id:02X} "
            f"label={page_label!r} slots={len(slots)}"
        )

    # restore default page radio for UX
    try:
        if pages:
            select_transmit_radio(session, "Rdo_0", log=lambda _m: None)
    except Exception:
        pass

    nonempty_pages = sum(1 for p in pages if any(not s.get("empty") for s in p.get("slots") or []))
    msg = f"读取 {len(pages)} 页，有名点约 {nonempty_pages} 页"
    return MapFlyResult(
        ok=True,
        action="catalog",
        message=msg,
        detail={"dlg": dlg, "pages": pages},
    )


def fly_official_slot(
    session: GameAttachSession,
    page_id: int,
    slot: int,
    *,
    label: str = "",
    wait_cooldown: bool = False,
    respect_cooldown: bool = True,
    open_if_needed: bool = True,
    prefer_ui_click: bool = False,
    radio_index: int | None = None,
    expected_pos: list | tuple | None = None,
    expected_scene: int | None = None,
    stop_event=None,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Fly to explicit page/slot via the generic 0x86 raw packet (no AUI / no UI click).

    Deprecated thin wrapper over fly_page_slot_packet; kept for API compatibility.
    page_id is packet page (0xFF default / 0..5 custom); slot 0..9.

    @author by ak
    """
    return fly_page_slot_packet(
        session,
        int(page_id),
        int(slot),
        label=label,
        wait_cooldown=wait_cooldown,
        respect_cooldown=respect_cooldown,
        expected_pos=expected_pos,
        expected_scene=expected_scene,
        stop_event=stop_event,
        log=log,
    )


def _snapshot_scene_pos(
    session: GameAttachSession,
    *,
    fresh: bool = False,
    log: LogFn | None = None,
) -> dict:
    """
    Best-effort scene_id + pos snapshot.

    fresh=False: 非马上需要，可先用 state/hub 短缓存。
    fresh=True: 起飞后校验等必须时效，直读并回写分发缓存。
    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {"ok": False}
    if not fresh:
        try:
            from app.core.state_dispatch import StateKind, get_state

            sc = get_state(session, StateKind.SCENE, fresh=False, log=lambda _m: None)
            if isinstance(sc, dict) and (
                sc.get("scene_id") is not None or sc.get("pos") is not None
            ):
                out["ok"] = True
                out["scene_id"] = sc.get("scene_id")
                pos = sc.get("pos")
                if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                    out["pos"] = [float(pos[0]), float(pos[1]), float(pos[2])]
                out["source"] = sc.get("source") or "state_soft"
                return out
        except Exception:
            pass
    try:
        from app.core.automove import read_scene_position

        sp = read_scene_position(session, log=lambda _m: None)
        out["ok"] = bool(getattr(sp, "ok", False))
        out["scene_id"] = getattr(sp, "scene_id", None)
        pos = getattr(sp, "scene_pos", None)
        if isinstance(pos, (list, tuple)) and len(pos) >= 3:
            out["pos"] = [float(pos[0]), float(pos[1]), float(pos[2])]
        out["source"] = "live"
        if out["ok"] and fresh:
            try:
                from app.core.live_scene_hub import publish_live_scene
                from app.core.state_dispatch import StateKind, invalidate_states

                # 直读成功：失效旧缓存并推送 hub，供其它模块分发
                invalidate_states(session, StateKind.SCENE, StateKind.POS)
                pos_t = None
                if isinstance(out.get("pos"), list) and len(out["pos"]) >= 3:
                    pos_t = (float(out["pos"][0]), float(out["pos"][1]), float(out["pos"][2]))
                publish_live_scene(
                    int(getattr(session, "pid", 0) or 0),
                    scene_id=out.get("scene_id"),
                    pos=pos_t,
                    source="map_fly_snapshot",
                )
            except Exception:
                pass
    except Exception as e:
        out["error"] = str(e)
    if fresh and not out.get("ok"):
        try:
            from app.core.remote_runtime import (
                get_pid_scene_snapshot,
                is_pid_scene_snapshot_stable,
            )

            pid = int(getattr(session, "pid", 0) or 0)
            gate = get_pid_scene_snapshot(pid, max_age_s=2.0)
            sid = int(gate.get("scene_id") or 0)
            if gate.get("ready") and sid > 0:
                out.update(
                    {
                        "ok": True,
                        "scene_id": sid,
                        "source": "scene_gate",
                        "scene_settling": not is_pid_scene_snapshot_stable(pid),
                    }
                )
        except Exception:
            pass
    return out


def _moved(before: dict | None, after: dict | None, *, min_dist: float = 1.5) -> bool:
    """True if scene changed or xz moved enough. @author by ak"""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if not before.get("ok") or not after.get("ok"):
        return False
    if before.get("scene_id") != after.get("scene_id"):
        return True
    bp = before.get("pos") or []
    ap = after.get("pos") or []
    if len(bp) >= 3 and len(ap) >= 3:
        dx = float(ap[0]) - float(bp[0])
        dz = float(ap[2]) - float(bp[2])
        return (dx * dx + dz * dz) ** 0.5 >= float(min_dist)
    return False


_FLY_MGR_CACHE: dict[int, tuple[float, int]] = {}
_FLY_MGR_CACHE_TTL_S = 3.0
# xajh.exe carries IMAGE_FILE_LARGE_ADDRESS_AWARE (verified in its PE
# header): as a 32-bit process it can place user-mode heap in the 2..4 GB
# window on x64 Windows (live example: fly_mgr=0x82990038). Pointer guards
# must accept that range; structural sanity checks catch actual garbage.
FLY_PTR_MAX = 0xFFFF0000
# GetHostData@0x4AE420: *global -> +0x24 -> +0x90; fly_mgr=*(host+0x40)
NOTE_VA_HOST_ROOT_GLOBAL = 0x015282D8


def _fly_mgr_via_rpm(session: GameAttachSession, base: int) -> int:
    """Resolve fly_mgr without remote code execution. @author by ak"""
    try:
        from app.core.plg_exports import note_va_to_live

        g_va = note_va_to_live(base, NOTE_VA_HOST_ROOT_GLOBAL)
        g = _read_u32(session, g_va)
        if not g or g < 0x10000:
            return 0
        mid = _read_u32(session, (g + 0x24) & 0xFFFFFFFF)
        if not mid or mid < 0x10000:
            return 0
        host = _read_u32(session, (mid + 0x90) & 0xFFFFFFFF)
        if not host or host < 0x10000:
            return 0
        mgr = int(_read_u32(session, (host + 0x40) & 0xFFFFFFFF) or 0) & 0xFFFFFFFF
        if not mgr or mgr < 0x10000 or mgr > FLY_PTR_MAX:
            return 0
        # page map region must be readable
        _ = _read_u32(session, (mgr + 0x16C) & 0xFFFFFFFF)
        return mgr
    except Exception:
        return 0


def _page_map_sane(session: GameAttachSession, mgr: int) -> bool:
    """True when fly_mgr's page hash-map looks like a plausible live structure.

    Guards against a stale/garbage fly_mgr pointer (e.g. a transient RPM read
    returned a wrong heap address) being cached and reused for seconds.

    @author by ak
    """
    mgr = int(mgr) & 0xFFFFFFFF
    if not mgr or mgr < 0x10000 or mgr > FLY_PTR_MAX:
        return False
    map_ptr = (mgr + FLY_MGR_PAGE_MAP_OFF) & 0xFFFFFFFF
    try:
        b = _read_u32(session, map_ptr + 0x14)
        e = _read_u32(session, map_ptr + 0x18)
        if not b or not e or e <= b:
            b = _read_u32(session, map_ptr + 0x10)
            e = _read_u32(session, map_ptr + 0x14)
        if not b or not e or e <= b:
            return False
        delta = int(e) - int(b)
        # bucket array must be a small multiple of 8
        if delta <= 0 or delta > 0x10000 or (delta % 8) != 0:
            return False
        return True
    except Exception:
        return False


def get_fly_manager_ptr(session: GameAttachSession, *, log: LogFn | None = None) -> int:
    """
    Fly/transmit data manager: *(GetHostData() + 0x40).

    Prefer pure RPM chain; remote_call only last resort (can crash client).

    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(getattr(session, "pid", 0) or 0)
    base = int(getattr(session, "module_base", 0) or 0)
    now = time.monotonic()
    if pid:
        hit = _FLY_MGR_CACHE.get(pid)
        if hit and (now - float(hit[0])) < _FLY_MGR_CACHE_TTL_S and hit[1]:
            mgr = int(hit[1]) & 0xFFFFFFFF
            try:
                if mgr and _page_map_sane(session, mgr):
                    return mgr
            except Exception:
                pass
            _FLY_MGR_CACHE.pop(pid, None)
    if not base or not pid:
        return 0

    mgr = _fly_mgr_via_rpm(session, base)
    if mgr:
        _FLY_MGR_CACHE[pid] = (now, mgr)
        return mgr

    try:
        from app.core.plg_exports import note_va_to_live
        from app.core.remote_runtime import remote_call_cdecl_x86

        log("map_fly: fly_mgr RPM miss → remote 0x4AE420 (last resort)")
        va = note_va_to_live(base, 0x004AE420)
        root = int(remote_call_cdecl_x86(pid, va, [], timeout_ms=800) or 0) & 0xFFFFFFFF
        if not root or root < 0x10000:
            return 0
        mgr = int(_read_u32(session, (root + 0x40) & 0xFFFFFFFF) or 0) & 0xFFFFFFFF
        if not mgr or mgr < 0x10000 or mgr > FLY_PTR_MAX:
            return 0
        # page map region must be readable (mirrors RPM path sanity check)
        try:
            if not _read_u32(session, (mgr + FLY_MGR_PAGE_MAP_OFF) & 0xFFFFFFFF):
                return 0
        except Exception:
            return 0
        _FLY_MGR_CACHE[pid] = (now, mgr)
        return mgr
    except Exception as e:
        log(f"map_fly: fly_mgr err: {e}")
        return 0


# Death/relive pos on fly manager (filled by S2C type 0x127 @ 0x51BB50)
FLY_MGR_DEATH_SCENE_OFF = 0x15C  # u16
FLY_MGR_DEATH_FLAG_OFF = 0x15E  # u8
FLY_MGR_DEATH_X_OFF = 0x15F  # f32
FLY_MGR_DEATH_Y_OFF = 0x163  # f32
FLY_MGR_DEATH_Z_OFF = 0x167  # f32
# Custom pages map (hash_map) at fly_mgr+0x16C; S2C 0x127 fills page objs
FLY_MGR_PAGE_MAP_OFF = 0x16C
# page object (size 0xD0): +0 page_id u8, +1 u8, +4 ACString name,
# slots base +0x08, stride 0x14:
#   +0 slot u8, +2 scene u16, +4/8/C f32 xyz, +0x10 ACString name
FLY_PAGE_SLOT_BASE = 0x08
FLY_PAGE_SLOT_STRIDE = 0x14
FLY_PAGE_SLOT_COUNT = 10
NOTE_VA_FLY_PAGE_MAP_FIND = 0x0051A120  # thiscall map*, key* -> &page_obj*
NOTE_VA_ACSTRING_EMPTY = 0x014E6A54  # global empty ACString data ptr


def read_death_point(
    session: GameAttachSession, *, log: LogFn | None = None
) -> MapFlyResult:
    """
    Read client-cached last-death / relive display pos from fly manager.

    Source: S2C 0x127 (server push). Local edit does NOT change server fly target.

    @author by ak
    """
    log = log or (lambda _m: None)
    mgr = get_fly_manager_ptr(session, log=log)
    if not mgr:
        return MapFlyResult(
            ok=False, action="read_death", message="fly_mgr null", error="no_mgr"
        )
    raw = _read_bytes(session, mgr + FLY_MGR_DEATH_SCENE_OFF, 0x10)
    if len(raw) < 0x10:
        return MapFlyResult(
            ok=False, action="read_death", message="rpm short", error="rpm"
        )
    scene = struct.unpack_from("<H", raw, 0)[0]
    flag = raw[2]
    x = struct.unpack_from("<f", raw, 3)[0]
    y = struct.unpack_from("<f", raw, 7)[0]
    z = struct.unpack_from("<f", raw, 0xB)[0]
    detail = {
        "fly_mgr": hex(mgr),
        "scene_id": int(scene),
        "flag": int(flag),
        "pos": [float(x), float(y), float(z)],
        "layout": "mgr+0x15C u16 scene, +0x15E u8, +0x15F/163/167 f32 xyz",
        "source": "S2C type 0x127 (server authoritative)",
        "forge_client_then_fly": False,
        "forge_reason": (
            "飞死亡点只发 page/slot=默认第3格；目的地以服务器死亡点为准。"
            "客户端改 mgr 缓存只影响显示，不会改 action=2 落点。"
        ),
    }
    log(
        f"map_fly: death point scene={scene} flag={flag} "
        f"pos=({x:.3f},{y:.3f},{z:.3f}) mgr=0x{mgr:X}"
    )
    return MapFlyResult(
        ok=True,
        action="read_death",
        message=f"死亡点缓存 scene={scene} ({x:.3f},{y:.3f},{z:.3f}) [S2C 0x127]",
        detail=detail,
    )


def research_death_coord_entry(
    session: GameAttachSession, *, log: LogFn | None = None
) -> MapFlyResult:
    """
    Research: can we set death coords then fly default slot2?

    @author by ak
    """
    log = log or (lambda _m: None)
    death = read_death_point(session, log=log)
    detail = {
        "live_cache": death.to_dict(),
        "s2c_type": 0x127,
        "s2c_handler_note_va": "0x51BB50 cmp type==0x127 then fill mgr+0x15C..",
        "c2s_report_death_xyz": False,
        "c2s_note": (
            "未发现客户端可任意上报死亡坐标的 c2s；"
            "死亡点由服务器在角色死亡时记录，再 0x127 下发。"
        ),
        "fly_death_slot": {
            "slot": 2,
            "page": 0xFF,
            "feasible_if_died_there": True,
            "feasible_if_forge_client_only": False,
        },
        "conclusion": [
            "不能：本地改死亡坐标 → 飞死亡点到任意位置",
            "可以：在地点 A 死亡（服端记录）→ 复活后飞默认第3格回 A",
            "不能：伪造「上报死亡坐标」包把死亡点写成没去过的地方（无此 c2s / 服权威）",
        ],
    }
    log("map_fly: death-coord research — 服权威 S2C 0x127；伪造客户端缓存无效")
    return MapFlyResult(
        ok=True,
        action="death_research",
        message=(
            "结论：死亡点坐标是服务器下发(S2C 0x127)，"
            "不是客户端可随意上报的；改本地缓存不能当任意飞。"
            "只有真实死亡才会更新服务器死亡点，之后可飞默认 slot2。"
        ),
        detail=detail,
    )



# ---------------------------------------------------------------------------
# Production fly (auto-task / group control)
# ---------------------------------------------------------------------------

# Client-side fly cooldown (seconds). Server may also reject; we wait & retry once.
DEFAULT_FLY_COOLDOWN_S = 3.0
FLY_SERVER_RETRY_WAIT_S = 2.2

_FLY_CD_LOCK = threading.Lock()
_FLY_CD_UNTIL: dict[int, float] = {}  # pid -> time.monotonic() deadline


def fly_cooldown_remain(pid: int) -> float:
    """Seconds remaining on client-side fly CD for pid. @author by ak"""
    with _FLY_CD_LOCK:
        until = float(_FLY_CD_UNTIL.get(int(pid), 0.0) or 0.0)
    left = until - time.monotonic()
    return left if left > 0 else 0.0


def mark_fly_cooldown(pid: int, seconds: float | None = None) -> float:
    """
    Start/refresh client-side fly CD. Returns deadline remaining seconds.

    @author by ak
    """
    sec = float(DEFAULT_FLY_COOLDOWN_S if seconds is None else seconds)
    sec = max(0.0, sec)
    until = time.monotonic() + sec
    with _FLY_CD_LOCK:
        prev = float(_FLY_CD_UNTIL.get(int(pid), 0.0) or 0.0)
        if until > prev:
            _FLY_CD_UNTIL[int(pid)] = until
        else:
            until = prev
    left = until - time.monotonic()
    return left if left > 0 else 0.0


def _wait_fly_cooldown(
    pid: int,
    *,
    stop_event=None,
    log: LogFn | None = None,
    max_wait_s: float = 12.0,
) -> tuple[bool, float]:
    """
    Wait until client fly CD clears (interruptible).

    Returns (ok_to_proceed, waited_seconds).
    @author by ak
    """
    log = log or (lambda _m: None)
    start = time.monotonic()
    while True:
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            return False, time.monotonic() - start
        left = fly_cooldown_remain(pid)
        if left <= 0:
            return True, time.monotonic() - start
        if time.monotonic() - start >= float(max_wait_s):
            log(f"map_fly: CD wait timeout left={left:.1f}s")
            return False, time.monotonic() - start
        log(f"map_fly: 冷却中 {left:.1f}s…")
        time.sleep(min(0.35, left))


def fly_official_preset(
    session: GameAttachSession,
    preset_key: str,
    *,
    wait_cooldown: bool = True,
    respect_cooldown: bool = True,
    open_if_needed: bool = True,
    prefer_ui_click: bool = True,
    single_send: bool = False,
    stop_event=None,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Fly a fixed default point (福州/师门/死亡) via the generic 0x86 raw packet.

    Deprecated thin wrapper over fly_page_slot_packet; kept for API compatibility.
    Only the packet send path is used (no AUI / no UI click).

    @author by ak
    """
    log = log or (lambda _m: None)
    key = str(preset_key or "").strip().lower()
    meta = PRESET_POINTS.get(key)
    if not meta:
        return MapFlyResult(
            ok=False,
            action="fly_official",
            message=f"未知预设 {preset_key}",
            error="bad_preset",
        )
    return fly_page_slot_packet(
        session,
        0xFF,
        int(meta["slot"]),
        label=str(meta["label"]),
        wait_cooldown=wait_cooldown,
        respect_cooldown=respect_cooldown,
        stop_event=stop_event,
        log=log,
    )


def fly_to_preset(
    session: GameAttachSession,
    preset_key: str,
    *,
    page_id: int | None = None,
    open_if_needed: bool = True,
    prefer_packet: bool = True,
    prefer_ui_click: bool = True,
    inspect_points: bool = True,
    single_send: bool = False,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Fly to a known default point via the generic 0x86 raw packet.

    Deprecated thin wrapper over fly_page_slot_packet; kept for API compatibility.
    Only the packet send path is used (no AUI / no UI click).

    Live default order (2026-07-21): slot0 福州城, slot1 师门, slot2 上次死亡地点.

    @author by ak
    """
    log = log or (lambda _m: None)
    meta = PRESET_POINTS.get(preset_key)
    if not meta:
        return MapFlyResult(
            ok=False,
            action="fly_preset",
            message=f"未知预设 {preset_key}",
            error="bad_preset",
        )
    page = 0xFF if page_id is None else (int(page_id) & 0xFF)
    return fly_page_slot_packet(
        session,
        page,
        int(meta["slot"]),
        label=str(meta["label"]),
        wait_cooldown=False,
        respect_cooldown=True,
        stop_event=None,
        log=log,
    )


def fly_by_slot(
    session: GameAttachSession,
    slot: int,
    *,
    page_id: int = 0xFF,
    open_if_needed: bool = False,
    log: LogFn | None = None,
) -> MapFlyResult:
    """Fly using explicit page/slot packet. @author by ak"""
    log = log or (lambda _m: None)
    if open_if_needed:
        open_transmit_flag(session, log=log)
    return send_transmit_packet(
        session, ACTION_FLY, int(page_id), int(slot), None, log=log
    )


def _near_expected_pos(
    after: dict | None,
    expected_pos: list | tuple | None,
    expected_scene: int | None = None,
) -> bool:
    """True when the after-snapshot is within range of the expected pos/scene."""
    if not expected_pos or not isinstance(after, dict) or not after.get("ok"):
        return False
    try:
        ax = float(after.get("x") if after.get("x") is not None else after.get("pos", [None])[0])
        az = float(after.get("z") if after.get("z") is not None else after.get("pos", [None, None, None])[2])
        ex = float(expected_pos[0])
        ez = float(expected_pos[2] if len(expected_pos) > 2 else expected_pos[1])
        # xz plane; scale is game units
        dist2 = (ax - ex) * (ax - ex) + (az - ez) * (az - ez)
        if dist2 > (80.0 * 80.0):
            return False
        if expected_scene is not None:
            sc = after.get("scene_id")
            if sc is not None and int(sc) != int(expected_scene):
                return False
        return True
    except Exception:
        return False


def build_fly_page_slot_packet(page_id: int, slot: int) -> bytes:
    """
    Build the generic 0x86 fly packet: 86 00 02 | page(1B) | slot(1B) | len(1B=00).

    page: 0xFF 默认页（福州/师门/死亡），0..5 自定义页。
    slot: 0..9（槽1 → 0x00，槽10 → 0x09）。
    No AUI / no UI click — pure raw packet, sent via send_raw_packet.

    @author by ak
    """
    page = int(page_id) & 0xFF
    sl = int(slot) & 0xFF
    return bytes((0x86, 0x00, ACTION_FLY & 0xFF, page, sl, 0x00))


def fly_page_slot_packet(
    session: GameAttachSession,
    page_id: int,
    slot: int,
    *,
    label: str = "",
    wait_cooldown: bool = False,
    respect_cooldown: bool = True,
    expected_pos: list | tuple | None = None,
    expected_scene: int | None = None,
    stop_event=None,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Common production fly via the generic 0x86 raw packet (no AUI / no UI click).

    page_id: packet page — 0xFF 默认页，0..5 自定义页（0 是第一自定义页）。
    slot:    0..9 槽位（0-based）。
    Keeps client-side cooldown + scene/pos displacement verification.

    @author by ak
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return MapFlyResult(
            ok=False,
            action="fly_packet",
            message=f"远程不可用: {brsn}",
            error=brsn or "remote_blocked",
        )
    page = int(page_id) & 0xFF
    sl = int(slot)
    if not (0 <= sl < HOST_DEFAULT_FLY_SLOTS):
        return MapFlyResult(
            ok=False,
            action="fly_packet",
            message=f"slot={sl} out of range 0..{HOST_DEFAULT_FLY_SLOTS-1}",
            error="bad_slot",
        )
    pid = int(getattr(session, "pid", 0) or 0)
    disp = str(label or f"page={page}/0x{page:02X} slot={sl}")

    waited = 0.0
    if respect_cooldown and pid:
        left0 = fly_cooldown_remain(pid)
        if left0 > 0:
            if not wait_cooldown:
                return MapFlyResult(
                    ok=False,
                    action="fly_packet",
                    message=f"飞行冷却中 {left0:.1f}s",
                    error="cooldown",
                    detail={"remain_s": left0, "page": page, "slot": sl, "label": disp},
                )
            ok_wait, waited = _wait_fly_cooldown(
                pid, stop_event=stop_event, log=log, max_wait_s=max(left0 + 1.0, 8.0)
            )
            if not ok_wait:
                return MapFlyResult(
                    ok=False,
                    action="fly_packet",
                    message="飞行冷却等待取消/超时",
                    error="cooldown_wait",
                    detail={"waited_s": waited, "page": page, "slot": sl},
                )

    if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
        return MapFlyResult(
            ok=False, action="fly_packet", message="飞行已取消", error="cancelled"
        )

    payload = build_fly_page_slot_packet(page, sl)
    before = _snapshot_scene_pos(session, log=log)
    pr: MapFlyResult | None = None
    try:
        from app.core.game_send import send_raw_packet

        ret = send_raw_packet(pid, payload, timeout_ms=3000, log=log)
        pr = MapFlyResult(
            ok=bool(ret),
            action="packet",
            message=f"pkt 0x86 page={page}/0x{page:02X} slot={sl} ret={ret}",
            detail={
                "type": TRANSMIT_PKT_TYPE,
                "action": ACTION_FLY,
                "page": page,
                "slot": sl,
                "payload": payload.hex().upper(),
                "ret": int(ret or 0),
                "method": "raw_packet",
            },
        )
    except Exception as e:
        log(f"map_fly: fly_page_slot_packet send err: {e}")
        pr = MapFlyResult(ok=False, action="packet", message=str(e), error=str(e))

    time.sleep(0.9)
    after = _snapshot_scene_pos(session, fresh=True, log=lambda _m: None)
    moved = _moved(before, after)
    near = _near_expected_pos(after, expected_pos, expected_scene) if expected_pos else False

    if pid and (pr.ok or pr.error not in ("bad_slot",)):
        remain = mark_fly_cooldown(pid, DEFAULT_FLY_COOLDOWN_S)
    else:
        remain = fly_cooldown_remain(pid) if pid else 0.0

    ok = bool(pr.ok)
    detail = dict(pr.detail or {})
    detail.update(
        {
            "page": page,
            "logical_page": page,
            "packet_page": page,
            "slot": sl,
            "label": disp,
            "before": before,
            "after": after,
            "verified_move": bool(moved),
            "matched_expected": bool(near) if expected_pos else None,
            "expected_pos": list(expected_pos) if expected_pos else None,
            "expected_scene": expected_scene,
            "cd_waited_s": waited,
            "cd_remain_s": remain,
            "official": True,
            "method": "raw_packet",
        }
    )
    if ok and moved:
        msg = f"已飞「{disp}」(packet={page}/0x{page:02X} slot={sl})"
    elif ok:
        msg = (
            f"已对「{disp}」发包但未确认位移 "
            f"(packet={page}/0x{page:02X} slot={sl})"
        )
    else:
        msg = pr.message or f"飞「{disp}」失败"
    if moved:
        _note_fly_state_change(session, after, log=log)
    return MapFlyResult(
        ok=ok,
        action="fly_packet",
        message=msg,
        error=None if ok else (pr.error or "fly_failed"),
        detail=detail,
    )


def sign_current_to_slot(
    session: GameAttachSession,
    slot: int,
    *,
    page_id: int = 1,
    open_if_needed: bool = True,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Record current position into a custom slot (action=1).

    Required before flying an unrecorded custom point.
    @author by ak
    """
    log = log or (lambda _m: None)
    if open_if_needed:
        open_transmit_flag(session, log=log)
    return send_transmit_packet(
        session, ACTION_SIGN, int(page_id), int(slot), None, log=log
    )


def research_coord_fly(
    session: GameAttachSession,
    x: float,
    y: float,
    z: float,
    *,
    scene_id: int | None = None,
    try_probe: bool = False,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Coordinate-fly research entry (unrecorded direct xyz).

    @author by ak
    """
    log = log or (lambda _m: None)
    summary = research_summary()["coord_fly"]
    rec = research_summary()["record_then_fly_elsewhere"]
    detail = {
        "requested": {
            "x": float(x),
            "y": float(y),
            "z": float(z),
            "scene_id": scene_id,
        },
        "protocol": summary,
        "record_then_fly": rec,
        "packet_type": TRANSMIT_PKT_TYPE,
        "has_xyz_field": False,
        "recommendation": list(rec.get("legal_flow") or []),
    }
    log(
        "map_fly: coord research — 0x86 无 xyz；"
        f"请求=({x:.3f},{y:.3f},{z:.3f}) scene={scene_id}"
    )
    if try_probe:
        detail["probe"] = {
            "executed": False,
            "reason": "拒绝构造非法 xyz 飞行包；请用 sign+fly 合法链路实测",
        }
    return MapFlyResult(
        ok=True,
        action="coord_research",
        message=(
            "结论：不能未记录直飞坐标；"
            "只能「到点定位(action=1)」后，再在别处飞该槽(action=2)。"
        ),
        detail=detail,
    )


def research_record_then_fly_elsewhere(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Research: record non-current coords? / fly recorded point from elsewhere?

    Protocol evidence (xajh.exe):
      - Btn_Sign -> cc9310(action=1, page, slot, name=NULL)  NO xyz
      - Btn_Trans -> cc9310(action=2, page, slot, name=NULL) NO xyz
      - Destination for action=2 is server-side slot table filled at action=1
        using the player's authoritative current position.

    Therefore:
      - Record position that is NOT where you stand: NOT supported by packet
      - After a slot is recorded, fly it from any other place: YES (intended)

    @author by ak
    """
    log = log or (lambda _m: None)
    rec = research_summary()["record_then_fly_elsewhere"]
    detail = {
        "packet_layout": "u16=0x86 | u8 action | u8 page | u8 slot | u8 namelen | name?",
        "actions_seen": {
            "0": "delete custom point",
            "1": "sign/record CURRENT server pos into page/slot",
            "2": "fly to page/slot (server saved destination)",
            "4": "rename-like with name string (still no xyz)",
        },
        "callers_note_va": [
            "0xA4D586 action=1 Btn_Sign",
            "0xA4D3C4/0xA4D1D4 action=2 Btn_Trans",
            "0xA4D286 action=0 Btn_Delete",
            "0xA4E040 action=4 name",
        ],
        "record_non_current": {
            "feasible": False,
            "why": (
                "定位包只有 page/slot，没有目标坐标；"
                "坐标由服务器在收包时取角色权威当前位置写入槽位。"
            ),
            "forging_client_memory": (
                "即便改客户端槽位显示，飞行仍只发 page/slot；"
                "以服务器已存点为准，本地改显示无效。"
            ),
        },
        "fly_recorded_from_elsewhere": {
            "feasible": True,
            "why": (
                "飞行包同样无坐标，只带 page/slot；"
                "这是飞行旗自定义点的正常用法：A 点定位 → B 点飞回 A。"
            ),
            "test_steps": list(rec.get("legal_flow") or []),
        },
        "panel_probe": {
            "sign": "定位到槽(sign) — action=1, 建议自定义 page=1, 空 slot",
            "fly": "飞该槽 — action=2, 同一 page/slot（可在任意位置点）",
        },
    }
    log(
        "map_fly: record/fly research — "
        "不能定位非当前坐标；已定位槽可从任意地点 action=2 飞回"
    )
    return MapFlyResult(
        ok=True,
        action="record_fly_research",
        message=(
            "结论：①不能「记录非当前地坐标」；"
            "②可以「在 A 定位后，到别处再飞该记录点」。"
        ),
        detail=detail,
    )


def probe_sign_then_ready_fly(
    session: GameAttachSession,
    slot: int,
    *,
    page_id: int = 1,
    open_if_needed: bool = True,
    log: LogFn | None = None,
) -> MapFlyResult:
    """
    Live helper: sign CURRENT pos into custom page/slot, then report how to fly later.

    Does NOT auto-move away; user walks elsewhere and clicks 飞该槽.

    @author by ak
    """
    log = log or (lambda _m: None)
    from app.core.automove import read_scene_position

    pos_info: dict = {}
    try:
        sp = read_scene_position(session, log=lambda _m: None)
        if getattr(sp, "ok", False) and getattr(sp, "scene_pos", None):
            x, y, z = sp.scene_pos
            pos_info = {
                "scene_id": getattr(sp, "scene_id", None),
                "pos": [float(x), float(y), float(z)],
            }
    except Exception as e:
        pos_info = {"error": str(e)}

    sign_r = sign_current_to_slot(
        session,
        int(slot),
        page_id=int(page_id),
        open_if_needed=open_if_needed,
        log=log,
    )
    detail = {
        "signed_at": pos_info,
        "page": int(page_id) & 0xFF,
        "slot": int(slot) & 0xFF,
        "sign": sign_r.to_dict(),
        "next": (
            f"离开当前位置后，保持 page={int(page_id)&0xFF} slot={int(slot)} "
            f"点「飞该槽」(action=2)，验证异地飞回。"
        ),
    }
    ok = bool(sign_r.ok)
    return MapFlyResult(
        ok=ok,
        action="probe_sign",
        message=(
            f"已对 page={int(page_id)&0xFF} slot={int(slot)} 发送定位(action=1)。"
            f" 当前坐标={pos_info.get('pos')}。"
            " 请到其他地方后再点「飞该槽」。"
            if ok
            else f"定位失败: {sign_r.message}"
        ),
        error=None if ok else (sign_r.error or "sign_failed"),
        detail=detail,
    )


def click_ui_trans_slot(
    session: GameAttachSession,
    slot: int,
    *,
    open_if_needed: bool = True,
    log: LogFn | None = None,
) -> MapFlyResult:
    """Click Btn_Trans{slot+1} on Win_TransmitFlag. @author by ak"""
    log = log or (lambda _m: None)
    if open_if_needed:
        op = open_transmit_flag(session, log=log)
        if not op.ok and not is_transmit_flag_open(session, log=log)[0]:
            return op
    shown, dlg = is_transmit_flag_open(session, log=log)
    if not shown or not dlg:
        return MapFlyResult(
            ok=False, action="ui_click", message="界面未开", error="dlg_closed"
        )
    sl = int(slot)
    btn = f"Btn_Trans{sl + 1}"
    bp = get_aui_dlg_item_ptr(session, dlg, btn, log=log)
    if not bp:
        return MapFlyResult(
            ok=False, action="ui_click", message=f"无控件 {btn}", error="no_ctrl"
        )
    br = read_aui_ctrl_rect(session, bp, name=btn, log=log)
    if not br or not getattr(br, "ok", False):
        return MapFlyResult(
            ok=False, action="ui_click", message=f"{btn} 无矩形", error="no_rect"
        )
    cx, cy = br.center()
    click_client_bg(session, cx, cy, log=log)
    return MapFlyResult(
        ok=True,
        action="ui_click",
        message=f"点击 {btn} @({cx},{cy})",
        detail={"btn": btn, "xy": (cx, cy), "dlg": dlg},
    )
