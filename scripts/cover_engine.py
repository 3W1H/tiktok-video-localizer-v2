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
from dataclasses import dataclass, field, replace as dc_replace
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
BRAND_LEAD = 0.60       # extra lead ABOVE the brand row, x the row above it.
# The brand mark is a signature, not the tagline's third line. With the flat
# ROW_GAP (0.16) the gap above it measured ~0px of real ink and the two rows
# merged into one blob (2026-10-15, user: 「下方的 logo 和文字混在一起」).
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")

# --- stack layout (2026-10-15) -------------------------------------------
# ``layout: stack`` puts headline / sub / stat / tagline / brand into ONE
# bottom-anchored block instead of an upper and a lower group. The block needs
# its own, roomier rhythm: with the flat ROW_GAP the two 127px rows and the
# brand all but touch, and the rows read as a pile.
STACK_ROW_GAP = 0.24        # gap inside the block, x the row's own height
# Lead above the brand row, as a fraction of the SAFE BAND height (1075px ->
# ~188px). 0.80 x the tagline height left the brand lockup ~38px under that row,
# which at 1080px wide reads as "the tagline is sitting on the signature"
# (2026-10-15, user: 「这跟 logo 挨着」). The brand is a signature, not another
# content row, so it gets real air.
# Deliberately NOT a multiple of the row above it any more: that row can wrap to
# two lines (Wade "193 CM OF PURE VIOLENCE", Rondo "PASSES NOBODY ELSE SEES"),
# which made the signature drop ~190px extra on those two covers alone. Tying
# the lead to the band keeps the air identical on all ten (2026-10-15, user:
# 「小字不能去掉吗？而且不能再和 Logo 之间的间距再大一点吗」).
# NB: the `split` anchor does not use this at all -- it pins the signature to the
# band foot instead (see Resolved.stack_anchor).
STACK_BRAND_LEAD = 0.175
STACK_GROUP_LEAD = 0.060    # title group -> stat group, x safe band height
STACK_BOTTOM_INSET = 0.075  # block bottom above s_bot, x safe band height
# 0.075 * 1075 = ~80px: the brand ink then sits ~83px clear of the 1:1
# profile-grid edge, instead of the 11px the `safe` layout left it at
# (2026-10-15, user: 「文字怎么都变成了上下」+ 「下方的 logo 和文字混在一起」).

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

LAYOUTS = ("safe", "stack", "classic")
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


def brand_lockup_box(font_path: str, size: int, text: str, stroke: int = 0):
    """Geometry of the sticker lockup WITHOUT drawing it.

    Returns ``(layer_w, layer_h, ink_top, ink_bottom)``, ink measured from the
    LAYER top -- the same origin ``draw_brand_lockup`` uses when it pastes at
    ``cy - layer_h / 2``.

    ⭐ The layout MUST advance the brand row by its INK height, never by
    ``block_h(font, text)`` (= ``font.size * 1.06``). Two reasons:

      1. ``draw_brand_lockup`` pads its layer by ``size * 0.55`` on EVERY side
         to leave room for the outline and the glow, so the layer is far taller
         than the glyphs (64px Impact -> 127px layer).
      2. Impact's glyph box is not vertically centred inside that padding:
         ``getbbox`` returns ``(0, 14, 498, 71)``, so the ink centre sits ~21px
         ABOVE the layer centre. Centring the *layer* on the row centre shoves
         the mark up by that much.

    Measured on the 2026-10-15 NBA batch (10/10 covers): the recorded logo
    region overlapped the tagline by 20px and the real ink gap was ~0px, so
    "THE FLASH" + "Hoop Dynasty Daily" read as one merged two-line block --
    user: 「下方的 logo 和文字混在一起」.
    """
    stroke = stroke or max(4, int(round(size * 0.13)))
    f = ImageFont.truetype(font_path, size)
    tb = f.getbbox(text)
    m = int(size * 0.55)
    th = tb[3] - tb[1]
    oy = m - tb[1]
    layer_w, layer_h = (tb[2] - tb[0]) + 2 * m, th + 2 * m
    # outline + outer glow reach. The glow is faint (alpha 35) but it is exactly
    # what visually welds two rows together, so it counts as ink.
    reach = stroke + max(2, int(round(size * 0.07)))
    return layer_w, layer_h, max(0, oy - reach), min(layer_h, oy + th + reach)


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

    layout: str = ""                    # "" -> style.layout, else "safe"
    picture: Optional[str] = None       # None -> style.picture_mode, else band
    safe_top: Optional[float] = None    # None -> style.safe_zone_top_pct
    safe_bottom: Optional[float] = None
    backdrop: str = "blur"
    backdrop_blur: float = 36
    backdrop_dim: float = 0.42
    backdrop_sat: float = 0.55
    face_box: Optional[Tuple[int, int, int, int]] = None
    # (x, y, w, h) in FRAME space -- generate_cover detects it (two-pass Haar,
    # sees large dark close-ups the single-pass here misses) on the exact frame
    # compose receives and passes it in; engine detection is only the fallback.
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
    layout: str = "safe"
    contrast_boost: bool = False
    # Secondary-row colours. They used to be module literals (GREY / WHITE),
    # which contradicts SKILL.md 8.2 ("the profile owns the colours; a script
    # must not carry a colour literal"). Now profile-driven, same defaults.
    sub_color: Tuple[int, int, int] = GREY
    tag_color: Tuple[int, int, int] = WHITE
    # --- stack-block placement (2026-10-15, user: 「标题太靠下了，已经和底部的
    # logo 放到一起了」) ---------------------------------------------------
    # The block is bottom-anchored, so it always ends `stack_inset` above the
    # safe-band bottom. Raising that value LIFTS the whole block; ``block_scale``
    # shrinks its type (the brand mark keeps its profile size -- it is the
    # signature, not another headline row). Note these work together: shrinking
    # alone would pull the block's top edge DOWN, because it is anchored by its
    # bottom, so a "smaller and higher" look needs both.
    stack_inset: float = STACK_BOTTOM_INSET
    block_scale: float = 1.0
    # Row kinds to omit entirely: any of "sub" / "stat" / "tag" (the headline and
    # the brand mark always stay). This is the 「精简」 lever.
    drop_rows: Tuple[str, ...] = ()
    # Where the rows sit in the safe band: "bottom" (default, historical look),
    # "center", "top", or "split". Dropping a row from a BOTTOM-anchored block
    # pulls its top edge DOWN -- so 「精简」 alone makes the title sit lower, not
    # higher; lifting needs this anchor (2026-10-15).
    #   "split" -- copy rows head the band, the signature is pinned to its foot
    #   ("title up top, mark at the bottom"). Both margins are `stack_inset`.
    stack_anchor: str = "bottom"


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
    # ⭐ the layout is a PROFILE value too (nba = "stack" since 2026-10-15).
    # Explicit spec.layout wins; "" / None means "ask the profile". Leaving the
    # CoverSpec default at "safe" silently overrode the profile in every caller
    # that did not pass it (the spec test caught exactly that).
    layout = spec.layout or style.get("layout") or "safe"
    max_w = int(canvas_w * spec.headline_max_width)
    brand_font = BOLD_FONT if spec.logo_style == "plain" else brand_font_path()
    return Resolved(head_size=head_size, head_stroke=head_stroke,
                    head_font=head_font, brand_stroke=brand_stroke,
                    logo_size=logo_size, safe_top=safe_top,
                    safe_bottom=safe_bottom, picture=picture, max_w=max_w,
                    brand_font=brand_font, layout=layout,
                    contrast_boost=bool(style.get("contrast_boost", False)),
                    sub_color=tuple(style.get("sub_color") or GREY),
                    tag_color=tuple(style.get("tagline_color") or WHITE),
                    stack_inset=float(style.get("stack_bottom_inset_pct")
                                      if style.get("stack_bottom_inset_pct")
                                      is not None else STACK_BOTTOM_INSET),
                    block_scale=float(style.get("block_scale") or 1.0),
                    drop_rows=tuple(style.get("drop_rows") or ()),
                    stack_anchor=str(style.get("stack_anchor") or "bottom"))


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------
def _largest_verified_face(frame):
    """最大的（眼部复核过的）人脸框，source px；无则 None。

    2026-10-29 国王封面"没有脸"第三层根因：fill 裁切在 img_h/scale == 全帧高时
    零余量，脸贴着源帧上缘就原样贴到 picture box 顶边、被标题压住。选帧已修
    （占位盲信/路径重接/眼部复核），这里是最后一层 —— 裁切要感知人脸。
    复用 pick_cover_frame 的级联 + 眼部二级验证，避免背影误报。

    ⚠️ 单遍全分辨率 Haar 对「大而暗的特写脸」会漏检（同 detect_faces 注释里
    Kobe 封面的教训）——调用方应优先用 generate_cover 检测（带半分辨率 rescue）
    并经 ``spec.face_box`` 传入，这里只作兜底。
    """
    try:
        import cv2
        import numpy as np
        from pick_cover_frame import _verified_faces
        bgr = cv2.cvtColor(np.array(frame.convert("RGB")), cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        cas = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        min_side = max(36, int(min(w, h) * 0.06))
        faces = cas.detectMultiScale(gray, 1.1, 5, minSize=(min_side, min_side))
        faces = _verified_faces(gray, faces)
        if not faces:
            return None
        return max(faces, key=lambda f: f[2] * f[3])
    except Exception:
        return None


def compose(frame: Image.Image, spec: CoverSpec,
            res: Optional[Resolved] = None) -> Tuple[Image.Image, Dict[str, Any]]:
    """Compose one cover from a frame. Pure: no IO, no plan, no frame choice.

    ``frame`` is a PIL RGB image at whatever resolution the source had; the
    engine handles the 1080-wide rescale and the clean-band crop.
    """
    res = res or resolve(spec)
    # ``stack`` = one single text block. The headline + sub stop floating at the
    # top of the band and join stat / tagline / brand in the bottom stack, so
    # the cover reads as ONE piece of copy instead of an upper and a lower
    # group. Geometry (fill picture, safe band, scrim, backdrop) is identical
    # to ``safe`` -- only the row list and the scrim reach change.
    lay = res.layout
    stack = lay == "stack"
    # ⚠️ `stack` MUST be normalised to "safe" here, not only when it differs
    # from spec.layout: the fill-picture branch below keys off
    # ``spec.layout == "safe"``, so leaving the literal "stack" in place makes
    # the geometry fall through to the `band` picture (picture strip only
    # 422-667 instead of the full 422-1497 band).
    norm = "safe" if stack else lay
    if spec.layout != norm:
        spec = dc_replace(spec, layout=norm)
    canvas_w, canvas_h = spec.canvas_w, spec.canvas_h
    style = spec.style or {}
    accent = tuple(style.get("fill_color") or GOLD)

    f_head = fit(res.head_font,
                 max(40, int(round(res.head_size * res.block_scale))))
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

    # ``stat_size: 0`` means "match the headline", and f_head already carries
    # block_scale -- only an explicit stat_size needs scaling of its own.
    stat_base = int(round(spec.stat_size * res.block_scale)) \
        if spec.stat_size else f_head.size
    f_stat = fit(res.head_font, max(40, stat_base))
    stat_text = wrap2(f_stat, spec.stat, res.max_w)
    while block_w(f_stat, stat_text) > res.max_w and f_stat.size > 40:
        f_stat = fit(res.head_font, f_stat.size - 4)
    f_tag = fit(BOLD_FONT, max(22, int(round(
        (spec.tagline_size or 44) * res.block_scale))))
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

    # The sticker/artistic lockup is painted on a padded layer whose ink is NOT
    # centred in it, so this row must be laid out from the INK box (see
    # brand_lockup_box). `plain` draws like any other text row.
    logo_lockup = spec.logo_style in ("artistic", "sticker")
    logo_layer_h = logo_ink_top = logo_ink_h = 0
    if spec.logo and logo_lockup:
        _, logo_layer_h, logo_ink_top, _ink_bot = brand_lockup_box(
            res.brand_font, f_logo.size, spec.logo, res.brand_stroke)
        logo_ink_h = _ink_bot - logo_ink_top

    # ``drop_rows`` is a BLOCK knob -- it prunes rows out of the single stacked
    # block. The `safe` fallback draws its title group separately (it never
    # reaches this builder) and stays byte-identical to what the user signed off
    # on, so the filter is only armed when the rows really form one block.
    drop = set(res.drop_rows) if stack else set()

    def build_rows(f_stat_):
        r = []
        if stack:
            # headline + grey sub become the first two rows of the single block
            if "headline" not in drop:
                r.append(("headline", head_text, f_head, accent, res.head_stroke))
            if spec.sub and "sub" not in drop:
                r.append(("sub", spec.sub, f_sub, res.sub_color, 3))
        if spec.stat and "stat" not in drop:
            r.append(("stat", stat_text, f_stat_, accent, 6))
        if spec.tagline and "tag" not in drop:
            r.append(("tag", tag_text, f_tag, res.tag_color, 4))
        if spec.logo and "logo" not in drop:
            r.append(("logo", spec.logo, f_logo, accent, res.brand_stroke))
        return r

    def row_h(kind, s, f):
        """Advance height of a row. The brand mark advances by its INK box."""
        if kind == "logo" and logo_lockup:
            return logo_ink_h
        return block_h(f, s)

    def gap_after(kind, nxt, hh):
        """Trailing gap of a row, as a fraction of that row's own height."""
        if stack:
            if kind == "sub" and nxt == "stat":
                # visual break between the title group and the info group
                return int(safe_h * STACK_GROUP_LEAD)
            if nxt == "logo":
                return int(safe_h * STACK_BRAND_LEAD)
            return int(hh * STACK_ROW_GAP)
        return int(hh * (BRAND_LEAD if nxt == "logo" else ROW_GAP))

    def rows_h(r):
        hs = [row_h(k, s, f) for (k, s, f, _, _) in r]
        return sum(hs) + sum(gap_after(r[i][0], r[i + 1][0], hs[i])
                             for i in range(len(hs) - 1))

    def anchored_rows_y(h: int) -> int:
        """Top edge of the row stack, per ``res.stack_anchor``.

        ``bottom`` reproduces the historical layout byte for byte; ``center``
        and ``top`` lift the block off the profile-grid edge (2026-10-15, user:
        「标题太靠下了，已经和底部的 logo 放到一起了」).

        ``split`` is the fourth mode: the COPY rows (headline / stat / tagline)
        sit at the head of the band and the signature is pinned to its foot, so
        the cover reads "title up top, mark at the bottom" (2026-10-15, user:
        「logo 怎么往上提了呢，还是要标题在上，logo 在下」). Only the copy rows
        are anchored here -- the brand row is placed absolutely in the draw loop.
        """
        if stack and res.stack_anchor == "top":
            return s_top + int(safe_h * 0.10)
        if stack and res.stack_anchor == "split":
            # same inset the signature gets at the foot -> symmetric margins
            return s_top + int(safe_h * res.stack_inset)
        if stack and res.stack_anchor == "center":
            return s_top + max(0, int((safe_h - h) / 2))
        return s_bot - pad_bottom - h

    # ------------------------------------------------------------ geometry --
    s_top = int(canvas_h * res.safe_top)
    s_bot = int(canvas_h * (1.0 - res.safe_bottom))
    safe_h = s_bot - s_top
    min_img = int(safe_h * spec.min_image)

    rows = build_rows(f_stat)
    pad = int(safe_h * 0.030)
    # Reserve above the safe-band bottom for the bottom stack. Deliberately
    # smaller than `pad`: the brand lockup's ink box already carries its own
    # outline + glow reach, and keeping the old 32px here would push the whole
    # stack 24px higher now that the brand row is sized honestly -- which on
    # 2026-10-15 moved the stat row into the face box on D-Rose (the one cover
    # that had scored a clean `cover=pass`). Bottom-anchoring from 8px instead
    # keeps the stat row exactly where it was while giving the brand its lead.
    pad_bottom = int(safe_h * 0.008)
    if stack:
        # one tall block: pull it clear of the profile-grid edge instead
        pad_bottom = int(safe_h * res.stack_inset)
    g_top = int(safe_h * 0.028)          # headline block -> picture
    g_bot = int(safe_h * 0.028)          # picture -> first bottom row
    stat_capped = False
    if spec.layout == "safe" and (res.picture == "fill" or stack):
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

    # `split` = copy rows at the head, signature at the foot. Resolved here (not
    # next to `rows`) because build_rows() may have been re-run by the cap loop
    # above, and the scrim + the draw loop both need the copy-only height.
    split = stack and res.stack_anchor == "split"
    content_h = rows_h([r for r in rows if r[0] != "logo"]) if split else 0

    # ------------------------------------------------------------- source ---
    src_w, src_h = frame.size
    # COVER-fit, never stretch. ``canvas_w / src_w`` alone makes a landscape
    # source (1280x720) only 607 px tall after scaling, and the band is 1075 --
    # the crop then got resized up to 1075 and every player came out 1.8x too
    # tall (2026-10-15, video-b1374643 Morant / video-bd71bcd1 Shaq: "封面里
    # 画面被拉长"). Scaling by the LARGER of the two ratios keeps the aspect
    # and simply crops the extra width instead.
    scale = max(canvas_w / src_w, img_h / src_h)
    base_scale = scale
    img = frame.resize((int(round(src_w * scale)), int(round(src_h * scale))),
                       Image.LANCZOS)
    pic_origin_x = max(0, (img.size[0] - canvas_w) // 2)

    # --- face-aware zoom (2026-10-29, 国王"没有脸"封面) ---------------------
    # fill 取景在竖直方向没有余量时（img_h/scale == 全帧高），脸若贴着源帧
    # 上/下缘就只能贴着 picture box 边缘，被标题/品牌字压住 —— 等于没脸。
    # 检出人脸时放大取景：脸占画面高约 30%，中心落在 40% 高度（三分法偏上）。
    # 放大上限 2.2x（防小脸拉出马赛克），窗口有 0.35*src_h 与 2.5*face_h 两道
    # 语境下限；脸已经足够大（win >= 全帧）时自动退回默认取景。
    face_box = spec.face_box or _largest_verified_face(frame)
    face_win = None
    if face_box and spec.layout != "classic":
        fx, fy, fw, fh = face_box
        win = max(fh / 0.30, 0.35 * src_h, fh * 2.5, (img_h / scale) / 2.2)
        win = min(win, float(src_h))
        if win * scale < img_h * 0.995:      # 只有真的需要放大时才改取景
            face_win = (fx + fw / 2.0, fy + fh / 2.0, win)
            scale = img_h / win
            img = frame.resize((int(round(src_w * scale)),
                                int(round(src_h * scale))), Image.LANCZOS)

    # 显式 face_box 但无需放大（win 被全帧封顶）时：水平取景仍应跟随脸
    # 中心 —— 默认居中把偏一侧的主体切掉半颗头（98全明星同框帧实录：
    # 乔丹在左缘被裁半头）。
    if face_win is None and face_box and spec.layout != "classic":
        fx, fy, fw, fh = face_box
        fcx = fx + fw / 2.0
        pic_origin_x = int(round(min(max(fcx * scale - canvas_w / 2.0, 0.0),
                                     max(0.0, img.size[0] - canvas_w))))

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
        if face_win:
            # 人脸感知取景：脸中心落在窗口 40% 高度，水平居中于脸
            # （frame 已在 generate_cover 里清理过，无需再避让烧字带）
            fcx, fcy, win = face_win
            need = min(need, win * scale)
            c_top = min(max(fcy - 0.40 * win, 0.0), max(0.0, src_h - win))
            nonlocal pic_origin_x
            pic_origin_x = int(round(min(max(fcx * scale - canvas_w / 2.0, 0.0),
                                         max(0.0, img.size[0] - canvas_w))))
            x0 = pic_origin_x
            crop = img.crop((x0, int(round(c_top * scale)), x0 + canvas_w,
                             int(round((c_top + need) * scale))))
            return crop, int(round(c_top * scale)), int(round(need * scale))
        lo, hi = ct_lim / scale, cb_lim / scale      # source px
        if need > (hi - lo):
            lo, hi = 0.0, float(src_h)
        need = min(need, hi - lo)
        c_mid = (lo + hi) / 2 + (spec.focus - 0.5) * (hi - lo)
        c_top = min(max(c_mid - need / 2, lo), hi - need)
        # the window is exactly canvas_w x target_h in RESIZED px; when the
        # cover-fit scale made the image wider than the canvas, crop the extra
        # width off CENTRED (the old x=0 kept the left edge, so a landscape
        # source always showed its left third)
        x0 = pic_origin_x
        crop = img.crop((x0, int(round(c_top * scale)), x0 + canvas_w,
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
        rows_y = anchored_rows_y(rows_h(rows))
        rows_step = None
    else:
        # Picture is a strip between the text rows, so each row keeps a clean
        # background of its own.
        img_y = s_top + pad + top_block_h + g_top
        img_crop, pic_origin_y, pic_crop_h = crop_band(img_h)
        head_y = s_top + pad
        rows_y = img_y + img_h + g_bot
        rows_step = None

    if split:
        # the copy rows head the band whatever the picture mode is
        rows_y = anchored_rows_y(rows_h(rows))

    if stack:
        # the whole copy is one block: nothing floats at the top of the band
        head_y = rows_y

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
    # 2026-10-29: backdrop=blur 不再限于 safe 版式 —— stack 版式此前直接落
    # 纯黑画布，标题/品牌区背景是死黑（用户："标题背景是纯黑色的，要原始
    # 背景图"、"logo 的黑框太大"）。模糊原帧作底后，文字区透出原始画面
    # 的暗化模糊版，黑框变成有质感的背景。
    if spec.backdrop == "blur":
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
            if split:
                # copy at the head, signature at the foot: the scrim only has to
                # reach the bottom of the copy -- darkening the whole band would
                # dim the picture for nothing
                depth = max(depth,
                            int(rows_y + content_h - s_top) + int(safe_h * 0.03))
            elif stack:
                # one tall block: the scrim has to reach its top edge
                depth = max(depth, int(s_bot - rows_y) + int(safe_h * 0.03))
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

    if not stack:
        record("headline",
               head_text,
               center_text(draw, head_text, f_head, head_y + head_h / 2,
                           accent, res.head_stroke, canvas_w=canvas_w))
        if spec.sub:
            record("sub", spec.sub,
                   center_text(draw, spec.sub, f_sub,
                               head_y + head_h + gap_head_sub + sub_h / 2,
                               res.sub_color, 3, canvas_w=canvas_w))

    brand_region = None
    y = rows_y
    n_rows = len(rows)
    for idx, (kind, s, f, fill, stroke) in enumerate(rows):
        hh = row_h(kind, s, f)
        if split and kind == "logo":
            # The mark is a FOOTER, not the copy block's last line: pin it to the
            # band foot instead of letting it hang below the stat row
            # (2026-10-15, user: 「logo 怎么往上提了呢，还是要标题在上，
            # logo 在下呢」). Mirrors the copy inset, so both margins match.
            y = s_bot - int(safe_h * res.stack_inset) - hh
        cy = y + hh / 2
        if kind == "logo" and logo_lockup:
            # Align the lockup's INK box with this row instead of centring its
            # padded layer -- otherwise the mark rides up into the tagline.
            draw_cy = y - logo_ink_top + logo_layer_h / 2
            lw, lh = draw_brand_lockup(canvas, s, res.brand_font, f.size, draw_cy,
                                       stroke=stroke,
                                       gradient=spec.logo_style == "artistic",
                                       glow=spec.logo_style == "artistic",
                                       highlight=spec.logo_style == "artistic",
                                       flat=accent)
            bx = int((canvas_w - lw) / 2)
            # record the INK box, not the padded layer: QA (face occlusion,
            # safe band) must see where the pixels actually are
            brand_region = {"text": s, "x": bx, "y": int(y), "w": lw, "h": hh}
            regions.append({"kind": "logo", "text": s, "x": bx, "y": int(y),
                            "w": lw, "h": hh})
        else:
            boxes = center_text(draw, s, f, cy, fill, stroke, canvas_w=canvas_w)
            record("logo" if kind == "logo" else kind, s, boxes)
            if kind == "logo":
                bb = boxes[0][2]
                brand_region = {"text": s, "x": int(bb[0]), "y": int(bb[1]),
                                "w": int(bb[2] - bb[0]),
                                "h": int(bb[3] - bb[1])}
        nxt = rows[idx + 1][0] if idx + 1 < n_rows else None
        y += hh + gap_after(kind, nxt, hh)

    def row_bottom():
        if split:
            # the signature is pinned to the band foot, not stacked under the
            # copy -- report where its ink actually ends
            return float(s_bot - int(safe_h * res.stack_inset))
        yy = rows_y
        for i, (kind, s, f, _, _) in enumerate(rows):
            hh = row_h(kind, s, f)
            nxt = rows[i + 1][0] if i + 1 < len(rows) else None
            yy += hh + gap_after(kind, nxt, hh)
        if rows and rows_step is None:
            yy -= gap_after(rows[-1][0], None,
                            row_h(rows[-1][0], rows[-1][1], rows[-1][2]))
        elif rows and rows_step is not None:
            yy -= rows_step
        return yy

    bottom = row_bottom() if rows else head_y + top_block_h
    safe_ok = (head_y >= s_top and bottom <= s_bot)

    meta: Dict[str, Any] = {
        "canvas": {"width": canvas_w, "height": canvas_h},
        "layout": lay,
        "picture_mode": res.picture if spec.layout == "safe" else None,
        "backdrop": spec.backdrop if spec.layout == "safe" else None,
        "safe_band": {"top": s_top, "bottom": s_bot,
                      "top_pct": res.safe_top, "bottom_pct": res.safe_bottom},
        # rect form so qa_video.check_cover can read it without extra plumbing
        "safe_text_zone": {"x": 0, "y": s_top, "w": canvas_w, "h": s_bot - s_top},
        "picture_box": {"y": int(img_y), "h": int(img_h)},
        "picture_src": {
            "scale": scale,                  # source px -> resized-frame px
            "crop_origin_x": pic_origin_x,   # in resized-frame px (centred)
            "crop_origin_y": pic_origin_y,   # in resized-frame px
            "crop_h": pic_crop_h,
            "dst_y": int(img_y),
            "dst_h": int(img_h),
        },
        "source": {"width": src_w, "height": src_h},
        "face_crop": ({"face_box_src": [int(v) for v in face_box],
                       "window_src_h": round(face_win[2], 1),
                       "zoom": round(scale / base_scale, 3)} if face_win else None),
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
    if res["layout"] in ("safe", "stack", "letter"):
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
    origin_x = float(picture_src.get("crop_origin_x") or 0)
    origin = float(picture_src.get("crop_origin_y") or 0)
    crop_h = float(picture_src.get("crop_h") or 1)
    dst_y = float(picture_src.get("dst_y") or 0)
    dst_h = float(picture_src.get("dst_h") or 1)
    fy = dst_h / max(1.0, crop_h)
    out: List[Dict[str, Any]] = []
    for b in boxes or []:
        x = float(b.get("x", 0)) * scale - origin_x
        w = float(b.get("w", 0)) * scale
        y = dst_y + (float(b.get("y", 0)) * scale - origin) * fy
        h = float(b.get("h", 0)) * scale * fy
        if drop_hidden and (y + h < dst_y or y > dst_y + dst_h
                            or x + w < 0 or x > 1080):
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
                           res.sub_color, 3, canvas_w=cw))

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
                        res.tag_color, 4))
    if spec.logo:
        pending.append(("logo", spec.logo,
                        fit_line(res.brand_font, spec.logo,
                                 res.logo_size or max(18, int(f_head.size * 0.5)),
                                 cw - 70, 18),
                        accent, res.brand_stroke))
    heights = [block_h(f, s) for (_, s, f, _, _) in pending]

    # Same ink-vs-layer correction as compose(): the sticker lockup's padded
    # layer is much taller than its ink and not centred on it, so the brand row
    # advances by its INK height and gets extra lead above it.
    logo_lockup = spec.logo_style in ("artistic", "sticker")
    logo_layer_h = logo_ink_top = 0
    if spec.logo and logo_lockup:
        li = next(i for i, p in enumerate(pending) if p[0] == "logo")
        _, logo_layer_h, logo_ink_top, _ib = brand_lockup_box(
            res.brand_font, pending[li][2].size, spec.logo, res.brand_stroke)
        heights[li] = _ib - logo_ink_top

    def _gap(i):
        if i + 1 >= len(pending):
            return 0
        hh = heights[i]
        return int(hh * (BRAND_LEAD if pending[i + 1][0] == "logo" else ROW_GAP))

    total = sum(heights) + sum(_gap(i) for i in range(len(heights) - 1))
    # same smaller bottom reserve as compose() -- see the pad_bottom note there
    pad_bottom = int(safe_h * 0.008)
    y = s_bot - pad_bottom - total
    row_sizes: Dict[str, Optional[int]] = {"stat": None, "tagline": None,
                                           "brand": None}
    key_of = {"stat": "stat", "tag": "tagline", "logo": "brand"}
    for idx, ((kind, s, f, fill, stroke), hh) in enumerate(zip(pending, heights)):
        row_sizes[key_of[kind]] = f.size
        if kind == "logo" and logo_lockup:
            draw_cy = y - logo_ink_top + logo_layer_h / 2
            lw, lh = draw_brand_lockup(
                canvas, s, res.brand_font, f.size, draw_cy, stroke=stroke,
                gradient=spec.logo_style == "artistic",
                glow=spec.logo_style == "artistic",
                highlight=spec.logo_style == "artistic", flat=accent)
            bx = int((cw - lw) / 2)
            brand_region = {"text": s, "x": bx, "y": int(y), "w": lw, "h": hh}
            regions.append({"kind": "logo", "text": s, "x": bx, "y": int(y),
                            "w": lw, "h": hh})
        else:
            boxes = center_text(draw, s, f, y + hh / 2, fill, stroke, canvas_w=cw)
            record(kind, s, boxes)
            if kind == "logo":
                bb = boxes[0][2]
                brand_region = {"text": s, "x": int(bb[0]), "y": int(bb[1]),
                                "w": int(bb[2] - bb[0]),
                                "h": int(bb[3] - bb[1])}
        y += hh + _gap(idx)

    if res.contrast_boost:
        canvas = ImageEnhance.Contrast(canvas).enhance(1.15)

    if pending:
        bottom = s_bot - pad_bottom
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
