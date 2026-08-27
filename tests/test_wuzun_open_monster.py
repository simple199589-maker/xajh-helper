from __future__ import annotations

import unittest

from app.core.wuzun_open_monster import (
    _select_rows,
    build_wuzun_dialogue_packets,
    learn_current_session,
    parse_system_packet,
    validate_wuzun_open_gate,
    is_wuzun_open_monster_active,
    _set_wuzun_open_monster_active,
)


def record(payload: bytes, tick: int) -> dict:
    return {"data": payload, "tick": tick}


class WuzunOpenMonsterProtocolTests(unittest.TestCase):
    def test_active_state_only_covers_packet_send_window(self) -> None:
        self.assertFalse(is_wuzun_open_monster_active(9988))
        _set_wuzun_open_monster_active(9988, True)
        try:
            self.assertTrue(is_wuzun_open_monster_active(9988))
            self.assertFalse(is_wuzun_open_monster_active(9989))
        finally:
            _set_wuzun_open_monster_active(9988, False)
        self.assertFalse(is_wuzun_open_monster_active(9988))

    def test_gate_only_requires_wuzun_scene_and_live_hang(self) -> None:
        self.assertEqual(validate_wuzun_open_gate(1255, True), (True, None))
        self.assertEqual(validate_wuzun_open_gate(1541, True), (True, None))
        self.assertFalse(validate_wuzun_open_gate(1255, False)[0])
        self.assertFalse(validate_wuzun_open_gate(9999, True)[0])

    def test_gate_does_not_require_hang_mode(self) -> None:
        self.assertEqual(validate_wuzun_open_gate(1255, True), (True, None))

    def test_parser_uses_current_opcode_and_payload(self) -> None:
        raw = bytes.fromhex("0A00 3412 A1B2C3D4 0001")
        parsed = parse_system_packet(record(raw, 10))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.opcode, 0x1234)
        self.assertEqual(parsed.raw, raw)
        self.assertEqual(parsed.body, bytes.fromhex("A1B2C3D40001"))

    def test_learn_uses_generic_monsters_after_dialogue_only(self) -> None:
        learned = learn_current_session([record(bytes.fromhex("0A00 9123 010203040001"), 100)])
        self.assertTrue(learned["ok"])
        self.assertEqual(len(learned["dialogue"]), 1)
        self.assertEqual(len(learned["monster"]), 16)

    def test_rows_are_selected_from_current_sequence(self) -> None:
        records = []
        for index in range(16):
            body = bytes([index]) * 10
            packet = (len(body) + 4).to_bytes(2, "little") + (0x7000 + index).to_bytes(2, "little") + body
            records.append(record(packet, index + 1))
        learned = learn_current_session([record(bytes.fromhex("0A00 1122 010203040001"), 0)] + records)
        monsters = learned["monster"]
        self.assertEqual(len(_select_rows(monsters, 0)), 16)
        self.assertEqual([p.raw for p in _select_rows(monsters, 1)], [p.raw for p in monsters[:5]])
        self.assertEqual([p.raw for p in _select_rows(monsters, 2)], [p.raw for p in monsters[5:10]])
        self.assertEqual([p.raw for p in _select_rows(monsters, 3)], [p.raw for p in monsters[10:16]])

    def test_short_frame_is_rejected(self) -> None:
        self.assertIsNone(parse_system_packet(record(bytes.fromhex("0B00 12"), 1)))

    def test_dialogue_packets_use_live_object_id_low_word(self) -> None:
        packets = build_wuzun_dialogue_packets(72124)
        self.assertEqual([packet.raw.hex().upper() for packet in packets], [
            "0A00BC19010000000001",
            "0C00BC19010000000001",
        ])
        self.assertEqual([packet.length for packet in packets], [10, 10])

        other = build_wuzun_dialogue_packets(86671)
        self.assertEqual(other[0].raw.hex().upper(), "0A008F52010000000001")
        self.assertEqual(other[1].raw.hex().upper(), "0C008F52010000000001")

    def test_dialogue_packet_rejects_invalid_object_id(self) -> None:
        with self.assertRaises(ValueError):
            build_wuzun_dialogue_packets(0)


if __name__ == "__main__":
    unittest.main()
