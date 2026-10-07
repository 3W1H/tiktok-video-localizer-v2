#!/usr/bin/env python3
"""Single-pass renderer: inpaint original Chinese text regions, then draw
custom-styled English overlays with PIL (bold font, heavy stroke, drop shadow).

Why: the stock `clean_video.py` path draws every item with independent ffmpeg
drawtext filters, so overlapping time windows stack two texts on top of each
other, the font is a thin non-bold face, and there is no shadow. This script
replaces that pass with a controllable Pillow renderer.

Usage:
  python render_final.py --video SRC --work-dir W --output OUT.mp4
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from clean_video import find_font, probe_color_args  # noqa: E402
from common import (  # noqa: E402
    get_logger,
    normalize_persistent_starts,
    log_persistent_snaps,
    HEAD_SNAP_SEC,
    PERSISTENT_MIN_SEC,
)
import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

log = get_logger()

# Optional per-process OpenCV thread cap.
#
# NOT the load fix on this machine. The conda build reports
# "Parallel framework: GCD", and cv2.setNumThreads() is ignored by that backend:
# getNumThreads() stays at the core count no matter what is requested. Measured
# 2026-09-27 -- cv2.inpaint on a 1080x1920 frame takes ~0.30s at 1, 2, 4 and 8
# requested threads, i.e. inpaint here is effectively single-threaded.
#
# So the OpenCV side cannot be throttled; the fix for the 2026-09-27 load spike
# (load average 230+ with 12 parallel render segments) is fewer segments on the
# renderer plus --x264-threads on the ffmpeg side, not this knob. Kept because
# it is a no-op when unset and does take effect on OpenMP/TBB builds.
_cv_threads = os.environ.get("LOCALIZER_CV_THREADS")
if _cv_threads:
    try:
        cv2.setNumThreads(max(1, int(_cv_threads)))
    except (TypeError, ValueError):
        pass

FFMPEG = shutil.which("ffmpeg") or os.path.expanduser("~/.local/bin/ffmpeg")

BOLD_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
]

GOLD = (255, 215, 0)   # fallback brand yellow (only when no --accent/--profile)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

# Brand colour = ONE value shared by cover and in-video marks.
#
# 2026-09-27: the brand mark ("logo") was hard-coded WHITE here while the cover
# drew the profile's yellow -> the delivered videos had a white wordmark over a
# yellow cover. User's call: "video logo must match the cover". So title, hook
# and logo now all take `accent`, and `accent` is resolved from the SAME
# profile field the cover uses (`cover_style.fill_color`) via --profile, with
# --accent as an explicit override.
#
# Subtitles stay WHITE on purpose: spoken subtitles are not brand marks, and
# white-on-video is what every platform caption style uses.
ACCENT = GOLD

ROI_PAD = 16  # context pixels around a mask box before inpainting that ROI

# Windows in the plan are OCR-derived and slightly tight, so the original text
# flashes for a few frames at each boundary (t=0 in particular: the first
# subtitle starts at 0.2s, so opening the file showed the original Chinese).
# Every mask/subtitle window is padded by these amounts on both ends.
PAD_HEAD = 0.5
PAD_TAIL = 0.35

# Brand mark ("logo") drawn on every video: the watermark slot is reused for
# the channel name. Size is a fraction of the frame width so the mark looks the
# same on 720/1080/1280-wide videos. 0.32 keeps it above the >=30% floor from
# SKILL.md 12.5 while staying "a bit bigger, not huge" (2026-09-27 feedback:
# the old 17/20px text was ~20% of the width and read as too small).
# Category names that mean "timed on-screen text" (see §1 5-action model /
# prompts/classify_text.md). All spellings must be accepted -- matching only
# the literal "subtitle" silently dropped every spoken subtitle from the
# render. Fixed 2026-09-27.
SUBTITLE_CATEGORIES = ("subtitle", "spoken_subtitle", "caption_overlay")

# Persistent head-overlay normalisation (title/slogan/watermark start snapped to
# 0.0 when they cover the head) lives in `common.py` -- one source of truth for
# prepare_plan.py (plan draft) / generate_masks.py (inpaint window) / this file
# (draw window). See `normalize_persistent_starts` there for the 2026-09-27
# incident it prevents.

LOGO_RATIO = 0.32       # target text width / frame width
LOGO_STROKE = 4
LOGO_MIN_SIZE = 12


def merge_boxes(boxes, gap=24):
    """Merge overlapping/nearby boxes so each frame needs only a few inpaints."""
    out = []
    for b in sorted(boxes, key=lambda b: (b[1], b[0])):
        x1, y1, x2, y2 = b
        for m in out:
            if not (x2 < m[0] - gap or x1 > m[2] + gap or
                    y2 < m[1] - gap or y1 > m[3] + gap):
                m[0] = min(m[0], x1)
                m[1] = min(m[1], y1)
                m[2] = max(m[2], x2)
                m[3] = max(m[3], y2)
                break
        else:
            out.append([x1, y1, x2, y2])
    return out


def inpaint_regions(frame, boxes, frame_w, frame_h, radius):
    """Inpaint only the ROI around each merged box. Equivalent to masking the
    whole frame but ~10-20x faster, because cv2.inpaint cost scales with the
    image it is given, not with the masked area."""
    kernel = np.ones((3, 3), np.uint8)
    for bx1, by1, bx2, by2 in merge_boxes(boxes):
        px1 = max(0, bx1 - ROI_PAD)
        py1 = max(0, by1 - ROI_PAD)
        px2 = min(frame_w, bx2 + ROI_PAD)
        py2 = min(frame_h, by2 + ROI_PAD)
        sub = frame[py1:py2, px1:px2]
        if sub.size == 0:
            continue
        m = np.zeros(sub.shape[:2], dtype=np.uint8)
        for ox1, oy1, ox2, oy2 in boxes:
            if ox2 < px1 or ox1 > px2 or oy2 < py1 or oy1 > py2:
                continue
            mx1 = max(0, ox1 - px1)
            my1 = max(0, oy1 - py1)
            mx2 = min(sub.shape[1], ox2 - px1)
            my2 = min(sub.shape[0], oy2 - py1)
            m[my1:my2, mx1:mx2] = 255
        m = cv2.dilate(m, kernel, iterations=1)
        frame[py1:py2, px1:px2] = cv2.inpaint(sub, m, radius, cv2.INPAINT_TELEA)
    return frame


def pick_bold_font():
    for p in BOLD_CANDIDATES:
        if os.path.exists(p):
            return p
    return find_font()


def measure(font, text):
    """Return (width, top, bottom) of rendered text."""
    l, t, r, b = font.getbbox(text)
    return r - l, l, t, b


def fit_width(path, text, target_w, max_size=240):
    """Largest Arial-size whose rendered width stays <= target_w."""
    size = LOGO_MIN_SIZE
    for s in range(LOGO_MIN_SIZE, max_size):
        w = measure(ImageFont.truetype(path, s), text)[0]
        if w > target_w:
            break
        size = s
    return ImageFont.truetype(path, size), size


def logo_font_size(frame_w, ratio=LOGO_RATIO, font_path=None, text="Hoop Dynasty Daily"):
    """Public helper: font size for the brand mark on a frame of width frame_w."""
    font_path = font_path or pick_bold_font()
    return fit_width(font_path, text, ratio * frame_w)[1]


def fit_font(path, text, size, max_w, min_size=18):
    """Shrink font size until the text fits max_w."""
    for s in range(size, min_size - 1, -2):
        f = ImageFont.truetype(path, s)
        w = measure(f, text)[0]
        if w <= max_w:
            return f, s
    return ImageFont.truetype(path, min_size), min_size


def draw_centered(draw, text, font, cy, img_w, fill, stroke_w=5,
                  stroke_fill=BLACK, shadow=True, alpha=255):
    """Draw text horizontally centered, vertically centered on cy."""
    w, l, t, b = measure(font, text)
    x = (img_w - w) // 2 - l
    y = int(cy - (t + b) / 2)

    def col(c):
        return (c[0], c[1], c[2], alpha)

    if shadow:
        draw.text((x, y + 4), text, font=font, fill=col((0, 0, 0)),
                  stroke_width=stroke_w, stroke_fill=col((0, 0, 0)))
    draw.text((x, y), text, font=font, fill=col(fill),
              stroke_width=stroke_w, stroke_fill=col(stroke_fill))


def resolve_accent(accent_str="", profile=""):
    """Brand yellow for title / hook / logo -- same source as the cover colour.

    Priority: ``--accent "R,G,B"`` > ``--profile``'s ``cover_style.fill_color``
    > ``GOLD``. Pass ``--profile nba`` (name or path) so the in-video marks and
    the cover can never drift apart again; that drift is exactly what shipped
    white logos over yellow covers on 2026-09-27.
    """
    if accent_str:
        parts = [p.strip() for p in accent_str.replace(";", ",").split(",")]
        if len(parts) != 3 or not all(parts):
            raise SystemExit(f"--accent needs 'R,G,B' (got {accent_str!r})")
        return tuple(max(0, min(255, int(float(p)))) for p in parts)

    if profile:
        path = profile
        if not os.path.exists(path):
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "profiles", f"{profile}.yaml")
        if not os.path.exists(path):
            raise SystemExit(f"--profile not found: {profile}")
        try:
            import yaml
        except ImportError:
            raise SystemExit("PyYAML required for --profile (pip install pyyaml)")
        data = yaml.safe_load(open(path, encoding="utf-8")) or {}
        col = (data.get("cover_style") or {}).get("fill_color") or []
        if len(col) != 3:
            raise SystemExit(f"{path}: cover_style.fill_color missing or invalid")
        return tuple(int(c) for c in col)

    print(f"!! no --profile/--accent: brand colour falls back to GOLD {GOLD} "
          f"and may not match the cover. Pass --profile <name>.")
    return GOLD


def build_overlays(plan, frame_w, frame_h, font_path, hook_seconds=3.4,
                   logo_text=None, logo_ratio=LOGO_RATIO, logo_y=None,
                   draw_logo=True, pad_head=PAD_HEAD, pad_tail=PAD_TAIL,
                   accent=GOLD, head_snap=HEAD_SNAP_SEC,
                   persistent_min_sec=PERSISTENT_MIN_SEC):
    """Translate plan items into timed draw instructions.

    The source watermark slot is reused for the channel brand mark (logo),
    sized as logo_ratio x frame width so every video in the series matches.
    """
    # Safety net: force head-covering persistent overlays to start at 0.0, so a
    # hand-written plan with start_time=1.0 can not leave the opening frames
    # uncleaned (see common.normalize_persistent_starts).
    log_persistent_snaps(
        normalize_persistent_starts(plan, head_snap, persistent_min_sec),
        "render_overlays")

    items = plan.get("items", [])
    for n, it in enumerate(items):
        it["_index"] = n
    subs, titles, hooks, logos = [], [], [], []

    for it in items:
        if it.get("action") not in ("translate", "remove"):
            continue
        text = it.get("target_text")
        if not text:
            continue
        bbox = it.get("bbox") or {}
        cat = it.get("category")
        cy = bbox.get("y", 0) + bbox.get("h", 0) / 2
        start = max(0.0, float(it.get("start_time", 0)) - pad_head)
        end = float(it.get("end_time", 999999)) + pad_tail

        if cat == "watermark":
            # original watermark is inpainted away; the brand mark goes back on
            if draw_logo:
                logos.append({"text": logo_text or text,
                              "cy": logo_y if logo_y is not None else cy,
                              "a": start, "b": end})
            continue
        if cat == "title":
            if cy < frame_h * 0.35:
                titles.append({"text": text, "cy": cy, "a": start, "b": end})
            else:
                hooks.append({"text": text, "cy": cy, "a": start, "b": hook_seconds})
        elif cat in SUBTITLE_CATEGORIES:
            # The classification prompt (prompts/classify_text.md) names the
            # spoken-subtitle category `spoken_subtitle`, while older plans in
            # this repo used `subtitle`. Accept both -- matching only the
            # literal "subtitle" silently dropped every spoken subtitle from
            # the render (mask still inpainted the Chinese away, so the hole was
            # left bare). Fixed 2026-09-27.
            subs.append({"text": text, "cy": cy, "a": start, "b": end,
                         "plan_index": it.get("_index")})

    # de-overlap subtitles: keep the later-starting line, trim the earlier one
    subs.sort(key=lambda s: (s["a"], s["b"]))
    for i in range(len(subs) - 1):
        if subs[i]["b"] > subs[i + 1]["a"]:
            subs[i]["b"] = max(subs[i]["a"] + 0.4, subs[i + 1]["a"] - 0.02)

    max_w = frame_w - 70
    out = []
    for t in titles:
        f, s = fit_font(font_path, t["text"], 36, max_w)
        out.append({"text": t["text"], "cy": t["cy"], "a": t["a"], "b": t["b"],
                    "font": f, "size": s, "fill": accent, "stroke": 4, "shadow": True,
                    "alpha": 255, "kind": "title"})
    for h in hooks:
        f, s = fit_font(font_path, h["text"], 32, max_w)
        out.append({"text": h["text"], "cy": h["cy"], "a": h["a"], "b": h["b"],
                    "font": f, "size": s, "fill": accent, "stroke": 4, "shadow": True,
                    "alpha": 255, "kind": "hook"})
    for sb in subs:
        f, s = fit_font(font_path, sb["text"], 40, max_w)
        out.append({"text": sb["text"], "cy": sb["cy"], "a": sb["a"], "b": sb["b"],
                    "font": f, "size": s, "fill": WHITE, "stroke": 5, "shadow": True,
                    "alpha": 255, "kind": "sub"})

    # brand mark (logo): one entry per unique text/position, sized by frame width
    if draw_logo:
        seen = set()
        for lg in logos:
            key = (lg["text"], round(lg["cy"]))
            if key in seen:
                continue
            seen.add(key)
            f, s = fit_width(font_path, lg["text"], logo_ratio * frame_w)
            out.append({"text": lg["text"], "cy": lg["cy"], "a": lg["a"],
                        "b": lg["b"], "font": f, "size": s, "fill": accent,
                        "stroke": LOGO_STROKE, "shadow": True, "alpha": 255,
                        "kind": "logo"})
        if not logos and logo_text:
            # plan has no watermark slot (e.g. clean source) -> place the mark
            cy = logo_y if logo_y is not None else frame_h * 0.9
            f, s = fit_width(font_path, logo_text, logo_ratio * frame_w)
            out.append({"text": logo_text, "cy": cy, "a": 0.0, "b": 9e9,
                        "font": f, "size": s, "fill": accent,
                        "stroke": LOGO_STROKE, "shadow": True, "alpha": 255,
                        "kind": "logo"})

    return {"draws": out, "n_title": len(titles), "n_hook": len(hooks),
            "n_sub": len(subs), "n_logo": sum(1 for d in out if d["kind"] == "logo"),
            "subs": subs}


def sync_timeline(work_dir, subs):
    """Write the de-overlapped subtitle windows back into timeline.json so the
    SRT generator and qa_video's conflict check agree with what was drawn."""
    path = os.path.join(work_dir, "timeline.json")
    if not os.path.exists(path):
        print("timeline.json not found, skip sync")
        return 0
    data = json.load(open(path))
    entries = data.get("entries", [])
    by_index = {e.get("index"): e for e in entries}
    changed = 0
    for s in subs:
        e = by_index.get(s.get("plan_index"))
        if e is None:
            continue
        if e.get("final_start") != s["a"] or e.get("final_end") != s["b"]:
            e["final_start"] = round(s["a"], 3)
            e["final_end"] = round(s["b"], 3)
            changed += 1
    bak = path + ".bak"
    if changed and not os.path.exists(bak):
        import shutil as _sh
        _sh.copy(path, bak)
    data["note_deoverlap"] = "final_start/final_end trimmed by render_overlays.py"
    json.dump(data, open(path, "w"), ensure_ascii=False, indent=1)
    print(f"timeline synced: {changed} entries trimmed (backup: {os.path.basename(bak)})")
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--inpaint-radius", type=int, default=5)
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--hook-seconds", type=float, default=3.4)
    ap.add_argument("--logo-text", default="",
                    help="override brand mark text (default: plan watermark target_text)")
    ap.add_argument("--logo-ratio", type=float, default=LOGO_RATIO,
                    help=f"brand mark width / frame width (default {LOGO_RATIO})")
    ap.add_argument("--logo-y", type=float, default=None,
                    help="brand mark vertical CENTER in px (default: watermark bbox center)")
    ap.add_argument("--accent", default="",
                    help="brand yellow 'R,G,B' for title/hook/logo; wins over --profile")
    ap.add_argument("--profile", default="",
                    help="profile name (nba/sports/generic) or yaml path; its "
                         "cover_style.fill_color becomes the brand yellow so the "
                         "in-video marks and the cover can never drift apart")
    ap.add_argument("--no-logo", action="store_true", help="do not draw the brand mark")
    ap.add_argument("--no-inpaint", action="store_true",
                    help="skip inpainting (logo/title-only pass on an already-cleaned video)")
    ap.add_argument("--save-clean-base", default="",
                    help="ALSO write the inpainted, text-free frames to this path. "
                         "Re-running just to change subtitle wording then costs "
                         "~2 min with '--video <base> --no-inpaint' instead of "
                         "re-inpainting the whole clip (~30 min).")
    ap.add_argument("--pad-head", type=float, default=PAD_HEAD,
                    help=f"extend every text window backwards by N s (default {PAD_HEAD})")
    ap.add_argument("--pad-tail", type=float, default=PAD_TAIL,
                    help=f"extend every text window forwards by N s (default {PAD_TAIL})")
    ap.add_argument("--head-snap", type=float, default=HEAD_SNAP_SEC,
                    help=f"a persistent title/slogan/watermark starting within N s "
                         f"of t=0 is snapped to 0.0 so the opening frames get "
                         f"cleaned (default {HEAD_SNAP_SEC})")
    ap.add_argument("--persistent-min-sec", type=float, default=PERSISTENT_MIN_SEC,
                    help=f"minimum duration for the above snap to apply -- keeps "
                         f"short lower-thirds untouched (default {PERSISTENT_MIN_SEC})")
    ap.add_argument("--print-logo-size", action="store_true",
                    help="print the computed brand mark font size and exit")
    ap.add_argument("--sync-timeline", action="store_true",
                    help="write de-overlapped windows back to work/timeline.json")
    ap.add_argument("--start-time", type=float, default=0.0,
                    help="skip source before this timestamp (sampling)")
    ap.add_argument("--max-frames", type=int, default=0,
                    help="stop after N written frames (sampling)")
    ap.add_argument("--ffmpeg-log", default=os.environ.get("LOCALIZER_FFMPEG_LOG", ""),
                    help="write each ffmpeg child's stderr to <path>.main / <path>.base "
                         "instead of /dev/null. Without this a dead pipe surfaces only as "
                         "'BrokenPipeError' and ffmpeg's actual error is thrown away "
                         "(2026-09-28: cost two debugging rounds on 03).")
    ap.add_argument("--x264-threads", type=int, default=0,
                    help="libx264 thread count; 0 = auto (all cores). Set a "
                         "small number when running several slices in parallel, "
                         "otherwise every slice spawns a full thread pool and "
                         "the box thrashes (load hit 114/8 cores on a 4-core "
                         "Intel Mac).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    plan = json.load(open(os.path.join(args.work_dir, "localization-plan.json")))
    masks_data = json.load(open(os.path.join(args.work_dir, "masks", "masks-index.json")))
    masks = masks_data.get("masks", [])

    cap = cv2.VideoCapture(args.video)
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    font_path = pick_bold_font()
    accent = resolve_accent(args.accent, args.profile)
    ov = build_overlays(plan, frame_w, frame_h, font_path, args.hook_seconds,
                        logo_text=args.logo_text or None,
                        logo_ratio=args.logo_ratio,
                        logo_y=args.logo_y,
                        draw_logo=not args.no_logo,
                        pad_head=args.pad_head,
                        pad_tail=args.pad_tail,
                        head_snap=args.head_snap,
                        persistent_min_sec=args.persistent_min_sec,
                        accent=accent)
    print(f"font: {font_path}")
    print(f"accent (title/hook/logo): rgb{tuple(accent)} "
          f"#{accent[0]:02X}{accent[1]:02X}{accent[2]:02X}"
          f"{(' from --profile ' + args.profile) if args.profile else ''}"
          f"{(' from --accent' if args.accent else '')}")
    print(f"overlays: {ov['n_title']} title / {ov['n_hook']} hook / "
          f"{ov['n_sub']} subs / {ov['n_logo']} logo")
    for d in ov["draws"][:3]:
        print(f"  [{d['a']:.1f}-{d['b']:.1f}] {d['kind']} size={d['size']} {d['text']!r}")
    print(f"  ... {len(ov['draws'])} total")
    for d in ov["draws"]:
        if d["kind"] == "logo":
            print(f"  LOGO {d['text']!r} size={d['size']} "
                  f"width={measure(d['font'], d['text'])[0]}px "
                  f"({measure(d['font'], d['text'])[0] / frame_w:.1%} of {frame_w}px) "
                  f"cy={d['cy']} window=[{d['a']:.1f},{d['b']:.1f}]")

    if args.print_logo_size:
        cap.release()
        return

    if args.dry_run:
        cap.release()
        if args.sync_timeline:
            sync_timeline(args.work_dir, ov["subs"])
        return

    if args.sync_timeline:
        sync_timeline(args.work_dir, ov["subs"])

    threads = ["-threads", str(args.x264_threads)] if args.x264_threads else []

    def ff_err(tag):
        """ffmpeg child stderr -> file when --ffmpeg-log is set, else /dev/null."""
        if not args.ffmpeg_log:
            return subprocess.DEVNULL
        os.makedirs(os.path.dirname(os.path.abspath(args.ffmpeg_log)), exist_ok=True)
        return open(f"{args.ffmpeg_log}.{tag}", "wb")

    cmd = [FFMPEG, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{frame_w}x{frame_h}", "-r", str(fps), "-i", "-",
           "-an", "-c:v", "libx264", "-preset", args.preset,
           "-crf", str(args.crf), "-pix_fmt", "yuv420p"] + threads
    cmd += probe_color_args(args.video)
    cmd.append(args.output)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=ff_err("main"))

    # Optional second sink: the inpainted-but-unlettered frames. Skipped when
    # inpainting is off (the source is already clean, so a copy adds nothing).
    base_proc = None
    if args.save_clean_base and not args.no_inpaint:
        bcmd = [FFMPEG, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{frame_w}x{frame_h}", "-r", str(fps), "-i", "-",
                "-an", "-c:v", "libx264", "-preset", args.preset,
                "-crf", str(args.crf), "-pix_fmt", "yuv420p"] + threads
        bcmd += probe_color_args(args.video)
        bcmd.append(args.save_clean_base)
        base_proc = subprocess.Popen(bcmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL,
                                     stderr=ff_err("base"))

    def alive(p, tag):
        """Turn a dead ffmpeg child into an actionable error, not a bare BrokenPipe."""
        if p.poll() is not None:
            hint = (f" (ffmpeg stderr: {args.ffmpeg_log}.{tag})"
                    if args.ffmpeg_log else
                    " (rerun with --ffmpeg-log /tmp/ff.log to see ffmpeg's error)")
            raise RuntimeError(
                f"{tag} ffmpeg exited early rc={p.returncode} before frame "
                f"{written} of segment; output={args.output}{hint}")

    idx = 0
    written = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = idx / fps
        idx += 1
        if t < args.start_time:
            continue
        if args.max_frames and written >= args.max_frames:
            break

        boxes = []
        if not args.no_inpaint:
            for m in masks:
                # pad the window like the text windows, otherwise the original
                # text flashes for the boundary frames (t<0.2 was visible before)
                if (m["start_time"] - args.pad_head) <= t <= (m["end_time"] + args.pad_tail):
                    b = m["bbox"]
                    boxes.append([max(0, b["x"] - 2), max(0, b["y"] - 2),
                                  min(frame_w, b["x"] + b["w"] + 2),
                                  min(frame_h, b["y"] + b["h"] + 2)])
        if boxes:
            frame = inpaint_regions(frame, boxes, frame_w, frame_h, args.inpaint_radius)

        # snapshot the clean frame BEFORE drawing on top of it
        if base_proc is not None:
            alive(base_proc, "clean-base")
            base_proc.stdin.write(frame.tobytes())

        active = [d for d in ov["draws"] if d["a"] <= t < d["b"]]
        if active:
            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            dr = ImageDraw.Draw(layer)
            for d in active:
                draw_centered(dr, d["text"], d["font"], d["cy"], frame_w,
                              d["fill"], d["stroke"], BLACK, d["shadow"], d["alpha"])
            img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
            frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

        alive(proc, "main")
        proc.stdin.write(frame.tobytes())
        written += 1
        if written % 500 == 0:
            print(f"  {written} frames written (src idx {idx})", flush=True)

    cap.release()
    proc.stdin.close()
    rc = proc.wait()
    if base_proc is not None:
        base_proc.stdin.close()
        brc = base_proc.wait()
        print(f"clean base -> {args.save_clean_base} rc={brc}")
    print(f"done written={written} src_frames={idx} rc={rc} -> {args.output}")


if __name__ == "__main__":
    main()
