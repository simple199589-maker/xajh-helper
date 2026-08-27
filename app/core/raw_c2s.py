# -*- coding: utf-8 -*-
"""
Raw c2s packet send via the game net-root sender 0xCD1740.

Promoted to a shared utility from the 丸子 direct-send reference
(.issues/deliver/第二系统指令交换_20260805 / app/core/wanzi_packet.py):

    thiscall 0xCD1740:  ecx = net/session root, arg1 = packet ptr, arg2 = packet len
    net root = *[*0x15282D8 + 0x2C]  (0x15282D8 is a preferred-base note VA)

The payload is the full c2s packet bytes (same shape the wanzi runner sends).
The send is fire-and-forget: it runs through CreateRemoteThread without the
scene-stability CRT gate, so it works during map loads / scene settling and does
not wait for any follow-state change.

@author by ak
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from typing import Callable

from app.core.plg_exports import DEFAULT_IMAGE_BASE
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

NOTE_VA_NET_ROOT_GLOBAL = 0x015282D8  # -> net root global
NET_ROOT_THIS_OFF = 0x2C  # [*global + 0x2C] = net/session this
NOTE_VA_RAW_SEND = 0x00CD1740  # thiscall net, (packet_ptr, packet_len) -> c2s
ORIGINAL_SEND_PREFIX = bytes.fromhex("64A100000000")  # mov fs:[0], eax prologue


def _note_to_live(session, note_va: int) -> int:
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        raise RuntimeError("raw_c2s: session has no module_base; attach first")
    return int(base + (int(note_va) - int(DEFAULT_IMAGE_BASE))) & 0xFFFFFFFF


def _p32(value: int) -> bytes:
    return struct.pack("<I", int(value) & 0xFFFFFFFF)


def _read_u32(handle: int, address: int) -> int:
    raw = read_process(handle, int(address), 4)
    return struct.unpack("<I", raw)[0] if len(raw) >= 4 else 0


def _remote_stub(root_va: int, send_va: int, payload_addr: int, size: int) -> bytes:
    """thiscall stub: ecx=[[root_va]+0x2C], args=(payload, size) -> call send."""
    size = int(size)
    code = bytearray()
    if size <= 0x7F:
        code += b"\x6A" + bytes([size])
    else:
        code += b"\x68" + _p32(size)
    code += b"\x68" + _p32(payload_addr)
    code += b"\x8B\x15" + _p32(root_va)   # mov edx, [root global]
    code += b"\x8B\x4A\x2C"              # mov ecx, [edx+0x2C]
    code += b"\xB8" + _p32(send_va)       # mov eax, send fn
    code += b"\xFF\xD0\xC3"              # call eax; ret
    return bytes(code)


def read_net_root(session) -> int:
    """Read the game net/session root used as the this pointer for 0xCD1740."""
    handle = 0
    try:
        handle = open_process(int(getattr(session, "pid", 0) or 0))
        root_va = _note_to_live(session, NOTE_VA_NET_ROOT_GLOBAL)
        root = _read_u32(handle, root_va)
        if not root:
            raise RuntimeError(f"raw_c2s: net root global null at 0x{root_va:X}")
        return _read_u32(handle, root + NET_ROOT_THIS_OFF)
    finally:
        if handle:
            kernel32.CloseHandle(wintypes.HANDLE(handle))


def send_raw_c2s_packet(
    session,
    payload: bytes,
    *,
    log: LogFn | None = None,
    timeout_ms: int = 1500,
) -> int:
    """
    Fire-and-forget raw c2s send through the game sender (no scene-stability gate).

    Returns the remote thread exit code (1 = client accepted/queued the packet).
    On a remote-thread timeout the page is intentionally leaked and 1 is returned:
    the thread was created and is still executing, so the packet is considered
    dispatched.  Hard errors (open process / alloc) still raise.

    @author by ak
    """
    log = log or (lambda _m: None)
    payload = bytes(payload or b"")
    if not payload:
        raise ValueError("raw_c2s: empty payload")
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        raise RuntimeError("raw_c2s: no pid")
    root_va = _note_to_live(session, NOTE_VA_NET_ROOT_GLOBAL)
    send_va = _note_to_live(session, NOTE_VA_RAW_SEND)

    remote = 0
    thread = 0
    finished = False
    handle = 0
    try:
        ensure_pid_remote_callable(pid)
        with pid_call_mutex(pid, timeout_ms=1000, namespace="Call"):
            ensure_pid_remote_callable(pid)
            handle = open_process(pid)
            prefix = read_process(handle, send_va, len(ORIGINAL_SEND_PREFIX))
            if prefix != ORIGINAL_SEND_PREFIX:
                recovered = False
                if prefix and prefix[0] == 0xE9:
                    try:
                        from app.core.packet_intercept import restore_stale_hook

                        recovered = restore_stale_hook(
                            pid,
                            send_va,
                            log=log,
                        )
                    except Exception as exc:
                        log(f"raw_c2s: stale hook recovery unavailable: {exc}")
                    if recovered:
                        prefix = read_process(handle, send_va, len(ORIGINAL_SEND_PREFIX))
                if prefix != ORIGINAL_SEND_PREFIX:
                    raise RuntimeError(
                        f"raw_c2s: 0x{send_va:08X} is hooked "
                        f"({prefix.hex().upper()}); refusing send"
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
            write_process(handle, remote, payload)
            stub = remote + 0x100
            code = _remote_stub(root_va, send_va, remote, len(payload))
            write_process(handle, stub, code)
            kernel32.FlushInstructionCache(
                wintypes.HANDLE(handle), ctypes.c_void_p(stub), len(code)
            )
            log(
                f"raw_c2s: send {len(payload)}B {payload.hex().upper()} "
                f"via 0x{send_va:X} (fire-and-forget)"
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
                operation="raw c2s packet",
                pid=pid,
            )
            if not finished:
                log(f"raw_c2s: remote thread timeout; page leaked, treating as sent")
                return 1
            result = wintypes.DWORD()
            if not kernel32.GetExitCodeThread(wintypes.HANDLE(thread), ctypes.byref(result)):
                raise OSError(f"GetExitCodeThread failed err={ctypes.get_last_error()}")
            return int(result.value)
    except Exception as exc:
        note_remote_os_error(pid, exc)
        raise
    finally:
        if thread:
            kernel32.CloseHandle(wintypes.HANDLE(thread))
        if remote and finished and handle:
            kernel32.VirtualFreeEx(
                wintypes.HANDLE(handle), ctypes.c_void_p(remote), 0, MEM_RELEASE
            )
        if handle:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
