#!/usr/bin/env python3
"""Shared TikTok cover composition engine.

Single source of truth for cover layout. Two CLIs sit on top of it:

  * ``scripts/make_cover.py``      -- manual / preview CLI (explicit text args)
  * ``scripts/generate_cover.py``  -- pipeline Job 3 (cover-plan driven)

They used to carry two independent composers, which is why a hand-tuned cover
and a pipeline-generated cover of the same video could differ
(2026-09-27). Worse, the pipeline one drew the headline at ``safe_top + 50``
and the brand at ``safe_bottom`` -- both OUTSIDE the safe band, so every
pipeline cover lost its title and logo in the profile grid.

The engine is **pure rendering**: it takes a PIL frame plus a ``CoverSpec`` and
returns ``(canvas, meta)``. It never touches the filesystem, never selects
frames and never reads ``localization-plan.json`` -- frame selection, frame
cleaning and JSON output stay in the calling CLI.

Layout contract (SKILL.md 8 / 8.1 / 13.4.2):

  * canvas 1080x1920, the composition confined to the centre 1080x1080 safe
    band (``cover_style.safe_zone_top_pct`` / ``safe_zone_bottom_pct``);
  * headline, stat row and brand signature ALL inside that band;
  * the brand mark belongs at the BOTTOM, never the top.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import resolve_font_chain  # noqa: E402

# --------------------------------------------------------------------------
# Canvas & typography constants
# --------------------------------------------------------------------------
W, H = 1080, 1920
LINE_H = 1.06           # line box as a multiple of the font size
ROW_GAP = 0.16          # gap between stacked bottom rows, x row height
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")

BLACK_FONT = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
BOLD_FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
IMPACT_FONT = "/System/Library/Fonts/Supplemental/Impact.ttf"

GOLD = (255, 208, 0)
WHITE = (255, 255, 255)
GREY = (196, 196, 196)

# SKILL.md 8: the brand mark is a TikTok-sticker lockup -- orange->yellow
# gradient, heavy black outline, outer glow, white highlight, Impact.
BRAND_C1 = (255, 100, 0)      # gradient top    - #FF6400
BRAND_C2 = (255, 200, 0)      # gradient bottom - #FFC800

LAYOUTS = ("safe", "classic")
PICTURES = ("band", "fill")
LOGO_STYLES = ("artistic", "sticker", "plain")


def brand_font_path() -> str:
    """Impact for the brand lockup (SKILL.md 8); Arial Black only if absent."""
    return resolve_font_chain(["Impact", "Arial Black"], fallback=BOLD_FONT)


# --------------------------------------------------------------------------
# Text measurement helpers
# --------------------------------------------------------------------------
def fit(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def text_w(font, s: str) -> Tuple[int, int, int]:
    b = font.getbbox(s)
    return b[2] - b[0], b[1], b[3]


def line_h(font) -> int:
    return int(font.size * LINE_H)


def block_h(font, s: str) -> int:
    return line_h(font) * len(s.split("\n"))


def block_w(font, s: str) -> int:
    return max(text_w(font, ln)[0] for ln in s.split("\n"))


def wrap2(font, s: str, max_w: int) -> str:
    """Wrap on a word boundary into the two most balanced lines. Used so a long
    title keeps its size (the spec fixes the px) instead of being shrunk until
    it fits."""
    if "\n" in s or text_w(font, s)[0] <= max_w:
        return s
    words = s.split(" ")
    if len(words) < 2:
        return s
    best, best_score = None, None
    for i in range(1, len(words)):
        l1, l2 = " ".join(words[:i]), " ".join(words[i:])
        score = max(text_w(font, l1)[0], text_w(font, l2)[0])
        if best_score is None or score < best_score:
            best, best_score = (l1, l2), score
    if best_score is not None and best_score <= max_w:
        return best[0] + "\n" + best[1]
    return s


def center_text(draw, s, font, cy, fill, stroke=0, stroke_fill=(0, 0, 0),
                canvas_w: int = W) -> List[Tuple[int, int, Tuple[int, int, int, int]]]:
    """Centre a (possibly multi-line) string on cy. '\\n' splits lines.

    Returns the drawn line boxes ``[(x, y, (l, t, r, b)), ...]`` so callers can
    record ``text_regions`` for QA (and the safe-band assertion) without
    re-deriving the metrics.
    """
    lines = s.split("\n")
    lh = line_h(font)
    y0 = cy - lh * len(lines) / 2
    boxes: List[Tuple[int, int, Tuple[int, int, int, int]]] = []
    for i, ln in enumerate(lines):
        w, t, b = text_w(font, ln)
        x = (canvas_w - w) // 2 - font.getbbox(ln)[0]
        y = int(y0 + lh * i + lh / 2 - (t + b) / 2)
        draw.text((x, y), ln, font=font, fill=fill,
                  stroke_width=stroke, stroke_fill=stroke_fill)
        boxes.append((x, y, draw.textbbox((x, y), ln, font=font,
                                          stroke_width=stroke)))
    return boxes


def draw_brand_lockup(canvas, text, font_path, size, cy,
                      c1=BRAND_C1, c2=BRAND_C2, stroke=0,
                      glow=True, highlight=True, gradient=True, flat=None):
    """TikTok-sticker brand lockup, drawn straight onto `canvas` (RGB), centred
    on `cy`. Implements SKILL.md 8: orange->yellow gradient fill, heavy black
    outline, outer glow, white specular highlight, Impact.

    `gradient=False` (with `flat=<rgb>`) gives the flat gold variant -- same
    Impact + heavy outline sticker geometry, no sheen.

    Painted on a local RGBA layer and alpha-pasted so the canvas' black bands
    stay untouched; the glow bleeds softly instead of banding against black.
    """
    stroke = stroke or max(4, int(round(size * 0.13)))
    f = ImageFont.truetype(font_path, size)
    tb = f.getbbox(text)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    m = int(size * 0.55)                       # room for stroke + glow
    lw, lh = tw + 2 * m, th + 2 * m
    ox, oy = m - tb[0], m - tb[1]

    layer = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))

    if glow:
        g = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
        ImageDraw.Draw(g).text((ox, oy), text, font=f,
                               stroke_width=stroke, stroke_fill=(*c1, 35),
                               fill=(*c1, 35))
        layer = Image.alpha_composite(layer, g.filter(
            ImageFilter.GaussianBlur(max(2.0, size * 0.07))))

    # heavy black outline, then the body clipped to the glyph itself (mask has
    # no stroke, or the fill would swallow the outline)
    ImageDraw.Draw(layer).text((ox, oy), text, font=f, fill=(0, 0, 0, 255),
                               stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    mask = Image.new("L", (lw, lh), 0)
    ImageDraw.Draw(mask).text((ox, oy), text, font=f, fill=255)
    body = Image.new("RGB", (lw, lh), tuple(flat or c1))
    if gradient:
        # Ramp across the GLYPH box (oy..oy+th), not the padded layer --
        # spreading it over the padding squeezed c1->c2 into a sliver and the
        # mark read as a flat cream colour instead of orange->yellow
        # (2026-09-27).
        ramp = Image.new("RGB", (1, th))
        for y in range(th):
            t = y / max(1, th - 1)
            ramp.putpixel((0, y), tuple(int(c1[i] + (c2[i] - c1[i]) * t)
                                        for i in range(3)))
        body.paste(ramp.resize((lw, th)), (0, oy))
    body = body.convert("RGBA")
    body.putalpha(mask)
    layer = Image.alpha_composite(layer, body)

    if highlight:
        # thin specular line riding the very top of the glyphs only
        hl = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
        ImageDraw.Draw(hl).text((ox, oy), text, font=f,
                                fill=(255, 255, 240, 85))
        band = Image.new("L", (lw, lh), 0)
        ImageDraw.Draw(band).rectangle(
            [0, oy - int(size * 0.02), lw, oy + int(size * 0.05)], fill=255)
        band = band.filter(ImageFilter.GaussianBlur(max(1.0, size * 0.025)))
        hl.putalpha(Image.composite(Image.new("L", (lw, lh), 0),
                                    hl.getchannel("A"), band))
        layer = Image.alpha_composite(layer, hl)

    canvas.paste(layer, (int((canvas.width - lw) / 2), int(cy - lh / 2)), layer)
    return lw, lh


# --------------------------------------------------------------------------
# Profile / misc helpers
# --------------------------------------------------------------------------
def profile_cover_style(name: str) -> Dict[str, Any]:
    """cover_style block from profiles/<name>.yaml -- the single source of
    truth for sizes and colours. Never hardcode those in a CLI."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "profiles", f"{name}.yaml")
    if not os.path.exists(p):
        return {}
    try:
        import yaml
        doc = yaml.safe_load(open(p, encoding="utf-8")) or {}
        return doc.get("cover_style") or {}
    except Exception:
        return {}


def as_px(value: Optional[float], fraction_default: float, ref: float) -> float:
    """--crop-top/--crop-bottom accept either a fraction of the source height
    (<=1, e.g. 0.195) or an absolute pixel row (>1, the legacy form)."""
    if value is None:
        return fraction_default * ref
    return value * ref if value <= 1 else value


def build_backdrop(img, blur, dim, sat, canvas_w: int = W, canvas_h: int = H):
    """Blurred + dimmed full-frame fill for the area outside the safe band, so
    the 9:16 canvas still reads full-bleed in the feed (the composition itself
    stays inside the band)."""
    s = max(canvas_w / img.width, canvas_h / img.height)
    bd = img.resize((max(canvas_w, int(round(img.width * s))),
                     max(canvas_h, int(round(img.height * s)))), Image.LANCZOS)
    left, top = (bd.width - canvas_w) // 2, (bd.height - canvas_h) // 2
    bd = bd.crop((left, top, left + canvas_w, top + canvas_h))
    if sat != 1.0:
        bd = ImageEnhance.Color(bd).enhance(sat)
    if dim != 1.0:
        bd = ImageEnhance.Brightness(bd).enhance(dim)
    if blur > 0:
        bd = bd.filter(ImageFilter.GaussianBlur(blur))
    return bd


# --------------------------------------------------------------------------
# Spec
# --------------------------------------------------------------------------
@dataclass
class CoverSpec:
    """Everything one cover composition needs, all of it explicit.

    ``style`` is the profile's ``cover_style`` block; the CLI passes it in so
    the engine stays free of file IO and the resolution rules live in one place
    (``resolve``)."""

    headline: str
    sub: str = ""
    stat: str = ""
    tagline: str = ""
    logo: str = ""
    style: Dict[str, Any] = field(default_factory=dict)

    headline_face: str = "auto"
    headline_size: int = 0              # 0 -> style.font_size
    headline_max_width: float = 0.96
    stat_size: int = 0                  # 0 -> same as resolved headline size
    tagline_size: int = 0               # 0 -> 44px auto
    logo_size: int = 0                  # 0 -> style.brand_size
    logo_ratio: Optional[float] = None  # ignored when logo_size is set
    logo_style: str = "sticker"
    brand_stroke: int = 0               # 0 -> style.brand_stroke_width

    layout: str = "safe"
    picture: Optional[str] = None       # None -> style.picture_mode, else band
    safe_top: Optional[float] = None    # None -> style.safe_zone_top_pct
    safe_bottom: Optional[float] = None
    backdrop: str = "blur"
    backdrop_blur: float = 36
    backdrop_dim: float = 0.42
    backdrop_sat: float = 0.55
    scrim: int = 140
    min_image: float = 0.30
    focus: float = 0.5
    crop_top: Optional[float] = None
    crop_bottom: Optional[float] = None

    contrast: float = 1.06
    saturation: float = 0.78
    brightness: float = 0.88
    sharpen: float = 1.15
    bg_blur: float = 8
    dof_x: float = 0.06
    dof_cy: float = 0.55
    dof_ry: float = 0.50
    bg_dim: float = 0.72
    vignette: int = 170

    canvas_w: int = W
    canvas_h: int = H
    resize_to_canvas: bool = True   # letter_cover only: scale to canvas first


@dataclass
class Resolved:
    """The size/geometry values the profile supplies, after defaults."""

    head_size: int
    head_stroke: int
    head_font: str
    brand_stroke: int
    logo_size: int
    safe_top: float
    safe_bottom: float
    picture: str
    max_w: int
    brand_font: str
    contrast_boost: bool = False


def resolve(spec: CoverSpec) -> Resolved:
    """Turn a spec + profile cover_style into concrete values.

    Sizes come from the profile -- never hardcode them in a CLI. Exposed
    separately so tests can assert the resolved numbers without rendering.
    """
    style = spec.style or {}
    canvas_w = spec.canvas_w
    head_size = spec.headline_size or int(style.get("font_size") or 108)
    head_stroke = int(style.get("stroke_width") or 6)
    head_font = resolve_font_chain(
        style.get("font_chain") if spec.headline_face in ("", "auto")
        else [spec.headline_face], fallback=BLACK_FONT)
    brand_stroke = spec.brand_stroke or int(style.get("brand_stroke_width") or 4)
    logo_size = spec.logo_size or int(style.get("brand_size") or 0)
    safe_top = float(style.get("safe_zone_top_pct") or 0.22) \
        if spec.safe_top is None else spec.safe_top
    safe_bottom = float(style.get("safe_zone_bottom_pct") or 0.22) \
        if spec.safe_bottom is None else spec.safe_bottom
    picture = spec.picture or style.get("picture_mode") or "band"
    max_w = int(canvas_w * spec.headline_max_width)
    brand_font = BOLD_FONT if spec.logo_style == "plain" else brand_font_path()
    return Resolved(head_size=head_size, head_stroke=head_stroke,
                    head_font=head_font, brand_stroke=brand_stroke,
                    logo_size=logo_size, safe_top=safe_top,
                    safe_bottom=safe_bottom, picture=picture, max_w=max_w,
                    brand_font=brand_font,
                    contrast_boost=bool(style.get("contrast_boost", False)))


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------
def compose(frame: Image.Image, spec: CoverSpec,
            res: Optional[Resolved] = None) -> Tuple[Image.Image, Dict[str, Any]]:
    """Compose one cover from a frame. Pure: no IO, no plan, no frame choice.

    ``frame`` is a PIL RGB image at whatever resolution the source had; the
    engine handles the 1080-wide rescale and the clean-band crop.
    """
    res = res or resolve(spec)
    canvas_w, canvas_h = spec.canvas_w, spec.canvas_h
    style = spec.style or {}
    accent = tuple(style.get("fill_color") or GOLD)

    f_head = fit(res.head_font, res.head_size)
    head_text = wrap2(f_head, spec.headline, res.max_w)
    while block_w(f_head, head_text) > res.max_w and f_head.size > 40:
        f_head = fit(res.head_font, f_head.size - 4)
    # keep the sub line proportional to whatever the headline ended up as
    f_sub = fit(BOLD_FONT, max(26, int(f_head.size * 0.37)))
    while spec.sub and text_w(f_sub, spec.sub)[0] > res.max_w:
        f_sub = fit(BOLD_FONT, f_sub.size - 2)
    head_h = block_h(f_head, head_text)
    sub_h = block_h(f_sub, spec.sub) if spec.sub else 0
    gap_head_sub = int(f_head.size * 0.14) if spec.sub else 0
    top_block_h = head_h + gap_head_sub + sub_h

    f_stat = fit(res.head_font, spec.stat_size or f_head.size)
    stat_text = wrap2(f_stat, spec.stat, res.max_w)
    while block_w(f_stat, stat_text) > res.max_w and f_stat.size > 40:
        f_stat = fit(res.head_font, f_stat.size - 4)
    f_tag = fit(BOLD_FONT, spec.tagline_size or 44)
    tag_text = wrap2(f_tag, spec.tagline, res.max_w)
    while block_w(f_tag, tag_text) > res.max_w and f_tag.size > 22:
        f_tag = fit(BOLD_FONT, f_tag.size - 2)

    if res.logo_size:
        fs_logo = res.logo_size
    elif spec.logo_ratio:
        lw_target = spec.logo_ratio * canvas_w
        fs_logo = 16
        for s in range(16, 200):
            if text_w(fit(res.brand_font, s), spec.logo)[0] > lw_target:
                break
            fs_logo = s
    else:
        fs_logo = max(18, int(f_head.size * 0.5))
    f_logo = fit(res.brand_font, fs_logo)
    while text_w(f_logo, spec.logo)[0] > canvas_w - 70 and f_logo.size > 18:
        f_logo = fit(res.brand_font, f_logo.size - 2)

    def build_rows(f_stat_):
        r = []
        if spec.stat:
            r.append(("stat", stat_text, f_stat_, accent, 6))
        if spec.tagline:
            r.append(("tag", tag_text, f_tag, WHITE, 4))
        if spec.logo:
            r.append(("logo", spec.logo, f_logo, accent, res.brand_stroke))
        return r

    def rows_h(r):
        hs = [block_h(f, s) for (_, s, f, _, _) in r]
        return sum(hs) + sum(int(h * ROW_GAP) for h in hs[:-1])

    # ------------------------------------------------------------ geometry --
    s_top = int(canvas_h * res.safe_top)
    s_bot = int(canvas_h * (1.0 - res.safe_bottom))
    safe_h = s_bot - s_top
    min_img = int(safe_h * spec.min_image)

    rows = build_rows(f_stat)
    pad = int(safe_h * 0.030)
    g_top = int(safe_h * 0.028)          # headline block -> picture
    g_bot = int(safe_h * 0.028)          # picture -> first bottom row
    stat_capped = False
    if spec.layout == "safe" and res.picture == "fill":
        img_h = safe_h
    else:
        img_h = safe_h - (2 * pad + top_block_h + g_top + g_bot + rows_h(rows))
        while img_h < min_img and spec.stat and f_stat.size > 60:
            # shrink the display row (never below 60px) until the picture gets
            # its minimum share of the band
            f_stat = fit(res.head_font, f_stat.size - 4)
            stat_capped = True
            rows = build_rows(f_stat)
            img_h = safe_h - (2 * pad + top_block_h + g_top + g_bot + rows_h(rows))
        img_h = max(80, img_h)

    # ------------------------------------------------------------- source ---
    src_w, src_h = frame.size
    scale = canvas_w / src_w
    img = frame.resize((canvas_w, int(src_h * scale)), Image.LANCZOS)

    # Trim away the bands that carry burned-in video titles/subtitles so the
    # cover never shows a second copy of the headline or a stray caption.
    ct_lim = as_px(spec.crop_top, 0.195, src_h) * scale
    cb_lim = as_px(spec.crop_bottom, 0.70, src_h) * scale
    ct_lim = max(0.0, min(ct_lim, img.size[1] - 40))
    cb_lim = max(ct_lim + 40, min(cb_lim, img.size[1]))

    def crop_band(target_h):
        """Source crop whose aspect matches target_h, centred on --focus.
        Inside the clean band when it fits, otherwise grown symmetrically
        around the focus point (a cleaned base frame can afford that; only the
        picture bleeds, never the text).

        Returns ``(crop, origin_y, crop_h)`` with the last two in RESIZED-frame
        px, so a caller can map source-space boxes (faces) into canvas space.
        """
        need = target_h / scale                      # source px
        lo, hi = ct_lim / scale, cb_lim / scale      # source px
        if need > (hi - lo):
            lo, hi = 0.0, float(src_h)
        need = min(need, hi - lo)
        c_mid = (lo + hi) / 2 + (spec.focus - 0.5) * (hi - lo)
        c_top = min(max(c_mid - need / 2, lo), hi - need)
        crop = img.crop((0, int(round(c_top * scale)), canvas_w,
                         int(round((c_top + need) * scale))))
        return crop, int(round(c_top * scale)), int(round(need * scale))

    rows_step: Optional[float]
    if spec.layout == "classic":
        # Legacy: the picture keeps the whole clean band and the text sits in
        # the black bars at the very top/bottom of the canvas. Headline and
        # brand banner get cut by the profile-grid crop -- kept for repro only.
        img_h = int(cb_lim - ct_lim)
        img_y = int((canvas_h - img_h) / 2)
        gap_bottom = canvas_h - (img_y + img_h)
        img_crop = img.crop((0, int(ct_lim), canvas_w, int(cb_lim)))
        pic_origin_y, pic_crop_h = int(ct_lim), int(cb_lim - ct_lim)
        head_y = (img_y - top_block_h) / 2
        rows_y = img_y + img_h + int(gap_bottom * 0.10)
        rows_step = max(2.0, (gap_bottom - 2 * int(gap_bottom * 0.10)
                              - rows_h(rows)) / (len(rows) + 1)) if rows else 0
    elif res.picture == "fill":
        # Picture fills the safe band, every text row floats on top of it.
        img_y = s_top
        img_crop, pic_origin_y, pic_crop_h = crop_band(img_h)
        head_y = s_top + pad
        rows_y = s_bot - pad - rows_h(rows)
        rows_step = None
    else:
        # Picture is a strip between the text rows, so each row keeps a clean
        # background of its own.
        img_y = s_top + pad + top_block_h + g_top
        img_crop, pic_origin_y, pic_crop_h = crop_band(img_h)
        head_y = s_top + pad
        rows_y = img_y + img_h + g_bot
        rows_step = None

    # --- picture: defocus the crowd, keep the subject crisp ----------------
    # A 720p sports frame upscaled to 1080 is noisy; sharpening it (the old
    # 1.6) made the crowd read as visual mush -- user: "画面不是花的吗".
    # Correct move is the opposite: pull colour out, blur everything except
    # a soft ellipse around the subject, then sink the edges into black.
    area = img_crop.resize((canvas_w, img_h), Image.LANCZOS) \
        if img_crop.size != (canvas_w, img_h) else img_crop
    area = ImageEnhance.Contrast(area).enhance(spec.contrast)
    area = ImageEnhance.Color(area).enhance(spec.saturation)
    area = ImageEnhance.Brightness(area).enhance(spec.brightness)
    if spec.sharpen != 1.0:
        area = ImageEnhance.Sharpness(area).enhance(spec.sharpen)

    if spec.bg_blur > 0:
        soft = area.filter(ImageFilter.GaussianBlur(spec.bg_blur))
        soft = ImageEnhance.Brightness(soft).enhance(spec.bg_dim)
        # smooth radial falloff -- a hard ellipse left visible diagonal edges
        yy, xx = np.mgrid[0:img_h, 0:canvas_w].astype(np.float32)
        cx, cy = canvas_w * 0.5, img_h * spec.dof_cy
        rx, ry = canvas_w * (0.5 - spec.dof_x), img_h * spec.dof_ry
        d = np.sqrt(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2)
        dof = Image.fromarray(
            (np.clip((1.18 - d) / 0.55, 0, 1) * 255).astype(np.uint8))
        area = Image.composite(area, soft, dof)      # crisp centre, soft edges

    black = Image.new("RGB", area.size, (0, 0, 0))
    mask = Image.new("L", area.size, 0)
    md = ImageDraw.Draw(mask)
    md.rectangle([0, 0, canvas_w, img_h], fill=spec.vignette)  # edges keep ~67%
    md.ellipse([-canvas_w * 0.25, -int(img_h * 0.30),
                canvas_w * 1.25, int(img_h * 1.10)], fill=255)  # centre 100%
    mask = mask.filter(ImageFilter.GaussianBlur(150))
    area = Image.composite(area, black, mask)

    # melt the edges into the surround instead of hard-cutting them
    fade = Image.new("L", area.size, 0)
    fh = max(16, int(img_h * 0.07))
    ImageDraw.Draw(fade).rectangle([0, fh, canvas_w, img_h - fh], fill=255)
    fade = fade.filter(ImageFilter.GaussianBlur(fh * 0.5))
    area = Image.composite(area, black, fade)

    # ------------------------------------------------------------- canvas ---
    if spec.layout == "safe" and spec.backdrop == "blur":
        canvas = build_backdrop(img, spec.backdrop_blur, spec.backdrop_dim,
                                spec.backdrop_sat, canvas_w, canvas_h)
    else:
        canvas = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))

    if spec.scrim > 0 and spec.layout == "safe":
        # black bleeding in from the canvas top/bottom, so the backdrop never
        # competes with the title or the brand mark
        ys = np.arange(canvas_h, dtype=np.float32)
        t = np.zeros(canvas_h, np.float32)
        if s_top > 0:
            t[:s_top] = 1.0 - ys[:s_top] / s_top
        if s_bot < canvas_h:
            t[s_bot:] = (ys[s_bot:] - s_bot) / (canvas_h - s_bot)
        if res.picture == "fill":
            # the text sits ON the picture, so it needs its own scrim
            depth = max(1, int(safe_h * 0.34))
            band = np.zeros(canvas_h, np.float32)
            i0 = np.arange(0, min(depth, safe_h))
            band[s_top + i0] = (1.0 - i0 / depth) ** 1.3
            i1 = np.arange(max(0, safe_h - depth), safe_h)
            band[s_top + i1] = np.maximum(
                band[s_top + i1], (1.0 - (safe_h - i1) / depth) ** 1.3)
            t = np.maximum(t, band * 0.88)
        t = np.clip(t, 0, 1) ** 1.5
        m = np.repeat((t * spec.scrim)[:, None], canvas_w, axis=1).astype(np.uint8)
        canvas = Image.composite(Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0)),
                                 canvas, Image.fromarray(m))

    canvas.paste(area, (0, int(img_y)))
    draw = ImageDraw.Draw(canvas, "RGBA")

    # ------------------------------------------------------------ drawing ---
    regions: List[Dict[str, Any]] = []

    def record(kind, text, boxes):
        for (x, y, bb) in boxes:
            regions.append({"kind": kind, "text": text.replace("\n", " "),
                            "x": int(bb[0]), "y": int(bb[1]),
                            "w": int(bb[2] - bb[0]), "h": int(bb[3] - bb[1])})

    record("headline",
           head_text,
           center_text(draw, head_text, f_head, head_y + head_h / 2,
                       accent, res.head_stroke, canvas_w=canvas_w))
    if spec.sub:
        record("sub", spec.sub,
               center_text(draw, spec.sub, f_sub,
                           head_y + head_h + gap_head_sub + sub_h / 2,
                           GREY, 3, canvas_w=canvas_w))

    brand_region = None
    y = rows_y
    for (kind, s, f, fill, stroke) in rows:
        hh = block_h(f, s)
        cy = y + hh / 2
        if kind == "logo" and spec.logo_style in ("artistic", "sticker"):
            lw, lh = draw_brand_lockup(canvas, s, res.brand_font, f.size, cy,
                                       stroke=stroke,
                                       gradient=spec.logo_style == "artistic",
                                       glow=spec.logo_style == "artistic",
                                       highlight=spec.logo_style == "artistic",
                                       flat=accent)
            bx, by = int((canvas_w - lw) / 2), int(cy - lh / 2)
            brand_region = {"text": s, "x": bx, "y": by, "w": lw, "h": lh}
            regions.append({"kind": "logo", "text": s, "x": bx, "y": by,
                            "w": lw, "h": lh})
        else:
            boxes = center_text(draw, s, f, cy, fill, stroke, canvas_w=canvas_w)
            record("logo" if kind == "logo" else kind, s, boxes)
            if kind == "logo":
                bb = boxes[0][2]
                brand_region = {"text": s, "x": int(bb[0]), "y": int(bb[1]),
                                "w": int(bb[2] - bb[0]),
                                "h": int(bb[3] - bb[1])}
        y += hh + (rows_step if rows_step is not None else int(hh * ROW_GAP))

    def row_bottom():
        yy = rows_y
        for (kind, s, f, _, _) in rows:
            hh = block_h(f, s)
            yy += hh + (rows_step if rows_step is not None else int(hh * ROW_GAP))
        return yy - (rows_step if rows_step is not None and rows else 0)

    bottom = row_bottom() if rows else head_y + top_block_h
    safe_ok = (head_y >= s_top and bottom <= s_bot)

    meta: Dict[str, Any] = {
        "canvas": {"width": canvas_w, "height": canvas_h},
        "layout": spec.layout,
        "picture_mode": res.picture if spec.layout == "safe" else None,
        "backdrop": spec.backdrop if spec.layout == "safe" else None,
        "safe_band": {"top": s_top, "bottom": s_bot,
                      "top_pct": res.safe_top, "bottom_pct": res.safe_bottom},
        # rect form so qa_video.check_cover can read it without extra plumbing
        "safe_text_zone": {"x": 0, "y": s_top, "w": canvas_w, "h": s_bot - s_top},
        "picture_box": {"y": int(img_y), "h": int(img_h)},
        "picture_src": {
            "scale": scale,                  # source px -> resized-frame px
            "crop_origin_y": pic_origin_y,   # in resized-frame px
            "crop_h": pic_crop_h,
            "dst_y": int(img_y),
            "dst_h": int(img_h),
        },
        "source": {"width": src_w, "height": src_h},
        "composition": {"top": float(head_y), "bottom": float(bottom)},
        "safe_ok": bool(safe_ok),
        "headline_lines": head_text.split("\n"),
        "sizes": {
            "headline": f_head.size,
            "sub": f_sub.size if spec.sub else None,
            "stat": f_stat.size if spec.stat else None,
            "stat_capped": stat_capped,
            "tagline": f_tag.size if spec.tagline else None,
            "brand": f_logo.size if spec.logo else None,
        },
        "full_font_size": res.head_size,
        "fonts": {
            "headline": os.path.basename(res.head_font),
            "headline_path": res.head_font,
            "font_chain": list(style.get("font_chain") or []),
            "brand": os.path.basename(res.brand_font),
        },
        "styles": {
            "logo_style": spec.logo_style,
            "stroke_width": res.head_stroke,
            "brand_stroke_width": res.brand_stroke,
            "fill_color": list(style.get("fill_color") or GOLD),
        },
        "text_regions": regions,
        "brand_region": brand_region,
        "brand_text": spec.logo or None,
        "brand_text_rendered": bool(spec.logo),
    }
    return canvas, meta


@dataclass
class CoverResult:
    image: Image.Image
    meta: Dict[str, Any]


def render_cover(frame: Image.Image, spec: CoverSpec,
                 res: Optional[Resolved] = None) -> CoverResult:
    """compose() + a stable result object for the CLIs."""
    res = res or resolve(spec)
    canvas, meta = compose(frame, spec, res)
    return CoverResult(image=canvas, meta=meta)


def format_report(spec: CoverSpec, meta: Dict[str, Any],
                  out_path: Optional[str] = None,
                  profile: Optional[str] = None) -> str:
    """The human-readable summary both CLIs print (identical wording)."""
    res = meta
    s_top = res["safe_band"]["top"]
    s_bot = res["safe_band"]["bottom"]
    sizes = res["sizes"]
    parts = [
        f"headline {sizes['headline']}",
        f"sub {sizes['sub'] or '-'}",
        f"stat {sizes['stat'] or '-'}"
        + (" (capped)" if sizes.get("stat_capped") else ""),
        f"tagline {sizes['tagline'] or '-'}",
        f"brand {sizes['brand'] or '-'}"
        + (f"/{res['styles']['logo_style']}" if sizes["brand"] else ""),
    ]
    box = res.get("picture_box")
    if box:
        pic = f", picture {box['y']}-{box['y'] + box['h']})"
    else:
        pic = ", lettered art)"
    where = f"cover -> {out_path}  " if out_path else "cover "
    head = (f"{where}({res['canvas']['width']}x{res['canvas']['height']}, "
            f"layout {res['layout']}"
            f"{'/' + res['picture_mode'] if res['picture_mode'] else ''}, "
            f"safe band {s_top}-{s_bot}{pic}")
    lines = [
        head,
        "  sizes: " + ", ".join(parts)
        + f"  [{f'profile {profile} ' if profile else ''}"
          f"face={res['fonts']['headline']} "
          f"font_size={res['full_font_size']}, "
          f"stroke={res['styles']['stroke_width']}, "
          f"brand_stroke={res['styles']['brand_stroke_width']}]",
    ]
    top, bottom = res["composition"]["top"], res["composition"]["bottom"]
    if res["layout"] in ("safe", "letter"):
        lines.append(
            f"  safe-zone: composition {int(top)}-{int(bottom)} inside "
            f"{s_top}-{s_bot} -> {'OK' if res['safe_ok'] else 'OVERFLOW'}")
    else:
        lines.append(
            f"  safe-zone: classic layout -- headline at {int(top)} and brand at "
            f"{int(bottom)} fall OUTSIDE {s_top}-{s_bot} and get cropped in the "
            f"profile grid")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Lettering an already-finished cover (generate_cover.py --cover-image)
# --------------------------------------------------------------------------
def cover_crop_resize(img: Image.Image, canvas_w: int = W,
                      canvas_h: int = H) -> Image.Image:
    """Cover-crop to the canvas aspect then scale -- "fit a finished image to
    1080x1920"."""
    src_w, src_h = img.size
    target = canvas_w / canvas_h
    current = src_w / src_h
    if current > target:
        new_w = int(src_h * target)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    else:
        new_h = int(src_w / target)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))
    return img.resize((canvas_w, canvas_h), Image.LANCZOS)


def fit_line(font_path: str, text: str, size: int, max_w: int,
             min_size: int = 18):
    """Largest size <= `size` at which `text` fits `max_w`."""
    f = fit(font_path, size)
    while text_w(f, text)[0] > max_w and f.size > min_size:
        f = fit(font_path, f.size - 2)
    return f


def map_boxes_to_canvas(boxes: List[Dict[str, Any]],
                        picture_src: Dict[str, Any],
                        drop_hidden: bool = True) -> List[Dict[str, Any]]:
    """Map source-space boxes (e.g. face rects) into canvas space.

    Uses the exact transform ``compose`` applied to the picture, so a face the
    QA occlusion check sees is where the viewer actually sees it. Boxes that
    fall outside the visible picture strip are dropped (``drop_hidden``) --
    reporting a face that isn't on the cover would raise a false occlusion
    warning.
    """
    scale = picture_src.get("scale") or 1.0
    origin = float(picture_src.get("crop_origin_y") or 0)
    crop_h = float(picture_src.get("crop_h") or 1)
    dst_y = float(picture_src.get("dst_y") or 0)
    dst_h = float(picture_src.get("dst_h") or 1)
    fy = dst_h / max(1.0, crop_h)
    out: List[Dict[str, Any]] = []
    for b in boxes or []:
        x = float(b.get("x", 0)) * scale
        w = float(b.get("w", 0)) * scale
        y = dst_y + (float(b.get("y", 0)) * scale - origin) * fy
        h = float(b.get("h", 0)) * scale * fy
        if drop_hidden and (y + h < dst_y or y > dst_y + dst_h):
            continue
        out.append({"x": int(round(x)), "y": int(round(y)),
                    "w": int(round(w)), "h": int(round(h))})
    return out


def letter_cover(img: Image.Image, spec: CoverSpec,
                 res: Optional[Resolved] = None) -> CoverResult:
    """Draw the type onto an ALREADY-FINISHED cover, leaving its picture alone.

    Used by ``generate_cover.py --cover-image``: the user supplied the artwork,
    so we only letter it. The type still obeys the safe band -- the previous
    implementation put the headline at 4% of the height, which the profile grid
    crops away (2026-09-27).
    """
    res = res or resolve(spec)
    canvas = img.convert("RGB")
    if spec.resize_to_canvas and canvas.size != (spec.canvas_w, spec.canvas_h):
        canvas = cover_crop_resize(canvas, spec.canvas_w, spec.canvas_h)
    cw, ch = canvas.size
    s_top = int(ch * res.safe_top)
    s_bot = int(ch * (1.0 - res.safe_bottom))
    safe_h = s_bot - s_top
    pad = int(safe_h * 0.030)

    if spec.scrim > 0:
        ys = np.arange(ch, dtype=np.float32)
        t = np.zeros(ch, np.float32)
        anchor_top = max(1, s_top + int(safe_h * 0.32))
        anchor_bot = min(ch - 1, s_bot - int(safe_h * 0.18))
        t[:anchor_top] = 1.0 - ys[:anchor_top] / anchor_top
        t[anchor_bot:] = (ys[anchor_bot:] - anchor_bot) / max(1, ch - anchor_bot)
        t = np.clip(t, 0, 1) ** 1.5
        m = np.repeat((t * spec.scrim)[:, None], cw, axis=1).astype(np.uint8)
        canvas = Image.composite(Image.new("RGB", (cw, ch), (0, 0, 0)), canvas,
                                 Image.fromarray(m))

    max_w = int(cw * spec.headline_max_width)
    f_head = fit_line(res.head_font, spec.headline, res.head_size, max_w, 40)
    head_text = wrap2(f_head, spec.headline, max_w)
    if block_w(f_head, head_text) > max_w:
        f_head = fit_line(res.head_font, head_text, f_head.size, max_w, 40)
    head_h = block_h(f_head, head_text)

    accent = tuple((spec.style or {}).get("fill_color") or GOLD)
    # Cover type colour comes from cover_style.fill_color (SKILL.md 8.2: a
    # profile value, never a literal). Until 2026-09-27 every element was
    # drawn in the module-level GOLD (255,208,0), which is DARKER than the
    # declared #FFDC00 (255,220,0); QA read the profile so it passed while
    # the pixels disagreed with the spec. Also feeds the brand lockup fill.
    draw = ImageDraw.Draw(canvas, "RGBA")
    regions: List[Dict[str, Any]] = []

    def record(kind, text, boxes):
        for (x, y, bb) in boxes:
            regions.append({"kind": kind, "text": text.replace("\n", " "),
                            "x": int(bb[0]), "y": int(bb[1]),
                            "w": int(bb[2] - bb[0]), "h": int(bb[3] - bb[1])})

    record("headline", head_text,
           center_text(draw, head_text, f_head, s_top + pad + head_h / 2,
                       accent, res.head_stroke, canvas_w=cw))

    f_sub = None
    if spec.sub:
        f_sub = fit_line(BOLD_FONT, spec.sub,
                         max(26, int(f_head.size * 0.37)), max_w, 20)
        record("sub", spec.sub,
               center_text(draw, spec.sub, f_sub,
                           s_top + pad + head_h
                           + int(f_head.size * 0.14) + block_h(f_sub, spec.sub) / 2,
                           GREY, 3, canvas_w=cw))

    # bottom-up stack, mirroring compose(): stat -> tagline -> brand
    brand_region = None
    y = s_bot - pad
    pending: List[Tuple[str, str, Any, Tuple[int, int, int], int]] = []
    if spec.stat:
        pending.append(("stat", spec.stat,
                        fit_line(res.head_font, spec.stat,
                                 spec.stat_size or f_head.size, max_w, 40),
                        accent, 6))
    if spec.tagline:
        pending.append(("tag", spec.tagline,
                        fit_line(BOLD_FONT, spec.tagline,
                                 spec.tagline_size or 44, max_w, 22),
                        WHITE, 4))
    if spec.logo:
        pending.append(("logo", spec.logo,
                        fit_line(res.brand_font, spec.logo,
                                 res.logo_size or max(18, int(f_head.size * 0.5)),
                                 cw - 70, 18),
                        accent, res.brand_stroke))
    heights = [block_h(f, s) for (_, s, f, _, _) in pending]
    total = sum(heights) + sum(int(h * ROW_GAP) for h in heights[:-1])
    y = s_bot - pad - total
    row_sizes: Dict[str, Optional[int]] = {"stat": None, "tagline": None,
                                           "brand": None}
    key_of = {"stat": "stat", "tag": "tagline", "logo": "brand"}
    for (kind, s, f, fill, stroke), hh in zip(pending, heights):
        row_sizes[key_of[kind]] = f.size
        cy = y + hh / 2
        if kind == "logo" and spec.logo_style in ("artistic", "sticker"):
            lw, lh = draw_brand_lockup(
                canvas, s, res.brand_font, f.size, cy, stroke=stroke,
                gradient=spec.logo_style == "artistic",
                glow=spec.logo_style == "artistic",
                highlight=spec.logo_style == "artistic", flat=accent)
            bx, by = int((cw - lw) / 2), int(cy - lh / 2)
            brand_region = {"text": s, "x": bx, "y": by, "w": lw, "h": lh}
            regions.append({"kind": "logo", "text": s, "x": bx, "y": by,
                            "w": lw, "h": lh})
        else:
            boxes = center_text(draw, s, f, cy, fill, stroke, canvas_w=cw)
            record(kind, s, boxes)
            if kind == "logo":
                bb = boxes[0][2]
                brand_region = {"text": s, "x": int(bb[0]), "y": int(bb[1]),
                                "w": int(bb[2] - bb[0]),
                                "h": int(bb[3] - bb[1])}
        y += hh + int(hh * ROW_GAP)

    if res.contrast_boost:
        canvas = ImageEnhance.Contrast(canvas).enhance(1.15)

    if pending:
        bottom = s_bot - pad
    else:
        bottom = s_top + pad + head_h
        if f_sub:
            bottom += int(f_head.size * 0.14) + block_h(f_sub, spec.sub)
    meta: Dict[str, Any] = {
        "canvas": {"width": cw, "height": ch},
        "layout": "letter",
        "picture_mode": None,
        "safe_band": {"top": s_top, "bottom": s_bot,
                      "top_pct": res.safe_top, "bottom_pct": res.safe_bottom},
        "safe_text_zone": {"x": 0, "y": s_top, "w": cw, "h": s_bot - s_top},
        "composition": {"top": float(s_top + pad), "bottom": float(bottom)},
        "safe_ok": True,
        "headline_lines": head_text.split("\n"),
        "sizes": {"headline": f_head.size,
                  "sub": f_sub.size if f_sub else None,
                  "stat": row_sizes["stat"], "stat_capped": False,
                  "tagline": row_sizes["tagline"],
                  "brand": row_sizes["brand"]},
        "full_font_size": res.head_size,
        "fonts": {"headline": os.path.basename(res.head_font),
                  "headline_path": res.head_font,
                  "font_chain": list((spec.style or {}).get("font_chain") or []),
                  "brand": os.path.basename(res.brand_font)},
        "styles": {"logo_style": spec.logo_style,
                   "stroke_width": res.head_stroke,
                   "brand_stroke_width": res.brand_stroke,
                   "fill_color": list((spec.style or {}).get("fill_color") or GOLD)},
        "text_regions": regions,
        "brand_region": brand_region,
        "brand_text": spec.logo or None,
        "brand_text_rendered": bool(spec.logo),
    }
    return CoverResult(image=canvas, meta=meta)
