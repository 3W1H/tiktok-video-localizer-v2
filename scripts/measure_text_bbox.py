#!/usr/bin/env python3
"""
measure_text_bbox.py — 用颜色阈值精确测量屏幕文字的 bbox 与出现时间。

存在理由（2026-09-22 两次踩坑）：
  只靠肉眼在缩略图/放大图上"估"文字位置和时间范围，结果：
  ① C 的红色标题实际持续 0.0-4.3s，plan 里写 0.0-3.6s -> 0.7 秒中文原样留在成片里；
  ② A 的中文标题实际在 x=169-563/y=339-375，plan 里写 x=85-585/y=295-395 -> 框过大，
     清理后容易留下明显的灰色补丁。
  肉眼镜位在缩略图尺度上必然有偏差。而视频里的频道字幕/水印几乎都是**高饱和描边色**
  （红字黄字白字），用颜色阈值可以做到像素级精确，且能自动求出起止时间。

本脚本只做测量，不改视频。产出可直接填进 localization-plan.json 的 bbox/start/end。

用法：
  # C 的红色描边大标题
  python measure_text_bbox.py --video c.mp4 --color red --y-min 0 --y-max 200
  # A 的黄色标题（全片常驻）
  python measure_text_bbox.py --video a.mp4 --color yellow --y-min 300 --y-max 400
  # 底部白色字幕带
  python measure_text_bbox.py --video c.mp4 --color white --y-min 700 --y-max 800 --step 0.2
  # 输出为可直接复制的 JSON bbox
  python measure_text_bbox.py --video c.mp4 --color red --y-max 200 --json

提示：
  --color white 最容易被画面亮部干扰，务必配合 --y-min/--y-max 限定搜索带。
  --color red 会被红色球衣/场地污染，同样建议限定 y 带 + 提高 --min-pixels。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

import cv2
import numpy as np

COLOR_RULES = {
    # name: (描述, lambda r,g,b -> mask)
    "red": ("红/橙红（频道标题常见）", lambda r, g, b: (r > 140) & (r - g > 70) & (r - b > 70)),
    "yellow": ("亮黄（TikTok 风标题常见）", lambda r, g, b: (r > 200) & (g > 170) & (b < 110) & (np.abs(r - g) < 70)),
    "white": ("白/浅灰（字幕常见）", lambda r, g, b: (r > 200) & (g > 200) & (b > 200)),
    "cyan": ("青蓝（部分台标）", lambda r, g, b: (b > 150) & (g > 140) & (r < 120)),
    "magenta": ("品红/紫（部分台标）", lambda r, g, b: (r > 130) & (b > 130) & (g < 110)),
}


def which_ffmpeg(explicit=None):
    for c in (explicit, shutil.which("ffmpeg"), os.path.expanduser("~/.local/bin/ffmpeg")):
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    print("[measure_bbox] FATAL: ffmpeg not found", file=sys.stderr)
    sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description="Measure on-screen text bbox/time via color threshold")
    ap.add_argument("--video", required=True)
    ap.add_argument("--color", default="red", choices=sorted(COLOR_RULES.keys()))
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--step", type=float, default=0.25, help="sampling interval in seconds")
    ap.add_argument("--y-min", type=int, default=0, help="top of search band (px)")
    ap.add_argument("--y-max", type=int, default=None, help="bottom of search band (px)")
    ap.add_argument("--min-pixels", type=int, default=300, help="ignore frames below this pixel count")
    ap.add_argument("--peak-ratio", type=float, default=0.5,
                    help="keep only frames with >= peak*ratio pixels (0.5 default; lower to catch fading titles)")
    ap.add_argument("--quantile", type=float, default=5.0,
                    help="trim this %% from each bbox edge to reject moving-background outliers")
    ap.add_argument("--pad", type=int, default=6, help="padding added to the merged bbox")
    ap.add_argument("--json", action="store_true", help="print a localization-plan-ready bbox JSON")
    ap.add_argument("--ffmpeg", default=None)
    args = ap.parse_args()

    video = os.path.expanduser(args.video)
    if not os.path.exists(video):
        print(f"[measure_bbox] FATAL: video not found: {video}", file=sys.stderr)
        return 2

    ffmpeg = which_ffmpeg(args.ffmpeg)
    probe = ffmpeg.replace("ffmpeg", "ffprobe")
    if not os.path.exists(probe):
        probe = shutil.which("ffprobe") or ffmpeg

    dur = args.end
    if dur is None:
        try:
            r = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=noprint_wrappers=1:nokey=1", video],
                               capture_output=True, text=True, check=True)
            dur = float(r.stdout.strip())
        except Exception:
            dur = 0.0

    desc, rule = COLOR_RULES[args.color]
    print(f"[measure_bbox] color={args.color} ({desc})  scan={args.start:.2f}-{dur:.2f}s  step={args.step}s")

    tmp = tempfile.mkdtemp(prefix="measure_bbox_")
    hits = []
    ts = args.start
    while ts <= dur + 1e-6:
        dst = os.path.join(tmp, "f.png")
        rc = subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", f"{ts:.3f}", "-i", video,
                             "-frames:v", "1", dst], capture_output=True).returncode
        ts += args.step
        if rc != 0 or not os.path.exists(dst):
            continue
        im = cv2.imread(dst)
        if im is None:
            continue
        y0 = max(0, args.y_min)
        y1 = im.shape[0] if args.y_max is None else min(im.shape[0], args.y_max)
        if y1 <= y0:
            continue
        band = im[y0:y1, :, :]
        b = band[:, :, 0].astype(int)
        g = band[:, :, 1].astype(int)
        r = band[:, :, 2].astype(int)
        m = rule(r, g, b)
        n = int(m.sum())
        if n >= args.min_pixels:
            ys, xs = np.where(m)
            hits.append({
                "t": round(ts - args.step, 2),
                "n": n,
                "x0": int(xs.min()), "x1": int(xs.max()),
                "y0": int(ys.min()) + y0, "y1": int(ys.max()) + y0,
            })

    shutil.rmtree(tmp, ignore_errors=True)

    if not hits:
        print(f"[measure_bbox] NO {args.color} text found in band y={args.y_min}-{args.y_max or 'bottom'}. "
              f"Nothing to clean there.")
        return 0

    # ---- 三重收敛：密度过滤 -> 最长连续段 -> 分位数裁剪 ----
    # 光有颜色阈值会被画面里的同色物体污染：红色球衣把 bbox 撑到画面边缘、
    # 零散帧把时间范围拉长。文字的三个特征可以用来收敛：
    #   ① 密度高（同色像素量大）  ② 时间连续（不是闪一下）  ③ 位置稳定（bbox 不漂移）
    peak = max(h["n"] for h in hits)
    strong = [h for h in hits if h["n"] >= peak * args.peak_ratio]

    strong.sort(key=lambda h: h["t"])
    segments, cur = [], [strong[0]]
    for h in strong[1:]:
        if h["t"] - cur[-1]["t"] <= args.step * 2.5:
            cur.append(h)
        else:
            segments.append(cur)
            cur = [h]
    segments.append(cur)
    seg = max(segments, key=len)
    if len(segments) > 1:
        print(f"[measure_bbox] found {len(segments)} temporal cluster(s); using the longest "
              f"({len(seg)} frames at t={seg[0]['t']:.2f}-{seg[-1]['t']:.2f}s)")

    q = args.quantile
    x0 = int(np.percentile([h["x0"] for h in seg], q))
    x1 = int(np.percentile([h["x1"] for h in seg], 100 - q))
    y0 = int(np.percentile([h["y0"] for h in seg], q))
    y1 = int(np.percentile([h["y1"] for h in seg], 100 - q))
    t0 = seg[0]["t"]
    t1 = seg[-1]["t"] + args.step

    p = args.pad
    bx = max(0, x0 - p); by = max(0, y0 - p)
    bw = (x1 - x0) + 2 * p; bh = (y1 - y0) + 2 * p

    print(f"[measure_bbox] frames with text: {len(hits)}   peak_density={peak}")
    print(f"[measure_bbox] raw span   x={x0}-{x1}  y={y0}-{y1}  t={t0:.2f}-{t1:.2f}s")
    print(f"[measure_bbox] padded bbox x={bx} y={by} w={bw} h={bh}   (pad={p}px)")
    rmethod = "blur" if bh > 100 else "inpaint"
    print(f"[measure_bbox] height={bh}px -> removal_method=['{rmethod}']")

    if bx <= 1 or by <= 1:
        print("[measure_bbox] WARNING: bbox touches the frame edge -> background colour noise may be "
              "included. Narrow the search band (--y-min/--y-max) or raise --min-pixels / --peak-ratio.")

    if args.json:
        item = {
            "bbox": {"x": bx, "y": by, "w": bw, "h": bh},
            "start_time": round(t0, 2),
            "end_time": round(t1, 2),
            "removal_method": ["blur" if bh > 100 else "inpaint"],
        }
        print(json.dumps(item, ensure_ascii=False, indent=2))

    print("[measure_bbox] NOTE: verify the span visually at BOTH ends (t0-0.3s and t1+0.3s) "
          "before finalizing - under-estimating end_time leaves residual text in the cut.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
