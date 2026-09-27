#!/usr/bin/env python3
"""
tts_timing.py - TTS 时间轴对齐

职责分离（严格遵守 Agent 决策、Script 执行）：
  Script 负责：
    - 测量 TTS 时长
    - 与原始时间窗口比较
    - 报告 overflow / underflow
    - 执行 Agent 已确定的调整方案
  Agent 负责（通过 prompts/tts_adjust.md）：
    - 判断如何调整
    - 判断是否需要缩短翻译
    - 判断是否需要调整语速
    - 判断是否需要拆句

子命令：
  --measure: 测量 TTS 时长，输出 timing-report.json（含 overflow/underflow，不做决策）
  --apply:   执行 Agent 写入 timing-decisions.json 的方案，输出 timeline.json（最终时间轴）

playback_rate 统一定义：
  1.0 = normal speed
  >1.0 = faster (1.10 = +10%)
  <1.0 = slower
  允许区间 [0.90, 1.15]
"""

import argparse
import os
import sys
import json
from typing import List, Dict, Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    save_json, load_json, make_result, log,
    work_path, require_tools, run,
    EXIT_DEGRADED,
)

PLAYBACK_RATE_MIN = 0.90
PLAYBACK_RATE_MAX = 1.15


def get_audio_duration(audio_path: str) -> float:
    """用 ffprobe 获取音频时长（秒）"""
    if not os.path.exists(audio_path):
        return 0.0
    r = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ], check=False)
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def measure_timing(work_dir: str, translated_path: str, tts_dir: str,
                    asr_path: str, tolerance_sec: float = 0.3) -> Dict:
    """
    --measure: 测量 TTS 时长，与原窗口比对，报告 overflow/underflow。
    不做任何调整决策。
    """
    translated = load_json(translated_path, {})
    asr = load_json(asr_path, {})

    entries = translated.get("entries", [])
    asr_segments = asr.get("segments", [])

    if not entries:
        log.warning("No translated entries to measure")
        return make_result(
            status="ok",
            confidence=1.0,
            entries=[],
            needs_agent_decision=[],
            warnings=["No translated entries"],
        )

    timing_entries = []
    needs_decision = []

    for i, entry in enumerate(entries):
        # 原视频时间窗口（主约束）
        source_start = entry.get("source_start", 0)
        source_end = entry.get("source_end", 0)
        window_sec = source_end - source_start

        # TTS 音频文件
        tts_file = entry.get("tts_file") or os.path.join(tts_dir, f"tts_{i:04d}.wav")
        tts_duration = get_audio_duration(tts_file)

        # 比对
        overflow_sec = max(0, tts_duration - window_sec)
        underflow_sec = max(0, window_sec - tts_duration)

        if overflow_sec > tolerance_sec:
            status = "overflow"
            needs_decision.append(i)
        elif underflow_sec > tolerance_sec * 2:
            status = "underflow"
            # underflow 通常不需要 Agent 决策，可以自动加停顿
        else:
            status = "ok"

        timing_entries.append({
            "index": i,
            "source_window": {"start": source_start, "end": source_end},
            "window_sec": round(window_sec, 3),
            "text_zh": entry.get("source_text", ""),
            "text_en": entry.get("target_text", ""),
            "tts_file": tts_file,
            "tts_raw_duration_sec": round(tts_duration, 3),
            "overflow_sec": round(overflow_sec, 3),
            "underflow_sec": round(underflow_sec, 3),
            "status": status,
        })

    result = make_result(
        status="ok" if not needs_decision else "needs_adjustment",
        confidence=0.9,
        entries=timing_entries,
        needs_agent_decision=needs_decision,
        overflow_count=len([e for e in timing_entries if e["status"] == "overflow"]),
        underflow_count=len([e for e in timing_entries if e["status"] == "underflow"]),
        ok_count=len([e for e in timing_entries if e["status"] == "ok"]),
        tolerance_sec=tolerance_sec,
    )

    out_path = work_path(work_dir, "timing-report.json")
    save_json(out_path, result)
    log.info(f"Timing measurement complete: {out_path}")
    log.info(f"  OK: {result['ok_count']}, Overflow: {result['overflow_count']}, Underflow: {result['underflow_count']}")
    if needs_decision:
        log.warning(f"  {len(needs_decision)} entries need Agent adjustment decision (see tts_adjust.md)")

    return result


def apply_timing(work_dir: str, decisions_path: str, timing_report_path: str,
                  translated_path: str, tts_dir: str, max_rounds: int = 2) -> Dict:
    """
    --apply: 执行 Agent 写入 timing-decisions.json 的调整方案，输出 timeline.json。
    Agent 决策内容：
      - method: shorten_translation | resegment | speed_up | shift_into_gap | combine
      - final_text: 调整后的最终文本
      - playback_rate: 语速（0.90-1.15）
      - final_start: 最终起始时间
    """
    decisions = load_json(decisions_path, {})
    timing_report = load_json(timing_report_path, {})
    translated = load_json(translated_path, {})

    decision_entries = decisions.get("decisions", [])
    timing_entries = timing_report.get("entries", [])

    if not timing_entries:
        log.error("No timing entries to apply")
        sys.exit(EXIT_DEGRADED)

    timeline_entries = []
    unresolved = []

    for i, t_entry in enumerate(timing_entries):
        idx = t_entry["index"]
        decision = next((d for d in decision_entries if d.get("index") == idx), None)

        source_window = t_entry["source_window"]
        window_sec = t_entry["window_sec"]

        if decision:
            method = decision.get("method", "none")
            final_text = decision.get("final_text", t_entry["text_en"])
            playback_rate = decision.get("playback_rate", 1.0)
            final_start = decision.get("final_start", source_window["start"])

            # 校验 playback_rate 范围
            playback_rate = max(PLAYBACK_RATE_MIN, min(PLAYBACK_RATE_MAX, playback_rate))

            # 计算最终时长（如果有重新 TTS 的文件，用实测；否则用 raw / playback_rate 估算）
            adjusted_tts_file = decision.get("adjusted_tts_file")
            if adjusted_tts_file and os.path.exists(adjusted_tts_file):
                final_duration = get_audio_duration(adjusted_tts_file)
            else:
                final_duration = t_entry["tts_raw_duration_sec"] / max(playback_rate, 0.01)

            final_end = final_start + final_duration
            fits_window = final_duration <= window_sec + 0.3  # 容忍度

            if not fits_window and method != "none":
                unresolved.append({
                    "index": idx,
                    "reason": f"Still overflows after {method}: {final_duration:.2f}s > {window_sec:.2f}s window",
                    "final_duration": round(final_duration, 3),
                    "window_sec": round(window_sec, 3),
                })

            timeline_entries.append({
                "index": idx,
                "source_window": source_window,
                "window_sec": round(window_sec, 3),
                "text_zh": t_entry["text_zh"],
                "text_en": final_text,
                "tts_raw_duration_sec": t_entry["tts_raw_duration_sec"],
                "adjustment": {
                    "method": method,
                    "decided_by": "agent",
                    "playback_rate": playback_rate,
                    "playback_rate_definition": "1.0=normal, >1.0=faster (1.10=+10%), <1.0=slower; allowed [0.90,1.15]",
                    "final_start": round(final_start, 3),
                },
                "final_duration_sec": round(final_duration, 3),
                "final_start": round(final_start, 3),
                "final_end": round(final_end, 3),
                "fits_window": fits_window,
                "tts_file": adjusted_tts_file or t_entry["tts_file"],
            })
        else:
            # 无决策，使用原始值
            final_start = source_window["start"]
            final_duration = t_entry["tts_raw_duration_sec"]
            final_end = final_start + final_duration
            fits_window = final_duration <= window_sec + 0.3

            timeline_entries.append({
                "index": idx,
                "source_window": source_window,
                "window_sec": round(window_sec, 3),
                "text_zh": t_entry["text_zh"],
                "text_en": t_entry["text_en"],
                "tts_raw_duration_sec": t_entry["tts_raw_duration_sec"],
                "adjustment": {
                    "method": "none",
                    "decided_by": "script",
                    "playback_rate": 1.0,
                    "final_start": round(final_start, 3),
                },
                "final_duration_sec": round(final_duration, 3),
                "final_start": round(final_start, 3),
                "final_end": round(final_end, 3),
                "fits_window": fits_window,
                "tts_file": t_entry["tts_file"],
            })

    # 按 final_start 排序
    timeline_entries.sort(key=lambda e: e["final_start"])

    result = make_result(
        status="ok" if not unresolved else "ok_with_unresolved",
        confidence=0.85 if not unresolved else 0.6,
        strategy="original_timeline_master",
        playback_rate_definition="1.0=normal, >1.0=faster (1.10=+10%), <1.0=slower; allowed [0.90,1.15]",
        entries=timeline_entries,
        unresolved=unresolved,
        total_entries=len(timeline_entries),
        fits_count=len([e for e in timeline_entries if e["fits_window"]]),
        unresolved_count=len(unresolved),
        adjustments_applied=len([e for e in timeline_entries if e["adjustment"]["method"] != "none"]),
    )

    out_path = work_path(work_dir, "timeline.json")
    save_json(out_path, result)
    log.info(f"Timing apply complete: {out_path}")
    log.info(f"  Total: {len(timeline_entries)}, Fits: {result['fits_count']}, "
             f"Adjusted: {result['adjustments_applied']}, Unresolved: {len(unresolved)}")
    if unresolved:
        log.warning(f"  {len(unresolved)} entries still unresolved -> QA WARNING")

    return result


def main():
    ap = argparse.ArgumentParser(description="TTS timing alignment (measure or apply)")
    sub = ap.add_subparsers(dest="command", required=True)

    # measure
    m = sub.add_parser("measure", help="Measure TTS duration and compare with original windows")
    m.add_argument("--work-dir", required=True)
    m.add_argument("--translated", help="Path to translated.json (default: work/translated.json)")
    m.add_argument("--tts-dir", help="Directory with TTS audio files (default: work/tts)")
    m.add_argument("--asr", help="Path to asr.json (default: work/asr.json)")
    m.add_argument("--tolerance", type=float, default=0.3, help="Overflow tolerance in seconds")

    # apply
    a = sub.add_parser("apply", help="Apply Agent decisions from timing-decisions.json")
    a.add_argument("--work-dir", required=True)
    a.add_argument("--decisions", help="Path to timing-decisions.json (default: work/timing-decisions.json)")
    a.add_argument("--timing-report", help="Path to timing-report.json (default: work/timing-report.json)")
    a.add_argument("--translated", help="Path to translated.json")
    a.add_argument("--tts-dir", help="Directory with TTS audio files")
    a.add_argument("--max-rounds", type=int, default=2, help="Max adjustment rounds")

    args = ap.parse_args()
    require_tools("ffprobe")

    if args.command == "measure":
        translated_path = args.translated or work_path(args.work_dir, "translated.json")
        tts_dir = args.tts_dir or os.path.join(args.work_dir, "tts")
        asr_path = args.asr or work_path(args.work_dir, "asr.json")
        measure_timing(args.work_dir, translated_path, tts_dir, asr_path, args.tolerance)

    elif args.command == "apply":
        decisions_path = args.decisions or work_path(args.work_dir, "timing-decisions.json")
        timing_report_path = args.timing_report or work_path(args.work_dir, "timing-report.json")
        translated_path = args.translated or work_path(args.work_dir, "translated.json")
        tts_dir = args.tts_dir or os.path.join(args.work_dir, "tts")
        apply_timing(args.work_dir, decisions_path, timing_report_path,
                     translated_path, tts_dir, args.max_rounds)


if __name__ == "__main__":
    main()
