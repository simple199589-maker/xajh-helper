# -*- coding: utf-8 -*-
"""
Hotkey-driven inject gate for the workbench.

Flow:
  1) GUI starts on welcome page only
  2) User focuses game and presses Delete
  3) Wait until the game client is startup-ready (avoid early crash)
  4) Find xajh.exe, inject bridge, ping, attach recon
  5) Unlock workbench tabs

Early inject during splash/login bootstrap can kill xajh.exe. Delete /
one-click therefore delay LoadLibrary until the main window is visible,
responsive, and past a short process-age floor.

@author by ak
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

LogFn = Callable[[str], None]
PhaseFn = Callable[[str, str], None]
ResultFn = Callable[["InjectOutcome"], None]

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

# Fresh clients still map modules / spin login UI; CreateRemoteThread here
# is a known flash-exit vector. Prefer waiting over force inject.
MIN_INJECT_PROCESS_AGE_S = 8.0
# Max time Delete / 一键登录 will wait for readiness before aborting.
INJECT_READY_WAIT_S = 45.0
# 一键登录批量：单实例就绪等待上限（部分未进角色/启动慢不堵死队列）。
INJECT_BATCH_READY_WAIT_S = 30.0
# Stable responsive window: two consecutive OK checks.
INJECT_READY_STABLE_CHECKS = 2
INJECT_READY_POLL_S = 0.45
# Login / main client window should already have a usable client area.
MIN_INJECT_CLIENT_W = 100
MIN_INJECT_CLIENT_H = 80


@dataclass
class InjectOutcome:
    ok: bool
    pid: int = 0
    hwnd: int = 0
    title: str = ""
    error: str | None = None
    bridge_note: str = ""
    ping_ret: int | None = None
    fail_code: str = ""
    reused: bool = False
    did_inject: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "pid": self.pid,
            "hwnd": self.hwnd,
            "title": self.title,
            "error": self.error,
            "bridge_note": self.bridge_note,
            "ping_ret": self.ping_ret,
            "fail_code": self.fail_code,
            "reused": self.reused,
            "did_inject": self.did_inject,
        }


def _verify_bridge_host_context(
    bridge,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
) -> tuple[bool, object | None]:
    """Return true only when the bridge proves a live role on the UI thread."""
    log = log or (lambda _m: None)
    try:
        result = bridge.host_context(hwnd=int(hwnd or 0) or None, timeout_ms=1500)
    except Exception as e:
        log(f"host_context verify raised: {e}")
        return False, None
    ok = bool(result.ok and int(result.ret or 0) != 0)
    log(
        "host_context verify "
        f"ok={getattr(result, 'ok', False)} ret={getattr(result, 'ret', None)} "
        f"err={getattr(result, 'error', None)!r} "
        f"note={getattr(result, 'note', '')!r}"
    )
    return ok, result


# Fast-transient failures worth one automatic retry in a 一键登录 batch.
# Long-running failures (not-ready wait / hard timeout / crash / permission)
# are recorded and the queue moves on so one stuck instance never blocks the rest.
BATCH_RETRYABLE_FAIL_CODES = frozenset(
    {
        "BRIDGE_PING_TIMEOUT",
        "PING_TIMEOUT_NO_HOOK",
        "HOOK_TIMEOUT",
        "HOOK_NO_HWND",
        "HOOK_SETWNDPROC_FAIL",
        "HOOK_SETTIMER_FAIL",
        "HOOK_NOT_INSTALLED",
        "SHM_NOT_OPEN",
        "POSTMESSAGE_FAIL",
        "ATTACH_SEH",
        "UNKNOWN",
    }
)


def is_batch_retryable(fail_code: str) -> bool:
    """True when a one-click batch failure is a fast transient worth one retry.

    PING/TIMEOUT families retry; the long ready-wait, hard 90s ceiling,
    crash and permission failures are terminal so the queue continues.

    @author by ak
    """
    code_u = str(fail_code or "").upper()
    if code_u in BATCH_RETRYABLE_FAIL_CODES:
        return True
    if code_u in ("INJECT_TIMEOUT", "GAME_NOT_READY", "GAME_CRASH", "GAME_DEAD"):
        return False
    if code_u.startswith("OPENPROCESS_") or "RESTART_GAME" in code_u:
        return False
    if "PING" in code_u or "TIMEOUT" in code_u:
        return True
    return False


def batch_failure_category(fail_code: str, error: str = "") -> str:
    """Human-readable 一键登录 failure category for summary logs. @author by ak"""
    code_u = str(fail_code or "").upper()
    if code_u == "GAME_NOT_READY":
        if "host_context" in str(error) or "上下文" in str(error) or "角色" in str(error):
            return "未进角色"
        return "游戏未就绪"
    if code_u == "INJECT_TIMEOUT":
        return "注入超时"
    if code_u in ("GAME_CRASH", "GAME_DEAD"):
        return "游戏崩溃/退出"
    if code_u.startswith("OPENPROCESS_") or code_u in (
        "INJECT_EXE_FAIL",
        "CREATEREMOTETHREAD_FAIL",
        "LOADLIBRARY_FAIL",
    ):
        return "权限/拦截"
    if code_u in ("BRIDGE_MISSING", "NO_GAME", "ARCH_MISMATCH", "DLL_PATH_LONG"):
        return "环境问题"
    if code_u == "GREEN_NO_HWND":
        return "绿端无窗口"
    if code_u in ("BRIDGE_PING_TIMEOUT", "PING_TIMEOUT_NO_HOOK", "HOOK_TIMEOUT"):
        return "桥接超时"
    return "注入失败"


def find_xajh_processes() -> list[dict]:
    """
    Enumerate running xajh.exe processes.
    Returns list of {pid, exe, create_time}.
    @author by ak
    """
    if psutil is None:
        return []
    out: list[dict] = []
    for p in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        try:
            name = (p.info.get("name") or "").lower()
            if name != "xajh.exe":
                continue
            out.append(
                {
                    "pid": int(p.info["pid"]),
                    "exe": p.info.get("exe") or "",
                    "create_time": float(p.info.get("create_time") or 0),
                }
            )
        except Exception:
            continue
    out.sort(key=lambda x: x.get("create_time") or 0, reverse=True)
    return out


def _process_create_time(pid: int) -> float:
    """
    Process create_time epoch seconds, or 0 if unknown.

    @author by ak
    """
    pid = int(pid or 0)
    if pid <= 0 or psutil is None:
        return 0.0
    try:
        return float(psutil.Process(pid).create_time() or 0.0)
    except Exception:
        return 0.0


def _hwnd_belongs_to_pid(hwnd: int, pid: int) -> bool:
    """True when hwnd is a live window owned by pid. @author by ak"""
    hwnd = int(hwnd or 0)
    pid = int(pid or 0)
    if not hwnd or not pid:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.IsWindow(wintypes.HWND(hwnd)):
        return False
    proc = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(proc))
    return int(proc.value or 0) == pid


def _window_client_size(hwnd: int) -> tuple[int, int]:
    """Client area width/height for hwnd, or (0, 0). @author by ak"""
    hwnd = int(hwnd or 0)
    if not hwnd:
        return 0, 0
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    rc = RECT()
    if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rc)):
        return 0, 0
    return max(0, int(rc.right - rc.left)), max(0, int(rc.bottom - rc.top))


def _window_responding(hwnd: int, *, timeout_ms: int = 250) -> bool:
    """
    True if the window message queue answers WM_NULL within timeout.

    Startup / hung login screens often fail this check.
    @author by ak
    """
    hwnd = int(hwnd or 0)
    if not hwnd:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    SMTO_ABORTIFHUNG = 0x0002
    result = wintypes.DWORD(0)
    try:
        ok = user32.SendMessageTimeoutW(
            wintypes.HWND(hwnd),
            0,  # WM_NULL
            0,
            0,
            SMTO_ABORTIFHUNG,
            int(timeout_ms),
            ctypes.byref(result),
        )
        return bool(ok)
    except Exception:
        return False


def resolve_inject_hwnd(
    pid: int,
    preferred_hwnd: int | None = None,
) -> tuple[int, str, str, str]:
    """
    Resolve best game hwnd for inject.

    Returns (hwnd, title, class_name, hwnd_source).
    @author by ak
    """
    pid = int(pid or 0)
    preferred = int(preferred_hwnd or 0)
    hwnd_enum, title, cls = find_main_hwnd_for_pid(pid)
    if preferred and _hwnd_belongs_to_pid(preferred, pid):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not title:
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(wintypes.HWND(preferred), buf, 512)
            title = buf.value or ""
        if not cls:
            cbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(wintypes.HWND(preferred), cbuf, 256)
            cls = cbuf.value or ""
        return preferred, title, cls, "preferred_fg"
    if hwnd_enum:
        return int(hwnd_enum), title, cls, "enum"
    return 0, title, cls, "none"


def assess_game_inject_ready(
    pid: int,
    *,
    hwnd: int = 0,
    create_time: float | None = None,
    min_age_s: float = MIN_INJECT_PROCESS_AGE_S,
) -> tuple[bool, str, int, str]:
    """
    Decide whether LoadLibrary inject is safe enough for this client.

    Returns (ready, reason, hwnd, title). reason is machine-readable when
    not ready, or "ok" when ready.
    @author by ak
    """
    pid = int(pid or 0)
    if pid <= 0:
        return False, "bad_pid", 0, ""

    try:
        from app.core import diag_log

        alive = bool(diag_log.process_alive(pid))
    except Exception:
        alive = True
    if not alive:
        return False, "game_dead", 0, ""

    ct = float(create_time or 0.0)
    if ct <= 0:
        ct = _process_create_time(pid)
    age = (time.time() - ct) if ct > 0 else 9999.0
    if age < float(min_age_s):
        return (
            False,
            f"process_age={age:.1f}s<{float(min_age_s):.1f}s",
            int(hwnd or 0),
            "",
        )

    hwnd_i, title, _cls, _src = resolve_inject_hwnd(pid, hwnd)
    if not hwnd_i:
        return False, "no_hwnd", 0, title or ""

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.IsWindowVisible(wintypes.HWND(hwnd_i)):
        return False, "hwnd_hidden", hwnd_i, title or ""

    w, h = _window_client_size(hwnd_i)
    if w < MIN_INJECT_CLIENT_W or h < MIN_INJECT_CLIENT_H:
        return False, f"client_size={w}x{h}", hwnd_i, title or ""

    if not _window_responding(hwnd_i):
        return False, "hwnd_not_responding", hwnd_i, title or ""

    return True, "ok", hwnd_i, title or ""


def wait_game_inject_ready(
    pid: int,
    *,
    preferred_hwnd: int | None = None,
    create_time: float | None = None,
    timeout_s: float = INJECT_READY_WAIT_S,
    min_age_s: float = MIN_INJECT_PROCESS_AGE_S,
    log: LogFn | None = None,
    on_phase: PhaseFn | None = None,
) -> tuple[bool, str, int, str]:
    """
    Poll until the game client is ready for delayed inject.

    Returns (ready, reason, hwnd, title).
    @author by ak
    """
    log = log or (lambda _m: None)
    phase_cb = on_phase or (lambda _k, _t: None)
    pid = int(pid or 0)
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    stable = 0
    last_reason = "pending"
    hwnd = int(preferred_hwnd or 0)
    title = ""
    logged_wait = False
    first_poll = True

    while True:
        ready, reason, hwnd, title = assess_game_inject_ready(
            pid,
            hwnd=hwnd or int(preferred_hwnd or 0),
            create_time=create_time,
            min_age_s=min_age_s,
        )
        last_reason = reason
        if reason == "game_dead":
            return False, "game_dead", int(hwnd or 0), title or ""
        if ready:
            # Already stable on first look: inject immediately (no extra lag).
            # After a wait, require consecutive OK polls to avoid race inject.
            need = 1 if first_poll and not logged_wait else int(
                INJECT_READY_STABLE_CHECKS
            )
            stable += 1
            if stable >= need:
                if logged_wait:
                    log(
                        f"inject ready pid={pid} hwnd=0x{int(hwnd):X} "
                        f"title={title!r} stable={stable}"
                    )
                return True, "ok", int(hwnd or 0), title or ""
        else:
            stable = 0
            if not logged_wait:
                logged_wait = True
                log(
                    f"delay inject: game not ready pid={pid} reason={reason} "
                    f"(wait up to {float(timeout_s):.0f}s)"
                )
            try:
                phase_cb("wait_ready", f"等待游戏启动就绪… ({reason})")
            except Exception:
                pass
        first_poll = False

        if time.monotonic() >= deadline:
            log(
                f"delay inject timeout pid={pid} last_reason={last_reason} "
                f"hwnd=0x{int(hwnd):X}"
            )
            return False, last_reason or "timeout", int(hwnd or 0), title or ""

        # Dead process: stop early.
        try:
            from app.core import diag_log

            if not diag_log.process_alive(pid):
                return False, "game_dead", int(hwnd or 0), title or ""
        except Exception:
            pass
        time.sleep(float(INJECT_READY_POLL_S))


def find_main_hwnd_for_pid(pid: int) -> tuple[int, str, str]:
    """
    Best-effort main window for pid.

    Score windows so one-click (background) does not latch onto a tiny
    splash/tool window. Prefers XAJHElementClient + large client area +
    in-world style titles.
    Returns (hwnd, title, class_name).
    @author by ak
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[tuple[int, str, str, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lp):
        proc = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc))
        if int(proc.value) != int(pid):
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        title_buf = ctypes.create_unicode_buffer(512)
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title_buf, 512)
        user32.GetClassNameW(hwnd, class_buf, 256)
        title = title_buf.value or ""
        cls = class_buf.value or ""
        w, h = _window_client_size(int(hwnd))
        area = int(w) * int(h)
        found.append((int(hwnd), title, cls, area))
        return True

    user32.EnumWindows(_enum, 0)
    if not found:
        return 0, "", ""

    def _score(item: tuple[int, str, str, int]) -> tuple[int, int]:
        _hwnd, title, cls, area = item
        blob = (title + " " + cls).lower()
        score = 0
        if "xajhelementclient" in blob.replace(" ", ""):
            score += 100
        if "xajh" in blob:
            score += 40
        if "笑傲" in title or "快乐江湖" in title or "绿端" in title:
            score += 30
        # In-world titles usually contain separators / role names.
        if " - " in title or "—" in title:
            score += 20
        if area >= (800 * 600):
            score += 25
        elif area >= (400 * 300):
            score += 15
        elif area >= (MIN_INJECT_CLIENT_W * MIN_INJECT_CLIENT_H):
            score += 5
        else:
            score -= 20
        if _window_responding(int(_hwnd), timeout_ms=120):
            score += 10
        return score, area

    found.sort(key=_score, reverse=True)
    hwnd, title, cls, _area = found[0]
    return int(hwnd), title, cls


def run_delete_inject(
    *,
    preferred_pid: int | None = None,
    preferred_hwnd: int | None = None,
    log: LogFn | None = None,
    on_phase: PhaseFn | None = None,
    on_result: ResultFn | None = None,
    ready_wait_s: float | None = None,
) -> InjectOutcome:
    """
    Inject bridge into xajh and verify with PING.
    Does not open full recon (caller may attach after).
    Writes detailed triage lines via diag_log + optional log callback.
    Always emits a fail_code on error for remote one-shot diagnosis.

    preferred_hwnd: foreground game window from Delete hotkey (critical for
    green_compat — DLL will not EnumWindows).
    ready_wait_s: readiness wait ceiling; 一键登录批量 uses a shorter bound so
    a slow-starting / not-in-role instance does not block the queue for long.
    on_phase: optional (phase_key, user_text) for UI loading feedback.
    on_result: publishes the final outcome before native frame cleanup finishes.
    @author by ak
    """
    from app.core import diag_log

    user_log = log or (lambda _m: None)
    phase_cb = on_phase or (lambda _k, _t: None)
    result_cb = on_result or (lambda _out: None)
    result_published = False

    def publish_result(out: InjectOutcome) -> InjectOutcome:
        nonlocal result_published
        if not result_published:
            result_published = True
            try:
                result_cb(out)
            except Exception:
                pass
        return out

    def phase(key: str, text: str) -> None:
        try:
            phase_cb(key, text)
        except Exception:
            pass

    def log_line(m: str) -> None:
        # Single file write (INJECT tag). user_log is console/UI only to avoid
        # doubling every line via shell._log -> diag_log.info again.
        diag_log.info(m, tag="INJECT")
        try:
            user_log(m)
        except Exception:
            pass

    log = log_line
    # Slim inject triage: full ENV snapshot is already written at login.
    # Per-Delete full snapshot doubled disk I/O and felt multi-minute on slow disks.
    phase("prepare", "准备注入…")
    diag_log.section("DELETE INJECT START")
    diag_log.info(
        f"admin={diag_log.is_admin()} log_file={diag_log.log_path()}",
        tag="INJECT",
    )
    # Light process list only (no heavy integrity/env dump per Delete).
    try:
        diag_log.log_process_list()
    except Exception:
        pass

    from app.core.client_profile import game_root_for_log, profile_from_proc
    from app.core.xajh_bridge import (
        CMD_PING,
        CMD_REHOOK,
        classify_bridge_fail,
        ensure_bridge,
        is_bridge_built,
        last_ensure_bridge_failure,
        last_ensure_meta,
        preflight_inject,
        stage_bridge_for_inject,
    )

    FAIL_MSG = {
        "BRIDGE_MISSING": "未编译桥接: native/bin/xajh_bridge.dll + xajh_inject.exe",
        "NO_GAME": "未找到 xajh.exe，请先启动并登录游戏",
        "GAME_DEAD": "游戏进程不存在",
        "ARCH_MISMATCH": "桥接 DLL 与游戏架构不匹配（需 x86 注入 32 位 xajh）",
        "OPENPROCESS_DENIED": "OpenProcess 被拒绝（请用管理员运行助手，并关闭冲突的安全软件）",
        "DLL_PATH_LONG": "DLL 路径过长（请把助手拷到短路径如 D:\\xajh_helper\\）",
        "INJECT_EXE_FAIL": "注入程序失败（OpenProcess/CreateRemoteThread/LoadLibrary）",
        "CREATEREMOTETHREAD_FAIL": "目标进程拒绝创建远程线程（权限/保护软件/进程状态）",
        "LOADLIBRARY_FAIL": "目标进程加载桥接 DLL 失败（路径/依赖/架构/保护软件）",
        "INJECT_TIMEOUT": "远程加载 DLL 超时（目标进程忙或被保护软件拦截）",
        "LEGACY_BRIDGE_RESTART_GAME": "请升级版本，或者完整退出游戏后重新注入",
        "STALE_BRIDGE_RESTART_GAME": "请升级版本，或者完整退出游戏后重新注入",
        "LOADED_BRIDGE_UNHEALTHY_RESTART_GAME": "请升级版本，或者完整退出游戏后重新注入",
        "GAME_NOT_READY": "游戏登录/启动尚未就绪（已延迟等待后仍不安全，已取消注入）",
        "GAME_CRASH": "游戏进程在注入后闪退",
        "SHM_NOT_OPEN": "注入返回成功但共享内存未打开（attach 未跑完/被拦截）",
        "HOOK_TIMEOUT": "WndProc hook 超时（hwnd 未写入或 SetWindowLongPtr 失败）",
        "HOOK_NO_HWND": "找不到游戏主窗口 hwnd",
        "HOOK_SETWNDPROC_FAIL": "SetWindowLongPtr 失败（权限/保护/错误窗口）",
        "HOOK_SETTIMER_FAIL": "SetTimer 失败（无法在 UI 线程安装 hook）",
        "HOOK_NOT_INSTALLED": "共享内存已开但 hook 未装上",
        "BRIDGE_PING_TIMEOUT": "桥接 PING 超时（后台窗口定时器未就绪，可自动重试或切到游戏再 Del）",
        "PING_TIMEOUT_NO_HOOK": "PING 超时（UI 定时器未装上或 hwnd 错误；绿端请点游戏窗口再试）",
        "POSTMESSAGE_FAIL": "PostMessage 失败（hwnd 无效）",
        "ATTACH_SEH": "DLL attach/timer 发生 SEH 异常",
        "GREEN_NO_HWND": "绿端兼容分支：助手侧未拿到游戏主窗口，请点一下游戏窗口后再按 Del",
        "UNKNOWN": "注入失败（未知，见完整日志）",
    }

    # Filled as inject progresses; always emitted in INJECT OUTCOME.
    triage: dict = {
        "profile": "",
        "attach_mode": None,
        "hwnd_source": "",
        "shm_name": "",
        "game_root": "",
    }

    def _fail(
        code: str,
        *,
        pid: int = 0,
        hwnd: int = 0,
        title: str = "",
        note: str = "",
        err: str | None = None,
        ping_ret=None,
        extra: str = "",
    ) -> InjectOutcome:
        msg = err or FAIL_MSG.get(code, FAIL_MSG["UNKNOWN"])
        if code and code not in msg:
            msg = f"[{code}] {msg}"
        out = InjectOutcome(
            ok=False,
            pid=pid,
            hwnd=hwnd,
            title=title,
            error=msg,
            bridge_note=note,
            ping_ret=ping_ret,
            fail_code=code,
        )
        # Publish before diagnostics/frame cleanup. Some native failure paths can
        # take time to unwind even though the user-visible outcome is final.
        publish_result(out)
        diag_log.log_inject_outcome(
            ok=False,
            pid=pid,
            hwnd=hwnd,
            title=title,
            error_msg=msg,
            bridge_note=note,
            ping_ret=ping_ret,
            extra=extra,
            fail_code=code,
            profile=str(triage.get("profile") or ""),
            attach_mode=triage.get("attach_mode"),
            hwnd_source=str(triage.get("hwnd_source") or ""),
            shm_name=str(triage.get("shm_name") or ""),
            game_root=str(triage.get("game_root") or ""),
        )
        return out

    if not is_bridge_built():
        diag_log.error("bridge not built", tag="INJECT")
        return _fail("BRIDGE_MISSING")

    procs = find_xajh_processes()
    if not procs:
        diag_log.error("no xajh.exe", tag="INJECT")
        return _fail("NO_GAME")

    pid = int(preferred_pid or 0)
    if pid and any(p["pid"] == pid for p in procs):
        chosen = next(p for p in procs if p["pid"] == pid)
    else:
        chosen = procs[0]
        pid = int(chosen["pid"])
        if preferred_pid and preferred_pid != pid:
            log(f"preferred pid={preferred_pid} not running; use {pid}")

    hwnd_enum, title, cls = find_main_hwnd_for_pid(pid)
    # Prefer Delete-time foreground hwnd (same process) over enum result.
    hwnd = int(preferred_hwnd or 0)
    hwnd_source = "none"
    if hwnd:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not user32.IsWindow(wintypes.HWND(hwnd)):
            log(f"preferred_hwnd=0x{hwnd:X} invalid; fall back to enum")
            hwnd = 0
        else:
            proc = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(proc))
            if int(proc.value) != int(pid):
                log(
                    f"preferred_hwnd=0x{hwnd:X} pid={int(proc.value)} "
                    f"!= target {pid}; fall back to enum"
                )
                hwnd = 0
            else:
                if not title:
                    buf = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, 512)
                    title = buf.value or ""
                hwnd_source = "preferred_fg"
                log(f"using preferred_hwnd=0x{hwnd:X} title={title!r}")
    if not hwnd:
        hwnd = hwnd_enum
        hwnd_source = "enum" if hwnd else "none"

    exe_path = str(chosen.get("exe") or "")
    triage["game_root"] = game_root_for_log(exe_path)
    triage["hwnd_source"] = hwnd_source

    profile = profile_from_proc(
        pid,
        exe_path=exe_path,
        title=title,
        window_class=cls,
    )
    triage["profile"] = profile.name
    triage["attach_mode"] = int(profile.attach_mode)
    log(
        f"inject target pid={pid} hwnd=0x{hwnd:X} hwnd_source={hwnd_source} "
        f"title={title!r} cls={cls!r} exe={exe_path!r}"
    )
    log(
        f"client_profile={profile.name} attach_mode=0x{profile.attach_mode:X} "
        f"allow_native_enum={profile.allow_native_enum} reason={profile.reason!r}"
    )
    # Skip per-pid integrity query here (expensive on some machines); OUTCOME logs it once.
    log(f"game_root={triage['game_root']!r} admin={diag_log.is_admin()}")
    diag_log.info(
        f"admin={diag_log.is_admin()} target_alive={diag_log.process_alive(pid)} "
        f"profile={profile.name} hwnd_source={hwnd_source}"
    )

    # Green path still needs helper hwnd eventually, but during splash/login the
    # window may appear a few seconds later — do not fail before delayed wait.

    # Stage short path for LoadLibrary (packaged builds).
    phase("stage", "准备桥接文件…")
    try:
        dll, inj = stage_bridge_for_inject(log=log)
        log(
            f"staged dll={dll} exists={dll.is_file()} "
            f"size={dll.stat().st_size if dll.is_file() else 0}"
        )
        log(f"staged inj={inj} exists={inj.is_file()}")
    except Exception as e:
        diag_log.exception(f"stage failed: {e}", tag="INJECT")

    pre: dict = {}
    phase("preflight", "检查进程权限…")
    try:
        pre = preflight_inject(pid, log=log)
    except Exception as e:
        diag_log.exception(f"preflight failed: {e}", tag="INJECT")
        pre = {}

    if pre and not pre.get("ok"):
        code = str(pre.get("fail_code") or "UNKNOWN")
        if code in (
            "GAME_DEAD",
            "ARCH_MISMATCH",
            "OPENPROCESS_DENIED",
            "BRIDGE_MISSING",
            "DLL_PATH_LONG",
        ) or code.startswith("OPENPROCESS_"):
            return _fail(
                code,
                pid=pid,
                hwnd=hwnd,
                title=title,
                note=f"preflight open_err={pre.get('open_process_err')}",
                extra=(
                    f"admin={pre.get('admin')} dll_mach={pre.get('dll_machine')} "
                    f"game_mach={pre.get('game_machine')} path_len={pre.get('path_len')} "
                    f"profile={profile.name}"
                ),
            )

    alive_before = diag_log.process_alive(pid)
    log(f"pre-inject alive={alive_before}")

    # Delayed inject: do not LoadLibrary while splash/login bootstrap is still
    # unstable. Reuse path inside ensure_bridge stays safe; this gate mainly
    # protects the first CreateRemoteThread against early-process flash-exit.
    create_time = float(chosen.get("create_time") or 0.0)
    ready, ready_reason, ready_hwnd, ready_title = wait_game_inject_ready(
        pid,
        preferred_hwnd=hwnd or preferred_hwnd,
        create_time=create_time,
        timeout_s=float(ready_wait_s or INJECT_READY_WAIT_S),
        min_age_s=MIN_INJECT_PROCESS_AGE_S,
        log=log,
        on_phase=phase,
    )
    if ready_hwnd:
        hwnd = int(ready_hwnd)
        if ready_title:
            title = ready_title
        triage["hwnd_source"] = triage.get("hwnd_source") or "ready_wait"
    if not ready:
        if ready_reason == "game_dead":
            return _fail(
                "GAME_DEAD",
                pid=pid,
                hwnd=hwnd,
                title=title,
                note=ready_reason,
                err="等待注入就绪时游戏进程已退出",
            )
        if profile.is_green and not hwnd:
            return _fail(
                "GREEN_NO_HWND",
                pid=pid,
                hwnd=0,
                title=title,
                note=ready_reason or profile.reason,
                extra="profile=green_compat",
            )
        return _fail(
            "GAME_NOT_READY",
            pid=pid,
            hwnd=hwnd,
            title=title,
            note=ready_reason,
            err=(
                "游戏登录/启动尚未就绪，已取消注入（避免闪退）。"
                f" 原因={ready_reason}。请等进入登录界面或角色后再按 Delete。"
            ),
            extra=f"wait_s={INJECT_READY_WAIT_S}",
        )
    if profile.is_green and not hwnd:
        return _fail(
            "GREEN_NO_HWND",
            pid=pid,
            hwnd=0,
            title=title,
            note=profile.reason,
            extra="profile=green_compat",
        )
    log(
        f"inject readiness ok pid={pid} hwnd=0x{int(hwnd):X} "
        f"reason={ready_reason} title={title!r}"
    )

    br = None
    ensure_meta: dict = {}
    try:
        phase("bridge", "连接/注入桥接…")
        log(f"ensure_bridge begin profile={profile.name}")
        # Reuse a healthy in-process bridge. Hot unload/reinject can unload
        # code while attach or hook callbacks are running; upgrades require a
        # game restart instead.
        br = ensure_bridge(
            pid,
            log=log,
            inject_if_needed=True,
            hwnd=hwnd or None,
            force_reinject=False,
            attach_mode=profile.attach_mode,
            wait_tries=profile.shm_wait_tries,
        )
        ensure_meta = last_ensure_meta(pid, clear=True)
        log(
            f"ensure_bridge end br={'ok' if br is not None else 'None'} "
            f"meta={ensure_meta!r}"
        )
        if br is not None:
            try:
                triage["shm_name"] = str(getattr(br, "shm_name", "") or "")
            except Exception:
                pass
            # Prefer prepared Global name if open did not stamp yet.
            if not triage["shm_name"]:
                try:
                    from app.core.xajh_bridge import _PREPARED_SHM

                    prep = _PREPARED_SHM.get(int(pid))
                    if prep and len(prep) >= 3:
                        triage["shm_name"] = str(prep[2] or "")
                except Exception:
                    pass
    except Exception as e:
        diag_log.exception(f"ensure_bridge raised: {e}", tag="INJECT")
        br = None

    reused = bool(ensure_meta.get("reused"))
    did_inject = bool(ensure_meta.get("did_inject"))
    ensure_pinged = bool(ensure_meta.get("pinged"))

    # Detect immediate crash after inject attempt (no long watch if already dead).
    if not diag_log.process_alive(pid):
        diag_log.error(
            f"game died immediately after ensure_bridge pid={pid}", tag="CRASH"
        )
        return _fail(
            "GAME_CRASH",
            pid=pid,
            hwnd=hwnd,
            title=title,
            note="crash",
            err=FAIL_MSG["GAME_CRASH"] + "（详见 logs\\xajh_helper_*.log）",
            extra="stage=post_ensure_immediate",
        )
    # Crash watch only after a fresh LoadLibrary (reuse path is already healthy).
    if did_inject and not reused:
        phase("watch", "确认游戏稳定…")
        if diag_log.watch_process_death(pid, seconds=0.6, tag="CRASH"):
            return _fail(
                "GAME_CRASH",
                pid=pid,
                hwnd=hwnd,
                title=title,
                note="crash",
                err=FAIL_MSG["GAME_CRASH"] + "（详见 logs\\xajh_helper_*.log）",
                extra="stage=post_ensure_watch",
            )

    if br is None:
        code = last_ensure_bridge_failure(pid, clear=True)
        if not code:
            code = classify_bridge_fail(
                pre=pre,
                alive=diag_log.process_alive(pid),
                inject_ok=None,
                shm_open=False,
            )
        if code == "UNKNOWN":
            code = "INJECT_EXE_FAIL"
        restart_required = "RESTART_GAME" in code
        other_pids = [int(p["pid"]) for p in procs if int(p["pid"]) != pid]
        err = None
        extra = f"target_pid={pid} other_pids={other_pids}"
        if restart_required:
            err = "请升级版本，或者完整退出游戏后重新注入"
            extra = (
                f"restart_required=1 target_pid={pid} "
                f"other_pids={other_pids}"
            )
        return _fail(
            code,
            pid=pid,
            hwnd=hwnd,
            title=title,
            err=err,
            extra=extra,
        )

    note_shm = ""
    try:
        note_shm = br._err()
    except Exception:
        pass
    log(f"bridge post-ensure note={note_shm!r} hwnd=0x{int(br._hwnd or 0):X}")

    def _publish_hwnd(h: int) -> int:
        h = int(h or 0)
        if not h:
            return 0
        try:
            br._set_u32(24, h)  # OFF_HWND
            br._hwnd = h
        except Exception:
            pass
        return h

    def _refresh_hwnd() -> int:
        nonlocal hwnd, title
        h, t, _c, _src = resolve_inject_hwnd(pid, hwnd or preferred_hwnd)
        if h:
            hwnd = int(h)
            if t:
                title = t
            return _publish_hwnd(hwnd)
        return _publish_hwnd(int(hwnd or 0))

    # Prefer game hwnd discovered here / refreshed for background one-click.
    _refresh_hwnd()

    # Reuse path already pinged successfully inside ensure_bridge — skip second
    # 6s PING. Fresh inject still verifies with a shorter first timeout.
    if reused and ensure_pinged:
        phase("ready", "桥接已就绪")
        ping_ret = ensure_meta.get("ping_ret")
        try:
            ping_ret = int(ping_ret) if ping_ret is not None else None
        except Exception:
            ping_ret = None
        ping_ok = True
        ping_note = note_shm or "pong"
        ping_hwnd = hwnd or int(getattr(br, "_hwnd", 0) or 0)
        ping_err = None
        log(f"skip verify ping (reused healthy bridge) ret={ping_ret}")
    else:
        # One-click often injects while the game is not foreground. Attach may
        # still be arming SetTimer; wait for ready note before hard PING fail.
        phase("timer", "等待桥接定时器…")
        ready_deadline = time.monotonic() + (12.0 if did_inject else 6.0)
        while time.monotonic() < ready_deadline:
            try:
                note_shm = br._err() or ""
            except Exception:
                note_shm = ""
            low = note_shm.lower()
            if (
                "bridge ready" in low
                or "rehook ok" in low
                or "already" in low
            ):
                break
            _refresh_hwnd()
            time.sleep(0.25)
        log(f"pre-ping note={note_shm!r} hwnd=0x{int(hwnd or 0):X}")

        phase("ping", "验证桥接…")
        ping = None
        ping_ok = False
        ping_ret = None
        ping_note = note_shm or ""
        ping_hwnd = int(hwnd or 0)
        ping_err = None
        timeouts = (2500, 4000, 6000) if did_inject else (3500, 5000, 7000)
        for attempt, timeout_ms in enumerate(timeouts, start=1):
            if not diag_log.process_alive(pid):
                diag_log.error("game died during ping retries", tag="CRASH")
                try:
                    br.close()
                except Exception:
                    pass
                return _fail(
                    "GAME_CRASH",
                    pid=pid,
                    hwnd=hwnd,
                    title=title,
                    note="crash",
                    err="游戏在 PING 前闪退",
                )
            h_use = _refresh_hwnd()
            try:
                note_now = (br._err() or "").lower()
            except Exception:
                note_now = ""
            timer_ready = (
                "bridge ready" in note_now
                or "rehook ok" in note_now
            )
            try:
                # Only REHOOK after timer is known-armed; otherwise PENDING hangs.
                if attempt > 1 and h_use and timer_ready:
                    rh = br.call(CMD_REHOOK, hwnd=h_use, timeout_ms=1800)
                    log(
                        f"rehook#{attempt} ok={rh.ok} note={rh.note!r} "
                        f"err={rh.error}"
                    )
                elif attempt > 1 and not timer_ready:
                    log(
                        f"skip rehook#{attempt}: timer not ready "
                        f"note={note_now!r}"
                    )
            except Exception as e:
                log(f"rehook#{attempt} err={e}")
            ping = br.call(CMD_PING, hwnd=h_use or None, timeout_ms=timeout_ms)
            log(
                f"ping{attempt} ok={ping.ok} ret={ping.ret} status={ping.status} "
                f"err={ping.error!r} note={ping.note!r} hwnd=0x{int(h_use or 0):X}"
            )
            if ping.ok:
                ping_ok = True
                break
            time.sleep(0.2)
        if ping is not None:
            ping_ret = ping.ret
            ping_note = ping.note or note_shm or "pong"
            ping_hwnd = hwnd or int(ping.hwnd or 0)
            ping_err = ping.error

    if not diag_log.process_alive(pid):
        diag_log.error("game died after ping path", tag="CRASH")
        try:
            br.close()
        except Exception:
            pass
        return _fail(
            "GAME_CRASH",
            pid=pid,
            hwnd=hwnd,
            title=title,
            note="crash",
            err="游戏在 PING 后闪退",
        )

    if not ping_ok:
        note = ping_note or note_shm or ""
        code = classify_bridge_fail(
            pre=pre,
            note=note,
            alive=True,
            inject_ok=True,
            shm_open=True,
            ping_err=str(ping_err or ""),
        )
        try:
            br.close()
        except Exception:
            pass
        err = ping_err or FAIL_MSG.get(code, "PING 失败")
        return _fail(
            code,
            pid=pid,
            hwnd=hwnd,
            title=title,
            note=note,
            err=err + "（请点一下游戏窗口后再试）",
            ping_ret=ping_ret,
        )

    log(f"bridge ping ok ret={ping_ret} note={ping_note!r} reused={reused}")
    # PING only proves that the injected DLL and its timer are alive.  A bridge
    # persists across character selection, where role-owned game objects are
    # absent and feature sampling is unsafe.  Require the UI-thread lifecycle
    # probe before mounting a business session.
    phase("role", "确认角色上下文…")
    host_ok, _host = _verify_bridge_host_context(
        br,
        hwnd=int(hwnd or 0),
        log=log,
    )
    if not host_ok:
        try:
            br.close()
        except Exception:
            pass
        return _fail(
            "GAME_NOT_READY",
            pid=pid,
            hwnd=hwnd,
            title=title,
            note="host_context=0",
            err=(
                "游戏当前没有可用角色上下文，已取消挂载（避免重选角色时闪退）。"
                "请进入角色后再按 Delete。"
            ),
            ping_ret=ping_ret,
            extra="stage=post_ping_host_context",
        )
    try:
        if not triage.get("shm_name"):
            triage["shm_name"] = str(getattr(br, "shm_name", "") or "")
    except Exception:
        pass
    try:
        br.close()
    except Exception:
        pass
    # The passive chat tap is independent from bridge commands. Failure keeps
    # the bridge usable, while captcha feedback falls back to legacy readers.
    try:
        from app.core.chat_tap import ensure_chat_tap

        tap_ok = ensure_chat_tap(pid, log=log)
        log(f"chat tap ready={tap_ok}")
    except Exception as e:
        log(f"chat tap setup skipped: {e}")
    phase("done", "注入完成")
    out = InjectOutcome(
        ok=True,
        pid=pid,
        hwnd=int(ping_hwnd or hwnd or 0),
        title=title,
        bridge_note=ping_note or "pong",
        ping_ret=ping_ret,
        fail_code="",
        reused=reused,
        did_inject=did_inject,
    )
    publish_result(out)
    diag_log.log_inject_outcome(
        ok=True,
        pid=pid,
        hwnd=int(ping_hwnd or hwnd or 0),
        title=title,
        bridge_note=ping_note or "pong",
        ping_ret=ping_ret,
        fail_code="",
        profile=str(triage.get("profile") or ""),
        attach_mode=triage.get("attach_mode"),
        hwnd_source=str(triage.get("hwnd_source") or ""),
        shm_name=str(triage.get("shm_name") or ""),
        game_root=str(triage.get("game_root") or ""),
    )
    return out


def run_delete_inject_async(
    on_done: Callable[[InjectOutcome], None],
    *,
    preferred_pid: int | None = None,
    preferred_hwnd: int | None = None,
    log: LogFn | None = None,
    on_phase: PhaseFn | None = None,
    ready_wait_s: float | None = None,
) -> None:
    """Background inject then callback. @author by ak"""

    def worker():
        out_box: dict[str, InjectOutcome] = {}
        result_ready = threading.Event()

        def _publish(out: InjectOutcome) -> None:
            if result_ready.is_set():
                return
            out_box["out"] = out
            result_ready.set()

        def _run():
            try:
                out = run_delete_inject(
                    preferred_pid=preferred_pid,
                    preferred_hwnd=preferred_hwnd,
                    log=log,
                    on_phase=on_phase,
                    on_result=_publish,
                    ready_wait_s=ready_wait_s,
                )
                _publish(out)
            except Exception as e:
                _publish(
                    InjectOutcome(ok=False, error=str(e), fail_code="UNKNOWN")
                )

        t = threading.Thread(target=_run, daemon=True, name="xajh-inject-inner")
        t.start()
        # Hard ceiling: ready-wait + ping retries + recovery must not leave the
        # shell stuck on 注入中 / inject_busy forever. Wait for the final result,
        # not for native/Python frame cleanup after that result has been decided.
        if not result_ready.wait(timeout=90.0):
            out = InjectOutcome(
                ok=False,
                pid=int(preferred_pid or 0),
                hwnd=int(preferred_hwnd or 0),
                error="注入超时（后台桥接未在 90s 内就绪，已中止避免一直注入中）",
                fail_code="INJECT_TIMEOUT",
            )
        else:
            out = out_box.get(
                "out",
                InjectOutcome(
                    ok=False,
                    error="inject worker missing result",
                    fail_code="UNKNOWN",
                ),
            )
        on_done(out)

    threading.Thread(target=worker, daemon=True, name="xajh-inject-async").start()
