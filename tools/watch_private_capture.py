# -*- coding: utf-8 -*-
"""watch_private_capture.py — 高频轮询 team_tap dump 槽位 + chat_tap ch=9，等待手动私聊。

用法: python tools/watch_private_capture.py [秒数=180]
输出: dist/private_capture_log.txt
@author by ak
"""
import ctypes
import struct
import sys
import time
from ctypes import wintypes

PIDS = [6456, 35832, 35952]  # 十丶三 / 苦寒未曾来 / 初一
TEAM_TAP_SIZE = 1748
OFF_DUMP_LEN = 1456
OFF_DUMP = 1460
CHAT_TAP_SIZE = 303640

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenFileMappingW.restype = wintypes.HANDLE
k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k32.MapViewOfFile.restype = wintypes.LPVOID
k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
k32.UnmapViewOfFile.argtypes = [wintypes.LPCVOID]
k32.CloseHandle.argtypes = [wintypes.HANDLE]

NAMES = {6456: "十丶三", 35832: "苦寒未曾来", 35952: "初一"}


def open_map(name: str, size: int):
    h = k32.OpenFileMappingW(0x0006, False, name)
    if not h:
        return None, None
    v = k32.MapViewOfFile(h, 0x0006, 0, 0, size)
    if not v:
        k32.CloseHandle(h)
        return None, None
    return h, v


def read_team_dump(v) -> bytes:
    d = ctypes.string_at(ctypes.c_void_p(v), TEAM_TAP_SIZE)
    ln = int.from_bytes(d[OFF_DUMP_LEN:OFF_DUMP_LEN + 4], "little")
    if not ln:
        return b""
    return d[OFF_DUMP:OFF_DUMP + min(ln, 256)]


def read_chat_events(v, cursors: dict) -> list:
    d = ctypes.string_at(ctypes.c_void_p(v), CHAT_TAP_SIZE)
    out = []
    for label, off_seq, off_ev, cap in [("main", 20, 156, 50), ("team", 27156, 27160, 512)]:
        newest = int.from_bytes(d[off_seq:off_seq + 4], "little")
        mark = cursors.get(label, newest)
        if newest <= mark:
            continue
        oldest = max(1, newest - cap + 1)
        start = max(mark + 1, oldest)
        for expected in range(start, newest + 1):
            slot = (expected - 1) % cap
            o = off_ev + slot * 540
            seq, tick, tid, caller, ch, flags, tlen = struct.unpack_from("<7I", d, o)
            if seq != expected:
                break
            text = d[o + 28:o + 28 + min(tlen, 255) * 2].decode("utf-16-le", "replace")
            out.append((label, ch, text))
        cursors[label] = newest
    return out


def fmt_dump(b: bytes) -> str:
    lines = []
    for i in range(0, len(b), 16):
        chunk = b[i:i + 16]
        hexs = " ".join(f"{x:02x}" for x in chunk)
        text = "".join(chr(x) if 0x20 <= x < 0x7F else "." for x in chunk)
        lines.append(f"    {i:04x}  {hexs:<47}  {text}")
    return "\n".join(lines)


def main() -> None:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0
    logf = open(r"dist\private_capture_log.txt", "a", encoding="utf-8")
    prints = {}

    def out(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d}] {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    out(f"=== 开始监听 pids={PIDS} 时长={duration}s — 等待手动私聊 ===")
    team_views, chat_views = {}, {}
    for pid in PIDS:
        h, v = open_map(f"Local\\XajhTeamTap_{pid}", TEAM_TAP_SIZE)
        if v:
            team_views[pid] = v
        h2, v2 = open_map(f"Local\\XajhChatTap_{pid}", CHAT_TAP_SIZE)
        if v2:
            chat_views[pid] = v2
        out(f"pid={pid}({NAMES.get(pid)}) team_tap={'OK' if v else '无'} chat_tap={'OK' if v2 else '无'}")

    last_dump = {pid: None for pid in team_views}
    chat_cursors = {pid: {} for pid in chat_views}
    deadline = time.monotonic() + duration
    n_dumps = 0
    while time.monotonic() < deadline:
        for pid, v in team_views.items():
            d = read_team_dump(v)
            if d and d != last_dump[pid]:
                last_dump[pid] = d
                n_dumps += 1
                out(f"### pid={pid}({NAMES.get(pid)}) 新封包 len={len(d)}")
                out(fmt_dump(d))
                try:
                    text = d[0x14:0x14 + d[0x13]].decode("utf-16-le", "replace")
                    out(f"    -> 若为聊天封包, text={text!r}")
                except Exception:
                    pass
        for pid, v in chat_views.items():
            try:
                for label, ch, text in read_chat_events(v, chat_cursors[pid]):
                    if ch in (9, 3, 2, 5):
                        out(f"### pid={pid}({NAMES.get(pid)}) [{label}] ch={ch} {text!r}")
            except Exception:
                pass
        time.sleep(0.005)
    out(f"=== 监听结束, 共捕获 {n_dumps} 个新封包 ===")


if __name__ == "__main__":
    main()
