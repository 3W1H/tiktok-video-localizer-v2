#!/usr/bin/env python3
"""Fix symmetric orange/yellow TRIANGLE residue left by inpainting a solid-colour
title badge (e.g. orange rounded-rectangle with black text).

ROOT CAUSE
----------
auto_decide.emit() sizes the erase box from the OCR *text* box + 8px. OCR cannot
see the coloured badge behind the text, so the box edge lands INSIDE the solid
colour block. cv2.INPAINT_TELEA then samples from that coloured edge and
converges inward -> two symmetric filled triangles (◀ ▶) survive every frame.
It is static & full-duration, so the residue is on 100% of frames yet the QA
Chinese-Residue gate (text OCR only) reports pass. This bit us repeatedly on
"固色底+黑字" titles (加内特/科比十佳球/纳什 ... 2026-11-17).

FIX
---
For each erase item whose box interior is dominated by one saturated colour
(solid badge, not plain glyph strokes):
  1. measure the TRUE colour extent from the ORIGINAL source frame (coarse png),
     anchored near the existing box, capped so it cannot run into footage;
  2. grow the box to that extent + a small margin (so no coloured edge remains
     inside the box);
  3. switch fill method to `vblend` (4-side background sampling + feather),
     which rebuilds the area from the dark background OUTSIDE the badge instead
     of sampling the badge colour itself.

Only the two plan JSONs are edited here. Re-render with render_overlays.py, then
remux the ORIGINAL audio back (keep_original clips) -- see --remux-audio.

Usage:
  python fix_solid_badge.py --work-dir <.../work> --source-video <orig.mp4> \
      [--apply] [--remux-audio <final.mp4>]
  Without --apply it only reports what it would change (dry run).
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.environ.get("TTL_FFMPEG", os.path.expanduser(
    "~/.local/share/ffmpeg-static/ffmpeg"))

SOLID_RATIO = 0.40      # (unused legacy) overall saturated-pixel ratio
EDGE_SOLID_RATIO = 0.55  # saturated fraction of the box's INNER RING => solid badge
COLOUR_SAT = 90      # HSV saturation threshold for "coloured badge"
COLOUR_VAL = 100     # HSV value threshold
GROW_CAP = 90        # max px to grow the box beyond each edge
MARGIN = 8           # extra px beyond the measured colour extent


def coarse_frames(work):
    out = []
    for sub in ("frames/coarse", "frames/dense", "frames/scene"):
        d = os.path.join(work, sub)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".png"):
                out.append(os.path.join(d, f))
        if out:
            break
    return out[:20]


def saturated_mask(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return (hsv[:, :, 1] > COLOUR_SAT) & (hsv[:, :, 2] > COLOUR_VAL)


def analyse(work):
    plan_p = os.path.join(work, "localization-plan.json")
    mask_p = os.path.join(work, "masks", "masks-index.json")
    plan = json.load(open(plan_p, encoding="utf-8"))
    frames = [cv2.imread(f) for f in coarse_frames(work)]
    frames = [f for f in frames if f is not None]
    if not frames:
        print("  !! no source frames found; cannot measure colour extent")
        return None
    H, W = frames[0].shape[:2]
    changes = []
    for it in plan.get("items", []):
        bb = it.get("bbox") or {}
        x, y = int(bb.get("x", 0)), int(bb.get("y", 0))
        w, h = int(bb.get("w", 0)), int(bb.get("h", 0))
        if w < 20 or h < 20:
            continue
        # only static, long-lived items (badge/title), not short in-play caps
        if float(it.get("end_time", 0)) - float(it.get("start_time", 0)) < 3.0:
            continue
        if y > 0.45 * H:   # top-area title badges only
            continue
        edge_ratios = []
        for f in frames:
            roi = f[max(0, y):min(H, y + h), max(0, x):min(W, x + w)]
            if roi.size < 100:
                continue
            m = saturated_mask(roi)
            eh = max(2, roi.shape[0] // 12)
            ew = max(2, roi.shape[1] // 12)
            ring = np.concatenate([
                m[:eh, :].ravel(), m[-eh:, :].ravel(),
                m[:, :ew].ravel(), m[:, -ew:].ravel()])
            edge_ratios.append(float(ring.mean()))
        # A SOLID badge fills the box right up to its edges, so the inner ring
        # is mostly saturated. Plain glyph strokes ("橙字无底色") leave the ring
        # dark even when overall stroke coverage is high -- this is the real
        # discriminator and avoids false positives on dense orange text.
        if not edge_ratios or np.median(edge_ratios) < EDGE_SOLID_RATIO:
            continue
        # TRUE colour extent of the WHOLE title stack. A badge title is often
        # TWO stacked rows (line 1 "凯文·加内特" orange glyphs, line 2 "生涯十
        # 佳球" solid orange plate). Measuring only this box leaves the vblend
        # top edge slicing through line 1 -> the sampled top row carries orange
        # strokes and the fill grows vertical stripes / a row of orange nubs.
        # So scan the entire contiguous saturated band above AND below this box
        # (capped to the top 45% of frame) and merge every nearby title row.
        band_y0, band_y1 = 0, int(0.45 * H)
        wx0, wx1 = max(0, x - GROW_CAP), min(W, x + w + GROW_CAP)
        wy0, wy1 = band_y0, band_y1
        # find the vertical span of the connected saturated band around [y,y+h]
        band_m = np.zeros((band_y1 - band_y0, wx1 - wx0), dtype=bool)
        for f in frames:
            band_m |= saturated_mask(f[band_y0:band_y1, wx0:wx1])
        row_cov = band_m.mean(axis=1)
        near = np.arange(band_y0, band_y1)
        gy = min(28, h // 2)
        seed = (near >= y - gy) & (near <= y + h + gy)
        # expand while rows are still part of a coloured title band
        top_i = int(np.where(seed)[0].min())
        bot_i = int(np.where(seed)[0].max())
        # Stacked badges ("凯文·加内特" above "生涯十佳球") are separated by a
        # small dark gap (~25px). Bridge gaps up to GAP_BRIDGE px so both rows
        # merge into ONE vblend box -- otherwise the top edge slices row 1 and
        # its orange strokes get smeared into vertical stripes.
        cov_th = 0.08
        GAP_BRIDGE = 45
        def grow(cur, step):
            gap = 0
            while 0 <= cur + step < len(row_cov):
                nxt = cur + step
                if row_cov[nxt] > cov_th:
                    gap = 0
                elif (row_cov[nxt] > 0.01) or gap < GAP_BRIDGE:
                    gap += 1
                else:
                    break
                cur = nxt
            return cur
        top_i = grow(top_i, -1)
        bot_i = grow(bot_i, +1)
        wy0, wy1 = band_y0 + top_i, band_y0 + bot_i + 1
        ex0, ey0, ex1, ey1 = W, H, 0, 0
        for f in frames:
            m = saturated_mask(f[wy0:wy1, wx0:wx1])
            ys, xs = np.where(m)
            if not len(xs):
                continue
            ex0 = min(ex0, int(xs.min()) + wx0)
            ex1 = max(ex1, int(xs.max()) + wx0)
            ey0 = min(ey0, int(ys.min()) + wy0)
            ey1 = max(ey1, int(ys.max()) + wy0)
        if ex1 <= ex0 or ey1 <= ey0:
            continue
        # reject colour that spans both frame edges (that's footage, not badge)
        if ex0 <= 8 and ex1 >= W - 8:
            continue
        nx0 = max(0, min(x, ex0 - MARGIN))
        ny0 = max(0, min(y, ey0 - MARGIN))
        nx1 = min(W, max(x + w, ex1 + 1 + MARGIN))
        ny1 = min(H, max(y + h, ey1 + 1 + MARGIN))
        # never let a single badge grow into a giant region
        if (nx1 - nx0) > 1.5 * w or (ny1 - ny0) > 2.0 * h:
            nx0, nx1 = max(0, x - MARGIN), min(W, x + w + MARGIN)
            ny0, ny1 = max(0, y - MARGIN), min(H, y + h + MARGIN)
        changes.append({
            "item_id": it.get("item_id"),
            "old": dict(bb),
            "new": {"x": int(nx0), "y": int(ny0),
                    "w": int(nx1 - nx0), "h": int(ny1 - ny0)},
            "sat_ratio": round(float(np.median(edge_ratios)), 3),
        })
    return plan, changes


def change_json(work, plan, changes, apply):
    if not changes:
        print("  no solid-badge items detected")
        return
    ids = {c["item_id"] for c in changes}
    for c in changes:
        print("  %-16s %s -> %s (solid=%.0f%%)" % (
            c["item_id"], c["old"], c["new"], c["sat_ratio"] * 100))
    if not apply:
        print("  DRY RUN -- pass --apply to write")
        return
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    for p in (os.path.join(work, "localization-plan.json"),
              os.path.join(work, "masks", "masks-index.json")):
        if os.path.exists(p):
            shutil.copy2(p, p + ".pre-badgefix-" + ts)

    plan_p = os.path.join(work, "localization-plan.json")
    for it in plan.get("items", []):
        if it.get("item_id") not in ids:
            continue
        c = next(c for c in changes if c["item_id"] == it["item_id"])
        it["bbox"] = c["new"]
        it["fill_method"] = "vblend"
        it["vblend_feather"] = 10
        it["vblend_bilinear"] = True
        it["removal_method"] = ["vblend"]
    json.dump(plan, open(plan_p, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    # Regenerate mask index: drop the vblend items from the TELEA box list so
    # inpaint does not re-introduce the triangles underneath.
    mask_p = os.path.join(work, "masks", "masks-index.json")
    md = json.load(open(mask_p, encoding="utf-8"))
    before = len(md.get("masks", []))
    md["masks"] = [m for m in md.get("masks", [])
                   if m.get("item_id") not in ids]
    md["masks_generated"] = len(md["masks"])
    json.dump(md, open(mask_p, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("  wrote plan (%d items -> vblend); masks %d -> %d" %
          (len(ids), before, len(md["masks"])))


def remux_audio(out_mp4, work):
    """Put the untouched source audio back onto a freshly re-rendered silent mp4."""
    src = None
    probe = os.path.join(work, "probe.json")
    if os.path.exists(probe):
        src = json.load(open(probe, encoding="utf-8")).get("path")
    if not src or not os.path.exists(src):
        print("  !! source video not found; audio NOT remuxed:", src)
        return 3
    tmp = out_mp4 + ".remux.tmp.mp4"
    cmd = [FFMPEG, "-y", "-i", out_mp4, "-i", src,
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-shortest", tmp]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0 or not os.path.exists(tmp):
        print("  !! remux failed:", r.stderr.decode("utf-8", "replace")[-500:])
        return 4
    os.replace(tmp, out_mp4)
    print("  audio remuxed from", src)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--remux-audio", metavar="MP4",
                    help="after fixing, remux original audio onto this mp4")
    a = ap.parse_args()
    res = analyse(a.work_dir)
    if res is None:
        return 2
    plan, changes = res
    change_json(a.work_dir, plan, changes, a.apply)
    if a.remux_audio:
        return remux_audio(a.remux_audio, a.work_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
