# -*- coding: utf-8 -*-
"""
切糕副本「分析怪频」——开发向低频校验。

扫 AOI 中「大漠射手」数量（正常 4），当其减少后对比普通怪个数/刷新。

@author by ak
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable

from app.core.plg_objects import CLASS_NPC, list_class_objects

LogFn = Callable[[str], None]

ARCHER_NAME = "大漠射手"
ARCHER_EXPECTED = 4
# 低频：默认约 45s（挂机中远程扫图过密易 CRT 超时/崩端）
DEFAULT_INTERVAL_S = 45.0
# 场景静物 / 非战斗干扰：不计入「普怪/可战斗」与「新出现」
IGNORE_MOB_NAMES = frozenset(
    {
        "木桶",
        "房屋",
        "炸弹",
        "箱子",
        "宝箱",
        "桶",
        "门",
        "旗",
        "旗帜",
        "火盆",
        "灯笼",
    }
)
# 名称子串命中也排除（覆盖「小木桶」等变体）
IGNORE_MOB_NAME_SUBSTR = ("木桶", "房屋", "炸弹", "箱子", "宝箱")


class MobFreqHardStop(RuntimeError):
    """Remote CRT/alloc denied while scanning AOI; caller should stop."""



@dataclass
class MobFreqSnapshot:
    t: float
    archer_n: int
    archer_keys: set[str] = field(default_factory=set)
    normal_n: int = 0  # 可战斗普怪（已排除干扰）
    normal_keys: set[str] = field(default_factory=set)
    normal_names: Counter = field(default_factory=Counter)
    raw_n: int = 0
    ignored_n: int = 0  # 静物/干扰数量
    ignored_names: Counter = field(default_factory=Counter)


@dataclass
class PhaseStats:
    """Stats while 大漠射手 count stays at a given level."""

    archer_n: int
    t0: float
    samples: int = 0
    normal_sum: int = 0
    normal_min: int | None = None
    normal_max: int | None = None
    seen_keys: set[str] = field(default_factory=set)
    new_spawns: int = 0  # first-seen normal keys in this phase
    name_counter: Counter = field(default_factory=Counter)

    def note(self, snap: MobFreqSnapshot) -> None:
        self.samples += 1
        self.normal_sum += int(snap.normal_n)
        if self.normal_min is None or snap.normal_n < self.normal_min:
            self.normal_min = int(snap.normal_n)
        if self.normal_max is None or snap.normal_n > self.normal_max:
            self.normal_max = int(snap.normal_n)
        for k in snap.normal_keys:
            if k not in self.seen_keys:
                self.seen_keys.add(k)
                self.new_spawns += 1
        self.name_counter.update(snap.normal_names)

    def avg_normal(self) -> float:
        if self.samples <= 0:
            return 0.0
        return float(self.normal_sum) / float(self.samples)

    def elapsed(self, now: float | None = None) -> float:
        now = time.time() if now is None else float(now)
        return max(0.0, now - float(self.t0))

    def spawn_per_min(self, now: float | None = None) -> float:
        el = self.elapsed(now)
        if el < 1.0:
            return 0.0
        return float(self.new_spawns) * 60.0 / el

    def summary(self, now: float | None = None) -> str:
        el = self.elapsed(now)
        top = ",".join(
            f"{n}×{c}" for n, c in self.name_counter.most_common(4)
        ) or "-"
        return (
            f"射手={self.archer_n} 采样={self.samples} 历时={el:.0f}s "
            f"可战斗均={self.avg_normal():.1f} 范围=[{self.normal_min},{self.normal_max}] "
            f"新出现={self.new_spawns} (~{self.spawn_per_min(now):.1f}/min) top=[{top}]"
        )


def _is_ignored_name(name: str) -> bool:
    """True for static/non-combat clutter (木桶/房屋/炸弹…). @author by ak"""
    n = (name or "").strip()
    if not n:
        return True
    if n in IGNORE_MOB_NAMES:
        return True
    for sub in IGNORE_MOB_NAME_SUBSTR:
        if sub and sub in n:
            return True
    return False


def _obj_key(o) -> str:
    """Stable-ish key for spawn tracking. @author by ak"""
    tid = getattr(o, "tid", None)
    ptr = int(getattr(o, "ptr", 0) or 0)
    oid = getattr(o, "obj_id", None)
    if oid is not None:
        return f"id:{int(oid)}"
    if tid is not None and getattr(o, "x", None) is not None:
        # coarse grid so same mob near same spot after respawn can reappear as new
        try:
            x = float(o.x)
            z = float(o.z) if getattr(o, "z", None) is not None else 0.0
            return f"tid:{int(tid)}@{int(x // 5)}:{int(z // 5)}:{ptr & 0xFFFF}"
        except Exception:
            pass
    return f"ptr:{ptr:X}"


def take_snapshot(
    session,
    *,
    host_pos: tuple[float, float, float] | None = None,
    radius: float = 200.0,
    limit: int = 256,
    archer_name: str = ARCHER_NAME,
    log: LogFn | None = None,
) -> MobFreqSnapshot:
    """
    One AOI sample: count 大漠射手 + combat normals.

    木桶/房屋/炸弹等静物记入 ignored，不进普怪/新出现统计。

    @author by ak
    """
    log = log or (lambda _m: None)
    now = time.time()
    try:
        objs = list_class_objects(
            session,
            CLASS_NPC,
            host_pos=host_pos,
            radius=float(radius) if radius else None,
            limit=int(limit),
            log=lambda _m: None,
        )
    except Exception as e:
        es = str(e)
        log(f"mob_freq scan err: {es}")
        low = es.lower()
        # 进程侧 CRT/VirtualAlloc 已拒绝：上抛让挂机循环停扫，勿吞成 0 怪继续打
        if any(
            k in low
            for k in (
                "hard-stop",
                "virtualalloc",
                "access is denied",
                "createremotethread",
            )
        ):
            raise MobFreqHardStop(es) from e
        return MobFreqSnapshot(t=now, archer_n=0, raw_n=0)

    target = (archer_name or ARCHER_NAME).strip()
    archers = []
    normals = []
    ignored = []
    for o in objs or []:
        name = str(getattr(o, "name", "") or "").strip()
        if not name:
            continue
        if target in name:
            archers.append(o)
        elif _is_ignored_name(name):
            ignored.append(o)
        else:
            normals.append(o)

    a_keys = {_obj_key(o) for o in archers}
    n_keys = {_obj_key(o) for o in normals}
    n_names: Counter = Counter()
    for o in normals:
        n_names[str(getattr(o, "name", "") or "?").strip()] += 1
    i_names: Counter = Counter()
    for o in ignored:
        i_names[str(getattr(o, "name", "") or "?").strip()] += 1

    return MobFreqSnapshot(
        t=now,
        archer_n=len(archers),
        archer_keys=a_keys,
        normal_n=len(normals),
        normal_keys=n_keys,
        normal_names=n_names,
        raw_n=len(objs or []),
        ignored_n=len(ignored),
        ignored_names=i_names,
    )


class MobFreqAnalyzer:
    """
    Stateful low-frequency 怪频分析 for 切糕.

    Tracks phases by 大漠射手 remaining count (4→3→2…).

    @author by ak
    """

    def __init__(
        self,
        *,
        archer_name: str = ARCHER_NAME,
        expected: int = ARCHER_EXPECTED,
        interval_s: float = DEFAULT_INTERVAL_S,
        radius: float = 200.0,
        log: LogFn | None = None,
    ):
        self.archer_name = archer_name
        self.expected = int(expected)
        self.interval_s = max(30.0, float(interval_s))  # hang-safe floor
        self.radius = float(radius)
        self.log = log or (lambda _m: None)
        self._next_t = 0.0
        self._phase: PhaseStats | None = None
        self._phases: list[PhaseStats] = []
        self._last_snap: MobFreqSnapshot | None = None
        self._started = False

    def reset(self) -> None:
        self._next_t = 0.0
        self._phase = None
        self._phases = []
        self._last_snap = None
        self._started = False

    def maybe_tick(
        self,
        session,
        *,
        host_pos: tuple[float, float, float] | None = None,
        force: bool = False,
    ) -> str | None:
        """
        Run sample if interval elapsed. Returns log line or None if skipped.

        @author by ak
        """
        now = time.time()
        if not force and now < self._next_t:
            return None
        self._next_t = now + self.interval_s
        snap = take_snapshot(
            session,
            host_pos=host_pos,
            radius=self.radius,
            archer_name=self.archer_name,
            log=self.log,
        )
        return self._ingest(snap)

    def _ingest(self, snap: MobFreqSnapshot) -> str:
        lines: list[str] = []
        if not self._started:
            self._started = True
            lines.append(
                f"[分析怪频] 启动 目标={self.archer_name} 期望={self.expected} "
                f"间隔={self.interval_s:.0f}s r={self.radius:.0f} "
                f"排除干扰={",".join(sorted(IGNORE_MOB_NAMES)[:6])}…"
            )

        prev = self._last_snap
        prev_a = None if prev is None else int(prev.archer_n)
        cur_a = int(snap.archer_n)

        # phase switch when archer count changes
        if self._phase is None or self._phase.archer_n != cur_a:
            if self._phase is not None:
                self._phases.append(self._phase)
                lines.append(
                    f"[分析怪频] 阶段结束 射手{self._phase.archer_n}→{cur_a} · "
                    + self._phase.summary(snap.t)
                )
                # compare to previous phase
                if len(self._phases) >= 1:
                    base = None
                    for ph in self._phases:
                        if ph.archer_n == self.expected and ph.samples >= 1:
                            base = ph
                            break
                    if base is None and self._phases:
                        base = self._phases[0]
                    if base is not None and base.archer_n != cur_a:
                        lines.append(
                            f"[分析怪频] 对比基线(射手={base.archer_n} 均可战斗={base.avg_normal():.1f} "
                            f"新出={base.new_spawns} {base.spawn_per_min(snap.t):.1f}/min) "
                            f"vs 当前射手={cur_a} 可战斗={snap.normal_n} 干扰={snap.ignored_n}"
                        )
            self._phase = PhaseStats(archer_n=cur_a, t0=snap.t)
            if prev_a is not None and cur_a < prev_a:
                lines.append(
                    f"[分析怪频] ★ 大漠射手减少 {prev_a}→{cur_a} "
                    f"（正常{self.expected}）开始观察普怪刷新"
                )
            elif prev_a is not None and cur_a > prev_a:
                lines.append(
                    f"[分析怪频] 大漠射手回升 {prev_a}→{cur_a}"
                )

        assert self._phase is not None
        self._phase.note(snap)
        self._last_snap = snap

        miss = max(0, self.expected - cur_a)
        top = ",".join(f"{n}×{c}" for n, c in snap.normal_names.most_common(5)) or "-"
        ign_top = (
            ",".join(f"{n}×{c}" for n, c in snap.ignored_names.most_common(3)) or "-"
        )
        lines.append(
            f"[分析怪频] 射手={cur_a}/{self.expected}(少{miss}) "
            f"可战斗={snap.normal_n} 干扰={snap.ignored_n} AOI={snap.raw_n} "
            f"阶段新出={self._phase.new_spawns}(~{self._phase.spawn_per_min(snap.t):.1f}/min) "
            f"可战斗名=[{top}] 干扰名=[{ign_top}]"
        )
        msg = " | ".join(lines)
        self.log(msg)
        return msg

    def final_report(self) -> str:
        """Collapse phases for stop/return. @author by ak"""
        now = time.time()
        if self._phase is not None and (
            not self._phases or self._phases[-1] is not self._phase
        ):
            # don't double-append same object if already closed
            if not self._phases or self._phases[-1].t0 != self._phase.t0:
                self._phases.append(self._phase)
        if not self._phases:
            return "[分析怪频] 无采样"
        parts = ["[分析怪频] 汇总:"]
        base = None
        for ph in self._phases:
            parts.append("  · " + ph.summary(now))
            if ph.archer_n == self.expected and base is None:
                base = ph
        if base is None and self._phases:
            base = self._phases[0]
        # delta table
        if base is not None and len(self._phases) > 1:
            parts.append(
                f"  · 基线射手={base.archer_n} 可战斗均={base.avg_normal():.1f} "
                f"刷新~{base.spawn_per_min(now):.1f}/min"
            )
            for ph in self._phases:
                if ph is base:
                    continue
                d_avg = ph.avg_normal() - base.avg_normal()
                d_rate = ph.spawn_per_min(now) - base.spawn_per_min(now)
                parts.append(
                    f"  · 射手={ph.archer_n} 相对基线 可战斗均{d_avg:+.1f} "
                    f"刷新{d_rate:+.1f}/min "
                    f"{'↑更密' if d_rate > 0.2 or d_avg > 0.5 else ('↓更稀' if d_rate < -0.2 or d_avg < -0.5 else '≈接近')}"
                )
        msg = "\n".join(parts)
        self.log(msg)
        return msg
