# -*- coding: utf-8 -*-
"""
AUI dialog helpers for captcha (CDlgActivityQuestion) 鈥?memory + UI-thread.

Live-mapped CDlgActivityQuestion (x86, 2026-07-16):
  dlg+0x288 Img_Question   (single image control holding 2x4 animal grid)
  dlg+0x28C Lab_Tip
  dlg+0x290 Edt_Input
  dlg+0x294 Btn_Ok
  dlg+0x298 Btn_Cancel
  dlg+0x29C Title

There are NO 8 separate Img_* child controls; cells are geometry slices of
Img_Question (2 rows x 4 cols, row-major positions 1..8).

Submit (Btn_Ok command handler) note VA 0x8CD2F0:
  thiscall reads Edt_Input text via vfunc+0x44 then packet 0xCC99E0.
  Bridge CMD_AQ_SUBMIT runs it on the UI thread (minimized-safe).

Background click fallbacks (human hold + point jitter when humanize=True):
  1) bridge CMD_UI_CLICK (PostMessage on UI thread, mode=hold_ms)
  2) external post/send message
  3) optional cursor (explicit allow only)

Confirm prefers Btn_Ok UI click so image selection latches; AQ_SUBMIT optional.

@author by ak
"""
from __future__ import annotations

import struct
import time
from dataclasses import asdict, dataclass
from typing import Callable

from app.core.human_input import human_hold_ms, human_slide_path, jitter_around, point_in_rect
from app.core.plg_ui import (
    AUI_DLG_OFF_H,
    AUI_DLG_OFF_W,
    AUI_DLG_OFF_X,
    AUI_DLG_OFF_Y,
    CAPTCHA_DIALOG_NAME_CANDIDATES,
    get_captcha_dlg_rect,
    get_game_ui_dlg,
    is_captcha_dialog_open,
    is_dlg_show,
)
from app.core.win_capture import click_client_smart

LogFn = Callable[[str], None]


@dataclass
class AuiCtrlRect:
    """
    Client-space AUI control rect (same field layout readers as AUIDialog).

    Positional order used by tests: ok, name, ctrl_ptr, x, y, w, h.
    @author by ak
    """

    ok: bool
    name: str = ""
    ctrl_ptr: int = 0
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    error: str | None = None

    @property
    def center(self) -> tuple[int, int]:
        """Integer client center. @author by ak"""
        return (int(self.x) + int(self.w) // 2, int(self.y) + int(self.h) // 2)

    def to_dict(self) -> dict:
        """Serialize for logs. @author by ak"""
        return asdict(self)


# AUIDialog::GetDlgItem(const char*) thiscall 鈥?note absolute VA at preferred base.
NOTE_VA_AUI_GET_DLG_ITEM = 0x00E918F0
DEFAULT_IMAGE_BASE = 0x400000
# CDlgActivityQuestion Btn_Ok command submit thiscall.
NOTE_VA_AQ_SUBMIT = 0x008CD2F0

# CDlgActivityQuestion cached control offsets (from init bind @ 0x8CD120).
# Win_Question3D live often has null/0-size cached slots 鈥?use GetDlgItem first.
AQ_OFF_IMG_QUESTION = 0x288
AQ_OFF_LAB_TIP = 0x28C
AQ_OFF_EDT_INPUT = 0x290
AQ_OFF_BTN_OK = 0x294
AQ_OFF_BTN_CANCEL = 0x298
AQ_OFF_TITLE = 0x29C

CAPTCHA_CTRL_BTN_OK = "Btn_Ok"
# Live Win_Question3D may bind alternate button labels; try in order.
CAPTCHA_CTRL_BTN_OK_NAMES = (
    CAPTCHA_CTRL_BTN_OK,
    "Btn_OK",
    "btn_ok",
    "Btn_Confirm",
    "Btn_Sure",
)
CAPTCHA_CTRL_IMG_QUESTION = "Img_Question"
CAPTCHA_CTRL_BTN_CANCEL = "Btn_Cancel"
CAPTCHA_CTRL_BTN_CLOSE = "Btn_Close"
CAPTCHA_DLG_NAME = "Win_ActivityQuestion"
# Prefer primary, then other question dialogs seen live (e.g. Win_Question3D).
CAPTCHA_DLG_NAMES = CAPTCHA_DIALOG_NAME_CANDIDATES
CAPTCHA_CANCEL_CTRL_NAMES = (
    CAPTCHA_CTRL_BTN_CANCEL,
    CAPTCHA_CTRL_BTN_CLOSE,
    "Btn_Quit",
    "Btn_Exit",
)

# Slight inset avoids chrome edges on the picture control.
_GRID_INSET_X = 0.02
_GRID_INSET_Y = 0.02
_GRID_COLS = 4
_GRID_ROWS = 2

# AUIDialog::IsShow = byte[this+0x94]; Show(bool,a2,a3) = vtable+0x1C (ret 0xC).
AUI_DLG_ISSHOW_OFF = 0x94
AUI_DLG_SHOW_VTABLE_OFF = 0x1C


def _read_u32_aui(session, addr: int) -> int:
    """Read remote u32. @author by ak"""
    addr = int(addr) & 0xFFFFFFFF
    if not addr:
        return 0
    pm = getattr(session, "pm", None)
    if pm is None:
        return 0
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(pm.process_handle, addr, 4)
        return int(struct.unpack_from("<I", raw, 0)[0]) & 0xFFFFFFFF
    except Exception:
        return 0


def hide_aui_dialog(
    session,
    dlg_ptr: int,
    *,
    force: bool = False,
    show_args: tuple[int, ...] | list[int] | None = None,
    try_variants: bool = False,
    log: LogFn | None = None,
) -> dict:
    """
    Hide AUIDialog via live vtable Show(...) at slot +0x1C (ret 0xC, 3 args).

    Native patterns:
      - captcha Btn_Ok / ToggleDlgShow: Show(0, 0, 1)
      - stuck Win_Prgs2 progress bar (live 2026-07-20): Show(0,0,1) keeps bar;
        Show(0) / Show(0,0,0) actually hides it.

    Use show_args for a fixed call; set try_variants=True to probe safe
    arg sets until IsDlgShow becomes false.
    @author by ak
    """
    log = log or (lambda _m: None)
    dlg = int(dlg_ptr) & 0xFFFFFFFF
    out: dict = {
        "ok": False,
        "dlg_ptr": dlg,
        "show_fn": 0,
        "shown_before": False,
        "shown_after": False,
        "error": None,
        "method": "vtable_Show",
        "args": None,
        "tried": [],
    }
    if not dlg:
        out["error"] = "dlg_ptr=0"
        return out
    try:
        out["shown_before"] = bool(is_dlg_show(session, dlg, log=lambda _m: None))
    except Exception:
        out["shown_before"] = False
    if not out["shown_before"] and not force:
        out["ok"] = True
        out["shown_after"] = False
        out["method"] = "already_hidden"
        return out
    try:
        vt = _read_u32_aui(session, dlg)
        show_fn = (
            _read_u32_aui(session, (vt + AUI_DLG_SHOW_VTABLE_OFF) & 0xFFFFFFFF)
            if vt
            else 0
        )
        out["show_fn"] = int(show_fn) & 0xFFFFFFFF
        if not vt or not show_fn:
            out["error"] = f"bad vtable vt=0x{vt:X} show=0x{show_fn:X}"
            return out
        from app.core.remote_runtime import remote_call_thiscall_x86

        pid = int(getattr(session, "pid", 0) or 0)

        # Always pass 3 stack args (Show ret 0xC). Pad short tuples with 0.
        def _call(args3: tuple[int, int, int]) -> None:
            remote_call_thiscall_x86(
                pid,
                int(show_fn) & 0xFFFFFFFF,
                dlg,
                [int(args3[0]), int(args3[1]), int(args3[2])],
                caller_cleanup=False,
                timeout_ms=3000,
            )

        def _pad(a: tuple[int, ...] | list[int]) -> tuple[int, int, int]:
            seq = [int(x) for x in list(a)[:3]]
            while len(seq) < 3:
                seq.append(0)
            return int(seq[0]), int(seq[1]), int(seq[2])

        variants: list[tuple[int, int, int]] = []
        if show_args is not None:
            variants.append(_pad(show_args))
        if try_variants or show_args is None:
            # Progress bar: (0,0,0) live-ok; captcha: (0,0,1) native.
            # Do NOT lead with (0,0,1) when try_variants 鈥?it re-holds Prgs2.
            for cand in ((0, 0, 0), (0, 0, 1), (0, 1, 0)):
                if cand not in variants:
                    variants.append(cand)
        if not variants:
            variants.append((0, 0, 0))

        last_err = None
        for args3 in variants:
            try:
                _call(args3)
                out["tried"].append({"args": args3, "ok": True})
            except Exception as e:
                last_err = str(e)
                out["tried"].append({"args": args3, "ok": False, "error": str(e)})
                log(f"hide_aui_dialog 0x{dlg:X} Show{args3} err: {e}")
                continue
            time.sleep(0.08)
            try:
                still = bool(is_dlg_show(session, dlg, log=lambda _m: None))
            except Exception:
                still = True
            out["args"] = args3
            out["method"] = f"vtable_Show{args3}"
            out["shown_after"] = still
            if not still:
                out["ok"] = True
                out["error"] = None
                return out
        out["shown_after"] = True
        out["ok"] = False
        out["error"] = last_err or (
            "Show issued but still shown; tried "
            + ",".join(str(v.get("args")) for v in out["tried"])
        )
        return out
    except Exception as e:
        out["error"] = f"Show(false) err: {e}"
        log(f"hide_aui_dialog 0x{dlg:X} err: {e}")
        return out



def _read_ptr(session, addr: int) -> int:
    """
    Read remote u32 pointer.

    @author by ak
    """
    pm = getattr(session, "pm", None)
    if pm is None or not addr:
        return 0
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(pm.process_handle, int(addr) & 0xFFFFFFFF, 4)
        return struct.unpack_from("<I", raw, 0)[0]
    except Exception:
        return 0


def get_aui_dlg_item_ptr(
    session,
    dlg_ptr: int,
    name: str,
    *,
    log: LogFn | None = None,
) -> int:
    """
    Resolve control pointer: GetDlgItem first, then cached CDlgActivityQuestion offs.

    Live Win_Question3D often has null/0-size cached Btn_Cancel at +0x298.
    @author by ak
    """
    log = log or (lambda _m: None)
    dlg = int(dlg_ptr) & 0xFFFFFFFF
    if not dlg:
        return 0
    name = (name or "").strip()
    if not name:
        return 0

    # 1) Live GetDlgItem(this, name) 鈥?works for Question3D / ActivityQuestion.
    try:
        from app.core.remote_runtime import remote_call_thiscall_x86

        base = int(getattr(session, "module_base", 0) or 0)
        pid = int(getattr(session, "pid", 0) or 0)
        if base and pid:
            va = base + (NOTE_VA_AUI_GET_DLG_ITEM - DEFAULT_IMAGE_BASE)
            payload = name.encode("ascii", "ignore") + b"\x00"
            from app.core.plg_ui import remote_alloc_bytes, remote_free

            h, remote, _sz = remote_alloc_bytes(pid, payload)
            try:
                # MSVC thiscall: callee cleans stack (ret 4) 鈫?caller_cleanup=False.
                ret = remote_call_thiscall_x86(
                    pid,
                    va,
                    dlg,
                    [int(remote) & 0xFFFFFFFF],
                    caller_cleanup=False,
                    timeout_ms=10000,
                    skip_scene_gate=True,
                )
                ptr = int(ret) & 0xFFFFFFFF
                if ptr:
                    return ptr
            finally:
                try:
                    remote_free(h, remote)
                except Exception:
                    pass
    except Exception as e:
        log(f"GetDlgItem({name!r}) err: {e}")

    # 2) Cached offsets only for known ActivityQuestion slots.
    off_map = {
        CAPTCHA_CTRL_IMG_QUESTION: AQ_OFF_IMG_QUESTION,
        "Lab_Tip": AQ_OFF_LAB_TIP,
        "Edt_Input": AQ_OFF_EDT_INPUT,
        CAPTCHA_CTRL_BTN_OK: AQ_OFF_BTN_OK,
        CAPTCHA_CTRL_BTN_CANCEL: AQ_OFF_BTN_CANCEL,
        "Title": AQ_OFF_TITLE,
    }
    off = off_map.get(name)
    if off is None:
        return 0
    return int(_read_ptr(session, dlg + off)) & 0xFFFFFFFF


def get_captcha_ctrl_rect(
    session,
    ctrl_name: str,
    *,
    dlg_name: str | None = None,
    names: tuple[str, ...] | list[str] | None = None,
    log: LogFn | None = None,
) -> AuiCtrlRect:
    """
    Resolve one captcha control client rect by control name.

    @author by ak
    """
    log = log or (lambda _m: None)
    dlg = get_activity_question_dlg(
        session, dlg_name=dlg_name, names=names, log=log
    )
    if not dlg:
        return AuiCtrlRect(ok=False, name=ctrl_name, error="dialog not shown")
    btn = get_aui_dlg_item_ptr(session, dlg, ctrl_name, log=log)
    if not btn:
        return AuiCtrlRect(ok=False, name=ctrl_name, error=f"{ctrl_name} ptr null")
    return read_aui_ctrl_rect(session, btn, name=ctrl_name, log=log)


def get_captcha_btn_ok_rect(
    session,
    *,
    dlg_name: str | None = None,
    names: tuple[str, ...] | list[str] | None = None,
    log: LogFn | None = None,
) -> AuiCtrlRect:
    """
    Resolve ActivityQuestion Btn_Ok client rect from memory.

    Live Win_Question3D often returns Btn_Ok ptr with 0x0 size. Try:
      1) GetDlgItem by several button names
      2) Derive a synthetic rect from the dialog frame (50%/90% 纭畾)
    Caller still keeps dialog-geometry click fallback when this fails.

    @author by ak
    """
    log = log or (lambda _m: None)
    last = AuiCtrlRect(ok=False, name=CAPTCHA_CTRL_BTN_OK, error="dialog not shown")
    for ctrl in CAPTCHA_CTRL_BTN_OK_NAMES:
        rect = get_captcha_ctrl_rect(
            session, ctrl, dlg_name=dlg_name, names=names, log=log
        )
        last = rect
        if rect.ok:
            return rect
        # Ptr found but size 0x0 鈥?keep for diagnostics, try next name.
        if rect.ctrl_ptr and (rect.w <= 2 or rect.h <= 2):
            log(
                f"get_captcha_btn_ok_rect {ctrl}: ptr=0x{rect.ctrl_ptr:X} "
                f"bad size {rect.w}x{rect.h}"
            )

    # Derive from dialog frame rect when control size is broken.
    try:
        dlg = get_activity_question_dlg(
            session, dlg_name=dlg_name, names=names, log=log
        )
        if dlg:
            drect = read_aui_ctrl_rect(session, dlg, name="captcha_dlg", log=log)
            if drect.ok and drect.w > 40 and drect.h > 40:
                bw = max(72, min(140, drect.w // 6))
                bh = max(24, min(40, drect.h // 12))
                cx = drect.x + int(drect.w * 0.50)
                cy = drect.y + int(drect.h * 0.925)
                derived = AuiCtrlRect(
                    ok=True,
                    name="Btn_Ok@dlg_ratio",
                    ctrl_ptr=int(last.ctrl_ptr or dlg) & 0xFFFFFFFF,
                    x=int(cx - bw // 2),
                    y=int(cy - bh // 2),
                    w=int(bw),
                    h=int(bh),
                )
                log(
                    f"get_captcha_btn_ok_rect derived from dlg "
                    f"({drect.x},{drect.y},{drect.w}x{drect.h}) -> "
                    f"({derived.x},{derived.y},{derived.w}x{derived.h})"
                )
                return derived
    except Exception as e:
        log(f"get_captcha_btn_ok_rect derive err: {e}")
    return last


def get_captcha_btn_cancel_rect(
    session,
    *,
    dlg_name: str | None = None,
    names: tuple[str, ...] | list[str] | None = None,
    log: LogFn | None = None,
) -> AuiCtrlRect:
    """
    Resolve cancel/close control rect (Btn_Cancel / Btn_Close / 鈥?.

    @author by ak
    """
    log = log or (lambda _m: None)
    last = AuiCtrlRect(ok=False, name=CAPTCHA_CTRL_BTN_CANCEL, error="dialog not shown")
    for ctrl in CAPTCHA_CANCEL_CTRL_NAMES:
        rect = get_captcha_ctrl_rect(
            session, ctrl, dlg_name=dlg_name, names=names, log=log
        )
        last = rect
        if rect.ok:
            return rect
    return last


def get_captcha_img_question_rect(
    session,
    *,
    dlg_name: str | None = None,
    names: tuple[str, ...] | list[str] | None = None,
    log: LogFn | None = None,
) -> AuiCtrlRect:
    """
    Resolve ActivityQuestion Img_Question client rect from memory.

    @author by ak
    """
    return get_captcha_ctrl_rect(
        session,
        CAPTCHA_CTRL_IMG_QUESTION,
        dlg_name=dlg_name,
        names=names,
        log=log,
    )


def read_aui_ctrl_rect(
    session,
    ctrl_ptr: int,
    *,
    name: str = "",
    log: LogFn | None = None,
) -> AuiCtrlRect:
    """
    Read AUIObject client rect (same layout fields as AUIDialog).

    @author by ak
    """
    log = log or (lambda _m: None)
    ptr = int(ctrl_ptr) & 0xFFFFFFFF
    if not ptr:
        return AuiCtrlRect(ok=False, name=name, error="null ctrl")
    pm = getattr(session, "pm", None)
    if pm is None:
        return AuiCtrlRect(ok=False, name=name, error="no pymem")
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(pm.process_handle, ptr, 0xC0)
        x = struct.unpack_from("<i", raw, AUI_DLG_OFF_X)[0]
        y = struct.unpack_from("<i", raw, AUI_DLG_OFF_Y)[0]
        w = struct.unpack_from("<i", raw, AUI_DLG_OFF_W)[0]
        h = struct.unpack_from("<i", raw, AUI_DLG_OFF_H)[0]
        if w <= 2 or h <= 2 or w > 4096 or h > 4096:
            return AuiCtrlRect(
                ok=False,
                name=name,
                ctrl_ptr=ptr,
                x=x,
                y=y,
                w=w,
                h=h,
                error=f"bad rect {w}x{h}",
            )
        return AuiCtrlRect(ok=True, name=name, ctrl_ptr=ptr, x=x, y=y, w=w, h=h)
    except Exception as e:
        log(f"read_aui_ctrl_rect err: {e}")
        return AuiCtrlRect(ok=False, name=name, ctrl_ptr=ptr, error=str(e))


def get_activity_question_dlg(
    session,
    *,
    dlg_name: str | None = None,
    names: tuple[str, ...] | list[str] | None = None,
    log: LogFn | None = None,
) -> int:
    """
    Resolve shown captcha dialog pointer (ActivityQuestion / Question3D鈥?, or 0.

    When dlg_name is set, only that name is tried. Otherwise scan names
    (default CAPTCHA_DLG_NAMES) via is_captcha_dialog_open.

    @author by ak
    """
    log = log or (lambda _m: None)
    if session is None:
        return 0
    if dlg_name:
        dlg = get_game_ui_dlg(session, dlg_name, log=log)
        if not dlg or not is_dlg_show(session, dlg, log=log):
            return 0
        return int(dlg) & 0xFFFFFFFF
    cands = tuple(names) if names else CAPTCHA_DLG_NAMES
    hit = is_captcha_dialog_open(session, names=cands, log=log)
    if hit.shown and hit.dlg_ptr:
        return int(hit.dlg_ptr) & 0xFFFFFFFF
    return 0


def captcha_cell_rect_from_img(
    img_rect: AuiCtrlRect,
    index_1based: int,
) -> AuiCtrlRect:
    """
    Slice one of 8 cells (2x4 row-major) from Img_Question rect.

    @author by ak
    """
    try:
        idx = int(index_1based) - 1
    except Exception:
        return AuiCtrlRect(ok=False, name=f"cell{index_1based}", error="bad index")
    if idx < 0 or idx >= _GRID_COLS * _GRID_ROWS:
        return AuiCtrlRect(ok=False, name=f"cell{index_1based}", error="index out of range")
    if not img_rect.ok or img_rect.w < 8 or img_rect.h < 8:
        return AuiCtrlRect(
            ok=False,
            name=f"cell{index_1based}",
            error=img_rect.error or "bad img rect",
        )
    ix = float(img_rect.x) + float(img_rect.w) * _GRID_INSET_X
    iy = float(img_rect.y) + float(img_rect.h) * _GRID_INSET_Y
    iw = float(img_rect.w) * (1.0 - 2.0 * _GRID_INSET_X)
    ih = float(img_rect.h) * (1.0 - 2.0 * _GRID_INSET_Y)
    col = idx % _GRID_COLS
    row = idx // _GRID_COLS
    cw = iw / float(_GRID_COLS)
    ch = ih / float(_GRID_ROWS)
    x = int(round(ix + col * cw))
    y = int(round(iy + row * ch))
    w = int(round(cw))
    h = int(round(ch))
    return AuiCtrlRect(
        ok=True,
        name=f"cell{index_1based}",
        ctrl_ptr=img_rect.ctrl_ptr,
        x=x,
        y=y,
        w=max(1, w),
        h=max(1, h),
    )


def get_captcha_cell_rect(
    session,
    index_1based: int,
    *,
    dlg_name: str | None = None,
    names: tuple[str, ...] | list[str] | None = None,
    log: LogFn | None = None,
) -> AuiCtrlRect:
    """
    Memory-backed cell rect for captcha position 1..8.

    @author by ak
    """
    img = get_captcha_img_question_rect(
        session, dlg_name=dlg_name, names=names, log=log
    )
    if not img.ok:
        return AuiCtrlRect(
            ok=False, name=f"cell{index_1based}", error=img.error or "no Img_Question"
        )
    return captcha_cell_rect_from_img(img, index_1based)


def get_captcha_cell_center(
    session,
    index_1based: int,
    *,
    dlg_name: str | None = None,
    names: tuple[str, ...] | list[str] | None = None,
    log: LogFn | None = None,
) -> tuple[int, int] | None:
    """
    Client center of captcha cell 1..8 from memory Img_Question.

    @author by ak
    """
    r = get_captcha_cell_rect(
        session, index_1based, dlg_name=dlg_name, names=names, log=log
    )
    if not r.ok:
        return None
    return r.center


def _bridge_for_session(session, *, log: LogFn):
    """
    Open existing bridge for session pid (no force reinject).

    @author by ak
    """
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        return None
    try:
        from app.core.xajh_bridge import ensure_bridge

        return ensure_bridge(pid, log=log, inject_if_needed=False)
    except Exception as e:
        log(f"aui_click bridge open err: {e}")
        return None


def click_client_via_bridge(
    session,
    hwnd: int,
    cx: int,
    cy: int,
    *,
    hold_ms: int = 0,
    log: LogFn | None = None,
) -> bool:
    """
    UI-thread PostMessage click via bridge CMD_UI_CLICK.

    hold_ms: mouse down hold on UI thread (0 = bridge default).
    @author by ak
    """
    log = log or (lambda _m: None)
    br = _bridge_for_session(session, log=log)
    if br is None:
        return False
    try:
        r = br.ui_click(
            int(cx),
            int(cy),
            hwnd=int(hwnd) or None,
            timeout_ms=max(2500, int(hold_ms) + 2000),
            hold_ms=int(hold_ms),
        )
        log(
            f"bridge UI_CLICK ({cx},{cy}) hold_ms={int(hold_ms)} "
            f"ok={r.ok} ret={r.ret} note={r.note!r} err={r.error}"
        )
        return bool(r.ok)
    except Exception as e:
        log(f"bridge UI_CLICK err: {e}")
        return False
    finally:
        try:
            br.close()
        except Exception:
            pass


def click_client_bg(
    session,
    hwnd: int,
    cx: int,
    cy: int,
    *,
    prefer_bridge: bool = True,
    prefer_post: bool = True,
    allow_cursor: bool = False,
    prefer_real_mouse: bool | None = None,
    humanize: bool = True,
    hold_ms: int | None = None,
    hold_min_ms: int = 70,
    hold_max_ms: int = 160,
    path_from: tuple[int, int] | None = None,
    slide: bool = False,
    slide_min_steps: int = 4,
    slide_max_steps: int = 10,
    slide_duration_min_s: float = 0.08,
    slide_duration_max_s: float = 0.28,
    stop_event=None,
    log: LogFn | None = None,
) -> bool:
    """
    Background-first client click: bridge UI thread -> post/send -> optional cursor.

    Default: inject/bridge only with humanized hold timing (no real mouse, no slide).
    prefer_real_mouse / slide only when caller explicitly enables them.
    @author by ak
    """
    log = log or (lambda _m: None)
    if hold_ms is None:
        hold_ms = (
            human_hold_ms(int(hold_min_ms), int(hold_max_ms))
            if humanize
            else max(40, int((int(hold_min_ms) + int(hold_max_ms)) // 2))
        )
    hold_ms = max(20, int(hold_ms))
    # Explicit prefer_real_mouse wins; else only if allow_cursor AND prefer_real True
    if prefer_real_mouse is None:
        use_real = False
    else:
        use_real = bool(prefer_real_mouse)

    path_points = None
    path_duration_s = 0.0
    if humanize and slide and path_from is not None and len(path_from) >= 2:
        try:
            path_points, path_duration_s = human_slide_path(
                int(path_from[0]),
                int(path_from[1]),
                int(cx),
                int(cy),
                min_steps=int(slide_min_steps),
                max_steps=int(slide_max_steps),
                duration_min_s=float(slide_duration_min_s),
                duration_max_s=float(slide_duration_max_s),
            )
            if use_real:
                # Real mouse will move the OS cursor along path_points; do not
                # also PostMessage a background trail (double path / wasted time).
                log(
                    f"click_client_bg real-slide prepared "
                    f"({path_from[0]},{path_from[1]})->({cx},{cy}) "
                    f"pts={len(path_points)} dur={path_duration_s:.3f}s "
                    f"hold_ms={hold_ms}"
                )
            else:
                from app.core.win_capture import post_path_client

                post_path_client(
                    int(hwnd),
                    path_points,
                    duration_s=path_duration_s,
                    stop_event=stop_event,
                    log=log,
                )
                log(
                    f"click_client_bg slide ({path_from[0]},{path_from[1]})->"
                    f"({cx},{cy}) pts={len(path_points)} dur={path_duration_s:.3f}s "
                    f"hold_ms={hold_ms}"
                )
        except Exception as e:
            log(f"click_client_bg slide err: {e}")
            path_points = None

    # Optional real OS mouse (off by default)
    if use_real:
        try:
            from app.core.win_capture import click_client_real_mouse, ensure_foreground

            # Need focus for visible cursor path + OS click to land on game.
            ensure_foreground(
                int(hwnd),
                retries=3,
                settle_s=0.05,
                force=not path_points,  # always force on teleport click
                log=log,
            )
            if click_client_real_mouse(
                int(hwnd),
                int(cx),
                int(cy),
                down_hold_s=hold_ms / 1000.0,
                path_points_client=path_points,
                path_duration_s=path_duration_s,
                focus=True,
                stop_event=stop_event,
                log=log,
            ):
                return True
            log("click_client_bg: real mouse miss, fallback inject")
        except Exception as e:
            log(f"click_client_bg real mouse err: {e}")

    if prefer_bridge and session is not None:
        if click_client_via_bridge(
            session, hwnd, cx, cy, hold_ms=hold_ms, log=log
        ):
            return True
    return click_client_smart(
        hwnd,
        cx,
        cy,
        prefer_post=prefer_post,
        allow_cursor=bool(allow_cursor) and not use_real,
        down_hold_s=hold_ms / 1000.0,
        path_points=None,  # no slide on post fallback unless already posted
        path_duration_s=0.0,
        stop_event=stop_event,
        log=log,
    )

def human_point_from_rect(
    rect: AuiCtrlRect,
    *,
    inset_frac: float = 0.18,
    center_bias: float = 0.40,
) -> tuple[int, int] | None:
    """
    Human-like click point inside an AUI control rect.

    @author by ak
    """
    if not rect.ok or rect.w < 4 or rect.h < 4:
        return None
    return point_in_rect(
        rect.x,
        rect.y,
        rect.w,
        rect.h,
        inset_frac=inset_frac,
        center_bias=center_bias,
    )


def captcha_btn_ok_hit_rect(
    rect: AuiCtrlRect,
    *,
    real_mouse_y_offset_px: int = 0,
) -> AuiCtrlRect:
    """Return the effective Btn_Ok hit rect for the selected input path."""
    offset_y = max(-48, min(48, int(real_mouse_y_offset_px or 0)))
    if not rect.ok or offset_y == 0:
        return rect
    return AuiCtrlRect(
        ok=True,
        name=rect.name,
        ctrl_ptr=rect.ctrl_ptr,
        x=rect.x,
        y=rect.y + offset_y,
        w=rect.w,
        h=rect.h,
        error=rect.error,
    )


def submit_activity_question(
    session,
    *,
    dlg_name: str | None = None,
    names: tuple[str, ...] | list[str] | None = None,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> bool:
    """
    Pure AUI Btn_Ok submit via bridge thiscall 0x8CD2F0 on UI thread.

    @author by ak
    """
    log = log or (lambda _m: None)
    dlg = get_activity_question_dlg(
        session, dlg_name=dlg_name, names=names, log=log
    )
    if not dlg:
        log("submit_activity_question: dialog not shown")
        return False
    br = _bridge_for_session(session, log=log)
    if br is None:
        log("submit_activity_question: no bridge")
        return False
    try:
        r = br.aq_submit(dlg, hwnd=int(hwnd) or None, timeout_ms=3000)
        log(
            f"bridge AQ_SUBMIT dlg=0x{dlg:X} ok={r.ok} ret={r.ret} "
            f"note={r.note!r} err={r.error}"
        )
        return bool(r.ok)
    except Exception as e:
        log(f"bridge AQ_SUBMIT err: {e}")
        return False
    finally:
        try:
            br.close()
        except Exception:
            pass


def click_captcha_btn_ok(
    session,
    hwnd: int,
    *,
    prefer_bridge: bool = True,
    prefer_post: bool = True,
    allow_cursor: bool = False,
    pure_submit: bool = False,
    humanize: bool = True,
    names: tuple[str, ...] | list[str] | None = None,
    path_from: tuple[int, int] | None = None,
    slide: bool = False,
    slide_min_steps: int = 8,
    slide_max_steps: int = 22,
    slide_duration_min_s: float = 0.25,
    slide_duration_max_s: float = 0.85,
    hold_min_ms: int = 60,
    hold_max_ms: int = 220,
    prefer_real_mouse: bool = False,
    real_mouse_y_offset_px: int = 0,
    stop_event=None,
    log: LogFn | None = None,
) -> bool:
    """
    Confirm captcha: prefer Btn_Ok UI click (selection state), optional AQ_SUBMIT.

    pure_submit=False by default so image captcha selection is latched first.
    When True, try thiscall AQ_SUBMIT first (faster, less human-like).
    Optional path_from/slide carries the last cell trail into the confirm button.

    Btn_Ok resolution order:
      1) live control rect (multi-name)
      2) dialog-frame derived rect (50%/90%)
      3) dialog geometry 纭畾 click (crop/box fallback)
      4) AQ_SUBMIT thiscall last resort (after selection clicks)

    @author by ak
    """
    log = log or (lambda _m: None)
    if pure_submit and prefer_bridge and session is not None:
        if submit_activity_question(session, names=names, hwnd=hwnd, log=log):
            log("click_captcha_btn_ok: pure AQ_SUBMIT ok")
            return True
        log("click_captcha_btn_ok: AQ_SUBMIT miss, fall back to Btn_Ok click")

    rect = get_captcha_btn_ok_rect(session, names=names, log=log)
    hit_rect = captcha_btn_ok_hit_rect(
        rect,
        real_mouse_y_offset_px=(
            int(real_mouse_y_offset_px or 0) if prefer_real_mouse else 0
        ),
    )
    if hit_rect.ok:
        pt = human_point_from_rect(hit_rect, inset_frac=0.22, center_bias=0.45)
        if pt is None:
            cx, cy = hit_rect.center
        else:
            cx, cy = pt
        if humanize:
            cx, cy = jitter_around(cx, cy, radius_x=2, radius_y=1)
            cx = max(hit_rect.x + 1, min(hit_rect.x + hit_rect.w - 2, cx))
            cy = max(hit_rect.y + 1, min(hit_rect.y + hit_rect.h - 2, cy))
        log(
            f"click_captcha_btn_ok name={rect.name!r} ptr=0x{rect.ctrl_ptr:X} "
            f"rect=({rect.x},{rect.y},{rect.w}x{rect.h}) "
            f"hit_rect=({hit_rect.x},{hit_rect.y},{hit_rect.w}x{hit_rect.h}) "
            f"real_y_offset={hit_rect.y - rect.y} click=({cx},{cy})"
        )
        ok_click = click_client_bg(
            session,
            hwnd,
            cx,
            cy,
            prefer_bridge=prefer_bridge,
            prefer_post=prefer_post,
            allow_cursor=allow_cursor,
            prefer_real_mouse=bool(prefer_real_mouse),
            humanize=humanize,
            hold_min_ms=int(hold_min_ms),
            hold_max_ms=int(hold_max_ms),
            path_from=path_from,
            slide=bool(slide and path_from is not None),
            slide_min_steps=int(slide_min_steps),
            slide_max_steps=int(slide_max_steps),
            slide_duration_min_s=float(slide_duration_min_s),
            slide_duration_max_s=float(slide_duration_max_s),
            stop_event=stop_event,
            log=log,
        )
        if ok_click:
            return True
        log("click_captcha_btn_ok: control/derived click returned False; try geom")

    # Win_Question3D often has Btn_Ok rect 0x0 鈥?fall back to dialog geometry 纭畾.
    log(f"click_captcha_btn_ok: {rect.error if not rect.ok else 'click miss'}; try dialog geometry 纭畾")
    try:
        from app.core.captcha_dialog import (
            DialogBox,
            clamp_dialog_confirm_point,
            dialog_confirm_point,
        )
        from app.core.plg_ui import read_dlg_rect

        box = None
        dlg = get_activity_question_dlg(session, names=names, log=log)
        if dlg:
            # Prefer live dialog client rect over screenshot crop when available.
            try:
                drect = read_aui_ctrl_rect(session, dlg, name="captcha_dlg", log=log)
                if drect.ok and drect.w > 40 and drect.h > 40:
                    box = DialogBox(
                        left=int(drect.x),
                        top=int(drect.y),
                        right=int(drect.x + drect.w),
                        bottom=int(drect.y + drect.h),
                        score=9.0,
                        method="mem_dlg_rect",
                    )
            except Exception:
                box = None
            if box is None:
                try:
                    gr = read_dlg_rect(session, dlg, name="captcha_dlg", log=log)
                    if gr is not None and getattr(gr, "ok", False):
                        box = DialogBox(
                            left=int(gr.x),
                            top=int(gr.y),
                            right=int(gr.x + gr.w),
                            bottom=int(gr.y + gr.h),
                            score=8.0,
                            method="plg_dlg_rect",
                        )
                except Exception:
                    pass
        if box is None:
            # Last geometry path: caller may still pass via export crop elsewhere;
            # try Img_Question expanded to dialog-like bounds.
            img = get_captcha_img_question_rect(session, names=names, log=log)
            if img.ok and img.w > 40 and img.h > 40:
                # Grid is upper body; expand downward to include 纭畾 row.
                left, top = img.x - 20, img.y - 40
                right = img.x + img.w + 20
                bottom = img.y + img.h + max(80, img.h // 2)
                box = DialogBox(
                    left=max(0, left),
                    top=max(0, top),
                    right=right,
                    bottom=bottom,
                    score=5.0,
                    method="img_question_expand",
                )
        if box is not None and box.width > 40 and box.height > 40:
            cx, cy = dialog_confirm_point(box)
            if prefer_real_mouse:
                offset_y = max(-48, min(48, int(real_mouse_y_offset_px or 0)))
                cy = max(box.top + 1, min(box.bottom - 2, cy + offset_y))
            if humanize:
                cx, cy = jitter_around(cx, cy, radius_x=8, radius_y=3)
            cx, cy = clamp_dialog_confirm_point(box, cx, cy)
            log(
                f"click_captcha_btn_ok geom fallback "
                f"box=({box.left},{box.top},{box.width}x{box.height}) "
                f"via={box.method} click=({cx},{cy})"
            )
            if click_client_bg(
                session,
                hwnd,
                cx,
                cy,
                prefer_bridge=prefer_bridge,
                prefer_post=prefer_post,
                allow_cursor=allow_cursor,
                prefer_real_mouse=bool(prefer_real_mouse),
                humanize=humanize,
                hold_min_ms=int(hold_min_ms),
                hold_max_ms=int(hold_max_ms),
                path_from=path_from,
                slide=bool(slide and path_from is not None),
                slide_min_steps=int(slide_min_steps),
                slide_max_steps=int(slide_max_steps),
                slide_duration_min_s=float(slide_duration_min_s),
                slide_duration_max_s=float(slide_duration_max_s),
                stop_event=stop_event,
                log=log,
            ):
                return True
    except Exception as e:
        log(f"click_captcha_btn_ok geom fallback err: {e}")

    # Last resort after selection should already be latched: pure AQ_SUBMIT.
    if prefer_bridge and session is not None and not pure_submit:
        if submit_activity_question(session, names=names, hwnd=hwnd, log=log):
            log("click_captcha_btn_ok: last-resort AQ_SUBMIT ok")
            return True
        log("click_captcha_btn_ok: last-resort AQ_SUBMIT miss")
    return False



def click_captcha_btn_cancel(
    session,
    hwnd: int,
    *,
    prefer_bridge: bool = True,
    prefer_post: bool = True,
    allow_cursor: bool = False,
    humanize: bool = True,
    names: tuple[str, ...] | list[str] | None = None,
    log: LogFn | None = None,
) -> bool:
    """
    Close captcha mini-game via Btn_Cancel / geometry fallback.

    Live Win_Question3D often has bad cached Btn_Cancel rect (0x0). Fallbacks:
      1) GetDlgItem/cancel control rect
      2) dialog geometry 鍙栨秷 point
      3) dialog top-right close chrome
    @author by ak
    """
    log = log or (lambda _m: None)
    cands = tuple(names) if names else CAPTCHA_DLG_NAMES
    rect = get_captcha_btn_cancel_rect(session, names=cands, log=log)
    if rect.ok:
        pt = human_point_from_rect(rect, inset_frac=0.22, center_bias=0.45)
        if pt is None:
            cx, cy = rect.center
        else:
            cx, cy = pt
        if humanize:
            cx, cy = jitter_around(cx, cy, radius_x=2, radius_y=1)
            cx = max(rect.x + 1, min(rect.x + rect.w - 2, cx))
            cy = max(rect.y + 1, min(rect.y + rect.h - 2, cy))
        log(
            f"click_captcha_btn_cancel ptr=0x{rect.ctrl_ptr:X} "
            f"rect=({rect.x},{rect.y},{rect.w}x{rect.h}) click=({cx},{cy})"
        )
        return click_client_bg(
            session,
            hwnd,
            cx,
            cy,
            prefer_bridge=prefer_bridge,
            prefer_post=prefer_post,
            allow_cursor=allow_cursor,
            prefer_real_mouse=False,
            humanize=humanize,
            log=log,
        )

    # Geometry fallback from live dialog rect (mem GetGameUIDlg).
    try:
        from app.core.captcha_dialog import DialogBox, dialog_close_points

        mem = get_captcha_dlg_rect(session, names=cands, log=log)
        if mem is not None and bool(getattr(mem, "ok", False)) and int(
            getattr(mem, "w", 0) or 0
        ) > 8:
            box = DialogBox(
                left=int(mem.x),
                top=int(mem.y),
                right=int(mem.x) + int(mem.w),
                bottom=int(mem.y) + int(mem.h),
                score=9.0,
                method="mem_rect_cancel_fallback",
            )
            for label, cx, cy in dialog_close_points(box):
                if humanize:
                    cx, cy = jitter_around(cx, cy, radius_x=3, radius_y=2)
                log(
                    f"click_captcha_btn_cancel fallback={label} "
                    f"box=({box.left},{box.top},{box.width}x{box.height}) "
                    f"click=({cx},{cy}) why={rect.error!r}"
                )
                if click_client_bg(
                    session,
                    hwnd,
                    cx,
                    cy,
                    prefer_bridge=prefer_bridge,
                    prefer_post=prefer_post,
                    allow_cursor=allow_cursor,
                    humanize=humanize,
                    log=log,
                ):
                    return True
        else:
            log(
                f"click_captcha_btn_cancel: no mem rect fallback "
                f"ctrl_err={rect.error!r}"
            )
    except Exception as e:
        log(f"click_captcha_btn_cancel geometry fallback err: {e}")
    log(f"click_captcha_btn_cancel fail: {rect.error}")
    return False


def close_captcha_dialog(
    session,
    hwnd: int = 0,
    *,
    prefer_bridge: bool = True,
    prefer_post: bool = True,
    allow_cursor: bool = False,
    humanize: bool = True,
    names: tuple[str, ...] | list[str] | None = None,
    attempts: int = 2,
    settle_s: float = 0.18,
    use_esc: bool = False,
    prefer_show_hide: bool = True,
    log: LogFn | None = None,
) -> dict:
    """
    Force-close captcha mini-game and report whether it is gone.

    Order:
      0) AUIDialog::Show(false, 0, 1) via vtable+0x1C  (PRIMARY)
         Live: Btn_Ok success path ends with the same 3-arg Show;
         geometry cancel clicks often no-op on Win_Question3D while
         Btn_Cancel rect stays 0x0 / unusable.
      1) Btn_Cancel / geometry cancel / close-x click (secondary)
      2) Esc via bridge UI_KEY only when use_esc=True
    Then re-check IsDlgShow. ok only when dialog actually gone.
    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "was_open": False,
        "closed": False,
        "cancel_clicks": 0,
        "esc": 0,
        "hide": 0,
        "name": "",
        "method": "",
        "error": None,
        "hide_detail": None,
    }
    if session is None:
        out["error"] = "no session"
        return out
    cands = tuple(names) if names else CAPTCHA_DLG_NAMES
    hit = is_captcha_dialog_open(session, names=cands, log=log)
    out["was_open"] = bool(hit.shown)
    out["name"] = str(hit.name or "")
    if not hit.shown:
        out["ok"] = True
        out["closed"] = True
        out["method"] = "already_closed"
        return out

    tries = max(1, min(4, int(attempts)))
    settle = max(0.08, float(settle_s or 0.18))

    def _esc_once(tag: str) -> None:
        try:
            from app.core.sys_input import VK_ESCAPE
            from app.core.xajh_bridge import UI_KEY_PRESS

            br = _bridge_for_session(session, log=log)
            if br is None:
                return
            try:
                kr = br.ui_key(
                    VK_ESCAPE,
                    action=UI_KEY_PRESS,
                    hold_ms=50,
                    hwnd=int(hwnd or 0) or None,
                    timeout_ms=1500,
                    no_focus=True,
                )
                if bool(kr.ok):
                    out["esc"] = int(out["esc"]) + 1
                    if not out.get("method"):
                        out["method"] = "esc"
                    log(f"close_captcha {tag}: Esc ok note={kr.note!r}")
                else:
                    log(
                        f"close_captcha {tag}: Esc fail "
                        f"{kr.error or kr.note}"
                    )
            finally:
                try:
                    br.close()
                except Exception:
                    pass
        except Exception as e:
            log(f"close_captcha Esc err: {e}")

    def _gone(tag: str) -> bool:
        if settle > 0:
            time.sleep(float(settle))
        hit2 = is_captcha_dialog_open(session, names=cands, log=log)
        out["name"] = str(hit2.name or out["name"] or "")
        if not hit2.shown:
            out["ok"] = True
            out["closed"] = True
            log(
                f"close_captcha closed via={out.get('method') or tag} "
                f"hide={out['hide']} cancel={out['cancel_clicks']} esc={out['esc']}"
            )
            return True
        return False

    def _try_show_hide(tag: str) -> bool:
        """Primary close: same Show(0,0,1) as native AQ dismiss. @author by ak"""
        dlg = int(getattr(hit, "dlg_ptr", 0) or 0) & 0xFFFFFFFF
        if not dlg:
            # re-resolve shown dlg
            try:
                hit3 = is_captcha_dialog_open(session, names=cands, log=log)
                dlg = int(getattr(hit3, "dlg_ptr", 0) or 0) & 0xFFFFFFFF
                if hit3.name:
                    out["name"] = str(hit3.name)
            except Exception:
                dlg = 0
        if not dlg:
            # last resort: name lookup
            try:
                name = out.get("name") or (cands[0] if cands else "")
                if name:
                    dlg = int(get_game_ui_dlg(session, name, log=log) or 0) & 0xFFFFFFFF
            except Exception:
                dlg = 0
        if not dlg:
            log(f"close_captcha {tag}: no dlg_ptr for Show(false)")
            return False
        hr = hide_aui_dialog(session, dlg, force=True, show_args=(0, 0, 1), try_variants=True, log=log)
        out["hide"] = int(out["hide"]) + 1
        out["hide_detail"] = hr
        out["method"] = "aui_Show(0,0,1)"
        log(
            f"close_captcha {tag}: Show(0,0,1) dlg=0x{dlg:X} "
            f"ok={hr.get('ok')} shown_after={hr.get('shown_after')} "
            f"fn={int(hr.get('show_fn') or 0):#x} err={hr.get('error')!r}"
        )
        return _gone(f"{tag}/show_hide")

    # 0) Primary: direct AUI hide (do not burn 4s of dead geometry first)
    if prefer_show_hide:
        if _try_show_hide("primary"):
            return out

    for i in range(tries):
        # Re-check still open before spending clicks.
        hit_now = is_captcha_dialog_open(session, names=cands, log=log)
        if not hit_now.shown:
            out["ok"] = True
            out["closed"] = True
            out["method"] = out.get("method") or "already_closed"
            return out
        hit = hit_now
        out["name"] = str(hit.name or out["name"] or "")

        # Retry Show hide each attempt (state may have changed).
        if prefer_show_hide and _try_show_hide(f"attempt#{i + 1}"):
            return out

        # Secondary: Btn_Cancel / geometry (often ineffective on Question3D).
        if click_captcha_btn_cancel(
            session,
            int(hwnd or 0),
            prefer_bridge=prefer_bridge,
            prefer_post=prefer_post,
            allow_cursor=allow_cursor,
            prefer_real_mouse=False,
            humanize=humanize,
            names=cands,
            log=log,
        ):
            out["cancel_clicks"] = int(out["cancel_clicks"]) + 1
            out["method"] = out.get("method") or "btn_cancel_or_geom"
            if _gone(f"attempt#{i + 1}/cancel"):
                return out

        try:
            from app.core.captcha_dialog import DialogBox, dialog_close_points

            mem = get_captcha_dlg_rect(session, names=cands, log=log)
            if mem is not None and bool(getattr(mem, "ok", False)) and int(
                getattr(mem, "w", 0) or 0
            ) > 8:
                box = DialogBox(
                    left=int(mem.x),
                    top=int(mem.y),
                    right=int(mem.x) + int(mem.w),
                    bottom=int(mem.y) + int(mem.h),
                    score=9.0,
                    method="mem_rect_close_multi",
                )
                # Keep geometry short: only first 2 high-value points.
                for label, cx, cy in list(dialog_close_points(box))[:3]:
                    if humanize:
                        cx, cy = jitter_around(cx, cy, radius_x=3, radius_y=2)
                    log(
                        f"close_captcha attempt#{i + 1}: geom={label} "
                        f"click=({cx},{cy})"
                    )
                    if click_client_bg(
                        session,
                        int(hwnd or 0),
                        cx,
                        cy,
                        prefer_bridge=prefer_bridge,
                        prefer_post=prefer_post,
                        allow_cursor=allow_cursor,
                        humanize=humanize,
                        log=log,
                    ):
                        out["cancel_clicks"] = int(out["cancel_clicks"]) + 1
                        out["method"] = f"geom:{label}"
                        if _gone(f"attempt#{i + 1}/{label}"):
                            return out
                        # After a failed geom click, try Show hide again immediately.
                        if prefer_show_hide and _try_show_hide(
                            f"attempt#{i + 1}/after_{label}"
                        ):
                            return out
        except Exception as e:
            log(f"close_captcha geometry multi err: {e}")

        if use_esc:
            _esc_once(f"attempt#{i + 1}")
            if _gone(f"attempt#{i + 1}/esc"):
                return out
        elif i == 0:
            log("close_captcha: Esc skipped (use_esc=False)")

    out["error"] = f"captcha still open name={out.get('name') or '?'}"
    log(f"close_captcha fail: {out['error']}")
    return out


def click_captcha_cells(
    session,
    hwnd: int,
    positions: list[int] | tuple[int, ...],
    *,
    prefer_bridge: bool = True,
    prefer_post: bool = True,
    allow_cursor: bool = False,
    humanize: bool = True,
    names: tuple[str, ...] | list[str] | None = None,
    log: LogFn | None = None,
) -> list[tuple[int, int, bool]]:
    """
    Click captcha cells by 1-based positions using memory Img_Question grid.

    Returns list of (cx, cy, ok) for each position attempted.
    Points are inset-random when humanize=True (not perfect cell center).
    @author by ak
    """
    log = log or (lambda _m: None)
    out: list[tuple[int, int, bool]] = []
    img = get_captcha_img_question_rect(session, names=names, log=log)
    if not img.ok:
        log(f"click_captcha_cells: {img.error}")
        return out
    log(
        f"click_captcha_cells Img_Question ptr=0x{img.ctrl_ptr:X} "
        f"rect=({img.x},{img.y},{img.w}x{img.h}) positions={list(positions)}"
    )
    last_pt: tuple[int, int] | None = None
    for pos in positions:
        cell = captcha_cell_rect_from_img(img, int(pos))
        if not cell.ok:
            log(f"click_captcha_cells cell{pos}: {cell.error}")
            out.append((0, 0, False))
            continue
        if humanize:
            pt = human_point_from_rect(cell, inset_frac=0.20, center_bias=0.38)
            cx, cy = pt if pt is not None else cell.center
        else:
            cx, cy = cell.center
        ok = click_client_bg(
            session,
            hwnd,
            cx,
            cy,
            prefer_bridge=prefer_bridge,
            prefer_post=prefer_post,
            allow_cursor=allow_cursor,
            prefer_real_mouse=False,
            humanize=humanize,
            path_from=None,
            slide=False,
            log=log,
        )
        log(f"click_captcha_cells cell{pos} ({cx},{cy}) ok={ok}")
        out.append((cx, cy, ok))
        if ok:
            last_pt = (int(cx), int(cy))
    return out


def get_captcha_cell_click_point(
    session,
    index_1based: int,
    *,
    dlg_name: str | None = None,
    names: tuple[str, ...] | list[str] | None = None,
    humanize: bool = True,
    log: LogFn | None = None,
) -> tuple[int, int] | None:
    """
    Client click point for captcha cell 1..8 (humanized inside cell when set).

    @author by ak
    """
    r = get_captcha_cell_rect(
        session, index_1based, dlg_name=dlg_name, names=names, log=log
    )
    if not r.ok:
        return None
    if not humanize:
        return r.center
    pt = human_point_from_rect(r, inset_frac=0.20, center_bias=0.38)
    return pt if pt is not None else r.center
