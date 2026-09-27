#!/usr/bin/env python3
"""analyze_bgm.py - BGM 节奏分析与高潮对齐（2026-09-28 新增）

回答「BGM 的节奏怎么跟视频对上」这个问题。

  输入：BGM 音频 + （可选）work/rhythm.json
  输出：work/bgm-plan.json

做四件事：
  1. **实测 BPM**        —— 不采信曲库里手写的 bpm 字段，真的量一遍
  2. **实测能量曲线**    —— 找出 drop（能量跃升点）与能量低谷
  3. **对齐到视频高潮**  —— 让 BGM 的 drop 落在 `rhythm.climax_mid` 上，
                           靠 `start_offset` 实现（不是把 BGM 从头平铺）
  4. **给变速建议**      —— BPM 与视频剪辑节奏差得远时用 `atempo` 拉近
                           （顺带完成 §12.4 第 9 条的音频指纹变形）

⚠️ 为什么必须实测 BPM：
   `configs/bgm-library.yaml` 里的 `bpm` 是**人工登记值**，而 `music/sports/` 里
   大量曲目是 `*_variant.mp3`（已做过 atempo 变速做指纹变形）—— 变体的真实 BPM
   与母带**不同**，照抄登记值会错。登记值只用于初筛，最终以本脚本实测为准。

实现（无新依赖，numpy 手写；librosa 本机未装）：
   * 谱通量 onset envelope -> 自相关求 BPM -> 相位搜索求首拍时刻
   * 逐 0.25s RMS 得能量曲线 -> 差分找 drop
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

SR = 22050
BPM_RANGE = (60.0, 190.0)
ENERGY_HOP_SEC = 0.25


# ============================================================
# 读音频
# ============================================================
def load_audio(path, sr=SR):
    """ffmpeg 解码为单声道 float32（临时用管道，不落盘）。"""
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


# ============================================================
# BPM
# ============================================================
def onset_envelope(a, sr, win=1024, hop=512):
    """谱通量 onset envelope（半波整流），并给出帧时间。"""
    if len(a) < win * 4:
        return None, None
    n_frames = 1 + (len(a) - win) // hop
    window = np.hanning(win).astype(np.float32)
    # 只取到 8kHz（打击乐/鼓点主要能量区，减少高频噪声干扰）
    n_bins = win // 2 + 1
    keep = max(4, int(n_bins * 8000 / (sr / 2)))
    prev = None
    flux = np.empty(n_frames, dtype=np.float32)
    for i in range(n_frames):
        seg = a[i * hop: i * hop + win] * window
        mag = np.abs(np.fft.rfft(seg))[:keep]
        if prev is None:
            flux[i] = 0.0
        else:
            d = mag - prev
            flux[i] = float(np.sum(d[d > 0]))
        prev = mag
    if flux.max() > 0:
        flux = flux / flux.max()
    times = (np.arange(n_frames) * hop + win / 2) / float(sr)
    return flux, times


def estimate_bpm(env, sr, hop=512):
    """onset envelope 自相关 -> BPM + 置信度 + 首拍相位（秒）。"""
    if env is None or len(env) < 64:
        return None
    x = env - float(np.mean(env))
    if not np.any(x):
        return None
    ac = np.correlate(x, x, mode="full")[len(x) - 1:]
    if ac[0] <= 0:
        return None
    ac = ac / ac[0]

    fps = sr / float(hop)
    lo = max(1, int(round(fps * 60.0 / BPM_RANGE[1])))
    hi = min(len(ac) - 1, int(round(fps * 60.0 / BPM_RANGE[0])))
    if hi <= lo:
        return None
    lag = lo + int(np.argmax(ac[lo:hi + 1]))
    if lag <= 0:
        return None
    bpm = 60.0 * fps / lag
    conf = float(ac[lag])

    # 倍频修正到常用区间
    while bpm < 70:
        bpm *= 2
    while bpm > 180:
        bpm /= 2

    # 首拍相位：以最终 BPM 的周期在 onset 上做相位搜索，取 onset 响应最强处
    period_frames = 60.0 / bpm * fps
    best_phase, best_score = 0.0, -1.0
    n_ph = max(4, int(round(period_frames)))
    for p in range(n_ph):
        idx = np.arange(p, len(env), max(1.0, period_frames)).astype(int)
        idx = idx[idx < len(env)]
        if len(idx) == 0:
            continue
        score = float(np.mean(env[idx]))
        if score > best_score:
            best_score, best_phase = score, p
    phase_sec = float(best_phase) / fps

    return {"bpm": round(float(bpm), 1), "confidence": round(conf, 3),
            "beat_phase_sec": round(phase_sec, 3),
            "beat_grid_sec": round(60.0 / bpm, 3)}


# ============================================================
# 能量曲线 / drop
# ============================================================
def energy_curve(a, sr, hop_sec=ENERGY_HOP_SEC):
    hop = max(1, int(sr * hop_sec))
    n = len(a) // hop
    if n < 4:
        return None, None
    seg = a[: n * hop].reshape(n, hop)
    rms = np.sqrt((seg ** 2).mean(axis=1)) + 1e-9
    db = 20.0 * np.log10(rms)
    db = np.clip(db, -60.0, 0.0)
    # 平滑（3 点）
    k = np.ones(3, dtype=np.float32) / 3.0
    db = np.convolve(db, k, mode="same")
    times = (np.arange(n) + 0.5) * hop_sec
    return times, db


def find_drops(times, db, top_n=3):
    """drop = 能量从低到高的最大跃升点（一阶差分的正峰）。"""
    if times is None or len(db) < 8:
        return []
    d = np.diff(db)
    try:
        from scipy.signal import find_peaks
        dist = max(2, int(round(4.0 / ENERGY_HOP_SEC)))     # 至少隔 4s
        idx, _ = find_peaks(d, distance=dist, height=3.0)   # 跃升 >=3dB 才算
    except ImportError:
        idx = np.array([int(np.argmax(d))])
    order = np.argsort(d[idx])[::-1] if len(idx) else []
    out = []
    for i in order[:top_n]:
        p = int(idx[i])
        out.append({"t": round(float(times[p]), 2),
                    "rise_db": round(float(d[p]), 2),
                    "energy_after_db": round(float(db[min(p + 1, len(db) - 1)]), 1)})
    return out


def find_valley(times, db, drops=(), lo_frac=0.10, hi_frac=0.95, guard_sec=3.0):
    """能量最低谷（循环衔接点候选）。

    ⚠️ 必须避开 drop 前后。drop 之前**天然就是全曲最低点**，直接取全局最小值
    会选中「build-up 的谷底」，在那里跳回开头会把整个进入段切断。
    2026-09-28 实测 Wallpaper_v2：选出 88.88s，而 drop 在 89.12s —— 只差 0.24s，
    一听就会觉得"刚要起来又被打断"。
    """
    if times is None or len(db) < 8:
        return None
    lo = int(len(db) * lo_frac)
    hi = max(lo + 1, int(len(db) * hi_frac))
    seg = db[lo:hi].astype(np.float32).copy()
    # 用 1s 移动平均避免选到瞬时静音
    k = max(1, int(round(1.0 / ENERGY_HOP_SEC)))
    if len(seg) >= k:
        seg = np.convolve(seg, np.ones(k, dtype=np.float32) / k, mode="same")
    tseg = times[lo:hi]
    for d in drops:
        t0 = float(d.get("t", 0.0))
        seg[np.abs(tseg - t0) <= guard_sec] = np.inf
    if not np.any(np.isfinite(seg)):
        return None
    i = int(np.argmin(seg)) + lo
    return {"t": round(float(times[i]), 2), "db": round(float(db[i]), 1)}


# ============================================================
# 对齐
# ============================================================
def plan_alignment(bgm_dur, drops, valley, rhythm, beat_grid):
    """决定怎么把 BGM 放到视频上，使它的 drop 落在视频高潮。"""
    plan = {
        "method": "as_is",
        "start_offset_sec": 0.0,
        "expected_drop_at_sec": None,
        "target_sec": None,
        "notes": [],
    }
    if not rhythm:
        plan["notes"].append("无 rhythm.json，按 from-zero 平铺")
        return plan

    target = float(rhythm.get("climax_mid") or rhythm.get("climax") or 0.0)
    video_dur = float(rhythm.get("duration_sec") or 0.0)
    plan["target_sec"] = target
    if not drops or target <= 0:
        plan["notes"].append("BGM 未检出 drop 或视频无高潮点，不做偏移")
        return plan

    # 优先让**第一个 drop** 对齐视频高潮：偏移 = drop 时刻 - 高潮时刻
    drop_t = float(drops[0]["t"])
    offset = drop_t - target

    if offset >= 0:
        plan.update(method="offset", start_offset_sec=round(offset, 3),
                    expected_drop_at_sec=round(target, 2))
        plan["notes"].append(
            f"从 BGM 第 {offset:.2f}s 起播，使其 drop 落在视频 {target:.2f}s 处")
    else:
        # drop 比高潮更早：BGM 前段不够长，无法靠偏移推后。
        # 若差得不多，可用「循环一次」把后面的 drop 补到前面来。
        need = -offset
        if bgm_dur and bgm_dur > need:
            plan.update(method="loop_shift", start_offset_sec=round(need, 3),
                        expected_drop_at_sec=round(target, 2))
            plan["notes"].append(
                f"首个 drop 早于高潮 {need:.2f}s —— 改为跳过前 {need:.2f}s，"
                f"用后一个 drop 对齐（起始点落在 BGM 中段，听感更连续）")
        else:
            plan["notes"].append(
                f"BGM 首个 drop 早于视频高潮 {need:.2f}s 且无足够长度补偿，"
                f"保持从头播放")
    return plan


def plan_loop(bgm_dur, video_dur, valley):
    """BGM 短于视频时的循环方案。"""
    if not bgm_dur or not video_dur:
        return {"needed": False}
    if bgm_dur >= video_dur:
        return {"needed": False,
                "trim_to_sec": round(video_dur, 3),
                "note": "BGM 足够长，裁到视频时长即可"}
    loops = int(np.ceil(video_dur / bgm_dur))
    out = {
        "needed": True,
        "loops": loops,
        "loop_start_sec": (valley["t"] if valley else None),
        "note": ("循环播放；衔接点取 BGM 能量低谷"
                 + (f" {valley['t']:.2f}s" if valley else "（未检出，退化为整段循环）")),
    }
    return out


# ============================================================
# 主流程
# ============================================================
def match_quality(bpm_measured, rhythm):
    if not rhythm:
        return "unknown", None
    target = float(rhythm.get("bpm_mid") or 0.0)
    if target <= 0:
        return "unknown", None
    delta = abs(bpm_measured - target) / target
    if delta <= 0.03:
        return "exact", round(delta, 4)
    if delta <= 0.12:
        return "close", round(delta, 4)
    return "far", round(delta, 4)


def analyze(bgm_path, rhythm=None, track_name=None):
    require_tools("ffmpeg")
    warnings = []
    a, sr = load_audio(bgm_path)
    bgm_dur = round(len(a) / float(sr), 3)

    env, _ = onset_envelope(a, sr)
    bpm_info = estimate_bpm(env, sr)
    if not bpm_info:
        warnings.append("BPM 估计失败（音频过短或无 onset）")
        bpm_info = {"bpm": None, "confidence": 0.0,
                    "beat_phase_sec": None, "beat_grid_sec": None}

    times, db = energy_curve(a, sr)
    drops = find_drops(times, db)
    valley = find_valley(times, db, drops)

    quality, delta = match_quality(bpm_info.get("bpm"), rhythm)

    # 变速建议：让 BGM 的 BPM 去贴合视频剪辑节奏
    atempo = 1.0
    atempo_note = None
    if rhythm and bpm_info.get("bpm"):
        target = float(rhythm.get("bpm_mid") or 0.0)
        if target > 0:
            atempo = round(target / bpm_info["bpm"], 4)
            if atempo > 1.06 or atempo < 0.94:
                atempo_note = (f"所需 atempo={atempo:.3f} 超出 ±6% 的可接受范围 "
                               f"（观感/音质变化明显）—— 建议换曲而不是硬拉")
                warnings.append(atempo_note)
                atempo = 1.0
            elif abs(atempo - 1.0) >= 0.01:
                atempo_note = (f"atempo={atempo:.4f}：BPM "
                               f"{bpm_info['bpm']:.1f} -> {target:.1f}，"
                               f"同时起到 §12.4 要求的指纹变形作用")

    rhythm_summary = None
    if rhythm:
        rhythm_summary = {
            "duration_sec": rhythm.get("duration_sec"),
            "climax_mid": rhythm.get("climax_mid"),
            "bpm_mid": rhythm.get("bpm_mid"),
            "intensity_profile": rhythm.get("intensity_profile"),
        }

    alignment = plan_alignment(bgm_dur, drops, valley, rhythm, bpm_info.get("beat_grid_sec"))
    loop = plan_loop(bgm_dur, float(rhythm.get("duration_sec") or 0.0) if rhythm else 0.0, valley)

    return make_result(
        status="ok" if not warnings else "ok_with_warnings",
        confidence=round(min(0.9, (bpm_info.get("confidence") or 0.0) + 0.3), 3),
        warnings=warnings,
        track=track_name or os.path.basename(bgm_path),
        asset_path=os.path.abspath(bgm_path),
        bgm_duration_sec=bgm_dur,
        bpm_measured=bpm_info.get("bpm"),
        bpm_confidence=bpm_info.get("confidence"),
        beat_phase_sec=bpm_info.get("beat_phase_sec"),
        beat_grid_sec=bpm_info.get("beat_grid_sec"),
        bpm_match=quality,
        bpm_delta_ratio=delta,
        atempo=atempo,
        atempo_note=atempo_note,
        energy_curve=(
            [[round(float(t), 2), round(float(v), 1)]
             for t, v in zip(times[::4], db[::4])] if times is not None else []
        ),
        drops=drops,
        primary_drop=(drops[0]["t"] if drops else None),
        energy_valley=valley,
        alignment=alignment,
        loop=loop,
        rhythm=rhythm_summary,
    )


def main():
    ap = argparse.ArgumentParser(description="BGM 节奏分析与高潮对齐 -> bgm-plan.json")
    ap.add_argument("--bgm", required=True, help="BGM 音频文件")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--rhythm", help="rhythm.json 路径（默认 <work>/rhythm.json）")
    ap.add_argument("--track-name", help="曲目名（默认取文件名）")
    ap.add_argument("--out", help="输出路径（默认 <work>/bgm-plan.json）")
    args = ap.parse_args()

    rhythm_path = args.rhythm or work_path(args.work_dir, "rhythm.json")
    rhythm = load_json(rhythm_path) if os.path.exists(rhythm_path) else None

    res = analyze(args.bgm, rhythm, args.track_name)
    out = args.out or work_path(args.work_dir, "bgm-plan.json")
    save_json(out, res)
    al = res["alignment"]
    log.info(
        "bgm: %s | %.1fs | BPM %s (%s) | drops %s | 对齐 %s @ %.2fs",
        res["track"], res["bgm_duration_sec"], res["bpm_measured"],
        res["bpm_match"], [d["t"] for d in res["drops"]],
        al["method"], al.get("target_sec") or 0.0,
    )
    print(out)


if __name__ == "__main__":
    main()
