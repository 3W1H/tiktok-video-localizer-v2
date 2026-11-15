#!/usr/bin/env python
"""BGM candidate reviewer -- USER RULE 2026-11-14 (last-resort BGM only).

Use ONLY when the source has neither narration nor BGM. The local music/
directory IS the whitelist (Kevin MacLeod / CC BY), so no unlicensed track
can be chosen by accident.

Encodes the user's requirements as measurable gates:
  * tempo / cut density must match the edit (--target-bpm or --video)
  * reject harsh timbre, over-loud or over-frequent cymbals / hi-hats
  * reject monotone loops and flat, boring dynamics
  * prefer tracks with musical movement that stays comfortable

Usage:
  bgm_review.py --target-bpm 96 [--top 8]
  bgm_review.py --video <clip.mp4> [--top 8]     # estimates cut rhythm
  bgm_review.py --limit 10 --target-bpm 100       # quick calibration
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess

import numpy as np
from scipy.signal import stft

ROOT = os.environ.get("LOCALIZER_BATCH_ROOT", os.path.dirname(os.path.abspath(__file__)))
LIB = os.environ.get("LOCALIZER_MUSIC_LIB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "music", "sports"))
FFMPEG = os.path.expanduser("~/.local/bin/ffmpeg")
SR = 22050
NFFT = 1024
HOP = 256
FRAME_RATE = SR / HOP  # onset-envelope frames per second


def decode(path: str, seconds: float = 60.0) -> np.ndarray:
    p = subprocess.run(
        [FFMPEG, "-v", "error", "-i", path, "-t", str(seconds),
         "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"],
        capture_output=True)
    return np.frombuffer(p.stdout, dtype="<f4").astype(np.float64)


def estimate_bpm(flux: np.ndarray) -> tuple[float, float]:
    """Autocorrelation of the onset envelope -> (bpm, beat_periodicity)."""
    x = flux - flux.mean()
    if not np.any(x):
        return 0.0, 0.0
    ac = np.correlate(x, x, mode="full")[len(x) - 1:]
    if ac[0] <= 0:
        return 0.0, 0.0
    lags = np.arange(20, min(120, len(ac)))
    best = int(lags[np.argmax(ac[lags])])
    bpm = 60.0 * FRAME_RATE / best
    # very high periodicity == mechanically repetitive loop
    return float(bpm), float(ac[best] / ac[0])


def analyze(path: str) -> dict:
    y = decode(path)
    if y.size < SR * 5:
        return {"path": path, "reject": ["decode too short"]}
    freqs, _, Z = stft(y, fs=SR, nperseg=NFFT, noverlap=NFFT - HOP)
    mag = np.abs(Z) + 1e-9
    flux = np.maximum(0.0, np.diff(mag, axis=1)).sum(axis=0)
    flux /= (flux.max() + 1e-9)

    bpm, periodicity = estimate_bpm(flux)

    hi_mask = freqs >= 6000
    hi_ratio = float(mag[hi_mask].sum() / mag.sum())
    hi_flux = np.maximum(0.0, np.diff(mag[hi_mask], axis=1)).sum(axis=0)
    hi_flux /= (hi_flux.max() + 1e-9)
    # "打镲声音过高、频繁": sharp high-band transients per second
    hi_trans = float(np.mean(hi_flux > 0.45) * FRAME_RATE)

    centroid = float((mag * freqs[:, None]).sum() / mag.sum())
    onset_rate = float(np.sum(flux > 0.35) / (len(y) / SR))

    rms = np.sqrt(np.mean(y[:len(y) // HOP * HOP].reshape(-1, HOP) ** 2, axis=1))
    dynamics = float(rms.std() / (rms.mean() + 1e-9))
    crest = float(np.max(np.abs(y)) / (np.sqrt(np.mean(y ** 2)) + 1e-9))

    # segment similarity: near-identical blocks == a monotonous loop
    segs = np.array_split(mag, 8, axis=1)
    bands = np.array([s.mean(axis=1) for s in segs])
    bands /= (np.linalg.norm(bands, axis=1, keepdims=True) + 1e-9)
    sim = bands @ bands.T
    loopiness = float(np.mean(sim[np.triu_indices(min(8, len(bands)), 1)]))

    reject = []
    if hi_ratio > 0.20:
        reject.append("high-frequency energy too strong (cymbals/hi-hat)")
    if hi_trans > 6.0:
        reject.append("hi-hat transients too frequent")
    if centroid > 4200:
        reject.append("harsh / piercing timbre")
    if crest > 12:
        reject.append("peaky, uncomfortable transients")
    if loopiness > 0.985:
        reject.append("monotone loop, no arrangement movement")
    if dynamics < 0.20:
        reject.append("flat dynamics, boring bed")
    if periodicity > 0.90:
        reject.append("mechanically repetitive beat")

    return {
        "name": os.path.basename(path),
        "path": path,
        "bpm": round(bpm, 1),
        "onset_rate": round(onset_rate, 2),
        "hi_ratio": round(hi_ratio, 4),
        "hi_trans_per_s": round(hi_trans, 2),
        "centroid_hz": round(centroid, 1),
        "crest": round(crest, 2),
        "dynamics": round(dynamics, 3),
        "loopiness": round(loopiness, 4),
        "beat_periodicity": round(periodicity, 3),
        "reject": reject,
    }


def bpm_distance(a: float, b: float) -> float:
    """Octave-equivalent distance: half/double tempo still grooves."""
    if a <= 0 or b <= 0:
        return 0.0
    return min(abs(a - b), abs(a * 2 - b), abs(a / 2 - b))


def target_from_video(video: str) -> tuple[float, str]:
    p = subprocess.run(
        [FFMPEG, "-v", "info", "-i", video,
         "-vf", "select='gt(scene,0.35)',showinfo", "-an", "-f", "null", "-"],
        capture_output=True, text=True)
    n = (p.stderr or "").count("pts_time:")
    dur_p = subprocess.run(
        [FFMPEG, "-v", "error", "-i", video, "-show_entries",
         "format=duration", "-of", "default=nw=1:nk=1"],
        capture_output=True, text=True)
    # ffprobe is more reliable for duration
    probe = subprocess.run(
        [os.path.expanduser("~/.local/bin/ffprobe"), "-v", "error",
         "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", video],
        capture_output=True, text=True)
    try:
        dur = float((probe.stdout or "0").strip())
    except ValueError:
        dur = 0.0
    cps = n / max(dur, 1.0)
    bpm = round(max(60.0, min(160.0, cps * 60.0)), 1)
    return bpm, ("estimated from %d scene cuts in %.1fs (%.2f cuts/s); "
                 "override with --target-bpm if the edit has a different feel"
                 % (n, dur, cps))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-bpm", type=float, default=0.0)
    ap.add_argument("--video", default="")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=os.path.join(ROOT, "diag", "bgm-candidates.json"))
    a = ap.parse_args()

    target, note = a.target_bpm, "user-specified target BPM"
    if a.video:
        target, note = target_from_video(a.video)
    print("target BPM: %.1f (%s)" % (target, note), flush=True)

    files = sorted(glob.glob(os.path.join(LIB, "*.mp3")))
    if a.limit:
        files = files[:a.limit]
    # Library analysis is video-independent (BPM / timbre / cymbal / loopiness).
    # Cache it once: re-decoding all 66 MP3s for every add_bgm clip wasted
    # minutes per clip in the full batch.
    cache_path = os.path.join(ROOT, "diag", "bgm-library-analysis.json")
    cache = {}
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path))
        except (OSError, ValueError):
            cache = {}
    rows = []
    fresh = 0
    for i, f in enumerate(files):
        key = "%s:%d:%d" % (os.path.basename(f), os.path.getmtime(f),
                            os.path.getsize(f))
        if key in cache:
            rows.append(dict(cache[key]))
        else:
            r = analyze(f)
            cache[key] = r
            rows.append(dict(r))
            fresh += 1
        if (i + 1) % 10 == 0:
            print("  reviewed %d/%d (new=%d)" % (i + 1, len(files), fresh),
                  flush=True)
    if fresh:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        json.dump(cache, open(cache_path, "w"), ensure_ascii=False)
    for r in rows:
        if r.get("reject"):
            continue
        r["tempo_gap"] = round(bpm_distance(r.get("bpm", 0), target), 1)
        r["score"] = round(
            -0.6 * r["tempo_gap"]
            - 30 * r.get("hi_ratio", 0)
            - 1.2 * r.get("hi_trans_per_s", 0)
            - 40 * max(0.0, r.get("loopiness", 0) - 0.90)
            + 12 * min(0.6, r.get("dynamics", 0)), 2)
    ok = sorted((r for r in rows if "score" in r),
                key=lambda r: -r["score"])
    out = {
        "rule": "USER RULE 2026-11-14: only add BGM when source has NO narration "
                "and NO BGM; licensed local library only; tempo must match the "
                "edit; no monotone loops, no harsh timbre, no over-loud/frequent "
                "cymbals, must be comfortable.",
        "target_bpm": target,
        "target_note": note,
        "library": LIB,
        "reviewed": len(rows),
        "approved": ok[:a.top],
        "rejected": [{"name": r.get("name"), "reasons": r["reject"],
                      "bpm": r.get("bpm"), "hi_ratio": r.get("hi_ratio"),
                      "hi_trans_per_s": r.get("hi_trans_per_s"),
                      "loopiness": r.get("loopiness"),
                      "dynamics": r.get("dynamics")}
                     for r in rows if r.get("reject")],
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(out, open(a.out, "w"), ensure_ascii=False, indent=2)
    print("\n=== approved (top %d of %d reviewed) ===" % (min(a.top, len(ok)), len(rows)))
    for r in ok[:a.top]:
        print("  %6.2f  %-34s bpm=%-6s gap=%-5s hi=%.3f hats=%.1f/s dyn=%.2f" %
              (r["score"], r["name"][:34], r["bpm"], r["tempo_gap"],
               r["hi_ratio"], r["hi_trans_per_s"], r["dynamics"]))
    print("\nrejected: %d   ->  %s" % (len(out["rejected"]), a.out))


if __name__ == "__main__":
    main()
