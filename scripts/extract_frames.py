#!/usr/bin/env python3
"""
extract_frames.py - 自适应帧采样

Pass 1 (coarse): 1 FPS + 场景变化帧
Pass 2 (dense): --dense-windows 指定疑似文字窗口，局部加密采样（如5FPS）

输出：work/frames/<pass>/... + work/frames-index.json（合并）

职责：确定性抽帧，不做文字检测（文字检测由 ocr_video.py 完成）。
四层采样策略：
  第一层：低成本粗采样（1FPS）
  第二层：Scene Change
  第三层：发现疑似文字后局部加密采样
  第四层：确认文字出现/消失时间（由 track_text.py 完成）
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    run, save_json, load_json, require_tools,
    make_result, log, work_path, ensure_dir,
)


def extract_coarse(video: str, work_dir: str, coarse_fps: float, scene_threshold: float) -> list:
    """Pass 1: 粗采样 - 1FPS + 场景变化"""
    frames_dir = ensure_dir(os.path.join(work_dir, "frames", "coarse"))
    scene_dir = ensure_dir(os.path.join(work_dir, "frames", "scene"))
    index = []

    # 1FPS 定时采样
    run([
        "ffmpeg", "-y", "-i", video,
        "-vf", f"fps={coarse_fps}",
        os.path.join(frames_dir, "c_%05d.png"),
    ])

    coarse_files = sorted([f for f in os.listdir(frames_dir) if f.endswith(".png")])
    for i, fn in enumerate(coarse_files):
        index.append({
            "frame_id": f"c_{i+1:05d}",
            "pass": "coarse",
            "path": os.path.join(frames_dir, fn),
            "timestamp_sec": round((i + 1) / coarse_fps, 3),
        })

    # 场景变化帧
    r = run([
        "ffmpeg", "-y", "-i", video,
        "-vf", f"select='gt(scene,{scene_threshold})',showinfo",
        "-vsync", "vfr",
        os.path.join(scene_dir, "s_%04d.png"),
    ], check=False)

    # 从 stderr 提取时间戳
    times = [float(m) for m in re.findall(r"pts_time:([\d.]+)", r.stderr or "")]
    scene_files = sorted([f for f in os.listdir(scene_dir) if f.endswith(".png")])
    for i, fn in enumerate(scene_files):
        ts = times[i] if i < len(times) else None
        index.append({
            "frame_id": f"s_{i+1:04d}",
            "pass": "scene",
            "path": os.path.join(scene_dir, fn),
            "timestamp_sec": round(ts, 3) if ts is not None else None,
        })

    return index


def extract_dense(video: str, work_dir: str, dense_fps: float, windows: list,
                   max_window_sec: float, pad_sec: float) -> list:
    """Pass 2: 局部加密采样 - 仅对疑似文字窗口加密"""
    dense_dir = ensure_dir(os.path.join(work_dir, "frames", "dense"))
    index = []

    for wi, w in enumerate(windows):
        start = max(0, float(w["start"]) - pad_sec)
        end = float(w["end"]) + pad_sec
        # 限制单窗口时长
        if end - start > max_window_sec:
            end = start + max_window_sec
            log.warning(f"Window {wi} exceeds max {max_window_sec}s, truncated")

        duration = end - start
        if duration <= 0:
            continue

        out_pattern = os.path.join(dense_dir, f"d_{wi:03d}_%05d.png")
        run([
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", video,
            "-t", str(duration),
            "-vf", f"fps={dense_fps}",
            out_pattern,
        ])

        dense_files = sorted([
            f for f in os.listdir(dense_dir)
            if f.startswith(f"d_{wi:03d}_") and f.endswith(".png")
        ])
        for i, fn in enumerate(dense_files):
            ts = round(start + (i + 1) / dense_fps, 3)
            index.append({
                "frame_id": f"d_{wi:03d}_{i+1:05d}",
                "pass": "dense",
                "window_index": wi,
                "path": os.path.join(dense_dir, fn),
                "timestamp_sec": ts,
            })

    return index


def main():
    ap = argparse.ArgumentParser(description="Adaptive frame extraction")
    ap.add_argument("--video", required=True, help="Path to input video")
    ap.add_argument("--work-dir", required=True, help="Working directory")
    ap.add_argument("--coarse-fps", type=float, default=1.0, help="Coarse sampling FPS")
    ap.add_argument("--dense-fps", type=float, default=5.0, help="Dense sampling FPS for suspect windows")
    ap.add_argument("--scene-threshold", type=float, default=0.3, help="Scene change threshold")
    ap.add_argument("--dense-windows", help="JSON file: [{start,end}] for pass 2 dense sampling")
    ap.add_argument("--max-window-sec", type=float, default=30.0, help="Max single dense window duration")
    ap.add_argument("--pad-sec", type=float, default=1.5, help="Padding around dense windows")
    args = ap.parse_args()

    require_tools("ffmpeg")
    os.makedirs(args.work_dir, exist_ok=True)

    index_path = work_path(args.work_dir, "frames-index.json")

    if not args.dense_windows:
        # Pass 1: coarse
        log.info("Pass 1: coarse sampling (1FPS + scene changes)")
        index = extract_coarse(args.video, args.work_dir, args.coarse_fps, args.scene_threshold)
        save_json(index_path, index)
        log.info(f"Coarse sampling complete: {len(index)} frames -> {index_path}")
    else:
        # Pass 2: dense on suspect windows
        windows = load_json(args.dense_windows, [])
        if not windows:
            log.warning("No dense windows specified, skipping pass 2")
            # 保留已有 index
            existing = load_json(index_path, [])
            save_json(index_path, existing)
            return

        log.info(f"Pass 2: dense sampling on {len(windows)} suspect windows")
        dense_index = extract_dense(
            args.video, args.work_dir, args.dense_fps,
            windows, args.max_window_sec, args.pad_sec,
        )

        # 合并到已有 index
        existing = load_json(index_path, [])
        # 去重：移除已有 dense pass 的帧
        existing = [f for f in existing if f.get("pass") != "dense"]
        merged = existing + dense_index
        # 按时间排序
        merged.sort(key=lambda f: f.get("timestamp_sec") or 0)
        save_json(index_path, merged)

        result = make_result(
            status="ok",
            confidence=1.0,
            total_frames=len(merged),
            coarse_frames=len([f for f in merged if f.get("pass") == "coarse"]),
            scene_frames=len([f for f in merged if f.get("pass") == "scene"]),
            dense_frames=len([f for f in merged if f.get("pass") == "dense"]),
            dense_windows=len(windows),
        )
        save_json(work_path(args.work_dir, "frames-result.json"), result)
        log.info(f"Dense sampling complete: {len(dense_index)} new frames, total {len(merged)}")


if __name__ == "__main__":
    main()
