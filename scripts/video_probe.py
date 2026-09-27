#!/usr/bin/env python3
"""
video_probe.py - FFprobe 提取视频元数据

输入：source video
输出：work/probe.json

职责：确定性地提取视频元数据，不做任何语义判断。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    run, save_json, require_tools, make_result,
    EXIT_FATAL, log, work_path,
)


def probe_video(video_path: str) -> dict:
    """用 ffprobe 提取视频元数据"""
    require_tools("ffprobe")

    if not os.path.exists(video_path):
        log.error(f"Video not found: {video_path}")
        sys.exit(EXIT_FATAL)

    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        video_path,
    ]
    r = run(cmd)
    data = json.loads(r.stdout)

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"),
        None,
    )

    if not video_stream:
        log.error("No video stream found")
        sys.exit(EXIT_FATAL)

    # 解析帧率
    fps = 30.0
    r_frame_rate = video_stream.get("r_frame_rate", "30/1")
    try:
        num, den = r_frame_rate.split("/")
        fps = round(int(num) / max(int(den), 1), 3)
    except (ValueError, ZeroDivisionError):
        pass

    # 解析时长
    duration = 0.0
    if data.get("format", {}).get("duration"):
        duration = float(data["format"]["duration"])
    elif video_stream.get("duration"):
        duration = float(video_stream["duration"])

    # 旋转角度
    rotation = 0
    side_data = video_stream.get("side_data_list", [])
    for sd in side_data:
        if sd.get("rotation") is not None:
            rotation = int(sd["rotation"])
            break

    # 宽高（考虑旋转）
    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    if abs(rotation) == 90:
        width, height = height, width

    result = make_result(
        status="ok",
        confidence=1.0,
        path=os.path.abspath(video_path),
        duration_sec=round(duration, 3),
        width=width,
        height=height,
        fps=fps,
        rotation=rotation,
        has_audio=audio_stream is not None,
        audio={
            "codec": audio_stream.get("codec_name") if audio_stream else None,
            "channels": audio_stream.get("channels") if audio_stream else None,
            "sample_rate": int(audio_stream.get("sample_rate", 0)) if audio_stream else None,
            "duration_sec": float(audio_stream.get("duration", 0)) if audio_stream and audio_stream.get("duration") else None,
        } if audio_stream else None,
        video_codec=video_stream.get("codec_name"),
        bit_rate=int(data.get("format", {}).get("bit_rate", 0)) if data.get("format", {}).get("bit_rate") else None,
        format_name=data.get("format", {}).get("format_name"),
    )

    # 警告
    warnings = []
    if duration < 1:
        warnings.append("Video duration is very short (<1s)")
    if fps < 1 or fps > 120:
        warnings.append(f"Unusual FPS: {fps}")
    if not audio_stream:
        warnings.append("No audio stream")
    result["warnings"] = warnings

    return result


def main():
    ap = argparse.ArgumentParser(description="Probe video metadata with ffprobe")
    ap.add_argument("--video", required=True, help="Path to input video")
    ap.add_argument("--work-dir", required=True, help="Working directory for intermediate files")
    args = ap.parse_args()

    os.makedirs(args.work_dir, exist_ok=True)

    result = probe_video(args.video)
    out_path = work_path(args.work_dir, "probe.json")
    save_json(out_path, result)

    log.info(f"Probe complete: {out_path}")
    log.info(f"  Duration: {result['duration_sec']}s, "
             f"Resolution: {result['width']}x{result['height']}, "
             f"FPS: {result['fps']}, "
             f"Audio: {'yes' if result['has_audio'] else 'no'}")

    if result["warnings"]:
        for w in result["warnings"]:
            log.warning(f"  Warning: {w}")


if __name__ == "__main__":
    main()
