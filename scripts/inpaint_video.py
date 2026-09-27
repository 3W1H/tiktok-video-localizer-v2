#!/usr/bin/env python3
"""
inpaint_video.py - 用 OpenCV inpaint 去除视频中的中文文字，然后叠加英文

处理流程：
1. 逐帧读取视频
2. 对指定区域进行 inpaint（用周围像素填充，去除中文）
3. 在 inpaint 后的区域写上英文
4. 写入输出视频（音频直接 copy）

用法：
python inpaint_video.py --video input.mp4 --output output.mp4 \
    --inpaint "top:y=170:h=160" \
    --inpaint "bottom_watermark:y=970:h=55" \
    --inpaint "bottom_subtitle:y=1025:h=100" \
    --overlay "text='BLAKE GRIFFIN\nTOP 20 DUNKS':y=195:fontsize=38:color=white" \
    --overlay "text='Hoop Dynasty Daily':y=982:fontsize=22:color=orange:italic" \
    --trim-start 0.5
"""

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import run, log, require_tools
from clean_video import FFmpegH264Writer, probe_color_args


def parse_region(spec: str) -> dict:
    """解析区域参数：name:y=170:h=160[:x=0][:w=720]"""
    parts = spec.split(":")
    result = {"name": parts[0], "x": 0, "y": 0, "w": None, "h": 0}
    for part in parts[1:]:
        if "=" in part:
            key, val = part.split("=", 1)
            if key in ("x", "y", "h"):
                result[key] = int(val)
            elif key == "w":
                result[key] = int(val)
    return result


def parse_overlay(spec: str) -> dict:
    """解析文字叠加参数：text='TITLE':y=195:fontsize=38:color=white[:italic][:x=(w-text_w)/2]"""
    result = {
        "text": "", "y": 100, "fontsize": 30, "color": "white",
        "x": None, "borderw": 2, "bordercolor": "black",
        "italic": False, "line_spacing": 8,
    }
    if spec.startswith("text='"):
        end = spec.find("'", 6)
        if end > 0:
            result["text"] = spec[6:end].replace("\\n", "\n")
            spec = spec[end + 1:]
    for part in spec.split(":"):
        if not part:
            continue
        if part == "italic":
            result["italic"] = True
        elif "=" in part:
            key, val = part.split("=", 1)
            if key == "fontsize":
                result[key] = int(val)
            elif key == "borderw":
                result[key] = int(val)
            elif key in ("y", "line_spacing"):
                result[key] = int(val)
            elif key in ("x", "color", "bordercolor"):
                result[key] = val
    return result


def get_font_path(italic: bool = False) -> str:
    """获取字体路径"""
    if italic:
        candidates = [
            "/Library/Fonts/Arial Italic.ttf",
            "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
        ]
    else:
        candidates = [
            "/Library/Fonts/Arial Black.ttf",
            "/System/Library/Fonts/Supplemental/Arial Black.ttf",
            "/Library/Fonts/Impact.ttf",
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return "/Library/Fonts/Arial.ttf"


def inpaint_video(video_path: str, output_path: str, regions: list, overlays: list,
                  trim_start: float = 0, inpaint_radius: int = 5,
                  blur_regions: list = None, blur_radius: int = 15, blur_alpha: int = 180):
    """用 OpenCV inpaint 处理视频，支持模糊遮罩区域"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        log.error("OpenCV not installed")
        sys.exit(1)

    if blur_regions is None:
        blur_regions = []

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        log.error(f"Cannot open video: {video_path}")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # 跳过开头的帧
    if trim_start > 0:
        skip_frames = int(trim_start * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, skip_frames)
        log.info(f"Skipping first {skip_frames} frames ({trim_start}s)")

    # 临时视频文件（无音频）—— 直接用 H.264，避免 mp4v 中间产物带来的二次损失
    temp_video = output_path + ".temp.mp4"
    writer = FFmpegH264Writer(temp_video, width, height, fps,
                              color_args=probe_color_args(video_path))

    # 预加载字体
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    fonts = {}
    for ov in overlays:
        key = (ov["fontsize"], ov["italic"])
        if key not in fonts:
            font_path = get_font_path(ov["italic"])
            try:
                fonts[key] = ImageFont.truetype(font_path, ov["fontsize"])
            except (IOError, OSError):
                fonts[key] = ImageFont.load_default()

    frame_idx = 0
    processed = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # 模糊遮罩区域（先模糊，再叠加半透明遮罩）
        if blur_regions:
            frame_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            for br in blur_regions:
                x1 = br["x"]
                y1 = br["y"]
                x2 = min(width, br["x"] + (br["w"] or width))
                y2 = min(height, br["y"] + br["h"])
                # 裁剪区域并模糊
                region = frame_pil.crop((x1, y1, x2, y2))
                region_blurred = region.filter(ImageFilter.GaussianBlur(radius=blur_radius))
                # 叠加半透明黑色遮罩
                overlay = Image.new("RGBA", region_blurred.size, (0, 0, 0, blur_alpha))
                region_blurred_rgba = region_blurred.convert("RGBA")
                region_blended = Image.alpha_composite(region_blurred_rgba, overlay)
                frame_pil.paste(region_blended.convert("RGB"), (x1, y1))
            frame = cv2.cvtColor(np.array(frame_pil), cv2.COLOR_RGB2BGR)

        # 创建 inpaint mask
        mask = np.zeros((height, width), dtype=np.uint8)
        for reg in regions:
            x1 = reg["x"]
            y1 = reg["y"]
            x2 = min(width, reg["x"] + (reg["w"] or width))
            y2 = min(height, reg["y"] + reg["h"])
            mask[y1:y2, x1:x2] = 255

        # inpaint
        if np.any(mask):
            frame = cv2.inpaint(frame, mask, inpaint_radius, cv2.INPAINT_TELEA)

        # 叠加英文文字（用 Pillow）
        if overlays:
            frame_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            draw = ImageDraw.Draw(frame_pil)
            for ov in overlays:
                font = fonts[(ov["fontsize"], ov["italic"])]
                text = ov["text"]
                lines = text.split("\n")
                y = ov["y"]
                for i, line in enumerate(lines):
                    bbox = draw.textbbox((0, 0), line, font=font)
                    text_w = bbox[2] - bbox[0]
                    if ov["x"] is None:
                        x = (width - text_w) // 2
                    else:
                        try:
                            x = int(ov["x"])
                        except (ValueError, TypeError):
                            x = (width - text_w) // 2
                    # 描边
                    for dx in [-ov["borderw"], 0, ov["borderw"]]:
                        for dy in [-ov["borderw"], 0, ov["borderw"]]:
                            if dx != 0 or dy != 0:
                                draw.text((x + dx, y + dy), line, font=font, fill=ov["bordercolor"])
                    # 主文字
                    draw.text((x, y), line, font=font, fill=ov["color"])
                    y += ov["fontsize"] + ov["line_spacing"]
            frame = cv2.cvtColor(np.array(frame_pil), cv2.COLOR_RGB2BGR)

        writer.write(frame)
        processed += 1
        frame_idx += 1

        if frame_idx % 500 == 0:
            log.info(f"  Progress: {frame_idx}/{total_frames} frames")

    cap.release()
    writer.release()

    log.info(f"Inpaint complete: {processed} frames processed")

    # 合并音频（从原视频复制，跳过开头）
    log.info("Merging audio...")
    if trim_start > 0:
        run(["ffmpeg", "-y", "-ss", str(trim_start), "-i", video_path,
             "-i", temp_video, "-c:v", "copy",
             "-c:a", "aac", "-b:a", "128k", "-map", "1:v:0", "-map", "0:a:0",
             "-shortest", output_path])
    else:
        run(["ffmpeg", "-y", "-i", video_path, "-i", temp_video,
             "-c:v", "copy",
             "-c:a", "copy", "-map", "1:v:0", "-map", "0:a:0",
             "-shortest", output_path])

    # 清理临时文件
    if os.path.exists(temp_video):
        os.remove(temp_video)

    log.info(f"Output: {output_path}")


def main():
    ap = argparse.ArgumentParser(description="Inpaint Chinese text from video and overlay English text")
    ap.add_argument("--video", required=True, help="Input video path")
    ap.add_argument("--output", required=True, help="Output video path")
    ap.add_argument("--inpaint", action="append", default=[],
                    help="Region to inpaint: name:y=170:h=160[:x=0][:w=720]")
    ap.add_argument("--overlay", action="append", default=[],
                    help="Text overlay: text='TITLE':y=195:fontsize=38:color=white[:italic]")
    ap.add_argument("--trim-start", type=float, default=0,
                    help="Trim N seconds from beginning (for cover frames)")
    ap.add_argument("--inpaint-radius", type=int, default=5,
                    help="OpenCV inpaint radius (default: 5)")
    ap.add_argument("--blur-region", action="append", default=[],
                    help="Region to blur + semi-transparent overlay (for large text areas). Format: name:y=170:h=160[:x=0][:w=720]")
    ap.add_argument("--blur-radius", type=int, default=15,
                    help="Gaussian blur radius for blur regions (default: 15)")
    ap.add_argument("--blur-alpha", type=int, default=180,
                    help="Semi-transparent overlay alpha (0-255, default: 180)")
    args = ap.parse_args()

    require_tools("ffmpeg", "ffprobe")

    regions = [parse_region(s) for s in args.inpaint]
    overlays = [parse_overlay(s) for s in args.overlay]
    blur_regions = [parse_region(s) for s in args.blur_region]

    if not regions and not overlays and not blur_regions:
        log.error("No action specified. Use --inpaint, --blur-region, or --overlay")
        sys.exit(1)

    inpaint_video(args.video, args.output, regions, overlays,
                  trim_start=args.trim_start, inpaint_radius=args.inpaint_radius,
                  blur_regions=blur_regions, blur_radius=args.blur_radius,
                  blur_alpha=args.blur_alpha)


if __name__ == "__main__":
    main()
