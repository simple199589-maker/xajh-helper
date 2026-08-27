# -*- coding: utf-8 -*-
"""Generic raw C2S packet sender for the game (CD1740 system send).

Sends one arbitrary byte payload through the game's system-instruction send
entry ``0x00CD1740`` from a serialized remote thread.  This is the same
proven capability used by the 丸子 packet runner (see ``wanzi_packet``), but
generalized to an arbitrary payload length so it can also drive dungeon
teleport dialogs (对话封包 → 上层/深处 封包).

Send chain (proven 2026-08-06):
    this   = *(*(0x015282D8) + 0x2C)
    target = 0x00CD1740
    CD1740(this, packet_ptr, len) runs safely from any thread.

@author by ak
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from typing import Callable

from app.core.remote_runtime import (
    MEM_COMMIT,
    MEM_RELEASE,
    MEM_RESERVE,
    PAGE_EXECUTE_READWRITE,
    ensure_pid_remote_callable,
    kernel32,
    note_remote_os_error,
    open_process,
    pid_call_mutex,
    read_process,
    wait_remote_thread_safely,
    write_process,
)

LogFn = Callable[[str], None]

ROOT_GLOBAL = 0x015282D8
THIS_OFFSET = 0x2C
SEND_TARGET = 0x00CD1740
ORIGINAL_TARGET_PREFIX = bytes.fromhex("64A100000000")
MAX_PACKET = 128

kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.ResumeThread.restype = wintypes.DWORD
kernel32.FlushInstructionCache.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
kernel32.FlushInstructionCache.restype = wintypes.BOOL
kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeThread.restype = wintypes.BOOL


def _p32(value: int) -> bytes:
    return struct.pack("<I", int(value) & 0xFFFFFFFF)


def _remote_stub(packet_address: int, length: int) -> bytes:
    """Remote thread stub: push len; push packet; this=root+0x2C; call CD1740.

    Stack after the two pushes reads [packet, len], which is exactly
    ``CD1740(this=ecx, packet, len)``. @author by ak
    """
    return (
        b"\x68" + _p32(int(length))           # push len
        + b"\x68" + _p32(int(packet_address))  # push packet
        + b"\x8B\x15" + _p32(ROOT_GLOBAL)     # mov edx,[root global]
        + b"\x8B\x4A\x2C"                     # mov ecx,[edx+0x2c]
        + b"\xB8" + _p32(SEND_TARGET)         # mov eax, system command fn
        + b"\xFF\xD0\xC3"                     # call eax; ret
    )


def send_raw_packet(
    pid: int,
    payload: bytes,
    *,
    timeout_ms: int = 3000,
    log: LogFn | None = None,
) -> int:
    """Send one raw C2S packet via game CD1740(this=root+0x2C, packet, len).

    Returns CD1740's EAX (1 = accepted).  Raises on remote-call failure;
    the remote page is leaked on timeout (the thread may still be running).
    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(pid)
    data = bytes(payload)
    if not data or len(data) > MAX_PACKET:
        raise ValueError(f"payload length {len(data)} out of range 1..{MAX_PACKET}")
    remote = 0
    thread = 0
    finished = False
    handle = 0
    try:
        ensure_pid_remote_callable(pid)
        with pid_call_mutex(pid, timeout_ms=1000, namespace="Call"):
            ensure_pid_remote_callable(pid)
            handle = open_process(pid)
            prefix = read_process(handle, SEND_TARGET, len(ORIGINAL_TARGET_PREFIX))
            if prefix != ORIGINAL_TARGET_PREFIX:
                raise RuntimeError(
                    f"0x{SEND_TARGET:08X} is hooked ({prefix.hex().upper()}); "
                    "refusing packet send"
                )
            remote = int(
                kernel32.VirtualAllocEx(
                    wintypes.HANDLE(handle),
                    None,
                    0x1000,
                    MEM_COMMIT | MEM_RESERVE,
                    PAGE_EXECUTE_READWRITE,
                )
                or 0
            )
            if not remote:
                raise OSError(f"VirtualAllocEx failed err={ctypes.get_last_error()}")
            write_process(handle, remote, data)
            stub = remote + 0x100
            code = _remote_stub(remote, len(data))
            write_process(handle, stub, code)
            kernel32.FlushInstructionCache(
                wintypes.HANDLE(handle), ctypes.c_void_p(stub), len(code)
            )
            tid = wintypes.DWORD()
            thread_h = kernel32.CreateRemoteThread(
                wintypes.HANDLE(handle),
                None,
                0,
                ctypes.c_void_p(stub),
                None,
                0x4,  # CREATE_SUSPENDED, matching the recovered chain
                ctypes.byref(tid),
            )
            if not thread_h:
                raise OSError(f"CreateRemoteThread failed err={ctypes.get_last_error()}")
            thread = int(thread_h)
            if kernel32.ResumeThread(wintypes.HANDLE(thread)) == 0xFFFFFFFF:
                raise OSError(f"ResumeThread failed err={ctypes.get_last_error()}")
            finished = wait_remote_thread_safely(
                thread,
                int(timeout_ms),
                operation="game raw packet",
                pid=pid,
            )
            if not finished:
                raise TimeoutError("game packet remote thread timed out")
            result = wintypes.DWORD()
            if not kernel32.GetExitCodeThread(wintypes.HANDLE(thread), ctypes.byref(result)):
                raise OSError(f"GetExitCodeThread failed err={ctypes.get_last_error()}")
            log(f"game_send: pid={pid} len={len(data)} ret={int(result.value)}")
            return int(result.value)
    except Exception as exc:
        note_remote_os_error(pid, exc)
        raise
    finally:
        if thread:
            kernel32.CloseHandle(wintypes.HANDLE(thread))
        # Timeout deliberately leaks the page: the remote code may still execute.
        if remote and finished and handle:
            kernel32.VirtualFreeEx(
                wintypes.HANDLE(handle), ctypes.c_void_p(remote), 0, MEM_RELEASE
            )
        if handle:
            kernel32.CloseHandle(wintypes.HANDLE(handle))


__all__ = [
    "send_raw_packet",
    "ROOT_GLOBAL",
    "THIS_OFFSET",
    "SEND_TARGET",
    "ORIGINAL_TARGET_PREFIX",
    "MAX_PACKET",
]
