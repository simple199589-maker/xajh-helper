# -*- coding: utf-8 -*-
"""
User login card-key (卡密) client: verify + machine unbind + HMAC sign.

POST /api/license/verify
POST /api/license/unbind

Headers (required):
  X-API-Key = business identity for this domain = the card_key itself
  X-Timestamp / X-Nonce / X-Signature = optional HMAC with LICENSE_API_SECRET

Note: X-API-Key is NOT a fixed packaged secret. Other domains use their own
user-filled keys (e.g. identify uses captcha key; cloud uses settings key).

@author by ak
"""
from __future__ import annotations

import hashlib
import hmac
import json
import platform
import re
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from app.core.build_profile import (
    default_license_api_secret,
    default_license_base_url,
)

LogFn = Callable[[str], None]

DEFAULT_LICENSE_BASE_URL = default_license_base_url()
DEFAULT_LICENSE_API_SECRET = default_license_api_secret()
DEFAULT_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) game-get/license",
    "Accept": "application/json, */*",
    "Content-Type": "application/json; charset=utf-8",
}


def mask_card_key(card_key: str) -> str:
    """Short safe key hint for logs (never full card). @author by ak"""
    k = (card_key or "").strip()
    if not k:
        return "-"
    if len(k) <= 8:
        return k[:2] + "…" if len(k) > 2 else "***"
    return f"{k[:4]}…{k[-4:]}"


def mask_machine_code(code: str) -> str:
    """Short machine_code hint for logs. @author by ak"""
    c = (code or "").strip()
    if not c:
        return "-"
    if len(c) <= 12:
        return c[:4] + "…"
    return f"{c[:6]}…{c[-4:]}"


def _reg_machine_guid() -> str:
    """Windows MachineGuid when available. @author by ak"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(val or "").strip()
    except Exception:
        return ""


def compute_machine_code() -> str:
    """
    Stable per-machine code: SHA-256 of hardware-ish fingerprint.

    @author by ak
    """
    parts = [
        _reg_machine_guid(),
        platform.node() or "",
        platform.system() or "",
        platform.machine() or "",
        platform.processor() or "",
    ]
    raw = "|".join(p.strip() for p in parts if str(p).strip())
    if not raw:
        raw = "unknown-host"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def build_license_signature_headers(
    method: str,
    path: str,
    raw_body: bytes,
    secret: str,
    *,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """
    Build X-Timestamp / X-Nonce / X-Signature for license API.

    string_to_sign =
      {METHOD}\\n{PATH}\\n{timestamp}\\n{nonce}\\n{sha256_hex(raw_body)}
    signature = HMAC_SHA256(secret, string_to_sign) as lowercase hex

    @author by ak
    """
    sec = (secret or "").strip()
    if not sec:
        raise ValueError("missing license api secret")
    m = (method or "POST").strip().upper() or "POST"
    p = path if str(path).startswith("/") else "/" + str(path or "")
    ts = str(timestamp if timestamp is not None else int(time.time()))
    n = str(nonce if nonce is not None else secrets.token_hex(16))
    body = raw_body if isinstance(raw_body, (bytes, bytearray)) else b""
    body_hash = hashlib.sha256(bytes(body)).hexdigest()
    string_to_sign = f"{m}\n{p}\n{ts}\n{n}\n{body_hash}"
    sign = hmac.new(
        sec.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Timestamp": ts,
        "X-Nonce": n,
        "X-Signature": sign,
    }


def build_token_signature_headers(
    method: str,
    path: str,
    raw_body: bytes,
    token: str,
    *,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Build request signature headers for an authenticated business token."""
    return build_license_signature_headers(
        method,
        path,
        raw_body,
        token,
        timestamp=timestamp,
        nonce=nonce,
    )


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout_s: float = 10.0,
) -> tuple[int, dict | None, str | None]:
    """
    Minimal JSON HTTP helper (stdlib).

    Returns (status, json_or_none, error).
    @author by ak
    """
    merged = dict(DEFAULT_HTTP_HEADERS)
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, data=body, method=method.upper())
    for k, v in merged.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            raw = resp.read()
            if not raw:
                return status, {}, None
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception as e:
                return status, None, f"json decode: {e}"
            if not isinstance(data, dict):
                return status, None, "response not object"
            return status, data, None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            data = json.loads(raw.decode("utf-8", errors="replace")) if raw else None
        except Exception:
            data = None
        msg = None
        if isinstance(data, dict):
            msg = str(data.get("message") or data.get("error") or "").strip()
        return int(e.code), data if isinstance(data, dict) else None, msg or str(e)
    except TimeoutError as e:
        return 0, None, f"timeout after {timeout_s}s: {e}"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        rs = str(reason)
        low = rs.lower()
        if "timed out" in low or "timeout" in low:
            return 0, None, f"timeout after {timeout_s}s: {rs}"
        return 0, None, f"url error: {rs}"
    except Exception as e:
        es = str(e)
        low = es.lower()
        if "timed out" in low or "timeout" in low:
            return 0, None, f"timeout after {timeout_s}s: {es}"
        return 0, None, es


def is_soft_license_network_failure(
    message: str | None = None,
    *,
    http_status: int | None = None,
) -> bool:
    """
    True for transport/timeout style failures that should NOT force-logout immediately.

    Real auth rejections (expired key, unbound machine, 401/403 body, etc.) stay hard.
    @author by ak
    """
    try:
        hs = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        hs = None
    # Explicit HTTP error from server = hard (except rare gateway 502/503/504).
    if hs is not None and hs > 0:
        if hs in (502, 503, 504, 408, 429):
            return True
        # 200 with business fail is hard; 401/403/404 hard.
        if hs >= 400:
            return False
    msg = str(message or "").strip()
    low = msg.lower()
    soft_cn = (
        "连接超时",
        "无法连接",
        "网络异常",
        "稍后重试",
        "返回异常",
        "授权服务返回异常",
    )
    if any(s in msg for s in soft_cn):
        return True
    if "timed out" in low or "timeout" in low:
        return True
    if "connection refused" in low or "url error" in low:
        return True
    if "json decode" in low or "response not object" in low:
        return True
    # No HTTP status + generic fail often transport.
    if (hs is None or hs == 0) and msg and not any(
        k in msg for k in ("卡密", "过期", "失效", "未授权", "解绑", "机器", "禁用", "封禁")
    ):
        # only if looks like network-ish
        if any(k in low for k in ("network", "connect", "socket", "reset", "unreachable")):
            return True
    return False


def local_network_message(error: str | None) -> str:
    """Human local transport error for license calls. @author by ak"""
    raw = str(error or "").strip()
    low = raw.lower()
    if not raw:
        return "网络异常，请稍后重试"
    if "timed out" in low or "timeout" in low:
        return "连接超时，请检查网络后重试"
    if "connection refused" in low or "url error" in low or "urlopen" in low:
        return "无法连接授权服务，请检查网络后重试"
    if "json decode" in low or "response not object" in low:
        return "授权服务返回异常，请稍后重试"
    # Keep short; avoid dumping English stack to users.
    if re.search(r"[A-Za-z]{4,}", raw) and "卡" not in raw and "密" not in raw:
        return "网络异常，请稍后重试"
    return raw


@dataclass
class LicenseResult:
    """Unified verify/unbind result. @author by ak"""

    ok: bool
    message: str = ""
    status: str | None = None
    expires_at: str | None = None
    machine_bound: bool | None = None
    machine_code: str | None = None
    token: str | None = None
    token_expires_at: str | None = None
    http_status: int | None = None
    raw: dict | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_result(
    http_status: int,
    data: dict | None,
    transport_err: str | None,
    *,
    success_default_msg: str,
) -> LicenseResult:
    """Map HTTP/json to LicenseResult; prefer server message. @author by ak"""
    if transport_err and (not data or http_status == 0):
        return LicenseResult(
            ok=False,
            message=local_network_message(transport_err),
            http_status=http_status or None,
            raw=data,
        )
    payload = data if isinstance(data, dict) else {}
    code = payload.get("code")
    msg = str(payload.get("message") or transport_err or "").strip()
    body = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    # Prefer explicit ok flag when present.
    if int(http_status or 0) == 200 and code == 0:
        if body.get("ok") is True:
            ok = True
        elif body.get("ok") is False:
            ok = False
        else:
            ok = True
    else:
        ok = False
    if ok:
        return LicenseResult(
            ok=True,
            message=msg or success_default_msg,
            status=str(body.get("status") or "") or None,
            expires_at=str(body.get("expires_at") or "") or None,
            machine_bound=(
                bool(body["machine_bound"])
                if "machine_bound" in body
                else None
            ),
            machine_code=(
                str(body.get("machine_code"))
                if body.get("machine_code") is not None
                else None
            ),
            token=str(body.get("token") or "").strip() or None,
            token_expires_at=str(body.get("token_expires_at") or "").strip()
            or None,
            http_status=http_status,
            raw=payload,
            detail=dict(body),
        )
    if not msg:
        msg = local_network_message(transport_err) if transport_err else "操作失败"
    return LicenseResult(
        ok=False,
        message=msg,
        status=str(body.get("status") or "") or None,
        expires_at=str(body.get("expires_at") or "") or None,
        machine_bound=(
            bool(body["machine_bound"]) if "machine_bound" in body else None
        ),
        machine_code=(
            str(body.get("machine_code"))
            if body.get("machine_code") is not None
            else None
        ),
        http_status=http_status or None,
        raw=payload if payload else data,
        detail=dict(body) if body else {},
    )


def _post_license(
    path: str,
    payload: dict[str, str],
    *,
    base_url: str,
    secret: str,
    api_key: str,
    timeout_s: float,
    log: LogFn,
    success_default_msg: str,
) -> LicenseResult:
    """
    POST signed license JSON.

    api_key is the domain X-API-Key (for license = card_key).
    @author by ak
    """
    key = (api_key or "").strip()
    if not key:
        return LicenseResult(
            ok=False,
            message="缺少 API Key（卡密）",
        )
    sec = (secret or "").strip() or DEFAULT_LICENSE_API_SECRET
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    headers: dict[str, str] = {}
    if sec:
        try:
            headers.update(build_license_signature_headers("POST", path, raw, sec))
        except Exception:
            return LicenseResult(ok=False, message="授权签名生成失败")
    headers["X-API-Key"] = key
    url = base_url.rstrip("/") + path
    status, data, err = _http_json(
        "POST",
        url,
        headers=headers,
        body=raw,
        timeout_s=float(timeout_s),
    )
    return _parse_result(
        status, data, err, success_default_msg=success_default_msg
    )


def verify_license(
    card_key: str,
    machine_code: str,
    *,
    base_url: str = DEFAULT_LICENSE_BASE_URL,
    secret: str = "",
    timeout_s: float = 10.0,
    log: LogFn | None = None,
) -> LicenseResult:
    """
    POST /api/license/verify.

    X-API-Key = card_key (user/login domain business key).
    @author by ak
    """
    log = log or (lambda _m: None)
    ck = (card_key or "").strip()
    mc = (machine_code or "").strip()
    if not ck:
        return LicenseResult(ok=False, message="请输入卡密")
    if not mc:
        return LicenseResult(ok=False, message="无法获取本机标识，请重试")
    path = "/api/license/verify"
    log(
        f"license verify card={mask_card_key(ck)} machine={mask_machine_code(mc)}"
    )
    result = _post_license(
        path,
        {"card_key": ck, "machine_code": mc},
        base_url=base_url or DEFAULT_LICENSE_BASE_URL,
        secret=secret,
        api_key=ck,
        timeout_s=timeout_s,
        log=log,
        success_default_msg="登录成功",
    )
    log(
        f"license verify -> ok={result.ok} http={result.http_status} "
        f"msg={result.message!r}"
    )
    return result


def unbind_license(
    card_key: str,
    machine_code: str | None = None,
    *,
    base_url: str = DEFAULT_LICENSE_BASE_URL,
    secret: str = "",
    timeout_s: float = 10.0,
    log: LogFn | None = None,
) -> LicenseResult:
    """
    POST /api/license/unbind.

    X-API-Key = card_key (user/login domain business key).
    @author by ak
    """
    log = log or (lambda _m: None)
    ck = (card_key or "").strip()
    if not ck:
        return LicenseResult(ok=False, message="请输入卡密")
    payload: dict[str, str] = {"card_key": ck}
    mc = (machine_code or "").strip()
    if mc:
        payload["machine_code"] = mc
    path = "/api/license/unbind"
    log(
        f"license unbind card={mask_card_key(ck)} "
        f"machine={mask_machine_code(mc) if mc else '-'}"
    )
    result = _post_license(
        path,
        payload,
        base_url=base_url or DEFAULT_LICENSE_BASE_URL,
        secret=secret,
        api_key=ck,
        timeout_s=timeout_s,
        log=log,
        success_default_msg="解绑成功",
    )
    log(
        f"license unbind -> ok={result.ok} http={result.http_status} "
        f"msg={result.message!r}"
    )
    return result
