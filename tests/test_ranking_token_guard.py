#!/usr/bin/env python3
"""Regression guard for the countdown / ranking-overlay fix (2026-10-07).

Why this exists:
  In the Garnett ("加内特") batch, a persistent bottom title bar
  "凯文加内特生涯十佳球TOP10" (and its per-segment variants TOP9..TOP6) was set to
  `action=remove`, which inpainted away the whole bar — including the "TOP N"
  countdown. That countdown is the *only* "which ball" cue in the video, so the
  output lost the "第几个球" explanation. The fix has three parts:
    1. common.py  -> RANKING_TOKEN_RE / has_ranking_token / extract_ranking_tokens
    2. prepare_plan.py -> forces action=translate + flags on ranking-token regions
    3. qa_video.py    -> check_localization warns on remove-with-ranking-token
  This test pins all three so a future edit that drops the guard fails loudly.

Run:
    /Users/jason/opt/miniconda3/bin/python tests/test_ranking_token_guard.py
No video fixtures needed: pure-function unit tests + source inspection.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import common  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


# ---------------------------------------------------------------------------
# 1) Pure-function unit tests (common.py)
# ---------------------------------------------------------------------------
def test_regex():
    print("== common.has_ranking_token / extract_ranking_tokens ==")
    pos = {
        "凯文加内特生涯十佳球TOP10": ["TOP10"],
        "NO.7": ["NO.7"],
        "第3球": ["第3球"],
        "RANK 5": ["RANK 5"],
        "BEST 9": ["BEST 9"],
        "3/10": ["3/10"],
        "NO 4": ["NO 4"],
    }
    for text, expected in pos.items():
        check(f"has_ranking_token({text!r})", common.has_ranking_token(text),
              "expected a token match")
        got = common.extract_ranking_tokens(text)
        check(f"extract({text!r}) == {expected}", got == expected, f"got {got}")

    # negatives: Chinese-only text with a *CJK* numeral (十) must NOT match,
    # because the regex only matches ASCII digits.
    neg = [
        "凯文加内特生涯十佳球",   # 十 is a CJK numeral, not \d
        "篮球集锦精彩片段",
        "这是最纯粹的篮球",
        "",
    ]
    for text in neg:
        check(f"no token in {text!r}", not common.has_ranking_token(text),
              "false positive")


# ---------------------------------------------------------------------------
# 2) Source-inspection guards (matching test_cleanup_scripts.py style)
# ---------------------------------------------------------------------------
def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_prepare_plan_guard():
    print("== prepare_plan.py guardrail present ==")
    src = _read(os.path.join(SCRIPTS, "prepare_plan.py"))
    check("imports extract_ranking_tokens", "extract_ranking_tokens" in src)
    check("imports has_ranking_token", "has_ranking_token" in src)
    check("forces action=translate on ranking token",
          "action = \"translate\"" in src and "ranking_tokens" in src)
    check("emits contains_ranking_token flag", "contains_ranking_token" in src)
    check("emits dynamic_ranking flag", "dynamic_ranking" in src)
    check("reason warns against remove", "never set action=remove" in src)


def test_qa_guard():
    print("== qa_video.py gate present ==")
    src = _read(os.path.join(SCRIPTS, "qa_video.py"))
    check("imports has_ranking_token", "has_ranking_token" in src)
    check("skips non-remove items", 'action") != "remove"' in src)
    check("collects removed_countdowns", "removed_countdowns" in src)
    check("warns which-ball info lost", "which-ball info will be lost" in src)


def main():
    test_regex()
    test_prepare_plan_guard()
    test_qa_guard()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
