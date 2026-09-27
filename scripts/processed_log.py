#!/usr/bin/env python3
"""
processed_log.py - 处理记录（防重复上传/处理）

记录所有已本地化的源视频（以源视频原始文件名为唯一键），
避免同一视频被重复处理、重复上传 TikTok。

存储位置：<skill_root>/processed-videos.json（跟随 skill，跨项目统一）
记录字段：
  source_file    源视频原始文件名（basename，唯一键）
  source_path    源视频绝对路径
  file_size      文件大小（字节）
  mtime          修改时间戳（检测文件是否更新）
  processed_at   处理完成时间（ISO）
  output_dir     输出目录（tiktok/<视频名简写>/）
  profile        使用的 profile
  status         PASS / WARNING / FAIL
  artifacts      产出的四件套文件名列表

子命令：
  check --video <path>            检查视频是否已处理
  add   --video <path> --output <dir> --profile <p> [--status S]  记录已处理
  list                            列出全部记录

退出码：
  0 = 未处理过（check）/ 记录成功（add）
  1 = 已处理过（check 命中，供调用方判断）
  2 = 致命错误（文件不存在 / 参数缺失）
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone

# skill 根目录 = 本脚本所在目录的上一级
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(SKILL_ROOT, "processed-videos.json")


def load_log() -> dict:
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("records"), list):
                return data
        except Exception:
            pass
    return {"version": 1, "records": []}


def save_log(data: dict) -> None:
    tmp = LOG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LOG_PATH)


def check_video(video_path: str, use_mtime: bool = True) -> dict:
    """检查视频是否已处理（按源文件名匹配；可选 mtime 检测文件是否更新）"""
    if not os.path.exists(video_path):
        return {"status": "error", "message": f"video not found: {video_path}"}

    base = os.path.basename(video_path)
    stat = os.stat(video_path)
    size = stat.st_size
    mtime = int(stat.st_mtime)
    data = load_log()

    matches = [r for r in data["records"] if r.get("source_file") == base]
    if not matches:
        return {"status": "not_processed", "source_file": base}

    # 同名命中：检查文件是否更新（大小/修改时间变化 → 视为新版本）
    current = matches[-1]
    changed = current.get("file_size") != size or current.get("mtime") != mtime
    if changed and use_mtime:
        return {
            "status": "changed",
            "source_file": base,
            "record": current,
            "message": "same filename but file content changed (size/mtime differs)",
        }

    return {
        "status": "processed",
        "source_file": base,
        "record": current,
        "output_dir": current.get("output_dir"),
        "processed_at": current.get("processed_at"),
        "record_status": current.get("status"),
    }


def add_video(video_path: str, output_dir: str, profile: str = "generic",
              status: str = "PASS", artifacts: list = None) -> dict:
    """记录已处理视频（同名覆盖旧记录，保留最新一次）"""
    if not os.path.exists(video_path):
        return {"status": "error", "message": f"video not found: {video_path}"}

    stat = os.stat(video_path)
    base = os.path.basename(video_path)
    record = {
        "source_file": base,
        "source_path": os.path.abspath(video_path),
        "file_size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "processed_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "output_dir": os.path.abspath(output_dir),
        "profile": profile,
        "status": status,
        "artifacts": artifacts or ["final.mp4", "cover.jpg", "subtitles-en.srt", "tiktok-post.md"],
    }

    data = load_log()
    # 同名覆盖（保留最新一次记录，防重复条目）
    data["records"] = [r for r in data["records"] if r.get("source_file") != base]
    data["records"].append(record)
    save_log(data)
    return {"status": "recorded", "source_file": base, "record": record}


def main():
    ap = argparse.ArgumentParser(description="Processed-video log (prevent re-processing)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="Check if a video has been processed")
    p_check.add_argument("--video", required=True)
    p_check.add_argument("--ignore-mtime", action="store_true",
                         help="Match by filename only, ignore size/mtime change")

    p_add = sub.add_parser("add", help="Record a processed video")
    p_add.add_argument("--video", required=True)
    p_add.add_argument("--output", required=True, help="Output directory")
    p_add.add_argument("--profile", default="generic")
    p_add.add_argument("--status", default="PASS")
    p_add.add_argument("--artifacts", nargs="*", default=None)

    sub.add_parser("list", help="List all processed videos")

    args = ap.parse_args()

    if args.cmd == "check":
        result = check_video(args.video, use_mtime=not args.ignore_mtime)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") == "processed":
            sys.exit(1)
        elif result.get("status") == "changed":
            sys.exit(1)
        sys.exit(0)

    elif args.cmd == "add":
        result = add_video(args.video, args.output, args.profile, args.status, args.artifacts)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result.get("status") == "recorded" else 2)

    elif args.cmd == "list":
        data = load_log()
        if not data["records"]:
            print("No processed videos recorded.")
            sys.exit(0)
        print(f"{'source_file':<40} {'status':<8} {'processed_at':<25} {'output_dir'}")
        print("-" * 120)
        for r in data["records"]:
            print(f"{r.get('source_file','')[:38]:<40} {r.get('status',''):<8} "
                  f"{r.get('processed_at',''):<25} {r.get('output_dir','')}")
        sys.exit(0)


if __name__ == "__main__":
    main()
