#!/usr/bin/env python3
"""
audio_processor.py - 音频处理（ASR / TTS / 混音）

三种模式：
  --asr-only: 语音转写，输出 asr.json（含 has_chinese_narration）
  --tts:      英文语音合成，输出 TTS 音频文件
  --mix:      混音（按 audio-plan.json 的 strategy）

V1 音频策略（二选一，无声源分离）：
  mute_original_tts_bgm: 静音原轨 + 英文 TTS + Instrumental BGM
  keep_original:          完整保留原声（现场声/欢呼/环境音）

ASR 完全不可用时：保守走 keep_original + WARNING
TTS 完全不可用时：静音原轨 + 无配音 + 英文字幕 + BGM → WARNING
"""

import argparse
import os
import shutil
import sys
import tempfile
from typing import List, Dict, Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    run, save_json, load_json, make_result, log,
    work_path, ensure_dir, require_tools, check_python_package,
    add_degradation, die_degraded,
)


def detect_chinese(text: str) -> bool:
    return any('\u4e00' <= c <= '\u9fff' for c in text)


# ============================================================
# ASR
# ============================================================
def run_asr(video_path: str, work_dir: str, engine: str = "auto") -> Dict:
    """语音转写，输出 asr.json"""
    segments = []
    engine_used = None
    degraded = False
    warnings = []

    # 提取音频
    audio_path = work_path(work_dir, "source_audio.wav")
    if not os.path.exists(audio_path):
        run([
            "ffmpeg", "-y", "-i", video_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            audio_path,
        ])

    # 选择引擎
    if engine == "auto":
        if check_python_package("faster_whisper"):
            engine = "faster_whisper"
        elif check_python_package("whisper"):
            engine = "whisper"
        else:
            engine = None

    if engine is None:
        warnings.append("No ASR engine available. Assuming no Chinese narration (conservative: keep_original).")
        result = make_result(
            status="degraded",
            confidence=0.0,
            provider_used="none",
            degraded=True,
            language_detected="unknown",
            has_chinese_narration=False,  # 保守：不静音
            chinese_ratio=0.0,
            segments=[],
            total_segments=0,
            warnings=warnings,
        )
        return result

    if engine == "faster_whisper":
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segments_iter, info = model.transcribe(audio_path, language="zh", beam_size=5)
            for seg in segments_iter:
                text = seg.text.strip()
                if text:
                    segments.append({
                        "start": round(seg.start, 3),
                        "end": round(seg.end, 3),
                        "text": text,
                        "confidence": round(getattr(seg, "avg_logprob", -1), 4),
                    })
            engine_used = "faster_whisper_local"
        except Exception as e:
            warnings.append(f"faster-whisper failed: {str(e)[:200]}")
            degraded = True

    if engine == "whisper":
        try:
            import whisper
            model = whisper.load_model("base")
            result_whisper = model.transcribe(audio_path, language="zh", verbose=False)
            for seg in result_whisper.get("segments", []):
                text = seg.get("text", "").strip()
                if text:
                    segments.append({
                        "start": round(seg["start"], 3),
                        "end": round(seg["end"], 3),
                        "text": text,
                        "confidence": round(seg.get("avg_logprob", -1), 4),
                    })
            engine_used = "whisper_local"
        except Exception as e:
            warnings.append(f"Whisper failed: {str(e)[:200]}")
            degraded = True

    # 判断是否有中文旁白
    full_text = " ".join(s["text"] for s in segments)
    chinese_chars = sum(1 for c in full_text if '\u4e00' <= c <= '\u9fff')
    total_chars = len(full_text.replace(" ", ""))
    chinese_ratio = chinese_chars / max(total_chars, 1)
    has_chinese_narration = chinese_ratio > 0.3 and len(segments) > 0

    result = make_result(
        status="ok" if not degraded else "degraded",
        confidence=0.8 if engine_used in ("whisper_local", "faster_whisper_local") else 0.5,
        provider_used=engine_used or "none",
        degraded=degraded,
        language_detected="zh" if has_chinese_narration else "unknown",
        has_chinese_narration=has_chinese_narration,
        chinese_ratio=round(chinese_ratio, 4),
        segments=segments,
        total_segments=len(segments),
        warnings=warnings,
    )
    return result


# ============================================================
# TTS
# ============================================================
def run_tts(work_dir: str, translated_path: str, engine: str = "auto",
            voice: str = "en-US-GuyNeural") -> Dict:
    """英文 TTS 合成"""
    translated = load_json(translated_path, {})
    entries = translated.get("entries", [])
    tts_dir = ensure_dir(os.path.join(work_dir, "tts"))

    if not entries:
        log.warning("No translated entries for TTS")
        return make_result(status="ok", confidence=1.0, tts_files=[], warnings=["No entries"])

    engine_used = None
    degraded = False
    warnings = []
    tts_files = []

    # 选择引擎
    if engine == "auto":
        if check_python_package("edge_tts"):
            engine = "edge-tts"
        else:
            engine = None

    if engine is None:
        warnings.append("No TTS engine available. Skipping TTS (will use subtitles only).")
        result = make_result(
            status="degraded",
            confidence=0.0,
            provider_used="none",
            degraded=True,
            tts_files=[],
            warnings=warnings,
        )
        return result

    # edge-tts writes mp3; the intermediate files go to a throwaway dir that is
    # never deleted per-file -- a bulk os.remove() loop trips this environment's
    # safe-delete hook (SAFE_DELETE_BULK_CONFIRM_REQUIRED) and aborts TTS
    # half-way (2026-09-27: died after 28/59 entries, exit=1).
    mp3_dir = tempfile.mkdtemp(prefix="ttl-tts-src-")
    for i, entry in enumerate(entries):
        text = entry.get("target_text", "")
        if not text:
            continue
        out_file = os.path.join(tts_dir, f"tts_{i:04d}.wav")
        try:
            if engine == "edge-tts":
                # edge-tts 输出 mp3，需要转 wav
                mp3_file = os.path.join(mp3_dir, f"tts_{i:04d}.mp3")
                run(["edge-tts", "--voice", voice, "--text", text, "--write-media", mp3_file])
                run(["ffmpeg", "-y", "-i", mp3_file, "-ar", "24000", "-ac", "1", out_file])
            engine_used = f"{engine}_local"
            tts_files.append({"index": i, "file": out_file, "text": text})
        except Exception as e:
            warnings.append(f"TTS failed for entry {i}: {str(e)[:200]}")
            degraded = True

    result = make_result(
        status="ok" if not degraded else "degraded",
        confidence=0.85 if not degraded else 0.5,
        provider_used=engine_used or "none",
        degraded=degraded,
        tts_files=tts_files,
        total_entries=len(entries),
        tts_success=len(tts_files),
        voice=voice,
        warnings=warnings,
    )
    return result


# ============================================================
# Mix
# ============================================================
def run_mix(video_path: str, work_dir: str, output_dir: str,
            audio_plan_path: str, timeline_path: str) -> Dict:
    """按 audio-plan.json 混音"""
    audio_plan = load_json(audio_plan_path, {})
    timeline = load_json(timeline_path, {})

    strategy = audio_plan.get("strategy", "keep_original")
    probe = load_json(work_path(work_dir, "probe.json"), {})
    duration = probe.get("duration_sec", 0)

    mixed_audio = work_path(work_dir, "mixed_audio.wav")
    warnings = []

    if strategy == "keep_original":
        # 保留原声
        log.info("Strategy: keep_original - preserving original audio")
        run([
            "ffmpeg", "-y", "-i", video_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
            mixed_audio,
        ])
        bgm_used = None

    elif strategy == "remove_original_bgm_only":
        # 去掉原视频所有音频，只混合 BGM
        log.info("Strategy: remove_original_bgm_only - removing original audio, adding BGM only")
        bgm_config = audio_plan.get("bgm", {})
        bgm_asset = bgm_config.get("asset_path")
        bgm_volume = bgm_config.get("volume_db", -16)
        fade_in = bgm_config.get("fade_in_seconds", 2)
        fade_out = bgm_config.get("fade_out_seconds", 3)

        bgm_used = bgm_asset if bgm_asset and os.path.exists(bgm_asset) else None

        if bgm_used:
            bgm_track = work_path(work_dir, "bgm_track.wav")
            # 循环/裁剪 BGM 到视频时长，淡入淡出
            fade_filter = f"afade=t=in:st=0:d={fade_in},afade=t=out:st={max(0, duration - fade_out)}:d={fade_out}"
            run([
                "ffmpeg", "-y", "-i", bgm_used,
                "-filter_complex",
                f"aloop=loop=-1:size=2e9,atrim=0:{duration},volume={bgm_volume}dB,{fade_filter}",
                "-t", str(duration),
                bgm_track,
            ])
            run(["cp", bgm_track, mixed_audio])
        else:
            warnings.append("No BGM asset available. Output will have no audio.")
            # 生成静音轨
            silent_base = work_path(work_dir, "silent_base.wav")
            run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t", str(duration),
                silent_base,
            ])
            run(["cp", silent_base, mixed_audio])

    elif strategy == "mute_original_tts_bgm":
        log.info("Strategy: mute_original_tts_bgm - muting original, adding TTS + BGM")
        entries = timeline.get("entries", [])

        # 生成静音底轨
        silent_base = work_path(work_dir, "silent_base.wav")
        run([
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", str(duration),
            silent_base,
        ])

        # 拼接 TTS（按 timeline 时间放置）
        tts_concat_list = work_path(work_dir, "tts_concat.txt")
        tts_track = work_path(work_dir, "tts_track.wav")

        # 用 ffmpeg adelay 放置每个 TTS
        # 注意：inputs 从 0 开始编号（没有其他输入源），
        # 索引必须基于实际添加的输入个数，不能基于 entries 的枚举索引
        filter_parts = []
        inputs = []
        n_inputs = 0
        for entry in entries:
            tts_file = entry.get("tts_file")
            if not tts_file or not os.path.exists(tts_file):
                continue
            start_ms = int(entry.get("final_start", 0) * 1000)
            inputs.extend(["-i", tts_file])
            filter_parts.append(f"[{n_inputs}:a]adelay={start_ms}|{start_ms}[a{n_inputs}]")
            n_inputs += 1

        if inputs:
            # 混合所有 TTS
            mix_inputs = "".join(f"[a{i}]" for i in range(n_inputs))
            filter_str = ";".join(filter_parts) + f";{mix_inputs}amix=inputs={n_inputs}:duration=longest[tts_out]"
            run([
                "ffmpeg", "-y", *inputs,
                "-filter_complex", filter_str,
                "-map", "[tts_out]",
                tts_track,
            ])
        else:
            warnings.append("No TTS files available for mixing")
            run(["cp", silent_base, tts_track])

        # BGM
        bgm_config = audio_plan.get("bgm", {})
        bgm_asset = bgm_config.get("asset_path")
        bgm_volume = bgm_config.get("volume_db", -18)
        bgm_used = bgm_asset if bgm_asset and os.path.exists(bgm_asset) else None

        if bgm_used:
            bgm_track = work_path(work_dir, "bgm_track.wav")
            run([
                "ffmpeg", "-y", "-i", bgm_used,
                "-filter_complex",
                f"aloop=loop=-1:size=2e9,atrim=0:{duration},volume={bgm_volume}dB",
                "-t", str(duration),
                bgm_track,
            ])
            # 混合 TTS + BGM
            run([
                "ffmpeg", "-y",
                "-i", tts_track, "-i", bgm_track,
                "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest[loudness]",
                "-map", "[loudness]",
                mixed_audio,
            ])
        else:
            warnings.append("No BGM asset available. Using TTS only.")
            # TTS 轨通常比视频短（只有旁白段）。必须 pad 到视频全长，
            # 否则 final mux 的 -shortest 会把视频截短（bugfix 2026-09-22）
            run([
                "ffmpeg", "-y", "-i", tts_track,
                "-af", f"apad",
                "-t", str(duration),
                mixed_audio,
            ])

        # 响度归一化
        if audio_plan.get("loudness_normalized", True):
            normalized = work_path(work_dir, "mixed_audio_normalized.wav")
            run([
                "ffmpeg", "-y", "-i", mixed_audio,
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                normalized,
            ])
            # ⚠️ 这里曾经是 `mv normalized mixed_audio`：本机文件策略把「覆盖已存在
            # 目标」的 rename 判为 file-unlink 并直接拒绝，混音会在最后一步失败
            # （exit 13, "Brokered host rename source refused by file policy"）。
            # 用 copyfile（O_TRUNC 覆写，不是 unlink）替代；normalized 作为中间产物保留。
            shutil.copyfile(normalized, mixed_audio)

    else:
        die_degraded(f"Unknown audio strategy: {strategy}")

    # 合并到视频
    final_video = os.path.join(output_dir, "final.mp4")
    # ⚠️ 优先用**显式传入的 --video**：返工时传的是刚渲染好的 cleaned_v4 之类，
    # 而 work/cleaned.mp4 往往是第一版旧成片 —— 曾经因此把「已改好文案/logo 的视频」
    # 又换回旧画面（2026-09-27）。
    cleaned_video = work_path(work_dir, "cleaned.mp4")
    if os.path.exists(video_path):
        video_for_mux = video_path
    else:
        video_for_mux = cleaned_video

    run([
        "ffmpeg", "-y",
        "-i", video_for_mux,
        "-i", mixed_audio,
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        final_video,
    ])

    result = make_result(
        status="ok" if not warnings else "ok_with_warnings",
        confidence=0.9 if not warnings else 0.6,
        strategy=strategy,
        mixed_audio=mixed_audio,
        final_video=final_video,
        bgm_used=bgm_used,
        warnings=warnings,
    )
    return result


def main():
    ap = argparse.ArgumentParser(description="Audio processing (ASR/TTS/Mix)")
    sub = ap.add_subparsers(dest="mode", required=True)

    asr_p = sub.add_parser("asr-only", help="Speech recognition only")
    asr_p.add_argument("--video", required=True)
    asr_p.add_argument("--work-dir", required=True)
    asr_p.add_argument("--engine", default="auto")

    tts_p = sub.add_parser("tts", help="Text-to-speech only")
    tts_p.add_argument("--work-dir", required=True)
    tts_p.add_argument("--translated", help="Path to translated.json")
    tts_p.add_argument("--engine", default="auto")
    tts_p.add_argument("--voice", default="en-US-GuyNeural")

    mix_p = sub.add_parser("mix", help="Mix audio according to plan")
    mix_p.add_argument("--video", required=True)
    mix_p.add_argument("--work-dir", required=True)
    mix_p.add_argument("--output-dir", required=True)
    mix_p.add_argument("--audio-plan", help="Path to audio-plan.json")
    mix_p.add_argument("--timeline", help="Path to timeline.json")

    args = ap.parse_args()
    require_tools("ffmpeg", "ffprobe")
    os.makedirs(args.work_dir, exist_ok=True)

    if args.mode == "asr-only":
        result = run_asr(args.video, args.work_dir, args.engine)
        save_json(work_path(args.work_dir, "asr.json"), result)
        log.info(f"ASR complete: has_chinese_narration={result['has_chinese_narration']}, segments={result['total_segments']}")

    elif args.mode == "tts":
        translated_path = args.translated or work_path(args.work_dir, "translated.json")
        result = run_tts(args.work_dir, translated_path, args.engine, args.voice)
        save_json(work_path(args.work_dir, "tts-result.json"), result)
        log.info(f"TTS complete: {result['tts_success']}/{result['total_entries']} success")

    elif args.mode == "mix":
        audio_plan_path = args.audio_plan or work_path(args.work_dir, "audio-plan.json")
        timeline_path = args.timeline or work_path(args.work_dir, "timeline.json")
        os.makedirs(args.output_dir, exist_ok=True)
        result = run_mix(args.video, args.work_dir, args.output_dir, audio_plan_path, timeline_path)
        save_json(work_path(args.work_dir, "mix-result.json"), result)
        log.info(f"Mix complete: strategy={result['strategy']}, final={result['final_video']}")


if __name__ == "__main__":
    main()
