#!/usr/bin/env python3
"""
locate_text.py - 文字区域 bbox 精确定位工具

用 FFmpeg drawbox 在视频帧上画半透明彩色框，帮助确认 bbox 坐标是否准确覆盖目标文字区域。

使用场景：
  - OCR 检测到的文字区域 bbox 不准确时
  - 手动识别文字区域需要精确定位时
  - delogo/inpaint 效果不好，需要调整 bbox 时

用法：
  # 在第30秒帧上画一个红色半透明框，测试坐标
  python locate_text.py --video input.mp4 --timestamp 30 --x 270 --y 700 --w 240 --h 45

  # 输出到指定路径
  python locate_text.py --video input.mp4 --timestamp 30 --x 270 --y 700 --w 240 --h 45 --output test-bbox.jpg

  # 用不同颜色（red/green/blue/yellow）
  python locate_text.py --video input.mp4 --timestamp 30 --x 270 --y 700 --w 240 --h 45 --color green

  # 批量测试多个 bbox（从 JSON 文件读取）
  python locate_text.py --video input.mp4 --timestamp 30 --bboxes bboxes.json
"""

import argparse
import json
import os
import sys
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import run, log, work_path, require_tools


# 预定义颜色
COLORS = {
    "red": "red@0.5",
    "green": "green@0.5",
    "blue": "blue@0.5",
    "yellow": "yellow@0.5",
    "cyan": "cyan@0.5",
    "magenta": "magenta@0.5",
    "white": "white@0.5",
}


def draw_bbox_on_frame(video_path: str, timestamp: float, bboxes: List[Dict],
                        output_path: str, color: str = "red") -> bool:
    """在视频指定时间点的帧上画半透明框

    Args:
        video_path: 输入视频路径
        timestamp: 时间点（秒）
        bboxes: bbox 列表，每个包含 x, y, w, h, 可选 color, label
        output_path: 输出图片路径
        color: 默认颜色

    Returns:
        True if successful
    """
    require_tools("ffmpeg")

    # 构建 drawbox 滤镜链
    drawbox_filters = []
    for i, bbox in enumerate(bboxes):
        x = bbox.get("x", 0)
        y = bbox.get("y", 0)
        w = bbox.get("w", 100)
        h = bbox.get("h", 100)
        box_color = bbox.get("color", COLORS.get(color, COLORS["red"]))
        label = bbox.get("label", f"bbox_{i}")

        # drawbox: t=fill 填充，t=数字 表示边框厚度
        drawbox_filters.append(
            f"drawbox=x={x}:y={y}:w={w}:h={h}:color={box_color}:t=fill"
        )

        # 在框上方画标签文字
        label_y = max(10, y - 25)
        drawbox_filters.append(
            f"drawtext=text='{label}':fontsize=20:fontcolor=white:"
            f"borderw=2:bordercolor=black:x={x}:y={label_y}"
        )

    vf = ",".join(drawbox_filters)

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(timestamp),
        "-i", video_path,
        "-vf", vf,
        "-vframes", "1",
        "-q:v", "2",
        output_path,
    ]

    log.info(f"Drawing {len(bboxes)} bbox(es) at t={timestamp}s")
    run(cmd)
    return os.path.exists(output_path)


def main():
    ap = argparse.ArgumentParser(description="Text region bbox localization tool")
    ap.add_argument("--video", required=True, help="Path to input video")
    ap.add_argument("--timestamp", type=float, default=1.0,
                    help="Timestamp in seconds to extract frame (default: 1.0)")
    ap.add_argument("--x", type=int, help="BBox x coordinate")
    ap.add_argument("--y", type=int, help="BBox y coordinate")
    ap.add_argument("--w", type=int, help="BBox width")
    ap.add_argument("--h", type=int, help="BBox height")
    ap.add_argument("--bboxes", help="Path to JSON file containing list of bboxes")
    ap.add_argument("--color", choices=list(COLORS.keys()), default="red",
                    help="Box color (default: red)")
    ap.add_argument("--label", help="Label for the bbox")
    ap.add_argument("--output", help="Output image path (default: work/bbox-test.jpg)")
    ap.add_argument("--work-dir", help="Working directory (for default output path)")
    args = ap.parse_args()

    require_tools("ffmpeg")

    # 构建 bbox 列表
    bboxes = []

    if args.bboxes:
        with open(args.bboxes, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            bboxes = data
        elif isinstance(data, dict) and "bboxes" in data:
            bboxes = data["bboxes"]
        else:
            bboxes = [data]

    if args.x is not None and args.y is not None and args.w is not None and args.h is not None:
        bbox = {
            "x": args.x,
            "y": args.y,
            "w": args.w,
            "h": args.h,
        }
        if args.label:
            bbox["label"] = args.label
        bboxes.append(bbox)

    if not bboxes:
        ap.error("Must provide --x/--y/--w/--h or --bboxes")

    # 输出路径
    if args.output:
        output_path = args.output
    elif args.work_dir:
        output_path = work_path(args.work_dir, "bbox-test.jpg")
    else:
        output_path = "bbox-test.jpg"

    # 执行
    success = draw_bbox_on_frame(args.video, args.timestamp, bboxes, output_path, args.color)

    if success:
        print(f"\n✓ BBox test image generated: {output_path}")
        print(f"  Timestamp: {args.timestamp}s")
        print(f"  BBoxes: {len(bboxes)}")
        for i, bbox in enumerate(bboxes):
            label = bbox.get("label", f"bbox_{i}")
            print(f"    [{label}] x={bbox['x']}, y={bbox['y']}, w={bbox['w']}, h={bbox['h']}")
        print(f"\n  查看图片确认框是否覆盖目标文字区域。")
        print(f"  如果位置不准确，调整 --x/--y/--w/--h 后重新运行。")
    else:
        print(f"\n✗ Failed to generate bbox test image")
        sys.exit(1)


if __name__ == "__main__":
    main()
