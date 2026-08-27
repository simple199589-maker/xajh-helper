# -*- coding: utf-8 -*-
"""
Game-side wall-clip (穿墙) toggle — one-byte conditional-jump patch.

Reversed from black shield 2026-08-06 (live diff):
    address 0x0086829D inside the movement/collision boundary check
    (0x868270 function).  OFF = 0x75 (JNZ, original); ON = 0x74 (JE).

Safe: writes a single byte under a toggled page protection; original is
restored when disabling.  No hooks, no remote threads, no breakpoints.

@author by ak
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes

WALL_PATCH_VA = 0x0086829D
WALL_BYTE_OFF = 0x75
WALL_BYTE_ON = 0x74

# Region-membership gate: 0x868190 内 `test al,al; je 0x8682fc` (0x86828B).
# OFF = original `74 6F` (je taken), ON = `90 90` (NOP, always fall through).
REGION_GATE_VA = 0x0086828B
REGION_GATE_OFF = bytes([0x74, 0x6F])
REGION_GATE_ON = bytes([0x90, 0x90])

# Ground-move gate: 0x868190 内 `test al,al; je 0x868425` (0x86834B, 8 bytes).
# OFF = original `84 c0 0f 84 d2 00 00 00`, ON = force al=1 then NOPs.
GROUND_GATE_VA = 0x0086834B
GROUND_GATE_OFF = bytes([0x84, 0xC0, 0x0F, 0x84, 0xD2, 0x00, 0x00, 0x00])
GROUND_GATE_ON = bytes([0xB0, 0x01, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90])

# Ground-position gate: 0x8681F7 calls 0x8675d0 (terrain-height / walkability);
# 0x8681FF `test al,al` + 0x868201 `je 0x86823b` skips the move when it fails.
# OFF = `84 c0 74 38`, ON = `84 c0 90 90` (test kept, je NOPed -> always continue).
GROUND_POS_VA = 0x008681FF
GROUND_POS_OFF = bytes([0x84, 0xC0, 0x74, 0x38])
GROUND_POS_ON = bytes([0x84, 0xC0, 0x90, 0x90])

# IsPosInPassMap entry: 0x867eb0 returns al = "target cell walkable".  It is the
# Lua/AutoMove pathfinding gate ONLY (calls 0x85DFB0 at 0x867EE2); patching it
# does NOT touch the other 23 movemap callers (monster AI / loot).  OFF = original
# prologue `83 EC 08 56`, ON = `mov al,1; ret; nop` -> pathfinding always passable.
PASSMAP_GATE_VA = 0x00867EB0
PASSMAP_GATE_OFF = bytes([0x83, 0xEC, 0x08, 0x56])
PASSMAP_GATE_ON = bytes([0xB0, 0x01, 0xC3, 0x90])

PAGE_EXECUTE_READWRITE = 0x40
PROCESS_ALL = 0x001F0FFF

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = wintypes.HANDLE
k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.ReadProcessMemory.restype = wintypes.BOOL
k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCVOID,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.restype = wintypes.BOOL
k32.VirtualProtectEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
k32.VirtualProtectEx.restype = wintypes.BOOL
k32.FlushInstructionCache.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, ctypes.c_size_t]
k32.FlushInstructionCache.restype = wintypes.BOOL


def _open(pid: int) -> int:
    h = int(k32.OpenProcess(PROCESS_ALL, False, int(pid)) or 0)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def read_wall_byte(pid: int) -> int:
    """Return the current byte at 0x0086829D (or -1 on read failure). @author by ak"""
    h = _open(pid)
    try:
        b = ctypes.create_string_buffer(1)
        n = ctypes.c_size_t()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(WALL_PATCH_VA), b, 1, ctypes.byref(n)):
            return -1
        return b.raw[0]
    finally:
        k32.CloseHandle(h)


def wall_clip_state(pid: int) -> bool | None:
    """True=穿墙开, False=关, None=无法读取/未知. @author by ak"""
    v = read_wall_byte(pid)
    if v == WALL_BYTE_ON:
        return True
    if v == WALL_BYTE_OFF:
        return False
    return None


def _patch_bytes(pid: int, va: int, want: bytes, other: bytes, name: str) -> None:
    """Write `want` bytes at `va` under toggled protection.

    Accepts either `other` or `want` as the current state so toggles are
    idempotent and reversible from both states.
    @author by ak
    """
    allowed = {other, want}
    h = _open(pid)
    try:
        cur = ctypes.create_string_buffer(max(len(want), len(other)))
        n = ctypes.c_size_t()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(va), cur, max(len(want), len(other)),
                                     ctypes.byref(n)):
            raise ctypes.WinError(ctypes.get_last_error())
        got = cur.raw[: max(len(want), len(other))]
        if got == want:
            return
        if got not in allowed:
            raise RuntimeError(
                f"0x{va:08X} unexpected bytes {got.hex(' ')} "
                f"(expected {other.hex(' ')} / {want.hex(' ')}) for {name}"
            )
        old_prot = wintypes.DWORD()
        if not k32.VirtualProtectEx(h, ctypes.c_void_p(va), len(want),
                                    PAGE_EXECUTE_READWRITE, ctypes.byref(old_prot)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            buf = ctypes.create_string_buffer(want)
            done = ctypes.c_size_t()
            if not k32.WriteProcessMemory(h, ctypes.c_void_p(va), buf, len(want),
                                          ctypes.byref(done)) or done.value != len(want):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            k32.VirtualProtectEx(h, ctypes.c_void_p(va), len(want),
                                 int(old_prot.value or 0), ctypes.byref(wintypes.DWORD()))
        k32.FlushInstructionCache(h, ctypes.c_void_p(va), len(want))
    finally:
        k32.CloseHandle(h)


def read_gate_bytes(pid: int, va: int, size: int) -> bytes:
    """Return current bytes at `va` (or b'' on read failure). @author by ak"""
    h = _open(pid)
    try:
        b = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(va), b, size, ctypes.byref(n)):
            return b""
        return b.raw[: max(0, n.value)]
    finally:
        k32.CloseHandle(h)


def region_gate_state(pid: int) -> bool | None:
    """True=区域闸门已跳过, False=原始, None=无法读取/未知. @author by ak"""
    got = read_gate_bytes(pid, REGION_GATE_VA, len(REGION_GATE_OFF))
    if got == REGION_GATE_ON:
        return True
    if got == REGION_GATE_OFF:
        return False
    return None


def set_region_gate(pid: int, enabled: bool) -> None:
    """Toggle the region-membership gate in the movement boundary check.

    Enabled -> NOP the `je` so positions outside all map regions are accepted.
    @author by ak
    """
    _patch_bytes(pid, REGION_GATE_VA,
                 REGION_GATE_ON if enabled else REGION_GATE_OFF,
                 REGION_GATE_OFF if enabled else REGION_GATE_ON, "region gate")


def ground_gate_state(pid: int) -> bool | None:
    """True=地面判定恒成功, False=原始, None=无法读取/未知. @author by ak"""
    got = read_gate_bytes(pid, GROUND_GATE_VA, len(GROUND_GATE_OFF))
    if got == GROUND_GATE_ON:
        return True
    if got == GROUND_GATE_OFF:
        return False
    return None


def set_ground_gate(pid: int, enabled: bool) -> None:
    """Toggle the ground-move gate in the movement boundary check.

    Enabled -> force al=1 (ground height check always passes).
    @author by ak
    """
    _patch_bytes(pid, GROUND_GATE_VA,
                 GROUND_GATE_ON if enabled else GROUND_GATE_OFF,
                 GROUND_GATE_OFF if enabled else GROUND_GATE_ON, "ground gate")


def ground_pos_gate_state(pid: int) -> bool | None:
    """True=地面位置失败也继续, False=原始, None=未知. @author by ak"""
    got = read_gate_bytes(pid, GROUND_POS_VA, len(GROUND_POS_OFF))
    if got == GROUND_POS_ON:
        return True
    if got == GROUND_POS_OFF:
        return False
    return None


def set_ground_pos_gate(pid: int, enabled: bool) -> None:
    """Toggle the ground-position gate in the movement boundary check.

    Enabled -> NOP the `je` after the 0x8675d0 terrain-height check so the
    move continues even when the target cell is judged non-walkable.
    @author by ak
    """
    _patch_bytes(pid, GROUND_POS_VA,
                 GROUND_POS_ON if enabled else GROUND_POS_OFF,
                 GROUND_POS_OFF if enabled else GROUND_POS_ON, "ground pos gate")


def passmap_gate_state(pid: int) -> bool | None:
    """True=寻路恒可走, False=原始, None=未知. @author by ak"""
    got = read_gate_bytes(pid, PASSMAP_GATE_VA, len(PASSMAP_GATE_OFF))
    if got == PASSMAP_GATE_ON:
        return True
    if got == PASSMAP_GATE_OFF:
        return False
    return None


def set_passmap_gate(pid: int, enabled: bool) -> None:
    """Toggle the IsPosInPassMap pathfinding gate.

    Enabled -> pathfinding always treats the target cell as walkable (does NOT
    touch the other movemap callers, so monster AI is unaffected).
    @author by ak
    """
    _patch_bytes(pid, PASSMAP_GATE_VA,
                 PASSMAP_GATE_ON if enabled else PASSMAP_GATE_OFF,
                 PASSMAP_GATE_OFF if enabled else PASSMAP_GATE_ON, "passmap gate")


def set_wall_clip(pid: int, enabled: bool, *, expect_original: bool = True) -> None:
    """Toggle wall clip on/off by patching the single condition byte.

    Enabled -> 0x74 (JE), disabled -> 0x75 (JNZ, original).
    @author by ak
    """
    want = WALL_BYTE_ON if enabled else WALL_BYTE_OFF
    h = _open(pid)
    try:
        cur = read_wall_byte(pid)
        if cur == want:
            return
        if expect_original and cur not in (WALL_BYTE_OFF, WALL_BYTE_ON):
            raise RuntimeError(
                f"0x{WALL_PATCH_VA:08X} unexpected byte 0x{cur:02X} "
                f"(expected 0x{WALL_BYTE_OFF:02X}/0x{WALL_BYTE_ON:02X})"
            )
        old_prot = wintypes.DWORD()
        if not k32.VirtualProtectEx(h, ctypes.c_void_p(WALL_PATCH_VA), 1,
                                    PAGE_EXECUTE_READWRITE, ctypes.byref(old_prot)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            buf = ctypes.create_string_buffer(bytes([want]))
            done = ctypes.c_size_t()
            if not k32.WriteProcessMemory(h, ctypes.c_void_p(WALL_PATCH_VA), buf, 1,
                                          ctypes.byref(done)) or done.value != 1:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            k32.VirtualProtectEx(h, ctypes.c_void_p(WALL_PATCH_VA), 1,
                                 int(old_prot.value or 0), ctypes.byref(wintypes.DWORD()))
        k32.FlushInstructionCache(h, ctypes.c_void_p(WALL_PATCH_VA), 1)
    finally:
        k32.CloseHandle(h)
