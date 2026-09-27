#!/usr/bin/env python3
"""
generate_masks.py - 根据 localization-plan 生成时序 mask

输入：localization-plan.json + probe.json
输出：work/masks/ 目录下的 mask 序列（PNG，每帧一个或按时间段）

职责：确定性生成 mask，不做清理。
为 action in [remove, replace, translate(需要 source_text_removal)] 的区域生成 mask。
人脸/主体保护：与保护区域冲突的 mask 做交集检查。
"""

import argparse
import os
import sys
from typing import List, Dict, Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    save_json, load_json, make_result, log,
    work_path, ensure_dir, require_python_package,
    normalize_persistent_starts, log_persistent_snaps,
)


def generate_mask_for_item(item: Dict, frame_w: int, frame_h: int,
                            protect_regions: List[Dict] = None) -> Optional[Dict]:
    """为单个 plan item 生成 mask 配置"""
    action = item.get("action")
    needs_removal = (
        action in ("remove", "replace") or
        (action == "translate" and item.get("source_text_removal", False))
    )

    if not needs_removal or action == "review":
        return None

    bbox = item.get("bbox", {})
    if not bbox or bbox.get("w", 0) <= 0 or bbox.get("h", 0) <= 0:
        return None

    # 边界裁剪
    x = max(0, int(bbox.get("x", 0)))
    y = max(0, int(bbox.get("y", 0)))
    w = min(int(bbox.get("w", 0)), frame_w - x)
    h = min(int(bbox.get("h", 0)), frame_h - y)

    if w <= 0 or h <= 0:
        return None

    # 与人脸/主体保护区域冲突检查
    if protect_regions:
        for pr in protect_regions:
            px, py = pr.get("x", 0), pr.get("y", 0)
            pw, ph = pr.get("w", 0), pr.get("h", 0)
            # 检查重叠
            overlap_x = max(0, min(x + w, px + pw) - max(x, px))
            overlap_y = max(0, min(y + h, py + ph) - max(y, py))
            overlap_area = overlap_x * overlap_y
            bbox_area = w * h
            if bbox_area > 0 and overlap_area / bbox_area > 0.5:
                log.warning(
                    f"Item {item.get('item_id')} bbox overlaps {overlap_area/bbox_area:.0%} "
                    f"with protected region, mask may be reduced"
                )

    return {
        "item_id": item.get("item_id"),
        "category": item.get("category"),
        "action": action,
        "bbox": {"x": x, "y": y, "w": w, "h": h},
        "start_time": item.get("start_time", 0),
        "end_time": item.get("end_time", 0),
        "removal_method": item.get("removal_method", ["inpaint"]),
        "replacement": item.get("replacement"),  # for replace action
        "confidence": item.get("confidence", 0.5),
    }


def render_mask_image(mask_config: Dict, frame_w: int, frame_h: int,
                       out_path: str, margin_px: int = 2) -> None:
    """渲染单张 mask 图片（白色区域=需要清理，黑色=保留）"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        log.error("OpenCV not installed")
        sys.exit(2)

    mask = np.zeros((frame_h, frame_w), dtype=np.uint8)
    bbox = mask_config["bbox"]
    x1 = max(0, bbox["x"] - margin_px)
    y1 = max(0, bbox["y"] - margin_px)
    x2 = min(frame_w, bbox["x"] + bbox["w"] + margin_px)
    y2 = min(frame_h, bbox["y"] + bbox["h"] + margin_px)
    mask[y1:y2, x1:x2] = 255

    # 轻微膨胀以覆盖文字边缘
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.dilate(mask, kernel, iterations=1)

    cv2.imwrite(out_path, mask)


def main():
    ap = argparse.ArgumentParser(description="Generate temporal masks from localization plan")
    ap.add_argument("--work-dir", required=True, help="Working directory")
    ap.add_argument("--plan", help="Path to localization-plan.json (default: work/localization-plan.json)")
    ap.add_argument("--probe", help="Path to probe.json (default: work/probe.json)")
    ap.add_argument("--margin-px", type=int, default=2, help="Mask margin in pixels")
    ap.add_argument("--render-images", action="store_true", help="Render mask PNG images")
    args = ap.parse_args()

    require_python_package("cv2", "pip install opencv-python")
    require_python_package("numpy", "pip install numpy")

    plan_path = args.plan or work_path(args.work_dir, "localization-plan.json")
    probe_path = args.probe or work_path(args.work_dir, "probe.json")

    plan = load_json(plan_path, {})
    probe = load_json(probe_path, {})

    items = plan.get("items", [])
    frame_w = probe.get("width", 1080)
    frame_h = probe.get("height", 1920)

    # ⚠️ mask 的 start/end 决定 inpaint 的真实时间窗 —— 常驻叠字若起点没归零，
    # 片头那几帧的原中文永远不会被抹掉（2026-09-27 事故，见 SKILL.md §4）。
    log_persistent_snaps(normalize_persistent_starts(plan), "generate_masks")

    if not items:
        log.warning("No items in localization plan")
        result = make_result(
            status="ok",
            confidence=1.0,
            masks=[],
            warnings=["No items to process"],
        )
        save_json(work_path(args.work_dir, "masks-index.json"), result)
        return

    log.info(f"Generating masks for {len(items)} plan items")

    masks_dir = ensure_dir(os.path.join(args.work_dir, "masks"))
    masks = []
    skipped = []

    for item in items:
        if item.get("action") == "review":
            skipped.append({"item_id": item.get("item_id"), "reason": "action=review, skipped"})
            continue

        mask_config = generate_mask_for_item(item, frame_w, frame_h)
        if mask_config:
            masks.append(mask_config)

            if args.render_images:
                mask_id = mask_config["item_id"]
                out_path = os.path.join(masks_dir, f"mask_{mask_id}.png")
                try:
                    render_mask_image(mask_config, frame_w, frame_h, out_path, args.margin_px)
                    mask_config["mask_image"] = out_path
                except Exception as e:
                    log.warning(f"Failed to render mask for {mask_id}: {e}")
        else:
            skipped.append({"item_id": item.get("item_id"), "reason": "no removal needed or invalid bbox"})

    # 保存 mask 索引
    result = make_result(
        status="ok",
        confidence=0.9,
        masks=masks,
        skipped=skipped,
        total_items=len(items),
        masks_generated=len(masks),
        frame_size={"width": frame_w, "height": frame_h},
    )

    save_json(os.path.join(masks_dir, "masks-index.json"), result)
    log.info(f"Mask generation complete: {len(masks)} masks, {len(skipped)} skipped")


if __name__ == "__main__":
    main()
