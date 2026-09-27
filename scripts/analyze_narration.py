#!/usr/bin/env python3
"""analyze_narration.py - 解说声学风格分析（2026-09-28 新增）

回答「原解说的语气、语调是什么样，英文解说该怎么配」。

  输入：原视频 + work/asr.json（用于定位解说时间段）
  输出：work/narration-style.json

量三个客观量，再映射成 edge-tts 参数：
  * 基频 F0 中位数  -> 性别 / 音高      -> voice + pitch
  * 语速（字/秒）   -> 节奏松紧          -> rate
  * 解说段能量      -> 情绪强度          -> 风格档（energetic / neutral / calm）

⚠️ 为什么不直接照抄 `audio_processor.py` 的硬编码默认值：
   过去 TTS 固定用 `en-US-GuyNeural` + 默认语速 —— 一条慢条斯理的战术讲解
   和一条嘶吼式的高光解说，配出来的英文是一模一样的腔调。
   「按原解说的语气语调」要落地，就得先把原解说量出来。

实现（numpy 手写自相关法提 F0，无新依赖）：
   逐 40ms 帧算 F0，只保留清晰度高（自相关峰/零延迟 > 0.3）的帧取中位数，
   避免把摩擦音、观众噪声算成基频。
"""

import argparse
import io
import os
import subprocess
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import save_json, load_json, make_result, log, work_path, require_tools  # noqa: E402

SR = 16000
CJK = lambda ch: "\u4e00" <= ch <= "\u9fff"  # noqa: E731

# 性别判定阈值（中文解说实测：男 95–165Hz，女 175–270Hz）
F0_SPLIT_HZ = 165.0
# 正常语速参考（字/秒）—— 低于/高于它分别映射到放慢/加快
SPEECH_RATE_REF = 4.5
# 最多分析多少秒解说（长片省时间；均匀采样）
MAX_ANALYZE_SEC = 90.0

# voice 映射表：(性别, 风格) -> edge-tts voice
VOICE_MAP = {
    ("male", "energetic"): ("en-US-GuyNeural", "Passion - 体育解说常用，情绪外放"),
    ("male", "neutral"):   ("en-US-AndrewNeural", "Warm, Confident - 沉稳可信"),
    ("male", "calm"):      ("en-US-ChristopherNeural", "Reliable, Authority - 权威娓娓道来"),
    ("female", "energetic"): ("en-US-AriaNeural", "Positive, Confident - 有冲劲"),
    ("female", "neutral"):   ("en-US-JennyNeural", "Friendly, Considerate - 亲切"),
    ("female", "calm"):      ("en-US-MichelleNeural", "Friendly, Pleasant - 温和"),
}


# ============================================================
# 音频
# ============================================================
def load_audio(path, sr=SR):
    cmd = ["ffmpeg", "-v", "error", "-i", path,
           "-ac", "1", "-ar", str(sr), "-f", "wav", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"decode failed: {r.stderr.decode('utf-8', 'ignore')[-300:]}")
    with wave.open(io.BytesIO(r.stdout), "rb") as w:
        n, ch = w.getnframes(), w.getnchannels()
        a = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def estimate_f0_track(a, sr, fmin=70.0, fmax=350.0, frame_ms=40, hop_ms=20):
    """逐帧自相关提 F0。返回 (f0 列表, 清晰度列表)。"""
    fl = max(64, int(sr * frame_ms / 1000.0))
    hp = max(1, int(sr * hop_ms / 1000.0))
    lo = int(sr / fmax)
    hi = int(sr / fmin)
    f0s, clar = [], []
    for i in range(0, len(a) - fl, hp):
        seg = a[i:i + fl]
        seg = seg - float(seg.mean())
        if float(np.sqrt((seg ** 2).mean())) < 1e-3:      # 静音
            continue
        ac = np.correlate(seg, seg, mode="full")[fl - 1:]
        if ac[0] <= 0 or hi >= len(ac):
            continue
        band = ac[lo:hi]
        if len(band) == 0:
            continue
        lag = lo + int(np.argmax(band))
        c = float(ac[lag] / ac[0])
        if c < 0.3:      # 清晰度不足：摩擦音/噪声，丢弃
            continue
        f0s.append(sr / float(lag))
        clar.append(c)
    return f0s, clar


# ============================================================
# ASR 幻觉检测
# ============================================================
def detect_asr_hallucination(segs, conf_threshold=-0.70, repeat_ratio=0.40,
                            fixed_dur_ratio=0.60):
    """判断 whisper 是不是在**没有清晰人声**时凭空造出了文本。

    whisper 在纯音乐/现场噪声上会输出高度规则的东西，2026-09-28 在 01 上实测到：
      * **时长整齐**：16 段里 15 段都是整 2.00s
      * **文本循环**：「我來做個高度 / 今天在吃飯 / 每天打飯 / 都請問說 / 都吃得好」反复
      * **置信度极低**：`avg_logprob` = −0.81（正常语音通常 > −0.5）

    ⚠️ 单条特征都可能误伤正常素材（真人解说也可能有重复口头禅），
       所以要求**至少两条同时命中**才判幻觉。

    为什么必须检出它：幻觉文本会让"有中文旁白"的判定为 true，
    于是给出「按原解说语气生成英文解说」的指令 —— 结果是**凭空造出与原片无关的旁白**，
    比漏掉一段解说严重得多。
    """
    from collections import Counter
    reasons = []
    if not segs:
        return False, reasons

    confs = [s.get("confidence") for s in segs if s.get("confidence") is not None]
    if confs:
        avg_conf = float(np.mean(confs))
        if avg_conf < conf_threshold:
            reasons.append(f"平均 avg_logprob {avg_conf:.2f} < {conf_threshold}")

    texts = [(s.get("text") or "").strip() for s in segs]
    texts = [t for t in texts if t]
    if texts:
        c = Counter(texts)
        rep = sum(v for v in c.values() if v > 1) / len(texts)
        if rep >= repeat_ratio:
            reasons.append(f"重复句占比 {rep:.0%} ≥ {repeat_ratio:.0%}")

    durs = [round(float(s["end"]) - float(s["start"]), 1) for s in segs]
    if len(durs) >= 5:
        cd = Counter(durs)
        top_dur, top_n = cd.most_common(1)[0]
        if top_n / len(durs) >= fixed_dur_ratio:
            reasons.append(
                f"{top_n}/{len(durs)} 段时长完全相同（{top_dur}s）—— 典型幻觉节奏")

    return (len(reasons) >= 2), reasons


# ============================================================
# 风格判定
# ============================================================
def classify(f0_median, rate_cps, energy_db, full_db):
    """-> (gender, style, reasons)"""
    gender = "male" if (f0_median or 0) < F0_SPLIT_HZ else "female"
    reasons = [f"F0 中位数 {f0_median:.0f}Hz -> {'男声' if gender=='male' else '女声'}"]

    # 情绪强度：解说比全片背景高出多少 dB
    rel = (energy_db - full_db) if (energy_db is not None and full_db is not None) else 0.0
    if rate_cps is None:
        style = "neutral"
        reasons.append("语速不可测（无解说文本）-> neutral")
    elif rate_cps >= 5.5 or rel >= 4.0:
        style = "energetic"
        reasons.append(f"语速 {rate_cps:.1f} 字/s / 相对能量 {rel:+.1f}dB -> 激昂")
    elif rate_cps <= 3.2 and rel < 1.0:
        style = "calm"
        reasons.append(f"语速 {rate_cps:.1f} 字/s / 相对能量 {rel:+.1f}dB -> 平缓")
    else:
        style = "neutral"
        reasons.append(f"语速 {rate_cps:.1f} 字/s / 相对能量 {rel:+.1f}dB -> 中性")
    return gender, style, reasons


def tts_params(gender, style, f0_median, rate_cps):
    voice, voice_note = VOICE_MAP[(gender, style)]

    # rate：以 4.5 字/s 为基准，映射到 ±%，并夹在 ±25% 内（超出会很机械）
    if rate_cps:
        pct = (rate_cps / SPEECH_RATE_REF - 1.0) * 100.0
    else:
        pct = 0.0
    pct = float(np.clip(pct, -25.0, 25.0))
    rate = f"{pct:+.0f}%"

    # pitch：相对同性别参考值的偏移，夹在 ±25Hz（过大很假）
    ref = 120.0 if gender == "male" else 210.0
    if f0_median:
        d = float(np.clip((f0_median - ref) * 0.35, -25.0, 25.0))
    else:
        d = 0.0
    pitch = f"{d:+.0f}Hz"

    return {"voice": voice, "rate": rate, "pitch": pitch,
            "voice_note": voice_note,
            "rate_note": f"原语速 {rate_cps:.1f} 字/s（基准 {SPEECH_RATE_REF}）" if rate_cps else "无文本",
            "pitch_note": f"原 F0 {f0_median:.0f}Hz（参考 {ref:.0f}Hz）" if f0_median else "F0 不可测"}


# ============================================================
# 主流程
# ============================================================
def analyze(video_path, asr=None, asr_path=None):
    require_tools("ffmpeg")
    warnings = []
    if asr is None and asr_path and os.path.exists(asr_path):
        asr = load_json(asr_path, {})

    segs = [s for s in ((asr or {}).get("segments") or []) if (s.get("text") or "").strip()]
    has_narration = bool((asr or {}).get("has_chinese_narration")) and bool(segs)

    a, sr = load_audio(video_path)
    full_dur = len(a) / float(sr)
    full_rms = float(np.sqrt((a ** 2).mean()) + 1e-9)
    full_db = 20.0 * np.log10(full_rms)

    if not has_narration or not segs:
        return make_result(
            status="ok", confidence=0.9,
            warnings=["未检出中文解说：无需英文解说，直接走 remove_original_bgm_only + BGM"],
            has_narration=False,
            full_duration_sec=round(full_dur, 3),
            tts_needed=False,
            tts_params=None,
        )

    # ⚠️ 先排幻觉：`has_chinese_narration=true` 可能是 whisper 在无清晰人声时
    # 凭空造出来的（见 detect_asr_hallucination）。漏掉这一步会生成假英文解说。
    halluc, hallu_reasons = detect_asr_hallucination(segs)
    if halluc:
        return make_result(
            status="ok_with_warnings", confidence=0.75,
            warnings=[
                "ASR 疑似幻觉（" + "；".join(hallu_reasons) + "）→ 判定为**无中文解说**。"
                "不要据此生成英文解说，否则会凭空造出与原片无关的旁白；"
                "音频按 remove_original_bgm_only 处理。若确有解说，请人工听一遍确认。"
            ],
            has_narration=False,
            asr_hallucination=True,
            hallucination_reasons=hallu_reasons,
            full_duration_sec=round(full_dur, 3),
            tts_needed=False,
            tts_params=None,
        )

    # 均匀采样解说片段（长片省时）
    total_speech = sum(max(0.0, float(s["end"]) - float(s["start"])) for s in segs)
    step = max(1, int(np.ceil(total_speech / MAX_ANALYZE_SEC))) if total_speech > MAX_ANALYZE_SEC else 1
    picked = segs[::step]
    if step > 1:
        warnings.append(f"解说总长 {total_speech:.0f}s > {MAX_ANALYZE_SEC:.0f}s，"
                        f"每 {step} 段采样 1 段做声学分析")

    pieces, speech_sec = [], 0.0
    for s in picked:
        t0, t1 = max(0.0, float(s["start"])), float(s["end"])
        if t1 <= t0:
            continue
        i0, i1 = int(t0 * sr), min(len(a), int(t1 * sr))
        if i1 - i0 > sr // 20:                 # 至少 50ms
            pieces.append(a[i0:i1])
            speech_sec += (i1 - i0) / float(sr)

    if not pieces:
        return make_result(
            status="degraded", confidence=0.3,
            warnings=["解说时间段无法从音轨切出"],
            has_narration=True, full_duration_sec=round(full_dur, 3),
            tts_needed=True, tts_params={"voice": "en-US-GuyNeural", "rate": "+0%", "pitch": "+0Hz"},
        )

    speech = np.concatenate(pieces)
    f0s, clar = estimate_f0_track(speech, sr)
    f0_median = round(float(np.median(f0s)), 1) if f0s else None
    clarity = round(float(np.median(clar)), 3) if clar else 0.0
    if f0_median is None:
        warnings.append("F0 不可测（清晰帧不足），性别按男声兜底")

    # 语速：只数汉字，除以**实际解说时长**（按 segments 全量算，不是采样后）
    text = "".join(s.get("text") or "" for s in segs)
    n_cjk = sum(1 for ch in text if CJK(ch))
    rate_cps = round(n_cjk / total_speech, 2) if total_speech > 0.2 else None

    sp_rms = float(np.sqrt((speech ** 2).mean()) + 1e-9)
    energy_db = round(20.0 * np.log10(sp_rms), 2)

    gender, style, reasons = classify(f0_median, rate_cps, energy_db, full_db)
    params = tts_params(gender, style, f0_median, rate_cps)

    return make_result(
        status="ok" if not warnings else "ok_with_warnings",
        confidence=round(min(0.9, 0.4 + clarity), 3),
        warnings=warnings,
        has_narration=True,
        full_duration_sec=round(full_dur, 3),
        narration_segments=len(segs),
        narration_total_sec=round(total_speech, 2),
        analyzed_sec=round(speech_sec, 2),
        f0_median_hz=f0_median,
        f0_clarity=clarity,
        speech_rate_cps=rate_cps,
        cjk_chars=n_cjk,
        speech_energy_db=energy_db,
        full_energy_db=round(full_db, 2),
        relative_energy_db=round(energy_db - full_db, 2),
        gender=gender,
        style=style,
        reasons=reasons,
        tts_needed=True,
        tts_params=params,
    )


def main():
    ap = argparse.ArgumentParser(description="解说声学风格分析 -> narration-style.json")
    ap.add_argument("--video", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--asr", help="asr.json 路径（默认 <work>/asr.json）")
    ap.add_argument("--out", help="输出路径（默认 <work>/narration-style.json）")
    args = ap.parse_args()

    asr_path = args.asr or work_path(args.work_dir, "asr.json")
    res = analyze(args.video, None, asr_path)
    out = args.out or work_path(args.work_dir, "narration-style.json")
    save_json(out, res)
    if res.get("has_narration"):
        p = res["tts_params"]
        log.info("narration: %s / %s | F0 %sHz 语速 %s字/s -> %s rate=%s pitch=%s",
                 res["gender"], res["style"], res["f0_median_hz"],
                 res["speech_rate_cps"], p["voice"], p["rate"], p["pitch"])
    else:
        log.info("narration: 未检出中文解说 -> 无需英文解说")
    print(out)


if __name__ == "__main__":
    main()
