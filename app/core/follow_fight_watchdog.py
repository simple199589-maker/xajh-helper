# -*- coding: utf-8 -*-
"""组队副本挂机（跟随打怪）机器卡监测。

与「忽略怪放行看门狗」(dungeon_fight_kick) 是两个独立功能，本模块只做
功能一（组队副本挂机的队长跟随打怪自动能力）的健康诊断：

  - 本模块：内挂状态机（instance_follow_fight）停在 StateAlert 且无选中
    目标、原地静止超过阈值时，判定疑似机器卡，按队长/队员身份给出处置
    提示。只提示，不干预，不依赖忽略怪开关。
  - dungeon_fight_kick：功能二，忽略怪（卡怪）目标提交，仅在忽略怪武装
    且怪在场时出手，平时待机不干扰。

机器卡背景（RE 见 .issues/DUNGEON_FIGHT_CHAIN_20260829.md）：队长身份
开挂时跟随快照自引用可致状态机卡在 StateAlert——开关挂机、过图、
StartAutoPlay 重跑均无法清除（StartAutoPlay 在 running 态为空操作
ret=0x7B8001 且机器不动），唯一解法 = 手动"移交队长→以队员身份重开
挂机→拿回队长"。队员窗卡死无此粘滞，关闭重开本窗挂机即可。

判定收紧（2026-08-30 实测）：StateAlert 亦可能是战斗中的正常驻留态，
仅凭状态位会让非队长窗口也频繁误报；故要求同时满足"无选中目标 ∧
原地静止 ≥ ALERT_STILL_S"才算疑似机器卡。

@author by ak
"""
from __future__ import annotations

import math
import threading
import time

from app.core.activity_auto import resolve_cec_autoplay_rpm, resolve_host_base_rpm
# RPM 原语单源复用忽略怪看门狗的实现（读内存 helpers 勿两处维护）。
from app.core.dungeon_fight_kick import (
    _host_pos,
    _rpm_u32,
    note_to_live,
    read_selected_target,
    resolve_host_rpm,
)

# StateAlert@instance_follow_fight（note 地址，module_base 相对）。
_NOTE_STATE_ALERT = 0x12BEE04

# 触发参数
ALERT_STILL_S = 10.0  # 无目标 ∧ 静止 持续时长（达到才判疑似机器卡）
ALERT_THROTTLE_S = 60.0  # 告警节流
MOVE_EPS_M = 0.5  # 位移复位阈值（与放行看门狗一致）

# 分支2：Alert ∧ 已握目标 ∧ 静止 → 驱动攻击组件接战（2026-08-30）。
# StateAttack 态每帧做的攻击驱动（0xC53BC0 → 攻击组件，桥接 UI 线程命令
# CMD_AUTOPLAY_DRIVE_ATTACK），Alert 态缺失该驱动 → 攻击系统空转、目标
# 握着不打。全队无技能 + 丸子输出编队下，机器回 Attack 即可恢复输出。
DRIVE_STILL_S = 8.0
DRIVE_ROUNDS = 4
DRIVE_GAP_S = 0.25
DRIVE_COOLDOWN_S = 30.0

_state_lock = threading.Lock()
_state: dict[int, dict] = {}


def reset_state(pid: int) -> None:
    """挂机开始/停止/切图时复位（状态可自愈，调用可选）。@author by ak"""
    with _state_lock:
        _state.pop(int(pid or 0), None)


def _emit(log, msg: str) -> None:
    if log is not None:
        try:
            log(msg)
        except Exception:
            pass
    else:
        print(f"follow fight watchdog: {msg}")


def _host_is_leader(session) -> bool:
    """本机角色是否队长（读取失败按队长处理，不丢队长处置提示）。"""
    try:
        from app.core.team_ops import list_party_members

        for member in list_party_members(
            session, fresh=True, log=lambda _m: None
        ) or []:
            if bool(member.get("is_self")):
                return bool(member.get("is_leader"))
    except Exception:
        pass
    return True


def watch_follow_fight_stuck(session, *, log=None) -> None:
    """挂机守护 tick 入口：跟随打怪状态机机器卡诊断（只提示，不干预）。

    低频廉价；任何异常都不影响守护主流程。独立于忽略怪开关——无论
    「忽略副本卡怪」是否勾选，副本挂机期间始终工作。
    @author by ak
    """
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        return
    module_base = int(getattr(session, "module_base", 0) or 0)
    if not module_base:
        return
    # 内挂未开启时零动作（自动副本关内挂等弹出/手动停挂等阶段让位）。
    try:
        mem = resolve_cec_autoplay_rpm(session)
    except Exception:
        return
    if mem.get("running") is not True:
        reset_state(pid)
        return
    ap = int(mem.get("autoplay") or 0)
    if not ap:
        reset_state(pid)
        return
    mg = ap + 0x578

    st_ptr = _rpm_u32(session, mg + 0x2C)
    st_vt = _rpm_u32(session, st_ptr) if st_ptr else 0
    alert_vt = note_to_live(module_base, _NOTE_STATE_ALERT)

    try:
        mid = int(resolve_host_base_rpm(session) or 0)
    except Exception:
        return
    if not mid:
        return
    host = resolve_host_rpm(session, mid=mid)
    if not host:
        reset_state(pid)
        return
    sel = read_selected_target(session, host)
    pos = _host_pos(session, host)

    # ---- 分支2：Alert ∧ 已握目标 ∧ 静止 → 驱动攻击组件接战 ----
    # 与下方"无目标机器卡告警"互斥：这里覆盖"目标握着却不打"的子场景。
    drive_fire = False
    if st_vt == alert_vt and sel:
        with _state_lock:
            st = _state.setdefault(pid, {})
            now = time.monotonic()
            dpos = _host_pos(session, host)
            if dpos is None:
                st.pop("drive_since", None)
            else:
                dlast = st.get("drive_last_pos")
                if dlast is not None and math.hypot(
                    dpos[0] - dlast[0], dpos[2] - dlast[2]
                ) > MOVE_EPS_M:
                    st["drive_since"] = None
                st["drive_last_pos"] = dpos
                dsince = st.get("drive_since")
                if dsince is None:
                    st["drive_since"] = now
                elif (
                    now - float(dsince) >= DRIVE_STILL_S
                    and now >= float(st.get("drive_cooldown_until") or 0)
                ):
                    st["drive_cooldown_until"] = now + DRIVE_COOLDOWN_S
                    st.pop("drive_since", None)
                    drive_fire = True

    if drive_fire:
        try:
            from app.core.xajh_bridge import ensure_bridge

            br = ensure_bridge(
                pid,
                log=log,
                inject_if_needed=False,
                hwnd=int(getattr(session, "hwnd", 0) or 0) or None,
            )
        except Exception:
            br = None
        if br is not None:
            hwnd_i = int(getattr(session, "hwnd", 0) or 0) or None
            try:
                for _ in range(DRIVE_ROUNDS):
                    br.autoplay_drive_attack(hwnd=hwnd_i, timeout_ms=1500)
                    time.sleep(DRIVE_GAP_S)
                _emit(log, "跟随看护：Alert 握目标不动，已驱动攻击组件接战×4")
            except Exception as e:
                _emit(log, f"跟随看护：驱动攻击组件失败 {e}")
            finally:
                try:
                    br.close()
                except Exception:
                    pass

    fire = False
    with _state_lock:
        st = _state.setdefault(pid, {})
        now = time.monotonic()
        if sel or pos is None:
            # 有目标（在打）或坐标不可读：静止窗口复位，不构成机器卡。
            st.pop("still_since", None)
            return
        last = st.get("last_pos")
        if last is not None and math.hypot(pos[0] - last[0], pos[2] - last[2]) > MOVE_EPS_M:
            st["still_since"] = None
        st["last_pos"] = pos
        still_since = st.get("still_since")
        if still_since is None:
            st["still_since"] = now
            return
        if now - float(still_since) < ALERT_STILL_S:
            return
        # 静止窗口已满：仅当当前确处于 StateAlert 才告警；
        # 不在 Alert 态时不消耗节流窗口（回到 Alert 即刻可报）。
        if st_vt != alert_vt:
            return
        if now < float(st.get("alert_cooldown_until") or 0):
            return
        st["alert_cooldown_until"] = now + ALERT_THROTTLE_S
        fire = True

    if fire:
        if _host_is_leader(session):
            _emit(
                log,
                "跟随看护：机器卡 StateAlert（队长身份开挂的已知粘滞）——"
                "请手动：移交队长给队员 → 关闭并重开本窗副本挂机 → 拿回队长",
            )
        else:
            _emit(
                log,
                "跟随看护：本窗内挂疑似机器卡（StateAlert·无目标·静止≥10s）"
                "——可关闭并重开本窗挂机；跟随掉队由主控卡队检测自动拉回",
            )
