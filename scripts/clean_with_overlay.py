#!/usr/bin/env python3
"""
clean_with_overlay.py - 高级视频清理工具（黑色条覆盖 + 文字叠加 + 第一帧替换）

[DEPRECATED] 正式交付请用 scripts/render_overlays.py（见 SKILL.md §12.5.2）。
本脚本保留，仅供「封面图第一帧中文处理」（--first-frame-blackbar / --trim-start）
与排障/一次性修补；它的黑条覆盖 + drawtext 叠加已被 render_overlays.py 取代。

适用于中文文字区域固定在顶部/底部的视频，用黑色条覆盖比 delogo 更彻底，无波纹闪现。
支持在黑色条上叠加英文标题和 logo 文字，支持替换第一帧（封面图中文处理）。

使用示例：
  # 黑色条覆盖顶部和底部，叠加英文标题和 logo
  python clean_with_overlay.py --video input.mp4 --output output.mp4 \
      --blackbar "top:y=170:h=160" \
      --blackbar "bottom:y=970:h=155" \
      --overlay-text "text='BLAKE GRIFFIN\\nTOP 20 DUNKS':y=195:fontsize=38:color=white" \
      --overlay-text "text='hoopdy nasty daily':y=982:fontsize=22:color=orange:italic"

  # 替换第一帧（用第二帧替换封面图）
  python clean_with_overlay.py --video input.mp4 --output output.mp4 --replace-first-frame
"""

import argparse
import os
import subprocess
import sys
import tempfile
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    run, log, require_tools, EXIT_FATAL,
)


def parse_blackbar(spec: str) -> Dict:
    """解析黑色条参数，格式：name:y=170:h=160[:color=black]"""
    parts = spec.split(":")
    result = {"name": parts[0], "y": 0, "h": 0, "color": "black", "x": 0, "w": "iw"}
    for part in parts[1:]:
        if "=" in part:
            key, val = part.split("=", 1)
            if key in ("y", "h", "x"):
                result[key] = int(val)
            elif key == "w":
                result[key] = val
            elif key == "color":
                result[key] = val
    return result


def parse_overlay(spec: str) -> Dict:
    """解析文字叠加参数，格式：text='TITLE':y=195:fontsize=38:color=white[:italic][:x=(w-text_w)/2]"""
    result = {
        "text": "", "y": 100, "fontsize": 30, "color": "white",
        "x": "(w-text_w)/2", "borderw": 2, "bordercolor": "black",
        "italic": False, "line_spacing": 8,
    }
    # 先提取 text（可能包含冒号，所以特殊处理）
    if spec.startswith("text='"):
        end = spec.find("'", 6)
        if end > 0:
            result["text"] = spec[6:end]
            spec = spec[end + 1:]
    # 解析其余参数
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
            elif key in ("y", "x", "color", "bordercolor", "line_spacing"):
                result[key] = val
    return result


def get_font_path(italic: bool = False) -> str:
    """获取字体路径"""
    if italic:
        candidates = [
            "/Library/Fonts/Arial Italic.ttf",
            "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
            "/Library/Fonts/Georgia Italic.ttf",
        ]
    else:
        candidates = [
            "/Library/Fonts/Arial Black.ttf",
            "/System/Library/Fonts/Supplemental/Arial Black.ttf",
            "/Library/Fonts/Impact.ttf",
            "/Library/Fonts/Arial Bold.ttf",
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return "/Library/Fonts/Arial.ttf"


def build_filter(blackbars: List[Dict], overlays: List[Dict]) -> str:
    """构建 FFmpeg filter 链"""
    filters = []

    # 黑色条覆盖
    for bb in blackbars:
        filters.append(
            f"drawbox=x={bb['x']}:y={bb['y']}:w={bb['w']}:h={bb['h']}:color={bb['color']}:t=fill"
        )

    # 文字叠加
    for ov in overlays:
        font_path = get_font_path(ov.get("italic", False))
        # 写入临时文件避免转义问题
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(ov["text"])
            text_file = f.name
        filters.append(
            f"drawtext=fontfile='{font_path}':textfile='{text_file}':"
            f"fontsize={ov['fontsize']}:fontcolor={ov['color']}:"
            f"borderw={ov['borderw']}:bordercolor={ov['bordercolor']}:"
            f"x={ov['x']}:y={ov['y']}:line_spacing={ov['line_spacing']}"
        )

    return ",".join(filters)


def replace_first_frame(video_path: str, output_path: str, work_dir: str):
    """用第二帧替换第一帧（处理封面图中文），保留音频"""
    frame2 = os.path.join(work_dir, "frame2_cover.jpg")
    intro = os.path.join(work_dir, "intro_cover.mp4")
    rest = os.path.join(work_dir, "rest_cover.mp4")
    concat_file = os.path.join(work_dir, "concat_cover.txt")

    # 1. 提取第二帧（1秒处）
    run(["ffmpeg", "-y", "-ss", "1", "-i", video_path, "-vframes", "1", frame2])

    # 2. 获取原视频音频参数
    sample_rate = "44100"
    channels = "2"
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-of", "default=noprint_wrappers=1:nokey=1",
             "-show_entries", "stream=sample_rate,channels", video_path],
            capture_output=True, text=True
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 1 and lines[0]:
            sample_rate = lines[0]
        if len(lines) >= 2 and lines[1]:
            channels = lines[1]
    except Exception:
        pass

    # 3. 创建带静音音频的 intro（0.04秒），确保 concat 后音频不丢失
    channel_layout = "stereo" if channels == "2" else "mono"
    run(["ffmpeg", "-y", "-loop", "1", "-i", frame2,
         "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={channel_layout}",
         "-c:v", "libx264", "-t", "0.04", "-pix_fmt", "yuv420p", "-r", "25",
         "-c:a", "aac", "-shortest",
         intro])

    # 4. 从原视频第0.04秒开始截取
    run(["ffmpeg", "-y", "-ss", "0.04", "-i", video_path, "-c", "copy", rest])

    # 5. 拼接（重新编码以确保音频同步）
    with open(concat_file, "w") as f:
        f.write(f"file '{intro}'\n")
        f.write(f"file '{rest}'\n")

    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file,
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-c:a", "aac", "-b:a", "128k",
         output_path])

    # 清理临时文件
    for f in [frame2, intro, rest, concat_file]:
        if os.path.exists(f):
            os.remove(f)


def generate_artistic_logo(text: str, output_path: str, font_size: int = 36,
                            color1: str = "#FF6400", color2: str = "#FFC800"):
    """用 Pillow 生成艺术化 logo 图片（渐变+描边+外发光+高光）"""
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except ImportError:
        log.error("Pillow not installed")
        sys.exit(EXIT_FATAL)

    W, H = 600, 100
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 加载字体
    font = None
    for font_name in ["Impact", "Arial Black", "Helvetica Bold", "Arial Bold"]:
        try:
            font = ImageFont.truetype(font_name, font_size)
            break
        except (IOError, OSError):
            continue
    if font is None:
        font = ImageFont.load_default()

    # 计算文字位置
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (W - text_w) // 2
    y = (H - text_h) // 2 - 5

    # 解析颜色
    def hex_to_rgb(hex_color):
        hex_color = hex_color.lstrip("#")
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

    c1 = hex_to_rgb(color1)
    c2 = hex_to_rgb(color2)

    # 1. 外发光效果
    for offset in [8, 6, 4, 2]:
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        glow_draw.text((x, y), text, font=font, fill=(*c1, 60))
        glow = glow.filter(ImageFilter.GaussianBlur(radius=offset))
        img = Image.alpha_composite(img, glow)

    # 2. 黑色粗描边
    draw = ImageDraw.Draw(img)
    for dx in [-3, -2, -1, 0, 1, 2, 3]:
        for dy in [-3, -2, -1, 0, 1, 2, 3]:
            if dx != 0 or dy != 0:
                draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 255))

    # 3. 主文字 - 渐变（下半部分 c2，上半部分 c1）
    draw.text((x, y), text, font=font, fill=(*c2, 255))
    top_half = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    top_draw = ImageDraw.Draw(top_half)
    top_draw.text((x, y), text, font=font, fill=(*c1, 255))
    mask = Image.new("L", (W, H), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rectangle([(0, 0), (W, y + text_h // 2)], fill=255)
    top_half.putalpha(mask)
    img = Image.alpha_composite(img, top_half)

    # 4. 白色高光（顶部细线）
    highlight = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hl_draw = ImageDraw.Draw(highlight)
    hl_draw.text((x, y - 1), text, font=font, fill=(255, 255, 200, 180))
    hl_mask = Image.new("L", (W, H), 0)
    hl_mask_draw = ImageDraw.Draw(hl_mask)
    hl_mask_draw.rectangle([(0, y), (W, y + 4)], fill=255)
    highlight.putalpha(hl_mask)
    img = Image.alpha_composite(img, highlight)

    # 裁剪到实际内容区域
    bbox = img.getbbox()
    if bbox:
        padding = 10
        img = img.crop((max(0, bbox[0] - padding), max(0, bbox[1] - padding),
                         min(W, bbox[2] + padding), min(H, bbox[3] + padding)))

    img.save(output_path, "PNG")
    log.info(f"Artistic logo generated: {output_path} ({img.size})")
    return output_path


def main():
    ap = argparse.ArgumentParser(description="Advanced video cleaner: black bars + text overlay + first frame replacement")
    ap.add_argument("--video", required=True, help="Input video path")
    ap.add_argument("--output", required=True, help="Output video path")
    ap.add_argument("--work-dir", default=".", help="Working directory for temp files")
    ap.add_argument("--blackbar", action="append", default=[],
                    help="Black bar spec: name:y=170:h=160[:color=black]")
    ap.add_argument("--overlay-text", action="append", default=[],
                    help="Overlay text spec: text='TITLE':y=195:fontsize=38:color=white[:italic]")
    ap.add_argument("--replace-first-frame", action="store_true",
                    help="Replace first frame with second frame (for cover images with Chinese text). NOTE: This re-encodes audio and may cause quality loss.")
    ap.add_argument("--first-frame-blackbar", action="append", default=[],
                    help="Black bar ONLY on first frame (n=0), preserves audio perfectly. Format: name:y=260:h=280[:color=black]. Recommended over --replace-first-frame.")
    ap.add_argument("--trim-start", type=float, default=0,
                    help="Trim N seconds from the beginning (for cover frames that last multiple frames). Audio is preserved via -c copy. e.g. --trim-start 0.2")
    args = ap.parse_args()

    print("[DEPRECATED] clean_with_overlay.py 是 legacy 脚本，正式交付请用 "
          "scripts/render_overlays.py（见 SKILL.md 12.5.2）。本脚本功能保留，"
          "仅供封面第一帧中文处理 / 排障 / 一次性修补。", file=sys.stderr)

    require_tools("ffmpeg", "ffprobe")
    os.makedirs(args.work_dir, exist_ok=True)

    # 解析参数
    blackbars = [parse_blackbar(s) for s in args.blackbar]
    first_frame_bars = [parse_blackbar(s) for s in args.first_frame_blackbar]
    overlays = [parse_overlay(s) for s in args.overlay_text]

    if not blackbars and not overlays and not args.replace_first_frame and not first_frame_bars:
        log.error("No action specified. Use --blackbar, --first-frame-blackbar, --overlay-text, or --replace-first-frame")
        sys.exit(1)

    # 如果有黑色条或文字叠加，先处理
    if blackbars or overlays or first_frame_bars:
        vf = build_filter(blackbars, overlays)
        # 添加只在第一帧生效的黑色条
        for bb in first_frame_bars:
            vf += f",drawbox=x={bb['x']}:y={bb['y']}:w={bb['w']}:h={bb['h']}:color={bb['color']}:t=fill:enable='lte(n,0)'"
        temp_output = os.path.join(args.work_dir, "cleaned_temp.mp4")
        log.info(f"Applying {len(blackbars)} black bars, {len(first_frame_bars)} first-frame bars, {len(overlays)} text overlays")
        run(["ffmpeg", "-y", "-i", args.video,
             "-vf", vf,
             "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-c:a", "copy",
             temp_output])
        input_for_next = temp_output
    else:
        input_for_next = args.video

    # 如果需要替换第一帧
    if args.replace_first_frame:
        log.info("Replacing first frame with second frame")
        replace_first_frame(input_for_next, args.output, args.work_dir)
    else:
        # 直接复制/重命名
        if input_for_next != args.output:
            import shutil
            shutil.copy(input_for_next, args.output)

    # 如果需要去掉开头N秒（处理持续多帧的封面图）
    if args.trim_start > 0:
        log.info(f"Trimming {args.trim_start}s from beginning")
        temp_trimmed = os.path.join(args.work_dir, "trimmed_temp.mp4")
        run(["ffmpeg", "-y", "-ss", str(args.trim_start), "-i", args.output,
             "-c", "copy", temp_trimmed])
        import shutil
        shutil.move(temp_trimmed, args.output)

    # 清理临时文件
    temp_output = os.path.join(args.work_dir, "cleaned_temp.mp4")
    if os.path.exists(temp_output):
        os.remove(temp_output)

    log.info(f"Clean complete: {args.output}")


if __name__ == "__main__":
    main()
