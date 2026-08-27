# -*- coding: utf-8 -*-
"""
Game client profile: standard (installed/ideal) vs green_compat (portable).

Do NOT classify by fixed drive letters, download folders, or pack brand paths.
Users may place either client or the helper anywhere. Game file trees are often
nearly identical; distinguish by install registration / managed platform, not
by copied content like patcher or gameinstallconfig.xml.

@author by ak
"""
from __future__ import annotations

import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# Written into shm.mode before inject so native attach can branch without
# env vars or a second DLL. Standard keeps mode=0 for business cmds later.
ATTACH_MODE_STANDARD = 0
ATTACH_MODE_GREEN_SAFE = 0x4743  # 'GC'


@dataclass(frozen=True)
class ClientProfile:
    """
    Runtime profile for one xajh process.

    @author by ak
    """

    name: str  # "standard" | "green_compat"
    attach_mode: int
    allow_native_enum: bool
    shm_wait_tries: int
    shm_wait_soft_s: float
    title_extra: tuple[str, ...]
    path_hits: tuple[str, ...]
    reason: str = ""

    @property
    def is_green(self) -> bool:
        """True for portable / non-registered clients. @author by ak"""
        return self.name == "green_compat"


# Product tokens in uninstall DisplayName / key (not filesystem locations).
_PRODUCT_TOKENS: tuple[str, ...] = (
    "笑傲江湖",
    "笑傲江湖ol",
    "xajh",
    "xajhol",
)

# Managed install roots by folder name only (no drive letter).
_MANAGED_ROOT_NAMES: tuple[str, ...] = (
    "wegameapps",
    "steamapps",
    "program files",
    "program files (x86)",
)


def _norm(s: str) -> str:
    """Lowercase path-ish string for token match. @author by ak"""
    return (s or "").replace("/", "\\").lower()


def _game_root_from_exe(exe_path: str) -> Path | None:
    """
    Resolve client root from xajh.exe path.

    Typical layout: <root>\\bin\\xajh.exe -> <root>
    @author by ak
    """
    if not exe_path:
        return None
    try:
        p = Path(exe_path).resolve()
    except Exception:
        try:
            p = Path(exe_path)
        except Exception:
            return None
    if p.parent.name.lower() == "bin":
        return p.parent.parent
    return p.parent


def _path_under(root: Path, child: Path) -> bool:
    """True if child is root or under root. @author by ak"""
    try:
        child.resolve().relative_to(root.resolve())
        return True
    except Exception:
        try:
            rn = _norm(str(root)).rstrip("\\")
            cn = _norm(str(child)).rstrip("\\")
            return cn == rn or cn.startswith(rn + "\\")
        except Exception:
            return False


def _iter_uninstall_entries() -> Iterable[tuple[str, str, str]]:
    """
    Yield (display_name, install_location, key_name) from Uninstall keys.

    @author by ak
    """
    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    bases = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    for hive in hives:
        for base in bases:
            try:
                root = winreg.OpenKey(hive, base)
            except OSError:
                continue
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                    i += 1
                except OSError:
                    break
                try:
                    sub = winreg.OpenKey(root, name)

                    def gv(n: str) -> str:
                        try:
                            return str(winreg.QueryValueEx(sub, n)[0] or "")
                        except OSError:
                            return ""

                    display = gv("DisplayName")
                    loc = gv("InstallLocation") or gv("InstallPath")
                    icon = gv("DisplayIcon")
                    if not loc and icon:
                        loc = icon.split(",", 1)[0].strip().strip('"')
                        try:
                            loc_p = Path(loc)
                            if loc_p.suffix.lower() == ".exe":
                                loc_p = loc_p.parent
                            if loc_p.name.lower() == "bin":
                                loc_p = loc_p.parent
                            loc = str(loc_p)
                        except Exception:
                            pass
                    yield display, loc, name
                    winreg.CloseKey(sub)
                except OSError:
                    continue
            try:
                winreg.CloseKey(root)
            except OSError:
                pass


def _registry_install_match(game_root: Path) -> tuple[bool, str]:
    """
    True if an uninstall entry covers this game root (any drive letter).

    @author by ak
    """
    if game_root is None:
        return False, ""
    root_n = _norm(str(game_root)).rstrip("\\")
    for display, loc, key in _iter_uninstall_entries():
        blob = _norm(f"{display}|{key}")
        productish = any(t in blob for t in _PRODUCT_TOKENS) or (
            "wegame" in blob or "完美" in blob
        )
        if not productish and not loc:
            continue
        if not loc:
            continue
        try:
            loc_p = Path(loc)
            if _path_under(loc_p, game_root) or _path_under(game_root, loc_p):
                return True, f"registry covers root key={key!r}"
            if root_n and root_n in _norm(loc).rstrip("\\"):
                return True, f"registry path contains root key={key!r}"
        except Exception:
            continue
    return False, ""


def _has_uninstaller(game_root: Path) -> tuple[bool, str]:
    """
    True if root has a real uninstaller binary (install footprint).

    Green packs usually omit uninstall.exe / unins000.exe even when they
    copy patcher + gameinstallconfig.xml.
    @author by ak
    """
    if game_root is None or not game_root.is_dir():
        return False, ""
    try:
        for name in ("uninstall.exe", "unins000.exe", "Uninstall.exe"):
            if (game_root / name).is_file():
                return True, f"uninstaller={name}"
        for p in game_root.glob("unins*.exe"):
            return True, f"uninstaller={p.name}"
    except Exception:
        return False, ""
    return False, ""


def _managed_platform_root(game_root: Path) -> tuple[bool, str]:
    """
    True if a path segment is a managed platform folder name.

    Folder names only — never drive letters or download sites.
    @author by ak
    """
    if game_root is None:
        return False, ""
    try:
        parts = [_norm(x) for x in game_root.parts]
    except Exception:
        return False, ""
    for seg in parts:
        for name in _MANAGED_ROOT_NAMES:
            if seg == name:
                return True, f"managed segment={seg!r}"
    return False, ""


def detect_client_profile(
    *,
    exe_path: str = "",
    title: str = "",
    window_class: str = "",
) -> ClientProfile:
    """
    Classify install vs portable green without fixed paths.

    Prefer registry + managed platform + uninstaller presence.
    Do not use brand path strings or netdisk folders.
    Copied game files (patcher, gameinstallconfig) are ignored as weak.

    @author by ak
    """
    del title, window_class
    root = _game_root_from_exe(exe_path)
    reasons: list[str] = []
    score = 0

    if root is not None:
        reg_ok, reg_why = _registry_install_match(root)
        if reg_ok:
            score += 3
            reasons.append(reg_why)

        managed_ok, managed_why = _managed_platform_root(root)
        if managed_ok:
            score += 2
            reasons.append(managed_why)

        un_ok, un_why = _has_uninstaller(root)
        if un_ok:
            score += 2
            reasons.append(un_why)

    # score >= 2: registered install or managed platform (or uninstaller).
    if score >= 2:
        return ClientProfile(
            name="standard",
            attach_mode=ATTACH_MODE_STANDARD,
            allow_native_enum=True,
            shm_wait_tries=50,
            shm_wait_soft_s=4.0,
            title_extra=(),
            path_hits=tuple(reasons),
            reason="installed/managed: " + "; ".join(reasons),
        )

    greason = "portable/green: no install registration"
    if root is not None:
        greason += f" root={root}"
    elif exe_path:
        greason += f" exe={exe_path}"
    if reasons:
        greason += " weak=" + ";".join(reasons)
    return ClientProfile(
        name="green_compat",
        attach_mode=ATTACH_MODE_GREEN_SAFE,
        allow_native_enum=False,
        shm_wait_tries=80,
        shm_wait_soft_s=8.0,
        title_extra=(),
        path_hits=tuple(reasons),
        reason=greason,
    )


def profile_from_proc(
    pid: int,
    *,
    exe_path: str = "",
    title: str = "",
    window_class: str = "",
) -> ClientProfile:
    """
    Detect profile; fill exe_path from process if omitted.

    @author by ak
    """
    exe = exe_path or ""
    if not exe and pid:
        try:
            import psutil

            exe = psutil.Process(int(pid)).exe() or ""
        except Exception:
            exe = ""
    return detect_client_profile(
        exe_path=exe, title=title, window_class=window_class
    )


def game_root_for_log(exe_path: str) -> str:
    """
    Game root string for inject logs (empty if unknown).

    @author by ak
    """
    root = _game_root_from_exe(exe_path)
    return str(root) if root is not None else ""
