import unittest
from unittest.mock import MagicMock, patch
from app.core.state_dispatch import (
    StateKind, get_state, require_fresh, prefetch_states, peek_state, invalidate_states,
)
from app.core.safe_dispatch import reset_dispatch_for_tests, get_dispatch
from app.core.remote_runtime import clear_pid_remote_state

class StateDispatchTests(unittest.TestCase):
    def setUp(self):
        reset_dispatch_for_tests()
        clear_pid_remote_state(9001)
        self.sess = MagicMock()
        self.sess.pid = 9001

    def tearDown(self):
        reset_dispatch_for_tests()
        clear_pid_remote_state(9001)

    def test_prefetch_and_peek_bag(self):
        items = [1, 2, 3]
        with patch("app.core.state_dispatch._produce", return_value=items) as prod:
            # call get_state via real produce path by patching package list
            pass
        with patch("app.core.package_api.list_packages_items", return_value=items) as lp:
            out = get_state(self.sess, StateKind.BAG, fresh=False)
            self.assertEqual(out, items)
            self.assertEqual(lp.call_count, 1)
            out2 = get_state(self.sess, StateKind.BAG, fresh=False)
            self.assertEqual(out2, items)
            # second should cache hit (no second producer if TTL allows)
            # list_packages called only from producer; cache hit => still 1
            self.assertEqual(lp.call_count, 1)

    def test_require_fresh_forces_producer(self):
        n = {"v": 0}
        def fake_list(*a, **k):
            n["v"] += 1
            return [n["v"]]
        with patch("app.core.package_api.list_packages_items", side_effect=fake_list):
            a = require_fresh(self.sess, StateKind.BAG)
            b = require_fresh(self.sess, StateKind.BAG)
            self.assertEqual(a, [1])
            self.assertEqual(b, [2])
            self.assertEqual(n["v"], 2)

    def test_require_fresh_replaces_warm_cache(self):
        values = iter((["warm"], ["fresh"]))
        with patch("app.core.package_api.list_packages_items", side_effect=lambda *a, **k: next(values)):
            warm = get_state(self.sess, StateKind.BAG, fresh=False)
            fresh = require_fresh(self.sess, StateKind.BAG)
            after = get_state(self.sess, StateKind.BAG, fresh=False)
        self.assertEqual(warm, ["warm"])
        self.assertEqual(fresh, ["fresh"])
        self.assertEqual(after, ["fresh"])

    def test_invalidate(self):
        with patch("app.core.package_api.list_packages_items", side_effect=[[1],[2]]):
            get_state(self.sess, StateKind.BAG)
            invalidate_states(self.sess, StateKind.BAG)
            v = get_state(self.sess, StateKind.BAG)
            self.assertEqual(v, [2])

if __name__ == "__main__":
    unittest.main()
