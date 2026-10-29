#!/usr/bin/env python3
"""Build work/timeline.json from localization-plan items (no-TTS flows).

Only caption/dialogue items go into the timeline; persistent titles stay out
(render_subtitles filters STATIC_CATEGORIES by index anyway).

Usage: build_timeline.py <work_dir>
"""
import os
import sys

sys.path.insert(0, "/Users/jason/Public/skills/tiktok-video-localizer-v2/scripts")
from common import work_path, load_json, save_json, log  # noqa: E402

STATIC = {"title", "logo", "watermark", "slogan"}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("work_dir_pos", nargs="?", default=None)
    ap.add_argument("--work-dir", dest="work_dir_opt", default=None)
    args = ap.parse_args()
    raw = args.work_dir_opt or args.work_dir_pos
    if not raw:
        print("usage: build_timeline.py <work_dir> | --work-dir <work_dir>", file=sys.stderr)
        sys.exit(2)
    work = os.path.abspath(raw)
    existing = load_json(work_path(work, "timeline.json"), {})
    if existing.get("entries"):
        log.info("timeline.json already has entries; skip")
        return
    plan = load_json(work_path(work, "localization-plan.json"), {}) or {}
    items = plan.get("items", [])
    # 防御（2026-10-29，98全明星 A1/A2 实测）：Agent 会把视频简介总结文案
    # 错标成全片窗口（0→187.7s）。这种「伪台词」进 timeline 后，SRT 的重叠
    # 修剪把它压成 0-0.4s 叠影、成片绘制把它变成全片常驻字幕。
    # 上游在这里直接剔除：窗口 >= max(60s, 50% 全片跨度) 的 dialogue 一律不进。
    video_span = max((float(it.get("end_time") or 0) - float(it.get("start_time") or 0)
                      for it in items), default=0.0)
    full_span_limit = max(60.0, 0.5 * video_span)
    entries = []
    n_pseudo = 0
    for i, it in enumerate(items):
        if it.get("category") in STATIC:
            continue
        tgt = it.get("target_text")
        if not tgt:
            continue
        span = float(it.get("end_time") or 0) - float(it.get("start_time") or 0)
        if span > full_span_limit:
            n_pseudo += 1
            log.warning(
                f"item[{i}] '{tgt[:30]}...' window {span:.1f}s >= "
                f"{full_span_limit:.1f}s (summary caption, not timed dialogue) "
                "- excluded from timeline")
            continue
        entries.append(_entry(i, it))
    if n_pseudo:
        log.warning(f"timeline: dropped {n_pseudo} full-video-window pseudo cue(s)")

    # 只有 persistent title 的视频：SRT 也要有内容，回退收录 STATIC 项
    if not entries:
        for i, it in enumerate(plan.get("items", [])):
            if it.get("category") in STATIC and it.get("target_text"):
                entries.append(_entry(i, it))
    save_json(work_path(work, "timeline.json"), {"entries": entries})
    log.info("timeline.json written: %d entries (from plan items)" % len(entries))


def _entry(i, it):
    return {
        "index": i,
        "source_window": {"start": it["start_time"], "end": it["end_time"]},
        "window_sec": round(it["end_time"] - it["start_time"], 3),
        "text_zh": it.get("source_text", ""),
        "text_en": it.get("target_text"),
        "tts_raw_duration_sec": 0.0,
        "adjustment": {"method": "overlay_only", "decided_by": "agent",
                       "playback_rate": 1.0,
                       "final_start": it["start_time"]},
        "final_duration_sec": round(it["end_time"] - it["start_time"], 3),
        "final_start": it["start_time"],
        "final_end": it["end_time"],
        "fits_window": True,
        "tts_file": None,
    }


if __name__ == "__main__":
    main()
