# -*- coding: utf-8 -*-
"""test_private_packet.py — 私聊封包假设实测。

假设 A1/A2：私聊 c2s = 队伍封包变体，channel u32@0x0D=9，
目标 role_id 填在 offset 0x02..0x05（A1=大端，A2=小端）。

用法:
    python tools/test_private_packet.py --sender 6456 --sender-rid 19611649 \
        --receiver 35832 --target-rid 647169 --text "PRIVTEST-A1" --variant be

流程：构建封包 → team_tap mailbox 发送 → 轮询收/发双方 chat_tap 主 ring
（含所有频道，ch=9 即私聊）8 秒，打印新增事件。
@author by ak
"""
import argparse
import struct
import sys
import time

sys.path.insert(0, ".")

from app.core.chat_tap import ChatTapReader
from app.core.team_chat import (
    _team_mailbox_send,
    build_team_chat_c2s,
    cache_role_identity,
)


def watch(reader: ChatTapReader, cursor: int, seconds: float, label: str) -> int:
    deadline = time.monotonic() + seconds
    cur = cursor
    while time.monotonic() < deadline:
        events, cur, lost, _hdr = reader.read_after(cur)
        for ev in events:
            text = ev["text"].replace("\n", " ")
            print(
                f"  [{label}] ch={ev['channel']:>2} seq={ev['seq']} {text[:120]}"
            )
        time.sleep(0.25)
    return cur


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sender", type=int, required=True, help="发送方游戏 pid")
    ap.add_argument("--sender-rid", type=int, required=True, help="发送方 role_id")
    ap.add_argument("--receiver", type=int, required=True, help="接收方游戏 pid（观测用）")
    ap.add_argument("--target-rid", type=int, required=True, help="目标 role_id")
    ap.add_argument("--text", default="PRIVTEST")
    ap.add_argument("--variant", choices=["be", "le"], default="be")
    args = ap.parse_args()

    cache_role_identity(args.sender, args.sender_rid)

    pkt = bytearray(
        build_team_chat_c2s(args.text, channel_id=9)
    )
    tgt = args.target_rid & 0xFFFFFFFF
    if args.variant == "be":
        pkt[0x02:0x06] = struct.pack(">I", tgt)
    else:
        pkt[0x02:0x06] = struct.pack("<I", tgt)
    print(f"封包({args.variant}): {bytes(pkt).hex(' ')}")

    rx = ChatTapReader.open(args.receiver)
    tx = ChatTapReader.open(args.sender)
    if rx is None:
        print("接收方 chat_tap 未注入，无法观测")
        return 2
    rx_cur = rx.latest_cursor
    tx_cur = tx.latest_cursor if tx else 0
    rx_seq0 = rx.latest_cursor

    result = _team_mailbox_send(args.sender, bytes(pkt), log=lambda m: print(f"  [send] {m}"))
    print(f"mailbox result: ok={result.get('ok')} ret={result.get('ret')} err={result.get('error')}")

    print("--- 发送方 ring（8s）---")
    tx_cur = watch(tx, tx_cur, 8.0, "发") if tx else 0
    print("--- 接收方 ring（8s）---")
    rx_cur = watch(rx, rx_cur, 8.0, "收")
    alive = rx.latest_cursor > rx_seq0
    print(f"接收方连接活性: write_seq {rx_seq0} -> {rx.latest_cursor} ({'活跃' if alive else '无新增'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
