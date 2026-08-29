# -*- coding: utf-8 -*-
"""Reader for the passive in-process AddChatMessage tap."""
from __future__ import annotations

import ctypes
import subprocess
import struct
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

CHAT_TAP_MAGIC = 0x50415443
CHAT_TAP_VERSION = 3
CHAT_TAP_CAPACITY = 50
CHAT_TAP_TEXT_CHARS = 256
CHAT_TAP_HEADER_SIZE = 156
CHAT_TAP_EVENT_SIZE = 540
# v3 = 主ring + 队伍ring + 私聊ring（channel==9），总尺寸 580124。
CHAT_TAP_SHARED_SIZE = 580124
# 队伍专用 ring（channel==3，主副控稳定读取）
TEAM_TAP_CAPACITY = 512
TEAM_TAP_WRITE_SEQ_OFF = 27156  # 主 events 之后
TEAM_TAP_EVENTS_OFF = 27160
# 私聊专用 ring（channel==9，组队前控制面：预检查/离队回执）
PRIVATE_TAP_CAPACITY = 512
PRIVATE_TAP_WRITE_SEQ_OFF = 303640  # 队伍 events 之后
PRIVATE_TAP_EVENTS_OFF = 303644
# v2 旧布局尺寸（无私聊 ring）。读取端兼容旧 DLL：主/队伍 ring 与 v3 完全同位。
CHAT_TAP_LEGACY_SIZE = 303640

CHAT_TAP_INIT = 0
CHAT_TAP_ACTIVE = 1
CHAT_TAP_ERROR = 2

_HEADER = struct.Struct("<7I128s")
_EVENT_META = struct.Struct("<7I")

FILE_MAP_READ = 0x0006

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenFileMappingW.argtypes = [
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
kernel32.OpenFileMappingW.restype = wintypes.HANDLE
kernel32.MapViewOfFile.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_size_t,
]
kernel32.MapViewOfFile.restype = wintypes.LPVOID
kernel32.UnmapViewOfFile.argtypes = [wintypes.LPCVOID]
kernel32.UnmapViewOfFile.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def parse_chat_tap_snapshot(
    data: bytes, cursor: int
) -> tuple[list[dict], int, int, dict]:
    """Parse committed ring entries after cursor from one atomic copy."""
    if len(data) < CHAT_TAP_LEGACY_SIZE:
        raise ValueError("chat tap mapping is truncated")
    magic, version, size, capacity, newest, status, target_va, error = (
        _HEADER.unpack_from(data, 0)
    )
    error_text = error.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    header = {
        "magic": magic,
        "version": version,
        "struct_size": size,
        "capacity": capacity,
        "write_seq": newest,
        "status": status,
        "target_va": target_va,
        "error": error_text,
    }
    if magic != CHAT_TAP_MAGIC:
        raise ValueError(f"chat tap magic mismatch: 0x{magic:08X}")
    if version == 2 and size == CHAT_TAP_LEGACY_SIZE:
        # v2 旧布局（无私聊 ring）：主/队伍 ring 与 v3 同位，允许直接解析。
        pass
    elif version != CHAT_TAP_VERSION or size != CHAT_TAP_SHARED_SIZE:
        raise ValueError(f"chat tap layout mismatch: version={version} size={size}")
    if capacity != CHAT_TAP_CAPACITY:
        raise ValueError(f"chat tap capacity mismatch: {capacity}")

    mark = max(0, int(cursor or 0))
    if newest <= mark:
        return [], mark, 0, header
    oldest = max(1, int(newest) - int(capacity) + 1)
    start = max(mark + 1, oldest)
    lost = max(0, oldest - (mark + 1))
    events: list[dict] = []
    consumed = start - 1
    for expected in range(start, int(newest) + 1):
        slot = (expected - 1) % int(capacity)
        offset = CHAT_TAP_HEADER_SIZE + slot * CHAT_TAP_EVENT_SIZE
        seq, tick_ms, thread_id, caller_va, channel, flags, text_len = (
            _EVENT_META.unpack_from(data, offset)
        )
        # write_seq is reserved before a slot is published. Stop at the first
        # uncommitted slot and retry it on the next poll.
        if seq != expected:
            break
        chars = min(int(text_len), CHAT_TAP_TEXT_CHARS - 1)
        raw = data[offset + _EVENT_META.size : offset + _EVENT_META.size + chars * 2]
        text = raw.decode("utf-16-le", errors="replace")
        events.append(
            {
                "seq": seq,
                "tick_ms": tick_ms,
                "thread_id": thread_id,
                "caller_va": caller_va,
                "channel": channel,
                "flags": flags,
                "text": text,
            }
        )
        consumed = expected
    return events, consumed, lost, header


def parse_team_events(
    data: bytes, cursor: int
) -> tuple[list[dict], int, int, dict]:
    """Parse the 队伍专用 ring（channel==3）after cursor from one atomic copy.

    与主 ring 同布局，但容量 TEAM_TAP_CAPACITY，偏移独立。
    @author by ak
    """
    if len(data) < CHAT_TAP_SHARED_SIZE:
        raise ValueError("chat tap mapping is truncated")
    newest = int.from_bytes(
        data[TEAM_TAP_WRITE_SEQ_OFF : TEAM_TAP_WRITE_SEQ_OFF + 4], "little"
    )
    header = {"team_write_seq": newest, "capacity": TEAM_TAP_CAPACITY}
    mark = max(0, int(cursor or 0))
    if newest <= mark:
        return [], mark, 0, header
    oldest = max(1, int(newest) - int(TEAM_TAP_CAPACITY) + 1)
    start = max(mark + 1, oldest)
    lost = max(0, oldest - (mark + 1))
    events: list[dict] = []
    consumed = start - 1
    for expected in range(start, int(newest) + 1):
        slot = (expected - 1) % int(TEAM_TAP_CAPACITY)
        offset = TEAM_TAP_EVENTS_OFF + slot * CHAT_TAP_EVENT_SIZE
        seq, tick_ms, thread_id, caller_va, channel, flags, text_len = (
            _EVENT_META.unpack_from(data, offset)
        )
        if seq != expected:
            break
        chars = min(int(text_len), CHAT_TAP_TEXT_CHARS - 1)
        raw = data[offset + _EVENT_META.size : offset + _EVENT_META.size + chars * 2]
        text = raw.decode("utf-16-le", errors="replace")
        events.append(
            {
                "seq": seq,
                "tick_ms": tick_ms,
                "thread_id": thread_id,
                "caller_va": caller_va,
                "channel": channel,
                "flags": flags,
                "text": text,
            }
        )
        consumed = expected
    return events, consumed, lost, header


def parse_private_events(
    data: bytes, cursor: int
) -> tuple[list[dict], int, int, dict]:
    """解析私聊专用 ring（channel==9）cursor 之后的新消息。

    布局与队伍 ring 完全同构，仅偏移/容量不同。
    @author by ak
    """
    if len(data) < CHAT_TAP_SHARED_SIZE:
        raise ValueError("chat tap mapping is truncated")
    newest = int.from_bytes(
        data[PRIVATE_TAP_WRITE_SEQ_OFF : PRIVATE_TAP_WRITE_SEQ_OFF + 4], "little"
    )
    header = {"private_write_seq": newest, "capacity": PRIVATE_TAP_CAPACITY}
    mark = max(0, int(cursor or 0))
    if newest <= mark:
        return [], mark, 0, header
    oldest = max(1, int(newest) - int(PRIVATE_TAP_CAPACITY) + 1)
    start = max(mark + 1, oldest)
    lost = max(0, oldest - (mark + 1))
    events: list[dict] = []
    consumed = start - 1
    for expected in range(start, int(newest) + 1):
        slot = (expected - 1) % int(PRIVATE_TAP_CAPACITY)
        offset = PRIVATE_TAP_EVENTS_OFF + slot * CHAT_TAP_EVENT_SIZE
        seq, tick_ms, thread_id, caller_va, channel, flags, text_len = (
            _EVENT_META.unpack_from(data, offset)
        )
        if seq != expected:
            break
        chars = min(int(text_len), CHAT_TAP_TEXT_CHARS - 1)
        raw = data[offset + _EVENT_META.size : offset + _EVENT_META.size + chars * 2]
        text = raw.decode("utf-16-le", errors="replace")
        events.append(
            {
                "seq": seq,
                "tick_ms": tick_ms,
                "thread_id": thread_id,
                "caller_va": caller_va,
                "channel": channel,
                "flags": flags,
                "text": text,
            }
        )
        consumed = expected
    return events, consumed, lost, header


class ChatTapReader:
    def __init__(
        self,
        pid: int,
        handle: int,
        view: int,
        *,
        size: int = CHAT_TAP_SHARED_SIZE,
        legacy: bool = False,
    ):
        self.pid = int(pid)
        self._handle = handle
        self._view = view
        self._size = int(size)
        # legacy=True：v2 旧 DLL（无私聊 ring），只有主/队伍 ring 可读。
        self.legacy = bool(legacy)

    @classmethod
    def open(cls, pid: int) -> "ChatTapReader | None":
        # 先按 v3 尺寸映射；失败（旧 DLL 小映射）回退 v2 尺寸。
        for size, legacy in (
            (CHAT_TAP_SHARED_SIZE, False),
            (CHAT_TAP_LEGACY_SIZE, True),
        ):
            for scope in ("Local", "Global"):
                name = f"{scope}\\XajhChatTap_{int(pid)}"
                handle = kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
                if not handle:
                    continue
                view = kernel32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, size)
                if view:
                    return cls(int(pid), handle, view, size=size, legacy=legacy)
                kernel32.CloseHandle(handle)
        return None

    def snapshot(self) -> bytes:
        if not self._view:
            raise RuntimeError("chat tap reader is closed")
        return ctypes.string_at(self._view, self._size)

    def header(self) -> dict:
        return parse_chat_tap_snapshot(self.snapshot(), 0)[3]

    @property
    def latest_cursor(self) -> int:
        return int(self.header()["write_seq"])

    def read_after(self, cursor: int) -> tuple[list[dict], int, int, dict]:
        return parse_chat_tap_snapshot(self.snapshot(), cursor)

    def read_team_after(self, cursor: int) -> tuple[list[dict], int, int, dict]:
        """读队伍专用 ring（channel==3）新增消息。@author by ak"""
        return parse_team_events(self.snapshot(), cursor)

    def latest_team_cursor(self) -> int:
        return int.from_bytes(
            self.snapshot()[TEAM_TAP_WRITE_SEQ_OFF : TEAM_TAP_WRITE_SEQ_OFF + 4],
            "little",
        )

    def read_private_after(self, cursor: int) -> tuple[list[dict], int, int, dict]:
        """读私聊专用 ring（channel==9）新增消息。@author by ak"""
        if self.legacy:
            raise RuntimeError("chat tap v2 无私聊专用 ring")
        return parse_private_events(self.snapshot(), cursor)

    def latest_private_cursor(self) -> int:
        if self.legacy:
            raise RuntimeError("chat tap v2 无私聊专用 ring")
        return int.from_bytes(
            self.snapshot()[PRIVATE_TAP_WRITE_SEQ_OFF : PRIVATE_TAP_WRITE_SEQ_OFF + 4],
            "little",
        )

    def close(self) -> None:
        if self._view:
            kernel32.UnmapViewOfFile(self._view)
            self._view = 0
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = 0


def wait_for_chat_tap(pid: int, timeout_s: float = 5.0) -> ChatTapReader | None:
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        reader = ChatTapReader.open(pid)
        if reader is not None:
            return reader
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def default_chat_tap_paths() -> tuple[Path, Path]:
    """Locate the passive tap DLL and the existing x86 injector."""
    try:
        from common.paths import NATIVE_BIN_DIR, app_root, bundle_root
    except Exception:
        root = Path(__file__).resolve().parents[2]
        NATIVE_BIN_DIR = root / "native" / "bin"
        app_root = lambda: root  # noqa: E731
        bundle_root = app_root
    search: list[Path] = []
    for directory in (
        app_root() / "native" / "bin",
        NATIVE_BIN_DIR,
        bundle_root() / "native" / "bin",
        app_root() / "_internal" / "native" / "bin",
        Path(__file__).resolve().parents[2] / "native" / "bin",
    ):
        if directory not in search:
            search.append(directory)
    dll = next(
        (directory / "xajh_chat_tap.dll" for directory in search if (directory / "xajh_chat_tap.dll").is_file()),
        search[0] / "xajh_chat_tap.dll",
    )
    injector = next(
        (directory / "xajh_inject.exe" for directory in search if (directory / "xajh_inject.exe").is_file()),
        search[0] / "xajh_inject.exe",
    )
    return dll, injector


def ensure_chat_tap(
    pid: int,
    *,
    log: Callable[[str], None] | None = None,
    timeout_s: float = 8.0,
) -> bool:
    """Load the passive tap once; never reinject an existing mapping."""
    log = log or (lambda _message: None)
    reader = ChatTapReader.open(pid)
    if reader is not None:
        try:
            header = reader.header()
            ok = int(header["status"]) == CHAT_TAP_ACTIVE
            if not ok:
                log(
                    f"chat tap resident status={header['status']} "
                    f"error={header['error']!r}"
                )
            return ok
        finally:
            reader.close()

    dll, injector = default_chat_tap_paths()
    if not dll.is_file() or not injector.is_file():
        log(f"chat tap missing dll={dll} injector={injector}")
        return False
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = int(subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
    try:
        result = subprocess.run(
            [str(injector), str(int(pid)), str(dll.resolve())],
            cwd=str(dll.parent),
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_s)),
            creationflags=creationflags,
            check=False,
        )
    except Exception as exc:
        log(f"chat tap inject error: {exc}")
        return False
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    log(f"chat tap inject rc={result.returncode} {output}")
    if result.returncode != 0:
        return False
    reader = wait_for_chat_tap(pid, timeout_s=3.0)
    if reader is None:
        log("chat tap mapping missing after inject")
        return False
    try:
        header = reader.header()
        ok = int(header["status"]) == CHAT_TAP_ACTIVE
        if not ok:
            log(
                f"chat tap inactive status={header['status']} "
                f"error={header['error']!r}"
            )
        return ok
    finally:
        reader.close()

