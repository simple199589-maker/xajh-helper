# -*- coding: utf-8 -*-
"""Tests for CD1740 packet annotation (dev panel 封包拦截 解析标注). @author by ak"""
from __future__ import annotations

import unittest

from app.core.packet_intercept import (
    annotate_packet,
    parse_jianglong_packet,
    format_annotated_record,
    packet_detail_text,
    parse_record,
)

P1_WAIGONG = bytes.fromhex(
    "1F0000000000005F0A01030100014200000000000000000000"
    "60C0FCC162FD7242E09E86C201"
)
P1_NEIGONG = bytes.fromhex(
    "1F0000000000005E0A01036300004200000000000000000000"
    "AC9303C262FD72428A7481C201"
)
P2 = bytes.fromhex(
    "1F0000000000005D0A01020800FF4100000000000000000000"
    "268B97C28D8A7A42A212C4C301"
)


def _rec(data: bytes, length: int | None = None) -> dict:
    import struct

    blob = bytearray(0x20 + len(data))
    this, packet, length, ret, tid, tick = 0x1, 0x2, length or len(data), 0x3, 0x4, 0x5
    struct.pack_into("<6I", blob, 0, this, packet, length, ret, tid, tick)
    blob[0x20:] = data
    return parse_record(bytes(blob))


class PacketAnnotateTests(unittest.TestCase):
    def test_jianglong_parser_keeps_runtime_variant_and_position(self) -> None:
        packet = bytes.fromhex(
            "1F000726010000052301FFFFFF00000000E407000000000001"
            "807B16C268F7724200002CC201"
        )
        parsed = parse_jianglong_packet(packet)
        self.assertTrue(parsed["known"])
        self.assertEqual(parsed["variant_id"], 2020)
        self.assertEqual(parsed["variant_bytes"], "E4070000")
        self.assertEqual(parsed["pos"], (-37.62060546875, 60.741607666015625, -43.0))

    def test_jianglong_parser_rejects_wrong_header(self) -> None:
        self.assertFalse(parse_jianglong_packet(b"\x00" * 38)["known"])

    def test_jianglong_annotation_shows_runtime_variant(self) -> None:
        packet = bytes.fromhex(
            "1F000726010000052301FFFFFF00000000E307000000000001"
            "807B16C268F77242000038C201"
        )
        ann = annotate_packet(packet)
        self.assertTrue(ann["known"])
        self.assertEqual(ann["name"], "降龙十八掌")
        self.assertEqual(ann["variant_id"], 2019)
        self.assertIn("variant=E3070000", format_annotated_record(_rec(packet), 1))

    def test_p1_waigong_recognized_with_package_slot_and_pos(self) -> None:
        ann = annotate_packet(P1_WAIGONG)
        self.assertTrue(ann["known"])
        self.assertEqual(ann["name"], "丸子P1·外功")
        self.assertEqual(ann["len16"], 0x1F)
        self.assertEqual(ann["tid"], 0x4201)
        self.assertEqual(ann["package"], 0x03)
        self.assertEqual(ann["slot"], 0x01)
        self.assertIsNotNone(ann["pos"])
        x, _y, _z = ann["pos"]
        self.assertNotEqual(x, 0.0)

    def test_p1_neigong_recognized(self) -> None:
        ann = annotate_packet(P1_NEIGONG)
        self.assertTrue(ann["known"])
        self.assertEqual(ann["name"], "丸子P1·内功")

    def test_p2_recognized(self) -> None:
        ann = annotate_packet(P2)
        self.assertTrue(ann["known"])
        self.assertEqual(ann["name"], "丸子P2")
        self.assertIsNone(ann["package"])

    def test_unknown_short_packet_not_known(self) -> None:
        ann = annotate_packet(b"\x04\x00ab")
        self.assertFalse(ann["known"])
        self.assertEqual(ann["len16"], 0x0004)

    def test_unknown_tid_out_of_range(self) -> None:
        # kind byte 0x5F but tid not in the wanzi family -> not recognized
        ann = annotate_packet(
            bytes.fromhex("1F0000000000005F0A0103010001ABCDEF00000000000000000000"
                          "60C0FCC162FD7242E09E86C201")
        )
        self.assertFalse(ann["known"])

    def test_format_annotated_record_appends_label(self) -> None:
        rec = _rec(P1_WAIGONG)
        line = format_annotated_record(rec, 7)
        self.assertIn("#7", line)
        self.assertIn("丸子P1·外功", line)
        self.assertIn("P3/S1", line)

    def test_packet_detail_text(self) -> None:
        rec = _rec(P1_WAIGONG)
        text = packet_detail_text(rec)
        self.assertIn("丸子P1·外功", text)
        self.assertIn("背包 P3 / 槽 1", text)
        self.assertIn("坐标", text)
        self.assertIn("HEX", text)

    def test_packet_detail_text_unknown(self) -> None:
        rec = _rec(b"\x04\x00ab")
        text = packet_detail_text(rec)
        self.assertIn("未识别系统指令", text)

    def test_replay_smoke_format_only(self) -> None:
        # Ensure annotated lines for P1/P2/P3 export path are strings.
        for payload in (P1_WAIGONG, P1_NEIGONG, P2, b"\x04\x00ab"):
            self.assertIsInstance(format_annotated_record(_rec(payload), 0), str)


if __name__ == "__main__":
    unittest.main()
