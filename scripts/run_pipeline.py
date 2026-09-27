#!/usr/bin/env python3
"""
run_pipeline.py - 全流程编排入口

按顺序执行：
  Job 1: analyze-video（probe → 抽帧 → OCR → tracking → ASR）
  Job 2: localize-video（清理 → 翻译 → TTS → 时间轴 → 混音 → 字幕）
  Job 3: generate-cover
  Job 4: generate-post
  QA

注意：Agent 介入点（视频理解、分类、翻译、tts_adjust、cover 文案、post 文案、QA 裁决）
由宿主 Agent 完成，本脚本只编排确定性脚本。
如果缺少 Agent 产出的 JSON，脚本会跳过对应步骤并给出 WARNING。
"""

import argparse
import os
import sys
import json
import subprocess
from typing import Dict, Any, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    save_json, load_json, make_result, log,
    work_path, output_path, ensure_dir, require_tools,
)

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def sanitize_folder_name(name: str, max_len: int = 30) -> str:
    """将视频文件名转换为安全的文件夹名（适当简写）

    规则：
    - 去掉扩展名
    - 替换空格、特殊字符为下划线
    - 去掉连续下划线
    - 截取前 max_len 个字符（太长则简写）
    - 去掉末尾的下划线

    Args:
        name: 原始文件名（不含路径）
        max_len: 最大长度，默认 30

    Returns:
        安全的文件夹名
    """
    import re
    # 去掉扩展名
    base = os.path.splitext(name)[0]
    # 替换非字母数字中文为下划线
    base = re.sub(r'[^\w\u4e00-\u9fff]+', '_', base)
    # 去掉连续下划线
    base = re.sub(r'_+', '_', base)
    # 去掉首尾下划线
    base = base.strip('_')
    # 截取前 max_len 个字符
    if len(base) > max_len:
        base = base[:max_len].rstrip('_')
    return base or "video"


def derive_output_dir(video_path: str) -> str:
    """根据源视频路径自动推导输出目录

    输出目录结构：<源视频所在目录>/tiktok/<视频名简写>/

    示例：
      /Users/jason/Downloads/乔丹十佳球.mp4
      → /Users/jason/Downloads/tiktok/乔丹十佳球/

      /Users/jason/Downloads/闲庭信步｜游刃有余🔥GOAT高光瞬间 #乔丹#NBA#篮球之神_xWT111.mp4
      → /Users/jason/Downloads/tiktok/闲庭信步_游刃有余_GOAT高光瞬间_乔丹_NBA_篮/

    Args:
        video_path: 源视频路径

    Returns:
        自动推导的输出目录绝对路径
    """
    video_dir = os.path.dirname(os.path.abspath(video_path))
    video_name = os.path.basename(video_path)
    folder_name = sanitize_folder_name(video_name)
    return os.path.join(video_dir, "tiktok", folder_name)


def derive_work_dir(video_path: str) -> str:
    """根据源视频路径自动推导工作目录（中间产物）

    工作目录放在输出目录下的 work/ 子目录，方便统一管理。

    Args:
        video_path: 源视频路径

    Returns:
        自动推导的工作目录绝对路径
    """
    return os.path.join(derive_output_dir(video_path), "work")


def run_script(script_name: str, args: List[str], work_dir: str,
               description: str = "") -> int:
    """运行一个脚本，返回退出码"""
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    cmd = [sys.executable, script_path] + args
    log.info(f"▶ {description or script_name}")
    log.debug(f"  CMD: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    log.info(f"  {line.strip()}")
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                if line.strip() and not line.startswith("["):
                    log.debug(f"  {line.strip()}")
        if result.returncode != 0:
            log.warning(f"  {script_name} exited with code {result.returncode}")
        return result.returncode
    except Exception as e:
        log.error(f"  Failed to run {script_name}: {e}")
        return 2


def job_analyze(video: str, work_dir: str, resume: bool = False) -> Dict:
    """Job 1: 分析视频"""
    log.info("=" * 60)
    log.info("Job 1: analyze-video")
    log.info("=" * 60)

    steps = [
        ("video_probe.py", ["--video", video, "--work-dir", work_dir], "Probe video metadata"),
        ("extract_frames.py", ["--video", video, "--work-dir", work_dir], "Extract coarse frames (1FPS + scene)"),
        ("ocr_video.py", ["--work-dir", work_dir], "OCR pass 1 (coarse)"),
        ("detect_text_regions.py", ["--work-dir", work_dir, "--emit-dense-windows"], "Detect text regions + dense windows"),
        ("extract_frames.py", ["--video", video, "--work-dir", work_dir,
                                "--dense-windows", work_path(work_dir, "dense-windows.json")],
         "Extract dense frames (suspect windows only)"),
        ("ocr_video.py", ["--work-dir", work_dir, "--pass", "dense"], "OCR pass 2 (dense)"),
        ("track_text.py", ["--work-dir", work_dir], "Track text across frames"),
        ("audio_processor.py", ["asr-only", "--video", video, "--work-dir", work_dir], "ASR speech recognition"),
    ]

    results = {}
    for script, args, desc in steps:
        # 断点续跑：检查产物是否已存在
        if resume:
            output_map = {
                "video_probe.py": "probe.json",
                "extract_frames.py": "frames-index.json",
                "ocr_video.py": "ocr.json",
                "detect_text_regions.py": "text-regions.json",
                "track_text.py": "text-tracks.json",
                "audio_processor.py": "asr.json",
            }
            expected = output_map.get(script)
            if expected and os.path.exists(work_path(work_dir, expected)):
                log.info(f"  ⏭ Skipping {script} (output exists)")
                results[script] = 0
                continue

        code = run_script(script, args, work_dir, desc)
        results[script] = code
        if code == 2:  # 致命错误
            log.error(f"Fatal error in {script}, aborting pipeline")
            break

    return results


def job_localize(video: str, work_dir: str, output_dir: str,
                  logo: Optional[str] = None, bgm: Optional[str] = None,
                  resume: bool = False) -> Dict:
    """Job 2: 本地化视频"""
    log.info("=" * 60)
    log.info("Job 2: localize-video")
    log.info("=" * 60)

    # 检查 localization-plan 是否存在（Agent 产出）
    plan_path = work_path(work_dir, "localization-plan.json")
    if not os.path.exists(plan_path):
        log.warning("localization-plan.json not found (Agent output). "
                    "Creating minimal plan with no text processing.")
        asr = load_json(work_path(work_dir, "asr.json"), {})
        has_chinese = asr.get("has_chinese_narration", False)
        minimal_plan = {
            "profile": "generic",
            "review_mode": False,
            "audio_strategy": "mute_original_tts_bgm" if has_chinese else "keep_original",
            "bgm": {"asset_source": "user_input" if bgm else "none",
                    "asset_path": bgm, "style_hint": "generic"},
            "items": [],
        }
        save_json(plan_path, minimal_plan)

    steps = [
        ("generate_masks.py", ["--work-dir", work_dir, "--render-images"], "Generate masks"),
        ("clean_video.py", ["--video", video, "--work-dir", work_dir, "--method", "opencv"], "Clean video (opencv inpaint + blur, no black bars)"),
        ("audio_processor.py", ["tts", "--work-dir", work_dir], "TTS synthesis"),
        ("tts_timing.py", ["measure", "--work-dir", work_dir], "Measure TTS timing"),
        ("tts_timing.py", ["apply", "--work-dir", work_dir], "Apply timing decisions"),
        ("audio_processor.py", ["mix", "--video", video, "--work-dir", work_dir,
                                 "--output-dir", output_dir], "Mix audio"),
        ("render_subtitles.py", ["--work-dir", work_dir, "--output-dir", output_dir,
                                  "--video", os.path.join(output_dir, "final.mp4"), "--burn"],
         "Render and burn subtitles"),
    ]

    results = {}
    for script, args, desc in steps:
        if resume:
            output_map = {
                "generate_masks.py": "masks/masks-index.json",
                "clean_video.py": "cleaned.mp4",
                "audio_processor.py": "tts-result.json",
                "tts_timing.py": "timeline.json",
                "render_subtitles.py": "../output/subtitles-en.srt",
            }
            expected = output_map.get(script)
            if expected:
                expected_path = os.path.join(work_dir, expected) if not expected.startswith("../") else os.path.normpath(os.path.join(work_dir, expected))
                if os.path.exists(expected_path):
                    log.info(f"  ⏭ Skipping {script} (output exists)")
                    results[script] = 0
                    continue

        code = run_script(script, args, work_dir, desc)
        results[script] = code
        if code == 2:
            log.error(f"Fatal error in {script}, aborting pipeline")
            break

    return results


def job_cover(work_dir: str, output_dir: str, profile: str = "generic",
              resume: bool = False) -> Dict:
    """Job 3: 生成封面"""
    log.info("=" * 60)
    log.info("Job 3: generate-cover")
    log.info("=" * 60)

    if resume and os.path.exists(output_path(output_dir, "cover.jpg")):
        log.info("  ⏭ Skipping cover generation (output exists)")
        return {"generate_cover.py": 0}

    code = run_script(
        "generate_cover.py",
        ["--work-dir", work_dir, "--output-dir", output_dir, "--profile", profile],
        work_dir, "Generate cover image",
    )
    return {"generate_cover.py": code}


def job_post(work_dir: str, output_dir: str, profile: str = "generic",
             resume: bool = False) -> Dict:
    """Job 4: 生成发布文案"""
    log.info("=" * 60)
    log.info("Job 4: generate-post")
    log.info("=" * 60)

    if resume and os.path.exists(output_path(output_dir, "tiktok-post.md")):
        log.info("  ⏭ Skipping post generation (output exists)")
        return {"generate_post.py": 0}

    code = run_script(
        "generate_post.py",
        ["--work-dir", work_dir, "--output-dir", output_dir, "--profile", profile],
        work_dir, "Generate TikTok post markdown",
    )
    return {"generate_post.py": code}


def job_qa(work_dir: str, output_dir: str) -> Dict:
    """QA 检查"""
    log.info("=" * 60)
    log.info("QA: quality assurance")
    log.info("=" * 60)

    code = run_script(
        "qa_video.py",
        ["--work-dir", work_dir, "--output-dir", output_dir],
        work_dir, "Run QA checks",
    )
    return {"qa_video.py": code}


def main():
    ap = argparse.ArgumentParser(description="TikTok Video Localizer - full pipeline")
    ap.add_argument("--input", required=True, help="Path to input video")
    ap.add_argument("--work-dir", default=None,
                    help="Working directory for intermediate files (default: <video_dir>/tiktok/<video_name>/work)")
    ap.add_argument("--output", default=None,
                    help="Output directory for final files (default: <video_dir>/tiktok/<video_name>/)")
    ap.add_argument("--profile", default="generic", choices=["nba", "sports", "generic"], help="Content profile")
    ap.add_argument("--review", action="store_true", help="Enable review mode for low-confidence items")
    ap.add_argument("--logo", help="Path to user brand logo (for logo replace)")
    ap.add_argument("--bgm", help="Path to user BGM asset")
    ap.add_argument("--resume", action="store_true", help="Resume from existing intermediate files")
    ap.add_argument("--force", action="store_true",
                    help="Re-process even if the source video is already recorded as processed")
    ap.add_argument("--ignore-mtime", action="store_true",
                    help="Processed-check matches by filename only, ignore file change (size/mtime)")
    ap.add_argument("--skip", nargs="*", default=[], help="Steps to skip (analyze/localize/cover/post/qa)")
    args = ap.parse_args()

    require_tools("ffmpeg", "ffprobe")

    # ═══ 处理记录：防重复处理/上传 ═══
    # 以源视频原始文件名为键。已处理过且文件未变化 → 直接提示输出位置并退出（除非 --force）
    from processed_log import check_video, add_video
    check_result = check_video(args.input, use_mtime=not args.ignore_mtime)
    if check_result.get("status") in ("processed", "changed") and not args.force:
        log.warning("=" * 60)
        log.warning("VIDEO ALREADY PROCESSED - skipping to avoid duplicate upload")
        log.warning(f"  Source file : {os.path.basename(args.input)}")
        log.warning(f"  Output dir  : {check_result.get('output_dir')}")
        log.warning(f"  Processed at: {check_result.get('processed_at')} (status={check_result.get('status')})")
        log.warning("  → Re-run with --force to process it again.")
        log.warning("=" * 60)
        sys.exit(0)

    # 自动推导输出目录和工作目录（如果未指定）
    if args.output is None:
        args.output = derive_output_dir(args.input)
    if args.work_dir is None:
        args.work_dir = derive_work_dir(args.input)

    # 准备目录
    ensure_dir(args.work_dir)
    ensure_dir(args.output)

    # 保存运行配置
    config = {
        "input": os.path.abspath(args.input),
        "work_dir": os.path.abspath(args.work_dir),
        "output_dir": os.path.abspath(args.output),
        "profile": args.profile,
        "review_mode": args.review,
        "logo": args.logo,
        "bgm": args.bgm,
    }
    save_json(work_path(args.work_dir, "pipeline-config.json"), config)

    log.info("TikTok Video Localizer - Pipeline Start")
    log.info(f"  Input: {args.input}")
    log.info(f"  Profile: {args.profile}")
    log.info(f"  Review mode: {args.review}")
    log.info("")

    all_results = {}

    # Job 1: Analyze
    if "analyze" not in args.skip:
        all_results["analyze"] = job_analyze(args.input, args.work_dir, args.resume)
    else:
        log.info("⏭ Skipping analyze step")

    # Job 2: Localize
    if "localize" not in args.skip:
        all_results["localize"] = job_localize(
            args.input, args.work_dir, args.output, args.logo, args.bgm, args.resume,
        )
    else:
        log.info("⏭ Skipping localize step")

    # Job 3: Cover
    if "cover" not in args.skip:
        all_results["cover"] = job_cover(args.work_dir, args.output, args.profile, args.resume)
    else:
        log.info("⏭ Skipping cover step")

    # Job 4: Post
    if "post" not in args.skip:
        all_results["post"] = job_post(args.work_dir, args.output, args.profile, args.resume)
    else:
        log.info("⏭ Skipping post step")

    # QA
    if "qa" not in args.skip:
        all_results["qa"] = job_qa(args.work_dir, args.output)
    else:
        log.info("⏭ Skipping QA step")

    # 汇总
    log.info("")
    log.info("=" * 60)
    log.info("Pipeline Complete")
    log.info("=" * 60)

    # 检查输出文件
    outputs = ["final.mp4", "cover.jpg", "subtitles-en.srt", "tiktok-post.md"]
    log.info("Output files:")
    for f in outputs:
        path = os.path.join(args.output, f)
        exists = os.path.exists(path)
        size = os.path.getsize(path) if exists else 0
        log.info(f"  {'✅' if exists else '❌'} {f} ({size/1024:.1f} KB)" if exists else f"  ❌ {f} (missing)")

    # QA 结果
    qa_path = work_path(args.work_dir, "qa.json")
    if os.path.exists(qa_path):
        qa = load_json(qa_path, {})
        log.info(f"\nQA Status: {qa.get('overall', 'UNKNOWN')}")
        if qa.get("degradations"):
            log.warning(f"  Degradations: {len(qa['degradations'])}")
        if qa.get("review_items"):
            log.warning(f"  Review items: {len(qa['review_items'])}")

    log.info(f"\nIntermediate files: {args.work_dir}/")
    log.info(f"Final output: {args.output}/")

    # ═══ 处理记录：完成后记录（防重复上传）═══
    qa_status = "UNKNOWN"
    qa_path = work_path(args.work_dir, "qa.json")
    if os.path.exists(qa_path):
        qa_status = load_json(qa_path, {}).get("overall", "UNKNOWN")
    rec = add_video(args.input, args.output, args.profile, qa_status)
    log.info(f"Processed-log updated: {rec.get('source_file')} -> status={qa_status}")

    # 保存运行结果
    save_json(work_path(args.work_dir, "pipeline-result.json"), all_results)


if __name__ == "__main__":
    main()
