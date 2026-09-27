#!/usr/bin/env python3
"""Regression guard for the four cleanup scripts.

Why this exists: the cleanup/overlay logic lives in FOUR scripts
(clean_video.py / render_overlays.py / clean_with_overlay.py / inpaint_video.py)
that drifted apart and the docs recommended different ones in different places.
When we converge them onto one official path, the hard requirement is that
*no existing feature silently stops working*. These tests pin each script's
CLI surface and key functions so a future edit that removes a flag or a
function fails loudly.

Run either way:

    /Users/jason/opt/miniconda3/bin/python tests/test_cleanup_scripts.py
    python -m pytest tests/test_cleanup_scripts.py -q      # if pytest is available

No video fixtures needed: compile + `--help` + source inspection only.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")

PYTHON = sys.executable

# Each cleanup script and the CLI flags that MUST keep working.
# These are the "public surface" the docs / pipeline depend on.
SCRIPT_FLAGS = {
    "clean_video.py": [
        "--video", "--work-dir", "--plan", "--masks", "--probe",
        "--method", "--crf", "--preset", "--black-threshold",
        "--inpaint-radius", "--blur-large-regions", "--no-render-titles",
        "--output",
    ],
    "render_overlays.py": [
        "--video", "--work-dir", "--output", "--inpaint-radius", "--crf",
        "--preset", "--hook-seconds", "--logo-text", "--logo-ratio",
        "--logo-y", "--no-logo", "--no-inpaint", "--save-clean-base",
        "--pad-head", "--pad-tail", "--print-logo-size", "--sync-timeline",
        "--start-time", "--max-frames", "--x264-threads", "--dry-run",
    ],
    "clean_with_overlay.py": [
        "--video", "--output", "--work-dir", "--blackbar", "--overlay-text",
        "--replace-first-frame", "--first-frame-blackbar", "--trim-start",
    ],
    "inpaint_video.py": [
        "--video", "--output", "--inpaint", "--overlay", "--trim-start",
        "--inpaint-radius", "--blur-region", "--blur-radius", "--blur-alpha",
    ],
}

# Key functions each script must keep exposing (checked by name in source).
SCRIPT_FUNCTIONS = {
    "clean_video.py": [
        "probe_color_args", "FFmpegH264Writer", "find_font", "check_logo_asset",
        "inpaint_with_opencv", "clean_with_ffmpeg", "render_translated_titles",
        "overlay_logo", "fast_clean_with_ffmpeg",
    ],
    "render_overlays.py": [
        "merge_boxes", "inpaint_regions", "pick_bold_font", "fit_width",
        "logo_font_size", "fit_font", "draw_centered", "build_overlays",
        "sync_timeline",
    ],
    "clean_with_overlay.py": [
        "parse_blackbar", "parse_overlay", "get_font_path", "build_filter",
        "replace_first_frame", "generate_artistic_logo",
    ],
    "inpaint_video.py": [
        "parse_region", "parse_overlay", "get_font_path", "inpaint_video",
    ],
}

# Legacy scripts must carry an explicit deprecation marker.
LEGACY = ("clean_with_overlay.py", "inpaint_video.py")


def _src(name: str) -> str:
    path = os.path.join(SCRIPTS, name)
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_all_scripts_compile():
    for name in SCRIPT_FLAGS:
        # compile() only parses, it does not write a .pyc (sandbox blocks that).
        compile(_src(name), name, "exec")
    print(f"  {len(SCRIPT_FLAGS)} scripts compile cleanly")


def test_cli_help_and_flags():
    for name, flags in SCRIPT_FLAGS.items():
        proc = subprocess.run(
            [PYTHON, os.path.join(SCRIPTS, name), "--help"],
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, (
            f"{name} --help failed (rc={proc.returncode}): {proc.stderr[:400]}")
        help_text = proc.stdout + proc.stderr
        missing = [f for f in flags if f not in help_text]
        assert not missing, f"{name} is missing flags: {missing}"
        print(f"  {name}: --help ok, {len(flags)} flags present")


def test_key_functions_present():
    for name, funcs in SCRIPT_FUNCTIONS.items():
        src = _src(name)
        missing = [f for f in funcs if not re.search(rf"\n(?:def|class) {re.escape(f)}\b", src)]
        assert not missing, f"{name} is missing functions: {missing}"
        print(f"  {name}: {len(funcs)} key functions present")


def test_legacy_scripts_marked_deprecated():
    for name in LEGACY:
        src = _src(name)
        assert "[DEPRECATED]" in src, f"{name} is legacy but missing [DEPRECATED] marker"
        assert "render_overlays.py" in src, (
            f"{name} deprecation notice must point to render_overlays.py")
        print(f"  {name}: marked [DEPRECATED] -> render_overlays.py")


def test_official_script_is_not_deprecated():
    """The official path must NOT be flagged as deprecated."""
    src = _src("render_overlays.py")
    assert "[DEPRECATED]" not in src, "render_overlays.py must not be marked deprecated"
    print("  render_overlays.py: clean (no deprecation marker)")


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
