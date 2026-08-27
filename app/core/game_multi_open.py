# -*- coding: utf-8 -*-
"""
Game client multi-open unlock for xajh.exe (memory-only).

The client limits concurrent processes with 3 named mutex slots:

  XAJHElementClient_Multiple_Instance_Tag_Name_{0,1,2}

Startup check (x86, image base 0x400000):

  loop edi=0/4/8:
    CreateMutexW(name[edi])
    if not ERROR_ALREADY_EXISTS -> take slot, continue boot
  if all taken -> MessageBox err 0x11 / title 0x12
    ("您开启的游戏窗口数量超出了可支持的最大数量")

Policy:
  - NEVER modify xajh.exe on disk (WeGame / client fingerprint must stay intact).
  - Only patch the limit branch in live process memory.
  - If a previous version left ``xajh.exe.multiopen.bak``, restore it once so
    the on-disk image matches the original fingerprint again.
  - Surface clear tips when the race is lost or OpenProcess fails.

@author by ak
"""
from __future__ import annotations

import ctypes
import shutil
import struct
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

# cmp edi, 0x0C; jb short; mov edi, 0x12  (title id 18 "启动失败")
_SIG = bytes.fromhex("83ff0c72c7bf12000000")
_PATCH_PREFIX = bytes.fromhex("e989000000")  # known-build example only
_ORIG_PREFIX = _SIG[:5]

PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.LPVOID,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.LPCVOID,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.WriteProcessMemory.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.VirtualProtectEx.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    ctypes.c_size_t,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.VirtualProtectEx.restype = wintypes.BOOL
kernel32.FlushInstructionCache.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCVOID,
    ctypes.c_size_t,
]
kernel32.FlushInstructionCache.restype = wintypes.BOOL

PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_READ = 0x20


def _log(log: LogFn | None, msg: str) -> None:
    if log:
        try:
            log(msg)
        except Exception:
            pass


def _backup_path(exe: Path) -> Path:
    return exe.with_suffix(exe.suffix + ".multiopen.bak")


def find_limit_site_file(data: bytes) -> int | None:
    """Return file offset of cmp edi,0xC / jb multi-open limit, or None."""
    i = data.find(_SIG)
    if i >= 0:
        return i
    pat = bytes.fromhex("83ff0c72")
    start = 0
    while True:
        j = data.find(pat, start)
        if j < 0:
            return None
        window = data[j : j + 0x40]
        if bytes.fromhex("bf12000000") in window and bytes.fromhex("bf11000000") in window:
            return j
        start = j + 1


def is_file_patched(data: bytes) -> bool:
    """True if a previous disk multi-open patch is still present."""
    if find_limit_site_file(data) is not None:
        return False
    pos = 0
    tail = bytes.fromhex("bf12000000")
    err = bytes.fromhex("bf11000000")
    while True:
        j = data.find(tail, pos)
        if j < 0:
            return False
        if j >= 5 and data[j - 5] == 0xE9 and err in data[j : j + 0x80]:
            return True
        pos = j + 1


def heal_previous_disk_patch(exe_path: Path | None, *, log: LogFn | None = None) -> str | None:
    """
    If an older helper version rewrote xajh.exe, restore from backup once.

    Does not apply any new on-disk patch. Returns a short status or None.
    @author by ak
    """
    if exe_path is None:
        return None
    exe_path = Path(exe_path)
    bak = _backup_path(exe_path)
    if not exe_path.is_file():
        return None
    try:
        data = exe_path.read_bytes()
    except Exception as e:
        return f"读取 xajh.exe 失败（跳过磁盘修复）: {e}"

    needs = is_file_patched(data) or bak.is_file()
    if not needs:
        return None
    if not bak.is_file():
        if is_file_patched(data):
            return (
                "检测到磁盘曾被多开改写，但缺少 xajh.exe.multiopen.bak，"
                "无法自动恢复指纹；请用 WeGame/更新器校验修复客户端"
            )
        return None
    # Only restore when current file differs from backup (or still patched).
    try:
        bak_data = bak.read_bytes()
    except Exception as e:
        return f"读取备份失败: {e}"
    if data == bak_data and not is_file_patched(data):
        return None
    try:
        shutil.copy2(bak, exe_path)
        _log(log, f"多开: 已从备份恢复原始 {exe_path.name}（不再改磁盘）")
        return f"已从 {bak.name} 恢复原始客户端（避免指纹不一致）"
    except Exception as e:
        return (
            f"自动恢复原始客户端失败: {e}；"
            "请手动用 WeGame 校验，或关闭游戏后将 "
            f"{bak.name} 复制为 {exe_path.name}"
        )


def _module_base(pid: int) -> int | None:
    """Return xajh.exe remote base (usually 0x400000)."""
    try:
        import pymem

        pm = pymem.Pymem()
        pm.open_process_from_id(int(pid))
        try:
            for mod in pm.list_modules():
                name = (getattr(mod, "name", None) or "").lower()
                if name == "xajh.exe":
                    return int(mod.lpBaseOfDll) & 0xFFFFFFFF
            base = int(getattr(pm, "base_address", 0) or 0)
            if base:
                return base & 0xFFFFFFFF
        finally:
            try:
                pm.close_process()
            except Exception:
                pass
    except Exception:
        pass
    return 0x400000


def _rpm(h, addr: int, n: int) -> bytes | None:
    buf = (ctypes.c_ubyte * n)()
    got = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(
        h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)
    )
    if not ok or got.value != n:
        return None
    return bytes(buf)


def _wpm(h, addr: int, data: bytes) -> bool:
    old = wintypes.DWORD(0)
    kernel32.VirtualProtectEx(
        h, ctypes.c_void_p(addr), len(data), PAGE_EXECUTE_READWRITE, ctypes.byref(old)
    )
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    wrote = ctypes.c_size_t(0)
    ok = kernel32.WriteProcessMemory(
        h, ctypes.c_void_p(addr), buf, len(data), ctypes.byref(wrote)
    )
    kernel32.VirtualProtectEx(
        h,
        ctypes.c_void_p(addr),
        len(data),
        old.value or PAGE_EXECUTE_READ,
        ctypes.byref(old),
    )
    try:
        kernel32.FlushInstructionCache(h, ctypes.c_void_p(addr), len(data))
    except Exception:
        pass
    return bool(ok) and wrote.value == len(data)


def find_limit_site_remote(h, base: int) -> int | None:
    """Scan remote .text for multi-open limit signature; return remote VA."""
    known_rva = 0xB8727
    for rva in (known_rva,):
        va = (base + rva) & 0xFFFFFFFF
        blob = _rpm(h, va, len(_SIG))
        if blob == _SIG:
            return va
        if blob and blob[:5] == _PATCH_PREFIX and blob[5:10] == bytes.fromhex(
            "bf12000000"
        ):
            return va
    start = (base + 0x1000) & 0xFFFFFFFF
    size = 0xE00000
    step = 0x100000
    for off in range(0, size, step):
        chunk = _rpm(h, start + off, min(step + 16, size - off))
        if not chunk:
            continue
        j = chunk.find(_SIG)
        if j >= 0:
            return start + off + j
        j = chunk.find(_PATCH_PREFIX + bytes.fromhex("bf12000000"))
        if j >= 0:
            return start + off + j
    return None


def apply_memory_patch(pid: int, *, log: LogFn | None = None) -> tuple[bool, str]:
    """Patch one live xajh.exe process (no disk write)."""
    pid = int(pid)
    rights = (
        PROCESS_VM_READ
        | PROCESS_VM_WRITE
        | PROCESS_VM_OPERATION
        | PROCESS_QUERY_INFORMATION
        | PROCESS_QUERY_LIMITED_INFORMATION
    )
    h = kernel32.OpenProcess(rights, False, pid)
    if not h:
        return False, f"OpenProcess pid={pid} 失败 err={ctypes.get_last_error()}（需管理员）"
    try:
        base = _module_base(pid) or 0x400000
        site = find_limit_site_remote(h, base)
        if site is None:
            return False, f"pid={pid} 未找到限制点（可能尚未映射完或版本变化）"
        cur = _rpm(h, site, 10)
        if not cur:
            return False, f"pid={pid} 读取失败"
        if cur[:5] == _PATCH_PREFIX or (
            len(cur) >= 5 and cur[0] == 0xE9 and cur[5:10] == bytes.fromhex("bf12000000")
        ):
            return True, f"pid={pid} 内存已解锁"
        if cur[:10] != _SIG and cur[:5] != _ORIG_PREFIX:
            return False, f"pid={pid} 特征不匹配 cur={cur.hex()}"

        pre = _rpm(h, site - 0x80, 0x80) or b""
        success = None
        k = 0
        while True:
            j = pre.find(b"\x0f\x84", k)
            if j < 0:
                break
            rel = struct.unpack_from("<i", pre, j + 2)[0]
            abs_va = (site - 0x80 + j + 6 + rel) & 0xFFFFFFFF
            if abs_va > site:
                success = abs_va
            k = j + 1
        if success is None:
            success = (site + 5 + 0x89) & 0xFFFFFFFF
        rel_jmp = ctypes.c_int32((success - (site + 5)) & 0xFFFFFFFF).value
        patch = b"\xE9" + struct.pack("<i", rel_jmp)
        if not _wpm(h, site, patch):
            return False, f"pid={pid} WriteProcessMemory 失败"
        _log(log, f"多开: 内存解锁 pid={pid} va=0x{site:X}")
        return True, f"pid={pid} 内存解锁成功"
    finally:
        kernel32.CloseHandle(h)


def patch_all_live_clients(*, log: LogFn | None = None) -> list[str]:
    """Apply memory patch to every running xajh.exe."""
    notes: list[str] = []
    try:
        from app.core.inject_gate import find_xajh_processes

        procs = find_xajh_processes()
    except Exception as e:
        return [f"枚举进程失败: {e}"]
    if not procs:
        return ["当前无 xajh.exe（启动游戏后守护会自动尝试内存解锁）"]
    for p in procs:
        pid = int(p.get("pid") or 0)
        if pid <= 0:
            continue
        ok, msg = apply_memory_patch(pid, log=log)
        notes.append(("OK " if ok else "FAIL ") + msg)
    return notes


class MultiOpenWatchdog:
    """
    Poll new xajh.exe and memory-patch as early as possible.

    Does not modify any game file on disk.
    @author by ak
    """

    def __init__(self, *, log: LogFn | None = None, interval_s: float = 0.25) -> None:
        self._log = log
        self._interval = max(0.15, float(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # pid -> first seen time
        self._seen: dict[int, float] = {}
        # pids already unlocked (log once, then silent)
        self._unlocked: set[int] = set()
        # pids that already received a failure tip
        self._fail_logged: set[int] = set()

    @property
    def running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="xajh-multiopen-wd", daemon=True
        )
        self._thread.start()
        _log(self._log, "多开: 内存守护已启动（不改 xajh.exe 文件）")

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            try:
                t.join(timeout=2.0)
            except Exception:
                pass
        self._thread = None
        _log(self._log, "多开: 内存守护已停止")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                from app.core.inject_gate import find_xajh_processes

                live = {
                    int(p["pid"]) for p in find_xajh_processes() if p.get("pid")
                }
                # drop dead
                for pid in list(self._seen):
                    if pid not in live:
                        self._seen.pop(pid, None)
                        self._unlocked.discard(pid)
                        self._fail_logged.discard(pid)
                now = time.time()
                for pid in live:
                    if pid in self._unlocked:
                        continue
                    first = self._seen.get(pid)
                    if first is None:
                        self._seen[pid] = now
                        first = now
                    age = now - first
                    # After early window, only rare silent re-check
                    if age > 8.0 and int(age * 2) % 20 != 0:
                        continue
                    ok = False
                    msg = ""
                    attempts = 12 if age < 2.0 else 3
                    for _ in range(attempts):
                        if self._stop.is_set():
                            break
                        ok, msg = apply_memory_patch(pid, log=None)
                        if ok:
                            break
                        time.sleep(0.05 if age < 2.0 else 0.12)
                    if ok:
                        self._unlocked.add(pid)
                        _log(self._log, f"多开守护: {msg}")
                        continue
                    # Fail: log once after a short grace, not every tick
                    if age >= 2.5 and pid not in self._fail_logged:
                        self._fail_logged.add(pid)
                        _log(self._log, f"多开守护: {msg}")
                        _log(
                            self._log,
                            "多开提示: 内存解锁未赶上启动检测时，游戏仍可能弹"
                            "「窗口数量超出最大数量」——不会修改 exe；"
                            "可先开助手游戏多开再逐个启动客户端重试",
                        )
            except Exception as e:
                _log(self._log, f"多开守护异常: {e}")
            self._stop.wait(self._interval)


_watchdog: MultiOpenWatchdog | None = None


def enable_multi_open(*, log: LogFn | None = None) -> tuple[bool, str]:
    """
    Enable game multi-open via memory only (never rewrite xajh.exe).

    @author by ak
    """
    global _watchdog
    from app.core.plg_exports import find_xajh_exe

    parts: list[str] = []
    exe = find_xajh_exe()
    heal = heal_previous_disk_patch(Path(exe) if exe else None, log=log)
    if heal:
        parts.append(heal)

    parts.append("模式=仅内存（不修改 xajh.exe，避免指纹校验失败）")
    notes = patch_all_live_clients(log=log)
    parts.extend(notes)

    if _watchdog is None:
        _watchdog = MultiOpenWatchdog(log=log, interval_s=0.25)
    else:
        _watchdog._interval = 0.25
    _watchdog.start()
    parts.append("内存守护已运行")

    # Success = watchdog up; live patch may be empty if no client yet.
    any_fail = any(n.startswith("FAIL ") for n in notes)
    ok = not any_fail
    if any_fail:
        parts.append(
            "提示: 部分进程内存解锁失败，请确认助手以管理员运行"
        )
    else:
        parts.append(
            "提示: 请先开本助手再启动游戏窗口；若仍弹数量上限，属启动竞态，可重试启动"
        )
    return ok, " | ".join(parts)


def disable_multi_open(*, log: LogFn | None = None) -> tuple[bool, str]:
    """
    Stop multi-open watchdog. Does not rewrite game files.

    @author by ak
    """
    global _watchdog
    parts: list[str] = []
    if _watchdog is not None:
        _watchdog.stop()
        _watchdog = None
        parts.append("内存守护已停")
    from app.core.plg_exports import find_xajh_exe

    exe = find_xajh_exe()
    heal = heal_previous_disk_patch(Path(exe) if exe else None, log=log)
    if heal:
        parts.append(heal)
    else:
        parts.append("未改磁盘")
    parts.append(
        "已运行客户端的内存状态保留到进程退出；新开窗口将恢复游戏默认最多 3 开"
    )
    return True, " | ".join(parts)


def resolve_xajh_exe() -> Path | None:
    """Locate xajh.exe path."""
    try:
        from app.core.plg_exports import find_xajh_exe

        p = find_xajh_exe()
        return Path(p) if p else None
    except Exception:
        return None
