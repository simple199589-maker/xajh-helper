import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time
from pathlib import Path

# Static findings for the verified xajh.exe build:
# TimeDateStamp=0x56736608, SizeOfImage=0x038A5000, preferred base=0x00400000
SEND_MGR_VTABLE_VA = 0x012C7C00
CRYPTO_CFG_A_VA = 0x01D61A88   # object + 0xCC
CRYPTO_CFG_B_VA = 0x01D61B9C   # object + 0xD0
SEND_BUFFER_CAP = 0x8000       # object + 0x24, observed on all live instances
OBJ_SIZE = 0xE0

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_size_t),
        ("AllocationBase", ctypes.c_size_t),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * wt.MAX_PATH),
    ]


def u32(buf, off):
    return int.from_bytes(buf[off:off + 4], "little")


def xajh_pids():
    TH32CS_SNAPPROCESS = 0x00000002
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    pids = []
    ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        if entry.szExeFile.lower() == "xajh.exe":
            pids.append(int(entry.th32ProcessID))
        ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)
    return sorted(pids)


def read_regions(pid):
    h = kernel32.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION | VM_READ
    if not h:
        raise OSError(f"OpenProcess({pid}) failed, winerr={ctypes.get_last_error()}")

    regions = []
    addr = 0
    mbi = MBI()
    while addr < 0x7FFF0000:
        if not kernel32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        size = int(mbi.RegionSize)
        if (mbi.State == 0x1000 and mbi.Protect not in (0, 1) and
                0 < size < 0x10000000):
            buf = (ctypes.c_char * size)()
            got = ctypes.c_size_t()
            if kernel32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
                regions.append((int(addr), buf.raw[:got.value], int(mbi.Type), int(mbi.Protect)))
        next_addr = addr + size
        addr = next_addr if next_addr > addr else addr + 0x1000

    kernel32.CloseHandle(h)
    return regions


def find_exact_send_mgr(pid):
    hits = []
    vtable_bytes = struct.pack("<I", SEND_MGR_VTABLE_VA)
    for base_addr, data, mem_type, protect in read_regions(pid):
        pos = 0
        while True:
            pos = data.find(vtable_bytes, pos)
            if pos < 0:
                break

            # Keep object alignment; this removes most accidental 4-byte matches.
            candidate = base_addr + pos
            if candidate & 7 == 0 and pos + OBJ_SIZE <= len(data):
                b = data[pos:pos + OBJ_SIZE]
                valid = (
                    u32(b, 0x00) == SEND_MGR_VTABLE_VA
                    and u32(b, 0x7C) == candidate
                    and u32(b, 0x9C) == candidate
                    and u32(b, 0xCC) == CRYPTO_CFG_A_VA
                    and u32(b, 0xD0) == CRYPTO_CFG_B_VA
                    and u32(b, 0x24) == SEND_BUFFER_CAP
                )
                if valid:
                    hits.append(
                        {
                            "pid": pid,
                            "send_mgr": candidate,
                            "vtable": u32(b, 0x00),
                            "buffer_base": u32(b, 0x1C),
                            "buffer_end": u32(b, 0x20),
                            "buffer_cap": u32(b, 0x24),
                            "crypto_a": u32(b, 0xCC),
                            "crypto_b": u32(b, 0xD0),
                            "mem_type": mem_type,
                            "protect": protect,
                        }
                    )
            pos += 1
    return hits


def main():
    raw = [int(x) for x in sys.argv[1:] if x.isdigit()]
    wait = 0.0
    if "--wait" in sys.argv:
        i = sys.argv.index("--wait")
        if i + 1 < len(sys.argv):
            wait = float(sys.argv[i + 1])

    deadline = time.time() + wait
    while True:
        pids = raw or xajh_pids()
        all_hits = []
        for pid in pids:
            try:
                all_hits.extend(find_exact_send_mgr(pid))
            except OSError as e:
                print(f"pid={pid} ✗ {e}")

        print("send_mgr 精确定位扫描")
        print("pids:", pids)
        print("匹配:", len(all_hits))
        for h in all_hits:
            print(
                f"  pid={h['pid']} send_mgr=0x{h['send_mgr']:08X} "
                f"vt=0x{h['vtable']:08X} cap=0x{h['buffer_cap']:X} "
                f"cfgA=0x{h['crypto_a']:08X} cfgB=0x{h['crypto_b']:08X}"
            )

        if all_hits or time.time() >= deadline:
            if not all_hits:
                print("未找到唯一精确对象；不要使用未校验地址。")
            return
        time.sleep(0.5)


if __name__ == "__main__":
    main()

