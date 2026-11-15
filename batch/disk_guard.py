#!/usr/bin/env python
"""Reclaim dead intermediate files so the 42-clip batch can finish on a 92%-full disk.

Source: 2026-11-14. /Users/jason/Downloads is on a 466Gi volume with only
~38Gi free. Each remaining 1080x1920 clip generates 0.5-1.1G of coarse/dense
OCR PNGs plus a cleaned.mp4 that is byte-for-byte the video stream copied into
final.mp4 (audio_processor.py uses -c:v copy). 26 clips would overflow the disk.

Safe to delete for a DELIVERED clip that will NOT be re-rendered:
  * work/frames/**/*.png  (ocr.json / text-tracks.json / frames-index.json hold
    all forensic results; these PNGs are not read again)
  * work/cleaned.mp4      (final.mp4 already carries the same video stream)
  * *.broken-restart      (truncated renders left by the 16:44 reboot)

NEVER delete while a clip is pending v2 rework: generate_cover.py scores cover
candidates from frames-index.json paths during re-render. Those clips are
identified dynamically by agent-decisions.json carrying --large-region-fill;
after the rework rewrites the decision, they become eligible at the END.

Kept: every deliverable, all JSON (decisions / plan / timeline / qa / indices),
cover-frame.png, wav/audio assets, clean-base.mp4 review copies.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import time

ROOT = os.environ.get("LOCALIZER_BATCH_ROOT", os.path.dirname(os.path.abspath(__file__)))
STATUS = ROOT + "/diag/queue-status.json"
LOG = ROOT + "/logs/disk_guard.log"
STATE = ROOT + "/diag/disk-guard.json"
OUT_ROOT = os.environ.get("LOCALIZER_OUT_ROOT", os.path.expanduser("~/Downloads/tiktok"))
GiB = 1024 ** 3


def note(s):
    line = "%s %s" % (time.strftime("%F %T"), s)
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load(p, default):
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return default


def save(p, obj):
    with open(p, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def work_dir(idx):
    sys.path.insert(0, ROOT)
    import run_queue as Q
    return Q.paths(idx)[2]


def needs_rework(idx):
    """v1 smear-band clips still waiting for the v2 re-render."""
    dec = load(os.path.join(work_dir(idx), "agent-decisions.json"), {})
    return bool(dec.get("auto_plan_items")) and \
        "--large-region-fill" in (dec.get("render_flags") or [])


def sweep_one(idx):
    """Return bytes freed. Refuse to touch anything unless final.mp4 is real."""
    w = work_dir(idx)
    out = os.path.dirname(w)
    final = os.path.join(out, "final.mp4")
    if not os.path.exists(final) or os.path.getsize(final) < 1_000_000:
        return 0
    freed = 0
    frames = os.path.join(w, "frames")
    if os.path.isdir(frames):
        for p in glob.glob(frames + "/**/*.png", recursive=True):
            try:
                n = os.path.getsize(p)
                os.remove(p)
                freed += n
            except OSError:
                pass
        # prune now-empty leaf dirs, keep the frames/ root + JSON indices
        for d, _, files in os.walk(frames, topdown=False):
            if d != frames and not files and not os.listdir(d):
                try:
                    os.rmdir(d)
                except OSError:
                    pass
    cleaned = os.path.join(w, "cleaned.mp4")
    if os.path.exists(cleaned):
        try:
            freed += os.path.getsize(cleaned)
            os.remove(cleaned)
        except OSError:
            pass
    for p in glob.glob(os.path.join(w, "*.broken-restart")):
        try:
            freed += os.path.getsize(p)
            os.remove(p)
        except OSError:
            pass
    return freed


def eligible():
    rows = load(STATUS, [])
    out = []
    for r in rows:
        i = r.get("idx")
        if r.get("stage") != "DELIVERED" or i is None:
            continue
        if needs_rework(i):
            continue
        w = work_dir(i)
        if os.path.isdir(os.path.join(w, "frames")) or \
                os.path.exists(os.path.join(w, "cleaned.mp4")):
            out.append(i)
    return sorted(out)


def batch_finished():
    """The guard exists only WHILE the batch is rendering.

    Once every clip is DELIVERED and there are no queued failures, there is no
    disk left to protect. Exiting keeps this script from becoming a resident
    process that spins forever doing nothing (2026-11-15).
    """
    rows = load(STATUS, [])
    if not rows or len(rows) < 42:
        return False
    if any(r.get("stage") != "DELIVERED" for r in rows):
        return False
    return not bool(load(os.path.join(ROOT, "diag", "queue-failures.json"), []))


def free_bytes():
    return shutil.disk_usage(OUT_ROOT).free


def run_once(force_all=False, low_watermark=8.0, high_watermark=12.0):
    state = load(STATE, {"swept": [], "freed_bytes": 0})
    done = set(state.get("swept", []))
    before = free_bytes()
    if not force_all and before >= low_watermark * GiB:
        return 0, before
    total = 0
    for i in eligible():
        if i in done:
            continue
        n = sweep_one(i)
        if n:
            total += n
            done.add(i)
            note("idx%02d reclaimed %.0f MB (free %.1f GiB)" %
                 (i, n / 1e6, free_bytes() / GiB))
        if not force_all and free_bytes() >= high_watermark * GiB:
            break
    state["swept"] = sorted(done)
    state["freed_bytes"] = state.get("freed_bytes", 0) + total
    state["last_run"] = time.strftime("%F %T")
    save(STATE, state)
    after = free_bytes()
    note("sweep done: freed %.2f GiB, disk %.1f -> %.1f GiB, swept=%d" %
         (total / GiB, before / GiB, after / GiB, len(done)))
    return total, after


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="sweep every eligible delivered clip regardless of disk level")
    ap.add_argument("--interval", type=int, default=180)
    ap.add_argument("--keep-alive", action="store_true",
                    help="stay resident even after the whole batch is delivered")
    a = ap.parse_args()
    note("disk guard start (free %.1f GiB)" % (free_bytes() / GiB))
    if a.once or a.all:
        run_once(force_all=a.all)
        return 0
    while True:
        if batch_finished() and not a.keep_alive:
            note("all 42 clips DELIVERED, no queued failures -> guard exits "
                 "(use --keep-alive only while a batch is actively rendering)")
            return 0
        try:
            run_once()
        except Exception as e:
            note("!! sweep error %r" % e)
        time.sleep(a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
