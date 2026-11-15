#!/usr/bin/env python
"""One-video orchestrator for the 2026-11-13 batch.

Phases (run separately so the Agent can author semantics in between):
  analyze  -> probe / coarse+dense OCR / tracks / ASR / source-audio diagnosis
             + contact sheet + first/last frame stills
  digest   -> compact JSON of what the Agent must decide (CJK lines, persistent
              items, audio verdict, watermark/ logo evidence)
  render   -> decisions -> contract JSONs -> masks -> render_overlays -> mix
              -> SRT -> final.mp4 -> cover -> post
  qa       -> qa_video + double_check (+ grid/sheet markers for visual review)

Paths always come from index.json by numeric idx; Chinese filenames are never
hand-typed (skill 13.11 rule 2).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.environ.get("LOCALIZER_BATCH_ROOT", os.path.dirname(os.path.abspath(__file__)))
SK = os.environ.get("LOCALIZER_SKILL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(SK, "scripts")
HARNESS = os.environ.get("LOCALIZER_HARNESS", "")
PY = os.environ.get("LOCALIZER_PYTHON", sys.executable)
OUT_ROOT = os.environ.get("LOCALIZER_OUT_ROOT", os.path.expanduser("~/Downloads/tiktok"))

ENV = os.environ.copy()
ENV["PATH"] = (os.environ.get("LOCALIZER_BIN_DIR", os.path.dirname(PY) or ".") + ":" + os.path.expanduser("~/.local/bin")
               + ":/usr/local/bin:/usr/bin:/bin:" + ENV.get("PATH", ""))

sys.path.insert(0, SCRIPTS)
from run_pipeline import sanitize_folder_name  # noqa: E402


def load_index():
    with open(os.path.join(ROOT, "diag", "index.json")) as f:
        return {int(r["idx"]): r for r in json.load(f)}


def paths(idx):
    rec = load_index()[idx]
    video = rec["path"]
    out = os.path.join(OUT_ROOT, sanitize_folder_name(os.path.basename(video)))
    return video, out, os.path.join(out, "work")


def run(script, args, work, tag=""):
    cmd = [PY, os.path.join(SCRIPTS, script)] + args
    t0 = time.time()
    print("  > %s %s" % (script, tag), flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, env=ENV, stdin=subprocess.DEVNULL)
    tail = [l for l in (r.stdout or "").splitlines() if l.strip()][-4:]
    for l in tail:
        print("    " + l[:180], flush=True)
    if r.returncode != 0:
        print("    !! rc=%d %s" % (r.returncode, (r.stderr or "")[-400:]), flush=True)
    print("    %.1fs" % (time.time() - t0), flush=True)
    return r.returncode


def exists(work, name):
    return os.path.exists(os.path.join(work, name))


def prepare_instrumental(work, video):
    """User rule 2026-11-14 (lyric-bearing source BGM).

    If the ORIGINAL track carries lyrics (Chinese pop song / sung vocals), the
    correct fix is NOT "mute everything + drop in a library track": separate the
    vocals with demucs and keep the ORIGINAL instrumental bed, which by definition
    has the same tempo, drops and arrangement as the edit. No third-party BGM,
    no TTS. audio_processor's `tts_keep_instrumental` consumes instrumental.wav.
    """
    ap = os.path.join(work, "audio-plan.json")
    plan = json.load(open(ap)) if os.path.exists(ap) else {}
    if plan.get("strategy") != "tts_keep_instrumental":
        return 0
    inst = os.path.join(work, "instrumental.wav")
    if os.path.exists(inst):
        print("  = instrumental.wav already present (demucs reuse)", flush=True)
        return 0
    src = os.path.join(work, "source_audio.wav")
    if not os.path.exists(src):
        ffmpeg = os.path.expanduser("~/.local/bin/ffmpeg")
        subprocess.run([ffmpeg, "-v", "error", "-y", "-i", video, "-vn",
                        "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", src],
                       env=ENV, check=False)
    if not os.path.exists(src):
        print("!! source_audio.wav missing; cannot separate", flush=True)
        return 2
    dem = os.path.join(work, "demucs")
    print("  > demucs --two-stems=vocals (keeping ORIGINAL instrumental rhythm)",
          flush=True)
    env2 = ENV.copy()
    env2["OMP_NUM_THREADS"] = "4"
    t0 = time.time()
    r = subprocess.run([PY, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs",
                        "-o", dem, src], capture_output=True, text=True, env=env2,
                       stdin=subprocess.DEVNULL)
    print("    %.1fs rc=%d %s" % (time.time() - t0, r.returncode,
                                  (r.stdout or "")[-300:].replace("\n", " ")), flush=True)
    if r.returncode != 0:
        print("    !! demucs: %s" % (r.stderr or "")[-600:], flush=True)
        return 2
    hits = glob.glob(os.path.join(dem, "**", "no_vocals.wav"), recursive=True)
    if not hits:
        print("!! demucs produced no no_vocals.wav", flush=True)
        return 2
    shutil.copyfile(hits[0], inst)
    print("  = instrumental.wav <- %s (%d bytes)" %
          (hits[0], os.path.getsize(inst)), flush=True)
    return 0


def phase_analyze(idx):
    video, out, work = paths(idx)
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "pipeline-config.json"), "w") as f:
        json.dump({"input": video, "profile": "nba", "idx": idx}, f, ensure_ascii=False, indent=2)
    print("=== ANALYZE idx=%02d -> %s" % (idx, out), flush=True)
    steps = [
        ("video_probe.py", ["--video", video, "--work-dir", work], "probe.json"),
        ("extract_frames.py", ["--video", video, "--work-dir", work], "frames-index.json"),
        ("ocr_video.py", ["--work-dir", work, "--pass", "coarse"], "ocr.json"),
        ("detect_text_regions.py", ["--work-dir", work, "--emit-dense-windows"], "dense-windows.json"),
        ("extract_frames.py", ["--video", video, "--work-dir", work,
                               "--dense-fps", "2.0",
                               "--dense-windows", os.path.join(work, "dense-windows.json")], None),
        ("ocr_video.py", ["--work-dir", work, "--pass", "dense"], None),
        ("track_text.py", ["--work-dir", work], "text-tracks.json"),
        ("audio_processor.py", ["asr-only", "--video", video, "--work-dir", work], "asr.json"),
    ]
    OCR_STEPS = {"extract_frames.py", "ocr_video.py",
                 "detect_text_regions.py", "track_text.py"}
    ocr_cached = exists(work, "text-tracks.json") and exists(work, "ocr.json")
    for script, args, marker in steps:
        # 2026-11-14 full-batch fix: a clip that already has the complete OCR
        # pipeline must NOT re-run coarse+dense OCR just because asr.json was
        # missing. idx2 (380s) wasted 20 minutes on this.
        if ocr_cached and script in OCR_STEPS:
            print("  = skip %s (OCR pipeline cached)" % script, flush=True)
            continue
        if marker and exists(work, marker) and script != "ocr_video.py":
            print("  = skip %s (cached)" % script, flush=True)
            continue
        run(script, args, work)
    # source-audio verdict: reuse the global batch result when available
    import shutil
    batch_profile = os.path.join(ROOT, "diag", "audio_%02d" % idx, "source-audio-profile.json")
    dst_profile = os.path.join(work, "source-audio-profile.json")
    if os.path.exists(batch_profile):
        shutil.copyfile(batch_profile, dst_profile)
        print("  = source-audio verdict reused from batch diagnosis", flush=True)
    elif not os.path.exists(dst_profile):
        run("analyze_source_audio.py", ["--video", video, "--work-dir", work], work, "audio")
    # visual evidence: full contact sheet + explicit frame 0 + tail frames
    sheet = os.path.join(work, "sheet_full.jpg")
    if not os.path.exists(sheet):
        run("contact_sheet.py", ["--video", video, "--out", sheet], work, "contact sheet")
    probe = json.load(open(os.path.join(work, "probe.json")))
    dur = float(probe.get("duration_sec") or probe.get("duration") or 0)
    ffmpeg = os.path.expanduser("~/.local/bin/ffmpeg")
    for label, t in (("frame0", 0.0), ("frame_mid", dur * 0.5), ("frame_tail", max(0, dur - 1.2))):
        dst = os.path.join(work, label + ".jpg")
        subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", "%.3f" % t, "-i", video,
                        "-frames:v", "1", "-q:v", "2", dst], env=ENV, timeout=90)
    # USER RULE 2026-11-14: attach the new BGM decision-tree verdict so the
    # Agent never falls back to the old "mute + library track" shortcut.
    pol = subprocess.run([PY, os.path.join(ROOT, "audio_policy.py"), "--work", work],
                         capture_output=True, text=True, env=ENV)
    print("  " + (pol.stdout or pol.stderr or "").strip()[:300], flush=True)
    print("=== ANALYZE DONE idx=%02d" % idx, flush=True)


def phase_digest(idx):
    import re
    video, out, work = paths(idx)
    CJK2 = re.compile(r"[\u4e00-\u9fff]{2,}")
    probe = json.load(open(os.path.join(work, "probe.json")))
    dur = float(probe.get("duration_sec") or probe.get("duration") or 0)
    ocr = json.load(open(os.path.join(work, "ocr.json")))
    groups = {}
    for d in ocr.get("detections", []):
        txt = (d.get("text") or "").strip()
        if not CJK2.search(txt):
            continue
        k = re.sub(r"[^\w\u4e00-\u9fff]", "", txt)
        g = groups.setdefault(k, {"text": txt, "hits": []})
        g["hits"].append({"t": round(float(d.get("timestamp_sec", 0)), 2),
                          "bbox": d.get("bbox"), "conf": round(float(d.get("confidence", 0)), 3)})
    lines = []
    for k, g in groups.items():
        ts = [h["t"] for h in g["hits"]]
        xs = [h["bbox"]["x"] for h in g["hits"] if h["bbox"]]
        ys = [h["bbox"]["y"] for h in g["hits"] if h["bbox"]]
        xe = [h["bbox"]["x"] + h["bbox"]["w"] for h in g["hits"] if h["bbox"]]
        ye = [h["bbox"]["y"] + h["bbox"]["h"] for h in g["hits"] if h["bbox"]]
        lines.append({
            "text": g["text"], "n": len(ts), "t0": min(ts), "t1": max(ts),
            "bbox": {"x": min(xs), "y": min(ys), "w": max(xe) - min(xs), "h": max(ye) - min(ys)},
            "conf": round(sum(h["conf"] for h in g["hits"]) / len(ts), 3),
            "persistent": (max(ts) - min(ts)) > 0.6 * max(dur, 1),
        })
    lines.sort(key=lambda r: (-int(r["persistent"]), -r["n"]))
    asr = json.load(open(os.path.join(work, "asr.json"))) if exists(work, "asr.json") else {}
    audio = (json.load(open(os.path.join(work, "source-audio-profile.json")))
             if exists(work, "source-audio-profile.json") else {})
    digest = {
        "idx": idx, "name": os.path.basename(video), "out": out, "work": work,
        "duration_sec": dur,
        "resolution": probe.get("resolution") or probe.get("width_height"),
        "cjk_lines": lines,
        "asr": {k: asr.get(k) for k in ("status", "language_detected", "has_chinese_narration",
                                       "has_foreign_narration", "chinese_ratio")},
        "asr_segments_sample": [s.get("text") for s in (asr.get("segments") or [])[:6]],
        "audio_profile": {k: audio.get(k) for k in ("has_speech", "speech_lang", "has_music",
                                                    "low_band_ratio", "recommended_strategy",
                                                    "evidence", "notes")},
        "evidence_images": [os.path.join(work, x) for x in
                            ("sheet_full.jpg", "frame0.jpg", "frame_mid.jpg", "frame_tail.jpg")],
    }
    dst = os.path.join(ROOT, "diag", "digest_%02d.json" % idx)
    with open(dst, "w") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)
    print(json.dumps(digest, ensure_ascii=False, indent=2)[:9000])
    print("\nDIGEST ->", dst)


def write_fast_masks(work, plan, probe):
    """Batch-mode mask index, without generate_masks.py's per-item video seeks.

    render_overlays.py only ever reads masks/masks-index.json -> masks[] with
    bbox + start/end. generate_masks.py additionally (a) calls
    expand_bbox_to_plate() per item = 6 random video seeks per item, and (b)
    with --render-images writes one full-frame PNG per item. On idx02 (584
    items, 1080x1080, 380s source) that stalled >16min with ZERO masks written.
    auto_decide.py already pads OCR union boxes by 8px, so plate expansion is
    redundant for erase-only cleanup; any leftover Chinese is caught by the QA
    chinese_residue hard gate and repaired by run_queue.repair_from_qa().
    """
    md = os.path.join(work, "masks")
    os.makedirs(md, exist_ok=True)
    fw = int(probe.get("width") or 1080)
    fh = int(probe.get("height") or 1920)
    masks = []
    for it in plan.get("items", []):
        action = it.get("action")
        if action == "review":
            continue
        if not (action in ("remove", "replace") or
                (action == "translate" and it.get("source_text_removal"))):
            continue
        bb = it.get("bbox") or {}
        x, y = max(0, int(bb.get("x", 0))), max(0, int(bb.get("y", 0)))
        w = min(int(bb.get("w", 0)), fw - x)
        h = min(int(bb.get("h", 0)), fh - y)
        if w <= 0 or h <= 0:
            continue
        masks.append({
            "item_id": it.get("item_id"),
            "category": it.get("category"),
            "action": action,
            "bbox": {"x": x, "y": y, "w": w, "h": h},
            "start_time": float(it.get("start_time", 0) or 0),
            "end_time": float(it.get("end_time", 0) or 0),
            "removal_method": it.get("removal_method", ["inpaint"]),
            "confidence": it.get("confidence", 1.0),
        })
    with open(os.path.join(md, "masks-index.json"), "w") as f:
        json.dump({"status": "ok", "confidence": 0.9, "masks": masks,
                   "skipped": [], "total_items": len(plan.get("items", [])),
                   "masks_generated": len(masks),
                   "frame_size": {"width": fw, "height": fh},
                   "mode": "batch-fast"}, f, ensure_ascii=False, indent=2)
    print("  fast masks: %d bboxes (no video seeks / no PNGs)" % len(masks), flush=True)


def phase_render(idx):
    video, out, work = paths(idx)
    print("=== RENDER idx=%02d" % idx, flush=True)
    # 1. Agent decisions -> contract JSONs (harness builder, reused from 11-02 batch)
    dec = os.path.join(work, "agent-decisions.json")
    if not os.path.exists(dec):
        print("!! missing agent-decisions.json", flush=True)
        return 2
    r = subprocess.run([PY, os.path.join(HARNESS, "build_semantics.py"), work],
                       capture_output=True, text=True, env=ENV)
    print((r.stdout or "")[-2500:], flush=True)
    if r.returncode != 0:
        print((r.stderr or "")[-800:], flush=True)
        return r.returncode
    # 1a. FULL-BATCH mode (auto_decide.py): the Agent-authored cleanup items are
    # the single source of truth. build_semantics' own OCR grouping is bypassed
    # (2s time buckets + vertical bands follow drifting marks better than one
    # giant union box), and the timeline is forced empty so the SRT stays empty
    # -> zero overlap / oversized-subtitle risk (USER RULE 2026-11-14).
    dec_data = json.load(open(dec))
    if dec_data.get("auto_plan_items"):
        pp = os.path.join(work, "localization-plan.json")
        plan = json.load(open(pp))
        plan["items"] = list(dec_data.get("extra_items") or [])
        with open(pp, "w") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        with open(os.path.join(work, "timeline.json"), "w") as f:
            json.dump({"entries": []}, f)
        print("  auto-plan: %d cleanup items, timeline forced empty" %
              len(plan["items"]), flush=True)
    # 1b. exact-match translation overrides.
    # build_semantics uses containment matching, so a short caption that is a
    # prefix of a longer one silently inherits the long translation. Fix by
    # exact source_text equality before masks are generated.
    dec_data = json.load(open(dec))
    exact = dec_data.get("exact_translations") or {}
    if exact:
        pp = os.path.join(work, "localization-plan.json")
        plan = json.load(open(pp))
        for it in plan.get("items", []):
            src = (it.get("source_text") or "").strip()
            if src in exact:
                it["target_text"] = exact[src]
                it["action"] = "translate"
                print("  exact-fix %s -> %s" % (src, exact[src]), flush=True)
        with open(pp, "w") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
    # 1c. timeline must exist BEFORE render_overlays --sync-timeline, otherwise
    # the renderer skips the sync and subtitles-en.srt comes out empty.
    run("build_timeline.py", ["--work-dir", work], work, "timeline seed")
    # 2. masks. Batch mode uses the fast index (no per-item video seeks);
    #    fine-grained/manual clips keep the official plate-expansion pipeline.
    if dec_data.get("auto_plan_items"):
        probe = json.load(open(os.path.join(work, "probe.json")))
        write_fast_masks(work, plan, probe)
    else:
        run("generate_masks.py", ["--work-dir", work, "--video", video,
                                  "--render-images"], work)
    # 3. official overlay renderer (single pass: ROI inpaint; erase-only by
    #    user rule 2026-11-14: NO extra English title/hook/subtitle on picture)
    cleaned = os.path.join(work, "cleaned.mp4")
    render_flags = list(dec_data.get("render_flags") or [])
    render_cmd = ["--video", video, "--work-dir", work, "--output", cleaned,
                  "--profile", "nba", "--crf", "18", "--preset", "veryfast",
                  "--logo-ratio", "0.32", "--sync-timeline",
                  "--no-extra-text", "--x264-threads", "4"] + render_flags
    # clean-base.mp4 is a review-only duplicate encode; batch mode has no
    # per-clip human review, so skip the second ffmpeg sink to halve I/O.
    if not dec_data.get("auto_plan_items"):
        render_cmd += ["--save-clean-base", os.path.join(work, "clean-base.mp4")]
    run("render_overlays.py", render_cmd, work, "overlays")
    if not os.path.exists(cleaned) or os.path.getsize(cleaned) < 10000:
        print("!! cleaned.mp4 missing", flush=True)
        return 2
    # 4. audio mix (strategy from agent-decisions / source-audio-profile)
    rc = prepare_instrumental(work, video)
    if rc:
        return rc
    run("audio_processor.py", ["mix", "--video", cleaned, "--work-dir", work,
                               "--output-dir", out,
                               "--audio-plan", os.path.join(work, "audio-plan.json"),
                               "--timeline", os.path.join(work, "timeline.json")], work, "mix")
    # 5. SRT sidecar only (external captions; picture stays text-free by rule)
    run("render_subtitles.py", ["--work-dir", work, "--output-dir", out], work, "srt")
    # 6. cover + post
    run("generate_cover.py", ["--work-dir", work, "--output-dir", out, "--profile", "nba"],
        work, "cover")
    run("generate_post.py", ["--work-dir", work, "--output-dir", out, "--profile", "nba"],
        work, "post")
    print("=== RENDER DONE idx=%02d -> %s" % (idx, out), flush=True)
    return 0


def phase_qa(idx):
    video, out, work = paths(idx)
    print("=== QA idx=%02d" % idx, flush=True)
    run("qa_video.py", ["--work-dir", work, "--output-dir", out], work, "qa")
    dec = os.path.join(work, "agent-decisions.json")
    if os.path.exists(dec) and json.load(open(dec)).get("auto_plan_items"):
        # FULL-BATCH mode: double_check.py runs a SECOND full-video 2fps OCR
        # pass purely to produce cover-grid/frame-sheet images for per-clip
        # human review, which batch mode deliberately does not do. qa_video.py
        # already enforced the chinese_residue / yellow_arrow hard gates.
        # Skipping it saves several minutes per clip with no loss of gating.
        print("  = batch mode: skip duplicate double_check OCR scan "
              "(per-clip visual review stays unrecorded -> QA cover warning)",
              flush=True)
        print("=== QA DONE (batch: OCR hard gate only) ===", flush=True)
        return 0
    dc = os.path.join(work, "double-check.json")
    r = subprocess.run([PY, os.path.join(SCRIPTS, "double_check.py"),
                        "--output-dir", out, "--json", dc,
                        "--shots-dir", os.path.join(work, "dc-shots"),
                        "--cover-mark", "--frame-mark"],
                       capture_output=True, text=True, env=ENV)
    print((r.stdout or "")[-3000:], flush=True)
    if r.returncode != 0:
        print((r.stderr or "")[-600:], flush=True)
    print("=== QA DONE (visual review still required) ===", flush=True)
    return r.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True,
                    choices=["analyze", "digest", "render", "qa", "paths"])
    ap.add_argument("--idx", type=int, required=True)
    a = ap.parse_args()
    if a.phase == "paths":
        print(json.dumps(paths(a.idx), ensure_ascii=False))
    else:
        {"analyze": phase_analyze, "digest": phase_digest,
         "render": phase_render, "qa": phase_qa}[a.phase](a.idx)


if __name__ == "__main__":
    main()
