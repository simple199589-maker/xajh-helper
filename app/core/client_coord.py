# -*- coding: utf-8 -*-
"""Unified client-coordinate mapping between client sizes.

The game UI is laid out in a fixed reference client (1600x900, 16:9); verified
click coordinates are recorded in that space. To click at a live client size
the reference point is mapped onto the physical client:

  * same aspect (16:9)  -> uniform proportional scale (x/y factors equal);
  * other aspect        -> the 16:9 content is scaled uniformly to FIT the
    client and centered (letterbox bars on the leftover axis), instead of a
    naive independent x/y stretch that would misplace every click.

This single mapper is shared by the login flow (server-confirm / credentials /
character-select clicks) so every resolution follows the same model.

@author by ak
"""
from __future__ import annotations

from typing import Callable

# Reference client the verified coordinates are recorded on.
DEFAULT_REF_SIZE = (1600, 900)
# Aspect-ratio tolerance: two sizes are treated as proportional when their
# ratios differ by less than this.
_ASPECT_TOL = 0.01

LogFn = Callable[[str], None]


def _client_size(hwnd: int) -> tuple[int, int] | None:
    """Live physical client size of hwnd (per-monitor DPI aware). @author by ak"""
    from app.core.window_layout import get_client_size

    return get_client_size(hwnd)


class ClientCoordMapper:
    """Map points/rects from a reference client size to a live client size.

    Handles both the proportional 16:9 case and the letterbox case (uniform
    scale + centering) so coordinates stay precise at any window ratio.

    @author by ak
    """

    DEFAULT_REF = DEFAULT_REF_SIZE

    def __init__(
        self,
        from_size: tuple[int, int] = DEFAULT_REF_SIZE,
        to_size: tuple[int, int] = DEFAULT_REF_SIZE,
    ) -> None:
        self.ref_w = max(1, int(from_size[0]))
        self.ref_h = max(1, int(from_size[1]))
        self.to_w = max(1, int(to_size[0]))
        self.to_h = max(1, int(to_size[1]))
        sx = self.to_w / self.ref_w
        sy = self.to_h / self.ref_h
        if abs(sx - sy) <= _ASPECT_TOL:
            # Same aspect: uniform proportional scale, no letterbox bars.
            self._scale = sx
            self._ox = 0.0
            self._oy = 0.0
        else:
            # Different aspect: fit the reference content uniformly + center.
            self._scale = min(sx, sy)
            self._ox = (self.to_w - self.ref_w * self._scale) / 2.0
            self._oy = (self.to_h - self.ref_h * self._scale) / 2.0

    @property
    def scale(self) -> float:
        """Uniform scale factor applied to the reference space. @author by ak"""
        return self._scale

    @property
    def aspect_mismatch(self) -> float:
        """Absolute difference between the reference and target aspect. @author by ak"""
        ref = self.ref_w / self.ref_h
        to = self.to_w / self.to_h
        return abs(to - ref)

    def scale_point(self, x, y=None) -> tuple[int, int]:
        """Map a reference-space point to the live client. @author by ak"""
        if y is None:
            x, y = x
        px = int(self._ox + int(x) * self._scale)
        py = int(self._oy + int(y) * self._scale)
        return px, py

    def scale_rect(self, x, y, w, h) -> tuple[int, int, int, int]:
        """Map a reference-space rect to the live client. @author by ak"""
        px, py = self.scale_point(x, y)
        return (
            px,
            py,
            max(1, int(int(w) * self._scale)),
            max(1, int(int(h) * self._scale)),
        )

    @classmethod
    def for_hwnd(
        cls,
        hwnd: int,
        from_size: tuple[int, int] = DEFAULT_REF_SIZE,
    ) -> "ClientCoordMapper":
        """Mapper from the reference client to hwnd's live client size. @author by ak"""
        to = _client_size(hwnd)
        if not to or to[0] <= 0 or to[1] <= 0:
            to = from_size
        return cls(from_size, to)


def scale_client_coord(
    xy: tuple[int, int],
    from_size: tuple[int, int],
    to_size: tuple[int, int],
) -> tuple[int, int]:
    """One-shot coordinate scaling via ClientCoordMapper (backward compatible).

    @author by ak
    """
    return ClientCoordMapper(from_size, to_size).scale_point(xy)


def client_size_diag(hwnd: int) -> dict:
    """Diagnostic client metrics of hwnd: DPI, physical (per-monitor aware) and
    logical (DPI-unaware) client sizes, plus the resulting aspect mismatch.

    Helps tell whether the game window is DPI-virtualized (physical != logical)
    which changes how the game interprets posted mouse coordinates.
    @author by ak
    """
    import ctypes
    import ctypes.wintypes as wt

    from app.core import win_utils
    from app.core.window_layout import get_client_size, get_window_dpi

    dpi = get_window_dpi(hwnd)
    physical = get_client_size(hwnd)
    logical = None
    try:
        prev = win_utils._dpi_ctx_unaware()
        try:
            rc = win_utils.RECT()
            if win_utils.user32.GetClientRect(wt.HWND(int(hwnd)), ctypes.byref(rc)):
                logical = (int(rc.right - rc.left), int(rc.bottom - rc.top))
        finally:
            win_utils._dpi_ctx_restore(prev)
    except Exception:
        pass
    return {
        "dpi": int(dpi or 0),
        "physical": tuple(physical) if physical else None,
        "logical": logical,
    }


def client_aspect_mismatch(
    client_size: tuple[int, int],
    *,
    target_aspect: float = 16.0 / 9.0,
) -> float:
    """Return how far a client size's aspect ratio is from ``target_aspect``.

    0.0 when the ratio matches; inf for an invalid size. @author by ak
    """
    w, h = int(client_size[0]), int(client_size[1])
    if w <= 0 or h <= 0:
        return float("inf")
    return abs((w / h) - target_aspect)
