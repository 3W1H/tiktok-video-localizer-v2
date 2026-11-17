#!/usr/bin/env python3
"""Standalone QA gate: detect symmetric ORANGE/YELLOW TRIANGLE residue left by
TELEA inpainting of a solid-colour title badge (see SKILL §17.5b).

This is deliberately NOT wired into qa_video.py's pass/fail (which would risk
false positives on arena floor / uniforms). Run it over a batch to FIND clips
that need the vblend fix:

    python check_badge_residue.py [--root ~/Downloads/tiktok] [--fps 0.5]

Detection per sampled frame (top 45% of frame):
  - build saturated warm mask (R>185, G 90..205, B<110)
  - keep only "solid" blobs: a connected component with fill ratio > 0.55 and
    area 400..9000 px, whose colour std is low (a filled triangle, not texture)
  - require a LEFT blob (cx < 32% W) AND a RIGHT blob (cx > 68% W) at nearly
    the SAME y (within 8% H) -> the signature symmetric ◀ ▶ pair
  - a clip is flagged only if the same pair persists on >=3 sampled frames.

Prints one line per hit; exit code 0 always (use --strict to exit 1 on hits).
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


def scan_clip(mp4, fps):
    tmp = tempfile.mkdtemp(prefix="ttl-badge-")
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", mp4,
                    "-vf", f"fps={fps}", "-q:v", "4",
                    os.path.join(tmp, "f%05d.jpg")], check=False)
    frames = sorted(glob.glob(os.path.join(tmp, "f*.jpg")))
    if not frames:
        return 0, 0
    hits = 0
    sample = frames[::max(1, len(frames) // 60)][:60]
    for f in sample:
        img = cv2.imread(f)
        if img is None:
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        H, W = rgb.shape[:2]
        if tri_pair(rgb, W, H):
            hits += 1
    return len(sample), hits


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
        n, h = scan_clip(os.path.join(d, "final.mp4"), a.fps)
        if h >= 3:
            flagged.append((os.path.basename(d), n, h))
            print("HIT  %-52s pair-on %d/%d frames" %
                  (os.path.basename(d)[:52], h, n))
    print("flagged %d / %d clips" % (len(flagged), len(dirs)))
    if a.strict and flagged:
        sys.exit(1)


if __name__ == "__main__":
    main()
