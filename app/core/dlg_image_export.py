# -*- coding: utf-8 -*-
"""
Export AUI dialog image without PrintWindow (avoids GDI hang).

Primary path:
  1) read AUIDialog rect from memory (+0x9C..+0xA8)
  2) call game CaptureScreen worker on UI thread via bridge (writes jpg under
     <game>/Screenshots/)
  3) crop dialog rect from that jpg (desktop client coords)

Fallback: BitBlt only (no PrintWindow) if bridge capture unavailable.

@author by ak
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable

from app.core.plg_exports import find_xajh_exe
from app.core.plg_ui import DlgRect, get_captcha_dlg_rect, read_dlg_rect
from app.core.win_capture import CaptureResult, get_client_rect_screen

LogFn = Callable[[str], None]

# Live note VA: CaptureScreen worker (saves %s\Screenshots\YYYY-mm-dd HH-MM-SS.jpg)
NOTE_VA_CAPTURE_SCREEN = 0x0082BBF0


@dataclass
class DlgImageExport:
    """
    One dialog image export from game/memory path.

    @author by ak
    """

    ok: bool
    png: bytes | None = None
    rect: DlgRect | None = None
    source_path: str | None = None
    method: str = ""
    client_w: int = 0
    client_h: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "png_len": len(self.png or b""),
            "rect": self.rect.to_dict() if self.rect else None,
            "source_path": self.source_path,
            "method": self.method,
            "client_w": self.client_w,
            "client_h": self.client_h,
            "error": self.error,
        }


def game_screenshots_dir(exe_path: str | Path | None = None) -> Path | None:
    """
    Resolve <game_root>/Screenshots.

    @author by ak
    """
    pe = find_xajh_exe(exe_path)
    if pe is None:
        return None
    # bin/xajh.exe -> game root parent of bin
    root = pe.parent.parent if pe.parent.name.lower() == "bin" else pe.parent
    for name in ("Screenshots", "screenshots"):
        d = root / name
        if d.is_dir():
            return d
    d = root / "Screenshots"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None


def _list_screenshot_files(folder: Path) -> list[Path]:
    files: list[Path] = []
    for pat in ("*.jpg", "*.jpeg", "*.png", "*.bmp"):
        files.extend(folder.glob(pat))
    files.sort(key=lambda p: p.stat().st_mtime)
    return files


def cleanup_game_screenshots(
    *,
    exe_path: str | Path | None = None,
    folder: Path | None = None,
    keep_newest: int = 0,
    older_than_s: float | None = None,
    log: LogFn | None = None,
) -> dict:
    """
    Delete files under game Screenshots to avoid disk fill.

    keep_newest: retain this many newest files (0 = delete all matching age).
    older_than_s: only delete files older than this many seconds; None = no age gate.
    Returns {ok, folder, removed, kept, bytes_freed, error}.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "folder": None,
        "removed": 0,
        "kept": 0,
        "bytes_freed": 0,
        "error": None,
    }
    try:
        d = Path(folder) if folder is not None else game_screenshots_dir(exe_path)
    except Exception as e:
        out["error"] = str(e)
        return out
    if d is None or not d.is_dir():
        out["error"] = "Screenshots dir missing"
        return out
    out["folder"] = str(d)
    files = _list_screenshot_files(d)
    if not files:
        out["ok"] = True
        return out
    keep_n = max(0, int(keep_newest))
    protect = set(files[-keep_n:]) if keep_n else set()
    now = time.time()
    age_gate = older_than_s is not None
    min_age = float(older_than_s or 0.0)
    removed = 0
    freed = 0
    for p in files:
        if p in protect:
            continue
        try:
            st = p.stat()
            if age_gate and (now - st.st_mtime) < min_age:
                continue
            sz = int(st.st_size)
            p.unlink(missing_ok=True)
            removed += 1
            freed += sz
        except Exception:
            continue
    out["ok"] = True
    out["removed"] = removed
    out["kept"] = len(files) - removed
    out["bytes_freed"] = freed
    log(
        f"cleanup Screenshots dir={d} removed={removed} "
        f"kept={out['kept']} freed={freed}"
    )
    return out


def delete_screenshot_file(path: str | Path | None, *, log: LogFn | None = None) -> bool:
    """
    Delete one CaptureScreen jpg after crop/use.

    @author by ak
    """
    log = log or (lambda _m: None)
    if not path:
        return False
    try:
        p = Path(path)
        if p.is_file():
            p.unlink(missing_ok=True)
            log(f"deleted screenshot {p.name}")
            return True
    except Exception as e:
        log(f"delete screenshot err: {e}")
    return False


def wait_new_screenshot(
    folder: Path,
    *,
    since_mtime: float,
    timeout_s: float = 4.0,
    poll_s: float = 0.15,
) -> Path | None:
    """
    Wait for a new file under Screenshots newer than since_mtime.

    @author by ak
    """
    deadline = time.time() + max(0.2, float(timeout_s))
    while time.time() < deadline:
        files = _list_screenshot_files(folder)
        for p in reversed(files):
            try:
                mt = p.stat().st_mtime
            except Exception:
                continue
            if mt > since_mtime + 1e-3 and p.stat().st_size > 1000:
                # allow writer to finish
                time.sleep(0.05)
                return p
        time.sleep(max(0.05, float(poll_s)))
    return None


def capture_screen_via_bridge(
    session,
    *,
    hwnd: int = 0,
    timeout_s: float = 5.0,
    log: LogFn | None = None,
) -> Path | None:
    """
    Trigger in-game CaptureScreen on UI thread; return new screenshot path.

    Requires bridge CMD_CAPTURE_SCREEN (worker @ 0x82BBF0).
    @author by ak
    """
    log = log or (lambda _m: None)
    folder = game_screenshots_dir(getattr(session, "exe_path", None))
    if folder is None:
        log("capture_screen: Screenshots dir missing")
        return None
    before = time.time()
    # also track newest existing
    existing = _list_screenshot_files(folder)
    if existing:
        before = max(before - 0.01, existing[-1].stat().st_mtime)

    try:
        from app.core.xajh_bridge import CMD_CAPTURE_SCREEN, ensure_bridge
    except Exception as e:
        log(f"capture_screen: bridge import fail {e}")
        return None

    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        log("capture_screen: no pid")
        return None
    # Reuse existing bridge; inject only if missing. Never force_reinject here —
    # captcha/loot loops call this while game may be minimized.
    br = ensure_bridge(
        pid,
        log=log,
        inject_if_needed=True,
        hwnd=hwnd or None,
        force_reinject=False,
    )
    if br is None:
        log("capture_screen: no bridge")
        return None
    try:
        r = br.call(CMD_CAPTURE_SCREEN, hwnd=hwnd or None, timeout_ms=int(timeout_s * 1000))
        log(
            f"capture_screen bridge ok={r.ok} ret={r.ret} note={r.note!r} err={r.error}"
        )
        if not r.ok:
            return None
    except Exception as e:
        log(f"capture_screen bridge call err: {e}")
        return None
    finally:
        try:
            br.close()
        except Exception:
            pass

    path = wait_new_screenshot(folder, since_mtime=before, timeout_s=timeout_s)
    if path is None:
        log("capture_screen: no new file in Screenshots")
    else:
        log(f"capture_screen file={path.name} size={path.stat().st_size}")
    return path


def capture_client_bitblt_only(
    hwnd: int,
    *,
    log: LogFn | None = None,
) -> CaptureResult:
    """
    BitBlt-only client capture (never PrintWindow — avoids hang).

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        import ctypes
        from ctypes import wintypes

        from PIL import Image

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        SRCCOPY = 0x00CC0020
        BI_RGB = 0
        DIB_RGB_COLORS = 0

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [
                ("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3),
            ]

        box = get_client_rect_screen(hwnd)
        if box is None:
            return CaptureResult(ok=False, error="invalid hwnd")
        left, top, right, bottom = box
        width = right - left
        height = bottom - top
        if width < 8 or height < 8:
            return CaptureResult(ok=False, error=f"tiny {width}x{height}")

        hdc_win = user32.GetDC(wintypes.HWND(hwnd))
        if not hdc_win:
            return CaptureResult(ok=False, error="GetDC failed")
        hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_win, width, height)
        old = gdi32.SelectObject(hdc_mem, hbmp)
        try:
            ok = bool(
                gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_win, 0, 0, SRCCOPY)
            )
            if not ok:
                return CaptureResult(ok=False, error="BitBlt failed")
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = width
            bmi.bmiHeader.biHeight = -height
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = BI_RGB
            buf_len = width * height * 4
            buf = (ctypes.c_ubyte * buf_len)()
            got = gdi32.GetDIBits(
                hdc_mem,
                hbmp,
                0,
                height,
                ctypes.byref(buf),
                ctypes.byref(bmi),
                DIB_RGB_COLORS,
            )
            if not got:
                return CaptureResult(ok=False, error="GetDIBits failed")
            img = Image.frombuffer(
                "RGBA", (width, height), bytes(buf), "raw", "BGRA", 0, 1
            ).convert("RGB")
            bio = BytesIO()
            img.save(bio, format="PNG")
            log(f"bitblt_only ok {width}x{height}")
            return CaptureResult(
                ok=True,
                png=bio.getvalue(),
                width=width,
                height=height,
                client_origin=(left, top),
                image=img,
            )
        finally:
            gdi32.SelectObject(hdc_mem, old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(wintypes.HWND(hwnd), hdc_win)
    except Exception as e:
        return CaptureResult(ok=False, error=str(e))


def crop_rect_from_image(img, rect: DlgRect):
    """
    Crop dialog rect from a full-client RGB image.

    @author by ak
    """
    iw, ih = img.size
    l = max(0, min(int(rect.x), iw - 1))
    t = max(0, min(int(rect.y), ih - 1))
    r = max(l + 1, min(int(rect.right), iw))
    b = max(t + 1, min(int(rect.bottom), ih))
    return img.crop((l, t, r, b))


def _save_export_failure_debug(
    full_img,
    mem_rect: DlgRect | None,
    *,
    log: LogFn | None = None,
) -> list[Path]:
    """Persist the full frame and raw memory crop beside the running app."""
    log = log or (lambda _m: None)
    saved: list[Path] = []
    try:
        from common.paths import captcha_debug_dir

        folder = captcha_debug_dir()
        stamp = f"{int(time.time() * 1000)}_export_fail"
        full_path = folder / f"{stamp}_full.png"
        full_img.save(full_path)
        saved.append(full_path)
        if mem_rect is not None and mem_rect.ok:
            raw = crop_rect_from_image(full_img, mem_rect)
            raw_path = folder / f"{stamp}_mem_raw.png"
            raw.save(raw_path)
            saved.append(raw_path)
        log("captcha export debug -> " + ", ".join(str(p) for p in saved))
    except Exception as e:
        log(f"captcha export debug save err: {e}")
    return saved


def _scale_rect_to_image(
    rect: DlgRect,
    img_w: int,
    img_h: int,
    *,
    ref_w: int = 0,
    ref_h: int = 0,
) -> DlgRect:
    """
    Map client-space dialog rect onto capture image pixels.

    CaptureScreen / render size may differ from AUI client metrics.
    @author by ak
    """
    if img_w <= 0 or img_h <= 0:
        return rect
    sx = sy = 1.0
    if ref_w > 8 and ref_h > 8:
        sx = float(img_w) / float(ref_w)
        sy = float(img_h) / float(ref_h)
    # If rect already near image size, leave it (no double scale).
    if abs(sx - 1.0) < 0.02 and abs(sy - 1.0) < 0.02:
        return rect
    if sx == 1.0 and sy == 1.0:
        return rect
    x = int(round(rect.x * sx))
    y = int(round(rect.y * sy))
    w = max(1, int(round(rect.w * sx)))
    h = max(1, int(round(rect.h * sy)))
    out = DlgRect(
        ok=rect.ok,
        name=rect.name,
        dlg_ptr=rect.dlg_ptr,
        x=x,
        y=y,
        w=w,
        h=h,
        shown=rect.shown,
        error=rect.error,
    )
    return out


def _looks_like_dialog_panel(img) -> bool:
    """
    Check crop is a dark modal panel (not full client HUD).

    @author by ak
    """
    if img is None:
        return False
    try:
        w, h = img.size
        if w < 280 or h < 180:
            return False
        ar = w / float(h)
        if ar < 1.20 or ar > 2.10:
            return False
        if w >= 900 or h >= 500:
            return False
        sample = img.resize((48, 30))
        px = list(sample.getdata())
        dark = 0
        for p in px:
            r, g, b = int(p[0]), int(p[1]), int(p[2])
            if r < 100 and g < 120 and b < 145:
                dark += 1
        ratio = dark / float(max(1, len(px)))
        return 0.48 <= ratio <= 0.94
    except Exception:
        return False


def _is_plausible_dialog_geometry(crop, full_w: int, full_h: int) -> bool:
    """Reject tiny/full-frame crops without assuming one client resolution."""
    if crop is None or full_w < 8 or full_h < 8:
        return False
    cw, ch = crop.size
    if cw < 280 or ch < 180:
        return False
    if full_w >= 640 and full_h >= 400:
        area_ratio = (cw * ch) / float(full_w * full_h)
        if area_ratio > 0.58:
            return False
        if cw > int(full_w * 0.82) or ch > int(full_h * 0.78):
            return False
        # Live clients use different resolutions. The verified 660x460 dialog
        # occupies only 34% of a 1916-wide CaptureScreen frame.
        if cw < int(full_w * 0.28) or ch < int(full_h * 0.24):
            return False
    ar = cw / float(ch)
    return 1.20 <= ar <= 2.10


def _is_plausible_memory_dialog_rect(
    rect: DlgRect,
    crop,
    full_w: int,
    full_h: int,
) -> bool:
    """Validate an IsDlgShow-backed rect without resolution-relative minimums."""
    if crop is None or full_w < 8 or full_h < 8:
        return False
    if rect.x < 0 or rect.y < 0 or rect.right > full_w or rect.bottom > full_h:
        return False
    cw, ch = crop.size
    if cw != int(rect.w) or ch != int(rect.h):
        return False
    if cw < 240 or ch < 160:
        return False
    area_ratio = (cw * ch) / float(full_w * full_h)
    if area_ratio > 0.58:
        return False
    if cw > int(full_w * 0.82) or ch > int(full_h * 0.78):
        return False
    ar = cw / float(ch)
    return 1.15 <= ar <= 2.20


def _is_plausible_dialog_crop(crop, full_w: int, full_h: int) -> bool:
    """
    Geometric gate: dialog must be a centered-ish panel, not near-full frame.

    @author by ak
    """
    if not _is_plausible_dialog_geometry(crop, full_w, full_h):
        return False
    return _looks_like_dialog_panel(crop)


def _crop_activity_dialog_from_full(
    full_img,
    *,
    mem_rect: DlgRect | None = None,
    mem_ref_w: int = 0,
    mem_ref_h: int = 0,
    scale_to_train: bool = True,
    log: LogFn | None = None,
) -> tuple[object | None, DlgRect | None, str]:
    """
    Crop ONLY the 活动限时答题 panel from a full client/frame image.

    A shown memory dialog rect is preferred and mapped from the live client
    size to CaptureScreen pixels. Visual detection is the fallback.
    Never returns a near-full-frame / full-scene image for identify API.
    @author by ak
    """
    log = log or (lambda _m: None)
    if full_img is None:
        return None, None, "no image"
    if getattr(full_img, "mode", "RGB") != "RGB":
        full_img = full_img.convert("RGB")
    iw, ih = full_img.size
    method = ""
    used_rect: DlgRect | None = None
    crop = None

    # --- 1) Memory rect (fast and authoritative after IsDlgShow) ---
    if mem_rect is not None and mem_rect.ok:
        # Untrust mem if it already looks like full client.
        if mem_rect.w >= int(iw * 0.82) or mem_rect.h >= int(ih * 0.75):
            log(
                f"mem rect untrusted fullish "
                f"({mem_rect.w}x{mem_rect.h} of {iw}x{ih})"
            )
        else:
            candidates: list[tuple[str, DlgRect]] = []
            if mem_ref_w > 8 and mem_ref_h > 8:
                candidates.append(
                    (
                        f"mem_rect_scaled_live_{mem_ref_w}x{mem_ref_h}",
                        _scale_rect_to_image(
                            mem_rect,
                            iw,
                            ih,
                            ref_w=mem_ref_w,
                            ref_h=mem_ref_h,
                        ),
                    )
                )
            candidates.append(("mem_rect", mem_rect))
            for tag, rect in candidates:
                if rect.w >= int(iw * 0.82) or rect.h >= int(ih * 0.75):
                    continue
                crop_try = crop_rect_from_image(full_img, rect)
                # A memory-confirmed, in-bounds dialog may have a bright skin;
                # geometry is sufficient. Visual candidates retain pixel gate.
                if _is_plausible_memory_dialog_rect(rect, crop_try, iw, ih):
                    crop = crop_try
                    used_rect = rect
                    method = tag
                    break
                log(
                    f"mem candidate rejected via={tag} "
                    f"rect=({rect.x},{rect.y},{rect.w}x{rect.h}) "
                    f"crop={crop_try.size} img={iw}x{ih}"
                )
            if crop is None:
                log(
                    f"mem rect crop rejected mem="
                    f"({mem_rect.x},{mem_rect.y},{mem_rect.w}x{mem_rect.h}) "
                    f"ref={mem_ref_w}x{mem_ref_h} img={iw}x{ih}"
                )

    # --- 2) Visual detector fallback (slower) ---
    if crop is None:
        try:
            from app.core.captcha_dialog import find_captcha_dialog

            box = find_captcha_dialog(full_img, log=log)
            if box is not None:
                crop_try = full_img.crop((box.left, box.top, box.right, box.bottom))
                if _is_plausible_dialog_crop(crop_try, iw, ih):
                    crop = crop_try
                    used_rect = DlgRect(
                        ok=True,
                        name=(mem_rect.name if mem_rect else "visual"),
                        dlg_ptr=mem_rect.dlg_ptr if mem_rect else 0,
                        x=int(box.left),
                        y=int(box.top),
                        w=int(box.width),
                        h=int(box.height),
                        shown=True,
                    )
                    method = f"visual_{box.method or 'find'}"
                else:
                    log(
                        f"visual box rejected geo/panel {crop_try.size} "
                        f"of {iw}x{ih} score={box.score:.2f}"
                    )
            else:
                log(f"visual dialog not found on {iw}x{ih}")
        except Exception as e:
            log(f"visual crop err: {e}")

    if crop is None:
        return None, mem_rect, "dialog panel crop failed (full frame not submitted)"

    cw, ch = crop.size
    # Absolute last line of defense.
    final_ok = (
        _is_plausible_memory_dialog_rect(used_rect, crop, iw, ih)
        if method.startswith("mem_rect") and used_rect is not None
        else _is_plausible_dialog_crop(crop, iw, ih)
    )
    if not final_ok:
        log(f"final reject crop {cw}x{ch} of {iw}x{ih}")
        return None, used_rect, "crop failed final geometry/panel gate"

    if scale_to_train:
        try:
            from app.core.captcha_dialog import TRAIN_DIALOG_H, TRAIN_DIALOG_W
            from PIL import Image as _Image

            if crop.size != (TRAIN_DIALOG_W, TRAIN_DIALOG_H):
                crop = crop.resize(
                    (TRAIN_DIALOG_W, TRAIN_DIALOG_H),
                    resample=getattr(_Image, "Resampling", _Image).BILINEAR
                    if hasattr(getattr(_Image, "Resampling", _Image), "BILINEAR")
                    else _Image.BILINEAR,
                )
        except Exception:
            try:
                from app.core.captcha_dialog import TRAIN_DIALOG_H, TRAIN_DIALOG_W

                crop = crop.resize((TRAIN_DIALOG_W, TRAIN_DIALOG_H))
            except Exception:
                pass

    log(
        f"dialog crop ok {cw}x{ch}-> {getattr(crop, 'size', '?')} via={method} "
        f"rect="
        f"{None if used_rect is None else f'({used_rect.x},{used_rect.y},{used_rect.w}x{used_rect.h})'}"
    )
    return crop, used_rect, method


def export_dlg_image(
    session,
    *,
    hwnd: int = 0,
    dlg_name: str | None = None,
    names: tuple[str, ...] | list[str] | None = None,
    scale_to_train: bool = True,
    prefer_bridge: bool = True,
    debug_save_fail: bool = False,
    log: LogFn | None = None,
) -> DlgImageExport:
    """
    Export ONLY the 活动限时答题 panel PNG (never full client scene).

    Full CaptureScreen/BitBlt frame is cropped to dialog before return.
    @author by ak
    """
    log = log or (lambda _m: None)
    if dlg_name:
        from app.core.plg_ui import query_dlg_show

        q = query_dlg_show(session, dlg_name, log=log)
        if not q.shown or not q.dlg_ptr:
            return DlgImageExport(ok=False, error=f"{dlg_name} not shown")
        rect = read_dlg_rect(session, q.dlg_ptr, name=dlg_name, log=log)
    else:
        rect = get_captcha_dlg_rect(session, names=names, log=log)
    if not rect.ok or not rect.shown:
        return DlgImageExport(
            ok=False,
            rect=rect if rect.dlg_ptr else None,
            error=rect.error or "dialog not shown",
        )

    full_img = None
    method = ""
    source_path = None
    client_w = client_h = 0
    mem_ref_w = mem_ref_h = 0
    if hwnd:
        try:
            client_box = get_client_rect_screen(hwnd)
            if client_box is not None:
                mem_ref_w = max(0, int(client_box[2] - client_box[0]))
                mem_ref_h = max(0, int(client_box[3] - client_box[1]))
        except Exception as e:
            log(f"captcha client rect read err: {e}")

    # Prefer in-game CaptureScreen (works when minimized). BitBlt often black
    # for iconic/minimized windows — only as last resort and log a warning.
    if prefer_bridge:
        path = capture_screen_via_bridge(session, hwnd=hwnd, log=log)
        if path is not None and path.is_file():
            try:
                from PIL import Image

                full_img = Image.open(path).convert("RGB")
                method = "bridge_CaptureScreen"
                source_path = str(path)
                client_w, client_h = full_img.size
            except Exception as e:
                log(f"open screenshot err: {e}")

    if full_img is None and hwnd:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            if user32.IsIconic(wintypes.HWND(int(hwnd))):
                log(
                    "export_dlg_image: game minimized; BitBlt likely black — "
                    "need working bridge CaptureScreen"
                )
        except Exception:
            pass
        cap = capture_client_bitblt_only(hwnd, log=log)
        if cap.ok and cap.image is not None:
            # Reject near-black frames (common minimized BitBlt failure).
            try:
                sample = cap.image.resize((32, 18))
                pixels = list(sample.getdata())
                mean = sum(sum(p[:3]) for p in pixels) / max(1, len(pixels) * 3.0)
                if mean < 8.0:
                    log(
                        f"export_dlg_image: bitblt frame too dark mean={mean:.1f} "
                        f"(minimized/blank); reject"
                    )
                    return DlgImageExport(
                        ok=False,
                        rect=rect,
                        error="bitblt blank/minimized frame",
                        method="bitblt_only",
                    )
            except Exception:
                pass
            full_img = cap.image
            method = "bitblt_only"
            client_w, client_h = cap.width, cap.height
        else:
            return DlgImageExport(
                ok=False,
                rect=rect,
                error=cap.error or "bitblt failed",
                method="bitblt_only",
            )

    if full_img is None:
        return DlgImageExport(ok=False, rect=rect, error="no frame source")

    log(
        f"captcha crop map mem=({rect.x},{rect.y},{rect.w}x{rect.h}) "
        f"client_ref={mem_ref_w}x{mem_ref_h} capture={full_img.size[0]}x{full_img.size[1]}"
    )
    # CRITICAL: never submit full scene — crop 活动限时答题 only.
    crop, used_rect, crop_method = _crop_activity_dialog_from_full(
        full_img,
        mem_rect=rect,
        mem_ref_w=mem_ref_w,
        mem_ref_h=mem_ref_h,
        scale_to_train=scale_to_train,
        log=log,
    )
    if crop is None:
        if debug_save_fail:
            _save_export_failure_debug(full_img, rect, log=log)
        # Still drop CaptureScreen file on failure to avoid Screenshots growth.
        delete_screenshot_file(source_path, log=log)
        return DlgImageExport(
            ok=False,
            rect=rect,
            source_path=source_path,
            method=f"{method}+crop_fail",
            client_w=client_w,
            client_h=client_h,
            error=crop_method or "dialog crop failed",
        )

    bio = BytesIO()
    crop.save(bio, format="PNG")
    png = bio.getvalue()
    out_rect = used_rect or rect
    # CaptureScreen jpg only needed for crop — delete after use.
    delete_screenshot_file(source_path, log=log)
    log(
        f"dlg export ok {out_rect.name} "
        f"rect=({out_rect.x},{out_rect.y},{out_rect.w}x{out_rect.h}) "
        f"png={len(png)} {crop.size} via={method}+{crop_method}"
    )
    return DlgImageExport(
        ok=True,
        png=png,
        rect=out_rect,
        source_path=None,
        method=f"{method}+{crop_method}",
        client_w=client_w,
        client_h=client_h,
    )
