# -*- coding: utf-8 -*-
"""
Login / card-key auth for the welcome shell.

Always uses real license API verify/unbind (X-API-Key=card_key; optional HMAC).
Also holds app-level preference: unlock game client multi-open (xajh.exe).

@author by ak
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.core.build_profile import (
    default_license_api_secret,
    default_license_base_url,
    license_reverify_interval_s,
)
from app.core.license_client import (
    compute_machine_code,
    mask_card_key,
    unbind_license,
    verify_license,
)

LogFn = Callable[[str], None]
LOCAL_MOCK_CARD_KEY = "admin"
LOCAL_MOCK_CARD_TTL = timedelta(days=3)

# Game client built-in concurrent window cap (xajh mutex slots 0..2).
GAME_CLIENT_DEFAULT_MAX_WINDOWS = 3
# Back-compat alias
DEFAULT_MAX_GAME_SESSIONS = GAME_CLIENT_DEFAULT_MAX_WINDOWS


def _prefs_path() -> Path:
    """
    Writable path for login prefs (next to exe, fallback LOCALAPPDATA).

    @author by ak
    """
    try:
        from common.paths import ensure_writable_dir

        d = ensure_writable_dir("runtime", "config")
        return d / "login_prefs.json"
    except Exception:
        return Path.cwd() / "runtime" / "config" / "login_prefs.json"


def load_login_prefs() -> dict[str, Any]:
    """
    Load cached login fields (key, debug_mode, multi_open). Missing file → {}.

    @author by ak
    """
    p = _prefs_path()
    try:
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_login_prefs(
    *,
    key: str | None = None,
    debug_mode: bool | None = None,
    multi_open: bool | None = None,
) -> None:
    """
    Merge and persist login prefs. Only non-None fields are updated.

    @author by ak
    """
    cur = load_login_prefs()
    if key is not None:
        cur["key"] = str(key).strip()
    if debug_mode is not None:
        cur["debug_mode"] = bool(debug_mode)
    if multi_open is not None:
        cur["multi_open"] = bool(multi_open)
    cur["updated_at"] = datetime.now().isoformat(timespec="seconds")
    p = _prefs_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(cur, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def last_login_key() -> str:
    """Cached card-key string for login entry prefill. @author by ak"""
    return str(load_login_prefs().get("key") or "").strip()


def preferred_login_key() -> str:
    """
    Login page 卡密 input prefill only (does not touch settings captcha key).

    1) login_prefs.key if user has logged in before (any channel)
    2) else, default to the local generic card `admin` in all builds

    This value is only the login form default. The card-key is used for the
    license endpoints; successful login supplies the token used by business APIs.

    @author by ak
    """
    cached = last_login_key()
    if cached:
        return cached
    return LOCAL_MOCK_CARD_KEY


def is_multi_open_enabled() -> bool:
    """
    True when login-page「游戏多开」is on.

    Means: unlock xajh.exe built-in 3-window mutex limit (game multi-open),
    not a limit on this helper process.
    @author by ak
    """
    try:
        return bool(load_login_prefs().get("multi_open", False))
    except Exception:
        return False


def set_multi_open_enabled(enabled: bool) -> None:
    """Persist game multi-open preference. @author by ak"""
    save_login_prefs(multi_open=bool(enabled))


def _parse_expires_at(raw: str | None) -> datetime | None:
    """Parse server UTC ISO expires_at into local-aware datetime. @author by ak"""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    except Exception:
        return None


def _format_expire_local(dt: datetime | None) -> str:
    """Card-key expiry line for tray / settings. @author by ak"""
    if dt is None:
        return "卡密：已授权"
    local = dt.astimezone() if dt.tzinfo else dt
    return f"卡密到期：{local.strftime('%Y-%m-%d %H:%M')}"


@dataclass
class AuthSession:
    """
    In-memory login session.

    @author by ak
    """

    key: str
    token: str = ""
    local_mock_card: bool = False
    logged_in: bool = False
    expire_at: datetime | None = None
    display_name: str = "已授权"

    def expire_text(self) -> str:
        """Human-readable expiry for tray tooltip. @author by ak"""
        if not self.logged_in:
            return "未登录"
        return _format_expire_local(self.expire_at)

    def tray_tip(self) -> str:
        """Tray hover text. @author by ak"""
        if not self.logged_in:
            return "XAJH 助手 · 未登录"
        return f"XAJH 助手 · {self.display_name}\n{self.expire_text()}"


class AuthService:
    """
    Card-key login + machine unbind via signed license API.

    @author by ak
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        secret: str | None = None,
        log: LogFn | None = None,
    ) -> None:
        self.session = AuthSession(key="")
        self.base_url = (base_url or default_license_base_url()).rstrip("/")
        self.secret = (
            str(secret).strip()
            if secret is not None
            else default_license_api_secret()
        )
        self._log = log or (lambda _m: None)

    def _machine_code(self) -> str:
        return compute_machine_code()

    def login(self, key: str) -> tuple[bool, str]:
        """
        Validate card-key via /api/license/verify and mark logged in.

        @author by ak
        """
        k = (key or "").strip()
        if not k:
            return False, "请输入卡密"
        if k.lower() == LOCAL_MOCK_CARD_KEY:
            expires_at = datetime.now().astimezone() + LOCAL_MOCK_CARD_TTL
            self.session = AuthSession(
                key=LOCAL_MOCK_CARD_KEY,
                logged_in=True,
                local_mock_card=True,
                expire_at=expires_at,
                display_name="本地测试",
            )
            save_login_prefs(key=LOCAL_MOCK_CARD_KEY)
            self._log("local mock admin login accepted; cloud control disabled")
            return True, self.session.expire_text()

        mc = self._machine_code()
        result = verify_license(
            k,
            mc,
            base_url=self.base_url,
            secret=self.secret,
            log=self._log,
        )
        if not result.ok:
            return False, result.message or "登录失败"
        token = str(result.token or "").strip()
        if not token:
            return False, "登录服务未返回令牌，请重试"

        expire_at = _parse_expires_at(result.expires_at)
        self.session = AuthSession(
            key=k,
            token=token,
            logged_in=True,
            expire_at=expire_at,
            display_name="已授权",
        )
        save_login_prefs(key=k)
        self._log(
            f"login ok card={mask_card_key(k)} expire={self.session.expire_text()}"
        )
        return True, self.session.expire_text()

    def unbind(self, key: str) -> tuple[bool, str]:
        """
        Unbind current machine from card-key. Message from server when present.

        @author by ak
        """
        k = (key or "").strip()
        if not k:
            return False, "请输入卡密"
        if k.lower() == LOCAL_MOCK_CARD_KEY or self.session.local_mock_card:
            return False, "本地测试卡无需解绑"
        mc = self._machine_code()
        result = unbind_license(
            k,
            mc,
            base_url=self.base_url,
            secret=self.secret,
            log=self._log,
        )
        return bool(result.ok), (
            result.message or ("解绑成功" if result.ok else "解绑失败")
        )

    def reverify(self) -> tuple[bool, str]:
        """
        Re-check current session against server.

        Failure should clear session at caller.
        @author by ak
        """
        if not self.session.logged_in:
            return False, "未登录"
        if self.session.local_mock_card:
            expiry = self.session.expire_at
            if expiry is not None and datetime.now().astimezone() >= expiry:
                return False, "本地测试卡已到期，请重新登录"
            return True, self.session.expire_text()
        k = (self.session.key or "").strip()
        if not k:
            return False, "未登录"
        mc = self._machine_code()
        result = verify_license(
            k,
            mc,
            base_url=self.base_url,
            secret=self.secret,
            log=self._log,
        )
        if not result.ok:
            return False, result.message or "卡密校验失败"
        token = str(result.token or "").strip()
        if not token:
            return False, "登录服务未返回令牌，请重新登录"
        expire_at = _parse_expires_at(result.expires_at)
        self.session.token = token
        self.session.expire_at = expire_at
        self.session.display_name = "已授权"
        return True, self.session.expire_text()

    def logout(self) -> None:
        """Clear login state (cached key kept for next prefill). @author by ak"""
        self.session = AuthSession(key="")

    @property
    def is_logged_in(self) -> bool:
        """True after successful login. @author by ak"""
        return bool(self.session.logged_in)

    @property
    def cloud_control_allowed(self) -> bool:
        """Local mock-card sessions must never create a cloud-control connection."""
        return bool(self.session.logged_in and not self.session.local_mock_card)

    @property
    def auto_loot_allowed(self) -> bool:
        """Whether the automatic chest feature is accessible. @author by ak"""
        return bool(self.session.logged_in)

    def reverify_interval_s(self) -> float:
        """Polling interval for background re-check. @author by ak"""
        if self.session.local_mock_card and self.session.expire_at is not None:
            remaining = (
                self.session.expire_at - datetime.now().astimezone()
            ).total_seconds()
            return max(5.0, min(60.0, remaining))
        return float(license_reverify_interval_s())
