# -*- coding: utf-8 -*-
"""
One-click login orchestration for the XAJH helper.

Ties together the verified login chain with the account settings so a single
call can: start the launcher (dismissing any update prompt), wait for the
server-select page, confirm the partition, fill account/password, submit,
choose a role, enter the world, then apply that role's hang / master-slave
settings.

All stages report progress through a ``progress`` callback (used by the UI to
show intermediate state) and any failure returns a structured dict with a
machine-readable ``error`` code plus a human ``message``.

@author by ak
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

LogFn = Callable[[str], None]
ProgressFn = Callable[[str, dict], None]

# Error codes surfaced to the caller.
ERR_LAUNCHER_MISSING = "launcher_missing"
ERR_LAUNCHER_START = "launcher_start_failed"
ERR_LAUNCHER_UPDATE = "launcher_update_blocked"
ERR_NO_GAME_HWND = "no_game_hwnd"
ERR_SERVER_CONFIRM = "server_confirm_failed"
ERR_LOGIN = "login_failed"
ERR_LOGIN_TIMEOUT = "login_timeout"
ERR_ROLE_ENTER = "role_enter_failed"
ERR_NO_CHARACTER = "no_character"
ERR_NO_ROLE_CFG = "role_config_missing"
ERR_APPLY = "apply_failed"

# Progress stage names (UI shows these).
P_LAUNCHER = "launcher"
P_UPDATE = "launcher_update"
P_SERVER = "server_select"
P_CREDENTIALS = "credentials"
P_CHAR_SELECT = "char_select"
P_ENTER_WORLD = "enter_world"
P_APPLY = "apply_settings"
P_DONE = "done"


@dataclass
class LoginProgress:
    """Accumulated progress + last error for one orchestration run. @author by ak"""

    stage: str = P_LAUNCHER
    message: str = ""
    extra: dict = field(default_factory=dict)
    error: str | None = None
    result: dict = field(default_factory=dict)

    def emit(self, fn: ProgressFn | None, stage: str, message: str = "", **extra) -> None:
        self.stage = stage
        self.message = message
        self.extra = dict(extra)
        if fn is not None:
            fn(stage, {"message": message, **extra})


def _default_log(m: str) -> None:
    print("[login]", m)


def start_launcher_and_wait_server(
    account: str,
    password: str,
    *,
    slot: int = 1,
    enter_world: bool = True,
    launcher_path: str = "",
    update_timeout_s: float = 8.0,
    poll_s: float = 1.5,
    settle_s: float = 4.0,
    xajh_timeout_s: float = 120.0,
    server_confirm_settle_s: float = 2.5,
    progress: ProgressFn | None = None,
    log: LogFn | None = None,
    on_before_click: Callable[[int, int, int], None] | None = None,
    stop_event=None,
) -> dict:
    """Drive the full login: launcher -> update dismiss -> server confirm ->
    credentials -> submit -> char select -> enter world -> apply settings.

    ``slot`` selects which role card (1/2/3) enters the world on char select.
    Account hang / master-slave settings are applied after the world loads.
    ``stop_event`` (threading.Event) aborts the flow promptly at the next
    stage / poll boundary, returning ``{"ok": False, "error": "cancelled"}``.

    Returns a structured dict; see module error constants for ``error`` codes.

    @author by ak
    """
    import ctypes
    import ctypes.wintypes as wt

    log = log or _default_log
    prog = LoginProgress()
    u32 = ctypes.WinDLL("user32", use_last_error=True)

    def _stopped() -> bool:
        try:
            return bool(stop_event is not None and stop_event.is_set())
        except Exception:
            return False

    def _cancel_out(msg: str) -> dict:
        out0 = {
            "ok": False,
            "stage": prog.stage,
            "error": "cancelled",
            "message": msg or "已停止",
            "pid": 0,
            "hwnd": 0,
            "role_id": "",
            "role_name": "",
            "char_select_roles": None,
            "login": None,
            "enter_world": None,
            "apply": None,
        }
        prog.emit(progress, prog.stage, out0["message"], error=out0["error"])
        return out0

    from app.core import account_manager as am
    from app.core.login_bridge import (
        LoginStage,
        auto_login_flow,
        drive_launcher_to_xajh,
        enter_world_from_char_select,
        inject_login_bridge,
        probe_login_stage,
        read_char_select_roles,
    )

    out = {
        "ok": False,
        "stage": prog.stage,
        "error": None,
        "message": "",
        "pid": 0,
        "hwnd": 0,
        "role_id": "",
        "role_name": "",
        "char_select_roles": None,
        "login": None,
        "enter_world": None,
        "apply": None,
    }

    # ---- 1) launcher (reuse running, else start) + drive to xajh ----
    launcher = str(launcher_path or "").strip() or am.remembered_launcher()
    if not launcher:
        out["error"] = ERR_LAUNCHER_MISSING
        out["message"] = "未设置登录器路径，请先在账号管理选择"
        prog.emit(progress, P_LAUNCHER, out["message"], error=out["error"])
        return out
    prog.emit(progress, P_LAUNCHER, "准备登录器…", launcher=launcher)
    if _stopped():
        return _cancel_out("已停止")
    drive = drive_launcher_to_xajh(
        launcher,
        update_timeout_s=update_timeout_s,
        poll_s=poll_s,
        settle_s=settle_s,
        xajh_timeout_s=xajh_timeout_s,
        log=log,
        stop_event=stop_event,
    )
    if _stopped():
        return _cancel_out("已停止")
    if not drive.get("ok"):
        err = str(drive.get("error") or "launcher_drive_failed")
        if err in ("launcher_update_blocked", "launcher_no_main", "launcher_no_start"):
            out["error"] = ERR_LAUNCHER_UPDATE
        elif err == "launcher_missing":
            out["error"] = ERR_LAUNCHER_MISSING
        elif err == "launcher_start_failed":
            out["error"] = ERR_LAUNCHER_START
        else:
            out["error"] = ERR_NO_GAME_HWND
        out["message"] = str(drive.get("message") or "登录器驱动失败")
        prog.emit(progress, P_UPDATE if out["error"] == ERR_LAUNCHER_UPDATE else P_SERVER,
                  out["message"], error=out["error"], drive=drive)
        return out
    pid = int(drive.get("xajh_pid") or 0)
    hwnd = int(drive.get("hwnd") or 0)
    out["pid"] = pid
    out["hwnd"] = hwnd
    prog.emit(progress, P_SERVER, f"游戏窗口已出现 pid={pid}")

    # keep window on-screen but do NOT resize
    u32.ShowWindow(wt.HWND(hwnd), 9)
    time.sleep(0.4)

    # Login click coordinates assume a 16:9 client: silently snap a non-16:9
    # window to its nearest 16:9 size (best-effort, never fatal). @author by ak
    try:
        from app.core.window_layout import normalize_game_window_to_nearest_16x9

        snap = normalize_game_window_to_nearest_16x9(hwnd, pid, log=log)
        if snap.ok and snap.changed:
            prog.emit(
                progress,
                P_SERVER,
                "窗口已就近调整为 "
                f"{snap.target_client[0]}x{snap.target_client[1]}（16:9）",
            )
        elif not snap.ok:
            log(f"nearest 16:9 snap skipped: {snap.reason}")
    except Exception as snap_err:  # pragma: no cover
        log(f"nearest 16:9 snap err: {snap_err}")

    # inject login bridge (needed for background clicks)
    prog.emit(progress, P_SERVER, "注入登录桥…")
    if not inject_login_bridge(pid, hwnd=hwnd, log=log):
        out["error"] = ERR_LOGIN
        out["message"] = "登录桥注入失败"
        prog.emit(progress, P_SERVER, out["message"], error=out["error"])
        return out

    from app.core.game_attach import GameAttachSession

    session = GameAttachSession(log=log)
    try:
        session.attach(pid)
        session.hwnd = hwnd

        # Warm the login-page manager cache now (first locate is ~1.3s but
        # happens during the launcher/game loading window; subsequent probes
        # resolve the current page via the mgr->child->page chain in <1ms).
        prog.emit(progress, P_SERVER, "初始化登录状态…")
        try:
            from app.core import login_bridge as _lb

            _lb.probe_login_stage(session, log=log)
        except Exception:  # pragma: no cover
            pass

        # ---- 4) auto login: server confirm + credentials + submit ----
        prog.emit(progress, P_CREDENTIALS, "执行自动登录…")
        login = auto_login_flow(
            session,
            account=account,
            password=password,
            main_hwnd=hwnd,
            server_confirm=True,
            submit_timeout_s=35.0,
            poll_s=poll_s,
            server_confirm_settle_s=server_confirm_settle_s,
            log=log,
            on_before_click=on_before_click,
            stop_event=stop_event,
        )
        out["login"] = login
        if _stopped():
            return _cancel_out("已停止")
        if not login.get("ok"):
            st = str(login.get("stage") or "")
            if st == LoginStage.CREDENTIALS.value:
                out["error"] = ERR_LOGIN
                out["message"] = "登录失败（账号/密码错误或网络问题）"
            else:
                out["error"] = ERR_LOGIN_TIMEOUT
                out["message"] = f"登录超时/异常: {login.get('error')}"
            out["stage"] = st
            prog.emit(progress, P_CREDENTIALS, out["message"], error=out["error"], login=login)
            return out
        prog.emit(progress, P_CHAR_SELECT, "已进入选角页", login=login)

        # ---- 4.5) read the role cards on char-select BEFORE entering the
        # world; the data is returned to the caller for UI display.
        char_roles = {"ok": False, "roles": [], "error": None}
        try:
            char_roles = read_char_select_roles(session, log=log)
        except Exception as e:  # pragma: no cover
            char_roles = {"ok": False, "roles": [], "error": str(e)}
        out["char_select_roles"] = char_roles
        if char_roles.get("ok"):
            prog.emit(progress, P_CHAR_SELECT,
                      "已读取选角列表", roles=char_roles.get("roles"))

        # ---- 5) enter world for the requested slot. When the account slot is
        # 未启用 (enter_world=False), stop on the character-select page instead
        # of picking a role arbitrarily. @author by ak
        if not enter_world:
            out["ok"] = True
            out["error"] = None
            out["message"] = "已停留在选角页"
            out["stage"] = LoginStage.CHARACTER_SELECT.value
            prog.emit(progress, P_CHAR_SELECT, out["message"],
                      role_id="", role_name="")
            return out
        prog.emit(progress, P_ENTER_WORLD, f"进入角色槽位 {slot}…")
        if _stopped():
            return _cancel_out("已停止")
        ew = enter_world_from_char_select(session, main_hwnd=hwnd, slot=slot, log=log,
                                          on_before_click=on_before_click,
                                          stop_event=stop_event)
        out["enter_world"] = ew
        if _stopped():
            return _cancel_out("已停止")
        if not ew.get("ok"):
            if str(ew.get("error")) == "no_character":
                out["error"] = ERR_NO_CHARACTER
                out["message"] = f"角色槽位 {slot} 无角色"
            else:
                out["error"] = ERR_ROLE_ENTER
                out["message"] = f"进世界失败: {ew.get('error')}"
            out["stage"] = str(ew.get("stage") or "")
            prog.emit(progress, P_ENTER_WORLD, out["message"], error=out["error"], enter=ew)
            return out
        prog.emit(progress, P_ENTER_WORLD, "已进入世界")

        # ---- 6) resolve role identity for the caller; do NOT orchestrate
        # hang / master-slave here — that is the caller's responsibility.
        # 首选：选角页已读到三角色卡（含 role_id/name），且明确进入了 slot 对应
        # 的角色——选角即已确定，无需等进世界后再读内存。read_host_identity
        # 仅作兜底（进世界后更精确），失败不影响角色归属。
        rid = ""
        rname = ""
        for r in (char_roles.get("roles") or []):
            try:
                if int(r.get("slot") or 0) == int(slot):
                    rid = str(r.get("role_id") or "")
                    rname = str(r.get("name") or "")
                    break
            except Exception:  # pragma: no cover
                continue
        try:
            from app.core.loot import open_attach_session
            from app.core.team_ops import read_host_identity

            attach = session
            try:
                attach = open_attach_session(pid, log=log)
            except Exception:  # pragma: no cover
                attach = session
            _name, _oid = read_host_identity(attach, need_name=True, log=log)
            if _oid:
                rid = str(_oid)
                rname = str(_name or "") if _name else rname
        except Exception as e:  # pragma: no cover
            log(f"resolve role identity err: {e}")
        out["role_id"] = rid
        out["role_name"] = rname

        out["ok"] = True
        out["error"] = None
        out["message"] = "一键登录完成"
        out["stage"] = LoginStage.IN_WORLD.value
        prog.emit(progress, P_DONE, out["message"], role_id=rid, role_name=rname)
        return out
    finally:
        try:
            session.close()
        except Exception:
            pass


def _apply_role_settings(
    session,
    *,
    pid: int,
    hwnd: int,
    enable_hang: bool = True,
    hang_mode: int | None = None,
    role_id: str = "",
    role_name: str = "",
    log: LogFn | None = None,
) -> dict:
    """Read the in-world role identity and apply its hang settings.

    Uses the game's live host identity to resolve the role_id, then loads the
    role's hang prefs, starts hang and
    registers the pid in the task-sync hub. When ``enable_hang`` is False the
    hang start is skipped but the control role is still registered.
    @author by ak
    """
    log = log or _default_log
    out = {"ok": False, "role_id": "", "role_name": "", "error": None, "message": ""}
    try:
        from app.core.account_manager import get_role, normalize_role_id
        from app.core.hang_settings import (
            apply_hang_prepare,
            get_hang_config,
            start_hang,
        )
        from app.core.loot import open_attach_session
        from app.core.team_ops import read_host_identity

        # 登录阶段已确认角色时优先复用，避免进世界后再次读内存失败。
        rid = normalize_role_id(role_id)
        resolved_name = str(role_name or "")
        try:
            attach = open_attach_session(pid, log=log)
        except Exception:
            attach = session
        if not rid:
            name, oid = read_host_identity(attach, need_name=True, log=log)
            rid = normalize_role_id(oid)
            resolved_name = str(name or "")
        if not rid:
            out["error"] = ERR_NO_ROLE_CFG
            out["message"] = "进世界后未能解析角色 id"
            return out
        out["role_id"] = rid
        out["role_name"] = resolved_name

        role_rec = get_role(rid)
        if role_rec is None:
            out["error"] = ERR_NO_ROLE_CFG
            out["message"] = f"未找到角色配置 role_id={rid}"
            return out

        # --- hang (副本挂机) ---
        hang_res = None
        if enable_hang:
            try:
                cfg = get_hang_config(None, None, char_id=rid)
                if hang_mode is not None:
                    cfg.mode = 1 if int(hang_mode) == 1 else 0
                attach.hwnd = int(hwnd)
                prepare = apply_hang_prepare(attach, cfg, log=log)
                if not bool(prepare.get("ok")):
                    hang_res = {
                        "ok": False,
                        "message": str(prepare.get("message") or "挂机参数设置失败"),
                        "prepare": prepare,
                    }
                else:
                    hang_res = start_hang(attach, cfg, hwnd=hwnd, log=log)
                    hang_res["prepare"] = prepare
            except Exception as e:  # pragma: no cover
                log(f"_apply_role_settings hang err: {e}")
                hang_res = {"ok": False, "error": str(e)}
        out["hang"] = hang_res

        # keep the attach session alive if a hang guard was started
        if hang_res and hang_res.get("ok"):
            out["ok"] = True
            out["message"] = "已应用挂机"
        elif not enable_hang:
            out["ok"] = True
            out["message"] = "挂机已跳过（未勾选开启挂机）"
        else:
            out["ok"] = True  # settings applied even if hang is optional
            out["message"] = f"挂机未启动（{str(hang_res.get('message') or hang_res.get('error') or '跳过')}）"
        return out
    except Exception as e:  # pragma: no cover
        import traceback

        log("".join(traceback.format_exception(type(e), e, e.__traceback__))[-800:])
        out["error"] = f"apply_exception: {e}"
        return out


def _collect_team_members(
    account_id: str,
    pid: int,
    *,
    log: LogFn | None = None,
) -> dict:
    """Post-login team control: read the current party and persist team members.

    Called when the account has 队内控 enabled and the roster still needs to be
    collected. Reads the live party from game memory (``read_cecteam_members``)
    and writes the comma/顿号 separated names into the shared team prefs used by
    the team-control feature. Returns {"ok", "members"}.

    @author by ak
    """
    log = log or _default_log
    out: dict = {"ok": False, "members": ""}
    attach = None
    try:
        from app.core.loot import open_attach_session
        from app.core.team_ops import read_cecteam_members, save_team_prefs

        attach = open_attach_session(pid, log=lambda m: log(m))
        members = list(read_cecteam_members(attach, log=lambda m: log(m)) or [])
        names = [
            str(m.get("name") or "").strip()
            for m in members
            if str(m.get("name") or "").strip()
        ]
        text = "、".join(names)
        if text:
            save_team_prefs(members=text)
            out["ok"] = True
            out["members"] = text
            log(f"账号管理: [{account_id}] 已收集队伍成员: {text}")
        else:
            log(f"账号管理: [{account_id}] 当前不在队伍中，暂无成员可收集")
    except Exception as e:  # pragma: no cover
        log(f"账号管理: [{account_id}] 收集队伍成员失败: {e}")
    finally:
        if attach is not None:
            try:
                attach.close()
            except Exception:
                pass
    return out


def schedule_post_login(
    account_id: str,
    pid: int,
    hwnd: int = 0,
    *,
    inject_delay_s: float = 15.0,
    hang_delay_s: float = 5.0,
    inject_enabled: bool = True,
    hang_enabled: bool = False,
    hang_mode: int = 0,
    log: LogFn | None = None,
    on_inject_ready=None,
    role_id: str = "",
    role_name: str = "",
) -> dict:
    """登录进世界后编排：进图稳定 → 延迟注入正式面板 → 注入成功后延迟开挂。

    账号/角色配置已在登录编排中写好（roles/{role_id}/ 下的 inject / control /
    hang），本函数只负责时序：
      1) 等 ``inject_delay_s``（默认 15s，进图稳定）后调用正式注入
         （run_delete_inject，同 Delete / 一键登录路径）；
      2) 注入成功后通过 ``on_inject_ready(pid, hwnd, role_id, role_name)`` 回调通知调用方创建正式
         面板（功能窗口需由 GUI 侧创建，core 不持 UI；默认隐藏创建，不弹前台）；
      3) 若 ``hang_enabled``（账号「是否开启挂机」勾选），注入成功后立即
         复用 ``_apply_role_settings`` 读取角色磁盘中的挂机设置并启动挂机；
         未勾选时不启动挂机，也不执行其它账号属性业务；
    全程后台线程执行，不阻塞 UI；日志走 ``log``。返回 {"ok", "note"}。
    @author by ak
    """
    import threading

    log = log or _default_log
    account_id = str(account_id or "").strip()
    pid = int(pid or 0)
    hwnd = int(hwnd or 0)
    out: dict = {"ok": True, "note": "", "error": None}
    if not account_id or not pid:
        out["ok"] = False
        out["error"] = "no_account_or_pid"
        return out

    def _sleep_interruptible(secs: float) -> None:
        end = time.monotonic() + max(0.0, float(secs))
        while time.monotonic() < end:
            time.sleep(0.5)

    def worker() -> None:
        try:
            if not inject_enabled:
                log(f"账号管理: [{account_id}] 未勾选注入，跳过登录后注入和挂机")
                return
            # Bind the outer hwnd into a worker-local name so reassignment on
            # the "find by pid" fallback never trips an UnboundLocalError.
            cur_hwnd = int(hwnd or 0)
            # 1) 进图稳定后延迟注入正式面板。
            log(f"账号管理: [{account_id}] 进图稳定等待 {inject_delay_s:.0f}s 后注入")
            _sleep_interruptible(inject_delay_s)
            from app.core.inject_gate import find_main_hwnd_for_pid, run_delete_inject

            if not cur_hwnd:
                try:
                    cur_hwnd, _t, _c = find_main_hwnd_for_pid(pid) or (0, "", "")
                except Exception:
                    cur_hwnd = 0
            if not cur_hwnd:
                log(f"账号管理: [{account_id}] 延迟注入跳过：无游戏窗口 pid={pid}")
                return
            log(f"账号管理: [{account_id}] 开始注入正式面板 pid={pid} hwnd=0x{cur_hwnd:X}")
            inj = run_delete_inject(
                preferred_pid=pid,
                preferred_hwnd=cur_hwnd or None,
                log=lambda m: log(m),
            )
            if not inj.ok:
                log(f"账号管理: [{account_id}] 注入失败 {inj.error or inj.fail_code or '?'}")
                return
            log(f"账号管理: [{account_id}] 注入成功 pid={pid}")
            # 2) 通知调用方打开正式面板（GUI 侧创建功能窗口）。
            if on_inject_ready is not None:
                try:
                    on_inject_ready(
                        int(pid), int(cur_hwnd), str(role_id or ""), str(role_name or "")
                    )
                except TypeError:
                    # Keep third-party/dev callers using the old two-argument
                    # callback working while the identity-aware path rolls out.
                    on_inject_ready(int(pid), int(cur_hwnd))
                except Exception as e:  # pragma: no cover
                    log(f"账号管理: [{account_id}] 打开正式面板失败 {e}")

            # 3) 注入成功后，等待指定时间再按角色已有挂机设置开启挂机。
            if hang_enabled:
                if hang_delay_s > 0:
                    log(f"账号管理: [{account_id}] 注入成功，等待 {hang_delay_s:.0f}s 后开启挂机")
                    _sleep_interruptible(hang_delay_s)
                else:
                    log(f"账号管理: [{account_id}] 注入成功，立即开启挂机")
                from app.core.loot import open_attach_session

                attach = None
                try:
                    attach = open_attach_session(pid, log=lambda m: log(m))
                    apply = _apply_role_settings(
                        attach,
                        pid=pid,
                        hwnd=cur_hwnd,
                        enable_hang=True,
                        hang_mode=hang_mode,
                        role_id=role_id,
                        role_name=role_name,
                        log=log,
                    )
                    log(
                        f"账号管理: [{account_id}] 挂机编排 "
                        f"ok={apply.get('ok')} {apply.get('message')}"
                    )
                finally:
                    if attach is not None:
                        try:
                            attach.close()
                        except Exception:
                            pass

        except Exception as e:  # pragma: no cover
            log(f"账号管理: [{account_id}] 延迟编排异常: {e}")

    threading.Thread(
        target=worker, daemon=True, name=f"xajh-post-login-{account_id}"
    ).start()
    note_parts = [
        f"已安排 {inject_delay_s:.0f}s 后注入" if inject_enabled else "未勾选注入",
    ]
    if hang_enabled:
        note_parts.append(f"注入成功后 {hang_delay_s:.0f}s 开挂")
    out["note"] = "，".join(note_parts)
    return out
