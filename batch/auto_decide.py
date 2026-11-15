#!/usr/bin/env python
"""Auto-author agent-decisions.json for the remaining 2026-11-14 batch.

USER RULES (2026-11-14) enforced here:
  * picture = ERASE ONLY. No added English title / hook / caption / logo.
    (render_overlays.py --no-extra-text does the drawing suppression; this
    script additionally emits ZERO target_text so timeline/SRT stay empty,
    which removes any overlap / oversized-subtitle risk.)
  * BGM decision tree from audio_policy.py:
      lyric-bearing BGM   -> tts_keep_instrumental (demucs, ORIGINAL rhythm)
      Chinese narration   -> tts_keep_instrumental + flag for English dub
      English narration/BGM -> keep_original
      neither             -> duck_original_add_bgm, licensed local library only
  * brand yellow #FFE800 lives on the COVER (profile nba) and on
    title/hook/logo drawn by the skill -- NOT on source burnt-in captions.
    SKILL.md §8 explicitly says: "字幕仍用白色" (subtitles stay white).

Cleanup items are generated from OCR detections with 2-second time buckets
and three vertical bands, so drifting watermarks are followed instead of
being covered by one giant full-width union box.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import subprocess
import sys

ROOT = os.environ.get("LOCALIZER_BATCH_ROOT", os.path.dirname(os.path.abspath(__file__)))
SK = os.environ.get("LOCALIZER_SKILL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_ROOT = os.environ.get("LOCALIZER_OUT_ROOT", os.path.expanduser("~/Downloads/tiktok"))
PY = os.environ.get("LOCALIZER_PYTHON", sys.executable)
CJK = re.compile(r"[\u4e00-\u9fff]")
KNOWN_IDS = {"hoopsryoku", "sunsource", "seza", "hoopdynastydaily"}

# Hand-authored English cover copy. Headline <=5 words, stat is a short line.
# Keys are the numeric idx from diag/index.json (Chinese filenames never typed).
COVER = {
    0:  ("WHEN IT WAS FUN", "KOBE, SHAQ, LEBRON & THE CREW", ["Kobe Bryant", "Shaquille O'Neal", "LeBron James"]),
    1:  ("KING JAMES 1.0", "THE FIRST CLEVELAND ERA", ["LeBron James"]),
    2:  ("UNANIMOUS MVP", "403 THREES, ONE REVOLUTION", ["Stephen Curry", "Golden State Warriors"]),
    3:  ("NBA CLASSICS", "UNFORGETTABLE HIGHLIGHTS", ["NBA legends"]),
    5:  ("61 AT THE GARDEN", "31-19 FG / 20-20 FT", ["Kobe Bryant", "New York Knicks"]),
    6:  ("DIRK 2011", "THE COMEBACK RING", ["Dirk Nowitzki", "Dallas Mavericks"]),
    7:  ("81 POINTS", "20 YEARS LATER", ["Kobe Bryant"]),
    8:  ("TOO BIG TOO FAST", "PRIME DERRICK ROSE", ["Derrick Rose"]),
    9:  ("HUMAN ALLEY-OOP", "BEST LOB FINISHES", ["NBA dunkers"]),
    11: ("THE DUCK KING", "SMALL-ON-BIG MASTERCLASS", ["Payton Pritchard", "Boston Celtics"]),
    12: ("KD HANDLES", "7-FOOT GUARD SKILLS", ["Kevin Durant"]),
    13: ("NEVER BACK DOWN", "KOBE REFUSED TO LOSE", ["Kobe Bryant"]),
    14: ("ALL-STAR ICONS", "SHAQ'S WILDEST MOMENTS", ["Shaquille O'Neal"]),
    15: ("KG TOP 10", "CAREER PLAYS", ["Kevin Garnett"]),
    16: ("HALF MAN HALF GOD", "VINCE CARTER IN TORONTO", ["Vince Carter", "Toronto Raptors"]),
    18: ("THE WORM", "RODMAN'S WILDEST PLAYS", ["Dennis Rodman"]),
    19: ("AIR CANADA", "RAPTORS-ERA VINCE", ["Vince Carter", "Toronto Raptors"]),
    20: ("THE FLASH", "WADE 08-09 TOP 20", ["Dwyane Wade", "Miami Heat"]),
    21: ("SHAQ'S BIGGEST GAME", "HE BOUGHT HIS OWN TICKET", ["Shaquille O'Neal", "LA Clippers"]),
    22: ("CURRY'S RHYTHM", "SOFT TOUCH AT THE RIM", ["Stephen Curry"]),
    23: ("CURRY FINISH PACKAGE", "RHYTHM OVER POWER", ["Stephen Curry"]),
    24: ("CHICAGO vs UTAH", "THE CLASSIC FINALS REEL", ["Michael Jordan", "Karl Malone", "Chicago Bulls"]),
    25: ("JORDAN & MALONE", "EVERY ICONIC POSSESSION", ["Michael Jordan", "Karl Malone", "Utah Jazz"]),
    26: ("WEMBY IS DIFFERENT", "THE NEXT FORM OF BASKETBALL", ["Victor Wembanyama", "San Antonio Spurs"]),
    27: ("MAMBA OUT", "60 IN THE FAREWELL", ["Kobe Bryant"]),
    28: ("2007 DUNK CONTEST", "THE STRONGEST JUDGES", ["NBA All-Star", "Vince Carter", "Michael Jordan"]),
    29: ("8 AND 24", "KOBE'S JERSEY SPEECH", ["Kobe Bryant", "Los Angeles Lakers"]),
    30: ("WEMBY TOP 15", "CAREER HIGHLIGHTS SO FAR", ["Victor Wembanyama"]),
    31: ("T-MAC'S GIFT", "BUILT DIFFERENT", ["Tracy McGrady", "Orlando Magic"]),
    32: ("KOBE vs LEBRON", "A DIFFERENT EDGE", ["Kobe Bryant", "LeBron James"]),
    33: ("KOBE TOP 10", "CAREER PLAYS PART 1", ["Kobe Bryant"]),
    34: ("THE FAREWELL", "TRIBUTES TO THE MAMBA", ["Kobe Bryant"]),
    35: ("NASH TOP 10", "PURE PASSING GENIUS", ["Steve Nash"]),
    36: ("THUNDER vs WARRIORS", "CURRY TAKES OVER", ["Stephen Curry", "Kevin Durant", "Russell Westbrook"]),
    37: ("JA MORANT", "TALENT VS CONTROVERSY", ["Ja Morant", "Memphis Grizzlies"]),
    38: ("LEBRON & WADE", "THE CONNECTION REEL", ["LeBron James", "Dwyane Wade"]),
    39: ("8 WAS UNREAL", "35.4 PPG / 81 POINTS", ["Kobe Bryant", "Los Angeles Lakers"]),
    40: ("NO BALL HOG", "PURE TEAM BASKETBALL", ["NBA team play"]),
}


def load(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


def paths(idx):
    sys.path.insert(0, SK + "/scripts")
    from run_pipeline import sanitize_folder_name
    rec = {int(r["idx"]): r for r in json.load(open(ROOT + "/diag/index.json"))}[idx]
    out = os.path.join(OUT_ROOT, sanitize_folder_name(os.path.basename(rec["path"])))
    return rec["path"], out, os.path.join(out, "work")


def pick_bgm(video, idx):
    """Last-resort licensed BGM, chosen by tempo + anti-cymbal/anti-loop gates."""
    out = os.path.join(ROOT, "diag", "bgm_pick_%02d.json" % idx)
    r = subprocess.run([PY, ROOT + "/bgm_review.py", "--video", video,
                        "--top", "3", "--out", out],
                       capture_output=True, text=True, timeout=900)
    d = load(out)
    ok = d.get("approved") or []
    if not ok:
        print("  !! bgm_review found no approved track (rc=%d)" % r.returncode, flush=True)
        return None, d
    return ok[0].get("path"), d


def cleanup_items(work, duration, frame_w, frame_h):
    """Erase-only cleanup items, grouped by POSITION then by time continuity.

    A persistent banner / channel mark sits at a fixed spot for the whole clip
    -> ONE box covering its whole span (the same shape as hand-authored plans).
    Only genuinely DRIFTING marks get 2s follow buckets. The old "every 2s
    bucket gets a box" rule produced 584 items on idx2 (380s) and made masks,
    cover and render crawl. 2026-11-14 full-batch fix.
    """
    ocr = load(os.path.join(work, "ocr.json"))
    dets = [d for d in (ocr.get("detections") or []) if d.get("bbox")]
    cjk_texts = []
    # high-frequency short latin IDs (HoopsRyoku etc.) are third-party marks
    freq = {}
    for d in dets:
        t = (d.get("text") or "").strip()
        if CJK.search(t):
            cjk_texts.append(t)
        elif 2 <= len(t) <= 16 and " " not in t and re.search(r"[A-Za-z]", t) \
                and float(d.get("confidence", 0)) >= 0.85:
            freq[t] = freq.get(t, 0) + 1
    id_words = {t.lower() for t, n in freq.items() if n >= 10}
    id_words |= KNOWN_IDS

    hits = []
    for d in dets:
        txt = (d.get("text") or "").strip()
        is_cjk = bool(CJK.search(txt))
        is_id = (not is_cjk) and txt.lower() in id_words
        if not (is_cjk or is_id):
            continue
        bb = d["bbox"]
        t = float(d.get("timestamp_sec", 0))
        if not (0 <= t <= duration + 0.5):
            continue
        cx = bb["x"] + bb["w"] / 2.0
        cy = bb["y"] + bb["h"] / 2.0
        band = 0 if cy < 0.30 * frame_h else (2 if cy > 0.70 * frame_h else 1)
        xb = int(cx // max(1.0, 0.20 * frame_w))
        hits.append((t, bb, txt, band, xb))
    groups = {}
    for h in hits:
        groups.setdefault((h[3], h[4]), []).append(h)

    def clusters(hits, fw, fh):
        """Split spatially distant detections instead of one giant union box.

        A 2s bucket often contains a top-left tag AND a bottom-right watermark;
        a single union bbox would inpaint half the picture. Merge intervals with
        small horizontal gaps first, then split each group by vertical gaps.
        """
        ivs = sorted([(h[1]["x"], h[1]["x"] + h[1]["w"],
                       h[1]["y"], h[1]["y"] + h[1]["h"], h[2]) for h in hits])
        xs = []
        cur, cur_x2 = [], None
        for iv in ivs:
            if cur and iv[0] - cur_x2 > 0.10 * fw:
                xs.append(cur)
                cur, cur_x2 = [], None
            cur.append(iv)
            cur_x2 = max(cur_x2 or iv[1], iv[1])
        if cur:
            xs.append(cur)
        out = []
        for g in xs:
            sub, cur_y2 = [], None
            for iv in sorted(g, key=lambda z: z[2]):
                if sub and iv[2] - cur_y2 > 0.07 * fh:
                    out.append(sub)
                    sub, cur_y2 = [], None
                sub.append(iv)
                cur_y2 = max(cur_y2 or iv[3], iv[3])
            if sub:
                out.append(sub)
        # final guard: very tall clusters -> horizontal strips
        final = []
        for c in out:
            y0 = min(z[2] for z in c)
            y1 = max(z[3] for z in c)
            if y1 - y0 > 0.22 * fh:
                strip = 0.16 * fh
                bands = {}
                for z in c:
                    k = int((z[2] - y0) // strip)
                    bands.setdefault(k, []).append(z)
                final.extend(list(bands.values()))
            else:
                final.append(c)
        return final

    def emit(c, t0, t1, tag):
        x0 = max(0, min(z[0] for z in c) - 8)
        y0 = max(0, min(z[2] for z in c) - 8)
        x1 = min(frame_w, max(z[1] for z in c) + 8)
        y1 = min(frame_h, max(z[3] for z in c) + 8)
        if x1 - x0 < 4 or y1 - y0 < 4:
            return
        items.append({
            "item_id": tag,
            "category": "watermark",     # STATIC -> never enters timeline / SRT
            "action": "remove",
            "confidence": 1.0,
            "bbox": {"x": int(x0), "y": int(y0), "w": int(x1 - x0), "h": int(y1 - y0)},
            "start_time": round(t0, 2),
            "end_time": round(max(t1, t0 + 0.3), 2),
            "source_text": " / ".join(sorted({z[4] for z in c}))[:60],
            "target_text": None,
            "source_text_removal": True,
            "removal_method": ["inpaint"],
            "replacement": None,
            "reason": "auto erase-only (%s), position-grouped" % ("drifting" if tag.startswith("D") else "static"),
        })

    items = []
    for (band, xb), hs in sorted(groups.items()):
        cxs = [h[1]["x"] + h[1]["w"] / 2.0 for h in hs]
        drifting = (max(cxs) - min(cxs) > 0.18 * frame_w) and len(hs) >= 6
        if drifting:
            buckets = {}
            for h in hs:
                buckets.setdefault(int(h[0] // 2.0), []).append(h)
            for b, bh in sorted(buckets.items()):
                for ci, c in enumerate(clusters(bh, frame_w, frame_h)):
                    emit(c, b * 2.0, min(duration, (b + 1) * 2.0 + 0.20),
                         "D%03d_%d_%d" % (b, band, ci))
            continue
        # static: split into continuous time segments (gap > 4s starts a new one)
        hs.sort(key=lambda z: z[0])
        segs, cur, last = [], [], None
        for h in hs:
            if cur and h[0] - last > 4.0:
                segs.append(cur)
                cur = []
            cur.append(h)
            last = h[0]
        if cur:
            segs.append(cur)
        for si, seg in enumerate(segs):
            st = max(0.0, min(h[0] for h in seg) - 0.6)
            en = min(duration, max(h[0] for h in seg) + 0.9)
            for ci, c in enumerate(clusters(seg, frame_w, frame_h)):
                emit(c, st, en, "S%02d_%d_%02d_%d" % (xb, band, si, ci))
    return items, sorted(set(cjk_texts)), sorted(id_words)


def cleanup_items_v2(work, duration, frame_w, frame_h):
    """Per-TEXT erase boxes with v1's time coverage -- visual-QA fix 2026-11-14.

    v1 grouped every detection in a (vertical band, x bucket) into ONE static
    union box: idx02 got a 679x228 box active for all 380s, so
    inpaint_regions() painted that area with a low-frequency four-edge blend on
    EVERY frame -> a clearly visible smeared band. A/B/C static preview proved
    small per-detection TELEA boxes are visually the cleanest because the real
    surrounding texture stays intact.

    A first v2 attempt used 2s buckets, which removed the smear but left Chinese
    visible (OCR sampling gaps). Final design = v1's continuous time segments
    (full coverage, no gaps) + per-detection small boxes (no smear). Drifting
    marks still get 2s follow buckets. A hard area cap means no giant box can
    ever reach the renderer.
    """
    ocr = load(os.path.join(work, "ocr.json"))
    dets = [d for d in (ocr.get("detections") or []) if d.get("bbox")]
    cjk_texts, freq = [], {}
    for d in dets:
        t = (d.get("text") or "").strip()
        if CJK.search(t):
            cjk_texts.append(t)
        elif 2 <= len(t) <= 16 and " " not in t and re.search(r"[A-Za-z]", t) \
                and float(d.get("confidence", 0)) >= 0.85:
            freq[t] = freq.get(t, 0) + 1
    id_words = {t.lower() for t, n in freq.items() if n >= 10} | KNOWN_IDS

    hits = []
    for d in dets:
        txt = (d.get("text") or "").strip()
        is_cjk = bool(CJK.search(txt))
        is_id = (not is_cjk) and txt.lower() in id_words
        if not (is_cjk or is_id):
            continue
        bb = d["bbox"]
        t = float(d.get("timestamp_sec", 0))
        if not (0 <= t <= duration + 0.5) or bb.get("w", 0) <= 0 or bb.get("h", 0) <= 0:
            continue
        cx = bb["x"] + bb["w"] / 2.0
        cy = bb["y"] + bb["h"] / 2.0
        band = 0 if cy < 0.30 * frame_h else (2 if cy > 0.70 * frame_h else 1)
        xb = int(cx // max(1.0, 0.20 * frame_w))
        hits.append((t, bb, txt, band, xb))
    groups = {}
    for h in hits:
        groups.setdefault((h[3], h[4]), []).append(h)

    # A wide, THIN line of Chinese (811x38 on idx03) is harmless to inpaint:
    # TELEA fills it from the rows immediately above/below. The smear came from
    # wide AND TALL union boxes (679x228 = 13% of the frame). So cap AREA and
    # HEIGHT, not width. Capping width at 0.6fw silently dropped the main
    # banner text on idx05/02/03 -> Chinese left visible (caught in preview).
    MAX_AREA = 0.05 * frame_w * frame_h
    MAX_W = 0.92 * frame_w
    MAX_H = 0.22 * frame_h

    def too_big(bx):
        return ((bx[2] - bx[0]) * (bx[3] - bx[1]) > MAX_AREA
                or (bx[2] - bx[0]) > MAX_W or (bx[3] - bx[1]) > MAX_H)

    def iou(a, b):
        ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        ua = ((a[2] - a[0]) * (a[3] - a[1])
              + (b[2] - b[0]) * (b[3] - b[1]) - inter)
        return inter / ua if ua > 0 else 0.0

    def merge_dupes(boxes):
        """Fold repeated sightings of the SAME text; never merge distant ones."""
        out = []
        for b in sorted(boxes, key=lambda z: (z[1], z[0])):
            hit = None
            for j, m in enumerate(out):
                nb = (min(b[0], m[0]), min(b[1], m[1]),
                      max(b[2], m[2]), max(b[3], m[3]))
                if too_big(nb) or iou(b, m) < 0.30:
                    continue
                hit = (j, nb)
                break
            if hit:
                out[hit[0]] = hit[1]
            else:
                out.append(b)
        return out

    items = []

    def emit_box(box, t0, t1, tag):
        x0, y0 = max(0, int(box[0]) - 6), max(0, int(box[1]) - 6)
        x1, y1 = min(frame_w, int(box[2]) + 6), min(frame_h, int(box[3]) + 6)
        if x1 - x0 < 4 or y1 - y0 < 4 or too_big((x0, y0, x1, y1)):
            return
        items.append({
            "item_id": tag, "category": "watermark", "action": "remove",
            "confidence": 1.0,
            "bbox": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
            "start_time": round(max(0.0, t0), 2),
            "end_time": round(min(duration, max(t1, t0 + 0.3)), 2),
            "source_text": "", "target_text": None,
            "source_text_removal": True, "removal_method": ["inpaint"],
            "replacement": None,
            "reason": "auto erase-only v2 (per-text box, v1 time coverage)",
        })

    def emit_hits(hs, t0, t1, prefix):
        boxes = [(h[1]["x"], h[1]["y"], h[1]["x"] + h[1]["w"],
                  h[1]["y"] + h[1]["h"]) for h in hs]
        for k, box in enumerate(merge_dupes(boxes)):
            emit_box(box, t0, t1, "%s_%02d" % (prefix, k))

    for (band, xb), hs in sorted(groups.items()):
        cxs = [h[1]["x"] + h[1]["w"] / 2.0 for h in hs]
        drifting = (max(cxs) - min(cxs) > 0.18 * frame_w) and len(hs) >= 6
        if drifting:
            buckets = {}
            for h in hs:
                buckets.setdefault(int(h[0] // 2.0), []).append(h)
            for b, bh in sorted(buckets.items()):
                emit_hits(bh, b * 2.0 - 0.30,
                          min(duration, (b + 1) * 2.0 + 0.55),
                          "D%03d_%d_%d" % (b, band, xb))
            continue
        hs.sort(key=lambda z: z[0])
        segs, cur, last = [], [], None
        for h in hs:
            if cur and h[0] - last > 4.0:
                segs.append(cur)
                cur = []
            cur.append(h)
            last = h[0]
        if cur:
            segs.append(cur)
        for si, seg in enumerate(segs):
            st = max(0.0, min(h[0] for h in seg) - 0.6)
            en = min(duration, max(h[0] for h in seg) + 0.9)
            emit_hits(seg, st, en, "S%02d_%d_%02d" % (xb, band, si))
    return items, sorted(set(cjk_texts)), sorted(id_words)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idx", type=int, required=True)
    a = ap.parse_args()
    idx = a.idx
    video, out, work = paths(idx)
    probe = load(os.path.join(work, "probe.json"))
    dur = float(probe.get("duration_sec") or probe.get("duration") or 0)
    # video_probe.py writes flat width/height; older harnesses used a
    # "resolution" / "width_height" field. Getting this wrong clamps every
    # cleanup box and leaves the right-hand CJK text untouched (idx0 QA FAIL
    # 2026-11-14: source was 960x720 but code assumed 720x1280).
    wh = probe.get("resolution") or probe.get("width_height")
    if isinstance(wh, dict):
        fw, fh = int(wh.get("width", 720)), int(wh.get("height", 1280))
    elif isinstance(wh, (list, tuple)) and len(wh) >= 2:
        fw, fh = int(wh[0]), int(wh[1])
    elif probe.get("width") and probe.get("height"):
        fw, fh = int(probe["width"]), int(probe["height"])
    else:
        fw, fh = 720, 1280
    pol = load(os.path.join(work, "audio-policy.json"))
    asr = load(os.path.join(work, "asr.json"))
    prof = load(os.path.join(work, "source-audio-profile.json"))

    items, cjk_texts, ids = cleanup_items_v2(work, dur, fw, fh)
    print("idx=%02d dur=%.1fs %dx%d  cleanup_items=%d cjk_groups=%d ids=%s" %
          (idx, dur, fw, fh, len(items), len(cjk_texts), ids), flush=True)

    case = pol.get("case", "")
    strategy = pol.get("strategy", "keep_original")
    bgm_asset, bgm_meta, needs_dub = None, {}, False
    if strategy == "narration_rewrite":
        # No local voice-cloning model; separating the Chinese voice and keeping
        # the ORIGINAL bed is the safe automated step. English dub is flagged.
        strategy, needs_dub = "tts_keep_instrumental", True
    elif strategy == "add_bgm":
        bgm_asset, bgm_meta = pick_bgm(video, idx)
        strategy = "duck_original_add_bgm" if bgm_asset else "keep_original"
        if not bgm_asset:
            print("  !! no approved BGM -> keep_original (no random track)", flush=True)
    if strategy not in ("keep_original", "tts_keep_instrumental", "duck_original_add_bgm"):
        strategy = "keep_original"

    if strategy == "keep_original":
        primary = {
            "name": "Original Source Audio (USED IN VIDEO)",
            "title": "Original Source Audio (USED IN VIDEO)",
            "artist": "Source clip - original commentary / crowd / music bed",
            "style": "Original mix, unchanged",
            "bpm": "N/A", "licensing": "No third-party track added; original audio retained in full",
            "source": "Source video audio track",
            "download_url": "N/A - already embedded in final.mp4",
            "why": "USER RULE 2026-11-14: English narration or English/instrumental BGM is reused as-is.",
        }
        reason = ("audio_policy case=%s; has_speech=%s lang=%s has_music=%s -> keep entire "
                  "original track, no added BGM, no TTS." %
                  (case, prof.get("has_speech"), asr.get("language_detected"), prof.get("has_music")))
    elif strategy == "tts_keep_instrumental":
        primary = {
            "name": "Original Source BGM - instrumental (USED IN VIDEO)",
            "title": "Original Source BGM - instrumental (USED IN VIDEO)",
            "artist": "Source clip (vocals removed with demucs htdemucs)",
            "style": "Original arrangement, original tempo, lyric-free",
            "bpm": "matches the source clip's cut rhythm",
            "licensing": "No third-party track added; original instrumental bed reused after vocal separation",
            "source": "work/instrumental.wav <- demucs no_vocals stem of work/source_audio.wav",
            "download_url": "N/A - already embedded in final.mp4",
            "why": "USER RULE 2026-11-14: a lyric-bearing BGM must be re-processed into a "
                   "lyric-free instrumental on the SAME rhythm, not replaced by a library track.",
        }
        reason = ("audio_policy case=%s lyric_score=%s; Chinese vocals separated with demucs "
                  "and the ORIGINAL instrumental bed kept. No library BGM, no mechanical TTS.%s" %
                  (case, pol.get("evidence", {}).get("lyric_score"),
                   " English dub flagged for later review." if needs_dub else ""))
    else:
        name = os.path.basename(bgm_asset or "")
        primary = {
            "name": name + " (USED IN VIDEO)", "title": name + " (USED IN VIDEO)",
            "artist": "Kevin MacLeod / licensed local library",
            "style": "Tempo-matched licensed instrumental",
            "bpm": bgm_meta.get("target_bpm", "N/A"),
            "licensing": "CC BY; local licensed library only (music/sports)",
            "source": bgm_asset,
            "download_url": "N/A - local licensed asset",
            "why": "USER RULE 2026-11-14: only when the source has NO narration and NO BGM. "
                   "Selected by bgm_review.py tempo/anti-cymbal/anti-loop gates.",
        }
        reason = ("audio_policy case=no_narration_no_bgm; last-resort licensed BGM %s "
                  "(target BPM %s), original ambience ducked, no monotone loop / harsh cymbals."
                  % (name, bgm_meta.get("target_bpm")))

    headline, stat, subjects = COVER.get(idx, ("NBA CLASSICS", "UNFORGETTABLE PLAYS", ["NBA"]))
    # 2026-11-15 USER FEEDBACK: the old template here made 36/40 posts
    # structurally identical (only the cover headline changed, 4 of 5
    # hashtags were the same everywhere, and summary carried a production
    # note which SKILL.md rule 1b forbids). Per-video copy is now the source
    # of truth, exported by rewrite_posts.py --export -> diag/post-copy.json.
    # The template below is only a loud fallback for an un-authored clip.
    per_video = (load(os.path.join(ROOT, "diag", "post-copy.json"), {}) or {}).get(str(idx))
    if per_video:
        summary = per_video["summary"]
        caption = per_video["caption"]
        alt_caption = per_video["alt_caption"]
        hashtags = list(per_video["hashtags"])
        pinned_comment = per_video["pinned_comment"]
    else:
        print("  !! WARNING idx%02d has no per-video copy; generic placeholder used. "
              "Author it in rewrite_posts.py before delivery." % idx, flush=True)
        summary = "%s - %s." % (headline, stat)
        caption = ("%s. %s. Clean cut of the original footage - no added on-screen text, "
                   "no replacement captions. Follow @HoopDynastyDaily for more NBA classics."
                   % (headline, stat))
        alt_caption = "%s - %s" % (headline, stat)
        hashtags = ["NBA", "Basketball", "NBAClassics", "HoopDynastyDaily",
                    re.sub(r"\W", "", headline)[:22] or "NBAHighlights"]
        pinned_comment = ("More NBA classics daily - follow @HoopDynastyDaily. "
                          "Which play is your favourite? Audio: %s." % primary["name"])
    dec = {
        "profile": "nba",
        "audio_strategy": strategy,
        "bgm_asset": bgm_asset,
        "bgm_volume_db": -16,
        "audio_strategy_reason": reason,
        "needs_english_dub": needs_dub,
        "translations": {},
        "exact_translations": {},
        "title_target": None,
        "watermarks": [],
        "remove_texts": cjk_texts,
        "ignore_texts": cjk_texts,     # suppress build_semantics' own OCR items
        "extra_items": items,          # the ONLY cleanup items (post-processed)
        "auto_plan_items": True,       # process_one: use extra_items verbatim
        # 2026-11-14 v2: boxes are all small (per text) -> the large-region
        # four-edge blend is not needed and would only reintroduce smear bands.
        "render_flags": [],
        "no_srt": True,
        "cover": {"headline": headline, "stat": stat},
        "understanding": {"summary": summary,
                          "subjects": subjects, "highlights": [stat]},
        "post": {
            "caption": caption,
            "alt_caption": alt_caption,
            "hashtags": hashtags,
            "pinned_comment": pinned_comment,
            "music_recommendation": [primary],
            "best_post_time_bj": "19:00-21:00",
            "best_post_time_et": "07:00-09:00",
        },
        "qa_note": ("auto-decide 2026-11-14: erase-only picture, SRT intentionally empty, "
                    "brand yellow on cover only, BGM case=%s -> %s." % (case, strategy)),
    }
    p = os.path.join(work, "agent-decisions.json")
    json.dump(dec, open(p, "w"), ensure_ascii=False, indent=2)
    print("  -> %s (%d items, strategy=%s, cover=%s)" %
          (p, len(items), strategy, headline), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
