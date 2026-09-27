#!/usr/bin/env python3
"""
track_text.py - 跨帧文字跟踪

输入：ocr.json（两遍采样合并后）+ probe.json（画面尺寸）+ asr.json（可选，用于 asr_text_overlap）
输出：work/text-tracks.json

职责：确定性跨帧关联，输出每个文字 track 的：
  text / language / bbox / start_time / end_time / confidence
  position（九宫格）/ persistence（full_video/long/short/flash）
  trajectory（static/moving）/ asr_text_overlap（词汇重叠，非语义！）

不做分类判断（分类由 Agent + classify_text.md 完成）。
asr_text_overlap 只是词汇重叠（中文 char-bigram Jaccard / 英文 word Jaccard），
不是语义重合；真语义判断（semantic_match）由 Agent 完成，脚本产出时恒为 null。
"""

import argparse
import os
import sys
from typing import List, Dict, Any, Optional
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    save_json, load_json, make_result, log,
    work_path, bbox_iou, bbox_position_zone, text_overlap,
)


def associate_tracks(detections: List[Dict], iou_threshold: float = 0.3,
                      text_sim_threshold: float = 0.6, max_gap_sec: float = 2.0) -> List[Dict]:
    """
    跨帧关联 OCR 检测结果为 tracks。
    关联条件：空间重叠 IoU > 阈值 或 文本相似度 > 阈值，且时间间隔 <= max_gap_sec。
    """
    # 按时间排序
    sorted_dets = sorted(
        [d for d in detections if d.get("timestamp_sec") is not None],
        key=lambda d: d["timestamp_sec"],
    )

    tracks = []  # 每个 track: {detections: [], ...}
    active_tracks = []  # 当前活跃的 track 索引

    for det in sorted_dets:
        ts = det["timestamp_sec"]
        best_track_idx = None
        best_score = 0

        # 清理过期的活跃 track
        active_tracks = [
            idx for idx in active_tracks
            if ts - tracks[idx]["last_time"] <= max_gap_sec
        ]

        # 在活跃 track 中找最佳匹配
        for idx in active_tracks:
            track = tracks[idx]
            last_det = track["detections"][-1]
            iou = bbox_iou(last_det["bbox"], det["bbox"])
            sim = text_overlap(last_det["text"], det["text"])
            score = max(iou, sim * 0.8)  # 综合评分
            if score > best_score and (iou > iou_threshold or sim > text_sim_threshold):
                best_score = score
                best_track_idx = idx

        if best_track_idx is not None:
            tracks[best_track_idx]["detections"].append(det)
            tracks[best_track_idx]["last_time"] = ts
        else:
            # 新 track
            new_idx = len(tracks)
            tracks.append({
                "track_id": f"t{new_idx + 1}",
                "detections": [det],
                "first_time": ts,
                "last_time": ts,
            })
            active_tracks.append(new_idx)

    return tracks


def compute_track_features(track: Dict, frame_w: int, frame_h: int,
                           asr_segments: List[Dict],
                           persistence_thresholds: Dict) -> Dict:
    """计算单个 track 的特征"""
    dets = track["detections"]
    if not dets:
        return None

    # 基本信息
    texts = [d["text"] for d in dets]
    # 取出现次数最多的文本作为代表
    text_counts = defaultdict(int)
    for t in texts:
        text_counts[t] += 1
    representative_text = max(text_counts, key=text_counts.get)

    languages = [d.get("language", "unknown") for d in dets]
    lang_counts = defaultdict(int)
    for l in languages:
        lang_counts[l] += 1
    representative_lang = max(lang_counts, key=lang_counts.get)

    # 时间范围
    start_time = track["first_time"]
    end_time = track["last_time"]
    duration = end_time - start_time

    # BBox：取所有检测的并集
    xs = [d["bbox"]["x"] for d in dets]
    ys = [d["bbox"]["y"] for d in dets]
    ws = [d["bbox"]["x"] + d["bbox"]["w"] for d in dets]
    hs = [d["bbox"]["y"] + d["bbox"]["h"] for d in dets]
    bbox = {
        "x": int(min(xs)),
        "y": int(min(ys)),
        "w": int(max(ws) - min(xs)),
        "h": int(max(hs) - min(ys)),
    }

    # 位置（九宫格）
    position = bbox_position_zone(bbox, frame_w, frame_h)

    # 持续时间分类
    if duration >= persistence_thresholds.get("long", 30):
        persistence = "full_video"
    elif duration >= persistence_thresholds.get("short", 5):
        persistence = "long"
    elif duration >= persistence_thresholds.get("flash", 1):
        persistence = "short"
    else:
        persistence = "flash"

    # 轨迹：判断是否移动
    if len(dets) >= 2:
        first_center = ((dets[0]["bbox"]["x"] + dets[0]["bbox"]["w"] / 2),
                        (dets[0]["bbox"]["y"] + dets[0]["bbox"]["h"] / 2))
        last_center = ((dets[-1]["bbox"]["x"] + dets[-1]["bbox"]["w"] / 2),
                       (dets[-1]["bbox"]["y"] + dets[-1]["bbox"]["h"] / 2))
        move_dist = ((first_center[0] - last_center[0]) ** 2 +
                     (first_center[1] - last_center[1]) ** 2) ** 0.5
        trajectory = "moving" if move_dist > max(bbox["w"], bbox["h"]) * 0.3 else "static"
        trajectory_points = [
            {"timestamp_sec": d["timestamp_sec"], "x": d["bbox"]["x"], "y": d["bbox"]["y"]}
            for d in dets[::max(1, len(dets) // 10)]  # 最多10个采样点
        ] if trajectory == "moving" else None
    else:
        trajectory = "static"
        trajectory_points = None

    # 置信度：取平均
    avg_confidence = sum(d.get("confidence", 0.5) for d in dets) / len(dets)

    # asr_text_overlap：与该时段 ASR 文本的词汇重叠（非语义！）
    asr_text_overlap_val = 0.0
    if asr_segments:
        # 找时间重叠的 ASR 段
        overlapping_asr = [
            seg for seg in asr_segments
            if seg.get("start", 0) <= end_time and seg.get("end", 0) >= start_time
        ]
        if overlapping_asr:
            asr_text = " ".join(seg.get("text", "") for seg in overlapping_asr)
            asr_text_overlap_val = text_overlap(representative_text, asr_text)

    return {
        "track_id": track["track_id"],
        "text": representative_text,
        "language": representative_lang,
        "bbox": bbox,
        "start_time": round(start_time, 3),
        "end_time": round(end_time, 3),
        "duration_sec": round(duration, 3),
        "position": position,
        "persistence": persistence,
        "trajectory": trajectory,
        "trajectory_points": trajectory_points,
        "confidence": round(avg_confidence, 4),
        "detection_count": len(dets),
        "asr_text_overlap": round(asr_text_overlap_val, 4),
        "asr_text_overlap_note": "lexical overlap only (zh char-bigram / en word Jaccard), NOT semantic",
        "semantic_match": None,  # 由 Agent 填写，脚本不做语义判断
        "frame_samples": [
            {"frame_id": d.get("frame_id"), "timestamp_sec": d.get("timestamp_sec")}
            for d in dets[::max(1, len(dets) // 5)]  # 最多5个采样帧
        ],
    }


def main():
    ap = argparse.ArgumentParser(description="Track text across frames and compute features")
    ap.add_argument("--work-dir", required=True, help="Working directory")
    ap.add_argument("--ocr-json", help="Path to ocr.json (default: work/ocr.json)")
    ap.add_argument("--probe-json", help="Path to probe.json (default: work/probe.json)")
    ap.add_argument("--asr-json", help="Path to asr.json (optional, for asr_text_overlap)")
    ap.add_argument("--iou-threshold", type=float, default=0.3, help="BBox IoU threshold for association")
    ap.add_argument("--text-sim-threshold", type=float, default=0.6, help="Text similarity threshold")
    ap.add_argument("--max-gap-sec", type=float, default=2.0, help="Max gap between detections in same track")
    args = ap.parse_args()

    # 加载数据
    ocr_path = args.ocr_json or work_path(args.work_dir, "ocr.json")
    probe_path = args.probe_json or work_path(args.work_dir, "probe.json")
    asr_path = args.asr_json or work_path(args.work_dir, "asr.json")

    ocr_data = load_json(ocr_path, {})
    detections = ocr_data.get("detections", [])
    probe = load_json(probe_path, {})
    asr_data = load_json(asr_path, {}) if os.path.exists(asr_path) else {}
    asr_segments = asr_data.get("segments", [])

    frame_w = probe.get("width", 1080)
    frame_h = probe.get("height", 1920)

    if not detections:
        log.warning("No OCR detections to track")
        result = make_result(
            status="ok",
            confidence=1.0,
            tracks=[],
            warnings=["No text detected"],
        )
        save_json(work_path(args.work_dir, "text-tracks.json"), result)
        return

    log.info(f"Tracking {len(detections)} detections across frames")

    # 关联 tracks
    raw_tracks = associate_tracks(
        detections, args.iou_threshold, args.text_sim_threshold, args.max_gap_sec,
    )
    log.info(f"Found {len(raw_tracks)} tracks")

    # 持久化阈值
    persistence_thresholds = {
        "flash": 1.0,
        "short": 5.0,
        "long": 30.0,
    }

    # 计算特征
    tracks = []
    for rt in raw_tracks:
        feat = compute_track_features(rt, frame_w, frame_h, asr_segments, persistence_thresholds)
        if feat:
            tracks.append(feat)

    # 按 start_time 排序
    tracks.sort(key=lambda t: t["start_time"])

    result = make_result(
        status="ok",
        confidence=0.85,
        tracks=tracks,
        total_tracks=len(tracks),
        chinese_tracks=len([t for t in tracks if t["language"] == "zh"]),
        full_video_tracks=len([t for t in tracks if t["persistence"] == "full_video"]),
        flash_tracks=len([t for t in tracks if t["persistence"] == "flash"]),
    )

    out_path = work_path(args.work_dir, "text-tracks.json")
    save_json(out_path, result)

    log.info(f"Text tracking complete: {len(tracks)} tracks -> {out_path}")
    log.info(f"  Chinese: {result['chinese_tracks']}, Full-video: {result['full_video_tracks']}, Flash: {result['flash_tracks']}")


if __name__ == "__main__":
    main()
