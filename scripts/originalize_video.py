#!/usr/bin/env python3
"""
originalize_video.py - 视频原创化处理脚本

用于突破 TikTok "非原创内容" 判定，通过多项创意改编让算法判定为原创。

原创化处理要素：
  1. 画面边缘裁剪 8%（改变构图，去边缘水印）
  2. 正方形转竖版（720x720 → 720x1280，模糊背景填充）
  3. Ken Burns 缓慢放大（1.0 → 1.08，破坏画面指纹）
  4. 精彩瞬间慢动作 0.6x（增加戏剧效果）
  5. 动态英文字幕（淡入淡出，在精彩瞬间弹出）
  6. 重新编码（libx264 crf20，改变元数据）
  7. 替换 BGM（音频也做改编）

⚠️ 片头标题卡已默认停用（2026-10-25）：
  封面 cover.jpg 已经承载了标题、内容简介和品牌 logo，视频第 0 帧再放一张
  同样内容的标题卡属于重复信息，白占 1.5 秒、拉低完播率，而且会让
  "封面 vs 首帧" 看起来像同一张图被塞了两遍。
  **成片第 0 帧必须是正片画面**，打开即进入主题。
  标题卡能力保留在 `--title-card` 后面（默认关闭），只在用户明确要求时启用；
  启用时仍遵守"禁止纯黑背景"的历史规范（视频帧模糊 + 轻暗化）。
"""

import argparse
import os
import sys
import subprocess
import json
from typing import Dict, Any, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    save_json, load_json, make_result, log,
    work_path, ensure_dir, require_tools,
)

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# 标题卡规范（LEGACY —— 默认不生成，仅 --title-card 时启用）
# 停用原因：封面已承载标题与品牌，首帧再放标题卡是重复信息（见模块 docstring）
TITLE_CARD_BRIGHTNESS = -0.15  # 暗化程度，不能太高（-0.35 会看起来像黑色）
TITLE_CARD_CONTRAST = 0.9
TITLE_CARD_BLUR = "15:2"  # 模糊程度
TITLE_CARD_DURATION = 1.5  # 标题卡时长（秒）
TITLE_CARD_FONT = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
TITLE_CARD_FONT_SIZE = 96
DYNAMIC_SUBTITLE_SIZE = 72


def fit_font_size(lines, frame_w: int, base_size: int,
                  ratio: float = 0.62, margin: float = 0.92) -> int:
    """按画面宽度自适应字号。

    Arial Black 大写平均字宽约 0.62em。方形/窄画幅（如 720x720）如果沿用竖屏
    的字号，文字会横向溢出被裁掉，所以按最长的行折算一个上限。
    """
    longest = max((len(l) for l in lines), default=1) or 1
    fit = int(frame_w * margin / (longest * ratio))
    return max(20, min(base_size, fit))
TITLE_CARD_SUBTITLE_SIZE = 40
TITLE_CARD_COLOR = "yellow"
TITLE_CARD_SUBTITLE_COLOR = "white"

# 原创化默认参数
DEFAULT_CROP_RATIO = 0.08  # 边缘裁剪 8%
DEFAULT_ZOOM_MAX = 1.08  # Ken Burns 最大放大倍数
DEFAULT_SLOWMO_SPEED = 0.6  # 慢动作速度
DEFAULT_VERTICAL_WIDTH = 720
DEFAULT_VERTICAL_HEIGHT = 1280


def run_cmd(cmd: List[str], description: str = "") -> int:
    """运行命令，返回退出码"""
    if description:
        log.info(f"▶ {description}")
    log.debug(f"  CMD: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    log.info(f"  {line.strip()}")
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                if line.strip() and not line.startswith("["):
                    log.debug(f"  {line.strip()}")
        if result.returncode != 0:
            log.warning(f"  Command exited with code {result.returncode}")
        return result.returncode
    except Exception as e:
        log.error(f"  Failed to run command: {e}")
        return 2


def probe_video(video: str) -> Dict:
    """探测视频信息"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=width,height,r_frame_rate,codec_type",
        "-of", "json", video,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    info = {"duration": float(data.get("format", {}).get("duration", 0))}
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            info["width"] = int(stream.get("width", 0))
            info["height"] = int(stream.get("height", 0))
            fps_str = stream.get("r_frame_rate", "30/1")
            num, den = fps_str.split("/")
            info["fps"] = int(num) / int(den) if int(den) > 0 else 30
    return info


def step_crop_and_vertical(video: str, output: str, work_dir: str,
                            crop_ratio: float, to_vertical: bool,
                            target_w: int, target_h: int) -> str:
    """步骤1：边缘裁剪 + 正方形转竖版"""
    log.info("=" * 60)
    log.info("Step 1: Crop edges + convert to vertical")
    log.info("=" * 60)

    probe = probe_video(video)
    w, h = probe["width"], probe["height"]
    is_square = (w == h)

    crop_w = int(w * (1 - crop_ratio))
    crop_h = int(h * (1 - crop_ratio))
    crop_x = (w - crop_w) // 2
    crop_y = (h - crop_h) // 2

    if to_vertical and is_square:
        # 正方形转竖版：模糊背景填充
        fg_scale = min(target_w, target_h)
        filter_complex = (
            f"[0:v]crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
            f"scale={fg_scale}:{fg_scale}[fg];"
            f"[0:v]crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
            f"scale={target_w}:{target_h},boxblur=30:5[bg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[vout]"
        )
        cmd = [
            "ffmpeg", "-y", "-i", video,
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-an",
            output,
        ]
    else:
        # 仅裁剪
        cmd = [
            "ffmpeg", "-y", "-i", video,
            "-vf", f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},format=yuv420p",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-an",
            output,
        ]

    run_cmd(cmd, f"Crop {crop_ratio*100:.0f}% edges" +
            (f" + square→{target_w}x{target_h} vertical" if to_vertical and is_square else ""))
    return output


def step_ken_burns(video: str, output: str, work_dir: str,
                    zoom_max: float, fps: int) -> str:
    """步骤2：Ken Burns 缓慢放大"""
    log.info("=" * 60)
    log.info("Step 2: Ken Burns slow zoom")
    log.info("=" * 60)

    probe = probe_video(video)
    w, h = probe["width"], probe["height"]
    zoom_increment = (zoom_max - 1.0) / (probe["duration"] * fps)

    filter_complex = (
        f"[0:v]zoompan=z='min(zoom+{zoom_increment:.6f},{zoom_max})':"
        f"d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={w}x{h}:fps={fps},format=yuv420p"
    )

    cmd = [
        "ffmpeg", "-y", "-i", video,
        "-filter_complex", filter_complex,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-an",
        output,
    ]
    run_cmd(cmd, f"Ken Burns zoom 1.0→{zoom_max}")
    return output


def step_slow_motion(video: str, output: str, work_dir: str,
                      slowmo_range: Optional[Tuple[float, float, float]]) -> str:
    """步骤3：精彩瞬间慢动作（分段提取+拼接，可靠方法）"""
    log.info("=" * 60)
    log.info("Step 3: Slow motion for highlight moment")
    log.info("=" * 60)

    if not slowmo_range:
        log.info("  ⏭ Skipping slow motion (no range specified)")
        return video

    start, end, speed = slowmo_range
    probe = probe_video(video)
    duration = probe["duration"]

    # 段1：0-start 正常
    seg1 = os.path.join(work_dir, "slowmo_seg1.mp4")
    if start > 0:
        run_cmd([
            "ffmpeg", "-y", "-ss", "0", "-i", video, "-t", str(start),
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-an", seg1,
        ], f"Segment 1: 0-{start}s (normal)")
    else:
        seg1 = None

    # 段2：start-end 慢动作（-t 必须放在 -i 前面限制输入时长）
    seg2 = os.path.join(work_dir, "slowmo_seg2.mp4")
    run_cmd([
        "ffmpeg", "-y", "-ss", str(start), "-t", str(end - start), "-i", video,
        "-filter:v", f"setpts=PTS/{speed}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-an", seg2,
    ], f"Segment 2: {start}-{end}s (slow motion {speed}x)")

    # 段3：end-duration 正常
    seg3 = os.path.join(work_dir, "slowmo_seg3.mp4")
    if end < duration:
        run_cmd([
            "ffmpeg", "-y", "-ss", str(end), "-i", video,
            "-t", str(duration - end),
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-an", seg3,
        ], f"Segment 3: {end}-{duration:.2f}s (normal)")
    else:
        seg3 = None

    # 拼接
    # ⚠️ concat demuxer 里的相对路径是「相对于 concat 文件所在目录」解析的，
    # 若 work_dir 本身是相对路径（如 --work-dir work），写进去会变成 work/work/xx.mp4。
    # 统一写绝对路径。
    concat_list = os.path.join(work_dir, "slowmo_concat.txt")
    with open(concat_list, "w") as f:
        if seg1 and os.path.exists(seg1):
            f.write(f"file '{os.path.abspath(seg1)}'\n")
        f.write(f"file '{os.path.abspath(seg2)}'\n")
        if seg3 and os.path.exists(seg3):
            f.write(f"file '{os.path.abspath(seg3)}'\n")

    run_cmd([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
        "-c", "copy", output,
    ], "Concatenate segments")

    return output


def step_title_card(video: str, output: str, work_dir: str,
                    title: str, subtitle: Optional[str]) -> str:
    """步骤4（LEGACY，默认不执行）：开头标题卡

    默认关闭：封面已承载标题/简介/品牌，首帧再放一张同样的卡属于重复信息。
    仅当显式传 `--title-card` 时才走本函数，且仍禁用纯黑背景。
    """
    log.info("=" * 60)
    log.info("Step 4: Opening title card (LEGACY, explicit --title-card only)")
    log.info("=" * 60)

    if not title:
        log.info("  ⏭ Skipping title card (no title specified)")
        return video

    # 提取第一帧作为标题卡背景
    bg_frame = os.path.join(work_dir, "title_bg.jpg")
    run_cmd([
        "ffmpeg", "-y", "-ss", "0.5", "-i", video,
        "-vframes", "1", "-q:v", "2", bg_frame,
    ], "Extract first frame for title card background")

    # 生成标题卡（关键：用视频帧模糊+暗化做背景，禁止纯黑背景）
    title_card = os.path.join(work_dir, "title_card.mp4")

    # 构建 drawtext 滤镜
    drawtext_filters = []

    # 主标题（分两行处理，如果包含换行）
    title_lines = title.split("\\n") if "\\n" in title else [title]

    probe = probe_video(video)
    fps = int(probe.get("fps", 30))
    frame_w = int(probe.get("width", 720))

    font_size = fit_font_size(title_lines, frame_w, TITLE_CARD_FONT_SIZE)
    line_height = int(font_size * 1.15)
    start_y = f"(h-text_h)/2-{int(line_height * (len(title_lines)-1) / 2)}"

    for i, line in enumerate(title_lines):
        y_offset = i * line_height
        drawtext_filters.append(
            f"drawtext=fontfile='{TITLE_CARD_FONT}':text='{line}':"
            f"fontcolor={TITLE_CARD_COLOR}:fontsize={font_size}:"
            f"x=(w-text_w)/2:y={start_y}+{y_offset}"
        )

    # 副标题
    if subtitle:
        sub_size = fit_font_size([subtitle], frame_w, TITLE_CARD_SUBTITLE_SIZE)
        sub_y = f"{start_y}+{int(line_height * len(title_lines)) + 20}"
        drawtext_filters.append(
            f"drawtext=fontfile='{TITLE_CARD_FONT}':text='{subtitle}':"
            f"fontcolor={TITLE_CARD_SUBTITLE_COLOR}:fontsize={sub_size}:"
            f"x=(w-text_w)/2:y={sub_y}"
        )

    # 完整滤镜：模糊 + 暗化 + 文字 + 淡入
    # 关键规范：brightness=-0.15（不能太高，否则看起来像黑色）
    filter_chain = (
        f"boxblur={TITLE_CARD_BLUR},"
        f"eq=brightness={TITLE_CARD_BRIGHTNESS}:contrast={TITLE_CARD_CONTRAST},"
        + ",".join(drawtext_filters) +
        f",fade=t=in:st=0:d=0.5,format=yuv420p"
    )

    run_cmd([
        "ffmpeg", "-y", "-loop", "1", "-i", bg_frame,
        "-filter_complex", filter_chain,
        "-t", str(TITLE_CARD_DURATION),
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-r", str(fps), "-an",
        title_card,
    ], f"Generate title card (blurred bg, brightness={TITLE_CARD_BRIGHTNESS})")

    # 拼接标题卡 + 主视频
    # 同 step_slow_motion：concat 列表必须写绝对路径（见该函数注释）
    concat_list = os.path.join(work_dir, "title_concat.txt")
    with open(concat_list, "w") as f:
        f.write(f"file '{os.path.abspath(title_card)}'\n")
        f.write(f"file '{os.path.abspath(video)}'\n")

    run_cmd([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
        "-c", "copy", output,
    ], "Concat title card + main video")

    return output


def step_dynamic_subtitle(video: str, output: str, work_dir: str,
                           subtitle_spec: Optional[Tuple[float, float, str]]) -> str:
    """步骤5：精彩瞬间动态英文字幕"""
    log.info("=" * 60)
    log.info("Step 5: Dynamic English subtitle at highlight moment")
    log.info("=" * 60)

    if not subtitle_spec:
        log.info("  ⏭ Skipping dynamic subtitle (no spec specified)")
        return video

    start, end, text = subtitle_spec
    fade_in = 0.5
    fade_out_start = end - 1.0

    # 字号按画面宽度自适应（方形画幅沿用 72 会溢出被裁）
    dyn_size = fit_font_size([text], int(probe_video(video).get("width", 720)),
                             DYNAMIC_SUBTITLE_SIZE)

    # 动态字幕：淡入淡出
    drawtext = (
        f"drawtext=fontfile='{TITLE_CARD_FONT}':text='{text}':"
        f"fontcolor=yellow:fontsize={dyn_size}:"
        f"x=(w-text_w)/2:y=h*0.3:"
        f"enable='between(t,{start},{end})':"
        f"alpha='if(lt(t,{start+fade_in}), (t-{start})/{fade_in}, "
        f"if(gt(t,{fade_out_start}), 1-(t-{fade_out_start})/1, 1))'"
    )

    run_cmd([
        "ffmpeg", "-y", "-i", video,
        "-vf", drawtext,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-an",
        output,
    ], f"Dynamic subtitle '{text}' at {start}-{end}s")

    return output


def step_mix_bgm(video: str, output: str, work_dir: str,
                 bgm: Optional[str]) -> str:
    """步骤6：混合 BGM（替换原音频）"""
    log.info("=" * 60)
    log.info("Step 6: Mix BGM (replace original audio)")
    log.info("=" * 60)

    if not bgm or not os.path.exists(bgm):
        log.warning("  ⚠️ No BGM specified or file not found, skipping audio mix")
        # 复制视频（无音频）
        run_cmd(["ffmpeg", "-y", "-i", video, "-c", "copy", "-an", output],
                "Copy video without audio")
        return output

    probe = probe_video(video)
    duration = probe["duration"]
    fade_in = 2
    fade_out = 3
    fade_out_start = duration - fade_out

    filter_complex = (
        f"[1:a]atrim=0:{duration},asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d={fade_in},"
        f"afade=t=out:st={fade_out_start}:d={fade_out},"
        f"volume=-8dB[bgm]"
    )

    run_cmd([
        "ffmpeg", "-y", "-i", video, "-i", bgm,
        "-filter_complex", filter_complex,
        "-c:v", "copy", "-map", "0:v", "-map", "[bgm]",
        "-shortest", output,
    ], f"Mix BGM (duration={duration:.2f}s, fade in={fade_in}s, fade out={fade_out}s)")

    return output


def parse_slowmo_spec(spec: Optional[str]) -> Optional[Tuple[float, float, float]]:
    """解析慢动作参数：start-end:speed，如 '44-46:0.6'"""
    if not spec:
        return None
    try:
        range_part, speed_part = spec.split(":")
        start, end = range_part.split("-")
        return (float(start), float(end), float(speed_part))
    except Exception as e:
        log.error(f"Invalid slowmo spec '{spec}': {e}")
        return None


def parse_subtitle_spec(spec: Optional[str]) -> Optional[Tuple[float, float, str]]:
    """解析动态字幕参数：start-end:text，如 '46-51:THE GOAT!'"""
    if not spec:
        return None
    try:
        range_part, text = spec.split(":", 1)
        start, end = range_part.split("-")
        return (float(start), float(end), text)
    except Exception as e:
        log.error(f"Invalid subtitle spec '{spec}': {e}")
        return None


def main():
    ap = argparse.ArgumentParser(
        description="TikTok Video Originalizer - 原创化处理，突破非原创判定",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 默认：正片直入，不加任何片头卡（推荐）
  python originalize_video.py --video input.mp4 --output final.mp4 \\
    --zoom 1.08 --slowmo "44-46:0.6" --dynamic-text "46-51:THE GOAT!" \\
    --bgm bgm.mp3

  [LEGACY] 确需片头标题卡时才显式开启（默认关闭）
  python originalize_video.py --video input.mp4 --output final.mp4 \\
    --title-card --title "BUZZER BEATER" --subtitle "AT THE BUZZER"

关键规范：
  - ⚠️ 成片第 0 帧必须是正片画面：封面已有标题/简介/logo，片头再来一张卡是重复信息
  - [LEGACY] 若显式启用标题卡：背景用视频帧模糊+暗化（brightness=-0.15），禁止纯黑背景
  - 慢动作 -t 参数必须放在 -i 前面
  - ⚠️ 边缘裁剪可能影响英文记分牌/比分牌，如记分牌在边缘请用 --no-crop 跳过裁剪
  - ⚠️ 只有中文区域才需要遮罩/inpaint处理，英文记分牌不需要处理
        """,
    )
    ap.add_argument("--video", required=True, help="输入视频路径")
    ap.add_argument("--output", required=True, help="输出视频路径")
    ap.add_argument("--work-dir", default="work", help="工作目录（中间文件）")
    ap.add_argument("--crop", type=float, default=DEFAULT_CROP_RATIO,
                    help=f"边缘裁剪比例（默认 {DEFAULT_CROP_RATIO}，即 8%%）")
    ap.add_argument("--no-crop", action="store_true",
                    help="跳过边缘裁剪（保留英文记分牌/比分牌，无中文视频推荐使用）")
    ap.add_argument("--to-vertical", action="store_true",
                    help="正方形视频自动转竖版（720x1280，模糊背景填充）")
    ap.add_argument("--zoom", type=float, default=DEFAULT_ZOOM_MAX,
                    help=f"Ken Burns 最大放大倍数（默认 {DEFAULT_ZOOM_MAX}）")
    ap.add_argument("--slowmo", help="慢动作：start-end:speed，如 '44-46:0.6'")
    ap.add_argument("--title-card", action="store_true",
                    help="[LEGACY] 生成片头标题卡。默认关闭：封面已含标题/品牌，"
                         "首帧必须是正片画面。仅在用户明确要求时开启。")
    ap.add_argument("--title", help="[LEGACY] 标题卡主标题（需同时传 --title-card）")
    ap.add_argument("--subtitle", help="[LEGACY] 标题卡副标题（需同时传 --title-card）")
    ap.add_argument("--dynamic-text", help="动态字幕：start-end:text，如 '46-51:THE GOAT!'")
    ap.add_argument("--bgm", help="BGM 文件路径（替换原音频）")
    ap.add_argument("--skip-steps", nargs="*", default=[],
                    help="跳过的步骤：crop/zoom/slowmo/title/subtitle/bgm")
    args = ap.parse_args()

    require_tools("ffmpeg", "ffprobe")
    ensure_dir(args.work_dir)

    log.info("=" * 60)
    log.info("TikTok Video Originalizer - 原创化处理")
    log.info("=" * 60)
    log.info(f"  Input: {args.video}")
    log.info(f"  Output: {args.output}")
    if args.title_card and args.title:
        log.info(f"  Title card: ENABLED (LEGACY) — {args.title!r}")
    else:
        log.info("  Title card: disabled (default) — 第 0 帧直接是正片画面")
        if args.title or args.subtitle:
            log.warning("  ⚠️ 收到 --title/--subtitle 但未加 --title-card，已忽略。"
                        "标题请写进 cover.jpg 与 tiktok-post.md，不要放在片头。")
    log.info(f"  Slowmo: {args.slowmo or '(none)'}")
    log.info(f"  Dynamic text: {args.dynamic_text or '(none)'}")
    log.info(f"  BGM: {args.bgm or '(none)'}")
    log.info("")

    current = args.video
    step_num = 0

    # 步骤1：裁剪 + 转竖版
    if "crop" not in args.skip_steps and not args.no_crop:
        step_num += 1
        out = os.path.join(args.work_dir, f"step{step_num}_crop.mp4")
        current = step_crop_and_vertical(
            current, out, args.work_dir,
            args.crop, args.to_vertical,
            DEFAULT_VERTICAL_WIDTH, DEFAULT_VERTICAL_HEIGHT,
        )
    elif args.no_crop and args.to_vertical:
        # 只转竖版，不裁剪
        log.info("⏭ Skipping edge crop (--no-crop), but converting to vertical...")
        step_num += 1
        out = os.path.join(args.work_dir, f"step{step_num}_vertical.mp4")
        # 用0裁剪比例调用，只做转竖版
        current = step_crop_and_vertical(
            current, out, args.work_dir,
            0.0, args.to_vertical,
            DEFAULT_VERTICAL_WIDTH, DEFAULT_VERTICAL_HEIGHT,
        )
    elif args.no_crop:
        log.info("⏭ Skipping edge crop (--no-crop)")

    # 步骤2：Ken Burns 缩放
    if "zoom" not in args.skip_steps:
        step_num += 1
        out = os.path.join(args.work_dir, f"step{step_num}_zoom.mp4")
        probe = probe_video(current)
        current = step_ken_burns(
            current, out, args.work_dir,
            args.zoom, int(probe.get("fps", 30)),
        )

    # 步骤3：慢动作
    if "slowmo" not in args.skip_steps:
        step_num += 1
        out = os.path.join(args.work_dir, f"step{step_num}_slowmo.mp4")
        slowmo_spec = parse_slowmo_spec(args.slowmo)
        current = step_slow_motion(current, out, args.work_dir, slowmo_spec)

    # 步骤4：标题卡（LEGACY，默认关闭 —— 首帧必须是正片画面）
    if args.title_card and args.title and "title" not in args.skip_steps:
        step_num += 1
        out = os.path.join(args.work_dir, f"step{step_num}_title.mp4")
        current = step_title_card(current, out, args.work_dir, args.title, args.subtitle)
    else:
        log.info("⏭ Skipping opening title card (default) — 成片直接进入正片")

    # 步骤5：动态字幕
    if "subtitle" not in args.skip_steps:
        step_num += 1
        out = os.path.join(args.work_dir, f"step{step_num}_subtitle.mp4")
        subtitle_spec = parse_subtitle_spec(args.dynamic_text)
        current = step_dynamic_subtitle(current, out, args.work_dir, subtitle_spec)

    # 步骤6：混合 BGM
    if "bgm" not in args.skip_steps:
        step_num += 1
        current = step_mix_bgm(current, args.output, args.work_dir, args.bgm)
    else:
        # 复制到最终输出
        run_cmd(["ffmpeg", "-y", "-i", current, "-c", "copy", args.output],
                "Copy to final output")

    # 验证输出
    log.info("")
    log.info("=" * 60)
    log.info("原创化处理完成")
    log.info("=" * 60)

    final_duration = 0.0
    if os.path.exists(args.output):
        size = os.path.getsize(args.output)
        out_probe = probe_video(args.output)
        final_duration = out_probe["duration"]
        log.info(f"  ✅ Output: {args.output}")
        log.info(f"  Size: {size/1024/1024:.1f} MB")
        log.info(f"  Duration: {final_duration:.2f}s")
        log.info(f"  Resolution: {out_probe.get('width', '?')}x{out_probe.get('height', '?')}")
    else:
        log.error(f"  ❌ Output file not found: {args.output}")

    # 保存结果
    # duration_sec 供 qa_video.check_video 作为时长基准：
    # 原创化（慢动作/变速/BGM）会改变总时长，若 QA 仍拿 probe.json 里的
    # 源时长比对，会误报 "Duration: expected=14.5s, actual=16.0s" 之类的 FAIL。
    steps_done = ["ken_burns_zoom", "bgm_replacement"]
    if "crop" not in args.skip_steps and not args.no_crop:
        steps_done.insert(0, "edge_crop")
    if "slowmo" not in args.skip_steps and args.slowmo:
        steps_done.insert(-1, "slow_motion")
    if "subtitle" not in args.skip_steps and args.dynamic_text:
        steps_done.insert(-1, "dynamic_subtitle")
    if args.title_card and args.title and "title" not in args.skip_steps:
        steps_done.insert(0, "title_card")

    result = make_result(
        status="ok",
        confidence=0.9,
        output_path=args.output,
        duration_sec=round(final_duration, 3),
        originalization_steps=steps_done,
        # 默认 False：首帧即正片画面，封面负责标题与品牌
        opening_title_card=bool(args.title_card and args.title),
    )
    if result.get("opening_title_card"):
        result["title_card_style"] = "blurred_video_frame_background (NOT black)"
    save_json(os.path.join(args.work_dir, "originalize-result.json"), result)


if __name__ == "__main__":
    main()
