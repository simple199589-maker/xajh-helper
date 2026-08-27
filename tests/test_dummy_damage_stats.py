from __future__ import annotations

import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from app.core.dummy_damage_stats import (
    extend_damage_history,
    find_damage_sequence_delta,
    format_damage_amount,
    measure_dummy_damage,
    parse_dummy_damage_reader_output,
    parse_dummy_damage_stat,
    scan_dummy_damage_sequences,
    select_training_dummy,
    update_damage_references,
)
from app.core.entity_scan import EntityHit


def _note(*, started: int, done: int, ms: int, hits: int, total: int) -> str:
    return (
        "DUMMY_DAMAGE t=02000000:89ABCDEF a=1 "
        f"s={started} d={done} ms={ms} n={hits} total={total} last=250"
    )


class DummyDamageStatsTests(unittest.TestCase):
    def test_format_damage_amount_uses_millions_then_billions(self) -> None:
        self.assertEqual(format_damage_amount(0), "0.00M")
        self.assertEqual(format_damage_amount(25_887_376), "25.89M")
        self.assertEqual(format_damage_amount(999_999_999), "1000.00M")
        self.assertEqual(format_damage_amount(1_000_000_000), "1.00B")
        self.assertEqual(format_damage_amount(9_269_446_656), "9.27B")

    def test_parse_native_reader_snapshot(self) -> None:
        parsed = parse_dummy_damage_reader_output(
            "RAW 7 RUNS 2\n"
            "SEQ 84F5B11E 3 10 20 30\n"
            "SEQ 84F5A1CE 2 40 50\n"
        )
        self.assertEqual(parsed, [[10, 20, 30], [40, 50]])

    def test_parse_native_reader_rejects_count_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "count mismatch"):
            parse_dummy_damage_reader_output("RAW 2 RUNS 1\nSEQ 1234ABCD 2 10\n")

    def test_scan_uses_external_reader_for_session_pid(self) -> None:
        class Session:
            pid = 23804
            pm = object()

        completed = CompletedProcess(
            args=[], returncode=0, stdout="RAW 1 RUNS 1\nSEQ ABCD1234 1 77\n", stderr=""
        )
        with patch(
            "app.core.dummy_damage_stats.default_dummy_damage_reader_path",
            return_value=Path("dummy_damage_reader.exe"),
        ), patch.object(Path, "is_file", return_value=True), patch(
            "app.core.dummy_damage_stats.subprocess.run", return_value=completed
        ) as run:
            self.assertEqual(scan_dummy_damage_sequences(Session()), [[77]])
        self.assertEqual(run.call_args.args[0][1], "23804")

    def test_present_reader_failure_does_not_fall_back_to_pymem(self) -> None:
        class Session:
            pid = 23804
            pm = object()

        completed = CompletedProcess(
            args=[], returncode=3, stdout="", stderr="OpenProcess failed error=5"
        )
        with patch(
            "app.core.dummy_damage_stats.default_dummy_damage_reader_path",
            return_value=Path("dummy_damage_reader.exe"),
        ), patch.object(Path, "is_file", return_value=True), patch(
            "app.core.dummy_damage_stats.subprocess.run", return_value=completed
        ):
            with self.assertRaisesRegex(RuntimeError, "OpenProcess failed"):
                scan_dummy_damage_sequences(Session())

    def test_parse_native_snapshot_keeps_64_bit_target_and_total(self) -> None:
        parsed = parse_dummy_damage_stat(
            _note(started=1, done=0, ms=12345, hits=37, total=9_876_543_210)
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["target_id"], 0x0200000089ABCDEF)
        self.assertEqual(parsed["elapsed_ms"], 12345)
        self.assertEqual(parsed["hits"], 37)
        self.assertEqual(parsed["total_damage"], 9_876_543_210)

    def test_select_training_dummy_requires_exact_name_and_nearest(self) -> None:
        entities = [
            EntityHit("npc", "高级木人桩", 1, dist=1.0),
            EntityHit("npc", "木人桩", 2, dist=9.0),
            EntityHit("npc", "木人桩", 3, dist=3.0),
        ]
        selected = select_training_dummy(entities)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.address, 3)

    def test_history_extension_handles_growth_and_ring_move(self) -> None:
        history = [1, 2, 3, 4, 5, 6, 7, 8]
        merged, added = extend_damage_history(
            history,
            ([90, 91], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),
        )
        self.assertEqual(merged, history + [9, 10])
        self.assertEqual(added, [9, 10])

        moved, moved_added = extend_damage_history(
            merged,
            ([3, 4, 5, 6, 7, 8, 9, 10, 11],),
        )
        self.assertEqual(moved, merged + [11])
        self.assertEqual(moved_added, [11])

    def test_history_extension_uses_length_growth_after_buffer_rebuild(self) -> None:
        history = [1, 2, 3, 4, 5, 6, 7, 8]
        rebuilt, added = extend_damage_history(
            history,
            ([90, 91], [101, 102, 103, 104, 105, 106, 107, 108, 9, 10]),
        )
        self.assertEqual(rebuilt, history + [9, 10])
        self.assertEqual(added, [9, 10])

    def test_candidate_delta_ignores_long_stale_copy_and_tracks_active_copy(self) -> None:
        stale = list(range(100, 140))
        active = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        references = update_damage_references([], (stale, active))
        current = (
            stale,
            active + [10, 11],
            active + [10, 11],
        )
        self.assertEqual(find_damage_sequence_delta(references, current), [10, 11])

    def test_candidate_delta_handles_long_repeated_damage_history(self) -> None:
        stale = [25_000_000, 80_000_000] * 400
        active = [95_830_704] * 179
        added = [95_830_704] * 74
        references = update_damage_references([], (stale, active))
        self.assertEqual(
            find_damage_sequence_delta(references, (active + added, active + added)),
            added,
        )

    def test_reader_parser_keeps_duplicate_copies_for_activity_voting(self) -> None:
        parsed = parse_dummy_damage_reader_output(
            "RAW 4 RUNS 2\n"
            "SEQ 84F5B11E 2 10 20\n"
            "SEQ 88F5B11E 2 10 20\n"
        )
        self.assertEqual(parsed, [[10, 20], [10, 20]])

    def test_candidate_delta_does_not_recount_returning_copy(self) -> None:
        old = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        references = update_damage_references([], (old,))
        self.assertEqual(find_damage_sequence_delta(references, (old,)), [])

    def test_candidate_delta_starts_from_first_nonempty_snapshot(self) -> None:
        first = [25_000_000, 80_000_000]
        self.assertEqual(find_damage_sequence_delta([], (first, first)), first)

    def test_candidate_delta_allows_small_growth_with_duplicate_vote(self) -> None:
        stale = [95_000_000] * 200
        active = [25_000_000] * 6
        grown = active + [80_000_000]
        references = update_damage_references([], (stale, active))
        self.assertEqual(find_damage_sequence_delta(references, (grown, grown)), [80_000_000])

    def test_measurement_waits_for_first_ui_hit_then_uses_fixed_window(self) -> None:
        class Session:
            pid = 123
            hwnd = 456

        updates = []
        snapshots = iter(
            (
                [[100, 200]],
                [[100, 200, 250]],
                [[100, 200, 250]],
            )
        )
        with patch(
            "app.core.dummy_damage_stats.scan_dummy_damage_sequences",
            side_effect=lambda _session: next(snapshots),
        ), patch("app.core.dummy_damage_stats.WINDOW_SECONDS", 0.01):
            result = measure_dummy_damage(
                Session(), poll_s=0.1, on_update=updates.append
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["completed"])
        self.assertEqual(result["hits"], 1)
        self.assertEqual(result["total_damage"], 250)
        self.assertEqual(
            [item["phase"] for item in updates],
            ["waiting", "running", "running"],
        )


if __name__ == "__main__":
    unittest.main()
