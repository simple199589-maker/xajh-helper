# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from app.core.task_sync import (
    ACTION_ACCEPT,
    ACTION_ACCEPT_DAILY_TASKS,
    ACTION_CLAIM_ACTIVITY,
    ACTION_COMPLETE,
    ACTION_HANG_SYNC,
    ACTION_PATH,
    ROLE_MASTER,
    ROLE_NONE,
    ROLE_SLAVE,
    TaskSyncHub,
)


class TaskSyncHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = TaskSyncHub()

    def test_default_role_is_none(self) -> None:
        self.assertEqual(self.hub.get_role(1), ROLE_NONE)

    def test_master_is_exclusive(self) -> None:
        self.hub.set_role(10, ROLE_MASTER)
        self.hub.set_role(20, ROLE_MASTER)
        self.assertEqual(self.hub.get_role(10), ROLE_NONE)
        self.assertEqual(self.hub.get_role(20), ROLE_MASTER)

    def test_master_publishes_to_slaves_only(self) -> None:
        got: list[tuple[int, str, int]] = []

        def make_listener(pid: int):
            def _cb(event) -> None:
                got.append((pid, event.action, event.task_id))

            return _cb

        self.hub.set_role(1, ROLE_MASTER)
        self.hub.set_role(2, ROLE_SLAVE)
        self.hub.set_role(3, ROLE_SLAVE)
        self.hub.set_role(4, ROLE_NONE)
        self.hub.subscribe(2, make_listener(2))
        self.hub.subscribe(3, make_listener(3))
        self.hub.subscribe(4, make_listener(4))
        n = self.hub.publish(
            action=ACTION_ACCEPT, task_id=10021, source_pid=1, name="test"
        )
        self.assertEqual(n, 2)
        self.assertEqual(sorted(got), [(2, ACTION_ACCEPT, 10021), (3, ACTION_ACCEPT, 10021)])

    def test_accept_daily_tasks_publishes_without_task_id(self) -> None:
        got = []
        self.hub.set_role(1, ROLE_MASTER)
        self.hub.set_role(2, ROLE_SLAVE)
        self.hub.subscribe(2, lambda event: got.append(event))
        self.assertEqual(self.hub.publish(action=ACTION_ACCEPT_DAILY_TASKS, task_id=0, source_pid=1), 1)
        self.assertEqual(got[0].action, ACTION_ACCEPT_DAILY_TASKS)
        self.assertEqual(got[0].task_id, 0)

    def test_non_master_cannot_publish(self) -> None:
        hit = []
        self.hub.set_role(1, ROLE_SLAVE)
        self.hub.set_role(2, ROLE_SLAVE)
        self.hub.subscribe(2, lambda e: hit.append(e))
        n = self.hub.publish(action=ACTION_COMPLETE, task_id=7, source_pid=1)
        self.assertEqual(n, 0)
        self.assertEqual(hit, [])

    def test_dedupe_same_event(self) -> None:
        hit = []
        self.hub.set_role(1, ROLE_MASTER)
        self.hub.set_role(2, ROLE_SLAVE)
        self.hub.subscribe(2, lambda e: hit.append(e.task_id))
        self.assertEqual(
            self.hub.publish(action=ACTION_ACCEPT, task_id=9, source_pid=1), 1
        )
        self.assertEqual(
            self.hub.publish(action=ACTION_ACCEPT, task_id=9, source_pid=1), 0
        )
        self.assertEqual(hit, [9])

    def test_path_action_publishes_with_portal_kind(self) -> None:
        got = []
        self.hub.set_role(1, ROLE_MASTER)
        self.hub.set_role(2, ROLE_SLAVE)
        self.hub.subscribe(2, lambda e: got.append(e))
        n = self.hub.publish(
            action=ACTION_PATH,
            task_id=10010,
            source_pid=1,
            name="每日杀怪",
            portal_kind="dungeon_upper",
        )
        self.assertEqual(n, 1)
        self.assertEqual(got[0].action, ACTION_PATH)
        self.assertEqual(got[0].portal_kind, "dungeon_upper")

    def test_path_action_carries_verified_route_snapshot(self) -> None:
        got = []
        self.hub.set_role(1, ROLE_MASTER)
        self.hub.set_role(2, ROLE_SLAVE)
        self.hub.subscribe(2, lambda e: got.append(e))
        n = self.hub.publish(
            action=ACTION_PATH,
            task_id=10010,
            source_pid=1,
            name="<每日BOSS>龙傲天",
            portal_kind="npc",
            origin_scene_id=68,
            portal_tid=100219,
            portal_x=-22.29,
            portal_y=60.75,
            portal_z=-85.58,
        )
        self.assertEqual(n, 1)
        self.assertEqual(got[0].portal_kind, "npc")
        self.assertEqual(got[0].origin_scene_id, 68)
        self.assertEqual(got[0].portal_tid, 100219)
        self.assertAlmostEqual(got[0].portal_x, -22.29)

    def test_hang_action_publishes_temporary_mode(self) -> None:
        got = []
        self.hub.set_role(1, ROLE_MASTER)
        self.hub.set_role(2, ROLE_SLAVE)
        self.hub.subscribe(2, lambda e: got.append(e))
        n = self.hub.publish(
            action=ACTION_HANG_SYNC,
            task_id=1,
            source_pid=1,
            name="on",
            hang_mode=1,
        )
        self.assertEqual(n, 1)
        self.assertEqual(got[0].hang_mode, 1)




    def test_claim_activity_publishes_without_task_id(self) -> None:
        got = []
        self.hub.set_role(1, ROLE_MASTER)
        self.hub.set_role(2, ROLE_SLAVE)
        self.hub.set_role(3, ROLE_SLAVE)
        self.hub.set_role(4, ROLE_NONE)
        self.hub.subscribe(2, lambda e: got.append(e))
        self.hub.subscribe(3, lambda e: got.append(e))
        self.hub.subscribe(4, lambda e: got.append(e))
        n = self.hub.publish(
            action=ACTION_CLAIM_ACTIVITY,
            task_id=0,
            source_pid=1,
            name="第1轮回城",
            points=35,
        )
        self.assertEqual(n, 2)
        self.assertEqual(len(got), 2)
        self.assertTrue(all(e.action == ACTION_CLAIM_ACTIVITY for e in got))
        self.assertTrue(all(e.task_id == 0 for e in got))
        self.assertTrue(all(e.points == 35 for e in got))
        # Dedupe within window
        self.assertEqual(
            self.hub.publish(
                action=ACTION_CLAIM_ACTIVITY,
                task_id=0,
                source_pid=1,
                points=35,
            ),
            0,
        )

    def test_non_master_cannot_publish_claim_activity(self) -> None:
        hit = []
        self.hub.set_role(1, ROLE_SLAVE)
        self.hub.set_role(2, ROLE_SLAVE)
        self.hub.subscribe(2, lambda e: hit.append(e))
        n = self.hub.publish(
            action=ACTION_CLAIM_ACTIVITY,
            task_id=0,
            source_pid=1,
            points=20,
        )
        self.assertEqual(n, 0)
        self.assertEqual(hit, [])


if __name__ == "__main__":
    unittest.main()
