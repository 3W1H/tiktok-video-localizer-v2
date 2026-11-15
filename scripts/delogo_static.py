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


def temporal_response(path: str, box=None, samples: int = 300,
                      ksize: int = 17) -> np.ndarray:
    """逐像素**中值**的 top-hat 响应。固定位置的水印笔画会留下来。

    ⚠️ 2026-11-13 改：原来用「高斯高通后**取平均**」。均值对水印友好，但对
    **高对比背景**（白色球衣、白色场地线）同样友好 —— 那些结构也会在平均后留下
    残影，于是掩膜被扩成一片，inpaint 出来就是一条 350px 的横带（用户口中的「糊块」，
    在「人类扣篮2.0」上被明确投诉过）。

    * top-hat（白顶帽）只保留**比邻域亮、且尺寸小于 ksize** 的结构 —— 手写体笔画
      正好落在这个尺寸带里，而大面积白球衣/地板纹理被滤掉；
    * 中值只容忍「一半以上的帧在这里是笔画」。水印是逐帧 alpha 混合、每帧都在，
      中位数把它们全部保住；而背景里偶尔出现的亮结构（灯光、记分牌闪烁）被丢弃。
    实测同一段片子：均值+高斯 会给出 30k 像素的整片白，中值+top-hat 给出
    10441 像素的干净笔画（约 34% 覆盖率），且掩膜 bbox 不触选区边界。
    """
    cap = cv2.VideoCapture(path)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or samples
    # 顺序读 + 等距跳帧：比「每次 CAP_PROP_POS_FRAMES 定位」快 5~10 倍
    # （定位是 seek，每帧一次；实测 5800 帧的片子 200 次 seek 要 40s，顺序读只要 6s）
    stride = max(1, n // max(1, samples))
    stack = []
    idx = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if idx % stride:
            idx += 1
            continue
        idx += 1
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        if box is not None:
            x0, y0, x1, y1 = box
            g = g[y0:y1, x0:x1]
        th = cv2.morphologyEx(g, cv2.MORPH_TOPHAT,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (ksize, ksize)))
        stack.append(th.astype(np.float32))
    cap.release()
    if not stack:
        raise SystemExit("no frames read")
    return np.median(np.stack(stack), axis=0)


def build_mask(resp: np.ndarray, shape, box, thr, dilate_px: int,
               min_area: int = 10) -> np.ndarray:
    """thr="auto" -> Otsu 自适应；否则用给定阈值（float）。"""
    r = np.clip(resp, 0, 255).astype(np.uint8)
    if isinstance(thr, str) and thr.lower() == "auto":
        t, _ = cv2.threshold(r, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        thr = max(float(t), 6.0)      # 下限：防止暗背景把 Otsu 拉到 3~4
    thr = float(thr)
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
    ap.add_argument("--thr", default="7",
                    help="top-hat 中值响应阈值。默认 7（实测：覆盖率 100%%、掩膜 12601px）；"
                         "给 auto 走 Otsu（更保守，但实测只覆盖 69%% 笔画，会留下残笔）")
    ap.add_argument("--dilate", type=int, default=3, help="safety margin in px")
    ap.add_argument("--radius", type=int, default=4, help="inpaint radius")
    ap.add_argument("--samples", type=int, default=900,
                    help="参与中值的帧数（等距跳帧；越多越稳，900 帧约 20s）")
    ap.add_argument("--ksize", type=int, default=17, help="top-hat kernel（≈笔画粗细 2~3 倍）")
    ap.add_argument("--hunt", action="store_true", help="only save the response map")
    ap.add_argument("--save-map", help="path for the response map png")
    ap.add_argument("--save-mask", help="path to write the generated mask png")
    ap.add_argument("--mask-only", action="store_true",
                    help="只生成/保存掩膜，不渲染视频（先看一眼再决定要不要跑全片）")
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
        resp = temporal_response(a.input, box, a.samples, a.ksize)
        mx = float(resp.max()) or 1.0
        img = np.clip(resp / mx * 255, 0, 255).astype(np.uint8)
        img = cv2.applyColorMap(img, cv2.COLORMAP_INFERNO)
        cv2.imwrite(a.save_map or "hunt.png", img)
        # 同时打印最亮点簇的坐标，省得人眼去数像素
        hot = (resp > max(9.0, resp.max() * 0.35)).astype(np.uint8)
        nl, lab, stats, _ = cv2.connectedComponentsWithStats(hot, 8)
        boxes = sorted(
            ([int(s[0]) + (box[0] if a.bbox else 0), int(s[1]) + (box[1] if a.bbox else 0),
              int(s[2]), int(s[3]), int(s[4])] for s in stats[1:]),
            key=lambda b: -b[4])[:5]
        print(f"response map -> {a.save_map or 'hunt.png'}  (bright = fixed overlay)")
        print("top components [x,y,w,h,area]:", boxes)
        return 0

    if not a.output and not a.mask_only:
        raise SystemExit("--output is required")

    if a.mask:
        mask = cv2.imread(a.mask, 0)
        if mask is None:
            raise SystemExit(f"cannot read mask {a.mask}")
        mask = (mask > 127).astype(np.uint8) * 255
        if mask.shape != (meta["h"], meta["w"]):
            raise SystemExit(f"mask is {mask.shape}, video is {(meta['h'], meta['w'])}")
    else:
        resp = temporal_response(a.input, box, a.samples, a.ksize)
        mask = build_mask(resp, (meta["h"], meta["w"]), box, a.thr, a.dilate)

    if a.save_mask:
        cv2.imwrite(a.save_mask, mask)
        print(f"mask -> {a.save_mask} ({int((mask > 0).sum())} px)")
    if a.mask_only:
        print("--mask-only: 不渲染视频")
        return 0

    n = render(a.input, a.output, mask, meta, a.radius)
    print(f"done: {n} frames -> {a.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
