# -*- coding: utf-8 -*-
"""
Package / grocery helpers: list bag items, use item, sell stack, read money.

Live-verified (2026-07-16 xajh):
  - plg::GetPackageCount / GetPackage / UseItemInPackage exports
  - package: cap@+0x20, size@+0x24, item_arr@+0x2C
  - item: tid@+0x0C, count@+0x70, name via item+0xE8 (inline/object; reject module-image garbage)
  - money: NOTE_VA_GET_MONEY_BY_PKG(package_index) -> u64 at package+0x10
  - sell: NOTE_VA_SELL_FROM_PACKAGE(pack, slot, count) -> c2s 0xE / subtype 2

@author by ak
"""
from __future__ import annotations

import ctypes
import re
import struct
from ctypes import wintypes
from dataclasses import asdict, dataclass
from typing import Callable

from app.core.client_build import session_supports
from app.core.remote_runtime import (
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    _open_process,
    _rpm,
    _wpm,
    remote_call_cdecl_x86,
    remote_module_base,
    remote_call_stdcall_x86 as _runtime_stdcall,
    remote_call_stdcall_x86_ret64 as _runtime_stdcall_i64,
)
from app.core.game_attach import GameAttachSession
from app.core.plg_exports import (
    EXPORT_BUY_ITEM,
    EXPORT_GET_PACKAGE,
    EXPORT_GET_PACKAGE_COUNT,
    EXPORT_NPC_SAY_HELLO,
    EXPORT_USE_ITEM_IN_PACKAGE,
    NOTE_VA_GET_MONEY2_BY_PKG,
    NOTE_VA_GET_MONEY_BY_PKG,
    NOTE_VA_SELL_FROM_PACKAGE,
    NOTE_VA_BUY_FROM_SHOP_SLOT,
    NOTE_VA_TASK_IFACE_OR_PKG_ROOT,
    DEFAULT_IMAGE_BASE,
    find_xajh_exe,
    note_va_to_live,
    resolve_export_rva,
)

LogFn = Callable[[str], None]


def _pid_blocked(session: GameAttachSession | int | None) -> tuple[bool, str]:
    """True when SafeDispatch/remote gate blocks this game pid. @author by ak"""
    try:
        from app.core.safe_dispatch import session_blocked

        return session_blocked(session)
    except Exception:
        return False, ""


# Inventory bag (杂货/使用/出售). Live 2026-07-16:
#   GetPackage(0) = equip/fashion (神器帽/衣/戒…), cap~96
#   GetPackage(2) = main bag, cap 160~600  ← grocery default (live may be 600)
# UseItemInPackage notes also push pack=2 for 物品栏.
DEFAULT_PACKAGE_INDEX = 2
# Live IVTRTYPE (script HelpSystem.IVTRTYPE_ENUM):
#   2=PACK 主包, 3=PACK1 扩展1, 4=PACK2 扩展2, 11=TRASHBOX 仓库
PACKAGE_INDEX_PACK1 = 3
PACKAGE_INDEX_PACK2 = 4
PACKAGE_INDEX_WAREHOUSE = 11
DEFAULT_EXCHANGE_PACKAGE_INDEXES = (2, 3, 4, 11)
# Carry packs only (no warehouse) — for free-slot planning near NPC trade
DEFAULT_CARRY_PACKAGE_INDEXES = (2, 3, 4)
# Money type index used by game UI gold checks (push 2; call GetMoney) — NOT a bag index.
DEFAULT_MONEY_PACKAGE_INDEX = 2
# 1 金 = 100 银 = 10000 铜 (Perfect World money unit).
GOLD_UNIT = 10000
SILVER_UNIT = 100
# 卖满金保底：按「金」填，默认 44990 金；内部一律转铜比较。
DEFAULT_FULL_GOLD_MIN_GOLD = 44990
DEFAULT_FULL_GOLD_MIN = DEFAULT_FULL_GOLD_MIN_GOLD * GOLD_UNIT
# 白云熊胆丸 NPC 卖价：1 个 = 10 金。
DEFAULT_REVIVE_PILL_GOLD = 10
DEFAULT_REVIVE_PILL_PRICE = DEFAULT_REVIVE_PILL_GOLD * GOLD_UNIT
# 持续守护：金充足时轮询间隔 / 无丹时重试间隔（秒）
DEFAULT_FULL_GOLD_POLL_S = 2.0
DEFAULT_FULL_GOLD_RETRY_S = 3.0
# Live full name from data.pck templ_id=44374; user may say 熊丹丸.
DEFAULT_REVIVE_PILL_NAME = "白云熊胆丸"
DEFAULT_REVIVE_PILL_PREFIX = "白云"
# Substring tokens that identify sellable revive-style pills.
DEFAULT_REVIVE_PILL_TOKENS = (
    "白云熊胆丸",
    "白云熊丹丸",
    "熊胆丸",
    "熊丹丸",
    "复活丹",
)
# data.pck templ_id for 白云熊胆丸 (卖满金).
DEFAULT_REVIVE_PILL_TID = 44374

# package object layout
PKG_CAP_OFF = 0x20
PKG_SIZE_OFF = 0x24
PKG_ARR_OFF = 0x2C
# item object layout (ref 背包.md: tid@+0x0C, count@+0x70)
ITEM_COUNT_OFF = 0x70
ITEM_TID_OFF = 0x0C
# Live 2026-07-22 sample: item+0x6C is 0/1 bind-ish flag for 快乐兑换丹
# (0=非绑, 1=绑定). Other items may reuse the field differently.
ITEM_BIND_OFF = 0x6C
# Live 2026-07-19 bag memory:
#   equip/named gear: display name via +0xE8 (ptr or inline utf-16)
#   consumables:      display name ptr at +0xD8 (e.g. 50%血药 / 活力药)
#   +0xE8 for consumables often lands in main module (vtable) — skip
ITEM_NAME_OFF = 0xD8
ITEM_TMPL_OFF = 0xE8
ITEM_READ_SIZE = 0x100
MAX_PACKAGE_SLOTS = 640

# Pure RPM inventory path, verified live 2026-08-11:
#   root = *[image_base + 0x11282D8]
#   host = *(*[root + 0x24] + 0x90)
#   package_mgr = *[host + 0x08]
#   package = *[package_mgr + 0x10 + package_index * 4]
# No game function call is needed once the client has refreshed its bags.
NOTE_VA_GAME_ROOT_GLOBAL = 0x015282D8
PKG_ROOT_MID_OFF = 0x24
PKG_ROOT_HOST_OFF = 0x90
PKG_HOST_MANAGER_OFF = 0x08
PKG_MANAGER_FIRST_PACKAGE_OFF = 0x10

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_EXECUTE_READWRITE = 0x40
WAIT_OBJECT_0 = 0


@dataclass
class PackageItem:
    """
    One slot inside a package.

    bind: live item+0x6C when 0/1 (0=非绑, 1=绑定 for 快乐兑换丹).
          -1 means unread / not applicable.

    @author by ak
    """

    package: int
    slot: int
    ptr: int
    tid: int = 0
    count: int = 0
    name: str = ""
    bind: int = -1

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_bound(self) -> bool:
        """True when bind flag is clearly 1. @author by ak"""
        return int(self.bind) == 1

    @property
    def is_unbound(self) -> bool:
        """True when bind flag is clearly 0 (非绑). @author by ak"""
        return int(self.bind) == 0


@dataclass
class PackageActionResult:
    """
    Outcome of use/sell/money read.

    money: primary copper used by 卖满金 (bound/bind 绑定币).
    money_trade: optional unbound/trade copper for display only.

    @author by ak
    """

    ok: bool
    action: str
    message: str = ""
    item: dict | None = None
    money: int | None = None
    money_trade: int | None = None
    ret: int | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _safe_rpm(h: int, addr: int, size: int) -> bytes:
    """
    Read process memory; return empty bytes on failure.

    @author by ak
    """
    if not addr or size <= 0:
        return b""
    try:
        return _rpm(h, int(addr) & 0xFFFFFFFF, int(size))
    except Exception:
        return b""


def _open_read_only_process(pid: int) -> int:
    """Open a handle that cannot allocate, write, or create a thread."""
    handle = kernel32.OpenProcess(
        int(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ), False, int(pid)
    )
    if not handle:
        raise OSError(f"OpenProcess(read-only) failed err={ctypes.get_last_error()}")
    return int(handle)


def _rpm_u32(h: int, address: int) -> int:
    raw = _safe_rpm(h, int(address), 4)
    if len(raw) != 4:
        return 0
    return int(struct.unpack_from("<I", raw, 0)[0])


def _sane_user_ptr(value: int) -> int:
    """Accept the full WOW64/LARGEADDRESSAWARE 32-bit user address range.

    The client can allocate package-manager objects above 0x80000000. RPM
    remains the readability authority; only null/low pointers and the top
    guard region are rejected here.
    """
    ptr = int(value or 0) & 0xFFFFFFFF
    return ptr if 0x10000 <= ptr < 0xFFFF0000 else 0


def _module_base_for_rpm(session: GameAttachSession) -> int:
    """Use session metadata or Toolhelp module enumeration, never a game call."""
    base = int(getattr(session, "module_base", 0) or 0) & 0xFFFFFFFF
    if base:
        return base
    pid = int(getattr(session, "pid", 0) or 0)
    if pid <= 0:
        return 0
    try:
        return int(remote_module_base(pid, "xajh.exe") or 0) & 0xFFFFFFFF
    except Exception:
        return 0


def _resolve_export_va(session: GameAttachSession, export_name: str) -> int:
    """
    module_base + export RVA.

    @author by ak
    """
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        raise RuntimeError("session has no module_base")
    pe = find_xajh_exe(getattr(session, "exe_path", None))
    if pe is None:
        raise RuntimeError("cannot locate xajh.exe")
    rva = resolve_export_rva(pe, export_name)
    if rva is None:
        raise RuntimeError(f"export not found: {export_name}")
    return base + int(rva)


def _note_va(session: GameAttachSession, note_va: int) -> int:
    """
    Map notes preferred VA to live VA.

    @author by ak
    """
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        raise RuntimeError("session has no module_base")
    required = {
        int(NOTE_VA_TASK_IFACE_OR_PKG_ROOT): "package.read",
        int(NOTE_VA_GET_MONEY_BY_PKG): "package.money",
        int(NOTE_VA_SELL_FROM_PACKAGE): "package.sell",
    }.get(int(note_va))
    if required and not session_supports(session, required):
        raise RuntimeError(
            f"unknown client build; fixed package symbol requires {required}"
        )
    return note_va_to_live(base, int(note_va))


def _clean_item_name(raw: str) -> str:
    """
    Strip Perfect World color/control prefix from item wchar name.

    Live forms (2026-07-16):
      - private-use + control + "王母玉胜.十阶"
      - control only + "神器帽.一百"
      - color-glyph + control + "真·淑雯叆叇-80"

    @author by ak
    """
    s = raw or ""
    # drop private-use / non-printable marks at front
    while s:
        o = ord(s[0])
        if o < 0x20 or 0xE000 <= o <= 0xF8FF:
            s = s[1:]
            continue
        if not s[0].isprintable():
            s = s[1:]
            continue
        break
    # color glyph + next control byte (common bag equip-like stacks)
    if len(s) >= 2 and 0x01 <= ord(s[1]) <= 0x1F:
        s = s[2:]
        while s and (ord(s[0]) < 0x20 or 0xE000 <= ord(s[0]) <= 0xF8FF):
            s = s[1:]
    s = "".join(ch for ch in s if ch.isprintable() or ch in ("·", "—", "-"))
    return s.strip()


def _is_good_item_name(name: str) -> bool:
    """
    Reject random-memory / mis-decoded utf-16 garbage.

    @author by ak
    """
    s = (name or "").strip()
    if len(s) < 2 or len(s) > 32:
        return False
    # Hangul / full-width latin dumps / private use
    for c in s:
        o = ord(c)
        if 0xAC00 <= o <= 0xD7AF:  # Hangul
            return False
        if 0xE000 <= o <= 0xF8FF:
            return False
        if o > 0xFF00:  # halfwidth/fullwidth forms often from code dumps
            return False
        if 0x0400 <= o <= 0x04FF:  # Cyrillic
            return False
    cjk = sum(1 for c in s if "一" <= c <= "鿿")
    if cjk < 2:
        return False
    # pure short CJK (2) from random mem is common false positive
    if len(s) == 2 and cjk == 2:
        return False
    ok_extra = set("·.—-_()（）[]【】+×xX%*·、0123456789 ")
    bad = 0
    for c in s:
        o = ord(c)
        if "一" <= c <= "鿿" or c.isalnum() or c in ok_extra:
            continue
        if 0x3000 <= o <= 0x303F:  # CJK punctuation
            continue
        bad += 1
    if bad > 0:
        return False
    # CJK should dominate non-digit content
    non_digit = sum(1 for c in s if not c.isdigit() and c not in "·.—-_()（）[]【】+×xX% ")
    if non_digit and cjk / non_digit < 0.5:
        return False
    return True


def _wstr_at(h: int, addr: int, max_chars: int = 40) -> str:
    """
    Read null-terminated utf-16le at addr; return cleaned name or "".

    @author by ak
    """
    if not addr or addr < 0x10000 or addr > 0x7FFE0000:
        return ""
    tb = _safe_rpm(h, int(addr) & 0xFFFFFFFF, max_chars * 2)
    if len(tb) < 4:
        return ""
    try:
        raw = tb.decode("utf-16le", "ignore").split("\0")[0]
    except Exception:
        return ""
    # require early terminator / short real name — long dumps are garbage
    if len(raw) > 40:
        return ""
    name = _clean_item_name(raw)
    return name if _is_good_item_name(name) else ""


def _ptr_name(
    h: int,
    ptr: int,
    *,
    module_base: int = 0,
    module_size: int = 0,
    skip_module: bool = True,
    try_header_skip: bool = False,
) -> str:
    """
    Resolve utf-16 name at ptr. Skip module image.

    Some consumable name blobs are {u32 hdr; wchar name[]} — try +4 when
    direct read fails (live: 首饰升阶*100 / 快乐兑换丹*100).

    @author by ak
    """
    if not ptr or ptr < 0x10000 or ptr > 0x7FFE0000:
        return ""
    if (
        skip_module
        and module_base
        and module_size
        and module_base <= int(ptr) < module_base + module_size
    ):
        return ""
    n = _wstr_at(h, int(ptr))
    if n:
        return n
    if try_header_skip:
        n = _wstr_at(h, int(ptr) + 4)
        if n:
            return n
    return ""


def _read_item_name(
    h: int, item_ptr: int, *, module_base: int = 0, module_size: int = 0
) -> str:
    """
    Read bag item display name from live memory.

    Priority (verified 2026-07-19 live bag):
      1) item+0xE8 — equip / renamed gear (skip main-module vtable)
      2) item+0xD8 — consumable name (direct or +4 after u32 header)
      3) item+0x5C / +0x144 / +0x14C — secondary labels when primary empty

    @author by ak
    """
    ib = _safe_rpm(h, item_ptr, max(ITEM_READ_SIZE, 0x158))
    if len(ib) < ITEM_NAME_OFF + 4:
        return ""
    # 1) equip path
    if len(ib) >= ITEM_TMPL_OFF + 4:
        e8 = struct.unpack_from("<I", ib, ITEM_TMPL_OFF)[0]
        n = _ptr_name(
            h, e8, module_base=module_base, module_size=module_size, skip_module=True
        )
        if n:
            return n
    # 2) consumable primary at +0xD8
    d8 = struct.unpack_from("<I", ib, ITEM_NAME_OFF)[0]
    n = _ptr_name(
        h,
        d8,
        module_base=module_base,
        module_size=module_size,
        skip_module=True,
        try_header_skip=True,
    )
    if n:
        return n
    # 3) secondary fields seen on remaining stacks
    for off in (0x5C, 0x144, 0x14C):
        if len(ib) < off + 4:
            continue
        p = struct.unpack_from("<I", ib, off)[0]
        n = _ptr_name(
            h,
            p,
            module_base=module_base,
            module_size=module_size,
            skip_module=True,
            try_header_skip=True,
        )
        if n:
            return n
    return ""


def _read_item(
    h: int,
    package: int,
    slot: int,
    item_ptr: int,
    *,
    module_base: int = 0,
    module_size: int = 0,
) -> PackageItem | None:
    """
    Build PackageItem from live pointer.

    Name: live inline utf-16 when safe; else app/data/item_names.json tid map;
    else tid=N. Live good names are learned for the session.

    @author by ak
    """
    if not item_ptr:
        return None
    ib = _safe_rpm(h, item_ptr, ITEM_READ_SIZE)
    if len(ib) < ITEM_TID_OFF + 4:
        return None
    tid = struct.unpack_from("<I", ib, ITEM_TID_OFF)[0]
    count = 1
    if len(ib) >= ITEM_COUNT_OFF + 4:
        c = struct.unpack_from("<I", ib, ITEM_COUNT_OFF)[0]
        if 1 <= c <= 99999:
            count = int(c)
    bind = -1
    if len(ib) >= ITEM_BIND_OFF + 4:
        b = struct.unpack_from("<I", ib, ITEM_BIND_OFF)[0]
        # Only treat classic 0/1 as bind flag; other values left as-is for callers.
        if b in (0, 1):
            bind = int(b)
        else:
            bind = int(b)
    live = _read_item_name(
        h, item_ptr, module_base=module_base, module_size=module_size
    )
    try:
        from app.core.item_names import resolve_item_display_name

        name = resolve_item_display_name(int(tid), live, learn=True)
    except Exception:
        name = live or (f"tid={int(tid)}" if tid else "")
    return PackageItem(
        package=int(package),
        slot=int(slot),
        ptr=int(item_ptr) & 0xFFFFFFFF,
        tid=int(tid),
        count=int(count),
        name=name,
        bind=int(bind),
    )


def get_package_count(session: GameAttachSession) -> int:
    """
    plg::GetPackageCount().

    @author by ak
    """
    va = _resolve_export_va(session, EXPORT_GET_PACKAGE_COUNT)
    ret = remote_call_cdecl_x86(int(session.pid), va, [])
    return max(0, int(ret or 0))


def get_package_ptr(session: GameAttachSession, index: int) -> int:
    """
    plg::GetPackage(index) -> package*.

    @author by ak
    """
    va = _resolve_export_va(session, EXPORT_GET_PACKAGE)
    ret = remote_call_cdecl_x86(int(session.pid), va, [int(index)])
    return int(ret or 0) & 0xFFFFFFFF


def read_package_slot(
    session: GameAttachSession,
    package_index: int,
    slot: int,
    *,
    log: LogFn | None = None,
    with_name: bool = False,
) -> PackageItem | None:
    """
    Fast single-slot read (no full bag scan).

    For open-first fire loops: only tid/count/ptr needed.

    @author by ak
    """
    log = log or (lambda _m: None)
    pack = int(package_index)
    sl = int(slot)
    if sl < 0:
        return None
    pid = int(session.pid)
    mod_base = int(getattr(session, "module_base", 0) or 0)
    mod_size = int(getattr(session, "module_size", 0) or 0)
    h = _open_process(pid)
    try:
        pkg = get_package_ptr(session, pack)
        if not pkg:
            return None
        raw = _safe_rpm(h, pkg, 0x40)
        if len(raw) < PKG_ARR_OFF + 4:
            return None
        cap = struct.unpack_from("<I", raw, PKG_CAP_OFF)[0]
        size = struct.unpack_from("<I", raw, PKG_SIZE_OFF)[0]
        arr = struct.unpack_from("<I", raw, PKG_ARR_OFF)[0]
        nslots = int(cap)
        if nslots <= 0 and 0 < int(size) <= MAX_PACKAGE_SLOTS:
            nslots = int(size)
        if not arr or nslots <= 0 or sl >= nslots:
            return None
        b = _safe_rpm(h, arr + sl * 4, 4)
        if len(b) < 4:
            return None
        ip = struct.unpack_from("<I", b, 0)[0]
        if not ip:
            return None
        if with_name:
            return _read_item(
                h,
                pack,
                sl,
                ip,
                module_base=mod_base,
                module_size=mod_size,
            )
        # minimal: tid@+0x0C count@+0x70 only
        ib = _safe_rpm(h, ip, max(ITEM_COUNT_OFF + 4, ITEM_TID_OFF + 4))
        if len(ib) < ITEM_TID_OFF + 4:
            return None
        tid = struct.unpack_from("<I", ib, ITEM_TID_OFF)[0]
        count = 1
        if len(ib) >= ITEM_COUNT_OFF + 4:
            c = struct.unpack_from("<I", ib, ITEM_COUNT_OFF)[0]
            if 1 <= c <= 99999:
                count = int(c)
        return PackageItem(
            package=pack,
            slot=sl,
            ptr=int(ip) & 0xFFFFFFFF,
            tid=int(tid),
            count=int(count),
            name="",
            bind=-1,
        )
    finally:
        try:
            kernel32.CloseHandle(wintypes.HANDLE(h))
        except Exception:
            pass


def list_package_items_rpm(
    session: GameAttachSession,
    package_index: int = DEFAULT_PACKAGE_INDEX,
    *,
    log: LogFn | None = None,
) -> list[PackageItem]:
    """Enumerate one package through a verified ReadProcessMemory chain only.

    This deliberately does not call ``GetPackage`` or any other game export.
    The client must already have refreshed the inventory data before use.
    """
    log = log or (lambda _m: None)
    pid = int(getattr(session, "pid", 0) or 0)
    index = int(package_index)
    if pid <= 0 or index < 0 or index > 63:
        return []
    base = _module_base_for_rpm(session)
    if not base:
        raise RuntimeError("RPM package read has no xajh.exe module_base")

    h = _open_read_only_process(pid)
    try:
        root_addr = int(base) + (NOTE_VA_GAME_ROOT_GLOBAL - DEFAULT_IMAGE_BASE)
        root = _sane_user_ptr(_rpm_u32(h, root_addr))
        mid = _sane_user_ptr(_rpm_u32(h, root + PKG_ROOT_MID_OFF)) if root else 0
        host = _sane_user_ptr(_rpm_u32(h, mid + PKG_ROOT_HOST_OFF)) if mid else 0
        manager = _sane_user_ptr(_rpm_u32(h, host + PKG_HOST_MANAGER_OFF)) if host else 0
        package = (
            _sane_user_ptr(
                _rpm_u32(h, manager + PKG_MANAGER_FIRST_PACKAGE_OFF + index * 4)
            )
            if manager
            else 0
        )
        if not package:
            raise RuntimeError(f"RPM package chain has no package index={index}")

        cap = _rpm_u32(h, package + PKG_CAP_OFF)
        size = _rpm_u32(h, package + PKG_SIZE_OFF)
        item_array = _sane_user_ptr(_rpm_u32(h, package + PKG_ARR_OFF))
        slots = int(cap)
        if slots <= 0 and 0 < int(size) <= MAX_PACKAGE_SLOTS:
            slots = int(size)
        if not item_array or not (0 < slots <= MAX_PACKAGE_SLOTS):
            raise RuntimeError(
                f"RPM package layout invalid index={index} cap={cap} size={size}"
            )

        out: list[PackageItem] = []
        for slot in range(slots):
            item_ptr = _sane_user_ptr(_rpm_u32(h, item_array + slot * 4))
            if not item_ptr:
                continue
            tid = _rpm_u32(h, item_ptr + ITEM_TID_OFF)
            count_raw = _rpm_u32(h, item_ptr + ITEM_COUNT_OFF)
            count = int(count_raw) if 1 <= count_raw <= 99999 else 1
            if tid:
                out.append(
                    PackageItem(
                        package=index,
                        slot=slot,
                        ptr=item_ptr,
                        tid=tid,
                        count=count,
                    )
                )
        return out
    finally:
        try:
            kernel32.CloseHandle(wintypes.HANDLE(h))
        except Exception:
            pass


def list_package_items(
    session: GameAttachSession,
    package_index: int = DEFAULT_PACKAGE_INDEX,
    *,
    log: LogFn | None = None,
) -> list[PackageItem]:
    """
    Enumerate non-empty slots in one package.

    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(session.pid)
    mod_base = int(getattr(session, "module_base", 0) or 0)
    mod_size = int(getattr(session, "module_size", 0) or 0)
    h = _open_process(pid)
    try:
        pkg = get_package_ptr(session, int(package_index))
        if not pkg:
            log(f"package_api: GetPackage({package_index})=null")
            return []
        raw = _safe_rpm(h, pkg, 0x40)
        if len(raw) < PKG_ARR_OFF + 4:
            return []
        cap = struct.unpack_from("<I", raw, PKG_CAP_OFF)[0]
        size = struct.unpack_from("<I", raw, PKG_SIZE_OFF)[0]
        arr = struct.unpack_from("<I", raw, PKG_ARR_OFF)[0]
        # 扩展包 PACK1/PACK2 live 可能 cap=0 但 size=100
        nslots = int(cap)
        if nslots <= 0 and 0 < int(size) <= MAX_PACKAGE_SLOTS:
            nslots = int(size)
        if not arr or nslots <= 0 or nslots > MAX_PACKAGE_SLOTS:
            log(f"package_api: bad package cap={cap} size={size} arr=0x{arr:X}")
            return []
        out: list[PackageItem] = []
        for slot in range(int(nslots)):
            b = _safe_rpm(h, arr + slot * 4, 4)
            if len(b) < 4:
                continue
            ip = struct.unpack_from("<I", b, 0)[0]
            if not ip:
                continue
            item = _read_item(
                h,
                int(package_index),
                slot,
                ip,
                module_base=mod_base,
                module_size=mod_size,
            )
            if item is not None:
                out.append(item)
        # 成功列背包极高频（刷金/换牌每秒多次），默认不落盘；失败路径仍会 log
        return out
    finally:
        try:
            kernel32.CloseHandle(wintypes.HANDLE(h))
        except Exception:
            pass


def find_items_by_name(
    session: GameAttachSession,
    name_substr: str,
    *,
    package_index: int = DEFAULT_PACKAGE_INDEX,
    require_prefix: str | None = None,
    log: LogFn | None = None,
) -> list[PackageItem]:
    """
    Filter package items by name substring (and optional prefix).

    @author by ak
    """
    key = (name_substr or "").strip()
    pref = (require_prefix or "").strip()
    items = list_package_items(session, package_index, log=log)
    out: list[PackageItem] = []
    for it in items:
        n = it.name or ""
        if key and key not in n:
            continue
        if pref and not n.startswith(pref) and pref not in n:
            continue
        out.append(it)
    return out


def _use_item_native_thiscall(
    session: GameAttachSession,
    package_index: int,
    slot: int,
    *,
    log: LogFn | None = None,
) -> PackageActionResult:
    """
    Fallback: host+0x1A84 inv thiscall UseItem(pack, slot, flag=1).

    Notes used call 0x743D60; current profile NOTE_VA_USE_ITEM_NATIVE=0x74BF50.
    External CRT may still fail on some items; ok judged by bag delta when possible.

    @author by ak
    """
    from app.core.plg_exports import HOST_SIDE_INV_OFF, NOTE_VA_USE_ITEM_NATIVE
    from app.core.plg_ui import get_host_player_ptr
    from app.core.remote_runtime import remote_call_thiscall_x86, remote_read_bytes

    log = log or (lambda _m: None)
    host = int(get_host_player_ptr(session, log=log) or 0)
    if not host:
        return PackageActionResult(
            ok=False, action="use", message="GetHostPlayer null", error="no host"
        )
    raw = remote_read_bytes(int(session.pid), host + HOST_SIDE_INV_OFF, 4)
    if len(raw) < 4:
        return PackageActionResult(
            ok=False, action="use", message="inv this unreadable", error="no inv"
        )
    inv = struct.unpack_from("<I", raw, 0)[0]
    if not inv or inv < 0x10000:
        return PackageActionResult(
            ok=False,
            action="use",
            message=f"inv this null host=0x{host:X}",
            error="no inv",
        )
    va = _note_va(session, NOTE_VA_USE_ITEM_NATIVE)
    ret = int(
        remote_call_thiscall_x86(
            int(session.pid),
            va,
            inv,
            [int(package_index), int(slot), 1],
            timeout_ms=4000,
        )
    )
    ok = bool(int(ret) & 0xFF)
    msg = (
        f"使用(native) 包{package_index} 槽{slot} "
        f"inv=0x{inv:X} ret={ret}"
    )
    log(f"package_api: {msg}")
    return PackageActionResult(ok=ok, action="use", message=msg, ret=int(ret))


def use_item_in_package(
    session: GameAttachSession,
    package_index: int,
    slot: int,
    *,
    log: LogFn | None = None,
    prefer_bridge: bool = True,
    bag_wait_s: float = 0.25,
    verify_bag: bool = True,
    quiet: bool = False,
    bridge=None,
) -> PackageActionResult:
    """
    Use bag item on the game UI thread when possible.

    Order:
      1) bridge CMD_USE_ITEM_IN_PACKAGE (UI timer = real bag right-click path)
      2) plg export via CRT (legacy fallback)
      3) native thiscall fallback

    verify_bag=True: success requires bag delta (slower; full/partial list).
    verify_bag=False: fire-and-forget; ok follows bridge/export ret (fast open-first).

    @author by ak
    """
    import time as _time

    log = log or (lambda _m: None)

    def _emit(msg: str) -> None:
        if not quiet:
            log(msg)

    pack = int(package_index)
    sl = int(slot)
    before_cnt = None
    before_tid = None
    if verify_bag:
        try:
            it0 = read_package_slot(session, pack, sl, log=lambda _m: None)
            if it0 is not None:
                before_cnt = int(it0.count or 0)
                before_tid = int(it0.tid or 0)
        except Exception:
            pass

    last = PackageActionResult(ok=False, action="use", message="no attempt")
    used_via = ""

    # 1) UI-thread bridge (preferred)
    if prefer_bridge:
        try:
            br = bridge
            if br is None:
                from app.core.xajh_bridge import ensure_bridge

                hwnd = int(getattr(session, "hwnd", 0) or 0)
                br = ensure_bridge(
                    int(session.pid),
                    log=lambda _m: None,
                    inject_if_needed=False,
                    hwnd=hwnd or None,
                )
            if br is not None:
                hwnd = int(getattr(session, "hwnd", 0) or 0)
                br_res = br.use_item_in_package(
                    pack, sl, hwnd=hwnd or None, timeout_ms=1500
                )
                ret = int(br_res.ret or 0) if br_res.ret is not None else 0
                ok_ret = bool(br_res.ok and (int(ret) & 0xFF))
                if br_res.ok:
                    used_via = "bridge"
                last = PackageActionResult(
                    ok=ok_ret,
                    action="use",
                    message=(
                        f"使用(bridge) 包{pack} 槽{sl} ret={ret}"
                        + (f" note={(br_res.note or '')[:40]}" if not quiet else "")
                    ),
                    ret=ret,
                    error=None if br_res.ok else (br_res.error or "bridge use fail"),
                )
                _emit(f"package_api: {last.message}")
            else:
                _emit("package_api: use bridge unavailable, fallback CRT")
        except Exception as e:
            _emit(f"package_api: use bridge failed: {e}")

    # 2/3) CRT only if bridge missing
    if used_via != "bridge":
        try:
            va = _resolve_export_va(session, EXPORT_USE_ITEM_IN_PACKAGE)
            ret = remote_call_cdecl_x86(int(session.pid), va, [pack, sl])
            ok = bool(int(ret or 0) & 0xFF)
            used_via = "export"
            last = PackageActionResult(
                ok=ok,
                action="use",
                message=f"使用 包{pack} 槽{sl} ret={ret}",
                ret=int(ret or 0),
            )
            _emit(f"package_api: {last.message}")
        except Exception as e:
            last = PackageActionResult(
                ok=False, action="use", message=str(e), error=str(e)
            )
            _emit(f"package_api: use export failed: {e}")

        if not last.ok:
            try:
                last = _use_item_native_thiscall(session, pack, sl, log=log)
                used_via = "native"
            except Exception as e:
                _emit(f"package_api: use native failed: {e}")
                if last.error is None:
                    last = PackageActionResult(
                        ok=False, action="use", message=str(e), error=str(e)
                    )

    if not verify_bag:
        # fire path: trust call ret; open-first cadence owns the sleep
        if last.ok:
            invalidate_bag_cache(session)
        return last

    # bag delta confirm (single-slot, short poll)
    try:
        wait = max(0.0, float(bag_wait_s))
        deadline = _time.monotonic() + wait
        confirmed = False
        while True:
            after = read_package_slot(session, pack, sl, log=lambda _m: None)
            if before_cnt is not None:
                if after is None:
                    last.ok = True
                    last.message = (
                        f"{last.message or 'use'} bag=empty via={used_via or '?'}"
                    )
                    confirmed = True
                    break
                ac = int(after.count or 0)
                at = int(after.tid or 0)
                if ac < before_cnt or (before_tid and at and at != before_tid):
                    last.ok = True
                    last.message = (
                        f"{last.message or 'use'} bag {before_cnt}->{ac} "
                        f"via={used_via or '?'}"
                    )
                    confirmed = True
                    break
            if _time.monotonic() >= deadline:
                break
            _time.sleep(0.02)

        if before_cnt is not None and not confirmed:
            after = read_package_slot(session, pack, sl, log=lambda _m: None)
            ac = int(after.count or 0) if after is not None else 0
            at = int(after.tid or 0) if after is not None else 0
            last.ok = False
            last.message = (
                f"{last.message or 'use'} bag unchanged "
                f"count={ac} tid={at} via={used_via or '?'}"
            )
            _emit(f"package_api: {last.message}")
        elif confirmed:
            _emit(f"package_api: {last.message}")
    except Exception as e:
        _emit(f"package_api: use bag-check: {e}")
    if last.ok:
        invalidate_bag_cache(session)
    return last



# Legacy local CRT stubs removed — use remote_runtime via aliases below.

# Compatibility names now route through the serialized shared runtime.
_remote_stdcall = _runtime_stdcall
_remote_stdcall_i64 = _runtime_stdcall_i64


# Sane host money upper bound (copper). 100万金 = 1e10 copper; keep 2e9 for u32.
_MAX_SANE_MONEY_COPPER = 2_000_000_000


def _sane_money(v: int | None) -> int | None:
    """Return copper amount if plausible host money, else None."""
    if v is None:
        return None
    try:
        n = int(v)
    except Exception:
        return None
    if 0 <= n < _MAX_SANE_MONEY_COPPER:
        return n
    return None


def _money_pair_from_package(
    session: GameAttachSession, package_index: int
) -> tuple[int | None, int | None, str]:
    """
    Read bind + trade copper from GetPackage(index).

    Live 2026-07-19 (pid=<PID> / rich char earlier):
      package+0x10 = GetMoney  = 可交易/非绑 (trade)
      package+0x18 = GetMoney2 = 绑定币 (bind)  ← 卖满金盯这个

    Returns (bind, trade, source_tag).

    @author by ak
    """
    h = _open_process(int(session.pid))
    try:
        pkg = get_package_ptr(session, int(package_index))
        if not pkg:
            return None, None, "none"
        raw = _safe_rpm(h, int(pkg) & 0xFFFFFFFF, 0x20)
        if len(raw) < 0x1C:
            return None, None, "none"
        lo10 = struct.unpack_from("<I", raw, 0x10)[0]
        lo18 = struct.unpack_from("<I", raw, 0x18)[0]
        trade = _sane_money(int(lo10))
        bind = _sane_money(int(lo18))
        # allow 0 as valid amount
        if trade is None:
            trade = 0 if 0 <= int(lo10) < _MAX_SANE_MONEY_COPPER else None
        if bind is None:
            bind = 0 if 0 <= int(lo18) < _MAX_SANE_MONEY_COPPER else None
        src = f"package[{package_index}]+0x18/bind +0x10/trade"
        return bind, trade, src
    except Exception:
        return None, None, "none"
    finally:
        try:
            kernel32.CloseHandle(wintypes.HANDLE(h))
        except Exception:
            pass


def _money_from_package_slot(
    session: GameAttachSession, package_index: int
) -> tuple[int | None, str]:
    """
    Backward-compatible: return bind copper only.

    @author by ak
    """
    bind, _trade, src = _money_pair_from_package(session, package_index)
    if bind is not None:
        return int(bind), src
    return None, "none"


def _money_from_wallet_table(
    session: GameAttachSession, money_index: int
) -> int | None:
    """
    Legacy wallet table at pkg_root+0x3C (stride 0x20, amount @ +0x10).

    Live 2026-07-18: entry[2]+0x10 often is NOT host gold (looks like cap/counter).
    Kept as last-resort only when package+0x10 and GetMoney both fail.

    @author by ak
    """
    h = _open_process(int(session.pid))
    try:
        root_va = _note_va(session, NOTE_VA_TASK_IFACE_OR_PKG_ROOT)
        root = remote_call_cdecl_x86(int(session.pid), root_va, [])
        if not root:
            return None
        p3c_b = _safe_rpm(h, int(root) + 0x3C, 4)
        if len(p3c_b) < 4:
            return None
        p3c = struct.unpack_from("<I", p3c_b, 0)[0]
        if not p3c:
            return None
        off = int(money_index) * 0x20 + 0x10
        raw = _safe_rpm(h, p3c + off, 8)
        if len(raw) < 4:
            return None
        lo = struct.unpack_from("<I", raw, 0)[0]
        hi = struct.unpack_from("<I", raw, 4)[0] if len(raw) >= 8 else 0
        # require hi==0 to avoid cap fields (often 0x2710=10000 beside amount)
        if hi != 0:
            return None
        return _sane_money(int(lo))
    except Exception:
        return None
    finally:
        try:
            kernel32.CloseHandle(wintypes.HANDLE(h))
        except Exception:
            pass


def _money_from_getmoney_export(
    session: GameAttachSession,
    money_index: int,
    *,
    note_va: int = NOTE_VA_GET_MONEY_BY_PKG,
) -> int | None:
    """
    GetMoney / GetMoney2 stdcall — trust EAX only (ignore EDX tag).

    @author by ak
    """
    try:
        va = _note_va(session, int(note_va))
        raw = int(
            _remote_stdcall(int(session.pid), va, [int(money_index) & 0xFF])
        )
        return _sane_money(int(raw) & 0xFFFFFFFF)
    except Exception:
        return None


def get_money(
    session: GameAttachSession,
    money_package_index: int = DEFAULT_MONEY_PACKAGE_INDEX,
    *,
    log: LogFn | None = None,
) -> PackageActionResult:
    """
    Read host money (copper).

    Primary money = 绑定币 (bind) at package+0x18 / GetMoney2.
    money_trade   = 可交易 (trade) at package+0x10 / GetMoney.
    卖满金只认绑定币。

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        idx = int(money_package_index)
        bind = 0
        trade = 0
        source = "none"
        b, t, src = _money_pair_from_package(session, idx)
        if b is not None or t is not None:
            bind = int(b or 0)
            trade = int(t or 0)
            source = src
        # export fallback if package read failed
        if source == "none" or (bind <= 0 and trade <= 0):
            m2 = _money_from_getmoney_export(
                session, idx, note_va=NOTE_VA_GET_MONEY2_BY_PKG
            )
            m1 = _money_from_getmoney_export(
                session, idx, note_va=NOTE_VA_GET_MONEY_BY_PKG
            )
            if m2 is not None:
                bind = int(m2)
                source = f"GetMoney2[{idx}].EAX"
            if m1 is not None:
                trade = int(m1)
                if source == "none":
                    source = f"GetMoney[{idx}].EAX"
                else:
                    source = f"{source}+GetMoney"
        if bind <= 0 and trade <= 0:
            for mi in (idx, 0, 1, 2, 3):
                alt = _money_from_wallet_table(session, mi)
                if alt is not None and alt > bind:
                    bind = int(alt)
                    source = f"wallet[{mi}]"
        # primary for 卖满金 = bind
        money = int(bind)
        msg = (
            f"绑定={format_gold(bind)} 非绑={format_gold(trade)} "
            f"type={idx} src={source}"
        )
        log(f"package_api: {msg}")
        return PackageActionResult(
            ok=True,
            action="money",
            message=msg,
            money=money,
            money_trade=int(trade),
        )
    except Exception as e:
        log(f"package_api: money failed: {e}")
        return PackageActionResult(
            ok=False, action="money", message=str(e), error=str(e), money=None
        )


def format_money_pair(bind: int | None, trade: int | None = None) -> str:
    """
    UI line: 绑定 + optional 非绑.

    @author by ak
    """
    b = format_gold(bind)
    if trade is None:
        return f"绑定 {b}"
    return f"绑定 {b}  |  非绑 {format_gold(trade)}"


def sell_item_from_package(
    session: GameAttachSession,
    package_index: int,
    slot: int,
    count: int = 1,
    *,
    log: LogFn | None = None,
) -> PackageActionResult:
    """
    Sell one stack via native sell-from-package (requires open grocer service).

    ret!=0 is not enough proof of success; callers that care about money
    should re-read get_money / bag after sell.

    @author by ak
    """
    log = log or (lambda _m: None)
    cnt = max(1, int(count))
    try:
        va = _note_va(session, NOTE_VA_SELL_FROM_PACKAGE)
        ret = _remote_stdcall(
            int(session.pid),
            va,
            [int(package_index) & 0xFF, int(slot) & 0xFFFF, int(cnt) & 0xFFFF],
        )
        # native often returns non-zero even when grocer UI is closed; keep ret
        # for diagnostics but do not treat ret alone as money proof.
        ok = True
        msg = f"出售 包{package_index} 槽{slot} x{cnt} ret={ret}"
        log(f"package_api: {msg}")
        invalidate_bag_cache(session)
        return PackageActionResult(
            ok=ok, action="sell", message=msg, ret=int(ret or 0)
        )
    except Exception as e:
        log(f"package_api: sell failed: {e}")
        return PackageActionResult(
            ok=False, action="sell", message=str(e), error=str(e)
        )


def npc_say_hello(
    session: GameAttachSession,
    npc_id: int,
    *,
    log: LogFn | None = None,
) -> PackageActionResult:
    """
    plg::NPCSayHello(int64 id).

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        va = _resolve_export_va(session, EXPORT_NPC_SAY_HELLO)
        lo = int(npc_id) & 0xFFFFFFFF
        hi = (int(npc_id) >> 32) & 0xFFFFFFFF
        ret = remote_call_cdecl_x86(int(session.pid), va, [lo, hi])
        ok = bool(int(ret or 0) & 0xFF)
        msg = f"NPCSayHello id={npc_id} ret={ret}"
        log(f"package_api: {msg}")
        return PackageActionResult(
            ok=ok, action="npc_hello", message=msg, ret=int(ret or 0)
        )
    except Exception as e:
        log(f"package_api: npc_hello failed: {e}")
        return PackageActionResult(
            ok=False, action="npc_hello", message=str(e), error=str(e)
        )


def copper_to_gold(money: int | None) -> float:
    """Copper amount -> 金 (float)."""
    if money is None:
        return 0.0
    return int(money) / float(GOLD_UNIT)


def gold_to_copper(gold: float | int) -> int:
    """金 amount -> copper (int, rounded down)."""
    return int(max(0.0, float(gold)) * GOLD_UNIT)


def pills_needed_for_gold(
    current_copper: int,
    target_copper: int,
    *,
    pill_price_copper: int = DEFAULT_REVIVE_PILL_PRICE,
) -> int:
    """
    How many 白云熊胆丸 to sell: ceil((target - current) / pill_price).

    Both sides copper. pill default = 10 金.

    @author by ak
    """
    cur = max(0, int(current_copper or 0))
    tgt = max(0, int(target_copper or 0))
    if cur >= tgt:
        return 0
    price = max(1, int(pill_price_copper or DEFAULT_REVIVE_PILL_PRICE))
    need = tgt - cur
    return (need + price - 1) // price


def format_gold(money: int | None) -> str:
    """
    Format copper as 金/银/铜 display (no raw copper in UI).

    @author by ak
    """
    if money is None:
        return "n/a"
    m = max(0, int(money))
    gold = m // GOLD_UNIT
    rem = m % GOLD_UNIT
    silver = rem // SILVER_UNIT
    copper = rem % SILVER_UNIT
    return f"{gold}金{silver}银{copper}铜"


_REVIVE_RE = re.compile(
    r"白云熊胆丸|白云熊丹丸|白云.*熊[胆丹]丸|熊胆丸|熊丹丸|白云.*复活丹|复活丹"
)


def is_revive_pill_name(name: str, tid: int = 0) -> bool:
    """
    True if item is 白云熊胆丸 (name or known tid) or legacy aliases.

    @author by ak
    """
    if int(tid or 0) == int(DEFAULT_REVIVE_PILL_TID):
        return True
    n = (name or "").strip()
    if not n:
        return False
    if n.startswith("tid="):
        try:
            return int(n.split("=", 1)[1]) == int(DEFAULT_REVIVE_PILL_TID)
        except Exception:
            return False
    for token in DEFAULT_REVIVE_PILL_TOKENS:
        if token and token in n:
            return True
    return bool(_REVIVE_RE.search(n))


def ensure_full_gold(
    session: GameAttachSession,
    *,
    min_money: int = DEFAULT_FULL_GOLD_MIN,
    pill_price: int = DEFAULT_REVIVE_PILL_PRICE,
    package_index: int = DEFAULT_PACKAGE_INDEX,
    money_package_index: int = DEFAULT_MONEY_PACKAGE_INDEX,
    stop_event=None,
    on_money=None,
    continuous: bool = True,
    poll_s: float = DEFAULT_FULL_GOLD_POLL_S,
    retry_s: float = DEFAULT_FULL_GOLD_RETRY_S,
    log: LogFn | None = None,
) -> PackageActionResult:
    """
    卖满金：保持金钱 >= 保底（默认 44990 金）。

    continuous=True（默认）时一直守护：
      - 不足 → 卖白云熊胆丸补到保底
      - 充足 → 轮询等待（客户端会消耗金币，之后再补）
      - 仅 stop_event 才退出

    公式：need_pills = ceil((目标金 - 当前金) / 10)
    min_money / pill_price 均为铜。

    @author by ak
    """
    import time as _time

    log = log or (lambda _m: None)
    target = max(0, int(min_money))
    price = max(1, int(pill_price or DEFAULT_REVIVE_PILL_PRICE))
    if price < GOLD_UNIT:
        log(
            f"package_api: pill_price={price} too small, "
            f"force {DEFAULT_REVIVE_PILL_PRICE} (10金/个)"
        )
        price = int(DEFAULT_REVIVE_PILL_PRICE)
    poll = max(0.5, float(poll_s or DEFAULT_FULL_GOLD_POLL_S))
    retry = max(0.5, float(retry_s or DEFAULT_FULL_GOLD_RETRY_S))

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    def _emit_money(val: int) -> None:
        if on_money is not None:
            try:
                on_money(int(val))
            except Exception:
                pass

    def _sleep_stop(seconds: float) -> bool:
        """Sleep in slices; return True if stop requested. Never sleep negative."""
        end = _time.time() + max(0.0, float(seconds or 0.0))
        while True:
            if _stopped():
                return True
            left = end - _time.time()
            if left <= 0:
                break
            _time.sleep(min(0.25, left))
        return _stopped()

    mres = get_money(session, money_package_index, log=log)
    money = int(mres.money or 0)
    _emit_money(money)
    start_money = money
    sold_total = 0
    topup_cycles = 0
    log(
        f"package_api: 卖满金守护 当前={format_gold(money)} "
        f"目标>={format_gold(target)} continuous={bool(continuous)} "
        f"单价={price // GOLD_UNIT}金/个 点停止结束"
    )

    while not _stopped():
        blocked, brsn = _pid_blocked(session)
        if blocked:
            msg = f"卖满金已停止(进程不可用): {brsn}"
            log(f"package_api: {msg}")
            return PackageActionResult(
                ok=sold_total > 0,
                action="full_gold",
                message=msg,
                error=brsn or "remote_blocked",
                money=money,
            )
        need_left = pills_needed_for_gold(money, target, pill_price_copper=price)
        if need_left <= 0:
            if not continuous:
                msg = f"金充足 {format_gold(money)} >= {format_gold(target)}"
                log(f"package_api: {msg}")
                return PackageActionResult(
                    ok=True, action="full_gold", message=msg, money=money
                )
            log(
                f"package_api: 金充足 {format_gold(money)}，"
                f"{poll:.1f}s 后复检（守护中）"
            )
            if _sleep_stop(poll):
                break
            m_idle = get_money(session, money_package_index, log=log)
            money = int(m_idle.money or 0)
            _emit_money(money)
            continue

        # 不足：补金一轮
        topup_cycles += 1
        plan = int(need_left)
        sold_cycle = 0
        fail_streak = 0
        rounds = 0
        max_rounds = max(plan + 50, 200)
        log(
            f"package_api: 补金#{topup_cycles} 当前={format_gold(money)} "
            f"差={copper_to_gold(target) - copper_to_gold(money):.2f}金 "
            f"需卖≈{plan}个"
        )
        while rounds < max_rounds and not _stopped():
            blocked, brsn = _pid_blocked(session)
            if blocked:
                msg = f"卖满金补金中断(进程不可用): {brsn}"
                log(f"package_api: {msg}")
                return PackageActionResult(
                    ok=sold_total > 0,
                    action="full_gold",
                    message=msg,
                    error=brsn or "remote_blocked",
                    money=money,
                )
            need_left = pills_needed_for_gold(
                money, target, pill_price_copper=price
            )
            if need_left <= 0:
                break
            pills = [
                it
                for it in list_package_items(session, package_index, log=log)
                if is_revive_pill_name(it.name, it.tid)
            ]
            if not pills:
                log(
                    f"package_api: 背包无{DEFAULT_REVIVE_PILL_NAME}，"
                    f"{retry:.1f}s 后重试"
                )
                if not continuous:
                    break
                if _sleep_stop(retry):
                    break
                m_wait = get_money(session, money_package_index, log=log)
                money = int(m_wait.money or 0)
                _emit_money(money)
                break  # outer loop re-evaluates
            it = pills[0]
            stack = max(1, int(it.count or 1))
            n_sell = min(need_left, stack)
            before = money
            before_cnt = stack
            r = sell_item_from_package(
                session, it.package, it.slot, n_sell, log=log
            )
            _time.sleep(0.12)
            m_mid = get_money(session, money_package_index, log=log)
            money = int(m_mid.money or 0)
            _emit_money(money)
            rounds += 1

            if money > before:
                gained = money - before
                units = (
                    max(1, (gained + price - 1) // price) if price > 0 else n_sell
                )
                sold_cycle += units
                sold_total += units
                fail_streak = 0
                log(
                    f"package_api: sell ok +{gained}铜 ≈{units}个 "
                    f"slot={it.slot} x{n_sell} ret={r.ret} "
                    f"sold_cycle={sold_cycle}/{plan} total={sold_total}"
                )
                continue

            after_items = list_package_items(
                session, package_index, log=lambda _m: None
            )
            after = next(
                (
                    x
                    for x in after_items
                    if int(x.slot) == int(it.slot)
                    and is_revive_pill_name(x.name, x.tid)
                ),
                None,
            )
            after_cnt = int(after.count or 0) if after is not None else 0
            if after is None or after_cnt < before_cnt:
                dropped = (
                    before_cnt - after_cnt if after is not None else before_cnt
                )
                sold_cycle += max(1, dropped)
                sold_total += max(1, dropped)
                fail_streak = 0
                log(
                    f"package_api: sell bag-progress slot={it.slot} "
                    f"count {before_cnt}->{after_cnt} money={money} "
                    f"total={sold_total}"
                )
                if rounds % 3 == 0:
                    m_mid = get_money(session, money_package_index, log=log)
                    money = int(m_mid.money or 0)
                    _emit_money(money)
                continue

            fail_streak += 1
            log(
                f"package_api: sell no progress "
                f"slot={it.slot} ret={r.ret} money={before}->{money} "
                f"count={before_cnt}->{after_cnt} fail={fail_streak}"
            )
            if fail_streak >= 5:
                if continuous:
                    log(
                        f"package_api: 连续失败，{retry:.1f}s 后重试"
                        f"（请保持杂货NPC交易打开）"
                    )
                    if _sleep_stop(retry):
                        break
                    fail_streak = 0
                    m_wait = get_money(session, money_package_index, log=log)
                    money = int(m_wait.money or 0)
                    _emit_money(money)
                    break
                break
            _time.sleep(0.2)

        if not continuous:
            break
        # continuous: loop forever until stop
        continue

    m2 = get_money(session, money_package_index, log=log)
    money2 = int(m2.money or 0)
    _emit_money(money2)
    if _stopped():
        msg = (
            f"卖满金已停止 累计卖约{sold_total}个 补金轮次={topup_cycles} "
            f"{format_gold(start_money)} -> {format_gold(money2)}"
        )
        log(f"package_api: {msg}")
        return PackageActionResult(
            ok=sold_total > 0 or money2 >= target,
            action="full_gold",
            message=msg,
            money=money2,
        )
    if money2 < target and sold_total <= 0:
        any_pill = any(
            is_revive_pill_name(it.name, it.tid)
            for it in list_package_items(session, package_index, log=log)
        )
        if not any_pill:
            msg = (
                f"当前 {format_gold(money2)} 不足目标 {format_gold(target)}，"
                f"背包无{DEFAULT_REVIVE_PILL_NAME}"
            )
        else:
            msg = (
                f"卖满金未成交 {format_gold(start_money)} -> {format_gold(money2)} "
                f"（请先打开杂货NPC交易）"
            )
        log(f"package_api: {msg}")
        return PackageActionResult(
            ok=False, action="full_gold", message=msg, money=money2
        )
    ok = money2 >= target or sold_total > 0
    msg = (
        f"卖满金 累计卖约{sold_total}个 补金轮次={topup_cycles} "
        f"{format_gold(start_money)} -> {format_gold(money2)}"
        + ("" if money2 >= target else "（未达保底）")
    )
    log(f"package_api: {msg}")
    return PackageActionResult(
        ok=ok, action="full_gold", message=msg, money=money2
    )


@dataclass
class PackageLayout:
    """Package capacity / occupancy snapshot. @author by ak"""

    package_index: int
    ptr: int = 0
    cap: int = 0
    size: int = 0
    used_slots: int = 0
    free_slots: int = 0
    items: list | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["items"] = [x.to_dict() if hasattr(x, "to_dict") else x for x in (self.items or [])]
        return d


def get_package_layout(
    session: GameAttachSession,
    package_index: int = DEFAULT_PACKAGE_INDEX,
    *,
    log: LogFn | None = None,
    with_items: bool = True,
) -> PackageLayout:
    """
    Read package cap/size and free slots (cap - non-empty).

    free_slots uses non-empty slot count when items are enumerated; falls back
    to max(0, cap - size) when with_items=False.

    @author by ak
    """
    log = log or (lambda _m: None)
    idx = int(package_index)
    out = PackageLayout(package_index=idx)
    try:
        pkg = get_package_ptr(session, idx)
    except Exception as e:
        log(f"package_api: layout GetPackage err: {e}")
        return out
    out.ptr = int(pkg or 0)
    if not pkg:
        return out
    h = _open_process(int(session.pid))
    try:
        raw = _safe_rpm(h, int(pkg), 0x40)
        if len(raw) < PKG_ARR_OFF + 4:
            return out
        cap = int(struct.unpack_from("<I", raw, PKG_CAP_OFF)[0])
        size = int(struct.unpack_from("<I", raw, PKG_SIZE_OFF)[0])
        # 扩展包可能 cap=0、size>0：对外 cap 用 max(cap,size)
        eff_cap = cap
        if eff_cap <= 0 and 0 < size <= MAX_PACKAGE_SLOTS:
            eff_cap = size
        if eff_cap <= 0 or eff_cap > MAX_PACKAGE_SLOTS:
            log(f"package_api: layout bad cap={cap} size={size}")
            return out
        out.cap = int(eff_cap)
        out.size = size
    finally:
        try:
            kernel32.CloseHandle(wintypes.HANDLE(h))
        except Exception:
            pass
    items: list[PackageItem] = []
    if with_items:
        try:
            items = list_package_items(session, idx, log=log)
        except Exception as e:
            log(f"package_api: layout list items err: {e}")
            items = []
        out.items = items
        out.used_slots = len(items)
        out.free_slots = max(0, int(out.cap) - int(out.used_slots))
    else:
        out.used_slots = max(0, min(int(out.size), int(out.cap)))
        out.free_slots = max(0, int(out.cap) - int(out.used_slots))
    return out


# Live RE 2026-07-22 (UI buy path ~C595E0 + c2s builder cca1d0):
# packet type=0xE, subtype=1, payload length = 9 bytes:
#   +0 u8  flag (UI writes 1)
#   +1 u32 goods_tid (shop template id)
#   +5 u16 booth_index (货架位；live 残页 goods+0x1C=7，idx=0 会发包无变化)
#   +7 u16 count
# Sell sibling (cca450) uses subtype=2 with different 9-byte payload
# (tid@0, pack@4, slot@5, count@7).
#
# 商店货架对象 (mgr=[4AE420]+8, pages=[mgr+0xF0], page+0x2C 指针表):
#   goods+0x0C = tid
#   goods+0x1C = booth_index (BuyItem 应用此值)
#   goods+0x38 = cost_tid, +0x3C = cost_count  (残页: 100023 x2)
BUY_ITEM_STRUCT_SIZE = 16
BUY_ITEM_FLAG_OFF = 0
BUY_ITEM_TID_OFF = 1
BUY_ITEM_INDEX_OFF = 5
BUY_ITEM_COUNT_OFF = 7
BUY_ITEM_FLAG_DEFAULT = 1
# live 缺省 booth（扫不到货架时用）
DEFAULT_BOOTH_BY_TID = {
    100007: 7,   # 绝学兑换残页（BuyItem booth；易 ret=1 无进度，优先 BuyShopSlot）
    100064: 18,  # 50丹切糕（买向；卖仍走 Sell）
    101020: 20,  # 一组9999兑换丹（买向）
}
# live 原生 BuyShopSlot(page,slot)：比 BuyItem 稳，卖切糕后货架扫描偶发空时用
DEFAULT_SHOP_SLOT_BY_TID: dict[int, tuple[int, int]] = {
    100007: (0, 47),   # 绝学兑换残页 · 东方兑换店
    101058: (1, 11),   # 霸刀牌 · 不败兑换商店（第一格菜单）
}
# 运行中最近一次成功的 page/slot（跨卖/买粘滞）
_LAST_SHOP_SLOT_BY_TID: dict[int, tuple[int, int]] = {}


def list_shop_goods(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
    max_pages: int = 16,
    max_slots: int = 96,
) -> list[dict]:
    """
    枚举当前已开商店货架：tid / booth / cost_tid / cost_n / page / slot。
    用于霸刀等「背包 tid ≠ 货架 goods tid」时按花费匹配。
    @author by ak
    """
    log = log or (lambda _m: None)
    out: list[dict] = []
    seen: set[int] = set()
    try:
        from app.core.plg_exports import note_va_to_live
        from app.core.remote_runtime import remote_call_cdecl_x86, remote_call_thiscall_x86
        import struct as _st

        base = int(getattr(session, "module_base", 0) or 0)
        if base <= 0:
            return out
        va = note_va_to_live(base, 0x004AE420)
        va_get = note_va_to_live(base, 0x0055ADF0)
        pid = int(session.pid)
        root = int(remote_call_cdecl_x86(pid, va, []) or 0)
        if root <= 0:
            return out
        h = _open_process(pid)
        try:
            def _u32(addr: int) -> int:
                b = _rpm(h, addr, 4)
                return _st.unpack("<I", b)[0] if b and len(b) == 4 else 0

            mgr = _u32(root + 8)
            if mgr <= 0x10000:
                return out
            arr = _u32(mgr + 0xF0)
            n = min(int(_u32(mgr + 0xF4) or 0), int(max_pages))
            if arr <= 0x10000 or n <= 0:
                log(f"package_api: list_shop_goods empty mgr pages n={n} arr={arr:#x}")
                return out
            for page in range(n):
                pg = _u32(arr + page * 4)
                if pg <= 0x10000:
                    continue
                rawp = _rpm(h, pg + 0x30, 2) or b"\x00\x00"
                cols = int(_st.unpack("<H", rawp[:2])[0] or 0) or 10
                cont = pg + 8
                # 原生 get slot
                for slot in range(int(max_slots)):
                    gp = 0
                    try:
                        gp = int(
                            remote_call_thiscall_x86(
                                pid, va_get, cont, [int(slot), 0]
                            )
                            or 0
                        )
                    except Exception:
                        gp = 0
                    if gp <= 0x10000 or gp in seen:
                        continue
                    try:
                        gb = _rpm(h, gp, 0x50) or b""
                    except Exception:
                        continue
                    if len(gb) < 0x40:
                        continue
                    tid = int(_st.unpack_from("<I", gb, 0x0C)[0])
                    if tid <= 0:
                        continue
                    seen.add(gp)
                    booth = int(_st.unpack_from("<I", gb, 0x1C)[0])
                    cost_tid = int(_st.unpack_from("<I", gb, 0x38)[0])
                    cost_n = int(_st.unpack_from("<I", gb, 0x3C)[0])
                    out.append(
                        {
                            "tid": tid,
                            "booth": booth,
                            "cost_tid": cost_tid,
                            "cost_n": cost_n,
                            "page": int(page),
                            "slot": int(slot),
                            "cols": int(cols),
                            "booth0": int(page) * int(cols) + int(slot),
                            "goods_ptr": int(gp),
                            "via": "55adf0",
                        }
                    )
                # ptr table supplement
                p2c = _u32(pg + 0x2C)
                if p2c <= 0x10000:
                    continue
                try:
                    raw = _rpm(h, p2c, min(0x400, max_slots * 4)) or b""
                except Exception:
                    continue
                for slot in range(0, len(raw) // 4):
                    gp = _st.unpack_from("<I", raw, slot * 4)[0]
                    if gp <= 0x10000 or gp in seen:
                        continue
                    try:
                        gb = _rpm(h, gp, 0x50) or b""
                    except Exception:
                        continue
                    if len(gb) < 0x40:
                        continue
                    tid = int(_st.unpack_from("<I", gb, 0x0C)[0])
                    if tid <= 0:
                        continue
                    seen.add(gp)
                    booth = int(_st.unpack_from("<I", gb, 0x1C)[0])
                    cost_tid = int(_st.unpack_from("<I", gb, 0x38)[0])
                    cost_n = int(_st.unpack_from("<I", gb, 0x3C)[0])
                    out.append(
                        {
                            "tid": tid,
                            "booth": booth,
                            "cost_tid": cost_tid,
                            "cost_n": cost_n,
                            "page": int(page),
                            "slot": int(slot),
                            "cols": int(cols),
                            "booth0": int(page) * int(cols) + int(slot),
                            "goods_ptr": int(gp),
                            "via": "ptrtab",
                        }
                    )
        finally:
            try:
                kernel32.CloseHandle(wintypes.HANDLE(h))
            except Exception:
                pass
    except Exception as e:
        log(f"package_api: list_shop_goods err: {e}")
    return out


def find_shop_goods(
    session: GameAttachSession,
    *,
    goods_tid: int = 0,
    cost_tid: int = 0,
    cost_n: int = 0,
    cost_n_candidates: list[int] | tuple[int, ...] | None = None,
    log: LogFn | None = None,
) -> dict:
    """
    在当前商店货架找商品。
    优先 goods_tid；否则按 cost_tid(+cost_n) 匹配（霸刀：花费=绝学残页x6000）。
    @author by ak
    """
    log = log or (lambda _m: None)
    items = list_shop_goods(session, log=log)
    if not items:
        return {}
    want_tid = int(goods_tid or 0)
    if want_tid > 0:
        for it in items:
            if int(it.get("tid", 0) or 0) == want_tid:
                log(
                    f"package_api: find_shop_goods by tid={want_tid} "
                    f"page={it.get('page')} slot={it.get('slot')} "
                    f"booth={it.get('booth')} cost={it.get('cost_tid')}x{it.get('cost_n')}"
                )
                return it
    ctid = int(cost_tid or 0)
    cands: list[int] = []
    if cost_n_candidates:
        cands = [int(x) for x in cost_n_candidates if int(x) > 0]
    cn = int(cost_n or 0)
    if cn > 0 and cn not in cands:
        cands.insert(0, cn)
    if ctid > 0:
        # 精确 cost_n
        for want_n in cands or [0]:
            for it in items:
                if int(it.get("cost_tid", 0) or 0) != ctid:
                    continue
                if want_n > 0 and int(it.get("cost_n", 0) or 0) != want_n:
                    continue
                if want_n <= 0 and int(it.get("cost_n", 0) or 0) <= 0:
                    continue
                log(
                    f"package_api: find_shop_goods by cost={ctid}x{it.get('cost_n')} "
                    f"-> tid={it.get('tid')} page={it.get('page')} slot={it.get('slot')} "
                    f"booth={it.get('booth')}"
                )
                return it
        # 宽松：只要 cost_tid 对上，取 cost_n 最大的（多半是整换配方）
        hits = [
            it
            for it in items
            if int(it.get("cost_tid", 0) or 0) == ctid
            and int(it.get("cost_n", 0) or 0) > 0
        ]
        if hits:
            hits.sort(key=lambda x: int(x.get("cost_n", 0) or 0), reverse=True)
            it = hits[0]
            log(
                f"package_api: find_shop_goods loose cost_tid={ctid} "
                f"pick max cost_n={it.get('cost_n')} tid={it.get('tid')} "
                f"page={it.get('page')} slot={it.get('slot')}"
            )
            return it
    # dump 便于 live 校准
    sample = items[:24]
    log(
        f"package_api: find_shop_goods miss tid={want_tid} cost={ctid}x{cands} "
        f"shelf_n={len(items)} sample="
        + ",".join(
            f"{it.get('tid')}:{it.get('cost_tid')}x{it.get('cost_n')}@p{it.get('page')}s{it.get('slot')}"
            for it in sample
        )
    )
    return {}


def resolve_shop_goods_meta(
    session: GameAttachSession,
    goods_tid: int,
    *,
    log: LogFn | None = None,
) -> dict:
    """
    从客户端商店货架表解析 booth_index / 花费。
    失败返回 {}；成功至少含 booth, tid。
    @author by ak
    """
    log = log or (lambda _m: None)
    tid_want = int(goods_tid or 0)
    if tid_want <= 0:
        return {}
    try:
        from app.core.plg_exports import note_va_to_live
        from app.core.remote_runtime import remote_call_cdecl_x86
        import struct as _st

        base = int(getattr(session, "module_base", 0) or 0)
        if base <= 0:
            return {}
        va = note_va_to_live(base, 0x004AE420)
        pid = int(session.pid)
        root = int(remote_call_cdecl_x86(pid, va, []) or 0)
        if root <= 0:
            return {}
        h = _open_process(pid)
        try:
            def _u32(addr: int) -> int:
                b = _rpm(h, addr, 4)
                return _st.unpack("<I", b)[0] if b and len(b) == 4 else 0

            mgr = _u32(root + 8)
            if mgr <= 0x10000:
                return {}
            arr = _u32(mgr + 0xF0)
            n = _u32(mgr + 0xF4)
            if arr <= 0x10000 or n <= 0:
                return {}
            n = min(int(n), 16)
            for i in range(n):
                pg = _u32(arr + i * 4)
                if pg <= 0x10000:
                    continue
                p2c = _u32(pg + 0x2C)
                if p2c <= 0x10000:
                    continue
                try:
                    raw = _rpm(h, p2c, 0x400) or b""
                except Exception:
                    continue
                for j in range(0, len(raw) - 3, 4):
                    gp = _st.unpack_from("<I", raw, j)[0]
                    if gp <= 0x10000:
                        continue
                    try:
                        gb = _rpm(h, gp, 0x50) or b""
                    except Exception:
                        continue
                    if len(gb) < 0x40:
                        continue
                    tid = _st.unpack_from("<I", gb, 0x0C)[0]
                    if tid != tid_want:
                        continue
                    booth = int(_st.unpack_from("<I", gb, 0x1C)[0])
                    cost_tid = int(_st.unpack_from("<I", gb, 0x38)[0])
                    cost_n = int(_st.unpack_from("<I", gb, 0x3C)[0])
                    meta = {
                        "tid": tid,
                        "booth": booth,
                        "cost_tid": cost_tid,
                        "cost_n": cost_n,
                        "page": i,
                        "slot_ptr": j // 4,
                        "goods_ptr": gp,
                    }
                    log(
                        f"package_api: shop goods tid={tid} booth={booth} "
                        f"cost={cost_tid}x{cost_n} page={i}"
                    )
                    return meta
        finally:
            try:
                kernel32.CloseHandle(wintypes.HANDLE(h))
            except Exception:
                pass
    except Exception as e:
        log(f"package_api: resolve_shop_goods_meta err: {e}")
    # fallback known booth
    if tid_want in DEFAULT_BOOTH_BY_TID:
        return {
            "tid": tid_want,
            "booth": int(DEFAULT_BOOTH_BY_TID[tid_want]),
            "cost_tid": 0,
            "cost_n": 0,
            "page": -1,
            "slot_ptr": -1,
            "goods_ptr": 0,
            "fallback": True,
        }
    return {}



def find_shop_page_slot_for_tid(
    session: GameAttachSession,
    goods_tid: int,
    *,
    log: LogFn | None = None,
    max_pages: int = 16,
    max_slots: int = 96,
) -> dict:
    """
    在商店货架页里找 tid，返回 {page, slot, cols, booth0, tid}.
    优先 thiscall 0x55ADF0(page+8, slot, 0) 与 0x749040 取货一致；
    失败再扫 page+0x2C 指针表。
    @author by ak
    """
    log = log or (lambda _m: None)
    tid_want = int(goods_tid or 0)
    if tid_want <= 0:
        return {}
    try:
        from app.core.plg_exports import note_va_to_live
        from app.core.remote_runtime import remote_call_thiscall_x86
        import struct as _st

        base = int(getattr(session, "module_base", 0) or 0)
        if base <= 0:
            return {}
        pid = int(session.pid)
        va_root = note_va_to_live(base, 0x004AE420)
        va_get = note_va_to_live(base, 0x0055ADF0)
        root = int(remote_call_cdecl_x86(pid, va_root, []) or 0)
        if root <= 0:
            return {}
        h = _open_process(pid)
        try:
            def _u32(addr: int) -> int:
                b = _rpm(h, addr, 4)
                return _st.unpack("<I", b)[0] if b and len(b) == 4 else 0

            mgr = _u32(root + 8)
            arr = _u32(mgr + 0xF0)
            n = min(int(_u32(mgr + 0xF4) or 0), int(max_pages))
            if arr <= 0x10000 or n <= 0:
                return {}
            # 1) 原生容器
            for page in range(n):
                pg = _u32(arr + page * 4)
                if pg <= 0x10000:
                    continue
                rawp = _rpm(h, pg + 0x30, 2) or b"\x00\x00"
                cols = int(_st.unpack("<H", rawp[:2])[0] or 0) or 10
                cont = pg + 8
                for slot in range(int(max_slots)):
                    try:
                        gp = int(
                            remote_call_thiscall_x86(
                                pid, va_get, cont, [int(slot), 0]
                            )
                            or 0
                        )
                    except Exception:
                        gp = 0
                    if gp <= 0x10000:
                        continue
                    try:
                        gb = _rpm(h, gp, 0x10) or b""
                    except Exception:
                        continue
                    if len(gb) < 0x10:
                        continue
                    tid = _st.unpack_from("<I", gb, 0x0C)[0]
                    if tid != tid_want:
                        continue
                    meta = {
                        "tid": tid,
                        "page": int(page),
                        "slot": int(slot),
                        "cols": int(cols),
                        "booth0": int(page) * int(cols) + int(slot),
                        "goods_ptr": int(gp),
                        "via": "55adf0",
                    }
                    log(
                        f"package_api: shop slot tid={tid} page={page} slot={slot} "
                        f"cols={cols} via=55adf0"
                    )
                    return meta
            # 2) 指针表回退
            for page in range(n):
                pg = _u32(arr + page * 4)
                if pg <= 0x10000:
                    continue
                rawp = _rpm(h, pg + 0x30, 2) or b"\x00\x00"
                cols = int(_st.unpack("<H", rawp[:2])[0] or 0) or 10
                p2c = _u32(pg + 0x2C)
                if p2c <= 0x10000:
                    continue
                try:
                    raw = _rpm(h, p2c, min(0x400, max_slots * 4)) or b""
                except Exception:
                    continue
                for slot in range(0, len(raw) // 4):
                    gp = _st.unpack_from("<I", raw, slot * 4)[0]
                    if gp <= 0x10000:
                        continue
                    try:
                        gb = _rpm(h, gp, 0x10) or b""
                    except Exception:
                        continue
                    if len(gb) < 0x10:
                        continue
                    tid = _st.unpack_from("<I", gb, 0x0C)[0]
                    if tid != tid_want:
                        continue
                    meta = {
                        "tid": tid,
                        "page": int(page),
                        "slot": int(slot),
                        "cols": int(cols),
                        "booth0": int(page) * int(cols) + int(slot),
                        "goods_ptr": int(gp),
                        "via": "ptrtab",
                    }
                    log(
                        f"package_api: shop ptrtab tid={tid} page={page} slot={slot} "
                        f"cols={cols}"
                    )
                    return meta
        finally:
            try:
                kernel32.CloseHandle(wintypes.HANDLE(h))
            except Exception:
                pass
    except Exception as e:
        log(f"package_api: find_shop_page_slot err: {e}")
    return {}


def buy_item_by_shop_slot(
    session: GameAttachSession,
    page: int,
    slot: int,
    count: int = 1,
    *,
    mode: int = 2,
    log: LogFn | None = None,
) -> PackageActionResult:
    """
    原生买货 NOTE_VA_BUY_FROM_SHOP_SLOT (0x749040)：
      thiscall ecx=[host+0x1a84], args (page, slot, count, mode)
    live callers push mode=2/3；与出售同需已开商店。
    @author by ak
    """
    log = log or (lambda _m: None)
    cnt = max(1, int(count) & 0xFFFF)
    md = int(mode)
    try:
        from app.core.plg_exports import note_va_to_live
        from app.core.remote_runtime import remote_call_thiscall_x86
        import struct as _st

        base = int(getattr(session, "module_base", 0) or 0)
        pid = int(session.pid)
        # host object: prefer 4AE400 (same as BuyItem path)
        va_host = note_va_to_live(base, 0x004AE400)
        host = int(remote_call_cdecl_x86(pid, va_host, []) or 0)
        if host <= 0:
            raise OSError("host object null")
        h = _open_process(pid)
        try:
            b = _rpm(h, host + 0x1A84, 4)
            this_ptr = _st.unpack("<I", b)[0] if b and len(b) == 4 else 0
        finally:
            try:
                kernel32.CloseHandle(wintypes.HANDLE(h))
            except Exception:
                pass
        if this_ptr <= 0x10000:
            raise OSError(f"host+0x1a84 invalid: 0x{this_ptr:X}")
        va = _note_va(session, NOTE_VA_BUY_FROM_SHOP_SLOT)
        ret = remote_call_thiscall_x86(
            pid,
            va,
            int(this_ptr),
            [
                int(page) & 0xFFFF,
                int(slot) & 0xFFFF,
                int(cnt) & 0xFFFF,
                int(md) & 0xFF,
            ],
        )
        msg = (
            f"BuyShopSlot this=0x{this_ptr:X} page={page} slot={slot} "
            f"x{cnt} mode={md} ret={ret}"
        )
        log(f"package_api: {msg}")
        ret_i = int(ret or 0)
        # live: ret=1 有机会成交；ret=0 客户端拒单（错店/槽位/会话），勿当 ok
        return PackageActionResult(
            ok=bool(ret_i != 0),
            action="buy_slot",
            message=msg,
            ret=ret_i,
            error="" if ret_i != 0 else "buy_slot_ret0",
        )
    except Exception as e:
        log(f"package_api: BuyShopSlot failed: {e}")
        return PackageActionResult(
            ok=False, action="buy_slot", message=str(e), error=str(e)
        )


def remember_shop_slot(
    goods_tid: int, page: int, slot: int, *, pid: int = 0
) -> None:
    """记下成功/可信的 page/slot（按 game pid 隔离）。"""
    tid = int(goods_tid or 0)
    if tid <= 0 or int(page) < 0 or int(slot) < 0:
        return
    try:
        bucket = _LAST_SHOP_SLOT_BY_TID.setdefault(int(pid or 0), {})
        bucket[tid] = (int(page), int(slot))
    except Exception:
        pass


def clear_shop_slot_cache(goods_tid: int = 0, *, pid: int | None = None) -> None:
    """购买失败/关店重开后清缓存，避免死磕错误 page/slot。"""
    tid = int(goods_tid or 0)
    try:
        if pid is None:
            # clear tid across all pids, or wipe all
            if tid > 0:
                for bucket in _LAST_SHOP_SLOT_BY_TID.values():
                    bucket.pop(tid, None)
            else:
                _LAST_SHOP_SLOT_BY_TID.clear()
            return
        bucket = _LAST_SHOP_SLOT_BY_TID.get(int(pid), {})
        if tid > 0:
            bucket.pop(tid, None)
        else:
            _LAST_SHOP_SLOT_BY_TID.pop(int(pid), None)
    except Exception:
        pass


def buy_goods_tid_via_shop(
    session: GameAttachSession,
    goods_tid: int,
    count: int = 1,
    *,
    goods_index: int = -1,
    npc_id: int = 0,
    log: LogFn | None = None,
    allow_buyitem_fallback: bool = False,
    prefer_known_slot: bool = True,
    allow_shelf_scan: bool = True,
) -> PackageActionResult:
    """
    BuyShopSlot 原生买（残页/霸刀稳路径）。

    **速度关键**：生产环境扫架 find_shop_page_slot 可达 3~4s/次。
    默认顺序：
      1) cache / DEFAULT_SHOP_SLOT 直接买（毫秒级）
      2) 仅 ret=0 或无已知槽 时才 live 扫架
      3) 可选 BuyItem（残页禁用）
    @author by ak
    """
    log = log or (lambda _m: None)
    cnt = max(1, int(count))
    tid = int(goods_tid or 0)
    tried: list[tuple[int, int, str]] = []
    last_r: PackageActionResult | None = None

    def _try(page: int, slot: int, via: str) -> PackageActionResult | None:
        nonlocal last_r
        key = (int(page), int(slot))
        for p0, s0, _v in tried:
            if (p0, s0) == key:
                return None
        tried.append((int(page), int(slot), via))
        r = buy_item_by_shop_slot(
            session, int(page), int(slot), cnt, mode=2, log=log
        )
        r.message = (
            f"{r.message} tid={tid} page={page} slot={slot} via={via}"
        )
        last_r = r
        ret_i = int(getattr(r, "ret", 0) or 0)
        if ret_i != 0:
            remember_shop_slot(tid, int(page), int(slot), pid=int(getattr(session, 'pid', 0) or 0))
            log(
                f"package_api: buy_goods ok-ish ret={ret_i} tid={tid} "
                f"page={page} slot={slot} via={via}"
            )
            return r
        log(
            f"package_api: buy_goods ret=0 tid={tid} page={page} slot={slot} "
            f"via={via}; try next source"
        )
        return None

    def _known_list() -> list[tuple[int, int, str]]:
        out: list[tuple[int, int, str]] = []
        try:
            _pid = int(getattr(session, "pid", 0) or 0)
        except Exception:
            _pid = 0
        cached = (_LAST_SHOP_SLOT_BY_TID.get(_pid) or {}).get(tid)
        if cached:
            out.append((int(cached[0]), int(cached[1]), "cache"))
        if tid in DEFAULT_SHOP_SLOT_BY_TID:
            d = DEFAULT_SHOP_SLOT_BY_TID[tid]
            # 与 cache 相同时跳过重复
            if not out or (int(d[0]), int(d[1])) != (out[0][0], out[0][1]):
                out.append((int(d[0]), int(d[1]), "default_slot"))
        return out

    # 1) 已知槽先打（生产机扫架极慢，绝不能每笔都扫）
    if prefer_known_slot or tid in DEFAULT_SHOP_SLOT_BY_TID:
        for page, slot, via in _known_list():
            hit = _try(page, slot, via)
            if hit is not None:
                return hit

    # 2) 已知全拒 / 无已知 → 才扫架
    if allow_shelf_scan:
        log(f"package_api: buy_goods shelf-scan tid={tid} (known miss or ret=0)")
        loc = find_shop_page_slot_for_tid(session, tid, log=log) or {}
        if loc and int(loc.get("page", -1)) >= 0 and int(loc.get("slot", -1)) >= 0:
            hit = _try(
                int(loc["page"]),
                int(loc["slot"]),
                str(loc.get("via") or "scan"),
            )
            if hit is not None:
                return hit
        else:
            log(f"package_api: buy_goods scan miss tid={tid}")

    clear_shop_slot_cache(tid)

    if last_r is not None:
        return last_r

    if not allow_buyitem_fallback:
        return PackageActionResult(
            ok=False,
            action="buy",
            message=f"buy_goods: no BuyShopSlot for tid={tid} (BuyItem disabled)",
            error="no_shop_slot",
        )

    log(f"package_api: buy_goods fallback BuyItem tid={tid} (no page/slot)")
    meta = resolve_shop_goods_meta(session, tid, log=log) or {}
    idx = int(goods_index)
    if idx < 0:
        idx = int(meta.get("booth", 0) or 0)
    if int(npc_id or 0) <= 0:
        return PackageActionResult(
            ok=False,
            action="buy",
            message="buy_goods: no page/slot and no npc_id",
            error="no_npc",
        )
    return buy_item_from_npc(
        session,
        int(npc_id),
        tid,
        cnt,
        goods_index=idx,
        log=log,
    )


def buy_item_from_npc(
    session: GameAttachSession,
    npc_id: int,
    goods_tid: int,
    count: int = 1,
    *,
    goods_index: int = 0,
    flag: int = BUY_ITEM_FLAG_DEFAULT,
    log: LogFn | None = None,
    require_goods_tid: bool = True,
) -> PackageActionResult:
    """
    plg::BuyItem(__int64 npc_id, buy_item_from_npc* req).

    Layout live-RE'd from UI buy path (9-byte payload for c2s 0xE/1).
    Still require goods_tid>0 (fail-closed). Callers must verify bag delta —
    booth index may need live capture per NPC service.

    @author by ak
    """
    log = log or (lambda _m: None)
    cnt = max(1, int(count))
    tid = int(goods_tid or 0)
    idx = max(0, int(goods_index or 0)) & 0xFFFF
    flg = int(flag if flag is not None else BUY_ITEM_FLAG_DEFAULT) & 0xFF
    if require_goods_tid and tid <= 0:
        msg = "BuyItem: goods_tid 未配置（fail-closed）"
        log(f"package_api: {msg}")
        return PackageActionResult(
            ok=False, action="buy", message=msg, error=msg
        )
    try:
        va = _resolve_export_va(session, EXPORT_BUY_ITEM)
    except Exception as e:
        log(f"package_api: BuyItem resolve failed: {e}")
        return PackageActionResult(
            ok=False, action="buy", message=str(e), error=str(e)
        )
    if not va:
        msg = "BuyItem export missing"
        return PackageActionResult(ok=False, action="buy", message=msg, error=msg)

    from app.core.remote_runtime import (
        remote_alloc,
        remote_free,
        write_process,
        remote_call_cdecl_x86,
    )

    pid = int(session.pid)
    h = _open_process(pid)
    remote = 0
    try:
        remote = int(remote_alloc(h, BUY_ITEM_STRUCT_SIZE) or 0)
        if not remote:
            raise OSError("remote_alloc buy_item struct failed")
        blob = bytearray(BUY_ITEM_STRUCT_SIZE)
        struct.pack_into("<B", blob, BUY_ITEM_FLAG_OFF, flg)
        struct.pack_into("<I", blob, BUY_ITEM_TID_OFF, tid & 0xFFFFFFFF)
        struct.pack_into("<H", blob, BUY_ITEM_INDEX_OFF, idx)
        struct.pack_into("<H", blob, BUY_ITEM_COUNT_OFF, cnt & 0xFFFF)
        write_process(h, remote, bytes(blob))
        lo = int(npc_id) & 0xFFFFFFFF
        hi = (int(npc_id) >> 32) & 0xFFFFFFFF
        # Mangled: YA_N_JPAU... = bool __cdecl (int64, ptr); x86 int64: lo, hi.
        ret = remote_call_cdecl_x86(pid, va, [lo, hi, int(remote) & 0xFFFFFFFF])
        ret_i = int(ret or 0)
        ok = bool(ret_i & 0xFF)
        msg = (
            f"BuyItem npc={npc_id} tid={tid} idx={idx} x{cnt} flag={flg} "
            f"ret={ret_i} low={ret_i & 0xFF} struct=0x{remote:X} (9B payload 0xE/1)"
        )
        log(f"package_api: {msg}")
        return PackageActionResult(
            ok=ok, action="buy", message=msg, ret=ret_i
        )
    except Exception as e:
        log(f"package_api: BuyItem failed: {e}")
        return PackageActionResult(
            ok=False, action="buy", message=str(e), error=str(e)
        )
    finally:
        try:
            if remote:
                remote_free(h, remote)
        except Exception:
            pass
        try:
            kernel32.CloseHandle(wintypes.HANDLE(h))
        except Exception:
            pass



def invalidate_bag_cache(session: GameAttachSession | int | None = None) -> None:
    """Drop bag caches for pid after write ops. @author by ak"""
    try:
        from app.core.safe_dispatch import CACHE_BAG, CACHE_BAG_SLOT, get_dispatch

        if session is None:
            return
        if isinstance(session, int):
            pid = int(session)
        else:
            pid = int(getattr(session, "pid", 0) or 0)
        if pid > 0:
            get_dispatch().invalidate(pid, CACHE_BAG, CACHE_BAG_SLOT)
            # state_dispatch keys (bag / money / warehouse consumers)
            try:
                from app.core.state_dispatch import StateKind, invalidate_states

                invalidate_states(pid, StateKind.BAG, StateKind.MONEY)
            except Exception:
                pass
    except Exception:
        pass


def list_packages_items(
    session: GameAttachSession,
    package_indexes: list[int] | tuple[int, ...] | None = None,
    *,
    log: LogFn | None = None,
    use_cache: bool = True,
    max_age_s: float | None = None,
) -> list[PackageItem]:
    """
    List items across multiple packages (main + 扩展 PACK1/PACK2).

    use_cache: short TTL via SafeDispatch (default on). max_age_s=0 forces refresh.

    @author by ak
    """
    log = log or (lambda _m: None)
    idxs = list(package_indexes or DEFAULT_EXCHANGE_PACKAGE_INDEXES)

    def _load() -> list[PackageItem]:
        out: list[PackageItem] = []
        for idx in idxs:
            try:
                out.extend(list_package_items(session, int(idx), log=log))
            except Exception as e:
                log(f"package_api: list package[{idx}] err: {e}")
                # hard fail: surface for dispatch gate
                msg = str(e).lower()
                if "err=5" in msg or "virtualallocex" in msg or "openprocess" in msg:
                    raise
        return out

    if not use_cache:
        return _load()
    try:
        from app.core.safe_dispatch import (
            OpKind,
            Priority,
            TTL_BAG_S,
            bag_cache_key,
            get_dispatch,
        )

        pid = int(getattr(session, "pid", 0) or 0)
        if pid <= 0:
            return _load()
        age = TTL_BAG_S if max_age_s is None else float(max_age_s)
        return get_dispatch().read_cached(
            pid,
            bag_cache_key(idxs),
            _load,
            max_age=age,
            ttl_s=TTL_BAG_S,
            priority=Priority.P2,
            kind=OpKind.RPM,
            op="list_packages_items",
        )
    except Exception as e:
        # cache layer must not break callers
        try:
            log(f"package_api: bag cache fallback: {e}")
        except Exception:
            pass
        return _load()



def move_item_between_packages(
    session: GameAttachSession,
    src_package: int,
    src_slot: int,
    dst_package: int,
    count: int = 0,
    *,
    dst_slot: int = -1,
    log: LogFn | None = None,
) -> PackageActionResult:
    """
    Move/stack items across packages (主包/扩展/仓库).

    Currently NO verified plg export or note VA for inventory move.
    Sell uses c2s 0xE subtype=2; Buy uses 0xE subtype=1 (9B). cmd_move_ivtr_item / cmd_exg_ivtr_item are S2C names only; C2S move / 门徒仓库技能 not wired yet.
    Fail-closed: returns error so callers can fall back to manual / 门徒仓库.

    @author by ak
    """
    log = log or (lambda _m: None)
    msg = (
        f"仓库/跨包移动未接通: src={src_package}:{src_slot} -> dst={dst_package} "
        f"count={count} (无 plg MoveItem 导出; 待 RE 0xE 子类型或门徒仓库技能)"
    )
    log(f"package_api: {msg}")
    return PackageActionResult(
        ok=False,
        action="move_item",
        message=msg,
        error="warehouse_move_unavailable",
    )


def package_free_slots(
    session: GameAttachSession,
    package_indexes: list[int] | tuple[int, ...] | None = None,
    *,
    log: LogFn | None = None,
) -> dict[int, dict]:
    """
    Per-package free/cap/used for main/ext/warehouse.

    @author by ak
    """
    log = log or (lambda _m: None)
    idxs = list(package_indexes or DEFAULT_EXCHANGE_PACKAGE_INDEXES)
    out: dict[int, dict] = {}
    for idx in idxs:
        lay = get_package_layout(session, int(idx), log=log, with_items=True)
        out[int(idx)] = {
            "cap": int(lay.cap or 0),
            "used": int(lay.used_slots or 0),
            "free": int(lay.free_slots or 0),
            "size": int(lay.size or 0),
        }
    return out
