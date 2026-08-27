import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.core.yaolu_auto import YaoluConfig, YaoluRunner, apply_yaolu_risk_constraint
from app.core.yaolu_prefs import (
    load_yaolu_risk_constraint,
    save_yaolu_risk_constraint,
)
from app.ui.pages._impl import YaoluPage


class YaoluRiskConstraintTests(unittest.TestCase):
    def test_default_eighty_percent_keeps_current_waits(self) -> None:
        original = YaoluConfig()
        cfg = apply_yaolu_risk_constraint(YaoluConfig(), 80)
        self.assertEqual(original.min_confidence, 0.25)
        self.assertEqual(cfg.min_confidence, 0.25)
        keys = (
            "enter_timeout_s",
            "enter_fail_grace_s",
            "fail_sleep_min_s",
            "fail_sleep_max_s",
            "entry_cd_s",
            "entry_cd_jitter_s",
            "success_rest_min_s",
            "success_rest_max_s",
            "answer_fail_rest_min_s",
            "answer_fail_rest_max_s",
            "consecutive_answer_fail_pause_s",
            "entry_reopen_base_s",
            "entry_reopen_growth",
            "captcha_api_fail_cooldown_s",
            "post_map_settle_s",
            "cancel_session_wait_s",
        )
        for key in keys:
            self.assertEqual(getattr(cfg, key), getattr(original, key), key)
        self.assertEqual(cfg.risk_constraint_pct, 80)
        self.assertEqual(
            (cfg.entry_blackout_start_min, cfg.entry_blackout_end_min),
            (55, 5),
        )

    def test_lower_percent_is_faster_but_keeps_hard_gates(self) -> None:
        fast = apply_yaolu_risk_constraint(YaoluConfig(), 1)
        current = apply_yaolu_risk_constraint(YaoluConfig(), 80)
        self.assertLess(fast.enter_timeout_s, current.enter_timeout_s)
        self.assertLess(fast.enter_fail_grace_s, current.enter_fail_grace_s)
        self.assertLess(fast.entry_reopen_base_s, current.entry_reopen_base_s)
        self.assertLess(fast.answer_fail_rest_max_s, current.answer_fail_rest_max_s)
        self.assertEqual(
            (fast.success_rest_min_s, fast.success_rest_max_s),
            (25.0, 90.0),
        )
        self.assertEqual(
            (
                apply_yaolu_risk_constraint(YaoluConfig(), 50).success_rest_min_s,
                apply_yaolu_risk_constraint(YaoluConfig(), 50).success_rest_max_s,
            ),
            (25.0, 90.0),
        )
        self.assertEqual(fast.post_map_settle_s, 20.0)
        self.assertEqual(
            apply_yaolu_risk_constraint(YaoluConfig(), 50).post_map_settle_s,
            20.0,
        )
        self.assertEqual(fast.post_map_settle_s, current.post_map_settle_s)
        self.assertEqual(
            (
                fast.entry_blackout_start_min,
                fast.entry_blackout_end_min,
                fast.entry_blackout_end_sec,
            ),
            (58, 0, 15),
        )
        self.assertTrue(fast.entry_blackout_enabled)
        self.assertTrue(fast.captcha_reject_low_confidence)
        self.assertEqual(fast.min_confidence, 0.25)
        self.assertEqual(fast.min_confidence, current.min_confidence)
        self.assertTrue(fast.shenfa_lock_day)
        self.assertEqual(fast.daily_success_limit, current.daily_success_limit)

    def test_higher_percent_adds_wait_margin(self) -> None:
        current = apply_yaolu_risk_constraint(YaoluConfig(), 80)
        cautious = apply_yaolu_risk_constraint(YaoluConfig(), 100)
        self.assertGreater(cautious.enter_timeout_s, current.enter_timeout_s)
        self.assertGreater(cautious.success_rest_min_s, current.success_rest_min_s)
        self.assertEqual(cautious.success_rest_max_s, 90.0)
        self.assertGreater(
            cautious.consecutive_answer_fail_pause_s,
            current.consecutive_answer_fail_pause_s,
        )
        self.assertEqual(
            (cautious.entry_blackout_start_min, cautious.entry_blackout_end_min),
            (54, 6),
        )

    @patch("app.core.yaolu_auto.read_scene_state")
    @patch("app.core.yaolu_auto.wait_return_fuzhou", return_value=True)
    def test_successful_return_arms_random_cooldown(
        self, _wait_return, read_scene
    ) -> None:
        read_scene.return_value = (68, (35.7, 55.3, -79.8), "福州城")
        cfg = YaoluConfig(success_rest_min_s=37.0, success_rest_max_s=37.0)
        runner = YaoluRunner(pid=123, cfg=cfg, on_event=lambda _event: None)
        with (
            patch.object(runner, "_bump_daily_ok", return_value=1),
            patch.object(runner, "_audit"),
            patch.object(runner, "_path_to_entry_until_arrived", return_value=True),
            patch.object(runner, "_maybe_stop_daily_limit", return_value=False),
            patch.object(runner, "_arm_open_rest") as arm_rest,
        ):
            runner._finish_enter_success(
                MagicMock(), scene_id=1529, label="九层妖楼", open_res=None
            )
        arm_rest.assert_called_once_with(
            37.0, "通关后降频休息", label="通关休息"
        )

    def test_selected_percentage_is_disk_backed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "yaolu_prefs.json"
            with patch("app.core.yaolu_prefs.yaolu_prefs_path", return_value=path):
                save_yaolu_risk_constraint("40%")
                self.assertEqual(load_yaolu_risk_constraint(), 40)
                self.assertIn('"risk_constraint_pct": 40', path.read_text("utf-8"))

    @patch("app.ui.pages._impl.cleanup_captcha_debug", return_value=4)
    @patch("app.ui.pages._impl.cleanup_game_screenshots")
    @patch("app.ui.pages._impl.game_screenshots_dir", return_value=Path("screens"))
    def test_merged_cleanup_runs_both_targets(
        self, _folder, cleanup_screens, cleanup_cache
    ) -> None:
        cleanup_screens.return_value = {
            "ok": True,
            "removed": 3,
            "bytes_freed": 1024 * 1024,
        }
        page = SimpleNamespace(var_status=MagicMock(), log=MagicMock())
        YaoluPage._on_cleanup_all(page)
        cleanup_screens.assert_called_once()
        cleanup_cache.assert_called_once_with(
            ttl_s=0.0,
            force_all=True,
            log=page.log,
        )
        page.var_status.set.assert_called_once()


if __name__ == "__main__":
    unittest.main()
