#!/usr/bin/env python3
"""
detect_text_regions.py - OCR 区域聚合去重，产出疑似文字窗口

输入：ocr.json
输出：
  - work/text-regions.json（聚合后的区域证据包）
  - work/dense-windows.json（疑似文字窗口列表，供 extract_frames.py 第二遍加密采样）

职责：确定性聚合，不做分类判断。
产出"证据包"（位置/时长/重复性特征），最终分类由 Agent + classify_text.md 完成。
"""

import argparse
import os
import sys
from typing import List, Dict, Any, Tuple
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    save_json, load_json, make_result, log,
    work_path, bbox_iou, text_overlap,
)


def merge_detections(detections: List[Dict], iou_threshold: float = 0.3,
                      text_sim_threshold: float = 0.6) -> List[Dict]:
    """
    聚合 OCR 检测结果：同一帧内空间重叠且文本相似的合并为一个区域。
    跨帧的关联由 track_text.py 完成。
    """
    # 按帧分组
    by_frame = defaultdict(list)
    for d in detections:
        by_frame[d.get("frame_id", "unknown")].append(d)

    merged = []
    for frame_id, frame_dets in by_frame.items():
        # 按 bbox 排序
        frame_dets.sort(key=lambda d: (d["bbox"]["y"], d["bbox"]["x"]))

        used = [False] * len(frame_dets)
        for i in range(len(frame_dets)):
            if used[i]:
                continue
            group = [frame_dets[i]]
            used[i] = True
            for j in range(i + 1, len(frame_dets)):
                if used[j]:
                    continue
                # 检查空间重叠和文本相似
                iou = bbox_iou(frame_dets[i]["bbox"], frame_dets[j]["bbox"])
                sim = text_overlap(frame_dets[i]["text"], frame_dets[j]["text"])
                if iou > iou_threshold or sim > text_sim_threshold:
                    group.append(frame_dets[j])
                    used[j] = True

            # 合并 group
            if len(group) == 1:
                merged.append(group[0].copy())
            else:
                # 合并 bbox
                xs = [g["bbox"]["x"] for g in group]
                ys = [g["bbox"]["y"] for g in group]
                ws = [g["bbox"]["x"] + g["bbox"]["w"] for g in group]
                hs = [g["bbox"]["y"] + g["bbox"]["h"] for g in group]
                merged_text = " ".join(g["text"] for g in group)
                merged.append({
                    "frame_id": frame_id,
                    "timestamp_sec": group[0]["timestamp_sec"],
                    "pass": group[0].get("pass", "unknown"),
                    "text": merged_text,
                    "language": group[0]["language"],
                    "bbox": {
                        "x": int(min(xs)),
                        "y": int(min(ys)),
                        "w": int(max(ws) - min(xs)),
                        "h": int(max(hs) - min(ys)),
                    },
                    "confidence": round(sum(g["confidence"] for g in group) / len(group), 4),
                    "merged_count": len(group),
                })

    return merged


def extract_dense_windows(regions: List[Dict], pad_sec: float = 1.5,
                           min_gap_sec: float = 2.0) -> List[Dict]:
    """
    从聚合区域中提取疑似文字窗口，供第二遍加密采样。
    合并时间上接近的窗口。
    """
    if not regions:
        return []

    # 按时间排序
    sorted_regions = sorted(regions, key=lambda r: r.get("timestamp_sec") or 0)

    windows = []
    current_start = None
    current_end = None

    for r in sorted_regions:
        ts = r.get("timestamp_sec")
        if ts is None:
            continue
        if current_start is None:
            current_start = ts
            current_end = ts
        elif ts - current_end <= min_gap_sec:
            current_end = ts
        else:
            windows.append({
                "start": round(max(0, current_start - pad_sec), 3),
                "end": round(current_end + pad_sec, 3),
            })
            current_start = ts
            current_end = ts

    if current_start is not None:
        windows.append({
            "start": round(max(0, current_start - pad_sec), 3),
            "end": round(current_end + pad_sec, 3),
        })

    # 合并重叠窗口
    merged = []
    for w in sorted(windows, key=lambda x: x["start"]):
        if merged and w["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], w["end"])
        else:
            merged.append(w.copy())

    return merged


def compute_region_features(regions: List[Dict]) -> List[Dict]:
    """为每个区域计算特征（供 Agent 分类使用的证据包）"""
    # 按文本聚类
    text_groups = defaultdict(list)
    for r in regions:
        key = r["text"][:20]  # 用前20字符做粗略聚类
        text_groups[key].append(r)

    enriched = []
    for r in regions:
        key = r["text"][:20]
        group = text_groups[key]
        timestamps = [g.get("timestamp_sec") for g in group if g.get("timestamp_sec") is not None]
        enriched.append({
            **r,
            "repeat_count": len(group),
            "first_seen": round(min(timestamps), 3) if timestamps else None,
            "last_seen": round(max(timestamps), 3) if timestamps else None,
            "time_span": round(max(timestamps) - min(timestamps), 3) if len(timestamps) > 1 else 0,
        })

    return enriched


def main():
    ap = argparse.ArgumentParser(description="Detect and aggregate text regions from OCR results")
    ap.add_argument("--work-dir", required=True, help="Working directory")
    ap.add_argument("--ocr-json", help="Path to ocr.json (default: work/ocr.json)")
    ap.add_argument("--iou-threshold", type=float, default=0.3, help="BBox IoU threshold for merging")
    ap.add_argument("--text-sim-threshold", type=float, default=0.6, help="Text similarity threshold for merging")
    ap.add_argument("--pad-sec", type=float, default=1.5, help="Padding for dense windows")
    ap.add_argument("--emit-dense-windows", action="store_true", help="Emit dense-windows.json for pass 2 sampling")
    args = ap.parse_args()

    ocr_path = args.ocr_json or work_path(args.work_dir, "ocr.json")
    ocr_data = load_json(ocr_path, {})
    detections = ocr_data.get("detections", [])

    if not detections:
        log.warning("No OCR detections found")
        result = make_result(
            status="ok",
            confidence=1.0,
            regions=[],
            dense_windows=[],
            warnings=["No text detected in video"],
        )
        save_json(work_path(args.work_dir, "text-regions.json"), result)
        if args.emit_dense_windows:
            save_json(work_path(args.work_dir, "dense-windows.json"), [])
        return

    log.info(f"Aggregating {len(detections)} OCR detections")

    # 合并同一帧内的检测
    merged = merge_detections(detections, args.iou_threshold, args.text_sim_threshold)
    log.info(f"Merged into {len(merged)} regions")

    # 计算特征
    enriched = compute_region_features(merged)

    # 提取密集窗口
    dense_windows = extract_dense_windows(merged, args.pad_sec) if args.emit_dense_windows else []

    result = make_result(
        status="ok",
        confidence=0.9,
        regions=enriched,
        dense_windows=dense_windows,
        total_detections=len(detections),
        merged_regions=len(merged),
        unique_texts=len(set(r["text"] for r in merged)),
        chinese_regions=len([r for r in merged if r["language"] == "zh"]),
    )

    save_json(work_path(args.work_dir, "text-regions.json"), result)

    if args.emit_dense_windows:
        save_json(work_path(args.work_dir, "dense-windows.json"), dense_windows)
        log.info(f"Emitted {len(dense_windows)} dense windows for pass 2 sampling")

    log.info(f"Region detection complete: {len(merged)} regions -> text-regions.json")
    log.info(f"  Chinese regions: {result['chinese_regions']}, Unique texts: {result['unique_texts']}")


if __name__ == "__main__":
    main()
