#!/usr/bin/env python3
"""
audio_processor.py - 音频处理（ASR / TTS / 混音）

三种模式：
  --asr-only: 语音转写，输出 asr.json（含 has_chinese_narration）
  --tts:      英文语音合成，输出 TTS 音频文件
  --mix:      混音（按 audio-plan.json 的 strategy）

音频策略（四选一，均无声源分离）：
  mute_original_tts_bgm:  静音原轨 + 英文 TTS + Instrumental BGM      （有中文旁白时的标准做法）
  keep_original:          完整保留原声（现场声/欢呼/环境音 + 原 BGM）
  remove_original_bgm_only: 丢掉原片**全部**音频，只铺授权 BGM        （名字有误导性，见 SKILL.md §3）
  duck_original_add_bgm:  保留原片全部音频但**压到很低**再叠授权 BGM   （2026-09-28 新增）
                          —— 用来解决"集锦片丢了现场声显得干"的问题。
                          ⚠️ 它**不是**声源分离：原 BGM 与现场声一起被压低（仍可闻），
                             只在 ASR 判定「无中文旁白」时可用（否则中文解说仍听得见）。
                          ⚠️ 名字里的 duck = **固定增益衰减**，不是 sidechain 动态闪避：
                             两条轨各乘一个常数再相加，全程比例不变，不会"人一说话音乐让路"。
                          ⚠️ 原片自身响度不可控（2026-09-28 实测本批素材 I 从 −10.2 到 −13.0 LUFS），
                             同一组参数在不同片子上"领先量"能差 3 倍（实测 +6.7dB vs +2.0dB）→
                             用之前先量原片与 BGM 的 `input_i` 再定 duck.volume_db。详见 SKILL.md §3。
  tts_keep_instrumental:  demucs 分离去掉中文解说、保留原 BGM/现场声 + 英文 TTS
                          （2026-10-27 新增，需先备好 work/instrumental.wav，见该分支注释）

⚠️ 关于 demucs：本机 conda 环境**已装** demucs 4.0.1（htdemucs 权重也在缓存里），
   但它是为「人声 / 伴奏」二分设计，**不能**把「观众现场声」从「音乐」里分出来
   （两者频谱重叠，会被归进同一个 stem）。所以不要以为装了 demucs 就能做
   "去 BGM 留现场声"——能落地的只有 duck_original_add_bgm。详见 SKILL.md §3。
   2026-09-28 实测补充：`vocals` 轨抓的是「人声频段的一切」——06 科比那条的
   vocals 轨 ASR 转出的是**英文歌词**（原 BGM 是一首英文歌），根本不是解说。

🎵 BGM 节奏匹配（2026-09-28 新增，落实"节奏要和视频一致、要有高潮起落"）：
   混音不再是从 BGM 第 0 秒平铺到片长，而是读 `work/bgm-plan.json`：
     * 曲线来源：`analyze_rhythm.py` 量视频（场景切换 + 运动强度）→ rhythm.json
                 `analyze_bgm.py`    量 BGM（实测 BPM + 能量曲线 + drop）→ bgm-plan.json
     * `atempo`：把 BGM 的 BPM 拉向视频剪辑节奏（顺带完成 §12.4 的指纹变形）
     * `offset`：跳过 BGM 开头，使其 drop 落在视频 `climax_mid`
     * `loop`  ：不够长则循环，衔接点取能量低谷（不在 drop 中间跳回开头）
   ⚠️ 曲库里手写的 `bpm` 字段**不可信**：实测 8 个可对照曲目 **8/8 偏差 >5%**，
      最差 31%（Breaktime 登记 132 / 实测 172）。选曲一律用
      `configs/bgm-bpm-measured.json` 的实测值。

🎙️ 英文解说的语气语调（2026-09-28 新增）：`--tts` 的 voice / rate / pitch 默认从
   `work/narration-style.json` 读，那份文件由 `analyze_narration.py` 按原解说的
   F0（判性别）、语速、能量算出来。并会先做 **ASR 幻觉检测** —— whisper 在纯音乐素材上
   会凭空造出「整 2 秒一段、反复重复、avg_logprob −0.8」的中文，据此生成英文解说
   等于无中生有。详见 analyze_narration.py 的 detect_asr_hallucination()。

ASR 完全不可用时：保守走 keep_original + WARNING
TTS 完全不可用时：静音原轨 + 无配音 + 英文字幕 + BGM → WARNING
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import List, Dict, Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    run, save_json, load_json, make_result, log,
    work_path, ensure_dir, require_tools, check_python_package,
    add_degradation, die_degraded,
)

# 无 narration-style.json 时的兜底音色（"Passion"，体育解说常用）
DEFAULT_TTS_VOICE = "en-US-GuyNeural"


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
            voice: str = None, rate: str = None, pitch: str = None,
            style_path: str = None) -> Dict:
    """英文 TTS 合成。

    ⭐ 风格化（2026-09-28）：voice / rate / pitch 默认从
    `work/narration-style.json` 的 `tts_params` 读 —— 那份文件由
    `analyze_narration.py` 按**原解说的声学特征**（F0 判性别、语速、能量）算出来，
    所以一条嘶吼式高光解说和一条慢条斯理的战术讲解，配出的英文腔调不再一样。
    显式传参可覆盖。
    """
    translated = load_json(translated_path, {})
    entries = translated.get("entries", [])
    tts_dir = ensure_dir(os.path.join(work_dir, "tts"))

    # 风格化参数：显式参数 > narration-style.json > 内置默认
    style_note = None
    if style_path is None:
        style_path = work_path(work_dir, "narration-style.json")
    style = load_json(style_path, {}) if os.path.exists(style_path) else {}
    params = (style.get("tts_params") or {}) if isinstance(style, dict) else {}
    if params:
        voice = voice or params.get("voice")
        rate = rate or params.get("rate")
        pitch = pitch or params.get("pitch")
        style_note = (f"{style.get('gender')}/{style.get('style')} "
                      f"F0={style.get('f0_median_hz')}Hz 语速={style.get('speech_rate_cps')}字/s")
    voice = voice or DEFAULT_TTS_VOICE
    if style_note:
        log.info(f"TTS 风格化：{voice} rate={rate} pitch={pitch}（来自 {style_note}）")
    else:
        log.info(f"TTS 使用默认音色 {voice}（无 narration-style.json）")

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
                cmd = ["edge-tts", "--voice", voice, "--text", text,
                       "--write-media", mp3_file]
                if rate:
                    cmd += ["--rate", rate]
                if pitch:
                    cmd += ["--pitch", pitch]
                run(cmd)
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
        rate=rate,
        pitch=pitch,
        style_source=(style_note or "default"),
        warnings=warnings,
    )
    return result


# ============================================================
# Mix
# ============================================================
def bgm_tone_filter(tone: Optional[Dict]) -> str:
    """把 `audio-plan.json` 的 `bgm.tone` 编译成一条 ffmpeg 滤镜（无配置返回空串）。

    用户 2026-10-16：「要鼓点，鼓不要打铁片的那种声音」—— 抱怨的是 BGM 里的
    hi-hat / cymbal。镲片是宽带瞬态噪声，能量几乎全在 **6–16 kHz**；而 kick 在
    40–110 Hz、snare 主体在 150–400 Hz、snare 的脆度在 2–6 kHz，**都不受
    8 kHz 搁架影响**。所以 `highshelf` 是「只削镲、不削鼓」的精准工具：实测把
    成品 7–15.5 kHz 能量占比从 5.2% 压到 2.3%（−56%），kick 瞬态计数不变。

    可用键（都在 `bgm.tone` 下）：
      * `high_shelf_hz` —— 搁架拐点（建议 6000–9000，越低越闷）
      * `high_shelf_db` —— 增益（负值；−4 轻微、−8 明显、−12 接近闷）
      * `peak_hz` / `peak_db` / `peak_octaves` —— **峰值均衡**（2026-10-16 新增）。
        编译成 `equalizer=...:width_type=o:w=<octaves>:g=<db>`。

    ⚠️ **为什么要加峰值均衡：搁架对"宽带镲"不够用**。搁架只能砍拐点**以上**，
    而实测 08 罗斯用的 `Ready Aim Fire`，其 hi-hat 是**从 1 kHz 一路铺到 16 kHz 的
    宽带瞬态**（逐 1/3 倍频程瞬态计数：1k=158、2k=187、4k=209、6.3k=214、8k=215 次/分，
    对照 kick 仅 70 次/分）。8 kHz 搁架 −10 dB 实测只把镲段落压低 **1.41 dB**、
    **瞬态计数一声没少**（213.8 → 213.8），基本无效。
    换成 `peak_hz=5000, peak_db=-14, peak_octaves=2.5` 后镲段落 −7.89 dB，
    而 kick(40–110) −0.04 dB、snare 主体(150–400) −0.09 dB —— 鼓完全没动。
    选定哪个方案必须**实测扫参数**，不要凭听感猜。
    """
    tone = tone or {}
    out = []
    try:
        hz = float(tone.get("high_shelf_hz") or 0)
        db = float(tone.get("high_shelf_db") or 0)
    except (TypeError, ValueError):
        hz, db = 0.0, 0.0
    if hz > 0 and abs(db) > 0.05:
        out.append(f"highshelf=f={hz:g}:g={db:g}")

    try:
        phz = float(tone.get("peak_hz") or 0)
        pdb = float(tone.get("peak_db") or 0)
        pw = float(tone.get("peak_octaves") or 0)
    except (TypeError, ValueError):
        phz, pdb, pw = 0.0, 0.0, 0.0
    if phz > 0 and abs(pdb) > 0.05:
        out.append(f"equalizer=f={phz:g}:width_type=o:w={(pw or 2.0):g}:g={pdb:g}")
    return ",".join(out)


def build_bgm_track(work_dir: str, bgm_asset: str, duration: float,
                    bgm_volume: float, fade_in: float, fade_out: float,
                    bgm_plan: Optional[Dict] = None,
                    tone: Optional[Dict] = None) -> str:
    """按 `work/bgm-plan.json` 的对齐方案生成 BGM 轨。

    ⭐ 2026-09-28 的变化：以前是「从 BGM 第 0 秒平铺到片长」—— BGM 的高潮与视频
    高潮毫无关系，用户反馈的"节奏对不上、没有高潮起落"就是这个。现在：

      0. 曲首垫片 —— `alignment.head_skip_sec`，输入级 `-ss` **截断**数字静音垫片
      1. `atempo`  —— 把 BGM 的 BPM 拉向视频的剪辑节奏
                      （同时天然完成 §12.4 第 9 条要求的音频指纹变形）
      2. 回卷（rotate）—— `atrim=start=<offset>` 的两段拼接，见下面 build 里的说明
      3. `aloop` —— 回卷之后的整曲还不够长才循环；循环体是**整曲**，
                      不是「切剩下的那一段」（旧实现的 bug，见下）

    ⭐⭐ 2026-10-16 的原则：**能零拼接就零拼接**。
    用户对 09 韦德的原话是「背景音乐怎么也是拼起来的呢，要一首完整的 BGM 不要多段拼凑」。
    只要「片长 ≤ 曲子的有用段」，就应该**一次连续播放**，`start_offset_sec` 保持 0，
    绝不为了把一个不存在的「高潮」对上而引入回卷 —— 回卷必然产生一次听得见的缝
    （切断 + 跳到曲子另一端 + 曲尾静音被搬到片中）。回卷只在「曲子真的比片子短」
    时才允许。

    ⚠️ `atrim` 之后**必须** `asetpts=N/SR/TB` 把时间戳归零：否则后面的 `afade`
       仍按 atrim 之前的时间戳算落点，淡出会跑到错误的位置上。

    ⭐ `tone`（2026-10-16 新增，来自 `audio-plan.json` 的 `bgm.tone`）：音色收敛。
    用户原话「要鼓点，鼓不要打铁片的那种声音」—— 抱怨的是 BGM 里的 hi-hat /
    cymbal。镲片是宽带瞬态噪声，能量几乎全在 **6–16 kHz**；而 kick 在 40–110 Hz、
    snare 主体在 150–400 Hz、snare 的脆度在 2–6 kHz，**都不受 8 kHz 搁架影响**。
    所以 `highshelf` 是"只削镲不削鼓"的精准工具，实测把成品 7–15.5 kHz 能量占比
    从 5.2% 压到 2.3%（−56%），kick 瞬态计数不变。
    """
    plan = bgm_plan or {}
    tone = tone or {}
    filters = []

    atempo = float(plan.get("atempo") or 1.0)
    if abs(atempo - 1.0) > 1e-3:
        # atempo 单次只接受 0.5–2.0；analyze_bgm 已把超出 ±6% 的值拦成 1.0
        filters.append(f"atempo={atempo:.4f}")

    # 起始偏移 = 「从 BGM 的第 offset 秒起播」。
    #
    # ⚠️ 2026-10-16 修掉一个能把整条片子毁掉的 bug。旧写法是
    #     `atrim=start=<offset>` 之后再 `aloop` —— 但 atrim 已经把前面切掉了，
    #     `aloop` 循环的是**切完之后剩下的那一段**，不是整曲。offset 一旦落在
    #     曲尾附近，循环体就只剩几秒：同一小段被反复搬运十几遍，而且每次都把
    #     曲尾的淡出一起带过来。实测 雷阿伦 xWT128：offset=144.12s / 曲长
    #     155.85s → 11.73s 的片段重复 **13.4 次**，每 11.75s 出现一个 **5.00s
    #     的静音空洞**，全片 **41% 是静音**（用户：「怎么老是一个调呢，
    #     而且也不连贯」）。互相关 0.993 证实就是这个拼接方式。
    #
    # 正确语义是**回卷（rotate）**：先播 [offset, 曲尾]，再回到 [0, offset] 接着播，
    # 之后整曲循环。用 `asplit` + 两段 `atrim` + `concat` 显式拼出来，
    # 这样循环体 = **整曲**，offset 只决定从哪儿进门。
    offset = float((plan.get("alignment") or {}).get("start_offset_sec") or 0.0)

    # 曲首的数字静音垫片（`alignment.head_skip_sec`）。
    #
    # ⚠️ 必须是**输入级 `-ss`（截断）**，不能写成 `-af atrim=start=` + 回卷：
    #    回卷会把垫片搬到**片尾**去（黑屏那 1 秒变成片尾凭空静音）。
    #    实测 `Black Vortex` 前 0.83s 低于 −60 dBFS；「从曲首平铺」若照铺，
    #    叠加默认 2s 淡入后，视频头 2 秒几乎没声。
    head_skip = float((plan.get("alignment") or {}).get("head_skip_sec") or 0.0)
    in_args = ["-ss", f"{head_skip:.3f}"] if head_skip > 0.02 else []
    if in_args:
        log.info("BGM 跳过曲首垫片 %.3fs（输入级 -ss，截断）", head_skip)

    tail_filters = [
        "aloop=loop=-1:size=2e9",
        f"atrim=0:{duration:.3f}",
        f"volume={bgm_volume}dB",
    ]

    # 音色收敛（去镲）。tone 缺省 = 不改动，保持历史行为。
    shelf = bgm_tone_filter(tone)
    if shelf:
        tail_filters.append(shelf)
        log.info("BGM 音色收敛：%s（压 hi-hat/cymbals）", shelf)

    if fade_in and fade_in > 0:
        tail_filters.append(f"afade=t=in:st=0:d={fade_in}")
    if fade_out and fade_out > 0:
        tail_filters.append(f"afade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out}")

    bgm_track = work_path(work_dir, "bgm_track.wav")
    log.info("BGM 对齐：atempo=%.4f offset=%.2fs（%s）",
             atempo, offset, (plan.get("alignment") or {}).get("method", "as_is"))

    if offset > 0.05:
        pre = (",".join(filters) + ",") if filters else ""
        graph = (
            f"[0:a]{pre}asplit=2[rt][rh];"
            f"[rt]atrim=start={offset:.3f},asetpts=N/SR/TB[rtail];"
            f"[rh]atrim=end={offset:.3f},asetpts=N/SR/TB[rhead];"
            f"[rtail][rhead]concat=n=2:v=0:a=1[rot];"
            f"[rot]{','.join(tail_filters)}[out]"
        )
        run(["ffmpeg", "-y", *in_args, "-i", bgm_asset, "-filter_complex", graph,
             "-map", "[out]", "-t", str(duration), bgm_track])
    else:
        run(["ffmpeg", "-y", *in_args, "-i", bgm_asset,
             "-af", ",".join(filters + tail_filters),
             "-t", str(duration), bgm_track])
    return bgm_track


def _has_audio_stream(path: str) -> bool:
    """该文件里到底有没有音轨。

    为什么需要：`keep_original` 的原声要从**源片**取，而标准 Job2 链路里传进来的
    `video_path` 是 `render_overlays.py` 产出的 `work/cleaned.mp4` —— 渲染时写了
    `-an`，**它没有音轨**。直接对它 `-vn -acodec pcm_s16le` 会得到
    "Output file #0 does not contain any stream"（2026-09-29 05 实测）。
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60).stdout.strip()
        return bool(out)
    except Exception:
        return False


def build_tts_track(entries: List[Dict[str, Any]], work_dir: str,
                    duration: float) -> tuple:
    """按 timeline 时间把各条 TTS cue 用 adelay 放到一条轨上。

    返回 (tts_track_path, n_used)。n_used=0 时返回静音底轨（沿用
    mute_original_tts_bgm 的旧行为）。从 mute_original_tts_bgm 分支抽出共用：
    tts_keep_instrumental（2026-10-27 新增）需要同样的放置逻辑。
    """
    # 生成静音底轨
    silent_base = work_path(work_dir, "silent_base.wav")
    run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", str(duration),
        silent_base,
    ])
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
        # ⚠️ 必须 normalize=0：amix 默认把每路输入除以输入数，64 条 cue 就是 -36dB，
        # 整条 TTS 轨会被压到 BGM 底下 20dB，成片听起来"只有 BGM 没有解说"
        # （2026-09-29 在 03 湖人 OK 上实锤：tts_track mean -57dB / max -36dB = 1/64）。
        # TTS cue 互不重叠，unity gain 相加是正确语义。
        mix_inputs = "".join(f"[a{i}]" for i in range(n_inputs))
        filter_str = ";".join(filter_parts) + f";{mix_inputs}amix=inputs={n_inputs}:duration=longest:normalize=0[tts_out]"
        run([
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filter_str,
            "-map", "[tts_out]",
            tts_track,
        ])
    else:
        run(["cp", silent_base, tts_track])
    return tts_track, n_inputs


def run_mix(video_path: str, work_dir: str, output_dir: str,
            audio_plan_path: str, timeline_path: str) -> Dict:
    """按 audio-plan.json 混音"""
    audio_plan = load_json(audio_plan_path, {})
    timeline = load_json(timeline_path, {})
    # BGM 节奏分析与高潮对齐（可选；由 analyze_bgm.py 产出）
    bgm_plan = load_json(work_path(work_dir, "bgm-plan.json"), {}) or {}

    strategy = audio_plan.get("strategy", "keep_original")
    probe = load_json(work_path(work_dir, "probe.json"), {})
    duration = probe.get("duration_sec", 0)

    # ⚠️ 「原声」必须从**源片**取：标准 Job2 链路传进来的 video_path 是
    # render_overlays 产出的 work/cleaned.mp4（渲染时 `-an`，**没有音轨**）。
    # 对它 `-vn -acodec pcm_s16le` 会得到 "Output file #0 does not contain any stream"
    # （2026-09-29 05 二十佳球实测，整条链路在最后一步 rc=1）。
    # 需要原声的策略（keep_original / duck_original_add_bgm）统一走这个解析。
    def _audio_source() -> str:
        src = (probe or {}).get("path") or (probe or {}).get("source_path") or ""
        if src and os.path.exists(src) and _has_audio_stream(src):
            log.info("audio source: %s (from probe.json, has audio track)",
                     os.path.basename(src))
            return src
        return video_path

    audio_src = _audio_source()

    mixed_audio = work_path(work_dir, "mixed_audio.wav")
    warnings = []

    def _normalize_loudness(path: str) -> None:
        """响度归一化到 −16 LUFS（就地覆盖）。

        ⚠️ 用 shutil.copyfile 而不是 `mv`：本机文件策略把「覆盖已存在目标」的 rename
        判为 file-unlink 并直接拒绝（exit 13, "Brokered host rename source refused
        by file policy"）。用 copyfile（O_TRUNC 覆写，不是 unlink）替代；
        normalized 作为中间产物保留。

        ⚠️⚠️ 必须**两遍**（2026-10-16 修正）。单遍 = loudnorm 的 dynamic 模式，
        它会**欠调**：实测 Harmful or Fatal（input_lra 16.7）只补了 +8.6 dB，
        目标 −16 只做到 −24。这不是参数问题 —— 动态模式在大动态素材上本就
        到不了目标。先用第一遍量出 measured_*，第二遍给 linear 增益一次到位。
        两遍都在同一份 mixed_audio.wav 上量/做，所以与分支无关。
        """
        if not audio_plan.get("loudness_normalized", True):
            return
        normalized = work_path(work_dir, "mixed_audio_normalized.wav")
        target = "I=-16:TP=-1.5:LRA=11"
        af = f"loudnorm={target}"
        probe = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
                     "-af", f"loudnorm={target}:print_format=json",
                     "-f", "null", "-"], check=False)
        m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', probe.stderr or "", re.S)
        if m:
            try:
                s = json.loads(m.group(0))
                # measured_thresh 是第一遍的**门限**，第二遍要原样喂回去；
                # 缺了它 linear 模式会自己再猜一次，结果又飘掉。
                af = (f"loudnorm={target}"
                      f":measured_I={s['input_i']}"
                      f":measured_TP={s['input_tp']}"
                      f":measured_LRA={s['input_lra']}"
                      f":measured_thresh={s['input_thresh']}"
                      f":offset={s['target_offset']}"
                      ":linear=true")
            except (ValueError, KeyError) as exc:
                log.warning("loudnorm 第一遍解析失败（%s），退回单遍", exc)
        else:
            log.warning("loudnorm 第一遍没有拿到测量值，退回单遍（可能欠调）")
        # ⚠️ `loudnorm` 内部固定跑在 192 kHz，**不给 -ar 时输出 WAV 就是 192 kHz**
        #    （实测 38MB/50s，最终还是要被 AAC 降回 44100，纯浪费，且个别编码器
        #    在 192k 上行为不可预期）。这里统一压回 44100，与全批成片一致。
        run(["ffmpeg", "-y", "-i", path, "-af", af, "-ar", "44100", normalized])
        shutil.copyfile(normalized, path)

    if strategy == "keep_original":
        # 保留原声（来源见上面 _audio_source 的说明）
        if not _has_audio_stream(audio_src):
            # 两边都没有音轨 -> 明确报错，别静默出一个没有声音的成品
            raise RuntimeError(
                f"keep_original: no audio stream in {audio_src} "
                f"(rendered video={video_path}, source="
                f"{(probe or {}).get('path') or 'unknown'})")
        log.info("Strategy: keep_original - preserving original audio "
                 "(from %s)", os.path.basename(audio_src))
        run([
            "ffmpeg", "-y", "-i", audio_src,
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
            # 按 bgm-plan 对齐（drop -> 视频高潮 / atempo / 能量低谷循环）
            bgm_track = build_bgm_track(work_dir, bgm_used, duration, bgm_volume,
                                        fade_in, fade_out, bgm_plan,
                                        bgm_config.get("tone"))
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

        # 响度归一化 —— ⚠️ 这一行曾经漏掉（2026-10-16 发现）。
        # 另外两个分支都有它，只有本分支没有，于是 `loudness_normalized: true`
        # 在本策略下**被静默忽略**：BGM 只吃到 `volume_db` 的衰减就出厂了。
        # 实测 03 奥尼尔：成品 −28.0 LUFS（= Metalmania 母带 −9.8 − 18），
        # 比 `loudnorm=I=-16` 的目标低 12 LU，整批 10 条同样偏低。
        _normalize_loudness(mixed_audio)

    elif strategy == "mute_original_tts_bgm":
        log.info("Strategy: mute_original_tts_bgm - muting original, adding TTS + BGM")
        entries = timeline.get("entries", [])

        tts_track, n_tts = build_tts_track(entries, work_dir, duration)
        if n_tts == 0:
            warnings.append("No TTS files available for mixing")

        # BGM
        bgm_config = audio_plan.get("bgm", {})
        bgm_asset = bgm_config.get("asset_path")
        bgm_volume = bgm_config.get("volume_db", -18)
        bgm_used = bgm_asset if bgm_asset and os.path.exists(bgm_asset) else None

        if bgm_used:
            # TTS 场景不做淡入淡出（人声是主体，BGM 突然升降会更突兀），
            # 但对齐仍然生效 —— 让 drop 落在视频高潮上。
            bgm_track = build_bgm_track(work_dir, bgm_used, duration, bgm_volume,
                                        0.0, 0.0, bgm_plan,
                                        bgm_config.get("tone"))
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
        _normalize_loudness(mixed_audio)

    elif strategy == "tts_keep_instrumental":
        # 2026-10-27 新增：保留原片 BGM/现场声、只去掉中文解说人声，英文 TTS 叠加。
        # 依赖声源分离（demucs --two-stems=vocals）：底轨 = work/instrumental.wav
        # （htdemucs 的 no_vocals stem，含原 BGM + 观众/现场声，不含解说人声）。
        # ⚠️ 分离由 Agent 预先完成（本脚本不内嵌 demucs 调用）：
        #   python -m demucs --two-stems=vocals -n htdemucs -o <work>/demucs <work>/source_audio.wav
        #   cp <work>/demucs/htdemucs/source_audio/no_vocals.wav <work>/instrumental.wav
        # ⚠️ htdemucs 是人声/伴奏二分：观众欢呼声可能部分泄进 vocals stem 丢失，
        #    属预期代价；入场前应先 ASR 校验 vocals stem 确实是中文解说。
        instrumental = work_path(work_dir, "instrumental.wav")
        if not os.path.exists(instrumental):
            die_degraded(
                "tts_keep_instrumental: work/instrumental.wav not found. "
                "Run demucs --two-stems=vocals first and copy no_vocals.wav "
                "to work/instrumental.wav")
        log.info("Strategy: tts_keep_instrumental - demucs instrumental + English TTS")
        entries = timeline.get("entries", [])
        tts_track, n_tts = build_tts_track(entries, work_dir, duration)
        if n_tts == 0:
            warnings.append("No TTS files available for mixing")
        # 伴奏 pad/trim 到片长，再与 TTS 叠加（normalize=0：两轨电平各自已定）
        run([
            "ffmpeg", "-y",
            "-i", instrumental, "-i", tts_track,
            "-filter_complex",
            f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"apad,atrim=0:{duration}[base];"
            "[1:a]aformat=sample_rates=44100:channel_layouts=stereo[tts];"
            "[base][tts]amix=inputs=2:duration=longest:normalize=0[loudness]",
            "-map", "[loudness]",
            mixed_audio,
        ])
        _normalize_loudness(mixed_audio)
        bgm_used = "demucs_no_vocals(instrumental.wav)"

    elif strategy == "duck_original_add_bgm":
        # 保留原片全部音频（现场声 / 观众欢呼 / 球鞋声 / 原 BGM），整体压到 duck_db，
        # 再把授权 BGM 叠上来。用来解决"集锦片丢掉现场声显得干"的问题（2026-09-28 新增）。
        #
        # ⚠️ 这不是声源分离：原 BGM 只是被压低、没有消失。
        #    所以仅当 ASR 判定「无中文旁白」时可用 —— 否则中文解说仍听得见。
        #    真正"去掉 BGM 但保留现场声"需要能区分「音乐 vs 观众噪声」的模型，
        #    demucs（人声/伴奏二分）做不到，见本文件头部说明与 SKILL.md §3。
        duck_config = audio_plan.get("duck") or {}
        duck_db = float(duck_config.get("volume_db", -15))
        bgm_config = audio_plan.get("bgm", {})
        bgm_asset = bgm_config.get("asset_path")
        bgm_volume = bgm_config.get("volume_db", -10)
        fade_in = bgm_config.get("fade_in_seconds", 2)
        fade_out = bgm_config.get("fade_out_seconds", 3)
        log.info(f"Strategy: duck_original_add_bgm - original at {duck_db}dB + BGM")

        orig_track = work_path(work_dir, "orig_ducked.wav")
        run([
            "ffmpeg", "-y", "-i", audio_src,
            "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
            "-af", f"volume={duck_db}dB",
            "-t", str(duration),
            orig_track,
        ])

        bgm_used = bgm_asset if bgm_asset and os.path.exists(bgm_asset) else None
        if bgm_used:
            bgm_track = work_path(work_dir, "bgm_track.wav")
            fade_filter = (
                f"afade=t=in:st=0:d={fade_in},"
                f"afade=t=out:st={max(0, duration - fade_out)}:d={fade_out}"
            )
            shelf = bgm_tone_filter(bgm_config.get("tone"))
            run([
                "ffmpeg", "-y", "-i", bgm_used,
                "-filter_complex",
                f"aloop=loop=-1:size=2e9,atrim=0:{duration},volume={bgm_volume}dB"
                + (f",{shelf}" if shelf else "")
                + f",{fade_filter}",
                "-t", str(duration),
                bgm_track,
            ])
            # normalize=0：两路电平均已各自 gain 过，不要让 amix 再按输入数除一次
            run([
                "ffmpeg", "-y",
                "-i", orig_track, "-i", bgm_track,
                "-filter_complex",
                "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[loudness]",
                "-map", "[loudness]",
                mixed_audio,
            ])
        else:
            warnings.append(
                "No BGM asset available. Kept original audio at duck level only "
                f"({duck_db}dB) - result may sound quiet."
            )
            run(["cp", orig_track, mixed_audio])

        _normalize_loudness(mixed_audio)

    else:
        die_degraded(f"Unknown audio strategy: {strategy}")

    # 合并到视频
    final_video = os.path.join(output_dir, "final.mp4")
    # ⚠️ 优先用**显式传入的 --video**：返工时传的是刚渲染好的 cleaned_v4 之类，
    # 而 work/cleaned.mp4 往往是第一版旧成片 —— 曾经因此把「已改好文案/logo 的视频」
    # 又换回旧画面（2026-09-27）。
    # ⚠️ 片源选择（2026-10-27 修正）：
    #  - 显式 --video 指向**非源片**（返修传 cleaned_v4 之类）→ 照旧优先用它；
    #  - 显式 --video 就是源片（run_pipeline 全新渲染时 clean 尚未跑完，
    #    job 开始时 cleaned.mp4 还不存在）→ 必须用 work/cleaned.mp4，
    #    否则 final = 带中文的原片（2026-10-27 超人瞬间实测：QA 67 帧残留）。
    cleaned_video = work_path(work_dir, "cleaned.mp4")
    if os.path.exists(video_path) and os.path.abspath(video_path) != os.path.abspath(
            (probe or {}).get("path") or video_path):
        video_for_mux = video_path
    elif os.path.exists(cleaned_video):
        video_for_mux = cleaned_video
    elif os.path.exists(video_path):
        video_for_mux = video_path
    else:
        video_for_mux = cleaned_video

    # ⚠️ 必须显式 `-map`（2026-10-16 踩坑）。不写 -map 时 ffmpeg 会自动挑流，
    #    两个输入都带音轨时**优先取更靠前那个输入的音频** —— 于是
    #    `--video <带音轨的成片>` 返修时，新混的音根本没被用上，成片音轨还是旧的，
    #    而命令 rc=0、视频流 MD5 也没变，静默骗过所有校验。
    #    标准 Job2 链路里 `video_for_mux` 是 render_overlays 产出的**无音轨**文件，
    #    所以一直没暴露；返修场景（--video 直接给成片）必踩。
    run([
        "ffmpeg", "-y",
        "-i", video_for_mux,
        "-i", mixed_audio,
        "-map", "0:v:0", "-map", "1:a:0",
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
    tts_p.add_argument("--voice", default=None,
                       help="音色（默认读 narration-style.json 的 tts_params.voice）")
    tts_p.add_argument("--rate", default=None, help="语速，如 +15%%")
    tts_p.add_argument("--pitch", default=None, help="音高，如 +20Hz")
    tts_p.add_argument("--style", default=None,
                       help="narration-style.json 路径（默认 <work>/narration-style.json）")

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
        result = run_tts(args.work_dir, translated_path, args.engine,
                         args.voice, args.rate, args.pitch, args.style)
        save_json(work_path(args.work_dir, "tts-result.json"), result)
        log.info(f"TTS complete: {result['tts_success']}/{result['total_entries']} success "
                 f"(voice={result.get('voice')} rate={result.get('rate')} "
                 f"pitch={result.get('pitch')})")

    elif args.mode == "mix":
        audio_plan_path = args.audio_plan or work_path(args.work_dir, "audio-plan.json")
        timeline_path = args.timeline or work_path(args.work_dir, "timeline.json")
        os.makedirs(args.output_dir, exist_ok=True)
        result = run_mix(args.video, args.work_dir, args.output_dir, audio_plan_path, timeline_path)
        save_json(work_path(args.work_dir, "mix-result.json"), result)
        log.info(f"Mix complete: strategy={result['strategy']}, final={result['final_video']}")


if __name__ == "__main__":
    main()
