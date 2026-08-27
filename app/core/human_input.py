# -*- coding: utf-8 -*-
"""
Small human-like timing / point / path helpers for UI interaction.

Used by captcha select/confirm so clicks are not perfect-center + fixed gaps.
Includes press-hold sampling and Bezier slide trajectories between points.
@author by ak
"""
from __future__ import annotations

import math
import random
import time
from typing import Callable

LogFn = Callable[[str], None]


def clamp(v: float, lo: float, hi: float) -> float:
    """Clamp value into [lo, hi]. @author by ak"""
    return max(float(lo), min(float(hi), float(v)))


def human_delay_s(min_s: float, max_s: float) -> float:
    """
    Sample a delay in seconds.

    Slightly right-skewed (more short-medium, occasional longer think).
    @author by ak
    """
    a = max(0.0, float(min_s))
    b = max(a, float(max_s))
    if b <= a:
        return a
    t = random.betavariate(2.0, 3.2)
    return a + (b - a) * t


def human_hold_ms(min_ms: int = 70, max_ms: int = 160) -> int:
    """
    Mouse button hold duration in ms (human press, not instant click).

    Defaults ~70-160ms with a soft peak around 90-120ms.
    @author by ak
    """
    a = max(15, int(min_ms))
    b = max(a, int(max_ms))
    if b <= a:
        return a
    t = random.betavariate(2.4, 3.0)
    return int(round(a + (b - a) * t))


def point_in_rect(
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    inset_frac: float = 0.18,
    center_bias: float = 0.35,
) -> tuple[int, int]:
    """
    Random client point inside a control/cell rect (not dead center).

    inset_frac: keep away from edges.
    center_bias: 0 = uniform in inset, 1 = almost always near center.
    @author by ak
    """
    w = max(1, int(w))
    h = max(1, int(h))
    x = int(x)
    y = int(y)
    inset = clamp(inset_frac, 0.0, 0.45)
    ix = x + int(round(w * inset))
    iy = y + int(round(h * inset))
    iw = max(1, w - 2 * int(round(w * inset)))
    ih = max(1, h - 2 * int(round(h * inset)))
    bias = clamp(center_bias, 0.0, 1.0)
    ux = ix + random.random() * iw
    uy = iy + random.random() * ih
    cx = ix + iw * 0.5
    cy = iy + ih * 0.5
    gx = random.gauss(cx, max(1.0, iw * 0.22))
    gy = random.gauss(cy, max(1.0, ih * 0.22))
    px = ux * (1.0 - bias) + gx * bias
    py = uy * (1.0 - bias) + gy * bias
    px = clamp(px, float(ix), float(ix + iw - 1))
    py = clamp(py, float(iy), float(iy + ih - 1))
    return int(round(px)), int(round(py))


def jitter_around(
    cx: int,
    cy: int,
    *,
    radius_x: int = 8,
    radius_y: int = 8,
) -> tuple[int, int]:
    """Small offset around a point when no full rect is available. @author by ak"""
    rx = max(0, int(radius_x))
    ry = max(0, int(radius_y))
    if rx <= 0 and ry <= 0:
        return int(cx), int(cy)
    dx = random.randint(-rx, rx) if rx else 0
    dy = random.randint(-ry, ry) if ry else 0
    return int(cx) + dx, int(cy) + dy


def _cubic_bezier(
    t: float,
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
) -> tuple[float, float]:
    u = 1.0 - t
    tt = t * t
    uu = u * u
    uuu = uu * u
    ttt = tt * t
    x = uuu * p0[0] + 3.0 * uu * t * p1[0] + 3.0 * u * tt * p2[0] + ttt * p3[0]
    y = uuu * p0[1] + 3.0 * uu * t * p1[1] + 3.0 * u * tt * p2[1] + ttt * p3[1]
    return x, y


def human_slide_path(
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    *,
    min_steps: int = 4,
    max_steps: int = 10,
    duration_min_s: float = 0.08,
    duration_max_s: float = 0.28,
    curve_strength: float = 0.28,
) -> tuple[list[tuple[int, int]], float]:
    """
    Build a curved slide path from (x0,y0) to (x1,y1).

    Returns (points including end, total_duration_s).
    @author by ak
    """
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    dx = float(x1 - x0)
    dy = float(y1 - y0)
    dist = math.hypot(dx, dy)
    lo = max(2, int(min_steps))
    hi = max(lo, int(max_steps))
    if dist < 12:
        steps = lo
    else:
        steps = int(round(lo + (hi - lo) * clamp(dist / 220.0, 0.0, 1.0)))
        steps = max(lo, min(hi, steps + random.randint(-1, 1)))

    if dist < 1e-3:
        nx, ny = 0.0, 0.0
    else:
        nx, ny = -dy / dist, dx / dist
    amp = max(4.0, dist * float(curve_strength) * random.uniform(0.45, 1.0))
    side = 1.0 if random.random() < 0.5 else -1.0
    c1 = (
        x0 + dx * random.uniform(0.22, 0.40) + nx * amp * side * random.uniform(0.5, 1.1),
        y0 + dy * random.uniform(0.22, 0.40) + ny * amp * side * random.uniform(0.5, 1.1),
    )
    c2 = (
        x0 + dx * random.uniform(0.58, 0.80) + nx * amp * (-side) * random.uniform(0.15, 0.7),
        y0 + dy * random.uniform(0.58, 0.80) + ny * amp * (-side) * random.uniform(0.15, 0.7),
    )
    p0 = (float(x0), float(y0))
    p3 = (float(x1), float(y1))

    pts: list[tuple[int, int]] = []
    for i in range(1, steps + 1):
        u = i / float(steps)
        te = u * u * (3.0 - 2.0 * u)
        x, y = _cubic_bezier(te, p0, c1, c2, p3)
        damp = 1.0 - te
        j = 1.2 * damp
        x += random.uniform(-j, j)
        y += random.uniform(-j, j)
        pts.append((int(round(x)), int(round(y))))
    if pts:
        pts[-1] = (x1, y1)
    else:
        pts = [(x1, y1)]

    # End hover micro-jitter (human hand settles before click, reduces "fly-in").
    # 2~4 tiny offsets around target, then snap to exact end.
    n_jitter = random.randint(2, 4)
    for _ in range(n_jitter):
        jx = x1 + random.randint(-5, 5)
        jy = y1 + random.randint(-4, 4)
        pts.append((jx, jy))
    pts.append((x1, y1))

    d_lo = max(0.02, float(duration_min_s))
    d_hi = max(d_lo, float(duration_max_s))
    scale = clamp(0.7 + dist / 280.0, 0.7, 1.45)
    # Shorter when we added end jitter (less time for fly-in)
    duration = human_delay_s(d_lo, d_hi) * scale * 0.85
    return pts, float(duration)


def human_inter_click_gap_s(base_s: float = 0.28, jitter_s: float = 0.35) -> float:
    """Gap between successive captcha cell clicks. @author by ak"""
    base = max(0.05, float(base_s))
    jit = max(0.0, float(jitter_s))
    return base + human_delay_s(0.0, jit) if jit > 0 else base


def sleep_human(
    min_s: float,
    max_s: float,
    stop_event=None,
    *,
    step: float = 0.05,
) -> bool:
    """Interruptible human delay. Returns False if stop_event set. @author by ak"""
    seconds = human_delay_s(min_s, max_s)
    end = time.time() + seconds
    while time.time() < end:
        if stop_event is not None and stop_event.is_set():
            return False
        time.sleep(min(step, max(0.0, end - time.time())))
    return True
