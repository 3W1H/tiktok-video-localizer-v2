#!/usr/bin/env python3
"""
run_pipeline.py - 全流程编排入口

按顺序执行（封面优先：analyze 之后立即出封面，视频处理后台继续）：
  Job 1: analyze-video（probe → 抽帧 → OCR → tracking → ASR）
  Job 3: generate-cover（封面优先出图，不再等视频清理/TTS/字幕）
  Job 2: localize-video（清理 → 翻译 → TTS → 时间轴 → 混音 → 字幕）
  Job 4: generate-post
  QA

`--input` 可以是单个视频文件，也可以是一个目录（批量模式）：目录模式下
自动扫描目录下的视频文件，逐个查 processed-videos.json，已处理的跳过、
未处理的才进入处理。

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
    EXIT_OK, EXIT_DEGRADED, EXIT_FATAL,
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


class StepProgress:
    """按「步骤签名」记录已完成的步骤，供 --resume 断点续跑。

    ⚠️ 之前是按「脚本名 → 产物文件名」查存在性来判断跳过，这在**同一个脚本出现
    两次**时是错的：Job 1 里 `extract_frames.py` / `ocr_video.py` 各有两遍
    （coarse 与 dense），coarse 跑完后 `frames-index.json` / `ocr.json` 已存在，
    于是 **dense 那一遍被静默跳过** —— 表现为轨迹时间窗只有 1FPS 精度、
    首尾有中文残留，而日志看起来一切正常（2026-09-27 实测发现）。

    改为记录 (脚本, 参数) 的签名，同一个脚本的第二遍不会被误判为已完成。
    """

    def __init__(self, work_dir: str):
        self.path = work_path(work_dir, ".pipeline-steps.json")
        self.done = set(load_json(self.path, {}).get("done", []))

    @staticmethod
    def _key(script: str, args: List[str]) -> str:
        return f"{script}|{' '.join(args)}"

    def is_done(self, script: str, args: List[str]) -> bool:
        return self._key(script, args) in self.done

    def mark(self, script: str, args: List[str]) -> None:
        self.done.add(self._key(script, args))
        save_json(self.path, {"done": sorted(self.done)})


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
    progress = StepProgress(work_dir)
    for script, args, desc in steps:
        # 断点续跑：按 (脚本, 参数) 签名判断，避免同一脚本的第二遍被误跳过
        if resume and progress.is_done(script, args):
            log.info(f"  ⏭ Skipping {script} (already done with these args)")
            results[script] = 0
            continue

        code = run_script(script, args, work_dir, desc)
        results[script] = code
        if code == 2:  # 致命错误
            log.error(f"Fatal error in {script}, aborting pipeline")
            break
        if code == 0:
            progress.mark(script, args)

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
    progress = StepProgress(work_dir)
    for script, args, desc in steps:
        # 断点续跑：按 (脚本, 参数) 签名判断。
        # ⚠️ 旧实现按「脚本名 → 产物名」查存在性，遇到同一脚本出现两次
        # （audio_processor.py 既有 tts 又有 mix）时会把 mix 静默跳过。
        if resume and progress.is_done(script, args):
            log.info(f"  ⏭ Skipping {script} (already done with these args)")
            results[script] = 0
            continue

        code = run_script(script, args, work_dir, desc)
        results[script] = code
        if code == 2:
            log.error(f"Fatal error in {script}, aborting pipeline")
            break
        if code == 0:
            progress.mark(script, args)

    return results


def job_cover(work_dir: str, output_dir: str, profile: str = "generic",
              resume: bool = False, cover_image: Optional[str] = None) -> Dict:
    """Job 3: 生成封面（封面优先，在 localize 之前执行）"""
    log.info("=" * 60)
    log.info("Job 3: generate-cover (cover-first)")
    log.info("=" * 60)

    if resume and os.path.exists(output_path(output_dir, "cover.jpg")):
        log.info("  ⏭ Skipping cover generation (output exists)")
        return {"generate_cover.py": 0}

    cover_args = ["--work-dir", work_dir, "--output-dir", output_dir,
                  "--profile", profile]
    if cover_image:
        cover_args += ["--cover-image", cover_image]

    code = run_script(
        "generate_cover.py",
        cover_args,
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


VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".flv", ".ts"}


def scan_videos(input_dir: str) -> List[str]:
    """扫描目录下所有视频文件（顶层，不递归，忽略隐藏文件）"""
    if not os.path.isdir(input_dir):
        return []
    videos = []
    for name in sorted(os.listdir(input_dir)):
        if name.startswith("."):
            continue
        full = os.path.join(input_dir, name)
        if os.path.isfile(full) and os.path.splitext(name)[1].lower() in VIDEO_EXTS:
            videos.append(full)
    return videos


def process_one(video: str, args) -> Dict[str, Any]:
    """处理单个视频：查重 → analyze → cover(优先) → localize → post → qa → 记录。

    封面优先：封面在 analyze 之后立即生成（不再等视频清理/TTS/字幕），
    视频处理（localize）随后继续。返回单视频处理结果摘要。
    """
    from processed_log import check_video, add_video

    check_result = check_video(video, use_mtime=not args.ignore_mtime)
    if check_result.get("status") in ("processed", "changed") and not args.force:
        log.info("=" * 60)
        log.info(f"⏭ SKIP (already processed): {os.path.basename(video)}")
        log.info(f"  Output dir  : {check_result.get('output_dir')}")
        log.info(f"  Processed at: {check_result.get('processed_at')} (status={check_result.get('status')})")
        log.info("  → Re-run with --force to process it again.")
        log.info("=" * 60)
        return {"video": video, "status": "skipped",
                "output_dir": check_result.get("output_dir")}

    # 自动推导输出目录和工作目录（单视频可被 --output/--work-dir 覆盖；
    # 批量模式下这些参数无效，逐个视频独立推导）
    output_dir = args.output or derive_output_dir(video)
    work_dir = args.work_dir or derive_work_dir(video)

    ensure_dir(work_dir)
    ensure_dir(output_dir)

    save_json(work_path(work_dir, "pipeline-config.json"), {
        "input": os.path.abspath(video),
        "work_dir": os.path.abspath(work_dir),
        "output_dir": os.path.abspath(output_dir),
        "profile": args.profile,
        "review_mode": args.review,
        "logo": args.logo,
        "bgm": args.bgm,
    })

    log.info("=" * 60)
    log.info(f"Pipeline Start: {os.path.basename(video)}")
    log.info(f"  Input: {video}")
    log.info(f"  Profile: {args.profile}")
    log.info(f"  Output: {output_dir}")
    log.info("=" * 60)

    all_results = {}

    # Job 1: Analyze
    if "analyze" not in args.skip:
        all_results["analyze"] = job_analyze(video, work_dir, args.resume)
    else:
        log.info("⏭ Skipping analyze step")

    # Job 3: Cover（封面优先，提前到 localize 之前）
    if "cover" not in args.skip:
        all_results["cover"] = job_cover(
            work_dir, output_dir, args.profile, args.resume,
            getattr(args, "cover_image", None),
        )
    else:
        log.info("⏭ Skipping cover step")

    # Job 2: Localize（视频处理，封面出图后继续）
    if "localize" not in args.skip:
        all_results["localize"] = job_localize(
            video, work_dir, output_dir, args.logo, args.bgm, args.resume,
        )
    else:
        log.info("⏭ Skipping localize step")

    # Job 4: Post
    if "post" not in args.skip:
        all_results["post"] = job_post(work_dir, output_dir, args.profile, args.resume)
    else:
        log.info("⏭ Skipping post step")

    # QA
    if "qa" not in args.skip:
        all_results["qa"] = job_qa(work_dir, output_dir)
    else:
        log.info("⏭ Skipping QA step")

    # 汇总
    log.info("")
    log.info("=" * 60)
    log.info(f"Pipeline Complete: {os.path.basename(video)}")
    log.info("=" * 60)

    outputs = ["final.mp4", "cover.jpg", "subtitles-en.srt", "tiktok-post.md"]
    for f in outputs:
        path = os.path.join(output_dir, f)
        exists = os.path.exists(path)
        size = os.path.getsize(path) if exists else 0
        log.info(f"  {'✅' if exists else '❌'} {f} ({size/1024:.1f} KB)" if exists else f"  ❌ {f} (missing)")

    qa_status = "UNKNOWN"
    qa_path = work_path(work_dir, "qa.json")
    if os.path.exists(qa_path):
        qa = load_json(qa_path, {})
        qa_status = qa.get("overall", "UNKNOWN")
        log.info(f"\nQA Status: {qa_status}")
        if qa.get("degradations"):
            log.warning(f"  Degradations: {len(qa['degradations'])}")
        if qa.get("review_items"):
            log.warning(f"  Review items: {len(qa['review_items'])}")

    rec = add_video(video, output_dir, args.profile, qa_status)
    log.info(f"Processed-log updated: {rec.get('source_file')} -> status={qa_status}")

    save_json(work_path(work_dir, "pipeline-result.json"), all_results)
    return {"video": video, "status": "processed", "output_dir": output_dir,
            "qa_status": qa_status}


def main():
    ap = argparse.ArgumentParser(
        description="TikTok Video Localizer - full pipeline "
                    "(accepts a single video file or a directory of videos)")
    ap.add_argument("--input", required=True,
                    help="Path to input video file, or a directory to batch-process")
    ap.add_argument("--work-dir", default=None,
                    help="Working directory for intermediate files "
                         "(single video only; default: <video_dir>/tiktok/<video_name>/work)")
    ap.add_argument("--output", default=None,
                    help="Output directory for final files "
                         "(single video only; default: <video_dir>/tiktok/<video_name>/)")
    ap.add_argument("--profile", default="generic", choices=["nba", "sports", "generic"],
                    help="Content profile")
    ap.add_argument("--review", action="store_true",
                    help="Enable review mode for low-confidence items")
    ap.add_argument("--logo", help="Path to user brand logo (for logo replace)")
    ap.add_argument("--bgm", help="Path to user BGM asset")
    ap.add_argument("--cover-image", help="Path to user-provided cover image (skips frame selection)")
    ap.add_argument("--resume", action="store_true",
                    help="Resume from existing intermediate files")
    ap.add_argument("--force", action="store_true",
                    help="Re-process even if the source video is already recorded as processed")
    ap.add_argument("--ignore-mtime", action="store_true",
                    help="Processed-check matches by filename only, ignore file change (size/mtime)")
    ap.add_argument("--skip", nargs="*", default=[],
                    help="Steps to skip (analyze/localize/cover/post/qa)")
    args = ap.parse_args()

    require_tools("ffmpeg", "ffprobe")

    # ═══ 输入判定：单视频文件 vs 目录批量 ═══
    if os.path.isdir(args.input):
        if args.output or args.work_dir:
            log.warning("--output/--work-dir are ignored in directory batch mode; "
                        "each video derives its own output dir.")
        videos = scan_videos(args.input)
        if not videos:
            log.error(f"No video files found in directory: {args.input}")
            sys.exit(EXIT_FATAL)
        log.info("=" * 60)
        log.info(f"Batch mode: {len(videos)} video(s) found in {args.input}")
        log.info("=" * 60)

        # 目录批量下 --output/--work-dir 无效，逐个视频独立推导
        args.output = None
        args.work_dir = None

        summary = []
        for v in videos:
            summary.append(process_one(v, args))

        log.info("")
        log.info("=" * 60)
        log.info("Batch Complete")
        log.info("=" * 60)
        processed = [s for s in summary if s["status"] == "processed"]
        skipped = [s for s in summary if s["status"] == "skipped"]
        log.info(f"  Processed: {len(processed)}")
        log.info(f"  Skipped  : {len(skipped)} (already in processed-videos.json)")
        for s in summary:
            marker = "✅" if s["status"] == "processed" else "⏭"
            log.info(f"  {marker} {os.path.basename(s['video'])}"
                     + (f" -> {s.get('output_dir')}" if s.get("output_dir") else ""))
        log.info("=" * 60)
    else:
        if not os.path.exists(args.input):
            log.error(f"Input not found: {args.input}")
            sys.exit(EXIT_FATAL)
        process_one(args.input, args)


if __name__ == "__main__":
    main()
