#!/usr/bin/env python
"""Map source-audio evidence to the USER RULE 2026-11-14 BGM decision tree.

Reads work/source-audio-profile.json + work/asr.json and writes
work/audio-policy.json. This is ADVICE for the Agent (it must still listen /
review when evidence conflicts) -- it deliberately does NOT auto-pick a
library BGM and does NOT auto-mute anything.

Decision tree (strict order):
  lyric-bearing BGM      -> tts_keep_instrumental (demucs, keep ORIGINAL rhythm)
  Chinese narration      -> narration_rewrite (separate voice, English dub on the
                            original timing, remix with the original bed)
  English narration/BGM  -> keep_original
  nothing at all         -> add_bgm (last resort; run bgm_review.py)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics

RULE = "USER RULE 2026-11-14"


def load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default if default is not None else {}


def lyric_score(segs, duration):
    """Heuristics separating a sung Chinese pop song from play-by-play speech."""
    texts = [re.sub(r"[^\w\u4e00-\u9fff]", "", s.get("text", "")) for s in segs]
    lens = [len(t) for t in texts if t]
    durs = [max(0.0, float(s.get("end", 0)) - float(s.get("start", 0))) for s in segs]
    if not lens:
        return 0.0, {}
    short = sum(1 for x in lens if 4 <= x <= 14) / len(lens)
    coverage = sum(durs) / max(duration, 1.0)
    uniform = 1.0 / (1.0 + (statistics.pstdev(durs) if len(durs) > 1 else 9.0))
    many = min(len(segs) / 8.0, 1.0)
    score = 0.38 * short + 0.28 * min(coverage, 1.0) + 0.18 * uniform + 0.16 * many
    return score, {"avg_chars": round(statistics.mean(lens), 1),
                   "short_line_ratio": round(short, 2),
                   "speech_coverage": round(min(coverage, 1.0), 2),
                   "segment_count": len(segs),
                   "duration_std": round(statistics.pstdev(durs) if len(durs) > 1 else 0, 2)}


def decide(work):
    prof = load(os.path.join(work, "source-audio-profile.json"))
    asr = load(os.path.join(work, "asr.json"))
    probe = load(os.path.join(work, "probe.json"))
    dur = float(probe.get("duration_sec") or probe.get("duration") or 0)
    segs = asr.get("segments", []) or []
    has_speech = bool(prof.get("has_speech"))
    has_music = bool(prof.get("has_music"))
    lang = asr.get("language_detected") or prof.get("speech_lang")
    zh = bool(asr.get("has_chinese_narration")) or lang == "zh"
    en = bool(asr.get("has_foreign_narration")) or lang == "en"

    lyric, feat = lyric_score(segs, dur)
    conflict = (not has_speech and bool(segs)) or (has_speech is False and zh)

    if en and not zh:
        case, strategy = "english_narration_or_bgm", "keep_original"
        actions = ["Keep the FULL original audio track. Do not add any BGM, "
                   "TTS or ducking."]
    elif zh and has_music and lyric >= 0.62:
        case, strategy = "lyric_bearing_bgm", "tts_keep_instrumental"
        actions = ["demucs --two-stems=vocals -n htdemucs on source_audio.wav",
                   "copy no_vocals.wav -> work/instrumental.wav (ORIGINAL tempo/drops)",
                   "NO third-party BGM, NO TTS, NO muting the whole mix",
                   "verify the instrumental with ASR: 0 lyric segments"]
    elif zh:
        case, strategy = "chinese_narration", "narration_rewrite"
        actions = ["demucs separate vocals (Chinese narration) + no_vocals (original bed)",
                   "ASR Chinese -> Agent writes natural English lines on the SAME timing",
                   "generate a natural-sounding English voice (no mechanical TTS timbre)",
                   "align to the original narration windows and remix over no_vocals",
                   "if original-voice English cloning is required, confirm: no local model"]
    elif has_music:
        case, strategy = "instrumental_bgm_only", "keep_original"
        actions = ["Original instrumental bed already fits the edit; keep it unchanged."]
    else:
        case, strategy = "no_narration_no_bgm", "add_bgm"
        actions = ["LAST RESORT only: run bgm_review.py --video <clip> --top 8",
                   "pick ONLY from the licensed local library, tempo must match the edit",
                   "reject monotone loops / harsh timbre / loud or frequent cymbals"]

    return {
        "rule": RULE,
        "case": case,
        "strategy": strategy,
        "needs_agent_review": bool(conflict or (zh and 0.45 <= lyric < 0.62)),
        "evidence": {
            "has_speech": has_speech,
            "has_music": has_music,
            "speech_lang": lang,
            "asr_status": asr.get("status"),
            "chinese_ratio": asr.get("chinese_ratio"),
            "lyric_score": round(lyric, 3),
            "lyric_features": feat,
            "old_analyzer_suggestion": prof.get("recommended_strategy"),
            "evidence_conflict": conflict,
        },
        "actions": actions,
        "note": ("Old 2026-11-13 analyzer suggestion is advisory only and may "
                 "conflict with this rule. When evidence conflicts, listen to the "
                 "clip before writing agent-decisions.json."),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    a = ap.parse_args()
    r = decide(a.work)
    p = os.path.join(a.work, "audio-policy.json")
    json.dump(r, open(p, "w"), ensure_ascii=False, indent=2)
    print("%-24s -> %-24s review=%s lyric=%.2f conflict=%s  %s" %
          (r["case"], r["strategy"], r["needs_agent_review"],
           r["evidence"]["lyric_score"], r["evidence"]["evidence_conflict"], p))


if __name__ == "__main__":
    main()
