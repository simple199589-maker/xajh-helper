# -*- coding: utf-8 -*-
"""SafeDispatch unit tests. @author by ak"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from app.core import remote_runtime
from app.core.safe_dispatch import (
    OpKind,
    PidHealth,
    Priority,
    RemoteBlockedError,
    SafeDispatch,
    bag_cache_key,
    get_dispatch,
    reset_dispatch_for_tests,
)


class SafeDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_dispatch_for_tests()
        remote_runtime.clear_pid_remote_state(4242)
        remote_runtime.clear_pid_remote_state(4243)

    def tearDown(self) -> None:
        reset_dispatch_for_tests()
        remote_runtime.clear_pid_remote_state(4242)
        remote_runtime.clear_pid_remote_state(4243)

    def test_single_flight_bag_read(self) -> None:
        d = get_dispatch()
        calls = {"n": 0}
        barrier = threading.Barrier(3)

        def prod():
            calls["n"] += 1
            time.sleep(0.05)
            return {"items": [1, 2, 3], "n": calls["n"]}

        results = []

        def worker():
            barrier.wait()
            results.append(
                d.read_cached(
                    4242,
                    bag_cache_key([2, 3]),
                    prod,
                    max_age=1.0,
                    ttl_s=1.0,
                    kind=OpKind.LOCAL,
                    priority=Priority.P2,
                )
            )

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(2.0)
        self.assertEqual(len(results), 3)
        self.assertEqual(calls["n"], 1)
        self.assertTrue(all(r["items"] == [1, 2, 3] for r in results))

    def test_cache_invalidate(self) -> None:
        d = get_dispatch()
        n = {"v": 0}

        def prod():
            n["v"] += 1
            return n["v"]

        a = d.read_cached(4242, "bag:x", prod, max_age=1, ttl_s=1, kind=OpKind.LOCAL)
        b = d.read_cached(4242, "bag:x", prod, max_age=1, ttl_s=1, kind=OpKind.LOCAL)
        self.assertEqual(a, 1)
        self.assertEqual(b, 1)
        d.invalidate(4242, "bag")
        c = d.read_cached(4242, "bag:x", prod, max_age=1, ttl_s=1, kind=OpKind.LOCAL)
        self.assertEqual(c, 2)

    def test_pid_cache_isolation(self) -> None:
        d = get_dispatch()
        d.put_cached(1, "bag:k", "A", ttl_s=5)
        d.put_cached(2, "bag:k", "B", ttl_s=5)
        self.assertEqual(d.get_cached(1, "bag:k"), "A")
        self.assertEqual(d.get_cached(2, "bag:k"), "B")
        d.invalidate(1, "bag")
        self.assertIsNone(d.get_cached(1, "bag:k"))
        self.assertEqual(d.get_cached(2, "bag:k"), "B")

    def test_priority_queue_orders_crt(self) -> None:
        d = get_dispatch()
        order: list[str] = []
        gate = threading.Event()

        def slow_p3():
            gate.wait(1.0)
            order.append("p3")
            return "p3"

        def fast_p0():
            order.append("p0")
            return "p0"

        # enqueue p3 first without waiting
        th = threading.Thread(
            target=lambda: d.submit(
                4242,
                slow_p3,
                priority=Priority.P3,
                kind=OpKind.CRT_READ,
                wait=True,
            )
        )
        th.start()
        time.sleep(0.05)  # let p3 become active or queued
        # release and submit p0 - if p3 already running, p0 waits; if not, p0 first
        # Better test: block worker with a hold, queue p3 then p0, release
        reset_dispatch_for_tests()
        d = get_dispatch()
        started = threading.Event()
        release = threading.Event()
        order.clear()

        def hold():
            started.set()
            release.wait(2.0)
            order.append("hold")
            return "hold"

        def p3():
            order.append("p3")
            return "p3"

        def p0():
            order.append("p0")
            return "p0"

        t_hold = threading.Thread(
            target=lambda: d.submit(
                4242, hold, priority=Priority.P2, kind=OpKind.CRT_READ, wait=True
            )
        )
        t_hold.start()
        self.assertTrue(started.wait(1.0))
        # queue p3 then p0 while hold runs
        t3 = threading.Thread(
            target=lambda: d.submit(
                4242, p3, priority=Priority.P3, kind=OpKind.CRT_READ, wait=True
            )
        )
        t0 = threading.Thread(
            target=lambda: d.submit(
                4242, p0, priority=Priority.P0, kind=OpKind.CRT_READ, wait=True
            )
        )
        t3.start()
        time.sleep(0.02)
        t0.start()
        time.sleep(0.02)
        release.set()
        t_hold.join(2)
        t0.join(2)
        t3.join(2)
        # after hold: p0 before p3
        self.assertIn("hold", order)
        self.assertLess(order.index("p0"), order.index("p3"))

    def test_hard_dead_blocks_crt(self) -> None:
        remote_runtime.mark_pid_hard_dead(4242)
        d = get_dispatch()
        self.assertEqual(d.pid_health(4242), PidHealth.HARD_DEAD)
        with self.assertRaises(RemoteBlockedError):
            d.submit(
                4242,
                lambda: 1,
                kind=OpKind.CRT_WRITE,
                priority=Priority.P0,
                bypass_queue=True,
            )

    def test_cross_pid_independent(self) -> None:
        d = get_dispatch()
        remote_runtime.mark_pid_hard_dead(4242)
        # other pid still ok
        v = d.submit(
            4243,
            lambda: 99,
            kind=OpKind.LOCAL,
            priority=Priority.P1,
            bypass_queue=True,
        )
        self.assertEqual(v, 99)

    def test_periodic_cancel(self) -> None:
        d = get_dispatch()
        hits = {"n": 0}

        def tick():
            hits["n"] += 1

        d.schedule_periodic(
            4242, "t1", 0.05, tick, priority=Priority.P2, kind=OpKind.LOCAL
        )
        time.sleep(0.18)
        d.cancel_periodic(4242, "t1")
        n1 = hits["n"]
        self.assertGreaterEqual(n1, 2)
        time.sleep(0.12)
        self.assertEqual(hits["n"], n1)

    def test_rpm_periodic_keeps_running_during_remote_hung_cooldown(self) -> None:
        d = get_dispatch()
        rpm_hits = {"n": 0}
        crt_hits = {"n": 0}
        remote_runtime.mark_pid_remote_hung(4242, cooldown_sec=2.0)

        d.schedule_periodic(
            4242,
            "rpm-safety-gate",
            0.05,
            lambda: rpm_hits.__setitem__("n", rpm_hits["n"] + 1),
            priority=Priority.P1,
            kind=OpKind.RPM,
        )
        d.schedule_periodic(
            4242,
            "crt-paused",
            0.05,
            lambda: crt_hits.__setitem__("n", crt_hits["n"] + 1),
            priority=Priority.P1,
            kind=OpKind.CRT_READ,
        )
        time.sleep(0.18)
        d.cancel_periodic(4242, "rpm-safety-gate")
        d.cancel_periodic(4242, "crt-paused")

        self.assertGreaterEqual(rpm_hits["n"], 2)
        self.assertEqual(crt_hits["n"], 0)


    def test_session_blocked(self) -> None:
        from app.core.safe_dispatch import session_blocked

        remote_runtime.mark_pid_hard_dead(4242)
        b, r = session_blocked(4242)
        self.assertTrue(b)
        self.assertIn("hard_dead", r)
        b2, r2 = session_blocked(4243)
        self.assertFalse(b2)

    def test_note_exception_marks_hard_dead(self) -> None:
        d = get_dispatch()
        d.note_exception(4242, OSError("VirtualAllocEx failed err=5"))
        self.assertTrue(remote_runtime.is_pid_hard_dead(4242))


    def test_business_gates_honor_hard_dead(self) -> None:
        """Entry gates on team/map/qshop must early-return when hard_dead."""
        from app.core.team_ops import leave_team, set_team_follow, invite_player_by_id
        from app.core.map_fly import open_transmit_flag
        from app.core.qshop_buy import buy_qshop_present

        class Sess:
            pid = 4242
            module_base = 0x400000

        remote_runtime.mark_pid_hard_dead(4242)
        s = Sess()
        r1 = leave_team(s, log=lambda _m: None)
        self.assertFalse(r1.ok)
        self.assertIn("remote", (r1.error or r1.message).lower())
        r2 = set_team_follow(s, enabled=True, log=lambda _m: None)
        self.assertFalse(r2.ok)
        r3 = invite_player_by_id(s, 123456, log=lambda _m: None)
        self.assertFalse(r3.ok)
        r4 = open_transmit_flag(s, log=lambda _m: None)
        self.assertFalse(r4.ok)
        r5 = buy_qshop_present(s, log=lambda _m: None)
        self.assertFalse(r5.ok)

    def test_superloot_loop_checks_session_blocked(self) -> None:
        import inspect
        from app.core.loot._impl import SuperLootRunner

        src = inspect.getsource(SuperLootRunner._loop)
        self.assertIn("session_blocked", src)
        self.assertIn("hard_dead", src)

    def test_full_gold_loop_stops_when_hard_dead(self) -> None:
        from app.core.package_api import ensure_full_gold, PackageActionResult

        class Sess:
            pid = 4242

        remote_runtime.mark_pid_hard_dead(4242)

        def fake_money(*_a, **_k):
            return PackageActionResult(ok=True, action="money", money=0, message="m")

        with patch("app.core.package_api.get_money", side_effect=fake_money):
            r = ensure_full_gold(
                Sess(),
                continuous=True,
                min_money=10_000,
                log=lambda _m: None,
            )
        self.assertFalse(r.ok if r.error else True)
        self.assertTrue(r.error)
        self.assertIn("remote", (r.error or "").lower() + (r.message or "").lower())


if __name__ == "__main__":
    unittest.main()
