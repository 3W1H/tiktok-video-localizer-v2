#!/usr/bin/env python3
"""analyze_source_audio.py - 原片音轨「结构诊断」：有没有人在说话、有没有自带 BGM。

为什么要有这一步（2026-11-13 事故复盘）
--------------------------------------
`analyze_narration.py` 只回答「有没有**中文**解说」。答「没有」之后，job 的既定动作是
`remove_original_bgm_only`（丢掉原片全部音频、只铺授权 BGM）。这对纯音乐素材没问题，
但下面两类素材会被毁掉：

| 素材 | 旧流程看到 | 旧流程动作 | 后果 |
|---|---|---|---|
| 英语现场解说、原片无音乐 | 无中文解说 | 丢整轨 | **把全片唯一的解说删了**，换成 10 分钟 BGM 垫片 |
| 现场声 + 原片自带 BGM | 无中文解说 | 丢整轨 | 丢掉观众声与原曲；用户反问「原片有 BGM 为什么不用原来的」 |

更糟的是这两件事**互相掩盖**：批处理为了省时间，直接把 ASR 标成
`skipped: batch uses remove_original_bgm_only`（实测 25 条里 **20 条** 是 skipped），
于是「决定删原声 → 不跑 ASR → 没有任何证据反对删原声」形成闭环。

用户两轮反馈都指向这个根因：
  * `乔丹40个经典时刻_四_`「BGM 太单调了，也太没有意思了」
  * `乔丹经典战役_科比第三次`「bgm 都是垃圾，如果原视频有 BGM 可以用原来的」

判定原则（本脚本的全部价值）
----------------------------
1. **语音必须取证，不能靠统计量猜。** 人声与「稀疏鼓点的电子乐」在频谱统计上高度重叠
   （实测 611s 英语解说+球馆：低频占比 4.6%、音节调制 0.32；而 Crunk Knight 是
   83% / 0.40 —— 单看这两项分不开）。可靠信号只有一个：ASR 的语言检测。
   所以 ASR 缺失时本脚本**主动跑 whisper 采样转写**（3×45s，约 20s 出结果）。
2. **音乐只看低频占比。** 有贝斯/底鼓的曲子 20–150Hz 占 51–83%，纯解说+场馆只有
   1–5%（11 条素材实测）。这不是精确分类器，但作为「原片有没有 BGM」的旁证够用。
3. **决策以语音为主轴。** 只要检出人在说话（任何语言），就绝不推荐删原声。

策略输出
--------
  has_speech && zh            -> mute_original_tts_bgm   （§12.3 硬规则）
  has_speech && !zh           -> keep_original           （★ 本轮修复的核心）
  !has_speech && has_music    -> keep_original           （原片自带 BGM，用户口径）
  !has_speech && !has_music   -> duck_original_add_bgm   （只有现场声：压低声+叠节奏）
  证据不足（ASR/whisper 都不可用且无低频）-> recommended_strategy = null + 强告警

**本脚本从不推荐 `remove_original_bgm_only`。** 那一档只能由 Agent 在「用户明确要求
换掉原曲」时显式选择，不能由「没检出中文」自动推导出来。

用法
----
  python scripts/analyze_source_audio.py --video src.mp4 --work-dir work
  python scripts/analyze_source_audio.py --video src.mp4 --work-dir work --no-asr   # 跳过取证
输出：`work/source-audio-profile.json`
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    run, save_json, load_json, make_result, log, work_path, require_tools,
)

SR = 16000
ASR_SAMPLES = 3          # 采样段数
ASR_SAMPLE_SEC = 45      # 每段长度


# ============================================================
# 音频载入
# ============================================================
def load_mono(path: str, sr: int = SR) -> np.ndarray:
    """解码成单声道 float32。视频也行（取第一条音轨）。"""
    fd, wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vn",
             "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", wav])
        w = wave.open(wav, "rb")
        n = w.getnframes()
        raw = w.readframes(n)
        w.close()
        if not raw:
            return np.zeros(0, np.float32)
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    finally:
        if os.path.exists(wav):
            os.remove(wav)


def rms_db(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    return float(20.0 * np.log10(max(float(np.sqrt(np.mean(x * x))), 1e-9)))


def band_distribution(x: np.ndarray, sr: int = SR) -> dict:
    from scipy import signal as sg
    if len(x) < 8192:
        return {}
    f, p = sg.welch(x, sr, nperseg=8192)
    bands = [(20, 80, "sub"), (80, 150, "low"), (150, 300, "lowmid"),
             (300, 1000, "mid"), (1000, 2000, "himid"), (2000, 4000, "presence"),
             (4000, 6000, "hfc"), (6000, 12000, "air"), (12000, 16000, "vhi")]
    tot = float(p[(f >= 20) & (f <= 16000)].sum()) + 1e-12
    return {name: round(float(p[(f >= a) & (f < b)].sum()) / tot * 100, 2)
            for a, b, name in bands}


def pulse_periodicity(x: np.ndarray, sr: int = SR) -> dict:
    """40-160Hz 包络自相关，在 50-200 BPM 找峰。有规律鼓点 -> score 高。"""
    from scipy import signal as sg
    if len(x) < sr * 4:
        return {"score": 0.0, "bpm": None}
    sos = sg.butter(4, [40, 160], btype="bandpass", fs=sr, output="sos")
    env = np.abs(sg.hilbert(sg.sosfiltfilt(sos, x)))
    rate = 200
    env = env[::max(1, sr // rate)]
    env = env - env.mean()
    if env.std() < 1e-6:
        return {"score": 0.0, "bpm": None}
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    ac = ac / (ac[0] + 1e-9)
    lo_lag = int(rate * 60 / 200)
    hi_lag = min(int(rate * 60 / 50), len(ac) - 1)
    if hi_lag <= lo_lag:
        return {"score": 0.0, "bpm": None}
    k = int(np.argmax(ac[lo_lag:hi_lag])) + lo_lag
    return {"score": round(float(ac[k]), 3), "bpm": round(60.0 * rate / k, 1)}


# ============================================================
# 语音取证：ASR 优先，缺失时主动跑 whisper 采样
# ============================================================
def _asr_evidence(asr: dict) -> dict | None:
    """从 asr.json 提取证据。被 skip 的 asr 不算证据（这正是事故根因）。"""
    if not asr:
        return None
    note = (asr.get("note") or "").lower()
    if asr.get("status") == "skipped" or "skipped" in note:
        return None
    segs = [s for s in (asr.get("segments") or []) if (s.get("text") or "").strip()]
    if not segs:
        return None
    lang = asr.get("language_detected") or asr.get("detected_language")
    if str(lang).lower() in ("unknown", "none", ""):
        # 老版 asr.json 把「非中文」一律写成 unknown（因为语言被钉死成 zh），
        # 这不是语言证据，交给调用方继续查
        lang = None
    return {
        "lang": lang,
        "segments": segs,
        "source": "asr.json",
        "prob": asr.get("language_probability"),
        # 原生 ASR 的中文判定（§12.3 硬门禁的既有证据），即使文本被判幻觉也要保住
        "zh_flag": bool(asr.get("has_chinese_narration")),
    }


def _whisper_probe(x: np.ndarray, sr: int = SR) -> dict | None:
    """均匀取 3 段转写，判语言 + 是否有真人说话。返回 None 表示引擎不可用。"""
    engine = None
    try:
        import faster_whisper  # noqa: F401
        engine = "faster_whisper"
    except ImportError:
        try:
            import whisper  # noqa: F401
            engine = "whisper"
        except ImportError:
            return None

    dur = len(x) / float(sr)
    seg_len = min(ASR_SAMPLE_SEC, max(10, int(dur // (ASR_SAMPLES + 1))))
    starts = [int(max(0, dur * f - seg_len / 2))
              for f in np.linspace(0.18, 0.86, ASR_SAMPLES)]
    clip = np.concatenate([x[s * sr:(s + seg_len) * sr] for s in starts])
    fd, wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        w = wave.open(wav, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(clip * 32767, -32768, 32767).astype("<i2").tobytes())
        w.close()

        segs, lang, prob = [], None, None
        if engine == "faster_whisper":
            from faster_whisper import WhisperModel
            model = WhisperModel("base", device="cpu", compute_type="int8")
            it, info = model.transcribe(wav, beam_size=5)
            lang = getattr(info, "language", None)
            prob = round(float(getattr(info, "language_probability", 0.0) or 0.0), 3)
            for s in it:
                if s.text.strip():
                    segs.append({"start": round(s.start, 2), "end": round(s.end, 2),
                                 "text": s.text.strip(),
                                 "confidence": round(getattr(s, "avg_logprob", -1), 3)})
        else:
            import whisper
            model = whisper.load_model("base")
            r = model.transcribe(wav, verbose=False)
            lang = r.get("language")
            for s in r.get("segments", []):
                if s.get("text", "").strip():
                    segs.append({"start": round(s["start"], 2), "end": round(s["end"], 2),
                                 "text": s["text"].strip(),
                                 "confidence": round(s.get("avg_logprob", -1), 3)})
        return {"lang": lang, "segments": segs, "source": f"{engine}_probe",
                "prob": prob}
    finally:
        if os.path.exists(wav):
            os.remove(wav)


def _is_hallucination(segs: list) -> bool:
    """纯音乐上 whisper 会造重复短句。复用 analyze_narration 的判定。"""
    try:
        from analyze_narration import detect_asr_hallucination
        hit, _ = detect_asr_hallucination(segs)
        return bool(hit)
    except Exception:  # noqa: BLE001
        # 退化判据：短句反复出现 + 平均 logprob 很低
        texts = [s["text"] for s in segs]
        if len(texts) < 3:
            return False
        uniq = len(set(texts)) / len(texts)
        lp = np.mean([s.get("confidence", -1) for s in segs])
        return uniq < 0.35 and lp < -0.9


# ============================================================
# 决策
# ============================================================
def decide(has_music: bool, has_speech: bool, lang: str | None,
           evidence: str) -> tuple[str | None, list]:
    reasons = []
    zh = bool(lang and str(lang).lower().startswith(("zh", "cmn", "yue")))

    if has_speech and lang and not zh:
        reasons.append(
            f"检出 {lang} 解说/对白（证据={evidence}）：remove_original_bgm_only 会把这轨"
            f"整段删掉 —— 2026-11-13「科比第三次」就是这么毁的。必须保留原声")
        return "keep_original", reasons

    if has_speech and zh:
        reasons.append("中文解说 -> mute_original_tts_bgm（§12.3：中文人声必须去掉）")
        if has_music:
            reasons.append("原片另有 BGM，但与本策略冲突时以「去中文人声」为先")
        return "mute_original_tts_bgm", reasons

    if has_speech:
        reasons.append(
            f"有人声但语言未判定（证据={evidence}）：宁可保留原声，也不赌它不是解说")
        return "keep_original", reasons

    if has_music:
        reasons.append("无人声 + 原片自带 BGM：用户口径「原视频有 BGM 可以用原来的」，"
                       "直接 keep_original，零版权风险")
        return "keep_original", reasons

    if evidence == "none":
        reasons.append(
            "**证据不足**：没有可用 ASR、也没有低频音乐特征。无法判断原片是否有解说，"
            "此时删原声属于赌博 —— 先人工听 15s 或补跑 ASR")
        return None, reasons

    reasons.append("无人声、无音乐（纯现场声/环境声）：压到低电平再叠节奏 BGM，"
                   "既留观众氛围又不至于死寂")
    return "duck_original_add_bgm", reasons


def analyze(video_path: str, asr_path: str | None = None,
            use_whisper: bool = True) -> dict:
    warnings = []
    x = load_mono(video_path)
    if x.size == 0:
        return make_result(status="fail", confidence=0.0,
                           warnings=["没有可解码的音轨"], recommended_strategy=None)

    dur = round(len(x) / float(SR), 3)
    if dur < 3:
        return make_result(status="fail", confidence=0.0, duration_sec=dur,
                           warnings=["音轨短于 3s，无法诊断"], recommended_strategy=None)

    peak = float(np.abs(x).max())
    loud = rms_db(x)
    bands = band_distribution(x)
    pulse = pulse_periodicity(x)
    low_ratio = round(float(bands.get("sub", 0)) + float(bands.get("low", 0)), 2)
    hf_ratio = round(float(bands.get("hfc", 0)) + float(bands.get("air", 0))
                     + float(bands.get("vhi", 0)), 2)

    # ---- 数字静音：没有可保留的内容音频 ----
    if peak < 0.005:
        return make_result(
            status="ok", confidence=0.95, duration_sec=dur,
            has_speech=False, has_music=False, speech_lang=None,
            speech_evidence="silence", recommended_strategy="remove_original_bgm_only",
            reasons=["整条音轨近乎数字静音（峰值 < -46 dBFS），没有内容音频可保留"],
            warnings=[], bands=bands,
            features={"peak": round(peak, 4), "rms_db": round(loud, 1)})

    # ---- 语音取证 ----
    ev = _asr_evidence(load_json(asr_path, {}) if asr_path and os.path.exists(asr_path) else {})
    if ev is None and use_whisper:
        log.info("asr.json 无有效证据（可能被 skip），改跑 whisper 采样取证…")
        ev = _whisper_probe(x)
        if ev is None:
            warnings.append("ASR 与 whisper 都不可用：语音判定只能靠低频/调制旁证")
    elif ev is not None and not ev.get("lang") and use_whisper:
        # 老版 asr.json 的语言字段不可信（全片被钉死 language="zh"，非中文一律写
        # unknown）。有文本但没语言时，补一次采样只为拿语言，不覆盖既有转写。
        log.info("asr.json 有文本但语言未知，补跑 whisper 采样判语言…")
        probe = _whisper_probe(x)
        if probe and probe.get("lang"):
            if not probe["segments"] or not _is_hallucination(probe["segments"]):
                ev["lang"] = probe["lang"]
                ev["source"] = ev["source"] + "+lang_probe"
    elif ev is None:
        warnings.append("--no-asr：未做语音取证，结论置信度低")

    has_speech, lang, evidence = False, None, "none"
    speech_sec = None
    if ev:
        segs = ev["segments"]
        lang = ev["lang"]
        joined = " ".join(s["text"] for s in segs)
        cjk = sum(1 for c in joined if '\u4e00' <= c <= '\u9fff')
        zh_in_text = cjk / max(len(joined.replace(" ", "")), 1) > 0.3
        if segs and _is_hallucination(segs):
            if zh_in_text:
                # ⚠️ 关键区分：幻觉判定管的是「这段转写能不能拿去生成英文 TTS」，
                #    不等于「音轨里没有中文人声」。中文解说被 whisper 转成重复短句
                #    （固定 2.0s 一段）时会被判幻觉 —— 若因此把 has_speech 打成
                #    False，就会漏掉 §12.3 的硬门禁（中文人声必须去掉），
                #    实测「科比屠龙名场面」正是这种情况（39 段、chinese_ratio 1.0）。
                warnings.append(
                    "转写疑似幻觉，但文本以汉字为主：仍判定**有中文人声**（§12.3）；"
                    "这份转写不可直接用于生成英文 TTS，需重跑或人工核")
            else:
                warnings.append(
                    "ASR 文本疑似幻觉（纯音乐上的重复短句）：不据此判为「有解说」")
                segs = []
        speech_sec = round(sum(max(0.0, s["end"] - s["start"]) for s in segs), 1)
        if segs:
            has_speech = True
            evidence = ev["source"]
            if not lang:
                if zh_in_text:
                    lang = "zh"
        elif ev.get("zh_flag"):
            # 转写被清空但原生 ASR 已判过中文旁白：中文是硬门禁，不能因为
            # 「转写质量差」就把它当成没人说话
            has_speech = True
            lang = "zh"
            evidence = ev["source"]
            warnings.append("asr.json 标记 has_chinese_narration=true（转写不可用）："
                            "仍按中文人声处理")
        else:
            # 判为幻觉/空文本：语言标签不可信（纯音乐上 whisper 会报 Latin/English 等），
            # 输出 la 之类会把下游误导成「有外语解说」，必须清掉
            warnings.append(f"whisper 检出 {lang} 但判定为幻觉，语言标签已丢弃")
            lang = None

    # ---- 音乐判定：低频占比为主，鼓点规律性为辅（阈值见模块 docstring）----
    has_music = bool(low_ratio >= 20.0 or (pulse["score"] >= 0.45 and pulse["bpm"]))

    strategy, reasons = decide(has_music, has_speech, lang, evidence)

    conf = 0.9 if evidence in ("asr.json", "faster_whisper_probe", "whisper_probe") else 0.45
    if strategy is None:
        conf = 0.3
    return make_result(
        status="ok" if not warnings else "ok_with_warnings",
        confidence=round(conf, 2),
        duration_sec=dur,
        has_speech=has_speech,
        has_music=has_music,
        speech_lang=lang,
        speech_evidence=evidence,
        speech_sec=speech_sec,
        low_ratio=low_ratio,
        hf_ratio=hf_ratio,
        recommended_strategy=strategy,
        reasons=reasons,
        warnings=warnings,
        bands=bands,
        features={
            "low_20_150_pct": low_ratio,
            "hf_6_16k_pct": hf_ratio,
            "pulse_score": pulse["score"],
            "pulse_bpm": pulse["bpm"],
            "rms_db": round(loud, 1),
            "peak": round(peak, 4),
        },
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="原片音轨结构诊断（解说/自带BGM/策略建议）")
    ap.add_argument("--video", required=True, help="源视频或任意音频文件")
    ap.add_argument("--work-dir", help="输出目录（默认与 --video 同级）")
    ap.add_argument("--asr", help="asr.json 路径（默认 <work>/asr.json）")
    ap.add_argument("--out", help="默认 <work>/source-audio-profile.json")
    ap.add_argument("--no-asr", action="store_true",
                    help="不做 whisper 采样取证（快，但置信度低）")
    a = ap.parse_args()
    require_tools("ffmpeg")

    work = a.work_dir or os.path.dirname(os.path.abspath(a.video))
    os.makedirs(work, exist_ok=True)
    asr = a.asr or work_path(work, "asr.json")

    res = analyze(a.video, asr, use_whisper=not a.no_asr)
    out = a.out or work_path(work, "source-audio-profile.json")
    save_json(out, res)

    f = res.get("features") or {}
    log.info("原声：has_speech=%s lang=%s（证据 %s）| has_music=%s（低频 %s%%）",
             res.get("has_speech"), res.get("speech_lang"), res.get("speech_evidence"),
             res.get("has_music"), f.get("low_20_150_pct"))
    log.info("建议策略：%s", res.get("recommended_strategy"))
    for r in res.get("reasons") or []:
        log.info("  · %s", r)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
