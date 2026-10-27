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


def useful_end(times, db, drop_db=20.0):
    """曲尾的「有用端」：最后一个电平仍在（峰值 − drop_db）之上的位置。

    很多 Kevin MacLeod 的曲子结尾会**淡出到数字静音**（实测 08 罗斯用的
    `Ready Aim Fire` 最后 2.0s 低于 −55 dBFS、最后 5.2s 已在淡出；
    `Five Armies` 更极端，尾巴 5s 全是洞）。这段静音**本身不是问题** ——
    问题是**回卷**：回卷会把它当成循环体内容搬到片子中间去。

    所以「片子能不能装进 [0, useful_end]」是选 offset 时必须先问的问题：
    装得下就**从头平铺、绝不回卷**，回卷点也就不可能落在尾巴上。
    """
    if times is None or len(db) < 8:
        return None
    peak = float(np.max(db))
    seg = np.asarray(db, dtype=np.float32)
    thr = peak - float(drop_db)
    idx = np.flatnonzero(seg > thr)
    if idx.size == 0:
        return None
    return round(float(times[int(idx[-1])]), 2)


def useful_start(times, db, rise_db=20.0, min_gap=0.30):
    """曲首的「有用起音点」：第一个电平已升到（峰值 − rise_db）之上的位置。

    很多 Kevin MacLeod 的曲子开头有一截 **0.5–1.5s 的数字静音垫片**
    （`Black Vortex` 实测前 0.83s 低于 −60 dBFS，`Ready Aim Fire`、`Five Armies`
    也都有）。「从曲首平铺」**不应该把垫片也铺到视频开头** —— 叠加引擎默认的
    2s 淡入之后，片头近 2 秒几乎是死的，而 TikTok 的头 2 秒是留人的。

    所以平铺时把这里返回的值交给引擎做**输入级 `-ss`**（`head_skip_sec`），
    让 BGM 从第一个音起。注意这是**截断**、不是回卷：回卷会把垫片搬到片尾去。

    只有「开头确实安静了一段」才算垫片（`min_gap`）。本来就从前 0.1s 起音的曲子
    返回 `None`，避免把正常的起拍削掉。
    """
    if times is None or len(db) < 8:
        return None
    peak = float(np.max(db))
    idx = np.flatnonzero(np.asarray(db, dtype=np.float32) > peak - float(rise_db))
    if idx.size == 0:
        return None
    # times[i] 是窗**中心**，往前退一个窗宽，别切掉起音的瞬态
    t0 = float(times[int(idx[0])]) - ENERGY_HOP_SEC
    if t0 < min_gap:
        return None
    return round(max(0.0, t0), 2)


# ============================================================
# 对齐
# ============================================================
def plan_alignment(bgm_dur, drops, valley, rhythm, beat_grid, atempo=1.0,
                   bgm_useful_end=None, bgm_useful_start=None):
    """决定怎么把 BGM 放到视频上，使它的 drop 落在视频高潮。

    ⚠️ 所有位置都必须在**播放时间轴**上算，也就是 `atempo` 之后的时间轴：

      * 引擎的滤镜顺序是 `atempo → 旋转(atrim) → 循环 → 淡入淡出`，所以
        `start_offset_sec` 解释在**变速之后**的音频上；
      * 变速 s 之后曲长 = bgm_dur / s，曲内某点 t 挪到 t / s。

    2026-10-16 之前这里拿**原始时间轴**的 drop 位置去算偏移，还漏了 `/s`
    的换算：雷阿伦换曲时 s=0.9624，`start_offset` 差出 11.05s ≈ 138 BPM 下的
    25 拍，drop 根本落不到高潮上（这正是「节奏对不上」的来源之一）。

    同日再修一条，并当天定稿：**零拼接优先** —— 先问「片子装不装得下」，
    再问「drop 对不对得上」。判据是
    `片长 ≤ min(变速后曲长, 有用段末端/变速)`，**与 `intensity_profile` 无关**
    （起初只对 `flat` 生效，结果三条 `wave` 片子继续回卷、继续在片中留静音洞）。
    装得下时只剩两种结局：能纯偏移对齐就 `offset`，否则 `as_is` 平铺；
    `loop_shift` 只在曲子真的比片子短时才出现。详见下面「零拼接优先」一段。
    """
    plan = {
        "method": "as_is",
        "start_offset_sec": 0.0,
        "expected_drop_at_sec": None,
        "target_sec": None,
        "notes": [],
    }

    # 「从曲首平铺」的两条路径都要顺手跳掉曲首的数字静音垫片（见 useful_start）。
    head_skip = float(bgm_useful_start or 0.0)

    def _tile_from_head(reason):
        plan["notes"].append(reason)
        if head_skip > 0.05:
            plan["head_skip_sec"] = round(head_skip, 3)
            plan["notes"].append(
                f"曲首有 {head_skip:.2f}s 数字静音垫片 —— 用输入级 -ss 跳过"
                f"（截断而非回卷），BGM 从第一个音起，片头不留死寂")
        return plan

    if not rhythm:
        return _tile_from_head("无 rhythm.json，按 from-zero 平铺")

    target = float(rhythm.get("climax_mid") or rhythm.get("climax") or 0.0)
    plan["target_sec"] = target
    if not drops or target <= 0:
        return _tile_from_head("BGM 未检出 drop 或视频无高潮点，不做偏移")

    speed = float(atempo or 1.0) or 1.0
    play_len = bgm_dur / speed if bgm_dur else 0.0
    drop_t = float(drops[0]["t"]) / speed          # 变速后的 drop 位置

    # ⭐⭐ 2026-10-16 定稿：**零拼接优先** —— 只要片子装得进曲子的「有用段」，就不许回卷。
    #
    # 回卷 = 一次听得见的接缝（在片子中间切断、跳到曲子另一端，还会把曲尾的淡出/
    # 数字静音搬到片中）。用户对这件事的原话是「背景音乐怎么也是拼起来的呢，要一首
    # 完整的 BGM 不要多段拼凑」。所以：**回卷只在曲子真的比片子短时才允许**。
    #
    # 之前只在 `intensity_profile == "flat"` 时强制平铺，实测漏掉一大类：强度曲线是
    # `wave` 的片子，只要 drop 落在高潮**之后**、且偏移又塞不下剩余长度，引擎照样回卷
    # —— 03 奥尼尔 / 约基奇 / 马布里 三条全栽在这（片长 98.4/96.6/144.0s，曲长
    # 330.7/147.0/214.8s，本来一次就播得完）。
    #
    # 唯一能让「对齐」与「零拼接」同时成立的写法是**纯偏移**：
    #     offset ≥ 0  且  offset + 片长 ≤ 有用段末端
    # 从 offset 起播、一路播到片尾，全程不换轨。
    video_dur = float(rhythm.get("duration_sec") or 0.0)
    usable = play_len
    if bgm_useful_end:
        # ⚠️ useful_end 量在**原始时间轴**上，比较前必须换算到变速后（和 drop_t 一致）
        usable = min(play_len, float(bgm_useful_end) / speed)

    if video_dur and play_len and video_dur <= usable:
        offset = drop_t - target
        if offset >= 0 and (offset + video_dur) <= usable:
            plan.update(method="offset", start_offset_sec=round(offset, 3),
                        expected_drop_at_sec=round(target, 2))
            plan["notes"].append(
                f"片长 {video_dur:.2f}s 装得进曲子的有用段末端 {usable:.2f}s；"
                f"从 BGM（变速后）第 {offset:.2f}s 起播、播到 "
                f"{offset + video_dur:.2f}s，drop 落在视频 {target:.2f}s —— 零拼接")
            return plan
        return _tile_from_head(
            f"片长 {video_dur:.2f}s 装得进曲子的有用段末端 {usable:.2f}s，"
            f"但 drop 对齐会要求回卷（起点 {offset:+.2f}s）—— "
            f"宁可不对齐也不拼接，从曲首平铺一次播完")

    offset = drop_t - target
    if offset >= 0:
        plan.update(method="offset", start_offset_sec=round(offset, 3),
                    expected_drop_at_sec=round(target, 2))
        plan["notes"].append(
            f"从 BGM（变速后）第 {offset:.2f}s 起播，使其 drop 落在视频 {target:.2f}s 处")
    else:
        # drop 比高潮更早：无法靠「往后挪」解决，改用**回卷（rotate）**——
        # 从第 p 秒起播，播到曲尾后回到曲首，于是「回卷之后的那一个 drop」
        # 落在视频高潮上。解方程 p + target ≡ drop_t (mod 曲长)：
        #     p = (drop_t - target) mod 曲长
        #
        # ⚠️ 2026-10-16 修符号错误：旧代码写 `need = -offset`，那是**互补值**
        # （曲长 - p）而不是 p。实测 雷阿伦 xWT128「Five Armies」：曲长 155.85s、
        # drop 1.88s、高潮 146.0s → 正确 p = 11.73s，旧代码给 144.12s。后者让
        # 「从 144.12s 起播」只剩曲尾 11.73s，配合当时 aloop 的实现（循环的是
        # 切剩下的那一段，不是整曲）→ 同一小段重复 13.4 次、每 11.75s 一个 5s
        # 静音空洞，全片 41% 是静音。这才有了「老是一个调、也不连贯」。
        if play_len > 0:
            start = offset % play_len
            plan.update(method="loop_shift", start_offset_sec=round(start, 3),
                        expected_drop_at_sec=round(target, 2))
            plan["notes"].append(
                f"首个 drop 早于高潮 {abs(offset):.2f}s —— 从 BGM（变速后）第 "
                f"{start:.2f}s 起播，播到曲尾后回卷到曲首，回卷后的 drop 落在"
                f"视频 {target:.2f}s")
        else:
            plan["notes"].append(
                f"BGM 首个 drop 早于视频高潮 {abs(offset):.2f}s 且无足够长度补偿，"
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

    # 偏移量与循环都在**变速后**的时间轴上算（见 plan_alignment 的说明）
    play_len = (bgm_dur / atempo) if atempo else bgm_dur
    # 曲尾有用端（淡出/静音之前）与曲首起音点，同样按变速后的时间轴换算
    tail_end = useful_end(times, db)
    tail_end = round(tail_end / atempo, 2) if tail_end else None
    head_start = useful_start(times, db)
    head_start = round(head_start / atempo, 2) if head_start else None
    alignment = plan_alignment(bgm_dur, drops, valley, rhythm,
                               bpm_info.get("beat_grid_sec"), atempo=atempo,
                               bgm_useful_end=tail_end,
                               bgm_useful_start=head_start)
    loop = plan_loop(play_len,
                     float(rhythm.get("duration_sec") or 0.0) if rhythm else 0.0, valley)

    return make_result(
        status="ok" if not warnings else "ok_with_warnings",
        confidence=round(min(0.9, (bpm_info.get("confidence") or 0.0) + 0.3), 3),
        warnings=warnings,
        track=track_name or os.path.basename(bgm_path),
        asset_path=os.path.abspath(bgm_path),
        bgm_duration_sec=bgm_dur,
        bgm_useful_end_sec=tail_end,
        bgm_useful_start_sec=head_start,
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
