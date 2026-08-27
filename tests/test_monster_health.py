from __future__ import annotations

import unittest

from app.core.monster_health import HealthThreshold, MonsterHealthSample, parse_percent


def sample(target: int, hp: float | None, *, ok: bool = True) -> MonsterHealthSample:
    return MonsterHealthSample(ok=ok, selected_id64=target, hp_pct=hp)


class MonsterHealthTests(unittest.TestCase):
    def test_parse_percent(self):
        self.assertEqual(parse_percent("100%"), 100.0)
        self.assertEqual(parse_percent(" 37.5 % "), 37.5)
        self.assertIsNone(parse_percent("弯刀驼"))

    def test_threshold_only_fires_on_downward_crossing(self):
        edge = HealthThreshold(35)
        self.assertFalse(edge.update(sample(10, 100)))
        self.assertFalse(edge.update(sample(10, 36)))
        self.assertTrue(edge.update(sample(10, 35)))
        self.assertFalse(edge.update(sample(10, 20)))
        self.assertFalse(edge.update(sample(10, 40)))
        self.assertFalse(edge.update(sample(10, 34)))

    def test_target_change_resets_threshold(self):
        edge = HealthThreshold(50)
        edge.update(sample(10, 80))
        self.assertTrue(edge.update(sample(10, 40)))
        self.assertFalse(edge.update(sample(11, 40)))
        self.assertFalse(edge.update(sample(11, 70)))
        self.assertTrue(edge.update(sample(11, 50)))

    def test_invalid_sample_does_not_destroy_previous_value(self):
        edge = HealthThreshold(25)
        edge.update(sample(10, 40))
        self.assertFalse(edge.update(sample(10, None, ok=False)))
        self.assertTrue(edge.update(sample(10, 20)))


if __name__ == "__main__":
    unittest.main()
