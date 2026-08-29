# -*- coding: utf-8 -*-
"""Interactive test: run the AOI team-stuck detection against a live game.

Usage:
    cd d:/work/python/game-get
    python tests/run_team_line_stuck_test.py

Logic:
  1. Track own position — if unchanged for 5s, self is "still"
  2. When self is still, check AOI: any teammate > 15m away?
  3. Both true -> stuck detected -> (would cancel + re-enable follow)
"""
from __future__ import annotations

import math
import os
import sys
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main():
    print("=" * 60)
    print("  AOI 队伍卡线检测 - 在线测试")
    print("  判定: 自己停留>5s + 队友>15m")
    print("=" * 60)

    # 1. Find game PID.
    from app.core.inject_gate import find_xajh_processes

    procs = find_xajh_processes()
    if not procs:
        print("\n未找到 xajh.exe 进程，请确保游戏正在运行。")
        sys.exit(1)
    if len(procs) > 1:
        print(f"\n找到 {len(procs)} 个 xajh.exe 进程:")
        for i, p in enumerate(procs):
            print(f"  [{i}] PID={p['pid']}")
        choice = input("选择序号: ").strip()
        try:
            pid = int(procs[int(choice)]["pid"])
        except Exception:
            print("无效选择。")
            sys.exit(1)
    else:
        pid = int(procs[0]["pid"])
    print(f"\n使用 PID={pid}")

    # 2. Attach.
    from app.core.game_attach import GameAttachSession

    print("[1] 附加游戏进程...")
    session = GameAttachSession(log=lambda m: print(f"  [attach] {m}"))
    try:
        session.attach(pid)
    except Exception as e:
        print(f"  附加失败: {e}")
        sys.exit(1)
    print(f"  附加成功, PID={session.pid}")

    # 2.5. Wait for scene stable.
    from app.core.remote_runtime import wait_scene_ready

    print("\n[1.5] 等待场景稳定...")
    settled = wait_scene_ready(session, timeout_s=15.0, log=lambda m: print(f"  [scene] {m}"))
    if not settled.get("ok"):
        print(f"  场景未稳定: {settled.get('error') or 'timeout'}")
        session.close()
        sys.exit(1)
    print(f"  场景已稳定, scene_id={settled.get('scene_id')}")

    # 3. Read party.
    from app.core.team_ops import list_party_members

    print("\n[2] 读取队伍成员...")
    party = list_party_members(session, fresh=True, log=lambda m: print(f"  [party] {m}"))
    if not party:
        print("  队伍为空或未组队，无法测试。")
        session.close()
        sys.exit(1)
    print(f"  队伍人数: {len(party)}")
    for m in party:
        tag = " (自己)" if m.get("is_self") else ""
        print(f"    {m.get('name', '?')}  pid={m.get('pid')}  obj_id={m.get('obj_id')}{tag}")

    # Build name set (excluding self).
    names: set[str] = set()
    for member in party:
        if member.get("is_self"):
            continue
        try:
            if int(member.get("pid") or 0) == int(pid):
                continue
        except Exception:
            pass
        name = str(member.get("name") or "").strip()
        if name:
            names.add(name)
    print(f"  队友名单: {names}")

    # 4. Poll loop: track own position + AOI check.
    from app.core.plg_objects import CLASS_PLAYER, list_class_objects
    from app.core.live_scene_hub import get_live_scene
    from app.core.automove import read_scene_position

    def _name_key(v: object) -> str:
        return "".join(str(v or "").split()).strip().casefold()

    wanted_keys = {_name_key(n) for n in names}

    print("\n[3] 持续监测 (Ctrl+C 退出)...")
    print(f"    判定: 自己停留>5s + 队友>动态阈值(3人=10m,4人=14m,5人=18m) → 卡队")
    print()

    last_own: tuple[float, float, float] | None = None
    still_since: float | None = None
    last_toggle = 0.0

    try:
        while True:
            # Own position.
            live_state = get_live_scene(pid, max_age_s=10.0)
            own_xyz = tuple(getattr(live_state, "pos", None) or ()) if live_state is not None else ()
            if len(own_xyz) != 3:
                scene_state = read_scene_position(session, log=lambda m: None, timeout_ms=5000)
                own_xyz = tuple(getattr(scene_state, "scene_pos", None) or ())
            if len(own_xyz) != 3:
                print("  [!] 自己位置读取失败")
                time.sleep(1.0)
                continue

            now = time.monotonic()

            # Check own movement.
            moved = False
            if last_own is not None:
                dx = math.hypot(
                    float(own_xyz[0]) - float(last_own[0]),
                    float(own_xyz[2]) - float(last_own[2]),
                )
                if dx > 0.5:
                    moved = True
                    still_since = None
            last_own = own_xyz

            if still_since is None:
                still_since = now

            still_dur = now - still_since

            # AOI scan.
            objects = list_class_objects(
                session, CLASS_PLAYER, radius=60.0, limit=96,
                read_name=True, read_tid=False, log=lambda m: None,
            )
            # 动态阈值：3人=10m, 4人=14m, 5人=18m
            far_threshold = 10.0 + max(0, len(party) - 3) * 4.0
            far_names: list[str] = []
            near_names: list[str] = []
            for obj in (objects or []):
                name = str(getattr(obj, "name", "") or "").strip()
                if not name or _name_key(name) not in wanted_keys:
                    continue
                x = getattr(obj, "x", None)
                z = getattr(obj, "z", None)
                if x is None or z is None:
                    continue
                dist = math.hypot(float(x) - float(own_xyz[0]), float(z) - float(own_xyz[2]))
                if dist > far_threshold:
                    far_names.append(f"{name}({dist:.0f}m)")
                else:
                    near_names.append(f"{name}({dist:.0f}m)")

            status = "移动中" if moved else f"静止 {still_dur:.1f}s"
            far_str = ", ".join(far_names) if far_names else "无"
            near_str = ", ".join(near_names) if near_names else "无"

            stuck = (still_dur >= 5.0 and len(far_names) > 0
                     and (now - last_toggle >= 30.0))

            marker = " <<< 卡队!" if stuck else ""
            print(
                f"  [{status}] 自己=({own_xyz[0]:.0f},{own_xyz[2]:.0f})  "
                f"远(>{far_threshold:.0f}m: {far_str}) 近({near_str}){marker}"
            )

            if stuck:
                last_toggle = now
                still_since = None
                print("  >>> 触发卡队处理：取消跟随 ...")
                from app.core.team_ops import set_team_follow

                r = set_team_follow(session, enabled=False, log=lambda m: print(f"       {m}"))
                if r.ok:
                    print("      已取消，等 1s ...")
                    time.sleep(1.0)
                    r2 = set_team_follow(session, enabled=True, log=lambda m: print(f"       {m}"))
                    print(f"      重开{'成功' if r2.ok else '失败'}: {r2.message}")
                else:
                    print(f"      取消失败: {r.message}")

            time.sleep(1.0)

    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        session.close()


if __name__ == "__main__":
    main()