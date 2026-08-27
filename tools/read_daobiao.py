# -*- coding: utf-8 -*-
"""Read the real-time 刀标 (marker) list from xajh.exe memory.

Auto-locates the marker data array (no hardcoded heap address), prints the
current count + full table (TID / name / map position), and saves a timestamped
snapshot so you can compare before/after killing a boss.

用法:
  python tools/read_daobiao.py            # 自动找「初一」角色
  python tools/read_daobiao.py 25360      # 指定 PID
  python tools/read_daobiao.py --json     # 只输出 JSON（便于对比）
  python tools/read_daobiao.py --pick 3    # 选 idx=3 的刀标：打印坐标并自动 HostMove 寻路
  python tools/read_daobiao.py --path 3    # 同上（别名）

@author by ak
"""
import argparse
import ctypes
import json
import struct
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.game_attach import GameAttachSession
from app.core.remote_runtime import kernel32, open_process, read_process

SNAP_DIR = ROOT / ".issues" / "recon"
BASE_CACHE = SNAP_DIR / ".daobiao_base"

# marker TID range for this dungeon (康巴水寨深处, scene 2032)
TID_MIN = 100065
TID_MAX = 100071
REC_STRIDE = 0x24

# Win_WorldMap dialog vtable; marker list is stored at obj+0x3EC (base),
# +0x3F0 (end), +0x3F8 (count). Works for any map (open world too).
DLG_VTABLE_WORLDMAP = 0x129C574
DLG_MARKER_LIST = 0x3EC
DLG_MARKER_END = 0x3F0
DLG_MARKER_COUNT = 0x3F8

VALID_TIDS = set(range(TID_MIN, TID_MAX + 1)) | {100072, 101013, 101015}

# 福州郊外等开放地图的 marker TID（按实际出现添加）
FUZHOU_TIDS = {11033, 44294, 71725, 89208, 44823, 44586, 101042}
VALID_TIDS |= FUZHOU_TIDS

# 无名字段的 marker 的显示名回退
NAME_BY_TID = {
    101013: "余沧海",
    101042: "上官霸刀",
    89208: "福州郊外特产供应商",
}

# 玩家自身标记（大地图列表 idx0 的噪音）默认从输出中排除
PLAYER_SELF_TID = 92717

# 多场景标定表（array mapX/mapY -> 大地图显示坐标 dispX/dispY -> 世界坐标）。
# dispX = dx[0]*mx + dx[1]*my + Tx ; dispY = dy[0]*mx + dy[1]*my + Ty
# worldX = dispX + wx_off ; worldZ = dispY + wz_off ; worldY ≈ 常量
# 线性部分 (dx[0],dx[1],dy[0],dy[1]) 跨缩放不变；平移 (tx,ty) 随缩放/平移漂移。
# 运行时用「稳定显示锚点」自动求解当前平移（无需读缩放/手动缩放）：
# 锚点 = 发送定位得到的 (显示X,显示Y,世界X,世界Z)，显示坐标跨缩放稳定。
# 全部锚点按「发送定位」精确测量（2026-08-17）。
SCENES = {
    # 野人峡谷深处 (scene 2030) —— 已知 TID，尚未完成地图坐标标定。
    2030: {
        "name": "野人峡谷深处",
        "tids": frozenset({101013}),
        "calibrated": False,
        "zoom": None,
        "dx": None,
        "dy": None,
        "wx_off": None,
        "wz_off": None,
        "world_y": 45.3,
        "anchors": [],
    },
    # 康巴水寨深处 (scene 2032)
    2032: {
        "name": "康巴水寨深处",
        "tids": frozenset(range(100065, 100072)),
        "calibrated": True,
        "dx": (0.8105, -0.0004, -272.9),
        "dy": (0.0051, -0.8111, 329.4),
        "wx_off": -255.3,
        "wz_off": 0.6,
        "world_y": 45.3,
        "anchors": [
            (193, 40, -62.15, 40.60),
            (281, 49, 25.65, 49.45),
            (366, 6, 110.88, 6.77),
            (366, -140, 110.55, -139.52),
        ],
    },
    # 东方不败 (scene 2034)
    2034: {
        "name": "东方不败",
        "tids": frozenset({100072}),
        "calibrated": True,
        "dx": (0.2162, -0.7083, 585.8),
        "dy": (-0.8598, 1.0565, 257.8),
        "wx_off": -511.51,
        "wz_off": 0.44,
        "world_y": 40.0,
        "anchors": [
            (407, 174, -104.07, 174.52),
            (368, 117, -143.05, 117.57),
            (486, 73, -25.95, 73.09),
            (511, -25, -0.96, -24.44),
        ],
    },
    # 龙傲天 (scene 2036) —— 未标定（仅 1 锚点: 数组(509.12,451.20)<->显示(-34,-14)<->世界(-290,-14)）
    2036: {
        "name": "龙傲天",
        "tids": frozenset({101015}),
        "calibrated": False,
        "zoom": 0.0744,
        "dx": None,
        "dy": None,
        "wx_off": None,
        "wz_off": None,
        "world_y": 45.3,
    },
    # 福州郊外 (scene 72) —— 大地图开放场景。
    # 标定自 4 只霸刀锚点（2026-08-17），其中 2 只活体经发送定位复核：
    #   idx6 arr(701.27,406.53)<->显示(336,137)<->world(80.62,137.89)
    #   idx7 arr(546.94,503.13)<->显示(89,-12)<->world(-166.86,-11.54)
    # 变换：dispX=1.6004*mx-786.3 ; dispY=-1.5424*my+764.1（当前最小缩放下成立）
    72: {
        "name": "福州郊外",
        "tids": frozenset(FUZHOU_TIDS),
        "calibrated": True,
        "zoom": 0.0015,
        "dx": (1.6004, 0.0, -786.3),
        "dy": (0.0, -1.5424, 764.1),
        "wx_off": -255.4,
        "wz_off": 0.45,
        "world_y": 30.0,
        "anchors": [],
    },
}

# 由 TID 反查场景（用于自动判定）
TID_TO_SCENE = {}
for _sid, _s in SCENES.items():
    for _t in _s["tids"]:
        TID_TO_SCENE[_t] = _sid


def find_game_pid(explicit: int | None = None) -> int | None:
    """Pick the xajh process titled 「初一」, else the single xajh, else explicit."""
    if explicit:
        return int(explicit)
    import psutil

    cands = []
    for p in psutil.process_iter(["name", "pid", "cmdline"]):
        if (p.info.get("name") or "").lower() != "xajh.exe":
            continue
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
        except Exception:
            cmd = ""
        title = ""
        try:
            hwnd = p.windows[0] if p.windows else 0
            if hwnd:
                title = _win_title(hwnd)
        except Exception:
            pass
        cands.append((p.info["pid"], cmd + "|" + title))
    if not cands:
        return None
    for pid, info in cands:
        if "初一" in info:
            return pid
    return cands[0][0] if len(cands) == 1 else None


def _win_title(hwnd: int) -> str:
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    n = u.GetWindowTextLengthW(hwnd)
    if not n:
        return ""
    b = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, b, n + 1)
    return b.value


def rd(h, a, n):
    try:
        value = int(a, 0) if isinstance(a, str) else int(a)
        return read_process(h, value & 0xFFFFFFFF, int(n))
    except Exception:
        return b""


def u32(h, a):
    raw = rd(h, a, 4)
    return struct.unpack_from("<I", raw)[0] if len(raw) == 4 else 0


def f32(h, a):
    raw = rd(h, a, 4)
    return struct.unpack_from("<f", raw)[0] if len(raw) == 4 else 0.0


def wstr(h, a, m=40):
    if not (0x10000 < a < 0x7FFE0000):
        return ""
    raw = rd(h, a, m * 2)
    out = []
    for i in range(0, len(raw) - 1, 2):
        c = struct.unpack_from("<H", raw, i)[0]
        if c == 0:
            break
        if c < 0x20 or (0xD800 <= c <= 0xDFFF):
            return ""
        out.append(chr(c))
    return "".join(out)


def valid_record(h, a) -> bool:
    a = int(a, 0) if isinstance(a, str) else int(a)
    tid = u32(h, a)
    if tid not in VALID_TIDS:
        return False
    np_ = u32(h, a + 4)
    mx, my = f32(h, a + 8), f32(h, a + 0xC)
    # Open-world Fuzhou markers often have a null/garbage name pointer;
    # TID plus valid map coordinates and rect is the authoritative shape.
    if tid in FUZHOU_TIDS:
        if not (0 < mx < 3000 and 0 < my < 3000):
            return False
        return all(u32(h, a + off) <= 3000 for off in (0x14, 0x18, 0x1C, 0x20))
    if not (0x10000 < np_ < 0x7FFE0000):
        return False
    nm = wstr(h, np_, 20)
    if not (2 <= len(nm) <= 6):
        return False
    if not (0 < mx < 3000 and 0 < my < 3000):
        return False
    # 龙傲天(BOSS自身在中心) 只有 3~4 只且名字指针常为空，不做名字/rect 校验
    if tid == 101015:
        return True
    for r in (u32(h, a + 0x14), u32(h, a + 0x18), u32(h, a + 0x1C), u32(h, a + 0x20)):
        if r > 3000:
            return False
    return True


def solve_scene_translation(scene, rows):
    """Auto-solve the current array->display translation from stable display anchors.

    The linear part (dx[0],dx[1],dy[0],dy[1]) is zoom-invariant, only the
    translation constants shift with zoom/pan. Anchor display coords are stable,
    so we brute-force the translation that makes the anchor display set appear
    among the live markers. Returns (tx, ty) or None if <2 anchors matched.
    @author by ak
    """
    anchors = scene.get("anchors") or []
    if len(anchors) < 2 or not rows:
        return None
    dx, dy = scene["dx"], scene["dy"]
    adisp = [(a[0], a[1]) for a in anchors]
    best, best_n = None, 0
    for r in rows:
        mx, my = r["mapX"], r["mapY"]
        for ax, ay in adisp:
            tx = ax - (dx[0] * mx + dx[1] * my)
            ty = ay - (dy[0] * mx + dy[1] * my)
            n = 0
            for gax, gay in adisp:
                for rr in rows:
                    ddx = dx[0] * rr["mapX"] + dx[1] * rr["mapY"] + tx
                    ddy = dy[0] * rr["mapX"] + dy[1] * rr["mapY"] + ty
                    if abs(ddx - gax) < 0.7 and abs(ddy - gay) < 0.7:
                        n += 1
                        break
            if n > best_n:
                best_n, best = n, (tx, ty)
    return best if best_n >= 2 else None


def read_block(h, base, max_records=30, validate_tid=True):
    rows = []
    for i in range(max_records):
        a = base + i * REC_STRIDE
        tid = u32(h, a)
        np_ = u32(h, a + 4)
        if tid == 0 and np_ == 0:
            break
        if validate_tid and tid not in VALID_TIDS:
            break
        sid = TID_TO_SCENE.get(tid)
        rows.append({
            "idx": i,
            "addr": hex(a),
            "tid": tid,
            "name": wstr(h, np_, 30) if (0x10000 < np_ < 0x7FFE0000) else "?",
            "mapX": round(f32(h, a + 8), 2),
            "mapY": round(f32(h, a + 0xC), 2),
            "rect": [u32(h, a + 0x14), u32(h, a + 0x18), u32(h, a + 0x1C), u32(h, a + 0x20)],
            "scene_id": sid,
            "dispX": None, "dispY": None,
            "worldX": None, "worldZ": None, "worldY": None,
            "trans": None,
        })
    by_scene = {}
    for r in rows:
        sc = SCENES.get(r["scene_id"])
        if sc is not None and sc.get("calibrated"):
            by_scene.setdefault(r["scene_id"], []).append(r)
    for sid, srows in by_scene.items():
        sc = SCENES[sid]
        dx, dy = sc["dx"], sc["dy"]
        T = solve_scene_translation(sc, srows)
        if T is not None:
            tx, ty = T
            trans = "auto"
        else:
            tx, ty = dx[2], dy[2]
            trans = "calib"
        for r in srows:
            mx, my = r["mapX"], r["mapY"]
            r["dispX"] = round(dx[0] * mx + dx[1] * my + tx, 1)
            r["dispY"] = round(dy[0] * mx + dy[1] * my + ty, 1)
            r["worldX"] = round(r["dispX"] + sc["wx_off"], 1)
            r["worldZ"] = round(r["dispY"] + sc["wz_off"], 1)
            r["worldY"] = sc.get("world_y", 45.3)
            r["trans"] = trans
    return rows


def find_worldmap_marker_list(h) -> tuple[int, int]:
    """Locate the Win_WorldMap marker list via the dialog object.

    Returns (list_base, count) or (0, 0). The dialog vtable is known and the
    list pointer/length live at fixed offsets — works for any map (dungeon or
    open world) without a TID whitelist.
    @author by ak
    """
    import ctypes
    from ctypes import wintypes

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", wintypes.LPVOID), ("AllocationBase", wintypes.LPVOID),
                    ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.DWORD),
                    ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                    ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
    vtpat = struct.pack("<I", DLG_VTABLE_WORLDMAP)
    addr = 0
    while addr < 0x7FFE0000:
        mbi = MBI()
        if not kernel32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(MBI)):
            break
        base = int(mbi.BaseAddress or 0); size = int(mbi.RegionSize or 0)
        if size == 0:
            break
        if mbi.State == 0x1000 and (mbi.Protect & 0xFF) in (0x02, 0x04, 0x40) and mbi.Type != 0x1000000:
            off = 0
            while off < size:
                n = min(1 << 22, size - off)
                buf = ctypes.create_string_buffer(n)
                got = ctypes.c_size_t(0)
                if kernel32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, n, ctypes.byref(got)) and got.value:
                    data = buf.raw[:got.value]
                    i = 0
                    while True:
                        j = data.find(vtpat, i)
                        if j < 0:
                            break
                        obj = base + off + j
                        lst = u32(h, obj + DLG_MARKER_LIST)
                        cnt = u32(h, obj + DLG_MARKER_COUNT)
                        if lst and 0 < cnt < 200:
                            return lst, int(cnt)
                        i = j + 1
                off += n
        addr = base + size
    return 0, 0


def _probe_scene_tids(pid: int | None) -> frozenset | None:
    """Current scene's marker TID set via the bridge, or None if unavailable. @author by ak"""
    if not pid:
        return None
    try:
        from app.core.remote_runtime import _probe_pid_scene_snapshot, get_pid_scene_snapshot
        if _probe_pid_scene_snapshot(pid):
            sid = get_pid_scene_snapshot(pid).get("scene_id")
            sc = SCENES.get(sid)
            if sc:
                return sc["tids"]
    except Exception:
        pass
    return None


def _scene_match_count(rows, scene_tids) -> int:
    """How many records carry a TID of the given scene (None => any). @author by ak"""
    if not rows:
        return 0
    if scene_tids is None:
        return len(rows)
    return sum(1 for r in rows if int(r["tid"]) in scene_tids)


def _read_cache() -> tuple[int, int]:
    """Read cached (base, count); tolerant of the old base-only format. @author by ak"""
    if not BASE_CACHE.exists():
        return 0, 0
    try:
        parts = BASE_CACHE.read_text(encoding="utf-8").split()
        base = int(parts[0], 16)
        cnt = int(parts[1]) if len(parts) > 1 else 30
        return base, cnt
    except Exception:
        return 0, 0


def locate_base(h, pid: int | None = None) -> tuple[int, int]:
    """Find (marker list base, count) for the CURRENT map.

    Priority: (1) a cached base whose records match the current scene; (2) the
    Win_WorldMap dialog list (authoritative count, includes the player marker,
    ~1.5s vtable scan) validated against the current scene; (3) an in-buffer
    TID scan limited to the current scene's TIDs as a stale-dialog fallback.
    A stale array left over from a previous map can never win because every
    candidate is scored by how many records match the current scene.
    @author by ak
    """
    scene_tids = _probe_scene_tids(pid)
    sids = scene_tids or VALID_TIDS

    def _check(cand: tuple[int, int]) -> tuple[bool, int, int]:
        """(ok, scene-match count over the kept run, record span length)."""
        try:
            rows = read_block(h, cand[0], max_records=cand[1] or 30, validate_tid=False)
        except Exception:
            return False, 0, 0
        if not rows:
            return False, 0, 0
        start = 0
        if rows[0]["tid"] not in sids:      # leading player marker / noise
            start = 1
        i = start
        while i < len(rows) and rows[i]["tid"] in sids and valid_record(h, rows[i]["addr"]):
            i += 1
        if i <= start:
            return False, 0, 0
        return True, i - start, i

    def _write_cache(b, c):
        BASE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        BASE_CACHE.write_text("0x%X %d" % (b, c), encoding="utf-8")

    # 1) cached base fast path (scene-validated, exact count)
    cb, cc = _read_cache()
    if cb:
        ok, match, length = _check((cb, cc))
        if match > 0:
            _write_cache(cb, length)
            return cb, length or 30

    # 2) dialog list (authoritative; includes player marker record)
    lst, cnt = find_worldmap_marker_list(h)
    if lst and cnt:
        ok, match, length = _check((lst, int(cnt)))
        if ok and (scene_tids is None or match > 0):
            _write_cache(lst, int(cnt))
            return lst, length or int(cnt)

    # 3) single TID scan limited to the current scene's TIDs (or the full
    #    whitelist when the scene is unknown). Per chunk, record every hit
    #    position, then only hits whose previous slot (0x24 back) is not also
    #    a hit can be run starts — no process reads for mid-run hits.
    scan_tids = scene_tids or VALID_TIDS
    import ctypes
    from ctypes import wintypes

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", wintypes.LPVOID), ("AllocationBase", wintypes.LPVOID),
                    ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.DWORD),
                    ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                    ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]

    MEM_COMMIT = 0x1000
    CHUNK = 1 << 22
    starts: dict[int, int] = {}
    cands: list[tuple[int, int]] = []
    addr = 0
    while addr < 0x7FFE0000:
        mbi = MBI()
        r = kernel32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(MBI))
        if not r:
            break
        base = int(mbi.BaseAddress or 0)
        size = int(mbi.RegionSize or 0)
        if size == 0:
            break
        if mbi.State == MEM_COMMIT and (mbi.Protect & 0xFF) in (0x02, 0x04, 0x40) and mbi.Type != 0x1000000:
            off = 0
            while off < size:
                n = min(CHUNK, size - off)
                buf = ctypes.create_string_buffer(n)
                got = ctypes.c_size_t(0)
                ok = kernel32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, n, ctypes.byref(got))
                if ok and got.value:
                    data = buf.raw[:got.value]
                    pos = set()
                    for tid in scan_tids:
                        pat = struct.pack("<I", tid)
                        s = 0
                        while True:
                            i = data.find(pat, s)
                            if i < 0:
                                break
                            pos.add(i)
                            s = i + 1
                    for i in sorted(pos):
                        if (i - REC_STRIDE) in pos:
                            continue  # mid-run hit
                        a = base + off + i
                        while valid_record(h, a - REC_STRIDE):
                            a -= REC_STRIDE
                        c = len(read_block(h, a))
                        if c:
                            starts.setdefault(a, 0)
                            if c > starts[a]:
                                starts[a] = c
                off += n
        addr = base + size
    cands = list(starts.items())
    if cands:
        cands.sort(key=lambda c: _check(c)[1], reverse=True)
        best_base, best_cnt = cands[0]
        ok, _, length = _check(cands[0])
        if ok:
            _write_cache(best_base, length or best_cnt or 30)
            return best_base, length or best_cnt or 30
    return 0, 0


def sess_scene_mode(sess, _row) -> int:
    """Current scene id as HostMove mode. @author by ak"""
    try:
        from app.core.automove import read_scene_position
        sp = read_scene_position(sess, log=lambda _m: None)
        if sp.ok and sp.scene_id is not None:
            return int(sp.scene_id)
    except Exception:
        pass
    return 0


def read_worldmap_zoom(pid: int) -> float | None:
    """Read current Win_WorldMap zoom float (+0x344) for pid. @author by ak"""
    import ctypes
    from ctypes import wintypes
    try:
        h = open_process(pid)
        try:
            # find dlg object whose vtable == 0x129C574 (Win_WorldMap)
            vtpat = struct.pack("<I", 0x129C574)

            class MBI(ctypes.Structure):
                _fields_ = [("BaseAddress", wintypes.LPVOID), ("AllocationBase", wintypes.LPVOID),
                            ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.DWORD),
                            ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                            ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
            addr = 0
            while addr < 0x7FFE0000:
                mbi = MBI()
                if not kernel32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(MBI)):
                    break
                base = int(mbi.BaseAddress or 0); size = int(mbi.RegionSize or 0)
                if size == 0:
                    break
                if mbi.State == 0x1000 and (mbi.Protect & 0xFF) in (0x02, 0x04, 0x40) and mbi.Type != 0x1000000:
                    off = 0
                    while off < size:
                        n = min(1 << 22, size - off)
                        buf = ctypes.create_string_buffer(n)
                        got = ctypes.c_size_t(0)
                        if kernel32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, n, ctypes.byref(got)) and got.value:
                            data = buf.raw[:got.value]
                            i = 0
                            while True:
                                j = data.find(vtpat, i)
                                if j < 0:
                                    break
                                obj = base + off + j
                                raw = read_process(h, obj + 0x344, 4)
                                if len(raw) == 4:
                                    return struct.unpack_from("<f", raw)[0]
                                i = j + 1
                        off += n
                addr = base + size
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(h))
    except Exception:
        return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", nargs="?", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--pick", type=int, default=None,
                    help="select marker idx, print display + world coords and auto HostMove to it")
    ap.add_argument("--path", type=int, default=None,
                    help="select marker idx and HostMove to its world coords")
    ap.add_argument("--name", type=str, default=None,
                    help="select marker by name (e.g. 左冷禅/令狐冲/龙傲天); use with --pick/--path")
    ap.add_argument("--tid", type=int, default=None,
                    help="only show markers with this TID (e.g. 101042 for 上官霸刀)")
    args = ap.parse_args()

    pid = find_game_pid(args.pid)
    if pid is None:
        print("未找到 xajh.exe（可显式传 PID）", file=sys.stderr)
        return 1

    sess = GameAttachSession(log=lambda m: None)
    sess.attach(pid)
    h = open_process(pid)
    try:
        base, cnt = locate_base(h, pid)
        if not base:
            print("未找到刀标数据数组（当前可能不在该副本/大地图未开？）", file=sys.stderr)
            return 2
        rows = read_block(h, base, max_records=cnt or 30, validate_tid=False)
        if args.tid is not None:
            rows = [m for m in rows if int(m["tid"]) == int(args.tid)]
        # 默认排除玩家自身标记（噪音），idx 重新编号
        rows = [m for m in rows if int(m["tid"]) != PLAYER_SELF_TID]
        for i, m in enumerate(rows):
            m["idx"] = i
        ts = time.strftime("%Y%m%d_%H%M%S")

        # 自动判定当前场景（取出现最多的 tid 所属场景）
        from collections import Counter
        tid_counts = Counter(m["tid"] for m in rows)
        active_sid = None
        if tid_counts:
            top_tid, _ = tid_counts.most_common(1)[0]
            active_sid = TID_TO_SCENE.get(top_tid)
        zoom_warn = ""
        if active_sid is not None and SCENES.get(active_sid, {}).get("calibrated"):
            sc = SCENES[active_sid]
            has_anchors = len(sc.get("anchors") or []) >= 2
            calib_used = any(m.get("scene_id") == active_sid and m.get("trans") == "calib" for m in rows)
            if has_anchors and calib_used:
                zoom_warn = ("  [警告] 锚点自动匹配失败，已回退标定平移常数，当前缩放下坐标可能漂移；"
                             "需 ≥2 只锚点刀标仍在地图上（用 发送定位 校准）")
        zoom_now = read_worldmap_zoom(pid) if active_sid is not None and zoom_warn else None

        # 选标（按 idx 或 name），用于 --pick / --path
        sel = None
        if args.name:
            for m in rows:
                if m["name"] == args.name:
                    sel = m
                    break
            if sel is None:
                print("未找到名字为 %s 的刀标（当前列表: %s）" % (
                    args.name, "、".join({m["name"] for m in rows if m["name"]})), file=sys.stderr)
                return 3
        elif args.pick is not None or args.path is not None:
            want = args.pick if args.pick is not None else args.path
            for m in rows:
                if int(m["idx"]) == int(want):
                    sel = m
                    break
            if sel is None:
                print("idx=%d 不在当前 0..%d 列表" % (int(want), len(rows) - 1), file=sys.stderr)
                return 3

        if sel is not None:
            dx, dy = sel.get("dispX"), sel.get("dispY")
            wx, wz = sel.get("worldX"), sel.get("worldZ")
            sid = sel.get("scene_id")
            sname = SCENES.get(sid, {}).get("name", "?") if sid is not None else "?"
            if dx is None or wx is None:
                print("该刀标未标定（%s scene %s 需补锚点），无法输出/寻路" % (sname, sid), file=sys.stderr)
                return 4
            disp_str = "%.0f,%.0f" % (dx, dy)
            print("已选 %s tid=%d 显示坐标=%s 世界坐标=(%.2f, %.2f) [%s]" % (
                sel["name"], sel["tid"], disp_str, wx, wz, "auto" if sel.get("trans") == "auto" else "calib"))
            if args.path is not None or args.pick is not None:
                from app.core.automove import PathTarget, host_move_to
                from app.core.remote_runtime import (
                    ensure_pid_remote_callable,
                    wait_pid_scene_stable,
                )
                ensure_pid_remote_callable(pid)
                # 本工具每次是新进程，场景栅栏的首次快照 stable_since=now，
                # 先等足 settle(1.2s) 再发 CRT，否则被 ensure_pid_scene_stable 拒绝。
                wait_pid_scene_stable(pid)
                tgt = PathTarget(x=wx, y=sel.get("worldY", 45.3), z=wz,
                                 mode=int(sess_scene_mode(sess, sel)))
                r = host_move_to(sess, tgt, log=lambda m: print("[automove]", m))
                print("寻路: ok=%s ret=%s %s" % (
                    r.ok, r.ret, r.error or r.note or ""))
            else:
                print("把 %s 输入大地图寻路框即可走位；或加 --pick/--path 自动 HostMove" % disp_str)
            return 0

        snap = {"pid": pid, "time": time.strftime("%H:%M:%S"), "count": len(rows),
                "array_base": hex(base), "scene": {str(k): v["name"] for k, v in SCENES.items()},
                "markers": rows}
        if args.json:
            snap["zoom"] = zoom_now
            snap["scene_id"] = active_sid
            print(json.dumps(snap, ensure_ascii=False))
        else:
            print("PID=%d  刀标数=%d  数组基址=%s" % (pid, len(rows), hex(base)))
            if active_sid is not None:
                print("当前场景: %s (%d)%s" % (SCENES.get(active_sid, {}).get("name", "?"), active_sid, zoom_warn))
            print("（dispX/dispY=大地图显示/寻路输入坐标；worldX/worldZ=世界坐标(HostMove用)）")
            print("%4s %8s %-8s %8s %8s %8s %8s %10s %10s" % (
                "idx", "tid", "name", "mapX", "mapY", "dispX", "dispY", "worldX", "worldZ"))
            for m in rows:
                dx = m.get("dispX"); dy = m.get("dispY")
                wx = m.get("worldX"); wz = m.get("worldZ")
                print("%4d %8d %-8s %8.2f %8.2f %8s %8s %10s %10s" % (
                    m["idx"], m["tid"], m["name"], m["mapX"], m["mapY"],
                    ("%.0f" % dx) if dx is not None else "?",
                    ("%.0f" % dy) if dy is not None else "?",
                    ("%.1f" % wx) if wx is not None else "?",
                    ("%.1f" % wz) if wz is not None else "?"))
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        out = SNAP_DIR / ("daobiao_snap_%s.json" % ts)
        out.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        if not args.json:
            print("快照 -> %s" % out)
        return 0
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(h))
        sess.close()


if __name__ == "__main__":
    raise SystemExit(main())
