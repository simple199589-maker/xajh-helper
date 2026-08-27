# -*- coding: utf-8 -*-
"""
HTTP client for local 九层妖楼 same-object captcha API.

API: POST /api/identify/image (X-API-Key + Key HMAC; no login token)
     POST /api/identify/feedback

@author by ak
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Callable

LogFn = Callable[[str], None]

# Base URL is shared; API key comes from build channel (dev filled / prod empty).
from app.core.build_profile import (
    default_captcha_api_key as _profile_captcha_key,
    default_captcha_base_url as _profile_captcha_url,
)
from app.core.license_client import build_token_signature_headers

DEFAULT_CAPTCHA_BASE_URL = _profile_captcha_url()
# Dev/test package or source: prefilled. Production package: "".
DEFAULT_CAPTCHA_API_KEY = _profile_captcha_key()
# CDN / WAF often blocks bare urllib without UA (403).
DEFAULT_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) game-get/yaolu",
    "Accept": "application/json, */*",
}


def api_key_fingerprint(api_key: str) -> str:
    """Return a non-reversible hint suitable for request diagnostics."""
    key = str(api_key or "").strip()
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] if key else "empty"
    return f"len={len(key)} sha256={digest}"


def api_key_header_error(api_key: str) -> str | None:
    """Validate that the key can be transported as an HTTP header value."""
    key = str(api_key or "").strip()
    if not key:
        return "未配置答题专用 Key"
    try:
        key.encode("latin-1")
    except UnicodeEncodeError:
        return "答题专用 Key 包含中文或其他非法字符，请只粘贴 Key 本身"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
        return "答题专用 Key 包含换行或控制字符，请只粘贴 Key 本身"
    return None


@dataclass
class CaptchaIdentifyResult:
    """Identify response for 8-grid animal captcha. @author by ak"""

    ok: bool
    positions: list[int] = field(default_factory=list)
    click_centers: list[list[float]] = field(default_factory=list)
    animal: str | None = None
    confidence: float | None = None
    identify_id: str | None = None
    top_pairs: list[dict] = field(default_factory=list)
    raw: dict | None = None
    error: str | None = None
    http_status: int | None = None
    # UTF-16 toast copy counts snapshot taken right before confirm click.
    answer_baseline: dict[str, int] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CaptchaFeedbackResult:
    """Feedback response. @author by ak"""

    ok: bool
    identify_id: str | None = None
    correct: bool | None = None
    error: str | None = None
    raw: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout_s: float = 30.0,
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
            msg = str(data.get("message") or data.get("error") or "")
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


def healthz(
    base_url: str = DEFAULT_CAPTCHA_BASE_URL,
    *,
    timeout_s: float = 3.0,
) -> bool:
    """True if captcha API /healthz is ok. @author by ak"""
    url = base_url.rstrip("/") + "/healthz"
    status, data, err = _http_json("GET", url, timeout_s=timeout_s)
    if err or status != 200:
        return False
    return bool(data and data.get("ok") is True)


def _api_error_text(
    status: int,
    data: dict | None,
    fallback: str | None = None,
) -> str:
    """Keep the HTTP status visible when the API returns an error payload."""
    message = ""
    code = None
    if isinstance(data, dict):
        message = str(data.get("message") or data.get("error") or "").strip()
        code = data.get("code")
    detail = message or (f"code={code}" if code not in (None, "") else "")
    if not detail:
        detail = str(fallback or "request failed").strip()
    if int(status or 0) >= 400:
        return f"HTTP {int(status)}: {detail}"
    return detail


def identify_image(
    png_bytes: bytes,
    *,
    api_key: str = DEFAULT_CAPTCHA_API_KEY,
    login_token: str = "",
    base_url: str = DEFAULT_CAPTCHA_BASE_URL,
    timeout_s: float = 45.0,
    log: LogFn | None = None,
) -> CaptchaIdentifyResult:
    """
    Submit PNG screenshot; return two matching cell positions (1-8).

    @author by ak
    """
    log = log or (lambda _m: None)
    if not png_bytes:
        return CaptchaIdentifyResult(ok=False, error="empty image")
    key = (api_key or "").strip()
    key_error = api_key_header_error(key)
    if key_error:
        return CaptchaIdentifyResult(ok=False, error=key_error)
    url = base_url.rstrip("/") + "/api/identify/image"
    b64 = base64.b64encode(png_bytes).decode("ascii")
    body = json.dumps({"img": b64}, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": key,
    }
    headers.update(
        build_token_signature_headers(
            "POST", "/api/identify/image", body, key
        )
    )
    log(
        f"captcha identify POST {url} bytes={len(png_bytes)} "
        f"timeout={float(timeout_s):.0f}s key_hint=({api_key_fingerprint(key)}) "
        "token=not-sent signature=key-hmac"
    )
    t0 = time.monotonic()
    status, data, err = _http_json(
        "POST", url, headers=headers, body=body, timeout_s=timeout_s
    )
    elapsed = time.monotonic() - t0
    if int(status or 0) >= 400:
        error_text = _api_error_text(status, data, err)
        code = data.get("code") if isinstance(data, dict) else None
        log(
            f"captcha identify API FAIL http={status} elapsed={elapsed:.1f}s "
            f"code={code!r} error={error_text!r}"
        )
        return CaptchaIdentifyResult(
            ok=False,
            error=error_text,
            raw=data,
            http_status=status,
        )
    if err and not data:
        log(f"captcha identify FAIL elapsed={elapsed:.1f}s err={err!r} http={status}")
        return CaptchaIdentifyResult(ok=False, error=err, http_status=status or None)
    if not data:
        log(f"captcha identify FAIL elapsed={elapsed:.1f}s empty response http={status}")
        return CaptchaIdentifyResult(
            ok=False, error=err or "empty response", http_status=status or None
        )
    log(f"captcha identify HTTP {status} elapsed={elapsed:.1f}s")
    code = data.get("code")
    if code not in (0, "0", None) and int(code or -1) != 0:
        error_text = _api_error_text(status, data, err)
        log(
            f"captcha identify API FAIL http={status} code={code!r} "
            f"error={error_text!r}"
        )
        return CaptchaIdentifyResult(
            ok=False,
            error=error_text,
            raw=data,
            http_status=status,
        )
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    positions = payload.get("positions") or []
    centers = payload.get("click_centers") or []
    try:
        positions = [int(p) for p in positions]
    except Exception:
        positions = []
    norm_centers: list[list[float]] = []
    for c in centers:
        try:
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                norm_centers.append([float(c[0]), float(c[1])])
        except Exception:
            continue
    conf = payload.get("confidence")
    try:
        conf_f = float(conf) if conf is not None else None
    except Exception:
        conf_f = None
    ok = len(positions) >= 2 or len(norm_centers) >= 2
    if not ok:
        return CaptchaIdentifyResult(
            ok=False,
            error=err or "no positions",
            raw=data,
            http_status=status,
            identify_id=str(payload.get("identify_id") or "") or None,
        )
    return CaptchaIdentifyResult(
        ok=True,
        positions=positions,
        click_centers=norm_centers,
        animal=str(payload.get("animal") or "") or None,
        confidence=conf_f,
        identify_id=str(payload.get("identify_id") or "") or None,
        top_pairs=list(payload.get("top_pairs") or []),
        raw=data,
        http_status=status,
    )


def send_feedback(
    identify_id: str,
    correct: bool,
    *,
    api_key: str = DEFAULT_CAPTCHA_API_KEY,
    login_token: str = "",
    base_url: str = DEFAULT_CAPTCHA_BASE_URL,
    timeout_s: float = 15.0,
    log: LogFn | None = None,
) -> CaptchaFeedbackResult:
    """
    Report whether identify result was correct in-game.

    @author by ak
    """
    log = log or (lambda _m: None)
    iid = (identify_id or "").strip()
    if not iid:
        return CaptchaFeedbackResult(ok=False, error="missing identify_id")
    key = (api_key or "").strip()
    key_error = api_key_header_error(key)
    if key_error:
        return CaptchaFeedbackResult(ok=False, identify_id=iid, error=key_error)
    url = base_url.rstrip("/") + "/api/identify/feedback"
    body = json.dumps(
        {"identify_id": iid, "correct": bool(correct)},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": key,
    }
    headers.update(
        build_token_signature_headers(
            "POST", "/api/identify/feedback", body, key
        )
    )
    log(
        f"captcha feedback id={iid} correct={bool(correct)} "
        f"key_hint=({api_key_fingerprint(key)}) "
        "token=not-sent signature=key-hmac"
    )
    status, data, err = _http_json(
        "POST", url, headers=headers, body=body, timeout_s=timeout_s
    )
    if err and not data:
        return CaptchaFeedbackResult(ok=False, identify_id=iid, error=err)
    if not data:
        return CaptchaFeedbackResult(
            ok=False, identify_id=iid, error=err or f"http={status}"
        )
    code = data.get("code")
    if code not in (0, "0", None) and int(code or -1) != 0:
        return CaptchaFeedbackResult(
            ok=False,
            identify_id=iid,
            error=str(data.get("message") or f"code={code}"),
            raw=data,
        )
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    return CaptchaFeedbackResult(
        ok=True,
        identify_id=str(payload.get("identify_id") or iid),
        correct=bool(payload.get("correct", correct)),
        raw=data,
    )
