# -*- coding: utf-8 -*-
"""副本挂机整链检查：静态接线 + 协议一致性逐环验证。

用法: python tools/check_hang_chain.py
覆盖: 开挂链 / 守护 tick / 过图 rearm / 停止恢复 / native 命令 / 版本单源。
输出: 每环 OK/FAIL + 汇总。任何 FAIL = 链路断点。

@author by ak
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[1]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def read(p: str) -> str:
    return (ROOT / p).read_text(encoding="utf-8", errors="replace")


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ---------- 1. 协议单源与一致性 ----------
section("1. 协议一致性")
bp_py = read("app/core/bridge_protocol.py")
bp_h = read("native/xajh_bridge/bridge_protocol.h")
m_py = re.search(r"BRIDGE_BUILD_ID = (\d+)", bp_py)
m_h = re.search(r"#define BRIDGE_BUILD_ID (\d+)u", bp_h)
check("BRIDGE_BUILD_ID 单源一致（py vs h）",
      bool(m_py and m_h and m_py.group(1) == m_h.group(1)),
      f"py={m_py.group(1) if m_py else '?'} h={m_h.group(1) if m_h else '?'}")
check("CMD_AUTOPLAY_DRIVE_ATTACK = 57 两侧注册",
      "AUTOPLAY_DRIVE_ATTACK = 57" in bp_py and "CMD_AUTOPLAY_DRIVE_ATTACK = 57" in bp_h)
check("能力映射注册", "AUTOPLAY_DRIVE_ATTACK: BridgeCapability.AUTOPLAY" in bp_py)
check("动作名注册", 'AUTOPLAY_DRIVE_ATTACK: "autoplay.drive"' in bp_py)

# ---------- 2. 开挂链（_start_hang_unlocked） ----------
section("2. 开挂链")
hs = read("app/core/hang_settings.py")
check("场景门等待先于本地初始化",
      0 < hs.find("wait_pid_scene_stable", hs.find("# 副本模式：封包发送前先本地直调"))
          < hs.find("start_autoplay_force(", hs.find("# 副本模式：封包发送前先本地直调")))
check("本地 StartAutoPlay 初始化已恢复", "start_autoplay_force(\n                session, send_packet=False" in hs
      or "start_autoplay_force(session, send_packet=False" in hs)
check("开启封包 1500 保留", "HANG_START_PACKET = bytes.fromhex(\"1500\")" in hs)
check("seed follow 调用点（hang_settings/activity_auto）",
      "autoplay_seed_follow" in hs or "autoplay_seed_follow" in read("app/core/activity_auto.py"))
check("锚点修复保留", "anchor repaired" in hs)
check("关闭恢复序列（重开再关 ×2）", "重发开启→关闭恢复序列" in hs)

# ---------- 3. 守护 tick 接线 ----------
section("3. 守护 tick 接线")
check("过图 rearm 重初始化（tick 驱动重试）", "scene rearm autoplay re-init" in hs or "scene rearm re-init" in hs)
check("重试上限常量", "_SCENE_REARM_RETRY_MAX" in hs)
check("重试计数定义", "_HANG_GUARD_REARM_RETRY" in hs)
check("换图清零重试计数", hs.count("_HANG_GUARD_REARM_RETRY.pop(pid, None)") >= 2)
check("跟随看护已接线", "watch_follow_fight_stuck" in hs)
check("放行看门狗已接线", "maybe_kick" in hs)

# ---------- 4. 跟随看护（follow_fight_watchdog） ----------
section("4. 跟随看护（follow_fight_watchdog）")
ffw = read("app/core/follow_fight_watchdog.py")
check("分支A：无目标机器卡告警", "机器卡 StateAlert" in ffw or "疑似机器卡" in ffw)
check("分支B：有目标驱动攻击组件", "autoplay_drive_attack" in ffw)
check("驱动上限/冷却常量", "DRIVE_ROUNDS" in ffw and "DRIVE_COOLDOWN_S" in ffw)
check("RPM helpers 单源复用（不自维护）", "from app.core.dungeon_fight_kick import" in ffw)

# ---------- 5. 放行看门狗（dungeon_fight_kick） ----------
section("5. 放行看门狗（dungeon_fight_kick）")
dfk = read("app/core/dungeon_fight_kick.py")
check("名单前置门", "_armed_ignore_tids" in dfk)
check("AOI 哈希遍历（native 同源走法）", "0x01260AF4" in dfk or "CECNPC_VTABLE" in dfk or "_NOTE_CECNPC_VTABLE" in dfk)
check("F 通道点名", "fire_native_pick" in dfk and "0x00C6A2E0" not in dfk)
check("驱动命令复用（不自维护）", "start_autoplay_force" not in dfk.replace(
    "from app.core.activity_auto import", ""))

# ---------- 6. native 命令（dllmain） ----------
section("6. native 命令")
dll = read("native/xajh_bridge/dllmain.cpp")
check("白名单含 DRIVE_ATTACK", "cmd == CMD_AUTOPLAY_DRIVE_ATTACK ||" in dll)
check("驱动 note 常量", "kNoteAutoPlayAttackDrive = 0x00C53BC0u" in dll)
check("DRIVE 处理器（组件判空 + 0xC53BC0 驱动）",
      "AUTOPLAY_DRIVE_ATTACK attack component null" in dll
      and "typedef void(__thiscall* FnDrive)(void*);" in dll)
check("方案1 hook（跟随链自引用修正）", "Hook_ResolveFollowChain" in dll
      and "g_follow_seed_lo" in dll)
check("方案1 hook 安装（seed 时挂载）", "InstallFollowChainHook" in dll
      and "g_follow_seed_lo = lo;" in dll)
check("无版本号副本产物", "xajh_bridge_%BRIDGE_BUILD_ID%.dll" not in read("native/xajh_bridge/build_x86.bat"))

# ---------- 7. 加载/复制链（单一名） ----------
section("7. 加载/复制链（统一 xajh_bridge.dll）")
xj = read("app/core/xajh_bridge.py")
check("加载器只找 xajh_bridge.dll", "xajh_bridge_{int(BRIDGE_BUILD_ID)}.dll" not in xj)
check("stage 只用 xajh_bridge.dll", 'stage / f"xajh_bridge_' not in xj)
inv = read("app/core/native_inventory.py")
check("清单无 stamped", "stamped_bridge_name" not in inv)
bat = read("native/xajh_bridge/build_x86.bat")
check("构建无版本号副本", "xajh_bridge_%BRIDGE_BUILD_ID%.dll" not in bat)

# ---------- 8. 调试红线（文档化确认） ----------
section("8. 编译与测试")
import py_compile
try:
    for f in ("app/core/hang_settings.py", "app/core/dungeon_fight_kick.py",
              "app/core/follow_fight_watchdog.py", "app/core/xajh_bridge.py",
              "app/core/activity_auto.py", "app/core/remote_runtime.py",
              "app/core/bridge_protocol.py", "app/core/native_inventory.py"):
        py_compile.compile(str(ROOT / f), doraise=True)
    check("核心文件编译", True)
except Exception as e:
    check("核心文件编译", False, str(e)[:80])

# ---------- 汇总 ----------
fails = [n for n, ok, _ in results if not ok]
print(f"\n===== 汇总：{len(results) - len(fails)}/{len(results)} 通过 =====")
if fails:
    print("FAIL 项：")
    for n in fails:
        print(f"  - {n}")
    sys.exit(1)
print("整链通畅。")
