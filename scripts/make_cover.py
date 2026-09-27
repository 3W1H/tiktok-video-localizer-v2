#!/usr/bin/env python3
"""Build a magazine-style TikTok cover: headline above a dimmed picture area,
stat line / tagline / brand signature below.

Usage:
  python make_cover.py --video SRC --time 46 --out cover.jpg \
      --headline "ALLEN IVERSON" --sub "2001 NBA FINALS - GAME 1" \
      --stat "48 POINTS" --tagline "SIXERS SHOCK THE LAKERS" \
      --logo "Hoop Dynasty Daily"

This is the **manual / preview** front end. All layout and drawing lives in
``cover_engine.py``, which ``generate_cover.py`` (pipeline Job 3) also uses --
so a hand-tuned cover and a pipeline cover of the same video are now the same
renderer, not two lookalikes (2026-09-27).

Canvas is always 1080x1920 (9:16, what TikTok expects for a custom cover), but
the *composition* is confined to a SAFE BAND (default: the centre 1080x1080 =
y 422..1498). That band is the only part of a 9:16 cover that survives
everywhere:

  * profile grid + search results -> centre-cropped (3:4 per most current
    sources, 1:1 per others) -- the top and bottom of the canvas are cut;
  * full-screen feed -> the bottom ~270-480px carries caption / username /
    button overlays.

Headline, stat line and the brand mark must ALL sit inside the band, so the
whole title and the whole logo stay readable in every view. The area outside
the band keeps a blurred, dimmed copy of the frame (--backdrop blur) so the
cover still reads full-bleed in the feed.

The brand mark is a deliberate design element at the BOTTOM of the composition
(SKILL.md 8 "位置：底部黑色条居中" / 8.1 "封面底部统一叠加品牌署名"), drawn in
gold. Do NOT rely on the small brand text baked into the video frame -- that
one is lettered onto the picture area by render_overlays.py and reads as a
stray caption on the cover (2026-09-27 user feedback).
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cover_engine import (  # noqa: E402
    IMAGE_EXTS, LOGO_STYLES, CoverSpec, format_report,
    profile_cover_style, render_cover, resolve,
)

FFMPEG = shutil.which("ffmpeg") or os.path.expanduser("~/.local/bin/ffmpeg")


def grab(video, t, out):
    """Pull one frame. An image path is accepted directly so a hand-cleaned
    still (e.g. a frame with the burned-in text inpainted out) can be used as
    the cover base without wrapping it in a video first."""
    if os.path.splitext(video)[1].lower() in IMAGE_EXTS:
        Image.open(video).convert("RGB").save(out)
        return
    subprocess.run([FFMPEG, "-y", "-ss", str(t), "-i", video, "-frames:v", "1", out],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--time", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--headline", required=True)
    ap.add_argument("--profile", default="nba",
                    help="profile whose cover_style supplies the headline size")
    ap.add_argument("--headline-face", default="auto",
                    help="auto = first installed face in cover_style.font_chain "
                         "(nba: Anton -> Impact -> Arial Black); otherwise a "
                         "font name (e.g. impact) or an absolute .ttf path")
    ap.add_argument("--headline-size", type=int, default=0,
                    help="override the headline font size; 0 = take "
                         "cover_style.font_size from --profile")
    ap.add_argument("--headline-max-width", type=float, default=0.96,
                    help="max headline width as a fraction of the canvas width "
                         "(stops the line from touching both edges)")
    ap.add_argument("--sub", default="")
    ap.add_argument("--stat", default="")
    ap.add_argument("--stat-size", type=int, default=0,
                    help="bottom display-line size; 0 = same as the resolved "
                         "headline size (2026-09-27 spec: top and bottom "
                         "display rows share one size)")
    ap.add_argument("--tagline", default="")
    ap.add_argument("--tagline-size", type=int, default=0,
                    help="white caption line in the bottom band; 0 = auto "
                         "(capped at 44px)")
    ap.add_argument("--logo", default="",
                    help="brand mark drawn at the BOTTOM of the composition "
                         "(e.g. the profile's brand_text). Empty = omit.")
    ap.add_argument("--logo-size", type=int, default=0,
                    help="brand-mark font size in px; 0 = cover_style.brand_size "
                         "from --profile, else 50%% of the headline size "
                         "(SKILL.md 8.1). 2026-09-27 spec: fixed 64px at 1080w, "
                         "independent of the headline size.")
    ap.add_argument("--logo-ratio", type=float, default=None,
                    help="override: brand mark width / canvas width. Ignored "
                         "when --logo-size is given.")
    ap.add_argument("--brand-stroke", type=int, default=0,
                    help="brand-mark stroke width; 0 = cover_style."
                         "brand_stroke_width from --profile (fallback 4)")
    ap.add_argument("--logo-style", choices=list(LOGO_STYLES),
                    default="sticker",
                    help="sticker (default) = SKILL.md 8 Impact lockup in the flat "
                         "profile fill_color -- what the 2026-09-27 reference cover uses; "
                         "artistic = same lockup with orange->yellow gradient, "
                         "outer glow and white highlight (keep it for marks "
                         "burned over busy picture, not on a black band); "
                         "plain = flat gold Arial Bold (legacy fallback)")
    ap.add_argument("--layout", choices=["safe", "classic"], default="safe",
                    help="safe (default) = composition confined to the "
                         "--safe-top/--safe-bottom band, which is the only part "
                         "of a 9:16 cover TikTok never crops (headline AND "
                         "brand mark stay visible in the profile grid); "
                         "classic = legacy full-canvas letterbox, which loses "
                         "the top and bottom lines in the grid. Kept only for "
                         "reproducing older covers.")
    ap.add_argument("--safe-top", type=float, default=None,
                    help="top of the safe band, as a fraction of canvas height "
                         "measured DOWN from the top; default = "
                         "cover_style.safe_zone_top_pct (0.22)")
    ap.add_argument("--safe-bottom", type=float, default=None,
                    help="bottom of the safe band, as a fraction of canvas "
                         "height measured UP from the bottom (same sense as "
                         "safe_zone_bottom_pct); default 0.22")
    ap.add_argument("--backdrop", choices=["blur", "black"], default="blur",
                    help="what fills the canvas outside the safe band: blur = "
                         "dimmed defocused copy of the frame (full-bleed look "
                         "in the feed); black = flat black")
    ap.add_argument("--picture", choices=["band", "fill"], default=None,
                    help="band = picture is a strip between the text rows "
                         "(text always sits on a clean background); fill = "
                         "picture fills the whole safe band and the text is "
                         "overlaid on it behind a soft scrim (bigger picture, "
                         "same safe-zone guarantee). Default = "
                         "cover_style.picture_mode, else band")
    ap.add_argument("--backdrop-blur", type=float, default=36)
    ap.add_argument("--backdrop-dim", type=float, default=0.42)
    ap.add_argument("--backdrop-sat", type=float, default=0.55)
    ap.add_argument("--scrim", type=int, default=140,
                    help="extra black gradient bleeding in from the canvas top "
                         "and bottom so text never fights the backdrop (0 = off)")
    ap.add_argument("--min-image", type=float, default=0.30,
                    help="minimum share of the safe band reserved for the "
                         "picture; the bottom stat row shrinks to honour it")
    ap.add_argument("--focus", type=float, default=0.5,
                    help="vertical focus of the picture inside the clean band "
                         "(0 = top, 1 = bottom)")
    ap.add_argument("--crop-top", type=float, default=None,
                    help="clean-band top: fraction of the source height (<=1) "
                         "or absolute source row (>1). Default 0.195, i.e. "
                         "below the burned-in video title")
    ap.add_argument("--crop-bottom", type=float, default=None,
                    help="clean-band bottom (same units). Default 0.70, i.e. "
                         "above the burned-in subtitle")
    ap.add_argument("--contrast", type=float, default=1.06)
    ap.add_argument("--saturation", type=float, default=0.78,
                    help="<1 drains colour from the crowd so the subject pops")
    ap.add_argument("--brightness", type=float, default=0.88)
    ap.add_argument("--sharpen", type=float, default=1.15,
                    help="keep low; a 720p frame upscaled to 1080 gets noisy")
    ap.add_argument("--bg-blur", type=float, default=8,
                    help="Gaussian radius for the defocused surround (0 = off)")
    ap.add_argument("--dof-x", type=float, default=0.06,
                    help="horizontal inset of the crisp ellipse, as a fraction of width")
    ap.add_argument("--dof-cy", type=float, default=0.55,
                    help="vertical centre of the crisp ellipse (fraction of image height)")
    ap.add_argument("--dof-ry", type=float, default=0.50,
                    help="vertical radius of the crisp ellipse (fraction of image height)")
    ap.add_argument("--bg-dim", type=float, default=0.72,
                    help="extra darkening applied to the defocused background only")
    ap.add_argument("--vignette", type=int, default=170,
                    help="edge brightness of the vignette (0 = black, 255 = none)")
    args = ap.parse_args()

    # Sizes come from profiles/<name>.yaml -- never hardcode them here.
    spec = CoverSpec(
        headline=args.headline,
        sub=args.sub,
        stat=args.stat,
        tagline=args.tagline,
        logo=args.logo,
        style=profile_cover_style(args.profile),
        headline_face=args.headline_face,
        headline_size=args.headline_size,
        headline_max_width=args.headline_max_width,
        stat_size=args.stat_size,
        tagline_size=args.tagline_size,
        logo_size=args.logo_size,
        logo_ratio=args.logo_ratio,
        logo_style=args.logo_style,
        brand_stroke=args.brand_stroke,
        layout=args.layout,
        picture=args.picture,
        safe_top=args.safe_top,
        safe_bottom=args.safe_bottom,
        backdrop=args.backdrop,
        backdrop_blur=args.backdrop_blur,
        backdrop_dim=args.backdrop_dim,
        backdrop_sat=args.backdrop_sat,
        scrim=args.scrim,
        min_image=args.min_image,
        focus=args.focus,
        crop_top=args.crop_top,
        crop_bottom=args.crop_bottom,
        contrast=args.contrast,
        saturation=args.saturation,
        brightness=args.brightness,
        sharpen=args.sharpen,
        bg_blur=args.bg_blur,
        dof_x=args.dof_x,
        dof_cy=args.dof_cy,
        dof_ry=args.dof_ry,
        bg_dim=args.bg_dim,
        vignette=args.vignette,
    )

    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, "f.jpg")
        grab(args.video, args.time, raw)
        frame = Image.open(raw).convert("RGB")

    result = render_cover(frame, spec, resolve(spec))
    result.image.save(args.out, quality=94, subsampling=0)
    print(format_report(spec, result.meta, args.out, args.profile))


if __name__ == "__main__":
    main()
