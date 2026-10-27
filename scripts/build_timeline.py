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
    entries = []
    for i, it in enumerate(plan.get("items", [])):
        if it.get("category") in STATIC:
            continue
        tgt = it.get("target_text")
        if not tgt:
            continue
        entries.append(_entry(i, it))

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
