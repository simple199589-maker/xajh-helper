# -*- coding: utf-8 -*-
"""
Background input helpers via shared-memory bridge.

Shift reticle (product): KEY_HOLD (CMD 24) process-local hooks —
IAT GetAsyncKeyState/GetKeyState + inline IsKeyTable/GetModMask.
Default NO SoftSend (does not pollute other windows). Keep-alive level-only
refresh while held. Proven to drive free-aim aimBuf + visible reticle when
the skill path is ready.

Legacy KEY_FORCE SoftSend remains for lab A/B only.

Mouse clicker: process-local KEY_HOLD button state plus timed window messages.

Skill recovery cancel: product default is CMD_CANCEL_SESSION (opcode 0x21)
when cast is busy — no keyboard keys. Optional macro sequence remains for
temporary key-style experiments only.

@author by ak
"""
from __future__ import annotations

import re

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from app.core.sys_input import (
    VK_ESCAPE,
    VK_Q,
    VK_RBUTTON,
    VK_SPACE,
    VK_X,
    focus_window,
    send_key_event,
    send_key_press,
    send_mouse_click,
)
from app.core.xajh_bridge import (
    UI_CLICK_LEFT,
    UI_CLICK_RIGHT,
    UI_KEY_PRESS,
    ensure_bridge,
)

# Temporary key-path probe helpers (UI "测按键" button).

LogFn = Callable[[str], None]

VK_SHIFT = 0x10
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LBUTTON = 0x01

MODE_HOLD = "hold"
MODE_CYCLE = "cycle"

# Skill cancel loop: two-step pipeline (cancel channels + cast).
# When-to-cancel policy (independent of which cancel channel runs).
CANCEL_WHEN_NEVER = "never"
CANCEL_WHEN_ALWAYS = "always"
CANCEL_WHEN_BUSY = "busy"
CANCEL_WHEN_POLICIES = frozenset(
    {CANCEL_WHEN_NEVER, CANCEL_WHEN_ALWAYS, CANCEL_WHEN_BUSY}
)

# Cast step mode: none = protocol/memory cancel only (no keys).
CAST_MODE_NONE = "none"
CAST_MODE_SINGLE = "single"
CAST_MODE_SEQUENCE = "sequence"
CAST_MODES = frozenset({CAST_MODE_NONE, CAST_MODE_SINGLE, CAST_MODE_SEQUENCE})

# Delivery: system (SendInput) first; bridge = injected UI_KEY (later).
DELIVERY_FOREGROUND = "foreground"
DELIVERY_SYSTEM = "system"  # alias of foreground (system-level SendInput)
DELIVERY_BRIDGE = "bridge"
DELIVERY_MODES = frozenset(
    {DELIVERY_FOREGROUND, DELIVERY_SYSTEM, DELIVERY_BRIDGE}
)

# Legacy mode strings → mapped on load for old settings.
CANCEL_MODE_KEY_ONLY = "key_only"
CANCEL_MODE_CANCEL_THEN_KEY = "cancel_then_key"
CANCEL_MODE_SMART = "smart"
CANCEL_LOOP_MODES = frozenset(
    {CANCEL_MODE_KEY_ONLY, CANCEL_MODE_CANCEL_THEN_KEY, CANCEL_MODE_SMART}
)

# Common skill bar digit keys 1-0 (VK 0x31..0x39, 0x30).
_SKILL_VK_BY_LABEL = {
    "1": 0x31,
    "2": 0x32,
    "3": 0x33,
    "4": 0x34,
    "5": 0x35,
    "6": 0x36,
    "7": 0x37,
    "8": 0x38,
    "9": 0x39,
    "0": 0x30,
}

# Named keys for sequence / cancel (G HUB labels).
_NAMED_VK = {
    "Esc": 0x1B,
    "Escape": 0x1B,
    "RMB": 0x02,
    "Space": 0x20,
    "C": 0x43,
    "V": 0x56,
    "F": 0x46,
    "Q": 0x51,
    "E": 0x45,
    "X": 0x58,
    "Z": 0x5A,
    "R": 0x52,
    "T": 0x54,
    "G": 0x47,
    "Tab": 0x09,
}
_CANCEL_VK_BY_LABEL = {
    "Esc": 0x1B,
    "RMB": 0x02,
    "Space": 0x20,
    "C": 0x43,
    "V": 0x56,
    "F": 0x46,
    "Q": 0x51,
    "E": 0x45,
    "X": 0x58,
}

# User macro timing (hold_ms / after_ms). Format: Key:hold_ms:after_ms
# Combat semantics (this client): Space = clear block/格挡; X = clear recovery/后摇.
# Q is part of the recorded chain between them.
RECORDED_LOGITECH_SEQUENCE = "Space:50:50,Q:50:260,X:50:100"
DEFAULT_LOGITECH_SEQUENCE = RECORDED_LOGITECH_SEQUENCE
# Optional presets (no-Space variants are opt-in).
PRESET_SKILL_ONLY = "1:35:40,1:35:40,1:35:30"
PRESET_SKILL_X = "1:30:35,X:25:35,1:30:35,X:25:25"
PRESET_Q_X = "Q:35:40,X:25:35,Q:35:40,X:25:25"
PRESET_SKILL_SPACE = "1:30:35,Space:25:30,1:30:35,Space:25:20"
PRESET_SKILL_SPACE_X = "1:28:30,Space:22:28,X:22:28,1:28:18"
PRESET_Q_SPACE = "Q:30:35,Space:25:30,Q:30:35,Space:25:20"


def parse_vk_label(text: str, default: int = 0x20) -> int:
    """
    Parse VK from hex/int/named label / single letter.

    Accepts: Space, Esc, F1..F24, 0-9, A-Z, LShift, 0x20, 32
    Invalid token returns default (use default=0 to skip unknowns).
    @author by ak
    """
    s = str(text or "").strip()
    if not s:
        return int(default) & 0xFF
    low = s.lower()
    named = {
        "space": 0x20,
        "spc": 0x20,
        "空格": 0x20,
        "esc": 0x1B,
        "escape": 0x1B,
        "shift": 0x10,
        "lshift": 0xA0,
        "rshift": 0xA1,
        "ctrl": 0x11,
        "control": 0x11,
        "lctrl": 0xA2,
        "rctrl": 0xA3,
        "alt": 0x12,
        "lalt": 0xA4,
        "ralt": 0xA5,
        "tab": 0x09,
        "enter": 0x0D,
        "return": 0x0D,
        "backspace": 0x08,
        "bs": 0x08,
        "caps": 0x14,
        "capslock": 0x14,
        "ins": 0x2D,
        "insert": 0x2D,
        "del": 0x2E,
        "delete": 0x2E,
        "home": 0x24,
        "end": 0x23,
        "pgup": 0x21,
        "pageup": 0x21,
        "pgdn": 0x22,
        "pagedown": 0x22,
        "up": 0x26,
        "down": 0x28,
        "left": 0x25,
        "right": 0x27,
        "1": 0x31,
        "2": 0x32,
        "3": 0x33,
        "4": 0x34,
        "5": 0x35,
        "6": 0x36,
        "7": 0x37,
        "8": 0x38,
        "9": 0x39,
        "0": 0x30,
    }
    if low in named:
        return int(named[low]) & 0xFF
    # punctuation (ASCII layout OEM VKs)
    punc = {
        "/": 0xBF,
        ".": 0xBE,
        ",": 0xBC,
        ";": 0xBA,
        "'": 0xDE,
        "[": 0xDB,
        "]": 0xDD,
        "-": 0xBD,
        "=": 0xBB,
        "`": 0xC0,
        "\\": 0xDC,
    }
    if s in punc:
        return punc[s]
    # F1..F24
    if len(low) >= 2 and low[0] == "f" and low[1:].isdigit():
        n = int(low[1:])
        if 1 <= n <= 24:
            return int(0x70 + n - 1) & 0xFF
    # single letter A-Z -> VK_A..VK_Z
    if len(low) == 1 and "a" <= low <= "z":
        return int(ord(low.upper())) & 0xFF
    try:
        if low.startswith("0x"):
            return int(low, 16) & 0xFF
        # bare decimal only when fully digits
        if low.isdigit():
            return int(low, 10) & 0xFF
    except Exception:
        pass
    return int(default) & 0xFF


def format_vk_label(vk: int) -> str:
    """Human label for a VK code. @author by ak"""
    v = int(vk) & 0xFF
    rev = {
        0x20: "Space",
        0x1B: "Esc",
        0x10: "Shift",
        0xA0: "LShift",
        0xA1: "RShift",
        0x11: "Ctrl",
        0xA2: "LCtrl",
        0xA3: "RCtrl",
        0x12: "Alt",
        0xA4: "LAlt",
        0xA5: "RAlt",
        0x09: "Tab",
        0x0D: "Enter",
        0x08: "Backspace",
        0x25: "Left",
        0x26: "Up",
        0x27: "Right",
        0x28: "Down",
    }
    if v in rev:
        return rev[v]
    if 0x30 <= v <= 0x39:
        return chr(v)
    if 0x41 <= v <= 0x5A:
        return chr(v)
    if 0x70 <= v <= 0x87:
        return f"F{v - 0x70 + 1}"
    return f"0x{v:02X}"


def format_vk_list(keys) -> str:
    """
    Comma-joined labels for VK list.

    Also accepts chord list [[Alt,R],[1]] -> "Alt+R,1".
    @author by ak
    """
    if not keys:
        return ""
    # chord-aware
    try:
        first = keys[0]
        if isinstance(first, (list, tuple)):
            return format_key_bind_chords(keys)
    except Exception:
        pass
    out = []
    for k in keys or []:
        try:
            vk = int(k) & 0xFF
        except Exception:
            continue
        if vk:
            out.append(format_vk_label(vk))
    return ",".join(out)


def key_hold_probe(
    pid: int,
    *,
    hwnd: int = 0,
    vk: int = 0x20,
    allow_softsend: bool = False,
    hold_ms: int = 400,
    log: LogFn | None = None,
) -> list[str]:
    """
    Lab probe for solution-2 KEY_HOLD (process-local hooks, default no SoftSend).

    Steps: install/status → ON → short hold(level) → OFF → clear note.
    @author by ak
    """
    lines: list[str] = []

    def emit(msg: str) -> None:
        lines.append(str(msg))
        if log:
            try:
                log(str(msg))
            except Exception:
                pass

    from app.core.xajh_bridge import ensure_bridge

    hwnd_i = int(hwnd or 0) or _resolve_hwnd(int(pid), 0)
    emit(
        f"KEY_HOLD 测: pid={int(pid)} hwnd=0x{int(hwnd_i or 0):X} "
        f"vk=0x{int(vk) & 0xFF:02X} softsend={1 if allow_softsend else 0}"
    )
    br = ensure_bridge(
        int(pid),
        log=lambda m: emit(f"  bridge: {m}"),
        inject_if_needed=True,
        hwnd=hwnd_i or None,
        force_reinject=False,
    )
    if br is None:
        emit("KEY_HOLD 测: 桥接未就绪")
        return lines
    try:
        r = br.key_hold(
            int(vk),
            install_only=True,
            allow_softsend=bool(allow_softsend),
            hwnd=hwnd_i or None,
            timeout_ms=3000,
        )
        emit(
            f"KEY_HOLD 测: hooks {'ok' if r.ok else 'FAIL'} "
            f"ret={r.ret} | {(r.note or r.error or '').strip()}"
        )
        if not r.ok:
            return lines
        r = br.key_hold(
            int(vk),
            down=True,
            level_only=False,
            allow_softsend=bool(allow_softsend),
            hwnd=hwnd_i or None,
            timeout_ms=3000,
        )
        emit(
            f"KEY_HOLD 测: ON {'ok' if r.ok else 'FAIL'} "
            f"ret={r.ret} | {(r.note or r.error or '').strip()}"
        )
        import time as _time

        end = _time.time() + max(0.05, int(hold_ms) / 1000.0)
        while _time.time() < end:
            br.key_hold(
                int(vk),
                down=True,
                level_only=True,
                allow_softsend=bool(allow_softsend),
                hwnd=hwnd_i or None,
                timeout_ms=1000,
            )
            _time.sleep(0.05)
        r = br.key_hold(
            int(vk),
            down=False,
            allow_softsend=bool(allow_softsend),
            hwnd=hwnd_i or None,
            timeout_ms=2500,
        )
        emit(
            f"KEY_HOLD 测: OFF {'ok' if r.ok else 'FAIL'} "
            f"| {(r.note or r.error or '').strip()}"
        )
        emit(
            "KEY_HOLD 测: 完成 — 期望 hooks=1、view 高位、tab=1；"
            "softsend=0 时 real 可仍为 0000（前台不被污染）"
        )
    except Exception as e:
        emit(f"KEY_HOLD 测: 异常 {e}")
    return lines


class KeyHoldRunner:
    """
    Solution-2 hold runner for lab (process-local hooks, no focus steal).

    Default allow_softsend=False. Not product reticle-ready by itself.
    @author by ak
    """

    def __init__(
        self,
        pid: int,
        hwnd: int = 0,
        *,
        vk: int = 0x20,
        allow_softsend: bool = False,
        refresh_ms: int = 300,
        log: LogFn | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.pid = int(pid)
        self.hwnd = int(hwnd or 0)
        self.vk = int(vk) & 0xFF
        self.allow_softsend = bool(allow_softsend)
        self.refresh_ms = max(100, int(refresh_ms or 300))
        self._log = log or (lambda _m: None)
        self._on_status = on_status or (lambda _m: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._br = None
        self._forced = False

    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def start(self) -> bool:
        if self.is_running():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="key-hold-s2", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> bool:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._clear()
        try:
            if self._br is not None:
                self._br.close()
        except Exception:
            pass
        self._br = None
        self._thread = None
        return True

    def _status(self, msg: str) -> None:
        self._on_status(msg)
        self._log(msg)

    def _open(self):
        from app.core.xajh_bridge import ensure_bridge

        hwnd = _resolve_hwnd(self.pid, self.hwnd)
        if hwnd:
            self.hwnd = hwnd
        br = ensure_bridge(
            self.pid,
            log=self._log,
            inject_if_needed=True,
            hwnd=hwnd or None,
            force_reinject=False,
        )
        self._br = br
        return br

    def _set(self, down: bool, *, level_only: bool = False) -> bool:
        br = self._br or self._open()
        if br is None:
            self._status("KEY_HOLD: 桥接未就绪")
            return False
        only = bool(level_only) or (bool(down) and self._forced)
        try:
            r = br.key_hold(
                self.vk,
                down=bool(down),
                level_only=only,
                allow_softsend=self.allow_softsend,
                hwnd=self.hwnd or None,
                timeout_ms=2500,
            )
        except Exception as e:
            self._br = None
            self._status(f"KEY_HOLD: 异常 {e}")
            return False
        if not r.ok:
            self._status(f"KEY_HOLD: 失败 {(r.error or r.note or '').strip()}")
            return False
        self._forced = bool(down)
        if down and not only:
            note = (r.note or "").strip()
            if note:
                self._status(f"KEY_HOLD: {note}")
        return True

    def _clear(self) -> None:
        try:
            if self._br is not None:
                self._br.key_hold(
                    self.vk,
                    down=False,
                    allow_softsend=self.allow_softsend,
                    hwnd=self.hwnd or None,
                    timeout_ms=2000,
                )
        except Exception:
            pass
        self._forced = False

    def _run(self) -> None:
        self._status(
            f"KEY_HOLD: 开始 vk=0x{self.vk:02X} softsend={int(self.allow_softsend)} "
            f"(解法2 进程内 Hook，不抢焦点)"
        )
        try:
            if not self._set(True, level_only=False):
                return
            refresh = self.refresh_ms / 1000.0
            while not self._stop.is_set():
                if self._stop.wait(refresh):
                    break
                if not self._set(True, level_only=True):
                    if self._stop.wait(0.4):
                        break
        finally:
            self._clear()
            self._status("KEY_HOLD: 已停止")



def ensure_key_hold_hooks(
    pid: int,
    hwnd: int = 0,
    *,
    log: LogFn | None = None,
    timeout_ms: int = 3000,
) -> tuple[bool, str]:
    """
    Install KEY_HOLD hooks after inject (idle-safe, no SoftSend, no focus).

    Call once when bridge becomes ready. Safe to call repeatedly.
    @author by ak
    """
    log = log or (lambda _m: None)
    hwnd_i = _resolve_hwnd(int(pid), int(hwnd or 0))
    br = ensure_bridge(
        int(pid),
        log=log,
        inject_if_needed=False,
        hwnd=hwnd_i or None,
        force_reinject=False,
    )
    if br is None:
        return False, "桥接未就绪"
    try:
        r = br.key_hold(
            0x20,
            install_only=True,
            allow_softsend=False,
            hwnd=hwnd_i or None,
            timeout_ms=int(timeout_ms),
        )
    except Exception as e:
        return False, str(e)
    note = (r.note or r.error or "").strip()
    if r.ok:
        return True, note or "hooks ok"
    return False, note or "hook install fail"


_MODIFIER_VKS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}


def _is_modifier_vk(vk: int) -> bool:
    """True for Shift/Ctrl/Alt family. @author by ak"""
    return (int(vk) & 0xFF) in _MODIFIER_VKS


def parse_key_chord_token(tok: str) -> list[int]:
    """
    Parse one chord token: "Alt+R" / "Ctrl+Shift+1" / "Q".

    Order preserved; modifiers should appear before the main key in the string.
    @author by ak
    """
    s = str(tok or "").strip()
    if not s:
        return []
    # normalize full-width plus
    s = s.replace("＋", "+").replace("＋", "+")
    parts = [p.strip() for p in s.split("+") if p.strip()]
    out: list[int] = []
    seen: set[int] = set()
    for p in parts:
        vk = int(parse_vk_label(p, 0) or 0) & 0xFF
        if not vk or vk in seen:
            continue
        seen.add(vk)
        out.append(vk)
    return out



def press_bg_chord_once(
    pid: int,
    chord: str | list[int] | list[list[int]],
    *,
    hwnd: int = 0,
    hold_ms: int = 50,
    allow_softsend: bool = False,
    clear_all_after: bool = True,
    log: LogFn | None = None,
) -> dict:
    """
    One-shot background chord press via KEY_HOLD (same path as 鼠标/键盘区).

    chord examples:
      "Alt+R"
      [0x12, 0x52]
      [[0x12, 0x52]]

    Down order as written; up reverse (R then Alt). No FG focus steal.
    Set clear_all_after=False for a pulse owned by another feature so it does
    not clear unrelated keys held by the same game window.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "chord": None,
        "error": None,
        "via": "key_hold",
    }
    try:
        chords: list[list[int]]
        if isinstance(chord, str):
            chords = parse_key_bind_chords(chord)
        elif chord and isinstance(chord[0], (list, tuple)):
            chords = [[int(v) & 0xFF for v in ch if int(v) & 0xFF] for ch in chord]  # type: ignore[index]
            chords = [ch for ch in chords if ch]
        else:
            chords = [[int(v) & 0xFF for v in chord if int(v) & 0xFF]]  # type: ignore[arg-type]
            chords = [ch for ch in chords if ch]
        if not chords:
            out["error"] = "empty chord"
            return out
        one = chords[0]
        out["chord"] = list(one)
        hwnd_i = _resolve_hwnd(int(pid), int(hwnd or 0))
        br = ensure_bridge(
            int(pid),
            log=log,
            inject_if_needed=False,
            hwnd=hwnd_i or None,
            force_reinject=False,
        )
        if br is None:
            out["error"] = "bridge not ready"
            return out
        try:
            # ensure hooks idle-ready
            br.key_hold(
                0x20,
                install_only=True,
                allow_softsend=bool(allow_softsend),
                hwnd=hwnd_i or None,
                timeout_ms=2000,
            )
            # down
            for vk in one:
                r = br.key_hold(
                    int(vk),
                    down=True,
                    allow_softsend=bool(allow_softsend),
                    hwnd=hwnd_i or None,
                    timeout_ms=2000,
                )
                if not r.ok:
                    out["error"] = r.error or r.note or "key_hold down fail"
                    # best-effort release what we pressed
                    for vk2 in reversed(one):
                        try:
                            br.key_hold(
                                int(vk2),
                                down=False,
                                allow_softsend=bool(allow_softsend),
                                hwnd=hwnd_i or None,
                                timeout_ms=1000,
                            )
                        except Exception:
                            pass
                    return out
            # hold
            wait = max(0.02, max(0, int(hold_ms)) / 1000.0)
            time.sleep(wait)
            # up reverse
            for vk in reversed(one):
                br.key_hold(
                    int(vk),
                    down=False,
                    allow_softsend=bool(allow_softsend),
                    hwnd=hwnd_i or None,
                    timeout_ms=2000,
                )
            if clear_all_after:
                try:
                    br.key_hold(
                        0x20,
                        clear_all=True,
                        hwnd=hwnd_i or None,
                        timeout_ms=1500,
                    )
                except Exception:
                    pass
            out["ok"] = True
            log(f"bg_chord once ok chord={format_key_bind_chords([one])} hold_ms={hold_ms}")
            return out
        finally:
            try:
                br.close()
            except Exception:
                pass
    except Exception as e:
        out["error"] = str(e)
        log(f"bg_chord once err: {e}")
        return out


def parse_key_bind_chords(raw) -> list[list[int]]:
    """
    Parse bind string into chord list.

    Separators between chords: comma / semicolon / pipe / whitespace.
    Within one chord: '+' joins simultaneous keys (e.g. Alt+R, Ctrl+Shift+1).

    Examples:
      "Alt+R" -> [[Alt, R]]
      "Alt+R,1,Space" -> [[Alt,R],[1],[Space]]
      "1 2 3" -> [[1],[2],[3]]
    @author by ak
    """
    s = str(raw or "").strip()
    if not s:
        return []
    s = s.replace("，", ",").replace(";", ",").replace("|", ",")
    parts: list[str] = []
    for chunk in s.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        # If chunk already has '+', keep as one chord token (do not split spaces
        # inside "Ctrl + R" variants after normalizing).
        if "+" in chunk or "＋" in chunk:
            # allow "Ctrl + R"
            chunk2 = re.sub(r"\s*\+\s*", "+", chunk.replace("＋", "+"))
            parts.append(chunk2)
            continue
        for tok in chunk.split():
            tok = tok.strip()
            if tok:
                parts.append(tok)
    out: list[list[int]] = []
    for tok in parts:
        chord = parse_key_chord_token(tok)
        if chord:
            out.append(chord)
    return out


def parse_key_bind_list(raw) -> list[int]:
    """
    Flat unique VK list (compat).

    "Alt+R,1" -> [Alt, R, 1]  (chord expanded left-to-right, unique)
    Prefer parse_key_bind_chords for combo-aware runners.
    @author by ak
    """
    out: list[int] = []
    seen: set[int] = set()
    for chord in parse_key_bind_chords(raw):
        for vk in chord:
            v = int(vk) & 0xFF
            if not v or v in seen:
                continue
            seen.add(v)
            out.append(v)
    return out


def format_key_bind_chords(chords) -> str:
    """Pretty-print chord list as Alt+R,1,Space. @author by ak"""
    parts: list[str] = []
    for ch in chords or []:
        if not ch:
            continue
        if isinstance(ch, (list, tuple)):
            labs = [format_vk_label(int(v) & 0xFF) for v in ch if int(v) & 0xFF]
            if labs:
                parts.append("+".join(labs))
        else:
            v = int(ch) & 0xFF
            if v:
                parts.append(format_vk_label(v))
    return ",".join(parts)


class BgKeyBindRunner:
    """
    Product background multi-key binder via KEY_HOLD (solution-2).

    mode=hold: keep ALL flat keys forced down until stop
    mode=tap: round-robin each chord (supports Alt+R style combos)
    mode=tap_all: each cycle press every chord (mods first), then release reverse

    Chord syntax in bind string: "Alt+R,1,Ctrl+Shift+1"
    Default allow_softsend=False (no FG pollution, no focus steal).
    @author by ak
    """

    MODE_HOLD = "hold"
    MODE_TAP = "tap"
    MODE_TAP_ALL = "tap_all"

    def __init__(
        self,
        pid: int,
        hwnd: int = 0,
        *,
        keys: list[int] | None = None,
        chords: list[list[int]] | None = None,
        mode: str = "tap",
        interval_ms: int = 200,
        hold_ms: int = 40,
        refresh_ms: int = 300,
        allow_softsend: bool = False,
        log: LogFn | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.pid = int(pid)
        self.hwnd = int(hwnd or 0)
        # chords preferred; keys= flat compat -> single-key chords
        parsed: list[list[int]] = []
        if chords:
            for ch in chords:
                one: list[int] = []
                seen_c: set[int] = set()
                for k in ch or []:
                    vk = int(k) & 0xFF
                    if not vk or vk in seen_c:
                        continue
                    seen_c.add(vk)
                    one.append(vk)
                if one:
                    parsed.append(one)
        if not parsed:
            seen: set[int] = set()
            for k in keys or [0x20]:
                vk = int(k) & 0xFF
                if not vk or vk in seen:
                    continue
                seen.add(vk)
                parsed.append([vk])
        self.chords: list[list[int]] = parsed
        # flat unique list (compat / hold mode)
        flat: list[int] = []
        seen_f: set[int] = set()
        for ch in self.chords:
            for vk in ch:
                if vk not in seen_f:
                    seen_f.add(vk)
                    flat.append(vk)
        self.keys = flat
        self.mode = str(mode or self.MODE_TAP).strip().lower()
        if self.mode not in (self.MODE_HOLD, self.MODE_TAP, self.MODE_TAP_ALL):
            # legacy aliases
            if self.mode in ("always", "hold_all", "press"):
                self.mode = self.MODE_HOLD
            elif self.mode in ("all", "chord", "sync"):
                self.mode = self.MODE_TAP_ALL
            else:
                self.mode = self.MODE_TAP
        self.interval_ms = max(20, int(interval_ms or 200))
        self.hold_ms = max(0, int(hold_ms or 40))
        # keep-alive reseed while always-holding (independent of tap interval)
        self.refresh_ms = max(100, min(1000, int(refresh_ms or 200)))
        self.allow_softsend = bool(allow_softsend)
        self._log = log or (lambda _m: None)
        self._on_status = on_status or (lambda _m: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._br = None
        self._down: set[int] = set()

    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def start(self) -> bool:
        if self.is_running():
            return True
        if not self.keys:
            self._status("后台键: 未绑定有效按键")
            return False
        self._stop.clear()
        self._down.clear()
        self._thread = threading.Thread(
            target=self._run, name="bg-key-bind", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> bool:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.5)
        self._clear_all()
        try:
            if self._br is not None:
                self._br.close()
        except Exception:
            pass
        self._br = None
        self._thread = None
        return True

    def _status(self, msg: str) -> None:
        self._on_status(msg)
        self._log(msg)

    def _open(self):
        hwnd = _resolve_hwnd(self.pid, self.hwnd)
        if hwnd:
            self.hwnd = hwnd
        br = ensure_bridge(
            self.pid,
            log=self._log,
            inject_if_needed=False,
            hwnd=hwnd or None,
            force_reinject=False,
        )
        self._br = br
        return br

    def _hold_one(self, vk: int, *, down: bool, level_only: bool = False) -> bool:
        br = self._br or self._open()
        if br is None:
            return False
        try:
            r = br.key_hold(
                int(vk),
                down=bool(down),
                level_only=bool(level_only),
                allow_softsend=self.allow_softsend,
                hwnd=self.hwnd or None,
                timeout_ms=2500,
            )
            ok = bool(r.ok)
            if ok:
                if down:
                    self._down.add(int(vk) & 0xFF)
                else:
                    self._down.discard(int(vk) & 0xFF)
            return ok
        except Exception as e:
            self._br = None
            self._status(f"后台键: 异常 {e}")
            return False

    def _press_many(self, keys: list[int], *, down: bool, level_only: bool = False) -> bool:
        """Press/release many keys. True if all succeed; partial still applies. @author by ak"""
        if not keys:
            return False
        seq = list(keys)
        if not down:
            # release reverse so main key lifts before modifiers (Alt+R)
            seq = list(reversed(seq))
        ok_n = 0
        for vk in seq:
            if self._stop.is_set():
                return False
            if self._hold_one(vk, down=down, level_only=level_only):
                ok_n += 1
        return ok_n == len(keys)

    def _press_chord(self, chord: list[int], *, down: bool, level_only: bool = False) -> bool:
        """
        Press or release one chord with modifier-safe order.

        Down: as written (Alt then R). Up: reverse (R then Alt).
        @author by ak
        """
        if not chord:
            return False
        return self._press_many(list(chord), down=down, level_only=level_only)

    def _clear_all(self) -> None:
        br = self._br
        keys = list(self._down) or list(self.keys)
        if br is None:
            self._down.clear()
            return
        try:
            for vk in keys:
                try:
                    br.key_hold(
                        int(vk),
                        down=False,
                        allow_softsend=self.allow_softsend,
                        hwnd=self.hwnd or None,
                        timeout_ms=1500,
                    )
                except Exception:
                    pass
        except Exception:
            pass
        try:
            br.key_hold(0x20, clear_all=True, hwnd=self.hwnd or None, timeout_ms=1500)
        except Exception:
            pass
        self._down.clear()

    def _run(self) -> None:
        labels = format_key_bind_chords(self.chords)
        mode_cn = {
            self.MODE_HOLD: "一直按住",
            self.MODE_TAP: "连发(轮流)",
            self.MODE_TAP_ALL: "连发(同时)",
        }.get(self.mode, self.mode)
        self._status(
            f"后台键: 开始 [{mode_cn}] keys=[{labels}] "
            f"n={len(self.chords)} interval={self.interval_ms}ms "
            f"hold={self.hold_ms}ms refresh={self.refresh_ms}ms "
            f"softsend={int(self.allow_softsend)}"
        )
        br = self._open()
        if br is None:
            self._status("后台键: 桥接未就绪")
            return
        # ensure hooks once (idle-safe)
        try:
            br.key_hold(
                (self.keys[0] if self.keys else 0x20),
                install_only=True,
                allow_softsend=self.allow_softsend,
                hwnd=self.hwnd or None,
                timeout_ms=3000,
            )
        except Exception:
            pass
        try:
            if self.mode == self.MODE_HOLD:
                self._loop_hold()
            elif self.mode == self.MODE_TAP_ALL:
                self._loop_tap_all()
            else:
                self._loop_tap()
        finally:
            self._clear_all()
            self._status("后台键: 已停止")

    def _loop_hold(self) -> None:
        """
        Keep all bound keys forced down until stop.

        Strategy:
        1) full edge ON for each key (first arm)
        2) level-only reseed all keys periodically (native timer also maintains)
        3) partial failure is tolerated; recover with full edge when needed
        @author by ak
        """
        labels = format_vk_list(self.keys)
        ok_n = 0
        for vk in self.keys:
            if self._stop.is_set():
                return
            # first arm: always full edge so inject/table path runs once per key
            if self._hold_one(vk, down=True, level_only=False):
                ok_n += 1
            else:
                self._status(f"后台键: 按下失败 {format_vk_label(vk)}")
        if ok_n <= 0:
            self._status("后台键: 按下失败（检查是否已注入最新桥接）")
            return
        # one more level pass so multi-key table is re-seeded together
        self._press_many(self.keys, down=True, level_only=True)
        self._status(
            f"后台键: 已一直按住 [{labels}] ok={ok_n}/{len(self.keys)} "
            f"（进程内 Hook，不抢焦点）"
        )
        # prefer snappier reseed for multi-key (native also maintains ~30ms)
        refresh = max(0.12, min(0.5, self.refresh_ms / 1000.0))
        fails = 0
        while not self._stop.is_set():
            if self._stop.wait(refresh):
                break
            ok = self._press_many(self.keys, down=True, level_only=True)
            if ok:
                fails = 0
                continue
            fails += 1
            if fails < 2:
                continue
            # recover: re-arm full edge for all keys still marked
            rec = 0
            for vk in self.keys:
                if self._stop.is_set():
                    return
                if self._hold_one(vk, down=True, level_only=False):
                    rec += 1
            if rec > 0:
                fails = 0
                self._status(f"后台键: 按住已恢复 {rec}/{len(self.keys)}")
            else:
                if self._stop.wait(0.35):
                    return

    def _loop_tap(self) -> None:
        """Round-robin: press one chord per cycle (Alt+R supported). @author by ak"""
        hold_s = max(0.0, self.hold_ms / 1000.0)
        gap_s = max(0.02, self.interval_ms / 1000.0)
        idx = 0
        chords = self.chords or [[k] for k in self.keys]
        while not self._stop.is_set():
            chord = list(chords[idx % len(chords)])
            idx += 1
            if not self._press_chord(chord, down=True, level_only=False):
                if self._stop.wait(0.3):
                    break
                continue
            if hold_s > 0 and self._stop.wait(hold_s):
                self._press_chord(chord, down=False)
                break
            self._press_chord(chord, down=False)
            if self._stop.wait(gap_s):
                break

    def _loop_tap_all(self) -> None:
        """
        Each cycle: press every chord (mods-first within chord), hold, reverse release.

        For multi-chord binds, chords are armed in list order then released reverse.
        @author by ak
        """
        hold_s = max(0.0, self.hold_ms / 1000.0)
        gap_s = max(0.02, self.interval_ms / 1000.0)
        chords = self.chords or [[k] for k in self.keys]
        while not self._stop.is_set():
            ok = True
            for ch in chords:
                if self._stop.is_set():
                    ok = False
                    break
                if not self._press_chord(list(ch), down=True, level_only=False):
                    ok = False
                    break
            if not ok:
                # best-effort release anything down
                for ch in reversed(chords):
                    self._press_chord(list(ch), down=False)
                if self._stop.wait(0.3):
                    break
                continue
            if hold_s > 0 and self._stop.wait(hold_s):
                for ch in reversed(chords):
                    self._press_chord(list(ch), down=False)
                break
            for ch in reversed(chords):
                self._press_chord(list(ch), down=False)
            if self._stop.wait(gap_s):
                break



def capture_next_vk(
    *,
    timeout_s: float = 10.0,
    stop_event: threading.Event | None = None,
    ignore: set[int] | None = None,
) -> int:
    """
    Capture the next physical key press (system-wide poll).

    Returns VK code, or 0 on timeout/cancel.
    Ignores mouse buttons and already-held keys at start.
    @author by ak
    """
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    ignore = set(int(x) & 0xFF for x in (ignore or set()))
    # mouse buttons / shift noise
    ignore.update({0x01, 0x02, 0x04, 0x05, 0x06})
    # scan range: common keys
    candidates = list(range(0x08, 0xFE))

    def down(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(int(vk) & 0xFF) & 0x8000)

    # wait until currently held keys released (except ignore)
    t0 = time.time()
    while time.time() - t0 < 3.0:
        if stop_event is not None and stop_event.is_set():
            return 0
        held = [vk for vk in candidates if vk not in ignore and down(vk)]
        if not held:
            break
        time.sleep(0.02)

    t0 = time.time()
    while time.time() - t0 < float(timeout_s):
        if stop_event is not None and stop_event.is_set():
            return 0
        for vk in candidates:
            if vk in ignore:
                continue
            if not down(vk):
                continue
            # debounce: wait brief confirm
            time.sleep(0.03)
            if not down(vk):
                continue
            # wait release so we don't sticky-record
            t1 = time.time()
            while down(vk) and time.time() - t1 < 2.0:
                if stop_event is not None and stop_event.is_set():
                    return int(vk) & 0xFF
                time.sleep(0.01)
            return int(vk) & 0xFF
        time.sleep(0.01)
    return 0


class FgKeyBindRunner:
    """
    Foreground multi-key runner via system SendInput.

    mode=once: press each key once then stop
    mode=hold: hold all keys down until stop
    mode=tap:  continuous press at interval_ms

    Requires game (or target) focused for effect.
    @author by ak
    """

    MODE_ONCE = "once"
    MODE_HOLD = "hold"
    MODE_TAP = "tap"

    def __init__(
        self,
        keys: list[int] | None = None,
        *,
        hwnd: int = 0,
        mode: str = "tap",
        interval_ms: int = 200,
        hold_ms: int = 40,
        log: LogFn | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        seen: set[int] = set()
        ordered: list[int] = []
        for k in keys or []:
            vk = int(k) & 0xFF
            if not vk or vk in seen:
                continue
            seen.add(vk)
            ordered.append(vk)
        self.keys = ordered
        self.hwnd = int(hwnd or 0)
        self.mode = str(mode or self.MODE_TAP).strip().lower()
        if self.mode in ("press", "click", "single", "一次", "按下"):
            self.mode = self.MODE_ONCE
        elif self.mode in ("always", "hold_all", "按住", "一直"):
            self.mode = self.MODE_HOLD
        elif self.mode in ("repeat", "auto", "连发"):
            self.mode = self.MODE_TAP
        # accept english constants as-is: once/hold/tap
        if self.mode not in (self.MODE_ONCE, self.MODE_HOLD, self.MODE_TAP):
            self.mode = self.MODE_TAP
        self.interval_ms = max(20, int(interval_ms or 200))
        self.hold_ms = max(0, int(hold_ms or 40))
        self._log = log or (lambda _m: None)
        self._on_status = on_status or (lambda _m: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._down: set[int] = set()

    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def start(self) -> bool:
        if self.is_running():
            return True
        if not self.keys:
            self._status("前台键: 未绑定有效按键")
            return False
        self._stop.clear()
        self._down.clear()
        self._thread = threading.Thread(
            target=self._run, name="fg-key-bind", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> bool:
        self._stop.set()
        t = self._thread
        # Never block UI long: runner finally() releases keys.
        if t is not None and t.is_alive():
            t.join(timeout=0.35)
        try:
            self._release_all()
        except Exception:
            pass
        self._thread = None
        return True

    def _status(self, msg: str) -> None:
        # Callers must pass thread-safe callbacks (queue/after). Do not touch Tk here.
        try:
            self._on_status(msg)
        except Exception:
            pass
        try:
            self._log(msg)
        except Exception:
            pass

    def _down_one(self, vk: int) -> bool:
        ok = bool(send_key_event(int(vk), key_up=False, hwnd=self.hwnd).ok)
        if ok:
            self._down.add(int(vk) & 0xFF)
        return ok

    def _up_one(self, vk: int) -> bool:
        ok = bool(send_key_event(int(vk), key_up=True, hwnd=self.hwnd).ok)
        self._down.discard(int(vk) & 0xFF)
        return ok

    def _press_one(self, vk: int) -> bool:
        return bool(
            send_key_press(
                int(vk),
                hold_ms=self.hold_ms,
                hwnd=self.hwnd,
                stop_event=self._stop,
            ).ok
        )

    def _release_all(self) -> None:
        for vk in list(self._down) or list(self.keys):
            try:
                send_key_event(int(vk), key_up=True, hwnd=self.hwnd)
            except Exception:
                pass
        self._down.clear()

    def _run(self) -> None:
        labels = format_vk_list(self.keys)
        mode_cn = {
            self.MODE_ONCE: "按下",
            self.MODE_HOLD: "一直",
            self.MODE_TAP: "连发",
        }.get(self.mode, self.mode)
        self._status(
            f"前台键: 开始 [{mode_cn}] keys=[{labels}] "
            f"interval={self.interval_ms}ms hold={self.hold_ms}ms "
            f"（SendInput，需游戏在前台）"
        )
        try:
            if self.mode == self.MODE_ONCE:
                self._loop_once()
            elif self.mode == self.MODE_HOLD:
                self._loop_hold()
            else:
                self._loop_tap()
        finally:
            self._release_all()
            self._status("前台键: 已停止")

    def _loop_once(self) -> None:
        for vk in self.keys:
            if self._stop.is_set():
                break
            if not self._press_one(vk):
                self._status(f"前台键: 按下失败 {format_vk_label(vk)}")
                break
            # tiny gap between multi keys
            if self._stop.wait(0.02):
                break
        self._status("前台键: 单次按下完成")

    def _loop_hold(self) -> None:
        ok_n = 0
        for vk in self.keys:
            if self._stop.is_set():
                return
            if self._down_one(vk):
                ok_n += 1
        if ok_n <= 0:
            self._status("前台键: 按住失败")
            return
        labels = format_vk_list(self.keys)
        self._status(f"前台键: 已按住 [{labels}]（前台 SendInput）")
        # keep thread alive; re-down occasionally in case OS lost state
        refresh = 0.15
        while not self._stop.is_set():
            if self._stop.wait(refresh):
                break
            for vk in self.keys:
                if self._stop.is_set():
                    break
                # level-style: ensure still down without extra up
                if (int(vk) & 0xFF) not in self._down:
                    self._down_one(vk)

    def _loop_tap(self) -> None:
        gap_s = max(0.02, self.interval_ms / 1000.0)
        idx = 0
        while not self._stop.is_set():
            vk = self.keys[idx % len(self.keys)]
            idx += 1
            if not self._press_one(vk):
                if self._stop.wait(0.25):
                    break
                continue
            if self._stop.wait(gap_s):
                break



@dataclass
class ShiftHoldConfig:
    """
    Background Shift free-aim reticle config.

    engine=hold (product): KEY_HOLD process-local hooks, no SoftSend.
    engine=force (lab/legacy): KEY_FORCE SoftSend — can dirty other clients.

    mode=hold: keep Shift forced until stop.
    mode=cycle: on hold_ms / off release_ms.
    @author by ak
    """

    mode: str = MODE_HOLD
    refresh_ms: int = 400  # KEY_HOLD level-only keep-alive
    hold_ms: int = 800
    release_ms: int = 200
    vk: int = VK_SHIFT
    allow_softsend: bool = False  # product: always False
    engine: str = "hold"  # hold | force
    skill_id: int = 0  # unused (compat)
    bar_slot: int = 0  # unused (compat)


@dataclass
class MouseClickerConfig:
    """
    Background mouse auto-clicker at fixed client coords (or center if 0,0).

    @author by ak
    """

    button: int = UI_CLICK_LEFT
    interval_ms: int = 100
    hold_ms: int = 40
    cx: int = 0
    cy: int = 0


def _resolve_hwnd(pid: int, hwnd: int = 0) -> int:
    """Prefer provided hwnd; else main window for pid. @author by ak"""
    h = int(hwnd or 0)
    if h:
        return h
    try:
        from app.core.inject_gate import find_main_hwnd_for_pid

        return int(find_main_hwnd_for_pid(int(pid)) or 0)
    except Exception:
        return 0


def _client_center(hwnd: int) -> tuple[int, int]:
    """Client-area center. @author by ak"""
    if not hwnd:
        return 400, 300
    try:
        from app.core.win_capture import get_client_rect_screen

        box = get_client_rect_screen(int(hwnd))
        if not box:
            return 400, 300
        left, top, right, bottom = box
        w = max(1, int(right - left))
        h = max(1, int(bottom - top))
        return w // 2, h // 2
    except Exception:
        return 400, 300


def probe_key_paths(
    pid: int,
    hwnd: int = 0,
    *,
    log: LogFn | None = None,
) -> list[str]:
    """
    Background-only key path diagnostic (no focus steal).

    Order:
      1) KEY_FORCE ON (readback diag: gate/gaks/tab/mod)
      2) KEY_FORCE OFF
      3) UI_KEY Space with no_focus=True (does not SetForegroundWindow)

    Never uses system SendInput Space — that hits the focused window and
    breaks pure-background testing.
    Returns log lines for UI. Does not leave Shift forced on.
    @author by ak
    """
    lines: list[str] = []
    log = log or (lambda _m: None)

    def emit(msg: str) -> None:
        lines.append(msg)
        try:
            log(msg)
        except Exception:
            pass

    hwnd_i = _resolve_hwnd(int(pid), int(hwnd or 0))
    emit(f"测按键: pid={int(pid)} hwnd=0x{int(hwnd_i or 0):X} (纯后台)")
    br = ensure_bridge(
        int(pid),
        log=log,
        inject_if_needed=False,
        hwnd=hwnd_i or None,
        force_reinject=False,
    )
    if br is None:
        emit("测按键: 桥接未就绪 — 请先 Delete 注入最新桥接")
        return lines

    # 1) KEY_FORCE ON — native FormatForceDiag note is the ground truth.
    try:
        r = br.key_force(
            VK_SHIFT,
            down=True,
            level_only=False,
            hwnd=hwnd_i or None,
            timeout_ms=3000,
        )
        note = (r.note or r.error or "").strip()
        if r.ok:
            emit(f"测按键: KEY_FORCE ON ok ret={r.ret} | {note}")
        else:
            emit(f"测按键: KEY_FORCE ON FAIL | {note}")
    except Exception as e:
        emit(f"测按键: KEY_FORCE ON 异常 {e}")

    # Hold briefly so maintain path / GAKS can settle for readback.
    time.sleep(0.35)

    # 2) KEY_FORCE OFF
    try:
        r = br.key_force(
            VK_SHIFT,
            down=False,
            level_only=False,
            hwnd=hwnd_i or None,
            timeout_ms=2500,
        )
        note = (r.note or r.error or "").strip()
        emit(
            f"测按键: KEY_FORCE OFF {'ok' if r.ok else 'FAIL'} "
            f"ret={r.ret} | {note}"
        )
    except Exception as e:
        emit(f"测按键: KEY_FORCE OFF 异常 {e}")

    # 3) UI_KEY Space — no_focus so DLL skips SetForegroundWindow.
    try:
        r = br.ui_key(
            VK_SPACE,
            action=UI_KEY_PRESS,
            hold_ms=40,
            hwnd=hwnd_i or None,
            timeout_ms=2500,
            no_focus=True,
        )
        note = (r.note or r.error or "").strip()
        emit(
            f"测按键: UI_KEY Space no_focus "
            f"{'ok' if r.ok else 'FAIL'} ret={r.ret} | {note}"
        )
    except Exception as e:
        emit(f"测按键: UI_KEY Space 异常 {e}")

    emit(
        "测按键: 完成(纯后台) — 看 KEY_FORCE 的 gate/gaks/tab/mod；"
        "UI_KEY 用 no_focus，不拉焦点、不发 sys Space"
    )
    try:
        br.close()
    except Exception:
        pass
    return lines


class ShiftHoldRunner:
    """
    Product free-aim Shift reticle via KEY_HOLD (default, no SoftSend).

    User-confirmed: HOLD drives aimBuf and on-screen reticle when skill
    free-aim is available. Not a skill auto-cast; only holds Shift state.
    @author by ak
    """

    def __init__(
        self,
        pid: int,
        hwnd: int = 0,
        cfg: ShiftHoldConfig | None = None,
        *,
        log: LogFn | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.pid = int(pid)
        self.hwnd = int(hwnd or 0)
        self.cfg = cfg or ShiftHoldConfig()
        self._log = log or (lambda _m: None)
        self._on_status = on_status or (lambda _m: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._br = None
        self._forced = False
        self._hooks_ready = False

    def is_running(self) -> bool:
        """True while worker thread is alive. @author by ak"""
        t = self._thread
        return t is not None and t.is_alive()

    def start(self) -> bool:
        """Start force-Shift loop. @author by ak"""
        if self.is_running():
            return False
        self._stop.clear()
        self._forced = False
        self._hooks_ready = False
        self._thread = threading.Thread(
            target=self._run, name="shift-reticle", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> bool:
        """Stop and clear forced Shift. @author by ak"""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        stopped = not bool(t and t.is_alive())
        if stopped:
            self._thread = None
        self._clear_force()
        try:
            if self._br is not None:
                self._br.close()
        except Exception:
            pass
        self._br = None
        return stopped

    def _status(self, msg: str) -> None:
        self._on_status(msg)
        self._log(msg)

    def _engine(self) -> str:
        eng = str(getattr(self.cfg, "engine", "hold") or "hold").strip().lower()
        return eng if eng in ("hold", "force") else "hold"

    def _open_bridge(self, *, quiet: bool = False):
        hwnd = _resolve_hwnd(self.pid, self.hwnd)
        if hwnd:
            self.hwnd = hwnd
        br = self._br
        if br is not None:
            try:
                if getattr(br, "_view", 0):
                    return br
            except Exception:
                pass
            self._br = None
        log_fn = (lambda _m: None) if quiet else self._log
        # KEY_HOLD needs inject; KEY_FORCE also needs live bridge.
        br = ensure_bridge(
            self.pid,
            log=log_fn,
            inject_if_needed=True,
            hwnd=hwnd or None,
            force_reinject=False,
        )
        self._br = br
        return br

    def _set_force(
        self, down: bool, *, quiet: bool = False, level_only: bool = False
    ) -> bool:
        br = self._open_bridge(quiet=quiet)
        if br is None:
            self._status("准星: 桥接未就绪，请先完成注入")
            return False
        only_level = bool(level_only) or (bool(down) and self._forced)
        vk = int(self.cfg.vk or VK_SHIFT) & 0xFF
        soft = bool(getattr(self.cfg, "allow_softsend", False))
        eng = self._engine()
        try:
            if eng == "force":
                r = br.key_force(
                    vk,
                    down=bool(down),
                    level_only=only_level,
                    hwnd=self.hwnd or None,
                    timeout_ms=2500,
                )
            else:
                if down and not self._hooks_ready:
                    hi = br.key_hold(
                        vk,
                        install_only=True,
                        allow_softsend=soft,
                        hwnd=self.hwnd or None,
                        timeout_ms=3000,
                    )
                    if not hi.ok:
                        err = (hi.error or hi.note or "").strip()
                        self._status(f"准星: Hook 安装失败 {err}")
                        self._br = None
                        return False
                    self._hooks_ready = True
                r = br.key_hold(
                    vk,
                    down=bool(down),
                    level_only=only_level,
                    allow_softsend=soft,
                    hwnd=self.hwnd or None,
                    timeout_ms=2500,
                )
        except Exception as e:
            self._br = None
            self._hooks_ready = False
            self._status(f"准星: 调用异常 {e}")
            return False
        if not r.ok:
            self._br = None
            self._hooks_ready = False
            err = (r.error or r.note or "").strip()
            low = err.lower()
            if "unknown cmd" in low or "unsupported" in low:
                self._status(
                    "准星: 桥接过旧 — 请退出游戏后 Delete 注入最新桥接"
                )
            else:
                self._status(f"准星: 失败 {err}")
            return False
        self._forced = bool(down)
        if down and not quiet and not only_level:
            note = (r.note or "").strip()
            if note:
                self._status(f"准星: {note}")
        return True

    def _clear_force(self) -> None:
        try:
            self._set_force(False, quiet=True)
        except Exception:
            pass
        self._forced = False

    def _run(self) -> None:
        mode = (self.cfg.mode or MODE_HOLD).strip().lower()
        if mode not in (MODE_HOLD, MODE_CYCLE):
            mode = MODE_HOLD
        eng = self._engine()
        eng_cn = "KEY_HOLD(无SoftSend)" if eng == "hold" else "KEY_FORCE(SoftSend)"
        mode_cn = "一直按住 Shift" if mode == MODE_HOLD else "单次循环"
        self._status(
            f"准星: 开始 {mode_cn} · {eng_cn} — "
            f"请确保技能可 free-aim；点停止会松开 Shift"
        )
        try:
            if mode == MODE_HOLD:
                self._loop_hold()
            else:
                self._loop_cycle()
        finally:
            self._clear_force()
            try:
                if self._br is not None:
                    self._br.close()
            except Exception:
                pass
            self._br = None
            self._status("准星: 已停止（已松开 Shift）")

    def _loop_hold(self) -> None:
        if not self._set_force(True):
            return
        self._status("准星: Shift 已按住（进程内）— 保持 free-aim 技能可用")
        refresh = max(200, int(self.cfg.refresh_ms or 400)) / 1000.0
        while not self._stop.is_set():
            if self._stop.wait(refresh):
                break
            if not self._set_force(True, quiet=True, level_only=True):
                if self._stop.wait(0.5):
                    break

    def _loop_cycle(self) -> None:
        on_ms = max(50, int(self.cfg.hold_ms or 800))
        off_s = max(0.05, int(self.cfg.release_ms or 200) / 1000.0)
        self._status(f"准星: 循环 按下{on_ms}ms / 松开{int(off_s * 1000)}ms")
        while not self._stop.is_set():
            self._forced = False
            if not self._set_force(True, quiet=True, level_only=False):
                if self._stop.wait(0.5):
                    break
                continue
            if self._stop.wait(on_ms / 1000.0):
                break
            self._set_force(False, quiet=True, level_only=False)
            self._forced = False
            if self._stop.wait(off_s):
                break


class MouseClickerRunner:
    """
    Background mouse auto-clicker using process-local button state plus messages.

    @author by ak
    """

    def __init__(
        self,
        pid: int,
        hwnd: int = 0,
        cfg: MouseClickerConfig | None = None,
        *,
        log: LogFn | None = None,
        on_status: Callable[[str], None] | None = None,
        name: str = "mouse-click",
    ) -> None:
        self.pid = int(pid)
        self.hwnd = int(hwnd or 0)
        self.cfg = cfg or MouseClickerConfig()
        self._log = log or (lambda _m: None)
        self._on_status = on_status or (lambda _m: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._name = name or "mouse-click"

    def is_running(self) -> bool:
        """True while worker thread is alive. @author by ak"""
        t = self._thread
        return t is not None and t.is_alive()

    def start(self) -> bool:
        """Start click loop. @author by ak"""
        if self.is_running():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=self._name, daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> bool:
        """Stop click loop. @author by ak"""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        stopped = not bool(t and t.is_alive())
        if stopped:
            self._thread = None
        return stopped

    def _status(self, msg: str) -> None:
        self._on_status(msg)
        self._log(msg)

    def _btn_label(self) -> str:
        return "右键" if int(self.cfg.button) == UI_CLICK_RIGHT else "左键"

    def _open_bridge(self):
        hwnd = _resolve_hwnd(self.pid, self.hwnd)
        if hwnd:
            self.hwnd = hwnd
        # Background skill-cancel depends on inject; try once if missing.
        return ensure_bridge(
            self.pid,
            log=self._log,
            inject_if_needed=True,
            hwnd=hwnd or None,
            force_reinject=False,
        )

    def _run(self) -> None:
        label = self._btn_label()
        interval = max(20, int(self.cfg.interval_ms or 100)) / 1000.0
        hold_ms = max(0, int(self.cfg.hold_ms or 40))
        btn = (
            UI_CLICK_RIGHT
            if int(self.cfg.button) == UI_CLICK_RIGHT
            else UI_CLICK_LEFT
        )
        mouse_vk = VK_RBUTTON if btn == UI_CLICK_RIGHT else VK_LBUTTON
        cx0 = int(self.cfg.cx or 0)
        cy0 = int(self.cfg.cy or 0)
        pos_txt = f"({cx0},{cy0})" if cx0 > 0 and cy0 > 0 else "中心"
        self._status(
            f"天机连点({label}): 开始 点={pos_txt} cycle={int(interval * 1000)}ms"
        )
        br = None
        first_click = True
        try:
            while not self._stop.is_set():
                cycle_started = time.monotonic()
                if br is None:
                    br = self._open_bridge()
                if br is None:
                    self._status(f"天机连点({label}): 桥接未就绪，请先 Delete 注入")
                    if self._stop.wait(0.8):
                        break
                    continue
                hwnd = self.hwnd
                cx = int(self.cfg.cx or 0)
                cy = int(self.cfg.cy or 0)
                if cx <= 0 or cy <= 0:
                    cx, cy = _client_center(hwnd)
                force_on = False
                try:
                    held = br.key_hold(
                        mouse_vk,
                        down=True,
                        level_only=False,
                        allow_softsend=False,
                        hwnd=hwnd or None,
                        timeout_ms=2000,
                    )
                    force_on = bool(held.ok)
                    if not force_on:
                        self._status(
                            f"天机连点({label}): 鼠标键 Hook 失败 "
                            f"{held.error or held.note or ''}"
                        )
                        if self._stop.wait(0.5):
                            break
                        continue
                    # Run DOWN/UP from this worker instead of CMD_UI_CLICK. The
                    # bridge command runs on the game's UI thread, where its
                    # hold sleep prevents the queued DOWN from being consumed
                    # until after UP is already queued. KEY_HOLD above also
                    # exposes the button to process-local GetAsyncKeyState polls.
                    from app.core.win_capture import post_click_client

                    click_ok = post_click_client(
                        hwnd,
                        cx,
                        cy,
                        right=(btn == UI_CLICK_RIGHT),
                        down_hold_s=max(0.0, hold_ms / 1000.0),
                        stop_event=self._stop,
                        log=self._log,
                        move_first=first_click,
                        settle_after=False,
                    )
                    first_click = False
                finally:
                    if force_on:
                        released = False
                        release_note = ""
                        for attempt in range(2):
                            try:
                                rel = br.key_hold(
                                    mouse_vk,
                                    down=False,
                                    level_only=False,
                                    allow_softsend=False,
                                    hwnd=hwnd or None,
                                    timeout_ms=2000,
                                )
                                released = bool(rel.ok)
                                release_note = str(rel.error or rel.note or "")
                            except Exception as e:
                                release_note = str(e)
                            if released:
                                break
                            if attempt == 0:
                                time.sleep(0.05)
                        if not released:
                            self._status(
                                f"天机连点({label}): 鼠标键释放失败 {release_note}"
                            )
                            try:
                                br.close()
                            except Exception:
                                pass
                            br = None
                if not click_ok:
                    self._status(
                        f"天机连点({label}): 失败 后台鼠标消息未送达"
                    )
                    if self._stop.wait(0.5):
                        break
                    continue
                elapsed = max(0.0, time.monotonic() - cycle_started)
                if self._stop.wait(max(0.0, interval - elapsed)):
                    break
        finally:
            if br is not None:
                try:
                    br.close()
                except Exception:
                    pass
            self._status(f"天机连点({label}): 已停止")


def parse_skill_vk(raw) -> int:
    """
    Parse skill hotkey to virtual-key code.

    Accepts int (already VK), digit label '1'..'0', or hex string '0x31'.
    Default 0x31 (key '1').
    """
    if raw is None:
        return 0x31
    if isinstance(raw, int):
        v = int(raw)
        return v if 1 <= v <= 0xFE else 0x31
    s = str(raw).strip()
    if not s:
        return 0x31
    if s in _SKILL_VK_BY_LABEL:
        return int(_SKILL_VK_BY_LABEL[s])
    low = s.lower()
    if low.startswith("0x"):
        try:
            v = int(low, 16)
            return v if 1 <= v <= 0xFE else 0x31
        except Exception:
            return 0x31
    if s.isdigit() and len(s) == 1 and s in _SKILL_VK_BY_LABEL:
        return int(_SKILL_VK_BY_LABEL[s])
    try:
        v = int(s, 10)
        if 1 <= v <= 0xFE:
            return v
    except Exception:
        pass
    return 0x31


def skill_vk_label(vk: int) -> str:
    """Human label for skill VK (digit or hex)."""
    v = int(vk or 0)
    for label, code in _SKILL_VK_BY_LABEL.items():
        if int(code) == v:
            return label
    for label, code in _NAMED_VK.items():
        if int(code) == v and label not in ("Escape",):
            return label
    return f"0x{v:02X}"


def parse_named_vk(raw) -> int:
    """Parse any named / digit / hex key (sequence step or cancel)."""
    if raw is None:
        return 0
    if isinstance(raw, int):
        v = int(raw)
        return v if 1 <= v <= 0xFE else 0
    s = str(raw).strip()
    if not s:
        return 0
    if s in _NAMED_VK:
        return int(_NAMED_VK[s])
    if s in _SKILL_VK_BY_LABEL:
        return int(_SKILL_VK_BY_LABEL[s])
    low = s.lower()
    for k, code in _NAMED_VK.items():
        if k.lower() == low:
            return int(code)
    named = {
        "esc": VK_ESCAPE,
        "escape": VK_ESCAPE,
        "rmb": VK_RBUTTON,
        "right": VK_RBUTTON,
        "rbutton": VK_RBUTTON,
        "space": VK_SPACE,
        "spc": VK_SPACE,
    }
    if low in named:
        return int(named[low])
    return parse_skill_vk(s)


def parse_cancel_vk(raw) -> int:
    """
    Parse cancel hotkey: named Esc/Space/C…, digit, or hex/int VK.
    Default Esc. RMB is encoded as VK_RBUTTON (handled as right-click).
    """
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return VK_ESCAPE
    v = parse_named_vk(raw)
    return v if v else VK_ESCAPE


def cancel_vk_label(vk: int) -> str:
    """Human label for cancel VK."""
    v = int(vk or 0)
    for label, code in _CANCEL_VK_BY_LABEL.items():
        if int(code) == v:
            return label
    return skill_vk_label(v)


@dataclass(frozen=True)
class KeyStep:
    """One G HUB-style block: press key hold_ms, then wait after_ms."""

    vk: int
    hold_ms: int = 50
    after_ms: int = 50

    def label(self) -> str:
        return skill_vk_label(self.vk)


def parse_key_sequence(raw) -> list[KeyStep]:
    """
    Parse compact sequence string used by UI / settings.

    Format (comma-separated steps):
      Key[:hold_ms[:after_ms]]
    Examples:
      Space:50:50,Q:50:50,Q:260:260,X:50:50,X:100:100
      1:40:80
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        out: list[KeyStep] = []
        for item in raw:
            if isinstance(item, KeyStep):
                out.append(item)
            elif isinstance(item, dict):
                vk = parse_named_vk(item.get("key") or item.get("vk"))
                if not vk:
                    continue
                out.append(
                    KeyStep(
                        vk=vk,
                        hold_ms=max(0, int(item.get("hold_ms") or 50)),
                        after_ms=max(0, int(item.get("after_ms") or 50)),
                    )
                )
        return out
    s = str(raw).strip()
    if not s:
        return []
    out = []
    for part in s.replace(";", ",").split(","):
        token = part.strip()
        if not token:
            continue
        bits = [b.strip() for b in token.split(":")]
        vk = parse_named_vk(bits[0])
        if not vk:
            continue
        hold = 50
        after = 50
        if len(bits) >= 2:
            try:
                hold = max(0, int(float(bits[1])))
            except Exception:
                hold = 50
        if len(bits) >= 3:
            try:
                after = max(0, int(float(bits[2])))
            except Exception:
                after = 50
        out.append(KeyStep(vk=vk, hold_ms=hold, after_ms=after))
    return out


def format_key_sequence(steps: list[KeyStep] | None) -> str:
    """Serialize steps back to compact string."""
    if not steps:
        return ""
    parts = []
    for st in steps:
        parts.append(f"{st.label()}:{int(st.hold_ms)}:{int(st.after_ms)}")
    return ",".join(parts)


def default_logitech_steps() -> list[KeyStep]:
    """Preset matching recorded G HUB: Space Space Q Q X X."""
    return parse_key_sequence(DEFAULT_LOGITECH_SEQUENCE)


def foreground_key_event(vk: int, *, key_up: bool = False, hwnd: int = 0) -> bool:
    """Compat wrapper → system-level send_key_event."""
    return bool(
        send_key_event(vk, key_up=key_up, hwnd=int(hwnd or 0)).ok
    )


def foreground_key_press(
    vk: int,
    *,
    hold_ms: int = 50,
    hwnd: int = 0,
    stop_event: threading.Event | None = None,
) -> bool:
    """Compat wrapper → system-level send_key_press."""
    return bool(
        send_key_press(
            vk,
            hold_ms=hold_ms,
            hwnd=int(hwnd or 0),
            stop_event=stop_event,
        ).ok
    )


def legacy_mode_to_pipeline(mode: str) -> tuple[str, bool, bool]:
    """
    Map old single-mode string to (when, use_protocol, use_key).

    Returns cancel_when, use_protocol_cancel, use_key_cancel.
    """
    m = str(mode or "").strip().lower()
    if m == CANCEL_MODE_KEY_ONLY:
        return CANCEL_WHEN_NEVER, False, False
    if m == CANCEL_MODE_CANCEL_THEN_KEY:
        return CANCEL_WHEN_ALWAYS, True, False
    if m == CANCEL_MODE_SMART:
        return CANCEL_WHEN_BUSY, True, False
    if m in CANCEL_WHEN_POLICIES:
        return m, False, False
    return CANCEL_WHEN_NEVER, False, False


@dataclass
class SkillCancelLoopConfig:
    """
    Two-step skill loop: [cancel channels] then [cast / sequence].

    Step 1 cancel (any combination, order fixed):
      - use_protocol_cancel: CMD_CANCEL_SESSION (verified packet path)
      - use_key_cancel: press cancel_vk (or RMB click)
      - use_memory_cancel: reserved; blocked until lab path is product-ready

    Step 2 cast:
      - cast_mode=single: press skill_vk
      - cast_mode=sequence: play sequence steps (G HUB timeline)

    delivery:
      - bridge: CMD_UI_KEY via inject, no_focus (background default)
      - foreground: SendInput (needs game focused; opt-in only)

    Safe default: stopped channels. 0x21 / macros are opt-in until lab proves
    skill-recovery (not whole-cast cancel). Do not auto-fire protocol cancel.
    """

    cancel_when: str = CANCEL_WHEN_NEVER
    use_protocol_cancel: bool = False
    use_key_cancel: bool = False
    use_memory_cancel: bool = False  # reserved / not product-ready
    cast_mode: str = CAST_MODE_NONE
    delivery: str = DELIVERY_BRIDGE
    skill_vk: int = 0x31
    cancel_vk: int = VK_ESCAPE
    sequence: str = DEFAULT_LOGITECH_SEQUENCE
    steps: list = field(default_factory=list)
    interval_ms: int = 50
    key_hold_ms: int = 40
    cancel_wait_ms: int = 40
    cancel_idle_hits: int = 1
    cancel_poll_ms: int = 40
    cancel_timeout_ms: int = 1200
    max_fail_streak: int = 10
    require_foreground: bool = False
    # Legacy field: if set, applied by normalize_skill_cancel_config
    mode: str = ""

    def normalized(self) -> "SkillCancelLoopConfig":
        """Apply legacy mode + clamp flags; return self for chaining."""
        return normalize_skill_cancel_config(self)


def normalize_skill_cancel_config(
    cfg: SkillCancelLoopConfig | None,
) -> SkillCancelLoopConfig:
    """Resolve legacy mode and sanitize channel flags."""
    c = cfg or SkillCancelLoopConfig()
    legacy = str(c.mode or "").strip().lower()
    if legacy in CANCEL_LOOP_MODES:
        when, proto, key = legacy_mode_to_pipeline(legacy)
        c.cancel_when = when
        c.use_protocol_cancel = proto
        c.use_key_cancel = key
        c.use_memory_cancel = False
        c.mode = ""
    when = str(c.cancel_when or CANCEL_WHEN_NEVER).strip().lower()
    if when not in CANCEL_WHEN_POLICIES:
        when = CANCEL_WHEN_NEVER
    c.cancel_when = when
    c.use_protocol_cancel = bool(c.use_protocol_cancel)
    c.use_key_cancel = bool(c.use_key_cancel)
    # Memory cancel is reserved: never enable from config alone.
    c.use_memory_cancel = False
    cast = str(c.cast_mode or CAST_MODE_NONE).strip().lower()
    if cast not in CAST_MODES:
        cast = CAST_MODE_NONE
    c.cast_mode = cast
    delivery = str(c.delivery or DELIVERY_FOREGROUND).strip().lower()
    if delivery == DELIVERY_SYSTEM:
        delivery = DELIVERY_FOREGROUND
    if delivery not in DELIVERY_MODES:
        delivery = DELIVERY_BRIDGE
    c.delivery = delivery
    # Background bridge never needs focus steal.
    if delivery == DELIVERY_BRIDGE:
        c.require_foreground = False
    c.skill_vk = parse_skill_vk(c.skill_vk)
    c.cancel_vk = parse_cancel_vk(c.cancel_vk)
    seq_steps = parse_key_sequence(c.steps) if c.steps else []
    if not seq_steps:
        seq_steps = parse_key_sequence(c.sequence)
    if not seq_steps and cast == CAST_MODE_SEQUENCE:
        seq_steps = default_logitech_steps()
    c.steps = seq_steps
    c.sequence = format_key_sequence(seq_steps) if seq_steps else str(
        c.sequence or ""
    )
    c.interval_ms = max(0, int(c.interval_ms if c.interval_ms is not None else 10))
    c.key_hold_ms = max(0, int(c.key_hold_ms if c.key_hold_ms is not None else 40))
    c.cancel_wait_ms = max(
        0, int(c.cancel_wait_ms if c.cancel_wait_ms is not None else 40)
    )
    c.require_foreground = bool(c.require_foreground)
    return c


class SkillCancelLoopRunner:
    """
    Background two-step loop: optional cancel channels, then skill key.

    Mirrors a programmable mouse macro; channels are independently selectable.
    """

    def __init__(
        self,
        pid: int,
        hwnd: int = 0,
        cfg: SkillCancelLoopConfig | None = None,
        *,
        log: LogFn | None = None,
        on_status: Callable[[str], None] | None = None,
        name: str = "skill-cancel",
    ) -> None:
        self.pid = int(pid)
        self.hwnd = int(hwnd or 0)
        self.cfg = normalize_skill_cancel_config(cfg)
        self._log = log or (lambda _m: None)
        self._on_status = on_status or (lambda _m: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._name = name or "skill-cancel"
        self._session = None
        self._fail_streak = 0

    def is_running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self) -> bool:
        if self.is_running():
            return False
        self.cfg = normalize_skill_cancel_config(self.cfg)
        self._stop.clear()
        self._fail_streak = 0
        self._thread = threading.Thread(
            target=self._run, name=self._name, daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> bool:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        stopped = not bool(t and t.is_alive())
        if stopped:
            self._thread = None
            self._close_session()
        return stopped

    def _status(self, msg: str) -> None:
        self._on_status(msg)
        self._log(msg)

    def _open_bridge(self):
        hwnd = _resolve_hwnd(self.pid, self.hwnd)
        if hwnd:
            self.hwnd = hwnd
        # Background skill-cancel depends on inject; try once if missing.
        return ensure_bridge(
            self.pid,
            log=self._log,
            inject_if_needed=True,
            hwnd=hwnd or None,
            force_reinject=False,
        )

    def _resolve_game_hwnd(self) -> int:
        hwnd = _resolve_hwnd(self.pid, self.hwnd)
        if hwnd:
            self.hwnd = hwnd
        return int(self.hwnd or 0)

    def _ensure_foreground(self) -> bool:
        """Bring game window forward for system SendInput path."""
        hwnd = self._resolve_game_hwnd()
        if not hwnd:
            return False
        ok = focus_window(hwnd, log=self._log)
        if not ok:
            self._log("技能取消: 前台聚焦失败")
        return ok

    def _press_key(
        self,
        br,
        vk: int,
        hold_ms: int,
        *,
        refocus: bool = False,
    ) -> tuple[bool, str]:
        """Deliver one key press via system SendInput or bridge KEY_HOLD."""
        delivery = str(self.cfg.delivery or DELIVERY_FOREGROUND).lower()
        if delivery == DELIVERY_SYSTEM:
            delivery = DELIVERY_FOREGROUND
        hold = max(0, int(hold_ms))
        hwnd = self._resolve_game_hwnd()
        # System path: no bridge required.
        if delivery == DELIVERY_FOREGROUND or br is None:
            if refocus and self.cfg.require_foreground:
                self._ensure_foreground()
            r = send_key_press(
                int(vk),
                hold_ms=hold,
                hwnd=hwnd,
                post_message=False,
                use_keybd_event=False,
                stop_event=self._stop,
            )
            if self._stop.is_set():
                return False, "stopped"
            if not r.ok:
                return False, r.error or r.note or "sys SendInput fail"
            return True, r.note or "sys key ok"
        # Bridge path: use the verified process-local key-state hooks. This
        # never steals foreground and keeps macro timing identical to a real
        # down/hold/up sequence.
        try:
            down = br.key_hold(
                int(vk),
                down=True,
                allow_softsend=False,
                hwnd=self.hwnd or None,
                timeout_ms=2000,
            )
        except Exception as e:
            return False, str(e)
        note = (down.note or down.error or "").strip()
        if not down.ok:
            return False, note or "KEY_HOLD down fail"
        stopped = self._stop.wait(hold / 1000.0) if hold > 0 else False
        try:
            up = br.key_hold(
                int(vk),
                down=False,
                allow_softsend=False,
                hwnd=self.hwnd or None,
                timeout_ms=2000,
            )
        except Exception as e:
            return False, f"KEY_HOLD up: {e}"
        up_note = (up.note or up.error or "").strip()
        if not up.ok:
            return False, up_note or "KEY_HOLD up fail"
        if stopped or self._stop.is_set():
            return False, "stopped"
        return True, up_note or note or "KEY_HOLD background ok"

    def _ensure_session(self):
        if self._session is not None:
            return self._session
        try:
            from app.core.loot import open_attach_session

            self._session = open_attach_session(self.pid, log=lambda _m: None)
        except Exception as e:
            self._log(f"技能取消: attach 失败 {e}")
            self._session = None
        return self._session

    def _close_session(self) -> None:
        sess = self._session
        self._session = None
        if sess is None:
            return
        try:
            close = getattr(sess, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    def _read_cast_busy(self) -> bool | None:
        """True if cast/session looks active; None if unreadable."""
        sess = self._ensure_session()
        if sess is None:
            return None
        try:
            from app.core.loot import read_host_cast_state

            st = read_host_cast_state(sess, log=lambda _m: None)
        except Exception:
            return None
        if not st.get("readable") and not st.get("session_readable"):
            return None
        return bool(st.get("active") or st.get("session_active"))

    def _wait_cast_idle(self, timeout_ms: int) -> bool:
        """Poll until cast looks idle, or timeout / stop."""
        need = max(1, int(self.cfg.cancel_idle_hits or 1))
        poll = max(20, int(self.cfg.cancel_poll_ms or 40)) / 1000.0
        deadline = time.monotonic() + max(0.05, float(timeout_ms) / 1000.0)
        hits = 0
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return False
            busy = self._read_cast_busy()
            if busy is False:
                hits += 1
                if hits >= need:
                    return True
            else:
                hits = 0
            if self._stop.wait(poll):
                return False
        return False

    def _should_cancel(self) -> bool:
        """Whether step-1 cancel channels should run this cycle."""
        when = str(self.cfg.cancel_when or CANCEL_WHEN_NEVER).lower()
        if when == CANCEL_WHEN_NEVER:
            return False
        if when == CANCEL_WHEN_ALWAYS:
            return True
        # busy
        busy = self._read_cast_busy()
        if busy is True:
            return True
        if busy is False:
            return False
        return True

    def _any_cancel_channel(self) -> bool:
        return bool(
            self.cfg.use_protocol_cancel
            or self.cfg.use_key_cancel
            or self.cfg.use_memory_cancel
        )

    def _do_protocol_cancel(self, br) -> tuple[bool, str]:
        try:
            r = br.cancel_session(
                hwnd=self.hwnd or None,
                timeout_ms=3000,
            )
        except Exception as e:
            return False, str(e)
        note = (r.note or r.error or "").strip()
        if not r.ok:
            return False, note or "CANCEL_SESSION fail"
        if "already idle" in note.lower():
            return True, note
        wait_ms = max(0, int(self.cfg.cancel_wait_ms or 0))
        timeout_ms = max(wait_ms, int(self.cfg.cancel_timeout_ms or 1500))
        if wait_ms > 0:
            if self._stop.wait(wait_ms / 1000.0):
                return False, "stopped"
        if self._wait_cast_idle(timeout_ms):
            return True, note or "cancel idle"
        busy = self._read_cast_busy()
        if busy is None:
            return True, note or "cancel queued (state unread)"
        if busy is False:
            return True, note or "cancel idle"
        return True, note or "cancel queued (still busy)"

    def _do_key_cancel(self, br) -> tuple[bool, str]:
        """Press cancel_vk or right-click (RMB) via system or bridge."""
        vk = parse_cancel_vk(self.cfg.cancel_vk)
        hold = max(0, int(self.cfg.key_hold_ms or 50))
        if int(vk) == VK_RBUTTON:
            delivery = str(self.cfg.delivery or DELIVERY_FOREGROUND).lower()
            hwnd = self._resolve_game_hwnd()
            cx, cy = _client_center(hwnd)
            if delivery in (DELIVERY_FOREGROUND, DELIVERY_SYSTEM) or br is None:
                r = send_mouse_click(
                    right=True,
                    hold_ms=hold,
                    hwnd=hwnd,
                    client_x=cx,
                    client_y=cy,
                    stop_event=self._stop,
                )
                if not r.ok:
                    return False, r.error or "sys RMB fail"
                return True, "cancel RMB sys"
            try:
                br_r = br.ui_click(
                    cx,
                    cy,
                    hwnd=self.hwnd or None,
                    hold_ms=hold,
                    button=UI_CLICK_RIGHT,
                    timeout_ms=2000,
                )
            except Exception as e:
                return False, str(e)
            note = (br_r.note or br_r.error or "").strip()
            if not br_r.ok:
                return False, note or "UI_CLICK R fail"
            return True, note or "cancel RMB ok"
        ok, note = self._press_key(br, vk, hold)
        if not ok:
            return False, note or "cancel key fail"
        return True, f"cancel key {cancel_vk_label(vk)}"

    def _do_memory_cancel(self) -> tuple[bool, str]:
        """Reserved: cast memory clear not product-ready."""
        return False, "memory cancel not enabled (lab only)"

    def _run_cancel_step(self, br) -> tuple[bool, str]:
        """
        Step 1: run enabled cancel channels in fixed order.
        protocol → key → memory(reserved).
        """
        if not self._should_cancel() or not self._any_cancel_channel():
            return True, "skip cancel"
        parts: list[str] = []
        ok_all = True
        if self.cfg.use_protocol_cancel:
            if br is None:
                ok_all = False
                parts.append("proto:bridge required")
            else:
                ok, note = self._do_protocol_cancel(br)
                parts.append(f"proto:{note}")
                if not ok and "stopped" not in note:
                    ok_all = False
            if self._stop.is_set():
                return False, "stopped"
            wait_ms = max(0, int(self.cfg.cancel_wait_ms or 0))
            if wait_ms > 0 and self.cfg.use_key_cancel:
                if self._stop.wait(wait_ms / 1000.0):
                    return False, "stopped"
        if self.cfg.use_key_cancel:
            ok, note = self._do_key_cancel(br)
            parts.append(f"key:{note}")
            if not ok:
                ok_all = False
            if self._stop.is_set():
                return False, "stopped"
            wait_ms = max(0, int(self.cfg.cancel_wait_ms or 0))
            if wait_ms > 0:
                if self._stop.wait(wait_ms / 1000.0):
                    return False, "stopped"
        if self.cfg.use_memory_cancel:
            ok, note = self._do_memory_cancel()
            parts.append(f"mem:{note}")
            if not ok:
                ok_all = False
        return ok_all, " | ".join(parts) if parts else "skip cancel"

    def _play_sequence(self, br, *, refocus: bool = False) -> tuple[bool, str]:
        """Play G HUB-style KeyStep list once."""
        steps = list(self.cfg.steps or [])
        if not steps:
            steps = default_logitech_steps()
        labels = []
        for i, st in enumerate(steps):
            if self._stop.is_set():
                return False, "stopped"
            ok, note = self._press_key(
                br, int(st.vk), int(st.hold_ms), refocus=(refocus and i == 0)
            )
            labels.append(st.label())
            if not ok:
                return False, f"seq {st.label()}: {note}"
            after = max(0, int(st.after_ms)) / 1000.0
            if after > 0 and self._stop.wait(after):
                return False, "stopped"
        return True, "seq " + "-".join(labels)

    def _do_cast_step(self, br, *, refocus: bool = False) -> tuple[bool, str]:
        """Step 2: optional key cast. CAST_MODE_NONE = no keys at all."""
        mode = str(self.cfg.cast_mode or CAST_MODE_NONE)
        if mode == CAST_MODE_NONE:
            return True, "cast skipped (protocol-only)"
        if mode == CAST_MODE_SEQUENCE:
            return self._play_sequence(br, refocus=refocus)
        vk = parse_skill_vk(self.cfg.skill_vk)
        hold = max(0, int(self.cfg.key_hold_ms or 50))
        return self._press_key(br, vk, hold, refocus=refocus)

    def _channel_summary(self) -> str:
        chans = []
        if self.cfg.use_protocol_cancel:
            chans.append("协议")
        if self.cfg.use_key_cancel:
            chans.append(f"按键({cancel_vk_label(self.cfg.cancel_vk)})")
        if self.cfg.use_memory_cancel:
            chans.append("内存")
        if not chans:
            chans.append("无")
        return "+".join(chans)

    def _cast_summary(self) -> str:
        mode = str(self.cfg.cast_mode or CAST_MODE_NONE)
        if mode == CAST_MODE_NONE:
            return "none(no-keys)"
        if mode == CAST_MODE_SEQUENCE:
            steps = self.cfg.steps or default_logitech_steps()
            return "seq(" + format_key_sequence(steps) + ")"
        return f"key({skill_vk_label(self.cfg.skill_vk)})"

    def _needs_bridge(self) -> bool:
        # Protocol cancel always needs inject.
        if self.cfg.use_protocol_cancel:
            return True
        # Pure system delivery: no bridge for keys / RMB.
        if str(self.cfg.delivery) == DELIVERY_BRIDGE:
            return True
        return False

    def _run(self) -> None:
        self.cfg = normalize_skill_cancel_config(self.cfg)
        interval = max(0, int(self.cfg.interval_ms or 0)) / 1000.0
        when = self.cfg.cancel_when
        delivery = self.cfg.delivery
        self._status(
            f"技能取消: 开始 delivery={delivery} when={when} "
            f"cancel=[{self._channel_summary()}] cast={self._cast_summary()} "
            f"loop_gap={int(interval * 1000)}ms"
        )
        if str(self.cfg.cast_mode) == CAST_MODE_NONE and self.cfg.use_protocol_cancel:
            self._status(
                "技能取消: 协议后摇取消 — busy 时发 0x21，无按键 "
                "（后台/桥接，需注入）"
            )
        elif delivery == DELIVERY_BRIDGE:
            self._status("技能取消: 桥接后台 — 不抢焦点（需已注入 / 将自动尝试注入）")
        elif delivery == DELIVERY_FOREGROUND:
            self._status("技能取消: 系统发送 — 1.2s 内切到游戏")
            self._ensure_foreground()
            if self._stop.wait(1.2):
                self._close_session()
                self._status("技能取消: 已停止")
                return
            self._ensure_foreground()
        cycle_n = 0
        try:
            while not self._stop.is_set():
                br = None
                opened = False
                if self._needs_bridge() or delivery == DELIVERY_BRIDGE:
                    br = self._open_bridge()
                    opened = br is not None
                    if self._needs_bridge() and br is None:
                        self._status("技能取消: 桥接未就绪，请先 Delete 注入")
                        if self._stop.wait(0.8):
                            break
                        continue
                cycle_ok = True
                detail = ""
                cycle_n += 1
                # Re-focus rarely — focus steals input timing.
                do_refocus = delivery == DELIVERY_FOREGROUND and (
                    cycle_n == 1 or cycle_n % 40 == 0
                )
                try:
                    # Sequence already embeds cancel keys (Space/X).
                    # Optional protocol/key cancel runs first only when enabled.
                    ok_c, detail_c = self._run_cancel_step(br)
                    detail = detail_c
                    if not ok_c and "stopped" not in detail_c:
                        cycle_ok = False
                        self._status(f"技能取消: step1 失败 {detail_c}")
                    if self._stop.is_set():
                        break
                    ok_k, detail_k = self._do_cast_step(br, refocus=do_refocus)
                    if not ok_k:
                        cycle_ok = False
                        detail = detail_k
                        if cycle_n <= 3 or cycle_n % 30 == 0:
                            self._status(f"技能取消: step2 失败 {detail_k}")
                    elif detail == "skip cancel":
                        detail = detail_k
                finally:
                    if opened and br is not None:
                        try:
                            br.close()
                        except Exception:
                            pass
                if cycle_ok:
                    self._fail_streak = 0
                else:
                    self._fail_streak += 1
                    max_fail = max(1, int(self.cfg.max_fail_streak or 12))
                    if self._fail_streak >= max_fail:
                        self._status(
                            f"技能取消: 连续失败 {self._fail_streak} 次，停止 "
                            f"({detail})"
                        )
                        break
                if interval > 0 and self._stop.wait(interval):
                    break
                elif interval <= 0 and self._stop.is_set():
                    break
        finally:
            self._close_session()
            self._status("技能取消: 已停止")



