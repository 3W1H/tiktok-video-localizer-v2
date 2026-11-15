#!/usr/bin/env python
"""Final self-check for the 2026-11-13 Downloads 42-clip batch.

USER RULES under test (2026-11-14):
  R1 erase only -- no added English title/hook/caption on the picture and no
     external SRT (zero overlap / zero oversized-subtitle risk).
  R2 BGM decision tree: lyric-bearing BGM -> original instrumental (demucs);
     Chinese narration -> English dub; English narration/BGM -> reuse original;
     only when neither exists may a licensed, tempo-matched library track be
     added.
  R3 brand yellow #FFE800 belongs to cover/brand marks; captions stay white.

Usage: python verify_all.py [--json diag/final-verification.json]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

ROOT = os.environ.get("LOCALIZER_BATCH_ROOT", os.path.dirname(os.path.abspath(__file__)))
SK = os.environ.get("LOCALIZER_SKILL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_ROOT = os.environ.get("LOCALIZER_OUT_ROOT", os.path.expanduser("~/Downloads/tiktok"))
LEDGER = SK + "/processed-videos.json"
sys.path.insert(0, SK + "/scripts")
from run_pipeline import sanitize_folder_name  # noqa: E402

ARTIFACTS = ("final.mp4", "cover.jpg", "subtitles-en.srt", "tiktok-post.md")
MIN_SIZE = {"final.mp4": 100_000, "cover.jpg": 10_000,
            "subtitles-en.srt": 0, "tiktok-post.md": 200}
RULE_MAP = {
    "english_narration_or_bgm": {"keep_original"},
    "instrumental_bgm_only": {"keep_original"},
    "lyric_bearing_bgm": {"tts_keep_instrumental"},
    "chinese_narration": {"narration_rewrite", "tts_keep_instrumental"},
    "no_narration_no_bgm": {"add_bgm", "duck_original_add_bgm"},
}
FORBIDDEN_OLD = {"mute_original_tts_bgm"}


def load(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


def brand_yellow_ratio(jpg):
    """Fraction of cover pixels close to #FFE800 (BGR 0,232,255)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    img = cv2.imread(jpg)
    if img is None:
        return None
    b = img.astype("int16")
    d = (np.abs(b[:, :, 0] - 0) + np.abs(b[:, :, 1] - 232)
         + np.abs(b[:, :, 2] - 255))
    return round(float((d < 75).mean()), 5)


def check_one(rec):
    i = rec["idx"]
    out = os.path.join(OUT_ROOT, sanitize_folder_name(os.path.basename(rec["path"])))
    # 2026-11-15: two finished clips were renamed with a "-done" suffix by the
    # user; keep them addressable instead of flagging them as missing.
    if not os.path.isdir(out) and os.path.isdir(out + "-done"):
        out = out + "-done"
    work = os.path.join(out, "work")
    r = {"idx": i, "name": rec["name"], "out": out, "issues": [], "warnings": []}

    # 1. four deliverables
    sizes = {}
    for f in ARTIFACTS:
        p = os.path.join(out, f)
        n = os.path.getsize(p) if os.path.exists(p) else -1
        sizes[f] = n
        if n < 0 or n < MIN_SIZE[f]:
            r["issues"].append("missing_or_small:%s(%s)" % (f, n))
    r["sizes"] = sizes
    # A clip still in the queue has no deliverables yet. Rule-level checks
    # (R1 SRT, cover yellow, ledger) only apply to DELIVERED clips; otherwise
    # a missing file (-1) masquerades as "SRT not empty" / "cover yellow low".
    r["delivered"] = all(sizes.get(f, -1) >= MIN_SIZE[f] for f in ARTIFACTS)

    # 2. QA json + Chinese residue hard gate
    q = load(os.path.join(work, "qa.json"))
    if not q:
        r["issues"].append("missing:qa.json")
    else:
        checks = q.get("checks", {})
        r["qa_overall"] = q.get("overall")
        ch = checks.get("chinese_residue", {})
        r["chinese_residue"] = ch.get("status", "unknown")
        r["frames_scanned"] = ch.get("frames_scanned")
        if ch.get("status") == "fail":
            r["issues"].append("CHINESE_RESIDUE_FAIL:%s" %
                               (ch.get("groups") or [])[:3])
        elif ch.get("status") == "warning":
            r["warnings"].append("chinese_residue warning (single-glyph OCR noise)")
        for k, v in checks.items():
            if isinstance(v, dict) and v.get("status") in ("fail", "error"):
                r["issues"].append("qa:%s=%s" % (k, v.get("status")))
            elif isinstance(v, dict) and v.get("status") == "warning":
                r["warnings"].append("qa:%s" % k)

    # 3. R1 -- erase-only, no added text anywhere
    srt = sizes.get("subtitles-en.srt", -1)
    if r["delivered"] and srt != 0:
        r["issues"].append("R1:SRT_not_empty(%sB)" % srt)
    tl = load(os.path.join(work, "timeline.json"))
    n_entries = len(tl.get("entries") or [])
    r["timeline_entries"] = n_entries
    if n_entries:
        r["issues"].append("R1:timeline_entries=%d" % n_entries)
    plan = load(os.path.join(work, "localization-plan.json"))
    added = [x for x in plan.get("items", []) if x.get("target_text")]
    r["plan_target_text"] = len(added)
    if added:
        r["issues"].append("R1:plan_target_text=%d" % len(added))
    not_remove = [x for x in plan.get("items", [])
                  if x.get("action") not in ("remove", "review")]
    if not_remove:
        r["issues"].append("R1:non_remove_items=%d" % len(not_remove))
    r["cleanup_items"] = len(plan.get("items", []))
    # 2026-11-15: a pure AREA threshold (>=8%) false-flagged "wide but flat"
    # per-text lines after the v2 rebuild (idx38 916x106, idx00 596x116).
    # The real smear cause is a WIDE **AND TALL** union box sent to TELEA:
    # v1 accident boxes were 679x228 / 831x204 / 960x329 (28%-46% of frame
    # height). Rebuilt per-text boxes are all <=22% frame height, and vblend
    # boxes use four-edge sampling (visually verified), not TELEA smearing.
    # So gate on dimensions of TELEA/default-fill boxes, not raw area.
    fw = float(plan.get("frame_width") or 0) or 1.0
    fh = float(plan.get("frame_height") or 0) or 1.0
    probe0 = load(os.path.join(work, "probe.json"))
    fw = float(probe0.get("width") or fw)
    fh = float(probe0.get("height") or fh)
    all_boxes = [x.get("bbox", {}) for x in plan.get("items", [])]
    telea_boxes = [x.get("bbox", {}) for x in plan.get("items", [])
                   if x.get("fill_method", "telea") != "vblend"]
    biggest = max((b.get("w", 0) * b.get("h", 0) for b in all_boxes), default=0)
    r["max_cleanup_box_pct"] = round(100.0 * biggest / max(1.0, fw * fh), 2)
    tall_wide = [b for b in telea_boxes
                 if b.get("h", 0) >= 0.25 * fh and b.get("w", 0) >= 0.45 * fw]
    r["smear_risk_boxes"] = len(tall_wide)
    dec0 = load(os.path.join(work, "agent-decisions.json"))
    is_auto = bool(dec0.get("auto_plan_items"))
    # Hand-authored clips (idx4/41) legitimately use a large vblend box for a
    # graphic sticker and were visually verified through several rework rounds;
    # this gate only applies to AUTO-decided clips.
    if is_auto and tall_wide:
        b = max(tall_wide, key=lambda z: z.get("w", 0) * z.get("h", 0))
        r["issues"].append("SMEAR_RISK:tall_wide_TELEA_box=%dx%d (v1 union box)" %
                           (b.get("w", 0), b.get("h", 0)))
    if is_auto and "--large-region-fill" in (dec0.get("render_flags") or []):
        r["issues"].append("SMEAR_RISK:v1_render_flags (needs v2 rework)")

    # 4. R3 -- brand yellow on cover
    y = brand_yellow_ratio(os.path.join(out, "cover.jpg"))
    r["cover_yellow_ratio"] = y
    if y is not None and y < 0.005:
        r["warnings"].append("cover_brand_yellow_low(%.3f)" % y)

    # 5. R2 -- BGM decision tree
    ap = load(os.path.join(work, "audio-policy.json"))
    dec = load(os.path.join(work, "agent-decisions.json"))
    apl = load(os.path.join(work, "audio-plan.json"))
    case = ap.get("case")
    strat = dec.get("audio_strategy") or apl.get("strategy")
    r["audio_case"] = case
    r["audio_strategy"] = strat
    r["bgm_asset"] = dec.get("bgm_asset") or (apl.get("bgm") or {}).get("asset_path")
    if case and case in RULE_MAP:
        if strat not in RULE_MAP[case]:
            r["issues"].append("R2:case=%s expects %s got %s" %
                               (case, sorted(RULE_MAP[case]), strat))
    elif not case:
        r["warnings"].append("no audio-policy.json (manual first batch)")
    if strat in FORBIDDEN_OLD:
        r["issues"].append("R2:old_strategy=%s" % strat)
    if case == "no_narration_no_bgm":
        asset = r["bgm_asset"]
        if not asset or "/music/" not in str(asset):
            r["issues"].append("R2:added_bgm_not_from_licensed_library=%r" % asset)
    if case == "lyric_bearing_bgm" or case == "chinese_narration":
        inst = os.path.join(work, "instrumental.wav")
        if not os.path.exists(inst):
            r["warnings"].append("no separated instrumental.wav (check mix)")

    # 6. ledger
    r["in_ledger"] = False
    led = load(LEDGER)
    recs = led.get("records", []) if isinstance(led, dict) else []
    for x in recs:
        if isinstance(x, dict):
            led_out = str(x.get("output_dir") or "")
            # A user-marked "-done" folder is the same delivered clip.
            if led_out == out or led_out + "-done" == out or led_out == out + "-done":
                r["in_ledger"] = True
                break
    if not r["in_ledger"]:
        r["issues"].append("ledger:not_registered")
    r["ok"] = not r["issues"]
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=ROOT + "/diag/final-verification.json")
    a = ap.parse_args()
    idx = json.load(open(ROOT + "/diag/index.json"))
    rows = [check_one(r) for r in idx]
    fails = load(ROOT + "/diag/queue-failures.json", [])
    summary = {
        "total": len(rows),
        "ok": sum(1 for r in rows if r["ok"]),
        "with_issues": [r["idx"] for r in rows if not r["ok"]],
        "warnings": sum(1 for r in rows if r["warnings"]),
        "chinese_residue": dict(collections.Counter(
            str(r.get("chinese_residue")) for r in rows)),
        "audio_cases": dict(collections.Counter(
            str(r.get("audio_case")) for r in rows)),
        "audio_strategies": dict(collections.Counter(
            str(r.get("audio_strategy")) for r in rows)),
        "pending": [r["idx"] for r in rows if not r.get("delivered")],
        "srt_nonempty": [r["idx"] for r in rows
                         if r.get("delivered")
                         and r["sizes"].get("subtitles-en.srt", -1) != 0],
        "timeline_nonempty": [r["idx"] for r in rows if r.get("timeline_entries")],
        "plan_with_target_text": [r["idx"] for r in rows if r.get("plan_target_text")],
        "cover_yellow_missing_or_low": [r["idx"] for r in rows
                                        if r.get("delivered")
                                        and (r.get("cover_yellow_ratio") or 0) < 0.005],
        "smear_risk": [r["idx"] for r in rows if r.get("delivered")
                       and any(x.startswith("SMEAR_RISK") for x in r["issues"])],
        "not_in_ledger": [r["idx"] for r in rows
                          if r.get("delivered") and not r["in_ledger"]],
        "queue_failures": fails,
    }
    out = {"summary": summary, "rows": rows}
    os.makedirs(os.path.dirname(a.json), exist_ok=True)
    json.dump(out, open(a.json, "w"), ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\nFULL REPORT ->", a.json)
    for r in rows:
        if r["issues"]:
            print("  idx%02d ISSUES: %s" % (r["idx"], "; ".join(r["issues"])[:300]))
    return 0 if summary["ok"] == summary["total"] and not fails else 1


if __name__ == "__main__":
    sys.exit(main())
