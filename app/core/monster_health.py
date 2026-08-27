# -*- coding: utf-8 -*-
"""Read the target monster health shown by the game's AUI target window.

The monster object fields previously sampled by the research scripts are not
authoritative real-time HP.  The target window is the client presentation
layer, and its ``Txt_HP`` control contains the same percentage rendered on
screen.  ``Prg_HP+0x17C`` is retained as a numeric cross-check.

All reads are ReadProcessMemory-only after the initial named-control lookup.
"""
from __future__ import annotations

import re
import struct
import time
from dataclasses import dataclass, asdict
from typing import Callable

from app.core.aui_click import get_aui_dlg_item_ptr
from app.core.plg_ui import get_game_ui_dlg
from app.core.remote_runtime import remote_read_bytes

LogFn = Callable[[str], None]
TARGET_DIALOG_NAME = "Win_TargetMonsterNPC"
TARGET_HP_TEXT = "Txt_HP"
TARGET_HP_PROGRESS = "Prg_HP"
TARGET_PROGRESS_VALUE_OFF = 0x17C
_PERCENT_RE = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*%")
_CAPTION_OFFSETS = (0xB8, 0xBC, 0xC0, 0xB0, 0xA8, 0xC4, 0xD0, 0xD4)


@dataclass(frozen=True)
class MonsterHealthSample:
    ok: bool
    hp_pct: float | None = None
    text: str = ""
    progress_pct: float | None = None
    selected_id64: int = 0
    dialog_ptr: int = 0
    text_ctrl: int = 0
    progress_ctrl: int = 0
    sampled_at: float = 0.0
    source: str = "target_ui"
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _read_u32(session, address: int) -> int:
    raw = _read_bytes(session, address, 4)
    return int(struct.unpack("<I", raw)[0]) if len(raw) == 4 else 0


def _read_bytes(session, address: int, size: int) -> bytes:
    pm = getattr(session, "pm", None)
    if pm is not None:
        try:
            import pymem.memory

            return pymem.memory.read_bytes(
                pm.process_handle, int(address) & 0xFFFFFFFF, int(size)
            )
        except Exception:
            return b""
    return remote_read_bytes(int(session.pid), int(address) & 0xFFFFFFFF, int(size))


def _decode_wstring(session, address: int, max_chars: int = 64) -> str:
    if not 0x10000 <= int(address) < 0x7FFE0000:
        return ""
    raw = _read_bytes(session, address, max_chars * 2 + 2)
    try:
        return raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]
    except Exception:
        return ""


def _read_caption(session, ctrl: int) -> str:
    if not ctrl:
        return ""
    raw = _read_bytes(session, ctrl, 0x160)
    if len(raw) < 0xC0:
        return ""
    for off in _CAPTION_OFFSETS:
        ptr = struct.unpack_from("<I", raw, off)[0]
        text = _decode_wstring(session, ptr)
        if text:
            return text
    return ""


def parse_percent(text: str) -> float | None:
    """Parse ``100%``/``37.5 %`` while rejecting unrelated text."""
    m = _PERCENT_RE.search(str(text or ""))
    if not m:
        return None
    try:
        return max(0.0, min(100.0, float(m.group(1).replace(",", "."))))
    except ValueError:
        return None


def read_selected_id64(session) -> int:
    """Read host-side selected target id64 without a game call."""
    root = _read_u32(session, int(session.module_base or 0x400000) + 0x15282D8 - 0x400000)
    mid = _read_u32(session, root + 0x24) if root else 0
    host = _read_u32(session, mid + 0x8C) if mid else 0
    if not host:
        return 0
    raw = _read_bytes(session, host + 0x19E8, 8)
    return int(struct.unpack("<Q", raw)[0]) if len(raw) == 8 else 0


class TargetMonsterHealthReader:
    """Bind target AUI controls once, then sample through RPM only."""

    def __init__(self, session, *, log: LogFn | None = None):
        self.session = session
        self.log = log or (lambda _m: None)
        self.dialog_ptr = 0
        self.text_ctrl = 0
        self.progress_ctrl = 0
        self.host_ptr = 0

    def bind(self) -> bool:
        self.dialog_ptr = int(
            get_game_ui_dlg(self.session, TARGET_DIALOG_NAME, log=self.log) or 0
        )
        if not self.dialog_ptr:
            return False
        self.text_ctrl = int(
            get_aui_dlg_item_ptr(
                self.session, self.dialog_ptr, TARGET_HP_TEXT, log=self.log
            )
            or 0
        )
        self.progress_ctrl = int(
            get_aui_dlg_item_ptr(
                self.session, self.dialog_ptr, TARGET_HP_PROGRESS, log=self.log
            )
            or 0
        )
        base = int(self.session.module_base or 0x400000)
        root = _read_u32(self.session, base + 0x15282D8 - 0x400000)
        mid = _read_u32(self.session, root + 0x24) if root else 0
        self.host_ptr = _read_u32(self.session, mid + 0x8C) if mid else 0
        return bool(self.text_ctrl or self.progress_ctrl)

    def _selected_id64(self) -> int:
        if not self.host_ptr:
            return 0
        raw = _read_bytes(self.session, self.host_ptr + 0x19E8, 8)
        return int(struct.unpack("<Q", raw)[0]) if len(raw) == 8 else 0

    def sample(self) -> MonsterHealthSample:
        now = time.time()
        if not self.dialog_ptr or not (self.text_ctrl or self.progress_ctrl):
            return MonsterHealthSample(ok=False, sampled_at=now, error="reader not bound")
        shown = _read_bytes(self.session, self.dialog_ptr + 0x94, 1)
        selected = self._selected_id64()
        if not shown or shown == b"\x00":
            return MonsterHealthSample(
                ok=False, selected_id64=selected, dialog_ptr=self.dialog_ptr,
                text_ctrl=self.text_ctrl, progress_ctrl=self.progress_ctrl,
                sampled_at=now, error="target dialog hidden",
            )
        text = _read_caption(self.session, self.text_ctrl)
        hp = parse_percent(text)
        progress = None
        if self.progress_ctrl:
            value = _read_u32(
                self.session, self.progress_ctrl + TARGET_PROGRESS_VALUE_OFF
            )
            if 0 <= value <= 100:
                progress = float(value)
        if hp is None:
            hp = progress
        return MonsterHealthSample(
            ok=hp is not None, hp_pct=hp, text=text, progress_pct=progress,
            selected_id64=selected, dialog_ptr=self.dialog_ptr,
            text_ctrl=self.text_ctrl, progress_ctrl=self.progress_ctrl,
            sampled_at=now,
            error=None if hp is not None else "Txt_HP has no percentage",
        )


def read_target_health(session, *, log: LogFn | None = None) -> MonsterHealthSample:
    """Return the currently selected monster's UI health percentage."""
    log = log or (lambda _m: None)
    try:
        reader = TargetMonsterHealthReader(session, log=log)
        if not reader.bind():
            return MonsterHealthSample(
                ok=False, selected_id64=read_selected_id64(session),
                sampled_at=time.time(), error="target controls unavailable",
            )
        return reader.sample()
    except Exception as exc:
        log(f"read_target_health err: {exc}")
        return MonsterHealthSample(ok=False, selected_id64=0, sampled_at=time.time(), error=str(exc))


class HealthThreshold:
    """Fire once when one selected id64 crosses down through a threshold."""

    def __init__(self, threshold_pct: float):
        self.threshold_pct = max(0.0, min(100.0, float(threshold_pct)))
        self._target = 0
        self._previous: float | None = None
        self._fired = False

    def update(self, sample: MonsterHealthSample) -> bool:
        target = int(sample.selected_id64 or 0)
        hp = sample.hp_pct
        if target != self._target:
            self._target, self._previous, self._fired = target, None, False
        if not sample.ok or hp is None:
            return False
        crossed = (
            not self._fired
            and self._previous is not None
            and self._previous > self.threshold_pct >= float(hp)
        )
        self._previous = float(hp)
        if crossed:
            self._fired = True
        return crossed


__all__ = [
    "MonsterHealthSample", "TargetMonsterHealthReader", "HealthThreshold",
    "parse_percent", "read_selected_id64", "read_target_health",
]
