#!/usr/bin/env python3
"""
generate_cover.py - Profile 驱动的 TikTok 封面生成（流水线 Job 3）

输入：frames-index.json + video-understanding.json + cover-plan.json（Agent 选帧+文案）
输出：
  - output/cover.jpg（1080×1920）
  - work/cover-plan.json

**排版与绘制全部交给 `cover_engine.py`**（与手动/预览用的 `make_cover.py`
同一个引擎）。本脚本只负责流水线特有的三件事：

  1. 选帧（cover-plan 指定 > 自动评分）
  2. 按 localization-plan 清理封面帧上的中文/水印
  3. 组装 CoverSpec、落盘 cover.jpg + cover-plan.json

2026-09-27 之前这里有一套独立的 `render_cover()`：标题画在 `safe_top + 50`、
品牌画在 `safe_bottom`，两者都落在安全区（中心 1080×1080）之外 —— 也就是说
流水线出的封面在主页网格里标题和 logo 全被裁掉，而手调的封面是对的。
两套渲染器是那个 bug 的根源，现在合成一套。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    load_json, load_profile, log, output_path, require_python_package,
    save_json, work_path,
)
from cover_engine import (  # noqa: E402
    CoverSpec, cover_crop_resize, format_report, letter_cover,
    map_boxes_to_canvas, render_cover,
)

DEFAULT_HEADLINE = "MUST WATCH"


# --------------------------------------------------------------------------
# Frame selection
# --------------------------------------------------------------------------
def detect_faces(image_path: str) -> List[Dict]:
    """Haar frontal-face boxes in SOURCE pixel space."""
    try:
        import cv2
    except ImportError:
        return []

    img = cv2.imread(image_path)
    if img is None:
        return []

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    face_cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                          minSize=(30, 30))
    return [{"x": int(x), "y": int(y), "w": int(fw), "h": int(fh)}
            for x, y, fw, fh in faces]


def select_best_frame(frames: List[Dict], video_understanding: Dict,
                      face_protection: bool = True) -> Optional[Dict]:
    """Pick the best cover frame.

    Primary scorer is ``pick_cover_frame.score_frame`` -- face area minus crowd
    clutter (SKILL.md 13.4.2 #6). A wide court shot is all crowd and no amount
    of post-processing rescues it, which is why the old
    "sharpness*0.5 + bonus" ranking produced covers the user called 花.
    The legacy scorer stays as a fallback so a missing cv2/scene folder does
    not break the job, but it logs a degradation.
    """
    if not frames:
        return None
    highlight_times = (video_understanding or {}).get("highlight_timestamps", [])

    try:
        from pick_cover_frame import score_frame
    except Exception as exc:                                    # pragma: no cover
        score_frame = None
        log.warning(f"pick_cover_frame unavailable ({exc}); falling back to the "
                    "legacy sharpness ranking")

    if score_frame is not None:
        scored = []
        for frame in frames:
            path = frame.get("path")
            if not path or not os.path.exists(path):
                continue
            r = score_frame(path)
            if not r:
                continue
            ts = frame.get("timestamp_sec", 0) or 0
            bonus = 25.0 if any(abs(ts - ht) < 3 for ht in highlight_times) else 0.0
            scored.append((r["score"] + bonus, frame, r))
        if scored:
            scored.sort(key=lambda x: -x[0])
            best, r = scored[0][1], scored[0][2]
            best["sharpness"] = round(r["lap"] / 1000.0, 4)
            best["cover_score"] = round(scored[0][0], 4)
            best["face_area"] = round(r["face"], 5)
            best["clutter"] = round(r["clutter"], 5)
            if face_protection and r["face"] < 0.01:
                log.warning("best frame has almost no face area -- it may be a "
                            "wide shot; eyeball it before delivering "
                            "(SKILL.md 13.4.2 #6)")
            return best

    # ------------------------------------------------------ legacy fallback --
    scored = []
    for frame in frames:
        path = frame.get("path")
        if not path or not os.path.exists(path):
            continue
        sharpness = compute_sharpness(path)
        ts = frame.get("timestamp_sec", 0) or 0
        highlight_bonus = 0.3 if any(abs(ts - ht) < 3 for ht in highlight_times) else 0
        face_bonus = 0.2 if (face_protection and detect_faces(path)) else 0
        scored.append((sharpness * 0.5 + highlight_bonus + face_bonus, frame,
                       sharpness))
    if not scored:
        return frames[0]
    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0][1]
    best["sharpness"] = round(scored[0][2], 4)
    return best


def compute_sharpness(image_path: str) -> float:
    """Laplacian variance / 1000 (legacy scorer only)."""
    try:
        import cv2
    except ImportError:
        return 0.5
    img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return 0.0
    return float(cv2.Laplacian(img, cv2.CV_64F).var()) / 1000.0


# --------------------------------------------------------------------------
# Frame cleaning (pipeline-specific: stays out of the engine)
# --------------------------------------------------------------------------
def clean_frame_with_plan(img, plan_items, blur_radius=25, blur_alpha=210,
                          inpaint_radius=5):
    """按 localization-plan 清理封面帧上的中文/水印（与视频清理一致）

    规则（与 SKILL.md 中文去除方式一致）：
      - 大区域（>100px 高，如顶部标题）：高斯模糊 + 半透明黑色遮罩
      - 小区域（<=100px 高，如水印/字幕）：OpenCV inpaint（露出原画面）
    封面帧必须与视频一致地干净，禁止把中文/水印带入封面。
    注意：封面会放大渲染（720p→1080x1920），模糊必须比视频清理更重
    （默认 radius=25 / alpha=210），否则放大后中文仍可辨认。

    Returns:
        (清理后的 PIL Image, 清理项计数)
    """
    from PIL import Image
    import numpy as np
    try:
        import cv2
    except ImportError:
        return img, 0

    items = [i for i in (plan_items or [])
             if i.get("action") in ("remove", "translate", "replace")
             and i.get("bbox")]
    if not items:
        return img, 0

    frame = np.array(img.convert("RGB"))
    h, w = frame.shape[:2]
    cleaned_count = 0

    for item in items:
        bbox = item.get("bbox", {})
        x = int(bbox.get("x", 0)); y = int(bbox.get("y", 0))
        bw = int(bbox.get("w", 0)); bh = int(bbox.get("h", 0))
        if bw <= 0 or bh <= 0:
            continue
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(w, x + bw), min(h, y + bh)
        if x1 <= x0 or y1 <= y0:
            continue

        if (y1 - y0) > 100:
            # 大区域：高斯模糊 + 半透明黑遮罩（能看到画面轮廓，非纯黑条）
            roi = frame[y0:y1, x0:x1].copy()
            k = (blur_radius * 2 + 1, blur_radius * 2 + 1)
            roi = cv2.GaussianBlur(roi, k, 0)
            frame[y0:y1, x0:x1] = roi
            frame[y0:y1, x0:x1] = (
                frame[y0:y1, x0:x1] * (1 - blur_alpha / 255.0)).astype(np.uint8)
        else:
            # 小区域：OpenCV inpaint（露出原画面）
            mask = np.zeros((h, w), dtype=np.uint8)
            mask[y0:y1, x0:x1] = 255
            frame = cv2.inpaint(frame, mask, inpaint_radius, cv2.INPAINT_TELEA)

        cleaned_count += 1

    return Image.fromarray(frame), cleaned_count


# --------------------------------------------------------------------------
# Spec assembly
# --------------------------------------------------------------------------
def build_spec(headline: str, style: Dict[str, Any], plan: Dict[str, Any],
               args) -> CoverSpec:
    """Map profile + cover-plan onto the engine's CoverSpec.

    cover-plan.json carries the Agent's copy; the field names are documented in
    ``jobs/generate-cover.job.md`` and ``SKILL.md`` 13.4.2:
    ``headline`` / ``sub`` / ``stat`` / ``tagline``.
    """
    canvas = style.get("canvas") or {}
    return CoverSpec(
        headline=headline,
        sub=args.sub or plan.get("sub") or plan.get("subheadline") or "",
        stat=args.stat or plan.get("stat") or plan.get("stat_line") or "",
        tagline=args.tagline or plan.get("tagline") or "",
        logo=style.get("brand_text") or "",
        style=style,
        headline_max_width=float(style.get("headline_max_width") or 0.96),
        logo_style=style.get("logo_style") or "sticker",
        layout=style.get("layout") or "safe",
        canvas_w=int(canvas.get("width") or 1080),
        canvas_h=int(canvas.get("height") or 1920),
    )


def write_plan(plan_path: str, payload: Dict[str, Any]) -> None:
    save_json(plan_path, payload)


def main():
    ap = argparse.ArgumentParser(description="Generate TikTok cover image")
    ap.add_argument("--work-dir", required=True, help="Working directory")
    ap.add_argument("--output-dir", required=True, help="Output directory")
    ap.add_argument("--profile", default="generic",
                    help="Profile name (nba/sports/generic)")
    ap.add_argument("--frames-index", help="Path to frames-index.json")
    ap.add_argument("--video-understanding", help="Path to video-understanding.json")
    ap.add_argument("--cover-plan", help="Path to cover-plan.json (Agent-selected frame + headline)")
    ap.add_argument("--headline", help="Cover headline text (overrides plan)")
    ap.add_argument("--sub", help="Grey sub-line (overrides plan)")
    ap.add_argument("--stat", help="Bottom display row, e.g. '48 POINTS' (overrides plan)")
    ap.add_argument("--tagline", help="White caption line (overrides plan)")
    ap.add_argument("--cover-image", help="Use user-provided cover image directly (skips frame selection and rendering)")
    ap.add_argument("--no-resize", action="store_true",
                    help="When using --cover-image, keep original size (do not resize to 1080x1920)")
    ap.add_argument("--no-text", action="store_true",
                    help="When using --cover-image, do not overlay headline text on the cover")
    ap.add_argument("--bottom-text", help="Bottom small text on cover (e.g. 'GOAT HIGHLIGHTS'), only with --cover-image")
    args = ap.parse_args()

    require_python_package("PIL", "pip install pillow")
    os.makedirs(args.output_dir, exist_ok=True)

    profile = load_profile(args.profile)
    cover_style = profile.get("cover_style", {}) or {}
    cover_path = output_path(args.output_dir, "cover.jpg")
    plan_path = args.cover_plan or work_path(args.work_dir, "cover-plan.json")

    # ---------------------------------------------------- user-provided art --
    if args.cover_image:
        from PIL import Image

        if not os.path.exists(args.cover_image):
            log.error(f"Cover image not found: {args.cover_image}")
            sys.exit(1)
        log.info(f"Using user-provided cover: {args.cover_image}")

        plan = load_json(plan_path, {}) or {}
        headline = args.headline or plan.get("headline") or DEFAULT_HEADLINE
        img = Image.open(args.cover_image).convert("RGB")
        original_size = {"width": img.width, "height": img.height}
        resize = not args.no_resize

        if args.no_text:
            # lettering off: just fit (or keep) the user's art
            out = cover_crop_resize(
                img,
                int((cover_style.get("canvas") or {}).get("width") or 1080),
                int((cover_style.get("canvas") or {}).get("height") or 1920),
            ) if resize else img
            out.save(cover_path, "JPEG", quality=95)
            render_info = {
                "canvas": {"width": out.width, "height": out.height},
                "source": "user_provided",
                "text_regions": [], "brand_region": None,
                "brand_text_rendered": False, "brand_text": None,
                "faces_detected": 0, "face_areas": [],
                "frame_cleaned_regions": 0,
                "safe_text_zone": {}, "frame_cleaned": False,
            }
            log.info(f"Cover generated (user-provided, no text): {cover_path}")
        else:
            spec = build_spec(headline, cover_style, plan, args)
            spec.resize_to_canvas = resize
            if args.bottom_text and not spec.tagline:
                spec.tagline = args.bottom_text
            spec.logo = "" if not cover_style.get("brand_text") else spec.logo
            result = letter_cover(img, spec)
            result.image.save(cover_path, "JPEG", quality=95)
            render_info = result.meta
            render_info["source"] = "user_provided"
            render_info["faces_detected"] = 0
            render_info["face_areas"] = []
            render_info["frame_cleaned_regions"] = 0
            print(format_report(spec, result.meta, cover_path, args.profile))
            log.info(f"Cover generated (user-provided): {cover_path}")

        write_plan(plan_path, {
            "profile": args.profile,
            "profile_cover_style": cover_style,
            "source": "user_provided",
            "cover_image": args.cover_image,
            "canvas": render_info["canvas"],
            "original_size": original_size,
            "headline": headline,
            "bottom_text": args.bottom_text,
            "text_rendered": not args.no_text,
            "render": render_info,
            "output_path": cover_path,
        })
        return

    # ------------------------------------------------- auto cover (Job 3) ---
    require_python_package("cv2", "pip install opencv-python-headless")

    frames = load_json(args.frames_index
                       or work_path(args.work_dir, "frames-index.json"), []) or []
    understanding = load_json(
        args.video_understanding
        or work_path(args.work_dir, "video-understanding.json"), {}) or {}
    plan = load_json(plan_path, {}) or {}

    selected_frame = plan.get("selected_frame")
    if not selected_frame:
        log.info("Selecting best frame automatically...")
        selected_frame = select_best_frame(
            frames, understanding, cover_style.get("face_protection", True))
        if selected_frame:
            log.info(f"Selected frame: {selected_frame.get('frame_id')} @ "
                     f"{selected_frame.get('timestamp_sec')}s "
                     f"(score={selected_frame.get('cover_score', '-')}, "
                     f"sharpness={selected_frame.get('sharpness')})")
    if not selected_frame or not os.path.exists(selected_frame.get("path", "")):
        log.error("No valid frame selected for cover")
        sys.exit(1)

    headline = (args.headline or plan.get("headline")
                or understanding.get("cover_headline") or DEFAULT_HEADLINE)

    plan_items = (load_json(work_path(args.work_dir, "localization-plan.json"),
                            {}) or {}).get("items", [])

    from PIL import Image
    img = Image.open(selected_frame["path"]).convert("RGB")
    src_size = {"width": img.width, "height": img.height}
    cleaned_count = 0
    if cover_style.get("clean_frame_enabled", True):
        img, cleaned_count = clean_frame_with_plan(img, plan_items)

    spec = build_spec(headline, cover_style, plan, args)
    result = render_cover(img, spec)
    result.image.save(cover_path, "JPEG", quality=95)
    print(format_report(spec, result.meta, cover_path, args.profile))

    # faces are detected in SOURCE space, then mapped through the same
    # transform the engine applied to the picture, so the QA occlusion check
    # compares like with like
    face_areas = map_boxes_to_canvas(
        detect_faces(selected_frame["path"]), result.meta["picture_src"])

    render_info = dict(result.meta)
    render_info["faces_detected"] = len(face_areas)
    render_info["face_areas"] = face_areas
    render_info["frame_cleaned_regions"] = cleaned_count
    render_info["lines"] = result.meta["headline_lines"]
    # font_used keeps the historical meaning (first name in the chain);
    # font_resolved is the file actually used -- reporting the first chain name
    # as "used" is how "Anton" got logged while Impact was drawing the glyphs.
    chain = cover_style.get("font_chain") or ["default"]
    render_info["font_used"] = chain[0]
    render_info["font_resolved"] = result.meta["fonts"]["headline"]

    write_plan(plan_path, {
        "profile": args.profile,
        "profile_cover_style": cover_style,
        "canvas": result.meta["canvas"],
        "selected_frame": {
            "frame_id": selected_frame.get("frame_id"),
            "timestamp_sec": selected_frame.get("timestamp_sec"),
            "path": selected_frame.get("path"),
            "sharpness": selected_frame.get("sharpness", 0),
            "cover_score": selected_frame.get("cover_score"),
            "face_area": selected_frame.get("face_area"),
            "clutter": selected_frame.get("clutter"),
        },
        "source_size": src_size,
        "headline": headline,
        "sub": spec.sub,
        "stat": spec.stat,
        "tagline": spec.tagline,
        "render": render_info,
        "output_path": cover_path,
    })

    log.info(f"Cover generated: {cover_path}")
    log.info(f"  Headline: '{headline}', Faces: {len(face_areas)}, "
             f"cleaned regions: {cleaned_count}")


if __name__ == "__main__":
    main()
