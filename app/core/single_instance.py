# -*- coding: utf-8 -*-
"""
Process single-instance guard (Windows named mutex).

Scopes (can run side-by-side):
  - source : unfrozen / python source tree
  - dev    : frozen package channel=dev
  - prod   : frozen package channel=prod

Same scope remains single-instance. Different scopes may coexist so local
debugging does not kick out the packaged helper already in use.

Inject ownership: only the highest-priority live scope controls Delete /
auto-inject (prod > dev > source), unless XAJH_ALLOW_PARALLEL_INJECT=1.

@author by ak
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ERROR_ALREADY_EXISTS = 183
MUTEX_ALL_ACCESS = 0x1F0001
SYNCHRONIZE = 0x00100000

# Legacy global name (pre-scope). Kept for peer detection / migration only.
MUTEX_NAME_LEGACY = "Local\\XAJH_GameGet_GUI_SingleInstance"
MUTEX_NAME_PREFIX = "Local\\XAJH_GameGet_GUI_SingleInstance_"

SCOPE_SOURCE = "source"
SCOPE_DEV = "dev"
SCOPE_PROD = "prod"
ALL_SCOPES = (SCOPE_PROD, SCOPE_DEV, SCOPE_SOURCE)

# Higher number wins Delete / inject when multiple scopes are alive.
SCOPE_PRIORITY: dict[str, int] = {
    SCOPE_SOURCE: 10,
    SCOPE_DEV: 20,
    SCOPE_PROD: 30,
}

SCOPE_LABELS_ZH: dict[str, str] = {
    SCOPE_SOURCE: "源码开发",
    SCOPE_DEV: "测试包",
    SCOPE_PROD: "正式包",
}

# Back-compat default: resolved at call time via mutex_name_for_scope().
MUTEX_NAME_DEFAULT = MUTEX_NAME_PREFIX + SCOPE_PROD

kernel32.CreateMutexW.argtypes = [
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
kernel32.ReleaseMutex.restype = wintypes.BOOL
kernel32.SetLastError.argtypes = [wintypes.DWORD]
kernel32.SetLastError.restype = None


@dataclass
class SingleInstanceLock:
    """
    Holds a named mutex for the process lifetime.

    Call release() on exit (optional; OS frees on process end).
    @author by ak
    """

    name: str
    handle: int
    owned: bool
    scope: str = SCOPE_PROD
    # Optional second handle (prod also holds legacy global mutex).
    extra_name: str = ""
    extra_handle: int = 0

    def release(self) -> None:
        """Release and close the mutex handle(s). @author by ak"""
        for h, owned_flag in (
            (self.extra_handle, True),
            (self.handle, self.owned),
        ):
            if not h:
                continue
            try:
                if owned_flag:
                    kernel32.ReleaseMutex(wintypes.HANDLE(h))
            except Exception:
                pass
            try:
                kernel32.CloseHandle(wintypes.HANDLE(h))
            except Exception:
                pass
        self.handle = 0
        self.extra_handle = 0
        self.owned = False


def instance_scope() -> str:
    """
    Runtime instance scope for single-instance + inject ownership.

    - source: unfrozen (python / IDE)
    - dev: frozen test package
    - prod: frozen production package

    Env XAJH_INSTANCE_SCOPE can force source|dev|prod (tests / special runs).
    @author by ak
    """
    forced = (os.environ.get("XAJH_INSTANCE_SCOPE") or "").strip().lower()
    if forced in (SCOPE_SOURCE, SCOPE_DEV, SCOPE_PROD):
        return forced
    try:
        from app.core.build_profile import channel, is_frozen

        if not is_frozen():
            return SCOPE_SOURCE
        ch = str(channel() or "prod").strip().lower()
        if ch in ("dev", "test"):
            return SCOPE_DEV
        return SCOPE_PROD
    except Exception:
        if bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS"):
            return SCOPE_PROD
        return SCOPE_SOURCE


def scope_label(scope: str | None = None) -> str:
    """Chinese label for scope. @author by ak"""
    s = (scope or instance_scope()).strip().lower()
    return SCOPE_LABELS_ZH.get(s, s or "未知")


def mutex_name_for_scope(scope: str | None = None) -> str:
    """Named mutex for one scope. @author by ak"""
    s = (scope or instance_scope()).strip().lower() or SCOPE_PROD
    if s not in SCOPE_PRIORITY:
        s = SCOPE_PROD
    return f"{MUTEX_NAME_PREFIX}{s}"


def is_mutex_held(name: str) -> bool:
    """
    True when the named mutex already exists (another process holds it).

    @author by ak
    """
    name = str(name or "").strip()
    if not name:
        return False
    existing = kernel32.OpenMutexW(SYNCHRONIZE, False, name)
    if not existing:
        return False
    try:
        kernel32.CloseHandle(existing)
    except Exception:
        pass
    return True


def is_scope_running(scope: str) -> bool:
    """True if another process holds this scope's single-instance mutex. @author by ak"""
    return is_mutex_held(mutex_name_for_scope(scope))


def alive_peer_scopes(*, exclude_self: bool = True) -> list[str]:
    """
    Scopes that currently hold a single-instance mutex.

    When exclude_self=True, drops this process scope (caller already owns it).
    @author by ak
    """
    me = instance_scope() if exclude_self else ""
    out: list[str] = []
    for s in ALL_SCOPES:
        if exclude_self and s == me:
            continue
        if is_scope_running(s):
            out.append(s)
    # Legacy single mutex (old builds): treat as prod peer if present.
    if is_mutex_held(MUTEX_NAME_LEGACY):
        if SCOPE_PROD not in out and (not exclude_self or me != SCOPE_PROD):
            out.append(SCOPE_PROD)
    return out


def parallel_inject_allowed_by_env() -> bool:
    """
    Env override: allow this process to inject even when a higher-priority peer runs.

    XAJH_ALLOW_PARALLEL_INJECT=1|true|yes
    @author by ak
    """
    v = (os.environ.get("XAJH_ALLOW_PARALLEL_INJECT") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def is_inject_controller() -> bool:
    """
    True when this process should own Delete / auto-inject.

    Highest-priority live scope wins (prod > dev > source). Own scope is
    assumed running; peers are detected via their mutexes.
    @author by ak
    """
    if parallel_inject_allowed_by_env():
        return True
    me = instance_scope()
    my_p = int(SCOPE_PRIORITY.get(me, 0))
    for peer in alive_peer_scopes(exclude_self=True):
        if int(SCOPE_PRIORITY.get(peer, 0)) > my_p:
            return False
    return True


def inject_block_reason() -> str:
    """
    Empty if inject allowed; else human-readable why this scope must not inject.

    @author by ak
    """
    if is_inject_controller():
        return ""
    me = instance_scope()
    my_p = int(SCOPE_PRIORITY.get(me, 0))
    higher: list[str] = []
    for peer in alive_peer_scopes(exclude_self=True):
        if int(SCOPE_PRIORITY.get(peer, 0)) > my_p:
            higher.append(scope_label(peer))
    if not higher:
        return ""
    peers = "、".join(higher)
    return (
        f"当前为「{scope_label(me)}」，更高优先级的「{peers}」已在运行；"
        f"已禁用 Delete / 一键注入，避免干扰正在使用的功能。"
        f"需要并行注入时设置环境变量 XAJH_ALLOW_PARALLEL_INJECT=1。"
    )


def _create_owned_mutex(name: str) -> int | None:
    """
    Create+own named mutex. Returns handle or None if already exists / failed.

    @author by ak
    """
    name = str(name or "").strip()
    if not name:
        return None
    existing = kernel32.OpenMutexW(SYNCHRONIZE, False, name)
    if existing:
        kernel32.CloseHandle(existing)
        return None
    kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        return None
    err = ctypes.get_last_error()
    if err == ERROR_ALREADY_EXISTS:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass
        return None
    return int(handle)


def try_acquire_single_instance(
    name: str | None = None,
) -> SingleInstanceLock | None:
    """
    Try to become the sole running instance for this scope.

    name=None → mutex for current instance_scope() (source/dev/prod separated).
    Returns a lock on success, None if another instance already holds the mutex.

    Prod also holds the legacy unscoped mutex so old builds still conflict with
    the new prod package (but source/dev can sit beside either).
    @author by ak
    """
    scope = instance_scope()
    if name is None or not str(name).strip():
        name = mutex_name_for_scope(scope)
        explicit = False
    else:
        name = str(name).strip()
        explicit = True

    handle = _create_owned_mutex(name)
    if handle is None:
        return None

    extra_name = ""
    extra_handle = 0
    # Only auto dual-hold when using default scoped naming.
    if (not explicit) and scope == SCOPE_PROD:
        extra_name = MUTEX_NAME_LEGACY
        extra_handle_i = _create_owned_mutex(extra_name)
        if extra_handle_i is None:
            # Old package (or another new prod that took legacy) is running.
            try:
                kernel32.ReleaseMutex(wintypes.HANDLE(handle))
            except Exception:
                pass
            try:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
            except Exception:
                pass
            return None
        extra_handle = int(extra_handle_i)

    return SingleInstanceLock(
        name=name,
        handle=int(handle),
        owned=True,
        scope=scope,
        extra_name=extra_name,
        extra_handle=int(extra_handle or 0),
    )
