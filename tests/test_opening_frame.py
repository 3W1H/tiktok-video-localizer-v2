#!/usr/bin/env python3
"""Regression guard for the "no opening title card" rule (2026-10-25).

Why this exists:
  用户明确否掉了在视频开头贴一张封面/标题卡的做法：
  「封面已经加了视频的内容和我的 logo，不要再在头帧再加 logo 或者视频内容
   这一帧，有点冗余了，直接就进入视频正题」。
  规范落在 SKILL.md §11.1，代码兜底有两处：
    1. originalize_video.py -> 片头卡默认关闭，--title 只在 --title-card 时生效
    2. qa_video.py::check_opening_frame -> 三条判据都只给 WARNING，不 fail
  这个测试把两侧都钉住，防止以后有人把默认值改回去。

Run:
    /Users/jason/opt/miniconda3/bin/python tests/test_opening_frame.py
需要 ffmpeg（用 lavfi 合成素材，不需要真实视频 fixture）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_clip(path: str, seconds: float, moving: bool):
    """moving=True → testsrc2（有运动）；False → 纯色静止画面。"""
    src = "testsrc2=size=360x640:rate=30" if moving else "color=c=0x334455:size=360x640:rate=30"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", src,
         "-t", str(seconds), "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-preset", "ultrafast", path],
        check=True,
    )


def grab_first_frame(video: str, out: str):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", video, "-frames:v", "1", out],
        check=True,
    )


def main():
    print("== originalize_video.py: 片头卡默认关闭 ==")
    orig = load_module("orig_test", "originalize_video.py")

    src_text = open(os.path.join(SCRIPTS, "originalize_video.py"), encoding="utf-8").read()
    check("--title-card 开关存在",
          '"--title-card"' in src_text)
    check("--title 帮助文案标注 LEGACY",
          "[LEGACY] 标题卡主标题" in src_text)
    check("默认分支不含标题卡调用",
          'if args.title_card and args.title' in src_text)
    check("默认关闭时有 WARNING 提示",
          "未加 --title-card，已忽略" in src_text)
    # 函数本身仍在（LEGACY 能力保留）
    check("step_title_card 仍保留", hasattr(orig, "step_title_card"))

    print("\n== qa_video.py: check_opening_frame 三条判据 ==")
    qa = load_module("qa_test", "qa_video.py")
    check("check_opening_frame 存在", hasattr(qa, "check_opening_frame"))

    tmp = tempfile.mkdtemp(prefix="ttl-test-open-")

    # case A：正片直入（画面在动）→ pass
    a = os.path.join(tmp, "a")
    os.makedirs(os.path.join(a, "work"))
    make_clip(os.path.join(a, "final.mp4"), 3.0, moving=True)
    grab_first_frame(os.path.join(a, "final.mp4"), os.path.join(a, "cover.jpg"))
    r = qa.check_opening_frame(a, os.path.join(a, "work"))
    # 注意：cover.jpg 就是首帧 → 判据 3 应命中，但这里只验证「不崩 + 结构完整」
    check("判据结构完整",
          all(k in r for k in ("status", "detail", "opening_static",
                               "opening_static_delta", "frame0_vs_cover_mae")))
    check("封面=首帧 → 判据 3 命中（warning）",
          r["status"] == "warning" and r["frame0_vs_cover_mae"] is not None
          and r["frame0_vs_cover_mae"] < 6.0,
          f"mae={r['frame0_vs_cover_mae']}, status={r['status']}")

    # case B：静止卡 → 判据 2 命中
    b = os.path.join(tmp, "b")
    os.makedirs(os.path.join(b, "work"))
    make_clip(os.path.join(b, "final.mp4"), 3.0, moving=False)
    # 用另一张完全不同的「封面」避免判据 3 干扰：纯白
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=white:size=360x640", "-frames:v", "1",
                    os.path.join(b, "cover.jpg")], check=True)
    r = qa.check_opening_frame(b, os.path.join(b, "work"))
    check("静止画面 → 判据 2 命中（warning）",
          r["status"] == "warning" and r["opening_static"] is True,
          f"delta={r['opening_static_delta']}, status={r['status']}")

    # case C：元数据自报 opening_title_card=true → 判据 1 命中
    c = os.path.join(tmp, "c")
    os.makedirs(os.path.join(c, "work"))
    make_clip(os.path.join(c, "final.mp4"), 3.0, moving=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=white:size=360x640", "-frames:v", "1",
                    os.path.join(c, "cover.jpg")], check=True)
    with open(os.path.join(c, "work", "originalize-result.json"), "w") as f:
        json.dump({"opening_title_card": True}, f)
    r = qa.check_opening_frame(c, os.path.join(c, "work"))
    check("opening_title_card=true → 判据 1 命中（warning）",
          r["status"] == "warning" and any("opening_title_card" in i for i in r["issues"]),
          r["detail"])

    # case D：动画面 + 差异大的封面 → pass
    d = os.path.join(tmp, "d")
    os.makedirs(os.path.join(d, "work"))
    make_clip(os.path.join(d, "final.mp4"), 3.0, moving=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=white:size=1080x1920", "-frames:v", "1",
                    os.path.join(d, "cover.jpg")], check=True)
    r = qa.check_opening_frame(d, os.path.join(d, "work"))
    check("正片直入 → pass", r["status"] == "pass", r["detail"])

    # case E：final.mp4 缺失 → warning 而不是崩溃
    e = os.path.join(tmp, "e")
    os.makedirs(os.path.join(e, "work"))
    r = qa.check_opening_frame(e, os.path.join(e, "work"))
    check("缺 final.mp4 → warning（不崩）", r["status"] == "warning")

    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 48}\nPASS {PASS} / {PASS + FAIL}")
    if FAIL:
        print(f"FAILED {FAIL}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
