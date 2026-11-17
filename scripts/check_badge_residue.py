#!/usr/bin/env python3
"""Standalone QA gate: detect symmetric ORANGE/YELLOW TRIANGLE residue left by
TELEA inpainting of a solid-colour title badge (see SKILL §17.5b).

This is deliberately NOT wired into qa_video.py's pass/fail (which would risk
false positives on arena floor / uniforms). Run it over a batch to FIND clips
that need the vblend fix:

    python check_badge_residue.py [--root ~/Downloads/tiktok] [--fps 0.5]

Detection per sampled frame:
  TOP (top 45%): symmetric orange triangle pair (see SKILL §17.5b)
    - saturated warm mask (R>185, G 90..205, B<110)
    - "solid" blobs: connected component fill ratio > 0.55, area 400..9000 px
    - LEFT blob (cx<32%W) AND RIGHT blob (cx>68%W) at same y (within 8%H)
  BOTTOM (0.70..0.94H, central 0.20..0.80W): brand subscribe bar residue
    (see SKILL §17.5c) - a RED rounded play button (red blob containing a white
    ▶), typical of "篮球世界" UP watermark left behind after text-only erase.
  A clip is flagged only if the signature persists on >=3 sampled frames.

NOTE: bottom RED detection can false-positive on arena floor / red uniforms.
This is a FIND tool, not a hard gate - always eyeball a contact sheet of hits
before acting. Prints one line per hit; exit 0 always (--strict -> exit 1).
"""
import argparse, glob, os, subprocess, sys, tempfile
import numpy as np
import cv2

FFMPEG = os.environ.get("TTL_FFMPEG", os.path.expanduser(
    "~/.local/share/ffmpeg-static/ffmpeg"))


def warm_mask(rgb):
    a = rgb.astype(int)
    return ((a[:, :, 0] > 185) & (a[:, :, 1] > 90) &
            (a[:, :, 1] < 205) & (a[:, :, 2] < 110)).astype(np.uint8)


def tri_pair(frame, W, H):
    top = frame[:int(0.45 * H)]
    m = warm_mask(top)
    n, lab, stats, cents = cv2.connectedComponentsWithStats(m, 8)
    blobs = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 400 or area > 9000 or w < 18 or h < 18:
            continue
        fill = area / float(w * h)
        if fill < 0.55:
            continue
        # triangular blobs have width ~= height and low internal colour spread
        if not (0.5 <= w / float(h) <= 2.0):
            continue
        blobs.append((cents[i][0], cents[i][1], area))
    left = [b for b in blobs if b[0] < 0.32 * W]
    right = [b for b in blobs if b[0] > 0.68 * W]
    for l in left:
        for r in right:
            if abs(l[1] - r[1]) < 0.08 * H:
                return (l, r)
    return None


def brand_button(frame, W, H):
    """RED rounded play-button: a compact red blob containing white pixels,
    in the lower-central brand-bar zone. Returns True if found."""
    roi = frame[int(0.70 * H):int(0.94 * H), int(0.20 * W):int(0.80 * W)]
    a = roi.astype(int)
    red = ((a[:, :, 0] > 165) & (a[:, :, 1] < 100) &
           (a[:, :, 2] < 100)).astype(np.uint8)
    white = ((a[:, :, 0] > 205) & (a[:, :, 1] > 205) &
             (a[:, :, 2] > 205)).astype(np.uint8)
    if red.sum() < 40:
        return False
    # play button = red pixels immediately adjacent to white (the white ▶)
    near = cv2.dilate(white, np.ones((5, 5), np.uint8), iterations=1)
    btn = red & near
    n, lab, stats, cents = cv2.connectedComponentsWithStats(btn, 8)
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        # compact button-sized blob, not a big red floor/uniform region
        if 25 <= area <= 4000 and 8 <= w <= 90 and 8 <= h <= 90:
            return True
    return False


def scan_clip(mp4, fps):
    tmp = tempfile.mkdtemp(prefix="ttl-badge-")
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", mp4,
                    "-vf", f"fps={fps}", "-q:v", "4",
                    os.path.join(tmp, "f%05d.jpg")], check=False)
    frames = sorted(glob.glob(os.path.join(tmp, "f*.jpg")))
    if not frames:
        return 0, 0, 0
    top_hits = 0
    bot_hits = 0
    sample = frames[::max(1, len(frames) // 60)][:60]
    for f in sample:
        img = cv2.imread(f)
        if img is None:
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        H, W = rgb.shape[:2]
        if tri_pair(rgb, W, H):
            top_hits += 1
        if brand_button(rgb, W, H):
            bot_hits += 1
    return len(sample), top_hits, bot_hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/Downloads/tiktok"))
    ap.add_argument("--fps", type=float, default=0.5)
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    dirs = [os.path.join(a.root, d) for d in sorted(os.listdir(a.root))
            if os.path.isdir(os.path.join(a.root, d)) and
            os.path.exists(os.path.join(a.root, d, "final.mp4"))]
    flagged = []
    for d in dirs:
        n, th, bh = scan_clip(os.path.join(d, "final.mp4"), a.fps)
        kinds = []
        if th >= 3:
            kinds.append("TOP-triangle %d/%d" % (th, n))
        if bh >= 3:
            kinds.append("BOTTOM-brandbar %d/%d" % (bh, n))
        if kinds:
            flagged.append(os.path.basename(d))
            print("HIT  %-50s %s" %
                  (os.path.basename(d)[:50], ", ".join(kinds)))
    print("flagged %d / %d clips (eyeball each HIT before fixing)" %
          (len(flagged), len(dirs)))
    if a.strict and flagged:
        sys.exit(1)


if __name__ == "__main__":
    main()
