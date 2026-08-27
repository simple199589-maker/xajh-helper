# -*- coding: utf-8 -*-
"""Runtime resident-container reader used by scheduled task flows.

The legacy path intentionally does not use scene-feature/AOI fallback. It consumes the
LiveSceneHub scene when executed in the scheduler process, preserves cached zero-row
containers, and bounds a full-memory miss with --scan-timeout.

The reader uses pure ``ReadProcessMemory`` and discovers the container from
its live array/count fields.  A per-process cache avoids repeating the full
scan while the process remains alive; every cached read is structurally
validated before it is used.

Examples:
    The command-line diagnostics remain in ``tools``; this module contains
    the runtime reader only.
"""
from __future__ import annotations

import ctypes
import argparse
import json
import math
import re
import struct
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from app.core.remote_runtime import kernel32, open_process, read_process

NAME_BY_TID = {
    100065: "左冷禅",
    100066: "林平之",
    100067: "令狐冲",
    100068: "陆柏",
    100069: "丁勉",
    100070: "费彬",
    100071: "乐厚",
    100072: "东方不败",
    101013: "余沧海",
    101015: "龙傲天",
    101042: "上官霸刀",
}
SCENE_BY_TID = {**{tid: 2032 for tid in range(100065, 100072)}, 100072: 2034, 101013: 2030, 101015: 2036, 101042: 72}
CACHE_DIR = ROOT / ".issues" / "recon"


def probe_scene_id(pid: int) -> int | None:
    """Read the current scene once so a cached container cannot cross maps."""
    try:
        from app.core.automove import read_scene_position
        from app.core.game_attach import GameAttachSession
        from app.core.remote_runtime import wait_pid_scene_stable

        wait_pid_scene_stable(pid, timeout_s=8)
        session = GameAttachSession(log=lambda _message: None)
        session.attach(pid)
        try:
            result = read_scene_position(session, log=lambda _message: None, timeout_ms=5000)
            if result.ok and result.scene_id is not None:
                return int(result.scene_id)
        finally:
            session.close()
    except Exception:
        return None
    return None

PTR_MIN = 0x10000
PTR_MAX = 0xFFF00000
MAX_COUNT = 256
KNOWN_TIDS = set(range(100065, 100073)) | {101013, 101015, 101042, 92717}
KNOWN_MONSTER_TIDS = KNOWN_TIDS - {92717}
RESIDENT_CONTAINER_VTABLE = 0x01270DFC
FUZHOU_TIDS = {11033, 44294, 71725, 89208, 44823, 44586, 101042}
SCENE_TID_GROUPS = (
    frozenset(range(100065, 100072)),
    frozenset({100072}),
    frozenset({101013}),
    frozenset({101015}),
    frozenset({101042}),
)


class MemoryBasicInformation(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", wintypes.LPVOID),
        ("AllocationBase", wintypes.LPVOID),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class ResidentReader:
    def __init__(self, pid: int, *, use_cache: bool = True, scene_id: int | None = None, scan_timeout_s: float | None = 12.0):
        self.pid = pid
        self.scene_id = scene_id
        self.scan_timeout_s = scan_timeout_s
        self.scan_timed_out = False
        self.handle = open_process(pid)
        self.cache_path = CACHE_DIR / f".resident_container_{pid}"
        self.cached_container: int | None = self._load_cached_container() if use_cache else None

    def _feature_hint_path(self, scene_id: int) -> Path:
        return CACHE_DIR / f".scene_feature_regions_{self.pid}_{int(scene_id)}.json"

    def _load_feature_regions(self, scene_id: int, available: set[tuple[int, int]]) -> list[tuple[int, int]]:
        try:
            payload = json.loads(self._feature_hint_path(scene_id).read_text(encoding="utf-8"))
            regions = []
            for item in payload.get("regions", []):
                base, size = int(item[0]), int(item[1])
                if (base, size) in available:
                    regions.append((base, size))
            return regions
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []

    def _save_feature_regions(self, scene_id: int, regions: set[tuple[int, int]]) -> None:
        if not regions:
            return
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            payload = {"pid": self.pid, "scene_id": int(scene_id), "regions": [[base, size] for base, size in sorted(regions)]}
            self._feature_hint_path(scene_id).write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    def _load_cached_container(self) -> int | None:
        try:
            value = int(self.cache_path.read_text(encoding="ascii").strip(), 0)
            return value if PTR_MIN <= value < PTR_MAX else None
        except (OSError, ValueError):
            return None

    def _save_cached_container(self, container: int) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(f"0x{container:08X}\n", encoding="ascii")
        except OSError:
            pass

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def read_bytes(self, address: int, size: int) -> bytes | None:
        if not (PTR_MIN <= address < PTR_MAX) or size <= 0:
            return None
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            self.handle,
            ctypes.c_void_p(address & 0xFFFFFFFF),
            buffer,
            size,
            ctypes.byref(read),
        )
        if not ok or read.value != size:
            return None
        return buffer.raw

    def u8(self, address: int) -> int | None:
        data = self.read_bytes(address, 1)
        return data[0] if data else None

    def u32(self, address: int) -> int | None:
        data = self.read_bytes(address, 4)
        return struct.unpack("<I", data)[0] if data else None

    def f32(self, address: int) -> float | None:
        data = self.read_bytes(address, 4)
        return struct.unpack("<f", data)[0] if data else None

    def regions(self, *, writable_only: bool = False):
        address = 0
        while address < PTR_MAX:
            info = MemoryBasicInformation()
            result = kernel32.VirtualQueryEx(
                self.handle,
                ctypes.c_void_p(address),
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not result:
                break
            base = int(info.BaseAddress or 0)
            size = int(info.RegionSize or 0)
            if (
                base
                and size
                and int(info.State) == 0x1000
                and (int(info.Protect) & 0xFF) in (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
                and (not writable_only or (int(info.Protect) & 0xFF) in (0x04, 0x08, 0x40, 0x80))
            ):
                yield base, size
            address = base + size

    def _merged_regions(self):
        pending = None
        for base, size in self.regions(writable_only=True):
            if pending is not None and pending[0] + pending[1] == base:
                pending = (pending[0], pending[1] + size)
            else:
                if pending is not None:
                    yield pending
                pending = (base, size)
        if pending is not None:
            yield pending

    def _decode_entry(self, slot: int, index: int) -> dict | None:
        item = self.u32(slot + 0x08)
        if not item or self.u32(item + 0x01) != 0:
            return None
        target = self.u32(item + 0x05)
        if not target:
            return None
        base = self.u32(target + (0x09 if self.u8(target) == 0 else 0x05))
        if not base:
            return None
        tid = self.u32(base + 0x11)
        x, y, z = self.f32(base + 0x15), self.f32(base + 0x19), self.f32(base + 0x1D)
        if tid is None or any(value is None or not math.isfinite(value) for value in (x, y, z)):
            return None
        if not (-100000.0 < x < 100000.0 and -100000.0 < y < 100000.0 and -100000.0 < z < 100000.0):
            return None
        scene_id = SCENE_BY_TID.get(tid)
        return {
            "idx": index,
            "tid": tid,
            "name": NAME_BY_TID.get(tid, "未知怪物"),
            "scene_id": scene_id,
            "x": x,
            "y": y,
            "z": z,
            "world_x": x,
            "world_y": y,
            "world_z": z,
        }

    def _container_header(self, container: int) -> tuple[int, int] | None:
        header = self.read_bytes(container, 0x20)
        if not header:
            return None
        array = struct.unpack_from("<I", header, 0x10)[0]
        count = struct.unpack_from("<I", header, 0x1C)[0]
        if not (PTR_MIN <= array < PTR_MAX and 0 <= count <= MAX_COUNT):
            return None
        return array, count

    def _rows_confident(self, rows: list[dict] | None) -> bool:
        if not rows:
            return False
        known = sum(row["tid"] in KNOWN_TIDS for row in rows)
        if known >= 3:
            return True
        fuzhou = sum(row["tid"] in FUZHOU_TIDS for row in rows)
        if len(rows) <= 4 and fuzhou >= 1 and all(row["tid"] in FUZHOU_TIDS for row in rows):
            return True
        if len(rows) > 4:
            return False
        tids = frozenset(row["tid"] for row in rows)
        return any(tids and tids <= group for group in SCENE_TID_GROUPS)

    def _rows_match_scene(self, rows: list[dict] | None) -> bool:
        if self.scene_id is None:
            return False
        if not rows:
            return True
        return all(row.get("scene_id") == self.scene_id for row in rows)

    def _quick_validate_slots(self, slots_raw: bytes, count: int) -> bool:
        tids = []
        for index in range(count):
            slot = struct.unpack_from("<I", slots_raw, index * 4)[0]
            if not (PTR_MIN <= slot < PTR_MAX):
                continue
            item = self.u32(slot + 0x08)
            if not item or self.u32(item + 0x01) != 0:
                continue
            target = self.u32(item + 0x05)
            if not target:
                continue
            base = self.u32(target + (0x09 if self.u8(target) == 0 else 0x05))
            tid = self.u32(base + 0x11) if base else None
            if tid in KNOWN_TIDS:
                tids.append(tid)
        if len(tids) >= 3:
            return True
        if count <= 4 and any(tid in FUZHOU_TIDS for tid in tids):
            return True
        if count > 4 or not tids:
            return False
        tids_set = frozenset(tids)
        return any(tids_set <= group for group in SCENE_TID_GROUPS)

    def _rows_from_slots(self, slots_raw: bytes, count: int) -> list[dict] | None:
        rows = []
        for index in range(count):
            slot = struct.unpack_from("<I", slots_raw, index * 4)[0]
            if not slot:
                continue
            row = self._decode_entry(slot, index)
            if row is not None:
                rows.append(row)
        return rows or None

    def read_container(self, container: int) -> list[dict] | None:
        header = self._container_header(container)
        if not header:
            return None
        array, count = header
        if count == 0:
            return []
        slots_raw = self.read_bytes(array, count * 4)
        if not slots_raw:
            return None
        return self._rows_from_slots(slots_raw, count)

    def read_container_consistent(self, container: int, attempts: int = 4) -> list[dict] | None:
        for _ in range(attempts):
            rows = self.read_container(container)
            if rows == [] and self.scene_id is not None:
                return rows
            if rows and self._rows_match_scene(rows) and self._rows_confident(rows):
                return rows
            time.sleep(0.01)
        return None

    def locate(self) -> tuple[int, list[dict]] | None:
        self.scan_timed_out = False
        if self.cached_container:
            rows = self.read_container_consistent(self.cached_container)
            if rows == [] and self.scene_id is not None:
                feature_result = read_scene_feature_rows(self, self.scene_id)
                if feature_result is not None and feature_result[1]:
                    return feature_result
                if feature_result is not None:
                    return self.cached_container, rows
            if rows and self._rows_match_scene(rows) and self._rows_confident(rows):
                header = self._container_header(self.cached_container)
                if self.scene_id is not None and header and header[1] > len(rows):
                    feature_result = read_scene_feature_rows(self, self.scene_id)
                    if feature_result is not None:
                        return self.cached_container, merge_live_rows(rows, feature_result[1])
                return self.cached_container, rows
            self.cached_container = None

        if self.scene_id is not None:
            feature_result = read_scene_feature_rows(self, self.scene_id)
            if feature_result is not None:
                return feature_result

        started = time.perf_counter()
        regions = list(self._merged_regions())
        regions.sort(key=lambda item: (0 if 0x40000000 <= item[0] < 0x50000000 else 1, abs(item[0] - 0x4A000000)))
        # Read each committed region once. The old implementation reread the
        # complete address space once per candidate count, which made a
        # count=1 scene take roughly 96 seconds on this client.
        try:
            import numpy as np
        except ImportError:
            np = None
        for region_base, region_size in regions:
            if self.scan_timeout_s is not None and time.perf_counter() - started >= self.scan_timeout_s:
                self.scan_timed_out = True
                return None
            offset = 0
            while offset < region_size:
                if self.scan_timeout_s is not None and time.perf_counter() - started >= self.scan_timeout_s:
                    self.scan_timed_out = True
                    return None
                size = min(16 * 1024 * 1024, region_size - offset)
                read_start = region_base + offset - (0x20 if offset else 0)
                read_size = size + (0x20 if offset else 0)
                data = self.read_bytes(read_start, read_size)
                if data:
                    if np is None:
                        candidate_offsets = range(0, max(0, len(data) - 0x20), 4)
                    else:
                        words = np.frombuffer(data, dtype=np.dtype("<u4"))
                        limit = max(0, len(words) - 7)
                        if limit:
                            arrays = words[4:limit + 4]
                            counts = words[7:limit + 7]
                            mask = (
                                (arrays >= PTR_MIN)
                                & (arrays < PTR_MAX)
                                & ((arrays & 3) == 0)
                                & (counts <= MAX_COUNT)
                            )
                            candidate_offsets = (int(index) * 4 for index in np.flatnonzero(mask))
                        else:
                            candidate_offsets = ()
                    for local_offset in candidate_offsets:
                        candidate = read_start + local_offset
                        if candidate < PTR_MIN or candidate % 4:
                            continue
                        array = struct.unpack_from("<I", data, local_offset + 0x10)[0]
                        count = struct.unpack_from("<I", data, local_offset + 0x1C)[0]
                        if not (PTR_MIN <= array < PTR_MAX and array % 4 == 0 and 1 <= count <= MAX_COUNT):
                            continue
                        slots_raw = self.read_bytes(array, count * 4)
                        if not slots_raw or not self._quick_validate_slots(slots_raw, count):
                            continue
                        rows = self._rows_from_slots(slots_raw, count)
                        if rows and self._rows_match_scene(rows) and self._rows_confident(rows):
                            self.cached_container = candidate
                            self._save_cached_container(candidate)
                            return candidate, rows
                offset += size
        return None

    def read(self) -> tuple[int, list[dict]] | None:
        return self.locate()


def merge_live_rows(primary: list[dict], supplement: list[dict]) -> list[dict]:
    merged = {}
    for row in [*primary, *supplement]:
        key = (int(row["tid"]), round(float(row["world_x"]), 3), round(float(row["world_y"]), 3), round(float(row["world_z"]), 3))
        merged.setdefault(key, row)
    rows = sorted(merged.values(), key=lambda row: (row["tid"], row["world_x"], row["world_z"]))
    for index, row in enumerate(rows):
        row["idx"] = index
    return rows


def read_scene_feature_rows(reader: ResidentReader, scene_id: int | None, *, _use_hints: bool = True) -> tuple[int | str, list[dict]] | None:
    """Read generic live scene-feature records for the current scene."""
    if scene_id is None:
        return None
    tids = {tid for tid, sid in SCENE_BY_TID.items() if sid == int(scene_id)}
    if not tids:
        return None
    try:
        found = {}
        needles = {struct.pack("<I", tid): tid for tid in tids}
        needle_pattern = re.compile(b"|".join(re.escape(needle) for needle in needles))
        resident_needle = struct.pack("<I", RESIDENT_CONTAINER_VTABLE)
        all_regions = list(reader._merged_regions())
        available = set(all_regions)
        hinted_regions = reader._load_feature_regions(int(scene_id), available) if _use_hints else []
        regions = hinted_regions or all_regions
        hints_failed = False
        hit_regions = set()
        checked_resident = set()
        resident_result = None
        for base, size in regions:
            offset = 0
            while offset < size:
                chunk = min(16 * 1024 * 1024, size - offset)
                start = base + offset - (0x40 if offset else 0)
                data = reader.read_bytes(start, chunk + (0x40 if offset else 0))
                if data is None:
                    if hinted_regions:
                        hints_failed = True
                    offset += chunk
                    continue
                if data:
                    for resident_match in re.finditer(re.escape(resident_needle), data):
                        resident_offset = resident_match.start()
                        candidate = start + resident_offset
                        if candidate in checked_resident or resident_offset + 0x20 > len(data):
                            continue
                        checked_resident.add(candidate)
                        array = struct.unpack_from("<I", data, resident_offset + 0x10)[0]
                        count = struct.unpack_from("<I", data, resident_offset + 0x1C)[0]
                        if not (PTR_MIN <= array < PTR_MAX and array % 4 == 0 and 1 <= count <= MAX_COUNT):
                            continue
                        resident_rows = reader.read_container(candidate)
                        if resident_rows and reader._rows_match_scene(resident_rows) and reader._rows_confident(resident_rows):
                            if reader._rows_match_scene(resident_rows):
                                if resident_result is None or len(resident_rows or []) > len(resident_result[1]):
                                    resident_result = (candidate, resident_rows or [])
                    for match in needle_pattern.finditer(data):
                        hit = match.start()
                        tid = needles[match.group()]
                        if hit + 0x34 > len(data):
                            continue
                        try:
                            x, y, z = struct.unpack_from("<fff", data, hit + 4)
                            sig16 = struct.unpack_from("<I", data, hit + 0x10)[0]
                            sig24 = struct.unpack_from("<I", data, hit + 0x24)[0]
                            sig30 = struct.unpack_from("<I", data, hit + 0x30)[0]
                        except struct.error:
                            continue
                        if (
                            sig16 == 16
                            and sig24 == 25700
                            and sig30 == 1
                            and all(math.isfinite(v) for v in (x, y, z))
                            and -1000.0 < x < 1000.0
                            and -1000.0 < y < 1000.0
                            and -1000.0 < z < 1000.0
                        ):
                            key = (tid, round(x, 3), round(y, 3), round(z, 3))
                            hit_regions.add((base, size))
                            found[key] = {
                                "idx": 0,
                                "tid": tid,
                                "name": NAME_BY_TID.get(tid, "未知怪物"),
                                "scene_id": int(scene_id),
                                "x": x,
                                "y": y,
                                "z": z,
                                "world_x": x,
                                "world_y": y,
                                "world_z": z,
                                "source": "scene_feature",
                            }
                offset += chunk
        if hinted_regions and hints_failed:
            return read_scene_feature_rows(reader, scene_id, _use_hints=False) if set(regions) != set(all_regions) else None
        if hit_regions and not hinted_regions:
            reader._save_feature_regions(int(scene_id), hit_regions)
        rows = list(found.values())
        rows.sort(key=lambda row: (row["tid"], row["world_x"], row["world_z"]))
        for index, row in enumerate(rows):
            row["idx"] = index
        if resident_result is not None:
            container, resident_rows = resident_result
            reader.cached_container = container
            reader._save_cached_container(container)
            return container, merge_live_rows(resident_rows, rows)
        return "dynamic:scene_feature", rows
    except Exception:
        return None

def read_plg_live_npcs(pid: int, scene_id: int | None) -> tuple[str, list[dict]] | None:
    """Generic live NPC fallback through the project's plg object manager."""
    if scene_id is None:
        return None
    try:
        from app.core.game_attach import GameAttachSession
        from app.core.plg_objects import CLASS_NPC, list_class_objects
        from app.core.remote_runtime import wait_pid_scene_stable

        wait_pid_scene_stable(pid, timeout_s=4)
        session = GameAttachSession(log=lambda _message: None)
        session.attach(pid)
        try:
            objects = list_class_objects(
                session,
                CLASS_NPC,
                host_pos=None,
                radius=None,
                limit=512,
                max_inspect=512,
                read_name=False,
                read_tid=True,
                log=lambda _message: None,
            )
            rows = []
            for obj in objects:
                if obj.tid not in KNOWN_MONSTER_TIDS or obj.x is None or obj.y is None or obj.z is None:
                    continue
                rows.append({
                    "idx": len(rows),
                    "tid": int(obj.tid),
                    "name": NAME_BY_TID.get(int(obj.tid), "未知怪物"),
                    "scene_id": int(scene_id),
                    "x": obj.x,
                    "y": obj.y,
                    "z": obj.z,
                    "world_x": obj.x,
                    "world_y": obj.y,
                    "world_z": obj.z,
                    "source": "plg_live_object",
                })
            return "dynamic:plg", rows
        finally:
            session.close()
    except Exception:
        return None

def read_current_position(pid: int) -> tuple[float, float, float] | None:
    try:
        from app.core.automove import read_scene_position
        from app.core.game_attach import GameAttachSession
        from app.core.remote_runtime import wait_pid_scene_stable

        wait_pid_scene_stable(pid, timeout_s=4)
        session = GameAttachSession(log=lambda _message: None)
        session.attach(pid)
        try:
            state = read_scene_position(session, log=lambda _message: None, timeout_ms=5000)
            if state.ok and state.scene_pos and len(state.scene_pos) >= 3:
                return tuple(float(value) for value in state.scene_pos[:3])
        finally:
            session.close()
    except Exception:
        return None
    return None


def read_fuzhou_markers(pid: int) -> tuple[str, list[dict]] | None:
    """Deprecated diagnostic path; resident task flows do not use it."""
    return None


def sort_rows_by_distance(rows: list[dict], origin: tuple[float, float, float] | None) -> list[dict]:
    if origin is None:
        return rows
    for row in rows:
        row["distance"] = _distance(origin, (row["world_x"], row["world_y"], row["world_z"]))
    rows.sort(key=lambda row: (row["distance"], row["tid"], row["world_x"], row["world_z"]))
    for index, row in enumerate(rows):
        row["idx"] = index
    return rows


def print_rows(container: int, rows: list[dict], as_json: bool, elapsed_sec: float | None = None, origin: tuple[float, float, float] | None = None) -> None:
    container_label = f"0x{container:08X}" if isinstance(container, int) else str(container)
    payload = {"container": container_label, "count": len(rows), "rows": rows}
    if origin is not None:
        payload["origin"] = {"world_x": origin[0], "world_y": origin[1], "world_z": origin[2]}
    if elapsed_sec is not None:
        payload["elapsed_sec"] = round(elapsed_sec, 4)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
        return
    timing = f" elapsed={elapsed_sec:.4f}s" if elapsed_sec is not None else ""
    origin_text = f" origin=({origin[0]:+.3f},{origin[1]:+.3f},{origin[2]:+.3f})" if origin is not None else ""
    print(f"container={container_label} rows={len(rows)}{timing}{origin_text}")
    for row in rows:
        print(
            f"{row['idx']:2d} {row['name']} tid={row['tid']:6d} "
            f"scene={row['scene_id'] or '?'} "
            f"distance={row.get('distance', float('nan')):8.3f} "
            f"world=({row['world_x']:+10.3f},{row['world_y']:+10.3f},{row['world_z']:+10.3f})"
        )


def _distance(a, b) -> float:
    return sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)) ** 0.5

def _path_and_verify(pid: int, row: dict, wait_seconds: float, arrival_radius: float, aoi_radius: float) -> int:
    from app.core.automove import PathTarget, host_move_to, read_scene_position
    from app.core.entity_scan import scan_nearby_entities
    from app.core.game_attach import GameAttachSession
    from app.core.remote_runtime import ensure_pid_remote_callable, wait_pid_scene_stable

    session = GameAttachSession(log=lambda message: print("[attach]", message))
    session.attach(pid)
    try:
        ensure_pid_remote_callable(pid)
        wait_pid_scene_stable(pid)
        before = read_scene_position(session, log=lambda message: None)
        scene_id = int(before.scene_id) if before.ok and before.scene_id is not None else int(row.get("scene_id") or 0)
        target = (row["world_x"], row["world_y"], row["world_z"])
        result = host_move_to(session, PathTarget(*target, mode=scene_id, map_hint=row["name"]), log=lambda message: print("[automove]", message))
        print(f"path target={row['name']} tid={row['tid']} scene={scene_id} xyz={target} ok={result.ok} ret={result.ret} error={result.error or ''}")
        if not result.ok:
            return 3
        deadline = time.monotonic() + max(0.0, wait_seconds)
        last_position = None
        while time.monotonic() <= deadline:
            state = read_scene_position(session, log=lambda message: None)
            if state.ok and state.scene_pos:
                last_position = tuple(state.scene_pos)
                if _distance(last_position, target) <= arrival_radius:
                    print(f"arrived=yes distance={_distance(last_position, target):.2f} position={last_position}")
                    break
            time.sleep(0.5)
        else:
            print(f"arrived=no position={last_position} distance={_distance(last_position, target) if last_position else None}")
        host_pos = last_position
        if host_pos is not None:
            hits = scan_nearby_entities(session, host_pos=host_pos, radius=aoi_radius, limit=120, kinds=("monster", "npc"), log=lambda message: None)
            matches = [hit for hit in hits if hit.tid == row["tid"] or hit.name == row["name"]]
            print(f"aoi_matches={len(matches)} aoi_total={len(hits)} radius={aoi_radius}")
            for hit in matches[:5]:
                print("aoi", json.dumps(hit.to_dict(), ensure_ascii=False))
        return 0
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pid", type=int)
    parser.add_argument("--watch", type=float, metavar="SECONDS", default=0.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-cache", action="store_true", help="跳过已有容器缓存，直接做实时定位")
    parser.add_argument("--scan-timeout", type=float, default=12.0, help="旧版全盘扫描最长秒数；设为 0 表示不限制")
    parser.add_argument("--map", action="store_true", help="scene 72: 从 Win_WorldMap 刀标读取（必要时自动按 M）")
    parser.add_argument("--tid", type=int, default=None, help="只输出指定 TID")
    parser.add_argument("--path", type=int, metavar="IDX", help="按输出 idx 寻路到该怪")
    parser.add_argument("--verify", action="store_true", help="配合 --path：等待到达并用 AOI 复核")
    parser.add_argument("--wait", type=float, default=30.0, help="寻路后最多等待秒数")
    parser.add_argument("--arrival-radius", type=float, default=8.0)
    parser.add_argument("--aoi-radius", type=float, default=30.0)
    args = parser.parse_args()

    if args.map:
        started = time.perf_counter()
        result = read_fuzhou_markers(args.pid)
        if result is None:
            print("未找到大地图刀标列表", file=sys.stderr)
            return 2
        container, rows = result
        if args.tid is not None:
            rows = [row for row in rows if row["tid"] == args.tid]
            for index, row in enumerate(rows):
                row["idx"] = index
        origin = read_current_position(args.pid)
        rows = sort_rows_by_distance(rows, origin)
        if args.path is not None:
            selected = next((row for row in rows if row["idx"] == args.path), None)
            if selected is None:
                print(f"idx={args.path} 不存在，当前有效 idx: {[row["idx"] for row in rows]}", file=sys.stderr)
                return 3
            return _path_and_verify(args.pid, selected, args.wait, args.arrival_radius, args.aoi_radius)
        print_rows(container, rows, as_json=args.json, elapsed_sec=time.perf_counter() - started, origin=origin)
        return 0

    scene_id = None
    try:
        from app.core.live_scene_hub import get_live_scene
        snap = get_live_scene(args.pid, max_age_s=2.5)
        if snap is not None and snap.scene_id is not None:
            scene_id = int(snap.scene_id)
            print(f"scene_source=live_scene_hub scene_id={scene_id}", file=sys.stderr)
    except Exception:
        pass
    if scene_id is None:
        scene_id = probe_scene_id(args.pid)
        if scene_id is not None:
            print(f"scene_source=direct scene_id={scene_id}", file=sys.stderr)
    reader = ResidentReader(args.pid, use_cache=not args.no_cache, scene_id=scene_id, scan_timeout_s=(None if args.scan_timeout <= 0 else args.scan_timeout))
    try:
        while True:
            started = time.perf_counter()
            result = reader.read()
            if result is None and reader.scan_timed_out:
                fallback = read_scene_feature_rows(reader, scene_id)
                if fallback is None:
                    fallback = read_plg_live_npcs(args.pid, scene_id)
                if fallback is not None:
                    container, rows = fallback
                    origin = read_current_position(args.pid)
                    rows = sort_rows_by_distance(rows, origin)
                    print_rows(container, rows, as_json=args.json, elapsed_sec=time.perf_counter() - started, origin=origin)
                    if args.watch <= 0:
                        return 0
                    time.sleep(args.watch)
                    continue
            if result is None:
                reason = "scan timeout" if reader.scan_timed_out else "not found"
                print(f"resident container {reason}", file=sys.stderr)
                return 2
            container, rows = result
            origin = read_current_position(args.pid)
            rows = sort_rows_by_distance(rows, origin)
            if args.path is not None:
                selected = next((row for row in rows if row["idx"] == args.path), None)
                if selected is None:
                    print(f"idx={args.path} 不存在，当前有效 idx: {[row['idx'] for row in rows]}", file=sys.stderr)
                    return 3
                return _path_and_verify(args.pid, selected, args.wait, args.arrival_radius, args.aoi_radius)
            print_rows(container, rows, as_json=args.json, elapsed_sec=time.perf_counter() - started, origin=origin)
            if args.watch <= 0:
                return 0
            time.sleep(args.watch)
    finally:
        reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
