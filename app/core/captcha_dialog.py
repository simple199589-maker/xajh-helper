# -*- coding: utf-8 -*-
"""
Detect / crop the 活动限时答题 dialog from a full client screenshot.

Training set images are ~701x430 dark teal panels. We only submit that
region (resized toward training size) to the identify API.

@author by ak
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from io import BytesIO
from typing import Callable

LogFn = Callable[[str], None]

# Live training corpus size (sameobject captcha).
TRAIN_DIALOG_W = 701
TRAIN_DIALOG_H = 430
TRAIN_ASPECT = TRAIN_DIALOG_W / float(TRAIN_DIALOG_H)  # ~1.630

# Dark panel pixels (teal/black UI chrome).
_DARK_R = 95
_DARK_G = 115
_DARK_B = 140


@dataclass
class DialogBox:
    """
    Captcha dialog rectangle in client-image pixels.

    left/top/right/bottom are exclusive-right/bottom crop bounds.
    @author by ak
    """

    left: int
    top: int
    right: int
    bottom: int
    score: float = 0.0
    method: str = ""

    @property
    def width(self) -> int:
        return max(0, int(self.right) - int(self.left))

    @property
    def height(self) -> int:
        return max(0, int(self.bottom) - int(self.top))

    @property
    def origin(self) -> tuple[int, int]:
        return int(self.left), int(self.top)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["width"] = self.width
        d["height"] = self.height
        return d


def _is_dark(r: int, g: int, b: int) -> bool:
    return r < _DARK_R and g < _DARK_G and b < _DARK_B


def _sample_dark_ratio(img, box: tuple[int, int, int, int], step: int = 4) -> float:
    """
    Fraction of sampled pixels that look like dialog chrome.

    @author by ak
    """
    l, t, r, b = box
    if r <= l or b <= t:
        return 0.0
    step = max(2, int(step))
    dark = 0
    total = 0
    px = img.load()
    y = t
    while y < b:
        x = l
        while x < r:
            p = px[x, y]
            if isinstance(p, int):
                if p < 90:
                    dark += 1
            else:
                if _is_dark(int(p[0]), int(p[1]), int(p[2])):
                    dark += 1
            total += 1
            x += step
        y += step
    if total <= 0:
        return 0.0
    return dark / float(total)


def _min_dialog_size(iw: int, ih: int) -> tuple[int, int]:
    """
    Minimum accepted dialog size for this client resolution.

    Real 答题 dialog is a large centered panel (~half+ client). Small HUD
    scraps are not the modal.
    @author by ak
    """
    # ~55% width / ~48% height floors; absolute floors for small clients.
    min_w = max(520, int(iw * 0.55))
    min_h = max(300, int(ih * 0.48))
    min_w = min(min_w, iw - 8)
    min_h = min(min_h, ih - 8)
    return min_w, min_h


def _score_candidate(
    img,
    box: tuple[int, int, int, int],
    *,
    min_w: int,
    min_h: int,
) -> float:
    """
    Score a dialog candidate for the live client resolution.

    @author by ak
    """
    l, t, r, b = box
    w = r - l
    h = b - t
    if w < min_w or h < min_h:
        return 0.0
    ar = w / float(h)
    ar_pen = abs(ar - TRAIN_ASPECT)
    if ar_pen > 0.40:
        return 0.0
    dark = _sample_dark_ratio(img, box, step=4)
    # Real dialog chrome is mostly dark; pure black scrap or mixed HUD fail.
    if dark < 0.58 or dark > 0.92:
        return 0.0
    title_h = max(14, int(h * 0.12))
    title_dark = _sample_dark_ratio(img, (l, t, r, t + title_h), step=3)
    if title_dark < 0.58:
        return 0.0
    mid_t = t + int(h * 0.20)
    mid_b = t + int(h * 0.80)
    mid_box = (l + int(w * 0.08), mid_t, r - int(w * 0.08), mid_b)
    mid_dark = _sample_dark_ratio(img, mid_box, step=4)
    # Grid has gray animals — mid should not be pure black slab.
    if mid_dark < 0.42 or mid_dark > 0.90:
        return 0.0
    # Outer ring around candidate should be brighter than interior.
    iw, ih = img.size
    pad = max(12, int(min(iw, ih) * 0.025))
    # sample only the ring (approx via full outer - but ratio vs outer pad frame)
    ring_dark = 0.0
    ring_n = 0
    # top strip outside
    if t - pad >= 0:
        ring_dark += _sample_dark_ratio(img, (l, max(0, t - pad), r, t), step=4)
        ring_n += 1
    if b + pad <= ih:
        ring_dark += _sample_dark_ratio(img, (l, b, r, min(ih, b + pad)), step=4)
        ring_n += 1
    if l - pad >= 0:
        ring_dark += _sample_dark_ratio(img, (max(0, l - pad), t, l, b), step=4)
        ring_n += 1
    if r + pad <= iw:
        ring_dark += _sample_dark_ratio(img, (r, t, min(iw, r + pad), b), step=4)
        ring_n += 1
    outer = ring_dark / float(ring_n) if ring_n else 1.0
    contrast = dark - outer
    if contrast < 0.18:
        return 0.0
    bot_t = t + int(h * 0.84)
    bot_dark = _sample_dark_ratio(
        img, (l + int(w * 0.2), bot_t, r - int(w * 0.2), b), step=3
    )
    score = (
        dark * 1.3
        + title_dark * 1.0
        + mid_dark * 0.7
        + bot_dark * 0.5
        + max(0.0, contrast) * 1.5
        - ar_pen * 1.4
    )
    area_norm = (w * h) / float(max(1, iw * ih))
    score += min(0.9, area_norm * 5.5)
    ideal_w = min(TRAIN_DIALOG_W, int(iw * 0.68))
    score += max(0.0, 0.5 - abs(w - ideal_w) / max(1.0, float(ideal_w)))
    return float(score)


def find_captcha_dialog(
    img,
    *,
    log: LogFn | None = None,
) -> DialogBox | None:
    """
    Locate the centered 答题 dialog on a full client RGB image.

    Rejects small dark HUD false-positives and returns None if no plausible
    modal dialog.

    @author by ak
    """
    log = log or (lambda _m: None)
    if img is None:
        return None
    if img.mode != "RGB":
        img = img.convert("RGB")
    iw, ih = img.size
    if iw < 640 or ih < 400:
        log(f"captcha dialog skip tiny client={iw}x{ih}")
        return None

    min_w, min_h = _min_dialog_size(iw, ih)

    # Candidate widths: relative first (works across resolutions), then absolute.
    widths: list[int] = []
    for frac in (0.52, 0.56, 0.60, 0.64, 0.68, 0.72, 0.48, 0.76):
        widths.append(int(iw * frac))
    for aw in (701, 680, 740, 640, 760, 620, 800, 560):
        widths.append(aw)
    seen: set[int] = set()
    cands_w: list[int] = []
    for w in widths:
        w = int(w)
        if w < min_w or w > iw - 8:
            continue
        if w in seen:
            continue
        seen.add(w)
        cands_w.append(w)
    if not cands_w:
        log(f"captcha dialog no width candidates client={iw}x{ih} min_w={min_w}")
        return None

    best: DialogBox | None = None
    y_offs = (0, -int(ih * 0.03), int(ih * 0.03), -int(ih * 0.06), int(ih * 0.05))
    x_offs = (0, -int(iw * 0.02), int(iw * 0.02), -int(iw * 0.04), int(iw * 0.04))

    for dw in cands_w:
        dh = int(round(dw / TRAIN_ASPECT))
        if dh < min_h or dh > ih - 8:
            # clamp height into client while keeping aspect-ish
            if ih - 8 < min_h:
                continue
            dh = min(max(min_h, dh), ih - 8)
        for xo in x_offs:
            for yo in y_offs:
                l = (iw - dw) // 2 + int(xo)
                t = (ih - dh) // 2 + int(yo)
                l = max(0, min(l, iw - dw))
                t = max(0, min(t, ih - dh))
                r = l + dw
                b = t + dh
                sc = _score_candidate(img, (l, t, r, b), min_w=min_w, min_h=min_h)
                if sc < 1.80:
                    continue
                if best is None or sc > best.score + 1e-6:
                    best = DialogBox(
                        left=l,
                        top=t,
                        right=r,
                        bottom=b,
                        score=sc,
                        method="dark_center",
                    )

    # Fallback dark-mass grow, still enforce min size.
    if best is None or best.score < 2.10:
        seed = _expand_dark_mass(img, min_w=min_w, min_h=min_h)
        if seed is not None:
            sc = _score_candidate(
                img,
                (seed.left, seed.top, seed.right, seed.bottom),
                min_w=min_w,
                min_h=min_h,
            )
            seed.score = sc
            seed.method = "dark_mass"
            if sc >= 1.80 and (best is None or sc > best.score):
                best = seed

    # Final acceptance: need strong score AND large enough panel.
    if best is None or best.score < 1.90:
        log(
            f"captcha dialog not found client={iw}x{ih} "
            f"min={min_w}x{min_h} best="
            f"{None if best is None else f'{best.width}x{best.height}@{best.score:.2f}'}"
        )
        return None
    if best.width < min_w or best.height < min_h:
        log(
            f"captcha dialog reject small {best.width}x{best.height} "
            f"< min {min_w}x{min_h} score={best.score:.2f}"
        )
        return None

    log(
        f"captcha dialog box=({best.left},{best.top})-"
        f"({best.right},{best.bottom}) {best.width}x{best.height} "
        f"score={best.score:.2f} via={best.method}"
    )
    return best


def _expand_dark_mass(
    img,
    *,
    min_w: int,
    min_h: int,
) -> DialogBox | None:
    """
    Grow a dark rectangle from image center.

    @author by ak
    """
    iw, ih = img.size
    px = img.load()
    cx, cy = iw // 2, ih // 2

    def dark_at(x: int, y: int) -> bool:
        if x < 0 or y < 0 or x >= iw or y >= ih:
            return False
        p = px[x, y]
        return _is_dark(int(p[0]), int(p[1]), int(p[2]))

    if not dark_at(cx, cy):
        found = False
        for dy in range(-50, 51, 4):
            for dx in range(-80, 81, 4):
                if dark_at(cx + dx, cy + dy):
                    cx, cy = cx + dx, cy + dy
                    found = True
                    break
            if found:
                break
        if not found:
            return None

    l = r = cx
    t = b = cy
    changed = True
    guard = 0
    while changed and guard < 900:
        changed = False
        guard += 1
        if l > 2:
            col_n = max(1, (b - t) // 3)
            col_dark = sum(1 for y in range(t, b + 1, 3) if dark_at(l - 2, y))
            if col_dark / col_n >= 0.60:
                l -= 2
                changed = True
        if r < iw - 3:
            col_n = max(1, (b - t) // 3)
            col_dark = sum(1 for y in range(t, b + 1, 3) if dark_at(r + 2, y))
            if col_dark / col_n >= 0.60:
                r += 2
                changed = True
        if t > 2:
            row_n = max(1, (r - l) // 3)
            row_dark = sum(1 for x in range(l, r + 1, 3) if dark_at(x, t - 2))
            if row_dark / row_n >= 0.60:
                t -= 2
                changed = True
        if b < ih - 3:
            row_n = max(1, (r - l) // 3)
            row_dark = sum(1 for x in range(l, r + 1, 3) if dark_at(x, b + 2))
            if row_dark / row_n >= 0.60:
                b += 2
                changed = True

    pad = 4
    l = max(0, l - pad)
    t = max(0, t - pad)
    r = min(iw, r + pad + 1)
    b = min(ih, b + pad + 1)
    w, h = r - l, b - t
    if w < min_w or h < min_h:
        return None
    # Snap to training aspect if close.
    ar = w / float(h)
    if abs(ar - TRAIN_ASPECT) > 0.35:
        nh = int(round(w / TRAIN_ASPECT))
        if nh >= min_h and nh < ih:
            mid = (t + b) // 2
            t = max(0, mid - nh // 2)
            b = min(ih, t + nh)
            t = max(0, b - nh)
    return DialogBox(left=l, top=t, right=r, bottom=b, score=0.0, method="dark_mass")


def crop_dialog_png(
    img,
    box: DialogBox,
    *,
    scale_to_train: bool = True,
) -> bytes:
    """
    Crop dialog to PNG bytes; optionally resize toward training 701x430.

    @author by ak
    """
    crop = img.crop((box.left, box.top, box.right, box.bottom))
    if scale_to_train and crop.size != (TRAIN_DIALOG_W, TRAIN_DIALOG_H):
        # Keep aspect; fit into train box then pad? API wants full dialog
        # frame like training — direct resize is fine for same layout.
        try:
            from PIL import Image as _Image

            crop = crop.resize(
                (TRAIN_DIALOG_W, TRAIN_DIALOG_H),
                resample=getattr(_Image, "Resampling", _Image).BILINEAR
                if hasattr(getattr(_Image, "Resampling", _Image), "BILINEAR")
                else _Image.BILINEAR,
            )
        except Exception:
            crop = crop.resize((TRAIN_DIALOG_W, TRAIN_DIALOG_H))
    bio = BytesIO()
    crop.save(bio, format="PNG")
    return bio.getvalue()


def dialog_confirm_rect(box: DialogBox) -> tuple[int, int, int, int]:
    """Client-space safe hit rect for the dialog's thin confirm button."""
    bw = max(72, min(140, box.width // 6))
    bh = max(24, min(40, box.height // 12))
    cx = box.left + int(box.width * 0.50)
    cy = box.top + int(box.height * 0.925)
    left = max(box.left, cx - bw // 2)
    top = max(box.top, cy - bh // 2)
    right = min(box.right, left + bw)
    bottom = min(box.bottom, top + bh)
    return left, top, right, bottom


def clamp_dialog_confirm_point(
    box: DialogBox, x: int, y: int, *, inset: int = 3
) -> tuple[int, int]:
    """Clamp a randomized confirm point into the button's safe interior."""
    left, top, right, bottom = dialog_confirm_rect(box)
    pad_x = min(max(1, int(inset)), max(1, (right - left - 1) // 2))
    pad_y = min(max(1, int(inset)), max(1, (bottom - top - 1) // 2))
    return (
        max(left + pad_x, min(right - pad_x - 1, int(x))),
        max(top + pad_y, min(bottom - pad_y - 1, int(y))),
    )


def dialog_confirm_point(box: DialogBox) -> tuple[int, int]:
    """
    Client-relative click for 确定 inside dialog.

    @author by ak
    """
    left, top, right, bottom = dialog_confirm_rect(box)
    return (left + right) // 2, (top + bottom) // 2


def dialog_cancel_point(box: DialogBox) -> tuple[int, int]:
    """
    Client-relative click for 取消 (bottom row, left of 确定).

    Live Win_Question3D 2026-07-20 lab: geom:cancel_mid closed dialog
    at ~42% width / 90% height (box 383,170,660x460 → click ~661,583).
    确定 is near center-bottom (~50%/90%); 取消 sits slightly left of it.
    @author by ak
    """
    cx = box.left + int(box.width * 0.42)
    cy = box.top + int(box.height * 0.90)
    return cx, cy


def dialog_close_x_point(box: DialogBox) -> tuple[int, int]:
    """
    Client-relative click for top-right close chrome.

    @author by ak
    """
    # Slightly inset from top-right corner (titlebar X).
    cx = box.left + int(box.width * 0.96)
    cy = box.top + int(box.height * 0.06)
    return cx, cy


def dialog_close_points(box: DialogBox) -> list[tuple[str, int, int]]:
    """
    Ordered close click candidates for stuck captcha dialogs.

    Live priority (Win_Question3D, lab 2026-07-20):
      cancel_mid (~42%/90%) closed the dialog; farther-left guesses wasted clicks.
    Btn_Exit/Btn_Cancel mem rect often null/0x0 — geometry only.

    @author by ak
    """
    pts: list[tuple[str, int, int]] = []
    # 1) Live-verified first
    cx, cy = dialog_cancel_point(box)
    pts.append(("cancel_mid", cx, cy))
    # 2) Slight jitter variants around verified zone (still left of 确定@50%)
    pts.append(
        (
            "cancel_mid_l",
            box.left + int(box.width * 0.40),
            box.top + int(box.height * 0.90),
        )
    )
    pts.append(
        (
            "cancel_mid_r",
            box.left + int(box.width * 0.45),
            box.top + int(box.height * 0.90),
        )
    )
    pts.append(
        (
            "cancel_row",
            box.left + int(box.width * 0.38),
            box.top + int(box.height * 0.88),
        )
    )
    # 3) Farther left only as backup
    pts.append(
        (
            "cancel_left",
            box.left + int(box.width * 0.28),
            box.top + int(box.height * 0.90),
        )
    )
    # 4) Titlebar X last (skin-dependent)
    cx, cy = dialog_close_x_point(box)
    pts.append(("close_x", cx, cy))
    pts.append(
        (
            "close_x_inset",
            box.left + int(box.width * 0.93),
            box.top + int(box.height * 0.05),
        )
    )
    return pts


def dialog_cell_point(box: DialogBox, index_1based: int) -> tuple[int, int] | None:
    """
    Client-relative center of cell 1-8 (row-major 2x4) inside dialog box.

    Fallback geometry when memory Img_Question rect is unavailable.
    Prefer app.core.aui_click.get_captcha_cell_center with a live session.
    @author by ak
    """
    try:
        idx = int(index_1based) - 1
    except Exception:
        return None
    if idx < 0 or idx >= 8:
        return None
    grid_l = box.left + int(box.width * 0.06)
    grid_r = box.left + int(box.width * 0.94)
    grid_t = box.top + int(box.height * 0.20)
    grid_b = box.top + int(box.height * 0.82)
    gw = grid_r - grid_l
    gh = grid_b - grid_t
    col = idx % 4
    row = idx // 4
    cx = grid_l + int((col + 0.5) * gw / 4.0)
    cy = grid_t + int((row + 0.5) * gh / 2.0)
    return cx, cy
