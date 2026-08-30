# -*- coding: utf-8 -*-
"""副本模式"忽略怪放行"看门狗（卡怪专用，平时不干扰）。

职责边界：只服务「忽略副本卡怪」功能——把游戏原生自动选怪拒绝的卡怪
（开局卡怪：北疆疯丐/上官霸刀等；以及角色忽略名单里的本，如沙漠古镇/
梅庄外围最终 boss）提交为当前目标，解决"到了怪面前不打怪"。平时
（忽略怪不在场）看门狗待机，绝不抢目标、不干扰正常挂机与跟随。

启动前置条件（两者同时满足才出手）：
  1. 「忽略副本卡怪」已勾选且原生守卫武装成功（pid 在武装注册表内，
     名单非空）——挂机关闭/非副本模式/未勾选/武装失败一律待机；
  2. AOI 内确实存在名单中的忽略怪（≤30m）——忽略怪不在副本/不在附近
     就不出手。

原生提交入口（RE 见 .issues/DUNGEON_FIGHT_CHAIN_20260829.md）：

    mid  = resolve_host_base_rpm()        # *(*(module+0x15282D8)+0x24) = CECGameRun
    host = *[mid + 0x8C]                  # CECHostPlayer
    hdl  = *[host + 0x478]                # CECHostNetCmdHdl
    F(0x754280) thiscall(hdl, pair*, 0, 0)   # pair = {id_lo, id_hi}

约束:
- 纯 RPM 读（AOI 哈希遍历与 native IsRejectedDungeonTargetCandidate 同源），
  不依赖 CRT/scene-settle 门；
- 仅在 (副本挂机中) ∧ (忽略怪在场) ∧ (无选中目标) ∧ (站街 ≥ STILL_S) ∧
  (冷却外) 触发；
- 只提交名单内忽略怪，远于 KICK_RANGE_M 的不放行（与 native 30m 规则一致）；
- 同一候选连续提交未生效（尸体/无效目标）自动拉黑改选次近忽略怪。

@author by ak
"""
from __future__ import annotations

import math
import struct
import threading
import time

from app.core.activity_auto import resolve_host_base_rpm
from app.core.plg_exports import DEFAULT_IMAGE_BASE

_NOTE_BASE = DEFAULT_IMAGE_BASE

_NOTE_F_DISPATCH = 0x754280
_NOTE_CECNPC_VTABLE = 0x1260AF4

_MID_HOST_OFF = 0x8C  # CECGameRun -> CECHostPlayer
_MID_SCENE_OFF = 0x0C  # CECGameRun -> 场景对象
_SCENE_AOI_MGR_OFF = 0x74
_AOI_COUNT_OFF = 0x18
_AOI_BUCKETS_OFF = 0x1C
_AOI_BUCKET_COUNT_OFF = 0x28
_HOST_NET_CMD_HDL_OFF = 0x478  # host -> CECHostNetCmdHdl（submit 的 this）
_HOST_SEL_TARGET_OFF = 0x19E8  # host -> 当前选中目标 id64（lo/hi）
_HOST_POS_X_OFF = 0x158
_OBJ_POS_X_OFF = 0x158
_OBJ_ID_LO_OFF = 0x140
_OBJ_TID_OFF = 0x4F8
_LIST_NODE_OBJ_OFF = 4

# 触发参数
KICK_RANGE_M = 30.0
STILL_S = 10.0
MOVE_EPS_M = 0.5
KICK_COOLDOWN_S = 12.0
EMPTY_COOLDOWN_S = 5.0
MAX_AOI_NODES = 512

# 候选拉黑：同一目标连续提交 N 次仍未被选中（尸体/无效候选，原生 ret=1 但
# 选中不生效），暂停放行一段时间，让看门狗改选次近的有效目标。
# 实测症状（2026-08-30 蝎王魔窟）：TID=0x8881 每 4~8s 反复提交 6+ 次全部
# 无"目标已选中"，全队站街到回城。
KICK_FAIL_BAN_AFTER = 2
KICK_FAIL_BAN_S = 120.0

# Alert 粘滞重初始化
_NOTE_STATE_ALERT = 0x12BEE04  # StateAlert@instance_follow_fight
ALERT_REINIT_AFTER_S = 10.0
REINIT_COOLDOWN_S = 60.0

_state_lock = threading.Lock()
_state: dict[int, dict] = {}

# 待机提示节流（前置门不满足时避免每 tick 刷屏）。
_gate_note_lock = threading.Lock()
_gate_note: dict[int, dict[str, float]] = {}
_GATE_LOG_THROTTLE_S = 120.0


def _gate_idle_log(pid: int, log, msg: str) -> None:
    """待机原因节流日志：同一 pid 同一原因最多每 120s 记一条。@author by ak"""
    now = time.monotonic()
    with _gate_note_lock:
        per = _gate_note.setdefault(int(pid or 0), {})
        if now - float(per.get(msg, 0.0)) < _GATE_LOG_THROTTLE_S:
            return
        per[msg] = now
    _emit(log, msg)


def _armed_ignore_tids(pid: int) -> tuple[int, ...]:
    """前置门：pid 的忽略怪 TID 名单（未开启忽略副本卡怪/未武装 → 空）。"""
    try:
        from app.core.hang_settings import get_dungeon_guard_ignore_tids

        return get_dungeon_guard_ignore_tids(pid)
    except Exception:
        return ()


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


def _log_msg(msg: str) -> None:
    print(f"dungeon fight kick: {msg}")


def note_to_live(module_base: int, note: int) -> int:
    """note 地址 → 实际运行地址。@author by ak"""
    return int(module_base) + (int(note) - _NOTE_BASE)


def _rpm_u32(session, addr: int) -> int:
    try:
        raw = session.pm.read_bytes(int(addr), 4)
        return int.from_bytes(raw, "little")
    except Exception:
        return 0


def _rpm_f32(session, addr: int) -> float:
    try:
        raw = session.pm.read_bytes(int(addr), 4)
        return struct.unpack_from("<f", raw)[0]
    except Exception:
        return float("nan")


def reset_state(pid: int) -> None:
    """挂机开始/停止/切图时复位站街窗口。@author by ak"""
    with _state_lock:
        _state.pop(int(pid or 0), None)
    with _gate_note_lock:
        _gate_note.pop(int(pid or 0), None)


def resolve_host_rpm(session, mid: int | None = None) -> int:
    """纯 RPM 解析 CECHostPlayer 指针，失败返回 0。@author by ak"""
    if not mid:
        try:
            mid = int(resolve_host_base_rpm(session) or 0)
        except Exception:
            return 0
    if not mid:
        return 0
    host = _rpm_u32(session, int(mid) + _MID_HOST_OFF)
    return int(host) if host else 0


def read_selected_target(session, host: int) -> int:
    """读 host 当前选中目标 id64，无目标返回 0。@author by ak"""
    lo = _rpm_u32(session, int(host) + _HOST_SEL_TARGET_OFF)
    hi = _rpm_u32(session, int(host) + _HOST_SEL_TARGET_OFF + 4)
    return ((int(hi) & 0xFFFFFFFF) << 32) | (int(lo) & 0xFFFFFFFF)


def walk_aoi_monsters(session, host: int, mid: int, module_base: int) -> list[dict]:
    """遍历 AOI 哈希表返回 CECNPC 怪物列表（含水平距离）。

    与 native IsRejectedDungeonTargetCandidate 的走法一致；任何一步读不
    到都视为空场景（fail open，绝不禁用正常攻击）。@author by ak
    """
    out: list[dict] = []
    if not mid:
        return out
    scene = _rpm_u32(session, int(mid) + _MID_SCENE_OFF)
    if not scene:
        return out
    mgr = _rpm_u32(session, scene + _SCENE_AOI_MGR_OFF)
    if not mgr:
        return out
    count = _rpm_u32(session, mgr + _AOI_COUNT_OFF)
    buckets = _rpm_u32(session, mgr + _AOI_BUCKETS_OFF)
    bucket_count = _rpm_u32(session, mgr + _AOI_BUCKET_COUNT_OFF)
    if not count or count > MAX_AOI_NODES or not buckets or not bucket_count \
            or bucket_count > 4096:
        return out
    hx = _rpm_f32(session, int(host) + _HOST_POS_X_OFF)
    hy = _rpm_f32(session, int(host) + _HOST_POS_X_OFF + 4)
    hz = _rpm_f32(session, int(host) + _HOST_POS_X_OFF + 8)
    if not all(math.isfinite(v) for v in (hx, hy, hz)):
        return out
    npc_vt = note_to_live(module_base, _NOTE_CECNPC_VTABLE)
    node_limit = count + 32
    visited = 0
    for i in range(bucket_count):
        node = _rpm_u32(session, buckets + i * 4)
        while node and visited < node_limit:
            visited += 1
            nxt = _rpm_u32(session, node)
            obj = _rpm_u32(session, node + _LIST_NODE_OBJ_OFF)
            node = nxt
            if not obj or _rpm_u32(session, obj) != npc_vt:
                continue
            lo = _rpm_u32(session, obj + _OBJ_ID_LO_OFF)
            hi = _rpm_u32(session, obj + _OBJ_ID_LO_OFF + 4)
            ox = _rpm_f32(session, obj + _OBJ_POS_X_OFF)
            oy = _rpm_f32(session, obj + _OBJ_POS_X_OFF + 4)
            oz = _rpm_f32(session, obj + _OBJ_POS_X_OFF + 8)
            if not all(math.isfinite(v) for v in (ox, oy, oz)):
                continue
            out.append(
                {
                    "id64": ((int(hi) & 0xFFFFFFFF) << 32) | (int(lo) & 0xFFFFFFFF),
                    "tid": _rpm_u32(session, obj + _OBJ_TID_OFF),
                    "x": ox,
                    "y": oy,
                    "z": oz,
                    "dist": math.hypot(ox - hx, oz - hz),
                }
            )
        if visited >= node_limit:
            break
    return out


def select_kick_candidate(
    monsters: list[dict],
    *,
    range_m: float = KICK_RANGE_M,
    want_tids: set[int] | None = None,
    exclude: set[int] | None = None,
) -> dict | None:
    """从 AOI 怪物中选放行候选：水平距离最近且 ≤ range_m。

    纯决策函数，可单测。远怪一律不放行（与 native 30m 规则一致）。
    want_tids 给定时只考虑名单内忽略怪（忽略怪专用前置）；exclude 中的
    id64 跳过（提交过但从未选中的无效候选）。
    @author by ak
    """
    best: dict | None = None
    for m in monsters or []:
        try:
            dist = float(m.get("dist", -1.0))
        except Exception:
            continue
        if dist < 0 or dist > float(range_m):
            continue
        if int(m.get("id64") or 0) == 0:
            continue
        if want_tids and int(m.get("tid") or 0) not in want_tids:
            continue
        if exclude and int(m.get("id64") or 0) in exclude:
            continue
        if best is None or dist < float(best["dist"]):
            best = m
    return best


def _host_pos(session, host: int) -> tuple[float, float, float] | None:
    x = _rpm_f32(session, int(host) + _HOST_POS_X_OFF)
    y = _rpm_f32(session, int(host) + _HOST_POS_X_OFF + 4)
    z = _rpm_f32(session, int(host) + _HOST_POS_X_OFF + 8)
    if not all(math.isfinite(v) for v in (x, y, z)):
        return None
    return (x, y, z)


def maybe_kick(session, *, log=None, still_s: float = STILL_S) -> None:
    """挂机守护 tick 入口：忽略怪放行（卡怪专用，平时待机不干扰）。

    前置条件见模块 docstring：忽略怪已武装 ∧ AOI 内有名单忽略怪才出手。
    StateAlert 粘滞告警在门之前，无条件保留。低频廉价；任何异常都不影响
    守护主流程。
    @author by ak
    """
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        return
    module_base = int(getattr(session, "module_base", 0) or 0)
    if not module_base:
        return
    # 内挂未开启时不放行（自动副本关内挂等弹出的尾巴、手动停挂等阶段让位）。
    try:
        from app.core.activity_auto import resolve_cec_autoplay_rpm

        mem = resolve_cec_autoplay_rpm(session)
    except Exception:
        return
    if mem.get("running") is not True:
        reset_state(pid)
        return
    ap = int(mem.get("autoplay") or 0)
    if not ap:
        return
    mg = ap + 0x578

    # StateAlert 原始状态（廉价 RPM 读）。
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

    alert_fire = False
    with _state_lock:
        st = _state.setdefault(pid, {})
        now = time.monotonic()
        if sel:
            # 有目标（含刚放行成功的）：窗口复位，并清除该候选的失败计数。
            st.pop("still_since", None)
            if st.pop("pending", None):
                _emit(log, "副本放行：目标已选中，交由内挂攻击")
            fails = st.get("cand_fails")
            if isinstance(fails, dict):
                fails.pop(sel, None)
            st["last_cand_id64"] = None
            st["cooldown_until"] = now + KICK_COOLDOWN_S
            return
        if pos is None:
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
        still_dur = now - float(still_since)

        # ---- StateAlert 机器卡告警（诊断，不受忽略怪前置门影响）----
        # 判定收紧：StateAlert ∧ 无选中目标 ∧ 站街≥10s。StateAlert 亦可能
        # 是战斗中的正常驻留态，旧逻辑仅按状态位判断，导致非队长窗口也
        # 频繁弹"移交队长"误报（2026-08-30 实测）。按身份给处置提示：
        #   队长窗 = 已知粘滞，仅手动移交队长可解；
        #   队员窗 = 关闭重开本窗挂机即可，掉队由主控卡队检测拉回。
        alert_fire = (
            st_vt == alert_vt
            and still_dur >= ALERT_REINIT_AFTER_S
            and now >= float(st.get("reinit_cooldown_until") or 0)
        )
        if alert_fire:
            st["reinit_cooldown_until"] = now + REINIT_COOLDOWN_S

        if still_dur < float(still_s):
            return
        if now < float(st.get("cooldown_until") or 0):
            return
        # 占位冷却，避免 AOI 遍历每 tick 都跑；提交成功后由 sel 分支续期。
        st["cooldown_until"] = now + EMPTY_COOLDOWN_S

    if alert_fire:
        if _host_is_leader(session):
            _emit(
                log,
                "副本看门狗：机器卡 StateAlert（队长身份开挂的已知粘滞）——"
                "请手动：移交队长给队员 → 关闭并重开本窗副本挂机 → 拿回队长",
            )
        else:
            _emit(
                log,
                "副本看门狗：本窗内挂疑似机器卡（StateAlert·无目标·静止≥10s）"
                "——可关闭并重开本窗挂机；跟随掉队由主控卡队检测自动拉回",
            )

    # ---- 启动前置条件：忽略怪功能未开启/未武装 → 放行待机，不干扰 ----
    # 注意：必须放在 StateAlert 告警之后——机器卡告警不受此门影响。
    # 名单来自「忽略副本卡怪」武装时同步的 packaged + 角色忽略 TID。
    want_tids = _armed_ignore_tids(pid)
    if not want_tids:
        _gate_idle_log(pid, log, "副本放行：忽略怪功能未开启，看门狗待机")
        return
    want_set = {int(t) & 0xFFFFFFFF for t in want_tids}

    monsters = walk_aoi_monsters(session, host, mid, module_base)
    # 拉黑过期清理：同目标反复提交却从未选中的候选（尸体/无效目标）跳过。
    bans = st.get("cand_bans")
    if isinstance(bans, dict) and bans:
        now_ban = time.monotonic()
        active_bans = {k for k, until in bans.items() if float(until) > now_ban}
        st["cand_bans"] = {k: until for k, until in bans.items() if float(until) > now_ban}
    else:
        active_bans = set()
    # 只在忽略怪面前出手：候选限定为名单内 TID（忽略怪不在 AOI → 待机）。
    cand = select_kick_candidate(
        monsters, want_tids=want_set, exclude=active_bans
    )
    if cand is None:
        _gate_idle_log(pid, log, "副本放行：AOI 内无忽略怪，待机")
        return
    cand_id = int(cand["id64"])
    if st.get("last_cand_id64") == cand_id:
        # 上一次提交的就是它且至今未被选中：累计失败，达阈值拉黑改选次近目标。
        fails = st.setdefault("cand_fails", {})
        fail_n = int(fails.get(cand_id, 0)) + 1
        fails[cand_id] = fail_n
        if fail_n >= KICK_FAIL_BAN_AFTER:
            st.setdefault("cand_bans", {})[cand_id] = time.monotonic() + KICK_FAIL_BAN_S
            fails.pop(cand_id, None)
            st["last_cand_id64"] = None
            st.pop("pending", None)
            _emit(
                log,
                f"副本放行：候选 0x{cand_id:X} 连续{fail_n}次提交未生效"
                f"（疑似尸体/无效目标），暂停放行 {KICK_FAIL_BAN_S:.0f}s",
            )
            return
    st["last_cand_id64"] = cand_id
    fire_native_pick(session, module_base, host, cand, log=log)


def _emit(log, msg: str) -> None:
    if log is not None:
        try:
            log(msg)
        except Exception:
            pass
    else:
        _log_msg(msg)


def fire_native_pick(
    session, module_base: int, host: int, cand: dict, *, log=None
) -> bool:
    """经 CECHostNetCmdHdl 原生提交入口放行目标。成功返回 True。@author by ak"""
    from app.core import remote_runtime as rr

    pid = int(getattr(session, "pid", 0) or 0)
    hdl = _rpm_u32(session, int(host) + _HOST_NET_CMD_HDL_OFF)
    if not hdl:
        _emit(log, "副本放行：NetCmdHdl 未解析，跳过")
        return False
    f_va = note_to_live(module_base, _NOTE_F_DISPATCH)
    id64 = int(cand["id64"])
    lo = id64 & 0xFFFFFFFF
    hi = (id64 >> 32) & 0xFFFFFFFF
    try:
        handle = rr.open_process(pid)
    except OSError as e:
        _emit(log, f"副本放行：打开进程失败 {e}")
        return False
    remote = 0
    try:
        # 远端调用走 scene-settle 门；新进程/刚过图后稳定窗未满时先等门。
        from app.core.remote_runtime import wait_pid_scene_stable

        wait_pid_scene_stable(pid, timeout_s=4.0)
        remote = rr.remote_alloc(handle, 16)
        rr.write_process(handle, remote, struct.pack("<II", lo, hi))
        ret = rr.remote_call_thiscall_x86(
            pid, f_va, hdl, [remote, 0, 0], timeout_ms=4000
        )
        with _state_lock:
            st = _state.setdefault(pid, {})
            st["pending"] = True
            st["cooldown_until"] = time.monotonic() + KICK_COOLDOWN_S
        _emit(
            log,
            f"副本放行：原生提交 TID=0x{int(cand.get('tid') or 0):X} "
            f"距离={float(cand['dist']):.1f}m ret=0x{int(ret) & 0xFFFFFFFF:08X}",
        )
        return True
    except Exception as e:
        _emit(log, f"副本放行：原生提交失败 {e}")
        return False
    finally:
        if remote:
            try:
                rr.remote_free(handle, remote)
            except Exception:
                pass
        try:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        except Exception:
            pass
