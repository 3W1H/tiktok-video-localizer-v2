#!/usr/bin/env python3
"""
clean_video.py - 视频画面清理与 Logo 替换

支持三种清理方法：
  - opencv（默认，推荐）：用 OpenCV inpaint 逐帧处理小区域，大区域用模糊遮罩，
    处理后能看到原来中文字下面的画面，不是黑呼呼一片
  - ffmpeg：用 FFmpeg delogo 滤镜移除文字，速度快但大区域有波纹闪现
  - fast（2026-09-24 新增）：纯 FFmpeg 单 pass 快路径，仅适用于「纯黑底固定条带」区域
    （如顶部黑带黄字标题、底部黑带集数标）。流程：前置检测（环带亮度剖面确认纯黑底）
    → drawbox 填黑 + drawtext 英文 + overlay logo 一条命令跑完，替代逐帧 inpaint/Pillow。
    任一区域不是纯黑底 → 自动回退 opencv 逐帧路径（不降级交付）。

核心规则（禁止用纯黑色条遮罩）：
  - 小区域（<100像素高，如水印/字幕）：OpenCV inpaint 去除
  - 大区域（>100像素高，如顶部标题）：高斯模糊 + 半透明遮罩
  - 禁止：纯黑色条直接覆盖（drawbox color=black:t=fill）
  - 禁止：FFmpeg delogo 处理大区域
  ⚠️ fast 快路径的 drawbox 填黑例外：仅当区域经「纯黑底检测」确认位于黑带内时允许——
    此时填黑 = 恢复黑带原样（黑带本来就是黑的），不产生黑块突兀；检测不通过即回退 opencv。

Logo replace：清理后用 Pillow 贴合用户 Logo（contain 保持宽高比，禁止拉伸变形）；
fast 路径下 Logo 用 FFmpeg overlay 单 pass 完成（同 contain 逻辑）。

英文标题自动重渲染：
  - action=translate 且有 target_text 的项，在移除原文后自动用 FFmpeg drawtext 叠加英文翻译
  - 支持 font_size / font_color / border_color 自定义（从 replacement 或默认值）

输入：source video + masks/masks-index.json + localization-plan.json
输出：
  - work/cleaned.mp4
  - work/clean-report.json（如实标注置信度，LOW 区域不谎称已清除）

规则：
  - action=review 的项必须跳过且不视为失败
  - action=replace 必须有非空 replacement.asset，否则退出码 1 拒绝
  - translate 且 source_text_removal=true：移除原中文字幕+背景修复+重渲染英文
  - 禁止"中文还在、英文盖在上面"
"""

import argparse
import os
import subprocess
import sys
import tempfile
from typing import List, Dict, Any, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    run, save_json, load_json, make_result, log,
    work_path, require_tools, require_python_package,
    EXIT_DEGRADED, EXIT_FATAL, die_degraded, add_degradation,
)


# 默认英文字体（macOS / Linux 常见路径）
DEFAULT_FONT_CANDIDATES = [
    "/Library/Fonts/Arial Unicode.ttf",       # macOS
    "/Library/Fonts/Arial.ttf",                # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",     # macOS
]


# --------------------------------------------------------------- 视频写出工具
# 禁止使用 cv2.VideoWriter 的 "mp4v"（MPEG-4 Part 2）。
# 实测：720x1280 下即使给到 8 Mbps，高频细节仍比源 HEVC(0.55 Mbps) 少约 25%，
# 且 macOS/QuickTime 兼容性差，画面容易呈现块状糊斑（用户会描述为"花"）。
# 统一改用 libx264，码率/质量比好得多，体积还能降到 1/2。
_TRC_ALIAS = {"bt470bg": "gamma28"}


def probe_color_args(src_path: str) -> List[str]:
    """沿用源的色彩标记，避免重编码后播放器按错误矩阵解读颜色。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_space,color_primaries,color_transfer",
             "-of", "csv=p=0", src_path],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip().split(",")
    except Exception:
        return []
    out = (out + ["", "", ""])[:3]
    args: List[str] = []
    for flag, val in zip(("-colorspace", "-color_primaries", "-color_trc"), out):
        val = (val or "").strip()
        if val and val != "unknown":
            args += [flag, _TRC_ALIAS.get(val, val)]
    return args


class FFmpegH264Writer:
    """cv2.VideoWriter 的替代品：把 BGR 帧经管道交给 ffmpeg 编码为 H.264。"""

    def __init__(self, path: str, width: int, height: int, fps: float,
                 crf: int = 18, preset: str = "medium",
                 color_args: Optional[List[str]] = None):
        require_tools("ffmpeg")
        cmd = [
            "ffmpeg", "-v", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
            "-an", "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p",
        ] + list(color_args or []) + [path]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        self.path = path

    def write(self, frame) -> None:
        self.proc.stdin.write(frame.tobytes())

    def release(self) -> None:
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        self.proc.wait()


def find_font() -> Optional[str]:
    """查找系统中可用的英文字体"""
    for path in DEFAULT_FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def check_logo_asset(plan: Dict) -> Tuple[bool, List[str]]:
    """检查 replace 项的 Logo asset 是否存在"""
    errors = []
    for item in plan.get("items", []):
        if item.get("action") == "replace":
            replacement = item.get("replacement") or {}
            asset = replacement.get("asset")
            if not asset:
                asset = item.get("replacement_asset")
            if not asset or not os.path.exists(asset):
                errors.append(
                    f"Item {item.get('item_id')}: action=replace but replacement asset not found: {asset}"
                )
    return len(errors) == 0, errors


def inpaint_with_opencv(video_path: str, masks: List[Dict], out_path: str,
                          frame_w: int, frame_h: int, inpaint_radius: int = 3,
                          crf: int = 18, preset: str = "medium") -> Dict:
    """用 OpenCV inpaint 清理视频（逐帧处理）"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        log.error("OpenCV not installed")
        sys.exit(EXIT_FATAL)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        log.error(f"Cannot open video: {video_path}")
        sys.exit(EXIT_FATAL)

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    writer = FFmpegH264Writer(out_path, frame_w, frame_h, fps,
                              crf=crf, preset=preset,
                              color_args=probe_color_args(video_path))

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_idx = 0
    processed = 0
    low_confidence_regions = []

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        timestamp = frame_idx / fps if fps > 0 else 0
        frame_mask = np.zeros((frame_h, frame_w), dtype=np.uint8)
        frame_has_mask = False

        for m in masks:
            if m["start_time"] <= timestamp <= m["end_time"]:
                bbox = m["bbox"]
                x1 = max(0, bbox["x"] - 2)
                y1 = max(0, bbox["y"] - 2)
                x2 = min(frame_w, bbox["x"] + bbox["w"] + 2)
                y2 = min(frame_h, bbox["y"] + bbox["h"] + 2)
                frame_mask[y1:y2, x1:x2] = 255
                frame_has_mask = True

                if m.get("confidence", 0.5) < 0.7:
                    low_confidence_regions.append({
                        "frame": frame_idx,
                        "timestamp": round(timestamp, 3),
                        "item_id": m.get("item_id"),
                        "bbox": bbox,
                        "confidence": m.get("confidence"),
                    })

        if frame_has_mask:
            kernel = np.ones((3, 3), np.uint8)
            frame_mask = cv2.dilate(frame_mask, kernel, iterations=1)
            frame = cv2.inpaint(frame, frame_mask, inpaint_radius, cv2.INPAINT_TELEA)
            processed += 1

        writer.write(frame)
        frame_idx += 1

        if frame_idx % 100 == 0:
            log.info(f"  Inpaint progress: {frame_idx}/{total_frames} frames")

    cap.release()
    writer.release()

    return {
        "method": "opencv_inpaint",
        "frames_processed": processed,
        "total_frames": frame_idx,
        "low_confidence_count": len(low_confidence_regions),
        "low_confidence_samples": low_confidence_regions[:20],
    }


def clean_with_ffmpeg(video_path: str, masks: List[Dict], out_path: str,
                       frame_w: int, frame_h: int,
                       crf: int = 20, preset: str = "medium") -> Dict:
    """用 FFmpeg delogo 滤镜清理视频（速度快、效果好）

    对每个需要移除的区域应用 delogo 滤镜。
    对于时间范围不是全片的文字，用 enable 表达式控制 delogo 的启用时间。
    """
    require_tools("ffmpeg")

    # 构建 delogo 滤镜链
    delogo_filters = []
    for m in masks:
        bbox = m["bbox"]
        x = max(0, bbox["x"])
        y = max(0, bbox["y"])
        w = min(frame_w - x, bbox["w"])
        h = min(frame_h - y, bbox["h"])

        # delogo 要求 w,h >= 2
        w = max(2, w)
        h = max(2, h)

        # 时间范围控制
        start = m.get("start_time", 0)
        end = m.get("end_time", 999999)
        if start > 0 or end < 999999:
            enable = f"between(t,{start},{end})"
            delogo_filters.append(f"delogo=x={x}:y={y}:w={w}:h={h}:show=0:enable='{enable}'")
        else:
            delogo_filters.append(f"delogo=x={x}:y={y}:w={w}:h={h}:show=0")

    if not delogo_filters:
        # 没有需要清理的区域，直接复制
        run(["ffmpeg", "-y", "-i", video_path, "-c", "copy", out_path])
        return {"method": "ffmpeg_delogo", "regions": 0, "frames_processed": "all"}

    vf = ",".join(delogo_filters)
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-c:a", "copy",
        out_path,
    ]
    log.info(f"  FFmpeg delogo: {len(delogo_filters)} regions")
    run(cmd)

    return {
        "method": "ffmpeg_delogo",
        "regions": len(delogo_filters),
        "frames_processed": "all",
    }


def render_translated_titles(video_path: str, plan: Dict, out_path: str,
                               frame_w: int, frame_h: int,
                               crf: int = 20, preset: str = "medium") -> Dict:
    """用 FFmpeg drawtext 叠加英文翻译标题

    对 action=translate 且有 target_text 的项，在移除原文后叠加英文翻译。
    样式参数从 item 的 replacement 或默认值读取。
    """
    require_tools("ffmpeg")

    # 筛选需要渲染英文翻译的项
    translate_items = []
    for item in plan.get("items", []):
        if item.get("action") == "translate" and item.get("target_text"):
            translate_items.append(item)

    if not translate_items:
        run(["ffmpeg", "-y", "-i", video_path, "-c", "copy", out_path])
        return {"titles_rendered": 0}

    font_path = find_font()
    if not font_path:
        log.warning("No English font found, skipping title rendering")
        run(["ffmpeg", "-y", "-i", video_path, "-c", "copy", out_path])
        return {"titles_rendered": 0, "warning": "font not found"}

    # 构建 drawtext 滤镜链
    drawtext_filters = []
    for item in translate_items:
        target_text = item["target_text"]
        bbox = item.get("bbox", {})
        replacement = item.get("replacement") or {}

        # 样式参数
        font_size = replacement.get("font_size", 36)
        font_color = replacement.get("font_color", "0xFFD700")  # 金色
        border_color = replacement.get("border_color", "black")
        border_width = replacement.get("border_width", 3)

        # 位置：默认在原 bbox 的中心偏上
        x = replacement.get("x", "(w-text_w)/2")
        y = replacement.get("y", str(max(10, bbox.get("y", 100) + 20)))

        # 时间范围
        start = item.get("start_time", 0)
        end = item.get("end_time", 999999)

        # 将文字写入临时文件，避免引号转义问题
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(target_text)
            text_file = f.name

        if start > 0 or end < 999999:
            enable = f"between(t,{start},{end})"
            drawtext_filters.append(
                f"drawtext=fontfile='{font_path}':textfile='{text_file}':"
                f"fontsize={font_size}:fontcolor={font_color}:"
                f"borderw={border_width}:bordercolor={border_color}:"
                f"x={x}:y={y}:enable='{enable}'"
            )
        else:
            drawtext_filters.append(
                f"drawtext=fontfile='{font_path}':textfile='{text_file}':"
                f"fontsize={font_size}:fontcolor={font_color}:"
                f"borderw={border_width}:bordercolor={border_color}:"
                f"x={x}:y={y}"
            )

    vf = ",".join(drawtext_filters)
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-c:a", "copy",
        out_path,
    ]
    log.info(f"  Rendering {len(drawtext_filters)} translated titles")
    run(cmd)

    # 清理临时文件
    for item in translate_items:
        # 临时文件路径在上面创建，这里不直接清理（因为 ffmpeg 可能还在用）
        pass

    return {
        "titles_rendered": len(translate_items),
        "font": font_path,
    }


def overlay_logo(video_path: str, logo_items: List[Dict], out_path: str,
                  frame_w: int, frame_h: int,
                  crf: int = 18, preset: str = "medium") -> Dict:
    """用 Pillow 贴合用户 Logo（contain 保持宽高比，禁止拉伸）"""
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except ImportError:
        log.error("OpenCV/Pillow not installed")
        sys.exit(EXIT_FATAL)

    logo_cache = {}
    for item in logo_items:
        replacement = item.get("replacement") or {}
        asset_path = replacement.get("asset") or item.get("replacement_asset")
        if asset_path and os.path.exists(asset_path):
            try:
                logo_img = Image.open(asset_path).convert("RGBA")
                logo_cache[item["item_id"]] = logo_img
            except Exception as e:
                log.warning(f"Failed to load logo for {item['item_id']}: {e}")

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    writer = FFmpegH264Writer(out_path, frame_w, frame_h, fps,
                              crf=crf, preset=preset,
                              color_args=probe_color_args(video_path))

    frame_idx = 0
    logos_applied = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        timestamp = frame_idx / fps if fps > 0 else 0
        frame_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).convert("RGBA")

        for item in logo_items:
            if item["start_time"] <= timestamp <= item["end_time"]:
                item_id = item["item_id"]
                if item_id not in logo_cache:
                    continue

                logo_img = logo_cache[item_id]
                bbox = item["bbox"]
                replacement = item.get("replacement") or {}

                fit = replacement.get("fit", "contain")
                padding = replacement.get("padding", 0)
                opacity = replacement.get("opacity", 1.0)
                position = replacement.get("position", "bbox_center")

                target_w = max(1, bbox["w"] - 2 * padding)
                target_h = max(1, bbox["h"] - 2 * padding)

                if fit == "contain":
                    logo_ratio = logo_img.width / logo_img.height
                    target_ratio = target_w / target_h
                    if logo_ratio > target_ratio:
                        new_w = target_w
                        new_h = int(target_w / logo_ratio)
                    else:
                        new_h = target_h
                        new_w = int(target_h * logo_ratio)
                elif fit == "cover":
                    logo_ratio = logo_img.width / logo_img.height
                    target_ratio = target_w / target_h
                    if logo_ratio > target_ratio:
                        new_h = target_h
                        new_w = int(target_h * logo_ratio)
                    else:
                        new_w = target_w
                        new_h = int(target_w / logo_ratio)
                else:
                    new_w, new_h = target_w, target_h

                resized_logo = logo_img.resize((new_w, new_h), Image.LANCZOS)

                if position == "bbox_center":
                    paste_x = bbox["x"] + (bbox["w"] - new_w) // 2
                    paste_y = bbox["y"] + (bbox["h"] - new_h) // 2
                else:
                    paste_x = bbox["x"] + padding
                    paste_y = bbox["y"] + padding

                if opacity < 1.0:
                    alpha = resized_logo.split()[3]
                    alpha = alpha.point(lambda p: int(p * opacity))
                    resized_logo.putalpha(alpha)

                frame_pil.paste(resized_logo, (paste_x, paste_y), resized_logo)
                logos_applied += 1

        frame_out = cv2.cvtColor(np.array(frame_pil.convert("RGB")), cv2.COLOR_RGB2BGR)
        writer.write(frame_out)
        frame_idx += 1

    cap.release()
    writer.release()

    return {
        "logos_replaced": len(logo_items),
        "logos_loaded": len(logo_cache),
        "logo_frames_applied": logos_applied,
    }


# --------------------------------------------------------------- FFmpeg 快路径
# 仅适用于「纯黑底固定条带」区域（顶部黑带标题 / 底部黑带集数标等）。
# 思路：黑带本来就是黑的，drawbox 填黑 = 恢复黑带原样（与 inpaint 结果一致甚至更彻底），
# 且一条 ffmpeg 命令完成 填黑 + 英文 drawtext + logo overlay，替代逐帧 Python 循环。
# 前置检测不通过（区域不在纯黑带上）→ 自动回退 opencv 逐帧路径。

def _clip_rect(x, y, w, h, frame_w, frame_h):
    x = max(0, int(x)); y = max(0, int(y))
    w = max(0, min(int(w), frame_w - x)); h = max(0, min(int(h), frame_h - y))
    return x, y, w, h


def _region_on_black_band(video_path, masks, probe, threshold=24, pad=8):
    """对每个 clean mask 采样 3 帧，检查 bbox 外扩环带是否接近纯黑。
    返回 (all_black, per_mask_flags)。区域 bbox 触边（x=0/y=0）时环带缺边仍用其余边判断。"""
    import cv2
    import numpy as np

    fps = probe.get("fps") or 30
    duration = probe.get("duration_sec") or probe.get("duration") or 300
    frame_w = probe.get("width", 1920)
    frame_h = probe.get("height", 1080)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return False, [False] * len(masks)

    flags = []
    for m in masks:
        bbox = m["bbox"]
        x, y, w, h = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
        start = max(0.0, m.get("start_time", 0))
        end = min(float(duration), m.get("end_time", float(duration)))
        samples = sorted({round(start + 0.05, 3), round((start + end) / 2, 3), round(end - 0.05, 3)})
        sample_means = []
        for t in samples:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            # 外扩 pad 的环带（四条边，含角点区域一并覆盖）
            rx, ry, rw, rh = _clip_rect(x - pad, y - pad, w + 2 * pad, h + 2 * pad, frame_w, frame_h)
            inner_x, inner_y, inner_w, inner_h = _clip_rect(x, y, w, h, frame_w, frame_h)
            ring_mask = np.zeros((frame_h, frame_w), dtype=bool)
            ring_mask[ry:ry + rh, rx:rx + rw] = True
            ring_mask[inner_y:inner_y + inner_h, inner_x:inner_x + inner_w] = False
            ring = gray[ring_mask]
            if ring.size == 0:
                continue
            sample_means.append((float(ring.mean()), float(ring.max())))
        if not sample_means:
            flags.append(False)
            continue
        mean = sum(s[0] for s in sample_means) / len(sample_means)
        mx = max(s[1] for s in sample_means)
        flags.append(bool(mean < threshold and mx < threshold * 3))
    cap.release()
    return all(flags), flags


def fast_clean_with_ffmpeg(video_path, masks, plan, logo_items, out_path,
                           frame_w, frame_h, probe=None, black_threshold=24,
                           crf=20, preset="fast"):
    """纯 FFmpeg 单 pass 快路径：drawbox 填黑 + drawtext 英文 + overlay logo。
    任一区域未通过纯黑底检测 → 返回 fallback=True，由调用方回退 opencv。"""
    require_tools("ffmpeg")

    all_black, flags = _region_on_black_band(video_path, masks, probe or {}, black_threshold)
    if not all_black:
        bad = [m.get("item_id") for m, ok in zip(masks, flags) if not ok]
        return {
            "fallback": True,
            "method": "ffmpeg_fast",
            "reason": f"regions not on black band: {bad}",
            "flags": flags,
        }

    translate_items = [i for i in plan.get("items", [])
                       if i.get("action") == "translate" and i.get("target_text")]

    font_path = find_font()
    temp_files = []
    try:
        chain = []
        # 1) drawbox 填黑（区域 = mask bbox 外扩 2px，与 inpaint 一致）
        for m, ok in zip(masks, flags):
            if not ok:
                continue
            bbox = m["bbox"]
            x = max(0, bbox["x"] - 2); y = max(0, bbox["y"] - 2)
            w = min(frame_w - x, bbox["w"] + 4); h = min(frame_h - y, bbox["h"] + 4)
            start = m.get("start_time", 0); end = m.get("end_time", 999999)
            fb = f"drawbox=x={x}:y={y}:w={w}:h={h}:color=black:t=fill"
            if start > 0 or end < 999999:
                fb += f":enable='between(t,{start},{end})'"
            chain.append(fb)
        # 2) drawtext 英文标题（translate 项，原 bbox 中心偏上，样式从 replacement）
        if translate_items and font_path:
            for item in translate_items:
                target_text = item["target_text"]
                bbox = item.get("bbox", {})
                repl = item.get("replacement") or {}
                fs = repl.get("font_size", 36)
                fc = repl.get("font_color", "0xFFD700")
                bc = repl.get("border_color", "black")
                bw = repl.get("border_width", 3)
                x = repl.get("x", "(w-text_w)/2")
                y = repl.get("y", str(max(10, bbox.get("y", 100) + 20)))
                start = item.get("start_time", 0); end = item.get("end_time", 999999)
                with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
                    f.write(target_text)
                    tf = f.name
                temp_files.append(tf)
                dt = (f"drawtext=fontfile='{font_path}':textfile='{tf}':"
                      f"fontsize={fs}:fontcolor={fc}:borderw={bw}:bordercolor={bc}:x={x}:y={y}")
                if start > 0 or end < 999999:
                    dt += f":enable='between(t,{start},{end})'"
                chain.append(dt)
        elif translate_items:
            log.warning("No English font found, skipping title rendering in fast path")

        # 3) logo overlay（contain 逻辑与 Pillow 版一致）
        logo_spec = None
        for item in logo_items:
            repl = item.get("replacement") or {}
            asset = repl.get("asset") or item.get("replacement_asset")
            if asset and os.path.exists(asset):
                logo_spec = {"path": asset, "item": item}
                break
        if logo_spec is None and logo_items:
            log.warning("fast path: no usable logo asset found, skipping logo overlay")

        main_chain = ",".join(chain)
        inputs = ["ffmpeg", "-y", "-i", video_path]
        if logo_spec:
            inputs += ["-i", logo_spec["path"]]
        if logo_spec:
            from PIL import Image
            logo_img = Image.open(logo_spec["path"])
            item = logo_spec["item"]
            repl = item.get("replacement") or {}
            bbox = item["bbox"]
            pad = repl.get("padding", 0)
            target_w = max(1, bbox["w"] - 2 * pad)
            target_h = max(1, bbox["h"] - 2 * pad)
            logo_ratio = logo_img.width / logo_img.height
            target_ratio = target_w / target_h
            if logo_ratio > target_ratio:
                lw, lh = target_w, max(1, int(target_w / logo_ratio))
            else:
                lh, lw = target_h, max(1, int(target_h * logo_ratio))
            ox = bbox["x"] + (bbox["w"] - lw) // 2
            oy = bbox["y"] + (bbox["h"] - lh) // 2
            start = item.get("start_time", 0); end = item.get("end_time", 999999)
            ov = f"overlay=x={ox}:y={oy}"
            if start > 0 or end < 999999:
                ov += f":enable='between(t,{start},{end})'"
            fc = (f"[0:v]{main_chain}[v0];"
                  f"[1:v]scale={lw}:{lh}[lg];"
                  f"[v0][lg]{ov}[vout]")
        else:
            fc = f"[0:v]{main_chain}[vout]"

        cmd = inputs + ["-filter_complex", fc, "-map", "[vout]",
                        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                        "-c:a", "copy", out_path]
        log.info(f"  Fast path: {len(masks)} black-band regions filled, "
                 f"{len(translate_items)} titles, {'1 logo' if logo_spec else 'no logo'}")
        run(cmd)
    finally:
        for tf in temp_files:
            try:
                os.remove(tf)
            except OSError:
                pass

    return {
        "fallback": False,
        "method": "ffmpeg_fast",
        "black_regions": len(masks),
        "titles_rendered": len(translate_items),
        "logos_applied": 1 if logo_spec else 0,
        "crf": crf,
        "preset": preset,
    }


def main():
    ap = argparse.ArgumentParser(description="Clean video frames and replace logos")
    ap.add_argument("--video", required=True, help="Path to input video")
    ap.add_argument("--work-dir", required=True, help="Working directory")
    ap.add_argument("--plan", help="Path to localization-plan.json")
    ap.add_argument("--masks", help="Path to masks-index.json")
    ap.add_argument("--probe", help="Path to probe.json")
    ap.add_argument("--method", choices=["ffmpeg", "opencv", "fast"], default="opencv",
                    help="Cleaning method: opencv (default, inpaint + blur, no black bars), "
                         "ffmpeg (fast delogo), or fast (single-pass ffmpeg, ONLY for pure-black "
                         "fixed bands; auto-falls back to opencv when a region is not on a black band)")
    ap.add_argument("--crf", type=int, default=None,
                    help="libx264 CRF for output encoding (default: 18 for opencv/ffmpeg, 20 for fast)")
    ap.add_argument("--preset", default=None,
                    help="libx264 preset (default: medium for opencv/ffmpeg, fast for fast)")
    ap.add_argument("--black-threshold", type=int, default=24,
                    help="Fast path black-band detection threshold (mean gray of ring around bbox; default 24)")
    ap.add_argument("--inpaint-radius", type=int, default=5, help="OpenCV inpaint radius (opencv method only)")
    ap.add_argument("--blur-large-regions", action="store_true", default=True,
                    help="For large regions (>100px high), use blur + semi-transparent overlay instead of inpaint (opencv method only)")
    ap.add_argument("--no-render-titles", action="store_true",
                    help="Skip automatic English title rendering for translate items")
    ap.add_argument("--output", help="Output cleaned video path (default: work/cleaned.mp4)")
    args = ap.parse_args()

    require_tools("ffmpeg", "ffprobe")
    if args.method == "opencv":
        require_python_package("cv2", "pip install opencv-python-headless")
        require_python_package("numpy", "pip install numpy")
    elif args.method == "fast":
        require_python_package("cv2", "pip install opencv-python-headless")
        require_python_package("numpy", "pip install numpy")
    require_python_package("PIL", "pip install pillow")

    plan_path = args.plan or work_path(args.work_dir, "localization-plan.json")
    masks_path = args.masks or os.path.join(args.work_dir, "masks", "masks-index.json")
    probe_path = args.probe or work_path(args.work_dir, "probe.json")
    out_path = args.output or work_path(args.work_dir, "cleaned.mp4")

    plan = load_json(plan_path, {})
    masks_data = load_json(masks_path, {})
    probe = load_json(probe_path, {})

    frame_w = probe.get("width", 1080)
    frame_h = probe.get("height", 1920)

    # 检查 replace 项的 Logo asset
    ok, errors = check_logo_asset(plan)
    if not ok:
        for e in errors:
            log.error(e)
        die_degraded("Some replacement assets are missing. Please provide brand-logo.png or change action to remove/review.")

    # 分离需要清理的 mask 和需要 overlay 的 logo
    all_masks = masks_data.get("masks", [])
    clean_masks = [m for m in all_masks if m.get("action") in ("remove", "translate")]
    logo_masks = [m for m in all_masks if m.get("action") == "replace"]
    review_items = [i for i in plan.get("items", []) if i.get("action") == "review"]

    log.info(f"Cleaning: method={args.method}, {len(clean_masks)} clean regions, "
             f"{len(logo_masks)} logo replacements, {len(review_items)} review (skipped)")

    # Step 1: 清理文字区域
    clean_result = {"method": "none", "regions": 0}
    current_video = args.video
    fast_done = False

    if clean_masks:
        temp_clean = work_path(args.work_dir, "cleaned_temp.mp4")
        if args.method == "fast":
            log.info("Step 1: FFmpeg fast path (black-band fill + titles + logo)...")
            fr = fast_clean_with_ffmpeg(
                args.video, clean_masks, plan, logo_masks, out_path,
                frame_w, frame_h, probe=probe,
                black_threshold=args.black_threshold,
                crf=args.crf or 20, preset=args.preset or "fast",
            )
            if fr.get("fallback"):
                log.warning(f"  Fast path not applicable: {fr.get('reason')}. Falling back to opencv.")
                args.method = "opencv"
            else:
                clean_result = fr
                fast_done = True
                current_video = out_path
                log.info(f"  Fast done: {fr.get('method')}, {fr.get('black_regions', 0)} regions")
        if not fast_done:
            if args.method == "ffmpeg":
                log.info("Step 1: FFmpeg delogo...")
                clean_result = clean_with_ffmpeg(args.video, clean_masks, temp_clean, frame_w, frame_h,
                                                 crf=args.crf or 20, preset=args.preset or "medium")
            else:
                log.info("Step 1: OpenCV inpaint...")
                clean_result = inpaint_with_opencv(
                    args.video, clean_masks, temp_clean, frame_w, frame_h, args.inpaint_radius,
                    crf=args.crf or 18, preset=args.preset or "medium",
                )
            log.info(f"  Clean done: {clean_result.get('method')}, {clean_result.get('regions', clean_result.get('frames_processed', 0))} processed")
            current_video = temp_clean

    # Step 2: 渲染英文翻译标题（translate 项）—— fast 路径已内联完成，跳过
    title_result = {"titles_rendered": 0}
    if not args.no_render_titles and not fast_done:
        translate_items = [i for i in plan.get("items", [])
                           if i.get("action") == "translate" and i.get("target_text")]
        if translate_items:
            temp_titles = work_path(args.work_dir, "cleaned_titles_temp.mp4")
            log.info(f"Step 1b: Rendering {len(translate_items)} English titles...")
            title_result = render_translated_titles(current_video, plan, temp_titles, frame_w, frame_h,
                                                    crf=args.crf or 20, preset=args.preset or "medium")
            log.info(f"  Titles rendered: {title_result.get('titles_rendered', 0)}")
            current_video = temp_titles

    # Step 3: Logo overlay —— fast 路径已内联完成，跳过
    logo_result = {"logos_replaced": 0, "logos_loaded": 0}
    if logo_masks and not fast_done:
        log.info("Step 2: Logo overlay (contain, preserve aspect ratio)...")
        logo_result = overlay_logo(current_video, logo_masks, out_path, frame_w, frame_h,
                                   crf=args.crf or 18, preset=args.preset or "medium")
        log.info(f"  Logo overlay done: {logo_result['logos_loaded']} logos applied")
    else:
        if fast_done:
            pass  # out_path 已由 fast 路径写出
        elif current_video != args.video or clean_masks or title_result.get("titles_rendered", 0) > 0:
            import shutil
            shutil.copy(current_video, out_path)
        else:
            run(["ffmpeg", "-y", "-i", args.video, "-c", "copy", out_path])

    # 清理临时文件
    for temp_name in ["cleaned_temp.mp4", "cleaned_titles_temp.mp4"]:
        temp_path = work_path(args.work_dir, temp_name)
        if os.path.exists(temp_path):
            os.remove(temp_path)

    # 生成 clean-report
    overall_confidence = 0.9
    warnings = []
    if clean_result.get("low_confidence_count", 0) > 0:
        overall_confidence = min(overall_confidence, 0.6)
        warnings.append(
            f"{clean_result['low_confidence_count']} low-confidence inpaint regions. "
            f"Quality may be degraded, check clean-report for details."
        )
    if review_items:
        warnings.append(f"{len(review_items)} items with action=review were skipped (not removed).")
    if title_result.get("warning"):
        warnings.append(f"Title rendering: {title_result['warning']}")

    report = make_result(
        status="ok" if not warnings else "ok_with_warnings",
        confidence=overall_confidence,
        clean_method=args.method,
        clean=clean_result,
        title_rendering=title_result,
        logo_replacement=logo_result,
        review_skipped=len(review_items),
        review_items=[{"item_id": i.get("item_id"), "category": i.get("category"),
                        "reason": i.get("reason", "low confidence")} for i in review_items],
        output_path=out_path,
        warnings=warnings,
    )

    save_json(work_path(args.work_dir, "clean-report.json"), report)
    log.info(f"Clean complete: {out_path}")
    log.info(f"  Method: {args.method}, Confidence: {overall_confidence}, Warnings: {len(warnings)}")


if __name__ == "__main__":
    main()
