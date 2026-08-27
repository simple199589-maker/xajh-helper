# -*- coding: utf-8 -*-
"""chat_tap 接收链路 4 点诊断脚本

用法（在项目根目录运行）：
    python tools/diag_chat_tap.py <pid1> [pid2] [pid3] ...

诊断覆盖：
  1. 共享内存注入状态（magic/version/status/error）
  2. Hook 是否触发（监听期间手动发队伍消息，看 write_seq 是否变化）
  3. 队伍 ring 最近事件内容（是否收到但被过滤）
  4. 多 pid 对比汇总（找共性问题）
"""
from __future__ import annotations

import sys
import time

# 确保项目根目录在 sys.path 中
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.chat_tap import (
    CHAT_TAP_ACTIVE,
    CHAT_TAP_ERROR,
    CHAT_TAP_INIT,
    ChatTapReader,
    TEAM_TAP_WRITE_SEQ_OFF,
)

STATUS_NAMES = {
    CHAT_TAP_INIT: "INIT(未激活)",
    CHAT_TAP_ACTIVE: "ACTIVE(正常)",
    CHAT_TAP_ERROR: "ERROR",
}

WATCH_SECONDS = 8.0  # 第 2 步监听窗口时长


def _status_label(v: int) -> str:
    return STATUS_NAMES.get(v, f"UNKNOWN({v})")


def step1_injection_status(pids: list[int]) -> dict[int, dict]:
    """检查每个 pid 的共享内存是否打开、头部字段是否正常。"""
    print("\n" + "=" * 70)
    print("诊断 1/4  共享内存注入状态")
    print("=" * 70)
    results = {}
    for pid in pids:
        reader = ChatTapReader.open(pid)
        if reader is None:
            print(f"  pid={pid}  ✗ 共享内存未找到（DLL 未注入或已卸载）")
            results[pid] = {"open": False}
            continue
        try:
            h = reader.header()
            status = int(h.get("status", 0))
            ok = status == CHAT_TAP_ACTIVE
            mark = "✓" if ok else "✗"
            print(
                f"  pid={pid}  {mark} magic=0x{h['magic']:08X} "
                f"ver={h['version']} status={_status_label(status)} "
                f"target_va=0x{h['target_va']:08X}"
            )
            if h.get("error"):
                print(f"           error={h['error']!r}")
            results[pid] = {"open": True, "header": h, "active": ok}
        except Exception as exc:
            print(f"  pid={pid}  ✗ 读取头部失败: {exc}")
            results[pid] = {"open": True, "error": str(exc)}
        finally:
            reader.close()
    return results


def step2_hook_trigger(pids: list[int]) -> dict[int, bool]:
    """监听 team ring write_seq，检测 hook 是否被游戏消息触发。"""
    print("\n" + "=" * 70)
    print(f"诊断 2/4  Hook 触发测试（接下来 {WATCH_SECONDS:.0f} 秒）")
    print("=" * 70)
    print("  >>> 请在游戏里用任意号在【队伍频道】手动发一条消息 <<<")
    print()

    # 记录初始 write_seq
    readers = {}
    seq_before = {}
    for pid in pids:
        r = ChatTapReader.open(pid)
        if r is None:
            print(f"  pid={pid}  跳过（共享内存未打开）")
            continue
        try:
            snap = r.snapshot()
            seq = int.from_bytes(
                snap[TEAM_TAP_WRITE_SEQ_OFF : TEAM_TAP_WRITE_SEQ_OFF + 4], "little"
            )
            seq_before[pid] = seq
            readers[pid] = r
            print(f"  pid={pid}  初始 team_write_seq={seq}")
        except Exception as exc:
            print(f"  pid={pid}  读取初始 seq 失败: {exc}")
            r.close()

    if not readers:
        return {}

    print(f"\n  监听中（{WATCH_SECONDS:.0f} 秒）...")
    deadline = time.monotonic() + WATCH_SECONDS
    seq_now = dict(seq_before)
    while time.monotonic() < deadline:
        time.sleep(0.5)
        for pid, r in readers.items():
            try:
                snap = r.snapshot()
                s = int.from_bytes(
                    snap[TEAM_TAP_WRITE_SEQ_OFF : TEAM_TAP_WRITE_SEQ_OFF + 4],
                    "little",
                )
                if s != seq_now[pid]:
                    print(f"  [{time.strftime('%H:%M:%S')}] pid={pid}  write_seq {seq_now[pid]} → {s}")
                    seq_now[pid] = s
            except Exception:
                pass

    results = {}
    print()
    for pid, r in readers.items():
        changed = seq_now[pid] != seq_before[pid]
        mark = "✓" if changed else "✗"
        reason = "hook 已触发（write_seq 有变化）" if changed else "hook 未触发（write_seq 无变化）"
        print(f"  pid={pid}  {mark} {reason}")
        results[pid] = changed
        r.close()
    return results


def step3_recent_events(pids: list[int], count: int = 10) -> dict[int, list[dict]]:
    """读队伍 ring 最近 N 条事件，检查内容是否为空或异常。"""
    print("\n" + "=" * 70)
    print(f"诊断 3/4  队伍 ring 最近 {count} 条事件")
    print("=" * 70)
    all_events = {}
    for pid in pids:
        r = ChatTapReader.open(pid)
        if r is None:
            print(f"\n  pid={pid}  跳过（共享内存未打开）")
            continue
        try:
            snap = r.snapshot()
            newest = int.from_bytes(
                snap[TEAM_TAP_WRITE_SEQ_OFF : TEAM_TAP_WRITE_SEQ_OFF + 4], "little"
            )
            cursor = max(0, newest - count)
            events, consumed, lost, hdr = r.read_team_after(cursor)
            all_events[pid] = events
            if not events:
                print(f"\n  pid={pid}  队伍 ring 为空（write_seq={newest}）")
            else:
                print(f"\n  pid={pid}  共 {len(events)} 条（write_seq={newest} lost={lost}）")
                for ev in events:
                    ch = ev.get("channel", "?")
                    txt = (ev.get("text") or "")[:80]
                    print(
                        f"    seq={ev['seq']:>4} ch={ch} len={ev.get('text_len', len(txt))} "
                        f"| {txt}"
                    )
        except Exception as exc:
            print(f"\n  pid={pid}  读取事件失败: {exc}")
            all_events[pid] = []
        finally:
            r.close()
    return all_events


def step4_summary(
    pids: list[int],
    inject: dict[int, dict],
    trigger: dict[int, bool],
    events: dict[int, list[dict]],
) -> None:
    """汇总四个维度，标出共性问题。"""
    print("\n" + "=" * 70)
    print("诊断 4/4  汇总")
    print("=" * 70)
    header = f"  {'pid':>7}  {'注入':>4}  {'状态':>14}  {'Hook触发':>8}  {'事件数':>6}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for pid in pids:
        inj = inject.get(pid, {})
        opened = inj.get("open", False)
        active = inj.get("active", False)
        status_str = _status_label(inj.get("header", {}).get("status", 0)) if opened else "未打开"
        hooked = trigger.get(pid)
        hook_str = ("✓" if hooked else "✗") if hooked is not None else "—"
        n_ev = len(events.get(pid, []))
        open_mark = "✓" if opened else "✗"
        active_mark = "✓" if active else "✗"
        print(
            f"  {pid:>7}  {open_mark:>4}  {status_str:>14}  {hook_str:>8}  {n_ev:>6}"
        )

    # 共性问题判定
    print()
    opens = [p for p in pids if inject.get(p, {}).get("open")]
    actives = [p for p in opens if inject.get(p, {}).get("active")]
    hooked = [p for p in opens if trigger.get(p)]

    if not opens:
        print("  ⚠ 所有 pid 共享内存均未打开 → chat_tap DLL 完全未注入")
        print("    → 检查 xajh_chat_tap.dll 是否存在、注入器是否报错")
    elif not actives:
        print("  ⚠ 共享内存已打开但所有 pid 均非 ACTIVE → DLL 加载后初始化失败")
        print("    → 查看 error 字段，可能是游戏版本更新导致 target_va 偏移失效")
    elif not hooked:
        print("  ⚠ DLL 注入且 ACTIVE，但监听期间无 pid 的 write_seq 变化")
        print("    → hook 地址 0x00885300 可能已过时（游戏更新后偏移变化）")
        print("    → 或消息不走该 hook 路径，需要用 x32dbg 交叉验证")
    elif len(hooked) < len(opens):
        bad = [p for p in opens if not trigger.get(p)]
        print(f"  ⚠ 部分 pid hook 未触发: {bad}")
        print("    → 可能是单进程注入时序问题，尝试重新注入该 pid")
    else:
        print("  ✓ 所有已注入 pid 的 hook 均已触发")
        no_events = [p for p in hooked if not events.get(p)]
        if no_events:
            print(f"  ⚠ 但以下 pid 队伍 ring 仍无事件: {no_events}")
            print("    → hook 触发了但事件未写入队伍 ring，检查 channel 过滤逻辑")
        else:
            print("  ✓ 队伍 ring 有事件，接收链路正常")
            print("    → 如果副控仍不回 PONG，问题在消息解析/命令分发层（而非 tap 层）")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        print("示例：python tools/diag_chat_tap.py 5252 11504 14044")
        sys.exit(1)

    try:
        pids = [int(a) for a in sys.argv[1:]]
    except ValueError:
        print("错误：pid 必须是整数")
        sys.exit(1)

    print(f"chat_tap 接收链路诊断  pids={pids}")
    print(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")

    inject = step1_injection_status(pids)
    trigger = step2_hook_trigger(pids)
    events = step3_recent_events(pids)
    step4_summary(pids, inject, trigger, events)

    print("\n诊断完成。")


if __name__ == "__main__":
    main()
