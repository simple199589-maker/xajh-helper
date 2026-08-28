import ctypes
import ctypes.wintypes as wt
import struct
import subprocess
import sys
import time
from pathlib import Path

MAGIC = 0x54544D50
STATUS_ACTIVE = 1
EXACT_FOUND = 2

OFF_STATUS = 12
OFF_SEND_MGR = 20
OFF_SEEN = 24
OFF_HIT_SEQ = 156
OFF_HITS = 160
HIT_COUNT = 64
HIT_SIZE = 12
OFF_IDENT_SEEN = 1728
OFF_EXACT_STATUS = 1732
OFF_EXACT_COUNT = 1736
OFF_EXACT_MGR = 1740
OFF_EXACT_SEEN = 1744
NEW_SIZE = 1748

VTABLE_VA = 0x012C7C00
CRYPTO_A_VA = 0x01D61A88
CRYPTO_B_VA = 0x01D61B9C
BUFFER_CAP = 0x8000
OBJ_SIZE = 0xE0

EXACT_NAMES = {
    0: "IDLE", 1: "RUNNING", 2: "FOUND", 3: "MULTIPLE", 4: "NOT_FOUND",
}

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.MapViewOfFile.restype = ctypes.c_void_p
kernel32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]


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
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * wt.MAX_PATH),
    ]


def u32(buf, off):
    return int.from_bytes(buf[off:off + 4], "little")


def xajh_pids():
    snap = kernel32.CreateToolhelp32Snapshot(2, 0)
    if snap == -1:
        return []
    e = PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    out = []
    ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        if e.szExeFile.lower() == "xajh.exe":
            out.append(int(e.th32ProcessID))
        ok = kernel32.Process32NextW(snap, ctypes.byref(e))
    kernel32.CloseHandle(snap)
    return sorted(out)


def paths():
    root = Path(__file__).resolve().parent
    candidates = [
        root,
        root / "native" / "bin",
        root / "_internal" / "native" / "bin",
        root / "runtime" / "native" / "bin",
        root.parent / "native" / "bin",
    ]
    dll = root / "native" / "bin" / "xajh_team_tap.dll"
    injector = None
    for d in candidates:
        p = d / "xajh_inject.exe"
        if p.is_file():
            injector = p
            break
    return dll, injector


def read_tap(pid):
    h = kernel32.OpenFileMappingW(0x0004, False, f"Local\\XajhTeamTap_{pid}")
    if not h:
        return None
    view = kernel32.MapViewOfFile(h, 0x0004, 0, 0, 0)
    if not view:
        kernel32.CloseHandle(h)
        return None
    try:
        mbi = MBI()
        mapped_size = NEW_SIZE
        if kernel32.VirtualQuery(ctypes.c_void_p(view), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            mapped_size = min(NEW_SIZE, int(mbi.RegionSize))
        raw = ctypes.string_at(view, mapped_size)
        if len(raw) < 28 or u32(raw, 0) != MAGIC:
            return {"bad": True, "size": len(raw)}
        declared_size = u32(raw, 8)
        if 28 <= declared_size <= len(raw):
            raw = raw[:declared_size]

        hits = []
        if OFF_HITS + HIT_COUNT * HIT_SIZE <= len(raw):
            seq_base = u32(raw, OFF_HIT_SEQ)
            for i in range(HIT_COUNT):
                p = OFF_HITS + i * HIT_SIZE
                seq, self_, caller = struct.unpack_from("<III", raw, p)
                if seq:
                    hits.append((seq, self_, caller))

        d = {
            "version": u32(raw, 4),
            "struct_size": u32(raw, 8),
            "status": u32(raw, OFF_STATUS),
            "send_mgr": u32(raw, OFF_SEND_MGR),
            "seen": u32(raw, OFF_SEEN),
            "hit_write_seq": u32(raw, OFF_HIT_SEQ),
            "hits": hits,
            "has_exact": declared_size >= NEW_SIZE and OFF_EXACT_SEEN + 4 <= len(raw),
            "exact_status": u32(raw, OFF_EXACT_STATUS) if OFF_EXACT_SEEN + 4 <= len(raw) else 0,
            "exact_count": u32(raw, OFF_EXACT_COUNT) if OFF_EXACT_SEEN + 4 <= len(raw) else 0,
            "exact_mgr": u32(raw, OFF_EXACT_MGR) if OFF_EXACT_SEEN + 4 <= len(raw) else 0,
            "exact_seen": u32(raw, OFF_EXACT_SEEN) if OFF_EXACT_SEEN + 4 <= len(raw) else 0,
        }
        return d
    finally:
        kernel32.UnmapViewOfFile(ctypes.c_void_p(view))
        kernel32.CloseHandle(h)


def inject(pid, dll, injector):
    if not dll.is_file() or not injector.is_file():
        return False, "dll/injector not found"
    try:
        r = subprocess.run(
            [str(injector), str(pid), str(dll.resolve())],
            cwd=str(dll.parent), capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return False, str(e)


def read_regions(pid):
    h = kernel32.OpenProcess(0x0410, False, pid)
    if not h:
        raise OSError(f"OpenProcess failed winerr={ctypes.get_last_error()}")
    regions = []
    addr = 0
    mbi = MBI()
    while addr < 0x7FFF0000:
        if not kernel32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        size = int(mbi.RegionSize)
        if mbi.State == 0x1000 and mbi.Protect not in (0, 1) and 0 < size < 0x10000000:
            buf = (ctypes.c_char * size)()
            got = ctypes.c_size_t()
            if kernel32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
                regions.append((addr, buf.raw[:got.value]))
        nxt = addr + size
        addr = nxt if nxt > addr else addr + 0x1000
    kernel32.CloseHandle(h)
    return regions


def exact_scan(pid):
    pats = struct.pack("<I", VTABLE_VA)
    found = []
    for base, data in read_regions(pid):
        pos = 0
        while True:
            pos = data.find(pats, pos)
            if pos < 0:
                break
            obj = base + pos
            if obj % 8 == 0 and pos + OBJ_SIZE <= len(data):
                b = data[pos:pos + OBJ_SIZE]
                if (u32(b, 0x00) == VTABLE_VA and u32(b, 0x7C) == obj and
                        u32(b, 0x9C) == obj and u32(b, 0xCC) == CRYPTO_A_VA and
                        u32(b, 0xD0) == CRYPTO_B_VA and u32(b, 0x24) == BUFFER_CAP):
                    found.append(obj)
            pos += 1
    return found


def parse_args():
    pids = []
    wait = 5.0
    do_inject = False
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == "--wait":
            i += 1
            wait = float(sys.argv[i])
        elif a == "--inject":
            do_inject = True
        elif a.isdigit():
            pids.append(int(a))
        i += 1
    return pids, wait, do_inject


def main():
    pids, wait, do_inject = parse_args()
    dll, injector = paths()
    deadline = time.time() + wait

    while True:
        test_pids = pids or xajh_pids()
        results = []
        for pid in test_pids:
            tap = read_tap(pid)
            if tap is None and do_inject:
                ok, msg = inject(pid, dll, injector)
                print(f"[{pid}] inject={ok} {msg}")
                if ok:
                    time.sleep(1.0)
                    tap = read_tap(pid)

            if tap is None:
                try:
                    found = exact_scan(pid)
                    results.append((pid, None, found))
                except OSError as e:
                    results.append((pid, None, [], str(e)))
                continue

            scan = []
            scan_error = ""
            if not (tap.get("has_exact") and tap.get("exact_seen")):
                try:
                    scan = exact_scan(pid)
                except OSError as e:
                    scan_error = str(e)
            results.append((pid, tap, scan, scan_error))

        all_ok = True
        print("=== team_tap exact send_mgr test ===")
        for row in results:
            pid = row[0]
            tap = row[1] if len(row) > 1 else None
            scan = row[2] if len(row) > 2 else []
            extra = row[3] if len(row) > 3 else ""
            if tap is None:
                all_ok = False
                if extra:
                    print(f"[{pid}] FAIL no-tap scan-error={extra}")
                elif not scan:
                    print(f"[{pid}] FAIL no-tap exact-object=no")
                else:
                    print(f"[{pid}] WARN no-tap exact-object={hex(scan[0])} (DLL not injected)")
                continue
            if tap.get("bad"):
                all_ok = False
                print(f"[{pid}] FAIL bad-tap size={tap.get('size')}")
                continue

            hits = len(tap.get("hits", []))
            if tap.get("has_exact"):
                main_mgr = tap["send_mgr"]
                exact_mgr = tap["exact_mgr"] or (scan[0] if len(scan) == 1 else 0)
                source = "exact" if tap.get("exact_seen") else "hook"
                ok = (tap["status"] == STATUS_ACTIVE and tap["seen"] == 1 and
                      main_mgr != 0 and tap["exact_seen"] == 1 and
                      tap["exact_status"] == EXACT_FOUND and
                      tap["exact_count"] == 1 and exact_mgr == main_mgr)
                print(
                    f"[{pid}] {'PASS' if ok else 'FAIL'} "
                    f"v={tap['version']} status={tap['status']} "
                    f"mgr=0x{main_mgr:08X} source={source} "
                    f"exact={EXACT_NAMES.get(tap['exact_status'], tap['exact_status'])} "
                    f"count={tap['exact_count']} hook_hits={hits}"
                )
            else:
                main_mgr = tap["send_mgr"]
                ok = (tap["status"] == STATUS_ACTIVE and tap["seen"] == 1 and
                      main_mgr != 0 and len(scan) == 1 and scan[0] == main_mgr)
                print(
                    f"[{pid}] {'PASS-old-protocol' if ok else 'FAIL'} "
                    f"v={tap['version']} size={tap['struct_size']} "
                    f"status={tap['status']} mgr=0x{main_mgr:08X} "
                    f"scan={hex(scan[0]) if len(scan) == 1 else len(scan)} "
                    f"hook_hits={hits}"
                )
            all_ok = all_ok and ok

        print(f"summary: {'PASS' if all_ok and results else 'FAIL'}")
        if time.time() >= deadline:
            return
        time.sleep(1.0)


if __name__ == "__main__":
    main()
