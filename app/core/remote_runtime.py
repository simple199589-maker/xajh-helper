# -*- coding: utf-8 -*-
"""Shared Win32 process-memory and remote x86 call runtime."""
from __future__ import annotations

import ctypes
import struct
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes
from enum import Enum
from typing import Iterator, Sequence


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_EXECUTE_READWRITE = 0x40
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 0x102
INFINITE = 0xFFFFFFFF

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
kernel32.ReleaseMutex.restype = wintypes.BOOL
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
kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
kernel32.GetProcessId.restype = wintypes.DWORD
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
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
kernel32.Module32FirstW.restype = wintypes.BOOL
kernel32.Module32NextW.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
kernel32.Module32NextW.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD


# pid -> monotonic timestamp when last remote hang was observed
_remote_hung_until: dict[int, float] = {}
_REMOTE_HUNG_COOLDOWN_SEC = 20.0
# after diagnostic timeout, wait this long before abandoning (no free)
_REMOTE_WAIT_GRACE_MS = 30_000
# pid -> sticky hard-dead until cleared (err=5 / process gone)
_remote_hard_dead: set[int] = set()

# Foreign CreateRemoteThread calls are unsafe while the client replaces its
# host/scene graph.  The injected bridge can sample that graph on the game UI
# thread, so keep a short per-pid scene generation fence here at the one common
# entry point used by all remote_call_* facades.
_SCENE_GATE_SETTLE_SEC = 1.2
# 连续读不到 host 快照多少次才认定为场景切换；单次失败是桥接并发抖动。
_SCENE_GATE_FAIL_STREAK = 3
# A feature action usually performs several serialized CRT calls.  Reopening
# the bridge and sampling the same UI-thread scene before every one made a
# single click pay the bridge timeout repeatedly.  The session window already
# publishes this snapshot every second; keep a shorter lease for probes made
# between those publishes while still failing closed on a reported transition.
_SCENE_GATE_PROBE_MAX_AGE_SEC = 0.75
_scene_gate_lock = threading.RLock()
_scene_gate_state: dict[int, dict[str, float | int | bool]] = {}


class SceneTransitionError(TimeoutError):
    """A remote call was refused while the game scene was not stable."""


def note_pid_scene_snapshot(
    pid: int,
    *,
    host_present: bool,
    scene_id: int | None,
    sampled_at: float | None = None,
) -> None:
    """Feed one UI-thread host snapshot into the remote-call scene fence.

    桥接是单 UI 线程资源，多调用方并发时 host_snapshot 的瞬时失败很常见；
    这类抖动不能清零稳定窗（否则 settle 门永远关不上远端调用）。只有
    scene_id 变化或连续多次读不到，才视为场景切换。

    @author by ak
    """
    pid = int(pid)
    now = time.monotonic() if sampled_at is None else float(sampled_at)
    sid = int(scene_id or 0)
    ready = bool(host_present and sid > 0)
    with _scene_gate_lock:
        prev = dict(_scene_gate_state.get(pid) or {})
        prev_sid = int(prev.get("scene_id") or 0)
        prev_ready = bool(prev.get("ready"))
        prev_fail = int(prev.get("fail_streak") or 0)
        stable_since = float(prev.get("stable_since") or now)
        if not ready:
            if prev_ready and prev_sid and sid and sid != prev_sid:
                # 读数给出了不同 scene_id：真实的切换证据，立即关门。
                _scene_gate_state[pid] = {
                    "ready": False,
                    "scene_id": sid,
                    "sampled_at": now,
                    "stable_since": now,
                    "fail_streak": prev_fail + 1,
                }
                return
            if prev_ready and prev_fail + 1 < _SCENE_GATE_FAIL_STREAK:
                # 吸收瞬时读数抖动：沿用上一次的就绪状态与稳定窗起点。
                _scene_gate_state[pid] = {
                    "ready": True,
                    "scene_id": prev_sid,
                    "sampled_at": now,
                    "stable_since": stable_since,
                    "fail_streak": prev_fail + 1,
                }
                return
            _scene_gate_state[pid] = {
                "ready": False,
                "scene_id": sid,
                "sampled_at": now,
                "stable_since": now,
                "fail_streak": prev_fail + 1,
            }
            return
        if prev_sid != sid or not prev_ready:
            stable_since = now
        _scene_gate_state[pid] = {
            "ready": True,
            "scene_id": sid,
            "sampled_at": now,
            "stable_since": stable_since,
            "fail_streak": 0,
        }


def is_pid_scene_snapshot_stable(
    pid: int, *, settle_sec: float = _SCENE_GATE_SETTLE_SEC
) -> bool:
    """Return whether the latest UI-thread snapshot belongs to a settled scene."""
    with _scene_gate_lock:
        state = dict(_scene_gate_state.get(int(pid)) or {})
    if not bool(state.get("ready")) or int(state.get("scene_id") or 0) <= 0:
        return False
    age = time.monotonic() - float(state.get("stable_since") or 0.0)
    return age >= max(0.0, float(settle_sec))


def get_pid_scene_snapshot(pid: int, *, max_age_s: float | None = None) -> dict:
    """Return the latest bridge scene-fence sample without probing again."""
    with _scene_gate_lock:
        state = dict(_scene_gate_state.get(int(pid)) or {})
    if not state:
        return {}
    sampled_at = float(state.get("sampled_at") or 0.0)
    age_s = time.monotonic() - sampled_at if sampled_at else float("inf")
    if max_age_s is not None and age_s > max(0.0, float(max_age_s)):
        return {}
    state["age_s"] = age_s
    return state


def _probe_pid_scene_snapshot(pid: int, *, timeout_ms: int = 700) -> bool | None:
    """Probe through an already-loaded bridge; None means no bridge exists."""
    from app.core.xajh_bridge import XajhBridge

    bridge = XajhBridge(int(pid))
    if not bridge.open(quiet=True):
        return None
    try:
        result = bridge.host_snapshot(timeout_ms=max(100, int(timeout_ms)))
        if not result.ok:
            raise SceneTransitionError(
                f"pid={int(pid)} scene snapshot unavailable: "
                f"{result.error or result.note or 'bridge error'}"
            )
        host_present = bool(int(result.ret or 0))
        scene_id = int(getattr(result, "mode", 0) or 0)
        note_pid_scene_snapshot(
            int(pid), host_present=host_present, scene_id=scene_id
        )
        return host_present and scene_id > 0
    finally:
        bridge.close()


def ensure_pid_scene_stable(
    pid: int,
    *,
    settle_sec: float = _SCENE_GATE_SETTLE_SEC,
    timeout_ms: int = 700,
) -> None:
    """Fail closed for CRT when an available bridge reports a scene transition."""
    pid = int(pid)
    now = time.monotonic()
    with _scene_gate_lock:
        state = dict(_scene_gate_state.get(pid) or {})
    sampled_at = float(state.get("sampled_at") or 0.0)
    sample_age = now - sampled_at if sampled_at else float("inf")
    if sample_age <= _SCENE_GATE_PROBE_MAX_AGE_SEC:
        available = bool(state.get("ready")) and int(state.get("scene_id") or 0) > 0
    else:
        available = _probe_pid_scene_snapshot(pid, timeout_ms=timeout_ms)
    # Compatibility for attach/recon paths before the bridge is injected.
    if available is None:
        return
    with _scene_gate_lock:
        state = dict(_scene_gate_state.get(pid) or {})
    sid = int(state.get("scene_id") or 0)
    if not available or not bool(state.get("ready")) or sid <= 0:
        raise SceneTransitionError(
            f"pid={pid} scene transition (host/scene unavailable); refusing CRT"
        )
    if not is_pid_scene_snapshot_stable(pid, settle_sec=settle_sec):
        age = time.monotonic() - float(state.get("stable_since") or 0.0)
        need = max(0.0, float(settle_sec))
        raise SceneTransitionError(
            f"pid={pid} scene={sid} settling {age:.2f}/{need:.2f}s; refusing CRT"
        )


def wait_pid_scene_stable(
    pid: int,
    *,
    timeout_s: float = 4.0,
    settle_sec: float = _SCENE_GATE_SETTLE_SEC,
) -> None:
    """Wait for the UI-thread scene fence, used before a batch of raw writes."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    last: BaseException | None = None
    while True:
        try:
            ensure_pid_scene_stable(int(pid), settle_sec=settle_sec)
            return
        except SceneTransitionError as exc:
            last = exc
        if time.monotonic() >= deadline:
            raise SceneTransitionError(str(last or f"pid={int(pid)} scene not stable"))
        time.sleep(0.2)



def wait_scene_ready(
    session,
    *,
    expected_scene: int = 0,
    timeout_s: float = 30.0,
    previous_pos=None,
    require_position_change: bool = False,
    log=None,
) -> dict:
    """Wait until the scheduler/direct host coordinate reports a usable scene.

    This is the shared business-level map-readiness gate. The scheduler's
    LiveSceneSnapshot is preferred; standalone tools fall back to the direct
    scene-position reader. When a route caused a map change, ``previous_pos``
    plus ``require_position_change`` prevents stale frozen coordinates from
    being treated as the new map. Use ``wait_pid_scene_stable`` separately
    when only the low-level CRT safety fence is needed.

    Returns a dict with ``ok``, ``scene_id``, ``pos``, ``source``,
    ``coord_changed`` and ``error`` keys.
    """
    import math

    logger = log or (lambda _message: None)
    pid = int(getattr(session, "pid", session) or 0)
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    baseline = None
    if previous_pos is not None:
        try:
            baseline = tuple(float(value) for value in previous_pos[:3])
        except Exception:
            baseline = None
    warned_fallback = False
    while time.monotonic() < deadline:
        live = None
        try:
            from app.core.live_scene_hub import get_live_scene

            live = get_live_scene(pid, max_age_s=5.0)
        except Exception:
            live = None
        source = "scheduler"
        scene_id = int(getattr(live, "scene_id", 0) or 0) if live is not None else 0
        pos = getattr(live, "pos", None) if live is not None else None
        pos_ok = bool(
            pos
            and len(pos) >= 3
            and all(math.isfinite(float(value)) and abs(float(value)) < 1.0e7 for value in pos[:3])
        )
        state = None
        if live is None or not pos_ok:
            source = "direct"
            try:
                from app.core.automove import read_scene_position

                state = read_scene_position(session, log=lambda _message: None, timeout_ms=5000)
                scene_id = int(state.scene_id or 0) if state.ok else 0
                pos = state.scene_pos if state.ok else None
                pos_ok = bool(
                    pos
                    and len(pos) >= 3
                    and all(math.isfinite(float(value)) and abs(float(value)) < 1.0e7 for value in pos[:3])
                )
                if not warned_fallback:
                    logger("scene ready: scheduler live snapshot unavailable; using direct coordinates")
                    warned_fallback = True
            except Exception:
                pos_ok = False
        changed = bool(
            pos_ok
            and baseline is not None
            and any(abs(float(pos[index]) - baseline[index]) > 0.01 for index in range(3))
        )
        if (
            scene_id > 0
            and (not expected_scene or scene_id == int(expected_scene))
            and pos_ok
            and (not require_position_change or changed)
        ):
            if state is None:
                try:
                    from app.core.automove import read_scene_position

                    state = read_scene_position(session, log=lambda _message: None, timeout_ms=5000)
                except Exception:
                    state = None
            if state is None or (
                getattr(state, "ok", False)
                and getattr(state, "scene_pos", None)
                and (not expected_scene or int(state.scene_id or 0) == int(expected_scene))
            ):
                logger(f"scene ready scene={scene_id} source={source} coord_changed={changed}")
                return {"ok": True, "scene_id": scene_id, "pos": tuple(pos[:3]), "source": source, "coord_changed": changed}
        time.sleep(0.2)
    return {"ok": False, "scene_id": scene_id, "source": source, "coord_changed": False, "error": f"scene={int(expected_scene or 0)} not ready"}


def mark_pid_remote_hung(pid: int, *, cooldown_sec: float | None = None) -> None:
    """Block further remote CRT into this game pid for a short cooldown."""
    import time

    sec = float(_REMOTE_HUNG_COOLDOWN_SEC if cooldown_sec is None else cooldown_sec)
    _remote_hung_until[int(pid)] = time.monotonic() + max(0.0, sec)


def mark_pid_hard_dead(pid: int) -> None:
    """Sticky block: process denied CRT/alloc (err=5) or is gone."""
    _remote_hard_dead.add(int(pid))
    # also arm short hung so concurrent waiters bail
    mark_pid_remote_hung(int(pid), cooldown_sec=5.0)


def clear_pid_remote_state(pid: int) -> None:
    """Clear hung/hard_dead after re-inject or confirmed alive."""
    pid = int(pid)
    _remote_hard_dead.discard(pid)
    _remote_hung_until.pop(pid, None)
    with _scene_gate_lock:
        _scene_gate_state.pop(pid, None)


def is_pid_hard_dead(pid: int) -> bool:
    return int(pid) in _remote_hard_dead


def is_pid_remote_hung(pid: int) -> bool:
    import time

    until = _remote_hung_until.get(int(pid))
    if until is None:
        return False
    return time.monotonic() < until


def is_pid_remote_blocked(pid: int) -> bool:
    return is_pid_hard_dead(pid) or is_pid_remote_hung(pid)


def note_remote_os_error(pid: int, exc: BaseException) -> None:
    """Classify OSError/Timeout from CRT path into hung/hard_dead."""
    msg = str(exc).lower()
    if (
        "err=5" in msg
        or "access is denied" in msg
        or "virtualallocex failed" in msg
        or "openprocess failed" in msg
        or ("createremotethread failed" in msg and "err=5" in msg)
    ):
        mark_pid_hard_dead(int(pid))
        try:
            from app.core import diag_log

            diag_log.error(
                f"pid={int(pid)} hard_dead: {exc}",
                tag="REMOTE",
            )
        except Exception:
            pass


def ensure_pid_remote_callable(pid: int) -> None:
    """Raise if this game pid is hard_dead or in hung cooldown."""
    import time

    pid = int(pid)
    if pid in _remote_hard_dead:
        raise TimeoutError(
            f"pid={pid} hard_dead (previous CRT/alloc denied; refusing new CRT)"
        )
    until = _remote_hung_until.get(pid)
    if until is None:
        return
    now = time.monotonic()
    if now < until:
        remain = until - now
        raise TimeoutError(
            f"pid={pid} remote hung cooldown remain={remain:.1f}s "
            f"(previous CreateRemoteThread did not finish; refusing new CRT)"
        )
    _remote_hung_until.pop(pid, None)


def wait_remote_thread_safely(
    thread: int,
    timeout_ms: int,
    *,
    operation: str = "remote call",
    grace_ms: int | None = None,
    pid: int | None = None,
) -> bool:
    """
    Wait for a remote thread without ever freeing code while it may execute.

    Returns True if the remote thread finished.

    Returns False if still running after diagnostic timeout + grace. Callers
    MUST NOT VirtualFreeEx the stub/code page in that case — leave the page
    leaked. Closing the thread handle is OK; it does not kill the thread.

    Infinite wait was intentionally removed: blocking the helper forever made
    concurrent UI/scripts stack more remote calls into an already-stuck game.
    """
    wait = int(
        kernel32.WaitForSingleObject(
            wintypes.HANDLE(thread), max(1, int(timeout_ms))
        )
    )
    if wait == WAIT_OBJECT_0:
        return True
    if wait == WAIT_TIMEOUT:
        try:
            from app.core import diag_log

            diag_log.error(
                f"{operation} exceeded {int(timeout_ms)}ms; "
                "grace-waiting before abandon (will NOT free remote page if still running)",
                tag="REMOTE",
            )
        except Exception:
            pass
        grace = int(_REMOTE_WAIT_GRACE_MS if grace_ms is None else grace_ms)
        if grace > 0:
            wait = int(
                kernel32.WaitForSingleObject(
                    wintypes.HANDLE(thread), max(1, grace)
                )
            )
            if wait == WAIT_OBJECT_0:
                return True
        try:
            from app.core import diag_log

            diag_log.error(
                f"{operation} still running after grace={grace}ms; "
                "abandoning wait, leaking remote page, arming pid cooldown",
                tag="REMOTE",
            )
        except Exception:
            pass
        if pid is not None:
            mark_pid_remote_hung(int(pid))
        return False
    raise OSError(f"{operation} wait status={wait}")


class CallConvention(str, Enum):
    CDECL = "cdecl"
    STDCALL = "stdcall"
    THISCALL = "thiscall"


class ReturnKind(str, Enum):
    I32 = "i32"
    U64 = "u64"
    FLOAT = "float"


def _u32(value: int | float) -> int:
    if isinstance(value, float):
        return struct.unpack("<I", struct.pack("<f", value))[0]
    return int(value) & 0xFFFFFFFF


@contextmanager
def pid_call_mutex(
    pid: int,
    *,
    timeout_ms: int = 7000,
    namespace: str = "Call",
) -> Iterator[None]:
    """Serialize remote and bridge calls across threads and helper processes."""
    name = f"Local\\Xajh{namespace}_{int(pid)}"
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise OSError(f"CreateMutexW failed err={ctypes.get_last_error()}")
    acquired = False
    try:
        wait = int(kernel32.WaitForSingleObject(handle, max(1, int(timeout_ms))))
        if wait not in (WAIT_OBJECT_0, WAIT_ABANDONED):
            if wait == WAIT_TIMEOUT:
                raise TimeoutError(f"pid call mutex timeout pid={pid}")
            raise OSError(f"WaitForSingleObject mutex status={wait}")
        acquired = True
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


def open_process(pid: int) -> int:
    access = (
        PROCESS_CREATE_THREAD
        | PROCESS_QUERY_INFORMATION
        | PROCESS_VM_OPERATION
        | PROCESS_VM_WRITE
        | PROCESS_VM_READ
    )
    handle = kernel32.OpenProcess(access, False, int(pid))
    if not handle:
        raise OSError(f"OpenProcess failed err={ctypes.get_last_error()}")
    return int(handle)


def write_process(handle: int, address: int, data: bytes) -> None:
    payload = bytes(data)
    count = ctypes.c_size_t(0)
    buf = (ctypes.c_char * len(payload)).from_buffer_copy(payload)
    ok = kernel32.WriteProcessMemory(
        wintypes.HANDLE(handle),
        ctypes.c_void_p(int(address)),
        buf,
        len(payload),
        ctypes.byref(count),
    )
    if not ok or count.value != len(payload):
        raise OSError(
            f"WriteProcessMemory failed err={ctypes.get_last_error()} n={count.value}"
        )


def read_process(handle: int, address: int, size: int) -> bytes:
    buf = (ctypes.c_char * int(size))()
    count = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(
        wintypes.HANDLE(handle),
        ctypes.c_void_p(int(address)),
        buf,
        int(size),
        ctypes.byref(count),
    )
    if not ok:
        raise OSError(f"ReadProcessMemory failed err={ctypes.get_last_error()}")
    return bytes(buf[: count.value])


def remote_alloc(handle: int, size: int) -> int:
    address = int(
        kernel32.VirtualAllocEx(
            wintypes.HANDLE(handle),
            None,
            int(size),
            MEM_COMMIT | MEM_RESERVE,
            PAGE_EXECUTE_READWRITE,
        )
        or 0
    )
    if not address:
        raise OSError(f"VirtualAllocEx failed err={ctypes.get_last_error()}")
    return address


def remote_free(handle: int, address: int) -> None:
    """Release a remote allocation unless this pid has a live timed-out CRT.

    A caller may allocate an argument block separately from ``remote_call_x86``.
    When that call times out, its thread is deliberately left running; freeing
    any such argument block would turn the timeout into a use-after-free in the
    target process. Leaking the allocation until process exit is intentional
    and matches the call-stub timeout contract.
    """
    if address:
        try:
            pid = int(kernel32.GetProcessId(wintypes.HANDLE(handle)) or 0)
            if pid > 0 and is_pid_remote_hung(pid):
                return
        except Exception:
            # Keep the established cleanup path when the handle cannot be
            # queried; remote_call_x86 still owns its own stub safety.
            pass
        kernel32.VirtualFreeEx(
            wintypes.HANDLE(handle), ctypes.c_void_p(int(address)), 0, MEM_RELEASE
        )


def build_call_stub(
    func_va: int,
    args: Sequence[int | float],
    *,
    ret_addr: int,
    convention: CallConvention = CallConvention.CDECL,
    this_ptr: int | None = None,
    return_kind: ReturnKind = ReturnKind.I32,
    caller_cleanup: bool | None = None,
) -> bytes:
    """Build a deterministic x86 call stub; no process access is performed."""
    if not func_va:
        raise ValueError("func_va is 0")
    if convention is CallConvention.THISCALL and not this_ptr:
        raise ValueError("thiscall requires this_ptr")
    code = bytearray()
    for arg in reversed(tuple(args)):
        code += b"\x68" + struct.pack("<I", _u32(arg))
    if convention is CallConvention.THISCALL:
        code += b"\xB9" + struct.pack("<I", _u32(int(this_ptr or 0)))
    code += b"\xB8" + struct.pack("<I", _u32(func_va))
    code += b"\xFF\xD0"
    if return_kind is ReturnKind.FLOAT:
        code += b"\xD9\x1D" + struct.pack("<I", _u32(ret_addr))
    else:
        code += b"\xA3" + struct.pack("<I", _u32(ret_addr))
        if return_kind is ReturnKind.U64:
            code += b"\x89\x15" + struct.pack("<I", _u32(ret_addr + 4))
    cleanup = convention is CallConvention.CDECL if caller_cleanup is None else caller_cleanup
    if cleanup and args:
        size = 4 * len(args)
        if size <= 0x7F:
            code += b"\x83\xC4" + bytes([size])
        else:
            code += b"\x81\xC4" + struct.pack("<I", size)
    code += b"\xC3"
    return bytes(code)


def remote_call_x86(
    pid: int,
    func_va: int,
    args: Sequence[int | float],
    *,
    convention: CallConvention = CallConvention.CDECL,
    this_ptr: int | None = None,
    return_kind: ReturnKind = ReturnKind.I32,
    caller_cleanup: bool | None = None,
    timeout_ms: int = 5000,
    skip_scene_gate: bool = False,
) -> int | float:
    """Execute one serialized x86 call and capture EAX, EDX:EAX, or ST0.

    ``skip_scene_gate=True`` bypasses the scene-stability fence. Use ONLY for
    pure UI queries that never touch the world (login stage: GetGameUIDlg /
    IsDlgShow / GetGameState), where no scene exists yet.
    """
    ensure_pid_remote_callable(pid)
    if not skip_scene_gate:
        ensure_pid_scene_stable(pid)
    with pid_call_mutex(pid, timeout_ms=timeout_ms + 2000):
        # Re-check both health and scene under the lock. A queued caller may
        # have passed the outer fence before another long call or a map load.
        ensure_pid_remote_callable(pid)
        if not skip_scene_gate:
            ensure_pid_scene_stable(pid)
        try:
            handle = open_process(pid)
        except OSError as e:
            note_remote_os_error(pid, e)
            raise
        remote = thread = 0
        completed = False
        try:
            try:
                remote = remote_alloc(handle, 256 + 8 * len(args))
            except OSError as e:
                note_remote_os_error(pid, e)
                raise
            ret_addr = remote
            code_addr = remote + 16
            stub = build_call_stub(
                func_va,
                args,
                ret_addr=ret_addr,
                convention=convention,
                this_ptr=this_ptr,
                return_kind=return_kind,
                caller_cleanup=caller_cleanup,
            )
            write_process(handle, ret_addr, b"\x00" * 8)
            write_process(handle, code_addr, stub)
            tid = wintypes.DWORD(0)
            thread = int(
                kernel32.CreateRemoteThread(
                    wintypes.HANDLE(handle),
                    None,
                    0,
                    ctypes.c_void_p(code_addr),
                    None,
                    0,
                    ctypes.byref(tid),
                )
                or 0
            )
            if not thread:
                err = OSError(
                    f"CreateRemoteThread failed err={ctypes.get_last_error()}"
                )
                note_remote_os_error(pid, err)
                raise err
            completed = wait_remote_thread_safely(
                thread,
                timeout_ms,
                operation=f"remote call pid={pid} va=0x{int(func_va):X}",
                pid=pid,
            )
            if not completed:
                # Arm cooldown immediately so other features stop stacking CRT.
                # Only mark hung here (not hard_dead): a single GetObjectName
                # timeout must not sticky-kill the whole session path.
                try:
                    mark_pid_remote_hung(int(pid), cooldown_sec=8.0)
                except Exception:
                    pass
                raise TimeoutError(
                    f"remote call pid={pid} va=0x{int(func_va):X} hung; "
                    "remote page intentionally leaked"
                )
            raw = read_process(handle, ret_addr, 8)
            if return_kind is ReturnKind.FLOAT:
                return float(struct.unpack_from("<f", raw)[0])
            if return_kind is ReturnKind.U64:
                lo, hi = struct.unpack_from("<II", raw)
                return (int(hi) << 32) | int(lo)
            return int(struct.unpack_from("<i", raw)[0])
        finally:
            if thread:
                kernel32.CloseHandle(thread)
            # Never free a still-running thread's code page.
            if remote and completed:
                remote_free(handle, remote)
            kernel32.CloseHandle(handle)


def remote_call_cdecl_x86(
    pid: int,
    func_va: int,
    args: Sequence[int | float],
    *,
    timeout_ms: int = 5000,
    skip_scene_gate: bool = False,
) -> int:
    return int(
        remote_call_x86(
            pid,
            func_va,
            args,
            timeout_ms=timeout_ms,
            skip_scene_gate=skip_scene_gate,
        )
    )


def remote_call_thiscall_x86(
    pid: int,
    func_va: int,
    this_ptr: int,
    args: Sequence[int | float] | None = None,
    *,
    caller_cleanup: bool = False,
    timeout_ms: int = 5000,
    skip_scene_gate: bool = False,
) -> int:
    return int(
        remote_call_x86(
            pid,
            func_va,
            args or (),
            convention=CallConvention.THISCALL,
            this_ptr=this_ptr,
            caller_cleanup=caller_cleanup,
            timeout_ms=timeout_ms,
            skip_scene_gate=skip_scene_gate,
        )
    )


def remote_call_stdcall_x86(
    pid: int,
    func_va: int,
    args: Sequence[int | float],
    *,
    timeout_ms: int = 5000,
    skip_scene_gate: bool = False,
) -> int:
    return int(
        remote_call_x86(
            pid,
            func_va,
            args,
            convention=CallConvention.STDCALL,
            timeout_ms=timeout_ms,
            skip_scene_gate=skip_scene_gate,
        )
    )


def remote_call_cdecl_x86_ret64(
    pid: int, func_va: int, args: Sequence[int | float], *, timeout_ms: int = 5000
) -> int:
    return int(
        remote_call_x86(
            pid, func_va, args, return_kind=ReturnKind.U64, timeout_ms=timeout_ms
        )
    )


def remote_call_stdcall_x86_ret64(
    pid: int, func_va: int, args: Sequence[int | float], *, timeout_ms: int = 5000
) -> int:
    return int(
        remote_call_x86(
            pid,
            func_va,
            args,
            convention=CallConvention.STDCALL,
            return_kind=ReturnKind.U64,
            timeout_ms=timeout_ms,
        )
    )


def remote_call_float_cdecl(
    pid: int, func_va: int, args: Sequence[int | float], *, timeout_ms: int = 5000
) -> float:
    return float(
        remote_call_x86(
            pid, func_va, args, return_kind=ReturnKind.FLOAT, timeout_ms=timeout_ms
        )
    )


def remote_read_bytes(pid: int, address: int, size: int) -> bytes:
    if not address or size <= 0:
        return b""
    handle = open_process(pid)
    try:
        return read_process(handle, address, size)
    finally:
        kernel32.CloseHandle(handle)


def remote_write_bytes(pid: int, address: int, data: bytes) -> int:
    if not address or not data:
        return 0
    ensure_pid_remote_callable(pid)
    ensure_pid_scene_stable(pid)
    with pid_call_mutex(pid):
        ensure_pid_remote_callable(pid)
        ensure_pid_scene_stable(pid)
        handle = open_process(pid)
        try:
            write_process(handle, address, data)
            return len(data)
        finally:
            kernel32.CloseHandle(handle)



def remote_module_base(pid: int, module_name: str) -> int:
    """Return base address of a loaded module in pid (Toolhelp32). @author by ak"""
    TH32CS_SNAPMODULE = 0x00000008
    TH32CS_SNAPMODULE32 = 0x00000010

    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("th32ModuleID", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("GlsUsageCount", wintypes.DWORD),
            ("ProccntUsage", wintypes.DWORD),
            ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
            ("modBaseSize", wintypes.DWORD),
            ("hModule", wintypes.HMODULE),
            ("szModule", wintypes.WCHAR * 256),
            ("szExePath", wintypes.WCHAR * 260),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, int(pid)
    )
    if not snap or int(ctypes.c_void_p(snap).value or 0) in (0, 0xFFFFFFFF):
        raise OSError(f"CreateToolhelp32Snapshot failed err={ctypes.get_last_error()}")
    try:
        me = MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(MODULEENTRY32W)
        if not kernel32.Module32FirstW(snap, ctypes.byref(me)):
            raise LookupError(f"no modules for pid={pid}")
        want = str(module_name or "").lower()
        while True:
            name = (me.szModule or "").lower()
            if name == want:
                base = ctypes.cast(me.modBaseAddr, ctypes.c_void_p).value or 0
                if base:
                    return int(base) & 0xFFFFFFFF
            if not kernel32.Module32NextW(snap, ctypes.byref(me)):
                break
        raise LookupError(f"module {module_name!r} not found in pid={pid}")
    finally:
        kernel32.CloseHandle(snap)


def remote_export_va(pid: int, module_name: str, export_name: str) -> int:
    """
    Resolve an export VA inside a remote process by parsing its PE export table.
    @author by ak
    """
    handle = open_process(pid)
    try:
        base = int(remote_module_base(pid, module_name)) & 0xFFFFFFFF
        dos = read_process(handle, base, 0x40)
        if len(dos) < 0x40 or dos[:2] != b"MZ":
            raise RuntimeError("bad DOS header")
        e_lfanew = struct.unpack_from("<I", dos, 0x3C)[0]
        nt = read_process(handle, base + e_lfanew, 0xF8)
        if len(nt) < 0x7C or nt[:4] != b"PE\x00\x00":
            raise RuntimeError("bad NT header")
        magic = struct.unpack_from("<H", nt, 0x18)[0]
        if magic != 0x10B:
            raise RuntimeError(f"not PE32 magic={magic:#x}")
        export_rva = struct.unpack_from("<I", nt, 0x78)[0]
        if not export_rva:
            raise LookupError(f"{module_name} has no export directory")
        exp = read_process(handle, base + export_rva, 0x40)
        n_names = struct.unpack_from("<I", exp, 0x18)[0]
        n_funcs = struct.unpack_from("<I", exp, 0x14)[0]
        addr_funcs = struct.unpack_from("<I", exp, 0x1C)[0]
        addr_names = struct.unpack_from("<I", exp, 0x20)[0]
        addr_ords = struct.unpack_from("<I", exp, 0x24)[0]
        names = read_process(handle, base + addr_names, 4 * int(n_names))
        ords = read_process(handle, base + addr_ords, 2 * int(n_names))
        funcs = read_process(handle, base + addr_funcs, 4 * int(n_funcs))
        want = export_name.encode("ascii")
        for i in range(int(n_names)):
            name_rva = struct.unpack_from("<I", names, i * 4)[0]
            raw = read_process(handle, base + name_rva, 96)
            z = raw.find(b"\x00")
            if z < 0 or raw[:z] != want:
                continue
            ord_i = struct.unpack_from("<H", ords, i * 2)[0]
            func_rva = struct.unpack_from("<I", funcs, ord_i * 4)[0]
            return int(base + func_rva) & 0xFFFFFFFF
        raise LookupError(f"export {export_name} not in {module_name}")
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))



# Compatibility aliases used by older modules.
_open_process = open_process
_rpm = read_process
_wpm = write_process
