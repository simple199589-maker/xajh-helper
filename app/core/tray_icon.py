# -*- coding: utf-8 -*-
"""
System tray helper (pystray optional; falls back to iconify-only).

@author by ak
"""
from __future__ import annotations

import threading
from typing import Callable


class TrayController:
    """
    Background tray icon with hover tip and show/quit menu.

    Requires pystray + Pillow when available; otherwise no-op (caller still iconify).

    @author by ak
    """

    def __init__(
        self,
        *,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
        tip: str = "XAJH 助手",
        on_dev: Callable[[], None] | None = None,
        on_accounts: Callable[[], None] | None = None,
        icon_name: str = "xajh_helper",
        accent_rgb: tuple[int, int, int] | None = None,
    ) -> None:
        self._on_show = on_show
        self._on_quit = on_quit
        self._on_dev = on_dev
        self._on_accounts = on_accounts
        self._tip = tip
        self._icon_name = str(icon_name or "xajh_helper").strip() or "xajh_helper"
        # prod blue; non-prod defaults to amber so two trays are visually distinct
        self._accent_rgb = accent_rgb or (0, 120, 212)
        self._icon = None
        self._thread: threading.Thread | None = None
        self._available = False
        self._started = False

    @property
    def available(self) -> bool:
        """True if tray backend loaded. @author by ak"""
        return self._available

    def start(self) -> bool:
        """
        Create tray icon on a daemon thread.

        Returns False if pystray/Pillow missing.
        @author by ak
        """
        if self._started:
            return self._available
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            self._available = False
            self._started = True
            return False

        r, g, b = (int(self._accent_rgb[0]), int(self._accent_rgb[1]), int(self._accent_rgb[2]))
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((4, 4, 60, 60), fill=(r, g, b, 255))
        draw.rectangle((22, 18, 42, 46), fill=(255, 255, 255, 255))

        items = [
            pystray.MenuItem("打开登录页", lambda: self._on_show()),
            pystray.MenuItem("账号管理", lambda: self._on_accounts()),
        ]
        if self._on_dev is not None:
            items.append(
                pystray.MenuItem("开发者面板", lambda: self._on_dev())
            )
        items.append(pystray.MenuItem("退出", lambda: self._on_quit()))
        menu = pystray.Menu(*items)
        self._icon = pystray.Icon(self._icon_name, image, self._tip, menu)
        self._available = True
        self._started = True

        def run() -> None:
            try:
                self._icon.run()
            except Exception:
                pass

        self._thread = threading.Thread(target=run, daemon=True, name="xajh-tray")
        self._thread.start()
        return True

    def update_tip(self, tip: str) -> None:
        """Update hover tooltip text. @author by ak"""
        self._tip = tip or "XAJH 助手"
        if self._icon is not None:
            try:
                self._icon.title = self._tip
            except Exception:
                pass

    def stop(self) -> None:
        """Stop tray icon. @author by ak"""
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:
            pass
        self._icon = None
