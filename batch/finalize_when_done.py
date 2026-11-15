#!/usr/bin/env python
"""Wait until all 42 clips are DELIVERED, then run the final self-check.

Runs as a detached background companion to the two queue workers so the final
verification + report are produced even if the chat turn ends first.

  1. refresh qa.json for idx04 / idx10 (their SRT was aligned to the
     erase-only rule after their original QA run)
  2. run verify_all.py
  3. write diag/FINAL-REPORT.md
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.environ.get("LOCALIZER_BATCH_ROOT", os.path.dirname(os.path.abspath(__file__)))
PY = os.environ.get("LOCALIZER_PYTHON", sys.executable)
STATUS = ROOT + "/diag/queue-status.json"
LOG = ROOT + "/logs/finalize.log"
ENV = os.environ.copy()
ENV["PATH"] = (os.environ.get("LOCALIZER_BIN_DIR", os.path.dirname(PY) or ".") + ":" + os.path.expanduser("~/.local/bin")
               + ":/usr/local/bin:/usr/bin:/bin:" + ENV.get("PATH", ""))
ENV["OMP_NUM_THREADS"] = "4"


def note(s):
    line = "%s %s" % (time.strftime("%F %T"), s)
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def delivered():
    try:
        rows = json.load(open(STATUS))
    except (OSError, ValueError):
        return []
    return [r["idx"] for r in rows if r.get("stage") == "DELIVERED"]


def workers_finished():
    """Both queue shards have printed QUEUE END (success, failure or crash)."""
    for name in ("worker_even.out", "worker_odd.out"):
        p = os.path.join(ROOT, "logs", name)
        try:
            if "QUEUE END" not in open(p).read():
                return False
        except OSError:
            return False
    return True


def main():
    note("finalizer armed, waiting for both queue shards (currently %d/42)" %
         len(delivered()))
    deadline = time.time() + 20 * 3600
    while time.time() < deadline:
        if workers_finished():
            break
        time.sleep(60)
    else:
        note("TIMEOUT waiting for queue shards; continuing with partial results")
    note("queue shards done with %d/42 delivered" % len(delivered()))

    # 2026-11-14 visual-QA fix: clips decided by auto_decide v1 carry
    # --large-region-fill and giant union boxes, which leave a visible smeared
    # band after erasing Chinese banners. v2 (per-text boxes) fixes both the
    # smear AND Chinese coverage. Re-render every v1 clip with the new decider.
    sys.path.insert(0, ROOT)
    import run_queue as Q
    v1 = []
    for row in json.load(open(STATUS)):
        if row.get("stage") != "DELIVERED":
            continue
        w = Q.paths(row["idx"])[2]
        dec = Q.load(os.path.join(w, "agent-decisions.json"))
        # Only AUTO-decided clips. idx41 etc. were hand-authored / visually
        # verified through 7 rework rounds and must NOT be re-rendered.
        if dec.get("auto_plan_items") and \
                "--large-region-fill" in (dec.get("render_flags") or []):
            v1.append(row["idx"])
    if v1:
        note("v1 smear-band rework: %s" % v1)
        procs = []
        for shard, name in ((([i for i in v1 if i % 2 == 0]), "even"),
                            (([i for i in v1 if i % 2 == 1]), "odd")):
            if not shard:
                continue
            lp = ROOT + "/logs/rework_v2_%s.log" % name
            f = open(lp, "w")
            procs.append((name, f, subprocess.Popen(
                [PY, ROOT + "/run_queue.py", "--force",
                 "--only", ",".join(str(i) for i in shard)],
                stdout=f, stderr=subprocess.STDOUT, env=ENV,
                stdin=subprocess.DEVNULL)))
        for name, f, p in procs:
            p.wait(timeout=20 * 3600)
            f.close()
            note("v2 rework %s rc=%d" % (name, p.returncode))
        note("v2 rework done -> %d/42 delivered" % len(delivered()))

    # Self-heal: retry any non-DELIVERED clip (crash / QA fail / timeout).
    # run_queue.py computes its own todo from queue-status.json and is idempotent.
    for attempt in (1, 2):
        missing = 42 - len(delivered())
        if missing <= 0:
            break
        note("retry pass %d: %d clip(s) still missing" % (attempt, missing))
        lp = ROOT + "/logs/finalize_retry_%d.log" % attempt
        with open(lp, "w") as f:
            r = subprocess.run([PY, ROOT + "/run_queue.py"], stdout=f,
                               stderr=subprocess.STDOUT, env=ENV,
                               stdin=subprocess.DEVNULL, timeout=20 * 3600)
        note("retry pass %d rc=%d -> %d/42" % (attempt, r.returncode,
                                               len(delivered())))

    # Refresh QA for the two manually delivered clips whose SRT was aligned.
    for i in (4, 10):
        lp = ROOT + "/logs/finalize_qa_%02d.log" % i
        with open(lp, "w") as f:
            r = subprocess.run([PY, ROOT + "/process_one.py", "--phase", "qa",
                                "--idx", str(i)], stdout=f, stderr=subprocess.STDOUT,
                               env=ENV, stdin=subprocess.DEVNULL, timeout=3600)
        note("refresh QA idx%02d rc=%d" % (i, r.returncode))

    out_json = ROOT + "/diag/final-verification.json"
    with open(ROOT + "/logs/finalize_verify.log", "w") as f:
        r = subprocess.run([PY, ROOT + "/verify_all.py", "--json", out_json],
                           stdout=f, stderr=subprocess.STDOUT, env=ENV,
                           stdin=subprocess.DEVNULL, timeout=1800)
    note("verify_all rc=%d" % r.returncode)

    try:
        rep = json.load(open(out_json))
        s, rows = rep["summary"], rep["rows"]
        lines = [
            "# 全批交付自查报告",
            "",
            "生成时间：%s" % time.strftime("%F %T"),
            "",
            "## 总览",
            "",
            "- 视频总数：%d" % s["total"],
            "- 已交付：%d；未交付：%s" % (s["total"] - len(s.get("pending") or []),
                                    s.get("pending") or "无"),
            "- 全部通过：%d" % s["ok"],
            "- 存在问题：%s" % (s["with_issues"] or "无"),
            "- 队列失败记录：%s" % (len(s["queue_failures"]) if s["queue_failures"] else "无"),
            "",
            "## 用户规则核查",
            "",
            "- R1 只擦除、画面不加文字：timeline 非空 = %s；plan 带 target_text = %s；SRT 非空 = %s" %
            (s["timeline_nonempty"] or "无", s["plan_with_target_text"] or "无",
             s["srt_nonempty"] or "无"),
            "- R2 BGM 决策树：%s" % json.dumps(s["audio_cases"], ensure_ascii=False),
            "- R2 实际策略：%s" % json.dumps(s["audio_strategies"], ensure_ascii=False),
            "- R3 封面品牌黄缺失/偏低：%s" % (s["cover_yellow_missing_or_low"] or "无"),
            "- 大框糊带风险（v1 遗留）：%s" % (s.get("smear_risk") or "无"),
            "- 中文残留硬门禁：%s" % json.dumps(s["chinese_residue"], ensure_ascii=False),
            "- 台账未登记：%s" % (s["not_in_ledger"] or "无"),
            "",
            "## 逐条明细",
            "",
            "| idx | 状态 | 中文残留 | BGM 场景 | 策略 | 清理框 | 封面黄 | SRT | 问题 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r0 in rows:
            lines.append("| %02d | %s | %s | %s | %s | %s | %s | %sB | %s |" % (
                r0["idx"], "OK" if r0["ok"] else "FAIL",
                r0.get("chinese_residue"), r0.get("audio_case"),
                r0.get("audio_strategy"), r0.get("cleanup_items"),
                r0.get("cover_yellow_ratio"),
                r0["sizes"].get("subtitles-en.srt"),
                "; ".join(r0["issues"])[:120] or "-"))
        lines += ["", "完整数据：`diag/final-verification.json`", ""]
        with open(ROOT + "/diag/FINAL-REPORT.md", "w") as f:
            f.write("\n".join(lines))
        note("FINAL-REPORT.md written; ok=%d/%d" % (s["ok"], s["total"]))
    except Exception as e:
        note("!! report generation failed: %r" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
