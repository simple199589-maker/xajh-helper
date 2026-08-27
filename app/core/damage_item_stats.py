# -*- coding: utf-8 -*-
"""One-minute successful damage-item release measurement for the dev panel."""
from __future__ import annotations

import re
import time
from typing import Callable, Iterable

from app.core.package_api import DEFAULT_EXCHANGE_PACKAGE_INDEXES, PackageItem, list_package_items

LogFn = Callable[[str], None]
UpdateFn = Callable[[dict], None]


def _matches_keyword(item: PackageItem, keyword: str) -> bool:
    """Apply an optional narrow filter without making the default too broad."""
    key = (keyword or "").strip().lower()
    if not key or key in ("伤害物品", "伤害药", "攻击药", "药丸"):
        return True
    tid = int(getattr(item, "tid", 0) or 0) & 0xFFFFFFFF
    name = str(getattr(item, "name", "") or "").lower()
    return key in name or key in str(tid) or key in f"0x{tid:x}"


def find_damage_items(
    items: Iterable[PackageItem], *, keyword: str = "伤害物品"
) -> list[PackageItem]:
    """Select consumable damage items and explicitly reject attack gems."""
    from app.core.activity_auto import is_autoplay_attack_recover_item

    found: dict[int, PackageItem] = {}
    for item in items:
        tid = int(getattr(item, "tid", 0) or 0) & 0xFFFFFFFF
        if not tid or not _matches_keyword(item, keyword):
            continue
        if not is_autoplay_attack_recover_item(
            name=str(getattr(item, "name", "") or ""), tid=tid
        ):
            continue
        found.setdefault(tid, item)
    return list(found.values())


def _read_bag_snapshot(session, package_indexes: Iterable[int]) -> list[PackageItem]:
    items: list[PackageItem] = []
    for package in package_indexes:
        items.extend(list_package_items(session, int(package), log=lambda _m: None))
    return items


_TRACE_RE = re.compile(r"attempts=(\d+)\s+success=(\d+)")


def parse_item_use_trace(note: str) -> tuple[int, int] | None:
    """Return (attempts, successful native UseItem returns) from bridge note."""
    match = _TRACE_RE.search(str(note or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def measure_damage_item_frequency(
    session,
    *,
    keyword: str = "伤害物品",
    duration_s: float = 60.0,
    poll_s: float = 0.5,
    package_indexes: Iterable[int] = DEFAULT_EXCHANGE_PACKAGE_INDEXES,
    stop_event=None,
    on_update: UpdateFn | None = None,
    log: LogFn | None = None,
) -> dict:
    """Measure real successful native UseItem returns for one selected item.

    The hook sits under the game's CheckHPItem / manual / key paths, so a
    reusable item such as 2200W is counted without relying on bag consumption.
    """
    log = log or (lambda _m: None)
    on_update = on_update or (lambda _d: None)
    indexes = tuple(int(i) for i in package_indexes)
    duration = max(0.0, float(duration_s))
    interval = max(0.1, float(poll_s))
    started = time.monotonic()
    result: dict = {
        "ok": False,
        "keyword": (keyword or "").strip() or "伤害物品",
        "duration_s": duration,
        "elapsed_s": 0.0,
        "successes": 0,
        "frequency_per_min": 0.0,
        "target_items": [],
        "target_tids": [],
        "target": None,
        "samples": 0,
        "stopped": False,
        "error": None,
    }
    bridge = None
    trace_armed = False
    try:
        baseline = _read_bag_snapshot(session, indexes)
        targets = find_damage_items(baseline, keyword=result["keyword"])
        if not targets:
            result["error"] = f"未找到伤害物品 key={result['keyword']!r}"
            return result

        # Native UseItem receives package+slot. One exact slot keeps results
        # attributable when several damage-item types coexist in extended bags.
        target = targets[0]
        result["target_tids"] = [int(target.tid) & 0xFFFFFFFF]
        result["target"] = {
            "package": int(target.package),
            "slot": int(target.slot),
            "tid": int(target.tid) & 0xFFFFFFFF,
            "name": str(target.name or ""),
        }
        result["target_items"] = [
            {
                "package": int(item.package),
                "slot": int(item.slot),
                "tid": int(item.tid) & 0xFFFFFFFF,
                "name": str(item.name or ""),
                "count": int(item.count or 0),
            }
            for item in targets
        ]
        from app.core.xajh_bridge import ensure_bridge

        bridge = ensure_bridge(
            int(session.pid),
            log=lambda _m: None,
            inject_if_needed=False,
            hwnd=int(getattr(session, "hwnd", 0) or 0) or None,
        )
        if bridge is None:
            result["error"] = "桥接未就绪；请重新注入最新桥接"
            return result
        armed = bridge.item_use_trace(target.package, target.slot, mode=1)
        if not armed.ok:
            result["error"] = armed.error or armed.note or "释放统计钩子启动失败"
            return result
        trace_armed = True
        on_update(
            {
                "phase": "started",
                "remaining_s": duration,
                "successes": 0,
                "target_items": result["target_items"],
            }
        )

        deadline = started + duration
        while True:
            now = time.monotonic()
            if stop_event is not None and stop_event.is_set():
                result["stopped"] = True
                break
            if now >= deadline:
                break
            wait_s = min(interval, max(0.0, deadline - now))
            if stop_event is not None and stop_event.wait(wait_s):
                result["stopped"] = True
                break
            probe = bridge.item_use_trace(target.package, target.slot, mode=2)
            parsed = parse_item_use_trace(probe.note) if probe.ok else None
            if parsed is None:
                raise RuntimeError(probe.error or probe.note or "释放统计钩子读取失败")
            attempts, successes = parsed
            now = time.monotonic()
            on_update(
                {
                    "phase": "running",
                    "remaining_s": max(0.0, deadline - now),
                    "successes": successes,
                    "attempts": attempts,
                }
            )

        final = bridge.item_use_trace(target.package, target.slot, mode=0)
        parsed = parse_item_use_trace(final.note) if final.ok else None
        if parsed is None:
            raise RuntimeError(final.error or final.note or "释放统计钩子停止失败")
        trace_armed = False
        attempts, successes = parsed
        elapsed = max(0.001, time.monotonic() - started)
        result.update(
            {
                "ok": True,
                "elapsed_s": elapsed,
                "successes": int(successes),
                "attempts": int(attempts),
                "frequency_per_min": float(successes) * 60.0 / elapsed,
                "samples": 0,
            }
        )
        log(
            f"damage-item stat: attempts={attempts} success={result['successes']} elapsed={elapsed:.1f}s "
            f"freq={result['frequency_per_min']:.1f}/min tids={result['target_tids']}"
        )
        return result
    except Exception as e:
        result["elapsed_s"] = max(0.0, time.monotonic() - started)
        result["error"] = str(e)
        return result
    finally:
        if trace_armed and bridge is not None and result.get("target"):
            try:
                target = result["target"]
                bridge.item_use_trace(
                    int(target["package"]), int(target["slot"]), mode=0
                )
            except Exception:
                pass
