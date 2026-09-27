#!/usr/bin/env python3
"""
prepare_plan.py — 从 Job1 analyze 产物自动生成 Agent 决策 JSON 的**草稿**。

存在理由（2026-09-22 复盘）：
  Agent 逐个 job 手工驱动时，最大的时间黑洞不是计算，而是「搞清契约格式」——
  读脚本源码猜字段、手敲 6 个大 JSON、再肉眼反复确认 bbox。
  实测一次 3 视频的批次里，26 分钟完全无产物推进，纯耗在看图 + 猜格式上。

本脚本把**确定性部分**做掉：中文区域筛选/去重/合并/几何分类/时间范围/bbox 取值/
封面候选选帧/音频策略推导。

Agent 只剩下的**语义判断**：英文译文怎么写、封面标题文案、post 文案、TTS 要不要缩短。
脚本绝不假装理解视频内容 —— target_text / headline / caption 一律留空或占位，由 Agent 填。

输出（全部写入 <work-dir>/，已存在的文件默认不覆盖，加 --force 才覆盖）：
  localization-plan.json      中文区 items（category/action/bbox/times），target_text 留空待填
  audio-plan.json             按 asr.has_chinese_narration 推导策略
  cover-plan.json             按清晰度给出最多 8 个候选帧
  video-understanding.json    骨架（Agent 补 summary/cover_headline）
  post-content.json           骨架（Agent 补 caption/hashtags/pinned）

用法：
  python prepare_plan.py --work-dir <work> --profile nba [--no-bgm] [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    normalize_persistent_starts,
    log_persistent_snaps,
    load_profile,
)

CJK = re.compile(r"[\u4e00-\u9fff]")
CJK_COUNT_MIN = 2  # 少于 2 个汉字的命中视为 OCR 误报

# 品牌黄兜底值。**只是兜底** —— main() 会从 `profile.cover_style.fill_color` 读取真值
# 并覆盖它（2026-09-27 起品牌色唯一真源在 profile，此前的字面量 0xFFDC00 已作废）。
DEFAULT_ACCENT_HEX = "0xFFE800"

# 同区域合并阈值（像素）
MERGE_DIST = 60


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def n_cjk(text: str) -> int:
    return len(CJK.findall(text or ""))


def get_size(work_dir: str, frames: list):
    """尽量拿到画面宽高；拿不到就退化为 0（分类时跳过边缘判定）。"""
    probe = load_json(os.path.join(work_dir, "probe.json"), {}) or {}
    for key in ("width", "video_width"):
        if probe.get(key):
            return probe[key], probe.get("height") or probe.get("video_height") or 0
    vid = (probe.get("video") or {}) if isinstance(probe, dict) else {}
    if vid.get("width"):
        return vid["width"], vid.get("height", 0)
    for stream in (probe.get("streams") or []):
        if stream.get("width"):
            return stream["width"], stream.get("height", 0)
    # 退化：读第一帧
    for fr in frames:
        p = fr.get("path")
        if p and os.path.exists(p):
            try:
                import cv2

                img = cv2.imread(p)
                if img is not None:
                    return img.shape[1], img.shape[0]
            except Exception:
                pass
            break
    return 0, 0


def pick_cjk_tracks(tracks: list) -> list:
    """筛出可信的中文文字区域（过滤 OCR 误报）。"""
    out = []
    for t in tracks:
        text = t.get("text") or ""
        if n_cjk(text) < CJK_COUNT_MIN and t.get("language") != "zh":
            continue
        # 单帧碎片判定：只出现一帧且时长为 0 → 大概率误报（球衣号码/纹理被当作汉字）
        det = t.get("detection_count", 0)
        dur = t.get("duration_sec", 0) or 0
        persistence = t.get("persistence") or ""
        if det < 2 and dur < 0.5 and persistence == "flash":
            continue
        out.append(t)
    return out


def merge_regions(tracks: list) -> list:
    """把空间上重合的多条 track 合并成一个「区域」，取并集 bbox 与并集时间。"""
    regions: list[dict] = []
    for t in sorted(tracks, key=lambda x: x.get("start_time", 0)):
        b = t.get("bbox") or {}
        cx, cy = b.get("x", 0) + b.get("w", 0) / 2, b.get("y", 0) + b.get("h", 0) / 2
        hit = None
        for r in regions:
            if abs(r["cx"] - cx) < MERGE_DIST and abs(r["cy"] - cy) < MERGE_DIST:
                hit = r
                break
        if hit is None:
            hit = {
                "cx": cx,
                "cy": cy,
                "tracks": [],
                "x0": b.get("x", 0),
                "y0": b.get("y", 0),
                "x1": b.get("x", 0) + b.get("w", 0),
                "y1": b.get("y", 0) + b.get("h", 0),
                "start": t.get("start_time", 0),
                "end": t.get("end_time", 0),
            }
            regions.append(hit)
        else:
            # 更新中心（加权移动到新区域中心，避免连锁偏移过大）
            hit["cx"] = (hit["cx"] + cx) / 2
            hit["cy"] = (hit["cy"] + cy) / 2
        hit["tracks"].append(t)
        hit["x0"] = min(hit["x0"], b.get("x", 0))
        hit["y0"] = min(hit["y0"], b.get("y", 0))
        hit["x1"] = max(hit["x1"], b.get("x", 0) + b.get("w", 0))
        hit["y1"] = max(hit["y1"], b.get("y", 0) + b.get("h", 0))
        hit["start"] = min(hit["start"], t.get("start_time", 0))
        hit["end"] = max(hit["end"], t.get("end_time", 0))
    return regions


def classify_region(r: dict, W: int, H: int) -> tuple[str, str, float]:
    """返回 (category, action, confidence)。只做几何分类，不做语义判断。"""
    top_ratio = r["y0"] / H if H else 0
    bot_ratio = r["y1"] / H if H else 0
    left_ratio = r["x0"] / W if W else 0
    right_ratio = r["x1"] / W if W else 0
    h = r["y1"] - r["y0"]

    # 代表文本取 detection_count 最多的一条
    best = max(r["tracks"], key=lambda t: (t.get("detection_count", 0), t.get("confidence", 0)))
    confidence = min(0.95, max(0.6, best.get("confidence", 0.7)))

    if top_ratio < 0.15 and h > 40:
        return "title", "translate", confidence
    if bot_ratio > 0.85:
        return "caption_overlay", "translate", confidence
    if left_ratio < 0.12 or right_ratio > 0.88:
        return "logo", "remove", confidence
    return "caption_overlay", "translate", confidence


def build_items(regions: list, W: int, H: int) -> list:
    items = []
    for i, r in enumerate(sorted(regions, key=lambda x: x["start"]), start=1):
        best = max(r["tracks"], key=lambda t: (t.get("detection_count", 0), t.get("confidence", 0)))
        category, action, confidence = classify_region(r, W, H)
        # mask 外扩 3px（ rule 12.2：贴合即可，禁止大块矩形）
        bbox = {
            "x": max(0, int(r["x0"]) - 3),
            "y": max(0, int(r["y0"]) - 3),
            "w": int(r["x1"] - r["x0"]) + 6,
            "h": int(r["y1"] - r["y0"]) + 6,
        }
        h = bbox["h"]
        item = {
            "item_id": f"A{i}",
            "category": category,
            "action": action,
            "confidence": round(confidence, 2),
            "bbox": bbox,
            "start_time": round(r["start"], 2),
            "end_time": round(r["end"], 2),
            "source_text": best.get("text", ""),
            "target_text": None,  # ← Agent 必填（translate 时）
            "source_text_removal": True,
            "removal_method": ["blur"] if h > 100 else ["inpaint"],  # rule 1：大区模糊/小区 inpaint
            "replacement": None,
            "reason": (
                f"auto-generated from text-tracks: {len(r['tracks'])} merged detection(s), "
                f"height={h}px -> {'blur' if h > 100 else 'inpaint'}. "
                f"TODO(agent): verify time range visually, then fill target_text."
            ),
        }
        if action == "translate":
            item["replacement"] = {
                "asset": None,
                "font_size": _suggest_font_size(h),
                "font_color": DEFAULT_ACCENT_HEX if category == "title" else "white",
                "border_color": "black",
                "border_width": 4 if category == "title" else 3,
                "x": "(w-text_w)/2",
                "y": str(max(0, int(r["y0"]))),
            }
        items.append(item)
    return items


def _suggest_font_size(region_h: int) -> int:
    return max(20, min(56, int(region_h * 0.62)))


def pick_cover_frame(work_dir: str, frames: list, avoid_after_ratio: float = 0.88):
    """按清晰度排序选封面帧；避开片尾尾部帧（常有落版字幕）。"""
    try:
        import cv2
    except ImportError:
        return frames[0] if frames else None

    probe = load_json(os.path.join(work_dir, "probe.json"), {}) or {}
    duration = probe.get("duration") or probe.get("format", {}).get("duration") or 0
    scored = []
    for fr in frames:
        ts = fr.get("timestamp_sec", 0)
        if duration and ts > duration * avoid_after_ratio:
            continue
        p = fr.get("path")
        if not p or not os.path.exists(p):
            continue
        img = cv2.imread(p)
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        sharp = cv2.Laplacian(gray, cv2.CV_64F).var()
        scored.append((sharp, fr))
    if not scored:
        return frames[0] if frames else None
    scored.sort(key=lambda x: -x[0])
    # 返回最清晰的 8 个候选，让 Agent 在少数几个里挑，而不是遍历全部
    return scored[:8]


def main():
    ap = argparse.ArgumentParser(description="Generate draft Agent decision JSONs from Job1 analyze output")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--profile", default="nba")
    ap.add_argument("--no-bgm", action="store_true", help="user asked to skip BGM entirely")
    ap.add_argument("--force", action="store_true", help="overwrite existing drafts")
    ap.add_argument("--video", default=None, help="source video path (for reference fields)")
    args = ap.parse_args()

    work = args.work_dir
    if not os.path.isdir(work):
        print(f"[prepare_plan] ERROR: work-dir not found: {work}", file=sys.stderr)
        return 2

    tracks_doc = load_json(os.path.join(work, "text-tracks.json"), [])
    tracks = tracks_doc if isinstance(tracks_doc, list) else (tracks_doc.get("tracks") or [])
    frames_doc = load_json(os.path.join(work, "frames-index.json"), [])
    frames = frames_doc if isinstance(frames_doc, list) else (frames_doc.get("frames") or [])
    asr = load_json(os.path.join(work, "asr.json"), {}) or {}
    probe = load_json(os.path.join(work, "probe.json"), {}) or {}

    W, H = get_size(work, frames)
    print(f"[prepare_plan] tracks={len(tracks)} frames={len(frames)} canvas={W}x{H}")

    cjk_tracks = pick_cjk_tracks(tracks)
    regions = merge_regions(cjk_tracks)
    items = build_items(regions, W, H)
    print(f"[prepare_plan] cjk tracks kept={len(cjk_tracks)} -> merged regions={len(regions)} -> items={len(items)}")

    # (a) 常驻叠字窗口归一化：OCR 会把覆盖片头的 title/slogan/watermark 报晚报，
    #     start_time 落在 1.0s 这类值上 → 片头几帧永不清理（2026-09-27 事故）。
    #     在**起草阶段**就归一，plan 文件本身即正确，下游 mask/渲染无需再猜。
    log_persistent_snaps(normalize_persistent_starts({"items": items}), "prepare_plan")

    # (b) 品牌色只有一个真源 = profile.cover_style.fill_color。
    #     以前这里硬编码 "0xFFDC00"（老黄），与 §8.2 定的新黄不一致 ——
    #     与 cover_engine 那次硬编码颜色是同一类 bug。
    _profile = load_profile(args.profile) or {}
    _fill = (_profile.get("cover_style") or {}).get("fill_color") or [255, 232, 0]
    try:
        accent_hex = "0x%02X%02X%02X" % (int(_fill[0]), int(_fill[1]), int(_fill[2]))
    except (TypeError, ValueError, IndexError):
        accent_hex = "0xFFE800"
    for it in items:
        rep = it.get("replacement")
        if rep and rep.get("font_color"):
            rep["font_color"] = accent_hex if it.get("category") == "title" else "white"
    print(f"[prepare_plan] brand accent = {accent_hex} (from profile '{args.profile}')")

    written = []

    def write(name, obj):
        path = os.path.join(work, name)
        if os.path.exists(path) and not args.force:
            print(f"[prepare_plan] skip {name} (exists; use --force to overwrite)")
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        written.append(name)

    # 1) localization-plan
    has_zh = asr.get("has_chinese_narration")
    strategy = "mute_original_tts_bgm" if has_zh else "keep_original"
    write(
        "localization-plan.json",
        {
            "profile": args.profile,
            "review_mode": False,
            "audio_strategy": strategy,
            "audio_strategy_reason": (
                f"auto-derived: asr.has_chinese_narration={has_zh}. "
                + ("Chinese narration -> must mute original track (rule 12.3). " if has_zh else "No Chinese narration -> kept original audio. ")
                + ("User requested NO BGM processing." if args.no_bgm else
                   "TODO(agent): pick BGM from configs/bgm-library.yaml, and if the "
                   "original BGM should not dominate, switch strategy to "
                   "remove_original_bgm_only (drops ALL original audio) or "
                   "duck_original_add_bgm (keeps crowd/scene sound at a low level) "
                   "-- see SKILL.md section 3.")
            ),
            "bgm": {
                "asset_source": "none" if args.no_bgm else "music-library",
                "asset_path": None,
                "style_hint": "none - user requested no BGM processing" if args.no_bgm else "TODO(agent)",
            },
            "items": items,
        },
    )

    # 2) audio-plan
    write(
        "audio-plan.json",
        {
            "strategy": strategy,
            "bgm": {"asset_path": None, "volume_db": -18},
            "loudness_normalized": strategy == "mute_original_tts_bgm",
            "note": f"auto-derived from asr.has_chinese_narration={has_zh}"
            + ("; no BGM per user instruction" if args.no_bgm else ""),
        },
    )

    # 3) cover-plan（给出候选，Agent 定夺 + 补 headline）
    cands = pick_cover_frame(work, frames)
    cands = cands if isinstance(cands, list) else ([cands] if cands else [])
    # pick_cover_frame 返回 (sharpness, frame_dict) 元组时，解包出 frame_dict
    norm = []
    for c in cands:
        fr = c[1] if isinstance(c, tuple) and len(c) == 2 and isinstance(c[1], dict) else c
        norm.append(fr)
    cands = norm
    write(
        "cover-plan.json",
        {
            "selected_frame": (cands[0] if cands else None),
            "candidates": [
                {"frame_id": c["frame_id"], "timestamp_sec": c["timestamp_sec"], "path": c["path"]} for c in cands
            ],
            "headline": None,  # ← Agent 必填（禁止模板化，rule 12.6）
            "bottom_text": None,
            "note": "auto-selected by sharpness; TODO(agent): drop frames with watermark/CJK/halftone, then set headline.",
        },
    )

    # 4) video-understanding 骨架
    write(
        "video-understanding.json",
        {
            "summary": None,  # ← Agent 必填（英文）
            "detected_subjects": [],
            "cover_headline_suggestion": None,
            "timeline_highlights": [],
            "note": "auto skeleton; TODO(agent): fill summary + cover headline in English.",
        },
    )

    # 5) post-content 骨架
    write(
        "post-content.json",
        {
            "caption": None,  # ← Agent 必填
            "alt_caption": None,
            "hashtags": [],  # ← 恰好 5 个，禁止 #fyp/#viral
            "pinned_comment": None,  # ← 三要素：内容预告 + @HoopDynastyDaily + 互动问题
            "music_recommendation": [],
            "best_post_time_bj": None,
            "best_post_time_et": None,
            "note": "auto skeleton; TODO(agent): all-English copy, per-video (rule 12.7 禁止套模板).",
        },
    )

    print(f"[prepare_plan] wrote: {', '.join(written) if written else '(nothing)'}")

    if items:
        print("\n[prepare_plan] --- items summary (verify & fill target_text) ---")
        for it in items:
            b = it["bbox"]
            print(
                f"  {it['item_id']:<4} {it['action']:<9} conf={it['confidence']} "
                f"{it['start_time']:>6.1f}-{it['end_time']:>6.1f}s "
                f"bbox=({b['x']},{b['y']},{b['w']},{b['h']}) "
                f"{it['category']:<16} src={it['source_text']!r}"
            )
    else:
        print("[prepare_plan] No Chinese text regions detected -> nothing to clean. Video may be safe to pass through.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
