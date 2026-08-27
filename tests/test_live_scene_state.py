# -*- coding: utf-8 -*-
"""Live scene/death state machine unit tests. @author by ak"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from app.core.activity_auto import is_dungeon_scene, is_fuzhou_scene
from app.core.live_scene_hub import (
    DEFAULT_LIVE_MAX_AGE_S,
    drop_live_scene_hub,
    get_live_scene,
    peek_live_scene,
    publish_live_scene,
)
from app.core.safe_dispatch import reset_dispatch_for_tests
from app.core.state_dispatch import StateKind, get_state


class LiveSceneStateTests(unittest.TestCase):
    def setUp(self):
        self.pid = 19001
        drop_live_scene_hub(self.pid)
        reset_dispatch_for_tests()

    def tearDown(self):
        drop_live_scene_hub(self.pid)
        reset_dispatch_for_tests()

    def test_city_id_not_poisoned_by_stale_label(self):
        # Old bug: scene_id dungeon + leftover "福州城" label => still city.
        self.assertFalse(is_fuzhou_scene(1238, "福州城"))
        self.assertTrue(is_dungeon_scene(1238, "福州城", gate="any_city"))
        self.assertTrue(is_fuzhou_scene(68, "福州城"))
        self.assertFalse(is_dungeon_scene(68, "福州城", gate="any_city"))

    def test_scene_change_clears_stale_label(self):
        publish_live_scene(self.pid, scene_id=68, scene_label="福州城", source="t1")
        snap = publish_live_scene(self.pid, scene_id=1238, source="t2")
        self.assertEqual(snap.scene_id, 1238)
        self.assertEqual(snap.scene_label, "")

    def test_dead_only_publish_does_not_refresh_scene_age(self):
        publish_live_scene(
            self.pid, scene_id=68, scene_label="福州城", pos=(1.0, 2.0, 3.0), source="t1"
        )
        old = peek_live_scene(self.pid)
        time.sleep(0.05)
        publish_live_scene(self.pid, dead=True, touch_dead=True, source="dead")
        new = peek_live_scene(self.pid)
        self.assertTrue(new.dead is True)
        self.assertEqual(new.updated_at, old.updated_at)
        self.assertGreater(new.dead_updated_at, 0)

    def test_state_dispatch_rejects_stale_hub(self):
        publish_live_scene(
            self.pid, scene_id=68, scene_label="福州城", pos=(1.0, 0.0, 1.0), source="old"
        )
        # Force hub age older than trust window.
        hub_snap = peek_live_scene(self.pid)
        hub = __import__("app.core.live_scene_hub", fromlist=["get_live_scene_hub"]).get_live_scene_hub(self.pid)
        with hub._lock:
            hub._snap.updated_at = time.time() - (DEFAULT_LIVE_MAX_AGE_S + 5.0)

        sess = MagicMock()
        sess.pid = self.pid

        fake = MagicMock()
        fake.ok = True
        fake.scene_id = 1238
        fake.scene_pos = (9.0, 0.0, 8.0)

        with patch("app.core.automove.read_scene_position", return_value=fake):
            with patch("app.core.map_names.format_scene_display", return_value="绿竹林"):
                sc = get_state(sess, StateKind.SCENE, fresh=True)
        self.assertEqual(int(sc["scene_id"]), 1238)
        live = get_live_scene(self.pid, max_age_s=2.5)
        self.assertIsNotNone(live)
        self.assertEqual(int(live.scene_id), 1238)


if __name__ == "__main__":
    unittest.main()
