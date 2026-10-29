#!/usr/bin/env python
"""delogo_static.py - remove a *static* burned-in watermark / reposter logo.

Why this exists
---------------
The normal text pipeline (detect_text_regions -> generate_masks -> inpaint_video)
only targets text that ends up as a localization item. English reposter
watermarks (e.g. "TONY-Zhong30") are usually hand-written style, OCR reads them
badly, and they never become an item -> no mask -> watermark stays in the final
video.

This tool detects a watermark by *temporal consistency* instead of OCR:

  A watermark is alpha-blended onto every frame at a fixed position, so if you
  average a large-sigma high-pass response over the whole clip the text shape
  stays sharp while everything else averages away. That averaged response is
  thresholded, dilated, and used with cv2.inpaint per frame.

Usage
-----
  # 1) find the region first (prints/saves a response map, no video written)
  python delogo_static.py --input in.mp4 --hunt --save-map hunt.png

  # 2) remove it
  python delogo_static.py --input in.mp4 --output out.mp4 \
      --bbox 470,468,820,610 --dilate 4

  # reuse a mask saved earlier
  python delogo_static.py --input in.mp4 --output out.mp4 --mask m.png

Notes
-----
* Audio is copied through untouched.
* --bbox should only *contain* the watermark, not the whole video: the
  threshold is computed inside that box, so a tight box gives a cleaner mask.
* --dilate is the safety margin in pixels. 2 is enough for crisp text, 4 for
  anti-aliased / soft script fonts. Too large smears the fill area.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

import cv2
import numpy as np


def probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return {"w": int(s["width"]), "h": int(s["height"]),
            "fps": float(num) / float(den), "fps_str": s["r_frame_rate"]}


def temporal_response(path: str, box=None, samples: int = 200, sigma: float = 12.0) -> np.ndarray:
    """Averaged high-pass response. Solid structure (watermark) survives."""
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    acc = None
    cnt = 0
    for t in np.linspace(1, max(n / fps - 1, 1), samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
        ok, fr = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if box is not None:
            x0, y0, x1, y1 = box
            g = g[y0:y1, x0:x1]
        hp = g - cv2.GaussianBlur(g, (0, 0), sigma)
        acc = hp if acc is None else acc + hp
        cnt += 1
    cap.release()
    if not cnt:
        raise SystemExit("no frames read")
    return acc / cnt


def build_mask(resp: np.ndarray, shape, box, thr: float, dilate_px: int,
               min_area: int = 10) -> np.ndarray:
    m = (resp > thr).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8), iterations=1)
    nl, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    keep = np.zeros_like(m)
    for i in range(1, nl):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            keep[lab == i] = 255
    if dilate_px > 0:
        k = 2 * dilate_px + 1
        keep = cv2.dilate(keep, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    full = np.zeros(shape, np.uint8)
    x0, y0, x1, y1 = box
    full[y0:y1, x0:x1] = keep
    return full


def render(src: str, dst: str, mask: np.ndarray, meta: dict, radius: int = 4) -> int:
    H, W = meta["h"], meta["w"]
    ys, xs = np.nonzero(mask)
    if not len(xs):
        raise SystemExit("empty mask")
    PAD = 20
    x0, x1 = max(int(xs.min()) - PAD, 0), min(int(xs.max()) + PAD + 1, W)
    y0, y1 = max(int(ys.min()) - PAD, 0), min(int(ys.max()) + PAD + 1, H)
    roim = np.ascontiguousarray(mask[y0:y1, x0:x1])

    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", src,
                            "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                           stdout=subprocess.PIPE)
    tmp = dst + ".video-only.mp4"
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo",
                            "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                            "-r", meta["fps_str"], "-i", "-", "-an",
                            "-c:v", "libx264", "-crf", "17", "-preset", "slow",
                            "-pix_fmt", "yuv420p", tmp], stdin=subprocess.PIPE)
    fb = W * H * 3
    n = 0
    while True:
        buf = dec.stdout.read(fb)
        if len(buf) < fb:
            break
        fr = np.frombuffer(buf, np.uint8).reshape(H, W, 3).copy()
        fr[y0:y1, x0:x1] = cv2.inpaint(fr[y0:y1, x0:x1].copy(), roim, radius,
                                       cv2.INPAINT_TELEA)
        enc.stdin.write(fr.tobytes())
        n += 1
        if n % 500 == 0:
            print(f"  {n} frames", flush=True)
    enc.stdin.close()
    enc.wait()
    dec.wait()
    if enc.returncode or dec.returncode:
        raise SystemExit("ffmpeg failed")

    # mux original audio back in
    has_audio = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", src],
        capture_output=True, text=True).stdout.strip() != ""
    if has_audio:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", tmp, "-i", src,
                        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                        "-movflags", "+faststart", dst], check=True)
        os.remove(tmp)
    else:
        os.replace(tmp, dst)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="remove a static burned-in watermark")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output")
    ap.add_argument("--bbox", help="x0,y0,x1,y1 region that contains the watermark")
    ap.add_argument("--mask", help="reuse a saved mask png (white = remove)")
    ap.add_argument("--thr", type=float, default=8.0, help="response threshold")
    ap.add_argument("--dilate", type=int, default=4, help="safety margin in px")
    ap.add_argument("--radius", type=int, default=4, help="inpaint radius")
    ap.add_argument("--samples", type=int, default=200)
    ap.add_argument("--hunt", action="store_true", help="only save the response map")
    ap.add_argument("--save-map", help="path for the response map png")
    ap.add_argument("--save-mask", help="path to write the generated mask png")
    a = ap.parse_args()

    meta = probe(a.input)
    box = None
    if a.bbox:
        box = [int(v) for v in a.bbox.split(",")]
        if len(box) != 4:
            raise SystemExit("--bbox needs x0,y0,x1,y1")
    else:
        box = [0, 0, meta["w"], meta["h"]]

    if a.hunt:
        resp = temporal_response(a.input, box, a.samples)
        img = np.clip((resp + 5) / 45 * 255, 0, 255).astype(np.uint8)
        cv2.imwrite(a.save_map or "hunt.png", img)
        print(f"response map -> {a.save_map or 'hunt.png'}  (bright = fixed overlay)")
        return 0

    if not a.output:
        raise SystemExit("--output is required")

    if a.mask:
        mask = cv2.imread(a.mask, 0)
        if mask is None:
            raise SystemExit(f"cannot read mask {a.mask}")
        mask = (mask > 127).astype(np.uint8) * 255
        if mask.shape != (meta["h"], meta["w"]):
            raise SystemExit(f"mask is {mask.shape}, video is {(meta['h'], meta['w'])}")
    else:
        resp = temporal_response(a.input, box, a.samples)
        mask = build_mask(resp, (meta["h"], meta["w"]), box, a.thr, a.dilate)

    if a.save_mask:
        cv2.imwrite(a.save_mask, mask)
        print(f"mask -> {a.save_mask} ({int((mask > 0).sum())} px)")

    n = render(a.input, a.output, mask, meta, a.radius)
    print(f"done: {n} frames -> {a.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
