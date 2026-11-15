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


def _video_path_from_config(work_dir: str) -> Optional[str]:
    """pipeline-config.json 里的源视频路径（拿不到就返回 None）。"""
    cfg = load_json(os.path.join(work_dir, "pipeline-config.json"), {}) or {}
    p = cfg.get("input")
    return p if p and os.path.exists(p) else None


def _static_overlay_std(video: str, t0: float, t1: float):
    """时间窗内逐像素时间标准差（小 = 静止叠加层，即字幕底板）。

    底板（浅蓝/红色胶囊、飘带）是烧死在画面上的静态贴片：整片位置不变，
    只有上面的字在换。背景即使虚化，也总有运动。所以「时间标准差低」是
    把底板和背景分开的可靠信号。
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    span = max(t1 - t0, 0.4)
    ts = [t0 + span * f for f in (0.08, 0.26, 0.44, 0.62, 0.80, 0.94)]
    frames = []
    for t in ts:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
        ok, fr = cap.read()
        if ok and fr is not None:
            frames.append(fr.astype("int16"))
    cap.release()
    if len(frames) < 3:
        return None
    stack = np.stack(frames)
    return stack.std(axis=0).max(axis=2)


def expand_bbox_to_plate(video: str, item: Dict, frame_w: int, frame_h: int,
                         max_grow: int = 90, std_thr: float = 7.0,
                         max_area_ratio: float = 3.6) -> Optional[Dict]:
    """把只框住文字的 bbox 扩到底色贴片的实际边界。

    2026-10-30 事故（NBA精彩超人瞬间 xWT127）：源片的名字字幕是「浅蓝色胶囊
    底板 + 白色中文」，OCR 的 bbox 只包住字形。mask 只盖字 → inpaint 把字换成
    底板蓝，底板整块留在画面上，观众看到的就是一个个「浅蓝色遮罩」；同时
    胶囊两端的播放三角、编号也一并残留。
    修复方式：以时间静止性找出底板连通域，用它替换 bbox。
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    bbox = item.get("bbox") or {}
    x, y = int(bbox.get("x", 0)), int(bbox.get("y", 0))
    w, h = int(bbox.get("w", 0)), int(bbox.get("h", 0))
    if w <= 0 or h <= 0:
        return None
    t0 = float(item.get("start_time", 0) or 0)
    t1 = float(item.get("end_time", t0 + 1) or (t0 + 1))
    std = _static_overlay_std(video, t0, t1)
    if std is None:
        return None

    rx0 = max(0, x - max_grow)
    ry0 = max(0, y - max_grow)
    rx1 = min(frame_w, x + w + max_grow)
    ry1 = min(frame_h, y + h + max_grow)
    sub = std[ry0:ry1, rx0:rx1]
    if sub.size == 0:
        return None

    static = (sub < std_thr).astype(np.uint8)
    static = cv2.morphologyEx(static, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    num, lab, stats, _ = cv2.connectedComponentsWithStats(static, 8)
    cx = min(max(x + w // 2 - rx0, 0), lab.shape[1] - 1)
    cy = min(max(y + h // 2 - ry0, 0), lab.shape[0] - 1)
    lb = lab[cy, cx]
    if lb == 0:
        # 中点可能正好落在会动的雪花/字缝上，换 bbox 四角再试
        for (px, py) in ((x - rx0, y - ry0), (x + w - 1 - rx0, y - ry0),
                         (x - rx0, y + h - 1 - ry0), (x + w - 1 - rx0, y + h - 1 - ry0)):
            px = min(max(px, 0), lab.shape[1] - 1)
            py = min(max(py, 0), lab.shape[0] - 1)
            if lab[py, px] != 0:
                lb = lab[py, px]
                break
    if lb == 0:
        return None

    ys, xs = np.nonzero(lab == lb)
    nx0, nx1 = int(xs.min()) + rx0, int(xs.max()) + rx0
    ny0, ny1 = int(ys.min()) + ry0, int(ys.max()) + ry0
    nw, nh = nx1 - nx0 + 1, ny1 - ny0 + 1
    if nw <= w and nh <= h:
        return None                      # 没有底板，保持原 bbox
    if nw * nh > max_area_ratio * w * h:
        return None                      # 连通域失控（整帧静止），不扩
    # 保守约束：底板只会略大于文字框。放太开就宁可不动，交给视觉复查
    # （plan.extra_mask_regions 手动补），否则会把大块画面 inpaint 成糊斑。
    if nh > 1.8 * h or nw > 2.4 * w:
        return None
    if not (nx0 <= x + 6 and ny0 <= y + 6 and nx1 >= x + w - 6 and ny1 >= y + h - 6):
        return None                      # 扩出来的框没包住文字，说明抓错东西了
    return {"x": nx0, "y": ny0, "w": nw, "h": nh}


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
    ap.add_argument("--video", help="Source video (default: pipeline-config.json input)")
    ap.add_argument("--expand-plates", dest="expand_plates", action="store_true",
                    default=True,
                    help="Grow text bboxes to their coloured caption plate (default on)")
    ap.add_argument("--no-expand-plates", dest="expand_plates", action="store_false")
    args = ap.parse_args()

    require_python_package("cv2", "pip install opencv-python")
    require_python_package("numpy", "pip install numpy")

    plan_path = args.plan or work_path(args.work_dir, "localization-plan.json")
    probe_path = args.probe or work_path(args.work_dir, "probe.json")

    plan = load_json(plan_path, {})
    probe = load_json(probe_path, {})

    items = list(plan.get("items", []))

    # Agent 视检补充的图形残留区域（extra_mask_regions）：
    # 静态贴片上的非文字图形 —— 箭头、横幅色块、台标、飘带、logo —— OCR 检不出，
    # 文字 mask 只盖住字，图形会整块残留（2026-10-29 麦迪十佳球：橙色箭头横幅
    # + YouTube 标黄飘带全程残留）。Agent 看首帧/中部帧后在 plan 里补区域，
    # 这里合成为 remove item，与文字 mask 同一管线处理。
    for i, r in enumerate(plan.get("extra_mask_regions", [])):
        items.append({
            "item_id": r.get("item_id") or f"XG_{i:02d}",
            "category": "graphic",
            "action": "remove",
            "confidence": 1.0,
            "bbox": {"x": int(r.get("x", 0)), "y": int(r.get("y", 0)),
                     "w": int(r.get("w", 0)), "h": int(r.get("h", 0))},
            "start_time": r.get("start_time", 0),
            "end_time": r.get("end_time", 0),
            "source_text": r.get("label", ""),
            "source_text_removal": True,
            "removal_method": ["inpaint"],
        })
    if plan.get("extra_mask_regions"):
        log.info(f"  + {len(plan['extra_mask_regions'])} extra graphic regions from plan")

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
        masks_dir = ensure_dir(os.path.join(args.work_dir, "masks"))
        save_json(os.path.join(masks_dir, "masks-index.json"), result)
        return

    log.info(f"Generating masks for {len(items)} plan items")

    # 字幕底板扩边：OCR bbox 只框字，底板会整块残留（见 expand_bbox_to_plate）。
    video = args.video or _video_path_from_config(args.work_dir)
    if args.expand_plates and video:
        grown = 0
        for item in items:
            if not item.get("bbox"):
                continue
            expanded = expand_bbox_to_plate(video, item, frame_w, frame_h)
            if expanded:
                log.info(f"  {item.get('item_id')}: bbox "
                         f"{item['bbox']} -> plate {expanded}")
                item["bbox"] = expanded
                item["bbox_expanded_to_plate"] = True
                grown += 1
        log.info(f"  plate expansion: {grown}/{len(items)} items")
    elif args.expand_plates:
        log.warning("  plate expansion skipped: source video not found")

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
