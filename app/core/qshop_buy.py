# -*- coding: utf-8 -*-
"""QShop (商城) buy helpers — native AUI protocol path (not NPC BuyItem / not UI click).

Live RE (2026-07-24, anonymized sample):
  - UI: Win_QShop + Win_QshopSubItem + Win_QshopCheckSingle
  - 白云熊胆丸 mall SKU tid=81997 present_id=635; bag receives 44374
  - present_info_t 14B: id,u32 tid,u32 extra,u32 tail,u16
  - CDlgQshopCheckSingle::Popup @ 0x9735E0 (thiscall ret 0x10)
    args: (arg0=192, count, check_type=0, present_info*)
  - 确认购买: CheckSingle vt[10] OnCommand @ 0xE8C690
    thiscall(this, \"Btn_Buy\") → bag_pill+N, confirm closes
  - NOT NPC BuyItem / BuyShopSlot

@author by ak
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Callable

from app.core.game_attach import GameAttachSession
from app.core.package_api import PackageActionResult, DEFAULT_REVIVE_PILL_TID

LogFn = Callable[[str], None]


def _pid_blocked(session) -> tuple[bool, str]:
    try:
        from app.core.safe_dispatch import session_blocked
        return session_blocked(session)
    except Exception:
        return False, ""


QSHOP_DLG = "Win_QShop"
QSHOP_SUBITEM = "Win_QshopSubItem"
QSHOP_CHECK = "Win_QshopCheckSingle"
CMD_BTN_BUY = "Btn_Buy"

SUBITEM_OFF_PRESENT_ID = 0x348
SUBITEM_OFF_GOODS_TID = 0x34C

CHECK_OFF_ARG0 = 0x2B0
CHECK_OFF_COUNT = 0x2B4
CHECK_OFF_CHECK_TYPE = 0x2B8

NOTE_VA_AUI_ON_COMMAND = 0x00E8C690
NOTE_VA_QSHOP_CHECK_POPUP = 0x009735E0
AUI_ON_COMMAND_VT_INDEX = 10

# Live default Popup arg0 when confirm was opened from 商品购买页
DEFAULT_POPUP_ARG0 = 192

PRESENT_INFO_SIZE = 0x0E

DEFAULT_MALL_PILL_PACK_TID = 81997
DEFAULT_MALL_PILL_BAG_TID = int(DEFAULT_REVIVE_PILL_TID)  # 44374
DEFAULT_MALL_PILL_PRESENT_ID = 635
QSHOP_PILL_GROUP_SIZE = 9999
MAX_QSHOP_PILL_GROUPS = 999
MAX_QSHOP_BUY_COUNT = 0xFFFF  # live wire count is u16
QSHOP_PILL_GROUPS_PER_ORDER = MAX_QSHOP_BUY_COUNT // QSHOP_PILL_GROUP_SIZE


def normalize_qshop_buy_count(count: int | str | None) -> int:
    """Normalize one native QShop order without the old UI-only 9999 cap."""
    try:
        value = int(count or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(MAX_QSHOP_BUY_COUNT, value))


def normalize_qshop_pill_groups(groups: int | str | None) -> int:
    """Normalize the UI group count for revive-pill purchases."""
    try:
        value = int(groups or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(MAX_QSHOP_PILL_GROUPS, value))


def qshop_pill_count_from_groups(groups: int | str | None) -> int:
    """Convert revive-pill groups to the actual native purchase count."""
    return normalize_qshop_pill_groups(groups) * QSHOP_PILL_GROUP_SIZE


def qshop_pill_batch_counts(groups: int | str | None) -> list[int]:
    """Split group orders so every native request fits the u16 wire count."""
    remaining = normalize_qshop_pill_groups(groups)
    batches: list[int] = []
    while remaining > 0:
        batch_groups = min(QSHOP_PILL_GROUPS_PER_ORDER, remaining)
        batches.append(batch_groups * QSHOP_PILL_GROUP_SIZE)
        remaining -= batch_groups
    return batches


def qshop_delivery_complete(
    requested: int, pill_delta: int, pack_delta: int = 0
) -> tuple[bool, int]:
    """Return exact-delivery status and the observed item delta."""
    delivered = int(pill_delta if pill_delta > 0 else pack_delta)
    return delivered == int(requested), delivered


@dataclass
class QShopBuySnapshot:
    ok: bool
    qshop_open: bool = False
    subitem_open: bool = False
    check_open: bool = False
    present_id: int = 0
    goods_tid: int = 0
    count: int = 0
    check_arg0: int = 0
    check_type: int = 0
    name_text: str = ""
    money_text: str = ""
    bag_pack: int = 0
    bag_pill: int = 0
    check_dlg: int = 0
    sub_dlg: int = 0
    qshop_dlg: int = 0
    message: str = ""
    error: str = ""


def _count_tid(session: GameAttachSession, tid: int, log: LogFn | None = None) -> int:
    from app.core import package_api

    total = 0
    for pkg in (2, 3, 4, 11):
        try:
            items = package_api.list_package_items(session, pkg, log=lambda m: None)
        except TypeError:
            try:
                items = package_api.list_package_items(
                    session, package=pkg, log=lambda m: None
                )
            except Exception:
                continue
        except Exception:
            continue
        for it in items or []:
            if isinstance(it, dict):
                t = int(it.get("tid") or 0)
                c = int(it.get("count") or 1)
            else:
                t = int(getattr(it, "tid", 0) or 0)
                c = int(getattr(it, "count", 1) or 1)
            if t == tid:
                total += c
    return total


def _read_ctrl_text(session, h: int, dlg: int, name: str) -> str:
    if not dlg or not h:
        return ""
    try:
        from app.core.aui_click import get_aui_dlg_item_ptr
        from app.core.remote_runtime import _rpm
    except Exception:
        return ""
    try:
        ctrl = int(get_aui_dlg_item_ptr(session, dlg, name, log=lambda m: None) or 0)
    except Exception:
        ctrl = 0
    if not ctrl:
        return ""
    cr = _rpm(h, ctrl, 0xC0) or b""
    if len(cr) < 0xBC:
        return ""
    p = struct.unpack_from("<I", cr, 0xB8)[0]
    if not (0x10000 < p < 0x7FFF0000):
        return ""
    b = _rpm(h, p, 96) or b""
    try:
        t = b.split(b"\x00\x00")[0]
        if len(t) >= 2:
            return t.decode("utf-16le", "ignore").strip("\x00")
    except Exception:
        pass
    try:
        return b.split(b"\x00")[0].decode("ascii", "ignore")
    except Exception:
        return ""


def snapshot_qshop_buy(
    session: GameAttachSession,
    *,
    pack_tid: int = DEFAULT_MALL_PILL_PACK_TID,
    pill_tid: int = DEFAULT_MALL_PILL_BAG_TID,
    log: LogFn | None = None,
) -> QShopBuySnapshot:
    """Read-only snapshot of mall buy UI + bag counts. Never buys."""
    log = log or (lambda _m: None)
    try:
        from app.core.plg_ui import get_game_ui_dlg, is_dlg_show
        from app.core.remote_runtime import _open_process, _rpm
    except Exception as e:
        return QShopBuySnapshot(ok=False, error=str(e), message="import fail")

    def _dlg(name: str) -> tuple[int, bool]:
        try:
            d = int(get_game_ui_dlg(session, name, log=lambda m: None) or 0) & 0xFFFFFFFF
        except Exception:
            d = 0
        sh = bool(is_dlg_show(session, d, log=lambda m: None)) if d else False
        return d, sh

    q, q_on = _dlg(QSHOP_DLG)
    sub, sub_on = _dlg(QSHOP_SUBITEM)
    chk, chk_on = _dlg(QSHOP_CHECK)

    present_id = goods_tid = count = arg0 = ctype = 0
    name_text = money_text = ""
    pid = int(getattr(session, "pid", 0) or 0)
    h = 0
    try:
        if pid:
            h = _open_process(pid)
        if sub and h:
            raw = _rpm(h, sub, 0x360) or b""
            if len(raw) >= SUBITEM_OFF_GOODS_TID + 4:
                present_id = struct.unpack_from("<I", raw, SUBITEM_OFF_PRESENT_ID)[0]
                goods_tid = struct.unpack_from("<I", raw, SUBITEM_OFF_GOODS_TID)[0]
        # 仅确认框真正 show 时才读 count/arg0/文案；隐藏对象上的字段是脏内存
        if chk and chk_on and h:
            raw = _rpm(h, chk, 0x2C0) or b""
            if len(raw) >= CHECK_OFF_CHECK_TYPE + 4:
                arg0 = struct.unpack_from("<I", raw, CHECK_OFF_ARG0)[0]
                count = struct.unpack_from("<I", raw, CHECK_OFF_COUNT)[0]
                ctype = struct.unpack_from("<I", raw, CHECK_OFF_CHECK_TYPE)[0]
            name_text = _read_ctrl_text(session, h, chk, "Txt_Name")
            money_text = _read_ctrl_text(session, h, chk, "Txt_Money")
            cnt_txt = _read_ctrl_text(session, h, chk, "Edt_InputNum")
            if cnt_txt.isdigit():
                count = max(int(count or 0), int(cnt_txt))
            # 脏值保护
            if arg0 < 0 or arg0 > 100000:
                arg0 = 0
            if ctype < 0 or ctype > 8:
                ctype = 0
            if count <= 0 or count > MAX_QSHOP_BUY_COUNT:
                count = 1
    finally:
        if h:
            try:
                from ctypes import windll, wintypes

                windll.kernel32.CloseHandle(wintypes.HANDLE(h))
            except Exception:
                pass

    bag_pack = _count_tid(session, int(pack_tid), log=log)
    bag_pill = _count_tid(session, int(pill_tid), log=log)
    ok = bool(q_on and (sub_on or chk_on or present_id or goods_tid))
    msg = (
        f"qshop={q_on} sub={sub_on} check={chk_on} "
        f"id={present_id} tid={goods_tid} x{count or 1} name={name_text!r} "
        f"price={money_text!r} bag_pack={bag_pack} bag_pill={bag_pill}"
    )
    log(f"qshop_buy: snapshot {msg}")
    return QShopBuySnapshot(
        ok=ok,
        qshop_open=q_on,
        subitem_open=sub_on,
        check_open=chk_on,
        present_id=int(present_id),
        goods_tid=int(goods_tid),
        count=int(count or 1),
        check_arg0=int(arg0),
        check_type=int(ctype),
        name_text=name_text,
        money_text=money_text,
        bag_pack=bag_pack,
        bag_pill=bag_pill,
        check_dlg=int(chk),
        sub_dlg=int(sub),
        qshop_dlg=int(q),
        message=msg,
    )


def pack_present_info(
    present_id: int,
    goods_tid: int,
    extra: int = 0,
    tail: int = 0,
) -> bytes:
    blob = bytearray(PRESENT_INFO_SIZE)
    struct.pack_into("<I", blob, 0, int(present_id) & 0xFFFFFFFF)
    struct.pack_into("<I", blob, 4, int(goods_tid) & 0xFFFFFFFF)
    struct.pack_into("<I", blob, 8, int(extra) & 0xFFFFFFFF)
    struct.pack_into("<H", blob, 12, int(tail) & 0xFFFF)
    return bytes(blob)


def _note_live(session: GameAttachSession, note_va: int) -> int:
    from app.core.plg_exports import DEFAULT_IMAGE_BASE, note_va_to_live

    base = int(getattr(session, "module_base", 0) or 0) or DEFAULT_IMAGE_BASE
    try:
        return int(note_va_to_live(base, int(note_va))) & 0xFFFFFFFF
    except Exception:
        return int(note_va) & 0xFFFFFFFF


def _resolve_on_command_va(session: GameAttachSession, dlg: int) -> int:
    from app.core.remote_runtime import _open_process, _rpm

    pid = int(getattr(session, "pid", 0) or 0)
    if dlg and pid:
        h = 0
        try:
            h = _open_process(pid)
            raw = _rpm(h, int(dlg) & 0xFFFFFFFF, 4) or b""
            if len(raw) >= 4:
                vt = struct.unpack_from("<I", raw, 0)[0]
                if vt:
                    slot = _rpm(h, vt + AUI_ON_COMMAND_VT_INDEX * 4, 4) or b""
                    if len(slot) >= 4:
                        fn = struct.unpack_from("<I", slot, 0)[0]
                        if 0x10000 < fn < 0x7F000000:
                            return int(fn) & 0xFFFFFFFF
        except Exception:
            pass
        finally:
            if h:
                try:
                    from ctypes import windll, wintypes

                    windll.kernel32.CloseHandle(wintypes.HANDLE(h))
                except Exception:
                    pass
    return _note_live(session, NOTE_VA_AUI_ON_COMMAND)


def _set_check_count(
    session: GameAttachSession, check_dlg: int, count: int, log: LogFn
) -> None:
    from app.core.remote_runtime import _open_process, write_process

    pid = int(getattr(session, "pid", 0) or 0)
    if not pid or not check_dlg or count <= 0:
        return
    h = 0
    try:
        h = _open_process(pid)
        write_process(
            h,
            (int(check_dlg) + CHECK_OFF_COUNT) & 0xFFFFFFFF,
            struct.pack("<I", int(count) & 0xFFFFFFFF),
        )
        log(f"qshop_buy: set count={count} @ check+0x{CHECK_OFF_COUNT:X}")
    except Exception as e:
        log(f"qshop_buy: set count failed: {e}")
    finally:
        if h:
            try:
                from ctypes import windll, wintypes

                windll.kernel32.CloseHandle(wintypes.HANDLE(h))
            except Exception:
                pass


def ensure_qshop_check_open(
    session: GameAttachSession,
    *,
    present_id: int = 0,
    goods_tid: int = 0,
    count: int = 1,
    arg0: int = DEFAULT_POPUP_ARG0,
    check_type: int = 0,
    force: bool = False,
    log: LogFn | None = None,
) -> tuple[bool, str, int]:
    """
    Ensure Win_QshopCheckSingle is shown. Uses Popup when hidden but dialog exists.
    force=True: always re-Popup with requested count (batch buy).
    Returns (ok, message, check_dlg).
    """
    log = log or (lambda _m: None)
    from app.core.plg_ui import get_game_ui_dlg, is_dlg_show
    from app.core.remote_runtime import (
        _open_process,
        remote_alloc,
        remote_free,
        write_process,
        remote_call_thiscall_x86,
    )

    snap = snapshot_qshop_buy(session, log=log)
    check = int(snap.check_dlg or 0)
    if not check:
        try:
            check = int(
                get_game_ui_dlg(session, QSHOP_CHECK, log=lambda m: None) or 0
            )
        except Exception:
            check = 0
    want = normalize_qshop_buy_count(count)
    if (
        check
        and is_dlg_show(session, check, log=lambda m: None)
        and not force
        and int(snap.count or 0) == want
    ):
        return True, "check already open", check
    if not check:
        return False, "无 Win_QshopCheckSingle 对象（请先打开商城商品页）", 0

    pid_p = int(present_id or snap.present_id or 0)
    gtid = int(goods_tid or snap.goods_tid or 0)
    if pid_p <= 0 or gtid <= 0:
        return False, f"Popup 缺 present id/tid id={pid_p} tid={gtid}", check

    cnt = normalize_qshop_buy_count(count)
    # 确认框未开时 snap.check_arg0/type 是脏内存，绝不能用
    if snap.check_open and 0 < int(snap.check_arg0 or 0) <= 100000:
        default_a0 = int(snap.check_arg0)
    else:
        default_a0 = int(DEFAULT_POPUP_ARG0)
    if snap.check_open and 0 <= int(snap.check_type or 0) <= 8:
        default_ctype = int(snap.check_type)
    else:
        default_ctype = 0
    a0 = int(arg0) if arg0 is not None and int(arg0) >= 0 else default_a0
    if a0 <= 0 or a0 > 100000:
        a0 = int(DEFAULT_POPUP_ARG0)
    ctype = int(check_type) if check_type is not None else default_ctype
    if ctype < 0 or ctype > 8:
        ctype = 0
    info = pack_present_info(pid_p, gtid)
    popup_va = _note_live(session, NOTE_VA_QSHOP_CHECK_POPUP)
    proc = int(getattr(session, "pid", 0) or 0)
    h = 0
    remote = 0
    try:
        h = _open_process(proc)
        remote = int(remote_alloc(h, 32) or 0)
        if not remote:
            return False, "remote_alloc present_info 失败", check
        write_process(h, remote, info)
        ret = remote_call_thiscall_x86(
            proc,
            int(popup_va) & 0xFFFFFFFF,
            int(check) & 0xFFFFFFFF,
            [
                int(a0) & 0xFFFFFFFF,
                int(cnt) & 0xFFFFFFFF,
                int(ctype) & 0xFFFFFFFF,
                int(remote) & 0xFFFFFFFF,
            ],
            caller_cleanup=False,  # ret 0x10
            timeout_ms=3000,
        )
        log(
            f"qshop_buy: Popup arg0={a0} x{cnt} type={ctype} "
            f"present={info.hex()} ret={int(ret or 0)}"
        )
    except TimeoutError as e:
        # remote_runtime deliberately leaves a hung CRT alive.  Popup may still
        # dereference present_info, so retain this allocation rather than free a
        # live thread's argument block.
        if remote:
            log(f"qshop_buy: Popup timeout; retain present_info=0x{remote:X}")
            remote = 0
        return False, f"Popup 调用超时: {e}", check
    except Exception as e:
        return False, f"Popup 调用失败: {e}", check
    finally:
        try:
            if remote and h:
                remote_free(h, remote)
        except Exception:
            pass
        if h:
            try:
                from ctypes import windll, wintypes

                windll.kernel32.CloseHandle(wintypes.HANDLE(h))
            except Exception:
                pass

    time.sleep(0.35)
    shown = bool(is_dlg_show(session, check, log=lambda m: None))
    if not shown:
        return False, "Popup 后确认框仍未显示", check
    return True, "Popup ok", check


def buy_qshop_present(
    session: GameAttachSession,
    *,
    count: int | None = None,
    present_id: int | None = None,
    goods_tid: int | None = None,
    pack_tid: int = DEFAULT_MALL_PILL_PACK_TID,
    pill_tid: int = DEFAULT_MALL_PILL_BAG_TID,
    popup_arg0: int | None = None,
    auto_popup: bool = True,
    force_popup: bool = False,
    log: LogFn | None = None,
    settle_s: float = 0.8,
) -> PackageActionResult:
    """
    商城购买（原生 AUI thiscall 协议路径，非鼠标点坐标）。

    1) 确认框未开 / force_popup / 数量不一致：Popup(count=N)
    2) CheckSingle.OnCommand("Btn_Buy") 一次确认（不要循环 count=1）
    3) 以背包 delta(丸 44374 / 礼包 81997) 判定成功；ret 不可信

    客户端原生入口会组 C2S；纯发包 VA 未 fail-closed 标定前禁止盲调。
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return PackageActionResult(
            ok=False,
            action="qshop_buy",
            message=f"远程不可用: {brsn}",
            error=brsn or "remote_blocked",
        )
    try:
        from app.core.state_dispatch import StateKind, warmup_session

        warmup_session(
            session,
            (StateKind.BAG, StateKind.MONEY),
            log=lambda m: log(f"qshop warmup: {m}") if m else None,
        )
    except Exception as e:
        log(f"qshop warmup skip: {e}")
    snap = snapshot_qshop_buy(
        session, pack_tid=pack_tid, pill_tid=pill_tid, log=log
    )
    has_explicit_goods = bool(
        present_id is not None
        and int(present_id or 0) > 0
        and goods_tid is not None
        and int(goods_tid or 0) > 0
    )
    if not snap.ok and not snap.qshop_open and not has_explicit_goods:
        msg = "qshop_buy: 请先打开商城（Win_QShop）并选中商品"
        log(msg)
        return PackageActionResult(
            ok=False, action="qshop_buy", message=msg, error=msg
        )

    gtid = int(goods_tid if goods_tid is not None else snap.goods_tid)
    pid_present = int(
        present_id if present_id is not None else snap.present_id
    )
    cnt = normalize_qshop_buy_count(
        count if count is not None else (snap.count or 1)
    )
    if not snap.qshop_open and not has_explicit_goods:
        msg = "qshop_buy: 请先打开商城（Win_QShop）并选中商品"
        log(msg)
        return PackageActionResult(
            ok=False, action="qshop_buy", message=msg, error=msg
        )
    if has_explicit_goods and not snap.qshop_open:
        log(
            f"qshop_buy: fixed-id direct path while QShop hidden "
            f"present={int(present_id or 0)} tid={int(goods_tid or 0)}"
        )
    if gtid <= 0 and pid_present <= 0:
        msg = f"qshop_buy: present_id/goods_tid 无效 id={pid_present} tid={gtid}"
        log(msg)
        return PackageActionResult(
            ok=False, action="qshop_buy", message=msg, error=msg
        )
    # 指定买丸时，当前商品页必须是目标礼包 tid（避免买错货）
    if int(pack_tid) > 0 and gtid > 0 and int(gtid) != int(pack_tid):
        msg = (
            f"qshop_buy: 当前商品 tid={gtid} 不是目标 {pack_tid}（白云熊胆丸礼包）；"
            f"请在商城点选「白云熊胆丸」后再买"
        )
        log(msg)
        return PackageActionResult(
            ok=False, action="qshop_buy", message=msg, error=msg
        )
    if cnt <= 0:
        msg = "qshop_buy: count 无效"
        return PackageActionResult(
            ok=False, action="qshop_buy", message=msg, error=msg
        )

    check_dlg = int(snap.check_dlg or 0)
    need_popup = (not snap.check_open) or bool(force_popup) or (
        int(snap.count or 0) != int(cnt)
    )
    if need_popup:
        if not auto_popup and not snap.check_open:
            msg = "qshop_buy: 请打开购买确认框（Win_QshopCheckSingle）"
            log(msg)
            return PackageActionResult(
                ok=False, action="qshop_buy", message=msg, error=msg
            )
        # 未开 / 强制 / 数量不一致：Popup 带 count=N（一次买满）
        use_arg0 = (
            int(popup_arg0)
            if popup_arg0 is not None
            else int(DEFAULT_POPUP_ARG0)
        )
        ok_p, pmsg, check_dlg = ensure_qshop_check_open(
            session,
            present_id=pid_present,
            goods_tid=gtid if gtid > 0 else DEFAULT_MALL_PILL_PACK_TID,
            count=cnt,
            arg0=use_arg0,
            check_type=0,
            force=True,
            log=log,
        )
        if not ok_p:
            log(f"qshop_buy: {pmsg}")
            return PackageActionResult(
                ok=False, action="qshop_buy", message=pmsg, error=pmsg
            )
        snap = snapshot_qshop_buy(
            session, pack_tid=pack_tid, pill_tid=pill_tid, log=log
        )
        check_dlg = int(snap.check_dlg or check_dlg)

    if not check_dlg:
        msg = "qshop_buy: 无确认框对象"
        return PackageActionResult(
            ok=False, action="qshop_buy", message=msg, error=msg
        )

    _set_check_count(session, check_dlg, cnt, log)

    before_pack = int(snap.bag_pack)
    before_pill = int(snap.bag_pill)
    info = pack_present_info(
        pid_present if pid_present > 0 else 0,
        gtid if gtid > 0 else DEFAULT_MALL_PILL_PACK_TID,
    )
    log(
        f"qshop_buy: exec OnCommand({CMD_BTN_BUY!r}) present={info.hex()} "
        f"count={cnt} name={snap.name_text!r} price={snap.money_text!r} "
        f"bag_pill={before_pill} bag_pack={before_pack}"
    )

    from app.core.remote_runtime import (
        remote_alloc,
        remote_free,
        write_process,
        remote_call_thiscall_x86,
        _open_process,
    )
    from app.core.plg_ui import is_dlg_show

    proc_pid = int(getattr(session, "pid", 0) or 0)
    if not proc_pid:
        msg = "qshop_buy: 无进程"
        return PackageActionResult(
            ok=False, action="qshop_buy", message=msg, error=msg
        )

    on_cmd = _resolve_on_command_va(session, check_dlg)
    if not on_cmd:
        msg = "qshop_buy: OnCommand VA 解析失败"
        return PackageActionResult(
            ok=False, action="qshop_buy", message=msg, error=msg
        )

    h = 0
    remote = 0
    ret_i = 0
    try:
        h = _open_process(proc_pid)
        remote = int(remote_alloc(h, 32) or 0)
        if not remote:
            raise OSError("remote_alloc cmd name failed")
        write_process(h, remote, CMD_BTN_BUY.encode("ascii") + b"\x00")
        ret = remote_call_thiscall_x86(
            proc_pid,
            int(on_cmd) & 0xFFFFFFFF,
            int(check_dlg) & 0xFFFFFFFF,
            [int(remote) & 0xFFFFFFFF],
            caller_cleanup=False,
            timeout_ms=3000,
        )
        ret_i = int(ret or 0)
    except TimeoutError as e:
        # See Popup above: command text can still be read by a CRT that did not
        # finish its grace wait, therefore do not free it on timeout.
        if remote:
            log(f"qshop_buy: OnCommand timeout; retain cmd=0x{remote:X}")
            remote = 0
        msg = f"qshop_buy: OnCommand 调用超时: {e}"
        return PackageActionResult(
            ok=False, action="qshop_buy", message=msg, error=str(e)
        )
    except Exception as e:
        msg = f"qshop_buy: OnCommand 调用失败: {e}"
        log(msg)
        return PackageActionResult(
            ok=False, action="qshop_buy", message=msg, error=str(e)
        )
    finally:
        try:
            if remote and h:
                remote_free(h, remote)
        except Exception:
            pass
        if h:
            try:
                from ctypes import windll, wintypes

                windll.kernel32.CloseHandle(wintypes.HANDLE(h))
            except Exception:
                pass

    time.sleep(max(0.2, float(settle_s)))
    after_pack = _count_tid(session, int(pack_tid), log=log)
    after_pill = _count_tid(session, int(pill_tid), log=log)
    d_pack = after_pack - before_pack
    d_pill = after_pill - before_pill
    try:
        still_check = bool(is_dlg_show(session, check_dlg, log=lambda m: None))
    except Exception:
        still_check = True

    ok, delivered = qshop_delivery_complete(cnt, d_pill, d_pack)
    msg = (
        f"qshop_buy OnCommand({CMD_BTN_BUY}) ret={ret_i} "
        f"d_pill={d_pill} d_pack={d_pack} still_check={still_check} "
        f"pill={after_pill} pack={after_pack} want={cnt} "
        f"id={pid_present} tid={gtid} name={snap.name_text!r}"
    )
    if delivered > 0 and delivered != int(cnt):
        msg += f" (部分到账：服端实发{delivered}/请求{cnt})"
    log(f"qshop_buy: {msg}")
    if delivered <= 0:
        return PackageActionResult(
            ok=False,
            action="qshop_buy",
            message=msg + " (无背包变化)",
            item={"requested": int(cnt), "delivered": delivered},
            error="no_bag_delta",
            ret=ret_i,
        )
    if not ok:
        return PackageActionResult(
            ok=False,
            action="qshop_buy",
            message=msg,
            item={"requested": int(cnt), "delivered": delivered},
            error="partial_delivery",
            ret=ret_i,
        )
    try:
        from app.core.package_api import invalidate_bag_cache

        invalidate_bag_cache(session)
    except Exception:
        pass
    return PackageActionResult(
        ok=True,
        action="qshop_buy",
        message=msg,
        item={"requested": int(cnt), "delivered": delivered},
        ret=ret_i,
    )
