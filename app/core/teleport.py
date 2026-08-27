# -*- coding: utf-8 -*-
"""
Direct host-position teleport (write host +0x158 float3).

Bypasses the movement boundary / ground / region checks entirely by writing the
host object world position directly, instead of walking there.  Server is
authoritative (NotifyHostPos may pull back); this is a lab/experiment path.

@author by ak
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from typing import Callable

from app.core.plg_objects import OBJ_POS_OFF

# Host position field copies (live-verified): authoritative pos, mirror, AOI,
# client-predicted pos all share the same (x,y,z) float3.
HOST_POS_FIELDS = (0x03C, 0x07C, OBJ_POS_OFF, 0x200)

LogFn = Callable[[str], None]

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCVOID,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.restype = wintypes.BOOL
k32.VirtualProtectEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
k32.VirtualProtectEx.restype = wintypes.BOOL

PAGE_EXECUTE_READWRITE = 0x40
PROCESS_ALL = 0x001F0FFF
MAX_COORD = 500000.0


def _open(pid: int) -> int:
    h = int(k32.OpenProcess(PROCESS_ALL, False, int(pid)) or 0)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def _read_bytes(h: int, va: int, size: int) -> bytes:
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t()
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(va), buf, size, ctypes.byref(n)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buf.raw[: max(0, n.value)]


def write_host_pos(
    session,
    xyz: tuple[float, float, float],
    *,
    log: LogFn | None = None,
) -> dict:
    """
    Write host world position at host +0x158 to xyz (float3).

    Returns {ok, pos, host, note|error}.
    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        return {"ok": False, "error": "no pid"}
    x, y, z = (float(v) for v in xyz)
    if not all(v == v and abs(v) <= MAX_COORD for v in (x, y, z)):
        return {"ok": False, "error": f"invalid coords {xyz}"}

    host = 0
    try:
        from app.core.plg_ui import get_host_player_ptr

        host = int(get_host_player_ptr(session, log=log) or 0) & 0xFFFFFFFF
    except Exception as e:
        log(f"teleport get_host err: {e}")
        return {"ok": False, "error": f"get_host: {e}"}
    if not host or host < 0x10000:
        return {"ok": False, "error": f"invalid host ptr 0x{host:X}"}

    va = int(host) + int(OBJ_POS_OFF)
    payload = struct.pack("<fff", x, y, z)
    h = _open(pid)
    try:
        written: list[str] = []
        for off in HOST_POS_FIELDS:
            va_i = int(host) + int(off)
            old = wintypes.DWORD()
            if not k32.VirtualProtectEx(h, ctypes.c_void_p(va_i), len(payload),
                                        PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                done = ctypes.c_size_t()
                if not k32.WriteProcessMemory(h, ctypes.c_void_p(va_i), payload,
                                              len(payload), ctypes.byref(done)) or done.value != len(payload):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                k32.VirtualProtectEx(h, ctypes.c_void_p(va_i), len(payload),
                                     int(old.value or 0), ctypes.byref(wintypes.DWORD()))
            rb = _read_bytes(h, va_i, 12)
            rx, ry, rz = struct.unpack("<fff", rb)
            ok = abs(rx - x) < 0.01 and abs(ry - y) < 0.01 and abs(rz - z) < 0.01
            written.append(f"+0x{off:X}:{'OK' if ok else 'MISMATCH'}")
        rx, ry, rz = struct.unpack("<fff", _read_bytes(h, int(host) + int(OBJ_POS_OFF), 12))
        log(f"teleport host 0x{host:X} -> ({x:.1f},{y:.1f},{z:.1f}) "
            f"[{', '.join(written)}] readback 0x158=({rx:.1f},{ry:.1f},{rz:.1f})")
        all_ok = all("OK" in w for w in written)
        return {
            "ok": all_ok,
            "pos": (float(rx), float(ry), float(rz)),
            "host": int(host),
            "fields": written,
            "note": "" if all_ok else "write-back mismatch",
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"write: {e}"}
    finally:
        k32.CloseHandle(h)


def read_host_pos(session, *, log: LogFn | None = None) -> dict:
    """Read host +0x158 float3. @author by ak"""
    log = log or (lambda _m: None)
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        return {"ok": False, "error": "no pid"}
    try:
        from app.core.plg_ui import get_host_player_ptr

        host = int(get_host_player_ptr(session, log=log) or 0) & 0xFFFFFFFF
    except Exception as e:
        return {"ok": False, "error": f"get_host: {e}"}
    if not host or host < 0x10000:
        return {"ok": False, "error": f"invalid host ptr 0x{host:X}"}
    va = int(host) + int(OBJ_POS_OFF)
    h = _open(pid)
    try:
        raw = _read_bytes(h, va, 12)
        x, y, z = struct.unpack("<fff", raw)
        return {"ok": True, "pos": (float(x), float(y), float(z)), "host": int(host)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"read: {e}"}
    finally:
        k32.CloseHandle(h)


# Host facing direction vector (unit circle) at +0x0C (dx) / +0x14 (dz).
HOST_FACING_DX_OFF = 0x0C
HOST_FACING_DZ_OFF = 0x14


def read_host_facing(session, *, log: LogFn | None = None) -> dict:
    """Read host facing direction (dx, dz) unit vector. @author by ak"""
    log = log or (lambda _m: None)
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        return {"ok": False, "error": "no pid"}
    try:
        from app.core.plg_ui import get_host_player_ptr

        host = int(get_host_player_ptr(session, log=log) or 0) & 0xFFFFFFFF
    except Exception as e:
        return {"ok": False, "error": f"get_host: {e}"}
    if not host or host < 0x10000:
        return {"ok": False, "error": f"invalid host ptr 0x{host:X}"}
    h = _open(pid)
    try:
        dx = struct.unpack("<f", _read_bytes(h, int(host) + HOST_FACING_DX_OFF, 4))[0]
        dz = struct.unpack("<f", _read_bytes(h, int(host) + HOST_FACING_DZ_OFF, 4))[0]
        return {"ok": True, "facing": (float(dx), float(dz)), "host": int(host)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"read: {e}"}
    finally:
        k32.CloseHandle(h)


def host_move_facing(
    session,
    distance: float,
    *,
    mode: int = 5,
    fallback_mode0: bool = False,
    log: LogFn | None = None,
) -> dict:
    """Pathfind `distance` units in the host's current facing direction.

    Uses plg::HostMoveToScenePosition(mode=5) which the server accepts for real
    movement.  Pair with wall-clip + passmap gates to cross non-walkable cells.
    @author by ak
    """
    log = log or (lambda _m: None)
    pos_r = read_host_pos(session, log=log)
    if not pos_r.get("ok"):
        return pos_r
    facing_r = read_host_facing(session, log=log)
    if not facing_r.get("ok"):
        return facing_r
    px, py, pz = pos_r["pos"]
    dx, dz = facing_r["facing"]
    try:
        from app.core.automove import PathTarget, host_move_to
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"import automove: {e}"}
    tx = px + float(dx) * float(distance)
    tz = pz + float(dz) * float(distance)
    log(
        f"host_move_facing d={distance} facing=({dx:.3f},{dz:.3f}) "
        f"target=({tx:.2f},{py:.2f},{tz:.2f})"
    )
    try:
        r = host_move_to(
            session,
            PathTarget(x=tx, y=py, z=tz, mode=mode),
            fallback_mode0=fallback_mode0,
            log=log,
        )
        return {
            "ok": bool(r and getattr(r, "ok", False)),
            "target": (float(tx), float(py), float(tz)),
            "facing": (float(dx), float(dz)),
            "ret": int(r.ret) if r else None,
            "note": (r.note if r else None) or "",
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"host_move: {e}"}
