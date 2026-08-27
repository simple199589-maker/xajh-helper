# -*- coding: utf-8 -*-
"""
Game window title helpers (inject / activity markers).

Idle mounted client:  ... [GUI]
While a feature runs: ... [捡箱子] / [妖楼] / [活跃] / combined [捡箱子·妖楼]

@author by ak
"""
from __future__ import annotations

import ctypes
import re
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

# Idle inject marker (uppercase as product display).
GUI_TITLE_SUFFIX = " [GUI]"
# Legacy lowercase still recognized for strip/has checks.
_LEGACY_GUI_SUFFIX = " [gui]"

# Feature page key -> short title tag on the game window.
ACTIVITY_LABELS: dict[str, str] = {
    "loot": "捡箱子",
    "yaolu": "妖楼",
    "activity": "副本",
    "task": "任务",
    "grocery": "杂货",
    "bg_key": "后台键",
    "fg_key": "前台键",
    "bg_shift": "准星",
    "bg_click_l": "左连点",
    "bg_click_r": "右连点",
    "bg_skill_cancel": "取消后摇",
}

_KNOWN_INNER = frozenset(
    {
        "GUI",
        "gui",
        "Gui",
        *ACTIVITY_LABELS.values(),
    }
)

# Trailing [...] whose inner is only known tags (single or ·/, joined).
_TRAILING_BRACKET_RE = re.compile(r"\s*\[([^\]]+)\]\s*$")
# Dynamic activity tags like 捡箱子×12 (count suffix on a known base label).
_DYNAMIC_COUNT_RE = re.compile(
    r"^(" + "|".join(re.escape(v) for v in ACTIVITY_LABELS.values()) + r")×\d+$"
)


def _is_known_tag_part(p: str) -> bool:
    """True if part is a known helper tag (optionally with ×count). @author by ak"""
    t = (p or "").strip()
    if not t:
        return False
    if t in _KNOWN_INNER or t.lower() == "gui":
        return True
    return bool(_DYNAMIC_COUNT_RE.match(t))


def get_window_title(hwnd: int) -> str:
    """Read window title text. @author by ak"""
    if not hwnd:
        return ""
    n = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
    return buf.value or ""


def set_window_title(hwnd: int, title: str) -> bool:
    """Set window title text. @author by ak"""
    if not hwnd:
        return False
    return bool(user32.SetWindowTextW(wintypes.HWND(hwnd), title or ""))


def strip_helper_markers_text(title: str) -> str:
    """
    Remove trailing helper markers ([GUI]/[gui]/[捡箱子]/[妖楼·活跃]…).

    Does not write to the window.
    @author by ak
    """
    t = (title or "").rstrip()
    if not t:
        return t
    while True:
        m = _TRAILING_BRACKET_RE.search(t)
        if not m:
            break
        inner = (m.group(1) or "").strip()
        parts = [p.strip() for p in re.split(r"[·,，/|]", inner) if p.strip()]
        if not parts:
            break
        if all(_is_known_tag_part(p) for p in parts):
            t = t[: m.start()].rstrip()
            continue
        break
    return t


def strip_gui_marker_text(title: str, suffix: str = GUI_TITLE_SUFFIX) -> str:
    """
    Remove inject/activity markers from a title string (no Win32 write).

    @author by ak
    """
    _ = suffix  # kept for call-site compat
    return strip_helper_markers_text(title)


def has_gui_title_suffix(title: str) -> bool:
    """
    True if title already carries idle [GUI]/[gui] or any activity marker.

    @author by ak
    """
    t = title or ""
    if not t:
        return False
    m = _TRAILING_BRACKET_RE.search(t.rstrip())
    if not m:
        # mid-string legacy
        return GUI_TITLE_SUFFIX in t or _LEGACY_GUI_SUFFIX in t
    inner = (m.group(1) or "").strip()
    parts = [p.strip() for p in re.split(r"[·,，/|]", inner) if p.strip()]
    return bool(parts) and all(_is_known_tag_part(p) for p in parts)


def ensure_gui_title_suffix(hwnd: int, suffix: str = GUI_TITLE_SUFFIX) -> str:
    """
    Append idle inject marker [GUI] if no helper marker is present.

    Does not clobber an activity tag already on the title.
    Returns the title after the operation.
    @author by ak
    """
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        return ""
    title = get_window_title(hwnd)
    if not title:
        return title
    if has_gui_title_suffix(title):
        return title
    mark = suffix or GUI_TITLE_SUFFIX
    if not mark.startswith(" "):
        mark = " " + mark
    new_title = f"{title.rstrip()}{mark}"
    set_window_title(hwnd, new_title)
    return get_window_title(hwnd) or new_title


def strip_gui_title_suffix(hwnd: int, suffix: str = GUI_TITLE_SUFFIX) -> str:
    """
    Best-effort restore title without any helper marker ([GUI]/activity).

    @author by ak
    """
    _ = suffix
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        return ""
    title = get_window_title(hwnd)
    if not title:
        return title
    restored = strip_helper_markers_text(title)
    if restored != title:
        set_window_title(hwnd, restored)
        return get_window_title(hwnd) or restored
    return title


def set_game_activity_markers(
    hwnd: int,
    labels: list[str] | tuple[str, ...] | None,
) -> str:
    """
    Set trailing helper tag from running activity labels.

    - labels empty/None -> idle `` [GUI]``
    - one or more -> `` [捡箱子]`` or `` [捡箱子·妖楼]``

    Base game title is preserved (markers stripped then re-appended).
    @author by ak
    """
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        return ""
    title = get_window_title(hwnd)
    if not title:
        return title
    base = strip_helper_markers_text(title)
    clean: list[str] = []
    seen: set[str] = set()
    for lab in labels or ():
        t = (lab or "").strip()
        if not t or t in seen:
            continue
        if t.lower() == "gui":
            continue
        seen.add(t)
        clean.append(t)
    if clean:
        suffix = " [" + "·".join(clean) + "]"
    else:
        suffix = GUI_TITLE_SUFFIX
    new_title = f"{base}{suffix}"
    if new_title != title:
        set_window_title(hwnd, new_title)
    return get_window_title(hwnd) or new_title


def activity_label_for_key(key: str) -> str | None:
    """
    Map feature page key to game-title activity tag.

    @author by ak
    """
    k = (key or "").strip().lower()
    return ACTIVITY_LABELS.get(k)


def parse_role_name_from_title(title: str) -> str | None:
    """
    Parse display role name from game window title (fallback only).

    Prefer plg GetHostPlayer+GetObjectName for live role name.
    Expected forms:
      "笑傲江湖OL - 角色名 服务器..."
      "笑傲江湖OL - 角色名 服务器... [GUI]"

    @author by ak
    """
    t = strip_helper_markers_text(title or "")
    if not t:
        return None
    if " - " not in t:
        return None
    # Green/launcher variants may append several server/channel segments. The
    # in-world character name is the final segment in those titles.
    rest = t.rsplit(" - ", 1)[1].strip()
    if not rest:
        return None
    name = rest.split()[0].strip()
    if not name or name in ("-", "xajh", "XAJH"):
        return None
    if len(name) > 24:
        name = name[:24]
    return name


def title_not_in_role(title: str) -> bool:
    """True when a game window title proves the game is not in-world yet.

    A bare client title (e.g. "笑傲江湖OL" with no " - " role segments) means
    the game is still at login / character-select. An empty title is
    inconclusive and returns False so callers fall back to a live probe.

    @author by ak
    """
    t = (title or "").strip()
    if not t:
        return False
    return parse_role_name_from_title(t) is None
