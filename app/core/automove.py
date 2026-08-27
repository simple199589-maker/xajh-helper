# -*- coding: utf-8 -*-
"""
Trigger client AutoMove via xajh.exe plg export HostMoveToScenePosition.

Uses remote x86 cdecl call into the game process (no SendInput, no SetPos).

Signature (mangled export):
  int __cdecl plg::HostMoveToScenePosition(int mode_or_scene, float x, float y, float z);

Also exposes GetCurrentScenePosition for scene id + pos cross-check.

@author by ak
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from app.core.plg_exports import (
    EXPORT_GET_CURRENT_SCENE_POSITION,
    EXPORT_HOST_MOVE_TO_SCENE_POSITION,
    find_xajh_exe,
    resolve_export_rva,
)
from app.core.remote_runtime import (
    _open_process as _runtime_open_process,
    _rpm as _runtime_rpm,
    _wpm as _runtime_wpm,
    ensure_pid_remote_callable,
    ensure_pid_scene_stable,
    pid_call_mutex,
    remote_call_cdecl_x86 as _runtime_call_cdecl_x86,
    remote_call_cdecl_x86_ret64 as _runtime_call_cdecl_x86_ret64,
    remote_call_thiscall_x86 as _runtime_call_thiscall_x86,
    remote_read_bytes as _runtime_read_bytes,
    remote_write_bytes as _runtime_write_bytes,
    wait_remote_thread_safely,
)

LogFn = Callable[[str], None]

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_ALL_ACCESS = 0x1F0FFF
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_EXECUTE_READWRITE = 0x40
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.VirtualAllocEx.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    ctypes.c_size_t,
    wintypes.DWORD,
    wintypes.DWORD,
]
kernel32.VirtualAllocEx.restype = wintypes.LPVOID
kernel32.VirtualFreeEx.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    ctypes.c_size_t,
    wintypes.DWORD,
]
kernel32.VirtualFreeEx.restype = wintypes.BOOL
kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.LPCVOID,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.WriteProcessMemory.restype = wintypes.BOOL
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCVOID,
    wintypes.LPVOID,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.CreateRemoteThread.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    ctypes.c_size_t,
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPDWORD,
]
kernel32.CreateRemoteThread.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
kernel32.GetExitCodeThread.restype = wintypes.BOOL


@dataclass
class PathTarget:
    """AutoMove destination in scene coordinates. @author by ak"""

    x: float
    y: float
    z: float
    mode: int = 0
    map_hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AutomoveResult:
    """Outcome of a remote HostMoveToScenePosition call. @author by ak"""

    ok: bool
    method: str
    target: dict
    func_va: int = 0
    ret: int | None = None
    scene_id: int | None = None
    scene_pos: tuple[float, float, float] | None = None
    error: str | None = None
    note: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.scene_pos is not None:
            d["scene_pos"] = list(self.scene_pos)
        return d


def _open_process(pid: int) -> int:
    access = (
        PROCESS_CREATE_THREAD
        | PROCESS_QUERY_INFORMATION
        | PROCESS_VM_OPERATION
        | PROCESS_VM_WRITE
        | PROCESS_VM_READ
    )
    h = kernel32.OpenProcess(access, False, int(pid))
    if not h:
        raise OSError(f"OpenProcess failed err={ctypes.get_last_error()}")
    return int(h)


def _wpm(h: int, addr: int, data: bytes) -> None:
    n = ctypes.c_size_t(0)
    buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
    ok = kernel32.WriteProcessMemory(
        wintypes.HANDLE(h),
        ctypes.c_void_p(addr),
        buf,
        len(data),
        ctypes.byref(n),
    )
    if not ok or n.value != len(data):
        raise OSError(f"WriteProcessMemory failed err={ctypes.get_last_error()} n={n.value}")


def _rpm(h: int, addr: int, size: int) -> bytes:
    buf = (ctypes.c_char * size)()
    n = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(
        wintypes.HANDLE(h),
        ctypes.c_void_p(addr),
        buf,
        size,
        ctypes.byref(n),
    )
    if not ok:
        raise OSError(f"ReadProcessMemory failed err={ctypes.get_last_error()}")
    return bytes(buf[: n.value])


def remote_call_cdecl_x86(
    pid: int,
    func_va: int,
    args: list[int | float],
    *,
    timeout_ms: int = 5000,
) -> int:
    """
    Call a cdecl x86 function in remote process; return EAX.

    Args are pushed right-to-left. int as 32-bit, float as IEEE754 bits.
    @author by ak
    """
    if not func_va:
        raise ValueError("func_va is 0")
    h = _open_process(pid)
    remote = 0
    thr = 0
    try:
        # layout: [0]=ret slot(4), [4..]=arg slots, then stub code
        arg_bytes = b""
        for a in args:
            if isinstance(a, float):
                arg_bytes += struct.pack("<f", float(a))
            else:
                # use unsigned so high userland ptrs (>0x7FFFFFFF) pack cleanly
                arg_bytes += struct.pack("<I", int(a) & 0xFFFFFFFF)
        header = 4  # ret slot
        stub_off = header + len(arg_bytes)
        # stub:
        #   push argN ... push arg0  (right-to-left => push last first)
        #   mov eax, func_va
        #   call eax
        #   mov [ret_slot], eax
        #   add esp, 4*nargs
        #   ret
        code = bytearray()
        n = len(args)
        # absolute addresses of arg slots
        # we fix them after alloc
        # use relative via placeholder then patch — allocate first
        total = stub_off + 64
        remote = int(
            kernel32.VirtualAllocEx(
                wintypes.HANDLE(h),
                None,
                total,
                MEM_COMMIT | MEM_RESERVE,
                PAGE_EXECUTE_READWRITE,
            )
            or 0
        )
        if not remote:
            raise OSError(f"VirtualAllocEx failed err={ctypes.get_last_error()}")

        ret_addr = remote
        arg_base = remote + header
        code_addr = remote + stub_off

        # write args
        _wpm(h, arg_base, arg_bytes)
        _wpm(h, ret_addr, b"\x00\x00\x00\x00")

        # build stub with absolute push from memory (push dword [imm32] not used)
        # push dword ptr [arg_slot] via: push imm32 where imm is value we already know
        # simpler: push immediate float bits / ints
        for a in reversed(args):
            if isinstance(a, float):
                bits = struct.unpack("<I", struct.pack("<f", float(a)))[0]
            else:
                bits = int(a) & 0xFFFFFFFF
            code += b"\x68" + struct.pack("<I", bits)  # push imm32
        # mov eax, func_va
        code += b"\xb8" + struct.pack("<I", int(func_va) & 0xFFFFFFFF)
        # call eax
        code += b"\xff\xd0"
        # mov [ret_addr], eax  -> mov dword ptr [imm32], eax  (A3 only for AL/AX/EAX to moffs — use)
        # A3 is mov moffs32, eax — absolute address
        code += b"\xa3" + struct.pack("<I", int(ret_addr) & 0xFFFFFFFF)
        # add esp, 4*n
        if n:
            code += b"\x83\xc4" + bytes([ (4 * n) & 0xFF ])
        # ret
        code += b"\xc3"

        _wpm(h, code_addr, bytes(code))

        tid = wintypes.DWORD(0)
        thr = int(
            kernel32.CreateRemoteThread(
                wintypes.HANDLE(h),
                None,
                0,
                ctypes.c_void_p(code_addr),
                None,
                0,
                ctypes.byref(tid),
            )
            or 0
        )
        if not thr:
            raise OSError(f"CreateRemoteThread failed err={ctypes.get_last_error()}")
        wr = kernel32.WaitForSingleObject(wintypes.HANDLE(thr), int(timeout_ms))
        if wr != WAIT_OBJECT_0:
            raise OSError(f"WaitForSingleObject timeout/status={wr}")
        raw = _rpm(h, ret_addr, 4)
        return int(struct.unpack("<i", raw)[0])
    finally:
        if thr:
            kernel32.CloseHandle(wintypes.HANDLE(thr))
        if remote:
            kernel32.VirtualFreeEx(wintypes.HANDLE(h), ctypes.c_void_p(remote), 0, MEM_RELEASE)
        kernel32.CloseHandle(wintypes.HANDLE(h))


def remote_read_bytes(pid: int, addr: int, size: int) -> bytes:
    """
    Read ``size`` bytes from remote process at ``addr``.

    @author by ak
    """
    if not addr or size <= 0:
        return b""
    h = _open_process(int(pid))
    try:
        return _rpm(h, int(addr) & 0xFFFFFFFF, int(size))
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h))


def remote_write_bytes(pid: int, addr: int, data: bytes) -> int:
    """
    Write bytes into remote process at ``addr``; return written length.

    Lab/debug use only. Caller must ensure address is valid.
    @author by ak
    """
    if not addr or not data:
        return 0
    h = _open_process(int(pid))
    try:
        _wpm(h, int(addr) & 0xFFFFFFFF, bytes(data))
        return len(data)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h))


def remote_call_thiscall_x86(
    pid: int,
    func_va: int,
    this: int,
    args: list[int | float] | None = None,
    *,
    timeout_ms: int = 5000,
) -> int:
    """
    Call a thiscall x86 function in remote process (ecx=this); return EAX.

    Stack args pushed right-to-left after setting ecx.
    @author by ak
    """
    if not func_va:
        raise ValueError("func_va is 0")
    args = list(args or [])
    h = _open_process(pid)
    remote = 0
    thr = 0
    try:
        header = 4  # ret slot
        code = bytearray()
        for a in reversed(args):
            if isinstance(a, float):
                bits = struct.unpack("<I", struct.pack("<f", float(a)))[0]
            else:
                bits = int(a) & 0xFFFFFFFF
            code += b"\x68" + struct.pack("<I", bits)  # push imm32
        # mov ecx, this
        code += b"\xb9" + struct.pack("<I", int(this) & 0xFFFFFFFF)
        # mov eax, func_va
        code += b"\xb8" + struct.pack("<I", int(func_va) & 0xFFFFFFFF)
        # call eax
        code += b"\xff\xd0"
        # store eax — patch after alloc
        stub_body = bytes(code)
        total = header + len(stub_body) + 24
        remote = int(
            kernel32.VirtualAllocEx(
                wintypes.HANDLE(h),
                None,
                total,
                MEM_COMMIT | MEM_RESERVE,
                PAGE_EXECUTE_READWRITE,
            )
            or 0
        )
        if not remote:
            raise OSError(f"VirtualAllocEx failed err={ctypes.get_last_error()}")
        ret_addr = remote
        code_addr = remote + header
        code = bytearray(stub_body)
        code += b"\xa3" + struct.pack("<I", int(ret_addr) & 0xFFFFFFFF)
        # thiscall: callee cleans stack for args? MSVC thiscall: callee pops args.
        # We still only ret — do not add esp for thiscall.
        code += b"\xc3"
        _wpm(h, ret_addr, b"\x00\x00\x00\x00")
        _wpm(h, code_addr, bytes(code))
        tid = wintypes.DWORD(0)
        thr = int(
            kernel32.CreateRemoteThread(
                wintypes.HANDLE(h),
                None,
                0,
                ctypes.c_void_p(code_addr),
                None,
                0,
                ctypes.byref(tid),
            )
            or 0
        )
        if not thr:
            raise OSError(f"CreateRemoteThread failed err={ctypes.get_last_error()}")
        wr = kernel32.WaitForSingleObject(wintypes.HANDLE(thr), int(timeout_ms))
        if wr != WAIT_OBJECT_0:
            raise OSError(f"WaitForSingleObject timeout/status={wr}")
        raw = _rpm(h, ret_addr, 4)
        return int(struct.unpack("<i", raw)[0])
    finally:
        if thr:
            kernel32.CloseHandle(wintypes.HANDLE(thr))
        if remote:
            kernel32.VirtualFreeEx(
                wintypes.HANDLE(h), ctypes.c_void_p(remote), 0, MEM_RELEASE
            )
        kernel32.CloseHandle(wintypes.HANDLE(h))


def remote_call_cdecl_x86_ret64(
    pid: int,
    func_va: int,
    args: list[int | float],
    *,
    timeout_ms: int = 5000,
) -> int:
    """
    Call a cdecl x86 function in remote process; return EDX:EAX as unsigned 64-bit.

    Used for plg exports that return __int64 / pointer-wide pairs
    (e.g. GetObjecti64States).
    @author by ak
    """
    if not func_va:
        raise ValueError("func_va is 0")
    h = _open_process(pid)
    remote = 0
    thr = 0
    try:
        # layout: [0]=eax(4), [4]=edx(4), then stub
        header = 8
        code = bytearray()
        for a in reversed(args):
            if isinstance(a, float):
                bits = struct.unpack("<I", struct.pack("<f", float(a)))[0]
            else:
                bits = int(a) & 0xFFFFFFFF
            code += b"\x68" + struct.pack("<I", bits)  # push imm32
        code += b"\xb8" + struct.pack("<I", int(func_va) & 0xFFFFFFFF)  # mov eax, func
        code += b"\xff\xd0"  # call eax
        # store EAX then EDX into ret slots (patched after alloc)
        # placeholders for A3/absolute stores — build after alloc
        stub_body = bytes(code)
        total = header + len(stub_body) + 32
        remote = int(
            kernel32.VirtualAllocEx(
                wintypes.HANDLE(h),
                None,
                total,
                MEM_COMMIT | MEM_RESERVE,
                PAGE_EXECUTE_READWRITE,
            )
            or 0
        )
        if not remote:
            raise OSError(f"VirtualAllocEx failed err={ctypes.get_last_error()}")
        eax_slot = remote
        edx_slot = remote + 4
        code_addr = remote + header
        code = bytearray(stub_body)
        # mov [eax_slot], eax
        code += b"\xa3" + struct.pack("<I", int(eax_slot) & 0xFFFFFFFF)
        # mov [edx_slot], edx  -> 89 15 imm32
        code += b"\x89\x15" + struct.pack("<I", int(edx_slot) & 0xFFFFFFFF)
        n = len(args)
        if n:
            code += b"\x83\xc4" + bytes([(4 * n) & 0xFF])
        code += b"\xc3"
        _wpm(h, eax_slot, b"\x00" * 8)
        _wpm(h, code_addr, bytes(code))
        tid = wintypes.DWORD(0)
        thr = int(
            kernel32.CreateRemoteThread(
                wintypes.HANDLE(h),
                None,
                0,
                ctypes.c_void_p(code_addr),
                None,
                0,
                ctypes.byref(tid),
            )
            or 0
        )
        if not thr:
            raise OSError(f"CreateRemoteThread failed err={ctypes.get_last_error()}")
        wr = kernel32.WaitForSingleObject(wintypes.HANDLE(thr), int(timeout_ms))
        if wr != WAIT_OBJECT_0:
            raise OSError(f"WaitForSingleObject timeout/status={wr}")
        raw = _rpm(h, eax_slot, 8)
        lo, hi = struct.unpack("<II", raw)
        return (int(hi) << 32) | int(lo)
    finally:
        if thr:
            kernel32.CloseHandle(wintypes.HANDLE(thr))
        if remote:
            kernel32.VirtualFreeEx(
                wintypes.HANDLE(h), ctypes.c_void_p(remote), 0, MEM_RELEASE
            )
        kernel32.CloseHandle(wintypes.HANDLE(h))


# Compatibility facade: callers importing the historical automove helpers now
# use the single serialized runtime implementation.
_open_process = _runtime_open_process
_rpm = _runtime_rpm
_wpm = _runtime_wpm
remote_call_cdecl_x86 = _runtime_call_cdecl_x86
remote_call_thiscall_x86 = _runtime_call_thiscall_x86
remote_call_cdecl_x86_ret64 = _runtime_call_cdecl_x86_ret64
remote_read_bytes = _runtime_read_bytes
remote_write_bytes = _runtime_write_bytes


def _resolve_paths(session) -> tuple[int, Path]:
    """Return (module_base, pe_path). @author by ak"""
    base = getattr(session, "module_base", None)
    if not base:
        raise RuntimeError("session has no module_base; attach break0 first")
    pe = find_xajh_exe(getattr(session, "exe_path", None))
    if pe is None:
        raise RuntimeError("cannot locate xajh.exe for export parse")
    return int(base), pe


def host_move_to(
    session,
    target: PathTarget,
    *,
    log: LogFn | None = None,
    fallback_mode0: bool = True,
) -> AutomoveResult:
    """
    Call plg::HostMoveToScenePosition(mode, x, y, z) in game process.

    mode meaning is scene/context dependent. Dev workbench path panel uses the
    value from「读场景」(scene_id) or an explicit entry; densest chest plan uses
    scene_id when move_mode is omitted.

    When fallback_mode0 is True and a non-zero mode returns 0, retry mode=0 once
    (legacy Fuzhou note). Callers that intentionally pass scene_id should set
    fallback_mode0=False if they do not want that override.
    @author by ak
    """
    log = log or (lambda _m: None)
    tdict = target.to_dict()
    mode = int(target.mode)
    note_extra = ""
    try:
        base, pe = _resolve_paths(session)
        rva = resolve_export_rva(pe, EXPORT_HOST_MOVE_TO_SCENE_POSITION)
        if rva is None:
            return AutomoveResult(
                ok=False,
                method="HostMoveToScenePosition",
                target=tdict,
                error="export not found in PE",
            )
        va = base + int(rva)
        pid = int(session.pid)
        log(
            f"automove call HostMoveToScenePosition va=0x{va:X} "
            f"mode={mode} xyz=({target.x:.3f},{target.y:.3f},{target.z:.3f}) pid={pid}"
        )
        ret = remote_call_cdecl_x86(
            pid,
            va,
            [int(mode), float(target.x), float(target.y), float(target.z)],
        )
        tried = [mode]
        if fallback_mode0 and mode != 0 and int(ret) == 0:
            log("automove ret=0 with non-zero mode; retry mode=0")
            ret0 = remote_call_cdecl_x86(
                pid,
                va,
                [0, float(target.x), float(target.y), float(target.z)],
            )
            tried.append(0)
            ret = ret0
            mode = 0
            note_extra += f"; fallback mode=0 ret={ret0}"
        note = (
            "remote cdecl call done; confirm character walks in-game"
            + note_extra
            + f"; tried_modes={tried}"
        )
        return AutomoveResult(
            ok=True,
            method="HostMoveToScenePosition",
            target={**tdict, "mode": mode},
            func_va=va,
            ret=ret,
            note=note,
        )
    except Exception as e:
        log(f"automove error: {e}")
        return AutomoveResult(
            ok=False,
            method="HostMoveToScenePosition",
            target=tdict,
            error=str(e),
        )


def read_scene_position(
    session,
    *,
    log: LogFn | None = None,
    timeout_ms: int = 5000,
    grace_ms: int | None = None,
) -> AutomoveResult:
    """
    Call GetCurrentScenePosition(out_scene, out_x, out_y, out_z).

    Allocates 16-byte out block remotely, passes four pointers.

    Serialized via pid_call_mutex (same lock as remote_call_x86). On hang we
    intentionally leak the remote page instead of VirtualFreeEx while CRT may
    still execute — that free-while-running path was a game crash vector.

    grace_ms: optional shorter abandon window for UI-first paths (取句柄).
    Default keeps remote_runtime grace (30s) for background automation safety.
    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        if session is None:
            return AutomoveResult(
                ok=False,
                method="GetCurrentScenePosition",
                target={},
                error="session is None",
            )
        pid_raw = getattr(session, "pid", None)
        if pid_raw is None:
            return AutomoveResult(
                ok=False,
                method="GetCurrentScenePosition",
                target={},
                error="session.pid is None",
            )
        base_raw = getattr(session, "module_base", None)
        if not base_raw:
            return AutomoveResult(
                ok=False,
                method="GetCurrentScenePosition",
                target={},
                error="session.module_base missing",
            )
        base, pe = _resolve_paths(session)
        rva = resolve_export_rva(pe, EXPORT_GET_CURRENT_SCENE_POSITION)
        if rva is None:
            return AutomoveResult(
                ok=False,
                method="GetCurrentScenePosition",
                target={},
                error="export not found",
            )
        va = int(base) + int(rva)
        pid = int(pid_raw)
        ensure_pid_remote_callable(pid)
        ensure_pid_scene_stable(pid)
        with pid_call_mutex(pid, timeout_ms=7000):
            ensure_pid_remote_callable(pid)
            ensure_pid_scene_stable(pid)
            h = _open_process(pid)
            remote = 0
            thr = 0
            completed = False
            try:
                # out: int scene; float x,y,z  = 16 bytes
                # also need stub region
                total = 16 + 128
                remote = int(
                    kernel32.VirtualAllocEx(
                        wintypes.HANDLE(h),
                        None,
                        total,
                        MEM_COMMIT | MEM_RESERVE,
                        PAGE_EXECUTE_READWRITE,
                    )
                    or 0
                )
                if not remote:
                    raise OSError(
                        f"VirtualAllocEx failed err={ctypes.get_last_error()}"
                    )
                out_scene = remote
                out_x = remote + 4
                out_y = remote + 8
                out_z = remote + 12
                code_addr = remote + 16
                _wpm(h, remote, b"\x00" * 16)
                # push out_z, out_y, out_x, out_scene; call va; add esp,16; ret
                code = bytearray()
                for ptr in (out_z, out_y, out_x, out_scene):
                    code += b"\x68" + struct.pack("<I", int(ptr) & 0xFFFFFFFF)
                code += b"\xb8" + struct.pack("<I", int(va) & 0xFFFFFFFF)
                code += b"\xff\xd0"
                code += b"\x83\xc4\x10"
                code += b"\xc3"
                _wpm(h, code_addr, bytes(code))
                tid = wintypes.DWORD(0)
                thr = int(
                    kernel32.CreateRemoteThread(
                        wintypes.HANDLE(h),
                        None,
                        0,
                        ctypes.c_void_p(code_addr),
                        None,
                        0,
                        ctypes.byref(tid),
                    )
                    or 0
                )
                if not thr:
                    raise OSError(
                        f"CreateRemoteThread failed err={ctypes.get_last_error()}"
                    )
                completed = wait_remote_thread_safely(
                    thr,
                    int(timeout_ms),
                    operation="GetCurrentScenePosition",
                    grace_ms=grace_ms,
                    pid=pid,
                )
                if not completed:
                    raise TimeoutError(
                        "GetCurrentScenePosition hung; remote page intentionally leaked"
                    )
                raw = _rpm(h, remote, 16)
                scene_id = struct.unpack_from("<i", raw, 0)[0]
                x, y, z = struct.unpack_from("<fff", raw, 4)
                # Quiet on success: super_loot polls often; coords stay in result only.
                return AutomoveResult(
                    ok=True,
                    method="GetCurrentScenePosition",
                    target={"x": x, "y": y, "z": z, "mode": scene_id},
                    func_va=va,
                    ret=0,
                    scene_id=scene_id,
                    scene_pos=(x, y, z),
                    note="scene pos from plg export",
                )
            finally:
                if thr:
                    kernel32.CloseHandle(wintypes.HANDLE(thr))
                # Never free while remote thread may still execute this page.
                if remote and completed:
                    kernel32.VirtualFreeEx(
                        wintypes.HANDLE(h),
                        ctypes.c_void_p(remote),
                        0,
                        MEM_RELEASE,
                    )
                kernel32.CloseHandle(wintypes.HANDLE(h))
    except Exception as e:
        log(f"read_scene_position error: {e}")
        return AutomoveResult(
            ok=False,
            method="GetCurrentScenePosition",
            target={},
            error=str(e),
        )


def stop_automove_best_effort(
    session,
    *,
    log: LogFn | None = None,
) -> AutomoveResult:
    """
    Best-effort stop: re-issue HostMove to current scene position.

    No StopAutoMove export; may not cancel path mid-walk on all builds.
    @author by ak
    """
    log = log or (lambda _m: None)
    if session is None:
        return AutomoveResult(
            ok=False,
            method="stop_best_effort",
            target={},
            error="session is None",
            note="cannot stop path without session",
        )
    sp = read_scene_position(session, log=log)
    if not sp.ok or not sp.scene_pos:
        return AutomoveResult(
            ok=False,
            method="stop_best_effort",
            target={},
            error=str(sp.error or "no current pos for stop"),
            note="StopAutoMove export missing; get scene pos first",
        )
    try:
        x, y, z = (float(sp.scene_pos[0]), float(sp.scene_pos[1]), float(sp.scene_pos[2]))
    except Exception as e:
        return AutomoveResult(
            ok=False,
            method="stop_best_effort",
            target={},
            error=f"bad scene_pos: {e}",
        )
    try:
        mode = int(sp.scene_id) if sp.scene_id is not None else 0
    except Exception:
        mode = 0
    tgt = PathTarget(x=x, y=y, z=z, mode=mode, map_hint="stop@current")
    log(f"stop_best_effort move-to-self mode={mode} ({x:.3f},{y:.3f},{z:.3f})")
    r = host_move_to(session, tgt, log=log)
    r.method = "stop_best_effort"
    r.note = (r.note or "") + "; stop via move-to-current"
    r.scene_id = sp.scene_id
    r.scene_pos = sp.scene_pos
    return r
