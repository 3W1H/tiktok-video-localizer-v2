#!/usr/bin/env python
"""Resumable, serial full-batch queue for the 2026-11-14 Downloads batch.

Idempotent: a clip is skipped when its four deliverables exist AND qa.json
says chinese_residue != fail. Every phase appends to logs/queue_NN.log, so a
crash / stopped session can be resumed with the exact same command.

Usage:
  python run_queue.py                # process every remaining clip
  python run_queue.py --limit 5      # at most 5 clips this run
  python run_queue.py --only 3,7,9   # specific idx only
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time

ROOT = os.environ.get("LOCALIZER_BATCH_ROOT", os.path.dirname(os.path.abspath(__file__)))
SK = os.environ.get("LOCALIZER_SKILL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_ROOT = os.environ.get("LOCALIZER_OUT_ROOT", os.path.expanduser("~/Downloads/tiktok"))
PY = os.environ.get("LOCALIZER_PYTHON", sys.executable)
LEDGER = SK + "/processed-videos.json"
STATUS = ROOT + "/diag/queue-status.json"
FAILS = ROOT + "/diag/queue-failures.json"
LOCK = ROOT + "/diag/queue.lock"
ARTIFACTS = ("final.mp4", "cover.jpg", "subtitles-en.srt", "tiktok-post.md")

ENV = os.environ.copy()
ENV["PATH"] = (os.environ.get("LOCALIZER_BIN_DIR", os.path.dirname(PY) or ".") + ":" + os.path.expanduser("~/.local/bin")
               + ":/usr/local/bin:/usr/bin:/bin:" + ENV.get("PATH", ""))
ENV["OMP_NUM_THREADS"] = "4"

sys.path.insert(0, SK + "/scripts")
from run_pipeline import sanitize_folder_name  # noqa: E402


def load(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


def save(p, d):
    with open(p, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def paths(idx):
    rec = {int(r["idx"]): r for r in json.load(open(ROOT + "/diag/index.json"))}[idx]
    out = os.path.join(OUT_ROOT, sanitize_folder_name(os.path.basename(rec["path"])))
    # 2026-11-15: the user marked two finished clips by appending "-done" to
    # their output folders. Keep them addressable so verify_all.py does not
    # report them as pending / missing artifacts.
    if not os.path.isdir(out) and os.path.isdir(out + "-done"):
        out = out + "-done"
    return rec["path"], out, os.path.join(out, "work")


@contextlib.contextmanager
def shared_lock():
    """Serialize all read-modify-write on shared status/ledger/failure files.

    2026-11-14: two queue workers (even/odd idx shards) run concurrently to use
    the 8 cores. Without a lock the last writer to queue-status.json would
    silently erase the other worker's DELIVERED rows.
    """
    with open(LOCK, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def log_all(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(ROOT + "/logs/queue_all.log", "a") as f:
        f.write(line + "\n")


def run_step(cmd, logpath, timeout=3600):
    t0 = time.time()
    with open(logpath, "a") as f:
        f.write("\n$ " + " ".join(str(c) for c in cmd) + "\n")
        f.flush()
        try:
            r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                               env=ENV, stdin=subprocess.DEVNULL, timeout=timeout)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            f.write("!! TIMEOUT after %ds\n" % timeout)
            rc = 124
    return rc, time.time() - t0


def is_delivered(idx):
    _, out, work = paths(idx)
    if not all(os.path.exists(os.path.join(out, f)) for f in ARTIFACTS):
        return False
    q = load(os.path.join(work, "qa.json"))
    if not q:
        return False
    return q.get("checks", {}).get("chinese_residue", {}).get("status") != "fail"


def update_status(idx, stage, qa=None, extra=None):
    with shared_lock():
        rows = load(STATUS, [])
        for r in rows:
            if r.get("idx") == idx:
                r["stage"] = stage
                if qa is not None:
                    r["qa"] = qa
                if extra:
                    r.update(extra)
        save(STATUS, rows)


def add_ledger(idx, out, status):
    import processed_log
    with shared_lock():
        led = load(LEDGER)
        recs = led.get("records", []) if isinstance(led, dict) else []
        if any(r.get("output_dir") == out for r in recs if isinstance(r, dict)):
            return False
        cfg = load(os.path.join(out, "work", "pipeline-config.json"))
        src = cfg.get("input") or paths(idx)[0]
        processed_log.add_video(src, out, "nba", status)
        return True


def repair_from_qa(idx, q, logp):
    """Append QA-detected Chinese residues as extra cleanup boxes, then re-render.

    Full-auto batching cannot eyeball every frame, so the OCR hard gate is the
    feedback loop: whatever it finds is turned into a follow-up erase item.
    """
    groups = (q.get("checks", {}).get("chinese_residue", {}).get("groups") or [])
    if not groups:
        return 0
    _, out, work = paths(idx)
    dp = os.path.join(work, "agent-decisions.json")
    dec = load(dp)
    probe = load(os.path.join(work, "probe.json"))
    fw = int(probe.get("width") or 960)
    fh = int(probe.get("height") or 720)
    new = []
    skipped = 0
    for k, g in enumerate(groups):
        b = g.get("bbox") or {}
        x0 = max(0, int(b.get("x", 0)) - 14)
        y0 = max(0, int(b.get("y", 0)) - 14)
        x1 = min(fw, int(b.get("x", 0) + b.get("w", 0)) + 14)
        y1 = min(fh, int(b.get("y", 0) + b.get("h", 0)) + 14)
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        # 2026-11-15 SMEAR_RISK root cause: QA groups are UNION boxes of a
        # whole Chinese line (e.g. 1163x127 = 16% of the frame). Inpainting
        # such a tall+wide hole leaves a visible low-frequency smear band.
        # Never emit one. Wide but THIN lines (h <= 22% frame height) are safe
        # for TELEA and are kept; anything taller must come from the per-text
        # OCR boxes instead (see fix_smear.py).
        if (y1 - y0) > 0.22 * fh or (x1 - x0) * (y1 - y0) > 0.05 * fw * fh:
            skipped += 1
            continue
        new.append({
            "item_id": "R%02d_%02d" % (len(dec.get("extra_items") or []), k),
            "category": "watermark", "action": "remove", "confidence": 1.0,
            "bbox": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
            "start_time": round(max(0.0, float(g.get("first_sec", 0)) - 0.7), 2),
            "end_time": round(float(g.get("last_sec", 0)) + 1.3, 2),
            "source_text": g.get("text", "QA residue"),
            "target_text": None, "source_text_removal": True,
            "removal_method": ["inpaint"], "replacement": None,
            "reason": "QA chinese_residue feedback: %r seen %.1f-%.1fs" %
                      (g.get("text"), g.get("first_sec", 0), g.get("last_sec", 0)),
        })
    if not new:
        if skipped:
            with open(logp, "a") as f:
                f.write("AUTO-REPAIR: skipped %d oversized union box(es) "
                        "(would cause smear); use per-text OCR boxes instead\n" % skipped)
        return 0
    dec["extra_items"] = list(dec.get("extra_items") or []) + new
    dec["qa_note"] = (dec.get("qa_note", "") + " | auto-repair +%d QA residue boxes" % len(new))
    save(dp, dec)
    with open(logp, "a") as f:
        f.write("AUTO-REPAIR: +%d boxes from QA residues\n" % len(new))
    return len(new)


def process_one(idx):
    video, out, work = paths(idx)
    logp = ROOT + "/logs/queue_%02d.log" % idx
    os.makedirs(ROOT + "/logs", exist_ok=True)
    os.makedirs(work, exist_ok=True)
    with open(logp, "a") as f:
        f.write("\n===== QUEUE RUN idx=%02d %s =====\n" % (idx, time.strftime("%F %T")))
    log_all("idx%02d START  %s" % (idx, os.path.basename(video)[:44]))

    # 1. forensics (cached when already analyzed)
    need = not (os.path.exists(os.path.join(work, "probe.json"))
                and os.path.exists(os.path.join(work, "text-tracks.json"))
                and os.path.exists(os.path.join(work, "asr.json")))
    if need:
        rc, dt = run_step([PY, ROOT + "/process_one.py", "--phase", "analyze",
                           "--idx", str(idx)], logp, timeout=3600)
        if rc:
            return "ANALYZE_FAIL", "rc=%d" % rc
        log_all("idx%02d analyze ok (%.0fs)" % (idx, dt))
    else:
        log_all("idx%02d analyze cached" % idx)

    # 2. digest + fresh audio-policy verdict
    run_step([PY, ROOT + "/process_one.py", "--phase", "digest", "--idx", str(idx)],
             logp, timeout=600)
    run_step([PY, ROOT + "/audio_policy.py", "--work", work], logp, timeout=300)

    # 3. Agent decisions
    rc, _ = run_step([PY, ROOT + "/auto_decide.py", "--idx", str(idx)], logp, timeout=1200)
    if rc:
        return "DECIDE_FAIL", "rc=%d" % rc
    dec = load(os.path.join(work, "agent-decisions.json"))
    log_all("idx%02d decided: %s / %s / %d items" %
            (idx, dec.get("cover", {}).get("headline"), dec.get("audio_strategy"),
             len(dec.get("extra_items") or [])))

    # 4-5. render + QA, with a bounded OCR-feedback repair loop
    q = {}
    for attempt in range(3):
        rc, dt = run_step([PY, ROOT + "/process_one.py", "--phase", "render",
                           "--idx", str(idx)], logp, timeout=5400)
        if rc:
            return "RENDER_FAIL", "rc=%d" % rc
        if not all(os.path.exists(os.path.join(out, f)) for f in ARTIFACTS):
            return "RENDER_FAIL", "missing artifacts"
        log_all("idx%02d rendered (%.0fs)%s" %
                (idx, dt, " [repair %d]" % attempt if attempt else ""))
        rc, dt = run_step([PY, ROOT + "/process_one.py", "--phase", "qa",
                           "--idx", str(idx)], logp, timeout=3600)
        q = load(os.path.join(work, "qa.json"))
        overall = q.get("overall", "UNKNOWN")
        ch = q.get("checks", {}).get("chinese_residue", {}).get("status", "unknown")
        log_all("idx%02d QA %s chinese=%s (%.0fs)" % (idx, overall, ch, dt))
        if rc and not q:
            return "QA_FAIL", "no qa.json"
        if ch != "fail":
            break
        if attempt < 2:
            n = repair_from_qa(idx, q, logp)
            log_all("idx%02d QA residue -> auto-repair %d boxes, re-render" % (idx, n))
            if not n:
                break
    ch = q.get("checks", {}).get("chinese_residue", {}).get("status", "unknown")
    overall = q.get("overall", "UNKNOWN")
    if ch == "fail":
        return "CHINESE_RESIDUE_FAIL", "%s" % q.get("checks", {}).get("chinese_residue", {}).get("groups", [])[:4]

    # 6. ledger + status
    status = str(overall).upper()
    try:
        add_ledger(idx, out, status)
    except Exception as e:
        with open(logp, "a") as f:
            f.write("!! ledger: %s\n" % e)
    srt = os.path.getsize(os.path.join(out, "subtitles-en.srt"))
    update_status(idx, "DELIVERED", overall, {
        "srt_entries": 0 if srt == 0 else open(os.path.join(out, "subtitles-en.srt")).read().count("-->"),
        "audio_strategy": dec.get("audio_strategy"),
        "auto_case": dec.get("qa_note", ""),
        "needs_english_dub": bool(dec.get("needs_english_dub")),
        "artifacts": {f: True for f in ARTIFACTS},
    })
    return "DELIVERED", overall


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--force", action="store_true",
                    help="Re-render even when four artifacts + QA already exist "
                         "(used by the v1 smear-band rework pass)")
    a = ap.parse_args()
    rows = load(STATUS, [])
    if a.only:
        todo = [int(x) for x in a.only.split(",") if x.strip()]
    else:
        todo = [r["idx"] for r in rows if r.get("stage") != "DELIVERED"
                or not is_delivered(r["idx"])]
    if not a.force:
        todo = [i for i in todo if not is_delivered(i)]
    if a.limit:
        todo = todo[:a.limit]
    log_all("QUEUE START remaining=%d order=%s" % (len(todo), todo))
    fails = load(FAILS, [])
    done = 0
    for idx in todo:
        t0 = time.time()
        try:
            state, detail = process_one(idx)
        except Exception as e:
            state, detail = "EXCEPTION", repr(e)
        log_all("idx%02d -> %-24s %s (%.0fs total)" %
                (idx, state, str(detail)[:90], time.time() - t0))
        if state != "DELIVERED":
            with shared_lock():
                fails = load(FAILS, [])
                fails.append({"idx": idx, "state": state,
                              "detail": str(detail)[:400],
                              "time": time.strftime("%F %T")})
                save(FAILS, fails)
            update_status(idx, state)
        else:
            done += 1
    log_all("QUEUE END delivered_this_run=%d failures=%d" %
            (done, len(load(FAILS, []))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
