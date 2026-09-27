#!/usr/bin/env python3
"""
contact_sheet.py — 生成视频「胶片接触印样」，让 Agent 一轮看完整条片。

存在理由（2026-09-22 复盘）：
  找中文跑马灯的正确做法是「抽帧 -> 看图 -> 定位」，但不能一张一张看。
  实测一次 3 视频批次里，为了找 4 处中文，抽了 43 张中间图、触发 43 次视觉推理，
  耗掉 26 分钟零产物推进。

  本脚本把整条片压成一张网格图（默认 6 列 x 8 行 = 48 帧），每格标注时间戳。
  Agent 一次读取即可回答「中文出现在哪几秒」，再针对可疑区间做二次加密采样。
  **一个视频从 ~15 次图片读取压到 1~2 次。**

为什么不直接用 ocr_video.py 的结果干活：
  ~~本 skill 默认 OCR 走 tesseract(chi_sim)，实测对视频里的中文描边/彩色美术字
  召回率接近 0（2026-09-22 实测：4 处中文全部漏检，调 psm/预处理均无效）。~~
  【2026-09-22 已根治】ocr_video.py 现默认走 RapidOCR（onnxruntime），对中文描边
  美术字召回良好（实测 conf 0.99），OCR 结果已可作为「有没有中文」的主要依据。
  但 OCR 每帧都要跑模型，成本远高于抽一张网格图；且 OCR 会漏掉极短暂的字幕闪现。
  因此**分工不变**：先用本脚本一轮概览定位可疑区间，再用 OCR/颜色阈值精确定 bbox。

依赖：ffmpeg/ffprobe（PATH 或 --ffmpeg 指定）、Pillow。不依赖 OCR。

用法：
  # 全片概览
  python contact_sheet.py --video a.mp4 --out sheet.jpg
  # 自定义网格 + 只看某段时间
  python contact_sheet.py --video a.mp4 --out sheet.jpg --cols 6 --rows 8 --start 30 --end 41
  # 二次加密：已知 37-41s 有字幕，放大底部区域再看一遍
  python contact_sheet.py --video a.mp4 --out zoom.jpg --start 37 --end 41 --crop 0,650,720,904
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw


def which_ffmpeg(explicit: str | None = None) -> str:
    for cand in (explicit, shutil.which("ffmpeg"), os.path.expanduser("~/.local/bin/ffmpeg")):
        if cand and os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
    print("[contact_sheet] FATAL: ffmpeg not found. Install it or pass --ffmpeg /path/to/ffmpeg", file=sys.stderr)
    sys.exit(2)


def probe_duration(ffprobe: str, video: str) -> float:
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def main():
    ap = argparse.ArgumentParser(description="Build a timestamped contact sheet from a video")
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", required=True, help="output jpg/png path")
    ap.add_argument("--cols", type=int, default=6)
    ap.add_argument("--rows", type=int, default=8)
    ap.add_argument("--start", type=float, default=None, help="start sec (default 0)")
    ap.add_argument("--end", type=float, default=None, help="end sec (default duration)")
    ap.add_argument("--crop", default=None,
                    help="crop each frame before tiling: x,y,w,h (e.g. 0,650,720,904 for bottom captions)")
    ap.add_argument("--cell-width", type=int, default=240, help="tile width in px (height keeps aspect)")
    ap.add_argument("--ffmpeg", default=None)
    ap.add_argument("--label-height", type=int, default=22)
    ap.add_argument("--jpeg-quality", type=int, default=88)
    args = ap.parse_args()

    video = os.path.expanduser(args.video)
    if not os.path.exists(video):
        print(f"[contact_sheet] FATAL: video not found: {video}", file=sys.stderr)
        return 2

    ffmpeg = which_ffmpeg(args.ffmpeg)
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")
    if not os.path.exists(ffprobe):
        ffprobe = shutil.which("ffprobe") or ffmpeg

    duration = probe_duration(ffprobe, video)
    start = args.start if args.start is not None else 0.0
    end = args.end if args.end is not None else (duration or 0.0)
    if end <= start:
        print(f"[contact_sheet] FATAL: bad range start={start} end={end} (duration={duration})", file=sys.stderr)
        return 2

    nframes = args.cols * args.rows
    span = end - start
    # 取格子中心而非左端点，避免首尾帧落在视频交界外
    stamps = [start + span * (i + 0.5) / nframes for i in range(nframes)]

    tmp = tempfile.mkdtemp(prefix="contact_sheet_")
    tiles = []
    try:
        for i, ts in enumerate(stamps):
            dst = os.path.join(tmp, f"t{i:03d}.png")
            cmd = [ffmpeg, "-v", "error", "-ss", f"{ts:.3f}", "-i", video,
                   "-frames:v", "1", dst, "-y"]
            rc = subprocess.run(cmd, capture_output=True).returncode
            if rc != 0 or not os.path.exists(dst):
                continue
            im = Image.open(dst).convert("RGB")
            if args.crop:
                try:
                    x0, y0, w0, h0 = [int(v) for v in args.crop.split(",")]
                    im = im.crop((x0, y0, x0 + w0, y0 + h0))
                except Exception as e:
                    print(f"[contact_sheet] bad --crop '{args.crop}': {e}", file=sys.stderr)
                    return 2
            tw = args.cell_width
            th = max(1, int(im.height * tw / im.width))
            tiles.append((ts, im.resize((tw, th), Image.LANCZOS)))
    finally:
        pass

    if not tiles:
        print("[contact_sheet] FATAL: no frames extracted", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)
        return 2

    cw = max(t.width for _, t in tiles)
    ch = max(t.height for _, t in tiles)
    lh = args.label_height
    cols = min(args.cols, len(tiles))
    rows = (len(tiles) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * cw, rows * (ch + lh)), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    for idx, (ts, im) in enumerate(tiles):
        r, c = divmod(idx, cols)
        x, y = c * cw, r * (ch + lh)
        canvas.paste(im, (x + (cw - im.width) // 2, y))
        draw.rectangle([x, y + ch, x + cw, y + ch + lh], fill=(16, 16, 16))
        draw.text((x + 5, y + ch + 4), f"{ts:.1f}s", fill=(255, 220, 0))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    canvas.save(args.out, quality=args.jpeg_quality)
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"[contact_sheet] {args.out}  {canvas.size[0]}x{canvas.size[1]}  tiles={len(tiles)}  range={start:.1f}-{end:.1f}s")
    if args.crop:
        print(f"[contact_sheet] cropped to {args.crop}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
