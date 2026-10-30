#!/usr/bin/env python3
"""
render_subtitles.py - 英文字幕生成与烧录

输入：timeline.json（最终时间轴，字幕与混音的唯一时间基准）
输出：
  - output/subtitles-en.srt
  - 烧录字幕后的视频（或由 audio_processor mix 时合并）

时间轴原则：原视频时间轴是主约束，TTS 时长是调整依据。
以 timeline.json 为准，不直接用 translated.json。
"""

import argparse
import os
import sys
from typing import List, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    run, save_json, load_json, make_result, log,
    work_path, output_path, require_tools, sec_to_srt_time,
)


STATIC_CATEGORIES = ("title", "watermark", "logo", "slogan")


def _collect_rows(timeline: Dict, plan: Dict = None) -> List:
    """从 timeline entries 收集字幕行（排除 static、无文本项）"""
    items = (plan or {}).get("items", [])
    rows = []
    for e in timeline.get("entries", []):
        idx = e.get("index")
        if items and idx is not None and idx < len(items):
            if items[idx].get("category") in STATIC_CATEGORIES:
                continue  # persistent overlay, not a spoken subtitle line
        text = e.get("text_en", "")
        if not text:
            continue
        start = float(e.get("final_start", 0))
        end = float(e.get("final_end", start + 2))
        if end <= start:
            win = e.get("source_window") or {}
            end = float(win.get("end", 0))
            if end <= start:
                end = start + 2
        rows.append([start, end, text, _item_y(items, idx) if items else 0])
    return rows


def _item_y(items, idx):
    """plan item 的绘制 y 坐标（用于同带判定），解析失败返回 0。"""
    try:
        rep = items[idx].get("replacement") or {}
        return float(rep.get("y", 0))
    except (TypeError, ValueError, IndexError):
        return 0.0


def generate_srt(timeline: Dict, output_path: str, plan: Dict = None) -> int:
    """从 timeline.json 生成 SRT 文件

    Fixes (2026-09-26):
      - static overlays (title / watermark / slogan) used to be written into the
        SRT with a bogus 0-3s window, duplicating on-screen titles as subtitles;
        they are now filtered out via localization-plan.json categories.
      - overlapping source windows used to produce stacked timestamps; windows are
        now trimmed so no two cues overlap.
    """
    entries = timeline.get("entries", [])
    if not entries:
        log.warning("No timeline entries for SRT generation (empty SRT)")
    rows = _collect_rows(timeline, plan)

    # 防御（2026-10-29，98全明星实测）：窗口接近全片长度的 cue 是总结文案
    # （Agent 错标的全片窗口），不是可定时台词 —— 留着会被重叠修剪逻辑压成
    # 与其他 cue 同起点的 0.4s 叠影。正常台词不会超过 60s，直接剔除。
    if rows:
        max_end = max(r[1] for r in rows)
        limit = max(30.0, 0.5 * max_end)
        kept = [r for r in rows if r[1] - r[0] <= limit]
        if len(kept) != len(rows):
            log.warning(
                f"SRT: dropped {len(rows) - len(kept)} cue(s) with absurd window "
                f"(span >= {limit:.1f}s) - summary captions, not timed lines"
            )
        rows = kept

    rows.sort(key=lambda r: (r[0], r[1]))

    # 防御（2026-10-30，森林狼跳舞大叔实测）：重叠修剪必须感知屏幕位置 ——
    # 顶部标题(y≈42)与底部字幕(y≈563)时间重叠是正常同显，互相挤压会把
    # 正常 cue 压成 0.4s。按 y 带（60px 桶）分组，组内独立做同窗口均分与残余修剪。
    def _band(r):
        return int(round((r[3] if len(r) > 3 else 0) / 60.0))

    band_map = {}
    for r in rows:
        band_map.setdefault(_band(r), []).append(r)
    out_rows = []
    for band in band_map.values():
        band.sort(key=lambda r: (r[0], r[1]))
        # 同带内多条 cue 挤同一窗口：按条数均分成先后切片
        i = 0
        while i < len(band):
            j = i
            while (j + 1 < len(band)
                   and band[j + 1][0] - band[i][0] < 0.35
                   and abs(band[j + 1][1] - band[i][1]) < 0.60):
                j += 1
            if j > i:
                w_start = min(r[0] for r in band[i:j + 1])
                w_end = max(r[1] for r in band[i:j + 1])
                step = (w_end - w_start) / (j - i + 1)
                for k, idx in enumerate(range(i, j + 1)):
                    band[idx][0] = round(w_start + k * step, 3)
                    band[idx][1] = round(w_start + (k + 1) * step - 0.02, 3)
                log.info(f"SRT: split {j - i + 1} cues sharing window "
                         f"{w_start:.2f}-{w_end:.2f}s into sequential slices")
                i = j + 1
            else:
                i += 1
        # 残余的部分重叠：压掉重叠段（保底 0.4s 可读时长）
        for i in range(len(band) - 1):
            if band[i][1] > band[i + 1][0]:
                band[i][1] = max(band[i][0] + 0.4, band[i + 1][0] - 0.02)
        for r in band:
            if r[1] - r[0] < 0.2:
                r[1] = r[0] + 0.2
        out_rows.extend(band)

    out_rows.sort(key=lambda r: (r[0], r[1]))
    rows = out_rows

    lines = []
    for i, (start, end, text, _y) in enumerate(rows, 1):
        lines.append(str(i))
        lines.append(f"{sec_to_srt_time(start)} --> {sec_to_srt_time(end)}")
        lines.append(text)
        lines.append("")  # 空行分隔

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return len(rows)


def burn_subtitles(video_path: str, srt_path: str, output_path: str,
                    style_config: Dict = None) -> None:
    """用 FFmpeg 烧录字幕到视频"""
    # 字幕样式
    style = "FontName=Arial,FontSize=24,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H80000000,BorderStyle=3,Outline=2,Shadow=1,Alignment=2,MarginV=40"
    if style_config:
        font = style_config.get("font", "Arial")
        font_size = style_config.get("font_size", 24)
        margin_v = style_config.get("margin_v", 40)
        outline = style_config.get("outline", 2)
        style = (f"FontName={font},FontSize={font_size},"
                 f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                 f"BackColour=&H80000000,BorderStyle=3,Outline={outline},"
                 f"Shadow=1,Alignment=2,MarginV={margin_v}")

    # 处理 srt 路径中的特殊字符
    srt_escaped = srt_path.replace(":", "\\:").replace("'", "\\'")

    run([
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"subtitles='{srt_escaped}':force_style='{style}'",
        "-c:a", "copy",
        output_path,
    ])


def main():
    ap = argparse.ArgumentParser(description="Generate and render English subtitles")
    ap.add_argument("--work-dir", required=True, help="Working directory")
    ap.add_argument("--output-dir", required=True, help="Output directory")
    ap.add_argument("--timeline", help="Path to timeline.json (default: work/timeline.json)")
    ap.add_argument("--video", help="Video to burn subtitles into (optional)")
    ap.add_argument("--style", help="Path to subtitle-style.json")
    ap.add_argument("--burn", action="store_true", help="Burn subtitles into video")
    args = ap.parse_args()

    require_tools("ffmpeg")
    os.makedirs(args.output_dir, exist_ok=True)

    timeline_path = args.timeline or work_path(args.work_dir, "timeline.json")
    timeline = load_json(timeline_path, {})

    # 生成 SRT（无 entries 也写空文件：四件套要求 SRT 恒存在，
    # 纯原声视频本就没有可翻译文字，空 SRT 是正确语义）
    plan_path = work_path(args.work_dir, "localization-plan.json")
    plan = load_json(plan_path, {}) if os.path.exists(plan_path) else {}
    srt_path = output_path(args.output_dir, "subtitles-en.srt")
    count = generate_srt(timeline, srt_path, plan)
    if not timeline.get("entries"):
        log.warning(f"No entries in timeline: {timeline_path} (empty SRT written)")
    log.info(f"SRT generated: {srt_path} ({count} entries)")

    # 烧录字幕
    if args.burn and args.video:
        style_config = None
        if args.style:
            style_config = load_json(args.style, {})
        elif os.path.exists(os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates", "subtitle-style.json")):
            style_config = load_json(
                os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates", "subtitle-style.json"), {}
            )

        burned_path = work_path(args.work_dir, "final_subtitled.mp4")
        burn_subtitles(args.video, srt_path, burned_path, style_config)
        log.info(f"Subtitles burned: {burned_path}")

    # 结果
    result = make_result(
        status="ok",
        confidence=0.95,
        srt_path=srt_path,
        entry_count=count,
        burned=args.burn,
    )
    save_json(work_path(args.work_dir, "subtitle-result.json"), result)


if __name__ == "__main__":
    main()
