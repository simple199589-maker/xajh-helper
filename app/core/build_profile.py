# -*- coding: utf-8 -*-
"""
Build channel profile: dev vs prod packaging defaults.

- Source (unfrozen): treated as dev (debug on, captcha key from environment).
- Frozen + build_profile.json channel=dev: same as test package.
- Frozen + channel=prod (or missing profile): debug off, empty captcha key.

Pack with: tools\\build_gui.bat        (prod)
           tools\\build_gui.bat dev    (test)

@author by ak
"""
from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

_DEFAULT_CAPTCHA_BASE_URL = "https://xiaoao.joini.cloud"
_DEFAULT_LICENSE_BASE_URL = "https://xiaoao.joini.cloud"

# Session re-verify interval (seconds).
_REVERIFY_INTERVAL_PROD_S = 30 * 60
_REVERIFY_INTERVAL_DEV_S = 30 * 60


def _ensure_dotenv() -> None:
    """Load repo .env for unfrozen runs only."""
    if is_frozen():
        return
    try:
        from common.dotenv_load import ensure_dotenv_loaded

        ensure_dotenv_loaded()
    except Exception:
        pass


def _captcha_key_from_env() -> str:
    """Process env (after optional source-tree .env load)."""
    _ensure_dotenv()
    return str(os.environ.get("XAJH_CAPTCHA_API_KEY") or "").strip()


def _captcha_base_url_from_env() -> str:
    """Optional override: XAJH_CAPTCHA_BASE_URL (process env / .env)."""
    _ensure_dotenv()
    return str(os.environ.get("XAJH_CAPTCHA_BASE_URL") or "").strip().rstrip("/")


def _license_base_url_from_env() -> str:
    """Optional override: XAJH_LICENSE_BASE_URL (mainly dev/local)."""
    _ensure_dotenv()
    return str(os.environ.get("XAJH_LICENSE_BASE_URL") or "").strip().rstrip("/")


def _license_api_secret_from_env() -> str:
    """
    HMAC secret shared with server LICENSE_API_SECRET.

    Accepts XAJH_LICENSE_API_SECRET or LICENSE_API_SECRET.
    """
    _ensure_dotenv()
    return (
        str(os.environ.get("XAJH_LICENSE_API_SECRET") or "").strip()
        or str(os.environ.get("LICENSE_API_SECRET") or "").strip()
    )


def _resolve_captcha_base_url(profile_url: str = "") -> str:
    """
    Priority: env/.env → profile → built-in default.

    @author by ak
    """
    return (
        _captcha_base_url_from_env()
        or str(profile_url or "").strip().rstrip("/")
        or _DEFAULT_CAPTCHA_BASE_URL
    )


def _resolve_license_base_url(profile_url: str = "", *, captcha_url: str = "") -> str:
    """
    License API base: env → profile → captcha/service base → built-in.

    Same host as identify/cloud by default (one env XAJH_CAPTCHA_BASE_URL is enough).
    Optional XAJH_LICENSE_BASE_URL only when license is split out.
    @author by ak
    """
    return (
        _license_base_url_from_env()
        or str(profile_url or "").strip().rstrip("/")
        or str(captcha_url or "").strip().rstrip("/")
        or _captcha_base_url_from_env()
        or _DEFAULT_LICENSE_BASE_URL
    )


def _license_api_secret_from_pack() -> str:
    """
    Package-embedded HMAC secret (generated at build into _pack_secret.py).

    Never read from build_profile.json — that file is user-visible plaintext.
    @author by ak
    """
    try:
        from app.core._pack_secret import license_api_secret as _pack_secret

        return str(_pack_secret() or "").strip()
    except Exception:
        return ""


def _resolve_license_api_secret(profile_secret: str = "") -> str:
    """
    License HMAC secret: env (dev) → package embed.

    profile_secret arg is ignored (legacy JSON must not hold this secret).
    @author by ak
    """
    _ = profile_secret  # deliberately ignored
    return (
        _license_api_secret_from_env()
        or _license_api_secret_from_pack()
    )


def is_frozen() -> bool:
    """
    True when running as a packaged binary.

    @author by ak
    """
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def _profile_candidates() -> list[Path]:
    """
    Paths that may hold build_profile.json.

    @author by ak
    """
    out: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(Path(meipass) / "app" / "data" / "build_profile.json")
    try:
        from common.paths import app_root, bundle_root

        out.append(bundle_root() / "app" / "data" / "build_profile.json")
        out.append(app_root() / "app" / "data" / "build_profile.json")
    except Exception:
        pass
    # Source tree: repo app/data next to this package
    out.append(Path(__file__).resolve().parents[1] / "data" / "build_profile.json")
    return out


def _read_profile_file() -> dict[str, Any]:
    """
    Load first existing build_profile.json.

    @author by ak
    """
    for p in _profile_candidates():
        try:
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data["_profile_path"] = str(p)
                    return data
        except Exception:
            continue
    return {}


@lru_cache(maxsize=1)
def load_profile() -> dict[str, Any]:
    """
    Resolved build profile (cached).

    Env XAJH_BUILD_CHANNEL=dev|prod overrides file/channel for tests.
    @author by ak
    """
    _ensure_dotenv()
    env = (os.environ.get("XAJH_BUILD_CHANNEL") or "").strip().lower()
    if env in ("dev", "test"):
        return {
            "channel": "dev",
            "debug_default": True,
            "captcha_base_url": _resolve_captcha_base_url(),
            "license_base_url": _resolve_license_base_url(),
            "license_api_secret": _resolve_license_api_secret(),
            "captcha_api_key": _captcha_key_from_env(),
            "_source": "env",
        }
    if env in ("prod", "production", "release"):
        return {
            "channel": "prod",
            "debug_default": False,
            "captcha_base_url": _resolve_captcha_base_url(),
            "license_base_url": _resolve_license_base_url(),
            "license_api_secret": _resolve_license_api_secret(),
            "captcha_api_key": "",
            "_source": "env",
        }

    file_data = _read_profile_file()
    if file_data:
        ch = str(file_data.get("channel") or "prod").strip().lower()
        is_dev = ch == "dev"
        env_key = _captcha_key_from_env()
        # A dev package may explicitly embed the build terminal's key in its
        # generated (ignored) profile. Production profiles never supply one.
        profile_key = (
            str(file_data.get("captcha_api_key") or "").strip()
            if is_dev
            else ""
        )
        key = env_key or profile_key
        dbg = file_data.get("debug_default")
        if dbg is None:
            dbg = is_dev
        return {
            "channel": "dev" if is_dev else "prod",
            "debug_default": bool(dbg),
            "captcha_base_url": _resolve_captcha_base_url(
                str(file_data.get("captcha_base_url") or "")
            ),
            "license_base_url": _resolve_license_base_url(
                str(file_data.get("license_base_url") or ""),
                captcha_url=str(file_data.get("captcha_base_url") or ""),
            ),
            # license secret never from JSON file
            "license_api_secret": _resolve_license_api_secret(),
            "captcha_api_key": key,
            "_source": "file",
            "_profile_path": file_data.get("_profile_path"),
        }

    # No file: source tree → dev; frozen bare → prod (safe).
    if is_frozen():
        return {
            "channel": "prod",
            "debug_default": False,
            "captcha_base_url": _resolve_captcha_base_url(),
            "license_base_url": _resolve_license_base_url(),
            "license_api_secret": _resolve_license_api_secret(),
            "captcha_api_key": "",
            "_source": "frozen_default",
        }
    return {
        "channel": "dev",
        "debug_default": True,
        "captcha_base_url": _resolve_captcha_base_url(),
        "license_base_url": _resolve_license_base_url(),
        "license_api_secret": _resolve_license_api_secret(),
        "captcha_api_key": _captcha_key_from_env(),
        "_source": "source_default",
    }


def clear_profile_cache() -> None:
    """
    Drop cached profile (tests / after rewrite).

    @author by ak
    """
    load_profile.cache_clear()


def channel() -> str:
    """
    Current channel: 'dev' or 'prod'.

    @author by ak
    """
    return str(load_profile().get("channel") or "prod")


def is_dev_build() -> bool:
    """
    True for test/dev package or source runs.

    @author by ak
    """
    return channel() == "dev"


def is_prod_build() -> bool:
    """
    True for production package defaults.

    @author by ak
    """
    return not is_dev_build()


def default_debug_mode() -> bool:
    """
    Login「开启调试」default checked state.

    @author by ak
    """
    return bool(load_profile().get("debug_default"))


def default_captcha_base_url() -> str:
    """
    Default identify API base URL (env/.env → profile → built-in).

    @author by ak
    """
    return str(
        load_profile().get("captcha_base_url") or _DEFAULT_CAPTCHA_BASE_URL
    ).strip().rstrip("/") or _DEFAULT_CAPTCHA_BASE_URL


def default_license_base_url() -> str:
    """
    Login card-key API base URL (env / profile / same as captcha host).

    @author by ak
    """
    return str(
        load_profile().get("license_base_url")
        or load_profile().get("captcha_base_url")
        or _DEFAULT_LICENSE_BASE_URL
    ).strip().rstrip("/") or _DEFAULT_LICENSE_BASE_URL


def default_license_api_secret() -> str:
    """
    HMAC secret for license API signing (env → packaged profile).

    @author by ak
    """
    return str(load_profile().get("license_api_secret") or "").strip()


def default_captcha_api_key() -> str:
    """
    Default key: runtime env, then explicit dev generated profile.

    @author by ak
    """
    return str(load_profile().get("captcha_api_key") or "").strip()


def license_reverify_interval_s() -> float:
    """
    How often to re-check card-key while logged in.

    default ~30min (dev/prod same; or XAJH_LICENSE_REVERIFY_S override).
    @author by ak
    """
    _ensure_dotenv()
    raw = str(os.environ.get("XAJH_LICENSE_REVERIFY_S") or "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except Exception:
            pass
    return float(
        _REVERIFY_INTERVAL_DEV_S if is_dev_build() else _REVERIFY_INTERVAL_PROD_S
    )




def profile_summary() -> str:
    """
    One-line profile for logs.

    @author by ak
    """
    p = load_profile()
    key = str(p.get("captcha_api_key") or "")
    key_hint = "set" if key else "empty"
    sec_hint = "set" if str(p.get("license_api_secret") or "").strip() else "empty"
    return (
        f"channel={p.get('channel')} debug_default={p.get('debug_default')} "
        f"captcha_key={key_hint} license={p.get('license_base_url')} "
        f"license_secret={sec_hint} source={p.get('_source')}"
    )
