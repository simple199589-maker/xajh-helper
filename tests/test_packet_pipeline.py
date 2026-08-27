from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packet.action_diff import compare, head_hist, size_hist, unique_by_exact_hex
from packet.frame_split import auto_guess, score_frames, try_split
from packet.stream_extract import (
    TcpSeg,
    extract_ipv4_tcp_payload,
    group_streams,
    parse_hex_block,
    segs_to_jsonl,
    summarize,
)
from packet.tcp_proxy import SessionRecorder, hexdump, parse_hostport


def _ipv4_tcp(payload: bytes = b"", *, prefix: bytes = b"", ihl_words: int = 5) -> bytes:
    ihl = ihl_words * 4
    ip = bytearray(ihl + 20 + len(payload))
    ip[0] = 0x40 | ihl_words
    struct.pack_into("!H", ip, 2, len(ip))
    ip[9] = 6
    ip[ihl + 12] = 0x50
    ip[ihl + 20:] = payload
    return prefix + bytes(ip)


class PacketPipelineTests(unittest.TestCase):
    def test_frame_split_and_score(self) -> None:
        data = b"\x04\x00ab\x05\x00xyz"
        frames = try_split(data)
        self.assertEqual(frames, [b"\x04\x00ab", b"\x05\x00xyz"])
        self.assertEqual(score_frames(frames, len(data))["coverage"], 1.0)
        self.assertEqual(auto_guess(data)[0]["leftover"], 0)

    def test_parse_hex_block(self) -> None:
        raw = parse_hex_block(["  0x0000:  45 00 00 28", "  0x0004:  01 02", "stop"])
        self.assertEqual(raw, bytes.fromhex("450000280102"))

    def test_extract_payload_at_zero_and_last_legal_offset(self) -> None:
        self.assertEqual(extract_ipv4_tcp_payload(_ipv4_tcp(b"abc")), b"abc")
        # Exactly 40 bytes after the prefix exercises the inclusive final offset.
        self.assertEqual(extract_ipv4_tcp_payload(_ipv4_tcp(b"", prefix=b"prefix")), b"")

    def test_extract_payload_supports_ipv4_options(self) -> None:
        self.assertEqual(extract_ipv4_tcp_payload(_ipv4_tcp(b"opts", ihl_words=6)), b"opts")
        self.assertIsNone(extract_ipv4_tcp_payload(b"not a packet"))

    def test_tcp_direction_group_and_summary(self) -> None:
        segs = [
            TcpSeg("t", "10.0.0.2", 5000, "1.2.3.4", 6598, "P", b"up", 42),
            TcpSeg("t", "1.2.3.4", 6598, "10.0.0.2", 5000, "P", b"down", 44),
        ]
        self.assertEqual(segs[0].direction("1.2.3.4", 6598), "c2s")
        grouped = group_streams(segs, "1.2.3.4", 6598)
        self.assertEqual(grouped["10.0.0.2:5000"], {"c2s": b"up", "s2c": b"down"})
        self.assertEqual(summarize(segs, "1.2.3.4", 6598)["s2c_bytes"], 4)

    def test_jsonl_output(self) -> None:
        seg = TcpSeg("t", "a", 1, "server", 2, "P", b"xx", 42)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.jsonl"
            segs_to_jsonl([seg], "server", 2, path)
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(row["hex"], "7878")

    def test_action_histograms_unique_and_compare(self) -> None:
        base = [{"dir": "c2s", "len": 2, "hex": "aabb"}]
        action = base + [{"dir": "c2s", "len": 3, "hex": "ccddee"}]
        self.assertEqual(size_hist(action, "c2s")[3], 1)
        self.assertEqual(head_hist(action, "c2s", 1)["aa"], 1)
        self.assertEqual(unique_by_exact_hex(action, base, "c2s")[0]["hex"], "ccddee")
        self.assertEqual(compare(base, action)["size_delta_c2s"]["3"], 1)

    def test_proxy_helpers(self) -> None:
        self.assertEqual(parse_hostport("127.0.0.1:123"), ("127.0.0.1", 123))
        dump = hexdump(b"A\x00B")
        self.assertIn("41 00 42", dump)
        self.assertIn("A.B", dump)

    @patch("builtins.print")
    def test_session_recorder_writes_both_directions(self, _print) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rec = SessionRecorder(root, "s")
            rec.write("c2s", b"up")
            rec.write("s2c", b"down")
            rec.close()
            self.assertEqual((root / "s.c2s.bin").read_bytes(), b"up")
            self.assertEqual((root / "s.s2c.bin").read_bytes(), b"down")
            self.assertEqual(len((root / "s.jsonl").read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
