# -*- coding: utf-8 -*-
"""私聊控制面端到端测试脚本（tools/test_private_e2e.py）。

阶段：
  0  环境自检    游戏进程/角色识别、team_tap v5 与 chat_tap 注入状态、花名册
  1  私聊收发    主→副 / 副→主 双向标记消息，接收方 ch=9 确认（走真实服务器）
  2  协议往返    [主P]PLEAVE → 副控处理(mock离队，不拆队) → [副G]PLEFT 回执
  3  真离队演练  同阶段2但副控真执行 leave_team（会拆掉当前队伍！默认跳过）

用法（仓库根目录）：
  python tools/test_private_e2e.py                    # 阶段 0,1,2（安全）
  python tools/test_private_e2e.py --real-leave       # 加跑阶段 3（真离队）
  python tools/test_private_e2e.py --master 6456 --slave 35952
  python tools/test_private_e2e.py --stages 1,2       # 只跑指定阶段

主副控默认自动识别：窗口标题取角色名 → 花名册取 rid → roles/<rid>/control.json
取 role；识别失败时用 --master/--slave 指定 pid。
阶段 1/2/3 均走游戏服务器中转，跨设备部署同样适用。
@author by ak
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, ".")

from app.core.chat_tap import ChatTapReader
from app.core.private_team_link import (
    PrivateChatWatch,
    handle_pleave_commands,
    request_slaves_leave,
)
from app.core.team_chat import (
    cache_role_identity,
    cache_role_name,
    send_private_message,
)

ROOT = Path(__file__).resolve().parents[1]
TEAM_PREFS = ROOT / "runtime" / "config" / "team_prefs.json"
ROLES_DIR = ROOT / "runtime" / "config" / "roles"

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, str, str]] = []


def report(stage: str, name: str, ok: bool, note: str = "") -> None:
    tag = PASS if ok else FAIL
    _results.append((stage, name, tag))
    print(f"  [{tag}] 阶段{stage} {name}" + (f" — {note}" if note else ""))


def summary() -> int:
    print()
    print("=" * 56)
    failed = [r for r in _results if r[2] == FAIL]
    print(f"结果: {len(_results) - len(failed)}/{len(_results)} PASS")
    for stage, name, tag in _results:
        if tag == FAIL:
            print(f"  失败: 阶段{stage} {name}")
    print("=" * 56)
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# 环境发现
# ---------------------------------------------------------------------------

def window_titles_by_pid() -> dict[int, str]:
    """枚举顶层窗口 → {pid: 标题}（取每个 pid 最长标题）。@author by ak"""
    out: dict[int, str] = {}
    user32 = ctypes.WinDLL("user32")
    EnumWindows = user32.EnumWindows
    GetWindowTextW = user32.GetWindowTextW
    GetWindowThreadProcessId = user32.GetWindowThreadProcessId
    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )

    titles: dict[int, list[str]] = {}

    def _cb(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        n = GetWindowTextW(ctypes.c_void_p(hwnd), buf, 256)
        if n <= 0:
            return True
        pid = wintypes.DWORD(0)
        GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        if pid.value:
            titles.setdefault(int(pid.value), []).append(buf.value)
        return True

    EnumWindows(WNDENUMPROC(_cb), None)
    for pid, ts in titles.items():
        out[pid] = max(ts, key=len)
    return out


def discover_games() -> dict[int, str]:
    """xajh 进程 → {pid: 角色名}（从窗口标题“- 名字”解析）。@author by ak"""
    import re

    games: dict[int, str] = {}
    for pid, title in window_titles_by_pid().items():
        if not title.startswith("笑傲江湖OL"):
            continue
        m = re.search(r"- ([^-]+?)(?:\s*\[GUI\])?\s*$", title)
        name = (m.group(1) if m else "").strip()
        if name:
            games[pid] = name
    return games


def load_roster() -> list[dict]:
    """花名册 team_verified_roster。@author by ak"""
    try:
        data = json.loads(TEAM_PREFS.read_text(encoding="utf-8"))
        return list(data.get("team_verified_roster") or [])
    except Exception:
        return []


def control_role(rid: int) -> str:
    """roles/<rid>/control.json 的 role 字段。@author by ak"""
    try:
        data = json.loads(
            (ROLES_DIR / str(rid) / "control.json").read_text(encoding="utf-8")
        )
        return str(data.get("role") or "")
    except Exception:
        return ""


def rid_by_name(roster: list[dict]) -> dict[str, int]:
    return {
        str((r or {}).get("name") or "").strip(): int((r or {}).get("obj_id") or 0)
        for r in roster
        if (r or {}).get("name")
    }


# ---------------------------------------------------------------------------
# 阶段
# ---------------------------------------------------------------------------

def stage0_env(games: dict[int, str], roster: list[dict]) -> dict:
    print(f"[阶段0] 环境自检 — games={games}")
    report("0", "游戏进程识别", bool(games), f"{len(games)} 个在线")
    names = rid_by_name(roster)
    missing = [n for n in games.values() if n not in names]
    report("0", "花名册覆盖", not missing, f"缺 rid: {missing}" if missing else f"{len(names)} 人")
    taps_ok = True
    for pid in games:
        r = ChatTapReader.open(pid)
        chat_ok = r is not None
        if r is not None:
            mode = "v3私聊ring" if not r.legacy else "v2主ring回退"
            r.close()
        report("0", f"chat_tap pid={pid}({games[pid]})", chat_ok, mode if chat_ok else "未注入")
        taps_ok = taps_ok and chat_ok
    return {"ok": bool(games) and not missing and taps_ok}


def stage1_roundtrip(master: dict, slave: dict) -> None:
    mp, sp = master["pid"], slave["pid"]
    print(f"[阶段1] 私聊收发 — {master['name']}({mp}) ↔ {slave['name']}({sp})")
    marker_down = f"PRIV-E2E-D-{int(time.time()) % 100000}"
    marker_up = f"PRIV-E2E-U-{int(time.time()) % 100000}"
    watch = PrivateChatWatch(sp)
    watch_up = PrivateChatWatch(mp)
    try:
        res = send_private_message(mp, slave["rid"], slave["name"], marker_down)
        report("1", f"发送 {master['name']}→{slave['name']}", bool(res.get("ok")),
               str(res.get("error") or ""))
        res2 = send_private_message(sp, master["rid"], master["name"], marker_up)
        report("1", f"发送 {slave['name']}→{master['name']}", bool(res2.get("ok")),
               str(res2.get("error") or ""))
        got_down = got_up = False
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline and not (got_down and got_up):
            for msg in watch.poll_messages():
                if marker_down in (msg.get("text") or ""):
                    got_down = True
            for msg in watch_up.poll_messages():
                if marker_up in (msg.get("text") or ""):
                    got_up = True
            time.sleep(0.25)
        report("1", f"接收 {slave['name']} 收到 ch=9", got_down, marker_down)
        report("1", f"接收 {master['name']} 收到 ch=9", got_up, marker_up)
    finally:
        watch.close()
        watch_up.close()


class _FakeOk:
    ok = True


def stage2_protocol(master: dict, slave: dict, roster: list[dict], real_leave: bool) -> None:
    mp, sp = master["pid"], slave["pid"]
    mode = "真离队" if real_leave else "mock离队(不拆队)"
    print(f"[阶段2] PLEAVE/PLEFT 协议往返 — 副控={slave['name']}({sp}) [{mode}]")
    if real_leave:
        print("  ⚠ 真离队模式：副控将执行 leave_team，当前队伍会被拆散！")
        time.sleep(2.0)

    leave_fn = None
    attach = None
    if real_leave:
        from app.core.team_ops import leave_team

        from app.core.loot import open_attach_session

        attach = open_attach_session(sp)
        if attach is None:
            report("2", "副控会话挂载", False, f"pid={sp} attach 失败")
            return
        leave_fn = lambda session, log=None: leave_team(session, log=log)  # noqa: E731
    else:
        leave_fn = lambda session, log=None: _FakeOk()  # noqa: E731

    stop = threading.Event()
    worker_done = threading.Event()

    def slave_loop() -> None:
        watch = PrivateChatWatch(sp)
        seen: set[str] = set()
        try:
            while not stop.is_set():
                try:
                    handle_pleave_commands(
                        sp,
                        attach if attach is not None else object(),
                        watch=watch,
                        roster=roster,
                        seen=seen,
                        leave_fn=leave_fn,
                        log=lambda m: print(f"    [副控] {m}"),
                    )
                except Exception as e:
                    print(f"    [副控 err] {e}")
                time.sleep(0.4)
        finally:
            watch.close()
            worker_done.set()

    worker = threading.Thread(target=slave_loop, daemon=True)
    worker.start()
    time.sleep(1.0)
    try:
        res = request_slaves_leave(
            mp,
            [{"name": slave["name"], "obj_id": slave["rid"]}],
            timeout_s=12.0,
            log=lambda m: print(f"    [主控] {m}"),
        )
        entry = res.get(slave["name"]) or {}
        report("2", "PLEAVE 已发送", bool(entry.get("sent")))
        report("2", "PLEFT 回执", bool(entry.get("replied")))
        report("2", "回执 OK=1", bool(entry.get("ok")))
    finally:
        stop.set()
        worker_done.wait(timeout=3.0)
        try:
            if attach is not None and hasattr(attach, "close"):
                attach.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="私聊控制面端到端测试")
    ap.add_argument("--master", type=int, default=0, help="主控 pid（默认自动识别）")
    ap.add_argument("--slave", type=int, default=0, help="副控 pid（默认自动识别）")
    ap.add_argument("--real-leave", action="store_true", help="阶段3真离队（拆队！）")
    ap.add_argument("--stages", default="0,1,2", help="要跑的阶段，如 0,1,2,3")
    args = ap.parse_args()
    stages = {int(s) for s in str(args.stages).split(",") if s.strip().isdigit()}

    games = discover_games()
    roster = load_roster()
    names = rid_by_name(roster)
    name_by_pid = {pid: nm for pid, nm in games.items() if nm in names}
    pid_by_name = {nm: pid for pid, nm in name_by_pid.items()}

    master_pid = args.master or next(
        (pid_by_name[n] for n, pid in pid_by_name.items() if control_role(names.get(n, 0)) == "master"),
        0,
    )
    slave_pid = args.slave or next(
        (pid_by_name[n] for n, pid in pid_by_name.items() if pid != master_pid),
        0,
    )
    if not master_pid or not slave_pid:
        print("无法确定主/副控，请用 --master <pid> --slave <pid> 指定")
        print(f"在线: {games}")
        return 2

    master = {
        "pid": master_pid,
        "name": games[master_pid],
        "rid": names.get(games[master_pid], 0),
    }
    slave = {
        "pid": slave_pid,
        "name": games[slave_pid],
        "rid": names.get(games[slave_pid], 0),
    }
    if not master["rid"] or not slave["rid"]:
        print(f"花名册缺 rid: {master} {slave}")
        return 2

    # 身份预置：发送不再触发 CRT（场景门友好）
    for who in (master, slave):
        cache_role_identity(who["pid"], who["rid"])
        cache_role_name(who["pid"], who["name"])

    print(f"主控: {master['name']} pid={master_pid} rid=0x{master['rid']:X}")
    print(f"副控: {slave['name']} pid={slave_pid} rid=0x{slave['rid']:X}")
    print()

    if 0 in stages:
        stage0_env(games, roster)
        print()
    if 1 in stages:
        stage1_roundtrip(master, slave)
        print()
    if 2 in stages:
        stage2_protocol(master, slave, roster, real_leave=False)
        print()
    if 3 in stages and args.real_leave:
        stage2_protocol(master, slave, roster, real_leave=True)
        print()
    elif 3 in stages:
        print("[阶段3] 跳过（需要 --real-leave 显式开启）")
        print()
    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
