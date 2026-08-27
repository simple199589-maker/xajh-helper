# -*- coding: utf-8 -*-
"""
Crash capture for helper + game — unified into the error log only.

All hard failures go to:
  <software>/logs/xajh_helper_error_YYYYMMDD.log

No separate helper_crash / game_crash / fault / emergency files.
Native bridge also appends game crash points into the same error log.

@author by ak
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

_EXIT_CODE_NAMES: dict[int, str] = {
    0xC0000005: "ACCESS_VIOLATION",
    0xC0000006: "IN_PAGE_ERROR",
    0xC0000008: "INVALID_HANDLE",
    0xC0000017: "NO_MEMORY",
    0xC000001D: "ILLEGAL_INSTRUCTION",
    0xC0000025: "NONCONTINUABLE_EXCEPTION",
    0xC0000026: "INVALID_DISPOSITION",
    0xC000008C: "ARRAY_BOUNDS_EXCEEDED",
    0xC000008D: "FLOAT_DENORMAL_OPERAND",
    0xC000008E: "FLOAT_DIVIDE_BY_ZERO",
    0xC000008F: "FLOAT_INEXACT_RESULT",
    0xC0000090: "FLOAT_INVALID_OPERATION",
    0xC0000091: "FLOAT_OVERFLOW",
    0xC0000092: "FLOAT_STACK_CHECK",
    0xC0000093: "FLOAT_UNDERFLOW",
    0xC0000094: "INTEGER_DIVIDE_BY_ZERO",
    0xC0000095: "INTEGER_OVERFLOW",
    0xC0000096: "PRIVILEGED_INSTRUCTION",
    0xC00000FD: "STACK_OVERFLOW",
    0xC0000135: "DLL_NOT_FOUND",
    0xC0000139: "ENTRYPOINT_NOT_FOUND",
    0xC0000142: "DLL_INIT_FAILED",
    0xC0000409: "STACK_BUFFER_OVERRUN",
    0xC0000417: "INVALID_CRUNTIME_PARAMETER",
    0xC000041D: "FATAL_USER_CALLBACK_EXCEPTION",
    0x80000003: "BREAKPOINT",
    0x80000004: "SINGLE_STEP",
    0x40010004: "CTRL_C_EXIT",
    0xC000013A: "CONTROL_C_EXIT",
}

_lock = threading.RLock()
_installed = False
_ready_written = False
_reported_game_pids: set[int] = set()
_last_bridge_ctx: dict[int, dict[str, Any]] = {}


def _error_path() -> Path:
    """Unified error/crash log path. @author by ak"""
    try:
        from app.core import diag_log

        return diag_log.error_log_path()
    except Exception:
        try:
            from common.paths import app_root

            root = app_root()
        except Exception:
            root = Path(sys.executable).resolve().parent
        day = datetime.now().strftime("%Y%m%d")
        d = root / "logs"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d / f"xajh_helper_error_{day}.log"


def error_log_path() -> Path:
    """Public alias for the unified error log. @author by ak"""
    return _error_path()


def native_game_crash_path(pid: int) -> Path:
    """
    Native dump target = same unified error log (pid kept for API compat).

    @author by ak
    """
    return _error_path()


def describe_exit_code(code: int | None) -> str:
    """Human-readable NTSTATUS / Win32 exit code. @author by ak"""
    if code is None:
        return "unknown"
    c = int(code) & 0xFFFFFFFF
    name = _EXIT_CODE_NAMES.get(c)
    if name:
        return f"0x{c:08X}({name})"
    if c == 0:
        return "0(clean_exit)"
    if c < 0xC0000000:
        return f"{c}(0x{c:X})"
    return f"0x{c:08X}"


def process_exit_code(pid: int) -> int | None:
    """Best-effort exit code for a dead pid. @author by ak"""
    pid = int(pid or 0)
    if pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        PROCESS_QUERY_INFORMATION = 0x0400
        STILL_ACTIVE = 259
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            h = k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
        if not h:
            return None
        code = wintypes.DWORD(0)
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        if not ok:
            return None
        val = int(code.value) & 0xFFFFFFFF
        if val == STILL_ACTIVE:
            return None
        return val
    except Exception:
        return None


def format_crash_point(
    exc_type: type[BaseException] | None = None,
    exc: BaseException | None = None,
    tb: Any = None,
) -> str:
    """One-line crash point. @author by ak"""
    try:
        frames = traceback.extract_tb(tb) if tb is not None else []
        if frames:
            last = frames[-1]
            loc = f"{last.filename}:{last.lineno} in {last.name}"
        else:
            loc = "<no-frame>"
        typ = getattr(exc_type, "__name__", None) or (
            type(exc).__name__ if exc else "?"
        )
        msg = ""
        if exc is not None:
            msg = str(exc).strip().replace("\n", " ")
            if len(msg) > 200:
                msg = msg[:197] + "..."
        if msg:
            return f"{loc} | {typ}: {msg}"
        return f"{loc} | {typ}"
    except Exception as e:
        return f"<crash_point_fmt_err:{e}>"


def _write_error_block(text: str) -> None:
    """Append a multi-line block into the unified error log. @author by ak"""
    # Prefer diag_log so header / always-on rules stay consistent.
    try:
        from app.core import diag_log

        # write() is one line oriented; dump block as one FATAL message.
        diag_log.write(text, level="FATAL", tag="CRASH")
        return
    except Exception:
        pass
    path = _error_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text if text.endswith("\n") else text + "\n"
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
    except Exception:
        pass


def note_bridge_context(
    pid: int,
    *,
    cmd: Any = None,
    status: Any = None,
    ret: Any = None,
    err: str = "",
    note: str = "",
    extra: str = "",
) -> None:
    """Remember last bridge call context for a game pid. @author by ak"""
    pid = int(pid or 0)
    if pid <= 0:
        return
    with _lock:
        _last_bridge_ctx[pid] = {
            "ts": time.time(),
            "cmd": cmd,
            "status": status,
            "ret": ret,
            "err": str(err or "")[:240],
            "note": str(note or "")[:240],
            "extra": str(extra or "")[:240],
        }


def get_bridge_context(pid: int) -> dict[str, Any]:
    """Last known bridge context for pid. @author by ak"""
    with _lock:
        return dict(_last_bridge_ctx.get(int(pid), {}) or {})


def parse_native_crash_point(native_text: str) -> str:
    """Extract crash_point= from a native dump block. @author by ak"""
    if not native_text:
        return ""
    for line in native_text.splitlines():
        s = line.strip()
        if s.lower().startswith("crash_point="):
            return s.split("=", 1)[1].strip()
        if s.lower().startswith("fault_addr="):
            return s
    for line in native_text.splitlines():
        s = line.strip()
        if s and not s.startswith("=") and not s.startswith("pid="):
            return s[:240]
    return ""


def read_native_game_crash(pid: int) -> str:
    """
    Best-effort: scan unified error log tail for this pid's native dump.

    @author by ak
    """
    path = _error_path()
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return ""
        # Read last ~256KB only.
        with open(path, "rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 256 * 1024), os.SEEK_SET)
            except Exception:
                f.seek(0)
            raw = f.read().decode("utf-8", errors="replace")
        marker = f"pid={int(pid)}"
        blocks = raw.split("=== XAJH GAME_CRASH")
        for block in reversed(blocks[1:]):
            if marker in block:
                return ("=== XAJH GAME_CRASH" + block).strip()
    except Exception:
        pass
    return ""


def report_helper_crash(
    source: str,
    *,
    exc_type: type[BaseException] | None = None,
    exc: BaseException | None = None,
    tb: Any = None,
    text: str = "",
    extra: str = "",
) -> str:
    """Write 助手崩溃 into the unified error log. @author by ak"""
    point = format_crash_point(exc_type, exc, tb)
    if not text:
        try:
            if exc_type is not None:
                text = "".join(traceback.format_exception(exc_type, exc, tb))
            else:
                text = traceback.format_exc()
        except Exception:
            text = repr(exc)
    thr = threading.current_thread()
    block = (
        f"HELPER_CRASH kind=助手崩溃 source={source}\n"
        f"crash_point={point}\n"
        f"helper_pid={os.getpid()} thread={getattr(thr, 'name', '?')} "
        f"tid={getattr(thr, 'ident', '?')}\n"
        f"exe={sys.executable}\n"
        f"argv={sys.argv!r}\n"
        f"error_log={_error_path()}\n"
    )
    if extra:
        block += f"extra={extra}\n"
    block += f"{text.rstrip()}"
    _write_error_block(block)
    return point


def report_game_crash(
    pid: int,
    *,
    title: str = "",
    reason: str = "",
    last_cmd: Any = None,
    last_err: str = "",
    extra: str = "",
    force: bool = False,
) -> str:
    """Write 游戏崩溃/退出 into the unified error log. @author by ak"""
    pid = int(pid or 0)
    if pid <= 0:
        return ""
    with _lock:
        if not force and pid in _reported_game_pids:
            return ""
        _reported_game_pids.add(pid)

    exit_code = process_exit_code(pid)
    exit_s = describe_exit_code(exit_code)
    native = read_native_game_crash(pid)
    native_point = parse_native_crash_point(native)
    ctx = get_bridge_context(pid)
    if last_cmd is None:
        last_cmd = ctx.get("cmd")
    if not last_err:
        last_err = str(ctx.get("err") or "")
    note = str(ctx.get("note") or "")

    fatal_nt = (
        exit_code is not None and (int(exit_code) & 0xFFFFFFFF) >= 0xC0000000
    )
    if native_point:
        point = native_point
        kind = "游戏崩溃"
    elif fatal_nt:
        point = f"exit_code={exit_s}"
        kind = "游戏崩溃"
    elif exit_code == 0:
        point = f"exit_code={exit_s}"
        kind = "游戏退出"
    elif reason:
        point = f"reason={reason}"
        kind = "游戏退出" if "poll" in str(reason) else "游戏崩溃"
    else:
        point = "process_exit(unknown_point)"
        kind = "游戏崩溃"

    block = (
        f"GAME_CRASH kind={kind} pid={pid}\n"
        f"crash_point={point}\n"
        f"exit_code={exit_s}\n"
        f"title={title!r}\n"
        f"reason={reason!r}\n"
        f"last_cmd={last_cmd!r} last_err={last_err!r} last_note={note!r}\n"
        f"bridge_ctx={ctx!r}\n"
        f"helper_pid={os.getpid()}\n"
        f"error_log={_error_path()}\n"
    )
    if extra:
        block += f"extra={extra}\n"
    if native:
        block += f"NATIVE_CRASH_DUMP\n{native}\n"
    _write_error_block(block)
    return point


def clear_game_crash_dedupe(pid: int | None = None) -> None:
    """Allow re-report after reinject / new mount. @author by ak"""
    with _lock:
        if pid is None:
            _reported_game_pids.clear()
        else:
            _reported_game_pids.discard(int(pid))


def _write_ready_marker() -> None:
    """One-line ready marker in error log. @author by ak"""
    global _ready_written
    with _lock:
        if _ready_written:
            return
        _ready_written = True
    try:
        from app.core import diag_log

        diag_log.write(
            f"CRASH_CAPTURE_READY helper_pid={os.getpid()} "
            f"error_log={_error_path()}",
            level="ERROR",
            tag="CRASH",
        )
    except Exception:
        _write_error_block(
            f"CRASH_CAPTURE_READY helper_pid={os.getpid()} error_log={_error_path()}"
        )


def install_crash_handlers() -> None:
    """Install helper crash hooks; all output goes to error log. @author by ak"""
    global _installed
    with _lock:
        already = _installed
        _installed = True
    if already:
        try:
            _write_ready_marker()
        except Exception:
            pass
        return

    # faulthandler dumps into the same error log file.
    try:
        import faulthandler

        try:
            fp = _error_path()
            f = open(fp, "a", encoding="utf-8", errors="replace")
            faulthandler.enable(file=f, all_threads=True)
            install_crash_handlers._fault_fp = f  # type: ignore[attr-defined]
        except Exception:
            try:
                faulthandler.enable(all_threads=True)
            except Exception:
                pass
    except Exception:
        pass

    prev = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            report_helper_crash(
                "sys.excepthook",
                exc_type=exc_type,
                exc=exc,
                tb=tb,
            )
        except Exception:
            try:
                text = "".join(traceback.format_exception(exc_type, exc, tb))
                _write_error_block(f"HELPER_CRASH_FALLBACK\n{text}")
            except Exception:
                pass
        try:
            prev(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _hook

    if hasattr(threading, "excepthook"):
        prev_th = threading.excepthook

        def _th_hook(args):  # type: ignore[no-untyped-def]
            try:
                report_helper_crash(
                    f"threading.excepthook name={getattr(args.thread, 'name', '?')}",
                    exc_type=args.exc_type,
                    exc=args.exc_value,
                    tb=args.exc_traceback,
                )
            except Exception:
                pass
            try:
                prev_th(args)
            except Exception:
                pass

        threading.excepthook = _th_hook  # type: ignore[assignment]

    try:
        _write_ready_marker()
    except Exception:
        pass


def report_tk_callback_exception(exc_type, exc_value, exc_tb) -> str:
    """Tk callback exception → error log. @author by ak"""
    return report_helper_crash(
        "tk.report_callback_exception",
        exc_type=exc_type,
        exc=exc_value,
        tb=exc_tb,
    )


def watch_process_death(
    pid: int,
    *,
    seconds: float = 3.0,
    interval: float = 0.2,
    title: str = "",
    reason: str = "watch",
) -> bool:
    """Poll until timeout; on death write game crash to error log. @author by ak"""
    pid = int(pid)
    if pid <= 0:
        return False
    deadline = time.time() + max(0.1, float(seconds))
    while time.time() < deadline:
        alive = True
        try:
            from app.core.diag_log import process_alive

            alive = process_alive(pid)
        except Exception:
            try:
                import psutil

                alive = bool(psutil.pid_exists(pid))
            except Exception:
                alive = True
        if not alive:
            time.sleep(0.15)
            report_game_crash(pid, title=title, reason=reason)
            return True
        time.sleep(interval)
    return False
