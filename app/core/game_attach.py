# -*- coding: utf-8 -*-
"""
Process attach + first-break recon for map / position.

破0 strategy (no hard offsets yet):
1. Attach by PID from dragged HWND
2. Collect module base (xajh.exe)
3. String-scan readable memory for map path / scene markers
4. Heuristic float-triple scan near map hits / host markers for walk position
5. Merge local userdata hints (role/server)
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]


@dataclass
class MapCandidate:
    address: int
    text: str
    encoding: str
    score: int = 0


@dataclass
class PosCandidate:
    address: int
    x: float
    y: float
    z: float
    score: int = 0
    note: str = ""


@dataclass
class AttachResult:
    ok: bool
    pid: int = 0
    hwnd: int = 0
    title: str = ""
    exe_path: str | None = None
    module_base: int | None = None
    module_size: int | None = None
    map_name: str | None = None
    map_name_cn: str | None = None
    map_display: str | None = None
    map_candidates: list[MapCandidate] = field(default_factory=list)
    pos: PosCandidate | None = None
    pos_candidates: list[PosCandidate] = field(default_factory=list)
    role_hint: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class GameAttachSession:
    """Hold an open pymem session for repeated reads."""

    def __init__(self, log: LogFn | None = None):
        self.log = log or (lambda _m: None)
        self.pm = None
        self.pid: int | None = None
        self.exe_path: str | None = None
        self.module_base: int | None = None
        self.module_size: int | None = None

    def close(self) -> None:
        tap_reader = getattr(self, "_chat_tap_reader", None)
        if tap_reader is not None:
            try:
                tap_reader.close()
            except Exception:
                pass
            self._chat_tap_reader = None
        if self.pm is not None:
            try:
                self.pm.close_process()
            except Exception:
                pass
        self.pm = None
        self.pid = None

    def attach(self, pid: int) -> None:
        import pymem

        self.close()
        pm = pymem.Pymem()
        pm.open_process_from_id(pid)
        self.pm = pm
        self.pid = pid
        self.exe_path = None
        self.module_base = None
        self.module_size = None
        try:
            # main module (list_modules yields a generator)
            for mod in pm.list_modules():
                name = (mod.name or "").lower()
                if name == "xajh.exe":
                    self.module_base = int(mod.lpBaseOfDll)
                    self.module_size = int(mod.SizeOfImage)
                    self.exe_path = (
                        getattr(mod, "filename", None)
                        or getattr(mod, "path", None)
                        or getattr(mod, "name", None)
                    )
                    break
            if self.module_base is None:
                for cand in ("base_address", "process_base"):
                    try:
                        v = getattr(pm, cand, None)
                    except Exception:
                        v = None
                    if v is not None:
                        try:
                            self.module_base = int(
                                v.lpBaseOfDll if hasattr(v, "lpBaseOfDll") else v
                            )
                            self.module_size = int(getattr(v, "SizeOfImage", 0) or 0)
                            break
                        except Exception:
                            self.module_base = None
            if self.module_base is None:
                # pymem Toolhelp base can fail on some sessions (64-bit python
                # enumerating a 32-bit target). xajh.exe uses the fixed preferred
                # base 0x400000 (no ASLR), so fall back to it.
                self.module_base = 0x00400000
                self.module_size = 0x03C00000
        except Exception as e:
            self.log(f"module enum warn: {e}")
        self.log(
            f"attached pid={pid} base={hex(self.module_base) if self.module_base else None} "
            f"size={self.module_size}"
        )
        # resolve full path if only basename
        if self.exe_path and "\\" not in self.exe_path and "/" not in self.exe_path:
            try:
                from app.core.win_utils import query_process_image_path

                full = query_process_image_path(pid)
                if full:
                    self.exe_path = full
            except Exception:
                pass
        if not self.exe_path:
            try:
                from app.core.win_utils import query_process_image_path

                self.exe_path = query_process_image_path(pid)
            except Exception:
                pass

    def recon_fast(self) -> AttachResult:
        """
        UI-first recon used by 取句柄 / 解锁工作台.

        Avoids full-process map string scanning (can stall for minutes on large
        heaps and leave the workbench stuck on 附加中…). Prefer plg scene pos
        + role disk hint; live pos polling fills the rest.

        @author by ak
        """
        if self.pm is None or self.pid is None:
            return AttachResult(ok=False, error="not attached")

        result = AttachResult(
            ok=True,
            pid=self.pid,
            exe_path=self.exe_path,
            module_base=self.module_base,
            module_size=self.module_size,
            role_hint=_read_role_hint_from_disk(self.exe_path),
        )
        self.log("recon_fast: skip full map string scan; try plg scene pos")
        # UI path: fail fast (5s + 2s grace) instead of default 5s+30s hang.
        plg_pos = read_host_scene_pos(
            self, log=self.log, timeout_ms=5000, grace_ms=2000
        )
        if plg_pos is not None:
            result.pos = plg_pos
            result.pos_candidates = [plg_pos]
            p = plg_pos
            try:
                note = p.note or ""
                if "scene=" in note:
                    sid = int(note.split("scene=", 1)[1].split()[0].strip(",;"))
                    from app.core.map_names import (
                        format_scene_display,
                        resolve_scene_id,
                    )

                    mid, cn = resolve_scene_id(sid)
                    disp = format_scene_display(sid)
                    if mid or cn:
                        result.map_name = mid or result.map_name
                        result.map_name_cn = cn or result.map_name_cn
                        result.map_display = disp if disp != "-" else result.map_display
                        self.log(
                            f"recon_fast map from scene_id={sid}: "
                            f"{result.map_display!r}"
                        )
            except Exception as e:
                self.log(f"recon_fast scene map resolve warn: {e}")
            self.log(
                f"recon_fast pos: ({p.x:.3f}, {p.y:.3f}, {p.z:.3f}) "
                f"score={p.score} {p.note}"
            )
        else:
            self.log("recon_fast: plg scene pos unavailable (live poll may fill later)")
        return result

    def recon_map_and_pos(self, max_map: int = 12, max_pos: int = 8) -> AttachResult:
        if self.pm is None or self.pid is None:
            return AttachResult(ok=False, error="not attached")

        result = AttachResult(
            ok=True,
            pid=self.pid,
            exe_path=self.exe_path,
            module_base=self.module_base,
            module_size=self.module_size,
            role_hint=_read_role_hint_from_disk(self.exe_path),
        )

        maps = scan_map_strings(self.pm, log=self.log, limit=max_map)
        result.map_candidates = maps
        if maps:
            best = max(maps, key=lambda m: m.score)
            result.map_name = _normalize_map_name(best.text)
            try:
                from app.core.map_names import format_map_display, resolve_map_name

                mid, cn = resolve_map_name(result.map_name)
                result.map_name = mid or result.map_name
                result.map_name_cn = cn
                result.map_display = format_map_display(result.map_name)
            except Exception as e:
                result.map_display = result.map_name
                self.log(f"map name resolve warn: {e}")
            self.log(
                f"map best: {result.map_display or result.map_name!r} "
                f"@ {hex(best.address)} score={best.score}"
            )

        # Prefer authoritative plg::GetCurrentScenePosition; heuristic scan as fallback.
        plg_pos = read_host_scene_pos(self, log=self.log)
        if plg_pos is not None:
            result.pos = plg_pos
            result.pos_candidates = [plg_pos]
            p = plg_pos
            # Override string-scan map with scene_id table (authoritative for open world).
            try:
                note = p.note or ""
                if "scene=" in note:
                    sid = int(note.split("scene=", 1)[1].split()[0].strip(",;"))
                    from app.core.map_names import (
                        format_scene_display,
                        resolve_scene_id,
                    )

                    mid, cn = resolve_scene_id(sid)
                    disp = format_scene_display(sid)
                    if mid or cn:
                        result.map_name = mid or result.map_name
                        result.map_name_cn = cn or result.map_name_cn
                        result.map_display = disp if disp != "-" else result.map_display
                        self.log(
                            f"map from scene_id={sid}: {result.map_display!r}"
                        )
            except Exception as e:
                self.log(f"scene map resolve warn: {e}")
            self.log(
                f"pos best: ({p.x:.3f}, {p.y:.3f}, {p.z:.3f}) @ {hex(p.address)} "
                f"score={p.score} {p.note}"
            )
            return result

        anchors = [m.address for m in maps[:5]]
        poses = scan_position_candidates(self.pm, anchors=anchors, log=self.log, limit=max_pos)
        result.pos_candidates = poses
        if poses:
            result.pos = max(poses, key=lambda p: p.score)
            p = result.pos
            self.log(
                f"pos best: ({p.x:.3f}, {p.y:.3f}, {p.z:.3f}) @ {hex(p.address)} score={p.score} {p.note}"
            )
        return result


def _read_role_hint_from_disk(exe_path: str | None) -> dict:
    hint: dict = {}
    try:
        if not exe_path:
            return hint
        # .../bin/xajh.exe -> game root
        root = Path(exe_path).resolve().parent.parent
        role = root / "userdata" / "currentrole.ini"
        server = root / "userdata" / "currentserver.ini"
        if role.exists():
            text = role.read_text(encoding="gbk", errors="replace")
            for line in text.splitlines():
                if "=" in line and not line.strip().startswith("["):
                    k, v = line.split("=", 1)
                    hint[k.strip()] = v.strip()
        if server.exists():
            text = server.read_text(encoding="gbk", errors="replace")
            for line in text.splitlines():
                if "=" in line and not line.strip().startswith("["):
                    k, v = line.split("=", 1)
                    hint[f"server_{k.strip()}"] = v.strip()
    except Exception:
        pass
    return hint


def _normalize_map_name(text: str) -> str:
    t = text.replace("/", "\\")
    # maps\xxx\yyy.ecwld -> xxx or full relative
    m = re.search(r"(?i)maps\\([^\\]+)", t)
    if m:
        return m.group(1)
    m = re.search(r"(?i)([^\\/]+)\.ecwld", t)
    if m:
        return m.group(1)
    return t.strip("\x00").strip()


_MAP_ASCII_RE = re.compile(
    rb"(?i)(?:maps[\\/][A-Za-z0-9_\-]+(?:[\\/][A-Za-z0-9_\.\-]+)*|scene_id|SceneID|SelfEnterScene|scene_now)"
)
_MAP_PATH_RE = re.compile(rb"(?i)maps[\\/][A-Za-z0-9_\-\\/\.]{2,80}")


_MAP_NOISE = (
    "shadowmap",
    "normalmap",
    "bloommap",
    "alphamap",
    "mipmap",
    "colormapping",
    "oldlens",
    "surfaces\\",
    "textures\\",
    "models\\",
    ".dds",
    ".tga",
    ".bmp",
    "shaders",
    ".aspx",
    "http:",
    "https:",
    "default.aspx",
    "geotager",
    "worldmap",
    "maps/default",
    "maps\\default",
)

# scene folder / file id used by this client (d10_1, x59, a01_2, ...)
_MAP_ID_RE = re.compile(rb"(?<![A-Za-z0-9_])([a-zA-Z]\d{1,2}(?:_\d{1,2})?)(?![A-Za-z0-9_])")
_MAP_PATH_GOOD_RE = re.compile(
    rb"(?i)maps[\\/]([A-Za-z0-9_\-]{1,32})(?:[\\/]([A-Za-z0-9_\.\-]{1,64}))?"
)


def _known_map_ids() -> set[str]:
    """Known map ids from InstInfo table (lowercase). @author by ak"""
    try:
        from app.core.map_names import load_map_names

        return {k.lower() for k in load_map_names().keys()}
    except Exception:
        return set()


def scan_map_strings(pm, log: LogFn | None = None, limit: int = 12) -> list[MapCandidate]:
    """
    Scan process memory for current/nearby map path or id strings.

    Live map paths often sit past the first few hundred VA regions; scan deeper
    and also accept bare known map ids (e.g. d10_1) when full maps\\ paths are absent.
    @author by ak
    """
    log = log or (lambda _m: None)
    import pymem.memory

    candidates: list[MapCandidate] = []
    seen_text: set[str] = set()
    known = _known_map_ids()
    id_hits: dict[str, tuple[int, int]] = {}  # id -> (addr, count)

    # 1200 regions covers large heaps where runtime map paths live
    regions = list(_iter_readable_regions(pm, max_regions=1200))
    log(f"scan map strings in {len(regions)} regions")

    for base, size in regions:
        read_size = min(size, 2 * 1024 * 1024)
        try:
            data = pymem.memory.read_bytes(pm.process_handle, base, read_size)
        except Exception:
            continue

        # ASCII maps\... paths
        for m in _MAP_PATH_GOOD_RE.finditer(data):
            raw = m.group()
            try:
                text = raw.decode("ascii", "ignore")
            except Exception:
                continue
            text = text.strip("\x00")
            if len(text) < 6 or text.lower() in seen_text:
                continue
            low = text.lower()
            if any(x in low for x in _MAP_NOISE):
                continue
            folder = (m.group(1) or b"").decode("ascii", "ignore").lower()
            # skip tiny non-id folders like "x64" unless known
            if folder and known and folder not in known and not re.fullmatch(r"[a-z]\d+(?:_\d+)?", folder):
                if ".ecwld" not in low and ".dis" not in low:
                    continue
            seen_text.add(low)
            score = 10
            if ".ecwld" in low:
                score += 40
            if ".dis" in low and ".disb" not in low:
                score += 25
            if re.search(r"maps[\\/][^\\/]+[\\/]", low):
                score += 12
            if re.fullmatch(r"maps[\\/][a-z0-9_\-]+", low):
                score += 8
            if folder and folder in known:
                score += 35
            elif folder and re.fullmatch(r"[a-z]\d+(?:_\d+)?", folder):
                score += 15
            candidates.append(
                MapCandidate(address=base + m.start(), text=text, encoding="ascii", score=score)
            )

        # UTF-16LE maps\...
        needle = "maps\\".encode("utf-16le")
        start = 0
        while True:
            idx = data.find(needle, start)
            if idx < 0:
                break
            chunk = data[idx : idx + 160]
            try:
                text = chunk.decode("utf-16le", "ignore").split("\x00", 1)[0]
            except Exception:
                start = idx + 2
                continue
            text = text.strip()
            low = text.lower()
            if len(text) >= 6 and low not in seen_text and not any(x in low for x in _MAP_NOISE):
                seen_text.add(low)
                score = 18
                if ".ecwld" in low:
                    score += 20
                mid = _normalize_map_name(text).lower()
                if mid in known:
                    score += 30
                candidates.append(
                    MapCandidate(address=base + idx, text=text, encoding="utf-16le", score=score)
                )
            start = idx + 2

        # bare known map ids (d10_1 / x59) — count density; runtime often keeps current id
        if known:
            for m in _MAP_ID_RE.finditer(data):
                mid = m.group(1).decode("ascii", "ignore").lower()
                if mid not in known:
                    continue
                addr = base + m.start()
                prev = id_hits.get(mid)
                if prev is None:
                    id_hits[mid] = (addr, 1)
                else:
                    id_hits[mid] = (prev[0], prev[1] + 1)

        if len(candidates) >= limit * 8 and len(id_hits) >= 8:
            # keep scanning a bit for better scores, but avoid endless work
            if base > 0x20000000:
                break

    # promote bare ids only as weak fallback (path/.ecwld hits already preferred)
    path_names = {
        _normalize_map_name(c.text).lower()
        for c in candidates
        if c.encoding in ("ascii", "utf-16le")
    }
    strong_path = any(c.score >= 50 for c in candidates)
    for mid, (addr, cnt) in id_hits.items():
        if mid in seen_text or mid in path_names:
            continue
        # short xN / a1 style ids flood PE/static tables — need density + shape
        if "_" not in mid and len(mid) <= 3 and cnt < 12:
            continue
        if cnt < 4:
            continue
        # when we already have strong path hits, bare ids are noise
        if strong_path:
            continue
        score = 8 + min(cnt, 20)
        if "_" in mid:
            score += 10
        candidates.append(
            MapCandidate(address=addr, text=mid, encoding="id", score=score)
        )
        seen_text.add(mid)

    candidates.sort(key=lambda c: c.score, reverse=True)
    best_by_name: dict[str, MapCandidate] = {}
    for c in candidates:
        name = _normalize_map_name(c.text).lower()
        if not name or name in ("maps", "default", "x64"):
            continue
        if name not in best_by_name or c.score > best_by_name[name].score:
            best_by_name[name] = c
    out = sorted(best_by_name.values(), key=lambda c: c.score, reverse=True)[:limit]
    log(f"map candidates: {len(out)}")
    return out


# xajh.exe carries IMAGE_FILE_LARGE_ADDRESS_AWARE: its 32-bit user space can
# reach ~4 GB on x64 Windows and live heaps (e.g. fly_mgr=0x82990038) may sit
# past the classic 2 GB line. Region iteration is split in two phases so the
# cheap classic-low walk stays first and the high window is only entered while
# the caller's quota still allows it.
REGION_LIMIT_CLASSIC = 0x7FFF0000
REGION_LIMIT_FULL = 0xFFFF0000


def _iter_regions_pm(pm, max_regions, hit_mask, mask_name):
    import pymem.memory

    remaining = int(max_regions)
    for lo, hi in (
        (0x10000, REGION_LIMIT_CLASSIC),
        (REGION_LIMIT_CLASSIC, REGION_LIMIT_FULL),
    ):
        if remaining <= 0:
            return
        address = lo
        while address < hi and remaining > 0:
            try:
                mbi = pymem.memory.virtual_query(pm.process_handle, address)
            except Exception:
                break
            size = int(getattr(mbi, "RegionSize", 0) or 0x1000)
            protect = int(getattr(mbi, "Protect", 0) or 0)
            state = int(getattr(mbi, "State", 0) or 0)
            base = int(getattr(mbi, "BaseAddress", address) or address)
            nxt = base + size
            if nxt <= address or nxt > REGION_LIMIT_FULL:
                break
            # MEM_COMMIT = 0x1000
            if state == 0x1000 and size > 0:
                if protect & hit_mask and not (protect & 0x101):
                    yield base, size
                    remaining -= 1
            address = nxt


def _iter_readable_regions(pm, max_regions: int = 400):
    """Yield readable regions: classic heap first, then LAA high window."""
    # readable-ish protections; skip PAGE_NOACCESS/PAGE_GUARD
    yield from _iter_regions_pm(pm, max_regions, 0xEE, "readable")




def _iter_writable_regions(pm, max_regions: int = 800):
    """Yield writable heap-ish regions; classic first then LAA window. @author by ak"""
    # writable: PAGE_READWRITE/WRITECOPY/EXECUTE_READWRITE/EXECUTE_WRITECOPY
    yield from _iter_regions_pm(pm, max_regions, 0xCC, "writable")


def scan_position_candidates(
    pm,
    anchors: list[int] | None = None,
    log: LogFn | None = None,
    limit: int = 8,
    *,
    dedup_values: bool = True,
    prefer_writable: bool = True,
    max_regions: int = 400,
    step: int = 4,
) -> list[PosCandidate]:
    """
    Heuristic: find float triples that look like world coordinates.

    dedup_values=True (default UI list): collapse near-identical xyz.
    dedup_values=False (diff lock pool): keep unique addresses so re-read can detect movement.
    @author by ak
    """
    log = log or (lambda _m: None)
    import pymem.memory

    anchors = anchors or []
    cands: list[PosCandidate] = []

    # 1) scan windows around map string anchors (need stronger score; path-adjacent floats are often junk)
    for a in anchors:
        for delta in range(-0x200, 0x400, 4):
            addr = a + delta
            if addr <= 0:
                continue
            pos = _read_float3(pm, addr)
            if pos is None:
                continue
            x, y, z = pos
            sc = _score_pos(x, y, z)
            if sc < 12:
                continue
            # reject near-origin graphics constants next to asset paths
            if max(abs(x), abs(y), abs(z)) < 5.0:
                continue
            cands.append(PosCandidate(address=addr, x=x, y=y, z=z, score=sc + 3, note="near_map_str"))

    # 2) prefer writable heaps (live player pos); fall back to readable
    region_iter = (
        _iter_writable_regions(pm, max_regions=max_regions)
        if prefer_writable
        else _iter_readable_regions(pm, max_regions=max_regions)
    )
    scanned = 0
    for base, size in region_iter:
        if size < 0x1000 or size > 8 * 1024 * 1024:
            continue
        if base < 0x01000000:
            continue
        # skip image/static-ish low module range where constants cluster
        if base < 0x02000000 and size < 0x10000:
            continue
        read_size = min(size, 512 * 1024)
        try:
            data = pymem.memory.read_bytes(pm.process_handle, base, read_size)
        except Exception:
            continue
        scanned += 1
        for off in range(0, len(data) - 12, max(4, int(step))):
            try:
                x, y, z = struct.unpack_from("<fff", data, off)
            except struct.error:
                continue
            sc = _score_pos(x, y, z)
            if sc < 10:
                continue
            cands.append(
                PosCandidate(
                    address=base + off,
                    x=x,
                    y=y,
                    z=z,
                    score=sc,
                    note="writable_scan" if prefer_writable else "region_scan",
                )
            )
        # address-stable pool needs more hits; UI list can stop earlier
        need_raw = limit * (80 if not dedup_values else 30)
        if scanned >= (180 if not dedup_values else 80) and len(cands) >= need_raw:
            break

    cands.sort(key=lambda p: p.score, reverse=True)
    uniq: list[PosCandidate] = []
    seen_addr: set[int] = set()
    for p in cands:
        if p.address in seen_addr:
            continue
        if dedup_values and any(
            abs(p.x - u.x) < 0.05 and abs(p.y - u.y) < 0.05 and abs(p.z - u.z) < 0.05 for u in uniq
        ):
            continue
        seen_addr.add(p.address)
        uniq.append(p)
        if len(uniq) >= limit:
            break
    log(f"pos candidates: {len(uniq)} (raw={len(cands)}, regions={scanned})")
    return uniq


def read_host_scene_pos(
    session,
    log: LogFn | None = None,
    *,
    timeout_ms: int = 5000,
    grace_ms: int | None = None,
) -> PosCandidate | None:
    """
    Read live host position via plg::GetCurrentScenePosition (authoritative).

    address=0 means export-backed (not a memory float3 slot).
    grace_ms/timeout_ms: pass through for UI-first recon (avoid 30s grace stall).
    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        from app.core.automove import read_scene_position

        r = read_scene_position(
            session,
            log=log,
            timeout_ms=int(timeout_ms),
            grace_ms=grace_ms,
        )
        if not r.ok or not r.scene_pos:
            return None
        x, y, z = r.scene_pos
        note = f"plg_scene scene={r.scene_id}"
        return PosCandidate(address=0, x=float(x), y=float(y), z=float(z), score=100, note=note)
    except Exception as e:
        log(f"read_host_scene_pos warn: {e}")
        return None


def diff_lock_positions(
    pm,
    log: LogFn | None = None,
    wait_sec: float = 3.0,
    pool_limit: int = 400,
    move_min: float = 0.08,
    move_max: float = 800.0,
    session=None,
) -> list[PosCandidate]:
    """
    Lock host position.

    Preferred: two plg::GetCurrentScenePosition samples (walk between).
    Fallback: address-stable float3 pool re-read in writable heaps.
    @author by ak
    """
    import time

    log = log or (lambda _m: None)

    # --- authoritative path ---
    if session is not None:
        p1 = read_host_scene_pos(session, log=log)
        if p1 is not None:
            log(f"diff plg sample1=({p1.x:.3f},{p1.y:.3f},{p1.z:.3f}), wait {wait_sec:.1f}s — walk")
            time.sleep(max(0.5, float(wait_sec)))
            p2 = read_host_scene_pos(session, log=log)
            if p2 is None:
                log("diff hits=0 (plg second sample failed)")
                return []
            dist = ((p2.x - p1.x) ** 2 + (p2.y - p1.y) ** 2 + (p2.z - p1.z) ** 2) ** 0.5
            p2.note = f"plg_moved_{dist:.2f}"
            p2.score = int(100 + min(dist, 50))
            log(f"diff hits=1 plg dist={dist:.3f} pos=({p2.x:.3f},{p2.y:.3f},{p2.z:.3f})")
            # always return plg pos as locked host even if still (user may not have walked)
            if dist < move_min:
                p2.note = f"plg_static_{dist:.2f}"
                log("note: little/no movement; still using plg scene pos as host")
            return [p2]

    # --- heuristic fallback ---
    first = scan_position_candidates(
        pm,
        anchors=[],
        log=log,
        limit=pool_limit,
        dedup_values=False,
        prefer_writable=True,
        max_regions=600,
        step=4,
    )
    if not first:
        first = scan_position_candidates(
            pm,
            anchors=[],
            log=log,
            limit=pool_limit,
            dedup_values=False,
            prefer_writable=False,
            max_regions=500,
            step=4,
        )
    log(f"diff pool size={len(first)}, wait {wait_sec:.1f}s — walk the character")
    snap = {p.address: (p.x, p.y, p.z, p.score) for p in first}
    time.sleep(max(0.5, float(wait_sec)))

    moved: list[PosCandidate] = []
    for addr, (x0, y0, z0, sc0) in snap.items():
        pos = _read_float3(pm, addr)
        if pos is None:
            continue
        x1, y1, z1 = pos
        dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5
        if move_min < dist < move_max:
            score = int(sc0 + 20 + min(dist, 50))
            moved.append(
                PosCandidate(
                    address=addr,
                    x=x1,
                    y=y1,
                    z=z1,
                    score=score,
                    note=f"moved_{dist:.2f}",
                )
            )
    moved.sort(key=lambda p: p.score, reverse=True)
    log(f"diff hits={len(moved)}")
    return moved


def _read_float3(pm, addr: int) -> tuple[float, float, float] | None:
    import pymem.memory

    try:
        data = pymem.memory.read_bytes(pm.process_handle, addr, 12)
        return struct.unpack("<fff", data)
    except Exception:
        return None


def _score_pos(x: float, y: float, z: float) -> int:
    # reject nan/inf
    for v in (x, y, z):
        if v != v or abs(v) == float("inf"):
            return 0
    # reject tiny/zero noise and absurd world sizes
    if abs(x) < 0.5 and abs(y) < 0.5 and abs(z) < 0.5:
        return 0
    if abs(x) > 200000 or abs(y) > 200000 or abs(z) > 200000:
        return 0

    # reject denormal / near-zero garbage components common in false positives
    near0 = sum(1 for v in (x, y, z) if abs(v) < 1e-3)
    if near0 >= 2:
        return 0

    score = 1

    def _frac(v: float) -> float:
        return abs(v - float(int(v)))

    # prefer continuous non-integer values (actual walk positions)
    for v in (x, y, z):
        if 0.001 < _frac(v) < 0.999:
            score += 2

    # typical open-world ranges for this client class
    if abs(x) < 50000 and abs(z) < 50000 and abs(y) < 20000:
        score += 4

    # Angelica-like engines often store X/Z ground and Y height
    # Prefer triples where two axes are large-ish and one is height-like
    vals = sorted([abs(x), abs(y), abs(z)])
    if vals[0] < 3000 and vals[1] > 5 and vals[2] > 5:
        score += 3

    # penalize almost-zero third axis (often float noise, not real pos)
    if min(abs(x), abs(y), abs(z)) < 1e-2:
        score -= 6
    if min(abs(x), abs(y), abs(z)) < 1e-4:
        score -= 8

    return score


def attach_and_recon(pid: int, hwnd: int = 0, title: str = "", log: LogFn | None = None) -> AttachResult:
    session = GameAttachSession(log=log)
    try:
        session.attach(pid)
        result = session.recon_map_and_pos()
        result.hwnd = hwnd
        result.title = title
        return result
    except Exception as e:
        return AttachResult(ok=False, pid=pid, hwnd=hwnd, title=title, error=str(e))
    finally:
        # keep closed after one-shot recon; UI may open long-lived session later
        session.close()
