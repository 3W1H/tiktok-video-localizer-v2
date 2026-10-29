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
def _haar_faces(gray, scales, cascades, min_size_px, min_neighbors,
                mirror=False):
    """Run Haar cascades over several image scales; return SOURCE-space boxes.

    ``scales`` are factors applied to the ORIGINAL image before detection --
    a smaller working image is what makes soft / motion-blurred / low-key
    faces detectable (downscaling acts as a low-pass filter and the fixed
    ``minSize`` window then spans proportionally more of the face).
    Detections are mapped back to source pixels and de-duplicated.

    ``mirror`` additionally runs each cascade on the horizontally flipped
    image -- only useful for the profile cascade, which fires on one turn
    direction only. Kept off by default because it doubles the false-positive
    rate on the frontal cascade.
    """
    import cv2

    h, w = gray.shape[:2]
    out: List[Dict] = []
    for s in scales:
        base = gray if s == 1.0 else cv2.resize(
            gray, (max(1, int(round(w * s))), max(1, int(round(h * s)))),
            interpolation=cv2.INTER_AREA)
        min_side = max(8, int(round(min_size_px * s / max(scales))))
        for cas in cascades:
            for flip in ((False, True) if mirror else (False,)):
                work = cv2.flip(base, 1) if flip else base
                try:
                    found = cas.detectMultiScale(work, scaleFactor=1.1,
                                                 minNeighbors=min_neighbors,
                                                 minSize=(min_side, min_side))
                except Exception:
                    continue
                ww = work.shape[1]
                for x, y, fw, fh in found:
                    if flip:
                        x = ww - x - fw
                    out.append({"x": int(round(x / s)), "y": int(round(y / s)),
                                "w": int(round(fw / s)),
                                "h": int(round(fh / s))})
    # de-duplicate: drop any box whose centre already sits inside a kept box
    kept: List[Dict] = []
    for b in sorted(out, key=lambda z: -(z["w"] * z["h"])):
        cx, cy = b["x"] + b["w"] / 2.0, b["y"] + b["h"] / 2.0
        if any(k["x"] <= cx <= k["x"] + k["w"] and k["y"] <= cy <= k["y"] + k["h"]
               for k in kept):
            continue
        kept.append(b)
    return kept


def detect_content_band(img, extra_frames=None, min_frac: float = 0.30):
    """检测源帧的实际内容区（picture band），返回 (top, bottom) 或 None。

    两类源视频版式会把非画面区域裁进封面（2026-10-29 国王/98全明星实录）：
      1. 硬 letterbox：画面只占中部，上下纯黑条 —— 标题压在黑条上
         （国王/麦迪类）；
      2. 模糊背景版式：转载视频自带「模糊背景+中部清晰横条」—— logo 区
         落在底部暗模糊带上，读作一个大黑框（98全明星类）。这类版式的
         模糊背景是**当前画面实时模糊**，跟着内容一起变（时间方差失效）；
         模糊带上还烧着大号描边字幕，降采样后笔画变宽变强，单帧的行纹理
         判据把字幕行误判成画面（98 实录：内容带顶部多包了 200px 模糊带）。

    两级检测，先 1 后 2（1 无结论才用 2）：

    Pass 1（硬黑条，最可靠）：行最大亮度 < 12 即黑条行（纯黑条连一个亮
    像素都没有；画面行几乎必有高光）。取最大非黑连续段。烧录横幅文字
    虽然亮，但其段远短于画面段，最大段自动胜出。

    Pass 2（模糊带版式，多帧行级中位数）：版式全片固定 —— 画面带行帧帧
    有纹理、模糊带行帧帧平滑，只有字幕在闪现/更换。对 work 目录均匀抽样
    ≤12 帧逐行算「中等纹理占比」（4x 降采样，|lap|∈(8,30] 的列占比），
    取**跨帧中位数**：字幕尖峰被中位数滤掉。阈值 max(0.10, 0.55*p90)，
    桥接 ≤4%H 缺口（画面内的白墙/天空平滑段）、剔 <12%H 小段（零散字幕
    岛）、取最大段。

    逐侧平滑门槛（防过度裁切）：候选带上/下方的排除区必须以平滑行为主
    （中位数 < 0.05 的行 ≥55%）才允许裁那一侧 —— 真模糊带/黑条几乎全平滑，
    而「全幅画面+底部字幕」类视频（森林狼实录）排除区大半是真实画面，
    门槛不过就不裁该侧。两侧都不过 → None。

    段高不足 min_frac 或本就是满幅 → None（不裁）。找到带后上下各内收 1%
    （避开画面↔模糊带的渐变过渡行）。可在帧清理之前调用（中位数天然
    抗单帧清理痕迹）。
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    w, h = img.size

    # --- Pass 1: 硬 letterbox（行最大亮度） ---
    g = np.asarray(img.convert("L"), dtype=np.float32)
    rowmax = g.max(axis=1)
    content = rowmax >= 12.0
    idx = np.where(content)[0]
    if len(idx):
        runs, s, p = [], idx[0], idx[0]
        for i in idx[1:]:
            if i - p <= 2:
                p = i
            else:
                runs.append((s, p))
                s = p = i
        runs.append((s, p))
        t, b = max(runs, key=lambda r: r[1] - r[0])
        if b - t >= h * min_frac and not (t <= 2 and b >= h - 2):
            return (int(t), int(b + 1))

    # --- Pass 2: 模糊背景版式（多帧行级中位数） ---
    paths = [p for p in (extra_frames or []) if p][:11]
    frames = [img]
    for p in paths:
        try:
            frames.append(Image.open(p))
        except Exception:
            continue
    H4 = max(20, h // 4)
    mids = []
    for fr in frames:
        small = fr.convert("L").resize((max(1, w // 4), H4), Image.BILINEAR)
        gs = np.asarray(small, dtype=np.float32)
        lap = np.abs(cv2.Laplacian(gs, cv2.CV_32F))
        mids.append(((lap > 8) & (lap <= 30)).mean(axis=1))
    med = np.median(np.stack(mids), axis=0)
    med = np.convolve(med, np.ones(5) / 5, mode="same")
    thresh = max(0.10, 0.55 * float(np.percentile(med, 90)))
    content = med > thresh
    idx = np.where(content)[0]
    if len(idx) == 0:
        return None
    gap_max = max(2, int(H4 * 0.04))
    runs, s, p = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - p <= gap_max:
            p = i
        else:
            runs.append((s, p))
            s = p = i
    runs.append((s, p))
    runs = [r for r in runs if r[1] - r[0] >= int(H4 * 0.12)]
    if not runs:
        return None
    t4, b4 = max(runs, key=lambda r: r[1] - r[0])
    t, b = int(t4 * h / H4), int((b4 + 1) * h / H4)

    # 逐侧平滑门槛：排除区必须以平滑行为主才裁那一侧
    smooth = med < 0.05

    def bandlike(a4, z4):
        seg = smooth[a4:z4]
        return seg.size == 0 or seg.mean() >= 0.55

    if not bandlike(0, t4):
        t = 0
    if not bandlike(b4 + 1, H4):
        b = h
    if b - t < h * min_frac or (t <= int(h * 0.01) and b >= int(h * 0.99)):
        return None
    # 内收 1% 避开过渡行
    trim = max(2, int(h * 0.01))
    return (min(t + trim, b - 1), max(b - trim, t + 1))


def detect_faces(image_path: str, subject_frac: float = 0.30) -> List[Dict]:
    """Haar face boxes in SOURCE pixel space.

    Two passes, and the second one only runs when the first finds *nothing*:

    1. the historical frontal pass at full resolution -- unchanged, so every
       frame the pipeline already handled behaves exactly as before;
    2. a rescue pass at HALF resolution, accepted only for faces at least
       ``subject_frac`` of the image's shorter side.

    Why the rescue exists (2026-10-15): the full-resolution pass silently
    missed the large, dark, three-quarter-profile face on the Kobe cover
    (728x720 source, ~254px face). ``qa_video.check_cover`` therefore reported
    ``face_occluded=False`` on a cover whose headline really did sit on the
    man's chin -- a false PASS, the worst kind. Re-running at half scale finds
    it (downscaling is a low-pass filter; the fixed ``minSize`` window then
    spans proportionally more of the face).

    Why the ``subject_frac`` floor: at half scale the same cascade also starts
    matching people's TORSOS. Measured over the ten covers of that batch, the
    spurious boxes reach 24% of the shorter side while the Kobe face is 35%,
    so a 30% floor separates them with margin. Without the floor this change
    flipped 8 of 10 covers to a bogus "text over face" warning.

    Known limitation: Haar still misses very dark or extreme-profile faces.
    Treat the occlusion result as a lower bound and keep spot-checking rendered
    covers by eye -- see SKILL.md.
    """
    try:
        import cv2
    except ImportError:
        return []

    img = cv2.imread(image_path)
    if img is None:
        return []

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cascades = []
    for name in ("haarcascade_frontalface_default.xml",):
        path = cv2.data.haarcascades + name
        if os.path.exists(path):
            cas = cv2.CascadeClassifier(path)
            if not cas.empty():
                cascades.append(cas)
    if not cascades:
        return []

    full = _haar_faces(gray, scales=[1.0], cascades=cascades,
                       min_size_px=30, min_neighbors=5)
    if full:
        return full

    h, w = gray.shape[:2]
    floor_px = int(round(min(h, w) * subject_frac))
    rescue = _haar_faces(gray, scales=[0.5], cascades=cascades,
                         min_size_px=30, min_neighbors=5)
    return [b for b in rescue
            if min(b["w"], b["h"]) >= floor_px and b["w"] < 4 * w]


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
            pick = scored[0]
            # 面脸优先（2026-10-28）：score 公式虽以 face 主导，但全片都无正面脸时
            # top1 依然没脸 —— 用户点名"封面怎么都没有脸了"。face_protection 开启时
            # 若存在有脸的帧，即使分数低也优先选有脸的。
            if face_protection and pick[2]["face"] < 0.015:
                with_face = [e for e in scored if e[2]["face"] >= 0.015]
                if with_face:
                    log.info("top-scored frame has no face; preferring best frame "
                             f"with a face (face_area={with_face[0][2]['face']:.3f}, "
                             f"t={with_face[0][1].get('timestamp_sec')}s, "
                             f"score={with_face[0][0]:.2f} vs {pick[0]:.2f})")
                    pick = with_face[0]
            best, r = pick[1], pick[2]
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
        # 兜底也要挑存在的文件：frames[0] 可能指向已改名的旧目录
        for fr in frames:
            if os.path.exists(fr.get("path", "")):
                return fr
        return None
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
def select_cleanable_items(plan_items):
    """plan 里真正需要在画面上擦除的项（有 bbox 且动作会动画面）。"""
    return [i for i in (plan_items or [])
            if i.get("action") in ("remove", "translate", "replace")
            and i.get("bbox")]


def plan_items_at(plan_items, frame_time, eps=1e-6):
    """只保留 ``frame_time`` 那一刻**真的在屏幕上**的项。

    封面是**单帧快照**，而 localization-plan 覆盖整条片子：不加时间过滤就会把
    全片 65 个区域统统擦一遍，其中"没出现在这一帧上"的区域等于凭空挖掉画面。

    2026-09-29 实例（03 湖人传奇羁绊）：plan 里唯一的 ``caption_overlay``
    remove 项是 0.6–3.4s 的中屏中文关键词弹窗，bbox h=128 ⇒ 走「大区域」分支
    （高斯模糊 + 82% 压暗）；封面帧取在 t=236.85s，那一刻屏上什么都没有，
    结果在科比脸上压出一块 361x191 的黑方块。同 01/02 没事，因为它们的
    plan 里没有 h>100 的项。

    ``start_time``/``end_time`` 缺失的项视为常驻（保守：宁可擦，不可留中文）。
    ``frame_time is None`` 时退化为旧行为（全部清理），供单独调用者使用。
    """
    if frame_time is None:
        return list(plan_items or [])
    out = []
    for it in plan_items or []:
        st, en = it.get("start_time"), it.get("end_time")
        if st is None or en is None:
            out.append(it)
            continue
        if float(st) - eps <= float(frame_time) <= float(en) + eps:
            out.append(it)
    return out


def _frame_ocr_boxes(frame_rgb):
    """全帧 RapidOCR 检出，返回 [(x0, y0, x1, y1, text)]（源像素坐标）。"""
    try:
        import cv2
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return []
    res, _ = RapidOCR()(cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR))
    out = []
    for box, text, conf in (res or []):
        if conf < 0.4:
            continue
        xs = [int(round(p[0])) for p in box]
        ys = [int(round(p[1])) for p in box]
        out.append((min(xs), min(ys), max(xs), max(ys), str(text)))
    return out


def clean_frame_with_plan(img, plan_items, frame_time=None, blur_radius=25,
                          blur_alpha=210, inpaint_radius=5):
    """按 localization-plan 清理封面帧上的中文/水印（与视频清理一致）

    规则（与 SKILL.md 中文去除方式一致）：
      - 大区域（>100px 高，如顶部标题）：高斯模糊 + 半透明黑色遮罩
      - 小区域（<=100px 高，如水印/字幕）：OpenCV inpaint（露出原画面）
    封面帧必须与视频一致地干净，禁止把中文/水印带入封面。
    注意：封面会放大渲染（720p→1080x1920），模糊必须比视频清理更重
    （默认 radius=25 / alpha=210），否则放大后中文仍可辨认。

    ``frame_time`` 传入封面帧时间戳后，只有该时刻在屏的项会被清理
    （见 ``plan_items_at``）——否则会把全片的区域都擦到这一帧上。

    ⭐ bbox↔OCR 相交裁剪（2026-10-29 98全明星实录）：translate 项的 bbox
    经常把**不同时刻出现的两条字幕**框在一起（如 A2 = "他是MVP…" +
    "致敬科比"，bbox 高 217px 横跨画面区和模糊带），而封面帧上只有一行
    在屏 —— 旧逻辑整块模糊+82% 黑遮罩，在真实画面上糊出一个大黑框
    （用户："logo 还有黑框"）。现在先 OCR 全帧，bbox 与实际检出文字
    **相交才清理、只清相交部分**；OCR 没命中的项（图形/水印类）才退回
    整块处理。

    Returns:
        (清理后的 PIL Image, 清理项计数)
    """
    from PIL import Image
    import numpy as np
    try:
        import cv2
    except ImportError:
        return img, 0

    items = select_cleanable_items(plan_items_at(plan_items, frame_time))
    if not items:
        return img, 0

    frame = np.array(img.convert("RGB"))
    h, w = frame.shape[:2]
    ocr_boxes = _frame_ocr_boxes(frame)
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

        # OCR 相交裁剪：只清 bbox 内实际检出的文字
        hits = []
        for (ox0, oy0, ox1, oy1, _t) in ocr_boxes:
            ix0, iy0 = max(x0, ox0), max(y0, oy0)
            ix1, iy1 = min(x1, ox1), min(y1, oy1)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            oarea = max(1, (ox1 - ox0) * (oy1 - oy0))
            if (ix1 - ix0) * (iy1 - iy0) / oarea >= 0.30:
                hits.append((ix0, iy0, ix1, iy1))

        if hits:
            mask = np.zeros((h, w), dtype=np.uint8)
            for (hx0, hy0, hx1, hy1) in hits:
                m = max(3, int((hy1 - hy0) * 0.25))
                mask[max(0, hy0 - m):min(h, hy1 + m),
                     max(0, hx0 - m):min(w, hx1 + m)] = 255
            frame = cv2.inpaint(frame, mask, inpaint_radius, cv2.INPAINT_TELEA)
        elif (y1 - y0) > 100:
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


def ocr_clean_cjk(img, conf_threshold: float = 0.5):
    """封面帧 CJK 兜底清理：plan 没命中的漏网中文由 OCR 全检补刀。

    背景（2026-10-28）：clean_frame_with_plan 只擦「frame_time 那一刻在 plan
    窗口内」的区域，字幕行/角标若不在窗口内就漏 —— 国王封面残留「韦伯生涯十佳球」、
    07 全明星封面残留「看我」，同一类 bug 两例。
    只擦**含 CJK 字符**的检出：球衣上的 KINGS、记分牌数字等英文/数字是画面
    内容，绝不能动。无 rapidocr/cv2 时静默跳过（不阻塞封面生成）。
    Returns: (cleaned PIL Image, n_regions)
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return img, 0
    try:
        frame = np.array(img.convert("RGB"))
        h, w = frame.shape[:2]
        res, _ = RapidOCR()(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
    except Exception as exc:                                    # pragma: no cover
        log.warning(f"OCR sweep skipped: {exc}")
        return img, 0
    if not res:
        return img, 0
    mask = np.zeros((h, w), dtype=np.uint8)
    n = 0
    for box, text, conf in res:
        if conf < conf_threshold:
            continue
        if not any('\u4e00' <= c <= '\u9fff' for c in (text or "")):
            continue
        cv2.fillPoly(mask, [np.array(box, np.int32)], 255)
        n += 1
    if n:
        mask = cv2.dilate(mask, np.ones((9, 9), np.uint8))
        frame = cv2.inpaint(frame, mask, 7, cv2.INPAINT_TELEA)
    return Image.fromarray(frame), n


def _rebase_frame_paths(frames, work_dir):
    """frames-index 里的绝对路径可能指向改名前的旧目录（如整目录后加 -done）。

    帧文件其实还在当前 work_dir 下。不重接的后果：所有路径不存在 → 评分全跳过
    → legacy 兜底 return frames[0] → 未评分的 t=0 粗扫帧直接当封面（2026-10-28 国王实测）。
    Returns: (frames, n_fixed)
    """
    fixed = 0
    for fr in frames or []:
        p = fr.get("path") or ""
        if p and not os.path.exists(p):
            idx = p.find("frames" + os.sep)
            if idx < 0:
                idx = p.find("frames/")
            if idx >= 0:
                cand = os.path.join(work_dir, p[idx:].replace("/", os.sep))
                if os.path.exists(cand):
                    fr["path"] = cand
                    fixed += 1
    return frames, fixed


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
        layout=style.get("layout") or "",
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
    ap.add_argument("--layout", choices=["safe", "stack", "classic"],
                    help="Override cover_style.layout: safe = title floats at the "
                         "top of the band, stack = every row in ONE bottom block")
    ap.add_argument("--safe-top", type=float,
                    help="Override cover_style.safe_zone_top_pct (e.g. 0.25)")
    ap.add_argument("--safe-bottom", type=float,
                    help="Override cover_style.safe_zone_bottom_pct")
    ap.add_argument("--sub-color",
                    help="Override cover_style.sub_color as #RRGGBB (grey by default)")
    ap.add_argument("--tagline-color",
                    help="Override cover_style.tagline_color as #RRGGBB (white by default)")
    ap.add_argument("--block-scale", type=float,
                    help="Scale the stack block's type (headline/sub/stat/tagline), "
                         "e.g. 0.8; the brand mark keeps its profile size")
    ap.add_argument("--stack-inset", type=float,
                    help="Override cover_style.stack_bottom_inset_pct -- how far the "
                         "block's bottom sits above the safe-band bottom, x band height "
                         "(default 0.075; raise it to LIFT the whole block)")
    ap.add_argument("--drop-rows",
                    help="Comma-separated stack rows to omit: any of sub,stat,tag")
    ap.add_argument("--stack-anchor", choices=["bottom", "center", "top", "split"],
                    help="Where the stack rows sit in the safe band: bottom / "
                         "center / top keep them in one block; split sends the "
                         "copy rows to the head and the signature to the foot "
                         "(default bottom)")
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
    if getattr(args, "layout", None):
        cover_style = dict(cover_style)
        cover_style["layout"] = args.layout
    if getattr(args, "safe_top", None) is not None:
        cover_style = dict(cover_style)
        cover_style["safe_zone_top_pct"] = args.safe_top
    if getattr(args, "safe_bottom", None) is not None:
        cover_style = dict(cover_style)
        cover_style["safe_zone_bottom_pct"] = args.safe_bottom
    for flag, key in (("sub_color", "sub_color"),
                      ("tagline_color", "tagline_color")):
        raw = getattr(args, flag, None)
        if raw:
            h = raw.lstrip("#")
            if len(h) != 6:
                raise SystemExit(f"--{flag.replace('_', '-')} expects #RRGGBB, got {raw!r}")
            cover_style = dict(cover_style)
            cover_style[key] = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    if getattr(args, "block_scale", None):
        cover_style = dict(cover_style)
        cover_style["block_scale"] = args.block_scale
    if getattr(args, "stack_inset", None) is not None:
        cover_style = dict(cover_style)
        cover_style["stack_bottom_inset_pct"] = args.stack_inset
    if getattr(args, "drop_rows", None):
        cover_style = dict(cover_style)
        cover_style["drop_rows"] = [k.strip() for k in args.drop_rows.split(",")
                                    if k.strip()]
    if getattr(args, "stack_anchor", None):
        cover_style = dict(cover_style)
        cover_style["stack_anchor"] = args.stack_anchor
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
    frames, n_rebased = _rebase_frame_paths(frames, args.work_dir)
    if n_rebased:
        log.info(f"rebased {n_rebased} frame path(s) onto current work dir "
                 "(frames-index pointed at a renamed directory)")
    understanding = load_json(
        args.video_understanding
        or work_path(args.work_dir, "video-understanding.json"), {}) or {}
    plan = load_json(plan_path, {}) or {}

    # ⚠️ selected_frame 有两种来源：Agent/上轮跑分写回的**决策**（带 cover_score），
    # 或 prepare_plan 播下的**占位**（cover_score=None，注释自述 TODO(agent)）。
    # 占位不是决策 —— 盲信它会把未评分的 t=0 粗扫帧当封面（2026-10-28 国王实测：
    # 脸被切在画面顶端）。无评分一律走自动选帧；自动选帧失败才退回占位。
    placeholder = plan.get("selected_frame")
    if placeholder and placeholder.get("cover_score") is None:
        if placeholder.get("path") and not os.path.exists(placeholder["path"]):
            fixed_list, _ = _rebase_frame_paths([placeholder], args.work_dir)
            placeholder = fixed_list[0] if fixed_list else None
        log.warning("cover-plan.selected_frame has no cover_score -- it is a "
                    "prepare_plan placeholder, not a decision; running "
                    "auto-selection instead")
        if not (placeholder and os.path.exists(placeholder.get("path", ""))):
            placeholder = None
        selected_frame = None
    else:
        selected_frame = placeholder
        placeholder = None
    if not selected_frame:
        log.info("Selecting best frame automatically...")
        selected_frame = select_best_frame(
            frames, understanding, cover_style.get("face_protection", True))
        if selected_frame:
            log.info(f"Selected frame: {selected_frame.get('frame_id')} @ "
                     f"{selected_frame.get('timestamp_sec')}s "
                     f"(score={selected_frame.get('cover_score', '-')}, "
                     f"sharpness={selected_frame.get('sharpness')})")
        elif placeholder:
            log.warning("auto-selection produced nothing; falling back to the "
                        "unscored placeholder frame")
            selected_frame = placeholder
    if not selected_frame or not os.path.exists(selected_frame.get("path", "")):
        log.error("No valid frame selected for cover")
        sys.exit(1)

    headline = (args.headline or plan.get("headline")
                or understanding.get("cover_headline") or DEFAULT_HEADLINE)

    plan_raw_items = (load_json(work_path(args.work_dir, "localization-plan.json"),
                               {}) or {}).get("items", [])
    # The cover is ONE frame: only the overlays on screen at that instant may be
    # cleaned. Cleaning the whole plan painted 03's 0.6-3.4s keyword pop-up onto
    # a t=236.85s frame as a black block (2026-09-29).
    frame_time = selected_frame.get("timestamp_sec")
    plan_items = plan_items_at(plan_raw_items, frame_time)
    items_at_frame = select_cleanable_items(plan_items)

    from PIL import Image
    img = Image.open(selected_frame["path"]).convert("RGB")
    src_size = {"width": img.width, "height": img.height}
    cleaned_count = 0
    if cover_style.get("clean_frame_enabled", True):
        img, cleaned_count = clean_frame_with_plan(img, plan_raw_items, frame_time)
        # CJK 兜底：plan 窗口没命中的漏网中文由 OCR 全检补刀
        # （只擦含 CJK 的检出，球衣/记分牌等英文数字不动）
        img, n_ocr = ocr_clean_cjk(img)
        if n_ocr:
            log.info(f"OCR sweep cleaned {n_ocr} leftover CJK region(s) "
                     "plan cleaning missed")

    # Letterbox 裁除：源帧带上下黑条时裁到实际画面区再交给引擎。
    # 必须在帧清理之后（clean/OCR 兜底用的都是全帧坐标）。
    # 黑条进封面 = 标题压纯黑、logo 区大黑框（2026-10-29 国王/98全明星实录）
    # 模糊带版式用多帧行级中位数判定 —— 抽选帧同目录的兄弟粗扫帧。
    sibling_dir = os.path.dirname(selected_frame["path"]) or "."
    try:
        siblings = sorted(
            os.path.join(sibling_dir, f)
            for f in os.listdir(sibling_dir)
            if f.lower().endswith((".png", ".jpg")))
    except OSError:
        siblings = []
    step = max(1, len(siblings) // 11)
    extra_frames = siblings[::step][:11]
    content_band = detect_content_band(img, extra_frames)
    if content_band:
        bt, bb = content_band
        log.info(f"Letterbox detected: footage band y{bt}-{bb} "
                 f"(bars {bt}px top / {img.height - bb}px bottom); "
                 "cropping frame to content")
        img = img.crop((0, bt, img.width, bb))
        # 内容带已排除黑条/模糊带/烧录标题带，引擎的 crop_top/crop_bottom
        # （为躲烧字设计的 0.195/0.70）不再需要那么保守，放宽以免白白
        # 裁掉真画面
        spec_crop_top = min(float(cover_style.get("crop_top", 0.195)), 0.08)
        spec_crop_bottom = max(float(cover_style.get("crop_bottom", 0.70)), 0.92)
        cover_style = dict(cover_style)
        cover_style["crop_top"] = spec_crop_top
        cover_style["crop_bottom"] = spec_crop_bottom

    # 人脸检测一次，引擎与 QA 共用：在最终交给引擎的帧上（已清理/已裁内容带）
    # 用两遍 Haar（全分辨率 + 半分辨率 rescue）检测 —— 引擎内部的单遍检测
    # 对大而暗的特写脸会漏检（Kobe/国王 close-up 实录），导致 face-aware
    # 裁切失效、脸被切在画面上缘。
    cover_frame_path = work_path(args.work_dir, "cover-frame.png")
    try:
        img.save(cover_frame_path)
    except Exception as e:  # 落盘失败不阻塞，引擎退回内部检测
        log.warning(f"cover-frame.png save failed: {e}")
        cover_frame_path = selected_frame["path"]
    face_boxes = detect_faces(cover_frame_path)
    spec_face_box = None
    if face_boxes:
        largest = max(face_boxes, key=lambda b: b["w"] * b["h"])
        spec_face_box = (int(largest["x"]), int(largest["y"]),
                         int(largest["w"]), int(largest["h"]))

    spec = build_spec(headline, cover_style, plan, args)
    spec.face_box = spec_face_box
    result = render_cover(img, spec)
    result.image.save(cover_path, "JPEG", quality=95)
    print(format_report(spec, result.meta, cover_path, args.profile))

    # faces 已在最终帧（work/cover-frame.png）上检测，坐标即引擎所见帧空间，
    # 直接经 picture_src 变换到画布，QA 遮挡检查即可 like-for-like
    face_areas = map_boxes_to_canvas(
        face_boxes, result.meta["picture_src"])

    render_info = dict(result.meta)
    render_info["content_band"] = (list(content_band)
                                   if content_band else None)
    render_info["faces_detected"] = len(face_areas)
    render_info["face_areas"] = face_areas
    render_info["frame_cleaned_regions"] = cleaned_count
    try:
        render_info["frame_ocr_cleaned_regions"] = n_ocr
    except NameError:
        render_info["frame_ocr_cleaned_regions"] = 0
    # QA compares cleaned vs expected -- expected means "on screen at the
    # selected frame", not "everywhere in the video".
    render_info["frame_clean_expected"] = len(items_at_frame)
    render_info["frame_clean_at_sec"] = frame_time
    render_info["plan_items_total"] = len(plan_raw_items)
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
             f"cleaned regions: {cleaned_count}"
             + (f" of {len(items_at_frame)} on screen at {frame_time}s"
                if frame_time is not None else "")
             + f" (plan total {len(plan_raw_items)})")


if __name__ == "__main__":
    main()
