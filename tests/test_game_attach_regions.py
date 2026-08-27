# -*- coding: utf-8 -*-
"""Two-phase region iteration tests (classic heap first, LAA window after)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.core.game_attach import (
    REGION_LIMIT_CLASSIC,
    _iter_readable_regions,
    _iter_writable_regions,
)

PAGE_READWRITE = 0x04
PAGE_NOACCESS = 0x01
MEM_COMMIT = 0x1000
MEM_FREE = 0x10000


class _FakeMem:
    """Regions list of (base, size); virtual_query resolves address -> region."""

    def __init__(self, regions):
        self.regions = regions
        self.process_handle = object()

    def query(self, handle, address):
        next_bases = []
        for base, size in self.regions:
            if base <= address < base + size:
                return SimpleNamespace(
                    BaseAddress=base,
                    RegionSize=size,
                    Protect=PAGE_READWRITE,
                    State=MEM_COMMIT,
                )
            if base > address:
                next_bases.append(base)
        if not next_bases:
            raise RuntimeError("end of address space")
        # miss inside a gap: report a free placeholder that steps to the
        # next known region so the walk keeps advancing like the real OS.
        nb = min(next_bases)
        return SimpleNamespace(
            BaseAddress=nb,
            RegionSize=0x1000,
            Protect=PAGE_NOACCESS,
            State=MEM_FREE,
        )


REGIONS = [
    (0x00100000, 0x10000),          # classic low heap
    (0x40000000, 0x20000),          # classic mid
    (REGION_LIMIT_CLASSIC + 0x10000, 0x30000),  # LAA high window
]


def _run(iter_fn, max_regions):
    fake = _FakeMem(REGIONS)
    with mock.patch("pymem.memory.virtual_query", fake.query):
        return list(iter_fn(fake, max_regions=max_regions))


class GameAttachRegionIterationTest(unittest.TestCase):
    def test_low_windows_first_then_laa_high(self) -> None:
        out = _run(_iter_readable_regions, 16)
        self.assertEqual(
            [(b, s) for b, s in out],
            [r[:2] if isinstance(r, tuple) and len(r) == 2 else r for r in REGIONS],
        )
        # order: both classic regions precede the high-window one
        self.assertLess(out[0][0], REGION_LIMIT_CLASSIC)
        self.assertLess(out[1][0], REGION_LIMIT_CLASSIC)
        self.assertGreaterEqual(out[2][0], REGION_LIMIT_CLASSIC)

    def test_quota_stops_after_classic_regions(self) -> None:
        out = _run(_iter_readable_regions, 2)
        self.assertEqual(len(out), 2)
        self.assertTrue(all(b < REGION_LIMIT_CLASSIC for b, _ in out))

    def test_quota_one_only_first_region(self) -> None:
        out = _run(_iter_readable_regions, 1)
        self.assertEqual([b for b, _ in out], [REGIONS[0][0]])

    def test_writable_iteration_uses_same_layout(self) -> None:
        out = _run(_iter_writable_regions, 16)
        self.assertEqual(len(out), len(REGIONS))
        self.assertGreaterEqual(out[-1][0], REGION_LIMIT_CLASSIC)


if __name__ == "__main__":
    unittest.main()
