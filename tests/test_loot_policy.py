from __future__ import annotations

import unittest
from unittest.mock import patch

from app.core.loot import (
    SuperLootConfig,
    SuperLootTarget,
    filter_reachable_hits,
    find_named_targets,
    mark_unreachable,
    pick_ground_target,
    select_loot_target,
)
from app.core.loot._impl import (
    cast_session_busy,
    host_cast_active,
    mark_weighted_path_failure,
    read_host_cast_state,
    resolve_hits_for_step,
    should_reuse_scan_cache,
    wait_open_cast_bar,
    ScanCache,
    evaluate_motion_gate,
    super_loot_step,
    purge_soft_runtime_skips,
    wait_until_in_range,
)
from app.core.plg_objects import CLASS_MATTER, PlgObject
from app.ui.pages import SuperLootPage


class LootPolicyTests(unittest.TestCase):
    def test_auto_chest_page_keeps_cast_wait(self) -> None:
        """The page must retain the real chest cast interaction. @author by ak"""
        class Value:
            def __init__(self, value: str):
                self.value = value

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        page = object.__new__(SuperLootPage)
        page.var_pick_range = Value("2.5")
        page.var_scan = Value("80")
        page.var_cast_wait = Value("6.5")
        page.var_item = Value("")
        cfg = page._cfg_from_ui()
        self.assertGreater(cfg.cast_start_grace_s, 0.0)
        self.assertEqual(cfg.cast_wait_s, 6.5)
        self.assertEqual(
            page._format_step_status(
                {"action": "open", "ok": False, "error": "no_cast"}
            ),
            "未见读条，重试",
        )
        self.assertEqual(
            page._format_step_status(
                {"action": "open", "ok": False, "error": "no_cast_backoff"}
            ),
            "连续未见读条，短暂跳过",
        )

    def test_default_chest_open_keeps_historical_cast_start_probe(self) -> None:
        """Cache chaining must not remove the required chest cast probe. @author by ak"""
        cfg = SuperLootConfig()
        self.assertEqual(cfg.cast_start_grace_s, 1.2)
        self.assertGreater(cfg.cast_wait_s, 0.0)

    @patch("app.core.loot._impl.read_host_cast_state")
    @patch("app.core.loot._impl.find_named_targets")
    def test_cast_wait_never_runs_remote_scan(self, scan_during_cast, read_state) -> None:
        """Chest casts stay RPM-only even when the field is classified sparse."""
        read_state.side_effect = [
            {"readable": True, "session_active": True},
            {"readable": True, "session_active": True},
            {"readable": True, "session_active": True},
            {"readable": True, "session_active": False},
            {"readable": True, "session_active": False},
        ]
        with patch("app.core.loot._impl.time.sleep", return_value=None):
            status = wait_open_cast_bar(
                SuperLootConfig(cast_wait_s=2.0),
                session=type("Session", (), {"pid": 9})(),
                name="box",
            )

        self.assertEqual(status, "done")
        scan_during_cast.assert_not_called()

    def test_fresh_pocket_cache_is_reused_before_rescan(self) -> None:
        """Restore the pre-7/18 continuous-loot cache path safely. @author by ak"""
        cfg = SuperLootConfig()
        cache = ScanCache(
            hits=[SuperLootTarget("next", 2, obj_id=2, dist=12.0, x=12.0, y=0.0, z=0.0)],
            host_pos=(0.0, 0.0, 0.0),
            ts=100.0,
        )
        reuse, hits, reason = should_reuse_scan_cache(
            cache, cfg, host_pos=(0.0, 0.0, 0.0), now=101.0
        )
        self.assertTrue(reuse)
        self.assertEqual(reason, "work_1")
        self.assertEqual([hit.obj_id for hit in hits], [2])

    @patch("app.core.loot._impl.find_named_targets", return_value=[])
    @patch("app.core.loot._impl.get_object_count", return_value=294)
    def test_public_chest_field_uses_bounded_dense_scan(
        self, _count, find_targets
    ) -> None:
        """A 294-matter chest field must not use the sparse 96-CRT path."""
        _hits, cache, _source, dense = resolve_hits_for_step(
            type("Session", (), {"pid": 9})(),
            SuperLootConfig(),
            host_pos=(0.0, 0.0, 0.0),
        )
        self.assertTrue(dense)
        self.assertTrue(cache.dense)
        self.assertTrue(find_targets.call_args.kwargs["dense"])

    @patch("app.core.loot._impl.find_named_targets")
    @patch("app.core.loot._impl.get_object_count", return_value=300)
    def test_dense_scan_advances_to_next_candidate_batch(
        self, count_objects, find_targets
    ) -> None:
        target = SuperLootTarget(
            "box", 25, obj_id=25, tid=1, dist=4.0, x=4.0, y=0.0, z=0.0
        )

        def _scan(*_args, **kwargs):
            excluded = set(kwargs.get("exclude_ptrs") or ())
            meta = kwargs["scan_meta_out"]
            if not excluded:
                meta.update(
                    candidate_count=30,
                    inspected_ptrs=list(range(1, 13)),
                    remaining=18,
                )
                return []
            self.assertEqual(excluded, set(range(1, 13)))
            meta.update(
                candidate_count=30,
                inspected_ptrs=list(range(13, 25)),
                remaining=6,
            )
            return [target]

        find_targets.side_effect = _scan
        hits, cache, source, dense = resolve_hits_for_step(
            type("Session", (), {"pid": 9})(),
            SuperLootConfig(),
            host_pos=(0.0, 0.0, 0.0),
            scene_id=1,
        )
        self.assertEqual(hits, [])
        self.assertTrue(dense)
        self.assertEqual(cache.inspected_ptrs, set(range(1, 13)))
        self.assertTrue(source.startswith("full_scan_batch:"))

        hits, cache, source, dense = resolve_hits_for_step(
            type("Session", (), {"pid": 9})(),
            SuperLootConfig(),
            host_pos=(0.0, 0.0, 0.0),
            scene_id=1,
            scan_cache=cache,
        )
        self.assertEqual([hit.obj_id for hit in hits], [25])
        self.assertEqual(cache.inspected_ptrs, set(range(1, 25)))
        self.assertTrue(source.startswith("full_scan_batch:"))
        count_objects.assert_called_once()

    @patch("app.core.loot._impl.find_named_targets", return_value=[])
    @patch("app.core.loot._impl.get_object_count", return_value=300)
    def test_scene_change_discards_old_hit_cache(self, _count, find_targets) -> None:
        old = ScanCache(
            hits=[SuperLootTarget("old", 1, obj_id=1, tid=1, dist=1.0)],
            host_pos=(0.0, 0.0, 0.0),
            ts=100.0,
            matter_count=300,
            dense=True,
            scene_id=1,
            inspected_ptrs={1},
        )
        hits, cache, source, dense = resolve_hits_for_step(
            type("Session", (), {"pid": 9})(),
            SuperLootConfig(),
            host_pos=(0.0, 0.0, 0.0),
            scene_id=2,
            scan_cache=old,
        )
        self.assertEqual(hits, [])
        self.assertTrue(dense)
        self.assertEqual(cache.scene_id, 2)
        self.assertEqual(source, "full_scan")
        find_targets.assert_called_once()

    @patch("app.core.loot._impl.list_class_objects", return_value=[])
    @patch("app.core.loot._impl.get_object_count", return_value=294)
    def test_manual_scan_uses_bounded_dense_scan(
        self, _count, list_objects
    ) -> None:
        """Manual Scan must share the automatic dense scan limits. @author by ak"""
        find_named_targets(
            type("Session", (), {"pid": 9})(),
            SuperLootConfig(),
            host_pos=(0.0, 0.0, 0.0),
        )
        kwargs = list_objects.call_args.kwargs
        self.assertEqual(kwargs["limit"], 16)
        self.assertEqual(kwargs["max_inspect"], 12)
        self.assertFalse(kwargs["read_name"])

    @patch("app.core.loot._impl.wait_open_cast_bar", return_value="no_cast")
    @patch("app.core.loot._impl.interact_target", return_value={"ok": True, "ret": 1})
    @patch("app.core.loot._impl.resolve_hits_for_step")
    @patch("app.core.loot._impl.read_scene_position")
    def test_missed_cast_sample_keeps_face_chest_for_retry(
        self, read_pos, resolve_hits, _interact, _wait_cast
    ) -> None:
        """A no-cast sample must not blacklist or discard a nearby chest. @author by ak"""
        target = SuperLootTarget(
            "box", 0x1000, obj_id=77, tid=1, dist=1.0, x=1.0, y=0.0, z=0.0
        )
        cache = ScanCache(
            hits=[target], host_pos=(0.0, 0.0, 0.0), ts=0.0
        )
        read_pos.return_value = type(
            "Pos", (), {"ok": True, "scene_pos": (0.0, 0.0, 0.0), "scene_id": 1}
        )()
        resolve_hits.return_value = ([target], cache, "cache:local_1", False)
        skipped: dict[int, float] = {}

        out = super_loot_step(
            type("Session", (), {"pid": 9})(),
            SuperLootConfig(),
            scan_cache=cache,
            skip_ids=skipped,
        )

        self.assertFalse(out.ok)
        self.assertEqual(out.error, "no_cast")
        self.assertEqual(out.scan_cache, cache)
        self.assertEqual(skipped, {})

    @patch("app.core.loot._impl.wait_open_cast_bar", return_value="no_cast")
    @patch("app.core.loot._impl.interact_target", return_value={"ok": True, "ret": 1})
    @patch("app.core.loot._impl.resolve_hits_for_step")
    @patch("app.core.loot._impl.read_scene_position")
    def test_no_cast_third_failure_enters_short_per_target_cooldown(
        self, read_pos, resolve_hits, _interact, _wait_cast
    ) -> None:
        target = SuperLootTarget(
            "box", 0x1000, obj_id=77, tid=1, dist=1.0, x=1.0, y=0.0, z=0.0
        )
        cache = ScanCache(
            hits=[target],
            host_pos=(0.0, 0.0, 0.0),
            ts=0.0,
            dense=True,
            inspected_ptrs={0x1000},
            candidate_count=24,
            batch_complete=False,
        )
        read_pos.return_value = type(
            "Pos", (), {"ok": True, "scene_pos": (0.0, 0.0, 0.0), "scene_id": 1}
        )()
        resolve_hits.return_value = ([target], cache, "cache:local_1", True)
        attempts: dict[tuple[int, int], int] = {}
        cooldowns: dict[tuple[int, int], float] = {}

        results = [
            super_loot_step(
                type("Session", (), {"pid": 9})(),
                SuperLootConfig(no_cast_retry_limit=3, skip_no_cast_s=4.0),
                scan_cache=cache,
                no_cast_attempts=attempts,
                no_cast_cooldowns=cooldowns,
            )
            for _ in range(3)
        ]

        self.assertEqual([r.error for r in results], ["no_cast", "no_cast", "no_cast_backoff"])
        self.assertEqual(attempts, {})
        self.assertEqual(len(cooldowns), 1)
        self.assertEqual(results[2].scan_cache.hits, [])
        self.assertFalse(results[2].scan_cache.batch_complete)

    @patch("app.core.loot._impl.pick_item")
    @patch("app.core.loot._impl.set_target")
    @patch("app.core.loot._impl._try_bridge", return_value=None)
    def test_pickup_never_falls_back_to_raw_action(
        self, _bridge, set_target, pick_item
    ) -> None:
        session = type("Session", (), {"pid": 77})()
        target = SuperLootTarget("drop", 1, obj_id=2, tid=3)
        out = pick_ground_target(session, target, use_bridge=True)
        self.assertFalse(out["ok"])
        self.assertTrue(out["bridge_dead"])
        set_target.assert_not_called()
        pick_item.assert_not_called()

    @patch("app.core.loot._impl.get_object_id64", return_value=0x200000001)
    @patch("app.core.loot._impl.list_class_objects")
    def test_name_based_open_scan_reads_required_tid(self, list_objects, _obj_id) -> None:
        list_objects.return_value = [
            PlgObject(
                class_id=CLASS_MATTER,
                ptr=0x1234,
                name="九层妖楼暗道",
                tid=778899,
                dist=1.2,
                x=1.0,
                y=2.0,
                z=3.0,
            )
        ]
        cfg = SuperLootConfig(
            item_name="九层妖楼暗道",
            tid=None,
            match_tid_fallback=False,
            interact_mode="open",
        )
        hits = find_named_targets(object(), cfg, host_pos=(0, 0, 0))
        self.assertTrue(list_objects.call_args.kwargs["read_tid"])
        self.assertEqual(hits[0].tid, 778899)

    @patch("app.core.loot._impl.get_object_id64", return_value=0x200000001)
    @patch("app.core.loot._impl.list_class_objects")
    def test_entry_name_fallback_keeps_configured_interact_tid(
        self, list_objects, _obj_id
    ) -> None:
        list_objects.return_value = [
            PlgObject(
                class_id=CLASS_MATTER,
                ptr=0x1234,
                name="九层妖楼暗道",
                tid=None,
                dist=1.2,
                x=1.0,
                y=2.0,
                z=3.0,
            )
        ]
        cfg = SuperLootConfig(
            item_name="九层妖楼暗道",
            tid=81184,
            match_tid_fallback=True,
            interact_mode="open",
        )
        hits = find_named_targets(object(), cfg, host_pos=(0, 0, 0))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].tid, 81184)

    def test_near_path_policy_and_blacklist(self) -> None:
        cfg = SuperLootConfig()
        hits = [
            SuperLootTarget("d", 4, obj_id=44, dist=20, x=18, y=0, z=10),
            SuperLootTarget("e", 7, obj_id=66, dist=21, x=20, y=0, z=11),
            SuperLootTarget("f", 8, obj_id=77, dist=22, x=19, y=0, z=12),
        ]
        target, reason = select_loot_target(hits, cfg, host_pos=(0, 0, 0))
        self.assertIsNotNone(target)
        self.assertEqual(reason, "near_path")
        # Face isolated@2.5m always beats far multi-chest (stand-open priority).
        face_iso = [
            SuperLootTarget("face", 5, obj_id=5, dist=2.5, x=2.5, y=0, z=0),
            SuperLootTarget("c1", 2, obj_id=2, dist=25.0, x=25, y=0, z=0),
            SuperLootTarget("c2", 3, obj_id=3, dist=25.4, x=25.5, y=0, z=0.4),
            SuperLootTarget("c3", 4, obj_id=4, dist=25.8, x=26, y=0, z=0.2),
        ]
        tf, rf = select_loot_target(face_iso, cfg, host_pos=(0, 0, 0))
        self.assertEqual(tf.obj_id, 5)
        self.assertEqual(rf, "local_nearest")

        # Nearer non-isolated@6m beats densest seed@12m (was near_dense path bug).
        near_vs_dense = [
            SuperLootTarget("near", 1, obj_id=1, dist=6.0, x=6, y=0, z=0),
            SuperLootTarget("n2", 11, obj_id=11, dist=6.5, x=6.4, y=0, z=0.3),
            SuperLootTarget("c1", 2, obj_id=2, dist=12.0, x=12, y=0, z=0),
            SuperLootTarget("c2", 3, obj_id=3, dist=12.2, x=12.5, y=0, z=0.5),
            SuperLootTarget("c3", 4, obj_id=4, dist=12.4, x=13, y=0, z=0.2),
        ]
        t2, r2 = select_loot_target(near_vs_dense, cfg, host_pos=(0, 0, 0))
        self.assertEqual(t2.obj_id, 1)
        self.assertEqual(r2, "near_path")
        # Isolated nearer@8m skipped when multi-chest cluster exists farther.
        # Dense pocket must sit outside local iso radius (~8m of seed), else
        # seed counts the pocket as neighbors.
        iso_vs_dense = [
            SuperLootTarget("iso", 9, obj_id=9, dist=8.0, x=8, y=0, z=0),
            SuperLootTarget("c1", 2, obj_id=2, dist=25.0, x=25, y=0, z=0),
            SuperLootTarget("c2", 3, obj_id=3, dist=25.4, x=25.5, y=0, z=0.4),
            SuperLootTarget("c3", 4, obj_id=4, dist=25.8, x=26, y=0, z=0.2),
        ]
        t3, r3 = select_loot_target(iso_vs_dense, cfg, host_pos=(0, 0, 0))
        self.assertIsNotNone(t3)
        self.assertNotEqual(t3.obj_id, 9)
        self.assertTrue(str(r3).startswith("dense_adj"), r3)

        skip: dict[int, float] = {}
        points: list[tuple[float, float, float]] = []
        mark_unreachable(
            skip,
            hits[0],
            ttl_s=60,
            skip_points=points,
            record_near_point=True,
        )
        # Default near radius is 0: only the stuck id/XZ is banned, not neighbors.
        kept = filter_reachable_hits(hits, skip, skip_points=points, near_m=0)
        self.assertNotIn(44, {item.obj_id for item in kept})
        self.assertIn(66, {item.obj_id for item in kept})
        self.assertIn(77, {item.obj_id for item in kept})

        # A re-scanned matter can receive a new object id at the same XZ.
        # The point blacklist must still reject it after a path timeout.
        replacement = SuperLootTarget(
            "d-recreated", 9, obj_id=144, dist=20, x=18, y=0, z=10
        )
        kept_replacement = filter_reachable_hits(
            [replacement], {}, skip_points=points, near_m=0
        )
        self.assertEqual(kept_replacement, [])

        # Recovering a fully filtered scan may purge short object-id skips,
        # but must not erase the XZ blacklist for a path-stuck chest.
        purge_soft_runtime_skips(skip, points)
        self.assertEqual(
            filter_reachable_hits(
                [replacement], {}, skip_points=points, near_m=1.5
            ),
            [],
        )

        # Explicit near wipe (opt-in) still drops neighbors within radius.
        skip2: dict[int, float] = {}
        points2: list[tuple[float, float, float]] = []
        mark_unreachable(
            skip2,
            hits[0],
            ttl_s=60,
            skip_points=points2,
            record_near_point=True,
        )
        kept2 = filter_reachable_hits(hits, skip2, skip_points=points2, near_m=3)
        self.assertNotIn(44, {item.obj_id for item in kept2})

    def test_path_failure_blacklist_uses_session_weighted_backoff(self) -> None:
        target = SuperLootTarget("blocked", 1, obj_id=7, x=12.0, y=0.0, z=8.0)
        replacement = SuperLootTarget(
            "blocked-recreated", 2, obj_id=8, x=12.0, y=0.0, z=8.0
        )
        skip: dict[int, float] = {}
        points: list[tuple[float, float, float]] = []
        weights: dict[int, int] = {}

        observed: list[tuple[int, float]] = []
        for current in (target, replacement, replacement, replacement, replacement):
            observed.append(
                mark_weighted_path_failure(
                    skip, points, weights, current, reason="path-stuck last_d=4.6"
                )
            )
        self.assertEqual(
            observed,
            [(1, 30.0), (2, 90.0), (3, 300.0), (4, 900.0), (5, 1800.0)],
        )
        # A re-created object at the same XZ accumulates against the same key.
        self.assertEqual(len(weights), 1)
        self.assertEqual(weights[next(iter(weights))], 5)
        for _ in range(10):
            _, capped_ttl = mark_weighted_path_failure(
                skip, points, weights, replacement, reason="path-stuck"
            )
        self.assertEqual(capped_ttl, 1800.0)

    def test_cast_active_requires_skill_and_progress(self) -> None:
        session = type("Session", (), {"pid": 1})()
        with patch(
            "app.core.skill_cast_probe.resolve_cast_this",
            return_value=type("P", (), {"cast_this": 0x1000, "host_ptr": 0})(),
        ), patch(
            "app.core.loot._impl._u32_rpm",
            side_effect=lambda _s, addr: {
                0x1000 + 0x10: 123,
                0x1000 + 0x80: 0,
                0x1000 + 0x7C: 0,
                0x1000 + 0x20: 0,
                0x1000 + 0x4A0: 0,
            }.get(int(addr), 0),
        ):
            st = read_host_cast_state(session)
            self.assertFalse(st["active"])
            self.assertFalse(host_cast_active(session))

        with patch(
            "app.core.skill_cast_probe.resolve_cast_this",
            return_value=type("P", (), {"cast_this": 0x1000, "host_ptr": 0})(),
        ), patch(
            "app.core.loot._impl._u32_rpm",
            side_effect=lambda _s, addr: {
                0x1000 + 0x10: 123,
                0x1000 + 0x80: 0,
                0x1000 + 0x7C: 1,
                0x1000 + 0x20: 10,
                0x1000 + 0x4A0: 0,
            }.get(int(addr), 0),
        ):
            st = read_host_cast_state(session)
            self.assertTrue(st["active"])
            self.assertTrue(host_cast_active(session))

        # Matter open: host+0x41C session gate busy while skill flags stay 0.
        with patch(
            "app.core.skill_cast_probe.resolve_cast_this",
            return_value=type(
                "P", (), {"cast_this": 0x1000, "host_ptr": 0x2000}
            )(),
        ), patch(
            "app.core.loot._impl._u32_rpm",
            side_effect=lambda _s, addr: {
                0x2000 + 0x41C: 0xB,
                0x1000 + 0x10: 0,
                0x1000 + 0x80: 0,
                0x1000 + 0x7C: 0,
                0x1000 + 0x20: 0,
                0x1000 + 0x4A0: 0,
            }.get(int(addr), 0),
        ):
            st = read_host_cast_state(session)
            self.assertFalse(st["active"])
            self.assertTrue(st["session_active"])
            self.assertTrue(host_cast_active(session))
            self.assertTrue(cast_session_busy(st))

        # cast+0x4A0 bit0 alone with skill id also counts as active.
        with patch(
            "app.core.skill_cast_probe.resolve_cast_this",
            return_value=type("P", (), {"cast_this": 0x1000, "host_ptr": 0})(),
        ), patch(
            "app.core.loot._impl._u32_rpm",
            side_effect=lambda _s, addr: {
                0x1000 + 0x10: 101044,
                0x1000 + 0x80: 0,
                0x1000 + 0x7C: 0,
                0x1000 + 0x20: 0,
                0x1000 + 0x4A0: 1,
            }.get(int(addr), 0),
        ):
            st = read_host_cast_state(session)
            self.assertTrue(st["active"])
            self.assertTrue(host_cast_active(session))

    def test_wait_open_cast_bar_needs_start_and_idle(self) -> None:
        session = type("Session", (), {"pid": 9})()
        cfg = SuperLootConfig(cast_start_grace_s=1.0, cast_wait_s=2.0)
        clock = {"t": 0.0}
        seq = [
            {"active": False},
            {"active": True},
            {"active": True},
            {"active": True},
            {"active": False},
            {"active": False},
        ]

        def _now() -> float:
            return float(clock["t"])

        def _sleep(dt: float) -> None:
            clock["t"] = float(clock["t"]) + float(dt)

        def _state(_session, log=None):
            if seq:
                return seq.pop(0)
            return {"active": False}

        with patch("app.core.loot._impl.read_host_cast_state", side_effect=_state), patch(
            "app.core.loot._impl.time.sleep", side_effect=_sleep
        ), patch("app.core.loot._impl.time.time", side_effect=_now):
            st = wait_open_cast_bar(cfg, session=session, name="box")
        self.assertEqual(st, "done")

        clock["t"] = 0.0
        with patch(
            "app.core.loot._impl.read_host_cast_state", return_value={"active": False}
        ), patch("app.core.loot._impl.time.sleep", side_effect=_sleep), patch(
            "app.core.loot._impl.time.time", side_effect=_now
        ):
            st = wait_open_cast_bar(cfg, session=session, name="box")
        self.assertEqual(st, "no_cast")

        # Without session memory, never invent success.
        st = wait_open_cast_bar(cfg, session=None, name="box")
        self.assertEqual(st, "no_cast")

        # Zero-wait path used by dialog-based openers (yaolu).
        st = wait_open_cast_bar(
            SuperLootConfig(cast_start_grace_s=0.0, cast_wait_s=0.0),
            session=session,
            name="entry",
        )
        self.assertEqual(st, "skip")

        clock["t"] = 0.0
        start_seq = [{"active": False}, {"active": True}, {"active": True}]
        with patch(
            "app.core.loot._impl.read_host_cast_state",
            side_effect=lambda *_a, **_k: start_seq.pop(0),
        ), patch("app.core.loot._impl.time.sleep", side_effect=_sleep), patch(
            "app.core.loot._impl.time.time", side_effect=_now
        ):
            st = wait_open_cast_bar(
                SuperLootConfig(
                    cast_start_grace_s=1.0,
                    cast_wait_s=0.0,
                    confirm_cast_start_only=True,
                ),
                session=session,
                name="entry",
            )
        self.assertEqual(st, "started")

        # session_active alone (no skill active) still proves open cast.
        clock["t"] = 0.0
        sess_seq = [
            {"active": False, "session_active": False, "session_state": 0},
            {"active": False, "session_active": True, "session_state": 0xB},
            {"active": False, "session_active": True, "session_state": 0xB},
            {"active": False, "session_active": True, "session_state": 0xB},
            {"active": False, "session_active": False, "session_state": 0},
            {"active": False, "session_active": False, "session_state": 0},
        ]
        with patch(
            "app.core.loot._impl.read_host_cast_state",
            side_effect=lambda *_a, **_k: sess_seq.pop(0),
        ), patch("app.core.loot._impl.time.sleep", side_effect=_sleep), patch(
            "app.core.loot._impl.time.time", side_effect=_now
        ):
            st = wait_open_cast_bar(cfg, session=session, name="box")
        self.assertEqual(st, "done")

    @patch("app.core.loot._impl.read_scene_position")
    def test_path_arrival_requires_player_to_stop(self, read_pos) -> None:
        target = SuperLootTarget("entry", 1, x=20.0, y=0.0, z=0.0)
        read_pos.side_effect = [
            type("P", (), {"ok": True, "scene_pos": (19.0, 0.0, 0.0), "scene_id": 68})(),
            type("P", (), {"ok": True, "scene_pos": (19.3, 0.0, 0.0), "scene_id": 68})(),
            type("P", (), {"ok": True, "scene_pos": (19.3, 0.0, 0.0), "scene_id": 68})(),
            type("P", (), {"ok": True, "scene_pos": (19.3, 0.0, 0.0), "scene_id": 68})(),
        ]
        clock = {"t": 0.0}

        def _now() -> float:
            return clock["t"]

        def _sleep(delta: float) -> None:
            clock["t"] += float(delta)

        with patch("app.core.loot._impl.time.time", side_effect=_now), patch(
            "app.core.loot._impl.time.sleep", side_effect=_sleep
        ):
            arrived, dist, pos = wait_until_in_range(
                object(), target, pick_range=2.0, timeout_s=3.0
            )
        self.assertTrue(arrived)
        self.assertEqual(pos, (19.3, 0.0, 0.0))
        self.assertAlmostEqual(dist, 0.7)




    def test_last_resort_isolated_when_only_far_left(self) -> None:
        """Only far isolates remain => still select nearest, do not idle."""
        cfg = SuperLootConfig(max_isolated_path=28.0)
        # Far apart so each is truly isolated (not a 2-chest dense pocket).
        hits = [
            SuperLootTarget("far1", 1, obj_id=1, dist=36.0, x=36, y=0, z=0),
            SuperLootTarget("far2", 2, obj_id=2, dist=55.0, x=0, y=0, z=55),
        ]
        t, r = select_loot_target(hits, cfg, host_pos=(0, 0, 0))
        self.assertIsNotNone(t)
        self.assertEqual(t.obj_id, 1)
        self.assertEqual(r, "last_resort_isolated")



class MotionGateTests(unittest.TestCase):
    def test_evaluate_motion_gate_jump_speed_air(self) -> None:
        prev = (0.0, 50.0, 0.0)
        # Large XZ jump
        hit, reason = evaluate_motion_gate(
            prev, 0.0, (30.0, 50.0, 0.0), 1.0,
            speed_mps=18.0, jump_m=26.0, y_jump_m=8.0,
        )
        self.assertTrue(hit)
        self.assertIn("jump_h", reason)

        # Vertical air spike
        hit, reason = evaluate_motion_gate(
            prev, 0.0, (1.0, 60.0, 1.0), 1.0,
            speed_mps=18.0, jump_m=26.0, y_jump_m=8.0,
        )
        self.assertTrue(hit)
        self.assertIn("jump_y", reason)

        # High speed short dt (manual fly), small total jump under jump_m
        hit, reason = evaluate_motion_gate(
            prev, 0.0, (10.0, 50.0, 0.0), 0.4,
            speed_mps=18.0, jump_m=26.0, y_jump_m=8.0,
        )
        self.assertTrue(hit)
        self.assertIn("speed", reason)

        # Normal script path ~11m over 2s should NOT pause
        hit, reason = evaluate_motion_gate(
            prev, 0.0, (11.0, 50.0, 0.0), 2.0,
            speed_mps=18.0, jump_m=26.0, y_jump_m=8.0,
        )
        self.assertFalse(hit)
        self.assertEqual(reason, "")

        # First sample no prev
        hit, reason = evaluate_motion_gate(
            None, 0.0, (0.0, 50.0, 0.0), 1.0,
            speed_mps=18.0, jump_m=26.0, y_jump_m=8.0,
        )
        self.assertFalse(hit)


if __name__ == "__main__":
    unittest.main()
