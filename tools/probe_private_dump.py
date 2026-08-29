# -*- coding: utf-8 -*-
"""probe_private_dump.py — 读取 team_tap v5 共享内存状态 + dump 槽位抓包内容。

用法:
    python tools/probe_private_dump.py [pid ...]

不传 pid 则自动枚举所有 xajh 进程。
@author by ak
"""
import ctypes
import sys
from ctypes import wintypes

TEAM_TAP_MAGIC = 0x54544D50
TEAM_TAP_VERSION = 5
TEAM_TAP_SIZE = 1748
OFF_MAGIC = 0
OFF_VERSION = 4
OFF_STRUCT_SIZE = 8
OFF_STATUS = 12
OFF_TARGET_VA = 16
OFF_SEND_MGR = 20
OFF_SEEN = 24
OFF_ERROR = 28  # 128B
OFF_HIT_SEQ = 156
OFF_HITS = 160  # 64 * 12
OFF_SEND_REQ = 928  # 16 + 512
OFF_DUMP_LEN = 1456
OFF_DUMP = 1460  # 256B
OFF_IDENT0 = 1716
OFF_IDENT1 = 1720
OFF_IDENT2 = 1724
OFF_IDENT_SEEN = 1728
OFF_EXACT_STATUS = 1732
OFF_EXACT_COUNT = 1736
OFF_EXACT_SEND_MGR = 1740
OFF_EXACT_SEEN = 1744

STATUS_MAP = {0: "INIT", 1: "ACTIVE", 2: "ERROR"}
EXACT_MAP = {0: "IDLE", 1: "RUNNING", 2: "FOUND", 3: "MULTIPLE", 4: "NOT_FOUND"}

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenFileMappingW.restype = wintypes.HANDLE
k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k32.MapViewOfFile.restype = wintypes.LPVOID
k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
k32.UnmapViewOfFile.argtypes = [wintypes.LPCVOID]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.GetLastError.restype = wintypes.DWORD


def read_shm(pid: int) -> bytes | None:
    h = k32.OpenFileMappingW(0x0006, False, f"Local\\XajhTeamTap_{pid}")
    if not h:
        return None
    view = k32.MapViewOfFile(h, 0x0006, 0, 0, TEAM_TAP_SIZE)
    if not view:
        k32.CloseHandle(h)
        return None
    try:
        return ctypes.string_at(ctypes.c_void_p(view), TEAM_TAP_SIZE)
    finally:
        k32.UnmapViewOfFile(ctypes.c_void_p(view))
        k32.CloseHandle(h)


def u32(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 4], "little")


def probe(pid: int) -> None:
    data = read_shm(pid)
    if data is None:
        print(f"[{pid}] team_tap 共享内存不存在（未注入或已失效）")
        return
    magic = u32(data, OFF_MAGIC)
    version = u32(data, OFF_VERSION)
    struct_size = u32(data, OFF_STRUCT_SIZE)
    status = u32(data, OFF_STATUS)
    send_mgr = u32(data, OFF_SEND_MGR)
    seen = u32(data, OFF_SEEN)
    err = data[OFF_ERROR:OFF_ERROR + 128].split(b"\0", 1)[0].decode("gbk", "replace")
    ident0, ident1, ident2 = u32(data, OFF_IDENT0), u32(data, OFF_IDENT1), u32(data, OFF_IDENT2)
    ident_seen = u32(data, OFF_IDENT_SEEN)
    exact_status = u32(data, OFF_EXACT_STATUS)
    exact_count = u32(data, OFF_EXACT_COUNT)
    exact_mgr = u32(data, OFF_EXACT_SEND_MGR)
    dump_len = u32(data, OFF_DUMP_LEN)
    dump = data[OFF_DUMP:OFF_DUMP + min(dump_len, 256)]

    print(f"=== pid={pid} ===")
    print(f"  magic=0x{magic:08X} ver={version} size={struct_size} status={STATUS_MAP.get(status, status)}")
    print(f"  send_mgr=0x{send_mgr:08X} seen={seen} exact={EXACT_MAP.get(exact_status, exact_status)} count={exact_count} exact_mgr=0x{exact_mgr:08X}")
    print(f"  ident=({ident0:#04x},{ident1:#04x},{ident2:#04x}) seen={ident_seen} error={err!r}")
    if dump_len:
        print(f"  dump_len={dump_len}")
        for i in range(0, len(dump), 16):
            chunk = dump[i:i + 16]
            hexs = " ".join(f"{b:02x}" for b in chunk)
            text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
            print(f"    {i:04x}  {hexs:<47}  {text}")
        # 尝试按 UTF-16LE 解析封包尾部文本区
        if dump_len > 0x14:
            n = dump[0x13]
            if 0 < n < 200 and 0x14 + n <= dump_len:
                try:
                    body = dump[0x14:0x14 + n].decode("utf-16-le", "replace")
                    print(f"  解析 text_len={n} text={body!r}")
                except Exception as exc:
                    print(f"  text 解析失败: {exc}")
    else:
        print("  dump 为空（自注入以来没有手动聊天发送）")


def main() -> None:
    pids = [int(a) for a in sys.argv[1:] if a.isdigit()]
    if not pids:
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq xajh.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) > 1 and parts[1].isdigit():
                pids.append(int(parts[1]))
    if not pids:
        print("未找到 xajh.exe 进程")
        return
    for pid in pids:
        probe(pid)


if __name__ == "__main__":
    main()
