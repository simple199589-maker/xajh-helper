# -*- coding: utf-8 -*-
"""Read-only one-minute training-dummy damage measurement from battle UI."""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from collections import Counter
from typing import Callable, Iterable

from app.core.entity_scan import EntityHit, scan_nearby_entities
from app.core.plg_interact import get_object_id64

LogFn = Callable[[str], None]
UpdateFn = Callable[[dict], None]

WINDOW_SECONDS = 60.0
_DUMMY_MARKER = "对木人桩造成了"
_DUMMY_LINE_RE = re.compile(
    r"\^ef4545\ue000(?:<[^>\r\n]{0,48}>)*\^c0a020"
    r"你的[^\r\n\x00]{0,50}?对木人桩造成了(\d+)点伤害"
)
_STAT_RE = re.compile(
    r"DUMMY_DAMAGE\s+t=([0-9A-Fa-f]{8}):([0-9A-Fa-f]{8})\s+"
    r"a=(\d+)\s+s=(\d+)\s+d=(\d+)\s+ms=(\d+)\s+n=(\d+)\s+"
    r"total=(-?\d+)\s+last=(-?\d+)"
)
_READER_HEADER_RE = re.compile(r"^RAW\s+(\d+)\s+RUNS\s+(\d+)\s*$")
_READER_SEQUENCE_RE = re.compile(r"^SEQ\s+([0-9A-Fa-f]{8})\s+(\d+)(?:\s+(.*))?$")


def format_damage_amount(value: float | int) -> str:
    """Format damage in millions, switching to billions at 1,000M."""
    amount = float(value or 0)
    if abs(amount) >= 1_000_000_000:
        return f"{amount / 1_000_000_000:.2f}B"
    return f"{amount / 1_000_000:.2f}M"


def default_dummy_damage_reader_path() -> Path:
    """Locate the read-only native reader in source and frozen layouts."""
    try:
        from common.paths import NATIVE_BIN_DIR, app_root, bundle_root

        roots = (
            app_root() / "native" / "bin",
            NATIVE_BIN_DIR,
            bundle_root() / "native" / "bin",
            app_root() / "_internal" / "native" / "bin",
        )
    except Exception:
        roots = ()
    candidates = list(roots) + [
        Path(__file__).resolve().parents[2] / "native" / "bin"
    ]
    for directory in candidates:
        candidate = Path(directory) / "dummy_damage_reader.exe"
        if candidate.is_file():
            return candidate
    return candidates[0] / "dummy_damage_reader.exe"


def parse_dummy_damage_reader_output(output: str) -> list[list[int]]:
    """Parse one successful native reader snapshot."""
    lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
    if not lines or _READER_HEADER_RE.fullmatch(lines[0]) is None:
        raise ValueError("invalid dummy damage reader header")
    sequences: list[list[int]] = []
    for line in lines[1:]:
        match = _READER_SEQUENCE_RE.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid dummy damage reader line: {line[:80]}")
        expected = int(match.group(2))
        fields = (match.group(3) or "").split()
        if len(fields) != expected:
            raise ValueError(
                f"dummy damage reader count mismatch: expected={expected} got={len(fields)}"
            )
        values = [int(field) for field in fields]
        if any(value <= 0 or value > 0xFFFFFFFF for value in values):
            raise ValueError("dummy damage reader returned an invalid damage value")
        if values:
            sequences.append(values)
    return sorted(sequences, key=len, reverse=True)


def _scan_with_native_reader(session, reader: Path) -> list[list[int]]:
    pid = int(getattr(session, "pid", 0) or 0)
    if pid <= 0:
        raise RuntimeError("木人桩只读统计缺少有效游戏 PID")
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    completed = subprocess.run(
        [str(reader), str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15.0,
        creationflags=creationflags,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            f"木人桩只读读取器失败(code={completed.returncode}): {detail[:200]}"
        )
    return parse_dummy_damage_reader_output(completed.stdout)


def parse_dummy_damage_stat(note: str) -> dict | None:
    """Parse the compact native counter snapshot returned in the bridge note."""
    match = _STAT_RE.search(str(note or ""))
    if not match:
        return None
    hi, lo, armed, started, done, elapsed_ms, hits, total, last = match.groups()
    return {
        "target_id": (int(hi, 16) << 32) | int(lo, 16),
        "armed": bool(int(armed)),
        "started": bool(int(started)),
        "done": bool(int(done)),
        "elapsed_ms": int(elapsed_ms),
        "hits": int(hits),
        "total_damage": int(total),
        "last_damage": int(last),
    }


def select_training_dummy(entities: Iterable[EntityHit]) -> EntityHit | None:
    """Return the nearest exact-name training dummy from an entity snapshot."""
    matches = [e for e in entities if str(e.name or "").strip() == "木人桩"]
    if not matches:
        return None
    return min(matches, key=lambda e: e.dist if e.dist is not None else float("inf"))


def find_training_dummy(
    session, *, radius: float = 80.0, log: LogFn | None = None
) -> dict | None:
    """Find one nearby 木人桩 and resolve its exact 64-bit runtime object id."""
    log = log or (lambda _m: None)
    host_pos = None
    try:
        from app.core.automove import read_scene_position

        position = read_scene_position(session, log=lambda _m: None)
        scene_pos = getattr(position, "scene_pos", None)
        if scene_pos is not None and len(scene_pos) >= 3:
            host_pos = tuple(float(v) for v in scene_pos[:3])
    except Exception:
        host_pos = None

    entities = scan_nearby_entities(
        session,
        host_pos=host_pos,
        radius=float(radius),
        limit=160,
        kinds=("npc", "monster"),
        require_pos=False,
        log=log,
    )
    target = select_training_dummy(entities)
    if target is None:
        return None
    object_id = get_object_id64(session, int(target.address))
    if object_id is None:
        return None
    return {
        "name": target.name,
        "address": int(target.address),
        "tid": int(target.tid) if target.tid is not None else None,
        "object_id": int(object_id) & 0xFFFFFFFFFFFFFFFF,
        "dist": float(target.dist) if target.dist is not None else None,
    }


def scan_dummy_damage_sequences(session) -> list[list[int]]:
    """Read candidate battle-UI histories without writing or injecting code."""
    reader = default_dummy_damage_reader_path()
    if reader.is_file():
        return _scan_with_native_reader(session, reader)

    # Development-only fallback for a source checkout without the native tool.
    # A present-but-failing reader is deliberately not hidden by this slower scan.
    pm = getattr(session, "pm", None)
    if pm is None:
        return []
    try:
        import pymem.memory

        addresses = sorted(
            set(
                pm.pattern_scan_all(
                    re.escape(_DUMMY_MARKER.encode("utf-16le")),
                    return_multiple=True,
                )
                or []
            )
        )
    except Exception:
        return []

    runs: list[list[int]] = []
    current: list[int] = []
    for address in addresses:
        if current and int(address) - current[-1] > 0x300:
            runs.append(current)
            current = []
        current.append(int(address))
    if current:
        runs.append(current)

    sequences: dict[tuple[int, ...], list[int]] = {}
    # Complete battle histories consistently form the longest runs. Limiting
    # this tail read prevents old render copies from making each poll slower as
    # a session grows.
    for run in sorted(runs, key=len, reverse=True)[:12]:
        start = max(0, run[0] - 400)
        size = min(0x30000, run[-1] - start + 1000)
        try:
            raw = pymem.memory.read_bytes(pm.process_handle, start, size)
        except Exception:
            continue
        values = [
            int(value)
            for value in _DUMMY_LINE_RE.findall(
                raw.decode("utf-16le", errors="ignore")
            )
        ]
        if values:
            sequences.setdefault(tuple(values), values)
    return sorted(sequences.values(), key=len, reverse=True)


def extend_damage_history(
    history: list[int],
    candidates: Iterable[Iterable[int]],
    *,
    count_if_empty: bool = True,
) -> tuple[list[int], list[int]]:
    """Merge a moved/grown battle buffer using its longest reliable overlap."""
    choices = [list(map(int, values)) for values in candidates if values]
    if not choices:
        return list(history), []
    if not history:
        selected = max(choices, key=len)
        return selected, list(selected) if count_if_empty else []

    best: tuple[int, int, int, list[int]] | None = None
    for candidate in choices:
        limit = min(len(history), len(candidate))
        overlap = 0
        for size in range(limit, 0, -1):
            if history[-size:] == candidate[:size]:
                overlap = size
                break
        minimum = min(8, len(history), len(candidate))
        if overlap < minimum:
            continue
        added = len(candidate) - overlap
        score = (overlap, added, len(candidate), candidate)
        if best is None or score[:3] > best[:3]:
            best = score
    if best is None or best[1] <= 0:
        # Some UI buffers are rebuilt at a new address and lose a reliable
        # prefix/suffix overlap. If their longest snapshot still grew, only the
        # length delta at the tail can be new history.
        selected = max(choices, key=len)
        growth = len(selected) - len(history)
        if growth > 0:
            extension = selected[-growth:]
            return list(history) + extension, extension
        return list(history), []
    candidate = best[3]
    extension = candidate[best[0] :]
    return list(history) + extension, extension


def find_damage_sequence_delta(
    references: Iterable[Iterable[int]],
    candidates: Iterable[Iterable[int]],
) -> list[int]:
    """Find one new tail against any previously observed UI history copy."""
    known = [tuple(map(int, values)) for values in references if values]
    current_all = [tuple(map(int, values)) for values in candidates if values]
    if not current_all:
        return []
    current_frequency = Counter(current_all)
    current = list(current_frequency)
    if not known:
        selected = max(current, key=lambda item: (current_frequency[item], len(item)))
        return list(selected)
    known_set = set(known)
    matches: list[tuple[tuple[int, ...], int, int]] = []
    for candidate in current:
        if candidate in known_set:
            continue
        prefix = [0] * len(candidate)
        matched = 0
        for index in range(1, len(candidate)):
            while matched and candidate[index] != candidate[matched]:
                matched = prefix[matched - 1]
            if candidate[index] == candidate[matched]:
                matched += 1
            prefix[index] = matched
        best_overlap = 0
        for previous in known:
            matched = 0
            for value in previous[-len(candidate) :]:
                while matched and (
                    matched == len(candidate) or value != candidate[matched]
                ):
                    matched = prefix[matched - 1]
                if value == candidate[matched]:
                    matched += 1
            best_overlap = max(best_overlap, matched)
        minimum = min(8, len(candidate), max((len(v) for v in known), default=0))
        small_voted_growth = (
            current_frequency[candidate] >= 2
            and best_overlap > 0
            and 0 < len(candidate) - best_overlap <= 8
        )
        if (best_overlap < minimum and not small_voted_growth) or best_overlap >= len(candidate):
            continue
        matches.append((candidate[best_overlap:], best_overlap, len(candidate)))
    if not matches:
        return []

    frequency: dict[tuple[int, ...], int] = {}
    for extension, _overlap, _size in matches:
        frequency[extension] = frequency.get(extension, 0) + 1
    extension, _overlap, _size = max(
        matches,
        key=lambda item: (
            frequency[item[0]],
            item[1],
            len(item[0]),
            item[2],
        ),
    )
    return list(extension)


def update_damage_references(
    references: Iterable[Iterable[int]],
    candidates: Iterable[Iterable[int]],
    *,
    limit: int = 2048,
) -> list[list[int]]:
    """Keep a bounded set of prior copies so returning residuals are ignored."""
    ordered: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()
    for values in list(references) + list(candidates):
        item = tuple(map(int, values))
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(list(item))
    cap = max(16, int(limit))
    return ordered[-cap:]


def measure_dummy_damage(
    session,
    *,
    poll_s: float = 0.15,
    stop_event=None,
    on_update: UpdateFn | None = None,
    log: LogFn | None = None,
) -> dict:
    """Measure one minute from battle-UI history, without bridge injection."""
    log = log or (lambda _m: None)
    on_update = on_update or (lambda _d: None)
    interval = max(0.1, float(poll_s))
    result: dict = {
        "ok": False,
        "target": None,
        "started": False,
        "completed": False,
        "stopped": False,
        "elapsed_s": 0.0,
        "hits": 0,
        "total_damage": 0,
        "last_damage": 0,
        "dps": 0.0,
        "error": None,
    }
    try:
        target = {
            "name": "木人桩",
            "address": 0,
            "tid": None,
            "object_id": 0,
            "dist": None,
            "source": "battle_ui",
        }
        result["target"] = target
        baseline = scan_dummy_damage_sequences(session)
        references = update_damage_references([], baseline)
        on_update({"phase": "waiting", "target": target})
        started_at: float | None = None
        total_damage = 0
        hits = 0
        last_damage = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                result["stopped"] = True
                break
            scan_started = time.monotonic()
            candidates = scan_dummy_damage_sequences(session)
            added = find_damage_sequence_delta(references, candidates)
            references = update_damage_references(references, candidates)
            if added:
                if started_at is None:
                    started_at = scan_started
                hits += len(added)
                total_damage += sum(added)
                last_damage = added[-1]
            now = time.monotonic()
            elapsed_s = min(
                WINDOW_SECONDS,
                max(0.0, now - started_at) if started_at is not None else 0.0,
            )
            phase = "running" if started_at is not None else "waiting"
            on_update(
                {
                    "phase": phase,
                    "target": target,
                    "remaining_s": max(0.0, WINDOW_SECONDS - elapsed_s),
                    "started": started_at is not None,
                    "done": elapsed_s >= WINDOW_SECONDS,
                    "elapsed_ms": int(elapsed_s * 1000.0),
                    "hits": hits,
                    "total_damage": total_damage,
                    "last_damage": last_damage,
                }
            )
            if elapsed_s >= WINDOW_SECONDS:
                result["completed"] = True
                break
            if stop_event is not None:
                stop_event.wait(interval)
            else:
                time.sleep(interval)

        elapsed_s = min(
            WINDOW_SECONDS,
            max(0.0, time.monotonic() - started_at)
            if started_at is not None
            else 0.0,
        )
        divisor = elapsed_s if elapsed_s > 0 else WINDOW_SECONDS
        result.update(
            {
                "ok": True,
                "started": started_at is not None,
                "elapsed_s": elapsed_s,
                "hits": hits,
                "total_damage": total_damage,
                "last_damage": last_damage,
                "dps": float(total_damage) / divisor,
            }
        )
        log(
            "dummy damage stat: "
            f"source=battle_ui elapsed={elapsed_s:.1f}s "
            f"hits={result['hits']} total={result['total_damage']} dps={result['dps']:.1f}"
        )
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result
