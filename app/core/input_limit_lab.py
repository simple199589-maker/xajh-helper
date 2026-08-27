"""Runtime research helpers for the game's sutra UI restrictions."""
from __future__ import annotations

import re
import struct
from typing import Callable

LogFn = Callable[[str], None]

INPUT_DIALOG_NAMES = ("Win_InputNO", "Win_InputNO2")
INPUT_CONTROL_NAME = "DEFAULT_Txt_No."
INPUT_CONTROL_MAX_OFFSET = 0x12C
INPUT_DIALOG_MODE_OFFSET = 0x288
HEART_APTITUDE_INPUT_MODE = 0x18
MAX_SIGNED_INPUT = 0x7FFFFFFF

SUTRA_DIALOG_NAME = "Win_SurtraPotential"
SUTRA_LEFT_POINT_CONTROL = "Txt_LeftPoint"

# Preferred-image VAs from the current xajh.exe build.  At AFBD21 the upgrade
# handler compares the current level with the client table count.  The normal
# branch jumps over the "maximum level" return; changing only the first two
# bytes of that return block redirects capped levels to the original request
# send path.  Levels below the cap still run their original cost checks.
DEFAULT_IMAGE_BASE = 0x400000
SUTRA_LEVEL_CAP_SIGNATURE_NOTE_VA = 0xAFBD21
SUTRA_LEVEL_CAP_SIGNATURE_ORIGINAL = bytes.fromhex(
    "3B 83 A4 09 00 00 7C 2B 6A FF 6A FF 68 A2 50 00 00"
)
SUTRA_LEVEL_CAP_PATCH_OFFSET = 8
SUTRA_LEVEL_CAP_ORIGINAL = bytes.fromhex("6A FF")
SUTRA_LEVEL_CAP_PATCHED = bytes.fromhex("EB 6D")


def _read_u32(session, addr: int) -> int:
    pm = getattr(session, "pm", None)
    if pm is None:
        raise RuntimeError("session has no process handle")
    raw = pm.read_bytes(int(addr), 4)
    if len(raw) != 4:
        raise RuntimeError(f"short read @0x{int(addr):X}")
    return struct.unpack("<I", raw)[0]


def _write_u32(session, addr: int, value: int) -> None:
    pm = getattr(session, "pm", None)
    if pm is None:
        raise RuntimeError("session has no process handle")
    pm.write_bytes(int(addr), struct.pack("<I", int(value) & 0xFFFFFFFF), 4)


def _read_bytes(session, addr: int, size: int) -> bytes:
    pm = getattr(session, "pm", None)
    if pm is None:
        raise RuntimeError("session has no process handle")
    raw = bytes(pm.read_bytes(int(addr), int(size)))
    if len(raw) != int(size):
        raise RuntimeError(f"short read @0x{int(addr):X}")
    return raw


def _write_bytes(session, addr: int, data: bytes) -> None:
    pm = getattr(session, "pm", None)
    if pm is None:
        raise RuntimeError("session has no process handle")
    payload = bytes(data)
    pm.write_bytes(int(addr), payload, len(payload))


def probe_sutra_remaining_aptitude(
    session, *, log: LogFn | None = None
) -> dict:
    """Read the live remaining aptitude shown by ``Txt_LeftPoint``."""
    log = log or (lambda _m: None)
    from app.core.aui_click import get_aui_dlg_item_ptr
    from app.core.map_fly import _aui_get_text
    from app.core.plg_ui import get_game_ui_dlg, is_dlg_show

    dlg = int(get_game_ui_dlg(session, SUTRA_DIALOG_NAME, log=log) or 0)
    if not dlg or not is_dlg_show(session, dlg, log=log):
        return {
            "ok": False,
            "error": f"{SUTRA_DIALOG_NAME} 未显示；请先打开心法界面",
        }
    ctrl = int(
        get_aui_dlg_item_ptr(
            session, dlg, SUTRA_LEFT_POINT_CONTROL, log=log
        )
        or 0
    )
    if ctrl < 0x10000:
        return {
            "ok": False,
            "dialog": SUTRA_DIALOG_NAME,
            "dialog_ptr": dlg,
            "error": f"cannot resolve {SUTRA_LEFT_POINT_CONTROL}",
        }
    text = str(_aui_get_text(session, ctrl, log=log) or "").strip()
    match = re.search(r"\d[\d,]*", text)
    if match is None:
        return {
            "ok": False,
            "dialog": SUTRA_DIALOG_NAME,
            "dialog_ptr": dlg,
            "control": SUTRA_LEFT_POINT_CONTROL,
            "control_ptr": ctrl,
            "text": text,
            "error": f"无法从剩余资质文本解析数字: {text!r}",
        }
    remaining = int(match.group(0).replace(",", ""))
    out = {
        "ok": True,
        "dialog": SUTRA_DIALOG_NAME,
        "dialog_ptr": dlg,
        "control": SUTRA_LEFT_POINT_CONTROL,
        "control_ptr": ctrl,
        "text": text,
        "remaining": remaining,
    }
    log(
        f"sutra remaining aptitude: dlg=0x{dlg:X} ctrl=0x{ctrl:X} "
        f"text={text!r} remaining={remaining}"
    )
    return out


def probe_shown_input_limit(session, *, log: LogFn | None = None) -> dict:
    """Resolve the shown numeric dialog and read its AUIEditBox maximum."""
    log = log or (lambda _m: None)
    from app.core.aui_click import get_aui_dlg_item_ptr
    from app.core.plg_ui import get_game_ui_dlg, is_dlg_show

    for name in INPUT_DIALOG_NAMES:
        dlg = int(get_game_ui_dlg(session, name, log=log) or 0)
        if not dlg or not is_dlg_show(session, dlg, log=log):
            continue
        mode = _read_u32(session, dlg + INPUT_DIALOG_MODE_OFFSET)
        if mode != HEART_APTITUDE_INPUT_MODE:
            return {
                "ok": False,
                "dialog": name,
                "dialog_ptr": dlg,
                "mode": mode,
                "error": (
                    f"当前 Win_InputNO 模式=0x{mode:X}，"
                    f"不是心法资质模式0x{HEART_APTITUDE_INPUT_MODE:X}"
                ),
            }
        ctrl = int(
            get_aui_dlg_item_ptr(session, dlg, INPUT_CONTROL_NAME, log=log) or 0
        )
        if ctrl < 0x10000:
            return {
                "ok": False,
                "dialog": name,
                "dialog_ptr": dlg,
                "error": f"cannot resolve {INPUT_CONTROL_NAME}",
            }
        addr = ctrl + INPUT_CONTROL_MAX_OFFSET
        limit = _read_u32(session, addr)
        vtable = _read_u32(session, ctrl)
        out = {
            "ok": True,
            "dialog": name,
            "dialog_ptr": dlg,
            "mode": mode,
            "control": INPUT_CONTROL_NAME,
            "control_ptr": ctrl,
            "limit_addr": addr,
            "limit": limit,
            "vtable": vtable,
        }
        log(
            f"input limit probe: {name} dlg=0x{dlg:X} ctrl=0x{ctrl:X} "
            f"max@+0x{INPUT_CONTROL_MAX_OFFSET:X}={limit}"
        )
        return out
    return {
        "ok": False,
        "error": "Win_InputNO 未显示；请先在游戏中打开输入数量弹窗",
    }


def set_shown_input_limit(
    session, new_limit: int, *, log: LogFn | None = None
) -> dict:
    """Set and verify the maximum on the currently shown numeric edit control."""
    log = log or (lambda _m: None)
    want = int(new_limit)
    if want < 0 or want > MAX_SIGNED_INPUT:
        return {"ok": False, "error": f"new_limit must be 0..{MAX_SIGNED_INPUT}"}
    before = probe_shown_input_limit(session, log=log)
    if not before.get("ok"):
        return before
    old = int(before["limit"])
    if old < 0 or old > MAX_SIGNED_INPUT:
        return {
            **before,
            "ok": False,
            "error": f"unexpected current limit: {old}",
        }
    addr = int(before["limit_addr"])
    _write_u32(session, addr, want)
    after = _read_u32(session, addr)
    ok = after == want
    out = {
        **before,
        "ok": ok,
        "old_limit": old,
        "new_limit": after,
        "error": None if ok else f"verify failed: {after} != {want}",
    }
    log(
        f"input limit patch: addr=0x{addr:X} {old} -> {after} ok={int(ok)}"
    )
    return out


def set_shown_input_limit_to_remaining(
    session, *, log: LogFn | None = None
) -> dict:
    """Make the numeric dialog's Maximum button use live remaining aptitude."""
    log = log or (lambda _m: None)
    remaining = probe_sutra_remaining_aptitude(session, log=log)
    if not remaining.get("ok"):
        return remaining
    out = set_shown_input_limit(
        session, int(remaining["remaining"]), log=log
    )
    return {
        **out,
        "remaining": int(remaining["remaining"]),
        "remaining_text": remaining.get("text"),
        "remaining_control_ptr": remaining.get("control_ptr"),
    }


def probe_sutra_level_cap_patch(
    session, *, log: LogFn | None = None
) -> dict:
    """Inspect the reversible client-side sutra level-cap branch patch."""
    log = log or (lambda _m: None)
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        return {"ok": False, "error": "session has no module_base"}
    signature_addr = base + (
        SUTRA_LEVEL_CAP_SIGNATURE_NOTE_VA - DEFAULT_IMAGE_BASE
    )
    raw = _read_bytes(
        session, signature_addr, len(SUTRA_LEVEL_CAP_SIGNATURE_ORIGINAL)
    )
    patch_addr = signature_addr + SUTRA_LEVEL_CAP_PATCH_OFFSET
    current = raw[
        SUTRA_LEVEL_CAP_PATCH_OFFSET : SUTRA_LEVEL_CAP_PATCH_OFFSET
        + len(SUTRA_LEVEL_CAP_ORIGINAL)
    ]
    expected_prefix = SUTRA_LEVEL_CAP_SIGNATURE_ORIGINAL[
        :SUTRA_LEVEL_CAP_PATCH_OFFSET
    ]
    expected_suffix = SUTRA_LEVEL_CAP_SIGNATURE_ORIGINAL[
        SUTRA_LEVEL_CAP_PATCH_OFFSET + len(SUTRA_LEVEL_CAP_ORIGINAL) :
    ]
    signature_ok = (
        raw[:SUTRA_LEVEL_CAP_PATCH_OFFSET] == expected_prefix
        and raw[
            SUTRA_LEVEL_CAP_PATCH_OFFSET + len(SUTRA_LEVEL_CAP_ORIGINAL) :
        ]
        == expected_suffix
    )
    if current == SUTRA_LEVEL_CAP_ORIGINAL:
        state = "original"
    elif current == SUTRA_LEVEL_CAP_PATCHED:
        state = "patched"
    else:
        state = "unexpected"
    ok = bool(signature_ok and state != "unexpected")
    out = {
        "ok": ok,
        "state": state,
        "signature_ok": signature_ok,
        "signature_addr": signature_addr,
        "patch_addr": patch_addr,
        "current_bytes": current.hex(" ").upper(),
        "error": None if ok else "心法等级上限代码签名不匹配，拒绝修改",
    }
    log(
        f"sutra level cap probe: addr=0x{patch_addr:X} "
        f"bytes={out['current_bytes']} state={state} signature={int(signature_ok)}"
    )
    return out


def set_sutra_level_cap_bypass(
    session, enabled: bool, *, log: LogFn | None = None
) -> dict:
    """Enable or restore the capped-level upgrade request path."""
    log = log or (lambda _m: None)
    before = probe_sutra_level_cap_patch(session, log=log)
    if not before.get("ok"):
        return before
    target = SUTRA_LEVEL_CAP_PATCHED if enabled else SUTRA_LEVEL_CAP_ORIGINAL
    target_state = "patched" if enabled else "original"
    addr = int(before["patch_addr"])
    if before.get("state") != target_state:
        _write_bytes(session, addr, target)
    after = probe_sutra_level_cap_patch(session, log=log)
    ok = bool(after.get("ok") and after.get("state") == target_state)
    out = {
        **after,
        "ok": ok,
        "enabled": bool(enabled),
        "old_state": before.get("state"),
        "error": None if ok else str(after.get("error") or "写入后校验失败"),
    }
    log(
        f"sutra level cap patch: addr=0x{addr:X} "
        f"{before.get('state')} -> {after.get('state')} ok={int(ok)}"
    )
    return out
