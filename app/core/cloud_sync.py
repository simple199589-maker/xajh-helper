# -*- coding: utf-8 -*-
"""
Cloud task sync client (master publishes, slave receives).

Room key = master_name (主控名称). Auth uses the login token returned by
/api/license/verify; every request is HMAC-signed with that token. Without a
token the bridge stays offline (no HTTP).

Transport: stdlib HTTP
  POST  {base}/api/cloud-control/join
  POST  {base}/api/cloud-control/leave
  POST  {base}/api/cloud-control/publish   # master only
  GET   {base}/api/cloud-control/poll      # slave long-poll
  POST  {base}/api/cloud-control/heartbeat

Server side only needs to implement the room fan-out for the same API key +
master_name; this module is the game-get client contract.

@author by ak
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from app.core.task_sync import (
    ACTION_ACCEPT,
    ACTION_ACCEPT_DAILY_TASKS,
    ACTION_DAILY_ROUTE,
    ACTION_CLAIM_ACTIVITY,
    ACTION_COMPLETE,
    ACTION_HANG_SYNC,
    ACTION_JIANGLONG_CAST,
    ACTION_MAP_FLY,
    ACTION_PATH,
    ACTION_TEAM_FOLLOW,
    ACTION_TEAM_LEAVE,
    ROLE_MASTER,
    ROLE_NONE,
    ROLE_SLAVE,
    TaskSyncEvent,
    VALID_ACTIONS,
)

_ZERO_TID_CLOUD = frozenset(
    {
        ACTION_CLAIM_ACTIVITY,
        ACTION_TEAM_FOLLOW,
        ACTION_TEAM_LEAVE,
        ACTION_MAP_FLY,
        ACTION_HANG_SYNC,
        ACTION_JIANGLONG_CAST,
        ACTION_ACCEPT_DAILY_TASKS,
        ACTION_DAILY_ROUTE,
        "team_accept",  # legacy alias
    }
)

LogFn = Callable[[str], None]
EventFn = Callable[[TaskSyncEvent], None]

PROTOCOL_VERSION = 1
DEFAULT_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) game-get/cloud-control",
    "Accept": "application/json, */*",
}

PATH_JOIN = "/api/cloud-control/join"
PATH_LEAVE = "/api/cloud-control/leave"
PATH_PUBLISH = "/api/cloud-control/publish"
PATH_POLL = "/api/cloud-control/poll"
PATH_HEARTBEAT = "/api/cloud-control/heartbeat"



def cloud_control_flag_enabled(settings: dict | None) -> bool:
    """True when UI/settings checkbox says cloud control on."""
    s = settings if isinstance(settings, dict) else {}
    if bool(s.get("cloud_control_enabled")):
        return True
    return str(s.get("cloud_control_enabled") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def resolve_cloud_login_token(settings: dict | None = None) -> str:
    """
    Login token for /api/cloud-control/*.

    Tokens are intentionally not persisted. AuthService injects this value into
    each window's settings after a successful login.
    @author by ak
    """
    s = settings if isinstance(settings, dict) else {}
    for field in ("login_token", "token", "access_token"):
        v = str(s.get(field) or "").strip()
        if v:
            return v
    return ""


def is_cloud_control_ready(settings: dict | None) -> bool:
    """
    True when this window can actually go online for cloud control.

    Requires: enabled + role master|slave + master_name + url + login token.
    """
    s = settings if isinstance(settings, dict) else {}
    if s.get("cloud_control_available") is False:
        return False
    if not cloud_control_flag_enabled(s):
        return False
    # 队内控与云控互斥：队内控开启时云控不可用。
    try:
        from app.core.team_chat import team_control_flag_enabled

        if team_control_flag_enabled(s):
            return False
    except Exception:
        pass
    role = str(s.get("task_control_role") or ROLE_NONE).strip().lower()
    if role not in (ROLE_MASTER, ROLE_SLAVE):
        return False
    if not normalize_master_name(str(s.get("cloud_control_master_name") or "")):
        return False
    # Service base comes from env/packaged profile, not per-window settings.
    try:
        from app.core.build_profile import default_captcha_base_url

        if not str(default_captcha_base_url() or "").strip():
            return False
    except Exception:
        if not str(s.get("captcha_base_url") or "").strip():
            return False
    if not resolve_cloud_login_token(s):
        return False
    return True


def is_cloud_slave_isolated(settings: dict | None) -> bool:
    """
    Cloud-enabled slave: ignore local TaskSyncHub, only execute cloud events.

    Isolation follows ready config (not live online state).
    """
    s = settings if isinstance(settings, dict) else {}
    role = str(s.get("task_control_role") or ROLE_NONE).strip().lower()
    return role == ROLE_SLAVE and is_cloud_control_ready(s)


def normalize_master_name(name: str) -> str:
    """Trim + collapse internal spaces for stable room keys."""
    s = " ".join(str(name or "").strip().split())
    return s[:64]


def make_msg_id(
    *,
    action: str,
    task_id: int,
    source_pid: int,
    ts: float | None = None,
) -> str:
    """Stable-ish id for dedupe across local+cloud delivery."""
    t = float(ts if ts is not None else time.time())
    return f"{int(source_pid)}-{str(action)}-{int(task_id) & 0xFFFFFFFF}-{int(t * 1000)}"


def build_publish_body(
    *,
    master_name: str,
    event: TaskSyncEvent | dict,
    client_id: str = "",
    msg_id: str = "",
        phase: str = "",
        target_scene_id: int | None = None,
        target_tid: int | None = None,
        target_x: float | None = None,
        target_y: float | None = None,
        target_z: float | None = None,
        target_round: int | None = None,
) -> dict[str, Any]:
    """Wire payload for POST /publish (server contract helper)."""
    if isinstance(event, TaskSyncEvent):
        ev = event.to_dict()
    else:
        ev = dict(event or {})
    mid = str(msg_id or ev.get("msg_id") or "").strip()
    if not mid:
        mid = make_msg_id(
            action=str(ev.get("action") or ""),
            task_id=int(ev.get("task_id") or 0),
            source_pid=int(ev.get("source_pid") or 0),
            ts=float(ev.get("ts") or time.time()),
        )
    ev["msg_id"] = mid
    ev["origin"] = "cloud"
    return {
        "v": PROTOCOL_VERSION,
        "type": "task_sync",
        "master_name": normalize_master_name(master_name),
        "client_id": str(client_id or ""),
        "msg_id": mid,
        "event": ev,
    }


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_cloud_event(payload: dict | None) -> TaskSyncEvent | None:
    """
    Accept either envelope {event:{...}} or bare task-sync dict.
    Returns None if invalid / unsupported action.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip().lower()
    if action not in VALID_ACTIONS:
        return None
    try:
        task_id = int(raw.get("task_id") or 0) & 0xFFFFFFFF
    except (TypeError, ValueError):
        task_id = 0
    if action not in _ZERO_TID_CLOUD and not task_id:
        return None
    try:
        source_pid = int(raw.get("source_pid") or 0)
    except (TypeError, ValueError):
        source_pid = 0
    can_finish = raw.get("can_finish")
    if can_finish is not None:
        can_finish = bool(can_finish)
    points = raw.get("points")
    try:
        points_i = int(points) if points is not None else None
    except (TypeError, ValueError):
        points_i = None
    try:
        ts = float(raw.get("ts") or time.time())
    except (TypeError, ValueError):
        ts = time.time()
    try:
        hang_mode = (
            int(raw.get("hang_mode"))
            if raw.get("hang_mode") is not None
            else None
        )
    except (TypeError, ValueError):
        hang_mode = None
    if hang_mode not in (0, 1):
        hang_mode = None
    return TaskSyncEvent(
        action=action,
        task_id=task_id,
        source_pid=source_pid,
        ts=ts,
        can_finish=can_finish,
        name=str(raw.get("name") or ""),
        origin="cloud",
        portal_kind=str(raw.get("portal_kind") or ""),
        origin_scene_id=_optional_int(raw.get("origin_scene_id")),
        portal_tid=_optional_int(raw.get("portal_tid")),
        portal_obj_id=_optional_int(raw.get("portal_obj_id")),
        portal_x=_optional_float(raw.get("portal_x")),
        portal_y=_optional_float(raw.get("portal_y")),
        portal_z=_optional_float(raw.get("portal_z")),
        points=points_i,
        members=str(raw.get("members") or ""),
        hang_mode=hang_mode,
        phase=str(raw.get("phase") or ""),
        target_scene_id=_optional_int(raw.get("target_scene_id")),
        target_tid=_optional_int(raw.get("target_tid")),
        target_x=_optional_float(raw.get("target_x")),
        target_y=_optional_float(raw.get("target_y")),
        target_z=_optional_float(raw.get("target_z")),
        target_round=_optional_int(raw.get("target_round")),
    )


def event_dedupe_key(event: TaskSyncEvent | dict, msg_id: str = "") -> str:
    if msg_id:
        return f"id:{msg_id}"
    if isinstance(event, TaskSyncEvent):
        d = event.to_dict()
    else:
        d = dict(event or {})
    mid = str(d.get("msg_id") or "").strip()
    if mid:
        return f"id:{mid}"
    return (
        f"a:{d.get('action')}|t:{int(d.get('task_id') or 0)}|"
        f"p:{int(d.get('source_pid') or 0)}|n:{d.get('name') or ''}|"
        f"k:{d.get('portal_kind') or ''}|pt:{d.get('points')}|"
        f"hm:{d.get('hang_mode')}|ph:{d.get('phase') or ''}|"
        f"tr:{d.get('target_round') or 0}"
    )


@dataclass
class CloudSyncStatus:
    enabled: bool = False
    online: bool = False
    role: str = ROLE_NONE
    master_name: str = ""
    last_error: str = ""
    last_ok_ts: float = 0.0
    published: int = 0
    received: int = 0
    detail: str = "未启用"

    def label(self) -> str:
        if not self.enabled:
            if self.last_error:
                return f"云控：{self.last_error[:48]}"
            return "云控：未开启（本机群控仍可用）"
        name = self.master_name or "?"
        role_name = "主控发送" if self.role == ROLE_MASTER else "副控接收"
        if self.online:
            count = (
                f" · 已发 {self.published} 条"
                if self.role == ROLE_MASTER
                else f" · 已收 {self.received} 条"
            )
            return f"云控：已连接 · {role_name} · 通道「{name}」{count}"
        if self.last_error:
            return f"云控：连接失败 · {self.last_error[:48]}"
        return f"云控：正在连接 · {role_name} · 通道「{name}」"


@dataclass
class CloudSyncConfig:
    enabled: bool = False
    base_url: str = ""
    login_token: str = ""
    master_name: str = ""
    role: str = ROLE_NONE
    client_id: str = ""
    pid: int = 0
    poll_wait_s: float = 25.0
    heartbeat_s: float = 20.0
    request_timeout_s: float = 30.0


class CloudSyncBridge:
    """
    Per-window cloud bridge: master publish-only, slave poll-only.

    @author by ak
    """

    def __init__(self, pid: int, *, log: LogFn | None = None) -> None:
        self.pid = int(pid)
        self._log = log or (lambda _m: None)
        self._lock = threading.RLock()
        self._cfg = CloudSyncConfig(pid=self.pid)
        self._status = CloudSyncStatus()
        self._listener: EventFn | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._session_id = ""
        self._cursor = ""
        self._recent_keys: dict[str, float] = {}
        self._dedupe_s = 8.0
        self._cfg_epoch = 0
        self._gen = 0

    # ---- public API -------------------------------------------------

    def set_log(self, log: LogFn | None) -> None:
        self._log = log or (lambda _m: None)

    def set_listener(self, listener: EventFn | None) -> None:
        with self._lock:
            self._listener = listener

    def status(self) -> CloudSyncStatus:
        with self._lock:
            return CloudSyncStatus(**asdict(self._status))

    def status_label(self) -> str:
        return self.status().label()

    def apply_config(
        self,
        *,
        enabled: bool,
        base_url: str,
        login_token: str,
        master_name: str,
        role: str,
        client_id: str = "",
    ) -> CloudSyncStatus:
        """
        Update config and (re)start background loop when needed.
        """
        role_s = str(role or ROLE_NONE).strip().lower()
        if role_s not in (ROLE_NONE, ROLE_MASTER, ROLE_SLAVE):
            role_s = ROLE_NONE
        name = normalize_master_name(master_name)
        url = str(base_url or "").strip().rstrip("/")
        token = str(login_token or "").strip()
        cid = str(client_id or "").strip() or f"pid-{self.pid}"

        want_on = (
            bool(enabled)
            and bool(url)
            and bool(token)
            and bool(name)
        )
        if role_s == ROLE_NONE:
            want_on = False

        with self._lock:
            prev_cfg = CloudSyncConfig(**asdict(self._cfg))
            prev = (
                self._cfg.enabled,
                self._cfg.base_url,
                self._cfg.login_token,
                self._cfg.master_name,
                self._cfg.role,
                self._cfg.client_id,
            )
            self._cfg = CloudSyncConfig(
                enabled=want_on,
                base_url=url,
                login_token=token,
                master_name=name,
                role=role_s if want_on else ROLE_NONE,
                client_id=cid,
                pid=self.pid,
            )
            self._status.enabled = want_on
            self._status.role = self._cfg.role
            self._status.master_name = name
            if not want_on:
                self._status.online = False
                if not enabled:
                    self._status.detail = "未启用"
                    self._status.last_error = ""
                elif role_s == ROLE_NONE:
                    self._status.detail = "无控不参与云控"
                    self._status.last_error = ""
                elif not name:
                    self._status.detail = "请填写主控名称"
                    self._status.last_error = "missing master_name"
                elif not token:
                    self._status.detail = "登录令牌不可用，请重新登录"
                    self._status.last_error = "missing login_token"
                elif not url:
                    self._status.detail = "缺少云控 URL"
                    self._status.last_error = "missing base_url"
            else:
                # Keep previous online/detail if config unchanged; avoid "连接中" flicker.
                if prev[0] and prev == (
                    want_on,
                    url,
                    token,
                    name,
                    role_s if want_on else ROLE_NONE,
                    cid,
                ):
                    pass
                else:
                    self._status.detail = "连接中…"
            new_tuple = (
                self._cfg.enabled,
                self._cfg.base_url,
                self._cfg.login_token,
                self._cfg.master_name,
                self._cfg.role,
                self._cfg.client_id,
            )
            # Only bump epoch when config actually changes — otherwise the
            # background worker sees epoch mismatch and exits without restart,
            # leaving master with empty session_id (publish → 未 join).
            if prev != new_tuple:
                self._cfg_epoch += 1
            epoch = self._cfg_epoch

        if not want_on:
            self._stop_worker(leave=True, leave_cfg=prev_cfg)
            return self.status()

        if prev != new_tuple or self._thread is None or not self._thread.is_alive():
            self._start_worker(epoch, leave_cfg=prev_cfg)
        return self.status()

    def publish_event(
        self,
        *,
        action: str,
        task_id: int,
        source_pid: int,
        can_finish: bool | None = None,
        name: str = "",
        portal_kind: str = "",
        origin_scene_id: int | None = None,
        portal_tid: int | None = None,
        portal_obj_id: int | None = None,
        portal_x: float | None = None,
        portal_y: float | None = None,
        portal_z: float | None = None,
        points: int | None = None,
        members: str = "",
        hang_mode: int | None = None,
        msg_id: str = "",
        phase: str = "",
        target_scene_id: int | None = None,
        target_tid: int | None = None,
        target_x: float | None = None,
        target_y: float | None = None,
        target_z: float | None = None,
        target_round: int | None = None,
    ) -> bool:
        """
        Master → cloud. No-op unless this bridge is online master.
        Returns True when server accepted publish.
        """
        action_s = str(action or "").strip().lower()
        if action_s not in VALID_ACTIONS:
            return False
        tid = int(task_id) & 0xFFFFFFFF
        if action_s not in _ZERO_TID_CLOUD and not tid:
            return False

        with self._lock:
            cfg0 = CloudSyncConfig(**asdict(self._cfg))
        if not cfg0.enabled or cfg0.role != ROLE_MASTER:
            return False

        # Ensure worker has joined (session_id) before publish.
        if not self._ensure_session(timeout_s=6.0):
            self._log(f"云控 [发失败] {action_s} #{tid}: 等待 join 超时")
            with self._lock:
                self._status.last_error = "waiting join timeout"
                self._status.detail = "发布失败: 未 join"
            return False

        event = TaskSyncEvent(
            action=action_s,
            task_id=tid,
            source_pid=int(source_pid or self.pid),
            can_finish=can_finish,
            name=str(name or ""),
            origin="cloud",
            portal_kind=str(portal_kind or ""),
            origin_scene_id=(
                int(origin_scene_id) if origin_scene_id is not None else None
            ),
            portal_tid=(int(portal_tid) if portal_tid is not None else None),
            portal_obj_id=(
                int(portal_obj_id) if portal_obj_id is not None else None
            ),
            portal_x=(float(portal_x) if portal_x is not None else None),
            portal_y=(float(portal_y) if portal_y is not None else None),
            portal_z=(float(portal_z) if portal_z is not None else None),
            points=(int(points) if points is not None else None),
            members=str(members or ""),
            hang_mode=(int(hang_mode) if hang_mode in (0, 1) else None),
            phase=str(phase or ""),
            target_scene_id=(int(target_scene_id) if target_scene_id is not None else None),
            target_tid=(int(target_tid) if target_tid is not None else None),
            target_x=(float(target_x) if target_x is not None else None),
            target_y=(float(target_y) if target_y is not None else None),
            target_z=(float(target_z) if target_z is not None else None),
            target_round=(int(target_round) if target_round is not None else None),
        )

        def _do_publish(session_id: str) -> tuple[bool, str | None, int]:
            with self._lock:
                cfg = CloudSyncConfig(**asdict(self._cfg))
            if not cfg.enabled or cfg.role != ROLE_MASTER:
                return False, "not master", 0
            body = build_publish_body(
                master_name=cfg.master_name,
                event=event,
                client_id=cfg.client_id,
                msg_id=msg_id,
            )
            if session_id:
                body["session_id"] = session_id
            status, data, err = self._http_json(
                "POST",
                cfg.base_url + PATH_PUBLISH,
                login_token=cfg.login_token,
                body=body,
                timeout_s=cfg.request_timeout_s,
                sign_path=PATH_PUBLISH,
            )
            ok_local = bool(
                status and 200 <= status < 300 and (not data or data.get("ok", True))
            )
            if not ok_local and isinstance(data, dict):
                msg = str(data.get("message") or data.get("error") or err or "")
                err = msg or err
            return ok_local, err, int(status or 0)

        with self._lock:
            session_id = self._session_id
            master_name = self._cfg.master_name
        ok, err, status = _do_publish(session_id)
        err_s = str(err or "")
        if (not ok) and (
            "session" in err_s.lower()
            or "join" in err_s.lower()
            or "过期" in err_s
            or "未 join" in err_s
        ):
            self._log(f"云控 [发] session 失效，重新 join 后重试 {action_s} #{tid}")
            with self._lock:
                self._session_id = ""
                self._status.online = False
            if self._ensure_session(timeout_s=8.0, force_rejoin=True):
                with self._lock:
                    session_id = self._session_id
                    master_name = self._cfg.master_name
                ok, err, status = _do_publish(session_id)
                err_s = str(err or "")

        with self._lock:
            if ok:
                self._status.published += 1
                self._status.last_ok_ts = time.time()
                self._status.last_error = ""
                self._status.online = True
                self._status.detail = "已发布"
            else:
                self._status.last_error = err_s or f"publish http {status}"
                self._status.detail = f"发布失败: {self._status.last_error[:40]}"
            master_name = self._cfg.master_name
        if ok:
            self._log(
                f"云控 [发] {action_s} #{tid} master={master_name!r}"
            )
        else:
            self._log(
                f"云控 [发失败] {action_s} #{tid}: {err_s or status} "
                f"master={master_name!r}"
            )
        return ok

    def stop(self) -> None:
        self.apply_config(
            enabled=False,
            base_url=self._cfg.base_url,
            login_token="",
            master_name="",
            role=ROLE_NONE,
            client_id=self._cfg.client_id,
        )

    # ---- session helpers --------------------------------------------

    def _ensure_session(
        self, *, timeout_s: float = 6.0, force_rejoin: bool = False
    ) -> bool:
        """
        Wait until master/slave bridge has a non-empty session_id.

        If worker is dead, restart it. Optional force_rejoin clears session and
        does one synchronous join for masters that need to publish immediately.
        """
        with self._lock:
            if not self._cfg.enabled:
                return False
            if force_rejoin:
                self._session_id = ""
                self._status.online = False
            if self._session_id and not force_rejoin:
                return True
            cfg = CloudSyncConfig(**asdict(self._cfg))
            thread_alive = self._thread is not None and self._thread.is_alive()
            epoch = self._cfg_epoch

        if force_rejoin or not thread_alive:
            # Prefer background worker; for force_rejoin also try sync join now.
            if not thread_alive:
                self._start_worker(epoch)
            if force_rejoin or not self._session_id:
                try:
                    if self._send_join(cfg):
                        with self._lock:
                            if self._session_id:
                                self._status.online = True
                                self._status.detail = "在线"
                                return True
                except Exception:
                    pass

        deadline = time.time() + float(timeout_s)
        while time.time() < deadline:
            with self._lock:
                if self._session_id:
                    return True
                enabled = self._cfg.enabled
                thread_alive = self._thread is not None and self._thread.is_alive()
                epoch = self._cfg_epoch
            if not enabled:
                return False
            if not thread_alive:
                self._start_worker(epoch)
            time.sleep(0.05)
        with self._lock:
            return bool(self._session_id)

    # ---- worker -----------------------------------------------------

    def _start_worker(self, epoch: int, leave_cfg: CloudSyncConfig | None = None) -> None:
        self._stop_worker(leave=True, leave_cfg=leave_cfg)
        self._stop = threading.Event()
        self._gen += 1
        gen = self._gen

        def run() -> None:
            self._worker_main(gen, epoch)

        th = threading.Thread(
            target=run,
            name=f"cloud-sync-{self.pid}",
            daemon=True,
        )
        self._thread = th
        th.start()

    def _stop_worker(self, *, leave: bool, leave_cfg: CloudSyncConfig | None = None) -> None:
        self._stop.set()
        th = self._thread
        self._thread = None
        if leave:
            try:
                self._send_leave(leave_cfg=leave_cfg)
            except Exception:
                pass
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=0.2)

    def _worker_main(self, gen: int, epoch: int) -> None:
        backoff = 1.0
        while not self._stop.is_set() and gen == self._gen:
            with self._lock:
                if epoch != self._cfg_epoch:
                    return
                cfg = CloudSyncConfig(**asdict(self._cfg))
            if not cfg.enabled:
                return

            ok_join = self._send_join(cfg)
            if not ok_join:
                with self._lock:
                    self._status.online = False
                if self._stop.wait(min(backoff, 15.0)):
                    return
                backoff = min(backoff * 1.7, 15.0)
                continue

            backoff = 1.0
            with self._lock:
                self._status.online = True
                self._status.last_error = ""
                self._status.detail = "在线"
                self._status.last_ok_ts = time.time()

            if cfg.role == ROLE_MASTER:
                # Master: heartbeat only.
                while not self._stop.is_set() and gen == self._gen:
                    with self._lock:
                        if epoch != self._cfg_epoch or not self._cfg.enabled:
                            return
                        hb = float(self._cfg.heartbeat_s)
                    if not self._send_heartbeat():
                        break
                    if self._stop.wait(max(5.0, hb)):
                        return
                continue

            # Slave: long-poll loop.
            while not self._stop.is_set() and gen == self._gen:
                with self._lock:
                    if epoch != self._cfg_epoch or not self._cfg.enabled:
                        return
                    cfg = CloudSyncConfig(**asdict(self._cfg))
                    cursor = self._cursor
                if cfg.role != ROLE_SLAVE:
                    break
                events, next_cursor, err = self._poll_once(cfg, cursor)
                if err:
                    with self._lock:
                        self._status.online = False
                        self._status.last_error = err
                        self._status.detail = f"轮询失败: {err[:40]}"
                    break
                with self._lock:
                    self._status.online = True
                    self._status.last_error = ""
                    self._status.last_ok_ts = time.time()
                    self._status.detail = "在线 · 监听中"
                    if next_cursor:
                        self._cursor = str(next_cursor)
                for item in events:
                    self._dispatch_incoming(item)

    def _dispatch_incoming(self, item: dict) -> None:
        msg_id = str(item.get("msg_id") or "")
        if not msg_id and isinstance(item.get("event"), dict):
            msg_id = str(item["event"].get("msg_id") or "")
        event = parse_cloud_event(item)
        if event is None:
            return
        key = event_dedupe_key(event, msg_id=msg_id)
        now = time.time()
        with self._lock:
            # prune
            self._recent_keys = {
                k: ts for k, ts in self._recent_keys.items() if now - ts <= self._dedupe_s
            }
            if key in self._recent_keys:
                return
            self._recent_keys[key] = now
            listener = self._listener
            self._status.received += 1
        self._log(
            f"云控 [收] {event.action} #{event.task_id} "
            f"from_pid={event.source_pid} msg={msg_id or key}"
        )
        if listener is None:
            return
        try:
            listener(event)
        except Exception as e:
            self._log(f"云控 [收处理失败] {e}")

    # ---- HTTP helpers -----------------------------------------------

    def _send_join(self, cfg: CloudSyncConfig) -> bool:
        body = {
            "v": PROTOCOL_VERSION,
            "master_name": cfg.master_name,
            "role": cfg.role,
            "client_id": cfg.client_id,
            "pid": int(cfg.pid or self.pid),
        }
        status, data, err = self._http_json(
            "POST",
            cfg.base_url + PATH_JOIN,
            login_token=cfg.login_token,
            body=body,
            timeout_s=min(15.0, cfg.request_timeout_s),
            sign_path=PATH_JOIN,
        )
        ok = bool(status and 200 <= status < 300 and (not data or data.get("ok", True)))
        if ok and isinstance(data, dict):
            sid = str(data.get("session_id") or data.get("session") or "").strip()
            if sid:
                with self._lock:
                    self._session_id = sid
            cur = data.get("cursor")
            if cur is not None:
                with self._lock:
                    self._cursor = str(cur)
        if not ok:
            with self._lock:
                self._status.last_error = err or f"join http {status}"
                self._status.detail = f"加入失败: {self._status.last_error[:40]}"
            self._log(f"云控 [join失败] {err or status} name={cfg.master_name!r}")
        else:
            self._log(
                f"云控 [join] role={cfg.role} name={cfg.master_name!r} "
                f"client={cfg.client_id}"
            )
        return ok

    def _send_leave(self, *, leave_cfg: CloudSyncConfig | None = None) -> None:
        with self._lock:
            cfg = leave_cfg or CloudSyncConfig(**asdict(self._cfg))
            sid = self._session_id
            # Leave uses the prior config when a token/config change stops a worker.
            url = cfg.base_url
            token = cfg.login_token
            name = cfg.master_name
            role = cfg.role
            client_id = cfg.client_id
            self._session_id = ""
            self._status.online = False
        if not url or not token or not name:
            return
        body = {
            "v": PROTOCOL_VERSION,
            "master_name": name,
            "role": role,
            "client_id": client_id,
            "session_id": sid,
            "pid": self.pid,
        }
        try:
            self._http_json(
                "POST",
                url + PATH_LEAVE,
                login_token=token,
                body=body,
                timeout_s=5.0,
                sign_path=PATH_LEAVE,
            )
        except Exception:
            pass

    def _send_heartbeat(self) -> bool:
        with self._lock:
            cfg = CloudSyncConfig(**asdict(self._cfg))
            sid = self._session_id
        if not cfg.enabled:
            return False
        body = {
            "v": PROTOCOL_VERSION,
            "master_name": cfg.master_name,
            "role": cfg.role,
            "client_id": cfg.client_id,
            "session_id": sid,
            "pid": self.pid,
        }
        status, data, err = self._http_json(
            "POST",
            cfg.base_url + PATH_HEARTBEAT,
            login_token=cfg.login_token,
            body=body,
            timeout_s=min(15.0, cfg.request_timeout_s),
            sign_path=PATH_HEARTBEAT,
        )
        ok = bool(status and 200 <= status < 300 and (not data or data.get("ok", True)))
        if not ok:
            with self._lock:
                self._status.last_error = err or f"heartbeat http {status}"
            self._log(f"云控 [heartbeat失败] {err or status}")
        return ok

    def _poll_once(
        self, cfg: CloudSyncConfig, cursor: str
    ) -> tuple[list[dict], str, str | None]:
        q = {
            "master_name": cfg.master_name,
            "client_id": cfg.client_id,
            "wait_s": str(int(max(1, min(60, cfg.poll_wait_s)))),
        }
        if cursor:
            q["cursor"] = cursor
        with self._lock:
            sid = self._session_id
        if sid:
            q["session_id"] = sid
        url = cfg.base_url + PATH_POLL + "?" + urllib.parse.urlencode(q)
        # long-poll: timeout slightly above wait_s
        timeout_s = float(cfg.poll_wait_s) + 10.0
        # sign path includes query (server verifies query integrity)
        qstr = urllib.parse.urlencode(q)
        sign_path = PATH_POLL + (("?" + qstr) if qstr else "")
        status, data, err = self._http_json(
            "GET",
            url,
            login_token=cfg.login_token,
            body=None,
            timeout_s=timeout_s,
            sign_path=sign_path,
        )
        if status == 0 and err and "timed out" in err.lower():
            # treat timeout as empty success
            return [], cursor, None
        if not status or status < 200 or status >= 300:
            return [], cursor, err or f"poll http {status}"
        if not isinstance(data, dict):
            return [], cursor, "poll bad json"
        if data.get("ok") is False:
            return [], cursor, str(data.get("message") or data.get("error") or "poll rejected")
        events_raw = data.get("events") or data.get("messages") or []
        out: list[dict] = []
        if isinstance(events_raw, list):
            for it in events_raw:
                if isinstance(it, dict):
                    out.append(it)
        next_cursor = data.get("cursor")
        if next_cursor is None:
            next_cursor = cursor
        return out, str(next_cursor or ""), None

    def _http_json(
        self,
        method: str,
        url: str,
        *,
        login_token: str,
        body: dict | None,
        timeout_s: float,
        sign_path: str = "",
    ) -> tuple[int, dict | None, str | None]:
        headers = dict(DEFAULT_HTTP_HEADERS)
        token = str(login_token or "").strip()
        if not token:
            return 0, None, "missing login token"
        headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            from app.core.license_client import build_token_signature_headers

            path_for_sign = str(sign_path or urllib.parse.urlparse(url).path or "/")
            headers.update(
                build_token_signature_headers(
                    method,
                    path_for_sign,
                    data or b"",
                    token,
                )
            )
        except Exception as e:
            return 0, None, f"sign failed: {e}"
        req = urllib.request.Request(url, data=data, method=method.upper())
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                raw = resp.read()
                if not raw:
                    return status, {}, None
                try:
                    parsed = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception as e:
                    return status, None, f"json decode: {e}"
                if not isinstance(parsed, dict):
                    return status, None, "response not object"
                return status, parsed, None
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
                parsed = json.loads(raw.decode("utf-8", errors="replace")) if raw else None
            except Exception:
                parsed = None
            msg = None
            if isinstance(parsed, dict):
                msg = str(parsed.get("message") or parsed.get("error") or "")
            return int(e.code), parsed if isinstance(parsed, dict) else None, msg or str(e)
        except TimeoutError as e:
            return 0, None, f"timed out: {e}"
        except socket.timeout as e:
            return 0, None, f"timed out: {e}"
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            return 0, None, f"url error: {reason}"
        except Exception as e:
            return 0, None, str(e)


_BRIDGES: dict[int, CloudSyncBridge] = {}
_BRIDGES_LOCK = threading.Lock()


def get_cloud_sync_bridge(pid: int, *, log: LogFn | None = None) -> CloudSyncBridge:
    """Process-wide bridge registry keyed by game pid."""
    pid = int(pid)
    with _BRIDGES_LOCK:
        br = _BRIDGES.get(pid)
        if br is None:
            br = CloudSyncBridge(pid, log=log)
            _BRIDGES[pid] = br
        elif log is not None:
            br.set_log(log)
        return br


def drop_cloud_sync_bridge(pid: int) -> None:
    pid = int(pid)
    with _BRIDGES_LOCK:
        br = _BRIDGES.pop(pid, None)
    if br is not None:
        try:
            br.stop()
        except Exception:
            pass



def apply_settings_to_bridge(
    settings: dict | None,
    pid: int,
    *,
    log: LogFn | None = None,
    listener: EventFn | None = None,
) -> CloudSyncStatus:
    """
    Map per-window settings dict onto the pid bridge.

    Uses the current login token from AuthService;
    room key = cloud_control_master_name.
    Every business request is HMAC-signed with that token.
    """
    pid = int(pid or 0)
    if not pid:
        return CloudSyncStatus(detail="no pid")
    s = settings if isinstance(settings, dict) else {}
    br = get_cloud_sync_bridge(pid, log=log)
    if listener is not None:
        br.set_listener(listener)
    if s.get("cloud_control_available") is False:
        br.stop()
        return CloudSyncStatus(
            last_error="本地测试卡不支持云控",
            detail="本地测试卡不支持云控",
        )
    enabled = cloud_control_flag_enabled(s)
    # 队内控与云控互斥：队内控开启时云控不上线。
    try:
        from app.core.team_chat import team_control_flag_enabled

        if team_control_flag_enabled(s):
            enabled = False
    except Exception:
        pass
    try:
        from app.core.build_profile import default_captcha_base_url

        service_base = str(default_captcha_base_url() or "").strip()
    except Exception:
        service_base = str(s.get("captcha_base_url") or "").strip()
    return br.apply_config(
        enabled=enabled,
        base_url=service_base,
        login_token=resolve_cloud_login_token(s),
        master_name=str(s.get("cloud_control_master_name") or "").strip(),
        role=str(s.get("task_control_role") or ROLE_NONE).strip().lower(),
        client_id=str(s.get("cloud_control_client_id") or f"pid-{pid}"),
    )


def reset_cloud_sync_bridges_for_tests() -> None:
    """Test helper: stop and clear all bridges."""
    with _BRIDGES_LOCK:
        items = list(_BRIDGES.items())
        _BRIDGES.clear()
    for _pid, br in items:
        try:
            br.stop()
        except Exception:
            pass
