#!/usr/bin/env python3
"""Patch the persistent top-left 'Airball' logo on an already-delivered video.

Only the logo ROI is rebuilt (vblend, four-edge background sampling on the
inpaint-free original frame). Everything else - the black intro card, all other
erases, subtitle burn, the final audio track - is byte-identical: video is
re-encoded from the final.mp4 frame stream, audio is stream-copied.

Usage:
  python patch_airball_logo.py --input final.mp4 --out work/final-patched.mp4 \
      --box 18,4,215,98 --start 1.33 --end 179.0
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys

# render_overlays.py contains the authoritative vblend implementation; import it
# so this patch and the main pipeline can never diverge. Works both from the
# skill tree and from a scratch copy on the Desktop.
SKILL_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if not os.path.exists(os.path.join(SKILL_SCRIPTS, "render_overlays.py")):
    SKILL_SCRIPTS = "/Users/jason/Public/skills/tiktok-video-localizer-v2/scripts"
sys.path.insert(0, SKILL_SCRIPTS)
from render_overlays import apply_vblend_regions  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402

FFMPEG = os.path.expanduser("~/.local/bin/ffmpeg")
FFPROBE = os.path.expanduser("~/.local/bin/ffprobe")


def probe(path: str) -> dict:
    raw = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration,codec_name",
         "-of", "json", path], capture_output=True, text=True, check=True).stdout
    s = json.loads(raw)["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return dict(w=int(s["width"]), h=int(s["height"]),
                fps=float(num) / float(den), fps_str=s["r_frame_rate"],
                n=int(s.get("nb_frames") or 0),
                duration=float(s.get("duration") or 0),
                codec=s.get("codec_name"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--box", default="18,4,215,98",
                    help="x1,y1,x2,y2 (x2/y2 exclusive), must clear the glyph by >=12px")
    ap.add_argument("--start", type=float, default=1.33)
    ap.add_argument("--end", type=float, default=179.0)
    ap.add_argument("--feather", type=int, default=6)
    ap.add_argument("--bilinear", type=int, default=1)
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--preset", default="veryfast")
    ap.add_argument("--x264-threads", type=int, default=4)
    a = ap.parse_args()

    meta = probe(a.input)
    x1, y1, x2, y2 = [int(v) for v in a.box.split(",")]
    print(f"input {meta['w']}x{meta['h']} {meta['fps']:.3f}fps "
          f"{meta['n']} frames {meta['duration']:.2f}s codec={meta['codec']}", flush=True)
    print(f"vblend box=({x1},{y1},{x2},{y2}) t=[{a.start},{a.end}) "
          f"feather={a.feather} bilinear={bool(a.bilinear)}", flush=True)

    tmp = a.out + ".video-only.mp4"
    dec = subprocess.Popen([FFMPEG, "-v", "error", "-i", a.input,
                            "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                           stdout=subprocess.PIPE)
    enc = subprocess.Popen(
        [FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{meta['w']}x{meta['h']}", "-r", meta["fps_str"], "-i", "-",
         "-an", "-c:v", "libx264", "-preset", a.preset, "-crf", str(a.crf),
         "-pix_fmt", "yuv420p", "-threads", str(a.x264_threads), tmp],
        stdin=subprocess.PIPE)
    fb = meta["w"] * meta["h"] * 3
    n = patched = 0
    while True:
        buf = dec.stdout.read(fb)
        if len(buf) < fb:
            break
        t = n / meta["fps"]
        fr = np.frombuffer(buf, np.uint8).reshape(meta["h"], meta["w"], 3).copy()
        if a.start <= t < a.end:
            # source == frame itself: rebuild from the *current* pixels, not
            # from any earlier inpaint. Feather ramp blends into untouched picture.
            fr = apply_vblend_regions(
                fr, [(x1, y1, x2, y2, a.start, a.end,
                      a.feather, bool(a.bilinear))], t, source=fr)
            patched += 1
        enc.stdin.write(fr.tobytes())
        n += 1
        if n % 900 == 0:
            print(f"  {n}/{meta['n']} frames ({patched} patched)", flush=True)
    enc.stdin.close()
    enc.wait()
    dec.wait()
    if enc.returncode or dec.returncode:
        raise SystemExit(f"ffmpeg failed: enc={enc.returncode} dec={dec.returncode}")

    # stream-copy the original audio: TTS/instrumental mix, untouched.
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", tmp, "-i", a.input,
                    "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                    "-movflags", "+faststart", a.out], check=True)
    os.remove(tmp)
    print(f"done: {n} frames, {patched} patched -> {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
