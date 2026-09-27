#!/usr/bin/env python3
"""Regression guard for the pipeline entry (run_pipeline.py).

Why this exists: the pipeline gained two behaviours in 2026-09-27 that must
not silently regress while the skill keeps being edited:

  1. **Directory batch mode** -- ``--input <dir>`` scans every video under the
     dir, checks processed-videos.json per video, skips processed ones.
  2. **Cover-first ordering** -- the cover is rendered right after analyze,
     *before* the (slow) video cleanup / TTS / subtitle pass, so the user can
     upload the cover while the video finishes in the background.

These are behavioural contracts, not just "nice to have". A future edit that
reorders the jobs or drops the directory branch would reintroduce the exact
pain the user asked to fix, so we pin them.

No video fixtures needed: compile + --help + source inspection + a pure
function call on scan_videos.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")

PYTHON = sys.executable
PIPELINE = "run_pipeline.py"


def _src() -> str:
    with open(os.path.join(SCRIPTS, PIPELINE), encoding="utf-8") as f:
        return f.read()


def test_pipeline_compiles():
    compile(_src(), PIPELINE, "exec")
    print("  run_pipeline.py compiles cleanly")


def test_cli_surface():
    proc = subprocess.run(
        [PYTHON, os.path.join(SCRIPTS, PIPELINE), "--help"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"--help failed: {proc.stderr[:400]}"
    help_text = proc.stdout + proc.stderr
    required = [
        "--input", "--work-dir", "--output", "--profile", "--review",
        "--logo", "--bgm", "--cover-image", "--resume", "--force",
        "--ignore-mtime", "--skip",
    ]
    missing = [f for f in required if f not in help_text]
    assert not missing, f"run_pipeline.py is missing flags: {missing}"
    print(f"  --help ok, {len(required)} flags present")


def test_scan_videos_filters_and_orders():
    sys.path.insert(0, SCRIPTS)
    from run_pipeline import scan_videos

    d = tempfile.mkdtemp()
    names = ["b.mp4", "a.MOV", "c.avi", "notes.txt", ".hidden.mp4", "e.mkv",
             "clip.webm", "not_a_video.pdf"]
    for n in names:
        open(os.path.join(d, n), "w").close()

    got = [os.path.basename(p) for p in scan_videos(d)]
    # sorted order; only real video extensions; hidden files and non-video skipped
    assert got == ["a.MOV", "b.mp4", "c.avi", "clip.webm", "e.mkv"], got

    # empty dir -> []
    assert scan_videos(tempfile.mkdtemp()) == []

    # a file path (not a dir) -> []
    assert scan_videos(os.path.join(d, "b.mp4")) == []
    print("  scan_videos: extension filter + hidden skip + sorted + non-dir -> []")


def test_cover_is_before_localize():
    """The cover job must be invoked before the localize job in process_one."""
    src = _src()
    cover_at = src.find("all_results[\"cover\"]")
    localize_at = src.find("all_results[\"localize\"]")
    assert cover_at != -1 and localize_at != -1, "expected cover & localize blocks"
    assert cover_at < localize_at, (
        "cover must run before localize (cover-first ordering)")
    print("  cover job is ordered before localize job")


def test_batch_branch_present():
    """Directory mode must exist: isdir check + scan_videos + per-video loop."""
    src = _src()
    assert "scan_videos" in src, "scan_videos function missing"
    assert "os.path.isdir(args.input)" in src, "directory branch missing"
    assert "def process_one" in src, "process_one (per-video) function missing"
    assert "Batch Complete" in src, "batch summary missing"
    print("  directory batch branch + process_one + summary present")


def test_batch_ignores_output_overrides():
    """In batch mode --output/--work-dir must be neutralised (per-video derive)."""
    src = _src()
    assert "args.output = None" in src and "args.work_dir = None" in src, (
        "batch mode must reset --output/--work-dir so each video derives its own")
    print("  batch mode resets --output/--work-dir")


def test_cover_image_flag_plumbed():
    """--cover-image must reach job_cover and be passed to generate_cover.py."""
    src = _src()
    assert "getattr(args, \"cover_image\", None)" in src, (
        "process_one must forward --cover-image to job_cover")
    assert "--cover-image" in src, "generate_cover.py must receive --cover-image"
    print("  --cover-image is plumbed through to generate_cover.py")


# --------------------------------------------------------------------------
def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        print(f"\n[{name}]")
        try:
            fn()
        except AssertionError as exc:
            failed.append((name, str(exc)))
            print(f"  FAIL: {exc}")
        except Exception as exc:                              # noqa: BLE001
            failed.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"  ERROR: {type(exc).__name__}: {exc}")
        else:
            print("  ok")
    print("\n" + "=" * 64)
    if failed:
        print(f"FAILED {len(failed)}/{len(tests)}")
        for name, msg in failed:
            print(f"  - {name}: {msg[:160]}")
        return 1
    print(f"PASSED {len(tests)}/{len(tests)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
