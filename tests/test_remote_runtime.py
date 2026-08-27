from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from app.core import remote_runtime
from app.core.remote_runtime import (
    CallConvention,
    ReturnKind,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    build_call_stub,
    ensure_pid_scene_stable,
    get_pid_scene_snapshot,
    is_pid_scene_snapshot_stable,
    note_pid_scene_snapshot,
    wait_remote_thread_safely,
)


class RemoteRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        remote_runtime.clear_pid_remote_state(9876)

    def test_cdecl_stub_pushes_reverse_and_cleans_stack(self) -> None:
        code = build_call_stub(0x401000, [1, 2], ret_addr=0x500000)
        self.assertTrue(code.startswith(bytes.fromhex("68020000006801000000")))
        self.assertIn(bytes.fromhex("83C408"), code)
        self.assertTrue(code.endswith(b"\xC3"))

    def test_thiscall_sets_ecx_without_caller_cleanup(self) -> None:
        code = build_call_stub(
            0x401000,
            [7],
            ret_addr=0x500000,
            convention=CallConvention.THISCALL,
            this_ptr=0x600000,
        )
        self.assertIn(bytes.fromhex("B900006000"), code)
        self.assertNotIn(bytes.fromhex("83C404"), code)

    def test_float_and_u64_return_stores(self) -> None:
        f = build_call_stub(
            1, [], ret_addr=0x500000, return_kind=ReturnKind.FLOAT
        )
        u = build_call_stub(1, [], ret_addr=0x500000, return_kind=ReturnKind.U64)
        self.assertIn(bytes.fromhex("D91D00005000"), f)
        self.assertIn(bytes.fromhex("891504005000"), u)

    def test_timeout_waits_for_completion_before_cleanup(self) -> None:
        with patch.object(
            remote_runtime.kernel32,
            "WaitForSingleObject",
            side_effect=[WAIT_TIMEOUT, WAIT_OBJECT_0],
        ) as wait, patch("app.core.diag_log.error") as log_error:
            wait_remote_thread_safely(123, 50, operation="unit")
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(wait.call_args_list[1].args[1], remote_runtime._REMOTE_WAIT_GRACE_MS)
        log_error.assert_called_once()

    def test_scene_gate_blocks_transition_and_new_generation(self) -> None:
        note_pid_scene_snapshot(9876, host_present=False, scene_id=0, sampled_at=10.0)
        with patch.object(remote_runtime, "_probe_pid_scene_snapshot", return_value=False):
            with self.assertRaises(remote_runtime.SceneTransitionError):
                ensure_pid_scene_stable(9876)

        note_pid_scene_snapshot(9876, host_present=True, scene_id=68)
        with patch.object(remote_runtime, "_probe_pid_scene_snapshot", return_value=True):
            with self.assertRaises(remote_runtime.SceneTransitionError):
                ensure_pid_scene_stable(9876, settle_sec=1.0)

    def test_scene_gate_allows_stable_generation(self) -> None:
        now = remote_runtime.time.monotonic()
        note_pid_scene_snapshot(
            9876, host_present=True, scene_id=68, sampled_at=now - 2.0
        )
        with patch.object(remote_runtime, "_probe_pid_scene_snapshot", return_value=True):
            ensure_pid_scene_stable(9876, settle_sec=1.0)

    def test_scene_gate_reuses_recent_snapshot_without_bridge_probe(self) -> None:
        now = remote_runtime.time.monotonic()
        note_pid_scene_snapshot(
            9876, host_present=True, scene_id=68, sampled_at=now - 0.1
        )
        with patch.object(remote_runtime, "_probe_pid_scene_snapshot") as probe:
            ensure_pid_scene_stable(9876, settle_sec=0.0)
            ensure_pid_scene_stable(9876, settle_sec=0.0)
        probe.assert_not_called()

    def test_scene_gate_refreshes_expired_snapshot(self) -> None:
        now = remote_runtime.time.monotonic()
        note_pid_scene_snapshot(
            9876, host_present=True, scene_id=68, sampled_at=now - 2.0
        )
        with patch.object(
            remote_runtime, "_probe_pid_scene_snapshot", return_value=True
        ) as probe:
            ensure_pid_scene_stable(9876, settle_sec=0.0)
        probe.assert_called_once_with(9876, timeout_ms=700)

    def test_scene_snapshot_stability_query_tracks_generation(self) -> None:
        now = remote_runtime.time.monotonic()
        note_pid_scene_snapshot(
            9876, host_present=True, scene_id=68, sampled_at=now - 2.0
        )
        self.assertTrue(is_pid_scene_snapshot_stable(9876, settle_sec=1.0))
        note_pid_scene_snapshot(9876, host_present=True, scene_id=69)
        self.assertFalse(is_pid_scene_snapshot_stable(9876, settle_sec=1.0))
        note_pid_scene_snapshot(9876, host_present=False, scene_id=0)
        self.assertFalse(is_pid_scene_snapshot_stable(9876, settle_sec=0.0))

    def test_scene_snapshot_peek_returns_recent_generation(self) -> None:
        note_pid_scene_snapshot(9876, host_present=True, scene_id=74)
        snap = get_pid_scene_snapshot(9876, max_age_s=1.0)
        self.assertTrue(snap["ready"])
        self.assertEqual(snap["scene_id"], 74)
        self.assertGreaterEqual(snap["age_s"], 0.0)

    def test_remote_mutations_recheck_scene_after_queue_lock(self) -> None:
        call_source = inspect.getsource(remote_runtime.remote_call_x86)
        write_source = inspect.getsource(remote_runtime.remote_write_bytes)
        self.assertGreaterEqual(call_source.count("ensure_pid_scene_stable(pid)"), 2)
        self.assertGreaterEqual(write_source.count("ensure_pid_scene_stable(pid)"), 2)


if __name__ == "__main__":
    unittest.main()
