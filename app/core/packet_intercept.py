# -*- coding: utf-8 -*-
"""
Game-side CD1740 packet interception + replay (dev panel feature).

Mirrors black shield's "封包拦截": hook the game's system-instruction send
entry 0x00CD1740, capture every outgoing packet (bytes copied at hook time
into a game-side ring buffer), and allow re-sending any captured packet via
the same entry.

Send chain (proven 2026-08-06):
    this   = *(*(0x015282D8) + 0x2C)   (NOT +0x24; +0x24 hangs/crashes)
    target = 0x00CD1740
    CD1740(this, packet_ptr, len) runs safely from any thread.

The inline hook copies up to MAX_PACKET bytes at entry, so the captured bytes
are exact even though the caller frees its buffer after the send.  The hook is
restored on stop()/disarm and on process exit safety; no breakpoints are used.

@author by ak
"""
from __future__ import annotations

import ctypes
import json
import struct
import threading
import time
from ctypes import wintypes
from pathlib import Path

TARGET_SYSTEM = 0x00CD1740
TARGET_SYSTEM_PROLOGUE = bytes.fromhex("64A100000000")  # mov eax, fs:[0]
TARGET_PLAIN = 0x00DAFAF0
TARGET_PLAIN_PROLOGUE = bytes.fromhex("518B4424088B")  # push ecx; mov eax,[esp+8]; mov edx,[eax+8] (6B)
TARGETS = {
    "all": (TARGET_PLAIN, TARGET_PLAIN_PROLOGUE),
    "system": (TARGET_SYSTEM, TARGET_SYSTEM_PROLOGUE),
}
ROOT_GLOBAL = 0x015282D8
THIS_OFFSET = 0x2C
MAX_RECORDS = 512
MAX_PACKET = 0x400  # 1024: system-instruction packets can exceed 128B
RECORD_SIZE = 0x20 + MAX_PACKET
PAGE_EXECUTE_READWRITE = 0x40
MEM_COMMIT_RESERVE = 0x3000
MEM_RELEASE = 0x8000
PROCESS_ALL = 0x001F0FFF

_BACKUP_DIR = Path(__file__).resolve().parents[2] / ".issues" / "packets"
_ACTIVE_INTERCEPTS: set[tuple[int, int]] = set()
_ACTIVE_INTERCEPTS_LOCK = threading.RLock()


def _hook_backup_path(pid: int, target: int = TARGET_SYSTEM) -> Path:
    """Persist the original target prologue so a stale hook can be restored. @author by ak"""
    return _BACKUP_DIR / f"hook_orig_{int(pid)}_{target:08X}.bin"


def is_intercept_active(pid: int, target: int = TARGET_SYSTEM) -> bool:
    """Return whether this Python process currently owns an interception hook."""
    with _ACTIVE_INTERCEPTS_LOCK:
        return (int(pid), int(target)) in _ACTIVE_INTERCEPTS


def restore_stale_hook(pid: int, target: int = TARGET_SYSTEM, *, log=None) -> bool:
    """Restore an orphaned hook from its saved prologue, if safe to do so.

    This is only for hooks left by an exited diagnostic process. An active hook
    owned by this process is never restored here; callers must stop it first.
    """
    log = log or (lambda _m: None)
    pid_i = int(pid)
    target_i = int(target)
    if is_intercept_active(pid_i, target_i):
        return False
    backup = _hook_backup_path(pid_i, target_i)
    if not backup.is_file():
        return False
    original = backup.read_bytes()
    expected = TARGETS.get("system", (0, b""))[1] if target_i == TARGET_SYSTEM else b""
    if len(original) != 6 or (expected and original != expected):
        log(f"packet intercept stale restore refused pid={pid_i}: invalid backup")
        return False
    h = 0
    try:
        h = open_process(pid_i)
        current = read_mem(h, target_i, 6)
        if current == original:
            backup.unlink(missing_ok=True)
            return True
        if not current or current[0] != 0xE9:
            log(f"packet intercept stale restore refused pid={pid_i}: current={current.hex().upper()}")
            return False
        _patch_safely(h, pid_i, target_i, original)
        restored = read_mem(h, target_i, 6)
        if restored != original:
            log(f"packet intercept stale restore verify failed pid={pid_i}")
            return False
        backup.unlink(missing_ok=True)
        log(f"packet intercept stale hook restored pid={pid_i} target=0x{target_i:08X}")
        return True
    except Exception as exc:
        log(f"packet intercept stale restore failed pid={pid_i}: {exc}")
        return False
    finally:
        if h:
            k32.CloseHandle(h)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = wintypes.HANDLE
k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.ReadProcessMemory.restype = wintypes.BOOL
k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCVOID,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.restype = wintypes.BOOL
k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
                               wintypes.DWORD, wintypes.DWORD]
k32.VirtualAllocEx.restype = wintypes.LPVOID
k32.VirtualFreeEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
k32.VirtualFreeEx.restype = wintypes.BOOL
k32.VirtualProtectEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
k32.VirtualProtectEx.restype = wintypes.BOOL
k32.FlushInstructionCache.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, ctypes.c_size_t]
k32.FlushInstructionCache.restype = wintypes.BOOL
k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
k32.Thread32First.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
k32.Thread32First.restype = wintypes.BOOL
k32.Thread32Next.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
k32.Thread32Next.restype = wintypes.BOOL
k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenThread.restype = wintypes.HANDLE
k32.SuspendThread.argtypes = [wintypes.HANDLE]
k32.SuspendThread.restype = wintypes.DWORD
k32.ResumeThread.argtypes = [wintypes.HANDLE]
k32.ResumeThread.restype = wintypes.DWORD
k32.CreateRemoteThread.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
                                   wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD)]
k32.CreateRemoteThread.restype = wintypes.HANDLE
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.WaitForSingleObject.restype = wintypes.DWORD


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


def p32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


def open_process(pid: int) -> int:
    h = int(k32.OpenProcess(PROCESS_ALL, False, int(pid)) or 0)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def read_mem(h: int, address: int, size: int) -> bytes:
    buf = ctypes.create_string_buffer(size)
    done = ctypes.c_size_t()
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(address), buf, size, ctypes.byref(done)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buf.raw[:done.value]


def write_mem(h: int, address: int, data: bytes) -> None:
    buf = ctypes.create_string_buffer(data)
    done = ctypes.c_size_t()
    if (not k32.WriteProcessMemory(h, ctypes.c_void_p(address), buf, len(data), ctypes.byref(done))
            or done.value != len(data)):
        raise ctypes.WinError(ctypes.get_last_error())


def u32(h: int, address: int) -> int:
    return struct.unpack("<I", read_mem(h, address, 4))[0]


def alloc_remote(h: int, size: int) -> int:
    addr = int(k32.VirtualAllocEx(h, None, size, MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE) or 0)
    if not addr:
        raise ctypes.WinError(ctypes.get_last_error())
    return addr


def _suspend_threads(pid: int):
    handles = []
    snap = k32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snap == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(te)
        ok = k32.Thread32First(snap, ctypes.byref(te))
        while ok:
            if te.th32OwnerProcessID == pid:
                th = k32.OpenThread(0x0002, False, te.th32ThreadID)
                if th:
                    if k32.SuspendThread(th) != 0xFFFFFFFF:
                        handles.append(th)
                    else:
                        k32.CloseHandle(th)
            ok = k32.Thread32Next(snap, ctypes.byref(te))
    finally:
        k32.CloseHandle(snap)
    return handles


def _resume_threads(handles) -> None:
    for th in reversed(handles):
        k32.ResumeThread(th)
        k32.CloseHandle(th)


def _patch_safely(h: int, pid: int, address: int, data: bytes) -> None:
    threads = _suspend_threads(pid)
    try:
        write_mem(h, address, data)
        k32.FlushInstructionCache(h, ctypes.c_void_p(address), len(data))
    finally:
        _resume_threads(threads)


def _rel8(buf: bytearray, pos: int, target: int) -> None:
    struct.pack_into("<b", buf, pos, target - (pos + 1))


def _rel32_cc(buf: bytearray, opcode_pos: int, target: int) -> None:
    """Patch a 0F 8x rel32 conditional jump at opcode_pos."""
    struct.pack_into("<i", buf, opcode_pos + 2, target - (opcode_pos + 6))


def build_hook_stub(log_addr: int, records_addr: int, trampoline: int) -> bytes:
    """Inline hook stub for CD1740 entry.  Captures this/packet/len/ret/tid/
    tick and up to MAX_PACKET packet bytes into the ring buffer."""
    c = bytearray(b"\x9c\x60")  # pushfd; pushad
    jumps: list[tuple[int, str]] = []

    def cc(op: bytes, cond: str) -> None:
        nonlocal c
        jumps.append((len(c), cond))
        c += op + b"\0\0\0\0"

    # eax = len (arg2 at [esp+0x2C]); require 4..MAX_PACKET
    c += b"\x8b\x44\x24\x2c"
    c += b"\x83\xf8\x04"
    cc(b"\x0f\x8c", "jl")          # jl skip
    c += b"\x3d" + p32(MAX_PACKET)
    cc(b"\x0f\x8f", "jg")          # jg skip

    # ecx = packet (arg1 at [esp+0x28]); require valid user pointer
    c += b"\x8b\x4c\x24\x28"
    c += b"\x85\xc9"
    cc(b"\x0f\x84", "jz")          # jz skip
    c += b"\x81\xf9" + p32(0x00400000)
    cc(b"\x0f\x82", "jb")          # jb skip
    c += b"\x81\xf9" + p32(0x7FFE0000)
    cc(b"\x0f\x83", "jae")         # jae skip

    # reserve slot (wraps): count = xadd [log],1 ; slot = count & (MAX-1)
    c += b"\xb8\x01\x00\x00\x00"
    c += b"\xf0\x0f\xc1\x05" + p32(log_addr)
    c += b"\x25" + p32(MAX_RECORDS - 1)
    c += b"\x69\xc0" + p32(RECORD_SIZE)
    c += b"\xba" + p32(records_addr)
    c += b"\x01\xd0"
    c += b"\x89\xc7"  # edi = record

    # header fields
    c += b"\x8b\x54\x24\x18\x89\x17"            # this
    c += b"\x8b\x54\x24\x28\x89\x57\x04"        # packet ptr
    c += b"\x8b\x54\x24\x2c\x89\x57\x08"        # len
    c += b"\x8b\x54\x24\x24\x89\x57\x0c"        # return address
    c += b"\x64\x8b\x15\x24\x00\x00\x00\x89\x57\x10"   # tid (fs:[0x24])
    c += b"\x8b\x15" + p32(0x7FFE0320) + b"\x89\x57\x14"  # tick

    # copy packet bytes: esi=packet, edi=record+0x20, ecx=min(len, MAX_PACKET)
    c += b"\x8b\x74\x24\x28"                    # esi = packet
    c += b"\x8d\x7f\x20"                        # edi += 0x20
    c += b"\x8b\x4c\x24\x2c"                    # ecx = len
    c += b"\x81\xf9" + p32(MAX_PACKET)
    jbe_pos = len(c)
    c += b"\x0f\x86\0\0\0\0"                    # jbe copy (patched below)
    c += b"\xb9" + p32(MAX_PACKET)
    copy = len(c)
    c += b"\xf3\xa4"                            # rep movsb

    skip = len(c)
    for pos, _cond in jumps:
        _rel32_cc(c, pos, skip)
    _rel32_cc(c, jbe_pos, copy)
    c += b"\x61\x9d"                            # popad; popfd
    c += b"\x68" + p32(trampoline) + b"\xc3"
    return bytes(c)


def build_plain_hook_stub(log_addr: int, records_addr: int, trampoline: int) -> bytes:
    """Inline hook stub for the plaintext crypto entry 0xDAFAF0 (all C2S).

    ARCFourSecurity::Update(this=ECX, octets).  octets: data@+4, end@+8, so the
    outgoing plaintext is [octets+4 .. octets+8).  Captures this/begin/len/ret/
    tid/tick and up to MAX_PACKET plaintext bytes into the ring buffer.
    """
    c = bytearray(b"\x9c\x60")  # pushfd; pushad
    jumps: list[tuple[int, str]] = []

    def cc(op: bytes, cond: str) -> None:
        nonlocal c
        jumps.append((len(c), cond))
        c += op + b"\0\0\0\0"

    # eax = octets (arg1 at [esp+0x28]); require valid object pointer
    c += b"\x8b\x44\x24\x28"
    c += b"\x85\xc0"
    cc(b"\x0f\x84", "jz")          # jz skip
    c += b"\x3d" + p32(0x00400000)
    cc(b"\x0f\x82", "jb")          # jb skip
    c += b"\x3d" + p32(0x7FFE0000)
    cc(b"\x0f\x83", "jae")         # jae skip
    # ebx = [octets+4] (data), ecx = [octets+8] - [octets+4] (len)
    c += b"\x8b\x58\x04"                    # mov ebx,[eax+4]
    c += b"\x8b\x48\x08"                    # mov ecx,[eax+8]
    c += b"\x29\xd9"                        # sub ecx,ebx  (len = end - begin)
    c += b"\x83\xf9\x04"
    cc(b"\x0f\x8c", "jl")          # jl skip
    c += b"\x81\xf9" + p32(MAX_PACKET)
    cc(b"\x0f\x8f", "jg")          # jg skip
    # data pointer range check
    c += b"\x81\xfb" + p32(0x00400000)
    cc(b"\x0f\x82", "jb")          # jb skip
    c += b"\x81\xfb" + p32(0x7FFE0000)
    cc(b"\x0f\x83", "jae")         # jae skip

    # reserve slot (wraps)
    c += b"\xb8\x01\x00\x00\x00"
    c += b"\xf0\x0f\xc1\x05" + p32(log_addr)
    c += b"\x25" + p32(MAX_RECORDS - 1)
    c += b"\x69\xc0" + p32(RECORD_SIZE)
    c += b"\xba" + p32(records_addr)
    c += b"\x01\xd0"
    c += b"\x89\xc7"  # edi = record

    # header fields (this=ECX, packet=ebx, len=ecx, ret, tid, tick)
    c += b"\x8b\x54\x24\x18\x89\x17"            # this
    c += b"\x89\x5f\x04"                        # packet (data ptr)
    c += b"\x89\x4f\x08"                        # len
    c += b"\x8b\x54\x24\x24\x89\x57\x0c"        # return address
    c += b"\x64\x8b\x15\x24\x00\x00\x00\x89\x57\x10"   # tid
    c += b"\x8b\x15" + p32(0x7FFE0320) + b"\x89\x57\x14"  # tick

    # copy plaintext: esi=data(ebx), edi=record+0x20, ecx=min(len, MAX_PACKET)
    c += b"\x8b\xf3"                            # mov esi,ebx
    c += b"\x8d\x7f\x20"                        # lea edi,[edi+0x20]
    c += b"\x81\xf9" + p32(MAX_PACKET)
    jbe_pos = len(c)
    c += b"\x0f\x86\0\0\0\0"                    # jbe copy (patched below)
    c += b"\xb9" + p32(MAX_PACKET)
    copy = len(c)
    c += b"\xf3\xa4"                            # rep movsb

    skip = len(c)
    for pos, _cond in jumps:
        _rel32_cc(c, pos, skip)
    _rel32_cc(c, jbe_pos, copy)
    c += b"\x61\x9d"                            # popad; popfd
    c += b"\x68" + p32(trampoline) + b"\xc3"
    return bytes(c)


def build_replay_stub(code: int, frame: int, packet: int, length: int, this_ptr: int, ret: int) -> bytes:
    """Remote thread stub: fake EBP frame ([EBP+8]=packet,[EBP+0xC]=len) then
    call CD1740(this, packet, len); store EAX; return."""
    c = bytearray()
    c += b"\x55"                      # push ebp
    c += b"\xBD" + p32(frame)         # mov ebp, frame
    c += b"\x68" + p32(length)        # push len
    c += b"\x68" + p32(packet)        # push packet
    c += b"\xB9" + p32(this_ptr)      # mov ecx, this
    c += b"\xB8" + p32(TARGET_SYSTEM)  # mov eax, CD1740
    c += b"\xFF\xD0"                  # call eax
    c += b"\xA3" + p32(ret)           # mov [ret], eax
    c += b"\x5D"                      # pop ebp
    c += b"\xC3"                      # ret
    return bytes(c)


def parse_record(rec: bytes) -> dict:
    this, packet, length, ret, tid, tick = struct.unpack_from("<6I", rec)
    n = max(0, min(int(length), MAX_PACKET))
    data = bytes(rec[0x20:0x20 + n])
    return {
        "this": this,
        "packet": packet,
        "len": length,
        "ret": ret,
        "tid": tid,
        "tick": tick,
        "data": data,
        "hex": data.hex().upper(),
    }


def format_record(rec: dict, seq: int = 0) -> str:
    h = rec["hex"]
    head = h[:24] + ("..." if len(h) > 24 else "")
    return (f"#{seq} len={rec['len']} this=0x{rec['this']:08X} "
            f"tid={rec['tid']} ret=0x{rec['ret']:08X} {head}")


# --- packet annotation (parse + label known system instructions) ---

# 丸子 family layout (38 bytes, proven live): len16@[0], kind@[7],
# P1 bag coordinate @[0x0A/0x0B], system-instruction tid (u16 LE) @[0x0D],
# position (3× f32 LE) @[0x19].  See app/core/wanzi_packet.py P1/P2.
WANZI_KIND_BY_BYTE7: dict[int, str] = {
    0x5D: "丸子P2",
    0x5E: "丸子P1·内功",
    0x5F: "丸子P1·外功",
}
WANZI_KIND_MEANING: dict[str, str] = {
    "丸子P1·外功": "使用外功丸子（系统指令）",
    "丸子P1·内功": "使用内功丸子（系统指令）",
    "丸子P2": "丸子攻击（系统指令）",
}
WANZI_TID_RANGE = (0x41FE, 0x4201)
WANZI_PACKAGE_OFF = 0x0A
WANZI_SLOT_OFF = 0x0B
WANZI_TID_OFF = 0x0D
WANZI_POS_OFF = 0x19

# 降龙包的公共结构来自多账号捕获对比。偏移 0x11 的 u32 是运行时
# 变体字段，不能按 PID/RID 推导，也不能用某个账号的样本值兜底。
JIANG_LONG_HEADER = bytes.fromhex("1F000726010000052301FFFFFF00000000")
JIANG_LONG_VARIANT_OFF = 0x11
JIANG_LONG_POS_OFF = 0x19
JIANG_LONG_PACKET_SIZE = 38


def parse_jianglong_packet(data: bytes) -> dict:
    """Parse a captured 38-byte 降龙 packet without guessing its variant.

    The per-client u32 at ``0x11`` is returned as ``variant_id``. It is
    intentionally not mapped to PID/RID or replaced with a sample default.
    """
    raw = bytes(data or b"")
    out: dict = {
        "known": False,
        "variant_id": None,
        "variant_bytes": "",
        "pos": None,
        "tail": None,
        "hex": raw.hex().upper(),
    }
    if len(raw) != JIANG_LONG_PACKET_SIZE:
        return out
    if not raw.startswith(JIANG_LONG_HEADER) or raw[-1:] != b"\x01":
        return out
    try:
        out["variant_id"] = struct.unpack_from("<I", raw, JIANG_LONG_VARIANT_OFF)[0]
        out["variant_bytes"] = raw[JIANG_LONG_VARIANT_OFF:JIANG_LONG_VARIANT_OFF + 4].hex().upper()
        out["pos"] = tuple(struct.unpack_from("<fff", raw, JIANG_LONG_POS_OFF))
        out["tail"] = raw[-1]
        out["known"] = True
    except (struct.error, ValueError):
        return out
    return out


def annotate_packet(data: bytes) -> dict:
    """Parse one captured CD1740 packet into structured fields + a label.

    Recognizes the 丸子 system-instruction family structurally (so P1 packets
    with any package/slot coordinate still match), otherwise falls back to a
    generic len16/tid parse.  Returns dict with ``known/name/meaning/kind/
    len16/tid/package/slot/pos``.

    @author by ak
    """
    data = bytes(data or b"")
    out: dict = {
        "known": False,
        "name": "",
        "meaning": "",
        "kind": "",
        "len16": None,
        "tid": None,
        "package": None,
        "slot": None,
        "pos": None,
        "variant_id": None,
        "variant_bytes": "",
        "hex": data.hex().upper(),
    }
    jianglong = parse_jianglong_packet(data)
    if jianglong["known"]:
        out["known"] = True
        out["name"] = "降龙十八掌"
        out["meaning"] = "降龙技能系统指令"
        out["variant_id"] = jianglong["variant_id"]
        out["variant_bytes"] = jianglong["variant_bytes"]
        out["pos"] = jianglong["pos"]
        return out

    if len(data) < 2:
        return out
    out["len16"] = struct.unpack("<H", data[:2])[0]
    if len(data) < 8:
        return out
    kind = data[7]
    out["kind"] = f"0x{kind:02X}"
    if len(data) >= 15:
        out["tid"] = struct.unpack("<H", data[0x0D:0x0F])[0]
    if (
        kind in WANZI_KIND_BY_BYTE7
        and out["tid"] is not None
        and WANZI_TID_RANGE[0] <= out["tid"] <= WANZI_TID_RANGE[1]
    ):
        name = WANZI_KIND_BY_BYTE7[kind]
        out["known"] = True
        out["name"] = name
        out["meaning"] = WANZI_KIND_MEANING.get(name, "")
        if name != "丸子P2" and len(data) >= 0x0C:
            out["package"] = data[WANZI_PACKAGE_OFF]
            out["slot"] = data[WANZI_SLOT_OFF]
        if len(data) >= WANZI_POS_OFF + 12:
            x, y, z = struct.unpack("<fff", data[WANZI_POS_OFF:WANZI_POS_OFF + 12])
            out["pos"] = (round(x, 2), round(y, 2), round(z, 2))
    return out


def format_annotated_record(rec: dict, seq: int = 0) -> str:
    """One-line list entry: raw record + packet annotation label. @author by ak"""
    base = format_record(rec, seq)
    ann = annotate_packet(rec.get("data") or b"")
    if ann.get("name") == "降龙十八掌":
        variant = ann.get("variant_bytes") or "?"
        return f"{base} · 降龙十八掌 variant={variant}"
    if not ann.get("known"):
        return base
    extra: list[str] = []
    if ann.get("package") is not None:
        extra.append(f"P{ann['package']}/S{ann['slot']}")
    if ann.get("pos"):
        x, y, z = ann["pos"]
        extra.append(f"({x},{y},{z})")
    suffix = f" · {ann['name']}"
    if extra:
        suffix += " " + " ".join(extra)
    return f"{base} {suffix}"


def packet_detail_text(rec: dict) -> str:
    """Multi-line human-readable breakdown of one captured packet. @author by ak"""
    ann = annotate_packet(rec.get("data") or b"")
    lines = [
        f"len={rec.get('len')} this=0x{rec.get('this', 0):08X} "
        f"tid={rec.get('tid')} ret=0x{rec.get('ret', 0):08X} "
        f"tick={rec.get('tick')}",
        f"HEX  {rec.get('hex', '')}",
    ]
    if not ann.get("known"):
        if ann.get("len16") is not None:
            lines.append(f"len16={ann['len16']} kind={ann.get('kind') or '-'} "
                         f"tid=0x{ann.get('tid') or 0:04X} (未识别系统指令)")
        else:
            lines.append("短包/无法解析")
        return "\n".join(lines)
    lines.append(
        f"识别 {ann['name']} · {ann['meaning']}  kind={ann['kind']} "
        f"tid=0x{ann['tid']:04X}"
    )
    if ann.get("package") is not None:
        lines.append(f"背包 P{ann['package']} / 槽 {ann['slot']}")
    if ann.get("pos"):
        x, y, z = ann["pos"]
        lines.append(f"坐标 ({x}, {y}, {z})")
    return "\n".join(lines)


RECORDING_VERSION = 1


def recording_row(rec: dict, index: int) -> dict:
    """Convert one live capture record to a portable JSON row."""
    return {
        "index": int(index),
        "tick": int(rec.get("tick") or 0),
        "len": int(rec.get("len") or 0),
        "ret": int(rec.get("ret") or 0),
        "tid": int(rec.get("tid") or 0),
        "this": int(rec.get("this") or 0),
        "packet": int(rec.get("packet") or 0),
        "hex": bytes(rec.get("data") or b"").hex().upper(),
    }


def write_recording(path: str | Path, pid: int, records: list[dict], *, label: str = "") -> Path:
    """Write a machine-readable action recording as JSONL."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({
            "type": "xajh_packet_recording",
            "version": RECORDING_VERSION,
            "pid": int(pid),
            "label": str(label or ""),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }, ensure_ascii=False, separators=(",", ":")) + "\n")
        for index, rec in enumerate(records):
            fh.write(json.dumps({"type": "packet", **recording_row(rec, index)},
                                ensure_ascii=False, separators=(",", ":")) + "\n")
    return target


def read_recording(path: str | Path) -> tuple[dict, list[dict]]:
    """Load a JSONL recording and reconstruct replayable packet records."""
    meta: dict = {}
    records: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if line_no == 1 and row.get("type") == "xajh_packet_recording":
                if int(row.get("version", 0)) != RECORDING_VERSION:
                    raise ValueError(f"不支持的记录版本: {row.get('version')}")
                meta = row
                continue
            if row.get("type") != "packet":
                continue
            data = bytes.fromhex(str(row.get("hex") or ""))
            if not 2 <= len(data) <= MAX_PACKET:
                raise ValueError(f"第 {line_no} 行封包长度无效: {len(data)}")
            records.append({
                "this": int(row.get("this") or 0),
                "packet": int(row.get("packet") or 0),
                "len": len(data),
                "ret": int(row.get("ret") or 0),
                "tid": int(row.get("tid") or 0),
                "tick": int(row.get("tick") or 0),
                "data": data,
                "hex": data.hex().upper(),
            })
    if not records:
        raise ValueError("记录文件中没有可回放封包")
    return meta, records


def replay_records(
    pid: int,
    records: list[dict],
    *,
    gap_scale: float = 1.0,
    max_gap_s: float = 3.0,
    min_gap_s: float = 0.01,
    stop_event: threading.Event | None = None,
) -> list[int]:
    """Replay a captured action sequence while preserving its relative timing."""
    if not records:
        return []
    if gap_scale < 0 or max_gap_s < 0 or min_gap_s < 0:
        raise ValueError("回放时序参数不能为负数")
    results: list[int] = []
    previous_tick: int | None = None
    for rec in records:
        if stop_event is not None and stop_event.is_set():
            break
        tick = int(rec.get("tick") or 0)
        if previous_tick is not None and tick >= previous_tick:
            gap = min(max_gap_s, max(min_gap_s, (tick - previous_tick) / 1000.0 * gap_scale))
            if stop_event is not None:
                if stop_event.wait(gap):
                    break
            else:
                time.sleep(gap)
        data = bytes(rec.get("data") or b"")
        results.append(replay_packet(pid, data))
        previous_tick = tick
    return results


class InterceptState:
    """Armed packet-interception session on one game process.

    target: "all" (0xDAFAF0 plaintext crypto entry — every C2S packet) or
    "system" (0xCD1740 system-instruction send).  Default "all".
    """

    def __init__(self, pid: int, max_records: int = MAX_RECORDS,
                 target: str = "system") -> None:
        self.pid = int(pid)
        self.max_records = int(max_records)
        target = str(target or "system").lower()
        if target not in TARGETS:
            raise ValueError(f"unknown interception target {target!r}")
        self.target = target
        self._va, self._prologue = TARGETS[target]
        self.lock = threading.Lock()
        self._h: int | None = None
        self._remote = 0
        self._old = b""
        self._armed = False
        self._last_count = 0

    @property
    def armed(self) -> bool:
        return self._armed

    def start(self) -> None:
        with self.lock:
            if self._armed:
                return
            if self.target == "all":
                raise RuntimeError(
                    "target='all' (0xDAFAF0 crypto entry) is integrity-checked by the "
                    "game and crashes it; use target='system' (CD1740)"
                )
            h = open_process(self.pid)
            try:
                va, prologue = self._va, self._prologue
                old = read_mem(h, va, 6)
                if old == prologue:
                    backup = _hook_backup_path(self.pid, va)
                    try:
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        backup.write_bytes(old)
                    except Exception:  # noqa: BLE001
                        pass
                elif old[0] == 0xE9:
                    # stale hook from a previous session: restore the saved prologue.
                    backup = _hook_backup_path(self.pid, va)
                    if backup.is_file():
                        try:
                            orig = backup.read_bytes()
                        except Exception:  # noqa: BLE001
                            orig = b""
                        if len(orig) == 6:
                            _patch_safely(h, self.pid, va, orig)
                            old = read_mem(h, va, 6)
                    if old != prologue:
                        raise RuntimeError(
                            f"0x{va:08X} is hooked by another session and no restore "
                            "copy was found"
                        )
                else:
                    raise RuntimeError(
                        f"0x{va:08X} prologue mismatch: {old.hex().upper()} "
                        "(game changed)"
                    )
                stub = (build_plain_hook_stub if self.target == "all"
                        else build_hook_stub)
                total = 4 + self.max_records * RECORD_SIZE
                remote = alloc_remote(h, total + 0x1000)
                try:
                    write_mem(h, remote, b"\0" * total)
                    hook = remote + total
                    tramp = hook + 0x400
                    write_mem(h, tramp, old + b"\xe9" + struct.pack(
                        "<i", (va + 6) - (tramp + 6 + 5)))
                    write_mem(h, hook, stub(remote, remote + 4, tramp))
                    old_protect = wintypes.DWORD()
                    if not k32.VirtualProtectEx(h, ctypes.c_void_p(va), 6,
                                                PAGE_EXECUTE_READWRITE, ctypes.byref(old_protect)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    detour = b"\xe9" + struct.pack("<i", hook - (va + 5)) + b"\x90"
                    _patch_safely(h, self.pid, va, detour)
                except Exception:
                    k32.VirtualFreeEx(h, ctypes.c_void_p(remote), 0, MEM_RELEASE)
                    raise
                self._h = h
                self._remote = remote
                self._old = old
                self._last_count = 0
                self._armed = True
                with _ACTIVE_INTERCEPTS_LOCK:
                    _ACTIVE_INTERCEPTS.add((self.pid, self._va))
            except Exception:
                k32.CloseHandle(h)
                raise

    def poll(self) -> list[dict]:
        with self.lock:
            if not self._armed or not self._h:
                return []
            count = min(u32(self._h, self._remote), self.max_records)
            new: list[dict] = []
            if count > self._last_count:
                blob = read_mem(self._h, self._remote + 4,
                                min(count, self.max_records) * RECORD_SIZE)
                for i in range(self._last_count, count):
                    rec = blob[i * RECORD_SIZE:(i + 1) * RECORD_SIZE]
                    if len(rec) == RECORD_SIZE:
                        new.append(parse_record(rec))
                self._last_count = count
            return new

    def stop(self) -> list[dict]:
        with self.lock:
            if not self._armed:
                return []
            records: list[dict] = []
            try:
                count = min(u32(self._h, self._remote), self.max_records)
                if count:
                    blob = read_mem(self._h, self._remote + 4, count * RECORD_SIZE)
                    records = [parse_record(blob[i * RECORD_SIZE:(i + 1) * RECORD_SIZE])
                               for i in range(count)]
            finally:
                if self._old:
                    try:
                        _patch_safely(self._h, self.pid, self._va, self._old)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    _hook_backup_path(self.pid, self._va).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
                if self._remote:
                    try:
                        k32.VirtualFreeEx(self._h, ctypes.c_void_p(self._remote), 0, MEM_RELEASE)
                    except Exception:
                        pass
                if self._h:
                    k32.CloseHandle(self._h)
                self._h = None
                self._remote = 0
                self._old = b""
                self._armed = False
                self._last_count = 0
                with _ACTIVE_INTERCEPTS_LOCK:
                    _ACTIVE_INTERCEPTS.discard((self.pid, self._va))
            return records


def resolve_send_this(pid: int) -> int:
    """this for CD1740 = *(*(0x15282D8) + 0x2C). @author by ak"""
    h = open_process(pid)
    try:
        root = u32(h, ROOT_GLOBAL)
        this_ptr = u32(h, root + THIS_OFFSET) if root else 0
        if not this_ptr:
            raise RuntimeError("game send this unavailable (root+0x2C = 0)")
        return this_ptr
    finally:
        k32.CloseHandle(h)


def replay_packet(pid: int, packet_bytes: bytes, timeout_ms: int = 4000) -> int:
    """Re-send a captured packet through game CD1740(this=root+0x2C, packet, len).
    Returns CD1740's EAX (1 = accepted). @author by ak"""
    if not packet_bytes or not 2 <= len(packet_bytes) <= MAX_PACKET:
        raise ValueError(f"packet length {len(packet_bytes)} out of range")
    this_ptr = resolve_send_this(pid)
    h = open_process(pid)
    remote = 0
    completed = False
    try:
        total = 0x1000 + len(packet_bytes)
        remote = alloc_remote(h, total)
        packet_addr = remote + 0x400
        frame_addr = remote + 0x100
        ret_addr = remote + 0x80
        code_addr = remote + 0x20
        write_mem(h, frame_addr, b"\0" * 8 + p32(packet_addr) + p32(len(packet_bytes)))
        write_mem(h, packet_addr, packet_bytes)
        write_mem(h, ret_addr, b"\0" * 4)
        write_mem(h, code_addr, build_replay_stub(
            code_addr, frame_addr, packet_addr, len(packet_bytes), this_ptr, ret_addr))
        tid = wintypes.DWORD()
        th = k32.CreateRemoteThread(h, None, 0, ctypes.c_void_p(code_addr), None, 0,
                                    ctypes.byref(tid))
        if not th:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if k32.WaitForSingleObject(th, max(1, timeout_ms)) != 0:
                raise TimeoutError("CD1740 replay timed out; remote allocation retained")
            completed = True
        finally:
            k32.CloseHandle(th)
        return u32(h, ret_addr)
    finally:
        if remote and completed:
            k32.VirtualFreeEx(h, ctypes.c_void_p(remote), 0, MEM_RELEASE)
        k32.CloseHandle(h)
