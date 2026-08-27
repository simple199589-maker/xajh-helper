from __future__ import annotations

import threading
import time
import unittest

from app.core.runner import RunnerLifecycle, interruptible_sleep


class RunnerTests(unittest.TestCase):
    def test_interruptible_sleep_stops_promptly(self) -> None:
        event = threading.Event()
        event.set()
        started = time.monotonic()
        self.assertFalse(interruptible_sleep(2, event))
        self.assertLess(time.monotonic() - started, 0.2)

    def test_lifecycle_is_idempotent(self) -> None:
        life = RunnerLifecycle("test-runner")

        def worker() -> None:
            interruptible_sleep(2, life.stop_event)

        first = life.start(worker)
        self.assertIsNotNone(first)
        self.assertIsNone(life.start(worker))
        life.stop()
        first.join(1)
        self.assertFalse(life.is_running())

    def test_stop_reports_worker_still_alive_after_bounded_wait(self) -> None:
        life = RunnerLifecycle("blocked-runner")
        release = threading.Event()
        first = life.start(lambda: release.wait(1))
        self.assertIsNotNone(first)
        self.assertFalse(life.stop(wait=True, timeout=0.001))
        self.assertTrue(life.is_running())
        self.assertIsNone(life.start(lambda: None))
        release.set()
        first.join(1)
        self.assertFalse(life.is_running())


if __name__ == "__main__":
    unittest.main()
