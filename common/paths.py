# -*- coding: utf-8 -*-
"""Shared path helpers for XAJH local client testing."""
from __future__ import annotations

import sys
from pathlib import Path

WEGAME_ROOT = Path(r"D:\WeGameApps")


def is_frozen() -> bool:
    """
    True when running as a frozen / packaged binary (PyInstaller etc.).

    @author by ak
    """
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def bundle_root() -> Path:
    """
    Read-only root for bundled code/data (PyInstaller _MEIPASS when frozen).

    @author by ak
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[1]


def app_root() -> Path:
    """
    Writable runtime root: directory of the exe when frozen, else repo root.

    @author by ak
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def user_data_root() -> Path:
    """
    Always-writable fallback under %LOCALAPPDATA%/game-get.

    Used when next-to-exe write fails (Program Files, no ACL, etc.).
    @author by ak
    """
    import os

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / "game-get"
    return Path.home() / "game-get"


def ensure_writable_dir(*parts: str, prefer: Path | None = None) -> Path:
    """
    Create and return a writable directory.

    Tries prefer (or app_root/parts), then user_data_root/parts, then cwd.
    @author by ak
    """
    candidates: list[Path] = []
    if prefer is not None:
        candidates.append(prefer)
    if parts:
        candidates.append(app_root().joinpath(*parts))
        candidates.append(user_data_root().joinpath(*parts))
    else:
        candidates.append(app_root())
        candidates.append(user_data_root())
    candidates.append(Path.cwd().joinpath(*parts) if parts else Path.cwd())

    seen: set[str] = set()
    for d in candidates:
        key = str(d.resolve()) if d.exists() else str(d)
        if key in seen:
            continue
        seen.add(key)
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return d
        except Exception:
            continue
    # Last resort: return first path even if not writable (caller logs).
    return candidates[0]


# Back-compat roots; runtime diagnostics stay beside the source/executable.
PROJECT_ROOT = app_root()
CAPTURE_DIR = PROJECT_ROOT / "captures"
ISSUE_DIR = PROJECT_ROOT / ".issues"
RECON_DIR = PROJECT_ROOT / "tools" / ".issues" / "recon"
APP_DATA_DIR = bundle_root() / "app" / "data"
NATIVE_BIN_DIR = bundle_root() / "native" / "bin"


def captcha_debug_dir() -> Path:
    """
    Captcha crop dir fixed beside the running source/executable.

    @author by ak
    """
    target = app_root() / "captures" / "captcha"
    target.mkdir(parents=True, exist_ok=True)
    return target


def issue_dir() -> Path:
    """
    Writable .issues dir next to exe, with LOCALAPPDATA fallback.

    @author by ak
    """
    return ensure_writable_dir(".issues", prefer=ISSUE_DIR)


def find_game_root() -> Path:
    """Locate the local XAJH install directory."""
    for p in WEGAME_ROOT.iterdir():
        if p.is_dir() and (p / "bin" / "xajh.exe").exists():
            return p
    raise FileNotFoundError("XAJH game root not found under D:\\WeGameApps")


def client_exe(game_root: Path | None = None) -> Path:
    root = game_root or find_game_root()
    return root / "bin" / "xajh.exe"


def launcher_candidates(game_root: Path | None = None) -> list[Path]:
    root = game_root or find_game_root()
    return sorted(root.glob("*.exe"), key=lambda p: p.stat().st_size, reverse=True)


def parse_server_address(value: str) -> tuple[str, int]:
    """Parse currentserver.ini style 'port:ip' into (ip, port)."""
    value = value.strip()
    if ":" not in value:
        raise ValueError(f"unsupported server address: {value}")
    left, right = value.split(":", 1)
    # format observed: 6598:193.112.93.158
    if left.isdigit() and any(c == "." for c in right):
        return right, int(left)
    # fallback ip:port
    if right.isdigit():
        return left, int(right)
    raise ValueError(f"unsupported server address: {value}")


def read_current_server(game_root: Path | None = None) -> dict:
    root = game_root or find_game_root()
    path = root / "userdata" / "currentserver.ini"
    text = path.read_text(encoding="gbk", errors="replace")
    data: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.strip().startswith("["):
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    addr = data.get("CurrentServerAddress")
    if addr:
        ip, port = parse_server_address(addr)
        data["_ip"] = ip
        data["_port"] = str(port)
    return data
